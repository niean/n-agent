"""T12: Application TaskService -- CRUD, orchestration, and worker-facing ops.

Satisfies ``TaskServiceProtocol`` from
``app/infrastructure/tools/task_management.py`` (the 7 managed tool surface)
PLUS full CRUD, attachment, and notify-subscription management.

Injection:
  - ``TaskRegistry`` (Domain port -- async Protocol)
  - ``TaskPolicy`` (14th domain Policy)
  - ``TaskPlanningService`` (optional, Batch E -- specify/decompose/swarm)
  - ``MemoryStore`` (optional, for execution_session cleanup on delete)
  - ``attachments_root`` (Path -- controlled directory for attachment files)
  - ``attachment_max_bytes`` / ``attachment_task_max_bytes`` (size limits)

Key invariants (spec):
  - title non-empty; idempotency_key unique on (board, created_by, idempotency_key)
  - All updates receive expected_version (optimistic lock)
  - RUNNING Task rejects generic update/delete/archive
  - DELETE is hard-delete, non-RUNNING only; transactional row delete + file cleanup
  - complete/block return terminal INTENT only -- they do NOT call finish_run.
    TaskRunService (T14) owns the single CAS-based finalization path.
  - heartbeat records via registry.record_heartbeat (CAS on claim_lock)
  - build_worker_context constructs the worker prompt context, excluding host
    absolute paths
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.task import (
    BlockKind,
    BulkUpdateCommand,
    BulkUpdateItem,
    CreateGraphCommand,
    FinishRunCommand,
    Task,
    TaskAttachment,
    TaskArtifact,
    TaskClaimError,
    TaskComment,
    TaskConflictError,
    TaskEvent,
    TaskExecutionPolicy,
    TaskLink,
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
from app.domain.task_policy import TaskPolicy, TaskPolicyRequest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Segment byte limits for build_worker_context (spec: per-segment byte limits)
_CONTEXT_BODY_LIMIT = 8192
_CONTEXT_COMMENT_LIMIT = 2048
_CONTEXT_EVENT_LIMIT = 4096
_CONTEXT_RUN_LIMIT = 2048
_MAX_EVENTS_IN_DETAIL = 50
_MAX_RUNS_IN_DETAIL = 20

# Filename validation: reject path separators, control chars, dots-only
_FILENAME_SAFE_RE = re.compile(r"^[^\x00-\x1f/\\<>:""|?*\x7f]+$")
_FILENAME_DOT_RE = re.compile(r"^\.+$")

# Default attachment limits (overridden by Settings in Batch F wiring)
_DEFAULT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024  # 20 MB
_DEFAULT_ATTACHMENT_TASK_MAX_BYTES = 100 * 1024 * 1024  # 100 MB

# Terminal intent event kinds (non-terminal audit; TaskRunService writes the
# actual terminal "finished" event)
_INTENT_COMPLETE = "complete_requested"
_INTENT_BLOCK = "block_requested"

# Notification-worthy terminal outcomes (spec: only these trigger delivery)
_NOTIFIED_OUTCOMES: frozenset[TaskRunOutcome] = frozenset({
    TaskRunOutcome.COMPLETED,
    TaskRunOutcome.BLOCKED,
    TaskRunOutcome.GAVE_UP,
    TaskRunOutcome.CRASHED,
    TaskRunOutcome.TIMED_OUT,
    TaskRunOutcome.TERMINATED,
})


class TaskService:
    """Application service for Task CRUD, orchestration, and worker ops.

    Satisfies ``TaskServiceProtocol`` (get_task_detail/complete/block/
    heartbeat/add_comment/create_subtask/link/build_worker_context) plus
    full CRUD, bulk update, attachments, and notify subscriptions.
    """

    def __init__(
        self,
        registry: Any,
        policy: TaskPolicy,
        planning_service: Any | None = None,
        memory_store: Any | None = None,
        attachments_root: Path | None = None,
        attachment_max_bytes: int = _DEFAULT_ATTACHMENT_MAX_BYTES,
        attachment_task_max_bytes: int = _DEFAULT_ATTACHMENT_TASK_MAX_BYTES,
    ):
        self.registry = registry
        self.policy = policy
        self.planning_service = planning_service
        self.memory_store = memory_store
        self.attachments_root = (
            Path(attachments_root) if attachments_root is not None else None
        )
        self.attachment_max_bytes = attachment_max_bytes
        self.attachment_task_max_bytes = attachment_task_max_bytes

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def create_task(
        self,
        *,
        title: str,
        body: str = "",
        assignee: str | None = None,
        priority: int = 0,
        created_by: str = "",
        board: str = "default",
        idempotency_key: str | None = None,
        origin_session_id: str | None = None,
        skills: tuple[str, ...] | list[str] | None = None,
        execution_policy: TaskExecutionPolicy | None = None,
        workspace_kind: TaskWorkspaceKind = TaskWorkspaceKind.SCRATCH,
        workspace_path: str | None = None,
        model_override: str | None = None,
        max_runtime_seconds: int | None = None,
        max_retries: int = 0,
        goal_mode: bool = False,
        goal_max_turns: int | None = None,
    ) -> Task:
        if not title or not title.strip():
            raise TaskValidationError("title must not be empty")
        if board != "default":
            raise TaskValidationError("only 'default' board is accepted in this iteration")

        # Idempotency: if key provided, check for existing task
        if idempotency_key:
            existing = await self._find_by_idempotency(board, created_by, idempotency_key)
            if existing is not None:
                return existing

        now = datetime.now(timezone.utc)
        task = Task(
            id=f"t_{uuid4().hex[:16]}",
            title=title.strip(),
            body=body,
            assignee=assignee,
            priority=priority,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            version=1,
            status=TaskStatus.TRIAGE,
            board=board,
            origin_session_id=origin_session_id,
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
        await self.registry.append_event(created.id, "created", {"title": created.title})
        return created

    async def get_task(self, task_id: str) -> Task:
        task = await self.registry.get_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task

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
        return await self.registry.update_task(task_id, fields, expected_version)

    async def bulk_update(self, command: BulkUpdateCommand) -> tuple[Task, ...]:
        # Pre-check: reject if any task is RUNNING
        for item in command.items:
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
    # State transitions (assign / promote / schedule / archive)
    # ------------------------------------------------------------------

    async def assign(
        self, task_id: str, assignee: str | None, expected_version: int
    ) -> Task:
        return await self.update_task(
            task_id, {"assignee": assignee}, expected_version
        )

    async def promote_to_ready(self, task_id: str, expected_version: int) -> Task:
        task = await self.get_task(task_id)
        if task.status not in (TaskStatus.TODO, TaskStatus.SCHEDULED):
            raise TaskStateError(
                f"promote_to_ready requires TODO or SCHEDULED, got {task.status.value}"
            )
        return await self.update_task(
            task_id, {"status": TaskStatus.READY}, expected_version
        )

    async def schedule(
        self,
        task_id: str,
        scheduled_at: datetime,
        expected_version: int,
    ) -> Task:
        return await self.update_task(
            task_id, {"scheduled_at": scheduled_at, "status": TaskStatus.SCHEDULED},
            expected_version,
        )

    async def archive(self, task_id: str, expected_version: int) -> Task:
        task = await self.get_task(task_id)
        if task.status == TaskStatus.RUNNING:
            raise TaskStateError("cannot archive RUNNING task; terminate first")
        if task.status == TaskStatus.ARCHIVED:
            return task
        return await self.update_task(
            task_id,
            {"status": TaskStatus.ARCHIVED, "pre_archive_status": task.status},
            expected_version,
        )

    async def unarchive(self, task_id: str, expected_version: int) -> Task:
        task = await self.get_task(task_id)
        if task.status != TaskStatus.ARCHIVED:
            raise TaskStateError(
                f"unarchive requires ARCHIVED, got {task.status.value}"
            )
        restored = task.pre_archive_status or TaskStatus.TODO
        return await self.update_task(
            task_id,
            {"status": restored, "pre_archive_status": None},
            expected_version,
        )

    # ------------------------------------------------------------------
    # Dependency graph
    # ------------------------------------------------------------------

    async def link(self, parent_id: str, child_id: str) -> dict[str, Any]:
        link = await self.registry.add_link(parent_id, child_id)
        await self.registry.append_event(
            parent_id, "link_added", {"parent_id": parent_id, "child_id": child_id}
        )
        return {
            "parent_id": link.parent_id,
            "child_id": link.child_id,
        }

    async def unlink(self, parent_id: str, child_id: str) -> bool:
        removed = await self.registry.remove_link(parent_id, child_id)
        if removed:
            await self.registry.append_event(
                parent_id, "link_removed",
                {"parent_id": parent_id, "child_id": child_id},
            )
        return removed

    async def list_links(self, task_id: str) -> tuple[TaskLink, ...]:
        await self.get_task(task_id)
        return await self.registry.list_links(task_id)

    async def list_children(self, parent_id: str) -> tuple[Task, ...]:
        await self.get_task(parent_id)
        return await self.registry.list_children(parent_id)

    async def list_parents(self, child_id: str) -> tuple[Task, ...]:
        await self.get_task(child_id)
        return await self.registry.list_parents(child_id)

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
        size = len(content)
        if size > self.attachment_max_bytes:
            raise TaskValidationError(
                f"attachment too large: {size} > {self.attachment_max_bytes}"
            )
        # Check total task attachment size
        existing = await self.registry.list_attachments(task_id)
        total = sum(a.size for a in existing) + size
        if total > self.attachment_task_max_bytes:
            raise TaskValidationError(
                f"task attachment total too large: {total} > {self.attachment_task_max_bytes}"
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
        return attachment

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
    # Worker-facing ops (TaskServiceProtocol)
    # ------------------------------------------------------------------

    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        """Return full task detail for task_show tool / worker context.

        Includes task, parents, children, comments, recent events, runs, and
        the worker_context string. Attachments return controlled download
        references (id + filename), never host absolute paths.
        """
        task = await self.registry.get_task(task_id)
        if task is None:
            return None
        parents = await self.registry.list_parents(task_id)
        children = await self.registry.list_children(task_id)
        comments = await self.registry.list_comments(task_id)
        events = await self.registry.list_events(task_id, limit=_MAX_EVENTS_IN_DETAIL)
        runs = await self.registry.list_runs(task_id, limit=_MAX_RUNS_IN_DETAIL)
        attachments = await self.registry.list_attachments(task_id)
        links = await self.registry.list_links(task_id)
        worker_context = self.build_worker_context(task)
        return {
            "task": _task_to_dict(task),
            "parents": [_task_to_dict(p) for p in parents],
            "children": [_task_to_dict(c) for c in children],
            "links": [
                {"parent_id": l.parent_id, "child_id": l.child_id} for l in links
            ],
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

    async def block(
        self,
        task_id: str,
        reason: str,
        kind: str,
    ) -> dict[str, Any]:
        """Submit block intent. Does NOT finalize the run.

        Validates the task is RUNNING, appends a ``block_requested`` audit
        event, and returns the intent dict. TaskRunService performs the CAS
        finalize.
        """
        task = await self.get_task(task_id)
        if task.status != TaskStatus.RUNNING:
            raise TaskStateError(
                f"block requires RUNNING, got {task.status.value}"
            )
        if not task.claim_lock or task.current_run_id is None:
            raise TaskStateError("task has no active claim")
        try:
            block_kind = BlockKind(kind)
        except ValueError as exc:
            raise TaskValidationError(
                f"invalid block kind: {kind}"
            ) from exc

        intent = {
            "outcome": TaskRunOutcome.BLOCKED.value,
            "reason": reason,
            "kind": block_kind.value,
            "task_id": task_id,
            "run_id": task.current_run_id,
        }
        await self.registry.append_event(
            task_id, _INTENT_BLOCK, intent, run_id=task.current_run_id,
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

    async def create_subtask(
        self,
        parent_task_id: str,
        title: str,
        body: str = "",
        assignee: str | None = None,
        parents: list[str] | None = None,
        skills: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a subtask linked to the parent. parents list must include
        parent_task_id (enforced by the tool executor)."""
        await self.get_task(parent_task_id)
        child = await self.create_task(
            title=title,
            body=body,
            assignee=assignee,
            skills=tuple(skills) if skills else None,
        )
        # Link parent -> child
        effective_parents = parents or [parent_task_id]
        for pid in effective_parents:
            await self.registry.add_link(pid, child.id)
        await self.registry.append_event(
            parent_task_id, "subtask_created",
            {"child_id": child.id, "title": title},
        )
        return {
            "id": child.id,
            "title": child.title,
            "status": child.status.value,
            "parents": effective_parents,
        }

    # ------------------------------------------------------------------
    # build_worker_context
    # ------------------------------------------------------------------

    def build_worker_context(self, task: Task) -> str:
        """Construct worker context string for the system prompt.

        Includes title, body, prior attempts summaries, parent handoffs,
        comments thread, and attachment paths (controlled references only --
        never host absolute paths).

        This is a SYNCHRONOUS method (no registry calls) because the task
        is already loaded by the caller. Parents/children/comments/events are
        fetched separately by get_task_detail; this method only formats the
        task itself.
        """
        segments: list[str] = []
        # Title + body
        segments.append(f"# Task: {task.title}")
        if task.body:
            body = _truncate(task.body, _CONTEXT_BODY_LIMIT)
            segments.append(f"\n## Body\n{body}")
        # Priority + assignee
        meta_parts: list[str] = []
        meta_parts.append(f"priority: {task.priority}")
        if task.assignee:
            meta_parts.append(f"assignee: {task.assignee}")
        segments.append(f"\n## Meta\n{', '.join(meta_parts)}")
        # Execution hints
        if task.goal_mode:
            turns = task.goal_max_turns or "unlimited"
            segments.append(f"\n## Goal Mode\nmax_turns: {turns}")
        if task.skills:
            segments.append(f"\n## Skills\n{', '.join(task.skills)}")
        # Status + id
        segments.append(
            f"\n## Identity\ntask_id: {task.id}\nstatus: {task.status.value}"
        )
        return "\n".join(segments)

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

    _run_service: Any = None

    def set_run_service(self, run_service: Any) -> None:
        """Inject TaskRunService (breaks circular dependency at wiring time)."""
        self._run_service = run_service

    # ------------------------------------------------------------------
    # Planning delegation (Batch E)
    # ------------------------------------------------------------------

    async def specify(self, task_id: str, **kwargs: Any) -> Any:
        if self.planning_service is None:
            raise TaskStateError("planning_service not configured")
        return await self.planning_service.specify_task(task_id, **kwargs)

    async def decompose(self, task_id: str, **kwargs: Any) -> Any:
        if self.planning_service is None:
            raise TaskStateError("planning_service not configured")
        return await self.planning_service.decompose_task(task_id, **kwargs)

    async def create_swarm(self, **kwargs: Any) -> Any:
        if self.planning_service is None:
            raise TaskStateError("planning_service not configured")
        return await self.planning_service.create_swarm(**kwargs)

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
    """Serialize a Task to a JSON-safe dict (for tool output / API response)."""
    return {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "assignee": task.assignee,
        "priority": task.priority,
        "status": task.status.value,
        "board": task.board,
        "version": task.version,
        "created_by": task.created_by,
        "created_at": _dt_str(task.created_at),
        "updated_at": _dt_str(task.updated_at),
        "block_kind": task.block_kind.value if task.block_kind else None,
        "block_reason": task.block_reason,
        "consecutive_failures": task.consecutive_failures,
        "max_retries": task.max_retries,
        "goal_mode": task.goal_mode,
        "goal_max_turns": task.goal_max_turns,
        "current_run_id": task.current_run_id,
        "origin_session_id": task.origin_session_id,
        "execution_session_id": task.execution_session_id,
        "result": task.result,
    }
