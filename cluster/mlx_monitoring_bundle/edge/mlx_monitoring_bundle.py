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
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

from mlx_logging import degraded, error, info, setup_logging

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
    """Read a central endpoint from config/nodes.yml [central] section."""
    central = config.get("central", {}) or {}
    val = central.get(key) or os.environ.get(key.upper(), default)
    return val


def find_node_config(config: dict, macbook_id: str) -> dict:
    for nb in (config.get("macbooks") or []):
        if nb.get("id") == macbook_id:
            return nb
    return {}


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
        except Exception:
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
        self.macbook = config.get("MLX_MACBOOK_ID", config.get("MACBOOK_ID", "unknown"))
        self.serial = config.get("MACBOOK_SERIAL", "unknown")
        self.node_config = find_node_config(config, self.macbook) or {}

        # Central endpoints (combined host:port)
        self.central_vm = central_value(config, "victoria_metrics", "192.168.1.100:8428")
        self.central_otel = central_value(config, "otel", "192.168.1.100:4318")
        self.central_opik = central_value(config, "opik", "192.168.1.100:32173")

        self.otlp_scheme = config.get("OTLP_SCHEME", "http")
        self.otlp_verify_tls = config.get("OTLP_VERIFY_TLS", "false").lower() == "true"

        self.otlp_endpoint = f"{self.otlp_scheme}://{self.central_otel}"
        self.opik_endpoint = f"{self.otlp_scheme}://{self.central_opik}"
        self.opik_otlp_endpoint = config.get("OPIK_OTLP_ENDPOINT", "")

        self.model = (
            config.get("MLX_MODEL")
            or self.node_config.get("model")
        )  # optional; auto-discovered by exporters if unset

        self.venv_python = config.get("VENV_PYTHON", str(Path.home() / ".venv" / "bin" / "python"))
        self.mlx_venv_python = config.get(
            "MLX_VENV_PYTHON", str(Path.home() / "venvs" / "mlx" / "bin" / "python")
        )
        self.mlx_launch = config.get(
            "MLX_LAUNCH", str(Path.home() / "venvs" / "mlx" / "bin" / "mlx.launch")
        )

        self.exporter_log_dir = Path(config.get("EXPORTER_LOG_DIR", str(CONFIG_DIR.parent / "logs")))
        self.log_dir = Path(config.get("LOG_DIR", str(LOG_DIR)))
        self.pid_dir = Path(config.get("PID_DIR", str(PID_DIR)))

        self.ssh_user = config.get("RANK1_SSH_USER") or self.node_config.get("ssh_user", "")
        self.ssh_key = config.get("RANK1_SSH_KEY") or self.node_config.get("ssh_key", "")
        self.ssh_timeout = float(config.get("RANK1_SSH_CONNECT_TIMEOUT",
                                              self.node_config.get("ssh_connect_timeout", 5)))

        self.procs: dict[str, subprocess.Popen] = {}

        self.enable_proxy = config.get("ENABLE_PROXY", "true").lower() == "true"
        self.enable_hw = config.get("ENABLE_HW_TELEMETRY", "true").lower() == "true"
        self.enable_kv = config.get("ENABLE_KV_AGENT", "true").lower() == "true"
        self.enable_supervisor = config.get("ENABLE_SUPERVISOR", "true").lower() == "true"
        self.enable_logtailer = config.get("ENABLE_LOGTAILER", "true").lower() == "true"

    def _spawn(self, name: str, cmd: list) -> int:
        logf = open(self.log_dir / f"{name}.bundle.log", "a", encoding="utf8")
        env = dict(os.environ)
        env["MLX_SERVICE_NAME"] = name
        env["MLX_MACBOOK_ID"] = self.macbook
        env["MLX_MODEL"] = self.model or ""
        env["MLX_OTLP_ENDPOINT"] = self.otlp_endpoint
        env["OPIK_ENDPOINT"] = self.opik_endpoint
        env["OPIK_OTLP_ENDPOINT"] = self.opik_otlp_endpoint
        env["MLX_SERVER_LOG"] = str(self.exporter_log_dir / "server.log")
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
            "--listen", config_val(self.config, "PROXY_LISTEN", "0.0.0.0:8080"),
            "--upstream", config_val(self.config, "MLX_UPSTREAM", "127.0.0.1:8081"),
            "--node-name", self.macbook,
            "--otlp-endpoint", self.otlp_endpoint,
            "--opik-endpoint", self.opik_endpoint,
            "--opik-otlp-endpoint", self.opik_otlp_endpoint,
            "--model", self.model or "",
            "--logprobs", config_val(self.config, "MLX_LOGPROBS", "3"),
            "--logprobs-stream-sample", config_val(self.config, "MLX_LOGPROBS_STREAM_SAMPLE", "0.0"),
            "--low-confidence-threshold", config_val(self.config, "MLX_LOW_CONFIDENCE", "0.5"),
            "--max-prompt-tokens", config_val(self.config, "MAX_PROMPT_TOKENS", "12000"),
            "--max-tokens-cap", config_val(self.config, "MAX_TOKENS_CAP", "4096"),
        ]
        rate_limit = config_val(self.config, "RATE_LIMIT", "0")
        if rate_limit not in ("0", ""):
            cmd += ["--rate-limit", rate_limit, "--rate-window",
                    config_val(self.config, "RATE_WINDOW", "10.0")]
        self._spawn("proxy", cmd)

    def start_hw_rank0(self):
        if not self.enable_hw:
            return
        cmd = [
            self.venv_python, "mlx_hw_telemetry.py",
            "--node-name", self.macbook,
            "--listen", config_val(self.config, "HW_LISTEN", "0.0.0.0:9102"),
        ]
        self._spawn("hw0", cmd)

    def start_hw_rank1(self):
        if not self.enable_hw:
            return
        rank1_ip = self.node_config.get("ip", "")
        if not rank1_ip:
            degraded("no rank1 ip in nodes.yml; skipping rank1 hw telemetry",
                     component="bundle")
            return
        ssh_user = self.node_config.get("ssh_user", self.ssh_user)
        ssh_key = self.node_config.get("ssh_key", self.ssh_key)
        if not ssh_key:
            degraded("no ssh_key for rank1; skipping rank1 hw telemetry "
                     "(password not supported for long-running daemon)",
                     component="bundle")
            return
        listen = config_val(self.config, "HW_LISTEN", "0.0.0.0:9102")
        remote_cmd = (
            f"nohup '{self.mlx_venv_python}' '{BUNDLE_DIR}/exporters/mlx_hw_telemetry.py' "
            f"--node-name rank1 --listen {listen} "
            f"> '{self.exporter_log_dir}/hw1.log' 2>&1 & echo $!"
        )
        try:
            p = subprocess.Popen(
                ["ssh", "-o", f"ConnectTimeout={self.ssh_timeout}",
                 "-i", ssh_key, f"{ssh_user}@{rank1_ip}", remote_cmd],
                cwd=str(BUNDLE_DIR), env=dict(os.environ),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            out, err = p.communicate(timeout=self.ssh_timeout + 5)
            pid = out.strip().decode()
            if pid:
                write_pid(self.pid_dir / "hw1.pid", int(pid))
        except Exception as exc:
            error(f"rank1 hw telemetry ssh failed: {exc!r}", component="bundle")

    def start_kv(self):
        if not self.enable_kv:
            return
        cmd = [
            self.venv_python, "mlx_kv_cache_agent.py",
            "--log-file", str(self.exporter_log_dir / "server.log"),
            "--model", self.model or "",
            "--upstream", f"http://127.0.0.1:8081",
            "--listen", config_val(self.config, "KV_LISTEN", "0.0.0.0:9104"),
        ]
        self._spawn("kv", cmd)

    def start_logtailer(self):
        if not self.enable_logtailer:
            return
        cmd = [
            self.venv_python, "mlx_server_log_tailer.py",
            "--log-file", str(self.exporter_log_dir / "server.log"),
            "--otlp-endpoint", self.otlp_endpoint,
            "--project", "mlx",
            "--listen", config_val(self.config, "LOGTAILER_LISTEN", "0.0.0.0:9106"),
        ]
        self._spawn("logtailer", cmd)

    def start_supervisor(self):
        if not self.enable_supervisor:
            return
        srv_cmd = (
            f"{self.mlx_launch} --hostfile {BUNDLE_DIR}/hosts.json --backend ring "
            f"--cwd {BUNDLE_DIR} --python {self.mlx_venv_python} "
            f"-- {self.mlx_venv_python} -m mlx_lm.server --model {self.model or 'unknown'} "
            f"--host 127.0.0.1 --port 8081 "
            f"--chat-template-args '{{\"enable_thinking\":false}}' "
            f"--prompt-cache-size 4 --prompt-cache-bytes 2g --prompt-concurrency 4"
        )
        cmd = [
            self.venv_python, "mlx_server_supervisor.py",
            "--command", srv_cmd,
            "--model", self.model or "",
            "--health", f"http://127.0.0.1:8081/v1/models",
            "--server-log", str(self.exporter_log_dir / "server.log"),
            "--listen", config_val(self.config, "SUPERVISOR_LISTEN", "0.0.0.0:9105"),
            "--probe-interval", "5",
            "--backoff", "3",
            "--backoff-max", "30",
            "--hang-after", "15",
            "--backoff-reset", "120",
        ]
        if self.ssh_key:
            rank1_ip = self.node_config.get("ip", "")
            if rank1_ip:
                cmd += ["--rank1-ssh-target",
                        f"{self.ssh_user or 'dk'}@{rank1_ip}",
                        "--ssh-timeout", str(self.ssh_timeout)]
        self._spawn("supervisor", cmd)

    def status(self) -> dict:
        pids = {}
        for name in ["supervisor", "logtailer", "kv", "hw0", "hw1", "proxy"]:
            pfile = self.pid_dir / f"{name}.pid"
            pids[name] = read_pid(pfile)
        return {
            "bundle_pid": read_pid(PID_FILE),
            "pids": pids,
            "macbook": self.macbook,
        }

    def stop(self):
        bp = read_pid(PID_FILE)
        if bp and pid_alive(bp):
            try:
                os.killpg(os.getpgid(bp), signal.SIGTERM)
            except Exception:
                try:
                    os.kill(bp, signal.SIGTERM)
                except Exception:
                    pass
        for name in ["supervisor", "logtailer", "kv", "hw0", "proxy"]:
            pfile = self.pid_dir / f"{name}.pid"
            pid = read_pid(pfile)
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
                remove_pid(pfile)
        rank1_ip = self.node_config.get("ip", "")
        if rank1_ip and self.ssh_key:
            try:
                subprocess.run(
                    ["ssh", "-o", f"ConnectTimeout={self.ssh_timeout}",
                     "-i", self.ssh_key, f"{self.ssh_user or 'dk'}@{rank1_ip}",
                     "pkill -f 'mlx_hw_telemetry.py' 2>/dev/null"],
                    timeout=5,
                )
            except Exception:
                pass
        remove_pid(PID_FILE)

    def start(self):
        bp = read_pid(PID_FILE)
        if bp and pid_alive(bp):
            info("bundle already running", component="bundle")
            return
        write_pid(PID_FILE, os.getpid())
        try:
            os.setsid()
        except Exception:
            pass

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
            self.start_hw_rank0(); started.append("hw0:9102")
            self.start_hw_rank1(); started.append("hw1:9102(ssh)")

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
