#!/usr/bin/env python3
"""
mlx_hw_telemetry.py
===================
Per-node hardware telemetry exporter for the MLX cluster.

Runs on EACH Mac mini (rank 0 and rank 1) and exposes the host metrics that
back the "hardware → LLM" monitoring story:

  * CPU load / active CPU count
  * memory pressure + used/total
  * disk used/free (the model cache lives on the boot volume)
  * process telemetry for the mlx_lm.server worker (CPU%, RSS)
  * CPU temperature (best effort: powermetrics via sudo, else NaN)
  * a per-node availability gauge and uptime

Metrics are served in Prometheus text format on :9102/metrics. This scrape is
the single source of truth for VictoriaMetrics (the hardware agent does not
push OTLP metrics, so `mlx_hw_*` series exist exactly once in VM).

Usage:
  mlx_hw_telemetry.py --node-name rank0 --listen 0.0.0.0:9102
  mlx_hw_telemetry.py --node-name rank1 --listen 0.0.0.0:9102
"""

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

NODE = "unknown"
LISTEN = ("0.0.0.0", 9102)
SAMPLE_INTERVAL = 5.0

# --------------------------------------------------------------------------
# Prometheus gauges
# --------------------------------------------------------------------------
G_UP = Gauge("mlx_hw_up", "1 if the node telemetry agent is running", ["node"])
G_UPTIME = Gauge("mlx_hw_uptime_seconds", "Host uptime in seconds", ["node"])
G_LOAD1 = Gauge("mlx_hw_load1", "1-minute system load average", ["node"])
G_LOAD5 = Gauge("mlx_hw_load5", "5-minute system load average", ["node"])
G_LOAD15 = Gauge("mlx_hw_load15", "15-minute system load average", ["node"])
G_CPU_COUNT = Gauge("mlx_hw_cpu_count", "Logical CPU count", ["node"])
G_CPU_TEMP = Gauge("mlx_hw_cpu_temp_celsius", "CPU temperature (NaN if unavailable)", ["node"])
G_MEM_TOTAL = Gauge("mlx_hw_mem_total_bytes", "Total physical memory", ["node"])
G_MEM_USED = Gauge("mlx_hw_mem_used_bytes", "Used physical memory", ["node"])
G_MEM_PRESSURE = Gauge("mlx_hw_mem_pressure", "vm.page_free_count pressure heuristic", ["node"])
G_DISK_TOTAL = Gauge("mlx_hw_disk_total_bytes", "Root volume total size", ["node"])
G_DISK_USED = Gauge("mlx_hw_disk_used_bytes", "Root volume used size", ["node"])
G_WORKER_CPU = Gauge("mlx_worker_cpu_percent", "mlx_lm.server CPU %", ["node"])
G_WORKER_RSS = Gauge("mlx_worker_rss_bytes", "mlx_lm.server resident set size", ["node"])
G_GPU_UTIL = Gauge("mlx_hw_gpu_utilization_percent", "GPU active residency % (NaN if unavailable)", ["node"])
G_GPU_FREQ = Gauge("mlx_hw_gpu_frequency_mhz", "GPU HW active frequency (NaN if unavailable)", ["node"])
G_GPU_POWER = Gauge("mlx_hw_gpu_power_milliwatts", "GPU power draw (NaN if unavailable)", ["node"])
G_PKG_POWER = Gauge("mlx_hw_package_power_milliwatts", "Combined CPU+GPU(+ANE) package power (NaN if unavailable)", ["node"])
G_CPU_POWER = Gauge("mlx_hw_cpu_power_milliwatts", "CPU cluster power (NaN if unavailable)", ["node"])
G_ANE_POWER = Gauge("mlx_hw_ane_power_milliwatts", "Neural Engine power (NaN if unavailable)", ["node"])
G_THERMAL = Gauge("mlx_hw_thermal_pressure", "Thermal throttling pressure 0..1 (pmset -g therm)", ["node"])
G_GPU_MEM_USED = Gauge("mlx_hw_gpu_mem_used_bytes", "GPU memory in use (IOKit In use system memory)", ["node"])
G_GPU_MEM_ALLOC = Gauge("mlx_hw_gpu_mem_alloc_bytes", "GPU memory allocated (IOKit Alloc system memory)", ["node"])
G_GPU_MEM_TOTAL = Gauge("mlx_hw_gpu_mem_total_bytes", "Total unified GPU memory (IORegistry VRAM,totalMB)", ["node"])
G_GPU_UTIL_IOKIT = Gauge("mlx_hw_gpu_iokit_util_percent", "GPU activity from IOKit Device Utilization %", ["node"])
G_PROC_CPU = Gauge(
    "mlx_proc_cpu_percent",
    "Per-stack-component CPU % (sum over matching PIDs)",
    ["node", "component"],
)
G_PROC_RSS = Gauge(
    "mlx_proc_rss_bytes",
    "Per-stack-component resident set size (sum over matching PIDs)",
    ["node", "component"],
)
C_NET_RX = Counter(
    "mlx_net_rx_bytes_total",
    "Cumulative bytes received on the ring interconnect interface",
    ["node", "iface"],
)
C_NET_TX = Counter(
    "mlx_net_tx_bytes_total",
    "Cumulative bytes sent on the ring interconnect interface",
    ["node", "iface"],
)
C_NET_RX_ERR = Counter(
    "mlx_net_rx_errors_total",
    "Cumulative receive errors on the ring interconnect interface",
    ["node", "iface"],
)
C_NET_TX_ERR = Counter(
    "mlx_net_tx_errors_total",
    "Cumulative transmit errors on the ring interconnect interface",
    ["node", "iface"],
)

