from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Body
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.application.knowledge_service import (
    KnowledgeBaseCreateInput,
    KnowledgeBaseUpdateInput,
    KnowledgeProbeInput,
    KnowledgeService,
)
from app.application.mcp_service import McpService, McpSiteInput
from app.application.model_service import ModelService
from app.application.provider_service import (
    ProviderCreateInput,
    ProviderService,
    ProviderUpdateInput,
)
from app.application.schedule_service import (
    ScheduledTaskCreateInput,
    ScheduledTaskNotFoundError,
    ScheduledTaskNotRunnableError,
    ScheduledTaskUpdateInput,
    ScheduleDeliveryContextError,
    ScheduleService,
    ScheduleServiceError,
    ScheduleValidationError,
)
from app.application.session_service import SessionService
from app.application.skill_service import SkillInput, SkillScanReport, SkillScanWarning, SkillService
from app.application.tool_service import ToolService
from app.domain.knowledge import (
    DuplicateKnowledgeBaseError,
    KnowledgeBase,
    KnowledgeBaseNotFoundError,
    KnowledgeBaseType,
    KnowledgeBaseValidationError,
    KnowledgeProbeError,
)
from app.domain.mcp import McpProbeError, McpSite, McpSiteNotFoundError, McpSiteValidationError, McpTool, McpTransportType
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
from app.domain.skill import SkillNotFoundError, SkillReadiness, SkillValidationError
from app.domain.tool import ToolDefinition


STATIC_DIR = Path(__file__).parent / "static"

DependencySnapshot = dict
HealthProvider = Callable[[], DependencySnapshot]


from app.application.external_memory_provider_service import ExternalMemoryProviderService
from app.application.external_memory_service import ExternalMemoryService
from app.domain.external_memory_provider import (
    DuplicateExternalMemoryProviderError,
    ExternalMemoryProviderConfig,
    ExternalMemoryProviderNotFoundError,
    ExternalMemoryProviderType,
    ExternalMemoryProviderValidationError,
)


