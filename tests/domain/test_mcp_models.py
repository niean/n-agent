from app.domain.mcp import McpProbeStatus, McpRemoteTool, McpSite, McpTool, McpTransportType


def test_mcp_site_defaults_to_enabled_unprobed_streamable_http():
    site = McpSite(name="docs", url="https://example.com/mcp")

    assert site.enabled is True
    assert site.transport_type is McpTransportType.STREAMABLE_HTTP
    assert site.last_probe_status is McpProbeStatus.NEVER


def test_mcp_tool_and_remote_tool_hold_schema():
    tool = McpTool("site-1", "search", "mcp_docs_search", "Search", {"type": "object"}, enabled=False)
    remote = McpRemoteTool("search", "Search", {"type": "object"})

    assert tool.enabled is False
    assert tool.remote_name == remote.name
    assert remote.input_schema == {"type": "object"}


def test_mcp_site_supports_stdio_config():
    site = McpSite(
        name="local",
        url="",
        transport_type=McpTransportType.STDIO,
        command="uvx",
        args=["mcp-server"],
        env={"TOKEN": "x"},
    )

    assert McpTransportType.STDIO.value == "stdio"
    assert site.command == "uvx"
    assert site.args == ["mcp-server"]
    assert site.env == {"TOKEN": "x"}
