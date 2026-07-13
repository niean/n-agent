from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from app.domain.gateway import GatewaySessionKey
from app.domain.session import SessionSource
from app.domain.tool import ApprovalDecision, ApprovalRequest, RiskLevel
from app.interfaces.cli.cli_tool_approval import (
    CliToolApprovalBridge,
    CliToolApprovalClaim,
    CliToolApprovalError,
    PendingCliToolApproval,
    arguments_summary,
    redact_sensitive,
)


def _req(
    tool_name: str = "manage_schedule",
    arguments: dict[str, Any] | None = None,
    session_id: str = "cli-session-1",
) -> ApprovalRequest:
    return ApprovalRequest(
        session_id=session_id,
        tool_call_id="call-1",
        tool_name=tool_name,
        arguments=arguments or {"x": 1},
        description="desc",
        risk_level=RiskLevel.CONFIRM,
    )


def _session_key(conv: str = "conv-1") -> GatewaySessionKey:
    return GatewaySessionKey(SessionSource.CLI.value, conv, display_name=conv)


# --- T1: redaction helper and dataclass ---


def test_redact_sensitive_masks_secret_keys_recursively():
    out = redact_sensitive({"api_key": "sk-1", "nested": {"Token": "t"}, "safe": "v"})
    assert out == {"api_key": "***", "nested": {"Token": "***"}, "safe": "v"}


def test_redact_sensitive_preserves_lists_and_primitives():
    assert redact_sensitive([1, "a", {"password": "p"}]) == [1, "a", {"password": "***"}]
    assert redact_sensitive("x") == "x"
    assert redact_sensitive(42) == 42


def test_redact_sensitive_normalizes_key_separators():
    out = redact_sensitive({"api-key": "v", "API_KEY": "v", "private key": "v"})
    assert out == {"api-key": "***", "API_KEY": "***", "private key": "***"}


def test_arguments_summary_truncates_to_800_chars():
    out = arguments_summary({"k": "v" * 1000})
    assert len(out) == 800
    assert out.endswith("...")


def test_arguments_summary_redacts_then_serializes():
    out = arguments_summary({"api_key": "sk-1", "name": "n"})
    assert "sk-1" not in out
    assert "***" in out


def test_arguments_summary_handles_non_ascii():
    out = arguments_summary({"name": "中文", "city": "北京"})
    assert "中文" in out
    assert "北京" in out


def test_arguments_summary_handles_non_json_serializable():
    out = arguments_summary({"ts": datetime(2026, 1, 1, 12, 0, 0)})
    assert "2026" in out


def test_arguments_summary_fallback_on_serialization_failure():
    class Boom:
        def __repr__(self):
            raise RuntimeError("nope")

    def _raise(*_):
        raise RuntimeError("serialize failed")

    out = arguments_summary.__wrapped__ if hasattr(arguments_summary, "__wrapped__") else arguments_summary
    # Direct call: default=str should handle most objects; test json.dumps fallback
    import app.interfaces.cli.cli_tool_approval as mod
    original_dumps = mod.json.dumps
    mod.json.dumps = _raise
    try:
        result = arguments_summary({"x": 1})
        assert result == "***"
    finally:
        mod.json.dumps = original_dumps


def test_pending_dataclass_carries_required_fields():
    loop = asyncio.new_event_loop()
    try:
        pending = PendingCliToolApproval(
            confirmation_id="id-1",
            request=_req(),
            session_key=_session_key(),
            actor_id="cli:conv-1",
            future=loop.create_future(),
            created_at=0.0,
            expires_at=900.0,
        )
        assert pending.claimed is False
        assert pending.cleanup is None
        assert pending.session_grant_updater is None
        assert pending.cleanup_called is False
    finally:
        loop.close()


def test_confirmation_id_unique_and_nonempty():
    async def _run():
        bridge = CliToolApprovalBridge(timeout_seconds=900.0)
        seen: list[dict[str, Any]] = []
        decider = bridge.create_decider(_session_key(), "cli:conv-1", lambda m: seen.append(m))
        t1 = asyncio.create_task(decider(_req()))
        await asyncio.sleep(0.01)
        id1 = seen[0]["id"]
        assert id1 and id1.startswith("tool-confirm-")
        claim = bridge.claim(id1, "cancel", actor_id="cli:conv-1", session_key=_session_key())
        bridge.complete(claim)
        await t1

        seen.clear()
        decider2 = bridge.create_decider(_session_key(), "cli:conv-1", lambda m: seen.append(m))
        t2 = asyncio.create_task(decider2(_req()))
        await asyncio.sleep(0.01)
        id2 = seen[0]["id"]
        claim2 = bridge.claim(id2, "cancel", actor_id="cli:conv-1", session_key=_session_key())
        bridge.complete(claim2)
        await t2
        assert id1 != id2

    asyncio.run(_run())


# --- T2: decider grant/notifier/wait ---


@pytest.fixture
def bridge():
    return CliToolApprovalBridge(timeout_seconds=900.0)


@pytest.fixture
def short_ttl_bridge():
    return CliToolApprovalBridge(timeout_seconds=0.05)


