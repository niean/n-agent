from __future__ import annotations

import asyncio
from typing import Any

from app.interfaces.cli.render import (
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
    if args.skill_command == "list":
        return _cmd_list(args)
    if args.skill_command == "view":
        return _cmd_view(args)
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
