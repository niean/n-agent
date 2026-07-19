"""T9 S2/S3: Atomic publish with generation snapshot + compensating rollback.

Covers:
- S2 (failure matrix): single plugin register failure drops that Context's
  candidates (tools/hooks/CLI) but independent plugins continue; concurrent
  scan serialized by ``_scan_lock``; candidate generation simultaneously
  contains registrations/hooks/CLI/ToolService defs+suppression/routes/
  registry public state. Inject failures at ToolService.replace_dynamic_definitions,
  route_refresher, registry.replace_all_plugins; assert caller sees the last
  successful generation (tools/hooks/CLI/routes/registry unchanged).
- S3 (compensating rollback): snapshot previous generation's plugin defs/
  override_names/routes/hooks/CLI/registrations/registry public rows before
  commit; commit in spec order (ToolService -> routes -> in-memory snapshot
  -> registry); on ANY failure compensate in REVERSE order; cross SQLite/memory
  no transaction atomicity claim, but caller only sees last successful
  generation; rollback failure records scan failure and does NOT set candidate
  as live; ``generation_id`` increments ONLY after all publish steps succeed.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin_service import (
    HookRegistration,
    PluginCliCommand,
    PluginContext,
    PluginService,
    PluginToolRegistration,
)
from app.domain.plugin import Plugin, PluginKind, PluginManifest, PluginSource
from app.domain.tool import ToolDefinition, ToolSourceType
from app.infrastructure.plugin.file_loader import (
    DiscoveryCandidate,
    LoaderToken,
    PluginDiscoveryResult,
    PluginRegisterFailed,
    PreparedPlugin,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_settings(**kwargs):
    s = MagicMock()
    s.plugin_tool_timeout_seconds = kwargs.get("plugin_tool_timeout_seconds", 30)
    s.plugin_hook_timeout_seconds = kwargs.get("plugin_hook_timeout_seconds", 5.0)
    s.plugins_enabled = kwargs.get("plugins_enabled", [])
    s.plugins_disabled = kwargs.get("plugins_disabled", [])
    s.plugins_override_allowlist = kwargs.get("plugins_override_allowlist", [])
    s.enable_plugin_entrypoints = kwargs.get("enable_plugin_entrypoints", False)
    return s


def _manifest(key: str, *, source: PluginSource = PluginSource.BUNDLED) -> PluginManifest:
    return PluginManifest(
        key=key,
        name=key,
        version="1.0.0",
        description="",
        source=source,
        path=f"/p/{key}",
        kind=PluginKind.STANDALONE,
    )


def _candidate(
    key: str,
    *,
    manifest: PluginManifest | None = None,
    source: PluginSource = PluginSource.BUNDLED,
    discovery_index: int = 0,
) -> DiscoveryCandidate:
    return DiscoveryCandidate(
        key=key,
        source=source,
        path=f"/p/{key}",
        discovery_index=discovery_index,
        status="ok",
        diagnostic=None,
        manifest=manifest if manifest is not None else _manifest(key, source=source),
    )


def _plugin(key: str, *, enabled: bool = True) -> Plugin:
    return Plugin(
        id=f"id-{key}", key=key, name=key,
        source=PluginSource.BUNDLED, enabled=enabled,
        kind=PluginKind.STANDALONE,
    )


def _tool_reg(name: str, *, plugin_key: str = "p1", override: bool = False) -> PluginToolRegistration:
    return PluginToolRegistration(
        plugin_key=plugin_key,
        name=name,
        schema={"parameters": {"type": "object", "properties": {}}},
        handler=lambda args, **kw: {"ok": True},
        override=override,
        description=f"plugin tool {name}",
        plugin_config={},
        secret_config={},
    )


def _hook_reg(plugin_key: str, hook_name: str, index: int = 0) -> HookRegistration:
    return HookRegistration(
        plugin_key=plugin_key,
        hook_name=hook_name,
        callback=lambda **kw: None,
        registration_index=index,
    )


def _cli_reg(name: str, *, plugin_key: str = "p1", index: int = 0) -> PluginCliCommand:
    return PluginCliCommand(
        plugin_key=plugin_key,
        name=name,
        help=f"help {name}",
        description="",
        setup_fn=lambda parser: None,
        handler_fn=None,
        registration_index=index,
    )


def _build_service(
    *,
    registry_plugins: list[Plugin],
    candidates: list[DiscoveryCandidate] | None = None,
    tool_registrations: dict[str, list[PluginToolRegistration]] | None = None,
    hook_registrations: dict[str, list[HookRegistration]] | None = None,
    cli_registrations: dict[str, list[PluginCliCommand]] | None = None,
    load_raises: dict[str, Exception] | None = None,
    tool_service=None,
    route_refresher=None,
    settings=None,
    replace_all_plugins_side_effect=None,
):
    """Build a PluginService with the 3-phase loader API mocked."""
    registry = AsyncMock()
    registry.list_plugins.return_value = list(registry_plugins)
    registry.get_plugin.side_effect = lambda key: next(
        (p for p in registry_plugins if p.key == key), None
    )
    registry.get_secret_config.return_value = {}
    registry.set_enabled.return_value = MagicMock()
    if replace_all_plugins_side_effect is not None:
        registry.replace_all_plugins.side_effect = replace_all_plugins_side_effect
    else:
        registry.replace_all_plugins = AsyncMock()

    cand_list = list(candidates or [])
    winners = {c.key: c for c in cand_list}
    discovery_result = PluginDiscoveryResult(
        candidates=cand_list, winners=winners, warnings=[],
    )

    loader = MagicMock()
    loader.discover.return_value = discovery_result

    _load_raises = load_raises or {}
    _tools = tool_registrations or {}
    _hooks = hook_registrations or {}
    _cli = cli_registrations or {}

    def _prepare(candidate):
        return PreparedPlugin(
            manifest=candidate.manifest, source=candidate.source,
            token=LoaderToken("directory", {"path": candidate.path}), warnings=[],
        )

    loader.prepare.side_effect = _prepare

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        if key in _load_raises:
            raise _load_raises[key]
        ctx = PluginContext(
            plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {},
        )
        for reg in _tools.get(key, []):
            ctx.tool_registrations.append(reg)
        for reg in _hooks.get(key, []):
            ctx.hook_registrations.append(reg)
        for reg in _cli.get(key, []):
            ctx.cli_command_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register

    if tool_service is None:
        tool_service = MagicMock()
        tool_service.list_definitions.return_value = []
        tool_service.definitions = {}
        tool_service.replace_dynamic_definitions = MagicMock()

    captured_routes: list[set[str]] = []
    if route_refresher is None:
        def refresher(names):
            captured_routes.append(set(names))
        route_refresher = refresher
    else:
        # Wrap to capture routes too
        original = route_refresher
        def wrapped(names):
            captured_routes.append(set(names))
            original(names)
        route_refresher = wrapped

    service = PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=route_refresher,
        settings=settings or _make_settings(),
    )
    # Expose captured_routes on the service for test inspection
    service._test_captured_routes = captured_routes  # type: ignore[attr-defined]
    return service


# ---------------------------------------------------------------------------
# S2: Failure matrix - single plugin register failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_plugin_register_failure_drops_all_its_candidates():
    """Single plugin register failure drops that Context's tool/hook/CLI
    candidates, but independent plugins continue to publish theirs."""
    cands = [
        _candidate("broken", discovery_index=0),
        _candidate("independent", discovery_index=1),
    ]
    plugins = [_plugin("broken"), _plugin("independent")]
    tools = {
        "broken": [_tool_reg("broken-tool", plugin_key="broken")],
        "independent": [_tool_reg("ind-tool", plugin_key="independent")],
    }
    hooks = {
        "broken": [_hook_reg("broken", "on_session_start", index=0)],
        "independent": [_hook_reg("independent", "on_session_start", index=0)],
    }
    cli = {
        "broken": [_cli_reg("broken-cmd", plugin_key="broken", index=0)],
        "independent": [_cli_reg("ind-cmd", plugin_key="independent", index=0)],
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools, hook_registrations=hooks, cli_registrations=cli,
        settings=_make_settings(plugins_enabled=["broken", "independent"]),
        load_raises={"broken": PluginRegisterFailed("register_failed", "boom")},
    )
    await service.scan()

    # Tools: only independent's tool is published
    assert "broken-tool" not in service._registrations
    assert "ind-tool" in service._registrations

    # Hooks: only independent's hook
    assert "on_session_start" in service._hooks
    hook_keys = [r.plugin_key for r in service._hooks["on_session_start"]]
    assert "broken" not in hook_keys
    assert "independent" in hook_keys

    # CLI: only independent's command
    cli_names = [c.name for c in service.list_cli_commands()]
    assert "broken-cmd" not in cli_names
    assert "ind-cmd" in cli_names


# ---------------------------------------------------------------------------
# S2: Failure matrix - ToolService.replace_dynamic_definitions failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_service_replace_failure_preserves_last_generation():
    """When ToolService.replace_dynamic_definitions raises on a candidate
    generation, the caller continues to see the last successful generation
    (tools/hooks/CLI/routes/registry unchanged)."""
    # First scan: succeed with alpha
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}
    hooks_v1 = {"alpha": [_hook_reg("alpha", "on_session_start", index=0)]}
    cli_v1 = {"alpha": [_cli_reg("alpha-cmd", plugin_key="alpha", index=0)]}

    # Use a real ToolService-like mock that tracks state
    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1, hook_registrations=hooks_v1, cli_registrations=cli_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()

    # Snapshot generation 1 state
    gen1_registrations = dict(service._registrations)
    gen1_hooks = {k: v for k, v in service._hooks.items()}
    gen1_cli = list(service.list_cli_commands())
    gen1_gen_id = service.generation_id

    # Second scan: replace_dynamic_definitions raises
    tool_service.replace_dynamic_definitions.side_effect = RuntimeError("tool svc boom")
    # alpha now registers a DIFFERENT tool name so the candidate generation differs
    tools_v2 = {"alpha": [_tool_reg("alpha-tool-v2", plugin_key="alpha")]}
    hooks_v2 = {"alpha": [_hook_reg("alpha", "on_turn_start", index=0)]}
    cli_v2 = {"alpha": [_cli_reg("alpha-cmd-v2", plugin_key="alpha", index=0)]}

    # Rebuild loader with v2 registrations
    registry = AsyncMock()
    registry.list_plugins.return_value = list(plugins)
    registry.get_plugin.side_effect = lambda key: next(
        (p for p in plugins if p.key == key), None
    )
    registry.get_secret_config.return_value = {}
    registry.set_enabled.return_value = MagicMock()
    registry.replace_all_plugins = AsyncMock()
    service._registry = registry

    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in tools_v2.get(key, []):
            ctx.tool_registrations.append(reg)
        for reg in hooks_v2.get(key, []):
            ctx.hook_registrations.append(reg)
        for reg in cli_v2.get(key, []):
            ctx.cli_command_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register
    service._loader = loader

    await service.scan()

    # Caller still sees generation 1 state
    assert "alpha-tool" in service._registrations  # v1 preserved
    assert "alpha-tool-v2" not in service._registrations  # v2 NOT published

    # Hooks preserved (only on_session_start, not on_turn_start)
    assert "on_session_start" in service._hooks
    assert "on_turn_start" not in service._hooks

    # CLI preserved (alpha-cmd, not alpha-cmd-v2)
    cli_names = [c.name for c in service.list_cli_commands()]
    assert "alpha-cmd" in cli_names
    assert "alpha-cmd-v2" not in cli_names

    # generation_id NOT incremented
    assert service.generation_id == gen1_gen_id


# ---------------------------------------------------------------------------
# S2: Failure matrix - route_refresher failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_refresher_failure_preserves_last_generation():
    """When route_refresher raises, the caller continues to see the last
    successful generation (tools/hooks/CLI/routes/registry unchanged)."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}
    hooks_v1 = {"alpha": [_hook_reg("alpha", "on_session_start", index=0)]}
    cli_v1 = {"alpha": [_cli_reg("alpha-cmd", plugin_key="alpha", index=0)]}

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1, hook_registrations=hooks_v1, cli_registrations=cli_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()
    gen1_gen_id = service.generation_id

    # Second scan with v2 registrations and route_refresher that raises
    tools_v2 = {"alpha": [_tool_reg("alpha-tool-v2", plugin_key="alpha")]}
    hooks_v2 = {"alpha": [_hook_reg("alpha", "on_turn_start", index=0)]}
    cli_v2 = {"alpha": [_cli_reg("alpha-cmd-v2", plugin_key="alpha", index=0)]}

    def boom_refresher(names):
        raise RuntimeError("route refresher boom")
    service._route_refresher = boom_refresher

    # Rebuild loader with v2
    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in tools_v2.get(key, []):
            ctx.tool_registrations.append(reg)
        for reg in hooks_v2.get(key, []):
            ctx.hook_registrations.append(reg)
        for reg in cli_v2.get(key, []):
            ctx.cli_command_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register
    service._loader = loader

    await service.scan()

    # v1 preserved, v2 NOT published
    assert "alpha-tool" in service._registrations
    assert "alpha-tool-v2" not in service._registrations
    assert "on_session_start" in service._hooks
    assert "on_turn_start" not in service._hooks
    cli_names = [c.name for c in service.list_cli_commands()]
    assert "alpha-cmd" in cli_names
    assert "alpha-cmd-v2" not in cli_names
    assert service.generation_id == gen1_gen_id


