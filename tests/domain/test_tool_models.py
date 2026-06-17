from __future__ import annotations

from app.domain.tool import (
    RiskLevel,
    ToolDefinition,
    ToolExecutionContext,
)


def test_tool_definition_defaults_managed_false():
    t = ToolDefinition(name="x", description="", input_schema={"type": "object"})
    assert t.managed is False


def test_tool_definition_can_mark_managed():
    t = ToolDefinition(
        name="manage_schedule",
        description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.CONFIRM,
        managed=True,
    )
    assert t.managed is True


def test_tool_execution_context_defaults():
    ctx = ToolExecutionContext()
    assert ctx.allowed_confirm_tools == {}
    assert ctx.session_id is None
    assert ctx.metadata == {}
    assert ctx.trusted_metadata == {}
    assert ctx.execution_context_mode == "realtime"
    assert ctx.permitted_managed_tools == set()


def test_tool_execution_context_carries_trusted_metadata():
    ctx = ToolExecutionContext(
        session_id="s1",
        trusted_metadata={
            "gateway.source_type": "feishu",
            "receive_id": "oc_x",
        },
        permitted_managed_tools={"manage_schedule"},
    )
    assert ctx.trusted_metadata["gateway.source_type"] == "feishu"
    assert "manage_schedule" in ctx.permitted_managed_tools
