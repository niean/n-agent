# Evaluation 状态机测试：对照 spec 6.4（job）与 spec 13（delivery）校验
# 合法迁移、非法迁移抛错与 skipped reason 枚举。
from __future__ import annotations

import pytest

from app.domain.evaluation.state import (
    ALLOWED_DELIVERY_TRANSITIONS,
    ALLOWED_JOB_TRANSITIONS,
    EvaluationDeliveryStatus,
    EvaluationJobStatus,
    IllegalEvaluationTransition,
    SkipReason,
    transition_delivery_status,
    transition_job_status,
)


class TestJobStatusTransitions:
    def test_legal_main_path(self):
        assert transition_job_status("queued", "running") == "running"
        assert transition_job_status("running", "succeeded") == "succeeded"
        assert transition_job_status("running", "skipped") == "skipped"
        assert transition_job_status("running", "error") == "error"
        assert transition_job_status("running", "cancelled") == "cancelled"

    def test_queued_can_be_cancelled(self):
        # 排队中的作业允许直接取消（spec 6.4：取消后不得提交 succeeded）
        assert transition_job_status("queued", "cancelled") == "cancelled"

    def test_running_reclaim_keeps_running(self):
        # running 的 lease 过期可重新领取：running -> running（追加 attempt）
        assert transition_job_status("running", "running") == "running"

    @pytest.mark.parametrize(
        "current, target",
        [
            # queued 不能跳到任何终态
            ("queued", "succeeded"),
            ("queued", "skipped"),
            ("queued", "error"),
            # 终态不可再迁移
            ("succeeded", "running"),
            ("succeeded", "error"),
            ("succeeded", "skipped"),
            ("succeeded", "cancelled"),
            ("skipped", "running"),
            ("skipped", "succeeded"),
            ("error", "running"),
            ("error", "succeeded"),
            ("cancelled", "running"),
            ("cancelled", "succeeded"),
            # 终态自迁移同样非法
            ("succeeded", "succeeded"),
            ("error", "error"),
            ("cancelled", "cancelled"),
            ("queued", "queued"),
        ],
    )
    def test_illegal_transitions_raise(self, current: str, target: str):
        with pytest.raises(IllegalEvaluationTransition):
            transition_job_status(current, target)

    def test_unknown_status_raises(self):
        with pytest.raises(IllegalEvaluationTransition):
            transition_job_status("pending", "running")
        with pytest.raises(IllegalEvaluationTransition):
            transition_job_status("queued", "done")

    def test_transition_table_covers_all_statuses(self):
        assert set(ALLOWED_JOB_TRANSITIONS) == set(EvaluationJobStatus)


class TestDeliveryStatusTransitions:
    def test_legal_main_path(self):
        assert transition_delivery_status("pending", "accepted") == "accepted"
        assert transition_delivery_status("accepted", "verified") == "verified"

    def test_retry_branches(self):
        # 429/5xx/网络错误退避等待，再回到 pending 重试
        assert transition_delivery_status("pending", "retry_wait") == "retry_wait"
        assert transition_delivery_status("retry_wait", "pending") == "pending"
        # 超过最大重试时长进入 dead_letter
        assert transition_delivery_status("retry_wait", "dead_letter") == "dead_letter"
        # 管理员显式补发
        assert transition_delivery_status("dead_letter", "pending") == "pending"
        # 已 accepted 但回读验证失败也可重试
        assert transition_delivery_status("accepted", "retry_wait") == "retry_wait"

    def test_unlinked_and_disabled_branches(self):
        # 目标无真实身份时不创建悬空 Score
        assert transition_delivery_status("pending", "unlinked") == "unlinked"
        # 关闭出口停止投递
        assert transition_delivery_status("pending", "disabled") == "disabled"
        assert transition_delivery_status("accepted", "disabled") == "disabled"
        assert transition_delivery_status("retry_wait", "disabled") == "disabled"
        assert transition_delivery_status("dead_letter", "disabled") == "disabled"
        # 删除传播（T18/spec 14）：已发送/退避中投递命中主体 tombstone
        # 改判 unlinked（不再作为正式投影，已发送内容走外部删除任务）
        assert transition_delivery_status("accepted", "unlinked") == "unlinked"
        assert transition_delivery_status("retry_wait", "unlinked") == "unlinked"

    def test_dead_letter_from_pending(self):
        assert transition_delivery_status("pending", "dead_letter") == "dead_letter"

    def test_disabled_recovery_reopens_unverified_delivery(self):
        # 恢复出口后补发未 verified 的结果
        assert transition_delivery_status("disabled", "pending") == "pending"

    @pytest.mark.parametrize(
        "current, target",
        [
            # 不能跳过 accepted 直接 verified
            ("pending", "verified"),
            # verified 终态不可再迁移
            ("verified", "pending"),
            ("verified", "accepted"),
            ("verified", "retry_wait"),
            ("verified", "disabled"),
            # unlinked 终态
            ("unlinked", "accepted"),
            ("unlinked", "pending"),
            # dead_letter 不直接 verified
            ("dead_letter", "verified"),
            ("dead_letter", "accepted"),
            # 自迁移非法
            ("pending", "pending"),
            ("accepted", "accepted"),
            ("retry_wait", "retry_wait"),
            ("dead_letter", "dead_letter"),
            ("unlinked", "unlinked"),
            ("disabled", "disabled"),
        ],
    )
    def test_illegal_transitions_raise(self, current: str, target: str):
        with pytest.raises(IllegalEvaluationTransition):
            transition_delivery_status(current, target)

    def test_unknown_status_raises(self):
        with pytest.raises(IllegalEvaluationTransition):
            transition_delivery_status("queued", "accepted")
        with pytest.raises(IllegalEvaluationTransition):
            transition_delivery_status("pending", "sent")

    def test_transition_table_covers_all_statuses(self):
        assert set(ALLOWED_DELIVERY_TRANSITIONS) == set(EvaluationDeliveryStatus)


class TestSkipReason:
    def test_exact_reason_set(self):
        # spec 6.4：skipped 必须携带明确 reason，缺证据不是数值 0；
        # no_answer 来自 spec 8 注释（无相关项查询的 Recall/MRR/NDCG skip）
        expected = {
            "missing_reference",
            "not_applicable",
            "no_answer",
            "evidence_missing",
            "redacted_evidence",
            "evidence_oversized",
            "budget_exhausted",
            "consistency",
        }
        assert {r.value for r in SkipReason} == expected
        assert len(SkipReason) == 8
