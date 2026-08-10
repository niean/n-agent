from dataclasses import FrozenInstanceError

import pytest

from app.application.tool_service import (
    ToolNotFoundError,
    ToolService,
    builtin_tool_definitions,
    knowledge_tool_definitions,
    schedule_tool_definitions,
)
from app.domain.policy import PolicyDecision, PolicyOutcome
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)
from app.domain.tool_policy import ToolExposurePolicy, ToolPolicy


class FakeExecutor:
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


class RecordingExecutor:
    def __init__(self):
        self.calls: list[ToolCallRequest] = []

    async def execute(self, request: ToolCallRequest, context=None) -> ToolResult:
        self.calls.append(request)
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


def _managed_def() -> ToolDefinition:
    return ToolDefinition(
        name="manage_schedule",
        description="",
        input_schema={
            "type": "object",
            "properties": {"action": {"type": "string"}},
        },
        risk_level=RiskLevel.CONFIRM,
        source_type=ToolSourceType.AGENT,
        toolset="schedule",
        managed=True,
    )


@pytest.mark.asyncio
async def test_tool_service_lists_openai_tools_and_executes_safe_tools():
    service = ToolService(FakeExecutor(), builtin_tool_definitions())

    schemas = service.list_openai_tools()
    result = await service.execute(ToolCallRequest(id="1", name="calculator", arguments={"expression": "1+1"}))

    assert {schema["function"]["name"] for schema in schemas} == {
        "get_current_time",
        "calculator",
        "list_directory",
        "read_text_file",
        "web_fetch",
        "vision_analyze",
        "skill_manage",
    }
    assert result.status == ToolResultStatus.SUCCESS


def test_tool_service_get_definition_returns_static_and_dynamic():
    from app.domain.tool import ToolDefinition, RiskLevel, ToolSourceType
    static_def = ToolDefinition(
        name="static_tool",
        description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.BUILTIN,
    )
    dynamic_def = ToolDefinition(
        name="dynamic_tool",
        description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.MCP,
    )
    service = ToolService(RecordingExecutor(), [static_def])
    service.set_dynamic_definitions("mcp-1", [dynamic_def])

    assert service.get_definition("static_tool") is static_def
    assert service.get_definition("dynamic_tool") is dynamic_def
    assert service.get_definition("missing_tool") is None


def test_builtin_tool_definitions_include_source_and_toolset_metadata():
    definitions = {definition.name: definition for definition in builtin_tool_definitions()}

    assert definitions["get_current_time"].source_type is ToolSourceType.BUILTIN
    assert definitions["get_current_time"].toolset == "system"
    assert definitions["calculator"].source_type is ToolSourceType.BUILTIN
    assert definitions["calculator"].toolset == "math"
    assert definitions["list_directory"].source_type is ToolSourceType.BUILTIN
    assert definitions["list_directory"].toolset == "file"
    assert definitions["read_text_file"].source_type is ToolSourceType.BUILTIN
    assert definitions["read_text_file"].toolset == "file"
    assert definitions["web_fetch"].source_type is ToolSourceType.BUILTIN
    assert definitions["web_fetch"].toolset == "web"
    assert definitions["web_fetch"].risk_level is RiskLevel.SAFE
    assert definitions["vision_analyze"].source_type is ToolSourceType.BUILTIN
    assert definitions["vision_analyze"].toolset == "vision"
    assert definitions["vision_analyze"].risk_level is RiskLevel.SAFE


def test_vision_analyze_definition_schema_rejects_additional_properties():
    definitions = {definition.name: definition for definition in builtin_tool_definitions()}
    schema = definitions["vision_analyze"].input_schema

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"image_url", "question"}


def test_vision_analyze_exposed_as_safe_builtin_tool():
    service = ToolService(FakeExecutor(), builtin_tool_definitions())

    schemas = service.list_openai_tools(risk_level=RiskLevel.SAFE)
    names = {schema["function"]["name"] for schema in schemas}

    assert "vision_analyze" in names


def test_vision_analyze_not_in_builtin_tool_names_constant():
    from app.infrastructure.tools.builtin import BUILTIN_TOOL_NAMES

    assert "vision_analyze" not in BUILTIN_TOOL_NAMES


def test_web_fetch_definition_can_be_disabled():
    service = ToolService(FakeExecutor(), builtin_tool_definitions(web_fetch_enabled=False))

    names = {schema["function"]["name"] for schema in service.list_openai_tools()}

    assert "web_fetch" not in names


