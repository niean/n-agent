from datetime import datetime, timezone

import pytest

from app.domain.session import ConversationMessage, ConversationSession
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


@pytest.mark.asyncio
async def test_append_message_if_session_exists_returns_none_when_absent(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    msg = ConversationMessage(role="system", name="ui.task_command", content="hi")
    result = await store.append_message_if_session_exists("no-such-session", msg)
    assert result is None
    # 会话未被创建
    assert await store.get_session("no-such-session") is None


@pytest.mark.asyncio
async def test_append_message_if_session_exists_appends_when_present(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    msg = ConversationMessage(role="system", name="ui.task_lifecycle", content="运行中")
    result = await store.append_message_if_session_exists("s1", msg)
    assert result is not None
    assert result.id == msg.id and result.name == "ui.task_lifecycle"
    msgs = await store.list_messages("s1")
    assert len(msgs) == 1
    assert msgs[0].role == "system" and msgs[0].content == "运行中"


@pytest.mark.asyncio
async def test_append_message_if_session_exists_updates_session_updated_at_no_regression(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    sess_before = await store.get_session("s1")
    # 用一个早于 created_at 的时间戳追加，updated_at 不应倒退
    early = datetime(2020, 1, 1, tzinfo=timezone.utc)
    msg = ConversationMessage(role="system", content="x", created_at=early)
    await store.append_message_if_session_exists("s1", msg)
    sess_after = await store.get_session("s1")
    assert sess_after is not None
    assert sess_after.updated_at >= sess_before.updated_at  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_list_messages_stable_order_same_timestamp(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    t = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        m = ConversationMessage(role="system", name="ui.task_command", content=str(i), created_at=t)
        await store.append_message("s1", m)
    msgs = await store.list_messages("s1")
    assert [m.content for m in msgs] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_append_message_if_session_exists_isolated_per_session(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    await store.create_session(ConversationSession(id="s2"))
    await store.append_message_if_session_exists(
        "s1", ConversationMessage(role="system", content="a")
    )
    s2_msgs = await store.list_messages("s2")
    assert s2_msgs == []
    s1_msgs = await store.list_messages("s1")
    assert len(s1_msgs) == 1
