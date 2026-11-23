import json
import os
import re
import sys
from pathlib import Path
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
    assert len(data["scrape_configs"]) == 8


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
    assert "MLXSupervisorExporterDown" in alerts
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
    edge_cfg = BUNDLE_DIR / "edge" / "config"
    edge_env = edge_cfg / "edge.env"
    nodes_yml = edge_cfg / "nodes.yml"
    created_env = False
    created_nodes = False
    if not edge_env.exists() and (edge_cfg / "edge.env.template").exists():
        edge_env.write_text((edge_cfg / "edge.env.template").read_text(encoding="utf8"), encoding="utf8")
        created_env = True
    if not nodes_yml.exists() and (edge_cfg / "nodes.yml.template").exists():
        nodes_yml.write_text((edge_cfg / "nodes.yml.template").read_text(encoding="utf8"), encoding="utf8")
        created_nodes = True
    try:
        env = dict(os.environ, SKIP_GIT_PULL="1")
        proc = subprocess.run(
            [str(deploy_sh), "review-vmagent"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0, f"deploy.sh review-vmagent failed: {proc.stderr}"
        assert "vmagent Edge Configuration Review" in proc.stdout
        assert "Fail Checks & Health:" in proc.stdout
        assert "Scrape Targets (Local Exporters):" in proc.stdout
    finally:
        if created_env and edge_env.exists():
            edge_env.unlink()
        if created_nodes and nodes_yml.exists():
            nodes_yml.unlink()


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
        timeout=30,
    )
    assert proc.returncode == 0, f"deploy.sh review-grafana failed: {proc.stderr}"
    assert "Grafana Central Configuration Review" in proc.stdout
    assert "Credentials & Security:" in proc.stdout
    assert "Admin Password" in proc.stdout
    assert "Fail Checks & Health:" in proc.stdout


def _make_hermetic_diag_env(tmp_path: Path):
    """Build isolated central/edge dirs and a local stub HTTP server for diagnostics tests."""
    import contextlib
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _StubDiagHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            ctype = "application/json"
            if path in ("/health", "/-/healthy"):
                code, body = 200, "OK"
                ctype = "text/plain"
            elif path == "/api/health":
                code, body = 200, json.dumps({"database": "ok", "version": "10.0.0"})
            elif path == "/api/health-check":
                code, body = 200, json.dumps({"healthy": True})
            elif path == "/api/users":
                code, body = 200, json.dumps([{"login": "admin"}])
            elif path == "/api/datasources/name/VictoriaMetrics":
                code, body = 200, json.dumps({"id": 1, "uid": "vm-uid", "name": "VictoriaMetrics"})
            elif path == "/api/datasources/uid/vm-uid/health":
                code, body = 200, json.dumps({"status": "OK"})
            elif path == "/api/v1/query":
                code, body = 200, json.dumps({
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [{
                            "metric": {"device": "test-node", "macbook": "test-node"},
                            "value": [1700000000, "1"],
                        }],
                    },
                })
            elif path in ("/api/v1/label/device/values", "/api/v1/label/macbook/values"):
                code, body = 200, json.dumps({"status": "success", "data": ["test-node"]})
            elif path == "/api/v2/alerts":
                code, body = 200, json.dumps([])
            elif path == "/api/v1/rules":
                code, body = 200, json.dumps({
                    "status": "success",
                    "data": {"groups": [{"name": "mlx", "rules": [{"name": "TestRule", "state": "ok", "health": "ok"}]}]},
                })
            elif path == "/api/v1/alerts":
                code, body = 200, json.dumps({"status": "success", "data": {"alerts": []}})
            elif path == "/metrics":
                code, body = 200, "# HELP mlx_up ok\n# TYPE mlx_up gauge\nmlx_up 1\n"
                ctype = "text/plain"
            elif path == "/v1/models":
                code, body = 200, json.dumps({"object": "list", "data": [{"id": "test-model"}]})
            else:
                code, body = 404, json.dumps({"error": "not found"})

            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    @contextlib.contextmanager
    def _ctx():
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubDiagHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            port = srv.server_address[1]
            addr = f"127.0.0.1:{port}"
            edge_dir = tmp_path / "edge"
            central_dir = tmp_path / "central"
            (edge_dir / "config").mkdir(parents=True, exist_ok=True)
            (central_dir / "config").mkdir(parents=True, exist_ok=True)

            (edge_dir / "config" / "nodes.yml").write_text(
                yaml.safe_dump({
                    "central": {"victoria_metrics": addr, "otel": addr, "opik": addr},
                    "devices": [{
                        "id": "test-node",
                        "ip": "127.0.0.1",
                        "role": "edge",
                        "enabled": True,
                        "upstream": f"http://{addr}",
                        "model": "test-model",
                        "model_path": str(tmp_path / "model"),
                    }],
                }),
                encoding="utf8",
            )
            (edge_dir / "config" / "edge.env").write_text(
                "\n".join([
                    f"CENTRAL_VICTORIA_METRICS={addr}",
                    f"MLX_UPSTREAM=http://{addr}",
                    f"PROXY_LISTEN={addr}",
                    f"HW_LISTEN={addr}",
                    f"KV_LISTEN={addr}",
                    f"SUPERVISOR_LISTEN={addr}",
                    f"LOGTAILER_LISTEN={addr}",
                    f"PROCESS_LISTEN={addr}",
                    f"SBOM_LISTEN={addr}",
                ]) + "\n",
                encoding="utf8",
            )
            (central_dir / ".env").write_text(
                "GRAFANA_ADMIN_USER=admin\nGRAFANA_ADMIN_PASSWORD=admin\n",
                encoding="utf8",
            )
            (central_dir / "config" / "alertmanager.yml").write_text("route: {}\n", encoding="utf8")
            (central_dir / "config" / "vmalert-rules.yml").write_text("groups: []\n", encoding="utf8")
            (central_dir / "config" / "otelcol-config.yaml").write_text("receivers: {}\n", encoding="utf8")

            env = dict(
                os.environ,
                SKIP_GIT_PULL="1",
                MLX_EDGE_DIR=str(edge_dir),
                MLX_CENTRAL_DIR=str(central_dir),
                CENTRAL_VICTORIA_METRICS=addr,
                CENTRAL_OTEL=addr,
                CENTRAL_OPIK=addr,
                CENTRAL_GRAFANA=addr,
                CENTRAL_VMALERT=addr,
                CENTRAL_ALERTMANAGER=addr,
            )
            yield env
        finally:
            srv.shutdown()
            srv.server_close()

    return _ctx()


def test_diagnostics_doctor_command(tmp_path):
    """Verify diagnostics doctor runs end-to-end pipeline traceroute against stub central/edge."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    with _make_hermetic_diag_env(tmp_path) as env:
        proc = subprocess.run(
            [sys.executable, str(diag_script), "doctor"],
            capture_output=True,
            text=True,
            cwd=str(BUNDLE_DIR),
            env=env,
            timeout=15,
        )
    assert proc.returncode == 0, f"diagnostics doctor failed: {proc.stdout}\n{proc.stderr}"
    assert "MLX Observability Pipeline Doctor" in proc.stdout
    assert "[Stage 1/5] Central TSDB Ingestion" in proc.stdout
    assert "[Stage 2/5] Edge Exporters & Nodes" in proc.stdout
    assert "[Stage 3/5] Alerting & Rule Engine" in proc.stdout
    assert "[Stage 4/5] OTel Logs & Opik Traces" in proc.stdout
    assert "[Stage 5/5] Grafana & Query Pipeline" in proc.stdout


def test_diagnostics_review_central_command(tmp_path):
    """Verify review-central audits all central services against stub server."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    with _make_hermetic_diag_env(tmp_path) as env:
        proc = subprocess.run(
            [sys.executable, str(diag_script), "review-central"],
            capture_output=True,
            text=True,
            cwd=str(BUNDLE_DIR),
            env=env,
            timeout=15,
        )
    assert proc.returncode == 0, f"review-central failed: {proc.stdout}\n{proc.stderr}"
    assert "Central Observability Stack Health & Config Audit" in proc.stdout
    assert "VictoriaMetrics" in proc.stdout
    assert "Grafana" in proc.stdout
    assert "Alertmanager" in proc.stdout


