#!/bin/zsh
# =============================================================================
# Start / stop / status for the distributed MLX stack.
#
# Topology (N-node ring; all addresses come from cluster/cluster.env + hosts.json):
#   mlx_server_supervisor.py   :9105  watchdog for mlx_lm.server (restarts it
#                                      with backoff, exports serving-status)
#   mlx_lm.server (mlx.launch) :8081  rank 0 only (httpd bind = MLX_SERVER_IP)
#   mlx_metrics_proxy.py       :8080  0.0.0.0 public OpenAI API + /metrics
#   mlx_hw_telemetry.py        :9102  on EVERY node (Prometheus hardware metrics)
#   mlx_kv_cache_agent.py      :9104  KV-cache / context-length gauges
#   mlx_server_log_tailer.py   :9106  streams server.log -> Opik (OTLP logs)
#
# CONFIG: cluster/cluster.env is the single source of truth for model, ports,
# server bind IP and observability endpoints. hosts.json (MLX_HOSTFILE) is the
# node list: entry order == ring rank order, host[0] == rank 0 == the serving
# node (this script must run there), every other entry is a ring peer that
# runs hw telemetry. The same repo is correct from any node.
#
# opencode and other clients talk to the proxy on MLX_PROXY_LISTEN; the proxy
# records TTFT, token rate, temperature and hallucination-risk heuristics, and
# can export OpenTelemetry spans/metrics/logs.
#
# Usage:
#   ./start_server.sh [start]           start the whole stack
#   ./start_server.sh stop              stop everything (local + every peer)
#   ./start_server.sh status            one-line status per component
#   ./start_server.sh restart           stop then start
#   ./start_server.sh logs              tail the last 40 lines of every log
# =============================================================================
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
VENV="$REPO/.venv"                       # py3.14: proxy / hw / kv / supervisor
MLX_VENV="$HOME/venvs/mlx"               # py3.12: mlx.launch + mlx_lm.server

# Single source of truth: cluster/cluster.env. Exported so every child process
# below (and anything invoked in this shell afterward) inherits it without
# needing its own flag.
source "$DIR/cluster.env"
export MLX_MODEL MLX_DEFAULT_TEMP
export MLX_LOGPROBS MLX_LOGPROBS_STREAM_SAMPLE MLX_LOW_CONFIDENCE
export MLX_MAX_PROMPT_TOKENS MLX_MAX_TOKENS_CAP
export MLX_HOSTFILE MLX_SERVER_IP MLX_SERVER_PORT MLX_RING_SUBNET
export MLX_PROXY_LISTEN MLX_SUPERVISOR_LISTEN MLX_HW_LISTEN MLX_KV_LISTEN MLX_LOGTAILER_LISTEN
export MLX_OTLP_ENDPOINT MLX_OPIK_OTLP_ENDPOINT OPIK_BASE MLX_JUDGE_URL
MODEL="$MLX_MODEL"
SERVER_HOST="${MLX_SERVER_IP:-127.0.0.1}"
SERVER_PORT="${MLX_SERVER_PORT:-8081}"
PROXY_PORT="${MLX_PROXY_LISTEN##*:}"
SUPERVISOR_PORT="${MLX_SUPERVISOR_LISTEN##*:}"
HW_PORT="${MLX_HW_LISTEN##*:}"
KV_PORT="${MLX_KV_LISTEN##*:}"
LOGTAILER_PORT="${MLX_LOGTAILER_LISTEN##*:}"

LOG="$DIR/logs"
BOOT="$LOG/bootstrap.log"
PID_SRV="$LOG/supervisor.pid"
PID_PROXY="$LOG/proxy.pid"
PID_HW0="$LOG/hw0.pid"
PID_KV="$LOG/kv.pid"
PID_LT="$LOG/logtailer.pid"

mkdir -p "$LOG"

# --- logging helpers ---------------------------------------------------------
ts() { date "+%Y-%m-%dT%H:%M:%S%z"; }
info() { echo "[$(ts)] [start] $*" | tee -a "$BOOT"; }
warn() { echo "[$(ts)] [start] WARN $*" | tee -a "$BOOT"; }
fail() { echo "[$(ts)] [start] ERROR $*" | tee -a "$BOOT"; }

