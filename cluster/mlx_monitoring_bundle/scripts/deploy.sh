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

_ensure_git_latest() {
  # If SKIP_GIT_PULL=1 is set, bypass auto-update
  if [[ "${SKIP_GIT_PULL:-0}" == "1" ]]; then
    return 0
  fi

  local git_root
  if command -v git >/dev/null 2>&1; then
    git_root="$(git -C "${BUNDLE_DIR}" rev-parse --show-toplevel 2>/dev/null || true)"
    if [[ -n "${git_root}" && -d "${git_root}/.git" ]]; then
      local current_branch
      current_branch="$(git -C "${git_root}" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
      if [[ -n "${current_branch}" && "${current_branch}" != "HEAD" ]]; then
        info "checking for repository updates on branch '${current_branch}'..."
        if ! git -C "${git_root}" pull --ff-only origin "${current_branch}" >&2; then
          warn "git pull failed or branch has diverged; continuing with local files"
        fi
      fi
    fi
  fi
}

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
  read -r val || true
  if [[ -z "${val}" ]]; then
    printf '%s' "${default}"
  else
    printf '%s' "${val}"
  fi
}

_prompt_password() {
  local prompt="$1" default="${2:-}" val=""
  if [[ -n "${default}" ]]; then
    printf '%s [%s]: ' "${prompt}" "${default}" >&2
  else
    printf '%s: ' "${prompt}" >&2
  fi
  read -r -s val || true
  printf '\n' >&2
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
  local choice; read -r choice || true
  [ -z "$choice" ] && return 1
  case "$choice" in ''|*[!0-9]*) return 1;; esac
  [ "$choice" -lt 1 ] || [ "$choice" -gt "$#" ] && return 1
  eval "echo \${$choice}"
}

prompt_confirm_word() {
  local verb="$1" expect="$2"
  printf 'Type "%s" to confirm %s: ' "$expect" "$verb" >&2
  local answer; read -r answer || true
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

  if [[ -t 0 ]]; then
    local review
    review="$(_prompt_input "Review vmagent configuration now? (y/N)" "N")"
    if [[ "${review}" =~ ^[yY] ]]; then
      cmd_review_vmagent
    fi
  fi
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
    vmagent-config|review-vmagent)
      cmd_review_vmagent
      ;;
    *)
      # Backwards compatibility: ./deploy.sh edge [macbook_id]
      export MLX_MACBOOK_ID="${action}"
      info "starting edge bundle for ${action}"
      exec "$(bundle_python)" mlx_monitoring_bundle.py start
      ;;
  esac
}

