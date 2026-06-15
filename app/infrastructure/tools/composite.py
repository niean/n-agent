from __future__ import annotations

from app.domain.tool import ToolCallRequest, ToolExecutionContext, ToolExecutor, ToolResult, ToolResultStatus


class CompositeToolExecutor:
    def __init__(self, routes: dict[str, ToolExecutor], fallback: ToolExecutor | None = None):
        self.routes = routes
        self.fallback = fallback

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        executor = self.routes.get(request.name) or self.fallback
        if executor is None:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "tool not found"})
        try:
            return await executor.execute(request, context)
        except TypeError:
            return await executor.execute(request)
