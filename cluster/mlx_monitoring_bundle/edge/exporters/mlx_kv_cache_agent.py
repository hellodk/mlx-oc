#!/usr/bin/env python3
"""
mlx_kv_cache_agent.py
=====================
Background KV-cache / context-length agent for the MLX performance dashboard.

Reads the mlx_lm.server log for KV-cache state and serves Prometheus metrics.
All configuration comes from environment variables (edge.env) and
config/nodes.yml - nothing is hardcoded.

MLX_MODEL is optional. If unset, the model name is discovered from the
upstream mlx_lm.server GET /v1/models endpoint. If discovery fails,
model-specific gauges are skipped with a degraded log entry - all other
monitoring continues unaffected.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST
import requests

from mlx_logging import (
    degraded,
    debug,
    error,
    info,
    parse_listen_addr,
    parse_upstream_url,
    setup_logging,
)
from mlx_model_info import model_info as _model_info

LISTEN_DEFAULT = "127.0.0.1:9104"
POLL_INTERVAL_DEFAULT = 5.0
MAX_SEQ_DEFAULT = 10  # mlx_lm server LRUPromptCache default

CACHE_RE = re.compile(r"Prompt Cache:\s+(\d+)\s+sequences?,\s+([\d.]+)\s+GB")
TYPE_RE = re.compile(r"-\s+(\w+):\s+(\d+)\s+sequences?,\s+([\d.]+)\s+GB")

G_UP = Gauge("mlx_kv_agent_up", "1 if the KV cache agent is running")
G_SEQ = Gauge(
    "mlx_kv_cache_sequences",
    "Cached prompt sequences held in the LRU KV cache (last seen in server log)",
    ["type"],
)
G_BYTES = Gauge(
    "mlx_kv_cache_bytes",
    "Bytes used by the LRU KV cache (last seen in server log)",
    ["type"],
)
G_MAX_SEQ = Gauge("mlx_kv_cache_max_sequences", "LRU prompt-cache depth")
G_KV_MAX_CTX = Gauge(
    "mlx_kv_est_bytes_max_context",
    "Approx KV bytes for one full-length context sequence",
)
G_UTIL = Gauge(
    "mlx_kv_cache_utilization",
    "KV cache bytes / (max_sequences x KV bytes for a full context)",
)


def _read_cache_stats(log_path: str) -> dict:
    """Return {type: (sequences, bytes)} from the last cache-stats block."""
    latest: dict[str, tuple[int, float]] = {}
    gib = float(1024 ** 3)
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size > 1_000_000:
                fh.seek(size - 1_000_000)
                fh.readline()  # drop partial first line only when seeking mid-file
            else:
                fh.seek(0)
            for raw_line in fh:
                line = raw_line.decode("utf8", errors="replace")
                m = CACHE_RE.search(line)
                if m:
                    latest = {}
                    latest["total"] = (int(m.group(1)), float(m.group(2)) * gib)
                    continue
                m = TYPE_RE.search(line)
                if m:
                    latest[m.group(1)] = (
                        int(m.group(2)),
                        float(m.group(3)) * gib,
                    )
    except FileNotFoundError:
        debug(f"log file not found yet: {log_path}", component="kv-agent")
        return {}
    return latest


def _pick_served_model(models: list, model_path: str | None) -> str | None:
    """Pick the model actually served by mlx_lm.server from /v1/models.

    mlx_lm.server appends the explicit ``--model`` path (as an absolute path)
    after any Hugging Face cache entries. Prefer that entry and return the
    basename, which matches the id used in API requests.
    """
    ids = [
        m.get("id") for m in models
        if isinstance(m, dict) and m.get("id")
    ]
    if not ids:
        return None

    non_default = [mid for mid in ids if mid != "default_model"]
    candidates = non_default or ids

    if model_path:
        target_name = Path(model_path.rstrip("/")).name
        for mid in reversed(candidates):
            if str(mid).rstrip("/") == str(model_path).rstrip("/"):
                return target_name
            if Path(str(mid)).name == target_name:
                return target_name

    for mid in reversed(candidates):
        if str(mid).startswith("/"):
            return Path(str(mid)).name
    return candidates[0]


def _discover_model_from_upstream(upstream_url: str,
                                  model_path: str | None = None) -> str | None:
    """Query upstream mlx_lm.server GET /v1/models to discover the loaded model."""
    try:
        resp = requests.get(f"{upstream_url.rstrip('/')}/v1/models", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data") or []
        return _pick_served_model(models, model_path)
    except Exception as exc:
        error(f"failed to discover model from upstream {upstream_url}: {exc!r}",
              component="kv-agent")
    return None


def _resolve_model(args_model: str | None, upstream_url: str,
                   model_path: str | None) -> str | None:
    """Resolve the model name: config > upstream discovery > model path."""
    if args_model:
        if args_model.startswith(("/", "~", "./", "../")):
            return Path(args_model.rstrip("/")).name
        return args_model
    discovered = _discover_model_from_upstream(upstream_url, model_path)
    if discovered:
        info(f"model auto-discovered from upstream: {discovered}",
             component="kv-agent")
        return discovered
    if model_path:
        name = Path(model_path.rstrip("/")).name
        info(f"model id derived from MLX_MODEL_PATH: {name}", component="kv-agent")
        return name
    degraded("MLX_MODEL/MLX_MODEL_PATH not set and upstream discovery failed; "
             "skipping model-specific gauges", component="kv-agent")
    return None


def _update_gauges(log_path: str, model_name: str | None,
                   model_path: str | None = None,
                   upstream_url: str | None = None) -> None:
    stats = _read_cache_stats(log_path)

    for label in ("total", "assistant", "user", "system"):
        if label in stats:
            n, b = stats[label]
            G_SEQ.labels(type=label).set(n)
            G_BYTES.labels(type=label).set(b)
        else:
            G_SEQ.labels(type=label).set(0)
            G_BYTES.labels(type=label).set(0)

    G_MAX_SEQ.set(MAX_SEQ_DEFAULT)

    info_obj = None
    if model_name or model_path:
        try:
            info_obj = _model_info(
                model_name,
                model_path=model_path,
                upstream_url=upstream_url,
            )
        except Exception as exc:
            degraded(f"could not resolve model info for '{model_name}': {exc!r}",
                     component="kv-agent")
    kv_max_ctx_bytes = 0
    if info_obj is not None:
        kv_max_ctx_bytes = info_obj.kv_bytes_for_max_context
    G_KV_MAX_CTX.set(kv_max_ctx_bytes)

    total_bytes = stats.get("total", (0, 0))[1]
    if not total_bytes:
        total_bytes = max((b for n, b in stats.values()), default=0)
    capacity = MAX_SEQ_DEFAULT * kv_max_ctx_bytes
    G_UTIL.set(total_bytes / capacity if capacity else 0.0)


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
    ap = argparse.ArgumentParser(description="MLX KV-cache / context metrics agent")
    ap.add_argument("--log-file", default=os.environ.get("MLX_SERVER_LOG"))
    ap.add_argument("--model", default=os.environ.get("MLX_MODEL"))
    ap.add_argument("--model-path", default=os.environ.get("MLX_MODEL_PATH"))
    ap.add_argument(
        "--upstream",
        default=os.environ.get("MLX_UPSTREAM", "http://127.0.0.1:8081"),
        help="upstream mlx_lm.server base URL for model discovery",
    )
    ap.add_argument("--listen", default=os.environ.get("MLX_KV_LISTEN", LISTEN_DEFAULT))
    ap.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("MLX_KV_INTERVAL", POLL_INTERVAL_DEFAULT)),
    )
    args = ap.parse_args()

    macbook_id = os.environ.get("MLX_DEVICE_ID") or os.environ.get("MLX_MACBOOK_ID") or "unknown"
    setup_logging(service="mlx-kv-agent", macbook=macbook_id)

    if not args.log_file:
        error("MLX_SERVER_LOG not set and --log-file not given; cannot run",
              component="kv-agent")
        sys.exit(1)

    try:
        _, _, _, upstream_base = parse_upstream_url(args.upstream, default_port=8081)
    except ValueError as exc:
        error(f"invalid upstream URL {args.upstream!r}: {exc}", component="kv-agent")
        sys.exit(1)

    model_name = _resolve_model(args.model, upstream_base, args.model_path)

    G_UP.set(1)
    info(f"log_file={args.log_file} model={model_name or '<skipped>'} "
         f"model_path={args.model_path or '<unset>'} "
         f"-> metrics on {args.listen}", component="kv-agent")

    stop_event = threading.Event()

    def _poll():
        nonlocal model_name
        while not stop_event.is_set():
            try:
                if model_name is None:
                    model_name = _discover_model_from_upstream(upstream_base, args.model_path)
                    if model_name:
                        info(f"model discovered on retry: {model_name}", component="kv-agent")
                _update_gauges(
                    args.log_file,
                    model_name,
                    model_path=args.model_path,
                    upstream_url=upstream_base,
                )
            except Exception as exc:
                error(f"poll error: {exc!r}", component="kv-agent")
            stop_event.wait(args.interval)

    threading.Thread(target=_poll, daemon=True).start()

    host, port = parse_listen_addr(args.listen, default_host="0.0.0.0", default_port=9104)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server = ThreadingHTTPServer((host, port), Handler)
    info(f"tailing {args.log_file}", component="kv-agent")

    def _shutdown(signum, frame):
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
