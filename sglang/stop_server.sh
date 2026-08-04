#!/bin/zsh
# Stop the two independent SGLang replicas (rank0 local + rank1 over ssh).
#
# Does NOT touch mlx_hw_telemetry.py, mlx_lm.server, or mlx.launch — those
# are cluster/'s concern (cluster/stop_server.sh) and are left running.
pkill -f "sglang.launch_server" 2>/dev/null
ssh -o ConnectTimeout=5 192.168.1.5 "pkill -f 'sglang.launch_server'" 2>/dev/null
echo "stopped"
