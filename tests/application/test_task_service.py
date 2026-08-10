"""T4: TaskService tests (Manus-aligned 7-state machine).

Tests CRUD, idempotency, optimistic lock, RUNNING guards, attachments,
notify subs, build_worker_context, and the new user-action surface
(propose_change / approve_change / reject_change / cancel_task /
retry_task).

Uses an in-memory FakeTaskRegistry to isolate from SQLite.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.application.task_service import TaskService, _task_to_dict
from app.domain.task import (
    BulkUpdateCommand,
    BulkUpdateItem,
    ClaimResult,
    FinishRunCommand,
    FinishRunResult,
    ProposalResolutionCommand,
    ProposalResolutionResult,
    RecoverRunCommand,
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
    TaskRunStatus,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    TaskWorkspaceKind,
)
from app.domain.task_policy import TaskPolicy
from app.domain.artifact import ArtifactContentUnavailableError


# ---------------------------------------------------------------------------
# Fake registry (in-memory, Manus-aligned 7-state)
# ---------------------------------------------------------------------------


class FakeTaskRegistry:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._runs: dict[int, TaskRun] = {}
        self._events: list[TaskEvent] = []
        self._comments: dict[str, list[TaskComment]] = {}
        self._attachments: dict[str, list] = {}
        self._notify_subs: list[dict] = []
        self._next_run_id = 1
        self._next_event_id = 1
        # T3: track Registry port calls for "validation failure does not
        # access Registry" assertions.
        self.resolve_proposal_calls: list[ProposalResolutionCommand] = []
        self.latest_waiting_approval_calls: list[str] = []
        self.get_task_calls: list[str] = []

    async def create_task(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise TaskConflictError(f"duplicate id: {task.id}")
        if task.idempotency_key:
            for existing in self._tasks.values():
                if (
                    existing.board == task.board
                    and existing.created_by == task.created_by
                    and existing.idempotency_key == task.idempotency_key
                ):
                    raise TaskConflictError("duplicate idempotency_key")
        self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> Task | None:
        self.get_task_calls.append(task_id)
        return self._tasks.get(task_id)

    async def list_tasks(self, board="default", cursor=None, limit=100,
                         include_archived=False):
        items = [
            t for t in self._tasks.values()
            if t.board == board and (include_archived or not t.is_archived)
        ]
        items.sort(key=lambda t: (t.created_at or datetime.min, t.id))
        return TaskListPage(items=tuple(items[:limit]))

    async def update_task(self, task_id, fields, expected_version):
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.version != expected_version:
            raise TaskConflictError("version conflict")
        from dataclasses import replace as dc_replace
        updated = dc_replace(
            task, **dict(fields), version=task.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
        self._tasks[task_id] = updated
        return updated

    async def bulk_update(self, command):
        updated = []
        for item in command.items:
            t = await self.update_task(
                item.task_id, item.fields, item.expected_version
            )
            updated.append(t)
        from app.domain.task import BulkUpdateResult
        return BulkUpdateResult(updated=tuple(updated))

    async def delete_task(self, task_id):
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        return True

    async def claim_task(self, task_id, claim_lock, lease_seconds):
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.QUEUED:
            return None
        if task.is_archived:
            return None
        now = datetime.now(timezone.utc)
        if task.scheduled_at is not None and task.scheduled_at > now:
            return None
        from dataclasses import replace as dc_replace
        from datetime import timedelta
        run_id = self._next_run_id
        self._next_run_id += 1
        expires = now + timedelta(seconds=lease_seconds)
        run = TaskRun(
            id=run_id, task_id=task_id, status=TaskRunStatus.RUNNING,
            claim_lock=claim_lock, claim_expires=expires,
            started_at=now, lease_seconds=lease_seconds,
        )
        self._runs[run_id] = run
        updated = dc_replace(
            task, status=TaskStatus.RUNNING, claim_lock=claim_lock,
            claim_expires=expires, current_run_id=run_id,
            worker_token=f"wt_{run_id}", last_heartbeat_at=now,
            started_at=now, version=task.version + 1,
        )
        self._tasks[task_id] = updated
        return ClaimResult(task=updated, run=run)

    async def record_heartbeat(self, task_id, run_id, claim_lock, now):
        task = self._tasks.get(task_id)
        if task is None or task.claim_lock != claim_lock or task.current_run_id != run_id:
            raise TaskClaimError("heartbeat CAS failed")
        from dataclasses import replace as dc_replace
        updated = dc_replace(task, last_heartbeat_at=now, version=task.version + 1)
        self._tasks[task_id] = updated
        return updated

    async def finish_run(self, command: FinishRunCommand):
        task = self._tasks.get(command.task_id)
        if task is None:
            raise TaskNotFoundError(command.task_id)
        run = self._runs.get(command.run_id)
        if run is None:
            raise TaskNotFoundError(f"run not found: {command.run_id}")
        if task.claim_lock != command.claim_lock or task.current_run_id != command.run_id:
            raise TaskClaimError("finish CAS failed")
        from dataclasses import replace as dc_replace
        now = datetime.now(timezone.utc)
        if command.target_task_status is not None:
            new_status = command.target_task_status
        elif command.outcome == TaskRunOutcome.COMPLETED:
            new_status = TaskStatus.SUCCEEDED
        elif command.outcome == TaskRunOutcome.WAITING_APPROVAL:
            new_status = TaskStatus.WAITING_APPROVAL
        elif command.outcome == TaskRunOutcome.TERMINATED:
            new_status = TaskStatus.CANCELLED
        elif command.outcome == TaskRunOutcome.EXPIRED:
            new_status = TaskStatus.EXPIRED
        else:
            # retryable failure
            new_status = TaskStatus.QUEUED
        failures = task.consecutive_failures
        if command.outcome in (TaskRunOutcome.FAILED, TaskRunOutcome.SPAWN_FAILED):
            failures += 1
        elif command.outcome == TaskRunOutcome.COMPLETED:
            failures = 0
        updated_task = dc_replace(
            task, status=new_status, claim_lock=None, claim_expires=None,
            current_run_id=None, worker_token=None,
            consecutive_failures=failures,
            version=task.version + 1, updated_at=now,
            result=command.summary if command.outcome == TaskRunOutcome.COMPLETED else task.result,
            completed_at=now if command.outcome in (
                TaskRunOutcome.COMPLETED, TaskRunOutcome.TERMINATED
            ) else task.completed_at,
        )
        self._tasks[command.task_id] = updated_task
        updated_run = dc_replace(
            run, status=TaskRunStatus.COMPLETED, outcome=command.outcome,
            ended_at=now, summary=command.summary, metadata=dict(command.metadata),
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
        return await self.finish_run(FinishRunCommand(
            task_id=command.task_id, run_id=command.run_id,
            claim_lock=command.claim_lock, outcome=command.outcome,
            error=command.error,
        ))

    async def list_queued_due(self, now=None, limit=100, board="default"):
        now = now or datetime.now(timezone.utc)
        items = [
            t for t in self._tasks.values()
            if t.board == board and t.status == TaskStatus.QUEUED
            and not t.is_archived
            and (t.scheduled_at is None or t.scheduled_at <= now)
        ]
        items.sort(key=lambda t: (-t.priority, t.created_at or datetime.min, t.id))
        return tuple(items[:limit])

    async def list_running(self, board="default"):
        return tuple(
            t for t in self._tasks.values()
            if t.board == board and t.status == TaskStatus.RUNNING
        )

    async def add_comment(self, task_id, author, body):
        from uuid import uuid4
        comment = TaskComment(
            id=f"tc_{uuid4().hex[:8]}", task_id=task_id, author=author,
            body=body, created_at=datetime.now(timezone.utc),
        )
        self._comments.setdefault(task_id, []).append(comment)
        return comment

    async def list_comments(self, task_id):
        return tuple(self._comments.get(task_id, []))

    async def append_event(self, task_id, kind, payload, run_id=None):
        event = TaskEvent(
            id=self._next_event_id, task_id=task_id, kind=kind,
            payload=dict(payload), run_id=run_id,
            created_at=datetime.now(timezone.utc),
        )
        self._next_event_id += 1
        self._events.append(event)
        return event

    async def list_events(self, task_id, since=0, limit=100):
        return tuple(
            e for e in self._events
            if e.task_id == task_id and e.id > since
        )[-limit:]

    async def list_runs(self, task_id, limit=50):
        return tuple(
            r for r in self._runs.values() if r.task_id == task_id
        )[-limit:]

    async def add_attachment(self, task_id, filename, stored_name, content_type,
                            size, checksum, uploaded_by):
        from uuid import uuid4
        from app.domain.task import TaskAttachment
        att = TaskAttachment(
            id=f"ta_{uuid4().hex[:8]}", task_id=task_id, filename=filename,
            stored_name=stored_name, content_type=content_type, size=size,
            checksum=checksum, uploaded_by=uploaded_by,
            created_at=datetime.now(timezone.utc),
        )
        self._attachments.setdefault(task_id, []).append(att)
        return att

    async def list_attachments(self, task_id):
        return tuple(self._attachments.get(task_id, []))

    async def get_attachment(self, attachment_id):
        for atts in self._attachments.values():
            for a in atts:
                if a.id == attachment_id:
                    return a
        return None

    async def delete_attachment(self, attachment_id):
        for task_id, atts in self._attachments.items():
            for i, a in enumerate(atts):
                if a.id == attachment_id:
                    del atts[i]
                    return True
        return False

    async def subscribe_notify(self, task_id, platform, chat_id, thread_id=None):
        self._notify_subs.append({
            "task_id": task_id, "platform": platform, "chat_id": chat_id,
            "thread_id": thread_id, "last_terminal_event_id": 0,
        })
        return True

    async def list_notify_subs(self, task_id):
        return tuple(
            {**s} for s in self._notify_subs if s["task_id"] == task_id
        )

    async def unsubscribe_notify(self, task_id, platform, chat_id, thread_id=None):
        before = len(self._notify_subs)
        self._notify_subs = [
            s for s in self._notify_subs
            if not (s["task_id"] == task_id and s["platform"] == platform
                    and s["chat_id"] == chat_id and s["thread_id"] == thread_id)
        ]
        return len(self._notify_subs) < before

    # ------------------------------------------------------------------
    # T3: proposal resolution port (atomic, mirrors SQLite registry)
    # ------------------------------------------------------------------

    _FAKE_DECISION_TO_KIND = {
        "approved": "change_approved",
        "rejected": "change_rejected",
        "revised": "change_revised",
    }
    _FAKE_RESOLUTION_MARKER_KINDS = frozenset(_FAKE_DECISION_TO_KIND.values())

    async def resolve_proposal(
        self, command: ProposalResolutionCommand,
    ) -> ProposalResolutionResult:
        """Atomic in-memory mirror of SQLiteTaskRegistry.resolve_proposal.

        Single logical transaction: entry validation, task CAS, pending
        proposal discovery by precise ``proposal_event_id`` match, decision
        event INSERT, task status CAS UPDATE. Returns the re-read Task and
        the newly-appended decision event.
        """
        self.resolve_proposal_calls.append(command)

        # 1. Entry validation (mirrors SQLite _validate_resolution_command)
        if not command.task_id:
            raise TaskValidationError("task_id must not be empty")
        if command.decision not in self._FAKE_DECISION_TO_KIND:
            raise TaskValidationError(
                f"invalid decision: {command.decision!r}; expected one of "
                f"{sorted(self._FAKE_DECISION_TO_KIND)}"
            )
        expected_kind = self._FAKE_DECISION_TO_KIND[command.decision]
        if command.event_kind != expected_kind:
            raise TaskValidationError(
                f"decision/event_kind mismatch: decision={command.decision!r} "
                f"requires event_kind={expected_kind!r}, "
                f"got {command.event_kind!r}"
            )
        if command.decision == "revised":
            if not command.note or not command.note.strip():
                raise TaskValidationError(
                    "revised decision requires a non-empty note"
                )

        # 2. Read task and validate state
        task = self._tasks.get(command.task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {command.task_id}")
        if task.version != command.expected_version:
            raise TaskConflictError(
                f"version conflict: expected {command.expected_version}, "
                f"got {task.version}"
            )
        if task.is_archived:
            raise TaskStateError(
                f"task {command.task_id} is archived; cannot resolve proposal"
            )
        if task.status is not TaskStatus.WAITING_APPROVAL:
            raise TaskStateError(
                f"resolve_proposal requires WAITING_APPROVAL, "
                f"got {task.status.value}"
            )

        # 3. Find latest pending change_proposed by precise proposal_event_id
        events = [e for e in self._events if e.task_id == command.task_id]
        resolved_ids: set[int] = set()
        for ev in events:
            if ev.kind in self._FAKE_RESOLUTION_MARKER_KINDS:
                pid = ev.payload.get("proposal_event_id")
                if pid is not None:
                    resolved_ids.add(int(pid))

        latest_pending: TaskEvent | None = None
        for ev in reversed(events):
            if ev.kind == "change_proposed" and ev.id not in resolved_ids:
                latest_pending = ev
                break

        if latest_pending is None:
            raise TaskStateError(
                f"task {command.task_id} has no pending change_proposed event"
            )

        proposal_event_id = latest_pending.id

        # 4. Append decision event (non-null proposal_event_id)
        now = datetime.now(timezone.utc)
        decision_event = TaskEvent(
            id=self._next_event_id,
            task_id=command.task_id,
            kind=command.event_kind,
            payload={
                "decision": command.decision,
                "note": command.note,
                "proposal_event_id": proposal_event_id,
            },
            run_id=None,
            created_at=now,
        )
        self._next_event_id += 1
        self._events.append(decision_event)

        # 5. CAS UPDATE task: status -> QUEUED, version+1
        from dataclasses import replace as dc_replace
        updated_task = dc_replace(
            task, status=TaskStatus.QUEUED, version=task.version + 1,
            updated_at=now,
        )
        self._tasks[command.task_id] = updated_task

        return ProposalResolutionResult(
            proposal_event_id=proposal_event_id,
            task=updated_task,
            decision_event=decision_event,
        )

    async def latest_waiting_approval_in_session(
        self, session_id: str,
    ) -> Task | None:
        self.latest_waiting_approval_calls.append(session_id)
        if not session_id:
            raise TaskValidationError("session_id must not be empty")
        candidates = [
            t for t in self._tasks.values()
            if t.origin_session_id == session_id
            and t.status == TaskStatus.WAITING_APPROVAL
            and not t.is_archived
        ]
        candidates.sort(
            key=lambda t: (t.created_at or datetime.min, t.id),
            reverse=True,
        )
        return candidates[0] if candidates else None


class FakeMemoryStore:
    def __init__(self):
        self.deleted_sessions: list[str] = []

    async def delete_session(self, session_id: str) -> bool:
        self.deleted_sessions.append(session_id)
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _claim(registry: FakeTaskRegistry, task_id: str, lock: str = "lock-1") -> int:
    """Claim a QUEUED task and return its run_id."""
    result = await registry.claim_task(task_id, lock, 900)
    assert result is not None, f"claim failed for {task_id}"
    return result.run.id


def _running_task(task_id: str = "t_run") -> Task:
    """Build a task directly in RUNNING with a claim (bypasses dispatcher)."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id, title="running", status=TaskStatus.RUNNING,
        created_at=now, created_by="u", version=1,
        claim_lock="lock-1", claim_expires=now + timedelta(seconds=900),
        current_run_id=1, worker_token="wt_1", last_heartbeat_at=now,
        started_at=now,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return FakeTaskRegistry()


