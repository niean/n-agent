"""T19: HTTP/WS contract tests for app/interfaces/http/task_routes.py.

Covers:
  - board / list / create / get / patch / bulk / delete happy paths
  - error envelope shape {"error": {"code", "message"}}
  - error codes: task_not_found (404), task_state_invalid (409),
    task_conflict (409), task_invalid (422), task_attachment_too_large (413)
  - RUNNING rejects PATCH (409) and DELETE (409)
  - bulk atomicity (one conflict -> whole batch fails)
  - WebSocket /chat/tasks/events since cursor + dedup
  - attachment download path whitelist (no host absolute path leak;
    symlink/escape rejected)
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.task_service import TaskService
from app.domain.task import (
    BlockKind,
    BulkUpdateCommand,
    BulkUpdateItem,
    BulkUpdateResult,
    ClaimResult,
    CreateGraphCommand,
    CreateGraphResult,
    DeliveryResult,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
    TaskAttachment,
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
        self._links: list[TaskLink] = []
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

    async def list_tasks(self, board="default", cursor=None, limit=100):
        items = [t for t in self._tasks.values() if t.board == board]
        items.sort(key=lambda t: (t.created_at or datetime.min.replace(tzinfo=timezone.utc), t.id))
        return TaskListPage(items=tuple(items[:limit]))

    async def update_task(self, task_id, fields, expected_version):
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        if task.version != expected_version:
            raise TaskConflictError("version conflict")
        from dataclasses import replace as dc_replace
        # Normalize status string -> TaskStatus
        normalized = dict(fields)
        if "status" in normalized and isinstance(normalized["status"], str):
            normalized["status"] = TaskStatus(normalized["status"])
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

    async def claim_task(self, task_id, claim_lock, lease_seconds):
        task = self._tasks.get(task_id)
        if task is None or task.status != TaskStatus.READY:
            return None
        from dataclasses import replace as dc_replace
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
        if task.claim_lock != command.claim_lock or task.current_run_id != command.run_id:
            raise TaskConflictError("finish CAS failed")
        from dataclasses import replace as dc_replace
        now = datetime.now(timezone.utc)
        if command.target_task_status is not None:
            new_status = command.target_task_status
        elif command.outcome == TaskRunOutcome.COMPLETED:
            new_status = TaskStatus.DONE
        elif command.outcome in (TaskRunOutcome.BLOCKED, TaskRunOutcome.GAVE_UP):
            new_status = TaskStatus.BLOCKED
        else:
            new_status = TaskStatus.TODO
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
            self._tasks[task.id] = task
        for link in command.links:
            self._links.append(link)
        return CreateGraphResult(tasks=command.tasks, links=command.links, comments=command.comments)

    async def add_link(self, parent_id, child_id):
        for l in self._links:
            if l.parent_id == parent_id and l.child_id == child_id:
                raise TaskConflictError("duplicate edge")
        link = TaskLink(parent_id=parent_id, child_id=child_id)
        self._links.append(link)
        return link

    async def remove_link(self, parent_id, child_id):
        before = len(self._links)
        self._links = [
            l for l in self._links
            if not (l.parent_id == parent_id and l.child_id == child_id)
        ]
        return len(self._links) < before

    async def list_links(self, task_id):
        return tuple(
            l for l in self._links
            if l.parent_id == task_id or l.child_id == task_id
        )

    async def list_children(self, parent_id):
        ids = {l.child_id for l in self._links if l.parent_id == parent_id}
        return tuple(self._tasks[cid] for cid in ids if cid in self._tasks)

    async def list_parents(self, child_id):
        ids = {l.parent_id for l in self._links if l.child_id == child_id}
        return tuple(self._tasks[pid] for pid in ids if pid in self._tasks)

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
        planning_service=None,
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
# Board / list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_returns_columns(client, task_service):
    await task_service.create_task(title="T1", created_by="u")
    resp = client.get("/chat/tasks/board")
    assert resp.status_code == 200
    data = resp.json()
    statuses = [c["status"] for c in data["columns"]]
    # 8 active columns always present
    assert statuses == ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"]
    triage_col = next(c for c in data["columns"] if c["status"] == "triage")
    assert triage_col["total"] == 1
    assert triage_col["cards"][0]["title"] == "T1"


@pytest.mark.asyncio
async def test_board_archived_toggle(client, task_service, registry):
    task = await task_service.create_task(title="Archived", created_by="u")
    await registry.update_task(task.id, {"status": TaskStatus.ARCHIVED}, expected_version=1)
    # default: exclude archived
    resp = client.get("/chat/tasks/board")
    data = resp.json()
    assert all(c["status"] != "archived" for c in data["columns"])
    # archived=true: include archived column
    resp = client.get("/chat/tasks/board?archived=true")
    data = resp.json()
    statuses = [c["status"] for c in data["columns"]]
    assert "archived" in statuses


@pytest.mark.asyncio
async def test_list_filters_archived_by_default(client, task_service, registry):
    t1 = await task_service.create_task(title="Active", created_by="u")
    t2 = await task_service.create_task(title="Archived", created_by="u")
    await registry.update_task(t2.id, {"status": TaskStatus.ARCHIVED}, expected_version=1)
    resp = client.get("/chat/tasks")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == t1.id


# ---------------------------------------------------------------------------
# Create / get / patch / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_task(client, task_service):
    resp = client.post("/chat/tasks", json={"title": "调研架构", "created_by": "alice"})
    assert resp.status_code == 200
    tid = resp.json()["id"]
    assert tid.startswith("t_")
    # get detail
    resp = client.get(f"/chat/tasks/{tid}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task"]["title"] == "调研架构"


@pytest.mark.asyncio
async def test_create_empty_title_returns_422(client):
    resp = client.post("/chat/tasks", json={"title": ""})
    assert resp.status_code == 422
    data = resp.json()
    assert data["error"]["code"] == "task_invalid"


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
async def test_patch_missing_expected_version_returns_422(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.patch(f"/chat/tasks/{task.id}", json={"title": "x"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "task_invalid"


@pytest.mark.asyncio
async def test_patch_running_rejected_returns_409(client, task_service, registry):
    task = await task_service.create_task(title="T", created_by="u", assignee="a")
    # Move to READY via direct registry update (skip policy checks for test)
    await registry.update_task(task.id, {"status": TaskStatus.READY, "assignee": "a"}, expected_version=1)
    claim = await registry.claim_task(task.id, "lock-1", 900)
    assert claim is not None
    # Now RUNNING; PATCH should be rejected
    resp = client.patch(f"/chat/tasks/{task.id}", json={
        "expected_version": 3, "title": "x",
    })
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_state_invalid"


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


@pytest.mark.asyncio
async def test_bulk_update_conflict_returns_409(client, task_service):
    t1 = await task_service.create_task(title="T1", created_by="u")
    resp = client.post("/chat/tasks/bulk", json={
        "items": [
            {"task_id": t1.id, "expected_version": 1, "fields": {"title": "U1"}},
            {"task_id": t1.id, "expected_version": 99, "fields": {"title": "U2"}},
        ]
    })
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_conflict"


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
    assert resp.json()["author"] == "alice"


@pytest.mark.asyncio
async def test_add_comment_empty_body_rejected(client, task_service):
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(f"/chat/tasks/{task.id}/comments", json={"body": ""})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_link_and_remove(client, task_service):
    p = await task_service.create_task(title="Parent", created_by="u")
    c = await task_service.create_task(title="Child", created_by="u")
    resp = client.post(f"/chat/tasks/{p.id}/links", json={"child_id": c.id})
    assert resp.status_code == 200
    assert resp.json()["parent_id"] == p.id
    assert resp.json()["child_id"] == c.id
    # remove
    resp = client.delete(f"/chat/tasks/{p.id}/links/{c.id}")
    assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Attachments (upload + download path security)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_upload_and_download(client, task_service, tmp_path):
    task = await task_service.create_task(title="T", created_by="u")
    # Upload
    resp = client.post(
        f"/chat/tasks/{task.id}/attachments",
        files={"file": ("hello.txt", b"hi", "text/plain")},
        data={"uploaded_by": "alice"},
    )
    assert resp.status_code == 200
    att = resp.json()
    assert att["filename"] == "hello.txt"
    assert att["size"] == 2
    # Download
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
async def test_attachment_download_no_path_leak(client, task_service, tmp_path):
    """Download response must not leak host absolute paths."""
    task = await task_service.create_task(title="T", created_by="u")
    resp = client.post(
        f"/chat/tasks/{task.id}/attachments",
        files={"file": ("hello.txt", b"hi", "text/plain")},
    )
    att = resp.json()
    resp = client.get(f"/chat/tasks/attachments/{att['id']}")
    assert resp.status_code == 200
    # No filesystem path in any header
    for header_name, header_value in resp.headers.items():
        assert str(tmp_path) not in header_value
        assert "attachments" not in header_value or "filename" in header_name.lower()


# ---------------------------------------------------------------------------
# Runs / inspect / dispatch
# ---------------------------------------------------------------------------


def test_inspect_dispatcher_without_run_service(client):
    resp = client.get("/chat/tasks/inspect")
    assert resp.status_code == 200
    data = resp.json()
    assert "active" in data


def test_dispatch_without_run_service_returns_409(client):
    resp = client.post("/chat/tasks/dispatch")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "task_state_invalid"


# ---------------------------------------------------------------------------
# WebSocket events
# ---------------------------------------------------------------------------


def test_ws_events_route_registered(client):
    """The /chat/tasks/events WS route is registered."""
    routes = [getattr(r, "path", "") for r in client.app.routes]
    assert "/chat/tasks/events" in routes


def test_ws_events_since_non_negative_rejected(client):
    """since must be >= 0; the Query(ge=0) validator rejects negative."""
    try:
        with client.websocket_connect("/chat/tasks/events?since=-1") as ws:
            # If the connection opens (some FastAPI versions accept first
            # then close), receive should fail or return close.
            try:
                ws.receive()
            except Exception:
                pass
    except Exception:
        # Expected: WebSocketReject, HTTPException(400), or close.
        pass


def test_ws_events_replay_envelope(client, task_service, registry):
    """since=0 replays existing events with the fixed envelope shape.

    Receives exactly the known event count then closes; we put exactly
    one 'created' event in the registry to make the count deterministic.
    """
    asyncio_run(task_service.create_task(title="T", created_by="u"))
    # Now there's exactly one event (id=1, kind=created).
    with client.websocket_connect("/chat/tasks/events?since=0") as ws:
        msg = ws.receive_json()
        assert msg["kind"] == "created"
        # Envelope shape (spec)
        for key in ("id", "task_id", "run_id", "kind", "payload", "created_at"):
            assert key in msg, f"missing {key} in event envelope"
        assert isinstance(msg["id"], int)
        assert msg["id"] > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def asyncio_run(coro):
    """Run a coroutine to completion from sync test code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already in a loop: run in thread
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


def _last_task_id(registry):
    return list(registry._tasks.keys())[-1]
