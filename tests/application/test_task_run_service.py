"""T5: TaskRunService tests (Manus-aligned 7-state machine).

Tests:
  - dispatch_once: list_queued_due order + concurrency + priority
  - run_claim: COMPLETED -> SUCCEEDED, FAILED retry/breaker, TIMED_OUT/CRASHED -> EXPIRED
  - finalize_propose: WAITING_APPROVAL unifies claim release + worker cancel + terminal event
  - terminate: user cancel RUNNING -> TERMINATED -> CANCELLED
  - stale recovery: lease/heartbeat expired -> EXPIRED
  - crash recovery: crashed worker -> EXPIRED
  - notify: WAITING_APPROVAL/EXPIRED notify; auto-retry QUEUED does not
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.application.task_agent_executor import TaskAgentResult
from app.application.task_run_service import TaskRunService
from app.domain.policy import PolicyOutcome
from app.domain.task import (
    ClaimResult,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
    TaskArtifact,
    TaskClaimError,
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
    """In-memory registry fake aligned with the 7-state machine.

    - claim_task requires QUEUED + not archived + scheduled_at due.
    - finish_run applies target_task_status (caller owns the decision).
    - recover_run delegates to finish_run with is_recover semantics.
    """

    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._runs: dict[int, TaskRun] = {}
        self._events: list[TaskEvent] = []
        self._next_run_id = 1
        self._next_event_id = 1
        self.finish_calls: list[FinishRunCommand] = []
        self.recover_calls: list[RecoverRunCommand] = []
        self.list_queued_due_calls: list[dict[str, Any]] = []

    async def create_task(self, task: Task) -> Task:
        self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list_queued_due(
        self, now: datetime, limit: int = 100, board: str = "default"
    ) -> tuple[Task, ...]:
        self.list_queued_due_calls.append({"now": now, "limit": limit, "board": board})
        now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        due = [
            t for t in self._tasks.values()
            if t.status is TaskStatus.QUEUED
            and not t.is_archived
            and (t.scheduled_at is None or t.scheduled_at <= now_utc)
        ]
        due.sort(key=lambda t: (-t.priority, t.created_at or now_utc, t.id))
        return tuple(due[:limit])

    async def list_running(self, board: str = "default") -> tuple[Task, ...]:
        return tuple(
            t for t in self._tasks.values() if t.status is TaskStatus.RUNNING
        )

    async def claim_task(self, task_id, claim_lock, lease_seconds):
        task = self._tasks.get(task_id)
        if task is None or task.status is not TaskStatus.QUEUED:
            return None
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=lease_seconds)
        run_id = self._next_run_id
        self._next_run_id += 1
        run = TaskRun(
            id=run_id, task_id=task_id, status=TaskRunStatus.RUNNING,
            claim_lock=claim_lock, claim_expires=expires, started_at=now,
            worker_token=f"wt_{run_id}",
        )
        self._runs[run_id] = run
        updated = dc_replace(
            task, status=TaskStatus.RUNNING, claim_lock=claim_lock,
            claim_expires=expires,
            current_run_id=run_id, worker_token=f"wt_{run_id}",
            last_heartbeat_at=now, started_at=now, version=task.version + 1,
        )
        self._tasks[task_id] = updated
        return ClaimResult(task=updated, run=run)

    async def finish_run(self, command: FinishRunCommand):
        self.finish_calls.append(command)
        task = self._tasks.get(command.task_id)
        run = self._runs.get(command.run_id)
        if task is None or run is None:
            raise TaskNotFoundError(
                f"task or run not found: {command.task_id}/{command.run_id}"
            )
        if (
            task.claim_lock != command.claim_lock
            or task.current_run_id != command.run_id
        ):
            raise TaskConflictError("CAS failed")
        now = datetime.now(timezone.utc)
        new_status = command.target_task_status or TaskStatus.FAILED
        failures = task.consecutive_failures
        if command.outcome in (TaskRunOutcome.FAILED, TaskRunOutcome.SPAWN_FAILED):
            failures += 1
        elif command.outcome == TaskRunOutcome.COMPLETED:
            failures = 0
        updated_task = dc_replace(
            task, status=new_status, claim_lock=None, claim_expires=None,
            current_run_id=None, worker_token=None,
            consecutive_failures=failures,
            completed_at=now if new_status in (
                TaskStatus.SUCCEEDED, TaskStatus.CANCELLED
            ) else task.completed_at,
            result=command.summary if new_status is TaskStatus.SUCCEEDED else task.result,
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
        return FinishRunResult(
            task=updated_task, run=updated_run, terminal_event=event,
        )

    async def recover_run(self, command: RecoverRunCommand):
        self.recover_calls.append(command)
        # Delegate to finish_run path; recover defaults target via outcome.
        target = self._default_recover_target(command.outcome)
        return await self.finish_run(FinishRunCommand(
            task_id=command.task_id, run_id=command.run_id,
            claim_lock=command.claim_lock, outcome=command.outcome,
            error=command.error, target_task_status=target,
        ))

    @staticmethod
    def _default_recover_target(outcome: TaskRunOutcome) -> TaskStatus:
        if outcome in (TaskRunOutcome.EXPIRED, TaskRunOutcome.CRASHED, TaskRunOutcome.TIMED_OUT):
            return TaskStatus.EXPIRED
        if outcome == TaskRunOutcome.TERMINATED:
            return TaskStatus.CANCELLED
        return TaskStatus.FAILED

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
        self.spawn_raises: Exception | None = None

    async def spawn(self, task: Task, run_id: int, claim_lock: str) -> str:
        if self.spawn_raises is not None:
            raise self.spawn_raises
        token = f"wt_{run_id}"
        self.spawns.append((task, run_id, claim_lock))
        self._active[run_id] = {
            "run_id": run_id, "task_id": task.id,
            "worker_token": token, "started_at": datetime.now(timezone.utc).isoformat(),
        }
        return token

    async def cancel(self, worker_token: str) -> bool:
        self.cancels.append(worker_token)
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


class CrashExecutor:
    async def run(self, task, run_id, claim_lock):
        raise RuntimeError("agent crashed")

    async def run_goal_loop(self, task, run_id, claim_lock):
        raise RuntimeError("agent crashed")


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


def _queued_task(**kwargs) -> Task:
    defaults = dict(
        id="t_1", title="Test", status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc), version=1, max_retries=3,
    )
    defaults.update(kwargs)
    return Task(**defaults)


def _running_task(**kwargs) -> Task:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id="t_run", title="Running", status=TaskStatus.RUNNING,
        created_at=now, claim_lock="lock-1",
        claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now, version=1, max_retries=3,
        consecutive_failures=0,
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
    await registry.create_task(_queued_task(id="t_1"))
    result = await run_service.dispatch_once()
    assert result["spawned"] == 1
    assert len(dispatcher.spawns) == 1
    # No recompute_ready / promoted key (removed)
    assert "promoted" not in result


@pytest.mark.asyncio
async def test_dispatch_uses_list_queued_due(run_service, registry):
    await registry.create_task(_queued_task(id="t_1"))
    await run_service.dispatch_once()
    assert len(registry.list_queued_due_calls) == 1


@pytest.mark.asyncio
async def test_dispatch_respects_max_concurrency(run_service, registry, dispatcher):
    run_service.max_concurrency = 2
    await registry.create_task(_queued_task(id="t_1", priority=5))
    await registry.create_task(_queued_task(id="t_2", priority=4))
    await registry.create_task(_queued_task(id="t_3", priority=3))
    result = await run_service.dispatch_once()
    assert result["spawned"] == 2
    assert len(dispatcher.spawns) == 2


@pytest.mark.asyncio
async def test_dispatch_priority_order(run_service, registry, dispatcher):
    await registry.create_task(_queued_task(
        id="t_low", priority=1, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    await registry.create_task(_queued_task(
        id="t_high", priority=10, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    ))
    await run_service.dispatch_once()
    assert dispatcher.spawns[0][0].id == "t_high"


class _FakeConfigProvider:
    """Hot-reload provider stub. Returns a fixed TaskConfig; counts calls."""

    def __init__(self, config):
        self._config = config
        self.call_count = 0

    async def current(self):
        self.call_count += 1
        return self._config


@pytest.mark.asyncio
async def test_dispatch_uses_provider_snapshot_for_concurrency(registry, dispatcher, notifier):
    from app.domain.task_config import TaskConfig
    # Provider says max_concurrency=1, overriding the constructor scalar.
    provider = _FakeConfigProvider(TaskConfig(task_max_concurrency=1))
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher, executor=FakeExecutor(),
        policy=TaskPolicy(), notifier=notifier,
        max_concurrency=4,  # would allow 4 without provider
        task_config_provider=provider,
    )
    await registry.create_task(_queued_task(id="t_1", priority=5))
    await registry.create_task(_queued_task(id="t_2", priority=4))
    result = await svc.dispatch_once()
    # Provider cap of 1 wins over constructor 4.
    assert result["spawned"] == 1
    # Single snapshot per dispatch (not per task).
    assert provider.call_count == 1



@pytest.mark.asyncio
async def test_dispatch_skips_not_due_queued(run_service, registry, dispatcher):
    """QUEUED task with future scheduled_at is not dispatched."""
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    await registry.create_task(_queued_task(id="t_future", scheduled_at=future))
    result = await run_service.dispatch_once()
    assert result["spawned"] == 0


@pytest.mark.asyncio
async def test_dispatch_single_failure_does_not_block_others(
    run_service, registry, dispatcher,
):
    await registry.create_task(_queued_task(id="t_1"))
    await registry.create_task(_queued_task(id="t_2"))
    # Pre-claim t_1 so the dispatcher's claim_task returns None
    await registry.claim_task("t_1", "other-lock", 900)
    result = await run_service.dispatch_once()
    assert result["spawned"] == 1


# ---------------------------------------------------------------------------
# run_claim tests
# ---------------------------------------------------------------------------


async def _run_claim_with_spawn(run_service, task, claim):
    await run_service.run_claim(
        claim.task, claim.run.id, claim.run.claim_lock or "lock-1",
    )


@pytest.mark.asyncio
async def test_run_claim_completes_to_succeeded(run_service, registry):
    task = _queued_task()
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    assert len(registry.finish_calls) == 1
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.COMPLETED
    assert cmd.target_task_status == TaskStatus.SUCCEEDED
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.SUCCEEDED
    # Claim released
    assert final_task.claim_lock is None
    assert final_task.current_run_id is None


@pytest.mark.asyncio
async def test_run_claim_failed_retry_to_queued(run_service, registry):
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="llm error",
    ))
    task = _queued_task(max_retries=3, consecutive_failures=0)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.FAILED
    assert cmd.target_task_status == TaskStatus.QUEUED
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_run_claim_failed_circuit_breaker_to_failed(run_service, registry):
    """consecutive_failures > max_retries -> FAILED (no GAVE_UP outcome)."""
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="llm error",
    ))
    task = _queued_task(max_retries=3, consecutive_failures=3)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    # Outcome stays FAILED (no GAVE_UP); target is FAILED (breaker tripped)
    assert cmd.outcome == TaskRunOutcome.FAILED
    assert cmd.target_task_status == TaskStatus.FAILED
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_run_claim_timeout_to_expired(run_service, registry):
    """asyncio.wait_for timeout -> TIMED_OUT -> task EXPIRED."""
    run_service.executor = FakeExecutor(delay=10)
    run_service.lease_seconds = 2
    task = _queued_task(max_runtime_seconds=1)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.TIMED_OUT
    assert cmd.target_task_status == TaskStatus.EXPIRED
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.EXPIRED


@pytest.mark.asyncio
async def test_run_claim_crash_to_expired(run_service, registry):
    """Executor raises -> CRASHED -> task EXPIRED (user must retry)."""
    run_service.executor = CrashExecutor()
    task = _queued_task(max_retries=3, consecutive_failures=0)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.CRASHED
    assert cmd.target_task_status == TaskStatus.EXPIRED
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.EXPIRED


# ---------------------------------------------------------------------------
# finalize_propose tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finalize_propose_releases_claim_and_sets_waiting(
    run_service, registry, dispatcher,
):
    """finalize_propose: outcome=WAITING_APPROVAL, task WAITING_APPROVAL,
    claim released, worker cancelled."""
    task = _queued_task(id="t_prop")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    # Simulate active worker
    dispatcher._active[claim.run.id] = {
        "run_id": claim.run.id, "task_id": task.id,
        "worker_token": claim.task.worker_token,
    }
    await run_service.finalize_propose(
        task.id, claim.run.id, claim.run.claim_lock, "need user confirm",
    )
    assert len(registry.finish_calls) == 1
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.WAITING_APPROVAL
    assert cmd.target_task_status == TaskStatus.WAITING_APPROVAL
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.WAITING_APPROVAL
    # Claim released (unified cleanup path)
    assert final_task.claim_lock is None
    assert final_task.current_run_id is None
    # Worker cancelled (reclaimed)
    assert len(dispatcher.cancels) == 1


@pytest.mark.asyncio
async def test_finalize_propose_notifies(run_service, registry, notifier):
    """WAITING_APPROVAL triggers user-visible notification."""
    task = _queued_task(id="t_prop")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await run_service.finalize_propose(
        task.id, claim.run.id, claim.run.claim_lock, "need user confirm",
    )
    assert len(notifier.deliveries) == 1


# ---------------------------------------------------------------------------
# terminate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_with_worker(run_service, registry, dispatcher):
    """Terminate cancels in-process worker and finishes TERMINATED -> CANCELLED."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_term", title="term", status=TaskStatus.RUNNING,
        created_at=now, claim_lock="lock-1",
        claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now, version=1, max_retries=3,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_term", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1",
    )
    dispatcher._active[1] = {
        "run_id": 1, "task_id": "t_term", "worker_token": "wt_1",
    }
    result = await run_service.terminate("t_term")
    assert result["status"] == "terminated"
    assert len(dispatcher.cancels) == 1
    assert len(registry.finish_calls) == 1
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.TERMINATED
    assert cmd.target_task_status == TaskStatus.CANCELLED
    final_task = await registry.get_task("t_term")
    assert final_task.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_terminate_without_worker(run_service, registry, dispatcher):
    """No in-process handle -> terminate_requested event."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_term2", title="term", status=TaskStatus.RUNNING,
        created_at=now, claim_lock="lock-1",
        claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now, version=1, max_retries=3,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_term2", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1",
    )
    result = await run_service.terminate("t_term2")
    assert result["status"] == "terminate_requested"


@pytest.mark.asyncio
async def test_terminate_not_running(run_service, registry):
    task = _queued_task()
    await registry.create_task(task)
    result = await run_service.terminate(task.id)
    assert result["status"] == "not_running"


# ---------------------------------------------------------------------------
# Stale recovery tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_stale_to_expired(run_service, registry):
    """RUNNING task with expired lease -> EXPIRED (not RECLAIMED, not TODO)."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_stale", title="stale", status=TaskStatus.RUNNING,
        created_at=now - timedelta(hours=1),
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
    assert registry.recover_calls[0].outcome == TaskRunOutcome.EXPIRED
    final_task = await registry.get_task("t_stale")
    assert final_task.status == TaskStatus.EXPIRED


