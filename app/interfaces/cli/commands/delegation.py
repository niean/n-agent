"""CLI commands for the Delegation subdomain (T13).

``n-agent delegation list|show|events|cancel``. Mirrors the task CLI
pattern: ``_load_delegation_registry`` indirection (so tests can
monkeypatch), ``asyncio.run`` for async registry calls, and
``--json``/``--form``/``--yaml`` output via ``render_data`` /
``resolve_format``.

Output is a safe projection: internal session ids, lease/claim tokens, and
unfiltered policy JSON are never printed.
"""
from __future__ import annotations

import argparse
import asyncio
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_data,
    resolve_format,
)


def _load_delegation_registry() -> Any:
    """Lazy indirection so tests can monkeypatch without importing main."""
    from app.main import build_application_services

    services = build_application_services()
    return getattr(services, "delegation_registry", None)


def _enum_value(x: Any) -> Any:
    return x.value if hasattr(x, "value") else x


def _delegation_to_dict(d: Any) -> dict[str, Any]:
    return {
        "id": d.id,
        "delegation_key": d.delegation_key,
        "status": _enum_value(d.status),
        "parent_source": d.parent.source,
        "parent_scope_id": d.parent.scope_id,
        "join_policy": _enum_value(d.join_policy),
        "aggregation": _enum_value(d.aggregation),
        "deadline_at": d.deadline_at,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


_LIST_HEADERS = ["id", "status", "parent_source", "parent_scope_id", "join_policy"]


def run(args) -> int:
    cmd = getattr(args, "delegation_command", None)
    dispatch = {
        "list": _cmd_list,
        "ls": _cmd_list,
        "show": _cmd_show,
        "events": _cmd_events,
        "cancel": _cmd_cancel,
    }
    handler = dispatch.get(cmd)
    if handler is None:
        console = make_console()
        console.print(f"[red]unknown delegation command: {cmd}[/red]")
        return 2
    return handler(args)


def _cmd_list(args) -> int:
    registry = _load_delegation_registry()
    if registry is None:
        console = make_console()
        console.print("[yellow]delegation subsystem disabled[/yellow]")
        return 0
    scope_id = getattr(args, "scope_id", None)
    status = getattr(args, "status", None)
    delegations = asyncio.run(
        registry.list_delegations(limit=100, scope_id=scope_id, status=status)
    )
    items = [_delegation_to_dict(d) for d in delegations]
    fmt = resolve_format(args)
    render_data(items, fmt=fmt, headers=_LIST_HEADERS, console=make_console())
    return 0


def _cmd_show(args) -> int:
    registry = _load_delegation_registry()
    if registry is None:
        return _disabled()
    delegation = asyncio.run(registry.get(args.id))
    if delegation is None:
        console = make_console()
        console.print(f"[red]delegation not found: {args.id}[/red]")
        return 1
    members = asyncio.run(registry.list_members(args.id))
    item = _delegation_to_dict(delegation)
    item["members"] = [
        {
            "ordinal": m.ordinal,
            "role": _enum_value(m.role),
            "title": m.title,
            "status": _enum_value(m.status),
        }
        for m in members
    ]
    render_data(item, fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_events(args) -> int:
    registry = _load_delegation_registry()
    if registry is None:
        return _disabled()
    delegation = asyncio.run(registry.get(args.id))
    if delegation is None:
        console = make_console()
        console.print(f"[red]delegation not found: {args.id}[/red]")
        return 1
    events = asyncio.run(registry.list_events(args.id, limit=200))
    items = [
        {
            "id": e.id,
            "kind": e.kind,
            "member_ordinal": e.member_ordinal,
            "created_at": e.created_at,
        }
        for e in events
    ]
    render_data(items, fmt=resolve_format(args), console=make_console())
    return 0


def _cmd_cancel(args) -> int:
    registry = _load_delegation_registry()
    if registry is None:
        return _disabled()
    delegation = asyncio.run(registry.get(args.id))
    if delegation is None:
        console = make_console()
        console.print(f"[red]delegation not found: {args.id}[/red]")
        return 1
    if hasattr(delegation, "is_terminal") and delegation.is_terminal:
        console = make_console()
        console.print(
            f"[yellow]delegation already terminal: {_enum_value(delegation.status)}[/yellow]"
        )
        return 0
    asyncio.run(registry.request_cancel(args.id, "user_cancel"))
    render_data(
        {"id": args.id, "status": "cancelling"},
        fmt=resolve_format(args),
        console=make_console(),
    )
    return 0


def _disabled() -> int:
    console = make_console()
    console.print("[yellow]delegation subsystem disabled[/yellow]")
    return 0
