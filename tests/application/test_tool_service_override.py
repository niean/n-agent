"""Tests for ToolService.replace_dynamic_definitions atomic override API (T5).

Covers:
- S1: Atomicity -- validation of the entire candidate batch before mutation;
  override_static_names must have same-name candidates; per-source suppression
  isolation; set_dynamic_definitions backward compat (empty suppression).
- S2: All query surfaces (list_definitions / get_definition /
  build_filtered_definitions / list_openai_tools / execute) resolve overridden
  static names to the plugin definition only; static dict not mutated; empty
  defs + empty suppression restores builtin; no duplicate definition at once.
"""
from __future__ import annotations

import pytest

from app.application.tool_service import ToolService
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


class RecordingExecutor:
    def __init__(self):
        self.calls: list[ToolCallRequest] = []

    async def execute(self, request: ToolCallRequest, context=None) -> ToolResult:
        self.calls.append(request)
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


def _definition(
    name: str,
    *,
    description: str = "description",
    risk_level: RiskLevel = RiskLevel.SAFE,
    source_type: ToolSourceType = ToolSourceType.BUILTIN,
    enabled: bool = True,
    managed: bool = False,
    input_schema=None,
    toolset: str = "builtin",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object"} if input_schema is None else input_schema,
        risk_level=risk_level,
        source_type=source_type,
        enabled=enabled,
        managed=managed,
        toolset=toolset,
    )


# ---------------------------------------------------------------------------
# S1: Atomicity -- no mutation on validation failure
# ---------------------------------------------------------------------------


class TestReplaceDynamicDefinitionsAtomicity:
    """replace_dynamic_definitions validates the entire candidate batch before
    mutating any state. On failure, old definitions and suppression are preserved."""

    def test_policy_validation_failure_preserves_old_definitions_and_suppression(self):
        static_calc = _definition("calculator", description="builtin", toolset="math")
        old_plugin = _definition(
            "calculator",
            description="old plugin",
            source_type=ToolSourceType.PLUGIN,
            toolset="plugin",
        )
        service = ToolService(RecordingExecutor(), [static_calc])
        service.replace_dynamic_definitions(
            "plugin", [old_plugin], override_static_names={"calculator"}
        )
        assert service.get_definition("calculator") is old_plugin

        # Attempt to replace with an invalid batch (managed=True + SAFE risk)
        with pytest.raises(ValueError, match="managed"):
            service.replace_dynamic_definitions(
                "plugin",
                [
                    _definition(
                        "new_tool",
                        source_type=ToolSourceType.PLUGIN,
                        managed=True,
                    ),
                ],
                override_static_names=set(),
            )

        # Old state preserved -- no partial mutation
        assert service.get_definition("calculator") is old_plugin
        assert service.dynamic_definitions["plugin"] == {"calculator": old_plugin}
        assert service._suppressed_static_names["plugin"] == {"calculator"}

    def test_override_name_without_same_name_candidate_raises_and_does_not_mutate(self):
        static_calc = _definition("calculator", description="builtin", toolset="math")
        service = ToolService(RecordingExecutor(), [static_calc])

        with pytest.raises(ValueError, match="override_static_names"):
            service.replace_dynamic_definitions(
                "plugin",
                [_definition("other_tool", source_type=ToolSourceType.PLUGIN)],
                override_static_names={"calculator"},  # no candidate named "calculator"
            )

        # No mutation at all
        assert service.get_definition("calculator") is static_calc
        assert "plugin" not in service.dynamic_definitions
        assert "plugin" not in service._suppressed_static_names

    def test_override_name_without_candidate_preserves_existing_override(self):
        static_calc = _definition("calculator", description="builtin", toolset="math")
        old_plugin = _definition(
            "calculator",
            description="old plugin",
            source_type=ToolSourceType.PLUGIN,
        )
        service = ToolService(RecordingExecutor(), [static_calc])
        service.replace_dynamic_definitions(
            "plugin", [old_plugin], override_static_names={"calculator"}
        )

        with pytest.raises(ValueError, match="override_static_names"):
            service.replace_dynamic_definitions(
                "plugin",
                [_definition("other", source_type=ToolSourceType.PLUGIN)],
                override_static_names={"calculator"},  # no candidate "calculator"
            )

        # Old override preserved
        assert service.get_definition("calculator") is old_plugin
        assert service._suppressed_static_names["plugin"] == {"calculator"}

    def test_per_source_suppression_isolated(self):
        static_calc = _definition("calculator", description="builtin", toolset="math")
        static_time = _definition(
            "get_current_time", description="builtin", toolset="system"
        )
        plugin_calc = _definition(
            "calculator",
            description="plugin calc",
            source_type=ToolSourceType.PLUGIN,
        )
        plugin_time = _definition(
            "get_current_time",
            description="plugin time",
            source_type=ToolSourceType.PLUGIN,
        )
        service = ToolService(RecordingExecutor(), [static_calc, static_time])

        # Source A overrides calculator
        service.replace_dynamic_definitions(
            "plugin-a", [plugin_calc], override_static_names={"calculator"}
        )
        # Source B overrides get_current_time
        service.replace_dynamic_definitions(
            "plugin-b", [plugin_time], override_static_names={"get_current_time"}
        )

        # Both overrides visible
        assert service.get_definition("calculator") is plugin_calc
        assert service.get_definition("get_current_time") is plugin_time

        # Removing source A restores calculator builtin, source B intact
        service.replace_dynamic_definitions("plugin-a", [], override_static_names=None)
        assert service.get_definition("calculator") is static_calc
        assert service.get_definition("get_current_time") is plugin_time
        assert service._suppressed_static_names.get("plugin-a") == set()

    def test_set_dynamic_definitions_backward_compatible_with_empty_suppression(self):
        static_calc = _definition("calculator", description="builtin", toolset="math")
        plugin_other = _definition(
            "other_tool",
            description="plugin",
            source_type=ToolSourceType.PLUGIN,
        )
        service = ToolService(RecordingExecutor(), [static_calc])

        # set_dynamic_definitions == replace with override_static_names=None
        service.set_dynamic_definitions("plugin", [plugin_other])

        assert service.get_definition("other_tool") is plugin_other
        assert service.get_definition("calculator") is static_calc
        # No suppression
        assert service._suppressed_static_names["plugin"] == set()

    def test_set_dynamic_definitions_drops_dynamic_shadowing_static_without_override(self):
        """Backward compat: dynamic def with same name as static is dropped
        when not declared in override_static_names (static wins)."""
        static_calc = _definition("calculator", description="builtin", toolset="math")
        shadow = _definition(
            "calculator",
            description="shadow",
            source_type=ToolSourceType.PLUGIN,
        )
        service = ToolService(RecordingExecutor(), [static_calc])

        service.set_dynamic_definitions("plugin", [shadow])

        # Static wins (shadow dropped, no suppression)
        assert service.get_definition("calculator") is static_calc
        assert service._suppressed_static_names["plugin"] == set()
        assert "calculator" not in service.dynamic_definitions.get("plugin", {})


