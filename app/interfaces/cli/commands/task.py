"""T20: CLI commands for the Task (Kanban / Manus Task) subdomain.

Mirrors the schedule CLI pattern: ``_load_task_service`` indirection (so tests
can monkeypatch), ``asyncio.run`` for async service calls, and
``--json``/``--form``/``--yaml`` output via ``render_data`` / ``resolve_format``.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_data,
    resolve_format,
)


def _load_task_service() -> Any:
    from app.main import build_application_services

    return build_application_services().task_service


def _load_task_run_service() -> Any:
    from app.main import build_application_services

    return build_application_services().task_run_service


def _task_to_dict(task: Any) -> dict[str, Any]:
    status = getattr(task, "status", None)
    block_kind = getattr(task, "block_kind", None)
    ws_kind = getattr(task, "workspace_kind", None)
    return {
        "id": task.id,
        "title": task.title,
        "body": getattr(task, "body", "") or "",
        "assignee": getattr(task, "assignee", None),
        "priority": getattr(task, "priority", 0),
        "status": status.value if hasattr(status, "value") else str(status),
        "block_kind": block_kind.value if hasattr(block_kind, "value") else (str(block_kind) if block_kind else None),
        "block_reason": getattr(task, "block_reason", None),
        "block_recurrences": getattr(task, "block_recurrences", 0),
        "consecutive_failures": getattr(task, "consecutive_failures", 0),
        "max_retries": getattr(task, "max_retries", 0),
        "goal_mode": getattr(task, "goal_mode", False),
        "goal_max_turns": getattr(task, "goal_max_turns", None),
        "skills": list(getattr(task, "skills", ()) or ()),
        "model_override": getattr(task, "model_override", None),
        "workspace_kind": ws_kind.value if hasattr(ws_kind, "value") else str(ws_kind),
        "workspace_path": getattr(task, "workspace_path", None),
        "origin_session_id": getattr(task, "origin_session_id", None),
        "execution_session_id": getattr(task, "execution_session_id", None),
        "current_run_id": getattr(task, "current_run_id", None),
        "worker_token": getattr(task, "worker_token", None),
        "version": getattr(task, "version", 1),
        "created_at": str(getattr(task, "created_at", "")) or "",
        "updated_at": str(getattr(task, "updated_at", "")) or "",
        "started_at": str(getattr(task, "started_at", "")) if getattr(task, "started_at", None) else "",
        "completed_at": str(getattr(task, "completed_at", "")) if getattr(task, "completed_at", None) else "",
        "result": getattr(task, "result", None),
    }


_LIST_HEADERS = ["id", "title", "status", "assignee", "priority", "goal_mode", "created_at"]


def run(args) -> int:
    cmd = args.task_command
    dispatch = {
        "list": _cmd_list,
        "ls": _cmd_list,
        "show": _cmd_show,
        "create": _cmd_create,
        "delete": _cmd_delete,
        "archive": _cmd_archive,
        "unarchive": _cmd_unarchive,
        "complete": _cmd_complete,
        "comment": _cmd_comment,
        "runs": _cmd_runs,
        "events": _cmd_events,
        "dispatch": _cmd_dispatch,
        "cancel": _cmd_cancel,
        "retry": _cmd_retry,
        "approve": _cmd_approve,
        "reject": _cmd_reject,
        "propose": _cmd_propose,
    }
    handler = dispatch.get(cmd)
    if handler is None:
        console = make_console()
        console.print(f"[red]unknown task command: {cmd}[/red]")
        return 2
    return handler(args)


def _cmd_list(args) -> int:
    service = _load_task_service()
    if service is None:
        console = make_console()
        console.print("[yellow]task subsystem disabled[/yellow]")
        return 0
    page = asyncio.run(service.list_tasks(limit=100))
    tasks = list(page.items)
    while getattr(args, "all", False) and page.next_cursor is not None:
        page = asyncio.run(
            service.list_tasks(cursor=page.next_cursor, limit=100)
        )
        tasks.extend(page.items)
    items = [_task_to_dict(t) for t in tasks]
    fmt = resolve_format(args)
    render_data(items, fmt=fmt, headers=_LIST_HEADERS, console=make_console())
    return 0


def _cmd_show(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    task = asyncio.run(service.get_task(args.id))
    fmt = resolve_format(args)
    render_data(_task_to_dict(task), fmt=fmt, console=make_console())
    return 0


def _cmd_delete(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    deleted = asyncio.run(service.delete_task(args.id))
    render_data({"id": args.id, "deleted": bool(deleted)}, fmt=resolve_format(args), console=make_console())
    return 0 if deleted else 1


def _cmd_create(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    kwargs: dict[str, Any] = {"title": args.title, "created_by": getattr(args, "created_by", "") or "cli"}
    if getattr(args, "body", None):
        kwargs["body"] = args.body
    if getattr(args, "priority", None) is not None:
        kwargs["priority"] = args.priority
    if getattr(args, "goal", False):
        kwargs["goal_mode"] = True
    if getattr(args, "goal_max_turns", None) is not None:
        kwargs["goal_max_turns"] = args.goal_max_turns
        kwargs["goal_mode"] = True
    if getattr(args, "max_runtime", None) is not None:
        kwargs["max_runtime_seconds"] = args.max_runtime
    if getattr(args, "max_retries", None) is not None:
        kwargs["max_retries"] = args.max_retries
    scheduled_at = getattr(args, "scheduled_at", None)
    if scheduled_at:
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(str(scheduled_at))
        except ValueError as exc:
            print(f"invalid scheduled_at: {exc}", file=sys.stderr)
            return 1
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        kwargs["scheduled_at"] = dt
    task = asyncio.run(service.create_task(**kwargs))
    fmt = resolve_format(args)
    render_data(_task_to_dict(task), fmt=fmt, console=make_console())
    return 0


def _cmd_cancel(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    result = asyncio.run(service.cancel_task(args.id))
    render_data(result if isinstance(result, dict) else _task_to_dict(result), fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_retry(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    result = asyncio.run(service.retry_task(args.id))
    render_data(result if isinstance(result, dict) else _task_to_dict(result), fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_archive(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    task = asyncio.run(service.set_archived(args.id, True, expected_version=args.version))
    render_data(_task_to_dict(task), fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_unarchive(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    task = asyncio.run(service.set_archived(args.id, False, expected_version=args.version))
    render_data(_task_to_dict(task), fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_complete(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    import json as _json

    metadata = _json.loads(args.metadata) if getattr(args, "metadata", None) else {}
    result = asyncio.run(service.complete(args.id, summary=args.summary, metadata=metadata))
    render_data(result if isinstance(result, dict) else _task_to_dict(result), fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_approve(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    note = getattr(args, "note", None)
    result = asyncio.run(service.approve_change(args.id, note=note))
    render_data(result if isinstance(result, dict) else _task_to_dict(result), fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_comment(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    comment = asyncio.run(service.add_comment(args.id, body=args.body, author=getattr(args, "author", "") or "cli"))
    render_data({"id": comment.id, "task_id": comment.task_id, "author": comment.author, "body": comment.body}, fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_reject(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    note = getattr(args, "note", None)
    result = asyncio.run(service.reject_change(args.id, note=note))
    render_data(result if isinstance(result, dict) else _task_to_dict(result), fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_runs(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    runs = asyncio.run(service.list_runs(args.id, limit=getattr(args, "limit", None) or 50))
    items = []
    for r in runs:
        status = getattr(r, "status", None)
        outcome = getattr(r, "outcome", None)
        items.append({
            "id": r.id, "task_id": r.task_id,
            "status": status.value if hasattr(status, "value") else str(status),
            "outcome": outcome.value if hasattr(outcome, "value") else (str(outcome) if outcome else None),
            "started_at": str(getattr(r, "started_at", "")) if getattr(r, "started_at", None) else "",
            "ended_at": str(getattr(r, "ended_at", "")) if getattr(r, "ended_at", None) else "",
            "summary": getattr(r, "summary", None),
            "error": getattr(r, "error", None),
        })
    render_data(items, fmt=resolve_format(args), headers=["id", "task_id", "status", "outcome", "started_at"], console=make_console())
    return 0


def _cmd_events(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    events = asyncio.run(service.list_events(args.id, limit=getattr(args, "limit", None) or 50))
    items = []
    for e in events:
        items.append({
            "id": e.id, "task_id": e.task_id, "kind": e.kind,
            "run_id": getattr(e, "run_id", None),
            "payload": getattr(e, "payload", {}) or {},
            "created_at": str(getattr(e, "created_at", "")) if getattr(e, "created_at", None) else "",
        })
    render_data(items, fmt=resolve_format(args), headers=["id", "task_id", "kind", "created_at"], console=make_console())
    return 0


def _cmd_dispatch(args) -> int:
    run_service = _load_task_run_service()
    if run_service is None:
        return _disabled()
    result = asyncio.run(run_service.dispatch_once())
    render_data(result, fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_propose(args) -> int:
    service = _load_task_service()
    if service is None:
        return _disabled()
    proposal = getattr(args, "proposal", "") or ""
    if not proposal:
        print("proposal is required", file=sys.stderr)
        return 1
    task = asyncio.run(service.get_task(args.id))
    if task is None:
        print(f"task not found: {args.id}", file=sys.stderr)
        return 1
    result = asyncio.run(service.propose_change(args.id, proposal, task.current_run_id))
    render_data(result if isinstance(result, dict) else _task_to_dict(result), fmt=resolve_format(args), console=make_console())
    return 0


def _disabled() -> int:
    console = make_console()
    console.print("[yellow]task subsystem disabled[/yellow]")
    return 0
