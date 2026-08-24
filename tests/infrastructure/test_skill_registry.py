import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.skill import (
    Skill,
    SkillFrontmatter,
    SkillNotFoundError,
    SkillReadiness,
    SkillSource,
)
from app.infrastructure.registry.sqlite_skill_registry import (
    SQLiteSkillRegistry,
    _initialize_skill_schema,
)


def _seed_skill(name: str, **overrides) -> Skill:
    fm = SkillFrontmatter(
        name=name, description="d", version="", platforms=["linux"],
        tags=[], related_skills=[], author="", license="",
        setup_help=None, required_env_vars=[], raw={"name": name},
    )
    now = datetime.now(timezone.utc)
    return Skill(
        id=overrides.get("id", f"id-{name}"),
        name=name, relative_path=overrides.get("relative_path", f"{name}/SKILL.md"),
        description=overrides.get("description", "d"),
        platforms=overrides.get("platforms", ["linux"]),
        frontmatter=fm,
        enabled=overrides.get("enabled", True),
        readiness=overrides.get("readiness", SkillReadiness.AVAILABLE),
        last_scan_status=overrides.get("last_scan_status", "ok"),
        last_scan_error=overrides.get("last_scan_error", None),
        last_seen_at=now, created_at=now, updated_at=now,
        source=overrides.get("source", SkillSource.USER),
        chat_selectable=overrides.get("chat_selectable", True),
    )


def test_chat_selectable_column_in_default_schema(tmp_path):
    """New installations create the column with NOT NULL DEFAULT 1."""
    reg = SQLiteSkillRegistry(tmp_path / "fresh.db")
    with sqlite3.connect(reg.path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
    assert "chat_selectable" in columns


def test_chat_selectable_migrates_legacy_db_with_default_true(tmp_path):
    """Legacy DB without the column gets it via PRAGMA-idempotent migration, all rows default 1."""
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE skills (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            description TEXT,
            platforms_json TEXT NOT NULL DEFAULT '[]',
            frontmatter_json TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            readiness TEXT NOT NULL,
            last_scan_status TEXT,
            last_scan_error TEXT,
            last_seen_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'user'
        );
        """
    )
    conn.execute(
        "INSERT INTO skills(id, name, relative_path, description, frontmatter_json, readiness, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "id-legacy", "legacy", "legacy/SKILL.md", "d", "{}",
            "available", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    reg = SQLiteSkillRegistry(legacy)
    skill = asyncio.run(reg.get_skill("legacy"))
    assert skill is not None
    assert skill.chat_selectable is True

    with sqlite3.connect(legacy) as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(skills)").fetchall()}
        values = [row[0] for row in check.execute("SELECT chat_selectable FROM skills")]
    assert "chat_selectable" in columns
    assert values and all(v == 1 for v in values)


def test_set_chat_selectable_updates_value(tmp_path):
    reg = SQLiteSkillRegistry(tmp_path / "skills.db")
    asyncio.run(reg.upsert_skill(_seed_skill("alpha")))
    updated = asyncio.run(reg.set_chat_selectable("alpha", False))
    assert updated.chat_selectable is False
    reloaded = asyncio.run(reg.get_skill("alpha"))
    assert reloaded.chat_selectable is False
    updated2 = asyncio.run(reg.set_chat_selectable("alpha", True))
    assert updated2.chat_selectable is True


def test_set_chat_selectable_raises_when_missing(tmp_path):
    reg = SQLiteSkillRegistry(tmp_path / "skills.db")
    with pytest.raises(SkillNotFoundError):
        asyncio.run(reg.set_chat_selectable("ghost", False))


def test_replace_all_skills_preserves_chat_selectable(tmp_path):
    reg = SQLiteSkillRegistry(tmp_path / "skills.db")
    asyncio.run(reg.upsert_skill(_seed_skill("alpha")))
    asyncio.run(reg.set_chat_selectable("alpha", False))
    # Re-scan with default chat_selectable=True should not clobber the stored False.
    asyncio.run(reg.replace_all_skills([_seed_skill("alpha")]))
    reloaded = asyncio.run(reg.get_skill("alpha"))
    assert reloaded.chat_selectable is False


def test_replace_all_skills_preserves_chat_selectable_across_enabled_false(tmp_path):
    """Disabling a Skill must not reset its chat_selectable value."""
    reg = SQLiteSkillRegistry(tmp_path / "skills.db")
    asyncio.run(reg.upsert_skill(_seed_skill("alpha")))
    asyncio.run(reg.set_chat_selectable("alpha", False))
    asyncio.run(reg.set_enabled("alpha", False))
    asyncio.run(reg.replace_all_skills([_seed_skill("alpha")]))
    reloaded = asyncio.run(reg.get_skill("alpha"))
    assert reloaded.enabled is False
    assert reloaded.chat_selectable is False


def test_upsert_skill_round_trips_chat_selectable(tmp_path):
    reg = SQLiteSkillRegistry(tmp_path / "skills.db")
    asyncio.run(reg.upsert_skill(_seed_skill("alpha", chat_selectable=False)))
    loaded = asyncio.run(reg.get_skill("alpha"))
    assert loaded.chat_selectable is False
