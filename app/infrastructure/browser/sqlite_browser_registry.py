"""SQLite-backed implementation of BrowserSessionRegistry.

Mirrors the pattern of SQLiteSkillRegistry: independent ``_connect()``,
idempotent migration, synchronous SQLite calls inside async methods.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.browser import (
    BrowserBackendType,
    BrowserSession,
    BrowserSessionStatus,
)

_UNSET: Any = object()  # sentinel: distinguish "keep existing" from "clear to None"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_sessions (
    id TEXT PRIMARY KEY,
    n_agent_session_id TEXT NOT NULL,
    backend_type TEXT NOT NULL CHECK(backend_type IN ('host_cdp', 'container')),
    status TEXT NOT NULL CHECK(status IN (
        'pending_authorization', 'active', 'paused', 'takeover', 'degraded', 'closed'
    )),
    profile_ref TEXT NOT NULL,
    document_revision INTEGER NOT NULL DEFAULT 0,
    pre_takeover_status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_browser_sessions_nagent_status
    ON browser_sessions(n_agent_session_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_session_active
    ON browser_sessions(n_agent_session_id, backend_type)
    WHERE status != 'closed';

CREATE TABLE IF NOT EXISTS browser_profile_leases (
    profile_ref TEXT PRIMARY KEY,
    browser_session_id TEXT NOT NULL UNIQUE,
    acquired_at TEXT NOT NULL,
    FOREIGN KEY (browser_session_id) REFERENCES browser_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS browser_host_grants (
    browser_session_id TEXT PRIMARY KEY,
    n_agent_session_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (browser_session_id) REFERENCES browser_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS browser_actions (
    id TEXT PRIMARY KEY,
    browser_session_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    arguments_summary_json TEXT NOT NULL,
    status TEXT NOT NULL,
    safe_url TEXT,
    title TEXT,
    text_summary TEXT,
    warning_code TEXT,
    error_code TEXT,
    duration_ms INTEGER NOT NULL,
    document_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (browser_session_id) REFERENCES browser_sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_browser_actions_session_created_id
    ON browser_actions(browser_session_id, created_at, id);
"""


