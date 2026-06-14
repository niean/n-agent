import asyncio

import pytest

from app.application.session_service import SessionService
from app.domain.session import ConversationSession
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
