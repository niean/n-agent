import sqlite3
from datetime import datetime, timezone

import pytest

from app.domain.session import (
    ConversationMessage,
    ConversationSession,
    Summary,
    TaskState,
    ToolCall,
)
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore


@pytest.mark.asyncio
async def test_acp_metadata_roundtrip(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    metadata = {"cwd": "/tmp/work", "mode": "plan", "model": "claude-opus-4"}
    await store.create_session(
        ConversationSession(id="s1", title="S1", source="acp", acp_metadata=metadata)
    )

    session = await store.get_session("s1")

    assert session is not None
    assert session.acp_metadata == metadata


@pytest.mark.asyncio
async def test_acp_metadata_default_none(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    await store.create_session(
        ConversationSession(id="s1", title="S1", source="api")
    )

    session = await store.get_session("s1")

    assert session is not None
    assert session.acp_metadata is None


@pytest.mark.asyncio
async def test_update_session_acp_metadata_persists(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    await store.create_session(
        ConversationSession(id="s1", title="S1", source="acp", acp_metadata={"cwd": "/a"})
    )

    await store.update_session_acp_metadata("s1", {"cwd": "/b", "mode": "code"})

    session = await store.get_session("s1")
    assert session is not None
    assert session.acp_metadata == {"cwd": "/b", "mode": "code"}


@pytest.mark.asyncio
async def test_update_session_acp_metadata_noop_on_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")

    # Should not raise
    await store.update_session_acp_metadata("missing-id", {"cwd": "/x"})

    # Verify nothing was inserted
    assert await store.get_session("missing-id") is None


@pytest.mark.asyncio
async def test_list_sessions_by_source_filters_source(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    await store.create_session(
        ConversationSession(id="acp-1", title="A1", source="acp", acp_metadata={"cwd": "/a"})
    )
    await store.create_session(
        ConversationSession(id="acp-2", title="A2", source="acp", acp_metadata={"cwd": "/b"})
    )
    await store.create_session(
        ConversationSession(id="api-1", title="U1", source="api")
    )

    sessions, cursor = await store.list_sessions_by_source("acp")

    assert cursor is None
    assert {s.id for s in sessions} == {"acp-1", "acp-2"}


@pytest.mark.asyncio
async def test_list_sessions_by_source_filters_cwd(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    await store.create_session(
        ConversationSession(
            id="acp-1", title="A1", source="acp", acp_metadata={"cwd": "/work"}
        )
    )
    await store.create_session(
        ConversationSession(
            id="acp-2", title="A2", source="acp", acp_metadata={"cwd": "/other"}
        )
    )
    await store.create_session(
        ConversationSession(
            id="acp-3", title="A3", source="acp", acp_metadata={"cwd": "/work"}
        )
    )

    sessions, cursor = await store.list_sessions_by_source("acp", cwd="/work")

    assert cursor is None
    assert {s.id for s in sessions} == {"acp-1", "acp-3"}


@pytest.mark.asyncio
async def test_list_sessions_by_source_cursor_pagination(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    # Use distinct updated_at to make ordering deterministic
    base = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        ts = base.replace(minute=i)
        await store.create_session(
            ConversationSession(
                id=f"acp-{i}",
                title=f"A{i}",
                source="acp",
                acp_metadata={"cwd": "/x"},
                updated_at=ts,
                created_at=ts,
            )
        )

    # Page 1: limit=2
    page1, cursor1 = await store.list_sessions_by_source("acp", limit=2)
    assert len(page1) == 2
    assert cursor1 is not None
    # DESC order by updated_at -> acp-2 (minute=2), acp-1 (minute=1)
    assert [s.id for s in page1] == ["acp-2", "acp-1"]

    # Page 2: cursor from page1
    page2, cursor2 = await store.list_sessions_by_source("acp", cursor=cursor1, limit=2)
    assert len(page2) == 1
    assert cursor2 is None
    assert page2[0].id == "acp-0"


@pytest.mark.asyncio
async def test_list_sessions_by_source_returns_none_cursor_when_no_more(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    await store.create_session(
        ConversationSession(id="acp-1", title="A1", source="acp", acp_metadata={"cwd": "/x"})
    )

    sessions, cursor = await store.list_sessions_by_source("acp", limit=50)

    assert cursor is None
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_list_sessions_by_source_returns_empty_when_cursor_not_found(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    # Use distinct updated_at to make ordering deterministic
    base = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        ts = base.replace(minute=i)
        await store.create_session(
            ConversationSession(
                id=f"acp-{i}",
                title=f"A{i}",
                source="acp",
                acp_metadata={"cwd": "/x"},
                updated_at=ts,
                created_at=ts,
            )
        )

    # Page 1: limit=2, capture cursor pointing at last item of page 1
    page1, cursor1 = await store.list_sessions_by_source("acp", limit=2)
    assert len(page1) == 2
    assert cursor1 is not None

    # Delete the session the cursor points at (last item of page1)
    cursor_session_id = cursor1.split("|", 1)[1]
    await store.delete_session(cursor_session_id)

    # Page 2 with stale cursor: should return ([], None) instead of re-returning page 1
    page2, cursor2 = await store.list_sessions_by_source("acp", cursor=cursor1, limit=2)

    assert page2 == []
    assert cursor2 is None


@pytest.mark.asyncio
async def test_clone_session_copies_messages_tool_calls_summaries_task_states(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    await store.create_session(
        ConversationSession(
            id="src-1",
            title="Source",
            source="api",
            acp_metadata={"cwd": "/work"},
        )
    )
    msg = await store.append_message("src-1", ConversationMessage(role="user", content="hi"))
    await store.save_tool_call(
        ToolCall(
            id="tc-1",
            session_id="src-1",
            message_id=msg.id,
            tool_name="calc",
            arguments={"x": 1},
            result={"y": 2},
            status="success",
            duration_ms=10,
        )
    )
    await store.save_task_state(
        TaskState(session_id="src-1", status="completed", iteration_count=2)
    )
    await store.save_summary(Summary(session_id="src-1", summary="sum"))

    await store.clone_session("src-1", "tgt-1")

    # Verify session cloned
    tgt = await store.get_session("tgt-1")
    assert tgt is not None
    assert tgt.title == "Source"
    assert tgt.source == "acp"
    assert tgt.acp_metadata == {"cwd": "/work"}
    assert tgt.id != "src-1"
    # Cloned session must have fresh created_at/updated_at (not copied from source)
    src = await store.get_session("src-1")
    assert tgt.created_at != src.created_at
    assert tgt.updated_at != src.updated_at

    # Verify messages cloned with new ids
    tgt_msgs = await store.list_messages("tgt-1")
    assert len(tgt_msgs) == 1
    assert tgt_msgs[0].content == "hi"
    assert tgt_msgs[0].id != msg.id

    # Verify tool_calls cloned with new ids and message linkage rebuilt
    tgt_tcs = await store.list_tool_calls("tgt-1")
    assert len(tgt_tcs) == 1
    assert tgt_tcs[0].tool_name == "calc"
    assert tgt_tcs[0].arguments == {"x": 1}
    assert tgt_tcs[0].result == {"y": 2}
    assert tgt_tcs[0].id != "tc-1"
    # message_id should point to the new cloned message id (or None if linkage dropped)
    assert tgt_tcs[0].message_id == tgt_msgs[0].id

    # Verify task_state cloned
    tgt_ts = await store.get_task_state("tgt-1")
    assert tgt_ts is not None
    assert tgt_ts.status == "completed"
    assert tgt_ts.iteration_count == 2

    # Verify summary cloned
    tgt_sum = await store.get_summary("tgt-1")
    assert tgt_sum is not None
    assert tgt_sum.summary == "sum"

    # Verify source session untouched
    src_msgs = await store.list_messages("src-1")
    assert len(src_msgs) == 1
    assert src_msgs[0].id == msg.id


@pytest.mark.asyncio
async def test_clone_session_sets_source_acp(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")
    await store.create_session(
        ConversationSession(id="src-1", title="Source", source="api")
    )

    await store.clone_session("src-1", "tgt-1")

    tgt = await store.get_session("tgt-1")
    assert tgt is not None
    assert tgt.source == "acp"


@pytest.mark.asyncio
async def test_clone_session_noop_on_missing(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "test.db")

    # Should not raise
    await store.clone_session("missing-src", "tgt-1")

    assert await store.get_session("tgt-1") is None


@pytest.mark.asyncio
async def test_old_db_migration_adds_acp_metadata_column(tmp_path):
    db_path = tmp_path / "old.db"
    # Create old schema without acp_metadata_json (also missing external_memory_slots_json)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            source TEXT NOT NULL,
            external_memory_enabled_json TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions(id, title, created_at, updated_at, source) VALUES (?, ?, ?, ?, ?)",
        (
            "old-1",
            "Old",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "api",
        ),
    )
    conn.commit()
    conn.close()

    # Construct store -- triggers initialize() -> migration
    store = SQLiteMemoryStore(db_path)

    # Verify column exists
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    conn.close()
    assert "acp_metadata_json" in columns

    # Verify old row is readable and acp_metadata is None
    session = await store.get_session("old-1")
    assert session is not None
    assert session.acp_metadata is None
    assert session.id == "old-1"
    assert session.title == "Old"
