"""ACP session bridge -- wraps SessionService + MemoryStore for ACP session ops.

The ACP agent (T12) constructs an :class:`ACPSessionBridge` once per ACP agent
process and delegates ``session/new``, ``session/load``, ``session/list``,
``session/fork`` and ``session/close`` requests to it. ``create`` persists
``source="acp"`` plus ACP metadata (cwd, host_cwd, mode, model, config_options,
allowed_confirm_tools); ``close`` only releases runtime state callbacks and
does NOT delete SQLite history (so ``session/load`` and ``session/list`` keep
working after a session is closed).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.application.session_service import SessionService
from app.domain.memory import MemoryStore
from app.domain.session import ConversationSession

CleanupCallback = Callable[[str], Any]
ACP_SESSION_ID_PREFIX = "acp-"


def new_acp_session_id() -> str:
    return f"{ACP_SESSION_ID_PREFIX}{uuid4()}"


class ACPSessionBridge:
    """ACP-specific session operations over SessionService + MemoryStore."""

    def __init__(
        self,
        session_service: SessionService,
        memory_store: MemoryStore,
    ) -> None:
        self.session_service = session_service
        self.memory_store = memory_store

    async def create(
        self,
        session_id: str,
        cwd: str,
        host_cwd: str | None = None,
        mode: str = "default",
        model: str | None = None,
        config_options: dict[str, Any] | None = None,
        allowed_confirm_tools: dict[str, str] | None = None,
    ) -> ConversationSession:
        session = await self.session_service.create_session(session_id, source="acp")
        metadata: dict[str, Any] = {
            "cwd": cwd,
            "mode": mode,
            "config_options": config_options or {},
            "allowed_confirm_tools": allowed_confirm_tools or {},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if host_cwd is not None:
            metadata["host_cwd"] = host_cwd
        if model is not None:
            metadata["model"] = model
        await self.memory_store.update_session_acp_metadata(session_id, metadata)
        refreshed = await self.memory_store.get_session(session_id)
        return refreshed if refreshed is not None else session

    async def load(self, session_id: str) -> ConversationSession | None:
        session = await self.memory_store.get_session(session_id)
        if session is None or session.source != "acp":
            return None
        return session

    async def resume(
        self,
        session_id: str,
        cwd: str,
        host_cwd: str | None = None,
        mode: str = "default",
        model: str | None = None,
        config_options: dict[str, Any] | None = None,
        allowed_confirm_tools: dict[str, str] | None = None,
    ) -> ConversationSession | None:
        existing = await self.memory_store.get_session(session_id)
        if existing is not None:
            if existing.source == "acp":
                return existing
            return None
        return await self.create(
            session_id,
            cwd=cwd,
            host_cwd=host_cwd,
            mode=mode,
            model=model,
            config_options=config_options,
            allowed_confirm_tools=allowed_confirm_tools,
        )

    async def list(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[ConversationSession], str | None]:
        return await self.memory_store.list_sessions_by_source(
            "acp", cwd=cwd, cursor=cursor, limit=limit,
        )

    async def fork(
        self,
        source_session_id: str,
        target_session_id: str | None = None,
    ) -> str | None:
        source = await self.memory_store.get_session(source_session_id)
        if source is None or source.source != "acp":
            return None
        if target_session_id is None:
            target_session_id = new_acp_session_id()
        await self.memory_store.clone_session(source_session_id, target_session_id)
        return target_session_id

    async def close(
        self,
        session_id: str,
        cleanup_callback: CleanupCallback | None = None,
    ) -> None:
        # ACP close only releases runtime state; SQLite history is preserved so
        # session/load and session/list continue to work after close.
        if cleanup_callback is None:
            return
        result = cleanup_callback(session_id)
        if inspect.isawaitable(result):
            await result
