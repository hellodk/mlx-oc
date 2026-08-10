#!/bin/zsh
# Stop the distributed MLX stack on this Mac and the remote node
# (mlx_lm.server + hw telemetry on both nodes + the local metrics proxy + KV agent).
# Run on rank 0 (the serving node, 192.168.2.2); the peer is rank 1 (192.168.2.1).
DIR="$(cd "$(dirname "$0")" && pwd)"
source "$DIR/cluster.env"
RANK1="${MLX_RANK1_IP:-192.168.2.1}"
pkill -f "mlx_metrics_proxy.py" 2>/dev/null
pkill -f "mlx_kv_cache_agent.py" 2>/dev/null
pkill -f "mlx_hw_telemetry.py" 2>/dev/null
pkill -f "mlx_server_supervisor.py" 2>/dev/null
pkill -f "mlx_server_log_tailer.py" 2>/dev/null
pkill -f "mlx_lm.server" 2>/dev/null
pkill -f "mlx.launch" 2>/dev/null
ssh -o ConnectTimeout=5 "$RANK1" "pkill -f 'mlx_hw_telemetry.py'; pkill -f 'mlx_lm.server'; pkill -f 'mlx.launch'" 2>/dev/null
echo "stopped"
