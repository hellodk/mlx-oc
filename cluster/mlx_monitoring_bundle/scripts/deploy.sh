#!/usr/bin/env bash
# deploy.sh
# =========
# One-command deployment of the mlx monitoring bundle.
#
# Edge (native Python, runs on each Mac):
#   - validate: check config/nodes.yml and local prerequisites
#   - edge:     start the bundle via mlx_monitoring_bundle.py
#
# Central (containers, runs on the observability server):
#   - central:  start VictoriaMetrics, OTel, Grafana, vmalert, alertmanager
#   - central --opik: also start the self-hosted Opik services
#
# Every Mac is a standalone node. Run `edge` on each Mac; no SSH or rank
# orchestration is involved.
#
# Usage:
#   ./deploy.sh validate
#   ./deploy.sh edge [macbook_id]
#   ./deploy.sh central [--opik]
#
# No files outside cluster/mlx_monitoring_bundle/ are modified.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
EDGE_DIR="${BUNDLE_DIR}/edge"
CENTRAL_DIR="${BUNDLE_DIR}/central"
ENV_FILE="${EDGE_DIR}/config/edge.env"

info() { printf '[deploy] %s\n' "$*" >&2; }
err()  { printf '[deploy][ERR] %s\n' "$*" >&2; }

# The bundle imports PyYAML, so prefer the monitoring venv from edge.env when
# it exists. System Python on macOS does not ship PyYAML.
bundle_python() {
  local candidate=""
  if [[ -f "${ENV_FILE}" ]]; then
    candidate="$(sed -n 's/^VENV_PYTHON=//p' "${ENV_FILE}" | tail -1 | tr -d '"')"
  fi
  if [[ -n "${candidate}" && -x "${candidate}" ]]; then
    printf '%s' "${candidate}"
    return
  fi
  printf '%s' "python3"
}

cmd_validate() {
  "$(bundle_python)" "${EDGE_DIR}/validate_nodes.py" \
    --nodes "${EDGE_DIR}/config/nodes.yml" "$@"
}

VMAGENT_RELEASE_URL="https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/v1.155.0/vmutils-darwin-arm64-v1.155.0.tar.gz"

ensure_vmagent() {
  local target="${1:-${HOME}/bin/vmagent}"
  if command -v vmagent >/dev/null 2>&1; then
    info "vmagent binary found in PATH: $(command -v vmagent)"
    return 0
  fi
  if [[ -x "${target}" ]]; then
    info "vmagent binary found at: ${target}"
    return 0
  fi

  info "vmagent not found. Downloading official Darwin arm64 release..."
  mkdir -p "$(dirname "${target}")"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  curl -fsSL "${VMAGENT_RELEASE_URL}" -o "${tmp_dir}/vmutils.tar.gz"
  tar -xzf "${tmp_dir}/vmutils.tar.gz" -C "${tmp_dir}"
  mv "${tmp_dir}/vmagent-prod" "${target}"
  chmod +x "${target}"
  rm -rf "${tmp_dir}"
  info "vmagent successfully installed to: ${target}"
}

cmd_setup_edge() {
  local central_ip="${1:-192.168.1.10}"
  local model_id="${2:-Qwen3.5-4B-MLX-4bit}"
  local model_path="${3:-${HOME}/mlx-models/${model_id}}"
  local vmagent_bin="${HOME}/bin/vmagent"

  info "setting up edge monitoring bundle on local machine"
  ensure_vmagent "${vmagent_bin}"

  mkdir -p "${EDGE_DIR}/config"
  mkdir -p "${EDGE_DIR}/logs"
  mkdir -p "${EDGE_DIR}/bin"

  # Find local python virtualenvs
  local mon_venv="${HOME}/bench/venv-mon"
  if [[ ! -d "${mon_venv}" && -d "${HOME}/mlx-monitoring-venv" ]]; then
    mon_venv="${HOME}/mlx-monitoring-venv"
  fi
  local mlx_venv="${HOME}/venvs/mlx"
  if [[ ! -d "${mlx_venv}" && -d "${HOME}/mlx-venv" ]]; then
    mlx_venv="${HOME}/mlx-venv"
  fi

  if [[ ! -f "${EDGE_DIR}/config/nodes.yml" ]]; then
    info "generating ${EDGE_DIR}/config/nodes.yml via discover_nodes.py"
    python3 "${SCRIPT_DIR}/discover_nodes.py" --local \
      --central-victoria-metrics "${central_ip}:8428" \
      --central-otel "${central_ip}:4318" \
      --central-opik "${central_ip}:32173" \
      --model "${model_id}" \
      --model-path "${model_path}" \
      > "${EDGE_DIR}/config/nodes.yml"
  else
    info "${EDGE_DIR}/config/nodes.yml already exists; preserving"
  fi

  if [[ ! -f "${ENV_FILE}" ]]; then
    info "generating ${ENV_FILE}"
    cat <<EOF > "${ENV_FILE}"
ENABLE_PROXY=true
ENABLE_HW_TELEMETRY=true
ENABLE_KV_AGENT=true
ENABLE_SUPERVISOR=true
ENABLE_LOGTAILER=true
ENABLE_VMAGENT=true

LOG_LEVEL=INFO
LOG_ROTATION_MAX_MB=50
LOG_ROTATION_BACKUPS=5

LOG_DIR=./logs
PID_DIR=./bin
CONFIG_DIR=./config

VENV_PYTHON=${mon_venv}/bin/python
MLX_VENV_PYTHON=${mlx_venv}/bin/python

MLX_UPSTREAM=http://127.0.0.1:11234
MLX_SERVER_PORT=11234

SUPERVISOR_ATTACH_HEALTHY=true
MLX_PROXY_STRICT_MODEL=false

MLX_NET_IFACE=

ENABLE_CONTAINER_STATS=false
MLX_SERVER_LOG=${EDGE_DIR}/logs/server.log

VMAGENT_SCRAPE_INTERVAL=10s
VMAGENT_QUEUE_CAPACITY=5000
VMAGENT_MAX_SHARDS=200
VMAGENT_BINARY=${vmagent_bin}
VMAGENT_PERSISTENT_QUEUE=

OTLP_VERIFY_TLS=false
EOF
  else
    info "${ENV_FILE} already exists; preserving"
  fi

  info "edge setup complete; validating configuration"
  cmd_validate
}

