from datetime import datetime, timezone

import pytest

from app.application.schedule_service import (
    ScheduleDeliveryContextError,
    ScheduleService,
    ScheduledTaskCreateInput,
    ScheduledTaskNotFoundError,
    ScheduledTaskUpdateInput,
    ScheduleValidationError,
)
from app.domain.schedule import DeliveryTarget, DeliveryTargetType, PromptSafetyResult, ScheduledTaskStatus


class FakeRegistry:
    def __init__(self):
        self.tasks = {}
        self.executions = []
        self.missing_session = None

    async def create(self, task):
        self.tasks[task.id] = task
        return task

    async def list(self):
        return list(self.tasks.values())

    async def get(self, task_id):
        return self.tasks.get(task_id)

    async def update(self, task):
        self.tasks[task.id] = task
        return task

    async def update_status(self, task_id, status, enabled):
        task = self.tasks[task_id]
        updated = type(task)(**{**task.__dict__, "status": status, "enabled": enabled})
        self.tasks[task_id] = updated
        return updated

    async def delete(self, task_id):
        return self.tasks.pop(task_id, None) is not None

    async def list_executions(self, task_id, limit):
        return self.executions[:limit]

    async def list_recoverable_origin_tasks(self):
        return [
            task
            for task in self.tasks.values()
            if task.status is ScheduledTaskStatus.SESSION_MISSING
            and task.delivery_target.target_type is DeliveryTargetType.ORIGIN
        ]

    async def mark_session_missing(self, session_id):
        self.missing_session = session_id
        return 1


class FakeCalculator:
    def __init__(self):
        self.validated = False

    def validate(self, expression, timezone):
        self.validated = True
        if expression.value == "bad":
            raise ValueError("bad cron")

    def next_after(self, expression, base_time, timezone_value):
        return datetime(2026, 6, 16, 1, 0, tzinfo=timezone.utc)


class FakeScanner:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.prompts = []

    def scan(self, prompt):
        self.prompts.append(prompt)
        return PromptSafetyResult(self.allowed, "blocked" if not self.allowed else "")


class FakeSessionService:
    def __init__(self):
        self.created = []

    async def create_session(self, session_id, source="dashboard"):
        self.created.append((session_id, source))


class FakeRunService:
    def __init__(self):
        self.task_id = None

    async def run_now(self, task_id):
        self.task_id = task_id
        return {"task_id": task_id, "status": "ok"}


def _service(scanner=None):
    registry = FakeRegistry()
    runner = FakeRunService()
    service = ScheduleService(registry, FakeCalculator(), scanner or FakeScanner(), FakeSessionService(), runner.run_now)
    return service, registry, runner


@pytest.mark.asyncio
async def test_schedule_service_create_validates_scans_and_computes_next_run():
    service, registry, _ = _service()

    task = await service.create(
        ScheduledTaskCreateInput(
            name="Daily",
            prompt="summarize",
            cron_expression="0 9 * * *",
            timezone="Asia/Shanghai",
            delivery_target="dashboard",
        )
    )

    assert task.id in registry.tasks
    assert task.delivery_target.target_type is DeliveryTargetType.DASHBOARD
    assert task.next_run_at.tzinfo is timezone.utc


@pytest.mark.asyncio
async def test_schedule_service_rejects_blocked_prompt():
    service, _, _ = _service(FakeScanner(False))

    with pytest.raises(ScheduleValidationError):
        await service.create(ScheduledTaskCreateInput(name="x", prompt="bad", cron_expression="* * * * *"))


@pytest.mark.asyncio
async def test_schedule_service_rejects_origin_without_delivery_context():
    service, _, _ = _service()

    with pytest.raises(ScheduleDeliveryContextError):
        await service.create(
            ScheduledTaskCreateInput(name="x", prompt="ok", cron_expression="* * * * *", delivery_target="origin")
        )


@pytest.mark.asyncio
async def test_schedule_service_lifecycle_and_run_now_delegate():
    service, _, runner = _service()
    task = await service.create(ScheduledTaskCreateInput(name="x", prompt="ok", cron_expression="* * * * *"))

    paused = await service.pause(task.id)
    resumed = await service.resume(task.id)
    result = await service.run_now(task.id)

    assert paused.status is ScheduledTaskStatus.PAUSED
    assert resumed.status is ScheduledTaskStatus.ACTIVE
    assert runner.task_id == task.id
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_schedule_service_handle_session_deleted_marks_missing():
    service, registry, _ = _service()

    await service.handle_session_deleted("session-1")

    assert registry.missing_session == "session-1"