@pytest.mark.asyncio
async def test_recover_stale_skips_valid_lease(run_service, registry, dispatcher):
    """RUNNING task with valid lease and active worker -> not recovered."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_active", title="active", status=TaskStatus.RUNNING,
        created_at=now - timedelta(minutes=5),
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
    dispatcher._active[1] = {"run_id": 1, "task_id": "t_active"}
    result = await run_service.dispatch_once()
    assert result["recovered_stale"] == 0


@pytest.mark.asyncio
async def test_recover_crashed_to_expired(run_service, registry, dispatcher):
    """In-process worker that crashed -> CRASHED -> task EXPIRED."""
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_crash", title="crash", status=TaskStatus.RUNNING,
        created_at=now - timedelta(minutes=5),
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
    final_task = await registry.get_task("t_crash")
    assert final_task.status == TaskStatus.EXPIRED


# ---------------------------------------------------------------------------
# Spawn failure tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_failure_records_spawn_failed(run_service, registry, dispatcher):
    dispatcher.spawn_raises = RuntimeError("spawn error")
    await registry.create_task(_queued_task(id="t_1"))
    await run_service.dispatch_once()
    assert len(registry.finish_calls) == 1
    cmd = registry.finish_calls[0]
    assert cmd.outcome == TaskRunOutcome.SPAWN_FAILED


# ---------------------------------------------------------------------------
# Notify tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_on_completed(run_service, registry, notifier):
    task = _queued_task()
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    assert len(notifier.deliveries) == 1


@pytest.mark.asyncio
async def test_notify_not_on_auto_retry(run_service, registry, notifier):
    """Retryable FAILED -> QUEUED (auto-retry) does not notify."""
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="retryable",
    ))
    task = _queued_task(max_retries=3, consecutive_failures=0)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    assert len(notifier.deliveries) == 0


@pytest.mark.asyncio
async def test_notify_on_circuit_breaker_failed(run_service, registry, notifier):
    """FAILED with breaker tripped -> FAILED (terminal) -> notify."""
    run_service.executor = FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.FAILED, error="final failure",
    ))
    task = _queued_task(max_retries=0, consecutive_failures=1)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    assert len(notifier.deliveries) == 1


@pytest.mark.asyncio
async def test_notify_on_expired(run_service, registry, notifier):
    """EXPIRED triggers notification (user must retry)."""
    run_service.executor = FakeExecutor(delay=10)
    run_service.lease_seconds = 2
    task = _queued_task(max_runtime_seconds=1)
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await _run_claim_with_spawn(run_service, task, claim)
    assert len(notifier.deliveries) == 1


@pytest.mark.asyncio
async def test_notify_on_terminated(run_service, registry, notifier, dispatcher):
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_term", title="term", status=TaskStatus.RUNNING,
        created_at=now, claim_lock="lock-1",
        claim_expires=now + timedelta(minutes=10),
        current_run_id=1, worker_token="wt_1",
        last_heartbeat_at=now, version=1, max_retries=3,
    )
    await registry.create_task(task)
    registry._runs[1] = TaskRun(
        id=1, task_id="t_term", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1",
    )
    dispatcher._active[1] = {
        "run_id": 1, "task_id": "t_term", "worker_token": "wt_1",
    }
    await run_service.terminate("t_term")
    assert len(notifier.deliveries) == 1


# ---------------------------------------------------------------------------
# Late worker / CAS conflict tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_late_worker_cas_conflict(run_service, registry):
    """Late worker finish_run CAS conflict -> logged, not overwritten."""
    task = _queued_task()
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    # Simulate the task already being finalized by a newer run
    finalized = dc_replace(
        claim.task, status=TaskStatus.SUCCEEDED, claim_lock=None,
        current_run_id=None, version=claim.task.version + 1,
    )
    registry._tasks[task.id] = finalized
    await _run_claim_with_spawn(run_service, task, claim)
    # CAS conflict caught; task stays SUCCEEDED (not overwritten)
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Outcome -> target_task_status mapping unit tests
# ---------------------------------------------------------------------------


def test_decide_target_status_completed():
    svc = TaskRunService(
        registry=None, dispatcher=None, executor=None,
        policy=TaskPolicy(), notifier=None,
    )
    task = Task(id="t", title="x", status=TaskStatus.RUNNING, max_retries=3)
    assert svc._decide_target_status(task, TaskRunOutcome.COMPLETED) is TaskStatus.SUCCEEDED


def test_decide_target_status_waiting_approval():
    svc = TaskRunService(
        registry=None, dispatcher=None, executor=None,
        policy=TaskPolicy(), notifier=None,
    )
    task = Task(id="t", title="x", status=TaskStatus.RUNNING, max_retries=3)
    assert (
        svc._decide_target_status(task, TaskRunOutcome.WAITING_APPROVAL)
        is TaskStatus.WAITING_APPROVAL
    )


def test_decide_target_status_terminated():
    svc = TaskRunService(
        registry=None, dispatcher=None, executor=None,
        policy=TaskPolicy(), notifier=None,
    )
    task = Task(id="t", title="x", status=TaskStatus.RUNNING, max_retries=3)
    assert svc._decide_target_status(task, TaskRunOutcome.TERMINATED) is TaskStatus.CANCELLED


def test_decide_target_status_crashed_timed_out_expired():
    svc = TaskRunService(
        registry=None, dispatcher=None, executor=None,
        policy=TaskPolicy(), notifier=None,
    )
    task = Task(id="t", title="x", status=TaskStatus.RUNNING, max_retries=3)
    assert svc._decide_target_status(task, TaskRunOutcome.CRASHED) is TaskStatus.EXPIRED
    assert svc._decide_target_status(task, TaskRunOutcome.TIMED_OUT) is TaskStatus.EXPIRED
    assert svc._decide_target_status(task, TaskRunOutcome.EXPIRED) is TaskStatus.EXPIRED


def test_decide_target_status_failed_retry_then_breaker():
    svc = TaskRunService(
        registry=None, dispatcher=None, executor=None,
        policy=TaskPolicy(), notifier=None,
    )
    # Under max_retries -> QUEUED (auto-retry)
    task = Task(
        id="t", title="x", status=TaskStatus.RUNNING,
        max_retries=3, consecutive_failures=0,
    )
    assert svc._decide_target_status(task, TaskRunOutcome.FAILED) is TaskStatus.QUEUED
    # Over max_retries -> FAILED (breaker)
    task_breaker = Task(
        id="t", title="x", status=TaskStatus.RUNNING,
        max_retries=3, consecutive_failures=3,
    )
    assert svc._decide_target_status(task_breaker, TaskRunOutcome.FAILED) is TaskStatus.FAILED


def test_decide_target_status_spawn_failed_retry_then_breaker():
    svc = TaskRunService(
        registry=None, dispatcher=None, executor=None,
        policy=TaskPolicy(), notifier=None,
    )
    task = Task(
        id="t", title="x", status=TaskStatus.RUNNING,
        max_retries=3, consecutive_failures=0,
    )
    assert svc._decide_target_status(task, TaskRunOutcome.SPAWN_FAILED) is TaskStatus.QUEUED
    task_breaker = Task(
        id="t", title="x", status=TaskStatus.RUNNING,
        max_retries=0, consecutive_failures=1,
    )
    assert svc._decide_target_status(task_breaker, TaskRunOutcome.SPAWN_FAILED) is TaskStatus.FAILED


# ---------------------------------------------------------------------------
# TaskArtifact normalization + register callback tests (T10)
# ---------------------------------------------------------------------------


class _RecordingArtifactCallback:
    """Records artifact_register_callback invocations."""

    def __init__(
        self,
        fail_on_ordinal: int | None = None,
        exc: Exception | None = None,
    ):
        self.calls: list[tuple[TaskArtifact, str, int, int]] = []
        self.fail_on_ordinal = fail_on_ordinal
        self.exc = exc or RuntimeError("callback boom")

    async def __call__(
        self, artifact: TaskArtifact, task_id: str, run_id: int, ordinal: int,
    ) -> None:
        self.calls.append((artifact, task_id, run_id, ordinal))
        if self.fail_on_ordinal is not None and ordinal == self.fail_on_ordinal:
            raise self.exc


async def _fake_normalizer(raw: dict[str, Any], task: Task) -> TaskArtifact | None:
    """Controlled normalizer that fills missing fields with deterministic values."""
    return TaskArtifact(
        type=raw["type"],
        name=raw["name"],
        mime=raw.get("mime") or "application/octet-stream",
        size=raw.get("size") if raw.get("size") is not None else 42,
        storage_ref=raw["storage_ref"],
        source_task_id=raw["source_task_id"],
        summary=raw.get("summary") or "auto-summary",
        checksum=raw.get("checksum") or "auto-checksum",
    )


async def _selective_normalizer(raw: dict[str, Any], task: Task) -> TaskArtifact | None:
    """Returns None for 'bad' storage_refs; otherwise fills missing fields."""
    if "bad" in raw.get("storage_ref", ""):
        return None
    return TaskArtifact(
        type=raw["type"],
        name=raw["name"],
        mime=raw.get("mime") or "application/octet-stream",
        size=raw.get("size") if raw.get("size") is not None else 42,
        storage_ref=raw["storage_ref"],
        source_task_id=raw["source_task_id"],
        summary=raw.get("summary") or "auto-summary",
        checksum=raw.get("checksum") or "auto-checksum",
    )


def _artifact_executor(artifacts: tuple[dict[str, Any], ...]) -> FakeExecutor:
    """Build a FakeExecutor whose TaskAgentResult carries the given artifact dicts."""
    return FakeExecutor(TaskAgentResult(
        status=TaskRunOutcome.COMPLETED, output="done", artifacts=artifacts,
    ))


# --- Normalization tests ---


@pytest.mark.asyncio
async def test_artifact_normalization_required_fields_skipped(registry, dispatcher):
    """Missing type/name/storage_ref -> artifact skipped."""
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
            {"name": "r2", "storage_ref": "ws:r2.pdf"},  # missing type
            {"type": "report", "storage_ref": "ws:r3.pdf"},  # missing name
            {"type": "report", "name": "r4"},  # missing storage_ref
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
    )
    task = _queued_task(id="t_norm")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    assert len(cmd.artifacts) == 1
    assert cmd.artifacts[0].name == "r1"


@pytest.mark.asyncio
async def test_artifact_normalization_source_task_id_force_overwritten(registry, dispatcher):
    """source_task_id is always overwritten with the current Task's id."""
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {
                "type": "report", "name": "r1", "storage_ref": "ws:r1.pdf",
                "source_task_id": "evil-task-id",
            },
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
    )
    task = _queued_task(id="t_own")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    assert len(cmd.artifacts) == 1
    assert cmd.artifacts[0].source_task_id == "t_own"
    assert cmd.artifacts[0].source_task_id != "evil-task-id"


