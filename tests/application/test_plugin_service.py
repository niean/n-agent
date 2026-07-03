from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin_service import (
    PluginService,
    PluginToolRegistration,
)
from app.domain.plugin import Plugin, PluginKind, PluginSource
from app.domain.tool import ToolDefinition, ToolResultStatus, ToolSourceType


def _make_settings(plugin_tool_timeout_seconds: int = 10, **kwargs):
    s = MagicMock()
    s.plugin_tool_timeout_seconds = plugin_tool_timeout_seconds
    s.plugins_enabled = kwargs.get("plugins_enabled", [])
    s.plugins_disabled = kwargs.get("plugins_disabled", [])
    return s


def _build_service(
    *,
    registry_plugins: list[Plugin],
    loader_registrations: dict[str, list[PluginToolRegistration]] | None = None,
    loader_manifests: list | None = None,
    tool_service_defs: list[ToolDefinition] | None = None,
    settings=None,
    captured_routes: list[set[str]] | None = None,
):
    registry = AsyncMock()
    registry.list_plugins.return_value = list(registry_plugins)
    registry.get_plugin.side_effect = lambda key: next((p for p in registry_plugins if p.key == key), None)
    registry.get_secret_config.return_value = {}

    loader = AsyncMock()
    from app.application.plugin_service import PluginScanResult

    loader.scan.return_value = PluginScanResult(
        manifests=loader_manifests or [],
        registrations=loader_registrations or {},
        warnings=[],
        errors={},
        unsupported={},
    )

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = tool_service_defs or []
    captured_defs = []
    tool_service.set_dynamic_definitions = lambda key, defs: captured_defs.extend(defs)

    if captured_routes is None:
        captured_routes = []

    def refresher(names):
        captured_routes.append(set(names))

    service = PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=refresher,
        settings=settings or _make_settings(),
    )
    return service, registry, loader, tool_service, captured_defs, captured_routes


@pytest.mark.asyncio
async def test_scan_only_executes_enabled_standalone_replace_all():
    plugins = [
        Plugin(id="p1", key="hello", name="hello", source=PluginSource.BUNDLED, enabled=True, kind=PluginKind.STANDALONE),
        Plugin(id="p2", key="backend1", name="backend1", source=PluginSource.USER, enabled=True, kind=PluginKind.BACKEND),
        Plugin(id="p3", key="disabled1", name="disabled1", source=PluginSource.USER, enabled=False, kind=PluginKind.STANDALONE),
    ]
    service, registry, loader, *_ = _build_service(registry_plugins=plugins)
    await service.scan()
    registry.replace_all_plugins.assert_awaited_once()
    registry.list_plugins.assert_awaited()


@pytest.mark.asyncio
async def test_set_enabled_triggers_scan_and_refresh():
    plugin = Plugin(id="p1", key="hello", name="hello", source=PluginSource.BUNDLED, enabled=False)
    registry = AsyncMock()
    registry.get_plugin.return_value = plugin
    registry.set_enabled.return_value = plugin
    loader = AsyncMock()
    from app.application.plugin_service import PluginScanResult

    loader.scan.return_value = PluginScanResult(
        manifests=[], registrations={}, warnings=[], errors={}, unsupported={}
    )
    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    captured = []
    service = PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=lambda names: captured.append(set(names)),
        settings=_make_settings(),
    )
    await service.set_enabled("hello", True)
    registry.set_enabled.assert_awaited_once_with("hello", True)
    assert captured  # refresh called


def test_refresh_tool_surface_drops_conflicting_non_override_tools():
    static = ToolDefinition(
        name="hello",
        description="static",
        input_schema={"type": "object", "properties": {}},
        source_type=ToolSourceType.BUILTIN,
        toolset="builtin",
    )
    service, *_rest = _build_service(
        registry_plugins=[],
        tool_service_defs=[static],
    )
    service._registrations = {
        "hello": PluginToolRegistration(
            plugin_key="p1",
            name="hello",
            schema={"name": "hello", "parameters": {"type": "object"}},
            handler=lambda a, **k: "",
            override=False,
        ),
        "world": PluginToolRegistration(
            plugin_key="p1",
            name="world",
            schema={"name": "world", "parameters": {"type": "object"}},
            handler=lambda a, **k: "",
            override=False,
        ),
    }
    service._refresh_tool_surface()
    assert service._registrations["hello"].available is False
    assert service._registrations["hello"].unavailable_reason is not None
    assert service._registrations["world"].available is True


