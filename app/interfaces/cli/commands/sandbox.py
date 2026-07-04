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


def run(args) -> int:
    cmd = args.sandbox_command
    if cmd == "list-active":
        return _cmd_list_active(args)
    if cmd == "list-released":
        return _cmd_list_released(args)
    if cmd == "list-history":
        return _cmd_list_history(args)
    if cmd == "release":
        return _cmd_release(args)
    if cmd == "delete-history":
        return _cmd_delete_history(args)
    if cmd == "config":
        return _cmd_config(args)
    return 0


def _disabled(payload: dict[str, Any], readonly: bool, fmt: str) -> int:
    render_action(payload, make_console(), fmt=fmt)
    return 0 if readonly else 1


def _cmd_list_active(args) -> int:
    fmt = resolve_format(args)
    service = _load_sandbox_service()
    if service is None:
        return _disabled({"error": "sandbox disabled"}, readonly=True, fmt=fmt)
    rows = asyncio.run(service.list_active_sandboxes())
    render_data(rows, make_console(), fmt=fmt)
    return 0


def _cmd_list_released(args) -> int:
    fmt = resolve_format(args)
    service = _load_sandbox_service()
    if service is None:
        return _disabled({"error": "sandbox disabled"}, readonly=True, fmt=fmt)
    rows = asyncio.run(service.list_released_sandboxes())
    render_data(rows, make_console(), fmt=fmt)
    return 0


def _cmd_list_history(args) -> int:
    fmt = resolve_format(args)
    service = _load_sandbox_service()
    if service is None:
        return _disabled({"error": "sandbox disabled"}, readonly=True, fmt=fmt)
    limit = args.limit if args.limit else 50
    rows = asyncio.run(service.list_execute_code_history(args.session_id, limit))
    render_data(rows, make_console(), fmt=fmt)
    return 0


def _cmd_release(args) -> int:
    fmt = resolve_format(args)
    service = _load_sandbox_service()
    if service is None:
        return _disabled({"error": "sandbox disabled"}, readonly=False, fmt=fmt)
    if not args.session_id:
        render_action({"error": "--session-id is required"}, make_console(), fmt=fmt)
        return 2
    try:
        result = asyncio.run(service.release_sandbox(args.session_id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    payload = {"released": args.session_id, "result": result}
    render_action(payload, make_console(), fmt=fmt)
    return 0


def _cmd_delete_history(args) -> int:
    fmt = resolve_format(args)
    service = _load_sandbox_service()
    if service is None:
        return _disabled({"error": "sandbox disabled"}, readonly=False, fmt=fmt)
    if not args.tool_call_id:
        render_action({"error": "tool-call-id is required"}, make_console(), fmt=fmt)
        return 2
    try:
        result = asyncio.run(service.delete_execute_code_history(args.tool_call_id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    payload = {"deleted": args.tool_call_id, "result": result}
    render_action(payload, make_console(), fmt=fmt)
    return 0


def _cmd_config(args) -> int:
    fmt = resolve_format(args)
    service = _load_sandbox_service()
    if service is None:
        return _disabled({"error": "sandbox disabled"}, readonly=True, fmt=fmt)
    config = asyncio.run(service.get_config())
    render_data(config, make_console(), fmt=fmt)
    return 0
