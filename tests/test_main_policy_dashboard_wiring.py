from __future__ import annotations

import dataclasses
from pathlib import Path

from app.config import Settings
from app.main import ApplicationServices, build_application_services, create_app


def _settings(tmp_path: Path) -> Settings:
    skills_root = tmp_path / "skills"
    skills_root.mkdir(exist_ok=True)
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir(exist_ok=True)
    return Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        skills_root=str(skills_root),
        plugins_root=str(plugins_root),
        scheduler_enabled=False,
        feishu_enabled=False,
    )


def test_policy_dashboard_service_assembled(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert services.policy_dashboard_service is not None
    from app.application.policy_dashboard_service import PolicyDashboardService
    assert isinstance(services.policy_dashboard_service, PolicyDashboardService)


def test_service_provider_uses_same_settings_instance(tmp_path: Path):
    settings = _settings(tmp_path)
    services = build_application_services(settings)
    # The dashboard service must reuse the single Settings instance, not build a second one.
    assert services.policy_dashboard_service._provider._settings is settings


def test_field_is_non_default_and_before_optional_fields():
    fields = dataclasses.fields(ApplicationServices)
    names = [f.name for f in fields]
    pdi = names.index("policy_dashboard_service")
    usage = names.index("usage_service")
    assert pdi < usage
    assert dataclasses.fields(ApplicationServices)[pdi].default is dataclasses.MISSING


def _route_paths(app) -> list[str]:
    # Starlette 1.x wraps included routers in _IncludedRouter (path=None,
    # real routes on .original_router); walk those + Mounts recursively.
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


def test_create_app_registers_security_and_policy_routes(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    paths = set(_route_paths(app))
    assert "/security" in paths
    assert "/chat/policies" in paths


def test_production_app_serves_policies(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        res = client.get("/chat/policies")
        assert res.status_code == 200
        assert res.headers["cache-control"] == "no-store"
        data = res.json()
        assert len(data["policies"]) == 10
        assert client.get("/security").status_code == 200
        assert client.post("/chat/policies").status_code == 405
