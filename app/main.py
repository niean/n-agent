from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionService
from app.application.model_service import ModelService
from app.application.provider_service import ProviderCreateInput, ProviderService
from app.application.runtime_provider import ActiveProviderHolder
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.config import Settings
from app.domain.provider import ProviderConfig
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry
from app.infrastructure.session.llm_title_generator import LLMTitleGenerator
from app.infrastructure.tools.builtin import BUILTIN_TOOL_NAMES, build_builtin_tool_executor
from app.infrastructure.tools.composite import CompositeToolExecutor
from app.infrastructure.tools.kb import KnowledgeSearchClient, KnowledgeToolExecutor
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router
from app.interfaces.http.openai import create_openai_router


def _provider_factory(cfg: ProviderConfig, api_key: str) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(cfg.base_url, api_key, cfg.model)


async def _seed_and_activate(
    registry: SQLiteProviderRegistry,
    holder: ActiveProviderHolder,
    settings: Settings,
) -> None:
    existing = await registry.list_providers()
    if not existing and settings.provider_base_url and settings.provider_model:
        service = ProviderService(registry, holder)
        await service.create_provider(
            ProviderCreateInput(
                name="default",
                base_url=settings.provider_base_url,
                model=settings.provider_model,
                api_key=settings.provider_api_key or "seed",
            )
        )
    active = await registry.get_active()
    if active is None:
        all_providers = await registry.list_providers()
        if all_providers:
            active = await registry.set_active(all_providers[0].id)
    if active is not None:
        secret = await registry.get_secret(active.id) or ""
        await holder.swap(active, secret)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    memory_store = SQLiteMemoryStore(settings.sqlite_path)
    summarizer = HeuristicSummarizer()
    registry = SQLiteProviderRegistry(settings.sqlite_path)
    holder = ActiveProviderHolder(_provider_factory)
    asyncio.run(_seed_and_activate(registry, holder, settings))
    provider_service = ProviderService(registry, holder)
    builtin_executor = build_builtin_tool_executor(settings.workspace_root)
    kb_enabled = settings.kb_enabled and bool(settings.kb_base_url.strip())
    kb_client = KnowledgeSearchClient(settings.kb_base_url, settings.kb_timeout_seconds) if kb_enabled else None
    kb_executor = KnowledgeToolExecutor(
        kb_client,
        enabled=kb_enabled,
        default_top_k=settings.kb_default_top_k,
        default_min_score=settings.kb_default_min_score,
    )
    routes = {tool_name: builtin_executor for tool_name in BUILTIN_TOOL_NAMES}
    routes["search_knowledge"] = kb_executor
    tool_executor = CompositeToolExecutor(routes)
    tool_definitions = builtin_tool_definitions() + knowledge_tool_definitions(enabled=kb_enabled)
    tool_service = ToolService(tool_executor, tool_definitions)
    graph_runner = AgentGraphRunner(
        holder,
        tool_service,
        memory_store,
        summarizer,
        settings.agent_iteration_limit,
    )
    session_service = SessionService(
        memory_store,
        title_generator=LLMTitleGenerator(holder, lambda: holder.current_model),
    )
    chat_service = ChatCompletionService(memory_store, graph_runner, session_service)
    model_service = ModelService(holder, lambda: holder.current_model)

    app = FastAPI(title="N-Agent")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_openai_router(chat_service, model_service))

    def health_snapshot() -> dict:
        memory_status = "ok"
        try:
            settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            settings.sqlite_path.touch(exist_ok=True)
        except Exception as exc:
            memory_status = f"error: {exc}"
        active = holder.current_config
        provider_configured = active is not None and active.api_key_present
        return {
            "provider": {
                "status": "ok" if provider_configured else "warn",
                "base_url": active.base_url if active else "",
                "model": active.model if active else "",
            },
            "memory": {"status": memory_status, "path": str(settings.sqlite_path)},
            "knowledge": {
                "status": "ok" if kb_enabled else "disabled",
                "base_url": settings.kb_base_url,
                "enabled": kb_enabled,
            },
        }

    app.include_router(
        create_dashboard_router(
            session_service,
            tool_service,
            model_service,
            health_snapshot,
            provider_service=provider_service,
        )
    )
    return app


app = create_app()
