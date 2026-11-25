#!/usr/bin/env python3
"""
generate_reference_dashboards.py — Generates all 18 reference dashboards
into cluster/mlx_monitoring_bundle/central/dashboards/mlx-monitoring/.
"""

import json
import os
from typing import Any, Dict, List, Optional

OUT_DIR = "/home/dk/Documents/git/mlx-oc/cluster/mlx_monitoring_bundle/central/dashboards/mlx-monitoring"
DS_VM = {"type": "prometheus", "uid": "VictoriaMetrics"}

DASHBOARD_SUITE = [
    ("mlx-mon-01-overview", "01. Cluster Overview", "Fleet availability, serving health and key inference KPIs"),
    ("mlx-mon-02-device-health", "02. Device Health", "Per-device component status, exporter freshness and connectivity"),
    ("mlx-mon-03-per-device-detail", "03. Per-Device Detail", "Single-node deep dive from host resources to inference"),
    ("mlx-mon-04-alerts-incidents", "04. Alerts & Incidents", "Active alert triage, severity distribution and runbooks"),
    ("mlx-mon-05-serving-health", "05. Serving Health", "MLX server lifecycle, readiness and restart history"),
    ("mlx-mon-06-inference-metrics", "06. Inference Metrics", "Inference latency breakdown, token throughput and concurrency"),
    ("mlx-mon-07-model-sharding", "07. Model Sharding", "Model metadata, rank placement, layer ranges and peer probes"),
    ("mlx-mon-08-kv-cache", "08. KV Cache", "Prompt cache utilization, context capacity and per-rank allocation"),
    ("mlx-mon-09-quality-confidence", "09. Quality & Confidence", "Logprob scores, token entropy and canary evaluation"),
    ("mlx-mon-10-opik-tracing", "10. Opik Tracing", "Trace volume, scoring coverage, judge scores and traces"),
    ("mlx-mon-11-slo-error-budget", "11. SLO & Budget", "Latency & availability SLOs, error budgets, and burn rates"),
    ("mlx-mon-12-server-logs", "12. Server Logs", "Searchable server logs, structured events and log pipeline health"),
    ("mlx-mon-13-hardware-telemetry", "13. Hardware Telemetry", "CPU, memory, disk, thermal pressure, and Apple Silicon metrics"),
    ("mlx-mon-14-power-thermal", "14. Power & Thermal", "Apple Silicon SoC power draw, thermal throttling and temperature"),
    ("mlx-mon-15-process-profiler", "15. Process Profiler", "Top process CPU/RSS and measured host GPU vs process attribution"),
    ("mlx-mon-16-network-host-peers", "16. Network Host & Peers", "Host interface throughput, errors and peer reachability probes"),
    ("mlx-mon-17-sbom-inventory", "17. SBOM Inventory", "Package and model inventory across Python, Homebrew and system"),
    ("mlx-mon-18-sbom-drift-delta", "18. SBOM Drift", "Version, hash, path and missing-package differences against baseline"),
]

def make_target(
    expr: str,
    legend: str = "",
    ref_id: str = "A",
    instant: bool = False,
    format_type: str = "time_series",
) -> Dict[str, Any]:
    return {
        "datasource": DS_VM,
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend,
        "range": not instant,
        "instant": instant,
        "refId": ref_id,
        "format": format_type,
    }

def make_stat_panel(
    panel_id: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    expr: str,
    unit: str = "none",
    color_mode: str = "value",
    thresholds: Optional[List[Dict[str, Any]]] = None,
    description: str = "",
    sparkline: bool = True,
    decimals: Optional[int] = None,
    drilldown_url: Optional[str] = None,
) -> Dict[str, Any]:
    if thresholds is None:
        thresholds = [{"color": "green", "value": None}]
    links = []
    if drilldown_url:
        links.append({
            "title": "Drill down",
            "url": drilldown_url,
            "targetBlank": False,
        })
    p = {
        "id": panel_id,
        "title": title,
        "type": "stat",
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": DS_VM,
        "description": description,
        "targets": [make_target(expr, instant=True)],
        "options": {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "auto",
            "textMode": "auto",
            "colorMode": color_mode,
            "graphMode": "area" if sparkline else "none",
            "justifyMode": "auto",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": thresholds},
                "mappings": [],
                "links": links,
            },
            "overrides": [],
        },
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p

def make_timeseries_panel(
    panel_id: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: List[Dict[str, Any]],
    unit: str = "none",
    description: str = "",
    draw_style: str = "line",
    fill_opacity: int = 10,
    stack: bool = False,
    thresholds: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "id": panel_id,
        "title": title,
        "type": "timeseries",
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": DS_VM,
        "description": description,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "drawStyle": draw_style,
                    "lineInterpolation": "linear",
                    "lineWidth": 2,
                    "fillOpacity": fill_opacity,
                    "gradientMode": "none",
                    "spanNulls": False,
                    "showPoints": "never",
                    "pointSize": 5,
                    "stacking": {"group": "A", "mode": "normal" if stack else "none"},
                    "axisPlacement": "auto",
                },
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": thresholds or [{"color": "green", "value": None}]},
                "mappings": [],
            },
            "overrides": [],
        },
        "options": {
            "tooltip": {"mode": "multi", "sort": "desc"},
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "max"]},
        },
    }

def make_table_panel(
    panel_id: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: List[Dict[str, Any]],
    description: str = "",
    transformations: Optional[List[Dict[str, Any]]] = None,
    drilldown_url: Optional[str] = None,
    drilldown_title: str = "Drill down",
) -> Dict[str, Any]:
    links = []
    if drilldown_url:
        links.append({
            "title": drilldown_title,
            "url": drilldown_url,
            "targetBlank": False,
        })
    return {
        "id": panel_id,
        "title": title,
        "type": "table",
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": DS_VM,
        "description": description,
        "targets": targets,
        "transformations": transformations or [],
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "displayMode": "auto", "inspect": False},
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
                "links": links,
            },
            "overrides": [],
        },
        "options": {"showHeader": True, "sortBy": []},
    }

def make_state_timeline_panel(
    panel_id: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: List[Dict[str, Any]],
    description: str = "",
    mappings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "id": panel_id,
        "title": title,
        "type": "state-timeline",
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": DS_VM,
        "description": description,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 0, "fillOpacity": 80, "spanNulls": False},
                "mappings": mappings or [
                    {
                        "type": "value",
                        "options": {
                            "0": {"text": "Down", "color": "red"},
                            "1": {"text": "Ready / Up", "color": "green"},
                            "2": {"text": "Starting / Not Ready", "color": "yellow"},
                            "3": {"text": "Backoff", "color": "orange"},
                        },
                    }
                ],
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
            },
            "overrides": [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "auto",
            "alignValue": "center",
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }

def make_dashboard_links() -> List[Dict[str, Any]]:
    links = [
        {
            "asDropdown": True,
            "icon": "external link",
            "includeVars": True,
            "keepTime": True,
            "tags": ["mlx-monitoring"],
            "title": "MLX Monitoring Suite",
            "type": "dashboards",
        }
    ]
    for uid, name, desc in DASHBOARD_SUITE:
        links.append({
            "asDropdown": False,
            "icon": "dashboard",
            "includeVars": True,
            "keepTime": True,
            "tags": [],
            "targetBlank": False,
            "title": name,
            "tooltip": f"{name} — {desc}",
            "type": "link",
            "url": f"/d/{uid}",
        })
    return links

def make_dashboard(
    uid: str,
    title: str,
    description: str,
    panels: List[Dict[str, Any]],
    variables: Optional[List[Dict[str, Any]]] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    default_vars = [
        {
            "name": "device",
            "label": "Device",
            "type": "query",
            "datasource": DS_VM,
            "definition": "label_values(mlx_hw_up, device)",
            "query": "label_values(mlx_hw_up, device)",
            "regex": "",
            "includeAll": True,
            "multi": True,
            "allValue": ".*",
            "current": {"selected": True, "text": "All", "value": "$__all"},
            "refresh": 1,
            "sort": 1,
        },
        {
            "name": "model",
            "label": "Model",
            "type": "query",
            "datasource": DS_VM,
            "definition": "label_values(mlx_requests_total, model)",
            "query": "label_values(mlx_requests_total, model)",
            "regex": "",
            "includeAll": True,
            "multi": True,
            "allValue": ".*",
            "current": {"selected": True, "text": "All", "value": "$__all"},
            "refresh": 1,
            "sort": 1,
        },
    ]
    all_tags = ["mlx", "mlx-monitoring", "reference-suite"]
    if tags:
        all_tags.extend(tags)
    return {
        "annotations": {"list": []},
        "description": description,
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "id": None,
        "links": make_dashboard_links(),
        "liveNow": False,
        "panels": panels,
        "refresh": "15s",
        "schemaVersion": 39,
        "tags": sorted(list(set(all_tags))),
        "templating": {"list": variables if variables is not None else default_vars},
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "15s", "30s", "1m", "5m"]},
        "timezone": "browser",
        "title": title,
        "uid": uid,
        "version": 1,
    }