def create_dashboard_router(
    session_service: SessionService,
    tool_service: ToolService,
    model_service: ModelService,
    health_provider: HealthProvider,
    provider_service: ProviderService | None = None,
    mcp_service: McpService | None = None,
    schedule_service: ScheduleService | None = None,
    skill_service: SkillService | None = None,
    knowledge_service: KnowledgeService | None = None,
    external_memory_service: ExternalMemoryService | None = None,
    external_memory_provider_service: ExternalMemoryProviderService | None = None,
    sandbox_dashboard_service=None,
    plugin_service=None,
    usage_service=None,
    memory_store=None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    @router.get("/summary", response_class=HTMLResponse)
    @router.get("/chat", response_class=HTMLResponse)
    @router.get("/observations/sessions", response_class=HTMLResponse)
    @router.get("/observations/sessions/{session_id}", response_class=HTMLResponse)
    @router.get("/observations/modules", response_class=HTMLResponse)
    @router.get("/sessions", response_class=HTMLResponse)
    @router.get("/memory", response_class=HTMLResponse)
    @router.get("/tools", response_class=HTMLResponse)
    @router.get("/tools/builtin", response_class=HTMLResponse)
    @router.get("/tools/knowledge", response_class=HTMLResponse)
    @router.get("/tools/mcp", response_class=HTMLResponse)
    @router.get("/tools/skill", response_class=HTMLResponse)
    @router.get("/tools/plugin", response_class=HTMLResponse)
    @router.get("/tools/external-memory", response_class=HTMLResponse)
    @router.get("/tools/sandbox", response_class=HTMLResponse)
    @router.get("/sandbox", response_class=HTMLResponse)
    @router.get("/models", response_class=HTMLResponse)
    @router.get("/scheduled-tasks", response_class=HTMLResponse)
    @router.get("/scheduled-tasks/{task_id}", response_class=HTMLResponse)
    @router.get("/platforms", response_class=HTMLResponse)
    async def shell():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    if usage_service is not None:
        from app.interfaces.http.usage_routes import register_usage_routes
        register_usage_routes(router, usage_service, memory_store=memory_store, tool_service=tool_service, skill_service=skill_service)

    if sandbox_dashboard_service is not None:
        from app.interfaces.http.sandbox_routes import register_sandbox_routes
        register_sandbox_routes(router, sandbox_dashboard_service)

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
    if mcp_service is not None:
        _register_mcp_routes(router, mcp_service)
    if schedule_service is not None:
        _register_schedule_routes(router, schedule_service)
    if skill_service is not None:
        _register_skill_routes(router, skill_service)
    if plugin_service is not None:
        from app.interfaces.http.plugin_routes import register_plugin_routes
        register_plugin_routes(router, plugin_service)
    if knowledge_service is not None:
        _register_knowledge_routes(router, knowledge_service, tool_service)

    if external_memory_service is not None:
        @router.get("/chat/external-memory/memory-providers")
        async def list_memory_providers():
            return {"providers": external_memory_service.list_providers()}

        # Only register the legacy /chat/external-memory/providers list route
        # when the new ExternalMemoryProviderService is not wired; otherwise the
        # new provider CRUD routes own this path.
        if external_memory_provider_service is None:
            @router.get("/chat/external-memory/providers")
            async def list_providers():
                return {"providers": external_memory_service.list_providers()}

        @router.post("/chat/external-memory/set-enabled")
        async def set_enabled(payload: dict = Body(...)):
            enabled = payload.get("enabled", [])
            if not isinstance(enabled, list):
                return JSONResponse(
                    status_code=422,
                    content={"error": {"code": "invalid_input", "message": "enabled must be a list"}},
                )
            external_memory_service.save_global_enabled([str(n) for n in enabled])
            return {"status": "ok"}

        @router.post("/chat/external-memory/projects/create")
        async def create_project(payload: dict = Body(...)):
            name = payload.get("name", "")
            if not isinstance(name, str) or not name:
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "name is required"},
                )
            # Validate name pattern
            import re
            if not re.match(r'^[A-Za-z0-9_-]+$', name):
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "name can only contain letters, numbers, hyphens and underscores"},
                )
            ok = external_memory_service.create_project(name)
            if ok:
                return {"success": True}
            else:
                return JSONResponse(
                    status_code=409,
                    content={"success": False, "error": "project already exists or cannot create directory"},
                )

        @router.post("/chat/external-memory/projects/delete")
        async def delete_project(payload: dict = Body(...)):
            name = payload.get("name", "")
            if not isinstance(name, str) or not name:
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "name is required"},
                )
            ok = external_memory_service.delete_project(name)
            if ok:
                return {"success": True}
            else:
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "project does not exist or cannot delete"},
                )

        @router.get("/chat/external-memory/projects/{project_name}/memory")
        async def get_external_memory(project_name: str, target: str = "memory"):
            if target not in ["memory", "user"]:
                return JSONResponse(
                    status_code=422,
                    content={"error": {"code": "invalid_target", "message": "target must be memory or user"}},
                )
            content = external_memory_service.get_external_memory(project_name, target)
            return {"project": project_name, "target": target, "content": content}

        @router.get("/chat/external-memory/projects/{project_name}/entries")
        async def list_project_entries(project_name: str, target: str = "memory"):
            if target not in ["memory", "user"]:
                return JSONResponse(
                    status_code=422,
                    content={"error": {"code": "invalid_target", "message": "target must be memory or user"}},
                )
            entries = external_memory_service.list_project_entries(project_name, target)
            return {"project": project_name, "target": target, "entries": entries}

        @router.put("/chat/external-memory/projects/{project_name}/memory")
        async def save_external_memory(project_name: str, payload: dict = Body(...)):
            content = payload.get("content", "")
            target = payload.get("target", "memory")
            if target not in ["memory", "user"]:
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "target must be memory or user"},
                )
            if not isinstance(content, str):
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "content must be a string"},
                )
            ok = external_memory_service.save_external_memory(project_name, content, target)
            if ok:
                return {"success": True}
            else:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "failed to save external memory"},
                )

        @router.post("/chat/external-memory/projects/{project_name}/entries")
        async def add_project_entry(project_name: str, payload: dict = Body(...)):
            content = payload.get("content", "")
            target = payload.get("target", "memory")
            if target not in ["memory", "user"]:
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "target must be memory or user"},
                )
            if not isinstance(content, str) or not content.strip():
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "content is required"},
                )
            ok = external_memory_service.add_project_entry(project_name, content.strip(), target)
            if ok:
                return {"success": True}
            else:
                return JSONResponse(
                    status_code=400,
                    content={"success": False, "error": "failed to add entry"},
                )

        @router.patch("/chat/external-memory/projects/{project_name}/entries/{entry_index}")
        async def update_project_entry(project_name: str, entry_index: int, payload: dict = Body(...)):
            content = payload.get("content", "")
            target = payload.get("target", "memory")
            if target not in ["memory", "user"]:
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "target must be memory or user"},
                )
            if not isinstance(content, str) or not content.strip():
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "content is required"},
                )
            ok = external_memory_service.update_project_entry(project_name, entry_index, content.strip(), target)
            if ok:
                return {"success": True}
            else:
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "entry not found or failed to update"},
                )

        @router.delete("/chat/external-memory/projects/{project_name}/entries/{entry_index}")
        async def delete_project_entry(project_name: str, entry_index: int, target: str = "memory"):
            if target not in ["memory", "user"]:
                return JSONResponse(
                    status_code=422,
                    content={"success": False, "error": "target must be memory or user"},
                )
            ok = external_memory_service.delete_project_entry(project_name, entry_index, target)
            if ok:
                return {"success": True}
            else:
                return JSONResponse(
                    status_code=404,
                    content={"success": False, "error": "entry not found or failed to delete"},
                )

    if external_memory_provider_service is not None:
        _register_external_memory_provider_routes(router, external_memory_provider_service)

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


