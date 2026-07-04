from __future__ import annotations

import asyncio
from typing import Any

from app.interfaces.cli.gateway_client import GatewayCliClient
from app.interfaces.cli.render import make_console, render_markdown


def _build_services() -> Any:
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    if args.conversation_id and args.session_source and args.conversation_id != args.session_source:
        return 2
    conversation_id = args.conversation_id or args.session_source or "local"
    services = _build_services()
    client = GatewayCliClient(services.gateway_service)
    console = make_console()
    asyncio.run(_send(client, conversation_id, console))
    return 0


async def _send(client: GatewayCliClient, conversation_id: str, console: Any) -> None:
    resp = await client.send("/sessions", conversation_id)
    for msg in resp.messages:
        render_markdown(msg.content, console)
