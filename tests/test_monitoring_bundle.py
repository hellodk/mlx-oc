import importlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_DIR = REPO_ROOT / "cluster" / "mlx_monitoring_bundle"
EDGE_DIR = BUNDLE_DIR / "edge"
EXPORTERS_DIR = EDGE_DIR / "exporters"

sys.path.insert(0, str(EDGE_DIR))
sys.path.insert(0, str(EXPORTERS_DIR))


def test_vmagent_yaml_rendering():
    """Verify vmagent.yml renders cleanly without corrupting YAML syntax."""
    template_path = EDGE_DIR / "vmagent-edge" / "vmagent.yml"
    assert template_path.exists(), "vmagent.yml template must exist"
    template = template_path.read_text(encoding="utf8")

    subs = {
        "CENTRAL_VICTORIA_METRICS": "192.168.1.10:8428",
        "OTLP_SCHEME": "http",
        "DEVICE_ID": "test-mac",
        "MACBOOK_ID": "test-mac",
        "MACBOOK_SERIAL": "SERIAL12345",
        "VMAGENT_SCRAPE_INTERVAL": "15s",
        "VMAGENT_MAX_SHARDS": "100",
        "VMAGENT_QUEUE_CAPACITY": "2500",
        "VMAGENT_PERSISTENT_QUEUE": "/tmp/test-pq",
    }

    def repl(m):
        expr = m.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return subs.get(var, default)
        if expr in subs:
            return subs[expr]
        return ""

    rendered = re.sub(r"\$\{([^}]+)\}", repl, template)
    unresolved = re.findall(r"\$\{([^}]+)\}", rendered)
    assert not unresolved, f"Unresolved placeholders found in rendered YAML: {unresolved}"

    data = yaml.safe_load(rendered)
    assert data["global"]["scrape_interval"] == "15s"
    assert data["global"]["external_labels"]["device"] == "test-mac"
    assert data["global"]["external_labels"]["macbook"] == "test-mac"
    assert data["global"]["external_labels"]["serial"] == "SERIAL12345"
    assert len(data["scrape_configs"]) == 7


def test_vmagent_yaml_rendering_with_defaults():
    """Verify fallback values work when variables are missing."""
    template_path = EDGE_DIR / "vmagent-edge" / "vmagent.yml"
    template = template_path.read_text(encoding="utf8")
    subs = {}

    def repl(m):
        expr = m.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return subs.get(var, default)
        if expr in subs:
            return subs[expr]
        return ""

    rendered = re.sub(r"\$\{([^}]+)\}", repl, template)
    data = yaml.safe_load(rendered)
    assert data["global"]["scrape_interval"] == "10s"
    assert data["global"]["external_labels"]["device"] == "unknown"
    assert data["global"]["external_labels"]["macbook"] == "unknown"


def test_no_duplicate_metrics_between_exporters():
    """Verify importing kv_cache_agent, proxy, process_telemetry, and sbom_collector does not throw CollectorRegistry collision."""
    import mlx_kv_cache_agent
    import mlx_metrics_proxy
    import mlx_process_telemetry
    import mlx_sbom_collector

    # All modules must import cleanly into the same process
    assert hasattr(mlx_kv_cache_agent, "G_UTIL")
    assert hasattr(mlx_metrics_proxy, "CONTEXT_MAX")
    assert hasattr(mlx_process_telemetry, "G_PROCESS_CPU")
    assert hasattr(mlx_sbom_collector, "G_SBOM_PACKAGE_INFO")


def test_model_info_graceful_fallback():
    """Verify missing model config returns None gracefully without unhandled exception."""
    import mlx_model_info
    result = mlx_model_info.model_info("NonExistentTestModel12345", model_path="/non/existent/path")
    assert result is None


def test_vmalert_rules_validity():
    """Verify vmalert-rules.yml parses and contains no unsupported increases( expressions."""
    rules_path = BUNDLE_DIR / "central" / "config" / "vmalert-rules.yml"
    text = rules_path.read_text(encoding="utf8")

    assert "increases(" not in text, "increases( is an unsupported MetricsQL function"

    data = yaml.safe_load(text)
    assert "groups" in data
    alerts = [r["alert"] for g in data["groups"] for r in g.get("rules", []) if "alert" in r]
    assert "MLXDeviceSilent" in alerts
    assert "MLXMacbookSilent" in alerts
    assert "MLXServerCrashLooping" in alerts
    assert "MLXHardwareExporterDown" in alerts
    assert "MLXMemoryPressureHigh" in alerts


def test_exporter_listen_defaults_are_local():
    """Verify standalone exporter scripts default to 127.0.0.1 for security."""
    from mlx_hw_telemetry import LISTEN_DEFAULT as hw_listen
    from mlx_kv_cache_agent import LISTEN_DEFAULT as kv_listen
    from mlx_server_supervisor import LISTEN_DEFAULT as sup_listen

    assert hw_listen.startswith("127.0.0.1:"), f"hw_telemetry should bind to 127.0.0.1 by default, got {hw_listen}"
    assert kv_listen.startswith("127.0.0.1:"), f"kv_cache_agent should bind to 127.0.0.1 by default, got {kv_listen}"
    assert sup_listen.startswith("127.0.0.1:"), f"supervisor should bind to 127.0.0.1 by default, got {sup_listen}"