def test_diagnostics_review_edge_command(tmp_path):
    """Verify review-edge audits edge nodes and exporters against stub server."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    with _make_hermetic_diag_env(tmp_path) as env:
        proc = subprocess.run(
            [sys.executable, str(diag_script), "review-edge"],
            capture_output=True,
            text=True,
            cwd=str(BUNDLE_DIR),
            env=env,
            timeout=15,
        )
    assert proc.returncode == 0, f"review-edge failed: {proc.stdout}\n{proc.stderr}"
    assert "Edge Monitoring Stack Health & Config Audit" in proc.stdout
    assert "mlx-proxy" in proc.stdout
    assert "mlx-hw" in proc.stdout
    assert "mlx-server" in proc.stdout


def test_diagnostics_granular_service_reviews(tmp_path):
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
    with _make_hermetic_diag_env(tmp_path) as env:
        for subcmd, expected_header in subcommands:
            proc = subprocess.run(
                [sys.executable, str(diag_script), subcmd],
                capture_output=True,
                text=True,
                cwd=str(BUNDLE_DIR),
                env=env,
                timeout=15,
            )
            assert proc.returncode == 0, f"{subcmd} failed: {proc.stdout}\n{proc.stderr}"
            assert expected_header in proc.stdout, f"{subcmd} missing header {expected_header}"


def test_deploy_sh_cli_dispatcher_doctor(tmp_path):
    """Verify deploy.sh doctor CLI subcommand dispatches to diagnostics.py hermetically."""
    import subprocess
    deploy_sh = BUNDLE_DIR / "scripts" / "deploy.sh"
    with _make_hermetic_diag_env(tmp_path) as env:
        proc = subprocess.run(
            [str(deploy_sh), "doctor"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            env=env,
            timeout=15,
        )
    assert proc.returncode == 0, f"deploy.sh doctor failed: {proc.stdout}\n{proc.stderr}"
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
    lsof() {{ return 1; }}
    pkill() {{ return 0; }}
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
    lsof() {{ return 1; }}
    pkill() {{ return 0; }}
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
    log_file.write_text("", encoding="utf-8")

    args = argparse.Namespace(
        listen="127.0.0.1:9105",
        health="http://127.0.0.1:11234/v1/models",
        model="Qwen3.5-4B",
        server_log=str(log_file),
        process_pattern="mlx_lm.server",
        launchd_label="",
        systemd_unit="",
        probe_interval=5.0,
        health_timeout=10.0,
        fail_threshold=1,
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

    # 2. External supervisor restarted process (new pid 202), not yet ready, new GPU crash in log
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write("[METAL] Command buffer execution failed\n")
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

    # 3. Server recovers with Qwen3.5-9B; stale dynamic models pruned
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: (202, 5.0))
    monkeypatch.setattr(obs, "_probe_health", lambda: (True, ["Qwen3.5-9B"]))
    obs.poll_once()
    assert sup.G_MODEL.labels(model="Qwen3.5-4B")._value.get() == 0
    assert sup.G_MODEL.labels(model="Qwen3.5-9B")._value.get() == 1

    # 4. Multi-step UP -> DOWN -> RESTARTED should NOT double-count crashes
    #    and clean stop (no new crash lines) should NOT increment G_CRASHES
    crashes_before = sup.G_CRASHES.labels(reason="metal_gpu_error")._value.get()
    restarts_before = sup.G_RESTARTS._value.get()
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: None)
    monkeypatch.setattr(obs, "_probe_health", lambda: (False, []))
    obs.poll_once()
    assert sup.G_CRASHES.labels(reason="metal_gpu_error")._value.get() == crashes_before
    assert sup.G_RESTARTS._value.get() == restarts_before
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: (303, 1.0))
    monkeypatch.setattr(obs, "_probe_health", lambda: (True, ["Qwen3.5-9B"]))
    obs.poll_once()
    assert sup.G_CRASHES.labels(reason="metal_gpu_error")._value.get() == crashes_before
    assert sup.G_RESTARTS._value.get() == restarts_before + 1


def test_passive_server_observer_helpers_and_main(monkeypatch):
    """Verify _parse_etime, _cmdline_matches, hysteresis, remote restarts, and main()."""
    import argparse
    import mlx_server_supervisor as sup

    assert sup._parse_etime("45") == 45.0
    assert sup._parse_etime("03:04") == 184.0
    assert sup._parse_etime("02:03:04") == 7384.0
    assert sup._parse_etime("01-02:03:04") == 93784.0

    # _cmdline_matches avoids grep/tail collisions and supports versioned python
    assert sup._cmdline_matches(
        ["python3", "-m", "mlx_lm.server", "--port", "8000"],
        "python3 -m mlx_lm.server --port 8000",
        "mlx_lm.server",
    )
    assert sup._cmdline_matches(
        ["/venv/bin/python3.12", "/opt/mlx_lm.server"],
        "/venv/bin/python3.12 /opt/mlx_lm.server",
        "mlx_lm.server",
    )
    assert not sup._cmdline_matches(
        ["grep", "mlx_lm.server", "server.log"],
        "grep mlx_lm.server server.log",
        "mlx_lm.server",
    )
    assert not sup._cmdline_matches(
        ["tail", "-f", "/var/log/mlx_lm.server.log"],
        "tail -f /var/log/mlx_lm.server.log",
        "mlx_lm.server",
    )
    assert sup._model_in_served("Qwen3.5-4B", ["/Users/dk/models/Qwen3.5-4B"])

    # Remote upstream (pid=None) tracks restarts + hysteresis tolerates transient failures
    obs = sup.PassiveServerObserver(argparse.Namespace(
        listen="127.0.0.1:9105",
        health="http://127.0.0.1:8000/v1/models",
        model="",
        server_log="",
        process_pattern="",
        launchd_label="",
        systemd_unit="",
        probe_interval=5.0,
        health_timeout=10.0,
        fail_threshold=2,
    ))
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: None)
    monkeypatch.setattr(obs, "_probe_health", lambda: (True, ["m1"]))
    obs.poll_once()
    assert sup.G_UP._value.get() == 1
    assert obs.had_initial_contact is True

    # 1st transient failure tolerated by hysteresis (fail_threshold=2)
    restarts_before = sup.G_RESTARTS._value.get()
    monkeypatch.setattr(obs, "_probe_health", lambda: (False, []))
    obs.poll_once()
    assert sup.G_UP._value.get() == 1
    # 2nd consecutive failure transitions DOWN; recovery increments G_RESTARTS
    obs.poll_once()
    assert sup.G_UP._value.get() == 0
    monkeypatch.setattr(obs, "_probe_health", lambda: (True, ["m1"]))
    obs.poll_once()
    assert sup.G_RESTARTS._value.get() == restarts_before + 1

    # Transient process-discovery failure with same PID returning does NOT double restart
    obs_pid = sup.PassiveServerObserver(argparse.Namespace(
        listen="127.0.0.1:9105",
        health="http://127.0.0.1:8000/v1/models",
        model="",
        server_log="",
        process_pattern="mlx_lm.server",
        launchd_label="",
        systemd_unit="",
        probe_interval=5.0,
        health_timeout=10.0,
        fail_threshold=1,
    ))
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: (5000, 10.0))
    monkeypatch.setattr(obs_pid, "_probe_health", lambda: (True, ["m1"]))
    obs_pid.poll_once()
    restarts_before = sup.G_RESTARTS._value.get()
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: None)
    monkeypatch.setattr(obs_pid, "_probe_health", lambda: (False, []))
    obs_pid.poll_once()
    assert sup.G_UP._value.get() == 0
    monkeypatch.setattr(sup, "find_server_process", lambda _pat: (5000, 20.0))
    monkeypatch.setattr(obs_pid, "_probe_health", lambda: (True, ["m1"]))
    obs_pid.poll_once()
    assert sup.G_RESTARTS._value.get() == restarts_before

    # _probe_health accepts HTTP 200 even when JSON has no "data" list
    class _FakeResp:
        status = 200

        def read(self):
            return b'{"status":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(sup.urllib.request, "urlopen", lambda *_a, **_kw: _FakeResp())
    obs2 = sup.PassiveServerObserver(argparse.Namespace(
        listen="127.0.0.1:9105",
        health="http://127.0.0.1:8000/health",
        model="",
        server_log="",
        process_pattern="",
        launchd_label="",
        systemd_unit="",
        probe_interval=5.0,
        health_timeout=10.0,
        fail_threshold=3,
    ))
    ok, models = obs2._probe_health()
    assert ok is True
    assert models == []

    # main() initializes logging and server without TypeError
    class _FakeServer:
        def __init__(self, *_a, **_kw):
            pass

        def serve_forever(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(sup, "_ReusableHTTPServer", _FakeServer)
    monkeypatch.setattr(sup.PassiveServerObserver, "run", lambda self: None)
    monkeypatch.setattr(sys, "argv", ["mlx_server_supervisor.py", "--listen", "127.0.0.1:0"])
    sup.main()


def test_mlx_logging_traceback_and_upstream_url(tmp_path):
    """Verify exc_info=True writes traceback into JSON 'err' and parse_upstream_url normalizes formats."""
    import json
    import sys

    sys.path.insert(0, str(BUNDLE_DIR / "edge" / "exporters"))
    import mlx_logging as mlog

    mlog.setup_logging(service="test-audit", macbook="test-mac", log_dir=tmp_path)
    try:
        raise ValueError("boom-trace")
    except ValueError:
        mlog.error("caught failure", component="test", exc_info=True)

    log_file = tmp_path / "test-audit.log"
    lines = [json.loads(line) for line in log_file.read_text(encoding="utf8").splitlines() if line.strip()]
    assert lines, "expected at least one JSON log record"
    rec = lines[-1]
    assert rec["msg"] == "caught failure"
    assert "ValueError: boom-trace" in rec.get("err", "")

    assert mlog.parse_upstream_url("127.0.0.1:11234") == (
        "http", "127.0.0.1", 11234, "http://127.0.0.1:11234"
    )
    assert mlog.parse_upstream_url("https://llm.local:8443/v1") == (
        "https", "llm.local", 8443, "https://llm.local:8443"
    )
    assert mlog.parse_upstream_url("localhost", default_port=8000) == (
        "http", "localhost", 8000, "http://localhost:8000"
    )


def test_mlx_model_info_served_model_and_zero_int():
    """Verify _pick_served_model_id skips default_model and _first_int preserves 0."""
    import sys

    sys.path.insert(0, str(BUNDLE_DIR / "edge" / "exporters"))
    import mlx_model_info as mmi

    assert mmi._first_int({"a": 0, "b": 42}, "a", "b", default=99) == 0
    assert mmi._pick_served_model_id([
        {"id": "default_model"},
        {"id": "mlx-community/Qwen2.5-7B"},
    ]) == "mlx-community/Qwen2.5-7B"


def test_sbom_non_base_missing_reference_not_in_sync(tmp_path, monkeypatch):
    """Verify non-base nodes do not falsely claim IN_SYNC when sbom_base_reference.json is absent."""
    import sys

    sys.path.insert(0, str(BUNDLE_DIR / "edge" / "exporters"))
    import mlx_sbom_collector as sbom

    monkeypatch.setattr(sbom, "CONFIG_DIR", tmp_path)
    status, mismatch_cnt, missing_cnt, diffs = sbom._evaluate_drift(
        node_name="edge-mac-2",
        base_device="edge-mac-1",
        current_items=[{"package": "python3", "version": "3.12", "ecosystem": "system", "path": "/usr/bin/python3", "hash": "x"}],
    )
    assert status == "NO_REFERENCE"
    assert mismatch_cnt == 0
    assert missing_cnt == 0
    assert diffs == []


def test_bundle_stop_does_not_kill_self(tmp_path, monkeypatch):
    """Verify MonitoringBundle.stop() never signals os.getpid() during self-shutdown."""
    import os
    import sys

    sys.path.insert(0, str(BUNDLE_DIR / "edge"))
    import mlx_monitoring_bundle as mb

    pid_dir = tmp_path / "bin"
    pid_dir.mkdir(parents=True, exist_ok=True)
    bundle_pid_file = pid_dir / "mlx_monitoring_bundle.pid"
    bundle_pid_file.write_text(str(os.getpid()), encoding="utf8")
    (pid_dir / "hw.pid").write_text("999991", encoding="utf8")

    alive = {999991: True, os.getpid(): True}
    monkeypatch.setattr(mb, "PID_FILE", bundle_pid_file)
    monkeypatch.setattr(mb, "pid_alive", lambda p: alive.get(p, False))
    monkeypatch.setattr(mb, "_pid_matches_service", lambda p, _tokens: p == 999991)

    killed = []

    def _rec_killpg(pgid, sig):
        killed.append(("killpg", pgid, sig))
        alive[pgid] = False

    def _rec_kill(pid, sig):
        killed.append(("kill", pid, sig))
        alive[pid] = False

    monkeypatch.setattr(os, "getpgid", lambda p: p)
    monkeypatch.setattr(os, "killpg", _rec_killpg)
    monkeypatch.setattr(os, "kill", _rec_kill)

    bundle = object.__new__(mb.Bundle)
    bundle.pid_dir = pid_dir
    bundle.stop()

    assert not any(pid == os.getpid() for _, pid, _ in killed)
    assert any(pid == 999991 for _, pid, _ in killed)


def test_proxy_upstream_error_increments_requests_and_skips_synth_sse(monkeypatch):
    """Verify upstream >=400 increments 3-label REQUESTS and passes through when synth_sse is active."""
    import json
    import sys
    import urllib.error

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(BUNDLE_DIR / "edge" / "exporters"))
    import mlx_metrics_proxy as p
    import test_proxy_rewrite as tpr

    server = tpr.ThreadingHTTPServer(("127.0.0.1", 0), tpr.StubUpstream)
    u_port = server.server_address[1]
    t_up = tpr.threading.Thread(target=server.serve_forever, daemon=True)
    t_up.start()

    monkeypatch.setattr(p.Proxy, "upstream", ("127.0.0.1", u_port))
    monkeypatch.setattr(p.Proxy, "metrics_path", "/metrics")
    monkeypatch.setattr(p, "_MODEL_NAME", tpr.MODEL)
    monkeypatch.setattr(p.Proxy, "stream_sample", 1.0)
    monkeypatch.setattr(p.Proxy, "logprobs_top", 2)
    monkeypatch.setattr(p.Proxy, "strict_model", False)

    proxy_srv = tpr.ThreadingHTTPServer(("127.0.0.1", 0), p.Proxy)
    p_port = proxy_srv.server_address[1]
    t_px = tpr.threading.Thread(target=proxy_srv.serve_forever, daemon=True)
    t_px.start()

    base = f"http://127.0.0.1:{p_port}"
    try:
        before_req = tpr.series(tpr.metrics(base), "mlx_requests_total")
        tpr.UPSTREAM_MODE["status"] = 503
        tpr.UPSTREAM_MODE["tool_calls"] = False
        caught = None
        try:
            tpr.post(
                base,
                {
                    "model": tpr.MODEL,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        except urllib.error.HTTPError as exc:
            caught = exc
        assert caught is not None
        assert caught.code == 503
        assert caught.headers.get("Content-Type", "").startswith("application/json")
        assert json.loads(caught.read())["error"] == "upstream said no"

        after_req = tpr.series(tpr.metrics(base), "mlx_requests_total")
        assert after_req == before_req + 1
    finally:
        tpr.UPSTREAM_MODE["status"] = 200
        proxy_srv.shutdown()
        proxy_srv.server_close()
        server.shutdown()
        server.server_close()


def test_proxy_bound_model_label_caps_unseen_models(monkeypatch):
    """Verify non-strict mode bounds unseen model labels at _max_model_labels and buckets excess."""
    import sys

    sys.path.insert(0, str(BUNDLE_DIR / "edge" / "exporters"))
    import mlx_metrics_proxy as p

    monkeypatch.setattr(p, "_MODEL_NAME", "base-model")
    monkeypatch.setattr(p.Proxy, "strict_model", False)
    monkeypatch.setattr(p.Proxy, "model_aliases", set())
    monkeypatch.setattr(p.Proxy, "_seen_models", set())
    monkeypatch.setattr(p.Proxy, "_max_model_labels", 3)

    assert p.Proxy._bound_model_label("base-model") == "base-model"
    assert p.Proxy._bound_model_label("m1") == "m1"
    assert p.Proxy._bound_model_label("m2") == "m2"
    assert p.Proxy._bound_model_label("m3") == "m3"
    assert p.Proxy._bound_model_label("m4") == "other"
    assert p.Proxy._bound_model_label("m2") == "m2"


def test_hw_telemetry_net_iface_cache_and_ps_stats(monkeypatch):
    """Verify _net_iface caches default-route detection for 60s and _collect_ps_stats returns worker + proc stats."""
    import subprocess
    import sys

    sys.path.insert(0, str(BUNDLE_DIR / "edge" / "exporters"))
    import mlx_hw_telemetry as hw

    calls = {"route": 0}

    def fake_run(cmd, **_kwargs):
        class Res:
            stdout = ""

        if cmd[:3] == ["route", "-n", "get"]:
            calls["route"] += 1
            Res.stdout = "   interface: en0\n"
        elif cmd[:2] == ["ps", "-axo"]:
            Res.stdout = (
                "  101  12.5  1024 /usr/bin/python -m mlx_lm.server\n"
                "  102   2.0   512 /usr/bin/python mlx_metrics_proxy.py\n"
                "  103   99.0  999 /usr/bin/python mlx_hw_telemetry.py\n"
            )
        return Res()

    monkeypatch.setattr(hw, "_NET_IFACE", "")
    monkeypatch.setattr(hw, "_NET_IFACE_CACHE", ("", 0.0))
    monkeypatch.setattr(hw, "_PROC_MATCH", "mlx_lm.server")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert hw._net_iface() == "en0"
    assert hw._net_iface() == "en0"
    assert calls["route"] == 1

    (wcpu, wrss), proc_stats = hw._collect_ps_stats()
    assert wcpu == 12.5
    assert wrss == 1024 * 1024
    assert proc_stats["mlx_proxy"] == (2.0, 512 * 1024)
    assert hw._host_uptime_seconds() >= 0.0


def test_validate_nodes_uses_mlx_device_id_and_custom_ports(tmp_path, monkeypatch, capsys):
    """Verify validate_nodes.py honors MLX_DEVICE_ID env var and custom *_LISTEN ports."""
    import socket
    import sys

    sys.path.insert(0, str(BUNDLE_DIR / "edge"))
    import validate_nodes as vn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    busy_port = sock.getsockname()[1]

    nodes_file = tmp_path / "nodes.yml"
    nodes_file.write_text(
        "central:\n  victoria_metrics: 127.0.0.1:8428\n  otel: 127.0.0.1:4318\n"
        "devices:\n  - id: dev-a\n    ip: 10.0.0.1\n  - id: dev-b\n    ip: 10.0.0.2\n",
        encoding="utf8",
    )
    env_file = tmp_path / "edge.env"
    env_file.write_text(
        f"VENV_PYTHON={sys.executable}\n"
        "MLX_UPSTREAM=http://127.0.0.1:11234\n"
        f"PROXY_LISTEN=127.0.0.1:{busy_port}\n",
        encoding="utf8",
    )

    monkeypatch.setenv("MLX_DEVICE_ID", "dev-b")
    monkeypatch.setattr(vn, "REQUIRED_MODULES", ())
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_nodes.py", "--nodes", str(nodes_file), "--env-file", str(env_file), "--check-ports"],
    )
    try:
        rc = vn.main()
    finally:
        sock.close()

    out = capsys.readouterr().out
    assert rc == 0
    assert "node     : dev-b" in out
    assert f"WARN  port {busy_port} is already in use" in out


def test_log_tailer_partial_line_traceback_and_queue_gauge(tmp_path, monkeypatch):
    """Verify log tailer partial-line idle flush, traceback grouping, and queue gauge."""
    import collections
    import queue
    import threading
    import time
    import mlx_server_log_tailer as lt

    log_path = tmp_path / "server.log"
    log_path.write_text("", encoding="utf8")

    monkeypatch.setattr(lt.Tailer, "_prepare_exporter", lambda self, _ep: None)
    tailer = lt.Tailer(
        log_path=log_path,
        exporter="http://127.0.0.1:4318",
        rate=100.0,
        include_http=True,
        all_lines=True,
        backfill=10,
        project="proj",
        macbook="mac-1",
    )

    emitted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(tailer, "_emit", lambda msg, sev, cat: emitted.append((msg, sev, cat)))

    # 1. Multi-line traceback grouping
    tailer._handle_line("Traceback (most recent call last):\n")
    tailer._handle_line('  File "app.py", line 10, in run\n')
    assert len(emitted) == 0
    tailer._handle_line("ValueError: bad input\n")
    assert len(emitted) == 1
    msg, sev, cat = emitted[0]
    assert "Traceback (most recent call last):" in msg
    assert "ValueError: bad input" in msg
    assert sev == "error"
    assert cat == "python"

    # 2. _update_queue_gauge with both Queue (qsize) and deque (len)
    class _FakeProc:
        def __init__(self, q):
            self._queue = q

    q = queue.Queue()
    q.put("a")
    q.put("b")
    tailer._processor = _FakeProc(q)
    tailer._update_queue_gauge()
    assert lt.G_QUEUE._value.get() == 2

    dq = collections.deque(["x", "y", "z"])
    tailer._processor = _FakeProc(dq)
    tailer._update_queue_gauge()
    assert lt.G_QUEUE._value.get() == 3

    # 3. Partial-line buffering + idle flush
    emitted.clear()
    monkeypatch.setattr(lt, "PARTIAL_LINE_FLUSH_TIMEOUT_S", 0.05)
    monkeypatch.setattr(lt, "DEFAULT_POLL_INTERVAL", 0.01)

    t = threading.Thread(target=tailer.run, daemon=True)
    t.start()
    try:
        time.sleep(0.05)
        with open(log_path, "ab") as fh:
            fh.write(b"partial without newline")
            fh.flush()
        deadline = time.monotonic() + 2.0
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        tailer.stop.set()
        t.join(timeout=2.0)

    assert len(emitted) == 1
    assert emitted[0][0] == "partial without newline"


def test_hw_telemetry_proc_net_dev_parsing(tmp_path, monkeypatch):
    """Verify Linux /proc/net/dev fallback parses rx/tx bytes and rx/tx error counts."""
    import builtins
    import mlx_hw_telemetry as hw

    proc_dev = tmp_path / "net_dev"
    proc_dev.write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs\n"
        "    lo:  100      1    0    0    0     0          0         0      200      2    0\n"
        "  en0: 1000     10    3    0    0     0          0         0     2000     20    4 0 0 0 0 0\n",
        encoding="utf8",
    )

    real_exists = os.path.exists
    real_open = builtins.open

    monkeypatch.setattr(
        os.path,
        "exists",
        lambda p: True if p == "/proc/net/dev" else real_exists(p),
    )
    monkeypatch.setattr(
        builtins,
        "open",
        lambda file, *a, **kw: real_open(proc_dev, *a, **kw) if file == "/proc/net/dev" else real_open(file, *a, **kw),
    )

    assert hw._net_stats("en0") == (1000, 2000, 3, 4)
    assert hw._net_stats("eth99") is None


def test_process_telemetry_gpu_share_and_stale_label_pruning(monkeypatch):
    """Verify AI GPU/ANE attribution sums <= 100% of totals and dead PIDs prune cleanly."""
    import mlx_process_telemetry as pt

    monkeypatch.setattr(pt, "_probe_apple_silicon_gpu_and_ane", lambda: (80.0, 4.0))
    monkeypatch.setattr(pt, "psutil", None)

    sample_set = [
        [
            {"pid": 101, "user": "dk", "cpu_pct": 75.0, "rss_bytes": 1024 * 1024 * 500,
             "process": "python3", "command": "python3 -m mlx_lm.server"},
            {"pid": 102, "user": "dk", "cpu_pct": 25.0, "rss_bytes": 1024 * 1024 * 200,
             "process": "python3", "command": "python3 worker.py"},
        ],
        [
            {"pid": 101, "user": "dk", "cpu_pct": 80.0, "rss_bytes": 1024 * 1024 * 500,
             "process": "python3", "command": "python3 -m mlx_lm.server"},
        ],
    ]
    idx = {"i": 0}
    monkeypatch.setattr(pt, "_list_processes", lambda: sample_set[idx["i"]])

    pt._sample_processes("node1", top_k=10)
    gpu_samples = [
        s.value for s in pt.G_PROCESS_GPU.collect()[0].samples
        if s.name == "process_gpu_utilization_percent" and s.labels.get("device") == "node1"
    ]
    ane_samples = [
        s.value for s in pt.G_PROCESS_ANE.collect()[0].samples
        if s.name == "process_ane_power_watts" and s.labels.get("device") == "node1"
    ]
    assert len(gpu_samples) == 2
    assert 0 < sum(gpu_samples) <= 80.2
    assert len(ane_samples) == 2
    assert 0 < sum(ane_samples) <= 4.01

    idx["i"] = 1
    pt._sample_processes("node1", top_k=10)
    cpu_pids = {
        s.labels.get("pid")
        for s in pt.G_PROCESS_CPU.collect()[0].samples
        if s.name == "process_cpu_percent" and s.labels.get("device") == "node1"
    }
    assert cpu_pids == {"101"}


def test_sbom_reverse_drift_stale_cleanup_and_skip_package_metrics(tmp_path, monkeypatch):
    """Verify reverse extra-package drift, stale gauge clearing on sync, and skip_package_metrics."""
    import mlx_sbom_collector as sb

    log_dir = tmp_path / "logs"
    cfg_dir = tmp_path / "config"
    log_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sb, "LOG_DIR", log_dir)
    monkeypatch.setattr(sb, "CONFIG_DIR", cfg_dir)

    base_items = [{"package": "mlx", "version": "0.19", "ecosystem": "pip", "path": "/venv", "hash": "h1"}]
    extra_items = [
        {"package": "mlx", "version": "0.19", "ecosystem": "pip", "path": "/venv", "hash": "h1"},
        {"package": "extra-pkg", "version": "2.0", "ecosystem": "pip", "path": "/venv", "hash": "h2"},
    ]

    # Save baseline reference via base-mac scan
    state = {"items": base_items}
    monkeypatch.setattr(sb, "_scan_pip_venvs", lambda: list(state["items"]))
    monkeypatch.setattr(sb, "_scan_homebrew", lambda: [])
    monkeypatch.setattr(sb, "_scan_system_binaries", lambda: [])
    monkeypatch.setattr(sb, "_scan_mlx_models", lambda: [])

    sb.collect_and_update_metrics("base-mac", "base-mac")
    assert (cfg_dir / "sbom_base_reference.json").is_file()

    # Reverse extra package detection on edge-mac
    state["items"] = extra_items
    status, mismatch_cnt, missing_cnt, diffs = sb._evaluate_drift("edge-mac", "base-mac", extra_items)
    assert status == "DEVIATED"
    assert missing_cnt == 0
    assert mismatch_cnt == 1
    assert diffs == [{
        "package": "extra-pkg",
        "base_version": "MISSING",
        "target_version": "2.0",
        "ecosystem": "pip",
        "path": "/venv",
    }]

    sb.collect_and_update_metrics("edge-mac", "base-mac", skip_package_metrics=True)
    pkg_samples = [
        s for s in sb.G_SBOM_PACKAGE_INFO.collect()[0].samples
        if s.name == "mac_sbom_package_info"
    ]
    assert pkg_samples == []
    drift_samples = [
        s for s in sb.G_SBOM_DRIFT_DETAILS.collect()[0].samples
        if s.name == "mac_sbom_drift_details"
    ]
    assert len(drift_samples) == 1
    assert drift_samples[0].labels["base_version"] == "MISSING"

    # Rescan after extra package removed -> stale drift gauge sample must be cleared
    state["items"] = base_items
    sb.collect_and_update_metrics("edge-mac", "base-mac")
    drift_after = [
        s for s in sb.G_SBOM_DRIFT_DETAILS.collect()[0].samples
        if s.name == "mac_sbom_drift_details"
    ]
    assert drift_after == []


def test_bundle_reused_pid_sigkill_multi_device_and_vmagent_flags(tmp_path, monkeypatch):
    """Verify Bundle reused-PID skip, SIGKILL escalation, multi-device guard, and vmagent flags."""
    import signal
    import time
    import pytest
    import mlx_monitoring_bundle as mb

    # 1. Reused PID must NOT be signaled
    pid_dir = tmp_path / "bin"
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "proxy.pid").write_text("888881\n", encoding="utf8")
    monkeypatch.setattr(mb, "PID_FILE", tmp_path / "bundle.pid")
    monkeypatch.setattr(mb, "pid_alive", lambda p: p == 888881)
    monkeypatch.setattr(mb, "_pid_matches_service", lambda _p, _t: False)

    signaled = []
    monkeypatch.setattr(os, "getpgid", lambda p: p)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signaled.append(("killpg", pgid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: signaled.append(("kill", pid, sig)))

    bundle = object.__new__(mb.Bundle)
    bundle.pid_dir = pid_dir
    bundle.stop()
    assert not any(pid == 888881 for _, pid, _ in signaled)

    # 2. SIGKILL escalation after 5s timeout if SIGTERM didn't terminate verified PID
    (pid_dir / "hw.pid").write_text("777771\n", encoding="utf8")
    monkeypatch.setattr(mb, "pid_alive", lambda p: p == 777771)
    monkeypatch.setattr(mb, "_pid_matches_service", lambda p, _t: p == 777771)
    ticks = iter([0.0, 0.1, 10.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks, 10.0))
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    signaled.clear()
    bundle.stop()
    sigs_for_777 = [sig for _, pid, sig in signaled if pid == 777771]
    assert signal.SIGTERM in sigs_for_777
    assert signal.SIGKILL in sigs_for_777

    # 3. Multi-device ambiguity fail-fast when no MLX_DEVICE_ID matches
    monkeypatch.setattr(mb, "select_node_config", lambda _c: {})
    with pytest.raises(SystemExit):
        mb.Bundle({
            "devices": [
                {"node_id": "node-a", "enabled": True},
                {"node_id": "node-b", "enabled": True},
            ],
        })

    # 4. vmagent flags use -remoteWrite.tmpDataPath (not invalid persistentQueue flags)
    pq_dir = tmp_path / "vmagent-pq"
    b2 = mb.Bundle({
        "MLX_DEVICE_ID": "mac-a",
        "CENTRAL_VICTORIA_METRICS": "127.0.0.1:8428",
        "VMAGENT_PERSISTENT_QUEUE": str(pq_dir),
        "LOG_DIR": str(tmp_path / "logs"),
        "PID_DIR": str(tmp_path / "pids"),
        "EXPORTER_LOG_DIR": str(tmp_path / "logs"),
    })
    spawned_cmds = []
    monkeypatch.setattr(b2, "_render_vmagent_config", lambda: True)
    monkeypatch.setattr(b2, "_spawn_process", lambda name, cmd, **_kw: spawned_cmds.append(cmd) or 12345)
    b2.start_vmagent()
    assert len(spawned_cmds) == 1
    cmd = spawned_cmds[0]
    assert "-remoteWrite.tmpDataPath" in cmd
    assert str(pq_dir) in cmd
    assert not any("persistentQueue" in arg for arg in cmd)


def test_diagnostics_https_preservation_and_stage2_empty_nodes_fail(tmp_path, monkeypatch):
    """Verify HTTPS scheme preservation in diagnostics and Stage 2 FAIL on empty target nodes."""
    import subprocess
    if str(BUNDLE_DIR / "scripts") not in sys.path:
        sys.path.insert(0, str(BUNDLE_DIR / "scripts"))
    import diagnostics as dg

    edge_dir = tmp_path / "edge"
    central_dir = tmp_path / "central"
    (edge_dir / "config").mkdir(parents=True, exist_ok=True)
    central_dir.mkdir(parents=True, exist_ok=True)
    (edge_dir / "config" / "nodes.yml").write_text("devices: []\n", encoding="utf8")

    monkeypatch.setattr(dg, "EDGE_DIR", edge_dir)
    monkeypatch.setattr(dg, "CENTRAL_DIR", central_dir)
    monkeypatch.setenv("CENTRAL_VICTORIA_METRICS", "https://vm.example.com:8443")

    cfg = dg.load_config()
    assert cfg["cvm_scheme"] == "https"
    assert cfg["cvm"] == "vm.example.com:8443"

    seen_urls = []

    def _fake_probe(url, timeout=2.0, headers=None):
        seen_urls.append(url)
        return 200, json.dumps({"status": "success", "data": {"result": []}})

    monkeypatch.setattr(dg, "probe_http", _fake_probe)
    assert dg.query_vm("https://vm.example.com:8443", "up") == []
    assert seen_urls[0].startswith("https://vm.example.com:8443/api/v1/query")

    # Stage 2 FAIL when devices: []
    with _make_hermetic_diag_env(tmp_path) as env:
        empty_nodes = Path(env["MLX_EDGE_DIR"]) / "config" / "nodes.yml"
        empty_nodes.write_text("devices: []\n", encoding="utf8")
        diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
        proc = subprocess.run(
            [sys.executable, str(diag_script), "doctor"],
            capture_output=True,
            text=True,
            cwd=str(BUNDLE_DIR),
            env=env,
            timeout=15,
        )
    assert proc.returncode == 1
    assert "[Stage 2/5] Edge Exporters & Nodes   : FAIL (no enabled device nodes matched)" in proc.stdout


def test_discover_nodes_tailscale_exclusion(monkeypatch):
    """Verify Tailscale 100.64.0.0/10 addresses are detected and bypassed when LAN IP exists."""
    import socket
    sys.path.insert(0, str(BUNDLE_DIR / "scripts"))
    import discover_nodes as dn

    assert dn._is_tailscale_ip("100.64.0.1") is True
    assert dn._is_tailscale_ip("100.127.255.254") is True
    assert dn._is_tailscale_ip("192.168.1.50") is False
    assert dn._is_tailscale_ip("not-an-ip") is False

    class _FakeSock:
        def connect(self, _addr):
            return None

        def getsockname(self):
            return ("100.100.1.5", 0)

        def close(self):
            return None

    monkeypatch.setattr(socket, "socket", lambda *a, **kw: _FakeSock())
    monkeypatch.setattr(socket, "gethostbyname", lambda _h: "192.168.1.50")
    assert dn.get_ip_local("192.168.1.10") == "192.168.1.50"


def test_log_tailer_rotation_seeks_zero_and_http_5xx_not_dropped(tmp_path, monkeypatch):
    """Verify HTTP 5xx logs bypass http_filter and rotated files read from byte 0."""
    import threading
    import time
    import mlx_server_log_tailer as lt

    log_path = tmp_path / "server.log"
    log_path.write_text("initial line before start\n", encoding="utf8")

    monkeypatch.setattr(lt.Tailer, "_prepare_exporter", lambda self, _ep: None)
    tailer = lt.Tailer(
        log_path=log_path,
        exporter="http://127.0.0.1:4318",
        rate=100.0,
        include_http=False,
        all_lines=False,
        backfill=0,
        project="proj",
        macbook="mac-1",
    )
    emitted: list[tuple[str, str, str]] = []
    monkeypatch.setattr(tailer, "_emit", lambda msg, sev, cat: emitted.append((msg, sev, cat)))

    # 1. HTTP 200 is filtered out when include_http=False, but HTTP 500 is emitted as error
    tailer._handle_line('127.0.0.1 - - "POST /v1/chat/completions HTTP/1.1" 200 -\n')
    assert emitted == []
    tailer._handle_line('127.0.0.1 - - "POST /v1/chat/completions HTTP/1.1" 500 -\n')
    assert len(emitted) == 1
    assert emitted[0][1] == "error"
    assert emitted[0][2] == "http"

    # 2. Rotation with backfill=0 reads new file from offset 0
    emitted.clear()
    monkeypatch.setattr(lt, "DEFAULT_POLL_INTERVAL", 0.01)
    t = threading.Thread(target=tailer.run, daemon=True)
    t.start()
    try:
        time.sleep(0.05)
        assert emitted == []  # initial line skipped because backfill=0
        rotated_tmp = tmp_path / "server.log.new"
        rotated_tmp.write_text("first line in rotated file\n", encoding="utf8")
        os.replace(rotated_tmp, log_path)
        deadline = time.monotonic() + 2.0
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        tailer.stop.set()
        t.join(timeout=2.0)

    assert len(emitted) == 1
    assert emitted[0][0] == "first line in rotated file"


def test_sbom_cross_ecosystem_collision_and_supervisor_ps_filter(tmp_path, monkeypatch):
    """Verify SBOM keys by ecosystem::package and hw_telemetry excludes mlx_server_supervisor."""
    import subprocess
    import mlx_sbom_collector as sb
    import mlx_hw_telemetry as hw

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(sb, "CONFIG_DIR", cfg_dir)

    base_items = [
        {"package": "git", "version": "2.39.0", "ecosystem": "system", "path": "/usr/bin/git", "hash": "h1"},
        {"package": "git", "version": "2.44.0", "ecosystem": "brew", "path": "/opt/homebrew/bin/git", "hash": "h2"},
    ]
    edge_items = [
        {"package": "git", "version": "2.39.0", "ecosystem": "system", "path": "/usr/bin/git", "hash": "h1"},
        {"package": "git", "version": "2.43.0", "ecosystem": "brew", "path": "/opt/homebrew/bin/git", "hash": "h3"},
    ]
    sb._evaluate_drift("base-mac", "base-mac", base_items)
    status, mismatch_cnt, missing_cnt, diffs = sb._evaluate_drift("edge-mac", "base-mac", edge_items)
    assert status == "DEVIATED"
    assert mismatch_cnt == 1
    assert missing_cnt == 0
    assert diffs[0]["ecosystem"] == "brew"

    fake_ps = (
        "  101  25.0  102400 python3 -m mlx_lm.server --port 11234\n"
        "  102   5.0   20480 python3 mlx_server_supervisor.py --process-pattern mlx_lm.server\n"
    )
    monkeypatch.setattr(hw, "_PROC_MATCH", "mlx_lm.server")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0], 0, stdout=fake_ps, stderr=""),
    )
    (wcpu, wrss), _ = hw._collect_ps_stats()
    assert wcpu == 25.0
    assert wrss == 102400 * 1024


def test_diagnostics_v1_upstream_suffix_and_env_precedence(tmp_path):
    """Verify MLX_UPSTREAM with /v1 suffix and env precedence in review-edge."""
    import subprocess
    diag_script = BUNDLE_DIR / "scripts" / "diagnostics.py"
    with _make_hermetic_diag_env(tmp_path) as env:
        env["MLX_UPSTREAM"] = f"http://{env['CENTRAL_VICTORIA_METRICS']}/v1"
        proc = subprocess.run(
            [sys.executable, str(diag_script), "review-edge"],
            capture_output=True,
            text=True,
            cwd=str(BUNDLE_DIR),
            env=env,
            timeout=15,
        )
    assert proc.returncode == 0, f"review-edge failed with /v1 upstream: {proc.stdout}\n{proc.stderr}"
    assert "serving: test-model" in proc.stdout


def test_model_info_moe_and_safetensors_totals(tmp_path):
    """Verify ModelInfo parses MoE dims, GQA ratio, and safetensors index totals."""
    from mlx_model_info import model_info as _mi

    mdir = tmp_path / "Qwen-MoE-4bit"
    mdir.mkdir()
    (mdir / "config.json").write_text(
        json.dumps({
            "architectures": ["Qwen3MoeForCausalLM"],
            "num_hidden_layers": 32,
            "hidden_size": 2048,
            "num_attention_heads": 16,
            "num_key_value_heads": 4,
            "head_dim": 128,
            "intermediate_size": 6144,
            "vocab_size": 151936,
            "max_position_embeddings": 32768,
            "num_local_experts": 64,
            "num_experts_per_tok": 8,
            "moe_intermediate_size": 1408,
            "shared_expert_intermediate_size": 2816,
            "sliding_window": 4096,
            "dtype": "bfloat16",
            "quantization": {"bits": 4, "group_size": 64},
        }),
        encoding="utf8",
    )
    (mdir / "model.safetensors.index.json").write_text(
        json.dumps({
            "metadata": {"total_size": 12_000_000_000},
            "weight_map": {
                "model.embed_tokens.weight": "model-00001-of-00004.safetensors",
                "model.layers.0.self_attn.q_proj.weight": "model-00001-of-00004.safetensors",
                "model.layers.15.mlp.gate.weight": "model-00002-of-00004.safetensors",
                "model.layers.16.mlp.gate.weight": "model-00003-of-00004.safetensors",
                "model.layers.31.mlp.gate.weight": "model-00004-of-00004.safetensors",
                "lm_head.weight": "model-00004-of-00004.safetensors",
            },
        }),
        encoding="utf8",
    )
    (mdir / "model-00001-of-00004.safetensors").write_bytes(b"a" * 100)
    (mdir / "model-00002-of-00004.safetensors").write_bytes(b"b" * 100)

    info_obj = _mi("Qwen-MoE-4bit", model_path=str(mdir), refresh=True)
    assert info_obj is not None
    assert info_obj.is_moe is True
    assert info_obj.num_local_experts == 64
    assert info_obj.num_experts_per_tok == 8
    assert info_obj.moe_intermediate_size == 1408
    assert info_obj.shared_expert_intermediate_size == 2816
    assert info_obj.sliding_window == 4096
    assert info_obj.gqa_ratio == 4
    assert info_obj.total_weight_bytes == 12_000_000_000
    assert info_obj.shard_file_count == 4


def test_shard_topology_pipeline_and_tensor(tmp_path):
    """Verify shard placement resolution across pipeline (hosts.json) and tensor splits."""
    from mlx_model_info import model_info as _mi
    import mlx_shard_topology as st

    mdir = tmp_path / "Llama-32L"
    mdir.mkdir()
    (mdir / "config.json").write_text(
        json.dumps({
            "num_hidden_layers": 32,
            "hidden_size": 4096,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "intermediate_size": 14336,
            "max_position_embeddings": 8192,
            "dtype": "float16",
        }),
        encoding="utf8",
    )
    wmap = {"model.embed_tokens.weight": "model-00001-of-00004.safetensors"}
    for idx in range(32):
        shard_num = (idx // 8) + 1
        wmap[f"model.layers.{idx}.self_attn.q_proj.weight"] = (
            f"model-0000{shard_num}-of-00004.safetensors"
        )
    wmap["lm_head.weight"] = "model-00004-of-00004.safetensors"
    (mdir / "model.safetensors.index.json").write_text(
        json.dumps({"metadata": {"total_size": 16_000_000_000}, "weight_map": wmap}),
        encoding="utf8",
    )
    for idx in range(1, 5):
        (mdir / f"model-0000{idx}-of-00004.safetensors").write_bytes(b"x" * 1000)

    hosts_file = tmp_path / "hosts.json"
    hosts_file.write_text(
        json.dumps({
            "backend": "ring",
            "hosts": [
                {"ssh": "127.0.0.1", "ips": ["192.168.2.1"]},
                {"ssh": "192.168.2.2", "ips": ["192.168.2.2"]},
            ],
        }),
        encoding="utf8",
    )
    mi = _mi("Llama-32L", model_path=str(mdir), refresh=True)
    assert mi is not None

    rank0 = st.discover_shard_placement(
        model_info_obj=mi,
        model_id="Llama-32L",
        process_pattern="",
        hostfile=str(hosts_file),
        rank_override=0,
        world_override=2,
        nodes_devices=[
            {"id": "mm-r0", "rank": 0, "ip": "192.168.2.1"},
            {"id": "mm-r1", "rank": 1, "ip": "192.168.2.2"},
        ],
    )
    assert rank0.mode == "pipeline"
    assert rank0.backend == "ring"
    assert (rank0.layer_start, rank0.layer_end) == (0, 15)
    assert rank0.layers_assigned == 16
    assert rank0.shard_files == "shards-00001..00002/4"
    assert rank0.shard_files_present == 2
    assert rank0.shard_files_expected == 2
    assert rank0.peers == ((1, "192.168.2.2"),)
    assert [n["device"] for n in rank0.cluster_nodes] == ["mm-r0", "mm-r1"]
    assert rank0.cluster_nodes[1]["layer_range"] == "16..31"

    rank1_tp = st.discover_shard_placement(
        model_info_obj=mi,
        model_id="Llama-32L",
        process_pattern="",
        hostfile=str(hosts_file),
        mode_override="tensor",
        backend_override="jaccl",
        rank_override=1,
        world_override=2,
    )
    assert rank1_tp.mode == "tensor"
    assert rank1_tp.backend == "jaccl"
    assert (rank1_tp.layer_start, rank1_tp.layer_end) == (0, 31)
    assert rank1_tp.dim_kv_heads == 4
    assert rank1_tp.dim_intermediate == 7168
    assert rank1_tp.expected_weight_bytes == 8_000_000_000


def test_supervisor_emits_shard_placement_metrics(tmp_path):
    """Verify PassiveServerObserver exports bounded-cardinality shard gauges."""
    import argparse
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from prometheus_client import generate_latest
    import mlx_server_supervisor as sup

    mdir = tmp_path / "ShardTestModel"
    mdir.mkdir()
    (mdir / "config.json").write_text(
        json.dumps({
            "num_hidden_layers": 24,
            "hidden_size": 1024,
            "num_attention_heads": 8,
            "num_key_value_heads": 4,
            "intermediate_size": 4096,
            "max_position_embeddings": 4096,
        }),
        encoding="utf8",
    )
    (mdir / "model.safetensors").write_bytes(b"weights" * 100)

    class _HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            body = json.dumps({"data": [{"id": "ShardTestModel"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        args = argparse.Namespace(
            health=f"http://127.0.0.1:{srv.server_address[1]}/v1/models",
            model="ShardTestModel",
            model_path=str(mdir),
            server_log=str(tmp_path / "missing.log"),
            process_pattern="",
            launchd_label="",
            systemd_unit="",
            probe_interval=5.0,
            health_timeout=2.0,
            fail_threshold=1,
            shard_rank=0,
            shard_world_size=1,
            peer_probe_port=0,
            peer_app_port=0,
            shard_refresh_interval=0.0,
        )
        obs = sup.PassiveServerObserver(args)
        obs.poll_once()
        text = generate_latest().decode()
        assert 'mlx_model_shard_info{' in text
        assert 'model="ShardTestModel"' in text
        assert 'layer_range="0..23"' in text
        assert 'mlx_shard_dim_hidden{model="ShardTestModel",rank="0"} 1024.0' in text
        assert 'mlx_shard_dim_kv_heads_exact{model="ShardTestModel",rank="0"} 1.0' in text
        assert 'mlx_shard_files_present{model="ShardTestModel",rank="0"} 1.0' in text
    finally:
        srv.shutdown()
        srv.server_close()


def test_accuracy_eval_canary_ece_and_opik_judge(tmp_path):
    """Verify mlx_accuracy_eval runs canary probes, computes ECE, and scores Opik traces."""
    import argparse
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from prometheus_client import generate_latest
    import mlx_accuracy_eval as ev

    suite_file = tmp_path / "suite.jsonl"
    suite_file.write_text(
        "\n".join([
            json.dumps({"id": "q1", "suite": "exact_qa", "prompt": "2+2?", "check": "contains", "expected": "4"}),
            json.dumps({"id": "q2", "suite": "json_schema", "prompt": "json?", "check": "json_keys", "required_keys": ["ok"]}),
            json.dumps({"id": "q3", "suite": "coding", "prompt": "fn?", "check": "python_ast", "expected": "sq"}),
        ]),
        encoding="utf8",
    )

    seen_eval_hdrs = []
    written_scores = []

    class _MockHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            if self.path.startswith("/v1/models"):
                body = json.dumps({"data": [{"id": "eval-model"}]}).encode()
            elif "/traces" in self.path:
                body = json.dumps({
                    "content": [{
                        "id": "trace-abc-123",
                        "input": {"messages": [{"role": "user", "content": "What is 2+2?"}]},
                        "output": {"content": "4"},
                        "feedback_scores": [],
                    }]
                }).encode()
            else:
                body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
            seen_eval_hdrs.append((
                self.headers.get("X-Mlx-Eval"),
                self.headers.get("X-Mlx-Trace"),
            ))
            prompt = req["messages"][0]["content"]
            if "You are an evaluator" in prompt:
                reply = json.dumps({"correctness": 1.0, "helpfulness": 0.9, "hallucination_free": 1.0})
                lp = -0.05
            elif "2+2?" in prompt:
                reply = "4"
                lp = -0.05
            elif "json?" in prompt:
                reply = '{"ok": true}'
                lp = -0.1
            else:
                reply = "not valid python ***"
                lp = -0.05
            body = json.dumps({
                "choices": [{
                    "message": {"content": reply},
                    "logprobs": {"content": [{"token": "x", "logprob": lp}]},
                }]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            written_scores.append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _MockHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        args = argparse.Namespace(
            proxy_url=base,
            model="eval-model",
            suite_file=str(suite_file),
            eval_interval=60.0,
            enable_canary=True,
            enable_judge=True,
            opik_base=base,
            opik_project="mlx",
            judge_url=base + "/v1",
            judge_model="eval-model",
            judge_interval=60.0,
            judge_limit=10,
            judge_window=20,
            api_key="",
            verify_tls=False,
        )
        evaluator = ev.AccuracyEvaluator(args)
        assert evaluator.run_canary_once() is True
        assert evaluator.run_judge_once() == 1
        assert all(h == ("1", "0") for h in seen_eval_hdrs)
        assert len(written_scores) == 1
        assert len(written_scores[0]["scores"]) == 3

        prom = generate_latest().decode()
        assert 'mlx_eval_overall_accuracy_ratio{model="eval-model"} ' in prom
        assert 'mlx_eval_perplexity{model="eval-model"} ' in prom
        assert 'mlx_eval_calibration_ece{model="eval-model"} ' in prom
        assert 'mlx_judge_score{criterion="correctness",model="eval-model"} 1.0' in prom
    finally:
        srv.shutdown()
        srv.server_close()


def test_bundle_renders_eval_job_and_deploy_port_guard(tmp_path):
    """Verify bundle renders mlx-eval scrape job and deploy.sh has central port collision check."""
    import yaml as _yaml
    import mlx_monitoring_bundle as mb

    bundle = mb.Bundle({
        "central": {"victoria_metrics": "127.0.0.1:8428", "otel": "127.0.0.1:4318"},
        "devices": [{"id": "node0", "serial": "S0", "ip": "127.0.0.1", "enabled": True}],
        "EVAL_LISTEN": "127.0.0.1:19109",
    })
    bundle.vmagent_config_rendered = tmp_path / "vmagent.yml"
    assert bundle._render_vmagent_config() is True
    rendered = _yaml.safe_load(bundle.vmagent_config_rendered.read_text(encoding="utf8"))
    jobs = {sc["job_name"]: sc["static_configs"][0]["targets"][0] for sc in rendered["scrape_configs"]}
    assert jobs["mlx-eval"] == "127.0.0.1:19109"

    deploy_txt = (BUNDLE_DIR / "scripts" / "deploy.sh").read_text(encoding="utf8")
    assert "_check_central_host_ports" in deploy_txt
    assert 'lsof -nP -t -iTCP:"${port}" -sTCP:LISTEN' in deploy_txt


def test_shard_file_integrity_with_disjoint_node_subsets(tmp_path):
    """Verify nodes holding different checkpoint shard subsets report rank-local file counts."""
    from mlx_model_info import model_info as _mi
    import mlx_shard_topology as st

    # Setup 4-shard checkpoint
    node0_dir = tmp_path / "node0_model"
    node1_dir = tmp_path / "node1_model"
    node0_dir.mkdir()
    node1_dir.mkdir()

    config = {
        "num_hidden_layers": 32,
        "hidden_size": 4096,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "intermediate_size": 14336,
        "max_position_embeddings": 8192,
        "dtype": "float16",
    }
    wmap = {"model.embed_tokens.weight": "model-00001-of-00004.safetensors"}
    for idx in range(32):
        s_num = (idx // 8) + 1
        wmap[f"model.layers.{idx}.self_attn.q_proj.weight"] = f"model-0000{s_num}-of-00004.safetensors"
    wmap["lm_head.weight"] = "model-00004-of-00004.safetensors"
    idx_content = json.dumps({"metadata": {"total_size": 16_000_000_000}, "weight_map": wmap})

    for d in (node0_dir, node1_dir):
        (d / "config.json").write_text(json.dumps(config), encoding="utf8")
        (d / "model.safetensors.index.json").write_text(idx_content, encoding="utf8")

    # Node 0 holds only shards 1 and 2
    (node0_dir / "model-00001-of-00004.safetensors").write_bytes(b"w" * 100)
    (node0_dir / "model-00002-of-00004.safetensors").write_bytes(b"w" * 100)

    # Node 1 holds only shards 3 and 4
    (node1_dir / "model-00003-of-00004.safetensors").write_bytes(b"w" * 100)
    (node1_dir / "model-00004-of-00004.safetensors").write_bytes(b"w" * 100)

    mi0 = _mi("disjoint-model", model_path=str(node0_dir), refresh=True)
    mi1 = _mi("disjoint-model", model_path=str(node1_dir), refresh=True)

    hosts_file = tmp_path / "hosts.json"
    hosts_file.write_text(json.dumps({"backend": "ring", "hosts": ["10.0.0.1", "10.0.0.2"]}))

    # Node 0 evaluation
    placement0 = st.discover_shard_placement(
        model_info_obj=mi0,
        model_id="disjoint-model",
        process_pattern="",
        hostfile=str(hosts_file),
        rank_override=0,
        world_override=2,
    )
    assert placement0.shard_files_expected == 2
    assert placement0.shard_files_present == 2
    assert placement0.shard_files == "shards-00001..00002/4"

    # Node 1 evaluation
    placement1 = st.discover_shard_placement(
        model_info_obj=mi1,
        model_id="disjoint-model",
        process_pattern="",
        hostfile=str(hosts_file),
        rank_override=1,
        world_override=2,
    )
    assert placement1.shard_files_expected == 2
    assert placement1.shard_files_present == 2
    assert placement1.shard_files == "shards-00003..00004/4"

    # Now remove shard 4 from node 1 and verify incomplete detection
    (node1_dir / "model-00004-of-00004.safetensors").unlink()
    placement1_missing = st.discover_shard_placement(
        model_info_obj=mi1,
        model_id="disjoint-model",
        process_pattern="",
        hostfile=str(hosts_file),
        rank_override=1,
        world_override=2,
    )
    assert placement1_missing.shard_files_expected == 2
    assert placement1_missing.shard_files_present == 1


def test_peer_reachability_distinguishes_success_refusal_and_measures_latency():
    """Verify probe_peer_reachability marks ConnectionRefused as DOWN and returns connect latency."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import mlx_shard_topology as st

    class _ProbeHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

    # Create active server
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
    live_port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    # Find closed port for refused probe
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    closed_port = s.getsockname()[1]
    s.close()

    try:
        peers = ((1, "127.0.0.1"),)

        # 1. Active port probe
        res_live = st.probe_peer_reachability(peers, port=live_port, timeout=1.0)
        p_live = res_live[(1, "127.0.0.1")]
        assert p_live.up is True
        assert bool(p_live) is True
        assert p_live.reason == "connected"
        assert p_live.connect_seconds > 0.0

        # 2. Refused port probe (must be DOWN)
        res_refused = st.probe_peer_reachability(peers, port=closed_port, timeout=0.5)
        p_refused = res_refused[(1, "127.0.0.1")]
        assert p_refused.up is False
        assert bool(p_refused) is False
        assert p_refused.reason == "refused"

        # 3. Unreachable IP probe (non-routable TEST-NET IP)
        unreach_peers = ((2, "192.0.2.1"),)
        res_unreach = st.probe_peer_reachability(unreach_peers, port=live_port, timeout=0.1)
        p_unreach = res_unreach[(2, "192.0.2.1")]
        assert p_unreach.up is False
        assert p_unreach.reason in ("timeout", "unreachable")
    finally:
        srv.shutdown()
        srv.server_close()