@pytest.fixture
def svc(registry, tmp_path):
    return TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
    )


# ---------------------------------------------------------------------------
# CRUD tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_basic(svc, registry):
    task = await svc.create_task(title="调研架构", created_by="alice")
    assert task.id.startswith("t_")
    assert task.status == TaskStatus.QUEUED
    assert task.title == "调研架构"
    assert task.board == "default"
    assert task.version == 1
    events = await registry.list_events(task.id)
    assert any(e.kind == "created" for e in events)


class _FakeConfigProvider:
    def __init__(self, config):
        self._config = config

    async def current(self):
        return self._config


# ---------------------------------------------------------------------------
# task_complete workspace_ref validation (bug: worker wrote file to sandbox
# cwd via open() then referenced workspace:{path}; file is not at workspace
# root -> artifact silently skipped, task falsely succeeded). Validation at
# complete() rejects the unreadable ref so the worker self-corrects via
# write_file or inline content before the run finalizes.
# ---------------------------------------------------------------------------


class _ProbeValidator:
    """Fake workspace_ref_validator: raises for refs in `missing`."""

    def __init__(self, missing: set[str] | None = None):
        self.missing = missing or set()
        self.calls: list[str] = []

    async def __call__(self, ref: str) -> None:
        self.calls.append(ref)
        if ref in self.missing:
            raise ArtifactContentUnavailableError(f"unreadable: {ref}")


