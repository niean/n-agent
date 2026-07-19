from __future__ import annotations

import argparse
import asyncio
import inspect
import logging
import sys
import warnings
from typing import TYPE_CHECKING

from .commands import (
    acp,
    chat,
    config,
    curator,
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
    task,
    usage,
)

if TYPE_CHECKING:
    from app.application.plugin_service import PluginCliCommand

logger = logging.getLogger(__name__)


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


def _build_task_parser(subparsers) -> None:
    parser = subparsers.add_parser("task", help="Task (Kanban / Manus Task) commands")
    sub = parser.add_subparsers(dest="task_command", required=True)

    def _fmt(p):
        return _add_format_flags(p)

    list_p = sub.add_parser("list", help="List tasks")
    list_p.add_argument("--status", default=None)
    list_p.add_argument("--all", action="store_true", help="List all pages")
    _fmt(list_p)

    ls_p = sub.add_parser("ls", help="Alias of list")
    ls_p.add_argument("--status", default=None)
    ls_p.add_argument("--all", action="store_true", help="List all pages")
    _fmt(ls_p)

    show_p = sub.add_parser("show", help="Show a task")
    show_p.add_argument("id")
    _fmt(show_p)

    create_p = sub.add_parser("create", help="Create a task")
    create_p.add_argument("--title", required=True)
    create_p.add_argument("--body", default=None)
    create_p.add_argument("--priority", type=int, default=None)
    create_p.add_argument("--goal", action="store_true", help="Enable goal_mode (autonomous loop)")
    create_p.add_argument("--goal-max-turns", type=int, default=None)
    create_p.add_argument("--max-runtime", type=int, default=None)
    create_p.add_argument("--max-retries", type=int, default=None)
    create_p.add_argument("--created-by", default=None)
    create_p.add_argument("--scheduled-at", default=None, help="Auto-schedule ISO datetime (queued until due)")
    _fmt(create_p)

    delete_p = sub.add_parser("delete", help="Delete a task (hard delete, non-RUNNING only)")
    delete_p.add_argument("id")
    _fmt(delete_p)

    cancel_p = sub.add_parser("cancel", help="Cancel a task (queued/running/waiting_approval/failed)")
    cancel_p.add_argument("id")
    _fmt(cancel_p)

    retry_p = sub.add_parser("retry", help="Retry a failed/expired task (back to queued)")
    retry_p.add_argument("id")
    _fmt(retry_p)

    archive_p = sub.add_parser("archive", help="Archive a task")
    archive_p.add_argument("id")
    archive_p.add_argument("--version", type=int, required=True)
    _fmt(archive_p)

    unarchive_p = sub.add_parser("unarchive", help="Restore an archived task")
    unarchive_p.add_argument("id")
    unarchive_p.add_argument("--version", type=int, required=True)
    _fmt(unarchive_p)

    complete_p = sub.add_parser("complete", help="Mark a task complete (worker intent)")
    complete_p.add_argument("id")
    complete_p.add_argument("--summary", required=True)
    complete_p.add_argument("--metadata", default=None, help="JSON metadata")
    _fmt(complete_p)

    approve_p = sub.add_parser("approve", help="Approve a waiting_approval task's proposal")
    approve_p.add_argument("id")
    approve_p.add_argument("--note", default=None, help="Optional approval note/feedback")
    _fmt(approve_p)

    comment_p = sub.add_parser("comment", help="Add a comment")
    comment_p.add_argument("id")
    comment_p.add_argument("--body", required=True)
    comment_p.add_argument("--author", default=None)
    _fmt(comment_p)

    reject_p = sub.add_parser("reject", help="Reject a waiting_approval task's proposal")
    reject_p.add_argument("id")
    reject_p.add_argument("--note", default=None, help="Optional rejection reason")
    _fmt(reject_p)

    runs_p = sub.add_parser("runs", help="List runs of a task")
    runs_p.add_argument("id")
    runs_p.add_argument("--limit", type=int, default=None)
    _fmt(runs_p)

    events_p = sub.add_parser("events", help="List events of a task")
    events_p.add_argument("id")
    events_p.add_argument("--limit", type=int, default=None)
    _fmt(events_p)

    dispatch_p = sub.add_parser("dispatch", help="Nudge the dispatcher (one tick)")
    _fmt(dispatch_p)

    propose_p = sub.add_parser("propose", help="Propose a change on a running task (manual/testing)")
    propose_p.add_argument("id")
    propose_p.add_argument("--proposal", required=True)
    _fmt(propose_p)


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
    _add_format_flags(list_p)

    get_p = sub.add_parser("get", help="Show a platform")
    get_p.add_argument("platform")
    _add_format_flags(get_p)

    sessions_p = sub.add_parser("sessions", help="List platform sessions")
    sessions_p.add_argument("platform")
    sessions_p.add_argument("--limit", type=int, default=None)
    sessions_p.add_argument("--offset", type=int, default=None)
    _add_format_flags(sessions_p)


