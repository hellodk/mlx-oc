#!/usr/bin/env python3
"""Lightweight HTTP ingest receiver for full process snapshots and CycloneDX SBOMs.

Stores kri-compatible process telemetry (top 200 processes with cmdline, is_llm,
threads, status, username, RSS/CPU/IO) and CycloneDX SBOM components (purl,
type, licenses, license_category) into PostgreSQL (`telemetry-db`) using only
the Python 3.12 standard library so Grafana's built-in postgres datasource can
query them directly without external plugins or custom Docker images.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PG_HOST = os.environ.get("TELEMETRY_DB_HOST", "telemetry-db")
PG_PORT = int(os.environ.get("TELEMETRY_DB_PORT", "5432"))
PG_USER = os.environ.get("TELEMETRY_DB_USER", "telemetry")
PG_DB = os.environ.get("TELEMETRY_DB_NAME", "telemetry")
LISTEN_HOST = os.environ.get("INGEST_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("INGEST_LISTEN_PORT", "8088"))

COPYLEFT_LICENSES = frozenset({
    "GPL-2.0", "GPL-2.0-only", "GPL-2.0-or-later",
    "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.0", "LGPL-2.0-only", "LGPL-2.0-or-later",
    "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later",
    "LGPL-3.0", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later",
    "EUPL-1.1", "EUPL-1.2", "OSL-3.0",
    "CC-BY-SA-4.0", "CC-BY-NC-SA-4.0",
})

PERMISSIVE_LICENSES = frozenset({
    "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
    "ISC", "Unlicense", "0BSD", "CC0-1.0", "PSF-2.0", "Python-2.0",
})


def classify_license(licenses: list[str] | str | None) -> str:
    """Classify license list into copyleft, permissive, or unknown."""
    if not licenses:
        return "unknown"
    items = [licenses] if isinstance(licenses, str) else list(licenses)
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return "unknown"
    for lic in cleaned:
        upper = lic.upper()
        if lic in COPYLEFT_LICENSES or "GPL" in upper or "AGPL" in upper:
            return "copyleft"
    for lic in cleaned:
        upper = lic.upper()
        if lic in PERMISSIVE_LICENSES or any(
            tok in upper for tok in ("MIT", "APACHE", "BSD", "ISC", "PSF", "UNLICENSE", "0BSD")
        ):
            return "permissive"
    return "other"


def _sql_str(val: Any) -> str:
    """Escape a Python value as a PostgreSQL literal."""
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val).replace("\x00", "")
    return "'" + s.replace("'", "''") + "'"


class _PgWireClient:
    """Minimal synchronous PostgreSQL v3 wire-protocol client (trust auth)."""

    def __init__(self, host: str, port: int, user: str, database: str, timeout: float = 10.0):
        self.host = host
        self.port = port
        self.user = user
        self.database = database
        self.timeout = timeout
        self.sock: socket.socket | None = None

    def __enter__(self) -> "_PgWireClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("PostgreSQL connection closed unexpectedly")
            buf.extend(chunk)
        return bytes(buf)

    def _read_msg(self) -> tuple[str, bytes]:
        hdr = self._recv_exact(5)
        mtype = chr(hdr[0])
        length = struct.unpack("!I", hdr[1:5])[0]
        payload = self._recv_exact(length - 4) if length > 4 else b""
        return mtype, payload

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        params = (
            b"user\x00" + self.user.encode("utf8") + b"\x00"
            + b"database\x00" + self.database.encode("utf8") + b"\x00\x00"
        )
        startup = struct.pack("!II", 8 + len(params), 196608) + params
        self.sock.sendall(startup)
        while True:
            mtype, payload = self._read_msg()
            if mtype == "R":
                auth_code = struct.unpack("!I", payload[:4])[0]
                if auth_code != 0:
                    raise RuntimeError(f"Unsupported PostgreSQL auth method: {auth_code}")
            elif mtype == "Z":
                break
            elif mtype == "E":
                raise RuntimeError(f"PostgreSQL startup error: {payload!r}")

    def execute(self, sql: str) -> None:
        assert self.sock is not None
        body = sql.encode("utf8") + b"\x00"
        msg = b"Q" + struct.pack("!I", 4 + len(body)) + body
        self.sock.sendall(msg)
        err_msg: str | None = None
        while True:
            mtype, payload = self._read_msg()
            if mtype == "E":
                err_msg = payload.decode("utf8", errors="replace")
            elif mtype == "Z":
                break
        if err_msg:
            raise RuntimeError(f"PostgreSQL query error: {err_msg}")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(b"X\x00\x00\x00\x04")
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS process_stats_latest (
    device TEXT NOT NULL,
    pid INTEGER NOT NULL,
    name TEXT NOT NULL,
    cmdline TEXT NOT NULL DEFAULT '',
    cpu_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    mem_rss_bytes BIGINT NOT NULL DEFAULT 0,
    mem_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    num_threads INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    io_read_bytes BIGINT,
    io_write_bytes BIGINT,
    is_llm BOOLEAN NOT NULL DEFAULT FALSE,
    collected_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (device, pid)
);
CREATE INDEX IF NOT EXISTS idx_proc_latest_device_rss
    ON process_stats_latest (device, mem_rss_bytes DESC);

CREATE TABLE IF NOT EXISTS process_stats_history (
    id BIGSERIAL PRIMARY KEY,
    device TEXT NOT NULL,
    collected_at TIMESTAMPTZ NOT NULL,
    pid INTEGER NOT NULL,
    name TEXT NOT NULL,
    cmdline TEXT NOT NULL DEFAULT '',
    cpu_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    mem_rss_bytes BIGINT NOT NULL DEFAULT 0,
    mem_pct DOUBLE PRECISION NOT NULL DEFAULT 0,
    num_threads INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    io_read_bytes BIGINT,
    io_write_bytes BIGINT,
    is_llm BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_proc_hist_device_time
    ON process_stats_history (device, collected_at DESC);

CREATE TABLE IF NOT EXISTS sbom_scans (
    id UUID PRIMARY KEY,
    device TEXT NOT NULL,
    scanned_at TIMESTAMPTZ NOT NULL,
    format TEXT NOT NULL DEFAULT 'cyclonedx',
    scanner TEXT NOT NULL DEFAULT 'native',
    total_packages INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sbom_scans_device_time
    ON sbom_scans (device, scanned_at DESC);

CREATE TABLE IF NOT EXISTS sbom_components (
    id BIGSERIAL PRIMARY KEY,
    scan_id UUID NOT NULL REFERENCES sbom_scans(id) ON DELETE CASCADE,
    device TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    ecosystem TEXT NOT NULL DEFAULT 'unknown',
    type TEXT NOT NULL DEFAULT 'library',
    purl TEXT NOT NULL DEFAULT '',
    licenses TEXT NOT NULL DEFAULT '',
    license_category TEXT NOT NULL DEFAULT 'unknown',
    path TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sbom_comp_scan
    ON sbom_components (scan_id);
CREATE INDEX IF NOT EXISTS idx_sbom_comp_device_eco
    ON sbom_components (device, ecosystem);
"""


