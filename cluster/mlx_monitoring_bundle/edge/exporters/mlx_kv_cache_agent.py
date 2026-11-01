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

import argparse
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

from mlx_logging import degraded, error, info, setup_logging
from mlx_model_info import model_info as _model_info

LISTEN_DEFAULT = "0.0.0.0:9104"
POLL_INTERVAL_DEFAULT = 5.0
MAX_SEQ_DEFAULT = 10  # mlx_lm server LRUPromptCache default

CACHE_RE = re.compile(r"Prompt Cache:\s+(\d+)\s+sequences?,\s+([\d.]+)\s+GB")
TYPE_RE = re.compile(r"-\s+(\w+):\s+(\d+)\s+sequences?,\s+([\d.]+)\s+GB")

from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

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
G_CTX_MAX = Gauge("mlx_context_length_max_tokens", "Model max context length (tokens)")
G_KV_PER_TOKEN = Gauge("mlx_kv_bytes_per_token", "Approx KV cache bytes per token")
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
    latest: dict[str, tuple[int, int]] = {}
    try:
        with open(log_path, "r", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 1_000_000))
            fh.readline()
            for line in fh:
                m = CACHE_RE.search(line)
                if m:
                    latest = {}
                    latest["total"] = (int(m.group(1)), float(m.group(2)) * 1e9)
                    continue
                m = TYPE_RE.search(line)
                if m:
                    latest[m.group(1)] = (
                        int(m.group(2)),
                        float(m.group(3)) * 1e9,
                    )
    except FileNotFoundError:
        return {}
    return latest


def _discover_model_from_upstream(upstream_url: str) -> str | None:
    """Query upstream mlx_lm.server GET /v1/models to discover the loaded model."""
    try:
        resp = requests.get(f"{upstream_url.rstrip('/')}/v1/models", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data") or []
        if models:
            return models[0].get("id")
    except Exception as exc:
        error(f"failed to discover model from upstream {upstream_url}: {exc!r}",
              component="kv-agent")
    return None


def _resolve_model(args_model: str | None, upstream_url: str) -> str | None:
    """Resolve the model name: config > upstream discovery > None (degraded)."""
    if args_model:
        return args_model
    discovered = _discover_model_from_upstream(upstream_url)
    if discovered:
        info(f"model auto-discovered from upstream: {discovered}",
             component="kv-agent")
        return discovered
    degraded("MLX_MODEL not set and upstream discovery failed; skipping "
             "model-specific gauges", component="kv-agent")
    return None


def _update_gauges(log_path: str, model_name: str | None) -> None:
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

    if model_name:
        try:
            info = _model_info(model_name)
            G_CTX_MAX.set(info.max_context_tokens)
            G_KV_PER_TOKEN.set(info.kv_bytes_per_token)
            G_KV_MAX_CTX.set(info.kv_bytes_for_max_context)
        except Exception as exc:
            degraded(f"could not resolve model info for '{model_name}': {exc!r}",
                     component="kv-agent")
    else:
        G_CTX_MAX.set(0)
        G_KV_PER_TOKEN.set(0)
        G_KV_MAX_CTX.set(0)

    total_bytes = stats.get("total", (0, 0))[1]
    if not total_bytes:
        total_bytes = max((b for n, b in stats.values()), default=0)
    capacity = MAX_SEQ_DEFAULT * (G_KV_MAX_CTX._value.get() or 0)
    G_UTIL.set(total_bytes / capacity if capacity else 0.0)


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
    ap = argparse.ArgumentParser(description="MLX KV-cache / context metrics agent")
    ap.add_argument("--log-file", default=os.environ.get("MLX_SERVER_LOG"))
    ap.add_argument("--model", default=os.environ.get("MLX_MODEL"))
    ap.add_argument("--upstream", default=os.environ.get("MLX_UPSTREAM",
             "http://127.0.0.1:8081"),
             help="upstream mlx_lm.server base URL for model discovery")
    ap.add_argument("--listen", default=os.environ.get("MLX_KV_LISTEN", LISTEN_DEFAULT))
    ap.add_argument("--interval", type=float,
             default=float(os.environ.get("MLX_KV_INTERVAL", POLL_INTERVAL_DEFAULT)))
    args = ap.parse_args()

    if not args.log_file:
        error("MLX_SERVER_LOG not set and --log-file not given; cannot run",
              component="kv-agent")
        sys.exit(1)

    setup_logging(service="mlx-kv-agent",
                  macbook=os.environ.get("MLX_MACBOOK_ID", "unknown"))

    model_name = _resolve_model(args.model, args.upstream)

    G_UP.set(1)
    info(f"log_file={args.log_file} model={model_name or '<skipped>'} "
         f"-> metrics on {args.listen}", component="kv-agent")

    def _poll():
        while True:
            try:
                _update_gauges(args.log_file, model_name)
            except Exception as exc:
                error(f"poll error: {exc!r}", component="kv-agent")
            time.sleep(args.interval)

    import threading
    threading.Thread(target=_poll, daemon=True).start()

    host, _, port = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port or 9104)), Handler)
    info(f"tailing {args.log_file}", component="kv-agent")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
