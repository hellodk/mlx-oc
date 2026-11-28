"""Deep Runtime Inspector for VictoriaMetrics Agent (vmagent).

Inspects vmagent process state, CLI flags, network connections,
effective scrape targets (/api/v1/targets), internal metrics (/metrics),
persistent queue spooling, and remote-write delivery status.
"""

from __future__ import annotations

import re
import urllib.request
import urllib.error
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import psutil  # type: ignore


@dataclass
class ScrapeTargetStatus:
    job: str
    instance: str
    endpoint: str
    health: str  # "up" or "down"
    last_error: str = ""
    last_scrape_duration_seconds: float = 0.0
    scraped_samples: int = 0
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class RemoteWriteStatus:
    destination_url: str
    pending_bytes: int = 0
    parts_on_disk: int = 0
    packets_sent_total: float = 0.0
    packets_dropped_total: float = 0.0
    conn_errors_total: float = 0.0
    retries_total: float = 0.0
    is_failing: bool = False
    is_backed_up: bool = False
    failure_reason: str = ""


@dataclass
class VMAgentInspectionReport:
    is_running: bool = False
    pid: Optional[int] = None
    listen_port: int = 8429
    config_file_path: str = ""
    persistent_queue_path: str = ""
    configured_remote_writes: List[str] = field(default_factory=list)
    targets: List[ScrapeTargetStatus] = field(default_factory=list)
    remote_write_status: Dict[str, RemoteWriteStatus] = field(default_factory=dict)
    active_tcp_connections: List[Dict[str, Any]] = field(default_factory=list)
    discrepancies: List[Dict[str, Any]] = field(default_factory=list)
    internal_metrics: Dict[str, float] = field(default_factory=dict)


