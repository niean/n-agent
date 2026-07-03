from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.domain.plugin import (
    Plugin,
    PluginKind,
    PluginNotFoundError,
    PluginRegistry,
    PluginScanStatus,
    PluginSource,
    PluginValidationError,
    new_plugin_id,
)


def _initialize_plugin_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS plugins (
            id TEXT PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            version TEXT,
            description TEXT,
            author TEXT,
            kind TEXT,
            source TEXT,
            source_path TEXT,
            enabled INTEGER NOT NULL DEFAULT 0,
            config_json TEXT NOT NULL DEFAULT '{}',
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            manifest_json TEXT NOT NULL DEFAULT '{}',
            last_scan_status TEXT,
            last_scan_error TEXT,
            last_scanned_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_plugins_enabled ON plugins(enabled);

        CREATE TABLE IF NOT EXISTS plugin_secrets (
            plugin_key TEXT NOT NULL,
            field_name TEXT NOT NULL,
            secret_value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(plugin_key, field_name),
            FOREIGN KEY(plugin_key) REFERENCES plugins(key) ON DELETE CASCADE
        );
        """
    )


class SQLitePluginRegistry(PluginRegistry):
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _initialize_plugin_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def list_plugins(self, include_disabled: bool = True) -> list[Plugin]:
        with self._connect() as conn:
            if include_disabled:
                rows = conn.execute("SELECT * FROM plugins ORDER BY key").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM plugins WHERE enabled = 1 ORDER BY key"
                ).fetchall()
            secret_index = _load_secret_index(conn, [row["key"] for row in rows])
        return [
            _plugin_from_row(row).with_secret_refs(secret_index.get(row["key"], {}))
            for row in rows
        ]

    async def get_plugin(self, key: str) -> Plugin | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM plugins WHERE key = ?", (key,)).fetchone()
            secret_refs: dict[str, str] = {}
            if row is not None:
                secret_refs = _load_secret_index(conn, [row["key"]]).get(row["key"], {})
        return _plugin_from_row(row).with_secret_refs(secret_refs) if row else None

    async def upsert_plugin(self, plugin: Plugin) -> Plugin:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id, created_at FROM plugins WHERE key = ?", (plugin.key,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE plugins SET
                        id = ?, name = ?, version = ?, description = ?, author = ?,
                        kind = ?, source = ?, source_path = ?, enabled = ?,
                        config_json = ?, capabilities_json = ?, manifest_json = ?,
                        last_scan_status = ?, last_scan_error = ?, last_scanned_at = ?,
                        updated_at = ?
                    WHERE key = ?
                    """,
                    (
                        plugin.id, plugin.name, plugin.version, plugin.description, plugin.author,
                        plugin.kind.value, plugin.source.value, plugin.source_path, int(plugin.enabled),
                        json.dumps(plugin.config), json.dumps(plugin.capabilities),
                        json.dumps(plugin.manifest),
                        plugin.last_scan_status, plugin.last_scan_error,
                        _dt_str(plugin.last_scanned_at),
                        now.isoformat(), plugin.key,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO plugins(
                        id, key, name, version, description, author, kind, source, source_path,
                        enabled, config_json, capabilities_json, manifest_json,
                        last_scan_status, last_scan_error, last_scanned_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        plugin.id or new_plugin_id(), plugin.key, plugin.name, plugin.version,
                        plugin.description, plugin.author, plugin.kind.value, plugin.source.value,
                        plugin.source_path, int(plugin.enabled),
                        json.dumps(plugin.config), json.dumps(plugin.capabilities),
                        json.dumps(plugin.manifest),
                        plugin.last_scan_status, plugin.last_scan_error,
                        _dt_str(plugin.last_scanned_at),
                        (plugin.created_at or now).isoformat(), now.isoformat(),
                    ),
                )
        result = await self.get_plugin(plugin.key)
        assert result is not None
        return result

    async def set_enabled(self, key: str, enabled: bool) -> Plugin:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE plugins SET enabled = ?, updated_at = ? WHERE key = ?",
                (int(enabled), now, key),
            )
            if cursor.rowcount == 0:
                raise PluginNotFoundError(key)
        result = await self.get_plugin(key)
        assert result is not None
        return result

    async def delete_plugin(self, key: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM plugins WHERE key = ?", (key,))
            return cursor.rowcount > 0

    async def update_config(
        self,
        key: str,
        config: dict[str, Any],
        secret_updates: dict[str, str] | None = None,
    ) -> Plugin:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM plugins WHERE key = ?", (key,)
            ).fetchone()
            if existing is None:
                raise PluginNotFoundError(key)
            conn.execute(
                "UPDATE plugins SET config_json = ?, updated_at = ? WHERE key = ?",
                (json.dumps(config), now.isoformat(), key),
            )
            if secret_updates:
                for field_name, secret_value in secret_updates.items():
                    conn.execute(
                        """
                        INSERT INTO plugin_secrets(plugin_key, field_name, secret_value, updated_at)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(plugin_key, field_name) DO UPDATE SET
                            secret_value = excluded.secret_value,
                            updated_at = excluded.updated_at
                        """,
                        (key, field_name, secret_value, now.isoformat()),
                    )
        result = await self.get_plugin(key)
        assert result is not None
        return result

    async def get_secret_config(self, key: str) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT field_name, secret_value FROM plugin_secrets WHERE plugin_key = ?",
                (key,),
            ).fetchall()
        return {row["field_name"]: row["secret_value"] for row in rows}

    async def replace_all_plugins(self, plugins: Iterable[Plugin]) -> list[Plugin]:
        plugins_list = list(plugins)
        keys = {p.key for p in plugins_list}
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            existing_rows = conn.execute(
                "SELECT key, enabled, config_json, created_at FROM plugins"
            ).fetchall()
            existing = {
                row["key"]: (
                    bool(row["enabled"]),
                    json.loads(row["config_json"] or "{}"),
                    row["created_at"],
                )
                for row in existing_rows
            }
            for plugin in plugins_list:
                prev = existing.get(plugin.key)
                if prev:
                    preserved_enabled = prev[0]
                    preserved_config = prev[1] if plugin.config == {} else plugin.config
                    created_at = datetime.fromisoformat(prev[2]) if prev[2] else now
                    conn.execute(
                        """
                        UPDATE plugins SET
                            id = ?, name = ?, version = ?, description = ?, author = ?,
                            kind = ?, source = ?, source_path = ?, enabled = ?,
                            config_json = ?, capabilities_json = ?, manifest_json = ?,
                            last_scan_status = ?, last_scan_error = ?, last_scanned_at = ?,
                            updated_at = ?
                        WHERE key = ?
                        """,
                        (
                            plugin.id, plugin.name, plugin.version, plugin.description, plugin.author,
                            plugin.kind.value, plugin.source.value, plugin.source_path,
                            int(preserved_enabled),
                            json.dumps(preserved_config), json.dumps(plugin.capabilities),
                            json.dumps(plugin.manifest),
                            plugin.last_scan_status, plugin.last_scan_error,
                            _dt_str(plugin.last_scanned_at),
                            now.isoformat(), plugin.key,
                        ),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO plugins(
                            id, key, name, version, description, author, kind, source, source_path,
                            enabled, config_json, capabilities_json, manifest_json,
                            last_scan_status, last_scan_error, last_scanned_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            plugin.id or new_plugin_id(), plugin.key, plugin.name, plugin.version,
                            plugin.description, plugin.author, plugin.kind.value, plugin.source.value,
                            plugin.source_path, int(plugin.enabled),
                            json.dumps(plugin.config), json.dumps(plugin.capabilities),
                            json.dumps(plugin.manifest),
                            plugin.last_scan_status, plugin.last_scan_error,
                            _dt_str(plugin.last_scanned_at),
                            (plugin.created_at or now).isoformat(), now.isoformat(),
                        ),
                    )
            for key in existing:
                if key not in keys:
                    conn.execute(
                        """
                        UPDATE plugins SET
                            enabled = 0,
                            last_scan_status = ?,
                            last_scan_error = ?,
                            last_scanned_at = ?,
                            updated_at = ?
                        WHERE key = ?
                        """,
                        (
                            PluginScanStatus.MISSING.value,
                            "plugin source no longer present on disk",
                            now.isoformat(), now.isoformat(), key,
                        ),
                    )
        return await self.list_plugins(include_disabled=True)


