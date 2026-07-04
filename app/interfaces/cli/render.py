from __future__ import annotations

import json as _json
import os
from typing import Any

import yaml as _yaml
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table


def flush_console(console: Any) -> None:
    output = getattr(console, "file", None)
    flush = getattr(output, "flush", None)
    if callable(flush):
        flush()


def resolve_format(args: Any) -> str:
    yaml_flag = getattr(args, "yaml", False)
    form_flag = getattr(args, "form", False)
    if yaml_flag:
        return "yaml"
    if form_flag:
        return "form"
    return "json"


def _emit_text(text: str) -> None:
    print(text, flush=True)


def _normalize_for_yaml(data: Any) -> Any:
    return _json.loads(_json.dumps(data, ensure_ascii=False, default=str))


def _dump_yaml(data: Any) -> str:
    return _yaml.safe_dump(
        _normalize_for_yaml(data),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def render_data(
    data: Any,
    console: Console,
    fmt: str = "json",
    headers: list[str] | None = None,
    form_renderer: Any = None,
) -> None:
    if fmt == "json":
        _emit_text(_json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    if fmt == "yaml":
        _emit_text(_dump_yaml(data))
        return
    if form_renderer is not None:
        form_renderer(data, console)
        flush_console(console)
        return
    if isinstance(data, list):
        if not data:
            console.print("[dim](empty)[/dim]")
            flush_console(console)
            return
        cols = headers or list(data[0].keys())
        render_table(data, cols, console)
        return
    if isinstance(data, dict):
        render_object(data, console, fmt="table")
        return
    console.print(str(data))
    flush_console(console)


def render_action(payload: dict[str, Any], console: Console, fmt: str = "json") -> None:
    if fmt == "json":
        _emit_text(_json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if fmt == "yaml":
        _emit_text(_dump_yaml(payload))
        return
    for k, v in payload.items():
        console.print(f"{k}: {v}")
    flush_console(console)


def render_doctor_data(items: list[dict[str, Any]], console: Console, fmt: str = "json") -> None:
    if fmt == "json":
        _emit_text(_json.dumps(items, ensure_ascii=False, indent=2, default=str))
        return
    if fmt == "yaml":
        _emit_text(_dump_yaml(items))
        return
    render_doctor_report(items, console)


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


def render_object(obj: dict[str, Any], console: Console, fmt: str = "table") -> None:
    if fmt == "json":
        console.print(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif fmt == "yaml":
        console.print(_dump_yaml(obj))
    else:
        table = Table(show_header=True)
        table.add_column("Field")
        table.add_column("Value")
        for k, v in obj.items():
            table.add_row(str(k), str(v))
        console.print(table)
    flush_console(console)


def render_paginated(rows: list[dict[str, Any]], headers: list[str], console: Console, page_size: int = 20) -> None:
    total = len(rows)
    if total == 0:
        console.print("[dim](empty)[/dim]")
        flush_console(console)
        return
    for start in range(0, total, page_size):
        page = rows[start:start + page_size]
        render_table(page, headers, console)
    flush_console(console)


def render_doctor_report(items: list[dict[str, Any]], console: Console) -> None:
    table = Table(show_header=True)
    table.add_column("Dimension")
    table.add_column("Status")
    table.add_column("Detail")
    style_map = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
    for it in items:
        status = str(it.get("status", ""))
        style = style_map.get(status, "")
        status_cell = f"[{style}]{status}[/{style}]" if style else status
        table.add_row(str(it.get("dimension", "")), status_cell, str(it.get("detail", "")))
    console.print(table)
    flush_console(console)