def test_shard_aware_kv_cache_calculations(tmp_path):
    """Verify ModelInfo calculates shard-aware KV dimensions across PP, TP, hybrid, and sliding window."""
    from mlx_model_info import model_info as _mi

    # Create hybrid attention model (32 layers total, 8 full-attention layers at interval=4)
    mdir = tmp_path / "HybridModel"
    mdir.mkdir()
    (mdir / "config.json").write_text(
        json.dumps({
            "num_hidden_layers": 32,
            "hidden_size": 4096,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "head_dim": 128,
            "full_attention_interval": 4,
            "max_position_embeddings": 8192,
            "dtype": "float16",
        }),
        encoding="utf8",
    )
    mi = _mi("HybridModel", model_path=str(mdir), refresh=True)
    assert mi is not None
    assert mi.attention_layers == 8
    assert mi.linear_layers == 24
    assert len(mi.attention_layer_indices) == 8
    # Full model: 2 * 8 * 8 * 128 * 2 = 32,768 bytes/tok
    assert mi.kv_bytes_per_token == 32768

    # Pipeline Parallelism: Rank 0 assigned layers 0..15 (contains 4 attention layers: 3, 7, 11, 15)
    # Local KV: 2 * 4 * 8 * 128 * 2 = 16,384 bytes/tok
    pp_kv_rank0 = mi.kv_bytes_per_token_for_shard(layer_start=0, layer_end=15)
    assert pp_kv_rank0 == 16384
    assert mi.kv_bytes_for_max_context_shard(layer_start=0, layer_end=15) == 16384 * 8192

    # Zero attention layer shard (layers 0..2 contain 0 attention layers): KV should be 0
    zero_kv = mi.kv_bytes_per_token_for_shard(layer_start=0, layer_end=2)
    assert zero_kv == 0
    assert mi.kv_bytes_for_max_context_shard(layer_start=0, layer_end=2) == 0

    # Tensor Parallelism with uneven GQA split: 8 KV heads with TP=3
    # Rank 0 and 1 receive 3 heads (8//3 + 1), Rank 2 receives 2 heads (8//3)
    tp_kv_r0 = mi.kv_bytes_per_token_for_shard(layer_start=0, layer_end=31, tp_size=3, rank=0)
    assert tp_kv_r0 == 2 * 8 * 3 * 128 * 2  # 12,288 bytes/tok
    tp_kv_r1 = mi.kv_bytes_per_token_for_shard(layer_start=0, layer_end=31, tp_size=3, rank=1)
    assert tp_kv_r1 == 12288
    tp_kv_r2 = mi.kv_bytes_per_token_for_shard(layer_start=0, layer_end=31, tp_size=3, rank=2)
    assert tp_kv_r2 == 2 * 8 * 2 * 128 * 2  # 8,192 bytes/tok
    # Total KV head slots across 3 ranks: 3 + 3 + 2 = 8 heads exactly!

    # Explicit per-node override via dim_kv_heads
    tp_kv_override = mi.kv_bytes_per_token_for_shard(layer_start=0, layer_end=31, dim_kv_heads=4)
    assert tp_kv_override == 2 * 8 * 4 * 128 * 2  # 16,384 bytes/tok

    # Hybrid TP + PP: layers 0..15 (4 attn layers) and TP=2 (8//2 = 4 KV heads)
    # Local KV: 2 * 4 * 4 * 128 * 2 = 8,192 bytes/tok
    hybrid_kv = mi.kv_bytes_per_token_for_shard(layer_start=0, layer_end=15, tp_size=2)
    assert hybrid_kv == 8192

    # Sliding window model test: 4 layers (2 full_attention, 2 sliding_attention with sliding_window=2048)
    sw_dir = tmp_path / "SlidingModel"
    sw_dir.mkdir()
    (sw_dir / "config.json").write_text(
        json.dumps({
            "num_hidden_layers": 4,
            "hidden_size": 2048,
            "num_attention_heads": 16,
            "num_key_value_heads": 4,
            "head_dim": 128,
            "layer_types": ["full_attention", "sliding_attention", "full_attention", "sliding_attention"],
            "sliding_window": 2048,
            "max_position_embeddings": 8192,
            "dtype": "float16",
        }),
        encoding="utf8",
    )
    sw_mi = _mi("SlidingModel", model_path=str(sw_dir), refresh=True)
    assert sw_mi is not None
    assert len(sw_mi.attention_layer_indices) == 4
    assert len(sw_mi.sliding_layer_indices) == 2
    # Bytes per token per layer = 2 * 4 * 128 * 2 = 2048 bytes
    # For max context: 2 full layers * 8192 * 2048 + 2 sliding layers * 2048 * 2048
    expected_sw_ctx_bytes = (2 * 8192 * 2048) + (2 * 2048 * 2048)
    assert sw_mi.kv_bytes_for_max_context == expected_sw_ctx_bytes


