from __future__ import annotations

import argparse
import logging
import warnings

from .commands import (
    chat,
    config,
    doctor,
    knowledge,
    logs,
    mcp,
    memory,
    platform,
    plugin,
    provider,
    sandbox,
    schedule,
    sessions,
    skill,
    status,
)


def _configure_cli_env() -> None:
    try:
        from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
    except ImportError:
        LangChainPendingDeprecationWarning = None
    if LangChainPendingDeprecationWarning is not None:
        warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
    logging.getLogger("app.infrastructure.skill.seed_runner").setLevel(logging.ERROR)
    logging.getLogger("app.infrastructure.plugin.seed_runner").setLevel(logging.ERROR)


def _add_format_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Output as JSON (default)")
    parser.add_argument("--form", action="store_true", help="Output as human-readable form (table/detail)")
    parser.add_argument("--yaml", action="store_true", help="Output as YAML")


def _build_provider_parser(subparsers) -> None:
    parser = subparsers.add_parser("provider", help="Provider commands")
    sub = parser.add_subparsers(dest="provider_command", required=True)

    list_p = sub.add_parser("list", help="List providers")
    _add_format_flags(list_p)

    get_p = sub.add_parser("get", help="Show a provider")
    get_p.add_argument("id")
    _add_format_flags(get_p)

    create_p = sub.add_parser("create", help="Create a provider")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--type", default=None)
    create_p.add_argument("--base-url", default=None)
    create_p.add_argument("--model", default=None)
    create_p.add_argument("--api-key", default=None)
    create_p.add_argument("--extra-headers", default=None)
    _add_format_flags(create_p)

    update_p = sub.add_parser("update", help="Update a provider")
    update_p.add_argument("id")
    update_p.add_argument("--name", default=None)
    update_p.add_argument("--base-url", default=None)
    update_p.add_argument("--model", default=None)
    update_p.add_argument("--api-key", default=None)
    update_p.add_argument("--extra-headers", default=None)
    _add_format_flags(update_p)

    delete_p = sub.add_parser("delete", help="Delete a provider")
    delete_p.add_argument("id")
    _add_format_flags(delete_p)

    activate_p = sub.add_parser("activate", help="Activate a provider")
    activate_p.add_argument("id")
    _add_format_flags(activate_p)


def _build_knowledge_parser(subparsers) -> None:
    parser = subparsers.add_parser("knowledge", help="Knowledge base commands")
    sub = parser.add_subparsers(dest="knowledge_command", required=True)

    list_p = sub.add_parser("list", help="List knowledge bases")
    _add_format_flags(list_p)

    get_p = sub.add_parser("get", help="Show a knowledge base")
    get_p.add_argument("id")
    _add_format_flags(get_p)

    create_p = sub.add_parser("create", help="Create a knowledge base")
    create_p.add_argument("--id", required=True)
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--description", required=True)
    create_p.add_argument("--base-type", required=True)
    create_p.add_argument("--base-url", required=True)
    create_p.add_argument("--dataset-id", required=True)
    create_p.add_argument("--api-key", default=None)
    create_p.add_argument("--enabled", dest="enabled", action="store_true", default=True)
    create_p.add_argument("--disabled", dest="enabled", action="store_false")
    create_p.add_argument("--default-top-k", type=int, default=None)
    create_p.add_argument("--default-min-score", type=float, default=None)
    _add_format_flags(create_p)

    update_p = sub.add_parser("update", help="Update a knowledge base")
    update_p.add_argument("id")
    update_p.add_argument("--name", default=None)
    update_p.add_argument("--description", default=None)
    update_p.add_argument("--base-type", default=None)
    update_p.add_argument("--base-url", default=None)
    update_p.add_argument("--dataset-id", default=None)
    update_p.add_argument("--api-key", default=None)
    update_p.add_argument("--enabled", dest="enabled", action="store_true", default=None)
    update_p.add_argument("--disabled", dest="enabled", action="store_false")
    update_p.add_argument("--default-top-k", type=int, default=None)
    update_p.add_argument("--default-min-score", type=float, default=None)
    update_p.add_argument("--clear-default-top-k", action="store_true")
    update_p.add_argument("--clear-default-min-score", action="store_true")
    _add_format_flags(update_p)

    delete_p = sub.add_parser("delete", help="Delete a knowledge base")
    delete_p.add_argument("id")
    _add_format_flags(delete_p)

    probe_p = sub.add_parser("probe", help="Probe a knowledge base")
    probe_p.add_argument("id")
    _add_format_flags(probe_p)


