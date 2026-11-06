from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.host_terminal_dashboard_service import HostTerminalDashboardService
from app.domain.host_terminal_policy import (
    HostTerminalPolicySnapshot,
    HostTerminalResourceLimits,
)
from app.interfaces.http.host_terminal_routes import register_host_terminal_routes


class FakeLoader:
    def __init__(self, snapshot=None, last_error=None):
        self._snapshot = snapshot
        self.last_error_code = last_error

    @property
    def snapshot(self):
        return self._snapshot


class FakeExecutor:
    def __init__(self, health="ok"):
        self.last_health_code = health


class FakeMemoryStore:
    async def list_recent_tool_calls(self, tool_name=None, limit=50):
        return []

    async def list_tool_calls(self, session_id):
        return []


def _snapshot():
    return HostTerminalPolicySnapshot(
        schema_version=1,
        version="example-v1",
        content_digest="a" * 64,
        loaded_at=datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc),
        limits=HostTerminalResourceLimits(
            default_timeout_seconds=120,
            max_timeout_seconds=120,
            max_stdout_bytes=8192,
            max_stderr_bytes=8192,
            max_args=0,
            max_arg_length=256,
            max_total_args_length=256,
            max_concurrency=1,
        ),
    )


def _app(service):
    app = FastAPI()
    register_host_terminal_routes(app.router, service)
    return app


def test_status_endpoint_unavailable_contract():
    svc = HostTerminalDashboardService(
        FakeLoader(), FakeExecutor(), FakeMemoryStore(), "host_terminal_disabled"
    )
    client = TestClient(_app(svc))
    r = client.get("/chat/host/status")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["health_code"] == "host_terminal_disabled"


def test_status_endpoint_enabled_contract():
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor("ok"), FakeMemoryStore(), None
    )
    client = TestClient(_app(svc))
    r = client.get("/chat/host/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_policy_endpoint_returns_200_when_no_snapshot():
    svc = HostTerminalDashboardService(
        FakeLoader(None, last_error="host_policy_load_failed"),
        FakeExecutor(),
        FakeMemoryStore(),
        "host_bridge_not_checked",
    )
    client = TestClient(_app(svc))
    r = client.get("/chat/host/policy")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["policy_last_error"] == "host_policy_load_failed"


def test_history_default_limit_ok():
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(), None
    )
    client = TestClient(_app(svc))
    assert client.get("/chat/host/history").status_code == 200
    assert client.get("/chat/host/history", params={"limit": 1}).status_code == 200
    assert client.get("/chat/host/history", params={"limit": 100}).status_code == 200


@pytest.mark.parametrize("bad_limit", [0, 101, -1])
def test_history_invalid_limit_returns_422(bad_limit):
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(), None
    )
    client = TestClient(_app(svc))
    r = client.get("/chat/host/history", params={"limit": bad_limit})
    assert r.status_code == 422


def test_history_session_id_param_accepted():
    svc = HostTerminalDashboardService(
        FakeLoader(_snapshot()), FakeExecutor(), FakeMemoryStore(), None
    )
    client = TestClient(_app(svc))
    r = client.get("/chat/host/history", params={"session_id": "sess-1"})
    assert r.status_code == 200


def test_no_write_or_refresh_endpoints_registered():
    svc = HostTerminalDashboardService(
        FakeLoader(), FakeExecutor(), FakeMemoryStore(), None
    )
    client = TestClient(_app(svc))
    assert client.post("/chat/host/policy/refresh").status_code == 404
    assert client.delete("/chat/host/history/abc").status_code == 404
    assert client.post("/chat/host/status").status_code == 405
    assert client.patch("/chat/host/policy").status_code == 405


class FakeGrants:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.revisions = []

    async def list_grants(self):
        if self.mode == "error":
            raise RuntimeError("/private/secret")
        return [{"revision": 9007199254740993, "state": "active"}]

    async def revoke(self, revision):
        self.revisions.append(revision)
        if self.mode == "stale":
            raise ValueError("host_script_grants_stale_revision")
        if self.mode == "error":
            raise RuntimeError("/private/secret")
        return {"success": self.mode != "pending", "revision": revision,
                "state": "pending_revoke" if self.mode == "pending" else "revoked",
                "error": "pending_sync" if self.mode == "pending" else None}


def grant_client(grants=None):
    return TestClient(_app(HostTerminalDashboardService(
        None, None, FakeMemoryStore(), None, script_grant_service=grants)))


def test_script_grants_disabled_contract():
    client = grant_client()
    assert client.get('/chat/host/script-grants').json() == {
        'enabled': False, 'grants': [], 'scan_error': None, 'sync_error': None}
    assert client.post('/chat/host/script-grants/1/revoke').status_code == 503


def test_script_grants_precision_and_revoke():
    grants = FakeGrants()
    client = grant_client(grants)
    assert client.get('/chat/host/script-grants').json()['grants'][0]['revision'] == '9007199254740993'
    response = client.post('/chat/host/script-grants/9007199254740993/revoke')
    assert response.status_code == 200
    assert response.json()['revision'] == '9007199254740993'
    assert grants.revisions == [9007199254740993]


@pytest.mark.parametrize('mode,code', [('stale', 409), ('pending', 202), ('error', 503)])
def test_script_grants_revoke_errors(mode, code):
    response = grant_client(FakeGrants(mode)).post('/chat/host/script-grants/1/revoke')
    assert response.status_code == code
    assert '/private' not in response.text
    if mode == 'pending':
        assert response.json()['success'] is False
        assert response.json()['state'] == 'pending_revoke'


def test_script_grants_list_error_and_invalid_revision():
    client = grant_client(FakeGrants('error'))
    response = client.get('/chat/host/script-grants')
    assert response.status_code == 503
    assert '/private' not in response.text
    assert client.post('/chat/host/script-grants/nope/revoke').status_code == 422


def test_script_grants_diagnostics_visible_without_exception_text():
    from types import SimpleNamespace
    service = HostTerminalDashboardService(
        None, None, FakeMemoryStore(), None, script_grant_service=FakeGrants(),
        skill_service=SimpleNamespace(last_auto_refresh_error='scan_failed',
                                      last_auto_sync_error='pending_sync'))
    client = TestClient(_app(service))
    payload = client.get('/chat/host/script-grants').json()
    assert payload['scan_error'] == 'scan_failed'
    assert payload['sync_error'] == 'pending_sync'
    assert client.get('/chat/host/status').json()['scan_error'] == 'scan_failed'
