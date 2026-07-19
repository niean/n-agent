from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin_dependency import PIP_MISSING, PIP_OK
from app.application.plugin_service import (
    PluginContext,
    PluginScanResult,
    PluginService,
    PluginToolRegistration,
)
from app.domain.plugin import Plugin, PluginKind, PluginManifest, PluginSource
from app.domain.tool import ToolDefinition, ToolResultStatus, ToolSourceType
from app.infrastructure.plugin.file_loader import (
    DiscoveryCandidate,
    LoaderToken,
    PluginDiscoveryResult,
    PreparedPlugin,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_settings(
    plugin_tool_timeout_seconds: int = 10,
    plugin_hook_timeout_seconds: float = 5.0,
    **kwargs,
):
    s = MagicMock()
    s.plugin_tool_timeout_seconds = plugin_tool_timeout_seconds
    s.plugin_hook_timeout_seconds = plugin_hook_timeout_seconds
    s.plugins_enabled = kwargs.get("plugins_enabled", [])
    s.plugins_disabled = kwargs.get("plugins_disabled", [])
    s.plugins_override_allowlist = kwargs.get("plugins_override_allowlist", [])
    s.enable_plugin_entrypoints = kwargs.get("enable_plugin_entrypoints", False)
    return s


def _manifest(
    key: str,
    *,
    kind: PluginKind = PluginKind.STANDALONE,
    requires_plugins: list[str] | None = None,
    pip_dependencies: list[str] | None = None,
    source: PluginSource = PluginSource.BUNDLED,
    provides_tools: list[str] | None = None,
) -> PluginManifest:
    return PluginManifest(
        key=key,
        name=key,
        version="1.0.0",
        description="",
        source=source,
        path=f"/p/{key}",
        kind=kind,
        requires_plugins=list(requires_plugins or []),
        pip_dependencies=list(pip_dependencies or []),
        provides_tools=list(provides_tools or [key]),
    )


def _candidate(
    key: str,
    *,
    manifest: PluginManifest | None = None,
    source: PluginSource = PluginSource.BUNDLED,
    discovery_index: int = 0,
    status: str = "ok",
    diagnostic: str | None = None,
) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        key=key,
        source=source,
        path=f"/p/{key}",
        discovery_index=discovery_index,
        status=status,
        diagnostic=diagnostic,
        manifest=manifest if manifest is not None else _manifest(key, source=source),
    )


def _build_service(
    *,
    registry_plugins: list[Plugin],
    candidates: list[DiscoveryCandidate] | None = None,
    prepare_raises: dict[str, Exception] | None = None,
    load_raises: dict[str, Exception] | None = None,
    tool_registrations: dict[str, list[PluginToolRegistration]] | None = None,
    tool_service_defs: list[ToolDefinition] | None = None,
    settings=None,
    captured_routes: list[set[str]] | None = None,
):
    registry = AsyncMock()
    registry.list_plugins.return_value = list(registry_plugins)
    registry.get_plugin.side_effect = lambda key: next(
        (p for p in registry_plugins if p.key == key), None
    )
    registry.get_secret_config.return_value = {}
    registry.set_enabled.return_value = MagicMock()
    registry.replace_all_plugins.side_effect = lambda plugins: list(plugins)

    # Build discovery result from candidates
    cand_list = list(candidates or [])
    winners = {c.key: c for c in cand_list}
    discovery_result = PluginDiscoveryResult(
        candidates=cand_list,
        winners=winners,
        warnings=[],
    )

    loader = MagicMock()  # sync methods
    loader.discover.return_value = discovery_result

    _prepare_raises = prepare_raises or {}
    _load_raises = load_raises or {}
    _tool_regs = tool_registrations or {}

    def _prepare(candidate):
        if candidate.key in _prepare_raises:
            raise _prepare_raises[candidate.key]
        if candidate.manifest is None:
            raise ValueError("no manifest")
        return PreparedPlugin(
            manifest=candidate.manifest,
            source=candidate.source,
            token=LoaderToken("directory", {"path": candidate.path}),
            warnings=[],
        )

    loader.prepare.side_effect = _prepare

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        if key in _load_raises:
            raise _load_raises[key]
        ctx = PluginContext(
            plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {},
        )
        for reg in _tool_regs.get(key, []):
            ctx.tool_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = tool_service_defs or []
    tool_service.definitions = {d.name: d for d in (tool_service_defs or [])}
    captured_defs: list = []
    tool_service.set_dynamic_definitions = lambda key, defs: captured_defs.extend(defs)
    tool_service.replace_dynamic_definitions = lambda key, defs, override_static_names=None: captured_defs.extend(defs)

    if captured_routes is None:
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
    return service, registry, loader, tool_service, captured_defs, captured_routes


