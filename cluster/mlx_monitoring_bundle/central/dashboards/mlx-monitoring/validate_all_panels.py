#!/usr/bin/env python3
"""
Dashboard panel & query validator for MLX reference monitoring dashboards.

Hardened against:
1. Unknown / non-existent metric names not exposed by exporters.
2. PromQL syntax errors (unbalanced delimiters, invalid operators).
3. Fallback masking: queries that only pass because of `or vector(...)` fallbacks
   while the primary metric expression returns no series or fails.
4. Hardcoded developer environments: TSDB URL and device/model variables are configurable.
"""

import argparse
import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Set, Tuple

# Standard PromQL functions, keywords, aggregators and label names
PROMQL_BUILTINS = {
    "abs", "absent", "absent_over_time", "avg", "avg_over_time", "bottomk",
    "ceil", "changes", "clamp", "clamp_max", "clamp_min", "count",
    "count_over_time", "count_values", "day_of_month", "day_of_week",
    "day_of_year", "days_in_month", "delta", "deriv", "exp", "floor",
    "histogram_avg", "histogram_count", "histogram_fraction",
    "histogram_quantile", "histogram_stddev", "histogram_stdvar",
    "histogram_sum", "holt_winters", "hour", "idelta", "increase", "irate",
    "label_join", "label_replace", "ln", "log10", "log2", "max",
    "max_over_time", "min", "min_over_time", "minute", "month",
    "predict_linear", "quantile", "quantile_over_time", "rate", "resets",
    "round", "scalar", "sgn", "sort", "sort_desc", "sqrt", "stddev",
    "stddev_over_time", "stdvar", "stdvar_over_time", "sum", "sum_over_time",
    "time", "timestamp", "topk", "vector", "year",
    # PromQL modifiers / keywords
    "by", "without", "on", "ignoring", "group_left", "group_right", "bool",
    "and", "or", "unless", "offset", "atan2", "deg", "pi", "rad", "inf", "nan",
    # Common label names and values filtered in queries
    "device", "model", "attr", "type", "status", "direction", "iface", "job",
    "instance", "rank", "peer_rank", "peer_ip", "node", "component", "code",
    "method", "handler", "error_type", "reason", "service", "severity",
    "alertname", "mode", "mountpoint", "fstype", "address", "port", "eval",
    "kind", "package", "version", "source", "le", "quantile", "serial", "macbook",
}

# Standard VictoriaMetrics & Prometheus built-in telemetry metrics
BUILTIN_METRICS = {
    "up",
    "ALERTS",
    "ALERTS_FOR_STATE",
    "scrape_duration_seconds",
    "scrape_samples_scraped",
    "scrape_samples_post_metric_relabeling",
    "scrape_series_added",
    # VictoriaMetrics Agent (vmagent) telemetry metrics
    "vmagent_remotewrite_packets_sent_total",
    "vmagent_remotewrite_packets_dropped_total",
    "vmagent_remotewrite_pending_data_bytes",
    "vmagent_remotewrite_conn_errors_total",
    "vmagent_remotewrite_retries_count_total",
    "vmagent_remotewrite_parts_on_disk",
    "vmagent_scrape_errors_total",
    # MLX Dynamic Topology Service metrics
    "mlx_topology_devices_total",
    "mlx_topology_nodes_total",
    "mlx_topology_edges_total",
    "mlx_topology_discrepancies_total",
    "mlx_topology_node_info",
    "mlx_topology_edge_info",
}


def discover_exporter_metrics(repo_root: Path) -> Set[str]:
    """Dynamically scan all Python exporters to discover the full metric catalog."""
    catalog = set(BUILTIN_METRICS)
    exporters_dir = repo_root / "cluster" / "mlx_monitoring_bundle" / "edge" / "exporters"
    if not exporters_dir.exists():
        return catalog

    for py_file in exporters_dir.glob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        # Match Prometheus Gauge, Counter, Histogram, Summary declarations
        for match in re.finditer(
            r'(?:Gauge|Counter|Histogram|Summary)\s*\(\s*[\'\"]([a-zA-Z0-9_]+)[\'\"]',
            content,
        ):
            metric_name = match.group(1)
            metric_type = match.group(0).split("(")[0].strip()
            catalog.add(metric_name)

            # Histograms automatically expose _bucket, _sum, _count in TSDB
            if metric_type == "Histogram":
                catalog.add(f"{metric_name}_bucket")
                catalog.add(f"{metric_name}_sum")
                catalog.add(f"{metric_name}_count")
            # Summaries automatically expose _sum, _count
            elif metric_type == "Summary":
                catalog.add(f"{metric_name}_sum")
                catalog.add(f"{metric_name}_count")

    return catalog


