#!/bin/zsh
# Start two INDEPENDENT SGLang replicas, one per Mac mini (rank0 + rank1).
#
# This is NOT the same shape as cluster/start_server.sh. MLX distributes one
# logical model across both nodes via `mlx.launch --backend ring` (NCCL-free
# ring collectives implemented in MLX itself). SGLang has no ring/tensor-
# parallel backend for Apple Silicon — its distributed paths assume NCCL and
# CUDA GPUs. So there is no way to run "one SGLang model across both Macs"
# here: each node runs its own full, independent copy of the model behind
# its own :30000 endpoint.
#
# Load balancing across the two independent replicas (round-robin nginx) is
# NOT scripted in this repo yet. vllm-metal/index.html section 5 ("Load
# balancing and routing") already documents an nginx config for exactly this
# two-node-independent-replica shape (vllm-metal runs the same way, on
# :8000) — reuse that pattern for sglang instead of duplicating it here.
#
# ############################################################################
# # UNVERIFIED: SGLang's primary backend is CUDA (FlashInfer/Triton kernels  #
# # on NVIDIA GPUs). There is no confirmed, mainstream Metal/MLX backend for #
# # SGLang analogous to vllm-metal's MLX fork. This script only wires up    #
# # the launch/log/health-check plumbing assuming an OpenAI-compatible      #
# # SGLang server ends up reachable at :30000 on each node — it does NOT    #
# # confirm SGLang runs correctly, or at all, on Apple Silicon GPUs. Read   #
# # SGLang's own docs and confirm device support before running this.      #
# ############################################################################
#
# Hardware telemetry is NOT started here. cluster/mlx_hw_telemetry.py is
# engine-agnostic (reads ioreg/powermetrics/pmset, nothing MLX-specific) and
# is already running on both nodes at :9102 via cluster/start_server.sh — it
# is reused as-is. SGLang also needs no metrics-proxy sidecar (unlike MLX's
# cluster/mlx_metrics_proxy.py): SGLang emits its own native Prometheus
# metrics on /metrics under the `sglang:*` namespace (e.g.
# sglang:time_to_first_token_seconds_bucket, sglang:num_running_reqs,
# sglang:num_waiting_reqs, sglang:token_usage, sglang:cache_hit_rate,
# sglang:gen_throughput).
#
# Health check path: /health   Metrics path: /metrics
# (per Hydra's llm_engine_catalog entry for sglang: port 30000, scheme http,
# health_path "/health", metrics_path "/metrics", auth none.)
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"

# HuggingFace model id sglang should load. There is no default — whatever
# model cluster/cluster.env points MLX at is an MLX-quantized artifact and is
# NOT loadable by sglang. Set this explicitly.
MODEL="${SGLANG_MODEL:-REPLACE_ME_HF_MODEL_ID}"
if [[ "$MODEL" == "REPLACE_ME_HF_MODEL_ID" ]]; then
  echo "WARNING: MODEL is unset — set SGLANG_MODEL to a real HuggingFace model id before this will do anything useful." >&2
fi

# Device backend for sglang.launch_server. Left EMPTY by default on purpose:
# the correct value (if any) for Apple Silicon is UNVERIFIED — confirm
# against SGLang's own docs/--help before setting this. Leaving it empty lets
# sglang auto-detect, which will very likely fall back to CPU and be far too
# slow for meaningful benchmarking.
SGLANG_DEVICE="${SGLANG_DEVICE:-}"

LOG="$DIR/logs"
mkdir -p "$LOG"

DEVICE_ARGS=()
if [[ -n "$SGLANG_DEVICE" ]]; then
  DEVICE_ARGS+=(--device "$SGLANG_DEVICE")
fi

# rank0 (local, this Mac mini).
nohup python3 -m sglang.launch_server \
  --model-path "$MODEL" \
  --host 0.0.0.0 --port 30000 \
  "${DEVICE_ARGS[@]}" \
  > "$LOG/sglang0.log" 2>&1 &
disown
echo "sglang rank0 launching (pid $!) -> $LOG/sglang0.log"

# rank1 (remote, over ssh), mirroring the ssh pattern cluster/start_server.sh
# uses for the remote hw telemetry agent.
nohup ssh -o ConnectTimeout=5 192.168.1.5 \
  "nohup python3 -m sglang.launch_server \
   --model-path '$MODEL' --host 0.0.0.0 --port 30000 ${SGLANG_DEVICE:+--device $SGLANG_DEVICE} \
   > '$LOG/sglang1.log' 2>&1 &" \
  > /dev/null 2>&1 &
disown
echo "sglang rank1 launching over ssh (pid $!) -> $LOG/sglang1.log (on rank1)"

echo "hw telemetry: already running on both nodes at :9102 via cluster/start_server.sh (not started here)"
echo "load balancing: not scripted here — see vllm-metal/index.html section 5 (nginx round-robin) for the pattern"
echo ""
echo "poll rank0: curl -s http://127.0.0.1:30000/health"
echo "poll rank1: curl -s http://192.168.1.5:30000/health"
echo "metrics rank0: curl -s http://127.0.0.1:30000/metrics | head"
echo "metrics rank1: curl -s http://192.168.1.5:30000/metrics | head"
