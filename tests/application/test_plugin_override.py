"""T8: Plugin override trust gate and deterministic conflict resolution.

Covers:
- S1: ``_tool_override_allowed(plugin_key, source)`` -- BUNDLED auto-allow;
  USER/PROJECT/ENTRY_POINT require exact allowlist; fail-closed; trust is based
  on the WINNING manifest's actual source, NOT inherited from a shadowed
  bundled same-key plugin.
- S2: Static conflict -- override=True allowed -> plugin is the唯一 live
  definition across ToolService queries / LLM exposure / execute / routes;
  override=True not allowed -> unavailable with exact reason; override=False ->
  unavailable with exact reason.
- S3: Inter-plugin/same-plugin same-name -- stable first-available wins, rest
  unavailable with ``conflicts with plugin tool from {plugin_key}``; override
  cannot bypass; requires_env/check_fn unavailable don't preempt; restore on
  disable / allowlist-removal / load failure / override cancellation.
"""
from __future__ import annotations

import pytest

from app.application.plugin_service import (
    PluginService,
    PluginToolRegistration,
)
from app.application.tool_service import ToolService
from app.domain.plugin import PluginManifest, PluginSource
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class RecordingExecutor:
    """Records execute() calls and returns a success result."""

    def __init__(self) -> None:
        self.calls: list[ToolCallRequest] = []

    async def execute(self, request: ToolCallRequest, context=None) -> ToolResult:
        self.calls.append(request)
        return ToolResult(
            request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True}
        )


def _make_settings(
    *,
    plugins_override_allowlist: list[str] | None = None,
    plugin_tool_timeout_seconds: int = 30,
) -> object:
    import types

    return types.SimpleNamespace(
        plugins_override_allowlist=list(plugins_override_allowlist or []),
        plugins_enabled=[],
        plugins_disabled=[],
        plugin_tool_timeout_seconds=plugin_tool_timeout_seconds,
        plugin_hook_timeout_seconds=5.0,
    )


def _static_def(
    name: str,
    *,
    description: str = "builtin",
    risk_level: RiskLevel = RiskLevel.SAFE,
    toolset: str = "builtin",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {}},
        risk_level=risk_level,
        source_type=ToolSourceType.BUILTIN,
        toolset=toolset,
    )


def _manifest(
    key: str,
    *,
    source: PluginSource = PluginSource.BUNDLED,
) -> PluginManifest:
    return PluginManifest(
        key=key,
        name=key,
        version="1.0.0",
        description="",
        source=source,
        path=f"/p/{key}",
    )


def _reg(
    name: str,
    *,
    plugin_key: str = "p1",
    override: bool = False,
    check_fn=None,
    requires_env=None,
    description: str = "plugin tool",
    handler=None,
) -> PluginToolRegistration:
    return PluginToolRegistration(
        plugin_key=plugin_key,
        name=name,
        schema={"parameters": {"type": "object", "properties": {}}},
        handler=handler or (lambda args, **kw: {"ok": True}),
        override=override,
        check_fn=check_fn,
        requires_env=requires_env,
        description=description,
        plugin_config={},
        secret_config={},
    )


def _build_service(
    *,
    static_defs: list[ToolDefinition] | None = None,
    settings=None,
    manifests_by_key: dict[str, PluginManifest] | None = None,
    plugin_registrations: dict[str, list[PluginToolRegistration]] | None = None,
) -> tuple[PluginService, ToolService, list[set[str]], RecordingExecutor]:
    """Build a PluginService wired to a REAL ToolService.

    Populates the fields that ``scan()`` would normally set, so
    ``_refresh_tool_surface`` can be called directly.
    """
    executor = RecordingExecutor()
    tool_service = ToolService(executor, list(static_defs or []))

    captured_routes: list[set[str]] = []

    def refresher(names: set[str]) -> None:
        captured_routes.append(set(names))

    from unittest.mock import MagicMock

    service = PluginService(
        registry=MagicMock(),
        loader=MagicMock(),
        tool_service=tool_service,
        route_refresher=refresher,
        settings=settings or _make_settings(),
    )
    # Populate fields normally set by scan() publish step
    service._plugin_registrations = dict(plugin_registrations or {})
    service._manifests_by_key = dict(manifests_by_key or {})
    return service, tool_service, captured_routes, executor


# ---------------------------------------------------------------------------
# S1: _tool_override_allowed
# ---------------------------------------------------------------------------


