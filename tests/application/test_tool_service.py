import pytest

from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.domain.tool import RiskLevel, ToolCallRequest, ToolDefinition, ToolResult, ToolResultStatus, ToolSourceType


class FakeExecutor:
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


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


def test_knowledge_tool_definition_uses_knowledge_source_and_toolset():
    definition = knowledge_tool_definitions(enabled=True)[0]

    assert definition.name == "search_knowledge"
    assert definition.source_type is ToolSourceType.KNOWLEDGE
    assert definition.toolset == "knowledge"


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
    result = await service.execute(ToolCallRequest(id="1", name="search_knowledge", arguments={"query": "x"}))

    assert schemas == []
    assert result.status == ToolResultStatus.PERMISSION_DENIED
