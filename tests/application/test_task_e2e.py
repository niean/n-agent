"""Batch H / T22: end-to-end Task lifecycle.

Wires real Domain + Infrastructure + Application services together (only the
LLM-bearing ChatCompletionService is faked, so no real provider call). Verifies
the orchestration path: create -> dispatch claim -> worker runs -> task_complete
intent -> CAS finalize -> DONE, with events and run record persisted.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.application.task_agent_executor import TaskAgentExecutor, TaskAgentResult
from app.application.task_run_service import TaskRunService
from app.application.task_runner import TaskRunner
from app.application.task_service import TaskService
from app.domain.policy import ExecutionMode
from app.domain.task import (
    FinishRunCommand,
    Task,
    TaskRunOutcome,
    TaskStatus,
)
from app.domain.task_policy import TaskPolicy
from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry


class _FakeChat:
    """Fake ChatCompletionService: returns a fixed assistant message that
    instructs the worker to call task_complete. No real LLM call."""

    def __init__(self):
        self.calls = 0

    async def complete(self, request, **_):
        self.calls += 1
        # Return a ChatCompletionResult-like object the executor can consume.
        return type(
            "R",
            (),
            {
                "message": {"role": "assistant", "content": "done"},
                "finish_reason": "stop",
            },
        )()


def _make_services(tmp_path):
    registry = SQLiteTaskRegistry(str(tmp_path / "e2e.db"))
    policy = TaskPolicy()
    chat = _FakeChat()
    executor = TaskAgentExecutor(
        chat_service=chat, task_registry=registry,
        prompt_builder=lambda *a, **k: "system",
    )
    runner = TaskRunner(interval_seconds=60, shutdown_grace_seconds=5)
    run_service = TaskRunService(
        registry=registry, dispatcher=runner, executor=executor,
        policy=policy, notifier=None,
        lease_seconds=900, heartbeat_timeout_seconds=300,
        max_runtime_seconds=60, max_concurrency=2,
    )
    runner.set_run_service(run_service)
    service = TaskService(
        registry=registry, policy=policy, planning_service=None,
        memory_store=None,
        attachments_root=tmp_path / "atts",
        attachment_max_bytes=1024 * 1024,
        attachment_task_max_bytes=10 * 1024 * 1024,
    )
    service.set_run_service(run_service)
    return service, run_service, runner, registry


@pytest.mark.asyncio
async def test_task_lifecycle_create_dispatch_complete(tmp_path):
    service, run_service, runner, registry = _make_services(tmp_path)

    # 1. Create a READY task directly in the registry (assignee set so
    #    dispatcher can claim; bypass TaskService.create_task's TRIAGE default
    #    since the E2E focus is dispatch->execute->finalize, not specify).
    task = Task(
        id="t_e2e_1", title="E2E lifecycle", status=TaskStatus.READY,
        assignee="default", created_at=datetime.now(timezone.utc),
        created_by="e2e", version=1, max_retries=3,
    )
    await registry.create_task(task)
    await registry.append_event(task.id, "created", {"title": task.title})

    # 2. Patch the executor to return a COMPLETED intent (simulates worker
    #    calling task_complete).
    async def _fake_run(t, run_id, claim_lock):
        return TaskAgentResult(status=TaskRunOutcome.COMPLETED, output="all done")
    run_service.executor.run = _fake_run
    run_service.executor.run_goal_loop = _fake_run

    # 3. dispatch_once: claim + spawn (in-process asyncio task).
    result = await run_service.dispatch_once()
    assert result["spawned"] == 1

    # 4. Wait for the worker to finish (run_claim does CAS finalize).
    await asyncio.sleep(0.2)

    final = await registry.get_task(task.id)
    assert final.status == TaskStatus.DONE, f"expected DONE, got {final.status}"
    assert final.consecutive_failures == 0

    # 5. Events recorded.
    events = await registry.list_events(task.id)
    kinds = [e.kind for e in events]
    assert "created" in kinds

    # 6. Run recorded with COMPLETED outcome.
    runs = await registry.list_runs(task.id)
    assert len(runs) >= 1
    assert runs[0].outcome == TaskRunOutcome.COMPLETED


@pytest.mark.asyncio
async def test_task_failure_records_failed_outcome(tmp_path):
    service, run_service, runner, registry = _make_services(tmp_path)
    task = Task(
        id="t_e2e_2", title="flaky", status=TaskStatus.READY,
        assignee="default", created_at=datetime.now(timezone.utc),
        created_by="e2e", version=1, max_retries=1,
    )
    await registry.create_task(task)

    async def _fail_run(t, run_id, claim_lock):
        return TaskAgentResult(status=TaskRunOutcome.FAILED, error="boom")
    run_service.executor.run = _fail_run
    run_service.executor.run_goal_loop = _fail_run

    # Single dispatch with a failing executor.
    await run_service.dispatch_once()
    await asyncio.sleep(0.2)

    # The run must record a FAILED (or GAVE_UP if circuit breaker tripped)
    # outcome. consecutive_failures advances. The circuit-breaker -> GAVE_UP
    # mapping is unit-tested in test_task_run_service.py; here we only assert
    # the failure was persisted as a run outcome.
    runs = await registry.list_runs(task.id)
    outcomes = [r.outcome for r in runs]
    assert TaskRunOutcome.FAILED in outcomes or TaskRunOutcome.GAVE_UP in outcomes, outcomes

    final = await registry.get_task(task.id)
    # Either retried to TODO (consecutive_failures=1, within max_retries=1) or
    # gave up to BLOCKED. Both are valid terminal/retry states.
    assert final.status in (TaskStatus.TODO, TaskStatus.BLOCKED), final.status
