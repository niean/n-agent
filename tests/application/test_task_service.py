"""T4: TaskService tests (Manus-aligned 7-state machine).

Tests CRUD, idempotency, optimistic lock, RUNNING guards, attachments,
notify subs, build_worker_context, and the new user-action surface
(propose_change / approve_change / reject_change / cancel_task /
retry_task).

Uses an in-memory FakeTaskRegistry to isolate from SQLite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.application.task_service import TaskService, _task_to_dict
from app.domain.task import (
    BulkUpdateCommand,
    BulkUpdateItem,
    ClaimResult,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
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
async def test_create_task_idempotency(svc):
    await svc.create_task(title="x", created_by="u", idempotency_key="k1")
    with pytest.raises(TaskConflictError):
        await svc.create_task(title="x", created_by="u", idempotency_key="k1")


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
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, session_id: str, content: str):
        self.calls.append((session_id, content))


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
    assert any(sid == "dashboard-s1" and "已取消" in c for (sid, c) in writer.calls)


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
