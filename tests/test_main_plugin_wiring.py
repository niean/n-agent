from __future__ import annotations

import pytest

from app.application.mcp_service import McpToolExecutor
from app.domain.tool import ToolSourceType
from app.main import build_application_services


@pytest.fixture(autouse=True)
def _disable_artifacts(monkeypatch):
    """Disable the artifact subsystem to avoid requiring /app/locals/artifacts."""
    monkeypatch.setenv("N_AGENT_ARTIFACTS_ENABLED", "false")


def test_plugin_service_wired():
    services = build_application_services()
    assert services.plugin_service is not None


def test_plugin_hello_tool_exposed():
    services = build_application_services()
    defs = services.tool_service.list_definitions()
    plugin_defs = [d for d in defs if d.source_type is ToolSourceType.PLUGIN]
    assert any(d.name == "hello" for d in plugin_defs), "hello plugin tool not exposed"


def test_plugin_routes_bound_to_composite_executor():
    services = build_application_services()
    executor = services.tool_service.executor
    assert "hello" in executor.routes
    assert isinstance(executor.fallback, McpToolExecutor)


def test_plugin_call_tool_hello_handler():
    import asyncio
    services = build_application_services()
    result = asyncio.run(services.plugin_service.call_tool("hello", {"name": "Alice"}, None))
    assert result.status.value == "success"
    assert result.content == {"message": "Hello, Alice!"}


def test_graph_runner_wired_with_plugin_hook_dispatcher():
    services = build_application_services()
    assert services.chat_service.graph_runner._hook_dispatcher is services.plugin_service


def test_session_service_wired_with_plugin_hook_dispatcher():
    services = build_application_services()
    assert services.session_service._hook_dispatcher is services.plugin_service
