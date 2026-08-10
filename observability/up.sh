#!/bin/zsh
# Bring up the local observability stack (VictoriaMetrics + otel-collector +
# Grafana + vmalert + alertmanager) via podman compose.
#
# Run this on node A (192.168.1.64) - the observability host. The rank-0 stack
# on node B (192.168.1.5 / 192.168.2.2) is scraped over the LAN.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if ! podman info >/dev/null 2>&1; then
  echo "[obs] starting podman machine (first boot can take a minute)..."
  podman machine start
  for i in {1..45}; do
    podman info >/dev/null 2>&1 && break
    sleep 2
  done
fi
podman info >/dev/null 2>&1 || { echo "[obs] podman still unreachable after start"; exit 1; }

echo "[obs] regenerating vm-scrape.yml (rank0=192.168.1.5 rank1=192.168.1.64)"
./setup.sh >/dev/null

echo "[obs] podman compose up -d"
podman compose up -d

echo "[obs] waiting for VictoriaMetrics /health..."
for i in {1..30}; do
  curl -sf -m 2 http://127.0.0.1:8428/health >/dev/null 2>&1 && break
  sleep 2
done
curl -sf -m 2 http://127.0.0.1:8428/health >/dev/null 2>&1 \
  || echo "[obs] VM not healthy yet - check: podman compose logs victoria-metrics"

echo
echo "[obs] observability up:"
echo "  Grafana           http://192.168.1.64:3000 (admin/admin)"
echo "  VictoriaMetrics   http://192.168.1.64:8428/vmui"
echo "  vmalert           http://192.168.1.64:8880"
echo "  alertmanager      http://192.168.1.64:9093"
