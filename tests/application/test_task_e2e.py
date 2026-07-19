"""T4: end-to-end Task lifecycle (Manus-aligned 7-state machine).

Wires real Domain + Infrastructure + Application services together (only the
LLM-bearing ChatCompletionService is faked, so no real provider call). Verifies
the orchestration path: create -> dispatch claim -> worker runs -> task_complete
intent -> CAS finalize -> SUCCEEDED, with events and run record persisted.

NOTE: task_run_service.py / task_agent_executor.py are adapted in T5/T6; this
e2e file only covers the subset that TaskService itself can drive. Full
propose/approve/reject e2e is covered by test_task_run_service.py (T5) and
the Docker E2E suite.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.application.task_service import TaskService
from app.domain.task import Task, TaskStatus
from app.domain.task_policy import TaskPolicy
from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry


def _make_services(tmp_path):
    registry = SQLiteTaskRegistry(str(tmp_path / "e2e.db"))
    policy = TaskPolicy()
    service = TaskService(
        registry=registry, policy=policy, memory_store=None,
        attachments_root=tmp_path / "atts",
        attachment_max_bytes=1024 * 1024,
        attachment_task_max_bytes=10 * 1024 * 1024,
    )
    return service, registry


@pytest.mark.asyncio
async def test_create_task_defaults_to_queued(tmp_path):
    service, registry = _make_services(tmp_path)
    task = await service.create_task(title="E2E lifecycle", created_by="e2e")
    assert task.status == TaskStatus.QUEUED
    assert task.is_archived is False

    fetched = await registry.get_task(task.id)
    assert fetched is not None
    assert fetched.status == TaskStatus.QUEUED

    events = await registry.list_events(task.id)
    assert any(e.kind == "created" for e in events)


@pytest.mark.asyncio
async def test_claim_transitions_queued_to_running(tmp_path):
    service, registry = _make_services(tmp_path)
    task = await service.create_task(title="claim me", created_by="e2e")

    result = await registry.claim_task(task.id, "lock-1", 900)
    assert result is not None
    assert result.task.status == TaskStatus.RUNNING
    assert result.run.id is not None
    assert result.task.current_run_id == result.run.id

    fetched = await registry.get_task(task.id)
    assert fetched.status == TaskStatus.RUNNING
    assert fetched.claim_lock == "lock-1"


@pytest.mark.asyncio
async def test_finish_run_completed_transitions_to_succeeded(tmp_path):
    service, registry = _make_services(tmp_path)
    task = await service.create_task(title="finish me", created_by="e2e")
    claim = await registry.claim_task(task.id, "lock-1", 900)
    from app.domain.task import FinishRunCommand, TaskRunOutcome
    result = await registry.finish_run(FinishRunCommand(
        task_id=task.id, run_id=claim.run.id, claim_lock="lock-1",
        outcome=TaskRunOutcome.COMPLETED, summary="all done",
        target_task_status=TaskStatus.SUCCEEDED,
    ))
    assert result.task.status == TaskStatus.SUCCEEDED
    assert result.task.result == "all done"
    assert result.task.consecutive_failures == 0

    runs = await registry.list_runs(task.id)
    assert len(runs) >= 1
    assert runs[0].outcome == TaskRunOutcome.COMPLETED


@pytest.mark.asyncio
async def test_propose_approve_cycle(tmp_path):
    """propose_change -> WAITING_APPROVAL; approve -> QUEUED."""
    service, registry = _make_services(tmp_path)
    task = await service.create_task(title="propose me", created_by="e2e")
    claim = await registry.claim_task(task.id, "lock-1", 900)

    # Worker proposes a change
    result = await service.propose_change(
        task.id, "switch to plan B", run_id=claim.run.id,
    )
    assert result["outcome"] == "waiting_approval"

    # Task is now WAITING_APPROVAL (claim released by the state transition;
    # in production T5's run finalization releases the claim atomically, but
    # the TaskService.propose_change path writes the event and advances state
    # regardless).
    events = await registry.list_events(task.id)
    assert any(e.kind == "change_proposed" for e in events)

    # Simulate run finalization releasing the claim (T5 would do this via
    # TaskRunService.finish_run with outcome=WAITING_APPROVAL).
    from app.domain.task import FinishRunCommand, TaskRunOutcome
    await registry.finish_run(FinishRunCommand(
        task_id=task.id, run_id=claim.run.id, claim_lock="lock-1",
        outcome=TaskRunOutcome.WAITING_APPROVAL,
        target_task_status=TaskStatus.WAITING_APPROVAL,
    ))

    # User approves
    approve_result = await service.approve_change(task.id)
    assert approve_result["decision"] == "approved"

    fetched = await registry.get_task(task.id)
    assert fetched.status == TaskStatus.QUEUED

    events = await registry.list_events(task.id)
    assert any(e.kind == "change_approved" for e in events)


@pytest.mark.asyncio
async def test_retry_failed_task(tmp_path):
    service, registry = _make_services(tmp_path)
    task = await service.create_task(title="flaky", created_by="e2e")
    # Directly place in FAILED (bypass run path)
    await registry.update_task(
        task.id, {"status": TaskStatus.FAILED}, expected_version=1,
    )
    await service.retry_task(task.id)
    fetched = await registry.get_task(task.id)
    assert fetched.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_cancel_queued_task(tmp_path):
    service, registry = _make_services(tmp_path)
    task = await service.create_task(title="cancel me", created_by="e2e")
    await service.cancel_task(task.id)
    fetched = await registry.get_task(task.id)
    assert fetched.status == TaskStatus.CANCELLED
    events = await registry.list_events(task.id)
    assert any(e.kind == "cancelled" for e in events)
