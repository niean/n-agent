from datetime import datetime, timedelta, timezone

import pytest

from app.domain.schedule import (
    DeliveryTarget,
    ScheduledTask,
    ScheduledTaskExecution,
    ScheduledTaskExecutionStatus,
    ScheduledTaskStatus,
    ScheduleExpression,
    ScheduleTimezone,
)
from app.infrastructure.schedule.croniter_calculator import CroniterScheduleCalculator
from app.infrastructure.registry.sqlite_schedule_registry import SQLiteScheduledTaskRegistry


def _task(task_id="task-1", next_run_at=None):
    return ScheduledTask(
        id=task_id,
        name="Daily",
        prompt="summarize",
        schedule=ScheduleExpression("*/5 * * * *"),
        timezone=ScheduleTimezone("UTC"),
        session_id="session-1",
        delivery_target=DeliveryTarget.dashboard(),
        next_run_at=next_run_at or datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def registry(tmp_path):
    return SQLiteScheduledTaskRegistry(tmp_path / "sessions.db", CroniterScheduleCalculator(), missed_grace_seconds=300)


@pytest.mark.asyncio
async def test_registry_initializes_schema_and_crud(registry):
    created = await registry.create(_task())
    listed = await registry.list()
    fetched = await registry.get("task-1")

    assert created.id == "task-1"
    assert [task.id for task in listed] == ["task-1"]
    assert fetched is not None
    assert fetched.schedule.value == "*/5 * * * *"

    paused = await registry.update_status("task-1", ScheduledTaskStatus.PAUSED, enabled=False)
    assert paused.enabled is False
    assert paused.status is ScheduledTaskStatus.PAUSED

    assert await registry.delete("task-1") is True
    assert await registry.get("task-1") is None


@pytest.mark.asyncio
async def test_claim_due_tasks_writes_claim_and_advances_next_run(registry):
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now))

    claims = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    second = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    claimed = await registry.get("task-1")

    assert len(claims) == 1
    assert second == []
    assert claims[0].claim_id
    assert claims[0].lease_owner
    assert claims[0].next_run_at > now
    assert claimed is not None
    assert claimed.claim_id == claims[0].claim_id
    assert claimed.lease_owner == claims[0].lease_owner


@pytest.mark.asyncio
async def test_expired_lease_can_be_claimed_with_new_claim(registry):
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now))
    first = (await registry.claim_due_tasks(now, limit=5, lease_seconds=1))[0]

    later = now + timedelta(seconds=2)
    claimed_task = await registry.get("task-1")
    assert claimed_task is not None
    await registry.update(
        ScheduledTask(
            **{**claimed_task.__dict__, "next_run_at": later, "lease_until": now - timedelta(seconds=1)}
        )
    )
    second = (await registry.claim_due_tasks(later, limit=5, lease_seconds=1))[0]

    assert second.claim_id != first.claim_id
    assert second.lease_owner != first.lease_owner


@pytest.mark.asyncio
async def test_run_now_and_due_claim_share_lease(registry):
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now + timedelta(hours=1)))

    run_now = await registry.claim_task_for_run_now("task-1", now, lease_seconds=7200)
    due = await registry.claim_due_tasks(now + timedelta(hours=1), limit=5, lease_seconds=900)

    assert run_now is not None
    assert due == []


@pytest.mark.asyncio
async def test_skipped_missed_claim_fast_forwards_without_executable_work(registry):
    now = datetime(2026, 6, 16, 1, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now - timedelta(hours=1)))

    claims = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    task = await registry.get("task-1")

    assert len(claims) == 1
    assert claims[0].skipped_missed is True
    assert task is not None
    assert task.next_run_at > now


@pytest.mark.asyncio
async def test_completion_and_delivery_require_current_claim(registry):
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now))
    claim = (await registry.claim_due_tasks(now, limit=5, lease_seconds=900))[0]
    execution = ScheduledTaskExecution(
        id="execution-1",
        task_id="task-1",
        session_id="session-1",
        claim_id=claim.claim_id,
        lease_owner=claim.lease_owner,
        status=ScheduledTaskExecutionStatus.RUNNING,
        claimed_next_run_at=claim.claimed_next_run_at,
        started_at=now,
    )
    await registry.record_execution_started(execution)

    stale = ScheduledTaskExecution(**{**execution.__dict__, "claim_id": "old", "status": ScheduledTaskExecutionStatus.SUCCEEDED})
    completed = ScheduledTaskExecution(
        **{**execution.__dict__, "status": ScheduledTaskExecutionStatus.SUCCEEDED, "completed_at": now, "output": "done"}
    )

    assert await registry.record_execution_completed(stale) is False
    assert await registry.record_delivery_result(stale) is False
    assert await registry.record_execution_completed(completed) is True
    delivered = ScheduledTaskExecution(**{**completed.__dict__, "delivery_status": "success"})
    assert await registry.record_delivery_result(delivered) is True


@pytest.mark.asyncio
async def test_mark_session_missing_pauses_bound_tasks(registry):
    await registry.create(_task())

    count = await registry.mark_session_missing("session-1")
    task = await registry.get("task-1")

    assert count == 1
    assert task is not None
    assert task.enabled is False
    assert task.status is ScheduledTaskStatus.SESSION_MISSING


@pytest.mark.asyncio
async def test_list_executions_returns_recent_history(registry):
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task())
    for index in range(3):
        await registry.record_execution_started(
            ScheduledTaskExecution(
                id=f"execution-{index}",
                task_id="task-1",
                session_id="session-1",
                claim_id=f"claim-{index}",
                lease_owner=f"owner-{index}",
                status=ScheduledTaskExecutionStatus.SUCCEEDED,
                started_at=now + timedelta(minutes=index),
                completed_at=now + timedelta(minutes=index, seconds=5),
                output=f"output-{index}",
                created_at=now + timedelta(minutes=index),
            )
        )

    executions = await registry.list_executions("task-1", 2)

    assert [execution.id for execution in executions] == ["execution-2", "execution-1"]
    assert executions[0].output == "output-2"
