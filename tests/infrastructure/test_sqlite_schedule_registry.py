from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3

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
async def test_registry_persists_execution_policy_allowed_tools(registry):
    from app.domain.schedule import ScheduledExecutionPolicy

    task = ScheduledTask(
        id="task-grant",
        name="photo",
        prompt="拍照上传",
        schedule=ScheduleExpression("0 10,18 * * *"),
        timezone=ScheduleTimezone("UTC"),
        session_id="session-1",
        delivery_target=DeliveryTarget.dashboard(),
        next_run_at=datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc),
        execution_policy=ScheduledExecutionPolicy(allowed_tools=("host_terminal",)),
    )
    await registry.create(task)

    fetched = await registry.get("task-grant")
    assert fetched is not None
    assert fetched.execution_policy.allowed_tools == ("host_terminal",)

    cleared = ScheduledTask(
        **{
            **fetched.__dict__,
            "execution_policy": ScheduledExecutionPolicy(allowed_tools=()),
        }
    )
    await registry.update(cleared)
    refetched = await registry.get("task-grant")
    assert refetched is not None
    assert refetched.execution_policy.allowed_tools == ()


@pytest.mark.asyncio
async def test_recover_stale_executions_marks_expired_running_failed_and_releases_lease(registry):
    task = _task()
    await registry.create(task)
    now = task.next_run_at

    claim = await registry.claim_task_for_run_now(task.id, now, 900)
    assert claim is not None
    execution = ScheduledTaskExecution(
        id="exec-stale",
        task_id=task.id,
        session_id=task.session_id,
        claim_id=claim.claim_id,
        lease_owner=claim.lease_owner,
        claimed_next_run_at=claim.claimed_next_run_at,
        started_at=now,
        status=ScheduledTaskExecutionStatus.RUNNING,
    )
    await registry.record_execution_started(execution)

    # before lease expiry: nothing recovered, execution still RUNNING
    assert await registry.recover_stale_executions(now + timedelta(seconds=600)) == 0
    assert (await registry.list_executions(task.id, 5))[0].status is ScheduledTaskExecutionStatus.RUNNING

    # after lease expiry: execution marked FAILED, lease released
    after = now + timedelta(seconds=901)
    assert await registry.recover_stale_executions(after) == 1
    execs = await registry.list_executions(task.id, 5)
    assert execs[0].status is ScheduledTaskExecutionStatus.FAILED
    assert execs[0].error == "execution_stale_recovered"
    refreshed = await registry.get(task.id)
    assert refreshed.lease_until is None


@pytest.mark.asyncio
async def test_recover_stale_executions_marks_superseded_claim_running_failed(registry):
    task = _task()
    await registry.create(task)
    now = task.next_run_at

    claim1 = await registry.claim_task_for_run_now(task.id, now, 900)
    assert claim1 is not None
    exec1 = ScheduledTaskExecution(
        id="exec-superseded",
        task_id=task.id,
        session_id=task.session_id,
        claim_id=claim1.claim_id,
        lease_owner=claim1.lease_owner,
        claimed_next_run_at=claim1.claimed_next_run_at,
        started_at=now,
        status=ScheduledTaskExecutionStatus.RUNNING,
    )
    await registry.record_execution_started(exec1)

    # lease for claim1 expires -> task is re-claimed (claim2) with a new execution
    after = now + timedelta(seconds=901)
    claim2 = await registry.claim_task_for_run_now(task.id, after, 900)
    assert claim2 is not None
    exec2 = ScheduledTaskExecution(
        id="exec-current",
        task_id=task.id,
        session_id=task.session_id,
        claim_id=claim2.claim_id,
        lease_owner=claim2.lease_owner,
        claimed_next_run_at=claim2.claimed_next_run_at,
        started_at=after,
        status=ScheduledTaskExecutionStatus.RUNNING,
    )
    await registry.record_execution_started(exec2)

    recovered = await registry.recover_stale_executions(after + timedelta(seconds=1))
    # only the superseded execution is recovered; the current one keeps running
    assert recovered == 1
    by_id = {e.id: e for e in await registry.list_executions(task.id, 5)}
    assert by_id["exec-superseded"].status is ScheduledTaskExecutionStatus.FAILED
    assert by_id["exec-current"].status is ScheduledTaskExecutionStatus.RUNNING


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
async def test_run_now_can_reclaim_completed_lease(registry):
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now + timedelta(hours=1)))
    first = await registry.claim_task_for_run_now("task-1", now, lease_seconds=900)
    assert first is not None
    execution = ScheduledTaskExecution(
        id="execution-1",
        task_id="task-1",
        session_id="session-1",
        claim_id=first.claim_id,
        lease_owner=first.lease_owner,
        status=ScheduledTaskExecutionStatus.SUCCEEDED,
        claimed_next_run_at=first.claimed_next_run_at,
        started_at=now,
        completed_at=now,
    )
    await registry.record_execution_started(execution)

    second = await registry.claim_task_for_run_now("task-1", now + timedelta(minutes=1), lease_seconds=900)

    assert second is not None
    assert second.claim_id != first.claim_id


