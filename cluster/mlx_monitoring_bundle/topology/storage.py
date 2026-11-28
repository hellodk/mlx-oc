"""Persistent Central Topology Storage.

Provides atomic, thread-safe, WAL-enabled SQLite storage for topology
nodes, edges, snapshots, device heartbeats, and discrepancy logs.
"""

from __future__ import annotations

import abc
import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

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


class TopologyStorage(abc.ABC):
    """Abstract interface for topology storage backends (SQLite, PostgreSQL, etc.)."""

    @abc.abstractmethod
    def save_snapshot(self, snapshot: TopologySnapshot) -> None:
        """Persist a point-in-time topology snapshot and upsert active nodes and edges."""
        pass

    @abc.abstractmethod
    def get_all_devices(self) -> List[Dict[str, Any]]:
        """List all registered devices, their status, and heartbeat timestamps."""
        pass

    @abc.abstractmethod
    def get_nodes(self, device_id: Optional[str] = None) -> List[TopologyNode]:
        """Fetch current active nodes, optionally filtered by device ID."""
        pass

    @abc.abstractmethod
    def get_edges(self, device_id: Optional[str] = None) -> List[TopologyEdge]:
        """Fetch current active edges, optionally filtered by device ID."""
        pass

    @abc.abstractmethod
    def get_discrepancies(self, device_id: Optional[str] = None) -> List[Discrepancy]:
        """Fetch active unresolved discrepancies and misconfigurations."""
        pass

    @abc.abstractmethod
    def mark_stale_devices(self, threshold_seconds: float = 60.0) -> List[str]:
        """Mark devices with no heartbeat within threshold as STALE."""
        pass


