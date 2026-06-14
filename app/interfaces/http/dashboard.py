from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.application.model_service import ModelService
from app.application.provider_service import (
    ProviderCreateInput,
    ProviderService,
    ProviderUpdateInput,
)
from app.application.session_service import SessionService
from app.application.tool_service import ToolService
from app.domain.provider import (
    DuplicateProviderError,
    ModelInfo,
    ProviderConfig,
    ProviderInUseError,
    ProviderNotFoundError,
    ProviderValidationError,
)
from app.domain.session import (
    ConversationMessage,
    ConversationSession,
    SessionNotFoundError,
    SessionValidationError,
    Summary,
    TaskState,
    ToolCall,
)
from app.domain.tool import ToolDefinition


STATIC_DIR = Path(__file__).parent / "static"

DependencySnapshot = dict
HealthProvider = Callable[[], DependencySnapshot]


def create_dashboard_router(
    session_service: SessionService,
    tool_service: ToolService,
    model_service: ModelService,
    health_provider: HealthProvider,
    provider_service: ProviderService | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    @router.get("/summary", response_class=HTMLResponse)
    @router.get("/chat", response_class=HTMLResponse)
    @router.get("/sessions", response_class=HTMLResponse)
    @router.get("/tools", response_class=HTMLResponse)
    @router.get("/models", response_class=HTMLResponse)
    @router.get("/status", response_class=HTMLResponse)
    async def shell():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @router.get("/chat/sessions")
    async def sessions():
        return [_session_to_dict(session) for session in await session_service.list_sessions()]

    @router.post("/chat/sessions")
    async def create_session(session_id: str):
        session = await session_service.create_session(session_id)
        return _session_to_dict(session)

    @router.get("/chat/sessions/{session_id}")
    async def session_detail(session_id: str):
        detail = await session_service.get_session_detail(session_id)
        return {
            "session": _session_to_dict(detail["session"]) if detail["session"] else None,
            "messages": [_message_to_dict(message) for message in detail["messages"]],
            "summary": _summary_to_dict(detail["summary"]) if detail["summary"] else None,
            "task_state": _task_state_to_dict(detail["task_state"]) if detail["task_state"] else None,
        }

    @router.get("/chat/sessions/{session_id}/tool-calls")
    async def tool_calls(session_id: str):
        return [_tool_call_to_dict(tool_call) for tool_call in await session_service.list_tool_calls(session_id)]

    @router.patch("/chat/sessions/{session_id}")
    async def rename_session(session_id: str, payload: dict = Body(...)):
        try:
            session = await session_service.rename_session(session_id, payload.get("title", ""))
        except (SessionNotFoundError, SessionValidationError) as exc:
            return _session_error_response(exc)
        return _session_to_dict(session)

    @router.delete("/chat/sessions/{session_id}")
    async def delete_session(session_id: str):
        try:
            await session_service.delete_session(session_id)
        except SessionNotFoundError as exc:
            return _session_error_response(exc)
        return Response(status_code=204)

    @router.get("/chat/tools")
    async def list_tools():
        return [_tool_definition_to_dict(definition) for definition in tool_service.list_definitions()]

    @router.get("/chat/models")
    async def list_admin_models():
        infos = await model_service.list_models()
        default_model = model_service.default_model
        return {
            "object": "list",
            "default_model": default_model,
            "data": [_model_info_to_dict(info, default_model) for info in infos],
        }

    @router.get("/chat/health/dependencies")
    async def health_dependencies():
        try:
            return health_provider()
        except Exception as exc:  # pragma: no cover - defensive
            return {"error": {"code": "health_check_failed", "message": str(exc)}}

    if provider_service is not None:
        _register_provider_routes(router, provider_service)

    return router


def _session_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, SessionNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "session_not_found", "message": str(exc)}},
        )
    if isinstance(exc, SessionValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "session_title_invalid", "message": str(exc)}},
        )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "session_error", "message": str(exc)}},
    )


def _provider_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ProviderNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "provider_not_found", "message": str(exc)}},
        )
    if isinstance(exc, ProviderInUseError):
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "provider_in_use", "message": str(exc)}},
        )
    if isinstance(exc, DuplicateProviderError):
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "provider_duplicate", "message": str(exc)}},
        )
    if isinstance(exc, ProviderValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "provider_invalid", "message": str(exc)}},
        )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "provider_error", "message": str(exc)}},
    )


