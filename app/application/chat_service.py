from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.application.agent_graph import AgentGraphRunner
from app.application.events import ChatEvent
from app.domain.agent import AgentState
from app.domain.memory import MemoryStore
from app.domain.session import ConversationMessage, ConversationSession


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
    def __init__(self, memory_store: MemoryStore, graph_runner: AgentGraphRunner):
        self.memory_store = memory_store
        self.graph_runner = graph_runner

    async def complete(self, request: ChatCompletionInput) -> ChatCompletionResult | AsyncIterator[ChatEvent]:
        session_id = request.session_id or request.metadata.get("session_id") or f"tmp-{uuid4()}"
        await self.memory_store.create_session(ConversationSession(id=session_id, source="api"))
        for message in request.messages:
            if message.get("role") == "user":
                await self.memory_store.append_message(
                    session_id,
                    ConversationMessage(role="user", content=message.get("content", "")),
                )
        state = AgentState(session_id=session_id, input_messages=request.messages)
        if request.stream:
            return self.graph_runner.stream_events(state, request.model, request.options)
        final_state = await self.graph_runner.run(state, request.model, request.options)
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
