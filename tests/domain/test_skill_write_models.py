from app.domain.skill import (
    Skill, SkillFrontmatter, SkillSource, SkillWriteOrigin, SkillWriteAction,
    SkillManageRequest, SkillUsage, SkillPendingWrite, SkillManageResult,
)


def test_skill_source_enum():
    assert SkillSource.SEED.value == "seed"
    assert SkillSource.AGENT.value == "agent"
    assert SkillSource.USER.value == "user"


def test_skill_write_origin_and_action_enums():
    assert SkillWriteOrigin.FOREGROUND.value == "foreground"
    assert SkillWriteOrigin.BACKGROUND_REVIEW.value == "background_review"
    assert {a.value for a in SkillWriteAction} == {
        "create", "patch", "edit", "delete", "write_file", "remove_file"
    }


def test_skill_manage_request_defaults():
    r = SkillManageRequest(action=SkillWriteAction.CREATE, name="x", origin=SkillWriteOrigin.FOREGROUND)
    assert r.approved_replay is False
    assert r.content == ""


def test_skill_has_source_field():
    from app.domain.skill import SkillReadiness
    fm = SkillFrontmatter(name="x", description="", version="", platforms=[], tags=[],
                          related_skills=[], author="", license="", setup_help=None,
                          required_env_vars=[], raw={})
    s = Skill(id="1", name="x", relative_path="x/SKILL.md", description="",
              platforms=[], frontmatter=fm, enabled=True, readiness=SkillReadiness.AVAILABLE,
              last_scan_status="ok", last_scan_error=None, last_seen_at=None,
              created_at=None, updated_at=None, source=SkillSource.AGENT)
    assert s.source is SkillSource.AGENT


def test_skill_usage_and_pending_write_frozen():
    u = SkillUsage(created_by="foreground", use_count=0, view_count=0, patch_count=0,
                   created_at=None, last_used_at=None, last_viewed=None, last_patched_at=None,
                   state="active", pinned=False, archived_at=None)
    assert u.pinned is False
    p = SkillPendingWrite(pending_id="p1", action=SkillWriteAction.PATCH, skill_name="x",
                          origin=SkillWriteOrigin.BACKGROUND_REVIEW, summary="s", diff="d",
                          payload={}, state="pending", error=None, created_at=None, updated_at=None)
    assert p.state == "pending"


def test_skill_frontmatter_has_metadata_field():
    # 既有必填字段构造，metadata/compatibility/allowed_tools 有默认值
    fm = SkillFrontmatter(name="x", description="", version="", platforms=[], tags=[],
                          related_skills=[], author="", license="", setup_help=None,
                          required_env_vars=[], raw={})
    assert fm.metadata == {}
    assert fm.compatibility == ""
    assert fm.allowed_tools == []

    # 可显式传入新字段
    fm2 = SkillFrontmatter(name="x", description="", version="", platforms=[], tags=[],
                           related_skills=[], author="", license="", setup_help=None,
                           required_env_vars=[], raw={},
                           metadata={"author": "n-agent"},
                           compatibility="linux",
                           allowed_tools=["bash"])
    assert fm2.metadata == {"author": "n-agent"}
    assert fm2.compatibility == "linux"
    assert fm2.allowed_tools == ["bash"]


def test_skill_frontmatter_raw_preserves_allowed_tools_key():
    # raw 保存规范化 frontmatter，允许包含 YAML key `allowed-tools`
    raw = {
        "name": "demo",
        "description": "demo skill",
        "allowed-tools": ["bash", "grep"],
    }
    fm = SkillFrontmatter(name="demo", description="demo skill", version="", platforms=[],
                          tags=[], related_skills=[], author="", license="",
                          setup_help=None, required_env_vars=[], raw=raw)
    assert fm.raw["allowed-tools"] == ["bash", "grep"]
