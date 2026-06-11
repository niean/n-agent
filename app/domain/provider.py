from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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
