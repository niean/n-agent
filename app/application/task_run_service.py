"""T14: TaskRunService -- dispatch + CAS termination + recovery.

The SOLE run terminator. It decides ``target_task_status`` via TaskPolicy
and passes it through ``FinishRunCommand.target_task_status``. The Registry
executes the CAS finalize (does not decide status).

Key flows:
  - ``dispatch_once`` (fixed order): recover finished in-process workers ->
    recover stale executions -> recompute_ready -> select READY+assignee by
    priority desc / created_at asc / id asc -> within task_max_concurrency
    claim+spawn each.
  - ``claim_and_spawn``: registry.claim_task -> dispatcher.spawn
  - ``run_claim``: asyncio.wait_for(executor.run, max_runtime) -> ONE-SHOT
    finish_run CAS. Spawn failure -> SPAWN_FAILED + retry rule.
  - ``recover_crashed_workers``: RUNNING + in-process worker done with
    exception -> CRASHED (preserve attribution before release lease).
  - ``recover_stale_executions``: claim TTL expired orphans -> RECLAIMED.
  - ``terminate``: cancel in-process worker + converge + TERMINATED.
  - ``notify``: terminal_event_id idempotent; only terminal states deliver.

TaskPolicy decides target_task_status:
  - COMPLETED -> DONE
  - BLOCKED -> BLOCKED
  - Retryable (FAILED/CRASHED/TIMED_OUT/SPAWN_FAILED/RECLAIMED):
    project consecutive_failures+1, evaluate RUNNING->TODO. If DENY
    (circuit breaker) -> GAVE_UP, target=BLOCKED. If ALLOW -> target=TODO.
  - TERMINATED -> BLOCKED
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from app.domain.policy import PolicyOutcome
from app.domain.task import (
    BlockKind,
    ClaimResult,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
    TaskClaimError,
    TaskConflictError,
    TaskNotFoundError,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStatus,
)
from app.domain.task_policy import TaskPolicy, TaskPolicyRequest

logger = logging.getLogger(__name__)


# Terminal outcomes that trigger notification delivery (spec)
_NOTIFIED_OUTCOMES: frozenset[TaskRunOutcome] = frozenset({
    TaskRunOutcome.COMPLETED,
    TaskRunOutcome.BLOCKED,
    TaskRunOutcome.GAVE_UP,
    TaskRunOutcome.CRASHED,
    TaskRunOutcome.TIMED_OUT,
    TaskRunOutcome.TERMINATED,
})

# Retryable outcomes (don't notify, go to TODO for retry)
_RETRYABLE_OUTCOMES: frozenset[TaskRunOutcome] = frozenset({
    TaskRunOutcome.FAILED,
    TaskRunOutcome.CRASHED,
    TaskRunOutcome.TIMED_OUT,
    TaskRunOutcome.SPAWN_FAILED,
    TaskRunOutcome.RECLAIMED,
})


class TaskRunService:
    """Dispatch loop coordinator and sole run terminator.

    Injection:
      - ``registry``: TaskRegistry (Domain port)
      - ``dispatcher``: TaskDispatcher (Domain port -- TaskRunner in prod)
      - ``executor``: TaskAgentExecutor
      - ``policy``: TaskPolicy (14th domain Policy)
      - ``notifier``: TaskNotifier (Domain port)
      - ``lease_seconds``: initial claim lease
      - ``heartbeat_timeout_seconds``: stale heartbeat threshold
      - ``max_runtime_seconds``: default hard timeout per run
      - ``max_concurrency``: global concurrent worker limit
    """

    def __init__(
        self,
        registry: Any,
        dispatcher: Any,
        executor: Any,
        policy: TaskPolicy,
        notifier: Any | None = None,
        lease_seconds: int = 900,
        heartbeat_timeout_seconds: int = 300,
        max_runtime_seconds: int = 3600,
        max_concurrency: int = 4,
    ):
        self.registry = registry
        self.dispatcher = dispatcher
        self.executor = executor
        self.policy = policy
        self.notifier = notifier
        self.lease_seconds = lease_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.max_runtime_seconds = max_runtime_seconds
        self.max_concurrency = max_concurrency

    # ------------------------------------------------------------------
    # dispatch_once (fixed order)
    # ------------------------------------------------------------------

    async def dispatch_once(self) -> dict[str, Any]:
        """Single dispatch tick. Fixed order:
        1. Recover finished in-process workers (crash detection)
        2. Recover stale executions (lease/heartbeat expired)
        3. Recompute ready (dependency graph)
        4. Select READY+assignee by priority desc / created_at asc / id asc
        5. Within task_max_concurrency, claim+spawn each

        Single candidate failure does not block others. Exceptions are
        caught and logged.
        """
        now = datetime.now(timezone.utc)
        recovered_crashed = await self._recover_crashed_workers(now)
        recovered_stale = await self._recover_stale_executions(now)
        promoted = await self.registry.recompute_ready()

        ready_tasks = await self.registry.list_ready(limit=100)
        # Sort by priority desc, created_at asc, id asc
        ready_sorted = sorted(
            ready_tasks,
            key=lambda t: (-t.priority, t.created_at or now, t.id),
        )

        # Check current concurrency
        active_count = await self._active_worker_count()
        spawned = 0
        spawn_failures = 0
        for task in ready_sorted:
            if active_count + spawned >= self.max_concurrency:
                break
            try:
                result = await self._claim_and_spawn(task)
                if result is not None:
                    spawned += 1
                else:
                    spawn_failures += 1
            except Exception as exc:
                logger.warning(
                    "claim+spawn failed for task %s: %s", task.id, exc
                )
                spawn_failures += 1

        return {
            "recovered_crashed": recovered_crashed,
            "recovered_stale": recovered_stale,
            "promoted": list(promoted),
            "spawned": spawned,
            "spawn_failures": spawn_failures,
        }

    # ------------------------------------------------------------------
    # claim_and_spawn
    # ------------------------------------------------------------------

    async def _claim_and_spawn(self, task: Task) -> str | None:
        """Atomically claim a READY task and spawn a worker.

        Returns the worker_token, or None if claim failed (already claimed
        by another tick / status changed).
        """
        claim_lock = f"cl-{uuid4().hex[:12]}"
        claim = await self.registry.claim_task(
            task.id, claim_lock, self.lease_seconds
        )
        if claim is None:
            return None

        try:
            worker_token = await self.dispatcher.spawn(
                claim.task, claim.run.id, claim_lock
            )
            return worker_token
        except Exception as exc:
            # Spawn failed -- record SPAWN_FAILED and apply retry rule
            logger.warning(
                "spawn failed for task %s run %s: %s",
                task.id, claim.run.id, exc,
            )
            await self._handle_spawn_failure(claim, str(exc))
            return None

    async def _handle_spawn_failure(self, claim: ClaimResult, error: str) -> None:
        """Record SPAWN_FAILED outcome and apply circuit breaker."""
        final_outcome = self._apply_circuit_breaker(claim.task, TaskRunOutcome.SPAWN_FAILED)
        target_status = await self._decide_target_status(claim.task, final_outcome)
        try:
            result = await self.registry.finish_run(FinishRunCommand(
                task_id=claim.task.id,
                run_id=claim.run.id,
                claim_lock=claim.run.claim_lock or "",
                outcome=final_outcome,
                error=error,
                target_task_status=target_status,
            ))
            await self._notify_if_terminal(result)
        except (TaskConflictError, TaskNotFoundError) as exc:
            logger.warning(
                "spawn_failed finish_run conflict for task %s: %s",
                claim.task.id, exc,
            )

    # ------------------------------------------------------------------
    # run_claim (called by dispatcher.spawn)
    # ------------------------------------------------------------------

    async def run_claim(
        self,
        task: Task,
        run_id: int,
        claim_lock: str,
    ) -> None:
        """Execute a single claim with hard timeout and CAS finalize.

        This is the worker entry point called by the dispatcher's spawn.
        The asyncio.wait_for wraps executor.run with a hard timeout that
        must be less than the lease. On any outcome (success, timeout,
        error), the service performs ONE-SHOT finish_run CAS.

        All exceptions are caught so the asyncio.Task never propagates.
        """
        max_runtime = task.max_runtime_seconds or self.max_runtime_seconds
        # Hard timeout must be < lease
        timeout = min(max_runtime, self.lease_seconds - 1)

        agent_result = None
        try:
            agent_result = await asyncio.wait_for(
                self._execute_task(task, run_id, claim_lock),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            agent_result = TaskAgentResult(
                status=TaskRunOutcome.TIMED_OUT,
                error=f"execution timed out after {timeout}s",
            )
        except Exception as exc:
            logger.exception(
                "task execution crashed: task=%s run=%s", task.id, run_id
            )
            agent_result = TaskAgentResult(
                status=TaskRunOutcome.CRASHED,
                error=f"execution error: {exc}",
            )

        await self._finalize_run(task, run_id, claim_lock, agent_result)

    async def _execute_task(self, task: Task, run_id: int, claim_lock: str):
        """Run the executor, handling goal_mode vs single-turn."""
        if task.goal_mode:
            return await self.executor.run_goal_loop(task, run_id, claim_lock)
        return await self.executor.run(task, run_id, claim_lock)

    async def _finalize_run(
        self,
        task: Task,
        run_id: int,
        claim_lock: str,
        agent_result: Any,
    ) -> None:
        """ONE-SHOT CAS finalize. Decides target_task_status via TaskPolicy."""
        status = getattr(agent_result, "status", TaskRunOutcome.FAILED)
        output = getattr(agent_result, "output", None)
        error = getattr(agent_result, "error", None)
        metadata = getattr(agent_result, "metadata", {}) or {}
        artifacts = getattr(agent_result, "artifacts", ()) or ()

        # Apply circuit breaker: if retryable outcome and circuit breaker
        # trips, change outcome to GAVE_UP (so notification fires).
        final_outcome = self._apply_circuit_breaker(task, status)
        target_status = await self._decide_target_status(task, final_outcome)

        # Build artifacts tuple for FinishRunCommand
        artifact_dicts = tuple(
            a if isinstance(a, dict) else {"type": "unknown", "name": str(a), "storage_ref": ""}
            for a in artifacts
        )

        try:
            result = await self.registry.finish_run(FinishRunCommand(
                task_id=task.id,
                run_id=run_id,
                claim_lock=claim_lock,
                outcome=final_outcome,
                summary=output or error or "",
                metadata=dict(metadata),
                artifacts=artifact_dicts,
                target_task_status=target_status,
                error=error,
            ))
            await self._notify_if_terminal(result)
        except TaskConflictError as exc:
            # Late worker or duplicate finish -- audit only, don't overwrite
            logger.info(
                "finish_run CAS conflict (late worker): task=%s run=%s: %s",
                task.id, run_id, exc,
            )
        except TaskNotFoundError as exc:
            logger.warning(
                "finish_run task/run not found: task=%s run=%s: %s",
                task.id, run_id, exc,
            )

    def _apply_circuit_breaker(
        self, task: Task, outcome: TaskRunOutcome
    ) -> TaskRunOutcome:
        """If the outcome is retryable and the circuit breaker trips,
        change the outcome to GAVE_UP so that:
          1. The task goes to BLOCKED (not TODO retry).
          2. Notification fires (GAVE_UP is in _NOTIFIED_OUTCOMES).
        """
        if outcome not in _RETRYABLE_OUTCOMES:
            return outcome
        projected_failures = task.consecutive_failures + 1
        if projected_failures > task.max_retries:
            return TaskRunOutcome.GAVE_UP
        return outcome

    # ------------------------------------------------------------------
    # TaskPolicy -> target_task_status decision
    # ------------------------------------------------------------------

    async def _decide_target_status(
        self,
        task: Task,
        outcome: TaskRunOutcome,
    ) -> TaskStatus:
        """Decide the target TaskStatus based on outcome and TaskPolicy.

        - COMPLETED -> DONE
        - BLOCKED -> BLOCKED
        - GAVE_UP -> BLOCKED
        - TERMINATED -> BLOCKED
        - Retryable (FAILED/CRASHED/TIMED_OUT/SPAWN_FAILED/RECLAIMED):
          Project consecutive_failures+1, evaluate RUNNING->TODO.
          If DENY (circuit breaker) -> GAVE_UP -> BLOCKED.
          If ALLOW -> TODO (retry).
        """
        if outcome == TaskRunOutcome.COMPLETED:
            return TaskStatus.DONE
        if outcome in (TaskRunOutcome.BLOCKED, TaskRunOutcome.GAVE_UP):
            return TaskStatus.BLOCKED
        if outcome == TaskRunOutcome.TERMINATED:
            return TaskStatus.BLOCKED

        # Retryable outcome: evaluate circuit breaker
        # Project the failure count AFTER this failure (current + 1)
        projected_failures = task.consecutive_failures + 1
        request = TaskPolicyRequest(
            current=TaskStatus.RUNNING,
            target=TaskStatus.TODO,
            block_kind=None,
            consecutive_failures=projected_failures,
            max_retries=task.max_retries,
            block_recurrences=task.block_recurrences,
        )
        decision = self.policy.evaluate(request)
        if decision is PolicyOutcome.DENY:
            # Circuit breaker trips -> GAVE_UP
            return TaskStatus.BLOCKED
        return TaskStatus.TODO

    # ------------------------------------------------------------------
    # recover_crashed_workers
    # ------------------------------------------------------------------

    async def _recover_crashed_workers(self, now: datetime) -> int:
        """Recover in-process workers that ended with exception.

        Only workers that the dispatcher tracks as done-with-exception are
        recovered here. The dispatcher's inspect() returns crashed workers;
        we finalize them as CRASHED.
        """
        if not hasattr(self.dispatcher, "get_crashed_workers"):
            return 0
        crashed = await self.dispatcher.get_crashed_workers()
        recovered = 0
        for entry in crashed:
            run_id = entry.get("run_id")
            task_id = entry.get("task_id")
            error = entry.get("error", "worker crashed")
            if run_id is None or task_id is None:
                continue
            try:
                task = await self.registry.get_task(task_id)
                if task is None or task.status != TaskStatus.RUNNING:
                    continue
                if task.current_run_id != run_id:
                    continue
                target = await self._decide_target_status(task, TaskRunOutcome.CRASHED)
                result = await self.registry.recover_run(RecoverRunCommand(
                    task_id=task_id,
                    run_id=run_id,
                    claim_lock=task.claim_lock or "",
                    outcome=TaskRunOutcome.CRASHED,
                    error=error,
                ))
                # recover_run doesn't take target_task_status; use the
                # default mapping (CRASHED -> TODO for retry, or BLOCKED
                # if circuit breaker). This is acceptable since the
                # registry's default maps retryable -> TODO.
                await self._notify_if_terminal(result)
                recovered += 1
            except (TaskConflictError, TaskNotFoundError) as exc:
                logger.warning(
                    "crash recovery failed for task %s run %s: %s",
                    task_id, run_id, exc,
                )
        return recovered

    # ------------------------------------------------------------------
    # recover_stale_executions
    # ------------------------------------------------------------------

    async def _recover_stale_executions(self, now: datetime) -> int:
        """Recover RUNNING tasks with expired leases or stale heartbeats.

        For each RUNNING task:
          - If claim_expires < now -> lease expired -> RECLAIMED
          - If last_heartbeat is stale (heartbeat_timeout) -> RECLAIMED
          - If no in-process worker handle -> only reclaim if lease expired
        """
        running_tasks = await self.registry.list_running()
        recovered = 0
        for task in running_tasks:
            if task.claim_lock is None or task.current_run_id is None:
                continue

            # Check if this task has an active in-process worker
            has_worker = await self._has_active_worker(task.current_run_id)
            if has_worker:
                # Worker is still running in-process; check heartbeat
                if task.is_stale(now, self.heartbeat_timeout_seconds):
                    # Heartbeat stale -- but worker still "active" in dispatcher.
                    # This could be a hung worker. Reclaim it.
                    pass
                else:
                    continue

            # Check lease expiry
            if task.claim_expires is not None:
                expires_aware = task.claim_expires
                if expires_aware.tzinfo is None:
                    expires_aware = expires_aware.replace(tzinfo=timezone.utc)
                if expires_aware > now:
                    # Lease still valid, don't reclaim
                    continue

            # Lease expired -> RECLAIMED
            try:
                target = await self._decide_target_status(task, TaskRunOutcome.RECLAIMED)
                result = await self.registry.recover_run(RecoverRunCommand(
                    task_id=task.id,
                    run_id=task.current_run_id,
                    claim_lock=task.claim_lock,
                    outcome=TaskRunOutcome.RECLAIMED,
                    error="lease expired or heartbeat stale",
                ))
                await self._notify_if_terminal(result)
                recovered += 1
            except (TaskConflictError, TaskNotFoundError) as exc:
                logger.info(
                    "stale recovery skipped for task %s: %s", task.id, exc
                )
        return recovered

    # ------------------------------------------------------------------
    # terminate
    # ------------------------------------------------------------------

    async def terminate(self, task_id: str, run_id: int | None = None) -> dict[str, Any]:
        """Cancel in-process worker and converge to TERMINATED.

        If the worker is in-process, cancel it and wait for convergence.
        If no handle (cross-process or restart orphan), write
        terminate_requested and wait for lease recovery.
        """
        task = await self.registry.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        if task.status != TaskStatus.RUNNING:
            return {"task_id": task_id, "status": "not_running"}

        target_run_id = run_id or task.current_run_id
        if target_run_id is None:
            return {"task_id": task_id, "status": "no_active_run"}

        # Check for an active in-process worker. worker_token being set does
        # NOT imply the worker is still live in-process (could be a restart
        # orphan with stale worker_token). Only cancel when truly active.
        cancelled = False
        has_active = await self._has_active_worker(target_run_id)
        if has_active and task.worker_token:
            try:
                cancelled = await self.dispatcher.cancel(task.worker_token)
            except Exception as exc:
                logger.warning(
                    "cancel worker failed for task %s: %s", task_id, exc
                )

        if cancelled:
            # Worker was in-process and cancelled -> TERMINATED
            target = await self._decide_target_status(task, TaskRunOutcome.TERMINATED)
            try:
                result = await self.registry.finish_run(FinishRunCommand(
                    task_id=task_id,
                    run_id=target_run_id,
                    claim_lock=task.claim_lock or "",
                    outcome=TaskRunOutcome.TERMINATED,
                    error="terminated by request",
                    target_task_status=target,
                ))
                await self._notify_if_terminal(result)
                return {"task_id": task_id, "status": "terminated"}
            except (TaskConflictError, TaskNotFoundError) as exc:
                logger.warning(
                    "terminate finish_run conflict for task %s: %s",
                    task_id, exc,
                )
                return {"task_id": task_id, "status": "conflict"}

        # No in-process handle -> write terminate_requested event
        await self.registry.append_event(
            task_id, "terminate_requested",
            {"run_id": target_run_id}, run_id=target_run_id,
        )
        return {"task_id": task_id, "status": "terminate_requested"}

    # ------------------------------------------------------------------
    # notify (idempotent by terminal_event_id)
    # ------------------------------------------------------------------

    async def _notify_if_terminal(self, result: FinishRunResult) -> None:
        """Deliver notification for terminal outcomes.

        Idempotent by terminal_event_id: the notifier checks
        task_notify_subs.last_terminal_event_id before delivering.
        Only _NOTIFIED_OUTCOMES trigger delivery; retryable FAILED/
        SPAWN_FAILED/RECLAIMED do not.
        """
        if self.notifier is None:
            return
        outcome = result.run.outcome
        if outcome not in _NOTIFIED_OUTCOMES:
            return
        try:
            await self.notifier.deliver(result.task, result.terminal_event)
        except Exception as exc:
            logger.warning(
                "notify delivery failed for task %s event %s: %s",
                result.task.id, result.terminal_event.id, exc,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _active_worker_count(self) -> int:
        """Count active in-process workers via dispatcher.inspect()."""
        try:
            snapshot = await self.dispatcher.inspect()
            active = snapshot.get("active", [])
            return len(active)
        except Exception:
            return 0

    async def _has_active_worker(self, run_id: int) -> bool:
        """Check if the dispatcher has an active worker for this run_id."""
        try:
            snapshot = await self.dispatcher.inspect()
            active = snapshot.get("active", [])
            return any(w.get("run_id") == run_id for w in active)
        except Exception:
            return False


# Import here to avoid circular import at module load
from app.application.task_agent_executor import TaskAgentResult  # noqa: E402
