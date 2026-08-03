#!/bin/zsh
# Start the distributed MLX stack across both Mac minis (ring: 10.0.0.1 <-> 10.0.0.2).
#
#   mlx_lm.server          rank 0 only, bound to 127.0.0.1:8081  (internal)
#   mlx_metrics_proxy.py   0.0.0.0:8080 -> 127.0.0.1:8081  (public OpenAI API + /metrics)
#   mlx_hw_telemetry.py    0.0.0.0:9102  on BOTH nodes (Prometheus + OTLP hardware metrics)
#
# opencode and other clients talk to the proxy on :8080; the proxy records
# TTFT, token rate, temperature and hallucination-risk heuristics, and can
# export OpenTelemetry spans/metrics/logs.
#
# Repo layout:  .venv/ at repo root (py3.14, rank0 proxy), cluster/ holds the
# runtime scripts, tools/ holds experiments + bench.py.
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
VENV="$REPO/.venv"
# The distributed server MUST use the py3.12 venv (~/venvs/mlx): nodeB's macOS
# Local-Network privacy silently blocks the third-party py3.14 binary from
# reaching local addresses when spawned over SSH (EHOSTUNREACH, no TCC entry).
# py3.12 has identical mlx 0.32.0 / mlx-lm 0.31.3 and is not blocked.
MLX_VENV="$HOME/venvs/mlx"
MODEL="mlx-community/Qwen3-1.7B-4bit"
LOG="$DIR/logs"
mkdir -p "$LOG"

# Optional OpenTelemetry endpoint.
#   local podman VM stack:   http://192.168.1.64:4318   (observability/compose.yaml) [default]
#   in-cluster otel-gateway: http://192.168.1.10:30318  (gRPC :30317)
# Leave empty to disable OTel (Prometheus /metrics is always on).
OTLP="${MLX_OTLP_ENDPOINT:-http://192.168.1.64:4318}"

# Optional Opik tracing (both paths; set empty to disable either).
#   --opik-endpoint      opik SDK base URL (frontend NodePort; proxy appends /api)
#   --opik-otlp-endpoint OTLP HTTP ingestion of the same instance
OPIK_ENDPOINT="${OPIK_ENDPOINT:-http://192.168.1.10:32173}"
OPIK_OTLP="${OPIK_OTLP_ENDPOINT:-http://192.168.1.10:32173/api/v1/private/otel}"

nohup "$MLX_VENV/bin/mlx.launch" \
  --hostfile "$DIR/hosts.json" \
  --backend ring \
  --cwd "$DIR" \
  --python "$MLX_VENV/bin/python" \
  -- "$MLX_VENV/bin/python" -m mlx_lm.server \
  --model "$MODEL" \
  --host 127.0.0.1 --port 8081 \
  --chat-template-args '{"enable_thinking":false}' \
  > "$LOG/server.log" 2>&1 &
disown
echo "mlx_lm.server launching (pid $!) -> $LOG/server.log"

# Hardware telemetry agent on this node (rank 0, local). Runs from the py3.14
# venv (local py3.14 is NOT blocked by Local-Network privacy).
nohup "$VENV/bin/python" "$DIR/mlx_hw_telemetry.py" \
  --node-name rank0 \
  --listen 0.0.0.0:9102 \
  --otlp-endpoint "$OTLP" \
  > "$LOG/hw0.log" 2>&1 &
disown
echo "hw telemetry rank0 (pid $!) -> $LOG/hw0.log"

# Hardware telemetry agent on node B (rank 1). Uses the py3.12 venv because
# nodeB's macOS Local-Network privacy blocks the py3.14 binary from pushing
# OTLP to the in-cluster gateway.
nohup ssh -o ConnectTimeout=5 192.168.1.5 \
  "MLX_OTLP_ENDPOINT='$OTLP' nohup '$MLX_VENV/bin/python' '$DIR/mlx_hw_telemetry.py' \
   --node-name rank1 --listen 0.0.0.0:9102 --otlp-endpoint '$OTLP' \
   > '$LOG/hw1.log' 2>&1 &" \
  > /dev/null 2>&1 &
disown
echo "hw telemetry rank1 launching over ssh (pid $!)"

PROXY_OPIK_ARGS=()
if [[ -n "$OPIK_ENDPOINT" ]]; then PROXY_OPIK_ARGS+=(--opik-endpoint "$OPIK_ENDPOINT"); fi
if [[ -n "$OPIK_OTLP" ]]; then PROXY_OPIK_ARGS+=(--opik-otlp-endpoint "$OPIK_OTLP"); fi

nohup "$VENV/bin/python" "$DIR/mlx_metrics_proxy.py" \
  --listen 0.0.0.0:8080 \
  --upstream 127.0.0.1:8081 \
  --default-temp 0.0 \
  --node-name rank0 \
  --otlp-endpoint "$OTLP" \
  "${PROXY_OPIK_ARGS[@]}" \
  > "$LOG/proxy.log" 2>&1 &
disown
echo "metrics proxy launching (pid $!) -> $LOG/proxy.log"

echo "poll: curl -s http://127.0.0.1:8080/v1/models"
echo "metrics: curl -s http://192.168.1.64:8080/metrics"
echo "hw rank0: curl -s http://127.0.0.1:9102/metrics | head"
echo "hw rank1: curl -s http://192.168.1.5:9102/metrics | head"