def _build_mcp_parser(subparsers) -> None:
    parser = subparsers.add_parser("mcp", help="MCP site commands")
    sub = parser.add_subparsers(dest="mcp_command", required=True)

    list_p = sub.add_parser("list", help="List MCP sites")
    _add_format_flags(list_p)

    get_p = sub.add_parser("get", help="Show an MCP site")
    get_p.add_argument("id")
    _add_format_flags(get_p)

    create_p = sub.add_parser("create", help="Create an MCP site")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--transport", required=True)
    create_p.add_argument("--url", default=None)
    create_p.add_argument("--command", default=None)
    create_p.add_argument("--args", default=None, help="JSON array of strings")
    create_p.add_argument("--env", default=None, help="JSON object of string->string")
    create_p.add_argument("--include-tools", default=None, help="JSON array of tool names")
    _add_format_flags(create_p)

    update_p = sub.add_parser("update", help="Update an MCP site")
    update_p.add_argument("id")
    update_p.add_argument("--name", default=None)
    update_p.add_argument("--url", default=None)
    update_p.add_argument("--command", default=None)
    update_p.add_argument("--args", default=None)
    update_p.add_argument("--env", default=None)
    update_p.add_argument("--enabled", action="store_true", default=None)
    update_p.add_argument("--disabled", action="store_true")
    _add_format_flags(update_p)

    delete_p = sub.add_parser("delete", help="Delete an MCP site")
    delete_p.add_argument("id")
    _add_format_flags(delete_p)

    probe_p = sub.add_parser("probe", help="Probe an MCP site")
    probe_p.add_argument("id")
    _add_format_flags(probe_p)

    refresh_p = sub.add_parser("refresh", help="Refresh tools from an MCP site")
    refresh_p.add_argument("id")
    _add_format_flags(refresh_p)

    tools_p = sub.add_parser("tools", help="List tools of an MCP site")
    tools_p.add_argument("id")
    _add_format_flags(tools_p)

    toggle_p = sub.add_parser("toggle", help="Toggle a tool enabled state")
    toggle_p.add_argument("id")
    toggle_p.add_argument("--tool-id", required=True)
    toggle_p.add_argument("--enabled", action="store_true", default=None)
    toggle_p.add_argument("--disabled", action="store_true")
    _add_format_flags(toggle_p)


def _build_schedule_parser(subparsers) -> None:
    parser = subparsers.add_parser("schedule", help="Scheduled task commands")
    sub = parser.add_subparsers(dest="schedule_command", required=True)

    list_p = sub.add_parser("list", help="List scheduled tasks")
    _add_format_flags(list_p)

    get_p = sub.add_parser("get", help="Show a scheduled task")
    get_p.add_argument("id")
    _add_format_flags(get_p)

    create_p = sub.add_parser("create", help="Create a scheduled task")
    create_p.add_argument("--name", required=True)
    create_p.add_argument("--prompt", required=True)
    create_p.add_argument("--cron", required=True)
    create_p.add_argument("--timezone", default=None)
    create_p.add_argument("--delivery-target", default=None)
    _add_format_flags(create_p)

    update_p = sub.add_parser("update", help="Update a scheduled task")
    update_p.add_argument("id")
    update_p.add_argument("--name", default=None)
    update_p.add_argument("--prompt", default=None)
    update_p.add_argument("--cron", default=None)
    update_p.add_argument("--timezone", default=None)
    update_p.add_argument("--delivery-target", default=None)
    _add_format_flags(update_p)

    pause_p = sub.add_parser("pause", help="Pause a scheduled task")
    pause_p.add_argument("id")
    _add_format_flags(pause_p)

    resume_p = sub.add_parser("resume", help="Resume a scheduled task")
    resume_p.add_argument("id")
    _add_format_flags(resume_p)

    run_p = sub.add_parser("run", help="Run a scheduled task now")
    run_p.add_argument("id")
    run_p.add_argument("--no-wait", action="store_true", help="Fire and forget (task may stuck if no scheduler runner)")
    run_p.add_argument("--timeout", type=int, default=None, help="Max seconds to wait (default 300)")
    _add_format_flags(run_p)

    delete_p = sub.add_parser("delete", help="Delete a scheduled task")
    delete_p.add_argument("id")
    _add_format_flags(delete_p)

    executions_p = sub.add_parser("executions", help="List executions of a task")
    executions_p.add_argument("id")
    executions_p.add_argument("--limit", type=int, default=None)
    _add_format_flags(executions_p)


