# Tear down the MLX observability stack (podman compose down, volumes kept).
#
# Not part of top.sls — apply it explicitly:
#   salt-call --local --file-root=infra/salt --pillar-root=infra/salt/pillar \
#     state.apply observability.down
{%- set o = pillar.get('obs', {}) %}
{%- set target = o.get('target_dir', '/Users/dk/mlx-oc/observability') %}

observability-down:
  cmd.run:
    - name: podman compose down
    - cwd: {{ target }}
