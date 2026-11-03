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

cmd_edge_all() {
  info "starting edge bundle on all enabled nodes from nodes.yml"
  local nodes_file="${EDGE_DIR}/config/nodes.yml"
  if [[ ! -f "${nodes_file}" ]]; then
    err "nodes.yml not found at ${nodes_file}"
    exit 2
  fi
  # Parse nodes.yml for enabled macbook IDs
  local macbook_ids
  macbook_ids=$(python3 -c "
import yaml, sys
with open('${nodes_file}') as f:
    data = yaml.safe_load(f)
for nb in data.get('macbooks', []):
    if nb.get('enabled', True):
        print(nb['id'])
" 2>/dev/null) || {
    err "failed to parse nodes.yml"
    exit 2
  }
  for macbook_id in ${macbook_ids}; do
    info "starting edge bundle for ${macbook_id}"
    export MLX_MACBOOK_ID="${macbook_id}"
    cd "${EDGE_DIR}"
    python3 mlx_monitoring_bundle.py start &
  done
  wait
  info "all edge bundles started"
}

case "${1:-}" in
  validate) shift; cmd_validate "$@" ;;
  edge)     shift; cmd_edge "$@" ;;
  edge-all) cmd_edge_all ;;
  central)  cmd_central ;;
  templates) cmd_templates ;;
  *)
    err "usage: $0 {validate|edge <macbook_id>|edge-all|central|templates}"
    exit 2
    ;;
esac
