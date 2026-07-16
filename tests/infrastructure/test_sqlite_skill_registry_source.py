import pytest
from app.infrastructure.registry.sqlite_skill_registry import SQLiteSkillRegistry
from app.domain.skill import Skill, SkillFrontmatter, SkillReadiness, SkillSource


def _skill(name, source):
    fm = SkillFrontmatter(
        name=name, description="", version="", platforms=[], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={},
    )
    return Skill(
        id="1", name=name, relative_path=f"{name}/SKILL.md", description="",
        platforms=[], frontmatter=fm, enabled=True, readiness=SkillReadiness.AVAILABLE,
        last_scan_status="ok", last_scan_error=None, last_seen_at=None,
        created_at=None, updated_at=None, source=source,
    )


@pytest.fixture
def reg(tmp_path):
    return SQLiteSkillRegistry(str(tmp_path / "r.db"))


@pytest.mark.asyncio
async def test_upsert_preserves_source(reg):
    await reg.upsert_skill(_skill("x", SkillSource.AGENT))
    s = await reg.get_skill("x")
    assert s.source == SkillSource.AGENT


@pytest.mark.asyncio
async def test_replace_all_preserves_existing_source(reg):
    await reg.upsert_skill(_skill("x", SkillSource.AGENT))
    # replace_all 传入 source=USER（扫描默认）应保留既有 AGENT
    await reg.replace_all_skills([_skill("x", SkillSource.USER)])
    s = await reg.get_skill("x")
    assert s.source == SkillSource.AGENT


@pytest.mark.asyncio
async def test_legacy_row_defaults_user(reg, tmp_path):
    await reg.upsert_skill(_skill("y", SkillSource.USER))
    s = await reg.get_skill("y")
    assert s.source == SkillSource.USER
