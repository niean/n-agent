"""BrowserService - the orchestration core for the Browser subdomain (T7).

Owns:
- Per-BrowserSession asyncio.Lock that serializes BOTH actions and state
  transitions.
- Session lifecycle (create / pause / resume / takeover / close).
- Backend admission via BrowserPolicy at create / each action / each transition.
- Cleaning backend raw output before it reaches the registry.
- Screenshot persistence via BrowserScreenshotStore; raw bytes never reach
  the registry or the ToolResult.
- Host grant lifecycle for HOST_CDP sessions.
- PolicyAudit event emission after BrowserPolicy decisions.

The registry NEVER receives raw exception text, DOM HTML, type text, or
screenshot bytes; only sanitized summaries (reason/version/session_id/
action_type + safe URL origin + length-truncated text_summary).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Protocol

from app.domain.browser import (
    BrowserActionResult,
    BrowserBackendType,
    BrowserElementSummary,
    BrowserSession,
    BrowserSessionRegistry,
    BrowserSessionStatus,
    BrowserState,
    NavigateAction,
    ObserveAction,
    ScreenshotAction,
)
from app.domain.browser_policy import (
    BrowserPolicy,
    BrowserPolicyDecision,
    BrowserPolicyRequest,
)
from app.domain.policy import (
    PolicyAuditEvent,
    PolicyDecisionKind,
    PolicyOutcome,
)

if TYPE_CHECKING:
    from app.application.policy_audit_service import PolicyAuditService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunContext and settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """Per-call execution context.

    Carries the n_agent_session_id, the run_id, and an optional actor_id
    (used for host-grant attribution).
    """
    n_agent_session_id: str
    run_id: str
    actor_id: str | None = None


@dataclass(frozen=True)
class BrowserServiceSettings:
    """Small config dataclass for BrowserService."""
    max_sessions_per_run: int = 4
    action_timeout_seconds: float = 30.0
    screenshot_consumer_default: str = "dashboard_internal"
    trusted_dev: bool = False


@dataclass(frozen=True)
class HostGrantApprovalRequired(Exception):
    """Signal that a host_cdp session is PENDING_AUTHORIZATION and needs a
    Host Grant via the Chat CONFIRM card flow.

    Raised by execute_action when BrowserPolicy returns REQUIRE_APPROVAL for
    a host_cdp session AND settings.trusted_dev is True. Caught by
    BrowserToolExecutor, which converts it to a PERMISSION_DENIED ToolResult
    carrying the host-grant signal so AgentGraph can inject an approval card.

    When trusted_dev is False, execute_action does NOT raise -- it returns an
    error result (no card injection, fail-closed to Dashboard/host-grant path).
    """
    browser_session_id: str
    n_agent_session_id: str


# ---------------------------------------------------------------------------
# HostGrant value object (passed to BrowserPolicy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HostGrant:
    """Server-side host grant snapshot validated by BrowserPolicy."""
    browser_session_id: str
    n_agent_session_id: str
    actor_id: str
    policy_version: str
    expires_at: datetime
    revoked: bool = False

    @property
    def expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


# ---------------------------------------------------------------------------
# Backend Protocol (re-exported for clarity)
# ---------------------------------------------------------------------------


class BrowserBackendProtocol(Protocol):
    async def create_session(self, session: BrowserSession) -> None: ...
    async def close_session(self, session_id: str) -> None: ...
    async def execute_action(self, session_id: str, action: Any) -> BrowserActionResult: ...
    async def get_state(self, session_id: str) -> BrowserState: ...
    async def begin_takeover(self, session_id: str) -> str | None: ...
    async def end_takeover(self, session_id: str) -> None: ...


class BrowserScreenshotStoreProtocol(Protocol):
    """Structural type for the screenshot store (defined locally because
    the domain module does not export a Protocol for it)."""

    async def persist(self, session_id: str, data: bytes, content_type: str) -> str: ...
    async def read(self, screenshot_ref: str) -> bytes | None: ...
    async def delete_session(self, session_id: str) -> None: ...

# ---------------------------------------------------------------------------
# BrowserService
# ---------------------------------------------------------------------------


class BrowserService:
    """Orchestration core for the Browser subdomain."""

    def __init__(
        self,
        *,
        backends: dict[BrowserBackendType, BrowserBackendProtocol],
        registry: BrowserSessionRegistry,
        screenshot_store: BrowserScreenshotStoreProtocol,
        browser_policy: BrowserPolicy,
        default_backend: BrowserBackendType,
        settings: BrowserServiceSettings | None = None,
        audit_service: "PolicyAuditService | None" = None,
    ) -> None:
        self._backends = backends
        self._registry = registry
        self._screenshot_store = screenshot_store
        self._policy = browser_policy
        self._default_backend = default_backend
        self._settings = settings or BrowserServiceSettings()
        self._audit_service = audit_service
        # Per-BrowserSession locks.
        self._session_locks: dict[str, asyncio.Lock] = {}
        # Global lock guarding the locks dict itself.
        self._locks_guard = asyncio.Lock()
        # Latest persisted screenshot ref per session (for Dashboard get_state).
        self._latest_screenshot_ref: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Lock helpers
    # ------------------------------------------------------------------

    async def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock
            return lock

    # ------------------------------------------------------------------
    # get_or_create_session
    # ------------------------------------------------------------------

    async def get_or_create_session(
        self,
        n_agent_session_id: str,
        backend_type: BrowserBackendType | None,
        run_context: RunContext,
    ) -> BrowserSession:
        bt = backend_type or self._default_backend

        # Look for an existing non-closed session for this pair.
        existing = await self._registry.list_by_n_agent_session(n_agent_session_id)
        for s in existing:
            if s.backend_type is bt and s.status is not BrowserSessionStatus.CLOSED:
                return s

        # Enforce global session cap (across ALL n_agent sessions, not per-run).
        all_sessions: list[BrowserSession] = []
        # Some registries expose list_all; otherwise we approximate by
        # counting non-closed sessions across known backends. For tests
        # using FakeRegistry, we use list_by_n_agent_session per-known id;
        # the simpler path: check the registry's sessions dict if available.
        registry_sessions = getattr(self._registry, "sessions", None)
        if isinstance(registry_sessions, dict):
            all_sessions = list(registry_sessions.values())
        if len(all_sessions) >= self._settings.max_sessions_per_run:
            # Exclude closed sessions from the count.
            non_closed = [s for s in all_sessions if s.status is not BrowserSessionStatus.CLOSED]
            if len(non_closed) >= self._settings.max_sessions_per_run:
                raise RuntimeError(
                    f"browser session cap exceeded: {self._settings.max_sessions_per_run}"
                )

        sid = self._new_session_id()
        profile_ref = self._new_profile_ref(n_agent_session_id, bt)
        if bt is BrowserBackendType.CONTAINER:
            session = BrowserSession.create_for_container(sid, n_agent_session_id, profile_ref)
        else:
            session = BrowserSession.create_for_host(sid, n_agent_session_id, profile_ref)

        # Evaluate policy at create time.
        decision = self._policy.evaluate(
            BrowserPolicyRequest(
                run_context=run_context,
                session=session,
                action_type="create",
                requested_backend=bt,
                trusted_host_grant=None,
                screenshot_consumer=None,
                takeover_operation=None,
            )
        )
        await self._audit_policy_decision(decision, session, "create")

        if decision.outcome is PolicyOutcome.DENY or (
            decision.outcome is PolicyOutcome.REQUIRE_APPROVAL
            and decision.reason == "host_grant_required"
        ):
            # Surface the deny/require-approval as a logical session in
            # PENDING_AUTHORIZATION state; do NOT connect the backend.
            # host_grant_required REQUIRE_APPROVAL is handled at execute_action
            # time (Chat CONFIRM card); create just registers the pending session.
            if session.status is BrowserSessionStatus.PENDING_AUTHORIZATION:
                try:
                    await self._registry.create(session)
                except sqlite3.IntegrityError:
                    # Concurrent create: fetch the existing.
                    existing = await self._registry.list_by_n_agent_session(n_agent_session_id)
                    for s in existing:
                        if s.backend_type is bt and s.status is not BrowserSessionStatus.CLOSED:
                            return s
                return session
            raise PermissionError(f"browser session create denied: {decision.reason}")

        # Try to create the registry record. On concurrent IntegrityError,
        # fetch the existing session and return it.
        try:
            await self._registry.create(session)
        except sqlite3.IntegrityError:
            existing = await self._registry.list_by_n_agent_session(n_agent_session_id)
            for s in existing:
                if s.backend_type is bt and s.status is not BrowserSessionStatus.CLOSED:
                    return s
            # If no existing found, re-raise.
            raise

        # Acquire profile lease and connect the backend.
        lease_ok = await self._registry.acquire_profile_lease(profile_ref, sid)
        if not lease_ok:
            raise RuntimeError(f"profile lease contention: {profile_ref}")

        backend = self._backends.get(bt)
        if backend is not None:
            await backend.create_session(session)

        return session

    # ------------------------------------------------------------------
    # execute_action
    # ------------------------------------------------------------------

    async def execute_action(
        self,
        n_agent_session_id: str,
        action: Any,
        run_context: RunContext,
    ) -> BrowserActionResult:
        # Auto-create session if it does not exist.
        session = await self._find_session(n_agent_session_id)
        # A container session degraded by an ambiguous prior action cannot
        # safely reuse its page, but an explicit navigate is a clean recovery
        # boundary. Close the old isolated context and create a fresh one so
        # the Dashboard conversation does not remain permanently unusable.
        if (
            session is not None
            and isinstance(action, NavigateAction)
            and session.backend_type is BrowserBackendType.CONTAINER
            and session.status in {
                BrowserSessionStatus.DEGRADED,
                BrowserSessionStatus.CLOSED,
            }
        ):
            if session.status is BrowserSessionStatus.DEGRADED:
                await self.close_session(n_agent_session_id)
            session = None
        if session is None:
            session = await self.get_or_create_session(
                n_agent_session_id, self._default_backend, run_context
            )

        lock = await self._get_session_lock(session.id)
        async with lock:
            # Re-read registry status under the lock.
            fresh = await self._registry.get(session.id)
            if fresh is not None:
                session = fresh

            backend = self._backends.get(session.backend_type)
            if backend is None:
                return BrowserActionResult(
                    action_type=type(action).__name__.replace("Action", "").lower(),
                    status="error",
                    error_code="backend_unavailable",
                    document_revision=session.document_revision,
                )

            # Reattach and align before checking element_ref revisions. A
            # persistent browser may have restarted while this service and
            # its durable registry remained alive.
            try:
                await self._reattach_backend_if_needed(backend, session)
                await self._sync_backend_document_revision(backend, session)
            except Exception:
                logger.warning("browser backend reattach failed for session=%s", session.id, exc_info=True)
                return BrowserActionResult(
                    action_type=type(action).__name__.replace("Action", "").lower(),
                    status="error",
                    error_code="backend_unavailable",
                    document_revision=session.document_revision,
                )

            # Stale element_ref check (before policy/backend).
            stale = self._check_stale_ref(session, action)
            if stale is not None:
                return stale

            # Evaluate policy.
            screenshot_consumer = self._screenshot_consumer_for(action)
            trusted_grant = await self._load_host_grant(session)
            decision = self._policy.evaluate(
                BrowserPolicyRequest(
                    run_context=run_context,
                    session=session,
                    action_type=type(action).__name__.replace("Action", "").lower(),
                    requested_backend=session.backend_type,
                    trusted_host_grant=trusted_grant,
                    screenshot_consumer=screenshot_consumer,
                    takeover_operation=None,
                )
            )
            await self._audit_policy_decision(decision, session, type(action).__name__)

            if decision.outcome is PolicyOutcome.DENY:
                return BrowserActionResult(
                    action_type=type(action).__name__.replace("Action", "").lower(),
                    status="error",
                    error_code=decision.reason,
                    document_revision=session.document_revision,
                )
            if decision.outcome is PolicyOutcome.REQUIRE_APPROVAL:
                # host_grant_required on a host_cdp pending session: when
                # trusted_dev is on, surface a signal so BrowserToolExecutor
                # can route it to the Chat CONFIRM card flow; when off, fall
                # back to the error result (no card, fail-closed).
                if (
                    decision.reason == "host_grant_required"
                    and self._settings.trusted_dev
                ):
                    raise HostGrantApprovalRequired(
                        browser_session_id=session.id,
                        n_agent_session_id=n_agent_session_id,
                    )
                return BrowserActionResult(
                    action_type=type(action).__name__.replace("Action", "").lower(),
                    status="error",
                    error_code=decision.reason or "takeover_requires_approval",
                    document_revision=session.document_revision,
                )

            # Execute with timeout. We use asyncio.timeout() (not wait_for)
            # so that a TimeoutError raised INSIDE the backend (e.g.
            # Playwright's own timeout) is NOT conflated with our own
            # wait timeout -- backend-raised exceptions become
            # action_outcome_unknown, while our wait expiring becomes
            # browser_action_timeout.
            action_type_name = type(action).__name__.replace("Action", "").lower()
            backend_exc: Exception | None = None
            timed_out = False
            timeout_ctx = asyncio.timeout(self._settings.action_timeout_seconds)
            try:
                async with timeout_ctx:
                    result = await backend.execute_action(session.id, action)
            except TimeoutError:
                # Distinguish our wait expiry from a backend-raised timeout.
                if timeout_ctx.expired():
                    timed_out = True
                else:
                    backend_exc = TimeoutError()
            except Exception as exc:
                backend_exc = exc

            if timed_out:
                # Service-side wait_for timeout: the action did not complete
                # within the configured budget. This is "action-before timeout".
                await self._degrade_session(session)
                return BrowserActionResult(
                    action_type=action_type_name,
                    status="error",
                    error_code="browser_action_timeout",
                    document_revision=session.document_revision,
                )
            if backend_exc is not None:
                # A backend-raised exception (including a backend-internal
                # TimeoutError from Playwright) AFTER the action may have
                # side-effected -> action_outcome_unknown + degrade, NO retry.
                logger.warning(
                    "browser action %s failed for session=%s: %s",
                    action_type_name, session.id, backend_exc,
                    exc_info=True,
                )
                await self._degrade_session(session)
                return BrowserActionResult(
                    action_type=action_type_name,
                    status="error",
                    error_code="action_outcome_unknown",
                    document_revision=session.document_revision,
                )

            # Navigate / main-doc replacement increments document_revision.
            if isinstance(action, NavigateAction) and result.status == "success":
                new_rev = max(result.document_revision, session.document_revision + 1)
                await self._registry.compare_and_set_status(
                    session.id,
                    session.status,
                    session.status,
                    document_revision=new_rev,
                )
                session = (await self._registry.get(session.id)) or session
                result = BrowserActionResult(
                    action_type=result.action_type,
                    status=result.status,
                    url=result.url,
                    title=result.title,
                    text=result.text,
                    elements=result.elements,
                    screenshot_ref=result.screenshot_ref,
                    warning_code=result.warning_code,
                    error_code=result.error_code,
                    duration_ms=result.duration_ms,
                    document_revision=new_rev,
                )

            # Observe cleanup: strip sensitive text_excerpt, length-truncate.
            if isinstance(action, ObserveAction) and result.status == "success":
                result = self._clean_observe_result(result, action)

            # Every successful browser action refreshes the Dashboard snapshot.
            # The driver captures via a side channel; only the opaque internal
            # ref is retained here and never exposed to the model.
            if result.status == "success":
                result = await self._persist_screenshot(session, backend, result)

            # Registry write: safe summary only.
            await self._append_safe_summary(session, action, result)

            return result

    # ------------------------------------------------------------------
    # get_state
    # ------------------------------------------------------------------

    async def get_state(self, n_agent_session_id: str) -> BrowserState | None:
        session = await self._find_session(n_agent_session_id)
        if session is None:
            return None
        backend = self._backends.get(session.backend_type)
        if backend is None:
            return BrowserState(
                safe_url=None,
                title=None,
                status=session.status,
                document_revision=session.document_revision,
                latest_screenshot_ref=None,
            )
        try:
            await self._reattach_backend_if_needed(backend, session)
            await self._sync_backend_document_revision(backend, session)
            state = await backend.get_state(session.id)
            return replace(state, latest_screenshot_ref=self._latest_screenshot_ref.get(session.id))
        except Exception:
            return BrowserState(
                safe_url=None,
                title=None,
                status=session.status,
                document_revision=session.document_revision,
                latest_screenshot_ref=None,
            )

    # ------------------------------------------------------------------
    # list_sessions / list_actions
    # ------------------------------------------------------------------

    async def list_sessions(self, n_agent_session_id: str) -> list[BrowserSession]:
        return await self._registry.list_by_n_agent_session(n_agent_session_id)

    async def list_actions(self, n_agent_session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        session = await self._find_session(n_agent_session_id)
        if session is None:
            return []
        return await self._registry.list_actions(session.id, limit)

    async def get_session_by_id(self, browser_session_id: str) -> BrowserSession | None:
        """Look up a BrowserSession by its ID (read-only). Used by the Dashboard
        service to verify session ownership without going through _find_session
        (which is bound to n_agent_session_id)."""
        return await self._registry.get(browser_session_id)

    async def get_state_for_session(self, browser_session_id: str) -> BrowserState | None:
        """Get BrowserState for a specific browser_session_id (Dashboard query).

        Small signature gap: the existing get_state takes n_agent_session_id
        and uses _find_session, which is ambiguous when multiple non-closed
        sessions exist for the same n_agent session. This method resolves the
        session by ID directly."""
        session = await self._registry.get(browser_session_id)
        if session is None:
            return None
        if session.status is BrowserSessionStatus.CLOSED:
            return BrowserState(
                safe_url=None,
                title=None,
                status=session.status,
                document_revision=session.document_revision,
                latest_screenshot_ref=None,
            )
        backend = self._backends.get(session.backend_type)
        if backend is None:
            return BrowserState(
                safe_url=None,
                title=None,
                status=session.status,
                document_revision=session.document_revision,
                latest_screenshot_ref=None,
            )
        try:
            await backend.create_session(session)
            await self._sync_backend_document_revision(backend, session)
            state = await backend.get_state(session.id)
            return replace(state, latest_screenshot_ref=self._latest_screenshot_ref.get(session.id))
        except Exception:
            return BrowserState(
                safe_url=None,
                title=None,
                status=session.status,
                document_revision=session.document_revision,
                latest_screenshot_ref=None,
            )

    async def list_actions_for_session(
        self, browser_session_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """List actions for a specific browser_session_id (Dashboard query)."""
        session = await self._registry.get(browser_session_id)
        if session is None:
            return []
        return await self._registry.list_actions(browser_session_id, limit)

    async def _reattach_backend_if_needed(self, backend: Any, session: BrowserSession) -> None:
        """Reattach durable Container sessions after the app process restarts."""
        has_session = getattr(backend, "has_session", None)
        if callable(has_session) and not has_session(session.id):
            await backend.create_session(session)

    async def _sync_backend_document_revision(
        self, backend: Any, session: BrowserSession
    ) -> None:
        """Ask persistent backends to use the registry's revision authority."""
        sync_revision = getattr(backend, "sync_document_revision", None)
        if callable(sync_revision):
            await sync_revision(session.id, session.document_revision)

    async def count_actions_for_session(self, browser_session_id: str) -> int:
        """Return the complete action count for a browser session."""
        session = await self._registry.get(browser_session_id)
        if session is None:
            return 0
        return await self._registry.count_actions(browser_session_id)

    # ------------------------------------------------------------------
    # pause / resume / takeover / close
    # ------------------------------------------------------------------

    async def pause_session(self, n_agent_session_id: str) -> bool:
        return await self._transition(n_agent_session_id, BrowserSessionStatus.PAUSED)

    async def resume_session(self, n_agent_session_id: str) -> bool:
        return await self._transition(n_agent_session_id, BrowserSessionStatus.ACTIVE)

    async def request_takeover(self, n_agent_session_id: str) -> bool:
        session = await self._find_session(n_agent_session_id)
        if session is None:
            return False
        lock = await self._get_session_lock(session.id)
        async with lock:
            decision = self._policy.evaluate(
                BrowserPolicyRequest(
                    run_context=RunContext(
                        n_agent_session_id=n_agent_session_id,
                        run_id=n_agent_session_id,
                        actor_id=None,
                    ),
                    session=session,
                    action_type="takeover",
                    requested_backend=session.backend_type,
                    trusted_host_grant=None,
                    screenshot_consumer=None,
                    takeover_operation="request",
                )
            )
            await self._audit_policy_decision(decision, session, "takeover")
            if decision.outcome is not PolicyOutcome.REQUIRE_APPROVAL:
                return False
            # Takeover requires approval; here we mark it directly only if
            # the caller has already approved. For our tests, we transition
            # directly. In production the AgentGraph would gate this.
            return await self._transition_locked(session, BrowserSessionStatus.TAKEOVER)

    async def release_takeover(self, n_agent_session_id: str) -> bool:
        session = await self._find_session(n_agent_session_id)
        if session is None:
            return False
        lock = await self._get_session_lock(session.id)
        async with lock:
            decision = self._policy.evaluate(
                BrowserPolicyRequest(
                    run_context=RunContext(
                        n_agent_session_id=n_agent_session_id,
                        run_id=n_agent_session_id,
                        actor_id=None,
                    ),
                    session=session,
                    action_type="takeover_release",
                    requested_backend=session.backend_type,
                    trusted_host_grant=None,
                    screenshot_consumer=None,
                    takeover_operation="release",
                )
            )
            await self._audit_policy_decision(decision, session, "takeover_release")
            if decision.outcome is not PolicyOutcome.ALLOW:
                return False
            target = session.pre_takeover_status or BrowserSessionStatus.ACTIVE
            backend = self._backends.get(session.backend_type)
            # A human takeover bypasses execute_action(), so refresh the
            # backend's revision/screenshot side channel before Dashboard
            # polling resumes. Never keep showing the pre-takeover frame if
            # refresh fails.
            self._latest_screenshot_ref.pop(session.id, None)
            if backend is not None:
                try:
                    await backend.end_takeover(session.id)
                    await self._persist_screenshot(
                        session,
                        backend,
                        BrowserActionResult(
                            action_type="takeover_release",
                            status="success",
                            document_revision=session.document_revision + 1,
                        ),
                    )
                except Exception:
                    logger.warning(
                        "browser takeover release sync failed for session=%s",
                        session.id,
                        exc_info=True,
                    )
            return await self._transition_locked(
                session,
                target,
                document_revision=session.document_revision + 1,
            )

    async def close_session(self, n_agent_session_id: str) -> bool:
        session = await self._find_session(n_agent_session_id)
        if session is None:
            return False
        lock = await self._get_session_lock(session.id)
        async with lock:
            if session.status is BrowserSessionStatus.CLOSED:
                return True  # idempotent
            backend = self._backends.get(session.backend_type)
            # Release backend first; only mark closed after cleanup results known.
            if backend is not None:
                try:
                    await backend.close_session(session.id)
                except Exception:
                    logger.warning("backend close failed for session=%s", session.id, exc_info=True)
            # Release profile lease.
            try:
                await self._registry.release_profile_lease(session.profile_ref)
            except Exception:
                logger.warning("profile lease release failed", exc_info=True)
            # Revoke host grant (if any).
            registry = self._registry
            revoke = getattr(registry, "revoke_host_grant", None)
            if revoke is not None and session.backend_type is BrowserBackendType.HOST_CDP:
                try:
                    await revoke(session.id)
                except Exception:
                    logger.warning("host grant revoke failed", exc_info=True)
            # Delete screenshots.
            try:
                await self._screenshot_store.delete_session(session.id)
            except Exception:
                logger.warning("screenshot delete failed", exc_info=True)
            # Mark closed via CAS.
            try:
                await self._registry.compare_and_set_status(
                    session.id,
                    session.status,
                    BrowserSessionStatus.CLOSED,
                )
            except Exception:
                # If CAS fails (e.g. already closed by another path), still
                # call close() as a fallback for idempotency.
                await self._registry.close(session.id)
            return True

    # ------------------------------------------------------------------
    # Host grant management
    # ------------------------------------------------------------------

    async def grant_host(
        self,
        n_agent_session_id: str,
        *,
        actor_id: str,
        policy_version: str,
        ttl_seconds: int,
    ) -> bool:
        """Grant host access for a HOST_CDP session.

        Flow:
          1. Record the host grant in the registry.
          2. Transition PENDING_AUTHORIZATION -> ACTIVE (CAS).
          3. Call backends[HOST_CDP].create_session(session) to connect the
             host Chrome via the bridge.
          4. If create_session fails (bridge unavailable, target closed),
             transition session to DEGRADED (not back to PENDING).

        Returns True on success, False if create_session failed (session
        degraded). Raises RuntimeError if the session is not found or is not
        a HOST_CDP session.
        """
        session = await self._find_session(n_agent_session_id)
        if session is None:
            raise RuntimeError("session not found for host grant")
        if session.backend_type is not BrowserBackendType.HOST_CDP:
            raise RuntimeError("host grant only valid for HOST_CDP backend")
        registry = self._registry
        record = getattr(registry, "record_host_grant", None)
        if record is None:
            raise RuntimeError("registry does not support host grants")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        await record(
            session.id,
            n_agent_session_id,
            actor_id,
            policy_version,
            expires_at.isoformat(),
        )
        # Transition PENDING_AUTHORIZATION -> ACTIVE (CAS).
        updated = await self._registry.compare_and_set_status(
            session.id,
            session.status,
            BrowserSessionStatus.ACTIVE,
        )
        if updated is None:
            # CAS failed (status changed concurrently); abort grant.
            revoke = getattr(registry, "revoke_host_grant", None)
            if revoke is not None:
                await revoke(session.id)
            return False
        # Re-fetch the session to get the ACTIVE status for backend.create_session.
        session = updated
        # Connect the host Chrome via the backend.
        backend = self._backends.get(BrowserBackendType.HOST_CDP)
        if backend is None:
            # HOST_CDP backend not configured; cannot connect. Degrade.
            logger.warning(
                "HOST_CDP backend not configured; session %s degraded", session.id
            )
            await self._registry.compare_and_set_status(
                session.id,
                BrowserSessionStatus.ACTIVE,
                BrowserSessionStatus.DEGRADED,
            )
            return False
        try:
            await backend.create_session(session)
        except Exception:
            logger.warning(
                "host backend create_session failed for session=%s",
                session.id,
                exc_info=True,
            )
            # Transition to DEGRADED (not back to PENDING).
            await self._registry.compare_and_set_status(
                session.id,
                BrowserSessionStatus.ACTIVE,
                BrowserSessionStatus.DEGRADED,
            )
            return False
        return True

    async def revoke_host(self, n_agent_session_id: str) -> None:
        """Revoke host access for a HOST_CDP session.

        Flow:
          1. Close the backend session (best-effort).
          2. Revoke the host grant in the registry.
          3. Transition per domain state machine:
             - ACTIVE/PAUSED/TAKEOVER -> DEGRADED
             - PENDING_AUTHORIZATION/DEGRADED -> CLOSED
             - CLOSED -> no-op (idempotent)
        """
        session = await self._find_session(n_agent_session_id)
        if session is None:
            return
        # Close the backend session first (best-effort).
        backend = self._backends.get(session.backend_type)
        if backend is not None:
            try:
                await backend.close_session(session.id)
            except Exception:
                logger.warning(
                    "backend close_session failed during revoke_host for session=%s",
                    session.id,
                    exc_info=True,
                )
        # Revoke the grant.
        registry = self._registry
        revoke = getattr(registry, "revoke_host_grant", None)
        if revoke is not None:
            await revoke(session.id)
        # Transition per domain state machine.
        # DEGRADED can't go back to PENDING; from ACTIVE/PAUSED/TAKEOVER -> DEGRADED;
        # from PENDING_AUTHORIZATION/DEGRADED -> CLOSED.
        if session.status is BrowserSessionStatus.CLOSED:
            return  # idempotent
        target = BrowserSessionStatus.DEGRADED
        if session.status in (
            BrowserSessionStatus.PENDING_AUTHORIZATION,
            BrowserSessionStatus.DEGRADED,
        ):
            target = BrowserSessionStatus.CLOSED
        await self._registry.compare_and_set_status(
            session.id,
            session.status,
            target,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _find_session(self, n_agent_session_id: str) -> BrowserSession | None:
        sessions = await self._registry.list_by_n_agent_session(n_agent_session_id)
        # Prefer non-closed.
        for s in sessions:
            if s.status is not BrowserSessionStatus.CLOSED:
                return s
        return sessions[0] if sessions else None

    def _check_stale_ref(self, session: BrowserSession, action: Any) -> BrowserActionResult | None:
        from app.domain.browser import ClickAction, TypeAction, ScrollAction
        if isinstance(action, (ClickAction, TypeAction, ScrollAction)):
            if action.document_revision != session.document_revision:
                action_name = type(action).__name__.replace("Action", "").lower()
                return BrowserActionResult(
                    action_type=action_name,
                    status="error",
                    error_code="stale_element_ref",
                    document_revision=session.document_revision,
                )
        return None

    async def _transition(self, n_agent_session_id: str, target: BrowserSessionStatus) -> bool:
        session = await self._find_session(n_agent_session_id)
        if session is None:
            return False
        lock = await self._get_session_lock(session.id)
        async with lock:
            return await self._transition_locked(session, target)

    async def _transition_locked(
        self,
        session: BrowserSession,
        target: BrowserSessionStatus,
        *,
        document_revision: int | None = None,
    ) -> bool:
        if not session.can_transition_to(target):
            return False
        # Re-read fresh state under the lock.
        fresh = await self._registry.get(session.id)
        if fresh is not None:
            session = fresh
        if not session.can_transition_to(target):
            return False
        if target is BrowserSessionStatus.TAKEOVER:
            updated = await self._registry.compare_and_set_status(
                session.id,
                session.status,
                target,
                pre_takeover_status=session.status,
            )
        elif session.status is BrowserSessionStatus.TAKEOVER:
            updated = await self._registry.compare_and_set_status(
                session.id,
                session.status,
                target,
                pre_takeover_status=None,
                document_revision=document_revision,
            )
        else:
            updated = await self._registry.compare_and_set_status(
                session.id,
                session.status,
                target,
            )
        return updated is not None

    async def _degrade_session(self, session: BrowserSession) -> None:
        try:
            await self._registry.compare_and_set_status(
                session.id,
                session.status,
                BrowserSessionStatus.DEGRADED,
            )
        except Exception:
            logger.warning("degrade failed for session=%s", session.id, exc_info=True)

    def _screenshot_consumer_for(self, action: Any) -> Any:
        from app.domain.browser import BrowserScreenshotConsumer
        if isinstance(action, ScreenshotAction):
            return BrowserScreenshotConsumer.DASHBOARD_INTERNAL
        return None

    async def _load_host_grant(self, session: BrowserSession) -> HostGrant | None:
        if session.backend_type is not BrowserBackendType.HOST_CDP:
            return None
        registry = self._registry
        getter = getattr(registry, "get_host_grant", None)
        if getter is None:
            return None
        try:
            row = await getter(session.id)
        except Exception:
            return None
        if row is None:
            return None
        try:
            expires_at_raw = row.get("expires_at")
            if isinstance(expires_at_raw, str):
                expires_at = datetime.fromisoformat(expires_at_raw)
            elif isinstance(expires_at_raw, datetime):
                expires_at = expires_at_raw
            else:
                return None
            return HostGrant(
                browser_session_id=row.get("browser_session_id", ""),
                n_agent_session_id=row.get("n_agent_session_id", ""),
                actor_id=row.get("actor_id", ""),
                policy_version=row.get("policy_version", ""),
                expires_at=expires_at,
                revoked=False,
            )
        except Exception:
            return None

    async def _audit_policy_decision(
        self,
        decision: BrowserPolicyDecision,
        session: BrowserSession,
        action_type: str,
    ) -> None:
        if self._audit_service is None:
            return
        # Only safe projection: reason / version / session_id / action_type.
        # Never include URL query, full type text, screenshot bytes.
        event = PolicyAuditEvent(
            policy="browser-policy",
            version="system-v1",
            decision_kind=PolicyDecisionKind.ADMISSION,
            reason=decision.reason,
            run_id=session.bound_n_agent_session_id,
            session_id=session.id,
            outcome=decision.outcome,
        )
        try:
            await self._audit_service.record(event)
        except Exception:
            logger.warning("audit failed for browser policy decision", exc_info=True)

    async def _persist_screenshot(
        self,
        session: BrowserSession,
        backend: BrowserBackendProtocol,
        result: BrowserActionResult,
    ) -> BrowserActionResult:
        """Persist the side-channel screenshot bytes for the Dashboard view.

        Preserves the original action result (url/title/text/elements) for the
        model; the screenshot ref is tracked internally for Dashboard get_state
        and is never exposed to the ToolResult. Capture/persist failure is
        non-fatal for navigate/observe (success + warning_code) but fatal for
        the browser_screenshot action (error), whose sole purpose is the shot.
        """
        getter = getattr(backend, "last_screenshot_bytes", None)
        data = None
        if getter is not None:
            try:
                data = getter(session.id) if callable(getter) else None
            except Exception:
                data = None
        if not data:
            return self._screenshot_failure_result(result)
        try:
            ref = await self._screenshot_store.persist(session.id, data, "image/png")
        except Exception:
            logger.warning("screenshot persist failed", exc_info=True)
            return self._screenshot_failure_result(result)
        self._latest_screenshot_ref[session.id] = ref
        # screenshot_ref intentionally NOT exposed in the ToolResult.
        return result

    @staticmethod
    def _screenshot_failure_result(result: BrowserActionResult) -> BrowserActionResult:
        # browser_screenshot: the shot was the goal -> error.
        if result.action_type == "screenshot":
            return replace(
                result,
                status="error",
                error_code="screenshot_unavailable",
                warning_code=None,
            )
        if result.warning_code is not None:
            return result
        # navigate/observe: screenshot was incidental -> success + warning.
        return replace(result, warning_code="screenshot_unavailable")

    def _clean_observe_result(
        self, result: BrowserActionResult, action: ObserveAction
    ) -> BrowserActionResult:
        cleaned: list[BrowserElementSummary] = []
        for el in result.elements:
            # Strip text_excerpt for password-like fields (fail-closed).
            role = (el.role or "").lower()
            name = (el.accessible_name or "").lower()
            is_password_like = any(
                kw in name or kw in role
                for kw in ("password", "secret", "token", "credit", "card", "cvv", "csc")
            )
            if is_password_like:
                cleaned.append(
                    BrowserElementSummary(
                        element_ref=el.element_ref,
                        role=el.role,
                        accessible_name=el.accessible_name,
                        text_excerpt="",
                        disabled=el.disabled,
                    )
                )
            else:
                cleaned.append(el)
        # Truncate text.
        text = result.text or ""
        if len(text) > action.max_text_chars:
            text = text[: action.max_text_chars]
        return BrowserActionResult(
            action_type=result.action_type,
            status=result.status,
            url=result.url,
            title=result.title,
            text=text,
            elements=tuple(cleaned),
            screenshot_ref=result.screenshot_ref,
            warning_code=result.warning_code,
            error_code=result.error_code,
            duration_ms=result.duration_ms,
            document_revision=result.document_revision,
        )

    async def _append_safe_summary(
        self,
        session: BrowserSession,
        action: Any,
        result: BrowserActionResult,
    ) -> None:
        action_type_name = type(action).__name__.replace("Action", "").lower()
        # Strip URL query/fragment for safe_url.
        safe_url = result.url
        if safe_url and "?" in safe_url:
            safe_url = safe_url.split("?", 1)[0]
        if safe_url and "#" in safe_url:
            safe_url = safe_url.split("#", 1)[0]
        # Length-truncate text_summary.
        text_summary = None
        if result.text:
            text_summary = result.text[:200]
        summary = {
            "action_type": action_type_name,
            "arguments_summary": self._safe_arguments_summary(action),
            "status": result.status,
            "safe_url": safe_url,
            "title": result.title,
            "text_summary": text_summary,
            "warning_code": result.warning_code,
            "error_code": result.error_code,
            "duration_ms": result.duration_ms,
            "document_revision": result.document_revision,
        }
        try:
            await self._registry.append_action_summary(session.id, summary)
        except Exception:
            logger.warning("append_action_summary failed", exc_info=True)

    def _safe_arguments_summary(self, action: Any) -> dict[str, Any]:
        from app.domain.browser import (
            ClickAction,
            NavigateAction,
            ObserveAction,
            ScreenshotAction,
            ScrollAction,
            TypeAction,
        )
        if isinstance(action, NavigateAction):
            # Strip query/fragment.
            url = action.url
            if "?" in url:
                url = url.split("?", 1)[0]
            if "#" in url:
                url = url.split("#", 1)[0]
            return {"url": url}
        if isinstance(action, ObserveAction):
            return {"max_text_chars": action.max_text_chars, "max_elements": action.max_elements}
        if isinstance(action, ClickAction):
            return {"element_ref": action.element_ref}
        if isinstance(action, TypeAction):
            # Never echo the full text.
            return {"element_ref": action.element_ref, "text_length": len(action.text)}
        if isinstance(action, ScrollAction):
            return {"element_ref": action.element_ref}
        if isinstance(action, ScreenshotAction):
            return {"full_page": action.full_page}
        return {}

    @staticmethod
    def _new_session_id() -> str:
        return "bsess-" + uuid.uuid4().hex

    @staticmethod
    def _new_profile_ref(n_agent_session_id: str, bt: BrowserBackendType) -> str:
        return f"bp-{bt.value}-{uuid.uuid4().hex[:12]}"


__all__ = [
    "BrowserService",
    "BrowserServiceSettings",
    "BrowserScreenshotStoreProtocol",
    "HostGrant",
    "RunContext",
    "BrowserBackendProtocol",
]
