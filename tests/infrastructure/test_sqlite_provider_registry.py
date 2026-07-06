import asyncio
from datetime import datetime, timezone

import pytest

from app.domain.provider import (
    DuplicateProviderError,
    ProviderConfig,
    ProviderNotFoundError,
)
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry


def _new_cfg(name="Default", model="qwen2.5", base_url="http://localhost:11434/v1", is_active=False, supports_vision=False):
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
        supports_vision=supports_vision,
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


def test_providers_table_has_supports_vision_column(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "t.db")
    with registry._connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
    assert "supports_vision" in cols


def test_create_provider_persists_supports_vision_true(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "t.db")
    cfg = asyncio.run(registry.create_provider(_new_cfg(supports_vision=True), api_key="k"))
    assert cfg.supports_vision is True


def test_create_provider_persists_supports_vision_false(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "t.db")
    cfg = asyncio.run(registry.create_provider(_new_cfg(supports_vision=False), api_key="k"))
    assert cfg.supports_vision is False


def test_update_provider_supports_vision(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "t.db")
    cfg = asyncio.run(registry.create_provider(_new_cfg(), api_key="k"))
    updated = asyncio.run(registry.update_provider(cfg.id, supports_vision=True))
    assert updated.supports_vision is True
    refreshed = asyncio.run(registry.get_provider(cfg.id))
    assert refreshed is not None and refreshed.supports_vision is True


def test_update_provider_supports_vision_none_keeps_value(tmp_path):
    registry = SQLiteProviderRegistry(tmp_path / "t.db")
    cfg = asyncio.run(registry.create_provider(_new_cfg(supports_vision=True), api_key="k"))
    asyncio.run(registry.update_provider(cfg.id, name="X"))
    refreshed = asyncio.run(registry.get_provider(cfg.id))
    assert refreshed is not None and refreshed.supports_vision is True


def test_migrate_legacy_db_adds_supports_vision(tmp_path):
    db = tmp_path / "t.db"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE providers (id TEXT, name TEXT, provider_type TEXT, base_url TEXT, "
        "model TEXT, api_key TEXT, extra_headers_json TEXT, is_active INTEGER, "
        "created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO providers VALUES ('p1','n','openai-compatible','http://x','m','k','{}',"
        "1,'2026-01-01','2026-01-01')"
    )
    conn.commit()
    conn.close()
    registry = SQLiteProviderRegistry(str(db))
    cfg = asyncio.run(registry.get_provider("p1"))
    assert cfg is not None
    assert cfg.supports_vision is True


def test_migrate_legacy_db_keeps_non_openai_provider_without_vision(tmp_path):
    db = tmp_path / "t.db"
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE providers (id TEXT, name TEXT, provider_type TEXT, base_url TEXT, "
        "model TEXT, api_key TEXT, extra_headers_json TEXT, is_active INTEGER, "
        "created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO providers VALUES ('p1','n','anthropic','http://x','m','k','{}',"
        "1,'2026-01-01','2026-01-01')"
    )
    conn.commit()
    conn.close()
    registry = SQLiteProviderRegistry(str(db))
    cfg = asyncio.run(registry.get_provider("p1"))
    assert cfg is not None
    assert cfg.supports_vision is False
