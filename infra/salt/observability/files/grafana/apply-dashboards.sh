#!/usr/bin/env bash
# apply-dashboards.sh
# ===================
# Push Grafana dashboard JSONs from observability/grafana/dashboards/ to the
# k8s "hydra-mlx" namespace as labeled ConfigMaps. The kiwigrid sidecar in the
# monitoring-grafana deployment watches for the label grafana_dashboard=1 across
# all namespaces (searchNamespace: ALL), drops the JSON into its dashboards dir
# and reloads Grafana provisioning, so the dashboard updates within ~30s.
#
# Datasource uid rewrite
# ----------------------
# The repo JSONs reference the datasource by uid `VictoriaMetrics` (and, in two
# files, the lowercase `victoriametrics`). Both are rewritten to `mlx-vm` on the
# way into the cluster, because in the k8s Grafana `VictoriaMetrics` is already
# taken by the in-cluster instance, which has never scraped the Macs - the
# panels would resolve cleanly and show nothing. The files on disk are left
# untouched so the compose Grafana keeps working unchanged.
#
# Grafana folder
# --------------
# The grafana_folder annotation only takes effect once the monitoring release
# sets sidecar.dashboards.folderAnnotation + provider.foldersFromFilesStructure.
# Until then the annotation is inert and dashboards land in the root folder.
# See infra/hydra-mlx/README.md.
#
# Usage: observability/grafana/apply-dashboards.sh [dashboard.json ...]
#   (defaults to every *.json in observability/grafana/dashboards/)
#
# Env overrides:
#   GRAFANA_NAMESPACE  target namespace          (default hydra-mlx)
#   GRAFANA_FOLDER     grafana_folder annotation (default "MLX")
#   MLX_DS_UID         datasource uid to write   (default mlx-vm)

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$DIR/../.." && pwd)"
SRC="$DIR/dashboards"
NS="${GRAFANA_NAMESPACE:-hydra-mlx}"
FOLDER="${GRAFANA_FOLDER:-MLX}"
DS_UID="${MLX_DS_UID:-mlx-vm}"

command -v kubectl >/dev/null || { echo "kubectl not found" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }

# Namespace + datasource first: a dashboard whose datasource does not exist yet
# renders as a broken panel until the sidecar re-syncs.
kubectl apply -f "$REPO/infra/hydra-mlx/namespace.yaml" >/dev/null
kubectl apply -f "$REPO/infra/hydra-mlx/datasource.yaml" >/dev/null
echo "namespace/$NS + datasource uid=$DS_UID ready"

files=("$@")
if [[ ${#files[@]} -eq 0 ]]; then
  files=("$SRC"/*.json)
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

for f in "${files[@]}"; do
  [[ -f "$f" ]] || { echo "skip: no such file $f"; continue; }
  base="$(basename "$f")"
  name="$(basename "$f" .json)-dashboard"

  # Rewrite every datasource uid variant to the single k8s uid.
  DS_UID="$DS_UID" python3 - "$f" "$tmp/$base" <<'PY'
import json, os, sys

src, dst = sys.argv[1], sys.argv[2]
uid = os.environ["DS_UID"]
doc = json.load(open(src))
n = 0


def walk(o):
    global n
    if isinstance(o, dict):
        ds = o.get("datasource")
        # Leave the built-in "-- Grafana --" datasource alone; it is not a
        # Prometheus reference and has no uid of ours to rewrite.
        if isinstance(ds, dict) and ds.get("type") == "prometheus":
            if ds.get("uid") != uid:
                ds["uid"] = uid
                n += 1
        for v in o.values():
            walk(v)
    elif isinstance(o, list):
        for v in o:
            walk(v)


walk(doc)
json.dump(doc, open(dst, "w"), indent=2)
print(f"  rewrote {n} datasource ref(s) -> {uid}")
PY

  kubectl create configmap "$name" -n "$NS" \
    --from-file="$base=$tmp/$base" \
    --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
  kubectl label cm "$name" -n "$NS" \
    grafana_dashboard=1 \
    app.kubernetes.io/part-of=hydra-mlx \
    app.kubernetes.io/component=dashboard --overwrite >/dev/null
  kubectl annotate cm "$name" -n "$NS" \
    grafana_folder="$FOLDER" --overwrite >/dev/null
  echo "applied $base -> $NS/configmap/$name (folder: $FOLDER)"
done

echo
echo "sidecar picks up the change within ~30s; verify:"
echo "  kubectl get cm -n $NS -l grafana_dashboard=1"
