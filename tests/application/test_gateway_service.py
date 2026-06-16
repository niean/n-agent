from dataclasses import dataclass, field

import pytest

from app.application.gateway_service import GatewayService
from app.application.chat_service import ChatCompletionResult
from app.domain.gateway import GatewaySessionKey, GatewaySessionLink, InteractionMessage, InteractionSourceType
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

    async def create_session(self, session_id: str, source: str = "dashboard"):
        session = ConversationSession(id=session_id, source=source)
        self.created.append(session)
        return session


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
    def __init__(self):
        self.created = []
        self.run_ids = []

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

    async def pause(self, task_id):
        return None

    async def resume(self, task_id):
        return None

    async def run_now(self, task_id):
        self.run_ids.append(task_id)
        return {"status": "ok"}

    async def delete(self, task_id):
        return True


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
    event.metadata.update({"receive_id": "oc_1", "receive_id_type": "chat_id", "capabilities": ["active_text_delivery"]})

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
