"""Tests for NAgentACPAgent (T12).

Covers S 1-S 10 of the T12 spec. Uses a minimal fake services object that
exposes the attributes NAgentACPAgent consumes; we do NOT construct a real
ApplicationServices dataclass because that requires SQLite + provider
seeding and pulls in dozens of unrelated modules.

The fake ChatService returns a configurable async iterator of ChatEvent so
we can exercise the prompt flow end-to-end, including the busy-refusal path
(S 4) by holding a lock open.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from acp.interfaces import Client
from acp.schema import (
    AllowedOutcome,
    AuthenticateResponse,
    ImageContentBlock,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    RequestPermissionResponse,
    TextContentBlock,
)

from app.application.events import ChatEvent, ChatEventType
from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionInput, ChatCompletionService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService
from app.config import Settings
from app.domain.gateway import GatewaySessionKey, GatewaySessionLink
from app.domain.session import ConversationMessage, ConversationSession
from app.domain.provider import LLMResult, ModelInfo
from app.domain.tool import (
    ApprovalRequest,
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
)
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.cli.commands.acp.agent import NAgentACPAgent


class FakeConn:
    """Records session_update calls; stands in for acp.Client."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permission_responses: dict[str, Any] = {}
        self.permission_calls: list[dict[str, Any]] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, **kwargs: Any) -> Any:
        self.permission_calls.append(kwargs)
        response = self.permission_responses.get("next")
        if response is None:
            raise RuntimeError("request_permission not expected in T12 tests")
        return response


class FakeChatService:
    """Returns a configurable async iterator from complete()."""

    def __init__(self) -> None:
        self.inputs: list[Any] = []
        self._events: list[ChatEvent] = [
            ChatEvent(ChatEventType.MESSAGE_START),
            ChatEvent(ChatEventType.CONTENT_DELTA, content="hi"),
            ChatEvent(ChatEventType.DONE),
        ]
        self._gate: asyncio.Event | None = None
        self.graph_runner = FakeGraphRunner()

    def set_events(self, events: list[ChatEvent]) -> None:
        self._events = events

    def set_gate(self, gate: asyncio.Event | None) -> None:
        self._gate = gate

    async def complete(self, request: Any) -> Any:
        self.inputs.append(request)
        events = list(self._events)
        gate = self._gate

        async def _stream():
            if gate is not None:
                await gate.wait()
            for event in events:
                yield event

        return _stream()


class FakeGraphRunner:
    """Records interrupt calls."""

    def __init__(self) -> None:
        self.interrupt_calls: list[str] = []

    def interrupt(self, session_id: str) -> bool:
        self.interrupt_calls.append(session_id)
        return True


class FakeGatewayRegistry:
    def __init__(self) -> None:
        self.active: dict[tuple[str, str, str], str] = {}

    async def set_active_session(self, key: GatewaySessionKey, session_id: str) -> GatewaySessionLink:
        self.active[key.conversation_parts] = session_id
        return GatewaySessionLink(key.platform_session_id, session_id, key.display_name)


class FakeGatewayService:
    def __init__(self, chat_service: FakeChatService) -> None:
        self.chat_service = chat_service
        self.calls: list[dict[str, Any]] = []

    async def handle_message_stream(self, event, **kwargs):
        self.calls.append({"event": event, "kwargs": kwargs})
        stream = await self.chat_service.complete(
            ChatCompletionInput(
                model=kwargs.get("model_override") or "test-model",
                messages=[{"role": "user", "content": event.text}],
                stream=True,
                session_id=event.session_key.platform_session_id,
                trusted_metadata=dict(kwargs.get("trusted_metadata_override") or {}),
                options=dict(kwargs.get("options_override") or {}),
                approval_decider=kwargs.get("approval_decider"),
                allowed_confirm_tools_override=kwargs.get("allowed_confirm_tools_override"),
            )
        )
        async for evt in stream:
            yield evt


class FakeProviderHolder:
    def __init__(self) -> None:
        self.current_model = "test-model"
        self.current_config = None


class FakeProviderService:
    def __init__(self) -> None:
        self.activate_calls: list[str] = []
        self.swap_calls: list[tuple[str, str]] = []

    async def activate(self, provider_id: str) -> None:
        self.activate_calls.append(provider_id)

    async def swap(self, provider_id: str, api_key: str) -> None:
        self.swap_calls.append((provider_id, api_key))


