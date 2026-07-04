from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.domain.gateway import GatewaySessionKey
from app.domain.platform import Platform
from app.interfaces.cli.gateway_client import GatewayCliClient
from app.interfaces.cli.render import (
    make_console,
    render_data,
    render_markdown,
    resolve_format,
)


def _build_services() -> Any:
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    if args.conversation_id and args.session_source and args.conversation_id != args.session_source:
        return 2
    conversation_id = args.conversation_id or args.session_source or "local"
    services = _build_services()
    if getattr(args, "browse", False):
        return _run_browse(services, conversation_id, args)
    return asyncio.run(_send(services, conversation_id, args))


async def _send(services: Any, conversation_id: str, args) -> int:
    fmt = resolve_format(args)
    client = GatewayCliClient(services.gateway_service)
    console = make_console()
    resp = await client.send("/sessions", conversation_id)
    messages = [{"content": m.content} for m in resp.messages]
    if fmt == "form":
        for msg in resp.messages:
            render_markdown(msg.content, console)
        return 0
    render_data(messages, console, fmt=fmt)
    return 0


def _run_browse(services: Any, conversation_id: str, args) -> int:
    fmt = resolve_format(args)
    registry = services.gateway_registry
    session_key = GatewaySessionKey(Platform.CLI, conversation_id, display_name=conversation_id)
    links = asyncio.run(registry.list_session_links(session_key))
    pick = getattr(args, "pick", None)
    if pick:
        return _show_detail(services, pick, fmt)
    if not _is_interactive(args):
        return _render_links(links, fmt)
    try:
        session_id = _prompt_for_session(links)
    except KeyboardInterrupt:
        return 130
    if session_id is None:
        return 0
    return _show_detail(services, session_id, fmt)


def _is_interactive(args) -> bool:
    if getattr(args, "no_interactive", False):
        return False
    return sys.stdout.isatty()


def _link_to_dict(link: Any) -> dict[str, Any]:
    return {
        "session_id": link.session_id,
        "conversation_id": link.conversation_id,
        "display_name": link.display_name,
        "updated_at": str(getattr(link, "updated_at", "")),
    }


def _render_links(links, fmt: str) -> int:
    rows = [_link_to_dict(link) for link in links]
    if not rows:
        render_data([], make_console(), fmt=fmt)
        return 0
    render_data(rows, make_console(), fmt=fmt, headers=list(rows[0].keys()))
    return 0


def _show_detail(services: Any, session_id: str, fmt: str) -> int:
    try:
        detail = asyncio.run(services.session_service.get_session_detail(session_id))
    except Exception as exc:
        from app.interfaces.cli.render import render_action
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    render_data(detail, make_console(), fmt=fmt)
    return 0


def _prompt_for_session(links) -> str | None:
    return _build_session_picker_app(links).run()


def _build_session_picker_app(links):
    from prompt_toolkit import Application
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

    state = {"filter": "", "selected": 0}
    entries = list(links)

    def _filtered() -> list:
        if not state["filter"]:
            return entries
        f = state["filter"].lower()
        return [
            e for e in entries
            if f in e.session_id.lower() or f in (e.display_name or "").lower()
        ]

    def _body_text():
        items = _filtered()
        lines = []
        for i, e in enumerate(items):
            cursor = ">" if i == state["selected"] else " "
            lines.append(f"{cursor} {e.session_id}  {e.display_name or ''}")
        if not items:
            lines.append("(no match)")
        return "\n".join(lines)

    def _clamp_selected() -> None:
        items = _filtered()
        if not items:
            state["selected"] = 0
            return
        state["selected"] = max(0, min(state["selected"], len(items) - 1))

    buffer = Buffer()
    kb = KeyBindings()

    @kb.add("c-c")
    def _exit_cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    @kb.add("enter")
    def _exit_select(event):
        items = _filtered()
        if items:
            event.app.exit(result=items[state["selected"]].session_id)
        else:
            event.app.exit(result=None)

    @kb.add("up")
    def _move_up(event):
        state["selected"] = max(0, state["selected"] - 1)
        event.app.invalidate()

    @kb.add("down")
    def _move_down(event):
        items = _filtered()
        if items:
            state["selected"] = min(len(items) - 1, state["selected"] + 1)
        event.app.invalidate()

    def _on_text_changed(_buffer):
        state["filter"] = _buffer.text
        state["selected"] = 0
        _clamp_selected()

    buffer.on_text_changed += _on_text_changed

    body = FormattedTextControl(text=_body_text, focusable=False)
    layout = Layout(
        HSplit([
            Window(content=body, wrap_lines=True),
            Window(height=1, char="-"),
            Window(content=BufferControl(buffer=buffer), height=1),
        ])
    )
    app = Application(layout=layout, key_bindings=kb, full_screen=True)
    return app
