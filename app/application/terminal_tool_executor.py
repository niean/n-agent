"""TerminalToolExecutor - shell command execution in sandbox.

Mirrors SandboxToolExecutor's structure but does NOT inherit or call it:
- No callback tools (terminal has no enabled_tools/callback_registry)
- No staging dir (uses sandbox_manager.default_workdir instead of new_call_staging)
- No SandboxExecutionRequest - calls sandbox.exec_command(command, workdir, timeout) directly
- No outer asyncio.wait_for - timeout handled by sandbox implementation layer
- Workdir validated per backend before calling sandbox
- History records authorized_callback_tools=[] and result["tool_name"]="terminal"

T10: SandboxPolicy + BudgetService integration.  When sandbox_policy is injected,
the executor evaluates the policy BEFORE acquiring the session lock or calling
get_or_create.  BudgetService reserves sandbox resources before policy
evaluation; deny -> 0 get_or_create calls.  After execution, settle uses ACTUAL
duration.  On exception, release.  Non-zero returncode -> SUCCESS preserved.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
from datetime import datetime, timezone
from pathlib import Path

from app.application.budget_service import BudgetService
from app.domain.budget import (
    BudgetActualUsage,
    BudgetReserveKind,
    BudgetReserveRequest,
    SandboxReserveSpec,
)
from app.domain.policy import PolicyOutcome
from app.domain.sandbox import (
    SandboxExecutionHistoryEntry,
    SandboxStatus,
)
from app.domain.sandbox_policy import (
    SandboxDomainConfig,
    SandboxExecutionGrant,
    SandboxMountAccess,
    SandboxMountSpec,
    SandboxPolicy,
    SandboxPolicyRequest,
    SandboxResourceSpec,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)

logger = logging.getLogger(__name__)


def _map_status(status: SandboxStatus) -> ToolResultStatus:
    if status is SandboxStatus.SUCCESS:
        return ToolResultStatus.SUCCESS
    if status is SandboxStatus.TIMEOUT:
        return ToolResultStatus.TIMEOUT
    return ToolResultStatus.ERROR


class TerminalToolExecutor:
    def __init__(
        self,
        sandbox_manager,
        settings,
        history_registry=None,
        sandbox_policy: SandboxPolicy | None = None,
        sandbox_config: SandboxDomainConfig | None = None,
        budget_service: BudgetService | None = None,
    ) -> None:
        self.sandbox_manager = sandbox_manager
        self.settings = settings
        self.history_registry = history_registry
        self.sandbox_policy = sandbox_policy
        self.sandbox_config = sandbox_config
        self.budget_service = budget_service

    async def execute(
        self, request: ToolCallRequest, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        tool_call_id = request.id
        session_id = (context.session_id if context and context.session_id else "") or request.id
        budget_run_id = (
            (context.run_id or context.session_id) if context is not None else None
        ) or request.id

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

        budget_reservation = None
        grant: SandboxExecutionGrant | None = None
        result: ToolResult | None = None

        try:
            # -- Phase 1: Budget + Policy (before session lock) --
            if self.sandbox_policy is not None and self.sandbox_config is not None:
                cfg = self.sandbox_config
                cpus = cfg.cpus
                memory_mb = cfg.memory_mb
                pids = cfg.pids_limit
                max_stdout = cfg.max_stdout_bytes
                max_stderr = cfg.max_stderr_bytes

                # Budget reserve
                if self.budget_service is not None:
                    spec = SandboxReserveSpec(
                        max_seconds=float(timeout_seconds),
                        max_cpu_seconds=cpus * timeout_seconds,
                        max_memory_mb_seconds=float(memory_mb * timeout_seconds),
                        max_callback_calls=0,
                    )
                    budget_reservation = await self.budget_service.reserve(
                        budget_run_id,
                        BudgetReserveRequest(
                            kind=BudgetReserveKind.SANDBOX_RESOURCE,
                            sandbox_spec=spec,
                        ),
                    )
                    if budget_reservation.outcome is PolicyOutcome.DENY:
                        result = ToolResult(
                            tool_call_id=tool_call_id,
                            tool_name="terminal",
                            status=ToolResultStatus.ERROR,
                            content={"error": f"budget denied: {budget_reservation.reason}"},
                        )
                        budget_reservation = None

                # Policy evaluate
                if result is None:
                    policy_req = SandboxPolicyRequest(
                        operation="terminal",
                        backend=self.sandbox_manager.sandbox_type,
                        network=False,
                        mounts=(
                            SandboxMountSpec(
                                target="/workspace",
                                access=SandboxMountAccess.READONLY,
                            ),
                            SandboxMountSpec(
                                target="/scratch",
                                access=SandboxMountAccess.READWRITE,
                            ),
                        ),
                        requested_callbacks=frozenset(),
                        registry_enabled_callbacks=frozenset(),
                        resources=SandboxResourceSpec(
                            timeout_seconds=timeout_seconds,
                            cpus=cpus,
                            memory_mb=memory_mb,
                            pids=pids,
                            max_stdout_bytes=max_stdout,
                            max_stderr_bytes=max_stderr,
                        ),
                    )
                    grant = self.sandbox_policy.evaluate(policy_req)
                    if not grant.allowed:
                        if budget_reservation is not None and self.budget_service is not None:
                            await self.budget_service.release(budget_run_id, budget_reservation)
                            budget_reservation = None
                        result = ToolResult(
                            tool_call_id=tool_call_id,
                            tool_name="terminal",
                            status=ToolResultStatus.ERROR,
                            content={"error": f"sandbox denied: {grant.reason}"},
                        )
                    elif grant.resources.timeout_seconds < timeout_seconds:
                        # Clamp timeout to grant max
                        timeout_seconds = grant.resources.timeout_seconds
            else:
                logger.warning("sandbox_policy not injected; using legacy settings-based behavior")

            # -- Phase 2: Execute (with session lock) --
            if result is None:
                result = await self._run_terminal(
                    command, code_hash, session_id, tool_call_id,
                    request.arguments.get("workdir"),
                    timeout_seconds, grant, budget_reservation,
                )
        except Exception as exc:
            if budget_reservation is not None and self.budget_service is not None:
                try:
                    await self.budget_service.release(budget_run_id, budget_reservation)
                except Exception:
                    logger.warning("budget release failed", exc_info=True)
            result = ToolResult(
                tool_call_id=tool_call_id,
                tool_name="terminal",
                status=ToolResultStatus.ERROR,
                content={"error": f"sandbox unavailable: {exc}"},
            )

        self._record_history(tool_call_id, command, code_hash, session_id, result)
        return result

    async def _run_terminal(
        self,
        command: str,
        code_hash: str,
        session_id: str,
        tool_call_id: str,
        workdir_arg,
        timeout_seconds: int,
        grant: SandboxExecutionGrant | None = None,
        budget_reservation=None,
    ) -> ToolResult:
        async with self.sandbox_manager.acquire_session_lock(session_id):
            sandbox = await self.sandbox_manager.get_or_create(
                session_id, grant=grant,
            )
            workdir, workdir_error = self._resolve_workdir(
                session_id, workdir_arg
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

            # Settle budget with actual duration (no callbacks for terminal)
            if budget_reservation is not None and self.budget_service is not None:
                actual_duration = None
                if "duration_seconds" in (result.content if isinstance(result.content, dict) else {}):
                    actual_duration = result.content["duration_seconds"]
                try:
                    await self.budget_service.settle(
                        budget_run_id,
                        budget_reservation,
                        BudgetActualUsage(
                            duration_seconds=actual_duration,
                            sandbox_callback_count=0,
                        ),
                    )
                except Exception:
                    logger.warning("budget settle failed", exc_info=True)

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
