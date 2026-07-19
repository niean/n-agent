from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from app.domain.plugin import PluginKind, PluginSource, PluginValidationError
from app.infrastructure.plugin.file_loader import (
    DirectoryLoadFailed,
    DiscoveryCandidate,
    EntrypointLoadFailed,
    LoaderToken,
    PluginFileLoader,
    PluginFileLoaderConfig,
    PluginLoaderError,
    PluginRegisterFailed,
    PreparedPlugin,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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


def _install_fake_module(
    module_name: str,
    *,
    register_fn=None,
    plugin_manifest=None,
):
    module = types.ModuleType(module_name)
    if register_fn is not None:
        module.register = register_fn
    if plugin_manifest is not None:
        module.PLUGIN_MANIFEST = plugin_manifest
    sys.modules[module_name] = module
    return module


def _make_loader_with_eps(eps, **config_kwargs):
    loader = PluginFileLoader(PluginFileLoaderConfig(enable_entrypoints=True, **config_kwargs))
    loader._entry_points_for = lambda groups: [ep for ep in eps if ep.group in groups]
    return loader


# ===========================================================================
# S1: discover()
# ===========================================================================


def test_discover_reads_directory_yaml_without_importing(tmp_path):
    # __init__.py raises on import; discover() must not trigger it.
    _write_dir_plugin(
        tmp_path, "boom",
        init_body="raise RuntimeError('discover must not import')\n",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = loader.discover()
    candidates = [c for c in result.candidates if c.key == "boom"]
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand.status == "ok"
    assert cand.source is PluginSource.USER
    assert cand.path == str(tmp_path / "boom")
    assert cand.diagnostic is None
    assert cand.manifest is not None
    assert cand.manifest.key == "boom"
    # discovery_index is stable and assigned
    assert cand.discovery_index == 0


def test_discover_entry_point_metadata_only_no_ep_load(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_mod_discover")
    _install_fake_module("_fake_ep_mod_discover", register_fn=lambda ctx: None)
    loader = _make_loader_with_eps([ep])
    result = loader.discover()
    cand = next(c for c in result.candidates if c.key == "epplug")
    assert cand.status == "ok"
    assert cand.source is PluginSource.ENTRY_POINT
    assert cand.path == "entrypoint:n_agent.plugins:epplug"
    assert cand.manifest.name == "epplug"
    assert cand.manifest.version == "0"  # no distribution version
    # The headline assertion: discover() never calls ep.load().
    assert ep.load_count == 0
    assert cand.entry_point is ep


def test_discover_corrupt_yaml_produces_failed_candidate(tmp_path):
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text("name: [unterminated\n", encoding="utf-8")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = loader.discover()
    cand = next(c for c in result.candidates if c.key == "broken")
    assert cand.status == "failed"
    assert cand.manifest is None
    assert cand.diagnostic is not None
    assert cand.diagnostic.startswith("yaml_parse_error:")
    # key/source/path still present so fail-closed resolution can shadow
    assert cand.key == "broken"
    assert cand.source is PluginSource.USER


def test_discover_invalid_manifest_produces_failed_candidate(tmp_path):
    plugin_dir = tmp_path / "badkind"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: badkind\nversion: 1.0.0\nkind: not-a-real-kind\n", encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = loader.discover()
    cand = next(c for c in result.candidates if c.key == "badkind")
    assert cand.status == "failed"
    assert cand.manifest is None
    assert cand.diagnostic is not None
    assert cand.diagnostic.startswith("invalid_manifest:")


def test_discovery_index_stable_across_sources(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    _write_dir_plugin(bundled, "a")
    _write_dir_plugin(user, "b")
    loader = PluginFileLoader(PluginFileLoaderConfig(bundled_root=bundled, user_root=user))
    result = loader.discover()
    indexed = sorted(result.candidates, key=lambda c: c.discovery_index)
    assert [c.discovery_index for c in indexed] == [0, 1]
    assert indexed[0].source is PluginSource.BUNDLED
    assert indexed[1].source is PluginSource.USER


def test_source_priority_bundled_lt_user_lt_project_lt_entry_point(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    project = tmp_path / "project"
    bundled.mkdir()
    user.mkdir()
    project.mkdir()
    _write_dir_plugin(bundled, "shared")
    _write_dir_plugin(user, "shared")
    _write_dir_plugin(project, "shared")
    ep = FakeEntryPoint(name="shared", group="n_agent.plugins", module_name="_fake_ep_shared")
    _install_fake_module("_fake_ep_shared", register_fn=lambda ctx: None)
    loader = _make_loader_with_eps([ep], enable_project=True, project_root=project, bundled_root=bundled, user_root=user)
    result = loader.discover()
    winner = result.winners["shared"]
    assert winner.source is PluginSource.ENTRY_POINT
    # all four candidates present
    sources = {c.source for c in result.candidates if c.key == "shared"}
    assert sources == {PluginSource.BUNDLED, PluginSource.USER, PluginSource.PROJECT, PluginSource.ENTRY_POINT}


def test_corrupted_higher_priority_shadows_lower_fail_closed(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    bundled.mkdir()
    user.mkdir()
    # valid bundled plugin
    _write_dir_plugin(bundled, "shared")
    # corrupted USER plugin (higher priority) -- must shadow bundled even though broken
    bad = user / "shared"
    bad.mkdir()
    (bad / "plugin.yaml").write_text("name: [bad\n", encoding="utf-8")
    loader = PluginFileLoader(PluginFileLoaderConfig(bundled_root=bundled, user_root=user))
    result = loader.discover()
    winner = result.winners["shared"]
    # fail-closed: the broken USER candidate wins
    assert winner.source is PluginSource.USER
    assert winner.status == "failed"
    assert winner.manifest is None
    # a shadowing warning records winner + shadowed sources
    shadow = [w for w in result.warnings if w.reason == "source_shadowed"]
    assert len(shadow) == 1
    assert "winner=user" in (shadow[0].detail or "")
    assert "shadowed=bundled" in (shadow[0].detail or "")


def test_project_shadows_user_shadows_bundled_when_all_ok(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for d in (bundled, user, project):
        d.mkdir()
        _write_dir_plugin(d, "shared")
    loader = PluginFileLoader(PluginFileLoaderConfig(
        bundled_root=bundled, user_root=user, project_root=project, enable_project=True,
    ))
    result = loader.discover()
    assert result.winners["shared"].source is PluginSource.PROJECT


def test_entry_point_group_collision_n_agent_wins(tmp_path):
    ep_nagent = FakeEntryPoint(name="dup", group="n_agent.plugins", module_name="_fake_dup_n")
    ep_hermes = FakeEntryPoint(name="dup", group="hermes_agent.plugins", module_name="_fake_dup_h")
    _install_fake_module("_fake_dup_n", register_fn=lambda ctx: None)
    _install_fake_module("_fake_dup_h", register_fn=lambda ctx: None)
    loader = _make_loader_with_eps([ep_nagent, ep_hermes])
    result = loader.discover()
    winner = result.winners["dup"]
    assert winner.entry_point is ep_nagent
    assert winner.path == "entrypoint:n_agent.plugins:dup"
    dup_warnings = [w for w in result.warnings if w.reason == "duplicate_entrypoint"]
    assert len(dup_warnings) == 1
    assert "hermes_agent.plugins" in (dup_warnings[0].detail or "")
    assert "n_agent.plugins" in (dup_warnings[0].detail or "")


# ===========================================================================
# S2: prepare()
# ===========================================================================


def test_prepare_directory_plugin_does_not_import(tmp_path):
    _write_dir_plugin(
        tmp_path, "boom",
        init_body="raise RuntimeError('prepare must not import')\n",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    result = loader.discover()
    winner = result.winners["boom"]
    # prepare must succeed even though __init__.py would raise on import
    prepared = loader.prepare(winner)
    assert prepared.token is not None
    assert prepared.token.kind == "directory"
    assert prepared.source is PluginSource.USER


def test_prepare_entry_point_calls_ep_load_exactly_once(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_prep")
    _install_fake_module("_fake_ep_prep", register_fn=lambda ctx: None)
    loader = _make_loader_with_eps([ep])
    result = loader.discover()
    assert ep.load_count == 0
    winner = result.winners["epplug"]
    prepared = loader.prepare(winner)
    assert ep.load_count == 1
    assert prepared.token is not None
    assert prepared.token.kind == "entrypoint"


def test_prepare_entry_point_load_failure_raises_typed_error(tmp_path):
    ep = FakeEntryPoint(
        name="badep", group="n_agent.plugins", module_name="_fake_ep_bad",
        load_raises=RuntimeError("kaboom"),
    )
    _install_fake_module("_fake_ep_bad", register_fn=lambda ctx: None)
    loader = _make_loader_with_eps([ep])
    result = loader.discover()
    winner = result.winners["badep"]
    with pytest.raises(EntrypointLoadFailed) as exc_info:
        loader.prepare(winner)
    assert exc_info.value.code == "entrypoint_load_failed"
    # single load attempt
    assert ep.load_count == 1


def test_prepare_plugin_manifest_supplements_description_author_kind(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_supp")
    _install_fake_module(
        "_fake_ep_supp",
        register_fn=lambda ctx: None,
        plugin_manifest={
            "description": "from-plugin-manifest",
            "author": "someone",
            "kind": "backend",
            "provides_tools": ["epplug"],
            "requires_plugins": ["dep1"],
            "pip_dependencies": ["requests"],
        },
    )
    loader = _make_loader_with_eps([ep])
    winner = loader.discover().winners["epplug"]
    prepared = loader.prepare(winner)
    m = prepared.manifest
    assert m.description == "from-plugin-manifest"
    assert m.author == "someone"
    assert m.kind is PluginKind.BACKEND
    assert m.provides_tools == ["epplug"]
    assert m.requires_plugins == ["dep1"]
    assert m.pip_dependencies == ["requests"]


def test_prepare_plugin_manifest_key_source_path_do_not_drift(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_drift")
    _install_fake_module(
        "_fake_ep_drift",
        register_fn=lambda ctx: None,
        plugin_manifest={
            "key": "drift-key",
            "source": "bundled",
            "path": "/somewhere/else",
            "name": "epplug",
            "version": "0",
        },
    )
    loader = _make_loader_with_eps([ep])
    winner = loader.discover().winners["epplug"]
    prepared = loader.prepare(winner)
    m = prepared.manifest
    assert m.key == "epplug"
    assert m.source is PluginSource.ENTRY_POINT
    assert m.path == "entrypoint:n_agent.plugins:epplug"
    # drift attempts are warned, not applied
    assert any("key drift" in w for w in prepared.warnings)
    assert any("source drift" in w for w in prepared.warnings)
    assert any("path drift" in w for w in prepared.warnings)


def test_prepare_plugin_manifest_name_version_mismatch_warns_discovery_wins(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_nv", dist_version="9.9.9")
    _install_fake_module(
        "_fake_ep_nv",
        register_fn=lambda ctx: None,
        plugin_manifest={"name": "different-name", "version": "0.0.1"},
    )
    loader = _make_loader_with_eps([ep])
    winner = loader.discover().winners["epplug"]
    # discovery version comes from distribution metadata
    assert winner.manifest.version == "9.9.9"
    prepared = loader.prepare(winner)
    # discovery identity wins
    assert prepared.manifest.name == "epplug"
    assert prepared.manifest.version == "9.9.9"
    assert any("name mismatch" in w for w in prepared.warnings)
    assert any("version mismatch" in w for w in prepared.warnings)


def test_prepare_plugin_manifest_non_mapping_raises(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_nm")
    _install_fake_module(
        "_fake_ep_nm",
        register_fn=lambda ctx: None,
        plugin_manifest=["not", "a", "mapping"],
    )
    loader = _make_loader_with_eps([ep])
    winner = loader.discover().winners["epplug"]
    with pytest.raises(EntrypointLoadFailed) as exc_info:
        loader.prepare(winner)
    assert exc_info.value.code == "entrypoint_invalid_manifest"


def test_prepare_failed_discovery_candidate_raises(tmp_path):
    plugin_dir = tmp_path / "broken"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text("name: [bad\n", encoding="utf-8")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    winner = loader.discover().winners["broken"]
    assert winner.manifest is None
    with pytest.raises(PluginLoaderError):
        loader.prepare(winner)


# ===========================================================================
# S2: load_and_register()
# ===========================================================================


def test_load_and_register_directory_creates_independent_context(tmp_path):
    _write_dir_plugin(tmp_path, "plug_a")
    _write_dir_plugin(tmp_path, "plug_b")
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    discovery = loader.discover()
    pa = loader.prepare(discovery.winners["plug_a"])
    pb = loader.prepare(discovery.winners["plug_b"])
    ctx_a = loader.load_and_register(pa, {}, {})
    ctx_b = loader.load_and_register(pb, {}, {})
    assert ctx_a is not ctx_b
    assert ctx_a.plugin_key == "plug_a"
    assert ctx_b.plugin_key == "plug_b"
    assert {r.name for r in ctx_a.tool_registrations} == {"plug_a"}
    assert {r.name for r in ctx_b.tool_registrations} == {"plug_b"}


def test_load_and_register_entry_point_does_not_double_load(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_lr")
    _install_fake_module("_fake_ep_lr", register_fn=lambda ctx: ctx.register_tool(
        name="ep_tool", toolset="ep", schema={"name": "ep_tool", "parameters": {"type": "object"}},
        handler=lambda a, **k: {"ok": True},
    ))
    loader = _make_loader_with_eps([ep])
    discovery = loader.discover()
    assert ep.load_count == 0
    prepared = loader.prepare(discovery.winners["epplug"])
    assert ep.load_count == 1
    ctx = loader.load_and_register(prepared, {}, {})
    # no second ep.load() during register
    assert ep.load_count == 1
    assert {r.name for r in ctx.tool_registrations} == {"ep_tool"}


def test_load_and_register_entry_point_no_directory_fallback(tmp_path):
    # entry-point path is "entrypoint:..." which does NOT exist as a directory;
    # load_and_register must use the cached module, not try __init__.py.
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_nofb")
    _install_fake_module("_fake_ep_nofb", register_fn=lambda ctx: ctx.register_tool(
        name="ep_tool", toolset="ep", schema={"name": "ep_tool", "parameters": {"type": "object"}},
        handler=lambda a, **k: {"ok": True},
    ))
    loader = _make_loader_with_eps([ep])
    prepared = loader.prepare(loader.discover().winners["epplug"])
    ctx = loader.load_and_register(prepared, {}, {})
    assert ctx.tool_registrations
    # no DirectoryLoadFailed raised; path is not a real directory


def test_load_and_register_directory_missing_init_raises_directory_load_failed(tmp_path):
    plugin_dir = tmp_path / "noinit"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: noinit\nversion: 1.0.0\ndescription: n\nkind: standalone\n", encoding="utf-8",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    prepared = loader.prepare(loader.discover().winners["noinit"])
    with pytest.raises(DirectoryLoadFailed) as exc_info:
        loader.load_and_register(prepared, {}, {})
    assert exc_info.value.code == "directory_load_failed"


def test_load_and_register_directory_import_error_raises_directory_load_failed(tmp_path):
    _write_dir_plugin(
        tmp_path, "boom",
        init_body="raise RuntimeError('boom on import')\n",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    prepared = loader.prepare(loader.discover().winners["boom"])
    with pytest.raises(DirectoryLoadFailed) as exc_info:
        loader.load_and_register(prepared, {}, {})
    assert exc_info.value.code == "directory_load_failed"
    assert "RuntimeError" in str(exc_info.value)


def test_load_and_register_register_exception_raises_register_failed(tmp_path):
    _write_dir_plugin(
        tmp_path, "badreg",
        init_body="def register(ctx):\n    raise ValueError('register boom')\n",
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))
    prepared = loader.prepare(loader.discover().winners["badreg"])
    with pytest.raises(PluginRegisterFailed) as exc_info:
        loader.load_and_register(prepared, {}, {})
    assert exc_info.value.code == "register_failed"
    assert "ValueError" in str(exc_info.value)


def test_load_and_register_entrypoint_missing_register_raises_register_failed(tmp_path):
    ep = FakeEntryPoint(name="noreg", group="n_agent.plugins", module_name="_fake_ep_noreg")
    _install_fake_module("_fake_ep_noreg")  # no register attr
    loader = _make_loader_with_eps([ep])
    prepared = loader.prepare(loader.discover().winners["noreg"])
    with pytest.raises(PluginRegisterFailed) as exc_info:
        loader.load_and_register(prepared, {}, {})
    assert exc_info.value.code == "register_failed"


def test_scan_public_summary_has_no_traceback(tmp_path):
    _write_dir_plugin(
        tmp_path, "badreg",
        init_body=(
            "import traceback\n"
            "def register(ctx):\n"
            "    raise RuntimeError('register boom with ' + 'X' * 500)\n"
        ),
    )
    loader = PluginFileLoader(PluginFileLoaderConfig(user_root=tmp_path))

    import asyncio
    result = asyncio.run(loader.scan(
        enabled_keys={"badreg"}, disabled_keys=set(),
        config_provider=lambda k: {}, secret_provider=lambda k: {},
    ))
    assert "badreg" in result.errors
    msg = result.errors["badreg"]
    assert "register_failed" in msg
    assert "Traceback" not in msg
    assert "File " not in msg


def test_scan_entry_point_not_enabled_does_not_load(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_disabled")
    _install_fake_module("_fake_ep_disabled", register_fn=lambda ctx: None)
    loader = _make_loader_with_eps([ep])
    import asyncio
    # epplug NOT in enabled_keys -> prepare/ep.load must not run
    result = asyncio.run(loader.scan(
        enabled_keys=set(), disabled_keys=set(),
        config_provider=lambda k: {}, secret_provider=lambda k: {},
    ))
    assert ep.load_count == 0
    keys = {m.key for m in result.manifests}
    assert "epplug" in keys  # manifest still discovered (metadata only)


def test_scan_entry_point_enabled_loads_and_registers(tmp_path):
    ep = FakeEntryPoint(name="epplug", group="n_agent.plugins", module_name="_fake_ep_enabled")
    _install_fake_module("_fake_ep_enabled", register_fn=lambda ctx: ctx.register_tool(
        name="ep_tool", toolset="ep", schema={"name": "ep_tool", "parameters": {"type": "object"}},
        handler=lambda a, **k: {"ok": True},
    ))
    loader = _make_loader_with_eps([ep])
    import asyncio
    result = asyncio.run(loader.scan(
        enabled_keys={"epplug"}, disabled_keys=set(),
        config_provider=lambda k: {}, secret_provider=lambda k: {},
    ))
    assert ep.load_count == 1
    assert "epplug" in result.registrations
    assert result.registrations["epplug"][0].name == "ep_tool"


def test_scan_propagates_prepare_warnings_to_scan_result(tmp_path):
    # PLUGIN_MANIFEST name/version mismatch must surface in PluginScanResult.warnings,
    # not just on PreparedPlugin.warnings (which scan() is the only consumer of today).
    ep = FakeEntryPoint(
        name="epplug", group="n_agent.plugins", module_name="_fake_ep_pwarn",
        dist_version="2.0.0",
    )
    _install_fake_module(
        "_fake_ep_pwarn",
        register_fn=lambda ctx: ctx.register_tool(
            name="ep_tool", toolset="ep",
            schema={"name": "ep_tool", "parameters": {"type": "object"}},
            handler=lambda a, **k: {"ok": True},
        ),
        plugin_manifest={"name": "different-name", "version": "0.0.1"},
    )
    loader = _make_loader_with_eps([ep])
    import asyncio
    result = asyncio.run(loader.scan(
        enabled_keys={"epplug"}, disabled_keys=set(),
        config_provider=lambda k: {}, secret_provider=lambda k: {},
    ))
    prep_warnings = [w for w in result.warnings if w.reason == "prepare_warning"]
    assert len(prep_warnings) >= 1
    assert any("name mismatch" in (w.detail or "") for w in prep_warnings)
    assert any("version mismatch" in (w.detail or "") for w in prep_warnings)
    # relative_path is the winner's path
    assert all(w.relative_path == "entrypoint:n_agent.plugins:epplug" for w in prep_warnings)
    # the plugin still loads successfully (warnings are non-fatal)
    assert "epplug" in result.registrations


def test_scan_collects_prepare_warnings_even_when_load_fails(tmp_path):
    # prepare succeeds (with a mismatch warning) but register raises; the
    # prepare_warning must still appear in PluginScanResult.warnings.
    ep = FakeEntryPoint(
        name="epplug", group="n_agent.plugins", module_name="_fake_ep_pwarn_fail",
    )

    def _raising_register(ctx):
        raise RuntimeError("register boom")

    _install_fake_module(
        "_fake_ep_pwarn_fail",
        register_fn=_raising_register,
        plugin_manifest={"name": "different-name"},
    )
    loader = _make_loader_with_eps([ep])
    import asyncio
    result = asyncio.run(loader.scan(
        enabled_keys={"epplug"}, disabled_keys=set(),
        config_provider=lambda k: {}, secret_provider=lambda k: {},
    ))
    # load failed
    assert "epplug" in result.errors
    assert "register_failed" in result.errors["epplug"]
    # but prepare diagnostic still surfaced (not lost on load failure)
    prep_warnings = [w for w in result.warnings if w.reason == "prepare_warning"]
    assert any("name mismatch" in (w.detail or "") for w in prep_warnings)