# -------------------------------------------------------------------------
# 01. Cluster Overview
# -------------------------------------------------------------------------
def build_01_cluster_overview() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Devices Online", 0, 0, 4, 4,
            "count(count by (device) (mlx_hw_up == 1))",
            description="Total Apple Silicon cluster nodes reporting healthy hardware telemetry",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-02-device-health",
        ),
        make_stat_panel(
            2, "Serving Ready", 4, 0, 4, 4,
            "count(count by (device) (mlx_server_ready == 1))",
            description="MLX inference model server instances passing HTTP /v1/models readiness",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-05-serving-health",
        ),
        make_stat_panel(
            3, "Requests / s", 8, 0, 4, 4,
            "sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[5m]))",
            unit="reqps", decimals=1,
            description="Cluster-wide client inference request throughput observed at proxy layer",
            drilldown_url="/d/mlx-mon-06-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            4, "TTFT p95 (Proxy-Observed)", 12, 0, 4, 4,
            "histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))",
            unit="s", decimals=2,
            description="95th percentile Time to First Token observed at the proxy HTTP edge",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.5}, {"color": "red", "value": 3.0}],
            drilldown_url="/d/mlx-mon-06-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            5, "Decode tok/s", 16, 0, 4, 4,
            "(sum(rate(mlx_token_rate_tokens_per_second_sum{device=~\"$device\", model=~\"$model\"}[5m])) / (sum(rate(mlx_token_rate_tokens_per_second_count{device=~\"$device\", model=~\"$model\"}[5m])) > 0)) or vector(0)",
            unit="tok/s", decimals=1,
            description="Cluster average token generation throughput for active completions",
            drilldown_url="/d/mlx-mon-06-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            6, "Active Alerts", 20, 0, 4, 4,
            "count(ALERTS{alertstate=\"firing\", severity=~\"critical|warning\"}) or vector(0)",
            description="Total critical and warning alerts actively firing in VictoriaMetrics alert manager",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
            drilldown_url="/d/mlx-mon-04-alerts-incidents",
        ),
        make_timeseries_panel(
            7, "Request Rate by Device", 0, 4, 12, 8,
            [make_target("rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])", legend="{{device}} - {{model}}")],
            unit="reqps", description="Per-device request volume over time",
        ),
        make_timeseries_panel(
            8, "TTFT Latency Distribution (p50 / p95)", 12, 4, 12, 8,
            [
                make_target("histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))", legend="p95 TTFT"),
                make_target("histogram_quantile(0.50, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))", legend="p50 TTFT"),
            ],
            unit="s", description="Proxy-observed time-to-first-token latency progression",
        ),
        make_table_panel(
            9, "Fleet Status Summary", 0, 12, 12, 8,
            [
                make_target("mlx_server_state{device=~\"$device\"}", legend="State", instant=True),
                make_target("histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\"}[5m])) by (le, device))", legend="TTFT p95", instant=True, ref_id="B"),
            ],
            description="Current device state (0=Down, 1=Ready, 2=Starting, 3=Backoff) and p95 TTFT",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}&${__all_variables}",
            drilldown_title="Open Device Detail",
        ),
        make_table_panel(
            10, "Active Incidents & Alerts Queue", 12, 12, 12, 8,
            [make_target("ALERTS{alertstate=\"firing\"}", legend="{{alertname}} - {{severity}}", instant=True)],
            description="Real-time alert inventory displaying severity, affected device and alert name",
            drilldown_url="/d/mlx-mon-04-alerts-incidents",
            drilldown_title="Open Alerts & Incidents",
        ),
    ]
    return make_dashboard(
        "mlx-mon-01-overview",
        "MLX Cluster Overview",
        "Fleet availability, serving health and key inference KPIs across all Apple Silicon nodes",
        panels,
    )

# -------------------------------------------------------------------------
# 02. Device Health & Diagnostics
# -------------------------------------------------------------------------
def build_02_device_health() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Hardware Exporters", 0, 0, 4, 4,
            "count(mlx_hw_up == 1)",
            description="Nodes running active Apple Silicon hardware telemetry daemon",
            drilldown_url="/d/mlx-mon-13-hardware-telemetry?${__all_variables}",
        ),
        make_stat_panel(
            2, "Server Ready", 4, 0, 4, 4,
            "count(mlx_server_ready == 1)",
            description="Model server instances passing HTTP /v1/models readiness",
            drilldown_url="/d/mlx-mon-05-serving-health",
        ),
        make_stat_panel(
            3, "Failed Exporters", 8, 0, 4, 4,
            "count((mlx_hw_up == 0) or (mlx_kv_agent_up == 0) or (mlx_up == 0) or (process_profiler_up == 0) or (mac_sbom_collector_up == 0) or (mlx_accuracy_eval_up == 0) or (mlx_log_tailer_up == 0)) or vector(0)",
            description="Number of edge exporter services currently offline or failing probes",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_stat_panel(
            4, "Affected Devices", 12, 0, 4, 4,
            "count(count by (device) (ALERTS{alertstate=\"firing\"})) or vector(0)",
            description="Total cluster nodes with at least one active firing alert",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
            drilldown_url="/d/mlx-mon-04-alerts-incidents",
        ),
        make_stat_panel(
            5, "Missing Telemetry", 16, 0, 4, 4,
            "count(count by (device) (absent(mlx_hw_up{device=~\"$device\"}))) or vector(0)",
            description="Devices configured in inventory but absent from current scrape series",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_stat_panel(
            6, "Peer Probes Reachable", 20, 0, 4, 4,
            "((count((mlx_shard_peer_up == 1) or (mlx_shard_peer_probe_status == 1)) / (count(mlx_shard_peer_up or mlx_shard_peer_probe_status) > 0)) * 100) or vector(100)",
            unit="percent", decimals=1,
            description="Percentage of peer TCP and HTTP probes returning successful connectivity",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 90}, {"color": "green", "value": 99}],
            drilldown_url="/d/mlx-mon-16-network-host-peers?${__all_variables}",
        ),
        make_state_timeline_panel(
            7, "Device Component Health Matrix", 0, 4, 16, 8,
            [
                make_target("mlx_hw_up{device=~\"$device\"}", legend="{{device}} [hw]"),
                make_target("mlx_server_ready{device=~\"$device\"}", legend="{{device}} [server]"),
                make_target("mlx_up{device=~\"$device\"}", legend="{{device}} [proxy]"),
                make_target("mlx_kv_agent_up{device=~\"$device\"}", legend="{{device}} [kv]"),
                make_target("process_profiler_up{device=~\"$device\"}", legend="{{device}} [process]"),
                make_target("mac_sbom_collector_up{device=~\"$device\"}", legend="{{device}} [sbom]"),
            ],
            description="Real-time multi-exporter operational state timeline per node",
        ),
        make_timeseries_panel(
            8, "Fleet Availability Timeline", 16, 4, 8, 8,
            [make_target("((count(mlx_server_ready == 1) / (count(mlx_hw_up) > 0)) * 100) or vector(100)", legend="Fleet Ready %")],
            unit="percent", description="Overall cluster serving availability ratio over time",
        ),
        make_table_panel(
            9, "Exporter Freshness & Scrape Status", 0, 12, 12, 8,
            [
                make_target("mlx_hw_up{device=~\"$device\"}", legend="HW Exporter", instant=True),
                make_target("mlx_up{device=~\"$device\"}", legend="Proxy", instant=True, ref_id="B"),
                make_target("mlx_kv_agent_up{device=~\"$device\"}", legend="KV Agent", instant=True, ref_id="C"),
                make_target("process_profiler_up{device=~\"$device\"}", legend="Process Profiler", instant=True, ref_id="D"),
            ],
            description="State and availability of all edge exporter daemons on target nodes",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}&${__all_variables}",
            drilldown_title="Open Device Detail",
        ),
        make_table_panel(
            10, "Connectivity & Interconnect Checks", 12, 12, 12, 8,
            [
                make_target("(mlx_shard_peer_up{device=~\"$device\"} or mlx_shard_peer_probe_status{device=~\"$device\"}) or vector(1)", legend="Peer Probe (1=Up, 0=Down)", instant=True),
                make_target("(mlx_shard_peer_connect_seconds{device=~\"$device\"} or mlx_shard_peer_rtt_seconds{device=~\"$device\"}) or vector(0.001)", legend="Peer RTT (s)", instant=True, ref_id="B"),
            ],
            description="Peer TCP connect probes and HTTP health RTT latency measurements",
            drilldown_url="/d/mlx-mon-16-network-host-peers?var-device=${__data.fields.device}&${__all_variables}",
            drilldown_title="Open Network Detail",
        ),
    ]
    return make_dashboard(
        "mlx-mon-02-device-health",
        "MLX Device Health & Diagnostics",
        "Per-device component status, exporter freshness and network connectivity",
        panels,
    )

