"""T14: TaskRunService tests.

Tests:
  - dispatch_once claims and spawns
  - run_claim finishes with CAS
  - crash recovery marks CRASHED
  - circuit breaker gives up (GAVE_UP)
  - notify idempotent by terminal event
  - spawn failure handling
  - timeout handling
  - stale recovery (lease expired)
  - terminate
  - dispatch order and concurrency
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.application.task_agent_executor import TaskAgentResult
from app.application.task_run_service import TaskRunService
from app.domain.policy import PolicyOutcome
from app.domain.task import (
    BlockKind,
    ClaimResult,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
    TaskConflictError,
    TaskEvent,
    TaskNotFoundError,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStatus,
)
from app.domain.task_policy import TaskPolicy, TaskPolicyRequest


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRegistry:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._runs: dict[int, TaskRun] = {}
        self._events: list[TaskEvent] = []
        self._next_run_id = 1
        self._next_event_id = 1
        self.finish_calls: list[FinishRunCommand] = []
        self.recover_calls: list[RecoverRunCommand] = []

    async def create_task(self, task: Task) -> Task:
        self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list_ready(self, board="default", limit=100):
        return tuple(
            t for t in self._tasks.values()
            if t.status == TaskStatus.READY and t.assignee
        )

    async def list_running(self, board="default"):
        return tuple(
            t for t in self._tasks.values() if t.status == TaskStatus.RUNNING
        )

    async def recompute_ready(self, board="default"):
        return ()

    async def claim_task(self, task_id, claim_lock, lease_seconds):
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.READY:
            return None
        from dataclasses import replace as dc_replace
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=lease_seconds)
        run_id = self._next_run_id
        self._next_run_id += 1
        run = TaskRun(
            id=run_id, task_id=task_id, status=TaskRunStatus.RUNNING,
            claim_lock=claim_lock, claim_expires=expires, started_at=now,
        )
        self._runs[run_id] = run
        updated = dc_replace(
            task, status=TaskStatus.RUNNING, claim_lock=claim_lock,
            claim_expires=expires,
            current_run_id=run_id, worker_token=f"wt_{run_id}",
            last_heartbeat_at=now, version=task.version + 1,
        )
        self._tasks[task_id] = updated
        return ClaimResult(task=updated, run=run)

    async def finish_run(self, command: FinishRunCommand):
        self.finish_calls.append(command)
        task = self._tasks.get(command.task_id)
        run = self._runs.get(command.run_id)
        if task is None or run is None:
            raise TaskNotFoundError(f"task or run not found: {command.task_id}/{command.run_id}")
        if task.claim_lock != command.claim_lock or task.current_run_id != command.run_id:
            raise TaskConflictError("CAS failed")
        from dataclasses import replace as dc_replace
        now = datetime.now(timezone.utc)
        new_status = command.target_task_status or TaskStatus.TODO
        if command.outcome == TaskRunOutcome.COMPLETED:
            new_status = TaskStatus.DONE
        failures = task.consecutive_failures
        if command.outcome not in (TaskRunOutcome.COMPLETED, TaskRunOutcome.BLOCKED, TaskRunOutcome.GAVE_UP):
            failures += 1
        elif command.outcome == TaskRunOutcome.COMPLETED:
            failures = 0
        updated_task = dc_replace(
            task, status=new_status, claim_lock=None, claim_expires=None,
            current_run_id=None, worker_token=None,
            consecutive_failures=failures,
            version=task.version + 1, updated_at=now,
        )
        self._tasks[command.task_id] = updated_task
        updated_run = dc_replace(
            run, status=TaskRunStatus.COMPLETED, outcome=command.outcome,
            ended_at=now, summary=command.summary, error=command.error,
        )
        self._runs[command.run_id] = updated_run
        event = TaskEvent(
            id=self._next_event_id, task_id=command.task_id, kind="finished",
            payload={"outcome": command.outcome.value}, run_id=command.run_id,
            created_at=now,
        )
        self._next_event_id += 1
        self._events.append(event)
        return FinishRunResult(task=updated_task, run=updated_run, terminal_event=event)

    async def recover_run(self, command: RecoverRunCommand):
        self.recover_calls.append(command)
        return await self.finish_run(FinishRunCommand(
            task_id=command.task_id, run_id=command.run_id,
            claim_lock=command.claim_lock, outcome=command.outcome,
            error=command.error,
        ))

    async def append_event(self, task_id, kind, payload, run_id=None):
        event = TaskEvent(
            id=self._next_event_id, task_id=task_id, kind=kind,
            payload=dict(payload), run_id=run_id,
            created_at=datetime.now(timezone.utc),
        )
        self._next_event_id += 1
        self._events.append(event)
        return event


class FakeDispatcher:
    def __init__(self):
        self.spawns: list[tuple[Task, int, str]] = []
        self.cancels: list[str] = []
        self._active: dict[int, dict[str, Any]] = {}
        self._crashed: list[dict[str, Any]] = []

    async def spawn(self, task: Task, run_id: int, claim_lock: str) -> str:
        token = f"wt_{run_id}"
        self.spawns.append((task, run_id, claim_lock))
        self._active[run_id] = {
            "run_id": run_id, "task_id": task.id,
            "worker_token": token, "started_at": datetime.now(timezone.utc).isoformat(),
        }
        return token

    async def cancel(self, worker_token: str) -> bool:
        self.cancels.append(worker_token)
        # Remove from active
        to_remove = [
            rid for rid, info in self._active.items()
            if info.get("worker_token") == worker_token
        ]
        for rid in to_remove:
            del self._active[rid]
        return True

    async def inspect(self) -> dict[str, Any]:
        return {"active": list(self._active.values())}

    async def get_crashed_workers(self) -> list[dict[str, Any]]:
        return list(self._crashed)

    def add_crashed(self, run_id: int, task_id: str, error: str = "crashed"):
        self._crashed.append({"run_id": run_id, "task_id": task_id, "error": error})


class FakeExecutor:
    def __init__(self, result: TaskAgentResult | None = None, delay: float = 0):
        self._result = result or TaskAgentResult(
            status=TaskRunOutcome.COMPLETED, output="done",
        )
        self.delay = delay
        self.calls: list[tuple[Task, int, str]] = []

    async def run(self, task: Task, run_id: int, claim_lock: str) -> TaskAgentResult:
        self.calls.append((task, run_id, claim_lock))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._result

    async def run_goal_loop(self, task: Task, run_id: int, claim_lock: str) -> TaskAgentResult:
        return await self.run(task, run_id, claim_lock)


class FakeNotifier:
    def __init__(self):
        self.deliveries: list[tuple[Task, TaskEvent]] = []

    async def deliver(self, task: Task, terminal_event: TaskEvent) -> Any:
        self.deliveries.append((task, terminal_event))
        from app.domain.task import DeliveryResult
        return DeliveryResult(delivered=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ready_task(**kwargs) -> Task:
    defaults = dict(
        id="t_1", title="Test", status=TaskStatus.READY, assignee="default",
        created_at=datetime.now(timezone.utc), version=1, max_retries=3,
    )
    defaults.update(kwargs)
    return Task(**defaults)


@pytest.fixture
def registry():
    return FakeRegistry()


@pytest.fixture
def dispatcher():
    return FakeDispatcher()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def run_service(registry, dispatcher, notifier):
    return TaskRunService(
        registry=registry,
        dispatcher=dispatcher,
        executor=FakeExecutor(),
        policy=TaskPolicy(),
        notifier=notifier,
        max_concurrency=4,
        lease_seconds=900,
        heartbeat_timeout_seconds=300,
    )


# ---------------------------------------------------------------------------
# dispatch_once tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_claims_and_spawns(run_service, registry, dispatcher):
    await registry.create_task(_ready_task(id="t_1"))
    result = await run_service.dispatch_once()
    assert result["spawned"] == 1
    assert len(dispatcher.spawns) == 1


@pytest.mark.asyncio
async def test_dispatch_skips_tasks_without_assignee(run_service, registry, dispatcher):
    await registry.create_task(_ready_task(id="t_1", assignee=None))
    result = await run_service.dispatch_once()
    assert result["spawned"] == 0
    assert len(dispatcher.spawns) == 0


@pytest.mark.asyncio
async def test_dispatch_respects_max_concurrency(run_service, registry, dispatcher):
    run_service.max_concurrency = 2
    await registry.create_task(_ready_task(id="t_1", priority=5))
    await registry.create_task(_ready_task(id="t_2", priority=4))
    await registry.create_task(_ready_task(id="t_3", priority=3))
    result = await run_service.dispatch_once()
    assert result["spawned"] == 2
    assert len(dispatcher.spawns) == 2


@pytest.mark.asyncio
async def test_dispatch_priority_order(run_service, registry, dispatcher):
    await registry.create_task(_ready_task(id="t_low", priority=1, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    await registry.create_task(_ready_task(id="t_high", priority=10, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc)))
    await run_service.dispatch_once()
    # Higher priority should be spawned first
    assert dispatcher.spawns[0][0].id == "t_high"


@pytest.mark.asyncio
async def test_dispatch_single_failure_does_not_block_others(run_service, registry, dispatcher):
    await registry.create_task(_ready_task(id="t_1"))
    await registry.create_task(_ready_task(id="t_2"))
    # Make the first claim fail by pre-claiming t_1
    await registry.claim_task("t_1", "other-lock", 900)
    result = await run_service.dispatch_once()
    # t_1 already claimed (not READY), t_2 should still spawn
    assert result["spawned"] == 1


# ---------------------------------------------------------------------------
# run_claim tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_claim_finishes_with_cas(run_service, registry):
    task = _ready_task()
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_service.run_claim(claim.task, claim.run.id, "lock-1")
    assert len(registry.finish_calls) == 1
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.COMPLETED
    assert cmd.target_task_status == TaskStatus.DONE
    # Task is no longer RUNNING
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_run_claim_blocked_intent(run_service, registry, dispatcher):
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.BLOCKED, error="need input",
        metadata={"block_kind": "needs_input"},
    ))
    task = _ready_task()
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.BLOCKED
    assert cmd.target_task_status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_run_claim_failed_retry_to_todo(run_service, registry):
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="llm error",
    ))
    task = _ready_task(max_retries=3, consecutive_failures=0)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.FAILED
    # consecutive_failures=0, projected=1, 1 > 3 = False -> ALLOW -> TODO
    assert cmd.target_task_status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_run_claim_circuit_breaker_gives_up(run_service, registry):
    """consecutive_failures > max_retries -> GAVE_UP -> BLOCKED."""
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="llm error",
    ))
    task = _ready_task(max_retries=3, consecutive_failures=3)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    # Circuit breaker trips (projected=4 > max_retries=3) -> outcome GAVE_UP
    assert cmd.outcome == TaskRunOutcome.GAVE_UP
    # GAVE_UP -> BLOCKED
    assert cmd.target_task_status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_run_claim_timeout(run_service, registry):
    """asyncio.wait_for timeout -> TIMED_OUT."""
    run_service.executor = FakeExecutor(delay=10)
    run_service.lease_seconds = 2  # Short lease for test
    task = _ready_task(max_runtime_seconds=1)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.TIMED_OUT


@pytest.mark.asyncio
async def test_run_claim_executor_crash(run_service, registry):
    """Executor raises exception -> CRASHED."""
    class CrashExecutor:
        async def run(self, task, run_id, claim_lock):
            raise RuntimeError("agent crashed")
        async def run_goal_loop(self, task, run_id, claim_lock):
            raise RuntimeError("agent crashed")

    run_service.executor = CrashExecutor()
    task = _ready_task(max_retries=3, consecutive_failures=0)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.CRASHED


# ---------------------------------------------------------------------------
# Spawn failure tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_failure_records_spawn_failed(run_service, registry, dispatcher):
    async def fail_spawn(*args, **kwargs):
        raise RuntimeError("spawn error")

    dispatcher.spawn = fail_spawn
    await registry.create_task(_ready_task(id="t_1"))
    await run_service.dispatch_once()
    # SPAWN_FAILED should be recorded
    assert len(registry.finish_calls) == 1
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.SPAWN_FAILED


# ---------------------------------------------------------------------------
# Notify tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_on_completed(run_service, registry, notifier):
    task = _ready_task()
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    assert len(notifier.deliveries) == 1


@pytest.mark.asyncio
async def test_notify_not_on_retryable_failed(run_service, registry, notifier):
    """Retryable FAILED does not trigger notification."""
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="retryable",
    ))
    task = _ready_task(max_retries=3, consecutive_failures=0)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    assert len(notifier.deliveries) == 0


@pytest.mark.asyncio
async def test_notify_on_gave_up(run_service, registry, notifier):
    """GAVE_UP (circuit breaker) triggers notification."""
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="final failure",
    ))
    task = _ready_task(max_retries=0, consecutive_failures=1)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    # projected failures = 2, 2 > 0 = True -> DENY -> BLOCKED (GAVE_UP)
    # But the outcome is FAILED, not GAVE_UP... The target is BLOCKED.
    # Let me check: the notification is based on the run outcome.
    # The run outcome is FAILED (from executor), but target_task_status is BLOCKED.
    # FAILED is retryable and NOT in _NOTIFIED_OUTCOMES.
    # Hmm, but the spec says GAVE_UP should notify. The issue is that the
    # outcome sent to finish_run is FAILED, not GAVE_UP.
    # The decision to GAVE_UP is made by TaskPolicy, but the outcome stays
    # as the original FAILED. The registry maps GAVE_UP -> BLOCKED but
    # doesn't change the outcome.
    # This is a design question: should TaskRunService change the outcome
    # to GAVE_UP when the circuit breaker trips?
    # Let me check the spec: "consecutive_failures > max_retries -> GAVE_UP"
    # So the outcome SHOULD be GAVE_UP when the circuit breaker trips.
    pass  # This test reveals a design issue -- see test below


@pytest.mark.asyncio
async def test_circuit_breaker_changes_outcome_to_gave_up(run_service, registry, notifier):
    """When circuit breaker trips, outcome changes to GAVE_UP for notification."""
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="final failure",
    ))
    task = _ready_task(max_retries=0, consecutive_failures=1)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    # The outcome should be GAVE_UP (not FAILED) when circuit breaker trips
    assert cmd.outcome == TaskRunOutcome.GAVE_UP
    assert cmd.target_task_status == TaskStatus.BLOCKED
    # GAVE_UP is in _NOTIFIED_OUTCOMES
    assert len(notifier.deliveries) == 1


# ---------------------------------------------------------------------------
# Recovery tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_stale_executions(run_service, registry):
    """RUNNING task with expired lease -> RECLAIMED."""
    from dataclasses import replace as dc_replace
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_stale", title="stale", status=TaskStatus.RUNNING,
        assignee="d", created_at=now - timedelta(hours=1),
        claim_lock="old-lock", claim_expires=now - timedelta(minutes=5),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now - timedelta(minutes=10),
        version=1, max_retries=3,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_stale", status=TaskRunStatus.RUNNING,
        claim_lock="old-lock",
    )
    result = await run_service.dispatch_once()
    assert result["recovered_stale"] >= 1
    assert len(registry.recover_calls) == 1
    assert registry.recover_calls[0].outcome == TaskRunOutcome.RECLAIMED


@pytest.mark.asyncio
async def test_recover_stale_skips_valid_lease(run_service, registry, dispatcher):
    """RUNNING task with valid lease and active worker -> not reclaimed."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_active", title="active", status=TaskStatus.RUNNING,
        assignee="d", created_at=now - timedelta(minutes=5),
        claim_lock="lock-1", claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now - timedelta(seconds=30),
        version=1, max_retries=3,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_active", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1",
    )
    # Simulate active worker in dispatcher
    dispatcher._active[1] = {"run_id": 1, "task_id": "t_active"}
    result = await run_service.dispatch_once()
    assert result["recovered_stale"] == 0


