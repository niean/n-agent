from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.gateway import GatewaySessionKey, GatewaySessionLink, InteractionSourceType


class SQLiteGatewaySessionRegistry:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            _initialize_gateway_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def get_active_session(self, key: GatewaySessionKey) -> GatewaySessionLink | None:
        with self._connect() as conn:
            conversation = _get_conversation(conn, key)
            if conversation is None or conversation["active_session_id"] is None:
                return None
            row = conn.execute(
                """
                SELECT l.*, c.display_name
                FROM gateway_session_links l
                JOIN gateway_conversations c ON c.id = l.conversation_id
                WHERE l.conversation_id = ? AND l.session_id = ?
                """,
                (conversation["id"], conversation["active_session_id"]),
            ).fetchone()
        return _link_from_row(row) if row is not None else None

    async def create_session_link(self, key: GatewaySessionKey, session_id: str) -> GatewaySessionLink:
        now = _now()
        with self._connect() as conn:
            conversation_id = _ensure_conversation(conn, key, now)
            link_id = str(uuid4())
            conn.execute(
                """
                INSERT OR IGNORE INTO gateway_session_links(id, conversation_id, session_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (link_id, conversation_id, session_id, now, now),
            )
            conn.execute(
                """
                UPDATE gateway_session_links SET updated_at = ?
                WHERE conversation_id = ? AND session_id = ?
                """,
                (now, conversation_id, session_id),
            )
            conn.execute(
                """
                UPDATE gateway_conversations SET active_session_id = ?, display_name = ?, updated_at = ?
                WHERE id = ?
                """,
                (session_id, key.display_name, now, conversation_id),
            )
            row = _get_link(conn, conversation_id, session_id)
        assert row is not None
        return _link_from_row(row)

    async def set_active_session(self, key: GatewaySessionKey, session_id: str) -> GatewaySessionLink:
        now = _now()
        with self._connect() as conn:
            conversation = _get_conversation(conn, key)
            if conversation is None:
                return await self.create_session_link(key, session_id)
            row = _get_link(conn, conversation["id"], session_id)
            if row is None:
                return await self.create_session_link(key, session_id)
            conn.execute(
                "UPDATE gateway_conversations SET active_session_id = ?, updated_at = ? WHERE id = ?",
                (session_id, now, conversation["id"]),
            )
            conn.execute("UPDATE gateway_session_links SET updated_at = ? WHERE id = ?", (now, row["id"]))
            row = _get_link(conn, conversation["id"], session_id)
        assert row is not None
        return _link_from_row(row)

    async def list_session_links(self, key: GatewaySessionKey) -> list[GatewaySessionLink]:
        with self._connect() as conn:
            conversation = _get_conversation(conn, key)
            if conversation is None:
                return []
            rows = conn.execute(
                """
                SELECT l.*, c.display_name
                FROM gateway_session_links l
                JOIN gateway_conversations c ON c.id = l.conversation_id
                WHERE l.conversation_id = ?
                ORDER BY l.updated_at DESC
                """,
                (conversation["id"],),
            ).fetchall()
        return [_link_from_row(row) for row in rows]

    async def delete_session_link(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM gateway_session_links WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE gateway_conversations SET active_session_id = NULL WHERE active_session_id = ?",
                (session_id,),
            )

    async def mark_event_processed(self, source_type: InteractionSourceType, event_id: str, message_id: str = "") -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_processed_events(id, source_type, event_id, message_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid4()), source_type.value, event_id, message_id, _now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True


def _initialize_gateway_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gateway_conversations (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL,
            active_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_type, source_id, thread_id),
            FOREIGN KEY(active_session_id) REFERENCES sessions(id)
        );
        CREATE TABLE IF NOT EXISTS gateway_session_links (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES gateway_conversations(id),
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            UNIQUE(conversation_id, session_id)
        );
        CREATE TABLE IF NOT EXISTS gateway_processed_events (
            id TEXT PRIMARY KEY,
            source_type TEXT NOT NULL,
            event_id TEXT NOT NULL,
            message_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(source_type, event_id),
            UNIQUE(source_type, message_id)
        );
        """
    )


def _ensure_conversation(conn: sqlite3.Connection, key: GatewaySessionKey, now: str) -> str:
    row = _get_conversation(conn, key)
    if row is not None:
        conn.execute(
            "UPDATE gateway_conversations SET display_name = ?, updated_at = ? WHERE id = ?",
            (key.display_name, now, row["id"]),
        )
        return row["id"]
    conversation_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO gateway_conversations(id, source_type, source_id, thread_id, display_name, active_session_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (conversation_id, key.source_type.value, key.source_id, key.thread_id, key.display_name, now, now),
    )
    return conversation_id


def _get_conversation(conn: sqlite3.Connection, key: GatewaySessionKey) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM gateway_conversations
        WHERE source_type = ? AND source_id = ? AND thread_id = ?
        """,
        key.conversation_parts,
    ).fetchone()


def _get_link(conn: sqlite3.Connection, conversation_id: str, session_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT l.*, c.display_name
        FROM gateway_session_links l
        JOIN gateway_conversations c ON c.id = l.conversation_id
        WHERE l.conversation_id = ? AND l.session_id = ?
        """,
        (conversation_id, session_id),
    ).fetchone()


def _link_from_row(row: sqlite3.Row) -> GatewaySessionLink:
    return GatewaySessionLink(
        id=row["id"],
        conversation_id=row["conversation_id"],
        session_id=row["session_id"],
        display_name=row["display_name"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