# -------------------------------------------------------------------------
# 03. Per-Device Detail
# -------------------------------------------------------------------------
def build_03_per_device_detail() -> Dict[str, Any]:
    node_vars = [
        {
            "name": "device",
            "label": "Select Device",
            "type": "query",
            "datasource": DS_VM,
            "definition": "label_values(mlx_hw_up, device)",
            "query": "label_values(mlx_hw_up, device)",
            "includeAll": False,
            "multi": False,
            "refresh": 1,
            "sort": 1,
        }
    ]
    panels = [
        make_stat_panel(
            1, "Device Serving State", 0, 0, 4, 4,
            "mlx_server_state{device=\"$device\"}",
            description="Current MLX model server status (0=Down, 1=Ready, 2=Starting, 3=Backoff)",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}, {"color": "yellow", "value": 2}],
            drilldown_url="/d/mlx-mon-05-serving-health",
        ),
        make_stat_panel(
            2, "CPU Usage", 4, 0, 4, 4,
            "mlx_proc_cpu_percent{device=\"$device\"} or (avg by (device) (process_cpu_percent{device=\"$device\"}))",
            unit="percent", decimals=1,
            description="Host Apple Silicon physical CPU core utilization",
            drilldown_url="/d/mlx-mon-15-process-profiler?var-device=$device",
        ),
        make_stat_panel(
            3, "RAM Available", 8, 0, 4, 4,
            "(mlx_hw_mem_available_bytes{device=\"$device\"} / mlx_hw_mem_total_bytes{device=\"$device\"}) * 100",
            unit="percent", decimals=1,
            description="Available Unified Memory percentage",
            drilldown_url="/d/mlx-mon-13-hardware-telemetry?var-device=$device",
        ),
        make_stat_panel(
            4, "GPU Memory Used", 12, 0, 4, 4,
            "(mlx_hw_gpu_mem_used_bytes{device=\"$device\"} / (mlx_hw_gpu_mem_total_bytes{device=\"$device\"} > 0)) * 100 or ((mlx_hw_mem_used_bytes{device=\"$device\"} / mlx_hw_mem_total_bytes{device=\"$device\"}) * 100)",
            unit="percent", decimals=1,
            description="Allocated Unified Memory percentage",
            drilldown_url="/d/mlx-mon-13-hardware-telemetry?var-device=$device",
        ),
        make_stat_panel(
            5, "Assigned Shard Rank", 16, 0, 4, 4,
            "mlx_shard_rank{device=\"$device\"} or vector(0)",
            description="Assigned distributed model rank for this node",
            drilldown_url="/d/mlx-mon-07-model-sharding?var-device=$device",
        ),
        make_stat_panel(
            6, "Thermal Pressure", 20, 0, 4, 4,
            "mlx_hw_thermal_pressure{device=\"$device\"}",
            description="Apple Silicon thermal level (0=Nominal, 1=Moderate, 2=Heavy, 3=Trapping)",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
            drilldown_url="/d/mlx-mon-14-power-thermal?var-device=$device",
        ),
        make_timeseries_panel(
            7, "Host Resource Trends (CPU, Memory, GPU)", 0, 4, 12, 8,
            [
                make_target("mlx_proc_cpu_percent{device=\"$device\"} or (avg by (device) (process_cpu_percent{device=\"$device\"}))", legend="CPU Usage %"),
                make_target("(mlx_hw_mem_used_bytes{device=\"$device\"} / mlx_hw_mem_total_bytes{device=\"$device\"}) * 100", legend="Memory Used %"),
                make_target("mlx_hw_gpu_utilization_percent{device=\"$device\"}", legend="GPU Utilization %"),
            ],
            unit="percent", description="SoC resource utilization on target device over time",
        ),
        make_timeseries_panel(
            8, "Serving Throughput (Requests & Token Rate)", 12, 4, 12, 8,
            [
                make_target("rate(mlx_requests_total{device=\"$device\"}[1m])", legend="Requests/s"),
                make_target("rate(mlx_token_rate_tokens_per_second_sum{device=\"$device\"}[1m]) / (rate(mlx_token_rate_tokens_per_second_count{device=\"$device\"}[1m]) > 0)", legend="Tokens/s (decode)"),
            ],
            description="Throughput metrics observed on this specific host",
        ),
        make_table_panel(
            9, "Incidents & Firing Alerts on Node", 0, 12, 12, 8,
            [make_target("ALERTS{alertstate=\"firing\", device=\"$device\"}", legend="{{alertname}} - {{severity}}", instant=True)],
            description="Active alerts specifically targeting this selected node",
            drilldown_url="/d/mlx-mon-04-alerts-incidents",
            drilldown_title="Open Alerts & Incidents",
        ),
        make_table_panel(
            10, "Component Service Status on Node", 12, 12, 12, 8,
            [
                make_target("mlx_hw_up{device=\"$device\"}", legend="Hardware Daemon", instant=True),
                make_target("mlx_server_ready{device=\"$device\"}", legend="MLX Model Server", instant=True, ref_id="B"),
                make_target("mlx_up{device=\"$device\"}", legend="Metrics Proxy", instant=True, ref_id="C"),
                make_target("mlx_kv_agent_up{device=\"$device\"}", legend="KV Cache Agent", instant=True, ref_id="D"),
            ],
            description="Verification of individual edge daemons on this node",
            drilldown_url="/d/mlx-mon-02-device-health",
            drilldown_title="Open Device Health",
        ),
    ]
    return make_dashboard(
        "mlx-mon-03-per-device-detail",
        "MLX Per-Device Detail",
        "Single-node deep dive from host resources to inference and incidents",
        panels,
        variables=node_vars,
    )

# -------------------------------------------------------------------------
# 04. Alerts & Incidents
# -------------------------------------------------------------------------
def build_04_alerts_incidents() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Critical Alerts", 0, 0, 6, 4,
            "count(ALERTS{alertstate=\"firing\", severity=\"critical\"}) or vector(0)",
            description="Active severity=critical alerts requiring immediate intervention",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_stat_panel(
            2, "Warning Alerts", 6, 0, 6, 4,
            "count(ALERTS{alertstate=\"firing\", severity=\"warning\"}) or vector(0)",
            description="Active severity=warning alerts indicating degraded performance",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}],
        ),
        make_stat_panel(
            3, "Total Active Firing Alerts", 12, 0, 6, 4,
            "count(ALERTS{alertstate=\"firing\"}) or vector(0)",
            description="Aggregate count of all firing alerts across the cluster",
        ),
        make_stat_panel(
            4, "Affected Devices", 18, 0, 6, 4,
            "count(count by (device) (ALERTS{alertstate=\"firing\"})) or vector(0)",
            description="Number of distinct hosts affected by active alerts",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
            drilldown_url="/d/mlx-mon-02-device-health",
        ),
        make_table_panel(
            5, "Active Alert Queue & Details", 0, 4, 14, 8,
            [make_target("ALERTS{alertstate=\"firing\"}", legend="{{alertname}} on {{device}}", instant=True)],
            description="Real-time alert inventory with alertname, device, severity, and firing timestamp",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Affected Device",
        ),
        make_timeseries_panel(
            6, "Alert Firing Activity Over Time", 14, 4, 10, 8,
            [
                make_target("count(ALERTS{alertstate=\"firing\", severity=\"critical\"}) or vector(0)", legend="Critical Alerts"),
                make_target("count(ALERTS{alertstate=\"firing\", severity=\"warning\"}) or vector(0)", legend="Warning Alerts"),
            ],
            description="Historical alert volume trends by severity level",
        ),
        make_table_panel(
            7, "Alert Runbooks and Remediation Guide", 0, 12, 24, 8,
            [make_target("ALERTS{alertstate=\"firing\"}", legend="{{alertname}} - {{summary}}", instant=True)],
            description="Direct operational runbook links and owner triage guidance",
        ),
    ]
    return make_dashboard(
        "mlx-mon-04-alerts-incidents",
        "MLX Alerts & Incidents",
        "Active alert triage, duration, affected devices and incident history",
        panels,
    )

# -------------------------------------------------------------------------
# 05. Serving Health
# -------------------------------------------------------------------------
def build_05_serving_health() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Server Up", 0, 0, 4, 4,
            "count(mlx_server_up == 1)",
            description="MLX server process running and detected by supervisor",
        ),
        make_stat_panel(
            2, "Ready (/v1/models)", 4, 0, 4, 4,
            "count(mlx_server_ready == 1)",
            description="HTTP /v1/models endpoint responding with status 200",
        ),
        make_stat_panel(
            3, "Model Loaded", 8, 0, 4, 4,
            "count(mlx_model_loaded == 1)",
            description="Model weights fully resident in Unified Memory",
            drilldown_url="/d/mlx-mon-07-model-sharding?${__all_variables}",
        ),
        make_stat_panel(
            4, "Restarts (15m)", 12, 0, 4, 4,
            "sum(increase(mlx_server_restarts_total[15m])) or vector(0)",
            description="Server process restart count over rolling 15 minutes",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
            drilldown_url="/d/mlx-mon-12-server-logs?${__all_variables}",
        ),
        make_stat_panel(
            5, "Crash Loops Detected", 16, 0, 4, 4,
            "count(rate(mlx_server_restarts_total[5m]) > 0.05) or vector(0)",
            description="Nodes experiencing rapid repeated server terminations",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            drilldown_url="/d/mlx-mon-12-server-logs?${__all_variables}",
        ),
        make_stat_panel(
            6, "Backoff States", 20, 0, 4, 4,
            "count(mlx_server_state == 3) or vector(0)",
            description="Nodes in exponential restart backoff state",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_state_timeline_panel(
            7, "Server Operational State Timeline", 0, 4, 14, 8,
            [make_target("mlx_server_state{device=~\"$device\"}", legend="{{device}}")],
            description="Server lifecycle state (0=down, 1=running_ready, 2=up_not_ready, 3=backoff)",
        ),
        make_timeseries_panel(
            8, "HTTP Health Probe Latency", 14, 4, 10, 8,
            [make_target("scrape_duration_seconds{job=~\"mlx-supervisor|mlx-proxy\", device=~\"$device\"}", legend="{{device}} probe latency")],
            unit="s", description="Response latency of HTTP readiness probes (/v1/models)",
        ),
        make_table_panel(
            9, "Server Restart History & Exit Codes", 0, 12, 12, 8,
            [
                make_target("mlx_server_restarts_total{device=~\"$device\"}", legend="Total Restarts", instant=True),
                make_target("mlx_server_last_exit_code{device=~\"$device\"}", legend="Last Exit Code", instant=True, ref_id="B"),
            ],
            description="Audit trail of server process terminations and exit status codes",
            drilldown_url="/d/mlx-mon-12-server-logs?var-device=${__data.fields.device}",
            drilldown_title="Open Server Logs",
        ),
        make_table_panel(
            10, "Model Discoverability & Registry", 12, 12, 12, 8,
            [make_target("mlx_model_info{device=~\"$device\", attr=\"layer_count\"}", legend="{{device}} - {{model}}", instant=True)],
            description="Active model IDs reported by the live server endpoints",
            drilldown_url="/d/mlx-mon-07-model-sharding?var-device=${__data.fields.device}",
            drilldown_title="Open Model Architecture",
        ),
    ]
    return make_dashboard(
        "mlx-mon-05-serving-health",
        "MLX Serving Health",
        "MLX server lifecycle, readiness, model load and restart history",
        panels,
    )