# component name -> substring to match in the ps command line (host processes)
_PROC_COMPONENTS = {
    "mlx_lm_server": "mlx_lm.server",
    "mlx_proxy": "mlx_metrics_proxy",
    "kv_agent": "mlx_kv_cache_agent",
    "log_tailer": "mlx_server_log_tailer",
    "supervisor": "mlx_server_supervisor",
}

# component name -> podman container name (the rest of the stack is
# containerized, so host ps cannot see it; podman stats can)
_CONTAINER_COMPONENTS = {
    "otel_collector": "otel-collector",
    "victoria_metrics": "victoria-metrics",
    "grafana": "grafana",
    "alertmanager": "alertmanager",
    "vmalert": "vmalert",
}

_PAGE_SIZE = 0
_MEM_TOTAL = 0
_PROC_MATCH = None

# Cache for the slow powermetrics GPU/power sampler (runs on its own cadence).
_GPU_LOCK = threading.Lock()
_GPU_STATS = {"util": float("nan"), "freq": float("nan"),
              "gpu_power": float("nan"), "pkg_power": float("nan"),
              "cpu_power": float("nan"), "ane_power": float("nan"),
              "gpu_mem_used": float("nan"), "gpu_mem_alloc": float("nan"),
              "gpu_mem_total": float("nan"), "gpu_util_iokit": float("nan")}
GPU_SAMPLE_INTERVAL = 15.0


def _sysctl(name, default=None):
    try:
        out = subprocess.run(
            ["sysctl", "-n", name], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        return out or default
    except Exception:
        return default


def _parse_page_size():
    global _PAGE_SIZE
    try:
        import sysconfig

        _PAGE_SIZE = sysconfig.get_config_var("PAGESIZE") or os.sysconf("SC_PAGE_SIZE")
    except Exception:
        _PAGE_SIZE = 4096


def _mem_info():
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5
        ).stdout
        free = 0
        inactive = 0
        spec = 0
        for line in out.splitlines():
            m = re.search(r"Pages free:\s+(\d+)", line)
            if m:
                free = int(m.group(1))
                continue
            m = re.search(r"Pages inactive:\s+(\d+)", line)
            if m:
                inactive = int(m.group(1))
                continue
            m = re.search(r"Pages speculative:\s+(\d+)", line)
            if m:
                spec = int(m.group(1))
        free_pages = free + inactive + spec
        used = _MEM_TOTAL - free_pages * _PAGE_SIZE
        return used, free_pages
    except Exception:
        return _MEM_TOTAL, 0


