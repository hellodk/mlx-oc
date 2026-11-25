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
  - Incremental server log crash patterns (MLX_SERVER_LOG)

All configuration comes from environment variables and CLI arguments -
nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from mlx_logging import debug, error, info, parse_upstream_url, setup_logging
from mlx_model_info import model_info as _discover_model_info
from mlx_shard_topology import discover_shard_placement, probe_peer_reachability

LISTEN_DEFAULT = "127.0.0.1:9105"


def normalize_upstream_url(raw: str) -> str:
    """Ensure upstream URL has an HTTP(S) scheme and no trailing slash."""
    cleaned = (raw or "").strip() or "http://127.0.0.1:11234"
    _, _, _, base_url = parse_upstream_url(cleaned, default_port=11234)
    return base_url


def _parse_float_env(name: str, default: float) -> float:
    """Parse a positive float from environment, falling back cleanly."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
        if val > 0:
            return val
    except ValueError:
        pass
    return default


def _parse_int_env(name: str, default: int) -> int:
    """Parse a positive integer from environment, falling back cleanly."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        if val > 0:
            return val
    except ValueError:
        pass
    return default


def _parse_listen(listen_addr: str) -> tuple[str, int]:
    """Parse host:port listen address with explicit validation."""
    host, sep, port_s = listen_addr.rpartition(":")
    if not sep or not port_s.isdigit():
        raise ValueError(f"Invalid --listen address {listen_addr!r}; expected host:port")
    port = int(port_s)
    if not (0 <= port <= 65535):
        raise ValueError(f"Port out of range in --listen {listen_addr!r}")
    return host or "127.0.0.1", port


_DEFAULT_UPSTREAM = normalize_upstream_url(
    os.environ.get("MLX_UPSTREAM", "http://127.0.0.1:11234")
)
HEALTH_URL_DEFAULT = os.environ.get("MLX_HEALTH_URL", f"{_DEFAULT_UPSTREAM}/v1/models")

CRASH_REASONS = [
    (
        re.compile(r"\[METAL\].*(Command buffer execution failed|GPU errors)", re.I),
        "metal_gpu_error",
    ),
    (
        re.compile(r"\[METAL\].*(reset|timeout|out of memory)", re.I),
        "metal_gpu_error",
    ),
    (re.compile(r"\b(out of memory|OOM|allocation failed)\b", re.I), "oom"),
    (re.compile(r"\bkilled(?::\s*9)?\b", re.I), "oom"),
    (
        re.compile(
            r"(Traceback \(most recent call last\):|segmentation fault|SIGABRT|SIGSEGV)",
            re.I,
        ),
        "python_error",
    ),
]
KNOWN_REASONS = ("metal_gpu_error", "oom", "python_error", "exit", "unknown")

G_STATE = Gauge("mlx_server_state", "0=down 1=running_ready 2=up_not_ready 3=backoff")
G_UP = Gauge("mlx_server_up", "1 if the mlx_lm.server process or endpoint is alive")
G_READY = Gauge("mlx_server_ready", "1 if /v1/models returns HTTP 200")
G_MODEL = Gauge(
    "mlx_model_loaded",
    "1 once the health probe observed the model being served",
    ["model"],
)
G_RESTARTS = Counter(
    "mlx_server_restarts_total", "mlx_lm.server restarts observed"
)
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

