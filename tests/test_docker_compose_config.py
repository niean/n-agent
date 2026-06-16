from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = ROOT / "docker"


def test_docker_compose_config():
    compose = (DOCKER_DIR / "docker-compose.yml.example").read_text()
    dockerfile = (DOCKER_DIR / "Dockerfile").read_text()
    env = (DOCKER_DIR / ".env.example").read_text()

    assert "n-agent:" in compose
    assert '"8201:8201"' in compose
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
    assert '"uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8201"' in dockerfile
    assert "N_AGENT_KB_ENABLED" in env
    assert "N_AGENT_KB_BASE_URL" in env
    assert "http://n-kb:8212" in env