cmd_review_vmagent() {
  local py_bin
  py_bin="$(bundle_python)"
  if [[ -z "${py_bin}" ]] || ! command -v "${py_bin}" >/dev/null 2>&1; then
    py_bin="python3"
  fi

  if [[ ! -f "${ENV_FILE}" || ! -f "${EDGE_DIR}/config/nodes.yml" ]]; then
    warn "Edge configuration files not found (${ENV_FILE} or ${EDGE_DIR}/config/nodes.yml)."
    warn "Run './scripts/deploy.sh setup-edge' first."
    return 1
  fi

  cd "${EDGE_DIR}"
  "${py_bin}" -c "
import os, sys, yaml
from pathlib import Path

edge_dir = Path('${EDGE_DIR}')
env_path = edge_dir / 'config' / 'edge.env'
nodes_path = edge_dir / 'config' / 'nodes.yml'

env = {}
for line in env_path.read_text(encoding='utf-8').splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        env[k.strip()] = v.strip().strip('\"\'')

nodes = yaml.safe_load(nodes_path.read_text(encoding='utf-8')) or {}
central_vm = nodes.get('central', {}).get('victoria_metrics', env.get('CENTRAL_VICTORIA_METRICS', '127.0.0.1:8428'))
otlp_scheme = env.get('OTLP_SCHEME', 'http')

macbooks = [nb for nb in nodes.get('macbooks', []) if nb.get('enabled', True)]
macbook = macbooks[0] if macbooks else {}
macbook_id = os.environ.get('MLX_MACBOOK_ID') or macbook.get('id', 'unknown')
serial = macbook.get('serial', 'unknown')

vmagent_bin = env.get('VMAGENT_BINARY', 'vmagent')
scrape_interval = env.get('VMAGENT_SCRAPE_INTERVAL', '10s')
pq = env.get('VMAGENT_PERSISTENT_QUEUE', '')
capacity = env.get('VMAGENT_QUEUE_CAPACITY', '5000')
shards = env.get('VMAGENT_MAX_SHARDS', '200')
enabled = env.get('ENABLE_VMAGENT', 'true')

import subprocess
vmagent_ver = 'not installed / not in PATH'
try:
    p = subprocess.run([os.path.expanduser(vmagent_bin), '--version'], capture_output=True, text=True, timeout=5)
    vmagent_ver = p.stdout.strip() or p.stderr.strip()
except Exception:
    pass

remote_url = f'{otlp_scheme}://{central_vm}/api/v1/write'

print('=' * 72)
print('                  vmagent Edge Configuration Review')
print('=' * 72)
print(f'Status            : {\"ENABLED\" if enabled.lower() == \"true\" else \"DISABLED\"} (ENABLE_VMAGENT={enabled})')
print(f'Binary Path       : {vmagent_bin}')
print(f'Binary Version    : {vmagent_ver}')
print(f'Central VM Target : {central_vm}')
print(f'Remote Write URL  : {remote_url}')
print(f'Scrape Interval   : {scrape_interval}')
print(f'Persistent Queue  : {pq if pq else \"[disabled / in-memory buffer]\"}')
print(f'Queue Capacity    : {capacity} records')
print(f'Max Shards        : {shards}')
print(f'External Labels   : macbook={macbook_id}, serial={serial}')
print('-' * 72)
print('Scrape Targets (Local Exporters):')
targets = [
    ('mlx-proxy', env.get('PROXY_LISTEN', '127.0.0.1:8080'), '/metrics'),
    ('mlx-hw', env.get('HW_LISTEN', '127.0.0.1:9102'), '/metrics'),
    ('mlx-kv', env.get('KV_LISTEN', '127.0.0.1:9104'), '/metrics'),
    ('mlx-supervisor', env.get('SUPERVISOR_LISTEN', '127.0.0.1:9105'), '/metrics'),
    ('mlx-logtailer', env.get('LOGTAILER_LISTEN', '127.0.0.1:9106'), '/metrics'),
]
for job, listen, path in targets:
    print(f'  • {job:<15} -> http://{listen}{path}')
print('-' * 72)
print('CLI Launch Command:')
pq_flags = f' -remoteWrite.persistentQueue=true -remoteWrite.persistentQueueDir={pq}' if pq else ''
print('  ' + vmagent_bin + ' \\\\')
print('    -promscrape.config logs/vmagent.yml \\\\')
print('    -promscrape.configCheckInterval 30s \\\\')
print('    -remoteWrite.url ' + remote_url + ' \\\\')
print('    -remoteWrite.maxDiskUsagePerURL 10GB' + pq_flags)
print('=' * 72)
"

  if [[ -t 0 ]]; then
    local show_yaml
    show_yaml="$(_prompt_input "Display full rendered vmagent.yml scrape configuration? (y/N)" "N")"
    if [[ "${show_yaml}" =~ ^[yY] ]]; then
      info "Rendered vmagent.yml:"
      "${py_bin}" mlx_monitoring_bundle.py vmagent-config | grep -v '^{"ts"' || true
    fi

    local modify
    modify="$(_prompt_input "Would you like to adjust vmagent settings in edge.env? (y/N)" "N")"
    if [[ "${modify}" =~ ^[yY] ]]; then
      local cur_interval cur_pq cur_enable
      cur_interval="$(grep -E '^VMAGENT_SCRAPE_INTERVAL=' "${ENV_FILE}" | cut -d= -f2- | tr -d '\"' || echo "10s")"
      cur_pq="$(grep -E '^VMAGENT_PERSISTENT_QUEUE=' "${ENV_FILE}" | cut -d= -f2- | tr -d '\"' || echo "")"
      cur_enable="$(grep -E '^ENABLE_VMAGENT=' "${ENV_FILE}" | cut -d= -f2- | tr -d '\"' || echo "true")"

      local new_interval new_pq new_enable
      new_interval="$(_prompt_input "Scrape Interval" "${cur_interval:-10s}")"
      new_pq="$(_prompt_input "Persistent Queue Directory (leave blank for in-memory)" "${cur_pq}")"
      new_enable="$(_prompt_input "Enable vmagent (true/false)" "${cur_enable:-true}")"

      "${py_bin}" -c "
import sys
env_path = sys.argv[1]
kvs = {'VMAGENT_SCRAPE_INTERVAL': sys.argv[2], 'VMAGENT_PERSISTENT_QUEUE': sys.argv[3], 'ENABLE_VMAGENT': sys.argv[4]}
lines = open(env_path, 'r', encoding='utf-8').readlines()
new_lines = []
for line in lines:
    matched = False
    for k in list(kvs.keys()):
        if line.strip().startswith(f'{k}='):
            new_lines.append(f'{k}={kvs[k]}\n')
            del kvs[k]
            matched = True
            break
    if not matched:
        new_lines.append(line)
for k, v in kvs.items():
    new_lines.append(f'{k}={v}\n')
open(env_path, 'w', encoding='utf-8').writelines(new_lines)
" "${ENV_FILE}" "${new_interval}" "${new_pq}" "${new_enable}"
      info "Updated ${ENV_FILE}"
      "${py_bin}" mlx_monitoring_bundle.py vmagent-config >/dev/null 2>&1 || true
      info "Re-rendered logs/vmagent.yml with new settings."
    fi
  fi
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
tpl_path, dst_path, oncall_cfg, default_cfg = sys.argv[1:5]
tpl = open(tpl_path, 'r', encoding='utf-8').read()
tpl = tpl.replace('{{ONCALL_WEBHOOK_CONFIG}}', oncall_cfg)
tpl = tpl.replace('{{DEFAULT_WEBHOOK_CONFIG}}', default_cfg)
open(dst_path, 'w', encoding='utf-8').write(tpl)
" "${tpl}" "${dst}" "${oncall_cfg}" "${default_cfg}"
}