@dataclass
class FakeServices:
    chat_service: FakeChatService = field(default_factory=FakeChatService)
    session_service: SessionService = None  # type: ignore[assignment]
    memory_store: SQLiteMemoryStore = None  # type: ignore[assignment]
    provider_holder: FakeProviderHolder = field(default_factory=FakeProviderHolder)
    provider_service: FakeProviderService = field(default_factory=FakeProviderService)
    gateway_registry: FakeGatewayRegistry = field(default_factory=FakeGatewayRegistry)
    gateway_service: FakeGatewayService = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.gateway_service is None:
            self.gateway_service = FakeGatewayService(self.chat_service)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        sqlite_path=tmp_path / "sessions.db",
        workspace_root=tmp_path / "workspace",
        acp_host_workspace_root=tmp_path / "host",
        acp_container_workspace_root=tmp_path / "workspace",
    )


@pytest.fixture
def memory_store(settings) -> SQLiteMemoryStore:
    store = SQLiteMemoryStore(settings.sqlite_path)
    return store


@pytest.fixture
def services(memory_store) -> FakeServices:
    return FakeServices(
        session_service=SessionService(memory_store),
        memory_store=memory_store,
    )


@pytest.fixture
def agent(services, settings) -> NAgentACPAgent:
    return NAgentACPAgent(services, settings)


@pytest.fixture
def conn() -> FakeConn:
    return FakeConn()


def _text_block(text: str) -> TextContentBlock:
    return TextContentBlock(text=text, type="text")


def _image_block(data: str = "aGVsbG8=", mime_type: str = "image/png") -> ImageContentBlock:
    return ImageContentBlock(type="image", data=data, mime_type=mime_type)


# ---- S 1: on_connect is sync ------------------------------------------------


def test_on_connect_is_sync_and_stores_conn(agent, conn):
    # Must NOT be a coroutine -- sync per SDK contract.
    result = agent.on_connect(conn)
    assert asyncio.iscoroutine(result) is False
    assert agent._conn is conn
    assert agent._permission_bridge is not None


# ---- S 2: initialize returns proper response --------------------------------


@pytest.mark.asyncio
async def test_initialize_returns_response_with_capabilities(agent):
    response = await agent.initialize()

    assert isinstance(response, InitializeResponse)
    assert response.agent_info.name == "n-agent"
    caps = response.agent_capabilities
    assert caps.load_session is True
    assert caps.prompt_capabilities.image is True
    assert caps.session_capabilities is not None
    assert caps.session_capabilities.fork is not None
    assert caps.session_capabilities.list is not None
    assert caps.session_capabilities.resume is not None
    # auth_methods always advertises the terminal setup method
    assert len(response.auth_methods) >= 1


# ---- S 2: authenticate ------------------------------------------------------


@pytest.mark.asyncio
async def test_authenticate_unknown_method_returns_none(agent):
    # initialize first so _auth_methods is populated
    await agent.initialize()
    result = await agent.authenticate(method_id="nonexistent-method")
    assert result is None


@pytest.mark.asyncio
async def test_authenticate_known_method_returns_response(agent):
    await agent.initialize()
    method_id = agent._auth_methods[0].id
    result = await agent.authenticate(method_id=method_id)
    assert isinstance(result, AuthenticateResponse)


# ---- S 2: new_session with mappable / unmappable cwd ------------------------