# ---------------------------------------------------------------------------
# S2: Failure matrix - registry.replace_all_plugins failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_replace_all_plugins_failure_preserves_last_generation():
    """When registry.replace_all_plugins raises, the caller continues to see
    the last successful generation (tools/hooks/CLI/routes/registry unchanged)."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}
    hooks_v1 = {"alpha": [_hook_reg("alpha", "on_session_start", index=0)]}
    cli_v1 = {"alpha": [_cli_reg("alpha-cmd", plugin_key="alpha", index=0)]}

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1, hook_registrations=hooks_v1, cli_registrations=cli_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()
    gen1_gen_id = service.generation_id

    # Second scan: registry.replace_all_plugins raises
    # Need to rebuild the whole registry to make replace_all_plugins raise
    async def boom_replace(plugins_list):
        raise RuntimeError("registry boom")
    service._registry.replace_all_plugins = boom_replace

    tools_v2 = {"alpha": [_tool_reg("alpha-tool-v2", plugin_key="alpha")]}
    hooks_v2 = {"alpha": [_hook_reg("alpha", "on_turn_start", index=0)]}
    cli_v2 = {"alpha": [_cli_reg("alpha-cmd-v2", plugin_key="alpha", index=0)]}

    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in tools_v2.get(key, []):
            ctx.tool_registrations.append(reg)
        for reg in hooks_v2.get(key, []):
            ctx.hook_registrations.append(reg)
        for reg in cli_v2.get(key, []):
            ctx.cli_command_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register
    service._loader = loader

    await service.scan()

    # v1 preserved, v2 NOT published
    assert "alpha-tool" in service._registrations
    assert "alpha-tool-v2" not in service._registrations
    assert "on_session_start" in service._hooks
    assert "on_turn_start" not in service._hooks
    cli_names = [c.name for c in service.list_cli_commands()]
    assert "alpha-cmd" in cli_names
    assert "alpha-cmd-v2" not in cli_names
    # generation_id NOT incremented on failure
    assert service.generation_id == gen1_gen_id


# ---------------------------------------------------------------------------
# S2: Concurrent scan serialized by _scan_lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_scans_serialized_by_scan_lock():
    """Two concurrent scans must be serialized by _scan_lock; both complete
    successfully and the final state reflects the second scan."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]

    # We track invocation order via an ordered list
    scan_order: list[str] = []

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    registry = AsyncMock()
    registry.list_plugins.return_value = list(plugins)
    registry.get_plugin.side_effect = lambda key: next(
        (p for p in plugins if p.key == key), None
    )
    registry.get_secret_config.return_value = {}
    registry.set_enabled.return_value = MagicMock()

    async def slow_replace(plugins_list):
        scan_order.append("registry_enter")
        await asyncio.sleep(0.05)
        scan_order.append("registry_exit")
    registry.replace_all_plugins.side_effect = slow_replace

    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        return PluginContext(plugin_key=prepared.manifest.key)

    loader.load_and_register.side_effect = _load_and_register

    service = PluginService(
        registry=registry, loader=loader, tool_service=tool_service,
        route_refresher=lambda names: None,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )

    # Launch two scans concurrently
    await asyncio.gather(service.scan(), service.scan())

    # The registry calls must NOT interleave: enter, exit, enter, exit
    # (serialization means one scan's registry call fully completes before
    #  the other scan's registry call starts)
    assert scan_order == [
        "registry_enter", "registry_exit",
        "registry_enter", "registry_exit",
    ]


