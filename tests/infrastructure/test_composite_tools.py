import pytest

from app.domain.tool import ToolCallRequest, ToolResult, ToolResultStatus
from app.infrastructure.tools.composite import CompositeToolExecutor


class FakeExecutor:
    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self.calls = []

    async def execute(self, request: ToolCallRequest) -> ToolResult:
        self.calls.append(request)
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"executor": self.tool_name})


@pytest.mark.asyncio
async def test_composite_routes_builtin_tool_to_builtin_executor():
    builtin = FakeExecutor("builtin")
    kb = FakeExecutor("kb")
    executor = CompositeToolExecutor({"calculator": builtin, "search_knowledge": kb})

    result = await executor.execute(ToolCallRequest(id="1", name="calculator", arguments={"expression": "1+1"}))

    assert result.status == ToolResultStatus.SUCCESS
    assert result.content == {"executor": "builtin"}
    assert len(builtin.calls) == 1
    assert kb.calls == []


@pytest.mark.asyncio
async def test_composite_routes_kb_tool_to_kb_executor():
    builtin = FakeExecutor("builtin")
    kb = FakeExecutor("kb")
    executor = CompositeToolExecutor({"calculator": builtin, "search_knowledge": kb})

    result = await executor.execute(ToolCallRequest(id="1", name="search_knowledge", arguments={"query": "python"}))

    assert result.status == ToolResultStatus.SUCCESS
    assert result.content == {"executor": "kb"}
    assert builtin.calls == []
    assert len(kb.calls) == 1


@pytest.mark.asyncio
async def test_composite_unknown_tool_returns_error():
    executor = CompositeToolExecutor({})

    result = await executor.execute(ToolCallRequest(id="1", name="missing_tool"))

    assert result.status == ToolResultStatus.ERROR
    assert result.content == {"error": "tool not found"}
