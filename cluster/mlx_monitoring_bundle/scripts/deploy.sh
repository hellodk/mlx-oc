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

info()  { printf '\033[1;34m[deploy]\033[0m %s\n' "$*" >&2; }
warn()  { printf '\033[1;33m[deploy]\033[0m %s\n' "$*" >&2; }
err()   { printf '\033[1;31m[deploy][ERR]\033[0m %s\n' "$*" >&2; }
die()   { printf '\033[1;31m[deploy][ERR]\033[0m %s\n' "$*" >&2; exit 1; }

_prompt_input() {
  local prompt="$1" default="${2:-}" val=""
  if [[ -n "${default}" ]]; then
    printf '%s [%s]: ' "${prompt}" "${default}" >&2
  else
    printf '%s: ' "${prompt}" >&2
  fi
  read -r val
  if [[ -z "${val}" ]]; then
    printf '%s' "${default}"
  else
    printf '%s' "${val}"
  fi
}

_prompt_select() {
  local prompt="$1"; shift
  local i=1
  printf '%s\n' "$prompt" >&2
  for item in "$@"; do printf '  %2d) %s\n' "$i" "$item" >&2; i=$((i+1)); done
  printf 'Select [1-%d]: ' "$#" >&2
  local choice; read -r choice
  [ -z "$choice" ] && return 1
  case "$choice" in ''|*[!0-9]*) return 1;; esac
  [ "$choice" -lt 1 ] || [ "$choice" -gt "$#" ] && return 1
  eval "echo \${$choice}"
}

prompt_confirm_word() {
  local verb="$1" expect="$2"
  printf 'Type "%s" to confirm %s: ' "$expect" "$verb" >&2
  local answer; read -r answer
  [ "$answer" = "$expect" ] || die "aborted (confirmation word mismatch)"
}

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

VMAGENT_VERSION="${VMAGENT_VERSION:-v1.155.0}"
MLX_MONITORING_DIR="${HOME}/mlx-monitoring"

_detect_vmagent_url() {
  local os_type arch version="${VMAGENT_VERSION}"
  os_type="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "${arch}" in
    x86_64|amd64) arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) die "unsupported system architecture: ${arch}" ;;
  esac
  case "${os_type}" in
    darwin) printf 'https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/%s/vmutils-darwin-%s-%s.tar.gz' "${version}" "${arch}" "${version}" ;;
    linux)  printf 'https://github.com/VictoriaMetrics/VictoriaMetrics/releases/download/%s/vmutils-linux-%s-%s.tar.gz' "${version}" "${arch}" "${version}" ;;
    *) die "unsupported operating system: ${os_type}" ;;
  esac
}

_add_path_to_shell_profile() {
  local bin_dir="$1"
  local line="export PATH=\"${bin_dir}:\$PATH\""

  # Update ~/.zshrc if zsh exists or is user shell
  if [[ -f "${HOME}/.zshrc" || "${SHELL:-}" == *"zsh"* ]]; then
    if ! grep -qs "${bin_dir}" "${HOME}/.zshrc" 2>/dev/null; then
      printf '\n# mlx-monitoring binary path\n%s\n' "${line}" >> "${HOME}/.zshrc"
      info "added ${bin_dir} to ${HOME}/.zshrc"
    fi
  fi

  # Update ~/.bashrc or ~/.bash_profile
  local bash_profile="${HOME}/.bashrc"
  if [[ "$(uname -s)" == "Darwin" && -f "${HOME}/.bash_profile" ]]; then
    bash_profile="${HOME}/.bash_profile"
  fi
  if ! grep -qs "${bin_dir}" "${bash_profile}" 2>/dev/null; then
    printf '\n# mlx-monitoring binary path\n%s\n' "${line}" >> "${bash_profile}"
    info "added ${bin_dir} to ${bash_profile}"
  fi
}