alive() { # alive <pidfile>
  [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null
}

wait_port_free() { # wait_port_free <port> [ttl]
  local port=$1 ttl=${2:-30} t0=$SECONDS
  while (( SECONDS - t0 < ttl )); do
    if ! lsof -nP -iTCP:$port -sTCP:LISTEN >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  warn "port $port still in use after ${ttl}s"
  return 1
}

# --- topology (hosts.json) ----------------------------------------------------
# Populates MLX_BACKEND, MLX_RANK_SSH/MLX_RANK_IP (1-based arrays in zsh),
# MLX_NRANKS, RANK0_SSH, RANK0_IP and PEERS. hosts.json entry order IS the ring
# rank order; host[1] is rank 0 (the serving node).
load_topology() {
  local hostfile="$REPO/$MLX_HOSTFILE"
  [[ -f "$hostfile" ]] || { fail "hostfile not found: $hostfile (MLX_HOSTFILE=$MLX_HOSTFILE)"; return 1; }
  local parsed
  parsed="$("$VENV/bin/python" - "$hostfile" 2>/dev/null <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get("backend", "ring"))
for h in d["hosts"]:
    ssh = h.get("ssh") or (h.get("ips") or [""])[0]
    mesh = (h.get("ips") or [ssh])[0]
    print(ssh)
    print(mesh)
PY
)"
  [[ -n "$parsed" ]] || { fail "could not parse $hostfile"; return 1; }
  local -a lines=("${(f)parsed}")
  MLX_BACKEND="${lines[1]:-ring}"
  MLX_RANK_SSH=(); MLX_RANK_IP=()
  local i=1
  while (( 2 + 2*(i-1) <= ${#lines} )); do
    MLX_RANK_SSH+=("${lines[$((2 + 2*(i-1)))]}")
    MLX_RANK_IP+=("${lines[$((3 + 2*(i-1)))]}")
    i=$((i+1))
  done
  MLX_NRANKS="${#MLX_RANK_SSH}"
  RANK0_SSH="${MLX_RANK_SSH[1]}"
  RANK0_IP="${MLX_RANK_IP[1]}"
  PEERS=("${MLX_RANK_SSH[@]:1}")
  (( MLX_NRANKS >= 1 )) || { fail "hostfile $hostfile has no hosts"; return 1; }
  info "topology: backend=$MLX_BACKEND nodes=$MLX_NRANKS rank0=$RANK0_IP (ssh=$RANK0_SSH) peers=${#PEERS} server=$SERVER_HOST:$SERVER_PORT"
}

# --- preflight ----------------------------------------------------------------
preflight() {
  load_topology || return 1
  info "preflight: venv=$VENV mlx_venv=$MLX_VENV model=$MODEL"
  local ok=1
  ifconfig 2>/dev/null | grep -Eq "inet ($SERVER_HOST|$RANK0_IP) " \
    || { fail "this machine is not rank 0 (owns neither $SERVER_HOST nor $RANK0_IP) - run start_server.sh ON the serving node"; ok=0; }
  [[ -x "$VENV/bin/python" ]]  || { fail "missing $VENV/bin/python (run: python3 -m venv $VENV)"; ok=0; }
  [[ -x "$MLX_VENV/bin/mlx.launch" ]] || { fail "missing $MLX_VENV/bin/mlx.launch (py3.12 venv)"; ok=0; }
  "$VENV/bin/python" -c "import prometheus_client" 2>/dev/null \
    || { fail "$VENV missing prometheus_client"; ok=0; }
  local peer
  for peer in "${PEERS[@]}"; do
    ssh -o ConnectTimeout=5 -o BatchMode=yes "$peer" "true" 2>/dev/null \
      || { fail "cannot ssh to $peer (passwordless auth required)"; ok=0; }
  done
  for p in "$SERVER_PORT" "$PROXY_PORT" "$HW_PORT" "$KV_PORT" "$SUPERVISOR_PORT" "$LOGTAILER_PORT"; do
    if lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
      warn "port $p already in use - is the stack already running?"
    fi
  done
  (( ok )) || { fail "preflight failed"; return 1; }
  info "preflight OK"
}

# --- component launchers -----------------------------------------------------
start_server() {
  info "starting mlx_lm.server via supervisor -> logs/server.log, metrics :$SUPERVISOR_PORT"
  # The distributed server MUST use the py3.12 venv (~/venvs/mlx): macOS
  # Local-Network privacy silently blocks the third-party py3.14 binary from
  # reaching local addresses when spawned over SSH (EHOSTUNREACH, no TCC entry).
  local srv_cmd="$MLX_VENV/bin/mlx.launch --hostfile $REPO/$MLX_HOSTFILE --backend $MLX_BACKEND \
 --cwd $DIR --python $MLX_VENV/bin/python -- $MLX_VENV/bin/python $DIR/mlx_server_launcher.py \
 --model $MODEL --host $SERVER_HOST --port $SERVER_PORT \
 --chat-template-args '{\"enable_thinking\":false}' \
 --prompt-cache-size 4 --prompt-cache-bytes 2g --prompt-concurrency 4"

  nohup "$VENV/bin/python" "$DIR/mlx_server_supervisor.py" \
    --model "$MODEL" \
    --hostfile "$REPO/$MLX_HOSTFILE" \
    --health "http://$SERVER_HOST:$SERVER_PORT/v1/models" \
    --server-log "$LOG/server.log" \
    --listen "$MLX_SUPERVISOR_LISTEN" \
    --probe-interval 5 \
    --backoff 3 \
    --backoff-max 30 \
    --hang-after 15 \
    --backoff-reset 120 \
    --command "$srv_cmd" \
    > "$LOG/supervisor.log" 2>&1 &
  echo $! > "$PID_SRV"
  disown
  info "supervisor pid $(cat "$PID_SRV") -> logs/supervisor.log"
}

start_proxy() {
  info "starting mlx_metrics_proxy -> logs/proxy.log"
  local proxy_args=(--listen "$MLX_PROXY_LISTEN" --upstream "$SERVER_HOST:$SERVER_PORT"
                    --default-temp "$MLX_DEFAULT_TEMP" --node-name rank0 --otlp-endpoint "$MLX_OTLP_ENDPOINT"
                    --opik-otlp-endpoint "$MLX_OPIK_OTLP_ENDPOINT" --model "$MODEL"
                    --logprobs "$MLX_LOGPROBS"
                    --logprobs-stream-sample "$MLX_LOGPROBS_STREAM_SAMPLE"
                    --low-confidence-threshold "$MLX_LOW_CONFIDENCE"
                    --max-prompt-tokens "$MLX_MAX_PROMPT_TOKENS"
                    --max-tokens-cap "$MLX_MAX_TOKENS_CAP")
  if [[ -n "${OPIK_ENDPOINT:-}" ]]; then proxy_args+=(--opik-endpoint "$OPIK_ENDPOINT"); fi
  nohup "$VENV/bin/python" "$DIR/mlx_metrics_proxy.py" "${proxy_args[@]}" \
    > "$LOG/proxy.log" 2>&1 &
  echo $! > "$PID_PROXY"
  disown
  info "proxy pid $(cat "$PID_PROXY") -> logs/proxy.log"
}

start_hw() {
  info "starting hw telemetry rank0 (local) -> logs/hw0.log"
  nohup "$VENV/bin/python" "$DIR/mlx_hw_telemetry.py" \
    --node-name rank0 --listen "$MLX_HW_LISTEN" \
    > "$LOG/hw0.log" 2>&1 &
  echo $! > "$PID_HW0"
  disown

  local i node name
  for (( i=2; i <= MLX_NRANKS; i++ )); do
    node="${MLX_RANK_SSH[$i]}"
    name="rank$((i-1))"
    info "starting hw telemetry $name ($node:$HW_PORT) -> logs/hw$((i-1)).log"
    nohup ssh -o ConnectTimeout=5 "$node" \
      "nohup '$MLX_VENV/bin/python' '$DIR/mlx_hw_telemetry.py' \
       --node-name '$name' --listen '$MLX_HW_LISTEN' \
       > '$LOG/hw$((i-1)).log' 2>&1 &" \
      > /dev/null 2>&1 &
    disown
  done
}

start_kv() {
  info "starting mlx_kv_cache_agent -> logs/kvagent.log"
  nohup "$VENV/bin/python" "$DIR/mlx_kv_cache_agent.py" \
    --log-file "$LOG/server.log" \
    --model "$MODEL" \
    --listen "$MLX_KV_LISTEN" \
    > "$LOG/kvagent.log" 2>&1 &
  echo $! > "$PID_KV"
  disown
  info "kv agent pid $(cat "$PID_KV") -> logs/kvagent.log"
}

start_logtailer() {
  info "starting mlx_server_log_tailer -> otel-collector logs -> logs/logtailer.log"
  wait_port_free "$LOGTAILER_PORT" 20 || true
  nohup "$VENV/bin/python" "$DIR/mlx_server_log_tailer.py" \
    --log-file "$LOG/server.log" \
    --otlp-endpoint "$MLX_OTLP_ENDPOINT" \
    --project mlx \
    --listen "$MLX_LOGTAILER_LISTEN" \
    > "$LOG/logtailer.log" 2>&1 &
  echo $! > "$PID_LT"
  disown
  info "logtailer pid $(cat "$PID_LT") -> logs/logtailer.log"
}

# --- readiness ----------------------------------------------------------------
wait_ready() {
  local ttl=${1:-180}
  local t0=$SECONDS
  info "waiting for mlx_lm.server readiness (ttl=${ttl}s)..."
  while (( SECONDS - t0 < ttl )); do
    if curl -sf -m 2 "http://$SERVER_HOST:$SERVER_PORT/v1/models" >/dev/null 2>&1; then
      info "mlx_lm.server ready after $((SECONDS - t0))s"
      return 0
    fi
    sleep 2
  done
  warn "mlx_lm.server not ready after ${ttl}s; tail logs/server.log"
  return 1
}

# --- status -------------------------------------------------------------------
status() {
  load_topology || return 1
  echo "[$(ts)] mlx cluster status ($MLX_NRANKS nodes)"
  local up down name port
  up="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://$SERVER_HOST:$SERVER_PORT/v1/models" 2>/dev/null)"; [[ "$up" == 200 ]] && up=UP || up=DOWN
  echo "  mlx_lm.server   :$SERVER_PORT  $up (process: $(pgrep -f mlx_server_launcher | wc -l | tr -d ' ') alive)"
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
  if [[ -f "$LOG/server.log" ]]; then
    echo "  last crash: $(grep -m1 -E 'METAL|terminating' "$LOG/server.log" 2>/dev/null || echo 'none')"
  fi
}

# --- stop ----------------------------------------------------------------------
stop() {
  load_topology || return 1
  info "stopping stack (local + ${#PEERS} peers)"
  for pidf in "$PID_SRV" "$PID_PROXY" "$PID_HW0" "$PID_KV" "$PID_LT"; do
    alive "$pidf" && kill "$(cat "$pidf")" 2>/dev/null && info "killed $(basename "$pidf") pid $(cat "$pidf")"
  done
  # Belt-and-braces for stragglers (supervisor term propagates to mlx.launch,
  # but the distributed python -m mlx_lm.server workers can survive it).
  pkill -f "mlx_metrics_proxy.py" 2>/dev/null
  pkill -f "mlx_kv_cache_agent.py" 2>/dev/null
  pkill -f "mlx_hw_telemetry.py" 2>/dev/null
  pkill -f "mlx_server_supervisor.py" 2>/dev/null
  pkill -f "mlx_server_log_tailer.py" 2>/dev/null
  pkill -f "mlx_lm.server" 2>/dev/null
  pkill -f "mlx_server_launcher" 2>/dev/null
  pkill -f "mlx.launch" 2>/dev/null
  # Bracket the dots so the wrapper's own cmdline cannot match and kill the
  # cleanup mid-run (see note in _kill_remote_rank).
  local peer
  for peer in "${PEERS[@]}"; do
    ssh -o ConnectTimeout=5 "$peer" "pkill -f 'mlx_hw_telemetry[.]py'; pkill -f 'mlx_lm[.]server'; pkill -f 'mlx_server_launcher[.]py'; pkill -f 'mlx[.]launch'" 2>/dev/null
  done
  for pidf in "$PID_SRV" "$PID_PROXY" "$PID_HW0" "$PID_KV" "$PID_LT"; do rm -f "$pidf"; done
  info "stopped"
}

# --- main ---------------------------------------------------------------------
ACTION="${1:-start}"
case "$ACTION" in
  start)
    preflight || exit 1
    # After a stop, the old supervisor/server can take a few seconds to release
    # :$SUPERVISOR_PORT/:$SERVER_PORT; spawning the new supervisor first would
    # die on EADDRINUSE.
    wait_port_free "$SUPERVISOR_PORT" 30 || true
    wait_port_free "$SERVER_PORT" 30 || true
    start_server
    start_proxy
    start_hw
    start_kv
    start_logtailer
    wait_ready || exit 1
    sleep 3
    status
    echo
    info "poll: curl -s http://127.0.0.1:$PROXY_PORT/v1/models"
    info "serving: curl -s http://127.0.0.1:$SUPERVISOR_PORT/metrics | grep mlx_server"
    ;;
  stop) stop ;;
  restart) stop; sleep 2; preflight || exit 1; wait_port_free "$SUPERVISOR_PORT" 30 || true; wait_port_free "$SERVER_PORT" 30 || true; start_server; start_proxy; start_hw; start_kv; start_logtailer; wait_ready || exit 1; status ;;
  status) status ;;
  logs)
    for f in "$LOG"/supervisor.log "$LOG"/server.log "$LOG"/proxy.log "$LOG"/hw0.log "$LOG"/kvagent.log "$LOG"/logtailer.log; do
      [[ -f "$f" ]] || continue
      echo "--- $f ---"
      tail -40 "$f"
    done
    ;;
  *) echo "usage: $0 [start|stop|restart|status|logs]" >&2; exit 2 ;;
esac
