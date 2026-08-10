#!/bin/zsh
# Stop the distributed MLX stack on the serving node and every ring peer
# (mlx_lm.server + hw telemetry on all nodes + the local metrics proxy + KV agent).
# Run on rank 0 (the serving node); the peer list comes from cluster/hosts.json.
DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$DIR")"
source "$DIR/cluster.env"

MLX_VENV="$HOME/venvs/mlx"
hostfile="$REPO/$MLX_HOSTFILE"
[[ -f "$hostfile" ]] || { echo "ERROR: hostfile $hostfile not found (MLX_HOSTFILE=$MLX_HOSTFILE)" >&2; exit 1; }

parsed="$("$MLX_VENV/bin/python" - "$hostfile" 2>/dev/null <<'PY'
import json, sys
for h in json.load(open(sys.argv[1]))["hosts"]:
    ssh = h.get("ssh") or (h.get("ips") or [""])[0]
    print(ssh)
PY
)"
[[ -n "$parsed" ]] || { echo "ERROR: could not parse $hostfile" >&2; exit 1; }
PEERS=("${(f)parsed[@]:1}")

pkill -f "mlx_metrics_proxy.py" 2>/dev/null
pkill -f "mlx_kv_cache_agent.py" 2>/dev/null
pkill -f "mlx_hw_telemetry.py" 2>/dev/null
pkill -f "mlx_server_supervisor.py" 2>/dev/null
pkill -f "mlx_server_log_tailer.py" 2>/dev/null
pkill -f "mlx_lm.server" 2>/dev/null
pkill -f "mlx_server_launcher" 2>/dev/null
pkill -f "mlx.launch" 2>/dev/null

for peer in "${PEERS[@]}"; do
  ssh -o ConnectTimeout=5 "$peer" "pkill -f 'mlx_hw_telemetry[.]py'; pkill -f 'mlx_lm[.]server'; pkill -f 'mlx_server_launcher[.]py'; pkill -f 'mlx[.]launch'" 2>/dev/null
done
echo "stopped"
