from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.domain.gateway import GatewaySessionKey
from app.domain.platform import Platform
from app.domain.tool import ApprovalDecision, ApprovalRequest, RiskLevel
from app.interfaces.feishu_tool_approval import (
    FeishuToolApprovalBridge,
    FeishuToolApprovalError,
)


def _request(
    *,
    session_id: str = "s1",
    tool_name: str = "mcp_site_probe",
    arguments: dict[str, Any] | None = None,
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        tool_call_id="tc-1",
        tool_name=tool_name,
        arguments=arguments or {"site": "demo"},
        description="Probe an MCP site",
        risk_level=RiskLevel.CONFIRM,
    )


def _key(*, thread_id: str = "") -> GatewaySessionKey:
    return GatewaySessionKey(
        Platform.FEISHU,
        "oc_1",
        thread_id=thread_id,
        display_name="ou_1",
    )


def _decider(
    bridge: FeishuToolApprovalBridge,
    sent: asyncio.Queue[dict[str, Any]],
    *,
    key: GatewaySessionKey | None = None,
    actor_id: str = "ou_1",
    receive_id: str = "oc_1",
    receive_id_type: str = "chat_id",
    session_grant_updater=None,
    session_grant_checker=None,
):
    async def sender(confirmation: dict[str, Any]) -> str:
        await sent.put(confirmation)
        return "card-msg-1"

    return bridge.create_decider(
        key or _key(),
        actor_id,
        receive_id,
        receive_id_type,
        sender,
        session_grant_updater=session_grant_updater,
        session_grant_checker=session_grant_checker,
    )


@pytest.mark.asyncio
async def test_allow_once_claim_completes_waiting_decider() -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(_decider(bridge, sent)(_request()))

    confirmation = await sent.get()
    claim = bridge.claim(
        confirmation["id"],
        "once",
        verified_chat_id="oc_1",
        verified_card_message_id="card-msg-1",
        actor_id="ou_1",
    )
    bridge.complete(claim)

    assert await task == ApprovalDecision(allowed=True, scope="once")
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_trust_session_calls_application_grant_updater() -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    grants: set[tuple[str, str, str]] = set()
    first = asyncio.create_task(
        _decider(
            bridge,
            sent,
            session_grant_updater=lambda session_id, actor_id, tool_name: grants.add(
                (session_id, actor_id, tool_name)
            ),
            session_grant_checker=lambda session_id, actor_id, tool_name: (
                session_id,
                actor_id,
                tool_name,
            ) in grants,
        )(_request())
    )
    confirmation = await sent.get()
    claim = bridge.claim(
        confirmation["id"],
        "trust_session",
        verified_chat_id="oc_1",
        verified_card_message_id="card-msg-1",
        actor_id="ou_1",
    )
    bridge.complete(claim)
    assert await first == ApprovalDecision(allowed=True, scope="session")
    assert grants == {("s1", "ou_1", "mcp_site_probe")}
    second = await _decider(
        bridge,
        sent,
        session_grant_checker=lambda session_id, actor_id, tool_name: (
            session_id,
            actor_id,
            tool_name,
        ) in grants,
    )(_request())
    assert second == ApprovalDecision(allowed=True, scope="session")
    assert sent.empty()
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_session_grant_update_failure_downgrades_to_once(caplog) -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def fail_update(session_id: str, actor_id: str, tool_name: str) -> None:
        raise RuntimeError("store failed")

    task = asyncio.create_task(
        _decider(bridge, sent, session_grant_updater=fail_update)(_request())
    )
    confirmation = await sent.get()
    claim = bridge.claim(
        confirmation["id"],
        "trust_session",
        verified_chat_id="oc_1",
        verified_card_message_id="card-msg-1",
        actor_id="ou_1",
    )

    bridge.complete(claim)

    assert await task == ApprovalDecision(True, "once", "session_grant_failed")
    assert "session tool grant update failed" in caplog.text


@pytest.mark.asyncio
async def test_cancel_returns_denied_decision() -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(_decider(bridge, sent)(_request()))
    confirmation = await sent.get()

    claim = bridge.claim(
        confirmation["id"],
        "cancel",
        verified_chat_id="oc_1",
        verified_card_message_id="card-msg-1",
        actor_id="ou_1",
    )
    bridge.complete(claim)

    assert await task == ApprovalDecision(
        allowed=False,
        scope="deny",
        reason="cancelled",
    )


