from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService
from app.domain.provider import ModelInfo
from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall
from app.domain.tool import ToolDefinition


STATIC_DIR = Path(__file__).parent / "static"

DependencySnapshot = dict
HealthProvider = Callable[[], DependencySnapshot]


def create_dashboard_router(
    session_service: SessionService,
    tool_service: ToolService,
    model_service: ModelService,
    health_provider: HealthProvider,
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

    return router


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
