"""Durable, append-only controlled-write intents; current updates use revision CAS."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from app.domain.host_script_grant import HostScriptIntent
from app.infrastructure.sqlite_support import open_sqlite


class SQLiteHostScriptIntentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = open_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("""CREATE TABLE IF NOT EXISTS host_script_intents (
            revision INTEGER PRIMARY KEY, skill_name TEXT NOT NULL,
            script_relative_path TEXT NOT NULL, sha256 TEXT NOT NULL,
            origin TEXT NOT NULL, updated_at TEXT NOT NULL,
            state TEXT NOT NULL, error TEXT)""")
        return conn

    async def prepare(
        self,
        skill_name: str,
        script_relative_path: str,
        sha256: str,
        origin: str,
        *,
        floor: int = 0,
    ) -> HostScriptIntent:
        def write():
            with closing(self._connect()) as conn, conn:
                conn.execute("BEGIN IMMEDIATE")
                maximum = conn.execute(
                    "SELECT COALESCE(MAX(revision),0) FROM host_script_intents"
                ).fetchone()[0]
                # Microsecond UTC floor survives a reset even while the host is offline.
                # Remote max is also seeded by prepare_write; old intents are never rebased.
                revision = max(time.time_ns() // 1000, maximum + 1, floor + 1)
                updated_at = datetime.now(UTC).isoformat()
                intent = HostScriptIntent(
                    skill_name,
                    script_relative_path,
                    sha256,
                    revision,
                    origin,
                    updated_at,
                    "prepared",
                )
                intent.grant()  # Validate the canonical identity before persisting.
                conn.execute(
                    "INSERT INTO host_script_intents VALUES (?,?,?,?,?,?,?,?)",
                    (
                        revision,
                        skill_name,
                        script_relative_path,
                        sha256,
                        origin,
                        updated_at,
                        "prepared",
                        None,
                    ),
                )
                return intent

        return await asyncio.to_thread(write)

    async def list_current(self) -> list[HostScriptIntent]:
        def read():
            with closing(self._connect()) as conn, conn:
                rows = conn.execute("""SELECT * FROM host_script_intents WHERE revision IN
                    (SELECT COALESCE(MAX(CASE WHEN state != 'prepared' THEN revision END), MAX(revision))
                     FROM host_script_intents GROUP BY skill_name, script_relative_path)
                     ORDER BY revision""").fetchall()
                return [HostScriptIntent(**dict(row)) for row in rows]

        return await asyncio.to_thread(read)

    async def transition(
        self, revision: int, expected: str, state: str, error: str | None = None
    ) -> bool:
        def write():
            with closing(self._connect()) as conn, conn:
                cursor = conn.execute(
                    """UPDATE host_script_intents SET state=?, error=?
                    WHERE revision=? AND state=? AND revision IN
                    (SELECT CASE WHEN ? = 'prepared' THEN MAX(revision)
                     ELSE COALESCE(MAX(CASE WHEN state != 'prepared' THEN revision END), MAX(revision)) END
                     FROM host_script_intents GROUP BY skill_name, script_relative_path)""",
                    (state, error, revision, expected, expected),
                )
                return cursor.rowcount == 1

        return await asyncio.to_thread(write)