def _mcp_error_response(exc: Exception, refresh: bool = False) -> JSONResponse:
    if isinstance(exc, McpSiteNotFoundError):
        return JSONResponse(status_code=404, content={"error": {"code": "mcp_site_not_found", "message": str(exc)}})
    if isinstance(exc, McpSiteValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "mcp_site_invalid", "message": str(exc)}})
    if isinstance(exc, McpProbeError):
        code = "mcp_refresh_failed" if refresh else "mcp_probe_failed"
        return JSONResponse(status_code=502, content={"error": {"code": code, "message": str(exc)}})
    return JSONResponse(status_code=500, content={"error": {"code": "mcp_error", "message": str(exc)}})


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
        "supports_vision": cfg.supports_vision,
    }


def _external_provider_to_dict(cfg: ExternalMemoryProviderConfig) -> dict:
    """Serialize an ExternalMemoryProviderConfig. Never expose api_key plaintext."""
    return {
        "id": cfg.id,
        "name": cfg.name,
        "provider_type": cfg.provider_type.value,
        "base_url": cfg.base_url,
        "api_key_present": cfg.api_key_present,
        "enabled": cfg.enabled,
        "extra_config": cfg.extra_config,
        "probe_status": cfg.probe_status.value if cfg.probe_status else None,
        "last_probe_error": cfg.last_probe_error,
        "last_probed_at": cfg.last_probed_at.isoformat() if cfg.last_probed_at else None,
        "created_at": cfg.created_at,
        "updated_at": cfg.updated_at,
    }


def _external_provider_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ExternalMemoryProviderNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "provider_not_found", "message": str(exc)}},
        )
    if isinstance(exc, DuplicateExternalMemoryProviderError):
        return JSONResponse(
            status_code=409,
            content={"error": {"code": "provider_duplicate", "message": str(exc)}},
        )
    if isinstance(exc, ExternalMemoryProviderValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "provider_invalid", "message": str(exc)}},
        )
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "provider_invalid", "message": str(exc)}},
    )


def _register_external_memory_provider_routes(
    router: APIRouter, service: ExternalMemoryProviderService,
) -> None:
    @router.get("/chat/external-memory/providers")
    async def list_external_providers():
        return {"providers": [_external_provider_to_dict(cfg) for cfg in service.list()]}

    @router.get("/chat/external-memory/providers/{provider_id}")
    async def get_external_provider(provider_id: str):
        try:
            cfg = service.get(provider_id)
        except ExternalMemoryProviderNotFoundError as exc:
            return _external_provider_error_response(exc)
        return _external_provider_to_dict(cfg)

    @router.post("/chat/external-memory/providers")
    async def create_external_provider(payload: dict = Body(...)):
        try:
            provider_type = ExternalMemoryProviderType(payload["provider_type"])
            cfg = service.create(
                name=payload["name"],
                provider_type=provider_type,
                base_url=payload.get("base_url", ""),
                api_key=payload.get("api_key"),
                extra_config=payload.get("extra_config", {}),
            )
        except (DuplicateExternalMemoryProviderError, ExternalMemoryProviderValidationError) as exc:
            return _external_provider_error_response(exc)
        except (KeyError, ValueError, TypeError) as exc:
            return _external_provider_error_response(exc)
        return JSONResponse(content=_external_provider_to_dict(cfg), status_code=201)

    @router.patch("/chat/external-memory/providers/{provider_id}")
    async def update_external_provider(provider_id: str, payload: dict = Body(...)):
        # api_key 三态：null 不变 / "" 清空 / 非空覆盖
        api_key = payload.get("api_key", None)
        clear_api_key = api_key == ""
        api_key_value = api_key if (api_key is not None and api_key != "") else None
        try:
            cfg, refresh_failed = service.update(
                provider_id,
                name=payload.get("name"),
                base_url=payload.get("base_url"),
                api_key=api_key_value,
                clear_api_key=clear_api_key,
                extra_config=payload.get("extra_config"),
            )
        except (ExternalMemoryProviderNotFoundError, DuplicateExternalMemoryProviderError, ExternalMemoryProviderValidationError) as exc:
            return _external_provider_error_response(exc)
        except (KeyError, ValueError, TypeError) as exc:
            return _external_provider_error_response(exc)
        data = _external_provider_to_dict(cfg)
        data["tool_surface_refresh_failed"] = refresh_failed
        return data

    @router.delete("/chat/external-memory/providers/{provider_id}")
    async def delete_external_provider(provider_id: str):
        try:
            service.delete(provider_id)
        except ExternalMemoryProviderNotFoundError as exc:
            return _external_provider_error_response(exc)
        return {"deleted": True}

    @router.post("/chat/external-memory/providers/{provider_id}/activate")
    async def activate_external_provider(provider_id: str):
        try:
            result = service.activate(provider_id)
        except ExternalMemoryProviderNotFoundError as exc:
            return _external_provider_error_response(exc)
        data = _external_provider_to_dict(result.config)
        data["tool_surface_refresh_failed"] = result.tool_surface_refresh_failed
        return data

    @router.post("/chat/external-memory/providers/{provider_id}/probe")
    async def probe_external_provider(provider_id: str):
        try:
            status = service.probe(provider_id)
            cfg = service.get(provider_id)
        except ExternalMemoryProviderNotFoundError as exc:
            return _external_provider_error_response(exc)
        return {"probe_status": status.value, "last_probe_error": cfg.last_probe_error}