def _plugin_from_row(row: sqlite3.Row) -> Plugin:
    try:
        kind = PluginKind(row["kind"]) if row["kind"] else PluginKind.STANDALONE
    except ValueError:
        kind = PluginKind.STANDALONE
    try:
        source = PluginSource(row["source"]) if row["source"] else PluginSource.USER
    except ValueError:
        source = PluginSource.USER
    return Plugin(
        id=row["id"],
        key=row["key"],
        name=row["name"],
        version=row["version"] or "",
        description=row["description"] or "",
        author=row["author"] or "",
        kind=kind,
        source=source,
        source_path=row["source_path"] or "",
        enabled=bool(row["enabled"]),
        config=json.loads(row["config_json"] or "{}"),
        secret_refs={},
        capabilities=json.loads(row["capabilities_json"] or "{}"),
        manifest=json.loads(row["manifest_json"] or "{}"),
        last_scan_status=row["last_scan_status"],
        last_scan_error=row["last_scan_error"],
        last_scanned_at=_dt_parse(row["last_scanned_at"]),
        created_at=_dt_parse(row["created_at"]),
        updated_at=_dt_parse(row["updated_at"]),
    )


def _load_secret_index(conn: sqlite3.Connection, keys: list[str]) -> dict[str, dict[str, str]]:
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT plugin_key, field_name FROM plugin_secrets WHERE plugin_key IN ({placeholders})",
        tuple(keys),
    ).fetchall()
    index: dict[str, dict[str, str]] = {}
    for row in rows:
        index.setdefault(row["plugin_key"], {})[row["field_name"]] = "set"
    return index


def _dt_str(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