@pytest.mark.asyncio
async def test_artifact_normalization_fills_missing_fields(registry, dispatcher):
    """Missing mime/size/summary/checksum filled by injected normalizer."""
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {
                "type": "report", "name": "r1", "storage_ref": "ws:r1.pdf",
                "source_task_id": "ignored",
            },
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
    )
    task = _queued_task(id="t_fill")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    assert len(cmd.artifacts) == 1
    art = cmd.artifacts[0]
    assert art.mime == "application/octet-stream"
    assert art.size == 42
    assert art.summary == "auto-summary"
    assert art.checksum == "auto-checksum"
    assert art.source_task_id == "t_fill"


@pytest.mark.asyncio
async def test_artifact_normalization_preserves_existing_fields(registry, dispatcher):
    """Fields already present in the dict are preserved by the normalizer."""
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {
                "type": "report", "name": "r1", "storage_ref": "ws:r1.pdf",
                "mime": "text/plain", "size": 100,
                "summary": "my-summary", "checksum": "abc123",
                "source_task_id": "ignored",
            },
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
    )
    task = _queued_task(id="t_pres")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    art = cmd.artifacts[0]
    assert art.mime == "text/plain"
    assert art.size == 100
    assert art.summary == "my-summary"
    assert art.checksum == "abc123"


