from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from app.application.gateway_service import GatewayService
from app.application.chat_service import ChatCompletionResult
from app.domain.gateway import GatewayConfirmationChoice, GatewayHomeTarget, GatewaySessionKey, GatewaySessionLink, InteractionMessage
from app.domain.platform import Platform
from app.domain.provider import ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import ToolDefinition


class FakeGatewayRegistry:
    def __init__(self):
        self.active: dict[tuple[str, str, str], str] = {}
        self.links: dict[tuple[str, str, str], list[GatewaySessionLink]] = {}
        self.processed: set[tuple[str, str]] = set()
        self.home_targets: dict[Platform, GatewayHomeTarget] = {}

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

    async def mark_event_processed(self, source, event_id, message_id=""):
        marker = (source, event_id)
        if marker in self.processed:
            return False
        self.processed.add(marker)
        return True

    async def set_home_target(self, target):
        self.home_targets[target.platform] = target
        return target

    async def get_home_target(self, platform):
        return self.home_targets.get(platform)


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
        self.compress_calls = []

    async def complete(self, request):
        self.requests.append(request)
        return ChatCompletionResult(
            session_id=request.session_id or "missing",
            model=request.model,
            message={"role": "assistant", "content": "pong"},
        )

    async def compress_session(self, session_id):
        self.compress_calls.append(session_id)
        return {"compressed": True, "reason": None}


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


def _schedule_task(task_id: str, *, receive_id: str, receive_id_type: str = "chat_id", thread_id: str = "", platform: str = "feishu"):
    from datetime import datetime, timezone

    from app.domain.schedule import (
        DeliveryTarget,
        ScheduledTask,
        ScheduleExpression,
        ScheduleTimezone,
    )

    origin = {"platform": platform, "receive_id": receive_id, "receive_id_type": receive_id_type, "thread_id": thread_id}
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

    def command_service_pending_confirmations(self):
        return self.service().command_service.pending_confirmations


def message(text="hello", event_id="event-1"):
    return InteractionMessage(
        id=event_id,
        session_key=GatewaySessionKey("cli", "local", display_name="Local"),
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
    assert harness.chat_service.requests[0].model == "model-a"


@pytest.mark.asyncio
async def test_gateway_service_passes_approval_decider_to_chat_service():
    harness = Harness()
    service = harness.service()
    approval_decider = object()

    await service.handle_message(message(), approval_decider=approval_decider)

    assert harness.chat_service.requests[0].approval_decider is approval_decider


@pytest.mark.asyncio
async def test_gateway_service_applies_session_tool_grants_by_actor():
    harness = Harness()
    service = harness.service()
    event = message(event_id="event-granted")
    event.metadata["actor_id"] = "ou_1"
    await harness.registry.create_session_link(event.session_key, "session-1")
    service.grant_tool_for_session("session-1", "ou_1", "mcp_site_probe")

    await service.handle_message(event)

    assert harness.chat_service.requests[0].allowed_confirm_tools_override == {
        "mcp_site_probe": "session"
    }


@pytest.mark.asyncio
async def test_gateway_service_does_not_share_session_tool_grants_with_other_actor():
    harness = Harness()
    service = harness.service()
    event = message(event_id="event-other-actor")
    event.metadata["actor_id"] = "ou_2"
    await harness.registry.create_session_link(event.session_key, "session-1")
    service.grant_tool_for_session("session-1", "ou_1", "mcp_site_probe")

    await service.handle_message(event)

    assert harness.chat_service.requests[0].allowed_confirm_tools_override == {}


@pytest.mark.asyncio
async def test_gateway_destructive_preflight_does_not_call_chat_with_approval_decider():
    harness = Harness()
    service = harness.service()
    event = message("/new")
    event.metadata["actor_id"] = "ou_1"

    response = await service.handle_message(event, approval_decider=object())

    assert response.messages[0].metadata["confirmation"]["action"] == "new"
    assert harness.chat_service.requests == []


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
async def test_gateway_confirmation_notifies_consumed_before_execution():
    harness = Harness()
    service = harness.service()
    event = message("/new")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)
    confirmation_id = pending.messages[0].metadata["confirmation"]["id"]
    callback_state = []

    async def on_consumed():
        callback_state.append(
            (
                service.owns_confirmation(confirmation_id),
                len(harness.session_service.created),
            )
        )

    await service.handle_confirmation(
        event.session_key,
        "ou_1",
        confirmation_id,
        GatewayConfirmationChoice.ONCE,
        on_consumed=on_consumed,
    )

    assert callback_state == [(False, 0)]
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
        tasks={"sched-1": _schedule_task("sched-1", receive_id=key.platform_session_id, thread_id=key.thread_id)}
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
        tasks={"sched-1": _schedule_task("sched-1", receive_id=key.platform_session_id, thread_id=key.thread_id, platform="")}
    )
    service = harness.service(schedule)
    event = message("/schedule remove sched-1")
    event.metadata["actor_id"] = "ou_1"
    event.metadata["receive_id"] = key.platform_session_id
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
async def test_gateway_service_compress_command_calls_compress_session():
    harness = Harness()
    service = harness.service()

    response = await service.handle_message(message("/compress", "event-compress"))

    assert "已压缩上下文" in response.messages[0].content
    assert harness.chat_service.compress_calls == [response.session_id]


