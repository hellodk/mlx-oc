# Dynamic Network Topology — Runtime Adapters & Extension Points

**Author**: Principal SRE & Observability Architect  
**Component**: MLX Distributed Monitoring Fleet  
**Status**: Implemented Core Architecture & Extension Specification

---

## 1. Overview

The MLX Dynamic Network Topology architecture decouples **runtime observation** from **reconciliation** using a normalized `TopologyNode` and `TopologyEdge` schema.

While the primary telemetry pipeline (`Exporters -> vmagent -> VictoriaMetrics -> Grafana`) is fully operational and continuously audited, this specification documents the pluggable adapters that extend the topology graph across OpenTelemetry logging/tracing, container runtimes (Podman & Docker), and Kubernetes clusters.

---

## 2. OpenTelemetry Logs & Traces Discovery Adapter

### 2.1 Decoupled Pipeline Architecture
In the MLX architecture, telemetry destinations are strictly segregated:
- **Metrics**: Edge Exporters -> `vmagent:8429` -> `remote_write` -> VictoriaMetrics TSDB (`:8428`).
- **LLM Evaluations & Spans**: `mlx_metrics_proxy` -> Direct Opik Python SDK / REST -> Opik Platform (`:32173` / `:8000`).
- **Inference Logs & Raw OTLP**: `mlx_metrics_proxy` & `mlx-engine` -> OTLP HTTP (`:4318`) -> OpenTelemetry Collector -> Durable JSONL File (`/var/log/otelcol/mlx-server-logs.jsonl`) & Debug Stdout.

### 2.2 OTel Collector Runtime Inspector
The OTel adapter (`OTelCollectorInspector`) audits:
1. **Collector Internal Metrics (`http://127.0.0.1:8888/metrics` or `:4318`)**:
   - `otelcol_receiver_accepted_log_records_total`: Ingested log count from proxy/engine.
   - `otelcol_exporter_sent_log_records_total`: Successfully written records.
   - `otelcol_exporter_enqueue_failed_log_records_total`: Backpressure or batch queue drops.
   - `otelcol_receiver_refused_log_records_total`: Rejected requests (e.g. rate limit, bad payload).
2. **File Exporter Target Validation**:
   - Inspects target path: `/var/log/otelcol/mlx-server-logs.jsonl`.
   - Audits write permissions, file size growth, and rotation limits (`max_megabytes: 50`, `max_backups: 5`).
   - Flags `MISCONFIGURED: LOG DESTINATION UNWRITABLE` if disk is full or read-only.
3. **Decoupled Opik State**:
   - Verifies that OTel Collector is **not** attempting to forward traces to Opik (which creates duplicate spans with corrupted attributes).
   - Validates that `_OPIK = Opik(...)` is communicating directly with `OPIK_ENDPOINT`.

---

## 3. Container Runtime Adapters: Podman & Docker

### 3.1 Bridge Network & Socket Mapping
When running under Podman or Docker Compose (`deploy/docker-compose.yml`), component discovery must resolve container-internal IPs (`172.28.0.0/16`) to host ports.

### 3.2 Adapter Implementation
```python
class ContainerTopologyAdapter:
    """Discovers containerized MLX services via Docker/Podman socket or CLI."""

    def __init__(self, socket_path: str = "/var/run/docker.sock"):
        self.socket_path = socket_path

    def inspect_containers(self) -> List[TopologyNode]:
        nodes = []
        # Query container engine API (/containers/json)
        # 1. Match container names: 'mlx-proxy', 'mlx-engine', 'vmagent', 'victoria-metrics'
        # 2. Extract network settings: IPAddress, Gateway, PortBindings (HostPort -> ContainerPort)
        # 3. Inspect health state: State.Health.Status ('healthy' | 'unhealthy')
        # 4. Map to normalized TopologyNode with deployment_type='podman' | 'docker'
        return nodes
```

### 3.3 Network Verification
- Verifies that `mlx-proxy` can resolve `mlx-engine:8030` via internal Docker DNS (`127.0.0.11`).
- Verifies that `vmagent` can reach each exporter container over bridge `mlx-net`.
- Flags `MISCONFIGURED: NETWORK_ISOLATION_FAILURE` if containers reside on disjoint bridge networks.

---

## 4. Kubernetes Discovery Adapter

### 4.1 Topology Extraction via EndpointSlices & Pods
In Kubernetes deployments (`deploy/k8s/`), services are represented as `Service` and `EndpointSlice` resources.

```mermaid
flowchart LR
    K8sAPI["Kubernetes API Server\n(/api/v1, /discovery.k8s.io/v1)"]
    K8sAdapter["K8sTopologyAdapter\n(In-Cluster Informer)"]
    K8sAPI -->|Watch EndpointSlices & Pods| K8sAdapter
    K8sAdapter -->|Generate Snapshots| TopoService["Central Topology Service\n(:8089)"]
```

### 4.2 Adapter Specification
1. **EndpointSlice Watcher**:
   - Watches `EndpointSlice` resources with label `kubernetes.io/service-name=mlx-proxy` and `mlx-engine`.
   - Inspects `endpoints[].conditions.ready`:
     - If `ready == true`: Marks node and forwarding edge `HEALTHY`.
     - If `ready == false`: Marks edge `DEGRADED` (Pod failing readiness probe).
     - If `endpoints: []`: Marks service `CRITICAL: NO_READY_ENDPOINTS`.
2. **ServiceMonitor Reconciler**:
   - Validates that Prometheus Operator `ServiceMonitor` resources exist in the same namespace as `mlx-proxy` and exporters.
   - Detects label mismatch: flags `MISCONFIGURED: SERVICEMONITOR_SELECTOR_MISMATCH` if `matchLabels` does not match Pod labels.

---

## 5. Central Registry Deployment & Scaling Path

### 5.1 Local & Single-Node Central Deployments
- Runs as `TopologyAPIService` on port `8089`.
- Storage: SQLite in Write-Ahead Logging (WAL) mode (`topology.db`).
- Benchmark verified: 100 concurrent edge devices report snapshots in < 3 seconds with 0 lock errors and average latency < 25ms.

### 5.2 1,000+ Device Scaling
- For fleets scaling beyond 1,000 devices:
  1. **PostgreSQL Storage Backend**: Swap `SQLiteTopologyStorage` with `PostgreSQLTopologyStorage` by implementing the `TopologyStorage` abstract interface.
  2. **Snapshot Compression**: Edge discovery agents compress snapshot payloads with `gzip` or `zstandard`.
  3. **Incremental Delta Reporting**: Edge nodes send full snapshots only on configuration change or startup; periodic heartbeats send only diffs and discrepancy state.
