# `hydra-mlx` namespace

Every Grafana object for the MLX / Hydra Apple-Silicon stack lives here: the 12
`mlx-*` dashboards, the 3 `hydra-*` dashboards, and the datasource they read.

The workloads themselves run on the Mac minis, not in k8s. This namespace exists
so the observability objects sit outside `monitoring`, which holds only the
Prometheus / Grafana / Alertmanager core install.

## Layout

| File | What |
|------|------|
| `namespace.yaml` | the namespace |
| `datasource.yaml` | `mlx-vm` datasource -> VictoriaMetrics holding the `mlx_*` series |

Dashboards are generated from `observability/grafana/dashboards/*.json` by
`observability/grafana/apply-dashboards.sh`, which is the single entry point:

```bash
observability/grafana/apply-dashboards.sh              # all 12
observability/grafana/apply-dashboards.sh path/to.json # one
```

The `hydra-*` dashboards are **not** sourced from this repo - they belong to the
Hydra project and were migrated here as-is. Whatever deploys them must be updated
to target `hydra-mlx`, otherwise the next Hydra deploy recreates them in
`monitoring` and Grafana ends up with two ConfigMaps claiming the same dashboard
uid.

## Datasource uid: `mlx-vm`

Dashboards on disk reference the datasource as `VictoriaMetrics` (and, in
`mlx-cluster` / `mlx-performance`, the lowercase `victoriametrics`). The apply
script rewrites every Prometheus datasource reference to `mlx-vm` on the way into
the cluster and leaves the files untouched, so the compose Grafana keeps working.

This matters because the k8s Grafana already has a datasource with uid
`VictoriaMetrics`, pointing at `vmsingle-monitoring.monitoring.svc:8429`. That
instance has never scraped the Macs - its vmagent scrapes exactly one target,
`otel-collector.monitoring.svc:8888`. Left alone, the mlx dashboards would
resolve cleanly against it and render empty panels.

`mlx-vm` is also the seam for the push-based cutover: when the `mlx_*` series
land in the in-cluster VictoriaMetrics, only the `url` in `datasource.yaml`
changes. No dashboard edits.

## Known gap: the `grafana_folder` annotation is currently inert

Both dashboard sets carry a `grafana_folder` annotation (`MLX` and
`Hydra — Apple Silicon LLM`). Neither produces a folder yet. Two independent
reasons, both in the live `monitoring` Grafana:

1. The sidecar has no `FOLDER_ANNOTATION` env var. The grafana chart renders it
   only `{{- with .Values.sidecar.dashboards.folderAnnotation }}`, and that value
   is unset, so kiwigrid/k8s-sidecar 2.8.1 falls back to its own default
   annotation key - not `grafana_folder`.
2. The dashboard provider is flat:

   ```yaml
   # configmap/monitoring-grafana-config-dashboards
   folder: ''
   foldersFromFilesStructure: false
   ```

   Even if the sidecar wrote into a subdirectory, the provider would flatten it.

### The fix

`monitoring` is **not** a Helm release - it is an ArgoCD Application
(`argocd/monitoring`) with `automated.selfHeal: true`, so editing the ConfigMap
in-cluster is reverted on the next sync. The change belongs in
[`hellodk/thecylon-helm-charts`](https://github.com/hellodk/thecylon-helm-charts),
`charts/victoria-metrics-k8s-stack/values-cylon-overlay.yaml`:

```yaml
  sidecar:
    dashboards:
      searchNamespace: ALL
      folderAnnotation: grafana_folder      # + these
      provider:
        foldersFromFilesStructure: true
    datasources:
      searchNamespace: ALL
```

Blast radius is small but not zero: dashboards with no annotation keep landing in
the root folder, but the provider change re-provisions every dashboard in that
Grafana, so expect a churn on the next sync.

## Verifying

The Grafana admin API returns 401 - the `grafana-admin` secret was rotated after
first boot and Grafana still holds the original password in its DB. `kubectl
exec` and `kubectl logs` also time out against this cluster (apiserver reachable,
kubelet path is not). Until one of those is fixed, folder placement can only be
confirmed in the Grafana UI by hand.
