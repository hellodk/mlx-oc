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

Metrics are served in Prometheus text format on /metrics.

Optional OpenTelemetry (--otlp-endpoint): each request becomes a trace span,
a log record, and OTLP metric points exported to an OTLP HTTP/gRPC gateway
(e.g. the in-cluster otel-gateway). Disabled unless --otlp-endpoint is set.

Usage:
  mlx_metrics_proxy.py --listen 0.0.0.0:8080 --upstream 127.0.0.1:8081
  mlx_metrics_proxy.py --otlp-endpoint http://192.168.1.10:30318 --node-name rank0
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

MODEL_DEFAULT = "mlx-community/Qwen3-1.7B-4bit"

# --------------------------------------------------------------------------
# OpenTelemetry (optional)
# --------------------------------------------------------------------------
_OTEL = None  # set by _setup_otel(); None == OTel disabled


def _setup_otel(endpoint, node_name, service_name="mlx-metrics-proxy"):
    """Best-effort OTel init. Returns an OTel helper or None on failure."""
    if not endpoint:
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
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
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
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(tp)

        # -- metrics -----------------------------------------------------
        mp = MeterProvider(
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=endpoint), export_interval_millis=15000
                )
            ],
            resource=resource,
        )
        metrics.set_meter_provider(mp)

        # -- logs --------------------------------------------------------
        lp = LoggerProvider(resource=resource)
        lp.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
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
            meter = mp.get_meter("mlx.proxy")

            req_counter = meter.create_counter(
                "mlx.requests", unit="1", description="requests served"
            )
            token_counter = meter.create_counter(
                "mlx.tokens.completion", unit="1", description="completion tokens"
            )
            tool_counter = meter.create_counter(
                "mlx.tool_calls", unit="1", description="tool calls"
            )
            ttft_hist = meter.create_histogram(
                "mlx.ttft.seconds", unit="s", description="time to first token"
            )
            rate_hist = meter.create_histogram(
                "mlx.token_rate.tps", unit="1", description="tokens per second"
            )
            risk_hist = meter.create_histogram(
                "mlx.hallucination.risk", unit="1", description="heuristic risk"
            )
            temp_gauge = meter.create_up_down_counter(
                "mlx.temperature", unit="1", description="sampling temperature"
            )
            risk_gauge = meter.create_up_down_counter(
                "mlx.hallucination.risk.last", unit="1", description="last risk"
            )

            @staticmethod
            def record(model, streaming, ttft, gen_time, comp_tokens, rate,
                       temperature, risk, tool_calls, status, flagged):
                attrs = {"model": model}
                Otel.req_counter.add(1, {**attrs, "streaming": str(streaming).lower()})
                Otel.token_counter.add(comp_tokens, attrs)
                Otel.tool_counter.add(tool_calls, attrs)
                if ttft is not None:
                    Otel.ttft_hist.record(ttft, attrs)
                if rate is not None:
                    Otel.rate_hist.record(rate, attrs)
                Otel.risk_hist.record(risk, attrs)
                Otel.temp_gauge.add(0, {**attrs, "value": str(temperature)})
                Otel.risk_gauge.add(0, {**attrs, "value": str(risk)})

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
H_REPET = Gauge("mlx_heuristic_repetition", "Most-repeated word-trigram ratio (0..1)")
H_REFUSAL = Gauge("mlx_heuristic_refusal", "1 if a refusal pattern was detected")
H_HEDGE = Gauge("mlx_heuristic_hedging", "Normalized hedge-word density (0..1)")
H_UNGROUNDED = Gauge(
    "mlx_heuristic_ungrounded",
    "1 if factual-looking answer was produced without any tool call",
)
UPSTREAM_UP = Gauge(
    "mlx_up",
    "1 if the upstream mlx_lm server is reachable",
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


def composite_risk(content: str, tools_provided: bool, tool_calls: int) -> tuple:
    rep = heuristic_repetition(content)
    ref = heuristic_refusal(content)
    hed = heuristic_hedging(content)
    ung = heuristic_ungrounded(content, tools_provided, tool_calls)
    risk = min(1.0, 0.5 * rep + 0.9 * ref + 0.4 * hed + 0.7 * ung)
    H_REPET.set(rep)
    H_REFUSAL.set(ref)
    H_HEDGE.set(hed)
    H_UNGROUNDED.set(ung)
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
        if self.command == "POST" and req_path.endswith("/v1/chat/completions"):
            try:
                req = json.loads(body or b"{}")
                model = req.get("model", model)
                streaming = bool(req.get("stream", False))
                temperature = req.get("temperature", self.default_temp)
                tools_provided = bool(req.get("tools"))
            except json.JSONDecodeError:
                pass

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
        try:
            conn.request(self.command, req_path, body=body, headers=headers)
            resp = conn.getresponse()
            UPSTREAM_UP.set(1)

            status = resp.status
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
                chunk = resp.read(65536)
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
            else:
                content = ""
                tool_calls = 0
                usage = None
                try:
                    obj = json.loads(b"".join(content_bytes).decode("utf-8", "replace"))
                    msg = (obj.get("choices") or [{}])[0].get("message") or {}
                    content = msg.get("content") or ""
                    tool_calls = len(msg.get("tool_calls") or [])
                    usage = obj.get("usage")
                except json.JSONDecodeError:
                    pass

            if usage:
                TOKENS_PROMPT.labels(model).inc(int(usage.get("prompt_tokens", 0)))
                comp_tokens = int(usage.get("completion_tokens", 0))
            else:
                comp_tokens = len(content.split())
            TOKENS_COMPLETION.labels(model).inc(comp_tokens)

            gen_time = t_done - t_send
            GEN_TIME.labels(model).observe(gen_time)
            if t_first_token is not None:
                TTFT.labels(model).observe(t_first_token - t_send)
            else:
                TTFT.labels(model).observe(gen_time)

            if comp_tokens > 0 and gen_time > 0:
                TOKEN_RATE.labels(model).observe(comp_tokens / gen_time)

            REQUESTS.labels(model, "true" if streaming else "false").inc()
            TOOL_CALLS.labels(model).inc(tool_calls)
            TEMPERATURE.labels(model).set(temperature)

            risk, flagged = composite_risk(content, tools_provided, tool_calls)
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
                span.set_attribute("mlx.tokens_prompt",
                                   int(usage.get("prompt_tokens", 0)) if usage else 0)
                span.set_attribute("mlx.tokens_completion", comp_tokens)
                span.set_attribute("mlx.tool_calls", tool_calls)
                span.set_attribute("mlx.hallucination_risk", risk)
                span.set_attribute("mlx.hallucination_flagged", flagged)
                span.set_attribute("http.response.status_code", status)
                span.set_status("OK")
                _OTEL.record(
                    model, streaming,
                    t_first_token - t_send if t_first_token else None,
                    gen_time, comp_tokens,
                    comp_tokens / gen_time if comp_tokens > 0 and gen_time > 0 else None,
                    temperature, risk, tool_calls, status, flagged,
                )
                span.end()

        except (socket.error, http.client.HTTPException, ConnectionRefusedError) as exc:
            UPSTREAM_UP.set(0)
            if span is not None:
                span.set_attribute("error.type", type(exc).__name__)
                span.set_attribute("http.response.status_code", 503)
                span.set_status("ERROR", str(exc))
                span.end()
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
            if span is not None:
                try:
                    span.end()
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
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen", default="0.0.0.0:8080", help="proxy bind address")
    ap.add_argument("--upstream", default="127.0.0.1:8081", help="mlx_lm server")
    ap.add_argument("--default-temp", type=float, default=0.0)
    ap.add_argument("--metrics-path", default="/metrics")
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
    args = ap.parse_args()

    host, _, port = args.listen.rpartition(":")
    up_host, _, up_port = args.upstream.rpartition(":")
    Proxy.upstream = (up_host or "127.0.0.1", int(up_port or 8081))
    Proxy.default_temp = args.default_temp
    Proxy.metrics_path = args.metrics_path

    global _OTEL
    _OTEL = _setup_otel(args.otlp_endpoint, args.node_name)

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
