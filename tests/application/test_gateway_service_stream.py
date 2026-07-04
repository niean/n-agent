from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult
from app.application.events import ChatEvent, ChatEventType
from app.application.gateway_service import GatewayService
from app.domain.gateway import GatewayHomeTarget, GatewaySessionKey, GatewaySessionLink, InteractionMessage
from app.domain.platform import Platform
from app.domain.provider import ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import ToolDefinition


class _FakeRegistry:
    def __init__(self) -> None:
        self.active: dict[tuple[str, str, str], str] = {}
        self.links: dict[tuple[str, str, str], list[GatewaySessionLink]] = {}
        self.processed: set[tuple[str, str]] = set()
        self.home_targets: dict[Platform, GatewayHomeTarget] = {}

    async def get_active_session(self, key: GatewaySessionKey) -> GatewaySessionLink | None:
        session_id = self.active.get(key.conversation_parts)
        if session_id is None:
            return None
        return GatewaySessionLink("conversation-1", session_id, key.display_name)

    async def create_session_link(self, key: GatewaySessionKey, session_id: str) -> GatewaySessionLink:
        link = GatewaySessionLink("conversation-1", session_id, key.display_name)
        self.links.setdefault(key.conversation_parts, []).append(link)
        self.active[key.conversation_parts] = session_id
        return link

    async def set_active_session(self, key: GatewaySessionKey, session_id: str) -> GatewaySessionLink:
        self.active[key.conversation_parts] = session_id
        return GatewaySessionLink("conversation-1", session_id, key.display_name)

    async def list_session_links(self, key: GatewaySessionKey) -> list[GatewaySessionLink]:
        return self.links.get(key.conversation_parts, [])

    async def delete_session_link(self, session_id: str) -> None:
        for key, links in self.links.items():
            self.links[key] = [link for link in links if link.session_id != session_id]
            if self.active.get(key) == session_id:
                self.active.pop(key)

    async def mark_event_processed(self, platform: Platform, event_id: str, message_id: str = "") -> bool:
        marker = (platform.value, event_id)
        if marker in self.processed:
            return False
        self.processed.add(marker)
        return True

    async def set_home_target(self, target: GatewayHomeTarget) -> GatewayHomeTarget:
        self.home_targets[target.platform] = target
        return target

    async def get_home_target(self, platform: Platform) -> GatewayHomeTarget | None:
        return self.home_targets.get(platform)


class _FakeSessionService:
    def __init__(self) -> None:
        self.created: list[ConversationSession] = []

    async def create_session(self, session_id: str, source: str = "dashboard") -> ConversationSession:
        session = ConversationSession(id=session_id, source=source)
        self.created.append(session)
        return session

    async def rename_session(self, session_id: str, title: str) -> ConversationSession:
        return ConversationSession(id=session_id, title=title)

    async def delete_session(self, session_id: str) -> None:
        return None


class _FakeChatService:
    def __init__(self) -> None:
        self.last_input_model: str | None = None
        self.last_trusted_metadata: dict[str, Any] = {}
        self.last_permitted_managed_tools: set[str] | None = None
        self.requests: list[ChatCompletionInput] = []

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult | AsyncIterator[ChatEvent]:
        self.requests.append(request)
        self.last_input_model = request.model
        self.last_trusted_metadata = dict(request.trusted_metadata)
        self.last_permitted_managed_tools = set()
        if request.stream:
            return self._stream()
        return ChatCompletionResult(
            session_id=request.session_id or "missing",
            model=request.model,
            message={"role": "assistant", "content": "pong"},
        )

    async def _stream(self) -> AsyncIterator[ChatEvent]:
        yield ChatEvent(ChatEventType.MESSAGE_START)
        yield ChatEvent(ChatEventType.CONTENT_DELTA, content="pong")
        yield ChatEvent(ChatEventType.MESSAGE_DONE, finish_reason="stop")
        yield ChatEvent(ChatEventType.DONE)


