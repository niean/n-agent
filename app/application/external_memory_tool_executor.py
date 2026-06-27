from __future__ import annotations

import json
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
)
from .external_memory_manager import ExternalMemoryManager


class ExternalMemoryToolExecutor(ToolExecutor):
    """适配 ExternalMemory 工具到 N-Agent 工具执行体系。

    所有外部记忆工具路由到此，再由 ExternalMemoryManager 分发给提供者。
    """

    def __init__(self, memory_manager: ExternalMemoryManager):
        self._memory_manager = memory_manager

    def can_handle(self, tool_name: str) -> bool:
        """检查是否是记忆提供者工具。"""
        return self._memory_manager.has_tool(tool_name)

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        """路由执行，返回结果。

        权限检查 —— **fail-closed**:
        - 默认 agent_context = "unattended"，不允许写入
        - 只有 context.trusted_metadata 明确设置 agent_context = "primary" 才允许写入
        - source_type=AGENT 保证 safe_only 不会暴露给 unattended/cron
        """
        agent_context = "unattended"  # fail-closed: 默认非 primary，不允许写入
        session_id = context.session_id if context and context.session_id else ""
        enabled_override = context.enabled_override if context else None
        if context and context.trusted_metadata:
            agent_context = context.trusted_metadata.get("agent_context", "unattended")

        result_json = self._memory_manager.handle_tool_call(
            request.name,
            request.arguments,
            agent_context=agent_context,
            session_id=session_id,
            enabled_override=enabled_override,
        )
        try:
            result_data = json.loads(result_json)
            if not isinstance(result_data, dict):
                return ToolResult(
                    tool_call_id=request.id,
                    tool_name=request.name,
                    status=ToolResultStatus.ERROR,
                    content={
                        "error": "provider returned invalid json, expected object",
                        "raw": result_json,
                    },
                    duration_ms=0,
                )
            if result_data.get("success") is True:
                return ToolResult(
                    tool_call_id=request.id,
                    tool_name=request.name,
                    status=ToolResultStatus.SUCCESS,
                    content=result_data,
                    duration_ms=0,
                )
            else:
                return ToolResult(
                    tool_call_id=request.id,
                    tool_name=request.name,
                    status=ToolResultStatus.ERROR,
                    content=result_data,
                    duration_ms=0,
                )
        except (json.JSONDecodeError, TypeError):
            return ToolResult(
                tool_call_id=request.id,
                tool_name=request.name,
                status=ToolResultStatus.ERROR,
                content={"error": "invalid json from provider", "raw": result_json},
                duration_ms=0,
            )
