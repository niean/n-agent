from __future__ import annotations

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

    return build_application_services().external_memory_provider_service


def _load_memory_service() -> Any:
    from app.main import build_application_services

    return build_application_services().external_memory_service


def run(args) -> int:
    cmd = args.memory_command
    if cmd == "list-providers":
        return _provider_list(args)
    if cmd == "get-provider":
        return _provider_get(args)
    if cmd == "create-provider":
        return _provider_create(args)
    if cmd == "update-provider":
        return _provider_update(args)
    if cmd == "delete-provider":
        return _provider_delete(args)
    if cmd == "activate-provider":
        return _provider_activate(args)
    if cmd == "deactivate-provider":
        return _provider_deactivate(args)
    if cmd == "probe-provider":
        return _provider_probe(args)
    if cmd == "list-projects":
        return _project_list(args)
    if cmd == "get-project":
        return _project_get(args)
    if cmd == "create-project":
        return _project_create(args)
    if cmd == "delete-project":
        return _project_delete(args)
    if cmd == "list-entries":
        return _entry_list(args)
    if cmd == "add-entry":
        return _entry_add(args)
    if cmd == "update-entry":
        return _entry_update(args)
    if cmd == "delete-entry":
        return _entry_delete(args)
    if cmd == "global":
        return _global_set(args)
    return 0


def _provider_to_dict(cfg: Any) -> dict[str, Any]:
    provider_type = cfg.provider_type
    probe_status = cfg.probe_status
    return {
        "id": cfg.id,
        "name": cfg.name,
        "provider_type": provider_type.value if hasattr(provider_type, "value") else str(provider_type),
        "base_url": cfg.base_url,
        "api_key_present": getattr(cfg, "api_key_present", False),
        "enabled": cfg.enabled,
        "extra_config": getattr(cfg, "extra_config", {}) or {},
        "probe_status": probe_status.value if hasattr(probe_status, "value") else (str(probe_status) if probe_status else ""),
        "last_probe_error": getattr(cfg, "last_probe_error", "") or "",
    }


_PROVIDER_LIST_HEADERS = ["id", "name", "provider_type", "base_url", "enabled", "probe_status", "api_key_present"]


def _provider_disabled(fmt: str) -> int:
    render_action({"error": "external memory provider service is disabled"}, make_console(), fmt=fmt)
    return 1


def _memory_disabled(fmt: str) -> int:
    render_action({"error": "external memory service is disabled"}, make_console(), fmt=fmt)
    return 1


def _provider_list(args) -> int:
    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    configs = service.list()
    rows = [_provider_to_dict(c) for c in configs]
    render_data(rows, make_console(), fmt=fmt, headers=_PROVIDER_LIST_HEADERS)
    return 0


def _provider_get(args) -> int:
    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    try:
        cfg = service.get(args.id)
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    render_data(_provider_to_dict(cfg), make_console(), fmt=fmt)
    return 0


def _provider_create(args) -> int:
    from app.domain.external_memory_provider import ExternalMemoryProviderType

    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    try:
        provider_type = ExternalMemoryProviderType(args.type)
    except ValueError as exc:
        render_action({"error": f"invalid provider type: {exc}"}, make_console(), fmt=fmt)
        return 2
    extra_config = {}
    if args.extra_config:
        try:
            extra_config = json.loads(args.extra_config)
        except json.JSONDecodeError as exc:
            render_action({"error": f"invalid --extra-config JSON: {exc}"}, make_console(), fmt=fmt)
            return 2
    try:
        cfg = service.create(
            name=args.name, provider_type=provider_type,
            base_url=args.base_url, api_key=args.api_key,
            extra_config=extra_config,
        )
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    render_data(_provider_to_dict(cfg), make_console(), fmt=fmt)
    return 0


