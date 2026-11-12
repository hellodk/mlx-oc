#!/usr/bin/env python3
"""
diagnostics.py
==============
Unified diagnostic engine and service configuration inspector for the
MLX Monitoring Bundle (Central Observability Stack & Edge Monitoring Stack).

Provides:
  - doctor           : Automated end-to-end telemetry pipeline traceroute
  - review-central   : Comprehensive health & configuration audit of Central services
  - review-edge      : Comprehensive health & configuration audit of Edge exporters
  - review-vm        : Granular VictoriaMetrics TSDB & storage audit
  - review-alertmanager : Alertmanager routing & webhook audit
  - review-vmalert   : vmalert rules & firing alerts audit
  - review-otel      : OpenTelemetry Collector pipelines & log audit
  - review-opik      : Opik distributed tracing suite audit
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = SCRIPT_DIR.parent
EDGE_DIR = BUNDLE_DIR / "edge"
CENTRAL_DIR = BUNDLE_DIR / "central"

# ---------------------------------------------------------------------------
# Helpers: Timezone, Network, and Process Primitives
# ---------------------------------------------------------------------------

def format_ist(ts: float | int | str | None) -> str:
    """Format any unix or ISO timestamp to IST (Asia/Kolkata, UTC+5:30)."""
    if ts is None:
        return "N/A"
    try:
        ist_tz = timezone(timedelta(hours=5, minutes=30))
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ist_tz)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(ist_tz)
        return dt.strftime("%d %b %Y, %H:%M:%S IST")
    except Exception:
        return str(ts)


def get_local_ips() -> set[str]:
    """Retrieve all active local IPv4 addresses without external assumptions."""
    ips = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return ips


def probe_tcp(addr: str, timeout: float = 0.8) -> tuple[bool, float | str]:
    """Test TCP socket connection and measure latency in milliseconds."""
    try:
        host, port = addr.split(":") if ":" in addr else ("127.0.0.1", addr)
        t0 = time.time()
        with socket.create_connection((host, int(port)), timeout=timeout):
            latency = (time.time() - t0) * 1000
            return True, latency
    except Exception as exc:
        return False, str(exc)


def probe_http(url: str, timeout: float = 2.0, headers: dict | None = None) -> tuple[int, str]:
    """Perform HTTP request returning (status_code, body_or_error_string)."""
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "mlx-diagnostics/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8", errors="replace")
            return resp.status, content
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)
        return exc.code, err_body
    except Exception as exc:
        return 0, str(exc)


# ---------------------------------------------------------------------------
# Configuration Loader (Zero Hardcoding)
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Load configuration from nodes.yml, edge.env, central/.env, and environment."""
    nodes_path = EDGE_DIR / "config" / "nodes.yml"
    edge_env_path = EDGE_DIR / "config" / "edge.env"
    central_env_path = CENTRAL_DIR / ".env"

    nodes = {}
    if nodes_path.is_file():
        try:
            nodes = yaml.safe_load(nodes_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass

    edge_env = {}
    if edge_env_path.is_file():
        for line in edge_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                edge_env[k.strip()] = v.strip().strip("\"'")

    central_env = {}
    if central_env_path.is_file():
        for line in central_env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                central_env[k.strip()] = v.strip().strip("\"'")

    central_cfg = nodes.get("central", {})
    cvm = os.environ.get("CENTRAL_VICTORIA_METRICS") or central_cfg.get("victoria_metrics", "127.0.0.1:8428")
    cotel = os.environ.get("CENTRAL_OTEL") or central_cfg.get("otel", "127.0.0.1:4318")
    copik = os.environ.get("CENTRAL_OPIK") or central_cfg.get("opik", "127.0.0.1:32173")

    central_host = cvm.split(":")[0] if ":" in cvm else "127.0.0.1"

    macbooks = [nb for nb in nodes.get("macbooks", []) if isinstance(nb, dict) and nb.get("enabled", True)]

    return {
        "nodes": nodes,
        "edge_env": edge_env,
        "central_env": central_env,
        "central_host": central_host,
        "cvm": cvm,
        "cotel": cotel,
        "copik": copik,
        "cgrafana": f"{central_host}:3000",
        "cvmalert": f"{central_host}:8880",
        "calertmanager": f"{central_host}:9093",
        "macbooks": macbooks,
        "grafana_user": os.environ.get("GRAFANA_ADMIN_USER") or central_env.get("GRAFANA_ADMIN_USER", "admin"),
        "grafana_password": os.environ.get("GRAFANA_ADMIN_PASSWORD") or central_env.get("GRAFANA_ADMIN_PASSWORD", "changeme"),
        "grafana_anon": os.environ.get("GRAFANA_ANONYMOUS_ENABLED") or central_env.get("GRAFANA_ANONYMOUS_ENABLED", "false"),
    }


# ---------------------------------------------------------------------------
# Container Engine Primitives
# ---------------------------------------------------------------------------

def detect_container_engine() -> tuple[str | None, str, bool]:
    """Detect docker or podman and verify if daemon is active."""
    for eng in ("docker", "podman"):
        if shutil.which(eng) is not None:
            try:
                proc = subprocess.run([eng, "info", "--format", "{{.ServerVersion}}"],
                                      capture_output=True, text=True, timeout=5)
                if proc.returncode == 0:
                    return eng, proc.stdout.strip(), True
                return eng, f"daemon inaccessible: {proc.stderr.strip()}", False
            except Exception as exc:
                return eng, str(exc), False
    return None, "neither docker nor podman installed", False


def inspect_container(engine: str | None, container_names: str | list[str]) -> dict:
    """Inspect container state returning status, running bool, and started timestamp."""
    if not engine:
        return {"status": "NO_ENGINE", "running": False, "started_at": "", "name": ""}
    if isinstance(container_names, str):
        candidates = [container_names]
    else:
        candidates = list(container_names)
    for cname in candidates:
        try:
            proc = subprocess.run(
                [engine, "inspect", cname, "--format", "{{.State.Status}}|{{.State.Running}}|{{.State.StartedAt}}"],
                capture_output=True, text=True, timeout=5
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parts = proc.stdout.strip().split("|", 2)
                if len(parts) == 3:
                    status, running, started = parts
                    return {"status": status.upper(), "running": running.lower() == "true", "started_at": started, "name": cname}
        except Exception:
            pass
    return {"status": "NOT_DEPLOYED", "running": False, "started_at": "", "name": candidates[0] if candidates else ""}


# ---------------------------------------------------------------------------
# VictoriaMetrics Query Helper
# ---------------------------------------------------------------------------

def query_vm(cvm_endpoint: str, query: str) -> list[dict] | None:
    """Execute MetricsQL query against VictoriaMetrics."""
    try:
        q = urllib.parse.quote(query)
        url = f"http://{cvm_endpoint}/api/v1/query?query={q}"
        status, body = probe_http(url, timeout=2.5)
        if status == 200:
            data = json.loads(body)
            return data.get("data", {}).get("result", [])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Command: Doctor (Automated End-to-End Pipeline Traceroute)
# ---------------------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    print("=" * 76)
    print("            MLX Observability Pipeline Doctor (Data Flow Tracer)")
    print("=" * 76)
    print("Configured Endpoints:")
    print(f"  • Central VictoriaMetrics : http://{cfg['cvm']}")
    print(f"  • Central Grafana         : http://{cfg['cgrafana']}")
    print(f"  • Central Alertmanager    : http://{cfg['calertmanager']}")
    print(f"  • Central vmalert         : http://{cfg['cvmalert']}")
    print(f"  • Central OTel Collector  : http://{cfg['cotel']}")
    print(f"  • Central Opik Gateway    : http://{cfg['copik']}")
    print("-" * 76)

    stages_passed = True
    breaks: list[dict] = []

    # -----------------------------------------------------------------------
    # Stage 1: Central Infrastructure & TSDB Ingestion
    # -----------------------------------------------------------------------
    engine, eng_ver, daemon_ok = detect_container_engine()
    vm_ok, vm_lat = probe_tcp(cfg["cvm"])
    
    stage1_status = "PASS"
    stage1_notes = []

    if not vm_ok:
        stage1_status = "FAIL"
        stages_passed = False
        breaks.append({
            "stage": "Stage 1: Central Ingestion (VictoriaMetrics)",
            "issue": f"Cannot connect to VictoriaMetrics at {cfg['cvm']} ({vm_lat}).",
            "impact": "No edge metrics can be stored or visualized.",
            "fix": "On central host, check container status: './scripts/deploy.sh review-vm' or run './scripts/deploy.sh central'."
        })
    else:
        stage1_notes.append(f"reachable in {vm_lat:.1f}ms")
        series_data = query_vm(cfg["cvm"], "count({__name__=~\".+\"})")
        total_series = series_data[0]["value"][1] if series_data else "0"
        stage1_notes.append(f"{total_series} total series active")

    print(f"[Stage 1/5] Central TSDB Ingestion   : {stage1_status} ({', '.join(stage1_notes) if stage1_notes else 'unreachable'})")

    # -----------------------------------------------------------------------
    # Stage 2: Edge Nodes Telemetry Generation & vmagent Shipping
    # -----------------------------------------------------------------------
    target_nodes = cfg["macbooks"]
    if args.node:
        target_nodes = [nb for nb in target_nodes if nb.get("id") == args.node]

    stage2_status = "PASS"
    stage2_details = []

    if not target_nodes:
        print("[Stage 2/5] Edge Exporters & Nodes   : WARN (no enabled macbook nodes found in nodes.yml)")
    else:
        for nb in target_nodes:
            nb_id = nb.get("id", "unknown")
            nb_ip = nb.get("ip", "unknown")

            # Query VictoriaMetrics for up targets reported by vmagent
            up_data = query_vm(cfg["cvm"], f"up{{macbook=\"{nb_id}\"}}") if vm_ok else None
            recency_data = query_vm(cfg["cvm"], f"time() - max(timestamp(up{{macbook=\"{nb_id}\"}}))") if vm_ok else None
            recency_sec = float(recency_data[0]["value"][1]) if recency_data else None

            if up_data is None or len(up_data) == 0:
                stage2_status = "FAIL"
                stages_passed = False
                breaks.append({
                    "stage": f"Stage 2: Edge Node '{nb_id}' Telemetry Shipping",
                    "issue": f"VictoriaMetrics has received ZERO metrics for macbook '{nb_id}' (IP: {nb_ip}).",
                    "impact": "Grafana dashboards for this node will be completely empty ('No Data').",
                    "fix": f"On node '{nb_id}' ({nb_ip}), run './scripts/deploy.sh review-vmagent' and start edge services with './scripts/deploy.sh edge daemon'."
                })
                stage2_details.append(f"node {nb_id}: NO DATA RECEIVED")
            else:
                up_jobs = {row["metric"].get("job"): row["value"][1] for row in up_data}
                down_jobs = [j for j, val in up_jobs.items() if val != "1"]
                
                recency_str = f"{recency_sec:.1f}s ago" if recency_sec is not None else "unknown"
                if recency_sec is not None and recency_sec > 300:
                    stage2_status = "FAIL"
                    stages_passed = False
                    breaks.append({
                        "stage": f"Stage 2: Edge Node '{nb_id}' Data Flow Stalled",
                        "issue": f"Last metric from '{nb_id}' arrived {recency_str} (stale > 300s).",
                        "impact": "Node has stopped streaming real-time metrics.",
                        "fix": f"On node '{nb_id}' ({nb_ip}), check exporter health: './scripts/deploy.sh edge status'."
                    })
                    stage2_details.append(f"node {nb_id}: STALLED ({recency_str})")
                elif down_jobs:
                    stage2_status = "WARN"
                    breaks.append({
                        "stage": f"Stage 2: Exporter Outage on '{nb_id}'",
                        "issue": f"vmagent reported {len(down_jobs)} exporter(s) DOWN: {', '.join(down_jobs)}.",
                        "impact": "Panels relying on these exporters will be empty.",
                        "fix": f"On node '{nb_id}', restart exporters: './scripts/deploy.sh edge stop && ./scripts/deploy.sh edge daemon'."
                    })
                    stage2_details.append(f"node {nb_id}: {len(up_jobs)-len(down_jobs)}/{len(up_jobs)} UP ({', '.join(down_jobs)} DOWN)")
                else:
                    stage2_details.append(f"node {nb_id}: 5/5 UP (last sample {recency_str})")

        print(f"[Stage 2/5] Edge Exporters & Nodes   : {stage2_status} ({'; '.join(stage2_details)})")

    # -----------------------------------------------------------------------
    # Stage 3: Alert Evaluation (vmalert & Alertmanager)
    # -----------------------------------------------------------------------
    va_ok, va_lat = probe_tcp(cfg["cvmalert"])
    am_ok, am_lat = probe_tcp(cfg["calertmanager"])
    stage3_status = "PASS"
    stage3_notes = []

    if va_ok:
        va_h_status, _ = probe_http(f"http://{cfg['cvmalert']}/health")
        if va_h_status == 200:
            stage3_notes.append("vmalert 200 OK")
        else:
            stage3_status = "WARN"
            stage3_notes.append(f"vmalert HTTP {va_h_status}")
    else:
        stage3_status = "WARN"
        stage3_notes.append("vmalert port 8880 unreachable")

    if am_ok:
        am_h_status, _ = probe_http(f"http://{cfg['calertmanager']}/-/healthy")
        if am_h_status == 200:
            stage3_notes.append("alertmanager 200 OK")
        else:
            stage3_status = "WARN"
            stage3_notes.append(f"alertmanager HTTP {am_h_status}")
    else:
        stage3_status = "WARN"
        stage3_notes.append("alertmanager port 9093 unreachable")

    print(f"[Stage 3/5] Alerting & Rule Engine   : {stage3_status} ({', '.join(stage3_notes)})")

    # -----------------------------------------------------------------------
    # Stage 4: OTel Logs & Opik Tracing Pipeline
    # -----------------------------------------------------------------------
    otel_ok, _ = probe_tcp(cfg["cotel"])
    opik_ok, _ = probe_tcp(cfg["copik"])
    stage4_status = "PASS"
    stage4_notes = []

    if otel_ok:
        stage4_notes.append("OTel 4318 listening")
    else:
        stage4_status = "WARN"
        stage4_notes.append("OTel 4318 unreachable")

    if opik_ok:
        opik_h, _ = probe_http(f"http://{cfg['copik']}/health")
        if opik_h == 200:
            stage4_notes.append("Opik UI 200 OK")
        else:
            stage4_notes.append(f"Opik HTTP {opik_h}")
    else:
        stage4_notes.append("Opik not active (use --opik to enable)")

    print(f"[Stage 4/5] OTel Logs & Opik Traces  : {stage4_status} ({', '.join(stage4_notes)})")

    # -----------------------------------------------------------------------
    # Stage 5: Grafana Datasource & Dashboard Query Matching
    # -----------------------------------------------------------------------
    gf_ok, gf_lat = probe_tcp(cfg["cgrafana"])
    stage5_status = "PASS"
    stage5_notes = []

    if not gf_ok:
        stage5_status = "FAIL"
        stages_passed = False
        breaks.append({
            "stage": "Stage 5: Grafana Service",
            "issue": f"Grafana is not reachable on http://{cfg['cgrafana']}.",
            "impact": "Dashboards cannot be accessed.",
            "fix": "Run './scripts/deploy.sh review-grafana' to inspect container state and logs."
        })
    else:
        # Check HTTP health
        h_code, h_body = probe_http(f"http://{cfg['cgrafana']}/api/health")
        if h_code != 200:
            stage5_status = "FAIL"
            stages_passed = False
            breaks.append({
                "stage": "Stage 5: Grafana Health",
                "issue": f"Grafana /api/health returned HTTP {h_code}.",
                "impact": "Grafana server process is unhealthy.",
                "fix": "Restart central stack with './scripts/deploy.sh central'."
            })
        else:
            stage5_notes.append("HTTP 200 OK")

            # Check Authentication
            auth_header = "Basic " + base64.b64encode(f"{cfg['grafana_user']}:{cfg['grafana_password']}".encode()).decode()
            auth_code, auth_body = probe_http(f"http://{cfg['cgrafana']}/api/users", headers={"Authorization": auth_header})
            
            if auth_code == 401:
                stage5_status = "FAIL"
                stages_passed = False
                breaks.append({
                    "stage": "Stage 5: Grafana Authentication Mismatch",
                    "issue": "Configured admin password in central/.env does not match Grafana database (HTTP 401 Unauthorized).",
                    "impact": "Users are locked out and automated provisioning queries fail.",
                    "fix": "Run './scripts/deploy.sh review-grafana' to live-synchronize the admin password."
                })
            elif auth_code != 200:
                stage5_status = "WARN"
                stage5_notes.append(f"Auth check HTTP {auth_code}")
            else:
                stage5_notes.append("Auth OK")

                # Check Datasource connection
                ds_code, ds_body = probe_http(f"http://{cfg['cgrafana']}/api/datasources/name/VictoriaMetrics", headers={"Authorization": auth_header})
                if ds_code == 200:
                    try:
                        ds_info = json.loads(ds_body)
                        ds_uid = ds_info.get("uid")
                        ds_url = ds_info.get("url", "http://victoria-metrics:8428")
                        dh_code, dh_body = probe_http(f"http://{cfg['cgrafana']}/api/datasources/uid/{ds_uid}/health", headers={"Authorization": auth_header})
                        if dh_code == 200 and json.loads(dh_body).get("status") == "OK":
                            stage5_notes.append("Datasource VictoriaMetrics OK")
                        else:
                            stage5_status = "FAIL"
                            stages_passed = False
                            breaks.append({
                                "stage": "Stage 5: Grafana VictoriaMetrics Datasource",
                                "issue": f"Grafana cannot query VictoriaMetrics inside container network ({dh_body}). Configured URL: {ds_url}.",
                                "impact": "All panels in all dashboards will show 'No Data'.",
                                "fix": f"Add 'links: - victoria-metrics' to central-grafana in docker-compose.central.yml, or set VICTORIAMETRICS_URL=http://{cfg['cvm']} in central/.env and recreate Grafana."
                            })
                    except Exception:
                        pass
                else:
                    stage5_status = "FAIL"
                    stages_passed = False
                    breaks.append({
                        "stage": "Stage 5: Missing Datasource",
                        "issue": "Datasource 'VictoriaMetrics' not found in Grafana.",
                        "impact": "Dashboards have no backend datasource.",
                        "fix": "Verify central/config/grafana/datasources.yaml provisioning."
                    })

    print(f"[Stage 5/5] Grafana & Query Pipeline : {stage5_status} ({', '.join(stage5_notes) if stage5_notes else 'unreachable'})")
    print("-" * 76)

    # -----------------------------------------------------------------------
    # Triage Findings & Actionable Fixes
    # -----------------------------------------------------------------------
    if stages_passed and not breaks:
        print("Diagnosis: ALL PIPELINE GATES PASSED.")
        print("Telemetry is flowing in real-time from Edge Exporters -> VictoriaMetrics -> Grafana.")
        print("=" * 76)
        return 0

    print("PIPELINE BREAK IDENTIFIED:")
    for idx, b in enumerate(breaks, 1):
        print(f"\n[{idx}] {b['stage']}")
        print(f"    • Root Issue     : {b['issue']}")
        print(f"    • User Impact    : {b['impact']}")
        print(f"    • Actionable Fix : {b['fix']}")
    print("=" * 76)
    return 1


# ---------------------------------------------------------------------------
# Command: Review Central (Comprehensive Central Audit)
# ---------------------------------------------------------------------------

def cmd_review_central(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine, eng_ver, daemon_ok = detect_container_engine()

    print("=" * 76)
    print("                Central Observability Stack Health & Config Audit")
    print("=" * 76)
    print(f"Container Engine  : {engine.capitalize() if engine else 'None'} ({eng_ver})")
    print(f"Host Endpoints    : VM={cfg['cvm']}, Grafana={cfg['cgrafana']}, Alertmanager={cfg['calertmanager']}")
    print("-" * 76)

    services = [
        ("VictoriaMetrics", "central-victoriametrics", cfg["cvm"], "/health", 200),
        ("Grafana", "central-grafana", cfg["cgrafana"], "/api/health", 200),
        ("vmalert", "central-vmalert", cfg["cvmalert"], "/health", 200),
        ("Alertmanager", "central-alertmanager", cfg["calertmanager"], "/-/healthy", 200),
        ("OTel Collector", ["central-otelcol", "central-otel-collector"], cfg["cotel"], "", None),
        ("Opik Gateway", "central-opik-frontend", cfg["copik"], "/health", 200),
    ]

    all_ok = True
    for name, cname, addr, health_path, expected_code in services:
        cinfo = inspect_container(engine, cname)
        cstatus = cinfo["status"]
        uptime = format_ist(cinfo["started_at"]) if cinfo["started_at"] else ""

        tcp_ok, lat = probe_tcp(addr)
        http_ok = True
        h_str = ""
        if tcp_ok and health_path:
            h_code, _ = probe_http(f"http://{addr}{health_path}")
            http_ok = (h_code == expected_code)
            h_str = f"HTTP {h_code}" if http_ok else f"HTTP {h_code} (FAILED)"

        is_healthy = cinfo["running"] and tcp_ok and http_ok
        if not is_healthy and name != "Opik Gateway":
            all_ok = False

        status_tag = "[OK]" if is_healthy else ("[DISABLED]" if name == "Opik Gateway" and cstatus == "NOT_DEPLOYED" else "[FAIL]")
        port_info = f"{addr} ({f'%.1fms' % lat if tcp_ok else 'unreachable'})"
        print(f"  {status_tag:<11} {name:<18} -> {cstatus:<12} | {port_info} {h_str}")

    print("-" * 76)
    if all_ok:
        print("Central Status: ALL ACTIVE SERVICES ARE HEALTHY.")
    else:
        print("Central Status: ONE OR MORE SERVICES FAILED. Run './scripts/deploy.sh doctor' for triage.")
    print("=" * 76)
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# Command: Review Edge (Comprehensive Edge Exporter Audit)
# ---------------------------------------------------------------------------

def cmd_review_edge(args: argparse.Namespace) -> int:
    cfg = load_config()
    print("=" * 76)
    print("                  Edge Monitoring Stack Health & Config Audit")
    print("=" * 76)

    nodes = cfg["macbooks"]
    if args.node:
        nodes = [nb for nb in nodes if nb.get("id") == args.node]

    if not nodes:
        print("No enabled edge nodes found in nodes.yml.")
        print("=" * 76)
        return 1

    local_ips = get_local_ips()
    all_ok = True

    for nb in nodes:
        nb_id = nb.get("id", "unknown")
        nb_ip = nb.get("ip", "127.0.0.1")
        is_local = (nb_ip in local_ips) or (nb_ip == "127.0.0.1")

        print(f"Node: {nb_id} (IP: {nb_ip}, Role: {'LOCAL HOST' if is_local else 'REMOTE NODE'})")
        print(f"  • Pinned Model   : {nb.get('model', 'none')} ({nb.get('model_path', 'none')})")

        # Exporters table
        exporters = [
            ("mlx-proxy", cfg["edge_env"].get("PROXY_LISTEN", "127.0.0.1:8080"), "/metrics"),
            ("mlx-hw", cfg["edge_env"].get("HW_LISTEN", "127.0.0.1:9102"), "/metrics"),
            ("mlx-kv", cfg["edge_env"].get("KV_LISTEN", "127.0.0.1:9104"), "/metrics"),
            ("mlx-supervisor", cfg["edge_env"].get("SUPERVISOR_LISTEN", "127.0.0.1:9105"), "/metrics"),
            ("mlx-logtailer", cfg["edge_env"].get("LOGTAILER_LISTEN", "127.0.0.1:9106"), "/metrics"),
        ]

        for exp_name, listen, path in exporters:
            # If checking local host, probe directly
            probe_addr = listen
            if not is_local:
                port = listen.split(":")[-1]
                probe_addr = f"{nb_ip}:{port}"

            tcp_ok, lat = probe_tcp(probe_addr, timeout=0.5)
            metric_count = 0
            details = "port not listening"
            if tcp_ok:
                h_code, h_body = probe_http(f"http://{probe_addr}{path}", timeout=1.0)
                if h_code == 200:
                    metric_count = sum(1 for line in h_body.splitlines() if line.startswith("mlx_"))
                    details = f"{metric_count} metrics"
                else:
                    details = f"HTTP {h_code}"

            if tcp_ok and metric_count > 0:
                status_tag = "[OK]"
            elif not tcp_ok and not is_local:
                # Edge exporters bind to loopback for security; check VictoriaMetrics telemetry ingestion
                vm_data = query_vm(cfg["cvm"], f'up{{macbook="{nb_id}", job="{exp_name}"}}')
                if vm_data and len(vm_data) > 0 and vm_data[0].get("value", [0, "0"])[1] == "1":
                    rec = query_vm(cfg["cvm"], f'time() - timestamp(up{{macbook="{nb_id}", job="{exp_name}"}})')
                    sec_str = f"last sample {float(rec[0]['value'][1]):.1f}s ago" if rec else "active"
                    status_tag = "[OK]"
                    details = f"scraped by local vmagent (UP in TSDB, {sec_str})"
                else:
                    status_tag = "[FAIL]"
            else:
                status_tag = "[FAIL]"

            if status_tag == "[FAIL]":
                all_ok = False
            print(f"    {status_tag:<8} {exp_name:<16} -> {probe_addr:<22} ({details})")

        # Check upstream model server
        srv_port = cfg["edge_env"].get("MLX_SERVER_PORT", "11234")
        srv_addr = f"{'127.0.0.1' if is_local else nb_ip}:{srv_port}"
        srv_ok, _ = probe_tcp(srv_addr, timeout=0.5)
        srv_info = "server unreachable"
        if srv_ok:
            s_code, s_body = probe_http(f"http://{srv_addr}/v1/models", timeout=1.0)
            if s_code == 200:
                try:
                    models = json.loads(s_body).get("data", [])
                    m_names = [m.get("id") for m in models]
                    srv_info = f"serving: {', '.join(m_names)}"
                except Exception:
                    srv_info = "model endpoint OK"
        elif not srv_ok and not is_local:
            vm_up = query_vm(cfg["cvm"], f'mlx_up{{macbook="{nb_id}"}}')
            if vm_up and len(vm_up) > 0 and vm_up[0].get("value", [0, "0"])[1] == "1":
                srv_ok = True
                srv_info = f"healthy (upstream confirmed by proxy in TSDB, model: {nb.get('model', 'none')})"

        if not srv_ok:
            all_ok = False
        print(f"    {'[OK]' if srv_ok else '[FAIL]':<8} {'mlx-server':<16} -> {srv_addr:<22} ({srv_info})")

    print("-" * 76)
    print("=" * 76)
    return 0 if all_ok else 1


# ---------------------------------------------------------------------------
# Command: Granular VictoriaMetrics Review (`review-vm`)
# ---------------------------------------------------------------------------

def cmd_review_vm(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine, eng_ver, daemon_ok = detect_container_engine()
    cinfo = inspect_container(engine, "central-victoriametrics")
    
    print("=" * 76)
    print("               VictoriaMetrics TSDB Configuration & Health")
    print("=" * 76)
    print(f"Container Status  : {cinfo['status']} (Started: {format_ist(cinfo['started_at']) if cinfo['started_at'] else 'N/A'})")
    print(f"Service Endpoint  : http://{cfg['cvm']}")
    
    tcp_ok, lat = probe_tcp(cfg["cvm"])
    h_code, _ = probe_http(f"http://{cfg['cvm']}/health")
    print(f"Health Probe      : {'200 OK' if (tcp_ok and h_code == 200) else 'FAILED'}")
    
    if tcp_ok and h_code == 200:
        total = query_vm(cfg["cvm"], "count({__name__=~\".+\"})")
        total_str = total[0]["value"][1] if total else "0"
        print(f"Total Series      : {total_str} active time series")

        # Nodes reporting
        label_data = []
        try:
            with urllib.request.urlopen(f"http://{cfg['cvm']}/api/v1/label/macbook/values", timeout=2) as r:
                label_data = json.loads(r.read().decode())["data"]
        except Exception:
            pass
        print(f"Reporting Nodes   : {', '.join(label_data) if label_data else 'None'}")
        for node in label_data:
            rec_q = f'time() - max(timestamp({{macbook="{node}"}}))'
            recency = query_vm(cfg["cvm"], rec_q)
            sec = float(recency[0]["value"][1]) if recency else None
            cnt_q = f'count({{macbook="{node}"}})'
            count_res = query_vm(cfg["cvm"], cnt_q)
            c_str = count_res[0]["value"][1] if count_res else "0"
            rec_str = f"{sec:.1f}s ago" if sec is not None else "no data"
            print(f"  • Node '{node}': {c_str} series (last sample {rec_str})")

    print("=" * 76)
    return 0 if (tcp_ok and h_code == 200) else 1


# ---------------------------------------------------------------------------
# Command: Granular Alertmanager Review (`review-alertmanager`)
# ---------------------------------------------------------------------------

def cmd_review_alertmanager(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine, _, _ = detect_container_engine()
    cinfo = inspect_container(engine, "central-alertmanager")

    print("=" * 76)
    print("                  Alertmanager Configuration & Health")
    print("=" * 76)
    print(f"Container Status  : {cinfo['status']} (Started: {format_ist(cinfo['started_at']) if cinfo['started_at'] else 'N/A'})")
    print(f"Service Endpoint  : http://{cfg['calertmanager']}")
    
    tcp_ok, lat = probe_tcp(cfg["calertmanager"])
    h_code, _ = probe_http(f"http://{cfg['calertmanager']}/-/healthy")
    print(f"Health Probe      : {'200 OK' if (tcp_ok and h_code == 200) else 'FAILED'}")

    am_cfg_path = CENTRAL_DIR / "config" / "alertmanager.yml"
    print(f"Config File       : {am_cfg_path} ({'present' if am_cfg_path.is_file() else 'missing'})")

    if tcp_ok and h_code == 200:
        a_code, a_body = probe_http(f"http://{cfg['calertmanager']}/api/v2/alerts")
        if a_code == 200:
            try:
                alerts = json.loads(a_body)
                print(f"Active Alerts     : {len(alerts)} alerts in Alertmanager")
            except Exception:
                pass
    print("=" * 76)
    return 0 if (tcp_ok and h_code == 200) else 1


# ---------------------------------------------------------------------------
# Command: Granular vmalert Review (`review-vmalert`)
# ---------------------------------------------------------------------------

def cmd_review_vmalert(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine, _, _ = detect_container_engine()
    cinfo = inspect_container(engine, "central-vmalert")

    print("=" * 76)
    print("                      vmalert Configuration & Health")
    print("=" * 76)
    print(f"Container Status  : {cinfo['status']} (Started: {format_ist(cinfo['started_at']) if cinfo['started_at'] else 'N/A'})")
    print(f"Service Endpoint  : http://{cfg['cvmalert']}")

    tcp_ok, lat = probe_tcp(cfg["cvmalert"])
    h_code, _ = probe_http(f"http://{cfg['cvmalert']}/health")
    print(f"Health Probe      : {'200 OK' if (tcp_ok and h_code == 200) else 'FAILED'}")

    rules_path = CENTRAL_DIR / "config" / "vmalert-rules.yml"
    print(f"Rules File        : {rules_path} ({'present' if rules_path.is_file() else 'missing'})")

    if tcp_ok and h_code == 200:
        r_code, r_body = probe_http(f"http://{cfg['cvmalert']}/api/v1/rules")
        if r_code == 200:
            try:
                r_data = json.loads(r_body).get("data", {})
                groups = r_data.get("groups", [])
                total_rules = sum(len(g.get("rules", [])) for g in groups)
                print(f"Active Rule Groups: {len(groups)} groups ({total_rules} total rules)")
            except Exception:
                pass

        a_code, a_body = probe_http(f"http://{cfg['cvmalert']}/api/v1/alerts")
        if a_code == 200:
            try:
                a_data = json.loads(a_body).get("data", {}).get("alerts", [])
                print(f"Firing Alerts     : {len(a_data)} active alerts")
                for a in a_data:
                    print(f"  • {a.get('name')} [{a.get('state')}]: {a.get('labels')}")
            except Exception:
                pass

    print("=" * 76)
    return 0 if (tcp_ok and h_code == 200) else 1


# ---------------------------------------------------------------------------
# Command: Granular OTel Collector Review (`review-otel`)
# ---------------------------------------------------------------------------

def cmd_review_otel(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine, _, _ = detect_container_engine()
    cinfo = inspect_container(engine, ["central-otelcol", "central-otel-collector"])

    print("=" * 76)
    print("              OpenTelemetry Collector Configuration & Health")
    print("=" * 76)
    print(f"Container Status  : {cinfo['status']} (Started: {format_ist(cinfo['started_at']) if cinfo['started_at'] else 'N/A'})")
    print(f"OTLP HTTP Receiver: http://{cfg['cotel']}")

    tcp_ok, lat = probe_tcp(cfg["cotel"])
    print(f"Port 4318 Probe   : {'LISTENING' if tcp_ok else 'UNREACHABLE'}")
    
    cfg_file = CENTRAL_DIR / "config" / "otelcol-config.yaml"
    print(f"Pipeline Config   : {cfg_file} ({'present' if cfg_file.is_file() else 'missing'})")
    print("=" * 76)
    return 0 if tcp_ok else 1


# ---------------------------------------------------------------------------
# Command: Granular Opik Review (`review-opik`)
# ---------------------------------------------------------------------------

def cmd_review_opik(args: argparse.Namespace) -> int:
    cfg = load_config()
    engine, _, _ = detect_container_engine()

    print("=" * 76)
    print("                   Opik Tracing Suite Health & Config")
    print("=" * 76)
    print(f"Gateway Endpoint  : http://{cfg['copik']}")

    tcp_ok, lat = probe_tcp(cfg["copik"])
    h_code, _ = probe_http(f"http://{cfg['copik']}/health")
    print(f"Health Probe      : {'200 OK' if (tcp_ok and h_code == 200) else 'FAILED / NOT RUNNING'}")

    opik_containers = [
        "central-opik-frontend", "central-opik-backend", "central-opik-python-backend",
        "central-opik-mysql", "central-opik-clickhouse", ["central-opik-rustfs", "central-opik-minio"],
        "central-opik-redis", "central-opik-zookeeper"
    ]
    for ctarget in opik_containers:
        cinfo = inspect_container(engine, ctarget)
        status_tag = "[OK]" if cinfo["running"] else "[STOPPED]"
        c_label = cinfo["name"] if cinfo["name"] else (ctarget if isinstance(ctarget, str) else ctarget[0])
        print(f"  {status_tag:<10} {c_label:<30} -> {cinfo['status']}")

    print("=" * 76)
    return 0 if (tcp_ok and h_code == 200) else 1


# ---------------------------------------------------------------------------
# CLI Entrypoint Dispatcher
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="MLX Monitoring Diagnostics & Telemetry Tracer")
    sub = ap.add_subparsers(dest="subcommand")

    # doctor
    p_doc = sub.add_parser("doctor", help="automated end-to-end telemetry pipeline traceroute")
    p_doc.add_argument("--node", help="target node id to trace (default: all enabled nodes)")

    # reviews
    p_rc = sub.add_parser("review-central", help="comprehensive audit of all central services")
    p_re = sub.add_parser("review-edge", help="comprehensive audit of all edge exporters")
    p_re.add_argument("--node", help="target node id to inspect")

    sub.add_parser("review-vm", help="granular VictoriaMetrics TSDB audit")
    sub.add_parser("review-alertmanager", help="granular Alertmanager audit")
    sub.add_parser("review-vmalert", help="granular vmalert audit")
    sub.add_parser("review-otel", help="granular OTel Collector audit")
    sub.add_parser("review-opik", help="granular Opik audit")

    args = ap.parse_args()

    dispatch = {
        "doctor": cmd_doctor,
        "review-central": cmd_review_central,
        "review-edge": cmd_review_edge,
        "review-vm": cmd_review_vm,
        "review-alertmanager": cmd_review_alertmanager,
        "review-vmalert": cmd_review_vmalert,
        "review-otel": cmd_review_otel,
        "review-opik": cmd_review_opik,
    }

    if args.subcommand in dispatch:
        sys.exit(dispatch[args.subcommand](args))
    else:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
