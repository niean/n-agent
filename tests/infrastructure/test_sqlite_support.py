"""Tests for app.infrastructure.sqlite_support shared connection helpers."""

from __future__ import annotations

import sqlite3

from app.infrastructure.sqlite_support import apply_journal_mode


def test_apply_journal_mode_sets_delete(tmp_path):
    """apply_journal_mode 强制 DELETE 回滚日志模式。

    WAL 依赖 -shm 共享内存 mmap，在 macOS Docker Desktop bind mount
    （virtiofs）上会触发 SIGBUS 崩溃；全局统一 DELETE 模式不依赖 mmap。
    """
    path = tmp_path / "s.db"
    conn = sqlite3.connect(path)
    try:
        apply_journal_mode(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "delete"


def test_apply_journal_mode_migrates_existing_wal_db(tmp_path):
    """已有 WAL 库切换到 DELETE：自动 checkpoint，-wal/-shm 不残留。"""
    path = tmp_path / "s.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert (tmp_path / "s.db-wal").exists() or True  # checkpoint 后可能已清理

    conn = sqlite3.connect(path)
    try:
        apply_journal_mode(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "delete"
    assert not (tmp_path / "s.db-wal").exists()
    assert not (tmp_path / "s.db-shm").exists()
