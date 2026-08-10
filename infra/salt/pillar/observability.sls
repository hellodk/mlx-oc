# MLX observability stack — pillar with every tunable.
#
# Consumed by infra/salt/observability/init.sls (and its templates). The
# Ansible equivalent lives in infra/ansible/group_vars/all/observability.yml —
# keep the two in sync when you change a knob here.
#
# Defaults match the manual deployment path in observability/ (up.sh /
# setup.sh / compose.yaml). See infra/salt/README.md for how to apply.
obs:
  # --- Nodes ---------------------------------------------------------------
  # The podman stack runs on the observability host (node A). It scrapes the
  # serving node (rank 0) and the hw-telemetry peer (rank 1) over the LAN.
  host_ip: 192.168.1.64       # node A — observability host (also ring rank 1)
  rank0_ip: 192.168.1.5       # Mac mini B — serving node, LAN IP
  rank1_ip: 192.168.1.64      # Mac mini A — hw-telemetry peer, LAN IP

  # --- Deployment ------------------------------------------------------------
  target_dir: /Users/dk/mlx-oc/observability    # rendered configs land here
  manage_prereqs: true        # pkg.installed podman + podman-compose
  podman_machine: podman-machine-default
  podman_cpus: 4
  podman_memory: 2            # GiB

  # --- Container images (pin tags per deployment) ----------------------------
  images:
    victoria_metrics: docker.io/victoriametrics/victoria-metrics:latest
    otel_collector: docker.io/otel/opentelemetry-collector-contrib:latest
    grafana: docker.io/grafana/grafana:latest
    vmalert: docker.io/victoriametrics/vmalert:latest
    alertmanager: docker.io/prom/alertmanager:latest

  # --- Host -> container ports ------------------------------------------------
  ports:
    vm: 8428
    otlp_grpc: 4317
    otlp_http: 4318
    grafana: 3000
    vmalert: 8880
    alertmanager: 9093

  # --- VictoriaMetrics / collector / scraping ---------------------------------
  vm_retention: 30d
  scrape_interval: 10s
  sglang_enabled: true
  otel_log_mb: 50
  otel_log_backups: 5

  # --- Grafana -----------------------------------------------------------------
  grafana_admin_user: admin
  grafana_admin_password: admin
  grafana_anonymous: true
  grafana_theme: light

  # --- Alertmanager --------------------------------------------------------------
  alertmanager_resolve_timeout: 5m
  alertmanager_repeat_interval: 4h
  webhook_oncall: "http://host.containers.internal:9000/hooks/oncall"
  webhook_default: "http://host.containers.internal:9000/hooks/alert"