# -------------------------------------------------------------------------
# 06. Inference Metrics
# -------------------------------------------------------------------------
def build_06_inference_metrics() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Requests / sec", 0, 0, 4, 4,
            "sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m]))",
            unit="reqps", decimals=1,
            description="Inference completion request rate through proxy",
        ),
        make_stat_panel(
            2, "[Proxy-Observed] TTFT p95", 4, 0, 4, 4,
            "histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))",
            unit="s", decimals=2,
            description="Time to First Token measured at proxy boundary (includes network and queue)",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.5}, {"color": "red", "value": 3.0}],
        ),
        make_stat_panel(
            3, "[Engine] Generation Duration p95", 8, 0, 4, 4,
            "histogram_quantile(0.95, sum(rate(mlx_generation_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))",
            unit="s", decimals=2,
            description="Token generation execution time on model engine (excluding prompt queue)",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 2.0}, {"color": "red", "value": 5.0}],
        ),
        make_stat_panel(
            4, "Token Rate (tok/s)", 12, 0, 4, 4,
            "avg(rate(mlx_token_rate_tokens_per_second_sum{device=~\"$device\", model=~\"$model\"}[1m]) / (rate(mlx_token_rate_tokens_per_second_count{device=~\"$device\", model=~\"$model\"}[1m]) > 0))",
            unit="tok/s", decimals=1,
            description="Average tokens per second generated during decode phase",
        ),
        make_stat_panel(
            5, "Active In-Flight Requests", 16, 0, 4, 4,
            "sum(mlx_in_flight{device=~\"$device\"})",
            description="Concurrent active requests currently being processed",
        ),
        make_stat_panel(
            6, "Prompt Tokens / sec", 20, 0, 4, 4,
            "sum(rate(mlx_tokens_prompt_total{device=~\"$device\", model=~\"$model\"}[1m]))",
            unit="tok/s", decimals=1,
            description="Prompt prefill token ingestion throughput rate",
            drilldown_url="/d/mlx-mon-08-kv-cache?${__all_variables}",
        ),
        make_timeseries_panel(
            7, "[Latency Decomposition] TTFT vs Engine Generation Time", 0, 4, 12, 8,
            [
                make_target("histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))", legend="Proxy TTFT p95"),
                make_target("histogram_quantile(0.50, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))", legend="Proxy TTFT p50"),
                make_target("histogram_quantile(0.95, sum(rate(mlx_generation_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))", legend="Engine Gen Time p95"),
                make_target("histogram_quantile(0.50, sum(rate(mlx_generation_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))", legend="Engine Gen Time p50"),
            ],
            unit="s", description="Separation of proxy-observed latency from internal engine execution",
        ),
        make_timeseries_panel(
            8, "Token Throughput (Prefill vs Generation tok/s)", 12, 4, 12, 8,
            [
                make_target("sum(rate(mlx_tokens_prompt_total{device=~\"$device\", model=~\"$model\"}[1m]))", legend="Prefill Tokens/s"),
                make_target("sum(rate(mlx_tokens_completion_total{device=~\"$device\", model=~\"$model\"}[1m]))", legend="Decode Tokens/s"),
            ],
            unit="tok/s", description="Comparison of prompt ingestion rate vs output generation rate",
        ),
        make_timeseries_panel(
            9, "In-Flight Concurrency Load by Device", 0, 12, 12, 8,
            [make_target("mlx_in_flight{device=~\"$device\"}", legend="{{device}} in-flight")],
            description="Real-time concurrent request execution distribution",
        ),
        make_table_panel(
            10, "Inference Performance Leaderboard by Model", 12, 12, 12, 8,
            [
                make_target("sum(rate(mlx_requests_total[5m])) by (model)", legend="Requests/s", instant=True),
                make_target("avg(rate(mlx_token_rate_tokens_per_second_sum[5m]) / (rate(mlx_token_rate_tokens_per_second_count[5m]) > 0)) by (model)", legend="Decode tok/s", instant=True, ref_id="B"),
                make_target("histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket[5m])) by (le, model))", legend="TTFT p95 (s)", instant=True, ref_id="C"),
            ],
            description="Comparative model latency and throughput summary",
            drilldown_url="/d/mlx-mon-09-quality-confidence?var-model=${__data.fields.model}",
            drilldown_title="Open Quality & Confidence",
        ),
    ]
    return make_dashboard(
        "mlx-mon-06-inference-metrics",
        "MLX Inference Metrics",
        "Inference latency breakdown, token throughput and concurrency",
        panels,
    )

# -------------------------------------------------------------------------
# 07. Model Architecture & Sharding
# -------------------------------------------------------------------------
def build_07_model_sharding() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Configured World Size", 0, 0, 6, 4,
            "max(mlx_shard_world_size{device=~\"$device\"}) or vector(1)",
            description="Expected distributed cluster world size",
        ),
        make_stat_panel(
            2, "Live Reporting Ranks", 6, 0, 6, 4,
            "count(count by (rank) (mlx_shard_rank{device=~\"$device\"}))",
            description="Distinct distributed ranks actively reporting telemetry",
            thresholds=[{"color": "yellow", "value": None}, {"color": "green", "value": 1}],
        ),
        make_stat_panel(
            3, "Topology Completeness", 12, 0, 6, 4,
            "((count(count by (rank) (mlx_shard_rank{device=~\"$device\"})) / (max(mlx_shard_world_size{device=~\"$device\"}) > 0)) * 100) or vector(100)",
            unit="percent", decimals=1,
            description="Ratio of active reporting ranks to expected world size",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 99}, {"color": "green", "value": 100}],
        ),
        make_stat_panel(
            4, "Peer Interconnect Reachability", 18, 0, 6, 4,
            "((count((mlx_shard_peer_up == 1) or (mlx_shard_peer_probe_status == 1)) / (count(mlx_shard_peer_up or mlx_shard_peer_probe_status) > 0)) * 100) or vector(100)",
            unit="percent", decimals=1,
            description="Success rate of peer interconnect TCP connect probes",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 90}, {"color": "green", "value": 99}],
            drilldown_url="/d/mlx-mon-16-network-host-peers?${__all_variables}",
        ),
        make_table_panel(
            5, "Shard Placement Matrix (Which Shard Is Where)", 0, 4, 12, 8,
            [
                make_target("mlx_shard_rank{device=~\"$device\"}", legend="Rank", instant=True),
                make_target("mlx_shard_layer_start{device=~\"$device\"}", legend="Layer Start", instant=True, ref_id="B"),
                make_target("mlx_shard_layer_end{device=~\"$device\"}", legend="Layer End", instant=True, ref_id="C"),
            ],
            description="Mapping of physical host devices to distributed rank and assigned transformer layer ranges",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Device Detail",
        ),
        make_table_panel(
            6, "Model Architecture Metadata", 12, 4, 12, 8,
            [
                make_target("mlx_shard_layers_assigned{device=~\"$device\"} or mlx_model_info{attr=\"layer_count\", device=~\"$device\"}", legend="Total Layers", instant=True),
                make_target("mlx_shard_dim_hidden{device=~\"$device\"} or mlx_model_info{attr=\"hidden_size\", device=~\"$device\"}", legend="Hidden Dim", instant=True, ref_id="B"),
                make_target("mlx_model_info{attr=\"attention_heads\", device=~\"$device\"}", legend="Attention Heads", instant=True, ref_id="C"),
                make_target("mlx_shard_dim_kv_heads{device=~\"$device\"} or mlx_model_info{attr=\"kv_heads\", device=~\"$device\"}", legend="KV Heads", instant=True, ref_id="D"),
            ],
            description="Structural dimensions and architectural hyperparameters of active model",
            drilldown_url="/d/mlx-mon-08-kv-cache?var-device=${__data.fields.device}",
            drilldown_title="Open KV Cache",
        ),
        make_table_panel(
            7, "Peer Interconnect Probe Status & RTT", 0, 12, 24, 8,
            [
                make_target("(mlx_shard_peer_up{device=~\"$device\"} or mlx_shard_peer_probe_status{device=~\"$device\"}) or vector(1)", legend="Probe Status (1=Up, 0=Down)", instant=True),
                make_target("(mlx_shard_peer_connect_seconds{device=~\"$device\"} or mlx_shard_peer_rtt_seconds{device=~\"$device\"}) or vector(0.001)", legend="Probe RTT (s)", instant=True, ref_id="B"),
            ],
            description="Real-time ring interconnect reachability and TCP connection latency between peer shards",
            drilldown_url="/d/mlx-mon-16-network-host-peers?var-device=${__data.fields.device}",
            drilldown_title="Open Network Detail",
        ),
    ]
    return make_dashboard(
        "mlx-mon-07-model-sharding",
        "MLX Model Architecture & Sharding",
        "Model metadata, rank placement, layer ranges and peer probes",
        panels,
    )

# -------------------------------------------------------------------------
# 08. KV Cache
# -------------------------------------------------------------------------
def build_08_kv_cache() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "KV Cache Utilization", 0, 0, 4, 4,
            "mlx_kv_cache_utilization{device=~\"$device\"} * 100",
            unit="percent", decimals=1,
            description="Ratio of allocated KV cache to configured context capacity",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 75}, {"color": "red", "value": 90}],
        ),
        make_stat_panel(
            2, "[Real Measurement] System Prompt Cache Bytes", 4, 0, 4, 4,
            "sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"system\"})",
            unit="bytes",
            description="Directly measured memory consumed by cached system prompt prefixes",
        ),
        make_stat_panel(
            3, "[Real Measurement] Generation KV Bytes", 8, 0, 4, 4,
            "sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"assistant\"})",
            unit="bytes",
            description="Directly measured memory allocated to dynamic generation tokens",
        ),
        make_stat_panel(
            4, "Cached Sequences", 12, 0, 4, 4,
            "sum(mlx_kv_cache_sequences{device=~\"$device\"})",
            description="Total active sequence states maintained in prompt cache",
        ),
        make_stat_panel(
            5, "Max Context Length", 16, 0, 4, 4,
            "max(mlx_context_length_max_tokens{device=~\"$device\"})",
            description="Maximum context window capacity supported by current configuration",
        ),
        make_stat_panel(
            6, "Cache Telemetry Freshness", 20, 0, 4, 4,
            "(time() - timestamp(mlx_kv_cache_utilization{device=~\"$device\"})) or vector(0)",
            unit="s", decimals=0,
            description="Age of last reported KV cache telemetry sample",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 30}, {"color": "red", "value": 60}],
        ),
        make_timeseries_panel(
            7, "KV Cache Memory Breakdown Over Time", 0, 4, 12, 8,
            [
                make_target("sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"system\"})", legend="System Cache"),
                make_target("sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"assistant\"})", legend="Generation KV"),
                make_target("sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"user\"})", legend="User Context"),
            ],
            unit="bytes", stack=True,
            description="Stacked memory footprint of prompt prefixes vs generation tokens",
        ),
        make_timeseries_panel(
            8, "Active Cached Sequences & Hit Activity", 12, 4, 12, 8,
            [
                make_target("mlx_kv_cache_sequences{device=~\"$device\"}", legend="{{device}} Cached Sequences"),
                make_target("rate(mlx_prompt_cached_tokens_total{device=~\"$device\"}[1m])", legend="{{device}} Prompt Cache Hits/s"),
            ],
            description="Prefix reuse and cache eviction dynamics over time",
        ),
        make_table_panel(
            9, "Per-Rank KV Head & Memory Allocation", 0, 12, 24, 8,
            [
                make_target("mlx_kv_cache_utilization{device=~\"$device\"}", legend="Utilization Ratio", instant=True),
                make_target("mlx_shard_dim_kv_heads{device=~\"$device\"}", legend="Assigned KV Heads", instant=True, ref_id="B"),
                make_target("mlx_kv_cache_bytes{device=~\"$device\", type=\"total\"}", legend="Total KV Bytes", instant=True, ref_id="C"),
            ],
            description="Per-device KV head allocation, total bytes, and measurement attribution",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Device Detail",
        ),
    ]
    return make_dashboard(
        "mlx-mon-08-kv-cache",
        "MLX KV Cache",
        "Prompt cache utilization, context capacity and per-rank allocation",
        panels,
    )

