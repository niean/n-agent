from __future__ import annotations

import os
from typing import Any

from app.interfaces.cli.render import (
    make_console,
    render_doctor_data,
    resolve_format,
)


def _load_services() -> Any:
    from app.main import build_application_services

    return build_application_services()


def run(args) -> int:
    services = _load_services()
    items: list[dict[str, str]] = []
    items.append(_check_config(services))
    items.append(_check_sqlite(services))
    items.append(_check_workspace(services))
    items.append(_check_provider_registry(services))
    items.append(_check_knowledge(services, probe=args.probe))
    items.append(_check_mcp(services, probe=args.probe))
    items.append(_check_external_memory(services, probe=args.probe))
    items.append(_check_loaders(services))
    render_doctor_data(items, make_console(), fmt=resolve_format(args))
    return 1 if any(item["status"] == "FAIL" for item in items) else 0


def _ok(dim: str, detail: str) -> dict[str, str]:
    return {"dimension": dim, "status": "PASS", "detail": detail}


def _warn(dim: str, detail: str) -> dict[str, str]:
    return {"dimension": dim, "status": "WARN", "detail": detail}


def _fail(dim: str, detail: str) -> dict[str, str]:
    return {"dimension": dim, "status": "FAIL", "detail": detail}


def _check_config(services) -> dict[str, str]:
    try:
        settings = services.settings
        if not getattr(settings, "provider_base_url", ""):
            return _fail("配置完整性", "provider_base_url is empty")
        if not getattr(settings, "provider_model", ""):
            return _fail("配置完整性", "provider_model is empty")
        return _ok("配置完整性", "settings loaded")
    except Exception as exc:
        return _fail("配置完整性", f"{type(exc).__name__}: {exc}")


def _check_sqlite(services) -> dict[str, str]:
    try:
        import asyncio
        sqlite_path = getattr(services.settings, "sqlite_path", None)
        if not sqlite_path or not os.path.exists(str(sqlite_path)):
            return _fail("SQLite 文件", f"sqlite_path not found: {sqlite_path}")
        asyncio.run(services.session_service.list_sessions())
        return _ok("SQLite 文件", f"ok: {sqlite_path}")
    except Exception as exc:
        return _fail("SQLite 文件", f"{type(exc).__name__}: {exc}")


def _check_workspace(services) -> dict[str, str]:
    try:
        workspace_root = getattr(services.settings, "workspace_root", None)
        if not workspace_root or not os.path.isdir(str(workspace_root)):
            return _fail("Workspace 路径", f"workspace_root not found: {workspace_root}")
        return _ok("Workspace 路径", f"ok: {workspace_root}")
    except Exception as exc:
        return _fail("Workspace 路径", f"{type(exc).__name__}: {exc}")


def _check_provider_registry(services) -> dict[str, str]:
    try:
        import asyncio
        asyncio.run(services.provider_service.list_providers())
        return _ok("Provider 注册表", "ok")
    except Exception as exc:
        return _fail("Provider 注册表", f"{type(exc).__name__}: {exc}")


def _check_knowledge(services, probe: bool) -> dict[str, str]:
    try:
        import asyncio
        bases = asyncio.run(services.knowledge_service.list_bases())
        if not bases:
            return _ok("Knowledge 后端", "no enabled KB")
        for kb in bases:
            asyncio.run(services.knowledge_service.get_base(kb.id))
            if probe:
                asyncio.run(services.knowledge_service.probe_base(kb.id))
        return _ok("Knowledge 后端", f"{len(bases)} KB checked" + (" (probed)" if probe else ""))
    except Exception as exc:
        return _fail("Knowledge 后端", f"{type(exc).__name__}: {exc}")


def _check_mcp(services, probe: bool) -> dict[str, str]:
    try:
        import asyncio
        from app.application.mcp_service import McpSiteInput
        sites = asyncio.run(services.mcp_service.list_sites())
        if not sites:
            return _ok("MCP 站点", "no enabled site")
        for site in sites:
            full_site = asyncio.run(services.mcp_service.get_site(site.id))
            if probe:
                payload = McpSiteInput(
                    name=full_site.name, url=full_site.url, transport_type=full_site.transport_type,
                    enabled=full_site.enabled, command=full_site.command,
                    args=list(full_site.args) if full_site.args else None,
                    env=dict(full_site.env) if full_site.env else None,
                )
                asyncio.run(services.mcp_service.probe_site(payload))
        return _ok("MCP 站点", f"{len(sites)} site checked" + (" (probed)" if probe else ""))
    except Exception as exc:
        return _fail("MCP 站点", f"{type(exc).__name__}: {exc}")


def _check_external_memory(services, probe: bool) -> dict[str, str]:
    try:
        provider_service = getattr(services, "external_memory_provider_service", None)
        if provider_service is None:
            return _warn("External Memory Provider", "disabled")
        configs = provider_service.list()
        if not configs:
            return _ok("External Memory Provider", "no provider")
        active = next((c for c in configs if c.enabled), None)
        if active is None:
            return _ok("External Memory Provider", f"{len(configs)} provider (none active)")
        if probe:
            provider_service.probe(active.id)
        return _ok("External Memory Provider", f"active: {active.id}" + (" (probed)" if probe else ""))
    except Exception as exc:
        return _fail("External Memory Provider", f"{type(exc).__name__}: {exc}")


def _check_loaders(services) -> dict[str, str]:
    try:
        import asyncio
        asyncio.run(services.skill_service.list_skills())
        asyncio.run(services.plugin_service.list_plugins())
        if getattr(services, "sandbox_dashboard_service", None) is None:
            return _warn("Skill/Plugin/Sandbox 加载", "sandbox disabled")
        return _ok("Skill/Plugin/Sandbox 加载", "ok")
    except Exception as exc:
        return _fail("Skill/Plugin/Sandbox 加载", f"{type(exc).__name__}: {exc}")
