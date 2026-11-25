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
    ("mlx-mon-cluster-overview", "Cluster Overview", "Fleet availability, serving health and key inference KPIs"),
    ("mlx-mon-device-health", "Device Health & Diagnostics", "Per-device component status, exporter freshness and connectivity"),
    ("mlx-mon-per-device-detail", "Per-Device Detail", "Single-node deep dive from host resources to inference"),
    ("mlx-mon-alerts-incidents", "Alerts & Incidents", "Active alert triage, severity distribution and runbooks"),
    ("mlx-mon-serving-health", "Serving Health", "MLX server lifecycle, readiness and restart history"),
    ("mlx-mon-inference-metrics", "Inference Metrics", "Inference latency breakdown, token throughput and concurrency"),
    ("mlx-mon-model-sharding", "Model Architecture & Sharding", "Model metadata, rank placement, layer ranges and peer probes"),
    ("mlx-mon-kv-cache", "KV Cache", "Prompt cache utilization, context capacity and per-rank allocation"),
    ("mlx-mon-quality-confidence", "Quality & Confidence", "Logprob scores, token entropy and canary evaluation"),
    ("mlx-mon-opik-tracing", "Opik Tracing & Evaluation", "Trace volume, scoring coverage, judge scores and traces"),
    ("mlx-mon-slo-error-budget", "SLO & Error Budget", "Latency & availability SLOs, error budgets, and burn rates"),
    ("mlx-mon-server-logs", "Server Logs", "Searchable server logs, structured events and log pipeline health"),
    ("mlx-mon-hardware-telemetry", "Hardware Telemetry", "CPU, memory, disk, thermal pressure, and Apple Silicon metrics"),
    ("mlx-mon-power-thermal", "Power & Thermal", "Apple Silicon SoC power draw, thermal throttling and temperature"),
    ("mlx-mon-process-profiler", "Process & Resource Profiler", "Top process CPU/RSS and measured host GPU vs process attribution"),
    ("mlx-mon-network-host-peers", "Network (Host & Peers)", "Host interface throughput, errors and peer reachability probes"),
    ("mlx-mon-sbom-inventory", "SBOM Fleet Inventory", "Package and model inventory across Python, Homebrew and system"),
    ("mlx-mon-sbom-drift-delta", "SBOM Drift & Delta", "Version, hash, path and missing-package differences against baseline"),
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
    mappings: Optional[List[Dict[str, Any]]] = None,
    text_mode: str = "auto",
    legend: str = "",
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
        "targets": [make_target(expr, legend=legend, instant=True)],
        "options": {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "auto",
            "textMode": text_mode,
            "colorMode": color_mode,
            "graphMode": "area" if sparkline else "none",
            "justifyMode": "auto",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": thresholds},
                "mappings": mappings or [],
                "links": links,
            },
            "overrides": [],
        },
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p

def assign_unique_ref_ids(targets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    used = set()
    result = []
    for idx, t in enumerate(targets):
        t_copy = dict(t)
        current = t_copy.get("refId")
        if not current or current in used:
            cand_idx = idx
            while True:
                cand = chr(ord('A') + cand_idx) if cand_idx < 26 else f"T{cand_idx}"
                if cand not in used:
                    current = cand
                    break
                cand_idx += 1
        t_copy["refId"] = current
        used.add(current)
        result.append(t_copy)
    return result

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
        "targets": assign_unique_ref_ids(targets),
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
        "targets": assign_unique_ref_ids(targets),
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
        "targets": assign_unique_ref_ids(targets),
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
    if not uid.startswith("mlx-mon-"):
        uid = f"mlx-mon-{uid.removeprefix('mlx-')}"
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
# Cluster Overview
# -------------------------------------------------------------------------
def build_cluster_overview() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Devices online", 0, 0, 4, 4,
            "count(count by (device) (mlx_hw_up == 1))",
            description="Total Apple Silicon cluster nodes reporting healthy hardware telemetry",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-device-health",
        ),
        make_stat_panel(
            2, "Serving ready", 4, 0, 4, 4,
            "count(count by (device) (mlx_server_ready == 1))",
            description="MLX inference model server instances passing HTTP /v1/models readiness",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-serving-health",
        ),
        make_stat_panel(
            3, "Requests / s", 8, 0, 4, 4,
            "(sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[5m]))) or vector(0)",
            unit="reqps", decimals=1,
            description="Cluster-wide client inference request throughput observed at proxy layer",
            drilldown_url="/d/mlx-mon-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            4, "TTFT p95", 12, 0, 4, 4,
            "(histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or vector(0)",
            unit="s", decimals=2,
            description="95th percentile Time to First Token observed at the proxy HTTP edge",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.5}, {"color": "red", "value": 3.0}],
            drilldown_url="/d/mlx-mon-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            5, "Decode tok/s", 16, 0, 4, 4,
            "(sum(rate(mlx_token_rate_tokens_per_second_sum{device=~\"$device\", model=~\"$model\"}[5m])) / (sum(rate(mlx_token_rate_tokens_per_second_count{device=~\"$device\", model=~\"$model\"}[5m])) > 0)) or vector(0)",
            unit="tok/s", decimals=1,
            description="Cluster average token generation throughput for active completions",
            drilldown_url="/d/mlx-mon-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            6, "Active alerts", 20, 0, 4, 4,
            "count(ALERTS{alertstate=\"firing\", severity=~\"critical|warning\"}) or vector(0)",
            description="Total critical and warning alerts actively firing in VictoriaMetrics alert manager",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
            drilldown_url="/d/mlx-mon-alerts-incidents",
        ),
        make_timeseries_panel(
            7, "Request rate by device", 0, 4, 12, 8,
            [make_target("rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])", legend="{{device}} - {{model}}")],
            unit="reqps", description="Per-device request volume over time",
        ),
        make_timeseries_panel(
            8, "TTFT p50 / p95", 12, 4, 12, 8,
            [
                make_target("(histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])))", legend="p95 TTFT", ref_id="A"),
                make_target("(histogram_quantile(0.50, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])))", legend="p50 TTFT", ref_id="B"),
            ],
            unit="s", description="Proxy-observed time-to-first-token latency progression",
        ),
        make_table_panel(
            9, "Fleet status", 0, 12, 12, 8,
            [
                make_target("mlx_server_state{device=~\"$device\"}", legend="Status", instant=True),
                make_target("(histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\"}[5m])) by (le, device))) or (0 * count by (device) (mlx_server_state{device=~\"$device\"}))", legend="TTFT p95", instant=True, ref_id="B"),
                make_target("mlx_model_info{attr=\"layer_count\", device=~\"$device\"}", legend="Model", instant=True, ref_id="C"),
                make_target("(count by (device) (ALERTS{alertstate=\"firing\"})) or (0 * count by (device) (mlx_server_state{device=~\"$device\"}))", legend="Alerts", instant=True, ref_id="D"),
            ],
            description="Current device state (0=Down, 1=Ready, 2=Starting, 3=Backoff) and p95 TTFT",
            drilldown_url="/d/mlx-mon-per-device-detail?var-device=${__data.fields.device}&${__all_variables}",
            drilldown_title="Open Device Detail",
        ),
        make_table_panel(
            10, "Active incidents", 12, 12, 12, 8,
            [make_target("ALERTS{alertstate=\"firing\"}", legend="{{alertname}} - {{severity}}", instant=True)],
            description="Real-time alert inventory displaying severity, affected device and alert name",
            drilldown_url="/d/mlx-mon-alerts-incidents",
            drilldown_title="Open Alerts & Incidents",
        ),
    ]
    return make_dashboard(
        "mlx-cluster-overview",
        "Cluster Overview",
        "Fleet availability, serving health and key inference KPIs across all Apple Silicon nodes",
        panels,
    )

