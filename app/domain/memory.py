from __future__ import annotations

from typing import Any, Protocol

from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall


class MemoryStore(Protocol):
    async def create_session(self, session: ConversationSession) -> ConversationSession:
        ...

    async def get_session(self, session_id: str) -> ConversationSession | None:
        ...

    async def list_sessions(self) -> list[ConversationSession]:
        ...

    async def update_session_title(self, session_id: str, title: str) -> None:
        ...

    async def lock_session_external_memory(
        self, session_id: str, enabled: list[str], slots: dict[str, str] | None = None,
    ) -> list[str]:
        ...

    async def delete_session(self, session_id: str) -> bool:
        ...

    async def append_message(self, session_id: str, message: ConversationMessage) -> ConversationMessage:
        ...

    async def list_messages(self, session_id: str) -> list[ConversationMessage]:
        ...

    async def save_tool_call(self, tool_call: ToolCall) -> ToolCall:
        ...

    async def list_tool_calls(self, session_id: str) -> list[ToolCall]:
        ...

    async def list_recent_tool_calls(
        self, tool_name: str | None = None, limit: int = 50,
    ) -> list[ToolCall]:
        ...

    async def delete_tool_call(self, tool_call_id: str) -> bool:
        ...

    async def save_task_state(self, task_state: TaskState) -> TaskState:
        ...

    async def get_task_state(self, session_id: str) -> TaskState | None:
        ...

    async def save_summary(self, summary: Summary) -> Summary:
        ...

    async def get_summary(self, session_id: str) -> Summary | None:
        ...


class Summarizer(Protocol):
    async def summarize(self, messages: list[dict[str, Any]], existing_summary: str = "") -> str:
        ...
