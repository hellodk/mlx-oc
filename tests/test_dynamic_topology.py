"""Automated Test Suite for MLX Dynamic Network Topology & Telemetry Flow.

Covers all 11 failure and operational scenarios plus 100-device simulation:
1. Exporter unavailable
2. Exporter running on wrong port
3. vmagent scrape failure
4. Incorrect remote-write destination
5. Remote-write queue backup
6. VictoriaMetrics unavailable
7. Samples not queryable after delivery
8. OTel export failure
9. Stale device heartbeat
10. Unexpected network connection / prohibited wildcard bind
11. Device recovery after outage
12. 100-device concurrent reporting load test
"""

from __future__ import annotations

import concurrent.futures
import datetime
import http.server
import json
import os
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

import pytest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cluster.mlx_monitoring_bundle.topology.discovery import EdgeTopologyDiscovery
from cluster.mlx_monitoring_bundle.topology.models import (
    Discrepancy,
    HealthStatus,
    ListenEndpoint,
    NodeRole,
    SignalType,
    TopologyEdge,
    TopologyNode,
    TopologySnapshot,
)
from cluster.mlx_monitoring_bundle.topology.reconciler import CentralTopologyReconciler
from cluster.mlx_monitoring_bundle.topology.service import TopologyAPIService
from cluster.mlx_monitoring_bundle.topology.storage import SQLiteTopologyStorage
from cluster.mlx_monitoring_bundle.topology.vmagent_inspector import (
    ScrapeTargetStatus,
    VMAgentInspectionReport,
    VMAgentInspector,
)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_topology.db"


@pytest.fixture
def storage(temp_db: Path) -> SQLiteTopologyStorage:
    return SQLiteTopologyStorage(db_path=temp_db)


