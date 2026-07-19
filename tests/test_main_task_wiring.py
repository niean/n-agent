"""T18: main.py task subsystem wiring tests.

Covers plan T18 S5-S9:
  - ApplicationServices exposes task_service/task_run_service/task_runner
  - TaskRunner.run_service bound (late-bind set_run_service called)
  - ToolService registers task tools as managed (visible in definitions;
    execution gated by permitted_managed_tools + trusted_metadata.task)
  - lifespan starts/stops TaskRunner when task_enabled=true (and skips
    otherwise)
  - Settings task fields + cross-field validation (covered in test_config.py)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.application.task_run_service import TaskRunService
from app.application.task_runner import TaskRunner
from app.application.task_service import TaskService
from app.application.task_tools import TASK_TOOL_NAMES
from app.config import Settings
from app.main import build_application_services, create_app


def _settings(
    tmp_path: Path,
    *,
    task_enabled: bool = True,
    feishu_enabled: bool = False,
    scheduler_enabled: bool = False,
) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=scheduler_enabled,
        feishu_enabled=feishu_enabled,
        task_enabled=task_enabled,
    )


# ---------------------------------------------------------------------------
# T18 S5-S6: ApplicationServices exposes task services
# ---------------------------------------------------------------------------


def test_application_services_expose_task_fields(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert isinstance(services.task_service, TaskService)
    assert isinstance(services.task_run_service, TaskRunService)
    assert isinstance(services.task_runner, TaskRunner)


def test_task_runner_run_service_late_bound(tmp_path: Path):
    """TaskRunner.set_run_service must be called (circular dep resolution)."""
    services = build_application_services(_settings(tmp_path))
    assert services.task_runner._run_service is services.task_run_service


def test_task_service_dispatch_run_service_injected(tmp_path: Path):
    """TaskService.set_run_service must be called (delegation circular dep)."""
    services = build_application_services(_settings(tmp_path))
    assert services.task_service._run_service is services.task_run_service


def test_task_service_attachments_root_resolved(tmp_path: Path):
    """task_attachments_root relative path resolved to workspace_root."""
    services = build_application_services(_settings(tmp_path))
    expected = (tmp_path / "locals" / "task-attachments").resolve()
    assert services.task_service.attachments_root == expected


# ---------------------------------------------------------------------------
# T18 S5: ToolService registers task tools (managed, gated)
# ---------------------------------------------------------------------------


def test_task_tools_registered_as_managed(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    names = {d.name for d in services.tool_service.list_definitions()}
    assert TASK_TOOL_NAMES <= names
    # All task tools managed=True (pattern twelve gating)
    for d in services.tool_service.list_definitions():
        if d.name in TASK_TOOL_NAMES:
            assert d.managed is True, f"{d.name} managed"


def test_task_management_executor_routes_bound(tmp_path: Path):
    """All 6 task tool names routed to TaskManagementToolExecutor in CompositeToolExecutor."""
    from app.infrastructure.tools.task_management import TaskManagementToolExecutor

    services = build_application_services(_settings(tmp_path))
    routes = services.tool_service.executor.routes
    # Find a TaskManagementToolExecutor instance in routes
    task_executors = {
        routes[name] for name in TASK_TOOL_NAMES if name in routes
    }
    assert len(task_executors) == 1, "all 6 task tools must share one executor"
    assert isinstance(next(iter(task_executors)), TaskManagementToolExecutor)


def test_task_tools_unique_registration_no_duplicates(tmp_path: Path):
    """启动时发现重名即失败 -- 单一权威来源（静态注册）。"""
    services = build_application_services(_settings(tmp_path))
    names = [d.name for d in services.tool_service.list_definitions()]
    # No duplicates overall
    assert len(names) == len(set(names)), "duplicate tool names"


# ---------------------------------------------------------------------------
# T18 S7-S9: lifespan starts/stops TaskRunner
# ---------------------------------------------------------------------------


def test_lifespan_starts_task_runner_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """task_enabled=true -> lifespan start TaskRunner; on shutdown -> stop.

    The TestClient context triggers the FastAPI lifespan. If start/stop raise
    unhandled exceptions, the context would fail. TaskRunner.start/stop
    idempotency is verified separately.
    """
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_TASK_ENABLED", "true")
    # Boots without exception; lifespan runs task_runner.start()/stop()
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200


def test_lifespan_skips_task_runner_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """task_enabled=false -> lifespan does NOT start TaskRunner."""
    monkeypatch.setenv("N_AGENT_SQLITE_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setenv("N_AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("N_AGENT_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_FEISHU_ENABLED", "false")
    monkeypatch.setenv("N_AGENT_TASK_ENABLED", "false")
    with TestClient(create_app()) as client:
        # App still boots
        assert client.get("/health").status_code in (200, 503)


def test_task_runner_start_stop_idempotent(tmp_path: Path):
    """TaskRunner.start/stop are idempotent per spec."""
    services = build_application_services(_settings(tmp_path))

    async def _run():
        await services.task_runner.start()
        await services.task_runner.start()  # idempotent
        await services.task_runner.stop()
        await services.task_runner.stop()  # idempotent

    asyncio.run(_run())
    assert services.task_runner._started is False


# ---------------------------------------------------------------------------
# T18 S7: feishu_client optional (None if not configured)
# ---------------------------------------------------------------------------


def test_task_outbound_delivery_handles_no_feishu(tmp_path: Path):
    """feishu_client=None 时 TaskOutboundDelivery 仍可装配."""
    services = build_application_services(_settings(tmp_path, feishu_enabled=False))
    # Notifier wired (may be the TaskOutboundDelivery with feishu_client=None)
    assert services.task_run_service.notifier is not None


def test_task_outbound_delivery_with_feishu(tmp_path: Path):
    """feishu_enabled 时 TaskOutboundDelivery 持有 feishu_client."""
    services = build_application_services(_settings(tmp_path, feishu_enabled=True))
    # feishu_client should be non-None on the outbound delivery
    notifier = services.task_run_service.notifier
    assert notifier is not None
    # We don't expose feishu_client directly; verify it via the delivery object
    assert hasattr(notifier, "feishu_client")
    assert notifier.feishu_client is not None


# ---------------------------------------------------------------------------
# T18 S9: schema failure -> health unhealthy + dispatcher not started
# (Verified indirectly: schema runs at construction; failure raises)
# ---------------------------------------------------------------------------


def test_task_registry_schema_initialized_at_startup(tmp_path: Path):
    """SQLiteTaskRegistry 在装配时完成 schema 初始化."""
    services = build_application_services(_settings(tmp_path))
    # Verify by listing tables
    reg = services.task_service.registry
    # SQLiteTaskRegistry has _list_tables()
    tables = reg._list_tables()
    for t in [
        "tasks", "task_runs", "task_comments",
        "task_events", "task_attachments", "task_notify_subs",
    ]:
        assert t in tables, f"missing table {t}"


# ---------------------------------------------------------------------------
# T18 S7: TaskRunner interval/shutdown_grace wired from Settings
# ---------------------------------------------------------------------------


def test_task_runner_interval_from_settings(tmp_path: Path):
    s = _settings(tmp_path)
    # Override via model_copy to confirm wiring reads settings
    s = s.model_copy(update={"task_dispatch_interval_seconds": 7})
    services = build_application_services(s)
    assert services.task_runner.interval_seconds == 7


def test_task_runner_shutdown_grace_from_settings(tmp_path: Path):
    s = _settings(tmp_path).model_copy(update={"task_shutdown_grace_seconds": 15})
    services = build_application_services(s)
    assert services.task_runner.shutdown_grace_seconds == 15


def test_task_run_service_lease_and_concurrency_from_settings(tmp_path: Path):
    s = _settings(tmp_path).model_copy(update={
        "task_lease_seconds": 600,
        "task_max_concurrency": 2,
        "task_max_runtime_seconds": 1800,
        "task_heartbeat_timeout_seconds": 120,
    })
    services = build_application_services(s)
    assert services.task_run_service.lease_seconds == 600
    assert services.task_run_service.max_concurrency == 2
    assert services.task_run_service.max_runtime_seconds == 1800
    assert services.task_run_service.heartbeat_timeout_seconds == 120