@pytest.mark.asyncio
async def test_complete_rejects_unreadable_workspace_ref(registry):
    """A workspace: storage_ref whose file is not at the workspace root is
    rejected at complete() so the worker can self-correct, instead of being
    silently dropped post-finalize with a falsely-succeeded task."""
    await registry.create_task(_running_task("t_ws_bad"))
    validator = _ProbeValidator(missing={"workspace:task3-output.md"})
    svc = TaskService(
        registry=registry, policy=TaskPolicy(), memory_store=FakeMemoryStore(),
        workspace_ref_validator=validator,
    )
    with pytest.raises(TaskValidationError) as exc_info:
        await svc.complete(
            "t_ws_bad", summary="done",
            metadata={},
            artifacts=[{
                "type": "text/markdown", "name": "task3-output.md",
                "storage_ref": "workspace:task3-output.md",
            }],
        )
    msg = str(exc_info.value)
    assert "workspace:task3-output.md" in msg
    assert "write_file" in msg or "content" in msg
    # intent not recorded (validation precedes append_event)
    events = await registry.list_events("t_ws_bad")
    assert not any(e.kind == "complete_requested" for e in events)


@pytest.mark.asyncio
async def test_complete_accepts_readable_workspace_ref(registry):
    await registry.create_task(_running_task("t_ws_ok"))
    validator = _ProbeValidator()  # nothing missing -> all readable
    svc = TaskService(
        registry=registry, policy=TaskPolicy(), memory_store=FakeMemoryStore(),
        workspace_ref_validator=validator,
    )
    intent = await svc.complete(
        "t_ws_ok", summary="done", metadata={},
        artifacts=[{
            "type": "text/markdown", "name": "out.md",
            "storage_ref": "workspace:out.md",
        }],
    )
    assert intent["outcome"] == "completed"
    assert validator.calls == ["workspace:out.md"]


@pytest.mark.asyncio
async def test_complete_inline_content_skips_workspace_validation(registry):
    """Inline-content artifacts (no workspace: ref) must not trigger the
    workspace probe -- the common text-output path stays validation-free."""
    await registry.create_task(_running_task("t_ws_inline"))
    validator = _ProbeValidator()
    svc = TaskService(
        registry=registry, policy=TaskPolicy(), memory_store=FakeMemoryStore(),
        workspace_ref_validator=validator,
    )
    await svc.complete(
        "t_ws_inline", summary="done", metadata={},
        artifacts=[{
            "type": "text", "name": "report.md", "content": "# report body",
        }],
    )
    assert validator.calls == []


@pytest.mark.asyncio
async def test_complete_no_validator_skips_workspace_validation(registry):
    """No validator wired (default) -> backward compatible, no probe."""
    await registry.create_task(_running_task("t_ws_noval"))
    svc = TaskService(
        registry=registry, policy=TaskPolicy(), memory_store=FakeMemoryStore(),
    )
    intent = await svc.complete(
        "t_ws_noval", summary="done", metadata={},
        artifacts=[{
            "type": "text/markdown", "name": "out.md",
            "storage_ref": "workspace:out.md",
        }],
    )
    assert intent["outcome"] == "completed"


@pytest.mark.asyncio
async def test_complete_workspace_ref_validated_against_real_content_store(
    registry, tmp_path,
):
    """Integration: the validator wired in main.py calls
    LocalArtifactContentStore.probe. A workspace: ref whose file was NOT
    written to the workspace root (the bug: worker used open() to scratch
    cwd) is rejected; after writing the file to the workspace root (via the
    write_file callback path) the same ref is accepted."""
    from app.infrastructure.artifact.content_store import LocalArtifactContentStore

    ws_root = tmp_path / "workspace"
    ws_root.mkdir()
    store = LocalArtifactContentStore(
        tmp_path / "artifacts", tmp_path / "attachments", ws_root,
        max_bytes=4096,
    )

    async def validate(ref: str) -> None:
        await store.probe(ref)

    await registry.create_task(_running_task("t_ws_real"))
    svc = TaskService(
        registry=registry, policy=TaskPolicy(), memory_store=FakeMemoryStore(),
        workspace_ref_validator=validate,
    )
    # File not at workspace root yet -> rejected (bug scenario: open() to cwd)
    with pytest.raises(TaskValidationError):
        await svc.complete(
            "t_ws_real", summary="done", metadata={},
            artifacts=[{
                "type": "text/markdown", "name": "out.md",
                "storage_ref": "workspace:out.md",
            }],
        )
    # Worker self-corrects: write_file writes to workspace root -> accepted
    (ws_root / "out.md").write_bytes(b"# output\nreal content")
    intent = await svc.complete(
        "t_ws_real", summary="done", metadata={},
        artifacts=[{
            "type": "text/markdown", "name": "out.md",
            "storage_ref": "workspace:out.md",
        }],
    )
    assert intent["outcome"] == "completed"


@pytest.mark.asyncio
async def test_create_task_max_retries_none_uses_provider_default(registry):
    """max_retries=None (caller did not specify) resolves to the configured
    task_failure_limit (hot-reload default). Explicit 0 is honored."""
    from app.domain.task_config import TaskConfig
    from app.application.task_service import TaskService
    from app.domain.task_policy import TaskPolicy
    provider = _FakeConfigProvider(TaskConfig(task_failure_limit=7))
    svc = TaskService(registry=registry, policy=TaskPolicy(), task_config_provider=provider)
    # None -> provider default (7).
    t1 = await svc.create_task(title="a", created_by="u")
    assert t1.max_retries == 7
    # Explicit 0 honored (not overridden by provider default).
    t2 = await svc.create_task(title="b", created_by="u", max_retries=0)
    assert t2.max_retries == 0
    # Explicit int honored.
    t3 = await svc.create_task(title="c", created_by="u", max_retries=2)
    assert t3.max_retries == 2



@pytest.mark.asyncio
async def test_create_task_no_assignee_field(svc):
    """create_task must not accept an assignee parameter."""
    import inspect
    sig = inspect.signature(svc.create_task)
    assert "assignee" not in sig.parameters


@pytest.mark.asyncio
async def test_create_task_with_scheduled_at(svc):
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    task = await svc.create_task(
        title="delayed", created_by="u", scheduled_at=future,
    )
    assert task.status == TaskStatus.QUEUED
    assert task.scheduled_at == future


@pytest.mark.asyncio
async def test_create_task_empty_title_rejected(svc):
    with pytest.raises(TaskValidationError):
        await svc.create_task(title="", created_by="u")
    with pytest.raises(TaskValidationError):
        await svc.create_task(title="   ", created_by="u")


@pytest.mark.asyncio
async def test_create_task_idempotency(svc, registry):
    await svc.create_task(title="x", created_by="u", idempotency_key="k1")
    with pytest.raises(TaskConflictError):
        await svc.create_task(title="x", created_by="u", idempotency_key="k1")
    # 自然语言委派复用此契约：同一 tool-call 重放（chat:{session}:{request.id}）
    # 不得创建第二个任务，registry 只含一条记录。
    page = await registry.list_tasks(limit=200)
    assert len(page.items) == 1


@pytest.mark.asyncio
async def test_create_task_non_default_board_rejected(svc):
    with pytest.raises(TaskValidationError):
        await svc.create_task(title="x", board="other")


@pytest.mark.asyncio
async def test_get_task_not_found(svc):
    with pytest.raises(TaskNotFoundError):
        await svc.get_task("t_nonexistent")


