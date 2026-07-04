from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Callable

from app.application.events import ChatEvent, ChatEventType
from app.interfaces.cli.render import flush_console, render_markdown, render_status, render_tool_call


async def consume_stream(
    stream: AsyncIterator[ChatEvent],
    console: Any,
    on_confirmation: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """Consume a ChatEvent stream, return exit code (0=ok, non-zero=error)."""
    accumulated: list[str] = []
    rendered_done = False
    line_open = False
    try:
        async for evt in stream:
            if evt.type is ChatEventType.MESSAGE_START:
                accumulated.clear()
                rendered_done = False
                line_open = False
            elif evt.type is ChatEventType.CONTENT_DELTA:
                accumulated.append(evt.content)
                console.print(evt.content, end="", highlight=False)
                flush_console(console)
                line_open = True
            elif evt.type is ChatEventType.TOOL_CALL_DELTA:
                if line_open:
                    console.print()
                    flush_console(console)
                    line_open = False
                render_tool_call(evt.tool_call or {}, console)
            elif evt.type is ChatEventType.MESSAGE_DONE:
                if not rendered_done:
                    full = "".join(accumulated)
                    if evt.content and (not full or evt.content.strip() != full.strip()):
                        if line_open:
                            console.print()
                            flush_console(console)
                            line_open = False
                        render_markdown(evt.content, console)
                    rendered_done = True
                if line_open:
                    console.print()
                    flush_console(console)
                    line_open = False
                if evt.finish_reason == "confirmation_required":
                    if on_confirmation is not None:
                        on_confirmation(evt.metadata)
            elif evt.type is ChatEventType.ERROR:
                if line_open:
                    console.print()
                    flush_console(console)
                    line_open = False
                render_status(evt.error or "error", "error", console)
                return 1
            elif evt.type is ChatEventType.DONE:
                if evt.metadata.get("duplicate"):
                    render_status("duplicate event ignored", "warning", console)
                break
        return 0
    except asyncio.CancelledError:
        render_status("\n[interrupted]", "warning", console)
        raise