# ---------------------------------------------------------------------------
# Existing tests (kept; updated for new scan flow)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_only_executes_enabled_standalone_replace_all():
    plugins = [
        Plugin(id="p1", key="hello", name="hello", source=PluginSource.BUNDLED, enabled=True, kind=PluginKind.STANDALONE),
        Plugin(id="p2", key="backend1", name="backend1", source=PluginSource.USER, enabled=True, kind=PluginKind.BACKEND),
        Plugin(id="p3", key="disabled1", name="disabled1", source=PluginSource.USER, enabled=False, kind=PluginKind.STANDALONE),
    ]
    cands = [
        _candidate("hello", discovery_index=0),
        _candidate("backend1", discovery_index=1, manifest=_manifest("backend1", kind=PluginKind.BACKEND, source=PluginSource.USER)),
        _candidate("disabled1", discovery_index=2, source=PluginSource.USER),
    ]
    service, registry, *_ = _build_service(registry_plugins=plugins, candidates=cands)
    await service.scan()
    registry.replace_all_plugins.assert_awaited_once()
    registry.list_plugins.assert_awaited()


@pytest.mark.asyncio
async def test_set_enabled_triggers_scan_and_refresh():
    plugin = Plugin(id="p1", key="hello", name="hello", source=PluginSource.BUNDLED, enabled=False)
    registry = AsyncMock()
    registry.get_plugin.return_value = plugin
    registry.set_enabled.return_value = plugin
    registry.list_plugins.return_value = [plugin]
    registry.get_secret_config.return_value = {}
    registry.replace_all_plugins.side_effect = lambda plugins: list(plugins)

    loader = MagicMock()
    loader.discover.return_value = PluginDiscoveryResult(
        candidates=[_candidate("hello")],
        winners={"hello": _candidate("hello")},
        warnings=[],
    )
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )
    loader.load_and_register.side_effect = lambda p, cfg, sec: PluginContext(plugin_key=p.manifest.key)

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    captured: list = []
    service = PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=lambda names: captured.append(set(names)),
        settings=_make_settings(),
    )
    await service.set_enabled("hello", True)
    registry.set_enabled.assert_awaited_once_with("hello", True)
    assert captured  # refresh called


def test_refresh_tool_surface_drops_conflicting_non_override_tools():
    static = ToolDefinition(
        name="hello",
        description="static",
        input_schema={"type": "object", "properties": {}},
        source_type=ToolSourceType.BUILTIN,
        toolset="builtin",
    )
    service, *_rest = _build_service(
        registry_plugins=[],
        tool_service_defs=[static],
    )
    service._plugin_registrations = {
        "p1": [
            PluginToolRegistration(
                plugin_key="p1",
                name="hello",
                schema={"name": "hello", "parameters": {"type": "object"}},
                handler=lambda a, **k: "",
                override=False,
            ),
            PluginToolRegistration(
                plugin_key="p1",
                name="world",
                schema={"name": "world", "parameters": {"type": "object"}},
                handler=lambda a, **k: "",
                override=False,
            ),
        ],
    }
    service._refresh_tool_surface()
    assert service._registrations["hello"].available is False
    assert service._registrations["hello"].unavailable_reason is not None
    assert service._registrations["world"].available is True


