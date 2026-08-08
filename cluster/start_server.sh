#!/bin/zsh
# =============================================================================
# Start / stop / status for the distributed MLX stack on both Mac minis.
#
# Topology:
#   mlx_server_supervisor.py   :9105  watchdog for mlx_lm.server (restarts it
#                                      with backoff, exports serving-status)
#   mlx_lm.server (mlx.launch) :8081  rank 0 only, bound to 127.0.0.1 (internal)
#   mlx_metrics_proxy.py       :8080  0.0.0.0 public OpenAI API + /metrics
#   mlx_hw_telemetry.py        :9102  on BOTH nodes (Prometheus hardware metrics)
#   mlx_kv_cache_agent.py      :9104  KV-cache / context-length gauges
#   mlx_server_log_tailer.py   :9106  streams server.log -> Opik (OTLP logs)
#
# opencode and other clients talk to the proxy on :8080; the proxy records
# TTFT, token rate, temperature and hallucination-risk heuristics, and can
# export OpenTelemetry spans/metrics/logs.
#
# Repo layout:  .venv/ at repo root (py3.14, rank0 proxy), cluster/ holds the
# runtime scripts, tools/ holds experiments + bench.py.
#
# Usage:
#   ./start_server.sh [start]           start the whole stack
#   ./start_server.sh stop              stop everything (local + node B)
#   ./start_server.sh status            one-line status per component
#   ./start_server.sh restart           stop then start
#   ./start_server.sh logs              tail the last 40 lines of every log
# =============================================================================
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
VENV="$REPO/.venv"                       # py3.14: proxy / hw / kv / supervisor
MLX_VENV="$HOME/venvs/mlx"               # py3.12: mlx.launch + mlx_lm.server

# Single source of truth for the model id: cluster/cluster.env. Exported so
# every child process below (and anything invoked in this shell afterward)
# inherits it without needing its own --model flag.
source "$DIR/cluster.env"
export MLX_MODEL MLX_DEFAULT_TEMP
export MLX_LOGPROBS MLX_LOGPROBS_STREAM_SAMPLE MLX_LOW_CONFIDENCE
MODEL="$MLX_MODEL"

LOG="$DIR/logs"
RANK1="10.0.0.2"

# Opik OTLP ingestion for traces (proxy) + logs (logtailer). On by default so
# one trace per request lands in the "mlx" project; override to disable.
OPIK_OTLP="${OPIK_OTLP_ENDPOINT:-http://192.168.1.10:32173/api/v1/private/otel}"

# Primary OTLP gateway (otel-collector): metrics/traces/logs from the proxy and
# the logtailer. Its logs pipeline writes a durable JSONL file.
OTLP="${MLX_OTLP_ENDPOINT:-http://192.168.1.64:4318}"

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

# --- preflight ----------------------------------------------------------------
preflight() {
  info "preflight: venv=$VENV mlx_venv=$MLX_VENV model=$MODEL rank1=$RANK1"
  local ok=1
  [[ -x "$VENV/bin/python" ]]  || { fail "missing $VENV/bin/python (run: python3 -m venv $VENV)"; ok=0; }
  [[ -x "$MLX_VENV/bin/mlx.launch" ]] || { fail "missing $MLX_VENV/bin/mlx.launch (py3.12 venv)"; ok=0; }
  "$VENV/bin/python" -c "import prometheus_client" 2>/dev/null \
    || { fail "$VENV missing prometheus_client"; ok=0; }
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$RANK1" "true" 2>/dev/null \
    || { fail "cannot ssh to $RANK1 (passwordless auth required)"; ok=0; }
  for p in 8081 8080 9102 9104 9105 9106; do
    if lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null 2>&1; then
      warn "port $p already in use - is the stack already running?"
    fi
  done
  (( ok )) || { fail "preflight failed"; return 1; }
  info "preflight OK"
}

# --- component launchers -----------------------------------------------------
start_server() {
  info "starting mlx_lm.server via supervisor -> logs/server.log, metrics :9105"
  # The distributed server MUST use the py3.12 venv (~/venvs/mlx): nodeB's macOS
  # Local-Network privacy silently blocks the third-party py3.14 binary from
  # reaching local addresses when spawned over SSH (EHOSTUNREACH, no TCC entry).
  local srv_cmd="$MLX_VENV/bin/mlx.launch --hostfile $DIR/hosts.json --backend ring \
--cwd $DIR --python $MLX_VENV/bin/python -- $MLX_VENV/bin/python -m mlx_lm.server \
--model $MODEL --host 127.0.0.1 --port 8081 \
--chat-template-args '{\"enable_thinking\":false}'"

  nohup "$VENV/bin/python" "$DIR/mlx_server_supervisor.py" \
    --model "$MODEL" \
    --health http://127.0.0.1:8081/v1/models \
    --server-log "$LOG/server.log" \
    --listen 0.0.0.0:9105 \
    --command "$srv_cmd" \
    > "$LOG/supervisor.log" 2>&1 &
  echo $! > "$PID_SRV"
  disown
  info "supervisor pid $(cat "$PID_SRV") -> logs/supervisor.log"
}

start_proxy() {
  info "starting mlx_metrics_proxy -> logs/proxy.log"
  local proxy_args=(--listen 0.0.0.0:8080 --upstream 127.0.0.1:8081
                    --default-temp "$MLX_DEFAULT_TEMP" --node-name rank0 --otlp-endpoint "$OTLP"
                    --opik-otlp-endpoint "$OPIK_OTLP" --model "$MODEL"
                    --logprobs "$MLX_LOGPROBS"
                    --logprobs-stream-sample "$MLX_LOGPROBS_STREAM_SAMPLE"
                    --low-confidence-threshold "$MLX_LOW_CONFIDENCE")
  if [[ -n "${OPIK_ENDPOINT:-}" ]]; then proxy_args+=(--opik-endpoint "$OPIK_ENDPOINT"); fi
  nohup "$VENV/bin/python" "$DIR/mlx_metrics_proxy.py" "${proxy_args[@]}" \
    > "$LOG/proxy.log" 2>&1 &
  echo $! > "$PID_PROXY"
  disown
  info "proxy pid $(cat "$PID_PROXY") -> logs/proxy.log"
}

