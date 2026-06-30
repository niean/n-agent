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
    key = GatewaySessionKey(Platform.CLI, "local")
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
