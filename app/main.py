from __future__ import annotations

import asyncio
import logging
import shutil
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.application.agent_graph import AgentGraphRunner
from app.application.budget_service import BudgetService
from app.application.browser_service import BrowserService, BrowserServiceSettings
from app.application.browser_confirmation_service import BrowserConfirmationService
from app.application.browser_dashboard_service import BrowserDashboardService
from app.application.browser_tool_executor import (
    BrowserToolExecutor,
    browser_tool_definitions,
)
from app.application.chat_service import ChatCompletionService
from app.application.gateway_service import GatewayService
from app.application.gateway_tool_approval_service import GatewayToolApprovalService
from app.application.host_terminal_tool_executor import (
    HostTerminalToolExecutor,
    host_terminal_tool_definition,
)
from app.application.runtime_memory_service import RuntimeMemoryService
from app.application.information_flow_service import InformationFlowService
from app.application.knowledge_service import KnowledgeBaseCreateInput, KnowledgeService, KnowledgeToolExecutor
from app.application.mcp_service import McpManagementToolExecutor, McpService, McpToolExecutor, mcp_management_tool_definitions
from app.application.model_service import ModelService
from app.application.platform_service import PlatformService
from app.application.policy_snapshot import (
    BudgetPolicyConfig,
    RunPolicySnapshotFactory,
    SettingsPolicyProfileProvider,
)
from app.application.policy_audit_service import PolicyAuditService
from app.application.policy_dashboard_service import PolicyDashboardService
from app.application.task_security_dashboard_service import TaskSecurityDashboardService
from app.application.task_config_service import TaskConfigService
from app.application.provider_service import ProviderCreateInput, ProviderService
from app.application.runtime_provider import ActiveProviderHolder
from app.application.schedule_run_service import ScheduleRunService
from app.application.schedule_service import ScheduleService
from app.application.scheduled_agent_executor import ScheduledAgentExecutor
from app.application.scheduler_runner import SchedulerRunner
from app.application.session_service import SessionService
from app.application.session_bootstrap import SessionBootstrapReader
from app.application.skill_service import SkillManageToolExecutor, SkillService, SkillToolExecutor, skill_tool_definitions
from app.application.skill_evolution_service import SkillEvolutionService
from app.application.plugin_service import PluginCliCommand, PluginService, PluginToolExecutor
from app.application.prompt_builder import BROWSER_GUIDANCE, build_system_prompt
from app.application.task_agent_executor import TaskAgentExecutor
from app.application.task_run_service import TaskRunService
from app.application.task_runner import TaskRunner
from app.application.task_service import TaskService
from app.application.task_tools import (
    task_tool_definitions,
    user_task_approval_tool_definitions,
    user_task_tool_definitions,
)
from app.application.tool_service import ToolService, builtin_tool_definitions, schedule_tool_definitions
from app.application.usage_service import UsageService
from app.application.vision_tool_executor import VisionAnalyzeToolExecutor
from app.config import Settings
from app.domain.knowledge import KnowledgeBaseType
from app.domain.llm_policy import LLMConfig, LLMPolicy
from app.domain.platform import Platform, PlatformDescriptor, PlatformKind, PlatformRegistry
from app.domain.provider import ProviderConfig
from app.domain.session import SessionNotFoundError
from app.domain.skill_format import SkillFormatValidator
from app.domain.skill_policy import SkillPolicy
from app.infrastructure.feishu.client import FeishuClient, FeishuConfig
from app.infrastructure.host_terminal.http_client import (
    HostTerminalHttpClient,
    HostTerminalHttpClientConfig,
)
from app.infrastructure.host_terminal.policy_loader import HostTerminalPolicyLoader
from app.infrastructure.image_store import LocalImageStore
from app.infrastructure.knowledge.http_adapters import HttpKnowledgeRetrieverConfig, KnowledgeHttpRetrieverFactory
from app.infrastructure.llm.anthropic_provider import AnthropicProvider
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.context.context_compressor import ContextCompressor
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
from app.infrastructure.skill.skill_usage_store import SkillUsageStore
from app.infrastructure.skill.skill_pending_store import SkillPendingStore
from app.infrastructure.skill.skill_backup_store import SkillBackupStore
from app.infrastructure.skill.curator_state_store import SqliteCuratorStateStore
from app.domain.curator_policy import CuratorPolicy
from app.application.skill_curator_service import SkillCuratorService
from app.domain.task_policy import TaskPolicy
from app.infrastructure.plugin.file_loader import PluginFileLoader, PluginFileLoaderConfig
from app.infrastructure.plugin.seed_runner import seed_default_plugins
from app.infrastructure.registry.sqlite_plugin_registry import SQLitePluginRegistry
from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry
from app.infrastructure.registry.sqlite_task_config_store import SqliteTaskConfigStore
from app.infrastructure.policy.task_config_logging_sink import TaskConfigLoggingSink
from app.infrastructure.tools.builtin import BUILTIN_TOOL_NAMES, build_builtin_tool_executor
from app.infrastructure.tools.composite import CompositeToolExecutor
from app.infrastructure.tools.schedule_management import ScheduleManagementToolExecutor
from app.infrastructure.tools.task_management import TaskManagementToolExecutor
from app.infrastructure.tools.user_task_management import UserTaskToolExecutor
from app.infrastructure.task.outbound import TaskOutboundDelivery
from app.infrastructure.usage.context_breakdown_calculator import ContextBreakdownCalculatorImpl
from app.infrastructure.usage.pricing_table import InMemoryPricingProvider
from app.infrastructure.usage.sqlite_usage_recorder import SqliteUsageRecorder
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
from app.interfaces.http.dashboard_tool_approval import DashboardToolApprovalBridge
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
from app.infrastructure.policy.logging_sink import LoggingPolicyAuditSink
from app.application.host_terminal_dashboard_service import HostTerminalDashboardService
from app.application.sandbox_dashboard_service import SandboxDashboardService
from app.application.sandbox_tool_executor import SandboxToolExecutor
from app.application.terminal_tool_executor import TerminalToolExecutor
from app.domain.sandbox_policy import SandboxDomainConfig, SandboxPolicy
from app.domain.tool import RiskLevel, ToolDefinition, ToolSourceType
from app.domain.browser import BrowserBackendType
from app.infrastructure.browser.container_backend import ContainerBrowserBackend
from app.infrastructure.browser.host_cdp_backend import (
    HostCdpBackendConfig,
    HostCdpBrowserBackend,
)
from app.infrastructure.browser.novnc_proxy import BrowserNoVncProxy
from app.infrastructure.browser import host_protocol
from app.infrastructure.browser.screenshot_store import SqliteBrowserScreenshotStore
from app.infrastructure.browser.sqlite_browser_registry import SqliteBrowserSessionRegistry
from app.infrastructure.browser.url_safety import UrlVerifier
from app.domain.browser_policy import BrowserPolicy

