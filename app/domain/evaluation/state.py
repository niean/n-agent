# app/domain/evaluation/state.py
"""评估作业与投递的状态机（spec 6.4、13）。

纯函数 + 迁移表，零 IO。非法迁移抛 IllegalEvaluationTransition；
合法迁移返回目标状态。终态（succeeded/skipped/error/cancelled、
verified/unlinked）不可再迁移：succeeded 的低分或 false 不是 error，
skipped 必须携带 SkipReason 明确原因（缺证据不是数值 0）。
"""
from __future__ import annotations

from enum import Enum


class EvaluationJobStatus(str, Enum):
    """评估作业状态（spec 6.4：queued -> running -> 终态）。

    继承 str：枚举成员可与状态字符串直接比较，便于账本持久化。"""

    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    skipped = "skipped"
    error = "error"
    cancelled = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_JOB_STATUSES


class EvaluationDeliveryStatus(str, Enum):
    """结果投递状态（spec 13：pending -> accepted -> verified 及分支）。"""

    pending = "pending"
    accepted = "accepted"
    verified = "verified"
    retry_wait = "retry_wait"
    dead_letter = "dead_letter"
    unlinked = "unlinked"
    disabled = "disabled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_DELIVERY_STATUSES


class SkipReason(str, Enum):
    """作业 skipped 的明确原因（spec 6.4）。缺证据不是数值 0；
    reference-dependent evaluator 在没有参考答案时不得运行。

    no_answer（spec 8 注释）：query 在 qrels 中没有相关项，Recall / MRR /
    NDCG 对其 skipped，同时必须有 no_answer 的任务判定，避免把"不应命中"
    排除后掩盖误答。
    """

    missing_reference = "missing_reference"
    not_applicable = "not_applicable"
    no_answer = "no_answer"
    evidence_missing = "evidence_missing"
    redacted_evidence = "redacted_evidence"
    evidence_oversized = "evidence_oversized"
    budget_exhausted = "budget_exhausted"
    consistency = "consistency"


class IllegalEvaluationTransition(ValueError):
    """评估状态机非法迁移。"""


# 作业状态迁移表：
# - queued -> running（领取）；queued -> cancelled（排队中取消）
# - running -> 终态四类；running -> running（lease 过期重新领取，追加 attempt）
# - 终态不可再迁移；重评/人工修订必须使用新 job
_ALLOWED_JOB_TRANSITIONS: dict[EvaluationJobStatus, frozenset[EvaluationJobStatus]] = {
    EvaluationJobStatus.queued: frozenset(
        {EvaluationJobStatus.running, EvaluationJobStatus.cancelled}
    ),
    EvaluationJobStatus.running: frozenset(
        {
            EvaluationJobStatus.succeeded,
            EvaluationJobStatus.skipped,
            EvaluationJobStatus.error,
            EvaluationJobStatus.cancelled,
            EvaluationJobStatus.running,
        }
    ),
    EvaluationJobStatus.succeeded: frozenset(),
    EvaluationJobStatus.skipped: frozenset(),
    EvaluationJobStatus.error: frozenset(),
    EvaluationJobStatus.cancelled: frozenset(),
}

_TERMINAL_JOB_STATUSES = frozenset(
    {
        EvaluationJobStatus.succeeded,
        EvaluationJobStatus.skipped,
        EvaluationJobStatus.error,
        EvaluationJobStatus.cancelled,
    }
)

