from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "docker"


def _load_compose(compose_name: str) -> dict:
    compose_path = DOCKER_DIR / compose_name
    if compose_name == "docker-compose.yml" and not compose_path.exists():
        pytest.skip(
            "machine-local docker/docker-compose.yml is absent in a clean checkout"
        )
    return yaml.safe_load(compose_path.read_text(encoding="utf-8"))


def _parse_env_assignments(text: str) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        assert separator, f"invalid env assignment: {key}"
        assert key not in assignments, f"duplicate env assignment: {key}"
        assignments[key] = value
    return assignments


def _published_target_ranges(service: dict) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for port in service.get("ports", []):
        if isinstance(port, dict):
            target = str(port["target"])
        else:
            target = str(port).rsplit("/", 1)[0].rsplit(":", 1)[-1]
        first, separator, last = target.partition("-")
        start = int(first)
        end = int(last) if separator else start
        ranges.append((min(start, end), max(start, end)))
    return ranges


def _assert_sensitive_ports_not_published(document: dict) -> None:
    sensitive_ports = {8766, 9222, 6080}
    for service in document["services"].values():
        for start, end in _published_target_ranges(service):
            assert not any(
                start <= port <= end for port in sensitive_ports
            ), f"sensitive target port published: {start}-{end}"


def _host_browser_token_mount(service: dict) -> str:
    return next(
        item
        for item in service["volumes"]
        if ":/app/locals/host-browser.token:" in item
    )


def test_docker_compose_config():
    compose = (DOCKER_DIR / "docker-compose.yml.example").read_text()
    dockerfile = (DOCKER_DIR / "Dockerfile").read_text()
    env = (DOCKER_DIR / ".env.example").read_text()

    assert "n-agent:" in compose
    assert '"8201:8201"' in compose
    assert "healthcheck:" in compose
    assert "http://127.0.0.1:8201/health" in compose
    assert "network_mode: host" not in compose
    assert "build:" in compose
    assert "context: .." in compose
    assert "dockerfile: docker/Dockerfile" in compose
    assert "env_file:" in compose
    assert "path: ./.env" in compose
    assert "required: false" in compose
    assert "/app/locals" in compose
    assert "/workspace" in compose
    assert "TZ: Asia/Shanghai" in compose
    assert "N_AGENT_SQLITE_PATH: /app/locals/sessions.db" in compose
    assert "N_AGENT_WORKSPACE_ROOT: /workspace" in compose
    assert "n-kb_default" in compose
    assert "external: true" in compose
    assert "FROM python:3.11-slim" in dockerfile
    assert "ENV TZ=Asia/Shanghai" in dockerfile
    assert "tzdata" in dockerfile
    assert "/etc/localtime" in dockerfile
    assert '"uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8201"' in dockerfile
    assert "N_AGENT_KB_ENABLED" in env
    assert "N_AGENT_KB_BASE_URL" in env
    assert "http://n-kb:8212" in env


def test_env_example_documents_host_cdp_without_enabling_browser_by_default():
    env = _parse_env_assignments(
        (DOCKER_DIR / ".env.example").read_text(encoding="utf-8")
    )
    assert env["N_AGENT_BROWSER_ENABLED"] == "false"
    assert env["N_AGENT_BROWSER_DEFAULT_BACKEND"] == "host_cdp"
    assert (
        env["N_AGENT_BROWSER_HOST_BRIDGE_URL"]
        == "http://host.docker.internal:8766"
    )
    assert (
        env["N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH"]
        == "/app/locals/host-browser.token"
    )
    assert env["N_AGENT_BROWSER_TRUSTED_DEV"] == "true"


def test_env_assignment_parser_rejects_duplicate_conflicts():
    with pytest.raises(AssertionError, match="duplicate env assignment"):
        _parse_env_assignments(
            "N_AGENT_BROWSER_ENABLED=false\n"
            "N_AGENT_BROWSER_ENABLED=true\n"
        )


def test_example_compose_adds_host_bridge_wiring_without_changing_topology():
    document = _load_compose("docker-compose.yml.example")
    assert set(document["services"]) == {"n-agent"}
    assert set(document["networks"]) == {"n-kb"}

    service = document["services"]["n-agent"]
    assert service["ports"] == ["8201:8201"]
    assert service["networks"] == ["default", "n-kb"]
    assert "depends_on" not in service
    environment = service["environment"]
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_URL"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_URL:-http://host.docker.internal:8766}"
    )
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH:-/app/locals/host-browser.token}"
    )
    assert (
        environment["N_AGENT_BROWSER_TRUSTED_DEV"]
        == "${N_AGENT_BROWSER_TRUSTED_DEV:-false}"
    )
    assert (
        environment["N_AGENT_BROWSER_DEFAULT_BACKEND"]
        == "${N_AGENT_BROWSER_DEFAULT_BACKEND:-container}"
    )
    assert _host_browser_token_mount(service).endswith(":ro")