class _FakeToolService:
    def list_definitions(self) -> list[ToolDefinition]:
        return [ToolDefinition("calculator", "Calculate", {"type": "object"})]


class _FakeModelService:
    @property
    def default_model(self) -> str:
        return "model-a"

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo("model-a", "Model A", "test", True, True)]


@dataclass
class _Harness:
    registry: _FakeRegistry = field(default_factory=_FakeRegistry)
    session_service: _FakeSessionService = field(default_factory=_FakeSessionService)
    chat_service: _FakeChatService = field(default_factory=_FakeChatService)

    def service(self) -> GatewayService:
        return GatewayService(
            self.registry,
            self.chat_service,
            self.session_service,
            _FakeToolService(),
            _FakeModelService(),
            lambda: {"provider": {"status": "ok"}, "gateway": {"status": "ok"}},
            schedule_service=None,
        )


def _cli_event(text: str, event_id: str = "evt-1") -> InteractionMessage:
    return InteractionMessage(
        id=event_id,
        session_key=GatewaySessionKey(Platform.CLI, "conv-1", display_name="conv-1"),
        text=text,
        metadata={"actor_id": "cli:conv-1"},
    )


@pytest.fixture
def gateway_service_with_fake_registry():
    harness = _Harness()
    return harness.service(), harness.chat_service


@pytest.mark.asyncio
async def test_handle_message_stream_duplicate_event_yields_done_only(gateway_service_with_fake_registry):
    svc, fake_chat = gateway_service_with_fake_registry
    event = _cli_event("hello", "evt-1")

    events1 = [e async for e in svc.handle_message_stream(event)]
    assert events1[-1].type == ChatEventType.DONE

    events2 = [e async for e in svc.handle_message_stream(event)]
    assert len(events2) == 1
    assert events2[0].type == ChatEventType.DONE
    assert events2[0].metadata.get("duplicate") is True


@pytest.mark.asyncio
async def test_handle_message_stream_slash_command_yields_message_done(gateway_service_with_fake_registry):
    svc, _ = gateway_service_with_fake_registry
    event = _cli_event("/sessions", "evt-2")

    events = [e async for e in svc.handle_message_stream(event)]
    types = [e.type for e in events]
    assert ChatEventType.MESSAGE_DONE in types
    assert types[-1] == ChatEventType.DONE


@pytest.mark.asyncio
async def test_handle_message_stream_destructive_yields_confirmation_metadata(gateway_service_with_fake_registry):
    svc, _ = gateway_service_with_fake_registry
    key = GatewaySessionKey(Platform.CLI, "conv-1", display_name="conv-1")
    await svc.registry.create_session_link(key, "session-1")
    event = _cli_event("/delete", "evt-3")

    events = [e async for e in svc.handle_message_stream(event)]
    done_evt = next(e for e in events if e.type is ChatEventType.MESSAGE_DONE)
    assert done_evt.finish_reason == "confirmation_required"
    confirmation = done_evt.metadata.get("confirmation") or {}
    assert "id" in confirmation
    assert confirmation["id"]


@pytest.mark.asyncio
async def test_handle_message_stream_normal_chat_uses_default_model(gateway_service_with_fake_registry):
    svc, fake_chat = gateway_service_with_fake_registry
    event = _cli_event("hello", "evt-4")

    events = [e async for e in svc.handle_message_stream(event)]
    assert events[-1].type == ChatEventType.DONE
    assert fake_chat.last_input_model == svc.command_service.model_service.default_model


@pytest.mark.asyncio
async def test_handle_message_stream_cli_trusted_metadata_no_feishu_managed(gateway_service_with_fake_registry):
    svc, fake_chat = gateway_service_with_fake_registry
    event = _cli_event("hello", "evt-5")

    await svc.handle_message_stream(event).__anext__()
    tm = fake_chat.last_trusted_metadata
    assert tm.get("gateway.platform") == "cli"
    permitted = fake_chat.last_permitted_managed_tools or set()
    assert "manage_schedule" not in permitted
