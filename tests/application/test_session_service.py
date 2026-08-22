import asyncio

import pytest

from app.application.session_service import SessionService
from app.domain.session import (
    ConversationSession,
    SessionNotFoundError,
    SessionValidationError,
)
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


class StubTitleGenerator:
    def __init__(self, title: str):
        self.title = title
        self.calls: list[str] = []

    async def generate(self, user_message: str) -> str:
        self.calls.append(user_message)
        return self.title


@pytest.mark.asyncio
async def test_ensure_title_generates_for_default_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    generator = StubTitleGenerator("如何新增预单")
    service = SessionService(store, title_generator=generator)

    await service.ensure_title("s1", "新增预单的流程")
    for _ in range(20):
        session = await store.get_session("s1")
        if session and not session.has_default_title():
            break
        await asyncio.sleep(0.05)

    session = await store.get_session("s1")
    assert session.title == "如何新增预单"
    assert generator.calls == ["新增预单的流程"]


@pytest.mark.asyncio
async def test_ensure_title_skips_when_session_has_custom_title(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    await store.update_session_title("s1", "已有标题")
    generator = StubTitleGenerator("不该被采用")
    service = SessionService(store, title_generator=generator)

    await service.ensure_title("s1", "新消息")
    await asyncio.sleep(0.1)

    session = await store.get_session("s1")
    assert session.title == "已有标题"
    assert generator.calls == []


@pytest.mark.asyncio
async def test_ensure_title_no_op_without_generator(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    service = SessionService(store)

    await service.ensure_title("s1", "新消息")
    await asyncio.sleep(0.05)

    session = await store.get_session("s1")
    assert session.has_default_title()


@pytest.mark.asyncio
async def test_ensure_title_no_op_for_empty_message(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    generator = StubTitleGenerator("X")
    service = SessionService(store, title_generator=generator)

    await service.ensure_title("s1", "")
    await asyncio.sleep(0.05)

    assert generator.calls == []


@pytest.mark.asyncio
async def test_ensure_title_handles_generator_failure(tmp_path):
    class ExplodingGenerator:
        async def generate(self, user_message: str) -> str:
            raise RuntimeError("boom")

    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    service = SessionService(store, title_generator=ExplodingGenerator())

    await service.ensure_title("s1", "x")
    await asyncio.sleep(0.05)

    session = await store.get_session("s1")
    assert session.has_default_title()


@pytest.mark.asyncio
async def test_rename_session_updates_title(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    service = SessionService(store)

    result = await service.rename_session("s1", "  我的会话  ")

    assert result.title == "我的会话"
    persisted = await store.get_session("s1")
    assert persisted.title == "我的会话"


@pytest.mark.asyncio
async def test_rename_session_truncates_to_60_chars(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    service = SessionService(store)

    long = "标题" * 50
    result = await service.rename_session("s1", long)

    assert len(result.title) == 60
    assert result.title == long[:60]


@pytest.mark.asyncio
async def test_rename_session_rejects_blank_title(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    service = SessionService(store)

    with pytest.raises(SessionValidationError):
        await service.rename_session("s1", "   ")


@pytest.mark.asyncio
async def test_rename_session_raises_when_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = SessionService(store)

    with pytest.raises(SessionNotFoundError):
        await service.rename_session("nope", "x")


@pytest.mark.asyncio
async def test_delete_session_removes_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    service = SessionService(store)

    await service.delete_session("s1")

    assert await store.get_session("s1") is None


@pytest.mark.asyncio
async def test_delete_session_notifies_after_delete(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    deleted_sessions = []

    async def on_deleted(session_id: str) -> None:
        deleted_sessions.append(session_id)

    service = SessionService(store, on_session_deleted=on_deleted)

    await service.delete_session("s1")

    assert deleted_sessions == ["s1"]


@pytest.mark.asyncio
async def test_delete_session_runs_required_pre_delete_cleanup(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    observed: list[bool] = []

    async def cleanup(session_id: str) -> None:
        observed.append(await store.get_session(session_id) is not None)

    service = SessionService(store, on_session_deleting_handlers=[cleanup])
    await service.delete_session("s1")

    assert observed == [True]
    assert await store.get_session("s1") is None


@pytest.mark.asyncio
async def test_delete_session_keeps_session_when_required_cleanup_fails(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))

    async def cleanup(session_id: str) -> None:
        raise RuntimeError("task cleanup failed")

    service = SessionService(store, on_session_deleting_handlers=[cleanup])
    with pytest.raises(RuntimeError, match="task cleanup failed"):
        await service.delete_session("s1")

    assert await store.get_session("s1") is not None


@pytest.mark.asyncio
async def test_delete_session_raises_when_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = SessionService(store)

    with pytest.raises(SessionNotFoundError):
        await service.delete_session("nope")


class _HookCapturingManager:
    """Test double for ExternalMemoryManager capturing session lifecycle hooks."""

    def __init__(self):
        self.switch_calls: list[tuple[str, dict]] = []
        self.end_calls: list[str] = []

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self.switch_calls.append((new_session_id, dict(kwargs)))

    def on_session_end(self, session_id: str) -> None:
        self.end_calls.append(session_id)


@pytest.mark.asyncio
async def test_create_session_fires_on_session_switch_for_new_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    manager = _HookCapturingManager()
    service = SessionService(store, external_memory_manager=manager)

    await service.create_session("s1", source="dashboard")

    assert manager.switch_calls == [("s1", {})]
    assert manager.end_calls == []


@pytest.mark.asyncio
async def test_create_session_skips_hook_when_session_exists(tmp_path):
    """Idempotent create_session on an existing session must not re-fire on_session_switch."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    manager = _HookCapturingManager()
    service = SessionService(store, external_memory_manager=manager)

    await service.create_session("s1", source="dashboard")

    assert manager.switch_calls == []


@pytest.mark.asyncio
async def test_delete_session_fires_on_session_end_before_deletion(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    manager = _HookCapturingManager()
    service = SessionService(store, external_memory_manager=manager)

    await service.delete_session("s1")

    assert manager.end_calls == ["s1"]
    assert await store.get_session("s1") is None


@pytest.mark.asyncio
async def test_delete_session_does_not_fire_hook_when_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    manager = _HookCapturingManager()
    service = SessionService(store, external_memory_manager=manager)

    with pytest.raises(SessionNotFoundError):
        await service.delete_session("nope")

    assert manager.end_calls == []


@pytest.mark.asyncio
async def test_delete_session_still_notifies_on_session_deleted_after_hook(tmp_path):
    """on_session_end fires before on_session_deleted callback."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    manager = _HookCapturingManager()
    deleted_order: list[str] = []

    async def on_deleted(session_id: str) -> None:
        deleted_order.append(f"deleted:{session_id}")

    service = SessionService(
        store,
        external_memory_manager=manager,
        on_session_deleted=on_deleted,
    )

    await service.delete_session("s1")

    assert manager.end_calls == ["s1"]
    assert deleted_order == ["deleted:s1"]


@pytest.mark.asyncio
async def test_create_session_without_manager_does_not_error(tmp_path):
    """Sessions work unchanged when no external_memory_manager is wired."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    service = SessionService(store)

    session = await service.create_session("s1", source="api")

    assert session.id == "s1"
    persisted = await store.get_session("s1")
    assert persisted is not None
