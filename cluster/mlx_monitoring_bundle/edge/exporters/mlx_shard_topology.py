#!/usr/bin/env python3
"""
mlx_shard_topology.py — Passive distributed model sharding & placement inspector.

Determines which model shard (layer range, tensor slice, safetensors files,
and weight byte footprint) runs on which node and rank using read-only
process inspection, hostfile / nodes.yml metadata, and HuggingFace
safetensors index files. Never starts, stops, or alters any server process.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mlx_logging import debug
from mlx_model_info import ModelInfo

_LAYER_KEY_RE = re.compile(r"(?:^|\.)(?:layers|h|blocks)\.(\d+)\.")
_SHARD_NUM_RE = re.compile(r"-(\d+)-of-\d+\.safetensors$")


@dataclass(frozen=True)
class PeerConnectResult:
    """TCP connection establishment probe result (transport layer reachability)."""

    up: bool
    connect_seconds: float
    reason: str  # "connected", "refused", "timeout", "unreachable", "disabled"

    def __bool__(self) -> bool:
        return self.up


@dataclass(frozen=True)
class PeerAppResult:
    """Application-level request/response RTT probe result (service layer reachability)."""

    up: bool
    app_rtt_seconds: float
    status_code: int
    reason: str  # "ok", "refused", "timeout", "error", "disabled"

    def __bool__(self) -> bool:
        return self.up


@dataclass(frozen=True)
class PeerProbeResult:
    """Reachability status, TCP connect duration, and application RTT for a single peer rank."""

    up: bool
    connect_seconds: float
    reason: str  # "connected", "refused", "timeout", "unreachable", "disabled"
    app_rtt_seconds: float = 0.0
    app_up: bool = False

    def __bool__(self) -> bool:
        return self.up


@dataclass(frozen=True)
class ShardPlacement:
    """Resolved shard assignment and cluster placement for the local node."""

    model_id: str
    rank: int
    world_size: int
    mode: str
    backend: str
    layer_start: int
    layer_end: int
    layers_assigned: int
    layer_range: str
    shard_files: str
    shard_files_present: int
    shard_files_expected: int
    expected_weight_bytes: int
    dim_hidden: int
    dim_kv_heads: int
    dim_intermediate: int
    transport_ip: str
    iface: str
    peers: tuple[tuple[int, str], ...]
    cluster_nodes: tuple[dict[str, Any], ...]
    tp_rank: int = 0
    pp_rank: int = 0
    tp_size: int = 1
    pp_size: int = 1
    kv_heads_exact: bool = True
    kv_source: str = "exact"


def inspect_safetensors_index(model_dir: Path | None) -> dict[str, Any]:
    """Inspect safetensors shard files and layer-to-file mapping in model_dir."""
    result: dict[str, Any] = {
        "total_bytes": 0,
        "expected_files": [],
        "present_files": [],
        "file_sizes": {},
        "layer_to_files": {},
        "embed_files": set(),
        "head_files": set(),
    }
    if model_dir is None or not model_dir.is_dir():
        return result

    idx_path = model_dir / "model.safetensors.index.json"
    if idx_path.is_file():
        try:
            data = json.loads(idx_path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError) as exc:
            debug(f"failed reading {idx_path}: {exc!r}", component="shard")
            data = None
        if isinstance(data, dict):
            wmap = data.get("weight_map")
            if isinstance(wmap, dict):
                files_seen: set[str] = set()
                for tensor_name, fname in wmap.items():
                    if not isinstance(fname, str):
                        continue
                    files_seen.add(fname)
                    m = _LAYER_KEY_RE.search(str(tensor_name))
                    if m:
                        lidx = int(m.group(1))
                        result["layer_to_files"].setdefault(lidx, set()).add(fname)
                    elif "embed" in str(tensor_name).lower():
                        result["embed_files"].add(fname)
                    elif (
                        "lm_head" in str(tensor_name).lower()
                        or str(tensor_name).endswith("norm.weight")
                    ):
                        result["head_files"].add(fname)
                result["expected_files"] = sorted(files_seen)
            meta = data.get("metadata")
            if isinstance(meta, dict) and isinstance(meta.get("total_size"), (int, float)):
                result["total_bytes"] = int(meta["total_size"])

    if not result["expected_files"]:
        try:
            st_files = sorted(p.name for p in model_dir.glob("*.safetensors") if p.is_file())
            result["expected_files"] = st_files
        except OSError:
            pass

    present: list[str] = []
    sizes: dict[str, int] = {}
    disk_total = 0
    for fname in result["expected_files"]:
        fp = model_dir / fname
        try:
            if fp.is_file():
                sz = fp.stat().st_size
                present.append(fname)
                sizes[fname] = sz
                disk_total += sz
        except OSError:
            continue
    result["present_files"] = present
    result["file_sizes"] = sizes
    if result["total_bytes"] <= 0:
        result["total_bytes"] = disk_total
    return result


def parse_hostfile(hostfile_path: str | Path | None) -> tuple[str, list[dict[str, Any]]]:
    """Parse JSON or plain text hostfile into (backend, host entries)."""
    if not hostfile_path:
        return "", []
    p = Path(str(hostfile_path)).expanduser()
    if not p.is_file():
        return "", []
    try:
        raw = p.read_text(encoding="utf8").strip()
    except OSError as exc:
        debug(f"cannot read hostfile {p}: {exc!r}", component="shard")
        return "", []
    if not raw:
        return "", []

    if raw.startswith("{") or raw.startswith("["):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            debug(f"invalid JSON hostfile {p}: {exc!r}", component="shard")
            return "", []
        backend = str(obj.get("backend", "")) if isinstance(obj, dict) else ""
        hosts_raw = obj.get("hosts", []) if isinstance(obj, dict) else obj
        hosts: list[dict[str, Any]] = []
        for idx, entry in enumerate(hosts_raw if isinstance(hosts_raw, list) else []):
            if isinstance(entry, dict):
                ips = [str(ip) for ip in (entry.get("ips") or []) if ip]
                ssh = str(entry.get("ssh") or "")
                hosts.append({"rank": idx, "ssh": ssh, "ips": ips})
            elif isinstance(entry, str) and entry.strip():
                hosts.append({"rank": idx, "ssh": entry.strip(), "ips": [entry.strip()]})
        return backend, hosts

    hosts = []
    for line in raw.splitlines():
        clean = line.split("#", 1)[0].strip()
        if not clean:
            continue
        token = clean.split()[0]
        hosts.append({"rank": len(hosts), "ssh": token, "ips": [token]})
    return "", hosts


def local_ip_iface_map() -> dict[str, str]:
    """Return mapping of local IPv4/IPv6 addresses to OS interface names."""
    mapping: dict[str, str] = {"127.0.0.1": "lo", "::1": "lo"}
    cmd = ["ifconfig"] if sys.platform == "darwin" else ["ip", "-o", "addr", "show"]
    try:
        out = subprocess.check_output(
            cmd, text=True, stderr=subprocess.DEVNULL, timeout=3
        )
    except Exception:
        return mapping

    if sys.platform == "darwin":
        cur_iface = ""
        for line in out.splitlines():
            if line and not line[0].isspace() and ":" in line:
                cur_iface = line.split(":", 1)[0].strip()
            elif cur_iface:
                m = re.match(r"^\s+inet6?\s+([^\s%/]+)", line)
                if m:
                    mapping[m.group(1)] = cur_iface
    else:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
                addr = parts[3].split("/", 1)[0].split("%", 1)[0]
                mapping[addr] = parts[1]
    return mapping


def _extract_flag_value(tokens: list[str], names: tuple[str, ...]) -> str:
    """Return value for `--flag val` or `--flag=val` in tokenized argv."""
    for idx, tok in enumerate(tokens):
        for name in names:
            if tok == name and idx + 1 < len(tokens):
                return tokens[idx + 1]
            if tok.startswith(f"{name}="):
                return tok.split("=", 1)[1]
    return ""


def _read_proc_env(pid: int) -> dict[str, str]:
    """Best-effort read of /proc/<pid>/environ on Linux."""
    env_path = Path(f"/proc/{pid}/environ")
    if not env_path.is_file():
        return {}
    try:
        raw = env_path.read_bytes()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for item in raw.split(b"\x00"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            out[k.decode("utf8", "replace")] = v.decode("utf8", "replace")
    return out


def infer_runtime_sharding(
    process_pattern: str,
    hostfile_override: str = "",
    mode_override: str = "",
    backend_override: str = "",
    rank_override: int | None = None,
    world_override: int | None = None,
) -> dict[str, Any]:
    """Passively inspect server and parent launcher flags for sharding metadata."""
    env_rank = None
    raw_rk = os.environ.get("MLX_RANK") or os.environ.get("RANK")
    if raw_rk and raw_rk.isdigit():
        env_rank = int(raw_rk)

    env_ws = None
    raw_ws = os.environ.get("MLX_WORLD_SIZE") or os.environ.get("WORLD_SIZE")
    if raw_ws and raw_ws.isdigit():
        env_ws = int(raw_ws)

    info: dict[str, Any] = {
        "hostfile": hostfile_override or os.environ.get("MLX_HOSTFILE", ""),
        "mode": mode_override or os.environ.get("MLX_DISTRIBUTED_MODE", "") or os.environ.get("MLX_PARALLEL_MODE", ""),
        "backend": backend_override or os.environ.get("MLX_DISTRIBUTED_BACKEND", ""),
        "rank": rank_override if rank_override is not None else env_rank,
        "world_size": world_override if world_override is not None else env_ws,
        "tp_size": 0,
        "pp_size": 0,
    }
    if not process_pattern:
        return info

    rows: dict[int, tuple[int, str]] = {}
    self_pid = os.getpid()
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid=,ppid=,args="],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            pid, ppid, args = int(parts[0]), int(parts[1]), parts[2]
            if pid != self_pid:
                rows[pid] = (ppid, args)
    except Exception as exc:
        debug(f"ps scan in shard topology failed: {exc!r}", component="shard")
        return info

    matched_pids = [pid for pid, (_, args) in rows.items() if process_pattern in args]
    if not matched_pids:
        return info

    chain_args: list[str] = []
    cur = matched_pids[0]
    for _ in range(4):
        if cur not in rows:
            break
        ppid, args = rows[cur]
        chain_args.append(args)
        proc_env = _read_proc_env(cur)
        for rk_key in ("MLX_RANK", "OMPI_COMM_WORLD_RANK", "PMI_RANK", "RANK"):
            if info["rank"] is None and proc_env.get(rk_key, "").isdigit():
                info["rank"] = int(proc_env[rk_key])
        for ws_key in ("MLX_WORLD_SIZE", "OMPI_COMM_WORLD_SIZE", "PMI_SIZE", "WORLD_SIZE"):
            if info["world_size"] is None and proc_env.get(ws_key, "").isdigit():
                info["world_size"] = int(proc_env[ws_key])
        cur = ppid

    for args in chain_args:
        try:
            toks = shlex.split(args)
        except ValueError:
            toks = args.split()
        if not info["hostfile"]:
            info["hostfile"] = _extract_flag_value(toks, ("--hostfile", "-H"))
        if not info["backend"]:
            info["backend"] = _extract_flag_value(toks, ("--backend",))
        if info["world_size"] is None:
            np_val = _extract_flag_value(toks, ("-np", "-n", "--world-size"))
            if np_val.isdigit() and int(np_val) > 0:
                info["world_size"] = int(np_val)
        tp_val = _extract_flag_value(toks, ("--tensor-parallel-size", "-tp"))
        if tp_val.isdigit() and int(tp_val) > 0:
            info["tp_size"] = int(tp_val)
        pp_val = _extract_flag_value(toks, ("--pipeline-parallel-size", "-pp"))
        if pp_val.isdigit() and int(pp_val) > 0:
            info["pp_size"] = int(pp_val)
        kv_val = _extract_flag_value(toks, ("--shard-kv-heads", "--kv-heads", "--num-kv-heads"))
        if kv_val.isdigit() and int(kv_val) > 0:
            info["shard_kv_heads"] = int(kv_val)
        if not info["mode"] and ("--pipeline" in toks or info["pp_size"] > 1):
            info["mode"] = "pipeline"

    if not info["mode"]:
        if info["tp_size"] > 1 and info["pp_size"] > 1:
            info["mode"] = "hybrid"
        elif info["tp_size"] > 1:
            info["mode"] = "tensor"
        elif info["pp_size"] > 1 or info["hostfile"]:
            info["mode"] = "pipeline"
    return info


def _split_layer_range(num_layers: int, rank: int, world_size: int) -> tuple[int, int]:
    """Partition [0, num_layers) evenly across `world_size` pipeline stages."""
    if num_layers <= 0 or world_size <= 1:
        return (0, max(0, num_layers - 1)) if num_layers > 0 else (-1, -1)
    r = max(0, min(rank, world_size - 1))
    base, rem = divmod(num_layers, world_size)
    start = r * base + min(r, rem)
    count = base + (1 if r < rem else 0)
    if count <= 0:
        return -1, -1
    return start, start + count - 1


def _parse_layer_override(spec: str) -> tuple[int, int] | None:
    """Parse explicit '0..15', '0-15', or '0:15' range string."""
    if not spec:
        return None
    m = re.match(r"^\s*(\d+)\s*(?:\.\.|-|:)\s*(\d+)\s*$", spec)
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    return (min(a, b), max(a, b))


def _summarize_shard_files(files: list[str], total_expected: int) -> str:
    """Format shard filename list into compact low-cardinality string."""
    if not files:
        return "none"
    if total_expected > 0 and len(files) >= total_expected:
        return f"all({total_expected})"
    nums = []
    for fname in sorted(files):
        m = _SHARD_NUM_RE.search(fname)
        if m:
            nums.append(m.group(1))
    if nums and len(nums) == len(files):
        if len(nums) == 1:
            return f"shard-{nums[0]}/{total_expected or len(files)}"
        return f"shards-{nums[0]}..{nums[-1]}/{total_expected or len(files)}"
    if len(files) <= 2:
        return ",".join(sorted(files))
    return f"{files[0]}..{files[-1]} ({len(files)})"


def _resolve_local_rank(
    hosts: list[dict[str, Any]],
    ip_iface: dict[str, str],
    device_ip: str = "",
) -> tuple[int, str, str]:
    """Match local IP/hostname against hostfile entries -> (rank, ip, iface)."""
    local_ips = set(ip_iface.keys())
    if device_ip:
        local_ips.add(device_ip)
    local_names = {socket.gethostname(), socket.gethostname().split(".")[0]}

    for entry in hosts:
        for ip in entry.get("ips", []):
            if ip in ip_iface and ip not in ("127.0.0.1", "::1"):
                return entry["rank"], ip, ip_iface[ip]
        if entry.get("ssh") in local_names or entry.get("ssh") in local_ips:
            first_ip = (entry.get("ips") or [entry.get("ssh") or "127.0.0.1"])[0]
            return entry["rank"], first_ip, ip_iface.get(first_ip, "unknown")
    for entry in hosts:
        if entry.get("ssh") in ("127.0.0.1", "localhost"):
            first_ip = (entry.get("ips") or ["127.0.0.1"])[0]
            return entry["rank"], first_ip, ip_iface.get(first_ip, "lo")
    return 0, device_ip or "127.0.0.1", ip_iface.get(device_ip or "127.0.0.1", "lo")


def _select_rank_files(
    index_info: dict[str, Any],
    layer_start: int,
    layer_end: int,
    mode: str,
    rank: int,
    world_size: int,
    pp_rank: int = 0,
    pp_size: int = 1,
) -> tuple[list[str], int]:
    """Return (shard filenames for rank, expected weight bytes)."""
    expected = index_info["expected_files"]
    total_bytes = int(index_info["total_bytes"] or 0)
    layer_to_files: dict[int, set[str]] = index_info["layer_to_files"]
    file_sizes: dict[str, int] = index_info["file_sizes"]

    if world_size <= 1 or mode == "standalone":
        return expected, total_bytes
    if mode == "tensor":
        return expected, total_bytes // max(1, world_size)

    if layer_to_files and layer_end >= layer_start >= 0:
        selected: set[str] = set()
        for lidx in range(layer_start, layer_end + 1):
            selected.update(layer_to_files.get(lidx, set()))
        is_first_stage = (pp_rank == 0) if mode == "hybrid" else (rank == 0)
        is_last_stage = (pp_rank == pp_size - 1) if mode == "hybrid" else (rank == world_size - 1)
        if is_first_stage:
            selected.update(index_info["embed_files"])
        if is_last_stage:
            selected.update(index_info["head_files"])
        sel_list = sorted(selected)
        sel_bytes = sum(file_sizes.get(f, 0) for f in sel_list)
        if not sel_bytes and total_bytes and expected:
            sel_bytes = int(total_bytes * (len(sel_list) / len(expected)))
        return sel_list, sel_bytes

    approx_bytes = total_bytes // max(1, world_size)
    return expected, approx_bytes


def discover_shard_placement(
    model_info_obj: ModelInfo | None,
    model_id: str,
    model_path: str = "",
    process_pattern: str = "mlx_lm.server",
    hostfile: str = "",
    mode_override: str = "",
    backend_override: str = "",
    rank_override: int | None = None,
    world_override: int | None = None,
    device_ip: str = "",
    device_id: str = "",
    shard_layers_override: str = "",
    nodes_devices: list[dict[str, Any]] | None = None,
) -> ShardPlacement:
    """Resolve current node's model shard assignment and cluster peer table."""
    runtime = infer_runtime_sharding(
        process_pattern=process_pattern,
        hostfile_override=hostfile,
        mode_override=mode_override,
        backend_override=backend_override,
        rank_override=rank_override,
        world_override=world_override,
    )
    hf_backend, hosts = parse_hostfile(runtime["hostfile"])
    ip_iface = local_ip_iface_map()

    backend = runtime["backend"] or hf_backend or "none"
    world_size = runtime["world_size"] or (len(hosts) if hosts else 0)
    if not world_size and (runtime["tp_size"] > 1 or runtime["pp_size"] > 1):
        world_size = max(1, runtime["tp_size"]) * max(1, runtime["pp_size"])
    if world_size <= 0:
        world_size = 1

    if runtime["rank"] is not None:
        rank = runtime["rank"]
        if rank < len(hosts) and hosts[rank].get("ips"):
            transport_ip = hosts[rank]["ips"][0]
            iface = ip_iface.get(transport_ip, "unknown")
        else:
            transport_ip = device_ip or "127.0.0.1"
            iface = ip_iface.get(transport_ip, "lo")
    elif hosts:
        rank, transport_ip, iface = _resolve_local_rank(hosts, ip_iface, device_ip)
    else:
        rank = 0
        transport_ip = device_ip or "127.0.0.1"
        iface = ip_iface.get(transport_ip, "lo")

    mode = runtime["mode"] or ("standalone" if world_size <= 1 else "pipeline")
    if world_size <= 1:
        mode = "standalone"

    tp_size = max(1, runtime["tp_size"] or (world_size if mode == "tensor" else int(os.environ.get("MLX_TP_SIZE", "1") or "1")))
    pp_size = max(1, runtime["pp_size"] or (world_size // tp_size if tp_size > 1 and world_size % tp_size == 0 else (world_size if mode == "pipeline" else 1)))

    if mode == "hybrid" or (tp_size > 1 and pp_size > 1 and world_size > 1):
        mode = "hybrid"
        tp_rank = rank % tp_size
        pp_rank = rank // tp_size
    elif mode == "tensor":
        tp_rank = rank
        pp_rank = 0
        tp_size = world_size
        pp_size = 1
    else:
        tp_rank = 0
        pp_rank = rank
        tp_size = 1
        pp_size = world_size

    eff_path = Path(model_info_obj.model_path if model_info_obj else model_path) if (
        model_info_obj or model_path
    ) else None
    index_info = inspect_safetensors_index(eff_path)
    num_layers = model_info_obj.num_layers if model_info_obj else 0

    parsed_override = _parse_layer_override(
        shard_layers_override or os.environ.get("MLX_SHARD_LAYERS", "")
    )
    if parsed_override is not None:
        l_start, l_end = parsed_override
    elif mode == "tensor":
        l_start, l_end = (0, max(0, num_layers - 1)) if num_layers > 0 else (-1, -1)
    elif mode == "hybrid":
        l_start, l_end = _split_layer_range(num_layers, pp_rank, pp_size)
    else:
        l_start, l_end = _split_layer_range(num_layers, rank, world_size)

    layers_assigned = (l_end - l_start + 1) if l_end >= l_start >= 0 else 0
    layer_range = f"{l_start}..{l_end}" if l_end >= l_start >= 0 else "unknown"

    rank_files, exp_bytes = _select_rank_files(
        index_info, l_start, l_end, mode, rank, world_size, pp_rank=pp_rank, pp_size=pp_size
    )
    if not exp_bytes and model_info_obj and model_info_obj.total_weight_bytes:
        exp_bytes = model_info_obj.total_weight_bytes // max(1, world_size)

    tp_div = max(1, tp_size)
    dim_hidden = model_info_obj.hidden_size if model_info_obj else 0

    local_dev = None
    if nodes_devices:
        for dev in nodes_devices:
            if device_id and dev.get("id") == device_id:
                local_dev = dev
                break
            if dev.get("rank") == rank:
                local_dev = dev
                break
            if device_ip and dev.get("ip") == device_ip:
                local_dev = dev
                break

    raw_shard_kv = os.environ.get("MLX_SHARD_KV_HEADS", "")
    node_kv = local_dev.get("shard_kv_heads") if local_dev else None

    kv_heads_exact = True
    kv_source = "exact"

    if raw_shard_kv and raw_shard_kv.isdigit() and int(raw_shard_kv) > 0:
        dim_kv = int(raw_shard_kv)
        kv_source = "env_override"
    elif node_kv is not None and str(node_kv).isdigit() and int(node_kv) > 0:
        dim_kv = int(node_kv)
        kv_source = "nodes_config"
    elif runtime.get("shard_kv_heads"):
        dim_kv = int(runtime["shard_kv_heads"])
        kv_source = "process_arg"
    elif model_info_obj and model_info_obj.num_key_value_heads:
        if tp_div <= 1:
            dim_kv = model_info_obj.num_key_value_heads
            kv_source = "full_model"
        elif model_info_obj.num_key_value_heads % tp_div == 0:
            dim_kv = model_info_obj.num_key_value_heads // tp_div
            kv_heads_exact = False
            kv_source = "uniform_division_estimate"
        else:
            base = model_info_obj.num_key_value_heads // tp_div
            rem = model_info_obj.num_key_value_heads % tp_div
            dim_kv = max(1, base + (1 if tp_rank < rem else 0))
            kv_heads_exact = False
            kv_source = "estimated_fallback"
    else:
        dim_kv = 0
        kv_heads_exact = False
        kv_source = "unknown"
    dim_inter = (
        max(1, model_info_obj.intermediate_size // tp_div)
        if model_info_obj and model_info_obj.intermediate_size
        else 0
    )

    peers: list[tuple[int, str]] = []
    cluster_nodes: list[dict[str, Any]] = []
    if hosts:
        for h in hosts:
            pr = int(h["rank"])
            pip = (h.get("ips") or [h.get("ssh") or "unknown"])[0]
            if pr != rank and pip not in ("127.0.0.1", "localhost"):
                peers.append((pr, pip))
            if mode == "tensor" and num_layers > 0:
                ps, pe = (0, max(0, num_layers - 1))
            elif mode == "hybrid" and num_layers > 0:
                ps, pe = _split_layer_range(num_layers, pr // tp_size, pp_size)
            else:
                ps, pe = _split_layer_range(num_layers, pr, world_size)
            node_name = device_id if pr == rank and device_id else f"rank{pr}"
            if nodes_devices:
                for dev in nodes_devices:
                    if dev.get("rank") == pr or dev.get("ip") == pip:
                        node_name = str(dev.get("id") or node_name)
                        break
            cluster_nodes.append({
                "device": node_name,
                "rank": pr,
                "world_size": world_size,
                "mode": mode,
                "layer_range": f"{ps}..{pe}" if pe >= ps >= 0 else "unknown",
                "node_ip": pip,
            })
    else:
        cluster_nodes.append({
            "device": device_id or "local",
            "rank": rank,
            "world_size": world_size,
            "mode": mode,
            "layer_range": layer_range,
            "node_ip": transport_ip,
        })

    total_model_files = len(index_info["expected_files"])
    if rank_files:
        expected_cnt = len(rank_files)
        present_cnt = sum(1 for f in rank_files if f in index_info.get("present_files", []))
    else:
        expected_cnt = total_model_files
        present_cnt = len(index_info["present_files"])

    return ShardPlacement(
        model_id=model_id or "unknown",
        rank=rank,
        world_size=world_size,
        mode=mode,
        backend=backend,
        layer_start=l_start,
        layer_end=l_end,
        layers_assigned=layers_assigned,
        layer_range=layer_range,
        shard_files=_summarize_shard_files(rank_files, total_model_files),
        shard_files_present=present_cnt,
        shard_files_expected=expected_cnt,
        expected_weight_bytes=exp_bytes,
        dim_hidden=dim_hidden,
        dim_kv_heads=dim_kv,
        dim_intermediate=dim_inter,
        transport_ip=transport_ip,
        iface=iface,
        peers=tuple(peers),
        cluster_nodes=tuple(cluster_nodes),
        tp_rank=tp_rank,
        pp_rank=pp_rank,
        tp_size=tp_size,
        pp_size=pp_size,
        kv_heads_exact=kv_heads_exact,
        kv_source=kv_source,
    )


def probe_peer_transport(
    peers: tuple[tuple[int, str], ...],
    port: int = 8081,
    timeout: float = 0.5,
) -> dict[tuple[int, str], PeerConnectResult]:
    """Passively check TCP reachability & connect latency (SYN-ACK) of peer ranks.

    Measures pure TCP connection establishment duration only. Does not transmit application data.
    """
    status: dict[tuple[int, str], PeerConnectResult] = {}
    if port <= 0:
        for pr, pip in peers:
            status[(pr, pip)] = PeerConnectResult(
                up=False,
                connect_seconds=0.0,
                reason="disabled",
            )
        return status
    for pr, pip in peers:
        t0 = time.monotonic()
        try:
            with socket.create_connection((pip, port), timeout=timeout):
                connect_elapsed = max(0.0, time.monotonic() - t0)
                status[(pr, pip)] = PeerConnectResult(
                    up=True,
                    connect_seconds=connect_elapsed,
                    reason="connected",
                )
        except ConnectionRefusedError:
            status[(pr, pip)] = PeerConnectResult(
                up=False,
                connect_seconds=0.0,
                reason="refused",
            )
        except socket.timeout:
            status[(pr, pip)] = PeerConnectResult(
                up=False,
                connect_seconds=0.0,
                reason="timeout",
            )
        except OSError:
            status[(pr, pip)] = PeerConnectResult(
                up=False,
                connect_seconds=0.0,
                reason="unreachable",
            )
    return status


def probe_peer_app(
    peers: tuple[tuple[int, str], ...],
    port: int = 8081,
    path: str = "/",
    timeout: float = 0.5,
) -> dict[tuple[int, str], PeerAppResult]:
    """Measure application request/response round-trip time (RTT) to peer service endpoint.

    Measures complete application round-trip time (request write through complete response body receipt),
    strictly isolated from TCP connection establishment.
    """
    status: dict[tuple[int, str], PeerAppResult] = {}
    if port <= 0:
        for pr, pip in peers:
            status[(pr, pip)] = PeerAppResult(
                up=False,
                app_rtt_seconds=0.0,
                status_code=0,
                reason="disabled",
            )
        return status
    for pr, pip in peers:
        try:
            with socket.create_connection((pip, port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                host_hdr = pip if ":" not in pip else f"[{pip}]"
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {host_hdr}:{port}\r\n"
                    f"User-Agent: mlx-probe\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode("ascii")
                t_req = time.monotonic()
                sock.sendall(req)

                chunks: list[bytes] = []
                header_end = -1
                content_length: int | None = None
                is_chunked = False

                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if header_end == -1:
                        buf = b"".join(chunks)
                        pos = buf.find(b"\r\n\r\n")
                        if pos != -1:
                            header_end = pos + 4
                            hdr_text = buf[:pos].decode("latin1", "replace")
                            for line in hdr_text.split("\r\n")[1:]:
                                if ":" in line:
                                    k, v = line.split(":", 1)
                                    k_lower = k.strip().lower()
                                    if k_lower == "content-length":
                                        try:
                                            content_length = int(v.strip())
                                        except ValueError:
                                            pass
                                    elif k_lower == "transfer-encoding" and "chunked" in v.lower():
                                        is_chunked = True
                    if header_end != -1 and content_length is not None and not is_chunked:
                        current_body_len = sum(len(c) for c in chunks) - header_end
                        if current_body_len >= content_length:
                            break
                    if is_chunked:
                        buf = b"".join(chunks)
                        if buf.endswith(b"0\r\n\r\n"):
                            break

                elapsed = max(0.0, time.monotonic() - t_req)
                resp_bytes = b"".join(chunks)
                status_code = 0
                is_http = False
                if resp_bytes:
                    first_line = resp_bytes.split(b"\r\n", 1)[0].decode("latin1", "replace")
                    parts = first_line.split()
                    if len(parts) >= 2 and parts[0].startswith("HTTP/") and parts[1].isdigit():
                        status_code = int(parts[1])
                        is_http = True
                is_up = is_http and (100 <= status_code < 600)
                status[(pr, pip)] = PeerAppResult(
                    up=is_up,
                    app_rtt_seconds=elapsed if is_up else 0.0,
                    status_code=status_code,
                    reason="ok" if is_up else (f"status_{status_code}" if is_http else "not_http"),
                )
        except ConnectionRefusedError:
            status[(pr, pip)] = PeerAppResult(
                up=False,
                app_rtt_seconds=0.0,
                status_code=0,
                reason="refused",
            )
        except socket.timeout:
            status[(pr, pip)] = PeerAppResult(
                up=False,
                app_rtt_seconds=0.0,
                status_code=0,
                reason="timeout",
            )
        except Exception as exc:
            status[(pr, pip)] = PeerAppResult(
                up=False,
                app_rtt_seconds=0.0,
                status_code=0,
                reason=f"error_{type(exc).__name__}",
            )
    return status


def probe_peer_reachability(
    peers: tuple[tuple[int, str], ...],
    port: int = 8081,
    timeout: float = 0.5,
    probe_app_rtt: bool = True,
    app_port: int | None = None,
) -> dict[tuple[int, str], PeerProbeResult]:
    """Passively check peer reachability, measuring both TCP connect and application RTT independently."""
    eff_app_port = app_port if app_port is not None else 0
    conn_results = probe_peer_transport(peers, port=port, timeout=timeout)
    app_results = (
        probe_peer_app(peers, port=eff_app_port, timeout=timeout)
        if probe_app_rtt and eff_app_port > 0
        else {}
    )

    status: dict[tuple[int, str], PeerProbeResult] = {}
    for pair, c_res in conn_results.items():
        a_res = app_results.get(pair)
        status[pair] = PeerProbeResult(
            up=c_res.up,
            connect_seconds=c_res.connect_seconds,
            reason=c_res.reason,
            app_rtt_seconds=a_res.app_rtt_seconds if a_res and a_res.up else 0.0,
            app_up=a_res.up if a_res else False,
        )
    return status
