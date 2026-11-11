#!/usr/bin/env python3
"""
validate_nodes.py
=================
Validate config/nodes.yml and local edge prerequisites.

The bundle is push-based and every Mac is a standalone node, so no SSH
connectivity check is needed. This tool verifies:

  - central VictoriaMetrics and OTel endpoints are configured
  - at least one enabled macbook entry exists
  - a node can be selected (MLX_MACBOOK_ID or exactly one enabled entry)
  - model_path (when set) contains a readable config.json
  - VENV_PYTHON and MLX_VENV_PYTHON point at executables
  - required Python packages import in the monitoring venv
  - optionally, that the exporter ports are free

Exit code 0 = valid, 1 = one or more errors.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path

import yaml

BUNDLE_DIR = Path(__file__).resolve().parent
DEFAULT_NODES_FILE = BUNDLE_DIR / "config" / "nodes.yml"
DEFAULT_ENV_FILE = BUNDLE_DIR / "config" / "edge.env"

REQUIRED_MODULES = ("yaml", "prometheus_client", "requests")


def load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf8")) or {}
    except FileNotFoundError:
        print(f"nodes file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"failed to parse {path}: {exc!r}", file=sys.stderr)
        sys.exit(2)


def load_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def select_node(data: dict, explicit: str | None) -> dict:
    macbooks = [
        nb for nb in (data.get("macbooks") or [])
        if isinstance(nb, dict) and nb.get("enabled", True)
    ]
    if explicit:
        for nb in macbooks:
            if nb.get("id") == explicit:
                return nb
        return {}
    if len(macbooks) == 1:
        return macbooks[0]
    return {}


def module_available(python: str, module: str) -> bool:
    try:
        import subprocess

        proc = subprocess.run(
            [python, "-c", f"import {module}"],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode == 0
    except Exception:
        return False


def port_free(port: int) -> bool:
    # Do not set SO_REUSEADDR: on macOS it lets a second bind to 127.0.0.1
    # succeed even while another process listens on 0.0.0.0, producing a
    # false "free" result.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Validate nodes.yml and edge prerequisites")
    ap.add_argument("--nodes", default=str(DEFAULT_NODES_FILE),
                    help="path to nodes.yml (default: config/nodes.yml)")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE),
                    help="path to edge.env (default: config/edge.env)")
    ap.add_argument("--node", default=os.environ.get("MLX_MACBOOK_ID"),
                    help="node id to validate (default: auto-select single enabled node)")
    ap.add_argument("--check-ports", action="store_true",
                    help="warn when exporter ports are already in use")
    args = ap.parse_args()

    nodes_path = Path(args.nodes)
    if not nodes_path.is_absolute():
        nodes_path = BUNDLE_DIR / nodes_path
    data = load_yaml(nodes_path)
    env = load_env_file(Path(args.env_file))

    errors: list[str] = []
    warnings: list[str] = []

    central = data.get("central") or {}
    for key, env_key in (
        ("victoria_metrics", "CENTRAL_VICTORIA_METRICS"),
        ("otel", "CENTRAL_OTEL"),
    ):
        if not (central.get(key) or env.get(env_key)):
            errors.append(f"central.{key} is not configured (or set {env_key})")
    if not (central.get("opik") or env.get("CENTRAL_OPIK")):
        warnings.append("central.opik is not configured; Opik tracing will be skipped")

    macbooks = [
        nb for nb in (data.get("macbooks") or [])
        if isinstance(nb, dict) and nb.get("enabled", True)
    ]
    if not macbooks:
        errors.append("no enabled macbook entries in nodes.yml")
    elif not args.node and len(macbooks) > 1:
        errors.append(
            "multiple enabled macbooks: pass --node or set MLX_MACBOOK_ID"
        )

    node = select_node(data, args.node)
    if macbooks and not node:
        errors.append(
            f"node '{args.node}' not found among enabled macbooks"
            if args.node else "could not select a node"
        )
    elif node:
        print(f"node     : {node.get('id', 'unknown')} "
              f"({node.get('ip', 'no ip')})")
        model = env.get("MLX_MODEL") or node.get("model")
        model_path = env.get("MLX_MODEL_PATH") or node.get("model_path")
        if model:
            print(f"model    : {model}")
        elif node.get("model_path"):
            print("model    : <discovered from model_path>")
        else:
            warnings.append("no model or model_path set; model gauges will be skipped")
        if model_path:
            cfg = Path(model_path).expanduser() / "config.json"
            if cfg.is_file():
                print(f"config   : {cfg}")
            else:
                errors.append(f"model_path does not contain config.json: {cfg}")
        elif model and model.startswith(("/", "~", "./", "../")):
            cfg = Path(model).expanduser() / "config.json"
            if cfg.is_file():
                print(f"config   : {cfg}")
            else:
                errors.append(f"model path does not contain config.json: {cfg}")

    venv_python = env.get("VENV_PYTHON", "python3")
    mlx_python = env.get("MLX_VENV_PYTHON", venv_python)
    for label, executable in (
        ("VENV_PYTHON", venv_python),
        ("MLX_VENV_PYTHON", mlx_python),
    ):
        path = Path(executable).expanduser()
        if not (path.is_file() and os.access(path, os.X_OK)):
            errors.append(f"{label} is not executable: {path}")
        else:
            print(f"{label:16}: {path}")

    if Path(venv_python).is_file():
        for module in REQUIRED_MODULES:
            if not module_available(venv_python, module):
                errors.append(f"{module} is not importable in {venv_python}")

    if args.check_ports:
        for port in (8080, 9102, 9104, 9105, 9106):
            if not port_free(port):
                warnings.append(f"port {port} is already in use")

    for warning in warnings:
        print(f"WARN  {warning}")
    for error in errors:
        print(f"FAIL  {error}")

    if errors:
        print(f"\n{len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"\nOK ({len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
