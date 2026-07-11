from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.domain.context import CONTEXT_SUMMARY_PREFIX
from app.domain.session import ConversationMessage
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


@pytest.fixture
def store(tmp_path):
    return SQLiteMemoryStore(tmp_path / "test.db")


@pytest.mark.asyncio
async def test_messages_table_has_is_summary_column(store):
    with store._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "is_summary" in cols


@pytest.mark.asyncio
async def test_migrate_idempotent_on_old_db(tmp_path):
    db_path = tmp_path / "old.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages("
            "id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content_json TEXT, "
            "created_at TEXT, provider_message_id TEXT, tool_call_id TEXT, name TEXT)"
        )
        conn.execute(
            "CREATE TABLE sessions("
            "id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, source TEXT)"
        )
    store = SQLiteMemoryStore(db_path)
    with store._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "is_summary" in cols
    store.initialize()
    with store._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "is_summary" in cols


@pytest.mark.asyncio
async def test_append_and_list_message_with_is_summary(store):
    sid = "test-session-1"
    plain_created = datetime(2026, 7, 10, 0, 0, 0, tzinfo=timezone.utc)
    plain = ConversationMessage(
        role="user", content="hello", created_at=plain_created, is_summary=False,
    )
    await store.append_message(sid, plain)
    created_at = datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc)
    summary_msg = ConversationMessage(
        role="user",
        content=f"{CONTEXT_SUMMARY_PREFIX}S1",
        created_at=created_at,
        is_summary=True,
    )
    await store.append_message(sid, summary_msg)
    msgs = await store.list_messages(sid)
    assert len(msgs) == 2
    assert msgs[0].is_summary is False
    assert msgs[1].is_summary is True
    assert msgs[1].created_at == created_at


@pytest.mark.asyncio
async def test_append_summary_message_keeps_old_and_inserts_new(store):
    sid = "test-session-2"
    old1 = ConversationMessage(role="user", content=f"{CONTEXT_SUMMARY_PREFIX}old1", is_summary=True)
    old2 = ConversationMessage(role="user", content=f"{CONTEXT_SUMMARY_PREFIX}old2", is_summary=True)
    await store.append_message(sid, old1)
    await store.append_message(sid, old2)
    plain = ConversationMessage(role="user", content="hello")
    await store.append_message(sid, plain)
    new_msg = ConversationMessage(
        role="user", content=f"{CONTEXT_SUMMARY_PREFIX}new", is_summary=True,
    )
    returned = await store.append_summary_message(sid, new_msg)
    assert returned.id == new_msg.id
    msgs = await store.list_messages(sid)
    summaries = [m for m in msgs if m.is_summary]
    assert len(summaries) == 3
    assert [m.content for m in summaries] == [
        f"{CONTEXT_SUMMARY_PREFIX}old1",
        f"{CONTEXT_SUMMARY_PREFIX}old2",
        f"{CONTEXT_SUMMARY_PREFIX}new",
    ]
    plains = [m for m in msgs if not m.is_summary]
    assert len(plains) == 1
    assert plains[0].content == "hello"


@pytest.mark.asyncio
async def test_append_summary_message_rejects_invalid_message(store):
    sid = "test-session-3"
    with pytest.raises(ValueError):
        await store.append_summary_message(
            sid,
            ConversationMessage(role="user", content=f"{CONTEXT_SUMMARY_PREFIX}x", is_summary=False),
        )
    with pytest.raises(ValueError):
        await store.append_summary_message(
            sid,
            ConversationMessage(role="assistant", content=f"{CONTEXT_SUMMARY_PREFIX}x", is_summary=True),
        )
    with pytest.raises(ValueError):
        await store.append_summary_message(
            sid,
            ConversationMessage(role="user", content="no prefix", is_summary=True),
        )
    with pytest.raises(ValueError):
        await store.append_summary_message(
            sid,
            ConversationMessage(role="user", content={"k": "v"}, is_summary=True),
        )


@pytest.mark.asyncio
async def test_delete_summary_messages_returns_count(store):
    sid = "test-session-4"
    await store.append_message(sid, ConversationMessage(role="user", content=f"{CONTEXT_SUMMARY_PREFIX}a", is_summary=True))
    await store.append_message(sid, ConversationMessage(role="user", content=f"{CONTEXT_SUMMARY_PREFIX}b", is_summary=True))
    await store.append_message(sid, ConversationMessage(role="user", content="plain"))
    count = await store.delete_summary_messages(sid)
    assert count == 2
    msgs = await store.list_messages(sid)
    assert len(msgs) == 1
    assert msgs[0].is_summary is False