def _cpu_temp():
    """Best-effort CPU temperature. Returns float or NaN."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "powermetrics", "-n", "1", "--samplers", "smc", "-f", "text"],
            capture_output=True,
            text=True,
            timeout=6,
        ).stdout
        for line in out.splitlines():
            m = re.search(r"(?:CPU die temperature|package.*temperature)\s*:\s*([\d.]+)", line, re.I)
            if m:
                return float(m.group(1))
        return float("nan")
    except Exception:
        return float("nan")


def _gpu_iokit_stats():
    """GPU memory + activity via IOKit (ioreg, no root).
    Returns (used, alloc, total, util) as floats; NaN when unavailable."""
    used = alloc = total = util = float("nan")
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOAccelerator", "-r", "-d", "2"],
            capture_output=True,
            text=True,
            timeout=5,
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
            # Unified memory on Apple Silicon: cap = physical RAM.
            mem = _sysctl("hw.memsize", "")
            if mem:
                total = float(mem)
        m = re.search(r'"Device Utilization %"=(\d+)', out)
        if m:
            util = float(m.group(1))
    except Exception:
        pass
    return used, alloc, total, util


def _gpu_power_stats():
    """Best-effort GPU/CPU/ANE/package power via powermetrics + IOKit GPU
    memory. Updates the cache."""
    global _GPU_STATS
    util = freq = gpu_power = pkg_power = cpu_power = ane_power = float("nan")
    try:
        out = subprocess.run(
            [
                "sudo", "-n", "powermetrics", "-n", "1",
                "--samplers", "cpu_power,gpu_power", "-i", "1000", "-f", "text",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        ).stdout
        for line in out.splitlines():
            m = re.search(r"GPU HW active residency:\s*([\d.]+)%", line)
            if m:
                util = float(m.group(1))
            m = re.search(r"GPU HW active frequency:\s*([\d.]+)\s*MHz", line)
            if m:
                freq = float(m.group(1))
            m = re.search(r"Combined Power \(CPU \+ GPU \+ ANE\):\s*([\d.]+)\s*mW", line, re.I)
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
    except Exception:
        pass
    gpu_mem_used, gpu_mem_alloc, gpu_mem_total, gpu_util_iokit = _gpu_iokit_stats()
    with _GPU_LOCK:
        _GPU_STATS = {
            "util": util, "freq": freq,
            "gpu_power": gpu_power, "pkg_power": pkg_power,
            "cpu_power": cpu_power, "ane_power": ane_power,
            "gpu_mem_used": gpu_mem_used, "gpu_mem_alloc": gpu_mem_alloc,
            "gpu_mem_total": gpu_mem_total, "gpu_util_iokit": gpu_util_iokit,
        }


def _thermal_pressure():
    """Thermal throttling pressure 0..1 from `pmset -g therm` (no sudo).
    0 = none; 1 = fully scheduler-limited. NaN when unavailable."""
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
    except Exception:
        return float("nan")


def _gpu_sampler_loop():
    """Slow-cadence powermetrics sampler; updates _GPU_STATS for _sample()."""
    while True:
        try:
            _gpu_power_stats()
        except Exception as exc:
            sys.stderr.write(f"[hw] gpu sampler error: {exc!r}\n")
        time.sleep(GPU_SAMPLE_INTERVAL)


def _worker_stats():
    cpu = 0.0
    rss = 0
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,pcpu=,rss=,command="], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            if _PROC_MATCH and _PROC_MATCH not in line:
                continue
            if "mlx_metrics_proxy" in line or "mlx_hw_telemetry" in line:
                continue
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                rss += int(parts[2]) * 1024
                cpu += float(parts[1])
            except ValueError:
                continue
    except Exception:
        pass
    return cpu, rss


def _proc_stats():
    """Per-stack-component (cpu%, rss) from one ps sweep, keyed by component."""
    stats = {name: (0.0, 0) for name in _PROC_COMPONENTS}
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,pcpu=,rss=,command="], capture_output=True, text=True, timeout=5
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
            for name, needle in _PROC_COMPONENTS.items():
                if needle in cmd:
                    c, r = stats[name]
                    stats[name] = (c + cpu, r + rss)
    except Exception:
        pass
    return stats


def _container_stats():
    """Per-container (cpu%, rss bytes) via `podman stats --no-stream`, keyed by
    component name. Rootless podman on the same host; empty on failure."""
    stats = {}
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
            for comp, ctn in _CONTAINER_COMPONENTS.items():
                if ctn == name:
                    stats[comp] = (float(cpu_pct), _mem_usage_bytes(parts[2]))
                    break
    except Exception:
        pass
    return stats


def _mem_usage_bytes(usage):
    """'505.6MB' / '1.23GiB' -> bytes."""
    m = re.match(r"([\d.]+)([KMGTP]?)(i?B)", usage)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "K":
        num *= 1024
    elif unit == "M":
        num *= 1024 ** 2
    elif unit == "G":
        num *= 1024 ** 3
    elif unit == "T":
        num *= 1024 ** 4
    return int(num)


def _ring_iface():
    """Auto-detect the ring interconnect interface: first iface with an IPv4
    in 10.0.0.0/24. Returns its name or None."""
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
            m = re.search(r"inet\s+10\.0\.0\.\d+\s", line)
            if m and cur:
                return cur
    except Exception:
        pass
    return None


def _net_stats(iface):
    """Cumulative (rx_bytes, tx_bytes, rx_err, tx_err) for iface from
    `netstat -ib` (the link row). Returns None when unavailable."""
    if not iface:
        return None
    try:
        out = subprocess.run(
            ["netstat", "-ib", "-I", iface], capture_output=True, text=True, timeout=3
        ).stdout
        for line in out.splitlines():
            fields = line.split()
            if len(fields) < 10 or fields[0] != iface:
                continue
            # link row: Name Mtu <Link#> Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
            if fields[2].startswith("<Link"):
                try:
                    return (
                        int(fields[6]),  # Ibytes
                        int(fields[9]),  # Obytes
                        int(fields[5]),  # Ierrs
                        int(fields[8]),  # Oerrs
                    )
                except (ValueError, IndexError):
                    return None
    except Exception:
        pass
    return None


_NET_LAST = {}


def _ring_net_since(counters):
    """Delta-inc the ring net counters so VM can derive rate(); establishes a
    baseline on the first sample so a fresh exporter doesn't fake a huge jump."""
    iface = _ring_iface()
    rx, tx, rx_err, tx_err = counters
    prev = _NET_LAST.get(NODE, {}).get(iface)
    if prev is None:
        _NET_LAST.setdefault(NODE, {})[iface] = (rx, tx, rx_err, tx_err)
        return
    prx, ptx, perr, oerr = prev
    if rx >= prx:
        C_NET_RX.labels(NODE, iface).inc(rx - prx)
    if tx >= ptx:
        C_NET_TX.labels(NODE, iface).inc(tx - ptx)
    if rx_err >= perr:
        C_NET_RX_ERR.labels(NODE, iface).inc(rx_err - perr)
    if tx_err >= oerr:
        C_NET_TX_ERR.labels(NODE, iface).inc(tx_err - oerr)
    _NET_LAST[NODE][iface] = (rx, tx, rx_err, tx_err)


