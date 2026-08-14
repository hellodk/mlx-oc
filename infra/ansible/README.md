# Ansible deployment for the MLX observability stack
# ===================================================
# Turns `cd observability && ./up.sh` into a parameterized playbook: the podman
# stack (VictoriaMetrics + otel-collector + Grafana + vmalert + Alertmanager)
# is rendered from group_vars/all/observability.yml and started on the
# observability host (node A). Nothing is hardcoded in the playbook or role —
# every address, image tag, port, interval, credential and webhook comes from
# the group_vars.

## Layout

| Path | What |
|------|------|
| `observability.yml` | playbook entry point (`hosts: observability`) |
| `inventories/example/hosts.yml` | example inventory (the observability host, `ansible_connection: local`) |
| `group_vars/all/observability.yml` | **every tunable** — IPs, images, ports, retention, Grafana creds, webhooks |
| `roles/mlx-observability/tasks/main.yml` | prereqs -> podman machine -> render -> `compose up` -> health -> (optional) k8s dashboards |
| `roles/mlx-observability/templates/*.j2` | parameterized `vm-scrape.yml`, `compose.yaml`, `otelcol-config.yaml`, `alertmanager.yml` |

The templates are mirrors of `observability/*.yaml`. When you deploy with
Ansible, change the *templates*; the manual files under `observability/` stay
the source of truth for the `./up.sh` path. Static assets (Grafana
provisioning + 12 dashboards, `vmalert/rules.yml`) are copied from the repo
checkout at `{{ obs_source_dir }}`, so they are never duplicated.

## Prerequisites

```bash
brew install ansible        # or: pipx install ansible-core
```

No inventory secrets: the playbook runs with `ansible_connection: local` on
the observability host itself (same machine that runs `observability/up.sh`).

## Usage

```bash
cd infra/ansible

# Deploy + start (render configs, pull images, compose up, wait for /health)
ansible-playbook observability.yml

# Tear down (compose down, volumes kept) — same as observability/down.sh
ansible-playbook observability.yml -e obs_state=absent

# Scope a run to a phase
ansible-playbook observability.yml --tags prereqs   # brew deps + podman machine
ansible-playbook observability.yml --tags config    # render configs only
ansible-playbook observability.yml --tags up        # compose up + health only

# Dry-run what would change
ansible-playbook observability.yml --check
```

## Making it fully configurable

Edit `group_vars/all/observability.yml`. The knobs:

- **Topology**: `obs_host_ip` (observability host), `obs_rank0_ip` /
  `obs_rank1_ip` (scrape targets) — swap these when the serving node changes.
- **Deployment**: `obs_state` (`present` / `absent`), `obs_repo_dir`,
  `obs_target_dir`, `obs_source_dir`, `obs_manage_prereqs`, podman machine
  name / cpus / memory.
- **Images**: pin tags under `obs_images` (e.g. a specific
  `victoriametrics/victoria-metrics:v1.107.0`).
- **Ports**: host:container map under `obs_ports`.
- **Tuning**: `obs_vm_retention`, `obs_scrape_interval`, `obs_sglang_enabled`,
  collector log rotation.
- **Grafana**: admin user / password, anonymous access, default theme.
- **Alertmanager**: resolve/repeat intervals, on-call + default webhook URLs.
- **k8s**: set `obs_apply_dashboards: true` to push the dashboards into the
  k0s Grafana afterwards (needs `kubectl` + the cluster kubeconfig).

Keep the values in sync with `infra/salt/pillar/observability.sls` if you
maintain both deployment paths.

## Verifying

```bash
open http://<obs_host_ip>:3000            # Grafana -> "MLX Cluster"
open http://<obs_host_ip>:8428/vmui       # VictoriaMetrics
curl -s http://127.0.0.1:8428/api/v1/query?query=mlx_requests_total
curl -s http://127.0.0.1:8428/api/v1/query?query='up{job="mlx-proxy"}'
```

## MLX cluster deploy (the ring itself)

`cluster.yml` deploys the distributed inference ring: per-node prereqs, venvs
(pinned wheels), model weights, repo sync and the rendered `hosts.json` /
`hosts_rev.json` / `cluster.env`, then starts the stack on rank0 via
`./cluster/start_server.sh`.

### Layout

| Path | What |
|------|------|
| `cluster.yml` | main playbook (`hosts: mlx_ring`); tags `prereqs` / `setup` / `start` / `stop` |
| `check.yml` | read-only pre-flight report (prereqs only, nothing modified) |
| `inventories/example/mlx-cluster.yml` | example ring inventory (group `mlx_ring`) |
| `inventories/generated/hosts.yml` | **written by the wizard** — don't hand-edit |
| `group_vars/all/mlx-cluster.yml` | **every knob** — nodes, model, versions, ports, pins |
| `roles/mlx-cluster/tasks/*` | prereqs -> setup -> deploy / stop |
| `roles/mlx-cluster/templates/*` | `hosts.json`, `hosts_rev.json`, `cluster.env`, `requirements-*.txt` |
| `mlx-deploy.example.json` | non-interactive config the wizard accepts via `--config` |