@pytest.mark.asyncio
async def test_update_task_optimistic_lock(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    updated = await svc.update_task(task.id, {"title": "y"}, expected_version=1)
    assert updated.title == "y"
    assert updated.version == 2
    with pytest.raises(TaskConflictError):
        await svc.update_task(task.id, {"title": "z"}, expected_version=1)


@pytest.mark.asyncio
async def test_update_task_running_rejected(svc, registry):
    task = Task(
        id="t_run", title="r", status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_run", "lock-1", 900)
    with pytest.raises(TaskStateError):
        await svc.update_task("t_run", {"title": "new"}, expected_version=1)


@pytest.mark.asyncio
async def test_delete_task_non_running(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    deleted = await svc.delete_task(task.id)
    assert deleted is True
    assert await registry.get_task(task.id) is None


@pytest.mark.asyncio
async def test_delete_task_running_rejected(svc, registry):
    task = Task(
        id="t_run2", title="r", status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_run2", "lock-1", 900)
    with pytest.raises(TaskStateError):
        await svc.delete_task("t_run2")


@pytest.mark.asyncio
async def test_delete_task_cleans_execution_session(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    await registry.update_task(
        task.id, {"execution_session_id": "task-t_exec"}, expected_version=1
    )
    await svc.delete_task(task.id)
    assert svc.memory_store.deleted_sessions == ["task-t_exec"]


@pytest.mark.asyncio
async def test_delete_task_calls_artifact_delete_callback(registry, tmp_path):
    """delete_task best-effort calls artifact_delete_callback(task_id) to remove
    artifacts registered against the task in the separate artifacts DB."""
    calls: list[str] = []

    async def delete_callback(task_id: str) -> None:
        calls.append(task_id)

    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        artifact_delete_callback=delete_callback,
    )
    task = await svc.create_task(title="x", created_by="u")
    await svc.delete_task(task.id)
    assert calls == [task.id]


@pytest.mark.asyncio
async def test_delete_task_artifact_delete_callback_failure_does_not_block(registry, tmp_path):
    """artifact_delete_callback failure is best-effort: task deletion still succeeds."""
    async def delete_callback(task_id: str) -> None:
        raise RuntimeError("artifact DB down")

    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        artifact_delete_callback=delete_callback,
    )
    task = await svc.create_task(title="x", created_by="u")
    deleted = await svc.delete_task(task.id)
    assert deleted is True
    assert await registry.get_task(task.id) is None


# ---------------------------------------------------------------------------
# propose_change / approve / reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_change_writes_event_and_advances_state(svc, registry):
    task = _running_task("t_p1")
    await registry.create_task(task)
    # Create a run row to match current_run_id
    run = TaskRun(
        id=1, task_id="t_p1", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1", started_at=datetime.now(timezone.utc),
    )
    registry._runs[1] = run

    result = await svc.propose_change(
        "t_p1", "switch to plan B", run_id=1,
    )
    assert result["outcome"] == "waiting_approval"
    assert result["proposal"] == "switch to plan B"
    assert result["run_id"] == 1
    assert "proposal_event_id" in result

    events = await registry.list_events("t_p1")
    proposed = [e for e in events if e.kind == "change_proposed"]
    assert len(proposed) == 1
    assert proposed[0].payload["proposal"] == "switch to plan B"
    assert proposed[0].payload["run_id"] == 1
    assert proposed[0].run_id == 1


@pytest.mark.asyncio
async def test_propose_change_not_running_rejected(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    # task is QUEUED, not RUNNING
    with pytest.raises(TaskStateError):
        await svc.propose_change(task.id, "p", run_id=1)


@pytest.mark.asyncio
async def test_propose_change_run_id_mismatch_rejected(svc, registry):
    task = _running_task("t_p2")
    task = task.__class__(
        **{**task.__dict__, "current_run_id": 5}
    )
    await registry.create_task(task)
    with pytest.raises(TaskStateError):
        await svc.propose_change("t_p2", "p", run_id=999)


@pytest.mark.asyncio
async def test_propose_change_default_proposal_type_is_approval(svc, registry):
    """propose_change 不传 proposal_type -> 默认 approval，event payload 含 proposal_type='approval'。"""
    task = _running_task("t_p3")
    await registry.create_task(task)
    run = TaskRun(
        id=1, task_id="t_p3", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1", started_at=datetime.now(timezone.utc),
    )
    registry._runs[1] = run

    result = await svc.propose_change("t_p3", "switch to plan B", run_id=1)
    assert result["outcome"] == "waiting_approval"
    assert result["proposal_type"] == "approval"

    events = await registry.list_events("t_p3")
    proposed = [e for e in events if e.kind == "change_proposed"]
    assert len(proposed) == 1
    assert proposed[0].payload["proposal"] == "switch to plan B"
    assert proposed[0].payload["proposal_type"] == "approval"


@pytest.mark.asyncio
async def test_propose_change_intent_request_proposal_type(svc, registry):
    """propose_change(proposal_type='intent_request') -> event payload 含 proposal_type='intent_request'。"""
    task = _running_task("t_p4")
    await registry.create_task(task)
    run = TaskRun(
        id=1, task_id="t_p4", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1", started_at=datetime.now(timezone.utc),
    )
    registry._runs[1] = run

    result = await svc.propose_change(
        "t_p4", "需要用户补充意图", run_id=1, proposal_type="intent_request",
    )
    assert result["outcome"] == "waiting_approval"
    assert result["proposal_type"] == "intent_request"

    events = await registry.list_events("t_p4")
    proposed = [e for e in events if e.kind == "change_proposed"]
    assert len(proposed) == 1
    assert proposed[0].payload["proposal_type"] == "intent_request"


@pytest.mark.asyncio
async def test_propose_change_invalid_proposal_type_rejected(svc, registry):
    """proposal_type 非 approval/intention_request -> TaskValidationError，不写 event。"""
    task = _running_task("t_p5")
    await registry.create_task(task)
    run = TaskRun(
        id=1, task_id="t_p5", status=TaskRunStatus.RUNNING,
        claim_lock="lock-1", started_at=datetime.now(timezone.utc),
    )
    registry._runs[1] = run

    with pytest.raises(TaskValidationError):
        await svc.propose_change(
            "t_p5", "p", run_id=1, proposal_type="unknown",
        )
    # 校验失败不写 event
    events = await registry.list_events("t_p5")
    assert not any(e.kind == "change_proposed" for e in events)


@pytest.mark.asyncio
async def test_approve_change_moves_to_queued(svc, registry):
    # Set up a WAITING_APPROVAL task with a prior change_proposed event
    task = _running_task("t_a1")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    proposal_event = await registry.append_event(
        "t_a1", "change_proposed",
        {"proposal": "do X", "run_id": 1}, run_id=1,
    )

    result = await svc.approve_change("t_a1")
    assert result["task_id"] == "t_a1"
    assert result["decision"] == "approved"
    assert result["proposal_event_id"] == proposal_event.id

    updated = await registry.get_task("t_a1")
    assert updated.status == TaskStatus.QUEUED

    events = await registry.list_events("t_a1")
    approved = [e for e in events if e.kind == "change_approved"]
    assert len(approved) == 1
    assert approved[0].payload["proposal_event_id"] == proposal_event.id


@pytest.mark.asyncio
async def test_reject_change_moves_to_queued(svc, registry):
    task = _running_task("t_r1")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    proposal_event = await registry.append_event(
        "t_r1", "change_proposed",
        {"proposal": "do Y", "run_id": 1}, run_id=1,
    )

    result = await svc.reject_change("t_r1")
    assert result["decision"] == "rejected"
    assert result["proposal_event_id"] == proposal_event.id

    updated = await registry.get_task("t_r1")
    assert updated.status == TaskStatus.QUEUED

    events = await registry.list_events("t_r1")
    rejected = [e for e in events if e.kind == "change_rejected"]
    assert len(rejected) == 1


@pytest.mark.asyncio
async def test_approve_not_waiting_rejected(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    # task is QUEUED, not WAITING_APPROVAL
    with pytest.raises(TaskStateError):
        await svc.approve_change(task.id)


@pytest.mark.asyncio
async def test_approve_does_not_change_scheduled_at(svc, registry):
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    task = _running_task("t_a2")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None,
           "scheduled_at": future}
    )
    await registry.create_task(task)
    await registry.append_event(
        "t_a2", "change_proposed", {"proposal": "p", "run_id": 1}, run_id=1,
    )

    await svc.approve_change("t_a2")
    updated = await registry.get_task("t_a2")
    assert updated.scheduled_at == future


@pytest.mark.asyncio
async def test_approve_change_with_note_persists_payload(svc, registry):
    task = _running_task("t_an1")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await registry.append_event(
        "t_an1", "change_proposed", {"proposal": "do X", "run_id": 1}, run_id=1,
    )

    result = await svc.approve_change("t_an1", note="looks good")
    assert result["note"] == "looks good"
    assert result["decision"] == "approved"
    events = await registry.list_events("t_an1")
    approved = [e for e in events if e.kind == "change_approved"]
    assert approved[0].payload["note"] == "looks good"


@pytest.mark.asyncio
async def test_reject_change_with_note_persists_payload(svc, registry):
    task = _running_task("t_an2")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await registry.append_event(
        "t_an2", "change_proposed", {"proposal": "do Y", "run_id": 1}, run_id=1,
    )
    result = await svc.reject_change("t_an2", note="reason: risky")
    assert result["note"] == "reason: risky"
    events = await registry.list_events("t_an2")
    rejected = [e for e in events if e.kind == "change_rejected"]
    assert rejected[0].payload["note"] == "reason: risky"


@pytest.mark.asyncio
async def test_approval_without_note_persists_null(svc, registry):
    task = _running_task("t_an3")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await registry.append_event(
        "t_an3", "change_proposed", {"proposal": "p", "run_id": 1}, run_id=1,
    )
    result = await svc.approve_change("t_an3")
    assert result["note"] is None
    events = await registry.list_events("t_an3")
    approved = [e for e in events if e.kind == "change_approved"]
    assert approved[0].payload["note"] is None


@pytest.mark.asyncio
async def test_build_worker_context_renders_approval_note(svc, registry):
    task = _running_task("t_wn1")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await registry.append_event(
        "t_wn1", "change_proposed", {"proposal": "do X", "run_id": 1}, run_id=1,
    )
    await svc.approve_change("t_wn1", note="proceed with care")
    updated = await registry.get_task("t_wn1")
    ctx = await svc.build_worker_context(updated)
    assert "审批决策" in ctx
    assert "proceed with care" in ctx


@pytest.mark.asyncio
async def test_build_worker_context_legacy_approval_without_note(svc, registry):
    # Legacy change_approved event written before note field existed.
    task = _running_task("t_wn2")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.QUEUED,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await registry.append_event(
        "t_wn2", "change_approved", {"decision": "approved"},
    )
    updated = await registry.get_task("t_wn2")
    ctx = await svc.build_worker_context(updated)
    assert "note=None" not in ctx
    assert "note=undefined" not in ctx


# ---------------------------------------------------------------------------
# T3: revise_change + unified _resolve_proposal + lifecycle + worker_context
# ---------------------------------------------------------------------------


def _waiting_approval_task(
    task_id: str = "t_wa",
    *,
    title: str = "waiting",
    origin_session_id: str | None = None,
) -> Task:
    """Build a task directly in WAITING_APPROVAL (claim already released)."""
    now = datetime.now(timezone.utc)
    return Task(
        id=task_id, title=title, status=TaskStatus.WAITING_APPROVAL,
        created_at=now, created_by="u", version=1,
        origin_session_id=origin_session_id,
    )


async def _seed_pending_proposal(
    registry: FakeTaskRegistry,
    task_id: str = "t_wa",
    *,
    proposal: str = "do X",
    origin_session_id: str | None = None,
    title: str = "waiting",
) -> TaskEvent:
    """Create a WAITING_APPROVAL task with a pending change_proposed event."""
    task = _waiting_approval_task(
        task_id, title=title, origin_session_id=origin_session_id,
    )
    await registry.create_task(task)
    return await registry.append_event(
        task_id, "change_proposed",
        {"proposal": proposal, "run_id": 1}, run_id=1,
    )


class _FailingLifecycleWriter:
    """Lifecycle writer that always raises (for failure-isolation tests)."""

    def __init__(self, exc: Exception | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self._exc = exc or RuntimeError("lifecycle writer failed")

    async def __call__(
        self, session_id: str, content: str, card: dict | None = None,
    ):
        self.calls.append((session_id, content, card))
        raise self._exc


# --- Point 1: revise_change trims note and returns full response ---


@pytest.mark.asyncio
async def test_revise_change_trims_note_and_returns_response(svc, registry):
    proposal = await _seed_pending_proposal(registry, "t_rev1")

    result = await svc.revise_change("t_rev1", note="  please redo with plan B  ")

    assert result["task_id"] == "t_rev1"
    assert result["title"] == "waiting"
    assert result["status"] == "queued"
    assert result["decision"] == "revised"
    assert result["proposal_event_id"] == proposal.id
    assert result["note"] == "please redo with plan B"

    updated = await registry.get_task("t_rev1")
    assert updated.status == TaskStatus.QUEUED

    events = await registry.list_events("t_rev1")
    revised = [e for e in events if e.kind == "change_revised"]
    assert len(revised) == 1
    assert revised[0].payload["note"] == "please redo with plan B"
    assert revised[0].payload["proposal_event_id"] == proposal.id


# --- Point 2: note normalization (approve/reject/revise) ---


@pytest.mark.asyncio
async def test_approve_note_normalizes_empty_to_none(svc, registry):
    """approve note: missing/null/whitespace -> None."""
    for note in (None, "   ", ""):
        registry._tasks.clear()
        registry._events.clear()
        registry._next_event_id = 1
        await _seed_pending_proposal(registry, "t_an")
        result = await svc.approve_change("t_an", note=note)
        assert result["note"] is None
        events = await registry.list_events("t_an")
        approved = [e for e in events if e.kind == "change_approved"]
        assert approved[0].payload["note"] is None


@pytest.mark.asyncio
async def test_reject_note_normalizes_empty_to_none(svc, registry):
    """reject note: missing/null/whitespace -> None."""
    for note in (None, "   ", ""):
        registry._tasks.clear()
        registry._events.clear()
        registry._next_event_id = 1
        await _seed_pending_proposal(registry, "t_rn")
        result = await svc.reject_change("t_rn", note=note)
        assert result["note"] is None


@pytest.mark.asyncio
async def test_revise_rejects_empty_or_whitespace_note(svc, registry):
    """revise note: missing/null/whitespace -> TaskValidationError."""
    for note in (None, "", "   ", "  \t  "):
        registry._tasks.clear()
        registry._events.clear()
        registry._next_event_id = 1
        await _seed_pending_proposal(registry, "t_re")
        with pytest.raises(TaskValidationError):
            await svc.revise_change("t_re", note=note)


@pytest.mark.asyncio
async def test_approval_rejects_non_string_note(svc, registry):
    """All three decisions reject non-string note (int/list/dict)."""
    for note in (123, ["a"], {"k": "v"}, True):
        registry._tasks.clear()
        registry._events.clear()
        registry._next_event_id = 1
        await _seed_pending_proposal(registry, "t_ns")
        with pytest.raises(TaskValidationError):
            await svc.approve_change("t_ns", note=note)
        with pytest.raises(TaskValidationError):
            await svc.reject_change("t_ns", note=note)
        with pytest.raises(TaskValidationError):
            await svc.revise_change("t_ns", note=note)


@pytest.mark.asyncio
async def test_approval_rejects_oversized_note(svc, registry):
    """All three decisions reject note > 2000 code points."""
    oversized = "x" * 2001
    for method_name in ("approve_change", "reject_change", "revise_change"):
        registry._tasks.clear()
        registry._events.clear()
        registry._next_event_id = 1
        await _seed_pending_proposal(registry, "t_os")
        method = getattr(svc, method_name)
        with pytest.raises(TaskValidationError):
            await method("t_os", note=oversized)


@pytest.mark.asyncio
async def test_approval_accepts_note_at_exactly_2000_codepoints(svc, registry):
    """Note at exactly 2000 code points is accepted (boundary)."""
    boundary = "x" * 2000
    for method_name in ("approve_change", "reject_change", "revise_change"):
        registry._tasks.clear()
        registry._events.clear()
        registry._next_event_id = 1
        await _seed_pending_proposal(registry, "t_bd")
        method = getattr(svc, method_name)
        result = await method("t_bd", note=boundary)
        assert result["note"] == boundary


# --- Point 3: validation failure does not access Registry; proposal_event_id never null ---


@pytest.mark.asyncio
async def test_validation_failure_does_not_access_registry(svc, registry):
    """Service-level validation failure (empty task_id, bad note) does not
    call Registry.resolve_proposal or Registry.get_task."""
    # Empty task_id
    registry.resolve_proposal_calls.clear()
    registry.get_task_calls.clear()
    with pytest.raises(TaskValidationError):
        await svc.approve_change("", note="x")
    assert registry.resolve_proposal_calls == []
    assert registry.get_task_calls == []

    # Non-string note
    with pytest.raises(TaskValidationError):
        await svc.reject_change("t_x", note=42)
    assert registry.resolve_proposal_calls == []
    assert registry.get_task_calls == []

    # Oversized note
    with pytest.raises(TaskValidationError):
        await svc.approve_change("t_x", note="y" * 2001)
    assert registry.resolve_proposal_calls == []
    assert registry.get_task_calls == []

    # revise with whitespace note
    with pytest.raises(TaskValidationError):
        await svc.revise_change("t_x", note="   ")
    assert registry.resolve_proposal_calls == []
    assert registry.get_task_calls == []


@pytest.mark.asyncio
async def test_proposal_event_id_never_null_on_success(svc, registry):
    """proposal_event_id is always a non-null int for all three decisions."""
    for method_name, note in (
        ("approve_change", None),
        ("reject_change", None),
        ("revise_change", "redo it"),
    ):
        registry._tasks.clear()
        registry._events.clear()
        registry._next_event_id = 1
        await _seed_pending_proposal(registry, "t_pn")
        method = getattr(svc, method_name)
        result = await method("t_pn", note=note)
        assert result["proposal_event_id"] is not None
        assert isinstance(result["proposal_event_id"], int)


# --- Point 4: lifecycle written exactly once on success ---


@pytest.mark.asyncio
async def test_approve_writes_approved_lifecycle(registry, tmp_path):
    writer = _FakeLifecycleWriter()
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        lifecycle_writer=writer,
    )
    await _seed_pending_proposal(
        registry, "t_la", origin_session_id="chat-s1", title="My Task",
    )

    await svc.approve_change("t_la")

    assert len(writer.calls) == 1
    sid, content, card = writer.calls[0]
    assert sid == "chat-s1"
    assert content == "已批准: t_la - My Task"
    assert card is None  # 决策回执无 card


@pytest.mark.asyncio
async def test_reject_writes_rejected_lifecycle(registry, tmp_path):
    writer = _FakeLifecycleWriter()
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        lifecycle_writer=writer,
    )
    await _seed_pending_proposal(
        registry, "t_lr", origin_session_id="chat-s1", title="Reject Me",
    )

    await svc.reject_change("t_lr")

    assert len(writer.calls) == 1
    sid, content, card = writer.calls[0]
    assert sid == "chat-s1"
    assert content == "已拒绝: t_lr - Reject Me"
    assert card is None  # 决策回执无 card


@pytest.mark.asyncio
async def test_revise_writes_revised_lifecycle_with_note(registry, tmp_path):
    writer = _FakeLifecycleWriter()
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        lifecycle_writer=writer,
    )
    await _seed_pending_proposal(
        registry, "t_lrev", origin_session_id="chat-s1", title="Revise Me",
    )

    await svc.revise_change("t_lrev", note="  use plan C instead  ")

    assert len(writer.calls) == 1
    sid, content, card = writer.calls[0]
    assert sid == "chat-s1"
    assert content == "已修订: t_lrev - Revise Me | 修订指示: use plan C instead"
    assert card is None  # 决策回执无 card


# --- Point 5: lifecycle failure does not block decision; failed decision no lifecycle ---


@pytest.mark.asyncio
async def test_lifecycle_writer_exception_decision_still_succeeds(registry, tmp_path):
    """When lifecycle writer raises, the decision still succeeds and Registry
    has exactly one decision event."""
    writer = _FailingLifecycleWriter()
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        lifecycle_writer=writer,
    )
    await _seed_pending_proposal(registry, "t_lf")

    result = await svc.revise_change("t_lf", note="redo")

    # Decision succeeded
    assert result["decision"] == "revised"
    assert result["proposal_event_id"] is not None

    # Registry has exactly one decision event
    events = await registry.list_events("t_lf")
    decision_events = [
        e for e in events
        if e.kind in ("change_approved", "change_rejected", "change_revised")
    ]
    assert len(decision_events) == 1
    assert decision_events[0].kind == "change_revised"

    # Writer was called (attempted) despite the exception
    assert len(writer.calls) == 1


