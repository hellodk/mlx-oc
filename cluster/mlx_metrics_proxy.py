#!/usr/bin/env python3
"""
mlx_metrics_proxy.py
====================
Reverse proxy + Prometheus exporter that sits in front of `mlx_lm.server`.

The proxy is the "front door" (0.0.0.0:8080) that opencode and other clients
talk to. It forwards every request to the real mlx_lm server (127.0.0.1:8081)
and records, per request:

  * TTFT           - time until the first content-bearing SSE token arrives
  * token rate     - completion tokens / generation time
  * temperature    - sampling temperature used for the request
  * tool calls     - number of function calls the model requested
  * hallucination  - heuristic risk signals (repetition, refusal, hedging,
                     ungrounded claims when tools were available)

Metrics are served in Prometheus text format on /metrics. This scrape is the
single source of truth for VM; the proxy does NOT export OTLP metrics so that
`mlx_*` series exist exactly once (see audit note on metric duplication).

Optional OpenTelemetry (--otlp-endpoint): each request becomes a trace span
and a log record exported to an OTLP HTTP gateway (e.g. the in-cluster
otel-collector). Disabled unless --otlp-endpoint is set.

Optional Opik tracing (two independent paths, both optional):

  * --opik-endpoint      base URL of a self-hosted Opik (e.g.
                         http://192.168.1.10:32173). Uses the opik SDK to log
                         each chat completion as a trace with an LLM span
                         (input/output/usage/metadata) into the "mlx" project.
  * --opik-otlp-endpoint OTLP HTTP endpoint of Opik's ingestion (e.g.
                         http://192.168.1.10:32173/api/v1/private/otel). Adds a
                         second trace exporter and stamps spans with
                         OpenInference attributes (input.value, llm.model_name,
                         token counts, ...) so the Opik collector renders them.

Usage:
  mlx_metrics_proxy.py --listen 0.0.0.0:8080 --upstream 127.0.0.1:8081
  mlx_metrics_proxy.py --otlp-endpoint http://192.168.1.10:30318 --node-name rank0
  mlx_metrics_proxy.py --opik-endpoint http://192.168.1.10:32173 --node-name rank0
  mlx_metrics_proxy.py --opik-otlp-endpoint http://192.168.1.10:32173/api/v1/private/otel
"""

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

MODEL_DEFAULT = os.environ.get("MLX_MODEL", "mlx-community/Qwen3.5-4B-MLX-8bit")
_CTX_MAX_TOKENS = 0  # model max context, set at startup from config

# Model context/KV metrics (best-effort; falls back to defaults if the model
# config cannot be located).
try:
    from mlx_model_info import model_info as _model_info
except Exception:  # pragma: no cover - import guard
    _model_info = None

# --------------------------------------------------------------------------
# OpenTelemetry (optional)
# --------------------------------------------------------------------------
_OTEL = None  # set by _setup_otel(); None == OTel disabled

# Opik integration (optional, independent of _OTEL)
_OPIK = None       # opik.Opik SDK client, or None
_OPIK_OTLP = False  # stamp OpenInference attrs for Opik's OTLP ingestion
_NODE_NAME = ""     # node/rank label attached to traces


def _sig_url(endpoint, sig):
    """Return the full OTLP URL for a signal path, e.g. <endpoint>/v1/metrics."""
    url = endpoint.rstrip("/")
    return url if url.endswith(f"/v1/{sig}") else f"{url}/v1/{sig}"