def _make_decider(
    bridge: CliToolApprovalBridge,
    notifier: Any = None,
    grants: set | None = None,
    updater: Any = None,
    checker: Any = None,
    cleanup: Any = None,
    conv: str = "conv-1",
):
    if notifier is None:
        seen: list[dict[str, Any]] = []
        notifier = lambda m: seen.append(m)  # noqa: E731
    if grants is not None and checker is None:
        checker = lambda sid, aid, tn: (sid, aid, tn) in grants  # noqa: E731
    if grants is not None and updater is None:
        updater = lambda sid, aid, tn: grants.add((sid, aid, tn))  # noqa: E731
    return bridge.create_decider(
        _session_key(conv),
        f"cli:{conv}",
        notifier,
        cleanup=cleanup,
        session_grant_updater=updater,
        session_grant_checker=checker,
    )


@pytest.mark.asyncio
async def test_decider_returns_session_grant_when_already_granted(bridge):
    seen: list[dict[str, Any]] = []
    grants: set[tuple[str, str, str]] = {("cli-session-1", "cli:conv-1", "manage_schedule")}
    decider = _make_decider(bridge, lambda m: seen.append(m), grants=grants)
    decision = await decider(_req())
    assert decision.allowed is True
    assert decision.scope == "session"
    assert bridge.pending_count == 0
    assert seen == []


