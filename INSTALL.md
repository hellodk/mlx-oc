# INSTALL — Air-gapped MLX Distributed Cluster

Installs the two-node MLX inference cluster from this repo on an **air-gapped
network** (no internet access). Everything is assembled on an internet-connected
Mac first, carried across the gap on a USB drive, and installed with
`pip install --no-index` from a local wheelhouse. The model weights live on
**both** nodes and are never downloaded on the target.

This is the full dependency guide for the running system documented in
[README.md](README.md) and blog posts 7, 13, 14 and 16.

---

## 1. What you are building

| Piece | Runs on | Port | Needs (venv) |
|---|---|---|---|
| `mlx_lm.server` (via `mlx.launch --backend ring`) | both nodes, sharded | `127.0.0.1:8081` (rank 0) | `~/venvs/mlx` (Python 3.12) |
| `cluster/mlx_metrics_proxy.py` | rank 0 | `0.0.0.0:8080` | repo `.venv` (Python 3.14) |
| `cluster/mlx_hw_telemetry.py` | both nodes | `0.0.0.0:9102` | repo `.venv` |
| `cluster/mlx_kv_cache_agent.py` | rank 0 | `0.0.0.0:9104` | repo `.venv` |
| `cluster/mlx_server_supervisor.py` | rank 0 | `0.0.0.0:9105` | repo `.venv` |
| `cluster/mlx_server_log_tailer.py` | rank 0 | `0.0.0.0:9106` | repo `.venv` |

Two venvs, two Pythons, one reason:

- **`~/venvs/mlx` — Python 3.12** runs the distributed server. macOS
  Local-Network privacy (TCC) silently blocks the third-party **py3.14** binary
  from reaching local subnets when spawned over SSH (`EHOSTUNREACH`), so the
  remote shard must use the py3.12 venv. See §9.
- **`.venv` — Python 3.14** at the repo root runs the proxy and the small
  telemetry agents (rank 0 only, no SSH).

Transport: the ring runs over a **dedicated Gigabit Ethernet link**
(`en0` = `10.0.0.0/24`), and ssh/management goes over a second LAN
(`192.168.1.0/24` in this repo; see §6).

---

## 2. Build the air-gap kit on an internet-connected Mac

Use an **Apple Silicon (arm64) Mac running the same macOS major version** as the
targets. Everything you need fits on a USB drive.

```
airgap-kit/
├── python-3.12.x-macos11.pkg          # python.org installers (both nodes)
├── python-3.14.x-macos11.pkg
├── mlx-oc.tar.gz                      # this repo
├── wheelhouse-mlx/                    # server venv wheels (py3.12)
│   └── requirements-mlx.txt
├── wheelhouse-proxy/                  # proxy venv wheels (py3.14)
│   └── requirements-proxy.txt
└── Qwen3.5-4B-MLX-8bit/               # model, ~4.8 GB — copy to BOTH nodes
```

### 2.1 Python installers

Download the **.pkg universal2 installers** from python.org for the exact
versions you will pin, and put them in the kit. (Homebrew is not practical to
bootstrap offline; the python.org packages install to
`/Library/Frameworks/Python.framework` and need no network.)

### 2.2 Repo

```bash
tar czf mlx-oc.tar.gz mlx-oc/          # or: git bundle create mlx-oc.bundle --all
```

### 2.3 Server wheelhouse (Python 3.12)

```bash
python3.12 -m venv /tmp/donor-mlx && source /tmp/donor-mlx/bin/activate
pip install \
  mlx==0.32.0 mlx-lm==0.31.3 mlx-metal==0.32.0 \
  prometheus_client==0.26.0 \
  opentelemetry-api==1.44.0 opentelemetry-sdk==1.44.0 \
  opentelemetry-exporter-otlp-proto-http==1.44.0 \
  opentelemetry-exporter-otlp-proto-common==1.44.0 \
  opentelemetry-proto==1.44.0 opentelemetry-semantic-conventions==0.65b0 \
  opentelemetry-instrumentation==0.65b0 opentelemetry-instrumentation-logging==0.65b0
pip freeze > wheelhouse-mlx/requirements-mlx.txt
pip download -d wheelhouse-mlx -r wheelhouse-mlx/requirements-mlx.txt
```

`mlx-metal` is a separate (large) platform wheel; installing only `mlx` gives
the CPU backend and no Metal/GPU acceleration. `pip download` pulls the
transitive deps (numpy, transformers, huggingface_hub, safetensors,
sentencepiece, tokenizers, hf-xet, …) as wheels.

If the donor Mac is **not** the same OS/arch, force the target's platform tag
(ask the target first: `python -c "import sysconfig; print(sysconfig.get_platform())"`):

```bash
pip download -d wheelhouse-mlx -r wheelhouse-mlx/requirements-mlx.txt \
  --only-binary=:all: --python-version 312 \
  --platform macosx_13_0_arm64     # replace with the target's platform tag
```