if TYPE_CHECKING:
    from app.infrastructure.sandbox.manager import SandboxManager as _SandboxManager


def _configure_logging() -> None:
    """Configure root logger so application INFO logs are visible.

    uvicorn 0.30+ 默认 LOGGING_CONFIG 不再为 root logger 配置 handler/level，
    导致应用层 logger.info() 因 root logger 无 handler 且 effective level 回落
    到 WARNING 而完全静默（仅 uvicorn.access logger 单独配置仍可见）。这里在
    FastAPI 工厂启动时显式配置 root logger，保证 HTTP 服务的应用日志可见。
    CLI 只复用本模块的服务装配，不应因此开启 INFO 日志。basicConfig 幂等：
    root logger 已有 handler 时（如 pytest 已配置）不覆盖。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


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
    skill_usage_store: SkillUsageStore
    skill_pending_store: SkillPendingStore
    skill_backup_store: SkillBackupStore
    skill_evolution_service: SkillEvolutionService
    skill_curator_service: SkillCuratorService
    curator_state_store: SqliteCuratorStateStore
    plugin_service: PluginService
    knowledge_service: KnowledgeService
    external_memory_service: ExternalMemoryService | None
    external_memory_provider_service: ExternalMemoryProviderService | None
    feishu_im_adapter: FeishuImAdapter | None
    platform_registry: PlatformRegistry
    platform_service: PlatformService
    health_snapshot: Callable[[], dict]
    policy_dashboard_service: PolicyDashboardService
    task_security_dashboard_service: TaskSecurityDashboardService
    task_config_service: TaskConfigService
    tool_approval_service: GatewayToolApprovalService
    image_store: LocalImageStore
    usage_service: UsageService | None = None
    sandbox_dashboard_service: "SandboxDashboardService | None" = None
    sandbox_manager: "_SandboxManager | None" = None
    host_terminal_dashboard_service: "HostTerminalDashboardService | None" = None
    # Task 子域服务 (T18). schema 迁移失败时 task_registry 为 None，其余服务
    # 也为 None；lifespan 不启动 dispatcher，health 报不健康。
    task_service: TaskService | None = None
    task_run_service: TaskRunService | None = None
    task_runner: TaskRunner | None = None
    # Browser 子域服务 (T10). None when browser_enabled=False.
    browser_service: BrowserService | None = None
    browser_dashboard_service: "BrowserDashboardService | None" = None
    browser_confirmation_service: "BrowserConfirmationService | None" = None
    browser_novnc_proxy: "BrowserNoVncProxy | None" = None


def _validate_host_terminal_host_mapping(
    settings: Settings, authority_paths: tuple[Path, Path]
) -> None:
    """Validate host path descriptors without touching the host filesystem."""
    host_workspace = settings.host_terminal_host_workspace_root
    host_skills = settings.host_terminal_host_skills_root
    if host_workspace is None or host_skills is None:
        raise ValueError("host_terminal_host_mapping_invalid")
    normalized_workspace = Path(str(host_workspace))
    normalized_skills = Path(str(host_skills))
    if (
        not normalized_workspace.is_absolute()
        or not normalized_skills.is_absolute()
        or normalized_workspace != Path(*normalized_workspace.parts)
        or normalized_skills != Path(*normalized_skills.parts)
        or ".." in normalized_workspace.parts
        or ".." in normalized_skills.parts
    ):
        raise ValueError("host_terminal_host_mapping_invalid")
    try:
        relative_skills = settings.skills_root.relative_to(settings.workspace_root)
    except ValueError as exc:
        raise ValueError("host_terminal_host_mapping_invalid") from exc
    if (
        relative_skills == Path(".")
        or host_workspace / relative_skills != host_skills
    ):
        raise ValueError("host_terminal_host_mapping_invalid")
    if any(
        descriptor == authority
        or descriptor.is_relative_to(authority)
        or authority.is_relative_to(descriptor)
        for descriptor in (host_workspace, host_skills)
        for authority in authority_paths
    ):
        raise ValueError("host_terminal_host_mapping_invalid")


def build_application_services(settings: Settings | None = None) -> ApplicationServices:
    settings = settings or Settings()
    memory_store = SQLiteMemoryStore(
        settings.sqlite_path,
        migration_protect_first_n=settings.context_compression_protect_first_n,
        migration_protect_last_n=settings.context_compression_protect_last_n,
    )
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
    vision_executor = VisionAnalyzeToolExecutor(
        provider=holder,
        vision_capability=lambda: bool(holder.current_config and holder.current_config.supports_vision),
        current_model=lambda: holder.current_model,
    )
    routes["vision_analyze"] = vision_executor
    knowledge_definition = _run_sync(knowledge_service.knowledge_tool_definition())
    tool_definitions = (
        builtin_tool_definitions(settings.web_fetch_enabled)
        + [knowledge_definition]
        + mcp_management_tool_definitions()
        + schedule_tool_definitions()
        + task_tool_definitions()
    )
    # Policy audit sink + service (T12 S4: wired into all Policy-bearing services)
    audit_sink = LoggingPolicyAuditSink()
    audit_service = PolicyAuditService(audit_sink)
    # Budget + InformationFlow services (created early so ToolService can enforce)
    information_flow_service = InformationFlowService.from_settings(settings, audit_service=audit_service)
    budget_service = BudgetService(
        BudgetPolicyConfig(
            max_wall_seconds=settings.budget_max_wall_seconds,
            max_llm_calls=settings.budget_max_llm_calls,
            max_tool_calls=settings.budget_max_tool_calls,
            max_token_cost=settings.budget_max_token_cost,
            max_usd_cost=settings.budget_max_usd_cost,
            max_sandbox_seconds=settings.budget_max_sandbox_seconds,
            max_sandbox_cpu_seconds=settings.budget_max_sandbox_cpu_seconds,
            max_sandbox_memory_mb_seconds=settings.budget_max_sandbox_memory_mb_seconds,
            max_sandbox_callback_calls=settings.budget_max_sandbox_callback_calls,
        ),
        audit_service=audit_service,
    )
    tool_service = ToolService(
        CompositeToolExecutor(routes),
        tool_definitions,
        budget_service=budget_service,
        information_flow_service=information_flow_service,
        audit_service=audit_service,
    )
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
    # Skill 自进化 stores + policy (T5/T6/T7/T2)
    skill_usage_store = SkillUsageStore(settings.sqlite_path)
    skill_pending_store = SkillPendingStore(settings.sqlite_path)
    skill_backup_store = SkillBackupStore(
        root=settings.skills_root,
        keep=settings.skills_backup_keep,
    )
    skill_policy = SkillPolicy()
    skill_service = SkillService(
        skill_registry,
        skill_loader,
        usage=skill_usage_store,
        pending=skill_pending_store,
        backup=skill_backup_store,
        policy=skill_policy,
        write_approval=settings.skills_write_approval,
        guard_agent_created=settings.skills_guard_agent_created,
        backup_enabled=settings.skills_backup_enabled,
        format_validator=SkillFormatValidator(),
    )
    skill_executor = SkillToolExecutor(skill_service)
    for definition in skill_tool_definitions():
        routes[definition.name] = skill_executor
    tool_service.set_dynamic_definitions("skill", skill_tool_definitions())
    # Register skill_manage tool (T10: SkillManageToolExecutor)
    skill_manage_executor = SkillManageToolExecutor(skill_service)
    routes["skill_manage"] = skill_manage_executor

    # Restricted host-terminal assembly. Configuration/Policy failures are
    # fail-closed and only publish a stable, non-sensitive health reason.
    image_store = LocalImageStore(settings.image_store_dir, settings.dashboard_base_url)
    host_terminal_executor: HostTerminalToolExecutor | None = None
    host_terminal_health_reason = (
        "host_terminal_disabled"
        if not settings.host_terminal_enabled
        else "host_terminal_config_incomplete"
    )
    host_policy_loader = None
    if settings.host_terminal_enabled:
        required_host_config = (
            bool(settings.host_terminal_bridge_url.strip()),
            settings.host_terminal_policy_path is not None,
            settings.host_terminal_token_path is not None,
            settings.host_terminal_host_workspace_root is not None,
            settings.host_terminal_host_skills_root is not None,
            bool(str(settings.skills_root)),
        )
        if all(required_host_config):
            try:
                authority_paths = (
                    settings.host_terminal_policy_path.resolve(),  # type: ignore[union-attr]
                    settings.host_terminal_token_path.resolve(),  # type: ignore[union-attr]
                )
                writable_roots = (
                    settings.workspace_root.resolve(),
                    settings.skills_root.resolve(),
                    *(
                        (settings.sandbox_scratch_root.resolve(),)
                        if settings.sandbox_scratch_root is not None
                        else ()
                    ),
                )
                if (
                    authority_paths[0] == authority_paths[1]
                    or any(
                        authority == root
                        or authority.is_relative_to(root)
                        or root.is_relative_to(authority)
                        for authority in authority_paths
                        for root in writable_roots
                    )
                ):
                    raise ValueError("host_terminal_authority_path_unsafe")
                _validate_host_terminal_host_mapping(settings, authority_paths)
                policy_loader = HostTerminalPolicyLoader(
                    settings.host_terminal_policy_path  # type: ignore[arg-type]
                )
                host_snapshot = policy_loader.load()
                if not host_snapshot.rules:
                    raise ValueError("host_policy_empty")
                host_client = HostTerminalHttpClient(
                    HostTerminalHttpClientConfig(
                        base_url=settings.host_terminal_bridge_url,
                        token_path=settings.host_terminal_token_path,
                        connect_timeout_seconds=settings.host_terminal_connect_timeout_seconds,
                        read_timeout_seconds=(
                            settings.host_terminal_bridge_timeout_seconds
                            + settings.host_terminal_transfer_margin_seconds
                        ),
                        max_response_bytes=settings.host_terminal_max_response_bytes,
                    )
                )
                host_terminal_executor = HostTerminalToolExecutor(
                    client=host_client,
                    skill_service=skill_service,
                    policy_loader=policy_loader,
                    tool_timeout_seconds=settings.host_terminal_tool_timeout_seconds,
                    bridge_timeout_seconds=settings.host_terminal_bridge_timeout_seconds,
                    max_stdout_bytes=settings.host_terminal_max_stdout_bytes,
                    max_stderr_bytes=settings.host_terminal_max_stderr_bytes,
                    max_concurrency=settings.host_terminal_max_concurrency,
                    audit_service=audit_service,
                    image_persister=image_store,
                )
                host_definition = host_terminal_tool_definition(
                    settings.host_terminal_tool_timeout_seconds
                )
                routes[host_definition.name] = host_terminal_executor
                tool_service.set_dynamic_definitions("host", [host_definition])
                # No startup connectivity dependency: the tool surface remains
                # stable and health moves to ok only after an actual success.
                host_terminal_health_reason = "host_bridge_not_checked"
                host_policy_loader = policy_loader
            except Exception as exc:
                host_terminal_health_reason = getattr(exc, "reason_code", None)
                if host_terminal_health_reason is None:
                    host_terminal_health_reason = getattr(
                        exc, "error_code", "host_terminal_configuration_invalid"
                    )
                if (
                    host_terminal_health_reason
                    == "host_terminal_configuration_invalid"
                    and str(exc)
                    in {
                        "host_terminal_authority_path_unsafe",
                        "host_terminal_host_mapping_invalid",
                    }
                ):
                    host_terminal_health_reason = str(exc)
                if not isinstance(host_terminal_health_reason, str):
                    host_terminal_health_reason = "host_terminal_configuration_invalid"
                logger.warning(
                    "host terminal not registered reason=%s",
                    host_terminal_health_reason,
                )

    host_terminal_dashboard_service = HostTerminalDashboardService(
        host_policy_loader,
        host_terminal_executor,
        memory_store,
        host_terminal_health_reason,
    )

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
    runtime_memory_service = RuntimeMemoryService(
        memory_store,
        audit_sink=audit_sink,
        external_memory_manager=external_memory_manager,
        cross_session_read_enabled=settings.memory_cross_session_read_enabled,
        unattended_write_enabled=settings.memory_unattended_write_enabled,
    )
    memory_executor = ExternalMemoryToolExecutor(
        external_memory_manager,
        runtime_memory_service=runtime_memory_service,
    )
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
        memory_executor_local = ExternalMemoryToolExecutor(
            external_memory_manager,
            runtime_memory_service=runtime_memory_service,
        )
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
    context_engine = None
    if settings.context_compression_enabled:
        context_engine = ContextCompressor(
            llm_provider=holder,
            model=lambda: holder.current_model,
            context_length=settings.context_length,
            threshold_percent=settings.context_compression_threshold,
            protect_first_n=settings.context_compression_protect_first_n,
            protect_last_n=settings.context_compression_protect_last_n,
            summary_target_ratio=settings.context_compression_target_ratio,
            tail_budget_enabled=settings.context_compression_tail_budget_enabled,
            cooldown_seconds=settings.context_compression_cooldown_seconds,
            fallback_summarizer=summarizer,
        )
    # Usage assembly (T7): recorder/pricing/breakdown -> UsageService -> runner.
    # SqliteUsageRecorder.init is async but performs synchronous sqlite3 DDL;
    # run via _run_sync so build_application_services stays callable from sync
    # contexts (CLI/tests). Sessions table already exists at this point because
    # SQLiteMemoryStore.initialize() runs in its __init__.
    usage_recorder = SqliteUsageRecorder(str(settings.sqlite_path))
    _run_sync(usage_recorder.init())
    usage_service = UsageService(
        usage_recorder,
        InMemoryPricingProvider(),
        ContextBreakdownCalculatorImpl(),
    )
    # information_flow_service and budget_service are created earlier (before ToolService)
    # so they can be wired into ToolService for封口 enforcement.
    llm_policy = LLMPolicy()
    llm_config = LLMConfig(fallback_enabled=settings.llm_fallback_enabled)
    graph_runner = AgentGraphRunner(
        holder,
        tool_service,
        memory_store,
        summarizer,
        iteration_limit=settings.agent_iteration_limit,
        turn_timeout_seconds=settings.agent_turn_timeout_seconds,
        external_memory_manager=external_memory_manager,
        vision_capability=lambda: bool(holder.current_config and holder.current_config.supports_vision),
        context_engine=context_engine,
        usage_service=usage_service,
        skill_service=skill_service,
        information_flow_service=information_flow_service,
        runtime_memory_service=runtime_memory_service,
        budget_service=budget_service,
        llm_policy=llm_policy,
        llm_config=llm_config,
        nudge_interval=settings.skills_creation_nudge_interval,
        hook_dispatcher=plugin_service,
        browser_guidance=BROWSER_GUIDANCE if settings.browser_enabled else None,
    )
    session_service = SessionService(
        memory_store,
        title_generator=LLMTitleGenerator(holder, lambda: holder.current_model),
        external_memory_manager=external_memory_manager,
        hook_dispatcher=plugin_service,
    )
    chat_service = ChatCompletionService(
        memory_store,
        graph_runner,
        session_service,
        external_memory_reader=external_memory_provider_service,
        slot_resolver=external_memory_manager.resolve_provider_slot,
        information_flow_service=information_flow_service,
        runtime_memory_service=runtime_memory_service,
        policy_snapshot_factory=RunPolicySnapshotFactory(
            SettingsPolicyProfileProvider(settings)
        ),
        session_bootstrap_reader=SessionBootstrapReader(memory_store),
    )
    # Skill 自进化 service (T11). Built after chat_service because it calls
    # chat.complete for background review; injected back into graph_runner to
    # break the runner -> evolution -> chat -> runner cycle.
    skill_evolution_service = SkillEvolutionService(
        chat=chat_service,
        tool_service=tool_service,
        max_iterations=settings.skills_background_review_max_iterations,
        max_concurrent=settings.skills_background_review_max_concurrent,
        enabled=settings.skills_background_review_enabled,
        nudge_interval=settings.skills_creation_nudge_interval,
        model=None,
        timeout_seconds=settings.skills_background_review_timeout_seconds,
    )
    graph_runner.evolution_service = skill_evolution_service
    # Skill Curator 周期维护 service. Built after evolution_service because
    # consolidation reuses its fork path; injected into graph_runner for the
    # finalize auto-trigger (maybe_run_curator, fire-and-forget).
    curator_state_store = SqliteCuratorStateStore(settings.sqlite_path)
    curator_policy = CuratorPolicy()
    skill_curator_service = SkillCuratorService(
        skill_registry=skill_registry,
        skill_usage_store=skill_usage_store,
        skill_service=skill_service,
        file_loader=skill_loader,
        backup_store=skill_backup_store,
        evolution_service=skill_evolution_service,
        curator_state_store=curator_state_store,
        curator_policy=curator_policy,
        settings=settings,
    )
    graph_runner.curator_service = skill_curator_service
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
        missed_grace_seconds=settings.scheduler_missed_grace_seconds,
        execution_timeout_seconds=settings.scheduler_execution_timeout_seconds,
        information_flow_service=information_flow_service,
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

    # Task subsystem wiring (T18 + T8). Built after schedule wiring because it
    # reuses chat_service + feishu_client + memory_store + tool_service.
    # Spec (T8): task_enabled=true -> initialize subsystem; init exceptions
    # propagate (fail-fast, NOT fail-soft). task_enabled=false -> skip entire
    # subsystem (task_service/task_run_service/task_runner stay None, no
    # user_task tools registered, lifespan does not start dispatcher).
    task_policy = TaskPolicy()
    task_registry: SQLiteTaskRegistry | None = None
    task_runner: TaskRunner | None = None
    task_agent_executor: TaskAgentExecutor | None = None
    task_outbound_delivery: TaskOutboundDelivery | None = None
    task_run_service: TaskRunService | None = None
    task_service: TaskService | None = None
    # task_schema_error stays "" under fail-fast semantics: when task_enabled
    # is True, init exceptions propagate and build_application_services raises;
    # when task_enabled is False, no init is attempted. Retained as a constant
    # so health_snapshot can keep its existing schema_error field shape.
    task_schema_error: str = ""
    # Task config service is assembled unconditionally (outside task_enabled):
    # the security page and config API must work even when the task subsystem
    # is disabled, so saved overrides apply on re-enable.
    task_config_store = SqliteTaskConfigStore(str(settings.sqlite_path))
    task_config_service = TaskConfigService(
        settings, task_config_store, TaskConfigLoggingSink(),
    )
    if settings.task_enabled:
        # SQLiteTaskRegistry performs schema init in __init__; failures
        # propagate (spec: 初始化异常必须让启动失败，不得伪装成"子系统不可用"
        # 静默降级).
        task_registry = SQLiteTaskRegistry(str(settings.sqlite_path))
        task_runner = TaskRunner(
            interval_seconds=settings.task_dispatch_interval_seconds,
            shutdown_grace_seconds=settings.task_shutdown_grace_seconds,
        )
        # Resolve attachments_root to an absolute path under workspace_root
        # (spec: attachments_root 解析到允许的本地持久化根; 不以字符串前缀
        # 代替 Path.resolve()/is_relative_to()).
        attachments_root = settings.task_attachments_root
        if not attachments_root.is_absolute():
            attachments_root = settings.workspace_root / attachments_root
        attachments_root = attachments_root.resolve()
        attachments_root.mkdir(parents=True, exist_ok=True)

        task_agent_executor = TaskAgentExecutor(
            chat_service=chat_service,
            task_registry=task_registry,
            prompt_builder=build_system_prompt,
            max_runtime_seconds=settings.task_max_runtime_seconds,
            goal_max_turns=settings.task_goal_max_turns,
            task_config_provider=task_config_service,
        )
        task_outbound_delivery = TaskOutboundDelivery(feishu_client, task_registry)

        async def _task_lifecycle_writer(
            session_id: str, content: str, card: dict[str, Any] | None = None,
        ) -> None:
            """向执行会话写 ui.task_lifecycle system 消息（TaskRunService/TaskService 共用）。

            card 为可选结构化交互载荷，透传给 SessionService.append_task_lifecycle_message。
            会话已不存在（SessionNotFoundError）静默跳过、不复活；其它异常向上传播，
            由调用方 _write_lifecycle 的 broad except 记录 warning。
            """
            try:
                await session_service.append_task_lifecycle_message(
                    session_id, content, card,
                )
            except SessionNotFoundError:
                logger.debug("task lifecycle session absent: %s", session_id)

        async def _task_result_writer(session_id: str, content: str) -> None:
            """向执行会话写 ui.task_result system 消息（SUCCEEDED 最终结果，普通消息渲染）。

            会话已不存在（SessionNotFoundError）静默跳过、不复活；其它异常向上传播，
            由调用方 _write_result 的 broad except 记录 warning。
            """
            try:
                await session_service.append_task_result_message(session_id, content)
            except SessionNotFoundError:
                logger.debug("task result session absent: %s", session_id)

        task_run_service = TaskRunService(
            registry=task_registry,
            dispatcher=task_runner,
            executor=task_agent_executor,
            policy=task_policy,
            notifier=task_outbound_delivery,
            lifecycle_writer=_task_lifecycle_writer,
            result_writer=_task_result_writer,
            lease_seconds=settings.task_lease_seconds,
            heartbeat_timeout_seconds=settings.task_heartbeat_timeout_seconds,
            max_runtime_seconds=settings.task_max_runtime_seconds,
            max_concurrency=settings.task_max_concurrency,
            task_config_provider=task_config_service,
        )
        # Late-bind to resolve circular dep:
        #   TaskRunService needs dispatcher=TaskRunner,
        #   TaskRunner.spawn needs run_service to call run_claim.
        task_runner.set_run_service(task_run_service)
        task_service = TaskService(
            registry=task_registry,
            policy=task_policy,
            memory_store=memory_store,
            attachments_root=attachments_root,
            attachment_max_bytes=settings.task_attachment_max_bytes,
            attachment_task_max_bytes=settings.task_attachment_task_max_bytes,
            lifecycle_writer=_task_lifecycle_writer,
            task_config_provider=task_config_service,
        )
        # TaskService.dispatch_tick delegates to TaskRunService (late-bind).
        task_service.set_run_service(task_run_service)
        # Wire TaskManagementToolExecutor into CompositeToolExecutor routes.
        # Single authoritative source for task tool execution (no duplicate
        # dynamic registration; spec: 启动时发现重名即失败).
        task_management_executor = TaskManagementToolExecutor(task_service)
        for definition in task_tool_definitions():
            routes[definition.name] = task_management_executor
        # Wire UserTaskToolExecutor (natural-language task delegation +
        # approval) into routes. 仅在 task 子系统启用时注册（本块已在 if
        # settings.task_enabled: 内）；source_type=AGENT+SAFE+managed=false，
        # realtime 可见、SAFE_ONLY 默认隐藏，worker/judge 不得 grant 这五个
        # 名字（防递归）。spec: spec-260720-chat-natural-language-task.md,
        # spec-260721-chat-nl-approval.md
        user_task_executor = UserTaskToolExecutor(task_service)
        # 启动时断言五个用户侧工具名未与既有 static/dynamic 定义或 route 冲突
        # （spec）。
        _existing_tool_names = {d.name for d in tool_service.list_definitions()}
        for _user_task_name in (
            "create_task",
            "list_tasks",
            "approve_task",
            "reject_task",
            "revise_task",
        ):
            if _user_task_name in routes or _user_task_name in _existing_tool_names:
                raise RuntimeError(
                    f"duplicate tool name on startup: {_user_task_name}"
                )
        routes["create_task"] = user_task_executor
        routes["list_tasks"] = user_task_executor
        routes["approve_task"] = user_task_executor
        routes["reject_task"] = user_task_executor
        routes["revise_task"] = user_task_executor
        # 五个用户侧工具合并到唯一 source key `user_task`，不传
        # override_static_names（spec）。
        tool_service.set_dynamic_definitions(
            "user_task",
            user_task_tool_definitions() + user_task_approval_tool_definitions(),
        )
        tool_service.executor = CompositeToolExecutor(
            routes, fallback=McpToolExecutor(mcp_service)
        )

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
    # Build SandboxPolicy + config (T10: grant-based authorization)
    _callback_tools_raw = settings.sandbox_callback_tools
    if isinstance(_callback_tools_raw, str):
        _allowed_callbacks = frozenset(
            t.strip() for t in _callback_tools_raw.split(",") if t.strip()
        )
    else:
        _allowed_callbacks = frozenset(_callback_tools_raw or [])
    sandbox_domain_config = SandboxDomainConfig(
        timeout_seconds=settings.sandbox_timeout_seconds,
        max_tool_calls=settings.sandbox_max_tool_calls,
        cpus=settings.sandbox_docker_cpus,
        memory_mb=settings.sandbox_docker_memory_mb,
        network_enabled=settings.sandbox_docker_network,
        idle_seconds=settings.sandbox_idle_seconds,
        workspace_readonly=True,
        max_stdout_bytes=settings.sandbox_max_stdout_bytes,
        max_stderr_bytes=settings.sandbox_max_stderr_bytes,
        pids_limit=256,
        allowed_backends=frozenset({settings.sandbox_type}),
        allowed_callbacks=_allowed_callbacks,
    )
    sandbox_policy = SandboxPolicy(sandbox_domain_config)

    sandbox_tool_executor = SandboxToolExecutor(
        sandbox_manager=sandbox_manager,
        callback_registry=sandbox_callback_registry,
        settings=settings,
        history_registry=sandbox_history_registry,
        summary_max_stdout=settings.sandbox_summary_max_stdout_bytes,
        summary_max_stderr=settings.sandbox_summary_max_stderr_bytes,
        sandbox_policy=sandbox_policy,
        sandbox_config=sandbox_domain_config,
        budget_service=budget_service,
    )
    terminal_tool_executor = TerminalToolExecutor(
        sandbox_manager=sandbox_manager,
        settings=settings,
        history_registry=sandbox_history_registry,
        sandbox_policy=sandbox_policy,
        sandbox_config=sandbox_domain_config,
        budget_service=budget_service,
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
        terminal_description = (
            "Execute a shell command in the session sandbox. "
            "Docker backend: no network, workspace mounted read-only, scratch writable. "
            "Local backend: trusted-dev only, NOT a security boundary. "
            "Use workdir to run in /scratch/<session> (default) or /workspace/<path>. "
            "Non-zero exit code means the command ran but failed (still returns success)."
        )
        terminal_definition = ToolDefinition(
            name="terminal",
            description=terminal_description,
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1},
                    "workdir": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="sandbox",
            managed=False,
            enabled=True,
        )
        tool_service.set_dynamic_definitions(
            "sandbox", [execute_code_definition, terminal_definition]
        )
        routes["execute_code"] = sandbox_tool_executor
        routes["terminal"] = terminal_tool_executor
        tool_service.executor = CompositeToolExecutor(routes, fallback=McpToolExecutor(mcp_service))

    # Browser subsystem wiring (T10/T11/T12/T13). Gated on browser_enabled:
    # when False, the entire subsystem is skipped (no tools, no service, no
    # routes). T13 wires both ContainerBrowserBackend and HostCdpBrowserBackend
    # based on configuration; when a backend's required config is absent, it is
    # omitted from the backends dict (degraded mode for that backend type).
    browser_service: BrowserService | None = None
    browser_dashboard_service_obj: BrowserDashboardService | None = None
    browser_confirmation_service_obj: BrowserConfirmationService | None = None
    browser_novnc_proxy_obj: BrowserNoVncProxy | None = None
    if settings.browser_enabled:
        browser_registry = SqliteBrowserSessionRegistry(settings.sqlite_path)
        browser_screenshot_store = SqliteBrowserScreenshotStore(
            settings.workspace_root / "locals" / "browser-screenshots",
            max_pixels=settings.browser_max_screenshot_pixels,
            max_per_session=settings.browser_per_session_screenshot_quota,
            ttl_seconds=settings.browser_screenshot_ttl_seconds,
        )
        browser_url_verifier = UrlVerifier()
        browser_policy = BrowserPolicy()
        browser_service_settings = BrowserServiceSettings(
            max_sessions_per_run=settings.browser_global_session_limit,
            action_timeout_seconds=float(settings.browser_action_timeout),
            screenshot_consumer_default="dashboard_internal",
            trusted_dev=settings.browser_trusted_dev,
        )
        # Build backends dict based on configuration.
        browser_backends: dict[BrowserBackendType, Any] = {}
        # Container backend: created when browser_container_endpoint is configured.
        if settings.browser_container_endpoint.strip():
            container_backend = ContainerBrowserBackend(
                endpoint=settings.browser_container_endpoint,
                url_verifier=browser_url_verifier,
                action_timeout_seconds=float(settings.browser_action_timeout),
                navigation_timeout_seconds=float(settings.browser_navigation_timeout),
                takeover_ttl_seconds=settings.browser_takeover_ttl_seconds,
                max_screenshot_bytes=settings.browser_max_screenshot_bytes,
                profile_runtime_endpoint=(
                    settings.browser_container_profile_runtime_endpoint
                ),
            )
            browser_backends[BrowserBackendType.CONTAINER] = container_backend
            browser_novnc_proxy_obj = BrowserNoVncProxy(
                settings.browser_container_novnc_endpoint
            )
        # Host CDP backend: created when host bridge URL + token path are
        # configured AND trusted_dev is True. trusted_dev gates host Chrome
        # access (production deployments must not enable host_cdp without
        # explicit trusted_dev opt-in).
        if (
            settings.browser_host_bridge_url.strip()
            and settings.browser_host_bridge_token_path is not None
            and settings.browser_trusted_dev
        ):
            if (
                settings.browser_max_screenshot_bytes
                != host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
            ):
                raise ValueError(
                    "host_bridge_screenshot_limit_invalid"
                )
            host_cdp_config = HostCdpBackendConfig(
                base_url=settings.browser_host_bridge_url,
                token_path=settings.browser_host_bridge_token_path,
                max_screenshot_bytes=(
                    host_protocol.HOST_CDP_MAX_SCREENSHOT_BYTES
                ),
                max_response_bytes=(
                    host_protocol.max_json_response_bytes()
                ),
            )
            host_cdp_backend = HostCdpBrowserBackend(host_cdp_config)
            browser_backends[BrowserBackendType.HOST_CDP] = host_cdp_backend
        browser_service = BrowserService(
            backends=browser_backends,
            registry=browser_registry,
            screenshot_store=browser_screenshot_store,
            browser_policy=browser_policy,
            default_backend=BrowserBackendType(settings.browser_default_backend),
            settings=browser_service_settings,
            audit_service=audit_service,
        )
        browser_tool_executor = BrowserToolExecutor(browser_service)
        browser_defs = browser_tool_definitions()
        tool_service.set_dynamic_definitions("browser", browser_defs)
        for browser_def in browser_defs:
            routes[browser_def.name] = browser_tool_executor
        tool_service.executor = CompositeToolExecutor(
            routes, fallback=McpToolExecutor(mcp_service)
        )
        # Browser Dashboard services (T14/T15).
        browser_confirmation_service_obj = BrowserConfirmationService(
            ttl_seconds=settings.browser_takeover_ttl_seconds
        )
        browser_dashboard_service_obj = BrowserDashboardService(
            browser_service=browser_service,
            screenshot_store=browser_screenshot_store,
            confirmation_service=browser_confirmation_service_obj,
            settings=settings,
        )
        # Inject browser dashboard service + grant TTL into the graph runner so
        # _request_browser_host_grant_approval can call grant_host after the
        # Chat CONFIRM card is approved. graph_runner was constructed earlier
        # (before browser services existed); setattr injects the dependency.
        if settings.browser_enabled:
            graph_runner._browser_dashboard_service = browser_dashboard_service_obj
            graph_runner._browser_host_grant_ttl_seconds = (
                settings.browser_host_grant_ttl_seconds
            )

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
            "host_terminal": {
                "status": (
                    "disabled"
                    if not settings.host_terminal_enabled
                    else "unavailable"
                    if host_terminal_executor is None
                    else "ok"
                    if host_terminal_executor.last_health_code == "ok"
                    else "degraded"
                ),
                "reason": (
                    host_terminal_executor.last_health_code
                    if host_terminal_executor is not None
                    else host_terminal_health_reason
                ),
                "enabled": host_terminal_executor is not None,
            },
            "task": {
                "status": (
                    "disabled"
                    if not settings.task_enabled
                    else "error"
                    if task_schema_error
                    else "ok"
                    if task_run_service is not None
                    else "error"
                ),
                "enabled": settings.task_enabled,
                "schema_error": task_schema_error,
                "runner_wired": task_run_service is not None,
            },
        }

    # T3: Create ONE GatewayToolApprovalService and share it between
    # GatewayService and the Dashboard router.  Do NOT let either side
    # construct its own -- the identity must be preserved so that session
    # grants issued via the Dashboard claim endpoint are visible to the
    # gateway's allowed_confirm_tools_override / trust_session checks.
    tool_approval_service = GatewayToolApprovalService(memory_store)
    gateway_service = GatewayService(
        gateway_registry,
        chat_service,
        session_service,
        tool_service,
        model_service,
        health_snapshot,
        schedule_service=schedule_service,
        require_actor_for_managed_actions=settings.gateway_require_actor_for_managed_actions,
        confirmation_ttl_seconds=settings.gateway_confirmation_ttl_seconds,
        tool_approval_service=tool_approval_service,
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
        skill_usage_store=skill_usage_store,
        skill_pending_store=skill_pending_store,
        skill_backup_store=skill_backup_store,
        skill_evolution_service=skill_evolution_service,
        skill_curator_service=skill_curator_service,
        curator_state_store=curator_state_store,
        plugin_service=plugin_service,
        knowledge_service=knowledge_service,
        external_memory_service=external_memory_service,
        external_memory_provider_service=external_memory_provider_service,
        feishu_im_adapter=feishu_im_adapter,
        platform_registry=platform_registry,
        platform_service=platform_service,
        health_snapshot=health_snapshot,
        policy_dashboard_service=PolicyDashboardService(SettingsPolicyProfileProvider(settings)),
        task_security_dashboard_service=TaskSecurityDashboardService(settings, task_config_service),
        task_config_service=task_config_service,
        tool_approval_service=tool_approval_service,
        image_store=image_store,
        usage_service=usage_service,
        sandbox_dashboard_service=sandbox_dashboard_service if settings.sandbox_enabled else None,
        sandbox_manager=sandbox_manager if settings.sandbox_enabled else None,
        host_terminal_dashboard_service=host_terminal_dashboard_service,
        task_service=task_service,
        task_run_service=task_run_service,
        task_runner=task_runner,
        browser_service=browser_service,
        browser_dashboard_service=browser_dashboard_service_obj,
        browser_confirmation_service=browser_confirmation_service_obj,
        browser_novnc_proxy=browser_novnc_proxy_obj,
    )


class _NoOpToolService:
    """Minimal no-op tool service for lightweight CLI command discovery.

    Provides just the interface ``PluginService.scan()`` needs (``definitions``
    + ``replace_dynamic_definitions``) without publishing tools to any live
    tool surface. Used by ``collect_plugin_cli_commands`` so plugin CLI
    command discovery does NOT construct the full ``ToolService`` or publish
    tools/hooks/routes.
    """

    def __init__(self) -> None:
        self.definitions: dict[str, Any] = {}

    def replace_dynamic_definitions(
        self,
        source_key: str,
        definitions: list[Any],
        override_static_names: set[str] | None = None,
    ) -> None:
        pass

    def list_definitions(self) -> list[Any]:
        return list(self.definitions.values())


def collect_plugin_cli_commands() -> list[PluginCliCommand]:
    """Lightweight composition helper for plugin CLI command discovery.

    Constructs ONLY the minimal objects needed to collect plugin CLI commands:
    ``Settings``, ``SQLitePluginRegistry``, ``PluginFileLoader``, and a
    ``PluginService`` wired with a no-op ``tool_service`` + no-op
    ``route_refresher``. Calls ``plugin_service.scan()`` to populate
    ``_cli_commands`` (this runs enabled plugins' ``register(ctx)`` to collect
    ``cli_command_registrations``; candidate Contexts are discarded after
    scan).

    MUST NOT call ``build_application_services()``; MUST NOT construct
    Provider/MCP/Feishu/Scheduler/AgentGraphRunner; MUST NOT publish
    tools/hooks/routes to any live ``tool_service``.

    On any global failure (exception): log warning, return empty list (so the
    CLI never crashes due to plugin discovery).
    """
    try:
        settings = Settings()
        plugin_registry = SQLitePluginRegistry(settings.sqlite_path)
        plugin_loader = PluginFileLoader(PluginFileLoaderConfig(
            bundled_root=Path(__file__).resolve().parent / "infrastructure" / "plugin" / "seeds",
            user_root=settings.plugins_root,
            project_root=settings.workspace_root / ".hermes" / "plugins",
            enable_entrypoints=settings.enable_plugin_entrypoints,
            enable_project=settings.enable_project_plugins,
            safe_mode=settings.plugins_safe_mode,
        ))
        plugin_service = PluginService(
            registry=plugin_registry,
            loader=plugin_loader,
            tool_service=_NoOpToolService(),
            route_refresher=lambda names: None,
            settings=settings,
        )
        _run_sync(plugin_service.scan())
        return plugin_service.list_cli_commands()
    except Exception:
        logger.warning("plugin CLI command discovery failed", exc_info=True)
        return []


def _default_browser_actor_resolver(request: Any) -> str | None:
    """Default actor resolver for Browser Dashboard routes.

    Derives the actor from a trusted server-side header (X-Dashboard-Actor)
    set by the Dashboard frontend. In production deployments this should be
    replaced with a resolver that reads from an authenticated session cookie
    or mTLS identity. The actor is NEVER read from HTTP body/query/metadata.
    """
    actor = request.headers.get("x-dashboard-actor", "") if hasattr(request, "headers") else ""
    if actor and isinstance(actor, str) and actor.strip():
        return actor.strip()
    # Fallback: dashboard operator (trusted same-origin context).
    return "dashboard-operator"


def create_app(settings: Settings | None = None) -> FastAPI:
    _configure_logging()
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
        # Task dispatcher (T18). Only start when task_enabled AND schema
        # migration succeeded (task_run_service is bound). Migration failure
        # -> health unhealthy and dispatcher not started (spec).
        if (
            services.settings.task_enabled
            and services.task_run_service is not None
            and services.task_runner is not None
        ):
            try:
                await services.task_runner.start()
            except Exception:
                logger.exception("TaskRunner start failed")
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
            if (
                services.task_runner is not None
                and services.task_runner._started
            ):
                try:
                    await services.task_runner.stop()
                except Exception:
                    logger.exception("TaskRunner stop failed")
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
            # Browser subsystem best-effort shutdown (T10). Close non-closed
            # sessions; failures are logged but do not block shutdown or fake
            # closed status. T10: backends dict is empty (no backends wired),
            # so close_session only releases registry/lease resources.
            if services.browser_service is not None:
                try:
                    import sqlite3 as _sqlite3
                    _registry = services.browser_service._registry
                    _db_path = getattr(_registry, "path", None)
                    if _db_path is not None:
                        _conn = _sqlite3.connect(str(_db_path))
                        _conn.row_factory = _sqlite3.Row
                        _rows = _conn.execute(
                            "SELECT id, n_agent_session_id FROM browser_sessions "
                            "WHERE status != 'closed'"
                        ).fetchall()
                        _conn.close()
                        for _row in _rows:
                            try:
                                await services.browser_service.close_session(
                                    _row["n_agent_session_id"]
                                )
                            except Exception:
                                logger.warning(
                                    "browser session close failed: %s",
                                    _row["id"],
                                    exc_info=True,
                                )
                except Exception:
                    logger.exception("browser shutdown failed")

    app = FastAPI(title="N-Agent", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_openai_compatible_router(services.chat_service, services.model_service, services.memory_store))
    app.include_router(create_platforms_router(services.platform_service))
    # T3: One DashboardToolApprovalBridge shared with the same
    # GatewayToolApprovalService instance that GatewayService holds.
    dashboard_tool_approval_bridge = DashboardToolApprovalBridge()
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
            host_terminal_dashboard_service=services.host_terminal_dashboard_service,
            usage_service=services.usage_service,
            memory_store=services.memory_store,
            chat_service=services.chat_service,
            policy_dashboard_service=services.policy_dashboard_service,
            skill_pending_store=services.skill_pending_store,
            skill_usage_store=services.skill_usage_store,
            image_store=services.image_store,
            task_security_dashboard_service=services.task_security_dashboard_service,
            task_config_service=services.task_config_service,
            task_service=services.task_service,
            task_run_service=services.task_run_service,
            browser_dashboard_service=services.browser_dashboard_service,
            browser_confirmation_service=services.browser_confirmation_service,
            browser_novnc_proxy=services.browser_novnc_proxy,
            browser_actor_resolver=_default_browser_actor_resolver,
            dashboard_tool_approval_bridge=dashboard_tool_approval_bridge,
            tool_approval_service=services.tool_approval_service,
            settings=services.settings,
        )
    )
    return app
