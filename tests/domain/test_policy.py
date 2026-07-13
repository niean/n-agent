from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.policy import Policy, PolicyDecision, PolicyOutcome


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

    policy: Policy[str, int] = ExamplePolicy()

    assert policy.evaluate("request").reason == "request:None"