@pytest.mark.asyncio
async def test_gateway_service_compress_command_without_chat_service():
    from app.application.gateway_service import GatewayCommandService
    from app.domain.gateway import GatewaySessionKey, InteractionMessage

    cmd = GatewayCommandService(
        registry=None,
        session_service=None,
        tool_service=None,
        model_service=None,
        health_provider=lambda: {},
        schedule_service=None,
        chat_service=None,
    )
    event = InteractionMessage(
        id="evt-1",
        session_key=GatewaySessionKey("cli", "local", display_name="Local"),
        text="/compress",
    )
    response = await cmd.handle(event, "session-1")
    assert "上下文压缩未启用" in response.messages[0].content


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
    assert schedule.created[0].origin["source"] == "cli"
    assert "platform" not in schedule.created[0].origin
    assert schedule.created[0].session_id is None


@pytest.mark.asyncio
async def test_gateway_schedule_add_parses_tools_grant():
    harness = Harness()
    schedule = FakeScheduleService()
    service = harness.service(schedule)
    event = message("/schedule add */5 * * * * summarize --tools host_terminal", "event-schedule-tools")
    event.metadata.update({"receive_id": "oc_1", "receive_id_type": "chat_id"})

    response = await service.handle_message(event)

    assert "sched-1" in response.messages[0].content
    created = schedule.created[0]
    assert created.cron_expression == "*/5 * * * *"
    assert created.prompt == "summarize"
    assert created.allowed_tools == ("host_terminal",)


@pytest.mark.asyncio
async def test_gateway_schedule_add_without_tools_has_empty_grant():
    harness = Harness()
    schedule = FakeScheduleService()
    service = harness.service(schedule)
    event = message("/schedule add */5 * * * * summarize", "event-schedule-no-tools")
    event.metadata.update({"receive_id": "oc_1", "receive_id_type": "chat_id"})

    await service.handle_message(event)

    assert schedule.created[0].allowed_tools == ()


@pytest.mark.asyncio
async def test_gateway_sethome_switches_future_schedule_home_reference():
    harness = Harness()
    service = harness.service(FakeScheduleService())
    first = InteractionMessage(
        id="event-home-1",
        session_key=GatewaySessionKey(Platform.FEISHU, "oc_current", display_name="Current"),
        text="/sethome",
    )
    first.metadata.update({"receive_id": "oc_old", "receive_id_type": "chat_id"})
    second = InteractionMessage(
        id="event-home-2",
        session_key=GatewaySessionKey(Platform.FEISHU, "oc_current", display_name="Current"),
        text="/sethome",
    )
    second.metadata.update({"receive_id": "oc_new", "receive_id_type": "chat_id"})

    await service.handle_message(first)
    await service.handle_message(second)

    assert harness.registry.home_targets[Platform.FEISHU].receive_id == "oc_new"


@pytest.mark.asyncio
async def test_feishu_schedule_add_uses_existing_home_reference_without_overwriting():
    harness = Harness()
    schedule = FakeScheduleService()
    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_current", display_name="Current")
    await harness.registry.set_home_target(GatewayHomeTarget(Platform.FEISHU, "oc_home", "chat_id"))
    service = harness.service(schedule)
    event = InteractionMessage(
        id="event-schedule-home",
        session_key=feishu_key,
        text="/schedule add */5 * * * * summarize",
        metadata={"receive_id": "oc_current", "receive_id_type": "chat_id"},
    )

    await service.handle_message(event)

    assert schedule.created[0].origin == {"platform": "feishu", "target": "home"}
    assert harness.registry.home_targets[Platform.FEISHU].receive_id == "oc_home"


