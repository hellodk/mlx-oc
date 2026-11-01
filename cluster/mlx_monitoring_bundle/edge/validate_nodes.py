#!/usr/bin/env python3
"""
validate_nodes.py
=================
SSH connectivity validation for config/nodes.yml entries.

Short-lived tool: prompts for SSH password via getpass if no ssh_key is
configured. The password is held in memory only for the lifetime of this
process and is forgotten on exit. No credentials are stored on disk.

Reads config/nodes.yml and, for each enabled macbook entry, attempts an SSH
connectivity check (a no-op `true` command over SSH). Reports which nodes are
reachable and which are not. Exit code 0 = all reachable, 1 = one or more
unreachable.
"""

import argparse
import getpass
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

BUNDLE_DIR = Path(__file__).resolve().parent
DEFAULT_NODES_FILE = BUNDLE_DIR / "config" / "nodes.yml"


def load_nodes(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf8")) or {}
    except FileNotFoundError:
        print(f"nodes file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"failed to parse {path}: {exc!r}", file=sys.stderr)
        sys.exit(2)


def check_node(node: dict, timeout: int, password_cache: dict) -> tuple[bool, str]:
    """Return (reachable, message) for one macbook node entry."""
    node_id = node.get("id", "unknown")
    ip = node.get("ip")
    ssh_user = node.get("ssh_user")
    ssh_key = node.get("ssh_key")
    node_timeout = int(node.get("ssh_connect_timeout", timeout))

    if not ip:
        return False, f"{node_id}: no ip configured"
    if not ssh_user:
        return False, f"{node_id}: no ssh_user configured"

    key_arg = []
    password = None
    if ssh_key:
        key_arg = ["-i", ssh_key]
    else:
        cache_key = f"{ssh_user}@{ip}"
        if cache_key not in password_cache:
            password_cache[cache_key] = getpass.getpass(
                f"SSH password for {ssh_user}@{ip}: "
            )
        password = password_cache[cache_key]

    cmd = [
        "ssh",
        "-o", "BatchMode=no",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"ConnectTimeout={node_timeout}",
        *key_arg,
        f"{ssh_user}@{ip}",
        "true",
    ]

    env = dict(os.environ)
    if password is not None:
        env["SSH_ASKPASS"] = "/dev/null"
        # Use sshpass-style via expect is complex; instead pass password to
        # ssh via a helper. paramiko would be cleaner but we avoid extra deps.
        # Here we rely on ssh's built-in password prompt; since BatchMode=no,
        # ssh will prompt on the terminal. For non-interactive validation,
        # ssh_key is strongly recommended.

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=node_timeout + 3,
            env=env,
        )
        elapsed = time.monotonic() - t0
        if proc.returncode == 0:
            return True, f"{node_id} ({ip}) reachable in {elapsed:.1f}s"
        return False, (
            f"{node_id} ({ip}): SSH exited {proc.returncode}; "
            f"stderr={(proc.stderr or '').strip()[:120]}"
        )
    except subprocess.TimeoutExpired:
        return False, f"{node_id} ({ip}): SSH connect timeout after {node_timeout}s"
    except FileNotFoundError:
        return False, f"{node_id} ({ip}): ssh binary not found on PATH"
    except Exception as exc:
        return False, f"{node_id} ({ip}): {exc!r}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Validate SSH connectivity for nodes.yml entries"
    )
    ap.add_argument("--nodes", default="config/nodes.yml",
                    help="path to nodes.yml (default: config/nodes.yml)")
    ap.add_argument("--timeout", type=int, default=5,
                    help="default SSH connect timeout in seconds (default: 5)")
    args = ap.parse_args()

    nodes_path = Path(args.nodes)
    if not nodes_path.is_absolute():
        nodes_path = BUNDLE_DIR / nodes_path

    data = load_nodes(nodes_path)
    macbooks = data.get("macbooks", []) or []

    if not macbooks:
        print("no macbook entries found in nodes.yml", file=sys.stderr)
        return 1

    results = []
    unreachable = 0
    password_cache: dict[str, str] = {}

    for node in macbooks:
        if not node.get("enabled", True):
            print(f"SKIP  {node.get('id','unknown')} (disabled)")
            continue
        ok, msg = check_node(node, args.timeout, password_cache)
        results.append((node, ok, msg))
        if ok:
            print(f"OK    {msg}")
        else:
            unreachable += 1
            print(f"FAIL  {msg}")

    total = len(results)
    print()
    print(f"validated {total} node(s): {total - unreachable} reachable, {unreachable} unreachable")

    # Clear password cache from memory
    for k in list(password_cache.keys()):
        password_cache[k] = ""
    password_cache.clear()

    return 1 if unreachable else 0


if __name__ == "__main__":
    import os
    sys.exit(main())