def test_knowledge_tool_definition_uses_knowledge_source_and_toolset():
    definition = knowledge_tool_definitions(enabled=True)[0]

    assert definition.name == "search_knowledge"
    assert definition.source_type is ToolSourceType.KNOWLEDGE
    assert definition.toolset == "knowledge"
    assert definition.input_schema["required"] == ["kb_id", "query"]


def test_openai_tool_schemas_exclude_n_agent_metadata():
    service = ToolService(FakeExecutor(), builtin_tool_definitions() + knowledge_tool_definitions(enabled=True))

    schemas = service.list_openai_tools()

    for schema in schemas:
        assert set(schema.keys()) == {"type", "function"}
        assert set(schema["function"].keys()) == {"name", "description", "parameters"}
        assert "source_type" not in str(schema)
        assert "toolset" not in str(schema)
        assert "risk_level" not in str(schema)


@pytest.mark.asyncio
async def test_tool_service_exposes_dangerous_in_default_but_denies_without_approval():
    """DANGEROUS tools are listed in DEFAULT (realtime) so the model can call
    them, but execution without an approval grant is denied (spec: DANGEROUS
    审批通过后执行). CONFIRM tools are also listed and denied without a grant."""
    definitions = [
        ToolDefinition("confirm_tool", "confirm", {"type": "object"}, RiskLevel.CONFIRM),
        ToolDefinition("dangerous_tool", "dangerous", {"type": "object"}, RiskLevel.DANGEROUS),
    ]
    service = ToolService(FakeExecutor(), definitions)

    schemas = service.list_openai_tools()
    confirm = await service.execute(ToolCallRequest(id="1", name="confirm_tool"))
    dangerous = await service.execute(ToolCallRequest(id="2", name="dangerous_tool"))

    assert {schema["function"]["name"] for schema in schemas} == {
        "confirm_tool",
        "dangerous_tool",
    }
    assert confirm.status == ToolResultStatus.PERMISSION_DENIED
    assert dangerous.status == ToolResultStatus.PERMISSION_DENIED


def test_list_openai_tools_can_filter_safe_only():
    definitions = [
        ToolDefinition("safe_tool", "safe", {"type": "object"}, RiskLevel.SAFE),
        ToolDefinition("confirm_tool", "confirm", {"type": "object"}, RiskLevel.CONFIRM),
        ToolDefinition("dangerous_tool", "dangerous", {"type": "object"}, RiskLevel.DANGEROUS),
    ]
    service = ToolService(FakeExecutor(), definitions)

    names = {schema["function"]["name"] for schema in service.list_openai_tools(risk_level=RiskLevel.SAFE)}

    assert names == {"safe_tool"}


def test_list_openai_tools_safe_only_exposes_permitted_managed_tool():
    """Regression: unattended task workers run under SAFE_ONLY but must still
    see the managed task tools declared in permitted_managed_tools."""
    definitions = [
        ToolDefinition("safe_tool", "safe", {"type": "object"}, RiskLevel.SAFE),
        _managed_def(),  # manage_schedule, CONFIRM + managed=True
    ]
    service = ToolService(FakeExecutor(), definitions)

    # SAFE_ONLY without permitted_managed_tools -> managed tool hidden
    names = {
        s["function"]["name"]
        for s in service.list_openai_tools(ToolExposurePolicy.SAFE_ONLY, ToolExecutionContext())
    }
    assert names == {"safe_tool"}

    # SAFE_ONLY with permitted_managed_tools -> managed tool exposed
    ctx = ToolExecutionContext(permitted_managed_tools={"manage_schedule"})
    names = {
        s["function"]["name"]
        for s in service.list_openai_tools(ToolExposurePolicy.SAFE_ONLY, ctx)
    }
    assert names == {"safe_tool", "manage_schedule"}



def test_kb_tool_definition_enabled_when_configured():
    definitions = builtin_tool_definitions() + knowledge_tool_definitions(enabled=True)
    service = ToolService(FakeExecutor(), definitions)

    names = {schema["function"]["name"] for schema in service.list_openai_tools()}

    assert "search_knowledge" in names


