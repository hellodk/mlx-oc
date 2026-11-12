# mlx-oc Monitoring Bundle

Push-based observability for Macbooks running local LLMs (mlx_lm.server).
Sends metrics/traces/logs to a central **VictoriaMetrics + Grafana + Opik** stack.
No Prometheus scraping — devices initiate all outbound connections.

---

## Table of Contents

1. [Architecture](#architecture)
   - [Push Model](#push-model)
   - [Data Flow](#data-flow)
   - [Component Overview](#component-overview)
2. [File System Layout](#file-system-layout)
3. [Configuration](#configuration)
   - [nodes.yml (Single Source of Truth)](#nodesyml-single-source-of-truth)
   - [edge.env (Runtime Overrides)](#edgeenv-runtime-overrides)
   - [Variable Precedence](#variable-precedence)
   - [Variable Reference](#variable-reference)
4. [Code Logic & Metrics Collection](#code-logic--metrics-collection)
   - [Shared Logger (`mlx_logging.py`)](#shared-logger-mlx_loggingpy)
   - [Edge Entrypoint (`mlx_monitoring_bundle.py`)](#edge-entrypoint-mlx_monitoring_bundlepy)
   - [Proxy (`mlx_metrics_proxy.py`)](#proxy-mlx_metrics_proxypy)
   - [Hardware Telemetry (`mlx_hw_telemetry.py`)](#hardware-telemetry-mlx_hw_telemetry-py)
   - [KV Cache Agent (`mlx_kv_cache_agent.py`)](#kv-cache-agent-mlx_kv_cache_agentpy)
   - [Server Supervisor (`mlx_server_supervisor.py`)](#server-supervisor-mlx_server_supervisor-py)
   - [Log Tailer (`mlx_server_log_tailer.py`)](#log-tailer-mlx_server_log_tailer-py)
   - [Node Validator (`validate_nodes.py`)](#node-validator-validate_nodespy)
   - [Node Discovery (`discover_nodes.py`)](#node-discovery-discover_nodespy)
5. [Grafana Dashboards](#grafana-dashboards)
   - [Template Variables](#template-variables)
   - [Dashboard List](#dashboard-list)
6. [Alerts](#alerts)
7. [Commands & Execution](#commands--execution)
   - [Prerequisites](#prerequisites)
   - [Step 1: Generate nodes.yml](#step-1-generate-nodesyml)
   - [Step 2: Validate SSH Connectivity](#step-2-validate-ssh-connectivity)
   - [Step 3: Configure edge.env](#step-3-configure-edgeenv)
   - [Step 4: Start the Edge Bundle](#step-4-start-the-edge-bundle)
   - [Step 5: Deploy Central Stack](#step-5-deploy-central-stack)
   - [Step 6: vmagent Config (Automatic)](#step-6-vmagent-config-automatic)
8. [Opik SDK Tracing](#opik-sdk-tracing)
9. [Security](#security)
10. [Troubleshooting](#troubleshooting)

---

## Architecture

### Push Model

The bundle uses a **push-only** model. Devices (Macbooks) initiate all outbound
connections to the central observability stack. No inbound ports are required
on the device.

```
┌─────────────────────────────────────┐      ┌────────────────────────────────────────┐
│         DEVICE (Macbook)            │      │     CENTRAL OBSERVABILITY SERVER       │
│                                     │      │                                          │
│  mlx_metrics_proxy      :8080  ─────┼─────▶│  otel-collector        :4318 (OTLP)    │
│  mlx_hw_telemetry       :9102  ─────┼─────▶│  victoria-metrics      :8428 (remote)  │
│  mlx_kv_cache_agent     :9104  ─────┼─────▶│                                                    │
│  mlx_server_supervisor  :9105  ─────┼─────▶│  grafana               :3000           │
│  mlx_server_log_tailer  :9106  ─────┼─────▶│  vmalert + alertmanager                  │
│                                     │      │  opik server + postgres + clickhouse   │
│  vmagent (optional sidecar)         │      │                                          │
│  scrapes localhost:8080-9106        │      │                                          │
│  remote_writes to central VM        │      │                                          │
└─────────────────────────────────────┘      └────────────────────────────────────────┘
       ▲                                               ▲
       │ push (remote_write, OTLP, SDK)                │
       └──────────────── static central IP ────────────┘
```

### Data Flow

| Signal | Source | Transport | Destination |
|--------|--------|-----------|-------------|
| Prometheus metrics | 5 exporters (`:9102-9106`) + proxy (`:8080`) | vmagent scrape → remote_write | VictoriaMetrics |
| OTLP traces | Proxy (OpenTelemetry + Opik SDK) | OTLP HTTP | OTel collector + Opik |
| OTLP logs | Log tailer | OTLP HTTP | OTel collector → JSONL file |
| Opik traces | Proxy (Opik SDK) | HTTPS | Opik server → Postgres/ClickHouse |
| Alerts | vmalert (VictoriaMetrics rules) | Alertmanager | Webhook (oncall/default) |

### Component Overview

| Component | Port | Purpose |
|-----------|------|---------|
| `mlx_metrics_proxy` | 8080 | OpenAI-compatible proxy in front of `mlx_lm.server`; emits Prometheus metrics + OTLP traces + Opik traces |
| `mlx_hw_telemetry` | 9102 | CPU/memory/disk/GPU power/thermal/network telemetry |
| `mlx_kv_cache_agent` | 9104 | KV-cache utilization, context length, KV bytes/token |
| `mlx_server_supervisor` | 9105 | `mlx_lm.server` process supervisor with crash auto-restart + health probes |
| `mlx_server_log_tailer` | 9106 | Tails `server.log`, classifies lines, ships to OTLP |
| `vmagent` | (sidecar) | Scrapes localhost exporters, remote_writes to central VictoriaMetrics |
| `otel-collector` | 4318 | Receives OTLP traces/logs from devices |

---

## File System Layout

```
cluster/mlx_monitoring_bundle/
├── README.md                                      # This file (indexed documentation)
├── .gitignore                                     # Excludes __pycache__, logs/, bin/, *.pyc
│
├── edge/                                          # === RUNS ON EACH DEVICE (NATIVE PYTHON) ===
│   ├── config/
│   │   ├── nodes.yml                              # Node registry + shared central endpoints (SOURCE OF TRUTH)
│   │   ├── nodes.yml.template                     # Documented template for nodes.yml
│   │   ├── edge.env                               # Runtime overrides (feature flags, paths, ports)
│   │   └── edge.env.template                      # Documented template for edge.env
│   ├── exporters/
│   │   ├── mlx_logging.py                         # Shared structured JSON logger (all exporters use this)
│   │   ├── mlx_metrics_proxy.py                   # :8080 Proxy + Prometheus + OTLP + Opik
│   │   ├── mlx_hw_telemetry.py                    # :9102 Hardware telemetry
│   │   ├── mlx_kv_cache_agent.py                  # :9104 KV-cache + context metrics
│   │   ├── mlx_server_supervisor.py               # :9105 mlx_lm.server supervisor + auto-restart
│   │   └── mlx_server_log_tailer.py               # :9106 server.log → OTLP logs
│   ├── vmagent-edge/
│   │   └── vmagent.yml                            # vmagent scrape config (template vars)
│   ├── mlx_monitoring_bundle.py                   # Edge entrypoint (start\|stop\|status\|restart)
│   ├── validate_nodes.py                          # Config + prerequisite validator
│   ├── requirements.txt                           # pip dependencies
│   ├── com.mlx.monitoring.bundle.plist            # launchd agent (macOS, auto-restart)
│   └── com.mlx.monitoring-bundle.service          # systemd unit (Linux, auto-restart)
│
├── central/                                       # === RUNS ON CENTRAL SERVER (CONTAINERS) ===
│   ├── config/
│   │   ├── otelcol-config.yaml                    # OTLP receiver config
│   │   ├── alertmanager.yml                       # Alert routing + inhibit rules
│   │   ├── vmalert-rules.yml                      # Multi-device alert rules
│   │   └── grafana/
│   │       ├── datasources.yaml                   # VictoriaMetrics datasource
│   │       └── dashboards.yaml                    # Grafana file provisioning
│   ├── dashboards/                                # 12 Grafana dashboards (device + IST + 5m)
│   ├── docker-compose.central.yml                 # Central stack composition (+ Opik profile)
│   └── opik/                                      # Vendored Opik config (NGINX + ClickHouse)
│
├── scripts/                                       # === SHARED, NODE-AGNOSTIC ===
│   ├── deploy.sh                                  # validate / edge / central [--opik]
│   └── discover_nodes.py                          # Auto-generate nodes.yml via SSH / local inspection
│
└── logs/                                          # Runtime rotating file logs (gitignored)
    └── bin/                                       # Runtime PID files (gitignored)
```

---

## Configuration

### nodes.yml (Single Source of Truth)

`edge/config/nodes.yml` is the single source of truth for node identity and shared
central endpoints. It is read by `mlx_monitoring_bundle.py` and `validate_nodes.py`.

Every Mac is a standalone node. Run the edge bundle locally on each machine;
no SSH or rank orchestration is involved. When exactly one enabled macbook is
listed it is selected automatically, so a single-Mac setup needs no per-host
flags. Set `MLX_MACBOOK_ID` only when the file lists more than one node.

```yaml
central:
  victoria_metrics: "192.168.1.10:8428"    # host:port of central VictoriaMetrics
  otel: "192.168.1.10:4318"                # host:port of central OTel collector
  opik: "192.168.1.10:32173"               # host:port of central Opik server

macbooks:
  - id: "mm"                               # REQUIRED: unique device identifier
    serial: "HM225L205P"                   # OPTIONAL: hardware serial
    ip: "192.168.1.64"                     # REQUIRED: device IP address
    enabled: true                          # OPTIONAL: false to skip this node
    model: "Qwen3.5-4B-MLX-4bit"           # OPTIONAL: model id (else auto-discovered)
    model_path: "/Users/dk/mlx-models/Qwen3.5-4B-MLX-4bit"  # OPTIONAL: model directory
```

`model_path` lets the exporters read `config.json` directly instead of guessing
architecture values. When it is set, the supervisor starts `mlx_lm.server` from
the model's parent directory and passes the basename as `--model`, so request
model ids stay stable.

### edge.env (Runtime Overrides)

`edge/config/edge.env` contains runtime flags only. Node identity lives in
`nodes.yml`.

```env
# Feature flags
ENABLE_PROXY=true
ENABLE_HW_TELEMETRY=true
ENABLE_KV_AGENT=true
ENABLE_SUPERVISOR=true
ENABLE_LOGTAILER=true
ENABLE_VMAGENT=true

# Listen ports
PROXY_LISTEN=0.0.0.0:8080
HW_LISTEN=0.0.0.0:9102
KV_LISTEN=0.0.0.0:9104
SUPERVISOR_LISTEN=0.0.0.0:9105
LOGTAILER_LISTEN=0.0.0.0:9106

# Proxy guardrails
MLX_LOGPROBS=3
MLX_LOGPROBS_STREAM_SAMPLE=0.0
MLX_LOW_CONFIDENCE=0.5
MAX_PROMPT_TOKENS=12000
MAX_TOKENS_CAP=4096
RATE_LIMIT=0
RATE_WINDOW=10.0

# Logging
LOG_LEVEL=INFO
LOG_ROTATION_MAX_MB=50
LOG_ROTATION_BACKUPS=5

# Paths (relative to edge dir or absolute)
LOG_DIR=./logs
PID_DIR=./bin
CONFIG_DIR=./config

# Python venvs
VENV_PYTHON=/Users/dk/bench/venv-mon/bin/python
MLX_VENV_PYTHON=/Users/dk/venvs/mlx/bin/python

# Upstream mlx_lm.server endpoint consumed by the proxy / KV agent
MLX_UPSTREAM=http://127.0.0.1:11234
MLX_SERVER_PORT=11234

# Adopt an already-healthy server instead of spawning a duplicate
SUPERVISOR_ATTACH_HEALTHY=true

# Reject requests whose model id does not match the served model
MLX_PROXY_STRICT_MODEL=true

# Empty = default-route interface (en1 on Wi-Fi, en0 on Ethernet)
MLX_NET_IFACE=

# Podman stats are meaningless on a Mac without podman
ENABLE_CONTAINER_STATS=false

# VM agent
VMAGENT_SCRAPE_INTERVAL=10s
VMAGENT_QUEUE_CAPACITY=5000
VMAGENT_MAX_SHARDS=200
VMAGENT_BINARY=vmagent

# OTLP transport
OTLP_SCHEME=http
OTLP_VERIFY_TLS=false
```

The model id and model directory belong in `nodes.yml`; `MLX_MODEL` and
`MLX_MODEL_PATH` can still be set in the environment to override them.

### Variable Precedence

Configuration is resolved in this order (lowest → highest priority):

1. **Code defaults** — listen ports, intervals, timeouts (documented in each exporter)
2. **`config/nodes.yml`** — node identity (`id`, `serial`, `ip`, `model`, `model_path`) + shared central endpoints
3. **`config/edge.env`** — per-deployment runtime overrides
4. **Actual environment variables** — highest priority; override everything

### Variable Reference

| Variable | Source | Description |
|----------|--------|-------------|
| `CENTRAL_VICTORIA_METRICS` | nodes.yml central section | host:port of VictoriaMetrics |
| `CENTRAL_OTEL` | nodes.yml central section | host:port of OTel collector |
| `CENTRAL_OPIK` | nodes.yml central section | host:port of Opik server |
| `OTLP_SCHEME` | edge.env / env | `http` \| `https` |
| `OTLP_VERIFY_TLS` | edge.env / env | `true` \| `false` |
| `MLX_MODEL` | nodes.yml per-node / edge.env | Pinned model id (optional) |
| `MLX_MODEL_PATH` | nodes.yml per-node / edge.env | Model directory containing `config.json` (optional) |
| `MLX_UPSTREAM` | edge.env / env | Upstream `mlx_lm.server` base URL (default `http://127.0.0.1:11234`) |
| `MLX_MACBOOK_ID` | env | Device identifier (auto-selected when only one node is enabled) |
| `ENABLE_*` | edge.env | Feature toggles per exporter |
| `*_LISTEN` | edge.env | Listen address per exporter |
| `LOG_DIR`, `PID_DIR` | edge.env | Log and PID file directories |

---

## Code Logic & Metrics Collection

### Shared Logger (`mlx_logging.py`)

All exporters use a single structured JSON logger. Each log record is one JSON
object per line, written to stderr (captured by launcher) plus a rotating file
under `edge/logs/`.

Fields: `ts`, `level`, `service`, `component`, `macbook`, `msg`, `[err]`,
`[trace_id]`, `[span_id]`.

Usage:
```python
from mlx_logging import setup_logging, info, error, warning, degraded

setup_logging(service="mlx-proxy", macbook=os.environ.get("MLX_MACBOOK_ID", "unknown"))
info("proxy listening on :8080", component="proxy")
error("upstream unreachable", component="proxy")
degraded("MLX_MODEL not set; skipping model gauges", component="kv-agent")
```

### Edge Entrypoint (`mlx_monitoring_bundle.py`)

Single entrypoint with four actions: `start`, `stop`, `status`, `restart`.

- **start**: Spawns enabled exporters as subprocesses with `start_new_session=True`,
  writes PID files to `edge/bin/`, then blocks waiting for signals.
- **stop**: Sends SIGTERM to the process group and all exporter PIDs. Leaves an
  adopted (externally started) `mlx_lm.server` running.
- **status**: Prints JSON of running PIDs, node identity, model path, and central endpoints.
- **restart**: stop → wait 2s → start.

Spawn order: supervisor → kv → logtailer → proxy → hw.

### Proxy (`mlx_metrics_proxy.py`)

The proxy sits between clients and `mlx_lm.server` (`:11234` by default), listening on `:8080`.

**Metrics collected** (Prometheus text format on `/metrics`):
- `mlx_requests_total` — requests by model, streaming, kind
- `mlx_tokens_prompt_total` / `mlx_tokens_completion_total` — token counts
- `mlx_ttft_seconds` — time to first token (histogram)
- `mlx_generation_seconds` — total generation time (histogram)
- `mlx_token_rate_tokens_per_second` — completion tokens/sec
- `mlx_hallucination_risk` — composite heuristic risk (0..1)
- `mlx_token_confidence_mean/min/std` — token-level confidence from logprobs
- `mlx_output_perplexity` — exp(-mean(log p))
- `mlx_error_total` — 4xx/5xx errors
- `mlx_up` — upstream reachability gauge

**Confidence scoring:** The proxy injects `logprobs`/`top_logprobs` into
non-streaming requests that didn't ask for them, then strips them from the
response. For streaming requests, a sampled fraction (`MLX_LOGPROBS_STREAM_SAMPLE`)
are de-streamed (run non-streamed) to collect logprobs.

**Model resolution:** `MLX_MODEL` (config) → `MLX_MODEL_PATH` basename →
upstream `GET /v1/models` (preferring the served absolute path) → skip model
gauges with a `[degraded]` log if all fail. Architecture metadata comes from
the model's own `config.json`; nothing is hardcoded. `--no-strict-model`
(env `MLX_PROXY_STRICT_MODEL=false`) forwards any model id instead of rejecting
mismatches.

**Opik tracing:** Each chat completion becomes an Opik trace with an LLM span
(input/output/usage/metadata), hallucination quality feedback scores, and token
confidence feedback scores. Requires `--opik-endpoint` and `opik>=1.4`.

### Hardware Telemetry (`mlx_hw_telemetry.py`)

Runs on each device, listens on `:9102`. Collects:

- CPU load (1/5/15min), CPU count, CPU temperature (powermetrics)
- Memory used/total/free/available (vm_stat; available = free + inactive +
  speculative + purgeable pages), page size
- Disk used/total (root volume)
- GPU power, package power, CPU power, ANE power (powermetrics, slow 15s cadence)
- GPU memory (IOKit), GPU utilization, thermal pressure (pmset)
- Process CPU/RSS for stack components (mlx_lm.server, proxy, kv, tailer, supervisor)
- Optional container stats via `podman stats` (`ENABLE_CONTAINER_STATS=true`)
- Network RX/TX bytes and errors on the default-route interface
  (`MLX_NET_IFACE` overrides)

All gauges carry the `node` label. Each Mac reports only its own telemetry;
there is no SSH fan-out.

### KV Cache Agent (`mlx_kv_cache_agent.py`)

Tails `server.log` for KV-cache state lines (`Prompt Cache: N sequences, X GB`)
and serves Prometheus metrics on `:9104`.

Metrics:
- `mlx_kv_cache_sequences{type}` — cached sequence count by type
- `mlx_kv_cache_bytes{type}` — KV cache bytes by type
- `mlx_kv_cache_utilization` — utilization vs max capacity
- `mlx_context_length_max_tokens` — from `mlx_model_info` (skipped if model unknown)
- `mlx_kv_bytes_per_token` — from `mlx_model_info` (skipped if model unknown)

**Graceful degradation:** If `MLX_MODEL`/`MLX_MODEL_PATH` are unset and upstream
`/v1/models` discovery fails, model-specific gauges are set to 0 and a
`[degraded]` log is emitted. Throughput and other metrics continue unaffected.

### Server Supervisor (`mlx_server_supervisor.py`)

Starts `mlx_lm.server`, watches the child process, and restarts with
exponential backoff on crash. Probes `/v1/models` to distinguish "process alive"
from "serving ready."

If a healthy server is already answering when the supervisor starts, it adopts
that process instead of spawning a duplicate (`SUPERVISOR_ATTACH_HEALTHY=true`,
disable with `--no-attach`). When `model_path` is configured the server is
launched from the model's parent directory with the basename as `--model`, so
request model ids stay stable.

Metrics:
- `mlx_server_state` (0=idle, 1=running, 2=starting, 3=backoff)
- `mlx_server_up` / `mlx_server_ready` / `mlx_model_loaded`
- `mlx_server_restarts_total`, `mlx_server_crashes_total{reason}`
- `mlx_server_uptime_seconds`, `mlx_server_last_exit_code`
- `mlx_server_health_checks_total{result}`, `mlx_server_backoff_seconds`

**Crash reason sniffing:** On exit, scans the tail of `server.log` for patterns:
metal GPU errors, OOM, python errors → classified as `metal_gpu_error`, `oom`,
`python_error`, `hung`, `exit`, or `unknown`.

**Keep-alive:** If the process is alive but `/v1/models` stops answering for
`--hang-after` seconds, the supervisor kills and restarts it.

### Log Tailer (`mlx_server_log_tailer.py`)

Tails `server.log` incrementally, classifies each line via regex table into
(severity, category), ships to OTLP endpoint as LogRecords.

Classifiers (first match wins): GPU errors → error/gpu, rank warnings →
warning/rank, Python errors → error/python, generation keywords → info/generation,
HTTP lines → info/http (filtered by default).

Metrics on `:9106`: lines classified, shipped, dropped, errors, file offset,
queue depth.

### Node Validator (`validate_nodes.py`)

Short-lived tool. Validates `nodes.yml` and local edge prerequisites: central
endpoints are configured, at least one node is enabled and selectable,
`model_path` contains a readable `config.json`, the configured Python
executables exist, and `yaml`/`prometheus_client`/`requests` import in the
monitoring venv. `--check-ports` warns when exporter ports are already in use.
No SSH or password handling.

### Node Discovery (`discover_nodes.py`)

Auto-generates `nodes.yml` by inspecting the local machine or a remote host via
SSH. Collects: hostname (id), serial (`system_profiler`), IP, and optional
`model` / `model_path`.

**No hardcoded IPs:** All central endpoints (`--central-victoria-metrics`,
`--central-otel`, `--central-opik`) are required flags with no defaults.

---

## Grafana Dashboards

### Template Variables

All 12 dashboards use a `$device` template variable (renamed from `$macbook`).
The underlying Prometheus label remains `macbook` (set by vmagent
`external_labels`). The variable queries `label_values(..., macbook)`.

| Variable | Type | Query |
|----------|------|-------|
| `$device` | query | `label_values(mlx_hw_up, macbook)` |
| `$node` | query | `label_values(mlx_hw_up, node)` (where applicable) |
| `$kind` | query | `label_values(mlx_requests_total, kind)` (inference dashboard) |

All dashboards default to **IST timezone** (`Asia/Kolkata`) and **5m auto
refresh**.

### Dashboard List

| Dashboard | File | Purpose |
|-----------|------|---------|
| Cluster Overview | `overview.json` | Cross-device request rate, errors, GPU, thermal, risk, confidence |
| Hardware | `hardware.json` | Per-device CPU, memory, disk, temperature, uptime |
| Serving | `serving.json` | `mlx_lm.server` up/ready/loaded, restarts, crashes, state |
| Inference | `inference.json` | TTFT, generation time, token rate, queue wait, finish reasons, tool calls |
| Quality | `quality.json` | Hallucination risk, confidence, perplexity, entropy, margin, heuristics |
| KV Cache | `kv-cache.json` | Cache utilization, sequences, bytes, context utilization |
| Logs | `logs.json` | Tailer lines classified/shipped/dropped, errors, offset |
| Network | `network.json` | Ring interconnect RX/TX bytes and errors |
| Power & Thermal | `power-thermal.json` | GPU/CPU/ANE/package power, thermal pressure, GPU freq |
| Alerts | `alerts.json` | Active MLX alerts by device, critical/warning counts |
| Opik | `opik.json` | Opik tracing correlation with proxy-scored quality signals |
| SLO | `slo.json` | 5xx error rate, error budget, availability |
| Detail | `detail.json` | Per-device consolidated view (hardware + serving + inference + quality + power) |

---

## Alerts

Alert rules live in `central/config/vmalert-rules.yml`, applied by vmalert.
All alerts are grouped by device label so different devices never collapse
into a single firing alert.

| Alert | Severity | Condition |
|-------|----------|-----------|
| `MLXDeviceSilent` | critical | No hw telemetry for 3 min |
| `MLXHardwareExporterDown` | warning | hw exporter not reporting |
| `MLXGPUHighUtilization` | warning | GPU > 95% for 10 min |
| `MLXThermalThrottling` | warning | Thermal pressure > 0.7 |
| `MLXLoadHigh` | warning | load15 > 90% CPU count |
| `MLXMemoryPressureHigh` | warning | Free pages < 2% RAM |
| `MLXDiskFull` | warning | Disk > 85% full |
| `MLXServerDown` | critical | `mlx_server_up == 0` |
| `MLXServerNotReady` | warning | Up but `/v1/models` failing |
| `MLXModelNotLoaded` | warning | Model gauge == 0 |
| `MLXServerCrashLooping` | critical | ≥3 restarts in 15 min |
| `MLXProxyDown` | critical | `mlx_up == 0` |
| `MLXUpstreamDown` | critical | Proxy up but upstream unreachable |
| `MLXThroughputDrop` | warning | < 0.1 req/s for 15 min |
| `MLXTTFTHigh` | warning | p95 TTFT > 5s |
| `MLXGenerationSlow` | warning | p95 gen > 60s |
| `MLXTokenRateLow` | warning | Median < 5 tok/s |
| `MLXHallucinationHigh` | warning | Risk > 0.7 |
| `MLXLowConfidence` | warning | Mean confidence < 0.5 |
| `MLXHighPerplexity` | warning | Perplexity > 3 |
| `MLXKVCacheFull` | warning | Utilization > 85% |
| `MLXErrorBudget` | warning | 5xx rate > 5% over 1h |

Alertmanager (`central/config/alertmanager.yml`) routes `critical` → oncall
webhook, everything → default webhook. Inhibit rules suppress downstream alerts
when a device or proxy is down.

---

## Commands & Execution

The primary entrypoint for all bundle operations is **`scripts/deploy.sh`**.
It operates in two modes:
1. **Interactive Menu**: Run `./scripts/deploy.sh` with no arguments to get an interactive numbered menu.
2. **Direct CLI**: Pass the desired command directly (`setup-edge`, `edge [start|daemon|stop|status]`, `validate`, `central`).

Before performing any action, `deploy.sh` automatically performs a fast-forward pull from `git` to keep your environment current.

### Step 1: Automated Edge Setup

Run the interactive setup on the Mac (no root access required):
```bash
./scripts/deploy.sh setup-edge
```
Or pass parameters directly:
```bash
./scripts/deploy.sh setup-edge <CENTRAL_IP> [MODEL_ID] [MODEL_PATH]
```

**What this automatically does:**
1. **Installs `vmagent`**: Detects OS and CPU architecture (Apple Silicon `darwin-arm64`, Intel `darwin-amd64`, or Linux), downloads the official pinned binary to `~/mlx-monitoring/bin/vmagent`, and adds it to your shell profile (`~/.zshrc` or `~/.bashrc`).
2. **Prepares Python Virtualenv**: Detects your existing monitoring venv or automatically creates `~/mlx-monitoring/venv` with all pinned dependencies installed.
3. **Discovers Hardware & Subnet**: Generates `edge/config/nodes.yml` with your Mac's serial number, hostname, and the correct LAN IP routing towards the Central IP.
4. **Generates Runtime Config**: Creates `edge/config/edge.env` mapping to your local venvs and `vmagent`.
5. **Validates Setup**: Runs `validate_nodes.py` to ensure everything is functional.

### Step 2: Manage the Edge Daemon

Start the edge bundle as a background daemon:
```bash
./scripts/deploy.sh edge daemon
```

Check process health and active PID table:
```bash
./scripts/deploy.sh edge status
```

Review vmagent edge configuration and target health:
```bash
./scripts/deploy.sh review-vmagent
```

Inspect live exporter logs:
```bash
tail -f edge/logs/deploy.log
tail -f edge/logs/vmagent.bundle.log
```

Stop the edge bundle cleanly:
```bash
./scripts/deploy.sh edge stop
```

*(To run attached in foreground instead: `./scripts/deploy.sh edge start`)*

### Step 3: Deploy Central Observability Stack

On the central server (supports both **Docker** and **Podman**):

```bash
# Standard stack: VictoriaMetrics + Grafana + vmalert + Alertmanager + OTel Collector
./scripts/deploy.sh central

# Extended stack with self-hosted Opik LLM tracing (includes ClickHouse & RustFS)
./scripts/deploy.sh central --opik

# Review Grafana credentials, configuration and health checks
./scripts/deploy.sh review-grafana
```

**Interactive Image Pull Gate:**
`deploy.sh central` will print the full list of container images to be pulled and prompt for explicit confirmation `(y/N)` before pulling or launching containers.

**Web Endpoints:**
- **Grafana**: `http://<central-ip>:3000` (provisioned with 13 pre-configured dashboards in IST)
- **VictoriaMetrics**: `http://<central-ip>:8428`
- **vmalert**: `http://<central-ip>:8880`
- **Alertmanager**: `http://<central-ip>:9093`
- **Opik UI**: `http://<central-ip>:32173` (when deployed with `--opik`)

---

### Step 4: End-to-End Pipeline Diagnostics & Service Reviews

When you deploy the stack and want to verify telemetry flow or troubleshoot missing metrics/logs/traces, use the automated diagnostics suite:

```bash
# 1. Automated Pipeline Doctor (Data Flow Traceroute)
./scripts/deploy.sh doctor

# 2. Comprehensive Central Stack Audit
./scripts/deploy.sh review-central

# 3. Comprehensive Edge Node Exporter Audit
./scripts/deploy.sh review-edge [node_id]

# 4. Granular Component Reviews
./scripts/deploy.sh review-vm             # VictoriaMetrics TSDB storage & ingestion rates
./scripts/deploy.sh review-grafana        # Grafana health, auth, datasources & credentials
./scripts/deploy.sh review-alertmanager   # Alertmanager notification routing & webhook status
./scripts/deploy.sh review-vmalert        # vmalert rule evaluation engine & firing alerts
./scripts/deploy.sh review-otel           # OpenTelemetry Collector receiver & pipelines
./scripts/deploy.sh review-opik           # Opik LLM tracing backend, frontend & storage
./scripts/deploy.sh review-vmagent        # Edge vmagent scrape targets & persistent queue
```

---

---

## Opik SDK Tracing

`opik>=1.4` is included in `edge/requirements.txt`.

**What it does:** The proxy logs each chat completion as an Opik trace with an
LLM span (input/output/usage/metadata), hallucination quality feedback scores
(0..1), and token confidence feedback scores.

**What you need to do:**
1. Start Opik on the central host (profile in `docker-compose.central.yml`):
   ```bash
   cd central
   docker compose -f docker-compose.central.yml --profile opik up -d
   ```
2. Set `central.opik: "<host>:32173"` in `nodes.yml`.
3. Start the edge bundle. It derives `OPIK_ENDPOINT` and the Opik OTLP endpoint
   from `central.opik`, and the proxy initializes the Opik SDK automatically.
4. Traces appear in the "mlx" project in the Opik UI at
   http://<central-ip>:32173.

**No code changes needed** — the proxy initializes the Opik SDK whenever an
Opik endpoint is configured.

---

## Security

- **No credentials on disk:** There is no SSH orchestration and no password
  handling. Every Mac runs the edge bundle locally.
- **No hardcoded IPs:** All central endpoints come from `nodes.yml` or environment
  variables.
- **OTLP TLS:** `OTLP_SCHEME=http` and `OTLP_VERIFY_TLS=false` by default.
  For production, set `OTLP_SCHEME=https` and `OTLP_VERIFY_TLS=true` with
  proper CA certificates.
- **No root:** All exporters bind to unprivileged ports (>1024). No sudo
  required except optional CPU temperature via `powermetrics -n 1 --samplers smc`.

---

## Troubleshooting

### Automated Pipeline Doctor (`deploy.sh doctor`)

When you deploy the monitoring stack and find that **no data is being populated in Grafana**, do not manually inspect individual containers or grep logs. Run:

```bash
./scripts/deploy.sh doctor
```

The Doctor executes a 5-stage telemetry traceroute that isolates the failure domain in seconds:

1. **Stage 1 (Central TSDB Ingestion Gate)**: Checks TCP reachability, HTTP `/health`, and total active time-series count in VictoriaMetrics. If this fails, the central TSDB is stopped or unreachable.
2. **Stage 2 (Edge Telemetry Ingestion Gate)**: Queries VictoriaMetrics for live `up{macbook="<node_id>"}` gauges across all 5 exporters (`mlx-proxy`, `mlx-hw`, `mlx-kv`, `mlx-supervisor`, `mlx-logtailer`) and measures sample recency (`time() - max(timestamp(up))`). If samples are >60s old or missing, vmagent on that edge Mac is down or blocked by network/firewall.
3. **Stage 3 (Alerting & Rule Engine Gate)**: Verifies `vmalert` rule execution and `alertmanager` notification routing.
4. **Stage 4 (Logs & Traces Ingestion Gate)**: Verifies OTLP HTTP receiver (`:4318`) and Opik Gateway (`:32173`).
5. **Stage 5 (Grafana & Query Pipeline Gate)**: Validates Grafana HTTP health, credentials/auth, VictoriaMetrics datasource registration, and executes an active datasource proxy query (`count(up)`).

### Common Failure Modes & Quick Fixes

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No data in Grafana | Pipeline broken at one of the 5 stages | Run `./scripts/deploy.sh doctor` to identify exact broken stage |
| No metrics in VictoriaMetrics | vmagent not running or cannot reach central | Run `./scripts/deploy.sh review-vmagent` on edge; check `CENTRAL_VICTORIA_METRICS` |
| `MLXDeviceSilent` firing | hw exporter not running or vmagent can't scrape | Run `./scripts/deploy.sh review-edge` to check exporter ports and process table |
| Model gauges at 0 | no `MLX_MODEL`/`MLX_MODEL_PATH` and `/v1/models` unreachable | Set `model_path` in `nodes.yml` or ensure upstream is reachable |
| Opik traces missing | `central.opik` not set or Opik profile not running | Run `./scripts/deploy.sh review-opik`; start with `deploy.sh central --opik` |
| Proxy rejects model id | strict model matching | Send the served model id or set `MLX_PROXY_STRICT_MODEL=false` |
| Duplicate `mlx_lm.server` | attach mode disabled while a server was already up | Keep `SUPERVISOR_ATTACH_HEALTHY=true` or stop the old process first |
| Crash loop on restart | Server dying repeatedly | Check `server.log` and `mlx_server_crashes_total{reason}` |
| Permission denied on logs | `LOG_DIR` not writable | Set `LOG_DIR` to a user-writable path in `edge.env` |

---