@pytest.mark.asyncio
async def test_invalid_actor_chat_and_card_identity_do_not_consume_pending() -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(
        _decider(bridge, sent, key=_key(thread_id="thread-1"))(_request())
    )
    confirmation = await sent.get()

    for kwargs in (
        {"verified_chat_id": "oc_1", "verified_card_message_id": "card-msg-1", "actor_id": "ou_2"},
        {"verified_chat_id": "oc_2", "verified_card_message_id": "card-msg-1", "actor_id": "ou_1"},
        {"verified_chat_id": "oc_1", "verified_card_message_id": "other-card", "actor_id": "ou_1"},
    ):
        with pytest.raises(FeishuToolApprovalError):
            bridge.claim(confirmation["id"], "once", **kwargs)
        assert bridge.pending_count == 1
        assert bridge.is_claimed(confirmation["id"]) is False

    claim = bridge.claim(
        confirmation["id"],
        "once",
        verified_chat_id="oc_1",
        verified_card_message_id="card-msg-1",
        actor_id="ou_1",
    )
    bridge.complete(claim)
    assert (await task).allowed is True


@pytest.mark.asyncio
async def test_open_id_fallback_uses_verified_operator_identity() -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    key = GatewaySessionKey(Platform.FEISHU, "ou_1", display_name="ou_1")
    task = asyncio.create_task(
        _decider(
            bridge,
            sent,
            key=key,
            receive_id="ou_1",
            receive_id_type="open_id",
        )(_request())
    )
    confirmation = await sent.get()

    claim = bridge.claim(
        confirmation["id"],
        "once",
        verified_chat_id="oc_verified",
        verified_card_message_id="card-msg-1",
        actor_id="ou_1",
    )
    bridge.complete(claim)
    assert (await task).allowed is True


@pytest.mark.asyncio
async def test_timeout_sender_error_and_task_cancel_fail_closed_and_clean_pending() -> None:
    bridge = FeishuToolApprovalBridge(timeout_seconds=0.01)
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    decision = await _decider(bridge, sent)(_request())
    assert decision == ApprovalDecision(False, "deny", "timeout")
    assert bridge.pending_count == 0

    async def fail_sender(confirmation: dict[str, Any]) -> str:
        raise RuntimeError("card failed")

    failing = bridge.create_decider(_key(), "ou_1", "oc_1", "chat_id", fail_sender)
    decision = await failing(_request())
    assert decision == ApprovalDecision(False, "deny", "card_send_failed")
    assert bridge.pending_count == 0

    pending = asyncio.create_task(_decider(bridge, sent)(_request()))
    await sent.get()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_duplicate_claim_and_complete_cannot_authorize_twice() -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(_decider(bridge, sent)(_request()))
    confirmation = await sent.get()
    claim = bridge.claim(
        confirmation["id"],
        "once",
        verified_chat_id="oc_1",
        verified_card_message_id="card-msg-1",
        actor_id="ou_1",
    )
    with pytest.raises(FeishuToolApprovalError):
        bridge.claim(
            confirmation["id"],
            "once",
            verified_chat_id="oc_1",
            verified_card_message_id="card-msg-1",
            actor_id="ou_1",
        )
    bridge.complete(claim)
    bridge.complete(claim)
    assert (await task).allowed is True
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_confirmation_metadata_redacts_secrets_and_limits_arguments_summary() -> None:
    bridge = FeishuToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.create_task(
        _decider(bridge, sent)(
            _request(
                arguments={
                    "api_key": "secret-value",
                    "nested": {"Access_Token": "token-value"},
                    "headers": [{"X-API-Key": "hyphen-secret"}],
                    "private-key": "private-secret",
                    "payload": "x" * 1200,
                }
            )
        )
    )
    confirmation = await sent.get()

    assert confirmation["kind"] == "tool_policy"
    assert confirmation["tool_name"] == "mcp_site_probe"
    assert "secret-value" not in confirmation["arguments_summary"]
    assert "token-value" not in confirmation["arguments_summary"]
    assert "hyphen-secret" not in confirmation["arguments_summary"]
    assert "private-secret" not in confirmation["arguments_summary"]
    assert "***" in confirmation["arguments_summary"]
    assert len(confirmation["arguments_summary"]) <= 800

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
