"""T14: End-to-end entry-point plugin contract tests.

Exercises the FULL pipeline (discover -> effective enabled -> prepare ->
load_and_register -> register) via PluginFileLoader + PluginService, using
fake entry points (FakeEntryPoint with load counter). Covers admission (S1),
identity/selection (S2), and error/manifest matrix (S3).

These tests use a REAL PluginFileLoader (not a mock) so that discover ->
prepare -> load_and_register are exercised end-to-end. PluginService is
backed by a mock registry/tool_service to isolate the loader contract.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin_service import PluginService
from app.domain.plugin import PluginKind, PluginSource
from app.infrastructure.plugin.file_loader import (
    PluginFileLoader,
    PluginFileLoaderConfig,
)


# ---------------------------------------------------------------------------
# helpers (mirroring test_plugin_loader_phases.py patterns)
# ---------------------------------------------------------------------------


class FakeEntryPoint:
    """A stand-in for importlib.metadata.EntryPoint that counts load() calls
    and resolves from a pre-registered fake module in sys.modules."""

    def __init__(self, name, group, module_name, dist_version=None, load_raises=None):
        self.name = name
        self.group = group
        self.value = module_name
        self._module_name = module_name
        self._load_raises = load_raises
        self.load_count = 0
        self._dist_version = dist_version

    @property
    def dist(self):
        if self._dist_version is None:
            return None
        return types.SimpleNamespace(version=self._dist_version)

    def load(self):
        self.load_count += 1
        if self._load_raises is not None:
            raise self._load_raises
        return sys.modules[self._module_name]


def _install_fake_module(module_name, *, register_fn=None, plugin_manifest=None):
    module = types.ModuleType(module_name)
    if register_fn is not None:
        module.register = register_fn
    if plugin_manifest is not None:
        module.PLUGIN_MANIFEST = plugin_manifest
    sys.modules[module_name] = module
    return module


def _write_dir_plugin(
    root: Path,
    name: str = "hello",
    manifest_body: str | None = None,
    init_body: str | None = None,
    manifest_name: str = "plugin.yaml",
) -> Path:
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    if manifest_body is None:
        manifest_body = (
            f"name: {name}\nversion: 1.0.0\ndescription: demo\nkind: standalone\n"
            f"provides_tools:\n  - {name}\n"
        )
    (plugin_dir / manifest_name).write_text(manifest_body, encoding="utf-8")
    if init_body is None:
        init_body = (
            "def register(ctx):\n"
            f"    ctx.register_tool(name={name!r}, toolset={name!r}, "
            f"schema={{'name': {name!r}, 'parameters': {{'type': 'object'}}}}, "
            f"handler=lambda a, **k: {{'ok': True}})\n"
        )
    (plugin_dir / "__init__.py").write_text(init_body, encoding="utf-8")
    return plugin_dir


def _make_settings(**kwargs):
    return types.SimpleNamespace(
        plugins_enabled=kwargs.get("plugins_enabled", []),
        plugins_disabled=kwargs.get("plugins_disabled", []),
        plugins_override_allowlist=kwargs.get("plugins_override_allowlist", []),
        plugin_tool_timeout_seconds=kwargs.get("plugin_tool_timeout_seconds", 10),
        plugin_hook_timeout_seconds=kwargs.get("plugin_hook_timeout_seconds", 5.0),
    )


def _build_service(
    eps=None,
    *,
    settings=None,
    bundled_root=None,
    user_root=None,
    project_root=None,
    enable_project=False,
    enable_entrypoints=True,
):
    """Build a PluginService backed by a REAL PluginFileLoader with fake EPs.

    Returns (service, loader, registry, captured_routes).
    """
    loader = PluginFileLoader(PluginFileLoaderConfig(
        enable_entrypoints=enable_entrypoints,
        bundled_root=bundled_root,
        user_root=user_root,
        project_root=project_root,
        enable_project=enable_project,
    ))
    eps = eps or []
    loader._entry_points_for = lambda groups: [ep for ep in eps if ep.group in groups]

    registry = AsyncMock()
    registry.list_plugins.return_value = []
    registry.get_plugin.return_value = None
    registry.get_secret_config.return_value = {}
    registry.set_enabled.return_value = MagicMock()
    registry.replace_all_plugins.side_effect = lambda plugins: list(plugins)

    tool_service = MagicMock()
    tool_service.definitions = {}

    captured_routes = []

    def refresher(names):
        captured_routes.append(set(names))

    service = PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=refresher,
        settings=settings or _make_settings(),
    )
    return service, loader, registry, captured_routes


def _register_tool_fn(tool_name="ep_tool", toolset="ep"):
    """Return a register(ctx) function that registers one tool."""
    def register(ctx):
        ctx.register_tool(
            name=tool_name, toolset=toolset,
            schema={"name": tool_name, "parameters": {"type": "object"}},
            handler=lambda a, **k: {"ok": True},
        )
    return register


@pytest.fixture(autouse=True)
def _cleanup_fake_modules():
    """Remove fake modules starting with _fake_e2e_ after each test."""
    yield
    for name in list(sys.modules.keys()):
        if name.startswith("_fake_e2e_"):
            sys.modules.pop(name, None)


# ===========================================================================
# S1: Admission matrix
# ===========================================================================


async def test_admission_enable_entrypoints_false_no_load(tmp_path):
    """enable_entrypoints=False -> entry points not discovered, ep.load()==0."""
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_e2e_s1a")
    _install_fake_module("_fake_e2e_s1a", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["epplug"]),
        enable_entrypoints=False,
        user_root=tmp_path,
    )
    result = await service.scan()
    assert ep.load_count == 0
    # entry point not in discovered manifests
    assert not any(m.key == "epplug" for m in result.manifests)
    assert "epplug" not in result.registrations


async def test_admission_key_not_in_effective_enabled_no_load(tmp_path):
    """Entry point discovered (metadata only) but not in effective enabled ->
    ep.load()==0. Plugin appears in manifests but not in registrations."""
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_e2e_s1b")
    _install_fake_module("_fake_e2e_s1b", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=[]),  # epplug NOT enabled
    )
    result = await service.scan()
    assert ep.load_count == 0
    # manifest still discovered (metadata only, no ep.load)
    assert any(m.key == "epplug" for m in result.manifests)
    assert "epplug" not in result.registrations


async def test_admission_first_discovered_settings_enabled_loads_and_registers(tmp_path):
    """First-discovered entry point + settings.plugins_enabled -> ep.load()==1
    in the same scan, register success, NO directory __init__.py needed."""
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_e2e_s1c")
    _install_fake_module("_fake_e2e_s1c", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["epplug"]),
    )
    result = await service.scan()
    assert ep.load_count == 1
    assert "epplug" in result.registrations
    assert result.registrations["epplug"][0].name == "ep_tool"
    assert "epplug" not in result.errors
    # no directory involved: path is entrypoint:..., not a real directory
    ep_manifest = next(m for m in result.manifests if m.key == "epplug")
    assert ep_manifest.path == "entrypoint:n_agent.plugins:epplug"
    assert not Path(ep_manifest.path).exists()


async def test_admission_disabled_overrides_enabled(tmp_path):
    """settings.plugins_disabled always overrides settings.plugins_enabled ->
    key not in effective_enabled -> ep.load()==0."""
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_e2e_s1d")
    _install_fake_module("_fake_e2e_s1d", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(
            plugins_enabled=["epplug"],
            plugins_disabled=["epplug"],
        ),
    )
    result = await service.scan()
    assert ep.load_count == 0
    assert "epplug" not in result.registrations


# ===========================================================================
# S2: Identity / selection matrix
# ===========================================================================


async def test_both_groups_loadable_independently(tmp_path):
    """n_agent.plugins and hermes_agent.plugins are both loadable independently."""
    ep_n = FakeEntryPoint(name="plug_n", group="n_agent.plugins", module_name="_fake_e2e_s2a_n")
    ep_h = FakeEntryPoint(name="plug_h", group="hermes_agent.plugins", module_name="_fake_e2e_s2a_h")
    _install_fake_module("_fake_e2e_s2a_n", register_fn=_register_tool_fn("tool_n"))
    _install_fake_module("_fake_e2e_s2a_h", register_fn=_register_tool_fn("tool_h"))
    service, loader, registry, _ = _build_service(
        [ep_n, ep_h],
        settings=_make_settings(plugins_enabled=["plug_n", "plug_h"]),
    )
    result = await service.scan()
    assert ep_n.load_count == 1
    assert ep_h.load_count == 1
    assert "plug_n" in result.registrations
    assert "plug_h" in result.registrations
    assert result.registrations["plug_n"][0].name == "tool_n"
    assert result.registrations["plug_h"][0].name == "tool_h"


async def test_same_group_precise_selection(tmp_path):
    """Two entry points in the same group, different names -> both selected."""
    ep_a = FakeEntryPoint(name="plug_a", group="n_agent.plugins", module_name="_fake_e2e_s2b_a")
    ep_b = FakeEntryPoint(name="plug_b", group="n_agent.plugins", module_name="_fake_e2e_s2b_b")
    _install_fake_module("_fake_e2e_s2b_a", register_fn=_register_tool_fn("tool_a"))
    _install_fake_module("_fake_e2e_s2b_b", register_fn=_register_tool_fn("tool_b"))
    service, loader, registry, _ = _build_service(
        [ep_a, ep_b],
        settings=_make_settings(plugins_enabled=["plug_a", "plug_b"]),
    )
    result = await service.scan()
    assert ep_a.load_count == 1
    assert ep_b.load_count == 1
    assert "plug_a" in result.registrations
    assert "plug_b" in result.registrations


async def test_cross_group_same_name_n_agent_wins_with_warning(tmp_path):
    """Cross-group same-name collision -> n_agent.plugins wins,
    hermes_agent.plugins not loaded, duplicate_entrypoint warning."""
    ep_n = FakeEntryPoint(name="dup", group="n_agent.plugins", module_name="_fake_e2e_s2c_n")
    ep_h = FakeEntryPoint(name="dup", group="hermes_agent.plugins", module_name="_fake_e2e_s2c_h")
    _install_fake_module("_fake_e2e_s2c_n", register_fn=_register_tool_fn("tool_n"))
    _install_fake_module("_fake_e2e_s2c_h", register_fn=_register_tool_fn("tool_h"))
    service, loader, registry, _ = _build_service(
        [ep_n, ep_h],
        settings=_make_settings(plugins_enabled=["dup"]),
    )
    result = await service.scan()
    # n_agent wins; hermes not loaded
    assert ep_n.load_count == 1
    assert ep_h.load_count == 0
    assert "dup" in result.registrations
    assert result.registrations["dup"][0].name == "tool_n"
    # duplicate_entrypoint warning
    dup_warnings = [w for w in result.warnings if w.reason == "duplicate_entrypoint"]
    assert len(dup_warnings) == 1
    assert "hermes_agent.plugins" in (dup_warnings[0].detail or "")
    assert "n_agent.plugins" in (dup_warnings[0].detail or "")
    # winner path reflects n_agent group
    dup_manifest = next(m for m in result.manifests if m.key == "dup")
    assert dup_manifest.path == "entrypoint:n_agent.plugins:dup"


async def test_entry_point_vs_directory_conflict_entry_point_wins(tmp_path):
    """Entry-point vs directory key conflict -> entry_point source wins
    (source priority: bundled < user < project < entry_point)."""
    # directory plugin (user source) with same key as entry point
    _write_dir_plugin(tmp_path, "shared", init_body=(
        "def register(ctx):\n"
        "    raise RuntimeError('directory should not be loaded')\n"
    ))
    ep = FakeEntryPoint(name="shared", group="n_agent.plugins", module_name="_fake_e2e_s2d_ep")
    _install_fake_module("_fake_e2e_s2d_ep", register_fn=_register_tool_fn("ep_tool"))
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["shared"]),
        user_root=tmp_path,
    )
    result = await service.scan()
    # entry point wins; directory not loaded (no error from directory register)
    assert ep.load_count == 1
    assert "shared" in result.registrations
    assert result.registrations["shared"][0].name == "ep_tool"
    assert "shared" not in result.errors
    # source_shadowed warning records the conflict
    shadow_warnings = [w for w in result.warnings if w.reason == "source_shadowed"]
    assert len(shadow_warnings) == 1
    assert "winner=entry_point" in (shadow_warnings[0].detail or "")


async def test_distribution_version_missing_is_zero(tmp_path):
    """Distribution version missing -> manifest version == '0'."""
    ep = FakeEntryPoint(name="noversion", group="n_agent.plugins", module_name="_fake_e2e_s2e")
    _install_fake_module("_fake_e2e_s2e", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["noversion"]),
    )
    result = await service.scan()
    ep_manifest = next(m for m in result.manifests if m.key == "noversion")
    assert ep_manifest.version == "0"


async def test_distribution_version_from_dist(tmp_path):
    """Distribution version present -> manifest version from dist metadata."""
    ep = FakeEntryPoint(
        name="versioned", group="n_agent.plugins",
        module_name="_fake_e2e_s2f", dist_version="3.2.1",
    )
    _install_fake_module("_fake_e2e_s2f", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["versioned"]),
    )
    result = await service.scan()
    ep_manifest = next(m for m in result.manifests if m.key == "versioned")
    assert ep_manifest.version == "3.2.1"


async def test_path_exactly_entrypoint_group_name(tmp_path):
    """Path is exactly 'entrypoint:{group}:{name}' for both groups."""
    ep_n = FakeEntryPoint(name="pathn", group="n_agent.plugins", module_name="_fake_e2e_s2g_n")
    ep_h = FakeEntryPoint(name="pathh", group="hermes_agent.plugins", module_name="_fake_e2e_s2g_h")
    _install_fake_module("_fake_e2e_s2g_n", register_fn=_register_tool_fn())
    _install_fake_module("_fake_e2e_s2g_h", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep_n, ep_h],
        settings=_make_settings(plugins_enabled=["pathn", "pathh"]),
    )
    result = await service.scan()
    manifest_n = next(m for m in result.manifests if m.key == "pathn")
    manifest_h = next(m for m in result.manifests if m.key == "pathh")
    assert manifest_n.path == "entrypoint:n_agent.plugins:pathn"
    assert manifest_h.path == "entrypoint:hermes_agent.plugins:pathh"


# ===========================================================================
# S3: Error / manifest matrix
# ===========================================================================


async def test_ep_load_raises_entrypoint_load_failed(tmp_path):
    """ep.load() raises -> entrypoint_load_failed diagnostic, no traceback."""
    ep = FakeEntryPoint(
        name="badep", group="n_agent.plugins",
        module_name="_fake_e2e_s3a", load_raises=RuntimeError("kaboom from load"),
    )
    _install_fake_module("_fake_e2e_s3a", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["badep"]),
    )
    result = await service.scan()
    assert ep.load_count == 1  # attempted once
    assert "badep" in result.errors
    msg = result.errors["badep"]
    assert "entrypoint_load_failed" in msg
    assert "kaboom from load" in msg
    assert "Traceback" not in msg
    assert "File " not in msg


async def test_no_callable_register_register_failed(tmp_path):
    """Loaded module has no callable register -> register_failed, no traceback."""
    ep = FakeEntryPoint(name="noreg", group="n_agent.plugins", module_name="_fake_e2e_s3b")
    _install_fake_module("_fake_e2e_s3b")  # no register attr
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["noreg"]),
    )
    result = await service.scan()
    assert ep.load_count == 1
    assert "noreg" in result.errors
    msg = result.errors["noreg"]
    assert "register_failed" in msg
    assert "Traceback" not in msg


async def test_register_raises_register_failed(tmp_path):
    """register(ctx) raises -> register_failed, no traceback."""
    ep = FakeEntryPoint(name="badreg", group="n_agent.plugins", module_name="_fake_e2e_s3c")
    _install_fake_module("_fake_e2e_s3c", register_fn=lambda ctx: (
        (_ for _ in ()).throw(ValueError("register boom"))
    ))
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["badreg"]),
    )
    result = await service.scan()
    assert ep.load_count == 1
    assert "badreg" in result.errors
    msg = result.errors["badreg"]
    assert "register_failed" in msg
    assert "register boom" in msg
    assert "Traceback" not in msg


async def test_plugin_manifest_non_mapping_entrypoint_invalid_manifest(tmp_path):
    """PLUGIN_MANIFEST is a list (non-mapping) -> entrypoint_invalid_manifest."""
    ep = FakeEntryPoint(name="badpm", group="n_agent.plugins", module_name="_fake_e2e_s3d")
    _install_fake_module(
        "_fake_e2e_s3d",
        register_fn=_register_tool_fn(),
        plugin_manifest=["not", "a", "mapping"],
    )
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["badpm"]),
    )
    result = await service.scan()
    assert "badpm" in result.errors
    msg = result.errors["badpm"]
    assert "entrypoint_invalid_manifest" in msg
    assert "Traceback" not in msg


async def test_name_version_mismatch_warns_discovery_wins(tmp_path):
    """name/version mismatch in PLUGIN_MANIFEST -> warning, discovery identity wins."""
    ep = FakeEntryPoint(
        name="epplug", group="n_agent.plugins",
        module_name="_fake_e2e_s3e", dist_version="2.0.0",
    )
    _install_fake_module(
        "_fake_e2e_s3e",
        register_fn=_register_tool_fn(),
        plugin_manifest={"name": "different-name", "version": "0.0.1"},
    )
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["epplug"]),
    )
    result = await service.scan()
    # plugin still loads successfully (warnings are non-fatal)
    assert "epplug" in result.registrations
    assert "epplug" not in result.errors
    # prepare warnings surfaced
    prep_warnings = [w for w in result.warnings if w.reason == "prepare_warning"]
    assert any("name mismatch" in (w.detail or "") for w in prep_warnings)
    assert any("version mismatch" in (w.detail or "") for w in prep_warnings)
    # discovery identity wins: check the Plugin stored in registry
    replace_call = registry.replace_all_plugins.call_args
    plugins = replace_call.args[0]
    epplug = next(p for p in plugins if p.key == "epplug")
    assert epplug.name == "epplug"  # discovery name, not "different-name"
    assert epplug.version == "2.0.0"  # discovery version (from dist), not "0.0.1"


async def test_key_source_path_drift_warned_no_drift(tmp_path):
    """key/source/path in PLUGIN_MANIFEST are ignored (drift warned)."""
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_e2e_s3f")
    _install_fake_module(
        "_fake_e2e_s3f",
        register_fn=_register_tool_fn(),
        plugin_manifest={
            "key": "drift-key",
            "source": "bundled",
            "path": "/somewhere/else",
            "name": "epplug",
            "version": "0",
        },
    )
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["epplug"]),
    )
    result = await service.scan()
    assert "epplug" in result.registrations
    prep_warnings = [w for w in result.warnings if w.reason == "prepare_warning"]
    assert any("key drift" in (w.detail or "") for w in prep_warnings)
    assert any("source drift" in (w.detail or "") for w in prep_warnings)
    assert any("path drift" in (w.detail or "") for w in prep_warnings)
    # discovery identity not drifted
    replace_call = registry.replace_all_plugins.call_args
    plugins = replace_call.args[0]
    epplug = next(p for p in plugins if p.key == "epplug")
    assert epplug.key == "epplug"
    assert epplug.source is PluginSource.ENTRY_POINT
    assert epplug.source_path == "entrypoint:n_agent.plugins:epplug"


async def test_dependency_fields_supplemented_from_plugin_manifest(tmp_path):
    """pip_dependencies/requires_plugins supplemented from PLUGIN_MANIFEST
    must flow through to the service's Plugin objects and dependency checking."""
    ep = FakeEntryPoint(name="depplug", group="n_agent.plugins", module_name="_fake_e2e_s3g")
    _install_fake_module(
        "_fake_e2e_s3g",
        register_fn=_register_tool_fn(),
        plugin_manifest={
            "pip_dependencies": ["requests"],
            "requires_plugins": ["other_plug"],
            "description": "deps from PLUGIN_MANIFEST",
        },
    )
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["depplug"]),
    )
    result = await service.scan()
    # plugin loads (requires_plugins references a non-discovered plugin, so
    # dep availability fails, but the plugin still registers if deps are
    # only checked for admission -- actually deps_ok would be False since
    # "other_plug" is not discovered. Let's check the behavior:
    # The plugin should have the supplemented fields in its manifest.
    replace_call = registry.replace_all_plugins.call_args
    plugins = replace_call.args[0]
    depplug = next(p for p in plugins if p.key == "depplug")
    # The stored manifest must carry supplemented dependency fields
    assert "requests" in depplug.manifest.get("pip_dependencies", [])
    assert "other_plug" in depplug.manifest.get("requires_plugins", [])
    assert depplug.description == "deps from PLUGIN_MANIFEST"