# 投递状态迁移表（spec 13）：
# - pending -> accepted（2xx）；verified 需回读完全匹配，不能跳过 accepted
# - 超时/429/5xx/网络错误 -> retry_wait（退避）-> pending 重试；超过最长
#   重试时长 -> dead_letter（管理员可显式补发回 pending）
# - 目标无真实身份 -> unlinked，不创建悬空 Score（投递建立时决定）
# - 删除传播（T18/spec 14）：已发送（accepted）或退避中（retry_wait）的
#   投递命中主体 tombstone 时改判 unlinked（不作为正式投影结果；已发送
#   内容按预登记 external_id 走外部删除任务），不再重发
# - 关闭出口 -> disabled，保留状态；恢复后仅未 verified 的投递回 pending
_ALLOWED_DELIVERY_TRANSITIONS: dict[
    EvaluationDeliveryStatus, frozenset[EvaluationDeliveryStatus]
] = {
    EvaluationDeliveryStatus.pending: frozenset(
        {
            EvaluationDeliveryStatus.accepted,
            EvaluationDeliveryStatus.retry_wait,
            EvaluationDeliveryStatus.dead_letter,
            EvaluationDeliveryStatus.unlinked,
            EvaluationDeliveryStatus.disabled,
        }
    ),
    EvaluationDeliveryStatus.accepted: frozenset(
        {
            EvaluationDeliveryStatus.verified,
            EvaluationDeliveryStatus.retry_wait,
            EvaluationDeliveryStatus.dead_letter,
            EvaluationDeliveryStatus.disabled,
            EvaluationDeliveryStatus.unlinked,
        }
    ),
    EvaluationDeliveryStatus.retry_wait: frozenset(
        {
            EvaluationDeliveryStatus.pending,
            EvaluationDeliveryStatus.dead_letter,
            EvaluationDeliveryStatus.disabled,
            EvaluationDeliveryStatus.unlinked,
        }
    ),
    EvaluationDeliveryStatus.dead_letter: frozenset(
        {EvaluationDeliveryStatus.pending, EvaluationDeliveryStatus.disabled}
    ),
    EvaluationDeliveryStatus.disabled: frozenset(
        {EvaluationDeliveryStatus.pending}
    ),
    EvaluationDeliveryStatus.verified: frozenset(),
    EvaluationDeliveryStatus.unlinked: frozenset(),
}

_TERMINAL_DELIVERY_STATUSES = frozenset(
    {
        EvaluationDeliveryStatus.verified,
        EvaluationDeliveryStatus.unlinked,
    }
)

# T18/M3：删除传播 sweep 关闭出口的归因标记（disable 落 delivery 行的
# last_error_type；worker 启动期自愈按此精确恢复，管理员显式 disable
# 无此标记不受自愈影响）。常量在 domain 层定义：infrastructure worker 与
# application 删除传播服务共用同一权威值（依赖方向单向可用）。
SWEEP_DISABLE_REASON = "deletion_sweep"

# 允许以原始状态名做迁移查询的公开只读视图
ALLOWED_JOB_TRANSITIONS = {
    status: frozenset(target.value for target in targets)
    for status, targets in _ALLOWED_JOB_TRANSITIONS.items()
}
ALLOWED_DELIVERY_TRANSITIONS = {
    status: frozenset(target.value for target in targets)
    for status, targets in _ALLOWED_DELIVERY_TRANSITIONS.items()
}


def _coerce_job_status(value: EvaluationJobStatus | str) -> EvaluationJobStatus:
    if isinstance(value, EvaluationJobStatus):
        return value
    try:
        return EvaluationJobStatus(value)
    except ValueError:
        raise IllegalEvaluationTransition(f"未知作业状态: {value!r}") from None


def _coerce_delivery_status(
    value: EvaluationDeliveryStatus | str,
) -> EvaluationDeliveryStatus:
    if isinstance(value, EvaluationDeliveryStatus):
        return value
    try:
        return EvaluationDeliveryStatus(value)
    except ValueError:
        raise IllegalEvaluationTransition(f"未知投递状态: {value!r}") from None


def transition_job_status(
    current: EvaluationJobStatus | str,
    target: EvaluationJobStatus | str,
) -> EvaluationJobStatus:
    """校验并执行作业状态迁移，非法迁移抛 IllegalEvaluationTransition。"""
    current_status = _coerce_job_status(current)
    target_status = _coerce_job_status(target)
    if target_status not in _ALLOWED_JOB_TRANSITIONS[current_status]:
        raise IllegalEvaluationTransition(
            f"非法作业状态迁移: {current_status.value} -> {target_status.value}"
        )
    return target_status


def transition_delivery_status(
    current: EvaluationDeliveryStatus | str,
    target: EvaluationDeliveryStatus | str,
) -> EvaluationDeliveryStatus:
    """校验并执行投递状态迁移，非法迁移抛 IllegalEvaluationTransition。"""
    current_status = _coerce_delivery_status(current)
    target_status = _coerce_delivery_status(target)
    if target_status not in _ALLOWED_DELIVERY_TRANSITIONS[current_status]:
        raise IllegalEvaluationTransition(
            f"非法投递状态迁移: {current_status.value} -> {target_status.value}"
        )
    return target_status
