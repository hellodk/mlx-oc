# MLX observability stack — Salt state.
#
# Renders the podman stack configs (VictoriaMetrics + otel-collector + Grafana
# + vmalert + Alertmanager) from Jinja2 templates using pillar/observability.sls
# and starts the stack, exactly like `cd observability && ./up.sh`.
#
# Static assets (Grafana provisioning + dashboards, vmalert rules) are served
# from files/ — re-run infra/salt/sync-files.sh after editing observability/.
#
# Apply (masterless):
#   salt-call --local --file-root=infra/salt --pillar-root=infra/salt/pillar state.apply
# Tear down:
#   salt-call --local --file-root=infra/salt --pillar-root=infra/salt/pillar state.apply observability.down

{%- set o = pillar.get('obs', {}) %}
{%- set im = o.get('images', {}) %}
{%- set p = o.get('ports', {}) %}
{%- set target = o.get('target_dir', '/Users/dk/mlx-oc/observability') %}

{%- if o.get('manage_prereqs', True) %}
install-podman-deps:
  pkg.installed:
    - pkgs:
      - podman
      - podman-compose
{%- endif %}

start-podman-machine:
  cmd.run:
    - name: podman machine start {{ o.get('podman_machine', 'podman-machine-default') }}
    - onlyif: |
        if podman info >/dev/null 2>&1; then
          exit 1
        fi
        exit 0

obs-target-dir:
  file.directory:
    - name: {{ target }}
    - makedirs: True

obs-subdirs:
  file.directory:
    - names:
      - {{ target }}/alertmanager
      - {{ target }}/vmalert
    - makedirs: True

obs-grafana-assets:
  file.recurse:
    - name: {{ target }}/grafana
    - source: salt://observability/files/grafana
    - include_empty: True

obs-vmalert-rules:
  file.managed:
    - name: {{ target }}/vmalert/rules.yml
    - source: salt://observability/files/vmalert/rules.yml

obs-vm-scrape:
  file.managed:
    - name: {{ target }}/vm-scrape.yml
    - source: salt://observability/templates/vm-scrape.yml.j2
    - template: jinja
    - mode: "0644"

obs-compose:
  file.managed:
    - name: {{ target }}/compose.yaml
    - source: salt://observability/templates/compose.yaml.j2
    - template: jinja
    - mode: "0644"

obs-otelcol-config:
  file.managed:
    - name: {{ target }}/otelcol-config.yaml
    - source: salt://observability/templates/otelcol-config.yaml.j2
    - template: jinja
    - mode: "0644"

obs-alertmanager:
  file.managed:
    - name: {{ target }}/alertmanager/alertmanager.yml
    - source: salt://observability/templates/alertmanager.yml.j2
    - template: jinja
    - mode: "0644"

obs-compose-up:
  cmd.run:
    - name: podman compose up -d
    - cwd: {{ target }}
    - unless: test -n "$(podman compose ps -q 2>/dev/null)"
    - watch:
      - file: obs-vm-scrape
      - file: obs-compose
      - file: obs-otelcol-config
      - file: obs-alertmanager

obs-health-wait:
  cmd.run:
    - name: |
        for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
          curl -sf -m 2 http://127.0.0.1:{{ p.get('vm', 8428) }}/health >/dev/null 2>&1 && exit 0
          sleep 2
        done
        echo "VictoriaMetrics not healthy on :{{ p.get('vm', 8428) }}" >&2
        exit 1
    - onchanges:
      - cmd: obs-compose-up