def init_schema_with_retry(max_attempts: int = 30) -> None:
    """Wait for telemetry-db and apply idempotent schema DDL."""
    for attempt in range(1, max_attempts + 1):
        try:
            with _PgWireClient(PG_HOST, PG_PORT, PG_USER, PG_DB) as pg:
                pg.execute(SCHEMA_SQL)
            print(f"[telemetry-ingest] PostgreSQL schema ready on {PG_HOST}:{PG_PORT}/{PG_DB}", flush=True)
            return
        except Exception as exc:
            print(f"[telemetry-ingest] waiting for DB ({attempt}/{max_attempts}): {exc}", flush=True)
            time.sleep(2.0)
    raise RuntimeError("Failed to initialize PostgreSQL schema")


def ingest_process_stats(payload: dict[str, Any]) -> int:
    """Replace latest snapshot for device and append bounded history."""
    device = str(payload.get("device") or "unknown").strip() or "unknown"
    collected_at = str(
        payload.get("collected_at") or datetime.now(timezone.utc).isoformat()
    )
    processes = payload.get("processes") or []
    if not isinstance(processes, list):
        raise ValueError("processes must be a list")

    latest_rows: list[str] = []
    hist_rows: list[str] = []
    for p in processes[:250]:
        if not isinstance(p, dict) or p.get("pid") is None:
            continue
        pid = int(p["pid"])
        name = str(p.get("name") or "")
        cmdline = str(p.get("cmdline") or "")[:1024]
        cpu_pct = float(p.get("cpu_pct") or 0.0)
        mem_rss = int(p.get("mem_rss_bytes") or 0)
        mem_pct = float(p.get("mem_pct") or 0.0)
        num_threads = int(p.get("num_threads") or 0)
        status = str(p.get("status") or "")
        username = str(p.get("username") or "")
        io_read = int(p["io_read_bytes"]) if p.get("io_read_bytes") is not None else None
        io_write = int(p["io_write_bytes"]) if p.get("io_write_bytes") is not None else None
        is_llm = bool(p.get("is_llm"))

        row_vals = (
            f"({_sql_str(device)}, {pid}, {_sql_str(name)}, {_sql_str(cmdline)}, "
            f"{cpu_pct}, {mem_rss}, {mem_pct}, {num_threads}, {_sql_str(status)}, "
            f"{_sql_str(username)}, {_sql_str(io_read)}, {_sql_str(io_write)}, "
            f"{_sql_str(is_llm)}, {_sql_str(collected_at)}::timestamptz)"
        )
        latest_rows.append(row_vals)
        hist_rows.append(
            f"({_sql_str(device)}, {_sql_str(collected_at)}::timestamptz, {pid}, "
            f"{_sql_str(name)}, {_sql_str(cmdline)}, {cpu_pct}, {mem_rss}, {mem_pct}, "
            f"{num_threads}, {_sql_str(status)}, {_sql_str(username)}, "
            f"{_sql_str(io_read)}, {_sql_str(io_write)}, {_sql_str(is_llm)})"
        )

    stmts = [
        "BEGIN",
        f"DELETE FROM process_stats_latest WHERE device = {_sql_str(device)}",
    ]
    if latest_rows:
        stmts.append(
            "INSERT INTO process_stats_latest "
            "(device, pid, name, cmdline, cpu_pct, mem_rss_bytes, mem_pct, "
            "num_threads, status, username, io_read_bytes, io_write_bytes, is_llm, collected_at) "
            "VALUES " + ", ".join(latest_rows)
        )
        stmts.append(
            "INSERT INTO process_stats_history "
            "(device, collected_at, pid, name, cmdline, cpu_pct, mem_rss_bytes, mem_pct, "
            "num_threads, status, username, io_read_bytes, io_write_bytes, is_llm) "
            "VALUES " + ", ".join(hist_rows)
        )
    stmts.append(
        f"DELETE FROM process_stats_history WHERE device = {_sql_str(device)} "
        "AND collected_at < NOW() - INTERVAL '24 hours'"
    )
    stmts.append("COMMIT")

    with _PgWireClient(PG_HOST, PG_PORT, PG_USER, PG_DB) as pg:
        pg.execute(";\n".join(stmts) + ";")
    return len(latest_rows)


