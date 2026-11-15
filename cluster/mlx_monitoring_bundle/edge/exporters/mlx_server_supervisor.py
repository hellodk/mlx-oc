#!/usr/bin/env python3
"""
mlx_server_supervisor.py
========================
Passive, read-only serving-status exporter for an external mlx_lm.server.

Never starts, stops, kills, or restarts mlx_lm.server. Instead it passively
observes:
  - The configured upstream HTTP endpoint (MLX_UPSTREAM /v1/models)
  - The local mlx_lm.server OS process (PID, uptime, PID-change restarts)
  - Optional external supervisor state (launchd label / systemd unit)
  - Server log crash patterns (MLX_SERVER_LOG)

All configuration comes from environment variables and CLI arguments -
nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

from mlx_logging import debug, error, info, setup_logging

LISTEN_DEFAULT = "127.0.0.1:9105"
_DEFAULT_UPSTREAM = os.environ.get("MLX_UPSTREAM", "http://127.0.0.1:11234").rstrip("/")
HEALTH_URL_DEFAULT = os.environ.get("MLX_HEALTH_URL", f"{_DEFAULT_UPSTREAM}/v1/models")

CRASH_REASONS = [
    (re.compile(r"\[METAL\].*(Command buffer execution failed|GPU errors)", re.I),
     "metal_gpu_error"),
    (re.compile(r"\[METAL\].*(reset|timeout|out of memory)", re.I), "metal_gpu_error"),
    (re.compile(r"\b(out of memory|OOM|allocation failed)\b", re.I), "oom"),
    (re.compile(r"\b(traceback|killed|segmentation fault|SIGABRT)\b", re.I), "python_error"),
]
KNOWN_REASONS = ("metal_gpu_error", "oom", "python_error", "hung", "exit", "unknown")

G_STATE = Gauge("mlx_server_state", "0=down 1=running_ready 2=up_not_ready 3=backoff")
G_UP = Gauge("mlx_server_up", "1 if the mlx_lm.server process or endpoint is alive")
G_READY = Gauge("mlx_server_ready", "1 if /v1/models returns HTTP 200")
G_MODEL = Gauge(
    "mlx_model_loaded", "1 once the health probe observed the model being served",
    ["model"],
)
G_RESTARTS = Counter("mlx_server_restarts_total", "mlx_lm.server restarts observed (PID changes)")
G_CRASHES = Counter(
    "mlx_server_crashes_total", "server crashes by sniffed reason", ["reason"]
)
G_UPTIME = Gauge("mlx_server_uptime_seconds", "current mlx_lm.server process uptime")
G_EXIT = Gauge("mlx_server_last_exit_code", "last observed exit code (-1 if unknown)")
G_REASON = Gauge(
    "mlx_server_last_crash_reason",
    "1 for the reason of the most recent crash",
    ["reason"],
)
G_CHECKS = Counter(
    "mlx_server_health_checks_total", "health probe outcomes", ["result"]
)
G_BACKOFF = Gauge("mlx_server_backoff_seconds", "backoff delay (0 in passive mode)")


def sniff_crash_reason(log_path: str, nbytes: int = 8192) -> str:
    """Inspect the tail of server.log to classify the most recent crash."""
    if not log_path:
        return "unknown"
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


def _parse_etime(etime_str: str) -> float:
    """Parse POSIX ps etime ([[dd-]hh:]mm:ss) into elapsed seconds."""
    s = etime_str.strip()
    days = 0
    if "-" in s:
        day_part, s = s.split("-", 1)
        if day_part.isdigit():
            days = int(day_part)
    parts = [int(p) for p in s.split(":") if p.isdigit()]
    if len(parts) == 3:
        return float(days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2])
    if len(parts) == 2:
        return float(days * 86400 + parts[0] * 60 + parts[1])
    if len(parts) == 1:
        return float(days * 86400 + parts[0])
    return 0.0


def find_server_process(pattern: str) -> tuple[int, float] | None:
    """Return (pid, uptime_seconds) for a matching local process, or None."""
    if not pattern:
        return None
    self_pid = os.getpid()
    try:
        import psutil  # type: ignore

        now = time.time()
        for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
            try:
                pid = int(proc.info["pid"])
                if pid == self_pid:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if pattern in cmdline and "mlx_server_supervisor.py" not in cmdline:
                    ct = proc.info.get("create_time") or now
                    return pid, max(0.0, now - float(ct))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return None
    except ImportError:
        pass

    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,etime=,args="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            pid_s, etime_s, args_s = parts
            if not pid_s.isdigit():
                continue
            pid = int(pid_s)
            if pid == self_pid:
                continue
            if pattern in args_s and "mlx_server_supervisor.py" not in args_s:
                return pid, _parse_etime(etime_s)
    except Exception:
        pass
    return None


def query_external_supervisor_exit_code(launchd_label: str, systemd_unit: str) -> int | None:
    """Read last exit status from launchd or systemd if configured."""
    if launchd_label and sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["launchctl", "list", launchd_label],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    if systemd_unit and sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(
                ["systemctl", "show", systemd_unit, "-p", "ExecMainStatus", "--value"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
            if out and out.lstrip("-").isdigit():
                return int(out)
        except Exception:
            pass
    return None


class PassiveServerObserver:
    """Passively monitors an external mlx_lm.server and its supervisor."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.stop_event = threading.Event()
        self.last_pid: int | None = None
        self.was_up = False
        self.http_up_since: float | None = None
        self.known_models: set[str] = set()
        if args.model:
            self.known_models.add(args.model)
        for r in KNOWN_REASONS:
            G_CRASHES.labels(reason=r).inc(0)
            G_REASON.labels(reason=r).set(0)

    def _probe_health(self) -> tuple[bool, list[str]]:
        try:
            with urllib.request.urlopen(self.args.health, timeout=3) as resp:
                if resp.status == 200:
                    models: list[str] = []
                    try:
                        body = resp.read().decode("utf-8", "replace")
                        payload = json.loads(body)
                        data = payload.get("data") if isinstance(payload, dict) else None
                        if isinstance(data, list):
                            models = [
                                str(item.get("id"))
                                for item in data
                                if isinstance(item, dict) and item.get("id")
                            ]
                    except Exception:
                        pass
                    return True, models
        except Exception as exc:
            debug(f"health check failed: {exc!r}", component="supervisor")
        return False, []

    def _record_transition_down(self, reason: str | None = None):
        rc = query_external_supervisor_exit_code(
            self.args.launchd_label, self.args.systemd_unit
        )
        G_EXIT.set(rc if rc is not None else -1)
        sniffed = reason or sniff_crash_reason(self.args.server_log)
        G_CRASHES.labels(reason=sniffed).inc()
        for r in KNOWN_REASONS:
            G_REASON.labels(reason=r).set(1 if r == sniffed else 0)
        info(f"observed server stop/crash reason={sniffed} exit_code={rc}", component="supervisor")

    def poll_once(self):
        proc_info = find_server_process(self.args.process_pattern)
        ok, served_models = self._probe_health()
        G_CHECKS.labels(result="ok" if ok else "fail").inc()

        pid = proc_info[0] if proc_info else None
        proc_uptime = proc_info[1] if proc_info else None

        # Detect external restarts (PID changed) or unexpected disappearance
        if self.last_pid is not None and pid is not None and pid != self.last_pid:
            G_RESTARTS.inc()
            if self.was_up:
                self._record_transition_down()
        elif self.was_up and pid is None and not ok:
            self._record_transition_down()
        elif not self.was_up and self.last_pid is not None and (pid is not None or ok):
            G_RESTARTS.inc()

        is_up = (pid is not None) or ok
        if pid is not None:
            self.last_pid = pid
        self.was_up = is_up

        G_UP.set(1 if is_up else 0)
        G_READY.set(1 if ok else 0)
        G_BACKOFF.set(0)

        if is_up and ok:
            G_STATE.set(1)
        elif is_up and not ok:
            G_STATE.set(2)
        else:
            G_STATE.set(0)

        if proc_uptime is not None:
            G_UPTIME.set(proc_uptime)
        elif ok:
            if self.http_up_since is None:
                self.http_up_since = time.monotonic()
            G_UPTIME.set(max(0.0, time.monotonic() - self.http_up_since))
        else:
            self.http_up_since = None
            G_UPTIME.set(0)

        for m in served_models:
            self.known_models.add(m)
        for m in list(self.known_models):
            loaded = 1 if (ok and m in served_models) else 0
            G_MODEL.labels(model=m).set(loaded)

    def run(self):
        G_STATE.set(0)
        G_BACKOFF.set(0)
        G_EXIT.set(0)
        info(
            f"passive server observer started health={self.args.health} "
            f"pattern={self.args.process_pattern!r}",
            component="supervisor",
        )
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                error(f"poll error: {exc!r}", component="supervisor")
            self.stop_event.wait(self.args.probe_interval)

    def stop(self):
        self.stop_event.set()


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/health", "/healthz"):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):
        pass


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main():
    setup_logging("mlx-supervisor", os.environ.get("MLX_MACBOOK_ID", "unknown"))
    p = argparse.ArgumentParser(
        description="Passive, read-only serving-status exporter for external mlx_lm.server."
    )
    p.add_argument(
        "--listen",
        default=os.environ.get("SUPERVISOR_LISTEN", LISTEN_DEFAULT),
        help=f"Listen host:port (default {LISTEN_DEFAULT})",
    )
    p.add_argument(
        "--health",
        default=HEALTH_URL_DEFAULT,
        help=f"Health URL to probe (default {HEALTH_URL_DEFAULT})",
    )
    p.add_argument(
        "--model",
        default=os.environ.get("MLX_MODEL", ""),
        help="Expected served model id (optional)",
    )
    p.add_argument(
        "--server-log",
        default=os.environ.get("MLX_SERVER_LOG", ""),
        help="Path to mlx_lm.server log for passive crash-reason sniffing",
    )
    p.add_argument(
        "--process-pattern",
        default=os.environ.get("MLX_PROCESS_PATTERN", "mlx_lm.server"),
        help="Process command-line substring to match (default mlx_lm.server)",
    )
    p.add_argument(
        "--launchd-label",
        default=os.environ.get("MLX_LAUNCHD_LABEL", ""),
        help="Optional macOS launchd service label to query exit status",
    )
    p.add_argument(
        "--systemd-unit",
        default=os.environ.get("MLX_SYSTEMD_UNIT", ""),
        help="Optional Linux systemd unit to query exit status",
    )
    p.add_argument(
        "--probe-interval",
        type=float,
        default=float(os.environ.get("SUPERVISOR_PROBE_INTERVAL", "5")),
        help="Probe interval in seconds (default 5)",
    )
    args = p.parse_args()

    host, _, port_s = args.listen.rpartition(":")
    host = host or "127.0.0.1"
    port = int(port_s)

    observer = PassiveServerObserver(args)
    httpd = _ReusableHTTPServer((host, port), MetricsHandler)

    def _shutdown(signum, _frame):
        info(f"received signal {signum}, stopping", component="supervisor")
        observer.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    t = threading.Thread(target=observer.run, daemon=True)
    t.start()
    info(f"serving metrics on http://{host}:{port}/metrics", component="supervisor")
    try:
        httpd.serve_forever()
    finally:
        observer.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
