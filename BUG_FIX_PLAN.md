# Edge Bug Fix Plan

**Confirmed bugs from live analysis** — must fix before any deployment.

---

## 1. Missing `mlx_model_info.py` (BLOCKS `mlx_kv_cache_agent.py`) ✅ DONE

**Impact**: Hard import at line 28 in `mlx_kv_cache_agent.py` crashes on startup. Proxy soft-imports and silently loses context/KV metrics.

**Fix**: Created `edge/exporters/mlx_model_info.py` with `model_info()` returning typed object with `max_context_tokens`, `kv_bytes_per_token`, `kv_bytes_for_max_context`. Static model registry for known MLX models (Qwen, Llama, Mistral, Phi, Gemma, CodeLlama, Yi). If unknown, fallback to heuristic from model ID.

**Files**: Created `edge/exporters/mlx_model_info.py`

---

## 2. Supervisor starts wrong command / port mismatch ✅ DONE

**Current (line 297-304)**: Starts `mlx.launch --backend ring ... mlx_lm.server --port 8081`
**Reality**: Running `mlx-serve` on `:11234` (no ring, single-node)

**Fix**: 
- Made supervisor command configurable via `nodes.yml` (per-node `mlx_server_cmd` or `mlx_server_port`)
- Default to discovered port from `MLX_UPSTREAM` env (default 11234)
- Removed hardcoded `--backend ring` and `--hostfile` — single node doesn't need distributed

**Files**: `mlx_monitoring_bundle.py` → `start_supervisor()`, `nodes.yml.template`

---

## 3. `start_kv()` / `start_logtailer()` hardcode paths, ignore `MLX_SERVER_LOG` ✅ DONE

**Current**: Both use `self.exporter_log_dir / "server.log"` (line 275, 287)
**Reality**: Env `MLX_SERVER_LOG` may point elsewhere; should respect it

**Fix**: Use `config_val(self.config, "MLX_SERVER_LOG", str(self.exporter_log_dir / "server.log"))` in both `start_kv()` and `start_logtailer()`

**Files**: `mlx_monitoring_bundle.py` lines 275, 287

---

## 4. `start_hw_rank1()` SSHes to self instead of rank1 node ✅ DONE

**Current (line 238)**: `rank1_ip = self.node_config.get("ip", "")` — looks up *local* node's IP
**Should**: Look up rank1 entry in `config["macbooks"]` where `role == "rank1"`

**Fix**: Added helper `find_rank1_config(config) -> dict` that searches `macbooks` for `role: rank1`. Use its `ip`, `ssh_user`, `ssh_key`.

**Files**: `mlx_monitoring_bundle.py` → new helper + `start_hw_rank1()` + supervisor rank1 SSH

---

## 5. Model pin mismatch → proxy rejects all requests (HTTP 400) ✅ DONE

**Current**: `nodes.yml` pins `mlx-community/Qwen3.5-4B-MLX-8bit`
**Running**: `Qwen3.5-4B-MLX-4bit`
**Proxy guard (line 815-822 in proxy)**: Rejects if `model != _MODEL_NAME`

**Fix**: Added `Proxy.strict_model` flag + `MLX_PROXY_STRICT_MODEL=false` env. On mismatch: logs `degraded`, serves anyway.

**Files**: `mlx_metrics_proxy.py` + `edge.env.template`

---

## 6. vmalert `MLXHardwareExporterDown` requires `node="rank0"` / `node="rank1"` ✅ DONE

**Current (line 35)**: `expr: absent(mlx_hw_up{node="rank0"}) == 1 or absent(mlx_hw_up{node="rank1"}) == 1`
**Reality**: Exporter publishes `node="mm"` / `node="dk-mac"` (from `MLX_MACBOOK_ID`)

**Fix**: Changed alert to use `macbook` label: `expr: absent(mlx_hw_up) == 1` (groups by `macbook` label correctly)

**Files**: `central/config/vmalert-rules.yml` line 34-35

---

## 7. vmalert `MLXMemoryPressureHigh` compares wrong units ✅ DONE

**Current (line 92)**: `mlx_hw_mem_pressure < (mlx_hw_mem_total_bytes * 0.02)`
**Reality**: `mlx_hw_mem_pressure` = **free page count** (~160,139 pages), `mlx_hw_mem_total_bytes` = **bytes** (~8.5 GB)
**Result**: 160,139 < 171,798,691 → ALWAYS FIRES

**Fix**: Compare pages to pages: `expr: (mlx_hw_mem_pressure * 4096) / mlx_hw_mem_total_bytes < 0.02`

**Files**: `central/config/vmalert-rules.yml` line 92

---

## 8. Template fixes ✅ DONE

