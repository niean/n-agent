from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.application.sandbox_dashboard_service import SandboxDashboardService
from app.domain.sandbox import ReleasedSandboxInfo, SandboxExecutionHistoryEntry
from app.domain.session import ConversationMessage, ConversationSession, ToolCall
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.sandbox.history_registry import SQLiteSandboxExecutionHistoryRegistry


def _service(tmp_path, memory_store, history_registry):
    return SandboxDashboardService(
        sandbox_manager=None,
        memory_store=memory_store,
        settings=SimpleNamespace(),
        history_registry=history_registry,
    )


@pytest.mark.asyncio
async def test_execute_code_history_survives_session_delete(tmp_path):
    db_path = tmp_path / "sessions.db"
    memory_store = SQLiteMemoryStore(db_path)
    history_registry = SQLiteSandboxExecutionHistoryRegistry(db_path)
    service = _service(tmp_path, memory_store, history_registry)
    created_at = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)
    await memory_store.create_session(ConversationSession(id="s1"))
    msg = await memory_store.append_message("s1", ConversationMessage(role="user", content="run"))
    await memory_store.save_tool_call(ToolCall(
        id="tc-1",
        session_id="s1",
        message_id=msg.id,
        tool_name="execute_code",
        arguments={"code": "print(1)", "code_hash": "hash-1"},
        result={"status": "success"},
        status="success",
        duration_ms=10,
        created_at=created_at,
    ))
    history_registry.record(SandboxExecutionHistoryEntry(
        id="tc-1",
        session_id="s1",
        code_hash="hash-1",
        code="print(1)",
        result={"status": "success"},
        status="success",
        duration_ms=10,
        authorized_callback_tools=[],
        created_at=created_at,
    ))

    await memory_store.delete_session("s1")
    rows = await service.list_execute_code_history(limit=50)

    assert [row["id"] for row in rows] == ["tc-1"]
    assert rows[0]["arguments"] == {"code": "print(1)", "code_hash": "hash-1"}
    assert rows[0]["result"] == {"status": "success"}


@pytest.mark.asyncio
async def test_delete_execute_code_history_deletes_sandbox_history_and_legacy_tool_call(tmp_path):
    db_path = tmp_path / "sessions.db"
    memory_store = SQLiteMemoryStore(db_path)
    history_registry = SQLiteSandboxExecutionHistoryRegistry(db_path)
    service = _service(tmp_path, memory_store, history_registry)
    created_at = datetime.now(timezone.utc)
    await memory_store.save_tool_call(ToolCall(
        id="tc-1",
        session_id="s1",
        tool_name="execute_code",
        arguments={"code": "print(1)", "code_hash": "hash-1"},
        result={"status": "success"},
        status="success",
        duration_ms=10,
        created_at=created_at,
    ))
    history_registry.record(SandboxExecutionHistoryEntry(
        id="tc-1",
        session_id="s1",
        code_hash="hash-1",
        code="print(1)",
        result={"status": "success"},
        status="success",
        duration_ms=10,
        authorized_callback_tools=[],
        created_at=created_at,
    ))

    result = await service.delete_execute_code_history("tc-1")

    assert result == {"ok": True}
    assert await service.list_execute_code_history(limit=50) == []


@pytest.mark.asyncio
async def test_list_released_sandboxes_returns_id(tmp_path):
    now = datetime.now(timezone.utc)
    info = ReleasedSandboxInfo(
        session_id="s1",
        sandbox_type="docker",
        sandbox_id="nagent-sandbox-s1",
        created_at=now,
        released_at=now,
        reason="manual",
        id="rec-1",
    )
    manager = SimpleNamespace(
        list_released=lambda: [info],
        delete_released=lambda entry_id: entry_id == "rec-1",
    )
    service = SandboxDashboardService(
        sandbox_manager=manager,
        memory_store=None,
        settings=SimpleNamespace(),
        history_registry=None,
    )

    rows = await service.list_released_sandboxes()
    assert rows == [{
        "id": "rec-1",
        "session_id": "s1",
        "sandbox_type": "docker",
        "sandbox_id": "nagent-sandbox-s1",
        "created_at": now.isoformat(),
        "released_at": now.isoformat(),
        "reason": "manual",
    }]


@pytest.mark.asyncio
async def test_delete_released_sandbox_delegates_to_manager(tmp_path):
    deleted_args = []

    def fake_delete(entry_id):
        deleted_args.append(entry_id)
        return entry_id == "rec-1"

    manager = SimpleNamespace(delete_released=fake_delete)
    service = SandboxDashboardService(
        sandbox_manager=manager,
        memory_store=None,
        settings=SimpleNamespace(),
        history_registry=None,
    )

    result = await service.delete_released_sandbox("rec-1")

    assert result == {"ok": True}
    assert deleted_args == ["rec-1"]


@pytest.mark.asyncio
async def test_delete_released_sandbox_unknown_id_returns_ok_false(tmp_path):
    manager = SimpleNamespace(delete_released=lambda entry_id: False)
    service = SandboxDashboardService(
        sandbox_manager=manager,
        memory_store=None,
        settings=SimpleNamespace(),
        history_registry=None,
    )

    result = await service.delete_released_sandbox("nonexistent")

    assert result == {"ok": False}


@pytest.mark.asyncio
async def test_delete_released_sandbox_without_manager_returns_error(tmp_path):
    service = SandboxDashboardService(
        sandbox_manager=None,
        memory_store=None,
        settings=SimpleNamespace(),
        history_registry=None,
    )

    result = await service.delete_released_sandbox("rec-1")

    assert result == {"ok": False, "error": "sandbox not enabled"}


@pytest.mark.asyncio
async def test_delete_released_sandbox_rejects_empty_id(tmp_path):
    manager = SimpleNamespace(delete_released=lambda entry_id: True)
    service = SandboxDashboardService(
        sandbox_manager=manager,
        memory_store=None,
        settings=SimpleNamespace(),
        history_registry=None,
    )

    result = await service.delete_released_sandbox("")

    assert result == {"ok": False, "error": "entry_id required"}
