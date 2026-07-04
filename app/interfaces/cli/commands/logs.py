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


def _load_sandbox_service() -> Any:
    from app.main import build_application_services

    return build_application_services().sandbox_dashboard_service


def _load_session_service() -> Any:
    from app.main import build_application_services

    return build_application_services().session_service


def _load_schedule_service() -> Any:
    from app.main import build_application_services

    return build_application_services().schedule_service


def run(args) -> int:
    cmd = args.logs_command
    if cmd == "sandbox":
        return _cmd_sandbox(args)
    if cmd == "tools":
        return _cmd_tools(args)
    if cmd == "scheduled":
        return _cmd_scheduled(args)
    if cmd == "runs":
        return _cmd_runs(args)
    return 0


def _cmd_sandbox(args) -> int:
    fmt = resolve_format(args)
    service = _load_sandbox_service()
    if service is None:
        render_action({"warning": "sandbox disabled"}, make_console(), fmt=fmt)
        return 0
    limit = args.limit if args.limit else 50
    rows = asyncio.run(service.list_execute_code_history(args.session_id, limit))
    render_data(rows, make_console(), fmt=fmt)
    return 0


def _cmd_tools(args) -> int:
    fmt = resolve_format(args)
    if not args.session_id:
        render_action({"error": "--session-id is required"}, make_console(), fmt=fmt)
        return 2
    service = _load_session_service()
    calls = asyncio.run(service.list_tool_calls(args.session_id))
    if args.limit:
        calls = calls[:args.limit]
    render_data(calls, make_console(), fmt=fmt)
    return 0


def _cmd_scheduled(args) -> int:
    fmt = resolve_format(args)
    if not args.task_id:
        render_action({"error": "--task-id is required"}, make_console(), fmt=fmt)
        return 2
    if args.limit is not None and (args.limit < 1 or args.limit > 50):
        render_action({"error": f"limit must be between 1 and 50, got {args.limit}"}, make_console(), fmt=fmt)
        return 2
    limit = args.limit if args.limit else 10
    service = _load_schedule_service()
    try:
        rows = asyncio.run(service.list_executions(args.task_id, limit))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    render_data(rows, make_console(), fmt=fmt)
    return 0


def _cmd_runs(args) -> int:
    fmt = resolve_format(args)
    if not args.session_id:
        render_action({"error": "--session-id is required"}, make_console(), fmt=fmt)
        return 2
    service = _load_session_service()
    try:
        detail = asyncio.run(service.get_session_detail(args.session_id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    render_data(detail, make_console(), fmt=fmt)
    return 0