class TestToolOverrideAllowed:
    """``_tool_override_allowed`` gate: BUNDLED auto-allow; else exact allowlist;
    fail-closed; winner's actual source, no inheritance."""

    def test_bundled_auto_allowed(self):
        service, *_ = _build_service()
        assert service._tool_override_allowed("any_key", PluginSource.BUNDLED) is True

    def test_user_in_allowlist_allowed(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=["allowed_user"])
        )
        assert service._tool_override_allowed("allowed_user", PluginSource.USER) is True

    def test_user_not_in_allowlist_denied(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=["other"])
        )
        assert service._tool_override_allowed("not_allowed", PluginSource.USER) is False

    def test_user_empty_allowlist_denied(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=[])
        )
        assert service._tool_override_allowed("any_user", PluginSource.USER) is False

    def test_project_in_allowlist_allowed(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=["proj_plug"])
        )
        assert service._tool_override_allowed("proj_plug", PluginSource.PROJECT) is True

    def test_project_not_in_allowlist_denied(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=[])
        )
        assert service._tool_override_allowed("proj_plug", PluginSource.PROJECT) is False

    def test_entry_point_in_allowlist_allowed(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=["ep_plug"])
        )
        assert service._tool_override_allowed("ep_plug", PluginSource.ENTRY_POINT) is True

    def test_entry_point_not_in_allowlist_denied(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=[])
        )
        assert service._tool_override_allowed("ep_plug", PluginSource.ENTRY_POINT) is False

    def test_allowlist_exact_match_case_sensitive(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=["MyPlugin"])
        )
        assert service._tool_override_allowed("MyPlugin", PluginSource.USER) is True
        assert service._tool_override_allowed("myplugin", PluginSource.USER) is False
        assert service._tool_override_allowed("MYPLUGIN", PluginSource.USER) is False

    def test_fail_closed_for_none_source(self):
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=["any_key"])
        )
        assert service._tool_override_allowed("any_key", None) is False

    def test_no_inheritance_from_shadowed_bundled(self):
        """Winner manifest source is USER; even if a BUNDLED same-key plugin
        was shadowed by T7 source-priority, the WINNER's source (USER) is what
        matters. A non-allowlisted USER winner must NOT inherit BUNDLED trust."""
        service, *_ = _build_service(
            settings=_make_settings(plugins_override_allowlist=[]),
            manifests_by_key={
                "shared_key": _manifest("shared_key", source=PluginSource.USER),
            },
        )
        # Winner manifest source is USER, not BUNDLED -> not allowed
        assert service._tool_override_allowed("shared_key", PluginSource.USER) is False

    def test_no_inheritance_integration_override_denied_for_user_winner(self):
        """End-to-end: a USER plugin (winner) with override=True colliding with
        a static tool is denied, even if conceptually a BUNDLED same-key plugin
        was shadowed. The winner's source (USER) controls the gate."""
        static = _static_def("calculator")
        service, tool_service, *_ = _build_service(
            static_defs=[static],
            settings=_make_settings(plugins_override_allowlist=[]),
            manifests_by_key={
                "shared_key": _manifest("shared_key", source=PluginSource.USER),
            },
            plugin_registrations={
                "shared_key": [_reg("calculator", plugin_key="shared_key", override=True)],
            },
        )
        service._refresh_tool_surface()

        reg = service._registrations["calculator"]
        assert reg.available is False
        assert reg.unavailable_reason == (
            "override not permitted; add plugin key to plugins_override_allowlist"
        )
        # Static definition is still the live one
        assert tool_service.get_definition("calculator") is static


# ---------------------------------------------------------------------------
# S2: Static conflict -- override gate + exact reasons
# ---------------------------------------------------------------------------