def test_refresh_tool_surface_override_static_still_unavailable_this_phase():
    static = ToolDefinition(
        name="hello",
        description="static",
        input_schema={"type": "object", "properties": {}},
        source_type=ToolSourceType.BUILTIN,
        toolset="builtin",
    )
    service, *_ = _build_service(registry_plugins=[], tool_service_defs=[static])
    service._plugin_registrations = {
        "p1": [
            PluginToolRegistration(
                plugin_key="p1",
                name="hello",
                schema={"name": "hello", "parameters": {"type": "object"}},
                handler=lambda a, **k: "",
                override=True,
            ),
        ],
    }
    service._refresh_tool_surface()
    assert service._registrations["hello"].available is False
    assert "override" in (service._registrations["hello"].unavailable_reason or "").lower() \
        or "static" in (service._registrations["hello"].unavailable_reason or "").lower()


def test_refresh_tool_surface_requires_env_missing_makes_unavailable():
    service, *_ = _build_service(registry_plugins=[], tool_service_defs=[])
    reg = PluginToolRegistration(
        plugin_key="p1",
        name="hello",
        schema={"name": "hello", "parameters": {"type": "object"}},
        handler=lambda a, **k: "",
        requires_env=[{"name": "MISSING_API_KEY"}],
        plugin_config={},
        secret_config={},
    )
    service._plugin_registrations = {"p1": [reg]}
    service._refresh_tool_surface()
    assert reg.available is False
    assert "env" in (reg.unavailable_reason or "").lower()


@pytest.mark.asyncio
async def test_call_tool_returns_error_when_not_found():
    service, *_ = _build_service(registry_plugins=[])
    result = await service.call_tool("missing", {}, None)
    assert result.status is ToolResultStatus.ERROR
    assert "not found" in str(result.content).lower()


@pytest.mark.asyncio
async def test_call_tool_invokes_handler_and_wraps_dict():
    reg = PluginToolRegistration(
        plugin_key="hello",
        name="hello",
        schema={"name": "hello", "parameters": {"type": "object"}},
        handler=lambda args, **kwargs: {"message": f"Hello, {args.get('name', 'plugin')}!"},
        is_async=False,
    )
    service, *_ = _build_service(registry_plugins=[])
    service._registrations = {"hello": reg}
    result = await service.call_tool("hello", {"name": "Alice"}, None, tool_call_id="tc-42")
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == {"message": "Hello, Alice!"}
    assert result.tool_call_id == "tc-42"


@pytest.mark.asyncio
async def test_call_tool_async_handler_awaited():
    async def async_handler(args, **kwargs):
        return {"ok": True}

    reg = PluginToolRegistration(
        plugin_key="hello",
        name="hello_async",
        schema={"name": "hello_async", "parameters": {"type": "object"}},
        handler=async_handler,
        is_async=True,
    )
    service, *_ = _build_service(registry_plugins=[])
    service._registrations = {"hello_async": reg}
    result = await service.call_tool("hello_async", {}, None)
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content == {"ok": True}


@pytest.mark.asyncio
async def test_call_tool_handler_exception_returns_error():
    def bad_handler(args, **kwargs):
        raise RuntimeError("boom")

    reg = PluginToolRegistration(
        plugin_key="hello",
        name="hello",
        schema={"name": "hello", "parameters": {"type": "object"}},
        handler=bad_handler,
    )
    service, *_ = _build_service(registry_plugins=[])
    service._registrations = {"hello": reg}
    result = await service.call_tool("hello", {}, None)
    assert result.status is ToolResultStatus.ERROR
    assert "boom" in str(result.content)


@pytest.mark.asyncio
async def test_plugin_tool_executor_delegates_to_service():
    from unittest.mock import ANY
    from app.application.plugin_service import PluginToolExecutor
    from app.domain.tool import ToolCallRequest, ToolExecutionContext

    service = AsyncMock()
    service.call_tool.return_value = __import__(
        "app.domain.tool", fromlist=["ToolResult"]
    ).ToolResult("tc1", "hello", ToolResultStatus.SUCCESS, {"message": "hi"})
    executor = PluginToolExecutor(service=service)
    result = await executor.execute(
        ToolCallRequest(id="tc1", name="hello", arguments={"name": "world"}),
        ToolExecutionContext(session_id="s1", metadata={}),
    )
    service.call_tool.assert_awaited_once_with("hello", {"name": "world"}, ANY, "tc1")
    assert result.status is ToolResultStatus.SUCCESS


# ===========================================================================
# S1: effective enabled set
# ===========================================================================


