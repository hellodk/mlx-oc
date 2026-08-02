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

Metrics are served in Prometheus text format on :9102/metrics and, when
`--otlp-endpoint` is given, also pushed as OTLP metrics to an OTLP gateway
(e.g. the in-cluster otel-gateway) where the cluster Prometheus can scrape
them via the collector's prometheus exporter.

Usage:
  mlx_hw_telemetry.py --node-name rank0 --listen 0.0.0.0:9102
  mlx_hw_telemetry.py --node-name rank1 --otlp-endpoint http://192.168.1.10:30318
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

from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST

NODE = "unknown"
LISTEN = ("0.0.0.0", 9102)
OTLP_ENDPOINT = ""
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
G_THERMAL = Gauge("mlx_hw_thermal_pressure", "Thermal throttling pressure 0..1 (pmset -g therm)", ["node"])

_PAGE_SIZE = 0
_MEM_TOTAL = 0
_PROC_MATCH = None

# Cache for the slow powermetrics GPU/power sampler (runs on its own cadence).
_GPU_LOCK = threading.Lock()
_GPU_STATS = {"util": float("nan"), "freq": float("nan"),
              "gpu_power": float("nan"), "pkg_power": float("nan")}
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


def _gpu_power_stats():
    """Best-effort GPU + package power via powermetrics. Updates the cache."""
    global _GPU_STATS
    util = freq = gpu_power = pkg_power = float("nan")
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
        in_gpu = False
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
    except Exception:
        pass
    with _GPU_LOCK:
        _GPU_STATS = {
            "util": util, "freq": freq,
            "gpu_power": gpu_power, "pkg_power": pkg_power,
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
    G_THERMAL.labels(NODE).set(_thermal_pressure())
    G_MEM_TOTAL.labels(NODE).set(_MEM_TOTAL)
    G_MEM_USED.labels(NODE).set(used)
    G_MEM_PRESSURE.labels(NODE).set(free_pages)
    G_DISK_TOTAL.labels(NODE).set(disk.total)
    G_DISK_USED.labels(NODE).set(disk.used)

    wcpu, wrss = _worker_stats()
    G_WORKER_CPU.labels(NODE).set(wcpu)
    G_WORKER_RSS.labels(NODE).set(wrss)


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


def _otel_loop():
    """Push current metric snapshot as OTLP metrics every SAMPLE_INTERVAL."""
    if not OTLP_ENDPOINT:
        return
    try:
        from opentelemetry import metrics
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME, HOST_NAME
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )

        resource = Resource.create(
            {
                SERVICE_NAME: "mlx-hw-telemetry",
                HOST_NAME: socket.gethostname(),
                "mlx.node.name": NODE,
            }
        )
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=OTLP_ENDPOINT), export_interval_millis=10000
        )
        provider = MeterProvider(metric_readers=[reader], resource=resource)
        metrics.set_meter_provider(provider)
        meter = metrics.get_meter("mlx.hw")

        load_g = meter.create_observable_gauge(
            "mlx.hw.load",
            callbacks=[
                lambda obs: [obs.observe(v, {"node": NODE}) for v in os.getloadavg()]
            ],
        )
        temp_g = meter.create_observable_gauge(
            "mlx.hw.cpu_temp_celsius",
            callbacks=[lambda obs: obs.observe(_cpu_temp(), {"node": NODE})],
        )
        used_g = meter.create_observable_gauge(
            "mlx.hw.mem_used_bytes",
            callbacks=[lambda obs: obs.observe(_mem_info()[0], {"node": NODE})],
        )
        gpu_util_g = meter.create_observable_gauge(
            "mlx.hw.gpu_utilization_percent",
            callbacks=[
                lambda obs: obs.observe(
                    _GPU_STATS["util"] if not _GPU_LOCK.locked() else float("nan"),
                    {"node": NODE},
                )
            ],
        )
        pkg_power_g = meter.create_observable_gauge(
            "mlx.hw.package_power_milliwatts",
            callbacks=[
                lambda obs: obs.observe(
                    _GPU_STATS["pkg_power"] if not _GPU_LOCK.locked() else float("nan"),
                    {"node": NODE},
                )
            ],
        )

        while True:
            time.sleep(SAMPLE_INTERVAL)
    except Exception as exc:
        print(f"[hw] OTel loop disabled: {exc!r}", flush=True)


def main():
    global NODE, LISTEN, OTLP_ENDPOINT, _MEM_TOTAL, _PROC_MATCH

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--node-name", default=os.environ.get("MLX_NODE_NAME", socket.gethostname()))
    ap.add_argument("--listen", default="0.0.0.0:9102")
    ap.add_argument(
        "--otlp-endpoint",
        default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        help="OTLP HTTP endpoint for metrics (e.g. http://192.168.1.10:30318)",
    )
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--worker-match", default="mlx_lm.server")
    args = ap.parse_args()

    NODE = args.node_name
    LISTEN = (args.listen.rsplit(":", 1)[0], int(args.listen.rsplit(":", 1)[1]))
    OTLP_ENDPOINT = args.otlp_endpoint
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
    threading.Thread(target=_otel_loop, daemon=True).start()

    server = ThreadingHTTPServer(LISTEN, Handler)
    print(
        f"[hw] node={NODE} listening on {LISTEN[0]}:{LISTEN[1]} "
        f"otlp={'on' if OTLP_ENDPOINT else 'off'}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