def _knowledge_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, KnowledgeBaseNotFoundError):
        return JSONResponse(status_code=404, content={"error": {"code": "knowledge_base_not_found", "message": str(exc)}})
    if isinstance(exc, KnowledgeBaseValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "knowledge_base_invalid", "message": str(exc)}})
    if isinstance(exc, DuplicateKnowledgeBaseError):
        return JSONResponse(status_code=409, content={"error": {"code": "knowledge_base_duplicate", "message": str(exc)}})
    if isinstance(exc, KnowledgeProbeError):
        return JSONResponse(status_code=502, content={"error": {"code": "knowledge_probe_failed", "message": str(exc)}})
    return JSONResponse(status_code=500, content={"error": {"code": "knowledge_base_error", "message": str(exc)}})


def _knowledge_base_to_dict(base: KnowledgeBase) -> dict:
    return {
        "id": base.id,
        "name": base.name,
        "description": base.description,
        "base_type": base.base_type.value,
        "base_url": base.base_url,
        "dataset_id": base.dataset_id,
        "api_key_present": base.api_key_present,
        "enabled": base.enabled,
        "default_top_k": base.default_top_k,
        "default_min_score": base.default_min_score,
        "last_probe_status": base.last_probe_status.value,
        "last_probe_error": base.last_probe_error,
        "last_probed_at": base.last_probed_at.isoformat() if base.last_probed_at else None,
        "created_at": base.created_at.isoformat(),
        "updated_at": base.updated_at.isoformat(),
    }


def _knowledge_create_input(payload: dict) -> KnowledgeBaseCreateInput:
    return KnowledgeBaseCreateInput(
        id=payload.get("id", ""),
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        base_type=KnowledgeBaseType(payload.get("base_type", "n_kb")),
        base_url=payload.get("base_url", ""),
        dataset_id=payload.get("dataset_id", ""),
        api_key=payload.get("api_key"),
        enabled=bool(payload.get("enabled", True)),
        default_top_k=payload.get("default_top_k"),
        default_min_score=payload.get("default_min_score"),
    )


def _knowledge_update_input(payload: dict) -> KnowledgeBaseUpdateInput:
    base_type = payload.get("base_type")
    return KnowledgeBaseUpdateInput(
        name=payload.get("name"),
        description=payload.get("description"),
        base_type=KnowledgeBaseType(base_type) if base_type is not None else None,
        base_url=payload.get("base_url"),
        dataset_id=payload.get("dataset_id"),
        api_key=payload.get("api_key"),
        enabled=payload.get("enabled"),
        default_top_k=payload.get("default_top_k"),
        default_min_score=payload.get("default_min_score"),
        clear_default_top_k=bool(payload.get("clear_default_top_k", False)),
        clear_default_min_score=bool(payload.get("clear_default_min_score", False)),
    )


def _knowledge_probe_input(payload: dict) -> KnowledgeProbeInput:
    return KnowledgeProbeInput(
        name=payload.get("name", ""),
        description=payload.get("description", ""),
        base_type=KnowledgeBaseType(payload.get("base_type", "n_kb")),
        base_url=payload.get("base_url", ""),
        dataset_id=payload.get("dataset_id", ""),
        api_key=payload.get("api_key"),
        default_top_k=payload.get("default_top_k"),
        default_min_score=payload.get("default_min_score"),
    )


async def _refresh_knowledge_tool_definition(knowledge_service: KnowledgeService, tool_service: ToolService) -> ToolDefinition:
    definition = await knowledge_service.knowledge_tool_definition()
    tool_service.definitions[definition.name] = definition
    return definition


