#!/usr/bin/env python3
"""
mlx_server_log_tailer.py - stream mlx_lm.server log lines to the OTLP gateway.

Reads the server log incrementally, classifies each new line with a regex
table into (severity, category), and ships qualifying lines as OTel LogRecords
to the OTLP gateway. All configuration comes from environment variables -
nothing is hardcoded.
"""

import argparse
import os
import re
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)

from mlx_logging import degraded, debug, error, info, setup_logging

DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_MAX_RATE = 50.0
DEFAULT_BACKFILL = 0

SEVERITY_NUMBER = {"debug": 1, "info": 9, "warning": 13, "error": 17, "fatal": 21}

CLASSIFIERS = [
    (
        re.compile(
            r"\[METAL\]|Command buffer execution failed|kIOGPUCommandBuffer|"
            r"GPU error|METAL command buffer|Execution of the command buffer",
            re.I,
        ),
        "error",
        "gpu",
    ),
    (
        re.compile(r"\[WARN\]|Node with rank|rank\s+\d+.*(failed|down|offline)|"
                   r"exited with code|Lost connection|peer.*closed", re.I),
        "warning",
        "rank",
    ),
    (
        re.compile(r"\b(Traceback|Fatal Python error|SIGABRT|SIGSEGV|SIGKILL|"
                   r"segmentation fault|KeyboardInterrupt|killed|OutOfMemory|"
                   r"CUDA|MLX error|RuntimeError)\b", re.I),
        "error",
        "python",
    ),
    (
        re.compile(r"\b(error|failed|fault)\b", re.I),
        "error",
        "python",
    ),
    (
        re.compile(r"\b(tokens/s|ttft|gen:.*ms|generation|prefill|prompt"
                   r" cache|prompt processing|processing progress|cache"
                   r" sequence|context length|kv cache)\b", re.I),
        "info",
        "generation",
    ),
    (re.compile(r"(POST|GET|DELETE|PUT) /"), "info", "http"),
]

FALLBACK = ("info", "other")

G_UP = Gauge("mlx_log_tailer_up", "1 if the server-log tailer is running")
G_LINES = Counter(
    "mlx_log_tailer_lines_total", "server.log lines classified",
    ["severity", "category"],
)
G_SHIPPED = Counter(
    "mlx_log_tailer_shipped_total", "lines shipped to OTLP", ["severity"],
)
G_DROPPED = Counter(
    "mlx_log_tailer_dropped_total",
    "lines dropped (rate-limited or filtered out)", ["reason"],
)
G_ERRORS = Counter(
    "mlx_log_tailer_errors_total", "read / export errors",
)
G_POS = Gauge("mlx_log_tailer_file_bytes", "current read offset in server.log")
G_QUEUE = Gauge("mlx_log_tailer_queue_depth", "lines buffered awaiting export")


def sig_url(endpoint: str, signal_name: str) -> str:
    """Return the full OTLP URL for a signal, e.g. <endpoint>/v1/logs."""
    url = endpoint.rstrip("/")
    return url if url.endswith(f"/v1/{signal_name}") else f"{url}/v1/{signal_name}"


def classify(line: str) -> tuple[str, str]:
    """Return (severity, category) for a log line."""
    for rx, severity, category in CLASSIFIERS:
        if rx.search(line):
            return severity, category
    return FALLBACK


