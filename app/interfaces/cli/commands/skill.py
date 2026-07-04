from __future__ import annotations

import asyncio
import json
from typing import Any


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
    print(json.dumps(rows, ensure_ascii=False))
    return 0


def _cmd_view(args) -> int:
    service = _load_skill_service()
    payload = asyncio.run(service.render_view(args.name))
    if not payload.get("success"):
        return 1
    from app.interfaces.cli.render import make_console, render_markdown
    render_markdown(payload.get("content", ""), make_console())
    return 0