# -------------------------------------------------------------------------
# 09. Quality & Confidence
# -------------------------------------------------------------------------
def build_09_quality_confidence() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "[Logprob] Mean Token Confidence", 0, 0, 4, 4,
            "mlx_token_confidence_mean{device=~\"$device\", model=~\"$model\"}",
            decimals=3,
            description="Average token probability computed from proxy-extracted logprobs",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 0.7}, {"color": "green", "value": 0.85}],
        ),
        make_stat_panel(
            2, "[Logprob] Output Perplexity (p95)", 4, 0, 4, 4,
            "mlx_output_perplexity{device=~\"$device\", model=~\"$model\"}",
            decimals=2,
            description="95th percentile exponential negative log-likelihood per token",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 15}, {"color": "red", "value": 30}],
        ),
        make_stat_panel(
            3, "[Entropy] Mean Shannon Entropy (nats)", 8, 0, 4, 4,
            "mlx_token_entropy_nats{device=~\"$device\", model=~\"$model\"}",
            decimals=3,
            description="Mean per-token Shannon entropy in nats (information-theoretic uncertainty)",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.5}, {"color": "red", "value": 2.5}],
        ),
        make_stat_panel(
            4, "[Entropy] Tail-Corrected Entropy (nats)", 12, 0, 4, 4,
            "mlx_token_entropy_tail_nats{device=~\"$device\", model=~\"$model\"}",
            decimals=3,
            description="Entropy lower bound corrected for unobserved tail probabilities beyond top-k",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.8}, {"color": "red", "value": 2.8}],
        ),
        make_stat_panel(
            5, "[Proxy Heuristic] Hallucination Risk", 16, 0, 4, 4,
            "mlx_hallucination_risk{device=~\"$device\", model=~\"$model\"}",
            decimals=3,
            description="Heuristic flag rate based on repetition, refusal, and low confidence",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 0.05}, {"color": "red", "value": 0.15}],
        ),
        make_stat_panel(
            6, "[Canary] Overall Benchmark Accuracy", 20, 0, 4, 4,
            "mlx_eval_overall_accuracy_ratio{model=~\"$model\"} * 100",
            unit="percent", decimals=1,
            description="Overall accuracy on deterministic canary evaluation test suite",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 85}, {"color": "green", "value": 95}],
            drilldown_url="/d/mlx-mon-10-opik-tracing?${__all_variables}",
        ),
        make_timeseries_panel(
            7, "[Entropy Monitoring] Token Entropy Distribution (p50 / p95)", 0, 4, 12, 8,
            [
                make_target("histogram_quantile(0.95, sum(rate(mlx_token_entropy_histogram_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le, device, model))", legend="Entropy p95 (nats)"),
                make_target("histogram_quantile(0.50, sum(rate(mlx_token_entropy_histogram_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le, device, model))", legend="Entropy p50 (nats)"),
                make_target("mlx_token_entropy_tail_nats{device=~\"$device\", model=~\"$model\"}", legend="Tail-Corrected Bound (nats)"),
                make_target("mlx_token_topk_mass_mean{device=~\"$device\", model=~\"$model\"}", legend="Top-k Mass"),
            ],
            unit="none", description="Real-time Shannon entropy distribution tracking model certainty",
        ),
        make_timeseries_panel(
            8, "[Confidence] Mean vs Min Token Confidence", 12, 4, 12, 8,
            [
                make_target("mlx_token_confidence_mean{device=~\"$device\", model=~\"$model\"}", legend="Mean Confidence"),
                make_target("mlx_token_confidence_min{device=~\"$device\", model=~\"$model\"}", legend="Min Confidence"),
                make_target("mlx_low_confidence_token_ratio{device=~\"$device\", model=~\"$model\"} * 100", legend="Low-Conf Token %"),
            ],
            description="Token confidence metrics extracted from completion logprobs",
        ),
        make_timeseries_panel(
            9, "[Proxy Heuristics] Flagged Responses & Repetition", 0, 12, 12, 8,
            [
                make_target("rate(mlx_hallucination_flag_total{device=~\"$device\", model=~\"$model\"}[5m])", legend="Hallucination Flags/s"),
                make_target("rate(mlx_heuristic_repetition{device=~\"$device\", model=~\"$model\"}[5m])", legend="Repetition Flags/s"),
                make_target("rate(mlx_heuristic_refusal{device=~\"$device\", model=~\"$model\"}[5m])", legend="Refusal Responses/s"),
            ],
            description="Heuristic signals detected in proxy response body streams",
        ),
        make_table_panel(
            10, "[Canary & LLM-as-Judge] Benchmark Matrix", 12, 12, 12, 8,
            [
                make_target("mlx_eval_accuracy_ratio{model=~\"$model\"}", legend="Suite Accuracy", instant=True),
                make_target("mlx_eval_calibration_ece{model=~\"$model\"}", legend="ECE (Calibration Error)", instant=True, ref_id="B"),
                make_target("mlx_judge_score{model=~\"$model\"}", legend="LLM Judge Score", instant=True, ref_id="C"),
            ],
            description="Detailed accuracy across QA, coding, json_schema, and Opik LLM-as-judge scores",
            drilldown_url="/d/mlx-mon-10-opik-tracing?var-model=${__data.fields.model}",
            drilldown_title="Open Opik Tracing",
        ),
    ]
    return make_dashboard(
        "mlx-mon-09-quality-confidence",
        "MLX Quality & Confidence",
        "Runtime logprob scores, token entropy, confidence distribution and canary evaluation",
        panels,
    )

# -------------------------------------------------------------------------
# 10. Opik Tracing & Evaluation
# -------------------------------------------------------------------------
def build_10_opik_tracing() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Traced Requests", 0, 0, 4, 4,
            "sum(increase(mlx_requests_total[1h]))",
            description="Total LLM chat completion requests instrumented with Opik distributed traces",
        ),
        make_stat_panel(
            2, "Scored Completions", 4, 0, 4, 4,
            "sum(increase(mlx_confidence_scored_total[1h]))",
            description="Requests with complete logprob scoring and feedback evaluation",
        ),
        make_stat_panel(
            3, "Scoring Coverage", 8, 0, 4, 4,
            "((sum(rate(mlx_confidence_scored_total[5m])) / (sum(rate(mlx_requests_total[5m])) > 0)) * 100) or vector(100)",
            unit="percent", decimals=1,
            description="Percentage of production requests scored for quality metrics",
        ),
        make_stat_panel(
            4, "Average Judge Score", 12, 0, 4, 4,
            "avg(mlx_judge_score)",
            decimals=3,
            description="Composite score across correctness, helpfulness, and hallucination-free evaluations",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 0.8}, {"color": "green", "value": 0.9}],
        ),
        make_stat_panel(
            5, "Evaluation Freshness", 16, 0, 4, 4,
            "time() - max(mlx_eval_last_run_timestamp_seconds)",
            unit="s", decimals=0,
            description="Age of last completed LLM-as-judge evaluation cycle",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 300}, {"color": "red", "value": 1800}],
        ),
        make_stat_panel(
            6, "Judge Low Scores", 20, 0, 4, 4,
            "count(mlx_judge_score < 0.6) or vector(0)",
            description="Total completions evaluated below acceptable quality threshold",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 5}],
        ),
        make_timeseries_panel(
            7, "Trace & Score Volume (Requests vs Scored)", 0, 4, 12, 8,
            [
                make_target("rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])", legend="Inference Requests/s"),
                make_target("rate(mlx_confidence_scored_total{device=~\"$device\", model=~\"$model\"}[1m])", legend="Scored Completions/s"),
            ],
            unit="reqps", description="Trace ingestion rate compared against logprob scoring throughput",
        ),
        make_timeseries_panel(
            8, "Confidence vs Judge Score Correlation", 12, 4, 12, 8,
            [
                make_target("mlx_token_confidence_mean{device=~\"$device\", model=~\"$model\"}", legend="Logprob Mean Confidence"),
                make_target("avg(mlx_judge_score) by (model)", legend="LLM Judge Average Score"),
            ],
            description="Correlation between statistical token confidence and semantic judge scoring",
        ),
        make_table_panel(
            9, "Recent Traces & Navigation Table", 0, 12, 24, 8,
            [
                make_target("mlx_judge_score{model=~\"$model\"}", legend="Criteria: {{criteria}}", instant=True),
                make_target("mlx_confidence_scored_total{model=~\"$model\"}", legend="Scored Total", instant=True, ref_id="B"),
            ],
            description="Navigation table linking scored requests directly into Opik trace inspector",
            drilldown_url="/d/mlx-mon-09-quality-confidence?var-model=${__data.fields.model}",
            drilldown_title="Open Quality & Confidence",
        ),
    ]
    return make_dashboard(
        "mlx-mon-10-opik-tracing",
        "MLX Opik Tracing & Evaluation",
        "Trace volume, scoring coverage, judge scores and recent trace navigation",
        panels,
    )

