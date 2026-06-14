from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall


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
                    source TEXT NOT NULL
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
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def create_session(self, session: ConversationSession) -> ConversationSession:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions(id, title, created_at, updated_at, source)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session.id, session.title, session.created_at.isoformat(), session.updated_at.isoformat(), session.source),
            )
        return session

    async def get_session(self, session_id: str) -> ConversationSession | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        return ConversationSession(id=row["id"], title=row["title"], source=row["source"])

    async def list_sessions(self) -> list[ConversationSession]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
        return [ConversationSession(id=row["id"], title=row["title"], source=row["source"]) for row in rows]

    async def update_session_title(self, session_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))

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

    async def get_context(self, session_id: str) -> dict[str, Any]:
        return {
            "session": await self.get_session(session_id),
            "messages": await self.list_messages(session_id),
            "tool_calls": await self.list_tool_calls(session_id),
            "task_state": await self.get_task_state(session_id),
            "summary": await self.get_summary(session_id),
        }
