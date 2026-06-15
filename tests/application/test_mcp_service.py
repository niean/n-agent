import pytest

from app.application.mcp_service import McpClient, McpService, McpSiteInput, mcp_management_tool_definitions
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.mcp import McpProbeResult, McpRemoteTool, McpSiteValidationError
from app.domain.tool import ToolCallRequest, ToolResult, ToolResultStatus
from app.infrastructure.registry.sqlite_mcp_registry import SQLiteMcpSiteRegistry


class FakeClient(McpClient):
    def __init__(self):
        self.calls = []

    async def probe_tools(self, site):
        self.calls.append(("probe", site.url))
        return McpProbeResult([McpRemoteTool("search", "Search", {"type": "object", "properties": {}})])

    async def call_tool(self, site, remote_name, arguments):
        self.calls.append(("call", remote_name, arguments))
        return {"remote": remote_name, "arguments": arguments}


class FailingClient(McpClient):
    async def probe_tools(self, site):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("HTTP 404: not found")])


class FakeExecutor:
    async def execute(self, request, context=None):
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


@pytest.mark.asyncio
async def test_mcp_service_create_refresh_definitions_and_call(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    tool_service = ToolService(FakeExecutor(), builtin_tool_definitions() + mcp_management_tool_definitions())
    client = FakeClient()
    service = McpService(registry, client, tool_service)

    probe = await service.probe_site(McpSiteInput("docs", "https://example.com/mcp"))
    site = await service.create_site_with_probe(McpSiteInput("docs", "https://example.com/mcp"), ["search"])
    definitions = await service.list_mcp_tool_definitions()
    result = await service.call_tool(definitions[0].name, {"q": "x"})

    assert probe.tools[0].name == "search"
    assert site.name == "docs"
    assert definitions[0].source_type.value == "mcp"
    assert result == {"remote": "search", "arguments": {"q": "x"}}


@pytest.mark.asyncio
async def test_mcp_service_refresh_keeps_local_tool_name_stable(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    tool_service = ToolService(FakeExecutor(), builtin_tool_definitions() + mcp_management_tool_definitions())
    service = McpService(registry, FakeClient(), tool_service)

    site = await service.create_site_with_probe(McpSiteInput("nkb", "https://example.com/mcp"), None)
    first = (await service.list_mcp_tool_definitions())[0]
    refreshed = await service.refresh_site_tools(site.id)
    second = (await service.list_mcp_tool_definitions())[0]

    assert first.name == "mcp_nkb_search"
    assert refreshed[0].local_name == "mcp_nkb_search"
    assert second.name == "mcp_nkb_search"


@pytest.mark.asyncio
async def test_mcp_service_blocks_disabled_site(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    tool_service = ToolService(FakeExecutor(), builtin_tool_definitions())
    service = McpService(registry, FakeClient(), tool_service)
    site = await service.create_site_with_probe(McpSiteInput("docs", "https://example.com/mcp"), None)
    await service.update_site(site.id, McpSiteInput("docs", "https://example.com/mcp", enabled=False))

    assert await service.list_mcp_tool_definitions() == []
    with pytest.raises(McpSiteValidationError):
        await service.call_tool("mcp_docs_search", {})


@pytest.mark.asyncio
async def test_mcp_service_probe_error_unwraps_exception_group(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    service = McpService(registry, FailingClient(), None)

    with pytest.raises(Exception) as exc:
        await service.probe_site(McpSiteInput("docs", "https://example.com/mcp"))

    assert "HTTP 404: not found" in str(exc.value)


@pytest.mark.asyncio
async def test_mcp_management_definitions_include_confirm_tools():
    levels = {definition.name: definition.risk_level.value for definition in mcp_management_tool_definitions()}

    assert levels["mcp_site_probe"] == "confirm"
    assert levels["mcp_site_add"] == "confirm"
    assert levels["mcp_site_refresh"] == "confirm"
    assert levels["mcp_site_list"] == "safe"
