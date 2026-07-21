from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from typing import Any

from app.domain.provider import LLMEvent, LLMEventType, LLMResult, ModelInfo, resolve_model

_INTERNAL_OPTION_KEYS = {
    "tool_execution_context",
    "tool_exposure_policy",
    "execution_context_mode",
    "external_memory_enabled",
    "stream_event_sink",
    "persist_messages",
}
_ALLOWED_OPTION_KEYS = {"temperature", "top_p", "top_k", "stop_sequences", "cache_control", "thinking", "output_config"}
_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "model_context_window_exceeded": "length",
}
_OUTPUT_LIMITS = {
    "claude-opus-4-7": 32768,
    "claude-opus-4-6": 32768,
    "claude-sonnet-4-6": 32768,
    "claude-haiku-4-5": 8192,
}


class AnthropicProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        extra_headers: dict[str, str] | None = None,
        client: Any | None = None,
    ):
        self.base_url = base_url
        self.default_model = default_model
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(
                api_key=api_key or "not-needed",
                base_url=base_url or None,
                default_headers=extra_headers or None,
            )
        self.client = client

    async def list_models(self) -> list[ModelInfo]:
        try:
            models = await self.client.models.list()
            data = getattr(models, "data", models)
            return [
                ModelInfo(
                    id=getattr(model, "id"),
                    display_name=getattr(model, "display_name", getattr(model, "id")),
                    provider="anthropic",
                )
                for model in data
            ]
        except Exception:
            return [ModelInfo(self.default_model, self.default_model, "anthropic")]

    async def supports_tools(self, model: str) -> bool:
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        stream: bool,
        model: str,
        options: dict[str, Any],
    ) -> LLMResult | AsyncIterator[LLMEvent]:
        selected_model = resolve_model(model, self.default_model)
        provider_options = _provider_options(selected_model, options or {})
        system, anthropic_messages = _convert_messages(
            messages,
            cache_control=provider_options.get("cache_control"),
        )
        kwargs = {
            "model": selected_model,
            "max_tokens": _resolve_max_tokens(selected_model, options or {}),
            "messages": anthropic_messages,
            **provider_options,
        }
        if system:
            kwargs["system"] = system
        converted_tools = _convert_tools(tools)
        if converted_tools:
            kwargs["tools"] = converted_tools
        if stream:
            kwargs["stream"] = True
            return _stream_events(await self.client.messages.create(**kwargs))
        response = await self.client.messages.create(**kwargs)
        return _normalize_response(response)


def _provider_options(model: str, options: dict[str, Any]) -> dict[str, Any]:
    skip_sampling = model == "claude-opus-4-7"
    result = {"cache_control": {"type": "ephemeral"}}
    for key, value in options.items():
        if key in _INTERNAL_OPTION_KEYS or key == "max_tokens":
            continue
        if skip_sampling and key in {"temperature", "top_p", "top_k"}:
            continue
        if key in _ALLOWED_OPTION_KEYS:
            result[key] = value
    return result


def _resolve_max_tokens(model: str, options: dict[str, Any]) -> int:
    value = options.get("max_tokens")
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = 4096
    if not math.isfinite(numeric) or numeric <= 0:
        numeric = 4096
    resolved = int(numeric)
    limit = _OUTPUT_LIMITS.get(model)
    if limit is not None:
        return min(resolved, limit)
    return min(resolved, 4096)


def _convert_messages(
    messages: list[dict[str, Any]],
    cache_control: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]] | str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    awaiting_tool_results: set[str] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if content:
                system_parts.append(str(content))
            index += 1
            continue
        if role == "tool":
            raise ValueError("orphan tool message without preceding assistant tool_use")
        if awaiting_tool_results:
            raise ValueError("tool result must immediately follow assistant tool_use")
        if role == "assistant":
            assistant, tool_ids = _convert_assistant_message(message)
            converted.append(assistant)
            index += 1
            if tool_ids:
                tool_messages = []
                while index < len(messages) and messages[index].get("role") == "tool":
                    tool_messages.append(messages[index])
                    index += 1
                if not tool_messages:
                    awaiting_tool_results = set(tool_ids)
                    continue
                converted.append(_convert_tool_messages(tool_messages, set(tool_ids)))
            continue
        converted.append({"role": "user", "content": _content_to_blocks(message.get("content", ""))})
        index += 1
    if awaiting_tool_results:
        raise ValueError("tool result must immediately follow assistant tool_use")
    if not system_parts:
        system: list[dict[str, Any]] | str | None = None
    else:
        system_text = "\n".join(system_parts)
        system = (
            [{"type": "text", "text": system_text, "cache_control": cache_control}]
            if cache_control
            else system_text
        )
    if cache_control:
        for message in converted:
            content = message.get("content")
            if message.get("role") == "user" and isinstance(content, str) and content:
                message["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": cache_control,
                    }
                ]
                break
    return system, converted


