from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult, ChatCompletionService
from app.application.events import ChatEvent, ChatEventType
from app.application.knowledge_service import (
    KnowledgeBaseCreateInput,
    KnowledgeBaseUpdateInput,
    KnowledgeProbeInput,
    KnowledgeService,
)
from app.application.mcp_service import McpService, McpSiteInput
from app.application.model_service import ModelService
from app.application.policy_snapshot import IngressFacts
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
from app.application.session_bootstrap import SessionBootstrapReader
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
from app.domain.memory import MemoryStore
from app.domain.mcp import McpProbeError, McpSite, McpSiteNotFoundError, McpSiteValidationError, McpTool, McpTransportType
from app.domain.provider import (
    DuplicateProviderError,
    ModelInfo,
    ProviderConfig,
    ProviderInUseError,
    ProviderNotFoundError,
    ProviderValidationError,
)
from app.domain.policy import ExecutionMode
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
    host_terminal_dashboard_service=None,
    plugin_service=None,
    usage_service=None,
    memory_store=None,
    chat_service: ChatCompletionService | None = None,
    policy_dashboard_service=None,
    skill_pending_store=None,
    skill_usage_store=None,
    image_store=None,
    task_security_dashboard_service=None,
    task_service=None,
    task_run_service=None,
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
    @router.get("/executors", response_class=HTMLResponse)
    @router.get("/executors/host", response_class=HTMLResponse)
    @router.get("/models", response_class=HTMLResponse)
    @router.get("/scheduled-tasks", response_class=HTMLResponse)
    @router.get("/scheduled-tasks/{task_id}", response_class=HTMLResponse)
    @router.get("/tasks", response_class=HTMLResponse)
    @router.get("/tasks/{task_id}", response_class=HTMLResponse)
    # 堆叠装饰器自下而上注册：字面路由须在 catch-all 之下，方能先于其命中
    @router.get("/tasks/observations", response_class=HTMLResponse)
    @router.get("/tasks/security", response_class=HTMLResponse)
    @router.get("/observations/tasks", response_class=HTMLResponse)
    @router.get("/platforms", response_class=HTMLResponse)
    @router.get("/security", response_class=HTMLResponse)
    async def shell():
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    if image_store is not None:
        @router.get("/chat/images/{image_id}")
        async def serve_image(image_id: str):
            path = image_store.resolve(image_id)
            if path is None:
                return JSONResponse(
                    {"error": {"code": "image_not_found", "message": "image not found"}},
                    status_code=404,
                )
            return FileResponse(path, media_type=image_store.media_type(image_id))

    if usage_service is not None:
        from app.interfaces.http.usage_routes import register_usage_routes
        register_usage_routes(router, usage_service, memory_store=memory_store, tool_service=tool_service, skill_service=skill_service)

    if sandbox_dashboard_service is not None:
        from app.interfaces.http.sandbox_routes import register_sandbox_routes
        register_sandbox_routes(router, sandbox_dashboard_service)

    if host_terminal_dashboard_service is not None:
        from app.interfaces.http.host_terminal_routes import register_host_terminal_routes
        register_host_terminal_routes(router, host_terminal_dashboard_service)

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

    @router.post("/chat/sessions/{session_id}/messages")
    async def append_session_message(session_id: str, request: Request):
        """持久化 /task 命令记录与结果通知（ui.task_command）。

        客户端只能提交正文 content；服务端固定 role=system、name=ui.task_command。
        会话不存在 -> 404 session_not_found（不复活）；非法 body/Content-Type/超长 -> 422
        session_message_invalid（不写消息）。错误形状统一 {"error":{"code","message"}}。
        """
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("application/json"):
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "session_message_invalid", "message": "content-type must be application/json"}},
            )
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "session_message_invalid", "message": "invalid json body"}},
            )
        if not isinstance(payload, dict) or set(payload.keys()) != {"content"}:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "session_message_invalid", "message": "body must be {content: string}"}},
            )
        try:
            message = await session_service.append_task_command_message(session_id, payload.get("content"))
        except SessionNotFoundError as exc:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "session_not_found", "message": str(exc)}},
            )
        except SessionValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "session_message_invalid", "message": str(exc)}},
            )
        return JSONResponse(status_code=201, content=_message_to_dict(message))

    @router.get("/chat/tools")
    async def list_tools():
        return [_tool_definition_to_dict(definition) for definition in tool_service.list_definitions()]

    if chat_service is not None and memory_store is not None:
        _dashboard_bootstrap = SessionBootstrapReader(memory_store)

        @router.post("/chat/completions")
        async def dashboard_chat_completions(
            payload: dict = Body(...),
            x_session_id: str | None = Header(default=None),
        ):
            # Dashboard is a cross-source operator/debug UI: the session list shows every
            # session regardless of origin, so the operator may continue any existing
            # session (dashboard/api/cli/feishu/dingtalk/wecom/acp/schedule). The session's
            # persisted source is preserved -- describe_unchecked does not rewrite it and
            # raises no scope error; only the ingress source is "dashboard" because the
            # request entered via the Dashboard. (pre-8649dc4 the Dashboard posted to
            # /v1/chat/completions with no source check; this restores that capability.)
            # No implicit create -- new sessions must be created via /chat/sessions first.
            if not x_session_id:
                return JSONResponse(
                    status_code=409,
                    content={"error": {"code": "dashboard_session_scope_mismatch", "message": "X-Session-ID required for dashboard chat"}},
                )
            descriptor = await _dashboard_bootstrap.describe_unchecked(
                x_session_id, provisional_source="dashboard"
            )
            if not descriptor.exists:
                return JSONResponse(
                    status_code=409,
                    content={"error": {"code": "dashboard_session_scope_mismatch", "message": "session does not exist; create via /chat/sessions first"}},
                )

            # Build IngressFacts (verified entry facts; body metadata is NOT promoted)
            # T12: IngressFacts will be passed to RunPolicySnapshotFactory
            ingress = IngressFacts(
                run_id=str(uuid4()),
                session_id=x_session_id,
                source="dashboard",
                actor_id=None,
                execution_mode=ExecutionMode.REALTIME,
                trusted_claims={},
            )

            resolved_model = payload.get("model") or model_service.default_model
            stream = payload.get("stream", True)
            messages = payload.get("messages", [])
            metadata = payload.get("metadata", {})
            options = payload.get("options", {})

            app_input = ChatCompletionInput(
                model=resolved_model,
                messages=messages,
                stream=stream,
                metadata=metadata,
                options=options,
                session_id=x_session_id,
                ingress_facts=ingress,
                session_descriptor=descriptor,
            )
            result = await chat_service.complete(app_input)
            if stream:
                return StreamingResponse(
                    _dashboard_sse(result),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache"},
                )
            assert isinstance(result, ChatCompletionResult)
            if result.finish_reason == "error":
                return JSONResponse(
                    status_code=500,
                    content={"error": {"message": result.message.get("content", "provider failure"), "type": "server_error"}},
                )
            return _dashboard_completion_response(result)

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
        _register_skill_routes(router, skill_service, skill_pending_store, skill_usage_store)
    if plugin_service is not None:
        from app.interfaces.http.plugin_routes import register_plugin_routes
        register_plugin_routes(router, plugin_service)
    if task_security_dashboard_service is not None:
        from app.interfaces.http.task_security_routes import register_task_security_routes
        # MUST register before register_task_routes so /chat/tasks/security is
        # not captured by the /chat/tasks/{task_id} catch-all.
        register_task_security_routes(router, task_security_dashboard_service)
    if task_service is not None:
        from app.interfaces.http.task_routes import register_task_routes
        register_task_routes(router, task_service, task_run_service)
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

    if policy_dashboard_service is not None:
        from app.interfaces.http.policy_routes import register_policy_routes
        register_policy_routes(router, policy_dashboard_service)

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
                    allowed_tools=_normalize_allowed_tools(payload.get("allowed_tools")),
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
    last_scan_error = skill.last_scan_error
    format_status = (
        "warning"
        if last_scan_error in {"format_warning", "injection_warning"}
        else "valid"
    )
    # No DB column persists full scan warning detail; surface the error
    # category so the UI can show something actionable. Future work may
    # store SkillScanWarning detail on the Skill.
    format_messages = [last_scan_error] if last_scan_error else []
    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "relative_path": skill.relative_path,
        "platforms": skill.platforms,
        "enabled": skill.enabled,
        "readiness": skill.readiness.value,
        "source": skill.source.value,
        "last_scan_status": skill.last_scan_status,
        "last_scan_error": skill.last_scan_error,
        "format_status": format_status,
        "format_messages": format_messages,
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


