"""Shared SQLite connection helper for infrastructure adapters.

The migration surface (`n-agent config export`, `config import --dry-run`) has
to open an existing deployment database without creating it and without any
chance of a stray write. Every SQLite adapter routes its ``sqlite3.connect``
through :func:`open_sqlite` so the read-only mode is one switch, not one
re-implementation per registry.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote


def apply_journal_mode(conn: sqlite3.Connection) -> None:
    """Force ``journal_mode=DELETE`` on every writable connection.

    WAL requires mmap of the ``-shm`` shared-memory index; on macOS Docker
    Desktop host bind mounts (virtiofs/osxfs) mmap is unreliable and kills
    the process with SIGBUS under concurrent writes. DELETE mode (rollback
    journal) never mmaps, so it is safe on every deployment target. The
    trade-off -- readers block during a write -- is acceptable for a
    single-instance product and is absorbed by ``busy_timeout``.
    Switching an existing WAL database checkpoints and removes ``-wal`` /
    ``-shm`` automatically.
    """
    conn.execute("PRAGMA journal_mode = DELETE")


def open_sqlite(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open ``path``. With ``read_only`` the SQLite URI ``mode=ro`` is used:
    a missing file raises instead of being created, and every write raises
    ``sqlite3.OperationalError: attempt to write a readonly database``.
    """
    if not read_only:
        return sqlite3.connect(path)
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
