from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.application.events import ChatEventType
from app.application.gateway_tool_approval_service import GatewayToolApprovalService
from app.domain.tool import ApprovalDecision, ApprovalRequest, RiskLevel
from app.interfaces.http.dashboard_tool_approval import (
    ClaimResult,
    DashboardToolApprovalBridge,
    _PendingApproval,
    _arguments_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _request(
    *,
    session_id: str = "s1",
    tool_name: str = "mcp_site_probe",
    tool_call_id: str = "tc-1",
    arguments: dict[str, Any] | None = None,
    description: str = "Probe an MCP site",
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        arguments=arguments or {"site": "demo"},
        description=description,
        risk_level=RiskLevel.CONFIRM,
    )


class _FakeClock:
    """Injectable monotonic clock for deterministic timeout tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self._time = start

    def __call__(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds


async def _start_decider(
    bridge: DashboardToolApprovalBridge,
    *,
    session_id: str = "s1",
    actor_id: str = "dashboard",
    request: ApprovalRequest | None = None,
    sender=None,
    session_grant_updater=None,
    session_grant_checker=None,
    session_grant_revoker=None,
) -> tuple[asyncio.Task[ApprovalDecision], dict[str, Any]]:
    """Start a decider task and wait for the sender to deliver metadata.

    Returns the running decider task and the confirmation metadata dict.
    """
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def default_sender(metadata: dict[str, Any]) -> None:
        await sent.put(metadata)

    decider = bridge.create_decider(
        session_id,
        actor_id,
        sender or default_sender,
        session_grant_updater=session_grant_updater,
        session_grant_checker=session_grant_checker,
        session_grant_revoker=session_grant_revoker,
    )
    req = request or _request(session_id=session_id)
    task = asyncio.create_task(decider(req))
    confirmation = await sent.get()
    return task, confirmation


async def _cancel_silently(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Event type
# ---------------------------------------------------------------------------

def test_tool_approval_required_event_type_exists() -> None:
    assert ChatEventType.TOOL_APPROVAL_REQUIRED.value == "tool_approval_required"


# ---------------------------------------------------------------------------
# GatewayToolApprovalService changes
# ---------------------------------------------------------------------------

def test_grant_session_returns_bool_true_on_success() -> None:
    service = GatewayToolApprovalService()
    assert service.grant_session("s1", "dashboard", "tool") is True


def test_grant_session_returns_false_when_any_field_empty() -> None:
    service = GatewayToolApprovalService()
    assert service.grant_session("", "dashboard", "tool") is False
    assert service.grant_session("s1", "", "tool") is False
    assert service.grant_session("s1", "dashboard", "") is False
    assert service.is_granted("s1", "dashboard", "tool") is False


def test_revoke_session_is_idempotent() -> None:
    service = GatewayToolApprovalService()
    service.grant_session("s1", "dashboard", "tool")
    assert service.is_granted("s1", "dashboard", "tool")
    service.revoke_session("s1", "dashboard", "tool")
    assert not service.is_granted("s1", "dashboard", "tool")
    # Idempotent -- no error on missing
    service.revoke_session("s1", "dashboard", "tool")
    assert not service.is_granted("s1", "dashboard", "tool")


def test_revoke_session_only_removes_matching_grant() -> None:
    service = GatewayToolApprovalService()
    service.grant_session("s1", "dashboard", "tool1")
    service.grant_session("s1", "dashboard", "tool2")
    service.grant_session("s2", "dashboard", "tool1")
    service.revoke_session("s1", "dashboard", "tool1")
    assert not service.is_granted("s1", "dashboard", "tool1")
    assert service.is_granted("s1", "dashboard", "tool2")
    assert service.is_granted("s2", "dashboard", "tool1")


# ---------------------------------------------------------------------------
# Approval metadata whitelist
# ---------------------------------------------------------------------------

async def test_approval_metadata_has_exactly_five_whitelist_fields() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    try:
        assert set(confirmation.keys()) == {
            "confirmation_id",
            "tool_name",
            "description",
            "arguments_summary",
            "expires_at",
        }
        assert confirmation["tool_name"] == "mcp_site_probe"
        assert confirmation["description"] == "Probe an MCP site"
        assert confirmation["confirmation_id"].startswith("tool-confirm-")
        assert confirmation["expires_at"].endswith("Z")
        assert confirmation["arguments_summary"]
    finally:
        await _cancel_silently(task)


async def test_approval_metadata_does_not_leak_sensitive_arguments() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(
        bridge,
        request=_request(
            arguments={
                "api_key": "secret-key-value",
                "password": "hunter2",
                "nested": {"Access_Token": "token-value"},
                "headers": [{"X-API-Key": "hyphen-secret"}],
                "private-key": "private-secret",
                "normal": "ok",
            }
        ),
    )
    try:
        summary = confirmation["arguments_summary"]
        assert "secret-key-value" not in summary
        assert "hunter2" not in summary
        assert "token-value" not in summary
        assert "hyphen-secret" not in summary
        assert "private-secret" not in summary
        assert "***" in summary
        assert "ok" in summary
        assert len(summary) <= 800
    finally:
        await _cancel_silently(task)


# ---------------------------------------------------------------------------
# arguments_summary redaction (projection fail-closed)
# ---------------------------------------------------------------------------

def test_arguments_summary_redacts_sensitive_keys_recursively() -> None:
    args = {
        "api_key": "secret-value",
        "nested": {"Access_Token": "token-value"},
        "headers": [{"X-API-Key": "hyphen-secret"}],
        "private-key": "private-secret",
        "payload": "x" * 1200,
        "normal_field": "ok",
    }
    summary = _arguments_summary(args)
    assert "secret-value" not in summary
    assert "token-value" not in summary
    assert "hyphen-secret" not in summary
    assert "private-secret" not in summary
    assert "***" in summary
    assert "ok" in summary
    assert len(summary) <= 800


def test_arguments_summary_depth_limit_fail_closed() -> None:
    deep: dict[str, Any] = {"safe": "ok"}
    for _ in range(20):
        deep = {"nested": deep}
    summary = _arguments_summary(deep)
    assert isinstance(summary, str)
    assert len(summary) <= 800


def test_arguments_summary_cycle_fail_closed() -> None:
    cycle: dict[str, Any] = {}
    cycle["self"] = cycle
    summary = _arguments_summary(cycle)
    # Cycle is detected and the cycled value is replaced with placeholder.
    # The whole summary must not infinitely expand or leak raw structure.
    assert isinstance(summary, str)
    assert "***" in summary
    assert len(summary) <= 800


def test_arguments_summary_unknown_type_fail_closed() -> None:
    class Unknown:
        pass

    args: dict[str, Any] = {"unknown": Unknown(), "safe": "ok"}
    summary = _arguments_summary(args)
    assert "ok" in summary
    assert "***" in summary


def test_arguments_summary_string_length_bounded() -> None:
    args = {"long_string": "x" * 500}
    summary = _arguments_summary(args)
    assert len(summary) <= 800


def test_arguments_summary_collection_length_bounded() -> None:
    args = {"items": list(range(100))}
    summary = _arguments_summary(args)
    assert len(summary) <= 800


def test_arguments_summary_total_serialized_length_bounded() -> None:
    args = {f"key_{i}": f"value_{i}" * 50 for i in range(50)}
    summary = _arguments_summary(args)
    assert len(summary) <= 800


def test_arguments_summary_handles_non_dict() -> None:
    assert _arguments_summary("not a dict") == "***"  # type: ignore[arg-type]


def test_arguments_summary_empty_dict() -> None:
    assert _arguments_summary({}) == "{}"


def test_arguments_summary_never_str_fallback() -> None:
    class Bad:
        def __str__(self) -> str:
            return "LEAKED"

        def __repr__(self) -> str:
            return "LEAKED"

    args = {"bad": Bad(), "safe": "ok"}
    summary = _arguments_summary(args)
    assert "LEAKED" not in summary
    assert "ok" in summary


# ---------------------------------------------------------------------------
# Decider: once allow
# ---------------------------------------------------------------------------

async def test_allow_once_claim_completes_waiting_decider() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    result = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert result.status == "ok"
    assert result.decision == ApprovalDecision(allowed=True, scope="once")
    decision = await task
    assert decision == ApprovalDecision(allowed=True, scope="once")
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Decider: session trust
# ---------------------------------------------------------------------------

async def test_trust_session_calls_grant_updater_and_allows() -> None:
    service = GatewayToolApprovalService()
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(
        bridge,
        session_grant_updater=service.grant_session,
        session_grant_checker=service.is_granted,
        session_grant_revoker=service.revoke_session,
    )
    result = bridge.claim(confirmation["confirmation_id"], "s1", "trust_session")
    assert result.status == "ok"
    assert result.decision == ApprovalDecision(allowed=True, scope="session")
    assert service.is_granted("s1", "dashboard", "mcp_site_probe")
    decision = await task
    assert decision == ApprovalDecision(allowed=True, scope="session")
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Decider: cancel
# ---------------------------------------------------------------------------

async def test_cancel_returns_denied_decision() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    result = bridge.claim(confirmation["confirmation_id"], "s1", "cancel")
    assert result.status == "ok"
    assert result.decision == ApprovalDecision(
        allowed=False, scope="deny", reason="cancelled"
    )
    decision = await task
    assert decision == ApprovalDecision(
        allowed=False, scope="deny", reason="cancelled"
    )
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Decider: existing grant skip
# ---------------------------------------------------------------------------

async def test_existing_grant_skips_interactive_approval() -> None:
    service = GatewayToolApprovalService()
    service.grant_session("s1", "dashboard", "mcp_site_probe")
    bridge = DashboardToolApprovalBridge()
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def sender(metadata: dict[str, Any]) -> None:
        await sent.put(metadata)

    decider = bridge.create_decider(
        "s1",
        "dashboard",
        sender,
        session_grant_checker=service.is_granted,
    )
    decision = await decider(_request())
    assert decision == ApprovalDecision(allowed=True, scope="session")
    assert sent.empty()
    assert bridge.pending_count == 0


async def test_grant_checker_raises_continues_to_interactive() -> None:
    """If the grant checker raises, the decider must NOT default-allow."""
    bridge = DashboardToolApprovalBridge()

    def bad_checker(session_id: str, actor_id: str, tool_name: str) -> bool:
        raise RuntimeError("check failed")

    task, confirmation = await _start_decider(
        bridge, session_grant_checker=bad_checker
    )
    try:
        assert bridge.pending_count == 1
    finally:
        bridge.claim(confirmation["confirmation_id"], "s1", "once")
        await task


# ---------------------------------------------------------------------------
# Decider: session mismatch
# ---------------------------------------------------------------------------

async def test_decider_rejects_session_mismatch() -> None:
    bridge = DashboardToolApprovalBridge()

    async def sender(metadata: dict[str, Any]) -> None:
        pass

    decider = bridge.create_decider("s1", "dashboard", sender)
    decision = await decider(_request(session_id="s2"))
    assert decision == ApprovalDecision(
        allowed=False, scope="deny", reason="session_mismatch"
    )
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Concurrent pending
# ---------------------------------------------------------------------------

async def test_concurrent_pending_does_not_block_each_other() -> None:
    bridge = DashboardToolApprovalBridge()
    task1, conf1 = await _start_decider(
        bridge, request=_request(tool_call_id="tc-1")
    )
    task2, conf2 = await _start_decider(
        bridge, request=_request(tool_call_id="tc-2")
    )
    assert conf1["confirmation_id"] != conf2["confirmation_id"]
    assert bridge.pending_count == 2

    r1 = bridge.claim(conf1["confirmation_id"], "s1", "once")
    assert r1.status == "ok"
    r2 = bridge.claim(conf2["confirmation_id"], "s1", "once")
    assert r2.status == "ok"

    d1 = await task1
    d2 = await task2
    assert d1.allowed and d2.allowed
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Timeout (decider wait_for)
# ---------------------------------------------------------------------------

async def test_decider_timeout_returns_deny_and_cleans_up() -> None:
    bridge = DashboardToolApprovalBridge(timeout_seconds=0.01)

    async def sender(metadata: dict[str, Any]) -> None:
        pass

    decider = bridge.create_decider("s1", "dashboard", sender)
    decision = await decider(_request())
    assert decision == ApprovalDecision(
        allowed=False, scope="deny", reason="timeout"
    )
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Claim expiry via monotonic clock
# ---------------------------------------------------------------------------

async def test_claim_detects_expiry_via_monotonic_clock() -> None:
    clock = _FakeClock(start=1000.0)
    bridge = DashboardToolApprovalBridge(timeout_seconds=900.0, clock=clock)
    task, confirmation = await _start_decider(bridge)
    clock.advance(901.0)
    result = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert result.status == "conflict"
    decision = await task
    assert decision == ApprovalDecision(
        allowed=False, scope="deny", reason="timeout"
    )
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# UTC display vs monotonic timeout separation
# ---------------------------------------------------------------------------

async def test_utc_display_vs_monotonic_timeout_separation() -> None:
    clock = _FakeClock(start=1000.0)
    bridge = DashboardToolApprovalBridge(timeout_seconds=900.0, clock=clock)
    task, confirmation = await _start_decider(bridge)
    try:
        expires_at = confirmation["expires_at"]
        # UTC RFC 3339 display string
        assert expires_at.endswith("Z")
        # Monotonic timeout is separate from UTC display
        assert bridge.pending_count == 1
    finally:
        # Advance monotonic clock past timeout; UTC string is irrelevant
        clock.advance(901.0)
        result = bridge.claim(confirmation["confirmation_id"], "s1", "once")
        assert result.status == "conflict"
        decision = await task
        assert decision == ApprovalDecision(
            allowed=False, scope="deny", reason="timeout"
        )
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Task cancellation (SSE disconnect) cleanup
# ---------------------------------------------------------------------------

async def test_task_cancel_cleans_up_pending_and_preserves_cancelled_error() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    assert bridge.pending_count == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.pending_count == 0
    # Tombstone written for same session so duplicate claim returns 409
    result = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert result.status == "conflict"


# ---------------------------------------------------------------------------
# Sender failure
# ---------------------------------------------------------------------------

async def test_sender_failure_returns_deny_and_cleans_up() -> None:
    bridge = DashboardToolApprovalBridge()

    async def fail_sender(metadata: dict[str, Any]) -> None:
        raise RuntimeError("send failed")

    decider = bridge.create_decider("s1", "dashboard", fail_sender)
    decision = await decider(_request())
    assert decision == ApprovalDecision(
        allowed=False, scope="deny", reason="sender_failed"
    )
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Duplicate / cross-session claim (404 vs 409)
# ---------------------------------------------------------------------------

async def test_duplicate_claim_same_session_returns_conflict() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    r1 = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r1.status == "ok"
    r2 = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r2.status == "conflict"
    await task
    assert bridge.pending_count == 0


async def test_cross_session_claim_returns_not_found() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge, session_id="s1")
    r = bridge.claim(confirmation["confirmation_id"], "s2", "once")
    assert r.status == "not_found"
    assert bridge.pending_count == 1
    # Original session can still claim
    r2 = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r2.status == "ok"
    await task


async def test_cross_session_claim_on_already_claimed_returns_not_found() -> None:
    """A cross-session caller must not learn an ID is already claimed (404, never 409)."""
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge, session_id="s1")
    r1 = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r1.status == "ok"
    r2 = bridge.claim(confirmation["confirmation_id"], "s2", "once")
    assert r2.status == "not_found"
    await task


# ---------------------------------------------------------------------------
# Grant update failure (no leftover grant)
# ---------------------------------------------------------------------------

async def test_grant_update_failure_no_leftover_grant() -> None:
    service = GatewayToolApprovalService()
    bridge = DashboardToolApprovalBridge()

    def fail_update(session_id: str, actor_id: str, tool_name: str) -> bool:
        raise RuntimeError("store failed")

    task, confirmation = await _start_decider(
        bridge,
        session_grant_updater=fail_update,
        session_grant_checker=service.is_granted,
        session_grant_revoker=service.revoke_session,
    )
    result = bridge.claim(confirmation["confirmation_id"], "s1", "trust_session")
    assert result.status == "ok"
    assert result.decision == ApprovalDecision(
        allowed=False, scope="deny", reason="session_grant_failed"
    )
    decision = await task
    assert decision == ApprovalDecision(
        allowed=False, scope="deny", reason="session_grant_failed"
    )
    assert not service.is_granted("s1", "dashboard", "mcp_site_probe")
    assert bridge.pending_count == 0


async def test_grant_update_returns_false_no_leftover_grant() -> None:
    service = GatewayToolApprovalService()
    bridge = DashboardToolApprovalBridge()

    def fail_update(session_id: str, actor_id: str, tool_name: str) -> bool:
        return False

    task, confirmation = await _start_decider(
        bridge,
        session_grant_updater=fail_update,
        session_grant_checker=service.is_granted,
    )
    result = bridge.claim(confirmation["confirmation_id"], "s1", "trust_session")
    assert result.status == "ok"
    assert result.decision.scope == "deny"
    assert result.decision.reason == "session_grant_failed"
    await task
    assert not service.is_granted("s1", "dashboard", "mcp_site_probe")


async def test_grant_readback_mismatch_revokes_and_denies() -> None:
    """If the checker reports the grant was not persisted, revoke and deny."""
    service = GatewayToolApprovalService()
    bridge = DashboardToolApprovalBridge()

    def bad_checker(session_id: str, actor_id: str, tool_name: str) -> bool:
        return False

    task, confirmation = await _start_decider(
        bridge,
        session_grant_updater=service.grant_session,
        session_grant_checker=bad_checker,
        session_grant_revoker=service.revoke_session,
    )
    result = bridge.claim(confirmation["confirmation_id"], "s1", "trust_session")
    assert result.status == "ok"
    assert result.decision.scope == "deny"
    assert result.decision.reason == "session_grant_failed"
    await task
    assert not service.is_granted("s1", "dashboard", "mcp_site_probe")


# ---------------------------------------------------------------------------
# ID identity-reuse safety
# ---------------------------------------------------------------------------

async def test_identity_reuse_safety() -> None:
    """Late cleanup of a removed pending must not affect a later record reusing the same ID."""
    bridge = DashboardToolApprovalBridge()
    loop = asyncio.get_running_loop()
    f1: asyncio.Future[ApprovalDecision] = loop.create_future()
    f2: asyncio.Future[ApprovalDecision] = loop.create_future()
    p1 = _PendingApproval(
        confirmation_id="C1",
        session_id="s1",
        actor_id="dashboard",
        tool_call_id="tc-1",
        tool_name="tool",
        arguments={},
        description="",
        future=f1,
        created_at=0.0,
        expires_at=900.0,
        expires_at_utc="2026-01-01T00:00:00Z",
        claimed=False,
    )
    p2 = _PendingApproval(
        confirmation_id="C1",
        session_id="s1",
        actor_id="dashboard",
        tool_call_id="tc-2",
        tool_name="tool",
        arguments={},
        description="",
        future=f2,
        created_at=0.0,
        expires_at=900.0,
        expires_at_utc="2026-01-01T00:00:00Z",
        claimed=False,
    )
    bridge._pending["C1"] = p1
    # Clean up p1
    bridge._cleanup_pending(p1, reason="aborted")
    assert "C1" not in bridge._pending
    assert f1.done()
    # Insert p2 with the same confirmation ID (simulating ID reuse)
    bridge._pending["C1"] = p2
    # Late cleanup of p1 must NOT affect p2
    bridge._cleanup_pending(p1, reason="aborted")
    assert bridge._pending.get("C1") is p2
    assert not f2.done()


# ---------------------------------------------------------------------------
# Tombstone session isolation and TTL cleanup
# ---------------------------------------------------------------------------

async def test_tombstone_session_isolation() -> None:
    """Same-session duplicate returns 409; cross-session always returns 404."""
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge, session_id="s1")
    r1 = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r1.status == "ok"
    await task
    # Same session duplicate -> 409
    r2 = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r2.status == "conflict"
    # Cross session -> 404 (does not leak ownership)
    r3 = bridge.claim(confirmation["confirmation_id"], "s2", "once")
    assert r3.status == "not_found"


async def test_tombstone_ttl_cleanup() -> None:
    clock = _FakeClock(start=1000.0)
    bridge = DashboardToolApprovalBridge(timeout_seconds=900.0, clock=clock)
    task, confirmation = await _start_decider(bridge)
    bridge.claim(confirmation["confirmation_id"], "s1", "once")
    await task
    assert bridge.tombstone_count == 1
    # Advance past tombstone TTL
    clock.advance(901.0)
    # Trigger cleanup via another claim
    r = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r.status == "not_found"
    assert bridge.tombstone_count == 0


# ---------------------------------------------------------------------------
# Pending cleanup after all operations
# ---------------------------------------------------------------------------

async def test_pending_cleanup_after_claim() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    bridge.claim(confirmation["confirmation_id"], "s1", "once")
    await task
    assert bridge.pending_count == 0
    assert bridge.tombstone_count == 1


async def test_pending_cleanup_after_cancel() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    bridge.claim(confirmation["confirmation_id"], "s1", "cancel")
    await task
    assert bridge.pending_count == 0


async def test_pending_cleanup_after_timeout() -> None:
    bridge = DashboardToolApprovalBridge(timeout_seconds=0.01)

    async def sender(metadata: dict[str, Any]) -> None:
        pass

    decider = bridge.create_decider("s1", "dashboard", sender)
    await decider(_request())
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# ClaimResult is a typed result (no exceptions for 404/409)
# ---------------------------------------------------------------------------

async def test_claim_result_is_structured_for_not_found() -> None:
    bridge = DashboardToolApprovalBridge()
    result = bridge.claim("nonexistent-id", "s1", "once")
    assert isinstance(result, ClaimResult)
    assert result.status == "not_found"
    assert result.decision is None


# ---------------------------------------------------------------------------
# Security regression: no context leakage in 404/409 results
# ---------------------------------------------------------------------------

async def test_claim_result_does_not_leak_session_or_tool_in_not_found() -> None:
    bridge = DashboardToolApprovalBridge()
    result = bridge.claim("nonexistent-id", "s1", "once")
    assert result.status == "not_found"
    assert result.decision is None
    # ClaimResult is a frozen dataclass with only status/decision fields
    assert not hasattr(result, "session_id")
    assert not hasattr(result, "tool_name")


async def test_claim_result_does_not_leak_session_or_tool_in_conflict() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge, session_id="s1")
    bridge.claim(confirmation["confirmation_id"], "s1", "once")
    await task
    result = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert result.status == "conflict"
    assert result.decision is None
    assert not hasattr(result, "session_id")
    assert not hasattr(result, "tool_name")


# ---------------------------------------------------------------------------
# Security regression: metadata never contains session, actor, or raw args
# ---------------------------------------------------------------------------

async def test_metadata_never_contains_session_actor_or_raw_args() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(
        bridge,
        request=_request(
            arguments={
                "api_key": "secret-value",
                "url": "https://example.com/path?token=leaked",
                "normal": "ok",
            }
        ),
    )
    try:
        # Exactly 5 fields
        assert set(confirmation.keys()) == {
            "confirmation_id", "tool_name", "description",
            "arguments_summary", "expires_at",
        }
        # No session ID or actor anywhere in the metadata
        for value in confirmation.values():
            assert "s1" not in str(value)
            assert "dashboard" not in str(value)
        # Raw arguments not in metadata (only summary)
        assert "raw" not in confirmation
        assert "arguments" not in confirmation
        assert "session_id" not in confirmation
        assert "actor" not in confirmation
    finally:
        await _cancel_silently(task)


# ---------------------------------------------------------------------------
# Security regression: all 5 metadata fields are JSON scalar strings
# ---------------------------------------------------------------------------

async def test_all_metadata_fields_are_strings() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    try:
        for key, value in confirmation.items():
            assert isinstance(value, str), f"{key} is not a string: {type(value)}"
    finally:
        await _cancel_silently(task)


# ---------------------------------------------------------------------------
# Security regression: tombstone stores no arguments or grant info
# ---------------------------------------------------------------------------

async def test_tombstone_stores_no_arguments_or_grant_info() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    bridge.claim(confirmation["confirmation_id"], "s1", "once")
    await task
    # Tombstone exists
    assert bridge.tombstone_count == 1
    tombstone = next(iter(bridge._tombstones.values()))
    assert not hasattr(tombstone, "arguments")
    assert not hasattr(tombstone, "tool_name")
    assert not hasattr(tombstone, "actor_id")
    assert not hasattr(tombstone, "description")
    assert not hasattr(tombstone, "future")
    # Only session_id and expires_at
    assert hasattr(tombstone, "session_id")
    assert hasattr(tombstone, "expires_at")


# ---------------------------------------------------------------------------
# Concurrent pending on different sessions
# ---------------------------------------------------------------------------

async def test_concurrent_pending_on_different_sessions() -> None:
    bridge = DashboardToolApprovalBridge()
    task1, conf1 = await _start_decider(
        bridge, session_id="s1", request=_request(session_id="s1", tool_call_id="tc-1")
    )
    task2, conf2 = await _start_decider(
        bridge, session_id="s2", request=_request(session_id="s2", tool_call_id="tc-2")
    )
    assert bridge.pending_count == 2
    # Cross-session claim returns 404
    r = bridge.claim(conf1["confirmation_id"], "s2", "once")
    assert r.status == "not_found"
    # Same-session claim succeeds
    r1 = bridge.claim(conf1["confirmation_id"], "s1", "once")
    assert r1.status == "ok"
    r2 = bridge.claim(conf2["confirmation_id"], "s2", "once")
    assert r2.status == "ok"
    await task1
    await task2
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Grant readback success path
# ---------------------------------------------------------------------------

async def test_grant_readback_success_allows_session() -> None:
    service = GatewayToolApprovalService()
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(
        bridge,
        session_grant_updater=service.grant_session,
        session_grant_checker=service.is_granted,
        session_grant_revoker=service.revoke_session,
    )
    result = bridge.claim(confirmation["confirmation_id"], "s1", "trust_session")
    assert result.status == "ok"
    assert result.decision == ApprovalDecision(allowed=True, scope="session")
    await task
    assert service.is_granted("s1", "dashboard", "mcp_site_probe")


# ---------------------------------------------------------------------------
# Trust_session without updater denies
# ---------------------------------------------------------------------------

async def test_trust_session_without_updater_denies() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    result = bridge.claim(confirmation["confirmation_id"], "s1", "trust_session")
    assert result.status == "ok"
    assert result.decision.scope == "deny"
    assert result.decision.reason == "session_grant_failed"
    await task
    assert bridge.pending_count == 0


# ---------------------------------------------------------------------------
# Grant readback without revoker still denies on mismatch
# ---------------------------------------------------------------------------

async def test_grant_readback_without_revoker_still_denies() -> None:
    service = GatewayToolApprovalService()
    bridge = DashboardToolApprovalBridge()

    def bad_checker(session_id: str, actor_id: str, tool_name: str) -> bool:
        return False

    task, confirmation = await _start_decider(
        bridge,
        session_grant_updater=service.grant_session,
        session_grant_checker=bad_checker,
        # No revoker supplied
    )
    result = bridge.claim(confirmation["confirmation_id"], "s1", "trust_session")
    assert result.status == "ok"
    assert result.decision.scope == "deny"
    assert result.decision.reason == "session_grant_failed"
    await task
    # Grant was written by the updater but readback failed.
    # Without a revoker the grant may linger, but the decision is still deny.
    # The router must supply a revoker for full safety.


# ---------------------------------------------------------------------------
# Cancelled decider tombstone is session-bound
# ---------------------------------------------------------------------------

async def test_cancelled_task_tombstone_same_session_returns_conflict() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge, session_id="s1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bridge.pending_count == 0
    # Same session -> 409 (tombstone)
    r = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r.status == "conflict"
    # Cross session -> 404 (no leak)
    r = bridge.claim(confirmation["confirmation_id"], "s2", "once")
    assert r.status == "not_found"


# ---------------------------------------------------------------------------
# Browser-type projected arguments are handled (defense-in-depth)
# ---------------------------------------------------------------------------

async def test_projected_browser_type_arguments_in_summary() -> None:
    """AgentGraph already projects browser_type args; the bridge's summary
    redaction is defense-in-depth and must not leak typed text."""
    bridge = DashboardToolApprovalBridge()
    # Simulate the already-projected browser_type args (as AgentGraph would
    # produce via project_browser_tool_arguments)
    projected_args = {
        "text": {"char_count": 5, "redacted": True},
        "element_ref": "btn-1",
        "document_revision": 3,
    }
    task, confirmation = await _start_decider(
        bridge,
        request=_request(
            tool_name="browser_type",
            arguments=projected_args,
        ),
    )
    try:
        summary = confirmation["arguments_summary"]
        # No typed text content (already projected by AgentGraph)
        assert "redacted" in summary or "***" in summary
        # element_ref is safe to show
        assert "btn-1" in summary
    finally:
        await _cancel_silently(task)


# ---------------------------------------------------------------------------
# Tombstone TTL with custom TTL
# ---------------------------------------------------------------------------

async def test_tombstone_custom_ttl() -> None:
    clock = _FakeClock(start=1000.0)
    bridge = DashboardToolApprovalBridge(
        timeout_seconds=900.0,
        tombstone_ttl_seconds=100.0,
        clock=clock,
    )
    task, confirmation = await _start_decider(bridge)
    bridge.claim(confirmation["confirmation_id"], "s1", "once")
    await task
    assert bridge.tombstone_count == 1
    # Advance past custom TTL (100s, not 900s)
    clock.advance(101.0)
    r = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r.status == "not_found"
    assert bridge.tombstone_count == 0


# ---------------------------------------------------------------------------
# Invalid choice raises ValueError (router maps to 422)
# ---------------------------------------------------------------------------

async def test_invalid_choice_raises_value_error() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge)
    try:
        with pytest.raises(ValueError):
            bridge.claim(confirmation["confirmation_id"], "s1", "invalid_choice")
    finally:
        await _cancel_silently(task)


# ---------------------------------------------------------------------------
# Existing grant with checker exception does not default-allow
# ---------------------------------------------------------------------------

async def test_existing_grant_checker_returns_falsy_continues_to_interactive() -> None:
    """If the checker returns False (no existing grant), proceed to interactive approval."""
    bridge = DashboardToolApprovalBridge()

    def false_checker(session_id: str, actor_id: str, tool_name: str) -> bool:
        return False

    task, confirmation = await _start_decider(
        bridge, session_grant_checker=false_checker
    )
    try:
        # Should have created a pending (did not skip to session allow)
        assert bridge.pending_count == 1
    finally:
        bridge.claim(confirmation["confirmation_id"], "s1", "once")
        await task


# ---------------------------------------------------------------------------
# Pending not affected by cross-session claim attempts
# ---------------------------------------------------------------------------

async def test_cross_session_claim_does_not_affect_original_pending() -> None:
    bridge = DashboardToolApprovalBridge()
    task, confirmation = await _start_decider(bridge, session_id="s1")
    # Cross-session claim
    r = bridge.claim(confirmation["confirmation_id"], "s2", "once")
    assert r.status == "not_found"
    # Original pending is still alive and claimable
    assert bridge.pending_count == 1
    r2 = bridge.claim(confirmation["confirmation_id"], "s1", "once")
    assert r2.status == "ok"
    await task
    assert bridge.pending_count == 0