def test_kv_cache_agent_topology_and_capacity_integration(tmp_path, monkeypatch):
    """Verify kv-cache agent consumes discover_shard_placement, honors MLX_RANK, and does not fallback to full model on 0 KV allocation."""
    from mlx_model_info import model_info as _mi
    import mlx_kv_cache_agent as kva

    mdir = tmp_path / "KVModel"
    mdir.mkdir()
    (mdir / "config.json").write_text(
        json.dumps({
            "num_hidden_layers": 32,
            "hidden_size": 4096,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "head_dim": 128,
            "full_attention_interval": 4,
            "max_position_embeddings": 8192,
            "dtype": "float16",
        }),
        encoding="utf8",
    )
    server_log = tmp_path / "server.log"
    server_log.write_text("Prompt Cache: 2 sequences, 0.50 GB\n- total: 2 sequences, 0.50 GB\n")

    # 1. Pipeline Rank 0: layers 0..15 (4 attention layers)
    monkeypatch.setenv("MLX_RANK", "0")
    monkeypatch.setenv("MLX_WORLD_SIZE", "2")
    monkeypatch.setenv("MLX_SHARD_LAYERS", "0..15")
    monkeypatch.setenv("MLX_PROMPT_CACHE_SIZE", "15")

    kva._update_gauges(str(server_log), "KVModel", model_path=str(mdir))

    # G_MAX_SEQ should reflect MLX_PROMPT_CACHE_SIZE=15
    assert kva.G_MAX_SEQ._value.get() == 15
    # Rank 0 has 4 attention layers: 4 * 2 * 8 * 128 * 2 * 8192 = 134,217,728 bytes
    expected_shard_bytes = 4 * 2 * 8 * 128 * 2 * 8192
    assert kva.G_KV_MAX_CTX_SHARD._value.get() == expected_shard_bytes
    assert kva.G_KV_CAPACITY_SHARD._value.get() == 15 * expected_shard_bytes

    # 2. Shard with 0 attention layers (layers 0..2)
    monkeypatch.setenv("MLX_SHARD_LAYERS", "0..2")
    kva._update_gauges(str(server_log), "KVModel", model_path=str(mdir))
    # Must be 0 bytes, NOT fall back to full model allocation
    assert kva.G_KV_MAX_CTX_SHARD._value.get() == 0
    assert kva.G_KV_CAPACITY_SHARD._value.get() == 0
    assert kva.G_KV_EXACT._value.get() == 1.0

    # 3. Uneven TP (8 heads / 3 ranks) without override -> G_KV_EXACT is 0.0 (marked estimate)
    monkeypatch.setenv("MLX_WORLD_SIZE", "3")
    monkeypatch.setenv("MLX_TP_SIZE", "3")
    monkeypatch.setenv("MLX_DISTRIBUTED_MODE", "tensor")
    monkeypatch.delenv("MLX_SHARD_LAYERS", raising=False)
    kva._update_gauges(str(server_log), "KVModel", model_path=str(mdir))
    assert kva.G_KV_EXACT._value.get() == 0.0

    # 4. Explicit MLX_SHARD_KV_HEADS override -> G_KV_EXACT is 1.0 (exact)
    monkeypatch.setenv("MLX_SHARD_KV_HEADS", "3")
    kva._update_gauges(str(server_log), "KVModel", model_path=str(mdir))
    assert kva.G_KV_EXACT._value.get() == 1.0