@pytest.mark.asyncio
async def test_failed_decision_does_not_write_lifecycle(registry, tmp_path):
    """Validation failure does not call the lifecycle writer."""
    writer = _FakeLifecycleWriter()
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        lifecycle_writer=writer,
    )
    await _seed_pending_proposal(registry, "t_fd")

    # revise with empty note -> validation failure
    with pytest.raises(TaskValidationError):
        await svc.revise_change("t_fd", note="   ")
    assert writer.calls == []

    # non-string note -> validation failure
    with pytest.raises(TaskValidationError):
        await svc.approve_change("t_fd", note=123)
    assert writer.calls == []


# --- Point 6: worker_context renders revised + proposal_event_id + note ---


@pytest.mark.asyncio
async def test_worker_context_renders_revised_decision(svc, registry):
    """After revise, 审批决策 segment shows revised + proposal_event_id + note."""
    proposal = await _seed_pending_proposal(registry, "t_wr")

    await svc.revise_change("t_wr", note="switch to plan B")
    updated = await registry.get_task("t_wr")
    ctx = await svc.build_worker_context(updated)

    assert "审批决策" in ctx
    assert "revised" in ctx
    assert f"proposal_event_id={proposal.id}" in ctx
    assert "switch to plan B" in ctx


@pytest.mark.asyncio
async def test_worker_context_progress_renders_change_revised(svc, registry):
    """After revise, 进度 segment includes a change_revised line."""
    await _seed_pending_proposal(registry, "t_wp2")

    await svc.revise_change("t_wp2", note="redo cleanly")
    updated = await registry.get_task("t_wp2")
    ctx = await svc.build_worker_context(updated)

    assert "进度" in ctx
    assert "change_revised" in ctx
    assert "redo cleanly" in ctx


