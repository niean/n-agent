"""SandboxToolExecutor — direct execution of execute_code.

Authorization model (post-Hermes-baseline):
- execute_code is a SAFE tool. No confirmation gate, no fast-path, no pending store.
- Sandbox itself is the security boundary: workspace :ro + scratch :rw + UDS RPC
  + callback tool allowlist + max_tool_calls + timeout + execution history audit.
- Every execution (success/error/timeout) is recorded in SandboxExecutionHistoryRegistry
  with code_hash for audit.
- Sandbox creation/execution exceptions are caught and returned as ToolResult(ERROR)
  so AgentGraph is not interrupted by Docker unavailability.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from app.domain.sandbox import (
    SandboxExecutionHistoryEntry,
    SandboxExecutionRequest,
    SandboxStatus,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)


def _map_status(status: SandboxStatus) -> ToolResultStatus:
    if status is SandboxStatus.SUCCESS:
        return ToolResultStatus.SUCCESS
    if status is SandboxStatus.TIMEOUT:
        return ToolResultStatus.TIMEOUT
    return ToolResultStatus.ERROR


def _result_to_dict(result: ToolResult) -> dict:
    content = result.content
    if isinstance(content, dict):
        return dict(content)
    return {"value": content}


class SandboxToolExecutor:
    def __init__(
        self,
        sandbox_manager,
        callback_registry,
        settings,
        history_registry=None,
        summary_max_stdout: int = 2000,
        summary_max_stderr: int = 500,
    ) -> None:
        self.sandbox_manager = sandbox_manager
        self.callback_registry = callback_registry
        self.settings = settings
        self.history_registry = history_registry
        self.summary_max_stdout = summary_max_stdout
        self.summary_max_stderr = summary_max_stderr

    def _resolve_enabled_tools(self, enabled_tools: list[str] | None) -> frozenset[str]:
        registry_enabled = frozenset(t.name for t in self.callback_registry.list_enabled())
        if enabled_tools:
            return frozenset(enabled_tools) & registry_enabled
        return registry_enabled

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        code = str(request.arguments.get("code", ""))
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        session_id = (context.session_id if context and context.session_id else "") or request.id
        trusted_metadata = dict(context.trusted_metadata) if context else {}
        enabled_tools = request.arguments.get("enabled_tools")
        try:
            result = await self._run_sandbox(
                code, code_hash, session_id, trusted_metadata, request.id, enabled_tools,
            )
        except Exception as exc:
            result = ToolResult(
                tool_call_id=request.id,
                tool_name="execute_code",
                status=ToolResultStatus.ERROR,
                content={"error": f"sandbox unavailable: {exc}"},
            )
        self._record_history(
            request.id, code, code_hash, session_id, result, enabled_tools,
        )
        return result

    def _record_history(
        self,
        tool_call_id: str,
        code: str,
        code_hash: str,
        session_id: str,
        result: ToolResult,
        enabled_tools,
    ) -> None:
        if self.history_registry is None:
            return
        result_dict = _result_to_dict(result)
        result_dict["authorized_callback_tools"] = sorted(self._resolve_enabled_tools(enabled_tools))
        try:
            self.history_registry.record(SandboxExecutionHistoryEntry(
                id=tool_call_id,
                session_id=session_id,
                code_hash=code_hash,
                code=code,
                result=result_dict,
                status=result.status.value,
                duration_ms=result.duration_ms,
                authorized_callback_tools=result_dict["authorized_callback_tools"],
                created_at=datetime.now(timezone.utc),
                execution_type="execute_code",
            ))
        except Exception:
            pass

    async def _run_sandbox(
        self,
        code: str,
        code_hash: str,
        session_id: str,
        trusted_metadata: dict,
        tool_call_id: str,
        enabled_tools,
    ) -> ToolResult:
        async with self.sandbox_manager.acquire_session_lock(session_id):
            sandbox = await self.sandbox_manager.get_or_create(session_id)
            staging = self.sandbox_manager.new_call_staging(session_id)
            enabled = self._resolve_enabled_tools(enabled_tools)
            req = SandboxExecutionRequest(
                code=code,
                timeout_seconds=self.settings.sandbox_timeout_seconds,
                max_tool_calls=self.settings.sandbox_max_tool_calls,
                enabled_callback_tools=enabled,
                workspace_root=self.sandbox_manager.workspace_root,
                session_id=session_id,
                trusted_metadata=trusted_metadata,
                scratch_dir=staging,
            )
            sbx_result = await sandbox.execute(req)
            status = _map_status(sbx_result.status)
            content = {
                "status": sbx_result.status.value,
                "stdout": sbx_result.stdout,
                "stderr": sbx_result.stderr,
                "returncode": sbx_result.returncode,
                "tool_calls_made": sbx_result.tool_calls_made,
                "tool_call_log": sbx_result.tool_call_log,
                "duration_seconds": sbx_result.duration_seconds,
            }
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name="execute_code",
                status=status,
                content=content,
                duration_ms=int(sbx_result.duration_seconds * 1000),
            )
