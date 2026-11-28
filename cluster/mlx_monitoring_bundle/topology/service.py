"""Central Topology API Service.

Provides a REST API and Prometheus metrics exporter for cluster topology,
device heartbeats, Grafana Node Graph integration, and reconciliation.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import threading
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .models import (
    HealthStatus,
    NodeRole,
    SignalType,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
)
from .reconciler import CentralTopologyReconciler
from .storage import SQLiteTopologyStorage, TopologyStorage

logger = logging.getLogger("mlx.topology.service")


class TopologyRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Central Topology Service."""

    storage: TopologyStorage
    reconciler: CentralTopologyReconciler

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress stdout access logs unless error or verbose."""
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        if path in ("/api/v1/topology/health", "/health"):
            self._send_json({"status": "HEALTHY", "service": "mlx-topology-service"})

        elif path == "/api/v1/topology/devices":
            devices = self.storage.get_all_devices()
            self._send_json({"devices": devices, "total": len(devices)})

        elif path == "/api/v1/topology/nodes":
            device_id = qs.get("device_id", [None])[0]
            nodes = [n.model_dump() for n in self.storage.get_nodes(device_id)]
            self._send_json({"nodes": nodes, "total": len(nodes)})

        elif path == "/api/v1/topology/edges":
            device_id = qs.get("device_id", [None])[0]
            edges = [e.model_dump() for e in self.storage.get_edges(device_id)]
            self._send_json({"edges": edges, "total": len(edges)})

        elif path == "/api/v1/topology/graph":
            device_id = qs.get("device_id", [None])[0]
            scope = qs.get("scope", ["device"])[0]

            if scope == "fleet":
                # Fleet aggregate: One node per device, plus central systems
                nodes_data, edges_data = self._build_fleet_overview_graph()
            else:
                nodes = self.storage.get_nodes(device_id)
                edges = self.storage.get_edges(device_id)
                nodes_data = [n.model_dump() for n in nodes]
                edges_data = [e.model_dump() for e in edges]

            self._send_json({
                "nodes": nodes_data,
                "edges": edges_data,
                "scope": scope,
                "device_id": device_id,
            })

        elif path == "/api/v1/topology/grafana/nodes":
            # Grafana Node Graph formatted nodes dataframe
            device_id = qs.get("device_id", [None])[0]
            scope = qs.get("scope", ["device"])[0]
            if scope == "fleet":
                nodes_raw, _ = self._build_fleet_overview_graph()
                nodes_gf = [n for n in nodes_raw]
            else:
                nodes = self.storage.get_nodes(device_id)
                nodes_gf = [n.to_grafana_node() for n in nodes]
            self._send_json(nodes_gf)

        elif path == "/api/v1/topology/grafana/edges":
            # Grafana Node Graph formatted edges dataframe
            device_id = qs.get("device_id", [None])[0]
            scope = qs.get("scope", ["device"])[0]
            if scope == "fleet":
                _, edges_raw = self._build_fleet_overview_graph()
                edges_gf = [e for e in edges_raw]
            else:
                edges = self.storage.get_edges(device_id)
                edges_gf = [e.to_grafana_edge() for e in edges]
            self._send_json(edges_gf)

        elif path == "/api/v1/topology/discrepancies":
            device_id = qs.get("device_id", [None])[0]
            discs = [d.model_dump() for d in self.storage.get_discrepancies(device_id)]
            self._send_json({"discrepancies": discs, "total": len(discs)})

        elif path == "/api/v1/topology/reconcile":
            report = self.reconciler.reconcile_fleet()
            self._send_json(report)

        elif path in ("/metrics", "/api/v1/topology/metrics"):
            # Prometheus metrics endpoint
            metrics_text = self._generate_prometheus_metrics()
            self._send_text(metrics_text, content_type="text/plain; version=0.0.4")

        else:
            self._send_json({"error": "Endpoint not found", "path": path}, status=404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/api/v1/topology/snapshot":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length).decode("utf-8")
                snapshot_dict = json.loads(body)
                snapshot = TopologySnapshot(**snapshot_dict)

                # Persist snapshot
                self.storage.save_snapshot(snapshot)

                self._send_json({
                    "status": "ACCEPTED",
                    "device_id": snapshot.device_id,
                    "node_count": len(snapshot.nodes),
                    "edge_count": len(snapshot.edges),
                    "discrepancy_count": len(snapshot.discrepancies),
                }, status=202)
            except Exception as exc:
                self._send_json({"error": "Failed to parse snapshot", "details": str(exc)}, status=400)
        else:
            self._send_json({"error": "Endpoint not found", "path": path}, status=404)

    def _build_fleet_overview_graph(self) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Aggregate all edge devices into a high-level fleet overview graph."""
        devices = self.storage.get_all_devices()
        nodes: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []

        # Central Hub Nodes
        nodes.append({
            "id": "central:victoria_metrics",
            "title": "VictoriaMetrics TSDB",
            "subTitle": "Central Repository (:8428)",
            "mainStat": "Storage",
            "secondaryStat": "TSDB Active",
            "color": "#0284c7",
            "role": "tsdb",
            "health": "HEALTHY",
            "device": "central",
        })
        nodes.append({
            "id": "central:grafana",
            "title": "Central Grafana",
            "subTitle": "Operations UI (:3000)",
            "mainStat": "Dashboards",
            "secondaryStat": "19 provisioned",
            "color": "#4f46e5",
            "role": "visuals",
            "health": "HEALTHY",
            "device": "central",
        })

        # Central VM -> Grafana edge
        edges.append({
            "id": "central:victoria_metrics->central:grafana:metrics",
            "source": "central:victoria_metrics",
            "target": "central:grafana",
            "mainStat": "PromQL",
            "secondaryStat": "Queries",
            "color": "#0284c7",
            "signalType": "metrics",
            "status": "HEALTHY",
        })

        # Add Device Aggregate Nodes
        for dev in devices:
            dev_id = dev["device_id"]
            if dev_id == "central":
                continue

            status = dev["status"]
            color = HealthStatus(status).color

            dev_node_id = f"fleet:{dev_id}"
            nodes.append({
                "id": dev_node_id,
                "title": f"Device {dev_id}",
                "subTitle": f"{dev['ip']} ({dev['deployment_type']})",
                "mainStat": status,
                "secondaryStat": f"Last: {dev['last_heartbeat'][-8:]}",
                "color": color,
                "role": "device_aggregate",
                "health": status,
                "device": dev_id,
                "detail__ip": dev["ip"],
                "detail__status": status,
                "detail__last_heartbeat": dev["last_heartbeat"],
            })

            # Edge: Device -> Central TSDB
            edges.append({
                "id": f"{dev_node_id}->central:victoria_metrics:metrics",
                "source": dev_node_id,
                "target": "central:victoria_metrics",
                "mainStat": "remote_write",
                "secondaryStat": status,
                "color": color if status != "HEALTHY" else "#10b981",
                "signalType": "metrics",
                "status": status,
            })

        return nodes, edges

    def _generate_prometheus_metrics(self) -> str:
        """Render Prometheus metrics for VictoriaMetrics scraping."""
        return render_prometheus_metrics(self.storage)


