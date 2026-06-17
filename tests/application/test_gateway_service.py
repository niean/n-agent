from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from app.application.gateway_service import GatewayService
from app.application.chat_service import ChatCompletionResult
from app.domain.gateway import GatewayConfirmationChoice, GatewaySessionKey, GatewaySessionLink, InteractionMessage, InteractionSourceType
from app.domain.provider import ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import ToolDefinition


class FakeGatewayRegistry:
    def __init__(self):
        self.active: dict[tuple[str, str, str], str] = {}
        self.links: dict[tuple[str, str, str], list[GatewaySessionLink]] = {}
        self.processed: set[tuple[str, str]] = set()

    async def get_active_session(self, key):
        session_id = self.active.get(key.conversation_parts)
        if session_id is None:
            return None
        return GatewaySessionLink("conversation-1", session_id, key.display_name)

    async def create_session_link(self, key, session_id):
        link = GatewaySessionLink("conversation-1", session_id, key.display_name)
        self.links.setdefault(key.conversation_parts, []).append(link)
        self.active[key.conversation_parts] = session_id
        return link

    async def set_active_session(self, key, session_id):
        self.active[key.conversation_parts] = session_id
        return GatewaySessionLink("conversation-1", session_id, key.display_name)

    async def list_session_links(self, key):
        return self.links.get(key.conversation_parts, [])

    async def delete_session_link(self, session_id):
        for key, links in self.links.items():
            self.links[key] = [link for link in links if link.session_id != session_id]
            if self.active.get(key) == session_id:
                self.active.pop(key)

    async def mark_event_processed(self, source_type, event_id, message_id=""):
        marker = (source_type.value, event_id)
        if marker in self.processed:
            return False
        self.processed.add(marker)
        return True


class FakeSessionService:
    def __init__(self):
        self.created: list[ConversationSession] = []
        self.renamed = []
        self.deleted = []

    async def create_session(self, session_id: str, source: str = "dashboard"):
        session = ConversationSession(id=session_id, source=source)
        self.created.append(session)
        return session

    async def rename_session(self, session_id: str, title: str):
        self.renamed.append((session_id, title))
        return ConversationSession(id=session_id, title=title)

    async def delete_session(self, session_id: str):
        self.deleted.append(session_id)


class FakeChatService:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return ChatCompletionResult(
            session_id=request.session_id or "missing",
            model=request.model,
            message={"role": "assistant", "content": "pong"},
        )


class FakeToolService:
    def list_definitions(self):
        return [ToolDefinition("calculator", "Calculate", {"type": "object"})]


class FakeModelService:
    @property
    def default_model(self):
        return "model-a"

    async def list_models(self):
        return [ModelInfo("model-a", "Model A", "test", True, True)]


class FakeScheduleService:
    def __init__(self, tasks: dict | None = None):
        self.created = []
        self.run_ids = []
        self.deleted_ids = []
        self.tasks = dict(tasks or {})

    async def create(self, request):
        self.created.append(request)
        from app.domain.schedule import DeliveryTarget, ScheduledTask, ScheduleExpression, ScheduleTimezone
        from datetime import datetime, timezone

        return ScheduledTask(
            id="sched-1",
            name=request.name,
            prompt=request.prompt,
            schedule=ScheduleExpression(request.cron_expression),
            timezone=ScheduleTimezone(request.timezone),
            session_id="session-1",
            delivery_target=DeliveryTarget.dashboard(),
            next_run_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
        )

    async def list(self):
        return []

    async def get(self, task_id):
        from app.application.schedule_service import ScheduledTaskNotFoundError

        task = self.tasks.get(task_id)
        if task is None:
            raise ScheduledTaskNotFoundError(task_id)
        return task

    async def pause(self, task_id):
        return None

    async def resume(self, task_id):
        return None

    async def run_now(self, task_id):
        self.run_ids.append(task_id)
        return {"status": "ok"}

    async def delete(self, task_id):
        self.deleted_ids.append(task_id)
        return True


def _schedule_task(task_id: str, *, receive_id: str, receive_id_type: str = "chat_id", thread_id: str = ""):
    from datetime import datetime, timezone

    from app.domain.schedule import (
        DeliveryTarget,
        ScheduledTask,
        ScheduleExpression,
        ScheduleTimezone,
    )

    origin = {"receive_id": receive_id, "receive_id_type": receive_id_type, "thread_id": thread_id}
    return ScheduledTask(
        id=task_id,
        name="demo",
        prompt="x",
        schedule=ScheduleExpression("0 9 * * *"),
        timezone=ScheduleTimezone("Asia/Shanghai"),
        session_id="session-1",
        delivery_target=DeliveryTarget.origin(origin),
        next_run_at=datetime(2026, 6, 16, tzinfo=timezone.utc),
        origin=origin,
    )