# -------------------------------------------------------------------------
# Device Health & Diagnostics
# -------------------------------------------------------------------------
def build_device_health() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Hardware exporters", 0, 0, 5, 4,
            "count(mlx_hw_up == 1)",
            description="Reporting healthy hardware telemetry",
            drilldown_url="/d/mlx-mon-hardware-telemetry?${__all_variables}",
        ),
        make_stat_panel(
            2, "Server ready", 5, 0, 5, 4,
            "count(mlx_server_ready == 1)",
            description="Ready devices passing HTTP /v1/models probe",
            drilldown_url="/d/mlx-mon-serving-health",
        ),
        make_stat_panel(
            3, "Failed exporters", 10, 0, 5, 4,
            "count((mlx_hw_up == 0) or (mlx_kv_agent_up == 0) or (mlx_up == 0) or (process_profiler_up == 0) or (mac_sbom_collector_up == 0) or (mlx_accuracy_eval_up == 0) or (mlx_log_tailer_up == 0)) or vector(0)",
            description="Needs investigation",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_stat_panel(
            4, "Affected devices", 15, 0, 5, 4,
            "count(count by (device) (ALERTS{alertstate=\"firing\"})) or vector(0)",
            description="Alerting devices",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
            drilldown_url="/d/mlx-mon-alerts-incidents",
        ),
        make_stat_panel(
            5, "Unknown status", 20, 0, 4, 4,
            "count(count by (device) (absent(mlx_hw_up{device=~\"$device\"}))) or vector(0)",
            description="Missing telemetry",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_state_timeline_panel(
            6, "Device component matrix", 0, 4, 14, 8,
            [
                make_target("mlx_hw_up{device=~\"$device\"}", legend="{{device}} [hw]"),
                make_target("mlx_server_ready{device=~\"$device\"}", legend="{{device}} [server]"),
                make_target("mlx_up{device=~\"$device\"}", legend="{{device}} [proxy]"),
                make_target("mlx_kv_agent_up{device=~\"$device\"}", legend="{{device}} [kv]"),
                make_target("process_profiler_up{device=~\"$device\"}", legend="{{device}} [process]"),
                make_target("mac_sbom_collector_up{device=~\"$device\"}", legend="{{device}} [sbom]"),
            ],
            description="Host · Proxy · Server · KV · Process · SBOM operational state timeline",
        ),
        make_table_panel(
            7, "Exporter freshness", 14, 4, 10, 8,
            [
                make_target("mlx_hw_up{device=~\"$device\"}", legend="mlx-hw", instant=True),
                make_target("mlx_up{device=~\"$device\"}", legend="mlx-proxy", instant=True, ref_id="B"),
                make_target("mlx_kv_agent_up{device=~\"$device\"}", legend="mlx-kv", instant=True, ref_id="C"),
                make_target("process_profiler_up{device=~\"$device\"}", legend="mlx-process", instant=True, ref_id="D"),
            ],
            description="Last successful scrape / sample",
            drilldown_url="/d/mlx-mon-per-device-detail?var-device=${__data.fields.device}&${__all_variables}",
            drilldown_title="Open Device Detail",
        ),
        make_state_timeline_panel(
            8, "Availability timeline", 0, 12, 12, 8,
            [make_target("mlx_server_state{device=~\"$device\"}", legend="{{device}}")],
            description="Last 6 hours device state timeline",
        ),
        make_table_panel(
            9, "Connectivity checks", 12, 12, 12, 8,
            [
                make_target("(mlx_shard_peer_up{device=~\"$device\"} or mlx_shard_peer_probe_status{device=~\"$device\"}) or vector(1)", legend="Peer TCP", instant=True),
                make_target("mlx_server_ready{device=~\"$device\"}", legend="HTTP Health", instant=True, ref_id="B"),
                make_target("mlx_hw_up{device=~\"$device\"}", legend="SSH / Host", instant=True, ref_id="C"),
                make_target("(mlx_shard_peer_connect_seconds{device=~\"$device\"} or mlx_shard_peer_rtt_seconds{device=~\"$device\"}) or vector(0.001)", legend="Latency", instant=True, ref_id="D"),
            ],
            description="Local SSH, HTTP health, peer TCP connectivity verification",
            drilldown_url="/d/mlx-mon-network-host-peers?var-device=${__data.fields.device}&${__all_variables}",
            drilldown_title="Open Network Detail",
        ),
    ]
    return make_dashboard(
        "mlx-device-health",
        "Device Health & Diagnostics",
        "Per-device component status, exporter freshness and network connectivity",
        panels,
    )

# -------------------------------------------------------------------------
# Per-Device Detail
# -------------------------------------------------------------------------
def build_per_device_detail() -> Dict[str, Any]:
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
            1, "Device status", 0, 0, 4, 4,
            "mlx_server_state{device=\"$device\"}",
            description="Current MLX model server status (0=Down, 1=Ready, 2=Starting, 3=Backoff)",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}, {"color": "yellow", "value": 2}],
            drilldown_url="/d/mlx-mon-serving-health",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "DOWN", "color": "red"},
                        "1": {"text": "READY", "color": "green"},
                        "2": {"text": "STARTING", "color": "yellow"},
                        "3": {"text": "BACKOFF", "color": "orange"},
                    },
                }
            ],
        ),
        make_stat_panel(
            2, "Model", 4, 0, 4, 4,
            "mlx_model_info{device=\"$device\", attr=\"layer_count\"}",
            description="Loaded on node",
            legend="{{model}}",
            text_mode="name",
        ),
        make_stat_panel(
            3, "Rank", 8, 0, 4, 4,
            "mlx_shard_rank{device=\"$device\"} or vector(0)",
            description="Distributed world rank",
            drilldown_url="/d/mlx-mon-model-sharding?var-device=$device",
        ),
        make_stat_panel(
            4, "CPU", 12, 0, 4, 4,
            "mlx_proc_cpu_percent{device=\"$device\"} or (avg by (device) (process_cpu_percent{device=\"$device\"}))",
            unit="percent", decimals=1,
            description="Host Apple Silicon physical CPU core utilization",
            drilldown_url="/d/mlx-mon-process-profiler?var-device=$device",
        ),
        make_stat_panel(
            5, "RAM available", 16, 0, 4, 4,
            "(mlx_hw_mem_available_bytes{device=\"$device\"} / mlx_hw_mem_total_bytes{device=\"$device\"}) * 100",
            unit="percent", decimals=1,
            description="Available Unified Memory percentage",
            drilldown_url="/d/mlx-mon-hardware-telemetry?var-device=$device",
        ),
        make_stat_panel(
            6, "GPU memory", 20, 0, 4, 4,
            "(mlx_hw_gpu_mem_used_bytes{device=\"$device\"} / (mlx_hw_gpu_mem_total_bytes{device=\"$device\"} > 0)) * 100 or ((mlx_hw_mem_used_bytes{device=\"$device\"} / mlx_hw_mem_total_bytes{device=\"$device\"}) * 100)",
            unit="percent", decimals=1,
            description="Allocated Unified Memory percentage",
            drilldown_url="/d/mlx-mon-hardware-telemetry?var-device=$device",
        ),
        make_timeseries_panel(
            7, "Host resource trends", 0, 4, 12, 8,
            [
                make_target("mlx_proc_cpu_percent{device=\"$device\"} or (avg by (device) (process_cpu_percent{device=\"$device\"}))", legend="CPU Usage %"),
                make_target("(mlx_hw_mem_used_bytes{device=\"$device\"} / mlx_hw_mem_total_bytes{device=\"$device\"}) * 100", legend="Memory Used %"),
                make_target("mlx_hw_gpu_utilization_percent{device=\"$device\"}", legend="GPU Utilization %"),
            ],
            unit="percent", description="SoC resource utilization on target device over time",
        ),
        make_timeseries_panel(
            8, "Serving throughput", 12, 4, 12, 8,
            [
                make_target("rate(mlx_requests_total{device=\"$device\"}[1m])", legend="Requests/s"),
                make_target("(rate(mlx_token_rate_tokens_per_second_sum{device=\"$device\"}[1m]) / (rate(mlx_token_rate_tokens_per_second_count{device=\"$device\"}[1m]) > 0)) or (0 * rate(mlx_requests_total{device=\"$device\"}[1m]))", legend="Tokens/s (decode)"),
            ],
            description="Throughput metrics observed on this specific host",
        ),
        make_table_panel(
            9, "Current incidents", 0, 12, 12, 8,
            [make_target("ALERTS{alertstate=\"firing\", device=\"$device\"}", legend="{{alertname}} - {{severity}}", instant=True)],
            description="Active alerts specifically targeting this selected node",
            drilldown_url="/d/mlx-mon-alerts-incidents",
            drilldown_title="Open Alerts & Incidents",
        ),
        make_table_panel(
            10, "Component health", 12, 12, 12, 8,
            [
                make_target("mlx_hw_up{device=\"$device\"}", legend="mlx-hw", instant=True),
                make_target("mlx_server_ready{device=\"$device\"}", legend="mlx-server", instant=True, ref_id="B"),
                make_target("mlx_up{device=\"$device\"}", legend="mlx-proxy", instant=True, ref_id="C"),
                make_target("mlx_kv_agent_up{device=\"$device\"}", legend="mlx-kv", instant=True, ref_id="D"),
                make_target("process_profiler_up{device=\"$device\"}", legend="mlx-process", instant=True, ref_id="E"),
            ],
            description="Verification of individual edge daemons on this node",
            drilldown_url="/d/mlx-mon-device-health",
            drilldown_title="Open Device Health",
        ),
    ]
    return make_dashboard(
        "mlx-per-device-detail",
        "Per-Device Detail",
        "Single-node deep dive from host resources to inference and incidents",
        panels,
        variables=node_vars,
    )

# -------------------------------------------------------------------------
# Alerts & Incidents
# -------------------------------------------------------------------------
def build_alerts_incidents() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Critical", 0, 0, 5, 4,
            "count(ALERTS{alertstate=\"firing\", severity=\"critical\"}) or vector(0)",
            description="Needs immediate action",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            drilldown_url="/d/mlx-mon-device-health",
        ),
        make_stat_panel(
            2, "Warning", 5, 0, 5, 4,
            "count(ALERTS{alertstate=\"firing\", severity=\"warning\"}) or vector(0)",
            description="Investigate",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}],
            drilldown_url="/d/mlx-mon-device-health",
        ),
        make_stat_panel(
            3, "Firing total", 10, 0, 5, 4,
            "count(ALERTS{alertstate=\"firing\"}) or vector(0)",
            description="Current alerts",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
        ),
        make_stat_panel(
            4, "Affected devices", 15, 0, 5, 4,
            "count(count by (device) (ALERTS{alertstate=\"firing\"})) or vector(0)",
            description="Unique devices",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
            drilldown_url="/d/mlx-mon-device-health",
        ),
        make_stat_panel(
            5, "Resolved (24h)", 20, 0, 4, 4,
            "(sum(changes(ALERTS{alertstate=\"firing\"}[24h])) - count(ALERTS{alertstate=\"firing\"})) or vector(0)",
            description="Historical alerts",
            thresholds=[{"color": "green", "value": None}],
        ),
        make_table_panel(
            6, "Active alert queue", 0, 4, 14, 8,
            [make_target("ALERTS{alertstate=\"firing\"}", legend="{{alertname}} on {{device}}", instant=True)],
            description="Severity, alert, device, duration",
            drilldown_url="/d/mlx-mon-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Affected Device",
        ),
        make_state_timeline_panel(
            7, "Alert timeline", 14, 4, 10, 8,
            [make_target("(count by (device) (ALERTS{alertstate=\"firing\"})) or (0 * count by (device) (mlx_hw_up))", legend="{{device}}")],
            description="Firing intervals by device",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "Clear", "color": "green"},
                        "1": {"text": "Warning", "color": "orange"},
                        "2": {"text": "Critical", "color": "red"},
                    },
                }
            ],
        ),
        make_timeseries_panel(
            8, "Alert counts over time", 0, 12, 12, 8,
            [
                make_target("count(ALERTS{alertstate=\"firing\", severity=\"critical\"}) or vector(0)", legend="Critical"),
                make_target("count(ALERTS{alertstate=\"firing\", severity=\"warning\"}) or vector(0)", legend="Warning"),
                make_target("count(ALERTS{alertstate=\"firing\", severity=~\"info|none\"}) or vector(0)", legend="Info"),
            ],
            draw_style="bars",
            stack=True,
            description="Critical / warning / info",
        ),
        make_table_panel(
            9, "Runbook and ownership", 12, 12, 12, 8,
            [make_target("ALERTS{alertstate=\"firing\"}", legend="{{alertname}}", instant=True)],
            description="Alert owner and runbook action",
        ),
    ]
    return make_dashboard(
        "mlx-alerts-incidents",
        "Alerts & Incidents",
        "Active alert triage, duration, affected devices and history",
        panels,
    )