class TokenBucket:
    """Simple token bucket used as the flood-protection rate limiter."""

    def __init__(self, rate: float, capacity: float):
        self.rate = max(rate, 0.0)
        self.capacity = max(capacity, 1.0)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()

    def take(self) -> bool:
        if self.rate == 0.0:
            return True
        now = time.monotonic()
        self.tokens = min(self.capacity,
                          self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Tailer:
    def __init__(self, log_path: Path, exporter: str, rate: float,
                 include_http: bool, all_lines: bool,
                 backfill: int, project: str, macbook: str):
        self.log_path = log_path
        self.include_http = include_http or all_lines
        self.all_lines = all_lines
        self.backfill = max(backfill, 0)
        self.project = project
        self.macbook = macbook
        self.stop = threading.Event()
        self._bucket = TokenBucket(rate=rate, capacity=max(rate, 50.0))
        self._provider = None
        self._logger = None
        self._prepare_exporter(exporter)

    def _prepare_exporter(self, endpoint: str) -> None:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import HOST_NAME, SERVICE_NAME, Resource

        resource = Resource.create({
            SERVICE_NAME: "mlx-lm-server",
            HOST_NAME: socket.gethostname(),
            "mlx.node.name": self.macbook,
        })
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=sig_url(endpoint, "logs"),
                    headers={"projectName": self.project},
                    timeout=5,
                ),
                max_queue_size=2048,
                max_export_batch_size=256,
                schedule_delay_millis=5000,
            )
        )
        set_logger_provider(provider)
        self._provider = provider
        self._logger = provider.get_logger("mlx.log.tailer")

    def _emit(self, line: str, severity: str, category: str) -> None:
        self._logger.emit(
            body=line,
            timestamp=time.time_ns(),
            observed_timestamp=time.time_ns(),
            severity_text=severity,
            severity_number=SEVERITY_NUMBER.get(severity, 9),
            attributes={
                "mlx.log.severity": severity,
                "mlx.log.category": category,
                "mlx.log.source": "mlx_lm.server",
                "mlx.log.project": self.project,
                "mlx.node.name": self.macbook,
            },
        )
        G_SHIPPED.labels(severity=severity).inc()

    def run(self) -> None:
        if not self.log_path.exists():
            info(f"{self.log_path} not found; waiting for it",
                 component="logtailer")
            while not self.stop.is_set() and not self.log_path.exists():
                time.sleep(1.0)

        with open(self.log_path, "rb") as fh:
            if self.backfill:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 1))
                while fh.tell() > 0 and size - fh.tell() < self.backfill * 200:
                    fh.seek(fh.tell() - 1)
                fh.readline()
            else:
                fh.seek(0, 2)

            line = fh.readline()
            while not self.stop.is_set():
                if line:
                    G_POS.set(fh.tell())
                    self._handle_line(line.decode("utf-8", "replace"))
                    line = fh.readline()
                    continue
                time.sleep(DEFAULT_POLL_INTERVAL)
                line = fh.readline()

    def _handle_line(self, raw: str) -> None:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            return
        severity, category = classify(line)
        G_LINES.labels(severity=severity, category=category).inc()

        if category == "http" and not self.include_http:
            G_DROPPED.labels(reason="http_filter").inc()
            return
        if not self._bucket.take():
            G_DROPPED.labels(reason="rate_limit").inc()
            return
        try:
            self._emit(line, severity, category)
        except Exception as exc:
            G_ERRORS.inc()
            error(f"export error: {exc!r}", component="logtailer")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description="MLX server-log -> OTLP tailer")
    ap.add_argument("--log-file", default=os.environ.get("MLX_SERVER_LOG"))
    ap.add_argument("--otlp-endpoint", default=os.environ.get("MLX_OTLP_ENDPOINT"))
    ap.add_argument("--project", default=os.environ.get("OPIK_PROJECT", "mlx"))
    ap.add_argument("--listen", default=os.environ.get("MLX_LOGTAILER_LISTEN",
             "0.0.0.0:9106"))
    ap.add_argument("--max-rate", type=float,
             default=float(os.environ.get("MLX_LOGTAILER_MAX_RATE",
                                          str(DEFAULT_MAX_RATE))),
             help="max lines/s shipped to OTLP (0 = unlimited)")
    ap.add_argument("--backfill", type=int,
             default=int(os.environ.get("MLX_LOGTAILER_BACKFILL",
                                        str(DEFAULT_BACKFILL))),
             help="ship last N log lines on start (crash forensics)")
    ap.add_argument("--include-http", action="store_true",
                    help="also ship HTTP access lines")
    ap.add_argument("--all", dest="all_lines", action="store_true",
                    help="ship every line (no severity/category filter)")
    args = ap.parse_args()

    if not args.log_file:
        error("MLX_SERVER_LOG not set and --log-file not given; cannot run",
              component="logtailer")
        sys.exit(1)
    if not args.otlp_endpoint:
        error("MLX_OTLP_ENDPOINT not set and --otlp-endpoint not given; cannot run",
              component="logtailer")
        sys.exit(1)

    setup_logging(service="mlx-logtailer",
                  macbook=os.environ.get("MLX_MACBOOK_ID", "unknown"))

    G_UP.set(1)
    info(
        f"file={args.log_file} -> otlp={args.otlp_endpoint} "
        f"project={args.project} rate<= {args.max_rate}/s "
        f"http={'in' if args.include_http else 'excluded'} "
        f"-> metrics on {args.listen}",
        component="logtailer",
    )

    tailer = Tailer(
        log_path=Path(args.log_file),
        exporter=args.otlp_endpoint,
        rate=args.max_rate,
        include_http=args.include_http,
        all_lines=args.all_lines,
        backfill=args.backfill,
        project=args.project,
        macbook=os.environ.get("MLX_MACBOOK_ID", "unknown"),
    )

    host, _, port = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port or 9106)), Handler)

    def _shutdown(signum, frame):
        tailer.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    t = threading.Thread(target=tailer.run, daemon=True)
    t.start()

    try:
        server.serve_forever()
    finally:
        tailer.stop.set()
        try:
            if tailer._provider:
                tailer._provider.force_flush(timeout_millis=3000)
                tailer._provider.shutdown()
        except Exception as exc:
            debug(f"opentelemetry provider shutdown failed: {exc!r}", component="log_tailer")
        server.server_close()


if __name__ == "__main__":
    main()