def _register_knowledge_routes(router: APIRouter, knowledge_service: KnowledgeService, tool_service: ToolService) -> None:
    @router.get("/chat/knowledge/bases")
    async def list_knowledge_bases():
        return [_knowledge_base_to_dict(base) for base in await knowledge_service.list_bases()]

    @router.get("/chat/knowledge/bases/{kb_id}")
    async def get_knowledge_base(kb_id: str):
        try:
            base = await knowledge_service.get_base(kb_id)
        except KnowledgeBaseNotFoundError as exc:
            return _knowledge_error_response(exc)
        return _knowledge_base_to_dict(base)

    @router.post("/chat/knowledge/tools/refresh")
    async def refresh_knowledge_tool():
        definition = await _refresh_knowledge_tool_definition(knowledge_service, tool_service)
        return _tool_definition_to_dict(definition)

    @router.post("/chat/knowledge/bases")
    async def create_knowledge_base(payload: dict = Body(...)):
        try:
            base = await knowledge_service.create_base(_knowledge_create_input(payload))
            await _refresh_knowledge_tool_definition(knowledge_service, tool_service)
        except (KnowledgeBaseNotFoundError, KnowledgeBaseValidationError, DuplicateKnowledgeBaseError, KnowledgeProbeError) as exc:
            return _knowledge_error_response(exc)
        return _knowledge_base_to_dict(base)

    @router.patch("/chat/knowledge/bases/{kb_id}")
    async def update_knowledge_base(kb_id: str, payload: dict = Body(...)):
        try:
            base = await knowledge_service.update_base(kb_id, _knowledge_update_input(payload))
            await _refresh_knowledge_tool_definition(knowledge_service, tool_service)
        except (KnowledgeBaseNotFoundError, KnowledgeBaseValidationError, DuplicateKnowledgeBaseError) as exc:
            return _knowledge_error_response(exc)
        return _knowledge_base_to_dict(base)

    @router.delete("/chat/knowledge/bases/{kb_id}")
    async def delete_knowledge_base(kb_id: str):
        try:
            await knowledge_service.delete_base(kb_id)
            await _refresh_knowledge_tool_definition(knowledge_service, tool_service)
        except KnowledgeBaseNotFoundError as exc:
            return _knowledge_error_response(exc)
        return Response(status_code=204)

    @router.post("/chat/knowledge/bases/probe")
    async def probe_unsaved_knowledge_base(payload: dict = Body(...)):
        try:
            await knowledge_service.probe_unsaved(_knowledge_probe_input(payload))
        except (KnowledgeBaseValidationError, KnowledgeProbeError) as exc:
            return _knowledge_error_response(exc)
        return {"status": "success"}

    @router.post("/chat/knowledge/bases/{kb_id}/probe")
    async def probe_saved_knowledge_base(kb_id: str):
        try:
            await knowledge_service.probe_base(kb_id)
        except (KnowledgeBaseNotFoundError, KnowledgeProbeError) as exc:
            return _knowledge_error_response(exc)
        return {"status": "success"}