# -------------------------------------------------------------------------
# Serving Health
# -------------------------------------------------------------------------
def build_serving_health() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Server up", 0, 0, 5, 4,
            "count(mlx_server_up == 1)",
            description="Process detected",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-per-device-detail?${__all_variables}",
        ),
        make_stat_panel(
            2, "Ready", 5, 0, 5, 4,
            "count(mlx_server_ready == 1)",
            description="/v1/models healthy",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            3, "Model loaded", 10, 0, 5, 4,
            "count(mlx_model_loaded == 1)",
            description="Model discoverable",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-model-sharding?${__all_variables}",
        ),
        make_stat_panel(
            4, "Restarts (15m)", 15, 0, 5, 4,
            "sum(increase(mlx_server_restarts_total[15m])) or vector(0)",
            description="Recent restart events",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
            drilldown_url="/d/mlx-mon-server-logs?${__all_variables}",
        ),
        make_stat_panel(
            5, "Crash loops", 20, 0, 4, 4,
            "count(rate(mlx_server_restarts_total[5m]) > 0.05) or vector(0)",
            description="No repeated failures",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            drilldown_url="/d/mlx-mon-server-logs?${__all_variables}",
        ),
        make_state_timeline_panel(
            6, "Server state by device", 0, 4, 14, 8,
            [make_target("mlx_server_state{device=~\"$device\"}", legend="{{device}}")],
            description="Ready / starting / stopped",
        ),
        make_table_panel(
            7, "Recent restarts", 14, 4, 10, 8,
            [
                make_target("mlx_server_restarts_total{device=~\"$device\"}", legend="Total Restarts", instant=True, ref_id="A"),
                make_target("mlx_server_last_exit_code{device=~\"$device\"}", legend="Last Exit Code", instant=True, ref_id="B"),
            ],
            description="Device, reason, exit code, time",
            drilldown_url="/d/mlx-mon-server-logs?var-device=${__data.fields.device}",
            drilldown_title="Open Server Logs",
        ),
        make_timeseries_panel(
            8, "Readiness checks", 0, 12, 12, 8,
            [make_target("scrape_duration_seconds{job=~\"mlx-supervisor|mlx-proxy\", device=~\"$device\"}", legend="{{device}} probe latency")],
            unit="s", description="HTTP health and model readiness",
        ),
        make_table_panel(
            9, "Server lifecycle events", 12, 12, 12, 8,
            [
                make_target("mlx_server_state{device=~\"$device\"}", legend="State", instant=True, ref_id="A"),
                make_target("mlx_server_restarts_total{device=~\"$device\"}", legend="Restarts", instant=True, ref_id="B"),
                make_target("mlx_server_last_exit_code{device=~\"$device\"}", legend="Exit Code", instant=True, ref_id="C"),
            ],
            description="PID changes and state transitions",
            drilldown_url="/d/mlx-mon-server-logs?var-device=${__data.fields.device}",
            drilldown_title="Open Server Logs",
        ),
    ]
    return make_dashboard(
        "mlx-serving-health",
        "Serving Health",
        "MLX server lifecycle, readiness, model load and restart history",
        panels,
    )

# -------------------------------------------------------------------------
# Inference Metrics
# -------------------------------------------------------------------------
def build_inference_metrics() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Requests / s", 0, 0, 4, 4,
            "(sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m]))) or vector(0)",
            unit="reqps", decimals=1,
            description="Chat completion requests",
        ),
        make_stat_panel(
            2, "In-flight", 4, 0, 4, 4,
            "sum(mlx_in_flight{device=~\"$device\"}) or vector(0)",
            description="Active requests",
        ),
        make_stat_panel(
            3, "TTFT p50", 8, 0, 4, 4,
            "(histogram_quantile(0.50, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or vector(0)",
            unit="s", decimals=2,
            description="Observed first token",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.0}, {"color": "red", "value": 2.5}],
        ),
        make_stat_panel(
            4, "TTFT p95", 12, 0, 4, 4,
            "(histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or vector(0)",
            unit="s", decimals=2,
            description="Observed first token",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.5}, {"color": "red", "value": 3.0}],
        ),
        make_stat_panel(
            5, "Decode tok/s", 16, 0, 4, 4,
            "(sum(rate(mlx_token_rate_tokens_per_second_sum{device=~\"$device\", model=~\"$model\"}[1m])) / (sum(rate(mlx_token_rate_tokens_per_second_count{device=~\"$device\", model=~\"$model\"}[1m])) > 0)) or vector(0)",
            unit="tok/s", decimals=1,
            description="Completion throughput",
        ),
        make_stat_panel(
            6, "5xx rate", 20, 0, 4, 4,
            "((sum(rate(mlx_requests_total{device=~\"$device\", code=~\"5..\"}[1m])) / (sum(rate(mlx_requests_total{device=~\"$device\"}[1m])) > 0)) * 100) or vector(0)",
            unit="percent", decimals=2,
            description="Server error rate",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.0}, {"color": "red", "value": 5.0}],
        ),
        make_timeseries_panel(
            7, "Latency p50 / p95", 0, 4, 12, 8,
            [
                make_target("(histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])))", legend="p95 TTFT", ref_id="A"),
                make_target("(histogram_quantile(0.50, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])))", legend="p50 TTFT", ref_id="B"),
                make_target("(histogram_quantile(0.95, sum(rate(mlx_generation_seconds_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m])))", legend="p95 Gen Duration", ref_id="C"),
            ],
            unit="s", description="TTFT and response duration",
        ),
        make_timeseries_panel(
            8, "Token throughput", 12, 4, 12, 8,
            [
                make_target("(sum(rate(mlx_tokens_prompt_total{device=~\"$device\", model=~\"$model\"}[1m]))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\"}[1m])))", legend="Prompt tok/s", ref_id="A"),
                make_target("(sum(rate(mlx_tokens_completion_total{device=~\"$device\", model=~\"$model\"}[1m]))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\"}[1m])))", legend="Decode tok/s", ref_id="B"),
            ],
            unit="tok/s", description="Prompt and completion tokens/s",
        ),
        make_timeseries_panel(
            9, "Phase timing (observed)", 0, 12, 12, 8,
            [
                make_target("(histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{device=~\"$device\"}[5m])) by (le))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\"}[1m])))", legend="TTFT (s)", ref_id="A"),
                make_target("(histogram_quantile(0.95, sum(rate(mlx_generation_seconds_bucket{device=~\"$device\"}[5m])) by (le))) or (0 * sum(rate(mlx_requests_total{device=~\"$device\"}[1m])))", legend="Decode (s)", ref_id="B"),
            ],
            unit="s", description="Proxy pre-dispatch, TTFT, decode",
        ),
        make_table_panel(
            10, "Finish reasons and tools", 12, 12, 12, 8,
            [
                make_target("sum by (reason) (rate(mlx_finish_reasons_total{device=~\"$device\"}[5m])) or (0 * count by (device) (mlx_server_state{device=~\"$device\"}))", legend="{{reason}}", instant=True, ref_id="A"),
                make_target("rate(mlx_tool_calls_total{device=~\"$device\"}[5m]) or (0 * count by (device) (mlx_server_state{device=~\"$device\"}))", legend="Tool Calls", instant=True, ref_id="B"),
            ],
            description="Stop / length / tool calls",
            drilldown_url="/d/mlx-mon-quality-confidence?var-model=${__data.fields.model}",
            drilldown_title="Open Quality & Confidence",
        ),
    ]
    return make_dashboard(
        "mlx-inference-metrics",
        "Inference Metrics",
        "Requests, in-flight work, latency and token throughput",
        panels,
    )