G_SHARD_INFO = Gauge(
    "mlx_model_shard_info",
    "Static & runtime model shard placement info on this node",
    [
        "model",
        "rank",
        "world_size",
        "mode",
        "backend",
        "layer_range",
        "shard_files",
        "transport_ip",
        "iface",
    ],
)
G_SHARD_WORLD_SIZE = Gauge(
    "mlx_shard_world_size",
    "Total distributed ranks serving the model",
    ["model", "mode", "backend"],
)
G_SHARD_RANK = Gauge(
    "mlx_shard_rank",
    "Rank index assigned to this node",
    ["model"],
)
G_SHARD_LAYER_START = Gauge(
    "mlx_shard_layer_start",
    "First transformer layer index hosted on this rank",
    ["model", "rank"],
)
G_SHARD_LAYER_END = Gauge(
    "mlx_shard_layer_end",
    "Last transformer layer index hosted on this rank",
    ["model", "rank"],
)
G_SHARD_LAYERS_ASSIGNED = Gauge(
    "mlx_shard_layers_assigned",
    "Number of transformer layers hosted on this rank",
    ["model", "rank"],
)
G_SHARD_DIM_HIDDEN = Gauge(
    "mlx_shard_dim_hidden",
    "Hidden dimension visible on this shard",
    ["model", "rank"],
)
G_SHARD_DIM_KV_HEADS = Gauge(
    "mlx_shard_dim_kv_heads",
    "KV attention heads assigned to this rank (override, runtime inspection, or estimated fallback)",
    ["model", "rank"],
)
G_SHARD_DIM_ATTENTION_HEADS = Gauge(
    "mlx_shard_dim_attention_heads",
    "Query attention heads assigned to this rank (override, runtime inspection, or estimated fallback)",
    ["model", "rank"],
)
G_SHARD_DIM_HEAD_DIM = Gauge(
    "mlx_shard_dim_head_dim",
    "Head dimension visible on this shard",
    ["model", "rank"],
)
G_SHARD_GQA_RATIO = Gauge(
    "mlx_shard_gqa_ratio",
    "Grouped-query attention ratio (query heads / KV heads) on this shard",
    ["model", "rank"],
)
G_SHARD_ATTENTION_LAYERS_ASSIGNED = Gauge(
    "mlx_shard_attention_layers_assigned",
    "Number of transformer attention layers assigned to this shard",
    ["model", "rank"],
)
G_SHARD_DIM_KV_HEADS_EXACT = Gauge(
    "mlx_shard_dim_kv_heads_exact",
    "1 if shard KV heads allocation is explicitly configured/discovered or uniformly divisible, 0 if estimated fallback",
    ["model", "rank"],
)
G_SHARD_DIM_INTERMEDIATE = Gauge(
    "mlx_shard_dim_intermediate",
    "FFN intermediate dimension assigned to this rank",
    ["model", "rank"],
)
G_SHARD_WEIGHT_BYTES = Gauge(
    "mlx_shard_weight_bytes",
    "Expected model weight bytes hosted by this rank",
    ["model", "rank"],
)
G_SHARD_FILES_PRESENT = Gauge(
    "mlx_shard_files_present",
    "Number of safetensors shard files present locally",
    ["model", "rank"],
)
G_SHARD_FILES_EXPECTED = Gauge(
    "mlx_shard_files_expected",
    "Total safetensors shard files expected for this rank",
    ["model", "rank"],
)
G_SHARD_PEER_UP = Gauge(
    "mlx_shard_peer_up",
    "1 if peer shard node is reachable over TCP transport IP",
    ["rank", "peer_rank", "peer_ip"],
)
G_SHARD_PEER_APP_UP = Gauge(
    "mlx_shard_peer_app_up",
    "1 if peer application service endpoint is healthy and responding to HTTP probes",
    ["rank", "peer_rank", "peer_ip"],
)
G_SHARD_PEER_CONNECT_SECONDS = Histogram(
    "mlx_shard_peer_connect_seconds",
    "TCP connection establishment latency (SYN-ACK) to peer transport port - reachability probe only, not application RTT or MLX distributed communication latency",
    ["rank", "peer_rank", "peer_ip"],
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)
G_SHARD_PEER_APP_RTT_SECONDS = Histogram(
    "mlx_shard_peer_app_rtt_seconds",
    "Application request/response round-trip time (RTT) to peer service endpoint - HTTP probe only, not MLX collective communication latency",
    ["rank", "peer_rank", "peer_ip"],
    buckets=(0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
G_CLUSTER_SHARD_PLACEMENT = Gauge(
    "mlx_cluster_shard_placement_info",
    "Cluster-wide node-to-shard mapping discovered from hostfile/nodes config",
    ["target_device", "model", "rank", "world_size", "mode", "layer_range", "node_ip"],
)


class CrashLogSniffer:
    """Stateful log tailer that only scans bytes written since the last check."""

    def __init__(self, log_path: str, max_bytes: int = 65536) -> None:
        self.log_path = log_path
        self.max_bytes = max_bytes
        self._inode: int | None = None
        self._offset: int = 0
        self.advance_to_end()

    def advance_to_end(self) -> None:
        """Record current file inode and size so prior errors are not re-read."""
        if not self.log_path:
            return
        try:
            st = Path(self.log_path).stat()
            self._inode = st.st_ino
            self._offset = st.st_size
        except OSError as exc:
            debug(f"log stat failed for {self.log_path}: {exc!r}", component="supervisor")

    def sniff_new(self) -> str:
        """Scan only bytes appended since last advance_to_end()."""
        if not self.log_path:
            return "unknown"
        try:
            st = Path(self.log_path).stat()
            if self._inode != st.st_ino or st.st_size < self._offset:
                start = max(0, st.st_size - self.max_bytes)
            else:
                start = max(self._offset, st.st_size - self.max_bytes)
            with open(self.log_path, "rb") as fh:
                fh.seek(start)
                tail = fh.read().decode("utf-8", "replace")
            self._inode = st.st_ino
            self._offset = st.st_size
        except (OSError, ValueError) as exc:
            debug(f"log read failed for {self.log_path}: {exc!r}", component="supervisor")
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


def _cmdline_matches(
    cmdline_tokens: list[str], cmdline_str: str, pattern: str
) -> bool:
    """Match server process without colliding with grep/tail/vim or self."""
    if "mlx_server_supervisor.py" in cmdline_str:
        return False
    if pattern == "mlx_lm.server":
        for idx, tok in enumerate(cmdline_tokens):
            if tok == "mlx_lm.server" and idx > 0 and cmdline_tokens[idx - 1] == "-m":
                return True
            if tok.endswith("/mlx_lm.server") or tok == "mlx_lm.server":
                exe_name = Path(cmdline_tokens[0]).name if cmdline_tokens else ""
                if idx == 0 or re.fullmatch(r"python(?:3(?:\.\d+)?)?", exe_name):
                    return True
        return False
    return pattern in cmdline_str


def find_server_process(pattern: str) -> tuple[int, float] | None:
    """Return (pid, uptime_seconds) for a matching local process, or None."""
    if not pattern:
        return None
    self_pid = os.getpid()
    try:
        import psutil  # type: ignore

        now = time.time()
        for proc in psutil.process_iter(["pid", "cmdline", "create_time", "status"]):
            try:
                raw_pid = proc.info.get("pid")
                if raw_pid is None:
                    continue
                pid = int(raw_pid)
                if pid == self_pid:
                    continue
                if proc.info.get("status") == psutil.STATUS_ZOMBIE:
                    continue
                tokens = list(proc.info.get("cmdline") or [])
                cmdline = " ".join(tokens)
                if _cmdline_matches(tokens, cmdline, pattern):
                    ct = proc.info.get("create_time") or now
                    return pid, max(0.0, now - float(ct))
            except (psutil.Error, KeyError, TypeError, ValueError):
                continue
        return None
    except ImportError:
        pass
    except Exception as exc:
        debug(f"psutil scan failed, falling back to ps: {exc!r}", component="supervisor")

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
            if _cmdline_matches(args_s.split(), args_s, pattern):
                return pid, _parse_etime(etime_s)
    except Exception as exc:
        debug(f"ps fallback failed: {exc!r}", component="supervisor")
    return None


def query_external_supervisor_status(
    launchd_label: str, systemd_unit: str
) -> tuple[int | None, int | None, bool]:
    """Return (pid, last_exit_code, in_backoff) from launchd or systemd."""
    if launchd_label and sys.platform == "darwin":
        try:
            out = subprocess.check_output(
                ["launchctl", "list", launchd_label],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            pid_m = re.search(r'"PID"\s*=\s*(\d+)', out)
            exit_m = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
            ext_pid = int(pid_m.group(1)) if pid_m else None
            ext_exit = int(exit_m.group(1)) if exit_m else None
            in_backoff = ext_pid is None and ext_exit is not None and ext_exit != 0
            return ext_pid, ext_exit, in_backoff
        except Exception as exc:
            debug(f"launchctl list failed for {launchd_label}: {exc!r}", component="supervisor")

    if systemd_unit and sys.platform.startswith("linux"):
        try:
            out = subprocess.check_output(
                [
                    "systemctl",
                    "show",
                    systemd_unit,
                    "-p",
                    "MainPID,ExecMainStatus,ActiveState",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            raw_pid = kv.get("MainPID", "0").strip()
            raw_exit = kv.get("ExecMainStatus", "").strip()
            active_state = kv.get("ActiveState", "").strip()
            ext_pid = int(raw_pid) if raw_pid.isdigit() and int(raw_pid) > 0 else None
            ext_exit = int(raw_exit) if raw_exit.lstrip("-").isdigit() else None
            in_backoff = ext_pid is None and active_state in ("activating", "reloading")
            return ext_pid, ext_exit, in_backoff
        except Exception as exc:
            debug(f"systemctl show failed for {systemd_unit}: {exc!r}", component="supervisor")

    return None, None, False


def _model_in_served(expected: str, served_models: list[str]) -> bool:
    """Match expected model id verbatim or by basename of path-qualified ids."""
    if expected in served_models:
        return True
    for sm in served_models:
        cleaned = sm.rstrip("/")
        if cleaned.endswith(f"/{expected}") or Path(cleaned).name == expected:
            return True
    return False


class PassiveServerObserver:
    """Passively monitors an external mlx_lm.server and its supervisor."""

    def __init__(
        self,
        args: argparse.Namespace,
        metrics_lock: threading.Lock | None = None,
    ) -> None:
        self.args = args
        self.metrics_lock = metrics_lock or threading.Lock()
        self.stop_event = threading.Event()
        self.last_pid: int | None = None
        self.was_up = False
        self.had_initial_contact = False
        self.http_up_since: float | None = None
        self.consecutive_failures = 0
        self.last_ok = False
        self.last_served_models: list[str] = []
        self.known_models: set[str] = set()
        self.sniffer = CrashLogSniffer(args.server_log)
        self._shard_labels: dict[Any, set[tuple[str, ...]]] = {}
        self._last_shard_poll_at = 0.0
        self._cached_shard_snapshot: tuple[Any, dict[tuple[int, str], bool]] | None = None
        if args.model:
            self.known_models.add(args.model)
        for reason in KNOWN_REASONS:
            G_CRASHES.labels(reason=reason).inc(0)
            G_REASON.labels(reason=reason).set(0)

    def _collect_shard_snapshot(
        self, served_models: list[str]
    ) -> tuple[Any, dict[tuple[int, str], bool]]:
        """Gather shard placement & peer status outside metrics_lock."""
        now = time.monotonic()
        refresh_interval = max(
            15.0, float(getattr(self.args, "shard_refresh_interval", 30.0))
        )
        if (
            self._cached_shard_snapshot is not None
            and (now - self._last_shard_poll_at) < refresh_interval
        ):
            return self._cached_shard_snapshot

        eff_model = (served_models[-1] if served_models else "") or getattr(
            self.args, "model", ""
        )
        m_path = getattr(self.args, "model_path", "") or os.environ.get(
            "MLX_MODEL_PATH", ""
        )
        m_info = None
        if eff_model or m_path:
            try:
                m_info = _discover_model_info(
                    eff_model or None,
                    model_path=m_path or None,
                    upstream_url=getattr(self.args, "health", "").rsplit("/v1/", 1)[0]
                    or None,
                )
            except Exception as exc:
                debug(f"shard model_info failed: {exc!r}", component="supervisor")

        if m_info and not eff_model:
            eff_model = m_info.model_id

        nodes_devices: list[dict[str, Any]] | None = None
        device_ip = ""
        device_id = os.environ.get("MLX_DEVICE_ID") or os.environ.get(
            "MLX_MACBOOK_ID", ""
        )
        nodes_cfg = getattr(self.args, "nodes_config", "") or os.environ.get(
            "MLX_NODES_CONFIG", ""
        )
        hostfile = getattr(self.args, "hostfile", "") or os.environ.get(
            "MLX_HOSTFILE", ""
        )
        backend_ovr = getattr(self.args, "backend", "") or os.environ.get(
            "MLX_DISTRIBUTED_BACKEND", ""
        )
        if nodes_cfg and Path(nodes_cfg).is_file():
            try:
                import yaml

                cfg_data = yaml.safe_load(
                    Path(nodes_cfg).read_text(encoding="utf8")
                ) or {}
                devs = cfg_data.get("devices") or cfg_data.get("macbooks") or []
                if isinstance(devs, list):
                    nodes_devices = [d for d in devs if isinstance(d, dict)]
                    for d in nodes_devices:
                        if d.get("id") == device_id:
                            device_ip = str(d.get("ip") or "")
                            if not hostfile and d.get("hostfile"):
                                hostfile = str(d.get("hostfile"))
                            break
                cluster_sec = cfg_data.get("cluster") or {}
                if isinstance(cluster_sec, dict):
                    if not hostfile and cluster_sec.get("hostfile"):
                        hostfile = str(cluster_sec["hostfile"])
                    if not backend_ovr and cluster_sec.get("backend"):
                        backend_ovr = str(cluster_sec["backend"])
            except Exception as exc:
                debug(f"nodes_config parse skipped: {exc!r}", component="supervisor")

        raw_rank = getattr(self.args, "shard_rank", None)
        if raw_rank is None and os.environ.get("MLX_RANK", "").isdigit():
            raw_rank = int(os.environ["MLX_RANK"])
        raw_ws = getattr(self.args, "shard_world_size", None)
        if raw_ws is None and os.environ.get("MLX_WORLD_SIZE", "").isdigit():
            raw_ws = int(os.environ["MLX_WORLD_SIZE"])

        placement = discover_shard_placement(
            model_info_obj=m_info,
            model_id=eff_model or "unknown",
            model_path=m_path,
            process_pattern=getattr(self.args, "process_pattern", "mlx_lm.server"),
            hostfile=hostfile,
            mode_override=getattr(self.args, "parallel_mode", ""),
            backend_override=backend_ovr,
            rank_override=raw_rank,
            world_override=raw_ws,
            device_ip=device_ip,
            device_id=device_id,
            shard_layers_override=getattr(self.args, "shard_layers", ""),
            nodes_devices=nodes_devices,
        )
        probe_port = int(getattr(self.args, "peer_probe_port", 8081))
        app_port = int(getattr(self.args, "peer_app_port", 8081))
        peers_up = probe_peer_reachability(
            placement.peers, port=probe_port, app_port=app_port, probe_app_rtt=(app_port > 0)
        )
        self._cached_shard_snapshot = (placement, peers_up)
        self._last_shard_poll_at = now
        return placement, peers_up

    def _set_gauge_series(
        self, gauge: Gauge, desired: dict[tuple[str, ...], float]
    ) -> None:
        """Update a labeled Gauge and prune labels no longer active."""
        prev = self._shard_labels.get(gauge, set())
        for stale in prev - set(desired.keys()):
            try:
                gauge.remove(*stale)
            except KeyError:
                pass
        for labels, val in desired.items():
            gauge.labels(*labels).set(val)
        self._shard_labels[gauge] = set(desired.keys())

    def _publish_shard_metrics(
        self, placement: Any, peers_up: dict[tuple[int, str], bool]
    ) -> None:
        """Apply shard placement snapshot to Prometheus gauges under metrics_lock."""
        app_port = int(getattr(self.args, "peer_app_port", 8081))
        rk = str(placement.rank)
        ws = str(placement.world_size)
        mid = placement.model_id
        self._set_gauge_series(
            G_SHARD_INFO,
            {
                (
                    mid,
                    rk,
                    ws,
                    placement.mode,
                    placement.backend,
                    placement.layer_range,
                    placement.shard_files,
                    placement.transport_ip,
                    placement.iface,
                ): 1.0
            },
        )
        self._set_gauge_series(
            G_SHARD_WORLD_SIZE,
            {(mid, placement.mode, placement.backend): float(placement.world_size)},
        )
        self._set_gauge_series(G_SHARD_RANK, {(mid,): float(placement.rank)})
        self._set_gauge_series(
            G_SHARD_LAYER_START, {(mid, rk): float(placement.layer_start)}
        )
        self._set_gauge_series(
            G_SHARD_LAYER_END, {(mid, rk): float(placement.layer_end)}
        )
        self._set_gauge_series(
            G_SHARD_LAYERS_ASSIGNED, {(mid, rk): float(placement.layers_assigned)}
        )
        self._set_gauge_series(
            G_SHARD_DIM_HIDDEN, {(mid, rk): float(placement.dim_hidden)}
        )
        self._set_gauge_series(
            G_SHARD_DIM_KV_HEADS, {(mid, rk): float(placement.dim_kv_heads)}
        )
        self._set_gauge_series(
            G_SHARD_DIM_ATTENTION_HEADS,
            {(mid, rk): float(getattr(placement, "dim_attention_heads", 0))},
        )
        self._set_gauge_series(
            G_SHARD_DIM_HEAD_DIM,
            {(mid, rk): float(getattr(placement, "dim_head_dim", 0))},
        )
        self._set_gauge_series(
            G_SHARD_GQA_RATIO,
            {(mid, rk): float(getattr(placement, "gqa_ratio", 0))},
        )
        self._set_gauge_series(
            G_SHARD_ATTENTION_LAYERS_ASSIGNED,
            {(mid, rk): float(getattr(placement, "attention_layers_assigned", placement.layers_assigned))},
        )
        self._set_gauge_series(
            G_SHARD_DIM_KV_HEADS_EXACT,
            {(mid, rk): 1.0 if getattr(placement, "kv_heads_exact", True) else 0.0},
        )
        self._set_gauge_series(
            G_SHARD_DIM_INTERMEDIATE, {(mid, rk): float(placement.dim_intermediate)}
        )
        self._set_gauge_series(
            G_SHARD_WEIGHT_BYTES, {(mid, rk): float(placement.expected_weight_bytes)}
        )
        self._set_gauge_series(
            G_SHARD_FILES_PRESENT, {(mid, rk): float(placement.shard_files_present)}
        )
        self._set_gauge_series(
            G_SHARD_FILES_EXPECTED, {(mid, rk): float(placement.shard_files_expected)}
        )
        self._set_gauge_series(
            G_SHARD_PEER_UP,
            {
                (rk, str(pr), pip): 1.0 if (res.up if hasattr(res, "up") else bool(res)) else 0.0
                for (pr, pip), res in peers_up.items()
            },
        )
        if app_port > 0:
            self._set_gauge_series(
                G_SHARD_PEER_APP_UP,
                {
                    (rk, str(pr), pip): 1.0 if getattr(res, "app_up", False) else 0.0
                    for (pr, pip), res in peers_up.items()
                },
            )
        else:
            self._set_gauge_series(G_SHARD_PEER_APP_UP, {})
        for (pr, pip), res in peers_up.items():
            conn_sec = getattr(res, "connect_seconds", 0.0)
            app_rtt = getattr(res, "app_rtt_seconds", 0.0)
            is_up = getattr(res, "up", bool(res))
            app_up = getattr(res, "app_up", False)
            if is_up and conn_sec > 0.0:
                G_SHARD_PEER_CONNECT_SECONDS.labels(rk, str(pr), pip).observe(conn_sec)
            if app_up and app_rtt > 0.0:
                G_SHARD_PEER_APP_RTT_SECONDS.labels(rk, str(pr), pip).observe(app_rtt)
        self._set_gauge_series(
            G_CLUSTER_SHARD_PLACEMENT,
            {
                (
                    str(n["device"]),
                    mid,
                    str(n["rank"]),
                    str(n["world_size"]),
                    str(n["mode"]),
                    str(n["layer_range"]),
                    str(n["node_ip"]),
                ): 1.0
                for n in placement.cluster_nodes
            },
        )

    def _probe_health(self) -> tuple[bool, list[str]]:
        """Probe upstream health endpoint; treat any HTTP 200 as ready."""
        timeout = getattr(self.args, "health_timeout", 10.0)
        try:
            with urllib.request.urlopen(self.args.health, timeout=timeout) as resp:
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
                    except Exception as exc:
                        debug(f"health JSON parse skipped: {exc!r}", component="supervisor")
                    return True, models
        except Exception as exc:
            debug(f"health check failed: {exc!r}", component="supervisor")
        return False, []

    def _record_transition_down(
        self, exit_code: int | None, sniffed: str
    ) -> None:
        """Record a stop/crash transition without double-counting clean stops."""
        G_EXIT.set(exit_code if exit_code is not None else -1)
        if exit_code == 0:
            is_crash = False
        elif exit_code is not None and exit_code != 0:
            is_crash = True
        else:
            is_crash = sniffed not in ("exit", "unknown")
        if is_crash:
            G_CRASHES.labels(reason=sniffed).inc()
        for reason in KNOWN_REASONS:
            G_REASON.labels(reason=reason).set(1 if reason == sniffed else 0)
        info(
            f"observed server stop/crash reason={sniffed} exit_code={exit_code}",
            component="supervisor",
        )

    def poll_once(self) -> None:
        """Execute one passive observation cycle (process + HTTP + log)."""
        ext_pid, ext_exit, in_backoff = query_external_supervisor_status(
            self.args.launchd_label, self.args.systemd_unit
        )
        proc_info = find_server_process(self.args.process_pattern)
        pid = proc_info[0] if proc_info else ext_pid
        proc_uptime = proc_info[1] if proc_info else None

        raw_ok, served_models = self._probe_health()
        G_CHECKS.labels(result="ok" if raw_ok else "fail").inc()

        fail_threshold = max(1, int(getattr(self.args, "fail_threshold", 3)))
        if raw_ok:
            self.consecutive_failures = 0
            ok = True
            self.last_served_models = served_models
            self.sniffer.advance_to_end()
        else:
            self.consecutive_failures += 1
            if pid is None and self.args.process_pattern:
                # Never apply latency hysteresis when the configured OS process is gone.
                ok = False
                self.consecutive_failures = fail_threshold
            elif self.last_ok and self.consecutive_failures < fail_threshold:
                # Hysteresis: tolerate transient LLM GIL/Metal latency bursts
                ok = True
                served_models = self.last_served_models
            else:
                ok = False
        self.last_ok = ok

        is_up = (pid is not None) or ok
        pid_changed = (
            pid is not None and self.last_pid is not None and pid != self.last_pid
        )
        transitioned_down = (pid_changed and self.was_up) or (
            self.was_up and not is_up
        )
        # Perform log disk & shard index I/O before acquiring metrics_lock
        sniffed = self.sniffer.sniff_new() if transitioned_down else "unknown"
        placement, peers_up = self._collect_shard_snapshot(served_models)

        with self.metrics_lock:
            if pid_changed:
                G_RESTARTS.inc()
                if self.was_up:
                    self._record_transition_down(ext_exit, sniffed)
            elif self.was_up and not is_up:
                self._record_transition_down(ext_exit, sniffed)
            elif not self.was_up and self.had_initial_contact and is_up:
                if pid is None or self.last_pid is None or pid != self.last_pid:
                    G_RESTARTS.inc()

            if is_up:
                self.had_initial_contact = True
            if pid is not None:
                self.last_pid = pid
            self.was_up = is_up

            G_UP.set(1 if is_up else 0)
            G_READY.set(1 if ok else 0)
            G_BACKOFF.set(0)

            if in_backoff and not is_up:
                G_STATE.set(3)
            elif is_up and ok:
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

            # Prune stale dynamically-discovered models so unloaded models do not
            # leave permanent MLXModelNotLoaded alerts or leak cardinality.
            for m in list(self.known_models):
                if m != self.args.model and not _model_in_served(m, served_models) and ok:
                    self.known_models.discard(m)
                    try:
                        G_MODEL.remove(m)
                    except KeyError:
                        pass
            for m in served_models:
                self.known_models.add(m)
            for m in list(self.known_models):
                loaded = 1 if (ok and _model_in_served(m, served_models)) else 0
                G_MODEL.labels(model=m).set(loaded)

            self._publish_shard_metrics(placement, peers_up)

    def run(self) -> None:
        """Continuously poll until stop_event is set."""
        with self.metrics_lock:
            G_STATE.set(0)
            G_BACKOFF.set(0)
            G_EXIT.set(-1)
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

    def stop(self) -> None:
        """Signal the observation loop to exit."""
        self.stop_event.set()


class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler serving Prometheus /metrics and /healthz."""

    metrics_lock: threading.Lock = threading.Lock()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            with self.metrics_lock:
                body = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        pass


class _ReusableHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with SO_REUSEADDR enabled for fast restarts."""

    allow_reuse_address = True


def main() -> None:
    """CLI entrypoint for the passive server observer exporter."""
    setup_logging("mlx-supervisor", os.environ.get("MLX_MACBOOK_ID", "unknown"))
    p = argparse.ArgumentParser(
        description="Passive, read-only serving-status exporter for mlx_lm.server."
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
        default=_parse_float_env("SUPERVISOR_PROBE_INTERVAL", 5.0),
        help="Probe interval in seconds (default 5)",
    )
    p.add_argument(
        "--health-timeout",
        type=float,
        default=_parse_float_env("SUPERVISOR_HEALTH_TIMEOUT", 10.0),
        help="Health probe HTTP timeout in seconds (default 10)",
    )
    p.add_argument(
        "--fail-threshold",
        type=int,
        default=_parse_int_env("SUPERVISOR_FAIL_THRESHOLD", 3),
        help="Consecutive probe failures before marking not-ready (default 3)",
    )
    p.add_argument(
        "--model-path",
        default=os.environ.get("MLX_MODEL_PATH", ""),
        help="Local model directory for safetensors shard & dim inspection",
    )
    p.add_argument(
        "--hostfile",
        default=os.environ.get("MLX_HOSTFILE", ""),
        help="Optional distributed hostfile (JSON or text) for rank/peer mapping",
    )
    p.add_argument(
        "--parallel-mode",
        default=os.environ.get("MLX_PARALLEL_MODE", ""),
        help="Optional parallelism mode override (standalone|pipeline|tensor)",
    )
    p.add_argument(
        "--backend",
        default=os.environ.get("MLX_DISTRIBUTED_BACKEND", ""),
        help="Optional distributed backend override (ring|jaccl|mpi|nccl)",
    )
    p.add_argument(
        "--shard-rank",
        type=int,
        default=(
            int(os.environ["MLX_RANK"])
            if os.environ.get("MLX_RANK", "").isdigit()
            else None
        ),
        help="Optional explicit rank index override",
    )
    p.add_argument(
        "--shard-world-size",
        type=int,
        default=(
            int(os.environ["MLX_WORLD_SIZE"])
            if os.environ.get("MLX_WORLD_SIZE", "").isdigit()
            else None
        ),
        help="Optional explicit world size override",
    )
    p.add_argument(
        "--shard-layers",
        default=os.environ.get("MLX_SHARD_LAYERS", ""),
        help="Optional explicit layer range override (e.g. 0..15)",
    )
    p.add_argument(
        "--nodes-config",
        default=os.environ.get("MLX_NODES_CONFIG", ""),
        help="Optional path to nodes.yml for device-to-rank mapping",
    )
    p.add_argument(
        "--peer-probe-port",
        type=int,
        default=_parse_int_env("MLX_PEER_PROBE_PORT", 8081),
        help="TCP port to probe distributed peer transport reachability (0 disables)",
    )
    p.add_argument(
        "--peer-app-port",
        type=int,
        default=_parse_int_env("MLX_PEER_APP_PORT", 8081),
        help="TCP port to probe distributed peer application HTTP endpoint (0 disables)",
    )
    args = p.parse_args()

    try:
        host, port = _parse_listen(args.listen)
    except ValueError as exc:
        error(str(exc), component="supervisor")
        sys.exit(2)

    metrics_lock = threading.Lock()
    MetricsHandler.metrics_lock = metrics_lock
    observer = PassiveServerObserver(args, metrics_lock=metrics_lock)
    _ReusableHTTPServer.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    httpd = _ReusableHTTPServer((host, port), MetricsHandler)

    shutdown_once = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        if shutdown_once.is_set():
            return
        shutdown_once.set()
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
        t.join(timeout=5.0)
        httpd.server_close()


if __name__ == "__main__":
    main()
