from __future__ import annotations

import asyncio
import logging

from app.domain.memory import MemoryStore
from app.domain.session import ConversationSession, TitleGenerator


logger = logging.getLogger(__name__)


class SessionService:
    def __init__(self, memory_store: MemoryStore, title_generator: TitleGenerator | None = None):
        self.memory_store = memory_store
        self.title_generator = title_generator

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

    async def ensure_title(self, session_id: str, user_message: str) -> None:
        if not self.title_generator or not user_message:
            return
        session = await self.memory_store.get_session(session_id)
        if session is None or not session.has_default_title():
            return
        asyncio.create_task(self._generate_and_save_title(session_id, user_message))

    async def _generate_and_save_title(self, session_id: str, user_message: str) -> None:
        try:
            title = (await self.title_generator.generate(user_message)).strip()
        except Exception as exc:
            logger.warning("title generation failed for %s: %s", session_id, exc)
            return
        if not title:
            return
        await self.memory_store.update_session_title(session_id, title[:60])
