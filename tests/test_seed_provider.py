import asyncio
from pathlib import Path

import pytest

from app.application.runtime_provider import ActiveProviderHolder
from app.config import Settings
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry
from app.main import _provider_factory, _seed_and_activate


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