@pytest.mark.asyncio
async def test_run_now_does_not_reclaim_running_lease(registry):
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now + timedelta(hours=1)))
    first = await registry.claim_task_for_run_now("task-1", now, lease_seconds=900)
    assert first is not None
    execution = ScheduledTaskExecution(
        id="execution-1",
        task_id="task-1",
        session_id="session-1",
        claim_id=first.claim_id,
        lease_owner=first.lease_owner,
        status=ScheduledTaskExecutionStatus.RUNNING,
        claimed_next_run_at=first.claimed_next_run_at,
        started_at=now,
    )
    await registry.record_execution_started(execution)

    second = await registry.claim_task_for_run_now("task-1", now + timedelta(minutes=1), lease_seconds=900)

    assert second is None


@pytest.mark.asyncio
async def test_past_grace_claim_advances_next_run_without_skipped_flag(registry):
    """Two-segment model: registry claims and advances next_run_at;
    skipped_missed decision is post-claim (SchedulePolicy)."""
    now = datetime(2026, 6, 16, 1, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now - timedelta(hours=1)))

    claims = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    task = await registry.get("task-1")

    assert len(claims) == 1
    assert claims[0].skipped_missed is False
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
async def test_completed_due_claim_releases_lease_for_next_interval(registry):
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
    completed = ScheduledTaskExecution(
        **{**execution.__dict__, "status": ScheduledTaskExecutionStatus.SUCCEEDED, "completed_at": now, "output": "done"}
    )

    assert await registry.record_execution_completed(completed) is True
    task = await registry.get("task-1")
    assert task is not None
    assert task.lease_until is None

    next_claims = await registry.claim_due_tasks(now + timedelta(minutes=5, seconds=1), limit=5, lease_seconds=900)

    assert len(next_claims) == 1
    assert next_claims[0].skipped_missed is False


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


