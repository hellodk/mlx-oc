#!/usr/bin/env python3
"""
mlx_logging.py
==============
Shared structured JSON logging for all mlx monitoring exporters.

Logs one JSON object per line to stderr plus a rotating file copy under
the edge logs directory. Fields:

  ts, level, service, component, macbook, msg, [err, trace_id, span_id]

Usage:

    from mlx_logging import setup_logging, log, info, error, warning, debug, degraded

    setup_logging(service="mlx-proxy", macbook=os.environ.get("MLX_MACBOOK_ID", "unknown"))
    info("proxy listening on :8080", component="proxy")
    error("upstream unreachable", component="proxy")
    degraded("MLX_MODEL not set; falling back to upstream /v1/models", component="kv-agent")
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

BUNDLE_DIR = Path(__file__).resolve().parent
LOG_DIR = BUNDLE_DIR.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

SERVICE = os.environ.get("MLX_SERVICE_NAME", "mlx-exporter")
MACBOOK = os.environ.get("MLX_MACBOOK_ID", "unknown")

_LOGGERS: dict[str, logging.Logger] = {}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", SERVICE),
            "component": getattr(record, "component", "general"),
            "macbook": getattr(record, "macbook", MACBOOK),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["err"] = self.formatException(record.exc_info)
        else:
            err_text = getattr(record, "err", None)
            if err_text:
                payload["err"] = err_text
        for key in ("trace_id", "span_id"):
            val = getattr(record, key, None)
            if val:
                payload[key] = val
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(service: str, macbook: str, level: str = "INFO",
                  log_dir: Path | None = None) -> logging.Logger:
    """Configure structured JSON logging for one exporter.

    Call once at startup before any log() calls.
    """
    global SERVICE, MACBOOK
    SERVICE = service
    MACBOOK = macbook
    log_dir = Path(log_dir or LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(f"mlx.{service}")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False

    # Close and remove pre-existing handlers to avoid FD leaks and duplicate logs
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(_JsonFormatter())
    logger.addHandler(sh)

    try:
        max_mb = max(1, int(os.environ.get("LOG_ROTATION_MAX_MB", "50")))
    except ValueError:
        max_mb = 50
    try:
        backups = max(0, int(os.environ.get("LOG_ROTATION_BACKUPS", "5")))
    except ValueError:
        backups = 5
    fh = logging.handlers.RotatingFileHandler(
        log_dir / f"{service}.log",
        maxBytes=max_mb * 1024 * 1024,
        backupCount=backups,
        encoding="utf8",
    )
    fh.setFormatter(_JsonFormatter())
    logger.addHandler(fh)

    _LOGGERS[service] = logger
    return logger


def _logger() -> logging.Logger:
    return logging.getLogger(f"mlx.{SERVICE}")


def log(level: str, msg: str, component: str = "general",
        macbook: str | None = None, trace_id: str | None = None,
        span_id: str | None = None, exc_info: bool = False) -> None:
    """Emit one structured JSON log record."""
    logger = _logger()
    level_no = getattr(logging, level.upper(), logging.INFO)
    if not logger.isEnabledFor(level_no):
        return
    active_exc = sys.exc_info() if exc_info else None
    if active_exc is not None and active_exc[0] is None:
        active_exc = None
    record = logging.LogRecord(
        name=f"mlx.{SERVICE}",
        level=level_no,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=active_exc,
    )
    record.service = SERVICE
    record.component = component
    record.macbook = macbook or MACBOOK
    if trace_id:
        record.trace_id = trace_id
    if span_id:
        record.span_id = span_id
    logger.handle(record)


def degraded(msg: str, component: str = "general",
             trace_id: str | None = None, span_id: str | None = None) -> None:
    """Log a graceful-degradation event (feature disabled due to missing config)."""
    warning(f"[degraded] {msg}", component=component, trace_id=trace_id, span_id=span_id)


def error(msg: str, component: str = "general", exc_info: bool = False,
          trace_id: str | None = None, span_id: str | None = None) -> None:
    log("error", msg, component=component, trace_id=trace_id, span_id=span_id, exc_info=exc_info)


def info(msg: str, component: str = "general",
         trace_id: str | None = None, span_id: str | None = None) -> None:
    log("info", msg, component=component, trace_id=trace_id, span_id=span_id)


def warning(msg: str, component: str = "general",
            trace_id: str | None = None, span_id: str | None = None) -> None:
    log("warning", msg, component=component, trace_id=trace_id, span_id=span_id)


def debug(msg: str, component: str = "general",
          trace_id: str | None = None, span_id: str | None = None) -> None:
    log("debug", msg, component=component, trace_id=trace_id, span_id=span_id)


def parse_upstream_url(
    raw: str,
    default_scheme: str = "http",
    default_port: int = 8080,
) -> tuple[str, str, int, str]:
    """Normalize an upstream target (host:port or URL) into (scheme, host, port, base_url).

    Accepts forms like ``127.0.0.1:8000``, ``http://127.0.0.1:8000``,
    ``https://llm.internal/v1``, or ``localhost``.
    """
    value = (raw or "").strip()
    if not value:
        raise ValueError("upstream URL/address must not be empty")
    if "://" not in value:
        value = f"{default_scheme}://{value}"
    parsed = urlparse(value)
    scheme = (parsed.scheme or default_scheme).lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported upstream scheme: {scheme!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"missing upstream hostname in {raw!r}")
    if parsed.port is not None:
        port = parsed.port
    elif scheme == "https":
        port = 443
    else:
        port = default_port
    base_url = f"{scheme}://{host}:{port}"
    return scheme, host, port, base_url
