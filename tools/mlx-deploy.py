#!/usr/bin/env python3
"""
mlx-deploy.py — interactive deployment wizard for the distributed MLX ring
===========================================================================
Walks you through topology, model, versions and tuning, scans every node for
prerequisites (and suggests fixes), then generates the Ansible inventory +
group_vars and (optionally) runs the playbook that creates the venvs, syncs
the repo, stages the model and starts the ring.

Can also run fully non-interactively from a config file, so the same cluster
can be re-deployed or expanded (see infra/ansible/mlx-deploy.example.json).

Usage:
  python3 tools/mlx-deploy.py                  # interactive wizard
  python3 tools/mlx-deploy.py --config cfg.json   # non-interactive (from file)
  python3 tools/mlx-deploy.py --check             # collect + prereq scan only
  python3 tools/mlx-deploy.py --apply             # collect + run the playbook
  python3 tools/mlx-deploy.py --no-run            # collect + write config, stop
  python3 tools/mlx-deploy.py --save-config cfg.json   # dump answers to a file
  python3 tools/mlx-deploy.py --list               # show the active config

Needs a plain python3 (no third-party deps — YAML output is emitted directly).
Needs ansible for --apply/--check (the scan tells you how to install it).

Verified versions this repo pins (blogs 13/14/23): Python 3.12 for the
distributed server venv (macOS TCC blocks a py3.14 binary spawned over SSH),
mlx 0.32.0, mlx-lm 0.31.3, mlx-metal 0.32.0.
"""

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

# ---------------------------------------------------------------- defaults ---

DEFAULTS = {
    "nodes": [
        {"name": "rank0", "ssh": "127.0.0.1", "ring_ip": "192.168.2.2", "local": True},
        {"name": "rank1", "ssh": "192.168.2.1", "ring_ip": "192.168.2.1", "local": False},
    ],
    "ring_backend": "ring",
    "interconnect": "ethernet",
    "ring_subnet": "192.168.2",
    "server_bind_ip": "127.0.0.1",
    "model": "mlx-community/Qwen3.5-4B-MLX-4bit",
    "model_name": "Qwen3.5-4B-MLX-4bit",
    "model_dir": "/opt/mlx-models",
    "download_model": False,
    "model_sync_from_rank0": False,
    "min_ram_mb": 8192,
    "min_disk_gb": 15,
    "python_server": "python3.12",
    "python_proxy": "python3.12",
    "venv_server": "~/venvs/mlx",
    "repo_dir": "~/mlx-oc",
    "sync_repo": True,
    "install_method": "online",
    "wheelhouse": "",
    "install_opik": False,
    "ports": {"server": 8081, "proxy": 8080, "supervisor": 9105, "hw": 9102, "kv": 9104, "logtailer": 9106},
    "default_temp": 0.0,
    "logprobs": 3,
    "logprobs_stream_sample": 0.05,
    "low_confidence": 0.5,
    "api_key": "",
    "max_prompt_tokens": 16000,
    "max_tokens_cap": 4096,
    "max_body_bytes": 4194304,
    "rate_limit": 0,
    "otlp_endpoint": "",
    "opik_otlp_endpoint": "",
    "opik_base": "",
    "judge_url": "http://127.0.0.1:8080/v1",
    "pins": {
        "mlx": "0.32.0",
        "mlx_lm": "0.31.3",
        "mlx_metal": "0.32.0",
        "huggingface_hub": "1.26.0",
        "prometheus_client": "0.26.0",
        "opentelemetry": "1.44.0",
        "otel_contrib": "0.65b0",
    },
}

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSIBLE_DIR = os.path.join(REPO, "infra", "ansible")
INVENTORY_DIR = os.path.join(ANSIBLE_DIR, "inventories", "generated")
INVENTORY = os.path.join(INVENTORY_DIR, "hosts.yml")
GROUP_VARS = os.path.join(ANSIBLE_DIR, "group_vars", "all", "mlx-cluster.yml")

OK, BAD, WARN = "  [OK]  ", "  [!!]  ", "  [~~]  "
GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def ok(msg):
    print(f"{GREEN}{OK}{RESET}{msg}")


def bad(msg):
    print(f"{RED}{BAD}{RESET}{msg}")


def warn(msg):
    print(f"{YELLOW}{WARN}{RESET}{msg}")


# ---------------------------------------------------------------- tiny yaml ---

def _yaml_scalar(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v)
    low = s.lower()
    if s == "" or low in ("true", "false", "null", "yes", "no") or (s and s[0] in "-.0123456789"):
        return f'"{s}"'
    return s


