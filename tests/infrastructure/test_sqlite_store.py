import json
import sqlite3
from datetime import datetime, timezone

import pytest

from app.domain.gateway import GatewaySessionKey
from app.domain.platform import Platform
from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_gateway_registry import SQLiteGatewaySessionRegistry


@pytest.mark.asyncio
async def test_sqlite_store_initializes_schema_and_indexes(tmp_path):
    db_path = tmp_path / "sessions.db"
    SQLiteMemoryStore(db_path)

    conn = sqlite3.connect(db_path)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}

    assert {
        "sessions",
        "messages",
        "tool_calls",
        "task_states",
        "summaries",
        "gateway_conversations",
        "gateway_session_links",
        "gateway_processed_events",
    }.issubset(tables)
    assert "idx_messages_session_created_at" in indexes
    assert "idx_tool_calls_session_created_at" in indexes

    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "external_memory_enabled_json" in columns


@pytest.mark.asyncio
async def test_sqlite_store_persists_session_context(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    session = await store.create_session(ConversationSession(id="s1", title="Session 1"))
    message = await store.append_message("s1", ConversationMessage(role="user", content="hello"))
    tool_call = await store.save_tool_call(
        ToolCall(
            id="call-1",
            session_id="s1",
            message_id=message.id,
            tool_name="calculator",
            arguments={"expression": "1+1"},
            result={"result": 2},
            status="success",
        )
    )
    task_state = await store.save_task_state(TaskState(session_id="s1", status="completed", iteration_count=1))
    summary = await store.save_summary(Summary(session_id="s1", summary="summary"))
    context = await store.get_context("s1")

    assert session.id == "s1"
    assert context["messages"][0].content == "hello"
    assert context["tool_calls"][0].id == tool_call.id
    assert context["task_state"].status == task_state.status
    assert context["summary"].summary == summary.summary


@pytest.mark.asyncio
async def test_sqlite_store_locks_session_external_memory_once(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")

    first = await store.lock_session_external_memory("s-memory", ["builtin", "project_memory_1"])
    second = await store.lock_session_external_memory("s-memory", ["builtin", "project_memory_2"])
    session = await store.get_session("s-memory")

    assert first == ["builtin", "project_memory_1"]
    assert second == ["builtin", "project_memory_1"]
    assert session is not None
    assert session.external_memory_enabled == ["builtin", "project_memory_1"]


@pytest.mark.asyncio
async def test_heuristic_summarizer_marks_truncated_summary():
    summarizer = HeuristicSummarizer(max_chars=10)

    result = await summarizer.summarize([{"role": "user", "content": "a" * 50}])

    assert result.startswith("heuristic summary:")


@pytest.mark.asyncio
async def test_heuristic_summarizer_generates_brief_for_short_dialog():
    summarizer = HeuristicSummarizer()

    result = await summarizer.summarize(
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在的"},
        ]
    )

    assert "用户" in result and "你好" in result
    assert "助手" in result and "在的" in result


@pytest.mark.asyncio
async def test_heuristic_summarizer_returns_existing_for_empty_messages():
    summarizer = HeuristicSummarizer()

    result = await summarizer.summarize([], existing_summary="prev")

    assert result == "prev"


@pytest.mark.asyncio
async def test_delete_session_cascades_related_rows(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)
    gateway_registry = SQLiteGatewaySessionRegistry(db_path)
    await store.create_session(ConversationSession(id="s-del"))
    msg = await store.append_message("s-del", ConversationMessage(role="user", content="hi"))
    key = GatewaySessionKey("cli", "local")
    await gateway_registry.create_session_link(key, "s-del")
    await store.save_tool_call(
        ToolCall(id="tc-del", session_id="s-del", message_id=msg.id, tool_name="calc", arguments={}, status="success")
    )
    await store.save_task_state(TaskState(session_id="s-del", status="idle", iteration_count=0))
    await store.save_summary(Summary(session_id="s-del", summary="x"))

    deleted = await store.delete_session("s-del")

    assert deleted is True
    assert await store.get_session("s-del") is None
    assert await store.list_messages("s-del") == []
    assert await store.list_tool_calls("s-del") == []
    assert await store.get_task_state("s-del") is None
    assert await store.get_summary("s-del") is None
    assert await gateway_registry.get_active_session(key) is None
    assert await gateway_registry.list_session_links(key) == []


@pytest.mark.asyncio
async def test_delete_session_returns_false_when_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    assert await store.delete_session("nope") is False


@pytest.mark.asyncio
async def test_list_recent_tool_calls_filters_by_name_and_orders_desc(tmp_path):
    """Dashboard execute-code history relies on this for session_id=None queries."""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="s1"))
    await store.create_session(ConversationSession(id="s2"))
    first_created_at = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    second_created_at = datetime(2026, 7, 1, 10, 0, 5, tzinfo=timezone.utc)
    third_created_at = datetime(2026, 7, 1, 10, 0, 10, tzinfo=timezone.utc)
    await store.save_tool_call(ToolCall(
        id="tc1", session_id="s1", tool_name="execute_code",
        arguments={"code_hash": "aaa"}, status="success", duration_ms=10,
        created_at=first_created_at,
    ))
    await store.save_tool_call(ToolCall(
        id="tc2", session_id="s2", tool_name="search_knowledge",
        arguments={"q": "x"}, status="success", duration_ms=5,
        created_at=second_created_at,
    ))
    await store.save_tool_call(ToolCall(
        id="tc3", session_id="s2", tool_name="execute_code",
        arguments={"code_hash": "bbb"}, status="error", duration_ms=20,
        created_at=third_created_at,
    ))

    # Filter by execute_code, limit 50
    history = await store.list_recent_tool_calls(tool_name="execute_code", limit=50)
    assert [tc.id for tc in history] == ["tc3", "tc1"]  # DESC by created_at
    assert [tc.created_at for tc in history] == [third_created_at, first_created_at]

    # No filter, limit 2
    recent = await store.list_recent_tool_calls(limit=2)
    assert len(recent) == 2
    assert recent[0].id == "tc3"  # most recent first
    assert recent[0].created_at == third_created_at

    # No filter, limit 50 returns all
    all_calls = await store.list_recent_tool_calls(limit=50)
    assert len(all_calls) == 3


