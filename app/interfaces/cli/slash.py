from __future__ import annotations

import os
from typing import Any

from app.interfaces.cli.management import is_management_command
from app.interfaces.cli.render import render_markdown, render_status

GATEWAY_COMMANDS = [
    "/new",
    "/rename",
    "/delete",
    "/tools",
    "/models",
    "/switch",
    "/sethome",
]

LOCAL_COMMANDS = ["/help", "/exit", "/clear", "/history", "/confirm", "/cancel"]

LOCAL_ONLY_PREFIXES = ("/help", "/exit", "/clear", "/history", "/confirm", "/cancel")


def is_local_command(text: str) -> bool:
    stripped = text.strip()
    if any(stripped == p or stripped.startswith(p + " ") for p in LOCAL_ONLY_PREFIXES):
        return True
    return is_management_command(stripped)


def handle_local_command(text: str, console: Any, history_path: str | None = None) -> int | None:
    """Return int if handled; None if not a local command."""
    stripped = text.strip()
    if stripped == "/help":
        _print_help(console, history_path)
        return 0
    if stripped == "/exit":
        return 0
    if stripped == "/clear":
        os.system("clear" if os.name == "posix" else "cls")
        return 0
    if stripped == "/history":
        if history_path and os.path.exists(history_path):
            render_status(f"history file: {history_path}", "info", console)
        else:
            render_status("no history yet", "info", console)
        return 0
    return None


def _print_help(console: Any, history_path: str | None) -> None:
    help_text = """# N-Agent CLI Commands

## Local
- /help - show this help
- /exit - exit REPL
- /clear - clear screen
- /history - show history file path
- /confirm once - confirm last destructive command (ONCE)
- /confirm trust - trust current session (TRUST_SESSION)
- /cancel - cancel last destructive command

## Management (local, direct service call)
- /provider list|get|create|update|delete|activate
- /knowledge list|get|create|update|delete|probe
- /mcp list|get|create|update|delete|probe|refresh|tools|toggle
- /schedule list|get|create|update|pause|resume|run|delete|executions
- /sandbox list-active|list-released|list-history|release|delete-history|config
- /memory list-providers|... (see --help)
- /platform list|get|sessions
- /skill list|view
- /plugin list|view
- /status - local health snapshot (JSON)
- /sessions [--browse [--pick <id>]] - list sessions for current conversation
- /doctor [--probe]
- /config [--section] [--json]
- /logs sandbox|tools|scheduled|runs
- Tip: append --help to any management command for details (e.g., /provider create --help)

## Gateway
- /new, /rename, /delete, /tools, /models, /switch, /sethome
"""
    render_markdown(help_text, console)
