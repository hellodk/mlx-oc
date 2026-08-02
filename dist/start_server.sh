#!/bin/zsh
# Start the distributed MLX stack across both Mac minis (ring: 10.0.0.1 <-> 10.0.0.2).
#
#   mlx_lm.server   rank 0 only, bound to 127.0.0.1:8081  (internal, no external access)
#   mlx_metrics_proxy.py  0.0.0.0:8080 -> 127.0.0.1:8081  (public OpenAI-compatible API + /metrics)
#
# opencode and other clients talk to the proxy on :8080; the proxy records
# TTFT, token rate, temperature and hallucination-risk heuristics.
DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
# The distributed server MUST use the py3.12 venv (~/venvs/mlx): nodeB's macOS
# Local-Network privacy silently blocks the third-party py3.14 binary from
# reaching local addresses when spawned over SSH (EHOSTUNREACH, no TCC entry).
# py3.12 has identical mlx 0.32.0 / mlx-lm 0.31.3 and is not blocked.
MLX_VENV="$HOME/venvs/mlx"
MODEL="mlx-community/Qwen3-1.7B-4bit"

# Optional OpenTelemetry endpoint for the in-cluster otel-gateway.
#   HTTP:  http://192.168.1.10:30318   (or any k0s node IP, port 30318)
#   gRPC:  http://192.168.1.10:30317
# Leave empty to disable OTel (Prometheus /metrics is always on).
OTLP="${MLX_OTLP_ENDPOINT:-http://192.168.1.10:30318}"

nohup "$MLX_VENV/bin/mlx.launch" \
  --hostfile "$DIR/hosts.json" \
  --backend ring \
  --cwd "$DIR" \
  --python "$MLX_VENV/bin/python" \
  -- "$MLX_VENV/bin/python" -m mlx_lm.server \
  --model "$MODEL" \
  --host 127.0.0.1 --port 8081 \
  --chat-template-args '{"enable_thinking":false}' \
  > "$DIR/server.log" 2>&1 &
disown
echo "mlx_lm.server launching (pid $!) -> $DIR/server.log"

# Hardware telemetry agent on this node (rank 0, local). Runs from the py3.14
# venv (local py3.14 is NOT blocked by Local-Network privacy).
nohup "$VENV/bin/python" "$DIR/mlx_hw_telemetry.py" \
  --node-name rank0 \
  --listen 0.0.0.0:9102 \
  --otlp-endpoint "$OTLP" \
  > "$DIR/hw0.log" 2>&1 &
disown
echo "hw telemetry rank0 (pid $!) -> $DIR/hw0.log"

# Hardware telemetry agent on node B (rank 1). Uses the py3.12 venv because
# nodeB's macOS Local-Network privacy blocks the py3.14 binary from pushing
# OTLP to the in-cluster gateway.
nohup ssh -o ConnectTimeout=5 192.168.1.5 \
  "MLX_OTLP_ENDPOINT='$OTLP' nohup '$MLX_VENV/bin/python' '$DIR/mlx_hw_telemetry.py' \
   --node-name rank1 --listen 0.0.0.0:9102 --otlp-endpoint '$OTLP' \
   > '$DIR/hw1.log' 2>&1 &" \
  > /dev/null 2>&1 &
disown
echo "hw telemetry rank1 launching over ssh (pid $!)"

nohup "$VENV/bin/python" "$DIR/mlx_metrics_proxy.py" \
  --listen 0.0.0.0:8080 \
  --upstream 127.0.0.1:8081 \
  --default-temp 0.0 \
  --node-name rank0 \
  --otlp-endpoint "$OTLP" \
  > "$DIR/proxy.log" 2>&1 &
disown
echo "metrics proxy launching (pid $!) -> $DIR/proxy.log"

echo "poll: curl -s http://127.0.0.1:8080/v1/models"
echo "metrics: curl -s http://192.168.1.64:8080/metrics"
echo "hw rank0: curl -s http://127.0.0.1:9102/metrics | head"
echo "hw rank1: curl -s http://192.168.1.5:9102/metrics | head"