@dataclass
class Harness:
    registry: FakeGatewayRegistry = field(default_factory=FakeGatewayRegistry)
    session_service: FakeSessionService = field(default_factory=FakeSessionService)
    chat_service: FakeChatService = field(default_factory=FakeChatService)

    def service(self, schedule_service=None):
        return GatewayService(
            self.registry,
            self.chat_service,
            self.session_service,
            FakeToolService(),
            FakeModelService(),
            lambda: {"provider": {"status": "ok"}, "gateway": {"status": "ok"}},
            schedule_service=schedule_service,
        )


def message(text="hello", event_id="event-1"):
    return InteractionMessage(
        id=event_id,
        session_key=GatewaySessionKey(InteractionSourceType.CLI, "local", display_name="Local"),
        text=text,
    )


def test_confirmation_choice_values_are_stable():
    assert GatewayConfirmationChoice.ONCE.value == "once"
    assert GatewayConfirmationChoice.TRUST_SESSION.value == "trust_session"
    assert GatewayConfirmationChoice.CANCEL.value == "cancel"


@pytest.mark.asyncio
async def test_gateway_service_creates_source_session_and_runs_chat():
    harness = Harness()
    service = harness.service()

    response = await service.handle_message(message())

    assert response.messages[0].content == "pong"
    assert harness.session_service.created[0].source == "cli"
    assert harness.chat_service.requests[0].session_id == response.session_id


@pytest.mark.asyncio
async def test_gateway_service_skips_duplicate_event():
    harness = Harness()
    service = harness.service()

    await service.handle_message(message())
    response = await service.handle_message(message())

    assert response.metadata["duplicate"] is True
    assert len(harness.chat_service.requests) == 1


@pytest.mark.asyncio
async def test_gateway_service_new_command_creates_new_active_session():
    harness = Harness()
    service = harness.service()

    response = await service.handle_message(message("/new"))

    assert response.messages[0].content.startswith("已创建新会话")
    assert harness.session_service.created[0].source == "cli"


@pytest.mark.asyncio
async def test_gateway_new_requires_confirmation_before_create():
    harness = Harness()
    service = harness.service()
    event = message("/new")
    event.metadata["actor_id"] = "ou_1"

    response = await service.handle_message(event)

    assert response.messages[0].metadata["confirmation"]["action"] == "new"
    assert harness.session_service.created == []