def _build_sandbox_parser(subparsers) -> None:
    parser = subparsers.add_parser("sandbox", help="Sandbox commands")
    sub = parser.add_subparsers(dest="sandbox_command", required=True)

    list_active_p = sub.add_parser("list-active", help="List active sandboxes")
    _add_format_flags(list_active_p)

    list_released_p = sub.add_parser("list-released", help="List released sandboxes")
    _add_format_flags(list_released_p)

    list_history_p = sub.add_parser("list-history", help="List code execution history")
    list_history_p.add_argument("--session-id", default=None)
    list_history_p.add_argument("--limit", type=int, default=None)
    _add_format_flags(list_history_p)

    release_p = sub.add_parser("release", help="Release a sandbox")
    release_p.add_argument("--session-id", required=True)
    _add_format_flags(release_p)

    delete_history_p = sub.add_parser("delete-history", help="Delete code execution history")
    delete_history_p.add_argument("--tool-call-id", required=True)
    _add_format_flags(delete_history_p)

    config_p = sub.add_parser("config", help="Show sandbox config")
    _add_format_flags(config_p)


def _build_memory_parser(subparsers) -> None:
    parser = subparsers.add_parser("memory", help="External memory commands")
    sub = parser.add_subparsers(dest="memory_command", required=True)

    list_providers_p = sub.add_parser("list-providers", help="List memory providers")
    _add_format_flags(list_providers_p)

    get_provider_p = sub.add_parser("get-provider", help="Show a memory provider")
    get_provider_p.add_argument("id")
    _add_format_flags(get_provider_p)

    create_provider_p = sub.add_parser("create-provider", help="Create a memory provider")
    create_provider_p.add_argument("--name", required=True)
    create_provider_p.add_argument("--type", required=True)
    create_provider_p.add_argument("--base-url", default=None)
    create_provider_p.add_argument("--api-key", default=None)
    create_provider_p.add_argument("--extra-config", default=None)
    _add_format_flags(create_provider_p)

    update_provider_p = sub.add_parser("update-provider", help="Update a memory provider")
    update_provider_p.add_argument("id")
    update_provider_p.add_argument("--name", default=None)
    update_provider_p.add_argument("--base-url", default=None)
    update_provider_p.add_argument("--api-key", default=None)
    update_provider_p.add_argument("--clear-api-key", action="store_true")
    update_provider_p.add_argument("--extra-config", default=None)
    _add_format_flags(update_provider_p)

    delete_provider_p = sub.add_parser("delete-provider", help="Delete a memory provider")
    delete_provider_p.add_argument("id")
    _add_format_flags(delete_provider_p)

    activate_provider_p = sub.add_parser("activate-provider", help="Activate a memory provider")
    activate_provider_p.add_argument("id")
    _add_format_flags(activate_provider_p)

    deactivate_provider_p = sub.add_parser("deactivate-provider", help="Deactivate a memory provider")
    deactivate_provider_p.add_argument("id")
    _add_format_flags(deactivate_provider_p)

    probe_provider_p = sub.add_parser("probe-provider", help="Probe a memory provider")
    probe_provider_p.add_argument("id")
    _add_format_flags(probe_provider_p)

    list_projects_p = sub.add_parser("list-projects", help="List memory projects")
    _add_format_flags(list_projects_p)

    get_project_p = sub.add_parser("get-project", help="Show a memory project")
    get_project_p.add_argument("--name", required=True)
    get_project_p.add_argument("--target", default="project")
    _add_format_flags(get_project_p)

    create_project_p = sub.add_parser("create-project", help="Create a memory project")
    create_project_p.add_argument("--name", required=True)
    _add_format_flags(create_project_p)

    delete_project_p = sub.add_parser("delete-project", help="Delete a memory project")
    delete_project_p.add_argument("--name", required=True)
    _add_format_flags(delete_project_p)

    list_entries_p = sub.add_parser("list-entries", help="List project entries")
    list_entries_p.add_argument("--project", required=True)
    list_entries_p.add_argument("--target", default="project")
    _add_format_flags(list_entries_p)

    add_entry_p = sub.add_parser("add-entry", help="Add a project entry")
    add_entry_p.add_argument("--project", required=True)
    add_entry_p.add_argument("--content", required=True)
    add_entry_p.add_argument("--target", default="project")
    _add_format_flags(add_entry_p)

    update_entry_p = sub.add_parser("update-entry", help="Update a project entry")
    update_entry_p.add_argument("--project", required=True)
    update_entry_p.add_argument("--index", type=int, required=True)
    update_entry_p.add_argument("--content", required=True)
    update_entry_p.add_argument("--target", default="project")
    _add_format_flags(update_entry_p)

    delete_entry_p = sub.add_parser("delete-entry", help="Delete a project entry")
    delete_entry_p.add_argument("--project", required=True)
    delete_entry_p.add_argument("--index", type=int, required=True)
    delete_entry_p.add_argument("--target", default="project")
    _add_format_flags(delete_entry_p)

    global_p = sub.add_parser("global", help="Set global enabled memory providers")
    global_p.add_argument("--providers", default="")
    _add_format_flags(global_p)