def _skill_pending_to_dict(pw) -> dict:
    return {
        "pending_id": pw.pending_id,
        "action": pw.action.value if hasattr(pw.action, "value") else str(pw.action),
        "skill_name": pw.skill_name,
        "origin": pw.origin.value if hasattr(pw.origin, "value") else str(pw.origin),
        "summary": pw.summary,
        "state": pw.state,
        "created_at": pw.created_at.isoformat() if pw.created_at else None,
    }


def _skill_usage_to_dict(name: str, usage) -> dict:
    return {
        "name": name,
        "created_by": usage.created_by,
        "use_count": usage.use_count,
        "view_count": usage.view_count,
        "patch_count": usage.patch_count,
        "state": usage.state,
        "pinned": usage.pinned,
        "created_at": usage.created_at.isoformat() if usage.created_at else None,
        "last_used_at": usage.last_used_at.isoformat() if usage.last_used_at else None,
        "last_viewed": usage.last_viewed.isoformat() if usage.last_viewed else None,
        "last_patched_at": usage.last_patched_at.isoformat() if usage.last_patched_at else None,
    }


def _skill_manage_result_to_dict(result) -> dict:
    return {
        "success": result.success,
        "staged": result.staged,
        "pending_id": result.pending_id,
        "skill_name": result.skill_name,
        "action": result.action.value if hasattr(result.action, "value") else str(result.action),
        "summary": result.summary,
        "diff": result.diff,
        "error": result.error,
    }