def _provider_update(args) -> int:
    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    extra_config = None
    if args.extra_config:
        try:
            extra_config = json.loads(args.extra_config)
        except json.JSONDecodeError as exc:
            render_action({"error": f"invalid --extra-config JSON: {exc}"}, make_console(), fmt=fmt)
            return 2
    try:
        cfg, refresh_failed = service.update(
            args.id, name=args.name, base_url=args.base_url,
            api_key=args.api_key, clear_api_key=args.clear_api_key,
            extra_config=extra_config,
        )
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    obj = _provider_to_dict(cfg)
    obj["refresh_failed"] = refresh_failed
    render_data(obj, make_console(), fmt=fmt)
    return 0


def _provider_delete(args) -> int:
    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    try:
        ok = service.delete(args.id)
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    if not ok:
        render_action({"error": f"delete failed: {args.id}"}, make_console(), fmt=fmt)
        return 1
    render_action({"deleted": args.id}, make_console(), fmt=fmt)
    return 0


def _provider_activate(args) -> int:
    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    try:
        result = service.activate(args.id)
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    obj = _provider_to_dict(result.config)
    obj["tool_surface_refresh_failed"] = result.tool_surface_refresh_failed
    render_data(obj, make_console(), fmt=fmt)
    return 0


def _provider_deactivate(args) -> int:
    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    try:
        cfg = service.deactivate(args.id)
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    render_data(_provider_to_dict(cfg), make_console(), fmt=fmt)
    return 0


def _provider_probe(args) -> int:
    fmt = resolve_format(args)
    service = _load_provider_service()
    if service is None:
        return _provider_disabled(fmt)
    try:
        status = service.probe(args.id)
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=fmt)
        return 1
    status_value = status.value if hasattr(status, "value") else str(status)
    render_action({"probe": args.id, "status": status_value}, make_console(), fmt=fmt)
    return 0


def _project_list(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    providers = service.list_providers()
    rows = [p for p in providers if p.get("slot") in ("builtin", "project", "multi-project")]
    render_data(rows, make_console(), fmt=fmt)
    return 0


def _project_get(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    content = service.get_external_memory(args.name, args.target)
    render_data({"project": args.name, "target": args.target, "content": content}, make_console(), fmt=fmt)
    return 0


def _project_create(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    ok = service.create_project(args.name)
    if not ok:
        render_action({"error": f"create project failed: {args.name}"}, make_console(), fmt=fmt)
        return 1
    render_action({"created": args.name}, make_console(), fmt=fmt)
    return 0


def _project_delete(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    ok = service.delete_project(args.name)
    if not ok:
        render_action({"error": f"delete project failed: {args.name}"}, make_console(), fmt=fmt)
        return 1
    render_action({"deleted": args.name}, make_console(), fmt=fmt)
    return 0


def _entry_list(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    entries = service.list_project_entries(args.project, args.target)
    render_data(entries, make_console(), fmt=fmt)
    return 0


def _entry_add(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    ok = service.add_project_entry(args.project, args.content, args.target)
    if not ok:
        render_action({"error": "add entry failed"}, make_console(), fmt=fmt)
        return 1
    render_action({"added": f"{args.project}/{args.target}"}, make_console(), fmt=fmt)
    return 0


def _entry_update(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    ok = service.update_project_entry(args.project, args.index, args.content, args.target)
    if not ok:
        render_action({"error": "update entry failed"}, make_console(), fmt=fmt)
        return 1
    render_action({"updated": f"{args.project}/{args.target}[{args.index}]"}, make_console(), fmt=fmt)
    return 0


def _entry_delete(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    ok = service.delete_project_entry(args.project, args.index, args.target)
    if not ok:
        render_action({"error": "delete entry failed"}, make_console(), fmt=fmt)
        return 1
    render_action({"deleted": f"{args.project}/{args.target}[{args.index}]"}, make_console(), fmt=fmt)
    return 0


def _global_set(args) -> int:
    fmt = resolve_format(args)
    service = _load_memory_service()
    if service is None:
        return _memory_disabled(fmt)
    providers = [p.strip() for p in args.providers.split(",") if p.strip()] if args.providers else []
    service.save_global_enabled(providers)
    render_action({"global_enabled": providers}, make_console(), fmt=fmt)
    return 0
