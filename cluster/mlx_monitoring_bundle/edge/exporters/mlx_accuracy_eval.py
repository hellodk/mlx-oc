#!/usr/bin/env python3
"""
mlx_accuracy_eval.py — Deterministic canary accuracy, reference perplexity,
calibration error (ECE), and Opik LLM-as-judge Prometheus exporter.

Runs on an edge node alongside mlx_metrics_proxy (:8080) and optionally polls
central Opik traces to score production responses and emit aggregate accuracy
metrics on :9109 without polluting production chat metrics (uses X-Mlx-Eval: 1).
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest

from mlx_logging import debug, degraded, error, info, parse_listen_addr, setup_logging

LISTEN_DEFAULT = "127.0.0.1:9109"
DEFAULT_EVAL_INTERVAL = 1800.0
DEFAULT_JUDGE_INTERVAL = 60.0
JUDGE_SCORE_NAMES = ("correctness", "helpfulness", "hallucination_free")

JUDGE_PROMPT = (
    "You are an evaluator. Score the assistant's answer to the user's question "
    "using three numbers between 0 and 1.\n\n"
    "Question: {question}\n\n"
    "Assistant answer: {answer}\n\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    '- "correctness": how factually correct and complete the answer is\n'
    '- "helpfulness": how well the answer addresses the request\n'
    '- "hallucination_free": 1.0 if fully grounded and fabricates nothing, '
    "0.0 if it fabricates\n"
    'Example: {"correctness": 0.9, "helpfulness": 0.8, "hallucination_free": 1.0}'
)

G_UP = Gauge("mlx_accuracy_eval_up", "1 if the accuracy evaluator exporter is running")
G_EVAL_SUITE_ACC = Gauge(
    "mlx_eval_accuracy_ratio",
    "Canary benchmark pass ratio per suite (0..1)",
    ["model", "suite"],
)
G_EVAL_OVERALL_ACC = Gauge(
    "mlx_eval_overall_accuracy_ratio",
    "Overall deterministic canary pass ratio (0..1)",
    ["model"],
)
C_EVAL_PASSED = Counter(
    "mlx_eval_samples_passed_total",
    "Total deterministic canary eval samples passed",
    ["model", "suite"],
)
C_EVAL_TOTAL = Counter(
    "mlx_eval_samples_total",
    "Total deterministic canary eval samples executed",
    ["model", "suite"],
)
G_EVAL_PPL = Gauge(
    "mlx_eval_perplexity",
    "Deterministic temperature=0 reference perplexity on canary suite",
    ["model"],
)
G_EVAL_CONF = Gauge(
    "mlx_eval_confidence_mean",
    "Mean token confidence on deterministic canary suite (0..1)",
    ["model"],
)
G_EVAL_ECE = Gauge(
    "mlx_eval_calibration_ece",
    "Expected Calibration Error across confidence bins on canary suite (0..1)",
    ["model"],
)
G_EVAL_LAST_RUN = Gauge(
    "mlx_eval_last_run_timestamp_seconds",
    "Unix timestamp of the last completed canary evaluation run",
    ["model"],
)
C_EVAL_ERRORS = Counter(
    "mlx_eval_errors_total",
    "Evaluation and judge execution errors",
    ["model", "reason"],
)

G_JUDGE_SCORE = Gauge(
    "mlx_judge_score",
    "Rolling mean LLM-as-judge score over recent Opik traces (0..1)",
    ["model", "criterion"],
)
C_JUDGE_LOW = Counter(
    "mlx_judge_low_score_total",
    "Judge-evaluated traces scoring below 0.5",
    ["model", "criterion"],
)
C_JUDGE_EVALUATED = Counter(
    "mlx_judge_traces_evaluated_total",
    "Total Opik traces scored by the online judge",
    ["model"],
)


def _ssl_ctx(verify_tls: bool) -> ssl.SSLContext | None:
    if verify_tls:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def load_eval_suite(path: Path) -> list[dict[str, Any]]:
    """Load JSONL deterministic evaluation items."""
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("prompt") and obj.get("suite"):
            items.append(obj)
    return items


def check_eval_sample(item: dict[str, Any], message: dict[str, Any]) -> bool:
    """Verify deterministic expectation for a single canary response message."""
    check = str(item.get("check") or "contains")
    content = str(message.get("content") or "").strip()

    if check == "contains":
        return str(item.get("expected", "")) in content
    if check == "contains_ci":
        return str(item.get("expected", "")).lower() in content.lower()
    if check == "json_keys":
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.S).strip()
        m = re.search(r"\{.*\}", cleaned, re.S)
        if not m:
            return False
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return False
        if not isinstance(parsed, dict):
            return False
        for key in item.get("required_keys") or []:
            if key not in parsed:
                return False
        for key, exp_val in (item.get("expected_values") or {}).items():
            if parsed.get(key) != exp_val:
                return False
        return True
    if check == "python_ast":
        code = re.sub(r"^```(?:python)?\s*|\s*```$", "", content, flags=re.S).strip()
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return False
        expected_fn = str(item.get("expected") or "")
        if not expected_fn:
            return True
        return any(
            isinstance(node, ast.FunctionDef) and node.name == expected_fn
            for node in ast.walk(tree)
        )
    if check == "tool_call":
        calls = message.get("tool_calls") or []
        expected_tool = str(item.get("expected_tool") or "")
        for tc in calls if isinstance(calls, list) else []:
            if isinstance(tc, dict):
                fn = (tc.get("function") or {}).get("name")
                if not expected_tool or fn == expected_tool:
                    return True
        return False
    return False


def extract_sample_logprob_stats(choice: dict[str, Any]) -> tuple[float | None, float | None]:
    """Return (mean_confidence, perplexity) from a choice logprobs dict."""
    lp = choice.get("logprobs")
    if not isinstance(lp, dict):
        return None, None
    entries = lp.get("content")
    if not isinstance(entries, list) or not entries:
        return None, None
    probs: list[float] = []
    for e in entries:
        if isinstance(e, dict) and "logprob" in e:
            try:
                probs.append(math.exp(float(e["logprob"])))
            except (TypeError, ValueError, OverflowError):
                continue
    if not probs:
        return None, None
    mean_conf = sum(probs) / len(probs)
    mean_logp = sum(math.log(max(p, 1e-12)) for p in probs) / len(probs)
    try:
        ppl = math.exp(-mean_logp)
    except OverflowError:
        ppl = 0.0
    return mean_conf, (ppl if math.isfinite(ppl) else None)


def compute_ece(samples: list[tuple[float, bool]], bins: int = 5) -> float:
    """Compute binned Expected Calibration Error over (confidence, correct)."""
    if not samples:
        return 0.0
    n = len(samples)
    ece = 0.0
    for b in range(bins):
        lo = b / bins
        hi = (b + 1) / bins
        bucket = [
            (c, ok)
            for (c, ok) in samples
            if (lo <= c < hi) or (b == bins - 1 and c == 1.0)
        ]
        if not bucket:
            continue
        avg_conf = sum(c for c, _ in bucket) / len(bucket)
        avg_acc = sum(1.0 if ok else 0.0 for _, ok in bucket) / len(bucket)
        ece += (len(bucket) / n) * abs(avg_acc - avg_conf)
    return round(ece, 4)


def parse_judge_scores(text: str) -> dict[str, float] | None:
    """Extract bounded 0..1 judge scores from LLM reply text."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    scores: dict[str, float] = {}
    for name in JUDGE_SCORE_NAMES:
        raw = data.get(name)
        if raw is None:
            return None
        try:
            scores[name] = max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return None
    return scores