def _register_mcp_routes(router: APIRouter, mcp_service: McpService) -> None:
    @router.get("/chat/mcp/sites")
    async def list_mcp_sites():
        return [_mcp_site_to_dict(site) for site in await mcp_service.list_sites()]

    @router.post("/chat/mcp/sites/probe")
    async def probe_mcp_site(payload: dict = Body(...)):
        try:
            result = await mcp_service.probe_site(_mcp_input(payload))
        except (McpSiteValidationError, McpProbeError) as exc:
            return _mcp_error_response(exc)
        return {"tools": [{"name": tool.name, "description": tool.description, "input_schema": tool.input_schema} for tool in result.tools]}

    @router.post("/chat/mcp/sites")
    async def create_mcp_site(payload: dict = Body(...)):
        try:
            site = await mcp_service.create_site_with_probe(_mcp_input(payload), payload.get("tool_include"))
        except (McpSiteValidationError, McpProbeError) as exc:
            return _mcp_error_response(exc)
        return _mcp_site_to_dict(site)

    @router.patch("/chat/mcp/sites/{site_id}")
    async def update_mcp_site(site_id: str, payload: dict = Body(...)):
        try:
            site = await mcp_service.update_site(site_id, _mcp_input(payload))
        except (McpSiteNotFoundError, McpSiteValidationError) as exc:
            return _mcp_error_response(exc)
        return _mcp_site_to_dict(site)

    @router.delete("/chat/mcp/sites/{site_id}")
    async def delete_mcp_site(site_id: str):
        try:
            await mcp_service.delete_site(site_id)
        except McpSiteNotFoundError as exc:
            return _mcp_error_response(exc)
        return Response(status_code=204)

    @router.post("/chat/mcp/sites/{site_id}/refresh")
    async def refresh_mcp_site(site_id: str):
        try:
            tools = await mcp_service.refresh_site_tools(site_id)
        except (McpSiteNotFoundError, McpSiteValidationError, McpProbeError) as exc:
            return _mcp_error_response(exc, refresh=True)
        return [_mcp_tool_to_dict(tool) for tool in tools]

    @router.get("/chat/mcp/sites/{site_id}/tools")
    async def list_mcp_site_tools(site_id: str):
        try:
            tools = await mcp_service.list_site_tools(site_id)
        except McpSiteNotFoundError as exc:
            return _mcp_error_response(exc)
        return [_mcp_tool_to_dict(tool) for tool in tools]

    @router.patch("/chat/mcp/sites/{site_id}/tools/{tool_id}")
    async def toggle_mcp_tool(site_id: str, tool_id: str, payload: dict = Body(...)):
        try:
            tool = await mcp_service.set_tool_enabled(site_id, tool_id, bool(payload.get("enabled")))
        except (McpSiteNotFoundError, McpSiteValidationError) as exc:
            return _mcp_error_response(exc)
        return _mcp_tool_to_dict(tool)


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
        supports_vision_raw = payload.get("supports_vision")
        if supports_vision_raw is not None and not isinstance(supports_vision_raw, bool):
            return _provider_error_response(ProviderValidationError("supports_vision must be a boolean"))
        try:
            cfg = await provider_service.create_provider(
                ProviderCreateInput(
                    name=payload.get("name", ""),
                    base_url=payload.get("base_url", ""),
                    model=payload.get("model", ""),
                    api_key=payload.get("api_key", ""),
                    provider_type=payload.get("provider_type", "openai-compatible"),
                    extra_headers=payload.get("extra_headers"),
                    supports_vision=supports_vision_raw,
                )
            )
        except (ProviderValidationError, DuplicateProviderError) as exc:
            return _provider_error_response(exc)
        return _provider_to_dict(cfg)

    @router.patch("/chat/providers/{provider_id}")
    async def update_provider(provider_id: str, payload: dict = Body(...)):
        supports_vision_raw = payload.get("supports_vision")
        if supports_vision_raw is not None and not isinstance(supports_vision_raw, bool):
            return _provider_error_response(ProviderValidationError("supports_vision must be a boolean"))
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
                    supports_vision=supports_vision_raw,
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
        except (ProviderNotFoundError, ProviderValidationError) as exc:
            return _provider_error_response(exc)
        return _provider_to_dict(cfg)


def _register_schedule_routes(router: APIRouter, schedule_service: ScheduleService) -> None:
    @router.get("/chat/scheduled-tasks")
    async def list_scheduled_tasks():
        return [_scheduled_task_to_dict(task) for task in await schedule_service.list()]

    @router.post("/chat/scheduled-tasks")
    async def create_scheduled_task(payload: dict = Body(...)):
        if payload.get("delivery_target") == "origin" or payload.get("origin") or payload.get("delivery_context"):
            return _schedule_error_response(ScheduleDeliveryContextError("Dashboard cannot create origin delivery tasks"))
        try:
            task = await schedule_service.create(
                ScheduledTaskCreateInput(
                    name=payload.get("name", ""),
                    prompt=payload.get("prompt", ""),
                    cron_expression=payload.get("cron_expression", ""),
                    timezone=payload.get("timezone", "Asia/Shanghai"),
                    delivery_target=payload.get("delivery_target", "dashboard"),
                    origin={},
                    session_id=payload.get("session_id"),
                )
            )
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)
        return _scheduled_task_to_dict(task)

    @router.get("/chat/scheduled-tasks/{task_id}")
    async def get_scheduled_task(task_id: str):
        try:
            task = await schedule_service.get(task_id)
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)
        return _scheduled_task_to_dict(task)

    @router.patch("/chat/scheduled-tasks/{task_id}")
    async def update_scheduled_task(task_id: str, payload: dict = Body(...)):
        try:
            task = await schedule_service.get(task_id)
            request = _scheduled_task_update_input(payload, task)
            return _scheduled_task_to_dict(await schedule_service.update(task_id, request))
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)

    @router.get("/chat/scheduled-tasks/{task_id}/executions")
    async def list_scheduled_task_executions(task_id: str, limit: int = 10):
        try:
            return [_scheduled_execution_to_dict(item) for item in await schedule_service.list_executions(task_id, limit)]
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)

    @router.post("/chat/scheduled-tasks/{task_id}/pause")
    async def pause_scheduled_task(task_id: str):
        try:
            return _scheduled_task_to_dict(await schedule_service.pause(task_id))
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)

    @router.post("/chat/scheduled-tasks/{task_id}/resume")
    async def resume_scheduled_task(task_id: str):
        try:
            return _scheduled_task_to_dict(await schedule_service.resume(task_id))
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)

    @router.post("/chat/scheduled-tasks/{task_id}/run")
    async def run_scheduled_task(task_id: str):
        try:
            return await schedule_service.run_now(task_id)
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)

    @router.delete("/chat/scheduled-tasks/{task_id}")
    async def delete_scheduled_task(task_id: str):
        try:
            await schedule_service.delete(task_id)
        except ScheduleServiceError as exc:
            return _schedule_error_response(exc)
        return Response(status_code=204)


