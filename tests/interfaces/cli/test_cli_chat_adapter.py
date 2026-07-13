from __future__ import annotations

from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_send_stream_passes_approval_decider(fake_gateway_service):
    client = CliChatAdapter(fake_gateway_service)
    sentinel = lambda req: None  # noqa: E731
    events = [e async for e in client.send_stream("hi", "conv-1", approval_decider=sentinel)]
    assert events
    assert fake_gateway_service.last_stream_kwargs.get("approval_decider") is sentinel


@pytest.mark.asyncio
async def test_send_stream_without_decider_passes_none(fake_gateway_service):
    client = CliChatAdapter(fake_gateway_service)
    _ = [e async for e in client.send_stream("hi", "conv-1")]
    assert fake_gateway_service.last_stream_kwargs.get("approval_decider") is None
    assert "allowed_confirm_tools_override" not in fake_gateway_service.last_stream_kwargs


@pytest.mark.asyncio
async def test_grant_proxies_delegate_to_gateway_service(fake_gateway_service):
    client = CliChatAdapter(fake_gateway_service)
    assert client.is_tool_granted("s1", "a1", "t1") is False
    client.grant_tool_for_session("s1", "a1", "t1")
    assert client.is_tool_granted("s1", "a1", "t1") is True


@pytest.mark.asyncio
async def test_send_non_stream_does_not_inject_decider(fake_gateway_service):
    """Single-message path (send) has no decider parameter and must stay fail-closed.
    Verifies _send_once (used by --message and stdin pipe) cannot inject a decider."""
    client = CliChatAdapter(fake_gateway_service)
    resp = await client.send("hello", "conv-1")
    assert resp is not None
    # handle_message (non-stream) has no approval_decider kwarg;
    # confirming the fake recorded the event but no decider was provided
    assert fake_gateway_service.last_event is not None
    assert not hasattr(fake_gateway_service, "last_stream_kwargs") or "approval_decider" not in (
        fake_gateway_service.last_stream_kwargs or {}
    )
