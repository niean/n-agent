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
async def test_delete_skill(tmp_path):
    registry = SQLiteSkillRegistry(tmp_path / "s.db")
    await registry.upsert_skill(_skill("a"))
    assert await registry.delete_skill("a") is True
    assert await registry.get_skill("a") is None
    assert await registry.delete_skill("a") is False


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


@pytest.mark.asyncio
async def test_roundtrip_preserves_normalized_frontmatter_fields(tmp_path):
    """Scan -> DB -> read round-trip must preserve all frontmatter fields
    when raw is normalized (legacy sunk to metadata).

    Regression guard: _skill_from_row must use the same metadata-aware
    construction as the file loader, not read legacy fields from top-level.
    """
    from app.domain.skill_format import skill_frontmatter_from_dict

    # Build raw with top-level legacy fields (as a real scan reads from disk),
    # then normalize via skill_frontmatter_from_dict so raw is normalized exactly
    # like a real scan produces before writing to DB.
    raw_input = {
        "name": "roundtrip",
        "description": "roundtrip skill",
        "version": "1.2.3",
        "tags": ["ops", "web"],
        "related_skills": ["skill-x"],
        "author": "tester",
        "setup_help": "pip install x",
        "required_env_vars": ["KEY1", "KEY2"],
        "platforms": ["linux"],
        "license": "MIT",
        "allowed-tools": ["bash", "grep"],
        "compatibility": ">=1.0",
        "metadata": {"custom": "val"},
    }
    platforms = ["linux"]
    fm = skill_frontmatter_from_dict(raw_input, "roundtrip", platforms)
    # Sanity: raw is normalized (legacy sunk to metadata)
    assert "version" not in fm.raw
    assert fm.raw["metadata"]["version"] == "1.2.3"

    skill = Skill(
        id="id-roundtrip", name="roundtrip", relative_path="roundtrip/SKILL.md",
        description="roundtrip skill", platforms=platforms, frontmatter=fm,
        enabled=True, readiness=SkillReadiness.AVAILABLE, last_scan_status="ok",
        last_scan_error=None, last_seen_at=None, created_at=None, updated_at=None,
    )

    registry = SQLiteSkillRegistry(tmp_path / "rt.db")
    await registry.upsert_skill(skill)
    fetched = await registry.get_skill("roundtrip")
    assert fetched is not None

    rtfm = fetched.frontmatter
    # Legacy extension fields (read from metadata after normalization)
    assert rtfm.version == "1.2.3"
    assert rtfm.tags == ["ops", "web"]
    assert rtfm.related_skills == ["skill-x"]
    assert rtfm.author == "tester"
    assert rtfm.setup_help == "pip install x"
    assert rtfm.required_env_vars == ["KEY1", "KEY2"]
    # Whitelist fields
    assert rtfm.allowed_tools == ["bash", "grep"]
    assert rtfm.compatibility == ">=1.0"
    assert rtfm.license == "MIT"
    # Metadata dict (sunk legacy + original custom key)
    assert rtfm.metadata.get("custom") == "val"
    assert rtfm.metadata.get("version") == "1.2.3"
    assert rtfm.metadata.get("tags") == "ops,web"
    # Platforms
    assert fetched.platforms == ["linux"]
    assert rtfm.platforms == ["linux"]