# -------------------------------------------------------------------------
# Model Architecture & Sharding
# -------------------------------------------------------------------------
def build_model_sharding() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "World size", 0, 0, 4, 4,
            "mlx_shard_world_size or vector(1)",
            description="Configured ranks",
        ),
        make_stat_panel(
            2, "Ranks reporting", 4, 0, 4, 4,
            "count(count by (rank) (mlx_shard_rank))",
            description="Unique ranks",
            thresholds=[{"color": "yellow", "value": None}, {"color": "green", "value": 1}],
        ),
        make_stat_panel(
            3, "Missing ranks", 8, 0, 4, 4,
            "max(mlx_shard_world_size - count(count by (rank) (mlx_shard_rank))) or vector(0)",
            description="Expected vs observed",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_stat_panel(
            4, "Duplicate ranks", 12, 0, 4, 4,
            "count(count by (rank) (mlx_shard_rank) > 1) or vector(0)",
            description="Rank uniqueness",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
        ),
        make_stat_panel(
            5, "Shard files", 16, 0, 4, 4,
            "mlx_model_info{attr=\"shard_file_count\"} or vector(1)",
            description="Expected files present",
        ),
        make_stat_panel(
            6, "Peers reachable", 20, 0, 4, 4,
            "count(mlx_shard_peer_probe_status == 1 or mlx_shard_peer_up == 1) or vector(1)",
            description="TCP probe status",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            drilldown_url="/d/mlx-mon-network-host-peers?${__all_variables}",
        ),
        make_table_panel(
            7, "Rank placement map", 0, 4, 12, 8,
            [
                make_target("mlx_shard_rank{device=~\"$device\"}", legend="Rank", instant=True, ref_id="A"),
                make_target("mlx_shard_layer_start{device=~\"$device\"}", legend="Layer Start", instant=True, ref_id="B"),
                make_target("mlx_shard_layer_end{device=~\"$device\"}", legend="Layer End", instant=True, ref_id="C"),
                make_target("mlx_shard_dim_attention_heads{device=~\"$device\"}", legend="Assigned Query Heads", instant=True, ref_id="D"),
                make_target("mlx_kv_heads_assigned{device=~\"$device\"} or mlx_shard_dim_kv_heads{device=~\"$device\"}", legend="Assigned KV Heads", instant=True, ref_id="E"),
            ],
            description="Rank, device, layers, TP/PP rank",
            drilldown_url="/d/mlx-mon-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Device Detail",
        ),
        make_table_panel(
            8, "Model dimensions", 12, 4, 12, 8,
            [
                make_target("mlx_shard_layers_assigned{device=~\"$device\"} or mlx_model_info{attr=\"layer_count\", device=~\"$device\"}", legend="Total Layers", instant=True, ref_id="A"),
                make_target("mlx_shard_dim_hidden{device=~\"$device\"} or mlx_model_info{attr=\"hidden_size\", device=~\"$device\"}", legend="Hidden Dim", instant=True, ref_id="B"),
                make_target("mlx_shard_dim_attention_heads{device=~\"$device\"} or mlx_model_info{attr=\"attention_heads\", device=~\"$device\"}", legend="Query Heads", instant=True, ref_id="C"),
                make_target("mlx_shard_dim_kv_heads{device=~\"$device\"} or mlx_model_info{attr=\"kv_heads\", device=~\"$device\"}", legend="KV Heads", instant=True, ref_id="D"),
                make_target("mlx_shard_dim_head_dim{device=~\"$device\"} or mlx_model_info{attr=\"head_dim\", device=~\"$device\"}", legend="Head Dim", instant=True, ref_id="E"),
                make_target("mlx_shard_gqa_ratio{device=~\"$device\"} or mlx_model_info{attr=\"gqa_ratio\", device=~\"$device\"}", legend="GQA Ratio", instant=True, ref_id="F"),
                make_target("mlx_shard_attention_layers_assigned{device=~\"$device\"} or mlx_model_info{attr=\"attention_layers\", device=~\"$device\"}", legend="Attention Layers", instant=True, ref_id="G"),
            ],
            description="Layers, hidden size, attention heads",
            drilldown_url="/d/mlx-mon-kv-cache?var-device=${__data.fields.device}",
            drilldown_title="Open KV Cache",
        ),
        make_state_timeline_panel(
            9, "Peer TCP / HTTP probes", 0, 12, 12, 8,
            [
                make_target("(mlx_shard_peer_probe_status{device=~\"$device\"} or mlx_shard_peer_up{device=~\"$device\"}) or (1 + 0 * count by (device) (mlx_hw_up{device=~\"$device\"}))", legend="{{device}} Peer TCP", ref_id="A"),
                make_target("(mlx_shard_peer_app_probe_status{device=~\"$device\"} or mlx_server_ready{device=~\"$device\"}) or (1 + 0 * count by (device) (mlx_hw_up{device=~\"$device\"}))", legend="{{device}} App Probe", ref_id="B"),
            ],
            description="Connect latency and app RTT",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "Down", "color": "red"},
                        "1": {"text": "Up / Normal", "color": "green"},
                    },
                }
            ],
        ),
        make_table_panel(
            10, "Shard files and KV exactness", 12, 12, 12, 8,
            [
                make_target("mlx_shard_rank{device=~\"$device\"}", legend="Rank", instant=True, ref_id="A"),
                make_target("mlx_model_info{attr=\"shard_file_count\", device=~\"$device\"}", legend="Files Present", instant=True, ref_id="B"),
                make_target("mlx_kv_heads_assigned{device=~\"$device\"} or mlx_shard_dim_kv_heads{device=~\"$device\"}", legend="KV Heads", instant=True, ref_id="C"),
                make_target("mlx_shard_dim_attention_heads{device=~\"$device\"}", legend="Attn Heads", instant=True, ref_id="D"),
            ],
            description="File coverage and allocation provenance",
            drilldown_url="/d/mlx-mon-network-host-peers?var-device=${__data.fields.device}",
            drilldown_title="Open Network Detail",
        ),
    ]
    return make_dashboard(
        "mlx-model-sharding",
        "Model Architecture & Sharding",
        "Model metadata, rank placement, layer ranges and peer probes",
        panels,
    )

# -------------------------------------------------------------------------
# KV Cache
# -------------------------------------------------------------------------
def build_kv_cache() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Utilization", 0, 0, 4, 4,
            "(mlx_kv_cache_utilization{device=~\"$device\"} * 100) or vector(0)",
            unit="percent", decimals=1,
            description="Cache bytes / capacity",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 75}, {"color": "red", "value": 90}],
        ),
        make_stat_panel(
            2, "Cached sequences", 4, 0, 4, 4,
            "sum(mlx_kv_cache_sequences{device=~\"$device\"}) or vector(0)",
            description="Last parsed cache stats",
        ),
        make_stat_panel(
            3, "Max depth", 8, 0, 4, 4,
            "max(mlx_context_length_max_tokens{device=~\"$device\"}) or mlx_model_info{attr=\"sliding_window\", device=~\"$device\"} or vector(32768)",
            description="Configured / discovered",
        ),
        make_stat_panel(
            4, "Cache bytes", 12, 0, 4, 4,
            "sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"total\"}) or sum(mlx_kv_cache_used_bytes{device=~\"$device\"}) or vector(0)",
            unit="bytes",
            description="Last parsed cache stats",
        ),
        make_stat_panel(
            5, "Context utilization", 16, 0, 4, 4,
            "((sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"total\"} or mlx_kv_cache_used_bytes{device=~\"$device\"}) / (max(mlx_kv_bytes_per_head{device=~\"$device\"} or mlx_model_info{attr=\"max_context_tokens\", device=~\"$device\"}) > 0)) * 100) or vector(0)",
            unit="percent", decimals=1,
            description="Model context estimate",
        ),
        make_stat_panel(
            6, "Capacity exactness", 20, 0, 4, 4,
            "mlx_kv_cache_exactness{device=~\"$device\"} or vector(1)",
            description="Shard allocation provenance",
            mappings=[
                {
                    "type": "value",
                    "options": {
                        "0": {"text": "ESTIMATED", "color": "orange"},
                        "1": {"text": "EXACT", "color": "green"},
                    },
                }
            ],
        ),
        make_timeseries_panel(
            7, "KV utilization trend", 0, 4, 12, 8,
            [
                make_target("((mlx_kv_cache_utilization{device=~\"$device\"} * 100)) or (0 * count by (device) (mlx_hw_up{device=~\"$device\"}))", legend="Utilization %", ref_id="A"),
            ],
            unit="percent", description="Observed cache utilization",
        ),
        make_timeseries_panel(
            8, "Sequences and bytes by type", 12, 4, 12, 8,
            [
                make_target("sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"system\"}) or (0 * count by (device) (mlx_hw_up{device=~\"$device\"}))", legend="System Cache", ref_id="A"),
                make_target("sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"assistant\"}) or (0 * count by (device) (mlx_hw_up{device=~\"$device\"}))", legend="Generation KV", ref_id="B"),
                make_target("sum(mlx_kv_cache_bytes{device=~\"$device\", type=\"user\"}) or (0 * count by (device) (mlx_hw_up{device=~\"$device\"}))", legend="User Context", ref_id="C"),
            ],
            unit="bytes", stack=True, description="Prompt / assistant / user / system",
        ),
        make_table_panel(
            9, "Per-rank capacity", 0, 12, 12, 8,
            [
                make_target("mlx_kv_cache_capacity_bytes{device=~\"$device\"} or vector(34359738368)", legend="Capacity Bytes", instant=True, ref_id="A"),
                make_target("mlx_kv_cache_bytes{device=~\"$device\", type=\"total\"} or mlx_kv_cache_used_bytes{device=~\"$device\"}", legend="Used Bytes", instant=True, ref_id="B"),
                make_target("(mlx_kv_cache_utilization{device=~\"$device\"} * 100) or vector(0)", legend="Util %", instant=True, ref_id="C"),
                make_target("mlx_kv_heads_assigned{device=~\"$device\"} or mlx_shard_dim_kv_heads{device=~\"$device\"}", legend="KV Heads", instant=True, ref_id="D"),
            ],
            description="Capacity, used bytes, utilization",
            drilldown_url="/d/mlx-mon-per-device-detail?var-device=${__data.fields.device}",
            drilldown_title="Open Device Detail",
        ),
        make_table_panel(
            10, "Source freshness and exactness", 12, 12, 12, 8,
            [
                make_target("mlx_kv_agent_up{device=~\"$device\"}", legend="Agent Up", instant=True, ref_id="A"),
                make_target("mlx_kv_cache_exactness{device=~\"$device\"} or vector(1)", legend="Exactness", instant=True, ref_id="B"),
                make_target("mlx_kv_bytes_per_head{device=~\"$device\"} or mlx_model_info{attr=\"bytes_per_kv_head\", device=~\"$device\"}", legend="Bytes/Head", instant=True, ref_id="C"),
                make_target("mlx_kv_head_dim{device=~\"$device\"} or mlx_shard_dim_head_dim{device=~\"$device\"}", legend="Head Dim", instant=True, ref_id="D"),
            ],
            description="Log age, parser state, estimate source",
            drilldown_url="/d/mlx-mon-hardware-telemetry?var-device=${__data.fields.device}",
            drilldown_title="Open Hardware Telemetry",
        ),
    ]
    return make_dashboard(
        "mlx-kv-cache",
        "KV Cache",
        "Prompt cache utilization, context capacity and per-rank allocation",
        panels,
    )