class TestStaticConflictOverride:
    """Override=True allowed ->唯一 live def; not allowed -> exact reason;
    override=False -> exact reason."""

    @pytest.mark.asyncio
    async def test_override_allowed_bundled_plugin_is_unique_live_definition(self):
        """BUNDLED plugin with override=True -> plugin is the唯一 live def across
        all ToolService query surfaces, execute, and routes."""
        static = _static_def("calculator", description="builtin calc", risk_level=RiskLevel.SAFE)
        service, tool_service, captured_routes, executor = _build_service(
            static_defs=[static],
            settings=_make_settings(plugins_override_allowlist=[]),
            manifests_by_key={"bundled_plug": _manifest("bundled_plug", source=PluginSource.BUNDLED)},
            plugin_registrations={
                "bundled_plug": [
                    _reg(
                        "calculator",
                        plugin_key="bundled_plug",
                        override=True,
                        description="plugin calc",
                    )
                ]
            },
        )
        service._refresh_tool_surface()

        # Registration is available
        reg = service._registrations["calculator"]
        assert reg.available is True
        assert reg.unavailable_reason is None

        # list_definitions -- only plugin version
        defs = {d.name: d for d in tool_service.list_definitions()}
        assert "calculator" in defs
        assert defs["calculator"].description == "plugin calc"
        assert defs["calculator"] is not static
        # No duplicate
        calc_count = sum(1 for d in tool_service.list_definitions() if d.name == "calculator")
        assert calc_count == 1

        # get_definition -- plugin version
        assert tool_service.get_definition("calculator") is not static
        assert tool_service.get_definition("calculator").description == "plugin calc"

        # list_openai_tools -- only plugin version
        schemas = tool_service.list_openai_tools()
        calc_schemas = [s for s in schemas if s["function"]["name"] == "calculator"]
        assert len(calc_schemas) == 1
        assert calc_schemas[0]["function"]["description"] == "plugin calc"

        # execute -- uses plugin definition (plugin is SAFE, so allowed)
        result = await tool_service.execute(
            ToolCallRequest(id="1", name="calculator", arguments={})
        )
        assert result.status is ToolResultStatus.SUCCESS
        assert len(executor.calls) == 1
        assert executor.calls[0].name == "calculator"

        # routes -- tool name captured for CompositeToolExecutor
        assert captured_routes
        assert "calculator" in captured_routes[-1]

        # Static dict not mutated
        assert tool_service.definitions["calculator"] is static

    def test_override_allowed_user_in_allowlist_is_unique_live_definition(self):
        """USER plugin in allowlist with override=True -> plugin is唯一 live def."""
        static = _static_def("calculator", description="builtin", risk_level=RiskLevel.SAFE)
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=_make_settings(plugins_override_allowlist=["user_plug"]),
            manifests_by_key={"user_plug": _manifest("user_plug", source=PluginSource.USER)},
            plugin_registrations={
                "user_plug": [
                    _reg(
                        "calculator",
                        plugin_key="user_plug",
                        override=True,
                        description="user plugin calc",
                    )
                ]
            },
        )
        service._refresh_tool_surface()

        reg = service._registrations["calculator"]
        assert reg.available is True
        assert tool_service.get_definition("calculator").description == "user plugin calc"
        assert "calculator" in captured_routes[-1]

    def test_override_not_permitted_user_not_in_allowlist_unavailable_exact_reason(self):
        """USER plugin not in allowlist with override=True -> unavailable with
        exact reason ``override not permitted; add plugin key to
        plugins_override_allowlist``."""
        static = _static_def("calculator")
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=_make_settings(plugins_override_allowlist=[]),
            manifests_by_key={"user_plug": _manifest("user_plug", source=PluginSource.USER)},
            plugin_registrations={
                "user_plug": [
                    _reg("calculator", plugin_key="user_plug", override=True)
                ]
            },
        )
        service._refresh_tool_surface()

        reg = service._registrations["calculator"]
        assert reg.available is False
        assert reg.unavailable_reason == (
            "override not permitted; add plugin key to plugins_override_allowlist"
        )
        # Static definition is still live
        assert tool_service.get_definition("calculator") is static
        # Not in routes
        assert captured_routes
        assert "calculator" not in captured_routes[-1]

    def test_override_false_conflicts_with_static_tool_exact_reason(self):
        """override=False colliding with static -> unavailable with exact reason
        ``conflicts with static tool``."""
        static = _static_def("calculator")
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=_make_settings(),
            manifests_by_key={"p1": _manifest("p1")},
            plugin_registrations={
                "p1": [_reg("calculator", plugin_key="p1", override=False)]
            },
        )
        service._refresh_tool_surface()

        reg = service._registrations["calculator"]
        assert reg.available is False
        assert reg.unavailable_reason == "conflicts with static tool"
        assert tool_service.get_definition("calculator") is static
        assert "calculator" not in captured_routes[-1]

    def test_non_overlapping_plugin_tool_available(self):
        """Plugin tool with no static collision is available and published."""
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[_static_def("calculator")],
            settings=_make_settings(),
            manifests_by_key={"p1": _manifest("p1")},
            plugin_registrations={
                "p1": [_reg("custom_tool", plugin_key="p1")]
            },
        )
        service._refresh_tool_surface()

        reg = service._registrations["custom_tool"]
        assert reg.available is True
        assert tool_service.get_definition("custom_tool") is not None
        assert "custom_tool" in captured_routes[-1]


