#!/usr/bin/env python3
"""
mlx_hw_telemetry.py
===================
Per-node hardware telemetry exporter for the MLX cluster.

Runs on each Mac and exposes host metrics (CPU, memory, disk, GPU power,
thermal pressure, network) as Prometheus metrics. All configuration comes
from environment variables - nothing is hardcoded.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

from mlx_logging import degraded, debug, error, info, parse_listen_addr, setup_logging

NODE_DEFAULT = "unknown"
NODE: str = NODE_DEFAULT
LISTEN_DEFAULT = "127.0.0.1:9102"
SAMPLE_INTERVAL_DEFAULT = 5.0
GPU_SAMPLE_INTERVAL_DEFAULT = 15.0

G_UP = Gauge("mlx_hw_up", "1 if the node telemetry agent is running", ["node"])
G_SSH_UP = Gauge("mlx_ssh_up", "1 if SSH daemon is reachable on local port 22", ["node"])
G_UPTIME = Gauge("mlx_hw_uptime_seconds", "Host uptime in seconds", ["node"])
G_LOAD1 = Gauge("mlx_hw_load1", "1-minute system load average", ["node"])
G_LOAD5 = Gauge("mlx_hw_load5", "5-minute system load average", ["node"])
G_LOAD15 = Gauge("mlx_hw_load15", "15-minute system load average", ["node"])
G_CPU_COUNT = Gauge("mlx_hw_cpu_count", "Logical CPU count", ["node"])
G_CPU_TEMP = Gauge("mlx_hw_cpu_temp_celsius", "CPU temperature", ["node"])
G_MEM_TOTAL = Gauge("mlx_hw_mem_total_bytes", "Total physical memory", ["node"])
G_MEM_USED = Gauge("mlx_hw_mem_used_bytes", "Used physical memory", ["node"])
G_MEM_FREE = Gauge("mlx_hw_mem_free_bytes", "Completely free physical memory", ["node"])
G_MEM_AVAILABLE = Gauge(
    "mlx_hw_mem_available_bytes",
    "Memory available to new processes (free + inactive + speculative + purgeable)",
    ["node"],
)
G_MEM_PRESSURE = Gauge("mlx_hw_mem_pressure", "Free page count", ["node"])
G_PAGE_SIZE = Gauge("mlx_hw_page_size_bytes", "Virtual memory page size", ["node"])
G_DISK_TOTAL = Gauge("mlx_hw_disk_total_bytes", "Root volume total size", ["node"])
G_DISK_USED = Gauge("mlx_hw_disk_used_bytes", "Root volume used size", ["node"])
G_WORKER_CPU = Gauge("mlx_worker_cpu_percent", "mlx_lm.server CPU %", ["node"])
G_WORKER_RSS = Gauge("mlx_worker_rss_bytes", "mlx_lm.server resident set size", ["node"])
G_GPU_UTIL = Gauge("mlx_hw_gpu_utilization_percent", "GPU active residency %", ["node"])
G_GPU_FREQ = Gauge("mlx_hw_gpu_frequency_mhz", "GPU active frequency (MHz)", ["node"])
G_GPU_POWER = Gauge("mlx_hw_gpu_power_milliwatts", "GPU power draw (mW)", ["node"])
G_PKG_POWER = Gauge("mlx_hw_package_power_milliwatts", "Package power (mW)", ["node"])
G_CPU_POWER = Gauge("mlx_hw_cpu_power_milliwatts", "CPU power (mW)", ["node"])
G_ANE_POWER = Gauge("mlx_hw_ane_power_milliwatts", "Neural Engine power (mW)", ["node"])
G_THERMAL = Gauge("mlx_hw_thermal_pressure", "Thermal pressure 0..1", ["node"])
G_GPU_MEM_USED = Gauge("mlx_hw_gpu_mem_used_bytes", "GPU memory in use", ["node"])
G_GPU_MEM_ALLOC = Gauge("mlx_hw_gpu_mem_alloc_bytes", "GPU memory allocated", ["node"])
G_GPU_MEM_TOTAL = Gauge("mlx_hw_gpu_mem_total_bytes", "Total GPU memory", ["node"])
G_GPU_UTIL_IOKIT = Gauge("mlx_hw_gpu_iokit_util_percent", "GPU activity %", ["node"])
G_PROC_CPU = Gauge(
    "mlx_proc_cpu_percent",
    "Per-stack-component CPU %",
    ["node", "component"],
)
G_PROC_RSS = Gauge(
    "mlx_proc_rss_bytes",
    "Per-stack-component resident set size",
    ["node", "component"],
)
C_NET_RX = Counter(
    "mlx_net_rx_bytes_total",
    "Cumulative bytes received on the ring interface",
    ["node", "iface"],
)
C_NET_TX = Counter(
    "mlx_net_tx_bytes_total",
    "Cumulative bytes sent on the ring interface",
    ["node", "iface"],
)
G_NET_RX_RATE = Gauge(
    "mlx_hw_net_rx_bytes_per_sec",
    "Current network receive throughput in bytes per second",
    ["node", "iface"],
)
G_NET_TX_RATE = Gauge(
    "mlx_hw_net_tx_bytes_per_sec",
    "Current network transmit throughput in bytes per second",
    ["node", "iface"],
)
C_NET_RX_ERR = Counter(
    "mlx_net_rx_errors_total",
    "Cumulative receive errors",
    ["node", "iface"],
)
C_NET_TX_ERR = Counter(
    "mlx_net_tx_errors_total",
    "Cumulative transmit errors",
    ["node", "iface"],
)

_PROC_COMPONENTS = {
    "mlx_lm_server": "mlx_lm.server",
    "mlx_proxy": "mlx_metrics_proxy",
    "kv_agent": "mlx_kv_cache_agent",
    "log_tailer": "mlx_server_log_tailer",
    "supervisor": "mlx_server_supervisor",
}

_CONTAINER_COMPONENTS = {
    "otel_collector": ("central-otelcol", "central-otel-collector", "otel-collector"),
    "victoria_metrics": ("central-victoriametrics", "victoria-metrics"),
    "grafana": ("central-grafana", "grafana"),
    "alertmanager": ("central-alertmanager", "alertmanager"),
    "vmalert": ("central-vmalert", "vmalert"),
}

_PAGE_SIZE = 0
_MEM_TOTAL = 0
_PROC_MATCH = None
_NET_IFACE = ""
_CONTAINER_STATS = False

_GPU_LOCK = threading.Lock()
_POWERMETRICS_LOCK = threading.Lock()
_SAMPLE_LOCK = threading.Lock()
_NET_IFACE_CACHE: tuple[str | None, float] = (None, 0.0)
_GPU_STATS = {"util": float("nan"), "freq": float("nan"),
              "gpu_power": float("nan"), "pkg_power": float("nan"),
              "cpu_power": float("nan"), "ane_power": float("nan"),
              "gpu_mem_used": float("nan"), "gpu_mem_alloc": float("nan"),
              "gpu_mem_total": float("nan"), "gpu_util_iokit": float("nan")}


def _sysctl(name: str, default=None):
    try:
        out = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        return out or default
    except Exception as exc:
        debug(f"sysctl {name} failed: {exc!r}, using default", component="hw")
        return default


def _parse_page_size():
    global _PAGE_SIZE
    try:
        import sysconfig
        _PAGE_SIZE = sysconfig.get_config_var("PAGESIZE") or os.sysconf("SC_PAGE_SIZE")
    except Exception as exc:
        debug(f"failed to parse page size: {exc!r}, using 4096", component="hw")
        _PAGE_SIZE = 4096


def _mem_info():
    """Return memory accounting in bytes from vm_stat (macOS) or /proc/meminfo (Linux)."""
    global _MEM_TOTAL
    if os.path.exists("/proc/meminfo"):
        try:
            kv: dict[str, int] = {}
            with open("/proc/meminfo", "r", encoding="utf8") as fh:
                for line in fh:
                    if ":" in line:
                        k, rest = line.split(":", 1)
                        val_parts = rest.strip().split()
                        if val_parts and val_parts[0].isdigit():
                            kv[k.strip()] = int(val_parts[0]) * 1024
            total = kv.get("MemTotal", _MEM_TOTAL)
            if total and not _MEM_TOTAL:
                _MEM_TOTAL = total
            free = kv.get("MemFree", 0)
            available = kv.get("MemAvailable", free)
            used = max(0, (total or 0) - available)
            page_size = _PAGE_SIZE or 4096
            return {
                "used": used,
                "free": free,
                "available": available,
                "free_pages": free // page_size,
                "page_size": page_size,
            }
        except Exception as exc:
            debug(f"/proc/meminfo failed: {exc!r}", component="hw")
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5
        ).stdout
        pages = {}
        for line in out.splitlines():
            m = re.match(r"([^:]+):\s+(\d+)", line.strip())
            if m:
                pages[m.group(1).strip().lower()] = int(m.group(2))
        page_size = _PAGE_SIZE or 4096
        free = pages.get("pages free", 0)
        inactive = pages.get("pages inactive", 0)
        spec = pages.get("pages speculative", 0)
        purgeable = pages.get("pages purgeable", 0)
        free_bytes = free * page_size
        available_bytes = (free + inactive + spec + purgeable) * page_size
        used = max(0, _MEM_TOTAL - available_bytes)
        return {
            "used": used,
            "free": free_bytes,
            "available": available_bytes,
            "free_pages": free,
            "page_size": page_size,
        }
    except Exception as exc:
        debug(f"mem_info failed: {exc!r}", component="hw")
        return {
            "used": _MEM_TOTAL,
            "free": 0,
            "available": 0,
            "free_pages": 0,
            "page_size": _PAGE_SIZE or 0,
        }


def _cpu_temp():
    if sys.platform != "darwin":
        try:
            import glob

            temps = []
            for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
                try:
                    with open(path, "r", encoding="utf8") as fh:
                        raw = fh.read().strip()
                    if raw.lstrip("-").isdigit():
                        c = int(raw) / 1000.0
                        if 0.0 < c < 150.0:
                            temps.append(c)
                except OSError:
                    continue
            return max(temps) if temps else 0.0
        except Exception:
            return 0.0
    try:
        with _POWERMETRICS_LOCK:
            out = subprocess.run(
                ["sudo", "-n", "powermetrics", "-n", "1", "--samplers", "thermal", "-f", "text"],
                capture_output=True, text=True, timeout=6,
            ).stdout
        for line in out.splitlines():
            m = re.search(r"(?:CPU die temperature|package.*temperature|temperature)\s*:\s*([\d.]+)",
                          line, re.I)
            if m:
                return float(m.group(1))
        return 0.0
    except Exception as exc:
        degraded(f"CPU temperature unavailable (powermetrics/sudo): {exc!r}", component="hw")
        return 0.0


def _gpu_iokit_stats():
    used = alloc = total = util = float("nan")
    if sys.platform != "darwin":
        return used, alloc, total, util
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOAccelerator", "-r", "-d", "2"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r'"In use system memory"=(\d+)', out)
        if m:
            used = float(m.group(1))
        m = re.search(r'"Alloc system memory"=(\d+)', out)
        if m:
            alloc = float(m.group(1))
        m = re.search(r'"VRAM,totalMB"=(\d+)', out)
        if m:
            total = float(m.group(1)) * 1024 * 1024
        else:
            mem = _sysctl("hw.memsize", "")
            if mem:
                total = float(mem)
        m = re.search(r'"Device Utilization %"=(\d+)', out)
        if m:
            util = float(m.group(1))
    except Exception as exc:
        debug(f"iostat GPU stats unavailable: {exc!r}", component="hw")
    return used, alloc, total, util


def _gpu_power_stats():
    global _GPU_STATS
    util = freq = gpu_power = pkg_power = cpu_power = ane_power = float("nan")
    if sys.platform != "darwin":
        with _GPU_LOCK:
            _GPU_STATS = {
                "util": util, "freq": freq,
                "gpu_power": gpu_power, "pkg_power": pkg_power,
                "cpu_power": cpu_power, "ane_power": ane_power,
                "gpu_mem_used": util, "gpu_mem_alloc": util,
                "gpu_mem_total": util, "gpu_util_iokit": util,
            }
        return
    try:
        with _POWERMETRICS_LOCK:
            out = subprocess.run(
                ["sudo", "-n", "powermetrics", "-n", "1",
                 "--samplers", "cpu_power,gpu_power", "-i", "1000", "-f", "text"],
                capture_output=True, text=True, timeout=8,
            ).stdout
        for line in out.splitlines():
            m = re.search(r"GPU HW active residency:\s*([\d.]+)%", line)
            if m:
                util = float(m.group(1))
            m = re.search(r"GPU HW active frequency:\s*([\d.]+)\s*MHz", line)
            if m:
                freq = float(m.group(1))
            m = re.search(r"Combined Power \(CPU \+ GPU \+ ANE\):\s*([\d.]+)\s*mW",
                          line, re.I)
            if m:
                pkg_power = float(m.group(1))
            m = re.search(r"GPU Power:\s*([\d.]+)\s*mW", line, re.I)
            if m:
                gpu_power = float(m.group(1))
            m = re.search(r"CPU Power:\s*([\d.]+)\s*mW", line, re.I)
            if m:
                cpu_power = float(m.group(1))
            m = re.search(r"ANE Power:\s*([\d.]+)\s*mW", line, re.I)
            if m:
                ane_power = float(m.group(1))
    except Exception as exc:
        degraded(f"GPU power stats unavailable (powermetrics/sudo): {exc!r}", component="hw")
    gmu, ga, gmt, gui = _gpu_iokit_stats()
    with _GPU_LOCK:
        _GPU_STATS = {
            "util": util, "freq": freq,
            "gpu_power": gpu_power, "pkg_power": pkg_power,
            "cpu_power": cpu_power, "ane_power": ane_power,
            "gpu_mem_used": gmu, "gpu_mem_alloc": ga,
            "gpu_mem_total": gmt, "gpu_util_iokit": gui,
        }


def _thermal_pressure():
    if sys.platform != "darwin":
        return 0.0
    try:
        out = subprocess.run(
            ["pmset", "-g", "therm"], capture_output=True, text=True, timeout=3
        ).stdout
        for line in out.splitlines():
            m = re.search(r"CPU_Scheduler_Limit:\s*(\d+)", line)
            if m:
                limit = int(m.group(1))
                return max(0.0, min(1.0, (100 - limit) / 100.0))
        return 0.0
    except Exception as exc:
        degraded(f"thermal pressure unavailable (pmset): {exc!r}", component="hw")
        return float("nan")


def _gpu_sampler_loop(interval: float, stop_event: threading.Event | None = None):
    if sys.platform != "darwin":
        return
    while stop_event is None or not stop_event.is_set():
        try:
            _gpu_power_stats()
        except Exception as exc:
            error(f"gpu sampler error: {exc!r}", component="hw")
        if stop_event is not None:
            stop_event.wait(interval)
        else:
            time.sleep(interval)


def _collect_ps_stats():
    """Single `ps` pass returning ((worker_cpu, worker_rss), proc_component_dict)."""
    wcpu = 0.0
    wrss = 0
    stats = {name: (0.0, 0) for name in _PROC_COMPONENTS}
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,pcpu=,rss=,command="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "mlx_hw_telemetry" in line:
                continue
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            cmd = parts[3]
            try:
                cpu = float(parts[1])
                rss = int(parts[2]) * 1024
            except ValueError:
                continue
            if (
                _PROC_MATCH
                and _PROC_MATCH in line
                and "mlx_metrics_proxy" not in line
                and "mlx_server_supervisor" not in line
            ):
                wcpu += cpu
                wrss += rss
            for name, needle in _PROC_COMPONENTS.items():
                if needle in cmd:
                    c, r = stats[name]
                    stats[name] = (c + cpu, r + rss)
    except Exception as exc:
        degraded(f"process stats collection failed: {exc!r}", component="hw")
    return (wcpu, wrss), stats


def _worker_stats():
    return _collect_ps_stats()[0]


def _proc_stats():
    return _collect_ps_stats()[1]


def _container_stats():
    stats = {}
    if not shutil.which("podman"):
        return stats
    try:
        out = subprocess.run(
            ["podman", "stats", "--no-stream",
             "--format", "{{.Name}} {{.CPUPerc}} {{.MemUsage}}"],
            capture_output=True, text=True, timeout=8,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name, cpu_pct = parts[0], parts[1].rstrip("%")
            for comp, aliases in _CONTAINER_COMPONENTS.items():
                if isinstance(aliases, str):
                    aliases = (aliases,)
                if name in aliases:
                    stats[comp] = (float(cpu_pct), _mem_usage_bytes(parts[2]))
                    break
    except Exception as exc:
        degraded(f"container stats collection failed: {exc!r}", component="hw")
    return stats


def _mem_usage_bytes(usage: str) -> int:
    m = re.match(r"([\d.]+)([KMGTP]?)(i?B)", usage)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)
    mult = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
    return int(num * mult.get(unit, 1))


def _net_iface():
    """Return the interface to monitor (cached for 60s unless overridden)."""
    global _NET_IFACE_CACHE
    if _NET_IFACE:
        return _NET_IFACE
    cached_iface, cached_at = _NET_IFACE_CACHE
    now = time.monotonic()
    if cached_iface and (now - cached_at) < 60.0:
        return cached_iface
    detected = None
    try:
        out = subprocess.run(
            ["route", "-n", "get", "default"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        m = re.search(r"interface:\s*(\S+)", out)
        if m:
            detected = m.group(1)
    except Exception as exc:
        debug(f"default route detection failed: {exc!r}", component="hw")
    if not detected:
        try:
            out = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            m = re.search(r"dev\s+(\S+)", out)
            if m:
                detected = m.group(1)
        except Exception:
            pass
    if not detected:
        try:
            out = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=3
            ).stdout
            cur = None
            for line in out.splitlines():
                m = re.match(r"^(\S+):", line)
                if m:
                    cur = m.group(1)
                    continue
                if cur and not cur.startswith("lo") and re.search(r"inet\s+\d+\.\d+\.\d+\.\d+\s", line):
                    detected = cur
                    break
        except Exception as exc:
            debug(f"iface fallback detection failed: {exc!r}", component="hw")
    if detected:
        _NET_IFACE_CACHE = (detected, now)
    return detected


def _net_stats(iface: str | None):
    if not iface:
        return None
    if os.path.exists("/proc/net/dev"):
        try:
            with open("/proc/net/dev", "r", encoding="utf8") as fh:
                for line in fh:
                    if ":" not in line:
                        continue
                    dev, rest = line.split(":", 1)
                    if dev.strip() != iface:
                        continue
                    fields = rest.split()
                    if len(fields) >= 11:
                        return (
                            int(fields[0]), int(fields[8]),
                            int(fields[2]), int(fields[10]),
                        )
        except Exception as exc:
            debug(f"/proc/net/dev stats failed: {exc!r}", component="hw")
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ib", "-I", iface],
            capture_output=True, text=True, timeout=3,
        ).stdout
        for line in out.splitlines():
            fields = line.split()
            if len(fields) < 10 or fields[0] != iface:
                continue
            if fields[2].startswith("<Link"):
                try:
                    return (
                        int(fields[6]), int(fields[9]),
                        int(fields[5]), int(fields[8]),
                    )
                except (ValueError, IndexError):
                    return None
    except Exception as exc:
        degraded(f"net stats collection failed: {exc!r}", component="hw")
    return None


_NET_LAST = {}
_NET_LAST_TIME = {}


def _ring_net_since(iface, counters):
    rx, tx, rx_err, tx_err = counters
    node = NODE  # set at startup
    now = time.time()
    prev = _NET_LAST.get(node, {}).get(iface)
    last_time = _NET_LAST_TIME.get(node, {}).get(iface, now)
    dt = max(0.001, now - last_time)
    if prev is None:
        _NET_LAST.setdefault(node, {})[iface] = (rx, tx, rx_err, tx_err)
        _NET_LAST_TIME.setdefault(node, {})[iface] = now
        return
    prx, ptx, perr, oerr = prev
    if rx >= prx:
        delta_rx = rx - prx
        C_NET_RX.labels(node, iface).inc(delta_rx)
        G_NET_RX_RATE.labels(node, iface).set(delta_rx / dt)
    if tx >= ptx:
        delta_tx = tx - ptx
        C_NET_TX.labels(node, iface).inc(delta_tx)
        G_NET_TX_RATE.labels(node, iface).set(delta_tx / dt)
    if rx_err >= perr:
        C_NET_RX_ERR.labels(node, iface).inc(rx_err - perr)
    if tx_err >= oerr:
        C_NET_TX_ERR.labels(node, iface).inc(tx_err - oerr)
    _NET_LAST[node][iface] = (rx, tx, rx_err, tx_err)
    _NET_LAST_TIME[node][iface] = now


def _host_uptime_seconds() -> float:
    if os.path.exists("/proc/uptime"):
        try:
            with open("/proc/uptime", "r", encoding="utf8") as fh:
                return max(0.0, float(fh.read().split()[0]))
        except (OSError, ValueError, IndexError):
            pass
    boot = _sysctl("kern.boottime", "") or ""
    m = re.search(r"sec\s*=\s*(\d+)", boot)
    if m:
        return max(0.0, time.time() - float(m.group(1)))
    return 0.0


def _sample():
    load1, load5, load15 = os.getloadavg()
    mem = _mem_info()
    disk = shutil.disk_usage("/")
    uptime = _host_uptime_seconds()
    cpu_temp = _cpu_temp()
    thermal = _thermal_pressure()
    (wcpu, wrss), proc_stats = _collect_ps_stats()
    cont_stats = _container_stats() if _CONTAINER_STATS else {}
    iface = _net_iface()
    net = _net_stats(iface)

    ssh_up = 0.0
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=0.3):
            ssh_up = 1.0
    except OSError:
        ssh_up = 0.0

    with _GPU_LOCK:
        stats = dict(_GPU_STATS)

    with _SAMPLE_LOCK:
        G_UP.labels(NODE).set(1)
        G_SSH_UP.labels(NODE).set(ssh_up)
        G_UPTIME.labels(NODE).set(uptime)
        G_LOAD1.labels(NODE).set(load1)
        G_LOAD5.labels(NODE).set(load5)
        G_LOAD15.labels(NODE).set(load15)
        G_CPU_COUNT.labels(NODE).set(os.cpu_count() or 0)
        G_CPU_TEMP.labels(NODE).set(cpu_temp)
        G_GPU_UTIL.labels(NODE).set(stats["util"])
        G_GPU_FREQ.labels(NODE).set(stats["freq"])
        G_GPU_POWER.labels(NODE).set(stats["gpu_power"])
        G_PKG_POWER.labels(NODE).set(stats["pkg_power"])
        G_CPU_POWER.labels(NODE).set(stats["cpu_power"])
        G_ANE_POWER.labels(NODE).set(stats["ane_power"])
        G_THERMAL.labels(NODE).set(thermal)
        G_GPU_MEM_USED.labels(NODE).set(stats["gpu_mem_used"])
        G_GPU_MEM_ALLOC.labels(NODE).set(stats["gpu_mem_alloc"])
        G_GPU_MEM_TOTAL.labels(NODE).set(stats["gpu_mem_total"])
        G_GPU_UTIL_IOKIT.labels(NODE).set(stats["gpu_util_iokit"])
        G_MEM_TOTAL.labels(NODE).set(_MEM_TOTAL)
        G_MEM_USED.labels(NODE).set(mem["used"])
        G_MEM_FREE.labels(NODE).set(mem["free"])
        G_MEM_AVAILABLE.labels(NODE).set(mem["available"])
        G_MEM_PRESSURE.labels(NODE).set(mem["free_pages"])
        G_PAGE_SIZE.labels(NODE).set(mem["page_size"])
        G_DISK_TOTAL.labels(NODE).set(disk.total)
        G_DISK_USED.labels(NODE).set(disk.used)

        G_WORKER_CPU.labels(NODE).set(wcpu)
        G_WORKER_RSS.labels(NODE).set(wrss)

        G_PROC_CPU.clear()
        G_PROC_RSS.clear()
        for name, (cpu, rss) in proc_stats.items():
            G_PROC_CPU.labels(NODE, name).set(cpu)
            G_PROC_RSS.labels(NODE, name).set(rss)

        for name, (cpu, rss) in cont_stats.items():
            G_PROC_CPU.labels(NODE, name).set(cpu)
            G_PROC_RSS.labels(NODE, name).set(rss)

        if net:
            _ring_net_since(iface, net)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        info("%s - %s" % (self.address_string(), fmt % args), component="hw")

    def do_GET(self):
        if self.path not in ("/", "/metrics"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with _SAMPLE_LOCK:
            payload = generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    global NODE, LISTEN, _MEM_TOTAL, _PROC_MATCH, SAMPLE_INTERVAL
    global _NET_IFACE, _CONTAINER_STATS

    ap = argparse.ArgumentParser(description="MLX hardware telemetry agent")
    device_id = (
        os.environ.get("MLX_DEVICE_ID")
        or os.environ.get("MLX_MACBOOK_ID")
        or socket.gethostname()
    )
    ap.add_argument(
        "--node-name",
        default=device_id,
    )
    ap.add_argument("--listen", default=os.environ.get("MLX_HW_LISTEN", LISTEN_DEFAULT))
    ap.add_argument(
        "--interval",
        type=float,
        default=float(os.environ.get("MLX_HW_INTERVAL", SAMPLE_INTERVAL_DEFAULT)),
    )
    ap.add_argument("--worker-match", default="mlx_lm.server")
    ap.add_argument(
        "--net-iface",
        default=os.environ.get("MLX_NET_IFACE", ""),
        help="interface to monitor (default: default-route interface)",
    )
    ap.add_argument(
        "--container-stats",
        action="store_true",
        default=os.environ.get("ENABLE_CONTAINER_STATS", "false").lower() == "true",
        help="include podman container CPU/RSS (default: off)",
    )
    ap.add_argument(
        "--gpu-interval",
        type=float,
        default=float(
            os.environ.get("MLX_HW_GPU_INTERVAL", str(GPU_SAMPLE_INTERVAL_DEFAULT))
        ),
    )
    args = ap.parse_args()

    NODE = args.node_name
    setup_logging(service="mlx-hw-telemetry", macbook=NODE)

    LISTEN = parse_listen_addr(args.listen, default_host="127.0.0.1", default_port=9102)
    SAMPLE_INTERVAL = max(1.0, args.interval)
    _PROC_MATCH = args.worker_match
    _NET_IFACE = args.net_iface or ""
    _CONTAINER_STATS = bool(args.container_stats)
    _parse_page_size()
    if sys.platform == "darwin":
        _MEM_TOTAL = int(_sysctl("hw.memsize", "0") or 0)
    else:
        _MEM_TOTAL = 0
        _mem_info()

    stop_event = threading.Event()

    def _loop():
        while not stop_event.is_set():
            try:
                _sample()
            except Exception as exc:
                error(f"sample error: {exc!r}", component="hw")
            stop_event.wait(SAMPLE_INTERVAL)

    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_gpu_sampler_loop, args=(args.gpu_interval, stop_event),
                     daemon=True).start()

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.address_family = socket.AF_INET6 if ":" in LISTEN[0] else socket.AF_INET
    server = ThreadingHTTPServer(LISTEN, Handler)
    info(f"node={NODE} listening on {LISTEN[0]}:{LISTEN[1]}",
         component="hw")

    def _shutdown(signum, frame):
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
