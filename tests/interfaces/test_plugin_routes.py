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
    tool_service._suppressed_static_names = {}

    def list_definitions():
        return list(tool_service.definitions.values())

    def set_dynamic_definitions(key, defs):
        tool_service.dynamic_definitions[key] = {d.name: d for d in defs}

    def replace_dynamic_definitions(key, defs, override_static_names=None):
        tool_service.dynamic_definitions[key] = {d.name: d for d in defs}
        tool_service._suppressed_static_names[key] = set(override_static_names or [])

    tool_service.list_definitions = list_definitions
    tool_service.set_dynamic_definitions = set_dynamic_definitions
    tool_service.replace_dynamic_definitions = replace_dynamic_definitions

    import types
    settings = types.SimpleNamespace(
        plugin_tool_timeout_seconds=10,
        plugins_enabled=[],
        plugins_disabled=[],
        plugins_override_allowlist=[],
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
async def test_get_plugin_detail_returns_manifest_and_dependency_status(tmp_path):
    """T13 S1: detail API returns raw manifest + capabilities.dependency_status
    with pip/external/requires_plugins/warnings keys."""
    service = _make_service(tmp_path)
    dependency_status = {
        "pip": [
            {
                "spec": "Pillow>=10",
                "name": "Pillow",
                "status": "missing",
                "installed_version": None,
                "diagnostic": "missing pip dependency: Pillow; run: pip install 'Pillow>=10'",
            }
        ],
        "requires_plugins": [
            {"key": "core", "available": False, "reason": "missing", "diagnostic": "missing required plugin: core"}
        ],
        "external": [
            {"name": "ffmpeg", "install": "apt-get install -y ffmpeg", "check": "ffmpeg -version"}
        ],
        "warnings": ["dependency_version_check_unavailable"],
    }
    raw_manifest = {
        "name": "hello",
        "version": "1.0.0",
        "pip_dependencies": ["Pillow>=10"],
        "external_dependencies": [{"name": "ffmpeg", "install": "apt-get install -y ffmpeg", "check": "ffmpeg -version"}],
        "requires_plugins": ["core"],
    }
    await service._registry.upsert_plugin(
        Plugin(
            id="plg-hello",
            key="hello",
            name="hello",
            source=PluginSource.BUNDLED,
            enabled=True,
            version="1.0.0",
            manifest=raw_manifest,
            capabilities={"unsupported": [], "provides_tools": [], "dependency_status": dependency_status},
        )
    )
    app = _make_app(service)
    client = TestClient(app)
    resp = client.get("/chat/plugins/hello")
    assert resp.status_code == 200
    data = resp.json()
    assert data["key"] == "hello"
    # detail returns raw manifest
    assert data["manifest"] == raw_manifest
    # detail returns capabilities.dependency_status with all 4 categories
    dep_status = data["capabilities"]["dependency_status"]
    assert set(dep_status.keys()) >= {"pip", "external", "requires_plugins", "warnings"}
    assert dep_status["pip"][0]["spec"] == "Pillow>=10"
    assert dep_status["pip"][0]["diagnostic"] == "missing pip dependency: Pillow; run: pip install 'Pillow>=10'"
    assert dep_status["external"][0]["name"] == "ffmpeg"
    assert dep_status["external"][0]["install"] == "apt-get install -y ffmpeg"
    assert dep_status["requires_plugins"][0]["key"] == "core"
    assert dep_status["requires_plugins"][0]["diagnostic"] == "missing required plugin: core"
    assert dep_status["warnings"] == ["dependency_version_check_unavailable"]


@pytest.mark.asyncio
async def test_list_plugins_omits_manifest_big_field(tmp_path):
    """T13 S1: list API uses to_public_view (no manifest big field); detail
    endpoint is the only one that returns the raw manifest."""
    service = _make_service(tmp_path)
    raw_manifest = {"name": "hello", "version": "1.0.0", "pip_dependencies": ["Pillow>=10"]}
    await service._registry.upsert_plugin(
        Plugin(
            id="plg-hello",
            key="hello",
            name="hello",
            source=PluginSource.BUNDLED,
            enabled=True,
            manifest=raw_manifest,
            capabilities={"dependency_status": {"pip": [], "external": [], "requires_plugins": [], "warnings": []}},
        )
    )
    app = _make_app(service)
    client = TestClient(app)
    list_resp = client.get("/chat/plugins")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1
    list_item = items[0]
    # list view must NOT include manifest (the big raw field)
    assert "manifest" not in list_item
    # list view still carries capabilities.dependency_status (already in view)
    assert "dependency_status" in list_item["capabilities"]
    # detail view DOES include manifest
    detail_resp = client.get("/chat/plugins/hello")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert detail["manifest"] == raw_manifest


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