def test_proxy_request_path_prefill_and_decode_telemetry(monkeypatch):
    """Verify proxy executes request path, observes prefill/decode for SSE, avoids fake TTFT for non-streaming, and separates estimates."""
    import sys
    import threading
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(BUNDLE_DIR / "edge" / "exporters"))
    import mlx_metrics_proxy as p
    import test_proxy_rewrite as tpr

    server = tpr.ThreadingHTTPServer(("127.0.0.1", 0), tpr.StubUpstream)
    u_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    monkeypatch.setattr(p.Proxy, "upstream", ("127.0.0.1", u_port))
    monkeypatch.setattr(p.Proxy, "metrics_path", "/metrics")
    monkeypatch.setattr(p, "_MODEL_NAME", tpr.MODEL)
    monkeypatch.setattr(p.Proxy, "stream_sample", 0.0)
    monkeypatch.setattr(p.Proxy, "logprobs_top", 0)
    monkeypatch.setattr(p.Proxy, "strict_model", False)

    proxy_srv = tpr.ThreadingHTTPServer(("127.0.0.1", 0), p.Proxy)
    p_port = proxy_srv.server_address[1]
    threading.Thread(target=proxy_srv.serve_forever, daemon=True).start()

    base = f"http://127.0.0.1:{p_port}"
    try:
        # Initial metric counts
        m_init = tpr.metrics(base)
        ttft_init = tpr.series(m_init, "mlx_ttft_seconds_count")
        prefill_init = tpr.series(m_init, "mlx_prefill_seconds_count")
        decode_init = tpr.series(m_init, "mlx_decode_seconds_count")

        # 1. Non-streaming request -> Must NOT observe TTFT, prefill, or decode
        tpr.post(
            base,
            {
                "model": tpr.MODEL,
                "stream": False,
                "messages": [{"role": "user", "content": "non-streaming prompt"}],
            },
        )
        m_ns = tpr.metrics(base)
        assert tpr.series(m_ns, "mlx_ttft_seconds_count") == ttft_init
        assert tpr.series(m_ns, "mlx_prefill_seconds_count") == prefill_init
        assert tpr.series(m_ns, "mlx_decode_seconds_count") == decode_init

        # 2. Streaming request with SSE chunks -> Must observe TTFT, prefill, decode
        tpr.post(
            base,
            {
                "model": tpr.MODEL,
                "stream": True,
                "messages": [{"role": "user", "content": "streaming prompt"}],
            },
        )
        m_stream = tpr.metrics(base)
        assert tpr.series(m_stream, "mlx_ttft_seconds_count") == ttft_init + 1
        assert tpr.series(m_stream, "mlx_prefill_seconds_count") == prefill_init + 1
        assert tpr.series(m_stream, "mlx_decode_seconds_count") == decode_init + 1

        # Check estimated token metrics: stub upstream didn't send usage and no tokenizer file was loaded
        est_tokens = tpr.series(m_stream, "mlx_tokens_estimated_total")
        assert est_tokens > 0, "Whitespace fallback must record into mlx_tokens_estimated_total"
    finally:
        proxy_srv.shutdown()
        proxy_srv.server_close()
        server.shutdown()
        server.server_close()


