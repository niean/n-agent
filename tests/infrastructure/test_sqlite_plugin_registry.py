from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.domain.plugin import (
    Plugin,
    PluginKind,
    PluginScanStatus,
    PluginSource,
)
from app.infrastructure.registry.sqlite_plugin_registry import SQLitePluginRegistry


def _make_plugin(
    key: str = "hello",
    name: str = "hello",
    source: PluginSource = PluginSource.BUNDLED,
    enabled: bool = False,
    config: dict | None = None,
    version: str = "1.0.0",
) -> Plugin:
    return Plugin(
        id=f"plg-{key}",
        key=key,
        name=name,
        source=source,
        enabled=enabled,
        version=version,
        description=f"{key} plugin",
        kind=PluginKind.STANDALONE,
        config=config or {},
    )


@pytest.mark.asyncio
async def test_upsert_and_get_plugin(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    plugin = _make_plugin(enabled=True, config={"k": "v"})
    await reg.upsert_plugin(plugin)
    fetched = await reg.get_plugin("hello")
    assert fetched is not None
    assert fetched.key == "hello"
    assert fetched.enabled is True
    assert fetched.config == {"k": "v"}


@pytest.mark.asyncio
async def test_set_enabled(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin(enabled=False))
    updated = await reg.set_enabled("hello", True)
    assert updated.enabled is True
    fetched = await reg.get_plugin("hello")
    assert fetched.enabled is True


@pytest.mark.asyncio
async def test_delete_plugin(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin())
    assert await reg.delete_plugin("hello") is True
    assert await reg.get_plugin("hello") is None
    assert await reg.delete_plugin("hello") is False


@pytest.mark.asyncio
async def test_update_config_stores_secret(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin())
    await reg.update_config(
        "hello",
        {"endpoint": "http://example.com"},
        secret_updates={"api_key": "secret-value"},
    )
    plugin = await reg.get_plugin("hello")
    assert plugin.config == {"endpoint": "http://example.com"}
    secrets = await reg.get_secret_config("hello")
    assert secrets == {"api_key": "secret-value"}


@pytest.mark.asyncio
async def test_replace_all_preserves_enabled_config_secrets(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin(enabled=True, config={"k": "v"}))
    await reg.update_config("hello", {"k": "v"}, secret_updates={"api_key": "secret-value"})
    # 重扫 replace_all：manifest 字段变化但 enabled/config/secret 应保留
    await reg.replace_all_plugins([
        Plugin(
            id="plg-hello",
            key="hello",
            name="hello",
            source=PluginSource.BUNDLED,
            enabled=False,
            version="2.0.0",
            description="updated",
            kind=PluginKind.STANDALONE,
            config={},
        ),
    ])
    plugin = await reg.get_plugin("hello")
    assert plugin is not None
    assert plugin.enabled is True  # 保留
    assert plugin.config == {"k": "v"}  # 保留
    assert plugin.version == "2.0.0"  # manifest 更新
    secrets = await reg.get_secret_config("hello")
    assert secrets == {"api_key": "secret-value"}  # 保留


@pytest.mark.asyncio
async def test_replace_all_marks_missing_plugins(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin(key="old1", enabled=True))
    # replace_all 不包含 old1 -> 标记 missing
    await reg.replace_all_plugins([_make_plugin(key="new1", enabled=False)])
    old1 = await reg.get_plugin("old1")
    assert old1 is not None
    assert old1.enabled is False  # missing 后强制禁用
    assert old1.last_scan_status == PluginScanStatus.MISSING.value
    assert old1.last_scan_error is not None


@pytest.mark.asyncio
async def test_list_plugins_filters_disabled(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin(key="a", enabled=True))
    await reg.upsert_plugin(_make_plugin(key="b", enabled=False))
    enabled_only = await reg.list_plugins(include_disabled=False)
    assert {p.key for p in enabled_only} == {"a"}
    all_plugins = await reg.list_plugins(include_disabled=True)
    assert {p.key for p in all_plugins} == {"a", "b"}


@pytest.mark.asyncio
async def test_secret_not_in_public_view(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin())
    await reg.update_config("hello", {}, secret_updates={"api_key": "super-secret"})
    plugins = await reg.list_plugins()
    view = plugins[0].to_public_view()
    assert view["secret_refs"] == {"api_key": True}
    assert "super-secret" not in str(view)


@pytest.mark.asyncio
async def test_get_secret_config_returns_empty_when_no_secrets(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin())
    assert await reg.get_secret_config("hello") == {}


@pytest.mark.asyncio
async def test_get_plugin_populates_secret_refs(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin())
    await reg.update_config("hello", {}, secret_updates={"api_key": "v1", "token": "v2"})
    plugin = await reg.get_plugin("hello")
    assert plugin is not None
    view = plugin.to_public_view()
    assert view["secret_refs"] == {"api_key": True, "token": True}


@pytest.mark.asyncio
async def test_list_plugins_populates_secret_refs(tmp_path):
    reg = SQLitePluginRegistry(tmp_path / "test.db")
    await reg.upsert_plugin(_make_plugin(key="a"))
    await reg.upsert_plugin(_make_plugin(key="b"))
    await reg.update_config("a", {}, secret_updates={"api_key": "v1"})
    plugins = await reg.list_plugins()
    refs = {p.key: p.to_public_view()["secret_refs"] for p in plugins}
    assert refs["a"] == {"api_key": True}
    assert refs["b"] == {}
