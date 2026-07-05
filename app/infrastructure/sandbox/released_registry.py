from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from app.domain.sandbox import ReleasedSandboxInfo


class SQLiteReleasedSandboxRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sandbox_released_history (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    sandbox_type TEXT NOT NULL,
                    sandbox_id TEXT,
                    created_at TEXT NOT NULL,
                    released_at TEXT NOT NULL,
                    reason TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sandbox_released_history_released_at
                ON sandbox_released_history(released_at)
                """
            )
            self._migrate_legacy_container_name(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _migrate_legacy_container_name(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(sandbox_released_history)").fetchall()
        }
        if "sandbox_id" not in columns:
            conn.execute("ALTER TABLE sandbox_released_history ADD COLUMN sandbox_id TEXT")
            columns.add("sandbox_id")
        if "container_name" in columns:
            conn.execute(
                """
                UPDATE sandbox_released_history
                SET sandbox_id = container_name
                WHERE sandbox_id IS NULL AND container_name IS NOT NULL
                """
            )

    def record(self, info: ReleasedSandboxInfo) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sandbox_released_history(
                    id, session_id, sandbox_type, sandbox_id,
                    created_at, released_at, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    info.session_id,
                    info.sandbox_type,
                    info.sandbox_id,
                    info.created_at.isoformat(),
                    info.released_at.isoformat(),
                    info.reason,
                ),
            )

    def list_recent(self, limit: int = 100) -> list[ReleasedSandboxInfo]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sandbox_released_history
                ORDER BY released_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_info(row) for row in rows]

    def delete(self, entry_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sandbox_released_history WHERE id = ?",
                (entry_id,),
            )
            return cur.rowcount > 0

    @staticmethod
    def _row_to_info(row: sqlite3.Row) -> ReleasedSandboxInfo:
        return ReleasedSandboxInfo(
            id=row["id"],
            session_id=row["session_id"],
            sandbox_type=row["sandbox_type"],
            sandbox_id=row["sandbox_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            released_at=datetime.fromisoformat(row["released_at"]),
            reason=row["reason"],
        )
