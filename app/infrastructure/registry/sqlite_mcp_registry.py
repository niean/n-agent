from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.domain.mcp import McpProbeStatus, McpSite, McpSiteNotFoundError, McpSiteRegistry, McpTool, McpTransportType


def _initialize_mcp_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mcp_sites (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            transport_type TEXT NOT NULL,
            url TEXT NOT NULL,
            command TEXT,
            args_json TEXT NOT NULL DEFAULT '[]',
            env_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            last_probe_status TEXT NOT NULL,
            last_probe_error TEXT,
            last_probed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mcp_tools (
            id TEXT PRIMARY KEY,
            site_id TEXT NOT NULL,
            remote_name TEXT NOT NULL,
            local_name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            input_schema_json TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_seen_at TEXT NOT NULL,
            FOREIGN KEY(site_id) REFERENCES mcp_sites(id),
            UNIQUE(site_id, remote_name)
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_tools_site_id ON mcp_tools(site_id);
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mcp_sites)").fetchall()}
    if "command" not in columns:
        conn.execute("ALTER TABLE mcp_sites ADD COLUMN command TEXT")
    if "args_json" not in columns:
        conn.execute("ALTER TABLE mcp_sites ADD COLUMN args_json TEXT NOT NULL DEFAULT '[]'")
    if "env_json" not in columns:
        conn.execute("ALTER TABLE mcp_sites ADD COLUMN env_json TEXT NOT NULL DEFAULT '{}'")


class SQLiteMcpSiteRegistry(McpSiteRegistry):
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _initialize_mcp_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def list_sites(self) -> list[McpSite]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM mcp_sites ORDER BY created_at ASC").fetchall()
        return [_site_from_row(row) for row in rows]

    async def get_site(self, site_id: str) -> McpSite | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM mcp_sites WHERE id = ?", (site_id,)).fetchone()
        return _site_from_row(row) if row else None

    async def get_site_by_name(self, name: str) -> McpSite | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM mcp_sites WHERE name = ?", (name,)).fetchone()
        return _site_from_row(row) if row else None

    async def create_site(self, site: McpSite) -> McpSite:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mcp_sites(id, name, transport_type, url, command, args_json, env_json, enabled, last_probe_status, last_probe_error, last_probed_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _site_params(site),
            )
        return site

    async def update_site(self, site: McpSite) -> McpSite:
        updated = McpSite(
            id=site.id,
            name=site.name,
            transport_type=site.transport_type,
            url=site.url,
            command=site.command,
            args=site.args,
            env=site.env,
            enabled=site.enabled,
            last_probe_status=site.last_probe_status,
            last_probe_error=site.last_probe_error,
            last_probed_at=site.last_probed_at,
            created_at=site.created_at,
            updated_at=datetime.now(timezone.utc),
        )
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE mcp_sites
                SET name = ?, transport_type = ?, url = ?, command = ?, args_json = ?, env_json = ?, enabled = ?, last_probe_status = ?, last_probe_error = ?, last_probed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated.name,
                    updated.transport_type.value,
                    updated.url,
                    updated.command,
                    json.dumps(updated.args),
                    json.dumps(updated.env),
                    int(updated.enabled),
                    updated.last_probe_status.value,
                    updated.last_probe_error,
                    _dt_to_str(updated.last_probed_at),
                    updated.updated_at.isoformat(),
                    updated.id,
                ),
            )
            if cursor.rowcount == 0:
                raise McpSiteNotFoundError(updated.id)
        return updated

    async def delete_site(self, site_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM mcp_tools WHERE site_id = ?", (site_id,))
            cursor = conn.execute("DELETE FROM mcp_sites WHERE id = ?", (site_id,))
            return cursor.rowcount > 0

    async def update_probe_status(self, site_id: str, status: McpProbeStatus, error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE mcp_sites
                SET last_probe_status = ?, last_probe_error = ?, last_probed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status.value, error, now, now, site_id),
            )
            if cursor.rowcount == 0:
                raise McpSiteNotFoundError(site_id)

    async def list_tools(self, site_id: str | None = None) -> list[McpTool]:
        with self._connect() as conn:
            if site_id is None:
                rows = conn.execute("SELECT * FROM mcp_tools ORDER BY site_id, remote_name").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM mcp_tools WHERE site_id = ? ORDER BY remote_name",
                    (site_id,),
                ).fetchall()
        return [_tool_from_row(row) for row in rows]

    async def replace_site_tools(self, site_id: str, tools: list[McpTool]) -> list[McpTool]:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            existing_rows = conn.execute("SELECT * FROM mcp_tools WHERE site_id = ?", (site_id,)).fetchall()
            existing = {row["remote_name"]: _tool_from_row(row) for row in existing_rows}
            conn.execute("DELETE FROM mcp_tools WHERE site_id = ?", (site_id,))
            saved: list[McpTool] = []
            for tool in tools:
                previous = existing.get(tool.remote_name)
                saved_tool = McpTool(
                    id=previous.id if previous else tool.id,
                    site_id=site_id,
                    remote_name=tool.remote_name,
                    local_name=tool.local_name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    enabled=previous.enabled if previous else tool.enabled,
                    last_seen_at=now,
                )
                conn.execute(
                    """
                    INSERT INTO mcp_tools(id, site_id, remote_name, local_name, description, input_schema_json, enabled, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _tool_params(saved_tool),
                )
                saved.append(saved_tool)
        return saved

    async def update_tool_enabled(self, site_id: str, tool_id: str, enabled: bool) -> McpTool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE mcp_tools SET enabled = ? WHERE site_id = ? AND id = ?",
                (int(enabled), site_id, tool_id),
            )
            if cursor.rowcount == 0:
                raise McpSiteNotFoundError(tool_id)
            row = conn.execute("SELECT * FROM mcp_tools WHERE site_id = ? AND id = ?", (site_id, tool_id)).fetchone()
        return _tool_from_row(row)


def _site_params(site: McpSite) -> tuple:
    return (
        site.id,
        site.name,
        site.transport_type.value,
        site.url,
        site.command,
        json.dumps(site.args),
        json.dumps(site.env),
        int(site.enabled),
        site.last_probe_status.value,
        site.last_probe_error,
        _dt_to_str(site.last_probed_at),
        site.created_at.isoformat(),
        site.updated_at.isoformat(),
    )


def _tool_params(tool: McpTool) -> tuple:
    return (
        tool.id,
        tool.site_id,
        tool.remote_name,
        tool.local_name,
        tool.description,
        json.dumps(tool.input_schema),
        int(tool.enabled),
        tool.last_seen_at.isoformat(),
    )


def _site_from_row(row: sqlite3.Row) -> McpSite:
    return McpSite(
        id=row["id"],
        name=row["name"],
        transport_type=McpTransportType(row["transport_type"]),
        url=row["url"],
        command=row["command"],
        args=json.loads(row["args_json"] or "[]"),
        env=json.loads(row["env_json"] or "{}"),
        enabled=bool(row["enabled"]),
        last_probe_status=McpProbeStatus(row["last_probe_status"]),
        last_probe_error=row["last_probe_error"],
        last_probed_at=_dt_from_str(row["last_probed_at"]),
        created_at=_dt_from_str(row["created_at"]),
        updated_at=_dt_from_str(row["updated_at"]),
    )


def _tool_from_row(row: sqlite3.Row) -> McpTool:
    return McpTool(
        id=row["id"],
        site_id=row["site_id"],
        remote_name=row["remote_name"],
        local_name=row["local_name"],
        description=row["description"],
        input_schema=json.loads(row["input_schema_json"]),
        enabled=bool(row["enabled"]),
        last_seen_at=_dt_from_str(row["last_seen_at"]),
    )


def _dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_from_str(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
