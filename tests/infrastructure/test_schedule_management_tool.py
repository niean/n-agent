from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from app.application.schedule_service import (
    ScheduledTaskCreateInput,
    ScheduledTaskNotFoundError,
    ScheduledTaskUpdateInput,
)
from app.domain.schedule import (
    DeliveryTarget,
    ScheduledTask,
    ScheduledTaskStatus,
    ScheduleExpression,
    ScheduleTimezone,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
)
from app.infrastructure.tools.schedule_management import ScheduleManagementToolExecutor


def _task(
    task_id: str,
    *,
    origin: dict[str, Any] | None = None,
    session_id: str = "s1",
    cron: str = "0 9 * * *",
    prompt: str = "提醒我",
    name: str = "demo",
    status: ScheduledTaskStatus = ScheduledTaskStatus.ACTIVE,
) -> ScheduledTask:
    now = datetime.now(timezone.utc)
    default_origin = {
        "platform": "feishu",
        "receive_id": "oc_a",
        "receive_id_type": "chat_id",
        "thread_id": "",
    }
    origin = dict(origin or default_origin)
    return ScheduledTask(
        id=task_id,
        name=name,
        prompt=prompt,
        schedule=ScheduleExpression(cron),
        timezone=ScheduleTimezone("Asia/Shanghai"),
        session_id=session_id,
        delivery_target=DeliveryTarget.origin(origin),
        next_run_at=now,
        enabled=True,
        status=status,
        origin=dict(origin),
        created_at=now,
        updated_at=now,
    )


class FakeScheduleService:
    def __init__(self, tasks: list[ScheduledTask] | None = None):
        self.tasks: list[ScheduledTask] = list(tasks or [])
        self.created: ScheduledTaskCreateInput | None = None
        self.updated: tuple[str, ScheduledTaskUpdateInput] | None = None
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.run: list[str] = []
        self.deleted: list[str] = []

    async def create(self, request: ScheduledTaskCreateInput) -> ScheduledTask:
        self.created = request
        task = _task(
            task_id="sched-new",
            origin=dict(request.origin),
            session_id=request.session_id or "s-auto",
            cron=request.cron_expression,
            prompt=request.prompt,
            name=request.name,
        )
        self.tasks.append(task)
        return task

    async def list(self) -> list[ScheduledTask]:
        return list(self.tasks)

    async def get(self, task_id: str) -> ScheduledTask:
        for t in self.tasks:
            if t.id == task_id:
                return t
        raise ScheduledTaskNotFoundError(task_id)

    async def update(self, task_id: str, request: ScheduledTaskUpdateInput) -> ScheduledTask:
        self.updated = (task_id, request)
        task = await self.get(task_id)
        new = replace(
            task,
            name=request.name or task.name,
            prompt=request.prompt or task.prompt,
            schedule=ScheduleExpression(request.cron_expression or task.schedule.value),
            timezone=ScheduleTimezone(request.timezone or task.timezone.value),
        )
        self.tasks = [new if t.id == task_id else t for t in self.tasks]
        return new

    async def pause(self, task_id: str) -> ScheduledTask:
        self.paused.append(task_id)
        task = await self.get(task_id)
        new = replace(task, status=ScheduledTaskStatus.PAUSED, enabled=False)
        self.tasks = [new if t.id == task_id else t for t in self.tasks]
        return new

    async def resume(self, task_id: str) -> ScheduledTask:
        self.resumed.append(task_id)
        task = await self.get(task_id)
        new = replace(task, status=ScheduledTaskStatus.ACTIVE, enabled=True)
        self.tasks = [new if t.id == task_id else t for t in self.tasks]
        return new

    async def run_now(self, task_id: str) -> Any:
        self.run.append(task_id)
        return {"status": "queued"}

    async def delete(self, task_id: str) -> bool:
        self.deleted.append(task_id)
        self.tasks = [t for t in self.tasks if t.id != task_id]
        return True


def _trusted_ctx(
    *,
    receive_id: str = "oc_a",
    receive_id_type: str = "chat_id",
    thread_id: str = "",
    session_id: str = "s1",
    mode: str = "realtime",
    permitted: set[str] | None = None,
    platform: str = "feishu",
) -> ToolExecutionContext:
    metadata: dict[str, Any] = {
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
        "thread_id": thread_id,
    }
    if platform:
        metadata["gateway.platform"] = platform
    return ToolExecutionContext(
        session_id=session_id,
        trusted_metadata=metadata,
        execution_context_mode=mode,
        permitted_managed_tools=permitted or {"manage_schedule"},
    )


def _payload(result):
    return json.loads(result.content) if isinstance(result.content, str) else result.content


