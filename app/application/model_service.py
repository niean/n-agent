from __future__ import annotations

from typing import Callable

from app.domain.provider import LLMProvider, ModelInfo, resolve_model


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

    def resolve_model(self, model: str | None) -> str:
        """Compat: resolve placeholder model to active default.

        Business logic has moved to LLMPolicy (app.domain.llm_policy).
        This thin wrapper delegates to the domain resolve_model function
        for callers that have not yet been migrated to LLMPolicy.
        """
        return resolve_model(model, self.default_model)

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
