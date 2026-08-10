"""T4: Application TaskService -- Manus-aligned 7-state machine.

CRUD, attachments, worker context, and user actions (propose/approve/reject/
cancel/retry). The dependency graph (``add_link`` / ``unlink`` /
``list_links`` / ``list_children`` / ``list_parents``), swarm planning
(``specify`` / ``decompose`` / ``create_swarm``), the ``planning_service``
constructor parameter, and the ``assignee`` field have all been removed.

State changes go through domain methods (``Task.propose_change`` /
``resolve_approval`` / ``cancel`` / ``retry`` / ``complete`` / ``set_archived``)
followed by ``registry.update_task`` with ``expected_version`` CAS. Run
finalization (claim release + worker cancel) is owned by TaskRunService;
``propose_change`` and ``cancel_task`` delegate to it when injected and
otherwise only advance state + write the audit event.

Injection:
  - ``TaskRegistry`` (Domain port -- async Protocol)
  - ``TaskPolicy`` (14th domain Policy)
  - ``MemoryStore`` (optional, for execution_session cleanup on delete)
  - ``attachments_root`` (Path -- controlled directory for attachment files)
  - ``attachment_max_bytes`` / ``attachment_task_max_bytes`` (size limits)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.task import (
    BulkUpdateCommand,
    BulkUpdateItem,
    FinishRunCommand,
    ProposalResolutionCommand,
    Task,
    TaskAttachment,
    TaskClaimError,
    TaskComment,
    TaskConflictError,
    TaskEvent,
    TaskExecutionPolicy,
    TaskListCursor,
    TaskListPage,
    TaskNotFoundError,
    TaskRun,
    TaskRunOutcome,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    TaskWorkspaceKind,
)
from app.application.task_session import task_execution_session_id
from app.domain.task_config import TaskConfig, TaskConfigProvider
from app.domain.task_policy import TaskPolicy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Segment byte limits for build_worker_context (spec: per-segment byte limits)
_CONTEXT_BODY_LIMIT = 8192
_CONTEXT_COMMENT_LIMIT = 2048
_CONTEXT_EVENT_LIMIT = 4096
_CONTEXT_RUN_LIMIT = 2048
_CONTEXT_PROPOSAL_LIMIT = 2048
_CONTEXT_DECISION_LIMIT = 1024
_CONTEXT_PROGRESS_LIMIT = 4096
_MAX_EVENTS_IN_DETAIL = 50
_MAX_RUNS_IN_DETAIL = 20
_MAX_PROGRESS_EVENTS = 10

# Filename validation: reject path separators, control chars, dots-only
_FILENAME_SAFE_RE = re.compile(r"^[^\x00-\x1f/\\<>:""|?*\x7f]+$")
_FILENAME_DOT_RE = re.compile(r"^\.+$")

# Default attachment limits (overridden by Settings in wiring)
_DEFAULT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
_DEFAULT_ATTACHMENT_TASK_MAX_BYTES = 100 * 1024 * 1024  # 100 MB

# Intent event kinds (non-terminal audit; TaskRunService writes the actual
# terminal "finished" event)
_INTENT_COMPLETE = "complete_requested"
_INTENT_FAIL = "fail_requested"

# Event kinds used by the propose/approve/reject/revise/cancel/retry surface
_EVENT_CHANGE_PROPOSED = "change_proposed"
_EVENT_CHANGE_APPROVED = "change_approved"
_EVENT_CHANGE_REJECTED = "change_rejected"
_EVENT_CHANGE_REVISED = "change_revised"
_EVENT_CANCELLED = "cancelled"
_EVENT_RETRIED = "retried"

# Approval note length cap (trim'd code points). Defends worker_context budget
# and aligns with task_routes._NOTE_MAX.
_NOTE_MAX_CODEPOINTS = 2000

# Event kinds surfaced in the worker context "progress" segment
_PROGRESS_EVENT_KINDS: frozenset[str] = frozenset({
    "comment_added",
    _INTENT_COMPLETE,
    _INTENT_FAIL,
    _EVENT_CHANGE_PROPOSED,
    _EVENT_CHANGE_APPROVED,
    _EVENT_CHANGE_REJECTED,
    _EVENT_CHANGE_REVISED,
    _EVENT_CANCELLED,
    _EVENT_RETRIED,
    "goal_judge_feedback",
    "finished",
})

# Notification-worthy terminal outcomes (spec: only these trigger delivery)
_NOTIFIED_OUTCOMES: frozenset[TaskRunOutcome] = frozenset({
    TaskRunOutcome.COMPLETED,
    TaskRunOutcome.WAITING_APPROVAL,
    TaskRunOutcome.FAILED,
    TaskRunOutcome.ABORTED,
    TaskRunOutcome.CRASHED,
    TaskRunOutcome.TIMED_OUT,
    TaskRunOutcome.EXPIRED,
    TaskRunOutcome.TERMINATED,
})


class TaskService:
    """Application service for Task CRUD, worker ops, and user actions.

    Satisfies the Manus-aligned 7-state surface: create (default QUEUED),
    propose_change / approve_change / reject_change (intent approval),
    cancel_task, retry_task, complete, heartbeat, set_archived, plus full
    CRUD, bulk update, attachments, and notify subscriptions.

    State changes use domain methods + ``registry.update_task`` CAS. Run
    finalization is delegated to TaskRunService when injected.
    """

    def __init__(
        self,
        registry: Any,
        policy: TaskPolicy,
        memory_store: Any | None = None,
        attachments_root: Path | None = None,
        attachment_max_bytes: int = _DEFAULT_ATTACHMENT_MAX_BYTES,
        attachment_task_max_bytes: int = _DEFAULT_ATTACHMENT_TASK_MAX_BYTES,
        lifecycle_writer: Callable[[str, str, dict[str, Any] | None], Awaitable[Any]] | None = None,
        task_config_provider: TaskConfigProvider | None = None,
        artifact_register_callback: Callable[[TaskAttachment], Awaitable[None]] | None = None,
        artifact_id_lookup: Callable[[str], Awaitable[str | None]] | None = None,
        artifact_delete_callback: Callable[[str], Awaitable[None]] | None = None,
        workspace_ref_validator: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.registry = registry
        self.policy = policy
        self.memory_store = memory_store
        self.lifecycle_writer = lifecycle_writer
        self.attachments_root = (
            Path(attachments_root) if attachments_root is not None else None
        )
        self.attachment_max_bytes = attachment_max_bytes
        self.attachment_task_max_bytes = attachment_task_max_bytes
        self._run_service: Any = None
        self._task_config_provider = task_config_provider
        self._artifact_register_callback = artifact_register_callback
        self._artifact_id_lookup = artifact_id_lookup
        self._artifact_delete_callback = artifact_delete_callback
        self._workspace_ref_validator = workspace_ref_validator

    async def _snapshot(self) -> TaskConfig:
        if self._task_config_provider is not None:
            return await self._task_config_provider.current()
        return TaskConfig(
            task_attachment_max_bytes=self.attachment_max_bytes,
            task_attachment_task_max_bytes=self.attachment_task_max_bytes,
            task_failure_limit=3,
            note_max_codepoints=_NOTE_MAX_CODEPOINTS,
        )

    @property
    def note_max_codepoints(self) -> int:
        """Synchronous fallback for the note limit (env constant).

        Routes call _normalize_note which uses the resolved config snapshot
        when a provider is set; this property is the legacy/env default used
        only when no provider is configured.
        """
        return _NOTE_MAX_CODEPOINTS

    async def _write_lifecycle(
        self, task: Task, content: str, card: dict[str, Any] | None = None,
    ) -> None:
        """向执行会话 best-effort 写 ui.task_lifecycle system 消息（非 RUNNING cancel 用）。

        card 为可选结构化交互载荷：决策回执/取消均为纯文本 lifecycle，card 恒为 None。
        writer 为 None 时跳过；异常仅 log.warning，不阻断 cancel CAS 结果。RUNNING cancel
        由 TaskRunService.terminate -> _finish 写，此处不重复写。
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

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_task(
        self,
        *,
        title: str,
        body: str = "",
        priority: int = 0,
        created_by: str = "",
        board: str = "default",
        idempotency_key: str | None = None,
        origin_session_id: str | None = None,
        scheduled_at: datetime | None = None,
        skills: tuple[str, ...] | list[str] | None = None,
        execution_policy: TaskExecutionPolicy | None = None,
        workspace_kind: TaskWorkspaceKind = TaskWorkspaceKind.SCRATCH,
        workspace_path: str | None = None,
        model_override: str | None = None,
        max_runtime_seconds: int | None = None,
        max_retries: int | None = None,
        goal_mode: bool = False,
        goal_max_turns: int | None = None,
    ) -> Task:
        # max_retries None means "caller did not specify"; resolve to the
        # configured task_failure_limit default. Explicit 0 (or any int) is
        # honored as the caller's intent (avoids the int=0 default-arg trap).
        if max_retries is None:
            cfg = await self._snapshot()
            max_retries = cfg.task_failure_limit
        if not title or not title.strip():
            raise TaskValidationError("title must not be empty")
        if board != "default":
            raise TaskValidationError(
                "only 'default' board is accepted in this iteration"
            )

        # Idempotency: if key provided, check for existing task
        if idempotency_key:
            existing = await self._find_by_idempotency(
                board, created_by, idempotency_key
            )
            if existing is not None:
                return existing

        now = datetime.now(timezone.utc)
        task = Task(
            id=f"t_{uuid4().hex[:16]}",
            title=title.strip(),
            body=body,
            priority=priority,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            version=1,
            status=TaskStatus.QUEUED,
            board=board,
            origin_session_id=origin_session_id,
            scheduled_at=scheduled_at,
            skills=tuple(skills) if skills else (),
            execution_policy=execution_policy or TaskExecutionPolicy(),
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            model_override=model_override,
            max_runtime_seconds=max_runtime_seconds,
            max_retries=max_retries,
            goal_mode=goal_mode,
            goal_max_turns=goal_max_turns,
            idempotency_key=idempotency_key,
        )
        created = await self.registry.create_task(task)
        await self.registry.append_event(
            created.id, "created", {"title": created.title}
        )
        return created

    async def get_task(self, task_id: str) -> Task:
        task = await self.registry.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task

    async def latest_waiting_approval_in_session(
        self, session_id: str,
    ) -> Task | None:
        """Return the most recent WAITING_APPROVAL Task in ``session_id``.

        Delegates to the Registry's session-scoped query. Empty ``session_id``
        is rejected with ``TaskValidationError`` to prevent full-board scans.
        Result satisfies ``origin_session_id == session_id``,
        ``status == WAITING_APPROVAL``, ``not is_archived``, ordered by
        ``created_at DESC, id DESC`` (Registry responsibility).
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise TaskValidationError("session_id must not be empty")
        return await self.registry.latest_waiting_approval_in_session(
            session_id.strip(),
        )

    async def list_tasks(
        self,
        board: str = "default",
        cursor: TaskListCursor | None = None,
        limit: int = 100,
    ) -> TaskListPage:
        return await self.registry.list_tasks(board, cursor, limit)

    async def update_task(
        self,
        task_id: str,
        fields: Mapping[str, Any],
        expected_version: int,
    ) -> Task:
        task = await self.get_task(task_id)
        if task.status == TaskStatus.RUNNING:
            raise TaskStateError(
                f"cannot update RUNNING task {task_id}; terminate first"
            )
        # Spec: status changes must go through propose/approve/reject/cancel/
        # retry or run finalization, never via generic PATCH.
        if "status" in fields:
            raise TaskStateError(
                "status cannot be changed via update_task; use "
                "propose_change/approve_change/reject_change/cancel_task/"
                "retry_task or run finalization"
            )
        return await self.registry.update_task(task_id, fields, expected_version)

    async def bulk_update(self, command: BulkUpdateCommand) -> tuple[Task, ...]:
        # Pre-check: reject if any task is RUNNING or any item tries to set
        # status (status must go through action methods).
        for item in command.items:
            if "status" in item.fields:
                raise TaskStateError(
                    "status cannot be changed via bulk_update; use action methods"
                )
            task = await self.registry.get_task(item.task_id)
            if task is not None and task.status == TaskStatus.RUNNING:
                raise TaskStateError(
                    f"cannot bulk_update RUNNING task {item.task_id}"
                )
        result = await self.registry.bulk_update(command)
        return result.updated

    async def delete_task(self, task_id: str) -> bool:
        task = await self.get_task(task_id)
        if task.status == TaskStatus.RUNNING:
            raise TaskStateError(
                f"cannot delete RUNNING task {task_id}; terminate first"
            )
        # Get attachment list before delete (CASCADE removes rows)
        attachments = await self.registry.list_attachments(task_id)
        attachment_paths = [
            self._attachment_path(task_id, a.stored_name) for a in attachments
        ]
        deleted = await self.registry.delete_task(task_id)
        if not deleted:
            return False

        # Clean up attachment files (best-effort; log failures)
        for path in attachment_paths:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "failed to delete attachment file %s: %s", path, exc
                    )

        # Clean up artifacts registered against this task (best-effort). Artifacts
        # live in a separate DB registered via artifact_register_callback; without
        # this cascade they would persist and still show in the artifact list.
        if self._artifact_delete_callback is not None:
            try:
                await self._artifact_delete_callback(task_id)
            except Exception as exc:
                logger.warning(
                    "failed to delete artifacts for task %s: %s", task_id, exc
                )

        # Clean up execution session (not origin session)
        if task.execution_session_id and self.memory_store is not None:
            try:
                await self.memory_store.delete_session(task.execution_session_id)
            except Exception as exc:
                logger.warning(
                    "failed to delete execution session %s: %s",
                    task.execution_session_id,
                    exc,
                )
        return True

    # ------------------------------------------------------------------
    # Soft-delete archive flag (does NOT change status)
    # ------------------------------------------------------------------

    async def set_archived(
        self, task_id: str, value: bool, expected_version: int
    ) -> Task:
        """Toggle ``is_archived`` without changing status.

        Archive is a soft-delete flag for list/board visibility; it is NOT a
        lifecycle state and never triggers run finalization. RUNNING tasks
        cannot be archived (terminate first).
        """
        task = await self.get_task(task_id)
        if task.status == TaskStatus.RUNNING:
            raise TaskStateError(
                "cannot archive RUNNING task; terminate first"
            )
        return await self.registry.update_task(
            task_id, {"is_archived": bool(value)}, expected_version,
        )

    # ------------------------------------------------------------------
    # Intent approval: propose_change / approve_change / reject_change /
    # revise_change
    # ------------------------------------------------------------------

    async def propose_change(
        self,
        task_id: str,
        proposal: str,
        run_id: int,
        proposal_type: str = "approval",
    ) -> dict[str, Any]:
        """Worker proposes a change requiring user approval.

        Validates the task is RUNNING and ``run_id`` matches
        ``current_run_id``. Writes a ``change_proposed`` event (with proposal
        + run_id + proposal_type), advances the task to WAITING_APPROVAL via
        the domain method, and CAS-updates the registry. If a TaskRunService is
        injected, delegates run finalization (claim release + worker cancel)
        with outcome=waiting_approval and the interaction_type carried on the
        lifecycle card; otherwise only state + event are written (T5 wires
        the cleanup path).

        ``proposal_type`` selects the WAITING_APPROVAL card flavor:
          - ``"approval"`` (default): card shows approve/reject buttons.
          - ``"intent_request"``: card shows revise/cancel buttons + a
            textarea so the user can supply intent/information/clarification
            before continuing.

        Any other value raises ``TaskValidationError``. The Task state
        machine is unchanged: both flavors transition RUNNING ->
        WAITING_APPROVAL and resolve via the same approve/reject/revise
        decisions.
        """
        if not proposal or not proposal.strip():
            raise TaskValidationError("proposal must not be empty")
        if proposal_type not in ("approval", "intent_request"):
            raise TaskValidationError(
                f"proposal_type must be 'approval' or 'intent_request', "
                f"got {proposal_type!r}"
            )

        task = await self.get_task(task_id)
        if task.status != TaskStatus.RUNNING:
            raise TaskStateError(
                f"propose_change requires RUNNING, got {task.status.value}"
            )
        if task.current_run_id is None or task.current_run_id != run_id:
            raise TaskStateError(
                f"run_id mismatch: expected {task.current_run_id}, got {run_id}"
            )
        if not task.claim_lock:
            raise TaskStateError("task has no active claim")

        # Write the change_proposed event first; its id serves as the
        # proposal_event_id that approve/reject will reference. proposal_type
        # is recorded for audit / worker_context.
        event = await self.registry.append_event(
            task_id,
            _EVENT_CHANGE_PROPOSED,
            {"proposal": proposal, "run_id": run_id, "proposal_type": proposal_type},
            run_id=run_id,
        )

        # Delegate run finalization to TaskRunService when injected (unified
        # claim-release + worker-cancel path). Otherwise advance state in
        # place; T5's run finalization will release the claim atomically.
        delegated = False
        if self._run_service is not None:
            finalize = getattr(self._run_service, "finalize_propose", None)
            if finalize is not None:
                try:
                    await finalize(
                        task_id, run_id, task.claim_lock, proposal, proposal_type,
                    )
                    delegated = True
                except Exception as exc:
                    logger.warning(
                        "run_service.finalize_propose failed for %s: %s",
                        task_id, exc,
                    )

        if not delegated:
            # Advance state ourselves (claim release is T5's job in
            # production; tests verify state + event only).
            updated = task.propose_change(proposal, run_id)
            await self.registry.update_task(
                task_id,
                {"status": updated.status},
                task.version,
            )

        return {
            "outcome": TaskRunOutcome.WAITING_APPROVAL.value,
            "proposal": proposal,
            "proposal_type": proposal_type,
            "run_id": run_id,
            "proposal_event_id": event.id,
            "task_id": task_id,
        }

    async def approve_change(self, task_id: str, note: str | None = None) -> dict[str, Any]:
        """User approves the latest unresolved proposal.

        ``note`` is an optional user-supplied approval comment/feedback;
        persisted in the ``change_approved`` event payload and surfaced to the
        worker via build_worker_context on the next run.

        Validates the task is WAITING_APPROVAL. Delegates to the Registry's
        atomic ``resolve_proposal`` port (single transaction: find pending
        proposal, append decision event, CAS task -> QUEUED). After the
        Registry succeeds, a best-effort ``ui.task_lifecycle`` message is
        written to the task's execution session.
        """
        return await self._resolve_proposal(
            task_id=task_id,
            decision="approved",
            event_kind=_EVENT_CHANGE_APPROVED,
            note=note,
            required=False,
        )

    async def reject_change(self, task_id: str, note: str | None = None) -> dict[str, Any]:
        """User rejects the latest unresolved proposal.

        ``note`` is an optional rejection reason; same persistence/feedback
        contract as approve_change.

        Same contract as ``approve_change`` but writes ``change_rejected``
        and the decision is ``"rejected"``. The task still returns to QUEUED
        (next run may choose a different path or re-propose).
        """
        return await self._resolve_proposal(
            task_id=task_id,
            decision="rejected",
            event_kind=_EVENT_CHANGE_REJECTED,
            note=note,
            required=False,
        )

    async def revise_change(self, task_id: str, note: str | None = None) -> dict[str, Any]:
        """User revises the latest unresolved proposal with revision instructions.

        The third approval decision alongside approve/reject: the user gives
        the worker revision instructions (``note``) for re-execution. The note
        is required, trimmed, persisted in a ``change_revised`` event payload,
        and surfaced to the worker via build_worker_context.

        Delegates to the same unified ``_resolve_proposal`` helper as
        approve/reject. The response includes ``title`` for chat display.
        """
        result = await self._resolve_proposal(
            task_id=task_id,
            decision="revised",
            event_kind=_EVENT_CHANGE_REVISED,
            note=note,
            required=True,
        )
        # revise_change adds title for chat display (approve/reject do not)
        task = await self.get_task(task_id)
        result["title"] = task.title
        return result

    def _normalize_note(self, *, required: bool, note: str | None, note_max_codepoints: int = _NOTE_MAX_CODEPOINTS) -> str | None:
        """Normalize an approval note (service-level, no Registry access).

        - Non-string (and non-None) -> TaskValidationError
        - None or trim-empty:
          - required=True (revise) -> TaskValidationError
          - required=False (approve/reject) -> None (normalized)
        - Otherwise -> stripped note
        - > note_max_codepoints code points (after trim) -> TaskValidationError

        ``note_max_codepoints`` defaults to the env constant; callers that
        have a resolved config snapshot pass it so the limit is hot-reloadable.
        """
        if note is None:
            if required:
                raise TaskValidationError("note must not be empty")
            return None
        if not isinstance(note, str):
            raise TaskValidationError("note must be a string")
        trimmed = note.strip()
        if not trimmed:
            if required:
                raise TaskValidationError("note must not be empty")
            return None
        if len(trimmed) > note_max_codepoints:
            raise TaskValidationError(
                f"note too long (>{note_max_codepoints} code points)"
            )
        return trimmed

    async def _resolve_proposal(
        self,
        *,
        task_id: str,
        decision: str,
        event_kind: str,
        note: str | None,
        required: bool,
    ) -> dict[str, Any]:
        """Unified Application path for approve/reject/revise decisions.

        Single atomic flow:
          1. Service-level validation (task_id + note) -- no Registry access.
          2. Read Task to obtain ``expected_version`` and check status is
             WAITING_APPROVAL.
          3. Call ``registry.resolve_proposal`` (single transaction: locate
             pending proposal, append decision event, CAS task -> QUEUED).
          4. Best-effort ``ui.task_lifecycle`` write to the task's execution
             session (writer None -> skip; exception -> log.warning, decision
             still succeeds).
          5. Return a stable whitelist response from the Registry result.

        The old non-atomic ``_resolve_approval`` path (append_event +
        update_task as separate calls) has been removed; all three decisions
        now go through this helper and the Registry's atomic port.
        """
        # 1. Service-level validation (no Registry access)
        if not isinstance(task_id, str) or not task_id.strip():
            raise TaskValidationError("task_id must not be empty")
        # Resolve note_max_codepoints from the configured provider (hot-reload);
        # falls back to the env constant when no provider is set.
        cfg = await self._snapshot()
        normalized_note = self._normalize_note(
            required=required, note=note, note_max_codepoints=cfg.note_max_codepoints,
        )

        # 2. Read Task for expected_version + status check
        task = await self.get_task(task_id)
        if task.status != TaskStatus.WAITING_APPROVAL:
            raise TaskStateError(
                f"resolve_proposal requires WAITING_APPROVAL, "
                f"got {task.status.value}"
            )

        # 3. Atomic resolution via Registry port
        command = ProposalResolutionCommand(
            task_id=task_id,
            expected_version=task.version,
            decision=decision,
            event_kind=event_kind,
            note=normalized_note,
        )
        result = await self.registry.resolve_proposal(command)

        # 4. Best-effort lifecycle write (does not block the decision)
        if decision == "approved":
            content = f"已批准: {result.task.id} - {result.task.title}"
        elif decision == "rejected":
            content = f"已拒绝: {result.task.id} - {result.task.title}"
        else:  # revised
            content = (
                f"已修订: {result.task.id} - {result.task.title} | "
                f"修订指示: {normalized_note or ''}"
            )
        await self._write_lifecycle(result.task, content, card=None)

        # 5. Stable whitelist response (Registry result is the source of truth)
        return {
            "task_id": task_id,
            "decision": decision,
            "proposal_event_id": result.proposal_event_id,
            "note": normalized_note,
            "status": result.task.status.value,
        }

    # ------------------------------------------------------------------
    # cancel_task / retry_task (user actions)
    # ------------------------------------------------------------------

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        """User cancels a task.

        Allowed source states: QUEUED / RUNNING / WAITING_APPROVAL / FAILED.
        Terminal states (SUCCEEDED / CANCELLED) and EXPIRED are rejected.

        For RUNNING tasks, delegates to TaskRunService.terminate (when
        injected) to cancel the worker and release the claim via the unified
        run-finalization path. For non-RUNNING tasks, CAS-updates status to
        CANCELLED and writes a ``cancelled`` event.
        """
        task = await self.get_task(task_id)
        # Domain validation (raises TaskStateError for terminal / EXPIRED)
        target = task.cancel()

        if task.status == TaskStatus.RUNNING and self._run_service is not None:
            terminate = getattr(self._run_service, "terminate", None)
            if terminate is not None:
                try:
                    await terminate(task_id)
                    await self.registry.append_event(
                        task_id,
                        _EVENT_CANCELLED,
                        {"source": task.status.value},
                    )
                    return {
                        "task_id": task_id,
                        "status": TaskStatus.CANCELLED.value,
                    }
                except Exception as exc:
                    logger.warning(
                        "run_service.terminate failed for %s: %s",
                        task_id, exc,
                    )

        # Non-RUNNING path (or no run_service): CAS status -> CANCELLED.
        await self.registry.update_task(
            task_id,
            {"status": TaskStatus.CANCELLED},
            task.version,
        )
        await self._write_lifecycle(
            task, f"已取消: {task.id} - {task.title}", card=None,
        )
        await self.registry.append_event(
            task_id,
            _EVENT_CANCELLED,
            {"source": task.status.value},
        )
        return {
            "task_id": task_id,
            "status": TaskStatus.CANCELLED.value,
        }

    async def retry_task(self, task_id: str) -> dict[str, Any]:
        """User retries a FAILED or EXPIRED task.

        Returns the task to QUEUED (clearing claim fields via the domain
        method) and writes a ``retried`` event. ``is_archived`` is NOT
        changed.
        """
        task = await self.get_task(task_id)
        # Domain validation (raises TaskStateError unless FAILED / EXPIRED)
        updated = task.retry()
        await self.registry.update_task(
            task_id,
            {
                "status": updated.status,
                "claim_lock": None,
                "claim_expires": None,
                "current_run_id": None,
            },
            task.version,
        )
        await self.registry.append_event(
            task_id,
            _EVENT_RETRIED,
            {"source": task.status.value},
        )
        return {
            "task_id": task_id,
            "status": updated.status.value,
        }

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    async def add_comment(
        self,
        task_id: str,
        body: str,
        author: str = "worker",
    ) -> dict[str, Any]:
        await self.get_task(task_id)
        comment = await self.registry.add_comment(task_id, author, body)
        await self.registry.append_event(
            task_id, "comment_added",
            {"comment_id": comment.id, "author": author},
        )
        return {
            "id": comment.id,
            "task_id": comment.task_id,
            "author": comment.author,
            "body": comment.body,
        }

    async def list_comments(self, task_id: str) -> tuple[TaskComment, ...]:
        await self.get_task(task_id)
        return await self.registry.list_comments(task_id)

    # ------------------------------------------------------------------
    # Events / Runs
    # ------------------------------------------------------------------

    async def list_events(
        self, task_id: str, since: int = 0, limit: int = 100
    ) -> tuple[TaskEvent, ...]:
        await self.get_task(task_id)
        return await self.registry.list_events(task_id, since, limit)

    async def list_runs(self, task_id: str, limit: int = 50) -> tuple[TaskRun, ...]:
        await self.get_task(task_id)
        return await self.registry.list_runs(task_id, limit)

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    async def upload_attachment(
        self,
        task_id: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        uploaded_by: str = "",
    ) -> TaskAttachment:
        await self.get_task(task_id)
        self._validate_attachment_filename(filename)
        # Single config snapshot per upload (hot-reload).
        cfg = await self._snapshot()
        size = len(content)
        if size > cfg.task_attachment_max_bytes:
            raise TaskValidationError(
                f"attachment too large: {size} > {cfg.task_attachment_max_bytes}"
            )
        # Check total task attachment size
        existing = await self.registry.list_attachments(task_id)
        total = sum(a.size for a in existing) + size
        if total > cfg.task_attachment_task_max_bytes:
            raise TaskValidationError(
                f"task attachment total too large: {total} > {cfg.task_attachment_task_max_bytes}"
            )
        if self.attachments_root is None:
            raise TaskStateError("attachments_root not configured")

        checksum = "sha256:" + hashlib.sha256(content).hexdigest()
        stored_name = f"{uuid4().hex[:16]}_{filename}"
        task_dir = self.attachments_root / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Atomic write: temp file -> rename
        target = task_dir / stored_name
        # Security: resolve and verify within attachments_root
        resolved = target.resolve()
        if not resolved.is_relative_to(self.attachments_root.resolve()):
            raise TaskValidationError("attachment path escapes attachments_root")
        if resolved.is_symlink():
            raise TaskValidationError("symlink attachment not allowed")

        # Write to temp file, verify checksum, atomic rename
        fd, tmp_path = tempfile.mkstemp(dir=str(task_dir), prefix=".tmp_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            # Verify checksum of written file
            actual = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    actual.update(chunk)
            actual_checksum = "sha256:" + actual.hexdigest()
            if actual_checksum != checksum:
                os.unlink(tmp_path)
                raise TaskValidationError("attachment checksum mismatch after write")
            os.rename(tmp_path, str(target))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        attachment = await self.registry.add_attachment(
            task_id=task_id,
            filename=filename,
            stored_name=stored_name,
            content_type=content_type,
            size=size,
            checksum=checksum,
            uploaded_by=uploaded_by,
        )
        await self.registry.append_event(
            task_id, "attachment_uploaded",
            {"attachment_id": attachment.id, "filename": filename},
        )
        # T9: best-effort artifact register callback. Called after BOTH the
        # file write AND registry.add_attachment succeed; does NOT participate
        # in the attachment DB transaction. Failures only log a warning with
        # safe fields (source_kind, attachment_id, exception type).
        await self._invoke_artifact_register_callback(attachment)
        return attachment

    async def _invoke_artifact_register_callback(
        self, attachment: TaskAttachment,
    ) -> None:
        """Best-effort invocation of the artifact register callback (T9).

        Called after the attachment file write AND ``registry.add_attachment``
        both succeed. The callback does NOT participate in the attachment DB
        transaction. On failure, logs a warning with ONLY safe fields
        (source_kind, attachment_id, exception type) -- never content,
        stored_name, filename, or absolute paths. The upload return value is
        unchanged regardless of callback outcome.
        """
        if self._artifact_register_callback is None:
            return
        try:
            await self._artifact_register_callback(attachment)
        except Exception as exc:
            logger.warning(
                "artifact_register_callback failed: "
                "source_kind=task_attachment, attachment_id=%s, error_type=%s",
                attachment.id, type(exc).__name__,
            )

    async def list_attachments(self, task_id: str) -> tuple[TaskAttachment, ...]:
        await self.get_task(task_id)
        return await self.registry.list_attachments(task_id)

    async def get_attachment(self, attachment_id: str) -> TaskAttachment | None:
        return await self.registry.get_attachment(attachment_id)

    async def delete_attachment(self, attachment_id: str) -> bool:
        att = await self.registry.get_attachment(attachment_id)
        if att is None:
            return False
        deleted = await self.registry.delete_attachment(attachment_id)
        if deleted:
            path = self._attachment_path(att.task_id, att.stored_name)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "failed to delete attachment file %s: %s", path, exc
                    )
            await self.registry.append_event(
                att.task_id, "attachment_deleted",
                {"attachment_id": attachment_id},
            )
        return deleted

    def get_attachment_path(self, task_id: str, stored_name: str) -> Path | None:
        """Return the resolved path of an attachment, or None if not configured."""
        return self._attachment_path(task_id, stored_name)

    def _attachment_path(self, task_id: str, stored_name: str) -> Path | None:
        if self.attachments_root is None:
            return None
        path = self.attachments_root / task_id / stored_name
        resolved = path.resolve()
        if not resolved.is_relative_to(self.attachments_root.resolve()):
            return None
        return resolved

    def _validate_attachment_filename(self, filename: str) -> None:
        if not filename or not filename.strip():
            raise TaskValidationError("filename must not be empty")
        if _FILENAME_DOT_RE.match(filename):
            raise TaskValidationError("filename must not be dots-only")
        if not _FILENAME_SAFE_RE.match(filename):
            raise TaskValidationError(
                f"filename contains invalid characters: {filename!r}"
            )
        if "/" in filename or "\\" in filename:
            raise TaskValidationError("filename must not contain path separators")

    # ------------------------------------------------------------------
    # Notify subscriptions
    # ------------------------------------------------------------------

    async def subscribe_notify(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool:
        await self.get_task(task_id)
        return await self.registry.subscribe_notify(
            task_id, platform, chat_id, thread_id
        )

    async def list_notify_subs(self, task_id: str) -> tuple[dict[str, Any], ...]:
        await self.get_task(task_id)
        return await self.registry.list_notify_subs(task_id)

    async def unsubscribe_notify(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool:
        await self.get_task(task_id)
        return await self.registry.unsubscribe_notify(
            task_id, platform, chat_id, thread_id
        )

    # ------------------------------------------------------------------
    # Worker-facing ops
    # ------------------------------------------------------------------

    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        """Return full task detail for task_show tool / worker context.

        Includes task, comments, recent events, runs, attachments, and the
        worker_context string. Dependency-graph fields (parents/children/
        links) have been removed.
        """
        task = await self.registry.get_task(task_id)
        if task is None:
            return None
        comments = await self.registry.list_comments(task_id)
        events = await self.registry.list_events(
            task_id, limit=_MAX_EVENTS_IN_DETAIL
        )
        runs = await self.registry.list_runs(task_id, limit=_MAX_RUNS_IN_DETAIL)
        attachments = await self.registry.list_attachments(task_id)
        # Resolve artifact_id for each attachment (best-effort, concurrent).
        artifact_ids: dict[str, str | None] = {}
        if self._artifact_id_lookup:
            results = await asyncio.gather(
                *(self._artifact_id_lookup(a.id) for a in attachments),
                return_exceptions=True,
            )
            for a, r in zip(attachments, results):
                artifact_ids[a.id] = r if isinstance(r, str) else None
        worker_context = await self.build_worker_context(task)
        return {
            "task": _task_to_dict(task),
            "comments": [
                {
                    "id": c.id,
                    "author": c.author,
                    "body": c.body,
                    "created_at": _dt_str(c.created_at),
                }
                for c in comments
            ],
            "events": [
                {
                    "id": e.id,
                    "kind": e.kind,
                    "payload": e.payload,
                    "run_id": e.run_id,
                    "created_at": _dt_str(e.created_at),
                }
                for e in events
            ],
            "runs": [
                {
                    "id": r.id,
                    "status": r.status.value,
                    "outcome": r.outcome.value if r.outcome else None,
                    "summary": r.summary,
                    "error": r.error,
                    "started_at": _dt_str(r.started_at),
                    "ended_at": _dt_str(r.ended_at),
                }
                for r in runs
            ],
            "attachments": [
                {
                    "id": a.id,
                    "filename": a.filename,
                    "content_type": a.content_type,
                    "size": a.size,
                    "artifact_id": artifact_ids.get(a.id),
                }
                for a in attachments
            ],
            "worker_context": worker_context,
        }

    async def complete(
        self,
        task_id: str,
        summary: str,
        metadata: dict[str, Any],
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Submit completion intent. Does NOT finalize the run.

        Validates the task is RUNNING, appends a ``complete_requested`` audit
        event (non-terminal), and returns the intent dict. TaskRunService
        reads the latest intent event and performs the CAS finalize.
        """
        task = await self.get_task(task_id)
        if task.status != TaskStatus.RUNNING:
            raise TaskStateError(
                f"complete requires RUNNING, got {task.status.value}"
            )
        if not task.claim_lock or task.current_run_id is None:
            raise TaskStateError("task has no active claim")

        # Validate artifacts JSON-serializable
        try:
            json.dumps(artifacts, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TaskValidationError(
                f"artifacts not serializable: {exc}"
            ) from exc

        # Pre-flight: a ``workspace:`` storage_ref must resolve to a readable
        # file at the workspace root BEFORE the run finalizes. Workers that
        # write the file to the sandbox cwd via open() (ephemeral scratch)
        # instead of the write_file callback (which writes to workspace root)
        # would otherwise have their artifact silently dropped post-finalize
        # while the task is marked succeeded. Reject here so the worker sees
        # the error and self-corrects (use write_file, or inline ``content``).
        if self._workspace_ref_validator is not None and isinstance(artifacts, list):
            for art in artifacts:
                if not isinstance(art, dict):
                    continue
                ref = art.get("storage_ref")
                if isinstance(ref, str) and ref.startswith("workspace:"):
                    name = art.get("name") or "(unnamed)"
                    try:
                        await self._workspace_ref_validator(ref)
                    except Exception as exc:
                        raise TaskValidationError(
                            f"artifact '{name}' references workspace file "
                            f"'{ref}' which is not readable at the workspace "
                            f"root. Write the file via write_file(path='{ref[len('workspace:'):]}'"
                            f", content=...) before task_complete, or put the "
                            f"text content directly in the artifact's 'content' "
                            f"field. Files written to the sandbox cwd via open() "
                            f"are NOT referenceable as workspace: refs."
                        ) from exc

        intent = {
            "outcome": TaskRunOutcome.COMPLETED.value,
            "summary": summary,
            "metadata": metadata,
            "artifacts": artifacts,
            "task_id": task_id,
            "run_id": task.current_run_id,
        }
        await self.registry.append_event(
            task_id, _INTENT_COMPLETE, intent, run_id=task.current_run_id,
        )
        return intent

    async def fail(
        self,
        task_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Submit worker fast-fail intent. Does NOT finalize the run.

        Worker 判定任务无法继续、不再重试（如必需工具不可用、任务指令禁止兜底）
        时调用。Validates the task is RUNNING, appends a ``fail_requested`` audit
        event (non-terminal), returns the intent dict. TaskRunService reads the
        latest intent event and performs the CAS finalize with outcome=ABORTED
        -> task FAILED（绕过断路器，不重试）。取消（CANCELLED）只认用户指令，
        worker 不得用本方法触发取消语义。
        """
        task = await self.get_task(task_id)
        if task.status != TaskStatus.RUNNING:
            raise TaskStateError(
                f"fail requires RUNNING, got {task.status.value}"
            )
        if not task.claim_lock or task.current_run_id is None:
            raise TaskStateError("task has no active claim")

        intent = {
            "outcome": TaskRunOutcome.ABORTED.value,
            "error": reason or "worker aborted",
            "task_id": task_id,
            "run_id": task.current_run_id,
        }
        await self.registry.append_event(
            task_id, _INTENT_FAIL, intent, run_id=task.current_run_id,
        )
        return intent

    async def heartbeat(self, task_id: str, note: str) -> dict[str, Any]:
        """Record heartbeat (CAS on claim_lock) and renew lease."""
        task = await self.get_task(task_id)
        if task.status != TaskStatus.RUNNING:
            raise TaskStateError(
                f"heartbeat requires RUNNING, got {task.status.value}"
            )
        if not task.claim_lock or task.current_run_id is None:
            raise TaskStateError("task has no active claim")
        now = datetime.now(timezone.utc)
        await self.registry.record_heartbeat(
            task_id, task.current_run_id, task.claim_lock, now,
        )
        if note:
            await self.registry.append_event(
                task_id, "heartbeat",
                {"note": note}, run_id=task.current_run_id,
            )
        return {"task_id": task_id, "heartbeat_at": _dt_str(now)}

    # ------------------------------------------------------------------
    # build_worker_context (async; queries events for proposal/decision/
    # progress segments)
    # ------------------------------------------------------------------

    async def build_worker_context(self, task: Task) -> str:
        """Construct worker context string for the system prompt.

        Includes:
          - Title + body
          - Meta (priority, scheduled_at)
          - Goal mode / skills (if set)
          - Identity (task_id, status, is_archived)
          - 待审批提案: latest unresolved ``change_proposed`` event
          - 审批决策: latest ``change_approved`` / ``change_rejected`` /
            ``change_revised`` event
          - 进度: recent events (comment/complete/propose/approve/reject/
            revise/retry/cancel/finished)

        Host absolute paths (e.g. workspace_path) are NOT emitted.
        """
        segments: list[str] = []
        # Title + body
        segments.append(f"# Task: {task.title}")
        if task.body:
            body = _truncate(task.body, _CONTEXT_BODY_LIMIT)
            segments.append(f"\n## Body\n{body}")
        # Meta
        meta_parts: list[str] = [f"priority: {task.priority}"]
        if task.scheduled_at is not None:
            meta_parts.append(f"scheduled_at: {_dt_str(task.scheduled_at)}")
        segments.append(f"\n## Meta\n{', '.join(meta_parts)}")
        # Execution hints
        if task.goal_mode:
            turns = task.goal_max_turns or "unlimited"
            segments.append(f"\n## Goal Mode\nmax_turns: {turns}")
        if task.skills:
            segments.append(f"\n## Skills\n{', '.join(task.skills)}")
        # Identity
        identity_parts = [
            f"task_id: {task.id}",
            f"status: {task.status.value}",
        ]
        if task.is_archived:
            identity_parts.append("is_archived: true")
        segments.append(f"\n## Identity\n{', '.join(identity_parts)}")

        # Event-based segments (require registry query)
        try:
            events = await self.registry.list_events(
                task.id, limit=_MAX_EVENTS_IN_DETAIL
            )
        except Exception:
            events = ()

        # 待审批提案: latest unresolved change_proposed
        proposal_text = self._latest_open_proposal_text(events)
        if proposal_text is not None:
            truncated = _truncate(proposal_text, _CONTEXT_PROPOSAL_LIMIT)
            segments.append(f"\n## 待审批提案\n{truncated}")

        # 审批决策: latest change_approved / change_rejected
        decision = self._latest_decision_text(events)
        if decision is not None:
            truncated = _truncate(decision, _CONTEXT_DECISION_LIMIT)
            segments.append(f"\n## 审批决策\n{truncated}")

        # 进度: recent progress events
        progress_lines = self._progress_lines(events)
        if progress_lines:
            body = "\n".join(progress_lines)
            truncated = _truncate(body, _CONTEXT_PROGRESS_LIMIT)
            segments.append(f"\n## 进度\n{truncated}")

        return "\n".join(segments)

    def _latest_open_proposal_text(
        self, events: tuple[TaskEvent, ...]
    ) -> str | None:
        """Return the proposal text of the latest unresolved proposal.

        "Unresolved" = no later ``change_approved``/``change_rejected``/
        ``change_revised`` event references this proposal's id via its
        ``proposal_event_id`` payload. The precise id match (not marker
        time) correctly handles interleaved proposals and markers.
        """
        for event in reversed(events):
            if event.kind != _EVENT_CHANGE_PROPOSED:
                continue
            resolved = any(
                e.id > event.id
                and e.kind in (
                    _EVENT_CHANGE_APPROVED,
                    _EVENT_CHANGE_REJECTED,
                    _EVENT_CHANGE_REVISED,
                )
                and e.payload.get("proposal_event_id") == event.id
                for e in events
            )
            if not resolved:
                return str(event.payload.get("proposal", ""))
        return None

    def _latest_decision_text(
        self, events: tuple[TaskEvent, ...]
    ) -> str | None:
        """Return a human-readable line for the latest approval decision."""
        for event in reversed(events):
            if event.kind == _EVENT_CHANGE_APPROVED:
                base = f"approved (proposal_event_id={event.payload.get('proposal_event_id')})"
                note = event.payload.get("note")
                return base + (f", note={note}" if note else "")
            if event.kind == _EVENT_CHANGE_REJECTED:
                base = f"rejected (proposal_event_id={event.payload.get('proposal_event_id')})"
                note = event.payload.get("note")
                return base + (f", note={note}" if note else "")
            if event.kind == _EVENT_CHANGE_REVISED:
                base = f"revised (proposal_event_id={event.payload.get('proposal_event_id')})"
                note = event.payload.get("note")
                return base + (f", note={note}" if note else "")
        return None

    def _progress_lines(
        self, events: tuple[TaskEvent, ...]
    ) -> list[str]:
        """Return formatted lines for the latest N progress events."""
        relevant = [
            e for e in events if e.kind in _PROGRESS_EVENT_KINDS
        ]
        recent = relevant[-_MAX_PROGRESS_EVENTS:]
        lines: list[str] = []
        for event in recent:
            ts = _dt_str(event.created_at) or ""
            if event.kind == "comment_added":
                author = event.payload.get("author", "?")
                lines.append(f"[{ts}] comment by {author}")
            elif event.kind == _INTENT_COMPLETE:
                summary = event.payload.get("summary", "")
                lines.append(f"[{ts}] complete_requested: {summary}")
            elif event.kind == _INTENT_FAIL:
                reason = event.payload.get("error", "")
                lines.append(f"[{ts}] fail_requested: {reason}")
            elif event.kind == _EVENT_CHANGE_PROPOSED:
                proposal = event.payload.get("proposal", "")
                lines.append(f"[{ts}] propose_change: {proposal}")
            elif event.kind == _EVENT_CHANGE_APPROVED:
                note = event.payload.get("note")
                lines.append(f"[{ts}] change_approved" + (f": {note}" if note else ""))
            elif event.kind == _EVENT_CHANGE_REJECTED:
                note = event.payload.get("note")
                lines.append(f"[{ts}] change_rejected" + (f": {note}" if note else ""))
            elif event.kind == _EVENT_CHANGE_REVISED:
                note = event.payload.get("note")
                lines.append(f"[{ts}] change_revised" + (f": {note}" if note else ""))
            elif event.kind == _EVENT_CANCELLED:
                lines.append(f"[{ts}] cancelled")
            elif event.kind == _EVENT_RETRIED:
                lines.append(f"[{ts}] retried")
            elif event.kind == "finished":
                outcome = event.payload.get("outcome", "")
                lines.append(f"[{ts}] run finished: {outcome}")
            elif event.kind == "goal_judge_feedback":
                reason = event.payload.get("reason", "")
                turn = event.payload.get("turn", "")
                lines.append(f"[{ts}] goal_judge_feedback (turn {turn}): {reason}")
        return lines

    # ------------------------------------------------------------------
    # Dispatch delegation
    # ------------------------------------------------------------------

    async def dispatch_tick(self) -> dict[str, Any]:
        """Delegate a single dispatch tick to TaskRunService.

        TaskService itself does not hold a TaskRunService reference (circular
        dependency); the caller (main.py wiring or CLI) sets
        ``_run_service`` via the ``set_run_service`` setter.
        """
        if self._run_service is None:
            raise TaskStateError("run_service not configured")
        return await self._run_service.dispatch_once()

    def set_run_service(self, run_service: Any) -> None:
        """Inject TaskRunService (breaks circular dependency at wiring time)."""
        self._run_service = run_service

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _find_by_idempotency(
        self, board: str, created_by: str, idempotency_key: str
    ) -> Task | None:
        """Find an existing task by (board, created_by, idempotency_key).

        Uses list_tasks to scan; production wiring may add a dedicated
        registry lookup. For now, the partial unique index on the DB ensures
        uniqueness, and we catch the conflict on create_task.
        """
        # The registry's create_task raises TaskConflictError on duplicate
        # idempotency. We handle that in create_task instead of pre-scanning.
        return None


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _dt_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _truncate(text: str, limit: int) -> str:
    """Truncate text to approximately ``limit`` bytes (UTF-8)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore") + "…"


def _task_to_dict(task: Task) -> dict[str, Any]:
    """Serialize a Task to a JSON-safe dict (for tool output / API response).

    Manus-aligned: outputs the 7-value ``status``, ``is_archived``,
    ``scheduled_at``, claim summary, and failure summary. Removed fields:
    ``assignee``, ``pre_archive_status``, ``block_kind``, ``block_reason``,
    dependency-graph, swarm.
    """
    return {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "priority": task.priority,
        "status": task.status.value,
        "board": task.board,
        "version": task.version,
        "created_by": task.created_by,
        "created_at": _dt_str(task.created_at),
        "updated_at": _dt_str(task.updated_at),
        "scheduled_at": _dt_str(task.scheduled_at),
        "started_at": _dt_str(task.started_at),
        "completed_at": _dt_str(task.completed_at),
        "is_archived": task.is_archived,
        # Claim summary
        "current_run_id": task.current_run_id,
        "claim_expires": _dt_str(task.claim_expires),
        "last_heartbeat_at": _dt_str(task.last_heartbeat_at),
        # Failure summary
        "consecutive_failures": task.consecutive_failures,
        "max_retries": task.max_retries,
        "last_failure_error": task.last_failure_error,
        # Execution config
        "goal_mode": task.goal_mode,
        "goal_max_turns": task.goal_max_turns,
        "workspace_kind": task.workspace_kind.value,
        "workspace_path": task.workspace_path,
        "skills": list(task.skills),
        "allowed_tools": list(task.execution_policy.allowed_tools),
        "model_override": task.model_override,
        "max_runtime_seconds": task.max_runtime_seconds,
        # Sessions
        "origin_session_id": task.origin_session_id,
        "execution_session_id": task.execution_session_id,
        # Result
        "result": task.result,
    }