def render_prometheus_metrics(storage: TopologyStorage) -> str:
    """Render Prometheus metrics for VictoriaMetrics scraping and direct TSDB import."""
    devices = storage.get_all_devices()
    nodes = storage.get_nodes()
    edges = storage.get_edges()
    discs = storage.get_discrepancies()

    lines = [
        "# HELP mlx_topology_devices_total Total registered fleet devices by health status",
        "# TYPE mlx_topology_devices_total gauge",
    ]
    status_counts: Dict[str, int] = {}
    for d in devices:
        st = d["status"]
        status_counts[st] = status_counts.get(st, 0) + 1

    for st in HealthStatus:
        count = status_counts.get(st.value, 0)
        lines.append(f'mlx_topology_devices_total{{status="{st.value}"}} {count}')

    lines.extend([
        "# HELP mlx_topology_nodes_total Total topology nodes by role and health",
        "# TYPE mlx_topology_nodes_total gauge",
    ])
    for n in nodes:
        lines.append(f'mlx_topology_nodes_total{{role="{n.role.value}",health="{n.health.value}"}} 1')

    lines.extend([
        "# HELP mlx_topology_edges_total Total communication edges by signal and status",
        "# TYPE mlx_topology_edges_total gauge",
    ])
    for e in edges:
        lines.append(f'mlx_topology_edges_total{{signal="{e.signal_type.value}",status="{e.status.value}"}} 1')

    lines.extend([
        "# HELP mlx_topology_discrepancies_total Active reconciliation discrepancies",
        "# TYPE mlx_topology_discrepancies_total gauge",
    ])
    for d in discs:
        lines.append(f'mlx_topology_discrepancies_total{{device="{d.device_id}",severity="{d.severity.value}",issue="{d.issue_type}"}} 1')

    lines.extend([
        "# HELP mlx_topology_node_info Node graph info metric for Grafana",
        "# TYPE mlx_topology_node_info gauge",
    ])
    for n in nodes:
        lines.append(f'mlx_topology_node_info{{id="{n.id}",name="{n.name}",device="{n.device_id}",role="{n.role.value}",health="{n.health.value}",color="{n.health.color}"}} 1')

    lines.extend([
        "# HELP mlx_topology_edge_info Edge graph info metric for Grafana",
        "# TYPE mlx_topology_edge_info gauge",
    ])
    for e in edges:
        lines.append(f'mlx_topology_edge_info{{id="{e.id}",source="{e.source_id}",target="{e.target_id}",signal="{e.signal_type.value}",status="{e.status.value}"}} 1')

    lines.append("")
    return "\n".join(lines)


