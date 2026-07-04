from __future__ import annotations

import asyncio
import json
from typing import Any

from app.domain.mcp import McpTransportType
from app.interfaces.cli.render import (
    make_console,
    render_action,
    render_data,
    resolve_format,
)


_TRANSPORT_ALIASES = {
    "http": McpTransportType.STREAMABLE_HTTP,
    "streamable-http": McpTransportType.STREAMABLE_HTTP,
    "streamable_http": McpTransportType.STREAMABLE_HTTP,
    "sse": McpTransportType.SSE,
    "stdio": McpTransportType.STDIO,
}

_SECRET_KEY_MARKERS = ("token", "password", "secret", "key")


def _load_mcp_service() -> Any:
    from app.main import build_application_services

    return build_application_services().mcp_service


def run(args) -> int:
    cmd = args.mcp_command
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
    if cmd == "refresh":
        return _cmd_refresh(args)
    if cmd == "tools":
        return _cmd_tools(args)
    if cmd == "toggle":
        return _cmd_toggle(args)
    return 0


def _normalize_transport(value: str) -> McpTransportType | None:
    return _TRANSPORT_ALIASES.get(value.lower())


def _parse_str_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON for args: {exc}") from exc
    if not isinstance(parsed, list) or not all(isinstance(x, str) for x in parsed):
        raise ValueError("args must be a JSON array of strings")
    return parsed


def _parse_str_dict(raw: str | None) -> dict[str, str] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON for env: {exc}") from exc
    if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
        raise ValueError("env must be a JSON object of string->string")
    return parsed


def _redact_env(env: dict[str, str] | None) -> dict[str, str]:
    if not env:
        return {}
    redacted: dict[str, str] = {}
    for k, v in env.items():
        lower = k.lower()
        if any(marker in lower for marker in _SECRET_KEY_MARKERS):
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


def _to_dict(site: Any) -> dict[str, Any]:
    transport = site.transport_type
    probe_status = site.last_probe_status
    return {
        "id": site.id,
        "name": site.name,
        "transport_type": transport.value if hasattr(transport, "value") else str(transport),
        "url": site.url,
        "command": site.command,
        "args": list(site.args) if site.args else [],
        "env": _redact_env(dict(site.env) if site.env else {}),
        "enabled": site.enabled,
        "last_probe_status": probe_status.value if hasattr(probe_status, "value") else str(probe_status),
        "last_probe_error": site.last_probe_error or "",
    }


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    return {
        "id": tool.id,
        "site_id": tool.site_id,
        "remote_name": tool.remote_name,
        "local_name": tool.local_name,
        "description": tool.description,
        "enabled": tool.enabled,
    }


def _site_to_input(site: Any) -> Any:
    from app.application.mcp_service import McpSiteInput
    return McpSiteInput(
        name=site.name,
        url=site.url,
        transport_type=site.transport_type,
        enabled=site.enabled,
        command=site.command,
        args=list(site.args) if site.args else None,
        env=dict(site.env) if site.env else None,
    )


_LIST_HEADERS = ["id", "name", "transport_type", "url", "enabled", "last_probe_status"]
_TOOL_LIST_HEADERS = ["id", "remote_name", "local_name", "description", "enabled"]


def _cmd_list(args) -> int:
    service = _load_mcp_service()
    sites = asyncio.run(service.list_sites())
    rows = [_to_dict(s) for s in sites]
    render_data(rows, make_console(), fmt=resolve_format(args), headers=_LIST_HEADERS)
    return 0


