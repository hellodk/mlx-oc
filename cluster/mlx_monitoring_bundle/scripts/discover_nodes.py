#!/usr/bin/env python3
"""
discover_nodes.py
=================
Auto-generate config/nodes.yml by discovering Macbooks on the network.

Common parameters (same for all nodes) are provided via flags:
  --central-victoria-metrics, --central-otel, --central-opik
  --ssh-user, --ssh-key

Per-node parameters are fetched via SSH or local inspection:
  - id: hostname
  - serial: `system_profiler SPHardwareDataType` (macOS)
  - ip: local interface IP
  - model: optional pinned model id
  - model_path: optional local model directory

Every Mac is a standalone node; there are no rank0/rank1 roles.

Usage:
  # Discover the local machine
  python3 scripts/discover_nodes.py --local \
      --central-victoria-metrics <VM_HOST:PORT> \
      --central-otel <OTEL_HOST:PORT> \
      --central-opik <OPIK_HOST:PORT> \
      --model-path <MODEL_DIR> \
      > edge/config/nodes.yml

  # Discover a remote Macbook via SSH
  python3 scripts/discover_nodes.py --host <REMOTE_HOST> --ssh-user <USER> ...

Output is YAML written to stdout. Review before deploying.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path


def run_local(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception as exc:
        print(f"run_local failed: {exc!r}", file=sys.stderr)
        return ""


def run_ssh(host: str, user: str, key: str | None, cmd: str,
            timeout: int = 10) -> str:
    ssh_cmd = ["ssh", "-o", "ConnectTimeout=5",
               "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null"]
    if key:
        ssh_cmd += ["-i", key]
    ssh_cmd += [f"{user}@{host}", cmd]
    try:
        return subprocess.run(ssh_cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception as exc:
        print(f"run_ssh to {host} failed: {exc!r}", file=sys.stderr)
        return ""


def get_serial_ssh(host, user, key) -> str:
    out = run_ssh(host, user, key,
                  "system_profiler SPHardwareDataType 2>/dev/null | "
                  "awk '/Serial Number/{print $4; exit}'")
    return out or "unknown"


def get_serial_local() -> str:
    out = run_local(["system_profiler", "SPHardwareDataType"])
    m = re.search(r"Serial Number\s*:\s*(\S+)", out)
    return m.group(1) if m else "unknown"


def get_ip_ssh(host, user, key) -> str:
    out = run_ssh(host, user, key,
                  r"ifconfig | awk '/inet 192\.168\./{print $2; exit}'")
    return out or host


def get_ip_local() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as exc:
        print(f"get_ip_local failed: {exc!r}", file=sys.stderr)
        return "127.0.0.1"


def get_hostname_ssh(host, user, key) -> str:
    out = run_ssh(host, user, key, "hostname -s 2>/dev/null || hostname")
    return out or host


def get_hostname_local() -> str:
    return socket.gethostname()


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-generate nodes.yml")
    ap.add_argument("--local", action="store_true",
                    help="discover the local machine")
    ap.add_argument("--host", help="remote host to discover via SSH")
    ap.add_argument("--ssh-user", help="SSH user for remote discovery")
    ap.add_argument("--ssh-key", help="SSH private key for remote discovery")
    ap.add_argument("--model", help="pinned model id (optional, auto-discovered if unset)")
    ap.add_argument("--model-path", help="local model directory (optional)")
    ap.add_argument("--enabled", action="store_true", default=True,
                    help="mark node as enabled (default: true)")
    ap.add_argument("--central-victoria-metrics", required=True,
                    help="VictoriaMetrics host:port (required, no default)")
    ap.add_argument("--central-otel", required=True,
                    help="OTel collector host:port (required, no default)")
    ap.add_argument("--central-opik", required=True,
                    help="Opik host:port (required, no default)")
    args = ap.parse_args()

    if args.local:
        host = args.host or get_ip_local()
        user = args.ssh_user or os.environ.get("USER", "dk")
        key = args.ssh_key
        serial = get_serial_local()
        ip = get_ip_local()
        node_id = get_hostname_local()
    else:
        if not args.host:
            print("--host or --local is required", file=sys.stderr)
            return 2
        if not args.ssh_user:
            print("--ssh-user is required for remote discovery", file=sys.stderr)
            return 2
        host = args.host
        user = args.ssh_user
        key = args.ssh_key
        serial = get_serial_ssh(host, user, key)
        ip = get_ip_ssh(host, user, key)
        node_id = get_hostname_ssh(host, user, key)

    node = {
        "id": node_id,
        "serial": serial,
        "ip": ip,
        "enabled": args.enabled,
    }
    if args.model:
        node["model"] = args.model
    if args.model_path:
        node["model_path"] = str(Path(args.model_path).expanduser())

    doc = {
        "central": {
            "victoria_metrics": args.central_victoria_metrics,
            "otel": args.central_otel,
            "opik": args.central_opik,
        },
        "macbooks": [node],
    }

    import yaml
    print(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
