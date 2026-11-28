#!/usr/bin/env python3
"""
mlx_topology_agent.py
=====================
Edge Topology Discovery Agent for MLX Inference Fleet.

Runs periodically alongside the edge monitoring bundle to discover:
  - Local process and port bindings (mlx_proxy, mlx_engine, exporters)
  - Active listening sockets and wildcard security audits
  - Local vmagent scrape targets, errors, queues, and remote-write status
  - Peer reachability and interconnect mesh topology
Normalizes this into a TopologySnapshot and publishes it to the Central Topology API.
Optionally exposes local Prometheus metrics for scraping.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Ensure exporters dir is on sys.path
EXPORTERS_DIR = Path(__file__).resolve().parent
if str(EXPORTERS_DIR) not in sys.path:
    sys.path.insert(0, str(EXPORTERS_DIR))

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from mlx_logging import debug, error, info, setup_logging

from cluster.mlx_monitoring_bundle.topology.discovery import EdgeTopologyDiscovery
from cluster.mlx_monitoring_bundle.topology.models import TopologySnapshot

# Prometheus metrics
G_AGENT_UP = Gauge("mlx_topology_agent_up", "Whether the edge topology agent is operational")
C_SNAPSHOTS_TOTAL = Counter("mlx_topology_snapshots_total", "Total snapshots collected")
C_SNAPSHOTS_SUCCESS = Counter("mlx_topology_snapshots_success_total", "Total snapshots successfully published")
C_SNAPSHOTS_FAILED = Counter("mlx_topology_snapshots_failed_total", "Total snapshots failed to publish")
G_LAST_SNAPSHOT_TS = Gauge("mlx_topology_last_snapshot_timestamp_seconds", "Timestamp of last snapshot attempt")
G_DISCOVERED_NODES = Gauge("mlx_topology_agent_discovered_nodes", "Number of nodes discovered in last snapshot")
G_DISCOVERED_EDGES = Gauge("mlx_topology_agent_discovered_edges", "Number of edges discovered in last snapshot")
G_DISCREPANCIES = Gauge("mlx_topology_agent_discrepancies", "Number of discrepancies detected in last snapshot")


class TopologyMetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path in ("/health", "/api/v1/health"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = b'{"status":"HEALTHY","service":"mlx-topology-agent"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/metrics", "/metrics/"):
            data = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()


class EdgeTopologyAgentDaemon:
    def __init__(
        self,
        device_id: str,
        device_ip: Optional[str] = None,
        central_url: str = "http://127.0.0.1:8089",
        vmagent_url: str = "http://127.0.0.1:8429",
        interval: float = 30.0,
        listen_addr: Optional[str] = "127.0.0.1:9110",
        inventory_path: Optional[str | Path] = None,
    ):
        self.device_id = device_id
        self.device_ip = device_ip
        self.central_url = central_url.rstrip("/")
        self.vmagent_url = vmagent_url.rstrip("/")
        self.interval = max(2.0, interval)
        self.listen_addr = listen_addr
        self.inventory_path = inventory_path

        self.discovery = EdgeTopologyDiscovery(
            device_id=self.device_id,
            device_ip=self.device_ip,
            components_inventory_path=self.inventory_path,
            vmagent_url=self.vmagent_url,
            central_url=self.central_url,
        )
        self.server: Optional[ThreadingHTTPServer] = None
        self.stop_event = threading.Event()
        self.last_snapshot: Optional[TopologySnapshot] = None

    def start_http_server(self) -> None:
        if not self.listen_addr:
            return
        host, port_str = self.listen_addr.rsplit(":", 1)
        port = int(port_str)
        self.server = ThreadingHTTPServer((host, port), TopologyMetricsHandler)
        t = threading.Thread(target=self.server.serve_forever, daemon=True, name="topology-http")
        t.start()
        info(f"Topology agent metrics listening on http://{self.listen_addr}/metrics", component="topology-agent")

    def stop(self) -> None:
        self.stop_event.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        G_AGENT_UP.set(0)

    def run_once(self) -> bool:
        G_AGENT_UP.set(1)
        C_SNAPSHOTS_TOTAL.inc()
        G_LAST_SNAPSHOT_TS.set(time.time())
        try:
            snapshot = self.discovery.discover()
            self.last_snapshot = snapshot
            G_DISCOVERED_NODES.set(len(snapshot.nodes))
            G_DISCOVERED_EDGES.set(len(snapshot.edges))
            G_DISCREPANCIES.set(len(snapshot.discrepancies))

            ok = self.discovery.publish_snapshot(snapshot)
            if ok:
                C_SNAPSHOTS_SUCCESS.inc()
                debug(f"Snapshot published successfully to {self.central_url} ({len(snapshot.nodes)} nodes, {len(snapshot.edges)} edges)", component="topology-agent")
            else:
                C_SNAPSHOTS_FAILED.inc()
                debug(f"Failed to publish snapshot to {self.central_url} (HTTP error or endpoint unreachable)", component="topology-agent")
            return ok
        except Exception as exc:
            C_SNAPSHOTS_FAILED.inc()
            error(f"Error during topology discovery: {exc!r}", component="topology-agent")
            return False

    def run_forever(self) -> None:
        self.start_http_server()
        info(f"Starting edge topology discovery agent for device={self.device_id}, central={self.central_url}, interval={self.interval}s", component="topology-agent")

        while not self.stop_event.is_set():
            self.run_once()
            if self.stop_event.wait(self.interval):
                break
        info("Edge topology discovery agent stopped", component="topology-agent")


def main() -> None:
    parser = argparse.ArgumentParser(description="MLX Edge Topology Discovery Agent")
    parser.add_argument("--device-id", default=os.environ.get("MLX_DEVICE_ID", os.environ.get("MLX_MACBOOK_ID", "node-01")))
    parser.add_argument("--device-ip", default=os.environ.get("MLX_DEVICE_IP", ""))
    parser.add_argument("--central-url", default=os.environ.get("CENTRAL_TOPOLOGY_URL", "http://127.0.0.1:8089"))
    parser.add_argument("--vmagent-url", default=os.environ.get("VMAGENT_URL", "http://127.0.0.1:8429"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("TOPOLOGY_INTERVAL", "30.0")))
    parser.add_argument("--listen", default=os.environ.get("TOPOLOGY_LISTEN", "127.0.0.1:9110"))
    parser.add_argument("--components-inventory", default=os.environ.get("COMPONENTS_INVENTORY", None))
    args = parser.parse_args()

    setup_logging(service="mlx-topology-agent", macbook=args.device_id)

    daemon = EdgeTopologyAgentDaemon(
        device_id=args.device_id,
        device_ip=args.device_ip or None,
        central_url=args.central_url,
        vmagent_url=args.vmagent_url,
        interval=args.interval,
        listen_addr=args.listen if args.listen not in ("", "0", "false") else None,
        inventory_path=args.components_inventory,
    )

    def _sig_handler(sig, frame):
        daemon.stop()

    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    daemon.run_forever()


if __name__ == "__main__":
    main()
