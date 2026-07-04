from __future__ import annotations

import asyncio
import json
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_action,
    render_data,
    resolve_format,
)


def _load_provider_service() -> Any:
    from app.main import build_application_services

    return build_application_services().provider_service


def run(args) -> int:
    cmd = args.provider_command
    if cmd == "list":
        return _cmd_list(args)
    if cmd == "get":
        return _cmd_get(args)
    if cmd == "create":
        return _cmd_create(args)
    if cmd == "update":
        return _cmd_update(args)
    if cmd == "delete":
        return _cmd_delete(args)
    if cmd == "activate":
        return _cmd_activate(args)
    return 0


def _to_dict(p: Any) -> dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "provider_type": p.provider_type,
        "base_url": p.base_url,
        "model": p.model,
        "is_active": p.is_active,
        "api_key_present": getattr(p, "api_key_present", False),
        "extra_headers": getattr(p, "extra_headers", {}) or {},
    }


_LIST_HEADERS = ["id", "name", "provider_type", "base_url", "model", "is_active", "api_key_present"]


def _cmd_list(args) -> int:
    service = _load_provider_service()
    providers = asyncio.run(service.list_providers())
    rows = [_to_dict(p) for p in providers]
    render_data(rows, make_console(), fmt=resolve_format(args), headers=_LIST_HEADERS)
    return 0


def _cmd_get(args) -> int:
    service = _load_provider_service()
    p = asyncio.run(service.get_provider(args.id))
    if p is None:
        render_action({"error": f"provider not found: {args.id}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(p), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_create(args) -> int:
    from app.application.provider_service import ProviderCreateInput
    if not args.api_key:
        render_action({"error": "--api-key is required"}, make_console(), fmt=resolve_format(args))
        return 2
    extra = json.loads(args.extra_headers) if args.extra_headers else None
    payload = ProviderCreateInput(
        name=args.name,
        provider_type=args.type or "openai-compatible",
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        extra_headers=extra,
    )
    service = _load_provider_service()
    try:
        p = asyncio.run(service.create_provider(payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(p), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_update(args) -> int:
    from app.application.provider_service import ProviderUpdateInput
    extra = json.loads(args.extra_headers) if args.extra_headers else None
    payload = ProviderUpdateInput(
        name=args.name,
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        extra_headers=extra,
    )
    service = _load_provider_service()
    try:
        p = asyncio.run(service.update_provider(args.id, payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(p), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_delete(args) -> int:
    service = _load_provider_service()
    try:
        asyncio.run(service.delete_provider(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({"deleted": args.id}, make_console(), fmt=resolve_format(args))
    return 0


def _cmd_activate(args) -> int:
    service = _load_provider_service()
    try:
        p = asyncio.run(service.activate_provider(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(p), make_console(), fmt=resolve_format(args))
    return 0
