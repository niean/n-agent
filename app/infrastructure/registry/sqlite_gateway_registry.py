from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.gateway import GatewayConversation, GatewayHomeTarget, GatewaySessionKey, GatewaySessionLink
from app.domain.platform import Platform


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

    async def mark_event_processed(self, source: str, event_id: str, message_id: str = "") -> bool:
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO gateway_processed_events(id, platform, event_id, message_id, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (str(uuid4()), source, event_id, message_id or None, _now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    async def set_home_target(self, target: GatewayHomeTarget) -> GatewayHomeTarget:
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gateway_home_targets(platform, receive_id, receive_id_type, thread_id, display_name, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform) DO UPDATE SET
                    receive_id=excluded.receive_id,
                    receive_id_type=excluded.receive_id_type,
                    thread_id=excluded.thread_id,
                    display_name=excluded.display_name,
                    updated_at=excluded.updated_at
                """,
                (
                    target.platform.value,
                    target.receive_id,
                    target.receive_id_type,
                    target.thread_id,
                    target.display_name,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM gateway_home_targets WHERE platform = ?", (target.platform.value,)).fetchone()
        assert row is not None
        return _home_target_from_row(row)

    async def get_home_target(self, platform: Platform) -> GatewayHomeTarget | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM gateway_home_targets WHERE platform = ?", (platform.value,)).fetchone()
        return _home_target_from_row(row) if row is not None else None

    async def list_conversations(
        self, platform: Platform | None = None, limit: int = 100, offset: int = 0
    ) -> list[GatewayConversation]:
        with self._connect() as conn:
            if platform is None:
                rows = conn.execute(
                    """
                    SELECT * FROM gateway_conversations
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM gateway_conversations
                    WHERE platform = ?
                    ORDER BY updated_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (platform.value, limit, offset),
                ).fetchall()
        conversations = [_conversation_from_row(row) for row in rows]
        return [conversation for conversation in conversations if conversation is not None]

    async def count_conversations(self, platform: Platform) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS c FROM gateway_conversations WHERE platform = ?",
                (platform.value,),
            ).fetchone()
        return int(row["c"]) if row is not None else 0

    async def get_last_active(self, platform: Platform) -> datetime | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) AS ts FROM gateway_conversations WHERE platform = ?",
                (platform.value,),
            ).fetchone()
        if row is None or row["ts"] is None:
            return None
        return datetime.fromisoformat(row["ts"])


def _initialize_gateway_schema(conn: sqlite3.Connection) -> None:
    _migrate_processed_events_message_id_nullable(conn)
    _migrate_legacy_source_columns(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gateway_conversations (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            platform_session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL,
            active_session_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform, platform_session_id, thread_id),
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
            platform TEXT NOT NULL,
            event_id TEXT NOT NULL,
            message_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(platform, event_id),
            UNIQUE(platform, message_id)
        );
        CREATE TABLE IF NOT EXISTS gateway_home_targets (
            platform TEXT PRIMARY KEY,
            receive_id TEXT NOT NULL,
            receive_id_type TEXT NOT NULL,
            thread_id TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        """
    )


def _migrate_processed_events_message_id_nullable(conn: sqlite3.Connection) -> None:
    table = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'gateway_processed_events'"
    ).fetchone()
    if table is None or "message_id TEXT NOT NULL" not in str(table["sql"]):
        return
    sql = str(table["sql"])
    has_legacy_column = "source_type" in sql
    column_decl = "source_type TEXT NOT NULL" if has_legacy_column else "platform TEXT NOT NULL"
    unique_event = "UNIQUE(source_type, event_id)" if has_legacy_column else "UNIQUE(platform, event_id)"
    unique_message = "UNIQUE(source_type, message_id)" if has_legacy_column else "UNIQUE(platform, message_id)"
    select_columns = (
        "id, source_type, event_id, NULLIF(message_id, ''), created_at"
        if has_legacy_column
        else "id, platform, event_id, NULLIF(message_id, ''), created_at"
    )
    conn.executescript(
        f"""
        ALTER TABLE gateway_processed_events RENAME TO gateway_processed_events_old;
        CREATE TABLE gateway_processed_events (
            id TEXT PRIMARY KEY,
            {column_decl},
            event_id TEXT NOT NULL,
            message_id TEXT,
            created_at TEXT NOT NULL,
            {unique_event},
            {unique_message}
        );
        INSERT OR IGNORE INTO gateway_processed_events SELECT {select_columns} FROM gateway_processed_events_old;
        DROP TABLE gateway_processed_events_old;
        """
    )


def _migrate_legacy_source_columns(conn: sqlite3.Connection) -> None:
    """Rename source_type/source_id columns to platform/platform_session_id (fail-fast)."""
    try:
        conv_cols = {row["name"] for row in conn.execute("PRAGMA table_info(gateway_conversations)").fetchall()}
        if conv_cols and "source_type" in conv_cols:
            conn.execute("ALTER TABLE gateway_conversations RENAME COLUMN source_type TO platform")
        if conv_cols and "source_id" in conv_cols:
            conn.execute("ALTER TABLE gateway_conversations RENAME COLUMN source_id TO platform_session_id")
        proc_cols = {row["name"] for row in conn.execute("PRAGMA table_info(gateway_processed_events)").fetchall()}
        if proc_cols and "source_type" in proc_cols:
            conn.execute("ALTER TABLE gateway_processed_events RENAME COLUMN source_type TO platform")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"failed to migrate gateway legacy source columns: {exc}; SQLite >= 3.34 required"
        ) from exc


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
        INSERT INTO gateway_conversations(id, platform, platform_session_id, thread_id, display_name, active_session_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
        """,
        (conversation_id, key.source_value, key.platform_session_id, key.thread_id, key.display_name, now, now),
    )
    return conversation_id


def _get_conversation(conn: sqlite3.Connection, key: GatewaySessionKey) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM gateway_conversations
        WHERE platform = ? AND platform_session_id = ? AND thread_id = ?
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


def _conversation_from_row(row: sqlite3.Row) -> GatewayConversation | None:
    try:
        platform = Platform(row["platform"])
    except ValueError:
        return None
    return GatewayConversation(
        id=row["id"],
        platform=platform,
        platform_session_id=row["platform_session_id"],
        thread_id=row["thread_id"],
        display_name=row["display_name"],
        active_session_id=row["active_session_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _home_target_from_row(row: sqlite3.Row) -> GatewayHomeTarget:
    return GatewayHomeTarget(
        platform=Platform(row["platform"]),
        receive_id=row["receive_id"],
        receive_id_type=row["receive_id_type"],
        thread_id=row["thread_id"],
        display_name=row["display_name"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