def _register_skill_routes(
    router: APIRouter,
    skill_service: SkillService,
    skill_pending_store=None,
    skill_usage_store=None,
) -> None:
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

    # ---- literal routes: must be registered BEFORE {name} catch-all ----

    if skill_pending_store is not None:
        @router.get("/chat/skills/pending")
        async def list_pending():
            items = await skill_pending_store.list()
            return {"pending": [_skill_pending_to_dict(pw) for pw in items]}

        @router.get("/chat/skills/pending/{pending_id}/diff")
        async def get_pending_diff(pending_id: str):
            pw = await skill_pending_store.get(pending_id)
            if pw is None:
                return JSONResponse(
                    status_code=404,
                    content={"error": {"code": "skill_pending_not_found", "message": f"pending write {pending_id} not found"}},
                )
            return {"diff": pw.diff, "summary": pw.summary}

        @router.post("/chat/skills/pending/{pending_id}/approve")
        async def approve_pending(pending_id: str):
            result = await skill_service.approve_pending(pending_id)
            if not result.success and result.error == "pending_not_found_or_taken":
                return JSONResponse(
                    status_code=404,
                    content={"error": {"code": "skill_pending_not_found", "message": f"pending write {pending_id} not found or already taken"}},
                )
            return _skill_manage_result_to_dict(result)

        @router.post("/chat/skills/pending/{pending_id}/reject")
        async def reject_pending(pending_id: str):
            ok = await skill_pending_store.reject(pending_id)
            return {"rejected": ok}

        @router.post("/chat/skills/pending/approve-all")
        async def approve_all_pending():
            items = await skill_pending_store.list()
            approved = 0
            for pw in items:
                result = await skill_service.approve_pending(pw.pending_id)
                if result.success:
                    approved += 1
            return {"approved": approved}

        @router.post("/chat/skills/pending/reject-all")
        async def reject_all_pending():
            items = await skill_pending_store.list()
            rejected = 0
            for pw in items:
                ok = await skill_pending_store.reject(pw.pending_id)
                if ok:
                    rejected += 1
            return {"rejected": rejected}

    if skill_usage_store is not None:
        @router.get("/chat/skills/usage")
        async def list_skill_usage():
            skills = await skill_service.list_skills(include_disabled=True)
            usage_list = []
            for s in skills:
                usage = await skill_usage_store.get(s.name)
                if usage is not None:
                    usage_list.append(_skill_usage_to_dict(s.name, usage))
            return {"usage": usage_list}

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

    if skill_usage_store is not None:
        @router.patch("/chat/skills/{name}/pin")
        async def pin_skill(name: str, payload: dict = Body(...)):
            try:
                await skill_service.get(name)
            except SkillNotFoundError as exc:
                return _skill_error_response(exc)
            pinned = bool(payload.get("pinned", False))
            await skill_usage_store.set_pinned(name, pinned)
            return {"pinned": pinned}

    # ---- catch-all {name} routes: registered AFTER literal routes ----

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
        "allowed_tools": list(task.execution_policy.allowed_tools),
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
            allowed_tools=_payload_allowed_tools(payload),
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
        allowed_tools=_payload_allowed_tools(payload),
    )


