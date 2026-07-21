"""用户侧任务工具执行器（create_task / list_tasks / approve_task /
reject_task / revise_task）。

spec: spec-260720-chat-natural-language-task.md, spec-260721-chat-nl-approval.md

与 worker 的 TaskManagementToolExecutor 区别：
  - 面向对话 Agent（realtime），不是 worker（unattended）
  - 工具定义 source_type=AGENT + SAFE + managed=false：realtime（DEFAULT）可见、
    unattended（SAFE_ONLY）默认隐藏 AGENT 源工具，故 worker/judge 不可见，防递归
  - 从 ctx.session_id 取 origin_session_id，从 ctx.trusted_metadata.actor_id 取 created_by
  - 不读 untrusted ctx.metadata（模式十二 trusted-only）
  - 防递归约束：worker/judge 的 granted_tools 不得含这些工具名（spec Constraints）

审批工具（approve/reject/revise）会话隔离：
  - 只读 ctx.session_id 定位同会话任务；task_id 缺省取最近 WAITING_APPROVAL 任务
  - 指定 task_id 校验 origin_session_id == ctx.session_id 且未归档
  - 跨会话、归档、不存在统一 task_not_found，不泄露存在性差异
  - status/decision 取 service 提交结果，不硬编码；terminal=False

错误处理：所有结果为 JSON object 且含 success；不向 Agent 泄露 traceback、数据库错误
或原始异常字符串。未知服务异常映射为稳定 task_internal_error / task_list_failed。
"""
from __future__ import annotations

import json
import time
from datetime import timezone
from typing import Any, Protocol

from app.application.task_tools import (
    USER_TASK_APPROVAL_TOOL_NAMES,
    USER_TASK_TOOL_APPROVE,
    USER_TASK_TOOL_CREATE,
    USER_TASK_TOOL_LIST,
    USER_TASK_TOOL_REJECT,
    USER_TASK_TOOL_REVISE,
    USER_TASK_TOOL_NAMES,
)
from app.domain.task import (
    Task,
    TaskConflictError,
    TaskNotFoundError,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
)


_TITLE_MAX_CODEPOINTS = 80
_LIST_PAGE_LIMIT = 200
_TASK_STATUS_VALUES = frozenset(s.value for s in TaskStatus)
_NOTE_MAX_CODEPOINTS = 2000
_APPROVAL_ALLOWED_FIELDS = frozenset({"task_id", "note"})
_APPROVAL_DECISION_APPROVE = "approved"
_APPROVAL_DECISION_REJECT = "rejected"
_APPROVAL_DECISION_REVISE = "revised"


class UserTaskServiceProtocol(Protocol):
    """用户侧工具依赖的 TaskService 子集（async）。

    具体 ``TaskService`` 已实现这些方法并满足本 Protocol；测试以 async fake 替换。
    不复用 worker 的 ``TaskServiceProtocol``（后者是 worker 导向，不含 create/list）。
    """

    async def create_task(
        self,
        *,
        title: str,
        body: str = "",
        priority: int = 0,
        created_by: str = "",
        board: str = "default",
        idempotency_key: str | None = None,
        origin_session_id: str | None = None,
        skills: tuple[str, ...] | list[str] | None = None,
        goal_mode: bool = False,
        **kwargs: Any,
    ) -> Any: ...

    async def list_tasks(
        self, board: str = "default", cursor: Any = None, limit: int = 100,
    ) -> Any: ...

    async def get_task(self, task_id: str) -> Task:
        """Return the Task for ``task_id`` or raise ``TaskNotFoundError``."""

    async def latest_waiting_approval_in_session(
        self, session_id: str,
    ) -> Task | None:
        """Return the most recent WAITING_APPROVAL Task in ``session_id``, or None."""

    async def approve_change(
        self, task_id: str, note: str | None = None,
    ) -> dict[str, Any]: ...

    async def reject_change(
        self, task_id: str, note: str | None = None,
    ) -> dict[str, Any]: ...

    async def revise_change(
        self, task_id: str, note: str | None = None,
    ) -> dict[str, Any]: ...


