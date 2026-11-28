"""Edge Topology Discovery Agent.

Inspects local processes, listening sockets, vmagent runtime telemetry,
and reconciles against desired component inventory to generate normalized
topology snapshots.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import socket
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psutil  # type: ignore
import yaml

from .models import (
    Discrepancy,
    HealthStatus,
    ListenEndpoint,
    NodeRole,
    SignalType,
    StateType,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
)
from .vmagent_inspector import VMAgentInspector, VMAgentInspectionReport


class EdgeTopologyDiscovery:
    """Discovers local runtime topology on an MLX fleet edge node."""

    def __init__(
        self,
        device_id: str = "node-01",
        device_ip: Optional[str] = None,
        components_inventory_path: Optional[str | Path] = None,
        vmagent_url: str = "http://127.0.0.1:8429",
        central_url: str = "http://127.0.0.1:8089",
    ):
        self.device_id = device_id
        self.device_ip = device_ip or self._detect_local_ip()
        self.vmagent_url = vmagent_url
        self.central_url = central_url.rstrip("/")

        # Locate components inventory
        if components_inventory_path:
            self.inventory_path = Path(components_inventory_path)
        else:
            # Default search paths
            candidates = [
                Path("cluster/mlx_monitoring_bundle/config/components.yml"),
                Path("../config/components.yml"),
                Path(__file__).parent.parent / "config" / "components.yml",
            ]
            self.inventory_path = next((p for p in candidates if p.exists()), candidates[0])

        self.inspector = VMAgentInspector(vmagent_url=self.vmagent_url)

    @staticmethod
    def _detect_local_ip() -> str:
        """Best-effort detection of local host IP."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip and not ip.startswith("127."):
                return ip
        except Exception:
            pass
        return "127.0.0.1"

    def load_desired_inventory(self) -> Dict[str, Any]:
        """Load desired component inventory if available."""
        if self.inventory_path.exists():
            try:
                with open(self.inventory_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}

    def discover(self) -> TopologySnapshot:
        """Execute full edge discovery and return normalized snapshot."""
        now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        desired_inv = self.load_desired_inventory()

        nodes: List[TopologyNode] = []
        edges: List[TopologyEdge] = []
        discrepancies: List[Discrepancy] = []

        # 1. Deep vmagent inspection
        vma_report = self.inspector.inspect()

        # 2. Gather active listening sockets and process mappings
        active_listeners, process_map = self._scan_system_listeners()

        # 3. Discover Edge Data Plane (Proxy & Engine)
        self._discover_data_plane(nodes, edges, discrepancies, active_listeners, process_map, desired_inv, now_ts)

        # 4. Discover Edge Exporters
        self._discover_exporters(nodes, edges, discrepancies, active_listeners, process_map, vma_report, desired_inv, now_ts)

        # 5. Discover vmagent Collector Node & Remote Write Edges
        self._discover_vmagent_and_destinations(nodes, edges, discrepancies, vma_report, desired_inv, now_ts)

        # Snapshot metadata
        meta = {
            "device_id": self.device_id,
            "ip_address": self.device_ip,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "cpu_count": psutil.cpu_count(logical=True),
            "memory_total_bytes": psutil.virtual_memory().total,
            "vmagent_running": vma_report.is_running,
            "scrape_target_count": len(vma_report.targets),
            "discrepancy_count": len(discrepancies),
        }

        return TopologySnapshot(
            device_id=self.device_id,
            timestamp=now_ts,
            nodes=nodes,
            edges=edges,
            discrepancies=discrepancies,
            metadata=meta,
        )

    def publish_snapshot(self, snapshot: TopologySnapshot) -> bool:
        """Send snapshot to central topology service API."""
        url = f"{self.central_url}/api/v1/topology/snapshot"
        payload = snapshot.model_dump_json().encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"mlx-edge-discovery/{self.device_id}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                return resp.status in (200, 201, 202)
        except Exception:
            return False

    def _scan_system_listeners(self) -> Tuple[Dict[int, List[ListenEndpoint]], Dict[int, Dict[str, Any]]]:
        """Scan active listening sockets and process metadata on the edge node."""
        listeners: Dict[int, List[ListenEndpoint]] = {}
        process_map: Dict[int, Dict[str, Any]] = {}

        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    pid = proc.info.get("pid")
                    if not pid:
                        continue
                    process_map[pid] = {
                        "name": proc.info.get("name", ""),
                        "cmdline": " ".join(proc.info.get("cmdline") or []),
                    }
                    conn_fn = getattr(proc, "net_connections", getattr(proc, "connections", None))
                    if conn_fn is not None:
                        for conn in conn_fn(kind="inet"):
                            status = getattr(conn, "status", "")
                            if status in ("LISTEN", getattr(psutil, "CONN_LISTEN", "LISTEN")) and conn.laddr:
                                ep = ListenEndpoint(
                                    address=str(conn.laddr.ip),
                                    port=int(conn.laddr.port),
                                    protocol="tcp",
                                    interface="loopback" if conn.laddr.ip.startswith("127.") else ("all" if conn.laddr.ip in ("0.0.0.0", "::") else "lan"),
                                )
                                listeners.setdefault(pid, []).append(ep)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

        return listeners, process_map

    def _find_pid_by_port_or_pattern(
        self,
        port: int,
        pattern: str,
        listeners: Dict[int, List[ListenEndpoint]],
        process_map: Dict[int, Dict[str, Any]],
    ) -> Tuple[Optional[int], List[ListenEndpoint]]:
        """Match process PID and active endpoints by port or command line pattern."""
        # Check listeners first
        for pid, eps in listeners.items():
            for ep in eps:
                if ep.port == port:
                    return pid, eps

        # Check process pattern fallback
        for pid, pinfo in process_map.items():
            if pattern and pattern in pinfo.get("cmdline", ""):
                return pid, listeners.get(pid, [])

        return None, []

    def _discover_data_plane(
        self,
        nodes: List[TopologyNode],
        edges: List[TopologyEdge],
        discrepancies: List[Discrepancy],
        listeners: Dict[int, List[ListenEndpoint]],
        process_map: Dict[int, Dict[str, Any]],
        desired_inv: Dict[str, Any],
        now_ts: str,
    ) -> None:
        """Inspect Ingress Proxy and Upstream Inference Engine."""
        # 1. Ingress Proxy (:8080)
        proxy_pid, proxy_eps = self._find_pid_by_port_or_pattern(8080, "mlx_metrics_proxy", listeners, process_map)
        proxy_health = HealthStatus.HEALTHY if proxy_pid is not None else HealthStatus.CRITICAL

        if not proxy_eps and proxy_pid is not None:
            proxy_eps = [ListenEndpoint(address="0.0.0.0", port=8080, protocol="tcp")]

        proxy_node = TopologyNode(
            id=f"{self.device_id}:mlx_metrics_proxy",
            name="MLX Ingress & Metrics Proxy",
            device_id=self.device_id,
            role=NodeRole.DATA_PLANE,
            deployment_type="host_process",
            runtime_id=str(proxy_pid or ""),
            listen_endpoints=proxy_eps,
            config_source="cluster/mlx_monitoring_bundle/edge/config/edge.env",
            health=proxy_health,
            last_seen_ts=now_ts,
            metadata={"proxy_port": 8080, "upstream": "http://127.0.0.1:8030"},
        )
        nodes.append(proxy_node)

        if proxy_health != HealthStatus.HEALTHY:
            discrepancies.append(Discrepancy(
                id=f"{self.device_id}:proxy_down",
                device_id=self.device_id,
                component="mlx_metrics_proxy",
                severity=HealthStatus.CRITICAL,
                issue_type="DATA_PLANE_UNAVAILABLE",
                message="MLX Ingress Proxy process is not running or listening on port 8080",
                expected="Listening on 0.0.0.0:8080",
                observed="Process not found",
                remediation="Start proxy via mlx_monitoring_bundle.py start or check proxy logs",
            ))

        # 2. MLX Engine (:8030)
        engine_pid, engine_eps = self._find_pid_by_port_or_pattern(8030, "mlx_lm.server", listeners, process_map)
        engine_health = HealthStatus.HEALTHY if engine_pid is not None else HealthStatus.CRITICAL

        # Audit engine listening address: Prohibit 0.0.0.0 wildcard binds!
        for ep in engine_eps:
            if ep.is_wildcard:
                engine_health = HealthStatus.MISCONFIGURED
                discrepancies.append(Discrepancy(
                    id=f"{self.device_id}:engine_wildcard_bind",
                    device_id=self.device_id,
                    component="mlx_engine",
                    severity=HealthStatus.MISCONFIGURED,
                    issue_type="PROHIBITED_WILDCARD_BIND",
                    message="MLX Model Engine bound to prohibited 0.0.0.0 wildcard address",
                    expected="Bound to loopback 127.0.0.1:8030",
                    observed=f"Bound to {ep.address}:{ep.port}",
                    remediation="Set MLX_LISTEN_HOST=127.0.0.1 in edge.env or pass --host 127.0.0.1",
                    evidence={"endpoint": f"{ep.address}:{ep.port}", "pid": engine_pid},
                ))

        if not engine_eps and engine_pid is not None:
            engine_eps = [ListenEndpoint(address="127.0.0.1", port=8030, protocol="tcp")]

        engine_node = TopologyNode(
            id=f"{self.device_id}:mlx_engine",
            name="MLX Model Inference Engine",
            device_id=self.device_id,
            role=NodeRole.DATA_PLANE,
            deployment_type="host_process",
            runtime_id=str(engine_pid or ""),
            listen_endpoints=engine_eps,
            config_source="mlx_lm.server / vLLM runtime",
            health=engine_health,
            last_seen_ts=now_ts,
            metadata={"api_port": 8030},
        )
        nodes.append(engine_node)

        # Client -> Proxy Edge (Hop 1)
        edges.append(TopologyEdge(
            id=f"client->{self.device_id}:mlx_metrics_proxy:application",
            source_id="client_applications",
            target_id=proxy_node.id,
            signal_type=SignalType.APPLICATION,
            protocol="http_rest_sse",
            expected_endpoint=":8080",
            observed_endpoint=":8080" if proxy_health == HealthStatus.HEALTHY else "",
            status=proxy_health,
            last_seen_ts=now_ts,
        ))

        # Proxy -> Engine Forwarding Edge (Hop 2)
        forward_status = HealthStatus.HEALTHY if (proxy_health == HealthStatus.HEALTHY and engine_health in (HealthStatus.HEALTHY, HealthStatus.MISCONFIGURED)) else HealthStatus.CRITICAL
        edges.append(TopologyEdge(
            id=f"{proxy_node.id}->{engine_node.id}:application",
            source_id=proxy_node.id,
            target_id=engine_node.id,
            signal_type=SignalType.APPLICATION,
            protocol="http_loopback",
            expected_endpoint="127.0.0.1:8030",
            observed_endpoint="127.0.0.1:8030",
            status=forward_status,
            failure_reason="Upstream engine down" if engine_pid is None else "",
            last_seen_ts=now_ts,
        ))

    def _discover_exporters(
        self,
        nodes: List[TopologyNode],
        edges: List[TopologyEdge],
        discrepancies: List[Discrepancy],
        listeners: Dict[int, List[ListenEndpoint]],
        process_map: Dict[int, Dict[str, Any]],
        vma_report: VMAgentInspectionReport,
        desired_inv: Dict[str, Any],
        now_ts: str,
    ) -> None:
        """Inspect edge Prometheus exporters and scrape edges into vmagent."""
        exporter_specs = [
            ("mlx_server_supervisor", 9101, "mlx_server_supervisor.py", NodeRole.EXPORTER),
            ("mlx_hw_telemetry", 9102, "mlx_hw_telemetry.py", NodeRole.EXPORTER),
            ("mlx_process_profiler", 9103, "mlx_process_profiler.py", NodeRole.EXPORTER),
            ("mlx_server_log_tailer", 9104, "mlx_server_log_tailer.py", NodeRole.EXPORTER),
            ("mlx_accuracy_eval", 9105, "mlx_accuracy_eval.py", NodeRole.EXPORTER),
            ("mlx_sbom_tracker", 9106, "mlx_sbom_collector.py", NodeRole.EXPORTER),
        ]

        # Index vmagent scrape targets by port
        vma_targets_by_port: Dict[int, Any] = {}
        for t in vma_report.targets:
            # Parse port from instance e.g. "mm:9102" or "127.0.0.1:9102"
            if ":" in t.instance:
                try:
                    p = int(t.instance.split(":")[-1])
                    vma_targets_by_port[p] = t
                except ValueError:
                    pass

        vma_node_id = f"{self.device_id}:vmagent"

        for comp_name, port, pattern, role in exporter_specs:
            pid, eps = self._find_pid_by_port_or_pattern(port, pattern, listeners, process_map)
            target_status = vma_targets_by_port.get(port)

            health = HealthStatus.HEALTHY
            fail_reason = ""

            # Check process & socket
            if pid is None and target_status is None:
                health = HealthStatus.CRITICAL
                fail_reason = "Exporter process not running"
            elif target_status and target_status.health == "down":
                health = HealthStatus.CRITICAL
                fail_reason = f"Scrape failed: {target_status.last_error or 'connection refused'}"
            elif pid is not None and not any(e.port == port for e in eps):
                # Process running but listening on wrong port!
                health = HealthStatus.MISCONFIGURED
                actual_ports = [e.port for e in eps]
                fail_reason = f"Port mismatch: running on {actual_ports}, expected {port}"
                discrepancies.append(Discrepancy(
                    id=f"{self.device_id}:{comp_name}_port_mismatch",
                    device_id=self.device_id,
                    component=comp_name,
                    severity=HealthStatus.MISCONFIGURED,
                    issue_type="PORT_MISMATCH",
                    message=f"{comp_name} is running on wrong port ({actual_ports}), expected {port}",
                    expected=f"Port {port}",
                    observed=f"Port(s) {actual_ports}",
                    remediation=f"Configure listen port to {port} in edge.env",
                ))

            if health == HealthStatus.CRITICAL and fail_reason:
                discrepancies.append(Discrepancy(
                    id=f"{self.device_id}:{comp_name}_down",
                    device_id=self.device_id,
                    component=comp_name,
                    severity=HealthStatus.CRITICAL,
                    issue_type="EXPORTER_UNAVAILABLE",
                    message=f"{comp_name} ({port}) is down: {fail_reason}",
                    expected=f"HTTP 200 on 127.0.0.1:{port}/metrics",
                    observed=fail_reason,
                    remediation=f"Restart exporter via mlx_monitoring_bundle.py restart {comp_name}",
                ))

            if not eps and pid is not None:
                eps = [ListenEndpoint(address="127.0.0.1", port=port, protocol="tcp")]

            node_id = f"{self.device_id}:{comp_name}"
            node = TopologyNode(
                id=node_id,
                name=comp_name,
                device_id=self.device_id,
                role=role,
                deployment_type="host_process",
                runtime_id=str(pid or ""),
                listen_endpoints=eps,
                config_source=f"exporters/{pattern}",
                health=health,
                last_seen_ts=now_ts,
                metadata={
                    "port": port,
                    "scraped_by_vmagent": target_status is not None,
                    "last_scrape_duration": target_status.last_scrape_duration_seconds if target_status else 0.0,
                },
            )
            nodes.append(node)

            # Edge: Exporter -> vmagent (Scrape Hop)
            edges.append(TopologyEdge(
                id=f"{node_id}->{vma_node_id}:metrics",
                source_id=node_id,
                target_id=vma_node_id,
                signal_type=SignalType.METRICS,
                protocol="http_prom_scrape",
                expected_endpoint=f"127.0.0.1:{port}/metrics",
                observed_endpoint=target_status.endpoint if target_status else f"127.0.0.1:{port}/metrics",
                status=health,
                failure_reason=fail_reason,
                last_seen_ts=now_ts,
            ))

    def _discover_vmagent_and_destinations(
        self,
        nodes: List[TopologyNode],
        edges: List[TopologyEdge],
        discrepancies: List[Discrepancy],
        vma_report: VMAgentInspectionReport,
        desired_inv: Dict[str, Any],
        now_ts: str,
    ) -> None:
        """Inspect vmagent collector and its remote-write destination to VictoriaMetrics."""
        vma_node_id = f"{self.device_id}:vmagent"
        vma_health = HealthStatus.HEALTHY if vma_report.is_running else HealthStatus.CRITICAL

        # Transfer any vmagent-level discrepancies
        for d in vma_report.discrepancies:
            sev = HealthStatus(d.get("severity", "CRITICAL"))
            if sev == HealthStatus.CRITICAL:
                vma_health = HealthStatus.CRITICAL
            elif sev in (HealthStatus.DEGRADED, HealthStatus.MISCONFIGURED) and vma_health != HealthStatus.CRITICAL:
                vma_health = sev

            discrepancies.append(Discrepancy(
                id=f"{self.device_id}:vmagent_{d.get('issue_type', 'error').lower()}",
                device_id=self.device_id,
                component=d.get("component", "vmagent"),
                severity=sev,
                issue_type=d.get("issue_type", "COLLECTOR_ERROR"),
                message=d.get("message", ""),
                expected=d.get("expected", ""),
                observed=d.get("observed", ""),
                remediation=d.get("remediation", ""),
                evidence=d.get("evidence", {}),
            ))

        vma_node = TopologyNode(
            id=vma_node_id,
            name="vmagent Collector",
            device_id=self.device_id,
            role=NodeRole.COLLECTOR,
            deployment_type="host_process",
            runtime_id=str(vma_report.pid or ""),
            listen_endpoints=[ListenEndpoint(address="127.0.0.1", port=vma_report.listen_port, protocol="tcp")],
            config_source=vma_report.config_file_path or "vmagent-edge/vmagent.yml",
            health=vma_health,
            last_seen_ts=now_ts,
            metadata={
                "scrape_targets": len(vma_report.targets),
                "remote_writes": vma_report.configured_remote_writes,
                "persistent_queue": vma_report.persistent_queue_path,
            },
        )
        nodes.append(vma_node)

        # Central VictoriaMetrics Node & Edge
        default_vm_host = self.central_url.split("://")[-1].split(":")[0] if "://" in self.central_url else "127.0.0.1"
        vm_target_url = vma_report.configured_remote_writes[0] if vma_report.configured_remote_writes else f"http://{default_vm_host}:8428/api/v1/write"
        rw_stat = vma_report.remote_write_status.get(vm_target_url)

        rw_status = HealthStatus.HEALTHY
        rw_reason = ""
        if rw_stat:
            if rw_stat.is_failing:
                rw_status = HealthStatus.CRITICAL
                rw_reason = rw_stat.failure_reason
            elif rw_stat.is_backed_up:
                rw_status = HealthStatus.DEGRADED
                rw_reason = rw_stat.failure_reason

        central_vm_id = "central:victoria_metrics"
        nodes.append(TopologyNode(
            id=central_vm_id,
            name="VictoriaMetrics TSDB",
            device_id="central",
            role=NodeRole.TSDB,
            deployment_type="container",
            listen_endpoints=[ListenEndpoint(address="0.0.0.0", port=8428, protocol="tcp")],
            health=HealthStatus.HEALTHY if rw_status != HealthStatus.CRITICAL else HealthStatus.CRITICAL,
            last_seen_ts=now_ts,
        ))

        # vmagent -> VictoriaMetrics Remote Write Edge
        edges.append(TopologyEdge(
            id=f"{vma_node_id}->{central_vm_id}:metrics",
            source_id=vma_node_id,
            target_id=central_vm_id,
            signal_type=SignalType.METRICS,
            protocol="prom_remote_write",
            expected_endpoint=vm_target_url,
            observed_endpoint=vm_target_url,
            rate=rw_stat.packets_sent_total if rw_stat else 0.0,
            error_rate=rw_stat.conn_errors_total if rw_stat else 0.0,
            queue_size=rw_stat.pending_bytes if rw_stat else 0,
            status=rw_status,
            failure_reason=rw_reason,
            evidence={
                "pending_bytes": rw_stat.pending_bytes if rw_stat else 0,
                "parts_on_disk": rw_stat.parts_on_disk if rw_stat else 0,
                "conn_errors": rw_stat.conn_errors_total if rw_stat else 0,
            },
            last_seen_ts=now_ts,
        ))
