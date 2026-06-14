from __future__ import annotations

from typing import Callable

from app.domain.provider import LLMProvider, ModelInfo


ModelResolver = str | Callable[[], str]


class ModelService:
    def __init__(self, provider: LLMProvider, default_model: ModelResolver):
        self.provider = provider
        self._default_model = default_model

    @property
    def default_model(self) -> str:
        if callable(self._default_model):
            return self._default_model() or ""
        return self._default_model or ""

    async def list_models(self) -> list[ModelInfo]:
        try:
            models = await self.provider.list_models()
        except Exception:
            models = []
        default = self.default_model
        return models or [
            ModelInfo(
                id=default,
                display_name=default,
                provider="configured",
                supports_tools=True,
                supports_streaming=True,
            )
        ]