# -------------------------------------------------------------------------
# 11. SLO & Error Budget
# -------------------------------------------------------------------------
def build_11_slo_error_budget() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Availability SLO (99.5% Target)", 0, 0, 6, 4,
            "(1 - (sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / sum(rate(mlx_requests_total{kind!=\"eval\"}[1h])))) * 100",
            unit="percent", decimals=2,
            description="Customer-facing 1h availability excluding synthetic evaluation probes",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 99.0}, {"color": "green", "value": 99.5}],
        ),
        make_stat_panel(
            2, "Latency SLO (p95 < 2.0s)", 6, 0, 6, 4,
            "(sum(rate(mlx_ttft_seconds_bucket{le=\"2.0\", kind!=\"eval\"}[1h])) / sum(rate(mlx_ttft_seconds_count{kind!=\"eval\"}[1h]))) * 100",
            unit="percent", decimals=1,
            description="Percentage of production requests satisfying TTFT under 2.0 seconds",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 90.0}, {"color": "green", "value": 95.0}],
            drilldown_url="/d/mlx-mon-06-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            3, "1h Error Budget Remaining", 12, 0, 6, 4,
            "(1 - ((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / sum(rate(mlx_requests_total{kind!=\"eval\"}[1h]))) / 0.005)) * 100",
            unit="percent", decimals=1,
            description="Remaining error budget for 1h window against 0.5% unreliability allowance",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 20.0}, {"color": "green", "value": 80.0}],
        ),
        make_stat_panel(
            4, "30d Rolling Error Budget Remaining", 18, 0, 6, 4,
            "(1 - ((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[30d])) / sum(rate(mlx_requests_total{kind!=\"eval\"}[30d]))) / 0.005)) * 100",
            unit="percent", decimals=1,
            description="30-day cumulative rolling error budget consumption",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 20.0}, {"color": "green", "value": 80.0}],
        ),
        make_timeseries_panel(
            5, "Multi-Window Error Budget Burn Rates", 0, 4, 12, 8,
            [
                make_target("((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / sum(rate(mlx_requests_total{kind!=\"eval\"}[1h]))) / 0.005)", legend="1h Burn Rate (Alert > 14.4x)"),
                make_target("((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[6h])) / sum(rate(mlx_requests_total{kind!=\"eval\"}[6h]))) / 0.005)", legend="6h Burn Rate (Alert > 6x)"),
                make_target("((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[24h])) / sum(rate(mlx_requests_total{kind!=\"eval\"}[24h]))) / 0.005)", legend="24h Burn Rate (Alert > 1x)"),
            ],
            description="Multi-window multi-burn-rate tracking according to Google SRE handbook principles",
        ),
        make_timeseries_panel(
            6, "Eligible Traffic vs Error Classification", 12, 4, 12, 8,
            [
                make_target("sum(rate(mlx_requests_total{kind!=\"eval\"}[1m]))", legend="Eligible Client Requests/s"),
                make_target("sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1m]))", legend="5xx Server Errors/s"),
                make_target("sum(rate(mlx_error_total{status=~\"4..\", kind!=\"eval\"}[1m]))", legend="4xx Client Errors/s"),
            ],
            unit="reqps", description="Request volume filtered for customer traffic versus 4xx and 5xx failures",
        ),
        make_table_panel(
            7, "SLO Degradation & Error Budget Impact by Route", 0, 12, 24, 8,
            [
                make_target("sum(rate(mlx_error_total{kind!=\"eval\"}[5m])) by (status, route)", legend="Errors/s", instant=True),
                make_target("sum(rate(mlx_requests_total{kind!=\"eval\"}[5m])) by (route)", legend="Total/s", instant=True, ref_id="B"),
            ],
            description="Detailed accounting of failures consuming the production error budget",
            drilldown_url="/d/mlx-mon-06-inference-metrics?${__all_variables}",
            drilldown_title="Open Inference Metrics",
        ),
    ]
    return make_dashboard(
        "mlx-mon-11-slo-error-budget",
        "MLX SLO & Error Budget",
        "Latency & availability SLOs, error budgets, and multi-window burn rates",
        panels,
    )

# -------------------------------------------------------------------------
# 12. Server Logs
# -------------------------------------------------------------------------
def build_12_server_logs() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Tailer Up", 0, 0, 4, 4,
            "count(mlx_log_tailer_up == 1)",
            description="Nodes running active log tailer daemon",
        ),
        make_stat_panel(
            2, "Lines Classified / s", 4, 0, 4, 4,
            "sum(rate(mlx_log_tailer_lines_total[1m]))",
            unit="ops", decimals=1,
            description="Rate of server log lines processed and parsed into telemetry",
        ),
        make_stat_panel(
            3, "Lines Shipped / s", 8, 0, 4, 4,
            "sum(rate(mlx_log_tailer_shipped_total[1m]))",
            unit="ops", decimals=1,
            description="Structured log entries successfully delivered to OTLP collector",
        ),
        make_stat_panel(
            4, "Lines Dropped / s", 12, 0, 4, 4,
            "sum(rate(mlx_log_tailer_dropped_total[1m]))",
            unit="ops", decimals=1,
            description="Log lines dropped due to shipper buffer overflows",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_stat_panel(
            5, "Read Offset Bytes", 16, 0, 4, 4,
            "sum(mlx_log_tailer_file_bytes)",
            unit="bytes",
            description="Current file read offset in monitored server log files",
        ),
        make_stat_panel(
            6, "Shipper Errors", 20, 0, 4, 4,
            "sum(rate(mlx_log_tailer_errors_total[5m])) or vector(0)",
            description="Rate of network or serialization failures delivering log events",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_timeseries_panel(
            7, "Log Event Ingestion by Classification", 0, 4, 24, 8,
            [
                make_target("sum(rate(mlx_log_tailer_lines_total{level=\"INFO\"}[1m]))", legend="INFO Lines/s"),
                make_target("sum(rate(mlx_log_tailer_lines_total{level=\"WARN\"}[1m]))", legend="WARN Lines/s"),
                make_target("sum(rate(mlx_log_tailer_lines_total{level=\"ERROR\"}[1m]))", legend="ERROR Lines/s"),
                make_target("sum(rate(mlx_log_tailer_lines_total{event_type=\"OOM\"}[1m]))", legend="OOM Events/s"),
            ],
            unit="ops", description="Real-time log message throughput grouped by severity and lifecycle event",
        ),
        make_table_panel(
            8, "Recent Critical Server Log Events", 0, 12, 24, 8,
            [make_target("mlx_log_tailer_lines_total{level=~\"ERROR|WARN\"}", legend="{{device}} [{{level}}] - {{event_type}}", instant=True)],
            description="Aggregated log alerts, model load failures and memory warning events",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Affected Device",
        ),
    ]
    return make_dashboard(
        "mlx-mon-12-server-logs",
        "MLX Server Logs",
        "Searchable server logs, structured events and log pipeline health",
        panels,
    )

# -------------------------------------------------------------------------
# 13. Hardware Telemetry
# -------------------------------------------------------------------------
def build_13_hardware_telemetry() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Fleet CPU Load15", 0, 0, 4, 4,
            "avg(mlx_hw_load15{device=~\"$device\"})",
            decimals=2, description="Fleet average 15-minute load average",
        ),
        make_stat_panel(
            2, "Physical Memory Used", 4, 0, 4, 4,
            "sum(mlx_hw_mem_used_bytes{device=~\"$device\"})",
            unit="bytes", description="Total Unified Memory actively allocated",
        ),
        make_stat_panel(
            3, "Free Pages Pressure", 8, 0, 4, 4,
            "avg(mlx_hw_mem_pressure{device=~\"$device\"})",
            decimals=2, description="Mach subsystem memory pressure indicator",
        ),
        make_stat_panel(
            4, "Disk Space Used %", 12, 0, 4, 4,
            "avg((mlx_hw_disk_used_bytes{device=~\"$device\"} / mlx_hw_disk_total_bytes{device=~\"$device\"}) * 100)",
            unit="percent", decimals=1,
            description="Root volume disk utilization percentage",
        ),
        make_stat_panel(
            5, "Thermal Pressure Level", 16, 0, 4, 4,
            "max(mlx_hw_thermal_pressure{device=~\"$device\"})",
            description="Peak thermal state reported across cluster nodes",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
            drilldown_url="/d/mlx-mon-14-power-thermal?${__all_variables}",
        ),
        make_stat_panel(
            6, "Hardware Daemons Up", 20, 0, 4, 4,
            "count(mlx_hw_up == 1)",
            description="Active nodes with healthy Apple Silicon hardware daemons",
            drilldown_url="/d/mlx-mon-02-device-health",
        ),
        make_timeseries_panel(
            7, "CPU Load Average vs Physical Cores", 0, 4, 12, 8,
            [
                make_target("mlx_hw_load1{device=~\"$device\"}", legend="{{device}} Load1"),
                make_target("mlx_hw_load5{device=~\"$device\"}", legend="{{device}} Load5"),
                make_target("mlx_hw_cpu_count{device=~\"$device\"}", legend="{{device}} CPU Cores"),
            ],
            description="1m and 5m load averages compared against available hardware core capacity",
        ),
        make_timeseries_panel(
            8, "Unified Memory Distribution (Used vs Available)", 12, 4, 12, 8,
            [
                make_target("mlx_hw_mem_used_bytes{device=~\"$device\"}", legend="{{device}} Used"),
                make_target("mlx_hw_mem_available_bytes{device=~\"$device\"}", legend="{{device}} Available"),
            ],
            unit="bytes", description="Physical Unified Memory allocation and reclaimable cache",
        ),
        make_timeseries_panel(
            9, "Disk I/O Throughput", 0, 12, 12, 8,
            [
                make_target("sum by (device) (process_disk_io_bytes_sec{device=~\"$device\"})", legend="{{device}} Disk I/O B/s"),
            ],
            unit="Bps", description="Process and NVMe disk read and write bandwidth",
        ),
        make_table_panel(
            10, "Host Hardware Overview Table", 12, 12, 12, 8,
            [
                make_target("mlx_hw_cpu_count{device=~\"$device\"}", legend="Cores", instant=True),
                make_target("mlx_hw_mem_total_bytes{device=~\"$device\"}", legend="Total RAM", instant=True, ref_id="B"),
                make_target("mlx_hw_thermal_pressure{device=~\"$device\"}", legend="Thermal State", instant=True, ref_id="C"),
            ],
            description="Hardware specifications and real-time physical status per machine",
            drilldown_url="/d/mlx-mon-14-power-thermal?var-device=${__data.fields.device}",
            drilldown_title="Open Power & Thermal",
        ),
    ]
    return make_dashboard(
        "mlx-mon-13-hardware-telemetry",
        "MLX Hardware Telemetry",
        "CPU, memory, disk, thermal pressure, and Apple Silicon hardware metrics",
        panels,
    )

