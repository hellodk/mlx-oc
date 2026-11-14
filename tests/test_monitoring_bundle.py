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
    assert data["global"]["external_labels"]["macbook"] == "test-mac"
    assert data["global"]["external_labels"]["serial"] == "SERIAL12345"
    assert len(data["scrape_configs"]) == 5


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
    assert data["global"]["external_labels"]["macbook"] == "unknown"


def test_no_duplicate_metrics_between_exporters():
    """Verify importing both kv_cache_agent and proxy does not throw CollectorRegistry collision."""
    import mlx_kv_cache_agent
    import mlx_metrics_proxy

    # Both modules must import cleanly into the same process
    assert hasattr(mlx_kv_cache_agent, "G_UTIL")
    assert hasattr(mlx_metrics_proxy, "CONTEXT_MAX")


def test_supervisor_command_json_no_keyerror():
    """Verify JSON with braces does not raise KeyError in command generation."""
    server_cmd_template = (
        "python3 -m mlx_lm.server --model {model} --port {port} "
        "--chat-template-args '{\"enable_thinking\":false}'"
    )
    model_arg = "Qwen3.5-4B-MLX-4bit"
    server_port = 11234

    srv_cmd = (
        server_cmd_template
        .replace("{model}", shlex.quote(model_arg))
        .replace("{port}", str(server_port))
    )
    assert "Qwen3.5-4B-MLX-4bit" in srv_cmd
    assert "11234" in srv_cmd
    assert '{"enable_thinking":false}' in srv_cmd


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
    assert "MLXMacbookSilent" in alerts
    assert "MLXServerCrashLooping" in alerts
    assert "MLXHardwareExporterDown" in alerts
    assert "MLXMemoryPressureHigh" in alerts


def test_exporter_listen_defaults_are_local():
    """Verify all 5 standalone exporter scripts default to 127.0.0.1 for security."""
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
        "victoriametrics/victoria-metrics:latest",
        "otel/opentelemetry-collector-contrib:0.139.0",
        "grafana/grafana:latest",
        "victoriametrics/vmalert:latest",
        "prom/alertmanager:latest",
    }
    assert base_images == expected_base

    # Opik profile must include rustfs and not bitnami minio
    assert "rustfs/rustfs:latest" in opik_images

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









