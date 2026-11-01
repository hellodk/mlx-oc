# mlx-oc Monitoring Bundle

Push-based observability for Macbooks running local LLMs (mlx_lm.server).
Sends metrics/traces/logs to a central **VictoriaMetrics + Grafana + Opik** stack.
No Prometheus scraping — Macbooks initiate all outbound connections.

## Architecture

```
Macbook (edge, native Python)                    Central (VictoriaMetrics + Grafana + Opik)
┌─────────────────────────┐                      ┌─────────────────────────────────────┐
│ mlx_metrics_proxy    :8080│────OTLP traces/logs─┤ otel-collector     :4318          │
│ mlx_hw_telemetry     :9102│                      │                                     │
│ mlx_kv_cache_agent   :9104│──── Prometheus scrape│ victoria-metrics   :8428          │
│ mlx_server_supervisor:9105│   (vmagent sidecar)  │                                     │
│ mlx_server_log_tailer:9106│                      │ grafana             :3000         │
│                        │                      │ vmalert + alertmanager        │
│                        │                      │ opik server + db              │
└─────────────────────────┘                      └─────────────────────────────────────┘
        │                                              │
        │ push (remote_write, OTLP)                    │
        └───────────────── static central IP ──────────┘
```

## Directory Layout

```
cluster/mlx_monitoring_bundle/
├── edge/                              # runs on each Macbook (native Python)
│   ├── config/
│   │   ├── nodes.yml                  # node registry + shared central endpoints
│   │   ├── nodes.yml.template         # documented template (copy & fill in)
│   │   ├── edge.env                   # runtime overrides
│   │   └── edge.env.template          # documented template (copy & fill in)
│   ├── exporters/
│   │   ├── mlx_logging.py             # shared structured JSON logger
│   │   ├── mlx_metrics_proxy.py       # :8080 OpenAI proxy + Prometheus + OTLP + Opik
│   │   ├── mlx_hw_telemetry.py        # :9102 hardware telemetry
│   │   ├── mlx_kv_cache_agent.py      # :9104 KV-cache + context-length gauges
│   │   ├── mlx_server_supervisor.py   # :9105 mlx_lm.server supervisor + auto-restart
│   │   └── mlx_server_log_tailer.py   # :9106 server.log -> OTLP logs
│   ├── vmagent-edge/
│   │   └── vmagent.yml                # vmagent scrape config (template vars)
│   ├── mlx_monitoring_bundle.py       # edge entrypoint (start|stop|status|restart)
│   ├── validate_nodes.py              # SSH connectivity check
│   ├── requirements.txt               # pip dependencies
│   ├── com.mlx.monitoring.bundle.plist # launchd (macOS)
│   └── com.mlx.monitoring-bundle.service # systemd (Linux)
├── central/                           # runs on central observability server
│   ├── config/
│   │   ├── otelcol-config.yaml
│   │   ├── alertmanager.yml
│   │   ├── vmalert-rules.yml
│   │   └── grafana/
│   │       ├── datasources.yaml
│   │       └── dashboards.yaml
│   ├── dashboards/                    # 12 dashboards, IST + 5m refresh
│   ├── docker-compose.central.yml
│   └── Dockerfile.central
├── scripts/                           # shared, node-agnostic
│   ├── build_and_push.sh              # build central image (quay.io/hellodk/hydra:central)
│   ├── deploy.sh                      # validate / edge / central / templates
│   └── discover_nodes.py              # auto-generate nodes.yml via SSH/system_profiler
├── .gitignore                         # __pycache__, logs/, bin/, *.pyc
└── README.md
```

## Quick Start

### 1. Install dependencies on each Macbook

```bash
cd cluster/mlx_monitoring_bundle/edge
pip install -r requirements.txt
```

### 2. Generate nodes.yml

**Option A — auto-discover the local machine:**

```bash
cd cluster/mlx_monitoring_bundle/scripts
python3 discover_nodes.py --local --role rank0 \
    --central-victoria-metrics 192.168.1.100:8428 \
    --central-otel 192.168.1.100:4318 \
    --central-opik 192.168.1.100:32173 \
    --ssh-user dk --ssh-key ~/.ssh/id_rsa \
    > ../edge/config/nodes.yml
```

**Option B — copy the template and edit manually:**

```bash
cp edge/config/nodes.yml.template edge/config/nodes.yml
# edit edge/config/nodes.yml
```

**Option C — discover a remote Macbook via SSH:**

```bash
python3 discover_nodes.py --host 192.168.1.11 --role rank1 \
    --ssh-user ashu --ssh-key ~/.ssh/id_rsa \
    --central-victoria-metrics 192.168.1.100:8428 \
    --central-otel 192.168.1.100:4318 \
    --central-opik 192.168.1.100:32173 \
    >> ../edge/config/nodes.yml
```

### 3. Validate SSH connectivity

```bash
cd cluster/mlx_monitoring_bundle/scripts
./deploy.sh validate
```

