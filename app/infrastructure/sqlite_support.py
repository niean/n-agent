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


def open_sqlite(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open ``path``. With ``read_only`` the SQLite URI ``mode=ro`` is used:
    a missing file raises instead of being created, and every write raises
    ``sqlite3.OperationalError: attempt to write a readonly database``.
    """
    if not read_only:
        return sqlite3.connect(path)
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