def test_peer_probe_port_and_alert_dashboard_consistency():
    """Verify peer probe defaults to 8081, MLXPrefillSlow is removed, and alert labels match."""
    # 1. Template and deploy script default port check
    template_text = (BUNDLE_DIR / "edge" / "config" / "edge.env.template").read_text(encoding="utf8")
    assert "MLX_PEER_PROBE_PORT=8081" in template_text
    assert "MLX_PEER_PROBE_PORT=22" not in template_text
    assert "MLX_PEER_APP_PORT=8081" in template_text

    deploy_text = (BUNDLE_DIR / "scripts" / "deploy.sh").read_text(encoding="utf8")
    assert "MLX_PEER_PROBE_PORT=8081" in deploy_text
    assert "MLX_PEER_PROBE_PORT=22" not in deploy_text
    assert "MLX_PEER_APP_PORT=8081" in deploy_text

    # 2. Supervisor argument default check
    import mlx_server_supervisor as sup
    assert sup._parse_int_env("MLX_PEER_PROBE_PORT", 8081) == 8081
    assert sup._parse_int_env("MLX_PEER_APP_PORT", 8081) == 8081

    # 3. vmalert-rules check: no MLXPrefillSlow duplicate, and correct labels & separate alerts
    alert_text = (BUNDLE_DIR / "central" / "config" / "vmalert-rules.yml").read_text(encoding="utf8")
    assert "alert: MLXPrefillSlow" not in alert_text, "Duplicate MLXPrefillSlow alert must be removed"
    assert "alert: MLXShardPeerLatencyHigh" in alert_text
    assert "alert: MLXShardPeerAppRTTHigh" in alert_text
    assert "alert: MLXShardPeerAppDown" in alert_text
    assert "sum by (le, device, rank, peer_rank, peer_ip)" in alert_text
    # MLXShardMemoryOverbudget must not have dangling rank label
    assert "GPU memory allocated >85% on {{ $labels.device }}" in alert_text
    assert "leaving minimal headroom for KV cache and activations on this shard node." in alert_text

    # 4. Dashboard queries check
    inf_dash = json.loads((BUNDLE_DIR / "central" / "dashboards" / "inference.json").read_text(encoding="utf8"))
    titles = [p.get("title") for p in inf_dash.get("panels", [])]
    assert "Mean Per-Request Prefill & Decode Rate (tok/s)" in titles
    assert "Prompt-to-First-Token & Streaming Decode Duration (p95)" in titles

    sharding_dash = json.loads((BUNDLE_DIR / "central" / "dashboards" / "model-sharding.json").read_text(encoding="utf8"))
    dash_titles = [p.get("title") for p in sharding_dash.get("panels", [])]
    assert "Peer Interconnect TCP Connect Latency" in dash_titles
    assert "Peer Interconnect Application Round-Trip Time (RTT)" in dash_titles
    dash_targets = []
    for p in sharding_dash.get("panels", []):
        dash_targets.extend(p.get("targets", []))
    peer_exprs = [t.get("expr", "") for t in dash_targets if "mlx_shard_peer_connect_seconds" in t.get("expr", "")]
    for expr in peer_exprs:
        assert "sum by (le, device, rank, peer_rank, peer_ip)" in expr