def _sample():
    load1, load5, load15 = os.getloadavg()
    used, free_pages = _mem_info()
    disk = shutil.disk_usage("/")

    G_UP.labels(NODE).set(1)
    boot = _sysctl("kern.boottime", "")
    m = re.search(r"sec\s*=\s*(\d+)", boot)
    if m:
        G_UPTIME.labels(NODE).set(max(0.0, time.time() - float(m.group(1))))
    else:
        G_UPTIME.labels(NODE).set(0)
    G_LOAD1.labels(NODE).set(load1)
    G_LOAD5.labels(NODE).set(load5)
    G_LOAD15.labels(NODE).set(load15)
    G_CPU_COUNT.labels(NODE).set(os.cpu_count() or 0)
    G_CPU_TEMP.labels(NODE).set(_cpu_temp())
    with _GPU_LOCK:
        stats = dict(_GPU_STATS)
    G_GPU_UTIL.labels(NODE).set(stats["util"])
    G_GPU_FREQ.labels(NODE).set(stats["freq"])
    G_GPU_POWER.labels(NODE).set(stats["gpu_power"])
    G_PKG_POWER.labels(NODE).set(stats["pkg_power"])
    G_CPU_POWER.labels(NODE).set(stats["cpu_power"])
    G_ANE_POWER.labels(NODE).set(stats["ane_power"])
    G_THERMAL.labels(NODE).set(_thermal_pressure())
    G_GPU_MEM_USED.labels(NODE).set(stats["gpu_mem_used"])
    G_GPU_MEM_ALLOC.labels(NODE).set(stats["gpu_mem_alloc"])
    G_GPU_MEM_TOTAL.labels(NODE).set(stats["gpu_mem_total"])
    G_GPU_UTIL_IOKIT.labels(NODE).set(stats["gpu_util_iokit"])
    G_MEM_TOTAL.labels(NODE).set(_MEM_TOTAL)
    G_MEM_USED.labels(NODE).set(used)
    G_MEM_PRESSURE.labels(NODE).set(free_pages)
    G_DISK_TOTAL.labels(NODE).set(disk.total)
    G_DISK_USED.labels(NODE).set(disk.used)

    wcpu, wrss = _worker_stats()
    G_WORKER_CPU.labels(NODE).set(wcpu)
    G_WORKER_RSS.labels(NODE).set(wrss)

    for name, (cpu, rss) in _proc_stats().items():
        G_PROC_CPU.labels(NODE, name).set(cpu)
        G_PROC_RSS.labels(NODE, name).set(rss)

    for name, (cpu, rss) in _container_stats().items():
        G_PROC_CPU.labels(NODE, name).set(cpu)
        G_PROC_RSS.labels(NODE, name).set(rss)

    net = _net_stats(_ring_iface())
    if net:
        _ring_net_since(net)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[hw] %s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        if self.path not in ("/", "/metrics"):
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _prom_scrape(host, port, path):
    import urllib.request

    try:
        urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=5).read()
    except Exception:
        pass


def main():
    global NODE, LISTEN, _MEM_TOTAL, _PROC_MATCH

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--node-name", default=os.environ.get("MLX_NODE_NAME", socket.gethostname()))
    ap.add_argument("--listen", default="0.0.0.0:9102")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--worker-match", default="mlx_lm.server")
    args = ap.parse_args()

    NODE = args.node_name
    LISTEN = (args.listen.rsplit(":", 1)[0], int(args.listen.rsplit(":", 1)[1]))
    SAMPLE_INTERVAL = max(1.0, args.interval)
    _PROC_MATCH = args.worker_match
    _MEM_TOTAL = int(_sysctl("hw.memsize", "0") or 0)
    _parse_page_size()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    def _loop():
        while True:
            try:
                _sample()
            except Exception as exc:
                sys.stderr.write(f"[hw] sample error: {exc!r}\n")
            time.sleep(SAMPLE_INTERVAL)

    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_gpu_sampler_loop, daemon=True).start()

    server = ThreadingHTTPServer(LISTEN, Handler)
    print(
        f"[hw] node={NODE} listening on {LISTEN[0]}:{LISTEN[1]}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
