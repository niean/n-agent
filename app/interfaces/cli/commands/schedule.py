from __future__ import annotations

import asyncio
import json
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_action,
    render_data,
    resolve_format,
)


def _load_schedule_service() -> Any:
    from app.main import build_application_services

    return build_application_services().schedule_service


def run(args) -> int:
    cmd = args.schedule_command
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "get":
        return _cmd_get(args)
    if cmd == "create":
        return _cmd_create(args)
    if cmd == "update":
        return _cmd_update(args)
    if cmd == "pause":
        return _cmd_simple(args, "pause")
    if cmd == "resume":
        return _cmd_simple(args, "resume")
    if cmd == "run":
        return _cmd_simple(args, "run_now")
    if cmd == "delete":
        return _cmd_delete(args)
    if cmd == "executions":
        return _cmd_executions(args)
    return 0


def _to_dict(task: Any) -> dict[str, Any]:
    status = getattr(task, "status", None)
    schedule = getattr(task, "schedule", None)
    tz = getattr(task, "timezone", None)
    delivery = getattr(task, "delivery_target", None)
    return {
        "id": task.id,
        "name": task.name,
        "cron": schedule.value if hasattr(schedule, "value") else (str(schedule) if schedule else ""),
        "timezone": tz.value if hasattr(tz, "value") else (str(tz) if tz else ""),
        "next_run_at": str(task.next_run_at) if getattr(task, "next_run_at", None) else "",
        "status": status.value if hasattr(status, "value") else (str(status) if status else ""),
        "enabled": getattr(task, "enabled", None),
        "delivery_target": delivery.target_type.value if hasattr(delivery, "target_type") else (str(delivery) if delivery else ""),
        "session_id": getattr(task, "session_id", None),
    }


_LIST_HEADERS = ["id", "name", "cron", "timezone", "next_run_at", "status", "enabled", "delivery_target"]

_EXECUTION_HEADERS = [
    "id", "task_id", "status", "started_at", "completed_at", "delivery_status", "error",
]


def _execution_to_dict(exec_: Any) -> dict[str, Any]:
    status = getattr(exec_, "status", None)
    return {
        "id": exec_.id,
        "task_id": exec_.task_id,
        "session_id": getattr(exec_, "session_id", None),
        "claim_id": getattr(exec_, "claim_id", None),
        "lease_owner": getattr(exec_, "lease_owner", None),
        "status": status.value if hasattr(status, "value") else (str(status) if status else ""),
        "claimed_next_run_at": str(exec_.claimed_next_run_at) if getattr(exec_, "claimed_next_run_at", None) else "",
        "started_at": str(exec_.started_at) if getattr(exec_, "started_at", None) else "",
        "completed_at": str(exec_.completed_at) if getattr(exec_, "completed_at", None) else "",
        "output": getattr(exec_, "output", None),
        "error": getattr(exec_, "error", None),
        "delivery_status": getattr(exec_, "delivery_status", None),
        "delivery_error": getattr(exec_, "delivery_error", None),
        "created_at": str(exec_.created_at) if getattr(exec_, "created_at", None) else "",
    }


def _cmd_list(args) -> int:
    service = _load_schedule_service()
    tasks = asyncio.run(service.list())
    rows = [_to_dict(t) for t in tasks]
    render_data(rows, make_console(), fmt=resolve_format(args), headers=_LIST_HEADERS)
    return 0


def _cmd_get(args) -> int:
    service = _load_schedule_service()
    try:
        task = asyncio.run(service.get(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(task), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_create(args) -> int:
    from app.application.schedule_service import ScheduledTaskCreateInput

    payload = ScheduledTaskCreateInput(
        name=args.name,
        prompt=args.prompt,
        cron_expression=args.cron,
        timezone=args.timezone or "Asia/Shanghai",
        delivery_target=args.delivery_target or "dashboard",
    )
    service = _load_schedule_service()
    try:
        task = asyncio.run(service.create(payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(task), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_update(args) -> int:
    from app.application.schedule_service import ScheduledTaskUpdateInput

    payload = ScheduledTaskUpdateInput(
        name=args.name,
        prompt=args.prompt,
        cron_expression=args.cron,
        timezone=args.timezone,
        delivery_target=args.delivery_target,
    )
    service = _load_schedule_service()
    try:
        task = asyncio.run(service.update(args.id, payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(task), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_simple(args, method: str) -> int:
    service = _load_schedule_service()
    if method == "run_now":
        return _cmd_run_now(args, service)
    try:
        asyncio.run(getattr(service, method)(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({method: args.id}, make_console(), fmt=resolve_format(args))
    return 0


def _cmd_run_now(args, service) -> int:
    no_wait = getattr(args, "no_wait", False)
    timeout = getattr(args, "timeout", None) or 300
    fmt = resolve_format(args)
    try:
        result = asyncio.run(_run_now_and_maybe_wait(service, args.id, no_wait, timeout))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    if isinstance(result, dict) and result.get("status") == "not_claimed":
        render_action({"run_now": args.id, "status": "not_claimed"}, make_console(), fmt=fmt)
        return 1
    final_status = result.get("final_status", "") if isinstance(result, dict) else ""
    if final_status == "timeout":
        render_action({"run_now": args.id, "status": "triggered", "note": "timeout, still running"}, make_console(), fmt=fmt)
        return 0
    if final_status:
        render_action({"run_now": args.id, "status": final_status}, make_console(), fmt=fmt)
    else:
        render_action({"run_now": args.id}, make_console(), fmt=fmt)
    if final_status in ("failed", "blocked"):
        return 1
    return 0


async def _run_now_and_maybe_wait(service, task_id: str, no_wait: bool, timeout: float) -> dict:
    result = await service.run_now(task_id)
    if no_wait or not isinstance(result, dict) or result.get("status") != "triggered":
        return result
    deadline = asyncio.get_event_loop().time() + timeout
    poll_interval = 1.0
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            executions = await service.list_executions(task_id, limit=1)
        except Exception:
            executions = []
        if not executions:
            continue
        latest = executions[0]
        status = latest.get("status", "") if isinstance(latest, dict) else getattr(latest, "status", "")
        status_value = status.value if hasattr(status, "value") else str(status)
        if status_value and status_value != "running":
            return {**result, "final_status": status_value}
    return {**result, "final_status": "timeout"}


def _cmd_delete(args) -> int:
    service = _load_schedule_service()
    try:
        ok = asyncio.run(service.delete(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    if not ok:
        render_action({"error": f"delete failed: {args.id}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({"deleted": args.id}, make_console(), fmt=resolve_format(args))
    return 0


def _cmd_executions(args) -> int:
    fmt = resolve_format(args)
    if args.limit is not None and (args.limit < 1 or args.limit > 50):
        render_action({"error": f"limit must be between 1 and 50, got {args.limit}"}, make_console(), fmt=fmt)
        return 2
    limit = args.limit if args.limit else 10
    service = _load_schedule_service()
    try:
        executions = asyncio.run(service.list_executions(args.id, limit))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    rows = [_execution_to_dict(e) for e in executions]
    render_data(rows, make_console(), fmt=fmt, headers=_EXECUTION_HEADERS)
    return 0