class AccuracyEvaluator:
    """Periodic canary eval runner and Opik online judge bridge."""

    def __init__(
        self,
        args: argparse.Namespace,
        metrics_lock: threading.Lock | None = None,
    ) -> None:
        self.args = args
        self.metrics_lock = metrics_lock or threading.Lock()
        self.stop_event = threading.Event()
        self.suite_items = load_eval_suite(Path(args.suite_file))
        self._rolling_judge: dict[str, list[float]] = {k: [] for k in JUDGE_SCORE_NAMES}
        self._rolling_limit = max(5, int(getattr(args, "judge_window", 50)))
        self._opik_warned = False

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        hdrs = {"Content-Type": "application/json"}
        if self.args.api_key:
            hdrs["Authorization"] = f"Bearer {self.args.api_key}"
        if extra:
            hdrs.update(extra)
        return hdrs

    def _discover_model(self) -> str:
        if self.args.model:
            return self.args.model
        url = self.args.proxy_url.rstrip("/") + "/v1/models"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(
                req, timeout=5, context=_ssl_ctx(self.args.verify_tls)
            ) as r:
                data = json.loads(r.read().decode("utf8", "replace"))
            for item in reversed(data.get("data") or []):
                if isinstance(item, dict) and item.get("id") and item["id"] != "default_model":
                    return str(item["id"])
        except Exception as exc:
            debug(f"accuracy eval model discovery failed: {exc!r}", component="eval")
        return "unknown"

    def _post_chat(
        self,
        url: str,
        payload: dict[str, Any],
        extra_headers: dict[str, str],
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf8"),
            headers=self._headers(extra_headers),
            method="POST",
        )
        with urllib.request.urlopen(
            req, timeout=timeout, context=_ssl_ctx(self.args.verify_tls)
        ) as r:
            return json.loads(r.read().decode("utf8", "replace"))

    def _upstream_ready(self) -> bool:
        """Check whether proxy /v1/models responds HTTP 200 before firing eval calls."""
        url = self.args.proxy_url.rstrip("/") + "/v1/models"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(
                req, timeout=3, context=_ssl_ctx(self.args.verify_tls)
            ) as r:
                return r.status == 200
        except Exception as exc:
            debug(f"canary/judge skipped (upstream not ready): {exc!r}", component="eval")
            return False

    def run_canary_once(self) -> bool:
        """Execute one deterministic canary evaluation sweep."""
        if not self.args.enable_canary or not self.suite_items:
            return False
        if not self._upstream_ready():
            return False
        model = self._discover_model()
        endpoint = self.args.proxy_url.rstrip("/") + "/v1/chat/completions"

        suite_counts: dict[str, list[int]] = {}
        cal_samples: list[tuple[float, bool]] = []
        ppls: list[float] = []
        confs: list[float] = []

        for item in self.suite_items:
            suite = str(item["suite"])
            payload: dict[str, Any] = {
                "model": model,
                "temperature": 0.0,
                "max_tokens": int(item.get("max_tokens") or 128),
                "logprobs": True,
                "top_logprobs": 3,
                "stream": False,
                "messages": [{"role": "user", "content": str(item["prompt"])}],
            }
            if item.get("tools"):
                payload["tools"] = item["tools"]
            try:
                resp = self._post_chat(
                    endpoint,
                    payload,
                    {"X-Mlx-Eval": "1", "X-Mlx-Trace": "0"},
                )
            except Exception as exc:
                C_EVAL_ERRORS.labels(model=model, reason="canary_request").inc()
                debug(f"canary request failed ({item.get('id')}): {exc!r}", component="eval")
                continue

            choices = resp.get("choices") or [{}]
            choice = choices[0] if isinstance(choices[0], dict) else {}
            msg = choice.get("message") or {}
            passed = check_eval_sample(item, msg if isinstance(msg, dict) else {})
            counts = suite_counts.setdefault(suite, [0, 0])
            counts[1] += 1
            if passed:
                counts[0] += 1
            mean_c, ppl = extract_sample_logprob_stats(choice)
            if mean_c is not None:
                confs.append(mean_c)
                cal_samples.append((mean_c, passed))
            if ppl is not None:
                ppls.append(ppl)

        total_executed = sum(v[1] for v in suite_counts.values())
        if total_executed == 0:
            return False
        total_passed = sum(v[0] for v in suite_counts.values())
        overall_acc = total_passed / total_executed
        ece = compute_ece(cal_samples)

        with self.metrics_lock:
            for suite, (passed_n, total_n) in suite_counts.items():
                G_EVAL_SUITE_ACC.labels(model=model, suite=suite).set(passed_n / total_n)
                C_EVAL_PASSED.labels(model=model, suite=suite).inc(passed_n)
                C_EVAL_TOTAL.labels(model=model, suite=suite).inc(total_n)
            G_EVAL_OVERALL_ACC.labels(model=model).set(overall_acc)
            G_EVAL_ECE.labels(model=model).set(ece)
            if ppls:
                G_EVAL_PPL.labels(model=model).set(sum(ppls) / len(ppls))
            if confs:
                G_EVAL_CONF.labels(model=model).set(sum(confs) / len(confs))
            G_EVAL_LAST_RUN.labels(model=model).set(time.time())

        info(
            f"canary eval complete model={model} acc={overall_acc:.3f} "
            f"ece={ece:.3f} passed={total_passed}/{total_executed}",
            component="eval",
        )
        return True

    def run_judge_once(self) -> int:
        """Score pending Opik traces and publish aggregate judge metrics."""
        if not self.args.enable_judge or not self.args.opik_base:
            return 0
        model = self._discover_model()
        api = self.args.opik_base.rstrip("/") + "/api/v1/private"
        url = (
            f"{api}/traces?project_name={urllib.parse.quote(self.args.opik_project)}"
            f"&page=1&size={self.args.judge_limit}"
        )
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url),
                timeout=10,
                context=_ssl_ctx(self.args.verify_tls),
            ) as r:
                traces = (json.loads(r.read().decode("utf8", "replace")) or {}).get(
                    "content", []
                )
            self._opik_warned = False
        except Exception as exc:
            if not self._opik_warned:
                degraded(f"opik trace poll unavailable: {exc!r}", component="eval")
                self._opik_warned = True
            return 0

        pending = []
        for t in traces if isinstance(traces, list) else []:
            if not isinstance(t, dict):
                continue
            fs = t.get("feedback_scores") or []
            names = {s.get("name") for s in fs if isinstance(s, dict)}
            if all(n in names for n in JUDGE_SCORE_NAMES):
                continue
            inp = t.get("input") if isinstance(t.get("input"), dict) else {}
            out = t.get("output") if isinstance(t.get("output"), dict) else {}
            msgs = inp.get("messages") if isinstance(inp.get("messages"), list) else []
            question = ""
            for m in reversed(msgs):
                if isinstance(m, dict) and m.get("role") == "user" and m.get("content"):
                    question = str(m["content"]).strip()
                    break
            answer = str(out.get("content") or "").strip()
            if question and answer and t.get("id"):
                pending.append((t["id"], question, answer))

        if not pending:
            return 0
        if not self._upstream_ready():
            return 0

        judge_url = self.args.judge_url.rstrip("/") + "/chat/completions"
        batch_scores: list[dict[str, Any]] = []
        evaluated = 0
        for tid, question, answer in pending:
            prompt = (
                JUDGE_PROMPT.replace("{question}", question[:2000]).replace(
                    "{answer}", answer[:2000]
                )
            )
            try:
                resp = self._post_chat(
                    judge_url,
                    {
                        "model": self.args.judge_model or model,
                        "temperature": 0.0,
                        "max_tokens": 128,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    {"X-Mlx-Trace": "0", "X-Mlx-Eval": "1"},
                    timeout=90.0,
                )
            except Exception as exc:
                C_EVAL_ERRORS.labels(model=model, reason="judge_call").inc()
                debug(f"judge call failed for trace={tid}: {exc!r}", component="eval")
                continue
            reply = ((resp.get("choices") or [{}])[0].get("message") or {}).get(
                "content", ""
            )
            parsed = parse_judge_scores(str(reply))
            if not parsed:
                C_EVAL_ERRORS.labels(model=model, reason="judge_parse").inc()
                continue
            evaluated += 1
            for criterion, val in parsed.items():
                batch_scores.append({
                    "id": tid,
                    "name": criterion,
                    "value": val,
                    "category_name": "judge",
                    "reason": "Automated LLM-as-judge via mlx_accuracy_eval",
                    "project_name": self.args.opik_project,
                    "source": "sdk",
                })
                hist = self._rolling_judge[criterion]
                hist.append(val)
                if len(hist) > self._rolling_limit:
                    del hist[: len(hist) - self._rolling_limit]
                if val < 0.5:
                    C_JUDGE_LOW.labels(model=model, criterion=criterion).inc()

        if batch_scores:
            try:
                put_req = urllib.request.Request(
                    f"{api}/traces/feedback-scores",
                    data=json.dumps({"scores": batch_scores}).encode("utf8"),
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                urllib.request.urlopen(
                    put_req, timeout=10, context=_ssl_ctx(self.args.verify_tls)
                ).close()
            except Exception as exc:
                C_EVAL_ERRORS.labels(model=model, reason="opik_write").inc()
                debug(f"opik feedback write failed: {exc!r}", component="eval")

        if evaluated:
            with self.metrics_lock:
                C_JUDGE_EVALUATED.labels(model=model).inc(evaluated)
                for criterion, vals in self._rolling_judge.items():
                    if vals:
                        G_JUDGE_SCORE.labels(model=model, criterion=criterion).set(
                            sum(vals) / len(vals)
                        )
        return evaluated

    def run(self) -> None:
        G_UP.set(1)
        last_canary = 0.0
        last_judge = 0.0
        tick = min(10.0, max(1.0, min(self.args.eval_interval, self.args.judge_interval)))
        while not self.stop_event.is_set():
            now = time.monotonic()
            if self.args.enable_canary and (now - last_canary) >= self.args.eval_interval:
                try:
                    self.run_canary_once()
                except Exception as exc:
                    error(f"canary loop error: {exc!r}", component="eval")
                last_canary = time.monotonic()
            if self.args.enable_judge and (now - last_judge) >= self.args.judge_interval:
                try:
                    self.run_judge_once()
                except Exception as exc:
                    error(f"judge loop error: {exc!r}", component="eval")
                last_judge = time.monotonic()
            self.stop_event.wait(tick)

    def stop(self) -> None:
        self.stop_event.set()


class MetricsHandler(BaseHTTPRequestHandler):
    metrics_lock: threading.Lock = threading.Lock()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            with self.metrics_lock:
                body = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/health", "/healthz"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        pass


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    setup_logging("mlx-eval", os.environ.get("MLX_MACBOOK_ID", "unknown"))
    proxy_listen = os.environ.get("PROXY_LISTEN", "127.0.0.1:8080")
    proxy_port = proxy_listen.rsplit(":", 1)[-1] if ":" in proxy_listen else "8080"
    default_proxy_url = f"http://127.0.0.1:{proxy_port}"
    default_suite = str(Path(__file__).resolve().parents[1] / "config" / "eval_suite.jsonl")

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--listen", default=os.environ.get("EVAL_LISTEN", LISTEN_DEFAULT))
    p.add_argument(
        "--proxy-url",
        default=os.environ.get("EVAL_PROXY_URL", default_proxy_url),
    )
    p.add_argument("--model", default=os.environ.get("MLX_MODEL", ""))
    p.add_argument(
        "--suite-file",
        default=os.environ.get("EVAL_SUITE_FILE", default_suite),
    )
    p.add_argument(
        "--eval-interval",
        type=float,
        default=float(os.environ.get("EVAL_INTERVAL", str(DEFAULT_EVAL_INTERVAL))),
    )
    p.add_argument(
        "--enable-canary",
        action="store_true",
        default=os.environ.get("ENABLE_CANARY_EVAL", "true").lower() == "true",
    )
    p.add_argument(
        "--no-canary",
        dest="enable_canary",
        action="store_false",
    )
    p.add_argument(
        "--enable-judge",
        action="store_true",
        default=os.environ.get("ENABLE_OPIK_JUDGE", "true").lower() == "true",
    )
    p.add_argument(
        "--no-judge",
        dest="enable_judge",
        action="store_false",
    )
    p.add_argument(
        "--opik-base",
        default=os.environ.get("OPIK_ENDPOINT", ""),
    )
    p.add_argument("--opik-project", default=os.environ.get("OPIK_PROJECT", "mlx"))
    p.add_argument(
        "--judge-url",
        default=os.environ.get("JUDGE_URL", f"{default_proxy_url}/v1"),
    )
    p.add_argument("--judge-model", default=os.environ.get("JUDGE_MODEL", ""))
    p.add_argument(
        "--judge-interval",
        type=float,
        default=float(os.environ.get("JUDGE_INTERVAL", str(DEFAULT_JUDGE_INTERVAL))),
    )
    p.add_argument(
        "--judge-limit",
        type=int,
        default=int(os.environ.get("JUDGE_LIMIT", "10")),
    )
    p.add_argument("--judge-window", type=int, default=50)
    p.add_argument("--api-key", default=os.environ.get("MLX_API_KEY", ""))
    p.add_argument(
        "--verify-tls",
        action="store_true",
        default=os.environ.get("OTLP_VERIFY_TLS", "false").lower() == "true",
    )
    args = p.parse_args()

    try:
        host, port = parse_listen_addr(args.listen, default_host="127.0.0.1", default_port=9109)
    except ValueError as exc:
        error(f"invalid listen address {args.listen!r}: {exc}", component="eval")
        sys.exit(2)

    lock = threading.Lock()
    MetricsHandler.metrics_lock = lock
    evaluator = AccuracyEvaluator(args, metrics_lock=lock)
    _ReusableHTTPServer.address_family = socket.AF_INET6 if ":" in host else socket.AF_INET
    httpd = _ReusableHTTPServer((host, port), MetricsHandler)

    once_stop = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        if once_stop.is_set():
            return
        once_stop.set()
        info(f"received signal {signum}, stopping", component="eval")
        evaluator.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    t = threading.Thread(target=evaluator.run, daemon=True)
    t.start()
    info(f"serving accuracy metrics on http://{host}:{port}/metrics", component="eval")
    try:
        httpd.serve_forever()
    finally:
        evaluator.stop()
        t.join(timeout=5.0)
        httpd.server_close()


if __name__ == "__main__":
    main()