class TestEffectiveEnabledSet:
    @pytest.mark.asyncio
    async def test_registry_enabled_union_settings_enabled_minus_settings_disabled(self):
        """Effective enabled = (registry enabled ∪ settings.enabled) - settings.disabled."""
        plugins = [
            Plugin(id="p1", key="reg_enabled", name="r", source=PluginSource.BUNDLED, enabled=True),
            Plugin(id="p2", key="reg_disabled", name="d", source=PluginSource.BUNDLED, enabled=False),
        ]
        cands = [
            _candidate("reg_enabled", discovery_index=0),
            _candidate("reg_disabled", discovery_index=1),
            _candidate("settings_only", discovery_index=2),
            _candidate("forced_disabled", discovery_index=3),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=plugins,
            candidates=cands,
            settings=_make_settings(
                plugins_enabled=["settings_only", "forced_disabled"],
                plugins_disabled=["forced_disabled"],
            ),
        )
        await service.scan()
        # Check which keys were prepared (only effective_enabled get prepared)
        prepared_keys = {call.args[0].key for call in loader.prepare.call_args_list}
        # reg_enabled (registry), settings_only (settings) -> prepared
        # reg_disabled (not enabled anywhere) -> not prepared
        # forced_disabled (settings disabled overrides) -> not prepared
        assert "reg_enabled" in prepared_keys
        assert "settings_only" in prepared_keys
        assert "reg_disabled" not in prepared_keys
        assert "forced_disabled" not in prepared_keys

    @pytest.mark.asyncio
    async def test_settings_disabled_highest_priority_over_registry_enabled(self):
        plugins = [
            Plugin(id="p1", key="plug", name="plug", source=PluginSource.BUNDLED, enabled=True),
        ]
        cands = [_candidate("plug")]
        service, registry, loader, *_ = _build_service(
            registry_plugins=plugins,
            candidates=cands,
            settings=_make_settings(plugins_disabled=["plug"]),
        )
        await service.scan()
        # settings disabled -> not prepared despite registry enabled
        prepared_keys = {call.args[0].key for call in loader.prepare.call_args_list}
        assert "plug" not in prepared_keys
        # registry set_enabled called to disable
        registry.set_enabled.assert_any_await("plug", False)

    @pytest.mark.asyncio
    async def test_new_candidate_not_in_registry_or_settings_defaults_disabled(self):
        cands = [_candidate("newplug", discovery_index=0)]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(),
        )
        await service.scan()
        prepared_keys = {call.args[0].key for call in loader.prepare.call_args_list}
        assert "newplug" not in prepared_keys
        # The plugin is persisted but disabled
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        newplug = next(p for p in plugins_list if p.key == "newplug")
        assert newplug.enabled is False

    @pytest.mark.asyncio
    async def test_first_discovered_entry_point_in_settings_enabled_prepared_same_scan(self):
        cands = [
            _candidate("epplug", discovery_index=0, source=PluginSource.ENTRY_POINT),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],  # not in registry yet
            candidates=cands,
            settings=_make_settings(plugins_enabled=["epplug"]),
        )
        await service.scan()
        prepared_keys = {call.args[0].key for call in loader.prepare.call_args_list}
        assert "epplug" in prepared_keys
        # load_and_register called (entry point loaded in same scan)
        loaded_keys = {call.args[0].manifest.key for call in loader.load_and_register.call_args_list}
        assert "epplug" in loaded_keys

    @pytest.mark.asyncio
    async def test_dashboard_toggle_preserved_when_not_settings_forced(self):
        """When settings doesn't force enabled/disabled, registry state is preserved."""
        plugins = [
            Plugin(id="p1", key="plug", name="plug", source=PluginSource.BUNDLED, enabled=True),
        ]
        cands = [_candidate("plug")]
        service, registry, loader, *_ = _build_service(
            registry_plugins=plugins,
            candidates=cands,
            settings=_make_settings(),  # no settings forcing
        )
        await service.scan()
        # set_enabled should NOT be called (no settings forcing)
        registry.set_enabled.assert_not_awaited()
        # The plugin stays enabled (preserved by replace_all_plugins)
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        assert plug.enabled is True

    @pytest.mark.asyncio
    async def test_settings_disabled_plugin_persists_enabled_false(self):
        """A registry-enabled plugin that settings disables must persist enabled=False."""
        plugins = [
            Plugin(id="p1", key="plug", name="plug", source=PluginSource.BUNDLED, enabled=True),
        ]
        cands = [_candidate("plug")]
        service, registry, loader, *_ = _build_service(
            registry_plugins=plugins,
            candidates=cands,
            settings=_make_settings(plugins_disabled=["plug"]),
        )
        await service.scan()
        # set_enabled called to disable
        registry.set_enabled.assert_any_await("plug", False)
        # The Plugin object passed to replace_all_plugins has enabled=False
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        assert plug.enabled is False

    @pytest.mark.asyncio
    async def test_settings_enabled_plugin_persists_enabled_true(self):
        """A registry-disabled plugin that settings enables must persist enabled=True."""
        plugins = [
            Plugin(id="p1", key="plug", name="plug", source=PluginSource.USER, enabled=False),
        ]
        cands = [_candidate("plug", source=PluginSource.USER)]
        service, registry, loader, *_ = _build_service(
            registry_plugins=plugins,
            candidates=cands,
            settings=_make_settings(plugins_enabled=["plug"]),
        )
        await service.scan()
        # set_enabled called to enable (any source, not just BUNDLED)
        registry.set_enabled.assert_any_await("plug", True)
        # The Plugin object passed to replace_all_plugins has enabled=True
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        assert plug.enabled is True


