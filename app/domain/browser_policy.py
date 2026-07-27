"""BrowserPolicy - 15th domain Policy. Fail-closed, IO-free, no cross-Policy imports."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.browser import (
    BrowserBackendType,
    BrowserScreenshotConsumer,
    BrowserSession,
    BrowserSessionStatus,
)
from app.domain.policy import PolicyOutcome


# Non-active statuses that deny all Agent actions (PENDING_AUTHORIZATION handled
# separately to surface the more informative host_grant_required reason).
_NON_ACTIVE_ACTION_STATUSES = frozenset({
    BrowserSessionStatus.PAUSED,
    BrowserSessionStatus.TAKEOVER,
    BrowserSessionStatus.DEGRADED,
    BrowserSessionStatus.CLOSED,
})


@dataclass(frozen=True)
class BrowserPolicyRequest:
    run_context: Any
    session: BrowserSession
    action_type: str
    requested_backend: BrowserBackendType
    trusted_host_grant: Any | None
    screenshot_consumer: BrowserScreenshotConsumer | None
    takeover_operation: str | None  # "request" | "release" | None


@dataclass(frozen=True)
class BrowserPolicyDecision:
    outcome: PolicyOutcome
    reason: str
    allowed_backend: BrowserBackendType | None = None
    screenshot_allowed: bool = False


class BrowserPolicy:
    """Evaluates backend admission, host grant, session state, screenshot release, takeover.

    Pure domain: no IO, no cross-Policy imports. Application constructs the request
    (including trusted_host_grant from the server-side grant store) and acts on the
    decision. Host Bridge re-evaluates with the same immutable snapshot.
    """

    def evaluate(self, request: BrowserPolicyRequest) -> BrowserPolicyDecision:
        session = request.session

        # Takeover requests: Agent-initiated takeover requires approval; only a
        # verified Dashboard challenge (handled by Application before calling with
        # takeover_operation="release" or a dedicated allow path) may ALLOW.
        if request.takeover_operation == "request":
            return BrowserPolicyDecision(PolicyOutcome.REQUIRE_APPROVAL, "takeover_requires_approval")
        if request.takeover_operation == "release":
            if session.status is BrowserSessionStatus.TAKEOVER:
                return BrowserPolicyDecision(
                    PolicyOutcome.ALLOW, "takeover_release", allowed_backend=session.backend_type
                )
            return BrowserPolicyDecision(PolicyOutcome.DENY, "not_in_takeover")

        # PENDING_AUTHORIZATION: host session awaiting grant.
        if session.status is BrowserSessionStatus.PENDING_AUTHORIZATION:
            return BrowserPolicyDecision(PolicyOutcome.DENY, "host_grant_required")

        # Other non-active states deny all Agent actions.
        if session.status in _NON_ACTIVE_ACTION_STATUSES:
            return BrowserPolicyDecision(PolicyOutcome.DENY, "session_not_active")

        # Screenshot release gate.
        if request.action_type == "screenshot" and request.screenshot_consumer is not None:
            if request.screenshot_consumer is BrowserScreenshotConsumer.DASHBOARD_INTERNAL:
                return BrowserPolicyDecision(
                    PolicyOutcome.ALLOW,
                    "screenshot_dashboard",
                    allowed_backend=session.backend_type,
                    screenshot_allowed=True,
                )
            return BrowserPolicyDecision(PolicyOutcome.DENY, "screenshot_consumer_denied")

        # Backend admission.
        if request.requested_backend is BrowserBackendType.HOST_CDP:
            if request.trusted_host_grant is None or not _grant_valid(request.trusted_host_grant, session):
                return BrowserPolicyDecision(PolicyOutcome.DENY, "host_grant_required")
            return BrowserPolicyDecision(
                PolicyOutcome.ALLOW,
                "host_grant_valid",
                allowed_backend=BrowserBackendType.HOST_CDP,
            )
        # CONTAINER default allow.
        return BrowserPolicyDecision(
            PolicyOutcome.ALLOW,
            "container_allowed",
            allowed_backend=BrowserBackendType.CONTAINER,
        )


def _grant_valid(grant: Any, session: BrowserSession) -> bool:
    """Fail-closed validation of ALL grant bindings per spec.

    The grant is constructed by Application from the server-side grant store; the
    domain Policy is the last defense and must validate every binding.
    """
    try:
        return (
            getattr(grant, "browser_session_id", None) == session.id
            and getattr(grant, "n_agent_session_id", None) == session.bound_n_agent_session_id
            and bool(getattr(grant, "actor_id", None))
            and bool(getattr(grant, "policy_version", None))
            and not getattr(grant, "expired", False)
            and not getattr(grant, "revoked", False)
        )
    except Exception:
        return False