# ---------------------------------------------------------------------------
# S2: Candidate generation simultaneously contains all published state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_scan_publishes_all_state_simultaneously():
    """A successful scan publishes registrations, hooks, CLI, ToolService defs,
    routes, and registry state all from the same candidate generation."""
    cands = [
        _candidate("alpha", discovery_index=0),
        _candidate("beta", discovery_index=1),
    ]
    plugins = [_plugin("alpha"), _plugin("beta")]
    tools = {
        "alpha": [_tool_reg("alpha-tool", plugin_key="alpha")],
        "beta": [_tool_reg("beta-tool", plugin_key="beta")],
    }
    hooks = {
        "alpha": [_hook_reg("alpha", "on_session_start", index=0)],
        "beta": [_hook_reg("beta", "on_turn_start", index=0)],
    }
    cli = {
        "alpha": [_cli_reg("alpha-cmd", plugin_key="alpha", index=0)],
        "beta": [_cli_reg("beta-cmd", plugin_key="beta", index=0)],
    }

    captured_routes: list[set[str]] = []

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    captured_defs: list = []
    def _replace(key, defs, override_static_names=None):
        captured_defs.append((key, list(defs), set(override_static_names or set())))
    tool_service.replace_dynamic_definitions = _replace

    def refresher(names):
        captured_routes.append(set(names))
    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools, hook_registrations=hooks, cli_registrations=cli,
        tool_service=tool_service, route_refresher=refresher,
        settings=_make_settings(plugins_enabled=["alpha", "beta"]),
    )
    result = await service.scan()

    # All state from the same candidate generation
    assert "alpha-tool" in service._registrations
    assert "beta-tool" in service._registrations
    assert "on_session_start" in service._hooks
    assert "on_turn_start" in service._hooks
    cli_names = {c.name for c in service.list_cli_commands()}
    assert cli_names == {"alpha-cmd", "beta-cmd"}

    # ToolService received both defs
    assert captured_defs
    last_key, last_defs, last_override = captured_defs[-1]
    assert last_key == "plugin"
    def_names = {d.name for d in last_defs}
    assert def_names == {"alpha-tool", "beta-tool"}

    # Routes include both tool names
    assert captured_routes
    assert {"alpha-tool", "beta-tool"} <= captured_routes[-1]

    # generation_id was incremented to 1
    assert service.generation_id == 1


