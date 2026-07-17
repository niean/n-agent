from __future__ import annotations

from dataclasses import dataclass

from app.domain.policy import Policy, PolicyOutcome
from app.domain.skill import SkillSource


@dataclass(frozen=True)
class CuratorPolicyRequest:
    """Curator 自动迁移与 archive 决策请求。

    source 为目标 skill 的 SkillSource；state 为当前生命周期状态
    （active/stale/archived）；action 为本次操作意图（transition/archive/restore）。
    is_protected_seed 标记是否为承载 UX 的出厂 seed（由 Service 从 seeds 目录名装载）。
    """

    name: str
    source: SkillSource | None
    state: str
    pinned: bool
    is_protected_seed: bool
    prune_seeds: bool
    action: str


class CuratorPolicy(Policy):
    """Curator 自动迁移与 archive 决策治理。

    独立领域 Policy（第 13 个，现有 12 个含 skill_policy）。优先级 deny > allow，
    纯函数无副作用。deny 原因记录在 Service 层日志，不编码进返回值（对齐
    skill_policy.py 返回裸 PolicyOutcome 枚举成员的契约）。

    规则:
      - protected seed -> deny（承载 UX 的出厂 seed 永不迁移/归档）
      - pinned -> deny（pinned 跳过所有自动迁移）
      - source=seed 且未开 prune_seeds -> deny（不维护出厂模板，除非显式开启）
      - source=user -> deny（用户创建的 Skill 不做自动迁移，归属治理留给 skill CLI/Dashboard）
      - state=archived 且 action != restore -> deny（已归档不再迁移，除非 restore）
      - 其余 -> allow
    """

    def evaluate(
        self,
        request: CuratorPolicyRequest,
        context: None = None,
    ) -> PolicyOutcome:
        if request.is_protected_seed:
            return PolicyOutcome.DENY
        if request.pinned:
            return PolicyOutcome.DENY
        if request.source == SkillSource.SEED and not request.prune_seeds:
            return PolicyOutcome.DENY
        if request.source == SkillSource.USER:
            return PolicyOutcome.DENY
        if request.state == "archived" and request.action != "restore":
            return PolicyOutcome.DENY
        return PolicyOutcome.ALLOW
