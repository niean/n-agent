"""SandboxToolExecutor - direct execution of execute_code.

Authorization model (post-Hermes-baseline):
- execute_code is a SAFE tool. No confirmation gate, no fast-path, no pending store.
- Sandbox itself is the security boundary: workspace :ro + scratch :rw + UDS RPC
  + callback tool allowlist + max_tool_calls + timeout + execution history audit.
- Every execution (success/error/timeout) is recorded in SandboxExecutionHistoryRegistry
  with code_hash for audit.
- Sandbox creation/execution exceptions are caught and returned as ToolResult(ERROR)
  so AgentGraph is not interrupted by Docker unavailability.

T10: SandboxPolicy + BudgetService integration.  When sandbox_policy is injected,
the executor evaluates the policy BEFORE acquiring the session lock or calling
get_or_create.  BudgetService reserves sandbox resources (seconds/CPU/memory/
callbacks) before policy evaluation; deny -> 0 get_or_create calls.  After
execution, settle uses ACTUAL duration/callbacks.  On exception, release.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

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
    SandboxExecutionRequest,
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
        sandbox_policy: SandboxPolicy | None = None,
        sandbox_config: SandboxDomainConfig | None = None,
        budget_service: BudgetService | None = None,
    ) -> None:
        self.sandbox_manager = sandbox_manager
        self.callback_registry = callback_registry
        self.settings = settings
        self.history_registry = history_registry
        self.summary_max_stdout = summary_max_stdout
        self.summary_max_stderr = summary_max_stderr
        self.sandbox_policy = sandbox_policy
        self.sandbox_config = sandbox_config
        self.budget_service = budget_service

    def _resolve_enabled_tools(self, enabled_tools: list[str] | None) -> frozenset[str]:
        registry_enabled = frozenset(t.name for t in self.callback_registry.list_enabled())
        if enabled_tools:
            return frozenset(enabled_tools) & registry_enabled
        return registry_enabled

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        code = str(request.arguments.get("code", ""))
        code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        session_id = (context.session_id if context and context.session_id else "") or request.id
        budget_run_id = (
            (context.run_id or context.session_id) if context is not None else None
        ) or request.id
        trusted_metadata = dict(context.trusted_metadata) if context else {}
        enabled_tools = request.arguments.get("enabled_tools")

        budget_reservation = None
        grant: SandboxExecutionGrant | None = None
        result: ToolResult | None = None

        try:
            # -- Phase 1: Budget + Policy (before session lock) --
            if self.sandbox_policy is not None and self.sandbox_config is not None:
                cfg = self.sandbox_config
                timeout = self.settings.sandbox_timeout_seconds
                cpus = cfg.cpus
                memory_mb = cfg.memory_mb
                pids = cfg.pids_limit
                max_stdout = cfg.max_stdout_bytes
                max_stderr = cfg.max_stderr_bytes

                # Budget reserve
                if self.budget_service is not None:
                    spec = SandboxReserveSpec(
                        max_seconds=float(timeout),
                        max_cpu_seconds=cpus * timeout,
                        max_memory_mb_seconds=float(memory_mb * timeout),
                        max_callback_calls=self.settings.sandbox_max_tool_calls,
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
                            tool_call_id=request.id,
                            tool_name="execute_code",
                            status=ToolResultStatus.ERROR,
                            content={"error": f"budget denied: {budget_reservation.reason}"},
                        )
                        budget_reservation = None  # consumed, don't release

                # Policy evaluate
                if result is None:
                    registry_enabled = frozenset(
                        t.name for t in self.callback_registry.list_enabled()
                    )
                    requested = (
                        frozenset(enabled_tools) if enabled_tools else registry_enabled
                    )
                    network_requested = bool(request.arguments.get("network", False))
                    policy_req = SandboxPolicyRequest(
                        operation="execute_code",
                        backend=self.sandbox_manager.sandbox_type,
                        network=network_requested,
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
                        requested_callbacks=requested,
                        registry_enabled_callbacks=registry_enabled,
                        resources=SandboxResourceSpec(
                            timeout_seconds=timeout,
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
                            tool_call_id=request.id,
                            tool_name="execute_code",
                            status=ToolResultStatus.ERROR,
                            content={"error": f"sandbox denied: {grant.reason}"},
                        )
            else:
                logger.warning("sandbox_policy not injected; using legacy settings-based behavior")

            # -- Phase 2: Execute (with session lock) --
            if result is None:
                result = await self._run_sandbox(
                    code, code_hash, session_id, trusted_metadata, request.id,
                    enabled_tools, grant, budget_reservation,
                )
        except Exception as exc:
            # Release budget on any unexpected exception
            if budget_reservation is not None and self.budget_service is not None:
                try:
                    await self.budget_service.release(budget_run_id, budget_reservation)
                except Exception:
                    logger.warning("budget release failed", exc_info=True)
            result = ToolResult(
                tool_call_id=request.id,
                tool_name="execute_code",
                status=ToolResultStatus.ERROR,
                content={"error": f"sandbox unavailable: {exc}"},
            )

        self._record_history(
            request.id, code, code_hash, session_id, result, enabled_tools, grant,
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
        grant: SandboxExecutionGrant | None = None,
    ) -> None:
        if self.history_registry is None:
            return
        result_dict = _result_to_dict(result)
        if grant is not None:
            authorized = sorted(grant.callbacks)
        else:
            authorized = sorted(self._resolve_enabled_tools(enabled_tools))
        result_dict["authorized_callback_tools"] = authorized
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
        grant: SandboxExecutionGrant | None = None,
        budget_reservation=None,
    ) -> ToolResult:
        async with self.sandbox_manager.acquire_session_lock(session_id):
            sandbox = await self.sandbox_manager.get_or_create(
                session_id, grant=grant,
            )
            staging = self.sandbox_manager.new_call_staging(session_id)

            if grant is not None:
                enabled = grant.callbacks
                timeout = grant.resources.timeout_seconds
            else:
                enabled = self._resolve_enabled_tools(enabled_tools)
                timeout = self.settings.sandbox_timeout_seconds

            req = SandboxExecutionRequest(
                code=code,
                timeout_seconds=timeout,
                max_tool_calls=self.settings.sandbox_max_tool_calls,
                enabled_callback_tools=enabled,
                workspace_root=self.sandbox_manager.workspace_root,
                session_id=session_id,
                trusted_metadata=trusted_metadata,
                scratch_dir=staging,
            )
            sbx_result = await sandbox.execute(req)

            # Settle budget with actual duration/callbacks
            if budget_reservation is not None and self.budget_service is not None:
                try:
                    await self.budget_service.settle(
                        budget_run_id,
                        budget_reservation,
                        BudgetActualUsage(
                            duration_seconds=sbx_result.duration_seconds,
                            sandbox_callback_count=sbx_result.tool_calls_made,
                        ),
                    )
                except Exception:
                    logger.warning("budget settle failed", exc_info=True)

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
