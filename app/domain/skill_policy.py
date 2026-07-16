from __future__ import annotations

from dataclasses import dataclass

from app.domain.policy import Policy, PolicyOutcome
from app.domain.skill import (
    SkillSource,
    SkillWriteAction,
    SkillWriteOrigin,
)


@dataclass(frozen=True)
class SkillPolicyRequest:
    target_source: SkillSource | None
    action: SkillWriteAction
    origin: SkillWriteOrigin
    pinned: bool
    name_exists: bool
    write_approval_enabled: bool
    approved_replay: bool
    exact_target_loaded: bool


_WRITE_ACTIONS = {
    SkillWriteAction.PATCH,
    SkillWriteAction.EDIT,
    SkillWriteAction.DELETE,
    SkillWriteAction.WRITE_FILE,
    SkillWriteAction.REMOVE_FILE,
}


class SkillPolicy(Policy):
    """Skill 写入治理。优先级 deny > require_approval > allow。"""

    def evaluate(
        self,
        request: SkillPolicyRequest,
        context: None = None,
    ) -> PolicyOutcome:
        r = request
        # deny 规则（全部先评估）
        if r.action == SkillWriteAction.CREATE and r.name_exists:
            return PolicyOutcome.DENY
        if r.action in _WRITE_ACTIONS and not r.name_exists:
            return PolicyOutcome.DENY
        if r.origin == SkillWriteOrigin.BACKGROUND_REVIEW:
            if r.target_source != SkillSource.AGENT:
                return PolicyOutcome.DENY
            if r.pinned:
                return PolicyOutcome.DENY
            if r.action in _WRITE_ACTIONS and not r.exact_target_loaded:
                return PolicyOutcome.DENY
        if (
            r.target_source == SkillSource.SEED
            and r.origin == SkillWriteOrigin.FOREGROUND
            and r.action in {SkillWriteAction.DELETE, SkillWriteAction.REMOVE_FILE}
        ):
            return PolicyOutcome.DENY
        if r.pinned and r.action == SkillWriteAction.DELETE:
            return PolicyOutcome.DENY
        # require_approval
        if (
            r.write_approval_enabled
            and r.action in _WRITE_ACTIONS | {SkillWriteAction.CREATE}
            and not r.approved_replay
        ):
            return PolicyOutcome.REQUIRE_APPROVAL
        return PolicyOutcome.ALLOW
