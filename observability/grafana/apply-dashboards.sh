#!/usr/bin/env bash
# apply-dashboards.sh
# ===================
# Push Grafana dashboard JSONs from observability/grafana/dashboards/ to the
# k8s "monitoring" namespace as labeled ConfigMaps. The kiwigrid sidecar in the
# monitoring-grafana deployment watches for the label grafana_dashboard=1,
# drops the JSON into /tmp/dashboards and reloads Grafana provisioning, so the
# dashboard updates within ~30s of running this.
#
# Usage: observability/grafana/apply-dashboards.sh [dashboard.json ...]
#   (defaults to every *.json in observability/grafana/dashboards/)

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$DIR/dashboards"
NS="${GRAFANA_NAMESPACE:-monitoring}"

files=("$@")
if [[ ${#files[@]} -eq 0 ]]; then
  files=("$SRC"/*.json)
fi

for f in "${files[@]}"; do
  [[ -f "$f" ]] || { echo "skip: no such file $f"; continue; }
  name="$(basename "$f" .json)-dashboard"
  kubectl create configmap "$name" -n "$NS" \
    --from-file="$(basename "$f")=$f" \
    --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
  kubectl label cm "$name" -n "$NS" grafana_dashboard=1 --overwrite >/dev/null
  echo "applied $f -> configmap/$name (grafana_dashboard=1)"
done

echo "sidecar picks up the change within ~30s; verify:"
echo "  curl -u admin:admin http://127.0.0.1:3000/api/dashboards/uid/mlx-alerts"
