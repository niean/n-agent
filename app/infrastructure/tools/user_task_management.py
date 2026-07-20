"""用户侧任务工具执行器（create_task / list_tasks）。

spec: spec-260720-chat-natural-language-task.md

与 worker 的 TaskManagementToolExecutor 区别：
  - 面向对话 Agent（realtime），不是 worker（unattended）
  - 工具定义 source_type=AGENT + SAFE + managed=false：realtime（DEFAULT）可见、
    unattended（SAFE_ONLY）默认隐藏 AGENT 源工具，故 worker/judge 不可见，防递归
  - 从 ctx.session_id 取 origin_session_id，从 ctx.trusted_metadata.actor_id 取 created_by
  - 不读 untrusted ctx.metadata（模式十二 trusted-only）
  - 防递归约束：worker/judge 的 granted_tools 不得含这两个名字（spec Constraints）

错误处理：所有结果为 JSON object 且含 success；不向 Agent 泄露 traceback、数据库错误
或原始异常字符串。未知服务异常映射为稳定 task_internal_error / task_list_failed。
"""
from __future__ import annotations

import json
import time
from datetime import timezone
from typing import Any, Protocol

from app.application.task_tools import (
    USER_TASK_TOOL_CREATE,
    USER_TASK_TOOL_LIST,
    USER_TASK_TOOL_NAMES,
)
from app.domain.task import TaskConflictError, TaskStatus, TaskValidationError
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


class UserTaskServiceProtocol(Protocol):
    """用户侧工具依赖的 TaskService 子集（async）。

    具体 ``TaskService`` 已实现这两个方法并满足本 Protocol；测试以 async fake 替换。
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


class UserTaskToolExecutor(ToolExecutor):
    """Dispatches create_task / list_tasks to TaskService, session-bound."""

    def __init__(self, service: UserTaskServiceProtocol):
        self.service = service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            if context is None or not context.session_id:
                raise _UserTaskDenied("session_missing")
            if request.name == USER_TASK_TOOL_CREATE:
                payload = await self._handle_create(request, context)
            elif request.name == USER_TASK_TOOL_LIST:
                payload = await self._handle_list(request, context)
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
