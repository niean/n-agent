"""T10: SessionService lifecycle hook dispatch tests (S5).

Covers on_session_start and on_session_end hook dispatch sites in
SessionService.create_session and SessionService.delete_session.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.application.session_service import SessionService
from app.domain.session import (
    ConversationSession,
    SessionNotFoundError,
    SessionSource,
)
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


class FakeHookDispatcher:
    """Records every invoke_hook call."""

    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def invoke_hook(self, hook_name: str, **kwargs: Any) -> list[Any]:
        self.calls.append((hook_name, dict(kwargs)))
        return []

    def calls_for(self, hook_name: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.calls if name == hook_name]


# ---------------------------------------------------------------------------
# on_session_start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_session_start_fires_after_new_session_persisted(tmp_path):
    """on_session_start fires once after create_session confirms not-exists + persist success."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    dispatcher = FakeHookDispatcher()
    service = SessionService(store, hook_dispatcher=dispatcher)

    created = await service.create_session("s1", source="dashboard")

    starts = dispatcher.calls_for("on_session_start")
    assert len(starts) == 1
    assert starts[0]["session_id"] == "s1"
    # source uses the created session's source
    assert starts[0]["source"] == created.source
    assert starts[0]["source"] == "dashboard"


@pytest.mark.asyncio
async def test_on_session_start_does_not_fire_for_existing_session(tmp_path):
    """create_session on an existing session must not dispatch on_session_start."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    service = SessionService(store, hook_dispatcher=dispatcher)

    await service.create_session("s1", source="dashboard")

    assert len(dispatcher.calls_for("on_session_start")) == 0


@pytest.mark.asyncio
async def test_on_session_start_does_not_fire_when_persist_fails(tmp_path):
    """If memory_store.create_session raises, on_session_start must not fire."""

    class _FailingStore:
        def __init__(self):
            self._sessions: dict[str, ConversationSession] = {}

        async def get_session(self, session_id):
            return self._sessions.get(session_id)

        async def create_session(self, session):
            raise RuntimeError("persist failed")

        async def list_sessions(self):
            return []

    dispatcher = FakeHookDispatcher()
    service = SessionService(_FailingStore(), hook_dispatcher=dispatcher)

    with pytest.raises(RuntimeError):
        await service.create_session("s1", source="dashboard")

    assert len(dispatcher.calls_for("on_session_start")) == 0


@pytest.mark.asyncio
async def test_on_session_start_not_dispatched_when_dispatcher_none(tmp_path):
    """When hook_dispatcher is None, no hooks fire (backward compat)."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    service = SessionService(store)

    session = await service.create_session("s1", source="api")
    assert session.id == "s1"
    persisted = await store.get_session("s1")
    assert persisted is not None


# ---------------------------------------------------------------------------
# on_session_end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_session_end_fires_after_delete_success(tmp_path):
    """on_session_end fires once after delete_session persists deletion."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1", source=SessionSource.DASHBOARD.value))
    dispatcher = FakeHookDispatcher()
    service = SessionService(store, hook_dispatcher=dispatcher)

    await service.delete_session("s1")

    ends = dispatcher.calls_for("on_session_end")
    assert len(ends) == 1
    assert ends[0]["session_id"] == "s1"
    # source is the existing session's source (saved before delete)
    assert ends[0]["source"] == SessionSource.DASHBOARD.value


@pytest.mark.asyncio
async def test_on_session_end_does_not_fire_when_session_missing(tmp_path):
    """delete_session on a missing session must not dispatch on_session_end."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    dispatcher = FakeHookDispatcher()
    service = SessionService(store, hook_dispatcher=dispatcher)

    with pytest.raises(SessionNotFoundError):
        await service.delete_session("nope")

    assert len(dispatcher.calls_for("on_session_end")) == 0


@pytest.mark.asyncio
async def test_on_session_end_does_not_fire_when_delete_fails(tmp_path):
    """If memory_store.delete_session returns False, on_session_end must not fire."""

    class _FailingDeleteStore:
        def __init__(self):
            self._sessions: dict[str, ConversationSession] = {
                "s1": ConversationSession(id="s1"),
            }

        async def get_session(self, session_id):
            return self._sessions.get(session_id)

        async def delete_session(self, session_id):
            return False  # delete failed

    dispatcher = FakeHookDispatcher()
    service = SessionService(_FailingDeleteStore(), hook_dispatcher=dispatcher)

    with pytest.raises(SessionNotFoundError):
        await service.delete_session("s1")

    assert len(dispatcher.calls_for("on_session_end")) == 0


@pytest.mark.asyncio
async def test_on_session_end_preserves_cleanup_handlers(tmp_path):
    """on_session_end must not break existing on_session_deleted cleanup handlers."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    dispatcher = FakeHookDispatcher()
    deleted_sessions: list[str] = []

    async def on_deleted(session_id: str) -> None:
        deleted_sessions.append(session_id)

    service = SessionService(
        store,
        on_session_deleted=on_deleted,
        hook_dispatcher=dispatcher,
    )

    await service.delete_session("s1")

    assert deleted_sessions == ["s1"]
    assert len(dispatcher.calls_for("on_session_end")) == 1


@pytest.mark.asyncio
async def test_on_session_end_not_dispatched_when_dispatcher_none(tmp_path):
    """When hook_dispatcher is None, no hooks fire (backward compat)."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1"))
    service = SessionService(store)

    await service.delete_session("s1")
    assert await store.get_session("s1") is None


@pytest.mark.asyncio
async def test_on_session_end_saves_source_before_delete(tmp_path):
    """The source passed to on_session_end is the pre-delete session's source."""
    store = SQLiteMemoryStore(tmp_path / "s.db")
    await store.create_session(ConversationSession(id="s1", source="api"))
    dispatcher = FakeHookDispatcher()
    service = SessionService(store, hook_dispatcher=dispatcher)

    await service.delete_session("s1")

    ends = dispatcher.calls_for("on_session_end")
    assert len(ends) == 1
    assert ends[0]["source"] == "api"
