# Suggestions — mlx-oc vs Hydra, and the path to a 3-engine benchmark

Context: mlx-oc will be used for real benchmarking of Apple MLX, vllm-metal, and
SGLang across the two Mac Minis (`192.168.1.64`/`10.0.0.1` rank0,
`192.168.1.5`/`10.0.0.2` rank1). `~/Documents/git/hydra` is a much larger,
mostly design-stage 28–40 node fleet platform that will be used as inspiration.
This doc captures what's worth reusing, what's broken, and what's missing —
found by reading both repos directly, not from memory.

---

## 1. Hydra already ran vLLM-MLX on one of these exact two nodes — and it's broken

`hosts.json` / `hosts_rev.json` put rank1 at `192.168.1.5`. Hydra's
`mlx_engine_comparison.html` benchmarked `vllm-mlx` at **that same IP** and
every request failed:

- `finish_reason: "error"`, 0 prompt tokens, 0 completion tokens — across chat,
  completions, array-content and system-message formats.
- `/health` reports `healthy` but also `model_type: "mllm"` (multimodal) with
  a batched engine — a text-only Qwen model is being routed through a vision
  code path that throws before tokenizing.

Hydra's diagnosed fix checklist (`mlx_engine_comparison.html`):

1. Tail the vllm-mlx process logs during one request — the real traceback is
   swallowed by the HTTP response.
2. Fix the `model_type` misdetection — force/confirm a text-LLM model type in
   server config.
3. Verify weights/tokenizer load — `prompt_tokens: 0` means input isn't being
   tokenized at all.
4. Restart, then re-run: `python3 mlx_loadtest.py --base http://192.168.1.5:8080 --label vllm-mlx --levels 1,2,4,8,16,32`

**Action**: apply this checklist before/instead of debugging the vllm-metal
deployment from scratch — it's the same bug on the same hardware.

## 2. Hydra already has concurrency-sweep numbers on this hardware

`mlx_loadtest.py` + `mlx23_results.json` + `mlx_engine_comparison.html`
compare **stock `mlx_lm.server`** vs a **batched MLX build** on Qwen3.5-4B at
concurrency 1/2/4/8/16:

| Concurrency | Stock mlx (.19) tok/s | Batched mlx (.23) tok/s | Speedup |
|---|---|---|---|
| 1 | 8.3 | 21.0 | 2.5x |
| 2 | 8.9 | 37.9 | 4.3x |
| 4 | 9.7 | 46.9 | 4.8x |
| 8 | 8.5 | 39.2 | 4.6x |
| 16 | 7.0 | 59.4 | 8.5x |

Stock mlx is flat (serializes, no batching — extra users only add latency,
5 min at 16 concurrent). Batched mlx scales ~5–6x with far lower p50.

**Action**: mlx-oc's `tools/bench.py` has no concurrency sweep — one
concurrency value per run, no persisted results. Check whether the
already-installed `mlx 0.32.0` / `mlx-lm 0.31.3` (newer than Hydra's tested
`.19`/`.23`) ships continuous batching that isn't being exercised in the
current benchmark — that's a bigger lever than the ring-overhead numbers
already documented in `README.md`. Port `mlx_loadtest.py`'s async
concurrency-sweep methodology (and `ingest_results.py`'s
result-persistence pattern) into `tools/`.

## 3. Hydra's engine-agnostic proxy/catalog pattern is what a 3-way comparison needs

mlx-oc's `cluster/mlx_metrics_proxy.py` is MLX-specific: `mlx_model_info.py`
parses MLX's `config.json`, KV-cache math comes from `mlx_lm.server`'s
`Prompt Cache:` log line. That's fine for one engine; it doesn't generalize to
three.

Hydra already solved this:

- `ansible/roles/llm-discovery` + `llm_engine_catalog`
  (`ansible/group_vars/all/main.yml`) — a per-engine table of
  port/scheme/health_path/metrics_path/auth (`vllm`, `vllm-mlx`, `sglang`,
  `mlx`, `mlx_perf`, `exo`, `ollama`, `llamacpp`, `litellm`). It distinguishes
  engines with **native** Prometheus metrics (vLLM, SGLang — scrape directly)
  from engines that need a **sidecar** (pure `mlx_lm`, exo — no metrics at
  all).
- `ansible/roles/llm-common/files/llm_perf_proxy.py` — the same
  "wrap an OpenAI-compatible engine that has no metrics" idea as mlx-oc's
  proxy, but engine-agnostic, with metric names deliberately mirroring vLLM's
  colon-namespaced convention (`mlx:time_to_first_token_seconds`,
  `mlx:generation_tokens_total`, `mlx:num_requests_running`,
  `mlx:gpu_cache_usage_perc` — best-effort estimate) so they fold into the same
  normalization rules as `vllm:*`/`sglang:*` without translation.
