import sqlite3

import pytest

from app.domain.mcp import McpProbeStatus, McpSite, McpTool, McpTransportType
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


@pytest.mark.asyncio
async def test_sqlite_mcp_registry_persists_stdio_config(tmp_path):
    registry = SQLiteMcpSiteRegistry(tmp_path / "sessions.db")
    site = await registry.create_site(McpSite(
        name="local",
        transport_type=McpTransportType.STDIO,
        command="uvx",
        args=["server"],
        env={"TOKEN": "x"},
    ))
    loaded = await registry.get_site(site.id)
    updated = await registry.update_site(McpSite(
        id=site.id,
        name="local",
        transport_type=McpTransportType.STDIO,
        command="npx",
        args=["other"],
        env={"TOKEN": "y"},
        created_at=site.created_at,
        updated_at=site.updated_at,
    ))

    assert loaded.command == "uvx"
    assert loaded.args == ["server"]
    assert loaded.env == {"TOKEN": "x"}
    assert updated.command == "npx"
    assert updated.args == ["other"]
    assert updated.env == {"TOKEN": "y"}


@pytest.mark.asyncio
async def test_sqlite_mcp_registry_migrates_existing_site_table(tmp_path):
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE mcp_sites (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                transport_type TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_probe_status TEXT NOT NULL,
                last_probe_error TEXT,
                last_probed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO mcp_sites(id, name, transport_type, url, enabled, last_probe_status, created_at, updated_at)
            VALUES ('site-1', 'docs', 'streamable_http', 'https://example.com/mcp', 1, 'never', '2026-06-15T00:00:00+00:00', '2026-06-15T00:00:00+00:00');
            """
        )

    registry = SQLiteMcpSiteRegistry(path)
    site = (await registry.list_sites())[0]
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(mcp_sites)").fetchall()}

    assert {"command", "args_json", "env_json"}.issubset(columns)
    assert site.name == "docs"
    assert site.command is None
    assert site.args == []
    assert site.env == {}
