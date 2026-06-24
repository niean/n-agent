from datetime import datetime, timezone

import pytest

from app.application.schedule_run_service import ScheduleRunService
from app.application.scheduled_agent_executor import ScheduledAgentResult
from app.domain.schedule import (
    DeliveryResult,
    DeliveryTarget,
    ScheduledTask,
    ScheduledTaskClaim,
    ScheduledTaskExecutionStatus,
    ScheduleExpression,
    ScheduleTimezone,
)


def _task():
    return ScheduledTask(
        id="task-1",
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("* * * * *"),
        timezone=ScheduleTimezone("UTC"),
        session_id="session-1",
        delivery_target=DeliveryTarget.dashboard(),
        next_run_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
    )


def _claim(skipped=False):
    now = datetime(2026, 6, 16, tzinfo=timezone.utc)
    return ScheduledTaskClaim(_task(), "claim-1", "owner-1", now, now, now, "due", skipped)


class FakeRegistry:
    def __init__(self, claim=None, stale=False):
        self.claim = claim
        self.started = []
        self.completed = []
        self.delivered = []
        self.stale = stale
        self.dashboard_unread = False

    async def claim_task_for_run_now(self, task_id, now, lease_seconds):
        return self.claim

    async def claim_due_tasks(self, now, limit, lease_seconds):
        return [self.claim] if self.claim else []

    async def record_execution_started(self, execution):
        self.started.append(execution)
        return execution

    async def record_execution_completed(self, execution):
        self.completed.append(execution)
        return not self.stale

    async def record_delivery_result(self, execution):
        self.delivered.append(execution)
        return not self.stale

    async def mark_dashboard_unread(self, task_id, claim_id, lease_owner):
        self.dashboard_unread = True
        return True


class FakeExecutor:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def run(self, task):
        self.calls.append(task)
        return self.result


class FakeDelivery:
    def __init__(self):
        self.contents = []

    async def deliver(self, target, content):
        self.contents.append(content)
        return DeliveryResult("success")


@pytest.mark.asyncio
async def test_run_due_claims_recovers_missing_origin_sessions_before_claiming():
    registry = FakeRegistry(_claim())
    recovered = []

    async def recover():
        recovered.append(True)

    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        FakeDelivery(),
        recover_missing_origin_sessions=recover,
    )

    await service.run_due_claims()

    assert recovered == [True]


@pytest.mark.asyncio
async def test_run_now_claims_and_runs_shared_path():
    registry = FakeRegistry(_claim())
    delivery = FakeDelivery()
    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
    )

    result = await service.run_now("task-1")

    assert result["status"] == "succeeded"
    assert registry.started
    assert registry.completed[0].status is ScheduledTaskExecutionStatus.SUCCEEDED
    assert delivery.contents == ["done"]
    assert registry.dashboard_unread is True


@pytest.mark.asyncio
async def test_skipped_missed_does_not_execute_or_deliver():
    registry = FakeRegistry(_claim(skipped=True))
    executor = FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done"))
    delivery = FakeDelivery()
    service = ScheduleRunService(registry, executor, delivery)

    await service.run_due_claims()

    assert executor.calls == []
    assert delivery.contents == []
    assert registry.completed[0].status is ScheduledTaskExecutionStatus.SKIPPED_MISSED


@pytest.mark.asyncio
async def test_blocked_execution_delivers_safety_summary():
    registry = FakeRegistry(_claim())
    delivery = FakeDelivery()
    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.BLOCKED, error="blocked prompt")),
        delivery,
    )

    await service.run_due_claims()

    assert registry.completed[0].status is ScheduledTaskExecutionStatus.BLOCKED
    assert "blocked prompt" in delivery.contents[0]


@pytest.mark.asyncio
async def test_stale_completion_does_not_deliver():
    registry = FakeRegistry(_claim(), stale=True)
    delivery = FakeDelivery()
    service = ScheduleRunService(
        registry,
        FakeExecutor(ScheduledAgentResult(status=ScheduledTaskExecutionStatus.SUCCEEDED, output="done")),
        delivery,
    )

    await service.run_due_claims()

    assert delivery.contents == []
