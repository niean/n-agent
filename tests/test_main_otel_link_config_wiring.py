"""T6 OTel link config wiring tests: composition root (main.py) assembly.

Covers (plan T6 S1):
- build_application_services assembles SqliteOtelLinkConfigStore +
  OtelLinkConfigLoggingSink + OtelLinkConfigService unconditionally
  (independent of otel_enabled); env base is mapped from settings;
- the regular assembly creates the otel_link_config table at store
  construction time (idempotent DDL, never inside a request);
- create_app passes the SAME service instance to create_dashboard_router
  and to register_otel_link_config_routes; the management GET/PATCH
  route is live and reflects the env base;
- otel_enabled=False keeps the config API and the env links working;
- otel_link_config_service=None registers no management route while the
  legacy env links keep rendering;
- the migration read-only assembly (build_config_bundle_services) never
  creates the otel_link_config table (sqlite_master before/after);
- the Config Bundle ten-section whitelist is unchanged.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.otel_link_config_service import OtelLinkConfigService
from app.config import Settings
from app.main import build_application_services, build_config_bundle_services, create_app

_ENV_URLS = {
    "otel_grafana_traces_base_url": "",
    "otel_grafana_metrics_base_url": "https://grafana.example.com/metrics",
    "otel_grafana_logs_base_url": "https://grafana.example.com/logs",
    "otel_grafana_profile_base_url": "https://grafana.example.com/profiles",
    "otel_jaeger_base_url": "https://jaeger.example.com",
}


def _settings(tmp_path: Path, *, otel_enabled: bool = False) -> Settings:
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
        artifacts_enabled=False,
        otel_enabled=otel_enabled,
        **_ENV_URLS,
    )


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


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# build_application_services
# ---------------------------------------------------------------------------


def test_build_application_services_wires_otel_link_config_service(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    assert isinstance(services.otel_link_config_service, OtelLinkConfigService)


@pytest.mark.asyncio
async def test_env_base_mapped_from_settings(tmp_path: Path):
    services = build_application_services(_settings(tmp_path))
    resolved = await services.otel_link_config_service.current()
    assert resolved.metric_base_url == _ENV_URLS["otel_grafana_metrics_base_url"]
    assert resolved.log_base_url == _ENV_URLS["otel_grafana_logs_base_url"]
    assert resolved.trace_base_url == _ENV_URLS["otel_jaeger_base_url"]
    assert resolved.profile_base_url == _ENV_URLS["otel_grafana_profile_base_url"]


def test_regular_assembly_creates_table_at_store_construction(tmp_path: Path):
    settings = _settings(tmp_path)
    build_application_services(settings)
    assert "otel_link_config" in _table_names(Path(settings.sqlite_path))


@pytest.mark.asyncio
async def test_otel_disabled_still_wires_service(tmp_path: Path):
    # otel_enabled=False is the default; the config service must exist anyway.
    services = build_application_services(_settings(tmp_path, otel_enabled=False))
    assert isinstance(services.otel_link_config_service, OtelLinkConfigService)
    resolved = await services.otel_link_config_service.current()
    assert resolved.metric_base_url == _ENV_URLS["otel_grafana_metrics_base_url"]


# ---------------------------------------------------------------------------
# create_app: same instance into dashboard router + management routes
# ---------------------------------------------------------------------------


def test_create_app_threads_same_service_instance(tmp_path: Path, monkeypatch):
    import app.main as app_main

    real_create_dashboard_router = app_main.create_dashboard_router
    real_register = app_main.register_otel_link_config_routes
    captured: dict = {}

    def spy_router(*args, **kwargs):
        captured["router_svc"] = kwargs.get("otel_link_config_service")
        return real_create_dashboard_router(*args, **kwargs)

    def spy_register(router, service):
        captured["register_svc"] = service
        return real_register(router, service)

    monkeypatch.setattr(app_main, "create_dashboard_router", spy_router)
    monkeypatch.setattr(app_main, "register_otel_link_config_routes", spy_register)
    create_app(_settings(tmp_path))

    router_svc = captured.get("router_svc")
    assert isinstance(router_svc, OtelLinkConfigService)
    # ApplicationServices, the dashboard parameter and the management routes
    # must all share one service instance.
    assert captured.get("register_svc") is router_svc


def test_create_app_registers_management_route(tmp_path: Path):
    app = create_app(_settings(tmp_path))
    assert "/chat/observations/otel/links" in _route_paths(app)


def test_management_route_serves_env_base_when_otel_disabled(tmp_path: Path):
    app = create_app(_settings(tmp_path, otel_enabled=False))
    client = TestClient(app)
    r = client.get("/chat/observations/otel/links")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    data = r.json()
    assert data["config"]["metric_base_url"] == _ENV_URLS["otel_grafana_metrics_base_url"]
    assert data["config"]["trace_base_url"] == _ENV_URLS["otel_jaeger_base_url"]
    assert data["overridden_fields"] == []


# ---------------------------------------------------------------------------
# None injection: no management route, legacy env links unchanged
# ---------------------------------------------------------------------------


def _dashboard_client(otel_link_config_service) -> TestClient:
    from app.interfaces.http.dashboard import create_dashboard_router

    app = FastAPI()
    app.include_router(
        create_dashboard_router(
            SimpleNamespace(),  # session_service (unused on these routes)
            SimpleNamespace(),  # tool_service
            SimpleNamespace(),  # model_service
            lambda: {},  # health_provider
            settings=None,
            otel_grafana_metrics_base_url=_ENV_URLS["otel_grafana_metrics_base_url"],
            otel_service_name="n-agent",
            otel_link_config_service=otel_link_config_service,
        )
    )
    return TestClient(app)


def test_none_service_registers_no_management_route():
    client = _dashboard_client(None)
    r = client.get("/chat/observations/otel/links")
    assert r.status_code == 404


def test_none_service_keeps_legacy_env_links():
    client = _dashboard_client(None)
    r = client.get("/chat/observations/otel")
    assert r.status_code == 200
    links = r.json()["links"]
    assert links["Metric"].startswith(_ENV_URLS["otel_grafana_metrics_base_url"])


# ---------------------------------------------------------------------------
# Migration read-only assembly: never creates the otel_link_config table
# ---------------------------------------------------------------------------


def test_bundle_assembly_never_creates_otel_link_config_table(tmp_path: Path):
    settings = _settings(tmp_path)
    # Create the bundle schema once (write mode); this DB intentionally lacks
    # the otel_link_config table.
    build_config_bundle_services(settings, read_only=False)
    db_path = Path(settings.sqlite_path)
    before = _table_names(db_path)
    assert "otel_link_config" not in before

    build_config_bundle_services(settings, read_only=True)
    build_config_bundle_services(settings, read_only=False)
    after = _table_names(db_path)
    assert after == before
    assert "otel_link_config" not in after


# ---------------------------------------------------------------------------
# Config Bundle whitelist regression
# ---------------------------------------------------------------------------


def test_config_bundle_section_whitelist_unchanged():
    from app.domain.config_bundle import SECTION_NAMES

    assert SECTION_NAMES == (
        "providers",
        "knowledge_bases",
        "mcp_sites",
        "external_memory_providers",
        "external_memory_global_config",
        "plugins",
        "skills",
        "scheduled_tasks",
        "gateway_home_targets",
        "task_config",
    )


def test_grafana_trace_url_takes_precedence_over_legacy_jaeger(tmp_path):
    settings = _settings(tmp_path).model_copy(update={"otel_grafana_traces_base_url": "https://grafana.example.com/explore"})
    services = build_application_services(settings)
    import asyncio
    resolved = asyncio.run(services.otel_link_config_service.current())
    assert resolved.trace_base_url == "https://grafana.example.com/explore"
