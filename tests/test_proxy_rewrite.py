#!/usr/bin/env python3
"""
End-to-end tests for the proxy's logprobs request/response rewriting.

A stub upstream stands in for mlx_lm.server: it mirrors the real server's
behaviour (logprobs on non-streaming responses only) and records what the
proxy actually sent it. The proxy runs in-process on an ephemeral port and is
driven over real HTTP, so the header/Content-Length/SSE handling is exercised
for real rather than mocked.

What must hold:
  * a client that never asked for logprobs never sees them
  * a de-streamed request still arrives as a well-formed SSE stream
  * tool calls survive the round trip
  * an upstream error is passed through untouched

Run:
  pytest tests/test_proxy_rewrite.py -q
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cluster"))

import mlx_metrics_proxy as p  # noqa: E402

MODEL = "test/model-1bit"
UPSTREAM_SEEN = []      # request bodies the stub upstream received
UPSTREAM_MODE = {"tool_calls": False, "status": 200}


def _logprobs_block(n_top):
    """Mirror mlx_lm.server's logprobs shape."""
    toks = [("Hello", -0.1), (" world", -1.2)]
    return {
        "content": [
            {
                "id": i,
                "token": tok,
                "logprob": lp,
                "top_logprobs": [
                    {"id": i, "token": tok, "logprob": lp},
                    {"id": 99, "token": "alt", "logprob": lp - 1.0},
                ][:n_top],
            }
            for i, (tok, lp) in enumerate(toks)
        ]
    }


class StubUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"object": "list", "data": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        req = json.loads(raw or b"{}")
        UPSTREAM_SEEN.append(req)

        if UPSTREAM_MODE["status"] >= 400:
            body = json.dumps({"error": "upstream said no"}).encode()
            self.send_response(UPSTREAM_MODE["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if req.get("stream"):
            # Real mlx_lm.server never puts logprobs in SSE chunks.
            # mlx_lm.server closes the connection at the end of a stream -
            # there is no Content-Length, so that close IS the framing.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            for piece in ("Hello", " world"):
                chunk = {
                    "id": "chatcmpl-stub", "object": "chat.completion.chunk",
                    "created": 1, "model": MODEL,
                    "choices": [{"index": 0, "finish_reason": None,
                                 "delta": {"role": "assistant",
                                           "content": piece}}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            done = {
                "id": "chatcmpl-stub", "object": "chat.completion.chunk",
                "created": 1, "model": MODEL,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "delta": {"role": "assistant"}}],
            }
            self.wfile.write(("data: " + json.dumps(done) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        msg = {"role": "assistant", "content": "Hello world"}
        finish = "stop"
        if UPSTREAM_MODE["tool_calls"]:
            msg["tool_calls"] = [{
                "id": "call_1", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }]
            finish = "tool_calls"
        choice = {"index": 0, "finish_reason": finish, "message": msg}
        if req.get("logprobs"):
            choice["logprobs"] = _logprobs_block(req.get("top_logprobs") or 1)
        body = json.dumps({
            "id": "chatcmpl-stub", "object": "chat.completion", "created": 1,
            "model": MODEL, "system_fingerprint": "stub",
            "choices": [choice],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                      "total_tokens": 7},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def stack():
    """Stub upstream + proxy, both on ephemeral ports."""
    up = ThreadingHTTPServer(("127.0.0.1", 0), StubUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()

    p.Proxy.upstream = ("127.0.0.1", up.server_address[1])
    p.Proxy.metrics_path = "/metrics"
    p.Proxy.logprobs_top = 2
    p.Proxy.stream_sample = 0.0
    p.Proxy.low_conf_threshold = 0.5

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), p.Proxy)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{proxy.server_address[1]}"
    yield base
    proxy.shutdown()
    up.shutdown()


def post(base, payload, timeout=10):
    req = urllib.request.Request(
        base + "/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Mlx-Trace": "0"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers, r.read()


def metrics(base):
    with urllib.request.urlopen(base + "/metrics", timeout=10) as r:
        return r.read().decode()


def series(text, name):
    """Sum of every sample of `name` in a Prometheus text exposition."""
    total = 0.0
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        head, _, value = line.rpartition(" ")
        if head.split("{", 1)[0] == name:
            total += float(value)
    return total


@pytest.fixture(autouse=True)
def reset():
    UPSTREAM_SEEN.clear()
    UPSTREAM_MODE.update(tool_calls=False, status=200)
    p.Proxy.stream_sample = 0.0
    p.Proxy.logprobs_top = 2


# -- injection on non-streaming requests -----------------------------------
def test_non_streaming_request_gets_logprobs_injected_and_stripped(stack):
    before = series(metrics(stack), "mlx_confidence_scored_total")
    status, headers, body = post(stack, {
        "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    # upstream was asked for logprobs...
    assert UPSTREAM_SEEN[-1]["logprobs"] is True
    assert UPSTREAM_SEEN[-1]["top_logprobs"] == 2
    # ...but the client never sees them
    obj = json.loads(body)
    assert "logprobs" not in obj["choices"][0]
    assert obj["choices"][0]["message"]["content"] == "Hello world"
    # Content-Length must match the rewritten body, not the upstream one
    assert int(headers["Content-Length"]) == len(body)
    # and the request was scored
    after = metrics(stack)
    assert series(after, "mlx_confidence_scored_total") == before + 1
    assert 'source="injected"' in after
    assert series(after, "mlx_output_perplexity") > 1.0


def test_client_requested_logprobs_are_passed_through(stack):
    status, _, body = post(stack, {
        "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
        "logprobs": True, "top_logprobs": 2,
    })
    assert status == 200
    obj = json.loads(body)
    # the caller asked, so the caller keeps them
    assert "logprobs" in obj["choices"][0]
    assert 'source="client"' in metrics(stack)


def test_injection_can_be_disabled(stack):
    p.Proxy.logprobs_top = 0
    status, _, body = post(stack, {
        "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
    })
    assert status == 200
    assert "logprobs" not in UPSTREAM_SEEN[-1]
    assert "logprobs" not in json.loads(body)["choices"][0]


def test_tool_calls_survive_the_strip(stack):
    UPSTREAM_MODE["tool_calls"] = True
    status, _, body = post(stack, {
        "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
    })
    obj = json.loads(body)
    assert obj["choices"][0]["message"]["tool_calls"][0]["id"] == "call_1"
    assert obj["choices"][0]["finish_reason"] == "tool_calls"
    assert "logprobs" not in obj["choices"][0]


def test_upstream_error_is_passed_through_untouched(stack):
    UPSTREAM_MODE["status"] = 503
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(stack, {"model": MODEL,
                     "messages": [{"role": "user", "content": "hi"}]})
    assert exc.value.code == 503
    assert json.loads(exc.value.read())["error"] == "upstream said no"


# -- streaming -------------------------------------------------------------
def test_streaming_is_untouched_when_sampling_is_off(stack):
    status, headers, body = post(stack, {
        "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    # upstream still got stream=true and no logprobs
    assert UPSTREAM_SEEN[-1]["stream"] is True
    assert "logprobs" not in UPSTREAM_SEEN[-1]
    assert body.count(b"data: ") == 4        # 2 content + 1 final + [DONE]
    assert body.endswith(b"data: [DONE]\n\n")


def test_destreamed_request_still_looks_like_a_stream(stack):
    p.Proxy.stream_sample = 1.0
    before = series(metrics(stack), "mlx_confidence_scored_total")
    status, headers, body = post(stack, {
        "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert "Content-Length" not in headers
    # upstream saw a non-streaming request with logprobs
    assert UPSTREAM_SEEN[-1]["stream"] is False
    assert UPSTREAM_SEEN[-1]["logprobs"] is True
    # the client still got a parseable stream with the same content
    parser = p._StreamParser()
    parser.feed(body.decode())
    assert parser.content == "Hello world"
    assert parser.finish_reason == "stop"
    assert body.endswith(b"data: [DONE]\n\n")
    # and it was scored
    after = metrics(stack)
    assert series(after, "mlx_confidence_scored_total") == before + 1
    assert 'source="destreamed"' in after


def test_destreamed_tool_calls_reach_the_client(stack):
    p.Proxy.stream_sample = 1.0
    UPSTREAM_MODE["tool_calls"] = True
    status, _, body = post(stack, {
        "model": MODEL, "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    })
    assert status == 200
    parser = p._StreamParser()
    parser.feed(body.decode())
    assert parser.tool_calls == 1
    assert parser.finish_reason == "tool_calls"


def test_destreaming_does_not_record_a_ttft(stack):
    """A de-streamed request has no first-token moment - it must not fake one."""
    p.Proxy.stream_sample = 1.0
    before = series(metrics(stack), "mlx_ttft_seconds_count")
    post(stack, {"model": MODEL, "stream": True,
                 "messages": [{"role": "user", "content": "hi"}]})
    assert series(metrics(stack), "mlx_ttft_seconds_count") == before
