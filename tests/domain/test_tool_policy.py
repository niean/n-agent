from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from app.domain.policy import Policy, PolicyDecision, PolicyOutcome
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolSourceType,
)
from app.domain.tool_policy import ToolExposurePolicy, ToolPolicy, ToolPolicyRequest


def definition(
    *,
    name: str = "tool",
    risk: RiskLevel = RiskLevel.CONFIRM,
    enabled: bool = True,
    source: ToolSourceType = ToolSourceType.BUILTIN,
    managed: bool = False,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="test tool",
        input_schema={"type": "object"},
        risk_level=risk,
        enabled=enabled,
        source_type=source,
        managed=managed,
    )


def request(*, name: str = "tool", arguments: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest(id="call-1", name=name, arguments={} if arguments is None else arguments)


def assert_decision(
    actual: PolicyDecision,
    outcome: PolicyOutcome,
    reason: str,
) -> None:
    assert actual == PolicyDecision(outcome=outcome, reason=reason)


@pytest.mark.parametrize(
    ("tool_definition", "exposure", "expected"),
    [
        (definition(enabled=False), ToolExposurePolicy.DEFAULT, False),
        (definition(enabled=False), ToolExposurePolicy.SAFE_ONLY, False),
        (definition(risk=RiskLevel.DANGEROUS), ToolExposurePolicy.DEFAULT, False),
        (definition(risk=RiskLevel.DANGEROUS), ToolExposurePolicy.SAFE_ONLY, False),
        (definition(risk=RiskLevel.CONFIRM), ToolExposurePolicy.DEFAULT, True),
        (definition(risk=RiskLevel.CONFIRM), ToolExposurePolicy.SAFE_ONLY, False),
        (
            definition(risk=RiskLevel.SAFE, source=ToolSourceType.AGENT),
            ToolExposurePolicy.DEFAULT,
            True,
        ),
        (
            definition(risk=RiskLevel.SAFE, source=ToolSourceType.AGENT),
            ToolExposurePolicy.SAFE_ONLY,
            False,
        ),
        (definition(risk=RiskLevel.SAFE), ToolExposurePolicy.DEFAULT, True),
        (definition(risk=RiskLevel.SAFE), ToolExposurePolicy.SAFE_ONLY, True),
    ],
)
def test_can_expose_matrix(tool_definition, exposure, expected):
    assert ToolPolicy().can_expose(tool_definition, exposure) is expected


def test_can_expose_safe_only_grants_expose_named_safe_agent_tool():
    policy = ToolPolicy()
    agent_safe = definition(risk=RiskLevel.SAFE, source=ToolSourceType.AGENT, name="host_terminal")
    assert policy.can_expose(agent_safe, ToolExposurePolicy.SAFE_ONLY, frozenset({"host_terminal"})) is True
    # not granted -> still hidden under SAFE_ONLY
    assert policy.can_expose(agent_safe, ToolExposurePolicy.SAFE_ONLY, frozenset()) is False
    assert policy.can_expose(agent_safe, ToolExposurePolicy.SAFE_ONLY, frozenset({"other_tool"})) is False


@pytest.mark.parametrize("risk", [RiskLevel.CONFIRM, RiskLevel.DANGEROUS])
def test_can_expose_grant_does_not_lift_confirm_or_dangerous_gating(risk):
    policy = ToolPolicy()
    defn = definition(risk=risk, source=ToolSourceType.AGENT, name="host_terminal")
    # granted but not SAFE -> still hidden (unattended has no approval channel)
    assert policy.can_expose(defn, ToolExposurePolicy.SAFE_ONLY, frozenset({"host_terminal"})) is False


@pytest.mark.parametrize("unknown", [None, "default", RiskLevel.SAFE, object()])
def test_can_expose_fails_closed_for_non_exposure_enum_values(unknown):
    assert ToolPolicy().can_expose(definition(risk=RiskLevel.SAFE), unknown) is False


def test_can_expose_safe_only_exposes_permitted_managed_tool():
    """Managed CONFIRM tools (e.g. task_*) must be exposed to unattended
    workers when the server-side executor declares them in
    permitted_managed_tools; otherwise task workers see no task tools."""
    policy = ToolPolicy()
    managed = definition(managed=True, name="task_complete")
    # permitted -> exposed even under SAFE_ONLY
    assert policy.can_expose(
        managed, ToolExposurePolicy.SAFE_ONLY,
        frozenset(), frozenset({"task_complete"}),
    ) is True
    # not permitted -> hidden
    assert policy.can_expose(
        managed, ToolExposurePolicy.SAFE_ONLY,
        frozenset(), frozenset(),
    ) is False
    assert policy.can_expose(
        managed, ToolExposurePolicy.SAFE_ONLY,
        frozenset(), frozenset({"other_tool"}),
    ) is False


def test_can_expose_safe_only_permitted_managed_does_not_lift_non_managed_confirm():
    """permitted_managed_tools must never expose a non-managed CONFIRM tool;
    only managed tools are lifted by the server-side declaration."""
    policy = ToolPolicy()
    confirm = definition(risk=RiskLevel.CONFIRM, managed=False, name="task_complete")
    assert policy.can_expose(
        confirm, ToolExposurePolicy.SAFE_ONLY,
        frozenset(), frozenset({"task_complete"}),
    ) is False


def test_tool_policy_implements_shared_policy_contract_and_request_is_frozen():
    tool_request = ToolPolicyRequest(definition(risk=RiskLevel.SAFE), request())
    policy: Policy[ToolPolicyRequest, ToolExecutionContext, PolicyDecision] = ToolPolicy()

    assert_decision(policy.evaluate(tool_request), PolicyOutcome.ALLOW, "safe_tool")
    with pytest.raises(FrozenInstanceError):
        tool_request.request = request(name="other")  # type: ignore[misc]


@pytest.mark.parametrize(
    ("tool_definition", "context", "outcome", "reason"),
    [
        (
            definition(enabled=False),
            ToolExecutionContext(
                allowed_confirm_tools={"tool": "session"},
                permitted_managed_tools={"tool"},
            ),
            PolicyOutcome.DENY,
            "tool_disabled",
        ),
        (
            definition(risk=RiskLevel.DANGEROUS),
            ToolExecutionContext(
                allowed_confirm_tools={"tool": "session"},
                permitted_managed_tools={"tool"},
            ),
            PolicyOutcome.DENY,
            "dangerous_tool",
        ),
        (definition(risk=RiskLevel.SAFE), None, PolicyOutcome.ALLOW, "safe_tool"),
        (
            definition(managed=True),
            ToolExecutionContext(permitted_managed_tools={"tool"}),
            PolicyOutcome.ALLOW,
            "managed_grant",
        ),
        (
            definition(managed=True),
            ToolExecutionContext(allowed_confirm_tools={"tool": "session"}),
            PolicyOutcome.REQUIRE_APPROVAL,
            "managed_approval_required",
        ),
        (
            definition(),
            ToolExecutionContext(allowed_confirm_tools={"tool": "session"}),
            PolicyOutcome.ALLOW,
            "session_grant",
        ),
        (
            definition(),
            ToolExecutionContext(allowed_confirm_tools={"tool": {"path": "/tmp"}}),
            PolicyOutcome.ALLOW,
            "argument_grant",
        ),
        (
            definition(),
            ToolExecutionContext(allowed_confirm_tools={"tool": {"path": "/other"}}),
            PolicyOutcome.REQUIRE_APPROVAL,
            "confirm_approval_required",
        ),
        (
            definition(),
            ToolExecutionContext(allowed_confirm_tools={"tool": {}}),
            PolicyOutcome.REQUIRE_APPROVAL,
            "confirm_approval_required",
        ),
        (definition(), None, PolicyOutcome.REQUIRE_APPROVAL, "confirm_approval_required"),
    ],
)
def test_execution_decision_matrix(tool_definition, context, outcome, reason):
    arguments = {"path": "/tmp", "nested": {"value": 1}}

    assert_decision(
        ToolPolicy().evaluate_execution(tool_definition, request(arguments=arguments), context),
        outcome,
        reason,
    )


def test_argument_grants_match_top_level_constraints_and_nested_values_whole():
    tool_definition = definition()
    call = request(arguments={"path": "/tmp", "nested": {"value": 1}, "extra": True})

    matching = ToolExecutionContext(
        allowed_confirm_tools={"tool": {"path": "/tmp", "nested": {"value": 1}}}
    )
    nested_subset = ToolExecutionContext(
        allowed_confirm_tools={"tool": {"nested": {"value": 1, "extra": True}}}
    )

    assert_decision(
        ToolPolicy().evaluate_execution(tool_definition, call, matching),
        PolicyOutcome.ALLOW,
        "argument_grant",
    )
    assert_decision(
        ToolPolicy().evaluate_execution(tool_definition, call, nested_subset),
        PolicyOutcome.REQUIRE_APPROVAL,
        "confirm_approval_required",
    )


def test_empty_argument_grant_only_matches_empty_arguments():
    context = ToolExecutionContext(allowed_confirm_tools={"tool": {}})

    assert_decision(
        ToolPolicy().evaluate_execution(definition(), request(arguments={}), context),
        PolicyOutcome.ALLOW,
        "argument_grant",
    )
    assert_decision(
        ToolPolicy().evaluate_execution(definition(), request(arguments={"x": 1}), context),
        PolicyOutcome.REQUIRE_APPROVAL,
        "confirm_approval_required",
    )


@pytest.mark.parametrize(
    "grant",
    [None, True, 1, "once", [], {1: "invalid"}],
)
def test_invalid_confirm_grants_are_treated_as_no_authorization(grant):
    context = ToolExecutionContext(allowed_confirm_tools={"tool": grant})

    assert_decision(
        ToolPolicy().evaluate_execution(definition(), request(arguments={1: "invalid"}), context),
        PolicyOutcome.REQUIRE_APPROVAL,
        "confirm_approval_required",
    )


def test_execution_ignores_non_authorization_context_fields():
    context = ToolExecutionContext(
        session_id="session-1",
        metadata={"allowed_confirm_tools": {"tool": "session"}},
        trusted_metadata={"permitted_managed_tools": ["tool"]},
        enabled_override=["tool"],
        approval_decider=lambda _: object(),  # type: ignore[arg-type,return-value]
    )

    assert_decision(
        ToolPolicy().evaluate_execution(definition(), request(), context),
        PolicyOutcome.REQUIRE_APPROVAL,
        "confirm_approval_required",
    )


@pytest.mark.parametrize(
    "tool_definition",
    [
        definition(risk=RiskLevel.SAFE, managed=True),
        definition(risk=RiskLevel.DANGEROUS, managed=True),
    ],
)
def test_validate_definition_rejects_managed_non_confirm(tool_definition):
    with pytest.raises(ValueError, match="managed"):
        ToolPolicy().validate_definition(tool_definition)


def test_validate_definition_accepts_managed_confirm():
    assert ToolPolicy().validate_definition(definition(managed=True)) is None


def test_authorize_once_copies_argument_grant_without_mutating_inputs():
    metadata = {"keep": []}
    trusted_metadata = {"keep": []}
    enabled_override = ["tool"]
    allowed = {"other": {"x": 1}}
    permitted = {"managed_other"}
    context = ToolExecutionContext(
        allowed_confirm_tools=allowed,
        permitted_managed_tools=permitted,
        metadata=metadata,
        trusted_metadata=trusted_metadata,
        enabled_override=enabled_override,
    )
    arguments = {"path": "/tmp", "nested": {"value": 1}}
    call = request(arguments=arguments)

    authorized = ToolPolicy().authorize_once(definition(), call, context)

    assert authorized is not context
    assert authorized.allowed_confirm_tools == {
        "other": {"x": 1},
        "tool": {"path": "/tmp", "nested": {"value": 1}},
    }
    assert authorized.allowed_confirm_tools is not allowed
    assert authorized.allowed_confirm_tools["tool"] is not arguments
    assert authorized.permitted_managed_tools == permitted
    assert authorized.permitted_managed_tools is not permitted
    assert authorized.metadata is metadata
    assert authorized.trusted_metadata is trusted_metadata
    assert authorized.enabled_override is enabled_override
    assert context.allowed_confirm_tools == {"other": {"x": 1}}
    assert context.permitted_managed_tools == {"managed_other"}

    arguments["later"] = True
    assert "later" not in authorized.allowed_confirm_tools["tool"]


def test_authorize_once_managed_tool_only_adds_managed_grant():
    context = ToolExecutionContext(
        allowed_confirm_tools={"other": "session"},
        permitted_managed_tools={"managed_other"},
    )

    authorized = ToolPolicy().authorize_once(definition(managed=True), request(), context)

    assert authorized.allowed_confirm_tools == {"other": "session"}
    assert authorized.allowed_confirm_tools is not context.allowed_confirm_tools
    assert authorized.permitted_managed_tools == {"managed_other", "tool"}
    assert authorized.permitted_managed_tools is not context.permitted_managed_tools


@pytest.mark.parametrize("managed", [False, True])
def test_authorize_once_filters_invalid_existing_confirm_grants(managed):
    valid_arguments = {"path": "/tmp"}
    allowed = {
        "valid_session": "session",
        "valid_arguments": valid_arguments,
        "": "session",
        1: "session",
        "bad_scope": "once",
        "bad_value": object(),
        "bad_argument_key": {1: "value"},
    }
    context = ToolExecutionContext(allowed_confirm_tools=allowed)  # type: ignore[arg-type]

    authorized = ToolPolicy().authorize_once(definition(managed=managed), request(), context)

    expected = {
        "valid_session": "session",
        "valid_arguments": {"path": "/tmp"},
    }
    if not managed:
        expected["tool"] = {}
    assert authorized.allowed_confirm_tools == expected
    assert authorized.allowed_confirm_tools["valid_arguments"] is not valid_arguments
    assert context.allowed_confirm_tools is allowed
    assert context.allowed_confirm_tools == allowed


def test_authorize_once_accepts_missing_context_as_empty_context():
    authorized = ToolPolicy().authorize_once(definition(), request(arguments={"x": 1}))

    assert authorized.allowed_confirm_tools == {"tool": {"x": 1}}
    assert authorized.permitted_managed_tools == set()


@pytest.mark.parametrize(
    ("tool_definition", "call", "context"),
    [
        (definition(name="definition"), request(name="request"), None),
        (definition(enabled=False), request(), None),
        (definition(risk=RiskLevel.SAFE), request(), None),
        (definition(risk=RiskLevel.DANGEROUS), request(), None),
        (definition(risk="unknown"), request(), None),  # type: ignore[arg-type]
        (
            definition(),
            request(),
            ToolExecutionContext(allowed_confirm_tools={"tool": "session"}),
        ),
        (
            definition(managed=True),
            request(),
            ToolExecutionContext(permitted_managed_tools={"tool"}),
        ),
        (definition(), request(), object()),
    ],
)
def test_authorize_once_rejects_inputs_that_do_not_currently_require_approval(
    tool_definition,
    call,
    context,
):
    with pytest.raises(ValueError):
        ToolPolicy().authorize_once(tool_definition, call, context)
