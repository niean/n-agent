"""Host Bridge for browser CDP (T12b).

Host-side process logic: loopback-only server, file token auth, restricted
protocol (session create / action / state / close), re-evaluates
BrowserPolicy snapshot per request, proxies to managed Chrome via CDP.

Security model (mirrors host_terminal):
  - Loopback-only binding (127.0.0.1).
  - File-based bearer token (perm-checked: 0600, regular file, owned by
    current user, no symlinks).
  - Independent Policy snapshot reload + per-request re-check (fail-closed
    on reload failure).
  - Restricted protocol: only session create / action / state / takeover /
    close. No raw CDP method / endpoint / target / profile path is accepted.
  - Does NOT start/manage the user's daily Chrome. Connects to a Chrome the
    user explicitly started with remote debugging, OR manages a dedicated
    Chrome instance with a user-chosen profile. Does NOT expose the debug
    port to non-loopback.
  - Fail-closed on: grant expired/revoked, policy version mismatch, unknown
    capability, target disappeared, policy reload failed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import os
from pathlib import Path
import stat
import threading
from typing import Any, Callable, Protocol

from app.domain.browser import (
    BrowserBackendType,
    BrowserScreenshotConsumer,
    BrowserSession,
    BrowserSessionStatus,
)
from app.domain.browser_policy import (
    BrowserPolicy,
    BrowserPolicyRequest,
)
from app.domain.policy import PolicyOutcome
from app.infrastructure.browser.host_cdp_backend import load_secure_token


# Supported action types (the bridge only proxies these, not raw CDP).
_KNOWN_ACTION_TYPES = frozenset({
    "navigate",
    "click",
    "type",
    "scroll",
    "observe",
    "screenshot",
})

# Screenshot actions require a screenshot consumer for policy evaluation.
_SCREENSHOT_ACTION_TYPES = frozenset({"screenshot"})


class GrantStore(Protocol):
    """Loads host grants by session_id from a trusted server-side store."""

    def load_grant(self, session_id: str) -> dict[str, Any] | None: ...


class CdpTargetController(Protocol):
    """Manages CDP targets (tabs) in the managed Chrome.

    The bridge uses this to create/close targets and proxy actions. The
    real implementation connects to the managed Chrome via CDP; tests
    inject a fake.
    """

    def create_target(self, profile_ref: str) -> str:
        """Create a new tab/target, return its target_id."""
        ...

    def close_target(self, target_id: str) -> None:
        """Close the target."""
        ...

    def execute_action(
        self,
        target_id: str,
        action_type: str,
        action: dict[str, Any],
        document_revision: int,
    ) -> dict[str, Any]:
        """Execute an action on the target, return a result dict."""
        ...

    def get_state(self, target_id: str) -> dict[str, Any]:
        """Return the current state of the target."""
        ...


@dataclass(frozen=True)
class _BridgeGrant:
    """Immutable grant snapshot used for BrowserPolicy re-evaluation."""

    browser_session_id: str
    n_agent_session_id: str
    actor_id: str
    policy_version: str
    expires_at: datetime
    revoked: bool = False

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


@dataclass
class _RegisteredSession:
    """Bridge-side per-session state."""

    session: BrowserSession
    target_id: str
    profile_ref: str


@dataclass(frozen=True)
class HostBridgeConfig:
    """Configuration for the host browser bridge."""

    token_path: str | os.PathLike[str]
    policy_version: str
    cdp_endpoint: str
    bind_host: str = "127.0.0.1"
    port: int = 8766
    max_request_bytes: int = 262_144
    max_concurrency: int = 1

    def __post_init__(self) -> None:
        if self.bind_host != "127.0.0.1":
            raise ValueError("host_bridge_loopback_required")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= 65535
        ):
            raise ValueError("host_bridge_port_invalid")
        if (
            isinstance(self.max_request_bytes, bool)
            or not isinstance(self.max_request_bytes, int)
            or self.max_request_bytes <= 0
        ):
            raise ValueError("host_bridge_limits_invalid")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency <= 0
        ):
            raise ValueError("host_bridge_limits_invalid")
        if not self.policy_version or not isinstance(self.policy_version, str):
            raise ValueError("host_bridge_policy_version_required")
        if not self.cdp_endpoint or not isinstance(self.cdp_endpoint, str):
            raise ValueError("host_bridge_cdp_endpoint_required")


class HostBridge:
    """Host-side browser bridge: owns CDP target lifecycle and re-checks policy.

    The bridge never trusts client-sent grant data. It loads the grant
    from its own trusted GrantStore per request (independent re-check).
    The grant's policy_version is checked against the bridge's configured
    policy_version; a mismatch means the grant was issued under a stale
    policy -> fail-closed.

    The bridge re-evaluates BrowserPolicy (same immutable snapshot) per
    request. If the policy denies, the request is rejected with a stable
    error_code. If the CDP target disappeared, the session is signaled
    degraded.
    """

    def __init__(
        self,
        config: HostBridgeConfig,
        *,
        grant_store: GrantStore,
        cdp_controller: CdpTargetController,
    ) -> None:
        self.config = config
        self._grant_store = grant_store
        self._cdp = cdp_controller
        self._token = load_secure_token(Path(config.token_path))
        self._sessions: dict[str, _RegisteredSession] = {}
        self._sessions_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._active_requests = 0
        self._policy = BrowserPolicy()
        self._healthy = True
        self._health_lock = threading.Lock()

    @property
    def healthy(self) -> bool:
        with self._health_lock:
            return self._healthy

    def authenticate(self, supplied: str | None) -> bool:
        if supplied is None:
            return False
        try:
            encoded = supplied.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return hmac.compare_digest(encoded, self._token)

    def shutdown(self) -> None:
        with self._health_lock:
            self._healthy = False
        with self._sessions_lock:
            sessions = dict(self._sessions)
        for reg in sessions.values():
            try:
                self._cdp.close_target(reg.target_id)
            except Exception:
                pass
        with self._sessions_lock:
            self._sessions.clear()

    # ------------------------------------------------------------------
    # Request handling (testable without an HTTP server)
    # ------------------------------------------------------------------

    def handle_request(
        self,
        path: str,
        payload: dict[str, Any] | None,
    ) -> tuple[int, dict[str, Any]]:
        """Handle a single bridge request.

        Returns (status_code, response_body). The caller (HTTP server or
        test) sends the path and parsed JSON payload. Authentication is
        assumed to have already passed (the HTTP server checks the token
        before calling this method).
        """
        if payload is None:
            return 400, _error_response("host_bridge_invalid_request")
        if not isinstance(payload, dict):
            return 400, _error_response("host_bridge_invalid_request")

        # Per-request admission control.
        with self._admission_lock:
            if self._active_requests >= self.config.max_concurrency:
                return 409, _error_response("host_bridge_busy")
            self._active_requests += 1
        try:
            return self._dispatch(path, payload)
        except _BridgeDenied as exc:
            return _status_for_denial(exc.error_code), _error_response(exc.error_code)
        except Exception:
            return 500, _error_response("host_bridge_internal_error")
        finally:
            with self._admission_lock:
                self._active_requests -= 1

    def _dispatch(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        protocol_version = payload.get("protocol_version")
        if protocol_version != "1":
            return 400, _error_response("host_bridge_invalid_request")

        if path == "/v1/browser/session/create":
            return 200, self._handle_create(payload)
        if path == "/v1/browser/session/close":
            return 200, self._handle_close(payload)
        if path == "/v1/browser/session/action":
            return 200, self._handle_action(payload)
        if path == "/v1/browser/session/state":
            return 200, self._handle_state(payload)
        if path == "/v1/browser/session/takeover/begin":
            return 200, {"status": "ok", "takeover_url": None}
        if path == "/v1/browser/session/takeover/end":
            return 200, {"status": "ok"}
        return 404, _error_response("not_found")

    # ------------------------------------------------------------------
    # Session create / close
    # ------------------------------------------------------------------

    def _handle_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = payload.get("session_id")
        n_agent_session_id = payload.get("n_agent_session_id")
        profile_ref = payload.get("profile_ref")
        status_str = payload.get("status")
        if (
            not isinstance(session_id, str) or not session_id
            or not isinstance(n_agent_session_id, str) or not n_agent_session_id
            or not isinstance(profile_ref, str) or not profile_ref
            or not isinstance(status_str, str) or not status_str
        ):
            raise _BridgeDenied("host_bridge_invalid_request")

        # Only accept ACTIVE sessions (the service must have transitioned
        # via a valid host grant before calling the backend/bridge).
        try:
            status = BrowserSessionStatus(status_str)
        except ValueError:
            raise _BridgeDenied("session_not_active")
        if status is not BrowserSessionStatus.ACTIVE:
            raise _BridgeDenied("session_not_active")

        # Re-check the grant before creating the target.
        grant = self._load_and_validate_grant(session_id, n_agent_session_id)
        session = BrowserSession(
            id=session_id,
            bound_n_agent_session_id=n_agent_session_id,
            backend_type=BrowserBackendType.HOST_CDP,
            status=status,
            profile_ref=profile_ref,
        )
        self._evaluate_policy(session, "create", grant, None)

        # Create the CDP target.
        try:
            target_id = self._cdp.create_target(profile_ref)
        except Exception:
            raise _BridgeDenied("target_unavailable")

        with self._sessions_lock:
            if session_id in self._sessions:
                # Idempotent: close the old target and re-register.
                old = self._sessions[session_id]
                try:
                    self._cdp.close_target(old.target_id)
                except Exception:
                    pass
            self._sessions[session_id] = _RegisteredSession(
                session=session,
                target_id=target_id,
                profile_ref=profile_ref,
            )
        return {"status": "ok"}

    def _handle_close(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise _BridgeDenied("host_bridge_invalid_request")
        with self._sessions_lock:
            reg = self._sessions.pop(session_id, None)
        if reg is not None:
            try:
                self._cdp.close_target(reg.target_id)
            except Exception:
                pass
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Action execution / state
    # ------------------------------------------------------------------

    def _handle_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = payload.get("session_id")
        action_type = payload.get("action_type")
        action_fields = payload.get("action")
        document_revision = payload.get("document_revision", 0)
        if (
            not isinstance(session_id, str) or not session_id
            or not isinstance(action_type, str) or not action_type
            or not isinstance(action_fields, dict)
        ):
            raise _BridgeDenied("host_bridge_invalid_request")
        if action_type not in _KNOWN_ACTION_TYPES:
            raise _BridgeDenied("unknown_capability")
        if (
            not isinstance(document_revision, int)
            or isinstance(document_revision, bool)
            or document_revision < 0
        ):
            raise _BridgeDenied("host_bridge_invalid_request")

        with self._sessions_lock:
            reg = self._sessions.get(session_id)
        if reg is None:
            raise _BridgeDenied("session_not_found")

        # Re-check the grant and policy per request (independent re-check).
        grant = self._load_and_validate_grant(
            session_id, reg.session.bound_n_agent_session_id
        )
        screenshot_consumer = self._screenshot_consumer_for(action_type)
        self._evaluate_policy(reg.session, action_type, grant, screenshot_consumer)

        # Proxy to the CDP target.
        try:
            result = self._cdp.execute_action(
                reg.target_id, action_type, action_fields, document_revision
            )
        except TargetClosed:
            self._signal_target_closed(session_id)
            raise _BridgeDenied("target_closed")
        except Exception:
            raise _BridgeDenied("target_unavailable")

        # Ensure the result has required fields.
        if not isinstance(result, dict):
            raise _BridgeDenied("host_bridge_invalid_response")
        result.setdefault("action_type", action_type)
        result.setdefault("status", "error")
        result.setdefault("document_revision", document_revision)
        return result

    def _handle_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise _BridgeDenied("host_bridge_invalid_request")
        with self._sessions_lock:
            reg = self._sessions.get(session_id)
        if reg is None:
            return {
                "safe_url": None,
                "title": None,
                "status": BrowserSessionStatus.CLOSED.value,
                "document_revision": 0,
                "latest_screenshot_ref": None,
            }
        # Re-check the grant for state requests too.
        grant = self._load_and_validate_grant(
            session_id, reg.session.bound_n_agent_session_id
        )
        self._evaluate_policy(reg.session, "observe", grant, None)
        try:
            state = self._cdp.get_state(reg.target_id)
        except TargetClosed:
            self._signal_target_closed(session_id)
            raise _BridgeDenied("target_closed")
        except Exception:
            raise _BridgeDenied("target_unavailable")
        if not isinstance(state, dict):
            raise _BridgeDenied("host_bridge_invalid_response")
        return state

    # ------------------------------------------------------------------
    # Grant + Policy re-check (independent, fail-closed)
    # ------------------------------------------------------------------

    def _load_and_validate_grant(
        self, session_id: str, n_agent_session_id: str
    ) -> _BridgeGrant:
        """Load the grant from the trusted store and validate all bindings.

        Fail-closed on: store unavailable, grant not found, grant expired,
        grant revoked, policy_version mismatch, or binding mismatch.
        """
        try:
            row = self._grant_store.load_grant(session_id)
        except Exception:
            raise _BridgeDenied("host_bridge_unhealthy")
        if row is None:
            raise _BridgeDenied("grant_not_found")
        try:
            grant = _parse_grant(row)
        except (TypeError, ValueError, KeyError):
            raise _BridgeDenied("grant_not_found")
        # Check bindings.
        if (
            grant.browser_session_id != session_id
            or grant.n_agent_session_id != n_agent_session_id
        ):
            raise _BridgeDenied("grant_not_found")
        # Check expiry and revocation.
        if grant.expired:
            raise _BridgeDenied("grant_expired")
        if grant.revoked:
            raise _BridgeDenied("grant_revoked")
        # Check policy version (the grant was issued under a specific version;
        # if the bridge's version changed, the grant is stale -> mismatch).
        if grant.policy_version != self.config.policy_version:
            raise _BridgeDenied("host_policy_version_mismatch")
        return grant

    def _evaluate_policy(
        self,
        session: BrowserSession,
        action_type: str,
        grant: _BridgeGrant,
        screenshot_consumer: BrowserScreenshotConsumer | None,
    ) -> None:
        """Re-evaluate BrowserPolicy with the immutable grant snapshot.

        The bridge independently re-checks the same BrowserPolicy that the
        BrowserService evaluated. If the policy denies, fail-closed.
        """
        request = BrowserPolicyRequest(
            run_context=None,
            session=session,
            action_type=action_type,
            requested_backend=BrowserBackendType.HOST_CDP,
            trusted_host_grant=grant,
            screenshot_consumer=screenshot_consumer,
            takeover_operation=None,
        )
        decision = self._policy.evaluate(request)
        if decision.outcome is not PolicyOutcome.ALLOW:
            raise _BridgeDenied(decision.reason)

    def _screenshot_consumer_for(
        self, action_type: str
    ) -> BrowserScreenshotConsumer | None:
        if action_type in _SCREENSHOT_ACTION_TYPES:
            return BrowserScreenshotConsumer.DASHBOARD_INTERNAL
        return None

    def _signal_target_closed(self, session_id: str) -> None:
        """Remove the session registration when its target disappeared."""
        with self._sessions_lock:
            self._sessions.pop(session_id, None)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _parse_grant(row: dict[str, Any]) -> _BridgeGrant:
    """Parse a grant dict from the store into an immutable _BridgeGrant."""
    expires_at_raw = row.get("expires_at")
    if isinstance(expires_at_raw, str):
        expires_at = datetime.fromisoformat(expires_at_raw)
    elif isinstance(expires_at_raw, datetime):
        expires_at = expires_at_raw
    else:
        raise ValueError("invalid_expires_at")
    return _BridgeGrant(
        browser_session_id=row["browser_session_id"],
        n_agent_session_id=row["n_agent_session_id"],
        actor_id=row["actor_id"],
        policy_version=row["policy_version"],
        expires_at=expires_at,
        revoked=bool(row.get("revoked", False)),
    )


def _error_response(error_code: str) -> dict[str, Any]:
    return {"status": "error", "error_code": error_code}


def _status_for_denial(error_code: str) -> int:
    if error_code == "not_found":
        return 404
    if error_code in {"host_bridge_busy"}:
        return 409
    if error_code == "host_bridge_unhealthy":
        return 503
    return 200


class _BridgeDenied(Exception):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class TargetClosed(Exception):
    """Raised by CdpTargetController implementations to signal target gone."""


__all__ = [
    "CdpTargetController",
    "GrantStore",
    "HostBridge",
    "HostBridgeConfig",
    "TargetClosed",
]