### 2.4 Proxy wheelhouse (Python 3.14)

Repeat with `python3.14` and a second donor venv:

```bash
python3.14 -m venv /tmp/donor-proxy && source /tmp/donor-proxy/bin/activate
pip install \
  prometheus_client==0.26.0 \
  opentelemetry-api==1.44.0 opentelemetry-sdk==1.44.0 \
  opentelemetry-exporter-otlp-proto-http==1.44.0 \
  opentelemetry-exporter-otlp-proto-common==1.44.0 \
  opentelemetry-proto==1.44.0 opentelemetry-semantic-conventions==0.65b0 \
  opentelemetry-instrumentation==0.65b0 opentelemetry-instrumentation-logging==0.65b0
# optional: only if you want Opik tracing / the agentic bench
pip install opik==2.2.13 aiohttp==3.14.3 litellm==1.95.0
pip freeze > wheelhouse-proxy/requirements-proxy.txt
pip download -d wheelhouse-proxy -r wheelhouse-proxy/requirements-proxy.txt
```

### 2.5 Model weights

Download once on the donor, either as a **plain directory** (simplest) or in the
HuggingFace cache layout:

```bash
pip install -U "huggingface_hub[cli]"     # donor only
huggingface-cli download mlx-community/Qwen3.5-4B-MLX-8bit \
  --local-dir Qwen3.5-4B-MLX-8bit
```

Ship the resulting directory (≈4.8 GB). **Both nodes need a copy** — the ring
shards the model across the two Macs, so rank 1 must hold the weights too.

---

## 3. Install Python (both nodes)

Run the two `.pkg` installers from the kit. Verify:

```bash
python3.12 --version     # from /Library/Frameworks/Python.framework/Versions/3.12/bin
python3.14 --version
```

---

## 4. Create the venvs and install wheels offline (both nodes)

Use `python -m pip` — the venv `bin/pip` script keeps a stale shebang if the
venv is ever moved (this bites the repo's own `.venv`; `pip` in `.venv/bin`
points at a now-missing path).

```bash
# server venv (Python 3.12) — required on BOTH nodes
python3.12 -m venv ~/venvs/mlx
~/venvs/mlx/bin/python -m pip install --no-index --find-links wheelhouse-mlx \
  -r wheelhouse-mlx/requirements-mlx.txt
~/venvs/mlx/bin/python -c "import mlx, mlx.core as mx; print('mlx ok')"

# proxy venv (Python 3.14) — rank 0 only, at the repo root
cd mlx-oc
python3.14 -m venv .venv
.venv/bin/python -m pip install --no-index --find-links wheelhouse-proxy \
  -r wheelhouse-proxy/requirements-proxy.txt
.venv/bin/python -c "import prometheus_client; print('proxy deps ok')"
```

`--no-index` disables PyPI; `--find-links` points pip at the local wheelhouse.
pip still resolves transitive dependencies *within* the wheelhouse, so the two
requirements files must contain the complete `pip freeze` output (they do —
that is why we ship `pip freeze` output, not just the top-level pins).

---

## 5. Network & SSH (one-time)

### 5.1 The ring link (dedicated Ethernet)

Connect `en0` on both Macs to a switch (or directly cable-to-cable). Give them
static IPs on the private subnet the repo expects:

| Node | Interface | IP |
|---|---|---|
| rank 0 (M2 8 GB, this repo's main node) | `en0` | `10.0.0.1/24` |
| rank 1 (M4 16 GB) | `en0` | `10.0.0.2/24` |

System Settings → Network → Ethernet → Configure IPv4 → Manual, or:

```bash
sudo networksetup -setmanual "Ethernet" 10.0.0.1 255.255.255.0
# on rank 1: sudo networksetup -setmanual "Ethernet" 10.0.0.2 255.255.255.0
```

Confirm the interface name first with `networksetup -listallhardwareports`
(it is `en0` on the Mac mini's built-in port). Verify L2 reachability:
`ping 10.0.0.2` from rank 0 — expect ~0.5 ms and 0% loss.

### 5.2 The management link (ssh / telemetry)

Keep a second LAN for management. In this repo rank 0 = `192.168.1.64` and
rank 1 = `192.168.1.5` (Wi-Fi/LAN `en1`). If your air-gapped site only has the
`10.0.0.0/24` link, that is fine — just point ssh at `10.0.0.2` and set
`RANK1="10.0.0.2"` in `cluster/start_server.sh`.

### 5.3 Passwordless SSH

```bash
# on rank 0
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
ssh-copy-id dk@10.0.0.2      # or dk@192.168.1.5 on the management LAN
ssh -o BatchMode=yes 192.168.1.5 true   # must return without a prompt
```

### 5.4 Hostfiles

`cluster/hosts.json` (ring order, rank 0 side) and `cluster/hosts_rev.json`
(the reverse file used on the remote) must match your subnet:

```json
{
  "backend": "ring",
  "envs": [],
  "hosts": [
    {"ssh": "127.0.0.1", "ips": ["10.0.0.1"]},
    {"ssh": "192.168.1.5", "ips": ["10.0.0.2"]}
  ]
}
```

Change `ips` (and the rank-1 `ssh` host if you use the 10.0.0.0/24 link) to
match. You can regenerate the hostfiles automatically instead:

```bash
~/venvs/mlx/bin/mlx.distributed_config --hosts 127.0.0.1,192.168.1.5 \
  --over ethernet --backend ring --auto-setup
```

---

## 6. Model placement (offline)

**Copy the model to both nodes.** Either layout works; pick one.

**Option A — plain local directory (recommended, no HF cache involved):**

```bash
# both nodes
sudo mkdir -p /opt/mlx-models && sudo cp -R Qwen3.5-4B-MLX-8bit /opt/mlx-models/
```

Then set `MODEL` in `cluster/start_server.sh` to the local path. `mlx_lm.server`
and the KV-cache agent (`cluster/mlx_model_info.py`) both accept an explicit
directory and read `config.json` from it — no hub lookup.

**Option B — keep the HF cache layout (zero script changes):**

```bash
# both nodes
mkdir -p ~/.cache/huggingface/hub
cp -R models--mlx-community--Qwen3.5-4B-MLX-8bit \
      ~/.cache/huggingface/hub/
```

Keep `MODEL="mlx-community/Qwen3.5-4B-MLX-8bit"` in `start_server.sh`.

### 6.1 Go fully offline

Whatever the layout, force `huggingface_hub` to never touch the network. Add
these to the top of `cluster/start_server.sh` (children inherit them):

```bash
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
```

---

## 7. Start and verify

```bash
cd mlx-oc
./cluster/start_server.sh              # supervisor, ring, proxy, hw, kv, logtailer
./cluster/start_server.sh status       # one-line status per component
curl -s http://127.0.0.1:8080/v1/models
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"/opt/mlx-models/Qwen3.5-4B-MLX-8bit", \
       "messages":[{"role":"user","content":"say hi"}],"max_tokens":16}'
curl -s http://127.0.0.1:9105/metrics | grep mlx_server_state   # expect 1.0
curl -s http://127.0.0.1:9102/metrics | grep mlx_hw_load1       # rank 0
curl -s http://192.168.1.5:9102/metrics | grep mlx_hw_load1     # rank 1
./cluster/stop_server.sh
```

The launch line baked into `start_server.sh` already carries the tuning from
blog 16 (`--decode-concurrency 8 --prompt-concurrency 4 --prefill-step-size 512
--prompt-cache-size 2 --max-tokens 1024`). The readiness check on `:8081` only
proves rank 0 is up; watch `cluster/logs/server.log` and the ring metrics for
the full picture.

---

## 8. Air-gapped gotchas

- **macOS Local-Network privacy (TCC)**: a third-party binary spawned *over
  SSH* can be silently blocked from reaching local subnets — instant
  `EHOSTUNREACH`, no TCC dialog. The repo's workaround is pinned in stone: the
  **distributed server runs on the Python 3.12 venv** (`~/venvs/mlx`); the proxy
  runs locally on py3.14. Do not "simplify" by running the server from the
  py3.14 venv on the remote — it is the documented failure.
- **Use `python -m pip`**, never the venv `bin/pip` script — the shebang can
  point at a path that no longer exists if the venv moved.
- **`mlx-metal` must be installed** — `mlx` alone is CPU-only.
- **The ring needs a full 2-node ring.** If `mlx.launch` fails, run
  `mlx.distributed_config --hosts ... --over ethernet` for ring diagnostics; it
  probes the `10.0.0.0/24` links and prints whether a full ring is possible.
- **`--hostfile` must match both sides.** Update `hosts.json` *and*
  `hosts_rev.json` together (see §5.4).
- **Model on both nodes.** A missing weight file on rank 1 crashes the ring at
  load; verify with `ls /opt/mlx-models/Qwen3.5-4B-MLX-8bit` on each node.
- **Logs dir**: `start_server.sh` creates `cluster/logs/`; the KV-cache agent
  tails `cluster/logs/server.log` — do not move that file.

### Optional: observability (not required to serve)

The Prometheus/VictoriaMetrics/Grafana stack in `observability/` needs the podman
images (`victoriametrics/victoria-metrics`, `otel/opentelemetry-collector`,
`grafana/grafana`, `prom/alertmanager`, etc.) shipped across the gap with
`podman save` / `podman load`, and `observability/setup.sh` renders
`vm-scrape.yml` from your IPs. The cluster serves fine without it.

### Optional: SGLang scaffold (separate, unverified)

`sglang/` is experimental scaffolding with an unconfirmed Apple Silicon backend;
it is a source build and out of scope for an air-gapped kit. Skip it unless you
also vendor the SGLang wheels.
