from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from app.domain.provider import LLMEvent, LLMEventType, LLMResult, ModelInfo


class OpenAICompatibleProvider:
    def __init__(self, base_url: str, api_key: str, default_model: str, client: Any | None = None):
        self.default_model = default_model
        self.client = client or AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed")

    async def list_models(self) -> list[ModelInfo]:
        try:
            response = await self.client.models.list()
            models = getattr(response, "data", response)
            return [
                ModelInfo(
                    id=getattr(model, "id", str(model)),
                    display_name=getattr(model, "id", str(model)),
                    provider="openai-compatible",
                    supports_tools=True,
                    supports_streaming=True,
                )
                for model in models
            ]
        except Exception:
            return [
                ModelInfo(
                    id=self.default_model,
                    display_name=self.default_model,
                    provider="openai-compatible",
                    supports_tools=True,
                    supports_streaming=True,
                )
            ]

    async def supports_tools(self, model: str) -> bool:
        models = await self.list_models()
        for info in models:
            if info.id == model:
                return info.supports_tools
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        model: str,
        options: dict[str, Any],
    ) -> LLMResult | AsyncIterator[LLMEvent]:
        kwargs = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": stream,
            **_provider_options(options),
        }
        if tools:
            kwargs["tools"] = tools
        if stream:
            return self._stream(kwargs)
        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        message = choice.message
        return LLMResult(
            message=_message_to_dict(message),
            finish_reason=choice.finish_reason or "stop",
            usage=getattr(response, "usage", None).model_dump() if getattr(response, "usage", None) else {},
            raw=None,
        )

    async def _stream(self, kwargs: dict[str, Any]) -> AsyncIterator[LLMEvent]:
        yield LLMEvent(LLMEventType.MESSAGE_START)
        try:
            stream = await self.client.chat.completions.create(**kwargs)
            async for chunk in stream:
                choice = chunk.choices[0]
                delta = choice.delta
                content = getattr(delta, "content", None)
                if content:
                    yield LLMEvent(LLMEventType.CONTENT_DELTA, content=content)
                tool_calls = getattr(delta, "tool_calls", None)
                if tool_calls:
                    for tool_call in tool_calls:
                        yield LLMEvent(LLMEventType.TOOL_CALL_DELTA, tool_call=_tool_call_to_dict(tool_call))
                if choice.finish_reason:
                    yield LLMEvent(LLMEventType.MESSAGE_DONE, finish_reason=choice.finish_reason)
        except Exception as exc:
            yield LLMEvent(LLMEventType.ERROR, error=str(exc))


def _provider_options(options: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in options.items()
        if key not in {"tool_execution_context", "tool_exposure_policy", "execution_context_mode"}
    }


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    result = {"role": getattr(message, "role", "assistant"), "content": getattr(message, "content", "") or ""}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = [_tool_call_to_dict(tool_call) for tool_call in tool_calls]
    return result


def _tool_call_to_dict(tool_call: Any) -> dict[str, Any]:
    if isinstance(tool_call, dict):
        return tool_call
    function = getattr(tool_call, "function", None)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", "") if function else "",
            "arguments": getattr(function, "arguments", "") if function else "",
        },
    }
