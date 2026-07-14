"""Tests for GatewayPolicy (Domain).

Covers:
- Verified Gateway actor + session match -> ALLOW (inbound)
- Managed/destructive action WITHOUT actor -> DENY (or REQUIRE_APPROVAL if channel)
- Outbound origin mismatch -> DENY
- Ordinary verified anonymous message -> ALLOW
"""

from app.domain.gateway import (
    GatewayConfirmationAction,
    GatewaySessionKey,
)
from app.domain.gateway_policy import (
    GatewayAccessDecision,
    GatewayDeliveryDecision,
    GatewayInboundRequest,
    GatewayOutboundRequest,
    GatewayPolicy,
)
from app.domain.platform import Platform
from app.domain.policy import PolicyOutcome


def _feishu_key(receive_id="oc_a", thread_id=""):
    return GatewaySessionKey(
        source=Platform.FEISHU,
        platform_session_id=receive_id,
        thread_id=thread_id,
        display_name="Feishu User",
    )


def _cli_key(display_name="Local"):
    return GatewaySessionKey("cli", "local", display_name=display_name)


# ---------------------------------------------------------------------------
# Inbound: ordinary verified anonymous message -> ALLOW
# ---------------------------------------------------------------------------


def test_ordinary_verified_anonymous_message_allowed():
    policy = GatewayPolicy()
    request = GatewayInboundRequest(
        session_key=_cli_key(),
        actor_id=None,
        action=None,
        is_bootstrap=False,
        has_confirmation_channel=False,
        session_id=None,
        require_actor_for_managed_actions=True,
    )

    decision = policy.evaluate_inbound(request)

    assert decision.verdict is PolicyOutcome.ALLOW
    assert decision.actor is None
    assert decision.reason


# ---------------------------------------------------------------------------
# Inbound: verified actor + session match -> ALLOW
# ---------------------------------------------------------------------------


def test_verified_actor_with_session_match_allowed():
    policy = GatewayPolicy()
    key = _feishu_key()
    request = GatewayInboundRequest(
        session_key=key,
        actor_id="ou_1",
        action=GatewayConfirmationAction.DELETE,
        is_bootstrap=False,
        has_confirmation_channel=True,
        session_id="session-1",
        require_actor_for_managed_actions=True,
    )

    decision = policy.evaluate_inbound(request)

    assert decision.verdict is PolicyOutcome.ALLOW
    assert decision.actor == "ou_1"
    assert decision.session_owner is not None


# ---------------------------------------------------------------------------
# Inbound: destructive without actor, with confirmation channel -> REQUIRE_APPROVAL
# ---------------------------------------------------------------------------


def test_destructive_without_actor_require_approval_when_channel_exists():
    policy = GatewayPolicy()
    request = GatewayInboundRequest(
        session_key=_feishu_key(),
        actor_id=None,
        action=GatewayConfirmationAction.DELETE,
        is_bootstrap=False,
        has_confirmation_channel=True,
        session_id="session-1",
        require_actor_for_managed_actions=True,
    )

    decision = policy.evaluate_inbound(request)

    assert decision.verdict is PolicyOutcome.REQUIRE_APPROVAL
    assert decision.confirmation_requirement is not None


# ---------------------------------------------------------------------------
# Inbound: destructive without actor, no confirmation channel -> DENY
# ---------------------------------------------------------------------------


def test_destructive_without_actor_denied_when_no_channel():
    policy = GatewayPolicy()
    request = GatewayInboundRequest(
        session_key=_feishu_key(),
        actor_id=None,
        action=GatewayConfirmationAction.DELETE,
        is_bootstrap=False,
        has_confirmation_channel=False,
        session_id="session-1",
        require_actor_for_managed_actions=True,
    )

    decision = policy.evaluate_inbound(request)

    assert decision.verdict is PolicyOutcome.DENY
    assert decision.actor is None


# ---------------------------------------------------------------------------
# Inbound: bootstrap (/new without actor) -> ALLOW
# ---------------------------------------------------------------------------


def test_bootstrap_new_without_actor_allowed():
    policy = GatewayPolicy()
    request = GatewayInboundRequest(
        session_key=_feishu_key(),
        actor_id=None,
        action=GatewayConfirmationAction.NEW,
        is_bootstrap=True,
        has_confirmation_channel=False,
        session_id=None,
        require_actor_for_managed_actions=True,
    )

    decision = policy.evaluate_inbound(request)

    assert decision.verdict is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Inbound: managed action without actor when require_actor=False -> ALLOW
# ---------------------------------------------------------------------------


def test_managed_action_without_actor_allowed_when_not_required():
    policy = GatewayPolicy()
    request = GatewayInboundRequest(
        session_key=_feishu_key(),
        actor_id=None,
        action=GatewayConfirmationAction.DELETE,
        is_bootstrap=False,
        has_confirmation_channel=False,
        session_id="session-1",
        require_actor_for_managed_actions=False,
    )

    decision = policy.evaluate_inbound(request)

    assert decision.verdict is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Inbound: destructive with actor but no session -> DENY
# ---------------------------------------------------------------------------


def test_destructive_with_actor_but_no_session_denied():
    policy = GatewayPolicy()
    request = GatewayInboundRequest(
        session_key=_feishu_key(),
        actor_id="ou_1",
        action=GatewayConfirmationAction.DELETE,
        is_bootstrap=False,
        has_confirmation_channel=True,
        session_id=None,
        require_actor_for_managed_actions=True,
    )

    decision = policy.evaluate_inbound(request)

    assert decision.verdict is PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# Outbound: origin mismatch -> DENY
# ---------------------------------------------------------------------------


def test_outbound_origin_mismatch_denied():
    policy = GatewayPolicy()
    origin_key = _feishu_key(receive_id="oc_a")
    target_key = _feishu_key(receive_id="oc_b")
    request = GatewayOutboundRequest(
        target_session_key=target_key,
        origin_session_key=origin_key,
        target_actor_id="ou_1",
        origin_actor_id="ou_2",
    )

    decision = policy.evaluate_outbound(request)

    assert decision.verdict is PolicyOutcome.DENY


def test_outbound_origin_match_allowed():
    policy = GatewayPolicy()
    key = _feishu_key(receive_id="oc_a")
    request = GatewayOutboundRequest(
        target_session_key=key,
        origin_session_key=key,
        target_actor_id="ou_1",
        origin_actor_id="ou_1",
    )

    decision = policy.evaluate_outbound(request)

    assert decision.verdict is PolicyOutcome.ALLOW


def test_outbound_session_key_mismatch_denied():
    policy = GatewayPolicy()
    request = GatewayOutboundRequest(
        target_session_key=_feishu_key(receive_id="oc_a", thread_id="t1"),
        origin_session_key=_feishu_key(receive_id="oc_a", thread_id="t2"),
        target_actor_id="ou_1",
        origin_actor_id="ou_1",
    )

    decision = policy.evaluate_outbound(request)

    assert decision.verdict is PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# Outbound: missing actor on either side -> DENY
# ---------------------------------------------------------------------------


def test_outbound_missing_origin_actor_denied():
    policy = GatewayPolicy()
    key = _feishu_key()
    request = GatewayOutboundRequest(
        target_session_key=key,
        origin_session_key=key,
        target_actor_id="ou_1",
        origin_actor_id=None,
    )

    decision = policy.evaluate_outbound(request)

    assert decision.verdict is PolicyOutcome.DENY
