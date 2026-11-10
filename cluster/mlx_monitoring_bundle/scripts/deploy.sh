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

cmd_edge() {
  local macbook_id="${1:-}"
  if [[ -n "${macbook_id}" ]]; then
    export MLX_MACBOOK_ID="${macbook_id}"
    info "starting edge bundle for ${macbook_id}"
  else
    info "starting edge bundle (single enabled node is selected automatically)"
  fi
  cd "${EDGE_DIR}"
  exec "$(bundle_python)" mlx_monitoring_bundle.py start
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
  validate) shift; cmd_validate "$@" ;;
  edge)     shift; cmd_edge "$@" ;;
  edge-all) shift; cmd_edge "$@" ;;
  central)  shift; cmd_central "$@" ;;
  *)
    err "usage: $0 {validate|edge [macbook_id]|edge-all|central [--opik]}"
    exit 2
    ;;
esac
