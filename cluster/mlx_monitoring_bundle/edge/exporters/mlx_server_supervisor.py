#!/usr/bin/env python3
"""
mlx_server_supervisor.py
========================
Process supervisor + serving-status exporter for mlx_lm.server.

Spawns the distributed server command, watches the child, restarts with
exponential backoff on crash, probes /v1/models to distinguish "process up"
from "serving", and keeps the server alive via a hang watchdog.

All configuration comes from environment variables and config/nodes.yml -
nothing is hardcoded.
"""

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Gauge, Counter, generate_latest, CONTENT_TYPE_LATEST

from mlx_logging import degraded, debug, error, info, setup_logging

LISTEN_DEFAULT = "0.0.0.0:9105"
HEALTH_URL_DEFAULT = "http://127.0.0.1:8081/v1/models"

CRASH_REASONS = [
    (re.compile(r"\[METAL\].*(Command buffer execution failed|GPU errors)", re.I),
     "metal_gpu_error"),
    (re.compile(r"\[METAL\].*(reset|timeout|out of memory)", re.I), "metal_gpu_error"),
    (re.compile(r"\b(out of memory|OOM|allocation failed)\b", re.I), "oom"),
    (re.compile(r"\b(traceback|killed|segmentation fault|SIGABRT)\b", re.I), "python_error"),
]

G_STATE = Gauge("mlx_server_state", "0=idle 1=running 2=starting 3=backoff")
G_UP = Gauge("mlx_server_up", "1 if the mlx_lm.server process is alive")
G_READY = Gauge("mlx_server_ready", "1 if /v1/models returns HTTP 200")
G_MODEL = Gauge(
    "mlx_model_loaded", "1 once the health probe observed the model being served",
    ["model"],
)
G_RESTARTS = Counter("mlx_server_restarts_total", "mlx_lm.server restarts performed")
G_CRASHES = Counter(
    "mlx_server_crashes_total", "server crashes by sniffed reason", ["reason"]
)
G_UPTIME = Gauge("mlx_server_uptime_seconds", "current mlx_lm.server process uptime")
G_EXIT = Gauge("mlx_server_last_exit_code", "exit code of the most recent child exit")
G_REASON = Gauge(
    "mlx_server_last_crash_reason",
    "1 for the reason of the most recent crash",
    ["reason"],
)
G_CHECKS = Counter(
    "mlx_server_health_checks_total", "health probe outcomes", ["result"]
)
G_BACKOFF = Gauge("mlx_server_backoff_seconds", "backoff delay before next restart")


def sniff_crash_reason(log_path: str, nbytes: int = 8192) -> str:
    try:
        size = Path(log_path).stat().st_size
        with open(log_path, "rb") as fh:
            fh.seek(max(0, size - nbytes))
            tail = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return "unknown"
    for pattern, reason in CRASH_REASONS:
        if pattern.search(tail):
            return reason
    return "exit"


