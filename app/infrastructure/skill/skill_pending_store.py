from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.domain.skill import (
    SkillPendingWrite,
    SkillWriteAction,
    SkillWriteOrigin,
)


def _initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS skill_pending_writes (
            pending_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            skill_name TEXT NOT NULL,
            origin TEXT NOT NULL,
            summary TEXT NOT NULL,
            diff TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL,
            error TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )


def _dt_str(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt_parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _row_to_pending(
    row: sqlite3.Row, state_override: str | None = None
) -> SkillPendingWrite:
    return SkillPendingWrite(
        pending_id=row["pending_id"],
        action=SkillWriteAction(row["action"]),
        skill_name=row["skill_name"],
        origin=SkillWriteOrigin(row["origin"]),
        summary=row["summary"],
        diff=row["diff"],
        payload=json.loads(row["payload_json"] or "{}"),
        state=state_override if state_override is not None else row["state"],
        error=row["error"],
        created_at=_dt_parse(row["created_at"]),
        updated_at=_dt_parse(row["updated_at"]),
    )


class SkillPendingStore:
    """SQLite-backed implementation of the :class:`SkillPendingStore` Protocol.

    Each public method is async and delegates the synchronous ``sqlite3`` work
    to ``asyncio.to_thread`` so the event loop is never blocked. Every call
    opens an independent short-lived connection (mirroring
    ``SQLiteSkillRegistry``/``SkillUsageStore``).
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            _initialize_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # synchronous helpers (run inside asyncio.to_thread)
    # ------------------------------------------------------------------

    def _stage_sync(self, write: SkillPendingWrite) -> str:
        # Always generate a fresh pending_id; ignore any id on the input write.
        pending_id = uuid4().hex
        now = datetime.now(timezone.utc)
        created_at = write.created_at or now
        updated_at = write.updated_at or now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO skill_pending_writes(
                    pending_id, action, skill_name, origin, summary, diff,
                    payload_json, state, error, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending_id,
                    write.action.value,
                    write.skill_name,
                    write.origin.value,
                    write.summary,
                    write.diff,
                    json.dumps(write.payload),
                    "pending",  # staged writes always start pending
                    write.error,
                    _dt_str(created_at),
                    _dt_str(updated_at),
                ),
            )
        return pending_id

    def _list_sync(self) -> list[SkillPendingWrite]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_pending_writes ORDER BY created_at"
            ).fetchall()
        return [_row_to_pending(row) for row in rows]

    def _get_sync(self, pending_id: str) -> SkillPendingWrite | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_pending_writes WHERE pending_id = ?",
                (pending_id,),
            ).fetchone()
        return _row_to_pending(row) if row else None

    def _approve_take_sync(self, pending_id: str) -> SkillPendingWrite | None:
        # Atomic: UPDATE + SELECT in a single transaction. The conditional
        # UPDATE (state='pending') makes this idempotent -- a second take finds
        # no matching row and returns None.
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE skill_pending_writes SET state = 'approved_in_progress', "
                "updated_at = ? WHERE pending_id = ? AND state = 'pending'",
                (now, pending_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM skill_pending_writes WHERE pending_id = ?",
                (pending_id,),
            ).fetchone()
        if row is None:
            return None
        # Return the ORIGINAL state ('pending') at take time, not the DB's new
        # 'approved_in_progress'. Callers consume the snapshot pre-transition.
        return _row_to_pending(row, state_override="pending")

    def _reject_sync(self, pending_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM skill_pending_writes WHERE pending_id = ?",
                (pending_id,),
            )
            return cursor.rowcount > 0

    def _clear_sync(self, pending_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM skill_pending_writes WHERE pending_id = ?",
                (pending_id,),
            )

    # ------------------------------------------------------------------
    # async public API (SkillPendingStore Protocol)
    # ------------------------------------------------------------------

    async def stage(self, write: SkillPendingWrite) -> str:
        return await asyncio.to_thread(self._stage_sync, write)

    async def list(self) -> list[SkillPendingWrite]:
        return await asyncio.to_thread(self._list_sync)

    async def get(self, pending_id: str) -> SkillPendingWrite | None:
        return await asyncio.to_thread(self._get_sync, pending_id)

    async def approve_take(self, pending_id: str) -> SkillPendingWrite | None:
        return await asyncio.to_thread(self._approve_take_sync, pending_id)

    async def reject(self, pending_id: str) -> bool:
        return await asyncio.to_thread(self._reject_sync, pending_id)

    async def clear(self, pending_id: str) -> None:
        await asyncio.to_thread(self._clear_sync, pending_id)
