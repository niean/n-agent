import pytest

from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.tool import RiskLevel, ToolCallRequest, ToolDefinition, ToolResult, ToolResultStatus


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
