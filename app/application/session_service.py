from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.domain.memory import MemoryStore
from app.domain.session import (
    ConversationSession,
    SessionNotFoundError,
    SessionSource,
    SessionValidationError,
    TitleGenerator,
)


if TYPE_CHECKING:
    from app.application.external_memory_manager import ExternalMemoryManager


logger = logging.getLogger(__name__)


# A session-deleted handler may be sync (e.g. cleanup_session on in-memory
# registries) or async (e.g. SandboxManager.release). Both must be callable.
SessionDeletedHandler = Callable[[str], Any]


@runtime_checkable
class HookDispatcherProtocol(Protocol):
    """Duck-typed dispatcher for plugin lifecycle hooks.

    PluginService implements this protocol. Kept as a Protocol to avoid
    importing PluginService into the Application layer (circular import / DDD).
    """

    async def invoke_hook(self, hook_name: str, **kwargs: Any) -> list[Any]:
        ...


class SessionService:
    def __init__(
        self,
        memory_store: MemoryStore,
        title_generator: TitleGenerator | None = None,
        on_session_deleted: SessionDeletedHandler | None = None,
        on_session_deleted_handlers: list[SessionDeletedHandler] | None = None,
        external_memory_manager: "ExternalMemoryManager | None" = None,
        hook_dispatcher: HookDispatcherProtocol | None = None,
    ):
        self.memory_store = memory_store
        self.title_generator = title_generator
        self.external_memory_manager = external_memory_manager
        self._hook_dispatcher = hook_dispatcher
        self._on_session_deleted_handlers: list[SessionDeletedHandler] = list(
            on_session_deleted_handlers or []
        )
        # Backward-compat: legacy single-handler assignment still works via the
        # on_session_deleted property (setter appends to the handler list).
        if on_session_deleted is not None:
            self._on_session_deleted_handlers.append(on_session_deleted)

    @property
    def on_session_deleted(self) -> SessionDeletedHandler | None:
        # Return the first handler if any (legacy callers may read this);
        # None when no handlers are registered.
        return self._on_session_deleted_handlers[0] if self._on_session_deleted_handlers else None

    @on_session_deleted.setter
    def on_session_deleted(self, handler: SessionDeletedHandler | None) -> None:
        # Legacy single-handler assignment REPLACES the list (old behavior).
        # New code should use add_session_deleted_handler to append.
        if handler is None:
            self._on_session_deleted_handlers = []
        else:
            self._on_session_deleted_handlers = [handler]

    def add_session_deleted_handler(self, handler: SessionDeletedHandler) -> None:
        self._on_session_deleted_handlers.append(handler)

    async def create_session(self, session_id: str, source: str = SessionSource.DASHBOARD.value) -> ConversationSession:
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
        # T10: on_session_start -- only for new sessions, after persist success.
        # source uses the created session's source.
        if is_new and self._hook_dispatcher is not None:
            try:
                await self._hook_dispatcher.invoke_hook(
                    "on_session_start",
                    session_id=session_id,
                    source=created.source,
                )
            except Exception:
                logger.warning(
                    "on_session_start hook failed for %s",
                    session_id,
                    exc_info=True,
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
        # T10: save existing.source before delete for on_session_end dispatch.
        existing_source = existing.source
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
        # T10: on_session_end -- after persist-delete success, using pre-delete source.
        if self._hook_dispatcher is not None:
            try:
                await self._hook_dispatcher.invoke_hook(
                    "on_session_end",
                    session_id=session_id,
                    source=existing_source,
                )
            except Exception:
                logger.warning(
                    "on_session_end hook failed for %s",
                    session_id,
                    exc_info=True,
                )
        await self._invoke_session_deleted_handlers(session_id)

    async def _invoke_session_deleted_handlers(self, session_id: str) -> None:
        # Handlers may be sync or async; invoke in registration order.
        # A failure in one handler does not block subsequent handlers.
        for handler in list(self._on_session_deleted_handlers):
            try:
                result = handler(session_id)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning(
                    "session-deleted handler failed for %s", session_id, exc_info=True,
                )

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
