"""T11: Infrastructure task tool executor (TaskManagementToolExecutor).

Implements the 6 managed task tools defined in `app/application/task_tools.py`
(Manus-aligned 7-state machine):
  - task_show:             read task + comments + events(50) + runs +
                           build_worker_context
  - task_complete:         submit completion intent (summary + metadata + artifacts)
  - task_heartbeat:        record heartbeat (note)
  - task_comment:          add comment (task_id + body)
  - task_propose_change:   propose a change requiring user approval (proposal)
  - task_fail:             worker 快速失败（不再重试）

Removed tools (Manus flattening):
  - task_block:   replaced by task_propose_change (intent approval)
  - task_create:  workers no longer spawn subtasks; creation only via user entrypoints
  - task_link:    dependency graph removed

Pattern twelve (trusted_metadata gating):
  - All task identity (task_id, run_id, claim_lock, write_origin) is read
    ONLY from `ctx.trusted_metadata["task"]`. The untrusted `ctx.metadata`
    channel is never consulted for these fields.
  - OpenAI HTTP clients cannot forge `trusted_metadata` -- it is injected
    server-side by `TaskAgentExecutor` when spawning a worker.
  - The tool name must also be present in `ctx.permitted_managed_tools` --
    this is the second layer of gating (worker might not be granted all 6).

Ownership enforcement:
  - `write_origin == "worker"` may only mutate the task identified by
    `trusted_metadata.task.task_id`.
  - task_complete / task_heartbeat / task_propose_change / task_fail
    operate on the current task (no task_id argument); they always use the
    trusted task_id.
  - task_comment takes task_id in arguments; if it differs from the trusted
    task_id, the call is rejected (cross-task comment forbidden).

Terminal intent only:
  - task_complete returns an intent payload; it does NOT call
    TaskRunService.finish_run. TaskRunService owns the single CAS-based
    finalization path. task_propose_change / task_fail likewise delegate
    run finalization to TaskRunService.
"""
from __future__ import annotations

import json
import time
from typing import Any, Protocol

from app.application.task_tools import (
    TASK_TOOL_FAIL,
    TASK_TOOL_COMPLETE,
    TASK_TOOL_HEARTBEAT,
    TASK_TOOL_PROPOSE_CHANGE,
    TASK_TOOL_SHOW,
    TASK_TOOL_COMMENT,
    TASK_TOOL_NAMES,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
)


# 最大返回 event 数（spec: 最近 50 events）
_MAX_EVENTS_IN_SHOW = 50


