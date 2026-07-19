"""T12: TaskService tests.

Tests CRUD, idempotency, optimistic lock, RUNNING guards, attachments,
notify subs, build_worker_context, and the TaskServiceProtocol surface
(get_task_detail/complete/block/heartbeat/add_comment/create_subtask/
link/build_worker_context).

Uses an in-memory FakeTaskRegistry to isolate from SQLite.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.application.task_service import TaskService
from app.domain.task import (
    BlockKind,
    BulkUpdateCommand,
    BulkUpdateItem,
    ClaimResult,
    CreateGraphCommand,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
    TaskAttachment,
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
    TaskRunStatus,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    TaskWorkspaceKind,
)
from app.domain.task_policy import TaskPolicy


# ---------------------------------------------------------------------------
# Fake registry (in-memory)
# ---------------------------------------------------------------------------


class FakeTaskRegistry:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._runs: dict[int, TaskRun] = {}
        self._events: list[TaskEvent] = []
        self._comments: dict[str, list[TaskComment]] = {}
        self._attachments: dict[str, list[TaskAttachment]] = {}
        self._links: list[TaskLink] = []
        self._notify_subs: list[dict] = []
        self._next_run_id = 1
        self._next_event_id = 1

    async def create_task(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise TaskConflictError(f"duplicate id: {task.id}")
        # Check idempotency
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

    async def list_tasks(self, board="default", cursor=None, limit=100):
        items = [
            t for t in self._tasks.values() if t.board == board
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
        updated = dc_replace(task, **dict(fields), version=task.version + 1,
                             updated_at=datetime.now(timezone.utc))
        self._tasks[task_id] = updated
        return updated

    async def bulk_update(self, command):
        updated = []
        for item in command.items:
            t = await self.update_task(item.task_id, item.fields, item.expected_version)
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
        if task is None or task.status != TaskStatus.READY:
            return None
        from dataclasses import replace as dc_replace
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        run_id = self._next_run_id
        self._next_run_id += 1
        run = TaskRun(
            id=run_id, task_id=task_id, status=TaskRunStatus.RUNNING,
            claim_lock=claim_lock, started_at=now,
        )
        self._runs[run_id] = run
        updated = dc_replace(
            task, status=TaskStatus.RUNNING, claim_lock=claim_lock,
            current_run_id=run_id, worker_token=f"wt_{run_id}",
            last_heartbeat_at=now, version=task.version + 1,
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
        # Determine new status
        if command.target_task_status is not None:
            new_status = command.target_task_status
        elif command.outcome == TaskRunOutcome.COMPLETED:
            new_status = TaskStatus.DONE
        elif command.outcome in (TaskRunOutcome.BLOCKED, TaskRunOutcome.GAVE_UP):
            new_status = TaskStatus.BLOCKED
        else:
            new_status = TaskStatus.TODO
        # Increment failures for retryable
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
            result=command.summary if command.outcome == TaskRunOutcome.COMPLETED else task.result,
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

    async def list_ready(self, board="default", limit=100):
        return tuple(
            t for t in self._tasks.values()
            if t.board == board and t.status == TaskStatus.READY
        )

    async def list_running(self, board="default"):
        return tuple(
            t for t in self._tasks.values()
            if t.board == board and t.status == TaskStatus.RUNNING
        )

    async def recompute_ready(self, board="default"):
        return ()

    async def create_graph(self, command: CreateGraphCommand):
        for task in command.tasks:
            await self.create_task(task)
        for link in command.links:
            await self.add_link(link.parent_id, link.child_id)
        from app.domain.task import CreateGraphResult
        return CreateGraphResult(tasks=command.tasks, links=command.links, comments=command.comments)

    async def add_link(self, parent_id, child_id):
        if parent_id == child_id:
            raise TaskValidationError("self-loop")
        if parent_id not in self._tasks or child_id not in self._tasks:
            raise TaskNotFoundError("parent or child not found")
        link = TaskLink(parent_id=parent_id, child_id=child_id)
        for existing in self._links:
            if existing.parent_id == parent_id and existing.child_id == child_id:
                raise TaskConflictError("duplicate link")
        # Cycle check: is there already a path from child_id to parent_id?
        if self._has_path(child_id, parent_id):
            raise TaskValidationError("cycle detected")
        self._links.append(link)
        return link

    def _has_path(self, start: str, target: str) -> bool:
        if start == target:
            return True
        visited: set[str] = set()
        stack: list[str] = [start]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            for link in self._links:
                if link.parent_id == node:
                    if link.child_id == target:
                        return True
                    stack.append(link.child_id)
        return False

    async def remove_link(self, parent_id, child_id):
        before = len(self._links)
        self._links = [
            l for l in self._links if not (l.parent_id == parent_id and l.child_id == child_id)
        ]
        return len(self._links) < before

    async def list_links(self, task_id):
        return tuple(
            l for l in self._links if l.parent_id == task_id or l.child_id == task_id
        )

    async def list_children(self, parent_id):
        child_ids = {l.child_id for l in self._links if l.parent_id == parent_id}
        return tuple(self._tasks[cid] for cid in child_ids if cid in self._tasks)

    async def list_parents(self, child_id):
        parent_ids = {l.parent_id for l in self._links if l.child_id == child_id}
        return tuple(self._tasks[pid] for pid in parent_ids if pid in self._tasks)

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

    async def add_attachment(self, task_id, filename, stored_name, content_type, size, checksum, uploaded_by):
        from uuid import uuid4
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
    assert task.status == TaskStatus.TRIAGE
    assert task.title == "调研架构"
    assert task.board == "default"
    assert task.version == 1
    # created event appended
    events = await registry.list_events(task.id)
    assert any(e.kind == "created" for e in events)


@pytest.mark.asyncio
async def test_create_task_empty_title_rejected(svc):
    with pytest.raises(TaskValidationError):
        await svc.create_task(title="", created_by="u")
    with pytest.raises(TaskValidationError):
        await svc.create_task(title="   ", created_by="u")


@pytest.mark.asyncio
async def test_create_task_idempotency(svc):
    t1 = await svc.create_task(title="x", created_by="u", idempotency_key="k1")
    # Second call with same key should raise (registry enforces uniqueness)
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
        id="t_run", title="r", status=TaskStatus.READY, assignee="d",
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
        id="t_run2", title="r", status=TaskStatus.READY, assignee="d",
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_run2", "lock-1", 900)
    with pytest.raises(TaskStateError):
        await svc.delete_task("t_run2")


@pytest.mark.asyncio
async def test_delete_task_cleans_execution_session(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    # Simulate execution session
    await registry.update_task(
        task.id, {"execution_session_id": "task-t_exec"}, expected_version=1
    )
    await svc.delete_task(task.id)
    assert svc.memory_store.deleted_sessions == ["task-t_exec"]


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign(svc):
    task = await svc.create_task(title="x", created_by="u")
    updated = await svc.assign(task.id, "worker-1", expected_version=1)
    assert updated.assignee == "worker-1"


@pytest.mark.asyncio
async def test_promote_to_ready(svc, registry):
    task = await svc.create_task(title="x", created_by="u", assignee="d")
    # Move to TODO first (TRIAGE -> TODO is allowed)
    await registry.update_task(task.id, {"status": TaskStatus.TODO}, expected_version=1)
    updated = await svc.promote_to_ready(task.id, expected_version=2)
    assert updated.status == TaskStatus.READY


@pytest.mark.asyncio
async def test_archive_unarchive(svc):
    task = await svc.create_task(title="x", created_by="u")
    archived = await svc.archive(task.id, expected_version=1)
    assert archived.status == TaskStatus.ARCHIVED
    assert archived.pre_archive_status == TaskStatus.TRIAGE
    unarchived = await svc.unarchive(task.id, expected_version=2)
    assert unarchived.status == TaskStatus.TRIAGE
    assert unarchived.pre_archive_status is None


@pytest.mark.asyncio
async def test_archive_running_rejected(svc, registry):
    task = Task(
        id="t_ar", title="r", status=TaskStatus.READY, assignee="d",
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_ar", "lock-1", 900)
    with pytest.raises(TaskStateError):
        await svc.archive("t_ar", expected_version=1)


# ---------------------------------------------------------------------------
# Dependency graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link(svc, registry):
    parent = await svc.create_task(title="p", created_by="u")
    child = await svc.create_task(title="c", created_by="u")
    result = await svc.link(parent.id, child.id)
    assert result["parent_id"] == parent.id
    assert result["child_id"] == child.id


@pytest.mark.asyncio
async def test_unlink(svc, registry):
    parent = await svc.create_task(title="p", created_by="u")
    child = await svc.create_task(title="c", created_by="u")
    await svc.link(parent.id, child.id)
    removed = await svc.unlink(parent.id, child.id)
    assert removed is True


@pytest.mark.asyncio
async def test_link_cycle_rejected(svc, registry):
    t1 = await svc.create_task(title="a", created_by="u")
    t2 = await svc.create_task(title="b", created_by="u")
    await svc.link(t1.id, t2.id)
    with pytest.raises(Exception):
        await svc.link(t2.id, t1.id)


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
    # Override max to small value
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
async def test_upload_attachment_control_chars(svc):
    task = await svc.create_task(title="x", created_by="u")
    with pytest.raises(TaskValidationError):
        await svc.upload_attachment(task.id, "bad\x00name", b"x")


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
# Worker-facing ops (TaskServiceProtocol)
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


@pytest.mark.asyncio
async def test_get_task_detail_not_found(svc):
    detail = await svc.get_task_detail("t_none")
    assert detail is None


@pytest.mark.asyncio
async def test_complete_returns_intent(svc, registry):
    task = Task(
        id="t_c", title="x", status=TaskStatus.READY, assignee="d",
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_c", "lock-1", 900)
    result = await svc.complete("t_c", "done", {"key": "val"}, [])
    assert result["outcome"] == "completed"
    assert result["summary"] == "done"
    assert result["metadata"] == {"key": "val"}
    # Intent event written
    events = await registry.list_events("t_c")
    assert any(e.kind == "complete_requested" for e in events)


@pytest.mark.asyncio
async def test_complete_not_running_rejected(svc, registry):
    task = await svc.create_task(title="x", created_by="u")
    with pytest.raises(TaskStateError):
        await svc.complete(task.id, "done", {}, [])


@pytest.mark.asyncio
async def test_block_returns_intent(svc, registry):
    task = Task(
        id="t_b", title="x", status=TaskStatus.READY, assignee="d",
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_b", "lock-1", 900)
    result = await svc.block("t_b", "need input", "needs_input")
    assert result["outcome"] == "blocked"
    assert result["kind"] == "needs_input"
    events = await registry.list_events("t_b")
    assert any(e.kind == "block_requested" for e in events)


@pytest.mark.asyncio
async def test_block_invalid_kind(svc, registry):
    task = Task(
        id="t_bk", title="x", status=TaskStatus.READY, assignee="d",
        created_at=datetime.now(timezone.utc), version=1,
    )
    await registry.create_task(task)
    await registry.claim_task("t_bk", "lock-1", 900)
    with pytest.raises(TaskValidationError):
        await svc.block("t_bk", "r", "invalid_kind")


@pytest.mark.asyncio
async def test_heartbeat(svc, registry):
    task = Task(
        id="t_h", title="x", status=TaskStatus.READY, assignee="d",
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


@pytest.mark.asyncio
async def test_create_subtask(svc, registry):
    parent = await svc.create_task(title="p", created_by="u")
    result = await svc.create_subtask(parent.id, "child task", "body")
    assert result["title"] == "child task"
    assert result["status"] == "triage"
    # Link created
    children = await registry.list_children(parent.id)
    assert any(c.id == result["id"] for c in children)


@pytest.mark.asyncio
async def test_build_worker_context(svc):
    task = Task(id="t_1", title="Test Task", body="Do something important")
    ctx = svc.build_worker_context(task)
    assert "Test Task" in ctx
    assert "Do something important" in ctx
    assert "t_1" in ctx


@pytest.mark.asyncio
async def test_build_worker_context_no_host_paths(svc):
    task = Task(
        id="t_1", title="x", body="y",
        workspace_path="/home/user/secret/path",
    )
    ctx = svc.build_worker_context(task)
    assert "/home/user/secret/path" not in ctx


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
        id="t_run3", title="r", status=TaskStatus.READY, assignee="d",
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
