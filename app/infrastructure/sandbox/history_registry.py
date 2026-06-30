from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from app.domain.sandbox import SandboxExecutionHistoryEntry


class SQLiteSandboxExecutionHistoryRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sandbox_execution_history (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    code TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    authorized_callback_tools_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sandbox_execution_history_created_at
                ON sandbox_execution_history(created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_sandbox_execution_history_session_created_at
                ON sandbox_execution_history(session_id, created_at)
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, entry: SandboxExecutionHistoryEntry) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sandbox_execution_history(
                    id, session_id, code_hash, code, result_json, status,
                    duration_ms, authorized_callback_tools_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.session_id,
                    entry.code_hash,
                    entry.code,
                    json.dumps(entry.result) if entry.result is not None else None,
                    entry.status,
                    entry.duration_ms,
                    json.dumps(entry.authorized_callback_tools),
                    entry.created_at.isoformat(),
                ),
            )

    def list_recent(
        self, session_id: str | None = None, limit: int = 50,
    ) -> list[SandboxExecutionHistoryEntry]:
        with self._connect() as conn:
            if session_id:
                rows = conn.execute(
                    """
                    SELECT * FROM sandbox_execution_history
                    WHERE session_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM sandbox_execution_history
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [self._row_to_entry(row) for row in rows]

    def delete(self, entry_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sandbox_execution_history WHERE id = ?",
                (entry_id,),
            )
        return cur.rowcount > 0

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> SandboxExecutionHistoryEntry:
        return SandboxExecutionHistoryEntry(
            id=row["id"],
            session_id=row["session_id"],
            code_hash=row["code_hash"],
            code=row["code"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            status=row["status"],
            duration_ms=row["duration_ms"],
            authorized_callback_tools=json.loads(row["authorized_callback_tools_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
