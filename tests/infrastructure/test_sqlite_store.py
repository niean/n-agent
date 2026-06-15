import sqlite3

import pytest

from app.domain.gateway import GatewaySessionKey, InteractionSourceType
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
    key = GatewaySessionKey(InteractionSourceType.CLI, "local")
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
