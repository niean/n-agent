from __future__ import annotations

import json
import time
from typing import Any

from app.application.schedule_service import (
    ScheduleDeliveryContextError,
    ScheduleService,
    ScheduleValidationError,
    ScheduledTaskCreateInput,
    ScheduledTaskNotFoundError,
    ScheduledTaskNotRunnableError,
    ScheduledTaskUpdateInput,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
)


_REALTIME_MODE = "realtime"
_TOOL_NAME_MANAGE = "manage_schedule"
_TOOL_NAME_QUERY = "schedule_query"


class _ScheduleAccessDenied(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ScheduleManagementToolExecutor(ToolExecutor):
    def __init__(self, service: ScheduleService):
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
        except _ScheduleAccessDenied as exc:
            payload = {"success": False, "error": exc.code}
            status = ToolResultStatus.PERMISSION_DENIED
        except (ScheduleValidationError, ScheduleDeliveryContextError) as exc:
            payload = {"success": False, "error": str(exc)}
            status = ToolResultStatus.ERROR
        except ScheduledTaskNotFoundError:
            payload = {"success": False, "error": "task not found"}
            status = ToolResultStatus.ERROR
        except ScheduledTaskNotRunnableError as exc:
            payload = {"success": False, "error": exc.code}
            status = ToolResultStatus.ERROR
        except Exception as exc:  # pragma: no cover - defensive
            payload = {"success": False, "error": str(exc)}
            status = ToolResultStatus.ERROR
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=status,
            content=json.dumps(payload, ensure_ascii=False),
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    async def _dispatch(
        self, request: ToolCallRequest, context: ToolExecutionContext | None
    ) -> dict[str, Any]:
        action = str(request.arguments.get("action") or "")
        if context is None:
            raise _ScheduleAccessDenied("execution_context_missing")
        if context.execution_context_mode != _REALTIME_MODE:
            raise _ScheduleAccessDenied("execution_context_not_realtime")
        origin = _origin_from_trusted(context.trusted_metadata)
        if origin is None:
            raise _ScheduleAccessDenied("trusted_origin_missing")
        if request.name == _TOOL_NAME_QUERY:
            return await self._handle_query(action, request, origin)
        if request.name == _TOOL_NAME_MANAGE:
            return await self._handle_manage(action, request, context, origin)
        return {"success": False, "error": f"unknown tool: {request.name}"}

    async def _handle_query(
        self, action: str, request: ToolCallRequest, origin: dict[str, str]
    ) -> dict[str, Any]:
        if action == "list":
            tasks = [t for t in await self.service.list() if _origin_matches(t.origin, origin)]
            return {"success": True, "tasks": [_serialize(t) for t in tasks]}
        if action == "get":
            task_id = str(request.arguments.get("task_id") or "")
            if not task_id:
                return {"success": False, "error": "task_id required"}
            try:
                task = await self.service.get(task_id)
            except ScheduledTaskNotFoundError:
                return {"success": False, "error": "task not found"}
            if not _origin_matches(task.origin, origin):
                return {"success": False, "error": "task not found"}
            return {"success": True, "task": _serialize(task)}
        return {"success": False, "error": f"unknown action: {action}"}

    async def _handle_manage(
        self,
        action: str,
        request: ToolCallRequest,
        context: ToolExecutionContext,
        origin: dict[str, str],
    ) -> dict[str, Any]:
        if action == "remove":
            return await self._remove(request, origin)
        if action == "create":
            return await self._create(request, context, origin)
        task_id = str(request.arguments.get("task_id") or "")
        if not task_id:
            return {"success": False, "error": "task_id required"}
        try:
            task = await self.service.get(task_id)
        except ScheduledTaskNotFoundError:
            return {"success": False, "error": "task not found"}
        if not _origin_matches(task.origin, origin):
            return {"success": False, "error": "task not found"}
        if action == "update":
            updated = await self.service.update(
                task_id,
                ScheduledTaskUpdateInput(
                    name=request.arguments.get("name"),
                    prompt=request.arguments.get("prompt"),
                    cron_expression=request.arguments.get("cron_expression"),
                    timezone=request.arguments.get("timezone"),
                ),
            )
            return {"success": True, "task": _serialize(updated)}
        if action == "pause":
            t = await self.service.pause(task_id)
            return {"success": True, "task": _serialize(t)}
        if action == "resume":
            t = await self.service.resume(task_id)
            return {"success": True, "task": _serialize(t)}
        if action == "run":
            res = await self.service.run_now(task_id)
            return {"success": True, "result": res}
        return {"success": False, "error": f"unknown action: {action}"}

    async def _create(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext,
        origin: dict[str, str],
    ) -> dict[str, Any]:
        prompt = str(request.arguments.get("prompt") or "")
        cron = str(request.arguments.get("cron_expression") or "")
        if not prompt or not cron:
            return {"success": False, "error": "prompt and cron_expression are required"}
        name = str(request.arguments.get("name") or prompt[:40] or "Scheduled Task")
        tz = str(request.arguments.get("timezone") or "Asia/Shanghai")
        task = await self.service.create(
            ScheduledTaskCreateInput(
                name=name,
                prompt=prompt,
                cron_expression=cron,
                timezone=tz,
                delivery_target="origin",
                origin=dict(origin),
                session_id=context.session_id,
            )
        )
        return {"success": True, "task": _serialize(task)}

    async def _remove(self, request: ToolCallRequest, origin: dict[str, str]) -> dict[str, Any]:
        task_id = str(request.arguments.get("task_id") or "")
        if not task_id:
            return {"success": False, "error": "task_id required"}
        try:
            task = await self.service.get(task_id)
        except ScheduledTaskNotFoundError:
            return {"success": False, "error": "task not found"}
        if not _origin_matches(task.origin, origin):
            return {"success": False, "error": "task not found"}
        return {
            "success": True,
            "confirmation_required": True,
            "instruction": f"请发送 /schedule remove {task_id} 走确认卡删除任务",
        }


def _origin_from_trusted(trusted: dict[str, Any]) -> dict[str, str] | None:
    receive_id = trusted.get("receive_id")
    receive_id_type = trusted.get("receive_id_type")
    if not receive_id or not receive_id_type:
        return None
    source_type = str(trusted.get("source_type") or trusted.get("gateway.source_type") or "")
    return {
        "source_type": source_type,
        "receive_id": str(receive_id),
        "receive_id_type": str(receive_id_type),
        "thread_id": str(trusted.get("thread_id") or ""),
    }


def _origin_matches(origin: dict[str, Any], expected: dict[str, str]) -> bool:
    if not origin:
        return False
    return (
        str(origin.get("receive_id") or "") == expected["receive_id"]
        and str(origin.get("receive_id_type") or "") == expected["receive_id_type"]
        and str(origin.get("thread_id") or "") == expected["thread_id"]
    )


def _serialize(task) -> dict[str, Any]:
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "cron_expression": task.schedule.value,
        "timezone": task.timezone.value,
        "status": task.status.value,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
    }