# -------------------------------------------------------------------------
# 14. Power & Thermal
# -------------------------------------------------------------------------
def build_14_power_thermal() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Package Power (W)", 0, 0, 4, 4,
            "sum(mlx_hw_package_power_milliwatts{device=~\"$device\"}) / 1000",
            unit="watt", decimals=1,
            description="Total SoC package power draw across CPU, GPU, and ANE",
        ),
        make_stat_panel(
            2, "GPU Power (W)", 4, 0, 4, 4,
            "sum(mlx_hw_gpu_power_milliwatts{device=~\"$device\"}) / 1000",
            unit="watt", decimals=1,
            description="Apple Silicon integrated GPU power consumption",
        ),
        make_stat_panel(
            3, "CPU Power (W)", 8, 0, 4, 4,
            "sum(mlx_hw_cpu_power_milliwatts{device=~\"$device\"}) / 1000",
            unit="watt", decimals=1,
            description="Apple Silicon Performance and Efficiency core cluster power draw",
        ),
        make_stat_panel(
            4, "ANE Power (W)", 12, 0, 4, 4,
            "sum(mlx_hw_ane_power_milliwatts{device=~\"$device\"}) / 1000",
            unit="watt", decimals=1,
            description="Apple Neural Engine matrix accelerator power draw",
        ),
        make_stat_panel(
            5, "Thermal Throttling Events", 16, 0, 4, 4,
            "count(mlx_hw_thermal_pressure{device=~\"$device\"} > 0.5) or vector(0)",
            description="Nodes currently experiencing non-nominal thermal pressure",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
        ),
        make_stat_panel(
            6, "Energy / Token (mJ/tok)", 20, 0, 4, 4,
            "(sum(mlx_hw_package_power_milliwatts{device=~\"$device\"}) / (sum(rate(mlx_token_rate_tokens_per_second_sum{device=~\"$device\"}[1m])) > 0)) or vector(0)",
            decimals=1,
            description="Estimated energetic efficiency per generated output token",
        ),
        make_timeseries_panel(
            7, "Apple Silicon SoC Power Decomposition (Watts)", 0, 4, 12, 8,
            [
                make_target("mlx_hw_gpu_power_milliwatts{device=~\"$device\"} / 1000", legend="{{device}} GPU (W)"),
                make_target("mlx_hw_cpu_power_milliwatts{device=~\"$device\"} / 1000", legend="{{device}} CPU (W)"),
                make_target("mlx_hw_ane_power_milliwatts{device=~\"$device\"} / 1000", legend="{{device}} ANE (W)"),
            ],
            unit="watt", stack=True,
            description="Continuous component power breakdown measured via Apple powermetrics",
        ),
        make_timeseries_panel(
            8, "Thermal Pressure State Over Time", 12, 4, 12, 8,
            [make_target("mlx_hw_thermal_pressure{device=~\"$device\"}", legend="{{device}} Thermal State")],
            description="Mach IOKit thermal pressure transitions (0=Nominal, 1=Moderate, 2=Heavy, 3=Trapping)",
        ),
        make_table_panel(
            9, "Power & Thermal Fleet Leaderboard", 0, 12, 24, 8,
            [
                make_target("mlx_hw_package_power_milliwatts{device=~\"$device\"} / 1000", legend="Package Power (W)", instant=True),
                make_target("mlx_hw_gpu_power_milliwatts{device=~\"$device\"} / 1000", legend="GPU Power (W)", instant=True, ref_id="B"),
                make_target("mlx_hw_thermal_pressure{device=~\"$device\"}", legend="Thermal State", instant=True, ref_id="C"),
            ],
            description="Per-device power consumption and thermal throttling status",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Device Detail",
        ),
    ]
    return make_dashboard(
        "mlx-mon-14-power-thermal",
        "MLX Power & Thermal",
        "Apple Silicon SoC power draw, thermal throttling and temperature trends",
        panels,
    )

# -------------------------------------------------------------------------
# 15. Process Profiler
# -------------------------------------------------------------------------
def build_15_process_profiler() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Profiler Up", 0, 0, 4, 4,
            "count(process_profiler_up == 1)",
            description="Nodes running active process telemetry sampler",
            drilldown_url="/d/mlx-mon-02-device-health",
        ),
        make_stat_panel(
            2, "[Measured] Host GPU Utilization", 4, 0, 4, 4,
            "mlx_hw_gpu_utilization_percent{device=~\"$device\"}",
            unit="percent", decimals=1,
            description="True hardware GPU busy residency measured directly from IOKit",
        ),
        make_stat_panel(
            3, "[Estimated] MLX Server GPU Share", 8, 0, 4, 4,
            "process_gpu_utilization_percent{device=~\"$device\", process=\"mlx_lm.server\"} or (process_gpu_utilization_percent{device=~\"$device\"})",
            unit="percent", decimals=1,
            description="Estimated GPU attribution allocated to MLX server process",
        ),
        make_stat_panel(
            4, "Active Sampled Processes", 12, 0, 4, 4,
            "count(count by (pid) (process_cpu_percent{device=~\"$device\"}))",
            description="Dynamic top resource-consuming processes actively tracked",
        ),
        make_stat_panel(
            5, "Host Memory Used %", 16, 0, 4, 4,
            "(mlx_hw_mem_used_bytes{device=~\"$device\"} / mlx_hw_mem_total_bytes{device=~\"$device\"}) * 100",
            unit="percent", decimals=1,
            description="Overall host Unified Memory consumption percentage",
            drilldown_url="/d/mlx-mon-13-hardware-telemetry?${__all_variables}",
        ),
        make_stat_panel(
            6, "Sampling Resolution", 20, 0, 4, 4,
            "avg(rate(process_profiler_samples_total{device=~\"$device\"}[1m])) or vector(5)",
            unit="s", decimals=1,
            description="Background process telemetry refresh cycle interval",
        ),
        make_timeseries_panel(
            7, "Top 10 Processes CPU Consumption (Resolved Names)", 0, 4, 12, 8,
            [make_target("topk(10, process_cpu_percent{device=~\"$device\"})", legend="{{process}} (pid={{pid}})")],
            unit="percent", description="Real-time process CPU consumption with human-readable process names",
        ),
        make_timeseries_panel(
            8, "Process Resident Memory (RSS) Footprint", 12, 4, 12, 8,
            [make_target("topk(10, process_memory_rss_bytes{device=~\"$device\"})", legend="{{process}} (pid={{pid}})")],
            unit="bytes", description="Memory allocation tracking per process to detect leaks and OOM risks",
        ),
        make_table_panel(
            9, "Process Resource Leaderboard", 0, 12, 16, 8,
            [
                make_target("process_cpu_percent{device=~\"$device\"}", legend="CPU %", instant=True),
                make_target("process_memory_rss_bytes{device=~\"$device\"}", legend="RSS Memory", instant=True, ref_id="B"),
                make_target("process_gpu_utilization_percent{device=~\"$device\"}", legend="[Estimated] GPU %", instant=True, ref_id="C"),
            ],
            description="Full process table including PID, resolved process name, user, CPU%, RSS, and GPU estimate",
            drilldown_url="/d/mlx-mon-03-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Device Detail",
        ),
        make_table_panel(
            10, "Attribution Methodology & Trust Levels", 16, 12, 8, 8,
            [make_target("process_ranking_info{device=~\"$device\"}", legend="{{process}} rank={{rank}}", instant=True)],
            description="Verified host GPU metrics are directly measured via IOKit; per-process GPU is an estimated model share",
        ),
    ]
    return make_dashboard(
        "mlx-mon-15-process-profiler",
        "MLX Process & Resource Profiler",
        "Top process CPU/RSS, disk activity and explicitly estimated GPU/ANE attribution",
        panels,
    )

# -------------------------------------------------------------------------
# 16. Network (Host & Peers)
# -------------------------------------------------------------------------
def build_16_network_host_peers() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Host Interface RX Rate", 0, 0, 4, 4,
            "sum(rate(mlx_net_rx_bytes_total{device=~\"$device\"}[1m])) or sum(mlx_hw_net_rx_bytes_per_sec{device=~\"$device\"})",
            unit="Bps",
            description="Inbound bandwidth on physical host network interface",
        ),
        make_stat_panel(
            2, "Host Interface TX Rate", 4, 0, 4, 4,
            "sum(rate(mlx_net_tx_bytes_total{device=~\"$device\"}[1m])) or sum(mlx_hw_net_tx_bytes_per_sec{device=~\"$device\"})",
            unit="Bps",
            description="Outbound bandwidth on physical host network interface",
        ),
        make_stat_panel(
            3, "RX Errors / s", 8, 0, 4, 4,
            "sum(rate(mlx_net_rx_errors_total{device=~\"$device\"}[1m])) or vector(0)",
            description="Inbound physical packet errors reported by network driver",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_stat_panel(
            4, "TX Errors / s", 12, 0, 4, 4,
            "sum(rate(mlx_net_tx_errors_total{device=~\"$device\"}[1m])) or vector(0)",
            description="Outbound physical packet errors reported by network driver",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_stat_panel(
            5, "Peer Probes Reachable", 16, 0, 4, 4,
            "((count((mlx_shard_peer_up == 1) or (mlx_shard_peer_probe_status == 1)) / (count(mlx_shard_peer_up or mlx_shard_peer_probe_status) > 0)) * 100) or vector(100)",
            unit="percent", decimals=1,
            description="Distributed interconnect peer TCP connect probe success percentage",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 90}, {"color": "green", "value": 99}],
            drilldown_url="/d/mlx-mon-07-model-sharding?${__all_variables}",
        ),
        make_stat_panel(
            6, "Peer RTT p95", 20, 0, 4, 4,
            "histogram_quantile(0.95, sum(rate(mlx_shard_peer_connect_seconds_bucket[5m])) by (le)) or histogram_quantile(0.95, sum(rate(mlx_shard_peer_rtt_seconds_bucket[5m])) by (le)) or vector(0.001)",
            unit="s", decimals=3,
            description="Round-trip TCP probe latency across distributed cluster interconnect",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 0.005}, {"color": "red", "value": 0.02}],
        ),
        make_timeseries_panel(
            7, "[Host Network] Physical Interface Throughput (RX / TX)", 0, 4, 12, 8,
            [
                make_target("rate(mlx_net_rx_bytes_total{device=~\"$device\"}[1m]) or mlx_hw_net_rx_bytes_per_sec{device=~\"$device\"}", legend="{{device}} RX Bytes/s"),
                make_target("rate(mlx_net_tx_bytes_total{device=~\"$device\"}[1m]) or mlx_hw_net_tx_bytes_per_sec{device=~\"$device\"}", legend="{{device}} TX Bytes/s"),
            ],
            unit="Bps", description="Default host network throughput (e.g. en0 1GbE/10GbE interface)",
        ),
        make_timeseries_panel(
            8, "[Host Network] Interface Error & Drop Counters", 12, 4, 12, 8,
            [
                make_target("rate(mlx_net_rx_errors_total{device=~\"$device\"}[1m])", legend="{{device}} RX Errors/s"),
                make_target("rate(mlx_net_tx_errors_total{device=~\"$device\"}[1m])", legend="{{device}} TX Errors/s"),
            ],
            description="Hardware packet errors and buffer drops on physical interfaces",
        ),
        make_table_panel(
            9, "[Distributed Interconnect] Peer Ring Probe Status & RTT", 0, 12, 24, 8,
            [
                make_target("(mlx_shard_peer_up{device=~\"$device\"} or mlx_shard_peer_probe_status{device=~\"$device\"}) or vector(1)", legend="Status (1=Up, 0=Down)", instant=True),
                make_target("(mlx_shard_peer_connect_seconds{device=~\"$device\"} or mlx_shard_peer_rtt_seconds{device=~\"$device\"}) or vector(0.001)", legend="RTT (s)", instant=True, ref_id="B"),
            ],
            description="Peer-to-peer interconnect reachability and latency matrix separated from default host routing",
            drilldown_url="/d/mlx-mon-07-model-sharding?var-device=${__data.fields.device}",
            drilldown_title="Open Model Sharding",
        ),
    ]
    return make_dashboard(
        "mlx-mon-16-network-host-peers",
        "MLX Network (Host & Peers)",
        "Selected host interface throughput, errors and peer reachability probes",
        panels,
    )

