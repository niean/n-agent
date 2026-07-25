"""SqliteTaskConfigStore -- single-row task_config persistence.

Mirrors SqliteCuratorStateStore's independent _connect() + asyncio.to_thread
pattern. Enhancements: id=1 CHECK, version column for CAS, BEGIN IMMEDIATE,
no INSERT OR IGNORE (first-write race must not bypass CAS).

Strict parsing: corrupt JSON, non-object, unknown keys, non-int/bool values,
or illegal metadata raise TaskConfigStoreError -- never silently fall back to
env. The management surface must surface corruption; the runtime surface
(TaskConfigService.current) catches and uses last-known-good.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.domain.task_config import (
    StoredTaskConfig,
    TaskConfigConflictError,
    TaskConfigOverrides,
    TaskConfigStore,
    TaskConfigStoreError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_config (
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


class SqliteTaskConfigStore(TaskConfigStore):
    """SQLite single-row task_config store with CAS."""

    def __init__(self, db_path: str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # fail-fast on init: a corrupted/readonly schema must not let the
        # service run with a broken store (aligns with core SQLite services).
        with self._connect() as conn:
            _initialize_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _get_sync(self) -> StoredTaskConfig | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT config_json, version, updated_at, updated_by FROM task_config WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        config_json, version, updated_at, updated_by = row
        try:
            data = json.loads(config_json)
        except (ValueError, TypeError) as exc:
            raise TaskConfigStoreError(f"config_json is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise TaskConfigStoreError("config_json must be a JSON object")
        overrides = TaskConfigOverrides.from_dict(data)  # strict: raises on bad keys/types
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise TaskConfigStoreError(f"illegal version: {version!r}")
        if not isinstance(updated_at, str) or not updated_at:
            raise TaskConfigStoreError("updated_at must be non-empty string")
        if not isinstance(updated_by, str) or not updated_by:
            raise TaskConfigStoreError("updated_by must be non-empty string")
        return StoredTaskConfig(
            overrides=overrides, version=version,
            updated_at=updated_at, updated_by=updated_by,
        )

    async def get(self) -> StoredTaskConfig | None:
        return await asyncio.to_thread(self._get_sync)

    def _save_sync(
        self, overrides: TaskConfigOverrides, expected_version: int, updated_by: str,
    ) -> StoredTaskConfig:
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise TaskConfigStoreError(f"illegal expected_version: {expected_version!r}")
        if not isinstance(updated_by, str) or not updated_by:
            raise TaskConfigStoreError("updated_by must be non-empty string")
        config_json = json.dumps(overrides.to_dict(), ensure_ascii=False, sort_keys=True)
        now = _now_utc_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version FROM task_config WHERE id = 1"
            ).fetchone()
            current_version = row[0] if row is not None else None
            if expected_version == 0:
                # First write: row must NOT exist.
                if current_version is not None:
                    raise TaskConfigConflictError(
                        f"first-write conflict: row already exists at version {current_version}"
                    )
                conn.execute(
                    "INSERT INTO task_config (id, config_json, version, updated_at, updated_by) "
                    "VALUES (1, ?, 1, ?, ?)",
                    (config_json, now, updated_by),
                )
                new_version = 1
            else:
                if current_version is None:
                    raise TaskConfigConflictError(
                        f"update conflict: row does not exist (expected version {expected_version})"
                    )
                if current_version != expected_version:
                    raise TaskConfigConflictError(
                        f"version mismatch: expected {expected_version}, got {current_version}"
                    )
                cur = conn.execute(
                    "UPDATE task_config SET config_json = ?, version = version + 1, "
                    "updated_at = ?, updated_by = ? WHERE id = 1 AND version = ?",
                    (config_json, now, updated_by, expected_version),
                )
                if cur.rowcount != 1:
                    raise TaskConfigConflictError(
                        f"CAS update failed: rowcount={cur.rowcount}"
                    )
                new_version = expected_version + 1
            conn.commit()
            return StoredTaskConfig(
                overrides=overrides, version=new_version,
                updated_at=now, updated_by=updated_by,
            )
        except TaskConfigConflictError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    async def save(
        self, overrides: TaskConfigOverrides, expected_version: int, updated_by: str,
    ) -> StoredTaskConfig:
        return await asyncio.to_thread(self._save_sync, overrides, expected_version, updated_by)
