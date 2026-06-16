from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionService
from app.application.gateway_service import GatewayService
from app.application.mcp_service import McpManagementToolExecutor, McpService, McpToolExecutor, mcp_management_tool_definitions
from app.application.model_service import ModelService
from app.application.provider_service import ProviderCreateInput, ProviderService
from app.application.runtime_provider import ActiveProviderHolder
from app.application.schedule_run_service import ScheduleRunService
from app.application.schedule_service import ScheduleService
from app.application.scheduled_agent_executor import ScheduledAgentExecutor
from app.application.scheduler_runner import SchedulerRunner
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions, knowledge_tool_definitions
from app.config import Settings
from app.domain.provider import ProviderConfig
from app.infrastructure.feishu.client import FeishuClient, FeishuConfig
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.mcp.sdk_client import McpClientLimits, McpSdkClient
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_gateway_registry import SQLiteGatewaySessionRegistry
from app.infrastructure.registry.sqlite_mcp_registry import SQLiteMcpSiteRegistry
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry
from app.infrastructure.registry.sqlite_schedule_registry import SQLiteScheduledTaskRegistry
from app.infrastructure.schedule.croniter_calculator import CroniterScheduleCalculator
from app.infrastructure.schedule.outbound import ScheduleOutboundDelivery
from app.infrastructure.schedule.prompt_safety import DeterministicPromptSafetyScanner
from app.infrastructure.session.llm_title_generator import LLMTitleGenerator
from app.infrastructure.tools.builtin import BUILTIN_TOOL_NAMES, build_builtin_tool_executor
from app.infrastructure.tools.composite import CompositeToolExecutor
from app.infrastructure.tools.kb import KnowledgeSearchClient, KnowledgeToolExecutor
from app.interfaces.feishu_long_connection import FeishuLongConnectionGateway
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


@dataclass(frozen=True)
class ApplicationServices:
    settings: Settings
    memory_store: SQLiteMemoryStore
    provider_registry: SQLiteProviderRegistry
    provider_holder: ActiveProviderHolder
    provider_service: ProviderService
    tool_service: ToolService
    mcp_service: McpService
    chat_service: ChatCompletionService
    session_service: SessionService
    model_service: ModelService
    gateway_registry: SQLiteGatewaySessionRegistry
    gateway_service: GatewayService
    schedule_service: ScheduleService
    scheduler_runner: SchedulerRunner
    health_snapshot: Callable[[], dict]