class VMAgentInspector:
    """Performs deep inspection of running or local vmagent instance."""

    def __init__(self, vmagent_url: str = "http://127.0.0.1:8429", timeout: float = 2.0):
        self.vmagent_url = vmagent_url.rstrip("/")
        self.timeout = timeout

    def inspect(self) -> VMAgentInspectionReport:
        report = VMAgentInspectionReport()
        self._inspect_process(report)
        self._query_targets_api(report)
        self._query_metrics_api(report)
        self._diagnose_reconciliation(report)
        return report

    def _inspect_process(self, report: VMAgentInspectionReport) -> None:
        """Inspect running vmagent process, CLI flags, and network sockets via psutil."""
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = proc.info.get("cmdline") or []
                    cmd_str = " ".join(cmdline)
                    if "vmagent" in proc.info.get("name", "") or "vmagent" in cmd_str:
                        report.is_running = True
                        report.pid = proc.info.get("pid")

                        # Parse CLI flags
                        for i, arg in enumerate(cmdline):
                            if arg == "-promscrape.config" and i + 1 < len(cmdline):
                                report.config_file_path = cmdline[i + 1]
                            elif arg.startswith("-promscrape.config="):
                                report.config_file_path = arg.split("=", 1)[1]
                            elif arg == "-remoteWrite.url" and i + 1 < len(cmdline):
                                report.configured_remote_writes.append(cmdline[i + 1])
                            elif arg.startswith("-remoteWrite.url="):
                                report.configured_remote_writes.append(arg.split("=", 1)[1])
                            elif arg == "-remoteWrite.tmpDataPath" and i + 1 < len(cmdline):
                                report.persistent_queue_path = cmdline[i + 1]
                            elif arg.startswith("-remoteWrite.tmpDataPath="):
                                report.persistent_queue_path = arg.split("=", 1)[1]
                            elif arg == "-httpListenAddr" and i + 1 < len(cmdline):
                                val = cmdline[i + 1]
                                if ":" in val:
                                    report.listen_port = int(val.split(":")[-1])
                            elif arg.startswith("-httpListenAddr="):
                                val = arg.split("=", 1)[1]
                                if ":" in val:
                                    report.listen_port = int(val.split(":")[-1])

                        # Inspect active sockets
                        conn_fn = getattr(proc, "net_connections", getattr(proc, "connections", None))
                        if conn_fn is not None:
                            try:
                                for c in conn_fn(kind="inet"):
                                    conn_info = {
                                        "status": getattr(c, "status", ""),
                                        "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                                        "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                                    }
                                    report.active_tcp_connections.append(conn_info)
                            except Exception:
                                pass
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:
            pass

    def _query_targets_api(self, report: VMAgentInspectionReport) -> None:
        """Fetch live scrape target status from /api/v1/targets."""
        url = f"{self.vmagent_url}/api/v1/targets"
        req = urllib.request.Request(url, headers={"User-Agent": "mlx-topology-inspector/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    targets_data = data.get("data", {}).get("activeTargets", [])
                    for t in targets_data:
                        job = t.get("labels", {}).get("job", "unknown")
                        instance = t.get("labels", {}).get("instance", "")
                        scrape_url = t.get("scrapeUrl", "")
                        health = t.get("health", "down")
                        last_err = t.get("lastError", "")
                        last_dur = float(t.get("lastScrapeDuration", 0.0))
                        samples = int(t.get("scrapedSamples", 0))
                        report.targets.append(ScrapeTargetStatus(
                            job=job,
                            instance=instance,
                            endpoint=scrape_url,
                            health=health,
                            last_error=last_err,
                            last_scrape_duration_seconds=last_dur,
                            scraped_samples=samples,
                            labels=t.get("labels", {}),
                        ))
        except Exception:
            pass

    def _query_metrics_api(self, report: VMAgentInspectionReport) -> None:
        """Scrape /metrics to evaluate queues, remote-write throughput, and error counters."""
        url = f"{self.vmagent_url}/metrics"
        req = urllib.request.Request(url, headers={"User-Agent": "mlx-topology-inspector/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status == 200:
                    text = resp.read().decode("utf-8")
                    self._parse_metrics_text(text, report)
        except Exception:
            pass

    def _parse_metrics_text(self, text: str, report: VMAgentInspectionReport) -> None:
        """Extract vmagent internal Prometheus counters and gauges."""
        pending_bytes = 0
        parts_on_disk = 0
        packets_sent = 0.0
        packets_dropped = 0.0
        conn_errors = 0.0
        retries = 0.0

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split()
            if len(parts) < 2:
                continue

            metric_name_raw = parts[0]
            try:
                metric_val = float(parts[1])
            except ValueError:
                continue

            # Store general metrics
            m_base = metric_name_raw.split("{")[0]
            report.internal_metrics[m_base] = metric_val

            if "vmagent_remotewrite_pending_data_bytes" in metric_name_raw:
                pending_bytes += int(metric_val)
            elif "vmagent_remotewrite_parts_on_disk" in metric_name_raw:
                parts_on_disk += int(metric_val)
            elif "vmagent_remotewrite_packets_sent_total" in metric_name_raw:
                packets_sent += metric_val
            elif "vmagent_remotewrite_packets_dropped_total" in metric_name_raw:
                packets_dropped += metric_val
            elif "vmagent_remotewrite_conn_errors_total" in metric_name_raw:
                conn_errors += metric_val
            elif "vmagent_remotewrite_retries_count_total" in metric_name_raw:
                retries += metric_val

        # Populate remote-write status per configured URL or default
        primary_url = report.configured_remote_writes[0] if report.configured_remote_writes else "unknown"
        is_failing = conn_errors > 0 or packets_dropped > 0
        is_backed_up = pending_bytes > (10 * 1024 * 1024) or parts_on_disk > 0  # >10MB or spilling to disk

        failure_reason = ""
        if conn_errors > 0:
            failure_reason = f"Connection refused or network timeout ({int(conn_errors)} errors)"
        elif packets_dropped > 0:
            failure_reason = f"Queue overflow; dropped {int(packets_dropped)} sample packets"
        elif is_backed_up:
            failure_reason = f"Remote-write queue backed up ({pending_bytes / (1024*1024):.1f} MB, {parts_on_disk} parts on disk)"

        report.remote_write_status[primary_url] = RemoteWriteStatus(
            destination_url=primary_url,
            pending_bytes=pending_bytes,
            parts_on_disk=parts_on_disk,
            packets_sent_total=packets_sent,
            packets_dropped_total=packets_dropped,
            conn_errors_total=conn_errors,
            retries_total=retries,
            is_failing=is_failing,
            is_backed_up=is_backed_up,
            failure_reason=failure_reason,
        )

    def _diagnose_reconciliation(self, report: VMAgentInspectionReport) -> None:
        """Detect scrape failures, queue backlogs, and remote-write delivery issues."""
        # 1. Check if vmagent is down
        if not report.is_running and not report.targets:
            report.discrepancies.append({
                "severity": "CRITICAL",
                "component": "vmagent",
                "issue_type": "COLLECTOR_UNAVAILABLE",
                "message": "vmagent process is not running on edge node",
                "remediation": "Start vmagent via mlx_monitoring_bundle.py start or check logs/vmagent.bundle.log",
            })
            return

        # 2. Check Scrape Target Failures
        for t in report.targets:
            if t.health == "down" or t.last_error:
                report.discrepancies.append({
                    "severity": "CRITICAL",
                    "component": t.job,
                    "issue_type": "SCRAPE_FAILURE",
                    "message": f"vmagent scrape target {t.job} ({t.instance}) is DOWN: {t.last_error}",
                    "expected": f"HTTP 200 on {t.endpoint}",
                    "observed": f"Scrape failed: {t.last_error or 'health=down'}",
                    "remediation": f"Verify process for {t.job} is running and listening on {t.instance}",
                    "evidence": {"endpoint": t.endpoint, "last_error": t.last_error, "duration": t.last_scrape_duration_seconds},
                })

        # 3. Check Remote-Write Failures
        for rw_url, rw_status in report.remote_write_status.items():
            if rw_status.is_failing:
                report.discrepancies.append({
                    "severity": "CRITICAL",
                    "component": "vmagent_remote_write",
                    "issue_type": "REMOTE_WRITE_FAILURE",
                    "message": f"vmagent remote_write to {rw_url} is failing: {rw_status.failure_reason}",
                    "expected": f"HTTP 200 ingestion at {rw_url}",
                    "observed": rw_status.failure_reason,
                    "remediation": "Check VictoriaMetrics TSDB status, network reachability, and firewall rules",
                    "evidence": {
                        "destination": rw_url,
                        "conn_errors": rw_status.conn_errors_total,
                        "dropped_packets": rw_status.packets_dropped_total,
                        "pending_bytes": rw_status.pending_bytes,
                    },
                })
            elif rw_status.is_backed_up:
                report.discrepancies.append({
                    "severity": "DEGRADED",
                    "component": "vmagent_remote_write",
                    "issue_type": "REMOTE_WRITE_QUEUE_BACKUP",
                    "message": f"vmagent queue backed up to {rw_url} ({rw_status.pending_bytes / 1024:.0f} KB in queue, {rw_status.parts_on_disk} parts on disk)",
                    "expected": "Queue size < 5 MB and 0 parts on disk",
                    "observed": f"{rw_status.pending_bytes / (1024*1024):.1f} MB buffered",
                    "remediation": "Verify VictoriaMetrics ingestion throughput and network bandwidth",
                    "evidence": {"pending_bytes": rw_status.pending_bytes, "parts_on_disk": rw_status.parts_on_disk},
                })
