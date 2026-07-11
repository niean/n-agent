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

    async def update_session_acp_metadata(
        self, session_id: str, metadata: dict[str, Any],
    ) -> None:
        ...

    async def list_sessions_by_source(
        self, source: str, cwd: str | None = None, cursor: str | None = None, limit: int = 50,
    ) -> tuple[list[ConversationSession], str | None]:
        ...

    async def clone_session(
        self, source_session_id: str, target_session_id: str,
    ) -> None:
        ...

    async def delete_summary_messages(self, session_id: str) -> int:
        """删除指定 session 的所有 is_summary=1 消息，返回删除行数。"""

    async def append_summary_message(
        self, session_id: str, message: ConversationMessage,
    ) -> ConversationMessage:
        """追加摘要消息(is_summary=1)到 messages 表，不删除旧摘要。

        保留所有摘要记录供 Dashboard 渲染历史摘要；上下文构建时仅取最新一条。
        """

    async def mark_messages_summarized(self, session_id: str, message_ids: list[str]) -> int:
        """将指定消息标记为 is_summarized=1（已被摘要吸收）。

        压缩成功后调用：middle 段（被摘要的原始消息）标记为 is_summarized=1，
        load_context 时过滤掉这些消息，避免 middle + summary 冗余。
        返回实际更新的行数。
        """


class Summarizer(Protocol):
    async def summarize(self, messages: list[dict[str, Any]], existing_summary: str = "") -> str:
        ...
