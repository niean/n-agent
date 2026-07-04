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


def _load_platform_service() -> Any:
    from app.main import build_application_services

    return build_application_services().platform_service


def run(args) -> int:
    cmd = args.platform_command
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "get":
        return _cmd_get(args)
    if cmd == "sessions":
        return _cmd_sessions(args)
    return 0


def _view_to_dict(view: Any) -> dict[str, Any]:
    platform = view.platform
    kind = view.kind
    last_active = view.last_active_at
    return {
        "platform": platform.value if hasattr(platform, "value") else str(platform),
        "display_name": view.display_name,
        "kind": kind.value if hasattr(kind, "value") else str(kind),
        "status": view.status,
        "session_count": view.session_count,
        "last_active_at": str(last_active) if last_active else "",
    }


def _redact_session_id(sid: str) -> str:
    if not sid or len(sid) <= 8:
        return sid
    return f"{sid[:4]}***{sid[-4:]}"


def _conversation_to_dict(conv: Any) -> dict[str, Any]:
    updated_at = getattr(conv, "updated_at", None) or getattr(conv, "created_at", None)
    return {
        "platform_session_id": _redact_session_id(getattr(conv, "platform_session_id", "") or ""),
        "active_session_id": getattr(conv, "active_session_id", "") or "",
        "updated_at": str(updated_at) if updated_at else "",
    }


_LIST_HEADERS = ["platform", "display_name", "status", "session_count", "last_active_at"]
_SESSION_HEADERS = ["platform_session_id", "active_session_id", "updated_at"]


def _cmd_list(args) -> int:
    service = _load_platform_service()
    views = asyncio.run(service.list_platforms(args.include_local))
    rows = [_view_to_dict(v) for v in views]
    render_data(rows, make_console(), fmt=resolve_format(args), headers=_LIST_HEADERS)
    return 0


def _cmd_get(args) -> int:
    service = _load_platform_service()
    try:
        detail = asyncio.run(service.get_platform(args.platform))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    obj = _view_to_dict(detail.platform)
    obj["total_sessions"] = detail.total_sessions
    obj["active_sessions"] = detail.active_sessions
    render_data(obj, make_console(), fmt=resolve_format(args))
    return 0


def _cmd_sessions(args) -> int:
    service = _load_platform_service()
    limit = args.limit if args.limit else 20
    offset = args.offset if args.offset else 0
    try:
        page = asyncio.run(service.list_platform_sessions(args.platform, limit, offset))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    rows = [_conversation_to_dict(c) for c in page.items]
    render_data(rows, make_console(), fmt=resolve_format(args), headers=_SESSION_HEADERS)
    return 0