def test_current_machine_compose_adds_host_bridge_wiring_without_forcing_backend():
    document = _load_compose("docker-compose.yml")
    assert set(document["services"]) == {"n-agent", "browser"}
    assert set(document["networks"]) == {"default", "n-kb"}

    service = document["services"]["n-agent"]
    assert service["ports"] == ["8201:8201"]
    assert service["networks"] == ["default", "n-kb"]
    assert service["depends_on"] == {"browser": {"condition": "service_healthy"}}
    environment = service["environment"]
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_URL"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_URL:-http://host.docker.internal:8766}"
    )
    assert (
        environment["N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH"]
        == "${N_AGENT_BROWSER_HOST_BRIDGE_TOKEN_PATH:-/app/locals/host-browser.token}"
    )
    assert (
        environment["N_AGENT_BROWSER_TRUSTED_DEV"]
        == "${N_AGENT_BROWSER_TRUSTED_DEV:-false}"
    )
    assert "N_AGENT_BROWSER_DEFAULT_BACKEND" not in environment
    assert (
        environment["N_AGENT_BROWSER_CONTAINER_NOVNC_ENDPOINT"]
        == "${N_AGENT_BROWSER_CONTAINER_NOVNC_ENDPOINT:-http://browser:6080}"
    )
    assert _host_browser_token_mount(service).endswith(":ro")
    browser = document["services"]["browser"]
    assert browser["networks"] == {
        "default": {"ipv4_address": "172.19.0.10"}
    }
    assert browser["expose"] == ["9223", "6080"]
    assert "ports" not in browser


def test_container_browser_uses_4_by_3_viewport():
    """Container Xvfb and Chromium use a 1280x960 viewport."""
    document = _load_compose("docker-compose.yml")
    browser = document["services"]["browser"]
    assert browser["environment"]["SCREEN_WIDTH"] == 1280
    assert browser["environment"]["SCREEN_HEIGHT"] == 960

    browser_dockerfile = (DOCKER_DIR / "browser" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    runtime = (DOCKER_DIR / "browser" / "profile_runtime.py").read_text(
        encoding="utf-8"
    )
    assert "SCREEN_WIDTH=1280" in browser_dockerfile
    assert "SCREEN_HEIGHT=960" in browser_dockerfile
    assert 'HEIGHT = os.environ.get("SCREEN_HEIGHT", "960")' in runtime

@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_compose_host_bridge_is_not_exposed_or_given_host_networking(compose_name):
    document = _load_compose(compose_name)
    serialized = (DOCKER_DIR / compose_name).read_text(encoding="utf-8")
    for service in document["services"].values():
        assert "network_mode" not in service
        assert "extra_hosts" not in service
    _assert_sensitive_ports_not_published(document)
    assert "network_mode: host" not in serialized
    assert "host-browser-bridge" not in document["services"]
    assert "browser-host" not in document["services"]


@pytest.mark.parametrize(
    "ports",
    [
        ["127.0.0.1:9222:9222"],
        ["12345:9222"],
        [{"target": 6080, "published": 12345, "protocol": "tcp", "mode": "host"}],
        ["12345-12347:9221-9223"],
    ],
)
def test_sensitive_port_helper_rejects_all_compose_publication_syntaxes(ports):
    document = {"services": {"malicious": {"ports": ports}}}
    with pytest.raises(AssertionError):
        _assert_sensitive_ports_not_published(document)


def test_machine_local_compose_is_optional_in_clean_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tests.test_docker_compose_config.DOCKER_DIR", tmp_path
    )
    with pytest.raises(
        pytest.skip.Exception, match="machine-local.*absent"
    ):
        _load_compose("docker-compose.yml")


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_compose_host_terminal_text_and_yaml_contract_is_safe(compose_name):
    compose_path = DOCKER_DIR / compose_name
    document = _load_compose(compose_name)
    service = document["services"]["n-agent"]
    environment = service["environment"]
    required_environment = {
        "N_AGENT_HOST_TERMINAL_ENABLED",
        "N_AGENT_HOST_TERMINAL_BRIDGE_URL",
        "N_AGENT_HOST_TERMINAL_POLICY_PATH",
        "N_AGENT_HOST_TERMINAL_TOKEN_PATH",
        "N_AGENT_HOST_TERMINAL_HOST_WORKSPACE_ROOT",
        "N_AGENT_HOST_TERMINAL_HOST_SKILLS_ROOT",
        "N_AGENT_HOST_TERMINAL_TOOL_TIMEOUT_SECONDS",
        "N_AGENT_HOST_TERMINAL_BRIDGE_TIMEOUT_SECONDS",
        "N_AGENT_HOST_TERMINAL_CONNECT_TIMEOUT_SECONDS",
        "N_AGENT_HOST_TERMINAL_TRANSFER_MARGIN_SECONDS",
        "N_AGENT_HOST_TERMINAL_MAX_RESPONSE_BYTES",
        "N_AGENT_HOST_TERMINAL_MAX_STDOUT_BYTES",
        "N_AGENT_HOST_TERMINAL_MAX_STDERR_BYTES",
        "N_AGENT_HOST_TERMINAL_MAX_CONCURRENCY",
    }
    assert required_environment <= set(environment)
    assert "host.docker.internal" in environment["N_AGENT_HOST_TERMINAL_BRIDGE_URL"]

    volumes = service["volumes"]
    policy_mount = next(
        item for item in volumes
        if ":/app/locals/host-terminal-policy.yaml:" in item
    )
    token_mount = next(
        item for item in volumes if ":/app/locals/host-terminal.token:" in item
    )
    assert policy_mount.endswith(":ro")
    assert token_mount.endswith(":ro")
    serialized = compose_path.read_text(encoding="utf-8")
    assert "oss.env" not in serialized.lower()
    assert not any(str(key).startswith("OSS_") for key in environment)



@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_compose_config_subprocess(compose_name):
    compose_path = DOCKER_DIR / compose_name
    if compose_name == "docker-compose.yml" and not compose_path.exists():
        pytest.skip(
            "machine-local docker/docker-compose.yml is absent in a clean checkout"
        )
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    completed = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "config", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=False,
        check=False,
    )
    assert completed.returncode == 0