def yaml_dump(obj, indent=0):
    pad = " " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(yaml_dump(v, indent + 2))
            elif isinstance(v, dict) and not v:
                lines.append(f"{pad}{k}: {{}}")
            elif isinstance(v, list) and not v:
                lines.append(f"{pad}{k}: []")
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(yaml_dump(item, indent + 2))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return "\n".join(lines)


# -------------------------------------------------------------- interactive ----

def ask(prompt, default):
    suffix = f" [{default}] " if default not in ("", None) else " "
    try:
        raw = input(f"{prompt}{suffix}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)
    return raw if raw else (default if default is not None else "")


def ask_bool(prompt, default):
    d = "Y" if default else "N"
    a = ask(prompt + " [y/N]", "N" if not default else "Y").lower()
    return a.startswith("y")


def ask_int(prompt, default, lo, hi):
    while True:
        raw = ask(prompt, str(default))
        try:
            n = int(raw)
            if lo <= n <= hi:
                return n
        except ValueError:
            pass
        print(f"  please enter an integer between {lo} and {hi}")


def ask_ip(prompt, default):
    while True:
        raw = ask(prompt, default)
        parts = raw.split(".")
        if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            return raw
        print("  please enter a valid IPv4 address")


def collect_interactive():
    print()
    print("=" * 68)
    print("MLX distributed inference cluster — setup wizard")
    print("=" * 68)
    print("You can press Enter to accept every default and re-deploy the")
    print("current two-node ring; or configure a brand new / bigger one.")
    print("For many nodes (>= 20) a JSON config file is faster: write one via")
    print("--save-config on a small ring, then edit the nodes list and re-run")
    print("with --config. Peer defaults below auto-increment the ring IP.")
    print()
    n = ask_int("How many nodes (rank0 + peers)?", 2, 1, 64)
    nodes = []
    for i in range(n):
        name = f"rank{i}"
        if i == 0:
            print(f"\n-- {name} (the SERVING node — this machine) --")
            ring_ip = ask_ip(f"  {name} ring IP (interconnect)", "192.168.2.2")
            subnet = ring_ip.rsplit(".", 1)[0]
            nodes.append({"name": name, "ssh": "127.0.0.1", "ring_ip": ring_ip, "local": True})
        else:
            print(f"\n-- {name} (ring peer) --")
            def_ip = f"{subnet}.{1 if i == 1 else i + 1}"
            ssh = ask(f"  ssh target (user@host or host) ", f"dk@{def_ip}")
            ring_ip = ask_ip(f"  {name} ring IP", def_ip)
            local = False
            nodes.append({"name": name, "ssh": ssh, "ring_ip": ring_ip, "local": local})
    interconnect = ask("Ring interconnect [ethernet/thunderbolt]", "ethernet")
    cfg = dict(DEFAULTS)
    cfg["nodes"] = nodes
    cfg["interconnect"] = interconnect
    cfg["ring_subnet"] = subnet
    cfg["server_bind_ip"] = ask_ip("rank0 server bind IP (httpd + proxy addr)", "127.0.0.1")
    print()
    model = ask("Model (HF id like mlx-community/..., or an absolute path)", DEFAULTS["model"])
    cfg["model"] = model
    if model.startswith("/"):
        cfg["model_name"] = os.path.basename(model.rstrip("/"))
        cfg["model_dir"] = os.path.dirname(model)
        cfg["download_model"] = False
    else:
        cfg["model_name"] = model.split("/")[-1]
        cfg["model_dir"] = ask("Model directory on EACH node", "/opt/mlx-models")
        cfg["download_model"] = ask_bool("Download weights onto every node now?", False)
    print()
    cfg["python_server"] = ask("Server venv python (must be 3.12 on the remote shard)", DEFAULTS["python_server"])
    cfg["python_proxy"] = ask("Proxy/telemetry venv python", DEFAULTS["python_proxy"])
    method = ask("pip install [online/wheelhouse]", "online").lower()
    cfg["install_method"] = method
    if method == "wheelhouse":
        cfg["wheelhouse"] = ask("Wheelhouse directory (absolute path, on every node)", "")
    print()
    if not ask_bool("Use the verified pins (mlx 0.32.0, mlx-lm 0.31.3, mlx-metal 0.32.0, py3.12)?", True):
        for k in ("mlx", "mlx_lm", "mlx_metal"):
            cfg["pins"][k] = ask(f"  pin {k}", cfg["pins"][k])
    print()
    cfg["repo_dir"] = ask("Repo path on EVERY node (same absolute path)", os.path.abspath(REPO))
    cfg["sync_repo"] = ask_bool("Sync this repo out to the peers before deploy?", True)
    cfg["install_opik"] = ask_bool("Install Opik tracing extras (proxy venv)?", False)
    return cfg


# ------------------------------------------------------------ config load -----

def load_config(path):
    with open(path) as fh:
        cfg = json.load(fh)
    base = json.loads(json.dumps(DEFAULTS))
    base.update(cfg)
    base["nodes"] = cfg.get("nodes", DEFAULTS["nodes"])
    if isinstance(base.get("pins"), dict):
        base["pins"] = {**DEFAULTS["pins"], **base["pins"]}
    if isinstance(base.get("ports"), dict):
        base["ports"] = {**DEFAULTS["ports"], **base["ports"]}
    return base


def save_config(path, cfg):
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------- local prereq scan --

def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        return type("R", (), {"returncode": -1, "stdout": "", "stderr": str(e)})()


def scan_local(cfg):
    print("\nPrereq scan (rank0 = this machine):")
    arch_ok = platform.machine() == "arm64"
    (ok if arch_ok else bad)(f"Apple Silicon (arm64): {platform.machine()}")
    if not arch_ok:
        bad("MLX needs arm64; there is no x86 path — aborting.")
        return False

    os_ok = platform.system() == "Darwin"
    (ok if os_ok else bad)(f"macOS: {platform.system()}")
    v = run(["sw_vers", "-productVersion"])
    if v.returncode == 0:
        print(f"        macOS version: {v.stdout.strip()}")

    py = shutil.which("python3.12")
    if py:
        ok(f"python3.12: {py}")
    else:
        bad("python3.12 not found — run:  brew install python@3.12  (or the python.org 3.12 .pkg)")

    ap = shutil.which("ansible-playbook")
    if ap:
        ok(f"ansible-playbook: {ap}")
    else:
        bad("ansible-playbook not found — run:  brew install ansible  (or: pipx install ansible-core)")

    mem = run(["sysctl", "-n", "hw.memsize"])
    ram_gb = int(mem.stdout.strip()) // (1024 ** 3) if mem.returncode == 0 else 0
    (ok if ram_gb >= cfg["min_ram_mb"] // 1024 else bad)(f"RAM: {ram_gb} GiB (need >= {cfg['min_ram_mb'] // 1024} GiB)")

    repo_dir = os.path.expanduser(cfg["repo_dir"])
    try:
        du = shutil.disk_usage(repo_dir)
        free_gb = du.free // (1024 ** 3)
        (ok if free_gb >= cfg["min_disk_gb"] else bad)(f"disk free on {repo_dir}: {free_gb} GiB (need >= {cfg['min_disk_gb']} GiB)")
    except FileNotFoundError:
        bad(f"repo_dir {repo_dir} does not exist on this machine")

    if not cfg.get("download_model"):
        staged = os.path.join(cfg["model_dir"], cfg["model_name"], "config.json")
        if os.path.exists(staged):
            ok(f"model staged: {staged}")
        else:
            warn(f"model not staged on rank0: {staged} (the playbook will re-check on every node)")

    all_ok = arch_ok and bool(py) and bool(ap)
    for node in cfg["nodes"]:
        if node.get("local"):
            continue
        p = run(["ping", "-c1", "-t2", node["ring_ip"]])
        (ok if p.returncode == 0 else bad)(f"ping {node['ring_ip']} ({node['name']})")
        s = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", node["ssh"], "true"])
        if s.returncode == 0:
            ok(f"passwordless ssh {node['ssh']}")
        else:
            bad(f"passwordless ssh {node['ssh']} FAILED — run:  ssh-copy-id {node['ssh']}")
            all_ok = False
    return all_ok


# ------------------------------------------------------------ config writing --

def write_inventory(cfg):
    os.makedirs(INVENTORY_DIR, exist_ok=True)
    lines = [
        "---",
        "# generated by tools/mlx-deploy.py — re-run the wizard to regenerate",
        "all:",
        "  children:",
        "    mlx_ring:",
        "      hosts:",
    ]
    for node in cfg["nodes"]:
        lines.append(f"        {node['name']}:")
        if node.get("local"):
            lines.append("          ansible_connection: local")
            lines.append("          ansible_host: 127.0.0.1")
        else:
            host = node["ssh"]
            if "@" in host:
                user, h = host.split("@", 1)
                lines.append(f"          ansible_user: {user}")
                host = h
            lines.append(f"          ansible_host: {host}")
    with open(INVENTORY, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return INVENTORY


def write_group_vars(cfg):
    os.makedirs(os.path.dirname(GROUP_VARS), exist_ok=True)
    body = yaml_dump(cfg, indent=2)
    with open(GROUP_VARS, "w") as fh:
        fh.write("---\n")
        fh.write("# =============================================================================\n")
        fh.write("# MLX cluster configuration — generated by tools/mlx-deploy.py\n")
        fh.write("# =============================================================================\n")
        fh.write("# Do not hand-edit while using the wizard; re-run it instead. See\n")
        fh.write("# infra/ansible/group_vars/all/README for the hand-edited workflow.\n")
        fh.write("mlx_cluster:\n")
        fh.write(body + "\n")
        fh.write("mlx_rank0_host: \"{{ mlx_cluster.nodes[0].name }}\"\n")
    return GROUP_VARS


def summary(cfg):
    print()
    print("=" * 68)
    print("Cluster plan")
    print("=" * 68)
    for node in cfg["nodes"]:
        local = " (serving node)" if node.get("local") else ""
        print(f"  {node['name']:<6} ssh={node['ssh']:<22} ring={node['ring_ip']}{local}")
    print(f"  interconnect: {cfg['interconnect']}  subnet: {cfg['ring_subnet']}")
    print(f"  model: {cfg['model']}  ->  {cfg['model_dir']}/{cfg['model_name']}")
    print(f"  server bind: {cfg['server_bind_ip']}:{cfg['ports']['server']}  proxy: :{cfg['ports']['proxy']}")
    print(f"  python: server={cfg['python_server']} proxy={cfg['python_proxy']}")
    print(f"  pins: mlx {cfg['pins']['mlx']} / mlx-lm {cfg['pins']['mlx_lm']} / mlx-metal {cfg['pins']['mlx_metal']}")
    print(f"  install: {cfg['install_method']}{('  wheelhouse=' + cfg['wheelhouse']) if cfg['wheelhouse'] else ''}")
    print(f"  model: download={cfg['download_model']}  sync-to-peers={cfg['model_sync_from_rank0']}")
    print(f"  repo: {cfg['repo_dir']}  (sync to peers: {cfg['sync_repo']})")


def run_playbook(name):
    cmd = ["ansible-playbook", "-i", "inventories/generated/hosts.yml", name]
    print("\n$ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=ANSIBLE_DIR)


# ------------------------------------------------------------------- main -----

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="non-interactive: load answers from a JSON config file")
    ap.add_argument("--save-config", help="write the collected answers to a JSON config file and stop")
    ap.add_argument("--check", action="store_true", help="collect + prereq scan + write config, then run the read-only check playbook")
    ap.add_argument("--apply", action="store_true", help="collect + prereq scan + write config, then run the full deploy playbook")
    ap.add_argument("--no-run", action="store_true", help="collect + prereq scan + write config, but do not run ansible")
    ap.add_argument("--list", action="store_true", help="print the active config from group_vars and exit")
    args = ap.parse_args()

    if args.list:
        with open(GROUP_VARS) as fh:
            print(fh.read())
        return

    if args.config:
        cfg = load_config(args.config)
        summary(cfg)
    else:
        cfg = collect_interactive()
        summary(cfg)

    if args.save_config:
        save_config(args.save_config, cfg)
        print(f"\nconfig written to {args.save_config}")
        return

    scan_ok = scan_local(cfg)

    inv = write_inventory(cfg)
    gv = write_group_vars(cfg)
    print(f"\nwrote {inv}")
    print(f"wrote {gv}")

    if args.no_run:
        print("\nnot running ansible. To deploy later:")
        print("  cd infra/ansible")
        print("  ansible-playbook -i inventories/generated/hosts.yml check.yml     # read-only report")
        print("  ansible-playbook -i inventories/generated/hosts.yml cluster.yml   # prereqs+setup+start")
        return

    if not shutil.which("ansible-playbook"):
        bad("ansible-playbook is not installed — nothing was run. Install it with:  brew install ansible")
        return

    if args.check:
        run_playbook("check.yml")
        return

    if args.apply:
        if not scan_ok:
            warn("local prereq scan had failures — running the read-only report instead of deploying.")
            run_playbook("check.yml")
        else:
            run_playbook("cluster.yml")
        return

    # no action flag: in interactive mode ask; in config mode just stop.
    if args.config:
        print("\nconfig applied to disk only (re-run with --apply to deploy).")
        return

    print()
    if ask_bool("Run the full deployment playbook now (venvs + model + ring)?", True):
        run_playbook("check.yml")
        run_playbook("cluster.yml")
    else:
        print("\nskipped. Re-run with --apply when ready.")


if __name__ == "__main__":
    main()
