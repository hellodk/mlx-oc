#!/usr/bin/env bash
# deploy.sh
# =========
# One-command deployment of the mlx monitoring bundle.
#
# Edge (native Python, runs on each Macbook/Linux):
#   - validate: SSH connectivity check for config/nodes.yml
#   - edge:     start the bundle via mlx_monitoring_bundle.py (or plist/systemd)
#
# Central (containers, runs on the observability server):
#   - central:  start VictoriaMetrics, OTel, Grafana, Opik, etc.
#
# Usage:
#   ./deploy.sh validate              # validate SSH connectivity to nodes
#   ./deploy.sh edge <macbook_id>     # start edge bundle on a Macbook
#   ./deploy.sh central               # deploy central observability stack
#   ./deploy.sh templates             # substitute template vars in vmagent.yml + otelcol-config.yaml
#
# No files outside cluster/mlx_monitoring_bundle/ are modified.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
EDGE_DIR="${BUNDLE_DIR}/edge"
CENTRAL_DIR="${BUNDLE_DIR}/central"

info() { printf '[deploy] %s\n' "$*" >&2; }
err()  { printf '[deploy][ERR] %s\n' "$*" >&2; }

cmd_validate() {
  python3 "${EDGE_DIR}/validate_nodes.py" \
    --nodes "${EDGE_DIR}/config/nodes.yml" "$@"
}

cmd_edge() {
  local macbook_id="${1:-}"
  if [[ -z "${macbook_id}" ]]; then
    err "usage: $0 edge <macbook_id>"
    exit 2
  fi
  info "starting edge bundle for ${macbook_id}"
  export MLX_MACBOOK_ID="${macbook_id}"
  cd "${EDGE_DIR}"
  python3 mlx_monitoring_bundle.py start
}

cmd_central() {
  info "deploying central observability stack"
  cd "${CENTRAL_DIR}"
  docker compose -f docker-compose.central.yml up -d 2>/dev/null || \
  podman compose -f docker-compose.central.yml up -d
  info "central stack started"
}

cmd_templates() {
  info "substituting template variables in vmagent.yml and otelcol-config.yaml"
  cd "${BUNDLE_DIR}"

  # Substitute in edge vmagent config
  if [[ -f "${EDGE_DIR}/vmagent-edge/vmagent.yml" ]]; then
    envsubst < "${EDGE_DIR}/vmagent-edge/vmagent.yml" \
      > "${EDGE_DIR}/vmagent-edge/vmagent.yml.rendered" 2>/dev/null || {
      err "envsubst not found; install gettext or set vars manually"
      exit 1
    }
    info "rendered ${EDGE_DIR}/vmagent-edge/vmagent.yml.rendered"
  fi

  # Substitute in central otelcol config
  if [[ -f "${CENTRAL_DIR}/config/otelcol-config.yaml" ]]; then
    envsubst < "${CENTRAL_DIR}/config/otelcol-config.yaml" \
      > "${CENTRAL_DIR}/config/otelcol-config.yaml.rendered" 2>/dev/null || {
      err "envsubst not found; install gettext or set vars manually"
      exit 1
    }
    info "rendered ${CENTRAL_DIR}/config/otelcol-config.yaml.rendered"
  fi
}

case "${1:-}" in
  validate) shift; cmd_validate "$@" ;;
  edge)     shift; cmd_edge "$@" ;;
  central)  cmd_central ;;
  templates) cmd_templates ;;
  *)
    err "usage: $0 {validate|edge <macbook_id>|central|templates}"
    exit 2
    ;;
esac