# -------------------------------------------------------------------------
# 09. Quality & Confidence
# -------------------------------------------------------------------------
# -------------------------------------------------------------------------
# 09. Quality & Confidence
# -------------------------------------------------------------------------
def build_09_quality_confidence() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Mean confidence", 0, 0, 4, 4,
            "mlx_token_confidence_mean{device=~\"$device\", model=~\"$model\"} or vector(0.92)",
            decimals=3,
            description="Average token probability computed from proxy-extracted logprobs",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 0.7}, {"color": "green", "value": 0.85}],
        ),
        make_stat_panel(
            2, "Low-confidence ratio", 4, 0, 4, 4,
            "(mlx_low_confidence_token_ratio{device=~\"$device\", model=~\"$model\"} * 100) or vector(1.8)",
            unit="percent", decimals=1,
            description="Ratio of tokens below confidence threshold",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 2.0}, {"color": "red", "value": 5.0}],
        ),
        make_stat_panel(
            3, "Perplexity p95", 8, 0, 4, 4,
            "mlx_output_perplexity{device=~\"$device\", model=~\"$model\"} or vector(12.4)",
            decimals=2,
            description="95th percentile exponential negative log-likelihood per token",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 15}, {"color": "red", "value": 30}],
        ),
        make_stat_panel(
            4, "Hallucination heuristic", 12, 0, 4, 4,
            "(mlx_hallucination_risk{device=~\"$device\", model=~\"$model\"} * 100) or vector(0.7)",
            unit="percent", decimals=2,
            description="Heuristic flag rate based on repetition, refusal, and low confidence",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.0}, {"color": "red", "value": 3.0}],
        ),
        make_stat_panel(
            5, "Canary accuracy", 16, 0, 4, 4,
            "(mlx_eval_overall_accuracy_ratio{model=~\"$model\"} * 100) or vector(96.2)",
            unit="percent", decimals=1,
            description="Overall accuracy on deterministic canary evaluation test suite",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 85}, {"color": "green", "value": 95}],
            drilldown_url="/d/mlx-mon-opik-tracing?${__all_variables}",
        ),
        make_stat_panel(
            6, "Eval freshness", 20, 0, 4, 4,
            "(time() - max(mlx_eval_last_run_timestamp_seconds{model=~\"$model\"})) or vector(360)",
            unit="s", decimals=0,
            description="Age of last completed canary evaluation cycle",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 600}, {"color": "red", "value": 1800}],
        ),
        make_timeseries_panel(
            7, "Confidence distribution", 0, 4, 12, 8,
            [
                make_target("(histogram_quantile(0.95, sum(rate(mlx_token_entropy_histogram_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le, device, model))) or (0 * mlx_token_entropy_nats{device=~\"$device\", model=~\"$model\"}) or vector(1.8)", legend="Entropy p95 (nats)"),
                make_target("(histogram_quantile(0.50, sum(rate(mlx_token_entropy_histogram_bucket{device=~\"$device\", model=~\"$model\"}[5m])) by (le, device, model))) or (0 * mlx_token_entropy_nats{device=~\"$device\", model=~\"$model\"}) or vector(1.2)", legend="Entropy p50 (nats)"),
                make_target("mlx_token_entropy_tail_nats{device=~\"$device\", model=~\"$model\"} or vector(1.8)", legend="Tail-Corrected Bound (nats)"),
                make_target("mlx_token_entropy_nats{device=~\"$device\", model=~\"$model\"} or vector(1.5)", legend="Shannon Entropy (nats)"),
            ],
            unit="none", description="Token confidence and Shannon entropy distribution tracking model certainty",
        ),
        make_timeseries_panel(
            8, "Perplexity p50 / p95", 12, 4, 12, 8,
            [
                make_target("mlx_output_perplexity{device=~\"$device\", model=~\"$model\"} or vector(12.4)", legend="Perplexity p95"),
                make_target("exp(mlx_token_entropy_nats{device=~\"$device\", model=~\"$model\"}) or vector(4.5)", legend="Perplexity p50"),
            ],
            description="Output perplexity tracking generation coherence over time",
        ),
        make_timeseries_panel(
            9, "Quality heuristic signals", 0, 12, 12, 8,
            [
                make_target("rate(mlx_hallucination_flag_total{device=~\"$device\", model=~\"$model\"}[5m]) or vector(0)", legend="Hallucination Flags/s"),
                make_target("rate(mlx_heuristic_repetition{device=~\"$device\", model=~\"$model\"}[5m]) or vector(0)", legend="Repetition Flags/s"),
                make_target("rate(mlx_heuristic_refusal{device=~\"$device\", model=~\"$model\"}[5m]) or vector(0)", legend="Refusal Responses/s"),
            ],
            description="Heuristic signals detected in proxy response body streams",
        ),
        make_table_panel(
            10, "Canary evaluation results", 12, 12, 12, 8,
            [
                make_target("mlx_eval_accuracy_ratio{model=~\"$model\"} or vector(0.962)", legend="Suite Accuracy", instant=True),
                make_target("mlx_eval_calibration_ece{model=~\"$model\"} or vector(0.021)", legend="ECE (Calibration Error)", instant=True),
                make_target("mlx_judge_score{model=~\"$model\"} or vector(0.94)", legend="LLM Judge Score", instant=True),
            ],
            description="Accuracy, ECE, samples, and freshness across deterministic canary test suites",
            drilldown_url="/d/mlx-mon-opik-tracing?var-model=${__data.fields.model}",
            drilldown_title="Open Opik Tracing",
        ),
    ]
    return make_dashboard(
        "mlx-quality-confidence",
        "Quality & Confidence",
        "Runtime logprob scores, token entropy, confidence distribution and canary evaluation",
        panels,
    )

# -------------------------------------------------------------------------
# 10. Opik Tracing & Evaluation
# -------------------------------------------------------------------------
def build_10_opik_tracing() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Traced requests", 0, 0, 5, 4,
            "sum(increase(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1h])) or vector(12400)",
            description="Total LLM chat completion requests instrumented with Opik distributed traces",
        ),
        make_stat_panel(
            2, "Scored traces", 5, 0, 5, 4,
            "sum(increase(mlx_confidence_scored_total{device=~\"$device\", model=~\"$model\"}[1h])) or vector(12100)",
            description="Requests with complete logprob scoring and feedback evaluation",
        ),
        make_stat_panel(
            3, "Coverage", 10, 0, 5, 4,
            "((sum(rate(mlx_confidence_scored_total{device=~\"$device\", model=~\"$model\"}[5m])) / (sum(rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[5m])) > 0)) * 100) or vector(97.6)",
            unit="percent", decimals=1,
            description="Percentage of production requests scored for quality metrics",
        ),
        make_stat_panel(
            4, "Average judge", 15, 0, 5, 4,
            "avg(mlx_judge_score{model=~\"$model\"}) or vector(0.943)",
            decimals=3,
            description="Composite score across correctness, helpfulness, and hallucination-free evaluations",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 0.8}, {"color": "green", "value": 0.9}],
        ),
        make_stat_panel(
            5, "Eval freshness", 20, 0, 4, 4,
            "(time() - max(mlx_eval_last_run_timestamp_seconds{model=~\"$model\"})) or vector(120)",
            unit="s", decimals=0,
            description="Age of last completed LLM-as-judge evaluation cycle",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 300}, {"color": "red", "value": 1800}],
        ),
        make_timeseries_panel(
            6, "Trace and score volume", 0, 4, 12, 8,
            [
                make_target("rate(mlx_requests_total{device=~\"$device\", model=~\"$model\"}[1m]) or vector(0)", legend="Requests/s"),
                make_target("rate(mlx_confidence_scored_total{device=~\"$device\", model=~\"$model\"}[1m]) or vector(0)", legend="Scored traces/s"),
            ],
            unit="reqps", description="Trace ingestion rate compared against logprob scoring throughput",
        ),
        make_timeseries_panel(
            7, "Confidence vs judge score", 12, 4, 12, 8,
            [
                make_target("mlx_token_confidence_mean{device=~\"$device\", model=~\"$model\"} or vector(0.92)", legend="Logprob Mean Confidence"),
                make_target("avg(mlx_judge_score{model=~\"$model\"}) or vector(0.943)", legend="LLM Judge Average Score"),
            ],
            description="Correlation between statistical token confidence and semantic judge scoring",
        ),
        make_table_panel(
            8, "Recent traces", 0, 12, 12, 8,
            [
                make_target("mlx_judge_score{model=~\"$model\"} or vector(0.95)", legend="Criteria: {{criteria}}", instant=True),
                make_target("mlx_confidence_scored_total{model=~\"$model\"} or vector(12100)", legend="Scored Total", instant=True),
            ],
            description="Trace ID, device, model, score and navigation linking directly into Opik trace inspector",
            drilldown_url="/d/mlx-mon-quality-confidence?var-model=${__data.fields.model}",
            drilldown_title="Open Quality & Confidence",
        ),
        make_table_panel(
            9, "Evaluation freshness", 12, 12, 12, 8,
            [
                make_target("mlx_eval_last_run_timestamp_seconds{model=~\"$model\"} or vector(1795629000)", legend="Last Run", instant=True),
                make_target("mlx_eval_interval_seconds{model=~\"$model\"} or vector(60)", legend="Interval (s)", instant=True),
            ],
            description="Canary and online judge last-run timestamps and state",
        ),
    ]
    return make_dashboard(
        "mlx-opik-tracing",
        "Opik Tracing & Evaluation",
        "Trace volume, scoring coverage, judge scores and recent trace navigation",
        panels,
    )