@pytest.mark.asyncio
async def test_create_uses_trusted_origin_and_session():
    fake = FakeScheduleService()
    executor = ScheduleManagementToolExecutor(fake)
    ctx = _trusted_ctx(receive_id="oc_a")
    request = ToolCallRequest(
        id="1",
        name="manage_schedule",
        arguments={
            "action": "create",
            "name": "daily report",
            "prompt": "提醒我看日报",
            "cron_expression": "0 9 * * *",
        },
    )
    result = await executor.execute(request, ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    assert payload["success"] is True
    assert fake.created is not None
    assert fake.created.session_id == "s1"
    assert fake.created.delivery_target == "origin"
    assert fake.created.origin == {
        "platform": "feishu",
        "receive_id": "oc_a",
        "receive_id_type": "chat_id",
        "thread_id": "",
    }


@pytest.mark.asyncio
async def test_fail_closed_without_trusted_metadata():
    fake = FakeScheduleService()
    executor = ScheduleManagementToolExecutor(fake)
    ctx = ToolExecutionContext(session_id="s2", execution_context_mode="realtime")
    result = await executor.execute(
        ToolCallRequest(
            id="1",
            name="manage_schedule",
            arguments={"action": "create", "prompt": "x", "cron_expression": "0 9 * * *"},
        ),
        ctx,
    )
    assert result.status is ToolResultStatus.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_fail_closed_when_mode_not_realtime():
    fake = FakeScheduleService()
    executor = ScheduleManagementToolExecutor(fake)
    ctx = ToolExecutionContext(
        session_id="s3",
        trusted_metadata={"gateway.platform": "feishu", "receive_id": "x", "receive_id_type": "chat_id"},
        execution_context_mode="unattended",
    )
    result = await executor.execute(
        ToolCallRequest(
            id="1",
            name="manage_schedule",
            arguments={"action": "create", "prompt": "x", "cron_expression": "0 9 * * *"},
        ),
        ctx,
    )
    assert result.status is ToolResultStatus.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_fail_closed_when_trusted_origin_missing_platform():
    fake = FakeScheduleService()
    executor = ScheduleManagementToolExecutor(fake)
    ctx = ToolExecutionContext(
        session_id="s4",
        trusted_metadata={"receive_id": "oc_a", "receive_id_type": "chat_id"},
        execution_context_mode="realtime",
        permitted_managed_tools={"manage_schedule"},
    )
    result = await executor.execute(
        ToolCallRequest(
            id="1",
            name="manage_schedule",
            arguments={"action": "create", "prompt": "x", "cron_expression": "0 9 * * *"},
        ),
        ctx,
    )
    payload = _payload(result)
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert payload["error"] == "trusted_origin_missing_platform"


@pytest.mark.asyncio
async def test_list_only_returns_origin_matching_tasks():
    fake = FakeScheduleService(
        tasks=[
            _task("sched-a", origin={"platform": "feishu", "receive_id": "oc_a", "receive_id_type": "chat_id", "thread_id": ""}),
            _task("sched-b", origin={"platform": "feishu", "receive_id": "oc_b", "receive_id_type": "chat_id", "thread_id": ""}),
        ]
    )
    executor = ScheduleManagementToolExecutor(fake)
    ctx = _trusted_ctx(receive_id="oc_a")
    result = await executor.execute(
        ToolCallRequest(id="1", name="schedule_query", arguments={"action": "list"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    payload = _payload(result)
    ids = [task["id"] for task in payload["tasks"]]
    assert ids == ["sched-a"]


@pytest.mark.asyncio
async def test_query_get_cross_origin_returns_not_found():
    fake = FakeScheduleService(
        tasks=[_task("sched-b", origin={"platform": "feishu", "receive_id": "oc_b", "receive_id_type": "chat_id", "thread_id": ""})]
    )
    executor = ScheduleManagementToolExecutor(fake)
    ctx = _trusted_ctx(receive_id="oc_a")
    result = await executor.execute(
        ToolCallRequest(id="1", name="schedule_query", arguments={"action": "get", "task_id": "sched-b"}),
        ctx,
    )
    payload = _payload(result)
    assert payload["success"] is False
    assert payload["error"] == "task not found"


@pytest.mark.asyncio
async def test_update_pause_resume_run_check_origin_ownership():
    fake = FakeScheduleService(
        tasks=[_task("sched-b", origin={"platform": "feishu", "receive_id": "oc_b", "receive_id_type": "chat_id", "thread_id": ""})]
    )
    executor = ScheduleManagementToolExecutor(fake)
    ctx = _trusted_ctx(receive_id="oc_a")
    for action in ("update", "pause", "resume", "run"):
        result = await executor.execute(
            ToolCallRequest(
                id="1",
                name="manage_schedule",
                arguments={"action": action, "task_id": "sched-b", "cron_expression": "0 10 * * *"},
            ),
            ctx,
        )
        payload = _payload(result)
        assert payload["success"] is False, action
        assert payload["error"] == "task not found", action

    assert fake.updated is None
    assert fake.paused == []
    assert fake.resumed == []
    assert fake.run == []


@pytest.mark.asyncio
async def test_remove_short_circuits_to_confirmation_required():
    fake = FakeScheduleService(
        tasks=[_task("sched-1", origin={"platform": "feishu", "receive_id": "oc_a", "receive_id_type": "chat_id", "thread_id": ""})]
    )
    executor = ScheduleManagementToolExecutor(fake)
    ctx = _trusted_ctx(receive_id="oc_a")
    result = await executor.execute(
        ToolCallRequest(id="1", name="manage_schedule", arguments={"action": "remove", "task_id": "sched-1"}),
        ctx,
    )
    payload = _payload(result)
    assert payload["confirmation_required"] is True
    assert "/schedule remove sched-1" in payload["instruction"]
    assert fake.deleted == []


@pytest.mark.asyncio
async def test_remove_cross_origin_returns_not_found_without_existence_leak():
    fake = FakeScheduleService(
        tasks=[_task("sched-b", origin={"platform": "feishu", "receive_id": "oc_b", "receive_id_type": "chat_id", "thread_id": ""})]
    )
    executor = ScheduleManagementToolExecutor(fake)
    ctx = _trusted_ctx(receive_id="oc_a")
    result = await executor.execute(
        ToolCallRequest(id="1", name="manage_schedule", arguments={"action": "remove", "task_id": "sched-b"}),
        ctx,
    )
    payload = _payload(result)
    assert payload["success"] is False
    assert payload["error"] == "task not found"
    assert fake.deleted == []
