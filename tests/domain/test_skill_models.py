import pytest

from app.domain.skill import (
    Skill,
    SkillFrontmatter,
    SkillReadiness,
    SkillRegistry,
    SkillNotFoundError,
    SkillValidationError,
    SkillScanError,
)


def test_skill_readiness_values():
    assert SkillReadiness.AVAILABLE.value == "available"
    assert SkillReadiness.UNSUPPORTED.value == "unsupported"
    assert SkillReadiness.SETUP_NEEDED.value == "setup_needed"
    assert SkillReadiness.SCAN_ERROR.value == "scan_error"


def test_skill_frontmatter_is_frozen():
    fm = SkillFrontmatter(
        name="demo",
        description="a demo skill",
        version="0.1.0",
        platforms=["linux"],
        tags=["x"],
        related_skills=[],
        author="",
        license="",
        setup_help=None,
        required_env_vars=[],
        raw={"name": "demo"},
    )
    with pytest.raises(Exception):
        fm.name = "other"  # type: ignore[misc]


def test_skill_entity_holds_metadata():
    fm = SkillFrontmatter(
        name="demo", description="", version="", platforms=["linux"], tags=[],
        related_skills=[], author="", license="", setup_help=None,
        required_env_vars=[], raw={},
    )
    skill = Skill(
        id="id-1", name="demo", relative_path="demo/SKILL.md",
        description="", platforms=["linux"], frontmatter=fm,
        enabled=True, readiness=SkillReadiness.AVAILABLE,
        last_scan_status="ok", last_scan_error=None,
        last_seen_at=None, created_at=None, updated_at=None,
    )
    assert skill.name == "demo"
    assert skill.readiness is SkillReadiness.AVAILABLE


def test_registry_is_protocol():
    assert hasattr(SkillRegistry, "list_skills")
    assert hasattr(SkillRegistry, "replace_all_skills")


def test_exceptions_are_hierarchy():
    assert issubclass(SkillNotFoundError, Exception)
    assert issubclass(SkillValidationError, Exception)
    assert issubclass(SkillScanError, Exception)