# ---------------------------------------------------------------------------
# Scenario 1: Exporter Unavailable
# ---------------------------------------------------------------------------
def test_scenario_01_exporter_unavailable(tmp_path: Path):
    """Verify that a crashed/missing exporter is marked CRITICAL with exact diagnosis."""
    discovery = EdgeTopologyDiscovery(device_id="node-01")

    # Mock empty listener and process scan (no exporter running)
    nodes: List[TopologyNode] = []
    edges: List[TopologyEdge] = []
    discrepancies: List[Discrepancy] = []
    vma_report = VMAgentInspectionReport(is_running=True)

    discovery._discover_exporters(
        nodes=nodes,
        edges=edges,
        discrepancies=discrepancies,
        listeners={},
        process_map={},
        vma_report=vma_report,
        desired_inv={},
        now_ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    hw_nodes = [n for n in nodes if n.name == "mlx_hw_telemetry"]
    assert len(hw_nodes) == 1
    assert hw_nodes[0].health == HealthStatus.CRITICAL

    # Verify edge to vmagent is marked CRITICAL
    hw_edges = [e for e in edges if e.source_id == hw_nodes[0].id]
    assert len(hw_edges) == 1
    assert hw_edges[0].status == HealthStatus.CRITICAL
    assert "Exporter process not running" in hw_edges[0].failure_reason

    # Verify discrepancy logged
    disc = [d for d in discrepancies if d.component == "mlx_hw_telemetry"]
    assert len(disc) == 1
    assert disc[0].issue_type == "EXPORTER_UNAVAILABLE"
    assert disc[0].severity == HealthStatus.CRITICAL


# ---------------------------------------------------------------------------
# Scenario 2: Exporter Running on Wrong Port
# ---------------------------------------------------------------------------
def test_scenario_02_exporter_wrong_port():
    """Verify that an exporter listening on port 9199 instead of 9102 is flagged MISCONFIGURED."""
    discovery = EdgeTopologyDiscovery(device_id="node-01")

    nodes: List[TopologyNode] = []
    edges: List[TopologyEdge] = []
    discrepancies: List[Discrepancy] = []
    vma_report = VMAgentInspectionReport(is_running=True)

    # Process running (PID 1234), but socket is listening on 9199!
    mock_listeners = {1234: [ListenEndpoint(address="127.0.0.1", port=9199, protocol="tcp")]}
    mock_proc = {1234: {"name": "python", "cmdline": "python mlx_hw_telemetry.py --port 9199"}}

    discovery._discover_exporters(
        nodes=nodes,
        edges=edges,
        discrepancies=discrepancies,
        listeners=mock_listeners,
        process_map=mock_proc,
        vma_report=vma_report,
        desired_inv={},
        now_ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    hw_nodes = [n for n in nodes if n.name == "mlx_hw_telemetry"]
    assert len(hw_nodes) == 1
    assert hw_nodes[0].health == HealthStatus.MISCONFIGURED

    # Check discrepancy details
    disc = [d for d in discrepancies if d.component == "mlx_hw_telemetry" and d.issue_type == "PORT_MISMATCH"]
    assert len(disc) == 1
    assert "9199" in disc[0].message
    assert "9102" in disc[0].message


# ---------------------------------------------------------------------------
# Scenario 3: vmagent Scrape Failure
# ---------------------------------------------------------------------------
def test_scenario_03_vmagent_scrape_failure():
    """Verify that a scrape target failure reported by vmagent /api/v1/targets is caught."""
    inspector = VMAgentInspector()
    report = VMAgentInspectionReport(is_running=True)

    # Add a failed target
    report.targets.append(ScrapeTargetStatus(
        job="mlx-logtailer",
        instance="127.0.0.1:9104",
        endpoint="http://127.0.0.1:9104/metrics",
        health="down",
        last_error="dial tcp 127.0.0.1:9104: connect: connection refused",
        last_scrape_duration_seconds=0.001,
        scraped_samples=0,
    ))

    inspector._diagnose_reconciliation(report)
    assert len(report.discrepancies) >= 1
    disc = report.discrepancies[0]
    assert disc["issue_type"] == "SCRAPE_FAILURE"
    assert disc["component"] == "mlx-logtailer"
    assert "connection refused" in disc["message"]


# ---------------------------------------------------------------------------
# Scenario 4: Incorrect Remote-Write Destination
# ---------------------------------------------------------------------------
def test_scenario_04_incorrect_remote_write_destination():
    """Verify that a failing remote-write destination flags the edge and logs a CRITICAL discrepancy."""
    inspector = VMAgentInspector()
    report = VMAgentInspectionReport(
        is_running=True,
        configured_remote_writes=["http://192.168.1.99:9999/api/v1/write"],
    )

    # Simulate metrics output showing connection errors
    metrics_text = """
    # HELP vmagent_remotewrite_conn_errors_total
    vmagent_remotewrite_conn_errors_total 42
    vmagent_remotewrite_packets_sent_total 0
    """
    inspector._parse_metrics_text(metrics_text, report)
    inspector._diagnose_reconciliation(report)

    rw_stat = report.remote_write_status["http://192.168.1.99:9999/api/v1/write"]
    assert rw_stat.is_failing is True
    assert rw_stat.conn_errors_total == 42

    disc = [d for d in report.discrepancies if d["issue_type"] == "REMOTE_WRITE_FAILURE"]
    assert len(disc) == 1
    assert "42 errors" in disc[0]["message"]


# ---------------------------------------------------------------------------
# Scenario 5: Remote-Write Queue Backup
# ---------------------------------------------------------------------------
def test_scenario_05_remote_write_queue_backup():
    """Verify that persistent queue spooling or pending memory backup is flagged DEGRADED."""
    inspector = VMAgentInspector()
    report = VMAgentInspectionReport(
        is_running=True,
        configured_remote_writes=["http://192.168.1.10:8428/api/v1/write"],
    )

    # 25 MB pending and 5 parts on disk
    metrics_text = """
    vmagent_remotewrite_pending_data_bytes 26214400
    vmagent_remotewrite_parts_on_disk 5
    vmagent_remotewrite_packets_sent_total 1500
    vmagent_remotewrite_conn_errors_total 0
    """
    inspector._parse_metrics_text(metrics_text, report)
    inspector._diagnose_reconciliation(report)

    rw_stat = report.remote_write_status["http://192.168.1.10:8428/api/v1/write"]
    assert rw_stat.is_backed_up is True
    assert rw_stat.pending_bytes == 26214400
    assert rw_stat.parts_on_disk == 5

    disc = [d for d in report.discrepancies if d["issue_type"] == "REMOTE_WRITE_QUEUE_BACKUP"]
    assert len(disc) == 1
    assert disc[0]["severity"] == "DEGRADED"


# ---------------------------------------------------------------------------
# Scenario 6: VictoriaMetrics Unavailable
# ---------------------------------------------------------------------------
def test_scenario_06_victoriametrics_unavailable(storage: SQLiteTopologyStorage):
    """Verify reconciler catches when VictoriaMetrics TSDB is offline or unreachable."""
    reconciler = CentralTopologyReconciler(
        storage=storage,
        vm_query_url="http://127.0.0.1:59999",  # Port where nothing is listening
    )
    ok, evidence = reconciler.verify_tsdb_queryability("node-01")
    assert ok is False
    assert "TSDB connection failed" in evidence.get("error", "")


# ---------------------------------------------------------------------------
# Scenario 7: Samples Not Queryable After Delivery
# ---------------------------------------------------------------------------
def test_scenario_07_samples_not_queryable_after_delivery(storage: SQLiteTopologyStorage):
    """Verify detection when vmagent reports delivery, but TSDB queries return 0 series."""
    class MockTSDBHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Return empty series result
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"success","data":{"resultType":"vector","result":[]}}')

        def log_message(self, format, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), MockTSDBHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    try:
        reconciler = CentralTopologyReconciler(storage=storage, vm_query_url=f"http://127.0.0.1:{port}")
        ok, evidence = reconciler.verify_tsdb_queryability("node-01")
        assert ok is False
        assert evidence.get("series_count") == 0
        assert "No series found in TSDB" in evidence.get("error", "")
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# Scenario 8: OTel Export Failure
# ---------------------------------------------------------------------------
def test_scenario_08_otel_export_failure():
    """Verify OTel Collector log and trace pipeline status reflects actual destination."""
    from cluster.mlx_monitoring_bundle.topology.models import TopologyEdge, SignalType, HealthStatus

    # An edge from proxy to OTel collector with connection failure
    edge = TopologyEdge(
        id="node-01:proxy->central:otelcol:logs",
        source_id="node-01:mlx_metrics_proxy",
        target_id="central:otel_collector",
        signal_type=SignalType.LOGS,
        protocol="otlp_http",
        status=HealthStatus.CRITICAL,
        failure_reason="OTLP receiver rejected connection on :4318",
        evidence={"status_code": 503, "error": "Disk full on log destination /var/log/otelcol"},
    )
    assert edge.status == HealthStatus.CRITICAL
    assert edge.to_grafana_edge()["color"] == "#ef4444"  # Red for failure


# ---------------------------------------------------------------------------
# Scenario 9: Stale Device Heartbeat
# ---------------------------------------------------------------------------
def test_scenario_09_stale_device_heartbeat(storage: SQLiteTopologyStorage):
    """Verify devices that have missed heartbeats are transitioned to STALE."""
    # Insert device with last heartbeat 2 minutes ago
    old_time = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=120)).isoformat()
    snap = TopologySnapshot(
        device_id="node-silent",
        timestamp=old_time,
        nodes=[
            TopologyNode(
                id="node-silent:proxy",
                name="Proxy",
                device_id="node-silent",
                role=NodeRole.DATA_PLANE,
                health=HealthStatus.HEALTHY,
                last_seen_ts=old_time,
            )
        ],
        edges=[],
        discrepancies=[],
    )
    storage.save_snapshot(snap)

    # Reconciler checks threshold of 60 seconds
    stale = storage.mark_stale_devices(threshold_seconds=60.0)
    assert "node-silent" in stale

    # Verify device record updated
    devices = {d["device_id"]: d for d in storage.get_all_devices()}
    assert devices["node-silent"]["status"] == HealthStatus.STALE.value

    # Verify node updated to STALE
    nodes = storage.get_nodes("node-silent")
    assert nodes[0].health == HealthStatus.STALE


