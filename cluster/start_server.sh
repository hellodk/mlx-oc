#!/bin/zsh
# =============================================================================
# Start / stop / status for the distributed MLX stack.
#
# start / stop / restart delegate to the Ansible lifecycle playbook
# (infra/ansible/cluster.yml). The playbook is driven entirely by the stack
# manifest cluster/mlx-stack.json — the single file that says which components
# run, on which node, on which port, and which kill patterns prove a clean
# stop. Anything Ansible starts is stopped and verified by Ansible; there is no
# second copy of the process list to drift.
#
# status and logs stay local (read-only) so you can inspect the stack without
# an Ansible run. Both read cluster/cluster.env (ports) and cluster/hosts.json
# (the peer list) — both rendered by the same playbook.
#
# Usage:
#   ./start_server.sh [start]    bring the whole stack up (ansible --tags start)
#   ./start_server.sh stop       stop everything, local + every peer (--tags stop)
#   ./start_server.sh restart    stop then start
#   ./start_server.sh status     one-line status per component
#   ./start_server.sh logs       tail the last 40 lines of every log
# =============================================================================
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
ANSIBLE="$(command -v ansible-playbook || echo /opt/homebrew/bin/ansible-playbook)"
INV="$REPO/infra/ansible/inventories/generated/hosts.yml"
PLAY="$REPO/infra/ansible/cluster.yml"
LOG="$DIR/logs"

ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }

run_play() { # run_play <tags>
  echo "[$(ts)] $ANSIBLE -i $INV $PLAY --tags $1"
  (cd "$REPO" && "$ANSIBLE" -i "$INV" "$PLAY" --tags "$1")
}

# --- status (read-only; ports + peers come from rendered config) ------------
source "$DIR/cluster.env"
SERVER_HOST="${MLX_SERVER_IP:-127.0.0.1}"
SERVER_PORT="${MLX_SERVER_PORT:-8081}"
PROXY_PORT="${MLX_PROXY_LISTEN##*:}"
SUPERVISOR_PORT="${MLX_SUPERVISOR_LISTEN##*:}"
HW_PORT="${MLX_HW_LISTEN##*:}"
KV_PORT="${MLX_KV_LISTEN##*:}"
LOGTAILER_PORT="${MLX_LOGTAILER_LISTEN##*:}"
PID_SRV="$LOG/supervisor.pid"
PID_PROXY="$LOG/proxy.pid"
PID_HW0="$LOG/hw0.pid"
PID_KV="$LOG/kv.pid"
PID_LT="$LOG/logtailer.pid"

alive() { # alive <pidfile>
  [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null
}

load_topology() {
  local hostfile="$REPO/$MLX_HOSTFILE"
  [[ -f "$hostfile" ]] || { echo "ERROR: hostfile not found: $hostfile (MLX_HOSTFILE=$MLX_HOSTFILE)" >&2; return 1; }
  local parsed
  parsed="$("$REPO/.venv/bin/python" - "$hostfile" 2>/dev/null <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("backend", "ring"))
for h in d["hosts"]:
    ssh = h.get("ssh") or (h.get("ips") or [""])[0]
    print(ssh)
PY
)"
  [[ -n "$parsed" ]] || { echo "ERROR: could not parse $hostfile" >&2; return 1; }
  local -a lines=("${(f)parsed}")
  MLX_BACKEND="${lines[1]:-ring}"
  MLX_RANK_SSH=()
  local i=1
  while (( 2 + (i-1) <= ${#lines} )); do
    MLX_RANK_SSH+=("${lines[$((1 + i))]}")
    i=$((i+1))
  done
  MLX_NRANKS="${#MLX_RANK_SSH}"
  PEERS=("${MLX_RANK_SSH[@]:1}")
}

status() {
  load_topology || return 1
  echo "[$(ts)] mlx cluster status ($MLX_NRANKS nodes)"
  local up name port
  up="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://$SERVER_HOST:$SERVER_PORT/v1/models" 2>/dev/null)"; [[ "$up" == 200 ]] && up=UP || up=DOWN
  echo "  mlx_lm.server   :$SERVER_PORT  $up"
  for pidf in "$PID_SRV" "$PID_PROXY" "$PID_HW0" "$PID_KV" "$PID_LT"; do
    case "$pidf" in
      *supervisor*) name=supervisor;  port="$SUPERVISOR_PORT" ;;
      *proxy*)      name=proxy;       port="$PROXY_PORT" ;;
      *hw0*)        name=hw_rank0;    port="$HW_PORT" ;;
      *kv*)         name=kv_agent;    port="$KV_PORT" ;;
      *logtailer*)  name=logtailer;   port="$LOGTAILER_PORT" ;;
    esac
    if alive "$pidf"; then
      local http="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$port/metrics" 2>/dev/null)"
      echo "  $name  :$port  RUNNING (pid $(cat "$pidf"), /metrics $http)"
    else
      echo "  $name  :$port  STOPPED"
    fi
  done
  local peer i=0
  for peer in "${PEERS[@]}"; do
    i=$((i+1))
    local pup="$(ssh -o ConnectTimeout=5 "$peer" "curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:$HW_PORT/metrics" 2>/dev/null)"
    echo "  hw_rank$i  $peer:$HW_PORT  ${pup:-000} (${pup:+metric HTTP $pup})"
  done
}

# --- main ---------------------------------------------------------------------
ACTION="${1:-start}"
case "$ACTION" in
  start)   run_play start ;;
  stop)    run_play stop ;;
  restart) run_play stop && sleep 2 && run_play start ;;
  status)  status ;;
  logs)
    for f in "$LOG"/supervisor.log "$LOG"/server.log "$LOG"/proxy.log "$LOG"/hw0.log "$LOG"/kvagent.log "$LOG"/logtailer.log; do
      [[ -f "$f" ]] || continue
      echo "--- $f ---"
      tail -40 "$f"
    done
    ;;
  *) echo "usage: $0 [start|stop|restart|status|logs]" >&2; exit 2 ;;
esac
