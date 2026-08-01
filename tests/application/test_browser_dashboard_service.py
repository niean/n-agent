"""Tests for BrowserDashboardService (T14).

Covers:
- visibility filter: mismatch -> not_found (no existence leak)
- screenshot ownership: screenshot_ref must belong to a visible session
- command delegation: pause/resume/takeover/release/close delegate to BrowserService
- takeover/release consume the confirmation challenge
- close revokes outstanding challenge tokens
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from app.application.browser_confirmation_service import BrowserConfirmationService
from app.application.browser_dashboard_service import BrowserDashboardService
from app.application.browser_service import (
    BrowserService,
    BrowserServiceSettings,
    RunContext,
)
from app.domain.browser import (
    BrowserActionResult,
    BrowserBackendType,
    BrowserSession,
    BrowserSessionStatus,
    BrowserState,
    NavigateAction,
    ObserveAction,
)
from app.domain.browser_policy import BrowserPolicy
from types import SimpleNamespace


# ---------------------------------------------------------------------------
# Fakes (shared with test_browser_service.py patterns)
# ---------------------------------------------------------------------------


class FakeBackend:
    def __init__(self, backend_type: BrowserBackendType) -> None:
        self.backend_type = backend_type
        self.sessions: dict[str, BrowserSession] = {}
        self.create_calls: list[BrowserSession] = []
        self.close_calls: list[str] = []
        self.action_calls: list[tuple[str, Any]] = []
        self.next_state: Any | None = None
        self.last_screenshot_bytes_val: bytes | None = None

    async def create_session(self, session: BrowserSession) -> None:
        self.create_calls.append(session)
        self.sessions[session.id] = session

    async def close_session(self, session_id: str) -> None:
        self.close_calls.append(session_id)
        self.sessions.pop(session_id, None)

    async def execute_action(self, session_id: str, action: Any) -> BrowserActionResult:
        self.action_calls.append((session_id, action))
        return BrowserActionResult(
            action_type=type(action).__name__.replace("Action", "").lower(),
            status="success",
            document_revision=0,
        )

    async def get_state(self, session_id: str) -> Any:
        if self.next_state is not None:
            return self.next_state
        return BrowserState(
            safe_url=None,
            title=None,
            status=BrowserSessionStatus.ACTIVE,
            document_revision=0,
            latest_screenshot_ref=None,
        )

    async def begin_takeover(self, session_id: str) -> str | None:
        return None

    async def end_takeover(self, session_id: str) -> None:
        pass

    def last_screenshot_bytes(self) -> bytes | None:
        return self.last_screenshot_bytes_val


class FakeRegistry:
    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}
        self.action_summaries: dict[str, list[dict[str, Any]]] = {}
        self.profile_leases: dict[str, str] = {}
        self.host_grants: dict[str, dict[str, Any]] = {}

    async def create(self, session: BrowserSession) -> None:
        self.sessions[session.id] = session

    async def get(self, session_id: str) -> BrowserSession | None:
        return self.sessions.get(session_id)

    async def list_by_n_agent_session(self, n_agent_session_id: str) -> list[BrowserSession]:
        return [
            s for s in self.sessions.values()
            if s.bound_n_agent_session_id == n_agent_session_id
        ]

    async def compare_and_set_status(
        self, session_id, expected, next_status, *,
        pre_takeover_status=None, document_revision=None,
    ) -> BrowserSession | None:
        session = self.sessions.get(session_id)
        if session is None or session.status is not expected:
            return None
        pre = pre_takeover_status if pre_takeover_status is not None else session.pre_takeover_status
        rev = document_revision if document_revision is not None else session.document_revision
        session = session.with_status(
            next_status,
            pre_takeover_status=pre,
            document_revision=rev,
        )
        self.sessions[session_id] = session
        return session

    async def acquire_profile_lease(self, profile_ref, session_id) -> bool:
        existing = self.profile_leases.get(profile_ref)
        if existing is None or existing == session_id:
            self.profile_leases[profile_ref] = session_id
            return True
        return False

    async def release_profile_lease(self, profile_ref) -> None:
        self.profile_leases.pop(profile_ref, None)

    async def append_action_summary(self, session_id, summary) -> None:
        self.action_summaries.setdefault(session_id, []).append(summary)

    async def list_actions(self, session_id, limit) -> list[dict[str, Any]]:
        return list(self.action_summaries.get(session_id, []))[:limit]

    async def count_actions(self, session_id) -> int:
        return len(self.action_summaries.get(session_id, []))

    async def close(self, session_id) -> None:
        session = self.sessions.get(session_id)
        if session is not None:
            self.sessions[session_id] = session.with_status(BrowserSessionStatus.CLOSED)

    async def record_host_grant(self, session_id, n_agent_session_id, actor_id, policy_version, expires_at) -> None:
        self.host_grants[session_id] = {
            "browser_session_id": session_id,
            "n_agent_session_id": n_agent_session_id,
            "actor_id": actor_id,
            "policy_version": policy_version,
            "expires_at": expires_at,
        }

    async def revoke_host_grant(self, session_id) -> None:
        self.host_grants.pop(session_id, None)

    async def get_host_grant(self, session_id) -> dict[str, Any] | None:
        return self.host_grants.get(session_id)


class FakeScreenshotStore:
    def __init__(self) -> None:
        self.stored: dict[str, bytes] = {}
        self.deleted_sessions: list[str] = []

    async def persist(self, session_id, data, content_type) -> str:
        ref = f"ref-{len(self.stored)}"
        self.stored[ref] = data
        return ref

    async def read(self, ref) -> bytes | None:
        return self.stored.get(ref)

    async def delete_session(self, session_id) -> None:
        self.deleted_sessions.append(session_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(
    *,
    backends=None,
    registry=None,
    screenshot_store=None,
    confirmation=None,
    settings=None,
) -> tuple[BrowserDashboardService, BrowserService, FakeRegistry, FakeScreenshotStore, BrowserConfirmationService]:
    if backends is None:
        backends = {
            BrowserBackendType.CONTAINER: FakeBackend(BrowserBackendType.CONTAINER),
            BrowserBackendType.HOST_CDP: FakeBackend(BrowserBackendType.HOST_CDP),
        }
    if registry is None:
        registry = FakeRegistry()
    if screenshot_store is None:
        screenshot_store = FakeScreenshotStore()
    if confirmation is None:
        confirmation = BrowserConfirmationService(ttl_seconds=60)
    if settings is None:
        settings = SimpleNamespace(
            browser_takeover_ttl_seconds=60,
            browser_host_grant_ttl_seconds=300,
            browser_trusted_dev=False,
            browser_container_endpoint="http://browser:9222",
        )
    browser_service = BrowserService(
        backends=backends,
        registry=registry,
        screenshot_store=screenshot_store,
        browser_policy=BrowserPolicy(),
        default_backend=BrowserBackendType.CONTAINER,
        settings=BrowserServiceSettings(max_sessions_per_run=4),
    )
    dashboard = BrowserDashboardService(
        browser_service=browser_service,
        screenshot_store=screenshot_store,
        confirmation_service=confirmation,
        settings=settings,
    )
    return dashboard, browser_service, registry, screenshot_store, confirmation


def _make_session(
    browser_session_id: str = "bsess-1",
    n_agent_session_id: str = "nagent-1",
    backend: BrowserBackendType = BrowserBackendType.CONTAINER,
    status: BrowserSessionStatus = BrowserSessionStatus.ACTIVE,
    pre_takeover_status: BrowserSessionStatus | None = None,
) -> BrowserSession:
    return BrowserSession(
        id=browser_session_id,
        bound_n_agent_session_id=n_agent_session_id,
        backend_type=backend,
        status=status,
        profile_ref="bp-test-123",
        document_revision=0,
        pre_takeover_status=pre_takeover_status,
    )


# ---------------------------------------------------------------------------
# Visibility filter: mismatch -> not_found (no existence leak)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_returns_none_for_wrong_nagent():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    # Correct nagent -> returns session
    result = await dashboard.get_session("bsess-1", "nagent-1")
    assert result is not None
    assert result["id"] == "bsess-1"
    # Wrong nagent -> None (no existence leak)
    result = await dashboard.get_session("bsess-1", "nagent-evil")
    assert result is None


@pytest.mark.asyncio
async def test_get_session_returns_none_for_unknown_session():
    dashboard, _, _, _, _ = _make_service()
    result = await dashboard.get_session("nonexistent", "nagent-1")
    assert result is None


@pytest.mark.asyncio
async def test_list_sessions_filters_by_nagent():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    registry.sessions["bsess-2"] = _make_session("bsess-2", "nagent-2")
    result = await dashboard.list_sessions("nagent-1")
    assert len(result) == 1
    assert result[0]["id"] == "bsess-1"
    assert result[0]["action_count"] == 0


@pytest.mark.asyncio
async def test_get_state_returns_none_for_wrong_nagent():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    result = await dashboard.get_state("bsess-1", "nagent-evil")
    assert result is None


@pytest.mark.asyncio
async def test_list_actions_returns_none_for_wrong_nagent():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    result = await dashboard.list_actions("bsess-1", "nagent-evil")
    assert result is None


# ---------------------------------------------------------------------------
# Screenshot ownership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_screenshot_returns_none_for_wrong_nagent():
    dashboard, browser_service, registry, screenshot_store, _ = _make_service()
    session = _make_session("bsess-1", "nagent-1")
    registry.sessions["bsess-1"] = session
    # Store a screenshot
    screenshot_store.stored["ref-1"] = b"\x89PNG fake"
    # Set up the backend to return a state with the screenshot ref
    backends = browser_service._backends
    backend = backends[BrowserBackendType.CONTAINER]
    backend.next_state = BrowserState(
        safe_url="https://example.com",
        title="Example",
        status=BrowserSessionStatus.ACTIVE,
        document_revision=0,
        latest_screenshot_ref="ref-1",
    )
    # BrowserService keeps the authoritative screenshot reference after it
    # persists a capture; backend state alone must not grant access to bytes.
    browser_service._latest_screenshot_ref["bsess-1"] = "ref-1"
    # Correct nagent -> returns screenshot
    result = await dashboard.read_screenshot("bsess-1", "nagent-1")
    assert result is not None
    data, content_type = result
    assert data == b"\x89PNG fake"
    assert content_type == "image/png"
    # Wrong nagent -> None (no existence leak)
    result = await dashboard.read_screenshot("bsess-1", "nagent-evil")
    assert result is None


@pytest.mark.asyncio
async def test_read_screenshot_returns_none_when_no_screenshot():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    result = await dashboard.read_screenshot("bsess-1", "nagent-1")
    assert result is None


# ---------------------------------------------------------------------------
# Command delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_delegates_to_browser_service():
    dashboard, browser_service, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.ACTIVE)
    result = await dashboard.pause("bsess-1", "nagent-1")
    assert result["ok"] is True
    # Verify the session was paused in the registry
    session = registry.sessions["bsess-1"]
    assert session.status is BrowserSessionStatus.PAUSED


@pytest.mark.asyncio
async def test_resume_delegates_to_browser_service():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.PAUSED)
    result = await dashboard.resume("bsess-1", "nagent-1")
    assert result["ok"] is True
    session = registry.sessions["bsess-1"]
    assert session.status is BrowserSessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_close_delegates_and_revokes_challenges():
    confirmation = BrowserConfirmationService(ttl_seconds=60)
    dashboard, _, registry, _, confirmation = _make_service(confirmation=confirmation)
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.ACTIVE)
    # Issue a challenge token for the session
    token = confirmation.issue(
        "POST", "/chat/browser/sessions/bsess-1/pause", "bsess-1", "nagent-1", "actor-1"
    )
    result = await dashboard.close("bsess-1", "nagent-1")
    assert result["ok"] is True
    # Challenge token should be revoked
    assert confirmation.consume(
        token, "POST", "/chat/browser/sessions/bsess-1/pause", "bsess-1", "nagent-1", "actor-1"
    ) is False


@pytest.mark.asyncio
async def test_pause_returns_not_found_for_wrong_nagent():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.ACTIVE)
    result = await dashboard.pause("bsess-1", "nagent-evil")
    assert result["ok"] is False
    assert result["error"] == "browser_session_not_found"


# ---------------------------------------------------------------------------
# Takeover/release consume the confirmation challenge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_takeover_consumes_challenge():
    confirmation = BrowserConfirmationService(ttl_seconds=60)
    dashboard, _, registry, _, confirmation = _make_service(confirmation=confirmation)
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.ACTIVE)
    # Issue a challenge for takeover
    token = confirmation.issue(
        "POST", "/chat/browser/sessions/bsess-1/takeover", "bsess-1", "nagent-1", "actor-1"
    )
    result = await dashboard.takeover("bsess-1", "nagent-1", "actor-1", token)
    assert result["ok"] is True
    # Challenge should be consumed (single-use)
    assert confirmation.consume(
        token, "POST", "/chat/browser/sessions/bsess-1/takeover", "bsess-1", "nagent-1", "actor-1"
    ) is False


@pytest.mark.asyncio
async def test_takeover_fails_without_valid_challenge():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.ACTIVE)
    result = await dashboard.takeover("bsess-1", "nagent-1", "actor-1", "invalid-token")
    assert result["ok"] is False
    assert result["error"] == "invalid_challenge"


@pytest.mark.asyncio
async def test_release_consumes_challenge_and_revokes_capabilities():
    confirmation = BrowserConfirmationService(ttl_seconds=60)
    dashboard, _, registry, _, confirmation = _make_service(confirmation=confirmation)
    registry.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1", status=BrowserSessionStatus.TAKEOVER,
        pre_takeover_status=BrowserSessionStatus.ACTIVE,
    )
    token = confirmation.issue(
        "POST", "/chat/browser/sessions/bsess-1/release", "bsess-1", "nagent-1", "actor-1"
    )
    result = await dashboard.release("bsess-1", "nagent-1", "actor-1", token)
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_release_fails_without_valid_challenge():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1", status=BrowserSessionStatus.TAKEOVER,
        pre_takeover_status=BrowserSessionStatus.ACTIVE,
    )
    result = await dashboard.release("bsess-1", "nagent-1", "actor-1", "invalid-token")
    assert result["ok"] is False
    assert result["error"] == "invalid_challenge"


# ---------------------------------------------------------------------------
# Invalid state transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_on_closed_returns_invalid_state_transition():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.CLOSED)
    result = await dashboard.pause("bsess-1", "nagent-1")
    assert result["ok"] is False
    assert result["error"] == "invalid_state_transition"


# ---------------------------------------------------------------------------
# Takeover-view (container only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_takeover_view_returns_url_for_container_takeover():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1",
        backend=BrowserBackendType.CONTAINER,
        status=BrowserSessionStatus.TAKEOVER,
    )
    result = await dashboard.get_takeover_view("bsess-1", "nagent-1", "actor-1")
    assert result is not None
    assert result["url"].startswith(
        "/chat/browser/sessions/bsess-1/interactive/vnc.html?"
    )
    assert "cap=" in result["url"]
    assert "browser:9222" not in result["url"]
    assert result["expires_at"] is not None


@pytest.mark.asyncio
async def test_takeover_view_returns_message_for_host_cdp():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1",
        backend=BrowserBackendType.HOST_CDP,
        status=BrowserSessionStatus.TAKEOVER,
    )
    result = await dashboard.get_takeover_view("bsess-1", "nagent-1", "actor-1")
    assert result is not None
    assert result["url"] is None
    assert "managed Chrome" in (result["message"] or "")


@pytest.mark.asyncio
async def test_takeover_view_returns_none_for_wrong_nagent():
    dashboard, _, registry, _, _ = _make_service()
    registry.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    result = await dashboard.get_takeover_view("bsess-1", "nagent-evil", "actor-1")
    assert result is None
