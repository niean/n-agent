"""Tests for app.infrastructure.sqlite_support shared connection helpers."""

from __future__ import annotations

import sqlite3

import pytest

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
    """迁移必须保留已提交数据，重开后仍使用 DELETE 且不再产生 WAL。"""
    path = tmp_path / "s.db"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL").fetchall()
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(path)
    try:
        apply_journal_mode(conn)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "delete"
    assert not (tmp_path / "s.db-wal").exists()

    # A leftover SHM pathname is not proof of WAL use. SQLite's journal
    # mode and the reopened data are the contract; deleting sidecars here
    # could race a different connection. See sqlite.org/wal.html and
    # sqlite.org/c3ref/c_fcntl_begin_atomic_write.html (PERSIST_WAL).
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert conn.execute("SELECT id FROM t").fetchall() == [(1,)]
        conn.execute("INSERT INTO t VALUES (2)")
        conn.commit()
    finally:
        conn.close()
    assert not (tmp_path / "s.db-wal").exists()
    # Immutable reads do not recover WAL: the committed rows must now be
    # self-contained in the main database, not merely visible via sidecars.
    readonly = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        assert readonly.execute("SELECT id FROM t ORDER BY id").fetchall() == [(1,), (2,)]
    finally:
        readonly.close()


def test_apply_journal_mode_refuses_live_wal_reader_without_losing_data(tmp_path):
    path = tmp_path / "s.db"
    writer = sqlite3.connect(path)
    reader = sqlite3.connect(path)
    migration = sqlite3.connect(path, timeout=0.01)
    try:
        writer.execute("PRAGMA journal_mode=WAL").fetchall()
        writer.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
        writer.execute("INSERT INTO t VALUES (1)")
        writer.commit()
        reader.execute("BEGIN")
        assert reader.execute("SELECT id FROM t").fetchall() == [(1,)]
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            apply_journal_mode(migration)
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (tmp_path / "s.db-wal").exists()
        assert (tmp_path / "s.db-shm").exists()
        writer.execute("INSERT INTO t VALUES (2)")
        writer.commit()
        assert reader.execute("SELECT id FROM t").fetchall() == [(1,)]
    finally:
        migration.close()
        reader.rollback()
        reader.close()
        writer.close()
    conn = sqlite3.connect(path)
    try:
        apply_journal_mode(conn)
        assert conn.execute("SELECT id FROM t ORDER BY id").fetchall() == [(1,), (2,)]
    finally:
        conn.close()


def test_apply_journal_mode_rejects_a_mode_sqlite_cannot_change():
    conn = sqlite3.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError, match="delete_required"):
            apply_journal_mode(conn)
    finally:
        conn.close()
