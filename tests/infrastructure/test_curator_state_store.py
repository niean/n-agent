from __future__ import annotations

import asyncio

import pytest

from app.domain.skill import CuratorState
from app.infrastructure.skill.curator_state_store import SqliteCuratorStateStore


def test_load_default_when_empty(tmp_path):
    store = SqliteCuratorStateStore(str(tmp_path / "curator.db"))
    state = asyncio.run(store.load())
    assert isinstance(state, CuratorState)
    assert state.last_run_at is None
    assert state.paused is False
    assert state.run_count == 0


def test_save_and_load_roundtrip(tmp_path):
    store = SqliteCuratorStateStore(str(tmp_path / "curator.db"))
    original = CuratorState(
        last_run_at="2026-07-17T10:00:00+00:00",
        last_run_duration_seconds=12.5,
        last_run_summary="auto: 1 archived",
        last_report_path="/app/locals/curator/20260717-100000/run.json",
        paused=False,
        run_count=3,
    )
    asyncio.run(store.save(original))
    loaded = asyncio.run(store.load())
    assert loaded.last_run_at == "2026-07-17T10:00:00+00:00"
    assert loaded.last_run_duration_seconds == 12.5
    assert loaded.last_run_summary == "auto: 1 archived"
    assert loaded.last_report_path.endswith("run.json")
    assert loaded.run_count == 3
    assert loaded.paused is False


def test_set_paused_true_and_false(tmp_path):
    store = SqliteCuratorStateStore(str(tmp_path / "curator.db"))
    asyncio.run(store.set_paused(True))
    assert asyncio.run(store.load()).paused is True
    asyncio.run(store.set_paused(False))
    assert asyncio.run(store.load()).paused is False


def test_set_paused_preserves_other_fields(tmp_path):
    store = SqliteCuratorStateStore(str(tmp_path / "curator.db"))
    asyncio.run(
        store.save(
            CuratorState(
                last_run_at="2026-07-17T10:00:00+00:00",
                run_count=5,
                last_run_summary="auto: no changes",
            )
        )
    )
    asyncio.run(store.set_paused(True))
    loaded = asyncio.run(store.load())
    assert loaded.paused is True
    assert loaded.run_count == 5
    assert loaded.last_run_at == "2026-07-17T10:00:00+00:00"
    assert loaded.last_run_summary == "auto: no changes"


def test_schema_migration_idempotent(tmp_path):
    db_path = str(tmp_path / "curator.db")
    SqliteCuratorStateStore(db_path)
    # 再次构造不报错（迁移幂等）
    store2 = SqliteCuratorStateStore(db_path)
    state = asyncio.run(store2.load())
    assert isinstance(state, CuratorState)


def test_cross_instance_read(tmp_path):
    db_path = str(tmp_path / "curator.db")
    store1 = SqliteCuratorStateStore(db_path)
    asyncio.run(store1.save(CuratorState(run_count=7, last_run_at="2026-07-17T10:00:00+00:00")))
    # 新实例读到已存 state
    store2 = SqliteCuratorStateStore(db_path)
    loaded = asyncio.run(store2.load())
    assert loaded.run_count == 7
    assert loaded.last_run_at == "2026-07-17T10:00:00+00:00"


def test_corrupt_json_returns_default(tmp_path):
    store = SqliteCuratorStateStore(str(tmp_path / "curator.db"))
    # 直接写坏数据
    import sqlite3
    with sqlite3.connect(str(tmp_path / "curator.db")) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO curator_state(key, value) VALUES (?, ?)",
            ("default", "{not valid json"),
        )
    state = asyncio.run(store.load())
    assert isinstance(state, CuratorState)
    assert state.run_count == 0
