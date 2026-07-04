from __future__ import annotations

import asyncio
import json
from typing import Any


def _load_plugin_service() -> Any:
    from app.main import build_application_services

    return build_application_services().plugin_service


def run(args) -> int:
    if args.plugin_command == "list":
        return _cmd_list(args)
    if args.plugin_command == "view":
        return _cmd_view(args)
    return 0


def _cmd_list(args) -> int:
    service = _load_plugin_service()
    plugins = asyncio.run(service.list_plugins())
    rows = [
        {
            "key": p.key,
            "kind": p.kind.value,
            "source": p.source.value,
            "enabled": p.enabled,
            "description": p.description,
        }
        for p in plugins
    ]
    print(json.dumps(rows, ensure_ascii=False))
    return 0


def _cmd_view(args) -> int:
    service = _load_plugin_service()
    plugin = asyncio.run(service.get_plugin(args.name))
    if plugin is None:
        print(json.dumps({"success": False, "error": "plugin not found"}, ensure_ascii=False))
        return 1
    print(json.dumps(plugin.to_public_view(), ensure_ascii=False))
    return 0
