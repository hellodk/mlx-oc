# mlx-oc

Distributed MLX LLM inference on a two-Mac-mini cluster, fronted by a
Prometheus/OpenTelemetry-observable proxy.

```
opencode / curl ──► mlx_metrics_proxy.py :8080 ──► mlx_lm.server :8081 (ring: rank0 + rank1)
                        │  /metrics (Prometheus text)
                        │  OTLP traces+metrics+logs ──► otel-gateway :30318
mlx_hw_telemetry.py :9102  per-node CPU / RAM / disk / temp
```

## Stack

| Piece | Where | Port | What it does |
|---|---|---|---|
| `mlx_lm.server` | both nodes (rank 0 + rank 1) | 127.0.0.1:8081 | distributed inference via the `ring` backend (`mlx.launch --backend ring`) |
| `dist/mlx_metrics_proxy.py` | rank 0 | 0.0.0.0:8080 | OpenAI-compatible front door; records TTFT, token rate, temperature, tool calls, hallucination-risk heuristics |
| `dist/mlx_hw_telemetry.py` | both nodes | 0.0.0.0:9102 | hardware telemetry (load, memory pressure, disk, worker CPU/RSS, best-effort CPU temp) |
| `dist/start_server.sh` / `dist/stop_server.sh` | rank 0 | – | launch / tear down the whole stack |

Model: `mlx-community/Qwen3-1.7B-4bit` (change `MODEL` in `start_server.sh`).

## Quick start

```bash
cd dist
./start_server.sh          # launches server (both nodes), proxy, hw agents
curl -s http://127.0.0.1:8080/v1/models
curl -s http://127.0.0.1:8080/metrics | head
curl -s http://127.0.0.1:9102/metrics | grep mlx_hw_load1
./stop_server.sh           # tears everything down
```

Any OpenAI-compatible client works against `http://<rank0-ip>:8080/v1` —
e.g. `opencode` pointed at `http://192.168.1.64:8080/v1/chat/completions`.

## Metrics (Prometheus)

Proxy `/metrics` (`--metrics-path`):

* `mlx_requests_total{model,streaming}`
* `mlx_ttft_seconds` (histogram), `mlx_gen_seconds`, `mlx_token_rate_tokens_per_second`
* `mlx_tokens_prompt_total`, `mlx_tokens_completion_total`
* `mlx_temperature`, `mlx_tool_calls_total`
* `mlx_hallucination_risk{model}` gauge + `mlx_hallucination_risk_histogram_bucket`
* `mlx_upstream_up`

Hardware agent `/metrics` (each node, `--node-name`):

* `mlx_hw_up`, `mlx_hw_uptime_seconds`, `mlx_hw_load{1,5,15}`, `mlx_hw_cpu_count`
* `mlx_hw_cpu_temp_celsius` (best effort; `NaN` when powermetrics/sudo unavailable)
* `mlx_hw_mem_total_bytes`, `mlx_hw_mem_used_bytes`, `mlx_hw_mem_pressure`
* `mlx_hw_disk_total_bytes`, `mlx_hw_disk_used_bytes`
* `mlx_worker_cpu_percent`, `mlx_worker_rss_bytes` (matches `mlx_lm.server`)

## OpenTelemetry

Pass `--otlp-endpoint` to the proxy and/or hw agent to enable OTLP export
(HTTP). `MLX_OTLP_ENDPOINT` is read as the default, and `start_server.sh`
uses `http://192.168.1.10:30318` (the in-cluster `otel-gateway` NodePort for
OTLP HTTP; `:30317` is gRPC). Traces land in Tempo, metrics in Prometheus
(via the collector's `:8889` prometheus exporter), logs in Loki/Tempo.

```bash
dist/mlx_metrics_proxy.py \
  --listen 0.0.0.0:8080 --upstream 127.0.0.1:8081 \
  --node-name rank0 --otlp-endpoint http://192.168.1.10:30318

dist/mlx_hw_telemetry.py \
  --node-name rank1 --listen 0.0.0.0:9102 \
  --otlp-endpoint http://192.168.1.10:30318
```

Each request becomes a `mlx.chat.completions` span carrying
`mlx.ttft_seconds`, `mlx.gen_seconds`, token counts, tool calls,
hallucination risk and HTTP status.

## Known platform quirks

* **macOS Local-Network privacy (TCC)**: on node B the third-party Homebrew
  `python3.14` binary is silently blocked from reaching local subnets over
  SSH (instant `EHOSTUNREACH`). The distributed server and node-B telemetry
  therefore run on the py3.12 venv `~/venvs/mlx`; the local (rank 0) proxy
  runs on the py3.14 venv `dist/.venv`. Both venvs carry identical
  `mlx 0.32.0` / `mlx-lm 0.31.3`.
* **CPU temperature** requires `powermetrics` via passwordless sudo; the
  sensor name differs per Apple Silicon generation, so this is best-effort
  and falls back to `NaN`.
* **Ring hostfile**: `dist/hosts.json` (rank 0: `10.0.0.1`) and the auto-
  generated hostfile used on the remote (`dist/hosts_rev.json`, rank 1:
  `10.0.0.2`). Update both if the cluster IPs change.

## Layout

```
dist/mlx_metrics_proxy.py    metrics + OTel proxy in front of mlx_lm.server
dist/mlx_hw_telemetry.py     per-node hardware telemetry exporter
dist/start_server.sh         launch ring server, proxy, hw agents (both nodes)
dist/stop_server.sh          stop the above
dist/hosts.json              ring hostfile (rank 0 side)
dist/hosts_rev.json          ring hostfile (rank 1 side)
```
