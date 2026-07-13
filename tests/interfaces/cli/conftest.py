from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.events import ChatEvent, ChatEventType
from app.domain.gateway import (
    GatewayHomeTarget,
    GatewaySessionKey,
    GatewaySessionLink,
    InteractionMessage,
    InteractionResponse,
)
from app.domain.platform import Platform
from app.domain.provider import ModelInfo
from app.domain.session import ConversationSession
from app.domain.tool import ToolDefinition
from app.interfaces.cli.render import make_console


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

    async def mark_event_processed(self, source: str, event_id: str, message_id: str = "") -> bool:
        marker = (source, event_id)
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
        self.requests: list[Any] = []

    async def complete(self, request):
        self.requests.append(request)
        self.last_input_model = request.model
        self.last_trusted_metadata = dict(request.trusted_metadata)
        self.last_permitted_managed_tools = set()
        if request.stream:
            return self._stream()
        from app.application.chat_service import ChatCompletionResult

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
class FakeServices:
    """Recording proxy wrapping a real GatewayService.

    Acts as both the services container and the gateway service: records the
    last event / conversation_id / confirmation_id seen, while delegating real
    behavior to the underlying GatewayService.
    """

    registry: _FakeRegistry = field(default_factory=_FakeRegistry)
    session_service: _FakeSessionService = field(default_factory=_FakeSessionService)
    chat_service: _FakeChatService = field(default_factory=_FakeChatService)
    last_conversation_id: str | None = None
    last_event: InteractionMessage | None = None
    last_confirmation_id: str | None = None
    _gateway: Any = None

    def __post_init__(self) -> None:
        from app.application.gateway_service import GatewayService

        self._gateway = GatewayService(
            self.registry,
            self.chat_service,
            self.session_service,
            _FakeToolService(),
            _FakeModelService(),
            lambda: {"provider": {"status": "ok"}, "gateway": {"status": "ok"}},
            schedule_service=None,
        )

    @property
    def gateway_service(self) -> "FakeServices":
        return self

    def health_snapshot(self) -> dict[str, Any]:
        return {"provider": {"status": "ok"}, "gateway": {"status": "ok"}}

    async def handle_message_stream(
        self, event: InteractionMessage, **kwargs: Any
    ) -> AsyncIterator[ChatEvent]:
        self.last_event = event
        self.last_conversation_id = event.session_key.platform_session_id
        self.last_stream_kwargs = kwargs
        async for evt in self._gateway.handle_message_stream(event, **kwargs):
            yield evt

    async def handle_message(self, event: InteractionMessage) -> InteractionResponse:
        self.last_event = event
        self.last_conversation_id = event.session_key.platform_session_id
        return await self._gateway.handle_message(event)

    async def handle_confirmation(self, session_key, actor_id, confirmation_id, choice):
        self.last_confirmation_id = confirmation_id
        return await self._gateway.handle_confirmation(session_key, actor_id, confirmation_id, choice)

    def grant_tool_for_session(self, session_id: str, actor_id: str, tool_name: str) -> None:
        self._gateway.grant_tool_for_session(session_id, actor_id, tool_name)

    def is_tool_granted(self, session_id: str, actor_id: str, tool_name: str) -> bool:
        return self._gateway.is_tool_granted(session_id, actor_id, tool_name)


@pytest.fixture
def fake_services() -> FakeServices:
    return FakeServices()


@pytest.fixture
def fake_gateway_service(fake_services: FakeServices) -> FakeServices:
    return fake_services


@pytest.fixture
def fake_console():
    return make_console(force_terminal=False)


@dataclass
class _FakeCliChatAdapter:
    """Fake CliChatAdapter for REPL tests. Records calls + returns scripted stream responses."""

    stream_responses: list[list[tuple[str, dict[str, Any]]]] = field(default_factory=list)
    last_confirm_id: str | None = None
    sent_texts: list[str] = field(default_factory=list)
    last_approval_decider: Any = None

    async def send_stream(
        self, text: str, conversation_id: str, *, approval_decider: Any = None
    ) -> AsyncIterator[ChatEvent]:
        self.sent_texts.append(text)
        self.last_approval_decider = approval_decider
        if self.stream_responses:
            scripted = self.stream_responses.pop(0)
        else:
            scripted = [
                ("message_start", {}),
                ("content_delta", {"content": "pong"}),
                ("message_done", {"finish_reason": "stop"}),
                ("done", {}),
            ]
        for evt_type, payload in scripted:
            yield ChatEvent(ChatEventType(evt_type), **payload)

    async def send(self, text: str, conversation_id: str):
        from app.domain.gateway import GatewayOutboundMessage, InteractionResponse

        self.sent_texts.append(text)
        return InteractionResponse(
            session_id="session-1",
            messages=[GatewayOutboundMessage(content="pong", metadata={})],
        )

    async def confirm(self, confirmation_id: str, choice: str, conversation_id: str):
        from app.domain.gateway import GatewayOutboundMessage, InteractionResponse

        self.last_confirm_id = confirmation_id
        return InteractionResponse(
            session_id="session-1",
            messages=[GatewayOutboundMessage(content="confirmed", metadata={})],
        )


@pytest.fixture
def fake_chat_adapter() -> _FakeCliChatAdapter:
    return _FakeCliChatAdapter()
