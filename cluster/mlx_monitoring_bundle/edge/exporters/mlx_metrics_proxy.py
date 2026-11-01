#!/usr/bin/env python3
"""
mlx_metrics_proxy.py
====================
Reverse proxy + Prometheus exporter in front of mlx_lm.server.

The proxy is the front door that clients talk to. It forwards every request
to the real mlx_lm server and records per-request metrics (TTFT, token rate,
temperature, tool calls, hallucination risk, confidence). Metrics are served
in Prometheus text format on /metrics.

Optional OpenTelemetry traces/logs are exported to an OTLP gateway.
Optional Opik tracing logs each chat completion as a trace.

All configuration comes from environment variables and config/nodes.yml -
nothing is hardcoded.

MLX_MODEL is optional. If unset, the model name is discovered from the
upstream mlx_lm server GET /v1/models endpoint. If discovery fails,
model-specific gauges are skipped with a degraded log entry.
"""

import argparse
import json
import logging
import math
import os
import random
import re
import socket
import sys
import threading
import time
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

from mlx_logging import degraded, error, info, setup_logging

try:
    from mlx_model_info import model_info as _model_info
except Exception:
    _model_info = None

_OTEL = None
_OPIK = None
_OPIK_OTLP = False
_NODE_NAME = ""
_MODEL_NAME = None  # resolved at startup: config > upstream discovery > None

_CTX_MAX_TOKENS = 0

_EPS = 1e-12


def _sig_url(endpoint: str, sig: str) -> str:
    url = endpoint.rstrip("/")
    return url if url.endswith(f"/v1/{sig}") else f"{url}/v1/{sig}"


def _setup_otel(endpoint: str, node_name: str, service_name: str = "mlx-metrics-proxy",
                opik_otlp_endpoint: str = ""):
    if not endpoint and not opik_otlp_endpoint:
        return None
    try:
        from opentelemetry import trace, metrics, _logs
        from opentelemetry.sdk.resources import (
            Resource, SERVICE_NAME, HOST_NAME, SERVICE_INSTANCE_ID,
        )
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )

        host = socket.gethostname()
        resource = Resource.create({
            SERVICE_NAME: service_name,
            HOST_NAME: host,
            SERVICE_INSTANCE_ID: f"{node_name or host}:proxy",
            "mlx.node.name": node_name or host,
        })

        tp = TracerProvider(resource=resource)
        if endpoint:
            tp.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=_sig_url(endpoint, "traces")))
            )
        if opik_otlp_endpoint:
            tp.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=_sig_url(opik_otlp_endpoint, "traces"),
                        headers={"projectName": "mlx"},
                    )
                )
            )
        trace.set_tracer_provider(tp)

        if endpoint:
            lp = LoggerProvider(resource=resource)
            lp.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(endpoint=_sig_url(endpoint, "logs"))
                )
            )
            _logs.set_logger_provider(lp)
            logging_handler = LoggingHandler(level=logging.INFO, logger_provider=lp)
            root = logging.getLogger()
            root.setLevel(logging.INFO)
            root.addHandler(logging_handler)

        class Otel:
            tracer = tp.get_tracer("mlx.proxy")

            @staticmethod
            def span(name, **attrs):
                return Otel.tracer.start_as_current_span(name, attributes=attrs)

        return Otel
    except Exception as exc:
        error(f"OTel setup failed ({exc!r}); continuing without OTel",
              component="proxy")
        return None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