@pytest.mark.asyncio
async def test_disabled_kb_tool_is_hidden_and_denied():
    definitions = knowledge_tool_definitions(enabled=False)
    service = ToolService(FakeExecutor(), definitions)

    schemas = service.list_openai_tools()
    result = await service.execute(ToolCallRequest(id="1", name="search_knowledge", arguments={"kb_id": "kb-1", "query": "x"}))

    assert schemas == []
    assert result.status == ToolResultStatus.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_tool_service_managed_tool_allowed_when_in_permitted_set():
    executor = RecordingExecutor()
    service = ToolService(executor, [_managed_def()])
    ctx = ToolExecutionContext(permitted_managed_tools={"manage_schedule"})
    result = await service.execute(
        ToolCallRequest(id="1", name="manage_schedule", arguments={"action": "list"}),
        ctx,
    )
    assert result.status is ToolResultStatus.SUCCESS
    assert executor.calls and executor.calls[0].name == "manage_schedule"


@pytest.mark.asyncio
async def test_tool_service_managed_tool_denied_when_not_permitted():
    service = ToolService(RecordingExecutor(), [_managed_def()])
    ctx = ToolExecutionContext()
    result = await service.execute(
        ToolCallRequest(id="1", name="manage_schedule", arguments={"action": "list"}),
        ctx,
    )
    assert result.status is ToolResultStatus.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_tool_service_managed_does_not_use_allowed_confirm_tools():
    service = ToolService(RecordingExecutor(), [_managed_def()])
    ctx = ToolExecutionContext(
        allowed_confirm_tools={"manage_schedule": {"action": "list"}},
        permitted_managed_tools=set(),
    )
    result = await service.execute(
        ToolCallRequest(id="1", name="manage_schedule", arguments={"action": "list"}),
        ctx,
    )
    assert result.status is ToolResultStatus.PERMISSION_DENIED


def test_tool_service_rejects_managed_with_non_confirm_risk():
    bad = ToolDefinition(
        name="bad",
        description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.SAFE,
        managed=True,
    )
    with pytest.raises(ValueError, match="managed"):
        ToolService(RecordingExecutor(), [bad])


def test_safe_only_hides_agent_source_tools():
    agent_def = ToolDefinition(
        name="schedule_query",
        description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.AGENT,
        toolset="schedule",
    )
    service = ToolService(RecordingExecutor(), [agent_def])
    schemas = service.list_openai_tools(risk_level=RiskLevel.SAFE)
    assert all(schema["function"]["name"] != "schedule_query" for schema in schemas)


def test_safe_only_exposes_agent_tool_when_granted_by_context():
    agent_def = ToolDefinition(
        name="host_terminal",
        description="",
        input_schema={"type": "object"},
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.AGENT,
        toolset="host",
    )
    service = ToolService(RecordingExecutor(), [agent_def])
    granted = service.list_openai_tools(
        risk_level=RiskLevel.SAFE,
        context=ToolExecutionContext(granted_tools=frozenset({"host_terminal"})),
    )
    assert "host_terminal" in {schema["function"]["name"] for schema in granted}

    # without grant the AGENT tool stays hidden under SAFE_ONLY
    ungranted = service.list_openai_tools(
        risk_level=RiskLevel.SAFE, context=ToolExecutionContext()
    )
    assert "host_terminal" not in {schema["function"]["name"] for schema in ungranted}


def test_schedule_tool_definitions_shape():
    defs = {d.name: d for d in schedule_tool_definitions()}
    assert set(defs) == {"manage_schedule", "schedule_query"}

    manage = defs["manage_schedule"]
    assert manage.risk_level is RiskLevel.CONFIRM
    assert manage.managed is True
    assert manage.source_type is ToolSourceType.AGENT
    assert manage.toolset == "schedule"
    props = manage.input_schema["properties"]
    assert props["action"]["enum"] == ["create", "update", "pause", "resume", "run", "remove"]
    assert "cron_expression" in props
    assert "prompt" in props
    assert "task_id" in props
    assert "timezone" in props
    assert "delivery_target" in props
    assert manage.input_schema["required"] == ["action"]

    query = defs["schedule_query"]
    assert query.risk_level is RiskLevel.SAFE
    assert query.managed is False
    assert query.source_type is ToolSourceType.AGENT
    assert query.input_schema["properties"]["action"]["enum"] == ["list", "get"]


