"""T19: HTTP/WS contract tests for app/interfaces/http/task_routes.py.

Manus-aligned 7-state machine: 5 swimlanes, no assignee/dependency/swarm,
intent-approval routes (propose-change/approve/reject/cancel/retry).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.task_service import TaskService
from app.domain.task import (
    BulkUpdateCommand,
    BulkUpdateResult,
    ClaimResult,
    DeliveryResult,
    FinishRunCommand,
    FinishRunResult,
    ProposalResolutionCommand,
    ProposalResolutionResult,
    RecoverRunCommand,
    Task,
    TaskAttachment,
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
from app.interfaces.http.dashboard import create_dashboard_router


# ---------------------------------------------------------------------------
# Fake registry (in-memory) - subset needed by TaskService for HTTP tests
# ---------------------------------------------------------------------------


class FakeTaskRegistry:
    def __init__(self):
        self._tasks: dict[str, Task] = {}
        self._runs: dict[int, TaskRun] = {}
        self._events: list[TaskEvent] = []
        self._comments: dict[str, list[TaskComment]] = {}
        self._attachments: dict[str, list[TaskAttachment]] = {}
        self._attachments_by_id: dict[str, TaskAttachment] = {}
        self._next_run_id = 1
        self._next_event_id = 1

    async def create_task(self, task: Task) -> Task:
        if task.id in self._tasks:
            raise TaskConflictError(f"duplicate id: {task.id}")
        self._tasks[task.id] = task
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list_tasks(self, board="default", cursor=None, limit=100, include_archived=False):
        items = [t for t in self._tasks.values() if t.board == board]
        if not include_archived:
            items = [t for t in items if not t.is_archived]
        items.sort(key=lambda t: (t.created_at or datetime.min.replace(tzinfo=timezone.utc), t.id))
        return TaskListPage(items=tuple(items[:limit]))

    async def update_task(self, task_id, fields, expected_version):
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.version != expected_version:
            raise TaskConflictError("version conflict")
        from dataclasses import replace as dc_replace
        normalized = dict(fields)
        updated = dc_replace(
            task, **normalized, version=task.version + 1,
            updated_at=datetime.now(timezone.utc),
        )
        self._tasks[task_id] = updated
        return updated

    async def bulk_update(self, command: BulkUpdateCommand) -> BulkUpdateResult:
        updated = []
        for item in command.items:
            t = await self.update_task(item.task_id, item.fields, item.expected_version)
            updated.append(t)
        return BulkUpdateResult(updated=tuple(updated))

    async def delete_task(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        del self._tasks[task_id]
        return True

    async def claim_task(self, task_id, claim_lock, lease_seconds, now=None):
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.QUEUED:
            return None
        from dataclasses import replace as dc_replace
        now = now or datetime.now(timezone.utc)
        run_id = self._next_run_id
        self._next_run_id += 1
        run = TaskRun(
            id=run_id, task_id=task_id, status=TaskRunStatus.RUNNING,
            claim_lock=claim_lock, started_at=now,
        )
        self._runs[run_id] = run
        updated = dc_replace(
            task, status=TaskStatus.RUNNING, claim_lock=claim_lock,
            claim_expires=now, current_run_id=run_id, worker_token=f"wt_{run_id}",
            last_heartbeat_at=now, version=task.version + 1,
        )
        self._tasks[task_id] = updated
        return ClaimResult(task=updated, run=run)

    async def record_heartbeat(self, task_id, run_id, claim_lock, now):
        task = self._tasks.get(task_id)
        if task is None or task.claim_lock != claim_lock or task.current_run_id != run_id:
            raise TaskConflictError("heartbeat CAS failed")
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
        from dataclasses import replace as dc_replace
        now = datetime.now(timezone.utc)
        if command.target_task_status is not None:
            new_status = command.target_task_status
        elif command.outcome == TaskRunOutcome.COMPLETED:
            new_status = TaskStatus.SUCCEEDED
        elif command.outcome == TaskRunOutcome.WAITING_APPROVAL:
            new_status = TaskStatus.WAITING_APPROVAL
        elif command.outcome in (TaskRunOutcome.CRASHED, TaskRunOutcome.TIMED_OUT):
            new_status = TaskStatus.EXPIRED
        elif command.outcome == TaskRunOutcome.TERMINATED:
            new_status = TaskStatus.CANCELLED
        else:
            new_status = TaskStatus.FAILED
        updated_task = dc_replace(
            task, status=new_status, claim_lock=None, claim_expires=None,
            current_run_id=None, worker_token=None,
            version=task.version + 1, updated_at=now,
        )
        self._tasks[command.task_id] = updated_task
        updated_run = dc_replace(
            run, status=TaskRunStatus.COMPLETED, outcome=command.outcome,
            ended_at=now, summary=command.summary,
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
        return tuple(
            t for t in self._tasks.values()
            if t.board == board and t.status == TaskStatus.QUEUED
            and not t.is_archived
            and (t.scheduled_at is None or t.scheduled_at <= now)
        )[:limit]

    async def list_running(self, board="default"):
        return tuple(
            t for t in self._tasks.values()
            if t.board == board and t.status == TaskStatus.RUNNING
        )

    async def add_comment(self, task_id, author, body):
        c = TaskComment(
            id=f"c_{uuid4().hex[:12]}", task_id=task_id, author=author,
            body=body, created_at=datetime.now(timezone.utc),
        )
        self._comments.setdefault(task_id, []).append(c)
        return c

    async def list_comments(self, task_id):
        return tuple(self._comments.get(task_id, []))

    async def append_event(self, task_id, kind, payload, run_id=None):
        e = TaskEvent(
            id=self._next_event_id, task_id=task_id, kind=kind,
            payload=dict(payload), run_id=run_id,
            created_at=datetime.now(timezone.utc),
        )
        self._next_event_id += 1
        self._events.append(e)
        return e

    async def list_events(self, task_id, since=0, limit=100):
        return tuple(
            e for e in self._events
            if e.task_id == task_id and e.id > since
        )[:limit]

    async def list_runs(self, task_id, limit=50):
        return tuple(
            r for r in self._runs.values()
            if r.task_id == task_id
        )[:limit]

    async def add_attachment(self, task_id, filename, stored_name, content_type, size, checksum, uploaded_by):
        att = TaskAttachment(
            id=f"a_{uuid4().hex[:12]}", task_id=task_id, filename=filename,
            stored_name=stored_name, content_type=content_type, size=size,
            checksum=checksum, uploaded_by=uploaded_by,
            created_at=datetime.now(timezone.utc),
        )
        self._attachments.setdefault(task_id, []).append(att)
        self._attachments_by_id[att.id] = att
        return att

    async def list_attachments(self, task_id):
        return tuple(self._attachments.get(task_id, []))

    async def get_attachment(self, attachment_id):
        return self._attachments_by_id.get(attachment_id)

    async def delete_attachment(self, attachment_id):
        att = self._attachments_by_id.pop(attachment_id, None)
        if att is None:
            return False
        self._attachments[att.task_id] = [
            a for a in self._attachments.get(att.task_id, []) if a.id != attachment_id
        ]
        return True

    async def subscribe_notify(self, task_id, platform, chat_id, thread_id=None):
        return True

    async def list_notify_subs(self, task_id):
        return ()

    async def unsubscribe_notify(self, task_id, platform, chat_id, thread_id=None):
        return True

    async def update_notify_sub_last_event(self, task_id, platform, chat_id, thread_id, last_terminal_event_id):
        return True

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
        """Atomic in-memory mirror of SQLiteTaskRegistry.resolve_proposal."""
        if not command.task_id:
            raise TaskValidationError("task_id must not be empty")
        if command.decision not in self._FAKE_DECISION_TO_KIND:
            raise TaskValidationError(
                f"invalid decision: {command.decision!r}"
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def registry():
    return FakeTaskRegistry()


@pytest.fixture
def task_service(registry, tmp_path):
    svc = TaskService(
        registry=registry,
        policy=TaskPolicy(),
        memory_store=None,
        attachments_root=tmp_path / "attachments",
        attachment_max_bytes=1024 * 1024,
        attachment_task_max_bytes=10 * 1024 * 1024,
    )
    return svc


@pytest.fixture
def app(task_service):
    app = FastAPI()
    app.include_router(create_dashboard_router(
        session_service=None,
        tool_service=None,
        model_service=None,
        health_provider=lambda: {},
        task_service=task_service,
        task_run_service=None,
    ))
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Board (5 swimlanes) / list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_returns_5_swimlanes(client, task_service, registry):
    await task_service.create_task(title="T1", created_by="u")
    resp = client.get("/chat/tasks/board")
    assert resp.status_code == 200
    data = resp.json()
    lane_ids = [c["id"] for c in data["columns"]]
    assert lane_ids == ["queued", "running", "waiting_approval", "failed_expired", "succeeded_cancelled"]
    queued_lane = next(c for c in data["columns"] if c["id"] == "queued")
    assert queued_lane["total"] == 1
    assert queued_lane["cards"][0]["title"] == "T1"


@pytest.mark.asyncio
async def test_board_orders_tasks_within_a_swimlane_by_newest_creation(client, registry):
    created_at = datetime(2026, 7, 19, tzinfo=timezone.utc)
    await registry.create_task(Task(
        id="t_old", title="旧任务", status=TaskStatus.QUEUED,
        priority=99, created_at=created_at,
    ))
    await registry.create_task(Task(
        id="t_new", title="新任务", status=TaskStatus.QUEUED,
        priority=0, created_at=created_at + timedelta(minutes=1),
    ))

    response = client.get("/chat/tasks/board")
    assert response.status_code == 200
    queued_lane = next(column for column in response.json()["columns"] if column["id"] == "queued")
    assert [card["id"] for card in queued_lane["cards"]] == ["t_new", "t_old"]


@pytest.mark.asyncio
async def test_list_excludes_archived_by_default(client, task_service, registry):
    t1 = await task_service.create_task(title="Active", created_by="u")
    t2 = await task_service.create_task(title="Archived", created_by="u")
    await registry.update_task(t2.id, {"is_archived": True}, expected_version=1)
    resp = client.get("/chat/tasks")
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = {i["id"] for i in items}
    assert t1.id in ids
    assert t2.id not in ids


# ---------------------------------------------------------------------------
# Create / get / patch / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_task(client, task_service):
    resp = client.post("/chat/tasks", json={"title": "调研架构", "created_by": "alice"})
    assert resp.status_code == 200
    tid = resp.json()["id"]
    assert tid.startswith("t_")
    assert resp.json()["status"] == "queued"
    resp = client.get(f"/chat/tasks/{tid}")
    assert resp.status_code == 200
    assert resp.json()["task"]["title"] == "调研架构"


@pytest.mark.asyncio
async def test_create_empty_title_returns_422(client):
    resp = client.post("/chat/tasks", json={"title": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"


@pytest.mark.asyncio
async def test_get_task_not_found_returns_404(client):
    resp = client.get("/chat/tasks/t_missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"


@pytest.mark.asyncio
async def test_patch_updates_fields(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.patch(f"/chat/tasks/{task.id}", json={
        "expected_version": 1, "title": "Updated",
    })
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated"
    assert resp.json()["version"] == 2


@pytest.mark.asyncio
async def test_patch_status_rejected(client, task_service):
    """status 不在 _PATCH_ALLOWED_FIELDS；PATCH 改 status 应被忽略或拒绝。"""
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.patch(f"/chat/tasks/{task.id}", json={
        "expected_version": 1, "status": "succeeded",
    })
    # status not updatable -> either 422 (no updatable fields) or 200 ignoring status
    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        assert resp.json()["status"] == "queued"


@pytest.mark.asyncio
async def test_patch_missing_expected_version_returns_422(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.patch(f"/chat/tasks/{task.id}", json={"title": "x"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"


@pytest.mark.asyncio
async def test_patch_stale_version_returns_409(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.patch(f"/chat/tasks/{task.id}", json={
        "expected_version": 99, "title": "x",
    })
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_conflict"


@pytest.mark.asyncio
async def test_delete_task(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.delete(f"/chat/tasks/{task.id}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_not_found(client):
    resp = client.delete("/chat/tasks/t_missing")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_atomic(client, task_service):
    t1 = await task_service.create_task(title="T1", created_by="u")
    t2 = await task_service.create_task(title="T2", created_by="u")
    resp = client.post("/chat/tasks/bulk", json={
        "items": [
            {"task_id": t1.id, "expected_version": 1, "fields": {"title": "U1"}},
            {"task_id": t2.id, "expected_version": 1, "fields": {"title": "U2"}},
        ]
    })
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    versions = {i["id"]: i["version"] for i in items}
    assert versions[t1.id] == 2
    assert versions[t2.id] == 2


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/comments", json={
        "body": "hello", "author": "alice",
    })
    assert resp.status_code == 200
    assert resp.json()["body"] == "hello"


@pytest.mark.asyncio
async def test_add_comment_empty_body_rejected(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/comments", json={"body": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Intent approval: propose-change / approve / reject / cancel / retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_change_on_non_running_returns_409(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/propose-change", json={"proposal": "p"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_state_invalid"


@pytest.mark.asyncio
async def test_approve_on_non_waiting_returns_409(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/approve")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_state_invalid"


async def _waiting_task(task_service, registry):
    from app.domain.task import TaskStatus
    task = await task_service.create_task(title="T", created_by="u")
    await registry.update_task(
        task.id, {"status": TaskStatus.WAITING_APPROVAL}, expected_version=1,
    )
    await registry.append_event(
        task.id, "change_proposed", {"proposal": "p", "run_id": 1}, run_id=1,
    )
    return task


@pytest.mark.asyncio
async def test_approve_with_note(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/approve", json={"note": "  ok  "})
    assert resp.status_code == 200
    assert resp.json()["note"] == "ok"


@pytest.mark.asyncio
async def test_reject_with_note(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/reject", json={"note": "reason"})
    assert resp.status_code == 200
    assert resp.json()["note"] == "reason"


@pytest.mark.asyncio
async def test_approve_no_body_compatible(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/approve")
    assert resp.status_code == 200
    assert resp.json()["note"] is None


@pytest.mark.asyncio
async def test_approve_note_too_long_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/approve", json={"note": "x" * 2001})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    after = len(await registry.list_events(task.id))
    assert after == before  # no decision event written on validation failure


@pytest.mark.asyncio
async def test_approve_note_non_string_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/approve", json={"note": ["a"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"


# ---------------------------------------------------------------------------
# T9: /revise route + shared _extract_note(required=...) + fixed messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revise_success_response_fields(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "请修改方案"})
    assert resp.status_code == 200
    data = resp.json()
    # Response matches TaskService.revise_change contract:
    # task_id/title/status/decision/proposal_event_id/note
    assert set(data.keys()) >= {
        "task_id", "title", "status", "decision",
        "proposal_event_id", "note",
    }
    assert data["task_id"] == task.id
    assert data["title"] == task.title
    assert data["status"] == "queued"
    assert data["decision"] == "revised"
    assert data["note"] == "请修改方案"
    assert isinstance(data["proposal_event_id"], int)
    assert data["proposal_event_id"] > 0


@pytest.mark.asyncio
async def test_revise_trims_note(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "  revise me  "})
    assert resp.status_code == 200
    assert resp.json()["note"] == "revise me"


@pytest.mark.asyncio
async def test_revise_body_non_object_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/revise", json="text")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before  # no decision event written


@pytest.mark.asyncio
async def test_revise_no_body_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/revise")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_revise_empty_object_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_revise_null_note_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": None})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_revise_blank_note_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "   "})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_revise_oversized_note_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "x" * 2001})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_revise_wrong_note_type_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": ["a"]})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_revise_numeric_note_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": 123})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"


@pytest.mark.asyncio
async def test_revise_extra_field_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(
        f"/chat/tasks/{task.id}/revise",
        json={"note": "redo", "extra": "field"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_revise_not_found_returns_404(client):
    resp = client.post("/chat/tasks/t_missing/revise", json={"note": "redo"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "task_not_found"
    assert resp.json()["error"]["message"] == "task not found"


@pytest.mark.asyncio
async def test_revise_non_waiting_returns_409(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "redo"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_state_invalid"
    assert resp.json()["error"]["message"] == "task state invalid"


@pytest.mark.asyncio
async def test_revise_conflict_returns_409_desensitized(client, task_service, registry):
    task = await _waiting_task(task_service, registry)

    async def conflict(command):
        raise TaskConflictError("secret version mismatch detail")
    registry.resolve_proposal = conflict

    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "redo"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_conflict"
    assert resp.json()["error"]["message"] == "task conflict"
    assert "secret" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_revise_service_validation_returns_422_desensitized(
    client, task_service, registry,
):
    task = await _waiting_task(task_service, registry)

    async def boom(command):
        raise TaskValidationError("service-side validation: sensitive detail")
    registry.resolve_proposal = boom

    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "redo"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    assert "sensitive" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_revise_unknown_exception_returns_500_desensitized(
    client, task_service, registry,
):
    task = await _waiting_task(task_service, registry)

    async def boom(command):
        raise Exception("sqlite3.OperationalError: sensitive internal /tmp/secret.db")
    registry.resolve_proposal = boom

    resp = client.post(f"/chat/tasks/{task.id}/revise", json={"note": "redo"})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "task_internal_error"
    assert resp.json()["error"]["message"] == "internal task error"
    assert "sensitive" not in resp.json()["error"]["message"]
    assert "sqlite3" not in resp.json()["error"]["message"]
    assert "/tmp/" not in resp.json()["error"]["message"]


# --- approve/reject regression: extra field, conflict, unknown exception ---


@pytest.mark.asyncio
async def test_approve_extra_field_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(
        f"/chat/tasks/{task.id}/approve",
        json={"note": "ok", "extra": "field"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_approve_body_non_object_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/approve", json="text")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"


@pytest.mark.asyncio
async def test_approve_conflict_returns_409_desensitized(client, task_service, registry):
    task = await _waiting_task(task_service, registry)

    async def conflict(command):
        raise TaskConflictError("secret version mismatch detail")
    registry.resolve_proposal = conflict

    resp = client.post(f"/chat/tasks/{task.id}/approve", json={"note": "ok"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_conflict"
    assert resp.json()["error"]["message"] == "task conflict"
    assert "secret" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_approve_service_validation_returns_422_desensitized(
    client, task_service, registry,
):
    task = await _waiting_task(task_service, registry)

    async def boom(command):
        raise TaskValidationError("service-side validation: sensitive detail")
    registry.resolve_proposal = boom

    resp = client.post(f"/chat/tasks/{task.id}/approve", json={"note": "ok"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    assert "sensitive" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_approve_unknown_exception_returns_500_desensitized(
    client, task_service, registry,
):
    task = await _waiting_task(task_service, registry)

    async def boom(command):
        raise Exception("sqlite3.OperationalError: sensitive internal /tmp/secret.db")
    registry.resolve_proposal = boom

    resp = client.post(f"/chat/tasks/{task.id}/approve", json={"note": "ok"})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "task_internal_error"
    assert resp.json()["error"]["message"] == "internal task error"
    assert "sensitive" not in resp.json()["error"]["message"]
    assert "sqlite3" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_reject_extra_field_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    before = len(await registry.list_events(task.id))
    resp = client.post(
        f"/chat/tasks/{task.id}/reject",
        json={"note": "no", "extra": "field"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"
    assert resp.json()["error"]["message"] == "invalid task request"
    after = len(await registry.list_events(task.id))
    assert after == before


@pytest.mark.asyncio
async def test_reject_body_non_object_returns_422(client, task_service, registry):
    task = await _waiting_task(task_service, registry)
    resp = client.post(f"/chat/tasks/{task.id}/reject", json="text")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"


@pytest.mark.asyncio
async def test_reject_conflict_returns_409_desensitized(client, task_service, registry):
    task = await _waiting_task(task_service, registry)

    async def conflict(command):
        raise TaskConflictError("secret version mismatch detail")
    registry.resolve_proposal = conflict

    resp = client.post(f"/chat/tasks/{task.id}/reject", json={"note": "no"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_conflict"
    assert resp.json()["error"]["message"] == "task conflict"
    assert "secret" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_reject_unknown_exception_returns_500_desensitized(
    client, task_service, registry,
):
    task = await _waiting_task(task_service, registry)

    async def boom(command):
        raise Exception("sqlite3.OperationalError: sensitive internal /tmp/secret.db")
    registry.resolve_proposal = boom

    resp = client.post(f"/chat/tasks/{task.id}/reject", json={"note": "no"})
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "task_internal_error"
    assert resp.json()["error"]["message"] == "internal task error"
    assert "sensitive" not in resp.json()["error"]["message"]
    assert "sqlite3" not in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_cancel_queued_task(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_retry_non_failed_returns_409(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_state_invalid"


@pytest.mark.asyncio
async def test_retry_failed_task(client, task_service, registry):
    task = await task_service.create_task(title="T", created_by="u")
    await registry.update_task(task.id, {"status": TaskStatus.FAILED}, expected_version=1)
    resp = client.post(f"/chat/tasks/{task.id}/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_upload_and_download(client, task_service, tmp_path):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(
        f"/chat/tasks/{task.id}/attachments",
        files={"file": ("hello.txt", b"hi", "text/plain")},
        data={"uploaded_by": "alice"},
    )
    assert resp.status_code == 200
    att = resp.json()
    assert att["filename"] == "hello.txt"
    assert att["size"] == 2
    resp = client.get(f"/chat/tasks/attachments/{att['id']}")
    assert resp.status_code == 200
    assert resp.content == b"hi"
    assert "attachment" in resp.headers["content-disposition"]
    assert "nosniff" in resp.headers["x-content-type-options"]


@pytest.mark.asyncio
async def test_attachment_too_large_returns_413(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    big = b"x" * (2 * 1024 * 1024)
    resp = client.post(
        f"/chat/tasks/{task.id}/attachments",
        files={"file": ("big.bin", big, "application/octet-stream")},
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "task_attachment_too_large"


@pytest.mark.asyncio
async def test_attachment_download_not_found(client):
    resp = client.get("/chat/tasks/attachments/a_missing")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "attachment_not_found"


@pytest.mark.asyncio
async def test_attachment_download_unicode_filename_does_not_500(client, task_service):
    """Non-ASCII attachment filenames must not crash the download endpoint.

    HTTP headers are latin-1; the legacy ``filename`` parameter built from
    ``stored_name`` must stay ASCII-only while the real name is carried via
    the RFC 5987 ``filename*`` parameter. Bug: ``stored_name`` preserves the
    non-ASCII original filename (e.g. ``<uuid>_横向-邮箱归属.md``), so
    ``filename="{stored_name}"`` raised UnicodeEncodeError -> HTTP 500."""
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(
        f"/chat/tasks/{task.id}/attachments",
        files={"file": ("横向-邮箱归属.md", "# 横向-邮箱归属\n".encode("utf-8"), "text/markdown")},
        data={"uploaded_by": "alice"},
    )
    assert resp.status_code == 200
    att = resp.json()
    assert att["filename"] == "横向-邮箱归属.md"
    resp = client.get(f"/chat/tasks/attachments/{att['id']}")
    assert resp.status_code == 200
    assert resp.content == "# 横向-邮箱归属\n".encode("utf-8")
    cd = resp.headers.get("content-disposition", "")
    # RFC 5987 UTF-8 form carries the real (non-ASCII) name.
    assert "filename*=UTF-8''" in cd
    assert quote("横向-邮箱归属.md", safe="") in cd
    # Legacy filename must be latin-1 encodable so the header builds.
    legacy = cd.split('filename="', 1)[1].split('"', 1)[0]
    legacy.encode("latin-1")  # must not raise


# ---------------------------------------------------------------------------
# Runs / inspect / dispatch
# ---------------------------------------------------------------------------


def test_inspect_dispatcher_without_run_service(client):
    resp = client.get("/chat/tasks/inspect")
    assert resp.status_code == 200
    assert "active" in resp.json()


def test_dispatch_without_run_service_returns_409(client):
    resp = client.post("/chat/tasks/dispatch")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_state_invalid"


# ---------------------------------------------------------------------------
# WebSocket events
# ---------------------------------------------------------------------------


def test_ws_events_route_registered(client):
    # Starlette 1.x wraps included routers in _IncludedRouter (path=None,
    # real routes on .original_router); walk those + Mounts recursively.
    paths: list[str] = []

    def walk(routes):
        for r in routes:
            p = getattr(r, "path", None)
            if p:
                paths.append(p)
            orig = getattr(r, "original_router", None)
            if orig is not None and hasattr(orig, "routes"):
                walk(orig.routes)
            sub = getattr(r, "routes", None)
            if isinstance(sub, list):
                walk(sub)

    walk(client.app.routes)
    assert "/chat/tasks/events" in paths


def test_ws_events_replay_envelope(client, task_service, registry):
    asyncio_run(task_service.create_task(title="T", created_by="u"))
    with client.websocket_connect("/chat/tasks/events?since=0") as ws:
        msg = ws.receive_json()
        assert msg["kind"] == "created"
        for key in ("id", "task_id", "run_id", "kind", "payload", "created_at"):
            assert key in msg, f"missing {key} in event envelope"
        assert isinstance(msg["id"], int)
        assert msg["id"] > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import threading
    box: dict = {}
    def runner():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:
            box["error"] = exc
    t = threading.Thread(target=runner)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")