class SQLiteTopologyStorage(TopologyStorage):
    """SQLite WAL-mode implementation of the central topology registry."""

    def __init__(self, db_path: str | Path = "topology.db"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    ip TEXT NOT NULL DEFAULT '',
                    deployment_type TEXT NOT NULL DEFAULT 'host_process',
                    status TEXT NOT NULL DEFAULT 'HEALTHY',
                    last_heartbeat TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS nodes (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    deployment_type TEXT NOT NULL,
                    runtime_id TEXT NOT NULL DEFAULT '',
                    listen_endpoints_json TEXT NOT NULL DEFAULT '[]',
                    config_source TEXT NOT NULL DEFAULT '',
                    health TEXT NOT NULL,
                    freshness_seconds REAL NOT NULL DEFAULT 0.0,
                    last_seen_ts TEXT NOT NULL,
                    state_type TEXT NOT NULL DEFAULT 'observed',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (device_id) REFERENCES devices (device_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS edges (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    expected_endpoint TEXT NOT NULL DEFAULT '',
                    observed_endpoint TEXT NOT NULL DEFAULT '',
                    rate REAL NOT NULL DEFAULT 0.0,
                    error_rate REAL NOT NULL DEFAULT 0.0,
                    queue_size INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    failure_reason TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    last_seen_ts TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS discrepancies (
                    id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    component TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    issue_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    expected TEXT NOT NULL DEFAULT '',
                    observed TEXT NOT NULL DEFAULT '',
                    remediation TEXT NOT NULL DEFAULT '',
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    resolved INTEGER NOT NULL DEFAULT 0,
                    timestamp TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    node_count INTEGER NOT NULL,
                    edge_count INTEGER NOT NULL,
                    discrepancy_count INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_devices_heartbeat ON devices (last_heartbeat);
                CREATE INDEX IF NOT EXISTS idx_nodes_device ON nodes (device_id);
                CREATE INDEX IF NOT EXISTS idx_edges_source ON edges (source_id);
                CREATE INDEX IF NOT EXISTS idx_edges_target ON edges (target_id);
                CREATE INDEX IF NOT EXISTS idx_edges_device ON edges (device_id);
                CREATE INDEX IF NOT EXISTS idx_discrepancies_device ON discrepancies (device_id, resolved);
                CREATE INDEX IF NOT EXISTS idx_snapshots_device_ts ON snapshots (device_id, timestamp);

                INSERT OR IGNORE INTO devices (device_id, name, ip, deployment_type, status, last_heartbeat)
                VALUES ('central', 'Central Observability Stack', '127.0.0.1', 'container', 'HEALTHY', '2026-01-01T00:00:00Z');
            """)

    def save_snapshot(self, snapshot: TopologySnapshot) -> None:
        """Persist snapshot, update device heartbeat, and upsert nodes/edges atomically."""
        now_ts = snapshot.timestamp or datetime.datetime.now(datetime.timezone.utc).isoformat()
        dev_id = snapshot.device_id

        # Determine overall device status
        dev_status = HealthStatus.HEALTHY.value
        severities = [d.severity.value for d in snapshot.discrepancies]
        if HealthStatus.CRITICAL.value in severities:
            dev_status = HealthStatus.CRITICAL.value
        elif HealthStatus.MISCONFIGURED.value in severities:
            dev_status = HealthStatus.MISCONFIGURED.value
        elif HealthStatus.DEGRADED.value in severities:
            dev_status = HealthStatus.DEGRADED.value

        with self._get_connection() as conn:
            with conn:
                # 1. Upsert Device Record
                conn.execute("""
                    INSERT INTO devices (device_id, name, ip, deployment_type, status, last_heartbeat, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        status = excluded.status,
                        last_heartbeat = excluded.last_heartbeat,
                        metadata_json = excluded.metadata_json
                """, (
                    dev_id,
                    snapshot.metadata.get("hostname", dev_id),
                    snapshot.metadata.get("ip", ""),
                    snapshot.metadata.get("deployment_type", "host_process"),
                    dev_status,
                    now_ts,
                    json.dumps(snapshot.metadata),
                ))

                # 2. Archive Snapshot
                conn.execute("""
                    INSERT INTO snapshots (device_id, timestamp, node_count, edge_count, discrepancy_count, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    dev_id,
                    now_ts,
                    len(snapshot.nodes),
                    len(snapshot.edges),
                    len(snapshot.discrepancies),
                    snapshot.model_dump_json(),
                ))

                # 3. Upsert Active Nodes
                for node in snapshot.nodes:
                    if node.device_id != dev_id:
                        conn.execute("""
                            INSERT OR IGNORE INTO devices (device_id, name, ip, deployment_type, status, last_heartbeat)
                            VALUES (?, ?, ?, ?, 'HEALTHY', ?)
                        """, (node.device_id, node.device_id, "", "container", now_ts))
                    conn.execute("""
                        INSERT INTO nodes (
                            id, device_id, name, role, deployment_type, runtime_id,
                            listen_endpoints_json, config_source, health, freshness_seconds,
                            last_seen_ts, state_type, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            name = excluded.name,
                            role = excluded.role,
                            deployment_type = excluded.deployment_type,
                            runtime_id = excluded.runtime_id,
                            listen_endpoints_json = excluded.listen_endpoints_json,
                            config_source = excluded.config_source,
                            health = excluded.health,
                            freshness_seconds = excluded.freshness_seconds,
                            last_seen_ts = excluded.last_seen_ts,
                            state_type = excluded.state_type,
                            metadata_json = excluded.metadata_json
                    """, (
                        node.id,
                        node.device_id,
                        node.name,
                        node.role.value,
                        node.deployment_type,
                        node.runtime_id,
                        json.dumps([e.model_dump() for e in node.listen_endpoints]),
                        node.config_source,
                        node.health.value,
                        node.freshness_seconds,
                        node.last_seen_ts,
                        node.state_type.value,
                        json.dumps(node.metadata),
                    ))

                # 4. Upsert Active Edges
                for edge in snapshot.edges:
                    conn.execute("""
                        INSERT INTO edges (
                            id, device_id, source_id, target_id, signal_type, protocol,
                            expected_endpoint, observed_endpoint, rate, error_rate,
                            queue_size, status, failure_reason, evidence_json,
                            last_seen_ts, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            source_id = excluded.source_id,
                            target_id = excluded.target_id,
                            signal_type = excluded.signal_type,
                            protocol = excluded.protocol,
                            expected_endpoint = excluded.expected_endpoint,
                            observed_endpoint = excluded.observed_endpoint,
                            rate = excluded.rate,
                            error_rate = excluded.error_rate,
                            queue_size = excluded.queue_size,
                            status = excluded.status,
                            failure_reason = excluded.failure_reason,
                            evidence_json = excluded.evidence_json,
                            last_seen_ts = excluded.last_seen_ts,
                            metadata_json = excluded.metadata_json
                    """, (
                        edge.id,
                        dev_id,
                        edge.source_id,
                        edge.target_id,
                        edge.signal_type.value,
                        edge.protocol,
                        edge.expected_endpoint,
                        edge.observed_endpoint,
                        edge.rate,
                        edge.error_rate,
                        edge.queue_size,
                        edge.status.value,
                        edge.failure_reason,
                        json.dumps(edge.evidence),
                        edge.last_seen_ts,
                        json.dumps(edge.metadata),
                    ))

                # 5. Reset and Record Active Discrepancies for Device
                conn.execute("UPDATE discrepancies SET resolved = 1 WHERE device_id = ?", (dev_id,))
                for disc in snapshot.discrepancies:
                    conn.execute("""
                        INSERT INTO discrepancies (
                            id, device_id, component, severity, issue_type,
                            message, expected, observed, remediation, evidence_json,
                            resolved, timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            severity = excluded.severity,
                            message = excluded.message,
                            expected = excluded.expected,
                            observed = excluded.observed,
                            remediation = excluded.remediation,
                            evidence_json = excluded.evidence_json,
                            resolved = 0,
                            timestamp = excluded.timestamp
                    """, (
                        disc.id,
                        disc.device_id,
                        disc.component,
                        disc.severity.value,
                        disc.issue_type,
                        disc.message,
                        disc.expected,
                        disc.observed,
                        disc.remediation,
                        json.dumps(disc.evidence),
                        disc.timestamp or now_ts,
                    ))

    def get_all_devices(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY device_id ASC").fetchall()
            devices = []
            for r in rows:
                devices.append({
                    "device_id": r["device_id"],
                    "name": r["name"],
                    "ip": r["ip"],
                    "deployment_type": r["deployment_type"],
                    "status": r["status"],
                    "last_heartbeat": r["last_heartbeat"],
                    "metadata": json.loads(r["metadata_json"]),
                })
            return devices

    def get_nodes(self, device_id: Optional[str] = None) -> List[TopologyNode]:
        query = "SELECT * FROM nodes"
        params: List[Any] = []
        if device_id:
            query += " WHERE device_id = ?"
            params.append(device_id)
        query += " ORDER BY id ASC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            nodes = []
            for r in rows:
                endpoints_raw = json.loads(r["listen_endpoints_json"])
                endpoints = [ListenEndpoint(**ep) for ep in endpoints_raw]
                nodes.append(TopologyNode(
                    id=r["id"],
                    name=r["name"],
                    device_id=r["device_id"],
                    role=NodeRole(r["role"]),
                    deployment_type=r["deployment_type"],
                    runtime_id=r["runtime_id"],
                    listen_endpoints=endpoints,
                    config_source=r["config_source"],
                    health=HealthStatus(r["health"]),
                    freshness_seconds=r["freshness_seconds"],
                    last_seen_ts=r["last_seen_ts"],
                    state_type=StateType(r["state_type"]),
                    metadata=json.loads(r["metadata_json"]),
                ))
            return nodes

    def get_edges(self, device_id: Optional[str] = None) -> List[TopologyEdge]:
        query = "SELECT * FROM edges"
        params: List[Any] = []
        if device_id:
            query += " WHERE device_id = ?"
            params.append(device_id)
        query += " ORDER BY id ASC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            edges = []
            for r in rows:
                edges.append(TopologyEdge(
                    id=r["id"],
                    source_id=r["source_id"],
                    target_id=r["target_id"],
                    signal_type=SignalType(r["signal_type"]),
                    protocol=r["protocol"],
                    expected_endpoint=r["expected_endpoint"],
                    observed_endpoint=r["observed_endpoint"],
                    rate=r["rate"],
                    error_rate=r["error_rate"],
                    queue_size=r["queue_size"],
                    status=HealthStatus(r["status"]),
                    failure_reason=r["failure_reason"],
                    evidence=json.loads(r["evidence_json"]),
                    last_seen_ts=r["last_seen_ts"],
                    metadata=json.loads(r["metadata_json"]),
                ))
            return edges

    def get_discrepancies(self, device_id: Optional[str] = None) -> List[Discrepancy]:
        query = "SELECT * FROM discrepancies WHERE resolved = 0"
        params: List[Any] = []
        if device_id:
            query += " AND device_id = ?"
            params.append(device_id)
        query += " ORDER BY timestamp DESC"

        with self._get_connection() as conn:
            rows = conn.execute(query, params).fetchall()
            discrepancies = []
            for r in rows:
                discrepancies.append(Discrepancy(
                    id=r["id"],
                    device_id=r["device_id"],
                    component=r["component"],
                    severity=HealthStatus(r["severity"]),
                    issue_type=r["issue_type"],
                    message=r["message"],
                    expected=r["expected"],
                    observed=r["observed"],
                    remediation=r["remediation"],
                    evidence=json.loads(r["evidence_json"]),
                    timestamp=r["timestamp"],
                ))
            return discrepancies

    def mark_stale_devices(self, threshold_seconds: float = 60.0) -> List[str]:
        """Detect and mark devices that have missed heartbeats as STALE."""
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=threshold_seconds)
        cutoff_iso = cutoff.isoformat()

        stale_devices: List[str] = []
        with self._get_connection() as conn:
            with conn:
                rows = conn.execute(
                    "SELECT device_id FROM devices WHERE last_heartbeat < ? AND status != 'STALE'",
                    (cutoff_iso,)
                ).fetchall()
                stale_devices = [r["device_id"] for r in rows]

                if stale_devices:
                    conn.executemany(
                        "UPDATE devices SET status = 'STALE' WHERE device_id = ?",
                        [(d,) for d in stale_devices]
                    )
                    conn.executemany(
                        "UPDATE nodes SET health = 'STALE' WHERE device_id = ?",
                        [(d,) for d in stale_devices]
                    )
                    conn.executemany(
                        "UPDATE edges SET status = 'STALE', failure_reason = 'Device heartbeat timed out' WHERE device_id = ?",
                        [(d,) for d in stale_devices]
                    )
        return stale_devices
