import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.gateway import GatewayHomeTarget, GatewaySessionKey
from app.domain.platform import Platform
from app.infrastructure.registry.sqlite_gateway_registry import SQLiteGatewaySessionRegistry


@pytest.mark.asyncio
async def test_gateway_registry_persists_and_switches_home_target(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")

    await registry.set_home_target(GatewayHomeTarget(Platform.FEISHU, "oc_old", "chat_id", display_name="Old"))
    await registry.set_home_target(GatewayHomeTarget(Platform.FEISHU, "oc_new", "chat_id", thread_id="thread-1", display_name="New"))

    target = await registry.get_home_target(Platform.FEISHU)
    assert target is not None
    assert target.receive_id == "oc_new"
    assert target.thread_id == "thread-1"
    assert target.display_name == "New"


@pytest.mark.asyncio
async def test_gateway_registry_creates_and_switches_active_session(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    key = GatewaySessionKey(Platform.CLI, "local", display_name="Local CLI")

    first = await registry.create_session_link(key, "session-1")
    second = await registry.create_session_link(key, "session-2")

    active = await registry.get_active_session(key)
    assert active is not None
    assert active.session_id == second.session_id
    assert active.display_name == "Local CLI"

    switched = await registry.set_active_session(key, first.session_id)
    links = await registry.list_session_links(key)

    assert switched.session_id == first.session_id
    assert [link.session_id for link in links] == ["session-1", "session-2"]


@pytest.mark.asyncio
async def test_gateway_registry_returns_none_for_missing_active_session(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    key = GatewaySessionKey(Platform.FEISHU, "chat-1")

    assert await registry.get_active_session(key) is None
    assert await registry.list_session_links(key) == []


@pytest.mark.asyncio
async def test_gateway_registry_marks_events_once(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")

    assert await registry.mark_event_processed(Platform.FEISHU, "event-1", "message-1") is True
    assert await registry.mark_event_processed(Platform.FEISHU, "event-1", "message-2") is False
    assert await registry.mark_event_processed(Platform.FEISHU, "event-2", "message-1") is False


@pytest.mark.asyncio
async def test_gateway_registry_allows_distinct_events_without_message_id(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")

    assert await registry.mark_event_processed(Platform.FEISHU, "event-1") is True
    assert await registry.mark_event_processed(Platform.FEISHU, "event-2") is True
    assert await registry.mark_event_processed(Platform.FEISHU, "event-1") is False


@pytest.mark.asyncio
async def test_gateway_registry_migrates_legacy_source_columns_to_platform(tmp_path):
    db_path = tmp_path / "sessions.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE gateway_conversations (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL,
                active_session_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_type, source_id, thread_id)
            );
            CREATE TABLE gateway_session_links (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(conversation_id, session_id)
            );
            CREATE TABLE gateway_processed_events (
                id TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                event_id TEXT NOT NULL,
                message_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(source_type, event_id),
                UNIQUE(source_type, message_id)
            );
            INSERT INTO gateway_conversations VALUES (
                'conv-legacy', 'feishu', 'oc_legacy', '', 'Legacy', NULL,
                '2026-06-15T00:00:00+00:00', '2026-06-15T00:00:00+00:00'
            );
            INSERT INTO gateway_processed_events VALUES (
                'evt-legacy', 'feishu', 'event-legacy', NULL, '2026-06-15T00:00:00+00:00'
            );
            """
        )

    registry = SQLiteGatewaySessionRegistry(db_path)

    with sqlite3.connect(db_path) as conn:
        conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(gateway_conversations)").fetchall()}
        proc_cols = {row[1] for row in conn.execute("PRAGMA table_info(gateway_processed_events)").fetchall()}
        assert "platform" in conv_cols and "platform_session_id" in conv_cols
        assert "source_type" not in conv_cols and "source_id" not in conv_cols
        assert "platform" in proc_cols and "source_type" not in proc_cols

    legacy_key = GatewaySessionKey(Platform.FEISHU, "oc_legacy")
    assert await registry.get_active_session(legacy_key) is None
    assert await registry.mark_event_processed(Platform.FEISHU, "event-legacy") is False
    assert await registry.mark_event_processed(Platform.FEISHU, "event-fresh") is True


@pytest.mark.asyncio
async def test_gateway_registry_legacy_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "sessions.db"
    SQLiteGatewaySessionRegistry(db_path)
    SQLiteGatewaySessionRegistry(db_path)

    registry = SQLiteGatewaySessionRegistry(db_path)
    assert await registry.mark_event_processed(Platform.FEISHU, "event-1") is True


@pytest.mark.asyncio
async def test_gateway_registry_lists_and_counts_conversations_by_platform(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    feishu_key = GatewaySessionKey(Platform.FEISHU, "oc_a", display_name="A")
    cli_key = GatewaySessionKey(Platform.CLI, "local")

    await registry.create_session_link(feishu_key, "s-1")
    await registry.create_session_link(GatewaySessionKey(Platform.FEISHU, "oc_b"), "s-2")
    await registry.create_session_link(cli_key, "s-3")

    feishu_convs = await registry.list_conversations(Platform.FEISHU)
    cli_convs = await registry.list_conversations(Platform.CLI)
    all_convs = await registry.list_conversations()

    assert {c.platform_session_id for c in feishu_convs} == {"oc_a", "oc_b"}
    assert {c.platform for c in feishu_convs} == {Platform.FEISHU}
    assert [c.platform_session_id for c in cli_convs] == ["local"]
    assert len(all_convs) == 3

    assert await registry.count_conversations(Platform.FEISHU) == 2
    assert await registry.count_conversations(Platform.CLI) == 1
    assert await registry.count_conversations(Platform.DINGTALK) == 0


@pytest.mark.asyncio
async def test_gateway_registry_get_last_active_returns_max_updated_at(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    assert await registry.get_last_active(Platform.FEISHU) is None

    await registry.create_session_link(GatewaySessionKey(Platform.FEISHU, "oc_a"), "s-1")
    last = await registry.get_last_active(Platform.FEISHU)
    assert isinstance(last, datetime)
    assert datetime.now(timezone.utc) - last < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_gateway_registry_deletes_session_link_and_clears_active(tmp_path):
    registry = SQLiteGatewaySessionRegistry(tmp_path / "sessions.db")
    key = GatewaySessionKey(Platform.CLI, "local")
    await registry.create_session_link(key, "session-1")

    await registry.delete_session_link("session-1")

    assert await registry.get_active_session(key) is None
    assert await registry.list_session_links(key) == []
