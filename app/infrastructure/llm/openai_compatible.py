from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from app.domain.provider import LLMEvent, LLMEventType, LLMResult, ModelInfo, resolve_model

_INTERNAL_OPTION_KEYS = {
    "tool_execution_context",
    "tool_exposure_policy",
    "execution_context_mode",
    "external_memory_enabled",
    "stream_event_sink",
    "_policy_snapshot",
    "force_compress",
    "max_iterations",
    "persist_messages",
    "activated_skills",
}


from app.infrastructure.llm.telemetry_hook import (
    NOOP_CHAT_ATTEMPT as _NOOP_CHAT_ATTEMPT,
)
from app.infrastructure.llm.telemetry_hook import ProviderTelemetryCallback, optional_hook


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        client: Any | None = None,
        telemetry_callback: ProviderTelemetryCallback | None = None,
    ):
        self.default_model = default_model
        self._telemetry_callback = telemetry_callback
        # SDK transparent retry is disabled DELIBERATELY and unconditionally
        # (telemetry on or off): a retry is a new LLM round-trip that spends
        # real tokens, so it must surface as its own chat attempt / its own
        # observable request rather than being silently absorbed inside the
        # SDK (spec lifecycle rules). Retrying, when wanted, belongs to the
        # caller (the agent loop), not the transport client.
        self.client = client or AsyncOpenAI(
            base_url=base_url, api_key=api_key or "not-needed", max_retries=0
        )

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

    def _begin_attempt(self, model: str, stream: bool):
        """Start one chat attempt under the telemetry callback (no-op when
        the callback is not injected)."""
        if self._telemetry_callback is None:
            return _NOOP_CHAT_ATTEMPT
        try:
            return self._telemetry_callback(model=model, stream=stream, provider="openai")
        except Exception:
            return _NOOP_CHAT_ATTEMPT

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        model: str,
        options: dict[str, Any],
    ) -> LLMResult | AsyncIterator[LLMEvent]:
        kwargs = {
            "model": resolve_model(model, self.default_model),
            "messages": messages,
            "stream": stream,
            **_provider_options(options),
        }
        if tools:
            kwargs["tools"] = tools
        if stream:
            # Ask the SDK to emit a final usage-only chunk so streaming
            # usage is observable (no per-chunk token counts needed).
            kwargs.setdefault("stream_options", {"include_usage": True})
            return self._stream(kwargs)
        attempt = self._begin_attempt(kwargs["model"], False)
        with attempt:
            optional_hook(attempt, "set_input", kwargs["messages"],
                          tools=kwargs.get("tools"), system=kwargs.get("system"))
            response = await self.client.chat.completions.create(**kwargs)
            choice = response.choices[0]
            message = choice.message
            usage = getattr(response, "usage", None)
            usage_dict = (
                usage.model_dump() if hasattr(usage, "model_dump") and usage else {}
            )
            attempt.set_finish_reasons(choice.finish_reason or "stop")
            if usage is not None:
                attempt.set_usage(
                    getattr(usage, "prompt_tokens", None),
                    getattr(usage, "completion_tokens", None),
                )
            normalized_message = _message_to_dict(message)
            optional_hook(attempt, "set_output", normalized_message)
            return LLMResult(
                message=normalized_message,
                finish_reason=choice.finish_reason or "stop",
                usage=usage_dict,
                raw=None,
            )

    async def _stream(self, kwargs: dict[str, Any]) -> AsyncIterator[LLMEvent]:
        yield LLMEvent(LLMEventType.MESSAGE_START)
        attempt = self._begin_attempt(kwargs["model"], True)
        with attempt:
            optional_hook(attempt, "set_input", kwargs["messages"],
                          tools=kwargs.get("tools"), system=kwargs.get("system"))
            try:
                stream = await self.client.chat.completions.create(**kwargs)
                async for chunk in stream:
                    # usage-only final chunk: empty choices, usage payload
                    if not chunk.choices:
                        usage = getattr(chunk, "usage", None)
                        if usage is not None:
                            attempt.set_usage(
                                getattr(usage, "prompt_tokens", None),
                                getattr(usage, "completion_tokens", None),
                            )
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    content = getattr(delta, "content", None)
                    if content:
                        optional_hook(attempt, "append_output_text", content)
                        yield LLMEvent(LLMEventType.CONTENT_DELTA, content=content)
                    tool_calls = getattr(delta, "tool_calls", None)
                    if tool_calls:
                        for tool_call in tool_calls:
                            function = _get(tool_call, "function")
                            optional_hook(attempt, "append_output_tool_delta",
                                          _get(tool_call, "index"), id=_get(tool_call, "id"),
                                          name=_get(function, "name"),
                                          arguments=_get(function, "arguments"))
                            yield LLMEvent(LLMEventType.TOOL_CALL_DELTA, tool_call=_tool_call_to_dict(tool_call))
                    if choice.finish_reason:
                        attempt.set_finish_reasons(choice.finish_reason)
                        yield LLMEvent(LLMEventType.MESSAGE_DONE, finish_reason=choice.finish_reason)
            except Exception as exc:
                error_text = str(exc)
                optional_hook(attempt, "set_error_detail", error_text)
                attempt.set_error(type(exc).__name__)
                yield LLMEvent(LLMEventType.ERROR, error=error_text)


def _provider_options(options: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in options.items() if key not in _INTERNAL_OPTION_KEYS}


def _message_to_dict(message: Any) -> dict[str, Any]:
    if isinstance(message, dict):
        return message
    result = {"role": getattr(message, "role", "assistant"), "content": getattr(message, "content", "") or ""}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = [_tool_call_to_dict(tool_call) for tool_call in tool_calls]
    return result


def _normalize_arguments(args: Any) -> Any:
    """Normalize tool_call arguments JSON string to readable UTF-8 (no \\uXXXX escapes).

    部分 LLM provider 返回的 arguments 含 \\uXXXX 转义；parse + ensure_ascii=False 重序列化
    为可读中文。best-effort：非字符串/不可解析原样返回。
    """
    if not isinstance(args, str):
        return args
    try:
        return json.dumps(json.loads(args), ensure_ascii=False)
    except (ValueError, TypeError):
        return args


def _tool_call_to_dict(tool_call: Any) -> dict[str, Any]:
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        if isinstance(function, dict) and "arguments" in function:
            normalized = dict(tool_call)
            normalized_function = dict(function)
            normalized_function["arguments"] = _normalize_arguments(function.get("arguments"))
            normalized["function"] = normalized_function
            return normalized
        return tool_call
    function = getattr(tool_call, "function", None)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", "") if function else "",
            "arguments": _normalize_arguments(getattr(function, "arguments", "") if function else ""),
        },
    }


def _get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)