@pytest.mark.asyncio
async def test_gateway_confirmation_once_executes_pending_new():
    harness = Harness()
    service = harness.service()
    event = message("/new")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    response = await service.handle_confirmation(
        event.session_key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert response.messages[0].content.startswith("已创建新会话")
    assert len(harness.session_service.created) == 1


@pytest.mark.asyncio
async def test_gateway_confirmation_cancel_does_not_execute_pending_new():
    harness = Harness()
    service = harness.service()
    event = message("/new")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    response = await service.handle_confirmation(
        event.session_key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.CANCEL,
    )

    assert "已取消" in response.messages[0].content
    assert harness.session_service.created == []


@pytest.mark.asyncio
async def test_gateway_confirmation_trust_session_skips_next_destructive_command():
    harness = Harness()
    service = harness.service()
    first = message("/new")
    first.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(first)

    await service.handle_confirmation(
        first.session_key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.TRUST_SESSION,
    )
    second = message("/new", "event-new-2")
    second.metadata["actor_id"] = "ou_1"
    response = await service.handle_message(second)

    assert "confirmation" not in response.messages[0].metadata
    assert response.messages[0].content.startswith("已创建新会话")


@pytest.mark.asyncio
async def test_gateway_confirmation_rejects_different_actor():
    harness = Harness()
    service = harness.service()
    event = message("/new")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    response = await service.handle_confirmation(
        event.session_key,
        "ou_2",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert "只有命令发起者可以确认" in response.messages[0].content
    assert harness.session_service.created == []


@pytest.mark.asyncio
async def test_gateway_confirmation_expires_after_ttl():
    harness = Harness()
    service = harness.service()
    service.command_service.confirmation_ttl = timedelta(seconds=0)
    event = message("/new")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    response = await service.handle_confirmation(
        event.session_key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert "确认已失效" in response.messages[0].content
    assert harness.session_service.created == []


@pytest.mark.asyncio
async def test_gateway_rename_confirmation_executes_against_captured_session():
    harness = Harness()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")
    service = harness.service()
    event = message("/rename New Title")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    await service.handle_confirmation(
        key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert harness.session_service.renamed == [("session-1", "New Title")]


@pytest.mark.asyncio
async def test_gateway_delete_confirmation_deletes_link_session_and_creates_replacement_session():
    harness = Harness()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")
    service = harness.service()
    event = message("/delete")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    response = await service.handle_confirmation(
        key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert harness.session_service.deleted == ["session-1"]
    assert response.session_id != "session-1"
    assert "session-1" not in [link.session_id for links in harness.registry.links.values() for link in links]
    assert harness.registry.active[key.conversation_parts] == response.session_id


@pytest.mark.asyncio
async def test_gateway_schedule_remove_requires_confirmation():
    harness = Harness()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")
    schedule = FakeScheduleService(
        tasks={"sched-1": _schedule_task("sched-1", receive_id=key.source_id, thread_id=key.thread_id)}
    )
    service = harness.service(schedule)
    event = message("/schedule remove sched-1")
    event.metadata["actor_id"] = "ou_1"

    pending = await service.handle_message(event)

    assert pending.messages[0].metadata["confirmation"]["action"] == "schedule_remove"
    assert schedule.deleted_ids == []


@pytest.mark.asyncio
async def test_gateway_schedule_remove_confirmation_deletes_task():
    harness = Harness()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")
    schedule = FakeScheduleService(
        tasks={"sched-1": _schedule_task("sched-1", receive_id=key.source_id, thread_id=key.thread_id)}
    )
    service = harness.service(schedule)
    event = message("/schedule remove sched-1")
    event.metadata["actor_id"] = "ou_1"
    event.metadata["receive_id"] = key.source_id
    event.metadata["receive_id_type"] = "chat_id"
    event.metadata["thread_id"] = key.thread_id
    pending = await service.handle_message(event)

    await service.handle_confirmation(
        key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert schedule.deleted_ids == ["sched-1"]


@pytest.mark.asyncio
async def test_destructive_commands_without_active_session_do_not_create_session():
    harness = Harness()
    service = harness.service(FakeScheduleService())
    event = message("/delete")
    event.metadata["actor_id"] = "ou_1"

    response = await service.handle_message(event)

    assert "没有当前会话" in response.messages[0].content
    assert harness.session_service.created == []


@pytest.mark.asyncio
async def test_rename_without_title_returns_usage_without_confirmation():
    harness = Harness()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")
    service = harness.service()
    event = message("/rename")
    event.metadata["actor_id"] = "ou_1"

    response = await service.handle_message(event)

    assert "用法" in response.messages[0].content
    assert "confirmation" not in response.messages[0].metadata


@pytest.mark.asyncio
async def test_rename_without_active_session_does_not_create_session():
    harness = Harness()
    service = harness.service()
    event = message("/rename New Title")
    event.metadata["actor_id"] = "ou_1"

    response = await service.handle_message(event)

    assert "没有当前会话" in response.messages[0].content
    assert harness.session_service.created == []


@pytest.mark.asyncio
async def test_schedule_remove_without_active_session_does_not_create_session():
    harness = Harness()
    service = harness.service(FakeScheduleService())
    event = message("/schedule remove sched-1")
    event.metadata["actor_id"] = "ou_1"

    response = await service.handle_message(event)

    assert "没有当前会话" in response.messages[0].content
    assert harness.session_service.created == []


@pytest.mark.asyncio
async def test_schedule_remove_without_task_id_returns_usage_without_confirmation():
    harness = Harness()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")
    service = harness.service(FakeScheduleService())
    event = message("/schedule remove")
    event.metadata["actor_id"] = "ou_1"

    response = await service.handle_message(event)

    assert "用法" in response.messages[0].content
    assert "confirmation" not in response.messages[0].metadata


@pytest.mark.asyncio
async def test_gateway_service_sessions_command_lists_links():
    harness = Harness()
    service = harness.service()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")

    response = await service.handle_message(message("/sessions"))

    assert "session-1" in response.messages[0].content


@pytest.mark.asyncio
async def test_gateway_service_tools_models_and_status_commands():
    service = Harness().service()

    tools = await service.handle_message(message("/tools", "event-tools"))
    models = await service.handle_message(message("/models", "event-models"))
    status = await service.handle_message(message("/status", "event-status"))

    assert "calculator" in tools.messages[0].content
    assert "model-a" in models.messages[0].content
    assert "provider" in status.messages[0].content


@pytest.mark.asyncio
async def test_gateway_schedule_add_uses_origin_metadata():
    harness = Harness()
    schedule = FakeScheduleService()
    service = harness.service(schedule)
    event = message("/schedule add */5 * * * * summarize", "event-schedule")
    event.metadata.update({"receive_id": "oc_1", "receive_id_type": "chat_id"})

    response = await service.handle_message(event)

    assert "sched-1" in response.messages[0].content
    assert schedule.created[0].cron_expression == "*/5 * * * *"
    assert schedule.created[0].prompt == "summarize"
    assert schedule.created[0].origin["receive_id"] == "oc_1"


@pytest.mark.asyncio
async def test_gateway_schedule_run_delegates_to_schedule_service():
    harness = Harness()
    schedule = FakeScheduleService()
    service = harness.service(schedule)

    response = await service.handle_message(message("/schedule run sched-1", "event-run"))

    assert schedule.run_ids == ["sched-1"]
    assert "ok" in response.messages[0].content


@pytest.mark.asyncio
async def test_gateway_service_switch_command_sets_active_session():
    harness = Harness()
    service = harness.service()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")

    response = await service.handle_message(message("/switch session-1"))

    assert "session-1" in response.messages[0].content
    assert harness.registry.active[key.conversation_parts] == "session-1"


@pytest.mark.asyncio
async def test_handle_message_propagates_trusted_metadata_to_chat_input():
    harness = Harness()
    service = harness.service()
    feishu_key = GatewaySessionKey(InteractionSourceType.FEISHU, "oc_a", thread_id="")
    event = InteractionMessage(
        id="evt-trust",
        session_key=feishu_key,
        text="每天早上 9 点提醒我看日报",
        metadata={
            "receive_id": "oc_a",
            "receive_id_type": "chat_id",
            "thread_id": "",
            "actor_id": "ou_x",
        },
    )

    await service.handle_message(event)

    request = harness.chat_service.requests[-1]
    assert request.trusted_metadata["gateway.source_type"] == "feishu"
    assert request.trusted_metadata["source_type"] == "feishu"
    assert request.trusted_metadata["receive_id"] == "oc_a"
    assert request.trusted_metadata["receive_id_type"] == "chat_id"
    assert request.trusted_metadata["actor_id"] == "ou_x"
    assert "capabilities" not in request.trusted_metadata
    assert request.metadata.get("gateway", {}).get("source_type") == "feishu"


@pytest.mark.asyncio
async def test_schedule_remove_confirmation_rejects_cross_origin_tasks():
    harness = Harness()
    feishu_key = GatewaySessionKey(InteractionSourceType.FEISHU, "oc_a", thread_id="")
    await harness.registry.create_session_link(feishu_key, "session-1")
    schedule = FakeScheduleService(
        tasks={"sched-1": _schedule_task("sched-1", receive_id="oc_b")}
    )
    service = harness.service(schedule)
    event = InteractionMessage(
        id="evt-remove",
        session_key=feishu_key,
        text="/schedule remove sched-1",
        metadata={
            "actor_id": "ou_x",
            "receive_id": "oc_a",
            "receive_id_type": "chat_id",
            "thread_id": "",
        },
    )

    pending = await service.handle_message(event)
    confirmation_id = pending.messages[0].metadata["confirmation"]["id"]
    response = await service.handle_confirmation(
        feishu_key,
        "ou_x",
        confirmation_id,
        GatewayConfirmationChoice.ONCE,
    )

    assert response.messages[0].content == "任务不存在"
    assert schedule.deleted_ids == []


@pytest.mark.asyncio
async def test_schedule_remove_confirmation_deletes_when_origin_matches():
    harness = Harness()
    feishu_key = GatewaySessionKey(InteractionSourceType.FEISHU, "oc_a", thread_id="")
    await harness.registry.create_session_link(feishu_key, "session-1")
    schedule = FakeScheduleService(
        tasks={"sched-1": _schedule_task("sched-1", receive_id="oc_a")}
    )
    service = harness.service(schedule)
    event = InteractionMessage(
        id="evt-remove-ok",
        session_key=feishu_key,
        text="/schedule remove sched-1",
        metadata={
            "actor_id": "ou_x",
            "receive_id": "oc_a",
            "receive_id_type": "chat_id",
            "thread_id": "",
        },
    )

    pending = await service.handle_message(event)
    confirmation_id = pending.messages[0].metadata["confirmation"]["id"]
    await service.handle_confirmation(
        feishu_key,
        "ou_x",
        confirmation_id,
        GatewayConfirmationChoice.ONCE,
    )

    assert schedule.deleted_ids == ["sched-1"]
