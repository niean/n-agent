from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_list_providers():
    """List providers returns 200 with empty or populated list."""
    response = client.get("/chat/external-memory/providers")
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