# ---------------------------------------------------------------------------
# S2: All query surfaces resolve override to plugin only
# ---------------------------------------------------------------------------


def _service_with_override():
    static_calc = _definition(
        "calculator",
        description="builtin calc",
        risk_level=RiskLevel.SAFE,
        toolset="math",
    )
    static_time = _definition(
        "get_current_time",
        description="builtin time",
        risk_level=RiskLevel.SAFE,
        toolset="system",
    )
    plugin_calc = _definition(
        "calculator",
        description="plugin calc",
        risk_level=RiskLevel.CONFIRM,
        source_type=ToolSourceType.PLUGIN,
        toolset="plugin",
    )
    service = ToolService(RecordingExecutor(), [static_calc, static_time])
    service.replace_dynamic_definitions(
        "plugin", [plugin_calc], override_static_names={"calculator"}
    )
    return service, static_calc, static_time, plugin_calc


class TestOverrideQueryResolution:
    """After commit, all query surfaces resolve overridden static names to the
    plugin definition only. No duplicate (static+plugin) at the same time."""

    def test_list_definitions_resolves_override_to_plugin_only(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        defs = {d.name: d for d in service.list_definitions()}

        # calculator is plugin version, not static
        assert defs["calculator"] is plugin_calc
        assert defs["calculator"] is not static_calc
        # get_current_time is still static
        assert defs["get_current_time"] is static_time
        # No duplicate calculator
        calc_count = sum(
            1 for d in service.list_definitions() if d.name == "calculator"
        )
        assert calc_count == 1

    def test_get_definition_resolves_override_to_plugin_only(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        assert service.get_definition("calculator") is plugin_calc
        assert service.get_definition("calculator") is not static_calc
        assert service.get_definition("get_current_time") is static_time

    def test_build_filtered_definitions_resolves_override_to_plugin_only(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        # No filter -- plugin calc visible
        defs = {d.name: d for d in service.build_filtered_definitions()}
        assert defs["calculator"] is plugin_calc

        # Filter by toolset "math" (static calc's toolset) -- plugin calc has
        # toolset="plugin" so it should NOT appear
        math_defs = service.build_filtered_definitions(allow_toolsets={"math"})
        assert all(d.name != "calculator" for d in math_defs)

        # Filter by toolset "plugin" -- plugin calc appears
        plugin_defs = service.build_filtered_definitions(allow_toolsets={"plugin"})
        assert any(d is plugin_calc for d in plugin_defs)

    def test_list_openai_tools_resolves_override_to_plugin_only(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        # Static calc is SAFE, plugin calc is CONFIRM.
        # list_openai_tools() (default = DEFAULT exposure) includes CONFIRM.
        schemas = service.list_openai_tools()
        calc_schemas = [s for s in schemas if s["function"]["name"] == "calculator"]
        assert len(calc_schemas) == 1
        assert calc_schemas[0]["function"]["description"] == "plugin calc"

        # SAFE only -- plugin calc (CONFIRM) not visible; static time (SAFE) is
        safe_schemas = service.list_openai_tools(risk_level=RiskLevel.SAFE)
        safe_names = {s["function"]["name"] for s in safe_schemas}
        assert "calculator" not in safe_names
        assert "get_current_time" in safe_names

    @pytest.mark.asyncio
    async def test_execute_resolves_override_to_plugin_only(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        # Plugin calc is CONFIRM -- execute without approval is denied.
        # (Static calc is SAFE and would be allowed -- this proves the plugin
        # definition is the one being used.)
        result = await service.execute(
            ToolCallRequest(id="1", name="calculator", arguments={})
        )
        assert result.status is ToolResultStatus.PERMISSION_DENIED
        assert result.content["reason"] == "confirm_approval_required"

        # With session approval, execute succeeds
        ctx = ToolExecutionContext(
            allowed_confirm_tools={"calculator": "session"},
            session_id="s1",
        )
        result = await service.execute(
            ToolCallRequest(id="2", name="calculator", arguments={}),
            ctx,
        )
        assert result.status is ToolResultStatus.SUCCESS

    def test_static_definitions_dict_not_mutated_by_override(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        # Static dict still contains the original static calc (not deleted)
        assert "calculator" in service.definitions
        assert service.definitions["calculator"] is static_calc

    def test_empty_definitions_and_suppression_restores_builtin(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        # Override is active
        assert service.get_definition("calculator") is plugin_calc

        # Restore: empty defs + no override
        service.replace_dynamic_definitions("plugin", [], override_static_names=None)

        # Builtin restored
        assert service.get_definition("calculator") is static_calc
        assert service._suppressed_static_names["plugin"] == set()
        defs = {d.name: d for d in service.list_definitions()}
        assert defs["calculator"] is static_calc

    def test_no_duplicate_definition_at_same_time(self):
        service, static_calc, static_time, plugin_calc = _service_with_override()

        # list_definitions has exactly one "calculator"
        calc_defs = [d for d in service.list_definitions() if d.name == "calculator"]
        assert len(calc_defs) == 1
        assert calc_defs[0] is plugin_calc

        # list_openai_tools has exactly one "calculator"
        schemas = service.list_openai_tools()
        calc_schemas = [s for s in schemas if s["function"]["name"] == "calculator"]
        assert len(calc_schemas) == 1

    def test_override_replaces_then_updates_then_restores_lifecycle(self):
        """Full lifecycle: override -> update override -> restore builtin."""
        static_calc = _definition(
            "calculator",
            description="builtin",
            risk_level=RiskLevel.SAFE,
            toolset="math",
        )
        service = ToolService(RecordingExecutor(), [static_calc])

        # 1. Override with v1
        v1 = _definition(
            "calculator",
            description="plugin v1",
            source_type=ToolSourceType.PLUGIN,
        )
        service.replace_dynamic_definitions(
            "plugin", [v1], override_static_names={"calculator"}
        )
        assert service.get_definition("calculator") is v1

        # 2. Update override with v2
        v2 = _definition(
            "calculator",
            description="plugin v2",
            source_type=ToolSourceType.PLUGIN,
        )
        service.replace_dynamic_definitions(
            "plugin", [v2], override_static_names={"calculator"}
        )
        assert service.get_definition("calculator") is v2
        assert service.dynamic_definitions["plugin"] == {"calculator": v2}

        # 3. Restore builtin
        service.replace_dynamic_definitions("plugin", [], override_static_names=None)
        assert service.get_definition("calculator") is static_calc
        assert service.dynamic_definitions["plugin"] == {}
        assert service._suppressed_static_names["plugin"] == set()