def build_application_services(settings: Settings | None = None) -> ApplicationServices:
    settings = settings or Settings()
    memory_store = SQLiteMemoryStore(settings.sqlite_path)
    summarizer = HeuristicSummarizer()
    registry = SQLiteProviderRegistry(settings.sqlite_path)
    gateway_registry = SQLiteGatewaySessionRegistry(settings.sqlite_path)
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
    mcp_registry = SQLiteMcpSiteRegistry(settings.sqlite_path)
    mcp_client = McpSdkClient(
        McpClientLimits(
            connect_timeout_seconds=settings.mcp_connect_timeout_seconds,
            max_tools=settings.mcp_max_tools,
            max_schema_bytes=settings.mcp_max_schema_bytes,
            max_result_bytes=settings.mcp_max_result_bytes,
            allow_private_hosts=settings.mcp_allow_private_hosts,
        )
    )
    routes = {tool_name: builtin_executor for tool_name in BUILTIN_TOOL_NAMES}
    routes["search_knowledge"] = kb_executor
    tool_definitions = builtin_tool_definitions() + knowledge_tool_definitions(enabled=kb_enabled) + mcp_management_tool_definitions()
    tool_service = ToolService(CompositeToolExecutor(routes), tool_definitions)
    mcp_service = McpService(mcp_registry, mcp_client, tool_service)
    mcp_management_executor = McpManagementToolExecutor(mcp_service)
    for definition in mcp_management_tool_definitions():
        routes[definition.name] = mcp_management_executor
    tool_service.executor = CompositeToolExecutor(routes, fallback=McpToolExecutor(mcp_service))
    try:
        asyncio.run(mcp_service.refresh_registered_tool_surface())
        mcp_status = "ok"
        mcp_error = ""
    except Exception as exc:
        mcp_status = "error"
        mcp_error = str(exc)
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
    schedule_calculator = CroniterScheduleCalculator()
    schedule_scanner = DeterministicPromptSafetyScanner()
    feishu_client = FeishuClient(
        FeishuConfig(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            tenant_key=settings.feishu_tenant_key,
            allowed_open_ids=settings.feishu_allowed_open_ids,
            allowed_chat_ids=settings.feishu_allowed_chat_ids,
        )
    ) if settings.feishu_enabled else None
    schedule_registry = SQLiteScheduledTaskRegistry(
        settings.sqlite_path,
        schedule_calculator,
        missed_grace_seconds=settings.scheduler_missed_grace_seconds,
    )
    scheduled_agent_executor = ScheduledAgentExecutor(chat_service, schedule_scanner)
    schedule_run_service = ScheduleRunService(
        schedule_registry,
        scheduled_agent_executor,
        ScheduleOutboundDelivery(feishu_client),
        max_due_per_tick=settings.scheduler_max_due_per_tick,
        lease_seconds=settings.scheduler_lease_seconds,
    )
    schedule_service = ScheduleService(
        schedule_registry,
        schedule_calculator,
        schedule_scanner,
        session_service,
        schedule_run_service.run_now,
    )
    scheduler_runner = SchedulerRunner(schedule_run_service, settings.scheduler_tick_seconds)

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
            "mcp": {"status": mcp_status, "error": mcp_error},
            "gateway": {"status": "ok" if settings.gateway_enabled else "disabled"},
            "scheduler": {
                "status": "ok" if settings.scheduler_enabled else "disabled",
                "tick_seconds": settings.scheduler_tick_seconds,
                "timezone": settings.scheduler_timezone,
            },
        }

    gateway_service = GatewayService(
        gateway_registry,
        chat_service,
        session_service,
        tool_service,
        model_service,
        health_snapshot,
        schedule_service=schedule_service,
    )
    session_service.on_session_deleted = schedule_service.handle_session_deleted
    return ApplicationServices(
        settings=settings,
        memory_store=memory_store,
        provider_registry=registry,
        provider_holder=holder,
        provider_service=provider_service,
        tool_service=tool_service,
        mcp_service=mcp_service,
        chat_service=chat_service,
        session_service=session_service,
        model_service=model_service,
        gateway_registry=gateway_registry,
        gateway_service=gateway_service,
        schedule_service=schedule_service,
        scheduler_runner=scheduler_runner,
        health_snapshot=health_snapshot,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    services = build_application_services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        feishu_task: asyncio.Task | None = None
        scheduler_task: asyncio.Task | None = None
        if services.settings.scheduler_enabled:
            scheduler_task = asyncio.create_task(services.scheduler_runner.run())
        if services.settings.feishu_enabled:
            feishu_client = FeishuClient(
                FeishuConfig(
                    app_id=services.settings.feishu_app_id,
                    app_secret=services.settings.feishu_app_secret,
                    tenant_key=services.settings.feishu_tenant_key,
                    allowed_open_ids=services.settings.feishu_allowed_open_ids,
                    allowed_chat_ids=services.settings.feishu_allowed_chat_ids,
                )
            )
            gateway = FeishuLongConnectionGateway(services.gateway_service, feishu_client)
            feishu_task = asyncio.create_task(gateway.start())
        try:
            yield
        finally:
            if scheduler_task is not None:
                services.scheduler_runner.stop()
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass
            if feishu_task is not None:
                feishu_task.cancel()
                try:
                    await feishu_task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(title="N-Agent", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_openai_router(services.chat_service, services.model_service))
    app.include_router(
        create_dashboard_router(
            services.session_service,
            services.tool_service,
            services.model_service,
            services.health_snapshot,
            provider_service=services.provider_service,
            mcp_service=services.mcp_service,
            schedule_service=services.schedule_service,
        )
    )
    return app


app = create_app()
