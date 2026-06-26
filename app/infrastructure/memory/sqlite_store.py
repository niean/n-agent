from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall
from app.infrastructure.registry.sqlite_gateway_registry import _initialize_gateway_schema
from app.infrastructure.registry.sqlite_mcp_registry import _initialize_mcp_schema


class SQLiteMemoryStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    external_memory_enabled_json TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    provider_message_id TEXT,
                    tool_call_id TEXT,
                    name TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS tool_calls (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    message_id TEXT,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    result_json TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id)
                );
                CREATE TABLE IF NOT EXISTS task_states (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    iteration_count INTEGER NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    session_id TEXT PRIMARY KEY,
                    summary TEXT NOT NULL,
                    source_message_id TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_created_at ON messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_session_created_at ON tool_calls(session_id, created_at);
                CREATE TABLE IF NOT EXISTS providers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    provider_type TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    model TEXT NOT NULL,
                    api_key TEXT,
                    extra_headers_json TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_providers_active
                    ON providers(is_active) WHERE is_active = 1;
                """
            )
            self._ensure_sessions_external_memory_column(conn)
            _initialize_gateway_schema(conn)
            _initialize_mcp_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def create_session(self, session: ConversationSession) -> ConversationSession:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions(id, title, created_at, updated_at, source, external_memory_enabled_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.title,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.source,
                    json.dumps(session.external_memory_enabled) if session.external_memory_enabled is not None else None,
                ),
            )
        return session

    async def get_session(self, session_id: str) -> ConversationSession | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return self._session_from_row(row)

    async def list_sessions(self) -> list[ConversationSession]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
        return [self._session_from_row(row) for row in rows]

    async def update_session_title(self, session_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

    async def lock_session_external_memory(self, session_id: str, enabled: list[str]) -> list[str]:
        await self.create_session(ConversationSession(id=session_id))
        normalized = [str(name) for name in enabled]
        encoded = json.dumps(normalized)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT external_memory_enabled_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is not None and row["external_memory_enabled_json"] is not None:
                return json.loads(row["external_memory_enabled_json"])
            conn.execute(
                "UPDATE sessions SET external_memory_enabled_json = ? WHERE id = ?",
                (encoded, session_id),
            )
        return normalized

    async def append_message(self, session_id: str, message: ConversationMessage) -> ConversationMessage:
        await self.create_session(ConversationSession(id=session_id))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(id, session_id, role, content_json, created_at, provider_message_id, tool_call_id, name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    session_id,
                    message.role,
                    json.dumps(message.content),
                    message.created_at.isoformat(),
                    None,
                    message.tool_call_id,
                    message.name,
                ),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (message.created_at.isoformat(), session_id))
        return message

    async def list_messages(self, session_id: str) -> list[ConversationMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [
            ConversationMessage(
                id=row["id"],
                role=row["role"],
                content=json.loads(row["content_json"]),
                tool_call_id=row["tool_call_id"],
                name=row["name"],
            )
            for row in rows
        ]

    async def save_tool_call(self, tool_call: ToolCall) -> ToolCall:
        await self.create_session(ConversationSession(id=tool_call.session_id))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tool_calls(id, session_id, message_id, tool_name, arguments_json, result_json, status, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tool_call.id,
                    tool_call.session_id,
                    tool_call.message_id,
                    tool_call.tool_name,
                    json.dumps(tool_call.arguments),
                    json.dumps(tool_call.result) if tool_call.result is not None else None,
                    tool_call.status,
                    tool_call.duration_ms,
                    tool_call.created_at.isoformat(),
                ),
            )
        return tool_call

    async def list_tool_calls(self, session_id: str) -> list[ToolCall]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        return [
            ToolCall(
                id=row["id"],
                session_id=row["session_id"],
                message_id=row["message_id"],
                tool_name=row["tool_name"],
                arguments=json.loads(row["arguments_json"]),
                result=json.loads(row["result_json"]) if row["result_json"] else None,
                status=row["status"],
                duration_ms=row["duration_ms"],
            )
            for row in rows
        ]

    async def save_task_state(self, task_state: TaskState) -> TaskState:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO task_states(session_id, status, iteration_count, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_state.session_id,
                    task_state.status,
                    task_state.iteration_count,
                    task_state.last_error,
                    task_state.updated_at.isoformat(),
                ),
            )
        return task_state

    async def get_task_state(self, session_id: str) -> TaskState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_states WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return TaskState(
            session_id=row["session_id"],
            status=row["status"],
            iteration_count=row["iteration_count"],
            last_error=row["last_error"],
        )

    async def save_summary(self, summary: Summary) -> Summary:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO summaries(session_id, summary, source_message_id, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (summary.session_id, summary.summary, summary.source_message_id, summary.updated_at.isoformat()),
            )
        return summary

    async def get_summary(self, session_id: str) -> Summary | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM summaries WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return Summary(session_id=row["session_id"], summary=row["summary"], source_message_id=row["source_message_id"])

    async def delete_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("DELETE FROM gateway_session_links WHERE session_id = ?", (session_id,))
            conn.execute("UPDATE gateway_conversations SET active_session_id = NULL WHERE active_session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM task_states WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cursor.rowcount > 0

    async def get_context(self, session_id: str) -> dict[str, Any]:
        return {
            "session": await self.get_session(session_id),
            "messages": await self.list_messages(session_id),
            "tool_calls": await self.list_tool_calls(session_id),
            "task_state": await self.get_task_state(session_id),
            "summary": await self.get_summary(session_id),
        }

    @staticmethod
    def _ensure_sessions_external_memory_column(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "external_memory_enabled_json" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN external_memory_enabled_json TEXT")

    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> ConversationSession:
        enabled_json = row["external_memory_enabled_json"]
        enabled = json.loads(enabled_json) if enabled_json is not None else None
        return ConversationSession(id=row["id"], title=row["title"], source=row["source"], external_memory_enabled=enabled)