class SqliteBrowserSessionRegistry:
    """SQLite-backed BrowserSessionRegistry."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # -- create / get ------------------------------------------------------

    async def create(self, session: BrowserSession) -> None:
        now = datetime.now(timezone.utc).isoformat()
        created_at = (session.created_at or datetime.now(timezone.utc)).isoformat()
        updated_at = (session.updated_at or datetime.now(timezone.utc)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO browser_sessions(
                    id, n_agent_session_id, backend_type, status, profile_ref,
                    document_revision, pre_takeover_status, created_at, updated_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.id,
                    session.bound_n_agent_session_id,
                    session.backend_type.value,
                    session.status.value,
                    session.profile_ref,
                    session.document_revision,
                    session.pre_takeover_status.value if session.pre_takeover_status else None,
                    created_at,
                    updated_at,
                    session.closed_at.isoformat() if session.closed_at else None,
                ),
            )

    async def get(self, session_id: str) -> BrowserSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM browser_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row) if row else None

    async def list_by_n_agent_session(self, n_agent_session_id: str) -> list[BrowserSession]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM browser_sessions WHERE n_agent_session_id = ? ORDER BY created_at",
                (n_agent_session_id,),
            ).fetchall()
        return [_session_from_row(row) for row in rows]

    # -- compare_and_set_status --------------------------------------------

    async def compare_and_set_status(
        self,
        session_id: str,
        expected: BrowserSessionStatus,
        next_status: BrowserSessionStatus,
        *,
        pre_takeover_status: BrowserSessionStatus | None = _UNSET,  # type: ignore[assignment]
        document_revision: int | None = _UNSET,  # type: ignore[assignment]
    ) -> BrowserSession | None:
        now = datetime.now(timezone.utc).isoformat()
        set_parts: list[str] = ["status = ?", "updated_at = ?"]
        params: list[Any] = [next_status.value, now]

        if pre_takeover_status is not _UNSET:
            set_parts.append("pre_takeover_status = ?")
            params.append(
                pre_takeover_status.value if pre_takeover_status is not None else None
            )
        if document_revision is not _UNSET:
            set_parts.append("document_revision = ?")
            params.append(document_revision)
        if next_status is BrowserSessionStatus.CLOSED:
            set_parts.append("closed_at = ?")
            params.append(now)

        params.extend([session_id, expected.value])
        sql = (
            f"UPDATE browser_sessions SET {', '.join(set_parts)} "
            f"WHERE id = ? AND status = ?"
        )
        with self._connect() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM browser_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return _session_from_row(row) if row else None

    # -- profile leases -----------------------------------------------------

    async def acquire_profile_lease(self, profile_ref: str, session_id: str) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO browser_profile_leases(profile_ref, browser_session_id, acquired_at)
                    VALUES (?, ?, ?)
                    """,
                    (profile_ref, session_id, now),
                )
            return True
        except sqlite3.IntegrityError:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT browser_session_id FROM browser_profile_leases WHERE profile_ref = ?",
                    (profile_ref,),
                ).fetchone()
            return row is not None and row["browser_session_id"] == session_id

    async def release_profile_lease(self, profile_ref: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM browser_profile_leases WHERE profile_ref = ?",
                (profile_ref,),
            )

    # -- action summaries --------------------------------------------------

    async def append_action_summary(self, session_id: str, summary: dict[str, Any]) -> None:
        action_id = summary.get("id") or uuid4().hex
        created_at = summary.get("created_at") or datetime.now(timezone.utc).isoformat()
        arguments_summary = summary.get("arguments_summary", {})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO browser_actions(
                    id, browser_session_id, action_type, arguments_summary_json,
                    status, safe_url, title, text_summary, warning_code, error_code,
                    duration_ms, document_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    session_id,
                    summary["action_type"],
                    json.dumps(arguments_summary),
                    summary["status"],
                    summary.get("safe_url"),
                    summary.get("title"),
                    summary.get("text_summary"),
                    summary.get("warning_code"),
                    summary.get("error_code"),
                    summary["duration_ms"],
                    summary["document_revision"],
                    created_at,
                ),
            )

    async def list_actions(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM browser_actions
                WHERE browser_session_id = ?
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [_action_from_row(row) for row in rows]

    async def count_actions(self, session_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM browser_actions WHERE browser_session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    # -- close -------------------------------------------------------------

    async def close(self, session_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE browser_sessions
                SET status = ?, closed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (BrowserSessionStatus.CLOSED.value, now, now, session_id),
            )
            conn.execute(
                "DELETE FROM browser_profile_leases WHERE browser_session_id = ?",
                (session_id,),
            )
            conn.execute(
                "DELETE FROM browser_host_grants WHERE browser_session_id = ?",
                (session_id,),
            )

    # -- host grants (extra methods beyond Protocol) -----------------------

    async def record_host_grant(
        self,
        session_id: str,
        n_agent_session_id: str,
        actor_id: str,
        policy_version: str,
        expires_at: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO browser_host_grants(
                    browser_session_id, n_agent_session_id, actor_id,
                    policy_version, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(browser_session_id) DO UPDATE SET
                    n_agent_session_id = excluded.n_agent_session_id,
                    actor_id = excluded.actor_id,
                    policy_version = excluded.policy_version,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at
                """,
                (session_id, n_agent_session_id, actor_id, policy_version, expires_at, now),
            )

    async def revoke_host_grant(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM browser_host_grants WHERE browser_session_id = ?",
                (session_id,),
            )

    async def get_host_grant(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM browser_host_grants WHERE browser_session_id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    async def expire_host_grants(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM browser_host_grants WHERE expires_at < ?", (now,)
            )
            return cursor.rowcount


# -- row -> domain helpers --------------------------------------------------

def _session_from_row(row: sqlite3.Row) -> BrowserSession:
    return BrowserSession(
        id=row["id"],
        bound_n_agent_session_id=row["n_agent_session_id"],
        backend_type=BrowserBackendType(row["backend_type"]),
        status=BrowserSessionStatus(row["status"]),
        profile_ref=row["profile_ref"],
        document_revision=row["document_revision"],
        pre_takeover_status=(
            BrowserSessionStatus(row["pre_takeover_status"])
            if row["pre_takeover_status"]
            else None
        ),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        closed_at=_parse_dt(row["closed_at"]),
    )


def _action_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "browser_session_id": row["browser_session_id"],
        "action_type": row["action_type"],
        "arguments_summary": json.loads(row["arguments_summary_json"]),
        "status": row["status"],
        "safe_url": row["safe_url"],
        "title": row["title"],
        "text_summary": row["text_summary"],
        "warning_code": row["warning_code"],
        "error_code": row["error_code"],
        "duration_ms": row["duration_ms"],
        "document_revision": row["document_revision"],
        "created_at": row["created_at"],
    }


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