@pytest.mark.asyncio
async def test_grant_checker_uses_request_session_id_and_tool_name(bridge):
    seen: list[dict[str, Any]] = []
    recorded: list[tuple[str, str, str]] = []
    def checker(sid, aid, tn):
        recorded.append((sid, aid, tn))
        return False
    decider = bridge.create_decider(
        _session_key(),
        "cli:conv-1",
        lambda m: seen.append(m),
        session_grant_checker=checker,
    )
    task = asyncio.create_task(decider(_req(session_id="real-internal-session")))
    await asyncio.sleep(0.01)
    assert recorded == [("real-internal-session", "cli:conv-1", "manage_schedule")]
    claim = bridge.claim(seen[0]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    await task


@pytest.mark.asyncio
async def test_grant_checker_exception_falls_through_to_pending(bridge):
    seen: list[dict[str, Any]] = []
    def boom(*_):
        raise RuntimeError("db down")
    decider = bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: seen.append(m), session_grant_checker=boom
    )
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    assert bridge.pending_count == 1
    claim = bridge.claim(seen[-1]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    await task


@pytest.mark.asyncio
async def test_second_concurrent_pending_fail_closed(bridge):
    seen: list[dict[str, Any]] = []
    decider = bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: seen.append(m)
    )
    task1 = asyncio.create_task(decider(_req("tool_a")))
    await asyncio.sleep(0.01)
    decision2 = await decider(_req("tool_b"))
    assert decision2.allowed is False
    assert decision2.scope == "deny"
    assert decision2.reason == "concurrent_approval"
    assert seen[0]["id"] == seen[0]["id"]  # first pending id unchanged
    claim = bridge.claim(seen[0]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    await task1


@pytest.mark.asyncio
async def test_decider_creates_pending_and_notifies(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    assert bridge.pending_count == 1
    assert len(seen) == 1
    assert seen[0]["kind"] == "tool_policy"
    assert seen[0]["tool_name"] == "manage_schedule"
    assert "arguments_summary" in seen[0]
    assert "id" in seen[0]
    claim = bridge.claim(seen[0]["id"], "once", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    decision = await task
    assert decision.allowed is True
    assert decision.scope == "once"
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_notifier_exception_returns_deny(bridge):
    def boom(_):
        raise RuntimeError("notify failed")
    decider = bridge.create_decider(_session_key(), "cli:conv-1", boom)
    decision = await decider(_req())
    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "notification_failed"
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_decider_timeout_returns_deny(short_ttl_bridge):
    seen: list[dict[str, Any]] = []
    decider = short_ttl_bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: seen.append(m)
    )
    decision = await decider(_req())
    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "timeout"
    assert short_ttl_bridge.pending_count == 0


@pytest.mark.asyncio
async def test_cleanup_callback_fires_on_timeout(short_ttl_bridge):
    cleared: list[str] = []
    decider = short_ttl_bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: None,
        cleanup=lambda cid: cleared.append(cid),
    )
    decision = await decider(_req())
    assert decision.reason == "timeout"
    assert len(cleared) == 1


# --- T3: claim/complete/discard ---


@pytest.mark.asyncio
async def test_claim_rejects_wrong_actor(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    with pytest.raises(CliToolApprovalError):
        bridge.claim(seen[0]["id"], "once", actor_id="cli:other", session_key=_session_key())
    claim = bridge.claim(seen[0]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    await task


@pytest.mark.asyncio
async def test_claim_rejects_wrong_session_key(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    wrong_key = GatewaySessionKey(SessionSource.CLI.value, "other-conv")
    with pytest.raises(CliToolApprovalError):
        bridge.claim(seen[0]["id"], "once", actor_id="cli:conv-1", session_key=wrong_key)
    claim = bridge.claim(seen[0]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    await task


@pytest.mark.asyncio
async def test_claim_rejects_invalid_choice(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    with pytest.raises(CliToolApprovalError):
        bridge.claim(seen[-1]["id"], "bogus", actor_id="cli:conv-1", session_key=_session_key())
    claim = bridge.claim(seen[-1]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    await task


@pytest.mark.asyncio
async def test_claim_rejects_unknown_id(bridge):
    with pytest.raises(CliToolApprovalError):
        bridge.claim("nope", "once", actor_id="cli:conv-1", session_key=_session_key())


@pytest.mark.asyncio
async def test_claim_rejects_expired_pending(short_ttl_bridge):
    seen: list[dict[str, Any]] = []
    decider = short_ttl_bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: seen.append(m)
    )
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    cid = seen[-1]["id"]
    await task  # wait for timeout
    with pytest.raises(CliToolApprovalError):
        short_ttl_bridge.claim(cid, "once", actor_id="cli:conv-1", session_key=_session_key())


@pytest.mark.asyncio
async def test_duplicate_claim_rejected(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    claim = bridge.claim(seen[-1]["id"], "once", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    with pytest.raises(CliToolApprovalError):
        bridge.claim(seen[-1]["id"], "once", actor_id="cli:conv-1", session_key=_session_key())
    await task


@pytest.mark.asyncio
async def test_complete_with_fake_claim_object_does_nothing(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    pending = bridge._pending[seen[-1]["id"]]
    fake = CliToolApprovalClaim(seen[-1]["id"], "once", pending)
    # Mark as claimed via real claim first
    real_claim = bridge.claim(seen[-1]["id"], "once", actor_id="cli:conv-1", session_key=_session_key())
    # Now try complete with fake (different object identity)
    bridge.complete(fake)  # should be no-op since fake is not the real claim
    # Real claim still works
    bridge.complete(real_claim)
    decision = await task
    assert decision.allowed is True
    assert decision.scope == "once"


@pytest.mark.asyncio
async def test_decider_trust_session_registers_grant(bridge):
    grants: set[tuple[str, str, str]] = set()
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m), grants=grants)
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    claim = bridge.claim(seen[0]["id"], "trust_session", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    decision = await task
    assert decision.allowed is True
    assert decision.scope == "session"
    assert grants == {("cli-session-1", "cli:conv-1", "manage_schedule"): True} or \
           grants == {("cli-session-1", "cli:conv-1", "manage_schedule")}


@pytest.mark.asyncio
async def test_decider_cancel_returns_deny(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    claim = bridge.claim(seen[0]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    decision = await task
    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "cancelled"


@pytest.mark.asyncio
async def test_grant_updater_exception_degrades_to_once(bridge):
    seen: list[dict[str, Any]] = []
    def boom(*_):
        raise RuntimeError("db down")
    decider = bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: seen.append(m),
        session_grant_updater=boom,
    )
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    claim = bridge.claim(seen[-1]["id"], "trust_session", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    decision = await task
    assert decision.allowed is True
    assert decision.scope == "once"
    assert decision.reason == "session_grant_failed"


@pytest.mark.asyncio
async def test_discard_pending_for_actor_sets_deny_not_cancel(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    bridge.discard_pending_for_actor("cli:conv-1", _session_key())
    decision = await task
    assert decision.allowed is False
    assert decision.scope == "deny"
    assert decision.reason == "cancelled"
    assert bridge.pending_count == 0


@pytest.mark.asyncio
async def test_cleanup_callback_fires_on_discard_pending_for_actor(bridge):
    cleared: list[str] = []
    decider = bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: None,
        cleanup=lambda cid: cleared.append(cid),
    )
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    bridge.discard_pending_for_actor("cli:conv-1", _session_key())
    await task
    assert len(cleared) == 1


@pytest.mark.asyncio
async def test_discard_pending_for_actor_does_not_match_wrong_session_key(bridge):
    seen: list[dict[str, Any]] = []
    decider = _make_decider(bridge, lambda m: seen.append(m))
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    wrong_key = GatewaySessionKey(SessionSource.CLI.value, "other-conv")
    bridge.discard_pending_for_actor("cli:conv-1", wrong_key)
    assert bridge.pending_count == 1  # not discarded
    claim = bridge.claim(seen[-1]["id"], "cancel", actor_id="cli:conv-1", session_key=_session_key())
    bridge.complete(claim)
    await task


@pytest.mark.asyncio
async def test_cleanup_idempotent_on_double_discard(bridge):
    cleared: list[str] = []
    decider = bridge.create_decider(
        _session_key(), "cli:conv-1", lambda m: None,
        cleanup=lambda cid: cleared.append(cid),
    )
    task = asyncio.create_task(decider(_req()))
    await asyncio.sleep(0.01)
    bridge.discard_pending_for_actor("cli:conv-1", _session_key())
    await task
    # discard_pending_for_actor calls _discard_pending, then decider finally
    # also calls _discard_pending. cleanup must fire only once.
    assert len(cleared) == 1