@pytest.mark.asyncio
async def test_feishu_schedule_add_auto_sets_home_when_missing():
    harness = Harness()
    schedule = FakeScheduleService()
    service = harness.service(schedule)
    event = InteractionMessage(
        id="event-schedule-auto-home",
        session_key=GatewaySessionKey(Platform.FEISHU, "oc_auto", display_name="Auto"),
        text="/schedule add */5 * * * * summarize",
        metadata={"receive_id": "oc_auto", "receive_id_type": "chat_id"},
    )

    await service.handle_message(event)

    assert schedule.created[0].origin == {"platform": "feishu", "target": "home"}
    assert harness.registry.home_targets[Platform.FEISHU].receive_id == "oc_auto"


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
    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_a", thread_id="")
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
    assert request.trusted_metadata["gateway.platform"] == "feishu"
    assert request.trusted_metadata["platform"] == "feishu"
    assert request.trusted_metadata["receive_id"] == "oc_a"
    assert request.trusted_metadata["receive_id_type"] == "chat_id"
    assert request.trusted_metadata["actor_id"] == "ou_x"
    assert "capabilities" not in request.trusted_metadata
    assert request.metadata.get("gateway", {}).get("platform") == "feishu"


@pytest.mark.asyncio
async def test_schedule_remove_confirmation_rejects_cross_origin_tasks():
    harness = Harness()
    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_a", thread_id="")
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
    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_a", thread_id="")
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


@pytest.mark.asyncio
async def test_gateway_new_session_feishu_uses_flattened_source_and_prefix():
    harness = Harness()
    service = harness.service()
    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_feishu", display_name="Feishu Chat")
    event = InteractionMessage(
        id="event-feishu-new",
        session_key=feishu_key,
        text="/new",
        metadata={},
    )

    response = await service.handle_message(event)

    assert response.messages[0].content.startswith("已创建新会话")
    created = harness.session_service.created[-1]
    assert created.source == "feishu"
    assert created.id.startswith("feishu-")


def test_session_id_prefix_and_source_flattens_im_platforms():
    from app.application.gateway_service import _session_id_prefix_and_source

    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_x")
    assert _session_id_prefix_and_source(feishu_key) == ("feishu", "feishu")

    dingtalk_key = GatewaySessionKey(Platform.DINGTALK, "oc_y")
    assert _session_id_prefix_and_source(dingtalk_key) == ("dingtalk", "dingtalk")

    wecom_key = GatewaySessionKey(Platform.WECOM, "oc_z")
    assert _session_id_prefix_and_source(wecom_key) == ("wecom", "wecom")

    cli_key = GatewaySessionKey("cli", "conv-1")
    assert _session_id_prefix_and_source(cli_key) == ("cli", "cli")

    acp_key = GatewaySessionKey("acp", "acp-session-1")
    assert _session_id_prefix_and_source(acp_key) == ("acp", "acp")


@pytest.mark.asyncio
async def test_gateway_handle_message_text_and_images_constructs_content_array():
    harness = Harness()
    service = harness.service()

    event = InteractionMessage(
        id="evt-img-1",
        session_key=GatewaySessionKey("cli", "local", display_name="Local"),
        text="看这张图",
        images=["data:image/png;base64,aGVsbG8="],
    )
    await service.handle_message(event)

    request = harness.chat_service.requests[0]
    content = request.messages[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "看这张图"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}}


@pytest.mark.asyncio
async def test_gateway_handle_message_image_only_constructs_image_only_content_array():
    harness = Harness()
    service = harness.service()

    event = InteractionMessage(
        id="evt-img-2",
        session_key=GatewaySessionKey("cli", "local", display_name="Local"),
        text="",
        images=["data:image/png;base64,aGVsbG8="],
    )
    await service.handle_message(event)

    request = harness.chat_service.requests[0]
    content = request.messages[0]["content"]
    assert isinstance(content, list)
    assert len(content) == 1
    assert content[0]["type"] == "image_url"


@pytest.mark.asyncio
async def test_gateway_handle_message_slash_with_images_rejected_without_confirmation():
    harness = Harness()
    service = harness.service()

    event = InteractionMessage(
        id="evt-slash-img",
        session_key=GatewaySessionKey("cli", "local", display_name="Local"),
        text="/new",
        images=["data:image/png;base64,aGVsbG8="],
        metadata={"actor_id": "cli:local"},
    )
    response = await service.handle_message(event)

    assert harness.command_service_pending_confirmations() == {}
    assert response.messages
    assert "图片" in response.messages[0].content or "不支持" in response.messages[0].content


@pytest.mark.asyncio
async def test_gateway_handle_message_destructive_slash_with_images_rejected_without_confirmation():
    harness = Harness()
    service = harness.service()
    await harness.registry.create_session_link(
        GatewaySessionKey("cli", "local", display_name="Local"), "sess-existing"
    )

    event = InteractionMessage(
        id="evt-slash-img-del",
        session_key=GatewaySessionKey("cli", "local", display_name="Local"),
        text="/delete",
        images=["data:image/png;base64,aGVsbG8="],
        metadata={"actor_id": "cli:local"},
    )
    response = await service.handle_message(event)

    assert harness.command_service_pending_confirmations() == {}
    assert response.messages


