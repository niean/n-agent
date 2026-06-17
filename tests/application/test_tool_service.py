import pytest

from app.application.tool_service import (
    ToolService,
    builtin_tool_definitions,
    knowledge_tool_definitions,
    schedule_tool_definitions,
)
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


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
    }
    assert result.status == ToolResultStatus.SUCCESS


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
async def test_tool_service_denies_confirm_and_hides_dangerous_tools():
    definitions = [
        ToolDefinition("confirm_tool", "confirm", {"type": "object"}, RiskLevel.CONFIRM),
        ToolDefinition("dangerous_tool", "dangerous", {"type": "object"}, RiskLevel.DANGEROUS),
    ]
    service = ToolService(FakeExecutor(), definitions)

    schemas = service.list_openai_tools()
    confirm = await service.execute(ToolCallRequest(id="1", name="confirm_tool"))
    dangerous = await service.execute(ToolCallRequest(id="2", name="dangerous_tool"))

    assert {schema["function"]["name"] for schema in schemas} == {"confirm_tool"}
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