# ---------------------------------------------------------------------------
# S3: generation_id increments ONLY after all publish steps succeed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generation_id_starts_at_zero_and_increments_on_success():
    """generation_id starts at 0 and increments to 1 after a successful scan."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    assert service.generation_id == 0
    await service.scan()
    assert service.generation_id == 1


@pytest.mark.asyncio
async def test_generation_id_increments_on_each_successful_scan():
    """Each successful scan increments generation_id by 1."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()
    assert service.generation_id == 1
    await service.scan()
    assert service.generation_id == 2
    await service.scan()
    assert service.generation_id == 3


@pytest.mark.asyncio
async def test_generation_id_not_incremented_on_failure():
    """generation_id is NOT incremented when a publish step fails."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}
    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()
    assert service.generation_id == 1

    # Second scan: replace_dynamic_definitions raises
    tool_service.replace_dynamic_definitions.side_effect = RuntimeError("boom")
    await service.scan()
    assert service.generation_id == 1  # unchanged


# ---------------------------------------------------------------------------
# S3: Compensating rollback - reverse order on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_service_rolled_back_on_registry_failure():
    """When registry.replace_all_plugins fails AFTER ToolService has been
    updated, ToolService is rolled back to the previous generation's state
    (defs + suppression)."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    # v1: alpha registers alpha-tool
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()
    gen1_gen_id = service.generation_id
    # Snapshot the defs+override that were committed in gen1
    assert tool_service.replace_dynamic_definitions.call_count == 1
    gen1_call = tool_service.replace_dynamic_definitions.call_args
    gen1_defs = list(gen1_call.args[1])
    gen1_override = set(gen1_call.args[2] if len(gen1_call.args) > 2 else set())

    # Second scan: v2 tools, registry fails
    tools_v2 = {"alpha": [_tool_reg("alpha-tool-v2", plugin_key="alpha")]}
    async def boom_replace(plugins_list):
        raise RuntimeError("registry boom")
    service._registry.replace_all_plugins = boom_replace

    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in tools_v2.get(key, []):
            ctx.tool_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register
    service._loader = loader

    await service.scan()

    # ToolService should have been called again with v2, then rolled back to v1
    # Final call should restore v1 state
    final_call = tool_service.replace_dynamic_definitions.call_args
    final_defs = final_call.args[1]
    final_override = set(final_call.args[2] if len(final_call.args) > 2 else set())
    final_def_names = {d.name for d in final_defs}
    assert final_def_names == {"alpha-tool"}  # v1, not v2
    assert final_override == gen1_override

    # Internal registrations reflect v1 (rolled back)
    assert "alpha-tool" in service._registrations
    assert "alpha-tool-v2" not in service._registrations
    # generation_id NOT incremented
    assert service.generation_id == gen1_gen_id


