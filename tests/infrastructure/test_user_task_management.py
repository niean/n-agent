"""用户侧任务工具执行器测试（UserTaskToolExecutor）。

spec: spec-260720-chat-natural-language-task.md
覆盖 create_task / list_tasks 的会话绑定、参数校验、错误映射、公开字段白名单、
幂等键、分页过滤与未知工具名。
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
