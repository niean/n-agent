from __future__ import annotations

import asyncio
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_action,
    render_data,
    resolve_format,
)


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
    fmt = resolve_format(args)
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
    render_data(rows, make_console(), fmt=fmt, headers=["key", "kind", "source", "enabled", "description"])
    return 0


def _cmd_view(args) -> int:
    fmt = resolve_format(args)
    service = _load_plugin_service()
    plugin = asyncio.run(service.get_plugin(args.name))
    if plugin is None:
        render_action({"success": False, "error": "plugin not found"}, make_console(), fmt=fmt)
        return 1
    render_data(plugin.to_public_view(), make_console(), fmt=fmt)
    return 0