@pytest.mark.asyncio
async def test_recover_crashed_workers(run_service, registry, dispatcher):
    """In-process worker that crashed -> CRASHED."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_crash", title="crash", status=TaskStatus.RUNNING,
        assignee="d", created_at=now - timedelta(minutes=5),
        claim_lock="lock-1", claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now - timedelta(seconds=30),
        version=1, max_retries=3, consecutive_failures=0,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_crash", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1",
    )
    dispatcher.add_crashed(1, "t_crash", "asyncio task crashed")
    result = await run_service.dispatch_once()
    assert result["recovered_crashed"] >= 1
    assert len(registry.recover_calls) == 1
    assert registry.recover_calls[0].outcome == TaskRunOutcome.CRASHED


# ---------------------------------------------------------------------------
# Terminate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_with_worker(run_service, registry, dispatcher):
    """Terminate cancels in-process worker and finishes TERMINATED."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_term", title="term", status=TaskStatus.RUNNING,
        assignee="d", created_at=now,
        claim_lock="lock-1", claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now, version=1, max_retries=3,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_term", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1",
    )
    dispatcher._active[1] = {"run_id": 1, "task_id": "t_term", "worker_token": "wt_1"}
    result = await run_service.terminate("t_term")
    assert result["status"] == "terminated"
    assert len(dispatcher.cancels) == 1
    assert len(registry.finish_calls) == 1
    assert registry.finish_calls[0].outcome == TaskRunOutcome.TERMINATED


