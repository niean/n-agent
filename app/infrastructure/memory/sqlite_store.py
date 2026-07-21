from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.context import CONTEXT_SUMMARY_PREFIX
from app.domain.session import ConversationMessage, ConversationSession, SessionSource, Summary, TaskState, ToolCall
from app.infrastructure.registry.sqlite_gateway_registry import _initialize_gateway_schema
from app.infrastructure.registry.sqlite_mcp_registry import _initialize_mcp_schema

logger = logging.getLogger(__name__)


class SQLiteMemoryStore:
    def __init__(self, path: Path, *, migration_protect_first_n: int = 3, migration_protect_last_n: int = 10):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migration_protect_first_n = migration_protect_first_n
        self._migration_protect_last_n = migration_protect_last_n
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
                    is_summary INTEGER NOT NULL DEFAULT 0,
                    is_summarized INTEGER NOT NULL DEFAULT 0,
                    source TEXT,
                    card_json TEXT,
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
            self._ensure_sessions_acp_metadata_column(conn)
            self._migrate_add_is_summary_column(conn)
            self._migrate_add_is_summarized_column(conn)
            self._migrate_add_source_column(conn)
            self._migrate_add_card_column(conn)
            self._migrate_mark_legacy_middle_summarized(
                conn,
                protect_first_n=self._migration_protect_first_n,
                protect_last_n=self._migration_protect_last_n,
            )
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
                INSERT OR IGNORE INTO sessions(id, title, created_at, updated_at, source, external_memory_enabled_json, acp_metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.title,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.source,
                    json.dumps(session.external_memory_enabled) if session.external_memory_enabled is not None else None,
                    json.dumps(session.acp_metadata) if session.acp_metadata is not None else None,
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

    async def lock_session_external_memory(
        self, session_id: str, enabled: list[str], slots: dict[str, str] | None = None,
    ) -> list[str]:
        await self.create_session(ConversationSession(id=session_id))
        normalized = [str(name) for name in enabled]
        encoded = json.dumps(normalized)
        slots_encoded = json.dumps(slots) if slots else None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT external_memory_enabled_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if row is not None and row["external_memory_enabled_json"] is not None:
                return json.loads(row["external_memory_enabled_json"])
            conn.execute(
                "UPDATE sessions SET external_memory_enabled_json = ?, external_memory_slots_json = ? WHERE id = ?",
                (encoded, slots_encoded, session_id),
            )
        return normalized

    async def append_message(self, session_id: str, message: ConversationMessage) -> ConversationMessage:
        await self.create_session(ConversationSession(id=session_id))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(id, session_id, role, content_json, created_at, provider_message_id, tool_call_id, name, is_summary, is_summarized, source, card_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    session_id,
                    message.role,
                    json.dumps(message.content, ensure_ascii=False),
                    message.created_at.isoformat(),
                    None,
                    message.tool_call_id,
                    message.name,
                    1 if message.is_summary else 0,
                    1 if message.is_summarized else 0,
                    message.source,
                    json.dumps(message.card, ensure_ascii=False) if message.card is not None else None,
                ),
            )
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (message.created_at.isoformat(), session_id))
        return message

    async def append_message_if_session_exists(
        self, session_id: str, message: ConversationMessage,
    ) -> ConversationMessage | None:
        """仅当 session 存在时追加 message；不存在返回 None，不建会话。

        单连接、单 ``BEGIN IMMEDIATE`` 事务内完成 session 存在性检查、messages INSERT
        与 sessions.updated_at 更新；任一步失败整体回滚。sqlite3 默认 ``isolation_level``
        会在 DML 前隐式 BEGIN，与显式 BEGIN IMMEDIATE 冲突，故先切 autocommit 再手动管事务。
        并发删除/追加以先获写锁方为准：追加先提交则消息随后随显式 delete_session 清理；
        删除先提交则追加返回 None。不留下孤儿消息、不复活 session。
        """
        with self._connect() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT updated_at FROM sessions WHERE id = ?", (session_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    """
                    INSERT INTO messages(id, session_id, role, content_json, created_at, provider_message_id, tool_call_id, name, is_summary, is_summarized, source, card_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message.id,
                        session_id,
                        message.role,
                        json.dumps(message.content, ensure_ascii=False),
                        message.created_at.isoformat(),
                        None,
                        message.tool_call_id,
                        message.name,
                        1 if message.is_summary else 0,
                        1 if message.is_summarized else 0,
                        message.source,
                        json.dumps(message.card, ensure_ascii=False) if message.card is not None else None,
                    ),
                )
                # updated_at = max(原值, message.created_at)，防迟到客户端时间令活动时间倒退
                conn.execute(
                    "UPDATE sessions SET updated_at = MAX(updated_at, ?) WHERE id = ?",
                    (message.created_at.isoformat(), session_id),
                )
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        return message

    async def list_messages(self, session_id: str) -> list[ConversationMessage]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, rowid ASC",
                (session_id,),
            ).fetchall()
        return [
            ConversationMessage(
                id=row["id"],
                role=row["role"],
                content=json.loads(row["content_json"]),
                tool_call_id=row["tool_call_id"],
                name=row["name"],
                created_at=datetime.fromisoformat(row["created_at"]),
                is_summary=bool(row["is_summary"]),
                is_summarized=bool(row["is_summarized"]) if "is_summarized" in row.keys() else False,
                source=row["source"] if "source" in row.keys() else None,
                card=self._decode_message_card(row),
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
        return [self._row_to_tool_call(row) for row in rows]

    async def list_recent_tool_calls(
        self, tool_name: str | None = None, limit: int = 50,
    ) -> list[ToolCall]:
        with self._connect() as conn:
            if tool_name is not None:
                rows = conn.execute(
                    "SELECT * FROM tool_calls WHERE tool_name = ? ORDER BY created_at DESC LIMIT ?",
                    (tool_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tool_calls ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_tool_call(row) for row in rows]

    async def delete_tool_call(self, tool_call_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM tool_calls WHERE id = ?",
                (tool_call_id,),
            )
        return cur.rowcount > 0

    @staticmethod
    def _row_to_tool_call(row) -> ToolCall:
        return ToolCall(
            id=row["id"],
            session_id=row["session_id"],
            message_id=row["message_id"],
            tool_name=row["tool_name"],
            arguments=json.loads(row["arguments_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            status=row["status"],
            duration_ms=row["duration_ms"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

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

    async def delete_summary_messages(self, session_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND is_summary = 1",
                (session_id,),
            )
            return cur.rowcount

    async def append_summary_message(
        self, session_id: str, message: ConversationMessage,
    ) -> ConversationMessage:
        if not message.is_summary:
            raise ValueError("append_summary_message requires is_summary=True")
        if message.role != "user":
            raise ValueError("summary message role must be 'user'")
        if not isinstance(message.content, str):
            raise ValueError("summary message content must be str")
        if not message.content.startswith(CONTEXT_SUMMARY_PREFIX):
            raise ValueError(f"summary message content must start with {CONTEXT_SUMMARY_PREFIX!r}")
        await self.create_session(ConversationSession(id=session_id))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages(id, session_id, role, content_json, created_at,
                    provider_message_id, tool_call_id, name, is_summary, is_summarized, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    session_id,
                    message.role,
                    json.dumps(message.content, ensure_ascii=False),
                    message.created_at.isoformat(),
                    None,
                    message.tool_call_id,
                    message.name,
                    1,
                    0,
                    message.source,
                ),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (message.created_at.isoformat(), session_id),
            )
        return message

    async def mark_messages_summarized(self, session_id: str, message_ids: list[str]) -> int:
        if not message_ids:
            return 0
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE messages SET is_summarized = 1 WHERE session_id = ? AND id IN (%s)"
                % ",".join("?" * len(message_ids)),
                [session_id, *message_ids],
            )
            return cur.rowcount

    async def delete_session(self, session_id: str) -> bool:
        with self._connect() as conn:
            existing_tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            conn.execute("DELETE FROM gateway_session_links WHERE session_id = ?", (session_id,))
            conn.execute("UPDATE gateway_conversations SET active_session_id = NULL WHERE active_session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM task_states WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM summaries WHERE session_id = ?", (session_id,))
            # Task 子域：删除 origin session 仅置空 tasks.origin_session_id；删除
            # execution session 仅置空 tasks.execution_session_id；均不删除 Task
            # 行。Task 删除由 TaskService 负责，并在删除后清理 execution session。
            # 旧库未启用 Task（tasks 表不存在）时跳过，保持向后兼容。
            if "tasks" in existing_tables:
                conn.execute(
                    "UPDATE tasks SET origin_session_id = NULL WHERE origin_session_id = ?",
                    (session_id,),
                )
                conn.execute(
                    "UPDATE tasks SET execution_session_id = NULL WHERE execution_session_id = ?",
                    (session_id,),
                )
            cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return cursor.rowcount > 0

    async def update_session_acp_metadata(self, session_id: str, metadata: dict[str, Any]) -> None:
        # No-op if session does not exist; session bridge (T10) verifies existence before calling.
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET acp_metadata_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(metadata), datetime.now(timezone.utc).isoformat(), session_id),
            )

    async def list_sessions_by_source(
        self, source: str, cwd: str | None = None, cursor: str | None = None, limit: int = 50,
    ) -> tuple[list[ConversationSession], str | None]:
        # ACP sessions are expected to be few (per-user, per-workspace); fetch all
        # source-matching rows, Python-filter by cwd, then paginate. This keeps
        # the cwd filter correct without SQL JSON queries.
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE source = ? ORDER BY updated_at DESC, id DESC",
                (source,),
            ).fetchall()
        sessions = [self._session_from_row(row) for row in rows]
        if cwd is not None:
            sessions = [
                s for s in sessions
                if s.acp_metadata is not None and s.acp_metadata.get("cwd") == cwd
            ]
        # Cursor format: "{updated_at_iso}|{id}" — points at the last item already returned.
        start_idx = 0
        if cursor is not None:
            try:
                cursor_updated_at, cursor_id = cursor.split("|", 1)
            except ValueError:
                return [], None
            cursor_found = False
            for idx, s in enumerate(sessions):
                if s.updated_at.isoformat() == cursor_updated_at and s.id == cursor_id:
                    start_idx = idx + 1
                    cursor_found = True
                    break
            if not cursor_found:
                # Cursor points at a session no longer present (e.g., deleted
                # between page fetches); treat as end-of-pagination to avoid
                # silently re-returning page 1.
                return [], None
        page = sessions[start_idx:start_idx + limit]
        next_cursor = None
        if start_idx + limit < len(sessions) and page:
            last = page[-1]
            next_cursor = f"{last.updated_at.isoformat()}|{last.id}"
        return page, next_cursor

    async def clone_session(self, source_session_id: str, target_session_id: str) -> None:
        # Single-connection transaction: sqlite3 context manager commits on success,
        # rolls back on exception. Source session existence is verified by the
        # session bridge (T10); if missing here, return silently.
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (source_session_id,),
            ).fetchone()
            if row is None:
                return
            new_ts = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO sessions(id, title, created_at, updated_at, source,
                    external_memory_enabled_json, external_memory_slots_json, acp_metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target_session_id,
                    row["title"],
                    new_ts,
                    new_ts,
                    SessionSource.ACP.value,
                    row["external_memory_enabled_json"] if "external_memory_enabled_json" in row.keys() else None,
                    row["external_memory_slots_json"] if "external_memory_slots_json" in row.keys() else None,
                    row["acp_metadata_json"] if "acp_metadata_json" in row.keys() else None,
                ),
            )
            # Clone messages: regenerate ids, record old->new mapping for tool_call linkage.
            msg_rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC",
                (source_session_id,),
            ).fetchall()
            msg_id_map: dict[str, str] = {}
            for mrow in msg_rows:
                new_msg_id = str(uuid4())
                msg_id_map[mrow["id"]] = new_msg_id
                conn.execute(
                    """
                    INSERT INTO messages(id, session_id, role, content_json, created_at,
                        provider_message_id, tool_call_id, name, is_summary, is_summarized, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_msg_id,
                        target_session_id,
                        mrow["role"],
                        mrow["content_json"],
                        mrow["created_at"],
                        mrow["provider_message_id"],
                        mrow["tool_call_id"],
                        mrow["name"],
                        mrow["is_summary"] if "is_summary" in mrow.keys() else 0,
                        mrow["is_summarized"] if "is_summarized" in mrow.keys() else 0,
                        mrow["source"] if "source" in mrow.keys() else None,
                    ),
                )
            # Clone tool_calls: regenerate ids, rebuild message_id linkage via msg_id_map.
            tc_rows = conn.execute(
                "SELECT * FROM tool_calls WHERE session_id = ? ORDER BY created_at ASC",
                (source_session_id,),
            ).fetchall()
            for trow in tc_rows:
                new_tc_id = str(uuid4())
                old_msg_id = trow["message_id"]
                new_msg_id = msg_id_map.get(old_msg_id) if old_msg_id is not None else None
                conn.execute(
                    """
                    INSERT INTO tool_calls(id, session_id, message_id, tool_name,
                        arguments_json, result_json, status, duration_ms, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_tc_id,
                        target_session_id,
                        new_msg_id,
                        trow["tool_name"],
                        trow["arguments_json"],
                        trow["result_json"],
                        trow["status"],
                        trow["duration_ms"],
                        trow["created_at"],
                    ),
                )
            # Clone task_states: session_id is PK, just insert with target id.
            ts_rows = conn.execute(
                "SELECT * FROM task_states WHERE session_id = ?", (source_session_id,),
            ).fetchall()
            for tsrow in ts_rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO task_states(session_id, status, iteration_count,
                        last_error, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        target_session_id,
                        tsrow["status"],
                        tsrow["iteration_count"],
                        tsrow["last_error"],
                        tsrow["updated_at"],
                    ),
                )
            # Clone summaries: session_id is PK, just insert with target id.
            sum_rows = conn.execute(
                "SELECT * FROM summaries WHERE session_id = ?", (source_session_id,),
            ).fetchall()
            for srow in sum_rows:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO summaries(session_id, summary, source_message_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        target_session_id,
                        srow["summary"],
                        srow["source_message_id"],
                        srow["updated_at"],
                    ),
                )

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
        if "external_memory_slots_json" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN external_memory_slots_json TEXT")

    @staticmethod
    def _ensure_sessions_acp_metadata_column(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "acp_metadata_json" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN acp_metadata_json TEXT")

    @staticmethod
    def _migrate_add_is_summary_column(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "is_summary" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN is_summary INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_summary_session "
            "ON messages(session_id) WHERE is_summary = 1"
        )

    @staticmethod
    def _migrate_add_is_summarized_column(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "is_summarized" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN is_summarized INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_add_source_column(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "source" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN source TEXT")

    @staticmethod
    def _migrate_add_card_column(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "card_json" not in columns:
            conn.execute("ALTER TABLE messages ADD COLUMN card_json TEXT")

    @staticmethod
    def _decode_message_card(row: sqlite3.Row) -> dict[str, Any] | None:
        """Decode card_json; tolerate missing/NULL/invalid without losing the message.

        Returns None for missing column, NULL, invalid JSON, or non-object JSON
        (scalar/array). Only card_json is tolerated; content_json errors are NOT
        masked and propagate normally.
        """
        keys = set(row.keys())
        if "card_json" not in keys:
            return None
        raw = row["card_json"]
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("invalid card_json ignored: %.200r", raw)
            return None
        if not isinstance(value, dict):
            logger.warning("non-object card_json ignored: %.200r", raw)
            return None
        return value

    @staticmethod
    def _migrate_mark_legacy_middle_summarized(
        conn: sqlite3.Connection, *, protect_first_n: int, protect_last_n: int,
    ) -> None:
        """一次性数据迁移：把存量会话中已被摘要吸收的 middle 消息标记为 is_summarized=1。

        存量会话在 is_summarized 字段引入前已压缩过，middle 消息 is_summarized=0，
        load 时不会被过滤，导致 middle + summary 冗余。本迁移对每个有 summary 的会话，
        把"最新 summary 之前、head 之后、tail 之前"的非 summary 消息标记为 is_summarized=1。
        head = 前 protect_first_n 条，tail = 最新 summary 之前最后 protect_last_n 条。
        """
        rows = conn.execute(
            """
            SELECT session_id, MAX(created_at) as latest_summary_at
            FROM messages WHERE is_summary = 1
            GROUP BY session_id
            """
        ).fetchall()
        for row in rows:
            session_id = row["session_id"]
            latest_summary_at = row["latest_summary_at"]
            msgs = conn.execute(
                """
                SELECT id FROM messages
                WHERE session_id = ? AND is_summary = 0 AND is_summarized = 0
                  AND created_at < ?
                ORDER BY created_at ASC
                """,
                (session_id, latest_summary_at),
            ).fetchall()
            total = len(msgs)
            if total <= protect_first_n + protect_last_n:
                continue
            ids_to_mark = [
                msgs[i]["id"]
                for i in range(protect_first_n, total - protect_last_n)
            ]
            if not ids_to_mark:
                continue
            placeholders = ",".join("?" * len(ids_to_mark))
            conn.execute(
                f"UPDATE messages SET is_summarized = 1 WHERE id IN ({placeholders})",
                ids_to_mark,
            )

    def migrate_session_id_prefixes(self) -> None:
        """Migrate historical session_id prefixes and source values to new format.

        Must be called after all session_id-referencing tables exist (sessions, messages,
        tool_calls, task_states, summaries, sandbox_execution_history, sandbox_released_history,
        scheduled_tasks, scheduled_task_executions, gateway_session_links, gateway_conversations).
        """
        with self._connect() as conn:
            _migrate_session_id_prefixes(conn)


    @staticmethod
    def _session_from_row(row: sqlite3.Row) -> ConversationSession:
        enabled_json = row["external_memory_enabled_json"]
        enabled = json.loads(enabled_json) if enabled_json is not None else None
        slots_json = row["external_memory_slots_json"] if "external_memory_slots_json" in row.keys() else None
        slots = json.loads(slots_json) if slots_json is not None else None
        acp_metadata_json = row["acp_metadata_json"] if "acp_metadata_json" in row.keys() else None
        acp_metadata = json.loads(acp_metadata_json) if acp_metadata_json is not None else None
        created_at = datetime.fromisoformat(row["created_at"])
        updated_at = datetime.fromisoformat(row["updated_at"])
        return ConversationSession(
            id=row["id"], title=row["title"], source=row["source"],
            external_memory_enabled=enabled, external_memory_slots=slots,
            created_at=created_at, updated_at=updated_at,
            acp_metadata=acp_metadata,
        )


_IM_PLATFORMS = SessionSource.im_platforms()


def _compute_new_session_id_and_source(old_id: str, source: str) -> tuple[str, str]:
    # Step 1: normalize source
    new_source = source
    if source == "local":
        new_source = SessionSource.CLI.value
    elif source.startswith("gw/"):
        # legacy spec-260702 format: gw/{platform}
        platform_str = source[len("gw/"):]
        if platform_str in _IM_PLATFORMS:
            new_source = platform_str
    # source already in _IM_PLATFORMS (e.g. "feishu") stays as-is (new format)

    # Step 2: normalize id prefix based on new_source
    new_id = old_id
    if new_source == SessionSource.DASHBOARD.value and old_id.startswith("session-"):
        new_id = "dashboard-" + old_id[len("session-"):]
    elif new_source == SessionSource.API.value and old_id.startswith("tmp-"):
        new_id = "api-" + old_id[len("tmp-"):]
    elif new_source == SessionSource.CLI.value:
        if old_id.startswith("gateway-"):
            new_id = "cli-" + old_id[len("gateway-"):]
        elif not old_id.startswith("cli-"):
            new_id = f"cli-{old_id}"
    elif new_source in _IM_PLATFORMS:
        if old_id.startswith("gateway-"):
            new_id = f"{new_source}-" + old_id[len("gateway-"):]
        elif old_id.startswith("gw-"):
            new_id = f"{new_source}-" + old_id[len("gw-"):]
        elif not old_id.startswith(f"{new_source}-"):
            new_id = f"{new_source}-{old_id}"

    return (new_id, new_source)


def _migrate_session_id_prefixes(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, source FROM sessions").fetchall()
    if not rows:
        return

    existing_tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    session_id_tables = [
        "messages", "tool_calls", "task_states", "summaries",
        "sandbox_execution_history", "sandbox_released_history",
        "scheduled_tasks", "scheduled_task_executions",
        "gateway_session_links",
    ]
    available_tables = [t for t in session_id_tables if t in existing_tables]
    has_gateway_conversations = "gateway_conversations" in existing_tables
    # Task 子域：tasks 表的 origin_session_id / execution_session_id 引用 session_id
    # （ON DELETE SET NULL，但 prefix 迁移需要主动级联更新保持引用一致）。
    # 旧库未启用 Task（tasks 表不存在）时跳过。
    has_tasks_table = "tasks" in existing_tables

    updates: list[tuple[str, str, str]] = []
    for row in rows:
        old_id = row["id"]
        source = row["source"]
        new_id, new_source = _compute_new_session_id_and_source(old_id, source)
        if new_id != old_id or new_source != source:
            existing = conn.execute(
                "SELECT 1 FROM sessions WHERE id=? AND id<>?", (new_id, old_id)
            ).fetchone()
            if existing is not None:
                logger.warning(
                    "skip session_id migration: collision old=%s new=%s source=%s",
                    old_id, new_id, source,
                )
                continue
            updates.append((old_id, new_id, new_source))

    if not updates:
        return

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        for old_id, new_id, new_source in updates:
            if new_id != old_id:
                conn.execute("UPDATE sessions SET id=? WHERE id=?", (new_id, old_id))
                for table in available_tables:
                    conn.execute(
                        f"UPDATE {table} SET session_id=? WHERE session_id=?",
                        (new_id, old_id),
                    )
                if has_gateway_conversations:
                    conn.execute(
                        "UPDATE gateway_conversations SET active_session_id=? WHERE active_session_id=?",
                        (new_id, old_id),
                    )
                # Task 子域：tasks 表的 origin/execution session_id 引用必须级联更新
                if has_tasks_table:
                    conn.execute(
                        "UPDATE tasks SET origin_session_id=? WHERE origin_session_id=?",
                        (new_id, old_id),
                    )
                    conn.execute(
                        "UPDATE tasks SET execution_session_id=? WHERE execution_session_id=?",
                        (new_id, old_id),
                    )
            conn.execute(
                "UPDATE sessions SET source=? WHERE id=?", (new_source, new_id)
            )
        conn.commit()
        logger.info("session_id prefix migrated rows=%d", len(updates))
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
