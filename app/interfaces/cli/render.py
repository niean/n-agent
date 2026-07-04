from __future__ import annotations

import os
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table


def flush_console(console: Any) -> None:
    output = getattr(console, "file", None)
    flush = getattr(output, "flush", None)
    if callable(flush):
        flush()


def make_console(force_terminal: bool | None = None) -> Console:
    cli_color = os.environ.get("N_AGENT_CLI_COLOR", "").strip().lower()
    color_enabled = cli_color in {"1", "true", "yes", "on", "always"}
    no_color = bool(os.environ.get("NO_COLOR")) or not color_enabled
    return Console(no_color=no_color, force_terminal=force_terminal)


def render_markdown(text: str, console: Console) -> None:
    console.print(Markdown(text))
    flush_console(console)


def render_table(rows: list[dict[str, Any]], headers: list[str], console: Console) -> None:
    table = Table(show_header=True)
    for header in headers:
        table.add_column(header)
    for row in rows:
        table.add_row(*[str(row.get(header, "")) for header in headers])
    console.print(table)
    flush_console(console)


def render_status(text: str, level: str, console: Console) -> None:
    style = {"info": "dim", "success": "green", "warning": "yellow", "error": "red"}.get(level, "")
    console.print(text, style=style)
    flush_console(console)


def render_tool_call(tool_call: dict[str, Any], console: Console) -> None:
    name = tool_call.get("name", "")
    status = tool_call.get("status", "")
    arguments = tool_call.get("arguments", {})
    style = {"pending": "yellow", "success": "green", "error": "red"}.get(status, "")
    console.print(f"[{status}] {name} {arguments}", style=style, markup=False, highlight=False)
    flush_console(console)
