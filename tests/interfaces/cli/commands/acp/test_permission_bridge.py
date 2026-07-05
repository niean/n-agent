"""Tests for ACP permission bridge (T11).

Verifies that :class:`ACPPermissionBridge` constructs an ACP ``ToolCallUpdate``
payload (NOT a ``ToolCallStart``), maps each ``PermissionOption`` outcome back to
the right :class:`ApprovalDecision`, persists ``allow_session`` via the injected
metadata updater, and fails closed on timeout/exception/unknown option.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from acp.schema import (
    AllowedOutcome,
    DeniedOutcome,
    PermissionOption,
    RequestPermissionResponse,
)

from app.domain.tool import ApprovalRequest, RiskLevel
from app.interfaces.cli.commands.acp.permission_bridge import ACPPermissionBridge


class FakeConn:
    """Records request_permission calls; returns a preconfigured response."""

    def __init__(self, response: RequestPermissionResponse | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: Any,
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        self.calls.append(
            {
                "options": options,
                "session_id": session_id,
                "tool_call": tool_call,
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _make_request() -> ApprovalRequest:
    return ApprovalRequest(
        session_id="s1",
        tool_call_id="tc-1",
        tool_name="manage_schedule",
        arguments={"x": 1},
        description="Manage schedules",
        risk_level=RiskLevel.CONFIRM,
    )


def _is_tool_call_update(tool_call: Any) -> bool:
    return type(tool_call).__name__ == "ToolCallProgress"


@pytest.mark.asyncio
async def test_allow_once_returns_once_decision_and_skips_metadata_updater():
    calls: list[tuple[str, str, str]] = []

    def updater(sid: str, name: str, scope: str) -> None:
        calls.append((sid, name, scope))

    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn, metadata_updater=updater)

    decision = await bridge.request(_make_request())

    assert decision.allowed is True
    assert decision.scope == "once"
    assert calls == []
    assert _is_tool_call_update(conn.calls[0]["tool_call"])


@pytest.mark.asyncio
async def test_allow_session_returns_session_decision_and_calls_metadata_updater():
    calls: list[tuple[str, str, str]] = []

    def updater(sid: str, name: str, scope: str) -> None:
        calls.append((sid, name, scope))

    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn, metadata_updater=updater)

    decision = await bridge.request(_make_request())

    assert decision.allowed is True
    assert decision.scope == "session"
    assert calls == [("s1", "manage_schedule", "session")]


@pytest.mark.asyncio
async def test_allow_session_supports_async_metadata_updater():
    calls: list[tuple[str, str, str]] = []

    async def updater(sid: str, name: str, scope: str) -> None:
        calls.append((sid, name, scope))

    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn, metadata_updater=updater)

    decision = await bridge.request(_make_request())

    assert decision.allowed is True
    assert decision.scope == "session"
    assert calls == [("s1", "manage_schedule", "session")]


@pytest.mark.asyncio
async def test_allow_session_without_metadata_updater_still_allows():
    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn, metadata_updater=None)

    decision = await bridge.request(_make_request())

    assert decision.allowed is True
    assert decision.scope == "session"


@pytest.mark.asyncio
async def test_reject_once_returns_once_denied_with_reason():
    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="reject_once", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn)

    decision = await bridge.request(_make_request())

    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "rejected"


@pytest.mark.asyncio
async def test_denied_outcome_cancelled_returns_deny():
    conn = FakeConn(RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled")))
    bridge = ACPPermissionBridge(conn)

    decision = await bridge.request(_make_request())

    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "cancelled"


@pytest.mark.asyncio
async def test_unknown_option_id_returns_deny():
    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="something_else", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn)

    decision = await bridge.request(_make_request())

    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "unknown option"


@pytest.mark.asyncio
async def test_timeout_returns_deny_with_timeout_reason():
    class SlowConn:
        async def request_permission(self, **kwargs: Any) -> RequestPermissionResponse:
            await asyncio.sleep(5.0)
            raise AssertionError("should not reach")

    bridge = ACPPermissionBridge(SlowConn(), timeout_seconds=0.05)

    decision = await bridge.request(_make_request())

    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "timeout"


@pytest.mark.asyncio
async def test_exception_returns_deny_with_reason():
    conn = FakeConn(RuntimeError("network down"))
    bridge = ACPPermissionBridge(conn)

    decision = await bridge.request(_make_request())

    assert decision.allowed is False
    assert decision.scope == "deny"
    assert "network down" in decision.reason


@pytest.mark.asyncio
async def test_tool_call_payload_is_tool_call_update_not_tool_call_start():
    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn)

    await bridge.request(_make_request())

    tool_call = conn.calls[0]["tool_call"]
    assert type(tool_call).__name__ == "ToolCallProgress"
    assert tool_call.tool_call_id == "tc-1"
    assert tool_call.title == "manage_schedule"
    assert tool_call.status == "in_progress"


@pytest.mark.asyncio
async def test_options_use_stable_ids_and_correct_kinds():
    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn)

    await bridge.request(_make_request())

    options = conn.calls[0]["options"]
    by_id = {opt.option_id: opt for opt in options}
    assert set(by_id.keys()) == {"allow_once", "allow_session", "reject_once"}
    assert by_id["allow_once"].kind == "allow_once"
    assert by_id["allow_session"].kind == "allow_always"
    assert by_id["reject_once"].kind == "reject_once"


@pytest.mark.asyncio
async def test_session_id_propagated_to_request_permission():
    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn)

    await bridge.request(_make_request())

    assert conn.calls[0]["session_id"] == "s1"


@pytest.mark.asyncio
async def test_allow_session_swallows_metadata_updater_exception():
    def updater(sid: str, name: str, scope: str) -> None:
        raise RuntimeError("storage offline")

    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_session", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn, metadata_updater=updater)

    decision = await bridge.request(_make_request())

    assert decision.allowed is True
    assert decision.scope == "session"


@pytest.mark.asyncio
async def test_call_delegates_to_request():
    # ApprovalDecider port is Callable[[ApprovalRequest], Awaitable[ApprovalDecision]].
    # agent_graph invokes the bridge as ``context.approval_decider(req)`` -- this
    # regression test pins the ``__call__`` delegation so a future refactor that
    # drops ``__call__`` fails fast instead of raising ``object is not callable``
    # at runtime inside a real ACP session.
    conn = FakeConn(
        RequestPermissionResponse(
            outcome=AllowedOutcome(option_id="allow_once", outcome="selected")
        )
    )
    bridge = ACPPermissionBridge(conn)

    raw = bridge(_make_request())
    assert asyncio.iscoroutine(raw)
    decision = await raw

    assert decision.allowed is True
    assert decision.scope == "once"
    assert len(conn.calls) == 1
