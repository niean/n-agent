"""Read-only SQLite port set used by `n-agent config export` / import dry-run.

The migration surface must be able to open an existing deployment DB without
creating it, without running DDL, and without any chance of a stray write.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.provider import ProviderConfig
from app.infrastructure.registry.config_bundle_readers import (
    ConfigBundleRegistries,
    build_config_bundle_registries,
)


def _provider(pid: str, name: str) -> ProviderConfig:
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    return ProviderConfig(
        id=pid,
        name=name,
        provider_type="openai-compatible",
        base_url="https://api.example.com",
        model="gpt-x",
        api_key_present=True,
        is_active=False,
        extra_headers=None,
        created_at=now,
        updated_at=now,
    )


def _seed_writable(db_path: Path) -> ConfigBundleRegistries:
    """A read-write port set creates the schema exactly like the app does."""
    return build_config_bundle_registries(db_path, read_only=False)


def test_read_only_set_refuses_to_create_a_missing_database(tmp_path: Path):
    db_path = tmp_path / "absent" / "sessions.db"
    with pytest.raises(sqlite3.OperationalError):
        build_config_bundle_registries(db_path, read_only=True)
    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_read_only_set_reads_rows_written_by_the_write_set(tmp_path: Path):
    import asyncio

    db_path = tmp_path / "sessions.db"
    writable = _seed_writable(db_path)
    asyncio.run(writable.provider_registry.create_provider(_provider("p1", "primary"), "sk-live"))

    readers = build_config_bundle_registries(db_path, read_only=True)
    rows = asyncio.run(readers.provider_registry.list_providers())
    assert [r.name for r in rows] == ["primary"]
    assert asyncio.run(readers.provider_registry.get_secret("p1")) == "sk-live"


def test_read_only_set_rejects_every_write(tmp_path: Path):
    import asyncio

    db_path = tmp_path / "sessions.db"
    _seed_writable(db_path)
    readers = build_config_bundle_registries(db_path, read_only=True)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(readers.provider_registry.create_provider(_provider("p2", "second"), "sk-live"))


def test_read_only_set_covers_every_bundle_section(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _seed_writable(db_path)
    readers = build_config_bundle_registries(db_path, read_only=True)
    for field in (
        "provider_registry",
        "knowledge_registry",
        "mcp_registry",
        "external_memory_provider_registry",
        "external_memory_config",
        "plugin_registry",
        "skill_registry",
        "schedule_registry",
        "gateway_registry",
        "task_config_store",
        "memory_store",
    ):
        assert getattr(readers, field) is not None


def test_read_only_set_does_not_touch_the_filesystem(tmp_path: Path):
    db_path = tmp_path / "sessions.db"
    _seed_writable(db_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    mtime = db_path.stat().st_mtime_ns
    build_config_bundle_registries(db_path, read_only=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert db_path.stat().st_mtime_ns == mtime


def test_write_set_does_not_seed_or_scan(tmp_path: Path, monkeypatch):
    """The bundle write assembly reuses an existing schema; it must never run
    the provider/KB seeders or the skill/plugin scanners."""
    import app.main as app_main

    calls: list[str] = []
    for name in ("seed_default_plugins", "seed_default_skills", "build_application_services"):
        if hasattr(app_main, name):
            monkeypatch.setattr(app_main, name, lambda *a, _n=name, **k: calls.append(_n))
    build_config_bundle_registries(tmp_path / "sessions.db", read_only=False)
    assert calls == []