def test_no_magic_container_domains():
    """Verify host.docker.internal and host.containers.internal are completely eliminated."""
    for path in (BUNDLE_DIR / "central").rglob("*"):
        if path.is_file() and path.suffix in (".yml", ".yaml", ".json"):
            content = path.read_text(encoding="utf8", errors="ignore")
            assert "host.docker.internal" not in content, f"Found host.docker.internal in {path}"
            assert "host.containers.internal" not in content, f"Found host.containers.internal in {path}"


def test_central_compose_image_resolution_and_profiles():
    """Verify compose images are correctly partitioned by profile and use rustfs."""
    compose_path = BUNDLE_DIR / "central" / "docker-compose.central.yml"
    assert compose_path.exists()
    doc = yaml.safe_load(compose_path.read_text(encoding="utf8"))
    services = doc.get("services", {})

    base_images = set()
    opik_images = set()

    for sname, sdata in services.items():
        profiles = sdata.get("profiles", [])
        img = sdata.get("image", "").replace("${OPIK_VERSION:-2.2.13}", "2.2.13")
        assert img, f"Service {sname} missing image declaration"
        assert "minio" not in img, f"Service {sname} references legacy minio image: {img}"
        if "opik" in profiles:
            opik_images.add(img)
        else:
            base_images.add(img)

    # Base profile: 5 monitoring containers
    expected_base = {
        "victoriametrics/victoria-metrics:v1.155.0",
        "otel/opentelemetry-collector-contrib:0.139.0",
        "grafana/grafana:13.3.0",
        "victoriametrics/vmalert:v1.155.0",
        "prom/alertmanager:v0.34.2",
    }
    assert base_images == expected_base

    # Opik profile must include rustfs and not bitnami minio
    assert "rustfs/rustfs:1.0.5" in opik_images

    # Backend must depend on minio, clickhouse, mysql, redis being healthy
    backend_deps = services["backend"].get("depends_on", {})
    assert "minio" in backend_deps
    assert backend_deps["minio"]["condition"] == "service_healthy"
    assert "clickhouse" in backend_deps
    assert backend_deps["clickhouse"]["condition"] == "service_healthy"


def test_vmagent_custom_listen_ports_rendering():
    """Verify vmagent.yml renders custom exporter listen ports properly."""
    template_path = EDGE_DIR / "vmagent-edge" / "vmagent.yml"
    template = template_path.read_text(encoding="utf8")
    subs = {
        "PROXY_LISTEN": "127.0.0.1:18080",
        "HW_LISTEN": "127.0.0.1:19102",
        "KV_LISTEN": "127.0.0.1:19104",
        "SUPERVISOR_LISTEN": "127.0.0.1:19105",
        "LOGTAILER_LISTEN": "127.0.0.1:19106",
        "PROCESS_LISTEN": "127.0.0.1:19107",
        "SBOM_LISTEN": "127.0.0.1:19108",
    }

    def repl(m):
        expr = m.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return subs.get(var, default)
        if expr in subs:
            return subs[expr]
        return ""

    rendered = re.sub(r"\$\{([^}]+)\}", repl, template)
    data = yaml.safe_load(rendered)
    targets = {cfg["job_name"]: cfg["static_configs"][0]["targets"][0] for cfg in data["scrape_configs"]}
    assert targets["mlx-proxy"] == "127.0.0.1:18080"
    assert targets["mlx-hw"] == "127.0.0.1:19102"
    assert targets["mlx-kv"] == "127.0.0.1:19104"
    assert targets["mlx-supervisor"] == "127.0.0.1:19105"
    assert targets["mlx-logtailer"] == "127.0.0.1:19106"
    assert targets["mlx-process"] == "127.0.0.1:19107"
    assert targets["mlx-sbom"] == "127.0.0.1:19108"


def test_vmagent_config_cli_action():
    """Verify mlx_monitoring_bundle.py vmagent-config renders and outputs YAML."""
    import subprocess
    bundle_script = EDGE_DIR / "mlx_monitoring_bundle.py"
    proc = subprocess.run(
        [sys.executable, str(bundle_script), "vmagent-config"],
        capture_output=True,
        text=True,
        cwd=str(BUNDLE_DIR),
        timeout=10,
    )
    assert proc.returncode == 0, f"vmagent-config failed: {proc.stderr}"
    assert "scrape_configs:" in proc.stdout
    assert "job_name: mlx-proxy" in proc.stdout
    assert "job_name: mlx-hw" in proc.stdout


