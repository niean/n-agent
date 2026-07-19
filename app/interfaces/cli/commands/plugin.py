from __future__ import annotations

import asyncio
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_action,
    render_data,
    resolve_format,
)


def _load_plugin_service() -> Any:
    from app.main import build_application_services

    return build_application_services().plugin_service


def run(args) -> int:
    if args.plugin_command == "list":
        return _cmd_list(args)
    if args.plugin_command == "view":
        return _cmd_view(args)
    if args.plugin_command == "deps":
        return _cmd_deps(args)
    return 0


def _cmd_list(args) -> int:
    fmt = resolve_format(args)
    service = _load_plugin_service()
    plugins = asyncio.run(service.list_plugins())
    rows = [
        {
            "key": p.key,
            "kind": p.kind.value,
            "source": p.source.value,
            "enabled": p.enabled,
            "description": p.description,
        }
        for p in plugins
    ]
    render_data(rows, make_console(), fmt=fmt, headers=["key", "kind", "source", "enabled", "description"])
    return 0


def _cmd_view(args) -> int:
    fmt = resolve_format(args)
    service = _load_plugin_service()
    plugin = asyncio.run(service.get_plugin(args.name))
    if plugin is None:
        render_action({"success": False, "error": "plugin not found"}, make_console(), fmt=fmt)
        return 1
    render_data(plugin.to_public_detail(), make_console(), fmt=fmt)
    return 0


def _cmd_deps(args) -> int:
    fmt = resolve_format(args)
    service = _load_plugin_service()
    plugin = asyncio.run(service.get_plugin(args.name))
    if plugin is None:
        render_action({"success": False, "error": "plugin not found"}, make_console(), fmt=fmt)
        return 1
    dep_status = _normalize_dependency_status(plugin)
    if fmt == "form":
        _render_deps_text(plugin, dep_status)
        return 0
    render_data(dep_status, make_console(), fmt=fmt)
    return 0


def _normalize_dependency_status(plugin: Any) -> dict[str, Any]:
    """Extract and normalize dependency_status from plugin capabilities.

    Returns a dict with pip/requires_plugins/external/warnings keys. Missing
    or partial structures are filled with empty lists. Never executes or
    interpolates external install/check strings.
    """
    capabilities = getattr(plugin, "capabilities", None) or {}
    raw = capabilities.get("dependency_status") or {}
    return {
        "pip": list(raw.get("pip") or []),
        "requires_plugins": list(raw.get("requires_plugins") or []),
        "external": list(raw.get("external") or []),
        "warnings": list(raw.get("warnings") or []),
    }


def _render_deps_text(plugin: Any, dep_status: dict[str, Any]) -> None:
    """Render dependency_status as categorized human-readable text.

    Each category shows declarations, current status, warnings, and fix hints.
    Empty categories show "None". External install/check strings are output as
    plain text only -- never shell-interpolated or executed.
    """
    print(f"Plugin: {plugin.key}", flush=True)
    print("", flush=True)

    # Pip dependencies
    print("== Pip dependencies ==", flush=True)
    pip = dep_status.get("pip") or []
    if not pip:
        print("  None", flush=True)
    else:
        for item in pip:
            spec = item.get("spec", "")
            name = item.get("name", "")
            status = item.get("status", "")
            installed = item.get("installed_version")
            diag = item.get("diagnostic", "")
            print(
                f"  {spec} [name={name} status={status} installed={installed}]",
                flush=True,
            )
            if diag:
                print(f"    diagnostic: {diag}", flush=True)
    print("", flush=True)

    # External dependencies
    print("== External dependencies ==", flush=True)
    external = dep_status.get("external") or []
    if not external:
        print("  None", flush=True)
    else:
        for item in external:
            name = item.get("name", "")
            install = item.get("install", "")
            check = item.get("check", "")
            print(f"  {name}", flush=True)
            if install:
                print(f"    install: {install}", flush=True)
            if check:
                print(f"    check: {check}", flush=True)
    print("", flush=True)

    # Required plugins
    print("== Required plugins ==", flush=True)
    reqs = dep_status.get("requires_plugins") or []
    if not reqs:
        print("  None", flush=True)
    else:
        for item in reqs:
            key = item.get("key", "")
            available = item.get("available", "")
            reason = item.get("reason", "")
            diag = item.get("diagnostic", "")
            print(
                f"  {key} [available={available} reason={reason}]",
                flush=True,
            )
            if diag:
                print(f"    diagnostic: {diag}", flush=True)
    print("", flush=True)

    # Warnings
    print("== Warnings ==", flush=True)
    warnings = dep_status.get("warnings") or []
    if not warnings:
        print("  None", flush=True)
    else:
        for w in warnings:
            print(f"  - {w}", flush=True)
