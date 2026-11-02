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


@pytest.mark.asyncio
async def test_run_footer_counts_all_tool_calls_per_run_on_real_records(tmp_path):
    path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(path)
    await store.create_session(ConversationSession(id="a"))
    await store.create_session(ConversationSession(id="b"))
    registry = SQLiteAgentRunRegistry(path)
    registry.register(run_id="run-parent", session_id="a", source="api", execution_kind="chat",
                      process_id="process", owner_token="owner", lease_seconds=60)
    registry.register(run_id="run-child", session_id="a", source="api", execution_kind="chat",
                      process_id="process", owner_token="owner", lease_seconds=60, parent_run_id="run-parent")
    registry.register(run_id="run-zero", session_id="a", source="api", execution_kind="chat",
                      process_id="process", owner_token="owner", lease_seconds=60)
    registry.register(run_id="run-other", session_id="b", source="api", execution_kind="chat",
                      process_id="process", owner_token="owner", lease_seconds=60)
    parent_binding = RunBinding("run-parent", "a", "owner", "process")
    registry.activate(parent_binding, 60)
    registry.finish(parent_binding, "completed", "done")
    child_binding = RunBinding("run-child", "a", "owner", "process")
    registry.activate(child_binding, 60)
    registry.finish(child_binding, "failed", "tool error")
    # 同名多次不去重；失败/执行中记录同样计数；父子 Run 各自独立计数。
    for index in range(3):
        await store.save_tool_call(ToolCall(id=f"tc-read-{index}", session_id="a", tool_name="read", arguments={}, run_id="run-parent"))
    await store.save_tool_call(ToolCall(id="tc-failed", session_id="a", tool_name="read", arguments={}, status="failed", run_id="run-parent"))
    await store.save_tool_call(ToolCall(id="tc-running", session_id="a", tool_name="write", arguments={}, status="running", run_id="run-parent"))
    await store.save_tool_call(ToolCall(id="tc-child", session_id="a", tool_name="read", arguments={}, run_id="run-child"))
    await store.save_tool_call(ToolCall(id="tc-other", session_id="b", tool_name="read", arguments={}, run_id="run-other"))
    # 其它会话的脏数据（run 归属他会的 run-parent）不得混入本会话计数。
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO tool_calls(id,session_id,tool_name,arguments_json,status,duration_ms,created_at,run_id) "
                     "VALUES ('tc-foreign','b','read','{}','completed',0,'2026-01-01T00:00:00+00:00','run-parent')")
    assert registry.get("run-zero")["tool_call_count"] == 0
    assert registry.get("run-parent")["tool_call_count"] == 5
    assert registry.get("run-child")["tool_call_count"] == 1
    assert registry.get("run-other")["tool_call_count"] == 1
    assert registry.get("missing") is None
    listed = registry.list_for_session("a")
    assert [row["id"] for row in listed] == ["run-parent", "run-child", "run-zero"]
    assert {row["id"]: row["tool_call_count"] for row in listed} == {"run-parent": 5, "run-child": 1, "run-zero": 0}
    for row in listed:
        assert row["tool_call_count"] == registry.get(row["id"])["tool_call_count"]
    assert registry.list_for_session("no-such-session") == []


@pytest.mark.asyncio
async def test_run_footer_count_batches_reads_beyond_single_query_param_limit(tmp_path, monkeypatch):
    path = tmp_path / "sessions.db"
    store = SQLiteMemoryStore(path)
    await store.create_session(ConversationSession(id="a"))
    registry = SQLiteAgentRunRegistry(path)
    run_ids = [f"run-{index:04d}" for index in range(600)]
    for run_id in run_ids:
        registry.register(run_id=run_id, session_id="a", source="api", execution_kind="chat",
                          process_id="process", owner_token="owner", lease_seconds=60)
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO tool_calls(id,session_id,tool_name,arguments_json,status,duration_ms,created_at,run_id) VALUES (?,?,?,?,?,?,?,?)",
            [(f"tc-{run_id}", "a", "read", "{}", "completed", 0, "2026-01-01T00:00:00+00:00", run_id) for run_id in run_ids],
        )
    statements = []
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    try:
        rows = registry.list_for_session("a")
    finally:
        monkeypatch.undo()
    assert {row["id"]: row["tool_call_count"] for row in rows} == {run_id: 1 for run_id in run_ids}
    with sqlite3.connect(path) as conn:
        expected_order = [row[0] for row in conn.execute("SELECT id FROM agent_runs WHERE session_id=? ORDER BY created_at,id", ("a",))]
    assert [row["id"] for row in rows] == expected_order
    count_queries = [statement for statement in statements if "FROM tool_calls" in statement]
    # 600 条 run 超过单条查询参数上限（LIMIT 500）：必须分批读取且未退化为逐条查询，
    # 不钉住具体批次数。
    assert len(count_queries) >= 2


def test_run_footer_bare_registry_without_tool_calls_table_counts_zero(tmp_path):
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL, external_memory_enabled_json TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('a','t','2026-01-01','2026-01-01','api',NULL)")
    registry = SQLiteAgentRunRegistry(path)
    registry.register(run_id="run-a", session_id="a", source="api", execution_kind="chat",
                      process_id="process", owner_token="owner", lease_seconds=60)
    assert registry.get("run-a")["tool_call_count"] == 0
    rows = registry.list_for_session("a")
    assert [row["id"] for row in rows] == ["run-a"]
    assert [row["tool_call_count"] for row in rows] == [0]
    assert registry.list_for_session("no-such-session") == []


def test_run_footer_count_does_not_swallow_sql_errors_when_tool_calls_exists(tmp_path):
    path = tmp_path / "sessions.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source TEXT NOT NULL, external_memory_enabled_json TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('a','t','2026-01-01','2026-01-01','api',NULL)")
    registry = SQLiteAgentRunRegistry(path)
    registry.register(run_id="run-a", session_id="a", source="api", execution_kind="chat",
                      process_id="process", owner_token="owner", lease_seconds=60)
    # tool_calls 表存在但缺少 run_id 列：计数查询必须报错，而不是吞成 0。
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE tool_calls (id TEXT PRIMARY KEY, session_id TEXT NOT NULL)")
    with pytest.raises(sqlite3.OperationalError):
        registry.get("run-a")
    with pytest.raises(sqlite3.OperationalError):
        registry.list_for_session("a")
