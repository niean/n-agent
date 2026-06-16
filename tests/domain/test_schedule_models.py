from datetime import datetime, timezone
from typing import get_args

from app.domain.schedule import (
    DeliveryTarget,
    DeliveryTargetType,
    ScheduledExecutionPolicy,
    ScheduledExecutionPolicyMode,
    ScheduledTask,
    ScheduledTaskClaim,
    ScheduledTaskExecution,
    ScheduledTaskExecutionStatus,
    ScheduledTaskStatus,
    ScheduleExpression,
    ScheduleTimezone,
)


def test_schedule_expression_preserves_standard_cron():
    expression = ScheduleExpression("0 9 * * *")

    assert expression.value == "0 9 * * *"


def test_schedule_timezone_defaults_to_asia_shanghai():
    timezone_value = ScheduleTimezone()

    assert timezone_value.value == "Asia/Shanghai"


def test_delivery_target_origin_requires_context_payload():
    target = DeliveryTarget.origin({"receive_id": "chat-1", "receive_id_type": "chat_id"})

    assert target.target_type is DeliveryTargetType.ORIGIN
    assert target.context["receive_id"] == "chat-1"


def test_execution_policy_defaults_to_unattended_safe_only():
    policy = ScheduledExecutionPolicy()

    assert policy.mode is ScheduledExecutionPolicyMode.UNATTENDED
    assert policy.tool_exposure_policy == "safe_only"
    assert policy.allow_confirm_tools is False


def test_scheduled_task_defaults_to_enabled_pending():
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    task = ScheduledTask(
        id="task-1",
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("0 9 * * *"),
        timezone=ScheduleTimezone("Asia/Shanghai"),
        session_id="session-1",
        delivery_target=DeliveryTarget.dashboard(),
        next_run_at=now,
    )

    assert task.enabled is True
    assert task.status is ScheduledTaskStatus.ACTIVE
    assert task.execution_policy.mode is ScheduledExecutionPolicyMode.UNATTENDED


def test_claim_carries_owner_and_claim_id():
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    task = ScheduledTask(
        id="task-1",
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("0 9 * * *"),
        timezone=ScheduleTimezone("Asia/Shanghai"),
        session_id="session-1",
        delivery_target=DeliveryTarget.silent(),
        next_run_at=now,
    )
    claim = ScheduledTaskClaim(
        task=task,
        claim_id="claim-1",
        lease_owner="runner-1",
        lease_until=now,
        claimed_next_run_at=now,
        next_run_at=now,
        reason="due",
    )

    assert claim.claim_id == "claim-1"
    assert claim.lease_owner == "runner-1"
    assert claim.skipped_missed is False


def test_execution_supports_blocked_status_and_delivery_error_separation():
    execution = ScheduledTaskExecution(
        id="execution-1",
        task_id="task-1",
        session_id="session-1",
        claim_id="claim-1",
        lease_owner="runner-1",
        status=ScheduledTaskExecutionStatus.BLOCKED,
        error="blocked prompt",
        delivery_status="failed",
        delivery_error="cannot send",
    )

    assert execution.status is ScheduledTaskExecutionStatus.BLOCKED
    assert execution.error == "blocked prompt"
    assert execution.delivery_error == "cannot send"


def test_registry_protocol_is_runtime_checkable():
    from app.domain.schedule import ScheduledTaskRegistry

    assert ScheduledTaskRegistry in get_args(ScheduledTaskRegistry | None)