def _definition(
    name: str,
    *,
    description: str = "description",
    risk_level: RiskLevel = RiskLevel.SAFE,
    source_type: ToolSourceType = ToolSourceType.BUILTIN,
    enabled: bool = True,
    managed: bool = False,
    input_schema=None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object"} if input_schema is None else input_schema,
        risk_level=risk_level,
        source_type=source_type,
        enabled=enabled,
        managed=managed,
    )


class RecordingPolicy(ToolPolicy):
    def __init__(self):
        self.validated: list[str] = []

    def validate_definition(self, definition):
        self.validated.append(definition.name)
        super().validate_definition(definition)


def test_constructor_injects_policy_and_validates_all_static_definitions_first():
    policy = RecordingPolicy()
    service = ToolService(
        RecordingExecutor(),
        [_definition("first"), _definition("second")],
        policy=policy,
    )

    assert service.policy is policy
    assert policy.validated == ["first", "second"]


def test_dynamic_definitions_filter_schema_before_dedup_and_keep_priority_order():
    static = _definition("static", description="static")
    first_source = _definition("shared", description="first source", source_type=ToolSourceType.MCP)
    second_source = _definition("shared", description="second source", source_type=ToolSourceType.PLUGIN)
    valid_after_invalid = _definition("later", description="valid", source_type=ToolSourceType.MCP)
    service = ToolService(RecordingExecutor(), [static])

    service.set_dynamic_definitions("first", [
        _definition("static", description="ignored static", source_type=ToolSourceType.MCP),
        _definition("later", input_schema={"type": "string"}, source_type=ToolSourceType.MCP),
        valid_after_invalid,
        _definition("later", description="duplicate", source_type=ToolSourceType.MCP),
        first_source,
    ])
    service.set_dynamic_definitions("second", [second_source])

    assert service.get_definition("static") is static
    assert service.get_definition("later") is valid_after_invalid
    assert service.get_definition("shared") is first_source
    assert [definition.name for definition in service.list_definitions()] == [
        "static",
        "later",
        "shared",
        "shared",
    ]


def test_dynamic_definition_replacement_is_atomic_and_preserves_source_order_on_failure():
    old = _definition("shared", description="old", source_type=ToolSourceType.MCP)
    fallback = _definition("shared", description="fallback", source_type=ToolSourceType.PLUGIN)
    service = ToolService(RecordingExecutor(), [])
    service.set_dynamic_definitions("first", [old])
    service.set_dynamic_definitions("second", [fallback])

    with pytest.raises(ValueError, match="confirm"):
        service.set_dynamic_definitions("first", [
            _definition("new", source_type=ToolSourceType.MCP),
            _definition("invalid", managed=True, source_type=ToolSourceType.MCP),
        ])

    assert service.get_definition("shared") is old
    assert list(service.dynamic_definitions) == ["first", "second"]


@pytest.mark.parametrize(
    ("exposure", "expected"),
    [
        (
            None,
            {
                "static_safe",
                "static_agent",
                "static_confirm",
                "static_dangerous",
                "dynamic_safe",
                "dynamic_agent",
                "dynamic_confirm",
                "dynamic_dangerous",
            },
        ),
        (RiskLevel.SAFE, {"static_safe", "dynamic_safe"}),
        (
            ToolExposurePolicy.DEFAULT,
            {
                "static_safe",
                "static_agent",
                "static_confirm",
                "static_dangerous",
                "dynamic_safe",
                "dynamic_agent",
                "dynamic_confirm",
                "dynamic_dangerous",
            },
        ),
        (ToolExposurePolicy.SAFE_ONLY, {"static_safe", "dynamic_safe"}),
        (RiskLevel.CONFIRM, {"static_confirm", "dynamic_confirm"}),
        (RiskLevel.DANGEROUS, {"static_dangerous", "dynamic_dangerous"}),
    ],
)
def test_list_openai_tools_applies_compatible_static_and_dynamic_exposure_matrix(
    exposure,
    expected,
):
    static = [
        _definition("static_safe"),
        _definition("static_agent", source_type=ToolSourceType.AGENT),
        _definition("static_confirm", risk_level=RiskLevel.CONFIRM),
        _definition("static_dangerous", risk_level=RiskLevel.DANGEROUS),
        _definition("static_disabled", enabled=False),
        _definition("static_bad_schema", input_schema={"type": "string"}),
    ]
    dynamic = [
        _definition("dynamic_safe", source_type=ToolSourceType.MCP),
        _definition("dynamic_agent", source_type=ToolSourceType.AGENT),
        _definition(
            "dynamic_confirm",
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.MCP,
        ),
        _definition(
            "dynamic_dangerous",
            risk_level=RiskLevel.DANGEROUS,
            source_type=ToolSourceType.MCP,
        ),
        _definition(
            "dynamic_disabled",
            enabled=False,
            source_type=ToolSourceType.MCP,
        ),
    ]
    service = ToolService(RecordingExecutor(), static)
    service.set_dynamic_definitions("dynamic", dynamic)

    schemas = service.list_openai_tools(exposure)

    assert {schema["function"]["name"] for schema in schemas} == expected
    assert all(set(schema) == {"type", "function"} for schema in schemas)
    assert all(
        set(schema["function"]) == {"name", "description", "parameters"}
        for schema in schemas
    )