@pytest.mark.asyncio
async def test_worker_context_approved_regression_unchanged(svc, registry):
    """approved/rejected rendering is unchanged by the revised addition."""
    proposal = await _seed_pending_proposal(registry, "t_war")

    await svc.approve_change("t_war", note="ok")
    updated = await registry.get_task("t_war")
    ctx = await svc.build_worker_context(updated)

    assert "审批决策" in ctx
    assert "approved" in ctx
    assert f"proposal_event_id={proposal.id}" in ctx
    assert "ok" in ctx
    assert "revised" not in ctx


@pytest.mark.asyncio
async def test_worker_context_rejected_regression_unchanged(svc, registry):
    proposal = await _seed_pending_proposal(registry, "t_wrr")

    await svc.reject_change("t_wrr", note="nope")
    updated = await registry.get_task("t_wrr")
    ctx = await svc.build_worker_context(updated)

    assert "审批决策" in ctx
    assert "rejected" in ctx
    assert f"proposal_event_id={proposal.id}" in ctx
    assert "nope" in ctx


# --- Point 7: _latest_open_proposal_text treats revised as marker ---


@pytest.mark.asyncio
async def test_latest_open_proposal_treats_revised_as_marker(svc, registry):
    """After revise, the proposal is no longer shown as 待审批提案."""
    await _seed_pending_proposal(registry, "t_lm1", proposal="original plan")

    await svc.revise_change("t_lm1", note="redo")
    updated = await registry.get_task("t_lm1")
    ctx = await svc.build_worker_context(updated)

    # The proposal was resolved by the revised marker -> no 待审批提案 segment
    assert "待审批提案" not in ctx


@pytest.mark.asyncio
async def test_latest_open_proposal_precise_proposal_id_match(svc, registry):
    """revised marker only resolves the referenced proposal_event_id; a
    different pending proposal remains open."""
    task = _waiting_approval_task("t_lm2")
    await registry.create_task(task)
    # Two proposals; only the first is resolved by the revised marker.
    first_proposal = await registry.append_event(
        "t_lm2", "change_proposed",
        {"proposal": "first proposal", "run_id": 1}, run_id=1,
    )
    second_proposal = await registry.append_event(
        "t_lm2", "change_proposed",
        {"proposal": "second proposal", "run_id": 1}, run_id=1,
    )

    # Manually append a revised marker referencing the FIRST proposal only.
    await registry.append_event(
        "t_lm2", "change_revised",
        {
            "decision": "revised",
            "note": "redo first",
            "proposal_event_id": first_proposal.id,
        },
    )

    updated = await registry.get_task("t_lm2")
    ctx = await svc.build_worker_context(updated)

    # The second proposal is still open -> 待审批提案 segment is present.
    assert "待审批提案" in ctx
    # Isolate the 待审批提案 segment: it ends at the next "## " header.
    proposal_section = ctx.split("## 待审批提案\n", 1)[1].split("## ", 1)[0]
    assert "second proposal" in proposal_section
    assert "first proposal" not in proposal_section
    assert second_proposal.id != first_proposal.id


# --- Point 8: note does not enter logs (caplog) ---


@pytest.mark.asyncio
async def test_lifecycle_failure_log_excludes_note(registry, tmp_path, caplog):
    """When lifecycle write fails, the warning log must not contain the
    user's revision note."""
    writer = _FailingLifecycleWriter()
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        lifecycle_writer=writer,
    )
    await _seed_pending_proposal(registry, "t_nl", origin_session_id="chat-s1")

    secret_note = "secret-revision-instruction-XYZ-DO-NOT-LEAK"
    with caplog.at_level(logging.WARNING, logger="app.application.task_service"):
        result = await svc.revise_change("t_nl", note=secret_note)

    # Decision still succeeded
    assert result["decision"] == "revised"
    assert result["note"] == secret_note

    # Log captured the lifecycle failure
    log_text = caplog.text
    assert "lifecycle write failed" in log_text
    # The user note must NOT appear anywhere in the log
    assert secret_note not in log_text
    assert "修订指示" not in log_text


# ---------------------------------------------------------------------------
# cancel_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_task_from_queued(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    result = await svc.cancel_task(task.id)
    assert result["task_id"] == task.id
    assert result["status"] == "cancelled"

    updated = await registry.get_task(task.id)
    assert updated.status == TaskStatus.CANCELLED

    events = await registry.list_events(task.id)
    assert any(e.kind == "cancelled" for e in events)


