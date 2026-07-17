from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_action,
    render_data,
    resolve_format,
)


def _load_services() -> Any:
    """惰性装配，沿用 skill CLI 隔离风格（不依赖运行时 HTTP 对象）。"""
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    cmd = args.curator_command
    dispatch = {
        "status": _cmd_status,
        "run": _cmd_run,
        "pause": _cmd_pause,
        "resume": _cmd_resume,
        "pin": _cmd_pin,
        "unpin": _cmd_unpin,
        "restore": _cmd_restore,
        "archive": _cmd_archive,
        "prune": _cmd_prune,
        "list-archived": _cmd_list_archived,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        return 0
    return fn(args)


def _cmd_status(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    view = asyncio.run(services.skill_curator_service.get_status_view())
    render_data(view, make_console(), fmt=fmt)
    return 0


def _cmd_run(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    dry = bool(getattr(args, "dry_run", False))
    consolidate = True if bool(getattr(args, "consolidate", False)) else None
    result = asyncio.run(
        services.skill_curator_service.run_curator_review(
            dry_run=dry, consolidate=consolidate
        )
    )
    render_action(
        {
            "started_at": result.started_at,
            "summary": result.summary_so_far,
            "auto_transitions": {
                "checked": result.auto_transitions.checked,
                "marked_stale": result.auto_transitions.marked_stale,
                "archived": result.auto_transitions.archived,
                "reactivated": result.auto_transitions.reactivated,
                "seeded": result.auto_transitions.seeded,
            },
        },
        make_console(),
        fmt=fmt,
    )
    return 0


def _cmd_pause(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    asyncio.run(services.curator_state_store.set_paused(True))
    render_action({"paused": True}, make_console(), fmt=fmt)
    return 0


def _cmd_resume(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    asyncio.run(services.curator_state_store.set_paused(False))
    render_action({"paused": False}, make_console(), fmt=fmt)
    return 0


def _cmd_pin(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    ok, msg = asyncio.run(
        services.skill_curator_service.manual_pin(args.skill, True)
    )
    render_action({"success": ok, "message": msg}, make_console(), fmt=fmt)
    return 0 if ok else 1


def _cmd_unpin(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    ok, msg = asyncio.run(
        services.skill_curator_service.manual_pin(args.skill, False)
    )
    render_action({"success": ok, "message": msg}, make_console(), fmt=fmt)
    return 0 if ok else 1


def _cmd_restore(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    ok, msg = asyncio.run(
        services.skill_curator_service.manual_restore(args.skill)
    )
    render_action({"success": ok, "message": msg}, make_console(), fmt=fmt)
    return 0 if ok else 1


def _cmd_archive(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    ok, msg = asyncio.run(
        services.skill_curator_service.manual_archive(args.skill)
    )
    render_action({"success": ok, "message": msg}, make_console(), fmt=fmt)
    return 0 if ok else 1


def _cmd_prune(args) -> int:
    from app.application.skill_curator_service import _parse_iso

    fmt = resolve_format(args)
    services = _load_services()
    days = getattr(args, "days", 90)
    if days < 1:
        print(f"curator: --days must be >= 1 (got {days})", file=sys.stderr)
        return 2
    svc = services.skill_curator_service
    cfg = svc.get_config()
    rows = asyncio.run(
        svc.skill_usage_store.list_curator_managed(
            prune_seeds=cfg.prune_seeds, protected_names=svc._protected_seeds
        )
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    candidates: list[str] = []
    for r in rows:
        if r.pinned or r.state == "archived":
            continue
        anchor = _parse_iso(r.last_activity_at) or r.created_at
        if anchor is None or anchor > cutoff:
            continue
        candidates.append(r.name)
    if not candidates:
        render_action(
            {"pruned": 0, "message": f"nothing to prune (no skills idle >= {days}d)"},
            make_console(),
            fmt=fmt,
        )
        return 0
    dry = bool(getattr(args, "dry_run", False))
    if dry:
        render_action(
            {"pruned": 0, "candidates": candidates, "dry_run": True},
            make_console(),
            fmt=fmt,
        )
        return 0
    archived = 0
    failures: list[list[str]] = []
    for name in candidates:
        ok, msg = asyncio.run(svc.manual_archive(name))
        if ok:
            archived += 1
        else:
            failures.append([name, msg or ""])
    render_action(
        {"pruned": archived, "total": len(candidates), "failures": failures},
        make_console(),
        fmt=fmt,
    )
    return 0 if not failures else 1


def _cmd_list_archived(args) -> int:
    fmt = resolve_format(args)
    services = _load_services()
    rows = asyncio.run(services.skill_curator_service.list_archived_skills())
    render_data(rows, make_console(), fmt=fmt, headers=["name", "archived_at"])
    return 0


def register_cli(parent: Any, add_format_flags: Any = None) -> None:
    """Attach `curator` subcommands to *parent* argparse parser.

    add_format_flags is main.py's _add_format_flags (adds --json/--form/--yaml);
    passed in to avoid a circular import.
    """
    parent.set_defaults(func=lambda a: (parent.print_help(), 0)[1])
    subs = parent.add_subparsers(dest="curator_command")

    def _fmt(p):
        if add_format_flags is not None:
            add_format_flags(p)
        return p

    _fmt(subs.add_parser("status", help="Show curator status and skill stats")).set_defaults(
        func=_cmd_status
    )

    p_run = subs.add_parser("run", help="Trigger a curator review now")
    p_run.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Report only - no state changes, no archives",
    )
    p_run.add_argument(
        "--consolidate", dest="consolidate", action="store_true",
        help="Force the LLM umbrella-building consolidation pass on for this run",
    )
    p_run.add_argument(
        "--sync", dest="sync", action="store_true",
        help="Synchronous (default); kept for Hermes CLI compatibility",
    )
    _fmt(p_run)
    p_run.set_defaults(func=_cmd_run)

    _fmt(subs.add_parser("pause", help="Pause the curator")).set_defaults(func=_cmd_pause)
    _fmt(subs.add_parser("resume", help="Resume a paused curator")).set_defaults(func=_cmd_resume)

    p_pin = subs.add_parser("pin", help="Pin a skill so the curator never auto-transitions it")
    p_pin.add_argument("skill", help="Skill name")
    _fmt(p_pin)
    p_pin.set_defaults(func=_cmd_pin)

    p_unpin = subs.add_parser("unpin", help="Unpin a skill")
    p_unpin.add_argument("skill", help="Skill name")
    _fmt(p_unpin)
    p_unpin.set_defaults(func=_cmd_unpin)

    p_restore = subs.add_parser("restore", help="Restore an archived skill")
    p_restore.add_argument("skill", help="Skill name")
    _fmt(p_restore)
    p_restore.set_defaults(func=_cmd_restore)

    p_archive = subs.add_parser("archive", help="Manually archive a skill")
    p_archive.add_argument("skill", help="Skill name")
    _fmt(p_archive)
    p_archive.set_defaults(func=_cmd_archive)

    p_prune = subs.add_parser("prune", help="Bulk-archive skills idle for >= N days")
    p_prune.add_argument("--days", type=int, default=90, help="Idle days threshold (default: 90)")
    p_prune.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview only")
    p_prune.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")
    _fmt(p_prune)
    p_prune.set_defaults(func=_cmd_prune)

    _fmt(subs.add_parser("list-archived", help="List archived skills")).set_defaults(
        func=_cmd_list_archived
    )