def _skill_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, SkillNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "skill_not_found", "message": str(exc)}},
        )
    if isinstance(exc, SkillValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "skill_invalid", "message": str(exc)}},
        )
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "skill_scan_failed", "message": str(exc)}},
    )


def _skill_to_dict(skill) -> dict:
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "relative_path": skill.relative_path,
        "platforms": skill.platforms,
        "enabled": skill.enabled,
        "readiness": skill.readiness.value,
        "last_scan_status": skill.last_scan_status,
        "last_scan_error": skill.last_scan_error,
        "frontmatter": skill.frontmatter.raw,
    }


def _skill_input_from_payload(payload: dict, current=None) -> SkillInput:
    readiness_value = payload.get(
        "readiness",
        getattr(current.readiness, "value", "available") if current else "available",
    )
    try:
        readiness = SkillReadiness(readiness_value)
    except ValueError as exc:
        raise SkillValidationError("invalid readiness") from exc
    raw_frontmatter = payload.get("frontmatter")
    if raw_frontmatter is None and current is not None:
        raw_frontmatter = dict(current.frontmatter.raw)
    elif raw_frontmatter is None:
        raw_frontmatter = {}
    enabled = payload.get("enabled", current.enabled if current else True)
    if not isinstance(enabled, bool):
        raise SkillValidationError("enabled must be boolean")
    return SkillInput(
        name=str(payload.get("name", current.name if current else "") or ""),
        relative_path=str(payload.get("relative_path", current.relative_path if current else "") or ""),
        description=str(payload.get("description", current.description if current else "") or ""),
        platforms=_skill_platforms_from_payload(payload.get("platforms", current.platforms if current else [])),
        enabled=enabled,
        readiness=readiness,
        frontmatter=raw_frontmatter,
    )


def _skill_platforms_from_payload(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise SkillValidationError("platforms must be a string list")


def _register_skill_routes(router: APIRouter, skill_service: SkillService) -> None:
    @router.get("/chat/skills")
    async def list_skills():
        items = await skill_service.list_skills(include_disabled=True)
        return {"skills": [_skill_to_dict(s) for s in items]}

    @router.post("/chat/skills")
    async def create_skill(payload: dict = Body(...)):
        try:
            skill = await skill_service.create_skill(_skill_input_from_payload(payload))
        except Exception as exc:
            return _skill_error_response(exc)
        return _skill_to_dict(skill)

    @router.get("/chat/skills/{name}")
    async def get_skill(name: str):
        try:
            skill = await skill_service.get(name)
        except SkillNotFoundError as exc:
            return _skill_error_response(exc)
        view = await skill_service.render_view(name)
        return {
            "skill": _skill_to_dict(skill),
            "content": view.get("content", ""),
            "linked_files": view.get("linked_files", {}),
        }

    @router.patch("/chat/skills/{name}")
    async def patch_skill(name: str, payload: dict = Body(...)):
        try:
            if set(payload.keys()) <= {"enabled"}:
                enabled = payload.get("enabled")
                if not isinstance(enabled, bool):
                    raise SkillValidationError("enabled must be boolean")
                skill = await skill_service.set_enabled(name, enabled)
            else:
                current = await skill_service.get(name)
                skill = await skill_service.update_skill(name, _skill_input_from_payload(payload, current))
        except Exception as exc:
            return _skill_error_response(exc)
        return _skill_to_dict(skill)

    @router.delete("/chat/skills/{name}")
    async def delete_skill(name: str):
        try:
            await skill_service.delete_skill(name)
        except Exception as exc:
            return _skill_error_response(exc)
        return Response(status_code=204)

    @router.post("/chat/skills/refresh")
    async def refresh_skills():
        try:
            report = await skill_service.scan_now()
        except Exception as exc:
            return _skill_error_response(exc)
        return {
            "skills_count": report.skills_count,
            "warnings": [
                {
                    "relative_path": w.relative_path,
                    "reason": w.reason,
                    "detail": w.detail,
                    "first_path": w.first_path,
                }
                for w in report.warnings
            ],
        }



def _scheduled_task_to_dict(task) -> dict:
    return {
        "id": task.id,
        "name": task.name,
        "prompt": task.prompt,
        "cron_expression": task.schedule.value,
        "timezone": task.timezone.value,
        "enabled": task.enabled,
        "status": task.status.value,
        "session_id": task.session_id,
        "delivery_target": task.delivery_target.target_type.value,
        "delivery_context": task.delivery_target.context,
        "origin": task.origin,
        "next_run_at": task.next_run_at.isoformat(),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "last_run_at": task.last_run_at.isoformat() if task.last_run_at else None,
        "last_status": task.last_status.value if task.last_status else None,
        "last_error": task.last_error,
        "last_delivery_error": task.last_delivery_error,
        "unread_count": task.unread_count,
    }


def _scheduled_execution_to_dict(execution) -> dict:
    return {
        "id": execution.id,
        "task_id": execution.task_id,
        "session_id": execution.session_id,
        "status": execution.status.value,
        "claimed_next_run_at": execution.claimed_next_run_at.isoformat() if execution.claimed_next_run_at else None,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": execution.completed_at.isoformat() if execution.completed_at else None,
        "output": execution.output,
        "error": execution.error,
        "delivery_status": execution.delivery_status,
        "delivery_error": execution.delivery_error,
        "created_at": execution.created_at.isoformat() if execution.created_at else None,
    }


def _scheduled_task_update_input(payload: dict, task) -> ScheduledTaskUpdateInput:
    if task.delivery_target.target_type.value == "origin":
        return ScheduledTaskUpdateInput(
            name=payload.get("name"),
            prompt=payload.get("prompt"),
            cron_expression=payload.get("cron_expression"),
            timezone=payload.get("timezone"),
        )
    delivery_target = payload.get("delivery_target")
    if delivery_target == "origin":
        raise ScheduleDeliveryContextError("Dashboard cannot create origin delivery tasks")
    return ScheduledTaskUpdateInput(
        name=payload.get("name"),
        prompt=payload.get("prompt"),
        cron_expression=payload.get("cron_expression"),
        timezone=payload.get("timezone"),
        delivery_target=delivery_target,
        session_id=payload.get("session_id"),
    )


def _schedule_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ScheduledTaskNotFoundError):
        return JSONResponse(status_code=404, content={"error": {"code": "scheduled_task_not_found", "message": str(exc)}})
    if isinstance(exc, ScheduledTaskNotRunnableError):
        return JSONResponse(status_code=409, content={"error": {"code": exc.code, "message": str(exc)}})
    if isinstance(exc, ScheduleDeliveryContextError):
        return JSONResponse(status_code=422, content={"error": {"code": "scheduled_task_delivery_context_invalid", "message": str(exc)}})
    if isinstance(exc, ScheduleValidationError):
        return JSONResponse(status_code=422, content={"error": {"code": "scheduled_task_invalid", "message": str(exc)}})
    return JSONResponse(status_code=500, content={"error": {"code": "scheduled_task_error", "message": str(exc)}})



