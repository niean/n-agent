from __future__ import annotations

from app.domain.tool import ToolCallRequest, ToolExecutor, ToolResult, ToolResultStatus


class CompositeToolExecutor:
    def __init__(self, routes: dict[str, ToolExecutor]):
        self.routes = routes

    async def execute(self, request: ToolCallRequest) -> ToolResult:
        executor = self.routes.get(request.name)
        if executor is None:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "tool not found"})
        return await executor.execute(request)
