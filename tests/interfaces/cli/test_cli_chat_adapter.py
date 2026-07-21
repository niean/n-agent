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
async def test_realtime_tool_context_passes_session_id_and_exposes_approval_tools(
    fake_gateway_service,
):
    """CLI Chat realtime contract (T11).

    CliChatAdapter.send_stream -> GatewayService.handle_message_stream must
    pass a resolved session_id through to ChatCompletionService, carry REALTIME
    execution_mode on ingress_facts, and the shared ToolService DEFAULT surface
    must contain approve_task / reject_task / revise_task (wired by T8).
    """
    from app.domain.policy import ExecutionMode

    client = CliChatAdapter(fake_gateway_service)
    events = [e async for e in client.send_stream("hello", "conv-1")]
    assert events

    # The conftest FakeServices wraps a real GatewayService, so the underlying
    # _FakeChatService captures the ChatCompletionInput sent to chat service.
    captured = fake_gateway_service.chat_service.requests
    assert captured, "ChatCompletionInput was not captured"
    request = captured[-1]

    # session_id passes through (resolved by gateway registry, non-empty)
    assert request.session_id
    assert request.session_id is not None

    # realtime execution mode on ingress_facts (authoritative claim)
    assert request.ingress_facts is not None
    assert request.ingress_facts.execution_mode is ExecutionMode.REALTIME
    # trusted_metadata must not downgrade to unattended
    assert request.trusted_metadata.get("execution_mode") != "unattended"

    # Shared ToolService DEFAULT surface contains the three approval tools
    tool_names = _default_exposed_tool_names_with_approval_tools()
    assert {"approve_task", "reject_task", "revise_task"}.issubset(tool_names)
