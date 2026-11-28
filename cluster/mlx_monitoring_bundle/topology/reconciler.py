"""Central Topology Reconciler.

Reconciles edge snapshots, detects stale devices, and validates that
metrics pushed to VictoriaMetrics are actually queryable.
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .models import Discrepancy, HealthStatus, TopologyEdge, TopologyNode
from .storage import TopologyStorage


class CentralTopologyReconciler:
    """Orchestrates multi-device reconciliation and VictoriaMetrics query verification."""

    def __init__(
        self,
        storage: TopologyStorage,
        vm_query_url: str = "http://127.0.0.1:8428",
        stale_threshold_seconds: float = 60.0,
    ):
        self.storage = storage
        self.vm_query_url = vm_query_url.rstrip("/")
        self.stale_threshold_seconds = stale_threshold_seconds

    def reconcile_fleet(self) -> Dict[str, Any]:
        """Perform periodic fleet reconciliation across all devices."""
        # 1. Mark stale devices that have missed heartbeats
        stale_device_ids = self.storage.mark_stale_devices(threshold_seconds=self.stale_threshold_seconds)

        # 2. Fetch all registered devices
        devices = self.storage.get_all_devices()

        # 3. For each active device, verify queryable delivery in VictoriaMetrics
        query_audit_results = {}
        for dev in devices:
            dev_id = dev["device_id"]
            if dev_id != "central" and dev["status"] != HealthStatus.STALE.value:
                query_ok, q_evidence = self.verify_tsdb_queryability(dev_id)
                query_audit_results[dev_id] = {
                    "queryable": query_ok,
                    "evidence": q_evidence,
                }
                if not query_ok:
                    # Record discrepancy
                    now_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    disc = Discrepancy(
                        id=f"{dev_id}:delivery_not_queryable",
                        device_id=dev_id,
                        component="victoria_metrics",
                        severity=HealthStatus.CRITICAL,
                        issue_type="DATA_ACCEPTED_BUT_NOT_QUERYABLE",
                        message=f"Telemetry accepted by vmagent on {dev_id} is not queryable in VictoriaMetrics TSDB",
                        expected=f"PromQL series count(up{{device='{dev_id}'}}) > 0",
                        observed=f"Result: {q_evidence.get('error') or q_evidence.get('series_count', 0)} series",
                        remediation="Verify VictoriaMetrics storage disk space, ingestion rate limits, and label relabeling",
                        evidence=q_evidence,
                        timestamp=now_ts,
                    )
                    # Update edge from vmagent to TSDB
                    edges = self.storage.get_edges(dev_id)
                    for edge in edges:
                        if edge.target_id == "central:victoria_metrics":
                            edge.status = HealthStatus.CRITICAL
                            edge.failure_reason = "Data accepted but not queryable in TSDB"
                            edge.evidence.update(q_evidence)

        # 4. Summary counts
        total_devices = len(devices)
        healthy_devices = sum(1 for d in devices if d["status"] == HealthStatus.HEALTHY.value)
        degraded_devices = sum(1 for d in devices if d["status"] == HealthStatus.DEGRADED.value)
        critical_devices = sum(1 for d in devices if d["status"] in (HealthStatus.CRITICAL.value, HealthStatus.MISCONFIGURED.value))
        stale_count = len(stale_device_ids)

        return {
            "total_devices": total_devices,
            "healthy_devices": healthy_devices,
            "degraded_devices": degraded_devices,
            "critical_devices": critical_devices,
            "stale_devices": stale_count,
            "stale_device_ids": stale_device_ids,
            "query_audits": query_audit_results,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

    def verify_tsdb_queryability(self, device_id: str) -> tuple[bool, Dict[str, Any]]:
        """Verify that metrics reported for device_id are actually queryable in VictoriaMetrics."""
        expr = f"count(up{{device='{device_id}'}})"
        url = f"{self.vm_query_url}/api/v1/query?query={urllib.parse_quote(expr)}"
        req = urllib.request.Request(url, headers={"User-Agent": "mlx-topology-reconciler/1.0"})

        try:
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    result = data.get("data", {}).get("result", [])
                    if result:
                        val = result[0].get("value", [0, "0"])[1]
                        count = int(float(val))
                        if count > 0:
                            return True, {"series_count": count, "query": expr}
                    return False, {"series_count": 0, "query": expr, "error": "No series found in TSDB for device"}
                return False, {"status_code": resp.status, "query": expr, "error": "Non-200 TSDB response"}
        except urllib.error.URLError as exc:
            return False, {"query": expr, "error": f"TSDB connection failed: {exc.reason}"}
        except Exception as exc:
            return False, {"query": expr, "error": f"Query execution failed: {exc!r}"}


# Module level urllib helper to avoid import conflict
import urllib.parse
urllib.parse_quote = urllib.parse.quote
