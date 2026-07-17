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

    # Plugin 配置
    plugins_root: Path = Field(default=Path("/workspace/plugins"))
    plugins_enabled: list[str] | str = Field(default_factory=lambda: ["hello"])
    plugins_disabled: list[str] | str = Field(default_factory=list)
    plugins_safe_mode: bool = Field(default=False)
    enable_project_plugins: bool = Field(default=False)
    enable_plugin_entrypoints: bool = Field(default=False)
    plugin_tool_timeout_seconds: int = Field(default=30, ge=1, le=300)

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

    model_config = SettingsConfigDict(env_file=".env", env_prefix="N_AGENT_", extra="ignore")

    @field_validator("sandbox_type")
    @classmethod
    def validate_sandbox_type(cls, value: str) -> str:
        if value not in ("local", "docker"):
            raise ValueError(
                f"invalid sandbox_type: {value!r} (must be 'local' or 'docker')"
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

    @field_validator(
        "sqlite_path", "workspace_root", "skills_root", "plugins_root",
        "sandbox_docker_host_workspace_root", "sandbox_docker_host_locals_root",
        "sandbox_scratch_root",
        "acp_host_workspace_root", "acp_container_workspace_root",
        "host_terminal_policy_path", "host_terminal_token_path",
        "host_terminal_host_workspace_root",
        "host_terminal_host_skills_root",
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

    @field_validator("feishu_allowed_open_ids", "feishu_allowed_chat_ids", "sandbox_callback_tools", "plugins_enabled", "plugins_disabled", mode="before")
    @classmethod
    def parse_csv_list(cls, value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
