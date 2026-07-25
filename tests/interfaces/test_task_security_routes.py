import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.task_security_dashboard_service import TaskSecurityDashboardService
from app.config import Settings
from app.interfaces.http.task_security_routes import register_task_security_routes


def _client(service) -> TestClient:
    app = FastAPI()
    register_task_security_routes(app.router, service)
    return TestClient(app)


def test_success_returns_profile_and_no_store():
    client = _client(TaskSecurityDashboardService(Settings()))
    r = client.get("/chat/tasks/security")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["profile_version"] == "task-security-v1"
    assert len(data["policies"]) == 5


def test_projection_failure_maps_to_fixed_500_no_leak(monkeypatch, caplog):
    service = TaskSecurityDashboardService(Settings())

    def boom():
        raise RuntimeError("internal: /Users/secret locals/sessions.db sqlite3.OperationalError api_key=sk-xxx")
    monkeypatch.setattr(service, "list_task_security", boom)
    client = _client(service)
    with caplog.at_level("ERROR"):
        r = client.get("/chat/tasks/security")
    assert r.status_code == 500
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["error"]["code"] == "task_security_load_failed"
    assert body["error"]["message"] == "Task security profile could not be loaded"
    # Response never leaks original exception text, paths, secrets.
    blob = repr(body)
    for forbidden in ("secret", "sqlite3", "api_key", "sk-xxx", "/Users"):
        assert forbidden not in blob, f"response leaks {forbidden}"
    # Server-side log uses the fixed context.
    assert any("task security profile could not be loaded" in rec.message
               or "task security profile could not be loaded" in (rec.exc_text or "")
               for rec in caplog.records)


def test_non_serializable_return_maps_to_fixed_500(monkeypatch):
    service = TaskSecurityDashboardService(Settings())

    class NotJsonable:
        pass

    def returns_garbage():
        return {"profile_version": "task-security-v1", "policies": [{"x": NotJsonable()}]}
    monkeypatch.setattr(service, "list_task_security", returns_garbage)
    client = _client(service)
    r = client.get("/chat/tasks/security")
    assert r.status_code == 500
    assert r.json()["error"]["code"] == "task_security_load_failed"


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
def test_write_methods_not_allowed(method):
    client = _client(TaskSecurityDashboardService(Settings()))
    r = client.request(method, "/chat/tasks/security")
    assert r.status_code == 405
