import asyncio
from pathlib import Path

import pytest

from app.application.runtime_provider import ActiveProviderHolder
from app.config import Settings
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry
from fastapi.testclient import TestClient

from app.main import _provider_factory, _seed_and_activate, build_application_services, create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        provider_base_url="https://example.test/v1",
        provider_api_key="seed-key",
        provider_model="seed-model",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
    )


def test_seed_creates_default_provider_when_empty(tmp_path: Path):
    settings = _settings(tmp_path)
    registry = SQLiteProviderRegistry(settings.sqlite_path)
    holder = ActiveProviderHolder(_provider_factory)

    asyncio.run(_seed_and_activate(registry, holder, settings))

    providers = asyncio.run(registry.list_providers())
    assert len(providers) == 1
    seeded = providers[0]
    assert seeded.name == "default"
    assert seeded.base_url == "https://example.test/v1"
    assert seeded.model == "seed-model"
    assert seeded.is_active is True
    assert holder.current_model == "seed-model"


def test_seed_skipped_when_table_not_empty(tmp_path: Path):
    settings = _settings(tmp_path)
    registry = SQLiteProviderRegistry(settings.sqlite_path)
    holder = ActiveProviderHolder(_provider_factory)

    from app.application.provider_service import ProviderCreateInput, ProviderService

    service = ProviderService(registry, holder)
    asyncio.run(
        service.create_provider(
            ProviderCreateInput(name="manual", base_url="http://m", model="mm", api_key="kk")
        )
    )

    asyncio.run(_seed_and_activate(registry, holder, settings))

    providers = asyncio.run(registry.list_providers())
    assert [p.name for p in providers] == ["manual"]
    assert providers[0].is_active is True


def test_seed_noop_when_settings_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
    )
    registry = SQLiteProviderRegistry(settings.sqlite_path)
    holder = ActiveProviderHolder(_provider_factory)

    asyncio.run(_seed_and_activate(registry, holder, settings))

    providers = asyncio.run(registry.list_providers())
    assert providers == []
    assert holder.current_config is None


def test_build_application_services_wires_scheduler(tmp_path: Path):
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        scheduler_enabled=False,
        feishu_enabled=False,
    )

    services = build_application_services(settings)

    assert services.schedule_service is not None
    assert services.scheduler_runner is not None
    assert services.session_service.on_session_deleted == services.schedule_service.handle_session_deleted
    assert services.health_snapshot()["scheduler"]["status"] == "disabled"


def test_create_app_exposes_scheduled_task_routes(tmp_path: Path):
    settings = Settings(
        provider_base_url="",
        provider_api_key="",
        provider_model="",
        sqlite_path=str(tmp_path / "sessions.db"),
        workspace_root=str(tmp_path),
        scheduler_enabled=False,
        feishu_enabled=False,
    )
    client = TestClient(create_app(settings))

    response = client.get("/chat/scheduled-tasks")

    assert response.status_code == 200
    assert response.json() == []
