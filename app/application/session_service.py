from __future__ import annotations

from app.domain.memory import MemoryStore
from app.domain.session import ConversationSession


class SessionService:
    def __init__(self, memory_store: MemoryStore):
        self.memory_store = memory_store

    async def create_session(self, session_id: str, source: str = "dashboard") -> ConversationSession:
        session = ConversationSession(id=session_id, source=source)
        return await self.memory_store.create_session(session)

    async def list_sessions(self) -> list[ConversationSession]:
        return await self.memory_store.list_sessions()

    async def get_session_detail(self, session_id: str) -> dict:
        return {
            "session": await self.memory_store.get_session(session_id),
            "messages": await self.memory_store.list_messages(session_id),
            "summary": await self.memory_store.get_summary(session_id),
            "task_state": await self.memory_store.get_task_state(session_id),
        }

    async def list_tool_calls(self, session_id: str):
        return await self.memory_store.list_tool_calls(session_id)
