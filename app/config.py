from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    provider_base_url: str = Field(default="http://localhost:11434/v1")
    provider_api_key: str = Field(default="")
    provider_model: str = Field(default="qwen2.5")
    sqlite_path: Path = Field(default=Path("locals/sessions.db"))
    workspace_root: Path = Field(default=Path.cwd())
    agent_iteration_limit: int = Field(default=5, ge=1, le=20)
    kb_enabled: bool = Field(default=False)
    kb_base_url: str = Field(default="")
    kb_default_top_k: int = Field(default=5, ge=1, le=50)
    kb_default_min_score: float = Field(default=0.5, ge=0, le=1)
    kb_timeout_seconds: float = Field(default=10, gt=0)
    gateway_enabled: bool = Field(default=True)
    feishu_enabled: bool = Field(default=False)
    feishu_app_id: str = Field(default="")
    feishu_app_secret: str = Field(default="")
    feishu_tenant_key: str = Field(default="")
    feishu_allowed_open_ids: list[str] | str = Field(default_factory=list)
    feishu_allowed_chat_ids: list[str] | str = Field(default_factory=list)

    model_config = SettingsConfigDict(env_file=".env", env_prefix="N_AGENT_", extra="ignore")

    @field_validator("sqlite_path", "workspace_root", mode="before")
    @classmethod
    def expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @field_validator("workspace_root")
    @classmethod
    def resolve_workspace(cls, value: Path) -> Path:
        return value.resolve()

    @field_validator("feishu_allowed_open_ids", "feishu_allowed_chat_ids", mode="before")
    @classmethod
    def parse_csv_list(cls, value: list[str] | str) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value