# ---------------------------------------------------------------------------
# S3: Inter-plugin conflict + restore
# ---------------------------------------------------------------------------


class TestInterPluginConflict:
    """Different plugins / same-plugin duplicate tool names: stable first-available
    wins; rest unavailable with exact reason; override can't bypass; requires_env /
    check_fn unavailable don't preempt; restore on disable / allowlist-removal /
    load failure / override cancellation."""

    def test_different_plugins_same_name_first_wins(self):
        """Two different plugins register the same tool name. First available
        (by plugin load order) wins; second is unavailable with exact reason."""
        service, tool_service, captured_routes, _ = _build_service(
            settings=_make_settings(),
            manifests_by_key={
                "p1": _manifest("p1"),
                "p2": _manifest("p2"),
            },
            plugin_registrations={
                "p1": [_reg("shared", plugin_key="p1", description="from p1")],
                "p2": [_reg("shared", plugin_key="p2", description="from p2")],
            },
        )
        service._refresh_tool_surface()

        # p1 wins (first in load order)
        winner = service._registrations["shared"]
        assert winner.plugin_key == "p1"
        assert winner.available is True
        # p2's registration is unavailable with exact reason
        p2_reg = service._plugin_registrations["p2"][0]
        assert p2_reg.available is False
        assert p2_reg.unavailable_reason == "conflicts with plugin tool from p1"
        # Only one definition published
        defs = [d for d in tool_service.list_definitions() if d.name == "shared"]
        assert len(defs) == 1
        assert defs[0].description == "from p1"
        assert "shared" in captured_routes[-1]

    def test_same_plugin_duplicate_name_first_wins(self):
        """Same plugin registers the same tool name twice. First wins; second
        is unavailable with exact reason referencing the same plugin_key."""
        service, tool_service, *_ = _build_service(
            settings=_make_settings(),
            manifests_by_key={"p1": _manifest("p1")},
            plugin_registrations={
                "p1": [
                    _reg("dup", plugin_key="p1", description="first"),
                    _reg("dup", plugin_key="p1", description="second"),
                ]
            },
        )
        service._refresh_tool_surface()

        winner = service._registrations["dup"]
        assert winner.description == "first"
        assert winner.available is True
        # Second registration unavailable
        second = service._plugin_registrations["p1"][1]
        assert second.available is False
        assert second.unavailable_reason == "conflicts with plugin tool from p1"

    def test_override_cannot_bypass_inter_plugin_conflict(self):
        """override=True does NOT bypass the inter-plugin conflict rule. The
        first available registration still wins; override only affects static
        conflict, not plugin-vs-plugin."""
        static = _static_def("shared")
        service, tool_service, *_ = _build_service(
            static_defs=[static],
            settings=_make_settings(plugins_override_allowlist=["p1", "p2"]),
            manifests_by_key={
                "p1": _manifest("p1", source=PluginSource.USER),
                "p2": _manifest("p2", source=PluginSource.USER),
            },
            plugin_registrations={
                "p1": [_reg("shared", plugin_key="p1", override=True, description="p1 override")],
                "p2": [_reg("shared", plugin_key="p2", override=True, description="p2 override")],
            },
        )
        service._refresh_tool_surface()

        # p1 wins (first in load order); override gate applies to winner only
        winner = service._registrations["shared"]
        assert winner.plugin_key == "p1"
        assert winner.available is True
        # p2 is unavailable due to inter-plugin conflict (NOT static conflict)
        p2_reg = service._plugin_registrations["p2"][0]
        assert p2_reg.available is False
        assert p2_reg.unavailable_reason == "conflicts with plugin tool from p1"
        # p1's override is active -> plugin is live def, static suppressed
        assert tool_service.get_definition("shared").description == "p1 override"

    def test_requires_env_unavailable_does_not_preempt(self):
        """A registration with unsatisfied requires_env is NOT available and does
        NOT preempt a later available registration for the same name."""
        service, tool_service, *_ = _build_service(
            settings=_make_settings(),
            manifests_by_key={
                "p1": _manifest("p1"),
                "p2": _manifest("p2"),
            },
            plugin_registrations={
                "p1": [
                    _reg(
                        "shared",
                        plugin_key="p1",
                        requires_env=[{"name": "MISSING_KEY"}],
                        description="p1 missing env",
                    )
                ],
                "p2": [_reg("shared", plugin_key="p2", description="p2 available")],
            },
        )
        service._refresh_tool_surface()

        # p1 is unavailable (missing env), p2 wins
        p1_reg = service._plugin_registrations["p1"][0]
        assert p1_reg.available is False
        assert "env" in (p1_reg.unavailable_reason or "").lower()
        # p2 is the winner
        winner = service._registrations["shared"]
        assert winner.plugin_key == "p2"
        assert winner.available is True
        defs = [d for d in tool_service.list_definitions() if d.name == "shared"]
        assert len(defs) == 1
        assert defs[0].description == "p2 available"

    def test_check_fn_false_does_not_preempt(self):
        """A registration with check_fn()=False is NOT available and does NOT
        preempt a later available registration for the same name."""
        service, tool_service, *_ = _build_service(
            settings=_make_settings(),
            manifests_by_key={
                "p1": _manifest("p1"),
                "p2": _manifest("p2"),
            },
            plugin_registrations={
                "p1": [
                    _reg(
                        "shared",
                        plugin_key="p1",
                        check_fn=lambda: False,
                        description="p1 check fails",
                    )
                ],
                "p2": [_reg("shared", plugin_key="p2", description="p2 available")],
            },
        )
        service._refresh_tool_surface()

        p1_reg = service._plugin_registrations["p1"][0]
        assert p1_reg.available is False
        assert "check_fn" in (p1_reg.unavailable_reason or "").lower()
        winner = service._registrations["shared"]
        assert winner.plugin_key == "p2"
        assert winner.available is True

    def test_check_fn_exception_does_not_preempt(self):
        """A registration whose check_fn raises is unavailable and does not
        preempt a later available registration."""
        def boom():
            raise RuntimeError("check exploded")

        service, tool_service, *_ = _build_service(
            settings=_make_settings(),
            manifests_by_key={
                "p1": _manifest("p1"),
                "p2": _manifest("p2"),
            },
            plugin_registrations={
                "p1": [_reg("shared", plugin_key="p1", check_fn=boom)],
                "p2": [_reg("shared", plugin_key="p2")],
            },
        )
        service._refresh_tool_surface()

        p1_reg = service._plugin_registrations["p1"][0]
        assert p1_reg.available is False
        assert "check_fn" in (p1_reg.unavailable_reason or "").lower()
        winner = service._registrations["shared"]
        assert winner.plugin_key == "p2"

    def test_all_registrations_unavailable_name_not_published(self):
        """If all registrations for a name are unavailable (requires_env /
        check_fn), the name has no winner and is not published."""
        service, tool_service, captured_routes, _ = _build_service(
            settings=_make_settings(),
            manifests_by_key={
                "p1": _manifest("p1"),
                "p2": _manifest("p2"),
            },
            plugin_registrations={
                "p1": [_reg("shared", plugin_key="p1", requires_env=[{"name": "X"}])],
                "p2": [_reg("shared", plugin_key="p2", check_fn=lambda: False)],
            },
        )
        service._refresh_tool_surface()

        assert "shared" not in service._registrations
        assert tool_service.get_definition("shared") is None
        assert "shared" not in captured_routes[-1]


