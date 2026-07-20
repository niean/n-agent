"""T5: TaskRunService -- dispatch + unified run finalization (Manus-aligned).

The SOLE run terminator. It decides ``target_task_status`` via TaskPolicy
(circuit breaker) and passes it through ``FinishRunCommand.target_task_status``.
The Registry executes the CAS finalize (does not decide status).

Unified run-finalization path (``_finish``): every outcome -- including
``WAITING_APPROVAL`` (worker propose) and ``TERMINATED`` (user cancel) --
releases the claim (``claim_lock``/``claim_expires``/``current_run_id``),
reclaims the worker (``dispatcher.cancel``), writes the run outcome, and
appends a terminal event. No outcome bypasses this cleanup.

Outcome -> target_task_status mapping (spec Data Model):
  COMPLETED            -> SUCCEEDED
  WAITING_APPROVAL     -> WAITING_APPROVAL (claim released, worker reclaimed)
  TERMINATED           -> CANCELLED (user cancel RUNNING)
  ABORTED              -> FAILED (worker deliberate fast-fail via task_fail;
                          no retry, bypasses circuit breaker. 取消只认用户指令)
  EXPIRED              -> EXPIRED (stale/lease expired)
  CRASHED / TIMED_OUT  -> EXPIRED (worker died; user must retry)
  FAILED / SPAWN_FAILED -> if consecutive_failures > max_retries: FAILED;
                          else QUEUED (auto-retry; TaskPolicy circuit breaker)

Notification policy:
  - WAITING_APPROVAL / terminal statuses (SUCCEEDED/FAILED/CANCELLED/EXPIRED)
    trigger user-visible notification.
  - Auto-retry to QUEUED does NOT notify.

dispatch_once order (fixed):
  1. Recover finished in-process workers (crash detection -> CRASHED -> EXPIRED)
  2. Recover stale executions (lease/heartbeat expired -> EXPIRED)
  3. Query due QUEUED tasks (``list_queued_due``; no recompute_ready/list_ready)
  4. Within max_concurrency, claim+spawn each

Removed concepts: ``BlockKind``, ``TaskRunOutcome.BLOCKED/GAVE_UP/RECLAIMED``,
``TaskStatus.TODO``, ``block_kind``/``block_recurrences``, ``recompute_ready``,
``list_ready``. The new state machine has no BLOCKED state and no dependency
graph.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.application.task_session import task_execution_session_id
from app.domain.policy import PolicyOutcome
from app.domain.task import (
    ClaimResult,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
    TaskConflictError,
    TaskNotFoundError,
    TaskRunOutcome,
    TaskStatus,
)
from app.domain.task_policy import TaskPolicy, TaskPolicyRequest

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Notification policy
# ---------------------------------------------------------------------------

# Target task statuses that trigger user-visible notification. Auto-retry to
# QUEUED is intentionally excluded (no terminal notification).
_NOTIFIED_TARGET_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.SUCCEEDED,
    TaskStatus.WAITING_APPROVAL,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.EXPIRED,
})

# Retryable outcomes that may auto-retry to QUEUED (subject to the circuit
# breaker). CRASHED/TIMED_OUT are NOT here -- they always go to EXPIRED
# (user-driven retry only).
_RETRYABLE_OUTCOMES: frozenset[TaskRunOutcome] = frozenset({
    TaskRunOutcome.FAILED,
    TaskRunOutcome.SPAWN_FAILED,
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
        lifecycle_writer: Callable[[str, str], Awaitable[Any]] | None = None,
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
        self.lifecycle_writer = lifecycle_writer
        self.lease_seconds = lease_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.max_runtime_seconds = max_runtime_seconds
        self.max_concurrency = max_concurrency

    # ------------------------------------------------------------------
    # Lifecycle chat messages (best-effort, never blocks finalization)
    # ------------------------------------------------------------------

    async def _write_lifecycle(self, task: Task, content: str) -> None:
        """向执行会话 best-effort 写 ui.task_lifecycle system 消息。

        writer 为 None（未装配/降级）时跳过；写入异常仅 log.warning，不改变任务 CAS、
        worker 回收或飞书投递结果。会话不存在时 SessionService 抛 SessionNotFoundError
        -> 这里吞掉（会话已删，不复活）。
        """
        if self.lifecycle_writer is None:
            return
        session_id = task_execution_session_id(task)
        try:
            await self.lifecycle_writer(session_id, content)
        except Exception:
            logger.warning(
                "lifecycle write failed for task %s", task.id, exc_info=True,
            )

    def _lifecycle_text(
        self,
        task: Task,
        target_status: TaskStatus,
        summary: str | None = None,
        error: str | None = None,
    ) -> str | None:
        """终态/等待批准 -> 生命周期正文；QUEUED 等非通知态返回 None（不写）。"""
        title = task.title
        if target_status == TaskStatus.WAITING_APPROVAL:
            return f"[任务状态] 等待批准: {task.id} - {title} | 提案: {summary or ''}"
        if target_status == TaskStatus.SUCCEEDED:
            return f"[任务状态] 已完成: {task.id} - {title} | {summary or ''}"
        if target_status == TaskStatus.FAILED:
            return f"[任务状态] 已失败: {task.id} - {title} | {error or summary or ''}"
        if target_status == TaskStatus.CANCELLED:
            return f"[任务状态] 已取消: {task.id} - {title}"
        if target_status == TaskStatus.EXPIRED:
            return f"[任务状态] 已过期: {task.id} - {title}"
        # QUEUED（自动重试）等：不写
        return None

    async def _write_lifecycle_for_status(
        self,
        task: Task,
        target_status: TaskStatus,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        text = self._lifecycle_text(task, target_status, summary=summary, error=error)
        if text is None:
            return
        await self._write_lifecycle(task, text)

    # ------------------------------------------------------------------
    # dispatch_once (fixed order)
    # ------------------------------------------------------------------

    async def dispatch_once(self) -> dict[str, Any]:
        """Single dispatch tick. Fixed order:
        1. Recover finished in-process workers (crash detection -> EXPIRED)
        2. Recover stale executions (lease/heartbeat expired -> EXPIRED)
        3. Query due QUEUED tasks (``list_queued_due``)
        4. Within max_concurrency, claim+spawn each

        Single candidate failure does not block others. Exceptions are
        caught and logged. No ``recompute_ready``/``list_ready`` -- the
        new state machine has no READY state.
        """
        now = datetime.now(timezone.utc)
        recovered_crashed = await self._recover_crashed_workers(now)
        recovered_stale = await self._recover_stale_executions(now)

        queued_tasks = await self.registry.list_queued_due(now, limit=100)
        # Registry already orders by priority desc, created_at asc, id asc;
        # sort again defensively in case the port is a fake.
        queued_sorted = sorted(
            queued_tasks,
            key=lambda t: (-t.priority, t.created_at or now, t.id),
        )

        active_count = await self._active_worker_count()
        spawned = 0
        spawn_failures = 0
        for task in queued_sorted:
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
            "spawned": spawned,
            "spawn_failures": spawn_failures,
        }

    # ------------------------------------------------------------------
    # claim_and_spawn
    # ------------------------------------------------------------------

    async def _claim_and_spawn(self, task: Task) -> str | None:
        """Atomically claim a QUEUED task and spawn a worker.

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
            logger.warning(
                "spawn failed for task %s run %s: %s",
                task.id, claim.run.id, exc,
            )
            await self._handle_spawn_failure(claim, str(exc))
            return None

    async def _handle_spawn_failure(self, claim: ClaimResult, error: str) -> None:
        """Record SPAWN_FAILED outcome and apply circuit breaker."""
        await self._finish(
            task=claim.task,
            run_id=claim.run.id,
            claim_lock=claim.run.claim_lock or "",
            outcome=TaskRunOutcome.SPAWN_FAILED,
            error=error,
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
        error), the service performs ONE-SHOT finish_run CAS via the
        unified ``_finish`` path.

        All exceptions are caught so the asyncio.Task never propagates.
        """
        max_runtime = task.max_runtime_seconds or self.max_runtime_seconds
        # Hard timeout must be < lease
        timeout = min(max_runtime, self.lease_seconds - 1)

        # 生命周期：worker 起始（best-effort，不阻断执行；spawn 失败走 _handle_spawn_failure，
        # 未进 run_claim，不写"开始运行"）
        await self._write_lifecycle(task, f"[任务状态] 开始运行: {task.id} - {task.title}")

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

    # ------------------------------------------------------------------
    # Unified run finalization (the SOLE cleanup path)
    # ------------------------------------------------------------------

    async def _finalize_run(
        self,
        task: Task,
        run_id: int,
        claim_lock: str,
        agent_result: Any,
    ) -> None:
        """Worker-driven finalization: extract outcome from agent_result
        and delegate to ``_finish``.

        Exceptions from ``_finish`` are already handled (CAS conflict /
        not found are logged, not propagated).
        """
        status = getattr(agent_result, "status", TaskRunOutcome.FAILED)
        output = getattr(agent_result, "output", None)
        error = getattr(agent_result, "error", None)
        metadata = getattr(agent_result, "metadata", {}) or {}
        artifacts = getattr(agent_result, "artifacts", ()) or ()

        await self._finish(
            task=task,
            run_id=run_id,
            claim_lock=claim_lock,
            outcome=status,
            summary=output or error or "",
            error=error,
            metadata=dict(metadata),
            artifacts=tuple(
                a if isinstance(a, dict) else {
                    "type": "unknown", "name": str(a), "storage_ref": "",
                }
                for a in artifacts
            ),
        )

    async def _finish(
        self,
        task: Task,
        run_id: int,
        claim_lock: str,
        outcome: TaskRunOutcome,
        summary: str | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        artifacts: tuple[dict[str, Any], ...] = (),
    ) -> FinishRunResult | None:
        """Unified CAS finalize -- the SOLE run cleanup path.

        Decides ``target_task_status`` via TaskPolicy (circuit breaker for
        retryable outcomes), calls ``registry.finish_run`` with the target
        status (so the Registry does not guess), and delivers notification
        for terminal / WAITING_APPROVAL targets.

        Returns the FinishRunResult, or None if a late-worker CAS conflict
        or missing task/run was logged and swallowed.
        """
        target_status = self._decide_target_status(task, outcome)

        try:
            result = await self.registry.finish_run(FinishRunCommand(
                task_id=task.id,
                run_id=run_id,
                claim_lock=claim_lock,
                outcome=outcome,
                summary=summary or "",
                metadata=dict(metadata or {}),
                artifacts=artifacts,
                target_task_status=target_status,
                error=error,
            ))
        except TaskConflictError as exc:
            # Late worker or duplicate finish -- audit only, don't overwrite.
            logger.info(
                "finish_run CAS conflict (late worker): task=%s run=%s: %s",
                task.id, run_id, exc,
            )
            return None
        except TaskNotFoundError as exc:
            logger.warning(
                "finish_run task/run not found: task=%s run=%s: %s",
                task.id, run_id, exc,
            )
            return None

        await self._write_lifecycle_for_status(
            result.task, target_status, summary=summary, error=error,
        )
        await self._notify_if_terminal(result, target_status)
        return result

    # ------------------------------------------------------------------
    # finalize_propose (worker propose -> WAITING_APPROVAL run finalization)
    # ------------------------------------------------------------------

    async def finalize_propose(
        self,
        task_id: str,
        run_id: int,
        claim_lock: str,
        proposal: str | None = None,
    ) -> dict[str, Any]:
        """Worker proposed a change requiring user approval -- finalize the
        run with outcome=WAITING_APPROVAL via the unified cleanup path.

        Releases the claim (claim_lock/claim_expires/current_run_id),
        reclaims the worker (``dispatcher.cancel``), writes the run outcome
        + terminal event, and transitions the task to WAITING_APPROVAL.
        The ``change_proposed`` audit event is written by TaskService before
        calling this; the ``proposal`` arg is only used as the run summary.

        Raises TaskNotFoundError if the task does not exist.
        """
        task = await self.registry.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")

        # Reclaim the in-process worker (unified cleanup).
        await self._cancel_worker_if_active(task, run_id)

        result = await self._finish(
            task=task,
            run_id=run_id,
            claim_lock=claim_lock,
            outcome=TaskRunOutcome.WAITING_APPROVAL,
            summary=proposal or "",
        )
        status = "finalized" if result is not None else "conflict"
        return {
            "task_id": task_id,
            "run_id": run_id,
            "outcome": TaskRunOutcome.WAITING_APPROVAL.value,
            "status": status,
        }

    # ------------------------------------------------------------------
    # terminate (user cancel RUNNING -> TERMINATED -> CANCELLED)
    # ------------------------------------------------------------------

    async def terminate(
        self, task_id: str, run_id: int | None = None
    ) -> dict[str, Any]:
        """Cancel an in-process worker and finalize the run as TERMINATED.

        For RUNNING tasks with an active in-process worker: cancel the
        worker and call ``_finish`` with outcome=TERMINATED ->
        target=CANCELLED (unified cleanup releases claim).

        For RUNNING tasks with no in-process handle (cross-process or
        restart orphan): append a ``terminate_requested`` event and let
        lease recovery handle the cleanup later.

        Returns a status dict:
          - ``terminated``: worker cancelled + run finalized
          - ``terminate_requested``: no in-process handle; event written
          - ``conflict``: CAS conflict during finalize
          - ``not_running`` / ``no_active_run``: preconditions not met
        """
        task = await self.registry.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        if task.status != TaskStatus.RUNNING:
            return {"task_id": task_id, "status": "not_running"}

        target_run_id = run_id or task.current_run_id
        if target_run_id is None:
            return {"task_id": task_id, "status": "no_active_run"}

        cancelled = await self._cancel_worker_if_active(task, target_run_id)
        if cancelled:
            result = await self._finish(
                task=task,
                run_id=target_run_id,
                claim_lock=task.claim_lock or "",
                outcome=TaskRunOutcome.TERMINATED,
                error="terminated by request",
            )
            if result is not None:
                return {"task_id": task_id, "status": "terminated"}
            return {"task_id": task_id, "status": "conflict"}

        # No in-process handle -> write terminate_requested event and wait
        # for lease recovery to reclaim.
        await self.registry.append_event(
            task_id, "terminate_requested",
            {"run_id": target_run_id}, run_id=target_run_id,
        )
        return {"task_id": task_id, "status": "terminate_requested"}

    # ------------------------------------------------------------------
    # TaskPolicy -> target_task_status decision
    # ------------------------------------------------------------------

    def _decide_target_status(
        self,
        task: Task,
        outcome: TaskRunOutcome,
    ) -> TaskStatus:
        """Decide the target TaskStatus based on outcome and TaskPolicy.

        Mapping (spec Data Model):
          COMPLETED            -> SUCCEEDED
          WAITING_APPROVAL     -> WAITING_APPROVAL
          TERMINATED           -> CANCELLED (user cancel)
          EXPIRED              -> EXPIRED
          CRASHED / TIMED_OUT  -> EXPIRED (worker died; user must retry)
          FAILED / SPAWN_FAILED -> circuit breaker:
              projected_failures = consecutive_failures + 1
              if projected_failures > max_retries -> FAILED
              else -> QUEUED (auto-retry)

        The circuit breaker uses TaskPolicy.evaluate(RUNNING -> QUEUED),
        which DENYs when consecutive_failures > max_retries.
        """
        if outcome == TaskRunOutcome.COMPLETED:
            return TaskStatus.SUCCEEDED
        if outcome == TaskRunOutcome.WAITING_APPROVAL:
            return TaskStatus.WAITING_APPROVAL
        if outcome == TaskRunOutcome.TERMINATED:
            return TaskStatus.CANCELLED
        if outcome == TaskRunOutcome.ABORTED:
            # Worker deliberate fast-fail (task_fail) -> FAILED terminal, no retry.
            # 绕过断路器（区别于 FAILED/SPAWN_FAILED 的可重试系统失败）。
            return TaskStatus.FAILED
        if outcome == TaskRunOutcome.EXPIRED:
            return TaskStatus.EXPIRED
        if outcome in (TaskRunOutcome.CRASHED, TaskRunOutcome.TIMED_OUT):
            return TaskStatus.EXPIRED

        # Retryable: FAILED, SPAWN_FAILED
        projected_failures = task.consecutive_failures + 1
        request = TaskPolicyRequest(
            current=TaskStatus.RUNNING,
            target=TaskStatus.QUEUED,
            consecutive_failures=projected_failures,
            max_retries=task.max_retries,
        )
        if self.policy.evaluate(request) is PolicyOutcome.DENY:
            # Circuit breaker tripped -> FAILED (terminal)
            return TaskStatus.FAILED
        return TaskStatus.QUEUED

    # ------------------------------------------------------------------
    # recover_crashed_workers (crashed in-process worker -> CRASHED -> EXPIRED)
    # ------------------------------------------------------------------

    async def _recover_crashed_workers(self, now: datetime) -> int:
        """Recover in-process workers that ended with an exception.

        Only workers that the dispatcher tracks as done-with-exception are
        recovered here. The outcome is CRASHED, which maps to EXPIRED
        (user must retry). The registry's recover_run default mapping
        handles the target status.
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
                result = await self.registry.recover_run(RecoverRunCommand(
                    task_id=task_id,
                    run_id=run_id,
                    claim_lock=task.claim_lock or "",
                    outcome=TaskRunOutcome.CRASHED,
                    error=error,
                ))
                # CRASHED -> EXPIRED (terminal) -> lifecycle + notify
                await self._write_lifecycle_for_status(result.task, TaskStatus.EXPIRED)
                await self._notify_if_terminal(result, TaskStatus.EXPIRED)
                recovered += 1
            except (TaskConflictError, TaskNotFoundError) as exc:
                logger.warning(
                    "crash recovery failed for task %s run %s: %s",
                    task_id, run_id, exc,
                )
        return recovered

    # ------------------------------------------------------------------
    # recover_stale_executions (lease/heartbeat expired -> EXPIRED)
    # ------------------------------------------------------------------

    async def _recover_stale_executions(self, now: datetime) -> int:
        """Recover RUNNING tasks with expired leases or stale heartbeats.

        For each RUNNING task:
          - If the in-process worker is still active, only reclaim when the
            heartbeat is stale (hung worker).
          - If no in-process worker, reclaim when the lease has expired.
          - Recovery outcome is EXPIRED, which maps to task status EXPIRED
            (user must retry; not auto-retry to QUEUED).
        """
        running_tasks = await self.registry.list_running()
        recovered = 0
        for task in running_tasks:
            if task.claim_lock is None or task.current_run_id is None:
                continue

            has_worker = await self._has_active_worker(task.current_run_id)
            if has_worker:
                # Worker still active in-process; check heartbeat staleness.
                if not task.is_stale(now, self.heartbeat_timeout_seconds):
                    continue
                # Heartbeat stale -- reclaim below.
            else:
                # No active worker; check lease expiry.
                if task.claim_expires is not None:
                    expires_aware = task.claim_expires
                    if expires_aware.tzinfo is None:
                        expires_aware = expires_aware.replace(tzinfo=timezone.utc)
                    if expires_aware > now:
                        # Lease still valid, don't reclaim.
                        continue

            # Reclaim as EXPIRED.
            try:
                result = await self.registry.recover_run(RecoverRunCommand(
                    task_id=task.id,
                    run_id=task.current_run_id,
                    claim_lock=task.claim_lock,
                    outcome=TaskRunOutcome.EXPIRED,
                    error="lease expired or heartbeat stale",
                ))
                await self._write_lifecycle_for_status(result.task, TaskStatus.EXPIRED)
                await self._notify_if_terminal(result, TaskStatus.EXPIRED)
                recovered += 1
            except (TaskConflictError, TaskNotFoundError) as exc:
                logger.info(
                    "stale recovery skipped for task %s: %s", task.id, exc
                )
        return recovered

    # ------------------------------------------------------------------
    # notify (idempotent by terminal_event_id)
    # ------------------------------------------------------------------

    async def _notify_if_terminal(
        self, result: FinishRunResult, target_status: TaskStatus
    ) -> None:
        """Deliver notification for terminal / WAITING_APPROVAL targets.

        Idempotent by terminal_event_id: the notifier checks
        ``task_notify_subs.last_terminal_event_id`` before delivering.
        Auto-retry to QUEUED does NOT trigger notification.
        """
        if self.notifier is None:
            return
        if target_status not in _NOTIFIED_TARGET_STATUSES:
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

    async def _cancel_worker_if_active(
        self, task: Task, run_id: int
    ) -> bool:
        """Cancel the in-process worker for this run if it is active.

        Returns True if the worker was cancelled, False otherwise (no
        active worker, no worker_token, or cancel raised).
        """
        has_active = await self._has_active_worker(run_id)
        if not has_active or not task.worker_token:
            return False
        try:
            return await self.dispatcher.cancel(task.worker_token)
        except Exception as exc:
            logger.warning(
                "cancel worker failed for run %s: %s", run_id, exc,
            )
            return False

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
