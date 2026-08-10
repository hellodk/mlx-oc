#!/usr/bin/env python3
"""
mlx_server_supervisor.py
========================
Process supervisor + serving-status exporter for `mlx_lm.server`.

Why this exists
---------------
On 2026-08-04 the distributed mlx_lm.server died with a Metal command-buffer
failure. The proxy kept serving 503s and vmalert fired a generic
"upstream unreachable" alert, but nothing restarted the server and nothing
said *why* it died or whether the model was loaded again. This agent closes
that loop:

  * spawns the distributed server command (mlx.launch ...) as a child
  * watches the child; on exit it records the exit code, sniffs the last
    lines of the server log for a crash *reason* (metal / oom / python /
    hung / unknown), and restarts the child with exponential backoff
  * probes the upstream /v1/models endpoint to distinguish "process up"
    from "process up AND serving" from "model actually loaded"
  * keep-alive: if the process stays alive but stops answering /v1/models for
    --hang-after seconds, the supervisor kills and restarts it itself
  * resets the restart backoff to the initial value once the server has been
    serving continuously for --backoff-reset seconds, so a single fresh crash
    after a long healthy run recovers immediately
  * exports Prometheus metrics on :9105/metrics that become the source of
    truth for model-serving health in VictoriaMetrics + vmalert + Grafana

Metrics (:9105/metrics)
-----------------------
  mlx_server_state                      0=idle 1=running 2=starting 3=backoff
  mlx_server_up                         1 if the mlx_lm.server process is alive
  mlx_server_ready                      1 if /v1/models returns 200
  mlx_model_loaded{model="..."}         1 once the health probe saw the model
  mlx_server_restarts_total             number of times the server was restarted
  mlx_server_crashes_total{reason}      server crashes by sniffed reason
  mlx_server_uptime_seconds             current child process uptime
  mlx_server_last_exit_code             exit code of the most recent child exit
  mlx_server_last_crash_reason{reason}  metal_gpu_error / oom / python_error / hung / unknown / none
  mlx_server_health_checks_total{result="ok"|"fail"}   health probe outcomes
  mlx_server_backoff_seconds            current backoff delay before next restart

Usage (normally driven by start_server.sh):
  <venv>/bin/python mlx_server_supervisor.py \
      --model mlx-community/Qwen3.5-4B-MLX-8bit \
      --health http://127.0.0.1:8081 \
      --server-log cluster/logs/server.log \
      --listen 0.0.0.0:9105 \
      --command "<mlx.launch ... mlx_lm.server ...>"
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

LISTEN = ("0.0.0.0", 9105)
HEALTH_URL = (
    f"http://{os.environ.get('MLX_SERVER_IP', '127.0.0.1')}"
    f":{os.environ.get('MLX_SERVER_PORT', '8081')}/v1/models"
)
DEFAULT_MODEL = os.environ.get("MLX_MODEL", "mlx-community/Qwen3.5-4B-MLX-8bit")

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
    """Scan the tail of the server log for a human-meaningful crash reason."""
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

    # -- process lifecycle -------------------------------------------------
    def _spawn(self):
        cmd = shlex.split(self.args.command)
        self.child_started = time.monotonic()
        self.state = 2  # starting
        G_STATE.set(2)
        # The server inherits our stdout/stderr otherwise, which buries its
        # logs in supervisor.log. Redirect it into server.log (the file the
        # KV-cache agent, log tailer and crash-sniffer all consume).
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

            # Fast MTTR: restart promptly on the first crash or whenever the
            # server had been serving continuously for >= --backoff-reset
            # seconds (backoff is reset after a long healthy run). Otherwise
            # ratchet the backoff for crash-loop protection.
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
                self.state = 3  # backoff
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
        # mlx.launch leaves the remote rank behind; ask it to clean up too.
        self._kill_remote_rank()

    def _kill_remote_rank(self):
        """Clean up every remote ring rank (hosts[1:] from the hostfile) and any
        local orphans left by mlx.launch. Bracketed patterns keep the wrapper's
        own cmdline from matching the pkill and killing the cleanup mid-run."""
        for peer in self._remote_peers():
            try:
                subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", peer,
                     "pkill -f 'mlx_lm[.]server'; pkill -f 'mlx_server_launcher[.]py'; "
                     "pkill -f 'mlx[.]launch'"],
                    capture_output=True, timeout=15,
                )
            except Exception:
                pass
        # mlx.launch terminates but its local python -m mlx_lm.server worker
        # can survive as an orphan holding the httpd port; take it down too.
        for pat in ("mlx_lm.server", "mlx_server_launcher", "mlx.launch"):
            subprocess.run(["pkill", "-f", pat], capture_output=True)

    def _remote_peers(self):
        """ssh targets of every ring rank except rank 0 (which runs locally)."""
        try:
            with open(self.args.hostfile) as f:
                hosts = json.load(f)["hosts"]
            return [
                (h.get("ssh") or (h.get("ips") or [""])[0])
                for h in hosts[1:]
                if (h.get("ssh") or (h.get("ips") or [""])[0])
            ]
        except Exception:
            return []

    # -- health ------------------------------------------------------------
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
            except Exception:
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
                            self.state = 1  # running
                            G_STATE.set(1)
                elif alive and not ok and self.last_ready:
                    # Was serving recently, the process is still alive, but
                    # /v1/models no longer answers: candidate hang. Kill it
                    # after --hang-after of consecutive failures so the run
                    # loop respawns it (keep-alive).
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
                        except Exception:
                            pass
                    if self.state == 1:
                        self.state = 2  # process alive but health degraded
                        G_STATE.set(2)
                else:
                    self.hung_since = None
                    if self.state == 1:
                        self.state = 2  # process alive but health degraded
                        G_STATE.set(2)

    # -- logging -----------------------------------------------------------
    def _log(self, msg):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        print(f"[{ts}] [supervisor] {msg}", flush=True)


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
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--hostfile", default=os.environ.get("MLX_HOSTFILE", "cluster/hosts.json"),
                    help="path to hosts.json; remote ring ranks (hosts[1:]) are "
                         "cleaned up on shutdown")
    ap.add_argument("--health", default=HEALTH_URL)
    ap.add_argument("--server-log", default="cluster/logs/server.log")
    ap.add_argument("--listen", default="0.0.0.0:9105")
    ap.add_argument("--probe-interval", type=float, default=5.0,
                    help="health probe interval in seconds (default 5s)")
    ap.add_argument("--backoff", type=float, default=3.0,
                    help="initial restart delay after a crash")
    ap.add_argument("--backoff-max", type=float, default=30.0,
                    help="maximum restart delay (crash-loop protection)")
    ap.add_argument("--hang-after", type=float, default=20.0,
                    help="kill + restart the server after this many seconds of "
                         "health failures while the process stays alive (keep-alive)")
    ap.add_argument("--backoff-reset", type=float, default=120.0,
                    help="reset the restart backoff to the initial value after "
                         "this many seconds of continuous serving")
    args = ap.parse_args()

    host, _, port = args.listen.rpartition(":")
    sup = Supervisor(args)
    MetricsHandler.supervisor = sup

    # Emit all crash-reason series at 0 so the dashboard has every reason
    # present before the first crash (counters only appear once incremented).
    for r in ("metal_gpu_error", "oom", "python_error", "hung", "exit", "unknown"):
        G_CRASHES.labels(reason=r).inc(0)

    threading.Thread(target=sup.health_loop, daemon=True).start()
    httpd = ThreadingHTTPServer((host or "0.0.0.0", int(port or 9105)), MetricsHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(
        f"[supervisor] metrics on {host or '0.0.0.0'}:{port or 9105} "
        f"model={args.model} health={args.health}",
        flush=True,
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
