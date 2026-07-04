from __future__ import annotations

from app.domain.tool import (
    ApprovalDecision,
    ApprovalRequest,
    RiskLevel,
    ToolExecutionContext,
)


def test_approval_request_frozen():
    req = ApprovalRequest(
        session_id="s1",
        tool_call_id="tc-1",
        tool_name="shell",
        arguments={"cmd": "ls"},
        description="list files",
        risk_level=RiskLevel.CONFIRM,
    )

    assert req.session_id == "s1"
    assert req.tool_call_id == "tc-1"
    assert req.tool_name == "shell"
    assert req.arguments == {"cmd": "ls"}
    assert req.description == "list files"
    assert req.risk_level is RiskLevel.CONFIRM


def test_approval_decision_defaults():
    decision = ApprovalDecision(allowed=True)

    assert decision.allowed is True
    assert decision.scope == "once"
    assert decision.reason == ""


def test_tool_execution_context_with_approval_decider():
    def decider(_req: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(allowed=True)

    ctx = ToolExecutionContext(session_id="s1", approval_decider=decider)

    assert ctx.approval_decider is not None
    assert callable(ctx.approval_decider)
    result = ctx.approval_decider(ApprovalRequest(
        session_id="s1",
        tool_call_id="tc-1",
        tool_name="shell",
        arguments={},
        description="",
        risk_level=RiskLevel.CONFIRM,
    ))
    assert result.allowed is True


def test_tool_execution_context_default_approval_decider_is_none():
    ctx = ToolExecutionContext()

    assert ctx.approval_decider is None
