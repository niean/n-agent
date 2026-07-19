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
