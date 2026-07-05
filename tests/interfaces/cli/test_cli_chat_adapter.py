from __future__ import annotations

import pytest

from app.interfaces.cli.cli_chat_adapter import CliChatAdapter


@pytest.mark.asyncio
async def test_send_stream_constructs_interaction_message_with_actor_id(fake_gateway_service):
    svc_holder = fake_gateway_service
    client = CliChatAdapter(svc_holder)
    events = [e async for e in client.send_stream("hello", "conv-1")]
    assert events
    assert svc_holder.last_event is not None
    assert svc_holder.last_event.metadata.get("actor_id") == "cli:conv-1"
    assert svc_holder.last_event.session_key.source_value == "cli"
    assert svc_holder.last_event.session_key.platform is None
    assert svc_holder.last_event.session_key.platform_session_id == "conv-1"


@pytest.mark.asyncio
async def test_send_non_stream_returns_response(fake_gateway_service):
    svc_holder = fake_gateway_service
    client = CliChatAdapter(svc_holder)
    resp = await client.send("hello", "conv-1")
    assert resp is not None


@pytest.mark.asyncio
async def test_confirm_calls_handle_confirmation(fake_gateway_service):
    svc_holder = fake_gateway_service
    client = CliChatAdapter(svc_holder)
    await client.confirm("conf-1", "once", "conv-1")
    assert svc_holder.last_confirmation_id == "conf-1"
