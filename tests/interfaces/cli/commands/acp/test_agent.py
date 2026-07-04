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
    AuthenticateResponse,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    TextContentBlock,
)

from app.application.events import ChatEvent, ChatEventType
from app.application.session_service import SessionService
from app.config import Settings
from app.domain.session import ConversationMessage, ConversationSession
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.cli.commands.acp.agent import NAgentACPAgent


class FakeConn:
    """Records session_update calls; stands in for acp.Client."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []
        self.permission_responses: dict[str, Any] = {}

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.updates.append((session_id, update))

    async def request_permission(self, **kwargs: Any) -> Any:
        # Not used in these tests; ACPPermissionBridge is covered in T11.
        raise RuntimeError("request_permission not expected in T12 tests")


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

    # UserMessageChunk + AgentMessageChunk updates emitted
    update_types = [getattr(u, "session_update", None) for _, u in conn.updates]
    assert "user_message_chunk" in update_types
    assert "agent_message_chunk" in update_types


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