def _extract_components(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept either CycloneDX JSON (`components`) or legacy `packages` list."""
    raw_components = payload.get("components")
    if isinstance(raw_components, list) and raw_components:
        out = []
        for c in raw_components:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            raw_lics = c.get("licenses") or []
            lic_names: list[str] = []
            if isinstance(raw_lics, list):
                for entry in raw_lics:
                    if isinstance(entry, str):
                        lic_names.append(entry)
                    elif isinstance(entry, dict):
                        lic_obj = entry.get("license") if isinstance(entry.get("license"), dict) else entry
                        lid = lic_obj.get("id") or lic_obj.get("name") or ""
                        if lid:
                            lic_names.append(str(lid))
            elif isinstance(raw_lics, str):
                lic_names.append(raw_lics)

            props = {
                str(p.get("name")): str(p.get("value", ""))
                for p in (c.get("properties") or [])
                if isinstance(p, dict) and p.get("name")
            }
            purl = str(c.get("purl") or "")
            eco = props.get("ecosystem") or (
                purl.split(":", 2)[1].split("/", 1)[0] if purl.startswith("pkg:") and "/" in purl else "unknown"
            )
            out.append({
                "name": str(c["name"]),
                "version": str(c.get("version") or ""),
                "ecosystem": eco,
                "type": str(c.get("type") or "library"),
                "purl": purl,
                "licenses": ", ".join(lic_names),
                "license_category": classify_license(lic_names),
                "path": props.get("path", ""),
                "hash": props.get("hash", ""),
            })
        return out

    packages = payload.get("packages") or []
    out = []
    for pkg in packages:
        if not isinstance(pkg, dict) or not (pkg.get("package") or pkg.get("name")):
            continue
        lics = pkg.get("licenses") or []
        lic_str = ", ".join(str(x) for x in lics) if isinstance(lics, list) else str(lics)
        out.append({
            "name": str(pkg.get("package") or pkg.get("name")),
            "version": str(pkg.get("version") or ""),
            "ecosystem": str(pkg.get("ecosystem") or "unknown"),
            "type": str(pkg.get("type") or "library"),
            "purl": str(pkg.get("purl") or ""),
            "licenses": lic_str,
            "license_category": classify_license(lics),
            "path": str(pkg.get("path") or ""),
            "hash": str(pkg.get("hash") or ""),
        })
    return out


def ingest_sbom(device: str, payload: dict[str, Any]) -> tuple[str, int]:
    """Insert a CycloneDX SBOM scan + components and retain last 3 scans per device."""
    scan_id = str(uuid.uuid4())
    scanned_at = str(payload.get("scanned_at") or datetime.now(timezone.utc).isoformat())
    fmt = "cyclonedx" if payload.get("bomFormat") == "CycloneDX" or payload.get("components") else "native"
    scanner = str(payload.get("scanner") or "native")
    components = _extract_components(payload)

    stmts = [
        "BEGIN",
        (
            "INSERT INTO sbom_scans (id, device, scanned_at, format, scanner, total_packages) VALUES "
            f"({_sql_str(scan_id)}::uuid, {_sql_str(device)}, {_sql_str(scanned_at)}::timestamptz, "
            f"{_sql_str(fmt)}, {_sql_str(scanner)}, {len(components)})"
        ),
    ]
    batch_size = 500
    for i in range(0, len(components), batch_size):
        chunk = components[i:i + batch_size]
        vals = [
            f"({_sql_str(scan_id)}::uuid, {_sql_str(device)}, {_sql_str(c['name'])}, "
            f"{_sql_str(c['version'])}, {_sql_str(c['ecosystem'])}, {_sql_str(c['type'])}, "
            f"{_sql_str(c['purl'])}, {_sql_str(c['licenses'])}, "
            f"{_sql_str(c['license_category'])}, {_sql_str(c['path'])}, {_sql_str(c['hash'])})"
            for c in chunk
        ]
        stmts.append(
            "INSERT INTO sbom_components "
            "(scan_id, device, name, version, ecosystem, type, purl, licenses, license_category, path, hash) "
            "VALUES " + ", ".join(vals)
        )
    # Retain last 3 scans per device (matching kri archive_old_scans keep_count=3)
    stmts.append(
        f"DELETE FROM sbom_scans WHERE device = {_sql_str(device)} AND id NOT IN ("
        f"SELECT id FROM sbom_scans WHERE device = {_sql_str(device)} "
        "ORDER BY scanned_at DESC LIMIT 3)"
    )
    stmts.append("COMMIT")

    with _PgWireClient(PG_HOST, PG_PORT, PG_USER, PG_DB) as pg:
        pg.execute(";\n".join(stmts) + ";")
    return scan_id, len(components)


class IngestHandler(BaseHTTPRequestHandler):
    """HTTP handler for process stats and SBOM ingestion."""

    def _send_json(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path in ("/healthz", "/health", "/"):
            self._send_json(200, {"status": "ok", "service": "telemetry-ingest"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0 or length > 32 * 1024 * 1024:
            self._send_json(400, {"error": "invalid content-length"})
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf8"))
        except Exception as exc:
            self._send_json(400, {"error": f"invalid json: {exc}"})
            return

        try:
            if self.path.rstrip("/") == "/api/v1/ingest/process_stats":
                count = ingest_process_stats(payload)
                self._send_json(200, {"status": "ingested", "count": count})
            elif self.path.startswith("/api/v1/ingest/sbom/"):
                device = self.path[len("/api/v1/ingest/sbom/"):].strip("/") or str(
                    payload.get("device") or "unknown"
                )
                scan_id, count = ingest_sbom(device, payload)
                self._send_json(200, {"status": "indexed", "scan_id": scan_id, "component_count": count})
            else:
                self._send_json(404, {"error": "unknown ingest endpoint"})
        except Exception as exc:
            print(f"[telemetry-ingest] error on {self.path}: {exc!r}", flush=True)
            self._send_json(500, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> int:
    init_schema_with_retry()
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), IngestHandler)
    print(f"[telemetry-ingest] listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)

    def _stop(signum: int, frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
