from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, Callable

from app.domain.provider import (
    LLMEvent,
    LLMProvider,
    LLMResult,
    ModelInfo,
    ProviderConfig,
)


ProviderFactory = Callable[[ProviderConfig, str], LLMProvider]


class ActiveProviderHolder:
    """运行时持有 active LLMProvider 实例，支持热替换。"""

    def __init__(self, factory: ProviderFactory):
        self._factory = factory
        self._provider: LLMProvider | None = None
        self._config: ProviderConfig | None = None
        self._lock = asyncio.Lock()

    @property
    def current_model(self) -> str:
        return self._config.model if self._config else ""

    @property
    def current_config(self) -> ProviderConfig | None:
        return self._config

    async def swap(self, config: ProviderConfig, api_key: str) -> None:
        async with self._lock:
            self._config = config
            self._provider = self._factory(config, api_key or "")

    async def list_models(self) -> list[ModelInfo]:
        return await self._require().list_models()

    async def supports_tools(self, model: str) -> bool:
        return await self._require().supports_tools(model)

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        model: str,
        options: dict[str, Any],
    ) -> LLMResult | AsyncIterator[LLMEvent]:
        provider = self._require()
        target_model = model or self.current_model
        return await provider.chat(messages, tools, stream, target_model, options)

    def _require(self) -> LLMProvider:
        if self._provider is None:
            raise RuntimeError("ActiveProviderHolder has no active provider configured")
        return self._provider
