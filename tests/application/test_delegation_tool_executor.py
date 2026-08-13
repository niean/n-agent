"""Tests for DelegateAgentsToolExecutor + delegate_agents tool definition (T11)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.delegation_request_parser import DelegationError
from app.application.delegation_tool_executor import (
    DelegateAgentsToolExecutor,
    delegate_agent_tool_definitions,
)
from app.domain.delegation import (
    DelegationResultSet,
    DelegationStatus,
    DelegationResult,
    DelegationMemberStatus,
)
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolExecutionContext,
    ToolResultStatus,
    ToolSourceType,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeDelegationService:
    """Captures the delegate call and returns a canned result set."""
    result_set: DelegationResultSet | None = None
    error: DelegationError | None = None
    captured: dict[str, Any] = field(default_factory=dict)

    async def delegate(self, *, parent_capability, delegation_key, children,
                       join_policy, aggregation, timeout_seconds,
                       aggregator_instruction=None) -> DelegationResultSet:
        self.captured = {
            "parent_capability": dict(parent_capability),
            "delegation_key": delegation_key,
            "children": list(children),
            "join_policy": join_policy,
            "aggregation": aggregation,
            "timeout_seconds": timeout_seconds,
            "aggregator_instruction": aggregator_instruction,
        }
        if self.error is not None:
            raise self.error
        if self.result_set is None:
            return DelegationResultSet(
                delegation_id="d1",
                status=DelegationStatus.SUCCEEDED,
                member_results=(
                    DelegationResult(
                        status=DelegationMemberStatus.SUCCEEDED,
                        summary="w0 done",
                    ),
                ),
            )
        return self.result_set


def _capability() -> dict[str, Any]:
    return {
        "source": "task", "scope_id": "t1", "run_id": "r1",
        "session_id": "s1", "actor_id": "user-1",
        "has_capability": True, "classification": "internal",
        "parent_allowed_tools": frozenset({"get_current_time"}),
        "system_child_allowlist": frozenset({"get_current_time"}),
    }


def _ctx(*, capability: dict[str, Any] | None = None,
         run_id="r1", session_id="s1") -> ToolExecutionContext:
    return ToolExecutionContext(
        session_id=session_id,
        run_id=run_id,
        trusted_metadata={"delegation_capability": capability},
        granted_tools=frozenset({"delegate_agents"}),
    )


def _req(**arguments) -> ToolCallRequest:
    base = {
        "delegation_key": "k1",
        "children": [
            {"title": "w0", "instruction": "do work",
             "allowed_tools": ["get_current_time"], "budget_tokens": 100},
        ],
        "join_policy": "all_completed",
        "aggregation": "parent",
        "timeout_seconds": 60,
    }
    base.update(arguments)
    return ToolCallRequest(id="c1", name="delegate_agents", arguments=base)


# ---------------------------------------------------------------------------
# tool definition
# ---------------------------------------------------------------------------


def test_delegate_agents_tool_definition_closed_schema():
    defs = delegate_agent_tool_definitions()
    assert len(defs) == 1
    d = defs[0]
    assert d.name == "delegate_agents"
    assert d.source_type is ToolSourceType.AGENT
    assert d.risk_level is RiskLevel.SAFE
    assert d.managed is False
    assert d.realtime_only is False
    assert d.toolset == "agent"
    # Closed schema.
    assert d.input_schema["additionalProperties"] is False
    # Five required top-level fields.
    required = set(d.input_schema["required"])
    assert required == {
        "delegation_key", "children", "join_policy", "aggregation",
        "timeout_seconds",
    }
    # if/then constraint: aggregation=agent -> aggregator required.
    assert "allOf" in d.input_schema


# ---------------------------------------------------------------------------
# tool executor: capability gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_denies_without_capability():
    svc = FakeDelegationService()
    exe = DelegateAgentsToolExecutor(delegation_service=svc)
    ctx = _ctx(capability=None)  # no capability signed
    result = await exe.execute(_req(), ctx)
    assert result.status is ToolResultStatus.ERROR
    payload = result.content if isinstance(result.content, dict) else {}
    assert payload.get("error") == "delegation_not_authorized"
    # DelegationService was never called.
    assert svc.captured == {}


@pytest.mark.asyncio
async def test_executor_denies_when_capability_has_no_flag():
    svc = FakeDelegationService()
    exe = DelegateAgentsToolExecutor(delegation_service=svc)
    cap = _capability()
    cap["has_capability"] = False
    ctx = _ctx(capability=cap)
    result = await exe.execute(_req(), ctx)
    assert result.status is ToolResultStatus.ERROR
    payload = result.content if isinstance(result.content, dict) else {}
    assert payload.get("error") == "delegation_not_authorized"
    assert svc.captured == {}


# ---------------------------------------------------------------------------
# tool executor: success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_verifies_capability_then_delegates():
    svc = FakeDelegationService()
    exe = DelegateAgentsToolExecutor(delegation_service=svc)
    ctx = _ctx(capability=_capability())
    result = await exe.execute(_req(), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = result.content if isinstance(result.content, dict) else {}
    assert payload["delegation_id"] == "d1"
    assert payload["status"] == "succeeded"
    assert "member_results" in payload
    # Internal session/lease/claim tokens are NOT leaked.
    assert "execution_session_id" not in payload
    assert "claim_lock" not in payload


@pytest.mark.asyncio
async def test_executor_passes_aggregator_instruction_when_agent():
    svc = FakeDelegationService()
    exe = DelegateAgentsToolExecutor(delegation_service=svc)
    ctx = _ctx(capability=_capability())
    await exe.execute(
        _req(
            aggregation="agent",
            aggregator={"instruction": "summarize all results",
                        "allowed_tools": []},
        ),
        ctx,
    )
    assert svc.captured["aggregation"] == "agent"
    assert svc.captured["aggregator_instruction"] == "summarize all results"


# ---------------------------------------------------------------------------
# tool executor: error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_maps_non_retriable_error():
    svc = FakeDelegationService(
        error=DelegationError("delegation_invalid", "bad request")
    )
    exe = DelegateAgentsToolExecutor(delegation_service=svc)
    ctx = _ctx(capability=_capability())
    result = await exe.execute(_req(), ctx)
    assert result.status is ToolResultStatus.ERROR
    payload = result.content if isinstance(result.content, dict) else {}
    assert payload["error"] == "delegation_invalid"
    assert payload.get("retriable") is False


@pytest.mark.asyncio
async def test_executor_maps_retriable_error():
    svc = FakeDelegationService(
        error=DelegationError("delegation_timeout", "timed out")
    )
    exe = DelegateAgentsToolExecutor(delegation_service=svc)
    ctx = _ctx(capability=_capability())
    result = await exe.execute(_req(), ctx)
    assert result.status is ToolResultStatus.ERROR
    payload = result.content if isinstance(result.content, dict) else {}
    assert payload["error"] == "delegation_timeout"
    assert payload.get("retriable") is True


@pytest.mark.asyncio
async def test_executor_returns_partial_flag_when_forced():
    from dataclasses import replace
    rs = DelegationResultSet(
        delegation_id="d2", status=DelegationStatus.FAILED,
        partial=True, partial_reason="deadline",
    )
    svc = FakeDelegationService(result_set=rs)
    exe = DelegateAgentsToolExecutor(delegation_service=svc)
    ctx = _ctx(capability=_capability())
    result = await exe.execute(_req(), ctx)
    assert result.status is ToolResultStatus.SUCCESS
    payload = result.content if isinstance(result.content, dict) else {}
    assert payload["partial"] is True
    assert payload["partial_reason"] == "deadline"
