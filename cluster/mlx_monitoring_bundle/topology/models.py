"""Normalized Topology Data Model for MLX Monitoring.

Defines the nodes, edges, listen endpoints, health states, discrepancies,
and snapshots for dynamic topology discovery and reconciliation.
"""

from __future__ import annotations

import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class HealthStatus(str, Enum):
    """Explicit health and observation states for nodes and edges."""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"
    MISCONFIGURED = "MISCONFIGURED"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    NOT_APPLICABLE = "NOT_APPLICABLE"

    @property
    def color(self) -> str:
        """Hex color code for Grafana visualization."""
        mapping = {
            "HEALTHY": "#10b981",       # Emerald green
            "DEGRADED": "#f59e0b",      # Amber
            "CRITICAL": "#ef4444",      # Red
            "MISCONFIGURED": "#dc2626",  # Dark red / violation
            "UNKNOWN": "#6b7280",       # Gray
            "STALE": "#9ca3af",         # Muted gray
            "NOT_APPLICABLE": "#4b5563" # Charcoal
        }
        return mapping.get(self.value, "#6b7280")


class SignalType(str, Enum):
    """Categorization of communication signals traversing topology edges."""
    METRICS = "metrics"
    LOGS = "logs"
    TRACES = "traces"
    APPLICATION = "application"
    HEALTH = "health"

    @property
    def default_color(self) -> str:
        """Standardized edge color semantics matching Grafana requirements."""
        mapping = {
            "APPLICATION": "#3b82f6",  # Blue: application traffic
            "METRICS": "#10b981",      # Green: metrics
            "LOGS": "#f97316",         # Orange: logs
            "TRACES": "#a855f7",       # Purple: traces
            "HEALTH": "#06b6d4",       # Cyan: health probes
        }
        return mapping.get(self.value, "#6b7280")


class NodeRole(str, Enum):
    """Architectural layer of the topology component."""
    CLIENT = "client"
    DATA_PLANE = "data_plane"
    EXPORTER = "exporter"
    COLLECTOR = "collector"
    TSDB = "tsdb"
    VISUALS = "visuals"
    EVAL = "eval"


class StateType(str, Enum):
    """Distinguishes between configured expectation, runtime observation, and reconciliation."""
    DESIRED = "desired"
    OBSERVED = "observed"
    RECONCILED = "reconciled"


class ListenEndpoint(BaseModel):
    """Represents an active or configured listening network socket."""
    address: str
    port: int
    protocol: str = "tcp"
    interface: str = "any"
    is_wildcard: bool = False

    def __init__(self, **data: Any):
        super().__init__(**data)
        self.is_wildcard = self.address in ("0.0.0.0", "::")


class Discrepancy(BaseModel):
    """Captures a reconciliation mismatch, misconfiguration, or health violation."""
    id: str
    device_id: str
    component: str
    severity: HealthStatus
    issue_type: str  # e.g. "PROHIBITED_WILDCARD_BIND", "SCRAPE_FAILURE", "QUEUE_BACKUP"
    message: str
    expected: str = ""
    observed: str = ""
    remediation: str = ""
    evidence: Dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())


