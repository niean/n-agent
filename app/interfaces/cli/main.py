from __future__ import annotations

import argparse
import logging
import warnings

from .commands import chat, plugin, sessions, skill, status


def _configure_cli_env() -> None:
    # langgraph upstream PendingDeprecationWarning about `allowed_objects` default
    # targets library authors, not CLI end users; filter the concrete subclass
    # because langchain_core re-registers filters on its own import
    try:
        from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
    except ImportError:
        LangChainPendingDeprecationWarning = None
    if LangChainPendingDeprecationWarning is not None:
        warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
    # seed_default_skills/plugins warn when /workspace paths are unwritable, which is
    # expected when running CLI outside docker; service mode still surfaces these
    logging.getLogger("app.infrastructure.skill.seed_runner").setLevel(logging.ERROR)
    logging.getLogger("app.infrastructure.plugin.seed_runner").setLevel(logging.ERROR)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="n-agent", description="N-Agent CLI")
    subparsers = parser.add_subparsers(dest="command")

    chat_parser = subparsers.add_parser("chat", help="Send a message or start interactive REPL")
    chat_parser.add_argument("message", nargs="?", default=None)
    chat_parser.add_argument("--session-source", default=None)
    chat_parser.add_argument("--conversation-id", default=None)
    chat_parser.add_argument("--no-stream", action="store_true")

    sessions_parser = subparsers.add_parser("sessions", help="List sessions")
    sessions_parser.add_argument("--session-source", default=None)
    sessions_parser.add_argument("--conversation-id", default=None)

    subparsers.add_parser("status", help="Show health status")

    skill_parser = subparsers.add_parser("skill", help="Skill commands")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command")
    skill_subparsers.add_parser("list", help="List skills")
    skill_view_parser = skill_subparsers.add_parser("view", help="View a skill")
    skill_view_parser.add_argument("name")

    plugin_parser = subparsers.add_parser("plugin", help="Plugin commands")
    plugin_subparsers = plugin_parser.add_subparsers(dest="plugin_command")
    plugin_subparsers.add_parser("list", help="List plugins")
    plugin_view_parser = plugin_subparsers.add_parser("view", help="View a plugin")
    plugin_view_parser.add_argument("name")

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_cli_env()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "chat":
        return chat.run(args)
    if args.command == "sessions":
        return sessions.run(args)
    if args.command == "status":
        return status.run(args)
    if args.command == "skill":
        return skill.run(args)
    if args.command == "plugin":
        return plugin.run(args)
    return 0