def _convert_assistant_message(message: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    content = message.get("content", "")
    blocks = content if isinstance(content, list) else ([{"type": "text", "text": str(content)}] if content else [])
    tool_ids = []
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function", {})
        tool_id = tool_call.get("id", "")
        tool_ids.append(tool_id)
        blocks.append(
            {
                "type": "tool_use",
                "id": tool_id,
                "name": function.get("name", ""),
                "input": _parse_arguments(function.get("arguments")),
            }
        )
    return {"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]}, tool_ids


def _convert_tool_messages(messages: list[dict[str, Any]], expected_ids: set[str]) -> dict[str, Any]:
    blocks = []
    text_blocks = []
    for message in messages:
        tool_use_id = message.get("tool_call_id", "")
        if tool_use_id not in expected_ids:
            raise ValueError("orphan tool message without matching assistant tool_use")
        content = message.get("content", "")
        blocks.append({"type": "tool_result", "tool_use_id": tool_use_id, "content": str(content)})
        text = message.get("text")
        if text:
            text_blocks.append({"type": "text", "text": str(text)})
    return {"role": "user", "content": blocks + text_blocks}


def _content_to_blocks(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, list):
        return [_convert_content_part(part) for part in content]
    if content is None:
        return ""
    return str(content)


def _convert_content_part(part: dict[str, Any]) -> dict[str, Any]:
    part_type = part.get("type")
    if part_type == "image_url":
        return _convert_image_url_part(part)
    return part


def _convert_image_url_part(part: dict[str, Any]) -> dict[str, Any]:
    image_url = part.get("image_url") or {}
    url = image_url.get("url") if isinstance(image_url, dict) else None
    if not url:
        return part
    if url.startswith("data:"):
        media_type, data = _parse_data_url(url)
        if media_type is None:
            return part
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    if url.startswith("http://") or url.startswith("https://"):
        raise ValueError("anthropic http image_url not supported")
    return part


def _parse_data_url(url: str) -> tuple[str | None, str]:
    # 格式：data:<mime>;base64,<data>
    if not url.startswith("data:"):
        return None, ""
    comma_idx = url.find(",")
    if comma_idx < 0:
        return None, ""
    header = url[5:comma_idx]
    data = url[comma_idx + 1 :]
    if ";base64" not in header:
        return None, ""
    media_type = header.split(";")[0]
    if not media_type.startswith("image/"):
        return None, ""
    return media_type, data


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if not arguments:
        return {}
    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    seen = set()
    for tool in tools:
        function = tool.get("function", {})
        name = function.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        converted.append(
            {
                "name": name,
                "description": function.get("description", ""),
                "input_schema": _normalize_schema(function.get("parameters")),
            }
        )
    return converted


def _normalize_schema(schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}}
    schema = dict(schema)
    if any(key in schema for key in ("oneOf", "anyOf", "allOf")) and "type" not in schema:
        return {"type": "object", "properties": {}}
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null = [item for item in schema_type if item != "null"]
        schema["type"] = non_null[0] if non_null else "object"
    if schema.get("type") == "object" and "properties" not in schema:
        schema["properties"] = {}
    return schema


def _normalize_response(response: Any) -> LLMResult:
    text_parts = []
    tool_calls = []
    for block in getattr(response, "content", []) or []:
        block_type = _get(block, "type")
        if block_type == "text":
            text_parts.append(_get(block, "text") or "")
        elif block_type == "tool_use":
            tool_calls.append(
                {
                    "id": _get(block, "id"),
                    "type": "function",
                    "function": {
                        "name": _get(block, "name"),
                        "arguments": json.dumps(_get(block, "input") or {}),
                    },
                }
            )
    message = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return LLMResult(
        message=message,
        finish_reason=_FINISH_REASON_MAP.get(getattr(response, "stop_reason", None), getattr(response, "stop_reason", None) or "stop"),
        usage=_usage_to_dict(getattr(response, "usage", None)),
        raw=response,
    )


def _usage_to_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump(exclude_none=True)
    if isinstance(usage, dict):
        return dict(usage)
    return {key: value for key, value in vars(usage).items() if value is not None}


async def _stream_events(stream: AsyncIterator[Any]) -> AsyncIterator[LLMEvent]:
    yield LLMEvent(LLMEventType.MESSAGE_START)
    current_tool: dict[str, Any] | None = None
    try:
        async for event in stream:
            event_type = _get(event, "type")
            if event_type == "content_block_start":
                block = _get(event, "content_block")
                if _get(block, "type") == "tool_use":
                    current_tool = {"id": _get(block, "id"), "name": _get(block, "name"), "arguments": ""}
                    yield LLMEvent(LLMEventType.TOOL_CALL_DELTA, tool_call=dict(current_tool))
            elif event_type == "content_block_delta":
                delta = _get(event, "delta")
                delta_type = _get(delta, "type")
                if delta_type == "text_delta":
                    yield LLMEvent(LLMEventType.CONTENT_DELTA, content=_get(delta, "text") or "")
                elif delta_type == "input_json_delta":
                    if current_tool is None:
                        current_tool = {"id": None, "name": None, "arguments": ""}
                    current_tool["arguments"] += _get(delta, "partial_json") or ""
                    yield LLMEvent(LLMEventType.TOOL_CALL_DELTA, tool_call=dict(current_tool))
            elif event_type == "message_delta":
                delta = _get(event, "delta")
                yield LLMEvent(LLMEventType.MESSAGE_DONE, finish_reason=_FINISH_REASON_MAP.get(_get(delta, "stop_reason"), _get(delta, "stop_reason")))
        return
    except Exception as exc:
        yield LLMEvent(LLMEventType.ERROR, error=str(exc))


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)
