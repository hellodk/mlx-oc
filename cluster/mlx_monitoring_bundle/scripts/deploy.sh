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

# Ensure standard binary search paths on macOS / Linux (e.g. Homebrew, system bins)
for p in /opt/homebrew/bin /usr/local/bin /usr/bin /bin; do
  if [[ -d "${p}" && ":${PATH}:" != *":${p}:"* ]]; then
    export PATH="${p}:${PATH}"
  fi
done

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

_ist_timestamp() {
  TZ="Asia/Kolkata" date +'%Y%m%d_%H%M%S_IST'
}

_ist_now_display() {
  TZ="Asia/Kolkata" date +'%d %b %Y, %H:%M:%S IST'
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
    review-edge|review)
      cmd_diagnostics review-edge "$@"
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
import os, sys, yaml, socket, subprocess, shutil
from pathlib import Path

edge_dir = Path('${EDGE_DIR}').resolve()
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

def probe_target(host, port, timeout=0.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False

# 1. Binary check
expanded_bin = Path(os.path.expanduser(vmagent_bin))
binary_found = expanded_bin.is_file() or shutil.which(vmagent_bin) is not None
vmagent_ver = 'not installed / not in PATH'
binary_ok = False
if binary_found:
    cmd_path = str(expanded_bin) if expanded_bin.is_file() else shutil.which(vmagent_bin)
    try:
        p = subprocess.run([cmd_path, '--version'], capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            vmagent_ver = p.stdout.strip() or p.stderr.strip()
            binary_ok = True
        else:
            vmagent_ver = f'execution error (exit code {p.returncode})'
    except Exception as e:
        vmagent_ver = f'execution error: {e}'

# 2. Process check
pid_file = edge_dir / 'bin' / 'vmagent.pid'
process_running = False
stale_pid = False
pid_val = None
if pid_file.is_file():
    try:
        pid_val = int(pid_file.read_text().strip())
        os.kill(pid_val, 0)
        ps_out = subprocess.run(['ps', '-p', str(pid_val), '-o', 'comm='], capture_output=True, text=True).stdout.strip()
        if 'vmagent' in ps_out or not ps_out:
            process_running = True
        else:
            stale_pid = True
    except (ValueError, OSError):
        stale_pid = True

# 3. Targets probe
targets = [
    ('mlx-proxy', env.get('PROXY_LISTEN', '127.0.0.1:8080'), '/metrics'),
    ('mlx-hw', env.get('HW_LISTEN', '127.0.0.1:9102'), '/metrics'),
    ('mlx-kv', env.get('KV_LISTEN', '127.0.0.1:9104'), '/metrics'),
    ('mlx-supervisor', env.get('SUPERVISOR_LISTEN', '127.0.0.1:9105'), '/metrics'),
    ('mlx-logtailer', env.get('LOGTAILER_LISTEN', '127.0.0.1:9106'), '/metrics'),
]
target_rows = []
up_count = 0
for job, listen, path in targets:
    thost, tport = listen.split(':') if ':' in listen else ('127.0.0.1', listen)
    is_up = probe_target(thost, tport, timeout=0.3)
    if is_up:
        up_count += 1
    target_rows.append((job, listen, path, is_up))

# 4. Central VM probe
cvm_host, cvm_port = central_vm.split(':') if ':' in central_vm else (central_vm, 8428)
cvm_reachable = probe_target(cvm_host, cvm_port, timeout=1.0)

remote_url = f'{otlp_scheme}://{central_vm}/api/v1/write'

print('=' * 74)
print('                  vmagent Edge Configuration Review')
print('=' * 74)
status_str = 'ENABLED' if enabled.lower() == 'true' else 'DISABLED'
print(f'Status            : {status_str} (ENABLE_VMAGENT={enabled})')
bin_status = 'INSTALLED' if binary_ok else 'NOT FOUND / NOT EXECUTABLE'
print(f'Binary Status     : {bin_status} ({vmagent_ver})')
print(f'Binary Path       : {vmagent_bin}')
if process_running:
    print(f'Process Status    : RUNNING (PID {pid_val})')
elif stale_pid:
    print(f'Process Status    : STOPPED (stale PID file: {pid_val})')
else:
    print('Process Status    : STOPPED (not running)')
cvm_status = 'REACHABLE' if cvm_reachable else 'UNREACHABLE'
print(f'Central VM Target : {central_vm} [{cvm_status}]')
print(f'Remote Write URL  : {remote_url}')
print(f'Scrape Interval   : {scrape_interval}')
print(f'Persistent Queue  : {pq if pq else \"[disabled / in-memory buffer]\"}')
print(f'Queue Capacity    : {capacity} records')
print(f'Max Shards        : {shards}')
print(f'External Labels   : macbook={macbook_id}, serial={serial}')
print('-' * 74)
print('Scrape Targets (Local Exporters):')
for job, listen, path, is_up in target_rows:
    stat = '[UP]' if is_up else '[DOWN: port not listening]'
    print(f'  • {job:<15} -> http://{listen}{path:<10} {stat}')
print('-' * 74)
print('CLI Launch Command:')
pq_flags = f' -remoteWrite.persistentQueue=true -remoteWrite.persistentQueueDir={pq}' if pq else ''
print('  ' + vmagent_bin + ' \\\\')
print('    -promscrape.config logs/vmagent.yml \\\\')
print('    -promscrape.configCheckInterval 30s \\\\')
print('    -remoteWrite.url ' + remote_url + ' \\\\')
print('    -remoteWrite.maxDiskUsagePerURL 10GB' + pq_flags)
print('-' * 74)
print('Fail Checks & Health:')
if binary_ok:
    print(f'  [OK] vmagent binary is executable ({vmagent_ver}).')
else:
    print(f'  [FAIL] vmagent binary not found or not executable at {vmagent_bin}.')
    print('         Run \"./scripts/deploy.sh setup-edge\" to download and install it.')

if process_running:
    print(f'  [OK] vmagent process is RUNNING (PID {pid_val}).')
elif stale_pid:
    print(f'  [FAIL] vmagent PID file exists (PID {pid_val}) but process is dead (stale PID).')
else:
    print('  [WARN] vmagent process is NOT RUNNING (start with \"./scripts/deploy.sh edge daemon\").')

if cvm_reachable:
    print(f'  [OK] Central VictoriaMetrics is REACHABLE at {central_vm}.')
else:
    print(f'  [FAIL] Central VictoriaMetrics at {central_vm} is UNREACHABLE (check IP/firewall).')

if up_count == len(targets):
    print(f'  [OK] All {len(targets)} local exporter scrape targets are UP.')
else:
    print(f'  [WARN] {len(targets) - up_count} of {len(targets)} local exporter scrape targets are DOWN.')
print('=' * 74)
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

cmd_review_grafana() {
  local py_bin
  py_bin="$(bundle_python)"
  if [[ -z "${py_bin}" ]] || ! command -v "${py_bin}" >/dev/null 2>&1; then
    py_bin="python3"
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
  fi

  cd "${CENTRAL_DIR}"
  "${py_bin}" -c "
import os, sys, yaml, socket, subprocess, urllib.request, json
from pathlib import Path
from datetime import datetime, timezone, timedelta

central_dir = Path('${CENTRAL_DIR}').resolve()
env_file = central_dir / '.env'
compose_file = central_dir / 'docker-compose.central.yml'
datasources_file = central_dir / 'config' / 'grafana' / 'datasources.yaml'
dashboards_dir = central_dir / 'dashboards'
engine_arg = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else None

def format_ist(iso_ts):
    try:
        dt = datetime.fromisoformat(iso_ts.replace('Z', '+00:00'))
        ist = dt.astimezone(timezone(timedelta(hours=5, minutes=30)))
        return ist.strftime('%d %b %Y, %H:%M:%S IST')
    except Exception:
        return iso_ts

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('192.168.1.1', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

env = {}
if env_file.is_file():
    for line in env_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('\"\'')

user = os.environ.get('GRAFANA_ADMIN_USER') or env.get('GRAFANA_ADMIN_USER', 'admin')
password = os.environ.get('GRAFANA_ADMIN_PASSWORD') or env.get('GRAFANA_ADMIN_PASSWORD', 'changeme')
anon = os.environ.get('GRAFANA_ANONYMOUS_ENABLED') or env.get('GRAFANA_ANONYMOUS_ENABLED', 'false')

engine = engine_arg
engine_ver = 'not installed'
daemon_ok = False
if engine:
    try:
        p = subprocess.run([engine, 'info', '--format', '{{.ServerVersion}}'], capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            daemon_ok = True
            engine_ver = p.stdout.strip()
        else:
            engine_ver = f'daemon not responding: {p.stderr.strip()}'
    except Exception as e:
        engine_ver = f'error: {e}'

container_status = 'NOT DEPLOYED'
container_uptime = ''
container_running = False
if daemon_ok and engine:
    for cname in ['central-grafana', 'grafana']:
        try:
            p = subprocess.run([engine, 'inspect', cname, '--format', '{{.State.Status}}|{{.State.Running}}|{{.State.StartedAt}}'],
                               capture_output=True, text=True, timeout=5)
            if p.returncode == 0 and p.stdout.strip():
                status, running, started = p.stdout.strip().split('|', 2)
                container_status = status.upper()
                container_running = running.lower() == 'true'
                container_uptime = format_ist(started)
                break
        except Exception:
            pass

api_healthy = False
api_info = {}
try:
    req = urllib.request.Request('http://127.0.0.1:3000/api/health')
    with urllib.request.urlopen(req, timeout=2) as resp:
        if resp.status == 200:
            api_healthy = True
            api_info = json.loads(resp.read().decode('utf-8'))
except Exception as e:
    api_info = {'error': str(e)}

auth_status = 'UNKNOWN'
auth_ok = False
if api_healthy:
    import base64
    auth_header = 'Basic ' + base64.b64encode(f'{user}:{password}'.encode('utf-8')).decode('utf-8')
    req = urllib.request.Request('http://127.0.0.1:3000/api/users', headers={'Authorization': auth_header})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                auth_status = 'VERIFIED (HTTP 200)'
                auth_ok = True
    except urllib.error.HTTPError as e:
        if e.code == 401:
            auth_status = 'FAILED (HTTP 401 Unauthorized)'
        else:
            auth_status = f'HTTP Error {e.code}'
    except Exception as e:
        auth_status = f'Error: {e}'

datasources = []
if datasources_file.is_file():
    try:
        ds_data = yaml.safe_load(datasources_file.read_text(encoding='utf-8'))
        for ds in ds_data.get('datasources', []):
            datasources.append(f\"{ds.get('name', 'unknown')} ({ds.get('url', '')})\")
    except Exception:
        pass

dashboards = []
if dashboards_dir.is_dir():
    dashboards = sorted([f.stem for f in dashboards_dir.glob('*.json')])

env_perms = 'missing'
env_mode_ok = False
if env_file.is_file():
    mode = oct(env_file.stat().st_mode & 0o777)
    env_perms = f'{mode} ({oct(env_file.stat().st_mode)[-3:]})'
    env_mode_ok = (env_file.stat().st_mode & 0o077) == 0

lan_ip = get_lan_ip()

print('=' * 74)
print('                 Grafana Central Configuration Review')
print('=' * 74)
print(f'Container Engine  : {engine.capitalize() if engine else \"None\"} ({engine_ver})')
print(f'Container Status  : {container_status}' + (f' (Started: {container_uptime})' if container_uptime else ''))
if api_healthy:
    db_status = api_info.get('database', 'unknown')
    g_ver = api_info.get('version', 'unknown')
    print(f'HTTP API Health   : HEALTHY (HTTP 200, database: {db_status}, version: {g_ver})')
else:
    print('HTTP API Health   : UNHEALTHY / NOT RESPONDING')
print(f'Authentication    : {auth_status}')
print(f'Config File       : {env_file} (mode: {env_perms})')
print('-' * 74)
print('Credentials & Security:')
print(f'  • Admin Username  : {user}')
pw_note = ''
if not password:
    pw_note = ' [FAIL: Password is empty]'
elif password == 'changeme':
    pw_note = ' [WARN: Using default password changeme]'
print(f'  • Admin Password  : {password}{pw_note}')
print(f'  • Anonymous Auth  : {\"ENABLED\" if anon.lower() == \"true\" else \"DISABLED\"} (GF_AUTH_ANONYMOUS_ENABLED={anon})')
print('Access Endpoints:')
print(f'  • Localhost       : http://127.0.0.1:3000')
print(f'  • LAN / Direct IP : http://{lan_ip}:3000')
print('Storage & Resources:')
print('  • Volume          : grafana-data (persistent SQLite /var/lib/grafana/grafana.db)')
print('  • Datasources     : ' + (', '.join(datasources) if datasources else 'None found'))
print(f'  • Dashboards ({len(dashboards)}) : ' + (', '.join(dashboards[:6]) + f' (+{len(dashboards)-6} more)' if len(dashboards) > 6 else ', '.join(dashboards)))
print('-' * 74)
print('Fail Checks & Health:')
if not engine:
    print('  [FAIL] Container engine (docker or podman) is NOT installed.')
elif not daemon_ok:
    print(f'  [FAIL] {engine.capitalize()} daemon is NOT running or socket is inaccessible.')
else:
    print(f'  [OK] Container engine ({engine}) is installed and daemon is active.')

if container_running:
    print('  [OK] central-grafana container is RUNNING.')
else:
    print(f'  [FAIL] central-grafana container is {container_status}. Run \"./scripts/deploy.sh central\" to start.')

if api_healthy:
    print('  [OK] Grafana HTTP service is responding on port 3000.')
else:
    print('  [FAIL] Grafana HTTP service is NOT responding on port 3000.')

if auth_ok:
    print('  [OK] Configured admin credentials authenticate successfully.')
elif api_healthy:
    print('  [FAIL] Configured password does NOT authenticate with Grafana (HTTP 401).')

if not password:
    print('  [FAIL] GRAFANA_ADMIN_PASSWORD is empty.')
elif password == 'changeme':
    print('  [WARN] Admin password is set to default \"changeme\". Change it to a secure password.')
else:
    print('  [OK] Admin password is non-default.')

if env_file.is_file() and env_mode_ok:
    print(f'  [OK] Config file permissions are restricted ({env_perms}).')
elif env_file.is_file():
    print(f'  [WARN] Config file permissions are too open ({env_perms}). Recommended: 0600.')
print('=' * 74)
" "${engine}"

  if [[ -t 0 ]]; then
    local cur_user cur_pass
    local env_file="${CENTRAL_DIR}/.env"
    cur_user="$(grep -E '^GRAFANA_ADMIN_USER=' "${env_file}" 2>/dev/null | cut -d= -f2- | tr -d '\"' || echo "admin")"
    cur_pass="$(grep -E '^GRAFANA_ADMIN_PASSWORD=' "${env_file}" 2>/dev/null | cut -d= -f2- | tr -d '\"' || echo "")"
    [[ -z "${cur_user}" ]] && cur_user="admin"

    local modify
    modify="$(_prompt_input "Would you like to change/sync Grafana admin credentials? (y/N)" "N")"
    if [[ "${modify}" =~ ^[yY] ]]; then
      local new_user new_pass
      new_user="$(_prompt_input "Grafana Admin Username" "${cur_user}")"
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
        new_pass="${p1}"
        break
      done

      export GRAFANA_ADMIN_USER="${new_user}"
      export GRAFANA_ADMIN_PASSWORD="${new_pass}"

      local tmp_env="${env_file}.tmp.$$"
      (
        umask 077
        cat > "${tmp_env}" <<EOF
# Central Monitoring Stack Environment
# Generated by scripts/deploy.sh on $(date -u '+%Y-%m-%dT%H:%M:%SZ')
GRAFANA_ADMIN_USER=${new_user}
GRAFANA_ADMIN_PASSWORD=${new_pass}
GRAFANA_ANONYMOUS_ENABLED=${GRAFANA_ANONYMOUS_ENABLED:-false}
EOF
        mv -f "${tmp_env}" "${env_file}"
        chmod 600 "${env_file}"
      )
      info "Updated credentials in ${env_file} (mode 0600)"

      # If container is running, live-sync the database
      local gf_cname=""
      if [[ -n "${engine}" ]]; then
        if "${engine}" ps --format '{{.Names}}' 2>/dev/null | grep -q '^central-grafana$'; then
          gf_cname="central-grafana"
        elif "${engine}" ps --format '{{.Names}}' 2>/dev/null | grep -q '^grafana$'; then
          gf_cname="grafana"
        fi
      fi
      if [[ -n "${gf_cname}" ]]; then
        if "${engine}" exec "${gf_cname}" grafana cli admin reset-admin-password "${new_pass}" >/dev/null 2>&1; then
          info "Successfully synchronized admin password in running Grafana internal database."
        else
          warn "Failed to live-sync password inside container. Restart container with './scripts/deploy.sh central'."
        fi
      fi
    fi
  fi
}

cmd_diagnostics() {
  local subcmd="$1"
  shift
  local py_bin
  py_bin="$(bundle_python)"
  if [[ -z "${py_bin}" ]] || ! command -v "${py_bin}" >/dev/null 2>&1; then
    py_bin="python3"
  fi
  "${py_bin}" "${SCRIPT_DIR}/diagnostics.py" "${subcmd}" "$@"
}

_get_podman_machine_memory_mb() {
  if ! command -v podman >/dev/null 2>&1; then
    echo "0"
    return 0
  fi
  local mem=""
  mem="$(podman machine inspect --format '{{.Resources.Memory}}' 2>/dev/null | grep -E '^[0-9]+$' | head -n 1 || true)"
  if [[ -n "${mem}" && "${mem}" =~ ^[0-9]+$ && "${mem}" -gt 0 ]]; then
    echo "${mem}"
    return 0
  fi

  local inspect_json=""
  inspect_json="$(podman machine inspect 2>/dev/null || true)"
  if [[ -n "${inspect_json}" ]]; then
    mem="$(python3 -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    if isinstance(data, list) and len(data) > 0:
        running_vm = next((m for m in data if m.get('State') == 'running' or m.get('Running')), data[0])
        val = running_vm.get('Resources', {}).get('Memory', 0)
        print(int(val))
    else:
        print(0)
except Exception:
    print(0)
" <<< "${inspect_json}" 2>/dev/null || echo 0)"
  fi

  if [[ -z "${mem}" || "${mem}" == "0" ]]; then
    local raw_list=""
    raw_list="$(podman machine list --format '{{.Memory}}' 2>/dev/null || true)"
    if [[ -n "${raw_list}" ]]; then
      mem="$(python3 -c "
import sys, re
def parse_mem(s):
    if not s:
        return 0
    m = re.match(r'^([0-9]+(?:\.[0-9]+)?)\s*(gib|gb|g|mib|mb|m)?$', s.strip(), re.I)
    if not m:
        return 0
    val, unit = float(m.group(1)), (m.group(2) or 'mb').lower()
    return int(val * 1024) if unit in ('gib', 'gb', 'g') else int(val)

for line in sys.stdin:
    v = parse_mem(line)
    if v > 0:
        print(v)
        break
else:
    print(0)
" <<< "${raw_list}" 2>/dev/null || echo 0)"
    fi
  fi

  echo "${mem:-0}"
}

_check_podman_machine_size() {
  local with_opik="${1:-0}"
  local force="${2:-${FORCE_DEPLOY:-0}}"
  local min_mb=2048
  local stack_desc="Central stack"

  if [[ "${with_opik}" == "1" ]]; then
    min_mb=4096
    stack_desc="Central stack with Opik"
  fi

  local actual_mb
  actual_mb="$(_get_podman_machine_memory_mb)"

  if [[ -z "${actual_mb}" || ! "${actual_mb}" =~ ^[0-9]+$ || "${actual_mb}" -le 0 ]]; then
    return 0
  fi

  if [[ "${actual_mb}" -lt "${min_mb}" ]]; then
    printf '\n[ERROR] Podman machine memory (%d MB) is insufficient for %s (requires at least %d MB).\n' \
      "${actual_mb}" "${stack_desc}" "${min_mb}" >&2
    printf 'To increase Podman machine memory, run:\n' >&2
    printf '  podman machine stop\n' >&2
    printf '  podman machine set --memory %d\n' "${min_mb}" >&2
    printf '  podman machine start\n\n' >&2
    if [[ "${force}" == "1" ]]; then
      printf '[WARN] Proceeding anyway due to force flag.\n' >&2
      return 0
    fi
    return 1
  fi

  return 0
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

render_grafana_datasource_config() {
  local tpl="${CENTRAL_DIR}/config/grafana/datasources.yaml.template"
  local dst="${CENTRAL_DIR}/config/grafana/datasources.yaml"
  if [[ ! -f "${tpl}" ]]; then
    return 0
  fi

  local vm_url="${VICTORIAMETRICS_URL:-http://victoria-metrics:8428}"

  python3 -c "
import sys
tpl_path, dst_path, vm_url = sys.argv[1:4]
tpl = open(tpl_path, 'r', encoding='utf-8').read()
tpl = tpl.replace('{{VICTORIAMETRICS_URL}}', vm_url)
open(dst_path, 'w', encoding='utf-8').write(tpl)
" "${tpl}" "${dst}" "${vm_url}"
}

ensure_central_env() {
  local env_file="${CENTRAL_DIR}/.env"

  local current_user="${GRAFANA_ADMIN_USER:-}"
  local current_password="${GRAFANA_ADMIN_PASSWORD:-}"
  local current_anon="${GRAFANA_ANONYMOUS_ENABLED:-}"
  local current_vm_url="${VICTORIAMETRICS_URL:-}"

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
      elif [[ "${key}" == "VICTORIAMETRICS_URL" && -z "${current_vm_url}" ]]; then
        current_vm_url="${val}"
      fi
    done < "${env_file}"
  fi

  [[ -z "${current_user}" ]] && current_user="admin"
  [[ -z "${current_anon}" ]] && current_anon="false"
  [[ -z "${current_vm_url}" ]] && current_vm_url="http://victoria-metrics:8428"

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
  export VICTORIAMETRICS_URL="${current_vm_url}"

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
VICTORIAMETRICS_URL=${current_vm_url}
EOF
    mv -f "${tmp_env}" "${env_file}"
    chmod 600 "${env_file}"
  )
}

cmd_central() {
  local with_opik=0
  for arg in "$@"; do
    case "${arg}" in
      --opik) with_opik=1 ;;
      -y|--yes|-f|--force) export FORCE_DEPLOY=1 ;;
    esac
  done
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
  if [[ "${engine}" == "podman" ]]; then
    if ! _check_podman_machine_size "${with_opik}" "${FORCE_DEPLOY:-0}"; then
      die "Podman machine size check failed. Increase VM memory or pass --force."
    fi
  fi
  ensure_central_env
  render_alertmanager_config
  render_grafana_datasource_config
  cd "${CENTRAL_DIR}"

  local compose_args=(-f docker-compose.central.yml)
  if [[ -f "${CENTRAL_DIR}/.env" ]]; then
    compose_args=(--env-file "${CENTRAL_DIR}/.env" "${compose_args[@]}")
  fi
  if [[ "${with_opik}" == "1" ]]; then
    compose_args=(--profile opik "${compose_args[@]}")
  fi

  local compose_cmd=("${engine}" "compose")
  if [[ "${engine}" == "podman" ]]; then
    if ! podman compose version >/dev/null 2>&1 && command -v podman-compose >/dev/null 2>&1; then
      compose_cmd=("podman-compose")
    fi
  fi
  if [[ "${compose_cmd[0]}" == "podman-compose" ]]; then
    compose_args=(--in-pod false "${compose_args[@]}")
  fi

  # 1. Determine container images required for this configuration
  local raw_images=()
  if "${compose_cmd[@]}" "${compose_args[@]}" config --images >/dev/null 2>&1; then
    while IFS= read -r img; do
      [[ -n "${img}" ]] && raw_images+=("${img}")
    done < <("${compose_cmd[@]}" "${compose_args[@]}" config --images | sort -u)
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
  local confirm="N"
  if [[ "${FORCE_DEPLOY:-0}" == "1" ]]; then
    confirm="y"
  elif [[ ${#missing_images[@]} -gt 0 ]]; then
    info "${#cached_images[@]} image(s) cached locally; ${#missing_images[@]} missing image(s) will be downloaded."
    confirm="$(_prompt_input "Proceed to download missing images and deploy the central stack? (y/N) [or enter 'pull' to re-pull all images]" "N")"
  else
    info "All ${#cached_images[@]} image(s) are already cached locally in ${engine} storage (no downloads required)."
    confirm="$(_prompt_input "Proceed to deploy the central stack? (y/N) [enter 'pull' to force re-download]" "N")"
  fi

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

  # 4. Pull only missing images unless user explicitly requested force pull
  if [[ "${force_pull}" == "1" ]]; then
    info "Force-pulling all ${#raw_images[@]} container images using ${engine}..."
    if ! "${compose_cmd[@]}" "${compose_args[@]}" pull; then
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
  if ! "${compose_cmd[@]}" "${compose_args[@]}" up -d; then
    local exit_code=$?
    err "container compose failed with exit code ${exit_code}"
    err "inspecting container status and failure causes:"
    while IFS='|' read -r cid cname cstat; do
      [[ -z "${cid}" ]] && continue
      local c_err oom_killed exit_c
      c_err="$("${engine}" inspect "${cid}" --format '{{.State.Error}}' 2>/dev/null || true)"
      oom_killed="$("${engine}" inspect "${cid}" --format '{{.State.OOMKilled}}' 2>/dev/null || true)"
      exit_c="$("${engine}" inspect "${cid}" --format '{{.State.ExitCode}}' 2>/dev/null || true)"
      err "  • Container ${cname} (${cid:0:12}): ${cstat}"
      if [[ -n "${exit_c}" && "${exit_c}" != "0" ]]; then
        err "    Exit code: ${exit_c}"
      fi
      if [[ "${oom_killed}" == "true" || "${exit_c}" == "137" ]]; then
        err "    >>> OOM KILLED: Process was terminated by the Linux Out-Of-Memory killer."
      fi
      if [[ -n "${c_err}" ]]; then
        err "    Runtime error: ${c_err}"
      fi
    done < <("${engine}" ps -a --format '{{.ID}}|{{.Names}}|{{.Status}}' 2>/dev/null | grep -E 'central-|opik|grafana|victoria|otel' || true)

    if [[ "$(uname -s)" == "Darwin" && "${engine}" == "podman" ]]; then
      local vm_mem
      vm_mem="$(podman machine list --format '{{.Memory}}' 2>/dev/null | head -1 || echo "")"
      if [[ -n "${vm_mem}" ]]; then
        err "  • Podman VM memory: ${vm_mem}"
        if [[ "${vm_mem}" == *"2GiB"* || "${vm_mem}" == *"2048"* ]]; then
          err "  >>> ROOT CAUSE: Podman VM memory is 2GiB. The Opik stack (ClickHouse, MySQL, JVM) requires at least 4GiB."
          err "  >>> ACTIONABLE FIX:"
          err "      1. Stop the VM:    podman machine stop"
          err "      2. Expand memory:  podman machine set --memory 4096"
          err "      3. Start the VM:   podman machine start"
          err "      4. Retry:          $0 central --opik"
        fi
      fi
    fi
    die "failed to start central container stack (see triage diagnostics above)"
  fi

  # 6. Synchronize admin password with Grafana internal DB
  local sync_cname=""
  if "${engine}" ps --format '{{.Names}}' | grep -q '^central-grafana$'; then
    sync_cname="central-grafana"
  elif "${engine}" ps --format '{{.Names}}' | grep -q '^grafana$'; then
    sync_cname="grafana"
  fi
  if [[ -n "${sync_cname}" ]]; then
    local retries=15
    while [[ ${retries} -gt 0 ]]; do
      if "${engine}" exec "${sync_cname}" grafana cli admin reset-admin-password "${GRAFANA_ADMIN_PASSWORD}" >/dev/null 2>&1; then
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

  if [[ -t 0 && "${FORCE_DEPLOY:-0}" != "1" ]]; then
    local review
    review="$(_prompt_input "Review Grafana configuration and credentials now? (y/N)" "N")"
    if [[ "${review}" =~ ^[yY] ]]; then
      cmd_review_grafana
    fi
  fi
}

clean_central() {
  local config_mode="${1:-reuse}"
  local engine=""
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    engine="docker"
  elif command -v podman >/dev/null 2>&1 && podman info >/dev/null 2>&1; then
    engine="podman"
  elif command -v docker >/dev/null 2>&1; then
    engine="docker"
  elif command -v podman >/dev/null 2>&1; then
    engine="podman"
  fi

  info "cleaning central observability stack (mode: ${config_mode})..."

  if [[ -n "${engine}" ]]; then
    local compose_cmd=("${engine}" "compose")
    if [[ "${engine}" == "podman" ]]; then
      if ! podman compose version >/dev/null 2>&1 && command -v podman-compose >/dev/null 2>&1; then
        compose_cmd=("podman-compose")
      fi
    fi

    cd "${CENTRAL_DIR}"
    local compose_args=(-f docker-compose.central.yml)
    if [[ -f "${CENTRAL_DIR}/.env" ]]; then
      compose_args=(--env-file "${CENTRAL_DIR}/.env" "${compose_args[@]}")
    fi
    compose_args=(--profile opik "${compose_args[@]}")
    if [[ "${compose_cmd[0]}" == "podman-compose" ]]; then
      compose_args=(--in-pod false "${compose_args[@]}")
    fi

    info "tearing down compose services and removing volumes..."
    "${compose_cmd[@]}" "${compose_args[@]}" down -v --remove-orphans 2>&1 || true

    # Clean any leftover or stopped containers
    local central_containers=(
      "central-grafana" "grafana"
      "central-victoriametrics" "central-victoria-metrics" "victoria-metrics"
      "central-vmalert" "vmalert"
      "central-alertmanager" "alertmanager"
      "central-otelcol" "central-otel-collector" "otel-collector"
      "central-opik-backend" "opik-backend"
      "central-opik-frontend" "opik-frontend"
      "central-opik-mysql" "opik-mysql"
      "central-opik-clickhouse" "opik-clickhouse"
      "central-opik-clickhouse-init" "opik-clickhouse-init"
      "central-opik-redis" "opik-redis"
      "central-opik-python-backend" "opik-python-backend"
      "central-opik-rustfs" "central-opik-storage" "opik-storage"
      "central-opik-zookeeper" "opik-zookeeper"
      "central-opik-init-bucket" "opik-init-bucket"
    )
    for cname in "${central_containers[@]}"; do
      if "${engine}" inspect "${cname}" >/dev/null 2>&1; then
        info "removing orphan container: ${cname}"
        "${engine}" rm -f "${cname}" >/dev/null 2>&1 || true
      fi
    done

    # Clean any leftover volumes
    local central_volumes=(
      "central_vm-data" "vm-data"
      "central_grafana-data" "grafana-data"
      "central_central-otel-logs" "central-otel-logs"
      "central_opik-mysql" "opik-mysql"
      "central_opik-clickhouse" "opik-clickhouse"
      "central_opik-clickhouse-log" "opik-clickhouse-log"
      "central_opik-clickhouse-config" "opik-clickhouse-config"
      "central_opik-zookeeper" "opik-zookeeper"
      "central_opik-redis" "opik-redis"
      "central_opik-storage" "opik-storage"
    )
    for vname in "${central_volumes[@]}"; do
      if "${engine}" volume inspect "${vname}" >/dev/null 2>&1; then
        info "removing orphan volume: ${vname}"
        "${engine}" volume rm -f "${vname}" >/dev/null 2>&1 || true
      fi
    done
  else
    warn "no container engine (docker/podman) detected; skipping container teardown"
  fi

  if [[ "${config_mode}" == "recreate" ]]; then
    local ts
    ts="$(_ist_timestamp)"
    if [[ -f "${CENTRAL_DIR}/.env" ]]; then
      local bak="${CENTRAL_DIR}/.env.bak.${ts}"
      cp -p "${CENTRAL_DIR}/.env" "${bak}"
      rm -f "${CENTRAL_DIR}/.env"
      info "backed up ${CENTRAL_DIR}/.env -> ${bak} and removed current file"
    fi
    if [[ -f "${CENTRAL_DIR}/config/alertmanager.yml" ]]; then
      rm -f "${CENTRAL_DIR}/config/alertmanager.yml"
      info "removed rendered alertmanager.yml"
    fi
    if [[ -f "${CENTRAL_DIR}/config/grafana/datasources.yaml" ]]; then
      rm -f "${CENTRAL_DIR}/config/grafana/datasources.yaml"
      info "removed rendered datasources.yaml"
    fi
  else
    info "preserving existing central configuration (${CENTRAL_DIR}/.env)"
  fi
  info "central clean completed"
}

clean_edge() {
  local config_mode="${1:-reuse}"
  info "cleaning edge monitoring bundle (mode: ${config_mode})..."

  # 1. Stop edge bundle via Python
  if [[ -f "${EDGE_DIR}/mlx_monitoring_bundle.py" ]]; then
    local py_bin
    py_bin="$(bundle_python)"
    if command -v "${py_bin}" >/dev/null 2>&1; then
      cd "${EDGE_DIR}"
      "${py_bin}" mlx_monitoring_bundle.py stop >/dev/null 2>&1 || true
    fi
  fi

  # 2. Check and terminate any processes tracked by PID files
  if [[ -d "${EDGE_DIR}/bin" ]]; then
    for pidfile in "${EDGE_DIR}/bin/"*.pid; do
      if [[ -f "${pidfile}" ]]; then
        local p
        p="$(cat "${pidfile}" 2>/dev/null | tr -d '[:space:]')"
        if [[ -n "${p}" ]] && kill -0 "${p}" 2>/dev/null; then
          info "terminating PID ${p} ($(basename "${pidfile}"))"
          kill "${p}" 2>/dev/null || true
          sleep 0.2
          if kill -0 "${p}" 2>/dev/null; then
            kill -9 "${p}" 2>/dev/null || true
          fi
        fi
        rm -f "${pidfile}"
      fi
    done
  fi

  # 3. Terminate any orphaned listeners on standard edge exporter ports
  local edge_ports=(8080 9102 9104 9105 9106 8429)
  if command -v lsof >/dev/null 2>&1; then
    for port in "${edge_ports[@]}"; do
      local pids
      pids="$(lsof -ti :${port} 2>/dev/null || true)"
      if [[ -n "${pids}" ]]; then
        for p in ${pids}; do
          info "terminating residual process on port ${port} (PID ${p})"
          kill "${p}" 2>/dev/null || true
          sleep 0.2
          if kill -0 "${p}" 2>/dev/null; then
            kill -9 "${p}" 2>/dev/null || true
          fi
        done
      fi
    done
  fi

  # 4. Terminate any running vmagent
  pkill -f 'vmagent.*promscrape\.config' >/dev/null 2>&1 || true

  # 5. Clean logs and transient cache/buffer files
  if [[ -d "${EDGE_DIR}/logs" ]]; then
    rm -f "${EDGE_DIR}/logs/"*.log
    rm -f "${EDGE_DIR}/logs/vmagent.yml"
    info "cleared edge logs and rendered configs"
  fi
  rm -rf /tmp/vmagent-remotewrite-data* 2>/dev/null || true

  # 6. Handle config
  if [[ "${config_mode}" == "recreate" ]]; then
    local ts
    ts="$(_ist_timestamp)"
    if [[ -f "${EDGE_DIR}/config/nodes.yml" ]]; then
      local bak_nodes="${EDGE_DIR}/config/nodes.yml.bak.${ts}"
      cp -p "${EDGE_DIR}/config/nodes.yml" "${bak_nodes}"
      rm -f "${EDGE_DIR}/config/nodes.yml"
      info "backed up ${EDGE_DIR}/config/nodes.yml -> ${bak_nodes} and removed current file"
    fi
    if [[ -f "${ENV_FILE}" ]]; then
      local bak_env="${ENV_FILE}.bak.${ts}"
      cp -p "${ENV_FILE}" "${bak_env}"
      rm -f "${ENV_FILE}"
      info "backed up ${ENV_FILE} -> ${bak_env} and removed current file"
    fi
  else
    info "preserving existing edge configuration (${ENV_FILE} and ${EDGE_DIR}/config/nodes.yml)"
  fi
  info "edge clean completed"
}

cmd_clean() {
  local target=""
  local config_mode="reuse"
  local mode_explicit=0
  local force=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      central|edge|all)
        target="$1"
        ;;
      --reuse|--reuse-config|-r)
        config_mode="reuse"
        mode_explicit=1
        ;;
      --recreate|--recreate-config|-c)
        config_mode="recreate"
        mode_explicit=1
        ;;
      -y|--yes|-f|--force)
        force=1
        ;;
      -h|--help)
        cat >&2 <<EOF
Usage: ./scripts/deploy.sh clean [target] [options]

Targets:
  central    tear down Central container stack and volumes
  edge       stop edge exporters, clear logs and transient queues
  all        clean both Central and Edge stacks

Options:
  --reuse-config, -r     preserve configuration files (.env, nodes.yml, edge.env) [default]
  --recreate-config, -c  backup existing configuration (IST timestamp) and wipe it
  -y, --yes, -f, --force skip confirmation prompt
EOF
        return 0
        ;;
      *)
        warn "unknown clean option: $1"
        ;;
    esac
    shift
  done

  # Interactive prompts if omitted
  if [[ -z "${target}" ]]; then
    if [[ -t 0 ]]; then
      local choice
      choice=$(_prompt_select "Select target to clean:" \
        "central — tear down central containers and purge volumes" \
        "edge    — stop edge exporters, clear logs and state" \
        "all     — clean both central and edge stacks" \
        "cancel") || die "aborted"
      case "${choice}" in
        central*) target="central" ;;
        edge*)    target="edge" ;;
        all*)     target="all" ;;
        *)        die "aborted" ;;
      esac
    else
      die "confirmation required: pass target (central, edge, all) and -y in non-interactive mode"
    fi
  fi

  if [[ ${mode_explicit} -eq 0 && -t 0 ]]; then
    local mchoice
    mchoice=$(_prompt_select "Configuration handling for ${target}:" \
      "reuse    — preserve configuration files (.env, nodes.yml)" \
      "recreate — backup and wipe configuration to recreate from scratch" \
      "cancel") || die "aborted"
    case "${mchoice}" in
      reuse*)    config_mode="reuse" ;;
      recreate*) config_mode="recreate" ;;
      *)         die "aborted" ;;
    esac
  fi

  # Confirmation gate
  if [[ ${force} -eq 0 ]]; then
    if [[ ! -t 0 ]]; then
      die "confirmation required: pass -y or --force in non-interactive mode"
    fi
    if [[ "${config_mode}" == "recreate" ]]; then
      prompt_confirm_word "cleaning ${target} and wiping configs" "CONFIRM"
    else
      local ans
      ans="$(_prompt_input "Are you sure you want to clean ${target} (config will be kept)? (y/N)" "N")"
      case "${ans}" in
        y|Y|yes|YES) ;;
        *) die "clean aborted by user" ;;
      esac
    fi
  fi

  case "${target}" in
    central)
      clean_central "${config_mode}"
      ;;
    edge)
      clean_edge "${config_mode}"
      ;;
    all)
      clean_central "${config_mode}"
      clean_edge "${config_mode}"
      ;;
    *)
      die "invalid target: ${target}"
      ;;
  esac

  info "clean operation completed successfully at $(_ist_now_display)"
}