### 4. Configure edge.env

```bash
cp edge/config/edge.env.template edge/config/edge.env
# edit edge/config/edge.env (feature flags, listen ports, log paths, OTLP scheme)
```

### 5. Start the edge bundle on a Macbook

**Via launchd (macOS, recommended):**

```bash
cp edge/com.mlx.monitoring.bundle.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mlx.monitoring.bundle.plist
launchctl start com.mlx.monitoring.bundle
```

**Via systemd (Linux):**

```bash
sudo cp edge/com.mlx.monitoring-bundle.service /etc/systemd/system/
sudo systemctl enable --now com.mlx.monitoring-bundle
```

**Manually:**

```bash
cd cluster/mlx_monitoring_bundle/edge
export MLX_MACBOOK_ID=dk-mac
python3 mlx_monitoring_bundle.py start
```

### 6. Deploy the central stack

On the central Linux server:

```bash
cd cluster/mlx_monitoring_bundle/scripts
./deploy.sh central
```

Grafana: http://<central-ip>:3000
VictoriaMetrics: http://<central-ip>:8428
Opik: http://<central-ip>:32173

### 7. Render template configs

vmagent.yml and otelcol-config.yaml use `${VAR}` placeholders. Render them:

```bash
export CENTRAL_VICTORIA_METRICS=192.168.1.100:8428
export CENTRAL_OTEL=192.168.1.100:4318
export CENTRAL_OPIK=192.168.1.100:32173
export OTLP_SCHEME=http
export MACBOOK_ID=dk-mac
export MACBOOK_SERIAL=ASHU1343
./deploy.sh templates
```

## Configuration

### nodes.yml (single source of truth)

| Section | Field | Description |
|---------|-------|-------------|
| `central` | `victoria_metrics` | host:port of central VictoriaMetrics |
| `central` | `otel` | host:port of central OTel collector |
| `central` | `opik` | host:port of central Opik server |
| `macbooks[]` | `id` | unique node identifier |
| `macbooks[]` | `serial` | hardware serial (optional) |
| `macbooks[]` | `ip` | node IP address |
| `macbooks[]` | `ssh_user` | SSH user (optional) |
| `macbooks[]` | `ssh_key` | SSH private key path (optional; password prompted if omitted) |
| `macbooks[]` | `role` | `rank0` or `rank1` |
| `macbooks[]` | `enabled` | true/false |
| `macbooks[]` | `model` | pinned model (optional; auto-discovered from upstream if unset) |

### edge.env (runtime overrides)

Only runtime flags live here. Node identity lives in nodes.yml.

- `ENABLE_PROXY`, `ENABLE_HW_TELEMETRY`, `ENABLE_KV_AGENT`, `ENABLE_SUPERVISOR`, `ENABLE_LOGTAILER` — feature toggles
- `PROXY_LISTEN`, `HW_LISTEN`, `KV_LISTEN`, `SUPERVISOR_LISTEN`, `LOGTAILER_LISTEN` — listen addresses
- `MLX_LOGPROBS`, `MLX_LOW_CONFIDENCE`, `MAX_PROMPT_TOKENS`, `MAX_TOKENS_CAP` — proxy guardrails
- `LOG_LEVEL`, `LOG_ROTATION_MAX_MB`, `LOG_ROTATION_BACKUPS` — logging
- `LOG_DIR`, `PID_DIR`, `CONFIG_DIR` — paths
- `VENV_PYTHON`, `MLX_VENV_PYTHON`, `MLX_LAUNCH` — Python venv paths
- `VMAGENT_SCRAPE_INTERVAL`, `VMAGENT_QUEUE_CAPACITY`, `VMAGENT_MAX_SHARDS` — vmagent tuning
- `MLX_MODEL` — optional pinned model (auto-discovered if unset)
- `OTLP_SCHEME`, `OTLP_VERIFY_TLS` — OTLP transport (`http`/`https`, `true`/`false`)

### MLX Model — optional auto-discovery

`MLX_MODEL` is optional. If unset, exporters discover the loaded model from
upstream `GET /v1/models`. If discovery fails, model-specific gauges are
skipped with a `[degraded]` log entry — all other monitoring continues.

### Security

- No SSH keys or passwords are stored on disk by the bundle.
- For long-running daemons, configure `ssh_key` in nodes.yml. Password prompts
  are only used by the short-lived `validate_nodes.py` tool.
- `OTLP_VERIFY_TLS=false` by default; set `true` with proper CA certs for production.

## Push Model

- Macbooks initiate **all** outbound connections (remote_write + OTLP).
- No inbound ports required on the Mac.
- All binding on 0.0.0.0, no root permissions required.
- TLS is configurable via `OTLP_SCHEME` and `OTLP_VERIFY_TLS`.

## Logs

Structured JSON logging to stderr plus rotating file logs under `edge/logs/`.
Configure rotation via `LOG_ROTATION_MAX_MB` and `LOG_ROTATION_BACKUPS`.
