from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.plugin_service import PluginService, PluginToolExecutor
from app.application.tool_service import ToolService
from app.domain.plugin import Plugin, PluginKind, PluginSource
from app.domain.tool import ToolDefinition, ToolSourceType
from app.infrastructure.plugin.file_loader import PluginFileLoader, PluginFileLoaderConfig
from app.infrastructure.registry.sqlite_plugin_registry import SQLitePluginRegistry
from app.interfaces.http.dashboard import create_dashboard_router


def _make_service(tmp_path: Path) -> PluginService:
    registry = SQLitePluginRegistry(tmp_path / "test.db")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path / "plugins"))
    tool_service = ToolService.__new__(ToolService)
    tool_service.definitions = {}
    tool_service.dynamic_definitions = {}

    def list_definitions():
        return list(tool_service.definitions.values())

    def set_dynamic_definitions(key, defs):
        tool_service.dynamic_definitions[key] = {d.name: d for d in defs}

    tool_service.list_definitions = list_definitions
    tool_service.set_dynamic_definitions = set_dynamic_definitions

    import types
    settings = types.SimpleNamespace(
        plugin_tool_timeout_seconds=10,
        plugins_enabled=[],
        plugins_disabled=[],
    )
    return PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=lambda names: None,
        settings=settings,
    )


def _make_app(service: PluginService) -> FastAPI:
    app = FastAPI()
    router = create_dashboard_router(
        session_service=None,
        tool_service=None,
        model_service=None,
        health_provider=lambda: {},
        plugin_service=service,
    )
    app.include_router(router)
    return app


@pytest.mark.asyncio
async def test_list_plugins_returns_no_secret(tmp_path):
    service = _make_service(tmp_path)
    await service._registry.upsert_plugin(
        Plugin(
            id="plg-hello",
            key="hello",
            name="hello",
            source=PluginSource.BUNDLED,
            enabled=True,
        )
    )
    await service._registry.update_config("hello", {}, secret_updates={"api_key": "super-secret"})

    app = _make_app(service)
    client = TestClient(app)
    resp = client.get("/chat/plugins")
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    hello = next(item for item in data["items"] if item["key"] == "hello")
    assert hello["secret_refs"] == {"api_key": True}
    assert "super-secret" not in resp.text


@pytest.mark.asyncio
async def test_refresh_route_not_captured_by_key_path(tmp_path):
    service = _make_service(tmp_path)
    app = _make_app(service)
    client = TestClient(app)
    resp = client.post("/chat/plugins:refresh")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


@pytest.mark.asyncio
async def test_get_plugin_detail(tmp_path):
    service = _make_service(tmp_path)
    await service._registry.upsert_plugin(
        Plugin(
            id="plg-hello",
            key="hello",
            name="hello",
            source=PluginSource.BUNDLED,
            enabled=True,
            version="1.0.0",
            manifest={"name": "hello", "version": "1.0.0"},
        )
    )
    app = _make_app(service)
    client = TestClient(app)
    resp = client.get("/chat/plugins/hello")
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "hello"
    assert data["manifest"]["name"] == "hello"


@pytest.mark.asyncio
async def test_get_plugin_not_found(tmp_path):
    service = _make_service(tmp_path)
    app = _make_app(service)
    client = TestClient(app)
    resp = client.get("/chat/plugins/missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "plugin_not_found"


@pytest.mark.asyncio
async def test_set_enabled_via_http(tmp_path):
    service = _make_service(tmp_path)
    await service._registry.upsert_plugin(
        Plugin(
            id="plg-hello",
            key="hello",
            name="hello",
            source=PluginSource.BUNDLED,
            enabled=False,
        )
    )
    app = _make_app(service)
    client = TestClient(app)
    resp = client.patch("/chat/plugins/hello/enabled", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


@pytest.mark.asyncio
async def test_update_config_stores_secret(tmp_path):
    service = _make_service(tmp_path)
    await service._registry.upsert_plugin(
        Plugin(
            id="plg-hello",
            key="hello",
            name="hello",
            source=PluginSource.BUNDLED,
        )
    )
    app = _make_app(service)
    client = TestClient(app)
    resp = client.patch(
        "/chat/plugins/hello/config",
        json={"config": {"endpoint": "http://x"}, "secret_updates": {"api_key": "secret-value"}},
    )
    assert resp.status_code == 200
    list_resp = client.get("/chat/plugins")
    assert "secret-value" not in list_resp.text
