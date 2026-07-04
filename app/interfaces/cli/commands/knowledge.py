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


def _load_knowledge_service() -> Any:
    from app.main import build_application_services

    return build_application_services().knowledge_service


def run(args) -> int:
    cmd = args.knowledge_command
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
    if cmd == "probe":
        return _cmd_probe(args)
    return 0


def _enum_value(v: Any) -> str:
    if hasattr(v, "value"):
        return v.value
    return str(v) if v else ""


def _to_dict(kb: Any) -> dict[str, Any]:
    base_type = kb.base_type
    return {
        "id": kb.id,
        "name": kb.name,
        "base_type": base_type.value if hasattr(base_type, "value") else str(base_type),
        "base_url": kb.base_url,
        "dataset_id": kb.dataset_id,
        "enabled": kb.enabled,
        "api_key_present": getattr(kb, "api_key_present", False),
        "last_probe_status": _enum_value(getattr(kb, "last_probe_status", "")),
        "last_probe_error": getattr(kb, "last_probe_error", "") or "",
        "default_top_k": getattr(kb, "default_top_k", None),
        "default_min_score": getattr(kb, "default_min_score", None),
    }


_LIST_HEADERS = ["id", "name", "base_type", "base_url", "dataset_id", "enabled", "last_probe_status", "api_key_present"]


def _cmd_list(args) -> int:
    service = _load_knowledge_service()
    bases = asyncio.run(service.list_bases())
    rows = [_to_dict(kb) for kb in bases]
    render_data(rows, make_console(), fmt=resolve_format(args), headers=_LIST_HEADERS)
    return 0


def _cmd_get(args) -> int:
    service = _load_knowledge_service()
    try:
        kb = asyncio.run(service.get_base(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(kb), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_create(args) -> int:
    from app.application.knowledge_service import KnowledgeBaseCreateInput
    from app.domain.knowledge import KnowledgeBaseType

    missing = []
    if not args.id:
        missing.append("--id")
    if not args.name:
        missing.append("--name")
    if not args.description:
        missing.append("--description")
    if not args.base_type:
        missing.append("--base-type")
    if not args.base_url:
        missing.append("--base-url")
    if not args.dataset_id:
        missing.append("--dataset-id")
    if missing:
        render_action({"error": f"missing required: {', '.join(missing)}"}, make_console(), fmt=resolve_format(args))
        return 2
    try:
        base_type = KnowledgeBaseType(args.base_type)
    except ValueError as exc:
        render_action({"error": f"invalid base_type: {exc}"}, make_console(), fmt=resolve_format(args))
        return 2
    payload = KnowledgeBaseCreateInput(
        id=args.id,
        name=args.name,
        description=args.description,
        base_type=base_type,
        base_url=args.base_url,
        dataset_id=args.dataset_id,
        api_key=args.api_key,
        enabled=args.enabled,
        default_top_k=args.default_top_k,
        default_min_score=args.default_min_score,
    )
    service = _load_knowledge_service()
    try:
        kb = asyncio.run(service.create_base(payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(kb), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_update(args) -> int:
    from app.application.knowledge_service import KnowledgeBaseUpdateInput
    from app.domain.knowledge import KnowledgeBaseType

    base_type = None
    if args.base_type:
        try:
            base_type = KnowledgeBaseType(args.base_type)
        except ValueError as exc:
            render_action({"error": f"invalid base_type: {exc}"}, make_console(), fmt=resolve_format(args))
            return 2
    payload = KnowledgeBaseUpdateInput(
        name=args.name,
        description=args.description,
        base_type=base_type,
        base_url=args.base_url,
        dataset_id=args.dataset_id,
        api_key=args.api_key,
        enabled=args.enabled,
        default_top_k=args.default_top_k,
        default_min_score=args.default_min_score,
        clear_default_top_k=args.clear_default_top_k,
        clear_default_min_score=args.clear_default_min_score,
    )
    service = _load_knowledge_service()
    try:
        kb = asyncio.run(service.update_base(args.id, payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(kb), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_delete(args) -> int:
    service = _load_knowledge_service()
    try:
        asyncio.run(service.delete_base(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({"deleted": args.id}, make_console(), fmt=resolve_format(args))
    return 0


def _cmd_probe(args) -> int:
    service = _load_knowledge_service()
    try:
        asyncio.run(service.probe_base(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({"probe": "ok", "id": args.id}, make_console(), fmt=resolve_format(args))
    return 0
