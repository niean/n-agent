from fastapi.testclient import TestClient
from app.main import create_app

app = create_app()
client = TestClient(app)


def test_list_memory_providers():
    """List builtin/project memory providers returns 200 with provider list."""
    response = client.get("/chat/external-memory/memory-providers")
    assert response.status_code == 200
    data = response.json()
    assert "providers" in data
    assert isinstance(data["providers"], list)


def test_set_enabled():
    """Setting enabled providers returns 200."""
    response = client.post(
        "/chat/external-memory/set-enabled",
        json={"enabled": ["builtin"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