def _seed_legacy_origin_row(db_path, task_id: str, origin: dict, delivery_context: dict | None = None) -> None:
    SQLiteScheduledTaskRegistry(db_path, CroniterScheduleCalculator())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO scheduled_tasks(id, name, prompt, cron_expression, timezone, enabled, status,
                session_id, origin_json, delivery_target, delivery_context_json, execution_policy_json,
                next_run_at, created_at, updated_at)
            VALUES (?, 'n', 'p', '*/5 * * * *', 'UTC', 1, ?, 'session-1', ?, 'origin', ?, '{}',
                '2026-06-16T00:00:00+00:00', '2026-06-16T00:00:00+00:00', '2026-06-16T00:00:00+00:00')
            """,
            (
                task_id,
                ScheduledTaskStatus.ACTIVE.value,
                json.dumps(origin),
                json.dumps(delivery_context if delivery_context is not None else origin),
            ),
        )


def test_origin_json_source_type_migrated_to_platform(tmp_path, caplog):
    db_path = tmp_path / "sessions.db"
    _seed_legacy_origin_row(
        db_path,
        "task-legacy",
        {"source_type": "feishu", "receive_id": "oc_a", "receive_id_type": "chat_id", "thread_id": ""},
    )

    with caplog.at_level(logging.INFO):
        SQLiteScheduledTaskRegistry(db_path, CroniterScheduleCalculator())

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT origin_json, delivery_context_json FROM scheduled_tasks WHERE id = 'task-legacy'").fetchone()
    origin = json.loads(row[0])
    delivery_context = json.loads(row[1])
    assert "source_type" not in origin
    assert origin["platform"] == "feishu"
    assert "source_type" not in delivery_context
    assert delivery_context["platform"] == "feishu"
    assert any("source_type→platform" in record.message for record in caplog.records)


def test_origin_json_migration_skips_already_migrated_rows(tmp_path):
    db_path = tmp_path / "sessions.db"
    _seed_legacy_origin_row(
        db_path,
        "task-modern",
        {"platform": "feishu", "receive_id": "oc_b", "receive_id_type": "chat_id", "thread_id": ""},
    )

    SQLiteScheduledTaskRegistry(db_path, CroniterScheduleCalculator())

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT origin_json, delivery_context_json FROM scheduled_tasks WHERE id = 'task-modern'").fetchone()
    origin = json.loads(row[0])
    delivery_context = json.loads(row[1])
    assert origin == {"platform": "feishu", "receive_id": "oc_b", "receive_id_type": "chat_id", "thread_id": ""}
    assert delivery_context == {"platform": "feishu", "receive_id": "oc_b", "receive_id_type": "chat_id", "thread_id": ""}


def test_origin_json_migration_is_idempotent_with_no_legacy_rows(tmp_path, caplog):
    db_path = tmp_path / "sessions.db"
    SQLiteScheduledTaskRegistry(db_path, CroniterScheduleCalculator())

    with caplog.at_level(logging.INFO):
        SQLiteScheduledTaskRegistry(db_path, CroniterScheduleCalculator())

    assert not any("source_type→platform" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# S4: Registry transaction atomicity tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_due_claims_only_one_succeeds(tmp_path):
    """Two concurrent claim_due_tasks calls -> only 1 claim succeeds (CAS)."""
    import asyncio

    registry = SQLiteScheduledTaskRegistry(
        tmp_path / "concurrent.db", CroniterScheduleCalculator(), missed_grace_seconds=300
    )
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now))

    results = await asyncio.gather(
        registry.claim_due_tasks(now, limit=5, lease_seconds=900),
        registry.claim_due_tasks(now, limit=5, lease_seconds=900),
    )

    total_claims = sum(len(r) for r in results)
    assert total_claims == 1, "only one concurrent claim should succeed"


@pytest.mark.asyncio
async def test_two_concurrent_run_now_claims_only_one_succeeds(tmp_path):
    """Two concurrent claim_task_for_run_now calls -> only 1 succeeds (CAS)."""
    import asyncio

    registry = SQLiteScheduledTaskRegistry(
        tmp_path / "concurrent_rn.db", CroniterScheduleCalculator(), missed_grace_seconds=300
    )
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now + timedelta(hours=1)))

    results = await asyncio.gather(
        registry.claim_task_for_run_now("task-1", now, lease_seconds=900),
        registry.claim_task_for_run_now("task-1", now, lease_seconds=900),
    )

    claims = [r for r in results if r is not None]
    assert len(claims) == 1, "only one concurrent run-now claim should succeed"


@pytest.mark.asyncio
async def test_claim_cas_checks_status_in_transaction(registry):
    """Status check is in the CAS WHERE clause -- paused tasks are not claimed."""
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now))
    await registry.update_status("task-1", ScheduledTaskStatus.PAUSED, enabled=False)

    claims = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    assert claims == []


@pytest.mark.asyncio
async def test_claim_cas_checks_due_in_transaction(registry):
    """Due check is in the CAS WHERE clause -- future tasks are not claimed."""
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now + timedelta(hours=1)))

    claims = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    assert claims == []


@pytest.mark.asyncio
async def test_claim_cas_checks_lease_in_transaction(registry):
    """Lease check is in the CAS WHERE clause -- leased tasks are not re-claimed."""
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now))

    first = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    second = await registry.claim_due_tasks(now, limit=5, lease_seconds=900)

    assert len(first) == 1
    assert second == []


@pytest.mark.asyncio
async def test_claim_does_not_set_last_status_skipped_missed(registry):
    """Two-segment model: registry does not set last_status=SKIPPED_MISSED.
    The missed decision is post-claim (SchedulePolicy)."""
    now = datetime(2026, 6, 16, 1, 0, tzinfo=timezone.utc)
    await registry.create(_task(next_run_at=now - timedelta(hours=1)))

    await registry.claim_due_tasks(now, limit=5, lease_seconds=900)
    task = await registry.get("task-1")

    assert task is not None
    assert task.last_status is not ScheduledTaskExecutionStatus.SKIPPED_MISSED
