from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Header
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.application.chat_service import ChatCompletionInput, ChatCompletionResult, ChatCompletionService
from app.application.events import ChatEvent, ChatEventType
from app.application.model_service import ModelService


EXTERNAL_MODEL_ID = "N-Agent"


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
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow")


def create_openai_router(chat_service: ChatCompletionService, model_service: ModelService) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def welcome():
        return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>N-Agent</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f6f7f9; color: #1f2937; }
    main { max-width: 760px; margin: 12vh auto; padding: 32px; background: white; border-radius: 16px; box-shadow: 0 12px 32px rgba(0,0,0,.08); }
    h1 { margin: 0 0 12px; font-size: 36px; }
    p { line-height: 1.6; }
    a { color: #1565c0; font-weight: 600; }
    code { background: #f1f5f9; padding: 2px 6px; border-radius: 5px; }
    ul { line-height: 2; }
  </style>
</head>
<body>
  <main>
    <h1>N-Agent</h1>
    <p>Welcome to N-Agent, an OpenAI-compatible local Agent service.</p>
    <ul>
      <li><a href="/chat">Chat</a></li>
      <li><a href="/health">Health</a></li>
      <li><a href="/v1/models">Models</a></li>
      <li><code>POST /v1/chat/completions</code></li>
    </ul>
  </main>
</body>
</html>
"""

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
        options = request.model_dump(exclude={"model", "messages", "stream", "tools", "tool_choice", "metadata"}, exclude_none=True)
        app_input = ChatCompletionInput(
            model=request.model,
            messages=[message.model_dump() for message in request.messages],
            stream=request.stream,
            metadata=request.metadata,
            options=options,
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
        yield f"data: {json.dumps(chunk)}\n\n"


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
