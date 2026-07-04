from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.interfaces.cli.gateway_client import GatewayCliClient
from app.interfaces.cli.render import make_console, render_markdown, render_status
from app.interfaces.cli.repl import ReplRunner
from app.interfaces.cli.streaming import consume_stream


def _build_services() -> Any:
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    if args.conversation_id and args.session_source and args.conversation_id != args.session_source:
        render_status("--conversation-id and --session-source conflict", "error", make_console())
        return 2
    conversation_id = args.conversation_id or args.session_source or "local"

    services = _build_services()
    console = make_console()
    client = GatewayCliClient(services.gateway_service)

    if args.message:
        return asyncio.run(_send_once(client, args.message, conversation_id, args.no_stream, console))

    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if not data.strip():
            render_status("no message provided on stdin", "error", console)
            return 2
        return asyncio.run(_send_once(client, data.strip(), conversation_id, args.no_stream, console))

    return asyncio.run(_run_repl(client, console, conversation_id))


async def _send_once(client: GatewayCliClient, text: str, conversation_id: str, no_stream: bool, console: Any) -> int:
    if no_stream:
        resp = await client.send(text, conversation_id)
        for msg in resp.messages:
            render_markdown(msg.content, console)
        return 0
    stream = client.send_stream(text, conversation_id)
    return await consume_stream(stream, console)


async def _run_repl(client: GatewayCliClient, console: Any, conversation_id: str) -> int:
    runner = ReplRunner(client, console, conversation_id=conversation_id, is_tty=sys.stdout.isatty())
    return await runner.run()
