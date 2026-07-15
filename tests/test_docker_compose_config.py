from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "docker"


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


@pytest.mark.parametrize(
    "compose_name", ["docker-compose.yml", "docker-compose.yml.example"]
)
def test_compose_host_terminal_text_and_yaml_contract_is_safe(compose_name):
    compose_path = DOCKER_DIR / compose_name
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
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
    if shutil.which("docker") is None:
        pytest.skip("docker CLI unavailable")
    compose_path = DOCKER_DIR / compose_name
    completed = subprocess.run(
        ["docker", "compose", "-f", str(compose_path), "config", "--quiet"],
        cwd=ROOT,
        capture_output=True,
        text=False,
        check=False,
    )
    assert completed.returncode == 0
