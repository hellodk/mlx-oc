"""Create (or replace) the mlx serving dashboard in the self-hosted Opik instance.

Idempotent: if a dashboard with the target name already exists it is deleted
first, then recreated. Safe to re-run any time the dashboard definition changes.

Usage:
    OPIK_BASE_URL=http://192.168.1.10:32173 python tools/opik_dashboards.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), ".venv", "lib"))

import opik
from opik import id_helpers
from opik.api_objects.dashboard import types

DASHBOARD_NAME = "mlx: serving health & quality"
PROJECT_NAME = "mlx"
BASE_URL = os.environ.get("OPIK_BASE_URL", "http://192.168.1.10:32173/api")


def _wid() -> str:
    return id_helpers.generate_id()


def _stats_card(name: str, metric: str, title: str, x: int, y: int) -> dict:
    return {
        "id": _wid(),
        "name": title,
        "type": types.WidgetType.PROJECT_STATS_CARD.value,
        "config": types.ProjectStatsCardConfig(
            metric=metric,
            metric_name=name,
            stats="sampleCount",
        ).to_jsonable(),
        "x": x,
        "y": y,
        "w": 2,
        "h": 1,
    }


def _time_series(name: str, title: str, x: int, y: int, metric: str,
                 breakdown: types.BreakdownField | None = None) -> dict:
    config = types.ProjectMetricsConfig(
        metric_type=metric,
        metric_name=name,
        chart_type=types.ChartType.LINE.value,
    )
    if breakdown is not None:
        config.breakdown = types.BreakdownConfig(field=breakdown.value)
    return {
        "id": _wid(),
        "name": title,
        "type": types.WidgetType.PROJECT_METRICS.value,
        "config": config.to_jsonable(),
        "x": x,
        "y": y,
        "w": 6,
        "h": 3,
    }


def _markdown(name: str, text: str, y: int) -> dict:
    return {
        "id": _wid(),
        "name": name,
        "type": types.WidgetType.TEXT_MARKDOWN.value,
        "config": types.TextMarkdownConfig(text=text).to_jsonable(),
        "x": 0,
        "y": y,
        "w": 12,
        "h": 1,
    }


def build_sections() -> list[dict]:
    sections = []

    overview_widgets = [
        _markdown(
            "header",
            "**mlx project** — metrics for the MLX serving proxy on the 192.168.1.x "
            "cluster. Traces are stamped by the proxy with model / provider / "
            "temperature / timings; judge feedback scores (correctness, helpfulness, "
            "hallucination_free) are written by the online evaluator.",
            0,
        ),
        _stats_card("total_traces", types.StatsCardMetric.TRACE_COUNT.value, "Total traces", 0, 1),
        _stats_card("latency_p50", types.StatsCardMetric.DURATION_P50.value, "Latency p50", 2, 1),
        _stats_card("latency_p90", types.StatsCardMetric.DURATION_P90.value, "Latency p90", 4, 1),
        _stats_card("latency_p99", types.StatsCardMetric.DURATION_P99.value, "Latency p99", 6, 1),
        _stats_card("errors", types.StatsCardMetric.ERROR_COUNT.value, "Errors", 8, 1),
        _stats_card(
            "tokens",
            types.StatsCardMetric.USAGE_TOTAL_TOKENS.value,
            "Total tokens",
            0,
            2,
        ),
        _stats_card(
            "cost",
            types.StatsCardMetric.TOTAL_ESTIMATED_COST_SUM.value,
            "Est. cost",
            2,
            2,
        ),
        _stats_card("score_correctness", "feedback_scores.correctness", "Correctness", 4, 2),
        _stats_card("score_helpfulness", "feedback_scores.helpfulness", "Helpfulness", 6, 2),
        _stats_card(
            "score_hallucination_free",
            "feedback_scores.hallucination_free",
            "Hallucination-free",
            8,
            2,
        ),
    ]
    overview = types.DashboardSection(
        title="Overview",
        widgets=overview_widgets,
        layout=[
            types.DashboardLayoutItem(
                i=w["id"], x=w["x"], y=w["y"], w=w["w"], h=w["h"]
            )
            for w in overview_widgets
        ],
    )
    sections.append(overview)

    trends_widgets = [
        _time_series("traces", "Trace volume", 0, 0, types.ProjectMetricType.TRACE_COUNT.value),
        _time_series("duration", "Trace duration", 6, 0, types.ProjectMetricType.DURATION.value),
        _time_series("tokens", "Token usage", 0, 3, types.ProjectMetricType.TOKEN_USAGE.value),
        _time_series("cost", "Estimated cost", 6, 3, types.ProjectMetricType.COST.value),
        _time_series(
            "errors",
            "Error rate",
            0,
            6,
            types.ProjectMetricType.TRACE_ERROR_RATE.value,
        ),
        _time_series(
            "spans_by_model",
            "LLM spans by model",
            6,
            6,
            types.ProjectMetricType.SPAN_COUNT.value,
            breakdown=types.BreakdownField.MODEL,
        ),
    ]
    trends = types.DashboardSection(
        title="Trends",
        widgets=trends_widgets,
        layout=[
            types.DashboardLayoutItem(
                i=w["id"], x=w["x"], y=w["y"], w=w["w"], h=w["h"]
            )
            for w in trends_widgets
        ],
    )
    sections.append(trends)
    return sections


def main() -> None:
    client = opik.Opik(host=BASE_URL, api_key="")

    for existing in client.get_dashboards(name=DASHBOARD_NAME):
        client.delete_dashboard(existing.id)
        print(f"[dashboards] deleted existing dashboard {existing.id}")

    created = client.create_dashboard(
        name=DASHBOARD_NAME,
        type=types.DashboardType.MULTI_PROJECT.value,
        description=(
            "Serving health & quality for the mlx project: trace volume, latency, "
            "token usage, cost, errors, and judge feedback scores."
        ),
        project_name=PROJECT_NAME,
        sections=build_sections(),
    )
    print(f"[dashboards] created {created.name!r} -> {created.id}")

    confirm = client.get_dashboard(created.id)
    print(f"[dashboards] verified: id={confirm.id} name={confirm.name!r}")


if __name__ == "__main__":
    main()