class TopologyAPIService:
    """Central Topology Service Daemon."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8089,
        db_path: str = "topology.db",
        vm_url: str = "http://192.168.1.64:8428",
        reconcile_interval: float = 15.0,
        central_ip: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.storage = SQLiteTopologyStorage(db_path=db_path, central_ip=central_ip)
        self.reconciler = CentralTopologyReconciler(storage=self.storage, vm_query_url=vm_url)
        self.reconcile_interval = max(2.0, reconcile_interval)
        self.server: Optional[ThreadingHTTPServer] = None
        self._stop_event = threading.Event()
        self._reconcile_thread: Optional[threading.Thread] = None

    def export_metrics_to_tsdb(self) -> bool:
        """Push rendered Prometheus metrics to VictoriaMetrics TSDB via /api/v1/import/prometheus."""
        url = f"{self.reconciler.vm_query_url}/api/v1/import/prometheus"
        body = render_prometheus_metrics(self.storage).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "text/plain; charset=utf-8", "User-Agent": "mlx-topology-service/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status in (200, 204)
        except Exception as exc:
            logger.debug(f"Metrics export to VictoriaMetrics TSDB skipped: {exc}")
            return False

    def _reconcile_loop(self) -> None:
        """Periodic background reconciliation of fleet devices and TSDB metric export."""
        while not self._stop_event.is_set():
            try:
                self.reconciler.reconcile_fleet()
                self.export_metrics_to_tsdb()
            except Exception as exc:
                logger.error(f"Error in topology background reconciliation loop: {exc}")
            self._stop_event.wait(self.reconcile_interval)

    def start(self) -> None:
        handler_cls = TopologyRequestHandler
        handler_cls.storage = self.storage
        handler_cls.reconciler = self.reconciler

        self._stop_event.clear()
        self._reconcile_thread = threading.Thread(
            target=self._reconcile_loop,
            daemon=True,
            name="topology-reconciler",
        )
        self._reconcile_thread.start()

        self.server = ThreadingHTTPServer((self.host, self.port), handler_cls)
        logger.info(f"MLX Topology Service listening on http://{self.host}:{self.port}")
        try:
            self.server.serve_forever()
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_event.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self._reconcile_thread and self._reconcile_thread.is_alive():
            self._reconcile_thread.join(timeout=2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="MLX Central Topology Service")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP listen host")
    parser.add_argument("--port", type=int, default=8089, help="HTTP listen port")
    parser.add_argument("--db-path", default="topology.db", help="Path to SQLite topology database")
    parser.add_argument("--vm-url", default=os.environ.get("VM_URL", "http://192.168.1.64:8428"), help="VictoriaMetrics TSDB base URL")
    parser.add_argument("--central-ip", default=os.environ.get("CENTRAL_IP", "192.168.1.64"), help="Central node IP address")
    parser.add_argument("--reconcile-interval", type=float, default=15.0, help="Reconciliation & TSDB export interval in seconds")
    args = parser.parse_args()

    svc = TopologyAPIService(
        host=args.host,
        port=args.port,
        db_path=args.db_path,
        vm_url=args.vm_url,
        reconcile_interval=args.reconcile_interval,
        central_ip=args.central_ip,
    )
    try:
        svc.start()
    except KeyboardInterrupt:
        svc.stop()


if __name__ == "__main__":
    main()
