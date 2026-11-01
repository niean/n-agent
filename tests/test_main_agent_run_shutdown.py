import sqlite3

import pytest

from fastapi.testclient import TestClient

import app.main as main
from app.application import telemetry_details, telemetry_semantics
from app.config import Settings
from app.domain.agent_run import RunCancelledError


@pytest.fixture(autouse=True)
def restore_telemetry_configuration(monkeypatch):
    monkeypatch.setattr(telemetry_details, "_config", telemetry_details._config)
    monkeypatch.setattr(telemetry_semantics, "_span_attr_sanitizer", telemetry_semantics._span_attr_sanitizer)


def test_ledger_shutdown_failure_does_not_skip_task_runner_cleanup(tmp_path, monkeypatch):
    settings = Settings(
        sqlite_path=tmp_path / "sessions.db", workspace_root=tmp_path,
        skills_root=tmp_path / "skills", provider_base_url="", provider_api_key="",
        plugins_root=tmp_path / "plugins",
        provider_model="", scheduler_enabled=False, feishu_enabled=False,
        artifacts_enabled=False, task_enabled=True,
    )
    services = main.build_application_services(settings)
    stopped = []
    original_stop = services.task_runner.stop

    async def failed_close():
        raise sqlite3.OperationalError("database is locked")

    async def stop():
        stopped.append(True)
        await original_stop()

    monkeypatch.setattr(services.agent_run_service, "close", failed_close)
    monkeypatch.setattr(services.task_runner, "stop", stop)
    monkeypatch.setattr(main, "build_application_services", lambda *args, **kwargs: services)
    with TestClient(main.create_app(settings)) as client:
        assert client.get("/health").status_code == 200
    assert stopped == [True]


def test_explicit_run_cancellation_uses_stable_http_error(tmp_path):
    app = main.create_app(Settings(
        sqlite_path=tmp_path / "sessions.db", workspace_root=tmp_path,
        skills_root=tmp_path / "skills", plugins_root=tmp_path / "plugins",
        provider_base_url="", provider_api_key="", provider_model="",
        scheduler_enabled=False, feishu_enabled=False, artifacts_enabled=False,
        task_enabled=False,
    ))

    @app.get("/test/cancelled")
    async def cancelled():
        raise RunCancelledError("run")

    with TestClient(app) as client:
        response = client.get("/test/cancelled")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "agent_run_cancelled"
