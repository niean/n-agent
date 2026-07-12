from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult, ChatCompletionService
from app.application.events import ChatEvent, ChatEventType
from app.application.model_service import ModelService
from app.domain.provider import resolve_model
from app.utils.content_utils import has_image_part, normalize_content


EXTERNAL_MODEL_ID = "N-Agent"
# 客户端可能用 /v1/models 广告的占位 id（EXTERNAL_MODEL_ID）或空 model 字段发请求；
# 这些值不是后端 provider 的真实模型 id，需要回退到 ModelService.default_model。
# 占位 id 集合与判定逻辑见 app.domain.provider（与所有 provider 共享，避免透传被拒）。


class ChatMessage(BaseModel):
    role: str
    content: Any = ""

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = True
    tools: list[dict[str, Any]] = Field(default_factory=list)
    tool_choice: Any = None
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


def create_openai_compatible_router(chat_service: ChatCompletionService, model_service: ModelService) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health():
        return {"status": "ok"}

    @router.get("/v1/models")
    async def models():
        infos = await model_service.list_models()
        return {
            "object": "list",
            "data": [
                {
                    "id": EXTERNAL_MODEL_ID,
                    "object": "model",
                    "created": 0,
                    "owned_by": "n-agent",
                }
                for _ in infos
            ],
        }

    @router.post("/v1/chat/completions")
    async def chat_completions(request: ChatCompletionRequest, x_session_id: str | None = Header(default=None)):
        resolved_model = resolve_model(request.model, model_service.default_model)
        normalized_messages: list[dict[str, Any]] = []
        for message in request.messages:
            raw = message.model_dump()
            role = raw.get("role")
            content = raw.get("content", "")
            if role == "user":
                try:
                    raw["content"] = normalize_content(content)
                except ValueError as exc:
                    code = str(exc) or "unsupported_content_type"
                    return JSONResponse(
                        status_code=400,
                        content={"error": {"code": code, "message": code}},
                    )
            elif role in ("system", "tool"):
                if has_image_part(content):
                    return JSONResponse(
                        status_code=400,
                        content={"error": {"code": "unsupported_content_type", "message": "unsupported_content_type"}},
                    )
            normalized_messages.append(raw)
        app_input = ChatCompletionInput(
            model=resolved_model,
            messages=normalized_messages,
            stream=request.stream,
            metadata=request.metadata,
            options=_merge_generation_params(request),
            session_id=x_session_id,
        )
        result = await chat_service.complete(app_input)
        if request.stream:
            return StreamingResponse(
                _sse(result, EXTERNAL_MODEL_ID),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
        assert isinstance(result, ChatCompletionResult)
        if result.finish_reason == "error":
            return JSONResponse(
                status_code=500,
                content={"error": {"message": result.message.get("content", "provider failure"), "type": "server_error"}},
            )
        return _completion_response(result)

    return router


def _merge_generation_params(request: ChatCompletionRequest) -> dict[str, Any]:
    """Merge top-level generation params (temperature/max_tokens/top_p) into
    the caller-provided options dict. Caller-provided options take precedence
    so advanced callers can override per-call."""
    merged: dict[str, Any] = {}
    if request.temperature is not None:
        merged["temperature"] = request.temperature
    if request.max_tokens is not None:
        merged["max_tokens"] = request.max_tokens
    if request.top_p is not None:
        merged["top_p"] = request.top_p
    merged.update(request.options)
    return merged


def _completion_response(result: ChatCompletionResult) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": EXTERNAL_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": result.message,
                "finish_reason": result.finish_reason,
            }
        ],
        "usage": result.usage,
    }


async def _sse(events: AsyncIterator[ChatEvent], model: str) -> AsyncIterator[str]:
    async for event in events:
        if event.type is ChatEventType.DONE:
            yield "data: [DONE]\n\n"
            continue
        chunk = _chunk_for_event(event, model)
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def _chunk_for_event(event: ChatEvent, model: str) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    finish_reason = None
    if event.type is ChatEventType.MESSAGE_START:
        delta["role"] = "assistant"
    elif event.type is ChatEventType.CONTENT_DELTA:
        delta["content"] = event.content
    elif event.type is ChatEventType.TOOL_CALL_DELTA:
        delta["tool_calls"] = [event.tool_call]
    elif event.type is ChatEventType.ERROR:
        delta["content"] = event.error or "error"
        finish_reason = "error"
    elif event.type is ChatEventType.MESSAGE_DONE:
        finish_reason = event.finish_reason or "stop"
    return {
        "id": f"chatcmpl-{uuid4()}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