def _build_acp_parser(subparsers) -> None:
    parser = subparsers.add_parser("acp", help="Run ACP stdio server")
    parser.add_argument("--check", action="store_true", help="Verify ACP dependencies and exit")
    parser.add_argument("--setup", action="store_true", help="Show provider setup instructions and exit")


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


def build_parser(
    plugin_commands: list[PluginCliCommand] | None = None,
) -> argparse.ArgumentParser:
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

    skill_pending_parser = skill_subparsers.add_parser("pending", help="List pending skill writes")
    _add_format_flags(skill_pending_parser)

    skill_diff_parser = skill_subparsers.add_parser("diff", help="Show diff of a pending write")
    skill_diff_parser.add_argument("pending_id")
    _add_format_flags(skill_diff_parser)

    skill_approve_parser = skill_subparsers.add_parser("approve", help="Approve a pending write")
    skill_approve_parser.add_argument("pending_id")
    _add_format_flags(skill_approve_parser)

    skill_reject_parser = skill_subparsers.add_parser("reject", help="Reject a pending write")
    skill_reject_parser.add_argument("pending_id")
    _add_format_flags(skill_reject_parser)

    skill_approve_all_parser = skill_subparsers.add_parser("approve-all", help="Approve all pending writes")
    _add_format_flags(skill_approve_all_parser)

    skill_reject_all_parser = skill_subparsers.add_parser("reject-all", help="Reject all pending writes")
    _add_format_flags(skill_reject_all_parser)

    skill_pin_parser = skill_subparsers.add_parser("pin", help="Pin a skill")
    skill_pin_parser.add_argument("name")
    _add_format_flags(skill_pin_parser)

    skill_unpin_parser = skill_subparsers.add_parser("unpin", help="Unpin a skill")
    skill_unpin_parser.add_argument("name")
    _add_format_flags(skill_unpin_parser)

    skill_usage_parser = skill_subparsers.add_parser("usage", help="List skill usage telemetry")
    _add_format_flags(skill_usage_parser)

    curator_parser = subparsers.add_parser("curator", help="Curator commands")
    curator.register_cli(curator_parser, _add_format_flags)

    plugin_parser = subparsers.add_parser("plugin", help="Plugin commands")
    plugin_subparsers = plugin_parser.add_subparsers(dest="plugin_command")
    plugin_list_parser = plugin_subparsers.add_parser("list", help="List plugins")
    _add_format_flags(plugin_list_parser)
    plugin_view_parser = plugin_subparsers.add_parser("view", help="View a plugin")
    plugin_view_parser.add_argument("name")
    _add_format_flags(plugin_view_parser)
    plugin_deps_parser = plugin_subparsers.add_parser(
        "deps", help="Show plugin dependency diagnostics"
    )
    plugin_deps_parser.add_argument("name")
    _add_format_flags(plugin_deps_parser)

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
    _build_acp_parser(subparsers)
    _build_task_parser(subparsers)
    usage_parser = subparsers.add_parser("usage", help="Print token usage and context stats")
    usage_parser.add_argument("session_id", nargs="?", default=None, help="Session ID (omit to list recent sessions)")
    _add_format_flags(usage_parser)

    if plugin_commands:
        _register_plugin_commands(subparsers, plugin_commands)

    return parser