ensure_central_env() {
  local env_file="${CENTRAL_DIR}/.env"

  local current_user="${GRAFANA_ADMIN_USER:-}"
  local current_password="${GRAFANA_ADMIN_PASSWORD:-}"
  local current_anon="${GRAFANA_ANONYMOUS_ENABLED:-}"

  # 1. Load existing central/.env if present
  if [[ -f "${env_file}" ]]; then
    while IFS='=' read -r key val || [[ -n "${key}" ]]; do
      key="$(echo "${key}" | tr -d '[:space:]')"
      [[ -z "${key}" || "${key}" =~ ^# ]] && continue
      val="$(echo "${val}" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//")"
      if [[ "${key}" == "GRAFANA_ADMIN_USER" && -z "${current_user}" ]]; then
        current_user="${val}"
      elif [[ "${key}" == "GRAFANA_ADMIN_PASSWORD" && -z "${current_password}" ]]; then
        current_password="${val}"
      elif [[ "${key}" == "GRAFANA_ANONYMOUS_ENABLED" && -z "${current_anon}" ]]; then
        current_anon="${val}"
      fi
    done < "${env_file}"
  fi

  [[ -z "${current_user}" ]] && current_user="admin"
  [[ -z "${current_anon}" ]] && current_anon="false"

  # 2. Check if password needs to be prompted
  if [[ -z "${current_password}" || "${current_password}" == "changeme" ]]; then
    if [[ -t 0 ]]; then
      info "Grafana admin password not configured (or set to default 'changeme')."
      current_user="$(_prompt_input "Grafana Admin Username" "${current_user}")"
      while true; do
        local p1 p2
        p1="$(_prompt_password "Grafana Admin Password")"
        if [[ -z "${p1}" || "${p1}" == "changeme" ]]; then
          warn "Password cannot be empty or 'changeme'. Please enter a secure password."
          continue
        fi
        p2="$(_prompt_password "Confirm Grafana Admin Password")"
        if [[ "${p1}" != "${p2}" ]]; then
          warn "Passwords do not match. Please try again."
          continue
        fi
        current_password="${p1}"
        break
      done
    else
      if [[ -z "${current_password}" || "${current_password}" == "changeme" ]]; then
        die "GRAFANA_ADMIN_PASSWORD must be configured in ${env_file} or via environment (cannot be empty or 'changeme')"
      fi
    fi
  else
    info "Using configured Grafana credentials from ${env_file}"
  fi

  export GRAFANA_ADMIN_USER="${current_user}"
  export GRAFANA_ADMIN_PASSWORD="${current_password}"
  export GRAFANA_ANONYMOUS_ENABLED="${current_anon}"

  # 3. Atomically write central/.env with strict permissions (0600)
  local tmp_env="${env_file}.tmp.$$"
  (
    umask 077
    cat > "${tmp_env}" <<EOF
# Central Monitoring Stack Environment
# Generated by scripts/deploy.sh on $(date -u '+%Y-%m-%dT%H:%M:%SZ')
GRAFANA_ADMIN_USER=${current_user}
GRAFANA_ADMIN_PASSWORD=${current_password}
GRAFANA_ANONYMOUS_ENABLED=${current_anon}
EOF
    mv -f "${tmp_env}" "${env_file}"
    chmod 600 "${env_file}"
  )
}

cmd_central() {
  local with_opik=0
  if [[ "${1:-}" == "--opik" ]]; then
    with_opik=1
  fi
  local engine=""
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    engine="docker"
  elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
    engine="podman"
  elif command -v docker >/dev/null 2>&1; then
    engine="docker"
  elif command -v podman >/dev/null 2>&1; then
    engine="podman"
  else
    die "docker or podman is required for the central stack"
  fi
  ensure_central_env
  render_alertmanager_config
  cd "${CENTRAL_DIR}"

  local compose_args=(-f docker-compose.central.yml)
  if [[ -f "${CENTRAL_DIR}/.env" ]]; then
    compose_args=(--env-file "${CENTRAL_DIR}/.env" "${compose_args[@]}")
  fi
  if [[ "${with_opik}" == "1" ]]; then
    compose_args=(--profile opik "${compose_args[@]}")
  fi

  # 1. Determine container images required for this configuration
  local raw_images=()
  if "${engine}" compose "${compose_args[@]}" config --images >/dev/null 2>&1; then
    while IFS= read -r img; do
      [[ -n "${img}" ]] && raw_images+=("${img}")
    done < <("${engine}" compose "${compose_args[@]}" config --images | sort -u)
  fi

  if [[ ${#raw_images[@]} -eq 0 ]]; then
    while IFS= read -r img; do
      [[ -n "${img}" ]] && raw_images+=("${img}")
    done < <(python3 -c "
import sys, yaml
with_opik = sys.argv[1] == '1'
try:
    doc = yaml.safe_load(open('docker-compose.central.yml'))
    for s in doc.get('services', {}).values():
        if 'opik' in s.get('profiles', []) and not with_opik:
            continue
        img = s.get('image', '').replace('\${OPIK_VERSION:-2.2.13}', '2.2.13')
        if img:
            print(img)
except Exception:
    pass
" "${with_opik}" 2>/dev/null | sort -u)
  fi

  if [[ ${#raw_images[@]} -eq 0 ]]; then
    while IFS= read -r img; do
      [[ -n "${img}" ]] && raw_images+=("${img}")
    done < <(grep -E '^\s*image:' docker-compose.central.yml | awk '{print $2}' | sed "s/\${OPIK_VERSION:-2.2.13}/2.2.13/g" | sort -u)
  fi

  # 2. Check each image in local container storage
  local cached_images=()
  local missing_images=()
  if [[ ${#raw_images[@]} -gt 0 ]]; then
    for img in "${raw_images[@]}"; do
      if "${engine}" image inspect "${img}" >/dev/null 2>&1; then
        cached_images+=("${img}")
      else
        missing_images+=("${img}")
      fi
    done
  fi

  info "Container image status in local ${engine} storage:"
  if [[ ${#cached_images[@]} -gt 0 ]]; then
    for img in "${cached_images[@]}"; do
      printf '  [cached]   %s\n' "${img}" >&2
    done
  fi
  if [[ ${#missing_images[@]} -gt 0 ]]; then
    for img in "${missing_images[@]}"; do
      printf '  [missing]  %s\n' "${img}" >&2
    done
  fi

  # 3. Confirmation gate
  local force_pull=0
  if [[ ${#missing_images[@]} -gt 0 ]]; then
    info "${#cached_images[@]} image(s) cached locally; ${#missing_images[@]} missing image(s) will be downloaded."
    local confirm
    confirm="$(_prompt_input "Proceed to download missing images and deploy the central stack? (y/N) [or enter 'pull' to re-pull all images]" "N")"
    case "${confirm}" in
      pull|PULL)
        force_pull=1
        ;;
      y|Y|yes|YES) ;;
      *)
        info "Deployment aborted by user."
        return 0
        ;;
    esac
  else
    info "All ${#cached_images[@]} image(s) are already cached locally in ${engine} storage (no downloads required)."
    local confirm
    confirm="$(_prompt_input "Proceed to deploy the central stack? (y/N) [enter 'pull' to force re-download]" "N")"
    case "${confirm}" in
      pull|PULL)
        force_pull=1
        ;;
      y|Y|yes|YES) ;;
      *)
        info "Deployment aborted by user."
        return 0
        ;;
    esac
  fi

  # 4. Pull only missing images unless user explicitly requested force pull
  if [[ "${force_pull}" == "1" ]]; then
    info "Force-pulling all ${#raw_images[@]} container images using ${engine}..."
    if ! "${engine}" compose "${compose_args[@]}" pull; then
      die "failed to pull container images"
    fi
  elif [[ ${#missing_images[@]} -gt 0 ]]; then
    info "Pulling ${#missing_images[@]} missing container image(s) using ${engine}..."
    for img in "${missing_images[@]}"; do
      info "pulling ${img}..."
      if ! "${engine}" pull "${img}"; then
        die "failed to pull image: ${img}"
      fi
    done
  else
    info "Skipping download as all images are already cached locally."
  fi

  # 5. Bring up the stack
  if [[ "${with_opik}" == "1" ]]; then
    info "starting central observability stack with Opik"
  else
    info "starting central observability stack"
  fi
  if ! "${engine}" compose "${compose_args[@]}" up -d; then
    die "failed to start central container stack"
  fi

  # 6. Synchronize admin password with Grafana internal DB
  if "${engine}" ps --format '{{.Names}}' | grep -q '^central-grafana$'; then
    local retries=5
    while [[ ${retries} -gt 0 ]]; do
      if "${engine}" exec central-grafana grafana cli admin reset-admin-password "${GRAFANA_ADMIN_PASSWORD}" >/dev/null 2>&1; then
        break
      fi
      sleep 1
      retries=$((retries - 1))
    done
  fi

  if [[ "${with_opik}" != "1" ]]; then
    info "Opik is available with: $0 central --opik"
  fi
  info "central stack started successfully"
}

usage() {
  cat >&2 <<EOF
scripts/deploy.sh — unified CLI for mlx monitoring bundle operations

Usage:
  ./scripts/deploy.sh                             interactive menu
  ./scripts/deploy.sh setup-edge [args]           interactive or parameterized edge setup
  ./scripts/deploy.sh review-vmagent              review and adjust vmagent edge configuration
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
  ./scripts/deploy.sh edge review-vmagent         preview rendered vmagent config and scrape targets

EOF
}

main() {
  case "${1:-}" in
    help|-h|--help)
      usage
      return 0
      ;;
  esac

  # Ensure working copy is updated before any operation
  _ensure_git_latest

  case "${1:-}" in
    validate)   shift; cmd_validate "$@" ;;
    setup-edge) shift; cmd_setup_edge "$@" ;;
    review-vmagent|vmagent-config) shift; cmd_review_vmagent "$@" ;;
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
        "review-vmagent — review and adjust vmagent edge configuration" \
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
        review-vmagent*) cmd_review_vmagent ;;
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
