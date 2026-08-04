#!/usr/bin/env python3
"""mlx_server_log_tailer.py - stream mlx_lm.server log lines to the OTLP gateway.

Reads cluster/logs/server.log incrementally (seek-to-end on start), classifies
each new line with a regex table into (severity, category), and ships the
qualifying lines as OTel LogRecords to the otel-collector (logs pipeline ->
durable JSONL file). Per-request LLM traces go to Opik separately from the
proxy; Opik has no OTLP logs ingestion, so runtime logs stop at the collector.

Default filter keeps WARN / ERROR / METAL-GPU / rank / generation lines and
drops HTTP access lines; use --include-http or --all to widen. Flood protection
is a token-bucket rate limiter (--max-rate) on top of the SDK's bounded batch
buffer; rate-limited lines are counted as dropped, not silently swallowed.

Self-health: Prometheus metrics on 0.0.0.0:9106 (mirrors the KV-cache agent).
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

DEFAULT_LOG = str(Path(__file__).resolve().parent / "logs" / "server.log")
DEFAULT_OTLP = "http://192.168.1.64:4318"
DEFAULT_PROJECT = "mlx"
POLL_INTERVAL = 0.5

SEVERITY_NUMBER = {"debug": 1, "info": 9, "warning": 13, "error": 17, "fatal": 21}

# (compiled regex, severity, category). First match wins; HTTP is last so a
# line carrying an error keyword is not miscast as an access log.
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
        re.compile(r"\b(tokens/s|ttft|gen:.*ms|generation|prefill|prompt:"
                   r"|context length|kv cache)\b", re.I),
        "info",
        "generation",
    ),
    (re.compile(r"(POST|GET|DELETE|PUT) /"), "info", "http"),
]

# Anything that did not match a classifier gets this fallback so a flood of
# unknown output can still be rate-limited and dropped explicitly.
FALLBACK = ("info", "other")

G_UP = Gauge("mlx_log_tailer_up", "1 if the server-log tailer is running")
G_LINES = Counter(
    "mlx_log_tailer_lines_total", "server.log lines classified",
    ["severity", "category"],
)
G_SHIPPED = Counter(
    "mlx_log_tailer_shipped_total", "lines shipped to Opik", ["severity"],
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


def sig_url(endpoint, signal_name):
    """Return the full OTLP URL for a signal, e.g. <endpoint>/v1/logs."""
    url = endpoint.rstrip("/")
    return url if url.endswith(f"/v1/{signal_name}") else f"{url}/v1/{signal_name}"


def classify(line):
    """Return (severity, category) for a log line."""
    for rx, severity, category in CLASSIFIERS:
        if rx.search(line):
            return severity, category
    return FALLBACK


class TokenBucket:
    """Simple token bucket used as the flood-protection rate limiter."""

    def __init__(self, rate, capacity):
        self.rate = max(rate, 0.0)
        self.capacity = max(capacity, 1)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()

    def take(self):
        if self.rate == 0.0:
            return True
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class Tailer:
    def __init__(self, log_path, exporter, rate, include_http, all_lines,
                 backfill, project):
        self.log_path = Path(log_path)
        self.include_http = include_http or all_lines
        self.all_lines = all_lines
        self.backfill = max(backfill, 0)
        self.project = project
        self.stop = threading.Event()
        self._bucket = TokenBucket(rate=rate, capacity=max(rate, 50.0))

        self._prepare_exporter(exporter)

    def _prepare_exporter(self, endpoint):
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import HOST_NAME, SERVICE_NAME, Resource

        resource = Resource.create({
            SERVICE_NAME: "mlx-lm-server",
            HOST_NAME: socket.gethostname(),
            "mlx.node.name": "rank0",
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

    def _emit(self, line, severity, category):
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
            },
        )
        G_SHIPPED.labels(severity=severity).inc()

    def run(self):
        if not self.log_path.exists():
            print(f"[logtailer] {self.log_path} not found; waiting for it", flush=True)
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
                fh.seek(0, 2)  # no historical backfill: start at current end

            line = fh.readline()
            while not self.stop.is_set():
                if line:
                    G_POS.set(fh.tell())
                    self._handle_line(line.decode("utf-8", "replace"))
                    line = fh.readline()
                    continue
                time.sleep(POLL_INTERVAL)
                line = fh.readline()

    def _handle_line(self, raw):
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
            print(f"[logtailer] export error: {exc!r}", flush=True)


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
    ap = argparse.ArgumentParser(description="MLX server-log -> Opik tailer")
    ap.add_argument("--log-file", default=os.environ.get("MLX_SERVER_LOG", DEFAULT_LOG))
    ap.add_argument("--otlp-endpoint", default=os.environ.get("MLX_OTLP_ENDPOINT", DEFAULT_OTLP))
    ap.add_argument("--project", default=os.environ.get("OPIK_PROJECT", DEFAULT_PROJECT))
    ap.add_argument("--listen", default=os.environ.get("MLX_LOGTAILER_LISTEN", "0.0.0.0:9106"))
    ap.add_argument("--max-rate", type=float, default=50.0,
                    help="max lines/s shipped to Opik (0 = unlimited)")
    ap.add_argument("--backfill", type=int, default=0,
                    help="ship last N log lines on start (crash forensics)")
    ap.add_argument("--include-http", action="store_true",
                    help="also ship HTTP access lines")
    ap.add_argument("--all", dest="all_lines", action="store_true",
                    help="ship every line (no severity/category filter)")
    args = ap.parse_args()

    G_UP.set(1)
    print(
        f"[logtailer] file={args.log_file} -> otlp={args.otlp_endpoint} "
        f"project={args.project} rate<= {args.max_rate}/s "
        f"http={'in' if args.include_http else 'excluded'} "
        f"-> metrics on {args.listen}",
        flush=True,
    )

    tailer = Tailer(
        log_path=args.log_file,
        exporter=args.otlp_endpoint,
        rate=args.max_rate,
        include_http=args.include_http,
        all_lines=args.all_lines,
        backfill=args.backfill,
        project=args.project,
    )

    host, _, port = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port or 9106)), Handler)

    def _shutdown(signum, frame):
        tailer.stop.set()
        # serve_forever() only returns after shutdown() (called from another
        # thread); this is what lets the finally block below run and release
        # the :9106 socket promptly on SIGTERM.
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
            tailer._provider.force_flush(timeout_millis=3000)
            tailer._provider.shutdown()
        except Exception:
            pass
        server.server_close()


if __name__ == "__main__":
    main()