def _register_plugin_commands(
    subparsers: argparse._SubParsersAction,
    plugin_commands: list[PluginCliCommand],
) -> None:
    """Register plugin CLI commands as top-level subparsers.

    Conflict rules:
    - Plugin name == builtin top-level name -> warning + skip (builtin wins).
    - Inter-plugin or same-plugin duplicate -> stable first-wins (first in
      list order), later -> warning + skip.
    - setup_fn raises -> warning + skip THAT command only, continue others.
    After any failure, builtin commands + other plugin commands still work.
    """
    builtin_names = set(_DISPATCH.keys())
    seen_names: set[str] = set()
    for cmd in plugin_commands:
        name = cmd.name
        if name in builtin_names:
            logger.warning(
                "plugin %s: CLI command %r conflicts with builtin; skipping",
                cmd.plugin_key,
                name,
            )
            continue
        if name in seen_names:
            logger.warning(
                "plugin %s: CLI command %r duplicates earlier registration; skipping",
                cmd.plugin_key,
                name,
            )
            continue
        try:
            subparser = subparsers.add_parser(name, help=cmd.help)
        except Exception:
            logger.warning(
                "plugin %s: CLI command %r add_parser failed; skipping",
                cmd.plugin_key,
                name,
                exc_info=True,
            )
            continue
        try:
            cmd.setup_fn(subparser)
        except Exception:
            logger.warning(
                "plugin %s: CLI command %r setup_fn failed; skipping",
                cmd.plugin_key,
                name,
                exc_info=True,
            )
            # Remove the partially-added subparser so it is not usable.
            subparsers.choices.pop(name, None)
            continue
        if cmd.handler_fn is not None:
            subparser.set_defaults(func=cmd.handler_fn)
        seen_names.add(name)


_DISPATCH = {
    "chat": chat.run,
    "sessions": sessions.run,
    "status": status.run,
    "skill": skill.run,
    "curator": curator.run,
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
    "acp": acp.run,
    "task": task.run,
    "usage": usage.run,
}


def main(argv: list[str] | None = None) -> int:
    _configure_cli_env()
    from app.main import collect_plugin_cli_commands

    plugin_commands = collect_plugin_cli_commands()
    parser = build_parser(plugin_commands=plugin_commands)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    if args.command is None:
        parser.print_help()
        return 0

    # Plugin commands set args.func via set_defaults; they take priority over
    # the builtin _DISPATCH table.
    handler = getattr(args, "func", None)
    if handler is None:
        handler = _DISPATCH.get(args.command)
    if handler is None:
        _print_command_help(parser, args.command)
        return 0
    return _invoke_handler(handler, args)


def _invoke_handler(handler, args) -> int:
    """Execute a CLI handler and normalise its return value.

    Contract:
    - None -> 0
    - int -> as-is
    - awaitable -> run to completion, then None->0 / int->as-is
    - other types or exception -> print short error + return 1
    """
    try:
        result = handler(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if inspect.isawaitable(result):
        try:
            result = _run_coroutine(result)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    print(
        f"error: handler returned unexpected type {type(result).__name__}",
        file=sys.stderr,
    )
    return 1


def _run_coroutine(coro):
    """Run a coroutine to completion from sync context.

    Uses ``asyncio.run`` when no event loop is running (the normal CLI case).
    If a loop is already running (e.g., inside an async test), runs the
    coroutine in a separate thread to avoid ``RuntimeError``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import threading

    box: dict = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _print_command_help(parser: argparse.ArgumentParser, command: str) -> None:
    """Print the help text for a specific subcommand."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparser = action.choices.get(command)
            if subparser is not None:
                subparser.print_help()
                return
    parser.print_help()