- `monitoring/prometheus/rules/hydra-llm-normalization.yml` — folds
  `vllm:*`/`sglang:*`/`mlx:*`/`exo:*` into one `hydra:llm:*` namespace
  (`ttft_seconds:p50/p95/p99`, `tokens_generated:rate1m`, `requests_running`,
  `kv_cache_usage_ratio`, `errors:rate5m`, etc.) via `or`-chained recording
  rules, keyed by `provider`/`model`/`node_id` labels.
- `apple-silicon-monitoring/alerts/{llm-inference,sglang-inference}.yaml` +
  `monitoring/grafana/dashboards/sglang-inference.json` — alerts and a
  dashboard already built against the normalized layer, with a "native engine"
  panel section plus a "Normalized — hydra:llm:\*" section that overlays all
  engines by `provider`.

**Action** (concrete steps, in order):

1. **Add a `provider` label to every scrape job** in
   `observability/vm-scrape.tmpl.yml`. Today jobs are differentiated only by
   `job` name; the `or`-merge pattern below needs a consistent label to key
   on.
   ```yaml
   - job_name: "mlx-proxy"
     static_configs:
       - targets: ["192.168.1.64:8080"]
         labels: {provider: "mlx", node_id: "rank0"}
   - job_name: "vllm-metal"
     static_configs:
       - targets: ["192.168.1.64:8000", "192.168.1.5:8000"]
         labels: {provider: "vllm"}
   - job_name: "sglang"
     static_configs:
       - targets: ["192.168.1.64:30000", "192.168.1.5:30000"]
         labels: {provider: "sglang"}
   ```
   vLLM and SGLang both emit native Prometheus metrics — no proxy needed for
   either. Only MLX needs `mlx_metrics_proxy.py` in front of it.

2. **Add a normalization rule group** to `observability/vmalert/rules.yml`
   (vmalert speaks the same recording-rule syntax as Prometheus):
   ```yaml
   - name: mlxoc-llm-normalization
     interval: 30s
     rules:
       - record: mlxoc:llm:up
         expr: up{job=~"mlx-proxy|vllm-metal|sglang"}

       - record: mlxoc:llm:ttft_seconds:p95
         expr: |
           histogram_quantile(0.95, sum by (le, provider) (rate(mlx_ttft_seconds_bucket[5m])))
           or
           histogram_quantile(0.95, sum by (le, provider) (rate(vllm:time_to_first_token_seconds_bucket[5m])))
           or
           histogram_quantile(0.95, sum by (le, provider) (rate(sglang:time_to_first_token_seconds_bucket[5m])))

       - record: mlxoc:llm:tokens_generated:rate1m
         expr: |
           sum by (provider) (rate(mlx_tokens_completion_total[1m]))
           or
           sum by (provider) (rate(vllm:generation_tokens_total[1m]))
           or
           sum by (provider) (rate(sglang:num_generated_tokens_total[1m]))

       - record: mlxoc:llm:requests_running
         expr: mlx_in_flight or vllm:num_requests_running or sglang:num_running_reqs

       - record: mlxoc:llm:kv_cache_usage_ratio
         expr: mlx_kv_cache_utilization or vllm:gpu_cache_usage_perc or sglang:token_usage

       - record: mlxoc:llm:errors:rate5m
         expr: |
           rate(mlx_error_total[5m])
           or
           sum by (provider) (rate(vllm:request_success_total{finished_reason="abort"}[5m]))
           # sglang omitted — Hydra flagged the same gap: no standard abort counter
   ```
   **Verify before trusting these** (same caveats Hydra left as `⚠` comments
   in `hydra-llm-normalization.yml`):
   - `vllm:gpu_cache_usage_perc` — confirm 0–1 not 0–100 on your vLLM version:
     `curl -s http://<node>:8000/metrics | grep gpu_cache_usage_perc`
   - `mlx_ttft_seconds` histogram bucket suffix exists:
     `curl -s http://127.0.0.1:8080/metrics | grep mlx_ttft_seconds_bucket`

3. **One comparison dashboard, not three.** Mirror
   `monitoring/grafana/dashboards/sglang-inference.json`'s layout: a native
   per-engine panel row (for debugging one engine specifically) plus a
   "Normalized — mlxoc:llm:\*" row with a `$provider` template variable so
   MLX/vllm-metal/SGLang overlay on the same panel instead of three separate
   Grafana tabs.