class Supervisor:
    def __init__(self, args):
        self.args = args
        self.child = None
        self.child_started = 0.0
        self.state = 0
        self.exit_code = 0
        self.reason = "none"
        self.backoff = 0.0
        self.stop_event = threading.Event()
        self.health_lock = threading.Lock()
        self.hung_since = None
        self.last_ready = 0.0
        self.forced_reason = None

    def _spawn(self):
        cmd = shlex.split(self.args.command)
        self.child_started = time.monotonic()
        self.state = 2
        G_STATE.set(2)
        server_log = open(self.args.server_log, "ab")
        return subprocess.Popen(
            cmd,
            cwd=self.args.cwd,
            stdin=subprocess.DEVNULL,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )

    def run(self):
        G_STATE.set(0)
        self.backoff = self.args.backoff
        first = True
        while not self.stop_event.is_set():
            self.child = self._spawn()
            G_UP.set(1)
            self._log(f"spawned pid={self.child.pid} cmd={self.args.command[:200]}")

            try:
                rc = self.child.wait()
            except KeyboardInterrupt:
                break
            G_UP.set(0)
            self.exit_code = rc
            self.reason = self.forced_reason or sniff_crash_reason(self.args.server_log)
            self.forced_reason = None
            G_EXIT.set(rc)
            G_CRASHES.labels(reason=self.reason).inc()
            for r in ("metal_gpu_error", "oom", "python_error", "hung", "exit", "unknown"):
                G_REASON.labels(reason=r).set(1 if r == self.reason else 0)
            G_UPTIME.set(0)
            G_READY.set(0)
            G_MODEL.labels(model=self.args.model).set(0)

            if self.stop_event.is_set():
                break
            G_RESTARTS.inc()

            stable = time.monotonic() - self.last_ready if self.last_ready else 0.0
            if first or stable >= self.args.backoff_reset:
                if not first:
                    self._log(
                        f"server was stable for {stable:.0f}s, "
                        f"resetting backoff to {self.args.backoff:.0f}s"
                    )
                self.backoff = self.args.backoff
                first = False
                delay = 0.0
            else:
                self.backoff = min(self.backoff * 2, self.args.backoff_max)
                delay = self.backoff
                self.state = 3
                G_STATE.set(3)
                G_BACKOFF.set(self.backoff)
            self._log(
                f"server exited rc={rc} reason={self.reason} "
                f"next_start_in={delay:.0f}s"
            )
            if delay > 0 and self.stop_event.wait(delay):
                break
            G_BACKOFF.set(0)
        self.state = 0
        G_STATE.set(0)
        self._log("supervisor stopped")

    def stop(self):
        self._log("supervisor shutting down, terminating server")
        self.stop_event.set()
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
        self._kill_remote_rank()

    def _kill_remote_rank(self):
        rank1 = self.args.rank1_ssh_target
        if not rank1:
            degraded("no rank1 SSH target configured; skipping remote rank cleanup",
                     component="supervisor")
            return
        ssh_cmd = ["ssh", "-o", f"ConnectTimeout={self.args.ssh_timeout}"]
        if self.args.rank1_ssh_key:
            ssh_cmd += ["-i", self.args.rank1_ssh_key]
        ssh_cmd += [rank1, "pkill -f 'mlx_lm.server'; pkill -f 'mlx.launch'"]
        try:
            subprocess.run(
                ssh_cmd,
                capture_output=True, timeout=15,
            )
        except Exception as exc:
            error(f"remote rank cleanup failed: {exc!r}", component="supervisor")
        for pat in ("mlx_lm.server", "mlx.launch"):
            subprocess.run(["pkill", "-f", pat], capture_output=True)

    def health_loop(self):
        import urllib.request
        while not self.stop_event.is_set():
            self.stop_event.wait(self.args.probe_interval)
            ok = False
            try:
                with urllib.request.urlopen(self.args.health, timeout=3) as resp:
                    if resp.status == 200:
                        body = resp.read().decode("utf-8", "replace")
                        ok = json.loads(body).get("data") is not None
            except Exception as exc:
                debug(f"health check failed: {exc!r}", component="supervisor")
                ok = False
            with self.health_lock:
                alive = self.child is not None and self.child.poll() is None
                G_CHECKS.labels(result="ok" if ok else "fail").inc()
                G_READY.set(1 if ok else 0)
                if ok:
                    self.hung_since = None
                    self.last_ready = time.monotonic()
                    G_MODEL.labels(model=self.args.model).set(1)
                    if alive:
                        G_UPTIME.set(max(0, time.monotonic() - self.child_started))
                        if self.state != 1:
                            self.state = 1
                            G_STATE.set(1)
                elif alive and not ok and self.last_ready:
                    if self.hung_since is None:
                        self.hung_since = time.monotonic()
                    elif time.monotonic() - self.hung_since >= self.args.hang_after:
                        self._log(
                            f"server not serving for {self.args.hang_after:.0f}s, "
                            "killing for restart (keep-alive)"
                        )
                        self.forced_reason = "hung"
                        self.hung_since = None
                        try:
                            self.child.terminate()
                        except Exception as exc:
                            debug(f"child terminate failed: {exc!r}", component="supervisor")
                    if self.state == 1:
                        self.state = 2
                        G_STATE.set(2)
                else:
                    self.hung_since = None
                    if self.state == 1:
                        self.state = 2
                        G_STATE.set(2)

    def _log(self, msg):
        info(msg, component="supervisor")


class MetricsHandler(BaseHTTPRequestHandler):
    supervisor = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path not in ("/metrics", "/-/healthy"):
            self.send_response(404)
            self.end_headers()
            return
        if self.path == "/-/healthy":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        payload = generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--command", required=True, help="shell command that runs mlx_lm.server")
    ap.add_argument("--cwd", default=".", help="working directory for the server command")
    ap.add_argument("--model", default=os.environ.get("MLX_MODEL"))
    ap.add_argument("--health", default=os.environ.get("MLX_HEALTH_URL", HEALTH_URL_DEFAULT))
    ap.add_argument("--server-log", default=os.environ.get("MLX_SERVER_LOG"))
    ap.add_argument("--listen", default=os.environ.get("MLX_SUPERVISOR_LISTEN", LISTEN_DEFAULT))
    ap.add_argument("--probe-interval", type=float, default=5.0)
    ap.add_argument("--backoff", type=float, default=3.0)
    ap.add_argument("--backoff-max", type=float, default=30.0)
    ap.add_argument("--hang-after", type=float, default=20.0)
    ap.add_argument("--backoff-reset", type=float, default=120.0)
    ap.add_argument("--rank1-ssh-target", default=os.environ.get("RANK1_SSH_TARGET", ""),
                    help="ssh user@host for rank1 cleanup (optional)")
    ap.add_argument("--rank1-ssh-key", default=os.environ.get("RANK1_SSH_KEY", ""),
                    help="ssh private key for rank1 cleanup (optional)")
    ap.add_argument("--ssh-timeout", type=float, default=5.0)
    args = ap.parse_args()

    if not args.server_log:
        error("MLX_SERVER_LOG not set and --server-log not given; cannot run",
              component="supervisor")
        sys.exit(1)
    if not args.model:
        degraded("MLX_MODEL not set; model_loaded gauge will be skipped",
                 component="supervisor")

    setup_logging(service="mlx-supervisor",
                  macbook=os.environ.get("MLX_MACBOOK_ID", "unknown"))

    host, _, port = args.listen.rpartition(":")
    sup = Supervisor(args)
    MetricsHandler.supervisor = sup

    for r in ("metal_gpu_error", "oom", "python_error", "hung", "exit", "unknown"):
        G_CRASHES.labels(reason=r).inc(0)

    threading.Thread(target=sup.health_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port or 9105)), MetricsHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    info(
        f"metrics on {host or '0.0.0.0'}:{port or 9105} "
        f"model={args.model or '<unset>'} health={args.health}",
        component="supervisor",
    )

    def _on_term(signum, frame):
        sup.stop()
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    try:
        sup.run()
    finally:
        sup.stop()


if __name__ == "__main__":
    main()
