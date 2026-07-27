from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    provider_base_url: str = Field(default="http://localhost:11434/v1")
    provider_api_key: str = Field(default="")
    provider_model: str = Field(default="qwen2.5")
    sqlite_path: Path = Field(default=Path("locals/sessions.db"))
    workspace_root: Path = Field(default=Path.cwd())
    agent_iteration_limit: int = Field(default=10, ge=1, le=20)
    kb_enabled: bool = Field(default=False)
    kb_base_url: str = Field(default="")
    kb_default_top_k: int = Field(default=5, ge=1, le=50)
    kb_default_min_score: float = Field(default=0.5, ge=0, le=1)
    kb_timeout_seconds: float = Field(default=10, gt=0)
    web_fetch_enabled: bool = Field(default=True)
    web_fetch_timeout_seconds: float = Field(default=10, gt=0)
    web_fetch_max_bytes: int = Field(default=131072, ge=1024)
    web_fetch_allow_private_urls: bool = Field(default=False)
    mcp_connect_timeout_seconds: float = Field(default=10, gt=0)
    mcp_max_tools: int = Field(default=50, ge=1, le=500)
    mcp_max_schema_bytes: int = Field(default=65536, ge=1024)
    mcp_max_result_bytes: int = Field(default=262144, ge=1024)
    mcp_allow_private_hosts: bool = Field(default=False)
    gateway_enabled: bool = Field(default=True)
    scheduler_enabled: bool = Field(default=True)
    scheduler_tick_seconds: float = Field(default=30, gt=0)
    scheduler_max_due_per_tick: int = Field(default=5, ge=1, le=100)
    scheduler_missed_grace_seconds: int = Field(default=300, ge=0)
    scheduler_lease_seconds: int = Field(default=900, ge=30)
    scheduler_execution_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    scheduler_timezone: str = Field(default="Asia/Shanghai")
    feishu_enabled: bool = Field(default=False)
    feishu_app_id: str = Field(default="")
    feishu_app_secret: str = Field(default="")
    feishu_tenant_key: str = Field(default="")
    feishu_allowed_open_ids: list[str] | str = Field(default_factory=list)
    feishu_allowed_chat_ids: list[str] | str = Field(default_factory=list)
    skills_root: Path = Field(default=Path("/workspace/skills"))
    skills_inline_shell_enabled: bool = Field(default=False)
    skills_inline_shell_timeout: int = Field(default=10, ge=1, le=120)
    skills_max_view_bytes: int = Field(default=131072, ge=1024)
    skills_max_count: int = Field(default=200, ge=1, le=2000)

    # Skill 自进化配置
    skills_creation_nudge_interval: int = Field(default=10)
    skills_background_review_enabled: bool = Field(default=True)
    skills_background_review_max_iterations: int = Field(default=16)
    skills_background_review_timeout_seconds: int = Field(default=120)
    skills_background_review_max_concurrent: int = Field(default=1)
    skills_write_approval: bool = Field(default=False)
    skills_guard_agent_created: bool = Field(default=True)
    skills_backup_enabled: bool = Field(default=True)
    skills_backup_keep: int = Field(default=10)
    skills_archive_not_delete: bool = Field(default=True)

    # Skill Curator 周期维护配置
    skills_curator_enabled: bool = Field(default=True)
    skills_curator_interval_hours: int = Field(default=168, ge=1)
    skills_curator_min_idle_hours: float = Field(default=2.0, gt=0)
    skills_curator_stale_after_days: int = Field(default=30, ge=1)
    skills_curator_archive_after_days: int = Field(default=90, ge=1)
    skills_curator_prune_seeds: bool = Field(default=False)
    skills_curator_consolidate: bool = Field(default=False)
    skills_curator_consolidate_max_iterations: int = Field(default=64, ge=1)

    # Restricted host-terminal bridge (disabled unless every required value is set)
    host_terminal_enabled: bool = Field(default=False)
    host_terminal_bridge_url: str = Field(default="")
    host_terminal_policy_path: Path | None = Field(default=None)
    host_terminal_token_path: Path | None = Field(default=None)
    host_terminal_host_workspace_root: Path | None = Field(default=None)
    host_terminal_host_skills_root: Path | None = Field(default=None)
    host_terminal_tool_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    host_terminal_bridge_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    host_terminal_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=60)
    host_terminal_transfer_margin_seconds: float = Field(default=5.0, gt=0, le=60)
    host_terminal_max_response_bytes: int = Field(default=1048576, ge=1024)
    host_terminal_max_stdout_bytes: int = Field(default=65536, ge=1)
    host_terminal_max_stderr_bytes: int = Field(default=16384, ge=1)
    host_terminal_max_concurrency: int = Field(default=1, ge=1)

    # 图片持久化：photo-upload 产出的 OSS 签名 URL 约 1h 过期，飞书投递时重传为
    # 永久 image_key，而 Dashboard Chat 存的是原始临时 URL、过期后裂图。后端在
    # 工具成功时把图片落地到 image_store_dir，经 /chat/images/{id} 永久对外服务。
    image_store_dir: Path = Field(default=Path("locals/images"))
    dashboard_base_url: str = Field(default="http://localhost:8201")

    # Plugin 配置
    plugins_root: Path = Field(default=Path("/workspace/plugins"))
    plugins_enabled: list[str] | str = Field(default_factory=lambda: ["hello"])
    plugins_disabled: list[str] | str = Field(default_factory=list)
    plugins_safe_mode: bool = Field(default=False)
    enable_project_plugins: bool = Field(default=False)
    enable_plugin_entrypoints: bool = Field(default=False)
    plugin_tool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    # override allowlist: 非空时仅允许名单内插件加载，精确匹配（不支持前缀/glob）
    plugins_override_allowlist: list[str] | str = Field(default_factory=list)
    # hook 执行超时（秒），覆盖 plugin_tool_timeout_seconds 的默认 30s 仅用于 hook 场景
    plugin_hook_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    # 外部记忆配置
    external_memory_provider: str | None = None
    external_memory_enabled_providers: list[str] | None = None
    external_memory_path: str = "./locals/external-memory"
    external_memory_memory_limit: int = 4000
    external_memory_user_limit: int = 2000
    # project_root 复用 workspace_root，不需要新增配置

    # 沙盒配置
    sandbox_enabled: bool = Field(default=False)
    sandbox_type: str = Field(default="docker")
    sandbox_timeout_seconds: int = Field(default=300, gt=0)
    sandbox_max_tool_calls: int = Field(default=50, ge=1)
    sandbox_max_stdout_bytes: int = Field(default=50000, ge=1024)
    sandbox_max_stderr_bytes: int = Field(default=10000, ge=1024)
    sandbox_summary_max_stdout_bytes: int = Field(default=2000, ge=256)
    sandbox_summary_max_stderr_bytes: int = Field(default=500, ge=128)
    sandbox_docker_image: str = Field(default="python:3.11-slim")
    sandbox_docker_cpus: float = Field(default=1.0, gt=0)
    sandbox_docker_memory_mb: int = Field(default=512, ge=64)
    sandbox_docker_network: bool = Field(default=False)
    sandbox_callback_tools: list[str] | str = Field(
        default_factory=lambda: [
            "read_file", "write_file", "search_files", "patch",
            "web_extract", "web_search",
        ]
    )
    sandbox_web_search_enabled: bool = Field(default=True)
    sandbox_search_provider: str = Field(default="duckduckgo")
    sandbox_idle_seconds: int = Field(default=900, gt=0)
    sandbox_release_wait_timeout_seconds: int = Field(default=30, gt=0)
    sandbox_docker_host_workspace_root: Path | None = Field(default=None)
    sandbox_docker_host_locals_root: Path | None = Field(default=None)
    sandbox_scratch_root: Path | None = Field(default=None)

    # ACP 配置
    acp_host_workspace_root: Path | None = Field(default=None)
    acp_container_workspace_root: Path | None = Field(default=None)

    # 上下文压缩配置
    context_compression_enabled: bool = True
    context_length: int = Field(default=32000, ge=1024)
    context_compression_threshold: float = Field(default=0.50, gt=0, le=1)
    context_compression_target_ratio: float = Field(default=0.20, gt=0, le=1)
    context_compression_tail_budget_enabled: bool = False
    context_compression_protect_first_n: int = Field(default=3, ge=0)
    context_compression_protect_last_n: int = Field(default=10, ge=0)
    context_compression_cooldown_seconds: int = Field(default=300, ge=0)

    # Policy 治理配置
    # Turn
    agent_turn_timeout_seconds: int = Field(default=900, gt=0)
    # LLM
    llm_fallback_enabled: bool = Field(default=False)
    # Memory
    memory_cross_session_read_enabled: bool = Field(default=False)
    memory_unattended_write_enabled: bool = Field(default=False)
    # Budget
    budget_max_wall_seconds: int = Field(default=900, gt=0)
    budget_max_llm_calls: int = Field(default=10, gt=0)
    budget_max_tool_calls: int = Field(default=100, gt=0)
    budget_max_token_cost: int | None = Field(default=None, ge=0)
    budget_max_usd_cost: Decimal | None = Field(default=None, ge=0)
    # Budget -- Sandbox 累计配额
    budget_max_sandbox_seconds: float | None = Field(default=None, ge=0)
    budget_max_sandbox_cpu_seconds: float | None = Field(default=None, ge=0)
    budget_max_sandbox_memory_mb_seconds: float | None = Field(default=None, ge=0)
    budget_max_sandbox_callback_calls: int | None = Field(default=None, ge=0)
    # Gateway
    gateway_confirmation_ttl_seconds: int = Field(default=900, gt=0)
    gateway_require_actor_for_managed_actions: bool = Field(default=True)
    # InformationFlow
    information_log_llm_payloads: bool = Field(default=False)
    information_store_usage_payloads: bool = Field(default=True)
    information_redact_secrets: bool = Field(default=True)

    # Task 子域配置 (T18 S3). N_AGENT_TASK_ 前缀.
    # 基础时长与并发
    task_enabled: bool = Field(default=True)
    task_dispatch_interval_seconds: int = Field(default=30, gt=0)
    task_lease_seconds: int = Field(default=900, gt=0)
    task_heartbeat_timeout_seconds: int = Field(default=300, gt=0)
    task_max_runtime_seconds: int = Field(default=3600, gt=0)
    task_failure_limit: int = Field(default=3, ge=1)  # 映射 Task.max_retries 默认
    task_max_concurrency: int = Field(default=4, ge=1)
    task_shutdown_grace_seconds: int = Field(default=30, gt=0)
    # 审批 note 上限（C 类可配，env N_AGENT_TASK_NOTE_MAX_CODEPOINTS）
    task_note_max_codepoints: int = Field(default=2000, ge=1)
    # 规划与附件上限（下游 T13/T16/T19 依赖）
    task_planning_max_children: int = Field(default=20, ge=1)
    task_goal_max_turns: int = Field(default=10, ge=1)
    task_attachment_max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    task_attachment_task_max_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    # 附件根路径（相对路径在 main.py 装配时 resolve 到 workspace_root 下）
    task_attachments_root: Path = Field(default=Path("locals/task-attachments"))

    # Browser 子域配置 (B class env-only). N_AGENT_BROWSER_ 前缀.
    # disabled unless explicitly enabled; container backend is the default.
    browser_enabled: bool = Field(default=False)
    browser_default_backend: str = Field(default="container")
    browser_container_endpoint: str = Field(default="")
    browser_action_timeout: int = Field(default=30, gt=0)
    browser_navigation_timeout: int = Field(default=30, gt=0)
    browser_max_observe_chars: int = Field(default=4000, ge=1, le=20000)
    browser_max_observe_elements: int = Field(default=80, ge=1, le=200)
    browser_max_screenshot_bytes: int = Field(default=1048576, ge=1024)
    browser_max_screenshot_pixels: int = Field(default=10_000_000, ge=1)
    browser_screenshot_ttl_seconds: int = Field(default=86400, gt=0)
    browser_per_session_screenshot_quota: int = Field(default=20, ge=1)
    browser_poll_interval_seconds: int = Field(default=2, ge=1, le=5)
    browser_global_session_limit: int = Field(default=4, ge=1)
    browser_host_bridge_url: str = Field(default="")
    browser_host_bridge_token_path: Path | None = Field(default=None)
    browser_host_grant_ttl_seconds: int = Field(default=300, gt=0)
    browser_takeover_ttl_seconds: int = Field(default=60, gt=0)
    browser_trusted_dev: bool = Field(default=False)

    model_config = SettingsConfigDict(env_file=".env", env_prefix="N_AGENT_", extra="ignore")

    @field_validator("sandbox_type")
    @classmethod
    def validate_sandbox_type(cls, value: str) -> str:
        if value not in ("local", "docker"):
            raise ValueError(
                f"invalid sandbox_type: {value!r} (must be 'local' or 'docker')"
            )
        return value

    @field_validator("browser_default_backend")
    @classmethod
    def validate_browser_default_backend(cls, value: str) -> str:
        if value not in ("host_cdp", "container"):
            raise ValueError(
                f"invalid browser_default_backend: {value!r} "
                "(must be 'host_cdp' or 'container')"
            )
        return value

    @model_validator(mode="after")
    def _validate_context_compression_ratios(self) -> "Settings":
        if self.context_compression_target_ratio >= self.context_compression_threshold:
            raise ValueError(
                "context_compression_target_ratio must be less than "
                "context_compression_threshold"
            )
        return self

    @model_validator(mode="after")
    def _validate_task_subsystem(self) -> "Settings":
        # lease 由 heartbeat 续租，max_runtime 可大于初始 lease，禁止加入
        # max_runtime < lease 的错误约束。
        if self.task_heartbeat_timeout_seconds >= self.task_lease_seconds:
            raise ValueError(
                "task_heartbeat_timeout_seconds must be less than "
                "task_lease_seconds"
            )
        if self.task_dispatch_interval_seconds >= self.task_lease_seconds:
            raise ValueError(
                "task_dispatch_interval_seconds must be less than "
                "task_lease_seconds"
            )
        if self.task_attachment_task_max_bytes < self.task_attachment_max_bytes:
            raise ValueError(
                "task_attachment_task_max_bytes must be >= "
                "task_attachment_max_bytes"
            )
        # attachments_root: 拒绝 ".." 穿越；不以字符串前缀代替路径校验
        if any(part == ".." for part in self.task_attachments_root.parts):
            raise ValueError(
                "task_attachments_root must not contain '..' traversal"
            )
        return self

    @model_validator(mode="after")
    def _validate_browser_backend_config(self) -> "Settings":
        """Cross-field validation for browser backend configuration.

        When browser_enabled is True and the default backend is host_cdp,
        require the host bridge URL, token path, and trusted_dev flag. When
        the default backend is container, require the container endpoint.
        When browser_enabled is False, no validation is applied (allows
        constructing Settings without backend config in disabled mode).
        """
        if not self.browser_enabled:
            return self
        if self.browser_default_backend == "host_cdp":
            if not self.browser_host_bridge_url.strip():
                raise ValueError(
                    "browser_host_bridge_url is required when "
                    "browser_default_backend is host_cdp"
                )
            if self.browser_host_bridge_token_path is None:
                raise ValueError(
                    "browser_host_bridge_token_path is required when "
                    "browser_default_backend is host_cdp"
                )
            if not self.browser_trusted_dev:
                raise ValueError(
                    "browser_trusted_dev must be True when "
                    "browser_default_backend is host_cdp (host Chrome access "
                    "requires explicit trusted_dev opt-in)"
                )
        elif self.browser_default_backend == "container":
            if not self.browser_container_endpoint.strip():
                raise ValueError(
                    "browser_container_endpoint is required when "
                    "browser_default_backend is container"
                )
        return self

    @field_validator(
        "sqlite_path", "workspace_root", "skills_root", "plugins_root",
        "sandbox_docker_host_workspace_root", "sandbox_docker_host_locals_root",
        "sandbox_scratch_root",
        "acp_host_workspace_root", "acp_container_workspace_root",
        "host_terminal_policy_path", "host_terminal_token_path",
        "host_terminal_host_workspace_root",
        "host_terminal_host_skills_root",
        "task_attachments_root",
        "browser_host_bridge_token_path",
        mode="before",
    )
    @classmethod
    def expand_path(cls, value: str | Path | None) -> Path | None:
        if value is None or value == "":
            return None
        return Path(value).expanduser()

    @field_validator("workspace_root")
    @classmethod
    def resolve_workspace(cls, value: Path) -> Path:
        return value.resolve()

    @field_validator("scheduler_timezone")
    @classmethod
    def validate_scheduler_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"invalid scheduler timezone: {value}") from exc
        return value

    @field_validator("feishu_allowed_open_ids", "feishu_allowed_chat_ids", "sandbox_callback_tools", "plugins_enabled", "plugins_disabled", "plugins_override_allowlist", mode="before")
    @classmethod
    def parse_csv_list(cls, value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            items = [item.strip() for item in value.split(",") if item.strip()]
        else:
            items = value
        # stable dedupe: preserve first-occurrence order
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result
