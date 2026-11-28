# Multi-Mode Deployment Guide

Every application component in MLX is build-ready for all three deployment modes from day 1.

| Mode | Entry Point / Manifest | Network Routing | Config Source |
|---|---|---|---|
| **docker-compose** | `deploy/docker-compose.yml` | Bridge (`mlx-net`), container `eth0` | `deploy/docker-compose.yml` + `.env` |
| **kubernetes** | `deploy/k8s/` | CNI (`cni0` / Pod IP via Downward API) | `deploy/k8s/configmap.yaml` |
| **standalone** | `deploy/standalone/` | Physical NIC (`en1`, `eth0`, `en2`) | `deploy/standalone/edge.env` & `cluster/mlx_monitoring_bundle/edge/config/nodes.yml` |

---

## 1. Standalone (Bare Linux / macOS / Mac Mini VM)

Bring up edge monitoring and exporters directly on the host using systemd or launchd:

```bash
# 1. Configure node identity & central TSDB IP in nodes.yml:
cp cluster/mlx_monitoring_bundle/edge/config/nodes.yml.template cluster/mlx_monitoring_bundle/edge/config/nodes.yml

# 2. Configure listening interfaces and upstream ports in edge.env:
cp deploy/standalone/edge.env cluster/mlx_monitoring_bundle/edge/config/edge.env

# 3. Start monitoring bundle:
cd cluster/mlx_monitoring_bundle/edge && python3 mlx_monitoring_bundle.py start
```

- **Network Interface**: Auto-detects default route interface (e.g. `en1` on Wi-Fi, `en0` on Ethernet, `eth0` on Linux VM). Can be overridden via `MLX_NET_IFACE`.
- **Ports**: Ingress proxy listens on `:8080`, forwards to engine API `:8030`. Exporters listen on `:9102` (HW), `:9104` (KV), `:9105` (Supervisor), `:9106` (Logs), `:9107` (Process), `:9108` (SBOM), `:9109` (Eval).

---

## 2. Docker Compose (Local / Dev / Small Prod)

Bring up the complete central stack and edge proxies with a single command:

```bash
docker compose -f deploy/docker-compose.yml up -d
```

- **Health Checks**:
  - VictoriaMetrics: `http://localhost:8428/health`
  - Grafana: `http://localhost:3000/api/health`
  - MLX Proxy: `http://localhost:8080/metrics`
  - MLX Engine: `http://localhost:8030/v1/models`
- **Config Customization**: Set `MLX_PROXY_PORT`, `MLX_ENGINE_PORT`, `VICTORIA_METRICS_URL` in `.env`.

---

## 3. Kubernetes (k8s)

Deploy onto any standard Kubernetes cluster using native manifests:

```bash
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/service.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/servicemonitor.yaml
```

- **Pod Network & Interfaces**: Pods obtain an internal IP on the CNI bridge (`cni0` / `flannel.1` / `cali*`). Pod IP and Host IP are injected via the Kubernetes Downward API (`POD_IP`, `HOST_IP`).
- **Service Discovery**: The Ingress Proxy routes traffic to `MLX_UPSTREAM=http://mlx-service:8030`.
- **Prometheus Scrapes**: Handled automatically via `ServiceMonitor` targeting ports `proxy-http` (`:8080`) and `hw-metrics` (`:9102`).
