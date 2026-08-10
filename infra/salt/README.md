# Salt deployment for the MLX observability stack
# ================================================
# Turns `cd observability && ./up.sh` into a Salt state: the podman stack
# (VictoriaMetrics + otel-collector + Grafana + vmalert + Alertmanager) is
# rendered from the pillar and started on the observability host (node A).
# Nothing is hardcoded in the states — every address, image tag, port,
# interval, credential and webhook comes from `pillar/observability.sls`.

## Layout

| Path | What |
|------|------|
| `top.sls` | state tree — binds `observability` to every minion in `base` |
| `observability/init.sls` | deploy + start: prereqs -> podman machine -> render -> `compose up` -> health wait |
| `observability/down.sls` | explicit teardown (`state.apply observability.down`) |
| `observability/templates/*.j2` | parameterized `vm-scrape.yml`, `compose.yaml`, `otelcol-config.yaml`, `alertmanager.yml` |
| `observability/files/` | **duplicated** static assets (Grafana provisioning + dashboards, vmalert rules) — see `sync-files.sh` |
| `pillar/observability.sls` | **every tunable** — IPs, images, ports, retention, Grafana creds, webhooks |
| `pillar/top.sls` | pillar tree |
| `sync-files.sh` | re-copies `observability/` static assets into `files/` after edits |

The templates are mirrors of `observability/*.yaml`. When you deploy with
Salt, change the *templates*; the manual files under `observability/` stay the
source of truth for the `./up.sh` path.

## Prerequisites

- Salt on the observability host (this machine): `brew install saltstack`
  (installs `salt-call` at `/opt/salt/bin/salt-call` — link it or call it by
  that path).
- `podman` + `podman-compose` (installed automatically by the state when
  `manage_prereqs: true`).
- No master required: the state is applied masterless (local mode).

## Usage

Masterless (from the repo root):

```bash
# Deploy + start (render configs, pull images, compose up, wait for /health)
salt-call --local --file-root=infra/salt --pillar-root=infra/salt/pillar state.apply

# Tear down (compose down, volumes kept)
salt-call --local --file-root=infra/salt --pillar-root=infra/salt/pillar state.apply observability.down

# Dry-run (what would change)
salt-call --local --file-root=infra/salt --pillar-root=infra/salt/pillar state.test

# Validate the state without running it
salt-call --local --file-root=infra/salt --pillar-root=infra/salt/pillar state.show_sls observability
```

With a master/minion setup, serve `infra/salt/` as the file root and
`infra/salt/pillar/` as the pillar root, then `salt 'node-a' state.apply`.

## Making it fully configurable

Edit `pillar/observability.sls`. The knobs:

- **Topology**: `host_ip` (observability host), `rank0_ip` / `rank1_ip`
  (scrape targets) — swap these when the serving node changes.
- **Deployment**: `target_dir`, `manage_prereqs`, podman machine name.
- **Images**: pin tags under `images`.
- **Ports**: per-service host/container ports under `ports`.
- **Tuning**: `vm_retention`, `scrape_interval`, `sglang_enabled`, collector
  log rotation.
- **Grafana**: admin user / password, anonymous access, default theme.
- **Alertmanager**: resolve/repeat intervals, on-call + default webhook URLs.

Keep the values in sync with `infra/ansible/group_vars/all/observability.yml`
if you maintain both deployment paths.

## Verifying

```bash
open http://<host_ip>:3000            # Grafana -> "MLX Cluster"
open http://<host_ip>:8428/vmui       # VictoriaMetrics
curl -s http://127.0.0.1:8428/api/v1/query?query=mlx_requests_total
```