def test_deploy_review_vmagent_command():
    """Verify deploy.sh review-vmagent runs cleanly and displays status/health checks."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    env = dict(os.environ, SKIP_GIT_PULL="1")
    proc = subprocess.run(
        [str(deploy_sh), "review-vmagent"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"deploy.sh review-vmagent failed: {proc.stderr}"
    assert "vmagent Edge Configuration Review" in proc.stdout
    assert "Fail Checks & Health:" in proc.stdout
    assert "Scrape Targets (Local Exporters):" in proc.stdout


def test_deploy_review_grafana_command():
    """Verify deploy.sh review-grafana runs cleanly and displays credentials/health checks."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    env = dict(os.environ, SKIP_GIT_PULL="1")
    proc = subprocess.run(
        [str(deploy_sh), "review-grafana"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"deploy.sh review-grafana failed: {proc.stderr}"
    assert "Grafana Central Configuration Review" in proc.stdout
    assert "Credentials & Security:" in proc.stdout
    assert "Admin Password" in proc.stdout
    assert "Fail Checks & Health:" in proc.stdout


def test_diagnostics_doctor_command():
    """Verify diagnostics doctor runs end-to-end pipeline traceroute."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    proc = subprocess.run(
        [sys.executable, str(diag_script), "doctor"],
        capture_output=True,
        text=True,
        cwd=str(BUNDLE_DIR),
        timeout=15,
    )
    assert proc.returncode == 0, f"diagnostics doctor failed: {proc.stderr}"
    assert "MLX Observability Pipeline Doctor" in proc.stdout
    assert "[Stage 1/5] Central TSDB Ingestion" in proc.stdout
    assert "[Stage 2/5] Edge Exporters & Nodes" in proc.stdout
    assert "[Stage 3/5] Alerting & Rule Engine" in proc.stdout
    assert "[Stage 4/5] OTel Logs & Opik Traces" in proc.stdout
    assert "[Stage 5/5] Grafana & Query Pipeline" in proc.stdout


def test_diagnostics_review_central_command():
    """Verify review-central audits all central services."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    proc = subprocess.run(
        [sys.executable, str(diag_script), "review-central"],
        capture_output=True,
        text=True,
        cwd=str(BUNDLE_DIR),
        timeout=15,
    )
    assert proc.returncode == 0, f"review-central failed: {proc.stderr}"
    assert "Central Observability Stack Health & Config Audit" in proc.stdout
    assert "VictoriaMetrics" in proc.stdout
    assert "Grafana" in proc.stdout
    assert "Alertmanager" in proc.stdout


def test_diagnostics_review_edge_command():
    """Verify review-edge audits edge nodes and exporters."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    proc = subprocess.run(
        [sys.executable, str(diag_script), "review-edge"],
        capture_output=True,
        text=True,
        cwd=str(BUNDLE_DIR),
        timeout=15,
    )
    assert proc.returncode == 0, f"review-edge failed: {proc.stderr}"
    assert "Edge Monitoring Stack Health & Config Audit" in proc.stdout
    assert "mlx-proxy" in proc.stdout
    assert "mlx-hw" in proc.stdout
    assert "mlx-server" in proc.stdout


def test_diagnostics_granular_service_reviews():
    """Verify granular review commands for VM, Alertmanager, vmalert, OTel, and Opik."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    subcommands = [
        ("review-vm", "VictoriaMetrics TSDB Configuration & Health"),
        ("review-alertmanager", "Alertmanager Configuration & Health"),
        ("review-vmalert", "vmalert Configuration & Health"),
        ("review-otel", "OpenTelemetry Collector Configuration & Health"),
        ("review-opik", "Opik Tracing Suite Health & Config"),
    ]
    for subcmd, expected_header in subcommands:
        proc = subprocess.run(
            [sys.executable, str(diag_script), subcmd],
            capture_output=True,
            text=True,
            cwd=str(BUNDLE_DIR),
            timeout=15,
        )
        if subcmd == "review-opik":
            # Opik is an optional compose profile (--opik)
            assert proc.returncode in (0, 1), f"{subcmd} failed unexpectedly: {proc.stderr}"
        else:
            assert proc.returncode == 0, f"{subcmd} failed: {proc.stderr}"
        assert expected_header in proc.stdout, f"{subcmd} missing header {expected_header}"


