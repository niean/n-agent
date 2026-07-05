from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.application.agent_graph import AgentGraphRunner
from app.application.chat_service import ChatCompletionService
from app.application.gateway_service import GatewayService
from app.application.knowledge_service import KnowledgeBaseCreateInput, KnowledgeService, KnowledgeToolExecutor
from app.application.mcp_service import McpManagementToolExecutor, McpService, McpToolExecutor, mcp_management_tool_definitions
from app.application.model_service import ModelService
from app.application.platform_service import PlatformService
from app.application.provider_service import ProviderCreateInput, ProviderService
from app.application.runtime_provider import ActiveProviderHolder
from app.application.schedule_run_service import ScheduleRunService
from app.application.schedule_service import ScheduleService
from app.application.scheduled_agent_executor import ScheduledAgentExecutor
from app.application.scheduler_runner import SchedulerRunner
from app.application.session_service import SessionService
from app.application.skill_service import SkillService, SkillToolExecutor, skill_tool_definitions
from app.application.plugin_service import PluginService, PluginToolExecutor
from app.application.tool_service import ToolService, builtin_tool_definitions, schedule_tool_definitions
from app.config import Settings
from app.domain.knowledge import KnowledgeBaseType
from app.domain.platform import Platform, PlatformDescriptor, PlatformKind, PlatformRegistry
from app.domain.provider import ProviderConfig
from app.infrastructure.feishu.client import FeishuClient, FeishuConfig
from app.infrastructure.knowledge.http_adapters import HttpKnowledgeRetrieverConfig, KnowledgeHttpRetrieverFactory
from app.infrastructure.llm.anthropic_provider import AnthropicProvider
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.mcp.sdk_client import McpClientLimits, McpSdkClient
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.in_memory_platform_registry import InMemoryPlatformRegistry
from app.infrastructure.registry.sqlite_gateway_registry import SQLiteGatewaySessionRegistry
from app.infrastructure.registry.sqlite_knowledge_registry import SQLiteKnowledgeBaseRegistry
from app.infrastructure.registry.sqlite_mcp_registry import SQLiteMcpSiteRegistry
from app.infrastructure.registry.sqlite_provider_registry import SQLiteProviderRegistry
from app.infrastructure.registry.sqlite_schedule_registry import SQLiteScheduledTaskRegistry
from app.infrastructure.registry.sqlite_skill_registry import SQLiteSkillRegistry
from app.infrastructure.schedule.croniter_calculator import CroniterScheduleCalculator
from app.infrastructure.schedule.outbound import ScheduleOutboundDelivery
from app.infrastructure.schedule.prompt_safety import DeterministicPromptSafetyScanner
from app.infrastructure.session.llm_title_generator import LLMTitleGenerator
from app.infrastructure.skill.file_loader import SkillFileLoader, SkillFileLoaderConfig
from app.infrastructure.skill.seed_runner import seed_default_skills
from app.infrastructure.plugin.file_loader import PluginFileLoader, PluginFileLoaderConfig
from app.infrastructure.plugin.seed_runner import seed_default_plugins
from app.infrastructure.registry.sqlite_plugin_registry import SQLitePluginRegistry
from app.infrastructure.tools.builtin import BUILTIN_TOOL_NAMES, build_builtin_tool_executor
from app.infrastructure.tools.composite import CompositeToolExecutor
from app.infrastructure.tools.schedule_management import ScheduleManagementToolExecutor
from app.domain.external_memory import ExternalMemoryConfigRegistry
from app.domain.external_memory_provider import ExternalMemoryProviderType
from app.application.external_memory_manager import ExternalMemoryManager
from app.application.external_memory_provider_service import ExternalMemoryProviderService
from app.application.external_memory_service import ExternalMemoryService
from app.application.external_memory_tool_executor import ExternalMemoryToolExecutor
from app.infrastructure.memory.builtin_project import BuiltinProjectMemory
from app.infrastructure.registry.sqlite_external_memory_config import SQLiteExternalMemoryConfig
from app.interfaces.feishu_im_adapter import FeishuImAdapter
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router
from app.interfaces.http.openai_compatible import create_openai_compatible_router
from app.interfaces.http.platforms import create_platforms_router
from app.infrastructure.sandbox.callback_tools import (
    PatchTool,
    ReadFileTool,
    SearchFilesTool,
    WebExtractTool,
    WebSearchTool,
    WriteFileTool,
)
from app.infrastructure.sandbox.history_registry import SQLiteSandboxExecutionHistoryRegistry
from app.infrastructure.sandbox.manager import SandboxManager
from app.infrastructure.sandbox.released_registry import SQLiteReleasedSandboxRegistry
from app.infrastructure.sandbox.registry import InMemorySandboxCallbackToolRegistry
from app.infrastructure.sandbox.search_provider import DuckDuckGoHtmlSearchProvider
from app.application.sandbox_dashboard_service import SandboxDashboardService
from app.application.sandbox_tool_executor import SandboxToolExecutor
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType

