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
    TaskArtifact,
    TaskConflictError,
    TaskNotFoundError,
    TaskRunOutcome,
    TaskStatus,
    available_lifecycle_actions,
)
from app.domain.task_config import TaskConfig, TaskConfigProvider
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
        lifecycle_writer: Callable[[str, str, dict[str, Any] | None], Awaitable[Any]] | None = None,
        result_writer: Callable[[str, str], Awaitable[Any]] | None = None,
        lease_seconds: int = 900,
        heartbeat_timeout_seconds: int = 300,
        max_runtime_seconds: int = 3600,
        max_concurrency: int = 4,
        task_config_provider: TaskConfigProvider | None = None,
        artifact_register_callback: Callable[[TaskArtifact, str, int, int], Awaitable[None]] | None = None,
        artifact_normalizer: Callable[[dict[str, Any], Task], Awaitable[TaskArtifact | None]] | None = None,
    ):
        self.registry = registry
        self.dispatcher = dispatcher
        self.executor = executor
        self.policy = policy
        self.notifier = notifier
        self.lifecycle_writer = lifecycle_writer
        self.result_writer = result_writer
        self.lease_seconds = lease_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.max_runtime_seconds = max_runtime_seconds
        self.max_concurrency = max_concurrency
        self._task_config_provider = task_config_provider
        self.artifact_register_callback = artifact_register_callback
        self.artifact_normalizer = artifact_normalizer

    def _fallback_config(self) -> TaskConfig:
        """Build a TaskConfig from constructor scalars (provider unavailable)."""
        return TaskConfig(
            task_max_concurrency=self.max_concurrency,
            task_lease_seconds=self.lease_seconds,
            task_heartbeat_timeout_seconds=self.heartbeat_timeout_seconds,
            task_max_runtime_seconds=self.max_runtime_seconds,
        )

    async def _snapshot(self) -> TaskConfig:
        """Single config snapshot for one operation. Provider for hot-reload;
        constructor scalars as fallback when no provider (tests/legacy)."""
        if self._task_config_provider is not None:
            return await self._task_config_provider.current()
        return self._fallback_config()

    # ------------------------------------------------------------------
    # Lifecycle chat messages (best-effort, never blocks finalization)
    # ------------------------------------------------------------------

    async def _write_lifecycle(
        self, task: Task, content: str, card: dict[str, Any] | None = None,
    ) -> None:
        """向执行会话 best-effort 写 ui.task_lifecycle system 消息。

        card 为可选结构化交互载荷：交互态（waiting/failed/expired）传版本化 payload，
        纯文本 lifecycle（succeeded/cancelled/开始运行）传 None。writer 为 None
        （未装配/降级）时跳过；写入异常仅 log.warning，不改变任务 CAS、worker 回收
        或飞书投递结果。会话不存在时 SessionService 抛 SessionNotFoundError
        -> 这里吞掉（会话已删，不复活）。
        """
        if self.lifecycle_writer is None:
            return
        session_id = task_execution_session_id(task)
        try:
            await self.lifecycle_writer(session_id, content, card)
        except Exception:
            logger.warning(
                "lifecycle write failed for task %s", task.id, exc_info=True,
            )

    async def _write_result(self, task: Task, content: str) -> None:
        """向执行会话 best-effort 写 ui.task_result system 消息（最终结果，普通消息渲染）。

        所有终态（SUCCEEDED/FAILED/CANCELLED/EXPIRED）时调用，与 ui.task_lifecycle
        任务状态卡片并存。writer 为 None 时跳过；写入异常仅 log.warning，不改变任务 CAS、
        worker 回收或飞书投递结果。会话不存在时吞掉（不复活）。
        """
        if self.result_writer is None:
            return
        session_id = task_execution_session_id(task)
        try:
            await self.result_writer(session_id, content)
        except Exception:
            logger.warning(
                "result write failed for task %s", task.id, exc_info=True,
            )

    def _lifecycle_text(
        self,
        task: Task,
        target_status: TaskStatus,
        summary: str | None = None,
        error: str | None = None,
    ) -> str | None:
        """终态/等待批准 -> 任务状态卡片正文；QUEUED 等非通知态返回 None（不写卡片）。

        所有任务结束情况（SUCCEEDED/FAILED/CANCELLED/EXPIRED）均写任务状态卡片，
        与 ui.task_result 结果消息并存：卡片为状态通知（折叠，含 summary/error），
        结果消息为可见结果（普通消息，打印在 Chat 框）。开始运行由 run_claim 直接写。
        """
        title = task.title
        if target_status == TaskStatus.WAITING_APPROVAL:
            return f"等待批准: {task.id} - {title} | 提案: {summary or ''}"
        if target_status == TaskStatus.SUCCEEDED:
            return f"已完成: {task.id} - {title} | {summary or ''}"
        if target_status == TaskStatus.FAILED:
            return f"已失败: {task.id} - {title} | {error or summary or ''}"
        if target_status == TaskStatus.CANCELLED:
            return f"已取消: {task.id} - {title}"
        if target_status == TaskStatus.EXPIRED:
            return f"已过期: {task.id} - {title}"
        # QUEUED（自动重试）等：不写
        return None

    def _lifecycle_card(
        self,
        task: Task,
        target_status: TaskStatus,
        summary: str | None = None,
        error: str | None = None,
        interaction_type: str | None = None,
    ) -> dict[str, Any] | None:
        """交互态 -> 版本化 card payload；无交互动作的状态返回 None。

        card schema（waiting_approval 8 字段，failed/expired 7 字段）：
          - schema_version: 1
          - kind: "task_lifecycle"
          - task_id / status / title / summary / available_actions
          - interaction_type（仅 waiting_approval）：'approval' 或 'intent_request'

        status 取传入的 target_status（target snapshot），不用 task.status，以
        反映本次 CAS 的目标态而非 CAS 前的旧态。summary 优先级：
          - WAITING_APPROVAL: summary（提案）
          - FAILED: error 后备 summary
          - EXPIRED: error/summary 后备稳定文案 "任务运行已过期"

        动作来自 available_lifecycle_actions(target_status, interaction_type)
        （app.domain.task）。无动作（QUEUED/RUNNING/SUCCEEDED/CANCELLED）返回 None。
        interaction_type 字段仅写入 waiting_approval card payload；failed/expired
        不带该字段（后端 Domain 层对 failed/expired 忽略 interaction_type）。
        """
        actions = available_lifecycle_actions(target_status, interaction_type)
        if not actions:
            return None
        if target_status == TaskStatus.WAITING_APPROVAL:
            card_summary = summary or ""
        elif target_status == TaskStatus.FAILED:
            card_summary = error or summary or ""
        elif target_status == TaskStatus.EXPIRED:
            card_summary = error or summary or "任务运行已过期"
        else:
            card_summary = ""
        card = {
            "schema_version": 1,
            "kind": "task_lifecycle",
            "task_id": task.id,
            "status": target_status.value,
            "title": task.title,
            "summary": card_summary,
            "available_actions": list(actions),
        }
        if target_status == TaskStatus.WAITING_APPROVAL:
            # 仅 waiting_approval card 携带 interaction_type；TaskService 校验
            # proposal_type in {"approval","intent_request"}，Domain 纯函数对
            # None/未知值回退到 approval 语义。
            card["interaction_type"] = interaction_type or "approval"
        return card

    def _terminal_result_text(
        self,
        task: Task,
        target_status: TaskStatus,
        summary: str | None = None,
        error: str | None = None,
    ) -> str | None:
        """终态 -> 最终结果正文（以普通消息渲染，打印在 Chat 框）。

        覆盖所有任务结束情况：SUCCEEDED（成功）/FAILED（错误）/CANCELLED（取消）/
        EXPIRED（过期）。非终态（WAITING_APPROVAL/QUEUED/RUNNING）返回 None。
        """
        title = task.title
        if target_status == TaskStatus.SUCCEEDED:
            s = (summary or "").strip()
            return f"任务已完成：{title}\n\n{s}" if s else f"任务已完成：{title}"
        if target_status == TaskStatus.FAILED:
            reason = (error or summary or "").strip()
            return f"任务已失败：{title}\n\n{reason}" if reason else f"任务已失败：{title}"
        if target_status == TaskStatus.CANCELLED:
            return f"任务已取消：{title}"
        if target_status == TaskStatus.EXPIRED:
            return f"任务已过期：{title}"
        return None

    async def _write_lifecycle_for_status(
        self,
        task: Task,
        target_status: TaskStatus,
        summary: str | None = None,
        error: str | None = None,
        interaction_type: str | None = None,
    ) -> None:
        text = self._lifecycle_text(task, target_status, summary=summary, error=error)
        if text is None:
            return
        card = self._lifecycle_card(
            task, target_status, summary=summary, error=error,
            interaction_type=interaction_type,
        )
        await self._write_lifecycle(task, text, card)

    async def _write_result_if_terminal(
        self,
        task: Task,
        target_status: TaskStatus,
        summary: str | None = None,
        error: str | None = None,
    ) -> None:
        """终态 -> 写 ui.task_result 最终结果（普通消息，打印在 Chat 框）。

        覆盖 SUCCEEDED/FAILED/CANCELLED/EXPIRED 所有任务结束情况；非终态不写。
        """
        text = self._terminal_result_text(
            task, target_status, summary=summary, error=error,
        )
        if text is None:
            return
        await self._write_result(task, text)

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
        cfg = await self._snapshot()
        recovered_crashed = await self._recover_crashed_workers(now)
        recovered_stale = await self._recover_stale_executions(now, cfg)

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
            if active_count + spawned >= cfg.task_max_concurrency:
                break
            try:
                result = await self._claim_and_spawn(task, cfg)
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

    async def _claim_and_spawn(self, task: Task, cfg: TaskConfig) -> str | None:
        """Atomically claim a QUEUED task and spawn a worker.

        Returns the worker_token, or None if claim failed (already claimed
        by another tick / status changed).
        """
        claim_lock = f"cl-{uuid4().hex[:12]}"
        claim = await self.registry.claim_task(
            task.id, claim_lock, cfg.task_lease_seconds
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
        cfg = await self._snapshot()
        max_runtime = task.max_runtime_seconds or cfg.task_max_runtime_seconds
        # Hard timeout must be < lease
        timeout = min(max_runtime, cfg.task_lease_seconds - 1)

        # 生命周期：worker 起始（best-effort，不阻断执行；spawn 失败走 _handle_spawn_failure，
        # 未进 run_claim，不写"开始运行"）。纯文本 lifecycle，card=None。
        await self._write_lifecycle(
            task, f"开始运行: {task.id} - {task.title}", card=None,
        )

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

        normalized_artifacts = await self._normalize_artifacts(
            tuple(artifacts), task,
        )

        await self._finish(
            task=task,
            run_id=run_id,
            claim_lock=claim_lock,
            outcome=status,
            summary=output or error or "",
            error=error,
            metadata=dict(metadata),
            artifacts=normalized_artifacts,
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
        artifacts: tuple[TaskArtifact, ...] = (),
        interaction_type: str | None = None,
    ) -> FinishRunResult | None:
        """Unified CAS finalize -- the SOLE run cleanup path.

        Decides ``target_task_status`` via TaskPolicy (circuit breaker for
        retryable outcomes), calls ``registry.finish_run`` with the target
        status (so the Registry does not guess), and delivers notification
        for terminal / WAITING_APPROVAL targets.

        ``interaction_type`` is only meaningful for ``WAITING_APPROVAL``
        outcomes (worker propose); it selects the lifecycle card flavor
        (approval vs intent_request) and is ignored for other outcomes.

        ``artifacts`` is a normalized ``tuple[TaskArtifact, ...]`` produced
        by ``_normalize_artifacts``. After CAS success, the optional
        ``artifact_register_callback`` is invoked for each artifact in stable
        ordinal order (best-effort, never affects the completed Task).

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
            interaction_type=interaction_type,
        )
        await self._write_result_if_terminal(
            result.task, target_status, summary=summary, error=error,
        )
        await self._notify_if_terminal(result, target_status)

        # Invoke artifact register callbacks (ONLY after CAS success).
        # Best-effort: single failure does not affect other items or the
        # completed Task.
        await self._invoke_artifact_callbacks(
            artifacts, task.id, run_id,
        )

        return result

    # ------------------------------------------------------------------
    # Artifact normalization + register callback (T10)
    # ------------------------------------------------------------------

    async def _normalize_artifacts(
        self,
        raw_artifacts: tuple[Any, ...],
        task: Task,
    ) -> tuple[TaskArtifact, ...]:
        """Normalize agent-result artifact entries to ``TaskArtifact`` tuples.

        Strict normalization rules:
          - ``type``, ``name``, ``storage_ref`` are required. Missing any
            -> skip that item with warning.
          - ``source_task_id`` is force-overwritten with ``task.id`` (never
            trust agent-supplied value).
          - ``mime``, ``size``, ``summary``, ``checksum`` -- if missing,
            filled via the injected ``artifact_normalizer`` (controlled /
            server-side). If no normalizer, safe defaults are used.
          - If the normalizer returns ``None`` (unreadable ref) or raises,
            the item is skipped with warning.

        The resulting ``tuple[TaskArtifact, ...]`` is passed to
        ``FinishRunCommand.artifacts``.
        """
        result: list[TaskArtifact] = []
        for raw in raw_artifacts:
            if not isinstance(raw, dict):
                raw = {"type": "unknown", "name": str(raw), "storage_ref": ""}
            art_type = raw.get("type") or ""
            art_name = raw.get("name") or ""
            storage_ref = raw.get("storage_ref") or ""
            if not art_type or not art_name or not storage_ref:
                logger.warning(
                    "artifact skipped: missing required field "
                    "(type=%r name=%r storage_ref=%r)",
                    art_type, art_name, storage_ref,
                )
                continue
            # Force-overwrite source_task_id (never trust agent-supplied value)
            raw = {**raw, "source_task_id": task.id}

            if self.artifact_normalizer is not None:
                try:
                    normalized = await self.artifact_normalizer(raw, task)
                except Exception as exc:
                    logger.warning(
                        "artifact normalization failed: "
                        "source_kind=task_artifact source_ref=%s exc_type=%s",
                        storage_ref, type(exc).__name__,
                    )
                    continue
                if normalized is None:
                    logger.warning(
                        "artifact skipped: unreadable "
                        "source_kind=task_artifact source_ref=%s",
                        storage_ref,
                    )
                    continue
                result.append(normalized)
            else:
                # No normalizer: use safe defaults for missing fields.
                size = raw.get("size")
                result.append(TaskArtifact(
                    type=art_type,
                    name=art_name,
                    mime=raw.get("mime") or "",
                    size=size if size is not None else 0,
                    storage_ref=storage_ref,
                    source_task_id=task.id,
                    summary=raw.get("summary") or "",
                    checksum=raw.get("checksum") or "",
                ))
        return tuple(result)

    async def _invoke_artifact_callbacks(
        self,
        artifacts: tuple[TaskArtifact, ...],
        task_id: str,
        run_id: int,
    ) -> None:
        """Invoke ``artifact_register_callback`` for each artifact in ordinal order.

        Called ONLY after ``registry.finish_run`` returns ``FinishRunResult``
        (CAS success). Self-contained: no-ops when no callback is registered
        or no artifacts to process. Single callback failure -> log warning
        (safe fields only: source_kind, source_ref, exception type; NO
        content/paths) and continue with remaining items. Does NOT affect
        the completed Task.
        """
        if self.artifact_register_callback is None or not artifacts:
            return
        for ordinal, artifact in enumerate(artifacts):
            try:
                await self.artifact_register_callback(
                    artifact, task_id, run_id, ordinal,
                )
            except Exception as exc:
                logger.warning(
                    "artifact register callback failed: "
                    "source_kind=task_artifact source_ref=%s exc_type=%s",
                    artifact.storage_ref, type(exc).__name__,
                )

    # ------------------------------------------------------------------
    # finalize_propose (worker propose -> WAITING_APPROVAL run finalization)
    # ------------------------------------------------------------------

    async def finalize_propose(
        self,
        task_id: str,
        run_id: int,
        claim_lock: str,
        proposal: str | None = None,
        proposal_type: str = "approval",
    ) -> dict[str, Any]:
        """Worker proposed a change requiring user approval -- finalize the
        run with outcome=WAITING_APPROVAL via the unified cleanup path.

        Releases the claim (claim_lock/claim_expires/current_run_id),
        reclaims the worker (``dispatcher.cancel``), writes the run outcome
        + terminal event, and transitions the task to WAITING_APPROVAL.
        The ``change_proposed`` audit event is written by TaskService before
        calling this; the ``proposal`` arg is only used as the run summary.
        ``proposal_type`` selects the lifecycle card flavor (approval vs
        intent_request) and is forwarded to ``_finish`` as
        ``interaction_type``; it does not change the Task state machine.

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
            interaction_type=proposal_type,
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
                # CRASHED -> EXPIRED (terminal) -> 任务状态卡片 + 最终结果(普通消息) + notify
                await self._write_lifecycle_for_status(result.task, TaskStatus.EXPIRED)
                await self._write_result_if_terminal(result.task, TaskStatus.EXPIRED)
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

    async def _recover_stale_executions(self, now: datetime, cfg: TaskConfig) -> int:
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
                if not task.is_stale(now, cfg.task_heartbeat_timeout_seconds):
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
                # 恢复结束的任务消息：EXPIRED 终态写任务状态卡片 + 最终结果(普通消息)
                await self._write_lifecycle_for_status(result.task, TaskStatus.EXPIRED)
                await self._write_result_if_terminal(result.task, TaskStatus.EXPIRED)
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