### The wizard (recommended)

`tools/mlx-deploy.py` is an interactive input wizard: it asks for node count,
per-node ssh + ring IPs, interconnect, model, Python/versions and install
method, scans rank0 + every peer for prerequisites (and tells you the fix —
`brew install ansible`, `ssh-copy-id`, static ring IP, staging the weights,
etc.), then writes the inventory + group_vars and optionally runs the playbook.

```bash
python3 tools/mlx-deploy.py                 # interactive
python3 tools/mlx-deploy.py --config infra/ansible/mlx-deploy.example.json --apply   # non-interactive
python3 tools/mlx-deploy.py --check         # collect + read-only pre-flight report
python3 tools/mlx-deploy.py --no-run        # collect + write config, stop
```

### Hand-edited equivalent

```bash
cd infra/ansible
# 1. edit group_vars/all/mlx-cluster.yml (nodes, model, pins) and the inventory
ansible-playbook -i inventories/example/mlx-cluster.yml check.yml              # read-only
ansible-playbook -i inventories/example/mlx-cluster.yml cluster.yml            # deploy + start
ansible-playbook -i inventories/example/mlx-cluster.yml cluster.yml --tags stop
```

### Validated start-only workflow (nodes already deployed)

When the venvs, weights and repo are already in place on every node (e.g. an
air-gapped install where setup was done once), the ring can be brought up with
the start tag alone — no prereq scan, no reinstall:

```bash
cd infra/ansible
ansible-playbook -i inventories/generated/hosts.yml check.yml            # read-only pre-flight (validated 2-node ring)
ansible-playbook -i inventories/generated/hosts.yml cluster.yml --tags start
```

`check.yml` asserts the per-node prereqs (python, venv, model, disk/RAM, ring
link reachability at ~0.5 ms, passwordless ssh to every peer, ports free); the
start tag runs `./cluster/start_server.sh start` on rank0 and waits for the
server and the OpenAI proxy. All components are daemonized (nohup, pid files
under `cluster/logs/`); the observability stack reads `cluster/logs/server.log`
for KV/context metrics, so keep the log files even on a quiet system.
Verified end-to-end against the live 2-node ring: check = 0 failures, start
brings up supervisor + mlx.launch ring + metrics proxy + hw/kv/logtailer agents,
and a chat completion round-trips through the proxy in seconds.

### Versions (decided from the blogs — post numbers in parens)

- **Python 3.12** for the server venv on every node (1, 23, INSTALL.md §8):
  macOS Local-Network privacy blocks a third-party **py3.14** binary spawned
  over SSH (`EHOSTUNREACH`), and mlx.launch spawns the remote shard exactly
  that way. The proxy/telemetry venv defaults to 3.12 too so the whole cluster
  is one interpreter; it never touches SSH, so it could stay on a newer Python.
- **mlx 0.32.0 / mlx-lm 0.31.3 / mlx-metal 0.32.0** identical on every rank
  (13, 14, 23). `mlx-metal` is a separate wheel — `mlx` alone is CPU-only (23).
- Optional extras pinned to the repo's versions: prometheus_client 0.26.0,
  otel 1.44.0 / 0.65b0, opik 2.2.13 + aiohttp 3.14.3 + litellm 1.95.0 (23).
- Offline sites: set `install_method: wheelhouse` and point `wheelhouse` at the
  air-gap kit's wheelhouse dir (INSTALL.md §2); wheels are installed with
  `--no-index --find-links`.

### Adding nodes to an existing cluster

**Yes — the ring reads its size from the hostfile at launch**, so a ring is not
fixed-size. To add a node:

1. Append it to `mlx_cluster.nodes` (rank order = hostfile order, rank0 first)
   and to the inventory.
2. On the new node: install Python 3.12, put it on the ring link (static IP in
   `ring_subnet`), `ssh-copy-id` from rank0, and re-run the playbook — setup
   creates the venv, installs the pins, syncs the repo and stages the model
   there.
3. Re-run `cluster.yml`; `start_server.sh start` re-creates the whole ring with
   N nodes.

Caveats (from the blogs): there is **no hot-add** — the ring is rebuilt on
restart, so a node change is a rolling restart of the stack (16); every node
must hold the full weights (23); the ring syncs to its **slowest member** and
adds per-token ring-sync latency, so heterogeneous M2+M4 pairs are the
documented crash source (13, 16). Distribution only pays off past a single
node's memory (19, 23).

### Single node

A one-entry `nodes` list works: `mlx.launch` with a single hostfile entry runs
the server locally with no ring sync — the whole stack still starts, proxies
and telemetries exactly the same.
