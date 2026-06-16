from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEvent
from app.application.session_service import SessionService
from app.domain.agent import AgentState
from app.domain.memory import MemoryStore
from app.domain.session import ConversationMessage, ConversationSession
from app.domain.tool import ToolExecutionContext


@dataclass(frozen=True)
class ChatCompletionInput:
    model: str
    messages: list[dict[str, Any]]
    stream: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None


@dataclass(frozen=True)
class ChatCompletionResult:
    session_id: str
    model: str
    message: dict[str, Any]
    finish_reason: str = "stop"
    usage: dict[str, Any] = field(default_factory=dict)


class ChatCompletionService:
    def __init__(
        self,
        memory_store: MemoryStore,
        graph_runner: AgentGraphRunner,
        session_service: SessionService,
    ):
        self.memory_store = memory_store
        self.graph_runner = graph_runner
        self.session_service = session_service

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult | AsyncIterator[ChatEvent]:
        session_id = request.session_id or request.metadata.get("session_id") or f"tmp-{uuid4()}"
        await self.memory_store.create_session(ConversationSession(id=session_id, source="api"))
        first_user_message = next(
            (message.get("content", "") for message in request.messages if message.get("role") == "user"),
            "",
        )
        for message in request.messages:
            if message.get("role") == "user":
                await self.memory_store.append_message(
                    session_id,
                    ConversationMessage(role="user", content=message.get("content", "")),
                )
        await self.session_service.ensure_title(session_id, str(first_user_message))
        state = AgentState(session_id=session_id, input_messages=request.messages)
        options = dict(request.options)
        if options.get("execution_context_mode") == "unattended":
            options["tool_exposure_policy"] = "safe_only"
        else:
            context = _mcp_tool_execution_context(str(first_user_message))
            if context.allowed_confirm_tools:
                options["tool_execution_context"] = context
        if request.stream:
            return self.graph_runner.stream_events(state, request.model, options)
        final_state = await self.graph_runner.run(state, request.model, options)
        if final_state.error:
            return ChatCompletionResult(
                session_id=session_id,
                model=request.model,
                message={"role": "assistant", "content": final_state.error},
                finish_reason="error",
            )
        return ChatCompletionResult(
            session_id=session_id,
            model=request.model,
            message=final_state.final_message or {"role": "assistant", "content": ""},
            finish_reason=final_state.finish_reason or "stop",
        )


def _mcp_tool_execution_context(user_message: str) -> ToolExecutionContext:
    text = user_message.strip()
    lowered = text.lower()
    allowed: dict[str, dict[str, object]] = {}
    url = _extract_url(text)
    transport_type = _extract_transport_type(lowered)
    if url and any(word in lowered for word in ("mcp", "站点", "site")):
        if any(word in lowered for word in ("探测", "probe", "检测")):
            expected: dict[str, object] = {"url": url}
            if transport_type:
                expected["transport_type"] = transport_type
            allowed["mcp_site_probe"] = expected
        if any(word in lowered for word in ("添加", "新增", "add", "保存")):
            expected = {"url": url}
            if transport_type:
                expected["transport_type"] = transport_type
            allowed["mcp_site_add"] = expected
    if any(word in lowered for word in ("刷新", "refresh")) and any(word in lowered for word in ("mcp", "站点", "site")):
        refresh_target = _extract_key_value(text, "site_id") or _extract_key_value(text, "name")
        if refresh_target:
            key = "site_id" if "site_id" in text else "name"
            allowed["mcp_site_refresh"] = {key: refresh_target}
    return ToolExecutionContext(allowed_confirm_tools=allowed)


def _extract_url(text: str) -> str | None:
    for token in text.replace("，", " ").replace("。", " ").split():
        if token.startswith(("http://", "https://")):
            return token.rstrip(",.;，。")
    return None


def _extract_key_value(text: str, key: str) -> str | None:
    for token in text.replace("，", " ").replace("。", " ").split():
        if token.startswith(f"{key}="):
            return token.split("=", 1)[1].strip().rstrip(",.;，。")
        if token.startswith(f"{key}:"):
            return token.split(":", 1)[1].strip().rstrip(",.;，。")
    return None


def _extract_transport_type(text: str) -> str | None:
    if "streamable_http" in text or "streamable http" in text:
        return "streamable_http"
    if "sse" in text:
        return "sse"
    return None