@pytest.mark.asyncio
async def test_routes_rolled_back_on_failure():
    """When a publish step fails AFTER routes have been refreshed, routes are
    rolled back to the previous generation's state."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    captured_routes: list[set[str]] = []
    def refresher(names):
        captured_routes.append(set(names))

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1,
        tool_service=tool_service, route_refresher=refresher,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()
    # gen1 routes: {alpha-tool}
    gen1_routes = set(captured_routes[-1])
    assert gen1_routes == {"alpha-tool"}

    # Second scan: registry fails after routes refreshed
    tools_v2 = {"alpha": [_tool_reg("alpha-tool-v2", plugin_key="alpha")]}
    async def boom_replace(plugins_list):
        raise RuntimeError("registry boom")
    service._registry.replace_all_plugins = boom_replace

    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in tools_v2.get(key, []):
            ctx.tool_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register
    service._loader = loader

    await service.scan()

    # Final route refresh should restore gen1 routes
    final_routes = captured_routes[-1]
    assert final_routes == {"alpha-tool"}  # not {"alpha-tool-v2"}


@pytest.mark.asyncio
async def test_in_memory_snapshot_rolled_back_on_failure():
    """When registry fails, in-memory state (_hooks, _cli_commands,
    _registrations, _plugin_registrations) is rolled back to the previous
    generation."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}
    hooks_v1 = {"alpha": [_hook_reg("alpha", "on_session_start", index=0)]}
    cli_v1 = {"alpha": [_cli_reg("alpha-cmd", plugin_key="alpha", index=0)]}

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1, hook_registrations=hooks_v1, cli_registrations=cli_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()

    # Second scan: v2 state, registry fails
    tools_v2 = {"alpha": [_tool_reg("alpha-tool-v2", plugin_key="alpha")]}
    hooks_v2 = {"alpha": [_hook_reg("alpha", "on_turn_start", index=0)]}
    cli_v2 = {"alpha": [_cli_reg("alpha-cmd-v2", plugin_key="alpha", index=0)]}
    async def boom_replace(plugins_list):
        raise RuntimeError("registry boom")
    service._registry.replace_all_plugins = boom_replace

    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in tools_v2.get(key, []):
            ctx.tool_registrations.append(reg)
        for reg in hooks_v2.get(key, []):
            ctx.hook_registrations.append(reg)
        for reg in cli_v2.get(key, []):
            ctx.cli_command_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register
    service._loader = loader

    await service.scan()

    # In-memory state reflects v1 (rolled back)
    assert "alpha-tool" in service._registrations
    assert "alpha-tool-v2" not in service._registrations
    assert "on_session_start" in service._hooks
    assert "on_turn_start" not in service._hooks
    cli_names = [c.name for c in service.list_cli_commands()]
    assert "alpha-cmd" in cli_names
    assert "alpha-cmd-v2" not in cli_names