@pytest.mark.asyncio
async def test_terminate_without_worker(run_service, registry, dispatcher):
    """Terminate without in-process handle -> terminate_requested event."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_term2", title="term", status=TaskStatus.RUNNING,
        assignee="d", created_at=now,
        claim_lock="lock-1", claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now, version=1, max_retries=3,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_term2", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1",
    )
    # No active worker in dispatcher
    result = await run_service.terminate("t_term2")
    assert result["status"] == "terminate_requested"


@pytest.mark.asyncio
async def test_terminate_not_running(run_service, registry):
    task = _ready_task()
    await registry.create_task(task)
    result = await run_service.terminate(task.id)
    assert result["status"] == "not_running"


# ---------------------------------------------------------------------------
# Late worker / CAS conflict tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_worker_cas_conflict(run_service, registry):
    """Late worker finish_run CAS conflict -> logged, not overwritten."""
    task = _ready_task()
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    # Simulate the task already being finalized by a newer run
    from dataclasses import replace as dc_replace
    finalized = dc_replace(
        claim.task, status=TaskStatus.DONE, claim_lock=None,
        current_run_id=None, version=claim.task.version + 1,
    )
    registry._tasks[task.id] = finalized
    # Now run_claim tries to finish -- should get CAS conflict
    await run_claim_with_spawn(run_service, task, claim)
    # The finish_run should have raised TaskConflictError, caught by _finalize_run
    # The task should still be DONE (not overwritten)
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.DONE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def run_claim_with_spawn(run_service, task, claim):
    """Helper: call run_claim directly (simulates what dispatcher.spawn does)."""
    await run_service.run_claim(claim.task, claim.run.id, claim.run.claim_lock or "lock-1")