class _FakeLifecycleWriter:
    """三参 lifecycle writer（T4）：记录三元组 (session_id, content, card)。

    决策回执/取消均为纯文本 lifecycle，card 恒为 None。
    """

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(
        self, session_id: str, content: str, card: dict | None = None,
    ):
        self.calls.append((session_id, content, card))


@pytest.mark.asyncio
async def test_cancel_task_from_queued_writes_cancelled_lifecycle(registry, tmp_path):
    """非 RUNNING cancel CAS 成功后写 ui.task_lifecycle '已取消' 到执行会话（origin 复用）。"""
    writer = _FakeLifecycleWriter()
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        lifecycle_writer=writer,
    )
    task = await svc.create_task(
        title="完成报告", created_by="u", origin_session_id="dashboard-s1",
    )
    await svc.cancel_task(task.id)
    assert any(
        sid == "dashboard-s1" and "已取消" in c
        for (sid, c, _card) in writer.calls
    )
    # 取消回执无 card
    assert all(card is None for (_sid, _c, card) in writer.calls)


@pytest.mark.asyncio
async def test_cancel_task_no_writer_does_not_crash(registry, tmp_path):
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
    )
    task = await svc.create_task(title="x", created_by="u")
    await svc.cancel_task(task.id)  # lifecycle_writer=None，不应抛


@pytest.mark.asyncio
async def test_cancel_task_from_waiting_approval(svc, registry):
    task = _running_task("t_c1")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await svc.cancel_task("t_c1")
    updated = await registry.get_task("t_c1")
    assert updated.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_task_from_failed(svc, registry):
    task = _running_task("t_c2")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.FAILED,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await svc.cancel_task("t_c2")
    updated = await registry.get_task("t_c2")
    assert updated.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_task_terminal_rejected(svc, registry):
    task = _running_task("t_c3")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.SUCCEEDED}
    )
    await registry.create_task(task)
    with pytest.raises(TaskStateError):
        await svc.cancel_task("t_c3")


@pytest.mark.asyncio
async def test_cancel_task_expired_rejected(svc, registry):
    """EXPIRED cannot be cancelled (must retry instead)."""
    task = _running_task("t_c4")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.EXPIRED,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    with pytest.raises(TaskStateError):
        await svc.cancel_task("t_c4")


# ---------------------------------------------------------------------------
# retry_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_task_from_failed(svc, registry):
    task = _running_task("t_rt1")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.FAILED,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None,
           "is_archived": False}
    )
    await registry.create_task(task)
    result = await svc.retry_task("t_rt1")
    assert result["task_id"] == "t_rt1"
    assert result["status"] == "queued"

    updated = await registry.get_task("t_rt1")
    assert updated.status == TaskStatus.QUEUED
    assert updated.is_archived is False

    events = await registry.list_events("t_rt1")
    assert any(e.kind == "retried" for e in events)


@pytest.mark.asyncio
async def test_retry_task_from_expired(svc, registry):
    task = _running_task("t_rt2")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.EXPIRED,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await svc.retry_task("t_rt2")
    updated = await registry.get_task("t_rt2")
    assert updated.status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_retry_task_not_failed_rejected(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    # task is QUEUED, not FAILED/EXPIRED
    with pytest.raises(TaskStateError):
        await svc.retry_task(task.id)


# ---------------------------------------------------------------------------
# Archive (soft-delete flag)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_archived_flag(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    archived = await svc.set_archived(task.id, True, expected_version=1)
    assert archived.is_archived is True
    assert archived.status == TaskStatus.QUEUED  # status unchanged
    unarchived = await svc.set_archived(task.id, False, expected_version=2)
    assert unarchived.is_archived is False
    assert unarchived.status == TaskStatus.QUEUED


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    result = await svc.add_comment(task.id, "hello", author="worker")
    assert result["body"] == "hello"
    assert result["author"] == "worker"


@pytest.mark.asyncio
async def test_list_comments(svc):
    task = await svc.create_task(title="x", created_by="u")
    await svc.add_comment(task.id, "first", author="a")
    await svc.add_comment(task.id, "second", author="b")
    comments = await svc.list_comments(task.id)
    assert len(comments) == 2


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_attachment(svc):
    task = await svc.create_task(title="x", created_by="u")
    att = await svc.upload_attachment(
        task.id, "report.md", b"# Hello", "text/markdown", "alice"
    )
    assert att.filename == "report.md"
    assert att.size == 7
    assert att.checksum.startswith("sha256:")


@pytest.mark.asyncio
async def test_upload_attachment_too_large(svc):
    task = await svc.create_task(title="x", created_by="u")
    svc.attachment_max_bytes = 10
    with pytest.raises(TaskValidationError):
        await svc.upload_attachment(task.id, "big.bin", b"x" * 100)


@pytest.mark.asyncio
async def test_upload_attachment_path_traversal(svc):
    task = await svc.create_task(title="x", created_by="u")
    with pytest.raises(TaskValidationError):
        await svc.upload_attachment(task.id, "../etc/passwd", b"x")
    with pytest.raises(TaskValidationError):
        await svc.upload_attachment(task.id, "a/b.txt", b"x")


@pytest.mark.asyncio
async def test_delete_attachment(svc):
    task = await svc.create_task(title="x", created_by="u")
    att = await svc.upload_attachment(task.id, "r.md", b"hello", "text/markdown", "u")
    deleted = await svc.delete_attachment(att.id)
    assert deleted is True


# ---------------------------------------------------------------------------
# T9: artifact_register_callback (best-effort, after file write + DB add)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_register_callback_called_once_with_attachment(
    registry, tmp_path,
):
    """After file write + add_attachment succeed, callback is called exactly
    once with the real TaskAttachment."""
    calls: list = []

    async def callback(att: TaskAttachment) -> None:
        calls.append(att)

    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        artifact_register_callback=callback,
    )
    task = await svc.create_task(title="x", created_by="u")
    att = await svc.upload_attachment(
        task.id, "report.md", b"# Hello", "text/markdown", "alice",
    )

    assert len(calls) == 1
    called = calls[0]
    assert called.id == att.id
    assert called.task_id == task.id
    assert called.filename == "report.md"
    assert called.size == 7
    assert called.checksum == att.checksum
    assert called.stored_name == att.stored_name

    # Existing event-writing semantics unchanged
    events = await registry.list_events(task.id)
    assert any(e.kind == "attachment_uploaded" for e in events)


@pytest.mark.asyncio
async def test_artifact_register_callback_none_preserves_old_behavior(
    registry, tmp_path,
):
    """When callback is None (default), upload_attachment behaves as before."""
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
    )
    task = await svc.create_task(title="x", created_by="u")
    att = await svc.upload_attachment(
        task.id, "report.md", b"# Hello", "text/markdown", "alice",
    )
    assert att.filename == "report.md"
    assert att.size == 7
    listed = await svc.list_attachments(task.id)
    assert len(listed) == 1
    assert listed[0].id == att.id


@pytest.mark.asyncio
async def test_artifact_register_callback_failure_logs_warning_and_preserves_upload(
    registry, tmp_path, caplog,
):
    """Callback failure -> warning logged (safe fields only), file/DB record
    NOT deleted, return value unchanged."""

    async def failing_callback(att: TaskAttachment) -> None:
        raise RuntimeError("BOOM-SECRET-MESSAGE-XYZ")

    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=FakeMemoryStore(),
        attachments_root=tmp_path / "attachments",
        artifact_register_callback=failing_callback,
    )
    task = await svc.create_task(title="x", created_by="u")

    secret_content = b"TOPSECRET-CONTENT-DO-NOT-LEAK"
    secret_filename = "leaky-filename.md"

    with caplog.at_level(logging.WARNING, logger="app.application.task_service"):
        att = await svc.upload_attachment(
            task.id, secret_filename, secret_content, "text/markdown", "alice",
        )

    # Return value unchanged
    assert att.filename == secret_filename
    assert att.size == len(secret_content)

    # DB record NOT deleted
    listed = await svc.list_attachments(task.id)
    assert len(listed) == 1
    assert listed[0].id == att.id

    # File NOT deleted
    file_path = svc.get_attachment_path(task.id, att.stored_name)
    assert file_path is not None
    assert file_path.exists()

    # Warning logged with safe fields
    log_text = caplog.text
    assert "artifact_register_callback failed" in log_text
    assert "source_kind=task_attachment" in log_text
    assert att.id in log_text
    assert "RuntimeError" in log_text

    # Sensitive fields must NOT appear in the warning
    assert "TOPSECRET-CONTENT-DO-NOT-LEAK" not in log_text  # content
    assert secret_filename not in log_text  # filename
    assert att.stored_name not in log_text  # stored_name
    assert "BOOM-SECRET-MESSAGE-XYZ" not in log_text  # exception message
    # Absolute paths must NOT appear
    assert str(tmp_path) not in log_text
    assert str(svc.attachments_root) not in log_text

    # Existing event-writing semantics unchanged
    events = await registry.list_events(task.id)
    assert any(e.kind == "attachment_uploaded" for e in events)