def test_evaluate_execution_returns_frozen_decision_and_display_from_one_definition_snapshot():
    original = _definition(
        "dynamic",
        description="approval text v1",
        risk_level=RiskLevel.CONFIRM,
        source_type=ToolSourceType.MCP,
    )
    service = ToolService(RecordingExecutor(), [])
    service.set_dynamic_definitions("mcp", [original])
    request = ToolCallRequest(id="call", name="dynamic", arguments={"value": 1})

    evaluation = service.evaluate_execution(request)
    service.set_dynamic_definitions("mcp", [
        _definition("dynamic", description="v2", source_type=ToolSourceType.MCP)
    ])

    assert evaluation.decision == PolicyDecision(
        PolicyOutcome.REQUIRE_APPROVAL,
        "confirm_approval_required",
    )
    assert evaluation.approval.name == "dynamic"
    assert evaluation.approval.description == "approval text v1"
    assert evaluation.approval.risk_level is RiskLevel.CONFIRM
    with pytest.raises(FrozenInstanceError):
        evaluation.approval.description = "mutated"


def test_evaluate_and_authorize_once_use_lookup_and_raise_clear_not_found_error():
    service = ToolService(RecordingExecutor(), [])
    request = ToolCallRequest(id="missing", name="missing")

    with pytest.raises(ToolNotFoundError, match="missing"):
        service.evaluate_execution(request)
    with pytest.raises(ValueError, match="missing"):
        service.authorize_once(request)


def test_authorize_once_uses_current_definition_and_returns_one_call_context():
    service = ToolService(RecordingExecutor(), [
        _definition("confirm", risk_level=RiskLevel.CONFIRM)
    ])
    request = ToolCallRequest(id="call", name="confirm", arguments={"path": "a"})

    authorized = service.authorize_once(request, ToolExecutionContext(session_id="s"))

    assert authorized.session_id == "s"
    assert authorized.allowed_confirm_tools == {"confirm": {"path": "a"}}


@pytest.mark.asyncio
async def test_old_evaluation_cannot_authorize_or_execute_replaced_dynamic_definition():
    old_definition = _definition(
        "dynamic",
        description="old confirm tool",
        risk_level=RiskLevel.CONFIRM,
        source_type=ToolSourceType.MCP,
    )
    new_definition = _definition(
        "dynamic",
        description="replacement tool",
        risk_level=RiskLevel.SAFE,
        source_type=ToolSourceType.MCP,
    )
    executor = RecordingExecutor()
    service = ToolService(executor, [])
    service.set_dynamic_definitions("mcp", [old_definition])
    request = ToolCallRequest(id="call", name="dynamic", arguments={"value": 1})
    evaluation = service.evaluate_execution(request)
    service.set_dynamic_definitions("mcp", [new_definition])

    with pytest.raises(ValueError, match="definition changed"):
        service.authorize_once(request, evaluation=evaluation)

    result = await service.execute(
        request,
        ToolExecutionContext(allowed_confirm_tools={"dynamic": {"value": 1}}),
        evaluation=evaluation,
    )

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert result.content == {
        "error": "permission_denied",
        "reason": "tool_definition_changed",
    }
    assert executor.calls == []


class CountingPolicy(ToolPolicy):
    def __init__(self):
        self.evaluations = 0

    def evaluate_execution(self, definition, request, context=None):
        self.evaluations += 1
        return super().evaluate_execution(definition, request, context)