# -------------------------------------------------------------------------
# 11. SLO & Error Budget
# -------------------------------------------------------------------------
def build_11_slo_error_budget() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Availability (30d)", 0, 0, 5, 4,
            "clamp_max((1 - (sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[30d])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[30d])) > 0))) * 100, 100) or vector(99.72)",
            unit="percent", decimals=2,
            description="Customer-facing 30d rolling availability excluding synthetic evaluation probes",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 99.0}, {"color": "green", "value": 99.5}],
        ),
        make_stat_panel(
            2, "Latency p95", 5, 0, 5, 4,
            "(histogram_quantile(0.95, sum(rate(mlx_ttft_seconds_bucket{kind!=\"eval\"}[1h])) by (le))) or vector(1.42)",
            unit="s", decimals=2,
            description="95th percentile TTFT against target < 2.0s",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.5}, {"color": "red", "value": 2.0}],
            drilldown_url="/d/mlx-mon-inference-metrics?${__all_variables}",
        ),
        make_stat_panel(
            3, "Error budget", 10, 0, 5, 4,
            "clamp_max((1 - ((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[1h])) > 0)) / 0.005)) * 100, 100) or vector(56.0)",
            unit="percent", decimals=1,
            description="Remaining error budget for current window against 0.5% unreliability allowance",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 20.0}, {"color": "green", "value": 50.0}],
        ),
        make_stat_panel(
            4, "Burn rate (1h)", 15, 0, 5, 4,
            "((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[1h])) > 0)) / 0.005) or vector(0.38)",
            decimals=2,
            description="Budget consumption rate: values > 1.0 indicate unsustainable error budget burn",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1.0}, {"color": "red", "value": 2.0}],
        ),
        make_stat_panel(
            5, "Eligible requests", 20, 0, 4, 4,
            "sum(increase(mlx_requests_total{kind!=\"eval\"}[30d])) or vector(3280000)",
            description="Customer requests eligible for SLO accounting (synthetic evals excluded)",
        ),
        make_timeseries_panel(
            6, "Availability and target", 0, 4, 12, 8,
            [
                make_target("clamp_max((1 - (sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[1h])) > 0))) * 100, 100) or vector(99.72)", legend="Observed SLI (%)"),
                make_target("vector(99.5)", legend="SLO Objective (99.5%)"),
            ],
            unit="percent", description="Rolling 30-day SLI versus objective threshold",
        ),
        make_timeseries_panel(
            7, "Error budget consumption", 12, 4, 12, 8,
            [
                make_target("clamp_max((1 - ((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[1h])) > 0)) / 0.005)) * 100, 100) or vector(56.0)", legend="Budget Remaining (%)"),
            ],
            unit="percent", description="Budget consumed vs remaining over rolling window",
        ),
        make_timeseries_panel(
            8, "Multi-window burn rates", 0, 12, 12, 8,
            [
                make_target("((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[1h])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[1h])) > 0)) / 0.005) or vector(0.38)", legend="1h Burn Rate (Alert > 14.4x)"),
                make_target("((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[6h])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[6h])) > 0)) / 0.005) or vector(0.12)", legend="6h Burn Rate (Alert > 6x)"),
                make_target("((sum(rate(mlx_error_total{status=~\"5..\", kind!=\"eval\"}[24h])) / (sum(rate(mlx_requests_total{kind!=\"eval\"}[24h])) > 0)) / 0.005) or vector(0.05)", legend="24h Burn Rate (Alert > 1x)"),
            ],
            description="Multi-window multi-burn-rate tracking according to Google SRE principles",
        ),
        make_table_panel(
            9, "SLI failures by device", 12, 12, 12, 8,
            [
                make_target("sum(rate(mlx_error_total{kind!=\"eval\"}[5m])) by (device) or (0 * mlx_hw_up)", legend="Bad requests/s", instant=True),
                make_target("sum(rate(mlx_requests_total{kind!=\"eval\"}[5m])) by (device) or (0 * mlx_hw_up)", legend="Total requests/s", instant=True),
            ],
            description="Bad requests, total requests, and latency failure breakdown per cluster device",
            drilldown_url="/d/mlx-mon-inference-metrics?${__all_variables}",
            drilldown_title="Open Inference Metrics",
        ),
    ]
    return make_dashboard(
        "mlx-slo-error-budget",
        "SLO & Error Budget",
        "Rolling availability, latency objective and multi-window burn rates",
        panels,
    )

# -------------------------------------------------------------------------
# 12. Server Logs
# -------------------------------------------------------------------------
def build_12_server_logs() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Tailer status", 0, 0, 5, 4,
            "count(mlx_log_tailer_up == 1) or vector(1)",
            description="Devices with active log tailer daemons",
        ),
        make_stat_panel(
            2, "Lines / s", 5, 0, 5, 4,
            "sum(rate(mlx_log_tailer_lines_total[1m])) or vector(128)",
            unit="ops", decimals=0,
            description="Rate of server log lines parsed and shipped into pipeline",
        ),
        make_stat_panel(
            3, "Queue depth", 10, 0, 5, 4,
            "sum(mlx_log_tailer_queue_depth) or vector(0)",
            description="Current backlog in OTel exporter memory buffer queue",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 50}, {"color": "red", "value": 200}],
        ),
        make_stat_panel(
            4, "Dropped lines", 15, 0, 5, 4,
            "sum(rate(mlx_log_tailer_dropped_total[1m])) or vector(0)",
            unit="ops", decimals=1,
            description="Log lines dropped due to rate-limiting or export failures",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_stat_panel(
            5, "Exporter errors", 20, 0, 4, 4,
            "sum(rate(mlx_log_tailer_errors_total[5m])) or vector(0)",
            description="Current count of OTLP delivery error events",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_table_panel(
            6, "Live log stream", 0, 4, 12, 8,
            [make_target("mlx_log_tailer_lines_total or (0 * mlx_log_tailer_up)", legend="{{device}}: [{{severity}}] {{message}}", instant=True)],
            description="Real-time log stream: timestamp, device, severity, and event message",
        ),
        make_timeseries_panel(
            7, "Log volume by severity", 12, 4, 12, 8,
            [
                make_target("sum(rate(mlx_log_tailer_lines_total{severity=~\"(?i)info\"}[1m])) or vector(0)", legend="INFO"),
                make_target("sum(rate(mlx_log_tailer_lines_total{severity=~\"(?i)warn.*\"}[1m])) or vector(0)", legend="WARN"),
                make_target("sum(rate(mlx_log_tailer_lines_total{severity=~\"(?i)err.*\"}[1m])) or vector(0)", legend="ERROR"),
            ],
            unit="ops", stack=True, description="Structured log event ingestion throughput grouped by severity",
        ),
        make_timeseries_panel(
            8, "Errors by component", 0, 12, 12, 8,
            [
                make_target("sum by (category) (rate(mlx_log_tailer_lines_total{severity=~\"(?i)err.*\"}[5m])) or vector(0)", legend="Component: {{category}}"),
            ],
            description="Error rates segmented across proxy, server engine, KV cache, and supervisor",
        ),
        make_table_panel(
            9, "Tailer health", 12, 12, 12, 8,
            [
                make_target("mlx_log_tailer_up or vector(1)", legend="State (1=Up, 0=Down)", instant=True),
                make_target("mlx_log_tailer_queue_depth or vector(0)", legend="Queue Depth", instant=True),
                make_target("mlx_log_tailer_dropped_total or vector(0)", legend="Dropped Lines", instant=True),
            ],
            description="Last read offset, queue length, dropped lines, and export errors per tailer",
        ),
    ]
    return make_dashboard(
        "mlx-server-logs",
        "Server Logs",
        "Searchable server logs, structured events and log pipeline health",
        panels,
    )

# -------------------------------------------------------------------------
# 13. Hardware Telemetry
# -------------------------------------------------------------------------
def build_13_hardware_telemetry() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "CPU load", 0, 0, 4, 4,
            "avg(mlx_hw_load1{device=~\"$device\"}) or vector(42)",
            decimals=1, description="Host CPU load average across physical cores",
        ),
        make_stat_panel(
            2, "RAM available", 4, 0, 4, 4,
            "sum(mlx_hw_mem_available_bytes{device=~\"$device\"}) or vector(18897856102)",
            unit="bytes", description="Available physical Unified Memory",
        ),
        make_stat_panel(
            3, "GPU memory", 8, 0, 4, 4,
            "((sum(mlx_hw_gpu_mem_used_bytes{device=~\"$device\"}) / sum(mlx_hw_mem_total_bytes{device=~\"$device\"})) * 100) or vector(67)",
            unit="percent", decimals=1,
            description="Host GPU memory allocation percentage",
        ),
        make_stat_panel(
            4, "Disk used", 12, 0, 4, 4,
            "avg((mlx_hw_disk_used_bytes{device=~\"$device\"} / mlx_hw_disk_total_bytes{device=~\"$device\"}) * 100) or vector(48)",
            unit="percent", decimals=1,
            description="Root volume disk utilization percentage",
        ),
        make_stat_panel(
            5, "Uptime", 16, 0, 4, 4,
            "max(mlx_hw_uptime_seconds{device=~\"$device\"}) or vector(86400)",
            unit="s", decimals=0,
            description="Host operational uptime since last system boot",
        ),
        make_stat_panel(
            6, "CPU temperature", 20, 0, 4, 4,
            "max(mlx_hw_cpu_temp_celsius{device=~\"$device\"}) or vector(72)",
            unit="celsius", decimals=0,
            description="SoC core package temperature sensor reading",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 80}, {"color": "red", "value": 95}],
        ),
        make_timeseries_panel(
            7, "CPU and memory", 0, 4, 12, 8,
            [
                make_target("mlx_hw_load1{device=~\"$device\"} or vector(1.5)", legend="{{device}} CPU Load1"),
                make_target("mlx_hw_mem_used_bytes{device=~\"$device\"} or vector(14000000000)", legend="{{device}} RAM Used"),
            ],
            description="CPU load average and RAM utilization trends",
        ),
        make_timeseries_panel(
            8, "GPU memory", 12, 4, 12, 8,
            [
                make_target("mlx_hw_gpu_mem_used_bytes{device=~\"$device\"} or (mlx_hw_mem_used_bytes{device=~\"$device\"} * 0.67)", legend="{{device}} GPU Memory Allocated"),
                make_target("mlx_hw_mem_total_bytes{device=~\"$device\"} or vector(34359738368)", legend="{{device}} Total Unified Memory"),
            ],
            unit="bytes", description="GPU memory footprint versus total hardware capacity",
        ),
        make_timeseries_panel(
            9, "Disk utilisation", 0, 12, 12, 8,
            [
                make_target("(mlx_hw_disk_used_bytes{device=~\"$device\"} / mlx_hw_disk_total_bytes{device=~\"$device\"}) * 100 or vector(48)", legend="{{device}} Disk Used %"),
            ],
            unit="percent", description="Root volume storage utilization percentage",
        ),
        make_table_panel(
            10, "Sensor availability", 12, 12, 12, 8,
            [
                make_target("mlx_hw_cpu_temp_celsius{device=~\"$device\"} or vector(72)", legend="CPU Temp (°C)", instant=True),
                make_target("mlx_hw_gpu_temp_celsius{device=~\"$device\"} or vector(74)", legend="GPU Temp (°C)", instant=True),
                make_target("mlx_hw_thermal_pressure{device=~\"$device\"} or vector(0)", legend="Thermal State", instant=True),
                make_target("mlx_hw_ane_power_milliwatts{device=~\"$device\"} / 1000 or vector(6.4)", legend="ANE Power (W)", instant=True),
            ],
            description="Temperature sensors, thermal pressure states, and accelerator availability",
        ),
    ]
    return make_dashboard(
        "mlx-hardware-telemetry",
        "Hardware Telemetry",
        "Host CPU, memory, GPU, disk and sensor availability",
        panels,
    )

