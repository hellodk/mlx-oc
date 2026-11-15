#!/usr/bin/env python3
"""
mlx_server_log_tailer.py - stream mlx_lm.server log lines to the OTLP gateway.

Reads the server log incrementally, classifies each new line with a regex
table into (severity, category), and ships qualifying lines as OTel LogRecords
to the OTLP gateway. All configuration comes from environment variables -
nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import sys
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

from mlx_logging import debug, error, info, parse_listen_addr, setup_logging

DEFAULT_POLL_INTERVAL = 0.5
DEFAULT_MAX_RATE = 50.0
DEFAULT_BACKFILL = 0
PARTIAL_LINE_FLUSH_TIMEOUT_S = 2.0
MAX_PARTIAL_LINE_BYTES = 65536
MAX_TRACEBACK_LINES = 50
TRACEBACK_FLUSH_TIMEOUT_S = 1.5

_EXC_LINE_RE = re.compile(
    r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit|Warning)(?:\s*:|$)"
)

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
        re.compile(r'"(?:POST|GET|DELETE|PUT)\s+/[^"]*"\s+5\d\d\b'),
        "error",
        "http",
    ),
    (
        re.compile(r'"(?:POST|GET|DELETE|PUT)\s+/[^"]*"\s+4\d\d\b'),
        "warning",
        "http",
    ),
    (
        re.compile(r'"(?:POST|GET|DELETE|PUT)\s+/[^"]*"\s+[123]\d\d\b'),
        "info",
        "http",
    ),
    (
        re.compile(r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b"),
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
                 backfill: int, project: str, macbook: str,
                 verify_tls: bool = True):
        self.log_path = log_path
        self.include_http = include_http or all_lines
        self.all_lines = all_lines
        self.backfill = max(backfill, 0)
        self.project = project
        self.macbook = macbook
        self.verify_tls = verify_tls
        self.stop = threading.Event()
        self._bucket = TokenBucket(rate=rate, capacity=max(rate, 50.0))
        self._provider = None
        self._logger = None
        self._processor = None
        self._traceback_lines: list[str] = []
        self._traceback_started_at: float | None = None
        self._prepare_exporter(exporter)

    def _prepare_exporter(self, endpoint: str) -> None:
        import requests
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
        session = requests.Session()
        session.verify = self.verify_tls
        provider = LoggerProvider(resource=resource)
        processor = BatchLogRecordProcessor(
            OTLPLogExporter(
                endpoint=sig_url(endpoint, "logs"),
                headers={"projectName": self.project},
                timeout=5,
                session=session,
            ),
            max_queue_size=2048,
            max_export_batch_size=256,
            schedule_delay_millis=5000,
        )
        provider.add_log_record_processor(processor)
        set_logger_provider(provider)
        self._provider = provider
        self._processor = processor
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

    def _update_queue_gauge(self) -> None:
        q = getattr(self._processor, "_queue", None)
        if q is None:
            return
        try:
            qsize_fn = getattr(q, "qsize", None)
            if callable(qsize_fn):
                G_QUEUE.set(qsize_fn())
            else:
                G_QUEUE.set(len(q))
        except (AttributeError, NotImplementedError, TypeError):
            pass

    def _flush_traceback(self) -> None:
        if not self._traceback_lines:
            return
        lines = self._traceback_lines
        self._traceback_lines = []
        self._traceback_started_at = None
        G_LINES.labels(severity="error", category="python").inc(len(lines))
        if not self._bucket.take():
            G_DROPPED.labels(reason="rate_limit").inc()
            return
        try:
            self._emit("\n".join(lines), "error", "python")
        except Exception as exc:
            G_ERRORS.inc()
            error(f"export error: {exc!r}", component="logtailer")

    def run(self) -> None:
        partial_buf = bytearray()
        partial_started_at: float | None = None

        def _flush_partial() -> None:
            nonlocal partial_started_at
            if not partial_buf:
                partial_started_at = None
                return
            text = bytes(partial_buf).decode("utf-8", "replace")
            partial_buf.clear()
            partial_started_at = None
            self._handle_line(text)
            self._update_queue_gauge()

        initial_open = True
        while not self.stop.is_set():
            if not self.log_path.exists():
                info(f"{self.log_path} not found; waiting for it",
                     component="logtailer")
                while not self.stop.is_set() and not self.log_path.exists():
                    self.stop.wait(1.0)
                if self.stop.is_set():
                    break

            try:
                with open(self.log_path, "rb") as fh:
                    st = os.fstat(fh.fileno())
                    cur_inode = (st.st_dev, st.st_ino)
                    partial_buf.clear()
                    partial_started_at = None
                    if initial_open:
                        initial_open = False
                        if self.backfill:
                            fh.seek(0, os.SEEK_END)
                            size = fh.tell()
                            approx_bytes = self.backfill * 200
                            if size > approx_bytes:
                                fh.seek(size - approx_bytes)
                                fh.readline()
                            else:
                                fh.seek(0)
                        else:
                            fh.seek(0, os.SEEK_END)
                    else:
                        fh.seek(0)
                    G_UP.set(1)

                    while not self.stop.is_set():
                        chunk = fh.readline()
                        if chunk:
                            G_POS.set(fh.tell())
                            partial_buf.extend(chunk)
                            if chunk.endswith(b"\n") or len(partial_buf) >= MAX_PARTIAL_LINE_BYTES:
                                _flush_partial()
                            elif partial_started_at is None:
                                partial_started_at = time.monotonic()
                            continue

                        now = time.monotonic()
                        if (
                            partial_buf
                            and partial_started_at is not None
                            and (now - partial_started_at) >= PARTIAL_LINE_FLUSH_TIMEOUT_S
                        ):
                            _flush_partial()
                        if (
                            self._traceback_lines
                            and self._traceback_started_at is not None
                            and (now - self._traceback_started_at) >= TRACEBACK_FLUSH_TIMEOUT_S
                        ):
                            self._flush_traceback()

                        self._update_queue_gauge()
                        # Detect rename rotation or copytruncate
                        try:
                            path_st = os.stat(self.log_path)
                            if (path_st.st_dev, path_st.st_ino) != cur_inode:
                                info(f"{self.log_path} rotated (inode changed); reopening",
                                     component="logtailer")
                                _flush_partial()
                                self._flush_traceback()
                                break
                            if path_st.st_size < fh.tell():
                                info(f"{self.log_path} truncated; seeking to 0",
                                     component="logtailer")
                                partial_buf.clear()
                                partial_started_at = None
                                self._flush_traceback()
                                fh.seek(0)
                                G_POS.set(0)
                        except FileNotFoundError:
                            info(f"{self.log_path} missing during rotation; waiting",
                                 component="logtailer")
                            _flush_partial()
                            self._flush_traceback()
                            break
                        self.stop.wait(DEFAULT_POLL_INTERVAL)
            except OSError as exc:
                G_ERRORS.inc()
                G_UP.set(0)
                error(f"log tailer I/O error on {self.log_path}: {exc!r}",
                      component="logtailer")
                self.stop.wait(1.0)
        _flush_partial()
        self._flush_traceback()

    def _handle_line(self, raw: str) -> None:
        line = raw.rstrip("\n").rstrip("\r")
        if not line:
            if self._traceback_lines:
                self._flush_traceback()
            return
        if "Traceback (most recent call last):" in line:
            if self._traceback_lines:
                self._flush_traceback()
            self._traceback_lines = [line]
            self._traceback_started_at = time.monotonic()
            return
        if self._traceback_lines:
            is_continuation = (
                line.startswith((" ", "\t"))
                or "During handling of the above exception" in line
                or "The above exception was the direct cause" in line
            )
            if is_continuation and len(self._traceback_lines) < MAX_TRACEBACK_LINES:
                self._traceback_lines.append(line)
                return
            if _EXC_LINE_RE.match(line) and len(self._traceback_lines) < MAX_TRACEBACK_LINES:
                self._traceback_lines.append(line)
                self._flush_traceback()
                return
            self._flush_traceback()

        severity, category = classify(line)
        G_LINES.labels(severity=severity, category=category).inc()

        if category == "http" and severity == "info" and not self.include_http:
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
            self.send_header("Content-Length", "0")
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
    ap.add_argument(
        "--listen",
        default=os.environ.get("MLX_LOGTAILER_LISTEN", "127.0.0.1:9106"),
    )
    ap.add_argument(
        "--otlp-verify-tls",
        action="store_true",
        default=os.environ.get("OTLP_VERIFY_TLS", "false").lower() == "true",
        help="verify TLS certificates for OTLP endpoints",
    )
    ap.add_argument(
        "--max-rate",
        type=float,
        default=float(os.environ.get("MLX_LOGTAILER_MAX_RATE", str(DEFAULT_MAX_RATE))),
        help="max lines/s shipped to OTLP (0 = unlimited)",
    )
    ap.add_argument(
        "--backfill",
        type=int,
        default=int(os.environ.get("MLX_LOGTAILER_BACKFILL", str(DEFAULT_BACKFILL))),
        help="ship last N log lines on start (crash forensics)",
    )
    ap.add_argument("--include-http", action="store_true",
                    help="also ship HTTP access lines")
    ap.add_argument("--all", dest="all_lines", action="store_true",
                    help="ship every line (no severity/category filter)")
    args = ap.parse_args()

    device_id = (
        os.environ.get("MLX_DEVICE_ID")
        or os.environ.get("MLX_MACBOOK_ID")
        or "unknown"
    )
    setup_logging(service="mlx-logtailer", macbook=device_id)

    if not args.log_file:
        error("MLX_SERVER_LOG not set and --log-file not given; cannot run",
              component="logtailer")
        sys.exit(1)
    if not args.otlp_endpoint:
        error("MLX_OTLP_ENDPOINT not set and --otlp-endpoint not given; cannot run",
              component="logtailer")
        sys.exit(1)

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
        macbook=device_id,
        verify_tls=args.otlp_verify_tls,
    )

    host, port = parse_listen_addr(args.listen, default_host="0.0.0.0", default_port=9106)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server = ThreadingHTTPServer((host, port), Handler)

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
        t.join(timeout=2.0)
        try:
            if tailer._provider:
                tailer._provider.force_flush(timeout_millis=3000)
                tailer._provider.shutdown()
        except Exception as exc:
            debug(f"opentelemetry provider shutdown failed: {exc!r}", component="logtailer")
        server.server_close()


if __name__ == "__main__":
    main()
