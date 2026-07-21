from __future__ import annotations

import asyncio
import copy
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from app.domain.memory import MemoryStore
from app.domain.session import (
    ConversationMessage,
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

    # ------------------------------------------------------------------
    # Task UI system messages (command records + lifecycle)
    # ------------------------------------------------------------------

    async def append_task_command_message(
        self, session_id: str, content: str,
    ) -> ConversationMessage:
        """持久化 /task 命令记录与结果通知。固定 role=system、name=ui.task_command。

        HTTP 入口路径：超长内容不截断、直接抛 SessionValidationError（422），由前端在
        请求前按同一合同截断。
        """
        return await self._append_system_message(
            session_id, "ui.task_command", content, truncate=False
        )

    async def append_task_lifecycle_message(
        self, session_id: str, content: str, card: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        """持久化任务生命周期状态通知。固定 role=system、name=ui.task_lifecycle。

        由 TaskRunService/TaskService 在状态 CAS 成功后 best-effort 调用，不走 HTTP。
        服务端构造的正文按 UTF-8 安全截断，不因超长丢失生命周期信号。
        card 为可选结构化交互载荷，经 _normalize_card 复制并截断 summary 后持久化。
        """
        return await self._append_system_message(
            session_id, "ui.task_lifecycle", content, truncate=True, card=card,
        )

    async def append_task_result_message(
        self, session_id: str, content: str,
    ) -> ConversationMessage:
        """持久化任务最终结果。固定 role=system、name=ui.task_result。

        所有任务结束情况（SUCCEEDED/FAILED/CANCELLED/EXPIRED）均由 TaskRunService
        best-effort 调用，最终结果以普通消息形式渲染、打印在 Chat 框（可见结果）；
        与之并存的是 ui.task_lifecycle 任务状态卡片（状态通知，折叠）。role=system 使其
        被 ContextService 排除出模型候选，不污染上下文。服务端构造的正文按 UTF-8 安全截断。
        """
        return await self._append_system_message(
            session_id, "ui.task_result", content, truncate=True
        )

    async def _append_system_message(
        self, session_id: str, name: str, content: str, *, truncate: bool,
        card: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        if not isinstance(content, str):
            raise SessionValidationError("content must be a string")
        cleaned = content.strip()
        if not cleaned:
            raise SessionValidationError("content must not be blank")
        if truncate:
            cleaned = _truncate_task_message_utf8(cleaned)
        elif len(cleaned.encode("utf-8")) > _TASK_MESSAGE_MAX_BYTES:
            raise SessionValidationError("content too large")
        normalized_card = self._normalize_card(card)
        message = ConversationMessage(role="system", name=name, content=cleaned, card=normalized_card)
        result = await self.memory_store.append_message_if_session_exists(session_id, message)
        if result is None:
            raise SessionNotFoundError(session_id)
        return result

    @staticmethod
    def _normalize_card(card: dict[str, Any] | None) -> dict[str, Any] | None:
        """Copy caller dict (deep) and truncate summary to the message byte cap; never mutate input."""
        if card is None:
            return None
        normalized = copy.deepcopy(card)
        summary = normalized.get("summary")
        if isinstance(summary, str):
            normalized["summary"] = _truncate_task_message_utf8(summary)
        return normalized


# Task UI 通知正文长度上限与截断后缀。命令侧在 HTTP 入口前由前端按同一合同截断；
# 生命周期侧由服务端截断（TaskRunService/TaskService 构造正文后经 SessionService 写入）。
_TASK_MESSAGE_MAX_BYTES = 65536
_TASK_MESSAGE_TRUNCATE_SUFFIX = "…[内容已截断]"


def _truncate_task_message_utf8(text: str) -> str:
    """UTF-8 字节安全截断：为后缀预留字节，按 code point 截取，strip 后不超上限。

    长度合法时原样返回（含 strip 由调用方先做；此处仅保证截断后总字节数 <= 上限）。
    """
    data = text.encode("utf-8")
    if len(data) <= _TASK_MESSAGE_MAX_BYTES:
        return text
    suffix = _TASK_MESSAGE_TRUNCATE_SUFFIX.encode("utf-8")
    budget = _TASK_MESSAGE_MAX_BYTES - len(suffix)
    if budget <= 0:
        # 后缀本身已超上限（理论不达），退化为硬截断
        return _TASK_MESSAGE_TRUNCATE_SUFFIX
    truncated = data[:budget].decode("utf-8", errors="ignore")
    return (truncated + _TASK_MESSAGE_TRUNCATE_SUFFIX).strip()
