from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from app.domain.skill import SkillUsage
from app.infrastructure.skill.skill_usage_store import SkillUsageStore


def _setup_db(db_path: str, skills: list[tuple[str, str]]) -> SkillUsageStore:
    """创建含 (name, source) 行的 skills 表，再构造 SkillUsageStore（建 skill_usage 表）。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE skills (name TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'user')"
    )
    conn.executemany("INSERT INTO skills(name, source) VALUES (?, ?)", skills)
    conn.commit()
    conn.close()
    return SkillUsageStore(db_path)


def test_list_curator_managed_only_agent(tmp_path):
    store = _setup_db(
        str(tmp_path / "test.db"),
        [("agent-skill", "agent"), ("seed-skill", "seed"), ("user-skill", "user")],
    )
    reports = asyncio.run(
        store.list_curator_managed(prune_seeds=False, protected_names=set())
    )
    names = [r.name for r in reports]
    assert "agent-skill" in names
    assert "seed-skill" not in names
    assert "user-skill" not in names


def test_list_curator_managed_prune_seeds(tmp_path):
    store = _setup_db(
        str(tmp_path / "test.db"),
        [("agent-skill", "agent"), ("seed-skill", "seed")],
    )
    reports = asyncio.run(
        store.list_curator_managed(prune_seeds=True, protected_names=set())
    )
    names = [r.name for r in reports]
    assert "agent-skill" in names
    assert "seed-skill" in names


def test_list_curator_managed_excludes_protected(tmp_path):
    store = _setup_db(
        str(tmp_path / "test.db"),
        [("agent-skill", "agent"), ("n-agent", "seed")],
    )
    reports = asyncio.run(
        store.list_curator_managed(prune_seeds=True, protected_names={"n-agent"})
    )
    names = [r.name for r in reports]
    assert "agent-skill" in names
    assert "n-agent" not in names


def test_list_curator_managed_persisted_and_activity(tmp_path):
    store = _setup_db(str(tmp_path / "test.db"), [("agent-skill", "agent")])
    now = datetime.now(timezone.utc)
    asyncio.run(
        store.upsert(
            "agent-skill",
            SkillUsage(
                created_by="agent",
                use_count=2,
                view_count=3,
                patch_count=1,
                created_at=now,
                last_used_at=now,
                last_viewed=now,
                last_patched_at=now,
                state="active",
                pinned=False,
                archived_at=None,
            ),
        )
    )
    reports = asyncio.run(
        store.list_curator_managed(prune_seeds=False, protected_names=set())
    )
    r = [x for x in reports if x.name == "agent-skill"][0]
    assert r._persisted is True
    assert r.use_count == 2
    assert r.view_count == 3
    assert r.patch_count == 1
    assert r.activity_count == 6
    assert r.last_activity_at is not None
    assert r.source == "agent"


def test_list_curator_managed_not_persisted(tmp_path):
    store = _setup_db(str(tmp_path / "test.db"), [("agent-skill", "agent")])
    reports = asyncio.run(
        store.list_curator_managed(prune_seeds=False, protected_names=set())
    )
    r = [x for x in reports if x.name == "agent-skill"][0]
    assert r._persisted is False
    assert r.use_count == 0
    assert r.activity_count == 0
    assert r.last_activity_at is None


def test_seed_record_if_missing(tmp_path):
    store = _setup_db(str(tmp_path / "test.db"), [("agent-skill", "agent")])
    asyncio.run(store.seed_record_if_missing("agent-skill"))
    usage = asyncio.run(store.get("agent-skill"))
    assert usage is not None
    assert usage.created_at is not None
    first_created = usage.created_at
    asyncio.run(store.seed_record_if_missing("agent-skill"))
    usage2 = asyncio.run(store.get("agent-skill"))
    assert usage2.created_at == first_created


def test_archive_skill(tmp_path):
    store = _setup_db(str(tmp_path / "test.db"), [("agent-skill", "agent")])
    asyncio.run(store.archive_skill("agent-skill"))
    usage = asyncio.run(store.get("agent-skill"))
    assert usage is not None
    assert usage.state == "archived"
    assert usage.archived_at is not None


def test_restore_skill_usage(tmp_path):
    store = _setup_db(str(tmp_path / "test.db"), [("agent-skill", "agent")])
    asyncio.run(store.archive_skill("agent-skill"))
    asyncio.run(store.restore_skill("agent-skill"))
    usage = asyncio.run(store.get("agent-skill"))
    assert usage is not None
    assert usage.state == "active"
    assert usage.archived_at is None
