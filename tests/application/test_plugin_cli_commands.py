"""T9 S1: CLI command snapshot tests.

Covers:
- ``list_cli_commands()`` returns a list COPY of the immutable snapshot;
  callers cannot mutate internal state.
- Snapshot is replaced wholesale on successful scan.
- Stable order by (plugin topo load order, registration_index).
- Disabled, admission-failed, and register-failed plugins contribute NO
  commands.
- ``PluginScanResult.cli_command_registrations`` is populated from
  PluginContext (direct collection, same pattern as hooks).
- ``_cli_commands`` is a tuple (immutable) snapshot.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.plugin_service import (
    PluginCliCommand,
    PluginContext,
    PluginService,
    PluginToolRegistration,
)
from app.domain.plugin import Plugin, PluginKind, PluginManifest, PluginSource
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


def _cli_reg(
    name: str,
    *,
    plugin_key: str = "p1",
    setup_fn=None,
    handler_fn=None,
    description: str = "",
    registration_index: int = 0,
) -> PluginCliCommand:
    return PluginCliCommand(
        plugin_key=plugin_key,
        name=name,
        help=f"help for {name}",
        description=description,
        setup_fn=setup_fn or (lambda parser: None),
        handler_fn=handler_fn,
        registration_index=registration_index,
    )


def _build_service(
    *,
    registry_plugins: list[Plugin],
    candidates: list[DiscoveryCandidate] | None = None,
    cli_registrations: dict[str, list[PluginCliCommand]] | None = None,
    load_raises: dict[str, Exception] | None = None,
    settings=None,
) -> PluginService:
    """Build a PluginService with 3-phase loader API mocked.

    CLI registrations are populated into PluginContext directly (same pattern
    that scan() uses for hooks and tools).
    """
    registry = AsyncMock()
    registry.list_plugins.return_value = list(registry_plugins)
    registry.get_plugin.side_effect = lambda key: next(
        (p for p in registry_plugins if p.key == key), None
    )
    registry.get_secret_config.return_value = {}
    registry.set_enabled.return_value = MagicMock()
    registry.replace_all_plugins = AsyncMock()

    cand_list = list(candidates or [])
    winners = {c.key: c for c in cand_list}
    discovery_result = PluginDiscoveryResult(
        candidates=cand_list, winners=winners, warnings=[],
    )

    loader = MagicMock()
    loader.discover.return_value = discovery_result

    _load_raises = load_raises or {}
    _cli_regs = cli_registrations or {}

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
        for reg in _cli_regs.get(key, []):
            ctx.cli_command_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = PluginService(
        registry=registry,
        loader=loader,
        tool_service=tool_service,
        route_refresher=lambda names: None,
        settings=settings or _make_settings(),
    )
    return service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cli_commands_returns_empty_list_by_default():
    """Before any scan, list_cli_commands() returns an empty list (not None)."""
    service = _build_service(registry_plugins=[])
    commands = service.list_cli_commands()
    assert commands == []
    assert isinstance(commands, list)


@pytest.mark.asyncio
async def test_list_cli_commands_returns_list_copy_of_snapshot():
    """list_cli_commands() returns a list copy; mutating it does not affect
    internal state."""
    plugins = [Plugin(id="p1", key="alpha", name="alpha", source=PluginSource.BUNDLED, enabled=True)]
    cands = [_candidate("alpha", discovery_index=0)]
    cli_regs = {
        "alpha": [
            _cli_reg("hello", plugin_key="alpha", registration_index=0),
            _cli_reg("world", plugin_key="alpha", registration_index=1),
        ]
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()

    # Internal snapshot is a tuple
    assert isinstance(service._cli_commands, tuple)
    assert len(service._cli_commands) == 2

    # list_cli_commands returns a list
    commands = service.list_cli_commands()
    assert isinstance(commands, list)
    assert len(commands) == 2
    assert [c.name for c in commands] == ["hello", "world"]

    # Mutating the returned list does NOT change internal state
    commands.clear()
    commands.append("malicious")

    commands2 = service.list_cli_commands()
    assert len(commands2) == 2
    assert [c.name for c in commands2] == ["hello", "world"]


@pytest.mark.asyncio
async def test_cli_commands_snapshot_replaced_wholesale_on_rescan():
    """Two consecutive scans replace (not accumulate) _cli_commands."""
    plugins = [Plugin(id="p1", key="alpha", name="alpha", source=PluginSource.BUNDLED, enabled=True)]
    manifest = _manifest("alpha")
    cand = _candidate("alpha", discovery_index=0)

    # v1: alpha registers 'hello'
    cli_v1 = {"alpha": [_cli_reg("hello", plugin_key="alpha", registration_index=0)]}
    # v2: alpha registers 'world' (no 'hello')
    cli_v2 = {"alpha": [_cli_reg("world", plugin_key="alpha", registration_index=0)]}

    # We need a loader that returns different ctx on different scans
    registry = AsyncMock()
    registry.list_plugins.return_value = list(plugins)
    registry.get_plugin.side_effect = lambda key: next(
        (p for p in plugins if p.key == key), None
    )
    registry.get_secret_config.return_value = {}
    registry.replace_all_plugins = AsyncMock()

    discovery_result = PluginDiscoveryResult(
        candidates=[cand], winners={cand.key: cand}, warnings=[],
    )
    loader = MagicMock()
    loader.discover.return_value = discovery_result
    loader.prepare.side_effect = lambda c: PreparedPlugin(
        manifest=c.manifest, source=c.source,
        token=LoaderToken("directory", {"path": c.path}), warnings=[],
    )

    cli_versions = [cli_v1, cli_v2]
    scan_idx = [0]

    def _load_and_register(prepared, cfg, secret):
        idx = min(scan_idx[0], len(cli_versions) - 1)
        current = cli_versions[idx]
        key = prepared.manifest.key
        ctx = PluginContext(plugin_key=key, plugin_config=cfg or {}, secret_config=secret or {})
        for reg in current.get(key, []):
            ctx.cli_command_registrations.append(reg)
        return ctx

    loader.load_and_register.side_effect = _load_and_register

    tool_service = MagicMock()
    tool_service.list_definitions.return_value = []
    tool_service.definitions = {}
    tool_service.replace_dynamic_definitions = MagicMock()

    service = PluginService(
        registry=registry, loader=loader, tool_service=tool_service,
        route_refresher=lambda names: None,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )

    await service.scan()
    assert [c.name for c in service.list_cli_commands()] == ["hello"]

    scan_idx[0] = 1
    await service.scan()
    # 'hello' must be gone; only 'world' present
    names = [c.name for c in service.list_cli_commands()]
    assert names == ["world"]
    assert "hello" not in names


@pytest.mark.asyncio
async def test_cli_commands_stable_order_by_plugin_load_order_then_registration_index():
    """CLI commands ordered by (plugin topo load order, registration_index).

    Manifest order: gamma(0), alpha(1), beta(2). Each plugin registers multiple
    commands with mixed registration_index values.
    """
    manifests = [_manifest("gamma"), _manifest("alpha"), _manifest("beta")]
    cands = [
        _candidate("gamma", discovery_index=0, manifest=manifests[0]),
        _candidate("alpha", discovery_index=1, manifest=manifests[1]),
        _candidate("beta", discovery_index=2, manifest=manifests[2]),
    ]
    plugins = [
        Plugin(id="p1", key="gamma", name="gamma", source=PluginSource.BUNDLED, enabled=True),
        Plugin(id="p2", key="alpha", name="alpha", source=PluginSource.BUNDLED, enabled=True),
        Plugin(id="p3", key="beta", name="beta", source=PluginSource.BUNDLED, enabled=True),
    ]
    cli_regs = {
        "alpha": [
            _cli_reg("a2", plugin_key="alpha", registration_index=5),
            _cli_reg("a1", plugin_key="alpha", registration_index=2),
        ],
        "beta": [
            _cli_reg("b1", plugin_key="beta", registration_index=1),
        ],
        "gamma": [
            _cli_reg("g1", plugin_key="gamma", registration_index=0),
        ],
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["alpha", "beta", "gamma"]),
    )
    await service.scan()

    commands = service.list_cli_commands()
    # Expected order: gamma(g1, idx 0), alpha(a1 idx 2), alpha(a2 idx 5), beta(b1 idx 1)
    assert [c.name for c in commands] == ["g1", "a1", "a2", "b1"]
    assert [c.registration_index for c in commands] == [0, 2, 5, 1]
    assert [c.plugin_key for c in commands] == ["gamma", "alpha", "alpha", "beta"]


@pytest.mark.asyncio
async def test_cli_commands_disabled_plugin_contributes_nothing():
    """A disabled plugin (not in effective_enabled) contributes no CLI commands."""
    plugins = [
        Plugin(id="p1", key="alpha", name="alpha", source=PluginSource.BUNDLED, enabled=True),
        Plugin(id="p2", key="beta", name="beta", source=PluginSource.BUNDLED, enabled=False),
    ]
    cands = [
        _candidate("alpha", discovery_index=0),
        _candidate("beta", discovery_index=1),
    ]
    cli_regs = {
        "alpha": [_cli_reg("hello", plugin_key="alpha")],
        "beta": [_cli_reg("world", plugin_key="beta")],
    }
    # beta is in registry as enabled=False and NOT in settings.plugins_enabled
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()

    names = [c.name for c in service.list_cli_commands()]
    assert names == ["hello"]
    assert "world" not in names


@pytest.mark.asyncio
async def test_cli_commands_admission_failed_plugin_contributes_nothing():
    """A plugin that fails admission (missing required dep) contributes nothing."""
    # dependent requires "base", but "base" is not discovered
    cands = [
        _candidate(
            "dependent", discovery_index=0,
            manifest=_manifest("dependent"),
        ),
    ]
    # Patch manifest to require a missing plugin
    cands[0] = _candidate(
        "dependent", discovery_index=0,
        manifest=PluginManifest(
            key="dependent", name="dependent", version="1.0.0", description="",
            source=PluginSource.BUNDLED, path="/p/dependent",
            kind=PluginKind.STANDALONE, requires_plugins=["base"],
        ),
    )
    plugins = [
        Plugin(id="p1", key="dependent", name="dependent", source=PluginSource.BUNDLED, enabled=True),
    ]
    cli_regs = {
        "dependent": [_cli_reg("dep-cmd", plugin_key="dependent")],
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["dependent"]),
    )
    await service.scan()

    # dependent should not have been admitted, so no CLI commands
    commands = service.list_cli_commands()
    assert commands == []


@pytest.mark.asyncio
async def test_cli_commands_register_failed_plugin_contributes_nothing():
    """A plugin whose register() raises contributes no CLI commands, but
    independent plugins continue."""
    cands = [
        _candidate("broken", discovery_index=0),
        _candidate("independent", discovery_index=1),
    ]
    plugins = [
        Plugin(id="p1", key="broken", name="broken", source=PluginSource.BUNDLED, enabled=True),
        Plugin(id="p2", key="independent", name="independent", source=PluginSource.BUNDLED, enabled=True),
    ]
    cli_regs = {
        "broken": [_cli_reg("broken-cmd", plugin_key="broken")],
        "independent": [_cli_reg("ind-cmd", plugin_key="independent")],
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["broken", "independent"]),
        load_raises={"broken": PluginRegisterFailed("register_failed", "boom")},
    )
    await service.scan()

    # broken's commands dropped; independent's command present
    names = [c.name for c in service.list_cli_commands()]
    assert names == ["ind-cmd"]
    assert "broken-cmd" not in names


@pytest.mark.asyncio
async def test_plugin_scan_result_has_cli_command_registrations_field():
    """PluginScanResult includes cli_command_registrations populated from
    successful PluginContexts (direct collection, not from loader.scan)."""
    plugins = [
        Plugin(id="p1", key="alpha", name="alpha", source=PluginSource.BUNDLED, enabled=True),
    ]
    cands = [_candidate("alpha", discovery_index=0)]
    cli_regs = {
        "alpha": [
            _cli_reg("hello", plugin_key="alpha", registration_index=0),
            _cli_reg("world", plugin_key="alpha", registration_index=1),
        ]
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    result = await service.scan()

    # PluginScanResult must expose cli_command_registrations keyed by plugin_key
    assert hasattr(result, "cli_command_registrations")
    assert "alpha" in result.cli_command_registrations
    assert len(result.cli_command_registrations["alpha"]) == 2
    names = [c.name for c in result.cli_command_registrations["alpha"]]
    assert names == ["hello", "world"]


@pytest.mark.asyncio
async def test_plugin_scan_result_excludes_failed_plugin_cli_registrations():
    """A failed plugin's CLI registrations do NOT appear in
    PluginScanResult.cli_command_registrations."""
    cands = [
        _candidate("broken", discovery_index=0),
        _candidate("independent", discovery_index=1),
    ]
    plugins = [
        Plugin(id="p1", key="broken", name="broken", source=PluginSource.BUNDLED, enabled=True),
        Plugin(id="p2", key="independent", name="independent", source=PluginSource.BUNDLED, enabled=True),
    ]
    cli_regs = {
        "broken": [_cli_reg("broken-cmd", plugin_key="broken")],
        "independent": [_cli_reg("ind-cmd", plugin_key="independent")],
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["broken", "independent"]),
        load_raises={"broken": PluginRegisterFailed("register_failed", "boom")},
    )
    result = await service.scan()

    assert "broken" not in result.cli_command_registrations
    assert "independent" in result.cli_command_registrations
    assert len(result.cli_command_registrations["independent"]) == 1


@pytest.mark.asyncio
async def test_cli_commands_snapshot_is_immutable_tuple():
    """_cli_commands is a tuple (immutable); cannot be modified in place."""
    plugins = [
        Plugin(id="p1", key="alpha", name="alpha", source=PluginSource.BUNDLED, enabled=True),
    ]
    cands = [_candidate("alpha", discovery_index=0)]
    cli_regs = {"alpha": [_cli_reg("hello", plugin_key="alpha")]}
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()

    assert isinstance(service._cli_commands, tuple)
    # Tuples don't support item assignment or append
    with pytest.raises((AttributeError, TypeError)):
        service._cli_commands.append(_cli_reg("evil"))  # type: ignore[attr-defined]
    with pytest.raises((TypeError, IndexError)):
        service._cli_commands[0] = _cli_reg("evil")  # type: ignore[index]


@pytest.mark.asyncio
async def test_cli_commands_snapshot_preserves_all_fields():
    """Each PluginCliCommand in the snapshot preserves all fields from the
    original registration."""
    def setup_fn(parser):
        return "setup"

    def handler_fn(args):
        return 42

    plugins = [
        Plugin(id="p1", key="alpha", name="alpha", source=PluginSource.BUNDLED, enabled=True),
    ]
    cands = [_candidate("alpha", discovery_index=0)]
    cli_regs = {
        "alpha": [
            PluginCliCommand(
                plugin_key="alpha",
                name="hello",
                help="greet user",
                description="a friendly command",
                setup_fn=setup_fn,
                handler_fn=handler_fn,
                registration_index=3,
            ),
        ]
    }
    service = _build_service(
        registry_plugins=plugins, candidates=cands, cli_registrations=cli_regs,
        settings=_make_settings(plugins_enabled=["alpha"]),
    )
    await service.scan()

    commands = service.list_cli_commands()
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd.plugin_key == "alpha"
    assert cmd.name == "hello"
    assert cmd.help == "greet user"
    assert cmd.description == "a friendly command"
    assert cmd.setup_fn is setup_fn
    assert cmd.handler_fn is handler_fn
    assert cmd.registration_index == 3
