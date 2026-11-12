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

    # Backend and MinIO must depend on opik-init-bucket completing successfully
    backend_deps = services["backend"].get("depends_on", {})
    assert "opik-init-bucket" in backend_deps
    assert backend_deps["opik-init-bucket"]["condition"] == "service_completed_successfully"

    minio_deps = services["minio"].get("depends_on", {})
    assert "opik-init-bucket" in minio_deps
    assert minio_deps["opik-init-bucket"]["condition"] == "service_completed_successfully"


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