4. **Alerts**: adapt
   `apple-silicon-monitoring/alerts/{llm-inference,sglang-inference}.yaml`
   directly — they're already parameterized by `provider`/`node_id`/`model`.
   Patterns worth copying: `EngineDown` (`up == 0` for 5m),
   `InferenceStalled` (`tokens_generated:rate1m == 0` AND `up == 1` for 2m),
   `TTFTHigh`/`TTFTCritical` (p95 > 5s / > 30s), `KVCacheSaturation` (> 0.9 for
   2m), `QueueBacklog` (`requests_waiting > 100`), and the meta-alert
   `absent(mlxoc:llm:up{provider=~"vllm|sglang|mlx"})` for 10m — catches a
   broken scrape pipeline going silently dark (exactly what would happen if
   the vllm-mlx bug in §1 resurfaces and `:8000/metrics` stops updating).

## 4. Naming collision to fix

`vllm-metal/index.html` labels the two Mac Minis `hydra-svc-01`/`hydra-svc-02`.
In Hydra's actual architecture (`ARCHITECTURE.md`), `hydra-svc-01` is a
specific, different role — the single "Services node" (Fleet API + Postgres +
Redis + Grafana) on the `192.168.10.0/24` fleet subnet. mlx-oc's nodes are on
`192.168.1.0/24` and were never part of that fleet. Rename them in that doc
(e.g. `mlx-oc-01`/`mlx-oc-02`) so it doesn't read as "these are provisioned
Hydra fleet nodes."

## 5. Where mlx-oc stands vs. Hydra

For **real 2-node benchmarking of MLX vs vllm-metal vs SGLang**, mlx-oc is the
better base — it's real and running; Hydra's equivalent (40-node fleet,
LiteLLM gateway, RDMA tensor-parallel pools) is mostly spec with many
"Pending" rows in `benchmarkings.md`. Import from Hydra:

| Gap in mlx-oc | Hydra's answer | Priority |
|---|---|---|
| No concurrency sweep, no persisted results | `mlx_loadtest.py` (async sweep) + `ingest_results.py` → `benchmarkings.md` pattern | High |
| Proxy is MLX-only | `llm_perf_proxy.py` (engine-agnostic sidecar) + `llm_engine_catalog` | High — needed once vllm-metal/sglang are live |
| No cross-engine dashboard | `hydra-llm-normalization.yml` pattern | Medium — do once ≥2 engines are live |
| vLLM-MLX untested here | Already broken + diagnosed at `192.168.1.5` | Fix before writing new deployment steps |
| SGLang device support on Apple Silicon | **Unverified** — see `sglang/` scaffold notes | Confirm before relying on it for benchmarking |
| ~~No logprob-derived quality signals~~ | ~~`detection.py` entropy/perplexity/confidence~~ | **Closed** — see below |

### Closed: token-confidence layer

Hydra's `llm_telemetry/detection.py` computes entropy, perplexity and
confidence stats from token probabilities the *calling application* hands it.
mlx-oc now sources them in the proxy instead, because `mlx_lm.server` returns
`choices[].logprobs.content[]` on **non-streaming responses only** — verified
by probe, not assumed — and every real client here streams.

`cluster/mlx_metrics_proxy.py` therefore injects `logprobs`/`top_logprobs`
into non-streaming upstream requests and strips the field back out of the
reply, and de-streams a sampled fraction of streaming requests
(`MLX_LOGPROBS_STREAM_SAMPLE`, default 0.05) so real traffic gets scored at no
extra GPU cost. Eight metrics (`mlx_output_perplexity`,
`mlx_token_entropy_nats`, `mlx_token_confidence_{mean,min,std}`,
`mlx_token_margin_mean`, `mlx_low_confidence_token_ratio`,
`mlx_confidence_scored_total{source}`), five alerts in the `mlx-confidence`
group, and a Grafana row on MLX Performance that leads with scoring coverage.

Two deliberate divergences from Hydra:

* **Entropy is computed over the renormalised top-k slice** and documented as a
  lower bound on true entropy. Hydra's `compute_entropy` sums `-p·log p` over
  the chosen-token probabilities, which is not a distribution and grows with
  completion length.
* **Confidence is not folded into `mlx_hallucination_risk`.** Only a sampled
  subset of requests carry logprobs; blending would make the composite score
  mean different things for different requests. Hydra's `_risk()` blends them
  because its callers always supply probabilities.

## 6. Open question flagged during SGLang scaffolding

SGLang's primary backend is CUDA (NVIDIA GPUs via FlashInfer/Triton). There is
no confirmed, mainstream Metal/MLX backend for SGLang analogous to vLLM's MLX
fork (`vllm-metal`). Before treating `sglang/` as a real benchmark target,
confirm what device backend actually runs on Apple Silicon (CPU fallback would
be too slow to be a meaningful comparison point). Hydra's own SGLang
dashboards/alerts (`sglang-inference.json`, `sglang-inference.yaml`) were built
ahead of any live SGLang node too — this isn't a solved problem on either
side of the fence.