# ---------------------------------------------------------------------------
# S3: Cross SQLite/memory no transaction atomicity claim; scan failure recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_failure_records_scan_failure_no_live_candidate():
    """If rollback ITSELF fails (e.g., ToolService restore raises), a scan
    failure is recorded (last_scan_error) and the candidate is NOT set as live.

    The caller may see a partially-inconsistent state in this edge case, but
    generation_id is NOT incremented and last_scan_error is set.
    """
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()
    gen1_gen_id = service.generation_id

    # Second scan: route_refresher fails (triggers rollback path), AND
    # ToolService restore also fails (rollback itself fails).
    # Order of operations on failure:
    #   1. ToolService.replace_dynamic_definitions (v2) - succeeds
    #   2. route_refresher (v2) - FAILS -> rollback triggered
    #   Rollback reverse order:
    #   1. routes restore (impossible since route_refresher failed; restore
    #      calls route_refresher with prev names -> may also fail)
    #   2. ToolService restore with prev defs - we make this fail too
    # When restore fails, scan failure recorded, candidate not live.

    # Make the SECOND call to replace_dynamic_definitions succeed (v2 commit),
    # but the THIRD call (rollback to v1) fail.
    call_count = [0]
    def _replace_side_effect(key, defs, override_static_names=None):
        call_count[0] += 1
        if call_count[0] == 3:
            # Rollback call - fail
            raise RuntimeError("restore boom")
    tool_service.replace_dynamic_definitions.side_effect = _replace_side_effect

    # route_refresher raises on v2 to trigger rollback
    def boom_refresher(names):
        raise RuntimeError("route boom")
    service._route_refresher = boom_refresher

    tools_v2 = {"alpha": [_tool_reg("alpha-tool-v2", plugin_key="alpha")]}
    discovery_result = PluginDiscoveryResult(
        candidates=cands, winners={c.key: c for c in cands}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    def _load_and_register(prepared, cfg, secret):
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in tools_v2.get(key, []):
            ctx.tool_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register
    service._loader = loader

    await service.scan()

    # generation_id NOT incremented (failure)
    assert service.generation_id == gen1_gen_id
    # last_scan_error recorded
    assert service.last_scan_error is not None
    assert "scan" in (service.last_scan_error or "").lower() or \
           "publish" in (service.last_scan_error or "").lower() or \
           "rollback" in (service.last_scan_error or "").lower() or \
           "route" in (service.last_scan_error or "").lower()


# ---------------------------------------------------------------------------
# S3: First scan with failure - no previous generation to roll back to
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_scan_failure_with_no_previous_generation():
    """When the first-ever scan fails at publish, there is no previous
    generation to roll back to. generation_id stays 0, and the failure is
    recorded without crashing."""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]
    tools_v1 = {"alpha": [_tool_reg("alpha-tool", plugin_key="alpha")]}

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()
    tool_service.replace_dynamic_definitions.side_effect = RuntimeError("boom")

    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        tool_registrations=tools_v1,
        tool_service=tool_service,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    assert service.generation_id == 0

    await service.scan()

    # generation_id NOT incremented
    assert service.generation_id == 0
    # No tools published
    assert service._registrations == {}
    assert service._hooks == {}
    assert service.list_cli_commands() == []
    # last_scan_error recorded
    assert service.last_scan_error is not None


# ---------------------------------------------------------------------------
# S3: Hook dispatch uses snapshot at call start (T6 invariant preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_dispatch_uses_snapshot_at_call_start_after_scan():
    """After a successful scan, invoke_hook uses the snapshot of _hooks taken
    at call start; replacing _hooks during invocation does not affect an
    in-flight dispatch. (T6 invariant preserved by T9.)"""
    cands = [_candidate("alpha", discovery_index=0)]
    plugins = [_plugin("alpha")]

    called: list[str] = []

    async def callback_a(**kwargs):
        called.append("A")
        # Replace _hooks during execution; callback_b should NOT be called
        service._hooks = {"on_session_start": (
            HookRegistration(
                plugin_key="p2", hook_name="on_session_start",
                callback=callback_b, registration_index=0,
            ),
        )}

    def callback_b(**kwargs):
        called.append("B")

    hooks_v1 = {
        "alpha": [
            HookRegistration(
                plugin_key="alpha", hook_name="on_session_start",
                callback=callback_a, registration_index=0,
            ),
        ]
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands,
        hook_registrations=hooks_v1,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()

    await service.invoke_hook("on_session_start")
    assert called == ["A"]  # B not called
