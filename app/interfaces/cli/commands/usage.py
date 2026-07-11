from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_data,
    resolve_format,
)
from rich.table import Table

logger = logging.getLogger(__name__)


def _load_usage_service() -> Any:
    from app.main import build_application_services

    return build_application_services().usage_service


def run(args) -> int:
    session_id = getattr(args, "session_id", None)
    if session_id:
        return _cmd_session(args, session_id)
    return _cmd_recent(args)


def _stats_dict(stats: Any) -> dict[str, Any]:
    return {
        "session_id": stats.session_id,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cache_read_tokens": stats.cache_read_tokens,
        "cache_write_tokens": stats.cache_write_tokens,
        "reasoning_tokens": stats.reasoning_tokens,
        "total_tokens": stats.total_tokens,
        "api_call_count": stats.api_call_count,
        "estimated_cost_usd": stats.estimated_cost_usd,
        "cost_status": stats.cost_status,
    }


def _record_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "model": r.model,
        "provider": r.provider,
        "input_tokens": r.input_tokens,
        "output_tokens": r.output_tokens,
        "cache_read_tokens": r.cache_read_tokens,
        "cache_write_tokens": r.cache_write_tokens,
        "reasoning_tokens": r.reasoning_tokens,
        "total_tokens": r.total_tokens,
        "estimated_cost_usd": r.estimated_cost_usd,
        "cost_status": r.cost_status,
        "latency_ms": r.latency_ms,
        "created_at": r.created_at,
    }


def _compression_dict(c: Any) -> dict[str, Any]:
    return {
        "id": c.id,
        "session_id": c.session_id,
        "before_tokens": c.before_tokens,
        "after_tokens": c.after_tokens,
        "tokens_saved": c.tokens_saved,
        "compression_ratio": c.compression_ratio,
        "created_at": c.created_at,
    }


def _cmd_session(args, session_id: str) -> int:
    fmt = resolve_format(args)
    service = _load_usage_service()
    stats, records, compressions = asyncio.run(_gather_session(service, session_id))
    if fmt == "json":
        print(_json.dumps({
            "stats": _stats_dict(stats),
            "records": [_record_dict(r) for r in records],
            "compressions": [_compression_dict(c) for c in compressions],
        }, ensure_ascii=False, indent=2, default=str), flush=True)
        return 0
    _render_session_tables(session_id, stats, records, compressions, fmt)
    return 0


async def _gather_session(service: Any, session_id: str):
    stats = await service.get_session_stats(session_id)
    records = await service.list_records(session_id, limit=50)
    compressions = await service.list_compressions(session_id)
    return stats, records, compressions


def _render_session_tables(session_id: str, stats: Any, records: list, compressions: list, fmt: str) -> None:
    console = make_console()
    stats_table = Table(title=f"Session {session_id} Usage", show_header=True)
    stats_table.add_column("Metric")
    stats_table.add_column("Value", justify="right")
    stats_table.add_row("Input tokens", str(stats.input_tokens))
    stats_table.add_row("Output tokens", str(stats.output_tokens))
    stats_table.add_row("Cache read", str(stats.cache_read_tokens))
    stats_table.add_row("Cache write", str(stats.cache_write_tokens))
    stats_table.add_row("Reasoning", str(stats.reasoning_tokens))
    stats_table.add_row("Total", str(stats.total_tokens))
    stats_table.add_row("API calls", str(stats.api_call_count))
    stats_table.add_row("Cost (USD)", str(stats.estimated_cost_usd))
    stats_table.add_row("Cost status", str(stats.cost_status))
    console.print(stats_table)

    if records:
        rt = Table(title="Recent API Calls", show_header=True)
        for col in ("Time", "Model", "In", "Out", "Cache", "Total", "Cost", "Latency"):
            rt.add_column(col)
        for r in records[:20]:
            cache_total = (r.cache_read_tokens or 0) + (r.cache_write_tokens or 0)
            rt.add_row(
                str(r.created_at or "")[:19],
                str(r.model or "-"),
                str(r.input_tokens),
                str(r.output_tokens),
                str(cache_total),
                str(r.total_tokens),
                str(r.estimated_cost_usd),
                f"{r.latency_ms}ms" if r.latency_ms is not None else "-",
            )
        console.print(rt)

    if compressions:
        ct = Table(title="Compressions", show_header=True)
        for col in ("Time", "Before", "After", "Saved", "Ratio"):
            ct.add_column(col)
        for c in compressions:
            ratio = float(c.compression_ratio or 0)
            ct.add_row(
                str(c.created_at or "")[:19],
                str(c.before_tokens),
                str(c.after_tokens),
                str(c.tokens_saved),
                f"{ratio * 100:.1f}%",
            )
        console.print(ct)


def _cmd_recent(args) -> int:
    fmt = resolve_format(args)
    service = _load_usage_service()
    sessions = asyncio.run(_gather_recent_sessions(service))
    if fmt == "json":
        print(_json.dumps(sessions, ensure_ascii=False, indent=2, default=str), flush=True)
        return 0
    console = make_console()
    if not sessions:
        console.print("[dim](no recent sessions)[/dim]")
        return 0
    table = Table(title="Recent Sessions", show_header=True)
    for col in ("Session ID", "Title", "API Calls", "Total Tokens", "Cost (USD)", "Status"):
        table.add_column(col)
    for s in sessions:
        table.add_row(
            str(s.get("session_id", "")),
            str(s.get("title", "") or "-"),
            str(s.get("api_call_count", 0)),
            str(s.get("total_tokens", 0)),
            str(s.get("estimated_cost_usd", "0")),
            str(s.get("cost_status", "unknown")),
        )
    console.print(table)
    return 0


async def _gather_recent_sessions(service: Any) -> list[dict[str, Any]]:
    from app.main import build_application_services

    services = build_application_services()
    session_service = services.session_service
    sessions = await session_service.list_sessions()
    result: list[dict[str, Any]] = []
    for session in sessions[:20]:
        try:
            stats = await service.get_session_stats(session.id)
            result.append({
                "session_id": stats.session_id,
                "title": getattr(session, "title", "") or "",
                "api_call_count": stats.api_call_count,
                "total_tokens": stats.total_tokens,
                "estimated_cost_usd": stats.estimated_cost_usd,
                "cost_status": stats.cost_status,
            })
        except Exception:
            logger.warning("get_session_stats failed for %s", session.id, exc_info=True)
            result.append({
                "session_id": session.id,
                "title": getattr(session, "title", "") or "",
                "api_call_count": 0,
                "total_tokens": 0,
                "estimated_cost_usd": "0",
                "cost_status": "unknown",
            })
    return result
