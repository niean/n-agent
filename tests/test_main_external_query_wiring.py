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
    # 用 with 触发 FastAPI lifespan（startup/shutdown）
    with TestClient(create_app()) as c:
        yield c


def test_external_memory_provider_registry_initialized(client):
    # 启动后 registry 已建表
    r = client.get("/chat/external-memory/providers")
    assert r.status_code == 200


def test_no_active_provider_at_startup(client):
    r = client.get("/chat/external-memory/providers")
    assert r.json() == {"providers": []}


def test_memory_providers_still_available_with_external_query_routes(client):
    r = client.get("/chat/external-memory/memory-providers")
    assert r.status_code == 200
    names = [p["name"] for p in r.json()["providers"]]
    assert "builtin" in names


def test_active_provider_loaded_after_restart(tmp_path, monkeypatch):
    # 第一次启动 activate 一个 holographic
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    with TestClient(create_app()) as client1:
        create = client1.post("/chat/external-memory/providers", json={
            "name": "h", "provider_type": "holographic",
            "base_url": "", "api_key": None, "extra_config": {},
        })
        pid = create.json()["id"]
        client1.post(f"/chat/external-memory/providers/{pid}/activate")

    # 第二次启动，应自动装载
    with TestClient(create_app()) as client2:
        tools = client2.get("/chat/tools").json()
        tool_names = [t["name"] for t in tools]
        assert "fact_store" in tool_names
