from __future__ import annotations

import inspect

from app.domain.memory import MemoryStore
from app.domain.session import ConversationSession, Summary, TaskState, ToolCall, ConversationMessage


class _StubMemoryStore:
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
        self, session_id: str, metadata: dict[str, object],
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


def test_memory_store_declares_update_session_acp_metadata():
    assert hasattr(MemoryStore, "update_session_acp_metadata")
    sig = inspect.signature(MemoryStore.update_session_acp_metadata)
    params = list(sig.parameters)
    assert params == ["self", "session_id", "metadata"]


def test_memory_store_declares_list_sessions_by_source():
    assert hasattr(MemoryStore, "list_sessions_by_source")
    sig = inspect.signature(MemoryStore.list_sessions_by_source)
    params = list(sig.parameters)
    assert params == ["self", "source", "cwd", "cursor", "limit"]
    assert sig.parameters["cwd"].default is None
    assert sig.parameters["cursor"].default is None
    assert sig.parameters["limit"].default == 50


def test_memory_store_declares_clone_session():
    assert hasattr(MemoryStore, "clone_session")
    sig = inspect.signature(MemoryStore.clone_session)
    params = list(sig.parameters)
    assert params == ["self", "source_session_id", "target_session_id"]


def test_stub_implements_new_methods():
    stub = _StubMemoryStore()
    assert callable(stub.update_session_acp_metadata)
    assert callable(stub.list_sessions_by_source)
    assert callable(stub.clone_session)