class TestRestoreOnChanges:
    """Restore builtin route/definition on disable / allowlist-removal / load
    failure / override cancellation."""

    def test_restore_on_disable(self):
        """When a plugin is disabled (removed from _plugin_registrations), the
        next _refresh_tool_surface produces no override_static_names for it,
        restoring the builtin definition + route."""
        static = _static_def("calculator")
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=_make_settings(),
            manifests_by_key={"p1": _manifest("p1", source=PluginSource.BUNDLED)},
            plugin_registrations={
                "p1": [_reg("calculator", plugin_key="p1", override=True, description="plugin")]
            },
        )
        # First scan: override active
        service._refresh_tool_surface()
        assert tool_service.get_definition("calculator").description == "plugin"
        assert "calculator" in captured_routes[-1]

        # Simulate disable: remove plugin from _plugin_registrations
        service._plugin_registrations = {}
        service._refresh_tool_surface()

        # Builtin restored
        assert tool_service.get_definition("calculator") is static
        assert "calculator" not in captured_routes[-1]

    def test_restore_on_allowlist_removal(self):
        """When a USER plugin is removed from the allowlist, its override is no
        longer permitted, so the builtin is restored."""
        static = _static_def("calculator")
        settings = _make_settings(plugins_override_allowlist=["user_plug"])
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=settings,
            manifests_by_key={"user_plug": _manifest("user_plug", source=PluginSource.USER)},
            plugin_registrations={
                "user_plug": [_reg("calculator", plugin_key="user_plug", override=True, description="plugin")]
            },
        )
        # Override active
        service._refresh_tool_surface()
        assert tool_service.get_definition("calculator").description == "plugin"

        # Remove from allowlist
        service._settings.plugins_override_allowlist = []
        service._refresh_tool_surface()

        # Builtin restored; plugin unavailable with exact reason
        assert tool_service.get_definition("calculator") is static
        reg = service._registrations["calculator"]
        assert reg.available is False
        assert reg.unavailable_reason == (
            "override not permitted; add plugin key to plugins_override_allowlist"
        )
        assert "calculator" not in captured_routes[-1]

    def test_restore_on_load_failure(self):
        """When a plugin fails load-register (simulated by removing it from
        _plugin_registrations, same as disable), the builtin is restored."""
        static = _static_def("calculator")
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=_make_settings(),
            manifests_by_key={"p1": _manifest("p1", source=PluginSource.BUNDLED)},
            plugin_registrations={
                "p1": [_reg("calculator", plugin_key="p1", override=True, description="plugin")]
            },
        )
        service._refresh_tool_surface()
        assert tool_service.get_definition("calculator").description == "plugin"

        # Simulate load failure: plugin not admitted -> not in _plugin_registrations
        service._plugin_registrations = {}
        service._refresh_tool_surface()

        assert tool_service.get_definition("calculator") is static
        assert "calculator" not in captured_routes[-1]

    def test_restore_on_override_cancellation(self):
        """When override is cancelled (override=False), the tool is unavailable
        with 'conflicts with static tool', and the builtin is restored."""
        static = _static_def("calculator")
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=_make_settings(),
            manifests_by_key={"p1": _manifest("p1", source=PluginSource.BUNDLED)},
            plugin_registrations={
                "p1": [_reg("calculator", plugin_key="p1", override=True, description="plugin")]
            },
        )
        service._refresh_tool_surface()
        assert tool_service.get_definition("calculator").description == "plugin"

        # Cancel override: change registration to override=False
        service._plugin_registrations["p1"][0].override = False
        service._refresh_tool_surface()

        assert tool_service.get_definition("calculator") is static
        reg = service._registrations["calculator"]
        assert reg.available is False
        assert reg.unavailable_reason == "conflicts with static tool"
        assert "calculator" not in captured_routes[-1]

    def test_empty_plugin_registrations_restores_builtin(self):
        """An empty scan (no plugin registrations) clears all suppression and
        restores builtin definitions."""
        static = _static_def("calculator")
        service, tool_service, captured_routes, _ = _build_service(
            static_defs=[static],
            settings=_make_settings(),
            manifests_by_key={"p1": _manifest("p1", source=PluginSource.BUNDLED)},
            plugin_registrations={
                "p1": [_reg("calculator", plugin_key="p1", override=True, description="plugin")]
            },
        )
        service._refresh_tool_surface()
        assert tool_service.get_definition("calculator").description == "plugin"

        # Empty scan
        service._plugin_registrations = {}
        service._refresh_tool_surface()

        assert tool_service.get_definition("calculator") is static
        # Suppression cleared
        assert tool_service._suppressed_static_names.get("plugin", set()) == set()
        assert captured_routes[-1] == set()


class TestRouteRefreshFailure:
    """On replace_dynamic_definitions failure, keep old tool surface + routes."""

    def test_route_not_refreshed_on_replace_failure(self):
        """If replace_dynamic_definitions raises, routes are NOT refreshed."""
        from unittest.mock import MagicMock

        static = _static_def("calculator")
        tool_service = MagicMock()
        tool_service.list_definitions.return_value = [static]
        tool_service.replace_dynamic_definitions.side_effect = ValueError("boom")

        captured_routes: list[set[str]] = []
        service = PluginService(
            registry=MagicMock(),
            loader=MagicMock(),
            tool_service=tool_service,
            route_refresher=lambda names: captured_routes.append(set(names)),
            settings=_make_settings(),
        )
        service._plugin_registrations = {
            "p1": [_reg("custom_tool", plugin_key="p1")]
        }
        service._manifests_by_key = {"p1": _manifest("p1")}
        service._refresh_tool_surface()

        # replace_dynamic_definitions was called but raised; routes NOT refreshed
        tool_service.replace_dynamic_definitions.assert_called_once()
        assert captured_routes == []
