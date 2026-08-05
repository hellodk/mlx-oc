#!/usr/bin/env python3
"""HTTP front end for k8s-drift.

Serves a single-page drift dashboard at / and a small JSON API:

  GET  /                      -> web/index.html
  GET  /api/config            -> sources, kinds, namespaces, reveal permission
  GET  /api/summary           -> per-kind drift counts
  GET  /api/drift?kind=..&ns=..&reveal=1  -> rows for one kind
  POST /api/refresh           -> re-fetch both clusters

Cluster sources are kubectl contexts or snapshot JSON files. With only one
cluster available, point both flags at the same context/snapshot: the result
is zero drift, which doubles as a smoke test. Pass --cluster-b a snapshot
(see snapshot.py) or a second context to compare real environments.
"""

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import drift

DEFAULT_PORT = 8090
CACHE_TTL = 30
WEB_DIR = "web"

_state = {
    "lock": threading.Lock(),
    "a": None, "b": None,
    "a_state": None, "b_state": None,
    "a_revealed": {}, "b_revealed": {},
    "report": None, "namespaces": [],
    "cached_at": 0.0,
    "source_a": "", "source_b": "",
    "reveal_allowed": True,
}


def _load(cluster_a, cluster_b, kubeconfig, reveal_allowed):
    sa = drift.fetch_cluster(cluster_a, kubeconfig, reveal=reveal_allowed)
    sb = drift.fetch_cluster(cluster_b, kubeconfig, reveal=reveal_allowed)
    with _state["lock"]:
        _state.update(
            a=cluster_a, b=cluster_b,
            a_state=sa["state"], b_state=sb["state"],
            a_revealed=sa["revealed"], b_revealed=sb["revealed"],
            report=drift.compare(sa["state"], sb["state"]),
            namespaces=drift.namespaces(sa["state"], sb["state"]),
            cached_at=time.time(), reveal_allowed=reveal_allowed,
            source_a=cluster_a, source_b=cluster_b,
        )


def _get(key):
    with _state["lock"]:
        return _state[key]


def _fresh(cluster_a, cluster_b, kubeconfig, reveal_allowed, force=False):
    if force or time.time() - _get("cached_at") > CACHE_TTL:
        try:
            _load(cluster_a, cluster_b, kubeconfig, reveal_allowed)
        except drift.DriftError:
            if _get("report") is None:
                raise
    return _get("report")


class Handler(BaseHTTPRequestHandler):
    server_version = "k8s-drift/1.0"

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[http] {self.client_address[0]} {fmt % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            try:
                with open(_web_path("index.html"), encoding="utf-8") as fh:
                    return self._html(fh.read().encode())
            except OSError:
                return self._json({"error": "web/index.html not found"}, 500)
        if path == "/api/config":
            return self._json({
                "source_a": _get("source_a"), "source_b": _get("source_b"),
                "cluster_a": _get("a"), "cluster_b": _get("b"),
                "kinds": drift.KINDS, "kind_label": drift.KIND_LABEL,
                "namespaces": _get("namespaces"),
                "reveal_allowed": _get("reveal_allowed"),
                "cached_at": _get("cached_at"),
                "same_cluster": _get("a") == _get("b"),
            })
        if path == "/api/summary":
            report = _fresh(*self.server.cfg)
            return self._json({
                "counts": report["counts"], "cached_at": _get("cached_at"),
            })
        if path == "/api/drift":
            kind = query.get("kind", ["deployments"])[0]
            ns = query.get("ns", [""])[0]
            reveal = query.get("reveal", ["0"])[0] == "1"
            report = _fresh(*self.server.cfg)
            if kind not in report["kinds"]:
                return self._json({"error": f"unknown kind {kind}"}, 400)
            rows = report["kinds"][kind]
            if ns:
                rows = {k: v for k, v in rows.items() if v["namespace"] == ns}
            payload = rows
            if kind == "secrets" and reveal:
                payload = {}
                for key, row in rows.items():
                    row = dict(row)
                    row["a_values"] = _get("a_revealed").get((row["namespace"], row["name"]))
                    row["b_values"] = _get("b_revealed").get((row["namespace"], row["name"]))
                    payload[key] = row
            return self._json({"kind": kind, "rows": payload,
                               "cached_at": _get("cached_at")})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path == "/api/refresh":
            try:
                _fresh(*self.server.cfg, force=True)
                return self._json({"ok": True, "cached_at": _get("cached_at")})
            except drift.DriftError as exc:
                return self._json({"error": str(exc)}, 502)
        return self._json({"error": "not found"}, 404)


def _web_path(name):
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), WEB_DIR, name)


def main():
    parser = argparse.ArgumentParser(description="Kubernetes drift-detection web app")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--cluster-a", default=None,
                        help="kubectl context or snapshot file (default: current context)")
    parser.add_argument("--cluster-b", default=None,
                        help="kubectl context or snapshot file (default: same as --cluster-a)")
    parser.add_argument("--kubeconfig", default=None)
    parser.add_argument("--no-reveal", action="store_true",
                        help="forbid returning secret values in the API")
    args = parser.parse_args()

    cluster_a = args.cluster_a or drift.current_context(args.kubeconfig)
    cluster_b = args.cluster_b or cluster_a
    reveal_allowed = not args.no_reveal

    _load(cluster_a, cluster_b, args.kubeconfig, reveal_allowed)
    report = _get("report")
    total = sum(c["total"] for c in report["counts"].values())
    diffs = sum(c["diff"] for c in report["counts"].values())
    print(f"[k8s-drift] {_get('source_a')} vs {_get('source_b')}: "
          f"{total} resources, {diffs} drifting")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.cfg = (cluster_a, cluster_b, args.kubeconfig, reveal_allowed)
    print(f"[k8s-drift] listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[k8s-drift] bye")


if __name__ == "__main__":
    main()
