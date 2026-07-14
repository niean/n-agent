from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from app.domain.policy import (
    ExecutionMode,
    Policy,
    PolicyAuditEvent,
    PolicyAuditSink,
    PolicyDecision,
    PolicyDecisionKind,
    PolicyOutcome,
    RunPolicyContext,
)


def test_policy_outcomes_have_stable_wire_values():
    assert PolicyOutcome.ALLOW.value == "allow"
    assert PolicyOutcome.DENY.value == "deny"
    assert PolicyOutcome.REQUIRE_APPROVAL.value == "require_approval"


def test_policy_decision_is_frozen_and_requires_a_reason():
    decision = PolicyDecision(PolicyOutcome.ALLOW, "stable_reason")

    with pytest.raises(FrozenInstanceError):
        decision.reason = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="reason"):
        PolicyDecision(PolicyOutcome.DENY, "")


def test_policy_protocol_describes_a_generic_evaluate_contract():
    class ExamplePolicy:
        def evaluate(self, request: str, context: int | None = None) -> PolicyDecision:
            return PolicyDecision(PolicyOutcome.ALLOW, f"{request}:{context}")

    policy: Policy[str, int, PolicyDecision] = ExamplePolicy()

    assert policy.evaluate("request").reason == "request:None"


def test_policy_decision_constructor_remains_backward_compatible():
    decision = PolicyDecision(PolicyOutcome.ALLOW, "safe_tool")
    assert decision.outcome is PolicyOutcome.ALLOW
    assert decision.reason == "safe_tool"
    assert decision.policy == "unknown"
    assert decision.version == "unknown"


def test_policy_decision_carries_policy_and_version_fields():
    decision = PolicyDecision(
        PolicyOutcome.DENY, "tool_disabled",
        policy="tool", version="tool-v1",
    )
    assert decision.policy == "tool"
    assert decision.version == "tool-v1"


def test_policy_decision_kind_has_stable_wire_values():
    assert PolicyDecisionKind.ADMISSION.value == "admission"
    assert PolicyDecisionKind.PLAN.value == "plan"
    assert PolicyDecisionKind.SELECTION.value == "selection"
    assert PolicyDecisionKind.ALLOCATION.value == "allocation"


def test_execution_mode_has_stable_wire_values():
    assert ExecutionMode.REALTIME.value == "realtime"
    assert ExecutionMode.UNATTENDED.value == "unattended"
    assert ExecutionMode.DELEGATED.value == "delegated"


def test_run_policy_context_stores_trusted_claims_as_immutable_mapping():
    original: dict[str, Any] = {"tenant": "t1", "role": "agent"}
    ctx = RunPolicyContext(
        run_id="run-1",
        session_id="s1",
        source="cli",
        actor_id="user-1",
        execution_mode=ExecutionMode.REALTIME,
        trusted_claims=original,
    )
    # Mutating the original dict does not affect the context
    original["tenant"] = "t2"
    assert ctx.trusted_claims["tenant"] == "t1"
    # The stored mapping is immutable
    with pytest.raises(TypeError):
        ctx.trusted_claims["tenant"] = "t3"  # type: ignore[index]


def test_run_policy_context_defaults_policy_scope_to_system():
    ctx = RunPolicyContext(
        run_id="run-1",
        session_id="s1",
        source="cli",
        actor_id=None,
        execution_mode=ExecutionMode.UNATTENDED,
        trusted_claims={},
    )
    assert ctx.policy_scope == "system"


def test_run_policy_context_is_frozen():
    ctx = RunPolicyContext(
        run_id="run-1",
        session_id="s1",
        source="cli",
        actor_id=None,
        execution_mode=ExecutionMode.REALTIME,
        trusted_claims={},
    )
    with pytest.raises(FrozenInstanceError):
        ctx.run_id = "run-2"  # type: ignore[misc]


def test_non_admission_audit_event_does_not_fake_allow():
    event = PolicyAuditEvent(
        policy="context",
        version="system-v1",
        decision_kind=PolicyDecisionKind.PLAN,
        reason="compression_not_required",
        run_id="run-1",
        session_id="s1",
        policy_scope="system",
        outcome=None,
    )
    assert event.outcome is None


def test_policy_audit_event_admission_records_outcome():
    event = PolicyAuditEvent(
        policy="tool",
        version="tool-v1",
        decision_kind=PolicyDecisionKind.ADMISSION,
        reason="safe_tool",
        run_id="run-1",
        session_id="s1",
        policy_scope="system",
        outcome=PolicyOutcome.ALLOW,
    )
    assert event.outcome is PolicyOutcome.ALLOW


def test_policy_audit_event_is_frozen():
    event = PolicyAuditEvent(
        policy="tool",
        version="tool-v1",
        decision_kind=PolicyDecisionKind.ADMISSION,
        reason="safe_tool",
        run_id="run-1",
        session_id="s1",
        policy_scope="system",
    )
    with pytest.raises(FrozenInstanceError):
        event.policy = "other"  # type: ignore[misc]


def test_policy_protocol_supports_typed_non_admission_decision():
    """A Policy can return a domain-specific typed decision, not just PolicyDecision."""

    class ContextPlan:
        def __init__(self, kept: int) -> None:
            self.kept = kept

    class ContextPolicy:
        def evaluate(self, request: str, context: int | None = None) -> ContextPlan:
            return ContextPlan(len(request))

    policy: Policy[str, int, ContextPlan] = ContextPolicy()
    result = policy.evaluate("hello")
    assert result.kept == 5


def test_policy_audit_event_rejects_empty_reason():
    with pytest.raises(ValueError, match="reason"):
        PolicyAuditEvent(
            policy="tool",
            version="tool-v1",
            decision_kind=PolicyDecisionKind.ADMISSION,
            reason="",
            run_id="run-1",
            session_id="s1",
            policy_scope="system",
        )


def test_policy_audit_sink_async_protocol_conformance():
    class Sink:
        async def record(self, event: PolicyAuditEvent) -> None:
            pass

    sink: PolicyAuditSink = Sink()
    assert hasattr(sink, "record")

