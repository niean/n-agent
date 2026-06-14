from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Protocol


class LLMEventType(str, Enum):
    MESSAGE_START = "message_start"
    CONTENT_DELTA = "content_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    MESSAGE_DONE = "message_done"
    ERROR = "error"


@dataclass(frozen=True)
class ModelInfo:
    id: str
    display_name: str
    provider: str
    supports_tools: bool = True
    supports_streaming: bool = True


@dataclass(frozen=True)
class LLMEvent:
    type: LLMEventType
    content: str = ""
    tool_call: dict[str, Any] | None = None
    finish_reason: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class LLMResult:
    message: dict[str, Any]
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


class LLMProvider(Protocol):
    async def list_models(self) -> list[ModelInfo]:
        ...

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        model: str,
        options: dict[str, Any],
    ) -> LLMResult | AsyncIterator[LLMEvent]:
        ...

    async def supports_tools(self, model: str) -> bool:
        ...


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    name: str
    provider_type: str
    base_url: str
    model: str
    api_key_present: bool
    is_active: bool
    extra_headers: dict[str, str] | None
    created_at: datetime
    updated_at: datetime


class ProviderNotFoundError(Exception):
    """Provider 不存在"""


class DuplicateProviderError(Exception):
    """Provider 名称等唯一约束冲突"""


class ProviderInUseError(Exception):
    """Provider 被运行时占用，禁止删除"""


class ProviderValidationError(Exception):
    """Provider 输入校验失败"""


class ProviderRegistry(Protocol):
    async def list_providers(self) -> list[ProviderConfig]:
        ...

    async def get_provider(self, provider_id: str) -> ProviderConfig | None:
        ...

    async def create_provider(self, config: ProviderConfig, api_key: str) -> ProviderConfig:
        ...

    async def update_provider(
        self,
        provider_id: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        provider_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        api_key: str | None = None,
        clear_api_key: bool = False,
    ) -> ProviderConfig:
        ...

    async def delete_provider(self, provider_id: str) -> None:
        ...

    async def set_active(self, provider_id: str) -> ProviderConfig:
        ...

    async def get_active(self) -> ProviderConfig | None:
        ...

    async def get_secret(self, provider_id: str) -> str | None:
        ...