cmd_reinstall() {
  local target=""
  local config_mode="reuse"
  local mode_explicit=0
  local with_opik=0
  local force=0

  while [[ $# -gt 0 ]]; do
    case "$1" in
      central|edge|all)
        target="$1"
        ;;
      --reuse|--reuse-config|-r)
        config_mode="reuse"
        mode_explicit=1
        ;;
      --recreate|--recreate-config|-c)
        config_mode="recreate"
        mode_explicit=1
        ;;
      --opik)
        with_opik=1
        ;;
      -y|--yes|-f|--force)
        force=1
        ;;
      -h|--help)
        cat >&2 <<EOF
Usage: ./scripts/deploy.sh reinstall [target] [options]

Targets:
  central    clean and reinstall Central observability container stack
  edge       clean and reinstall Edge monitoring bundle
  all        clean and reinstall both Central and Edge stacks

Options:
  --reuse-config, -r     clean and reinstall using existing configuration [default]
  --recreate-config, -c  backup existing configuration and reconfigure from scratch
  --opik                 (central/all) include Opik suite in reinstallation
  -y, --yes, -f, --force skip confirmation prompt
EOF
        return 0
        ;;
      *)
        warn "unknown reinstall option: $1"
        ;;
    esac
    shift
  done

  # Interactive prompts if omitted
  if [[ -z "${target}" ]]; then
    if [[ -t 0 ]]; then
      local choice
      choice=$(_prompt_select "Select target to reinstall fresh:" \
        "central — clean and reinstall central container stack" \
        "edge    — clean and reinstall edge monitoring stack" \
        "all     — clean and reinstall both central and edge stacks" \
        "cancel") || die "aborted"
      case "${choice}" in
        central*) target="central" ;;
        edge*)    target="edge" ;;
        all*)     target="all" ;;
        *)        die "aborted" ;;
      esac
    else
      die "confirmation required: pass target (central, edge, all) and -y in non-interactive mode"
    fi
  fi

  if [[ ${mode_explicit} -eq 0 && -t 0 ]]; then
    local mchoice
    mchoice=$(_prompt_select "Configuration handling for ${target} reinstallation:" \
      "reuse    — keep existing configuration and redeploy fresh" \
      "recreate — backup and reconfigure fresh from scratch" \
      "cancel") || die "aborted"
    case "${mchoice}" in
      reuse*)    config_mode="reuse" ;;
      recreate*) config_mode="recreate" ;;
      *)         die "aborted" ;;
    esac
  fi

  # Ask about Opik if target is central or all and not explicitly requested
  if [[ ("${target}" == "central" || "${target}" == "all") && ${with_opik} -eq 0 && -t 0 ]]; then
    local opik_choice
    opik_choice="$(_prompt_input "Include Opik tracing suite in reinstallation? (y/N)" "N")"
    if [[ "${opik_choice}" =~ ^[yY] ]]; then
      with_opik=1
    fi
  fi

  # Confirmation gate
  if [[ ${force} -eq 0 ]]; then
    if [[ ! -t 0 ]]; then
      die "confirmation required: pass -y or --force in non-interactive mode"
    fi
    if [[ "${config_mode}" == "recreate" ]]; then
      prompt_confirm_word "fresh reinstallation of ${target} (config recreated)" "REINSTALL"
    else
      local ans
      ans="$(_prompt_input "Confirm fresh reinstallation of ${target} (re-using config)? (y/N)" "N")"
      case "${ans}" in
        y|Y|yes|YES) ;;
        *) die "reinstallation aborted by user" ;;
      esac
    fi
  fi

  if [[ ${force} -eq 1 ]]; then
    export FORCE_DEPLOY=1
  fi

  info "Starting fresh reinstallation of ${target} (mode: ${config_mode}) at $(_ist_now_display)"

  # Step 1: Clean
  case "${target}" in
    central)
      clean_central "${config_mode}"
      ;;
    edge)
      clean_edge "${config_mode}"
      ;;
    all)
      clean_central "${config_mode}"
      clean_edge "${config_mode}"
      ;;
  esac

  # Step 2: Deploy / Reinstall
  if [[ "${target}" == "central" || "${target}" == "all" ]]; then
    info "redeploying central observability stack..."
    if [[ ${with_opik} -eq 1 ]]; then
      cmd_central --opik
    else
      cmd_central
    fi
  fi

  if [[ "${target}" == "edge" || "${target}" == "all" ]]; then
    info "redeploying edge monitoring bundle..."
    if [[ "${config_mode}" == "recreate" ]]; then
      cmd_setup_edge
    else
      cmd_validate
    fi
    cmd_edge daemon
  fi

  info "reinstallation of ${target} completed successfully at $(_ist_now_display)"
}