# ---------------------------------------------------------------------------
# Spy tests: verify denials happen BEFORE any business write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spy_forged_actor_confirmation_denies_before_writes():
    """A different actor confirming must not trigger any SessionService/registry writes."""
    harness = Harness()
    key = message().session_key
    await harness.registry.create_session_link(key, "session-1")
    service = harness.service()
    event = message("/delete")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    # Forge: a different actor tries to confirm
    response = await service.handle_confirmation(
        key,
        "ou_forged",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert "只有命令发起者可以确认" in response.messages[0].content
    # Spy: no business writes occurred
    assert harness.session_service.created == []
    assert harness.session_service.renamed == []
    assert harness.session_service.deleted == []


@pytest.mark.asyncio
async def test_spy_cross_session_confirmation_denies_before_writes():
    """Confirmation from a different session_key must not trigger writes."""
    harness = Harness()
    await harness.registry.create_session_link(message().session_key, "session-1")
    service = harness.service()
    event = message("/delete")
    event.metadata["actor_id"] = "ou_1"
    pending = await service.handle_message(event)

    # Cross-session: different session_key tries to confirm
    other_key = GatewaySessionKey("cli", "other-session", display_name="Other")
    response = await service.handle_confirmation(
        other_key,
        "ou_1",
        pending.messages[0].metadata["confirmation"]["id"],
        GatewayConfirmationChoice.ONCE,
    )

    assert "确认已失效" in response.messages[0].content
    # Spy: no business writes occurred
    assert harness.session_service.created == []
    assert harness.session_service.renamed == []
    assert harness.session_service.deleted == []


@pytest.mark.asyncio
async def test_spy_cross_origin_schedule_remove_denies_before_writes():
    """Cross-origin schedule remove must not call schedule_service.delete."""
    harness = Harness()
    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_a", thread_id="")
    await harness.registry.create_session_link(feishu_key, "session-1")
    schedule = FakeScheduleService(
        tasks={"sched-1": _schedule_task("sched-1", receive_id="oc_b")}
    )
    service = harness.service(schedule)
    event = InteractionMessage(
        id="evt-remove-spy",
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
    # Spy: schedule_service.delete was not called
    assert schedule.deleted_ids == []
    # Spy: no session writes either
    assert harness.session_service.created == []
    assert harness.session_service.deleted == []


# ---------------------------------------------------------------------------
# T11: realtime contract -- session_id passing + DEFAULT approval tool surface
# ---------------------------------------------------------------------------


def _default_exposed_tool_names_with_approval_tools() -> set[str]:
    """Construct a ToolService wired with user_task_approval_tool_definitions
    (mirrors T8 wiring in app/main.py) and return DEFAULT-exposed tool names."""
    from app.application.task_tools import user_task_approval_tool_definitions
    from app.application.tool_service import ToolService
    from app.domain.tool_policy import ToolExposurePolicy

    class _UnusedExecutor:
        async def execute(self, request, context=None):
            raise RuntimeError("executor not used by list_openai_tools")

    tool_service = ToolService(executor=_UnusedExecutor(), definitions=[])
    tool_service.set_dynamic_definitions(
        "user_task", user_task_approval_tool_definitions()
    )
    return {
        t["function"]["name"]
        for t in tool_service.list_openai_tools(ToolExposurePolicy.DEFAULT, None)
    }


@pytest.mark.asyncio
async def test_realtime_tool_context_passes_session_id_and_exposes_approval_tools():
    """Web/API Chat realtime contract (T11).

    GatewayService.handle_message must pass the resolved session_id through to
    ChatCompletionService, carry REALTIME execution_mode on ingress_facts, and
    the shared ToolService DEFAULT surface must contain approve_task /
    reject_task / revise_task (wired by T8).
    """
    from app.domain.policy import ExecutionMode

    harness = Harness()
    service = harness.service()

    response = await service.handle_message(message(event_id="event-rt-contract"))

    # session_id passes through to ChatCompletionInput unchanged
    request = harness.chat_service.requests[-1]
    assert request.session_id is not None
    assert request.session_id == response.session_id

    # realtime execution mode on ingress_facts (authoritative claim)
    assert request.ingress_facts is not None
    assert request.ingress_facts.execution_mode is ExecutionMode.REALTIME
    # trusted_metadata must not downgrade to unattended
    assert request.trusted_metadata.get("execution_mode") != "unattended"

    # Shared ToolService DEFAULT surface contains the three approval tools
    tool_names = _default_exposed_tool_names_with_approval_tools()
    assert {"approve_task", "reject_task", "revise_task"}.issubset(tool_names)