class UserTaskToolExecutor(ToolExecutor):
    """Dispatches create_task / list_tasks / approve_task / reject_task /
    revise_task to TaskService, session-bound.

    审批工具（approve/reject/revise）定位只读 ``context.session_id``：task_id
    缺省时取当前会话最近一个 WAITING_APPROVAL 任务；指定 task_id 时校验
    ``origin_session_id == ctx.session_id`` 且未归档。跨会话、归档、不存在统一
    ``task_not_found``，不泄露存在性差异。status/decision 取 service 提交结果，
    不硬编码。``terminal=False``：审批决策不是对话终态。
    """

    def __init__(self, service: UserTaskServiceProtocol):
        self.service = service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            if context is None or not (context.session_id or "").strip():
                raise _UserTaskDenied("session_missing")
            if request.name == USER_TASK_TOOL_CREATE:
                payload = await self._handle_create(request, context)
            elif request.name == USER_TASK_TOOL_LIST:
                payload = await self._handle_list(request, context)
            elif request.name == USER_TASK_TOOL_APPROVE:
                payload = await self._handle_approve(request, context)
            elif request.name == USER_TASK_TOOL_REJECT:
                payload = await self._handle_reject(request, context)
            elif request.name == USER_TASK_TOOL_REVISE:
                payload = await self._handle_revise(request, context)
            else:
                raise _UserTaskInvalid(f"unknown tool: {request.name}")
            status = ToolResultStatus.SUCCESS
        except _UserTaskDenied as exc:
            payload = {"success": False, "error": exc.code}
            status = ToolResultStatus.PERMISSION_DENIED
        except _UserTaskInvalid as exc:
            payload = {"success": False, "error": str(exc)}
            status = ToolResultStatus.ERROR
        except _UserTaskListFailed:
            payload = {
                "success": False,
                "error": "task_list_failed",
                "items": [],
                "count": 0,
            }
            status = ToolResultStatus.ERROR
        except TaskNotFoundError:
            payload = {"success": False, "error": "task_not_found"}
            status = ToolResultStatus.ERROR
        except TaskStateError:
            payload = {"success": False, "error": "task_state_invalid"}
            status = ToolResultStatus.ERROR
        except TaskValidationError:
            payload = {"success": False, "error": "task_invalid"}
            status = ToolResultStatus.ERROR
        except TaskConflictError:
            payload = {"success": False, "error": "task_conflict"}
            status = ToolResultStatus.ERROR
        except Exception:  # defensive: 不向 Agent 泄露内部异常文本
            payload = {"success": False, "error": "task_internal_error"}
            status = ToolResultStatus.ERROR
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=status,
            content=json.dumps(payload, ensure_ascii=False, default=str),
            duration_ms=int((time.monotonic() - start) * 1000),
            terminal=False,
        )

    # ------------------------------------------------------------------
    # create_task
    # ------------------------------------------------------------------

    async def _handle_create(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}

        goal = args.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise _UserTaskInvalid("goal_required")

        raw_title = args.get("title")
        if raw_title is None or (isinstance(raw_title, str) and not raw_title.strip()):
            title = _derive_title(goal)
        elif isinstance(raw_title, str) and raw_title.strip():
            title = _truncate(raw_title.strip(), _TITLE_MAX_CODEPOINTS)
        else:
            raise _UserTaskInvalid("title_invalid")

        priority = args.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
            raise _UserTaskInvalid("invalid_arguments")

        goal_mode = args.get("goal_mode", False)
        if not isinstance(goal_mode, bool):
            raise _UserTaskInvalid("invalid_arguments")

        skills_raw = args.get("skills")
        if skills_raw is None:
            skills: tuple[str, ...] = ()
        elif isinstance(skills_raw, list) and all(
            isinstance(s, str) and s for s in skills_raw
        ):
            seen: list[str] = []
            for s in skills_raw:
                if s not in seen:
                    seen.append(s)
            skills = tuple(seen)
        else:
            raise _UserTaskInvalid("invalid_arguments")

        origin_session_id = ctx.session_id
        created_by = str((ctx.trusted_metadata or {}).get("actor_id") or "chat")
        idempotency_key = f"chat:{origin_session_id}:{request.id}"

        task = await self.service.create_task(
            title=title,
            body=goal,
            priority=priority,
            created_by=created_by,
            origin_session_id=origin_session_id,
            idempotency_key=idempotency_key,
            skills=skills,
            goal_mode=goal_mode,
        )
        return {
            "success": True,
            "task": {
                "id": getattr(task, "id", ""),
                "title": getattr(task, "title", ""),
                "status": getattr(getattr(task, "status", None), "value", ""),
                "goal_mode": bool(getattr(task, "goal_mode", False)),
            },
        }

    # ------------------------------------------------------------------
    # list_tasks
    # ------------------------------------------------------------------

    async def _handle_list(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        args = request.arguments or {}
        status_filter = args.get("status")
        if status_filter is not None:
            if (
                not isinstance(status_filter, str)
                or status_filter not in _TASK_STATUS_VALUES
            ):
                raise _UserTaskInvalid("invalid_status")

        items: list[Any] = []
        cursor: Any = None
        # 只把服务调用纳入异常映射；过滤逻辑的 bug 应向上传播，不被静默吞掉。
        while True:
            try:
                page = await self.service.list_tasks(
                    board="default", cursor=cursor, limit=_LIST_PAGE_LIMIT,
                )
            except Exception:
                raise _UserTaskListFailed()
            page_items = getattr(page, "items", ()) or ()
            for t in page_items:
                if getattr(t, "origin_session_id", None) != ctx.session_id:
                    continue
                if getattr(t, "is_archived", False):
                    continue
                if status_filter is not None and (
                    getattr(getattr(t, "status", None), "value", None)
                    != status_filter
                ):
                    continue
                items.append(t)
            cursor = getattr(page, "next_cursor", None)
            if cursor is None:
                break

        return {
            "success": True,
            "items": [
                {
                    "id": getattr(t, "id", ""),
                    "title": getattr(t, "title", ""),
                    "status": getattr(getattr(t, "status", None), "value", ""),
                    "created_at": _dt_str(getattr(t, "created_at", None)),
                }
                for t in items
            ],
            "count": len(items),
        }

    # ------------------------------------------------------------------
    # approve_task / reject_task / revise_task
    # ------------------------------------------------------------------

    async def _handle_approve(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        task_id_raw, note = self._parse_approval_args(request, required=False)
        task = await self._resolve_task(task_id_raw, ctx)
        result = await self.service.approve_change(task.id, note=note)
        return _approval_success_payload(task, result)

    async def _handle_reject(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        task_id_raw, note = self._parse_approval_args(request, required=False)
        task = await self._resolve_task(task_id_raw, ctx)
        result = await self.service.reject_change(task.id, note=note)
        return _approval_success_payload(task, result)

    async def _handle_revise(
        self, request: ToolCallRequest, ctx: ToolExecutionContext,
    ) -> dict[str, Any]:
        task_id_raw, note = self._parse_approval_args(request, required=True)
        task = await self._resolve_task(task_id_raw, ctx)
        result = await self.service.revise_change(task.id, note)
        return _approval_success_payload(task, result)

    # ------------------------------------------------------------------
    # approval helpers
    # ------------------------------------------------------------------

    def _parse_approval_args(
        self, request: ToolCallRequest, *, required: bool,
    ) -> tuple[str | None, str | None]:
        """Parse and validate ``task_id`` + ``note`` from approval tool arguments.

        Returns ``(task_id_raw, note)`` where ``task_id_raw`` is ``None`` when
        absent (caller delegates to ``latest_waiting_approval_in_session``),
        or a trimmed non-empty string when specified. ``note`` is ``None`` for
        approve/reject when blank, or a trimmed string. Raises
        ``_UserTaskInvalid`` with a stable code on any schema violation.
        """
        args = request.arguments
        if not isinstance(args, dict):
            raise _UserTaskInvalid("invalid_arguments")
        # 拒绝未知字段（schema 绕过防御）
        if any(k not in _APPROVAL_ALLOWED_FIELDS for k in args):
            raise _UserTaskInvalid("invalid_arguments")

        raw_task_id = args.get("task_id")
        task_id: str | None
        if raw_task_id is None:
            task_id = None
        elif isinstance(raw_task_id, str):
            trimmed_id = raw_task_id.strip()
            if not trimmed_id:
                raise _UserTaskInvalid("invalid_arguments")
            task_id = trimmed_id
        else:
            raise _UserTaskInvalid("invalid_arguments")

        note = _validate_note(args.get("note"), required=required)
        return task_id, note

    async def _resolve_task(
        self, task_id: str | None, ctx: ToolExecutionContext,
    ) -> Task:
        """Locate the target Task for an approval decision.

        - ``task_id`` is ``None``: delegate to
          ``latest_waiting_approval_in_session(ctx.session_id)``; no candidate
          -> ``no_waiting_approval``.
        - ``task_id`` specified: call ``get_task`` and verify
          ``origin_session_id == ctx.session_id`` and ``not is_archived``;
          cross-session / archived / not-found all map to ``task_not_found``
          without leaking which case occurred.

        ``TaskNotFoundError`` from ``get_task`` is normalized here so the
        handler's service call (which may raise ``TaskNotFoundError`` in a
        race) is still caught by ``execute``.
        """
        if task_id is None:
            task = await self.service.latest_waiting_approval_in_session(
                ctx.session_id,
            )
            if task is None:
                raise _UserTaskInvalid("no_waiting_approval")
            return task

        try:
            task = await self.service.get_task(task_id)
        except TaskNotFoundError:
            raise _UserTaskInvalid("task_not_found")
        if (
            getattr(task, "origin_session_id", None) != ctx.session_id
            or getattr(task, "is_archived", False)
        ):
            raise _UserTaskInvalid("task_not_found")
        return task


# ---------------------------------------------------------------------------
# Internal exceptions
# ---------------------------------------------------------------------------


class _UserTaskDenied(Exception):
    """Access denied (maps to PERMISSION_DENIED)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _UserTaskInvalid(Exception):
    """Invalid tool call (maps to ERROR)."""


class _UserTaskListFailed(Exception):
    """list_tasks 服务调用失败（映射为 ERROR + task_list_failed + 空 items）。"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_title(goal: str) -> str:
    for line in goal.splitlines():
        s = line.strip()
        if s:
            return _truncate(s, _TITLE_MAX_CODEPOINTS)
    return "未命名任务"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _dt_str(dt: Any) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        if getattr(dt, "tzinfo", None) is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def _validate_note(value: Any, *, required: bool) -> str | None:
    """Normalize an approval ``note`` argument.

    - ``None`` (absent or explicit null): ``required`` -> ``note_required``;
      otherwise -> ``None``.
    - Non-string -> ``invalid_arguments``.
    - Trimmed empty: ``required`` -> ``note_required``; otherwise -> ``None``.
    - Trimmed length > ``_NOTE_MAX_CODEPOINTS`` -> ``note_too_long``.
    - Otherwise -> trimmed string.
    """
    if value is None:
        if required:
            raise _UserTaskInvalid("note_required")
        return None
    if not isinstance(value, str):
        raise _UserTaskInvalid("invalid_arguments")
    trimmed = value.strip()
    if not trimmed:
        if required:
            raise _UserTaskInvalid("note_required")
        return None
    if len(trimmed) > _NOTE_MAX_CODEPOINTS:
        raise _UserTaskInvalid("note_too_long")
    return trimmed


def _approval_success_payload(task: Task, result: dict[str, Any]) -> dict[str, Any]:
    """Build the whitelist success response for an approval decision.

    ``id``/``title`` come from the pre-decision Task snapshot (read by
    ``_resolve_task``); ``status``/``decision`` come from the service's
    committed result -- never hardcoded.
    """
    return {
        "success": True,
        "task": {
            "id": getattr(task, "id", ""),
            "title": getattr(task, "title", ""),
            "status": result.get("status", ""),
            "decision": result.get("decision", ""),
        },
    }
