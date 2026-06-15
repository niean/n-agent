import pytest

from app.domain.gateway import GatewaySessionKey, InteractionSourceType
from app.infrastructure.registry.sqlite_gateway_registry import SQLiteGatewaySessionRegistry


@pytest.mark.asyncio
async def test_gateway_registry_creates_and_switches_active_session(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    key = GatewaySessionKey(InteractionSourceType.CLI, "local", display_name="Local CLI")

    first = await registry.create_session_link(key, "session-1")
    second = await registry.create_session_link(key, "session-2")

    active = await registry.get_active_session(key)
    assert active is not None
    assert active.session_id == second.session_id
    assert active.display_name == "Local CLI"

    switched = await registry.set_active_session(key, first.session_id)
    links = await registry.list_session_links(key)

    assert switched.session_id == first.session_id
    assert [link.session_id for link in links] == ["session-1", "session-2"]


@pytest.mark.asyncio
async def test_gateway_registry_returns_none_for_missing_active_session(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    key = GatewaySessionKey(InteractionSourceType.FEISHU, "chat-1")

    assert await registry.get_active_session(key) is None
    assert await registry.list_session_links(key) == []


@pytest.mark.asyncio
async def test_gateway_registry_marks_events_once(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")

    assert await registry.mark_event_processed(InteractionSourceType.FEISHU, "event-1", "message-1") is True
    assert await registry.mark_event_processed(InteractionSourceType.FEISHU, "event-1", "message-2") is False
    assert await registry.mark_event_processed(InteractionSourceType.FEISHU, "event-2", "message-1") is False


@pytest.mark.asyncio
async def test_gateway_registry_deletes_session_link_and_clears_active(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    key = GatewaySessionKey(InteractionSourceType.CLI, "local")
    await registry.create_session_link(key, "session-1")

    await registry.delete_session_link("session-1")

    assert await registry.get_active_session(key) is None
    assert await registry.list_session_links(key) == []