def test_refresh_tool_surface_override_static_still_unavailable_this_phase():
    static = ToolDefinition(
        name="hello",
        description="static",
        input_schema={"type": "object", "properties": {}},
        source_type=ToolSourceType.BUILTIN,
        toolset="builtin",
    )
    service, *_ = _build_service(registry_plugins=[], tool_service_defs=[static])
    service._registrations = {
        "hello": PluginToolRegistration(
            plugin_key="p1",
            name="hello",
            schema={"name": "hello", "parameters": {"type": "object"}},
            handler=lambda a, **k: "",
            override=True,
        ),
    }
    service._refresh_tool_surface()
    assert service._registrations["hello"].available is False
    assert "override" in (service._registrations["hello"].unavailable_reason or "").lower() \
        or "static" in (service._registrations["hello"].unavailable_reason or "").lower()


def test_refresh_tool_surface_requires_env_missing_makes_unavailable():
    service, *_ = _build_service(registry_plugins=[], tool_service_defs=[])
    reg = PluginToolRegistration(
        plugin_key="p1",
        name="hello",
        schema={"name": "hello", "parameters": {"type": "object"}},
        handler=lambda a, **k: "",
        requires_env=[{"name": "MISSING_API_KEY"}],
        plugin_config={},
        secret_config={},
    )
    service._registrations = {"hello": reg}
    service._refresh_tool_surface()
    assert reg.available is False
    assert "env" in (reg.unavailable_reason or "").lower()


@pytest.mark.asyncio
async def test_call_tool_returns_error_when_not_found():
    service, *_ = _build_service(registry_plugins=[])
    result = await service.call_tool("missing", {}, None)
    assert result.status is ToolResultStatus.ERROR
    assert "not found" in str(result.content).lower()


@pytest.mark.asyncio
async def test_call_tool_invokes_handler_and_wraps_dict():
    reg = PluginToolRegistration(
        plugin_key="hello",
        name="hello",
        schema={"name": "hello", "parameters": {"type": "object"}},
        handler=lambda args, **kwargs: {"message": f"Hello, {args.get('name', 'plugin')}!"},
        is_async=False,
    )
    service, *_ = _build_service(registry_plugins=[])
    service._registrations = {"hello": reg}
    result = await service.call_tool("hello", {"name": "Alice"}, None, tool_call_id="tc-42")
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == {"message": "Hello, Alice!"}
    assert result.tool_call_id == "tc-42"


@pytest.mark.asyncio
async def test_call_tool_async_handler_awaited():
    async def async_handler(args, **kwargs):
        return {"ok": True}

    reg = PluginToolRegistration(
        plugin_key="hello",
        name="hello_async",
        schema={"name": "hello_async", "parameters": {"type": "object"}},
        handler=async_handler,
        is_async=True,
    )
    service, *_ = _build_service(registry_plugins=[])
    service._registrations = {"hello_async": reg}
    result = await service.call_tool("hello_async", {}, None)
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == {"ok": True}


@pytest.mark.asyncio
async def test_call_tool_handler_exception_returns_error():
    def bad_handler(args, **kwargs):
        raise RuntimeError("boom")

    reg = PluginToolRegistration(
        plugin_key="hello",
        name="hello",
        schema={"name": "hello", "parameters": {"type": "object"}},
        handler=bad_handler,
    )
    service, *_ = _build_service(registry_plugins=[])
    service._registrations = {"hello": reg}
    result = await service.call_tool("hello", {}, None)
    assert result.status is ToolResultStatus.ERROR
    assert "boom" in str(result.content)


@pytest.mark.asyncio
async def test_plugin_tool_executor_delegates_to_service():
    from unittest.mock import ANY
    from app.application.plugin_service import PluginToolExecutor
    from app.domain.tool import ToolCallRequest, ToolExecutionContext

    service = AsyncMock()
    service.call_tool.return_value = __import__(
        "app.domain.tool", fromlist=["ToolResult"]
    ).ToolResult("tc1", "hello", ToolResultStatus.SUCCESS, {"message": "hi"})
    executor = PluginToolExecutor(service=service)
    result = await executor.execute(
        ToolCallRequest(id="tc1", name="hello", arguments={"name": "world"}),
        ToolExecutionContext(session_id="s1", metadata={}),
    )
    service.call_tool.assert_awaited_once_with("hello", {"name": "world"}, ANY, "tc1")
    assert result.status is ToolResultStatus.SUCCESS
