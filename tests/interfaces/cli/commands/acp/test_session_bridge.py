"""Tests for ACP session bridge (T10).

Uses real :class:`SQLiteMemoryStore` and :class:`SessionService` per S 1.
Covers create/load/resume/list/fork/close invariants from S 2-S 6.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.application.session_service import SessionService
from app.domain.session import ConversationSession
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.cli.commands.acp.session_bridge import ACPSessionBridge


@pytest.fixture
def memory_store(tmp_path):
    return SQLiteMemoryStore(tmp_path / "sessions.db")


@pytest.fixture
def session_service(memory_store):
    return SessionService(memory_store)


@pytest.fixture
def bridge(session_service, memory_store):
    return ACPSessionBridge(session_service, memory_store)


@pytest.mark.asyncio
async def test_create_persists_acp_source_and_metadata(bridge, memory_store):
    session = await bridge.create(
        "s1",
        cwd="/workspace/project-a",
        host_cwd="/Users/x/project-a",
        mode="plan",
        model="qwen2.5",
        config_options={"k": "v"},
        allowed_confirm_tools={"manage_schedule": "session"},
    )

    assert session.source == "acp"
    stored = await memory_store.get_session("s1")
    assert stored is not None
    assert stored.source == "acp"
    assert stored.acp_metadata is not None
    assert stored.acp_metadata["cwd"] == "/workspace/project-a"
    assert stored.acp_metadata["host_cwd"] == "/Users/x/project-a"
    assert stored.acp_metadata["mode"] == "plan"
    assert stored.acp_metadata["model"] == "qwen2.5"
    assert stored.acp_metadata["config_options"] == {"k": "v"}
    assert stored.acp_metadata["allowed_confirm_tools"] == {"manage_schedule": "session"}
    assert "updated_at" in stored.acp_metadata


@pytest.mark.asyncio
async def test_create_with_model_none_omits_optional_fields(bridge, memory_store):
    await bridge.create("s1", cwd="/work")

    stored = await memory_store.get_session("s1")
    assert stored is not None
    assert stored.acp_metadata is not None
    assert "model" not in stored.acp_metadata
    assert "host_cwd" not in stored.acp_metadata
    assert stored.acp_metadata["config_options"] == {}
    assert stored.acp_metadata["allowed_confirm_tools"] == {}


@pytest.mark.asyncio
async def test_create_with_explicit_model(bridge, memory_store):
    await bridge.create("s1", cwd="/work", model="claude-opus-4")

    stored = await memory_store.get_session("s1")
    assert stored is not None
    assert stored.acp_metadata["model"] == "claude-opus-4"


@pytest.mark.asyncio
async def test_load_returns_session_when_source_acp(bridge):
    await bridge.create("s1", cwd="/work")

    loaded = await bridge.load("s1")

    assert loaded is not None
    assert loaded.id == "s1"
    assert loaded.source == "acp"


@pytest.mark.asyncio
async def test_load_returns_none_when_session_missing(bridge):
    loaded = await bridge.load("missing")

    assert loaded is None


@pytest.mark.asyncio
async def test_load_returns_none_when_source_not_acp(bridge, memory_store):
    await memory_store.create_session(
        ConversationSession(id="s1", title="S1", source="api")
    )

    loaded = await bridge.load("s1")

    assert loaded is None


@pytest.mark.asyncio
async def test_resume_returns_existing_acp_session_as_is(bridge):
    await bridge.create("s1", cwd="/work", mode="plan")

    resumed = await bridge.resume("s1", cwd="/work", mode="code")

    assert resumed is not None
    assert resumed.id == "s1"
    assert resumed.acp_metadata["mode"] == "plan"


@pytest.mark.asyncio
async def test_resume_creates_new_acp_session_when_missing(bridge, memory_store):
    resumed = await bridge.resume(
        "s1",
        cwd="/work",
        host_cwd="/host/work",
        mode="code",
        model="qwen2.5",
    )

    assert resumed is not None
    assert resumed.id == "s1"
    assert resumed.source == "acp"
    stored = await memory_store.get_session("s1")
    assert stored is not None
    assert stored.source == "acp"
    assert stored.acp_metadata is not None
    assert stored.acp_metadata["cwd"] == "/work"
    assert stored.acp_metadata["host_cwd"] == "/host/work"
    assert stored.acp_metadata["mode"] == "code"
    assert stored.acp_metadata["model"] == "qwen2.5"


@pytest.mark.asyncio
async def test_resume_returns_none_when_session_exists_but_not_acp(bridge, memory_store):
    await memory_store.create_session(
        ConversationSession(id="s1", title="S1", source="api")
    )

    resumed = await bridge.resume("s1", cwd="/work")

    assert resumed is None
    stored = await memory_store.get_session("s1")
    assert stored is not None
    assert stored.source == "api"


@pytest.mark.asyncio
async def test_list_returns_only_acp_sessions(bridge, memory_store):
    await bridge.create("acp-1", cwd="/a")
    await bridge.create("acp-2", cwd="/b")
    await memory_store.create_session(
        ConversationSession(id="api-1", title="U1", source="api")
    )

    sessions, cursor = await bridge.list()

    assert cursor is None
    assert {s.id for s in sessions} == {"acp-1", "acp-2"}


@pytest.mark.asyncio
async def test_list_filters_by_cwd(bridge):
    await bridge.create("acp-1", cwd="/work")
    await bridge.create("acp-2", cwd="/other")
    await bridge.create("acp-3", cwd="/work")

    sessions, cursor = await bridge.list(cwd="/work")

    assert cursor is None
    assert {s.id for s in sessions} == {"acp-1", "acp-3"}


@pytest.mark.asyncio
async def test_list_cursor_pagination(bridge, memory_store):
    base = datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        ts = base.replace(minute=i)
        await memory_store.create_session(
            ConversationSession(
                id=f"acp-{i}",
                title=f"A{i}",
                source="acp",
                acp_metadata={"cwd": "/x"},
                updated_at=ts,
                created_at=ts,
            )
        )

    page1, cursor1 = await bridge.list(limit=2)
    assert len(page1) == 2
    assert cursor1 is not None
    assert [s.id for s in page1] == ["acp-2", "acp-1"]

    page2, cursor2 = await bridge.list(cursor=cursor1, limit=2)
    assert len(page2) == 1
    assert cursor2 is None
    assert page2[0].id == "acp-0"


@pytest.mark.asyncio
async def test_fork_clones_to_new_id_with_same_metadata(bridge, memory_store):
    await bridge.create(
        "src-1",
        cwd="/work",
        host_cwd="/host/work",
        mode="plan",
        model="qwen2.5",
        config_options={"k": "v"},
        allowed_confirm_tools={"manage_schedule": "session"},
    )

    forked_id = await bridge.fork("src-1", "tgt-1")

    assert forked_id == "tgt-1"
    tgt = await memory_store.get_session("tgt-1")
    assert tgt is not None
    assert tgt.source == "acp"
    src = await memory_store.get_session("src-1")
    assert tgt.acp_metadata == src.acp_metadata


@pytest.mark.asyncio
async def test_fork_generates_target_id_when_none(bridge):
    await bridge.create("src-1", cwd="/work")

    forked_id = await bridge.fork("src-1")

    assert forked_id is not None
    assert forked_id != "src-1"
    assert forked_id.startswith("acp-")


@pytest.mark.asyncio
async def test_fork_returns_none_when_source_missing(bridge):
    forked_id = await bridge.fork("missing-src", "tgt-1")

    assert forked_id is None


@pytest.mark.asyncio
async def test_fork_returns_none_when_source_not_acp(bridge, memory_store):
    await memory_store.create_session(
        ConversationSession(id="api-1", title="A", source="api")
    )

    forked_id = await bridge.fork("api-1", "tgt-1")

    assert forked_id is None


@pytest.mark.asyncio
async def test_close_does_not_delete_session(bridge, memory_store):
    await bridge.create("s1", cwd="/work")

    await bridge.close("s1")

    stored = await memory_store.get_session("s1")
    assert stored is not None
    assert stored.source == "acp"


@pytest.mark.asyncio
async def test_close_invokes_cleanup_callback(bridge, memory_store):
    await bridge.create("s1", cwd="/work")
    invoked_with: list[str] = []

    def cleanup(session_id: str) -> None:
        invoked_with.append(session_id)

    await bridge.close("s1", cleanup_callback=cleanup)

    assert invoked_with == ["s1"]
    stored = await memory_store.get_session("s1")
    assert stored is not None


@pytest.mark.asyncio
async def test_close_awaits_async_cleanup_callback(bridge, memory_store):
    await bridge.create("s1", cwd="/work")
    invoked_with: list[str] = []

    async def cleanup(session_id: str) -> None:
        invoked_with.append(session_id)

    await bridge.close("s1", cleanup_callback=cleanup)

    assert invoked_with == ["s1"]
