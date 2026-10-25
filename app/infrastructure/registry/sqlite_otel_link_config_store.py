"""SqliteOtelLinkConfigStore -- single-row otel_link_config persistence.

Mirrors SqliteTaskConfigStore: independent _connect() + asyncio.to_thread,
id=1 CHECK, version CAS with BEGIN IMMEDIATE, strict parsing.

Differences from the task_config store:
  - updated_at is strictly validated: ISO 8601 parseable, timezone-aware,
    UTC offset exactly 0 (naive and non-UTC timestamps are rejected).
  - All read/serialize/write/commit failures are wrapped in
    OtelLinkConfigStoreError whose message never embeds the original value
    or the original exception text (stored URLs may carry credentials).
    OtelLinkConfigConflictError is preserved unwrapped.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.domain.otel_link_config import (
    OtelLinkConfigConflictError,
    OtelLinkConfigOverrides,
    OtelLinkConfigStore,
    OtelLinkConfigStoreError,
    StoredOtelLinkConfig,
)
from app.infrastructure.sqlite_support import open_sqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS otel_link_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT    NOT NULL,
    version     INTEGER NOT NULL CHECK (version >= 1),
    updated_at  TEXT    NOT NULL,
    updated_by  TEXT    NOT NULL
)
"""


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_updated_at(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise OtelLinkConfigStoreError("updated_at must be non-empty string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise OtelLinkConfigStoreError("updated_at is not ISO 8601 parseable") from None
    if parsed.tzinfo is None:
        raise OtelLinkConfigStoreError("updated_at must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise OtelLinkConfigStoreError("updated_at must be UTC (offset 0)")
    return value


class SqliteOtelLinkConfigStore(OtelLinkConfigStore):
    """SQLite single-row otel_link_config store with CAS."""

    def __init__(self, db_path: str, *, initialize: bool = True, read_only: bool = False) -> None:
        self.path = Path(db_path)
        self._read_only = read_only
        if not initialize or read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # fail-fast on init: a corrupted/readonly schema must not let the
        # service run with a broken store.
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            _initialize_schema(conn)
            conn.commit()
        except Exception:
            raise OtelLinkConfigStoreError("failed to initialize otel_link_config schema") from None
        finally:
            if conn is not None:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self.path, read_only=self._read_only)

    def _get_sync(self) -> StoredOtelLinkConfig | None:
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT config_json, version, updated_at, updated_by "
                "FROM otel_link_config WHERE id = 1"
            ).fetchone()
        except Exception:
            raise OtelLinkConfigStoreError("failed to read otel_link_config row") from None
        finally:
            if conn is not None:
                conn.close()
        if row is None:
            return None
        config_json, version, updated_at, updated_by = row
        try:
            data = json.loads(config_json)
        except (ValueError, TypeError):
            raise OtelLinkConfigStoreError("config_json is not valid JSON") from None
        overrides = OtelLinkConfigOverrides.from_dict(data)  # strict: raises StoreError
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise OtelLinkConfigStoreError("illegal version in otel_link_config row")
        if not isinstance(updated_by, str) or not updated_by:
            raise OtelLinkConfigStoreError("updated_by must be non-empty string")
        return StoredOtelLinkConfig(
            overrides=overrides,
            version=version,
            updated_at=_parse_updated_at(updated_at),
            updated_by=updated_by,
        )

    async def get(self) -> StoredOtelLinkConfig | None:
        return await asyncio.to_thread(self._get_sync)

    def _save_sync(
        self, overrides: OtelLinkConfigOverrides, expected_version: int, updated_by: str
    ) -> StoredOtelLinkConfig:
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise OtelLinkConfigStoreError("illegal expected_version")
        if not isinstance(updated_by, str) or not updated_by:
            raise OtelLinkConfigStoreError("updated_by must be non-empty string")
        try:
            config_json = json.dumps(overrides.to_dict(), ensure_ascii=False, sort_keys=True)
        except Exception:
            raise OtelLinkConfigStoreError("failed to serialize overrides") from None
        now = _now_utc_iso()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT version FROM otel_link_config WHERE id = 1").fetchone()
            current_version = row[0] if row is not None else None
            if expected_version == 0:
                # First write: row must NOT exist.
                if current_version is not None:
                    raise OtelLinkConfigConflictError(
                        "first-write conflict: row already exists"
                    )
                conn.execute(
                    "INSERT INTO otel_link_config (id, config_json, version, updated_at, "
                    "updated_by) VALUES (1, ?, 1, ?, ?)",
                    (config_json, now, updated_by),
                )
                new_version = 1
            else:
                if current_version is None:
                    raise OtelLinkConfigConflictError("update conflict: row does not exist")
                if current_version != expected_version:
                    raise OtelLinkConfigConflictError("version mismatch")
                cur = conn.execute(
                    "UPDATE otel_link_config SET config_json = ?, version = version + 1, "
                    "updated_at = ?, updated_by = ? WHERE id = 1 AND version = ?",
                    (config_json, now, updated_by, expected_version),
                )
                if cur.rowcount != 1:
                    raise OtelLinkConfigConflictError("CAS update failed")
                new_version = expected_version + 1
            conn.commit()
            return StoredOtelLinkConfig(
                overrides=overrides, version=new_version, updated_at=now, updated_by=updated_by
            )
        except OtelLinkConfigConflictError:
            if conn is not None:
                conn.rollback()
            raise
        except Exception:
            if conn is not None:
                conn.rollback()
            raise OtelLinkConfigStoreError("failed to save otel_link_config row") from None
        finally:
            if conn is not None:
                conn.close()

    async def save(
        self, overrides: OtelLinkConfigOverrides, expected_version: int, updated_by: str
    ) -> StoredOtelLinkConfig:
        return await asyncio.to_thread(self._save_sync, overrides, expected_version, updated_by)