@pytest.mark.asyncio
async def test_new_session_with_mappable_cwd_creates_acp_session(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    response = await agent.new_session(cwd=str(host_root / "project"))

    assert isinstance(response, NewSessionResponse)
    assert response.session_id
    assert response.session_id.startswith("acp-")
    stored = await services.memory_store.get_session(response.session_id)
    assert stored is not None
    assert stored.source == "acp"
    assert stored.acp_metadata is not None
    # cwd is mapped to container root + project
    assert "project" in stored.acp_metadata["cwd"]


@pytest.mark.asyncio
async def test_new_session_with_unmappable_cwd_raises(agent):
    # Settings has acp_host_workspace_root set, so a cwd outside that root
    # cannot be mapped and new_session should refuse.
    with pytest.raises(ValueError):
        await agent.new_session(cwd="/elsewhere")


# ---- S 2: load_session returns None for missing/non-acp ---------------------


@pytest.mark.asyncio
async def test_load_session_returns_none_for_missing(agent):
    response = await agent.load_session(cwd="/host", session_id="missing")
    assert response is None


@pytest.mark.asyncio
async def test_load_session_returns_none_for_non_acp_session(agent, services):
    await services.memory_store.create_session(
        ConversationSession(id="api-1", title="A", source="api")
    )
    response = await agent.load_session(cwd="/host", session_id="api-1")
    assert response is None


@pytest.mark.asyncio
async def test_load_session_replays_history_for_acp_session(agent, services, conn, settings):
    agent.on_connect(conn)
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id
    await services.memory_store.append_message(
        sid, ConversationMessage(role="user", content="past user text")
    )
    conn.updates.clear()

    response = await agent.load_session(cwd=str(host_root), session_id=sid)

    assert response is not None
    # History replay should have emitted at least one update
    assert len(conn.updates) >= 1


# ---- S 5: prompt refuses unknown session ------------------------------------


@pytest.mark.asyncio
async def test_prompt_with_unknown_session_returns_refusal(agent, conn):
    agent.on_connect(conn)
    response = await agent.prompt(
        prompt=[_text_block("hi")], session_id="never-existed"
    )
    assert response.stop_reason == "refusal"


# ---- S 3, S 6: prompt with existing acp session streams events ---------------


@pytest.mark.asyncio
async def test_prompt_streams_events_and_returns_end_turn(agent, services, conn, settings):
    agent.on_connect(conn)
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.prompt(
        prompt=[_text_block("hello world")], session_id=sid
    )

    assert response.stop_reason == "end_turn"
    # chat_service should have been called with the user text
    assert len(services.gateway_service.calls) == 1
    gateway_call = services.gateway_service.calls[0]
    assert gateway_call["event"].session_key.source_value == "acp"
    assert gateway_call["event"].session_key.platform_session_id == sid
    assert services.gateway_registry.active[("acp", sid, "")] == sid
    assert len(services.chat_service.inputs) == 1
    chat_input = services.chat_service.inputs[0]
    assert chat_input.messages == [{"role": "user", "content": "hello world"}]
    assert chat_input.stream is True
    assert chat_input.session_id == sid
    assert chat_input.trusted_metadata["agent_context"] == "primary"
    assert chat_input.trusted_metadata["acp.cwd"]
    # approval_decider wired (Option 1)
    assert chat_input.approval_decider is agent._permission_bridge
    # allowed_confirm_tools_override defaults to empty dict (session has none)
    assert chat_input.allowed_confirm_tools_override == {}

    # Only AgentMessageChunk updates emitted during prompt; UserMessageChunk is
    # NOT emitted here because VsCode ACP clients optimistically render the
    # prompt text on send -- a server-side UserMessageChunk would duplicate it.
    # UserMessageChunk is reserved for replay_history (session/load).
    update_types = [getattr(u, "session_update", None) for _, u in conn.updates]
    assert "user_message_chunk" not in update_types
    assert "agent_message_chunk" in update_types


@pytest.mark.asyncio
async def test_prompt_with_text_and_image_passes_images_to_gateway(agent, services, conn, settings):
    agent.on_connect(conn)
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.prompt(
        prompt=[_text_block("看这张图"), _image_block(data="aGVsbG8=", mime_type="image/png")],
        session_id=sid,
    )

    assert response.stop_reason == "end_turn"
    assert len(services.gateway_service.calls) == 1
    gateway_event = services.gateway_service.calls[0]["event"]
    assert gateway_event.text == "看这张图"
    assert len(gateway_event.images) == 1
    assert gateway_event.images[0] == "data:image/png;base64,aGVsbG8="


@pytest.mark.asyncio
async def test_prompt_with_image_only_not_treated_as_empty(agent, services, conn, settings):
    agent.on_connect(conn)
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.prompt(
        prompt=[_image_block(data="aGVsbG8=", mime_type="image/png")],
        session_id=sid,
    )

    assert response.stop_reason == "end_turn"
    assert len(services.gateway_service.calls) == 1
    gateway_event = services.gateway_service.calls[0]["event"]
    assert gateway_event.text == ""
    assert len(gateway_event.images) == 1
    assert gateway_event.images[0] == "data:image/png;base64,aGVsbG8="


# ---- S 4: concurrent prompt on same session returns refusal -----------------


@pytest.mark.asyncio
async def test_concurrent_prompt_returns_refusal(agent, services, conn, settings):
    agent.on_connect(conn)
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    gate = asyncio.Event()
    services.chat_service.set_gate(gate)

    first_task = asyncio.create_task(
        agent.prompt(prompt=[_text_block("first")], session_id=sid)
    )
    # Yield to let first_task acquire the lock
    await asyncio.sleep(0.05)

    second_resp = await agent.prompt(
        prompt=[_text_block("second")], session_id=sid
    )
    assert second_resp.stop_reason == "refusal"

    # Release the first prompt
    gate.set()
    first_resp = await asyncio.wait_for(first_task, timeout=2.0)
    assert first_resp.stop_reason == "end_turn"


# ---- S 7: set_session_model only updates metadata ---------------------------


@pytest.mark.asyncio
async def test_set_session_model_updates_metadata_only(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    await agent.set_session_model(model_id="claude-opus-4", session_id=sid)

    stored = await services.memory_store.get_session(sid)
    assert stored is not None
    assert stored.acp_metadata["model"] == "claude-opus-4"
    # ProviderService NOT called
    assert services.provider_service.activate_calls == []
    assert services.provider_service.swap_calls == []


# ---- S 8: set_session_mode validates ----------------------------------------


@pytest.mark.asyncio
async def test_set_session_mode_default_updates_metadata(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.set_session_mode(mode_id="default", session_id=sid)
    assert response is not None
    stored = await services.memory_store.get_session(sid)
    assert stored.acp_metadata["mode"] == "default"


@pytest.mark.asyncio
async def test_set_session_mode_safe_only_updates_metadata(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.set_session_mode(mode_id="safe_only", session_id=sid)
    assert response is not None
    stored = await services.memory_store.get_session(sid)
    assert stored.acp_metadata["mode"] == "safe_only"


@pytest.mark.asyncio
async def test_set_session_mode_unknown_returns_none(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.set_session_mode(mode_id="unknown", session_id=sid)
    assert response is None
    # mode unchanged
    stored = await services.memory_store.get_session(sid)
    assert stored.acp_metadata["mode"] == "default"


@pytest.mark.asyncio
async def test_prompt_with_safe_only_mode_sets_tool_exposure_policy(agent, services, conn, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id
    await agent.set_session_mode(mode_id="safe_only", session_id=sid)
    agent.on_connect(conn)

    await agent.prompt(prompt=[_text_block("hi")], session_id=sid)

    chat_input = services.chat_service.inputs[0]
    assert chat_input.options["tool_exposure_policy"] == "safe_only"


# ---- S 9: set_config_option writes metadata ---------------------------------


@pytest.mark.asyncio
async def test_set_config_option_writes_metadata(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.set_config_option(
        config_id="theme", session_id=sid, value="dark"
    )
    assert response is not None
    stored = await services.memory_store.get_session(sid)
    assert stored.acp_metadata["config_options"] == {"theme": "dark"}


@pytest.mark.asyncio
async def test_set_config_option_preserves_existing_options(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id
    await agent.set_config_option(config_id="a", session_id=sid, value="1")
    await agent.set_config_option(config_id="b", session_id=sid, value=True)

    stored = await services.memory_store.get_session(sid)
    assert stored.acp_metadata["config_options"] == {"a": "1", "b": True}


# ---- S 10: cancel calls graph_runner.interrupt ------------------------------


@pytest.mark.asyncio
async def test_cancel_calls_graph_runner_interrupt(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    await agent.cancel(session_id=sid)

    assert services.chat_service.graph_runner.interrupt_calls == [sid]


# ---- close_session releases lock -------------------------------------------


@pytest.mark.asyncio
async def test_close_session_releases_lock_and_preserves_history(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id
    assert sid in agent._session_locks

    await agent.close_session(session_id=sid)

    assert sid not in agent._session_locks
    # History preserved (close does not delete)
    stored = await services.memory_store.get_session(sid)
    assert stored is not None
    assert stored.source == "acp"


# ---- fork_session ----------------------------------------------------------


@pytest.mark.asyncio
async def test_fork_session_creates_new_id(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.fork_session(cwd=str(host_root), session_id=sid)

    assert response.session_id != sid
    assert response.session_id.startswith("acp-")
    forked = await services.memory_store.get_session(response.session_id)
    assert forked is not None
    assert forked.source == "acp"


# ---- list_sessions ---------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_returns_only_acp(agent, services, settings):
    host_root = settings.acp_host_workspace_root
    await agent.new_session(cwd=str(host_root / "a"))
    await agent.new_session(cwd=str(host_root / "b"))
    await services.memory_store.create_session(
        ConversationSession(id="api-1", title="A", source="api")
    )

    response = await agent.list_sessions()

    assert response.next_cursor is None
    api_present = any(s.session_id == "api-1" for s in response.sessions)
    assert not api_present
    assert len(response.sessions) == 2


# ---- approval override propagation (Option 1) ------------------------------


@pytest.mark.asyncio
async def test_prompt_propagates_allowed_confirm_tools_override(agent, services, conn, settings):
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id
    # Manually seed an allow_session entry
    stored = await services.memory_store.get_session(sid)
    metadata = dict(stored.acp_metadata or {})
    metadata["allowed_confirm_tools"] = {"manage_schedule": "session"}
    await services.memory_store.update_session_acp_metadata(sid, metadata)

    agent.on_connect(conn)
    await agent.prompt(prompt=[_text_block("go")], session_id=sid)

    chat_input = services.chat_service.inputs[0]
    assert chat_input.allowed_confirm_tools_override == {"manage_schedule": "session"}


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["session", ["bad"]])
async def test_prompt_treats_malformed_confirm_grant_root_as_empty(
    agent,
    services,
    conn,
    settings,
    malformed,
):
    sid = (await agent.new_session(
        cwd=str(settings.acp_host_workspace_root / "project")
    )).session_id
    session = await services.memory_store.get_session(sid)
    metadata = dict(session.acp_metadata or {})
    metadata["allowed_confirm_tools"] = malformed
    await services.memory_store.update_session_acp_metadata(sid, metadata)
    agent.on_connect(conn)

    await agent.prompt(prompt=[_text_block("go")], session_id=sid)

    assert services.chat_service.inputs[0].allowed_confirm_tools_override == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", ["session", ["bad"]])
async def test_allow_session_repairs_malformed_confirm_grant_root(
    agent,
    services,
    conn,
    settings,
    malformed,
):
    sid = (await agent.new_session(
        cwd=str(settings.acp_host_workspace_root / "project")
    )).session_id
    session = await services.memory_store.get_session(sid)
    metadata = dict(session.acp_metadata or {})
    metadata["allowed_confirm_tools"] = malformed
    await services.memory_store.update_session_acp_metadata(sid, metadata)
    conn.permission_responses["next"] = RequestPermissionResponse(
        outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
    )
    agent.on_connect(conn)

    decision = await agent._permission_bridge.request(ApprovalRequest(
        session_id=sid,
        tool_call_id="call-1",
        tool_name="manage_schedule",
        arguments={"action": "list"},
        description="Manage schedules",
        risk_level=RiskLevel.CONFIRM,
    ))

    stored = await services.memory_store.get_session(sid)
    assert decision.allowed is True
    assert decision.scope == "session"
    assert stored.acp_metadata["allowed_confirm_tools"] == {
        "manage_schedule": "session"
    }


@pytest.mark.asyncio
async def test_allow_session_persistence_is_loaded_by_the_next_prompt(agent, services, conn, settings):
    host_root = settings.acp_host_workspace_root
    sid = (await agent.new_session(cwd=str(host_root / "project"))).session_id
    conn.permission_responses["next"] = RequestPermissionResponse(
        outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
    )
    agent.on_connect(conn)

    decision = await agent._permission_bridge.request(ApprovalRequest(
        session_id=sid,
        tool_call_id="call-1",
        tool_name="manage_schedule",
        arguments={"action": "list"},
        description="Manage schedules",
        risk_level=RiskLevel.CONFIRM,
    ))
    await agent.prompt(prompt=[_text_block("go")], session_id=sid)

    assert decision.allowed is True
    assert decision.scope == "session"
    assert services.chat_service.inputs[0].allowed_confirm_tools_override == {
        "manage_schedule": "session"
    }


@pytest.mark.asyncio
async def test_failed_allow_session_persistence_is_not_loaded_by_the_next_prompt(
    agent, services, conn, settings, monkeypatch
):
    host_root = settings.acp_host_workspace_root
    sid = (await agent.new_session(cwd=str(host_root / "project"))).session_id
    conn.permission_responses["next"] = RequestPermissionResponse(
        outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
    )
    original_update = services.memory_store.update_session_acp_metadata

    async def fail_update(session_id, metadata):
        raise RuntimeError("storage offline")

    monkeypatch.setattr(
        services.memory_store,
        "update_session_acp_metadata",
        fail_update,
    )
    agent.on_connect(conn)
    decision = await agent._permission_bridge.request(ApprovalRequest(
        session_id=sid,
        tool_call_id="call-1",
        tool_name="manage_schedule",
        arguments={"action": "list"},
        description="Manage schedules",
        risk_level=RiskLevel.CONFIRM,
    ))
    monkeypatch.setattr(
        services.memory_store,
        "update_session_acp_metadata",
        original_update,
    )

    await agent.prompt(prompt=[_text_block("go")], session_id=sid)

    assert decision.allowed is True
    assert decision.scope == "session"
    assert services.chat_service.inputs[0].allowed_confirm_tools_override == {}


class _ToolLoopProvider:
    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name

    async def list_models(self):
        return [ModelInfo("test-model", "test-model", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        if messages and messages[-1].get("role") == "tool":
            return LLMResult(
                message={"role": "assistant", "content": "done"},
                finish_reason="stop",
            )
        return LLMResult(
            message={
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"call-{len(messages)}",
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "arguments": '{"action":"list"}',
                    },
                }],
            },
            finish_reason="tool_calls",
        )


class _ToolLoopExecutor:
    def __init__(self) -> None:
        self.calls: list[ToolCallRequest] = []

    async def execute(self, request, context=None):
        self.calls.append(request)
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            {"ok": True},
        )


def _install_real_chat_service(services, tool_name: str, managed: bool):
    executor = _ToolLoopExecutor()
    runner = AgentGraphRunner(
        _ToolLoopProvider(tool_name),
        ToolService(executor, [
            ToolDefinition(
                name=tool_name,
                description="Confirm action",
                input_schema={"type": "object"},
                risk_level=RiskLevel.CONFIRM,
                managed=managed,
            )
        ]),
        services.memory_store,
        HeuristicSummarizer(),
        iteration_limit=4,
    )
    chat_service = ChatCompletionService(
        services.memory_store,
        runner,
        services.session_service,
    )
    services.chat_service = chat_service
    services.gateway_service = FakeGatewayService(chat_service)
    return executor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "managed"),
    [("ordinary_confirm", False), ("managed_confirm", True)],
)
async def test_persisted_acp_session_grant_executes_without_reapproval(
    agent, services, conn, settings, tool_name, managed
):
    executor = _install_real_chat_service(services, tool_name, managed)
    sid = (await agent.new_session(
        cwd=str(settings.acp_host_workspace_root / "project")
    )).session_id
    session = await services.memory_store.get_session(sid)
    metadata = dict(session.acp_metadata or {})
    metadata["allowed_confirm_tools"] = {tool_name: "session"}
    await services.memory_store.update_session_acp_metadata(sid, metadata)
    agent.on_connect(conn)

    await agent.prompt(prompt=[_text_block("run")], session_id=sid)

    assert [call.name for call in executor.calls] == [tool_name]
    assert conn.permission_calls == []


@pytest.mark.asyncio
async def test_failed_acp_persistence_executes_current_call_and_reapproves_next_round(
    agent, services, conn, settings, monkeypatch
):
    tool_name = "managed_confirm"
    executor = _install_real_chat_service(services, tool_name, managed=True)
    sid = (await agent.new_session(
        cwd=str(settings.acp_host_workspace_root / "project")
    )).session_id
    conn.permission_responses["next"] = RequestPermissionResponse(
        outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
    )
    original_update = services.memory_store.update_session_acp_metadata

    async def fail_update(session_id, metadata):
        raise RuntimeError("storage offline")

    monkeypatch.setattr(
        services.memory_store,
        "update_session_acp_metadata",
        fail_update,
    )
    agent.on_connect(conn)
    await agent.prompt(prompt=[_text_block("first")], session_id=sid)
    monkeypatch.setattr(
        services.memory_store,
        "update_session_acp_metadata",
        original_update,
    )

    await agent.prompt(prompt=[_text_block("second")], session_id=sid)

    assert [call.name for call in executor.calls] == [tool_name, tool_name]
    assert len(conn.permission_calls) == 2


# ---- error path maps to refusal -------------------------------------------


@pytest.mark.asyncio
async def test_prompt_error_event_maps_to_refusal(agent, services, conn, settings):
    services.chat_service.set_events([
        ChatEvent(ChatEventType.MESSAGE_START),
        ChatEvent(ChatEventType.ERROR, error="boom", finish_reason="error"),
        ChatEvent(ChatEventType.DONE),
    ])
    agent.on_connect(conn)
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.prompt(
        prompt=[_text_block("hi")], session_id=sid
    )
    assert response.stop_reason == "refusal"


# ---------------------------------------------------------------------------
# T11: realtime contract -- session_id passing + DEFAULT approval tool surface
# ---------------------------------------------------------------------------


def _default_exposed_tool_names_with_approval_tools() -> set[str]:
    """Construct a ToolService wired with user_task_approval_tool_definitions
    (mirrors T8 wiring in app/main.py) and return DEFAULT-exposed tool names."""
    from app.application.task_tools import user_task_approval_tool_definitions
    from app.application.tool_service import ToolService as _ToolService
    from app.domain.tool_policy import ToolExposurePolicy

    class _UnusedExecutor:
        async def execute(self, request, context=None):
            raise RuntimeError("executor not used by list_openai_tools")

    tool_service = _ToolService(executor=_UnusedExecutor(), definitions=[])
    tool_service.set_dynamic_definitions(
        "user_task", user_task_approval_tool_definitions()
    )
    return {
        t["function"]["name"]
        for t in tool_service.list_openai_tools(ToolExposurePolicy.DEFAULT, None)
    }


@pytest.mark.asyncio
async def test_realtime_tool_context_passes_session_id_and_exposes_approval_tools(
    agent, services, conn, settings
):
    """ACP realtime contract (T11).

    NAgentACPAgent.prompt -> GatewayService.handle_message_stream must pass the
    ACP session_id through to ChatCompletionService, carry realtime indicators
    (agent_context=primary, execution_context_mode=realtime, tool_exposure_policy
    != safe_only), and the shared ToolService DEFAULT surface must contain
    approve_task / reject_task / revise_task (wired by T8).
    """
    agent.on_connect(conn)
    host_root = settings.acp_host_workspace_root
    new_resp = await agent.new_session(cwd=str(host_root / "project"))
    sid = new_resp.session_id

    response = await agent.prompt(prompt=[_text_block("hello")], session_id=sid)
    assert response.stop_reason == "end_turn"

    # ChatCompletionInput captured by FakeChatService
    assert services.chat_service.inputs, "ChatCompletionInput was not captured"
    chat_input = services.chat_service.inputs[0]

    # session_id passes through unchanged (ACP session_id == platform_session_id)
    assert chat_input.session_id == sid

    # realtime indicators on trusted_metadata + options
    assert chat_input.trusted_metadata.get("agent_context") == "primary"
    assert chat_input.options.get("execution_context_mode") == "realtime"
    # DEFAULT exposure (not SAFE_ONLY) for default mode
    assert chat_input.options.get("tool_exposure_policy") != "safe_only"

    # Shared ToolService DEFAULT surface contains the three approval tools
    tool_names = _default_exposed_tool_names_with_approval_tools()
    assert {"approve_task", "reject_task", "revise_task"}.issubset(tool_names)
