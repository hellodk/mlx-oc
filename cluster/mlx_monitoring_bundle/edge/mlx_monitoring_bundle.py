#!/usr/bin/env python3
"""
mlx_monitoring_bundle.py
========================
Single entrypoint for the mlx monitoring bundle edge node.

Spawns the five Python exporters as subprocesses and manages the vmagent
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
  - mlx_server_supervisor.py :9105  mlx_lm.server supervisor + auto-restart
  - mlx_server_log_tailer.py :9106  server.log tailer -> OTLP logs

Push-only model: the edge initiates all outbound connections. No inbound
ports required on the Mac.
"""

import argparse
import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

# Exporters live in edge/exporters and share mlx_logging.py. The bundle is
# launched from edge/, so add the exporters directory to the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent / "exporters"))

from mlx_logging import degraded, debug, error, info, setup_logging

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
    for nb in (config.get("macbooks") or []):
        if nb.get("id") == macbook_id:
            return nb
    return {}


def select_node_config(config: dict) -> dict:
    """Select the node for this host.

    MLX_MACBOOK_ID wins. When it is unset and nodes.yml contains exactly one
    enabled entry, that entry is selected automatically, which keeps the
    single-Mac setup free of per-host flags.
    """
    macbook_id = config.get("MLX_MACBOOK_ID") or config.get("MACBOOK_ID")
    macbooks = [
        nb for nb in (config.get("macbooks") or [])
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
        self.macbook = (
            config.get("MLX_MACBOOK_ID")
            or config.get("MACBOOK_ID")
            or self.node_config.get("id")
            or socket.gethostname()
        )
        self.serial = (
            config.get("MACBOOK_SERIAL")
            or self.node_config.get("serial")
            or "unknown"
        )

        # Central endpoints (combined host:port)
        self.central_vm = central_value(config, "victoria_metrics")
        self.central_otel = central_value(config, "otel")
        self.central_opik = central_value(config, "opik")

        self.otlp_scheme = config.get("OTLP_SCHEME", "http")
        self.otlp_verify_tls = config.get("OTLP_VERIFY_TLS", "false").lower() == "true"

        self.otlp_endpoint = (
            f"{self.otlp_scheme}://{self.central_otel}" if self.central_otel else ""
        )
        self.opik_endpoint = (
            f"{self.otlp_scheme}://{self.central_opik}" if self.central_opik else ""
        )
        self.opik_otlp_endpoint = config.get("OPIK_OTLP_ENDPOINT", "")

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
        self.server_port = self.upstream_addr.rsplit(":", 1)[-1]

        self.venv_python = _resolve_python(
            config.get("VENV_PYTHON"), str(Path.home() / ".venv" / "bin" / "python")
        )
        self.mlx_venv_python = _resolve_python(
            config.get("MLX_VENV_PYTHON"), str(Path.home() / "venvs" / "mlx" / "bin" / "python")
        )
        self.mlx_launch = config.get(
            "MLX_LAUNCH", str(Path.home() / "venvs" / "mlx" / "bin" / "mlx.launch")
        )

        self.exporter_log_dir = Path(config.get("EXPORTER_LOG_DIR", str(CONFIG_DIR.parent / "logs")))
        self.log_dir = Path(config.get("LOG_DIR", str(LOG_DIR)))
        self.pid_dir = Path(config.get("PID_DIR", str(PID_DIR)))

        self.procs: dict[str, subprocess.Popen] = {}

        self.enable_proxy = config.get("ENABLE_PROXY", "true").lower() == "true"
        self.enable_hw = config.get("ENABLE_HW_TELEMETRY", "true").lower() == "true"
        self.enable_kv = config.get("ENABLE_KV_AGENT", "true").lower() == "true"
        self.enable_supervisor = config.get("ENABLE_SUPERVISOR", "true").lower() == "true"
        self.enable_logtailer = config.get("ENABLE_LOGTAILER", "true").lower() == "true"
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
        env["MLX_MACBOOK_ID"] = self.macbook
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
        aliases = self.node_config.get("model_aliases") or self.config.get("MLX_MODEL_ALIASES")
        if aliases:
            if isinstance(aliases, (list, tuple)):
                env["MLX_MODEL_ALIASES"] = ",".join(str(a) for a in aliases)
            else:
                env["MLX_MODEL_ALIASES"] = str(aliases)
        env["MLX_SERVER_LOG"] = config_val(self.config, "MLX_SERVER_LOG", str(self.exporter_log_dir / "server.log"))
        return env

    def _spawn(self, name: str, cmd: list) -> int:
        logf = open(self.log_dir / f"{name}.bundle.log", "a", encoding="utf8")
        env = self._child_env(name)
        p = subprocess.Popen(
            cmd,
            cwd=str(BUNDLE_DIR / "exporters"),
            env=env,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )
        self.procs[name] = p
        write_pid(self.pid_dir / f"{name}.pid", p.pid)
        return p.pid

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
            "--logprobs", config_val(self.config, "MLX_LOGPROBS", "3"),
            "--logprobs-stream-sample", config_val(self.config, "MLX_LOGPROBS_STREAM_SAMPLE", "0.0"),
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

    def _render_vmagent_config(self) -> bool:
        """Render vmagent.yml template with environment variables."""
        if not self.vmagent_config_template.exists():
            error(f"vmagent template not found: {self.vmagent_config_template}", component="bundle")
            return False
        try:
            template = self.vmagent_config_template.read_text(encoding="utf8")
            # Build substitution dict from config + node config
            subs = {
                "CENTRAL_VICTORIA_METRICS": self.central_vm,
                "OTLP_SCHEME": self.otlp_scheme,
                "MACBOOK_ID": self.macbook,
                "MACBOOK_SERIAL": self.serial,
                "VMAGENT_SCRAPE_INTERVAL": config_val(self.config, "VMAGENT_SCRAPE_INTERVAL", "10s"),
                "VMAGENT_MAX_SHARDS": config_val(self.config, "VMAGENT_MAX_SHARDS", "200"),
                "VMAGENT_QUEUE_CAPACITY": config_val(self.config, "VMAGENT_QUEUE_CAPACITY", "5000"),
                "VMAGENT_PERSISTENT_QUEUE": config_val(self.config, "VMAGENT_PERSISTENT_QUEUE", ""),
            }
            # Single-pass substitution: replace ${VAR} and ${VAR:-default}.
            import re

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
        remote_write_url = f"{self.otlp_scheme}://{self.central_vm}/api/v1/write"
        cmd = [
            self.vmagent_binary,
            "-promscrape.config", str(self.vmagent_config_rendered),
            "-promscrape.configCheckInterval", "30s",
            "-remoteWrite.url", remote_write_url,
            "-remoteWrite.maxDiskUsagePerURL", "10GB",
        ]
        pq_dir = config_val(self.config, "VMAGENT_PERSISTENT_QUEUE", "")
        if pq_dir:
            cmd += [
                "-remoteWrite.persistentQueue", "true",
                "-remoteWrite.persistentQueueDir", pq_dir,
                "-remoteWrite.persistentQueueMaxSize", "10GB",
            ]
        if self.otlp_scheme == "https" and not self.otlp_verify_tls:
            cmd.append("-remoteWrite.tlsSkipVerify=true")
        logf = open(self.log_dir / "vmagent.bundle.log", "a", encoding="utf8")
        env = dict(os.environ)
        env["MLX_SERVICE_NAME"] = "vmagent"
        env["MLX_MACBOOK_ID"] = self.macbook
        p = subprocess.Popen(
            cmd,
            cwd=str(BUNDLE_DIR),
            env=env,
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )
        self.procs["vmagent"] = p
        write_pid(self.pid_dir / "vmagent.pid", p.pid)
        info(f"started vmagent (pid={p.pid})", component="bundle")

    def start_supervisor(self):
        if not self.enable_supervisor:
            return

        server_port = self.node_config.get("mlx_server_port") or self.server_port
        server_cmd_template = self.node_config.get("mlx_server_cmd") or config_val(
            self.config,
            "MLX_SERVER_CMD",
            f"{self.mlx_venv_python} -m mlx_lm.server --model {{model}} "
            f"--host 127.0.0.1 --port {{port}} "
            f"--prompt-cache-size 4 --prompt-cache-bytes 2g "
            f"--prompt-concurrency 4"
        )
        # The served model id is the basename; running from the model's parent
        # directory lets mlx_lm.server resolve it without an absolute path and
        # keeps request model ids stable.
        server_cwd = str(BUNDLE_DIR)
        model_arg = self.model or "unknown"
        if self.model_path:
            model_arg = self.model or Path(self.model_path.rstrip("/")).name
            server_cwd = str(Path(self.model_path).parent)
        srv_cmd = (
            server_cmd_template
            .replace("{model}", shlex.quote(model_arg))
            .replace("{port}", str(server_port))
        )

        cmd = [
            self.venv_python, "mlx_server_supervisor.py",
            "--command", srv_cmd,
            "--cwd", server_cwd,
            "--model", model_arg if model_arg != "unknown" else "",
            "--health", f"http://127.0.0.1:{server_port}/v1/models",
            "--server-log", config_val(self.config, "MLX_SERVER_LOG", str(self.exporter_log_dir / "server.log")),
            "--listen", config_val(self.config, "SUPERVISOR_LISTEN", "127.0.0.1:9105"),
            "--probe-interval", "5",
            "--backoff", "3",
            "--backoff-max", "30",
            "--hang-after", "15",
            "--backoff-reset", "120",
        ]
        if config_val(self.config, "SUPERVISOR_ATTACH_HEALTHY", "true").lower() != "true":
            cmd.append("--no-attach")
        self._spawn("supervisor", cmd)

    def status(self) -> dict:
        pids = {}
        for name in ["supervisor", "logtailer", "kv", "hw", "proxy", "vmagent"]:
            pfile = self.pid_dir / f"{name}.pid"
            pids[name] = read_pid(pfile)
        return {
            "bundle_pid": read_pid(PID_FILE),
            "pids": pids,
            "macbook": self.macbook,
            "model": self.model,
            "model_path": self.model_path,
            "central": {
                "victoria_metrics": self.central_vm,
                "otel": self.central_otel,
                "opik": self.central_opik,
            },
        }

    def stop(self):
        bp = read_pid(PID_FILE)
        if bp and pid_alive(bp):
            try:
                os.killpg(os.getpgid(bp), signal.SIGTERM)
            except Exception as exc:
                debug(f"killpg bundle failed: {exc!r}", component="bundle")
                try:
                    os.kill(bp, signal.SIGTERM)
                except Exception as exc2:
                    debug(f"kill bundle failed: {exc2!r}", component="bundle")
        for name in ["supervisor", "logtailer", "kv", "hw", "proxy", "vmagent"]:
            pfile = self.pid_dir / f"{name}.pid"
            pid = read_pid(pfile)
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception as exc:
                    debug(f"killpg {name} failed: {exc!r}", component="bundle")
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception as exc2:
                        debug(f"kill {name} failed: {exc2!r}", component="bundle")
                remove_pid(pfile)
        remove_pid(PID_FILE)

    def start(self):
        bp = read_pid(PID_FILE)
        if bp and pid_alive(bp):
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
        except Exception as exc:
            debug(f"os.setsid failed: {exc!r}", component="bundle")

        started = []
        if self.enable_supervisor:
            self.start_supervisor(); started.append("supervisor:9105")
            time.sleep(2)
        if self.enable_kv:
            self.start_kv(); started.append("kv:9104")
        if self.enable_logtailer:
            self.start_logtailer(); started.append("logtailer:9106")
        if self.enable_proxy:
            self.start_proxy(); started.append("proxy:8080")
        if self.enable_hw:
            self.start_hw(); started.append("hw:9102")
        if self.enable_vmagent:
            self.start_vmagent(); started.append("vmagent")

        info(f"started: {', '.join(started)}", component="bundle")
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            pass


def config_val(config: dict, key: str, default: str) -> str:
    return config.get(key, os.environ.get(key, default))


def main():
    ap = argparse.ArgumentParser(description="mlx monitoring bundle entrypoint")
    ap.add_argument("action", choices=["start", "stop", "status", "restart"])
    args = ap.parse_args()

    config = merge_config()
    bundle = Bundle(config)

    setup_logging(service="mlx-bundle",
                  macbook=bundle.macbook,
                  log_dir=bundle.log_dir)

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
