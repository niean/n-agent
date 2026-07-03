from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.application.plugin_service import PluginFileLoaderProtocol
from app.domain.plugin import PluginKind
from app.infrastructure.plugin.file_loader import (
    PluginFileLoader,
    PluginFileLoaderConfig,
)


def _write_hello_plugin(root: Path, name: str = "hello") -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ndescription: demo\nkind: standalone\nprovides_tools:\n  - {name}\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "from . import schemas, tools\n"
        "def register(ctx):\n"
        f"    ctx.register_tool(name={name!r}, toolset={name!r}, schema=schemas.SCHEMA, handler=tools.handle, description='demo')\n",
        encoding="utf-8",
    )
    (plugin_dir / "schemas.py").write_text(
        "SCHEMA = {\n"
        f"    'name': {name!r},\n"
        "    'description': 'demo',\n"
        "    'parameters': {'type': 'object', 'properties': {'name': {'type': 'string'}}},\n"
        "}\n",
        encoding="utf-8",
    )
    (plugin_dir / "tools.py").write_text(
        "def handle(args, **kwargs):\n"
        f"    return {{'message': f'Hello, {{args.get(\"name\", \"{name}\")}}!'}}\n",
        encoding="utf-8",
    )
    return plugin_dir


def _write_two_level_plugin(root: Path, category: str, name: str) -> Path:
    plugin_dir = root / category / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ndescription: nested\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    ctx.register_tool(name={name!r}, toolset={name!r}, schema={{'name': {name!r}, 'parameters': {{'type': 'object'}}}}, handler=lambda a, **k: {{'ok': True}})\n",
        encoding="utf-8",
    )
    return plugin_dir


@pytest.mark.asyncio
async def test_scan_flat_directory(tmp_path):
    _write_hello_plugin(tmp_path, "hello")
    loader = PluginFileLoader(PluginFileLoaderConfig(
        bundled_root=None,
        user_root=tmp_path,
        project_root=None,
    ))
    result = await loader.scan(
        enabled_keys={"hello"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    keys = {m.key for m in result.manifests}
    assert "hello" in keys
    assert "hello" in result.registrations
    assert result.registrations["hello"][0].name == "hello"


@pytest.mark.asyncio
async def test_scan_two_level_directory(tmp_path):
    _write_two_level_plugin(tmp_path, "web", "exa")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = await loader.scan(
        enabled_keys={"web/exa"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    keys = {m.key for m in result.manifests}
    assert "web/exa" in keys
    assert "web/exa" in result.registrations


@pytest.mark.asyncio
async def test_scan_safe_mode_keeps_manifests_but_skips_register(tmp_path):
    _write_hello_plugin(tmp_path, "hello")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path, safe_mode=True))
    result = await loader.scan(
        enabled_keys={"hello"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    assert any(m.key == "hello" for m in result.manifests)
    assert result.registrations == {}


@pytest.mark.asyncio
async def test_scan_failed_import_records_error(tmp_path):
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: broken\nversion: 1.0.0\ndescription: b\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "raise RuntimeError('boom on import')\n",
        encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = await loader.scan(
        enabled_keys={"broken"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    assert "broken" in result.errors
    assert "RuntimeError" in result.errors["broken"]


@pytest.mark.asyncio
async def test_scan_missing_init_records_error(tmp_path):
    plugin_dir = tmp_path / "noinit"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: noinit\nversion: 1.0.0\ndescription: n\nkind: standalone\n",
        encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = await loader.scan(
        enabled_keys={"noinit"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    assert "noinit" in result.errors


@pytest.mark.asyncio
async def test_source_precedence_user_overrides_bundled(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    _write_hello_plugin(bundled, "hello")
    _write_hello_plugin(user, "hello")
    (user / "hello" / "tools.py").write_text(
        "def handle(args, **kwargs):\n    return {'message': 'from-user'}\n",
        encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(
        bundled_root=bundled,
        user_root=user,
    ))
    result = await loader.scan(
        enabled_keys={"hello"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    manifest = next(m for m in result.manifests if m.key == "hello")
    assert manifest.source.value == "user"


@pytest.mark.asyncio
async def test_disabled_plugin_not_executed(tmp_path):
    _write_hello_plugin(tmp_path, "hello")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = await loader.scan(
        enabled_keys=set(),
        disabled_keys={"hello"},
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    assert "hello" in {m.key for m in result.manifests}
    assert "hello" not in result.registrations


@pytest.mark.asyncio
async def test_backend_plugin_not_executed(tmp_path):
    plugin_dir = tmp_path / "backend1"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: backend1\nversion: 1.0.0\ndescription: b\nkind: backend\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n    ctx.register_tool(name='b1', toolset='b1', schema={'name':'b1','parameters':{'type':'object'}}, handler=lambda a, **k: '')\n",
        encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = await loader.scan(
        enabled_keys={"backend1"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    assert "backend1" in {m.key for m in result.manifests}
    assert "backend1" not in result.registrations


@pytest.mark.asyncio
async def test_unsupported_api_does_not_break_scan(tmp_path):
    plugin_dir = tmp_path / "hooky"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: hooky\nversion: 1.0.0\ndescription: h\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_hook('pre_tool_call', lambda: None)\n"
        "    ctx.register_tool(name='hooky_tool', toolset='hooky', schema={'name':'hooky_tool','parameters':{'type':'object'}}, handler=lambda a, **k: {'ok': True})\n",
        encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = await loader.scan(
        enabled_keys={"hooky"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )
    assert "hooky" in result.registrations
    assert "hooky" in result.unsupported
    assert "hook" in result.unsupported["hooky"]


@pytest.mark.asyncio
async def test_loader_implements_protocol():
    loader = PluginFileLoader(PluginFileLoaderConfig())
    assert isinstance(loader, PluginFileLoaderProtocol)