def _session_to_dict(session: ConversationSession) -> dict:
    return {
        "id": session.id,
        "title": session.title,
        "source": session.source,
        "external_memory_enabled": session.external_memory_enabled,
        "external_memory_slots": session.external_memory_slots,
    }


def _message_to_dict(message: ConversationMessage) -> dict:
    content = message.content
    tool_calls = None
    if message.role == "assistant" and isinstance(content, dict) and "tool_calls" in content:
        tool_calls = content.get("tool_calls") or []
        content = content.get("content", "")
    data = {
        "id": message.id,
        "role": message.role,
        "content": content,
        "tool_call_id": message.tool_call_id,
        "name": message.name,
        "is_summary": message.is_summary,
    }
    if tool_calls:
        data["tool_calls"] = tool_calls
    return data


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
        "source_type": definition.source_type.value,
        "toolset": definition.toolset,
        "description": definition.description,
        "risk_level": definition.risk_level.value,
        "enabled": definition.enabled,
        "input_schema": definition.input_schema,
    }


def _mcp_input(payload: dict) -> McpSiteInput:
    return McpSiteInput(
        name=payload.get("name", ""),
        url=payload.get("url", ""),
        transport_type=McpTransportType(payload.get("transport_type", "streamable_http")),
        enabled=bool(payload.get("enabled", True)),
        command=payload.get("command"),
        args=payload.get("args"),
        env=payload.get("env"),
    )


def _mcp_site_to_dict(site: McpSite) -> dict:
    return {
        "id": site.id,
        "name": site.name,
        "transport_type": site.transport_type.value,
        "url": site.url,
        "command": site.command,
        "args": site.args,
        "env": site.env,
        "enabled": site.enabled,
        "last_probe_status": site.last_probe_status.value,
        "last_probe_error": site.last_probe_error,
        "last_probed_at": site.last_probed_at.isoformat() if site.last_probed_at else None,
        "created_at": site.created_at.isoformat(),
        "updated_at": site.updated_at.isoformat(),
    }


def _mcp_tool_to_dict(tool: McpTool) -> dict:
    return {
        "id": tool.id,
        "site_id": tool.site_id,
        "remote_name": tool.remote_name,
        "local_name": tool.local_name,
        "description": tool.description,
        "input_schema": tool.input_schema,
        "enabled": tool.enabled,
        "last_seen_at": tool.last_seen_at.isoformat(),
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
