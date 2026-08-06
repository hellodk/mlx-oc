#!/usr/bin/env python3
"""
mlx_kv_cache_agent.py
=====================
Background KV-cache / context-length agent for the MLX performance dashboard.

mlx_lm.server has no HTTP stats endpoint, but it logs the prompt (KV) cache
state at the start of every generation:

    Prompt Cache: 4 sequences, 0.03 GB
    - assistant: 3 sequences, 0.02 GB

This agent tails that log, keeps the last-seen cache state, combines it with
the model's context length and KV bytes-per-token (from config.json, via
mlx_model_info), and serves the result as Prometheus metrics on :9104/metrics:

    mlx_kv_cache_sequences{type=...}        last seen cached sequence count
    mlx_kv_cache_bytes{type=...}            last seen KV cache bytes
    mlx_kv_cache_max_sequences              LRU prompt-cache depth (default 10)
    mlx_kv_cache_utilization                bytes / (max_sequences * max ctx KV)
    mlx_context_length_max_tokens           model max_position_embeddings
    mlx_kv_bytes_per_token                  fp16 KV bytes/token from model dims
    mlx_kv_est_bytes_max_context            KV bytes for one full-length sequence

Usage:
  mlx_kv_cache_agent.py
  mlx_kv_cache_agent.py --log-file cluster/logs/server.log --listen 0.0.0.0:9104
"""

import argparse
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

from mlx_model_info import model_info

LISTEN = ("0.0.0.0", 9104)
DEFAULT_LOG = str(Path(__file__).resolve().parent / "logs" / "server.log")
DEFAULT_MODEL = os.environ.get("MLX_MODEL", "mlx-community/Qwen3.5-4B-MLX-8bit")

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
G_MAX_SEQ = Gauge("mlx_kv_cache_max_sequences", "LRU prompt-cache depth (mlx_lm default 10)")
G_UTIL = Gauge(
    "mlx_kv_cache_utilization",
    "KV cache bytes / (max_sequences x KV bytes for a full context)",
)
G_CTX_MAX = Gauge("mlx_context_length_max_tokens", "Model max context length (tokens)")
G_KV_PER_TOKEN = Gauge("mlx_kv_bytes_per_token", "Approx KV cache bytes per token (fp16)")
G_KV_MAX_CTX = Gauge(
    "mlx_kv_est_bytes_max_context",
    "Approx KV bytes for one full-length context sequence",
)


def _read_cache_stats(log_path: str) -> dict:
    """Return {type: (sequences, bytes)} from the last cache-stats block."""
    latest: dict[str, tuple[int, int]] = {}
    try:
        with open(log_path, "r", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 1_000_000))  # only scan the tail
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


def _update_gauges(log_path: str, info) -> None:
    stats = _read_cache_stats(log_path)

    for label in ("total", "assistant", "user", "system"):
        if label in stats:
            n, b = stats[label]
            G_SEQ.labels(type=label).set(n)
            G_BYTES.labels(type=label).set(b)
        else:
            G_SEQ.labels(type=label).set(0)
            G_BYTES.labels(type=label).set(0)

    max_seq = 10  # mlx_lm server LRUPromptCache default
    G_MAX_SEQ.set(max_seq)
    G_CTX_MAX.set(info.max_context_tokens)
    G_KV_PER_TOKEN.set(info.kv_bytes_per_token)
    G_KV_MAX_CTX.set(info.kv_bytes_for_max_context)

    total_bytes = stats.get("total", (0, 0))[1]
    if not total_bytes:
        total_bytes = max((b for n, b in stats.values()), default=0)
    capacity = max_seq * info.kv_bytes_for_max_context
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
    ap.add_argument("--log-file", default=os.environ.get("MLX_SERVER_LOG", DEFAULT_LOG))
    ap.add_argument("--model", default=os.environ.get("MLX_MODEL", DEFAULT_MODEL))
    ap.add_argument("--listen", default=os.environ.get("MLX_KV_LISTEN", "0.0.0.0:9104"))
    ap.add_argument("--interval", type=float, default=5.0, help="log poll interval (s)")
    args = ap.parse_args()

    info = model_info(args.model)
    G_UP.set(1)
    print(
        f"[kv-agent] model={args.model} ctx={info.max_context_tokens} "
        f"kv={info.kv_bytes_per_token} B/tok -> metrics on {args.listen}",
        flush=True,
    )

    def _poll():
        while True:
            try:
                _update_gauges(args.log_file, info)
            except Exception as exc:
                print(f"[kv-agent] poll error ({exc!r})", flush=True)
            time.sleep(args.interval)

    import threading

    threading.Thread(target=_poll, daemon=True).start()

    host, _, port = args.listen.rpartition(":")
    server = ThreadingHTTPServer((host or "0.0.0.0", int(port or 9104)), Handler)
    print(f"[kv-agent] tailing {args.log_file}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