def test_migrate_session_id_prefixes_flattens_gw_feishu(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    with store._connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("gw-abc123", "Old Feishu", "gw/feishu", "2026-07-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("msg-1", "gw-abc123", "user", '"hi"', "2026-07-01T00:00:00+00:00"),
        )
        conn.commit()

    store.migrate_session_id_prefixes()

    with store._connect() as conn:
        session = conn.execute("SELECT id, source FROM sessions WHERE id = ?", ("feishu-abc123",)).fetchone()
        assert session is not None
        assert session["source"] == "feishu"
        msg = conn.execute("SELECT session_id FROM messages WHERE id = ?", ("msg-1",)).fetchone()
        assert msg["session_id"] == "feishu-abc123"
        old = conn.execute("SELECT 1 FROM sessions WHERE id = ?", ("gw-abc123",)).fetchone()
        assert old is None


def test_migrate_session_id_prefixes_idempotent_on_flattened_format(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    with store._connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("feishu-abc123", "New Feishu", "feishu", "2026-07-05T00:00:00+00:00", "2026-07-05T00:00:00+00:00"),
        )
        conn.commit()

    store.migrate_session_id_prefixes()

    with store._connect() as conn:
        session = conn.execute("SELECT id, source FROM sessions WHERE id = ?", ("feishu-abc123",)).fetchone()
        assert session is not None
        assert session["source"] == "feishu"


def test_migrate_session_id_prefixes_handles_legacy_feishu_with_gateway_prefix(tmp_path):
    db_path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(db_path)

    with store._connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("gateway-xyz", "Legacy Feishu", "feishu", "2026-06-15T00:00:00+00:00", "2026-06-15T00:00:00+00:00"),
        )
        conn.commit()

    store.migrate_session_id_prefixes()

    with store._connect() as conn:
        session = conn.execute("SELECT id, source FROM sessions").fetchone()
        assert session["id"] == "feishu-xyz"
        assert session["source"] == "feishu"


