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


def _write_hello_plugin(root: Path, name: str = "hello", manifest_name: str = "plugin.yaml") -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / manifest_name).write_text(
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


def _write_two_level_plugin(root: Path, category: str, name: str, manifest_name: str = "plugin.yaml") -> Path:
    plugin_dir = root / category / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / manifest_name).write_text(
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
        "    ctx.register_command('cmd', lambda: None)\n"
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
    assert "command" in result.unsupported["hooky"]


@pytest.mark.asyncio
async def test_loader_implements_protocol():
    loader = PluginFileLoader(PluginFileLoaderConfig())
    assert isinstance(loader, PluginFileLoaderProtocol)


def test_skip_names_default_contains_platforms():
    config = PluginFileLoaderConfig()
    assert config.skip_names == frozenset({"platforms"})


def test_skip_names_custom_replaces_default():
    config = PluginFileLoaderConfig(skip_names=frozenset({"ignored"}))
    assert config.skip_names == frozenset({"ignored"})
    assert "platforms" not in config.skip_names


def test_skip_names_empty_frozenset_allowed():
    config = PluginFileLoaderConfig(skip_names=frozenset())
    assert config.skip_names == frozenset()


def test_manifest_path_for_prefers_plugin_yaml(tmp_path):
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text("name: yaml\n", encoding="utf-8")
    (plugin_dir / "plugin.yml").write_text("name: yml\n", encoding="utf-8")

    result = loader._manifest_path_for(plugin_dir)

    assert result == plugin_dir / "plugin.yaml"


def test_manifest_path_for_falls_back_to_plugin_yml(tmp_path):
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yml").write_text("name: yml\n", encoding="utf-8")

    result = loader._manifest_path_for(plugin_dir)

    assert result == plugin_dir / "plugin.yml"


def test_manifest_path_for_returns_none_when_no_manifest(tmp_path):
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()

    assert loader._manifest_path_for(plugin_dir) is None


@pytest.mark.asyncio
async def test_scan_flat_plugin_yml(tmp_path):
    _write_hello_plugin(tmp_path, "hello", manifest_name="plugin.yml")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"hello"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    assert "hello" in {m.key for m in result.manifests}
    assert "hello" in result.registrations


@pytest.mark.asyncio
async def test_scan_two_level_plugin_yml(tmp_path):
    _write_two_level_plugin(tmp_path, "web", "exa", manifest_name="plugin.yml")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"web/exa"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    assert "web/exa" in {m.key for m in result.manifests}
    assert "web/exa" in result.registrations


@pytest.mark.asyncio
async def test_scan_plugin_yaml_preferred_over_plugin_yml(tmp_path):
    plugin_dir = _write_hello_plugin(tmp_path, "demo")
    (plugin_dir / "plugin.yaml").write_text(
        "name: from-yaml\nversion: 1.0.0\ndescription: yaml-description\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "plugin.yml").write_text(
        "name: from-yml\nversion: 1.0.0\ndescription: yml-description\nkind: standalone\n",
        encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"demo"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    manifest = next(m for m in result.manifests if m.key == "demo")
    assert manifest.description == "yaml-description"


@pytest.mark.asyncio
async def test_scan_skip_names_default_skips_platforms(tmp_path):
    _write_two_level_plugin(tmp_path, "platforms", "feishu")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"platforms/feishu"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    assert "platforms/feishu" not in {m.key for m in result.manifests}


@pytest.mark.asyncio
async def test_scan_skip_names_custom_replaces_default(tmp_path):
    _write_two_level_plugin(tmp_path, "ignored", "p1")
    _write_two_level_plugin(tmp_path, "platforms", "p2")
    loader = PluginFileLoader(
        PluginFileLoaderConfig(user_root=tmp_path, skip_names=frozenset({"ignored"}))
    )

    result = await loader.scan(
        enabled_keys={"ignored/p1", "platforms/p2"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    keys = {m.key for m in result.manifests}
    assert "ignored/p1" not in keys
    assert "platforms/p2" in keys


@pytest.mark.asyncio
async def test_scan_skip_names_empty_allows_platforms(tmp_path):
    _write_two_level_plugin(tmp_path, "platforms", "p1")
    loader = PluginFileLoader(
        PluginFileLoaderConfig(user_root=tmp_path, skip_names=frozenset())
    )

    result = await loader.scan(
        enabled_keys={"platforms/p1"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    assert "platforms/p1" in {m.key for m in result.manifests}


@pytest.mark.asyncio
async def test_scan_depth_cap_excludes_three_level_plugins(tmp_path):
    plugin_dir = tmp_path / "a" / "b" / "c"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: c\nversion: 1.0.0\ndescription: deep\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_tool(name='c', toolset='c', schema={'name':'c','parameters':{'type':'object'}}, handler=lambda a, **k: {'ok': True})\n",
        encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"a/b/c"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    assert "a/b/c" not in {m.key for m in result.manifests}


@pytest.mark.asyncio
async def test_scan_prefix_accumulates_category_key(tmp_path):
    _write_two_level_plugin(tmp_path, "web", "exa")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"web/exa"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    assert {m.key for m in result.manifests} == {"web/exa"}


@pytest.mark.asyncio
async def test_scan_skips_hidden_and_pycache_directories_at_all_levels(tmp_path):
    _write_hello_plugin(tmp_path / ".hidden", "secret")
    _write_hello_plugin(tmp_path / "__pycache__", "cached")
    _write_two_level_plugin(tmp_path, "web", ".hidden")
    _write_two_level_plugin(tmp_path, "web", "__pycache__")
    _write_two_level_plugin(tmp_path, "web", "visible")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"web/visible", ".hidden/secret", "__pycache__/cached"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    keys = {m.key for m in result.manifests}
    assert "web/visible" in keys
    assert ".hidden/secret" not in keys
    assert "__pycache__/cached" not in keys
    assert "web/.hidden" not in keys
    assert "web/__pycache__" not in keys


@pytest.mark.asyncio
async def test_scan_max_plugins_shared_across_recursion_and_warns_once(tmp_path):
    _write_hello_plugin(tmp_path, "a-flat")
    _write_two_level_plugin(tmp_path, "b-category", "nested")
    _write_hello_plugin(tmp_path, "c-late")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path, max_plugins=2))

    result = await loader.scan(
        enabled_keys={"a-flat", "b-category/nested", "c-late"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    keys = {m.key for m in result.manifests}
    assert keys == {"a-flat", "b-category/nested"}
    max_plugins_warnings = [w for w in result.warnings if w.reason == "max_plugins_exceeded"]
    assert len(max_plugins_warnings) == 1
    assert max_plugins_warnings[0].relative_path == str(tmp_path)


@pytest.mark.asyncio
async def test_scan_path_escape_uses_validate_return_value(tmp_path, monkeypatch):
    _write_hello_plugin(tmp_path, "escaped")

    def fake_validate(path, root):
        return "simulated escape error"

    monkeypatch.setattr("app.infrastructure.path_security.validate_within_dir", fake_validate)
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"escaped"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    escape_warnings = [w for w in result.warnings if w.reason == "path_escape"]
    assert len(escape_warnings) == 1
    assert escape_warnings[0].relative_path == str(tmp_path / "escaped")
    assert escape_warnings[0].detail == "simulated escape error"
    assert "escaped" not in {m.key for m in result.manifests}


@pytest.mark.asyncio
async def test_scan_plugin_yml_yaml_parse_warning_relative_path(tmp_path):
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yml").write_text("name: [unterminated\n", encoding="utf-8")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    result = await loader.scan(
        enabled_keys={"demo"},
        disabled_keys=set(),
        config_provider=lambda k: {},
        secret_provider=lambda k: {},
    )

    yaml_warnings = [w for w in result.warnings if w.reason == "yaml_parse_error"]
    assert len(yaml_warnings) == 1
    assert yaml_warnings[0].relative_path == str(plugin_dir / "plugin.yml")
    assert "demo" not in {m.key for m in result.manifests}
