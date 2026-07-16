from app.domain.skill_policy import SkillPolicy, SkillPolicyRequest
from app.domain.skill import SkillSource, SkillWriteOrigin, SkillWriteAction
from app.domain.policy import PolicyOutcome

def _req(**kw):
    base = dict(target_source=SkillSource.AGENT, action=SkillWriteAction.PATCH,
                origin=SkillWriteOrigin.BACKGROUND_REVIEW, pinned=False,
                name_exists=True, write_approval_enabled=False,
                approved_replay=False, exact_target_loaded=True)
    base.update(kw)
    return SkillPolicyRequest(**base)

def test_bg_review_cannot_modify_seed():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.SEED))
    assert out == PolicyOutcome.DENY

def test_bg_review_cannot_modify_user_skill():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.USER))
    assert out == PolicyOutcome.DENY

def test_bg_review_can_modify_agent_skill_when_loaded():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.AGENT, exact_target_loaded=True))
    assert out == PolicyOutcome.ALLOW

def test_bg_review_denied_without_read_before_write():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.AGENT, exact_target_loaded=False))
    assert out == PolicyOutcome.DENY

def test_bg_review_cannot_modify_pinned():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.AGENT, pinned=True))
    assert out == PolicyOutcome.DENY

def test_foreground_can_patch_seed():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.SEED, origin=SkillWriteOrigin.FOREGROUND, action=SkillWriteAction.PATCH))
    assert out == PolicyOutcome.ALLOW

def test_foreground_cannot_delete_seed():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.SEED, origin=SkillWriteOrigin.FOREGROUND, action=SkillWriteAction.DELETE))
    assert out == PolicyOutcome.DENY

def test_any_origin_cannot_delete_pinned():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.USER, origin=SkillWriteOrigin.FOREGROUND, action=SkillWriteAction.DELETE, pinned=True))
    assert out == PolicyOutcome.DENY

def test_create_existing_denied():
    out = SkillPolicy().evaluate(_req(action=SkillWriteAction.CREATE, name_exists=True))
    assert out == PolicyOutcome.DENY

def test_write_approval_stages_when_not_replay():
    out = SkillPolicy().evaluate(_req(origin=SkillWriteOrigin.FOREGROUND, write_approval_enabled=True, approved_replay=False))
    assert out == PolicyOutcome.REQUIRE_APPROVAL

def test_write_approval_replay_skips_stage():
    out = SkillPolicy().evaluate(_req(origin=SkillWriteOrigin.FOREGROUND, write_approval_enabled=True, approved_replay=True))
    assert out == PolicyOutcome.ALLOW

def test_deny_overrides_require_approval():
    out = SkillPolicy().evaluate(_req(target_source=SkillSource.SEED, origin=SkillWriteOrigin.BACKGROUND_REVIEW, write_approval_enabled=True))
    assert out == PolicyOutcome.DENY
