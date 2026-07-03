from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Response
from fastapi.responses import JSONResponse

from app.application.plugin_service import PluginService
from app.domain.plugin import PluginNotFoundError, PluginValidationError


def register_plugin_routes(router: APIRouter, service: PluginService) -> None:
    @router.get("/chat/plugins")
    async def list_plugins():
        plugins = await service.list_plugins(include_disabled=True)
        return {"items": [p.to_public_view() for p in plugins]}

    @router.post("/chat/plugins:refresh")
    async def refresh_plugins():
        try:
            await service.refresh()
        except Exception as exc:
            return _plugin_error_response("plugin_scan_failed", str(exc), 500)
        return {"ok": True}

    @router.patch("/chat/plugins/{key:path}/enabled")
    async def set_enabled(key: str, payload: dict = Body(default_factory=dict)):
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return _plugin_error_response("plugin_invalid", "enabled must be boolean", 422)
        try:
            plugin = await service.set_enabled(key, enabled)
        except PluginNotFoundError:
            return _plugin_error_response("plugin_not_found", f"plugin not found: {key}", 404)
        except Exception as exc:
            return _plugin_error_response("plugin_invalid", str(exc), 422)
        return plugin.to_public_view()

    @router.patch("/chat/plugins/{key:path}/config")
    async def update_config(key: str, payload: dict = Body(default_factory=dict)):
        config = payload.get("config") or {}
        secret_updates = payload.get("secret_updates") or None
        try:
            plugin = await service.update_config(key, config, secret_updates)
        except PluginNotFoundError:
            return _plugin_error_response("plugin_not_found", f"plugin not found: {key}", 404)
        except PluginValidationError as exc:
            return _plugin_error_response("plugin_invalid", str(exc), 422)
        except Exception as exc:
            return _plugin_error_response("plugin_invalid", str(exc), 422)
        return plugin.to_public_view()

    @router.get("/chat/plugins/{key:path}")
    async def get_plugin(key: str):
        plugin = await service.get_plugin(key)
        if plugin is None:
            return _plugin_error_response("plugin_not_found", f"plugin not found: {key}", 404)
        return plugin.to_public_detail()


def _plugin_error_response(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
