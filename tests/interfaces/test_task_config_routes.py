import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.task_config_service import TaskConfigService
from app.config import Settings
from app.domain.task_config import TaskConfigOverrides
from app.infrastructure.policy.task_config_logging_sink import TaskConfigLoggingSink
from app.infrastructure.registry.sqlite_task_config_store import SqliteTaskConfigStore
from app.interfaces.http.task_config_routes import register_task_config_routes


def _app(tmp_path):
    store = SqliteTaskConfigStore(str(tmp_path / "sessions.db"))
    settings = Settings(
        provider_base_url="", provider_api_key="", provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"), workspace_root=".",
        scheduler_enabled=False, feishu_enabled=False,
    )
    svc = TaskConfigService(settings, store, TaskConfigLoggingSink())
    app = FastAPI()
    register_task_config_routes(app.router, svc)
    return app, svc


def test_get_no_row_returns_env_version_zero(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).get("/chat/tasks/security/config")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    data = r.json()
    assert data["version"] == 0
    assert data["overridden_fields"] == []
    assert data["updated_at"] is None
    assert data["updated_by"] is None
    assert data["config"]["task_max_concurrency"] == 4  # env default


def test_patch_first_write_success(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "task_max_concurrency": 8})
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 1
    assert data["config"]["task_max_concurrency"] == 8
    assert "task_max_concurrency" in data["overridden_fields"]
    assert data["updated_by"] == "dashboard-local"


def test_patch_conflict_409(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    client.patch("/chat/tasks/security/config", json={"expected_version": 0, "task_max_concurrency": 8})
    # Same expected_version=0 again -> row exists -> 409.
    r = client.patch("/chat/tasks/security/config", json={"expected_version": 0, "task_max_concurrency": 9})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "task_config_conflict"


def test_patch_unknown_field_422(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "unknown_field": 1})
    assert r.status_code == 422


def test_patch_empty_patch_422(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0})
    assert r.status_code == 422


def test_patch_rejects_body_updated_by(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "task_max_concurrency": 8, "updated_by": "attacker"})
    assert r.status_code == 422


def test_patch_rejects_bool_value(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "task_max_concurrency": True})
    assert r.status_code == 422


def test_patch_rejects_bad_expected_version(tmp_path):
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": -1, "task_max_concurrency": 8})
    assert r.status_code == 422
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": True, "task_max_concurrency": 8})
    assert r.status_code == 422


def test_patch_validation_heartbeat_ge_lease_422(tmp_path):
    app, _ = _app(tmp_path)
    # heartbeat 900 >= lease 900 -> invalid
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "task_heartbeat_timeout_seconds": 900})
    assert r.status_code == 422


def test_patch_validation_lease_le_dispatch_422(tmp_path):
    app, _ = _app(tmp_path)
    # dispatch default 30; lease 30 -> invalid
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "task_lease_seconds": 30})
    assert r.status_code == 422


def test_patch_a_class_field_rejected(tmp_path):
    # A-class fields are not in TASK_CONFIG_FIELDS, so they're unknown -> 422.
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "approval_tools_stripped": False})
    assert r.status_code == 422


def test_patch_b_class_field_rejected(tmp_path):
    # B-class (task_enabled) is not configurable -> 422.
    app, _ = _app(tmp_path)
    r = TestClient(app).patch("/chat/tasks/security/config", json={"expected_version": 0, "task_enabled": False})
    assert r.status_code == 422


def test_patch_partial_preserves_other_overrides(tmp_path):
    app, _ = _app(tmp_path)
    client = TestClient(app)
    r1 = client.patch("/chat/tasks/security/config", json={"expected_version": 0, "task_max_concurrency": 8})
    r2 = client.patch("/chat/tasks/security/config", json={"expected_version": 1, "note_max_codepoints": 3000})
    assert r2.status_code == 200
    data = r2.json()
    assert data["config"]["task_max_concurrency"] == 8  # preserved
    assert data["config"]["note_max_codepoints"] == 3000
    assert set(data["overridden_fields"]) == {"task_max_concurrency", "note_max_codepoints"}
