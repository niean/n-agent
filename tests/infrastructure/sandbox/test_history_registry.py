from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain.sandbox import SandboxExecutionHistoryEntry
from app.infrastructure.sandbox.history_registry import SQLiteSandboxExecutionHistoryRegistry


def _entry(entry_id: str, session_id: str, created_at: datetime) -> SandboxExecutionHistoryEntry:
    return SandboxExecutionHistoryEntry(
        id=entry_id,
        session_id=session_id,
        code_hash=f"hash-{entry_id}",
        code="print(1)",
        result={"status": "success", "authorized_callback_tools": ["read_file"]},
        status="success",
        duration_ms=12,
        authorized_callback_tools=["read_file"],
        created_at=created_at,
    )


def test_sandbox_execution_history_persists_across_registry_instances(tmp_path):
    db_path = tmp_path / "sessions.db"
    now = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    registry = SQLiteSandboxExecutionHistoryRegistry(db_path)
    registry.record(_entry("tc-1", "s1", now))
    registry.record(_entry("tc-2", "s2", now + timedelta(seconds=1)))

    reloaded = SQLiteSandboxExecutionHistoryRegistry(db_path)
    rows = reloaded.list_recent(limit=10)

    assert [row.id for row in rows] == ["tc-2", "tc-1"]
    assert rows[0].code == "print(1)"
    assert rows[0].authorized_callback_tools == ["read_file"]
    assert all(row.execution_type == "execute_code" for row in rows)


def test_sandbox_execution_history_records_terminal_execution_type(tmp_path):
    db_path = tmp_path / "sessions.db"
    now = datetime.now(timezone.utc)
    registry = SQLiteSandboxExecutionHistoryRegistry(db_path)
    registry.record(SandboxExecutionHistoryEntry(
        id="tc-term-1",
        session_id="s1",
        code_hash="hash-term",
        code="ls /workspace",
        result={"status": "success"},
        status="success",
        duration_ms=5,
        authorized_callback_tools=[],
        created_at=now,
        execution_type="terminal",
    ))

    reloaded = SQLiteSandboxExecutionHistoryRegistry(db_path)
    rows = reloaded.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].execution_type == "terminal"


def test_sandbox_execution_history_migrates_legacy_schema_without_execution_type(tmp_path):
    db_path = tmp_path / "sessions.db"
    now = datetime.now(timezone.utc)
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sandbox_execution_history (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                code TEXT NOT NULL,
                result_json TEXT,
                status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                authorized_callback_tools_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO sandbox_execution_history(
                id, session_id, code_hash, code, result_json, status,
                duration_ms, authorized_callback_tools_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tc-legacy-1", "s1", "hash-legacy", "print(1)",
                '{"status": "success"}', "success", 8, "[]", now.isoformat(),
            ),
        )

    registry = SQLiteSandboxExecutionHistoryRegistry(db_path)
    rows = registry.list_recent(limit=10)
    assert len(rows) == 1
    assert rows[0].execution_type == "execute_code"


def test_sandbox_execution_history_filters_and_deletes(tmp_path):
    db_path = tmp_path / "sessions.db"
    now = datetime.now(timezone.utc)
    registry = SQLiteSandboxExecutionHistoryRegistry(db_path)
    registry.record(_entry("tc-1", "s1", now))
    registry.record(_entry("tc-2", "s2", now + timedelta(seconds=1)))

    assert [row.id for row in registry.list_recent(session_id="s1")] == ["tc-1"]
    assert registry.delete("tc-1") is True
    assert registry.delete("missing") is False
    assert registry.list_recent(session_id="s1") == []
    assert [row.id for row in registry.list_recent()] == ["tc-2"]
