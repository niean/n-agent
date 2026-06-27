from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from app.domain.memory import MemoryStore
from app.domain.session import (
    ConversationSession,
    SessionNotFoundError,
    SessionValidationError,
    TitleGenerator,
)


if TYPE_CHECKING:
    from app.application.external_memory_manager import ExternalMemoryManager


logger = logging.getLogger(__name__)


SessionDeletedHandler = Callable[[str], Awaitable[object]]


class SessionService:
    def __init__(
        self,
        memory_store: MemoryStore,
        title_generator: TitleGenerator | None = None,
        on_session_deleted: SessionDeletedHandler | None = None,
        external_memory_manager: "ExternalMemoryManager | None" = None,
    ):
        self.memory_store = memory_store
        self.title_generator = title_generator
        self.on_session_deleted = on_session_deleted
        self.external_memory_manager = external_memory_manager

    async def create_session(self, session_id: str, source: str = "dashboard") -> ConversationSession:
        existing = await self.memory_store.get_session(session_id)
        is_new = existing is None
        session = ConversationSession(id=session_id, source=source)
        created = await self.memory_store.create_session(session)
        if is_new and self.external_memory_manager is not None:
            try:
                self.external_memory_manager.on_session_switch(session_id)
            except Exception as exc:
                logger.warning(
                    "memory provider session-switch hook failed for %s: %s",
                    session_id,
                    exc,
                )
        return created

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

    async def rename_session(self, session_id: str, title: str) -> ConversationSession:
        if title is None:
            raise SessionValidationError("title is required")
        cleaned = title.strip()
        if not cleaned:
            raise SessionValidationError("title must not be blank")
        cleaned = cleaned[:60]
        session = await self.memory_store.get_session(session_id)
        if session is None:
            raise SessionNotFoundError(session_id)
        await self.memory_store.update_session_title(session_id, cleaned)
        refreshed = await self.memory_store.get_session(session_id)
        return refreshed if refreshed is not None else session

    async def delete_session(self, session_id: str) -> None:
        existing = await self.memory_store.get_session(session_id)
        if existing is None:
            raise SessionNotFoundError(session_id)
        if self.external_memory_manager is not None:
            try:
                self.external_memory_manager.on_session_end(session_id)
            except Exception as exc:
                logger.warning(
                    "memory provider session-end hook failed for %s: %s",
                    session_id,
                    exc,
                )
        deleted = await self.memory_store.delete_session(session_id)
        if not deleted:
            raise SessionNotFoundError(session_id)
        if self.on_session_deleted is not None:
            await self.on_session_deleted(session_id)

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
