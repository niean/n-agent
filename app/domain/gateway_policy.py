"""Gateway inbound/outbound Policy (Domain).

Governance for Gateway inbound source trust, actor can operate target
conversation/session, and outbound platform/target/thread ownership.

GatewayPolicy ONLY governs Gateway inbound + Gateway outbound.  It does NOT
intercept OpenAI direct API or Dashboard local-debug (those use low-trust
server facts, no GatewayPolicy).

Pure Domain: imports only stdlib + app.domain.gateway + app.domain.policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from app.domain.gateway import (
    GatewayConfirmationAction,
    GatewaySessionKey,
)
from app.domain.policy import PolicyOutcome


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayAccessDecision:
    """Inbound access decision for a Gateway message."""

    verdict: PolicyOutcome
    actor: str | None
    session_owner: tuple[str, str, str] | None
    trusted_claims: Mapping[str, Any]
    confirmation_requirement: str | None
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "trusted_claims",
            MappingProxyType(dict(self.trusted_claims)),
        )


@dataclass(frozen=True)
class GatewayDeliveryDecision:
    """Outbound delivery decision for a Gateway message."""

    verdict: PolicyOutcome
    reason: str
    target_owner: tuple[str, str, str] | None = None


# ---------------------------------------------------------------------------
# Requests (verified facts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayInboundRequest:
    """Verified facts for a Gateway inbound message.

    All fields are verified by the Interfaces layer (signature, allowlist,
    actor parse).  The policy only consumes verified facts.
    """

    session_key: GatewaySessionKey
    actor_id: str | None
    action: GatewayConfirmationAction | None
    is_bootstrap: bool
    has_confirmation_channel: bool
    session_id: str | None
    require_actor_for_managed_actions: bool = True


@dataclass(frozen=True)
class GatewayOutboundRequest:
    """Verified facts for a Gateway outbound delivery."""

    target_session_key: GatewaySessionKey
    origin_session_key: GatewaySessionKey
    target_actor_id: str | None
    origin_actor_id: str | None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class GatewayPolicy:
    """Domain policy for Gateway inbound/outbound governance.

    Decision table:
    - Verified actor + session match -> ALLOW (inbound).
    - Managed/destructive action WITHOUT actor ->
        DENY (or REQUIRE_APPROVAL if confirmation channel exists; fail-closed).
    - Outbound origin mismatch -> DENY.
    - Ordinary verified anonymous message (non-destructive) -> ALLOW.
    """

    def evaluate_inbound(
        self,
        request: GatewayInboundRequest,
    ) -> GatewayAccessDecision:
        # Ordinary verified anonymous message (non-destructive) -> ALLOW
        if request.action is None:
            return GatewayAccessDecision(
                verdict=PolicyOutcome.ALLOW,
                actor=request.actor_id,
                session_owner=(
                    request.session_key.conversation_parts
                    if request.session_id
                    else None
                ),
                trusted_claims={},
                confirmation_requirement=None,
                reason="ordinary verified message allowed",
            )

        # Bootstrap action (/new without actor) -> ALLOW
        if request.is_bootstrap:
            return GatewayAccessDecision(
                verdict=PolicyOutcome.ALLOW,
                actor=request.actor_id,
                session_owner=None,
                trusted_claims={},
                confirmation_requirement=None,
                reason="bootstrap action allowed",
            )

        # If require_actor_for_managed_actions is False, allow without actor
        if not request.require_actor_for_managed_actions:
            return GatewayAccessDecision(
                verdict=PolicyOutcome.ALLOW,
                actor=request.actor_id,
                session_owner=(
                    request.session_key.conversation_parts
                    if request.session_id
                    else None
                ),
                trusted_claims={},
                confirmation_requirement=None,
                reason="managed action allowed (actor not required)",
            )

        # Managed/destructive action WITHOUT actor -> DENY or REQUIRE_APPROVAL
        if request.actor_id is None:
            if request.has_confirmation_channel:
                return GatewayAccessDecision(
                    verdict=PolicyOutcome.REQUIRE_APPROVAL,
                    actor=None,
                    session_owner=None,
                    trusted_claims={},
                    confirmation_requirement="once",
                    reason="destructive action requires actor confirmation",
                )
            return GatewayAccessDecision(
                verdict=PolicyOutcome.DENY,
                actor=None,
                session_owner=None,
                trusted_claims={},
                confirmation_requirement=None,
                reason="destructive action without actor denied (no confirmation channel)",
            )

        # Destructive action with actor but no existing session -> DENY
        if request.session_id is None and request.action is not GatewayConfirmationAction.NEW:
            return GatewayAccessDecision(
                verdict=PolicyOutcome.DENY,
                actor=request.actor_id,
                session_owner=None,
                trusted_claims={},
                confirmation_requirement=None,
                reason="destructive action requires existing session",
            )

        # Verified actor + session match -> ALLOW
        return GatewayAccessDecision(
            verdict=PolicyOutcome.ALLOW,
            actor=request.actor_id,
            session_owner=(
                request.session_key.conversation_parts
                if request.session_id
                else None
            ),
            trusted_claims={},
            confirmation_requirement=None,
            reason="verified actor with session match",
        )

    def evaluate_outbound(
        self,
        request: GatewayOutboundRequest,
    ) -> GatewayDeliveryDecision:
        """Evaluate outbound delivery governance.

        Wired by ScheduleRunService._deliver for ORIGIN delivery targets
        (InformationFlow release -> GatewayPolicy.evaluate_outbound ->
        OutboundDelivery).  DASHBOARD and SILENT targets skip this check
        (no external client call).
        """
        # Missing actor on either side -> DENY
        if request.origin_actor_id is None or request.target_actor_id is None:
            return GatewayDeliveryDecision(
                verdict=PolicyOutcome.DENY,
                reason="outbound delivery requires actor on both sides",
                target_owner=request.target_session_key.conversation_parts,
            )

        # Outbound origin mismatch (different actor or session/thread) -> DENY
        if (
            request.target_actor_id != request.origin_actor_id
            or request.target_session_key.conversation_parts
            != request.origin_session_key.conversation_parts
        ):
            return GatewayDeliveryDecision(
                verdict=PolicyOutcome.DENY,
                reason="outbound origin mismatch: target belongs to different actor/session/thread",
                target_owner=request.target_session_key.conversation_parts,
            )

        return GatewayDeliveryDecision(
            verdict=PolicyOutcome.ALLOW,
            reason="outbound origin match",
            target_owner=request.target_session_key.conversation_parts,
        )