def _provider_to_dict(cfg: ProviderConfig) -> dict:
    return {
        "id": cfg.id,
        "name": cfg.name,
        "provider_type": cfg.provider_type,
        "base_url": cfg.base_url,
        "model": cfg.model,
        "api_key_present": cfg.api_key_present,
        "is_active": cfg.is_active,
        "extra_headers": cfg.extra_headers,
        "created_at": cfg.created_at.isoformat(),
        "updated_at": cfg.updated_at.isoformat(),
    }


def _register_provider_routes(router: APIRouter, provider_service: ProviderService) -> None:
    @router.get("/chat/providers")
    async def list_providers():
        items = await provider_service.list_providers()
        return [_provider_to_dict(item) for item in items]

    @router.get("/chat/providers/{provider_id}")
    async def get_provider(provider_id: str):
        cfg = await provider_service.get_provider(provider_id)
        if cfg is None:
            return _provider_error_response(ProviderNotFoundError(provider_id))
        return _provider_to_dict(cfg)

    @router.post("/chat/providers")
    async def create_provider(payload: dict = Body(...)):
        try:
            cfg = await provider_service.create_provider(
                ProviderCreateInput(
                    name=payload.get("name", ""),
                    base_url=payload.get("base_url", ""),
                    model=payload.get("model", ""),
                    api_key=payload.get("api_key", ""),
                    provider_type=payload.get("provider_type", "openai-compatible"),
                    extra_headers=payload.get("extra_headers"),
                )
            )
        except (ProviderValidationError, DuplicateProviderError) as exc:
            return _provider_error_response(exc)
        return _provider_to_dict(cfg)

    @router.patch("/chat/providers/{provider_id}")
    async def update_provider(provider_id: str, payload: dict = Body(...)):
        try:
            cfg = await provider_service.update_provider(
                provider_id,
                ProviderUpdateInput(
                    name=payload.get("name"),
                    base_url=payload.get("base_url"),
                    model=payload.get("model"),
                    provider_type=payload.get("provider_type"),
                    api_key=payload.get("api_key"),
                    extra_headers=payload.get("extra_headers"),
                ),
            )
        except (
            ProviderNotFoundError,
            ProviderValidationError,
            DuplicateProviderError,
        ) as exc:
            return _provider_error_response(exc)
        return _provider_to_dict(cfg)

    @router.delete("/chat/providers/{provider_id}")
    async def delete_provider(provider_id: str):
        try:
            await provider_service.delete_provider(provider_id)
        except (ProviderNotFoundError, ProviderInUseError) as exc:
            return _provider_error_response(exc)
        return Response(status_code=204)

    @router.post("/chat/providers/{provider_id}/activate")
    async def activate_provider(provider_id: str):
        try:
            cfg = await provider_service.activate_provider(provider_id)
        except ProviderNotFoundError as exc:
            return _provider_error_response(exc)
        return _provider_to_dict(cfg)


def _session_to_dict(session: ConversationSession) -> dict:
    return {"id": session.id, "title": session.title, "source": session.source}


def _message_to_dict(message: ConversationMessage) -> dict:
    return {
        "id": message.id,
        "role": message.role,
        "content": message.content,
        "tool_call_id": message.tool_call_id,
        "name": message.name,
    }


def _summary_to_dict(summary: Summary) -> dict:
    return {"session_id": summary.session_id, "summary": summary.summary, "source_message_id": summary.source_message_id}


def _task_state_to_dict(task_state: TaskState) -> dict:
    return {
        "session_id": task_state.session_id,
        "status": task_state.status,
        "iteration_count": task_state.iteration_count,
        "last_error": task_state.last_error,
    }


def _tool_call_to_dict(tool_call: ToolCall) -> dict:
    return {
        "id": tool_call.id,
        "session_id": tool_call.session_id,
        "tool_name": tool_call.tool_name,
        "arguments": tool_call.arguments,
        "result": tool_call.result,
        "status": tool_call.status,
        "duration_ms": tool_call.duration_ms,
    }


def _tool_definition_to_dict(definition: ToolDefinition) -> dict:
    return {
        "name": definition.name,
        "description": definition.description,
        "risk_level": definition.risk_level.value,
        "enabled": definition.enabled,
        "input_schema": definition.input_schema,
    }


def _model_info_to_dict(info: ModelInfo, default_model: str) -> dict:
    return {
        "id": info.id,
        "display_name": info.display_name,
        "provider": info.provider,
        "supports_tools": info.supports_tools,
        "supports_streaming": info.supports_streaming,
        "is_default": info.id == default_model,
    }
