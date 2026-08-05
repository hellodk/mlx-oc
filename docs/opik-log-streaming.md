# Plan: Stream `mlx_lm.server` runtime logs into observability

Status: **IMPLEMENTED** (with one deviation from the original review, see §2)

## 1. Problem

Opik is self-hosted and reachable (`http://192.168.1.10:32173`), and its `mlx`
project exists — but it is **empty**. Verified at plan time:

1. **Zero traces.** The running proxy was started with only
   `--otlp-endpoint http://192.168.1.64:4318`. `start_server.sh` gates the two
   Opik paths behind env vars (`OPIK_ENDPOINT`, `OPIK_OTLP_ENDPOINT`) that are
   not set. No request has ever been traced into Opik.
2. **Logs are mis-routed.** The proxy *does* configure an OTel logs pipeline
   (`LoggerProvider` + `LoggingHandler`, `mlx_metrics_proxy.py:148-162`) but
   only toward the local otel-collector `:4318`, whose `logs` pipeline ends at
   a `debug` exporter (`otelcol-config.yaml:36-39`) — i.e. `podman logs`
   stdout. Not persisted, not Opik. The proxy mostly uses `print()`, which
   bypasses `logging` anyway, so the pipeline is barely fed.
3. **The server's own log never leaves the machine.** `cluster/logs/server.log`
   has exactly two consumers: the KV-cache agent (→ Prometheus gauges) and the
   supervisor (→ crash-reason label). The raw stream is not shipped anywhere.

Goal: every request's trace in Opik should be able to tell the server-side
part of the story — the `[METAL]` GPU fault, the rank-down warnings, the
generation lines — not just the proxy's view.

## 2. Design: one pipeline, two paths

Both paths speak OTLP/HTTP and share the same exporter stack
(`OTLPSpanExporter` / `OTLPLogExporter`). One pipeline, two feeds.

### Deviation discovered during implementation

The original plan assumed Opik accepts OTLP logs at
`/api/v1/private/otel/v1/logs`. **It does not** — verified three ways:
`POST /v1/logs` → 404 (while `/v1/traces` works), the OTel ingestion docs say
trace-only, and the installed `opik` SDK 2.2.13 has no log API or
`LoggingHandler`. Opik is a **trace-first** platform on this version.

So the honest split is:
- **Path A — request tracing → Opik** (`projectName: mlx`). Works, one trace
  per request.
- **Path B — runtime logs → otel-collector** (`:4318`), whose `logs` pipeline
  now writes a durable JSONL file (`mlx-logs` volume). Logs leave the machine
  and are persisted; traces stay in Opik.

### Path A — Enable request tracing (existing code, currently OFF)

Turn on `OPIK_OTLP_ENDPOINT=http://192.168.1.10:32173/api/v1/private/otel` in
`start_server.sh`. The proxy's existing second span processor
(`mlx_metrics_proxy.py:137-145`) then ships every chat completion to Opik with
OpenInference attributes.

**Decision — use the OTLP path, keep the Opik SDK path OFF.** Enabling both
`--opik-endpoint` (SDK) and `--opik-otlp-endpoint` (OTLP) would create
**two traces per request**. One pipeline is cheaper to reason about and shares
infrastructure with the log tailer. The SDK path remains documented as an
optional fallback.

### Path B — New `mlx_server_log_tailer.py` (new code)

A small tailer (same shape as `mlx_kv_cache_agent.py`) that:

1. **Reads incrementally.** Opens `cluster/logs/server.log`, seeks to the
   current end on start (no backfill flood), then follows with a poll interval
   (500 ms). Optional `--backfill N` sends the last N lines once at startup.
2. **Classifies each line** by regex → severity + category (table below).
3. **Emits OTLP LogRecords** to the collector `<otlp_endpoint>/v1/logs`
   (default `http://192.168.1.64:4318`):
   - `body` = raw line
   - attrs: `mlx.log.severity`, `mlx.log.category`, `mlx.log.source =
     "mlx_lm.server"`, `service.name = "mlx-lm-server"`, `mlx.node.name =
     "rank0"`, `host.name`
4. **Protects the collector from floods** (see §3).
5. **Exports self-health** on `:9106` (`mlx_log_tailer_up`,
   `mlx_log_tailer_lines_total{severity,category}`, `mlx_log_tailer_shipped_total`,
   `mlx_log_tailer_dropped_total{reason}`, `mlx_log_tailer_errors_total`)
   so a dead tailer is visible in VM, mirroring the KV agent pattern.

### Second fix uncovered during implementation: `server.log` was never fed

`mlx_server_supervisor.py._spawn()` ran `subprocess.Popen(cmd, cwd=...)` with
**no stdout/stderr redirect**, so `mlx_lm.server` output went into
`supervisor.log`, never `server.log`. `--server-log` was only used for crash
sniffing. The fix redirects the child's stdout/stderr into `server.log`
(append), which is what the KV-cache agent, the log tailer, and the crash
sniffer all consume — and what makes the whole log-streaming feature real.

### Line classification

| Regex signal | severity | category |
|---|---|---|
| `[METAL]`, `Command buffer execution failed`, `kIOGPUCommandBufferCallback` | error | gpu |
| `[WARN]`, `Node with rank`, `exited with code` | warning | rank |
| `Traceback`, `ERROR`, `fatal`, `SIGABRT`, `segmentation fault` | error | python |
| `cache`, `tokens`, `ttft`, `gen`, `generation` (KV/generation lines) | info | generation |
| HTTP access lines (`"POST /v1/...` ) | info | http — **dropped by default** |

