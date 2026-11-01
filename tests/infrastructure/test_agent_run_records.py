import sqlite3

import pytest

from app.domain.session import ConversationMessage, ConversationSession, Summary, TaskState, ToolCall
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.domain.agent_run import RunBinding, RunOwnershipLost, bind_run
from app.domain.usage import CanonicalUsage, UsageCost
from app.application.runtime_memory_service import RuntimeMemoryService
from app.infrastructure.registry.sqlite_agent_run_registry import SQLiteAgentRunRegistry
from app.infrastructure.usage.sqlite_usage_recorder import SqliteUsageRecorder


@pytest.mark.asyncio
async def test_run_message_and_tool_preserve_identity_and_reject_other_session(tmp_path):
    path = tmp_path / "agent.db"
    store = SQLiteMemoryStore(path)
    await store.create_session(ConversationSession(id="a"))
    await store.create_session(ConversationSession(id="b"))
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE agent_runs (id TEXT PRIMARY KEY, session_id TEXT, status TEXT, owner_token TEXT, lease_expires_at REAL)")
        conn.execute("INSERT INTO agent_runs VALUES ('run-a', 'a', 'completed', 'token', 0)")
    message = ConversationMessage(role="assistant", content="answer", run_id="run-a", origin_kind="agent")
    await store.append_message("a", message)
    assert (await store.list_messages("a"))[0].run_id == "run-a"
    assert (await store.list_messages("a"))[0].origin_kind == "agent"
    with pytest.raises(ValueError):
        await store.append_message("b", message)
    call = ToolCall(id="tool-a", session_id="a", tool_name="read", arguments={}, run_id="run-a")
    await store.save_tool_call(call)
    assert (await store.list_tool_calls("a"))[0].run_id == "run-a"
    with pytest.raises(ValueError):
        await store.save_tool_call(ToolCall(id="bad", session_id="b", tool_name="read", arguments={}, run_id="run-a"))


@pytest.mark.asyncio
async def test_new_notification_is_not_invented_as_agent_execution(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "agent.db")
    await store.append_message("a", ConversationMessage(role="system", content="done"))
    message = (await store.list_messages("a"))[0]
    assert message.run_id is None
    assert message.origin_kind == "notification"


@pytest.mark.asyncio
async def test_runtime_records_use_binding_and_fence_late_writes(tmp_path):
    path = tmp_path / "agent.db"
    store = SQLiteMemoryStore(path)
    await store.create_session(ConversationSession(id="a"))
    registry = SQLiteAgentRunRegistry(path)
    binding = RunBinding("run-a", "a", "owner", "process")
    registry.register(run_id="run-a", session_id="a", source="api", execution_kind="chat",
                      process_id="process", owner_token="owner", lease_seconds=60)
    registry.activate(binding, 60)
    recorder = SqliteUsageRecorder(str(path))
    await recorder.init()
    memory = RuntimeMemoryService(store)
    with bind_run(binding):
        await memory.append_user_message("a", "hello")
        await memory.append_assistant_message("a", "answer")
        await recorder.record_call("a", "model", "provider", CanonicalUsage(input_tokens=5), UsageCost("0", "unknown"), 1)
    assert {m.run_id for m in await store.list_messages("a")} == {"run-a"}
    assert (await recorder.list_records("a"))[0].run_id == "run-a"
    with pytest.raises(ValueError, match="session_has_active_agent_runs"):
        await store.delete_session("a")
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE agent_runs SET lease_expires_at=0")
    with bind_run(binding):
        with pytest.raises(RunOwnershipLost):
            await memory.append_assistant_message("a", "late")
        with pytest.raises(RunOwnershipLost):
            await recorder.record_call("a", "model", "provider", CanonicalUsage(input_tokens=5), UsageCost("0", "unknown"), 1)
    assert len(await store.list_messages("a")) == 2
    assert len(await recorder.list_records("a")) == 1
    registry.reconcile_expired()
    assert await store.delete_session("a")
    assert registry.get("run-a") is None
    with bind_run(binding), pytest.raises(ValueError, match="agent_run_session_mismatch"):
        await memory.append_assistant_message("a", "deleted session")
    assert await store.get_session("a") is None


@pytest.mark.asyncio
async def test_migration_marks_old_message_unknown_without_assigning_run(tmp_path):
    path = tmp_path / "agent.db"
    store = SQLiteMemoryStore(path)
    await store.append_message("a", ConversationMessage(role="user", content="old"))
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE messages DROP COLUMN origin_kind")
    store.initialize()
    message = (await store.list_messages("a"))[0]
    assert message.run_id is None
    assert message.origin_kind == "legacy_unknown"


@pytest.mark.asyncio
async def test_expired_owner_cannot_overwrite_session_projections(tmp_path):
    path = tmp_path / "agent.db"
    store = SQLiteMemoryStore(path)
    await store.create_session(ConversationSession(id="a"))
    registry = SQLiteAgentRunRegistry(path)
    binding = RunBinding("run-a", "a", "owner", "process")
    registry.register(run_id="run-a", session_id="a", source="task", execution_kind="judge",
                      process_id="process", owner_token="owner", lease_seconds=60)
    await store.save_task_state(TaskState(session_id="a", status="completed"))
    await store.save_summary(Summary(session_id="a", summary="current"))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE agent_runs SET lease_expires_at=0")
    with bind_run(binding):
        with pytest.raises(RunOwnershipLost):
            await store.save_task_state(TaskState(session_id="a", status="failed"))
        with pytest.raises(RunOwnershipLost):
            await store.save_summary(Summary(session_id="a", summary="stale"))
        # Unrelated Session work and notifications are not fenced by this Run.
        await store.save_task_state(TaskState(session_id="b", status="idle"))
    assert (await store.get_task_state("a")).status == "completed"
    assert (await store.get_summary("a")).summary == "current"