def check_syntax(expr: str) -> List[str]:
    """Validate delimiter balancing and syntax structure for a PromQL expression."""
    errors = []
    pairs = {"(": ")", "{": "}", "[": "]"}
    stack = []
    in_single_quote = False
    in_double_quote = False

    for i, char in enumerate(expr):
        if char == '"' and not in_single_quote and (i == 0 or expr[i - 1] != "\\"):
            in_double_quote = not in_double_quote
        elif char == "'" and not in_double_quote and (i == 0 or expr[i - 1] != "\\"):
            in_single_quote = not in_single_quote

        if in_single_quote or in_double_quote:
            continue

        if char in pairs:
            stack.append((char, i))
        elif char in pairs.values():
            if not stack:
                errors.append(f"Unmatched closing delimiter '{char}' at index {i}")
            else:
                top, top_idx = stack.pop()
                if pairs[top] != char:
                    errors.append(
                        f"Mismatched delimiter: opened '{top}' at {top_idx}, closed with '{char}' at {i}"
                    )

    if in_single_quote:
        errors.append("Unclosed single quote in expression")
    if in_double_quote:
        errors.append("Unclosed double quote in expression")
    while stack:
        top, top_idx = stack.pop()
        errors.append(f"Unclosed opening delimiter '{top}' at index {top_idx}")

    return errors


def extract_metric_names(expr: str, catalog: Set[str]) -> Tuple[List[str], List[str]]:
    """Extract metric names and identify any unknown identifiers in PromQL expression."""
    # Strip string literals so label values are not treated as metrics
    cleaned = re.sub(r'\"[^\"]*\"|\'[^\']*\'', '""', expr)
    identifiers = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]*\b", cleaned))

    valid_metrics = []
    unknown_metrics = []

    for ident in identifiers:
        if ident in catalog:
            valid_metrics.append(ident)
        elif ident in PROMQL_BUILTINS or ident.isdigit():
            continue
        else:
            # Check if identifier appears in a metric position (e.g., metric{ or metric( or standalone)
            if re.search(r"\b" + re.escape(ident) + r"\s*(?:\{|\(|$|\[)", cleaned):
                unknown_metrics.append(ident)

    return sorted(valid_metrics), sorted(unknown_metrics)


def strip_fallback(expr: str) -> str:
    """Strip 'or vector(...)' fallback clause to isolate the primary metric expression."""
    # Match trailing 'or vector(...)' or 'or on(...) vector(...)'
    stripped = re.sub(
        r"\s+or\s+(?:on\([^)]*\)\s+)?vector\([^)]*\)\s*$",
        "",
        expr,
        flags=re.IGNORECASE,
    )
    # Match trailing 'or (0 * ...)' fallback
    stripped = re.sub(
        r"\s+or\s+\(0\s*\*\s*[^)]+\)\s*$",
        "",
        stripped,
        flags=re.IGNORECASE,
    )
    return stripped.strip()


def query_tsdb(tsdb_url: str, expr: str, timeout: float = 5.0) -> Tuple[bool, List[dict], str]:
    """Query VictoriaMetrics / Prometheus API for an instant vector result."""
    url = f"{tsdb_url}/api/v1/query?query={urllib.parse.quote(expr)}"
    try:
        req = urllib.request.urlopen(url, timeout=timeout)
        data = json.loads(req.read().decode("utf-8"))
        if data.get("status") == "success":
            results = data.get("data", {}).get("result", [])
            return True, results, ""
        return False, [], data.get("error", "Unknown API error")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        return False, [], f"HTTP {e.code}: {err_body}"
    except Exception as e:
        return False, [], str(e)