class TopologyNode(BaseModel):
    """Normalized component entity in the cluster topology."""
    id: str  # e.g. "node-01:mlx_metrics_proxy" or "central:victoria_metrics"
    name: str
    device_id: str
    role: NodeRole
    deployment_type: str = "host_process"  # host_process | macos_launchd | systemd | podman | docker | k8s
    runtime_id: str = ""  # PID or container/pod ID
    listen_endpoints: List[ListenEndpoint] = Field(default_factory=list)
    config_source: str = ""
    health: HealthStatus = HealthStatus.UNKNOWN
    freshness_seconds: float = 0.0
    last_seen_ts: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    state_type: StateType = StateType.OBSERVED
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_grafana_node(self) -> Dict[str, Any]:
        """Convert to Grafana Node Graph compatible row representation."""
        main_stat = ""
        sec_stat = ""
        if self.role == NodeRole.DATA_PLANE:
            main_stat = f"{self.metadata.get('req_rate', 0.0):.1f} req/s"
            sec_stat = f"{self.metadata.get('latency_ms', 0.0):.1f} ms"
        elif self.role == NodeRole.COLLECTOR:
            main_stat = f"{self.metadata.get('samples_rate', 0.0):.0f} smp/s"
            sec_stat = f"{self.metadata.get('pending_bytes', 0) / 1024:.0f} KB q"
        elif self.role == NodeRole.EXPORTER:
            main_stat = f"P:{self.listen_endpoints[0].port if self.listen_endpoints else '?'}"
            sec_stat = f"{self.metadata.get('scrape_duration_ms', 0.0):.1f} ms"
        else:
            main_stat = self.health.value
            sec_stat = self.deployment_type

        return {
            "id": self.id,
            "title": self.name,
            "subTitle": f"{self.device_id} ({self.deployment_type})",
            "mainStat": main_stat,
            "secondaryStat": sec_stat,
            "color": self.health.color,
            "role": self.role.value,
            "health": self.health.value,
            "device": self.device_id,
            "detail__runtime_id": self.runtime_id,
            "detail__endpoints": ", ".join(f"{e.address}:{e.port}" for e in self.listen_endpoints) or "None",
            "detail__config_source": self.config_source,
            "detail__last_seen": self.last_seen_ts,
        }


class TopologyEdge(BaseModel):
    """Normalized directed communication edge between two components."""
    id: str  # e.g. "node-01:mlx_metrics_proxy->central:victoria_metrics:metrics"
    source_id: str
    target_id: str
    device_id: str = ""
    signal_type: SignalType
    protocol: str = "http"  # http_prom_scrape | prom_remote_write | otlp_http | opik_sdk | http_app
    expected_endpoint: str = ""
    observed_endpoint: str = ""
    rate: float = 0.0  # requests/s, samples/s, or bytes/s
    error_rate: float = 0.0
    queue_size: int = 0  # bytes or items
    status: HealthStatus = HealthStatus.UNKNOWN
    failure_reason: str = ""
    evidence: Dict[str, Any] = Field(default_factory=dict)
    last_seen_ts: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    metadata: Dict[str, Any] = Field(default_factory=dict)

    def to_grafana_edge(self) -> Dict[str, Any]:
        """Convert to Grafana Node Graph compatible edge representation."""
        # Color: red if failed, else signal color
        edge_color = self.status.color if self.status in (HealthStatus.CRITICAL, HealthStatus.MISCONFIGURED, HealthStatus.DEGRADED) else self.signal_type.default_color

        main_stat = f"{self.rate:.1f}/s" if self.rate > 0 else (f"{self.error_rate:.2f} errs" if self.error_rate > 0 else "0/s")
        sec_stat = f"{self.queue_size / 1024:.0f} KB q" if self.queue_size > 0 else self.protocol

        return {
            "id": self.id,
            "source": self.source_id,
            "target": self.target_id,
            "device": self.device_id,
            "mainStat": main_stat,
            "secondaryStat": sec_stat,
            "color": edge_color,
            "signalType": self.signal_type.value,
            "status": self.status.value,
            "detail__protocol": self.protocol,
            "detail__expected_endpoint": self.expected_endpoint,
            "detail__observed_endpoint": self.observed_endpoint,
            "detail__failure_reason": self.failure_reason or "None",
            "detail__evidence": str(self.evidence) if self.evidence else "None",
            "detail__last_seen": self.last_seen_ts,
        }


class TopologySnapshot(BaseModel):
    """Point-in-time topology capture emitted by an edge node or central reconciler."""
    device_id: str
    timestamp: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    nodes: List[TopologyNode] = Field(default_factory=list)
    edges: List[TopologyEdge] = Field(default_factory=list)
    discrepancies: List[Discrepancy] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