@pytest.mark.asyncio
async def test_append_summary_message_preserves_plain_message_with_prefix(store):
    sid = "test-session-5"
    prefix_plain = ConversationMessage(
        role="user", content=f"{CONTEXT_SUMMARY_PREFIX}not a real summary", is_summary=False,
    )
    await store.append_message(sid, prefix_plain)
    old_summary = ConversationMessage(
        role="user", content=f"{CONTEXT_SUMMARY_PREFIX}real old", is_summary=True,
    )
    await store.append_message(sid, old_summary)
    new_msg = ConversationMessage(
        role="user", content=f"{CONTEXT_SUMMARY_PREFIX}real new", is_summary=True,
    )
    await store.append_summary_message(sid, new_msg)
    msgs = await store.list_messages(sid)
    plains = [m for m in msgs if not m.is_summary]
    assert len(plains) == 1
    assert plains[0].content == f"{CONTEXT_SUMMARY_PREFIX}not a real summary"
    summaries = [m for m in msgs if m.is_summary]
    assert len(summaries) == 2
    assert [m.content for m in summaries] == [
        f"{CONTEXT_SUMMARY_PREFIX}real old",
        f"{CONTEXT_SUMMARY_PREFIX}real new",
    ]


@pytest.mark.asyncio
async def test_migrate_mark_legacy_middle_summarized(tmp_path):
    """存量数据迁移：已有 summary 但 middle 未标记 is_summarized 的会话，
    迁移后 middle 被标记，head 和 tail 保护。"""
    import json as _json
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages("
            "id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content_json TEXT, "
            "created_at TEXT, provider_message_id TEXT, tool_call_id TEXT, name TEXT, "
            "is_summary INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE sessions("
            "id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, source TEXT, "
            "external_memory_enabled_json TEXT, external_memory_slots_json TEXT, acp_metadata_json TEXT)"
        )
        sid = "legacy-session-1"
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(30):
            conn.execute(
                "INSERT INTO messages(id, session_id, role, content_json, created_at, "
                "provider_message_id, tool_call_id, name, is_summary) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0)",
                (f"m{i}", sid, "user", _json.dumps(f"msg{i}"), base.replace(second=i).isoformat()),
            )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content_json, created_at, "
            "provider_message_id, tool_call_id, name, is_summary) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 1)",
            ("s1", sid, "user", _json.dumps(f"{CONTEXT_SUMMARY_PREFIX}summary"), base.replace(second=40).isoformat()),
        )
    store = SQLiteMemoryStore(
        db_path, migration_protect_first_n=3, migration_protect_last_n=20,
    )
    msgs = await store.list_messages(sid)
    assert msgs[0].is_summarized is False
    assert msgs[1].is_summarized is False
    assert msgs[2].is_summarized is False
    for i in range(3, 10):
        assert msgs[i].is_summarized is True, f"msg {i} should be marked"
    for i in range(10, 30):
        assert msgs[i].is_summarized is False, f"msg {i} should be protected"
    assert msgs[30].is_summarized is False
    assert msgs[30].is_summary is True


@pytest.mark.asyncio
async def test_migrate_mark_legacy_middle_idempotent(tmp_path):
    """迁移幂等：重复 initialize 不会重复标记。"""
    import json as _json
    db_path = tmp_path / "legacy2.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE messages("
            "id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content_json TEXT, "
            "created_at TEXT, provider_message_id TEXT, tool_call_id TEXT, name TEXT, "
            "is_summary INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE sessions("
            "id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, source TEXT, "
            "external_memory_enabled_json TEXT, external_memory_slots_json TEXT, acp_metadata_json TEXT)"
        )
        sid = "legacy-session-2"
        base = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(30):
            conn.execute(
                "INSERT INTO messages(id, session_id, role, content_json, created_at, "
                "provider_message_id, tool_call_id, name, is_summary) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0)",
                (f"m{i}", sid, "user", _json.dumps(f"msg{i}"), base.replace(second=i).isoformat()),
            )
        conn.execute(
            "INSERT INTO messages(id, session_id, role, content_json, created_at, "
            "provider_message_id, tool_call_id, name, is_summary) "
            "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 1)",
            ("s1", sid, "user", _json.dumps(f"{CONTEXT_SUMMARY_PREFIX}summary"), base.replace(second=40).isoformat()),
        )
    store = SQLiteMemoryStore(db_path, migration_protect_first_n=3, migration_protect_last_n=20)
    store.initialize()
    msgs = await store.list_messages(sid)
    middle_marked = [m for m in msgs[3:10] if m.is_summarized]
    assert len(middle_marked) == 7