# ===========================================================================
# S2: dependency admission in scan (integration)
# ===========================================================================


class TestScanDependencyAdmission:
    @pytest.mark.asyncio
    async def test_missing_required_plugin_makes_dependent_partial(self):
        """Plugin requiring a missing plugin gets PARTIAL, does not register."""
        cands = [
            _candidate("base", discovery_index=0),
            _candidate("dependent", discovery_index=1, manifest=_manifest("dependent", requires_plugins=["base"])),
        ]
        # Remove 'base' from discovery -> dependent's dep is missing
        cands = [
            _candidate("dependent", discovery_index=1, manifest=_manifest("dependent", requires_plugins=["base"])),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["dependent"]),
        )
        await service.scan()
        # dependent should NOT be loaded (dep missing)
        loaded_keys = {call.args[0].manifest.key for call in loader.load_and_register.call_args_list}
        assert "dependent" not in loaded_keys
        # dependent should be PARTIAL with "missing required plugin: base"
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        dep = next(p for p in plugins_list if p.key == "dependent")
        assert dep.last_scan_status == "partial"
        assert "missing required plugin: base" in (dep.last_scan_error or "")

    @pytest.mark.asyncio
    async def test_cycle_members_failed_not_registered(self):
        """Plugins in a dependency cycle are FAILED and not registered."""
        cands = [
            _candidate("a", discovery_index=0, manifest=_manifest("a", requires_plugins=["b"])),
            _candidate("b", discovery_index=1, manifest=_manifest("b", requires_plugins=["a"])),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["a", "b"]),
        )
        await service.scan()
        loaded_keys = {call.args[0].manifest.key for call in loader.load_and_register.call_args_list}
        assert "a" not in loaded_keys
        assert "b" not in loaded_keys
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        for p in plugins_list:
            assert p.last_scan_status == "failed"
            assert "circular plugin dependency" in (p.last_scan_error or "")

    @pytest.mark.asyncio
    async def test_independent_branch_continues_when_other_fails(self):
        """A failed plugin's independent branch continues to load."""
        cands = [
            _candidate("broken", discovery_index=0),
            _candidate("independent", discovery_index=1),
        ]
        from app.infrastructure.plugin.file_loader import PluginRegisterFailed
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["broken", "independent"]),
            load_raises={"broken": PluginRegisterFailed("register_failed", "boom")},
        )
        await service.scan()
        loaded_keys = {call.args[0].manifest.key for call in loader.load_and_register.call_args_list}
        assert "broken" in loaded_keys  # load was attempted
        assert "independent" in loaded_keys  # independent still loads
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        broken = next(p for p in plugins_list if p.key == "broken")
        assert broken.last_scan_status == "failed"
        indep = next(p for p in plugins_list if p.key == "independent")
        assert indep.last_scan_status == "ok"

    @pytest.mark.asyncio
    async def test_pip_missing_makes_plugin_partial_not_registered(self):
        """A plugin with a missing pip dependency is PARTIAL and not registered."""
        cands = [
            _candidate("plug", discovery_index=0, manifest=_manifest("plug", pip_dependencies=["no-such-pkg-xyz-999>=1.0"])),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["plug"]),
        )
        await service.scan()
        loaded_keys = {call.args[0].manifest.key for call in loader.load_and_register.call_args_list}
        assert "plug" not in loaded_keys
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        assert plug.last_scan_status == "partial"
        assert "missing pip dependency" in (plug.last_scan_error or "")

    @pytest.mark.asyncio
    async def test_dependency_loaded_before_dependent(self):
        """Base plugin is loaded before dependent (topological order)."""
        cands = [
            _candidate("dependent", discovery_index=0, manifest=_manifest("dependent", requires_plugins=["base"])),
            _candidate("base", discovery_index=1),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["base", "dependent"]),
        )
        await service.scan()
        loaded_order = [call.args[0].manifest.key for call in loader.load_and_register.call_args_list]
        assert loaded_order.index("base") < loaded_order.index("dependent")

    @pytest.mark.asyncio
    async def test_transitive_failed_dependency_is_partial(self):
        """A -> B -> C, C load fails: B is PARTIAL (missing required), A is PARTIAL (unavailable)."""
        from app.infrastructure.plugin.file_loader import PluginRegisterFailed
        cands = [
            _candidate("a", discovery_index=0, manifest=_manifest("a", requires_plugins=["b"])),
            _candidate("b", discovery_index=1, manifest=_manifest("b", requires_plugins=["c"])),
            _candidate("c", discovery_index=2),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["a", "b", "c"]),
            load_raises={"c": PluginRegisterFailed("register_failed", "boom")},
        )
        await service.scan()
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        c_plug = next(p for p in plugins_list if p.key == "c")
        b_plug = next(p for p in plugins_list if p.key == "b")
        a_plug = next(p for p in plugins_list if p.key == "a")
        assert c_plug.last_scan_status == "failed"
        assert b_plug.last_scan_status == "partial"
        assert "missing required plugin: c" in (b_plug.last_scan_error or "")
        assert a_plug.last_scan_status == "partial"
        assert "required plugin unavailable: b" in (a_plug.last_scan_error or "")

    @pytest.mark.asyncio
    async def test_node_depending_on_cycle_is_partial_not_failed(self):
        """A requires B, B<->C cycle. A is PARTIAL (not FAILED); B,C are FAILED."""
        cands = [
            _candidate("a", discovery_index=0, manifest=_manifest("a", requires_plugins=["b"])),
            _candidate("b", discovery_index=1, manifest=_manifest("b", requires_plugins=["c"])),
            _candidate("c", discovery_index=2, manifest=_manifest("c", requires_plugins=["b"])),
        ]
        service, registry, loader, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["a", "b", "c"]),
        )
        await service.scan()
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        a_plug = next(p for p in plugins_list if p.key == "a")
        b_plug = next(p for p in plugins_list if p.key == "b")
        c_plug = next(p for p in plugins_list if p.key == "c")
        # B and C are cycle members -> FAILED
        assert b_plug.last_scan_status == "failed"
        assert c_plug.last_scan_status == "failed"
        assert "circular plugin dependency" in (b_plug.last_scan_error or "")
        assert "circular plugin dependency" in (c_plug.last_scan_error or "")
        # B's error only lists B and C (not A) - check the member list part
        b_err = b_plug.last_scan_error or ""
        assert "b, c" in b_err or "c, b" in b_err
        assert b_err.count("a") == b_err.count("circular")  # 'a' only from 'circular'
        # A depends on cycle member B -> PARTIAL (not FAILED)
        assert a_plug.last_scan_status == "partial"
        assert "required plugin unavailable: b" in (a_plug.last_scan_error or "")
        assert "circular" not in (a_plug.last_scan_error or "")
        # A was not loaded
        loaded_keys = {call.args[0].manifest.key for call in loader.load_and_register.call_args_list}
        assert "a" not in loaded_keys
        assert "b" not in loaded_keys
        assert "c" not in loaded_keys

    @pytest.mark.asyncio
    async def test_multi_cycle_error_message_only_lists_own_cycle(self):
        """Two disjoint cycles: each member's error only lists its own cycle members."""
        cands = [
            _candidate("a", discovery_index=0, manifest=_manifest("a", requires_plugins=["b"])),
            _candidate("b", discovery_index=1, manifest=_manifest("b", requires_plugins=["a"])),
            _candidate("c", discovery_index=2, manifest=_manifest("c", requires_plugins=["d"])),
            _candidate("d", discovery_index=3, manifest=_manifest("d", requires_plugins=["c"])),
        ]
        service, registry, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["a", "b", "c", "d"]),
        )
        await service.scan()
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        a_plug = next(p for p in plugins_list if p.key == "a")
        c_plug = next(p for p in plugins_list if p.key == "c")
        # A's error lists only a, b (not c, d) - check member list after colon
        a_err = a_plug.last_scan_error or ""
        assert ": " in a_err, f"a_err missing ': ' separator: {a_err!r}"
        assert "a, b" in a_err or "b, a" in a_err
        a_members = a_err.split(": ", 1)[-1]
        assert "c" not in a_members, f"c should not be in a's cycle members: {a_members!r}"
        assert "d" not in a_members, f"d should not be in a's cycle members: {a_members!r}"
        # C's error lists only c, d (not a, b)
        c_err = c_plug.last_scan_error or ""
        assert ": " in c_err, f"c_err missing ': ' separator: {c_err!r}"
        assert "c, d" in c_err or "d, c" in c_err
        c_members = c_err.split(": ", 1)[-1]
        assert "a" not in c_members, f"a should not be in c's cycle members: {c_members!r}"
        assert "b" not in c_members, f"b should not be in c's cycle members: {c_members!r}"