@pytest.mark.asyncio
async def test_artifact_normalization_skips_unreadable_ref(registry, dispatcher):
    """Normalizer returns None (unreadable) -> artifact skipped."""
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
            {"type": "report", "name": "r2", "storage_ref": "ws:bad.pdf"},
            {"type": "report", "name": "r3", "storage_ref": "ws:r3.pdf"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_selective_normalizer,
    )
    task = _queued_task(id="t_skip")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    assert len(cmd.artifacts) == 2
    assert cmd.artifacts[0].name == "r1"
    assert cmd.artifacts[1].name == "r3"


@pytest.mark.asyncio
async def test_artifact_normalization_passes_task_artifact_tuple(registry, dispatcher):
    """FinishRunCommand.artifacts is tuple[TaskArtifact, ...], not dict."""
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
    )
    task = _queued_task(id="t_tuple")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    assert isinstance(cmd.artifacts, tuple)
    assert all(isinstance(a, TaskArtifact) for a in cmd.artifacts)
    assert not any(isinstance(a, dict) for a in cmd.artifacts)


@pytest.mark.asyncio
async def test_artifact_normalization_no_normalizer_uses_defaults(registry, dispatcher):
    """No normalizer injected -> missing fields use safe defaults."""
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
        )),
        policy=TaskPolicy(),
    )
    task = _queued_task(id="t_def")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    assert len(cmd.artifacts) == 1
    art = cmd.artifacts[0]
    assert isinstance(art, TaskArtifact)
    assert art.mime == ""
    assert art.size == 0
    assert art.summary == ""
    assert art.checksum == ""
    assert art.source_task_id == "t_def"