REQUESTS = Counter(
    "mlx_requests_total",
    "Chat completion requests served",
    ["model", "streaming", "kind"],
)
TOKENS_PROMPT = Counter(
    "mlx_tokens_prompt_total",
    "Prompt tokens processed",
    ["model", "kind"],
)
TOKENS_COMPLETION = Counter(
    "mlx_tokens_completion_total",
    "Completion tokens generated",
    ["model", "kind"],
)
TTFT = Histogram(
    "mlx_ttft_seconds",
    "Time to first token",
    ["model"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
GEN_TIME = Histogram(
    "mlx_generation_seconds",
    "Total generation time per request",
    ["model"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)
TOKEN_RATE = Histogram(
    "mlx_token_rate_tokens_per_second",
    "Completion tokens per second",
    ["model"],
    buckets=(1, 2, 5, 10, 20, 40, 80, 160, 320, 640),
)
TOOL_CALLS = Counter(
    "mlx_tool_calls_total",
    "Tool calls requested by the model",
    ["model", "kind", "tool", "type"],
)
FINISH_REASON = Counter(
    "mlx_finish_reason_total",
    "Completions by finish_reason",
    ["model", "reason", "kind"],
)
TEMPERATURE = Gauge(
    "mlx_temperature",
    "Sampling temperature used for the most recent request",
    ["model", "kind"],
)
RISK = Gauge(
    "mlx_hallucination_risk",
    "Composite heuristic hallucination risk (0..1) for the most recent request",
    ["model", "kind"],
)
RISK_HIST = Histogram(
    "mlx_hallucination_risk_histogram",
    "Distribution of hallucination risk scores",
    ["model"],
    buckets=(0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)
FLAGS = Counter(
    "mlx_hallucination_flag_total",
    "Requests flagged as high hallucination risk (>0.5)",
    ["model"],
)
H_REPET = Gauge("mlx_heuristic_repetition", "Most-repeated word-trigram ratio (0..1)", ["model"])
H_REFUSAL = Gauge("mlx_heuristic_refusal", "1 if a refusal pattern was detected", ["model"])
H_HEDGE = Gauge("mlx_heuristic_hedging", "Normalized hedge-word density (0..1)", ["model"])
H_UNGROUNDED = Gauge(
    "mlx_heuristic_ungrounded",
    "1 if factual-looking answer was produced without any tool call",
    ["model"],
)
UPSTREAM_UP = Gauge(
    "mlx_up",
    "1 if the upstream mlx_lm server is reachable",
)
IN_FLIGHT = Gauge(
    "mlx_in_flight",
    "Chat completion requests currently being processed",
    ["model"],
)
ERRORS = Counter(
    "mlx_error_total",
    "Requests by HTTP status class (4xx/5xx)",
    ["model", "status", "kind"],
)
CONTEXT_MAX = Gauge(
    "mlx_context_length_max_tokens",
    "Model maximum context length (max_position_embeddings)",
    ["model"],
)
CONTEXT_USED = Gauge(
    "mlx_context_used_tokens",
    "Tokens in the context of the most recent request",
    ["model"],
)
CONTEXT_UTIL = Gauge(
    "mlx_context_utilization",
    "Context used divided by the model maximum context (0..1)",
    ["model"],
)
KV_BYTES_PER_TOKEN = Gauge(
    "mlx_kv_bytes_per_token",
    "Approximate KV cache bytes per token (fp16, from model dims)",
    ["model"],
)
PROMPT_CACHED = Counter(
    "mlx_prompt_cached_tokens_total",
    "Prompt tokens served from the prefix cache",
    ["model", "kind"],
)
PROMPT_CACHE_RATIO = Gauge(
    "mlx_prompt_cache_ratio",
    "cached_tokens / prompt_tokens for the most recent completion (0..1)",
    ["model"],
)
QUEUE_WAIT = Histogram(
    "mlx_queue_wait_seconds",
    "Time a completion request waited before forwarding upstream",
    ["model"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
MODEL_INFO = Gauge(
    "mlx_model_info",
    "Static model configuration attributes",
    ["model", "attr"],
)
MODEL_ARCH = Gauge(
    "mlx_model_arch",
    "Model architecture info",
    ["model", "arch", "dtype"],
)

PERPLEXITY = Gauge(
    "mlx_output_perplexity",
    "Perplexity of the most recent scored completion",
    ["model"],
)
PERPLEXITY_HIST = Histogram(
    "mlx_output_perplexity_histogram",
    "Distribution of completion perplexity",
    ["model"],
    buckets=(1.05, 1.1, 1.25, 1.5, 2.0, 3.0, 5.0, 8.0, 13.0, 21.0, 50.0),
)
ENTROPY = Gauge(
    "mlx_token_entropy_nats",
    "Mean per-token Shannon entropy (nats)",
    ["model"],
)
ENTROPY_HIST = Histogram(
    "mlx_token_entropy_histogram",
    "Distribution of mean per-token entropy (nats)",
    ["model"],
    buckets=(0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0),
)
CONF_MEAN = Gauge(
    "mlx_token_confidence_mean",
    "Mean probability of the tokens the sampler actually picked (0..1)",
    ["model"],
)
CONF_MIN = Gauge(
    "mlx_token_confidence_min",
    "Probability of the least-confident token (0..1)",
    ["model"],
)
CONF_STD = Gauge(
    "mlx_token_confidence_std",
    "Standard deviation of per-token confidence",
    ["model"],
)
MARGIN_MEAN = Gauge(
    "mlx_token_margin_mean",
    "Mean gap between top-1 and top-2 token probabilities (0..1)",
    ["model"],
)
LOW_CONF_RATIO = Gauge(
    "mlx_low_confidence_token_ratio",
    "Fraction of tokens below the low-confidence threshold (0..1)",
    ["model"],
)
SCORED = Counter(
    "mlx_confidence_scored_total",
    "Completions scored with logprobs",
    ["model", "source"],
)
LOW_CONF_RESPONSES = Counter(
    "mlx_low_confidence_response_total",
    "Scored completions whose mean token confidence was below threshold",
    ["model"],
)

REFUSAL_RE = re.compile(
    r"\b(i (can'?t|cannot|do not know|don'?t know|am not able|am unable|"
    r"don'?t have access|couldn'?t find)|no information|not possible "
    r"for me to)\b",
    re.IGNORECASE,
)
HEDGE_WORDS = (
    "maybe", "perhaps", "i think", "i believe", "probably", "roughly",
    "approximately", "i guess", "it seems", "likely", "possibly",
)
_STOPWORDS = set(
    "a an and are as at be but by for from has have in is it its of on or "
    "that the this to was were will with you i he she they we".split()
)


def _trigrams(words):
    return [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]


def heuristic_repetition(text: str) -> float:
    words = [w.strip(".,!?;:()\"'").lower() for w in text.split()]
    words = [w for w in words if w and w not in _STOPWORDS]
    if len(words) < 9:
        return 0.0
    grams = _trigrams(words)
    if not grams:
        return 0.0
    seen = {}
    for g in grams:
        seen[g] = seen.get(g, 0) + 1
    return max(seen.values()) / len(grams)


def heuristic_refusal(text: str) -> float:
    return 1.0 if REFUSAL_RE.search(text) else 0.0


def heuristic_hedging(text: str) -> float:
    low = text.lower()
    count = sum(low.count(w) for w in HEDGE_WORDS)
    return min(count / 5.0, 1.0)


def heuristic_ungrounded(content: str, tools_provided: bool, tool_calls: int) -> float:
    if not tools_provided or tool_calls > 0:
        return 0.0
    has_numbers = re.search(r"\d", content) is not None
    return 1.0 if has_numbers else 0.0


def composite_risk(model: str, content: str, tools_provided: bool,
                   tool_calls: int) -> tuple:
    rep = heuristic_repetition(content)
    ref = heuristic_refusal(content)
    hed = heuristic_hedging(content)
    ung = heuristic_ungrounded(content, tools_provided, tool_calls)
    risk = min(1.0, 0.5 * rep + 0.25 * ref + 0.4 * hed + 0.7 * ung)
    H_REPET.labels(model).set(rep)
    H_REFUSAL.labels(model).set(ref)
    H_HEDGE.labels(model).set(hed)
    H_UNGROUNDED.labels(model).set(ung)
    return risk, bool(risk > 0.5)


def logprob_tokens(logprobs_obj) -> list:
    if not isinstance(logprobs_obj, dict):
        return []
    entries = logprobs_obj.get("content")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and "logprob" in e]


def token_confidences(entries) -> list:
    out = []
    for e in entries:
        try:
            out.append(math.exp(float(e["logprob"])))
        except (TypeError, ValueError, OverflowError):
            continue
    return out


def perplexity(confidences) -> float:
    if not confidences:
        return 0.0
    mean_logp = sum(math.log(max(p, _EPS)) for p in confidences) / len(confidences)
    try:
        return math.exp(-mean_logp)
    except OverflowError:
        return float("inf")


def confidence_stats(confidences) -> tuple:
    if not confidences:
        return 0.0, 0.0, 0.0
    n = len(confidences)
    mean = sum(confidences) / n
    if n < 2:
        return mean, 0.0, confidences[0]
    var = sum((p - mean) ** 2 for p in confidences) / (n - 1)
    return mean, math.sqrt(var), min(confidences)


def mean_token_entropy(entries) -> float:
    totals = []
    for e in entries:
        top = e.get("top_logprobs")
        if not isinstance(top, list) or not top:
            continue
        probs = []
        for cand in top:
            try:
                probs.append(math.exp(float(cand["logprob"])))
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        mass = sum(probs)
        if mass <= 0:
            continue
        h = 0.0
        for p in probs:
            q = p / mass
            if q > _EPS:
                h -= q * math.log(q)
        totals.append(h)
    if not totals:
        return 0.0
    return sum(totals) / len(totals)


def mean_top2_margin(entries) -> float:
    margins = []
    for e in entries:
        top = e.get("top_logprobs")
        if not isinstance(top, list) or len(top) < 2:
            continue
        try:
            probs = sorted(
                (math.exp(float(c["logprob"])) for c in top[:8] if "logprob" in c),
                reverse=True,
            )
        except (TypeError, ValueError, OverflowError):
            continue
        if len(probs) < 2:
            continue
        margins.append(max(0.0, probs[0] - probs[1]))
    if not margins:
        return 0.0
    return sum(margins) / len(margins)


def score_confidence(logprobs_obj, threshold: float) -> dict:
    try:
        entries = logprob_tokens(logprobs_obj)
        confidences = token_confidences(entries)
        if not confidences:
            return {}
        mean, std, lowest = confidence_stats(confidences)
        low = sum(1 for p in confidences if p < threshold) / len(confidences)
        return {
            "tokens": len(confidences),
            "perplexity": perplexity(confidences),
            "entropy": mean_token_entropy(entries),
            "confidence_mean": mean,
            "confidence_std": std,
            "confidence_min": lowest,
            "margin_mean": mean_top2_margin(entries),
            "low_confidence_ratio": low,
        }
    except Exception:
        return {}


def _conf_metadata(conf: dict, source: str) -> dict:
    if not conf:
        return {}
    return {
        "logprobs_source": source,
        "scored_tokens": conf["tokens"],
        "perplexity": round(conf["perplexity"], 4),
        "token_entropy_nats": round(conf["entropy"], 4),
        "confidence_mean": round(conf["confidence_mean"], 4),
        "confidence_min": round(conf["confidence_min"], 4),
        "confidence_std": round(conf["confidence_std"], 4),
        "token_margin_mean": round(conf["margin_mean"], 4),
        "low_confidence_token_ratio": round(conf["low_confidence_ratio"], 4),
    }


def choice_logprobs(resp_obj):
    if not isinstance(resp_obj, dict):
        return None
    choices = resp_obj.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    return first.get("logprobs") if isinstance(first, dict) else None


def strip_logprobs(resp_obj) -> bool:
    removed = False
    if not isinstance(resp_obj, dict):
        return False
    for choice in resp_obj.get("choices") or []:
        if isinstance(choice, dict) and choice.pop("logprobs", None) is not None:
            removed = True
    return removed


def sse_frames(resp_obj, fallback_model: str) -> list:
    choice = (resp_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    base = {
        "id": resp_obj.get("id", "chatcmpl-destreamed"),
        "object": "chat.completion.chunk",
        "created": resp_obj.get("created", int(time.time())),
        "model": resp_obj.get("model", fallback_model),
    }
    if resp_obj.get("system_fingerprint"):
        base["system_fingerprint"] = resp_obj["system_fingerprint"]

    def frame(delta, finish_reason=None):
        obj = dict(base)
        obj["choices"] = [
            {"index": 0, "finish_reason": finish_reason, "delta": delta}
        ]
        return ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")

    out = []
    content = msg.get("content") or ""
    if content:
        out.append(frame({"role": "assistant", "content": content}))
    tool_calls = msg.get("tool_calls") or []
    if tool_calls:
        out.append(
            frame(
                {
                    "role": "assistant",
                    "tool_calls": [
                        dict(tc, index=i) for i, tc in enumerate(tool_calls)
                    ],
                }
            )
        )
    out.append(frame({"role": "assistant"}, choice.get("finish_reason") or "stop"))
    out.append(b"data: [DONE]\n\n")
    return out


class _StreamParser:
    def __init__(self):
        self._buf = ""
        self.content_parts = []
        self._tool_calls = {}
        self.usage = None
        self.finish_reason = None

    def feed(self, chunk: str):
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if "usage" in obj:
                self.usage = obj["usage"]
            choices = obj.get("choices") or []
            if not choices:
                continue
            self.finish_reason = choices[0].get("finish_reason")
            delta = choices[0].get("delta") or {}
            if delta.get("content"):
                self.content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                tid = tc.get("id")
                if not tid:
                    continue
                entry = self._tool_calls.setdefault(tid, {"type": None, "name": ""})
                if tc.get("type"):
                    entry["type"] = tc["type"]
                name_frag = (tc.get("function") or {}).get("name")
                if name_frag:
                    entry["name"] += name_frag

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def tool_calls(self) -> int:
        return len(self._tool_calls)

    @property
    def tools(self) -> list:
        return [
            {"type": entry["type"] or "unknown",
             "name": entry["name"] or entry["type"] or "unknown"}
            for entry in self._tool_calls.values()
        ]

    def completion_tokens(self) -> int:
        if self.usage and self.usage.get("completion_tokens"):
            return int(self.usage["completion_tokens"])
        return len(self.content.split())


def _discover_model_from_upstream(upstream_url: str) -> str | None:
    """Query upstream mlx_lm.server GET /v1/models to discover the loaded model."""
    try:
        resp = requests.get(f"{upstream_url.rstrip('/')}/v1/models", timeout=5)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("data") or []
        if models:
            return models[0].get("id")
    except Exception as exc:
        error(f"failed to discover model from upstream {upstream_url}: {exc!r}",
              component="proxy")
    return None


def _resolve_model(args_model: str | None, upstream_url: str) -> str | None:
    """Resolve model: config > upstream discovery > None (degraded)."""
    if args_model:
        return args_model
    discovered = _discover_model_from_upstream(upstream_url)
    if discovered:
        info(f"model auto-discovered from upstream: {discovered}",
             component="proxy")
        return discovered
    degraded("MLX_MODEL not set and upstream discovery failed; skipping "
             "model-specific gauges", component="proxy")
    return None


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ("127.0.0.1", 8081)
    metrics_path = "/metrics"
    logprobs_top = 0
    stream_sample = 0.0
    low_conf_threshold = 0.5
    max_prompt_tokens = 12000
    max_tokens_cap = 4096
    max_body_bytes = 4 * 1024 * 1024
    api_key = ""
    rate_limit = 0
    rate_window = 10.0
    _rate_hits = {}

    def _rate_limited(self, key):
        now = time.monotonic()
        hits = [t for t in self._rate_hits.get(key, [])
                if now - t < self.rate_window]
        if len(hits) >= self.rate_limit:
            self._rate_hits[key] = hits
            return True
        hits.append(now)
        self._rate_hits[key] = hits
        return False

    def log_message(self, fmt, *args):
        line = "%s - %s" % (self.address_string(), fmt % args)
        info(line, component="proxy.http")

    def _send_rewritten(self, status, resp_headers, resp_obj, raw_chunks,
                        synth_sse, model):
        if resp_obj is None:
            payload = b"".join(raw_chunks)
            self.send_response(status)
            for k, v in resp_headers:
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            return

        strip_logprobs(resp_obj)
        if synth_sse:
            self.send_response(status)
            for k, v in resp_headers:
                if k.lower() in ("content-length", "content-type"):
                    continue
                self.send_header(k, v)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for frame in sse_frames(resp_obj, model):
                self.wfile.write(frame)
                self.wfile.flush()
            return

        payload = json.dumps(resp_obj).encode("utf-8")
        self.send_response(status)
        for k, v in resp_headers:
            if k.lower() == "content-length":
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _reject(self, code, obj, model, kind):
        try:
            ERRORS.labels(model, f"{code // 100}xx", kind).inc()
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except Exception:
            pass
        self.close_connection = True

    def _forward(self):
        t_arrival = time.monotonic()
        length = int(self.headers.get("Content-Length", 0) or 0)

        if self.api_key and self.headers.get("Authorization") != f"Bearer {self.api_key}":
            self._reject(401, {"error": {"message": "unauthorized",
                                         "type": "authentication_error"}},
                         _MODEL_NAME or "unknown", "auth")
            return
        if length > self.max_body_bytes:
            self._reject(413, {"error": {"message": "request body too large",
                                         "type": "invalid_request_error"}},
                         _MODEL_NAME or "unknown", "chat")
            return

        body = self.rfile.read(length) if length else b""

        req_path = self.path
        if self.path == self.metrics_path:
            payload = generate_latest()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPE_LATEST)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        model = _MODEL_NAME or "unknown"
        streaming = False
        temperature = 0.0
        tools_provided = False
        messages = []
        opik_trace = None
        opik_span = None
        lp_source = ""
        synth_sse = False
        kind = "chat"
        is_chat = self.command == "POST" and req_path.endswith(
            "/v1/chat/completions"
        )
        if is_chat:
            try:
                req = json.loads(body or b"{}")
                model = req.get("model", model)
                streaming = bool(req.get("stream", False))
                temperature = req.get("temperature", 0.0)
                tools_provided = bool(req.get("tools"))
                messages = req.get("messages") or []
                if bool(req.get("enable_thinking", False)):
                    kind = "reasoning"

                if model != _MODEL_NAME and _MODEL_NAME is not None:
                    self._reject(
                        400,
                        {"error": {"message": f"model not loaded (proxy serves "
                         f"{_MODEL_NAME})", "type": "invalid_request_error"}},
                        model, kind,
                    )
                    return
                prompt_tokens = sum(
                    len((m.get("content") or "") if isinstance(m.get("content"), str)
                        else json.dumps(m.get("content") or ""))
                    for m in messages
                ) // 4
                if prompt_tokens > self.max_prompt_tokens:
                    self._reject(
                        400,
                        {"error": {"message": f"prompt too long (~{prompt_tokens} "
                         f"tokens > max {self.max_prompt_tokens})",
                         "type": "invalid_request_error"}},
                        model, kind,
                    )
                    return
                mt = req.get("max_tokens")
                if mt is None:
                    req["max_tokens"] = self.max_tokens_cap
                elif mt > self.max_tokens_cap:
                    req["max_tokens"] = self.max_tokens_cap
                    body = json.dumps(req).encode("utf-8")

                if req.get("logprobs"):
                    lp_source = "client" if not streaming else ""
                elif self.logprobs_top > 0:
                    if not streaming:
                        lp_source = "injected"
                    elif self.stream_sample > 0 and random.random() < self.stream_sample:
                        lp_source = "destreamed"
                        synth_sse = True
                    if lp_source:
                        req["logprobs"] = True
                        req["top_logprobs"] = self.logprobs_top
                        if synth_sse:
                            req["stream"] = False
                        body = json.dumps(req).encode("utf-8")
            except json.JSONDecodeError:
                pass
            if _OPIK is not None and self.headers.get("X-Mlx-Trace", "1") != "0":
                try:
                    opik_trace = _OPIK.trace(
                        name="mlx.chat.completions",
                        input={
                            "model": model,
                            "stream": streaming,
                            "temperature": temperature,
                            "tools_provided": tools_provided,
                            "messages": messages,
                        },
                    )
                    opik_span = opik_trace.span(
                        name="chat.completions",
                        type="llm",
                        model=model,
                        provider="mlx",
                        input={
                            "model": model,
                            "stream": streaming,
                            "temperature": temperature,
                            "tools_provided": tools_provided,
                            "messages": messages,
                        },
                    )
                    _OPIK.flush()
                except Exception as exc:
                    error(f"opik trace start failed ({exc!r})", component="proxy")
                    opik_trace = None
                    opik_span = None

        conn = http.client.HTTPConnection(
            self.upstream[0], self.upstream[1], timeout=300
        )
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in (
                "host", "connection", "transfer-encoding",
                "keep-alive", "upgrade", "proxy-connection",
                "content-length",
            ):
                continue
            headers[k] = v
        t_send = time.monotonic()
        if is_chat:
            QUEUE_WAIT.labels(model).observe(t_send - t_arrival)
        t_first_token = None
        span = None
        if _OTEL is not None:
            span = _OTEL.tracer.start_span(
                "mlx.chat.completions",
                attributes={
                    "http.request.method": self.command,
                    "url.path": req_path,
                    "mlx.model": model,
                    "mlx.streaming": streaming,
                    "mlx.kind": kind,
                    "mlx.temperature": temperature,
                },
            )
            span.set_attribute("mlx.tools_provided", tools_provided)
            if _OPIK_OTLP:
                span.set_attribute("openinference.span.kind", "LLM")
                span.set_attribute("llm.provider", "mlx")
                span.set_attribute(
                    "input.value",
                    json.dumps(
                        {
                            "model": model,
                            "stream": streaming,
                            "temperature": temperature,
                            "tools_provided": tools_provided,
                            "messages": messages,
                        }
                    ),
                )
                span.set_attribute("input.mime_type", "application/json")
                span.set_attribute("llm.model_name", model)
                span.set_attribute(
                    "llm.invocation_parameters",
                    json.dumps(
                        {
                            "temperature": temperature,
                            "stream": streaming,
                            "tools_provided": tools_provided,
                        }
                    ),
                )
        if is_chat:
            IN_FLIGHT.labels(model).inc()
        try:
            conn.request(self.command, req_path, body=body, headers=headers)
            resp = conn.getresponse()
            UPSTREAM_UP.set(1)

            status = resp.status
            if status >= 400:
                ERRORS.labels(model, f"{status // 100}xx", kind).inc()
            resp_headers = []
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "keep-alive"):
                    continue
                resp_headers.append((k, v))

            is_sse = (resp.getheader("Content-Type", "") or "").startswith(
                "text/event-stream"
            )
            rewrite = synth_sse or (
                lp_source == "injected" and not is_sse and status < 400
            )
            self.close_connection = True
            if not rewrite:
                self.send_response(status)
                for k, v in resp_headers:
                    self.send_header(k, v)
                self.send_header("Connection", "close")
                self.end_headers()

            parser = _StreamParser()
            content_bytes = []

            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if is_sse:
                    parser.feed(chunk.decode("utf-8", "replace"))
                    if t_first_token is None and parser.content_parts:
                        t_first_token = time.monotonic()
                else:
                    content_bytes.append(chunk)
                if not rewrite:
                    self.wfile.write(chunk)
                    self.wfile.flush()

            t_done = time.monotonic()

            resp_obj = None
            if is_sse:
                content = parser.content
                tool_calls = parser.tool_calls
                tools = parser.tools
                usage = parser.usage
                finish_reason = parser.finish_reason
            else:
                content = ""
                tool_calls = 0
                tools = []
                usage = None
                finish_reason = None
                try:
                    obj = json.loads(b"".join(content_bytes).decode("utf-8", "replace"))
                    resp_obj = obj
                    choice = (obj.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    content = msg.get("content") or ""
                    calls = msg.get("tool_calls") or []
                    tool_calls = len(calls)
                    tools = [
                        {
                            "type": tc.get("type") or "unknown",
                            "name": (tc.get("function") or {}).get("name")
                            or tc.get("type") or "unknown",
                        }
                        for tc in calls
                    ]
                    usage = obj.get("usage")
                    finish_reason = choice.get("finish_reason")
                except json.JSONDecodeError:
                    pass

            lp_obj = choice_logprobs(resp_obj) if lp_source else None

            if rewrite:
                self._send_rewritten(
                    status, resp_headers, resp_obj, content_bytes,
                    synth_sse, model,
                )

            if not is_chat:
                return

            if usage:
                prompt_tokens = int(usage.get("prompt_tokens", 0))
                comp_tokens = int(usage.get("completion_tokens", 0))
            else:
                prompt_tokens = sum(
                    len(str(m.get("content", "")).split()) for m in messages
                )
                comp_tokens = len(content.split())
            TOKENS_PROMPT.labels(model, kind).inc(prompt_tokens)
            TOKENS_COMPLETION.labels(model, kind).inc(comp_tokens)
            if usage:
                cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
                if cached > 0:
                    PROMPT_CACHED.labels(model, kind).inc(cached)
                if prompt_tokens > 0:
                    PROMPT_CACHE_RATIO.labels(model=model).set(
                        min(1.0, cached / float(prompt_tokens))
                    )

            context_used = prompt_tokens + comp_tokens
            CONTEXT_USED.labels(model=model).set(context_used)
            if _CTX_MAX_TOKENS > 0:
                CONTEXT_UTIL.labels(model=model).set(
                    min(1.0, context_used / float(_CTX_MAX_TOKENS))
                )

            gen_time = t_done - t_send
            GEN_TIME.labels(model).observe(gen_time)
            if t_first_token is not None:
                TTFT.labels(model).observe(t_first_token - t_send)
            elif not synth_sse:
                TTFT.labels(model).observe(gen_time)

            if comp_tokens > 0 and gen_time > 0:
                TOKEN_RATE.labels(model).observe(comp_tokens / gen_time)

            REQUESTS.labels(model, "true" if streaming else "false", kind).inc()
            FINISH_REASON.labels(model, finish_reason or "unknown", kind).inc()
            for t in tools:
                TOOL_CALLS.labels(model, kind, t["name"], t["type"]).inc()
            TEMPERATURE.labels(model, kind).set(temperature)

            risk, flagged = composite_risk(model, content, tools_provided, tool_calls)
            RISK.labels(model, kind).set(risk)
            RISK_HIST.labels(model).observe(risk)
            if flagged:
                FLAGS.labels(model).inc()

            conf = score_confidence(lp_obj, self.low_conf_threshold) if lp_obj else {}
            if conf:
                PERPLEXITY.labels(model).set(conf["perplexity"])
                PERPLEXITY_HIST.labels(model).observe(conf["perplexity"])
                ENTROPY.labels(model).set(conf["entropy"])
                ENTROPY_HIST.labels(model).observe(conf["entropy"])
                CONF_MEAN.labels(model).set(conf["confidence_mean"])
                CONF_MIN.labels(model).set(conf["confidence_min"])
                CONF_STD.labels(model).set(conf["confidence_std"])
                MARGIN_MEAN.labels(model).set(conf["margin_mean"])
                LOW_CONF_RATIO.labels(model).set(conf["low_confidence_ratio"])
                SCORED.labels(model, lp_source).inc()
                if conf["confidence_mean"] < self.low_conf_threshold:
                    LOW_CONF_RESPONSES.labels(model).inc()

            if span is not None:
                span.set_attribute(
                    "mlx.ttft_seconds",
                    (t_first_token - t_send) if t_first_token is not None else gen_time,
                )
                span.set_attribute("mlx.gen_seconds", gen_time)
                span.set_attribute("mlx.tokens_prompt", prompt_tokens)
                span.set_attribute("mlx.tokens_completion", comp_tokens)
                span.set_attribute("mlx.tool_calls", tool_calls)
                if tools:
                    span.set_attribute(
                        "mlx.tool_names",
                        ", ".join(t["name"] for t in tools),
                    )
                span.set_attribute("mlx.hallucination_risk", risk)
                span.set_attribute("mlx.hallucination_flagged", flagged)
                span.set_attribute("http.response.status_code", status)
                if conf:
                    span.set_attribute("mlx.logprobs_source", lp_source)
                    span.set_attribute("mlx.perplexity", round(conf["perplexity"], 4))
                    span.set_attribute("mlx.token_entropy", round(conf["entropy"], 4))
                    span.set_attribute(
                        "mlx.confidence_mean", round(conf["confidence_mean"], 4)
                    )
                    span.set_attribute(
                        "mlx.confidence_min", round(conf["confidence_min"], 4)
                    )
                    span.set_attribute(
                        "mlx.low_confidence_token_ratio",
                        round(conf["low_confidence_ratio"], 4),
                    )
                if _OPIK_OTLP:
                    span.set_attribute(
                        "output.value",
                        json.dumps({"content": content, "tool_calls": tool_calls}),
                    )
                    span.set_attribute("output.mime_type", "application/json")
                    span.set_attribute("llm.token_count.prompt", prompt_tokens)
                    span.set_attribute(
                        "llm.token_count.completion", comp_tokens
                    )
                    span.set_attribute(
                        "llm.token_count.total", prompt_tokens + comp_tokens
                    )
                    span.set_attribute(
                        "metadata",
                        json.dumps(
                            {
                                "node": _NODE_NAME,
                                "ttft_seconds": round(
                                    (t_first_token - t_send)
                                    if t_first_token is not None else gen_time,
                                    4,
                                ),
                                "generation_seconds": round(gen_time, 4),
                                "hallucination_risk": risk,
                                "hallucination_flagged": flagged,
                                **_conf_metadata(conf, lp_source),
                            }
                        ),
                    )
                span.set_status("OK")
                span.end()

            if opik_span is not None:
                try:
                    opik_span.update(
                        output={
                            "content": content,
                            "tool_calls": tool_calls,
                            "usage": {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": comp_tokens,
                                "total_tokens": prompt_tokens + comp_tokens,
                            },
                        },
                        usage={
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": comp_tokens,
                            "total_tokens": prompt_tokens + comp_tokens,
                        },
                        metadata={
                            "node": _NODE_NAME,
                            "ttft_seconds": round(
                                (t_first_token - t_send)
                                if t_first_token is not None else gen_time,
                                4,
                            ),
                            "generation_seconds": round(gen_time, 4),
                            "hallucination_risk": risk,
                            "hallucination_flagged": flagged,
                            **_conf_metadata(conf, lp_source),
                        },
                    )
                    opik_span.end()
                except Exception as exc:
                    error(f"opik span end failed ({exc!r})", component="proxy")

            if opik_trace is not None:
                try:
                    opik_trace.update(
                        output={
                            "content": content,
                            "tool_calls": tool_calls,
                            "usage": {
                                "prompt_tokens": prompt_tokens,
                                "completion_tokens": comp_tokens,
                                "total_tokens": prompt_tokens + comp_tokens,
                            },
                        }
                    )
                    opik_trace.end()
                except Exception as exc:
                    error(f"opik trace end failed ({exc!r})", component="proxy")
                if opik_trace is not None:
                    try:
                        opik_trace.log_feedback_score(
                            name="hallucination_quality",
                            value=round(1.0 - risk, 3),
                            category_name="quality",
                            reason=(
                                "proxy heuristic (repetition/refusal/hedging/"
                                "ungrounded signals), higher is better"
                            ),
                        )
                        opik_trace.log_feedback_score(
                            name="hallucination_flagged",
                            value=0.0 if flagged else 1.0,
                            category_name="safety",
                            reason=(
                                "1.0 == proxy heuristics did NOT flag the "
                                "output for hallucination risk"
                            ),
                        )
                        if conf:
                            opik_trace.log_feedback_score(
                                name="token_confidence",
                                value=round(conf["confidence_mean"], 3),
                                category_name="quality",
                                reason=(
                                    "mean probability of the sampled tokens "
                                    f"({conf['tokens']} scored, source="
                                    f"{lp_source}), higher is better"
                                ),
                            )
                    except Exception as exc:
                        error(
                            f"opik feedback scores failed ({exc!r})",
                            component="proxy",
                        )

        except (socket.error, http.client.HTTPException, ConnectionRefusedError) as exc:
            UPSTREAM_UP.set(0)
            ERRORS.labels(model, "5xx", kind).inc()
            if span is not None:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_attribute("http.response.status_code", 503)
                span.set_status("ERROR", str(exc))
                span.end()
            if opik_span is not None:
                try:
                    opik_span.end(
                        error_info={
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        }
                    )
                except Exception:
                    pass
            if opik_trace is not None:
                try:
                    opik_trace.end(
                        error_info={
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                        }
                    )
                except Exception:
                    pass
            try:
                self.send_response(503)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"mlx upstream unavailable\n")
                self.close_connection = True
            except Exception:
                pass
        finally:
            if is_chat:
                IN_FLIGHT.labels(model).dec()
            if span is not None:
                try:
                    span.end()
                except Exception:
                    pass
            if _OPIK is not None and (opik_trace is not None or opik_span is not None):
                try:
                    _OPIK.flush()
                except Exception:
                    pass
            conn.close()

    def do_GET(self):
        self._forward()

    def do_POST(self):
        self._forward()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


def _health_watch(upstream):
    while True:
        try:
            c = http.client.HTTPConnection(upstream[0], upstream[1], timeout=3)
            c.request("GET", "/v1/models")
            r = c.getresponse()
            UPSTREAM_UP.set(1 if r.status == 200 else 0)
            c.close()
        except Exception:
            UPSTREAM_UP.set(0)
        time.sleep(15)


def main():
    global _OTEL, _OPIK, _OPIK_OTLP, _NODE_NAME, _MODEL_NAME, _CTX_MAX_TOKENS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", default=os.environ.get("MLX_PROXY_LISTEN", "0.0.0.0:8080"))
    ap.add_argument("--upstream", default=os.environ.get("MLX_UPSTREAM", "127.0.0.1:8081"))
    ap.add_argument("--metrics-path", default=os.environ.get("MLX_METRICS_PATH", "/metrics"))
    ap.add_argument(
        "--logprobs",
        type=int,
        default=int(os.environ.get("MLX_LOGPROBS", "3")),
        help="top_logprobs to request from upstream for confidence scoring",
    )
    ap.add_argument(
        "--logprobs-stream-sample",
        type=float,
        default=float(os.environ.get("MLX_LOGPROBS_STREAM_SAMPLE", "0.0")),
    )
    ap.add_argument(
        "--low-confidence-threshold",
        type=float,
        default=float(os.environ.get("MLX_LOW_CONFIDENCE", "0.5")),
    )
    ap.add_argument(
        "--model",
        default=os.environ.get("MLX_MODEL"),
        help="model id (optional; if omitted, discovered from upstream /v1/models)",
    )
    ap.add_argument(
        "--otlp-endpoint",
        default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
    )
    ap.add_argument(
        "--node-name",
        default=os.environ.get("MLX_NODE_NAME", ""),
    )
    ap.add_argument(
        "--opik-endpoint",
        default=os.environ.get("OPIK_ENDPOINT", ""),
    )
    ap.add_argument(
        "--opik-otlp-endpoint",
        default=os.environ.get("OPIK_OTLP_ENDPOINT", ""),
    )
    ap.add_argument(
        "--max-prompt-tokens",
        type=int,
        default=int(os.environ.get("MAX_PROMPT_TOKENS", "12000")),
    )
    ap.add_argument(
        "--max-tokens-cap",
        type=int,
        default=int(os.environ.get("MAX_TOKENS_CAP", "4096")),
    )
    ap.add_argument(
        "--max-body-bytes",
        type=int,
        default=int(os.environ.get("MLX_MAX_BODY_BYTES", str(4 * 1024 * 1024))),
    )
    ap.add_argument(
        "--rate-limit",
        type=int,
        default=int(os.environ.get("MLX_RATE_LIMIT", "0")),
    )
    args = ap.parse_args()

    setup_logging(service="mlx-proxy",
                  macbook=os.environ.get("MLX_MACBOOK_ID", "unknown"))

    host, _, port = args.listen.rpartition(":")
    up_host, _, up_port = args.upstream.rpartition(":")
    Proxy.upstream = (up_host or "127.0.0.1", int(up_port or 8081))
    Proxy.metrics_path = args.metrics_path
    Proxy.logprobs_top = max(0, min(10, args.logprobs))
    Proxy.stream_sample = max(0.0, min(1.0, args.logprobs_stream_sample))
    Proxy.low_conf_threshold = args.low_confidence_threshold
    Proxy.api_key = os.environ.get("MLX_API_KEY", "")
    Proxy.max_prompt_tokens = args.max_prompt_tokens
    Proxy.max_tokens_cap = args.max_tokens_cap
    Proxy.max_body_bytes = args.max_body_bytes
    Proxy.rate_limit = args.rate_limit

    upstream_url = f"http://{Proxy.upstream[0]}:{Proxy.upstream[1]}"
    _MODEL_NAME = _resolve_model(args.model, upstream_url)

    TOOL_CALLS.labels(model=_MODEL_NAME or "unknown", kind="", tool="", type="").inc(0)
    FLAGS.labels(model=_MODEL_NAME or "unknown").inc(0)
    _NODE_NAME = args.node_name
    _OTEL = _setup_otel(
        args.otlp_endpoint, args.node_name, opik_otlp_endpoint=args.opik_otlp_endpoint
    )
    if args.opik_endpoint:
        try:
            from opik import Opik
            opik_host = args.opik_endpoint
            if not opik_host.rstrip("/").endswith("/api"):
                opik_host = opik_host.rstrip("/") + "/api"
            _OPIK = Opik(host=opik_host, project_name="mlx")
            info(f"opik SDK -> {opik_host} (project=mlx)", component="proxy")
        except Exception as exc:
            error(f"opik SDK init failed ({exc!r})", component="proxy")
            _OPIK = None
    if args.opik_otlp_endpoint:
        _OPIK_OTLP = True
        info(f"opik OTLP -> {args.opik_otlp_endpoint} (project=mlx)", component="proxy")

    try:
        info_obj = _model_info(_MODEL_NAME) if _model_info and _MODEL_NAME else None
        if info_obj:
            _CTX_MAX_TOKENS = info_obj.max_context_tokens
            CONTEXT_MAX.labels(model=_MODEL_NAME).set(info_obj.max_context_tokens)
            KV_BYTES_PER_TOKEN.labels(model=_MODEL_NAME).set(info_obj.kv_bytes_per_token)
            for attr, val in (
                ("layer_count", info_obj.layers),
                ("kv_heads", info_obj.kv_heads),
                ("head_dim", info_obj.head_dim),
                ("hidden_size", info_obj.hidden_size),
                ("attention_heads", info_obj.attention_heads),
                ("vocab_size", info_obj.vocab_size),
                ("intermediate_size", info_obj.intermediate_size),
                ("quant_bits", info_obj.quant_bits),
                ("quant_group_size", info_obj.quant_group_size),
                ("linear_key_heads", info_obj.linear_key_heads),
                ("linear_value_heads", info_obj.linear_value_heads),
                ("linear_key_head_dim", info_obj.linear_key_head_dim),
                ("linear_value_head_dim", info_obj.linear_value_head_dim),
                ("linear_conv_kernel_dim", info_obj.linear_conv_kernel_dim),
                ("mtp_layers", info_obj.mtp_layers),
                ("tie_word_embeddings", int(info_obj.tie_word_embeddings)),
            ):
                MODEL_INFO.labels(model=_MODEL_NAME, attr=attr).set(val)
            if info_obj.arch or info_obj.dtype:
                MODEL_ARCH.labels(
                    model=_MODEL_NAME, arch=info_obj.arch or "unknown",
                    dtype=info_obj.dtype or "unknown",
                ).set(1)
            info(
                f"model ctx={info_obj.max_context_tokens} kv={info_obj.kv_bytes_per_token} B/tok "
                f"layers={info_obj.layers} hidden={info_obj.hidden_size}",
                component="proxy",
            )
    except Exception as exc:
        error(f"model info failed ({exc!r})", component="proxy")

    threading.Thread(
        target=_health_watch, args=(Proxy.upstream,), daemon=True
    ).start()

    server = ThreadingHTTPServer((host or "0.0.0.0", int(port or 8080)), Proxy)
    info(
        f"listening on {host or '0.0.0.0'}:{port or 8080} "
        f"-> upstream {Proxy.upstream[0]}:{Proxy.upstream[1]} "
        f"(metrics at {args.metrics_path})",
        component="proxy",
    )
    if Proxy.logprobs_top:
        info(
            f"confidence scoring: top_logprobs={Proxy.logprobs_top} "
            f"stream_sample={Proxy.stream_sample} "
            f"low_conf<{Proxy.low_conf_threshold}",
            component="proxy",
        )
    else:
        info("confidence scoring: disabled (--logprobs 0)", component="proxy")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