@pytest.mark.asyncio
async def test_unchanged_evaluation_authorizes_and_execute_rechecks_policy():
    policy = CountingPolicy()
    executor = RecordingExecutor()
    service = ToolService(
        executor,
        [_definition("confirm", risk_level=RiskLevel.CONFIRM)],
        policy=policy,
    )
    request = ToolCallRequest(id="call", name="confirm", arguments={"path": "a"})
    evaluation = service.evaluate_execution(request)

    authorized = service.authorize_once(request, evaluation=evaluation)
    evaluations_before_execute = policy.evaluations
    result = await service.execute(
        request,
        authorized,
        evaluation=evaluation,
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert policy.evaluations == evaluations_before_execute + 1
    assert executor.calls == [request]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        ToolCallRequest(id="call", name="confirm", arguments={"path": "a"}),
        ToolCallRequest(id="other-id", name="confirm", arguments={"path": "a"}),
        ToolCallRequest(id="call", name="other", arguments={"path": "a"}),
        ToolCallRequest(id="call", name="confirm", arguments={"path": "b"}),
    ],
)
async def test_evaluation_cannot_authorize_or_execute_replacement_request(replacement):
    executor = RecordingExecutor()
    service = ToolService(executor, [
        _definition("confirm", risk_level=RiskLevel.CONFIRM),
        _definition("other", risk_level=RiskLevel.CONFIRM),
    ])
    original = ToolCallRequest(
        id="call",
        name="confirm",
        arguments={"path": "a"},
    )
    evaluation = service.evaluate_execution(original)

    with pytest.raises(ValueError):
        service.authorize_once(replacement, evaluation=evaluation)

    result = await service.execute(
        replacement,
        ToolExecutionContext(
            allowed_confirm_tools={replacement.name: "session"},
        ),
        evaluation=evaluation,
    )

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "reason"),
    [
        (_definition("disabled", enabled=False), "tool_disabled"),
        (_definition("dangerous", risk_level=RiskLevel.DANGEROUS), "dangerous_approval_required"),
        (_definition("confirm", risk_level=RiskLevel.CONFIRM), "confirm_approval_required"),
    ],
)
async def test_execute_denies_non_allow_decisions_with_stable_non_sensitive_content(
    definition,
    reason,
):
    executor = RecordingExecutor()
    service = ToolService(executor, [definition])
    request = ToolCallRequest(id="call", name=definition.name, arguments={"secret": "value"})
    context = ToolExecutionContext(
        metadata={"authorization": "untrusted"},
        trusted_metadata={"token": "trusted-secret"},
    )

    result = await service.execute(request, context)

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert result.content == {"error": "permission_denied", "reason": reason}
    assert executor.calls == []
    assert "secret" not in str(result.content)
    assert "authorization" not in str(result.content)


@pytest.mark.asyncio
async def test_execute_not_found_keeps_compatible_error_shape():
    service = ToolService(RecordingExecutor(), [])

    result = await service.execute(ToolCallRequest(id="call", name="missing"))

    assert result.status is ToolResultStatus.ERROR
    assert result.content == {"error": "tool not found"}


class TypeErrorPolicy(ToolPolicy):
    def evaluate_execution(self, definition, request, context=None):
        raise TypeError("policy failure")


@pytest.mark.asyncio
async def test_execute_does_not_apply_executor_typeerror_fallback_to_policy_errors():
    service = ToolService(
        RecordingExecutor(),
        [_definition("safe")],
        policy=TypeErrorPolicy(),
    )

    with pytest.raises(TypeError, match="policy failure"):
        await service.execute(ToolCallRequest(id="call", name="safe"))


# ---------------------------------------------------------------------------
# Tool Budget enforcement (T12 S3): reserve before executor; settle/release
# ---------------------------------------------------------------------------

from app.application.budget_service import BudgetService
from app.application.policy_snapshot import BudgetPolicyConfig


def _budget_service(max_tool_calls: int = 100) -> BudgetService:
    return BudgetService(BudgetPolicyConfig(max_tool_calls=max_tool_calls))


@pytest.mark.asyncio
async def test_budget_reserved_on_allowed_path():
    """A SAFE tool call that passes ToolPolicy admission reserves Budget."""
    executor = RecordingExecutor()
    budget = _budget_service(max_tool_calls=10)
    service = ToolService(executor, [_definition("safe")], budget_service=budget)
    ctx = ToolExecutionContext(session_id="sess-budget-1")

    await service.execute(ToolCallRequest(id="c1", name="safe", arguments={}), ctx)

    state = budget.get_state("sess-budget-1")
    assert state is not None
    assert state.tool_calls_reserved == 1  # settled (counter stays at 1)