cmd_edge() {
  local action="${1:-start}"
  shift || true
  local macbook_id="${1:-}"

  if [[ -n "${macbook_id}" ]]; then
    export MLX_MACBOOK_ID="${macbook_id}"
  fi

  cd "${EDGE_DIR}"
  case "${action}" in
    start)
      info "starting edge bundle"
      exec "$(bundle_python)" mlx_monitoring_bundle.py start
      ;;
    daemon)
      info "starting edge bundle in background (daemon mode)"
      nohup "$(bundle_python)" mlx_monitoring_bundle.py start > "${EDGE_DIR}/logs/deploy.log" 2>&1 &
      sleep 2
      "$(bundle_python)" mlx_monitoring_bundle.py status
      ;;
    stop)
      info "stopping edge bundle"
      "$(bundle_python)" mlx_monitoring_bundle.py stop
      ;;
    status)
      "$(bundle_python)" mlx_monitoring_bundle.py status
      ;;
    *)
      # Backwards compatibility: ./deploy.sh edge [macbook_id]
      export MLX_MACBOOK_ID="${action}"
      info "starting edge bundle for ${action}"
      exec "$(bundle_python)" mlx_monitoring_bundle.py start
      ;;
  esac
}

render_alertmanager_config() {
  local tpl="${CENTRAL_DIR}/config/alertmanager.yml.template"
  local dst="${CENTRAL_DIR}/config/alertmanager.yml"
  if [[ ! -f "${tpl}" ]]; then
    return 0
  fi

  local oncall_url="${ONCALL_WEBHOOK_URL:-${ALERT_WEBHOOK_URL:-}}"
  local default_url="${ALERT_WEBHOOK_URL:-}"

  local oncall_cfg="webhook_configs: []"
  if [[ -n "${oncall_url}" ]]; then
    oncall_cfg="webhook_configs:\n      - url: \"${oncall_url}\"\n        send_resolved: true"
  fi

  local default_cfg="webhook_configs: []"
  if [[ -n "${default_url}" ]]; then
    default_cfg="webhook_configs:\n      - url: \"${default_url}\"\n        send_resolved: true"
  fi

  python3 -c "
import sys
tpl = open('${tpl}', 'r').read()
tpl = tpl.replace('{{ONCALL_WEBHOOK_CONFIG}}', '''${oncall_cfg}''')
tpl = tpl.replace('{{DEFAULT_WEBHOOK_CONFIG}}', '''${default_cfg}''')
open('${dst}', 'w').write(tpl)
"
}

cmd_central() {
  local with_opik=0
  if [[ "${1:-}" == "--opik" ]]; then
    with_opik=1
  fi
  if ! command -v docker >/dev/null 2>&1 && ! command -v podman >/dev/null 2>&1; then
    err "docker or podman is required for the central stack"
    exit 2
  fi
  local engine="docker"
  if ! command -v docker >/dev/null 2>&1; then
    engine="podman"
  fi
  render_alertmanager_config
  cd "${CENTRAL_DIR}"
  if [[ "${with_opik}" == "1" ]]; then
    info "deploying central observability stack with Opik"
    "${engine}" compose --profile opik -f docker-compose.central.yml up -d
  else
    info "deploying central observability stack"
    "${engine}" compose -f docker-compose.central.yml up -d
    info "Opik is available with: $0 central --opik"
  fi
  info "central stack started"
}

case "${1:-}" in
  validate)   shift; cmd_validate "$@" ;;
  setup-edge) shift; cmd_setup_edge "$@" ;;
  edge)       shift; cmd_edge "$@" ;;
  edge-all)   shift; cmd_edge "$@" ;;
  central)    shift; cmd_central "$@" ;;
  *)
    err "usage: $0 {validate|setup-edge [central_ip] [model_id] [model_path]|edge [start|daemon|stop|status] [macbook_id]|edge-all|central [--opik]}"
    exit 2
    ;;
esac
