from __future__ import annotations

from app.domain.provider import LLMProvider, ModelInfo


class ModelService:
    def __init__(self, provider: LLMProvider, default_model: str):
        self.provider = provider
        self.default_model = default_model

    async def list_models(self) -> list[ModelInfo]:
        try:
            models = await self.provider.list_models()
        except Exception:
            models = []
        return models or [
            ModelInfo(
                id=self.default_model,
                display_name=self.default_model,
                provider="configured",
                supports_tools=True,
                supports_streaming=True,
            )
        ]