ensure_vmagent() {
  local bin_dir="${MLX_MONITORING_DIR}/bin"
  local target="${1:-${bin_dir}/vmagent}"

  if command -v vmagent >/dev/null 2>&1; then
    info "vmagent binary found in PATH: $(command -v vmagent)"
    return 0
  fi
  if [[ -x "${target}" ]]; then
    info "vmagent binary found at: ${target}"
    _add_path_to_shell_profile "${bin_dir}"
    return 0
  fi
  # Also check legacy ~/bin/vmagent
  if [[ -x "${HOME}/bin/vmagent" ]]; then
    info "vmagent binary found at legacy path: ${HOME}/bin/vmagent"
    mkdir -p "${bin_dir}"
    cp "${HOME}/bin/vmagent" "${target}"
    _add_path_to_shell_profile "${bin_dir}"
    return 0
  fi

  local download_url
  download_url="$(_detect_vmagent_url)"
  info "vmagent not found. Downloading official release from ${download_url}..."
  mkdir -p "${bin_dir}"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  curl -fsSL "${download_url}" -o "${tmp_dir}/vmutils.tar.gz"
  tar -xzf "${tmp_dir}/vmutils.tar.gz" -C "${tmp_dir}"
  mv "${tmp_dir}/vmagent-prod" "${target}"
  chmod +x "${target}"
  rm -rf "${tmp_dir}"
  _add_path_to_shell_profile "${bin_dir}"
  info "vmagent successfully installed to: ${target} (no root required)"
}

ensure_monitoring_venv() {
  local venv_dir="${MLX_MONITORING_DIR}/venv"
  local venv_python="${venv_dir}/bin/python"

  # First check if an existing functional venv is available
  local existing
  existing="$(_detect_venv || true)"
  if [[ -n "${existing}" && -x "${existing}" ]]; then
    printf '%s' "${existing}"
    return 0
  fi

  # Otherwise create the default venv in ~/mlx-monitoring/venv
  info "creating default monitoring virtualenv at ${venv_dir}..."
  mkdir -p "${MLX_MONITORING_DIR}"
  python3 -m venv "${venv_dir}"
  info "installing pinned monitoring dependencies into ${venv_dir}..."
  "${venv_python}" -m pip install --quiet --upgrade pip
  "${venv_python}" -m pip install --quiet -r "${EDGE_DIR}/requirements.txt"
  info "monitoring virtualenv ready at ${venv_python}"
  printf '%s' "${venv_python}"
}

_detect_venv() {
  local candidates=(
    "${VENV_PYTHON:-}"
    "${MLX_MONITORING_DIR}/venv/bin/python"
    "${HOME}/bench/venv-mon/bin/python"
    "${HOME}/mlx-monitoring-venv/bin/python"
    "${HOME}/.venv/bin/python"
    "${BUNDLE_DIR}/.venv/bin/python"
  )
  for c in "${candidates[@]}"; do
    if [[ -n "${c}" && -x "${c}" ]]; then
      if "${c}" -c 'import yaml, prometheus_client' >/dev/null 2>&1; then
        printf '%s' "${c}"
        return 0
      fi
    fi
  done
  return 1
}

_detect_mlx_venv() {
  local candidates=(
    "${MLX_VENV_PYTHON:-}"
    "${HOME}/venvs/mlx/bin/python"
    "${HOME}/mlx-venv/bin/python"
    "${HOME}/.venv-mlx/bin/python"
  )
  for c in "${candidates[@]}"; do
    if [[ -n "${c}" && -x "${c}" ]]; then
      printf '%s' "${c}"
      return 0
    fi
  done
  if command -v python3 >/dev/null 2>&1; then
    if python3 -c 'import mlx_lm' >/dev/null 2>&1; then
      command -v python3
      return 0
    fi
  fi
  return 1
}