async def test_append_message_persists_and_reads_source(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    sid = "task-src-1"
    await store.append_message(sid, ConversationMessage(role="user", content="work task t1", source="task"))
    msgs = await store.list_messages(sid)
    assert len(msgs) == 1
    assert msgs[0].source == "task"


async def test_append_message_null_source_roundtrip(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    sid = "api-src-1"
    await store.append_message(sid, ConversationMessage(role="user", content="hi"))
    msgs = await store.list_messages(sid)
    assert msgs[0].source is None


async def test_source_migration_idempotent_on_legacy_db(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, source TEXT)")
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content_json TEXT, "
        "created_at TEXT, provider_message_id TEXT, tool_call_id TEXT, name TEXT, "
        "is_summary INTEGER DEFAULT 0, is_summarized INTEGER DEFAULT 0)"
    )
    conn.execute("INSERT INTO sessions(id,title,created_at,updated_at,source) VALUES('s','t','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','api')")
    conn.execute("INSERT INTO messages(id,session_id,role,content_json,created_at) VALUES('m','s','user','\"old\"','2026-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()
    store = SQLiteMemoryStore(db)
    msgs = await store.list_messages("s")
    assert len(msgs) == 1
    assert msgs[0].source is None
    cols = {row["name"] for row in store._connect().execute("PRAGMA table_info(messages)").fetchall()}
    assert "source" in cols
    store2 = SQLiteMemoryStore(db)
    assert len(await store2.list_messages("s")) == 1


async def test_clone_session_preserves_source_and_summary_flags(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    src = "src-clone-1"
    await store.append_message(src, ConversationMessage(role="user", content="work task t1", source="task"))
    await store.append_message(src, ConversationMessage(role="user", content="[CONTEXT SUMMARY]: x", is_summary=True))
    await store.clone_session(src, "dst-clone-1")
    cloned = await store.list_messages("dst-clone-1")
    assert len(cloned) == 2
    assert cloned[0].source == "task"
    assert cloned[0].is_summary is False
    assert cloned[1].is_summary is True
    assert cloned[1].source is None


@pytest.mark.asyncio
async def test_append_message_persists_and_reads_card(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-1"))
    card = {"schema_version": 1, "kind": "task_lifecycle", "task_id": "t_1",
            "status": "waiting_approval", "title": "T", "summary": "p",
            "available_actions": ["approve", "reject", "revise", "cancel"]}
    await store.append_message("sess-1", ConversationMessage(
        role="system", content="等待批准", name="ui.task_lifecycle", card=card))
    msgs = await store.list_messages("sess-1")
    assert msgs[-1].card == card


@pytest.mark.asyncio
async def test_append_message_null_card_roundtrip(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-1"))
    await store.append_message("sess-1", ConversationMessage(
        role="system", content="x", name="ui.task_lifecycle"))
    assert (await store.list_messages("sess-1"))[-1].card is None


@pytest.mark.asyncio
async def test_card_migration_idempotent_on_legacy_db(tmp_path):
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, created_at TEXT, updated_at TEXT, source TEXT)")
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content_json TEXT, "
        "created_at TEXT, provider_message_id TEXT, tool_call_id TEXT, name TEXT, "
        "is_summary INTEGER DEFAULT 0, is_summarized INTEGER DEFAULT 0, source TEXT)"
    )
    conn.execute("INSERT INTO sessions(id,title,created_at,updated_at,source) VALUES('s','t','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00','api')")
    conn.execute("INSERT INTO messages(id,session_id,role,content_json,created_at,name,is_summary,is_summarized) VALUES('m','s','system','\"x\"','2026-01-01T00:00:00+00:00','ui.task_lifecycle',0,0)")
    conn.commit()
    conn.close()
    store = SQLiteMemoryStore(db)
    msgs = await store.list_messages("s")  # 触发懒初始化与迁移
    assert msgs[-1].card is None  # legacy row reads None
    cols = {row["name"] for row in store._connect().execute("PRAGMA table_info(messages)").fetchall()}
    assert "card_json" in cols
    store2 = SQLiteMemoryStore(db)
    assert (await store2.list_messages("s"))[-1].card is None  # 二次初始化无副作用


@pytest.mark.asyncio
async def test_decode_message_card_invalid_json_returns_none(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-1"))
    with store._connect() as conn:
        conn.execute("INSERT INTO messages(id,session_id,role,content_json,created_at,name,is_summary,is_summarized,card_json) VALUES('m','sess-1','system','\"x\"','2026-01-01T00:00:00+00:00','ui.task_lifecycle',0,0,'not-json')")
        conn.commit()
    msgs = await store.list_messages("sess-1")
    assert msgs[-1].card is None  # invalid JSON -> None, message preserved
    assert msgs[-1].content == "x"


@pytest.mark.asyncio
async def test_decode_message_card_non_object_returns_none(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-1"))
    with store._connect() as conn:
        conn.execute("INSERT INTO messages(id,session_id,role,content_json,created_at,name,is_summary,is_summarized,card_json) VALUES('m','sess-1','system','\"x\"','2026-01-01T00:00:00+00:00','ui.task_lifecycle',0,0,'[1,2,3]')")
        conn.commit()
    assert (await store.list_messages("sess-1"))[-1].card is None  # JSON array -> None


@pytest.mark.asyncio
async def test_decode_message_card_does_not_mask_content_json_error(tmp_path):
    """card 容错不掩盖 content_json 损坏：content_json 非法 JSON 仍抛原有 decode error。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-1"))
    with store._connect() as conn:
        conn.execute("INSERT INTO messages(id,session_id,role,content_json,created_at,name,is_summary,is_summarized,card_json) VALUES('m','sess-1','system','not-json-content','2026-01-01T00:00:00+00:00','ui.task_lifecycle',0,0,'not-json-card')")
        conn.commit()
    with pytest.raises(json.JSONDecodeError):
        await store.list_messages("sess-1")


@pytest.mark.asyncio
async def test_append_message_card_not_json_serializable_fails_atomically(tmp_path):
    """card 含不可 JSON 编码值时 append 整体失败，不写半条消息。"""
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    await store.create_session(ConversationSession(id="sess-1"))
    bad_card = {"unencodable": object()}  # object() not JSON serializable
    with pytest.raises(TypeError):
        await store.append_message("sess-1", ConversationMessage(
            role="system", content="x", name="ui.task_lifecycle", card=bad_card))
    # 无半条消息
    assert await store.list_messages("sess-1") == []