def _setup_otel(endpoint, node_name, service_name="mlx-metrics-proxy", opik_otlp_endpoint=""):
    """Best-effort OTel init. Returns an OTel helper or None on failure.

    `endpoint` is the primary OTLP HTTP gateway (otel-collector); when set,
    traces + logs are exported there. `opik_otlp_endpoint` (e.g.
    http://host/api/v1/private/otel) adds a second trace processor that sends
    the same spans to Opik's OTLP ingestion, tagged to the "mlx" project.
    """
    if not endpoint and not opik_otlp_endpoint:
        return None
    try:
        from opentelemetry import trace, metrics, _logs
        from opentelemetry.sdk.resources import (
            Resource,
            SERVICE_NAME,
            HOST_NAME,
            SERVICE_INSTANCE_ID,
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
        resource = Resource.create(
            {
                SERVICE_NAME: service_name,
                HOST_NAME: host,
                SERVICE_INSTANCE_ID: f"{node_name or host}:proxy",
                "mlx.node.name": node_name or host,
            }
        )

        # -- traces ------------------------------------------------------
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

        # -- logs (primary gateway only) ---------------------------------
        if endpoint:
            lp = LoggerProvider(resource=resource)
            lp.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(endpoint=_sig_url(endpoint, "logs"))
                )
            )
            _logs.set_logger_provider(lp)
            logging_handler = LoggingHandler(
                level=logging.INFO, logger_provider=lp
            )
            root = logging.getLogger()
            root.setLevel(logging.INFO)
            root.addHandler(logging_handler)

        class Otel:
            tracer = tp.get_tracer("mlx.proxy")

            @staticmethod
            def span(name, **attrs):
                return Otel.tracer.start_as_current_span(name, attributes=attrs)

        return Otel
    except Exception as exc:  # keep the proxy alive without OTel
        print(
            f"[proxy] OTel setup failed ({exc!r}); continuing without OTel",
            flush=True,
        )
        return None

# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
REQUESTS = Counter(
    "mlx_requests_total",
    "Chat completion requests served",
    ["model", "streaming"],
)
TOKENS_PROMPT = Counter(
    "mlx_tokens_prompt_total",
    "Prompt tokens processed",
    ["model"],
)
TOKENS_COMPLETION = Counter(
    "mlx_tokens_completion_total",
    "Completion tokens generated",
    ["model"],
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
    "Function calls requested by the model",
    ["model"],
)
FINISH_REASON = Counter(
    "mlx_finish_reason_total",
    "Completions by finish_reason (stop/length/tool_calls/...). A rising "
    "'length' share means responses are getting truncated before finishing "
    "their thought - a first-order quality signal for agentic/coding use.",
    ["model", "reason"],
)
TEMPERATURE = Gauge(
    "mlx_temperature",
    "Sampling temperature used for the most recent request",
    ["model"],
)
RISK = Gauge(
    "mlx_hallucination_risk",
    "Composite heuristic hallucination risk (0..1) for the most recent request",
    ["model"],
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
    "Chat completion requests currently being processed (queue depth)",
    ["model"],
)
ERRORS = Counter(
    "mlx_error_total",
    "Requests by HTTP status class (4xx/5xx)",
    ["model", "status"],
)
CONTEXT_MAX = Gauge(
    "mlx_context_length_max_tokens",
    "Model maximum context length (max_position_embeddings)",
    ["model"],
)
CONTEXT_USED = Gauge(
    "mlx_context_used_tokens",
    "Tokens in the context of the most recent request (prompt + completion)",
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

# --------------------------------------------------------------------------
# Hallucination heuristics (lightweight proxies, NOT ground-truth detection)
# --------------------------------------------------------------------------
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


def composite_risk(model: str, content: str, tools_provided: bool, tool_calls: int) -> tuple:
    rep = heuristic_repetition(content)
    ref = heuristic_refusal(content)
    hed = heuristic_hedging(content)
    ung = heuristic_ungrounded(content, tools_provided, tool_calls)
    # refusal is the *opposite* of a hallucination, so it is weighted lightly.
    risk = min(1.0, 0.5 * rep + 0.25 * ref + 0.4 * hed + 0.7 * ung)
    H_REPET.labels(model).set(rep)
    H_REFUSAL.labels(model).set(ref)
    H_HEDGE.labels(model).set(hed)
    H_UNGROUNDED.labels(model).set(ung)
    return risk, bool(risk > 0.5)


# --------------------------------------------------------------------------
# SSE parsing helpers
# --------------------------------------------------------------------------
class _StreamParser:
    """Incrementally parses an OpenAI-compatible SSE stream."""

    def __init__(self):
        self._buf = ""
        self.content_parts = []
        self.tool_call_ids = set()
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
                if tc.get("id"):
                    self.tool_call_ids.add(tc["id"])

    @property
    def content(self) -> str:
        return "".join(self.content_parts)

    @property
    def tool_calls(self) -> int:
        return len(self.tool_call_ids)

    def completion_tokens(self) -> int:
        if self.usage and self.usage.get("completion_tokens"):
            return int(self.usage["completion_tokens"])
        return len(self.content.split())


# --------------------------------------------------------------------------
# Proxy
# --------------------------------------------------------------------------
class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ("127.0.0.1", 8081)
    default_temp = 0.0

    # -- helpers -----------------------------------------------------------
    def log_message(self, fmt, *args):
        line = "%s - %s" % (self.address_string(), fmt % args)
        sys.stderr.write("[proxy] %s\n" % line)
        logging.getLogger("mlx.proxy.http").info("request: %s", line)

    def _forward(self):
        """Forward the current request to upstream and stream the reply."""
        length = int(self.headers.get("Content-Length", 0) or 0)
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

        # -- parse the request for stats ----------------------------------
        model = MODEL_DEFAULT
        streaming = False
        temperature = self.default_temp
        tools_provided = False
        messages = []
        opik_trace = None
        opik_span = None
        if self.command == "POST" and req_path.endswith("/v1/chat/completions"):
            try:
                req = json.loads(body or b"{}")
                model = req.get("model", model)
                streaming = bool(req.get("stream", False))
                temperature = req.get("temperature", self.default_temp)
                tools_provided = bool(req.get("tools"))
                messages = req.get("messages") or []
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
                    # Force the create POSTs out before any update: the SDK
                    # streamer can otherwise send PATCH updates ahead of the
                    # batched create, and the backend's create is idempotent so
                    # it never backfills name/input/start_time once the trace
                    # exists. A flush here keeps the create first.
                    _OPIK.flush()
                except Exception as exc:
                    print(f"[proxy] opik trace start failed ({exc!r})", flush=True)
                    opik_trace = None
                    opik_span = None

        # -- forward -------------------------------------------------------
        conn = http.client.HTTPConnection(
            self.upstream[0], self.upstream[1], timeout=300
        )
        headers = {}
        for k, v in self.headers.items():
            if k.lower() in (
                "host", "connection", "transfer-encoding",
                "keep-alive", "upgrade", "proxy-connection",
            ):
                continue
            headers[k] = v
        t_send = time.monotonic()
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
                    "mlx.temperature": temperature,
                },
            )
            span.set_attribute("mlx.tools_provided", tools_provided)
            if _OPIK_OTLP:
                # OpenInference attributes so Opik's OTLP ingestion can render
                # prompt/completion content and LLM metadata.
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
        IN_FLIGHT.labels(model).inc()
        try:
            conn.request(self.command, req_path, body=body, headers=headers)
            resp = conn.getresponse()
            UPSTREAM_UP.set(1)

            status = resp.status
            if status >= 400:
                ERRORS.labels(model, f"{status // 100}xx").inc()
            resp_headers = []
            for k, v in resp.getheaders():
                if k.lower() in ("transfer-encoding", "connection", "keep-alive"):
                    continue
                resp_headers.append((k, v))

            self.send_response(status)
            for k, v in resp_headers:
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            is_sse = (resp.getheader("Content-Type", "") or "").startswith(
                "text/event-stream"
            )
            parser = _StreamParser()
            content_bytes = []

            while True:
                # read1(): return available bytes instead of blocking until
                # the full buffer size is read. read(65536) would buffer the
                # entire SSE response and destroy streaming / TTFT accuracy.
                chunk = resp.read1(65536)
                if not chunk:
                    break
                if is_sse:
                    parser.feed(chunk.decode("utf-8", "replace"))
                    if t_first_token is None and parser.content_parts:
                        t_first_token = time.monotonic()
                else:
                    content_bytes.append(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()

            t_done = time.monotonic()

            # -- resolve stats --------------------------------------------
            if is_sse:
                content = parser.content
                tool_calls = parser.tool_calls
                usage = parser.usage
                finish_reason = parser.finish_reason
            else:
                content = ""
                tool_calls = 0
                usage = None
                finish_reason = None
                try:
                    obj = json.loads(b"".join(content_bytes).decode("utf-8", "replace"))
                    choice = (obj.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    content = msg.get("content") or ""
                    tool_calls = len(msg.get("tool_calls") or [])
                    usage = obj.get("usage")
                    finish_reason = choice.get("finish_reason")
                except json.JSONDecodeError:
                    pass

            if usage:
                prompt_tokens = int(usage.get("prompt_tokens", 0))
                comp_tokens = int(usage.get("completion_tokens", 0))
            else:
                # upstream mlx_lm.server does not return a `usage` object;
                # fall back to a word-count estimate for prompt tokens.
                prompt_tokens = sum(
                    len(str(m.get("content", "")).split()) for m in messages
                )
                comp_tokens = len(content.split())
            TOKENS_PROMPT.labels(model).inc(prompt_tokens)
            TOKENS_COMPLETION.labels(model).inc(comp_tokens)

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
            else:
                TTFT.labels(model).observe(gen_time)

            if comp_tokens > 0 and gen_time > 0:
                TOKEN_RATE.labels(model).observe(comp_tokens / gen_time)

            REQUESTS.labels(model, "true" if streaming else "false").inc()
            FINISH_REASON.labels(model, finish_reason or "unknown").inc()
            TOOL_CALLS.labels(model).inc(tool_calls)
            TEMPERATURE.labels(model).set(temperature)

            risk, flagged = composite_risk(model, content, tools_provided, tool_calls)
            RISK.labels(model).set(risk)
            RISK_HIST.labels(model).observe(risk)
            if flagged:
                FLAGS.labels(model).inc()

            if span is not None:
                span.set_attribute(
                    "mlx.ttft_seconds",
                    (t_first_token - t_send) if t_first_token is not None else gen_time,
                )
                span.set_attribute("mlx.gen_seconds", gen_time)
                span.set_attribute("mlx.tokens_prompt", prompt_tokens)
                span.set_attribute("mlx.tokens_completion", comp_tokens)
                span.set_attribute("mlx.tool_calls", tool_calls)
                span.set_attribute("mlx.hallucination_risk", risk)
                span.set_attribute("mlx.hallucination_flagged", flagged)
                span.set_attribute("http.response.status_code", status)
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
                                    if t_first_token is not None
                                    else gen_time,
                                    4,
                                ),
                                "generation_seconds": round(gen_time, 4),
                                "hallucination_risk": risk,
                                "hallucination_flagged": flagged,
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
                                if t_first_token is not None
                                else gen_time,
                                4,
                            ),
                            "generation_seconds": round(gen_time, 4),
                            "hallucination_risk": risk,
                            "hallucination_flagged": flagged,
                        },
                    )
                    opik_span.end()
                except Exception as exc:
                    print(f"[proxy] opik span end failed ({exc!r})", flush=True)

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
                    print(f"[proxy] opik trace end failed ({exc!r})", flush=True)
                # In-band feedback: the proxy's heuristics become Opik feedback
                # scores (higher is better). These go through the same streamer
                # and are flushed with the final _OPIK.flush() below.
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
                    except Exception as exc:
                        print(
                            f"[proxy] opik feedback scores failed ({exc!r})",
                            flush=True,
                        )

        except (socket.error, http.client.HTTPException, ConnectionRefusedError) as exc:
            UPSTREAM_UP.set(0)
            ERRORS.labels(model, "5xx").inc()
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

    # -- handler hooks -----------------------------------------------------
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
    """Keep mlx_up honest even when idle."""
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
    global _OTEL, _OPIK, _OPIK_OTLP, _NODE_NAME, _CTX_MAX_TOKENS, MODEL_DEFAULT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", default="0.0.0.0:8080", help="proxy bind address")
    ap.add_argument("--upstream", default="127.0.0.1:8081", help="mlx_lm server")
    ap.add_argument(
        "--default-temp",
        type=float,
        default=float(os.environ.get("MLX_DEFAULT_TEMP", 0.0)),
        help="fallback temperature when a request omits it (default: $MLX_DEFAULT_TEMP)",
    )
    ap.add_argument("--metrics-path", default="/metrics")
    ap.add_argument(
        "--model",
        default=MODEL_DEFAULT,
        help="model id for context/KV gauges and the per-request fallback label "
        "(default: $MLX_MODEL env var, or the last-known-good literal)",
    )
    ap.add_argument(
        "--otlp-endpoint",
        default=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
        help="OTLP HTTP endpoint (e.g. http://192.168.1.10:30318). "
        "Enables traces/metrics/logs export via OpenTelemetry.",
    )
    ap.add_argument(
        "--node-name",
        default=os.environ.get("MLX_NODE_NAME", ""),
        help="node/rank label attached to OTel resources (e.g. rank0)",
    )
    ap.add_argument(
        "--opik-endpoint",
        default=os.environ.get("OPIK_ENDPOINT", ""),
        help="Opik self-hosted base URL (e.g. http://192.168.1.10:32173). "
        "Enables opik-SDK tracing of chat completions into the 'mlx' project. "
        "A trailing '/api' is appended automatically when missing.",
    )
    ap.add_argument(
        "--opik-otlp-endpoint",
        default=os.environ.get("OPIK_OTLP_ENDPOINT", ""),
        help="Opik OTLP HTTP endpoint (e.g. "
        "http://192.168.1.10:32173/api/v1/private/otel). Adds OpenInference "
        "spans exported to Opik's OTLP ingestion.",
    )
    args = ap.parse_args()

    host, _, port = args.listen.rpartition(":")
    up_host, _, up_port = args.upstream.rpartition(":")
    Proxy.upstream = (up_host or "127.0.0.1", int(up_port or 8081))
    Proxy.default_temp = args.default_temp
    Proxy.metrics_path = args.metrics_path

    MODEL_DEFAULT = args.model
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
            print(
                f"[proxy] opik SDK -> {opik_host} (project=mlx)",
                flush=True,
            )
        except Exception as exc:
            print(f"[proxy] opik SDK init failed ({exc!r})", flush=True)
            _OPIK = None
    if args.opik_otlp_endpoint:
        _OPIK_OTLP = True
        print(
            f"[proxy] opik OTLP -> {args.opik_otlp_endpoint} (project=mlx)",
            flush=True,
        )

    # Model context/KV gauges (static once known).
    try:
        info = _model_info(MODEL_DEFAULT) if _model_info else None
        if info:
            _CTX_MAX_TOKENS = info.max_context_tokens
            CONTEXT_MAX.labels(model=MODEL_DEFAULT).set(info.max_context_tokens)
            KV_BYTES_PER_TOKEN.labels(model=MODEL_DEFAULT).set(info.kv_bytes_per_token)
            print(
                f"[proxy] model ctx={info.max_context_tokens} kv={info.kv_bytes_per_token} B/tok",
                flush=True,
            )
    except Exception as exc:
        print(f"[proxy] model info failed ({exc!r})", flush=True)

    threading.Thread(
        target=_health_watch, args=(Proxy.upstream,), daemon=True
    ).start()

    server = ThreadingHTTPServer((host or "0.0.0.0", int(port or 8080)), Proxy)
    print(
        f"[proxy] listening on {host or '0.0.0.0'}:{port or 8080} "
        f"-> upstream {Proxy.upstream[0]}:{Proxy.upstream[1]} "
        f"(metrics at {args.metrics_path})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
