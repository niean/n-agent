from dataclasses import fields

from app.domain.agent import AgentState
from app.domain.provider import ModelInfo
from app.domain.tool import RiskLevel, ToolDefinition


def test_tool_definition_has_no_handler_field():
    names = {field.name for field in fields(ToolDefinition)}

    assert "handler" not in names


def test_risk_levels_cover_mvp_permissions():
    assert {level.value for level in RiskLevel} == {"safe", "confirm", "dangerous"}


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