start_hw() {
  info "starting hw telemetry rank0 (:9102) -> logs/hw0.log"
  nohup "$VENV/bin/python" "$DIR/mlx_hw_telemetry.py" \
    --node-name rank0 --listen 0.0.0.0:9102 \
    > "$LOG/hw0.log" 2>&1 &
  echo $! > "$PID_HW0"
  disown
  info "hw rank0 pid $(cat "$PID_HW0")"

  info "starting hw telemetry rank1 ($RANK1:9102) -> logs/hw1.log"
  nohup ssh -o ConnectTimeout=5 "$RANK1" \
    "nohup '$MLX_VENV/bin/python' '$DIR/mlx_hw_telemetry.py' \
     --node-name rank1 --listen 0.0.0.0:9102 \
     > '$LOG/hw1.log' 2>&1 &" \
    > /dev/null 2>&1 &
  disown
  info "hw rank1 launching over ssh"
}

start_kv() {
  info "starting mlx_kv_cache_agent -> logs/kvagent.log"
  nohup "$VENV/bin/python" "$DIR/mlx_kv_cache_agent.py" \
    --log-file "$LOG/server.log" \
    --model "$MODEL" \
    --listen 0.0.0.0:9104 \
    > "$LOG/kvagent.log" 2>&1 &
  echo $! > "$PID_KV"
  disown
  info "kv agent pid $(cat "$PID_KV") -> logs/kvagent.log"
}

start_logtailer() {
  info "starting mlx_server_log_tailer -> otel-collector logs -> logs/logtailer.log"
  wait_port_free 9106 20 || true
  nohup "$VENV/bin/python" "$DIR/mlx_server_log_tailer.py" \
    --log-file "$LOG/server.log" \
    --otlp-endpoint "$OTLP" \
    --project mlx \
    --listen 0.0.0.0:9106 \
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
    if curl -sf -m 2 http://127.0.0.1:8081/v1/models >/dev/null 2>&1; then
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
  echo "[$(ts)] mlx cluster status"
  local up down name port
  up="$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:8081/v1/models 2>/dev/null)"; [[ "$up" == 200 ]] && up=UP || up=DOWN
  echo "  mlx_lm.server   :8081  $up (process: $(pgrep -f mlx_lm.server | wc -l | tr -d ' ') alive)"
  for pidf in "$PID_SRV" "$PID_PROXY" "$PID_HW0" "$PID_KV" "$PID_LT"; do
    case "$pidf" in
      *supervisor*) name=supervisor; port=9105 ;;
      *proxy*)      name=proxy;      port=8080 ;;
      *hw0*)        name=hw_rank0;   port=9102 ;;
      *kv*)         name=kv_agent;   port=9104 ;;
      *logtailer*)  name=logtailer;  port=9106 ;;
    esac
    if alive "$pidf"; then
      local http="$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$port/metrics" 2>/dev/null)"
      echo "  $name  :$port  RUNNING (pid $(cat "$pidf"), /metrics $http)"
    else
      echo "  $name  :$port  STOPPED"
    fi
  done
  local rank1up="$(ssh -o ConnectTimeout=5 "$RANK1" "curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:9102/metrics" 2>/dev/null)"
  echo "  hw_rank1        :9102  ${rank1up:-000} (${rank1up:+metric HTTP $rank1up})"
  if [[ -f "$LOG/server.log" ]]; then
    echo "  last crash: $(grep -m1 -E 'METAL|terminating' "$LOG/server.log" 2>/dev/null || echo 'none')"
  fi
}

# --- stop ----------------------------------------------------------------------
stop() {
  info "stopping stack (local + $RANK1)"
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
  pkill -f "mlx.launch" 2>/dev/null
  ssh -o ConnectTimeout=5 "$RANK1" "pkill -f 'mlx_hw_telemetry.py'; pkill -f 'mlx_lm.server'; pkill -f 'mlx.launch'" 2>/dev/null
  for pidf in "$PID_SRV" "$PID_PROXY" "$PID_HW0" "$PID_KV" "$PID_LT"; do rm -f "$pidf"; done
  info "stopped"
}

# --- main ---------------------------------------------------------------------
ACTION="${1:-start}"
case "$ACTION" in
  start)
    preflight || exit 1
    # After a stop, the old supervisor/server can take a few seconds to release
    # :9105/:8081; spawning the new supervisor first would die on EADDRINUSE.
    wait_port_free 9105 30 || true
    wait_port_free 8081 30 || true
    start_server
    start_proxy
    start_hw
    start_kv
    start_logtailer
    wait_ready || exit 1
    sleep 3
    status
    echo
    info "poll: curl -s http://127.0.0.1:8080/v1/models"
    info "metrics: curl -s http://192.168.1.64:8080/metrics"
    info "serving: curl -s http://127.0.0.1:9105/metrics | grep mlx_server"
    ;;
  stop) stop ;;
  restart) stop; sleep 2; preflight || exit 1; wait_port_free 9105 30 || true; wait_port_free 8081 30 || true; start_server; start_proxy; start_hw; start_kv; start_logtailer; wait_ready || exit 1; status ;;
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