# -------------------------------------------------------------------------
# 17. SBOM Fleet Inventory
# -------------------------------------------------------------------------
def build_17_sbom_inventory() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Total Cataloged Packages", 0, 0, 4, 4,
            "sum(mac_sbom_total_packages{device=~\"$device\"})",
            description="Aggregate count of cataloged software packages across all environments",
            drilldown_url="/d/mlx-mon-18-sbom-drift-delta?${__all_variables}",
        ),
        make_stat_panel(
            2, "Python / pip Packages", 4, 0, 4, 4,
            "count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"python\"})",
            description="Packages cataloged across virtualenvs and global Python installations",
        ),
        make_stat_panel(
            3, "Homebrew Formulae", 8, 0, 4, 4,
            "count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"homebrew\"})",
            description="Homebrew formulas and cellar packages installed on hosts",
        ),
        make_stat_panel(
            4, "System Binaries", 12, 0, 4, 4,
            "count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"system\"})",
            description="System binaries cataloged in Darwin/Linux core utilities",
        ),
        make_stat_panel(
            5, "MLX Model Artifacts", 16, 0, 4, 4,
            "count(count by (model) (mlx_model_info{device=~\"$device\"}))",
            description="Model snapshot weights, tokenizers, and configuration files cataloged",
            drilldown_url="/d/mlx-mon-07-model-sharding?${__all_variables}",
        ),
        make_stat_panel(
            6, "Inventory Freshness Age", 20, 0, 4, 4,
            "time() - max(mac_sbom_last_scan_timestamp_seconds{device=~\"$device\"})",
            unit="s", decimals=0,
            description="Time elapsed since last completed SBOM filesystem scan",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 3600}, {"color": "red", "value": 7200}],
        ),
        make_timeseries_panel(
            7, "Packages by Ecosystem Over Time", 0, 4, 12, 8,
            [
                make_target("count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"python\"})", legend="Python (pip)"),
                make_target("count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"homebrew\"})", legend="Homebrew"),
                make_target("count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"system\"})", legend="System"),
            ],
            stack=True, description="Ecosystem package count evolution over time",
        ),
        make_table_panel(
            8, "Top Packages by Fleet Coverage", 12, 4, 12, 8,
            [make_target("mac_sbom_package_info{device=~\"$device\"}", legend="{{package}} ({{version}})", instant=True)],
            description="Most widely deployed dependencies across all cluster nodes",
            drilldown_url="/d/mlx-mon-18-sbom-drift-delta?var-device=${__data.fields.device}",
            drilldown_title="Open SBOM Drift",
        ),
        make_table_panel(
            9, "Cataloged Package, Version, and Executable Path Inventory", 0, 12, 24, 8,
            [make_target("mac_sbom_package_info{device=~\"$device\"}", legend="{{device}}: {{package}}=={{version}} ({{ecosystem}})", instant=True)],
            description="Comprehensive searchable inventory of packages, versions, and installation paths",
            drilldown_url="/d/mlx-mon-18-sbom-drift-delta?var-device=${__data.fields.device}",
            drilldown_title="Open SBOM Drift",
        ),
    ]
    return make_dashboard(
        "mlx-mon-17-sbom-inventory",
        "MLX SBOM Fleet Inventory",
        "Package and model inventory across Python, Homebrew and system ecosystems",
        panels,
    )

# -------------------------------------------------------------------------
# 18. SBOM Drift & Delta
# -------------------------------------------------------------------------
def build_18_sbom_drift_delta() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Devices in Sync", 0, 0, 4, 4,
            "count(mac_sbom_drift_status == 1)",
            description="Nodes matching the golden reference baseline with zero discrepancies",
            drilldown_url="/d/mlx-mon-17-sbom-inventory?${__all_variables}",
        ),
        make_stat_panel(
            2, "Deviated Devices", 4, 0, 4, 4,
            "count(mac_sbom_drift_mismatch_count > 0 or mac_sbom_missing_package_count > 0) or vector(0)",
            description="Nodes exhibiting version, hash, or package mismatches against baseline",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
        ),
        make_stat_panel(
            3, "Version Differences", 8, 0, 4, 4,
            "sum(mac_sbom_drift_mismatch_count)",
            description="Total package version discrepancies detected across the cluster",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 5}],
        ),
        make_stat_panel(
            4, "Missing Packages", 12, 0, 4, 4,
            "sum(mac_sbom_missing_package_count)",
            description="Required baseline packages absent on target edge nodes",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_stat_panel(
            5, "Hash / Path Diffs", 16, 0, 4, 4,
            "sum(mac_sbom_drift_mismatch_count)",
            description="Binaries or libraries matching version but differing in executable path or hash",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
        ),
        make_stat_panel(
            6, "Fleet Uniformity Score", 20, 0, 4, 4,
            "avg(mac_sbom_drift_status) * 100",
            unit="percent", decimals=1,
            description="Cluster-wide configuration and dependency consistency score",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 90}, {"color": "green", "value": 98}],
        ),
        make_timeseries_panel(
            7, "Drift Severity by Target Device", 0, 4, 12, 8,
            [
                make_target("mac_sbom_drift_mismatch_count{device=~\"$device\"}", legend="{{device}} Version Diffs"),
                make_target("mac_sbom_missing_package_count{device=~\"$device\"}", legend="{{device}} Missing Pkgs"),
            ],
            description="Drift breakdown per node compared against baseline",
        ),
        make_table_panel(
            8, "Baseline Provenance & Authority Status", 12, 4, 12, 8,
            [make_target("mac_sbom_device_drift_summary", legend="Baseline: {{base_device}}", instant=True)],
            description="Provenance, publication timestamp, and authority node of the active baseline",
            drilldown_url="/d/mlx-mon-17-sbom-inventory?var-device=${__data.fields.device}",
            drilldown_title="Open SBOM Inventory",
        ),
        make_table_panel(
            9, "Package Differences Detail (Baseline vs Current)", 0, 12, 24, 8,
            [make_target("mac_sbom_device_drift_summary{device=~\"$device\"}", legend="{{device}} status={{drift_status}}", instant=True)],
            description="Granular delta table specifying package name, baseline version, current version, and change type",
            drilldown_url="/d/mlx-mon-17-sbom-inventory?var-device=${__data.fields.device}",
            drilldown_title="Open SBOM Inventory",
        ),
    ]
    return make_dashboard(
        "mlx-mon-18-sbom-drift-delta",
        "MLX SBOM Drift & Delta",
        "Version, hash, path and missing-package differences against a versioned baseline",
        panels,
    )

def main():
    generators = [
        ("01-cluster-overview.json", build_01_cluster_overview),
        ("02-device-health-diagnostics.json", build_02_device_health),
        ("03-per-device-detail.json", build_03_per_device_detail),
        ("04-alerts-incidents.json", build_04_alerts_incidents),
        ("05-serving-health.json", build_05_serving_health),
        ("06-inference-metrics.json", build_06_inference_metrics),
        ("07-model-architecture-sharding.json", build_07_model_sharding),
        ("08-kv-cache.json", build_08_kv_cache),
        ("09-quality-confidence.json", build_09_quality_confidence),
        ("10-opik-tracing-evaluation.json", build_10_opik_tracing),
        ("11-slo-error-budget.json", build_11_slo_error_budget),
        ("12-server-logs.json", build_12_server_logs),
        ("13-hardware-telemetry.json", build_13_hardware_telemetry),
        ("14-power-thermal.json", build_14_power_thermal),
        ("15-process-profiler.json", build_15_process_profiler),
        ("16-network-host-peers.json", build_16_network_host_peers),
        ("17-sbom-fleet-inventory.json", build_17_sbom_inventory),
        ("18-sbom-drift-delta.json", build_18_sbom_drift_delta),
    ]

    os.makedirs(OUT_DIR, exist_ok=True)
    generated = []
    for filename, gen_fn in generators:
        dash_obj = gen_fn()
        path = os.path.join(OUT_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dash_obj, f, indent=2)
        generated.append(filename)
        print(f"Generated {filename} (UID: {dash_obj['uid']}, Panels: {len(dash_obj['panels'])})")

    print(f"\nSuccessfully generated {len(generated)} reference dashboards in {OUT_DIR}")

if __name__ == "__main__":
    main()
