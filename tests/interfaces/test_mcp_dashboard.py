from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.mcp_service import McpService
from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.mcp import McpProbeResult, McpRemoteTool
from app.domain.provider import ModelInfo
from app.domain.tool import ToolCallRequest, ToolResult, ToolResultStatus
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_mcp_registry import SQLiteMcpSiteRegistry
from app.interfaces.http.dashboard import create_dashboard_router


class FakeClient:
    async def probe_tools(self, site):
        return McpProbeResult([McpRemoteTool("search", "Search", {"type": "object"})])

    async def call_tool(self, site, remote_name, arguments):
        return {}


class FakeExecutor:
    async def execute(self, request: ToolCallRequest, context=None):
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


class FakeProvider:
    async def list_models(self):
        return [ModelInfo("m", "m", "fake")]

    async def supports_tools(self, model):
        return True

    async def chat(self, *args, **kwargs):
        raise NotImplementedError


def test_mcp_dashboard_routes(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    tool_service = ToolService(FakeExecutor(), builtin_tool_definitions())
    mcp_service = McpService(SQLiteMcpSiteRegistry(tmp_path / "sessions.db"), FakeClient(), tool_service)
    app = FastAPI()
    app.include_router(create_dashboard_router(
        SessionService(store),
        tool_service,
        ModelService(FakeProvider(), "m"),
        lambda: {},
        mcp_service=mcp_service,
    ))
    client = TestClient(app)

    probe = client.post("/chat/mcp/sites/probe", json={"name": "docs", "url": "https://example.com/mcp"})
    created = client.post("/chat/mcp/sites", json={"name": "docs", "url": "https://example.com/mcp"})
    site_id = created.json()["id"]
    listed = client.get("/chat/mcp/sites")
    tools = client.get(f"/chat/mcp/sites/{site_id}/tools")
    toggled = client.patch(f"/chat/mcp/sites/{site_id}/tools/{tools.json()[0]['id']}", json={"enabled": False})
    refreshed = client.post(f"/chat/mcp/sites/{site_id}/refresh")
    deleted = client.delete(f"/chat/mcp/sites/{site_id}")

    assert probe.status_code == 200
    assert created.status_code == 200
    assert listed.json()[0]["name"] == "docs"
    assert tools.status_code == 200
    assert toggled.json()["enabled"] is False
    assert refreshed.status_code == 200
    assert deleted.status_code == 204


def test_mcp_dashboard_accepts_stdio_sites(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    tool_service = ToolService(FakeExecutor(), builtin_tool_definitions())
    mcp_service = McpService(SQLiteMcpSiteRegistry(tmp_path / "sessions.db"), FakeClient(), tool_service)
    app = FastAPI()
    app.include_router(create_dashboard_router(
        SessionService(store),
        tool_service,
        ModelService(FakeProvider(), "m"),
        lambda: {},
        mcp_service=mcp_service,
    ))
    client = TestClient(app)

    payload = {
        "name": "local",
        "transport_type": "stdio",
        "command": "uvx",
        "args": ["server"],
        "env": {"TOKEN": "x"},
    }
    probe = client.post("/chat/mcp/sites/probe", json=payload)
    created = client.post("/chat/mcp/sites", json=payload)
    listed = client.get("/chat/mcp/sites")

    assert probe.status_code == 200
    assert created.status_code == 200
    assert created.json()["transport_type"] == "stdio"
    assert created.json()["command"] == "uvx"
    assert created.json()["args"] == ["server"]
    assert created.json()["env"] == {"TOKEN": "x"}
    assert listed.json()[0]["command"] == "uvx"