if TYPE_CHECKING:
    from app.infrastructure.sandbox.manager import SandboxManager as _SandboxManager


logger = logging.getLogger(__name__)


class _BuiltinWebFetcherAdapter:
    """WebFetcher shim that reuses BuiltinToolExecutor's _web_fetch impl."""

    def __init__(
        self,
        workspace_root,
        timeout_seconds: float,
        max_bytes: int,
        allow_private_urls: bool,
    ) -> None:
        from app.infrastructure.tools.builtin import BuiltinToolExecutor

        self._executor = BuiltinToolExecutor(
            workspace_root,
            web_fetch_timeout_seconds=timeout_seconds,
            web_fetch_max_bytes=max_bytes,
            web_fetch_allow_private_urls=allow_private_urls,
        )

    async def fetch(self, url: str) -> dict:
        try:
            return self._executor._web_fetch(url, "text")
        except PermissionError as exc:
            return {"status": "error", "error": str(exc)}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}


def _provider_factory(cfg: ProviderConfig, api_key: str):
    if cfg.provider_type == "openai-compatible":
        return OpenAICompatibleProvider(cfg.base_url, api_key, cfg.model)
    if cfg.provider_type == "anthropic":
        return AnthropicProvider(cfg.base_url, api_key, cfg.model, cfg.extra_headers)
    raise RuntimeError(f"unsupported provider_type: {cfg.provider_type}")


def _run_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    result = {}

    def runner():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _mask_app_id(app_id: str) -> str:
    return f"{app_id[:4]}****" if len(app_id) > 4 else "****"


async def _seed_legacy_knowledge_base(service: KnowledgeService, settings: Settings) -> None:
    existing = await service.list_bases()
    if existing or not (settings.kb_enabled and settings.kb_base_url.strip()):
        return
    await service.create_base(
        KnowledgeBaseCreateInput(
            id="legacy-n-kb",
            name="Legacy N-KB",
            description="Legacy N-KB seeded from N_AGENT_KB_* settings.",
            base_type=KnowledgeBaseType.N_KB,
            base_url=settings.kb_base_url,
            dataset_id="",
            enabled=True,
            default_top_k=settings.kb_default_top_k,
            default_min_score=settings.kb_default_min_score,
        )
    )


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
    skill_service: SkillService
    plugin_service: PluginService
    knowledge_service: KnowledgeService
    external_memory_service: ExternalMemoryService | None
    external_memory_provider_service: ExternalMemoryProviderService | None
    feishu_im_adapter: FeishuImAdapter | None
    platform_registry: PlatformRegistry
    platform_service: PlatformService
    health_snapshot: Callable[[], dict]
    sandbox_dashboard_service: "SandboxDashboardService | None" = None
    sandbox_manager: "_SandboxManager | None" = None


