from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    with TestClient(create_app()) as c:
        yield c


def test_list_providers_empty(client):
    r = client.get("/chat/external-memory/providers")
    assert r.status_code == 200
    assert r.json() == {"providers": []}


def test_create_provider(client):
    r = client.post("/chat/external-memory/providers", json={
        "name": "my-mem0", "provider_type": "mem0",
        "base_url": "https://app.mem0.ai/v1", "api_key": "sk-x",
        "extra_config": {"user_id": "u1"},
    })
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "my-mem0"
    assert "api_key" not in data
    assert data["api_key_present"] is True


def test_create_duplicate_name(client):
    client.post("/chat/external-memory/providers", json={
        "name": "m", "provider_type": "mem0",
        "base_url": "u", "api_key": "k", "extra_config": {},
    })
    r = client.post("/chat/external-memory/providers", json={
        "name": "m", "provider_type": "honcho",
        "base_url": "u", "api_key": "k", "extra_config": {},
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "provider_duplicate"


def test_activate_provider(client):
    create = client.post("/chat/external-memory/providers", json={
        "name": "h", "provider_type": "holographic",
        "base_url": "", "api_key": None, "extra_config": {},
    })
    pid = create.json()["id"]
    r = client.post(f"/chat/external-memory/providers/{pid}/activate")
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["tool_surface_refresh_failed"] is False


def test_delete_active_provider(client):
    create = client.post("/chat/external-memory/providers", json={
        "name": "h", "provider_type": "holographic",
        "base_url": "", "api_key": None, "extra_config": {},
    })
    pid = create.json()["id"]
    client.post(f"/chat/external-memory/providers/{pid}/activate")
    r = client.delete(f"/chat/external-memory/providers/{pid}")
    assert r.status_code == 200


def test_provider_not_found(client):
    r = client.get("/chat/external-memory/providers/missing")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "provider_not_found"


def test_probe_provider(client):
    create = client.post("/chat/external-memory/providers", json={
        "name": "h", "provider_type": "holographic",
        "base_url": "", "api_key": None, "extra_config": {},
    })
    pid = create.json()["id"]
    r = client.post(f"/chat/external-memory/providers/{pid}/probe")
    assert r.status_code == 200
    assert r.json()["probe_status"] == "ok"


def test_patch_active_provider_response_includes_refresh_failed(client):
    create = client.post("/chat/external-memory/providers", json={
        "name": "h", "provider_type": "holographic",
        "base_url": "", "api_key": None, "extra_config": {"recall_mode": "hybrid"},
    })
    pid = create.json()["id"]
    client.post(f"/chat/external-memory/providers/{pid}/activate")
    resp = client.patch(
        f"/chat/external-memory/providers/{pid}",
        json={"extra_config": {"recall_mode": "context"}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "tool_surface_refresh_failed" in data
    assert data["tool_surface_refresh_failed"] is False


def test_patch_inactive_provider_response_refresh_failed_none(client):
    create = client.post("/chat/external-memory/providers", json={
        "name": "h", "provider_type": "holographic",
        "base_url": "", "api_key": None, "extra_config": {"recall_mode": "hybrid"},
    })
    pid = create.json()["id"]
    resp = client.patch(
        f"/chat/external-memory/providers/{pid}",
        json={"extra_config": {"recall_mode": "tools"}},
    )
    assert resp.status_code == 200
    assert resp.json()["tool_surface_refresh_failed"] is None