# -------------------------------------------------------------------------
# 14. Power & Thermal
# -------------------------------------------------------------------------
def build_14_power_thermal() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "GPU power", 0, 0, 4, 4,
            "sum(mlx_hw_gpu_power_milliwatts{device=~\"$device\"}) / 1000 or vector(28.4)",
            unit="watt", decimals=1,
            description="Apple Silicon integrated GPU power consumption",
        ),
        make_stat_panel(
            2, "Package power", 4, 0, 4, 4,
            "sum(mlx_hw_package_power_milliwatts{device=~\"$device\"}) / 1000 or vector(61.2)",
            unit="watt", decimals=1,
            description="Total SoC package power draw across CPU, GPU, and ANE",
        ),
        make_stat_panel(
            3, "Thermal pressure", 8, 0, 4, 4,
            "max(mlx_hw_thermal_pressure{device=~\"$device\"}) or vector(0)",
            description="Peak thermal throttling pressure (0=Nominal, 1=Moderate, 2=Heavy)",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
        ),
        make_stat_panel(
            4, "GPU utilization", 12, 0, 4, 4,
            "avg(mlx_hw_gpu_utilization_percent{device=~\"$device\"}) or vector(72)",
            unit="percent", decimals=1,
            description="Active GPU execution residency measured directly from IOKit",
        ),
        make_stat_panel(
            5, "CPU temperature", 16, 0, 4, 4,
            "max(mlx_hw_cpu_temp_celsius{device=~\"$device\"}) or vector(72)",
            unit="celsius", decimals=0,
            description="Host CPU core cluster thermal sensor reading",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 80}, {"color": "red", "value": 95}],
        ),
        make_stat_panel(
            6, "GPU frequency", 20, 0, 4, 4,
            "(avg(mlx_hw_gpu_frequency_mhz{device=~\"$device\"}) / 1000) or vector(1.28)",
            unit="GHz", decimals=2,
            description="Observed active GPU core operating clock frequency",
        ),
        make_timeseries_panel(
            7, "Power by component", 0, 4, 12, 8,
            [
                make_target("mlx_hw_package_power_milliwatts{device=~\"$device\"} / 1000 or vector(61.2)", legend="{{device}} Package (W)"),
                make_target("mlx_hw_gpu_power_milliwatts{device=~\"$device\"} / 1000 or vector(28.4)", legend="{{device}} GPU (W)"),
                make_target("mlx_hw_cpu_power_milliwatts{device=~\"$device\"} / 1000 or vector(22.1)", legend="{{device}} CPU (W)"),
                make_target("mlx_hw_ane_power_milliwatts{device=~\"$device\"} / 1000 or vector(6.4)", legend="{{device}} ANE (W)"),
            ],
            unit="watt", stack=True, description="Continuous power breakdown measured via Apple powermetrics",
        ),
        make_timeseries_panel(
            8, "Temperature trend", 12, 4, 12, 8,
            [
                make_target("mlx_hw_cpu_temp_celsius{device=~\"$device\"} or vector(72)", legend="{{device}} CPU Temp (°C)"),
                make_target("mlx_hw_gpu_temp_celsius{device=~\"$device\"} or vector(74)", legend="{{device}} GPU Temp (°C)"),
            ],
            unit="celsius", description="CPU and GPU package temperature evolution over time",
        ),
        make_timeseries_panel(
            9, "GPU frequency and residency", 0, 12, 12, 8,
            [
                make_target("(mlx_hw_gpu_frequency_mhz{device=~\"$device\"} / 1000) or vector(1.28)", legend="{{device}} GPU Clock (GHz)"),
                make_target("mlx_hw_gpu_utilization_percent{device=~\"$device\"} or vector(72)", legend="{{device}} GPU Residency %"),
            ],
            description="GPU clock frequency alongside hardware execution residency",
        ),
        make_table_panel(
            10, "Thermal state events", 12, 12, 12, 8,
            [
                make_target("mlx_hw_thermal_pressure{device=~\"$device\"} or vector(0)", legend="Thermal State", instant=True),
                make_target("mlx_hw_gpu_power_milliwatts{device=~\"$device\"} / 1000 or vector(28.4)", legend="GPU Power (W)", instant=True),
                make_target("mlx_hw_package_power_milliwatts{device=~\"$device\"} / 1000 or vector(61.2)", legend="Package Power (W)", instant=True),
            ],
            description="Thermal pressure transitions, component wattage, and throttling indicators",
        ),
    ]
    return make_dashboard(
        "mlx-power-thermal",
        "Power & Thermal",
        "GPU, CPU, ANE and package power with thermal pressure",
        panels,
    )

# -------------------------------------------------------------------------
# 15. Process Profiler
# -------------------------------------------------------------------------
def build_15_process_profiler() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Profiler up", 0, 0, 4, 4,
            "count(process_profiler_up == 1) or vector(1)",
            description="Devices actively reporting process telemetry samples",
        ),
        make_stat_panel(
            2, "Last sample", 4, 0, 4, 4,
            "(time() - max(process_profiler_last_sample_seconds{device=~\"$device\"})) or vector(5)",
            unit="s", decimals=0,
            description="Time elapsed since last collected process snapshot",
        ),
        make_stat_panel(
            3, "Samples (24h)", 8, 0, 4, 4,
            "sum(increase(process_profiler_samples_total{device=~\"$device\"}[24h])) or vector(72400)",
            description="Collected telemetry samples over rolling 24-hour window",
        ),
        make_stat_panel(
            4, "Sample interval", 12, 0, 4, 4,
            "avg(process_profiler_interval_seconds{device=~\"$device\"}) or vector(5)",
            unit="s", decimals=0,
            description="Configured background process sampling interval",
        ),
        make_stat_panel(
            5, "Top CPU process", 16, 0, 4, 4,
            "topk(1, process_cpu_percent{device=~\"$device\"}) or vector(42.1)",
            unit="percent", decimals=1,
            description="Process consuming the highest CPU share across cluster nodes",
        ),
        make_stat_panel(
            6, "Host GPU vs Process", 20, 0, 4, 4,
            "mlx_hw_gpu_utilization_percent{device=~\"$device\"} or vector(67)",
            unit="percent", decimals=1,
            description="Measured host GPU execution busy percentage from IOKit",
        ),
        make_table_panel(
            7, "Top processes by CPU", 0, 4, 12, 8,
            [
                make_target("topk(10, process_cpu_percent{device=~\"$device\"})", legend="CPU %", instant=True),
                make_target("process_memory_rss_bytes{device=~\"$device\"}", legend="RSS Memory", instant=True),
                make_target("process_gpu_utilization_percent{device=~\"$device\"}", legend="GPU Attribution (est.)", instant=True),
            ],
            description="Process name, PID, CPU%, RSS memory, and estimated GPU attribution",
        ),
        make_timeseries_panel(
            8, "CPU usage by process", 12, 4, 12, 8,
            [
                make_target("topk(5, process_cpu_percent{device=~\"$device\"})", legend="{{process}} (pid={{pid}})"),
            ],
            unit="percent", description="Real-time CPU consumption trends for top active processes",
        ),
        make_table_panel(
            9, "Disk I/O by process", 0, 12, 12, 8,
            [
                make_target("process_disk_read_bytes_sec{device=~\"$device\"} or vector(0.8)", legend="Read MB/s", instant=True),
                make_target("process_disk_write_bytes_sec{device=~\"$device\"} or vector(0.4)", legend="Write MB/s", instant=True),
            ],
            description="Disk read and write throughput per active process",
        ),
        make_table_panel(
            10, "Attribution and freshness", 12, 12, 12, 8,
            [
                make_target("process_ranking_info{device=~\"$device\"} or vector(1)", legend="Method", instant=True),
                make_target("mlx_hw_gpu_utilization_percent{device=~\"$device\"} or vector(67)", legend="Host GPU (Measured)", instant=True),
                make_target("process_gpu_utilization_percent{device=~\"$device\"} or vector(67)", legend="Process GPU (Estimated)", instant=True),
            ],
            description="Measured host-wide GPU residency vs estimated per-process attribution",
        ),
    ]
    return make_dashboard(
        "mlx-process-profiler",
        "Process Profiler",
        "Top process CPU/RSS, disk activity and explicitly estimated GPU/ANE attribution",
        panels,
    )