def build_application_services(settings: Settings | None = None) -> ApplicationServices:
    settings = settings or Settings()
    memory_store = SQLiteMemoryStore(settings.sqlite_path)
    summarizer = HeuristicSummarizer()
    registry = SQLiteProviderRegistry(settings.sqlite_path)
    gateway_registry = SQLiteGatewaySessionRegistry(settings.sqlite_path)
    holder = ActiveProviderHolder(_provider_factory)
    _run_sync(_seed_and_activate(registry, holder, settings))
    provider_service = ProviderService(registry, holder)
    builtin_executor = build_builtin_tool_executor(
        settings.workspace_root,
        web_fetch_timeout_seconds=settings.web_fetch_timeout_seconds,
        web_fetch_max_bytes=settings.web_fetch_max_bytes,
        web_fetch_allow_private_urls=settings.web_fetch_allow_private_urls,
    )
    knowledge_registry = SQLiteKnowledgeBaseRegistry(settings.sqlite_path)
    knowledge_factory = KnowledgeHttpRetrieverFactory(
        HttpKnowledgeRetrieverConfig(timeout_seconds=settings.kb_timeout_seconds)
    )
    knowledge_service = KnowledgeService(knowledge_registry, knowledge_factory)
    _run_sync(_seed_legacy_knowledge_base(knowledge_service, settings))
    kb_executor = KnowledgeToolExecutor(knowledge_service)
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
    knowledge_definition = _run_sync(knowledge_service.knowledge_tool_definition())
    tool_definitions = (
        builtin_tool_definitions(settings.web_fetch_enabled)
        + [knowledge_definition]
        + mcp_management_tool_definitions()
        + schedule_tool_definitions()
    )
    tool_service = ToolService(CompositeToolExecutor(routes), tool_definitions)
    mcp_service = McpService(mcp_registry, mcp_client, tool_service)
    mcp_management_executor = McpManagementToolExecutor(mcp_service)
    for definition in mcp_management_tool_definitions():
        routes[definition.name] = mcp_management_executor

    skill_registry = SQLiteSkillRegistry(settings.sqlite_path)
    skill_loader = SkillFileLoader(SkillFileLoaderConfig(
        root=settings.skills_root,
        current_platform=sys.platform,
        inline_shell_enabled=settings.skills_inline_shell_enabled,
        inline_shell_timeout=settings.skills_inline_shell_timeout,
        max_view_bytes=settings.skills_max_view_bytes,
        max_count=settings.skills_max_count,
    ))
    seed_default_skills(settings.skills_root)
    skill_service = SkillService(skill_registry, skill_loader)
    skill_executor = SkillToolExecutor(skill_service)
    for definition in skill_tool_definitions():
        routes[definition.name] = skill_executor
    tool_service.set_dynamic_definitions("skill", skill_tool_definitions())

    # Plugin 装配
    seed_default_plugins(settings.plugins_root)
    plugin_registry = SQLitePluginRegistry(settings.sqlite_path)
    plugin_loader = PluginFileLoader(PluginFileLoaderConfig(
        bundled_root=Path(__file__).resolve().parent / "infrastructure" / "plugin" / "seeds",
        user_root=settings.plugins_root,
        project_root=settings.workspace_root / ".hermes" / "plugins",
        enable_entrypoints=settings.enable_plugin_entrypoints,
        enable_project=settings.enable_project_plugins,
        safe_mode=settings.plugins_safe_mode,
    ))
    plugin_tool_executor_holder: dict[str, PluginToolExecutor | None] = {"executor": None}

    def _is_plugin_tool_name(name: str) -> bool:
        executor = plugin_tool_executor_holder["executor"]
        return executor is not None and routes.get(name) is executor

    def plugin_route_refresher(tool_names: set[str]) -> None:
        executor = plugin_tool_executor_holder["executor"]
        if executor is None:
            return
        for name in list(routes.keys()):
            if routes.get(name) is executor and name not in tool_names:
                del routes[name]
        for name in tool_names:
            routes[name] = executor

    plugin_service = PluginService(
        registry=plugin_registry,
        loader=plugin_loader,
        tool_service=tool_service,
        route_refresher=plugin_route_refresher,
        settings=settings,
    )
    plugin_tool_executor = PluginToolExecutor(service=plugin_service)
    plugin_tool_executor_holder["executor"] = plugin_tool_executor
    try:
        _run_sync(plugin_service.scan())
        logger.info("plugin startup scan ok")
    except Exception:
        logger.exception("plugin startup scan failed; dashboard refresh available as fallback")

    # External memory setup
    external_memory_manager = ExternalMemoryManager()
    # Keep original builtin for backward compatibility
    builtin_project_memory = BuiltinProjectMemory(
        project_root=settings.workspace_root,
        memory_path=settings.external_memory_path,
        memory_char_limit=settings.external_memory_memory_limit,
        user_char_limit=settings.external_memory_user_limit,
    )
    builtin_project_memory.initialize(session_id="", project_root=str(settings.workspace_root))
    external_memory_manager.add_provider(builtin_project_memory)
    # Multi-external-memory supports multiple independent external memory sets
    from app.infrastructure.memory.multi_project import MultiProjectMemory
    multi_project_memory = MultiProjectMemory(
        project_root=settings.workspace_root,
        memory_base_path=settings.external_memory_path,
        memory_char_limit=settings.external_memory_memory_limit,
        user_char_limit=settings.external_memory_user_limit,
    )
    multi_project_memory.initialize(session_id="")
    external_memory_manager.add_provider(multi_project_memory)

    tool_service.set_dynamic_definitions(
        "external_memory",
        external_memory_manager.get_tool_definitions(),
    )
    memory_executor = ExternalMemoryToolExecutor(external_memory_manager)
    for tool_def in external_memory_manager.get_tool_definitions():
        routes[tool_def.name] = memory_executor

    # External memory global config
    external_memory_config: ExternalMemoryConfigRegistry = SQLiteExternalMemoryConfig(settings.sqlite_path)
    external_memory_config.create_tables()
    external_memory_base_dir = settings.workspace_root / settings.external_memory_path
    external_memory_service = ExternalMemoryService(
        external_memory_manager=external_memory_manager,
        config_registry=external_memory_config,
        settings_default=settings.external_memory_enabled_providers,
        base_dir=external_memory_base_dir,
    )

    # External memory provider registry (mem0/holographic/honcho) -- minimal wiring.
    # Full tool-surface callback registration and startup active-provider load happen in T12.
    from app.infrastructure.registry.sqlite_external_memory_provider_registry import (
        SQLiteExternalMemoryProviderRegistry,
    )
    from app.infrastructure.memory.external.http_client import ExternalMemoryHttpClient
    from app.infrastructure.memory.external.mem0 import Mem0Adapter
    from app.infrastructure.memory.external.holographic import HolographicAdapter
    from app.infrastructure.memory.external.honcho import HonchoAdapter

    external_provider_registry = SQLiteExternalMemoryProviderRegistry(settings.sqlite_path)
    external_provider_registry.create_tables()

    external_http_client = ExternalMemoryHttpClient()
    external_factories = {
        ExternalMemoryProviderType.MEM0: lambda cfg, secret: Mem0Adapter.factory(
            http_client=external_http_client, config=cfg, secret=secret),
        ExternalMemoryProviderType.HOLOGRAPHIC: lambda cfg, secret: HolographicAdapter.factory(
            config=cfg, secret=secret),
        ExternalMemoryProviderType.HONCHO: lambda cfg, secret: HonchoAdapter.factory(
            http_client=external_http_client, config=cfg, secret=secret),
    }
    external_memory_provider_service = ExternalMemoryProviderService(
        registry=external_provider_registry,
        manager=external_memory_manager,
        factories=external_factories,
        workspace_root=settings.workspace_root,
    )

    # 延迟绑定检索记忆 provider catalog：list_providers 据此合并 inactive 条目，
    # 供历史会话忠实展示当时选择的检索记忆 Provider（即使现已非 active）。
    external_memory_service.set_external_query_catalog(
        external_memory_provider_service.list
    )

    # 注册工具面回调：swap 后刷新 ToolService + Composite routes
    def _refresh_external_memory_tools():
        tool_defs = external_memory_manager.get_tool_definitions()
        tool_service.set_dynamic_definitions("external_memory", tool_defs)
        memory_executor_local = ExternalMemoryToolExecutor(external_memory_manager)
        # 先移除不再存在的 external memory 工具路由（避免 stale 路由堆积）
        current_names = {d.name for d in tool_defs}
        stale = [n for n in list(routes) if n not in current_names and _is_external_memory_tool_name(n)]
        for name in stale:
            routes.pop(name, None)
        for tool_def in tool_defs:
            routes[tool_def.name] = memory_executor_local

    def _is_external_memory_tool_name(name: str) -> bool:
        """识别 external memory 域的工具名（builtin/multi-project/external-query 三类 slot）。"""
        return name in {
            "external_memory", "multi_external_memory",
            "mem0_profile", "mem0_search", "mem0_conclude",
            "fact_store", "fact_feedback",
            "honcho_profile", "honcho_search", "honcho_reasoning",
            "honcho_context", "honcho_conclude",
        }

    external_memory_manager.register_tool_surface_callback(_refresh_external_memory_tools)

    # 启动时装载 active external-query provider（至多一个）
    for cfg in external_provider_registry.list_providers():
        if cfg.enabled:
            secret = external_provider_registry.get_secret(cfg.id)
            factory = external_factories[cfg.provider_type]
            adapter = factory(dict(cfg.extra_config), secret.api_key if secret else None)
            adapter.initialize(session_id="", project_root=str(settings.workspace_root))
            external_memory_manager.swap_external_query_provider(adapter)
            break

    tool_service.executor = CompositeToolExecutor(routes, fallback=McpToolExecutor(mcp_service))
    try:
        _run_sync(mcp_service.refresh_registered_tool_surface())
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
        external_memory_manager=external_memory_manager,
    )
    session_service = SessionService(
        memory_store,
        title_generator=LLMTitleGenerator(holder, lambda: holder.current_model),
        external_memory_manager=external_memory_manager,
    )
    chat_service = ChatCompletionService(
        memory_store,
        graph_runner,
        session_service,
        external_memory_reader=external_memory_provider_service,
        slot_resolver=external_memory_manager.resolve_provider_slot,
    )
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
        ScheduleOutboundDelivery(feishu_client, gateway_registry.get_home_target),
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
    schedule_run_service.recover_missing_origin_sessions = schedule_service.recover_missing_origin_sessions
    scheduler_runner = SchedulerRunner(schedule_run_service, settings.scheduler_tick_seconds)

    schedule_management_executor = ScheduleManagementToolExecutor(schedule_service)
    routes["manage_schedule"] = schedule_management_executor
    routes["schedule_query"] = schedule_management_executor
    tool_service.executor = CompositeToolExecutor(routes, fallback=McpToolExecutor(mcp_service))

    # Sandbox assembly (T24)
    sandbox_callback_registry = InMemorySandboxCallbackToolRegistry()
    sandbox_search_provider = DuckDuckGoHtmlSearchProvider()
    sandbox_web_fetcher = _BuiltinWebFetcherAdapter(
        settings.workspace_root,
        timeout_seconds=settings.web_fetch_timeout_seconds,
        max_bytes=settings.web_fetch_max_bytes,
        allow_private_urls=settings.web_fetch_allow_private_urls,
    )
    sandbox_callback_tool_instances = {
        "read_file": ReadFileTool(name="read_file"),
        "write_file": WriteFileTool(name="write_file"),
        "search_files": SearchFilesTool(name="search_files"),
        "patch": PatchTool(name="patch"),
        "web_extract": WebExtractTool(fetcher=sandbox_web_fetcher),
        "web_search": WebSearchTool(provider=sandbox_search_provider),
    }
    desired_callback_tools = set(settings.sandbox_callback_tools or [])
    for name, tool in sandbox_callback_tool_instances.items():
        if name not in desired_callback_tools:
            continue
        if name == "web_extract" and not settings.web_fetch_enabled:
            continue
        if name == "web_search" and not (
            settings.sandbox_web_search_enabled and sandbox_search_provider.is_available()
        ):
            continue
        tool.enabled = True
        sandbox_callback_registry.register(tool)

    sandbox_released_registry = SQLiteReleasedSandboxRegistry(settings.sqlite_path)
    sandbox_history_registry = SQLiteSandboxExecutionHistoryRegistry(settings.sqlite_path)
    sandbox_scratch_root = (
        settings.sandbox_scratch_root
        or (settings.workspace_root.parent / "locals" / "sandbox-scratch")
    )
    # host_scratch_root: 容器化部署时宿主可见的 scratch 路径。
    # 若用户配置了 sandbox_docker_host_locals_root，scratch 挂在其下 sandbox-scratch 子目录；
    # 否则默认与 scratch_root 相同（n-agent 直接跑宿主机时成立）。
    if settings.sandbox_docker_host_locals_root is not None:
        sandbox_host_scratch_root = settings.sandbox_docker_host_locals_root / "sandbox-scratch"
    else:
        sandbox_host_scratch_root = sandbox_scratch_root
    sandbox_manager = SandboxManager(
        sandbox_type=settings.sandbox_type,
        workspace_root=settings.workspace_root,
        idle_seconds=settings.sandbox_idle_seconds,
        settings=settings,
        callback_registry=sandbox_callback_registry,
        scratch_root=sandbox_scratch_root,
        release_wait_timeout_seconds=settings.sandbox_release_wait_timeout_seconds,
        host_workspace_root=settings.sandbox_docker_host_workspace_root or settings.workspace_root,
        host_scratch_root=sandbox_host_scratch_root,
        released_registry=sandbox_released_registry,
    )
    sandbox_tool_executor = SandboxToolExecutor(
        sandbox_manager=sandbox_manager,
        callback_registry=sandbox_callback_registry,
        settings=settings,
        history_registry=sandbox_history_registry,
        summary_max_stdout=settings.sandbox_summary_max_stdout_bytes,
        summary_max_stderr=settings.sandbox_summary_max_stderr_bytes,
    )
    sandbox_dashboard_service = SandboxDashboardService(
        sandbox_manager=sandbox_manager,
        memory_store=memory_store,
        settings=settings,
        history_registry=sandbox_history_registry,
    )

    # Register execute_code tool when sandbox enabled
    sandbox_docker_available = False
    if settings.sandbox_enabled:
        if settings.sandbox_type == "local":
            logger.warning(
                "sandbox_type=local is trusted-dev only, not a security sandbox; "
                "use docker for production"
            )
        elif settings.sandbox_type == "docker":
            docker_path = shutil.which("docker")
            if not docker_path:
                logger.warning(
                    "sandbox_type=docker but docker CLI not found on PATH; "
                    "sandbox will report docker_unavailable"
                )
            else:
                import subprocess as _sb
                try:
                    proc = _sb.run(
                        ["docker", "info"],
                        capture_output=True,
                        timeout=10,
                    )
                    if proc.returncode == 0:
                        sandbox_docker_available = True
                    else:
                        logger.warning(
                            "sandbox_type=docker but `docker info` exited %d; "
                            "sandbox will report docker_unavailable "
                            "(is docker.sock mounted?)",
                            proc.returncode,
                        )
                except Exception as exc:
                    logger.warning(
                        "sandbox_type=docker but `docker info` failed: %s; "
                        "sandbox will report docker_unavailable",
                        exc,
                    )

    if settings.sandbox_enabled:
        enabled_tool_names = sorted(
            t.name for t in sandbox_callback_registry.list_enabled()
        )
        execute_code_description = (
            "Execute Python code in a sandboxed Python 3.11 environment. "
            "Sandbox constraints: NO network access (socket/urllib/requests will fail), "
            "workspace mounted read-only, write only to cwd (scratch). "
            "To access external resources, use callback tools via bare function calls "
            f"(imported automatically): {', '.join(enabled_tool_names) or 'none'}. "
            "Example: web_extract(url='https://...') or web_search(query='...')."
        )
        execute_code_definition = ToolDefinition(
            name="execute_code",
            description=execute_code_description,
            input_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "enabled_tools": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["code"],
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="sandbox",
            managed=False,
            enabled=True,
        )
        tool_service.set_dynamic_definitions("sandbox", [execute_code_definition])
        routes["execute_code"] = sandbox_tool_executor
        tool_service.executor = CompositeToolExecutor(routes, fallback=McpToolExecutor(mcp_service))

    def health_snapshot() -> dict:
        memory_status = "ok"
        try:
            settings.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            settings.sqlite_path.touch(exist_ok=True)
        except Exception as exc:
            memory_status = f"error: {exc}"
        active = holder.current_config
        provider_configured = active is not None and active.api_key_present
        knowledge_bases = _run_sync(knowledge_service.list_bases())
        knowledge_enabled_count = sum(1 for base in knowledge_bases if base.enabled)
        return {
            "provider": {
                "status": "ok" if provider_configured else "warn",
                "base_url": active.base_url if active else "",
                "model": active.model if active else "",
            },
            "memory": {"status": memory_status, "path": str(settings.sqlite_path)},
            "knowledge": {
                "status": "ok" if knowledge_enabled_count else "disabled",
                "enabled_count": knowledge_enabled_count,
                "total_count": len(knowledge_bases),
            },
            "mcp": {"status": mcp_status, "error": mcp_error},
            "gateway": {"status": "ok" if settings.gateway_enabled else "disabled"},
            "scheduler": {
                "status": "ok" if settings.scheduler_enabled else "disabled",
                "tick_seconds": settings.scheduler_tick_seconds,
                "timezone": settings.scheduler_timezone,
            },
            "sandbox": {
                "status": (
                    "disabled" if not settings.sandbox_enabled else (
                        "ok" if settings.sandbox_type == "docker" and sandbox_docker_available else
                        "warn" if settings.sandbox_type == "local" else
                        "docker_unavailable"
                    )
                ),
                "type": settings.sandbox_type,
                "docker_available": sandbox_docker_available if settings.sandbox_type == "docker" else None,
                "enabled": settings.sandbox_enabled,
                "idle_seconds": settings.sandbox_idle_seconds,
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
    feishu_im_adapter = (
        FeishuImAdapter(gateway_service, feishu_client) if feishu_client is not None else None
    )
    descriptors = []
    lifecycles = {}
    if settings.feishu_enabled:
        descriptors.append(
            PlatformDescriptor(
                Platform.FEISHU,
                "飞书",
                PlatformKind.IM,
                {
                    "app_id_suffix": _mask_app_id(settings.feishu_app_id),
                    "tenant_key": settings.feishu_tenant_key,
                    "allowed_open_id_count": len(settings.feishu_allowed_open_ids),
                    "allowed_chat_id_count": len(settings.feishu_allowed_chat_ids),
                },
            )
        )
        if feishu_im_adapter is not None:
            lifecycles[Platform.FEISHU] = feishu_im_adapter
    platform_registry = InMemoryPlatformRegistry(descriptors, lifecycles)
    platform_service = PlatformService(platform_registry, gateway_registry)
    session_service.add_session_deleted_handler(schedule_service.handle_session_deleted)
    if settings.sandbox_enabled:
        async def _release_sandbox_on_session_deleted(session_id: str) -> None:
            await sandbox_manager.release(session_id, reason="session")
        session_service.add_session_deleted_handler(_release_sandbox_on_session_deleted)
    memory_store.migrate_session_id_prefixes()
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
        skill_service=skill_service,
        plugin_service=plugin_service,
        knowledge_service=knowledge_service,
        external_memory_service=external_memory_service,
        external_memory_provider_service=external_memory_provider_service,
        feishu_im_adapter=feishu_im_adapter,
        platform_registry=platform_registry,
        platform_service=platform_service,
        health_snapshot=health_snapshot,
        sandbox_dashboard_service=sandbox_dashboard_service if settings.sandbox_enabled else None,
        sandbox_manager=sandbox_manager if settings.sandbox_enabled else None,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    services = build_application_services(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        feishu_task: asyncio.Task | None = None
        scheduler_task: asyncio.Task | None = None
        try:
            report = await services.skill_service.scan_now()
            logger.info(
                "skill startup scan ok skills=%s warnings=%s",
                report.skills_count,
                len(report.warnings),
            )
        except Exception:
            logger.exception("skill startup scan failed; dashboard refresh available as fallback")
        try:
            await services.plugin_service.scan()
            logger.info("plugin lifespan scan ok")
        except Exception:
            logger.exception("plugin lifespan scan failed; dashboard refresh available as fallback")
        if services.settings.scheduler_enabled:
            scheduler_task = asyncio.create_task(services.scheduler_runner.run())
        if services.feishu_im_adapter is not None:
            feishu_task = asyncio.create_task(services.feishu_im_adapter.start())
        if services.sandbox_manager is not None:
            try:
                await services.sandbox_manager.cleanup_orphan_containers()
            except Exception:
                logger.exception("sandbox orphan cleanup failed at startup")
            services.sandbox_manager.start_reaper()
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
            if services.sandbox_manager is not None:
                try:
                    await services.sandbox_manager.stop_reaper()
                except Exception:
                    logger.exception("sandbox reaper stop failed")
            if services.external_memory_service is not None:
                try:
                    services.external_memory_service.shutdown()
                except Exception:
                    logger.exception("external memory shutdown failed")

    app = FastAPI(title="N-Agent", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_openai_compatible_router(services.chat_service, services.model_service))
    app.include_router(create_platforms_router(services.platform_service))
    app.include_router(
        create_dashboard_router(
            services.session_service,
            services.tool_service,
            services.model_service,
            services.health_snapshot,
            provider_service=services.provider_service,
            mcp_service=services.mcp_service,
            schedule_service=services.schedule_service,
            skill_service=services.skill_service,
            plugin_service=services.plugin_service,
            knowledge_service=services.knowledge_service,
            external_memory_service=services.external_memory_service,
            external_memory_provider_service=services.external_memory_provider_service,
            sandbox_dashboard_service=services.sandbox_dashboard_service,
        )
    )
    return app
