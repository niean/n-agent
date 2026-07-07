"""TerminalToolExecutor — shell command execution in sandbox.

Mirrors SandboxToolExecutor's structure but does NOT inherit or call it:
- No callback tools (terminal has no enabled_tools/callback_registry)
- No staging dir (uses sandbox_manager.default_workdir instead of new_call_staging)
- No SandboxExecutionRequest — calls sandbox.exec_command(command, workdir, timeout) directly
- No outer asyncio.wait_for — timeout handled by sandbox implementation layer
- Workdir validated per backend before calling sandbox
- History records authorized_callback_tools=[] and result["tool_name"]="terminal"
"""

from __future__ import annotations

import hashlib
import posixpath
from datetime import datetime, timezone
from pathlib import Path

from app.domain.sandbox import (
    SandboxExecutionHistoryEntry,
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


class TerminalToolExecutor:
    def __init__(self, sandbox_manager, settings, history_registry=None) -> None:
        self.sandbox_manager = sandbox_manager
        self.settings = settings
        self.history_registry = history_registry

    async def execute(
        self, request: ToolCallRequest, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        tool_call_id = request.id
        session_id = (context.session_id if context and context.session_id else "") or request.id

        command_raw = request.arguments.get("command")
        command = command_raw if isinstance(command_raw, str) else ""
        code_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()

        if not command.strip():
            result = ToolResult(
                tool_call_id=tool_call_id,
                tool_name="terminal",
                status=ToolResultStatus.ERROR,
                content={"error": "command required"},
            )
            self._record_history(tool_call_id, command, code_hash, session_id, result)
            return result

        timeout_arg = request.arguments.get("timeout")
        if timeout_arg is None:
            timeout_seconds = self.settings.sandbox_timeout_seconds
        else:
            try:
                timeout_int = int(timeout_arg)
                if timeout_int <= 0:
                    raise ValueError("not positive")
                timeout_seconds = timeout_int
            except (TypeError, ValueError):
                result = ToolResult(
                    tool_call_id=tool_call_id,
                    tool_name="terminal",
                    status=ToolResultStatus.ERROR,
                    content={"error": "timeout must be a positive integer"},
                )
                self._record_history(tool_call_id, command, code_hash, session_id, result)
                return result

        try:
            async with self.sandbox_manager.acquire_session_lock(session_id):
                sandbox = await self.sandbox_manager.get_or_create(session_id)
                workdir, workdir_error = self._resolve_workdir(
                    session_id, request.arguments.get("workdir")
                )
                if workdir_error is not None:
                    result = ToolResult(
                        tool_call_id=tool_call_id,
                        tool_name="terminal",
                        status=ToolResultStatus.ERROR,
                        content={"error": workdir_error},
                    )
                else:
                    sbx_result = await sandbox.exec_command(
                        command, workdir, timeout_seconds
                    )
                    status = _map_status(sbx_result.status)
                    content = {
                        "status": sbx_result.status.value,
                        "stdout": sbx_result.stdout,
                        "stderr": sbx_result.stderr,
                        "returncode": sbx_result.returncode,
                        "duration_seconds": sbx_result.duration_seconds,
                    }
                    result = ToolResult(
                        tool_call_id=tool_call_id,
                        tool_name="terminal",
                        status=status,
                        content=content,
                        duration_ms=int(sbx_result.duration_seconds * 1000),
                    )
        except Exception as exc:
            result = ToolResult(
                tool_call_id=tool_call_id,
                tool_name="terminal",
                status=ToolResultStatus.ERROR,
                content={"error": f"sandbox unavailable: {exc}"},
            )

        self._record_history(tool_call_id, command, code_hash, session_id, result)
        return result

    def _resolve_workdir(
        self, session_id: str, workdir_arg
    ) -> tuple[str | None, str | None]:
        if workdir_arg is None:
            return self.sandbox_manager.default_workdir(session_id), None
        workdir_str = workdir_arg if isinstance(workdir_arg, str) else str(workdir_arg)
        if self.sandbox_manager.sandbox_type == "docker":
            validated = self._validate_docker_workdir(workdir_str)
            if validated is None:
                return None, "workdir must be under /scratch or /workspace in sandbox container"
            return validated, None
        return self._prepare_local_workdir(workdir_str)

    def _validate_docker_workdir(self, workdir: str) -> str | None:
        if not workdir or "\x00" in workdir:
            return None
        norm = posixpath.normpath(workdir)
        if not norm.startswith("/"):
            return None
        if norm == "/scratch" or norm.startswith("/scratch/"):
            return norm
        if norm == "/workspace" or norm.startswith("/workspace/"):
            return norm
        return None

    def _prepare_local_workdir(
        self, workdir: str
    ) -> tuple[str | None, str | None]:
        invalid_msg = "workdir must be under sandbox scratch or workspace root"
        if not workdir or "\x00" in workdir:
            return None, invalid_msg
        p = Path(workdir)
        if not p.is_absolute():
            return None, invalid_msg
        try:
            resolved = p.resolve(strict=False)
        except (OSError, ValueError):
            return None, invalid_msg
        scratch_root = self.sandbox_manager.scratch_root.resolve()
        workspace_root = self.sandbox_manager.workspace_root.resolve()
        in_scratch = self._is_within(resolved, scratch_root)
        in_workspace = self._is_within(resolved, workspace_root)
        if not (in_scratch or in_workspace):
            return None, invalid_msg
        if in_workspace:
            if not resolved.exists():
                return None, f"workspace workdir does not exist: {resolved}"
            return str(resolved), None
        try:
            resolved.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return None, f"failed to create scratch workdir: {resolved}: {exc}"
        return str(resolved), None

    @staticmethod
    def _is_within(resolved: Path, root: Path) -> bool:
        if resolved == root:
            return True
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            return False

    def _record_history(
        self,
        tool_call_id: str,
        command: str,
        code_hash: str,
        session_id: str,
        result: ToolResult,
    ) -> None:
        if self.history_registry is None:
            return
        result_dict = self._result_to_dict(result)
        try:
            self.history_registry.record(
                SandboxExecutionHistoryEntry(
                    id=tool_call_id,
                    session_id=session_id,
                    code_hash=code_hash,
                    code=command,
                    result=result_dict,
                    status=result.status.value,
                    duration_ms=result.duration_ms,
                    authorized_callback_tools=[],
                    created_at=datetime.now(timezone.utc),
                    execution_type="terminal",
                )
            )
        except Exception:
            pass

    def _result_to_dict(self, result: ToolResult) -> dict:
        content = result.content
        if isinstance(content, dict):
            d = dict(content)
        else:
            d = {"value": content}
        d["tool_name"] = "terminal"
        return d
