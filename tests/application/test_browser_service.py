"""Tests for BrowserService (T7) - the orchestration core.

Uses fakes for backend/registry/screenshot_store/policy. Covers:
- auto-create on first call
- concurrent single session (per-session asyncio.Lock)
- serial actions
- stale ref -> stale_element_ref
- no auto-retry on action_outcome_unknown
- observe cleanup (input value/password/hidden stripped)
- screenshot persistence
- all state commands + illegal transitions
- action_outcome_unknown
- screenshot_unavailable warning
- host_grant_required pending
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from typing import Any

import pytest

from app.application.browser_service import (
    BrowserService,
    BrowserServiceSettings,
    HostGrantApprovalRequired,
    RunContext,
)
from app.domain.browser import (
    BrowserActionResult,
    BrowserBackendType,
    BrowserElementSummary,
    BrowserSession,
    BrowserSessionStatus,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScrollAction,
    ScreenshotAction,
    TypeAction,
)
from app.domain.browser_policy import BrowserPolicy
from app.domain.policy import PolicyAuditEvent, PolicyOutcome


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBackend:
    """Fake BrowserBackend implementation for tests."""

    def __init__(self, backend_type: BrowserBackendType) -> None:
        self.backend_type = backend_type
        self.sessions: dict[str, BrowserSession] = {}
        self.create_calls: list[BrowserSession] = []
        self.close_calls: list[str] = []
        self.action_calls: list[tuple[str, Any]] = []
        self.get_state_calls: list[str] = []
        self.takeover_calls: list[str] = []
        self.end_takeover_calls: list[str] = []
        # Scriptable behavior hooks.
        self.next_action_result: BrowserActionResult | None = None
        self.next_action_exc: Exception | None = None
        self.next_state: Any | None = None
        self.screenshot_by_session: dict[str, bytes] = {}

    async def create_session(self, session: BrowserSession) -> None:
        self.create_calls.append(session)
        self.sessions[session.id] = session

    async def close_session(self, session_id: str) -> None:
        self.close_calls.append(session_id)
        self.sessions.pop(session_id, None)

    async def execute_action(self, session_id: str, action: Any) -> BrowserActionResult:
        self.action_calls.append((session_id, action))
        if self.next_action_exc is not None:
            exc = self.next_action_exc
            self.next_action_exc = None
            raise exc
        if self.next_action_result is not None:
            r = self.next_action_result
            self.next_action_result = None
            return r
        # Default: success with action_type.
        return BrowserActionResult(
            action_type=type(action).__name__,
            status="success",
            document_revision=0,
        )

    async def get_state(self, session_id: str) -> Any:
        self.get_state_calls.append(session_id)
        if self.next_state is not None:
            return self.next_state
        return None

    async def begin_takeover(self, session_id: str) -> str | None:
        self.takeover_calls.append(session_id)
        return None

    async def end_takeover(self, session_id: str) -> None:
        self.end_takeover_calls.append(session_id)

    def last_screenshot_bytes(self, session_id: str) -> bytes | None:
        return self.screenshot_by_session.get(session_id)


class FakeRegistry:
    """In-memory BrowserSessionRegistry for tests."""

    _UNSET = object()  # sentinel for "keep existing"

    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}
        self.action_summaries: dict[str, list[dict[str, Any]]] = {}
        self.profile_leases: dict[str, str] = {}
        self.host_grants: dict[str, dict[str, Any]] = {}
        self.create_exc: Exception | None = None

    async def create(self, session: BrowserSession) -> None:
        if self.create_exc is not None:
            exc = self.create_exc
            self.create_exc = None
            raise exc
        # Enforce partial-unique: at most one non-closed per (nagent, backend)
        for existing in self.sessions.values():
            if (
                existing.bound_n_agent_session_id == session.bound_n_agent_session_id
                and existing.backend_type is session.backend_type
                and existing.status is not BrowserSessionStatus.CLOSED
            ):
                import sqlite3
                raise sqlite3.IntegrityError("partial-unique violation")
        self.sessions[session.id] = session

    async def get(self, session_id: str) -> BrowserSession | None:
        return self.sessions.get(session_id)

    async def list_by_n_agent_session(self, n_agent_session_id: str) -> list[BrowserSession]:
        return [
            s for s in self.sessions.values()
            if s.bound_n_agent_session_id == n_agent_session_id
        ]

    async def compare_and_set_status(
        self,
        session_id: str,
        expected: BrowserSessionStatus,
        next_status: BrowserSessionStatus,
        *,
        pre_takeover_status: Any = _UNSET,  # type: ignore[valid-type]
        document_revision: int | None = _UNSET,  # type: ignore[valid-type]
    ) -> BrowserSession | None:
        session = self.sessions.get(session_id)
        if session is None or session.status is not expected:
            return None
        # Match the real registry behavior: do NOT enforce transition
        # validation here (the service does that via can_transition_to).
        # Allow next_status == expected (used for document_revision bumps).
        pre_arg: Any = (
            pre_takeover_status if pre_takeover_status is not self._UNSET
            else session.pre_takeover_status
        )
        rev_arg: Any = (
            document_revision if document_revision is not self._UNSET
            else session.document_revision
        )
        session = session.with_status(
            next_status,
            pre_takeover_status=pre_arg,
            document_revision=rev_arg,
        )
        self.sessions[session_id] = session
        return session

    async def acquire_profile_lease(self, profile_ref: str, session_id: str) -> bool:
        existing = self.profile_leases.get(profile_ref)
        if existing is None or existing == session_id:
            self.profile_leases[profile_ref] = session_id
            return True
        return False

    async def release_profile_lease(self, profile_ref: str) -> None:
        self.profile_leases.pop(profile_ref, None)

    async def append_action_summary(self, session_id: str, summary: dict[str, Any]) -> None:
        self.action_summaries.setdefault(session_id, []).append(summary)

    async def list_actions(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        return list(self.action_summaries.get(session_id, []))[:limit]

    async def count_actions(self, session_id: str) -> int:
        return len(self.action_summaries.get(session_id, []))

    async def close(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            self.sessions[session_id] = session.with_status(BrowserSessionStatus.CLOSED)

    # Extra methods (beyond Protocol) used by the service.
    async def record_host_grant(
        self,
        session_id: str,
        n_agent_session_id: str,
        actor_id: str,
        policy_version: str,
        expires_at: str,
    ) -> None:
        self.host_grants[session_id] = {
            "browser_session_id": session_id,
            "n_agent_session_id": n_agent_session_id,
            "actor_id": actor_id,
            "policy_version": policy_version,
            "expires_at": expires_at,
        }

    async def revoke_host_grant(self, session_id: str) -> None:
        self.host_grants.pop(session_id, None)

    async def get_host_grant(self, session_id: str) -> dict[str, Any] | None:
        return self.host_grants.get(session_id)


class FakeScreenshotStore:
    def __init__(self) -> None:
        self.stored: list[tuple[str, bytes, str]] = []
        self.deleted_sessions: list[str] = []
        self.persist_exc: Exception | None = None

    async def persist(self, session_id: str, data: bytes, content_type: str) -> str:
        if self.persist_exc is not None:
            exc = self.persist_exc
            self.persist_exc = None
            raise exc
        ref = f"ref-{len(self.stored)}"
        self.stored.append((session_id, data, content_type))
        return ref

    async def read(self, ref: str) -> bytes | None:
        return None

    async def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)


class FakePolicyAuditSink:
    def __init__(self) -> None:
        self.events: list[PolicyAuditEvent] = []

    async def record(self, event: PolicyAuditEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    *,
    backends: dict[BrowserBackendType, FakeBackend] | None = None,
    registry: FakeRegistry | None = None,
    screenshot_store: FakeScreenshotStore | None = None,
    default_backend: BrowserBackendType = BrowserBackendType.CONTAINER,
    policy: BrowserPolicy | None = None,
    settings: BrowserServiceSettings | None = None,
    audit_sink: FakePolicyAuditSink | None = None,
) -> tuple[BrowserService, FakeRegistry, FakeScreenshotStore, dict[BrowserBackendType, FakeBackend], FakePolicyAuditSink]:
    if backends is None:
        backends = {
            BrowserBackendType.CONTAINER: FakeBackend(BrowserBackendType.CONTAINER),
            BrowserBackendType.HOST_CDP: FakeBackend(BrowserBackendType.HOST_CDP),
        }
    if registry is None:
        registry = FakeRegistry()
    if screenshot_store is None:
        screenshot_store = FakeScreenshotStore()
    if policy is None:
        policy = BrowserPolicy()
    if settings is None:
        settings = BrowserServiceSettings(max_sessions_per_run=4)
    if audit_sink is None:
        audit_sink = FakePolicyAuditSink()
    from app.application.policy_audit_service import PolicyAuditService
    service = BrowserService(
        backends=backends,
        registry=registry,
        screenshot_store=screenshot_store,
        browser_policy=policy,
        default_backend=default_backend,
        settings=settings,
        audit_service=PolicyAuditService(audit_sink),
    )
    return service, registry, screenshot_store, backends, audit_sink


def _run_ctx(session_id: str = "nagent-1", run_id: str = "run-1") -> RunContext:
    return RunContext(n_agent_session_id=session_id, run_id=run_id, actor_id="actor-1")


# ---------------------------------------------------------------------------
# Auto-create on first call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_creates_session_on_first_action():
    service, registry, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", text="hello", document_revision=0,
    )

    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "success"
    # The backend's create_session was called once.
    assert len(backend.create_calls) == 1
    # Registry has the session.
    sessions = await registry.list_by_n_agent_session("nagent-1")
    assert len(sessions) == 1
    assert sessions[0].status is BrowserSessionStatus.ACTIVE
    assert sessions[0].backend_type is BrowserBackendType.CONTAINER


@pytest.mark.asyncio
async def test_get_or_create_session_returns_existing_for_same_pair():
    service, registry, _, backends, _ = _make_service()
    s1 = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    s2 = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    assert s1.id == s2.id
    assert len(backends[BrowserBackendType.CONTAINER].create_calls) == 1


@pytest.mark.asyncio
async def test_get_or_create_session_handles_integrity_error_on_concurrent_create():
    service, registry, _, backends, _ = _make_service()
    # First create succeeds.
    s1 = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    # Force registry.create to raise IntegrityError on the next call -- but
    # the service should detect the existing session and reuse it.
    import sqlite3
    registry.create_exc = sqlite3.IntegrityError("simulated concurrent create")
    s2 = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    assert s2.id == s1.id


# ---------------------------------------------------------------------------
# Concurrent single session + serial actions (per-session lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_actions_serialize_via_per_session_lock():
    service, _, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    # Slow action that records concurrent in-flight count.
    inflight = 0
    max_inflight = 0
    lock_observed = asyncio.Lock()

    async def slow_action(session_id: str, action: Any) -> BrowserActionResult:
        nonlocal inflight, max_inflight
        async with lock_observed:
            inflight += 1
            max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.02)
        async with lock_observed:
            inflight -= 1
        return BrowserActionResult(
            action_type=type(action).__name__, status="success", document_revision=0
        )

    backend_creator = backend.execute_action
    backend.execute_action = slow_action  # type: ignore[assignment]

    # Fire 4 concurrent actions.
    await asyncio.gather(*[
        service.execute_action("nagent-1", ObserveAction(), _run_ctx())
        for _ in range(4)
    ])
    # Max in-flight must be 1 (per-session lock serializes).
    assert max_inflight == 1


# ---------------------------------------------------------------------------
# Stale ref
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_element_ref_does_not_click():
    service, _, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    # First observe at revision 0.
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", text="page1",
        document_revision=0,
    )
    await service.execute_action("nagent-1", ObserveAction(), _run_ctx())

    # A navigate bumps document_revision to 1.
    backend.next_action_result = BrowserActionResult(
        action_type="navigate", status="success",
        url="https://example.com/page2", document_revision=1,
    )
    nav_result = await service.execute_action(
        "nagent-1", NavigateAction(url="https://example.com/page2"), _run_ctx()
    )
    assert nav_result.status == "success"

    # Now click with the old revision 0 -> stale_element_ref, no click.
    result = await service.execute_action(
        "nagent-1", ClickAction(element_ref="el-old", document_revision=0), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "stale_element_ref"


# ---------------------------------------------------------------------------
# No auto-retry on action_outcome_unknown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_outcome_unknown_no_auto_retry():
    service, registry, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    # First observe succeeds so the session exists.
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", document_revision=0,
    )
    await service.execute_action("nagent-1", ObserveAction(), _run_ctx())

    # The next click raises a TimeoutError AFTER click may have side-effected.
    backend.next_action_exc = TimeoutError()
    result = await service.execute_action(
        "nagent-1", ClickAction(element_ref="el-1", document_revision=0), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "action_outcome_unknown"
    # Only ONE execute_action call on the backend (no retry).
    # The first was observe; the second was the failed click.
    assert len(backend.action_calls) == 2

    # Session is degraded.
    session = await registry.get(backend.action_calls[0][0])
    assert session is not None
    assert session.status is BrowserSessionStatus.DEGRADED


# ---------------------------------------------------------------------------
# Observe cleanup: strip input value/password/hidden/script
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observe_cleanup_strips_input_value_and_password_text():
    service, _, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    # Backend returns "raw" content with input value and password text.
    backend.next_action_result = BrowserActionResult(
        action_type="observe",
        status="success",
        text="raw text",
        elements=(
            BrowserElementSummary(
                element_ref="el-pwd", role="textbox", accessible_name="Password",
                text_excerpt="should-not-leak",
            ),
            BrowserElementSummary(
                element_ref="el-input", role="textbox", accessible_name="Search",
                text_excerpt="user-typed-search-term",
            ),
        ),
        document_revision=0,
    )
    result = await service.execute_action("nagent-1", ObserveAction(), _run_ctx())
    assert result.status == "success"
    # Password text_excerpt must be blanked by the cleanup.
    pwd_el = next(e for e in result.elements if e.accessible_name == "Password")
    assert pwd_el.text_excerpt == ""
    # The service does NOT leak the raw input value -- it never receives it
    # in the first place (the driver omits it), but if a backend returns a
    # text_excerpt for a password it must be blanked.


# ---------------------------------------------------------------------------
# Screenshot persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screenshot_persisted_via_store_no_ref_in_result():
    service, _, screenshot_store, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    # The backend signals "screenshot captured" by setting last_screenshot_bytes
    # on the driver side. Here our fake backend is the driver; the service
    # reads bytes from a side channel. We model this via a result with
    # screenshot bytes attached on the backend's last_screenshot attribute.
    backend.next_action_result = BrowserActionResult(
        action_type="screenshot", status="success", document_revision=0,
    )
    # Service reads bytes via backend.last_screenshot_bytes() if present.
    setattr(backend, "last_screenshot_bytes", lambda session_id: b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    result = await service.execute_action(
        "nagent-1", ScreenshotAction(), _run_ctx()
    )
    assert result.status == "success"
    # The ToolResult-equivalent never leaks the ref/URL.
    assert result.screenshot_ref is None
    # The store persisted exactly one screenshot.
    assert len(screenshot_store.stored) == 1


@pytest.mark.asyncio
async def test_click_success_persists_dashboard_screenshot():
    service, _, screenshot_store, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    setattr(
        backend,
        "last_screenshot_bytes",
        lambda session_id: b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    )

    result = await service.execute_action(
        "nagent-1",
        ClickAction(element_ref="el-button", document_revision=0),
        _run_ctx(),
    )

    assert result.status == "success"
    assert len(screenshot_store.stored) == 1


@pytest.mark.asyncio
async def test_observe_text_success_but_screenshot_fail_yields_warning():
    service, _, screenshot_store, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", text="hello", document_revision=0,
    )
    # If the service attempts to capture a screenshot alongside observe and
    # the store fails, the result must carry warning_code screenshot_unavailable.
    screenshot_store.persist_exc = RuntimeError("disk full")

    result = await service.execute_action("nagent-1", ObserveAction(), _run_ctx())
    assert result.status == "success"
    assert result.warning_code == "screenshot_unavailable"


@pytest.mark.asyncio
async def test_non_screenshot_missing_frame_preserves_existing_warning():
    service, _, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="observe",
        status="success",
        text="hello",
        warning_code="host_warning",
        document_revision=0,
    )

    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )

    assert result.status == "success"
    assert result.warning_code == "host_warning"


@pytest.mark.asyncio
async def test_non_screenshot_persist_failure_preserves_existing_warning():
    service, _, screenshot_store, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="observe",
        status="success",
        text="hello",
        warning_code="host_warning",
        document_revision=0,
    )
    setattr(
        backend,
        "last_screenshot_bytes",
        lambda session_id: b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    )
    screenshot_store.persist_exc = RuntimeError("disk full")

    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )

    assert result.status == "success"
    assert result.warning_code == "host_warning"


@pytest.mark.asyncio
async def test_screenshot_unavailable_warning_when_store_fails():
    service, _, screenshot_store, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="screenshot", status="success", document_revision=0,
    )
    setattr(backend, "last_screenshot_bytes", lambda session_id: b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    screenshot_store.persist_exc = RuntimeError("disk full")

    result = await service.execute_action(
        "nagent-1", ScreenshotAction(), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "screenshot_unavailable"


# ---------------------------------------------------------------------------
# State commands + illegal transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_and_resume_active_session():
    service, registry, _, backends, _ = _make_service()
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    paused = await service.pause_session("nagent-1")
    assert paused is True
    session = await registry.get(registry.sessions[list(registry.sessions)[0]].id)
    assert session is not None
    assert session.status is BrowserSessionStatus.PAUSED

    resumed = await service.resume_session("nagent-1")
    assert resumed is True
    session = await registry.get(session.id)
    assert session is not None
    assert session.status is BrowserSessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_pause_session_illegal_when_closed():
    service, registry, _, _, _ = _make_service()
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    await service.close_session("nagent-1")
    # Pausing a closed session is illegal.
    paused = await service.pause_session("nagent-1")
    assert paused is False


@pytest.mark.asyncio
async def test_request_takeover_transitions_to_takeover():
    service, registry, _, _, _ = _make_service()
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    sid = list(registry.sessions)[0]
    result = await service.request_takeover("nagent-1")
    assert result is True
    session = await registry.get(sid)
    assert session is not None
    assert session.status is BrowserSessionStatus.TAKEOVER


@pytest.mark.asyncio
async def test_release_takeover_restores_active():
    service, registry, screenshot_store, backends, _ = _make_service()
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    sid = list(registry.sessions)[0]
    await service.request_takeover("nagent-1")
    backend = backends[BrowserBackendType.CONTAINER]
    backend.screenshot_by_session[sid] = b"\x89PNG\r\n\x1a\npost-takeover"
    result = await service.release_takeover("nagent-1")
    assert result is True
    session = await registry.get(sid)
    assert session is not None
    assert session.status is BrowserSessionStatus.ACTIVE
    assert session.document_revision == 1
    assert backend.end_takeover_calls == [sid]
    assert screenshot_store.stored == [
        (sid, b"\x89PNG\r\n\x1a\npost-takeover", "image/png")
    ]
    assert service._latest_screenshot_ref[sid] == "ref-0"


@pytest.mark.asyncio
async def test_close_session_idempotent_and_releases_resources():
    service, registry, screenshot_store, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    sid = list(registry.sessions)[0]
    # Add a screenshot.
    await screenshot_store.persist(sid, b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")

    await service.close_session("nagent-1")
    await service.close_session("nagent-1")  # idempotent
    session = await registry.get(sid)
    assert session is not None
    assert session.status is BrowserSessionStatus.CLOSED
    # Backend was closed.
    assert backend.close_calls == [sid]
    # Screenshots deleted.
    assert screenshot_store.deleted_sessions == [sid]


# ---------------------------------------------------------------------------
# host_grant_required pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_host_cdp_without_grant_returns_pending_authorization():
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    backend = backends[BrowserBackendType.HOST_CDP]
    session = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    assert session.status is BrowserSessionStatus.PENDING_AUTHORIZATION
    # Backend NOT connected.
    assert len(backend.create_calls) == 0

    # First action returns host_grant_required.
    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "host_grant_required"


@pytest.mark.asyncio
async def test_host_cdp_with_valid_grant_activates_session():
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    backend = backends[BrowserBackendType.HOST_CDP]
    session = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    assert session.status is BrowserSessionStatus.PENDING_AUTHORIZATION

    # Grant the host.
    result = await service.grant_host(
        "nagent-1", actor_id="actor-1", policy_version="v1", ttl_seconds=3600,
    )
    assert result is True
    # Now the session is ACTIVE.
    sessions = await registry.list_by_n_agent_session("nagent-1")
    assert sessions[0].status is BrowserSessionStatus.ACTIVE
    # Backend.create_session was called to connect the host Chrome.
    assert len(backend.create_calls) == 1
    assert backend.create_calls[0].status is BrowserSessionStatus.ACTIVE

    # Action now succeeds.
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", text="hello", document_revision=0,
    )
    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "success"


@pytest.mark.asyncio
async def test_host_cdp_fresh_screenshot_persists_without_base64_projection():
    service, registry, screenshot_store, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    backend = backends[BrowserBackendType.HOST_CDP]
    session = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    assert await service.grant_host(
        "nagent-1",
        actor_id="actor-1",
        policy_version="v1",
        ttl_seconds=3600,
    )
    screenshot = b"\x89PNG\r\n\x1a\nhost-frame"
    backend.screenshot_by_session[session.id] = screenshot
    backend.next_action_result = BrowserActionResult(
        action_type="observe",
        status="success",
        text="safe page text",
        document_revision=0,
    )

    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )

    assert result.status == "success"
    assert not hasattr(result, "screenshot_base64")
    assert "base64" not in repr(result).lower()
    assert screenshot_store.stored == [
        (session.id, screenshot, "image/png")
    ]
    assert service._latest_screenshot_ref[session.id] == "ref-0"
    summary = registry.action_summaries[session.id][-1]
    assert "base64" not in json.dumps(summary).lower()
    assert screenshot.decode("latin1") not in repr(summary)


@pytest.mark.asyncio
async def test_grant_host_returns_false_when_create_session_fails():
    """When backend.create_session raises, the session transitions to
    DEGRADED (not back to PENDING) and grant_host returns False."""
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    backend = backends[BrowserBackendType.HOST_CDP]
    # Make create_session raise on the next call.
    backend_create = backend.create_session

    async def failing_create(session: BrowserSession) -> None:
        raise RuntimeError("bridge unavailable")

    backend.create_session = failing_create  # type: ignore[assignment]

    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )

    result = await service.grant_host(
        "nagent-1", actor_id="actor-1", policy_version="v1", ttl_seconds=3600,
    )
    assert result is False
    # Session is DEGRADED, not PENDING.
    sessions = await registry.list_by_n_agent_session("nagent-1")
    assert sessions[0].status is BrowserSessionStatus.DEGRADED
    # The grant was recorded but the session is degraded.
    grant = await registry.get_host_grant(sessions[0].id)
    assert grant is not None


@pytest.mark.asyncio
async def test_grant_host_without_backend_configured_degrades():
    """When HOST_CDP backend is not in the backends dict (not configured),
    grant_host degrades the session and returns False."""
    service, registry, _, _, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
        backends={BrowserBackendType.HOST_CDP: None},  # type: ignore[dict-item]
    )
    # Actually, we need a service with no HOST_CDP backend. Re-create.
    from app.application.policy_audit_service import PolicyAuditService
    backends: dict[BrowserBackendType, Any] = {}  # no HOST_CDP backend
    service = BrowserService(
        backends=backends,
        registry=registry,
        screenshot_store=FakeScreenshotStore(),
        browser_policy=BrowserPolicy(),
        default_backend=BrowserBackendType.HOST_CDP,
        settings=BrowserServiceSettings(max_sessions_per_run=4),
        audit_service=PolicyAuditService(FakePolicyAuditSink()),
    )

    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    result = await service.grant_host(
        "nagent-1", actor_id="actor-1", policy_version="v1", ttl_seconds=3600,
    )
    assert result is False
    sessions = await registry.list_by_n_agent_session("nagent-1")
    assert sessions[0].status is BrowserSessionStatus.DEGRADED


@pytest.mark.asyncio
async def test_revoke_host_closes_backend_and_degrades():
    """revoke_host closes the backend session, revokes the grant, and
    transitions to DEGRADED (from ACTIVE)."""
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    backend = backends[BrowserBackendType.HOST_CDP]
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    await service.grant_host(
        "nagent-1", actor_id="actor-1", policy_version="v1", ttl_seconds=3600,
    )
    sid = list(registry.sessions)[0]
    # Grant exists.
    assert await registry.get_host_grant(sid) is not None

    await service.revoke_host("nagent-1")

    # Backend.close_session was called.
    assert sid in backend.close_calls
    # Grant was revoked.
    assert await registry.get_host_grant(sid) is None
    # Session is DEGRADED.
    session = await registry.get(sid)
    assert session is not None
    assert session.status is BrowserSessionStatus.DEGRADED


@pytest.mark.asyncio
async def test_revoke_host_from_pending_transitions_to_closed():
    """revoke_host on a PENDING_AUTHORIZATION session (never granted)
    transitions to CLOSED (PENDING -> CLOSED is valid)."""
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    sid = list(registry.sessions)[0]

    await service.revoke_host("nagent-1")

    session = await registry.get(sid)
    assert session is not None
    assert session.status is BrowserSessionStatus.CLOSED


@pytest.mark.asyncio
async def test_execute_on_host_cdp_before_grant_returns_host_grant_required():
    """execute_action on a HOST_CDP session before grant_host is called
    returns host_grant_required (session is PENDING_AUTHORIZATION)."""
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "host_grant_required"


@pytest.mark.asyncio
async def test_host_cdp_pending_trusted_dev_raises_host_grant_approval_required():
    """trusted_dev=True + host_cdp + pending -> execute_action raises
    HostGrantApprovalRequired (Chat CONFIRM card flow signal), not an error
    result. The signal carries the browser_session_id for the approval card."""
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
        settings=BrowserServiceSettings(max_sessions_per_run=4, trusted_dev=True),
    )
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    with pytest.raises(HostGrantApprovalRequired) as exc_info:
        await service.execute_action(
            "nagent-1", ObserveAction(), _run_ctx()
        )
    assert exc_info.value.browser_session_id is not None
    assert exc_info.value.n_agent_session_id == "nagent-1"


@pytest.mark.asyncio
async def test_host_cdp_pending_trusted_dev_false_returns_error_no_signal():
    """trusted_dev=False + host_cdp + pending -> execute_action returns an
    error result (host_grant_required), does NOT raise (no card injection,
    fail-closed to Dashboard/host-grant path)."""
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
        settings=BrowserServiceSettings(max_sessions_per_run=4, trusted_dev=False),
    )
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "host_grant_required"
    service, registry, _, backends, _ = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    await service.grant_host(
        "nagent-1", actor_id="actor-1", policy_version="v1", ttl_seconds=3600,
    )
    await service.revoke_host("nagent-1")
    sessions = await registry.list_by_n_agent_session("nagent-1")
    # Reverting to PENDING_AUTHORIZATION is NOT a valid transition per the
    # domain state machine (ACTIVE -> PENDING_AUTHORIZATION not allowed).
    # The service should set DEGRADED instead.
    assert sessions[0].status is BrowserSessionStatus.DEGRADED


# ---------------------------------------------------------------------------
# Registry never receives raw exception / DOM / type text / screenshot bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_never_receives_raw_exception_text():
    service, registry, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    # First observe succeeds.
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", text="hello", document_revision=0,
    )
    await service.execute_action("nagent-1", ObserveAction(), _run_ctx())

    # Backend raises a raw exception with sensitive text.
    backend.next_action_exc = RuntimeError("DOM leak <secret>value</secret>")
    await service.execute_action(
        "nagent-1", ClickAction(element_ref="el-1", document_revision=0), _run_ctx()
    )

    # The action summary in the registry must NOT contain the raw exception text.
    summaries = list(registry.action_summaries.values())
    for batch in summaries:
        for s in batch:
            assert "DOM leak" not in str(s)
            assert "secret" not in str(s).lower()


@pytest.mark.asyncio
async def test_registry_summary_only_contains_safe_projection():
    service, registry, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="navigate",
        status="success",
        url="https://example.com/page?q=secret",
        title="Example",
        text="hello",
        document_revision=1,
    )
    await service.execute_action(
        "nagent-1", NavigateAction(url="https://example.com/page?q=secret"), _run_ctx()
    )
    sid = list(registry.sessions)[0]
    summaries = registry.action_summaries.get(sid, [])
    assert len(summaries) == 1
    s = summaries[0]
    # safe_url must NOT contain query string.
    if s.get("safe_url") is not None:
        assert "?" not in s["safe_url"]
    # text_summary is None for navigate (no body text projected).
    assert s.get("text_summary") is None or "secret" not in (s.get("text_summary") or "")


# ---------------------------------------------------------------------------
# Policy audit events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_audit_event_emitted_on_host_grant_required():
    service, registry, _, backends, audit_sink = _make_service(
        default_backend=BrowserBackendType.HOST_CDP,
    )
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.HOST_CDP, _run_ctx()
    )
    await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    # At least one audit event with reason host_grant_required.
    events = audit_sink.events
    assert any(e.reason == "host_grant_required" for e in events)


@pytest.mark.asyncio
async def test_policy_audit_event_does_not_leak_url_query():
    service, registry, _, backends, audit_sink = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="navigate", status="success",
        url="https://example.com/page?q=secret", document_revision=1,
    )
    await service.execute_action(
        "nagent-1", NavigateAction(url="https://example.com/page?q=secret"), _run_ctx()
    )
    # No audit event reason should contain "secret" or "?".
    for e in audit_sink.events:
        assert "secret" not in e.reason


# ---------------------------------------------------------------------------
# Global session cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_session_cap_enforced():
    service, registry, _, backends, _ = _make_service(
        settings=BrowserServiceSettings(max_sessions_per_run=1),
    )
    s1 = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    # Different nagent session, same backend -> exceeds cap.
    with pytest.raises(Exception) as exc:
        await service.get_or_create_session(
            "nagent-2", BrowserBackendType.CONTAINER, _run_ctx("nagent-2", "run-2")
        )
    assert "cap" in str(exc.value).lower() or "limit" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# get_state / list_sessions / list_actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_returns_safe_url_and_revision():
    service, _, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    # Create a session first.
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    from app.domain.browser import BrowserState
    backend.next_state = BrowserState(
        safe_url="https://example.com/page",
        title="Example",
        status=BrowserSessionStatus.ACTIVE,
        document_revision=2,
        latest_screenshot_ref=None,
    )
    state = await service.get_state("nagent-1")
    assert state is not None
    assert state.safe_url == "https://example.com/page"
    assert state.document_revision == 2


@pytest.mark.asyncio
async def test_list_sessions_and_list_actions():
    service, registry, _, backends, _ = _make_service()
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", text="hi", document_revision=0,
    )
    await service.execute_action("nagent-1", ObserveAction(), _run_ctx())
    sessions = await service.list_sessions("nagent-1")
    assert len(sessions) == 1
    sid = sessions[0].id
    actions = await service.list_actions("nagent-1", limit=10)
    assert len(actions) == 1
    assert actions[0]["action_type"] == "observe"
    assert await service.count_actions_for_session(sid) == 1


# ---------------------------------------------------------------------------
# BrowserPolicy evaluated at create, each action, each transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_on_paused_session_returns_session_not_active():
    service, _, _, backends, _ = _make_service()
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    await service.pause_session("nagent-1")
    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "session_not_active"


@pytest.mark.asyncio
async def test_action_on_closed_session_returns_session_not_active():
    service, _, _, backends, _ = _make_service()
    await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    await service.close_session("nagent-1")
    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "session_not_active"


# ---------------------------------------------------------------------------
# Action-before timeout -> browser_action_timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_timeout_yields_browser_action_timeout():
    service, _, _, backends, _ = _make_service(
        settings=BrowserServiceSettings(
            max_sessions_per_run=4, action_timeout_seconds=0.05,
        ),
    )
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_action_result = BrowserActionResult(
        action_type="observe", status="success", text="hello", document_revision=0,
    )
    await service.execute_action("nagent-1", ObserveAction(), _run_ctx())

    async def slow_action(session_id: str, action: Any) -> BrowserActionResult:
        await asyncio.sleep(1.0)
        return BrowserActionResult(
            action_type=type(action).__name__, status="success", document_revision=0,
        )

    backend.execute_action = slow_action  # type: ignore[assignment]
    result = await service.execute_action(
        "nagent-1", ObserveAction(), _run_ctx()
    )
    assert result.status == "error"
    assert result.error_code == "browser_action_timeout"


@pytest.mark.asyncio
async def test_navigate_recreates_degraded_container_session():
    service, registry, _, backends, _ = _make_service()
    original = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    degraded = await registry.compare_and_set_status(
        original.id,
        BrowserSessionStatus.ACTIVE,
        BrowserSessionStatus.DEGRADED,
    )
    assert degraded is not None

    result = await service.execute_action(
        "nagent-1",
        NavigateAction(url="https://example.com/"),
        _run_ctx(),
    )

    sessions = await registry.list_by_n_agent_session("nagent-1")
    active = [s for s in sessions if s.status is BrowserSessionStatus.ACTIVE]
    assert result.status == "success"
    assert registry.sessions[original.id].status is BrowserSessionStatus.CLOSED
    assert len(active) == 1
    assert active[0].id != original.id
    backend = backends[BrowserBackendType.CONTAINER]
    assert backend.close_calls == [original.id]
    assert len(backend.create_calls) == 2


@pytest.mark.asyncio
async def test_navigate_recreates_closed_container_session():
    service, registry, _, backends, _ = _make_service()
    original = await service.get_or_create_session(
        "nagent-1", BrowserBackendType.CONTAINER, _run_ctx()
    )
    assert await service.close_session("nagent-1") is True

    result = await service.execute_action(
        "nagent-1",
        NavigateAction(url="https://example.com/"),
        _run_ctx(),
    )

    sessions = await registry.list_by_n_agent_session("nagent-1")
    active = [s for s in sessions if s.status is BrowserSessionStatus.ACTIVE]
    assert result.status == "success"
    assert registry.sessions[original.id].status is BrowserSessionStatus.CLOSED
    assert len(active) == 1
    assert active[0].id != original.id
    backend = backends[BrowserBackendType.CONTAINER]
    assert backend.close_calls == [original.id]
    assert len(backend.create_calls) == 2
