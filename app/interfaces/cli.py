from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

from app.domain.gateway import GatewaySessionKey, InteractionMessage, InteractionSourceType
from app.main import build_application_services


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="n-agent")
    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat")
    chat_parser.add_argument("message", nargs="?")
    chat_parser.add_argument("--session-source", default="local")

    subparsers.add_parser("sessions")
    subparsers.add_parser("status")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command is None:
        parser.print_help()
        return 0

    services = build_application_services()
    if args.command == "status":
        print(services.health_snapshot())
        return 0
    if args.command == "sessions":
        response = asyncio.run(_send(services.gateway_service, "/sessions", "local"))
        _print_response(response)
        return 0
    if args.command == "chat":
        if args.message:
            response = asyncio.run(_send(services.gateway_service, args.message, args.session_source))
            _print_response(response)
            return 0
        return _interactive_chat(services.gateway_service, args.session_source)
    return 0


def _interactive_chat(gateway_service, source_id: str) -> int:
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            return 0
        if text in {"/exit", "exit", "quit"}:
            return 0
        if not text:
            continue
        response = asyncio.run(_send(gateway_service, text, source_id))
        _print_response(response)


async def _send(gateway_service, text: str, source_id: str):
    event = InteractionMessage(
        id=f"cli-{uuid4()}",
        session_key=GatewaySessionKey(InteractionSourceType.CLI, source_id, display_name=source_id),
        text=text,
    )
    return await gateway_service.handle_message(event)


def _print_response(response) -> None:
    for message in response.messages:
        print(message.content)


if __name__ == "__main__":
    raise SystemExit(main())
