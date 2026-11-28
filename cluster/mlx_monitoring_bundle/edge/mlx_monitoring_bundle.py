#!/usr/bin/env python3
"""
mlx_monitoring_bundle.py
========================
Single entrypoint for the mlx monitoring bundle edge node.

Spawns the Python exporters as subprocesses and manages the vmagent
sidecar (if containerised). All configuration comes from config/nodes.yml,
config/edge.env, and environment variables - nothing is hardcoded.

Config precedence (low to high):
  1. code defaults
  2. config/nodes.yml (node identity, central endpoints)
  3. config/edge.env (runtime flags, feature toggles, paths)
  4. actual environment variables (highest priority)

Components:
  - mlx_metrics_proxy.py     :8080  OpenAI proxy + Prometheus + OTLP + Opik
  - mlx_hw_telemetry.py      :9102  hardware telemetry
  - mlx_kv_cache_agent.py    :9104  KV-cache + context-length gauges
  - mlx_server_supervisor.py :9105  passive mlx_lm.server health/process observer
  - mlx_server_log_tailer.py :9106  server.log tailer -> OTLP logs

Push-only model: the edge initiates all outbound connections. No inbound
ports required on the Mac.
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
from pathlib import Path

import yaml

# Exporters live in edge/exporters and share mlx_logging.py. The bundle is
# launched from edge/, so add the exporters directory to the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent / "exporters"))

from mlx_logging import degraded, debug, error, info, setup_logging  # noqa: E402

BUNDLE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BUNDLE_DIR / "config"
LOG_DIR = BUNDLE_DIR / "logs"
PID_DIR = BUNDLE_DIR / "bin"

for d in (LOG_DIR, PID_DIR):
    d.mkdir(parents=True, exist_ok=True)

NODES_FILE = CONFIG_DIR / "nodes.yml"
ENV_FILE = CONFIG_DIR / "edge.env"
PID_FILE = PID_DIR / "mlx_monitoring_bundle.pid"


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf8")) or {}
    except Exception as exc:
        error(f"failed to parse {path}: {exc!r}", component="bundle")
        return {}


def load_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def merge_config() -> dict:
    """Merge config: nodes.yml < edge.env < actual environment."""
    config = load_yaml(NODES_FILE)
    config.update(load_env_file(ENV_FILE))
    config.update({k: v for k, v in os.environ.items()})
    return config


def central_value(config: dict, key: str, default: str = "") -> str:
    """Read a central endpoint from [central] or CENTRAL_<KEY> env vars."""
    central = config.get("central", {}) or {}
    env_key = f"CENTRAL_{key.upper()}"
    return (
        central.get(key)
        or config.get(env_key)
        or config.get(key.upper())
        or default
    )


def find_node_config(config: dict, macbook_id: str) -> dict:
    for nb in (config.get("devices") or config.get("macbooks") or []):
        if nb.get("id") == macbook_id:
            return nb
    return {}


def select_node_config(config: dict) -> dict:
    """Select the node for this host.

    MLX_DEVICE_ID / MLX_MACBOOK_ID wins. When it is unset and nodes.yml contains exactly one
    enabled entry, that entry is selected automatically, which keeps the
    single-node setup free of per-host flags.
    """
    macbook_id = (
        config.get("MLX_DEVICE_ID")
        or config.get("DEVICE_ID")
        or config.get("MLX_MACBOOK_ID")
        or config.get("MACBOOK_ID")
    )
    macbooks = [
        nb for nb in (config.get("devices") or config.get("macbooks") or [])
        if isinstance(nb, dict) and nb.get("enabled", True)
    ]
    if macbook_id:
        return find_node_config(config, macbook_id)
    if len(macbooks) == 1:
        return macbooks[0]
    return {}


def looks_like_path(value: str) -> bool:
    return value.startswith(("/", "~", "./", "../"))


def _resolve_python(configured: str | None, fallback_default: str) -> str:
    """Resolve a Python interpreter path.

    Returns the configured path if it exists, otherwise falls back to
    fallback_default if that exists, otherwise sys.executable.
    """
    import sys as _sys

    if configured:
        p = Path(configured).expanduser()
        if p.exists():
            return str(p)
    fb = Path(fallback_default).expanduser()
    if fb.exists():
        return str(fb)
    return _sys.executable


_SERVICE_MATCH_TOKENS: dict[str, tuple[str, ...]] = {
    "supervisor": ("mlx_server_supervisor.py",),
    "logtailer": ("mlx_server_log_tailer.py",),
    "kv": ("mlx_kv_cache_agent.py",),
    "hw": ("mlx_hw_telemetry.py",),
    "proxy": ("mlx_metrics_proxy.py",),
    "process": ("mlx_process_telemetry.py",),
    "sbom": ("mlx_sbom_collector.py",),
    "eval": ("mlx_accuracy_eval.py",),
    "topology": ("mlx_topology_agent.py",),
    "vmagent": ("vmagent",),
}
MAX_BUNDLE_LOG_BYTES = 10 * 1024 * 1024


def _pid_matches_service(pid: int, tokens: tuple[str, ...] | list[str]) -> bool:
    """Return True if the live process cmdline contains any expected service token."""
    cmdline = ""
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        if proc_cmdline.is_file():
            raw = proc_cmdline.read_bytes()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except OSError:
        cmdline = ""
    if not cmdline:
        try:
            res = subprocess.run(
                ["ps", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if res.returncode == 0:
                cmdline = res.stdout.strip()
        except Exception as exc:
            debug(f"ps inspection failed for pid={pid}: {exc!r}", component="bundle")
            return False
    if not cmdline:
        return False
    return any(tok in cmdline for tok in tokens)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_pid(pfile: Path) -> int | None:
    if pfile.exists():
        try:
            return int(pfile.read_text().strip())
        except Exception as exc:
            debug(f"read_pid failed: {exc!r}", component="bundle")
            return None
    return None


def write_pid(pfile: Path, pid: int):
    pfile.write_text(str(pid))


def remove_pid(pfile: Path):
    try:
        pfile.unlink()
    except FileNotFoundError:
        pass


class Bundle:
    def __init__(self, config: dict):
        self.config = config
        self.node_config = select_node_config(config)
        explicit_id = (
            config.get("MLX_DEVICE_ID")
            or config.get("DEVICE_ID")
            or config.get("MLX_MACBOOK_ID")
            or config.get("MACBOOK_ID")
        )
        enabled_devices = [
            nb for nb in (config.get("devices") or config.get("macbooks") or [])
            if isinstance(nb, dict) and nb.get("enabled", True)
        ]
        if not self.node_config and not explicit_id and len(enabled_devices) > 1:
            msg = (
                "multiple enabled devices in nodes.yml and no MLX_DEVICE_ID / "
                "MLX_MACBOOK_ID specified; cannot select node unambiguously"
            )
            print(msg, file=sys.stderr)
            error(msg, component="bundle")
            sys.exit(1)
        self.macbook = (
            explicit_id
            or self.node_config.get("id")
            or socket.gethostname()
        )
        self.device = self.macbook
        self.serial = (
            config.get("DEVICE_SERIAL")
            or config.get("MACBOOK_SERIAL")
            or self.node_config.get("serial")
            or "unknown"
        )

        # Central endpoints (combined host:port)
        self.central_vm = central_value(config, "victoria_metrics")
        self.central_otel = central_value(config, "otel")
        self.central_opik = central_value(config, "opik")
        self.central_topology = central_value(config, "topology")

        self.otlp_scheme = config.get("OTLP_SCHEME", "http")
        self.otlp_verify_tls = config.get("OTLP_VERIFY_TLS", "false").lower() == "true"

        self.otlp_endpoint = (
            f"{self.otlp_scheme}://{self.central_otel}" if self.central_otel else ""
        )
        self.opik_endpoint = (
            f"{self.otlp_scheme}://{self.central_opik}" if self.central_opik else ""
        )
        self.opik_otlp_endpoint = config.get("OPIK_OTLP_ENDPOINT", "")
        self.topology_endpoint = (
            config.get("CENTRAL_TOPOLOGY_URL")
            or (f"{self.otlp_scheme}://{self.central_topology}" if self.central_topology else "")
            or "http://127.0.0.1:8089"
        )

        model_path = (
            config.get("MLX_MODEL_PATH")
            or self.node_config.get("model_path")
            or ""
        )
        self.model_path = str(Path(model_path).expanduser()) if model_path else ""
        model = config.get("MLX_MODEL") or self.node_config.get("model") or ""
        if model and looks_like_path(model) and not self.model_path:
            self.model_path = str(Path(model).expanduser())
        if model and looks_like_path(model):
            model = Path(model.rstrip("/")).name
        # Optional; exporters discover the id from upstream or model_path.
        self.model = model or None

        raw_upstream = config_val(
            config, "MLX_UPSTREAM", "http://127.0.0.1:11234"
        )
        self.upstream_url = (
            raw_upstream if "://" in raw_upstream else f"http://{raw_upstream}"
        )
        self.upstream_addr = self.upstream_url.split("://", 1)[1].rstrip("/")

        self.venv_python = _resolve_python(
            config.get("VENV_PYTHON"), str(Path.home() / ".venv" / "bin" / "python")
        )

        self.exporter_log_dir = Path(config.get("EXPORTER_LOG_DIR", str(CONFIG_DIR.parent / "logs")))
        self.log_dir = Path(config.get("LOG_DIR", str(LOG_DIR)))
        self.pid_dir = Path(config.get("PID_DIR", str(PID_DIR)))

        self.procs: dict[str, subprocess.Popen] = {}

        self.enable_proxy = config.get("ENABLE_PROXY", "true").lower() == "true"
        self.enable_hw = config.get("ENABLE_HW_TELEMETRY", "true").lower() == "true"
        self.enable_kv = config.get("ENABLE_KV_AGENT", "true").lower() == "true"
        self.enable_server_monitor = config.get(
            "ENABLE_SERVER_MONITOR", config.get("ENABLE_SUPERVISOR", "true")
        ).lower() == "true"
        self.enable_logtailer = config.get("ENABLE_LOGTAILER", "true").lower() == "true"
        self.enable_process = config.get("ENABLE_PROCESS_PROFILER", "true").lower() == "true"
        self.enable_sbom = config.get("ENABLE_SBOM_COLLECTOR", "true").lower() == "true"
        self.enable_eval = config.get("ENABLE_ACCURACY_EVAL", "true").lower() == "true"
        self.enable_topology = config.get(
            "ENABLE_TOPOLOGY_AGENT", config.get("ENABLE_TOPOLOGY_DISCOVERY", "true")
        ).lower() == "true"
        self.topology_interval = config.get("TOPOLOGY_INTERVAL", "30.0")
        self.topology_listen = config.get("TOPOLOGY_LISTEN", "127.0.0.1:9110")
        self.enable_vmagent = config.get("ENABLE_VMAGENT", "true").lower() == "true"
        self.vmagent_binary = config.get("VMAGENT_BINARY", "vmagent")
        self.vmagent_config_template = BUNDLE_DIR / "vmagent-edge" / "vmagent.yml"
        self.vmagent_config_rendered = self.log_dir / "vmagent.yml"

    def _child_env(self, name: str) -> dict:
        """Build the child environment from the merged config.

        Exporters previously only received a handful of variables, so
        MLX_MODEL_PATH, MLX_UPSTREAM, MLX_NET_IFACE and similar settings from
        edge.env never reached them. Scalar config values are copied here,
        then the values the bundle owns are set explicitly.
        """
        env = dict(os.environ)
        for key, value in self.config.items():
            if isinstance(value, (str, int, float, bool)):
                env[key] = str(value)
        env["MLX_SERVICE_NAME"] = name
        env["MLX_DEVICE_ID"] = self.device
        env["MLX_MACBOOK_ID"] = self.macbook
        env["DEVICE_SERIAL"] = self.serial
        env["MACBOOK_SERIAL"] = self.serial
        env["MLX_MODEL"] = self.model or ""
        env["MLX_MODEL_PATH"] = self.model_path
        env["MLX_UPSTREAM"] = config_val(
            self.config, "MLX_UPSTREAM", "http://127.0.0.1:11234"
        )
        env["MLX_OTLP_ENDPOINT"] = self.otlp_endpoint
        env["OPIK_ENDPOINT"] = self.opik_endpoint
        env["OPIK_OTLP_ENDPOINT"] = self.opik_otlp_endpoint
        env["OTLP_VERIFY_TLS"] = "true" if self.otlp_verify_tls else "false"
        if NODES_FILE.is_file():
            env.setdefault("MLX_NODES_CONFIG", str(NODES_FILE))
        if self.node_config.get("rank") is not None:
            env.setdefault("MLX_RANK", str(self.node_config["rank"]))
            env.setdefault("MLX_SHARD_RANK", str(self.node_config["rank"]))
        if self.node_config.get("world_size") is not None:
            env.setdefault("MLX_WORLD_SIZE", str(self.node_config["world_size"]))
            env.setdefault("MLX_SHARD_WORLD_SIZE", str(self.node_config["world_size"]))
        if self.node_config.get("shard_layers"):
            env.setdefault("MLX_SHARD_LAYERS", str(self.node_config["shard_layers"]))
        if self.node_config.get("shard_kv_heads"):
            env.setdefault("MLX_SHARD_KV_HEADS", str(self.node_config["shard_kv_heads"]))
        if self.node_config.get("tp_size"):
            env.setdefault("MLX_TP_SIZE", str(self.node_config["tp_size"]))
        if self.node_config.get("prompt_cache_size"):
            env.setdefault("MLX_PROMPT_CACHE_SIZE", str(self.node_config["prompt_cache_size"]))
        if self.node_config.get("hostfile"):
            env.setdefault("MLX_HOSTFILE", str(self.node_config["hostfile"]))
        peer_port = self.node_config.get("peer_probe_port") or self.config.get("MLX_PEER_PROBE_PORT")
        if peer_port:
            env.setdefault("MLX_PEER_PROBE_PORT", str(peer_port))
        peer_app = self.node_config.get("peer_app_port") or self.config.get("MLX_PEER_APP_PORT")
        if peer_app:
            env.setdefault("MLX_PEER_APP_PORT", str(peer_app))
        aliases = self.node_config.get("model_aliases") or self.config.get("MLX_MODEL_ALIASES")
        if aliases:
            if isinstance(aliases, (list, tuple)):
                env["MLX_MODEL_ALIASES"] = ",".join(str(a) for a in aliases)
            else:
                env["MLX_MODEL_ALIASES"] = str(aliases)
        env["MLX_SERVER_LOG"] = config_val(self.config, "MLX_SERVER_LOG", str(self.exporter_log_dir / "server.log"))
        return env

    def _open_rotated_log(self, log_path: Path):
        try:
            if log_path.is_file() and log_path.stat().st_size > MAX_BUNDLE_LOG_BYTES:
                log_path.replace(log_path.with_name(f"{log_path.name}.1"))
        except OSError as exc:
            debug(f"log rotation skipped for {log_path}: {exc!r}", component="bundle")
        return open(log_path, "a", encoding="utf8")

    def _spawn_process(
        self, name: str, cmd: list, cwd: Path, env: dict, log_filename: str
    ) -> int:
        if not hasattr(self, "_spawn_specs"):
            self._spawn_specs = {}
        self._spawn_specs[name] = (list(cmd), cwd, dict(env), log_filename)
        with self._open_rotated_log(self.log_dir / log_filename) as logf:
            p = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=env,
                stdout=logf,
                stderr=logf,
                start_new_session=True,
            )
        self.procs[name] = p
        write_pid(self.pid_dir / f"{name}.pid", p.pid)
        return p.pid

    def _spawn(self, name: str, cmd: list) -> int:
        env = self._child_env(name)
        return self._spawn_process(
            name=name,
            cmd=cmd,
            cwd=BUNDLE_DIR / "exporters",
            env=env,
            log_filename=f"{name}.bundle.log",
        )

    def start_proxy(self):
        if not self.enable_proxy:
            return
        cmd = [
            self.venv_python, "mlx_metrics_proxy.py",
            "--listen", config_val(self.config, "PROXY_LISTEN", "127.0.0.1:8080"),
            "--upstream", self.upstream_addr,
            "--node-name", self.macbook,
            "--otlp-endpoint", self.otlp_endpoint,
            "--opik-endpoint", self.opik_endpoint,
            "--opik-otlp-endpoint", self.opik_otlp_endpoint,
            "--model", self.model or "",
            "--model-path", self.model_path,
            "--logprobs", config_val(self.config, "MLX_LOGPROBS", "5"),
            "--logprobs-stream-sample", config_val(self.config, "MLX_LOGPROBS_STREAM_SAMPLE", "0.25"),
            "--low-confidence-threshold", config_val(self.config, "MLX_LOW_CONFIDENCE", "0.5"),
            "--max-prompt-tokens", config_val(self.config, "MAX_PROMPT_TOKENS", "12000"),
            "--max-tokens-cap", config_val(self.config, "MAX_TOKENS_CAP", "4096"),
        ]
        if config_val(self.config, "MLX_PROXY_STRICT_MODEL", "true").lower() != "true":
            cmd.append("--no-strict-model")
        if self.otlp_verify_tls:
            cmd.append("--otlp-verify-tls")
        rate_limit = config_val(self.config, "RATE_LIMIT", "0")
        if rate_limit not in ("0", ""):
            cmd += ["--rate-limit", rate_limit, "--rate-window",
                    config_val(self.config, "RATE_WINDOW", "10.0")]
        self._spawn("proxy", cmd)

    def start_hw(self):
        if not self.enable_hw:
            return
        cmd = [
            self.venv_python, "mlx_hw_telemetry.py",
            "--node-name", self.macbook,
            "--listen", config_val(self.config, "HW_LISTEN", "127.0.0.1:9102"),
        ]
        if config_val(self.config, "ENABLE_CONTAINER_STATS", "false").lower() == "true":
            cmd.append("--container-stats")
        net_iface = config_val(self.config, "MLX_NET_IFACE", "")
        if net_iface:
            cmd += ["--net-iface", net_iface]
        self._spawn("hw", cmd)

    def start_kv(self):
        if not self.enable_kv:
            return
        log_file = config_val(self.config, "MLX_SERVER_LOG", str(self.exporter_log_dir / "server.log"))
        cmd = [
            self.venv_python, "mlx_kv_cache_agent.py",
            "--log-file", log_file,
            "--model", self.model or "",
            "--model-path", self.model_path,
            "--upstream", self.upstream_url,
            "--listen", config_val(self.config, "KV_LISTEN", "127.0.0.1:9104"),
        ]
        self._spawn("kv", cmd)

    def start_logtailer(self):
        if not self.enable_logtailer:
            return
        log_file = config_val(self.config, "MLX_SERVER_LOG", str(self.exporter_log_dir / "server.log"))
        cmd = [
            self.venv_python, "mlx_server_log_tailer.py",
            "--log-file", log_file,
            "--otlp-endpoint", self.otlp_endpoint,
            "--project", "mlx",
            "--listen", config_val(self.config, "LOGTAILER_LISTEN", "127.0.0.1:9106"),
        ]
        if self.otlp_verify_tls:
            cmd.append("--otlp-verify-tls")
        self._spawn("logtailer", cmd)

    def start_process(self):
        if not self.enable_process:
            return
        cmd = [
            self.venv_python, "mlx_process_telemetry.py",
            "--node-name", self.macbook,
            "--listen", config_val(self.config, "PROCESS_LISTEN", "127.0.0.1:9107"),
            "--interval", config_val(self.config, "PROCESS_SAMPLE_INTERVAL", "5.0"),
            "--top-k", config_val(self.config, "PROCESS_TOP_K", "10"),
        ]
        self._spawn("process", cmd)

    def start_sbom(self):
        if not self.enable_sbom:
            return
        base_dev = config_val(self.config, "SBOM_BASE_DEVICE", "") or self.macbook
        cmd = [
            self.venv_python, "mlx_sbom_collector.py",
            "--node-name", self.macbook,
            "--listen", config_val(self.config, "SBOM_LISTEN", "127.0.0.1:9108"),
            "--interval", config_val(self.config, "SBOM_SCAN_INTERVAL", "3600.0"),
            "--base-device", base_dev,
        ]
        if config_val(self.config, "SBOM_SKIP_PACKAGE_METRICS", "false").lower() == "true":
            cmd.append("--skip-package-metrics")
        self._spawn("sbom", cmd)

    def start_eval(self):
        if not self.enable_eval:
            return
        cmd = [
            self.venv_python, "mlx_accuracy_eval.py",
            "--listen", config_val(self.config, "EVAL_LISTEN", "127.0.0.1:9109"),
            "--model", self.model or "",
            "--eval-interval", config_val(self.config, "EVAL_INTERVAL", "1800"),
            "--judge-interval", config_val(self.config, "JUDGE_INTERVAL", "60"),
        ]
        if self.opik_endpoint:
            cmd += ["--opik-base", self.opik_endpoint]
        judge_url = config_val(self.config, "JUDGE_URL", "")
        if judge_url:
            cmd += ["--judge-url", judge_url]
        judge_model = config_val(self.config, "JUDGE_MODEL", "")
        if judge_model:
            cmd += ["--judge-model", judge_model]
        if config_val(self.config, "ENABLE_CANARY_EVAL", "true").lower() != "true":
            cmd.append("--no-canary")
        if config_val(self.config, "ENABLE_OPIK_JUDGE", "true").lower() != "true":
            cmd.append("--no-judge")
        if self.otlp_verify_tls:
            cmd.append("--verify-tls")
        self._spawn("eval", cmd)

    def start_topology(self):
        if not self.enable_topology:
            return
        cmd = [
            self.venv_python, "mlx_topology_agent.py",
            "--device-id", self.device,
            "--central-url", self.topology_endpoint,
            "--vmagent-url", f"{self.otlp_scheme}://127.0.0.1:8429",
            "--interval", str(self.topology_interval),
            "--listen", config_val(self.config, "TOPOLOGY_LISTEN", "127.0.0.1:9110"),
        ]
        self._spawn("topology", cmd)

    def _render_vmagent_config(self) -> bool:
        """Render vmagent.yml template with environment variables."""
        if not self.vmagent_config_template.exists():
            error(f"vmagent template not found: {self.vmagent_config_template}", component="bundle")
            return False
        try:
            template = self.vmagent_config_template.read_text(encoding="utf8")
            # Build substitution dict from config + node config
            def _normalize_scrape_target(addr: str) -> str:
                if addr.startswith("0.0.0.0:"):
                    return f"127.0.0.1:{addr.split(':', 1)[1]}"
                if addr.startswith("[::]:"):
                    return f"127.0.0.1:{addr.split(']:', 1)[1]}"
                return addr

            subs = {
                "CENTRAL_VICTORIA_METRICS": self.central_vm,
                "OTLP_SCHEME": self.otlp_scheme,
                "DEVICE_ID": self.device,
                "DEVICE_SERIAL": self.serial,
                "MACBOOK_ID": self.macbook,
                "MACBOOK_SERIAL": self.serial,
                "VMAGENT_SCRAPE_INTERVAL": config_val(self.config, "VMAGENT_SCRAPE_INTERVAL", "10s"),
                "VMAGENT_MAX_SHARDS": config_val(self.config, "VMAGENT_MAX_SHARDS", "200"),
                "VMAGENT_QUEUE_CAPACITY": config_val(self.config, "VMAGENT_QUEUE_CAPACITY", "5000"),
                "VMAGENT_PERSISTENT_QUEUE": config_val(self.config, "VMAGENT_PERSISTENT_QUEUE", ""),
                "PROXY_LISTEN": _normalize_scrape_target(config_val(self.config, "PROXY_LISTEN", "127.0.0.1:8080")),
                "HW_LISTEN": _normalize_scrape_target(config_val(self.config, "HW_LISTEN", "127.0.0.1:9102")),
                "KV_LISTEN": _normalize_scrape_target(config_val(self.config, "KV_LISTEN", "127.0.0.1:9104")),
                "SUPERVISOR_LISTEN": _normalize_scrape_target(config_val(self.config, "SUPERVISOR_LISTEN", "127.0.0.1:9105")),
                "LOGTAILER_LISTEN": _normalize_scrape_target(config_val(self.config, "LOGTAILER_LISTEN", "127.0.0.1:9106")),
                "PROCESS_LISTEN": _normalize_scrape_target(config_val(self.config, "PROCESS_LISTEN", "127.0.0.1:9107")),
                "SBOM_LISTEN": _normalize_scrape_target(config_val(self.config, "SBOM_LISTEN", "127.0.0.1:9108")),
                "EVAL_LISTEN": _normalize_scrape_target(config_val(self.config, "EVAL_LISTEN", "127.0.0.1:9109")),
                "TOPOLOGY_LISTEN": _normalize_scrape_target(config_val(self.config, "TOPOLOGY_LISTEN", "127.0.0.1:9110")),
            }
            # Single-pass substitution: replace ${VAR} and ${VAR:-default}.

            def repl(m):
                expr = m.group(1)
                if ":-" in expr:
                    var, default = expr.split(":-", 1)
                    return subs.get(var, default)
                if expr in subs:
                    return subs[expr]
                degraded(f"vmagent template variable {expr} has no value",
                         component="bundle")
                return ""

            template = re.sub(r"\$\{([^}]+)\}", repl, template)
            self.vmagent_config_rendered.write_text(template, encoding="utf8")
            info(f"rendered vmagent config: {self.vmagent_config_rendered}", component="bundle")
            return True
        except Exception as exc:
            error(f"failed to render vmagent config: {exc!r}", component="bundle")
            return False

    def start_vmagent(self):
        if not self.enable_vmagent:
            return
        if not self._render_vmagent_config():
            return
        remote_write_urls = []
        rw_env = config_val(self.config, "VMAGENT_REMOTE_WRITE_URL", "")
        if rw_env:
            for u in rw_env.split(","):
                u = u.strip()
                if u:
                    remote_write_urls.append(u if "://" in u else f"{self.otlp_scheme}://{u}/api/v1/write")
        elif self.central_vm:
            for vm in self.central_vm.split(","):
                vm = vm.strip()
                if vm:
                    remote_write_urls.append(f"{self.otlp_scheme}://{vm}/api/v1/write")
        if not remote_write_urls:
            remote_write_urls.append(f"{self.otlp_scheme}://127.0.0.1:8428/api/v1/write")

        cmd = [
            self.vmagent_binary,
            "-promscrape.config", str(self.vmagent_config_rendered),
            "-promscrape.configCheckInterval", "30s",
        ]
        for r_url in remote_write_urls:
            cmd += ["-remoteWrite.url", r_url]
        cmd += ["-remoteWrite.maxDiskUsagePerURL", "10GB"]
        pq_dir = config_val(self.config, "VMAGENT_PERSISTENT_QUEUE", "")
        if pq_dir:
            pq_path = Path(pq_dir).expanduser()
            pq_path.mkdir(parents=True, exist_ok=True)
            cmd += ["-remoteWrite.tmpDataPath", str(pq_path)]
        if self.otlp_scheme == "https" and not self.otlp_verify_tls:
            cmd.append("-remoteWrite.tlsSkipVerify=true")
        env = dict(os.environ)
        env["MLX_SERVICE_NAME"] = "vmagent"
        env["MLX_DEVICE_ID"] = self.device
        env["MLX_MACBOOK_ID"] = self.macbook
        pid = self._spawn_process(
            name="vmagent",
            cmd=cmd,
            cwd=BUNDLE_DIR,
            env=env,
            log_filename="vmagent.bundle.log",
        )
        info(f"started vmagent (pid={pid})", component="bundle")

    def start_server_monitor(self):
        if not self.enable_server_monitor:
            return
        health_url = config_val(
            self.config,
            "MLX_HEALTH_URL",
            f"{self.upstream_url.rstrip('/')}/v1/models",
        )
        cmd = [
            self.venv_python, "mlx_server_supervisor.py",
            "--listen", config_val(self.config, "SUPERVISOR_LISTEN", "127.0.0.1:9105"),
            "--health", health_url,
            "--model", self.model or "",
            "--server-log", config_val(self.config, "MLX_SERVER_LOG", str(self.exporter_log_dir / "server.log")),
            "--process-pattern", config_val(self.config, "MLX_PROCESS_PATTERN", "mlx_lm.server"),
            "--probe-interval", config_val(self.config, "SUPERVISOR_PROBE_INTERVAL", "5"),
        ]
        launchd_label = config_val(self.config, "MLX_LAUNCHD_LABEL", "")
        if launchd_label:
            cmd += ["--launchd-label", launchd_label]
        systemd_unit = config_val(self.config, "MLX_SYSTEMD_UNIT", "")
        if systemd_unit:
            cmd += ["--systemd-unit", systemd_unit]
        if self.model_path:
            cmd += ["--model-path", self.model_path]
        hostfile = config_val(self.config, "MLX_HOSTFILE", self.node_config.get("hostfile", ""))
        if hostfile:
            cmd += ["--hostfile", str(hostfile)]
        if NODES_FILE.is_file():
            cmd += ["--nodes-config", str(NODES_FILE)]
        self._spawn("supervisor", cmd)

    def status(self) -> dict:
        pids = {}
        for name in [
            "supervisor", "logtailer", "kv", "hw", "proxy", "process", "sbom", "eval", "topology", "vmagent"
        ]:
            pfile = self.pid_dir / f"{name}.pid"
            pids[name] = read_pid(pfile)
        return {
            "bundle_pid": read_pid(PID_FILE),
            "pids": pids,
            "device": self.device,
            "macbook": self.macbook,
            "model": self.model,
            "model_path": self.model_path,
            "central": {
                "victoria_metrics": self.central_vm,
                "otel": self.central_otel,
                "opik": self.central_opik,
                "topology": self.topology_endpoint,
            },
        }

    def _signal_pid(self, pid: int, sig: int, label: str) -> None:
        try:
            os.killpg(os.getpgid(pid), sig)
        except OSError as exc:
            debug(f"killpg {label} failed: {exc!r}", component="bundle")
            try:
                os.kill(pid, sig)
            except OSError as exc2:
                debug(f"kill {label} failed: {exc2!r}", component="bundle")

    def stop(self):
        # Stop verified child exporters before signalling any external bundle PID so an
        # in-process signal handler never kills itself mid-cleanup, and reused PIDs are safe.
        pending: list[tuple[str, int, tuple[str, ...]]] = []
        for name in [
            "supervisor", "logtailer", "kv", "hw", "proxy", "process", "sbom", "eval", "topology", "vmagent"
        ]:
            pfile = self.pid_dir / f"{name}.pid"
            pid = read_pid(pfile)
            tokens = _SERVICE_MATCH_TOKENS.get(name, (name,))
            if pid and pid != os.getpid() and pid_alive(pid) and _pid_matches_service(pid, tokens):
                self._signal_pid(pid, signal.SIGTERM, name)
                pending.append((name, pid, tokens))
            elif pid and pid != os.getpid() and pid_alive(pid):
                debug(f"skipping stale/reused pid={pid} for {name}", component="bundle")
            remove_pid(pfile)

        if pending:
            deadline = time.monotonic() + 5.0
            while pending and time.monotonic() < deadline:
                still_alive = [
                    (name, pid, tokens) for (name, pid, tokens) in pending
                    if pid_alive(pid)
                ]
                pending = still_alive
                if pending:
                    time.sleep(0.1)
            for name, pid, tokens in pending:
                if pid_alive(pid) and _pid_matches_service(pid, tokens):
                    degraded(f"{name} (pid={pid}) did not exit on SIGTERM; sending SIGKILL",
                             component="bundle")
                    self._signal_pid(pid, signal.SIGKILL, name)

        bp = read_pid(PID_FILE)
        if (
            bp
            and bp != os.getpid()
            and pid_alive(bp)
            and _pid_matches_service(bp, ("mlx_monitoring_bundle",))
        ):
            self._signal_pid(bp, signal.SIGTERM, "bundle")
            deadline = time.monotonic() + 5.0
            while pid_alive(bp) and time.monotonic() < deadline:
                time.sleep(0.1)
            if pid_alive(bp) and _pid_matches_service(bp, ("mlx_monitoring_bundle",)):
                self._signal_pid(bp, signal.SIGKILL, "bundle")
        remove_pid(PID_FILE)

    def start(self):
        bp = read_pid(PID_FILE)
        if bp and pid_alive(bp) and _pid_matches_service(bp, ("mlx_monitoring_bundle",)):
            info("bundle already running", component="bundle")
            return
        if not self.central_vm or not self.central_otel:
            error(
                "central.victoria_metrics and central.otel must be set in "
                "config/nodes.yml (or CENTRAL_VICTORIA_METRICS / CENTRAL_OTEL)",
                component="bundle",
            )
            return
        write_pid(PID_FILE, os.getpid())
        try:
            os.setsid()
        except OSError as exc:
            debug(f"os.setsid failed: {exc!r}", component="bundle")

        started = []
        if self.enable_server_monitor:
            self.start_server_monitor()
            started.append("supervisor:9105")
        if self.enable_kv:
            self.start_kv()
            started.append("kv:9104")
        if self.enable_logtailer:
            self.start_logtailer()
            started.append("logtailer:9106")
        if self.enable_proxy:
            self.start_proxy()
            started.append("proxy:8080")
        if self.enable_hw:
            self.start_hw()
            started.append("hw:9102")
        if self.enable_process:
            self.start_process()
            started.append("process:9107")
        if self.enable_sbom:
            self.start_sbom()
            started.append("sbom:9108")
        if self.enable_eval:
            self.start_eval()
            started.append("eval:9109")
        if self.enable_topology:
            self.start_topology()
            started.append("topology:9110")
        if self.enable_vmagent:
            self.start_vmagent()
            started.append("vmagent")

        info(f"started: {', '.join(started)}", component="bundle")

        stop_event = threading.Event()

        def _handle_exit(signum, frame):
            stop_event.set()

        signal.signal(signal.SIGTERM, _handle_exit)
        signal.signal(signal.SIGINT, _handle_exit)

        now_start = time.monotonic()
        proc_started_at: dict[str, float] = {n: now_start for n in self.procs}
        backoff_s: dict[str, float] = {}
        restart_at: dict[str, float] = {}
        spawn_specs = getattr(self, "_spawn_specs", {})

        try:
            while not stop_event.wait(2):
                now = time.monotonic()
                for name, proc in list(self.procs.items()):
                    if name in restart_at:
                        if now >= restart_at[name] and name in spawn_specs:
                            cmd, cwd, env, log_filename = spawn_specs[name]
                            try:
                                new_pid = self._spawn_process(name, cmd, cwd, env, log_filename)
                                proc_started_at[name] = now
                                restart_at.pop(name, None)
                                info(f"restarted child process {name} (pid={new_pid})", component="bundle")
                            except Exception as exc:
                                cur_backoff = min(60.0, backoff_s.get(name, 1.0) * 2.0)
                                backoff_s[name] = cur_backoff
                                restart_at[name] = now + cur_backoff
                                error(
                                    f"failed to respawn child process {name}: {exc!r}; "
                                    f"retrying in {cur_backoff:.0f}s",
                                    component="bundle",
                                )
                        continue
                    rc = proc.poll()
                    if rc is not None:
                        uptime = now - proc_started_at.get(name, now)
                        cur_backoff = 1.0 if uptime > 60.0 else min(60.0, backoff_s.get(name, 1.0) * 2.0)
                        if uptime > 60.0:
                            cur_backoff = 1.0
                        backoff_s[name] = cur_backoff
                        restart_at[name] = now + cur_backoff
                        error(
                            f"child process {name} (pid={proc.pid}) exited unexpectedly "
                            f"with rc={rc} after {uptime:.1f}s; restarting in {cur_backoff:.0f}s",
                            component="bundle",
                        )
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def config_val(config: dict, key: str, default: str) -> str:
    return config.get(key, os.environ.get(key, default))


def main():
    ap = argparse.ArgumentParser(description="mlx monitoring bundle entrypoint")
    ap.add_argument("action", choices=["start", "stop", "status", "restart", "vmagent-config"])
    args = ap.parse_args()

    config = merge_config()
    bundle = Bundle(config)

    setup_logging(service="mlx-bundle",
                  macbook=bundle.macbook,
                  log_dir=bundle.log_dir)

    if args.action == "vmagent-config":
        if bundle._render_vmagent_config() and bundle.vmagent_config_rendered.exists():
            print(bundle.vmagent_config_rendered.read_text(encoding="utf8"))
        return

    if args.action == "status":
        print(json.dumps(bundle.status(), indent=2))
        return

    if args.action == "stop":
        bundle.stop()
        info("bundle stopped", component="bundle")
        return

    if args.action in ("start", "restart"):
        if args.action == "restart":
            bundle.stop()
            time.sleep(2)
        bundle.start()
        return


if __name__ == "__main__":
    main()
