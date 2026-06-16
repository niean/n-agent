import pytest
from datetime import datetime, timezone

from app.domain.skill import Skill, SkillFrontmatter, SkillReadiness
from app.infrastructure.registry.sqlite_skill_registry import SQLiteSkillRegistry


def _skill(name: str, enabled: bool = True, readiness: SkillReadiness = SkillReadiness.AVAILABLE) -> Skill:
    fm = SkillFrontmatter(
        name=name, description="", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={"name": name},
    )
    return Skill(
        id=f"id-{name}", name=name, relative_path=f"{name}/SKILL.md",
        description="d", platforms=["linux"], frontmatter=fm,
        enabled=enabled, readiness=readiness, last_scan_status="ok",
        last_scan_error=None, last_seen_at=None, created_at=None, updated_at=None,
    )


@pytest.mark.asyncio
async def test_upsert_and_get(tmp_path):
    registry = SQLiteSkillRegistry(tmp_path / "s.db")
    saved = await registry.upsert_skill(_skill("a"))
    assert saved.name == "a"
    fetched = await registry.get_skill("a")
    assert fetched is not None and fetched.name == "a"
    assert (await registry.get_skill("missing")) is None


@pytest.mark.asyncio
async def test_set_enabled(tmp_path):
    registry = SQLiteSkillRegistry(tmp_path / "s.db")
    await registry.upsert_skill(_skill("a"))
    updated = await registry.set_enabled("a", False)
    assert updated.enabled is False


@pytest.mark.asyncio
async def test_replace_all_preserves_enabled(tmp_path):
    registry = SQLiteSkillRegistry(tmp_path / "s.db")
    await registry.upsert_skill(_skill("a"))
    await registry.set_enabled("a", False)
    await registry.upsert_skill(_skill("gone"))
    new_a = _skill("a")
    new_b = _skill("b")
    saved = await registry.replace_all_skills([new_a, new_b])
    saved_by_name = {s.name: s for s in saved}
    assert saved_by_name["a"].enabled is False
    assert saved_by_name["b"].enabled is True
    assert (await registry.get_skill("gone")) is None


@pytest.mark.asyncio
async def test_list_skills_include_disabled(tmp_path):
    registry = SQLiteSkillRegistry(tmp_path / "s.db")
    await registry.upsert_skill(_skill("a"))
    await registry.upsert_skill(_skill("b"))
    await registry.set_enabled("b", False)
    enabled_only = await registry.list_skills(include_disabled=False)
    assert {s.name for s in enabled_only} == {"a"}
    all_skills = await registry.list_skills(include_disabled=True)
    assert {s.name for s in all_skills} == {"a", "b"}