Default ships only WARN/ERROR/METAL/generation lines. `--include-http` opts
into the HTTP noise; `--all` is the raw stream (documented as expensive).

## 3. Keeping the flood out

- **Filter first.** In steady state the 4B-era `server.log` is mostly HTTP
  access lines; the filtered stream is a handful of lines/day. Negligible for
  Opik.
- **Rate cap.** Token bucket, default `--max-rate 50 lines/s`. A crash-spam
  loop or `--all` cannot outrun the budget.
- **Backfill default OFF.** `--backfill N` exists only for initial seeding.
- **Bounded queue.** The SDK's `BatchLogRecordProcessor` caps the in-memory
  buffer (2048 records); rate-limited lines are counted as dropped
  (`mlx_log_tailer_dropped_total{reason="rate_limit"}`), never unbounded.
- **`--include-http` and `--all` are opt-in**, clearly documented as noisy
  (MBs/day under bench load).

## 4. Correlation: what phase 1 does and doesn't do

- **Phase 1 = project-level logs.** Log records land in the collector's JSONL
  file (and proxy logs share the same pipeline), correlated to Opik traces by
  time + service attributes.
- **Phase 2 = precise trace attachment.** Attaching a server-log line to its
  exact Opik trace requires a request-ID that flows proxy → upstream →
  `server.log` → `trace_id` on the log record. `mlx_lm.server` does not echo
  per-request IDs into its log today, so this is deferred and documented, not
  built.

## 5. Wiring

- `cluster/start_server.sh` — add the tailer as a **5th component**:
  `start_logtailer()`, pidfile `logtailer.pid`, status line, `logs` subcommand
  entry, and stop-kill. The tailer targets the collector (`$OTLP`), not Opik.
  Traces go to Opik via `OPIK_OTLP_ENDPOINT` (default
  `http://192.168.1.10:32173/api/v1/private/otel`).
- **Restart hardening (uncovered during implementation):** `stop()` + a
  `wait_port_free` step before starting the supervisor/logtailer, because a
  slow-exiting old process would otherwise kill the new one with `EADDRINUSE`.
- `observability/vm-scrape.tmpl.yml` — add `mlx-logtailer` job
  (`__MLX_IP__:9106`, service `mlx-logtailer`, node rank0); regenerate via
  `setup.sh`.
- `observability/vmalert/rules.yml` — add `MLXLogTailerDown`
  (`mlx_log_tailer_up == 0`, warning, `for: 2m`) and `MLXLogTailerNotSending`
  (up but zero shipped in 15m) so a silent tailer is caught.
- `observability/otelcol-config.yaml` + `compose.yaml` — logs pipeline gains a
  `file` exporter (JSONL, `mlx-logs` volume, 50 MB × 5 rotation); collector
  runs as root so the distroless non-root user can append the file.
- `.gitignore` — nothing new (logs already ignored).

## 6. Acceptance gates

1. `./start_server.sh restart` → status shows `log_tailer RUNNING`,
   `:9106/metrics` returns 200, `mlx_log_tailer_up 1`.
2. One request → Opik API shows exactly **one** new trace in `mlx` (no
   duplicates); the trace has an LLM span with input/output/usage.
3. A request → `mlx_log_tailer_lines_total` increments and the line appears in
   the collector's JSONL file (verified via `podman cp
   otel-collector:/var/log/otelcol/mlx-server-logs.jsonl`), with the right
   severity/category.
4. VM has an `mlx-logtailer` target up; `MLXLogTailerDown` / `MLXLogTailerNotSending`
   evaluate without firing.
5. No regression: `mlx_server_*` and `mlx_kv_*` metrics unchanged;
   `--opik-endpoint` (SDK) remains OFF to guarantee one-trace-per-request.

All five passed on 2026-08-05 (3 requests → 3 traces, no duplicates).

## 7. Out of scope (phase 2)

- Trace-attached server logs (needs request-ID plumbing).
- Structured parsing of KV-cache/generation lines into attributes (free-text
  body in phase 1).
- Alerting beyond `MLXLogTailerDown` / `MLXLogTailerNotSending`.
- Tailing rank1's worker log over SSH. Assumption for phase 1: the distributed
  run's output (including "Node with rank 1 exited" warnings) is mirrored into
  rank0's `server.log`; verified true during implementation.
- Getting logs into Opik's UI. Requires an Opik version with a logs feature;
  our install is trace-only (OTel `/v1/logs` → 404, SDK has no log API). The
  collector JSONL is the durable log store until then.

## 8. Files touched

| File | Change |
|---|---|
| `cluster/mlx_server_log_tailer.py` | **new** — tailer + `:9106` self-metrics |
| `cluster/mlx_server_supervisor.py` | redirect server stdout/stderr into `server.log` |
| `cluster/start_server.sh` | 5th component (pidfile, status, logs, stop), `OPIK_OTLP_ENDPOINT` default, `wait_port_free` hardening |
| `observability/vm-scrape.tmpl.yml` | `mlx-logtailer` scrape job |
| `observability/vmalert/rules.yml` | `MLXLogTailerDown` + `MLXLogTailerNotSending` warnings |
| `observability/otelcol-config.yaml` | logs pipeline → `file` exporter (durable JSONL) |
| `observability/compose.yaml` | `mlx-logs` volume, collector `user: "0"` |
| `blog/10-opik-log-streaming.html` | **new** blog post |
| `docs/opik-log-streaming.md` | this doc (plan → IMPLEMENTED) |
