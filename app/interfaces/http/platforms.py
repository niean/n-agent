from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.application.platform_service import (
    PaginatedSessions,
    PlatformDetail,
    PlatformInvalidError,
    PlatformNotFoundError,
    PlatformService,
    PlatformView,
)
from app.domain.gateway import GatewayConversation


def create_platforms_router(platform_service: PlatformService) -> APIRouter:
    router = APIRouter()

    @router.get("/chat/gateways")
    async def list_platforms():
        return {"platforms": [_view_to_dict(view) for view in await platform_service.list_platforms()]}

    @router.get("/chat/gateways/{platform}")
    async def get_platform(platform: str):
        try:
            detail = await platform_service.get_platform(platform)
        except (PlatformInvalidError, PlatformNotFoundError) as exc:
            return _platform_error_response(exc)
        return _detail_to_dict(detail)

    @router.get("/chat/gateways/{platform}/sessions")
    async def list_platform_sessions(
        platform: str,
        limit: int = Query(default=20, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ):
        try:
            page = await platform_service.list_platform_sessions(platform, limit, offset)
        except (PlatformInvalidError, PlatformNotFoundError) as exc:
            return _platform_error_response(exc)
        return _page_to_dict(page)

    return router


def _platform_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, PlatformInvalidError):
        return JSONResponse(status_code=422, content={"error": {"code": "platform_invalid", "message": str(exc)}})
    if isinstance(exc, PlatformNotFoundError):
        return JSONResponse(status_code=404, content={"error": {"code": "platform_not_found", "message": str(exc)}})
    return JSONResponse(status_code=500, content={"error": {"code": "platform_error", "message": str(exc)}})


def _detail_to_dict(detail: PlatformDetail) -> dict[str, Any]:
    return {
        "platform": _view_to_dict(detail.platform),
        "total_sessions": detail.total_sessions,
        "active_sessions": detail.active_sessions,
    }


def _page_to_dict(page: PaginatedSessions) -> dict[str, Any]:
    return {
        "items": [_conversation_to_dict(item) for item in page.items],
        "total": page.total,
        "limit": page.limit,
        "offset": page.offset,
    }


def _view_to_dict(view: PlatformView) -> dict[str, Any]:
    return {
        "platform": view.platform.value,
        "display_name": view.display_name,
        "kind": view.kind.value,
        "status": view.status,
        "error_code": view.error_code,
        "error_message": view.error_message,
        "config_summary": dict(view.config_summary),
        "session_count": view.session_count,
        "last_active_at": _iso(view.last_active_at),
    }


def _conversation_to_dict(conversation: GatewayConversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "platform": conversation.platform.value,
        "platform_session_id": _mask_platform_session_id(conversation.platform_session_id),
        "thread_id": conversation.thread_id,
        "display_name": conversation.display_name,
        "active_session_id": conversation.active_session_id,
        "created_at": _iso(conversation.created_at),
        "updated_at": _iso(conversation.updated_at),
    }


def _mask_platform_session_id(value: str) -> str:
    return f"{value[:8]}****" if len(value) > 8 else "****"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