usage() {
  cat >&2 <<EOF
scripts/deploy.sh — unified CLI for mlx monitoring bundle operations

Usage:
  ./scripts/deploy.sh                             interactive menu
  ./scripts/deploy.sh setup-edge [args]           interactive or parameterized edge setup
  ./scripts/deploy.sh doctor                      automated end-to-end data pipeline traceroute
  ./scripts/deploy.sh review-central              audit all central observability services
  ./scripts/deploy.sh review-edge [node_id]       audit edge exporters and upstream server
  ./scripts/deploy.sh review-vmagent              review and adjust vmagent edge configuration
  ./scripts/deploy.sh review-grafana              review Grafana configuration, credentials and health
  ./scripts/deploy.sh review-vm                   audit VictoriaMetrics TSDB
  ./scripts/deploy.sh review-alertmanager         audit Alertmanager notification routing
  ./scripts/deploy.sh review-vmalert              audit vmalert rule evaluation
  ./scripts/deploy.sh review-otel                 audit OTel Collector pipelines & buffer
  ./scripts/deploy.sh review-opik                 audit Opik LLM observability suite
  ./scripts/deploy.sh clean [target] [options]    tear down containers, stop exporters and purge state
  ./scripts/deploy.sh reinstall [target] [opts]   clean & reinstall fresh (config reuse or recreate)
  ./scripts/deploy.sh reinstall-reuse [target]    clean & reinstall fresh keeping existing configs
  ./scripts/deploy.sh reinstall-recreate [target] clean & reinstall fresh with new configuration from scratch
  ./scripts/deploy.sh edge [start|daemon|stop|status] [node_id]  manage edge services
  ./scripts/deploy.sh validate                    validate edge configuration
  ./scripts/deploy.sh central [--opik]            deploy central Docker/Podman stack

Clean & Reinstall targets:
  central, edge, all (default: prompt interactively, or all in non-interactive)

Clean & Reinstall options:
  --reuse-config, -r       keep existing configuration (.env, nodes.yml, edge.env) [default]
  --recreate-config, -c    backup old config (IST timestamp) and recreate from scratch
  --opik                   (central/all) include Opik suite on reinstall
  -y, --yes, -f, --force   skip confirmation prompt

Setup-edge arguments:
  ./scripts/deploy.sh setup-edge <central_ip> [model_id] [model_path]
  If arguments are omitted, the script asks interactively.

Edge arguments:
  ./scripts/deploy.sh edge start                  start in foreground
  ./scripts/deploy.sh edge daemon                 start in background
  ./scripts/deploy.sh edge status                 show process table and central targets
  ./scripts/deploy.sh edge stop                   cleanly stop all edge exporters
  ./scripts/deploy.sh edge review-vmagent         preview rendered vmagent config and scrape targets
  ./scripts/deploy.sh edge review-edge            audit edge exporters and upstream server

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
    doctor)     shift; cmd_diagnostics doctor "$@" ;;
    review-central) shift; cmd_diagnostics review-central "$@" ;;
    review-edge) shift; cmd_diagnostics review-edge "$@" ;;
    review-vm) shift; cmd_diagnostics review-vm "$@" ;;
    review-alertmanager) shift; cmd_diagnostics review-alertmanager "$@" ;;
    review-vmalert) shift; cmd_diagnostics review-vmalert "$@" ;;
    review-otel) shift; cmd_diagnostics review-otel "$@" ;;
    review-opik) shift; cmd_diagnostics review-opik "$@" ;;
    review-vmagent|vmagent-config) shift; cmd_review_vmagent "$@" ;;
    review-grafana|grafana-config) shift; cmd_review_grafana "$@" ;;
    clean)              shift; cmd_clean "$@" ;;
    reinstall)          shift; cmd_reinstall "$@" ;;
    reinstall-reuse)    shift; cmd_reinstall --reuse-config "$@" ;;
    reinstall-recreate) shift; cmd_reinstall --recreate-config "$@" ;;
    edge)       shift; cmd_edge "$@" ;;
    edge-all)   shift; cmd_edge "$@" ;;
    central)    shift; cmd_central "$@" ;;
    help|-h|--help) usage ;;
    "")
      # Interactive menu mode (following scripts/avika pattern)
      local choice
      choice=$(_prompt_select "MLX Monitoring Bundle operations:" \
        "setup-edge          — setup edge node (detect venvs, vmagent, generate config)" \
        "edge daemon         — start edge monitoring stack in background" \
        "edge status         — check running status of edge exporters" \
        "edge stop           — stop edge monitoring bundle" \
        "edge start          — start edge bundle in foreground" \
        "doctor              — automated end-to-end data pipeline traceroute & diagnostics" \
        "review-central      — comprehensive review of all central services" \
        "review-edge         — comprehensive review of edge exporters & server" \
        "review-vmagent      — review and adjust vmagent edge configuration" \
        "review-grafana      — review Grafana configuration, credentials and health" \
        "review-vm           — granular review of VictoriaMetrics TSDB" \
        "review-vmalert      — granular review of vmalert alert evaluation engine" \
        "review-alertmanager — granular review of Alertmanager notification routing" \
        "review-otel         — granular review of OTel Collector pipelines" \
        "review-opik         — granular review of Opik tracing platform" \
        "validate            — validate nodes.yml and edge.env" \
        "central             — deploy central observability stack" \
        "central --opik      — deploy central observability stack with Opik" \
        "reinstall-reuse     — clean & reinstall fresh keeping existing configs" \
        "reinstall-recreate  — clean & reinstall fresh with new configuration from scratch" \
        "clean               — tear down containers, stop exporters and purge state" \
        "quit") || die "bye"

      case "${choice}" in
        setup-edge*) cmd_setup_edge ;;
        "edge daemon"*) cmd_edge daemon ;;
        "edge status"*) cmd_edge status ;;
        "edge stop"*) cmd_edge stop ;;
        "edge start"*) cmd_edge start ;;
        doctor*) cmd_diagnostics doctor ;;
        review-central*) cmd_diagnostics review-central ;;
        review-edge*) cmd_diagnostics review-edge ;;
        review-vmagent*) cmd_review_vmagent ;;
        review-grafana*) cmd_review_grafana ;;
        review-vm*) cmd_diagnostics review-vm ;;
        review-vmalert*) cmd_diagnostics review-vmalert ;;
        review-alertmanager*) cmd_diagnostics review-alertmanager ;;
        review-otel*) cmd_diagnostics review-otel ;;
        review-opik*) cmd_diagnostics review-opik ;;
        validate*) cmd_validate ;;
        "central --opik"*) cmd_central --opik ;;
        central*) cmd_central ;;
        reinstall-reuse*) cmd_reinstall --reuse-config ;;
        reinstall-recreate*) cmd_reinstall --recreate-config ;;
        clean*) cmd_clean ;;
        quit) die "bye" ;;
      esac
      ;;
    *)
      usage
      die "unknown command: $1"
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