class _TaskAccessDenied(Exception):
    """Raised when trusted_metadata gating or ownership check fails."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _TaskInvalidArgument(Exception):
    """Raised when tool arguments fail validation."""


# ---------------------------------------------------------------------------
# TaskServiceProtocol: minimal interface the executor depends on.
#
# Batch D will implement `TaskService` to satisfy this Protocol. The executor
# only calls the methods declared here; it does not import the concrete
# TaskService (avoiding an Infrastructure -> Application -> Infrastructure
# cycle, and allowing tests to substitute a fake).
# ---------------------------------------------------------------------------


class TaskServiceProtocol(Protocol):
    """Subset of TaskService methods used by TaskManagementToolExecutor.

    Batch D's TaskService is expected to implement these methods (async).
    Method signatures match the duck-typed calls in the executor; Batch D
    may add richer typed wrappers as long as the call shape is preserved.
    """

    async def get_task_detail(self, task_id: str) -> dict[str, Any] | None: ...

    async def complete(
        self,
        task_id: str,
        summary: str,
        metadata: dict[str, Any],
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]: ...

    async def heartbeat(self, task_id: str, note: str) -> dict[str, Any]: ...

    async def add_comment(
        self,
        task_id: str,
        body: str,
        author: str = "worker",
    ) -> dict[str, Any]: ...

    async def propose_change(
        self,
        task_id: str,
        proposal: str,
        run_id: int,
        proposal_type: str = "approval",
    ) -> dict[str, Any]: ...

    async def cancel_task(self, task_id: str) -> dict[str, Any]: ...

    async def build_worker_context(self, task: Any) -> str: ...


# ---------------------------------------------------------------------------
# TaskManagementToolExecutor
# ---------------------------------------------------------------------------


class TaskManagementToolExecutor(ToolExecutor):
    """Dispatches 7 task tools to TaskService, with trusted_metadata gating.

    Constructor takes a TaskServiceProtocol (Batch D's TaskService will
    satisfy it). Tests substitute a FakeTaskService.
    """

    def __init__(self, service: TaskServiceProtocol):
        self.service = service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            payload = await self._dispatch(request, context)
            status = ToolResultStatus.SUCCESS
        except _TaskAccessDenied as exc:
            payload = {"success": False, "error": exc.code}
            status = ToolResultStatus.PERMISSION_DENIED
        except _TaskInvalidArgument as exc:
            payload = {"success": False, "error": str(exc)}
            status = ToolResultStatus.ERROR
        except Exception as exc:  # pragma: no cover - defensive
            payload = {"success": False, "error": str(exc)}
            status = ToolResultStatus.ERROR
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=status,
            content=json.dumps(payload, ensure_ascii=False, default=str),
            duration_ms=int((time.monotonic() - start) * 1000),
            # These tools submit terminal task intents. Continuing the graph
            # after a successful call can repeatedly submit the same intent
            # until the Agent iteration limit is reached.
            terminal=(
                status is ToolResultStatus.SUCCESS
                and request.name in {
                    TASK_TOOL_COMPLETE,
                    TASK_TOOL_PROPOSE_CHANGE,
                    TASK_TOOL_FAIL,
                }
            ),
        )

    # -----------------------------------------------------------------
    # Dispatch
    # -----------------------------------------------------------------

    async def _dispatch(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None,
    ) -> dict[str, Any]:
        if context is None:
            raise _TaskAccessDenied("execution_context_missing")
        if request.name not in TASK_TOOL_NAMES:
            # Unknown tool name -- programming/schema error, not a permission
            # issue. Raise so the wrapper maps to ERROR status.
            raise _TaskInvalidArgument(f"unknown tool: {request.name}")

        # Gate 1: tool must be in permitted_managed_tools (server-injected).
        if request.name not in context.permitted_managed_tools:
            raise _TaskAccessDenied("tool_not_permitted")

        # Gate 2: trusted_metadata.task context must exist (server-injected).
        # NEVER read from untrusted `context.metadata`.
        task_ctx = _origin_from_trusted(context)
        if task_ctx is None:
            raise _TaskAccessDenied("trusted_task_context_missing")

        # Dispatch to handler. Each handler enforces ownership as needed.
        if request.name == TASK_TOOL_SHOW:
            return await self._handle_show(request, task_ctx)
        if request.name == TASK_TOOL_COMPLETE:
            return await self._handle_complete(request, task_ctx)
        if request.name == TASK_TOOL_HEARTBEAT:
            return await self._handle_heartbeat(request, task_ctx)
        if request.name == TASK_TOOL_COMMENT:
            return await self._handle_comment(request, task_ctx)
        if request.name == TASK_TOOL_PROPOSE_CHANGE:
            return await self._handle_propose_change(request, task_ctx)
        if request.name == TASK_TOOL_FAIL:
            return await self._handle_fail(request, task_ctx)
        # Unreachable: TASK_TOOL_NAMES check above guarantees one of the 6.
        raise _TaskInvalidArgument(f"unknown tool: {request.name}")

    # -----------------------------------------------------------------
    # Handlers
    # -----------------------------------------------------------------

    async def _handle_show(
        self,
        request: ToolCallRequest,
        task_ctx: TaskContextOrigin,
    ) -> dict[str, Any]:
        # task_show 允许查看自己 claim 的 task；arguments.task_id 必须与
        # trusted task_id 一致（防御越权读取其它 task 的上下文）。
        target = str(request.arguments.get("task_id") or "")
        if target and target != task_ctx.task_id:
            # worker 试图查看其它 task -> 拒绝
            raise _TaskAccessDenied("cross_task_access_denied")
        target = target or task_ctx.task_id

        detail = await self.service.get_task_detail(target)
        if detail is None:
            return {"success": False, "error": "task not found"}
        # 限制 events 到最近 50 条（spec 约束）
        events = detail.get("events") or []
        if len(events) > _MAX_EVENTS_IN_SHOW:
            detail = {**detail, "events": events[-_MAX_EVENTS_IN_SHOW:]}
        return {"success": True, **detail}

    async def _handle_complete(
        self,
        request: ToolCallRequest,
        task_ctx: TaskContextOrigin,
    ) -> dict[str, Any]:
        summary = str(request.arguments.get("summary") or "")
        if not summary:
            raise _TaskInvalidArgument("summary is required")
        metadata = request.arguments.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        artifacts = request.arguments.get("artifacts")
        if not isinstance(artifacts, list):
            artifacts = []
        # 防御性校验 artifacts 可 JSON 序列化
        try:
            json.dumps(artifacts, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise _TaskInvalidArgument(f"artifacts not serializable: {exc}") from exc

        result = await self.service.complete(
            task_id=task_ctx.task_id,
            summary=summary,
            metadata=metadata,
            artifacts=artifacts,
        )
        return {"success": True, **result}

    async def _handle_heartbeat(
        self,
        request: ToolCallRequest,
        task_ctx: TaskContextOrigin,
    ) -> dict[str, Any]:
        note = str(request.arguments.get("note") or "")
        result = await self.service.heartbeat(
            task_id=task_ctx.task_id,
            note=note,
        )
        return {"success": True, **result}

    async def _handle_comment(
        self,
        request: ToolCallRequest,
        task_ctx: TaskContextOrigin,
    ) -> dict[str, Any]:
        # task_comment 的 task_id 必须与 trusted task_id 一致（worker 只能
        # 评论自己 claim 的 task）。跨 task 评论 -> 拒绝。
        target = str(request.arguments.get("task_id") or "")
        if target and target != task_ctx.task_id:
            raise _TaskAccessDenied("cross_task_comment_denied")
        target = target or task_ctx.task_id
        body = str(request.arguments.get("body") or "")
        if not body:
            raise _TaskInvalidArgument("body is required")

        result = await self.service.add_comment(
            task_id=target,
            body=body,
            author="worker",
        )
        return {"success": True, **result}

    async def _handle_propose_change(
        self,
        request: ToolCallRequest,
        task_ctx: TaskContextOrigin,
    ) -> dict[str, Any]:
        proposal = str(request.arguments.get("proposal") or "")
        if not proposal:
            raise _TaskInvalidArgument("proposal is required")
        # proposal_type 可选，默认 'approval'；executor 做一层 schema 级校验，
        # TaskService.propose_change 再做权威校验。
        proposal_type = str(request.arguments.get("proposal_type") or "approval")
        if proposal_type not in ("approval", "intent_request"):
            raise _TaskInvalidArgument(
                f"proposal_type must be 'approval' or 'intent_request', got {proposal_type!r}"
            )

        result = await self.service.propose_change(
            task_id=task_ctx.task_id,
            proposal=proposal,
            run_id=task_ctx.run_id,
            proposal_type=proposal_type,
        )
        return {"success": True, **result}

    async def _handle_fail(
        self,
        request: ToolCallRequest,
        task_ctx: TaskContextOrigin,
    ) -> dict[str, Any]:
        reason = str(request.arguments.get("reason") or "")
        if not reason:
            raise _TaskInvalidArgument("reason is required")
        result = await self.service.fail(task_id=task_ctx.task_id, reason=reason)
        return {"success": True, **result}


# ---------------------------------------------------------------------------
# Trusted task context origin extraction (pattern twelve)
# ---------------------------------------------------------------------------


class TaskContextOrigin:
    """Immutable view of the trusted task context fields used by the executor.

    ``write_origin`` is included for future extension (e.g. planner fork may
    use a different origin with read-only tools); the worker path always
    sets ``write_origin="worker"``.
    """

    __slots__ = ("task_id", "run_id", "claim_lock", "write_origin")

    def __init__(
        self,
        *,
        task_id: str,
        run_id: int,
        claim_lock: str,
        write_origin: str,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.claim_lock = claim_lock
        self.write_origin = write_origin


def _origin_from_trusted(ctx: ToolExecutionContext) -> TaskContextOrigin | None:
    """Read task_id / run_id / claim_lock / write_origin from
    ``ctx.trusted_metadata["task"]``.

    Returns None if the trusted task context is absent. NEVER reads from
    ``ctx.metadata`` (untrusted) -- pattern twelve.
    """
    trusted = ctx.trusted_metadata or {}
    task = trusted.get("task")
    if not isinstance(task, dict):
        return None
    task_id = task.get("task_id")
    if not task_id or not isinstance(task_id, str):
        return None
    run_id = task.get("run_id")
    if run_id is None or not isinstance(run_id, int):
        return None
    claim_lock = task.get("claim_lock")
    if not claim_lock or not isinstance(claim_lock, str):
        return None
    write_origin = task.get("write_origin")
    if not write_origin or not isinstance(write_origin, str):
        return None
    return TaskContextOrigin(
        task_id=task_id,
        run_id=run_id,
        claim_lock=claim_lock,
        write_origin=write_origin,
    )
