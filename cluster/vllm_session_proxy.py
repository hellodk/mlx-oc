#!/usr/bin/env python3
"""
vllm_session_proxy.py
=====================
Session-affinity load-balancing proxy for the two vLLM (vllm-metal) nodes.

Splits test requests across backends *by session*: every request that carries
the same session id (X-Session-Id header, X-Conversation-Id header, or a
`session` / `session_id` field in the JSON body) is routed to the same backend,
so a conversation's KV cache stays warm on one node. Different sessions are
spread across both nodes by hashing the session id.

Usage:
  python3 vllm_session_proxy.py [--port 8080] [--backends URL,URL]
      --backends defaults to "http://127.0.0.1:8081,http://192.168.2.2:8081"

The proxy exposes /metrics with per-backend request counters so a benchmark
run can show the session split actually happened.
"""

import argparse
import hashlib
import http.client
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_START = time.time()
_LOCK = threading.Lock()
_COUNTS = {}        # backend_url -> total requests
_SESSIONS = {}      # session_id -> (first_seen, requests)


def _extract_session(body, header_session):
    if header_session:
        return header_session
    if not body:
        return None
    try:
        data = json.loads(body)
        for key in ("session", "session_id", "conversation_id"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
    except (ValueError, AttributeError):
        pass
    return None


class _CTX:
    backends = []   # list of (url_string, (host, port))


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/metrics"):
            self._send_metrics()
            return
        self._forward(b"")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._forward(body)

    def _send_metrics(self):
        lines = [
            "# HELP vllm_session_proxy_reqs_total requests forwarded per backend",
            "# TYPE vllm_session_proxy_reqs_total counter",
            "# HELP vllm_session_proxy_sessions active sessions",
            "# TYPE vllm_session_proxy_sessions gauge",
            "# HELP vllm_session_proxy_session_first_seen first request timestamp per session",
            "# TYPE vllm_session_proxy_session_first_seen gauge",
            "# HELP vllm_session_proxy_session_requests requests per session",
            "# TYPE vllm_session_proxy_session_requests gauge",
            "# HELP vllm_session_proxy_up proxy is up",
            "# TYPE vllm_session_proxy_up gauge",
            "# HELP vllm_session_proxy_uptime_seconds proxy uptime",
            "# TYPE vllm_session_proxy_uptime_seconds gauge",
        ]
        for url, n in sorted(_COUNTS.items()):
            lines.append(f'vllm_session_proxy_reqs_total{{backend="{url}"}} {n}')
        for sid, (first, n) in sorted(_SESSIONS.items()):
            lines.append(f'vllm_session_proxy_sessions{{session="{sid}"}} 1')
            lines.append(f'vllm_session_proxy_session_first_seen{{session="{sid}"}} {first:.0f}')
            lines.append(f'vllm_session_proxy_session_requests{{session="{sid}"}} {n}')
        lines.append("vllm_session_proxy_up 1")
        lines.append(f"vllm_session_proxy_uptime_seconds {time.time() - _START:.0f}")
        payload = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _forward(self, body):
        header_session = self.headers.get("X-Session-Id") or self.headers.get(
            "X-Conversation-Id"
        )
        session = _extract_session(body, header_session)
        if not session:
            session = f"client:{self.client_address[0]}"
        idx = int(hashlib.sha256(session.encode()).hexdigest()[:8], 16)
        url, (host, port) = _CTX.backends[idx % len(_CTX.backends)]
        with _LOCK:
            _COUNTS[url] = _COUNTS.get(url, 0) + 1
            first, n = _SESSIONS.get(session, (time.time(), 0))
            _SESSIONS[session] = (first, n + 1)

        path = self.path or "/"
        headers = {k: v for k, v in self.headers.items()}
        headers.pop("Host", None)
        headers.pop("Connection", None)
        headers["Connection"] = "close"
        headers["X-Forwarded-For"] = self.client_address[0]
        conn = http.client.HTTPConnection(host, port, timeout=300)
        try:
            conn.request(self.command, path, body=body, headers=headers)
            resp = conn.getresponse()
            self.send_response(resp.status)
            ctype = resp.getheader("Content-Type")
            if ctype:
                self.send_header("Content-Type", ctype)
            self.send_header("Connection", "close")
            self.send_header("X-VLLM-Backend", f"http://{host}:{port}")
            self.send_header("X-Session-Backend", f"{session}->{host}:{port}")
            self.end_headers()
            while True:
                line = resp.readline(65536)
                if not line:
                    break
                self.wfile.write(line)
        except Exception as exc:
            try:
                payload = json.dumps({
                    "error": {"message": f"proxy upstream error: {exc}",
                              "type": "proxy_error"},
                }).encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except Exception:
                pass
        finally:
            conn.close()


def parse_backend(url):
    p = urllib.parse.urlsplit(url)
    return (url, (p.hostname, p.port or 8081))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--backends", default="http://127.0.0.1:8081,http://192.168.2.2:8081",
                    help="comma-separated vLLM base URLs")
    args = ap.parse_args()
    _CTX.backends = [parse_backend(u) for u in args.backends.split(",")]
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"[session-proxy] listening on {args.bind}:{args.port} "
          f"backends={args.backends}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