# ===========================================================================
# S4: dependency_status in public views
# ===========================================================================


class TestDependencyStatusPublic:
    @pytest.mark.asyncio
    async def test_dependency_status_in_capabilities(self):
        cands = [_candidate("plug", discovery_index=0)]
        service, registry, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["plug"]),
        )
        await service.scan()
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        assert "dependency_status" in plug.capabilities
        dep_status = plug.capabilities["dependency_status"]
        assert "pip" in dep_status
        assert "requires_plugins" in dep_status
        assert "external" in dep_status
        assert "warnings" in dep_status

    @pytest.mark.asyncio
    async def test_list_view_no_new_large_fields(self):
        cands = [_candidate("plug", discovery_index=0)]
        service, registry, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["plug"]),
        )
        await service.scan()
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        view = plug.to_public_view()
        # list view should not have manifest field
        assert "manifest" not in view
        # but should have capabilities (which contains dependency_status)
        assert "capabilities" in view

    @pytest.mark.asyncio
    async def test_detail_view_keeps_manifest_and_dependency_status(self):
        cands = [_candidate("plug", discovery_index=0)]
        service, registry, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["plug"]),
        )
        await service.scan()
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        detail = plug.to_public_detail()
        assert "manifest" in detail
        assert "capabilities" in detail
        assert "dependency_status" in detail["capabilities"]

    @pytest.mark.asyncio
    async def test_last_scan_error_no_traceback(self):
        from app.infrastructure.plugin.file_loader import PluginRegisterFailed
        cands = [_candidate("plug", discovery_index=0)]
        service, registry, *_ = _build_service(
            registry_plugins=[],
            candidates=cands,
            settings=_make_settings(plugins_enabled=["plug"]),
            load_raises={"plug": PluginRegisterFailed("register_failed", "boom detail")},
        )
        await service.scan()
        replace_call = registry.replace_all_plugins.call_args
        plugins_list = replace_call.args[0]
        plug = next(p for p in plugins_list if p.key == "plug")
        assert "Traceback" not in (plug.last_scan_error or "")
        assert "File " not in (plug.last_scan_error or "")
        assert "register_failed" in (plug.last_scan_error or "")