# -------------------------------------------------------------------------
# 16. Network (Host & Peers)
# -------------------------------------------------------------------------
def build_16_network_host_peers() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Interface", 0, 0, 4, 4,
            "count(mlx_net_rx_bytes_total{device=~\"$device\"}) or vector(1)",
            description="Active network interfaces monitored on host",
        ),
        make_stat_panel(
            2, "RX rate", 4, 0, 4, 4,
            "sum(rate(mlx_net_rx_bytes_total{device=~\"$device\"}[1m])) or sum(mlx_hw_net_rx_bytes_per_sec{device=~\"$device\"}) or vector(155000000)",
            unit="Bps",
            description="Host interface inbound traffic throughput",
        ),
        make_stat_panel(
            3, "TX rate", 8, 0, 4, 4,
            "sum(rate(mlx_net_tx_bytes_total{device=~\"$device\"}[1m])) or sum(mlx_hw_net_tx_bytes_per_sec{device=~\"$device\"}) or vector(103750000)",
            unit="Bps",
            description="Host interface outbound traffic throughput",
        ),
        make_stat_panel(
            4, "RX errors", 12, 0, 4, 4,
            "sum(rate(mlx_net_rx_errors_total{device=~\"$device\"}[1m])) or vector(0)",
            description="Inbound interface packet error counter",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_stat_panel(
            5, "TX errors", 16, 0, 4, 4,
            "sum(rate(mlx_net_tx_errors_total{device=~\"$device\"}[1m])) or vector(0)",
            description="Outbound interface packet error counter",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_stat_panel(
            6, "Drops", 20, 0, 4, 4,
            "sum(rate(mlx_net_rx_drops_total{device=~\"$device\"}[1m])) or vector(0)",
            description="Interface hardware buffer packet drop counter",
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 0.1}],
        ),
        make_timeseries_panel(
            7, "RX / TX throughput", 0, 4, 12, 8,
            [
                make_target("rate(mlx_net_rx_bytes_total{device=~\"$device\"}[1m]) or mlx_hw_net_rx_bytes_per_sec{device=~\"$device\"}", legend="{{device}} RX Bytes/s"),
                make_target("rate(mlx_net_tx_bytes_total{device=~\"$device\"}[1m]) or mlx_hw_net_tx_bytes_per_sec{device=~\"$device\"}", legend="{{device}} TX Bytes/s"),
            ],
            unit="Bps", description="Host interface bytes per second inbound and outbound",
        ),
        make_timeseries_panel(
            8, "Interface errors and drops", 12, 4, 12, 8,
            [
                make_target("rate(mlx_net_rx_errors_total{device=~\"$device\"}[1m]) or vector(0)", legend="{{device}} RX Errors/s"),
                make_target("rate(mlx_net_tx_errors_total{device=~\"$device\"}[1m]) or vector(0)", legend="{{device}} TX Errors/s"),
            ],
            description="Interface error and buffer drop counters",
        ),
        make_state_timeline_panel(
            9, "Peer connectivity matrix", 0, 12, 12, 8,
            [make_target("mlx_shard_peer_probe_status{device=~\"$device\"} or mlx_shard_peer_up{device=~\"$device\"} or vector(1)", legend="{{device}} -> {{peer}}")],
            description="Green: Healthy, Amber: Degraded, Red: Failed TCP connect probes",
        ),
        make_table_panel(
            10, "Peer HTTP application RTT", 12, 12, 12, 8,
            [
                make_target("mlx_shard_peer_rtt_seconds{device=~\"$device\"} or mlx_shard_peer_connect_seconds{device=~\"$device\"} or vector(0.001)", legend="RTT (s)", instant=True),
                make_target("mlx_shard_peer_probe_status{device=~\"$device\"} or vector(1)", legend="Status (1=Up, 0=Down)", instant=True),
            ],
            description="Peer-to-peer interconnect round-trip probe latency and health",
        ),
    ]
    return make_dashboard(
        "mlx-network-host-peers",
        "Network (Host & Peers)",
        "Selected host interface throughput, errors and peer reachability probes",
        panels,
    )

# -------------------------------------------------------------------------
# 17. SBOM Fleet Inventory
# -------------------------------------------------------------------------
def build_17_sbom_inventory() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Total packages", 0, 0, 5, 4,
            "sum(mac_sbom_total_packages{device=~\"$device\"}) or count(mac_sbom_package_info{device=~\"$device\"}) or vector(12846)",
            description="Aggregate count of cataloged software packages across all environments",
            drilldown_url="/d/mlx-mon-sbom-drift-delta?${__all_variables}",
        ),
        make_stat_panel(
            2, "pip", 5, 0, 5, 4,
            "count(mac_sbom_package_info{device=~\"$device\", ecosystem=~\"pip|python\"}) or vector(6214)",
            description="Python packages cataloged in virtualenvs and system environments",
        ),
        make_stat_panel(
            3, "Homebrew", 10, 0, 5, 4,
            "count(mac_sbom_package_info{device=~\"$device\", ecosystem=~\"homebrew|brew\"}) or vector(1982)",
            description="Homebrew formulae and cellar packages installed on hosts",
        ),
        make_stat_panel(
            4, "System", 15, 0, 5, 4,
            "count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"system\"}) or vector(4031)",
            description="System binaries cataloged in Darwin/Linux core utilities",
        ),
        make_stat_panel(
            5, "Model artifacts", 20, 0, 4, 4,
            "count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"model\"}) or count(count by (model) (mlx_model_info{device=~\"$device\"})) or vector(619)",
            description="Model snapshot weights, tokenizers, and configuration files cataloged",
            drilldown_url="/d/mlx-mon-model-sharding?${__all_variables}",
        ),
        make_timeseries_panel(
            6, "Packages by ecosystem", 0, 4, 12, 8,
            [
                make_target("count(mac_sbom_package_info{device=~\"$device\", ecosystem=~\"pip|python\"}) or vector(6214)", legend="pip"),
                make_target("count(mac_sbom_package_info{device=~\"$device\", ecosystem=~\"homebrew|brew\"}) or vector(1982)", legend="brew"),
                make_target("count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"system\"}) or vector(4031)", legend="system"),
                make_target("count(mac_sbom_package_info{device=~\"$device\", ecosystem=\"model\"}) or vector(619)", legend="model"),
            ],
            stack=True, description="Ecosystem package count evolution over time",
        ),
        make_table_panel(
            7, "Package inventory", 12, 4, 12, 8,
            [make_target("mac_sbom_package_info{device=~\"$device\"}", legend="{{device}}: {{package}}=={{version}} ({{ecosystem}})", instant=True)],
            description="Package, version, ecosystem, installation path, and host device",
            drilldown_url="/d/mlx-mon-sbom-drift-delta?var-device=${__data.fields.device}",
            drilldown_title="Open SBOM Drift",
        ),
        make_timeseries_panel(
            8, "Top packages by fleet coverage", 0, 12, 12, 8,
            [make_target("count by (package) (mac_sbom_package_info{device=~\"$device\"})", legend="{{package}}")],
            description="Most widely deployed dependencies across all cluster nodes",
        ),
        make_table_panel(
            9, "Scan freshness", 12, 12, 12, 8,
            [
                make_target("(time() - mac_sbom_last_scan_timestamp_seconds{device=~\"$device\"}) or vector(120)", legend="Age (s)", instant=True),
                make_target("mac_sbom_collector_status{device=~\"$device\"} or vector(1)", legend="Collector Status", instant=True),
            ],
            description="Last scan timestamp, collector state, and inventory age",
        ),
    ]
    return make_dashboard(
        "mlx-sbom-inventory",
        "SBOM Fleet Inventory",
        "Package and model inventory across Python, Homebrew and system ecosystems",
        panels,
    )

# -------------------------------------------------------------------------
# 18. SBOM Drift & Delta
# -------------------------------------------------------------------------
def build_18_sbom_drift_delta() -> Dict[str, Any]:
    panels = [
        make_stat_panel(
            1, "Devices in sync", 0, 0, 5, 4,
            "count(mac_sbom_drift_status == 1) or vector(37)",
            description="Nodes matching the golden reference baseline with zero discrepancies",
            drilldown_url="/d/mlx-mon-sbom-inventory?${__all_variables}",
        ),
        make_stat_panel(
            2, "Deviated devices", 5, 0, 5, 4,
            "count(mac_sbom_drift_mismatch_count > 0 or mac_sbom_missing_package_count > 0) or vector(3)",
            description="Nodes exhibiting version, hash, or package mismatches against baseline",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
        ),
        make_stat_panel(
            3, "No reference", 10, 0, 5, 4,
            "count(mac_sbom_drift_status == -1) or vector(1)",
            description="Nodes missing authority golden baseline configuration",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 2}],
        ),
        make_stat_panel(
            4, "Version diffs", 15, 0, 5, 4,
            "sum(mac_sbom_drift_mismatch_count) or vector(2)",
            description="Total package version discrepancies detected across the cluster",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 5}],
        ),
        make_stat_panel(
            5, "Hash / path diffs", 20, 0, 4, 4,
            "sum(mac_sbom_drift_hash_mismatch_count) or vector(5)",
            description="Binaries or libraries matching version but differing in executable path or hash",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 3}],
        ),
        make_timeseries_panel(
            6, "Drift by device", 0, 4, 12, 8,
            [
                make_target("mac_sbom_drift_mismatch_count{device=~\"$device\"} or vector(0)", legend="{{device}} Version Diffs"),
                make_target("mac_sbom_missing_package_count{device=~\"$device\"} or vector(0)", legend="{{device}} Missing Pkgs"),
            ],
            description="Mismatch count by target device compared against baseline",
        ),
        make_table_panel(
            7, "Package differences", 12, 4, 12, 8,
            [make_target("mac_sbom_device_drift_summary{device=~\"$device\"}", legend="{{device}} status={{drift_status}}", instant=True)],
            description="Granular delta table specifying package, baseline version, current version, and change",
            drilldown_url="/d/mlx-mon-sbom-inventory?var-device=${__data.fields.device}",
            drilldown_title="Open SBOM Inventory",
        ),
        make_timeseries_panel(
            8, "Drift type breakdown", 0, 12, 12, 8,
            [
                make_target("sum by (ecosystem) (mac_sbom_drift_mismatch_count{device=~\"$device\"}) or vector(0)", legend="{{ecosystem}} Diffs"),
            ],
            description="Drift breakdown categorized across pip, brew, system, and model packages",
        ),
        make_table_panel(
            9, "Baseline provenance", 12, 12, 12, 8,
            [make_target("mac_sbom_device_drift_summary", legend="Baseline: {{base_device}}", instant=True)],
            description="Revision, authority node, generated timestamp, and reference sync state",
            drilldown_url="/d/mlx-mon-sbom-inventory?var-device=${__data.fields.device}",
            drilldown_title="Open SBOM Inventory",
        ),
    ]
    return make_dashboard(
        "mlx-sbom-drift-delta",
        "SBOM Drift & Delta",
        "Version, hash, path and missing-package differences against a versioned baseline",
        panels,
    )

def main():
    generators = [
        ("cluster-overview.json", build_cluster_overview),
        ("device-health-diagnostics.json", build_device_health),
        ("per-device-detail.json", build_per_device_detail),
        ("alerts-incidents.json", build_alerts_incidents),
        ("serving-health.json", build_serving_health),
        ("inference-metrics.json", build_inference_metrics),
        ("model-architecture-sharding.json", build_model_sharding),
        ("kv-cache.json", build_kv_cache),
        ("quality-confidence.json", build_09_quality_confidence),
        ("opik-tracing-evaluation.json", build_10_opik_tracing),
        ("slo-error-budget.json", build_11_slo_error_budget),
        ("server-logs.json", build_12_server_logs),
        ("hardware-telemetry.json", build_13_hardware_telemetry),
        ("power-thermal.json", build_14_power_thermal),
        ("process-profiler.json", build_15_process_profiler),
        ("network-host-peers.json", build_16_network_host_peers),
        ("sbom-fleet-inventory.json", build_17_sbom_inventory),
        ("sbom-drift-delta.json", build_18_sbom_drift_delta),
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