def _cmd_get(args) -> int:
    service = _load_mcp_service()
    try:
        site = asyncio.run(service.get_site(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(site), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_create(args) -> int:
    from app.application.mcp_service import McpSiteInput

    transport = _normalize_transport(args.transport)
    if transport is None:
        render_action({"error": f"invalid transport: {args.transport} (allowed: http/sse/stdio)"}, make_console(), fmt=resolve_format(args))
        return 2
    try:
        parsed_args = _parse_str_list(args.args)
        parsed_env = _parse_str_dict(args.env)
    except ValueError as exc:
        render_action({"error": str(exc)}, make_console(), fmt=resolve_format(args))
        return 2
    if transport is McpTransportType.STDIO and not args.command:
        render_action({"error": "--command is required for stdio transport"}, make_console(), fmt=resolve_format(args))
        return 2
    if transport is not McpTransportType.STDIO and not args.url:
        render_action({"error": "--url is required for http/sse transport"}, make_console(), fmt=resolve_format(args))
        return 2
    tool_include = None
    if args.include_tools:
        try:
            tool_include = json.loads(args.include_tools)
        except json.JSONDecodeError as exc:
            render_action({"error": f"invalid --include-tools JSON: {exc}"}, make_console(), fmt=resolve_format(args))
            return 2
    payload = McpSiteInput(
        name=args.name,
        url=args.url or "",
        transport_type=transport,
        enabled=True,
        command=args.command,
        args=parsed_args,
        env=parsed_env,
    )
    service = _load_mcp_service()
    try:
        site = asyncio.run(service.create_site_with_probe(payload, tool_include=tool_include))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(site), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_update(args) -> int:
    from app.application.mcp_service import McpSiteInput

    service = _load_mcp_service()
    try:
        current = asyncio.run(service.get_site(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    try:
        parsed_args = _parse_str_list(args.args)
        parsed_env = _parse_str_dict(args.env)
    except ValueError as exc:
        render_action({"error": str(exc)}, make_console(), fmt=resolve_format(args))
        return 2
    enabled = current.enabled
    if args.enabled:
        enabled = True
    elif args.disabled:
        enabled = False
    payload = McpSiteInput(
        name=args.name or current.name,
        url=args.url or current.url,
        transport_type=current.transport_type,
        enabled=enabled,
        command=args.command if args.command is not None else current.command,
        args=parsed_args if parsed_args is not None else (list(current.args) if current.args else None),
        env=parsed_env if parsed_env is not None else (dict(current.env) if current.env else None),
    )
    try:
        site = asyncio.run(service.update_site(args.id, payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_data(_to_dict(site), make_console(), fmt=resolve_format(args))
    return 0


def _cmd_delete(args) -> int:
    service = _load_mcp_service()
    try:
        asyncio.run(service.delete_site(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({"deleted": args.id}, make_console(), fmt=resolve_format(args))
    return 0


def _cmd_probe(args) -> int:
    service = _load_mcp_service()
    try:
        current = asyncio.run(service.get_site(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    payload = _site_to_input(current)
    try:
        asyncio.run(service.probe_site(payload))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({"probe": "ok", "id": args.id}, make_console(), fmt=resolve_format(args))
    return 0


def _cmd_refresh(args) -> int:
    service = _load_mcp_service()
    try:
        tools = asyncio.run(service.refresh_site_tools(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    rows = [_tool_to_dict(t) for t in tools]
    fmt = resolve_format(args)
    if fmt == "form":
        render_action({"refreshed": len(rows)}, make_console(), fmt="form")
        render_data(rows, make_console(), fmt="form", headers=["id", "remote_name", "local_name", "enabled"])
        return 0
    render_data({"refreshed": len(rows), "tools": rows}, make_console(), fmt=fmt)
    return 0


def _cmd_tools(args) -> int:
    service = _load_mcp_service()
    try:
        tools = asyncio.run(service.list_site_tools(args.id))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    rows = [_tool_to_dict(t) for t in tools]
    render_data(rows, make_console(), fmt=resolve_format(args), headers=_TOOL_LIST_HEADERS)
    return 0


def _cmd_toggle(args) -> int:
    if not args.tool_id:
        render_action({"error": "--tool-id is required"}, make_console(), fmt=resolve_format(args))
        return 2
    enabled = True
    if args.disabled:
        enabled = False
    elif args.enabled is not None:
        enabled = bool(args.enabled)
    service = _load_mcp_service()
    try:
        asyncio.run(service.set_tool_enabled(args.id, args.tool_id, enabled))
    except Exception as exc:
        render_action({"error": f"{type(exc).__name__}: {exc}"}, make_console(), fmt=resolve_format(args))
        return 1
    render_action({"toggled": args.tool_id, "enabled": enabled}, make_console(), fmt=resolve_format(args))
    return 0