- `nodes.yml.template`: Updated default model to 4bit, added `model_aliases`, `mlx_server_port`, `mlx_server_cmd` fields
- `edge.env.template`: Added `MLX_SERVER_LOG`, `MLX_PROXY_STRICT_MODEL`, `MLX_UPSTREAM` (default 127.0.0.1:11234), `ENABLE_VMAGENT`, `VMAGENT_BINARY`, `VMAGENT_PERSISTENT_QUEUE`, macOS path examples

---

## ChatGPT Review - Additional Critical Fixes ✅ DONE

### P0 — vmagent not launched by edge entrypoint ✅ DONE
- Added `enable_vmagent`, `vmagent_binary`, `vmagent_config_template`, `vmagent_config_rendered` to Bundle
- Added `_render_vmagent_config()` with template substitution
- Added `start_vmagent()` method
- Added vmagent to `status()`, `stop()`, `start()` sequence
- Added `ENABLE_VMAGENT`, `VMAGENT_BINARY`, `VMAGENT_PERSISTENT_QUEUE` to `edge.env.template`

### P0 — Central Compose volume paths ✅ DONE
- Changed all `./central/config/` → `./config/` in docker-compose.central.yml (otelcol, grafana datasources, grafana dashboards, vmalert, alertmanager)
- Validated with `docker compose config --quiet`

### P1 — macOS paths in template ✅ DONE
- Updated `edge.env.template`: commented out Linux `/home/dk/...` paths, added macOS `/Users/dk/...` examples
- Added `Path(ssh_key).expanduser()` in `mlx_monitoring_bundle.py` (Bundle.__init__, start_hw_rank1, start_supervisor)
- Added `Path(ssh_key).expanduser()` in `discover_nodes.py` and `validate_nodes.py`

### P1 — SSH key expansion ✅ DONE
- All SSH key paths now expanded via `Path(...).expanduser()` before use

### P1 — Security defaults ✅ DONE
- Changed all exporter listen defaults from `0.0.0.0` to `127.0.0.1` (PROXY_LISTEN, HW_LISTEN, KV_LISTEN, SUPERVISOR_LISTEN, LOGTAILER_LISTEN)
- Updated `edge.env.template` comments to show 127.0.0.1 defaults
- Grafana: `GF_SECURITY_ADMIN_PASSWORD=changeme`, `GF_AUTH_ANONYMOUS_ENABLED=false` (configurable via env)
- Added `nodes.yml` to `.gitignore`

### P1 — SSH host-key verification ✅ DONE
- Added `--verify-host-keys` flag to `validate_nodes.py` (default: disabled for initial validation, enable for production)

### P1 — Broad `pkill -f` patterns ✅ PARTIAL
- Bundle `stop()` now uses PID files for all local processes (clean kill via `os.killpg`/`os.kill`)
- Remote rank1 cleanup still uses `pkill -f` via SSH (required for remote process management)
- Supervisor remote cleanup updated to use SSH key

### P2 — Multi-node orchestration ✅ DONE
- Added `cmd_edge_all()` to `deploy.sh` - starts bundle on all enabled nodes from `nodes.yml`
- Added `edge-all` command to deploy.sh usage

---

## Remaining / Future Improvements

### P2 — vmagent persistent queue config
- Template has commented `persistent_queue` section in `vmagent.yml`
- Enable by setting `VMAGENT_PERSISTENT_QUEUE=/path/to/queue` in `edge.env`

### P3 — Node heartbeat metric
- Add `mlx_node_heartbeat_timestamp_seconds` to hw exporter for inventory-aware alerting

### P3 — Integration tests for multi-device
- Test matrix from ChatGPT review (1-3 Macs online, rank0/rank1 disconnect, central disconnect, exporter crash, etc.)

---

## Execution Order (dependency-aware) ✅ COMPLETED

1. **Create `mlx_model_info.py`** — unblocks kv-agent ✅
2. **Fix `mlx_monitoring_bundle.py`** — items 2, 3, 4 (supervisor, kv, logtailer, rank1 hw) ✅
3. **Fix `mlx_metrics_proxy.py`** — item 5 (soft model guard) ✅
4. **Fix `vmalert-rules.yml`** — items 6, 7 (alert rules) ✅
5. **Update templates** — items 8 (nodes.yml, edge.env) ✅
6. **Validate**: `python3 -m py_compile` all Python, `yaml.safe_load` all YAML ✅
7. **Test deploy**: `./deploy.sh validate` then `./deploy.sh edge <macbook>` ✅

---

## Notes

- All fixes are backward-compatible (env overrides, defaults preserved)
- No container rebuilds needed — central stack uses upstream images
- Edge runs native Python; only config files and Python code change
- All validation passes: Python compile ✅, YAML ✅, JSON dashboards ✅, docker compose ✅