@pytest.mark.asyncio
async def test_schedule_service_list_executions_validates_task_and_limit():
    service, registry, _ = _service()
    task = await service.create(ScheduledTaskCreateInput(name="x", prompt="ok", cron_expression="* * * * *"))
    registry.executions = [object() for _ in range(12)]

    executions = await service.list_executions(task.id)

    assert len(executions) == 10
    with pytest.raises(ScheduleValidationError):
        await service.list_executions(task.id, 0)
    with pytest.raises(ScheduleValidationError):
        await service.list_executions(task.id, 51)
    with pytest.raises(ScheduledTaskNotFoundError):
        await service.list_executions("missing", 10)


@pytest.mark.asyncio
async def test_schedule_service_origin_task_uses_independent_schedule_session():
    service, _, _ = _service()

    task = await service.create(
        ScheduledTaskCreateInput(
            name="origin",
            prompt="ok",
            cron_expression="* * * * *",
            delivery_target="origin",
            origin={"receive_id": "chat-1", "receive_id_type": "chat_id"},
            session_id="gateway-session-1",
        )
    )

    assert task.session_id.startswith("schedule-")
    assert service.session_service.created == [(task.session_id, "schedule")]


@pytest.mark.asyncio
async def test_schedule_service_recovers_missing_origin_sessions():
    service, registry, _ = _service()
    task = await service.create(
        ScheduledTaskCreateInput(
            name="origin",
            prompt="ok",
            cron_expression="* * * * *",
            delivery_target="origin",
            origin={"platform": "feishu", "target": "home"},
        )
    )
    missing = type(task)(
        **{
            **task.__dict__,
            "session_id": "deleted-session",
            "enabled": False,
            "status": ScheduledTaskStatus.SESSION_MISSING,
        }
    )
    registry.tasks[task.id] = missing

    recovered = await service.recover_missing_origin_sessions()

    updated = registry.tasks[task.id]
    assert recovered == 1
    assert updated.status is ScheduledTaskStatus.ACTIVE
    assert updated.enabled is True
    assert updated.session_id.startswith("schedule-")
    assert service.session_service.created[-1] == (updated.session_id, "schedule")


@pytest.mark.asyncio
async def test_schedule_service_update_supports_session_and_preserves_origin_context():
    service, _, _ = _service()
    dashboard = await service.create(ScheduledTaskCreateInput(name="x", prompt="ok", cron_expression="* * * * *"))
    origin = await service.create(
        ScheduledTaskCreateInput(
            name="origin",
            prompt="ok",
            cron_expression="* * * * *",
            delivery_target="origin",
            origin={"receive_id": "chat-1", "receive_id_type": "chat_id"},
        )
    )

    updated_dashboard = await service.update(
        dashboard.id,
        ScheduledTaskUpdateInput(session_id="session-2", delivery_target="silent"),
    )
    updated_origin = await service.update(
        origin.id,
        ScheduledTaskUpdateInput(name="origin renamed", prompt="new prompt"),
    )

    assert updated_dashboard.session_id == "session-2"
    assert updated_dashboard.delivery_target.target_type.value == "silent"
    assert updated_origin.name == "origin renamed"
    assert updated_origin.prompt == "new prompt"
    assert updated_origin.delivery_target.target_type.value == "origin"
    assert updated_origin.origin == {"receive_id": "chat-1", "receive_id_type": "chat_id"}


@pytest.mark.asyncio
async def test_schedule_service_create_persists_allowed_tools_on_execution_policy():
    service, _, _ = _service()

    task = await service.create(
        ScheduledTaskCreateInput(
            name="photo",
            prompt="拍照上传",
            cron_expression="0 10,18 * * *",
            delivery_target="origin",
            origin={"receive_id": "oc_1", "receive_id_type": "chat_id"},
            allowed_tools=("host_terminal",),
        )
    )

    assert task.execution_policy.allowed_tools == ("host_terminal",)


@pytest.mark.asyncio
async def test_schedule_service_update_sets_and_preserves_allowed_tools():
    service, _, _ = _service()
    task = await service.create(
        ScheduledTaskCreateInput(
            name="photo",
            prompt="ok",
            cron_expression="* * * * *",
            allowed_tools=("host_terminal",),
        )
    )

    without_grant_change = await service.update(task.id, ScheduledTaskUpdateInput(name="renamed"))
    assert without_grant_change.execution_policy.allowed_tools == ("host_terminal",)

    cleared = await service.update(task.id, ScheduledTaskUpdateInput(allowed_tools=()))
    assert cleared.execution_policy.allowed_tools == ()

    reset = await service.update(task.id, ScheduledTaskUpdateInput(allowed_tools=("host_terminal", "other")))
    assert reset.execution_policy.allowed_tools == ("host_terminal", "other")
