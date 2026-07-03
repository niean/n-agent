from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

from app.domain.gateway import GatewaySessionKey, InteractionMessage
from app.domain.platform import Platform
from app.main import build_application_services


def _load_skill_service():
    return build_application_services().skill_service


def _load_plugin_service():
    return build_application_services().plugin_service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="n-agent")
    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat")
    chat_parser.add_argument("message", nargs="?")
    chat_parser.add_argument("--session-source", default="local")

    subparsers.add_parser("sessions")
    subparsers.add_parser("status")

    skill_parser = subparsers.add_parser("skill")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command")
    skill_subparsers.add_parser("list")
    skill_view_parser = skill_subparsers.add_parser("view")
    skill_view_parser.add_argument("name")

    plugin_parser = subparsers.add_parser("plugin")
    plugin_subparsers = plugin_parser.add_subparsers(dest="plugin_command")
    plugin_subparsers.add_parser("list")
    plugin_view_parser = plugin_subparsers.add_parser("view")
    plugin_view_parser.add_argument("name")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "skill":
        service = _load_skill_service()
        if args.skill_command == "list":
            return _cmd_skill_list(args, service)
        if args.skill_command == "view":
            return _cmd_skill_view(args, service)
        skill_parser.print_help()
        return 0

    if args.command == "plugin":
        service = _load_plugin_service()
        if args.plugin_command == "list":
            return _cmd_plugin_list(args, service)
        if args.plugin_command == "view":
            return _cmd_plugin_view(args, service)
        plugin_parser.print_help()
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


def _cmd_skill_list(args, service) -> int:
    skills = asyncio.run(service.list_skills(include_disabled=True))
    for s in skills:
        readiness = s.readiness.value if hasattr(s.readiness, "value") else str(s.readiness)
        print(f"{s.name}\t{readiness}\t{'on' if s.enabled else 'off'}\t{s.description}")
    return 0


def _cmd_skill_view(args, service) -> int:
    payload = asyncio.run(service.render_view(args.name))
    if not payload.get("success"):
        print(json.dumps(payload, ensure_ascii=False))
        return 1
    print(payload.get("content", ""))
    return 0


def _cmd_plugin_list(args, service) -> int:
    plugins = asyncio.run(service.list_plugins())
    for p in plugins:
        print(f"{p.key}\t{p.kind.value}\t{p.source.value}\t{'on' if p.enabled else 'off'}\t{p.description}")
    return 0


def _cmd_plugin_view(args, service) -> int:
    plugin = asyncio.run(service.get_plugin(args.name))
    if plugin is None:
        print(json.dumps({"success": False, "error": "plugin not found"}, ensure_ascii=False))
        return 1
    print(json.dumps(plugin.to_public_view(), ensure_ascii=False))
    return 0


def _interactive_chat(gateway_service, platform_session_id: str) -> int:
    while True:
        try:
            text = input("> ").strip()
        except EOFError:
            return 0
        if text in {"/exit", "exit", "quit"}:
            return 0
        if not text:
            continue
        response = asyncio.run(_send(gateway_service, text, platform_session_id))
        _print_response(response)


async def _send(gateway_service, text: str, platform_session_id: str):
    event = InteractionMessage(
        id=f"cli-{uuid4()}",
        session_key=GatewaySessionKey(Platform.CLI, platform_session_id, display_name=platform_session_id),
        text=text,
    )
    return await gateway_service.handle_message(event)


def _print_response(response) -> None:
    for message in response.messages:
        print(message.content)


if __name__ == "__main__":
    raise SystemExit(main())
