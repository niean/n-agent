import pytest

from app.domain.mcp import McpProbeStatus, McpSite, McpTool
from app.infrastructure.registry.sqlite_mcp_registry import SQLiteMcpSiteRegistry


@pytest.mark.asyncio
async def test_sqlite_mcp_registry_crud_and_tool_refresh_preserves_disabled(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    site = await registry.create_site(McpSite(name="docs", url="https://example.com/mcp"))

    assert (await registry.get_site(site.id)).name == "docs"
    assert (await registry.get_site_by_name("docs")).id == site.id

    tools = await registry.replace_site_tools(site.id, [McpTool(site.id, "search", "mcp_docs_search", "Search", {"type": "object"})])
    disabled = await registry.update_tool_enabled(site.id, tools[0].id, False)
    refreshed = await registry.replace_site_tools(site.id, [McpTool(site.id, "search", "mcp_docs_search_v2", "Search 2", {"type": "object"})])

    assert disabled.enabled is False
    assert refreshed[0].enabled is False
    assert refreshed[0].local_name == "mcp_docs_search_v2"

    await registry.update_probe_status(site.id, McpProbeStatus.SUCCESS)
    assert (await registry.get_site(site.id)).last_probe_status is McpProbeStatus.SUCCESS

    assert await registry.delete_site(site.id) is True
    assert await registry.list_tools(site.id) == []
