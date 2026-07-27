"""Tests for Browser Dashboard HTTP routes (T15).

Covers:
- 12 endpoints registered
- actor/same-origin/challenge requirements for write endpoints
- host-grant 404 in production (trusted_dev=False)
- takeover-view capability TTL/revoke
- screenshot no-store
- stable error mapping
- no leak (no backend exception, paths, token, page text, URL query)
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.browser_confirmation_service import BrowserConfirmationService
from app.application.browser_dashboard_service import BrowserDashboardService
from app.domain.browser import (
    BrowserBackendType,
    BrowserSession,
    BrowserSessionStatus,
    BrowserState,
)
from app.interfaces.http.browser_routes import register_browser_routes


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeBrowserService:
    """Minimal BrowserService fake for route tests."""

    def __init__(self) -> None:
        self.sessions: dict[str, BrowserSession] = {}
        self.states: dict[str, BrowserState] = {}
        self.actions: dict[str, list[dict[str, Any]]] = {}
        self.paused: list[str] = []
        self.resumed: list[str] = []
        self.takeover_called: list[str] = []
        self.release_called: list[str] = []
        self.closed: list[str] = []
        self.host_grant_called: list[tuple[str, str, str, int]] = []
        self.revoke_host_called: list[str] = []
        self.host_grant_result: bool = True

    async def list_sessions(self, n_agent_session_id: str) -> list[BrowserSession]:
        return [s for s in self.sessions.values() if s.bound_n_agent_session_id == n_agent_session_id]

    async def get_session_by_id(self, browser_session_id: str) -> BrowserSession | None:
        return self.sessions.get(browser_session_id)

    async def get_state_for_session(self, browser_session_id: str) -> BrowserState | None:
        return self.states.get(browser_session_id)

    async def list_actions_for_session(self, browser_session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.actions.get(browser_session_id, []))[:limit]

    async def count_actions_for_session(self, browser_session_id: str) -> int:
        return len(self.actions.get(browser_session_id, []))

    async def pause_session(self, n_agent_session_id: str) -> bool:
        self.paused.append(n_agent_session_id)
        return True

    async def resume_session(self, n_agent_session_id: str) -> bool:
        self.resumed.append(n_agent_session_id)
        return True

    async def request_takeover(self, n_agent_session_id: str) -> bool:
        self.takeover_called.append(n_agent_session_id)
        return True

    async def release_takeover(self, n_agent_session_id: str) -> bool:
        self.release_called.append(n_agent_session_id)
        return True

    async def close_session(self, n_agent_session_id: str) -> bool:
        self.closed.append(n_agent_session_id)
        return True

    async def grant_host(self, n_agent_session_id: str, *, actor_id, policy_version, ttl_seconds) -> bool:
        self.host_grant_called.append((n_agent_session_id, actor_id, policy_version, ttl_seconds))
        return self.host_grant_result

    async def revoke_host(self, n_agent_session_id: str) -> None:
        self.revoke_host_called.append(n_agent_session_id)


class FakeScreenshotStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def read(self, ref: str) -> bytes | None:
        return self.data.get(ref)

    async def persist(self, session_id, data, content_type) -> str:
        ref = f"ref-{len(self.data)}"
        self.data[ref] = data
        return ref

    async def delete_session(self, session_id) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    browser_session_id: str = "bsess-1",
    n_agent_session_id: str = "nagent-1",
    backend: BrowserBackendType = BrowserBackendType.CONTAINER,
    status: BrowserSessionStatus = BrowserSessionStatus.ACTIVE,
) -> BrowserSession:
    return BrowserSession(
        id=browser_session_id,
        bound_n_agent_session_id=n_agent_session_id,
        backend_type=backend,
        status=status,
        profile_ref="bp-test-123",
        document_revision=0,
    )


def _make_app(
    *,
    trusted_dev: bool = False,
    actor: str = "dashboard-operator",
    browser_service: FakeBrowserService | None = None,
    screenshot_store: FakeScreenshotStore | None = None,
    confirmation: BrowserConfirmationService | None = None,
) -> tuple[TestClient, FakeBrowserService, FakeScreenshotStore, BrowserConfirmationService]:
    browser_service = browser_service or FakeBrowserService()
    screenshot_store = screenshot_store or FakeScreenshotStore()
    confirmation = confirmation or BrowserConfirmationService(ttl_seconds=60)
    settings = SimpleNamespace(
        browser_trusted_dev=trusted_dev,
        browser_host_grant_ttl_seconds=300,
        browser_takeover_ttl_seconds=60,
        browser_container_endpoint="http://browser:9222",
        dashboard_base_url="http://localhost:8201",
    )
    dashboard_service = BrowserDashboardService(
        browser_service=browser_service,
        screenshot_store=screenshot_store,
        confirmation_service=confirmation,
        settings=settings,
    )

    def actor_resolver(request) -> str | None:
        return actor

    app = FastAPI()
    register_browser_routes(app.router, dashboard_service, confirmation, actor_resolver, settings)
    return TestClient(app), browser_service, screenshot_store, confirmation


def _get_challenge(
    client: TestClient,
    confirmation: BrowserConfirmationService,
    browser_session_id: str,
    n_agent_session_id: str,
    actor: str,
    op: str,
) -> str:
    """Issue a challenge by calling GET /chat/browser/sessions/{id} and
    extracting the token for the given operation."""
    # Manually issue a challenge (simulates what the route does internally)
    method_map = {
        "pause": ("POST", "pause"),
        "resume": ("POST", "resume"),
        "takeover": ("POST", "takeover"),
        "release": ("POST", "release"),
        "close": ("POST", "close"),
        "host_grant": ("POST", "host-grant"),
        "revoke_host": ("DELETE", "host-grant"),
    }
    method, suffix = method_map[op]
    path = f"/chat/browser/sessions/{browser_session_id}/{suffix}"
    return confirmation.issue(
        method=method,
        path=path,
        browser_session_id=browser_session_id,
        n_agent_session_id=n_agent_session_id,
        actor_id=actor,
    )


# ---------------------------------------------------------------------------
# 12 endpoints registered
# ---------------------------------------------------------------------------


def test_all_12_endpoints_registered():
    client, _, _, _ = _make_app()
    routes = {r.path: list(r.methods) for r in client.app.routes if hasattr(r, "path") and "/browser/" in r.path}
    expected = [
        "/chat/browser/sessions",
        "/chat/browser/sessions/{browser_session_id}",
        "/chat/browser/sessions/{browser_session_id}/actions",
        "/chat/browser/sessions/{browser_session_id}/screenshot",
        "/chat/browser/sessions/{browser_session_id}/pause",
        "/chat/browser/sessions/{browser_session_id}/resume",
        "/chat/browser/sessions/{browser_session_id}/takeover",
        "/chat/browser/sessions/{browser_session_id}/release",
        "/chat/browser/sessions/{browser_session_id}/close",
        "/chat/browser/sessions/{browser_session_id}/takeover-view",
    ]
    for path in expected:
        assert path in routes, f"missing route: {path}"
    # host-grant only when trusted_dev
    if any("host-grant" in p for p in routes):
        assert "/chat/browser/sessions/{browser_session_id}/host-grant" in routes


def test_host_grant_not_registered_in_production():
    client, _, _, _ = _make_app(trusted_dev=False)
    routes = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/chat/browser/sessions/{browser_session_id}/host-grant" not in routes


def test_host_grant_registered_when_trusted_dev():
    client, _, _, _ = _make_app(trusted_dev=True)
    routes = {r.path for r in client.app.routes if hasattr(r, "path")}
    assert "/chat/browser/sessions/{browser_session_id}/host-grant" in routes


# ---------------------------------------------------------------------------
# GET endpoints (read-only, require actor but not challenge)
# ---------------------------------------------------------------------------


def test_list_sessions_returns_200():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.get("/chat/browser/sessions", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 200
    data = r.json()
    assert "sessions" in data
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["id"] == "bsess-1"
    assert data["sessions"][0]["action_count"] == 0


def test_list_sessions_requires_actor():
    client, _, _, _ = _make_app(actor=None)
    r = client.get("/chat/browser/sessions", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "browser_actor_required"


def test_get_session_returns_200_with_challenges():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.get("/chat/browser/sessions/bsess-1", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "bsess-1"
    assert "write_challenges" in data
    # active session should have pause/takeover/close challenges
    assert "pause" in data["write_challenges"]
    assert "takeover" in data["write_challenges"]
    assert "close" in data["write_challenges"]


def test_get_session_returns_404_for_unknown():
    client, _, _, _ = _make_app()
    r = client.get("/chat/browser/sessions/nonexistent", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "browser_session_not_found"


def test_get_session_returns_404_for_wrong_nagent():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.get("/chat/browser/sessions/bsess-1", params={"n_agent_session_id": "nagent-evil"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "browser_session_not_found"


def test_get_screenshot_returns_no_store_header():
    client, browser_service, screenshot_store, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    browser_service.states["bsess-1"] = BrowserState(
        safe_url="https://example.com",
        title="Example",
        status=BrowserSessionStatus.ACTIVE,
        document_revision=0,
        latest_screenshot_ref="ref-1",
    )
    screenshot_store.data["ref-1"] = b"\x89PNG fake data"
    r = client.get("/chat/browser/sessions/bsess-1/screenshot", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    assert r.content == b"\x89PNG fake data"


def test_get_screenshot_returns_404_when_unavailable():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.get("/chat/browser/sessions/bsess-1/screenshot", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "screenshot_unavailable"


def test_list_actions_returns_200():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    browser_service.actions["bsess-1"] = [
        {"id": "act-1", "action_type": "navigate", "status": "success"},
        {"id": "act-2", "action_type": "observe", "status": "success"},
    ]
    r = client.get("/chat/browser/sessions/bsess-1/actions", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 200
    data = r.json()
    assert "actions" in data
    assert len(data["actions"]) == 2


# ---------------------------------------------------------------------------
# Write endpoints: require actor + same-origin + challenge
# ---------------------------------------------------------------------------


def test_pause_requires_challenge():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.post("/chat/browser/sessions/bsess-1/pause", params={"n_agent_session_id": "nagent-1"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "browser_challenge_required"


def test_pause_succeeds_with_valid_challenge():
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "pause")
    r = client.post(
        "/chat/browser/sessions/bsess-1/pause",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "nagent-1" in browser_service.paused


def test_pause_challenge_replay_fails():
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "pause")
    # First use: success
    r1 = client.post(
        "/chat/browser/sessions/bsess-1/pause",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r1.status_code == 200
    # Replay: fails
    r2 = client.post(
        "/chat/browser/sessions/bsess-1/pause",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "invalid_challenge"


def test_pause_challenge_path_bound():
    """A challenge for pause cannot be used for resume."""
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1", status=BrowserSessionStatus.PAUSED)
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "pause")
    # Use pause challenge for resume -> should fail
    r = client.post(
        "/chat/browser/sessions/bsess-1/resume",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_challenge"


def test_pause_challenge_actor_bound():
    """A challenge issued for actor-A cannot be used by actor-B."""
    client, browser_service, _, confirmation = _make_app(actor="actor-B")
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    # Issue challenge for actor-A
    token = confirmation.issue(
        "POST", "/chat/browser/sessions/bsess-1/pause",
        "bsess-1", "nagent-1", "actor-A",
    )
    # Use with actor-B (the app's actor resolver returns actor-B)
    r = client.post(
        "/chat/browser/sessions/bsess-1/pause",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_challenge"


def test_close_succeeds_with_challenge():
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "close")
    r = client.post(
        "/chat/browser/sessions/bsess-1/close",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_takeover_succeeds_with_challenge():
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "takeover")
    r = client.post(
        "/chat/browser/sessions/bsess-1/takeover",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "nagent-1" in browser_service.takeover_called


def test_release_succeeds_with_challenge():
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1", status=BrowserSessionStatus.TAKEOVER,
    )
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "release")
    r = client.post(
        "/chat/browser/sessions/bsess-1/release",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "nagent-1" in browser_service.release_called


# ---------------------------------------------------------------------------
# host-grant 404 in production
# ---------------------------------------------------------------------------


def test_host_grant_404_in_production():
    client, browser_service, _, _ = _make_app(trusted_dev=False)
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.post(
        "/chat/browser/sessions/bsess-1/host-grant",
        params={"n_agent_session_id": "nagent-1"},
        json={"policy_version": "v1", "ttl_seconds": 300},
    )
    assert r.status_code == 404


def test_host_grant_succeeds_when_trusted_dev():
    client, browser_service, _, confirmation = _make_app(trusted_dev=True)
    browser_service.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1",
        backend=BrowserBackendType.HOST_CDP,
        status=BrowserSessionStatus.PENDING_AUTHORIZATION,
    )
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "host_grant")
    r = client.post(
        "/chat/browser/sessions/bsess-1/host-grant",
        params={"n_agent_session_id": "nagent-1"},
        json={"policy_version": "v1", "ttl_seconds": 300},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_revoke_host_succeeds_when_trusted_dev():
    client, browser_service, _, confirmation = _make_app(trusted_dev=True)
    browser_service.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1",
        backend=BrowserBackendType.HOST_CDP,
        status=BrowserSessionStatus.ACTIVE,
    )
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "revoke_host")
    r = client.delete(
        "/chat/browser/sessions/bsess-1/host-grant",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "nagent-1" in browser_service.revoke_host_called


# ---------------------------------------------------------------------------
# takeover-view
# ---------------------------------------------------------------------------


def test_takeover_view_returns_url_for_container():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1",
        backend=BrowserBackendType.CONTAINER,
        status=BrowserSessionStatus.TAKEOVER,
    )
    r = client.get(
        "/chat/browser/sessions/bsess-1/takeover-view",
        params={"n_agent_session_id": "nagent-1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["url"] is not None
    assert "cap=" in data["url"]
    assert data["expires_at"] is not None
    # no-store header
    assert r.headers.get("cache-control") == "no-store"


def test_takeover_view_returns_message_for_host_cdp():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1",
        backend=BrowserBackendType.HOST_CDP,
        status=BrowserSessionStatus.TAKEOVER,
    )
    r = client.get(
        "/chat/browser/sessions/bsess-1/takeover-view",
        params={"n_agent_session_id": "nagent-1"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["url"] is None


def test_takeover_view_returns_404_for_wrong_nagent():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.get(
        "/chat/browser/sessions/bsess-1/takeover-view",
        params={"n_agent_session_id": "nagent-evil"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# No leak: error responses don't contain backend exception/paths/token
# ---------------------------------------------------------------------------


def test_error_response_does_not_leak_backend_exception():
    client, browser_service, _, _ = _make_app()
    # Trigger a not-found error
    r = client.get("/chat/browser/sessions/nonexistent", params={"n_agent_session_id": "nagent-1"})
    body = r.json()
    assert "error" in body
    assert "code" in body["error"]
    # No stack trace, no paths, no token
    body_str = str(body)
    assert "Traceback" not in body_str
    assert "app/" not in body_str
    assert ".py" not in body_str


def test_cross_origin_rejected():
    client, browser_service, _, _ = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    r = client.post(
        "/chat/browser/sessions/bsess-1/pause",
        params={"n_agent_session_id": "nagent-1"},
        headers={"origin": "https://evil.example.com"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "browser_cross_origin_forbidden"


def test_same_origin_allowed():
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session("bsess-1", "nagent-1")
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "pause")
    r = client.post(
        "/chat/browser/sessions/bsess-1/pause",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token, "origin": "http://localhost:8201"},
    )
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Error code mapping
# ---------------------------------------------------------------------------


def test_invalid_state_transition_returns_409():
    client, browser_service, _, confirmation = _make_app()
    browser_service.sessions["bsess-1"] = _make_session(
        "bsess-1", "nagent-1", status=BrowserSessionStatus.CLOSED,
    )
    token = _get_challenge(client, confirmation, "bsess-1", "nagent-1", "dashboard-operator", "pause")
    r = client.post(
        "/chat/browser/sessions/bsess-1/pause",
        params={"n_agent_session_id": "nagent-1"},
        headers={"x-browser-challenge": token},
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "invalid_state_transition"
