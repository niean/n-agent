from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_docker_compose_config():
    compose = (ROOT / "docker-compose.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    env = (ROOT / ".env.example").read_text()

    assert "n-agent:" in compose
    assert '"8201:8201"' in compose
    assert "network_mode: host" not in compose
    assert "env_file:" in compose
    assert "path: .env" in compose
    assert "required: false" in compose
    assert "/Users/niean/install/n-agent/locals:/app/locals" in compose
    assert "/Users/niean/install/n-agent/workspace:/workspace" in compose
    assert "N_AGENT_SQLITE_PATH: /app/locals/agent.db" in compose
    assert "N_AGENT_WORKSPACE_ROOT: /workspace" in compose
    assert "FROM python:3.11-slim" in dockerfile
    assert '"uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8201"' in dockerfile
    assert "N_AGENT_SQLITE_PATH" in env
    assert "N_AGENT_WORKSPACE_ROOT" in env