def test_deploy_sh_cli_dispatcher_doctor():
    """Verify deploy.sh doctor CLI subcommand dispatches to diagnostics.py."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    env = dict(os.environ, SKIP_GIT_PULL="1")
    proc = subprocess.run(
        [str(deploy_sh), "doctor"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"deploy.sh doctor failed: {proc.stderr}"
    assert "MLX Observability Pipeline Doctor" in proc.stdout
    assert "ALL PIPELINE GATES PASSED" in proc.stdout


def test_deploy_sh_clean_reinstall_cli_help():
    """Verify clean and reinstall help screens exist and document flags."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    env = dict(os.environ, SKIP_GIT_PULL="1")

    for cmd in ["clean", "reinstall"]:
        proc = subprocess.run(
            [str(deploy_sh), cmd, "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 0, f"deploy.sh {cmd} --help failed: {proc.stderr}"
        out = proc.stderr + proc.stdout
        assert f"{cmd} [target] [options]" in out, f"Missing usage in {cmd} help"
        assert "--reuse-config" in out, f"Missing --reuse-config in {cmd} help"
        assert "--recreate-config" in out, f"Missing --recreate-config in {cmd} help"


def test_deploy_sh_clean_reinstall_non_interactive_guard():
    """Verify clean and reinstall require explicit -y or confirmation in non-interactive mode."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    env = dict(os.environ, SKIP_GIT_PULL="1")

    for cmd in ["clean", "reinstall"]:
        proc = subprocess.run(
            [str(deploy_sh), cmd],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=10,
        )
        assert proc.returncode != 0, f"deploy.sh {cmd} should fail when unconfirmed non-interactively"
        assert "confirmation required" in proc.stderr or "aborted" in proc.stderr


def test_clean_edge_unit_reuse_and_recreate(tmp_path):
    """Verify clean_edge preserves config in reuse mode and backs up in recreate mode."""
    import subprocess
    edge_dir = tmp_path / "edge"
    edge_dir.mkdir(parents=True)
    logs_dir = edge_dir / "logs"
    logs_dir.mkdir()
    bin_dir = edge_dir / "bin"
    bin_dir.mkdir()
    cfg_dir = edge_dir / "config"
    cfg_dir.mkdir()

    # Create dummy files
    (logs_dir / "test.log").write_text("dummy log")
    (logs_dir / "vmagent.yml").write_text("dummy yaml")
    (bin_dir / "dummy.pid").write_text("99999999")
    (cfg_dir / "nodes.yml").write_text("nodes: dummy")
    (cfg_dir / "edge.env").write_text("ENV=dummy")

    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"

    # Test reuse mode
    script_reuse = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    EDGE_DIR="{edge_dir}"
    ENV_FILE="{cfg_dir / 'edge.env'}"
    clean_edge reuse
    """
    res = subprocess.run(["bash", "-c", script_reuse], capture_output=True, text=True)
    assert res.returncode == 0, f"clean_edge reuse failed: {res.stderr}"
    assert not (logs_dir / "test.log").exists()
    assert not (logs_dir / "vmagent.yml").exists()
    assert not (bin_dir / "dummy.pid").exists()
    assert (cfg_dir / "nodes.yml").exists(), "nodes.yml should be preserved in reuse mode"
    assert (cfg_dir / "edge.env").exists(), "edge.env should be preserved in reuse mode"

    # Test recreate mode
    (logs_dir / "new.log").write_text("new log")
    script_recreate = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    EDGE_DIR="{edge_dir}"
    ENV_FILE="{cfg_dir / 'edge.env'}"
    clean_edge recreate
    """
    res = subprocess.run(["bash", "-c", script_recreate], capture_output=True, text=True)
    assert res.returncode == 0, f"clean_edge recreate failed: {res.stderr}"
    assert not (logs_dir / "new.log").exists()
    assert not (cfg_dir / "nodes.yml").exists(), "nodes.yml should be deleted in recreate mode"
    assert not (cfg_dir / "edge.env").exists(), "edge.env should be deleted in recreate mode"

    # Verify backup files were created with IST timestamp suffix
    nodes_baks = list(cfg_dir.glob("nodes.yml.bak.*_IST"))
    env_baks = list(cfg_dir.glob("edge.env.bak.*_IST"))
    assert len(nodes_baks) == 1, f"Expected 1 nodes.yml backup, found {nodes_baks}"
    assert len(env_baks) == 1, f"Expected 1 edge.env backup, found {env_baks}"
    assert re.match(r"^nodes\.yml\.bak\.\d{8}_\d{6}_IST$", nodes_baks[0].name)
    assert re.match(r"^edge\.env\.bak\.\d{8}_\d{6}_IST$", env_baks[0].name)


def test_clean_central_unit_reuse_and_recreate(tmp_path):
    """Verify clean_central preserves .env in reuse mode and backs up in recreate mode."""
    import subprocess
    central_dir = tmp_path / "central"
    central_dir.mkdir(parents=True)
    cfg_dir = central_dir / "config"
    cfg_dir.mkdir()

    env_file = central_dir / ".env"
    env_file.write_text("GRAFANA_ADMIN_PASSWORD=secret\n")
    am_file = cfg_dir / "alertmanager.yml"
    am_file.write_text("rendered: true\n")

    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"

    # Test reuse mode (mocking engine to none to avoid touching docker daemon in unit test)
    script_reuse = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    CENTRAL_DIR="{central_dir}"
    docker() {{ return 1; }}
    podman() {{ return 1; }}
    clean_central reuse
    """
    res = subprocess.run(["bash", "-c", script_reuse], capture_output=True, text=True)
    assert res.returncode == 0, f"clean_central reuse failed: {res.stderr}"
    assert env_file.exists(), ".env should be preserved in reuse mode"

    # Test recreate mode
    script_recreate = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    CENTRAL_DIR="{central_dir}"
    docker() {{ return 1; }}
    podman() {{ return 1; }}
    clean_central recreate
    """
    res = subprocess.run(["bash", "-c", script_recreate], capture_output=True, text=True)
    assert res.returncode == 0, f"clean_central recreate failed: {res.stderr}"
    assert not env_file.exists(), ".env should be deleted in recreate mode"
    assert not am_file.exists(), "alertmanager.yml should be deleted in recreate mode"

    baks = list(central_dir.glob(".env.bak.*_IST"))
    assert len(baks) == 1, f"Expected 1 .env backup, found {baks}"
    assert re.match(r"^\.env\.bak\.\d{8}_\d{6}_IST$", baks[0].name)


def test_reinstall_cli_aliases_and_flags():
    """Verify reinstall-reuse and reinstall-recreate CLI subcommands dispatch."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    env = dict(os.environ, SKIP_GIT_PULL="1")

    for alias in ["reinstall-reuse", "reinstall-recreate"]:
        proc = subprocess.run(
            [str(deploy_sh), alias, "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=10,
        )
        assert proc.returncode == 0, f"deploy.sh {alias} --help failed: {proc.stderr}"
        out = proc.stderr + proc.stdout
        assert "Usage: ./scripts/deploy.sh reinstall" in out


def test_compose_central_resource_limits():
    """Verify all services in docker-compose.central.yml have memory resource limits."""
    compose_path = BUNDLE_DIR / "central" / "docker-compose.central.yml"
    assert compose_path.is_file(), "docker-compose.central.yml must exist"
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = doc.get("services", {})
    assert len(services) >= 10, "Expected at least 10 services in central stack"

    for sname, scfg in services.items():
        # One-shot init containers don't need persistent limits
        if sname in ("clickhouse-init", "opik-init-bucket"):
            continue
        # Verify memory limit is set under deploy.resources.limits.memory or mem_limit
        mem_limit = None
        deploy_cfg = scfg.get("deploy", {})
        if isinstance(deploy_cfg, dict):
            limits = deploy_cfg.get("resources", {}).get("limits", {})
            if isinstance(limits, dict):
                mem_limit = limits.get("memory")
        if not mem_limit:
            mem_limit = scfg.get("mem_limit")
        assert mem_limit is not None, f"Service '{sname}' must have a memory limit configured"

    # Specific bounds on memory-heavy services
    ch_limits = services["clickhouse"]["deploy"]["resources"]["limits"]["memory"]
    assert "M" in ch_limits or "G" in ch_limits
    mysql_limits = services["mysql"]["deploy"]["resources"]["limits"]["memory"]
    assert "M" in mysql_limits or "G" in mysql_limits
    backend_limits = services["backend"]["deploy"]["resources"]["limits"]["memory"]
    assert "M" in backend_limits or "G" in backend_limits


def test_grafana_datasource_url_not_empty_or_broken_syntax():
    """Verify datasources.yaml has valid URL and does not contain broken ':-' syntax."""
    ds_path = BUNDLE_DIR / "central" / "config" / "grafana" / "datasources.yaml"
    assert ds_path.is_file(), "datasources.yaml must exist"
    raw_text = ds_path.read_text(encoding="utf-8")
    assert ":-" not in raw_text, "datasources.yaml must not contain unsupported ':-' default expansion syntax"

    doc = yaml.safe_load(raw_text)
    datasources = doc.get("datasources", [])
    assert len(datasources) >= 1
    vm_ds = datasources[0]
    assert vm_ds["name"] == "VictoriaMetrics"
    assert vm_ds["url"], "Datasource URL must not be empty"


def test_podman_machine_size_check_logic():
    """Verify _check_podman_machine_size enforces 2GB for central and 4GB for opik."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"

    # 1. 2048 MB with Opik should fail
    script_2g_opik = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    _get_podman_machine_memory_mb() {{ echo 2048; }}
    _check_podman_machine_size 1 0
    """
    res = subprocess.run(["bash", "-c", script_2g_opik], capture_output=True, text=True)
    assert res.returncode != 0, "2048 MB should fail for Opik"
    assert "requires at least 4096 MB" in res.stderr

    # 2. 4096 MB with Opik should succeed
    script_4g_opik = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    _get_podman_machine_memory_mb() {{ echo 4096; }}
    _check_podman_machine_size 1 0
    """
    res = subprocess.run(["bash", "-c", script_4g_opik], capture_output=True, text=True)
    assert res.returncode == 0, f"4096 MB should pass for Opik: {res.stderr}"

    # 3. 2048 MB without Opik should succeed
    script_2g_std = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    _get_podman_machine_memory_mb() {{ echo 2048; }}
    _check_podman_machine_size 0 0
    """
    res = subprocess.run(["bash", "-c", script_2g_std], capture_output=True, text=True)
    assert res.returncode == 0, f"2048 MB should pass for standard central: {res.stderr}"

    # 4. 1024 MB without Opik should fail
    script_1g_std = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    _get_podman_machine_memory_mb() {{ echo 1024; }}
    _check_podman_machine_size 0 0
    """
    res = subprocess.run(["bash", "-c", script_1g_std], capture_output=True, text=True)
    assert res.returncode != 0, "1024 MB should fail for standard central"


def test_clean_central_container_names_cover_compose():
    """Verify clean_central in deploy.sh cleans all container_name values in docker-compose.central.yml."""
    compose_path = BUNDLE_DIR / "central" / "docker-compose.central.yml"
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose_containers = set()
    for sname, scfg in doc.get("services", {}).items():
        cname = scfg.get("container_name")
        if cname:
            compose_containers.add(cname)

    assert len(compose_containers) >= 10, "Expected at least 10 named containers in compose"

    deploy_sh_text = (BUNDLE_DIR / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    for cname in compose_containers:
        assert f'"{cname}"' in deploy_sh_text, f"Container '{cname}' from compose missing in clean_central cleanup array"


def test_podman_machine_memory_unit_conversion():
    """Verify _get_podman_machine_memory_mb handles unit string conversions from podman machine list."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"

    script = f"""
    export SKIP_GIT_PULL=1
    source "{deploy_sh}"
    podman() {{
        if [[ "$1" == "machine" && "$2" == "inspect" ]]; then
            return 1
        fi
        if [[ "$1" == "machine" && "$2" == "list" ]]; then
            echo "2GiB"
            return 0
        fi
        return 1
    }}
    _get_podman_machine_memory_mb
    """
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert res.returncode == 0
    assert res.stdout.strip() == "2048", f"Expected 2048 MB from 2GiB, got '{res.stdout.strip()}'"


def _run_deploy_bash(bash_body: str, env: dict | None = None):
    """Run a bash snippet with deploy.sh sourced (SKIP_GIT_PULL=1)."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    script = f'export SKIP_GIT_PULL=1\nsource "{deploy_sh}"\n{bash_body}\n'
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=run_env)


def test_podman_compose_provider_in_pod_flag_detection(tmp_path):
    """Verify _resolve_central_compose sets IS_PODMAN_COMPOSE and COMPOSE_CMD across providers."""
    # Scenario A: podman compose delegates to external podman-compose provider -> IS_PODMAN_COMPOSE=1
    res_a = _run_deploy_bash(
        """
        podman() {
            if [[ "$1" == "compose" && "$2" == "version" ]]; then
                echo '>>>> Executing external compose provider "/opt/homebrew/bin/podman-compose". <<<<'
                echo 'podman-compose version: 1.2.0'
                return 0
            fi
            return 1
        }
        COMPOSE_CMD=()
        IS_PODMAN_COMPOSE=0
        _resolve_central_compose "podman"
        echo "CMD=${COMPOSE_CMD[*]}|FLAG=${IS_PODMAN_COMPOSE}"
        """
    )
    assert res_a.returncode == 0, res_a.stderr
    assert res_a.stdout.strip() == "CMD=podman compose|FLAG=1"

    # Scenario B: podman compose delegates to docker-compose without podman-compose -> rejected
    res_b = _run_deploy_bash(
        """
        podman() {
            if [[ "$1" == "compose" && "$2" == "version" ]]; then
                echo '>>>> Executing external compose provider "/usr/local/bin/docker-compose". Please see podman-compose(1) for how to disable this message. <<<<'
                echo 'Docker Compose version v2.32.2'
                return 0
            fi
            return 1
        }
        COMPOSE_CMD=()
        IS_PODMAN_COMPOSE=0
        _resolve_central_compose "podman"
        """
    )
    assert res_b.returncode != 0
    assert "podman-compose is required" in res_b.stderr

    # Scenario C: podman compose version fails, standalone podman-compose exists on PATH -> fallback
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    pc_bin = fake_bin / "podman-compose"
    pc_bin.write_text("#!/usr/bin/env bash\necho 'podman-compose 1.2.0'\n", encoding="utf-8")
    pc_bin.chmod(0o755)
    res_c = _run_deploy_bash(
        f"""
        export PATH="{fake_bin}:$PATH"
        podman() {{
            if [[ "$1" == "compose" && "$2" == "version" ]]; then
                return 1
            fi
            return 1
        }}
        COMPOSE_CMD=()
        IS_PODMAN_COMPOSE=0
        _resolve_central_compose "podman"
        echo "CMD=${{COMPOSE_CMD[*]}}|FLAG=${{IS_PODMAN_COMPOSE}}"
        """
    )
    assert res_c.returncode == 0, res_c.stderr
    assert res_c.stdout.strip() == "CMD=podman-compose|FLAG=1"

    # Scenario D: docker engine -> IS_PODMAN_COMPOSE=0
    res_d = _run_deploy_bash(
        """
        COMPOSE_CMD=()
        IS_PODMAN_COMPOSE=0
        _resolve_central_compose "docker"
        echo "CMD=${COMPOSE_CMD[*]}|FLAG=${IS_PODMAN_COMPOSE}"
        """
    )
    assert res_d.returncode == 0, res_d.stderr
    assert res_d.stdout.strip() == "CMD=docker compose|FLAG=0"


def test_remove_orphan_central_pods_cleans_named_and_filtered_pods(tmp_path):
    """Verify _remove_orphan_central_pods removes filtered pods and legacy pod_mlx-central/pod_central names."""
    log_file = tmp_path / "podman_rm.log"
    res = _run_deploy_bash(
        f"""
        podman() {{
            if [[ "$1" == "pod" && "$2" == "ps" ]]; then
                echo "abc123def456"
                return 0
            fi
            if [[ "$1" == "pod" && "$2" == "exists" ]]; then
                [[ "$3" == "pod_mlx-central" || "$3" == "pod_central" ]] && return 0
                return 1
            fi
            if [[ "$1" == "pod" && "$2" == "rm" ]]; then
                echo "RM:$*" >> "{log_file}"
                return 0
            fi
            return 1
        }}
        _remove_orphan_central_pods "docker"
        _remove_orphan_central_pods "podman"
        """
    )
    assert res.returncode == 0, res.stderr
    rm_lines = log_file.read_text(encoding="utf-8").splitlines()
    assert "RM:pod rm -f abc123def456" in rm_lines
    assert "RM:pod rm -f pod_mlx-central" in rm_lines
    assert "RM:pod rm -f pod_central" in rm_lines
    assert len(rm_lines) == 3


def test_write_and_ensure_central_env_preserve_custom_keys_and_perms(tmp_path):
    """Verify _write_central_env and ensure_central_env keep custom keys, set defaults, and apply 0600 perms."""
    import stat

    central_dir = tmp_path / "central"
    central_dir.mkdir()
    env_file = central_dir / ".env"
    env_file.write_text(
        "# old comment\n"
        "GRAFANA_ADMIN_USER=opsadmin\n"
        "GRAFANA_ADMIN_PASSWORD=StrongTestPass99\n"
        "GRAFANA_ANONYMOUS_ENABLED=true\n"
        "OPIK_VERSION=2.3.0\n"
        "ONCALL_WEBHOOK_URL=https://hooks.example.com/alert\n",
        encoding="utf-8",
    )

    res = _run_deploy_bash(
        f"""
        CENTRAL_DIR="{central_dir}"
        ensure_central_env
        echo "EXPORTED_OPIK=${{OPIK_VERSION:-}}|WEBHOOK=${{ONCALL_WEBHOOK_URL:-}}|VM=${{VICTORIAMETRICS_URL:-}}"
        """
    )
    assert res.returncode == 0, res.stderr
    assert "EXPORTED_OPIK=2.3.0|WEBHOOK=https://hooks.example.com/alert|VM=http://victoria-metrics:8428" in res.stdout

    updated = env_file.read_text(encoding="utf-8")
    assert "IST" in updated
    assert "GRAFANA_ADMIN_USER=opsadmin" in updated
    assert "GRAFANA_ADMIN_PASSWORD=StrongTestPass99" in updated
    assert "GRAFANA_ANONYMOUS_ENABLED=true" in updated
    assert "VICTORIAMETRICS_URL=http://victoria-metrics:8428" in updated
    assert "OPIK_VERSION=2.3.0" in updated
    assert "ONCALL_WEBHOOK_URL=https://hooks.example.com/alert" in updated
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_cmd_central_passes_in_pod_false_cleans_pods_and_reports_compose_exit_code(tmp_path):
    """Verify cmd_central cleans pods, passes --in-pod=false to podman-compose, and reports real compose errors."""
    central_dir = tmp_path / "central"
    central_dir.mkdir()
    (central_dir / "docker-compose.central.yml").write_text(
        "name: mlx-central\nservices:\n  victoriametrics:\n    image: victoriametrics/victoria-metrics:latest\n",
        encoding="utf-8",
    )
    (central_dir / ".env").write_text(
        "GRAFANA_ADMIN_USER=admin\nGRAFANA_ADMIN_PASSWORD=StrongPass42\nVICTORIAMETRICS_URL=http://victoria-metrics:8428\n",
        encoding="utf-8",
    )

    log_file = tmp_path / "podman.log"
    res_ok = _run_deploy_bash(
        f"""
        CENTRAL_DIR="{central_dir}"
        _detect_container_engine() {{ echo "podman"; }}
        _check_podman_machine_size() {{ return 0; }}
        render_alertmanager_config() {{ return 0; }}
        render_grafana_datasource_config() {{ return 0; }}
        podman() {{
            echo "$*" >> "{log_file}"
            if [[ "$1" == "compose" && "$2" == "version" ]]; then
                echo '>>>> Executing external compose provider "/opt/homebrew/bin/podman-compose". <<<<'
                echo 'podman-compose version: 1.2.0'
                return 0
            fi
            if [[ "$1" == "pod" && "$2" == "ps" ]]; then
                echo "deadbeefpod1"
                return 0
            fi
            if [[ "$1" == "pod" && "$2" == "exists" ]]; then
                [[ "$3" == "pod_mlx-central" ]] && return 0
                return 1
            fi
            if [[ "$1" == "compose" ]]; then
                if [[ "$*" == *" config --images"* ]]; then
                    echo '>>>> Executing external compose provider "/opt/homebrew/bin/podman-compose". <<<<'
                    echo 'victoriametrics/victoria-metrics:latest'
                    return 0
                fi
                return 0
            fi
            if [[ "$1" == "image" && "$2" == "inspect" ]]; then
                return 0
            fi
            if [[ "$1" == "ps" ]]; then
                return 0
            fi
            return 0
        }}
        cmd_central --yes
        """,
        env={"NO_PROXY": ""},
    )
    assert res_ok.returncode == 0, res_ok.stderr
    calls = log_file.read_text(encoding="utf-8").splitlines()
    assert "pod rm -f deadbeefpod1" in calls
    assert "pod rm -f pod_mlx-central" in calls
    up_calls = [c for c in calls if c.startswith("compose ") and " up -d" in c]
    assert len(up_calls) == 1
    assert "--in-pod=false" in up_calls[0]
    assert "--force-recreate" in up_calls[0]

    # Verify non-zero compose exit code is reported accurately (not masked to 0 by `if ! ...`)
    res_fail = _run_deploy_bash(
        f"""
        CENTRAL_DIR="{central_dir}"
        _detect_container_engine() {{ echo "podman"; }}
        _check_podman_machine_size() {{ return 0; }}
        render_alertmanager_config() {{ return 0; }}
        render_grafana_datasource_config() {{ return 0; }}
        podman() {{
            if [[ "$1" == "compose" && "$2" == "version" ]]; then
                echo 'podman-compose version: 1.2.0'
                return 0
            fi
            if [[ "$1" == "compose" && "$*" == *" config --images"* ]]; then
                echo 'victoriametrics/victoria-metrics:latest'
                return 0
            fi
            if [[ "$1" == "compose" && "$*" == *" up -d"* ]]; then
                return 42
            fi
            if [[ "$1" == "image" && "$2" == "inspect" ]]; then
                return 0
            fi
            return 0
        }}
        cmd_central --yes
        """
    )
    assert res_fail.returncode != 0
    assert "failed with exit code 42" in res_fail.stderr


def test_process_telemetry_metrics_sampling():
    """Verify mlx_process_telemetry samples top-10 consumers and registers Prometheus metrics."""
    import mlx_process_telemetry
    from prometheus_client import generate_latest

    # Run sampling round
    mlx_process_telemetry._sample_processes("test-node-42", top_k=10)

    output = generate_latest(mlx_process_telemetry.REGISTRY).decode("utf-8")
    assert 'process_profiler_up{device="test-node-42"} 1.0' in output
    assert "process_cpu_percent" in output
    assert "process_memory_rss_bytes" in output
    assert "process_disk_io_bytes_sec" in output
    assert "process_network_io_bytes_sec" in output
    assert "process_ranking_info" in output


def test_sbom_collector_inventory_and_drift():
    """Verify mlx_sbom_collector builds catalog, computes drift, and exposes Prometheus metrics."""
    import mlx_sbom_collector
    from prometheus_client import generate_latest

    mlx_sbom_collector.collect_and_update_metrics("target-mac", "base-mac")

    output = generate_latest(mlx_sbom_collector.REGISTRY).decode("utf-8")
    assert 'mac_sbom_collector_up{device="target-mac"} 1.0' in output
    assert "mac_sbom_package_info" in output
    assert "mac_sbom_total_packages" in output
    assert 'mac_sbom_drift_status{base="base-mac",device="target-mac"' in output

    snap_file = mlx_sbom_collector.LOG_DIR / "sbom_snapshot.json"
    assert snap_file.is_file()
    snap_data = json.loads(snap_file.read_text(encoding="utf-8"))
    assert snap_data["device"] == "target-mac"
    assert snap_data["macbook"] == "target-mac"
    assert "scanned_at_ist" in snap_data
    assert snap_data["scanned_at_ist"].endswith(" IST")
    assert snap_data["total_packages"] > 0


def test_dashboards_json_validity():
    """Verify all 17 production dashboards parse cleanly, contain deterministic UIDs, and have header links."""
    dashboards_dir = BUNDLE_DIR / "central" / "dashboards"
    expected = {
        "alerts.json": "mlx-alerts",
        "detail.json": "mlx-detail",
        "device-health.json": "mlx-device-health",
        "hardware.json": "mlx-hardware",
        "inference.json": "mlx-inference",
        "kv-cache.json": "mlx-kv-cache",
        "logs.json": "mlx-logs",
        "network.json": "mlx-network",
        "opik.json": "mlx-opik",
        "overview.json": "mlx-overview",
        "power-thermal.json": "mlx-power-thermal",
        "process-profiler.json": "mlx-process-profiler",
        "quality.json": "mlx-quality",
        "sbom-drift.json": "mlx-sbom-drift",
        "sbom-inventory.json": "mlx-sbom-inventory",
        "serving.json": "mlx-serving",
        "slo.json": "mlx-slo",
    }
    for fname, expected_uid in expected.items():
        fpath = dashboards_dir / fname
        assert fpath.is_file(), f"Missing dashboard file {fname}"
        data = json.loads(fpath.read_text(encoding="utf-8"))
        assert data.get("uid") == expected_uid
        assert data.get("title")
        assert len(data.get("panels", [])) > 0
        assert len(data.get("links", [])) > 0, f"Dashboard {fname} missing header drill-down links"
        assert "mlx" in data.get("tags", []), f"Dashboard {fname} missing 'mlx' tag for dynamic discovery"


def test_passive_server_observer_never_spawns_and_tracks_restarts(tmp_path, monkeypatch):
    """Verify mlx_server_supervisor.py is strictly passive and tracks PID-change restarts."""
    import argparse
    import mlx_server_supervisor as sup

    source = (EDGE_DIR / "exporters" / "mlx_server_supervisor.py").read_text(encoding="utf-8")
    assert "subprocess.Popen" not in source
    assert "os.kill" not in source

    log_file = tmp_path / "server.log"
    log_file.write_text("[METAL] Command buffer execution failed\n", encoding="utf-8")

    args = argparse.Namespace(
        listen="127.0.0.1:9105",
        health="http://127.0.0.1:11234/v1/models",
        model="Qwen3.5-4B",
        server_log=str(log_file),
        process_pattern="mlx_lm.server",
        launchd_label="",
        systemd_unit="",
        probe_interval=5.0,
    )
    obs = sup.PassiveServerObserver(args)

    # 1. Process alive (pid 101) and serving Qwen3.5-4B
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: (101, 12.0))
    monkeypatch.setattr(obs, "_probe_health", lambda: (True, ["Qwen3.5-4B"]))
    obs.poll_once()
    assert sup.G_UP._value.get() == 1
    assert sup.G_READY._value.get() == 1
    assert sup.G_STATE._value.get() == 1
    assert sup.G_MODEL.labels(model="Qwen3.5-4B")._value.get() == 1

    # 2. External supervisor restarted process (new pid 202), not yet ready
    restarts_before = sup.G_RESTARTS._value.get()
    crashes_before = sup.G_CRASHES.labels(reason="metal_gpu_error")._value.get()
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: (202, 1.0))
    monkeypatch.setattr(obs, "_probe_health", lambda: (False, []))
    obs.poll_once()
    assert sup.G_RESTARTS._value.get() == restarts_before + 1
    assert sup.G_CRASHES.labels(reason="metal_gpu_error")._value.get() == crashes_before + 1
    assert sup.G_UP._value.get() == 1
    assert sup.G_READY._value.get() == 0
    assert sup.G_STATE._value.get() == 2