async def test_token_per_scan_not_reused_across_scans(tmp_path):
    """Prepare token is valid per-scan; ep.load() is called once per scan,
    NOT reused across scans."""
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_e2e_s3h")
    _install_fake_module("_fake_e2e_s3h", register_fn=_register_tool_fn())
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["epplug"]),
    )
    # scan 1
    result1 = await service.scan()
    assert ep.load_count == 1
    assert "epplug" in result1.registrations
    # scan 2: token from scan 1 is NOT reused; ep.load() called again
    result2 = await service.scan()
    assert ep.load_count == 2
    assert "epplug" in result2.registrations


async def test_public_errors_have_no_traceback_all_error_cases(tmp_path):
    """All error cases produce public diagnostics without tracebacks."""
    # Case 1: ep.load raises
    ep1 = FakeEntryPoint(
        name="err_load", group="n_agent.plugins",
        module_name="_fake_e2e_s3i1", load_raises=RuntimeError("load boom" + "X" * 200),
    )
    _install_fake_module("_fake_e2e_s3i1", register_fn=_register_tool_fn())

    # Case 2: register raises
    ep2 = FakeEntryPoint(name="err_reg", group="n_agent.plugins", module_name="_fake_e2e_s3i2")
    def _raising_register(ctx):
        raise RuntimeError("register boom" + "Y" * 200)
    _install_fake_module("_fake_e2e_s3i2", register_fn=_raising_register)

    # Case 3: PLUGIN_MANIFEST non-mapping
    ep3 = FakeEntryPoint(name="err_pm", group="n_agent.plugins", module_name="_fake_e2e_s3i3")
    _install_fake_module(
        "_fake_e2e_s3i3",
        register_fn=_register_tool_fn(),
        plugin_manifest="not-a-mapping",
    )

    service, loader, registry, _ = _build_service(
        [ep1, ep2, ep3],
        settings=_make_settings(plugins_enabled=["err_load", "err_reg", "err_pm"]),
    )
    result = await service.scan()

    for key in ("err_load", "err_reg", "err_pm"):
        assert key in result.errors, f"expected error for {key}"
        msg = result.errors[key]
        assert "Traceback" not in msg, f"traceback leaked in {key}: {msg}"
        assert "File " not in msg, f"file path leaked in {key}: {msg}"


async def test_prepare_warnings_surfaced_even_when_load_fails(tmp_path):
    """Prepare warnings (name/version mismatch, drift) are surfaced in
    PluginScanResult.warnings even when load_and_register subsequently fails."""
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_e2e_s3j")
    def _raising_register(ctx):
        raise ValueError("register boom")
    _install_fake_module(
        "_fake_e2e_s3j",
        register_fn=_raising_register,
        plugin_manifest={"name": "different-name", "key": "drift-key"},
    )
    service, loader, registry, _ = _build_service(
        [ep],
        settings=_make_settings(plugins_enabled=["epplug"]),
    )
    result = await service.scan()
    # load failed
    assert "epplug" in result.errors
    assert "register_failed" in result.errors["epplug"]
    # but prepare warnings still surfaced
    prep_warnings = [w for w in result.warnings if w.reason == "prepare_warning"]
    assert any("name mismatch" in (w.detail or "") for w in prep_warnings)
    assert any("key drift" in (w.detail or "") for w in prep_warnings)
