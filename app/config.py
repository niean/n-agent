from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
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

    model_config = SettingsConfigDict(env_file=".env", env_prefix="N_AGENT_", extra="ignore")

    @field_validator("sandbox_type")
    @classmethod
    def validate_sandbox_type(cls, value: str) -> str:
        if value not in ("local", "docker"):
            raise ValueError(
                f"invalid sandbox_type: {value!r} (must be 'local' or 'docker')"
            )
        return value

    @field_validator(
        "sqlite_path", "workspace_root", "skills_root", "plugins_root",
        "sandbox_docker_host_workspace_root", "sandbox_docker_host_locals_root",
        "sandbox_scratch_root",
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
