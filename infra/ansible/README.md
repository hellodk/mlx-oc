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
