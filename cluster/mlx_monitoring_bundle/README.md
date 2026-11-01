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
   - [Step 6: Render Template Configs](#step-6-render-template-configs)
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
│   ├── validate_nodes.py                          # SSH connectivity validator
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
│   ├── docker-compose.central.yml                 # Central stack composition
│   └── Dockerfile.central                         # Central config image
│
├── scripts/                                       # === SHARED, NODE-AGNOSTIC ===
│   ├── deploy.sh                                  # validate / edge / central / templates
│   ├── build_and_push.sh                          # Build central image (quay.io/hellodk/hydra:central)
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

```yaml
central:
  victoria_metrics: "192.168.1.100:8428"   # host:port of central VictoriaMetrics
  otel: "192.168.1.100:4318"                # host:port of central OTel collector
  opik: "192.168.1.100:32173"               # host:port of central Opik server

macbooks:
  - id: "dk-mac"                            # REQUIRED: unique device identifier
    serial: "ASHU1343"                      # OPTIONAL: hardware serial
    ip: "192.168.1.10"                      # REQUIRED: device IP address
    ssh_user: "dk"                          # OPTIONAL: SSH user
    ssh_key: "~/.ssh/id_rsa"                # OPTIONAL: SSH key (omit to use password prompt)
    ssh_connect_timeout: 5                  # OPTIONAL: SSH connect timeout (seconds)
    role: "rank0"                           # REQUIRED: rank0 | rank1
    enabled: true                           # OPTIONAL: false to skip this node
    model: "mlx-community/Qwen3.5-4B-MLX-8bit"  # OPTIONAL: pinned model (auto-discovered if unset)
```

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
VENV_PYTHON=/home/dk/.venv/bin/python
MLX_VENV_PYTHON=/home/dk/venvs/mlx/bin/python
MLX_LAUNCH=/home/dk/venvs/mlx/bin/mlx.launch

# VM agent
VMAGENT_SCRAPE_INTERVAL=10s
VMAGENT_QUEUE_CAPACITY=5000
VMAGENT_MAX_SHARDS=200

# OTLP transport
OTLP_SCHEME=http
OTLP_VERIFY_TLS=false

# MLX model (optional; auto-discovered from upstream /v1/models if unset)
# MLX_MODEL=mlx-community/Qwen3.5-4B-MLX-8bit
```

### Variable Precedence

Configuration is resolved in this order (lowest → highest priority):

1. **Code defaults** — listen ports, intervals, timeouts (documented in each exporter)
2. **`config/nodes.yml`** — node identity (`id`, `serial`, `ip`, `ssh_*`, `role`, `model`) + shared central endpoints
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
| `MLX_MODEL` | nodes.yml per-node / edge.env | Pinned model (optional) |
| `MLX_MACBOOK_ID` | env | Device identifier (set by launcher) |
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
- **stop**: Sends SIGTERM to the process group and all exporter PIDs, plus
  rank1 hw telemetry via SSH.
- **status**: Prints JSON of running PIDs.
- **restart**: stop → wait 2s → start.

Spawn order: supervisor → kv → logtailer → proxy → hw(rank0) → hw(rank1 via SSH).

### Proxy (`mlx_metrics_proxy.py`)

The proxy sits between clients and `mlx_lm.server` (`:8081`), listening on `:8080`.

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

**Model resolution:** `MLX_MODEL` (config) → upstream `GET /v1/models` → skip
model gauges with `[degraded]` log if both fail.

**Opik tracing:** Each chat completion becomes an Opik trace with an LLM span
(input/output/usage/metadata), hallucination quality feedback scores, and token
confidence feedback scores. Requires `--opik-endpoint` and `opik>=1.4`.

### Hardware Telemetry (`mlx_hw_telemetry.py`)

Runs on each device, listens on `:9102`. Collects:

- CPU load (1/5/15min), CPU count, CPU temperature (powermetrics)
- Memory used/total/pressure (vm_stat)
- Disk used/total (root volume)
- GPU power, package power, CPU power, ANE power (powermetrics, slow 15s cadence)
- GPU memory (IOKit), GPU utilization, thermal pressure (pmset)
- Process CPU/RSS for stack components (mlx_lm.server, proxy, kv, tailer, supervisor)
- Container stats via `podman stats` (for central stack components)
- Ring interconnect network bytes/errors (ifconfig + netstat)

All gauges carry the `node` label. Rank1 telemetry is collected via SSH from
the rank0 device.

### KV Cache Agent (`mlx_kv_cache_agent.py`)

Tails `server.log` for KV-cache state lines (`Prompt Cache: N sequences, X GB`)
and serves Prometheus metrics on `:9104`.

Metrics:
- `mlx_kv_cache_sequences{type}` — cached sequence count by type
- `mlx_kv_cache_bytes{type}` — KV cache bytes by type
- `mlx_kv_cache_utilization` — utilization vs max capacity
- `mlx_context_length_max_tokens` — from `mlx_model_info` (skipped if model unknown)
- `mlx_kv_bytes_per_token` — from `mlx_model_info` (skipped if model unknown)

**Graceful degradation:** If `MLX_MODEL` is unset and upstream `/v1/models`
discovery fails, model-specific gauges are set to 0 and a `[degraded]` log is
emitted. Throughput and other metrics continue unaffected.

### Server Supervisor (`mlx_server_supervisor.py`)

Spawns `mlx_lm.server` via `mlx.launch`, watches the child process, and
restarts with exponential backoff on crash. Probes `/v1/models` to distinguish
"process alive" from "serving ready."

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

Short-lived tool. For each enabled node in `nodes.yml`, runs `ssh user@ip true`
to verify connectivity. If no `ssh_key` is configured, prompts for password via
`getpass` — held in memory only, cleared on exit. No credentials stored on disk.

### Node Discovery (`discover_nodes.py`)

Auto-generates `nodes.yml` by inspecting the local machine or a remote host via
SSH. Collects: hostname (id), serial (`system_profiler`), IP, role (from flag).

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

### Prerequisites

On each device:
```bash
pip install -r edge/requirements.txt
```

On central server:
```bash
docker compose -f central/docker-compose.central.yml up -d
# or
podman compose -f central/docker-compose.central.yml up -d
```

### Step 1: Generate nodes.yml

**Option A — Auto-discover local machine:**
```bash
cd scripts
python3 discover_nodes.py --local --role rank0 \
    --central-victoria-metrics <VM_HOST:PORT> \
    --central-otel <OTEL_HOST:PORT> \
    --central-opik <OPIK_HOST:PORT> \
    --ssh-user <USER> --ssh-key <KEY_PATH> \
    > ../edge/config/nodes.yml
```

**Option B — Copy template and edit:**
```bash
cp edge/config/nodes.yml.template edge/config/nodes.yml
# edit edge/config/nodes.yml
```

**Option C — Discover remote host via SSH:**
```bash
python3 discover_nodes.py --host <REMOTE_HOST> --role rank1 \
    --ssh-user <USER> --ssh-key <KEY_PATH> \
    --central-victoria-metrics <VM_HOST:PORT> \
    --central-otel <OTEL_HOST:PORT> \
    --central-opik <OPIK_HOST:PORT> \
    >> ../edge/config/nodes.yml
```

### Step 2: Validate SSH Connectivity

```bash
cd scripts
./deploy.sh validate
```

All nodes must report `OK` before deploying.

### Step 3: Configure edge.env

```bash
cp edge/config/edge.env.template edge/config/edge.env
# edit edge/config/edge.env (feature flags, listen ports, log paths)
```

### Step 4: Start the Edge Bundle

**Via launchd (macOS):**
```bash
cp edge/com.mlx.monitoring.bundle.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mlx.monitoring.bundle.plist
launchctl start com.mlx.monitoring.bundle
```

**Via systemd (Linux):**
```bash
sudo cp edge/com.mlx.monitoring-bundle.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now com.mlx.monitoring-bundle
```

**Manually:**
```bash
cd edge
export MLX_MACBOOK_ID=dk-mac
python3 mlx_monitoring_bundle.py start
```

**Check status:**
```bash
python3 mlx_monitoring_bundle.py status
```

**Stop:**
```bash
python3 mlx_monitoring_bundle.py stop
```

### Step 5: Deploy Central Stack

```bash
cd scripts
./deploy.sh central
```

Access:
- Grafana: http://<central-ip>:3000
- VictoriaMetrics: http://<central-ip>:8428
- Opik: http://<central-ip>:32173

### Step 6: Render Template Configs

vmagent.yml and otelcol-config.yaml use `${VAR}` placeholders:

```bash
export CENTRAL_VICTORIA_METRICS=192.168.1.100:8428
export CENTRAL_OTEL=192.168.1.100:4318
export CENTRAL_OPIK=192.168.1.100:32173
export OTLP_SCHEME=http
export MACBOOK_ID=dk-mac
export MACBOOK_SERIAL=ASHU1343
./deploy.sh templates
```

This produces `vmagent.yml.rendered` and `otelcol-config.yaml.rendered`.

---

## Opik SDK Tracing

`opik>=1.4` is included in `edge/requirements.txt`.

**What it does:** The proxy logs each chat completion as an Opik trace with an
LLM span (input/output/usage/metadata), hallucination quality feedback scores
(0..1), and token confidence feedback scores.

**What you need to do:**
1. Run Opik server on the central host (part of `docker-compose.central.yml`):
   ```bash
   cd central
   docker compose up -d opik-server opik-postgres opik-redis opik-clickhouse
   ```
2. Set `CENTRAL_OPIK=<host:port>` in `nodes.yml` (default port 32173).
3. Start the edge bundle with `--opik-endpoint http://<CENTRAL_OPIK>` or set
   `OPIK_ENDPOINT` env var.
4. The proxy automatically creates traces in the "mlx" project. View them in
   the Opik UI at http://<central-ip>:32173.

**No code changes needed** — the proxy already initializes the Opik SDK when
`--opik-endpoint` is provided.

---

## Security

- **No credentials on disk:** SSH keys are optional; passwords are prompted
  via `getpass` only in the short-lived `validate_nodes.py` and cleared from
  memory on exit. Long-running daemons require `ssh_key` in `nodes.yml`.
- **No hardcoded IPs:** All central endpoints come from `nodes.yml` or flags.
- **OTLP TLS:** `OTLP_SCHEME=http` and `OTLP_VERIFY_TLS=false` by default.
  For production, set `OTLP_SCHEME=https` and `OTLP_VERIFY_TLS=true` with
  proper CA certificates.
- **No root:** All exporters bind to unprivileged ports (>1024). No sudo
  required except optional CPU temperature via `powermetrics -n 1 --samplers smc`.

---

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| No metrics in VictoriaMetrics | vmagent not rendering template vars | Run `./deploy.sh templates` with `CENTRAL_VICTORIA_METRICS` set |
| `MLX_DEVICE_SILENT` firing | hw exporter not running or vmagent can't scrape | Check `:9102/metrics` on device; verify vmagent config |
| Model gauges at 0 | `MLX_MODEL` unset and `/v1/models` unreachable | Set `MLX_MODEL` in `nodes.yml` or ensure upstream is reachable |
| Opik traces missing | `OPIK_ENDPOINT` not set or Opik server down | Set `OPIK_ENDPOINT=http://<CENTRAL_OPIK>`; verify Opik server |
| Crash loop on restart | Server dying repeatedly | Check `server.log` and `mlx_server_crashes_total{reason}` |
| Permission denied on logs | `LOG_DIR` not writable | Set `LOG_DIR` to a user-writable path in `edge.env` |

---

