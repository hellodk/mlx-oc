#!/bin/zsh
# Tear down the local observability stack (podman compose down, volumes kept).
# Run on node A (192.168.1.64) - the observability host.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

podman info >/dev/null 2>&1 || { echo "[obs] podman not running; nothing to do"; exit 0; }
podman compose down
echo "[obs] observability down"