# ---------------------------------------------------------------------------
# Notify subscriptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_list_unsubscribe_notify(svc):
    task = await svc.create_task(title="x", created_by="u")
    await svc.subscribe_notify(task.id, "feishu", "chat-1", "thread-1")
    subs = await svc.list_notify_subs(task.id)
    assert len(subs) == 1
    assert subs[0]["platform"] == "feishu"
    removed = await svc.unsubscribe_notify(task.id, "feishu", "chat-1", "thread-1")
    assert removed is True
    subs = await svc.list_notify_subs(task.id)
    assert len(subs) == 0


# ---------------------------------------------------------------------------
# get_task_detail (no parents/children/links)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_task_detail(svc, registry):
    task = await svc.create_task(title="x", body="do work", created_by="u")
    detail = await svc.get_task_detail(task.id)
    assert detail is not None
    assert detail["task"]["id"] == task.id
    assert detail["task"]["title"] == "x"
    assert "worker_context" in detail
    assert "events" in detail
    assert "comments" in detail
    # Dependency graph fields removed
    assert "parents" not in detail
    assert "children" not in detail
    assert "links" not in detail


@pytest.mark.asyncio
async def test_get_task_detail_not_found(svc):
    detail = await svc.get_task_detail("t_none")
    assert detail is None


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat(svc, registry):
    task = Task(
        id="t_h", title="x", status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_h", "lock-1", 900)
    result = await svc.heartbeat("t_h", "still working")
    assert result["task_id"] == "t_h"
    assert "heartbeat_at" in result


@pytest.mark.asyncio
async def test_heartbeat_not_running_rejected(svc):
    task = await svc.create_task(title="x", created_by="u")
    with pytest.raises(TaskStateError):
        await svc.heartbeat(task.id, "note")


# ---------------------------------------------------------------------------
# build_worker_context (async, with proposal/decision/progress segments)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_worker_context_basic(svc, registry):
    task = await svc.create_task(title="Test Task", body="Do something", created_by="u")
    ctx = await svc.build_worker_context(task)
    assert "Test Task" in ctx
    assert "Do something" in ctx
    assert task.id in ctx


@pytest.mark.asyncio
async def test_build_worker_context_no_host_paths(svc):
    task = Task(
        id="t_1", title="x", body="y",
        workspace_path="/home/user/secret/path",
    )
    ctx = await svc.build_worker_context(task)
    assert "/home/user/secret/path" not in ctx


@pytest.mark.asyncio
async def test_build_worker_context_includes_pending_proposal(svc, registry):
    """WAITING_APPROVAL task with unresolved change_proposed shows proposal."""
    task = _running_task("t_wp1")
    task = task.__class__(
        **{**task.__dict__, "status": TaskStatus.WAITING_APPROVAL,
           "claim_lock": None, "claim_expires": None,
           "current_run_id": None, "worker_token": None}
    )
    await registry.create_task(task)
    await registry.append_event(
        "t_wp1", "change_proposed",
        {"proposal": "switch to plan B", "run_id": 1}, run_id=1,
    )

    ctx = await svc.build_worker_context(task)
    assert "待审批提案" in ctx
    assert "switch to plan B" in ctx


@pytest.mark.asyncio
async def test_build_worker_context_includes_approval_decision(svc, registry):
    """After approve/reject, the decision segment shows the latest decision."""
    task = await svc.create_task(title="x", created_by="u")
    # Simulate a prior propose->approve cycle
    proposal_event = await registry.append_event(
        task.id, "change_proposed",
        {"proposal": "do X", "run_id": 1}, run_id=1,
    )
    await registry.append_event(
        task.id, "change_approved",
        {"proposal_event_id": proposal_event.id, "decision": "approved"},
    )

    ctx = await svc.build_worker_context(task)
    assert "审批决策" in ctx
    assert "approved" in ctx or "批准" in ctx


@pytest.mark.asyncio
async def test_build_worker_context_includes_progress(svc, registry):
    """Progress segment includes recent comment/propose/approve events."""
    task = await svc.create_task(title="x", created_by="u")
    await registry.append_event(task.id, "comment_added", {"author": "w"})
    proposal_event = await registry.append_event(
        task.id, "change_proposed", {"proposal": "p", "run_id": 1}, run_id=1,
    )
    await registry.append_event(
        task.id, "change_approved",
        {"proposal_event_id": proposal_event.id},
    )

    ctx = await svc.build_worker_context(task)
    assert "进度" in ctx


# ---------------------------------------------------------------------------
# _task_to_dict (no assignee/block/dependency fields)
# ---------------------------------------------------------------------------


def test_task_to_dict_has_no_assignee_or_block_fields():
    task = Task(
        id="t_d", title="x", body="y", status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc), version=1,
        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1),
        is_archived=False,
    )
    d = _task_to_dict(task)
    assert d["id"] == "t_d"
    assert d["status"] == "queued"
    assert d["is_archived"] is False
    assert d["scheduled_at"] is not None
    # Removed fields
    assert "assignee" not in d
    assert "block_kind" not in d
    assert "block_reason" not in d
    assert "pre_archive_status" not in d


def test_task_to_dict_includes_claim_and_failure_summary():
    now = datetime.now(timezone.utc)
    task = Task(
        id="t_d2", title="x", status=TaskStatus.FAILED,
        created_at=now, version=2,
        claim_lock="lock-1", claim_expires=now + timedelta(seconds=900),
        current_run_id=5, consecutive_failures=3,
        last_failure_error="boom",
    )
    d = _task_to_dict(task)
    assert d["status"] == "failed"
    assert d["current_run_id"] == 5
    assert d["consecutive_failures"] == 3
    assert d["last_failure_error"] == "boom"


def test_task_to_dict_includes_execution_configuration():
    task = Task(
        id="t_config", title="x", workspace_kind=TaskWorkspaceKind.DIR,
        workspace_path="/workspace/repo", skills=("review",),
        execution_policy=TaskExecutionPolicy(allowed_tools=("shell",)),
        model_override="model-x", max_runtime_seconds=120,
    )
    d = _task_to_dict(task)
    assert d["workspace_kind"] == "dir"
    assert d["workspace_path"] == "/workspace/repo"
    assert d["skills"] == ["review"]
    assert d["allowed_tools"] == ["shell"]
    assert d["model_override"] == "model-x"
    assert d["max_runtime_seconds"] == 120


# ---------------------------------------------------------------------------
# Dispatch delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_tick_without_run_service_raises(svc):
    with pytest.raises(TaskStateError):
        await svc.dispatch_tick()


@pytest.mark.asyncio
async def test_dispatch_tick_delegates(svc):
    class FakeRunService:
        def __init__(self):
            self.called = False

        async def dispatch_once(self):
            self.called = True
            return {"dispatched": 1}

    fake = FakeRunService()
    svc.set_run_service(fake)
    result = await svc.dispatch_tick()
    assert fake.called
    assert result == {"dispatched": 1}


# ---------------------------------------------------------------------------
# Bulk update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_atomic(svc):
    t1 = await svc.create_task(title="a", created_by="u")
    t2 = await svc.create_task(title="b", created_by="u")
    command = BulkUpdateCommand(items=(
        BulkUpdateItem(task_id=t1.id, fields={"priority": 5}, expected_version=1),
        BulkUpdateItem(task_id=t2.id, fields={"priority": 3}, expected_version=1),
    ))
    updated = await svc.bulk_update(command)
    assert len(updated) == 2
    t1_refreshed = await svc.get_task(t1.id)
    assert t1_refreshed.priority == 5


@pytest.mark.asyncio
async def test_bulk_update_running_rejected(svc, registry):
    t1 = await svc.create_task(title="a", created_by="u")
    task = Task(
        id="t_run3", title="r", status=TaskStatus.QUEUED,
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_run3", "lock-1", 900)
    command = BulkUpdateCommand(items=(
        BulkUpdateItem(task_id=t1.id, fields={"priority": 5}, expected_version=1),
        BulkUpdateItem(task_id="t_run3", fields={"priority": 3}, expected_version=1),
    ))
    with pytest.raises(TaskStateError):
        await svc.bulk_update(command)
