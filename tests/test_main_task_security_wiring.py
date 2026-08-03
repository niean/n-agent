"""T3: TaskSecurityDashboardService wiring in main.py + dashboard.py.

Covers:
  - ApplicationServices.task_security_dashboard_service is a non-default field.
  - build_application_services constructs it for task_enabled True/False and
    holds the same Settings instance (no caching, resolve-time read).
  - create_app passes the same service instance to create_dashboard_router.
  - /chat/tasks/security registered before /chat/tasks/{task_id} (route order).
  - task_enabled=False: endpoint still registered, returns 200, task_enabled=false.
  - /tasks/security shell returns index.html; literal shell route before catch-all.
  - Unknown similar URLs return 404 (no wildcard shell handler).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.application.task_security_dashboard_service import TaskSecurityDashboardService
from app.config import Settings
from app.main import build_application_services, create_app


def _settings(tmp_path: Path, *, task_enabled: bool = True) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        scheduler_enabled=False,
        feishu_enabled=False,
        task_enabled=task_enabled,
        artifacts_enabled=False,
    )


def test_service_is_non_default_dataclass_field():
    import dataclasses
    from app.main import ApplicationServices
    fields = {f.name: f for f in dataclasses.fields(ApplicationServices)}
    assert "task_security_dashboard_service" in fields
    # Non-nullable: no default value.
    assert fields["task_security_dashboard_service"].default is dataclasses.MISSING


def test_service_constructed_regardless_of_task_enabled(tmp_path):
    s_off = build_application_services(_settings(tmp_path, task_enabled=False))
    s_on = build_application_services(_settings(tmp_path, task_enabled=True))
    assert isinstance(s_off.task_security_dashboard_service, TaskSecurityDashboardService)
    assert isinstance(s_on.task_security_dashboard_service, TaskSecurityDashboardService)


@pytest.mark.asyncio
async def test_service_holds_same_settings_instance(tmp_path):
    settings = _settings(tmp_path)
    services = build_application_services(settings)
    assert services.task_security_dashboard_service._settings is settings
    data = await services.task_security_dashboard_service.list_task_security()
    assert data["profile_version"] == "task-security-v1"


def test_create_app_passes_service_to_router(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    captured = {}

    real_create_dashboard_router = __import__(
        "app.interfaces.http.dashboard", fromlist=["create_dashboard_router"]
    ).create_dashboard_router

    def spy(*args, **kwargs):
        captured["svc"] = kwargs.get("task_security_dashboard_service")
        return real_create_dashboard_router(*args, **kwargs)

    # app.main imported create_dashboard_router by name at module load, so patch
    # the app.main reference (not app.interfaces.http.dashboard).
    monkeypatch.setattr("app.main.create_dashboard_router", spy)
    create_app(settings)
    svc = captured.get("svc")
    # create_app passes a real service to the router (not None), proving it
    # threads the service through rather than reconstructing inside the router.
    assert isinstance(svc, TaskSecurityDashboardService)
    # create_app rebuilds services internally from the same settings; the
    # service passed to the router holds that same settings instance.
    assert svc._settings is settings


def _route_paths(app) -> list[str]:
    # Starlette 1.x wraps each included router in an _IncludedRouter object
    # (path=None, the real routes live on .original_router) instead of
    # flattening them into app.routes. Walk those + Mounts so registration
    # and ordering assertions keep working.
    paths: list[str] = []

    def walk(routes):
        for r in routes:
            p = getattr(r, "path", None)
            if p:
                paths.append(p)
            orig = getattr(r, "original_router", None)
            if orig is not None and hasattr(orig, "routes"):
                walk(orig.routes)
            sub = getattr(r, "routes", None)
            if isinstance(sub, list):
                walk(sub)

    walk(app.routes)
    return paths


def test_api_route_registered_before_task_id_catchall(tmp_path):
    app = create_app(_settings(tmp_path, task_enabled=True))
    paths = _route_paths(app)
    assert "/chat/tasks/security" in paths
    assert "/chat/tasks/{task_id}" in paths
    assert paths.index("/chat/tasks/security") < paths.index("/chat/tasks/{task_id}")


def test_api_returns_200_when_task_enabled(tmp_path):
    app = create_app(_settings(tmp_path, task_enabled=True))
    client = TestClient(app)
    r = client.get("/chat/tasks/security")
    assert r.status_code == 200
    data = r.json()
    assert data["profile_version"] == "task-security-v1"
    assert len(data["policies"]) == 5


def test_api_returns_200_when_task_disabled(tmp_path):
    app = create_app(_settings(tmp_path, task_enabled=False))
    client = TestClient(app)
    r = client.get("/chat/tasks/security")
    assert r.status_code == 200
    data = r.json()
    te = next(p for p in data["policies"] if p["key"] == "task_execution")
    assert {c["key"]: c["value"] for c in te["config"]}["task_enabled"] is False


def test_shell_route_returns_index_html(tmp_path):
    app = create_app(_settings(tmp_path, task_enabled=True))
    client = TestClient(app)
    r = client.get("/tasks/security")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


def test_shell_literal_route_registered_before_catchall(tmp_path):
    app = create_app(_settings(tmp_path, task_enabled=True))
    paths = _route_paths(app)
    assert "/tasks/security" in paths
    assert "/tasks/{task_id}" in paths
    assert paths.index("/tasks/security") < paths.index("/tasks/{task_id}")


def test_no_new_wildcard_shell_handler_added(tmp_path):
    # /tasks/security/extra is a two-segment path that no shell route matches ->
    # 404. (The pre-existing /tasks/{task_id} catch-all matches single-segment
    # paths like /tasks/security-extra and returns index.html; that is existing
    # behavior, not a wildcard introduced by this change.)
    app = create_app(_settings(tmp_path, task_enabled=True))
    client = TestClient(app)
    r = client.get("/tasks/security/extra")
    assert r.status_code == 404
    # The literal /tasks/security shell route exists exactly once.
    paths = _route_paths(app)
    assert paths.count("/tasks/security") == 1


def test_task_config_service_assembled_regardless_of_task_enabled(tmp_path):
    from app.application.task_config_service import TaskConfigService
    s_off = build_application_services(_settings(tmp_path, task_enabled=False))
    s_on = build_application_services(_settings(tmp_path, task_enabled=True))
    assert isinstance(s_off.task_config_service, TaskConfigService)
    assert isinstance(s_on.task_config_service, TaskConfigService)


def test_config_route_registered_before_task_id_catchall(tmp_path):
    app = create_app(_settings(tmp_path, task_enabled=True))
    paths = _route_paths(app)
    assert "/chat/tasks/security/config" in paths
    assert paths.index("/chat/tasks/security/config") < paths.index("/chat/tasks/{task_id}")


def test_config_route_available_when_task_disabled(tmp_path):
    app = create_app(_settings(tmp_path, task_enabled=False))
    client = TestClient(app)
    r = client.get("/chat/tasks/security/config")
    assert r.status_code == 200
    assert r.json()["version"] == 0


# ---------------------------------------------------------------------------
# T3: DashboardToolApprovalBridge + GatewayToolApprovalService wiring
# ---------------------------------------------------------------------------

def test_tool_approval_service_is_non_default_dataclass_field():
    """ApplicationServices.tool_approval_service is a non-default field."""
    import dataclasses
    from app.main import ApplicationServices
    fields = {f.name: f for f in dataclasses.fields(ApplicationServices)}
    assert "tool_approval_service" in fields
    assert fields["tool_approval_service"].default is dataclasses.MISSING


def test_build_application_services_creates_tool_approval_service(tmp_path):
    from app.application.gateway_tool_approval_service import GatewayToolApprovalService
    services = build_application_services(_settings(tmp_path))
    assert isinstance(services.tool_approval_service, GatewayToolApprovalService)
    # GatewayService holds the SAME instance (not a freshly-constructed one)
    assert services.gateway_service.tool_approval_service is services.tool_approval_service


def test_create_app_shares_tool_approval_service_between_gateway_and_router(tmp_path, monkeypatch):
    """create_app creates ONE GatewayToolApprovalService and shares it between
    GatewayService and the dashboard router (identity check ``is``)."""
    from app.application.gateway_tool_approval_service import GatewayToolApprovalService
    from app.interfaces.http.dashboard_tool_approval import DashboardToolApprovalBridge

    settings = _settings(tmp_path)
    captured = {}

    real_create_dashboard_router = __import__(
        "app.interfaces.http.dashboard", fromlist=["create_dashboard_router"]
    ).create_dashboard_router

    def router_spy(*args, **kwargs):
        captured["router_tool_approval_service"] = kwargs.get("tool_approval_service")
        captured["router_bridge"] = kwargs.get("dashboard_tool_approval_bridge")
        return real_create_dashboard_router(*args, **kwargs)

    real_build = build_application_services

    def build_spy(s):
        services = real_build(s)
        captured["services"] = services
        return services

    monkeypatch.setattr("app.main.create_dashboard_router", router_spy)
    monkeypatch.setattr("app.main.build_application_services", build_spy)

    create_app(settings)

    # Real bridge passed to the router
    assert isinstance(captured["router_bridge"], DashboardToolApprovalBridge)
    # Real GatewayToolApprovalService passed to the router
    assert isinstance(captured["router_tool_approval_service"], GatewayToolApprovalService)
    # Identity: GatewayService and the router receive the SAME instance
    services = captured["services"]
    assert services.gateway_service.tool_approval_service is captured["router_tool_approval_service"]


def test_create_app_registers_claim_route(tmp_path):
    """The claim endpoint is registered when the app is wired with the bridge."""
    app = create_app(_settings(tmp_path))
    paths = _route_paths(app)
    assert "/chat/tool-approvals/{confirmation_id}" in paths