def _build_platform_parser(subparsers) -> None:
    parser = subparsers.add_parser("platform", help="Platform commands")
    sub = parser.add_subparsers(dest="platform_command", required=True)

    list_p = sub.add_parser("list", help="List platforms")
    list_p.add_argument("--include-local", action="store_true")
    _add_format_flags(list_p)

    get_p = sub.add_parser("get", help="Show a platform")
    get_p.add_argument("platform")
    _add_format_flags(get_p)

    sessions_p = sub.add_parser("sessions", help="List platform sessions")
    sessions_p.add_argument("platform")
    sessions_p.add_argument("--limit", type=int, default=None)
    sessions_p.add_argument("--offset", type=int, default=None)
    _add_format_flags(sessions_p)


def _build_doctor_parser(subparsers) -> None:
    parser = subparsers.add_parser("doctor", help="Run health checks")
    parser.add_argument("--probe", action="store_true", help="Run network probes")
    _add_format_flags(parser)


def _build_config_parser(subparsers) -> None:
    parser = subparsers.add_parser("config", help="Show runtime config (redacted)")
    parser.add_argument("--section", default=None)
    _add_format_flags(parser)


def _build_logs_parser(subparsers) -> None:
    parser = subparsers.add_parser("logs", help="Logs commands")
    sub = parser.add_subparsers(dest="logs_command", required=True)

    sandbox_p = sub.add_parser("sandbox", help="Sandbox execution logs")
    sandbox_p.add_argument("--session-id", default=None)
    sandbox_p.add_argument("--limit", type=int, default=None)
    _add_format_flags(sandbox_p)

    tools_p = sub.add_parser("tools", help="Tool call logs")
    tools_p.add_argument("--session-id", required=True)
    tools_p.add_argument("--limit", type=int, default=None)
    _add_format_flags(tools_p)

    scheduled_p = sub.add_parser("scheduled", help="Scheduled task executions")
    scheduled_p.add_argument("--task-id", required=True)
    scheduled_p.add_argument("--limit", type=int, default=None)
    _add_format_flags(scheduled_p)

    runs_p = sub.add_parser("runs", help="Session run history")
    runs_p.add_argument("--session-id", required=True)
    _add_format_flags(runs_p)


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
    sessions_parser.add_argument("--browse", action="store_true", help="Interactive session picker")
    sessions_parser.add_argument("--pick", default=None, help="Non-interactive: pick session by id")
    sessions_parser.add_argument("--no-interactive", action="store_true", help="Force non-interactive mode")
    _add_format_flags(sessions_parser)

    status_parser = subparsers.add_parser("status", help="Show health status")
    _add_format_flags(status_parser)

    skill_parser = subparsers.add_parser("skill", help="Skill commands")
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command")
    skill_list_parser = skill_subparsers.add_parser("list", help="List skills")
    _add_format_flags(skill_list_parser)
    skill_view_parser = skill_subparsers.add_parser("view", help="View a skill")
    skill_view_parser.add_argument("name")
    _add_format_flags(skill_view_parser)

    plugin_parser = subparsers.add_parser("plugin", help="Plugin commands")
    plugin_subparsers = plugin_parser.add_subparsers(dest="plugin_command")
    plugin_list_parser = plugin_subparsers.add_parser("list", help="List plugins")
    _add_format_flags(plugin_list_parser)
    plugin_view_parser = plugin_subparsers.add_parser("view", help="View a plugin")
    plugin_view_parser.add_argument("name")
    _add_format_flags(plugin_view_parser)

    _build_provider_parser(subparsers)
    _build_knowledge_parser(subparsers)
    _build_mcp_parser(subparsers)
    _build_schedule_parser(subparsers)
    _build_sandbox_parser(subparsers)
    _build_memory_parser(subparsers)
    _build_platform_parser(subparsers)
    _build_doctor_parser(subparsers)
    _build_config_parser(subparsers)
    _build_logs_parser(subparsers)

    return parser


_DISPATCH = {
    "chat": chat.run,
    "sessions": sessions.run,
    "status": status.run,
    "skill": skill.run,
    "plugin": plugin.run,
    "provider": provider.run,
    "knowledge": knowledge.run,
    "mcp": mcp.run,
    "schedule": schedule.run,
    "sandbox": sandbox.run,
    "memory": memory.run,
    "platform": platform.run,
    "doctor": doctor.run,
    "config": config.run,
    "logs": logs.run,
}


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

    handler = _DISPATCH.get(args.command)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