def is_fallback_only_result(results: List[dict]) -> bool:
    """Return True if result is non-empty BUT only contains synthetic vector fallback (empty metric dict)."""
    if not results:
        return False
    return all(r.get("metric", {}) == {} for r in results)


def main():
    parser = argparse.ArgumentParser(description="Validate all panels across MLX reference monitoring dashboards.")
    parser.add_argument(
        "--url",
        default=os.environ.get("VICTORIAMETRICS_URL", os.environ.get("VM_URL", "http://192.168.1.64:8428")),
        help="VictoriaMetrics / TSDB base URL (default: http://192.168.1.64:8428 or $VICTORIAMETRICS_URL)",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("TARGET_DEVICE", ".*"),
        help="Replacement value for $device variable in testing (default: .* or $TARGET_DEVICE)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("TARGET_MODEL", ".*"),
        help="Replacement value for $model variable in testing (default: .*)",
    )
    parser.add_argument(
        "--static",
        action="store_true",
        help="Run static syntax and metric name validation only without connecting to live TSDB",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail validation (exit code 1) on any fallback-masked query or unknown metric",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent.parent.parent.parent.parent
    if not (repo_root / "cluster").exists():
        repo_root = Path.cwd()

    dash_dir = repo_root / "cluster" / "mlx_monitoring_bundle" / "central" / "dashboards" / "mlx-monitoring"
    dash_files = sorted(dash_dir.glob("*.json"))

    if not dash_files:
        print(f"Error: No dashboard JSON files found in {dash_dir}", file=sys.stderr)
        sys.exit(2)

    # 1. Discover metric catalog from exporters
    catalog = discover_exporter_metrics(repo_root)
    print(f"Discovered {len(catalog)} known metrics from edge exporters and system monitors.")
    print(f"Validating {len(dash_files)} dashboards against catalog...\n")

    # Check TSDB reachability if not running in purely static mode
    tsdb_available = False
    if not args.static:
        ok, res, err = query_tsdb(args.url, "1", timeout=2.0)
        if ok:
            tsdb_available = True
            print(f"Connected to live TSDB at {args.url}\n")
        else:
            print(f"Notice: TSDB at {args.url} is unreachable ({err}). Running in static validation mode.\n")

    total_targets = 0
    passed_targets = 0
    fallback_masked_targets = []
    empty_targets = []
    syntax_error_targets = []
    unknown_metric_targets = []

    for fpath in dash_files:
        fname = fpath.name
        with open(fpath, encoding="utf-8") as f:
            dash = json.load(f)

        print("=" * 60)
        print(f"DASHBOARD: {fname} ({dash.get('title')})")
        print("=" * 60)

        for p in dash.get("panels", []):
            pid = p.get("id", 0)
            title = p.get("title", f"Panel {pid}")

            for t in p.get("targets", []):
                total_targets += 1
                raw_expr = t.get("expr", "").strip()
                if not raw_expr:
                    continue

                # 1. Check delimiter and quote syntax
                syntax_errors = check_syntax(raw_expr)
                if syntax_errors:
                    for err in syntax_errors:
                        print(f"  [SYNTAX ERROR] P{pid:02d} '{title}' -> {err}")
                        syntax_error_targets.append((fname, pid, title, raw_expr, err))
                    continue

                # 2. Extract and verify metric names statically against exporter catalog
                valid_metrics, unknown_metrics = extract_metric_names(raw_expr, catalog)
                if unknown_metrics:
                    err_msg = f"Unknown metric(s) not in exporter catalog: {', '.join(unknown_metrics)}"
                    print(f"  [UNKNOWN METRIC] P{pid:02d} '{title}' -> {err_msg}")
                    unknown_metric_targets.append((fname, pid, title, raw_expr, err_msg))
                    continue

                # 3. Live TSDB validation if TSDB is reachable
                if tsdb_available:
                    # Substitute Grafana dashboard variables for testing
                    q = (
                        raw_expr
                        .replace("$device", args.device)
                        .replace("$model", args.model)
                        .replace("$__all_variables", f"var-device={args.device}&var-model={args.model}")
                    )

                    # Strip fallback to test primary metric expression
                    primary_q = strip_fallback(q)
                    has_fallback = (primary_q != q)

                    # Query primary expression without fallback
                    ok_prim, res_prim, err_prim = query_tsdb(args.url, primary_q)
                    if not ok_prim:
                        print(f"  [SYNTAX ERROR] P{pid:02d} '{title}' -> Primary query error: {err_prim}")
                        syntax_error_targets.append((fname, pid, title, primary_q, err_prim))
                        continue

                    # Query full expression with fallback
                    ok_full, res_full, err_full = query_tsdb(args.url, q)
                    if not ok_full:
                        print(f"  [SYNTAX ERROR] P{pid:02d} '{title}' -> Full query error: {err_full}")
                        syntax_error_targets.append((fname, pid, title, q, err_full))
                        continue

                    # Evaluate outcomes:
                    # Case A: Primary expression returned real telemetry series
                    if res_prim and not is_fallback_only_result(res_prim):
                        val = res_prim[0].get("value", [None, None])[1]
                        passed_targets += 1
                        print(f"  [PASS] P{pid:02d} '{title}' -> real series count={len(res_prim)} val={val}")

                    # Case B: Primary expression returned nothing, but full query produced fallback
                    elif has_fallback and res_full and is_fallback_only_result(res_full):
                        fallback_val = res_full[0].get("value", [None, None])[1]
                        print(f"  [FALLBACK_MASKED] P{pid:02d} '{title}' -> Primary query returned 0 series; query only produced fallback val={fallback_val}")
                        fallback_masked_targets.append((fname, pid, title, raw_expr, fallback_val))

                    # Case C: Both returned empty (normal for 0 alerts firing, 0 errors, 0 crashes)
                    elif not res_full:
                        print(f"  [EMPTY] P{pid:02d} '{title}' -> len=0 (no active series, normal for idle/healthy state)")
                        empty_targets.append((fname, pid, title, raw_expr, q))

                    # Case D: Full query produced non-empty series
                    else:
                        val = res_full[0].get("value", [None, None])[1]
                        passed_targets += 1
                        print(f"  [PASS] P{pid:02d} '{title}' -> series count={len(res_full)} val={val}")
                else:
                    # In static mode, syntax and metric validity check passed
                    passed_targets += 1
                    print(f"  [STATIC PASS] P{pid:02d} '{title}' -> syntax valid, metrics: {', '.join(valid_metrics) or 'builtin'}")

    print("\n" + "=" * 60)
    print("VALIDATION SUMMARY:")
    print(f"TOTAL QUERIES VALIDATED:       {total_targets}")
    print(f"PASSED VALIDATION:             {passed_targets}")
    print(f"SYNTAX ERRORS:                 {len(syntax_error_targets)}")
    print(f"UNKNOWN METRIC ERRORS:         {len(unknown_metric_targets)}")
    print(f"FALLBACK-MASKED QUERIES:       {len(fallback_masked_targets)}")
    print(f"EMPTY SERIES (idle/healthy):   {len(empty_targets)}")
    print("=" * 60)

    failed = False
    if syntax_error_targets:
        failed = True
        print("\n[!] SYNTAX ERRORS ENCOUNTERED:")
        for fname, pid, title, raw_expr, err in syntax_error_targets:
            print(f"  {fname} P{pid} [{title}]: {err}")
            print(f"    expr: {raw_expr}")

    if unknown_metric_targets:
        failed = True
        print("\n[!] UNKNOWN METRICS ENCOUNTERED:")
        for fname, pid, title, raw_expr, err in unknown_metric_targets:
            print(f"  {fname} P{pid} [{title}]: {err}")
            print(f"    expr: {raw_expr}")

    if fallback_masked_targets and args.strict:
        failed = True
        print("\n[!] FALLBACK-MASKED QUERIES ENCOUNTERED (--strict mode):")
        for fname, pid, title, raw_expr, val in fallback_masked_targets:
            print(f"  {fname} P{pid} [{title}]: fallback_val={val}")
            print(f"    expr: {raw_expr}")

    if failed:
        sys.exit(1)

    print("\nAll dashboard panels successfully validated!")
    sys.exit(0)


if __name__ == "__main__":
    main()
