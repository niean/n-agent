from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.provider import LLMProvider, LLMResult
from app.domain.tool import ToolCallRequest, ToolExecutionContext, ToolResult, ToolResultStatus
from app.utils.content_utils import validate_image_url


class VisionAnalyzeToolExecutor:
    def __init__(
        self,
        provider: LLMProvider,
        vision_capability: Callable[[], bool],
        current_model: Callable[[], str],
    ):
        self.provider = provider
        self.vision_capability = vision_capability
        self.current_model = current_model

    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        image_url = request.arguments.get("image_url")
        question = request.arguments.get("question") or "describe this image"
        try:
            validated_url = validate_image_url(image_url) if isinstance(image_url, str) else ""
        except ValueError:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "invalid_image_url"})
        if not validated_url:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "invalid_image_url"})

        if not self.vision_capability():
            return ToolResult(
                request.id,
                request.name,
                ToolResultStatus.ERROR,
                {"error": "active provider does not support vision"},
            )

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {"type": "image_url", "image_url": {"url": validated_url}},
                ],
            }
        ]
        try:
            result = await self.provider.chat(
                messages,
                [],
                False,
                self.current_model(),
                {},
            )
        except Exception as exc:
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": str(exc)})

        if not isinstance(result, LLMResult):
            return ToolResult(request.id, request.name, ToolResultStatus.ERROR, {"error": "provider returned non-LLMResult"})

        content = result.message.get("content", "") if isinstance(result.message, dict) else ""
        return ToolResult(
            request.id,
            request.name,
            ToolResultStatus.SUCCESS,
            {"answer": content},
        )


__all__ = ["VisionAnalyzeToolExecutor"]
