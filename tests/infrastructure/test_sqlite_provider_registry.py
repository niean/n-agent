import asyncio
from datetime import datetime, timezone

import pytest

from app.domain.provider import (
    DuplicateProviderError,
    ProviderConfig,
    ProviderNotFoundError,
)
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry


def _new_cfg(name="Default", model="qwen2.5", base_url="http://localhost:11434/v1", is_active=False):
    now = datetime.now(timezone.utc)
    return ProviderConfig(
        id="",
        name=name,
        provider_type="openai-compatible",
        base_url=base_url,
        model=model,
        api_key_present=False,
        is_active=is_active,
        extra_headers=None,
        created_at=now,
        updated_at=now,
    )


def test_create_and_list(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "sessions.db")
    cfg = asyncio.run(registry.create_provider(_new_cfg(), api_key="sk-1"))
    assert cfg.id and cfg.api_key_present is True
    items = asyncio.run(registry.list_providers())
    assert len(items) == 1 and items[0].name == "Default"


def test_duplicate_name_rejected(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "sessions.db")
    asyncio.run(registry.create_provider(_new_cfg(name="A"), api_key=""))
    with pytest.raises(DuplicateProviderError):
        asyncio.run(registry.create_provider(_new_cfg(name="A"), api_key=""))


def test_set_active_swaps_unique(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "sessions.db")
    a = asyncio.run(registry.create_provider(_new_cfg(name="A"), api_key="k1"))
    b = asyncio.run(registry.create_provider(_new_cfg(name="B"), api_key="k2"))
    asyncio.run(registry.set_active(a.id))
    assert asyncio.run(registry.get_active()).id == a.id
    asyncio.run(registry.set_active(b.id))
    active = asyncio.run(registry.get_active())
    assert active is not None and active.id == b.id


def test_update_api_key_three_states(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "sessions.db")
    cfg = asyncio.run(registry.create_provider(_new_cfg(), api_key="orig"))
    asyncio.run(registry.update_provider(cfg.id, name="X"))
    assert asyncio.run(registry.get_secret(cfg.id)) == "orig"
    asyncio.run(registry.update_provider(cfg.id, api_key="new"))
    assert asyncio.run(registry.get_secret(cfg.id)) == "new"
    asyncio.run(registry.update_provider(cfg.id, clear_api_key=True))
    assert asyncio.run(registry.get_secret(cfg.id)) is None


def test_delete_and_not_found(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "sessions.db")
    cfg = asyncio.run(registry.create_provider(_new_cfg(), api_key=""))
    asyncio.run(registry.delete_provider(cfg.id))
    with pytest.raises(ProviderNotFoundError):
        asyncio.run(registry.get_secret(cfg.id))
    with pytest.raises(ProviderNotFoundError):
        asyncio.run(registry.delete_provider(cfg.id))


def test_get_provider_returns_none_when_missing(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "sessions.db")
    assert asyncio.run(registry.get_provider("missing")) is None
    assert asyncio.run(registry.get_active()) is None