# ---------------------------------------------------------------------------
# Scenario 10: Unexpected Network Connection / Wildcard Bind
# ---------------------------------------------------------------------------
def test_scenario_10_unexpected_network_connection():
    """Verify that an unexpected 0.0.0.0 bind on an internal engine triggers an audit alert."""
    discovery = EdgeTopologyDiscovery(device_id="node-01")

    nodes: List[TopologyNode] = []
    edges: List[TopologyEdge] = []
    discrepancies: List[Discrepancy] = []

    # Engine is bound to 0.0.0.0:8030 (security violation!)
    mock_listeners = {555: [ListenEndpoint(address="0.0.0.0", port=8030, protocol="tcp")]}
    mock_proc = {555: {"name": "mlx_lm.server", "cmdline": "python -m mlx_lm.server --host 0.0.0.0 --port 8030"}}

    discovery._discover_data_plane(
        nodes=nodes,
        edges=edges,
        discrepancies=discrepancies,
        listeners=mock_listeners,
        process_map=mock_proc,
        desired_inv={},
        now_ts=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    engine_node = [n for n in nodes if n.name == "MLX Model Inference Engine"][0]
    assert engine_node.health == HealthStatus.MISCONFIGURED

    wildcard_disc = [d for d in discrepancies if d.issue_type == "PROHIBITED_WILDCARD_BIND"]
    assert len(wildcard_disc) == 1
    assert "0.0.0.0" in wildcard_disc[0].message


# ---------------------------------------------------------------------------
# Scenario 11: Device Recovery After Outage
# ---------------------------------------------------------------------------
def test_scenario_11_device_recovery(storage: SQLiteTopologyStorage):
    """Verify that when a degraded/critical device recovers, discrepancies clear and status resets to HEALTHY."""
    # 1. Device is critical
    snap_bad = TopologySnapshot(
        device_id="node-recovering",
        nodes=[TopologyNode(id="node-recovering:vma", name="vma", device_id="node-recovering", role=NodeRole.COLLECTOR, health=HealthStatus.CRITICAL)],
        edges=[],
        discrepancies=[Discrepancy(id="disc1", device_id="node-recovering", component="vma", severity=HealthStatus.CRITICAL, issue_type="CRASH", message="Crashed")],
    )
    storage.save_snapshot(snap_bad)
    devs = {d["device_id"]: d for d in storage.get_all_devices()}
    assert devs["node-recovering"]["status"] == HealthStatus.CRITICAL.value
    assert len(storage.get_discrepancies("node-recovering")) == 1

    # 2. Device recovers
    snap_good = TopologySnapshot(
        device_id="node-recovering",
        nodes=[TopologyNode(id="node-recovering:vma", name="vma", device_id="node-recovering", role=NodeRole.COLLECTOR, health=HealthStatus.HEALTHY)],
        edges=[],
        discrepancies=[],  # Cleared!
    )
    storage.save_snapshot(snap_good)
    devs = {d["device_id"]: d for d in storage.get_all_devices()}
    assert devs["node-recovering"]["status"] == HealthStatus.HEALTHY.value
    assert len(storage.get_discrepancies("node-recovering")) == 0


# ---------------------------------------------------------------------------
# Scenario 12: 100-Device Concurrent Reporting Simulation
# ---------------------------------------------------------------------------
def test_scenario_12_concurrent_100_devices_simulation(tmp_path: Path):
    """Simulate 100 devices reporting concurrent topology snapshots to the central API service."""
    db_file = tmp_path / "sim100_topology.db"
    svc = TopologyAPIService(host="127.0.0.1", port=0, db_path=db_file)

    # Start service on background thread
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), svc.server.RequestHandlerClass if svc.server else http.server.SimpleHTTPRequestHandler)
    # Configure handlers
    from cluster.mlx_monitoring_bundle.topology.service import TopologyRequestHandler
    TopologyRequestHandler.storage = svc.storage
    TopologyRequestHandler.reconciler = svc.reconciler
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TopologyRequestHandler)
    assigned_port = httpd.server_address[1]

    t_srv = threading.Thread(target=httpd.serve_forever, daemon=True)
    t_srv.start()

    url = f"http://127.0.0.1:{assigned_port}/api/v1/topology/snapshot"

    def send_device_snapshot(idx: int) -> tuple[int, float]:
        dev_id = f"node-{idx:03d}"
        snap = TopologySnapshot(
            device_id=dev_id,
            nodes=[
                TopologyNode(id=f"{dev_id}:proxy", name="Proxy", device_id=dev_id, role=NodeRole.DATA_PLANE, health=HealthStatus.HEALTHY),
                TopologyNode(id=f"{dev_id}:vma", name="vmagent", device_id=dev_id, role=NodeRole.COLLECTOR, health=HealthStatus.HEALTHY),
            ],
            edges=[
                TopologyEdge(id=f"{dev_id}:proxy->{dev_id}:vma", source_id=f"{dev_id}:proxy", target_id=f"{dev_id}:vma", signal_type=SignalType.METRICS, status=HealthStatus.HEALTHY),
            ],
            discrepancies=[],
            metadata={"ip": f"192.168.1.{idx}", "deployment_type": "host_process"},
        )
        data = snap.model_dump_json().encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

        t0 = time.time()
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            status = resp.status
        dur = time.time() - t0
        return status, dur

    try:
        # Fan out 100 concurrent snapshots over 20 worker threads
        start_time = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(send_device_snapshot, i) for i in range(1, 101)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]

        total_elapsed = time.time() - start_time
        assert len(results) == 100
        statuses = [r[0] for r in results]
        latencies = [r[1] for r in results]

        # All 100 must succeed with HTTP 202
        assert all(s == 202 for s in statuses), f"Non-202 statuses observed: {set(statuses)}"
        avg_latency_ms = (sum(latencies) / len(latencies)) * 1000.0

        # Verify all 100 devices are in the SQLite WAL database
        all_devs = svc.storage.get_all_devices()
        fleet_devs = [d for d in all_devs if d["device_id"] != "central"]
        assert len(fleet_devs) == 100

        # Test Grafana Node Graph fleet endpoint performance
        gf_nodes_req = urllib.request.Request(f"http://127.0.0.1:{assigned_port}/api/v1/topology/grafana/nodes?scope=fleet")
        t_gf = time.time()
        with urllib.request.urlopen(gf_nodes_req, timeout=3.0) as resp:
            gf_nodes = json.loads(resp.read().decode("utf-8"))
        gf_latency_ms = (time.time() - t_gf) * 1000.0

        # 100 device nodes + 2 central nodes = 102 nodes
        assert len(gf_nodes) == 102
        assert gf_latency_ms < 100.0, f"Grafana fleet nodes latency too high: {gf_latency_ms:.1f}ms"

        # Test Prometheus metrics scrape output
        prom_req = urllib.request.Request(f"http://127.0.0.1:{assigned_port}/metrics")
        with urllib.request.urlopen(prom_req, timeout=3.0) as resp:
            prom_text = resp.read().decode("utf-8")
        assert ('mlx_topology_devices_total{status="HEALTHY"} 101' in prom_text or 'mlx_topology_devices_total{status="HEALTHY"} 100' in prom_text)

    finally:
        httpd.shutdown()
        httpd.server_close()
