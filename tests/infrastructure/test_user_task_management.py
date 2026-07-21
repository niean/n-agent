"""用户侧任务工具执行器测试（UserTaskToolExecutor）。

spec: spec-260720-chat-natural-language-task.md, spec-260721-chat-nl-approval.md
覆盖 create_task / list_tasks / approve_task / reject_task / revise_task 的会话绑定、
参数校验、错误映射、公开字段白名单、幂等键、分页过滤与未知工具名。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from app.domain.task import (
    Task,
    TaskConflictError,
    TaskListCursor,
    TaskListPage,
    TaskNotFoundError,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    TaskWorkspaceKind,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
)
from app.infrastructure.tools.user_task_management import UserTaskToolExecutor


def _task(*, id="t_1", title="标题", body="目标", origin_session_id="sess-1",
          status=TaskStatus.QUEUED, goal_mode=False, is_archived=False):
    now = datetime.now(timezone.utc)
    return Task(
        id=id, title=title, body=body, status=status, origin_session_id=origin_session_id,
        created_at=now, updated_at=now, goal_mode=goal_mode,
        workspace_kind=TaskWorkspaceKind.SCRATCH, is_archived=is_archived,
    )


class FakeUserTaskService:
    """满足 UserTaskServiceProtocol 的 async fake。"""

    def __init__(self):
        self.created: list[dict[str, Any]] = []
        self.list_pages: list[TaskListPage] = []
        self.raise_on_create: Exception | None = None
        self.raise_on_list: Exception | None = None
        self.list_calls = 0
        # Approval-related state
        self.approval_calls: list[dict[str, Any]] = []
        self.tasks_by_id: dict[str, Task] = {}
        self.latest_waiting_task: Task | None = None
        self.latest_waiting_calls: list[str] = []
        self.get_task_calls: list[str] = []
        self.raise_on_approve: Exception | None = None
        self.raise_on_reject: Exception | None = None
        self.raise_on_revise: Exception | None = None
        self.raise_on_get_task: Exception | None = None
        self.raise_on_latest_waiting: Exception | None = None
        self.approve_result: dict[str, Any] = {
            "task_id": "t_1",
            "decision": "approved",
            "proposal_event_id": "e_1",
            "note": None,
            "status": "queued",
        }
        self.reject_result: dict[str, Any] = {
            "task_id": "t_1",
            "decision": "rejected",
            "proposal_event_id": "e_1",
            "note": None,
            "status": "queued",
        }
        self.revise_result: dict[str, Any] = {
            "task_id": "t_1",
            "decision": "revised",
            "proposal_event_id": "e_1",
            "note": "修订指示",
            "status": "queued",
            "title": "标题",
        }

    async def create_task(self, *, title, body="", priority=0, created_by="",
                          origin_session_id=None, idempotency_key=None,
                          skills=(), goal_mode=False, **_kw):
        if self.raise_on_create is not None:
            raise self.raise_on_create
        rec = dict(title=title, body=body, priority=priority, created_by=created_by,
                   origin_session_id=origin_session_id, idempotency_key=idempotency_key,
                   skills=tuple(skills), goal_mode=goal_mode)
        self.created.append(rec)
        return _task(title=title, body=body, origin_session_id=origin_session_id, goal_mode=goal_mode)

    async def list_tasks(self, board="default", cursor=None, limit=100):
        self.list_calls += 1
        if self.raise_on_list is not None:
            raise self.raise_on_list
        if self.list_pages:
            return self.list_pages.pop(0)
        return TaskListPage(items=(), next_cursor=None)

    async def get_task(self, task_id: str) -> Task:
        self.get_task_calls.append(task_id)
        if self.raise_on_get_task is not None:
            raise self.raise_on_get_task
        task = self.tasks_by_id.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task

    async def latest_waiting_approval_in_session(self, session_id: str) -> Task | None:
        self.latest_waiting_calls.append(session_id)
        if self.raise_on_latest_waiting is not None:
            raise self.raise_on_latest_waiting
        return self.latest_waiting_task

    async def approve_change(self, task_id: str, note: str | None = None) -> dict[str, Any]:
        self.approval_calls.append({"method": "approve", "task_id": task_id, "note": note})
        if self.raise_on_approve is not None:
            raise self.raise_on_approve
        result = dict(self.approve_result)
        result["task_id"] = task_id
        if note is not None:
            result["note"] = note
        return result

    async def reject_change(self, task_id: str, note: str | None = None) -> dict[str, Any]:
        self.approval_calls.append({"method": "reject", "task_id": task_id, "note": note})
        if self.raise_on_reject is not None:
            raise self.raise_on_reject
        result = dict(self.reject_result)
        result["task_id"] = task_id
        if note is not None:
            result["note"] = note
        return result

    async def revise_change(self, task_id: str, note: str | None = None) -> dict[str, Any]:
        self.approval_calls.append({"method": "revise", "task_id": task_id, "note": note})
        if self.raise_on_revise is not None:
            raise self.raise_on_revise
        result = dict(self.revise_result)
        result["task_id"] = task_id
        if note is not None:
            result["note"] = note
        return result


def _ctx(session_id="sess-1", actor_id="user-1", metadata=None):
    return ToolExecutionContext(
        session_id=session_id,
        trusted_metadata={"actor_id": actor_id},
        metadata=metadata or {},
    )


def _payload(result):
    return json.loads(result.content)


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_success_public_fields_only():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="call-1", name="create_task",
                          arguments={"goal": "帮我写周报", "title": "写周报"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    body = _payload(res)
    assert body["success"] is True
    task = body["task"]
    assert set(task) == {"id", "title", "status", "goal_mode"}
    assert task["title"] == "写周报"
    rec = svc.created[0]
    assert rec["origin_session_id"] == "sess-1"
    assert rec["body"] == "帮我写周报"
    assert rec["idempotency_key"] == "chat:sess-1:call-1"
    assert rec["created_by"] == "user-1"
    assert res.terminal is False


@pytest.mark.asyncio
async def test_create_task_does_not_read_untrusted_metadata():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="call-1", name="create_task", arguments={"goal": "x"})
    # metadata 伪造 origin/actor，执行器必须忽略
    res = await ex.execute(req, _ctx(session_id="real-sess", actor_id="real-actor",
                                     metadata={"actor_id": "forged", "session_id": "forged"}))
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.created[0]["origin_session_id"] == "real-sess"
    assert svc.created[0]["created_by"] == "real-actor"


@pytest.mark.asyncio
async def test_create_task_title_derived_from_goal_first_line():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="call-2", name="create_task",
                          arguments={"goal": "生成本周工作周报\n保存到 /workspace/weekly.md"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.created[0]["title"] == "生成本周工作周报"


@pytest.mark.asyncio
async def test_create_task_title_truncated_to_80_codepoints():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    long = "字" * 120
    req = ToolCallRequest(id="call-3", name="create_task", arguments={"goal": long})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert len(svc.created[0]["title"]) == 80


@pytest.mark.asyncio
async def test_create_task_title_blank_falls_back_to_goal():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "目标行", "title": "   "})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.created[0]["title"] == "目标行"


@pytest.mark.asyncio
async def test_create_task_title_non_string_invalid():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "x", "title": 123})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "title_invalid"


@pytest.mark.asyncio
async def test_create_task_goal_required():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task", arguments={"goal": "   "})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "goal_required"


@pytest.mark.asyncio
async def test_create_task_goal_non_string_required():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task", arguments={"goal": 42})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "goal_required"


@pytest.mark.asyncio
async def test_create_task_session_missing():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task", arguments={"goal": "x"})
    res = await ex.execute(req, ToolExecutionContext())
    assert res.status is ToolResultStatus.PERMISSION_DENIED
    assert _payload(res)["error"] == "session_missing"
    # session 缺失时不调用服务
    assert ex.service.created == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_create_task_priority_bool_invalid():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "x", "priority": True})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_create_task_priority_negative_invalid():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "x", "priority": -1})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_create_task_goal_mode_non_bool_invalid():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "x", "goal_mode": "yes"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_create_task_skills_dedup_preserves_order():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "x", "skills": ["b", "a", "b"]})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.created[0]["skills"] == ("b", "a")


@pytest.mark.asyncio
async def test_create_task_skills_empty_string_invalid():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "x", "skills": ["b", ""]})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_create_task_goal_mode_true_passed_through():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="create_task",
                          arguments={"goal": "x", "goal_mode": True})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.created[0]["goal_mode"] is True


@pytest.mark.asyncio
async def test_create_task_task_validation_error_mapped():
    svc = FakeUserTaskService()
    svc.raise_on_create = TaskValidationError("bad")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="create_task", arguments={"goal": "x"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_invalid"


@pytest.mark.asyncio
async def test_create_task_conflict_error_mapped():
    svc = FakeUserTaskService()
    svc.raise_on_create = TaskConflictError("dup")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="create_task", arguments={"goal": "x"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_conflict"


@pytest.mark.asyncio
async def test_create_task_unknown_exception_mapped_to_stable_code():
    svc = FakeUserTaskService()
    svc.raise_on_create = RuntimeError("internal db detail: sqlite3.OperationalError")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="create_task", arguments={"goal": "x"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    body = _payload(res)
    assert body["error"] == "task_internal_error"
    # 不泄露内部异常文本
    assert "sqlite3" not in json.dumps(body)


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tasks_filters_by_session_across_pages():
    svc = FakeUserTaskService()
    svc.list_pages = [
        TaskListPage(items=(
            _task(id="t_1", origin_session_id="sess-1"),
            _task(id="t_2", origin_session_id="other"),
        ), next_cursor=TaskListCursor(created_at=datetime.now(timezone.utc), task_id="t_2")),
        TaskListPage(items=(
            _task(id="t_3", origin_session_id="sess-1"),
        ), next_cursor=None),
    ]
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="list_tasks", arguments={})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.SUCCESS
    body = _payload(res)
    assert body["count"] == 2
    assert {i["id"] for i in body["items"]} == {"t_1", "t_3"}
    assert set(body["items"][0]) == {"id", "title", "status", "created_at"}
    assert res.terminal is False


@pytest.mark.asyncio
async def test_list_tasks_excludes_archived():
    svc = FakeUserTaskService()
    svc.list_pages = [
        TaskListPage(items=(
            _task(id="t_1", origin_session_id="sess-1", is_archived=False),
            _task(id="t_2", origin_session_id="sess-1", is_archived=True),
        ), next_cursor=None),
    ]
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="list_tasks", arguments={})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.SUCCESS
    body = _payload(res)
    assert {i["id"] for i in body["items"]} == {"t_1"}


@pytest.mark.asyncio
async def test_list_tasks_status_filter():
    svc = FakeUserTaskService()
    svc.list_pages = [
        TaskListPage(items=(
            _task(id="t_1", origin_session_id="sess-1", status=TaskStatus.QUEUED),
            _task(id="t_2", origin_session_id="sess-1", status=TaskStatus.RUNNING),
        ), next_cursor=None),
    ]
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="list_tasks", arguments={"status": "running"})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.SUCCESS
    body = _payload(res)
    assert {i["id"] for i in body["items"]} == {"t_2"}


@pytest.mark.asyncio
async def test_list_tasks_status_invalid():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="list_tasks", arguments={"status": "bogus"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_status"


@pytest.mark.asyncio
async def test_list_tasks_session_missing():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="list_tasks", arguments={})
    res = await ex.execute(req, ToolExecutionContext())
    assert res.status is ToolResultStatus.PERMISSION_DENIED
    assert _payload(res)["error"] == "session_missing"


@pytest.mark.asyncio
async def test_list_tasks_service_exception_returns_error_no_leak():
    svc = FakeUserTaskService()
    svc.raise_on_list = RuntimeError("db explode: sqlite3.OperationalError: no such table")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="list_tasks", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    body = _payload(res)
    assert body["success"] is False
    assert body["error"] == "task_list_failed"
    assert body["items"] == []
    assert body["count"] == 0
    assert "sqlite3" not in json.dumps(body)


# ---------------------------------------------------------------------------
# approve_task / reject_task / revise_task -- session isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_task_context_none_permission_denied():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, None)
    assert res.status is ToolResultStatus.PERMISSION_DENIED
    assert _payload(res)["error"] == "session_missing"
    assert svc.approval_calls == []
    assert svc.latest_waiting_calls == []
    assert svc.get_task_calls == []


@pytest.mark.asyncio
async def test_approve_task_session_id_blank_permission_denied_no_service_call():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    for sid in (None, "", "   "):
        res = await ex.execute(req, _ctx(session_id=sid))  # type: ignore[arg-type]
        assert res.status is ToolResultStatus.PERMISSION_DENIED
        assert _payload(res)["error"] == "session_missing"
    assert svc.approval_calls == []
    assert svc.latest_waiting_calls == []
    assert svc.get_task_calls == []


@pytest.mark.asyncio
async def test_reject_task_session_missing_no_service_call():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="reject_task", arguments={})
    res = await ex.execute(req, ToolExecutionContext())
    assert res.status is ToolResultStatus.PERMISSION_DENIED
    assert _payload(res)["error"] == "session_missing"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_revise_task_session_missing_no_service_call():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="revise_task", arguments={"note": "x"})
    res = await ex.execute(req, ToolExecutionContext())
    assert res.status is ToolResultStatus.PERMISSION_DENIED
    assert _payload(res)["error"] == "session_missing"
    assert svc.approval_calls == []


# ---------------------------------------------------------------------------
# approve_task / reject_task / revise_task -- task_id default & no_waiting_approval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_task_default_uses_latest_waiting_approval():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_9", title="待批准任务",
                                     origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.latest_waiting_calls == ["sess-1"]
    assert svc.get_task_calls == []
    assert svc.approval_calls[0]["task_id"] == "t_9"


@pytest.mark.asyncio
async def test_approve_task_no_waiting_approval_error():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = None
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "no_waiting_approval"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_revise_task_no_waiting_approval_error():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = None
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="revise_task", arguments={"note": "改一下"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "no_waiting_approval"
    assert svc.approval_calls == []


# ---------------------------------------------------------------------------
# approve_task / reject_task / revise_task -- cross-session / archived / not-found
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_task_cross_session_task_not_found():
    svc = FakeUserTaskService()
    svc.tasks_by_id["t_1"] = _task(id="t_1", origin_session_id="other-session",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={"task_id": "t_1"})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_not_found"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_approve_task_archived_task_not_found():
    svc = FakeUserTaskService()
    svc.tasks_by_id["t_1"] = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL,
                                     is_archived=True)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={"task_id": "t_1"})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_not_found"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_approve_task_nonexistent_task_not_found():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={"task_id": "nope"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_not_found"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_approve_task_not_found_no_existence_leak():
    """跨会话、归档、不存在三类统一 task_not_found，content 不泄露差异。"""
    svc = FakeUserTaskService()
    svc.tasks_by_id["t_cross"] = _task(id="t_cross", origin_session_id="other")
    svc.tasks_by_id["t_arch"] = _task(id="t_arch", origin_session_id="sess-1",
                                       is_archived=True)
    ex = UserTaskToolExecutor(svc)
    bodies = []
    for tid in ("t_cross", "t_arch", "t_missing"):
        req = ToolCallRequest(id="c", name="approve_task", arguments={"task_id": tid})
        res = await ex.execute(req, _ctx(session_id="sess-1"))
        assert res.status is ToolResultStatus.ERROR
        body = _payload(res)
        assert body["error"] == "task_not_found"
        bodies.append(json.dumps(body))
    # 三类响应内容一致（不含 task_id 差异、不含 "archived"/"cross" 等区分词）
    assert bodies[0] == bodies[1] == bodies[2]
    for b in bodies:
        assert "archived" not in b
        assert "cross" not in b
        assert "missing" not in b


# ---------------------------------------------------------------------------
# approve_task / reject_task / revise_task -- argument & note validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_task_unknown_field_invalid_arguments():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task",
                          arguments={"task_id": "t_1", "extra": "x"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_approve_task_task_id_non_string_invalid_arguments():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="approve_task", arguments={"task_id": 123})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_approve_task_task_id_blank_invalid_arguments():
    ex = UserTaskToolExecutor(FakeUserTaskService())
    req = ToolCallRequest(id="c", name="approve_task", arguments={"task_id": "   "})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_approve_task_arguments_none_invalid_arguments():
    """arguments 为 None（schema 绕过）-> invalid_arguments，不访问 service。"""
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments=None)  # type: ignore[arg-type]
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"
    assert svc.approval_calls == []
    assert svc.latest_waiting_calls == []
    assert svc.get_task_calls == []


@pytest.mark.asyncio
async def test_approve_task_note_non_string_invalid_arguments():
    svc = FakeUserTaskService()
    svc.tasks_by_id["t_1"] = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task",
                          arguments={"task_id": "t_1", "note": 42})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "invalid_arguments"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_approve_task_note_too_long():
    svc = FakeUserTaskService()
    svc.tasks_by_id["t_1"] = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    long_note = "字" * 2001
    req = ToolCallRequest(id="c", name="approve_task",
                          arguments={"task_id": "t_1", "note": long_note})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "note_too_long"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_revise_task_note_required():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    # 缺省 note
    req = ToolCallRequest(id="c", name="revise_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "note_required"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_revise_task_note_blank_required():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="revise_task",
                          arguments={"note": "   "})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "note_required"
    assert svc.approval_calls == []


@pytest.mark.asyncio
async def test_revise_task_note_null_required():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="revise_task",
                          arguments={"note": None})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "note_required"


# ---------------------------------------------------------------------------
# approve_task / reject_task / revise_task -- exception mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_task_service_not_found_error_mapped():
    """service.approve_change 抛 TaskNotFoundError -> task_not_found。"""
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    svc.raise_on_approve = TaskNotFoundError("vanished between read and approve")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_not_found"
    assert "vanished" not in json.dumps(_payload(res))


@pytest.mark.asyncio
async def test_approve_task_state_error_mapped():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    svc.raise_on_approve = TaskStateError("not waiting_approval")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_state_invalid"


@pytest.mark.asyncio
async def test_approve_task_conflict_error_mapped():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    svc.raise_on_approve = TaskConflictError("version mismatch")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_conflict"


@pytest.mark.asyncio
async def test_revise_task_validation_error_mapped():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    svc.raise_on_revise = TaskValidationError("bad note")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="revise_task", arguments={"note": "ok"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert _payload(res)["error"] == "task_invalid"


@pytest.mark.asyncio
async def test_reject_task_unknown_exception_mapped_no_leak():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    svc.raise_on_reject = RuntimeError("internal db detail: sqlite3.OperationalError")
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="reject_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    body = _payload(res)
    assert body["error"] == "task_internal_error"
    assert "sqlite3" not in json.dumps(body)
    assert "internal db detail" not in json.dumps(body)


# ---------------------------------------------------------------------------
# approve_task / reject_task / revise_task -- success whitelist & terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_task_success_whitelist():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_7", title="周报任务",
                                     origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.SUCCESS
    body = _payload(res)
    assert body["success"] is True
    task = body["task"]
    assert set(task) == {"id", "title", "status", "decision"}
    assert task["id"] == "t_7"
    assert task["title"] == "周报任务"
    assert task["status"] == "queued"
    assert task["decision"] == "approved"
    assert res.terminal is False


@pytest.mark.asyncio
async def test_reject_task_success_whitelist():
    svc = FakeUserTaskService()
    svc.tasks_by_id["t_3"] = _task(id="t_3", title="拒绝任务",
                                    origin_session_id="sess-1",
                                    status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="reject_task", arguments={"task_id": "t_3"})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.SUCCESS
    body = _payload(res)
    assert body["success"] is True
    task = body["task"]
    assert set(task) == {"id", "title", "status", "decision"}
    assert task["id"] == "t_3"
    assert task["title"] == "拒绝任务"
    assert task["status"] == "queued"
    assert task["decision"] == "rejected"
    assert res.terminal is False


@pytest.mark.asyncio
async def test_revise_task_success_whitelist():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_5", title="修订任务",
                                     origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="revise_task", arguments={"note": "调整范围"})
    res = await ex.execute(req, _ctx(session_id="sess-1"))
    assert res.status is ToolResultStatus.SUCCESS
    body = _payload(res)
    assert body["success"] is True
    task = body["task"]
    assert set(task) == {"id", "title", "status", "decision"}
    assert task["id"] == "t_5"
    assert task["title"] == "修订任务"
    assert task["status"] == "queued"
    assert task["decision"] == "revised"
    assert res.terminal is False


@pytest.mark.asyncio
async def test_approve_task_status_from_service_not_hardcoded():
    """status 取 service 返回值，不硬编码 queued。"""
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    svc.approve_result = {
        "task_id": "t_1", "decision": "approved",
        "proposal_event_id": "e_1", "note": None,
        "status": "running",  # 非典型值，验证不硬编码
    }
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert _payload(res)["task"]["status"] == "running"


@pytest.mark.asyncio
async def test_approve_task_does_not_read_untrusted_metadata():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx(session_id="real-sess", actor_id="real-actor",
                                     metadata={"session_id": "forged",
                                               "actor_id": "forged"}))
    assert res.status is ToolResultStatus.SUCCESS
    # 定位用 real-sess，不是 forged
    assert svc.latest_waiting_calls == ["real-sess"]


# ---------------------------------------------------------------------------
# approve_task / reject_task / revise_task -- note trimming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_task_note_trimmed_passed_to_service():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task",
                          arguments={"note": "  好的  "})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.approval_calls[0]["note"] == "好的"


@pytest.mark.asyncio
async def test_reject_task_note_trimmed_passed_to_service():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="reject_task",
                          arguments={"note": "\t不行\n"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.approval_calls[0]["note"] == "不行"


@pytest.mark.asyncio
async def test_revise_task_note_trimmed_passed_to_service():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="revise_task",
                          arguments={"note": "  改一下方案  "})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.approval_calls[0]["note"] == "改一下方案"


@pytest.mark.asyncio
async def test_approve_task_note_absent_passes_none_to_service():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task", arguments={})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.approval_calls[0]["note"] is None


@pytest.mark.asyncio
async def test_approve_task_note_blank_passes_none_to_service():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="approve_task",
                          arguments={"note": "   "})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.approval_calls[0]["note"] is None


@pytest.mark.asyncio
async def test_approve_task_note_exactly_2000_chars_ok():
    svc = FakeUserTaskService()
    svc.latest_waiting_task = _task(id="t_1", origin_session_id="sess-1",
                                     status=TaskStatus.WAITING_APPROVAL)
    ex = UserTaskToolExecutor(svc)
    note = "字" * 2000
    req = ToolCallRequest(id="c", name="approve_task",
                          arguments={"note": note})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.SUCCESS
    assert svc.approval_calls[0]["note"] == note


# ---------------------------------------------------------------------------
# unknown tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_name_returns_error_without_service_call():
    svc = FakeUserTaskService()
    ex = UserTaskToolExecutor(svc)
    req = ToolCallRequest(id="c", name="create_task_v2", arguments={"goal": "x"})
    res = await ex.execute(req, _ctx())
    assert res.status is ToolResultStatus.ERROR
    assert svc.created == []
    assert svc.list_calls == 0