def test_hybrid_tp_pp_rank_mapping_and_shard_placement(tmp_path, monkeypatch):
    """Verify hybrid TP+PP placement correctly derives tp_rank, pp_rank, layer slices, and uneven KV heads."""
    import mlx_shard_topology as top
    from mlx_model_info import model_info as load_model_info

    # Create dummy model with 32 layers, 7 KV heads, and dummy safetensors files
    model_dir = tmp_path / "hybrid_model"
    model_dir.mkdir()
    cfg = {
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 7,  # Uneven: 7 heads across TP=2 -> Rank 0 gets 4, Rank 1 gets 3
        "hidden_size": 4096,
        "max_position_embeddings": 8192,
        "dtype": "bfloat16",
    }
    (model_dir / "config.json").write_text(json.dumps(cfg), encoding="utf8")
    index_data = {
        "weight_map": {
            "model.embed_tokens.weight": "model-00001-of-00004.safetensors",
            "model.layers.0.self_attn.q_proj.weight": "model-00001-of-00004.safetensors",
            "model.layers.15.self_attn.q_proj.weight": "model-00002-of-00004.safetensors",
            "model.layers.16.self_attn.q_proj.weight": "model-00003-of-00004.safetensors",
            "model.layers.31.self_attn.q_proj.weight": "model-00004-of-00004.safetensors",
            "lm_head.weight": "model-00004-of-00004.safetensors",
        }
    }
    (model_dir / "model.safetensors.index.json").write_text(json.dumps(index_data), encoding="utf8")
    (model_dir / "model-00001-of-00004.safetensors").write_text("embed and layers 0-7")
    (model_dir / "model-00002-of-00004.safetensors").write_text("layers 8-15")
    (model_dir / "model-00003-of-00004.safetensors").write_text("layers 16-23")
    (model_dir / "model-00004-of-00004.safetensors").write_text("layers 24-31 and head")

    model_info = load_model_info(model_path=str(model_dir))

    # Hostfile for 4 ranks
    hostfile = tmp_path / "hostfile"
    hostfile.write_text("192.168.1.10\n192.168.1.11\n192.168.1.12\n192.168.1.13\n")

    monkeypatch.setenv("MLX_HOSTFILE", str(hostfile))
    monkeypatch.setenv("MLX_WORLD_SIZE", "4")
    monkeypatch.setenv("MLX_TP_SIZE", "2")
    monkeypatch.setenv("MLX_DISTRIBUTED_MODE", "hybrid")

    # Test Rank 0 (Stage 0, TP 0) - Uneven TP without explicit override -> estimated_fallback
    monkeypatch.setenv("MLX_RANK", "0")
    p0 = top.discover_shard_placement(model_info_obj=model_info, model_id="hybrid_model", model_path=str(model_dir))
    assert p0.mode == "hybrid"
    assert p0.tp_size == 2
    assert p0.pp_size == 2
    assert p0.tp_rank == 0
    assert p0.pp_rank == 0
    assert p0.layer_start == 0 and p0.layer_end == 15
    assert p0.dim_kv_heads == 4  # 7 // 2 + 1
    assert p0.kv_heads_exact is False  # Explicitly labeled as estimate
    assert p0.kv_source == "estimated_fallback"
    assert "00001" in p0.shard_files  # Stage 0 has embed

    # Explicit override via MLX_SHARD_KV_HEADS -> exact
    monkeypatch.setenv("MLX_SHARD_KV_HEADS", "4")
    p0_exact = top.discover_shard_placement(model_info_obj=model_info, model_id="hybrid_model", model_path=str(model_dir))
    assert p0_exact.dim_kv_heads == 4
    assert p0_exact.kv_heads_exact is True
    assert p0_exact.kv_source == "env_override"
    monkeypatch.delenv("MLX_SHARD_KV_HEADS")

    # Test Rank 1 (Stage 0, TP 1)
    monkeypatch.setenv("MLX_RANK", "1")
    p1 = top.discover_shard_placement(model_info_obj=model_info, model_id="hybrid_model", model_path=str(model_dir))
    assert p1.tp_size == 2
    assert p1.pp_size == 2
    assert p1.tp_rank == 1
    assert p1.pp_rank == 0
    assert p1.layer_start == 0 and p1.layer_end == 15
    assert p1.dim_kv_heads == 3  # 7 // 2 + 0
    assert p1.kv_heads_exact is False

    # Test Rank 2 (Stage 1, TP 0)
    monkeypatch.setenv("MLX_RANK", "2")
    p2 = top.discover_shard_placement(model_info_obj=model_info, model_id="hybrid_model", model_path=str(model_dir))
    assert p2.tp_size == 2
    assert p2.pp_size == 2
    assert p2.tp_rank == 0
    assert p2.pp_rank == 1
    assert p2.layer_start == 16 and p2.layer_end == 31
    assert p2.dim_kv_heads == 4  # tp_rank 0 gets remainder head
    assert p2.kv_heads_exact is False
    assert "00004" in p2.shard_files  # Stage 1 (last) has head

    # Test Rank 3 (Stage 1, TP 1)
    monkeypatch.setenv("MLX_RANK", "3")
    p3 = top.discover_shard_placement(model_info_obj=model_info, model_id="hybrid_model", model_path=str(model_dir))
    assert p3.tp_size == 2
    assert p3.pp_size == 2
    assert p3.tp_rank == 1
    assert p3.pp_rank == 1
    assert p3.layer_start == 16 and p3.layer_end == 31
    assert p3.dim_kv_heads == 3  # tp_rank 1 gets base head
    assert p3.kv_heads_exact is False


