from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.domain.policy import PolicyOutcome
from app.domain.schedule import (
    DeliveryTarget,
    ScheduledExecutionPolicy,
    ScheduledExecutionPolicyMode,
    ScheduledTask,
    ScheduledTaskClaim,
    ScheduledTaskStatus,
    ScheduleExpression,
    ScheduleTimezone,
)
from app.domain.schedule_policy import (
    DeliveryMode,
    MissedAction,
    RetryPlan,
    RunOverrides,
    ScheduleClaimConstraints,
    SchedulePolicy,
    ScheduleRunDecision,
)

NOW = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)


def _task(
    *,
    status: ScheduledTaskStatus = ScheduledTaskStatus.ACTIVE,
    enabled: bool = True,
    delivery_target: DeliveryTarget | None = None,
    next_run_at: datetime | None = None,
    execution_policy: ScheduledExecutionPolicy | None = None,
) -> ScheduledTask:
    return ScheduledTask(
        id="task-1",
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("*/5 * * * *"),
        timezone=ScheduleTimezone("UTC"),
        session_id="session-1",
        delivery_target=delivery_target or DeliveryTarget.dashboard(),
        next_run_at=next_run_at or NOW,
        enabled=enabled,
        status=status,
        execution_policy=execution_policy or ScheduledExecutionPolicy(),
    )


def _claim(
    task: ScheduledTask | None = None,
    *,
    claimed_next_run_at: datetime | None = None,
) -> ScheduledTaskClaim:
    task = task or _task()
    ct = claimed_next_run_at or task.next_run_at
    return ScheduledTaskClaim(
        task=task,
        claim_id="claim-1",
        lease_owner="owner-1",
        lease_until=NOW + timedelta(seconds=900),
        claimed_next_run_at=ct,
        next_run_at=ct,
        reason="due",
    )


# ---------------------------------------------------------------------------
# Decision table
# ---------------------------------------------------------------------------


class TestSchedulePolicyDecisionTable:
    def test_disabled_task_denies(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        decision = policy.evaluate(_claim(_task(enabled=False)), NOW)
        assert decision.verdict is PolicyOutcome.DENY
        assert decision.reason

    def test_deleted_task_denies(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        decision = policy.evaluate(_claim(_task(status=ScheduledTaskStatus.DELETED)), NOW)
        assert decision.verdict is PolicyOutcome.DENY

    def test_session_missing_task_denies(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        decision = policy.evaluate(_claim(_task(status=ScheduledTaskStatus.SESSION_MISSING)), NOW)
        assert decision.verdict is PolicyOutcome.DENY

    def test_paused_task_denies(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        decision = policy.evaluate(_claim(_task(status=ScheduledTaskStatus.PAUSED)), NOW)
        assert decision.verdict is PolicyOutcome.DENY

    def test_super_missed_grace_skipped_missed(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        due_at = NOW
        now = due_at + timedelta(seconds=301)
        decision = policy.evaluate(_claim(_task(next_run_at=due_at), claimed_next_run_at=due_at), now)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.missed_action is MissedAction.SKIPPED_MISSED
        assert decision.delivery_mode is DeliveryMode.SILENT

    def test_exactly_at_grace_boundary_runs_now(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        due_at = NOW
        now = due_at + timedelta(seconds=300)
        decision = policy.evaluate(_claim(_task(next_run_at=due_at), claimed_next_run_at=due_at), now)
        assert decision.missed_action is MissedAction.RUN_NOW

    def test_within_grace_runs_now(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        due_at = NOW
        now = due_at + timedelta(seconds=100)
        decision = policy.evaluate(_claim(_task(next_run_at=due_at), claimed_next_run_at=due_at), now)
        assert decision.verdict is PolicyOutcome.ALLOW
        assert decision.missed_action is MissedAction.RUN_NOW

    def test_unattended_safe_only_no_confirm_overrides(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        task = _task(
            next_run_at=NOW,
            execution_policy=ScheduledExecutionPolicy(
                mode=ScheduledExecutionPolicyMode.UNATTENDED,
                tool_exposure_policy="safe_only",
                allow_confirm_tools=False,
            ),
        )
        decision = policy.evaluate(_claim(task, claimed_next_run_at=NOW), NOW)
        assert decision.run_overrides.execution_mode == "unattended"
        assert decision.run_overrides.tool_exposure_policy == "safe_only"
        assert decision.run_overrides.allow_confirm_tools is False

    def test_agent_fail_default_no_retry(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        decision = policy.evaluate(_claim(), NOW)
        assert decision.retry_plan is RetryPlan.NONE

    def test_silent_delivery_mode(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        task = _task(next_run_at=NOW, delivery_target=DeliveryTarget.silent())
        decision = policy.evaluate(_claim(task, claimed_next_run_at=NOW), NOW)
        assert decision.delivery_mode is DeliveryMode.SILENT

    def test_dashboard_delivery_mode(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        task = _task(next_run_at=NOW, delivery_target=DeliveryTarget.dashboard())
        decision = policy.evaluate(_claim(task, claimed_next_run_at=NOW), NOW)
        assert decision.delivery_mode is DeliveryMode.DELIVER

    def test_origin_delivery_mode(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        task = _task(
            next_run_at=NOW,
            delivery_target=DeliveryTarget.origin({"platform": "feishu", "receive_id": "oc_1"}),
        )
        decision = policy.evaluate(_claim(task, claimed_next_run_at=NOW), NOW)
        assert decision.delivery_mode is DeliveryMode.DELIVER

    def test_allowed_decision_has_reason(self):
        policy = SchedulePolicy(missed_grace_seconds=300)
        decision = policy.evaluate(_claim(), NOW)
        assert decision.reason


# ---------------------------------------------------------------------------
# ScheduleClaimConstraints (in-transaction specification)
# ---------------------------------------------------------------------------


class TestScheduleClaimConstraints:
    def test_active_enabled_due_task_satisfies(self):
        constraints = ScheduleClaimConstraints.for_due_claim(NOW)
        task = _task(next_run_at=NOW, enabled=True, status=ScheduledTaskStatus.ACTIVE)
        assert constraints.is_satisfied_by(task, NOW)

    def test_disabled_task_not_satisfied(self):
        constraints = ScheduleClaimConstraints.for_due_claim(NOW)
        task = _task(enabled=False)
        assert not constraints.is_satisfied_by(task, NOW)

    def test_deleted_task_not_satisfied(self):
        constraints = ScheduleClaimConstraints.for_due_claim(NOW)
        task = _task(status=ScheduledTaskStatus.DELETED)
        assert not constraints.is_satisfied_by(task, NOW)

    def test_future_task_not_satisfied(self):
        constraints = ScheduleClaimConstraints.for_due_claim(NOW)
        task = _task(next_run_at=NOW + timedelta(hours=1))
        assert not constraints.is_satisfied_by(task, NOW)

    def test_leased_task_not_satisfied(self):
        constraints = ScheduleClaimConstraints.for_due_claim(NOW)
        task = ScheduledTask(
            **{
                **_task(next_run_at=NOW).__dict__,
                "lease_until": NOW + timedelta(seconds=300),
            }
        )
        assert not constraints.is_satisfied_by(task, NOW)

    def test_expired_lease_satisfied(self):
        constraints = ScheduleClaimConstraints.for_due_claim(NOW)
        task = ScheduledTask(
            **{
                **_task(next_run_at=NOW).__dict__,
                "lease_until": NOW - timedelta(seconds=1),
            }
        )
        assert constraints.is_satisfied_by(task, NOW)