@pytest.mark.asyncio
async def test_budget_reserved_on_approval_authorized_path():
    """A CONFIRM tool call with explicit approval reserves Budget."""
    executor = RecordingExecutor()
    budget = _budget_service(max_tool_calls=10)
    service = ToolService(executor, [_definition("confirm", risk_level=RiskLevel.CONFIRM)], budget_service=budget)
    ctx = ToolExecutionContext(
        session_id="sess-budget-2",
        allowed_confirm_tools={"confirm": {"path": "a"}},
    )

    await service.execute(ToolCallRequest(id="c1", name="confirm", arguments={"path": "a"}), ctx)

    state = budget.get_state("sess-budget-2")
    assert state is not None
    assert state.tool_calls_reserved == 1


@pytest.mark.asyncio
async def test_budget_not_reserved_on_deny_path():
    """A DANGEROUS tool call denied by ToolPolicy never touches Budget."""
    executor = RecordingExecutor()
    budget = _budget_service(max_tool_calls=10)
    service = ToolService(executor, [_definition("danger", risk_level=RiskLevel.DANGEROUS)], budget_service=budget)
    ctx = ToolExecutionContext(session_id="sess-budget-3")

    result = await service.execute(ToolCallRequest(id="c1", name="danger"), ctx)

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert len(executor.calls) == 0
    # Budget account should not have been created
    state = budget.get_state("sess-budget-3")
    assert state is None or state.tool_calls_reserved == 0


@pytest.mark.asyncio
async def test_budget_not_reserved_on_approval_required_path():
    """A CONFIRM tool call without approval is denied; Budget is never touched."""
    executor = RecordingExecutor()
    budget = _budget_service(max_tool_calls=10)
    service = ToolService(executor, [_definition("confirm", risk_level=RiskLevel.CONFIRM)], budget_service=budget)
    ctx = ToolExecutionContext(session_id="sess-budget-4")

    result = await service.execute(ToolCallRequest(id="c1", name="confirm", arguments={}), ctx)

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert len(executor.calls) == 0
    state = budget.get_state("sess-budget-4")
    assert state is None or state.tool_calls_reserved == 0


@pytest.mark.asyncio
async def test_budget_released_on_executor_exception():
    """When the executor raises, the Budget reservation is released (no leak)."""

    class FailingExecutor:
        async def execute(self, request: ToolCallRequest, context=None) -> ToolResult:
            raise RuntimeError("executor failure")

    budget = _budget_service(max_tool_calls=10)
    service = ToolService(FailingExecutor(), [_definition("safe")], budget_service=budget)
    ctx = ToolExecutionContext(session_id="sess-budget-5")

    with pytest.raises(RuntimeError, match="executor failure"):
        await service.execute(ToolCallRequest(id="c1", name="safe"), ctx)

    state = budget.get_state("sess-budget-5")
    assert state is not None
    assert state.tool_calls_reserved == 0  # released -- no leak


@pytest.mark.asyncio
async def test_budget_released_on_cancel():
    """When the executor is cancelled, the Budget reservation is released (no leak).

    asyncio.CancelledError is a BaseException (not Exception) in Python 3.8+,
    so it needs explicit handling to release the budget reservation.
    """
    import asyncio

    class CancellingExecutor:
        async def execute(self, request: ToolCallRequest, context=None) -> ToolResult:
            raise asyncio.CancelledError()

    budget = _budget_service(max_tool_calls=10)
    service = ToolService(CancellingExecutor(), [_definition("safe")], budget_service=budget)
    ctx = ToolExecutionContext(session_id="sess-budget-6")

    with pytest.raises(asyncio.CancelledError):
        await service.execute(ToolCallRequest(id="c1", name="safe"), ctx)

    state = budget.get_state("sess-budget-6")
    assert state is not None
    assert state.tool_calls_reserved == 0  # released -- no leak


def test_skill_manage_exposed_in_skill_toolset():
    from app.application.skill_service import skill_manage_tool_definition
    d = skill_manage_tool_definition()
    assert d.name == "skill_manage"
    # ToolService 静态/内置定义应包含 skill_manage
    from app.application.tool_service import builtin_tool_definitions
    names = [td.name for td in builtin_tool_definitions()]
    assert "skill_manage" in names
