from __future__ import annotations

import asyncio
from typing import Any

from app.interfaces.cli.render import (
    flush_console,
    make_console,
    render_action,
    render_data,
    render_markdown,
    resolve_format,
)


def _load_skill_service() -> Any:
    from app.main import build_application_services

    return build_application_services().skill_service


def run(args) -> int:
    cmd = args.skill_command
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "view":
        return _cmd_view(args)
    if cmd == "pending":
        return _cmd_pending(args)
    if cmd == "diff":
        return _cmd_diff(args)
    if cmd == "approve":
        return _cmd_approve(args)
    if cmd == "reject":
        return _cmd_reject(args)
    if cmd == "approve-all":
        return _cmd_approve_all(args)
    if cmd == "reject-all":
        return _cmd_reject_all(args)
    if cmd == "pin":
        return _cmd_pin(args)
    if cmd == "unpin":
        return _cmd_unpin(args)
    if cmd == "usage":
        return _cmd_usage(args)
    return 0


def _cmd_list(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    skills = asyncio.run(service.list_skills(include_disabled=True))
    rows = [
        {
            "name": s.name,
            "readiness": s.readiness.value if hasattr(s.readiness, "value") else str(s.readiness),
            "enabled": s.enabled,
            "description": s.description,
        }
        for s in skills
    ]
    render_data(rows, make_console(), fmt=fmt, headers=["name", "readiness", "enabled", "description"])
    return 0


def _cmd_view(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    payload = asyncio.run(service.render_view(args.name))
    if not payload.get("success"):
        render_action(payload or {"success": False, "error": "skill not found"}, make_console(), fmt=fmt)
        return 1
    if fmt == "form":
        render_markdown(payload.get("content", ""), make_console())
        return 0
    render_data(payload, make_console(), fmt=fmt)
    return 0


# ------------------------------------------------------------------
# pending / approval subcommands
# ------------------------------------------------------------------

_PENDING_HEADERS = [
    "pending_id", "skill_name", "action", "origin", "summary", "state", "created_at",
]

_USAGE_HEADERS = [
    "name", "created_by", "state", "pinned", "use_count", "view_count", "patch_count",
]


def _pending_to_dict(pw) -> dict:
    return {
        "pending_id": pw.pending_id,
        "action": pw.action.value if hasattr(pw.action, "value") else str(pw.action),
        "skill_name": pw.skill_name,
        "origin": pw.origin.value if hasattr(pw.origin, "value") else str(pw.origin),
        "summary": pw.summary,
        "state": pw.state,
        "created_at": pw.created_at.isoformat() if pw.created_at else None,
    }


def _manage_result_to_dict(result) -> dict:
    return {
        "success": result.success,
        "staged": result.staged,
        "pending_id": result.pending_id,
        "skill_name": result.skill_name,
        "action": result.action.value if hasattr(result.action, "value") else str(result.action),
        "summary": result.summary,
        "diff": result.diff,
        "error": result.error,
    }


def _usage_to_dict(name: str, usage) -> dict:
    return {
        "name": name,
        "created_by": usage.created_by,
        "state": usage.state,
        "pinned": usage.pinned,
        "use_count": usage.use_count,
        "view_count": usage.view_count,
        "patch_count": usage.patch_count,
    }


def _cmd_pending(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    items = asyncio.run(service.list_pending())
    rows = [_pending_to_dict(pw) for pw in items]
    render_data(rows, make_console(), fmt=fmt, headers=_PENDING_HEADERS)
    return 0


def _cmd_diff(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    pw = asyncio.run(service.get_pending(args.pending_id))
    if pw is None:
        render_action(
            {"success": False, "error": "pending write not found", "pending_id": args.pending_id},
            make_console(), fmt=fmt,
        )
        return 1
    if fmt == "form":
        console = make_console()
        console.print(pw.diff or "(no diff)")
        flush_console(console)
        return 0
    render_data(
        {"pending_id": pw.pending_id, "summary": pw.summary, "diff": pw.diff},
        make_console(), fmt=fmt,
    )
    return 0


def _cmd_approve(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    result = asyncio.run(service.approve_pending(args.pending_id))
    payload = _manage_result_to_dict(result)
    render_action(payload, make_console(), fmt=fmt)
    return 0 if result.success else 1


def _cmd_reject(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    ok = asyncio.run(service.reject_pending(args.pending_id))
    render_action({"rejected": ok, "pending_id": args.pending_id}, make_console(), fmt=fmt)
    return 0 if ok else 1


def _cmd_approve_all(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    count = asyncio.run(service.approve_all_pending())
    render_action({"approved": count}, make_console(), fmt=fmt)
    return 0


def _cmd_reject_all(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    count = asyncio.run(service.reject_all_pending())
    render_action({"rejected": count}, make_console(), fmt=fmt)
    return 0


def _cmd_pin(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    try:
        asyncio.run(service.set_pinned(args.name, True))
    except Exception as exc:
        render_action({"success": False, "error": str(exc)}, make_console(), fmt=fmt)
        return 1
    render_action({"pinned": True, "name": args.name}, make_console(), fmt=fmt)
    return 0


def _cmd_unpin(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    try:
        asyncio.run(service.set_pinned(args.name, False))
    except Exception as exc:
        render_action({"success": False, "error": str(exc)}, make_console(), fmt=fmt)
        return 1
    render_action({"pinned": False, "name": args.name}, make_console(), fmt=fmt)
    return 0


def _cmd_usage(args) -> int:
    fmt = resolve_format(args)
    service = _load_skill_service()
    items = asyncio.run(service.list_usage())
    rows = [_usage_to_dict(name, usage) for name, usage in items]
    render_data(rows, make_console(), fmt=fmt, headers=_USAGE_HEADERS)
    return 0
