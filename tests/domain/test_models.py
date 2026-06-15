from dataclasses import fields

from app.domain.agent import AgentState
from app.domain.provider import ModelInfo
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType


def test_tool_definition_has_no_handler_field():
    names = {field.name for field in fields(ToolDefinition)}

    assert "handler" not in names


def test_risk_levels_cover_mvp_permissions():
    assert {level.value for level in RiskLevel} == {"safe", "confirm", "dangerous"}


def test_tool_source_types_cover_registered_and_future_sources():
    assert {source.value for source in ToolSourceType} == {
        "builtin",
        "knowledge",
        "skill",
        "mcp",
        "plugin",
        "agent",
    }


def test_tool_definition_defaults_to_builtin_source_and_toolset():
    definition = ToolDefinition("name", "desc", {"type": "object"})

    assert definition.source_type is ToolSourceType.BUILTIN
    assert definition.toolset == "builtin"


def test_tool_definition_preserves_positional_risk_level_argument():
    definition = ToolDefinition("confirm_tool", "confirm", {"type": "object"}, RiskLevel.CONFIRM)

    assert definition.risk_level is RiskLevel.CONFIRM
    assert definition.source_type is ToolSourceType.BUILTIN


def test_agent_state_defaults_iteration_count_to_zero():
    state = AgentState(session_id="session-1")

    assert state.iteration_count == 0


def test_model_info_describes_capabilities():
    model = ModelInfo(
        id="model-a",
        display_name="Model A",
        provider="test",
        supports_tools=True,
        supports_streaming=False,
    )

    assert model.supports_tools is True
    assert model.supports_streaming is False