def _normalize_allowed_tools(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = [name.strip() for name in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(name).strip() for name in value]
    else:
        return ()
    return tuple(name for name in items if name)


def _payload_allowed_tools(payload: dict) -> tuple[str, ...] | None:
    if "allowed_tools" not in payload:
        return None
    return _normalize_allowed_tools(payload.get("allowed_tools"))


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


def _normalize_tool_call_arguments(args):
    """Normalize tool_call arguments JSON string to readable UTF-8 (no \\uXXXX escapes).

    部分 LLM provider 返回的 arguments JSON 字符串含 \\uXXXX 转义（如中文被转义），
    Dashboard 原样展示人看不懂。parse + 以 ensure_ascii=False 重序列化为可读中文。
    best-effort：非字符串或不可解析时原样返回。
    """
    if not isinstance(args, str):
        return args
    try:
        return json.dumps(json.loads(args), ensure_ascii=False)
    except (ValueError, TypeError):
        return args


def _normalize_tool_call_args(tool_call):
    """Normalize a single tool_call dict's function.arguments (readable UTF-8)."""
    if not isinstance(tool_call, dict):
        return tool_call
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return tool_call
    normalized = dict(tool_call)
    normalized_function = dict(function)
    normalized_function["arguments"] = _normalize_tool_call_arguments(function.get("arguments"))
    normalized["function"] = normalized_function
    return normalized


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
        "source": message.source,
        "card": message.card,
        # 消息时间戳（UTC ISO），供 Dashboard Chat Hover 展示消息时间（飞书风格）
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }
    if tool_calls:
        data["tool_calls"] = [_normalize_tool_call_args(tc) for tc in tool_calls]
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
        "arguments": _normalize_tool_call_arguments(tool_call.arguments),
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


async def _dashboard_sse(events: AsyncIterator[ChatEvent]) -> AsyncIterator[str]:
    async for event in events:
        if event.type is ChatEventType.DONE:
            yield "data: [DONE]\n\n"
            continue
        chunk = _dashboard_chunk_for_event(event)
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _dashboard_chunk_for_event(event: ChatEvent) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    finish_reason = None
    if event.type is ChatEventType.MESSAGE_START:
        delta["role"] = "assistant"
    elif event.type is ChatEventType.CONTENT_DELTA:
        delta["content"] = event.content
    elif event.type is ChatEventType.TOOL_CALL_DELTA:
        delta["tool_calls"] = [event.tool_call]
    elif event.type is ChatEventType.ERROR:
        delta["content"] = event.error or "error"
        finish_reason = "error"
    elif event.type is ChatEventType.MESSAGE_DONE:
        finish_reason = event.finish_reason or "stop"
    return {
        "id": f"chatcmpl-{uuid4()}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "N-Agent",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _dashboard_completion_response(result: ChatCompletionResult) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "N-Agent",
        "choices": [
            {
                "index": 0,
                "message": result.message,
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": result.usage,
    }
