from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.domain.sandbox import ReleasedSandboxInfo
from app.infrastructure.sandbox.released_registry import SQLiteReleasedSandboxRegistry


def test_released_sandbox_history_persists_across_registry_instances(tmp_path):
    db_path = tmp_path / "sessions.db"
    registry = SQLiteReleasedSandboxRegistry(db_path)
    first = ReleasedSandboxInfo(
        session_id="s1",
        sandbox_type="docker",
        sandbox_id="nagent-sandbox-s1",
        created_at=datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc),
        released_at=datetime(2026, 7, 1, 1, 10, tzinfo=timezone.utc),
        reason="manual",
    )
    second = ReleasedSandboxInfo(
        session_id="s2",
        sandbox_type="local",
        sandbox_id=None,
        created_at=datetime(2026, 7, 1, 2, 0, tzinfo=timezone.utc),
        released_at=datetime(2026, 7, 1, 2, 10, tzinfo=timezone.utc),
        reason="idle",
    )

    registry.record(first)
    registry.record(second)

    reloaded = SQLiteReleasedSandboxRegistry(db_path)
    rows = reloaded.list_recent(limit=10)

    assert [row.session_id for row in rows] == ["s2", "s1"]
    assert rows[0].reason == "idle"
    assert rows[1].sandbox_id == "nagent-sandbox-s1"


def test_released_sandbox_history_honors_limit(tmp_path):
    registry = SQLiteReleasedSandboxRegistry(tmp_path / "sessions.db")
    now = datetime.now(timezone.utc)
    for index in range(3):
        registry.record(
            ReleasedSandboxInfo(
                session_id=f"s{index}",
                sandbox_type="docker",
                sandbox_id=None,
                created_at=now,
                released_at=now + timedelta(seconds=index),
                reason="idle",
            )
        )

    rows = registry.list_recent(limit=2)

    assert [row.session_id for row in rows] == ["s2", "s1"]


def test_released_sandbox_history_migrates_legacy_container_name(tmp_path):
    db_path = tmp_path / "sessions.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE sandbox_released_history (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            sandbox_type TEXT NOT NULL,
            container_name TEXT,
            created_at TEXT NOT NULL,
            released_at TEXT NOT NULL,
            reason TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sandbox_released_history(
            id, session_id, sandbox_type, container_name,
            created_at, released_at, reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-1",
            "s-legacy",
            "docker",
            "nagent-sandbox-legacy",
            datetime(2026, 7, 1, 1, 0, tzinfo=timezone.utc).isoformat(),
            datetime(2026, 7, 1, 1, 10, tzinfo=timezone.utc).isoformat(),
            "idle",
        ),
    )
    conn.commit()
    conn.close()

    registry = SQLiteReleasedSandboxRegistry(db_path)
    rows = registry.list_recent(limit=10)

    assert rows[0].session_id == "s-legacy"
    assert rows[0].sandbox_id == "nagent-sandbox-legacy"