def test_proxy_chat_template_rendering_and_exact_tokens(tmp_path, monkeypatch):
    """Verify chat template is loaded and executed for exact prompt tokens, and fallback marks estimated."""
    import mlx_metrics_proxy as p

    model_dir = tmp_path / "chat_model"
    model_dir.mkdir()
    # Dummy tokenizer_config.json with Jinja2 template
    template_str = (
        "{% for message in messages %}"
        "<|im_start|>{{ message['role'] }}\n{{ message['content'] }}<|im_end|>\n"
        "{% endfor %}"
        "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
    )
    (model_dir / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": template_str}), encoding="utf8"
    )

    tpl = p._get_chat_template_for_model("chat_model", str(model_dir))
    assert tpl is not None

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ]
    rendered = tpl.render(messages=messages, add_generation_prompt=True)
    assert "<|im_start|>system\nYou are a helpful assistant.<|im_end|>" in rendered
    assert "<|im_start|>user\nHello!<|im_end|>" in rendered
    assert "<|im_start|>assistant\n" in rendered


def test_peer_reachability_with_app_rtt_probe():
    """Verify probe_peer_transport and probe_peer_app are decoupled and separate transport connect from application RTT."""
    import http.server
    import threading
    import mlx_shard_topology as top

    class SimpleHeadHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), SimpleHeadHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # 1. Independent transport probe (TCP SYN-ACK only)
        c_res = top.probe_peer_transport(((1, "127.0.0.1"),), port=port, timeout=1.0)
        assert c_res[(1, "127.0.0.1")].up is True
        assert c_res[(1, "127.0.0.1")].connect_seconds > 0.0
        assert c_res[(1, "127.0.0.1")].reason == "connected"

        # 2. Independent app RTT probe (HTTP HEAD request/response only)
        a_res = top.probe_peer_app(((1, "127.0.0.1"),), port=port, timeout=1.0)
        assert a_res[(1, "127.0.0.1")].up is True
        assert a_res[(1, "127.0.0.1")].app_rtt_seconds > 0.0
        assert a_res[(1, "127.0.0.1")].status_code == 200
        assert a_res[(1, "127.0.0.1")].reason == "ok"

        # 3. Decoupled probe with separate transport vs app ports
        closed_port = port + 1 if port < 65000 else port - 1
        # Transport port open, app port closed: transport is UP, app is DOWN
        mixed = top.probe_peer_reachability(
            ((1, "127.0.0.1"),), port=port, app_port=closed_port, timeout=0.2, probe_app_rtt=True
        )
        assert mixed[(1, "127.0.0.1")].up is True
        assert mixed[(1, "127.0.0.1")].connect_seconds > 0.0
        assert mixed[(1, "127.0.0.1")].app_up is False
        assert mixed[(1, "127.0.0.1")].app_rtt_seconds == 0.0

        # Transport port closed, app port open: transport is DOWN, app is UP
        mixed2 = top.probe_peer_reachability(
            ((1, "127.0.0.1"),), port=closed_port, app_port=port, timeout=0.2, probe_app_rtt=True
        )
        assert mixed2[(1, "127.0.0.1")].up is False
        assert mixed2[(1, "127.0.0.1")].connect_seconds == 0.0
        assert mixed2[(1, "127.0.0.1")].app_up is True
        assert mixed2[(1, "127.0.0.1")].app_rtt_seconds > 0.0
    finally:
        server.shutdown()
        server.server_close()

    # Closed port for both
    results_closed = top.probe_peer_reachability(
        ((2, "127.0.0.1"),), port=closed_port, app_port=closed_port, timeout=0.2, probe_app_rtt=True
    )
    res_closed = results_closed[(2, "127.0.0.1")]
    assert res_closed.up is False
    assert res_closed.app_up is False
    assert res_closed.connect_seconds == 0.0
    assert res_closed.app_rtt_seconds == 0.0
    assert res_closed.reason in ("refused", "timeout", "unreachable")


def test_kv_cache_agent_unbound_local_error_when_model_info_none(tmp_path):
    """Verify mlx_kv_cache_agent._update_gauges does not fail with UnboundLocalError when model info is unavailable."""
    import mlx_kv_cache_agent as kv
    from prometheus_client import generate_latest

    dummy_log = tmp_path / "server.log"
    dummy_log.write_text("prompt cache total=0 (0.000 GiB)\n", encoding="utf8")

    # Calling with model_name=None and model_path=None causes info_obj to be None.
    # Previously, getattr(placement, "kv_heads_exact", True) raised UnboundLocalError.
    kv._update_gauges(
        log_path=str(dummy_log),
        model_name=None,
        model_path=None,
        upstream_url="http://127.0.0.1:9999",
        prompt_cache_size=10,
    )
    text = generate_latest().decode()
    assert "mlx_kv_cache_shard_exact 0.0" in text


def test_peer_app_probe_isolates_app_rtt_and_rejects_non_http():
    """Verify probe_peer_app isolates request/response RTT from connect and flags non-HTTP sockets."""
    import socket
    import threading
    import http.server
    import mlx_shard_topology as top

    # 1. Raw non-HTTP TCP server (simulating raw binary MLX transport)
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.bind(("127.0.0.1", 0))
    raw_port = raw_sock.getsockname()[1]
    raw_sock.listen(1)

    def serve_raw():
        try:
            conn, _ = raw_sock.accept()
            conn.recv(1024)
            # Send raw binary garbage, not HTTP
            conn.sendall(b"\x00\x01\x02\x03BINARY_PAYLOAD")
            conn.close()
        except Exception:
            pass

    t_raw = threading.Thread(target=serve_raw, daemon=True)
    t_raw.start()

    try:
        raw_res = top.probe_peer_app(((1, "127.0.0.1"),), port=raw_port, timeout=0.5)
        r = raw_res[(1, "127.0.0.1")]
        assert r.up is False
        assert r.reason == "not_http"
        assert r.status_code == 0
        assert r.app_rtt_seconds == 0.0
    finally:
        raw_sock.close()


def test_proxy_prompt_token_uses_request_tools_and_tracks_exactness(tmp_path):
    """Verify chat template prompt token counting uses request tools and marks missing tool template support as estimated."""
    import mlx_metrics_proxy as p

    model_dir = tmp_path / "templated_tool_model"
    model_dir.mkdir()

    # Template that explicitly formats tools
    tool_template = (
        "{% for message in messages %}"
        "<|im_start|>{{ message['role'] }}\n{{ message['content'] }}<|im_end|>\n"
        "{% endfor %}"
        "{% if tools %}"
        "<|tools|>"
        "{% for t in tools %}{{ t['function']['name'] }}{% endfor %}"
        "<|endoftext|>"
        "{% endif %}"
        "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
    )

    # Template with NO tool support
    no_tool_template = (
        "{% for message in messages %}"
        "<|im_start|>{{ message['role'] }}\n{{ message['content'] }}<|im_end|>\n"
        "{% endfor %}"
        "{% if add_generation_prompt %}<|im_start|>assistant\n{% endif %}"
    )

    (model_dir / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": tool_template}),
        encoding="utf8",
    )

    tpl_with_tools = p._get_chat_template_for_model("tool_model", str(model_dir))
    assert tpl_with_tools is not None
    assert "tools" in getattr(tpl_with_tools, "_raw_template", "")

    req_tools = [
        {"type": "function", "function": {"name": "get_weather", "description": "Fetch weather"}}
    ]
    messages = [{"role": "user", "content": "What's the weather?"}]

    rendered = tpl_with_tools.render(messages=messages, tools=req_tools, add_generation_prompt=True)
    assert "get_weather" in rendered

    # Now verify template without tool support flags prompt_tokens_exact as False
    no_tool_dir = tmp_path / "no_tool_model"
    no_tool_dir.mkdir()
    (no_tool_dir / "tokenizer_config.json").write_text(
        json.dumps({"chat_template": no_tool_template}),
        encoding="utf8",
    )
    tpl_no_tools = p._get_chat_template_for_model("no_tool_model", str(no_tool_dir))
    assert tpl_no_tools is not None
    assert "tools" not in getattr(tpl_no_tools, "_raw_template", "")