@pytest.mark.asyncio
async def test_artifact_normalization_normalizer_exception_skips(registry, dispatcher):
    """Normalizer raising -> artifact skipped with warning."""

    async def bad_normalizer(raw: dict[str, Any], task: Task) -> TaskArtifact | None:
        raise OSError("disk error")

    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "a", "name": "n1", "storage_ref": "ws:1"},
            {"type": "b", "name": "n2", "storage_ref": "ws:2"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=bad_normalizer,
    )
    task = _queued_task(id="t_exc")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    cmd = registry.finish_calls[0]
    assert len(cmd.artifacts) == 0


# --- Callback ordering tests ---


@pytest.mark.asyncio
async def test_artifact_callback_called_after_cas_success(registry, dispatcher):
    """Callback called only after registry.finish_run returns FinishRunResult."""
    callback = _RecordingArtifactCallback()
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_cb")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    assert len(callback.calls) == 1
    art, task_id, run_id, ordinal = callback.calls[0]
    assert isinstance(art, TaskArtifact)
    assert task_id == "t_cb"
    assert run_id == claim.run.id
    assert ordinal == 0


@pytest.mark.asyncio
async def test_artifact_callback_called_in_ordinal_order(registry, dispatcher):
    """Multiple artifacts -> callback called in stable ordinal order."""
    callback = _RecordingArtifactCallback()
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "a", "name": "n1", "storage_ref": "ws:1"},
            {"type": "b", "name": "n2", "storage_ref": "ws:2"},
            {"type": "c", "name": "n3", "storage_ref": "ws:3"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_ord")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    assert len(callback.calls) == 3
    assert [c[3] for c in callback.calls] == [0, 1, 2]
    assert [c[0].name for c in callback.calls] == ["n1", "n2", "n3"]


@pytest.mark.asyncio
async def test_artifact_callback_not_called_on_cas_conflict(registry, dispatcher):
    """CAS conflict -> callback NOT called."""
    callback = _RecordingArtifactCallback()
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_conf")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    # Pre-finalize the task so CAS conflicts
    finalized = dc_replace(
        claim.task, status=TaskStatus.SUCCEEDED, claim_lock=None,
        current_run_id=None, version=claim.task.version + 1,
    )
    registry._tasks[task.id] = finalized
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    assert len(callback.calls) == 0


@pytest.mark.asyncio
async def test_artifact_callback_not_called_on_not_found(registry, dispatcher):
    """Task/run not found -> callback NOT called."""
    callback = _RecordingArtifactCallback()
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_nf")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    # Remove the run so finish_run raises TaskNotFoundError
    del registry._runs[claim.run.id]
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    assert len(callback.calls) == 0


@pytest.mark.asyncio
async def test_artifact_callback_not_called_on_repeat_finish(registry, dispatcher):
    """Repeat finish (same run finalized twice) -> callback NOT called on 2nd."""
    callback = _RecordingArtifactCallback()
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_rep")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    # First finish succeeds
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    assert len(callback.calls) == 1
    callback.calls.clear()
    # Second finish (repeat) -> CAS conflict -> no callback
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    assert len(callback.calls) == 0


@pytest.mark.asyncio
async def test_artifact_callback_single_failure_continues(registry, dispatcher):
    """Single callback failure -> other callbacks still called."""
    callback = _RecordingArtifactCallback(fail_on_ordinal=1)
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "a", "name": "n1", "storage_ref": "ws:1"},
            {"type": "b", "name": "n2", "storage_ref": "ws:2"},
            {"type": "c", "name": "n3", "storage_ref": "ws:3"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_fail")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    # All 3 callbacks attempted; ordinal 1 failed but 0 and 2 succeeded
    assert len(callback.calls) == 3
    assert [c[3] for c in callback.calls] == [0, 1, 2]


@pytest.mark.asyncio
async def test_artifact_callback_failure_does_not_affect_task(registry, dispatcher):
    """Callback failure does not roll back CAS or affect the completed Task."""
    callback = _RecordingArtifactCallback(fail_on_ordinal=0)
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "a", "name": "n1", "storage_ref": "ws:1"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_task")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    # Task still SUCCEEDED despite callback failure
    final_task = await registry.get_task(task.id)
    assert final_task.status == TaskStatus.SUCCEEDED
    assert len(registry.finish_calls) == 1


@pytest.mark.asyncio
async def test_artifact_callback_warning_no_content_paths(registry, dispatcher, caplog):
    """Callback failure warning logs only safe fields (no content/paths)."""
    secret_content = "SECRET_FILE_CONTENT_SHOULD_NOT_LEAK"
    callback = _RecordingArtifactCallback(
        fail_on_ordinal=0,
        exc=RuntimeError(f"boom {secret_content} /abs/secret/path"),
    )
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "report", "name": "r1", "storage_ref": "ws:r1.pdf"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_fake_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_log")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    with caplog.at_level(logging.WARNING):
        await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    # Warning was logged
    assert any(
        "callback" in r.getMessage().lower() for r in caplog.records
    )
    # Secret content and absolute paths did NOT leak into logs
    all_log_text = " ".join(r.getMessage() for r in caplog.records)
    assert secret_content not in all_log_text
    assert "/abs/secret/path" not in all_log_text


@pytest.mark.asyncio
async def test_artifact_callback_skips_normalized_out_artifacts(registry, dispatcher):
    """Artifacts skipped during normalization -> callback not called for them."""
    callback = _RecordingArtifactCallback()
    svc = TaskRunService(
        registry=registry, dispatcher=dispatcher,
        executor=_artifact_executor((
            {"type": "a", "name": "n1", "storage_ref": "ws:1"},
            {"type": "b", "name": "n2", "storage_ref": "ws:bad"},  # skipped
            {"type": "c", "name": "n3", "storage_ref": "ws:3"},
        )),
        policy=TaskPolicy(),
        artifact_normalizer=_selective_normalizer,
        artifact_register_callback=callback,
    )
    task = _queued_task(id="t_skip_cb")
    await registry.create_task(task)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    await svc.run_claim(claim.task, claim.run.id, claim.run.claim_lock)
    # Only 2 callbacks (n1 and n3; n2 skipped during normalization)
    assert len(callback.calls) == 2
    assert callback.calls[0][0].name == "n1"
    assert callback.calls[1][0].name == "n3"
    # Ordinals are 0 and 1 (re-indexed after normalization)
    assert callback.calls[0][3] == 0
    assert callback.calls[1][3] == 1
