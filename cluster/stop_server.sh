#!/bin/zsh
# Stop the distributed MLX stack on this Mac and the remote node
# (mlx_lm.server + hw telemetry on both nodes + the local metrics proxy + KV agent).
pkill -f "mlx_metrics_proxy.py" 2>/dev/null
pkill -f "mlx_kv_cache_agent.py" 2>/dev/null
pkill -f "mlx_hw_telemetry.py" 2>/dev/null
pkill -f "mlx_lm.server" 2>/dev/null
pkill -f "mlx.launch" 2>/dev/null
ssh -o ConnectTimeout=5 192.168.1.5 "pkill -f 'mlx_hw_telemetry.py'; pkill -f 'mlx_lm.server'; pkill -f 'mlx.launch'" 2>/dev/null
echo "stopped"