cmd_setup_edge() {
  local central_ip="${1:-}"
  local model_id="${2:-}"
  local model_path="${3:-}"
  local vmagent_bin="${MLX_MONITORING_DIR}/bin/vmagent"

  info "setting up edge monitoring bundle on local machine"
  mkdir -p "${MLX_MONITORING_DIR}"
  ensure_vmagent "${vmagent_bin}"

  mkdir -p "${EDGE_DIR}/config"
  mkdir -p "${EDGE_DIR}/logs"
  mkdir -p "${EDGE_DIR}/bin"

  # Interactive prompt if parameters are omitted and not provided in environment
  if [[ -z "${central_ip}" ]]; then
    central_ip="$(_prompt_input "Central VictoriaMetrics IP / Host (e.g. 192.168.1.10)")"
    [[ -n "${central_ip}" ]] || die "central_ip is required"
  fi

  if [[ -z "${model_id}" ]]; then
    model_id="$(_prompt_input "Model ID (press Enter to skip or specify served model)")"
  fi

  if [[ -n "${model_id}" && -z "${model_path}" ]]; then
    local default_mpath=""
    if [[ -d "${HOME}/mlx-models/${model_id}" ]]; then
      default_mpath="${HOME}/mlx-models/${model_id}"
    elif [[ -d "${HOME}/.cache/huggingface/hub" ]]; then
      default_mpath="${HOME}/.cache/huggingface/hub"
    fi
    model_path="$(_prompt_input "Local Model Directory Path" "${default_mpath}")"
  fi

  # Ensure virtualenv (creates ~/mlx-monitoring/venv if none exists)
  local mon_python mlx_python
  mon_python="$(ensure_monitoring_venv)"

  mlx_python="$(_detect_mlx_venv || true)"
  if [[ -z "${mlx_python}" ]]; then
    warn "Could not auto-detect a virtualenv with mlx_lm installed."
    mlx_python="$(_prompt_input "Path to MLX Python binary (press Enter to use monitoring python)" "${mon_python}")"
  fi

  # Generate nodes.yml using the monitoring python (which has pyyaml)
  local disc_cmd=(
    "${mon_python}" "${SCRIPT_DIR}/discover_nodes.py" --local
    --central-victoria-metrics "${central_ip}:8428"
    --central-otel "${central_ip}:4318"
    --central-opik "${central_ip}:32173"
  )
  if [[ -n "${model_id}" ]]; then
    disc_cmd+=(--model "${model_id}")
  fi
  if [[ -n "${model_path}" ]]; then
    disc_cmd+=(--model-path "${model_path}")
  fi

  info "generating ${EDGE_DIR}/config/nodes.yml via discover_nodes.py"
  "${disc_cmd[@]}" > "${EDGE_DIR}/config/nodes.yml"

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

VENV_PYTHON=${mon_python}
MLX_VENV_PYTHON=${mlx_python}

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

usage() {
  cat >&2 <<EOF
scripts/deploy.sh — unified CLI for mlx monitoring bundle operations

Usage:
  ./scripts/deploy.sh                             interactive menu
  ./scripts/deploy.sh setup-edge [args]           interactive or parameterized edge setup
  ./scripts/deploy.sh edge [start|daemon|stop|status] [node_id]  manage edge services
  ./scripts/deploy.sh validate                    validate edge configuration
  ./scripts/deploy.sh central [--opik]            deploy central Docker/Podman stack

Setup-edge arguments:
  ./scripts/deploy.sh setup-edge <central_ip> [model_id] [model_path]
  If arguments are omitted, the script asks interactively.

Edge arguments:
  ./scripts/deploy.sh edge start                  start in foreground
  ./scripts/deploy.sh edge daemon                 start in background
  ./scripts/deploy.sh edge status                 show process table and central targets
  ./scripts/deploy.sh edge stop                   cleanly stop all edge exporters

EOF
}

main() {
  case "${1:-}" in
    validate)   shift; cmd_validate "$@" ;;
    setup-edge) shift; cmd_setup_edge "$@" ;;
    edge)       shift; cmd_edge "$@" ;;
    edge-all)   shift; cmd_edge "$@" ;;
    central)    shift; cmd_central "$@" ;;
    help|-h|--help) usage ;;
    "")
      # Interactive menu mode (following scripts/avika pattern)
      local choice
      choice=$(_prompt_select "MLX Monitoring Bundle operations:" \
        "setup-edge     — setup edge node (detect venvs, vmagent, generate config)" \
        "edge daemon    — start edge monitoring stack in background" \
        "edge status    — check running status of edge exporters" \
        "edge stop      — stop edge monitoring bundle" \
        "edge start     — start edge bundle in foreground" \
        "validate       — validate nodes.yml and edge.env" \
        "central        — deploy central observability stack" \
        "central --opik — deploy central observability stack with Opik" \
        "quit") || die "bye"

      case "${choice}" in
        setup-edge*) cmd_setup_edge ;;
        "edge daemon"*) cmd_edge daemon ;;
        "edge status"*) cmd_edge status ;;
        "edge stop"*) cmd_edge stop ;;
        "edge start"*) cmd_edge start ;;
        validate*) cmd_validate ;;
        "central --opik"*) cmd_central --opik ;;
        central*) cmd_central ;;
        quit) die "bye" ;;
      esac
      ;;
    *)
      usage
      die "unknown command: $1"
      ;;
  esac
}

main "$@"
