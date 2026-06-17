import pytest

from app.application.mcp_service import McpClient, McpService, McpSiteInput, McpToolExecutor, mcp_management_tool_definitions
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.mcp import McpProbeResult, McpRemoteTool, McpSiteValidationError, McpTransportType
from app.domain.tool import ToolCallRequest, ToolResult, ToolResultStatus
from app.infrastructure.registry.sqlite_mcp_registry import SQLiteMcpSiteRegistry


class FakeClient(McpClient):
    def __init__(self):
        self.calls = []

    async def probe_tools(self, site):
        self.calls.append(("probe", site.url, site.command, site.args, site.env))
        return McpProbeResult([McpRemoteTool("search", "Search", {"type": "object", "properties": {}})])

    async def call_tool(self, site, remote_name, arguments):
        self.calls.append(("call", remote_name, arguments))
        return {"remote": remote_name, "arguments": arguments}


class FailingClient(McpClient):
    async def probe_tools(self, site):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("HTTP 404: not found")])


class TimeoutCallClient(FakeClient):
    async def call_tool(self, site, remote_name, arguments):
        raise TimeoutError


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
async def test_mcp_service_preserves_stdio_config_on_create_and_update(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    service = McpService(registry, FakeClient(), None)

    site = await service.create_site_with_probe(
        McpSiteInput(
            "local",
            "",
            transport_type=McpTransportType.STDIO,
            command="uvx",
            args=["server"],
            env={"TOKEN": "x"},
        ),
        None,
    )
    updated = await service.update_site(
        site.id,
        McpSiteInput(
            "local",
            "",
            transport_type=McpTransportType.STDIO,
            command="npx",
            args=["other"],
            env={"TOKEN": "y"},
        ),
    )

    assert site.command == "uvx"
    assert site.args == ["server"]
    assert site.env == {"TOKEN": "x"}
    assert updated.command == "npx"
    assert updated.args == ["other"]
    assert updated.env == {"TOKEN": "y"}


@pytest.mark.asyncio
async def test_mcp_service_validates_stdio_config(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    service = McpService(registry, FakeClient(), None)

    with pytest.raises(McpSiteValidationError):
        await service.probe_site(McpSiteInput("local", "", transport_type=McpTransportType.STDIO))
    with pytest.raises(McpSiteValidationError):
        await service.probe_site(McpSiteInput("local", "", transport_type=McpTransportType.STDIO, command="uvx", args=[1]))
    with pytest.raises(McpSiteValidationError):
        await service.probe_site(McpSiteInput("local", "", transport_type=McpTransportType.STDIO, command="uvx", args="server"))
    with pytest.raises(McpSiteValidationError):
        await service.probe_site(McpSiteInput("local", "", transport_type=McpTransportType.STDIO, command="uvx", env={"TOKEN": 1}))
    with pytest.raises(McpSiteValidationError):
        await service.probe_site(McpSiteInput("local", "", transport_type=McpTransportType.STDIO, command="uvx", env=["TOKEN=x"]))


@pytest.mark.asyncio
async def test_mcp_service_probe_error_unwraps_exception_group(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    service = McpService(registry, FailingClient(), None)

    with pytest.raises(Exception) as exc:
        await service.probe_site(McpSiteInput("docs", "https://example.com/mcp"))

    assert "HTTP 404: not found" in str(exc.value)


@pytest.mark.asyncio
async def test_mcp_tool_executor_handles_empty_error_message(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    service = McpService(registry, TimeoutCallClient(), None)
    await service.create_site_with_probe(McpSiteInput("docs", "https://example.com/mcp"), None)
    executor = McpToolExecutor(service)

    result = await executor.execute(ToolCallRequest("call-1", "mcp_docs_search", {"q": "x"}))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "TimeoutError"}


@pytest.mark.asyncio
async def test_mcp_management_definitions_include_confirm_tools():
    levels = {definition.name: definition.risk_level.value for definition in mcp_management_tool_definitions()}

    assert levels["mcp_site_probe"] == "confirm"
    assert levels["mcp_site_add"] == "confirm"
    assert levels["mcp_site_refresh"] == "confirm"
    assert levels["mcp_site_list"] == "safe"
