import pytest

from app.domain.agent_run import RunBinding
from app.domain.session import ConversationSession
from app.domain.task import Task, TaskStatus
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.registry.sqlite_agent_run_registry import SQLiteAgentRunRegistry
from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry
from app.application.agent_run_service import AgentRunService
from app.application.agent_run_retry_policy import AgentRunRetryPolicy


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["task", "member"])
async def test_equal_timestamps_keep_actual_predecessor_insertion_order(tmp_path, owner):
    path = tmp_path / "sessions.db"
    memory = SQLiteMemoryStore(path)
    await memory.create_session(ConversationSession(id="session"))
    ledger = SQLiteAgentRunRegistry(path)
    with ledger.connect() as conn:
        conn.execute("CREATE TABLE task_runs(id INTEGER PRIMARY KEY, task_id TEXT)")
        conn.executemany("INSERT INTO task_runs VALUES (?, 'task')", [(1,), (2,)])
        conn.execute("CREATE TABLE delegation_members(id TEXT PRIMARY KEY, execution_session_id TEXT)")
        conn.execute("INSERT INTO delegation_members VALUES ('member', 'session')")
    older, newer = "f" * 32, "0" * 31 + "1"
    kind = "task_worker" if owner == "task" else "delegation_worker"
    for run_id in (older, newer):
        ledger.register(
            run_id=run_id, session_id="session", source=owner, execution_kind=kind,
            process_id="process", owner_token="token", lease_seconds=60,
            task_run_id=1 if owner == "task" else None,
            delegation_member_id="member" if owner == "member" else None,
        )
    with ledger.connect() as conn:
        conn.execute("UPDATE agent_runs SET created_at=1")
    facts = ledger.retry_facts(2 if owner == "task" else None, "member" if owner == "member" else None)
    assert AgentRunRetryPolicy().resolve(kind, facts) == newer
    if owner == "task":
        assert [row.run_id for row in ledger.retry_facts(1, None).current_attempt_runs] == [newer, older]


@pytest.mark.asyncio
@pytest.mark.parametrize("assignment,expected", [("assigned", "assigned"), ("", "delegation-member")])
async def test_member_session_must_match_persisted_assignment(tmp_path, assignment, expected):
    path = tmp_path / "sessions.db"
    memory = SQLiteMemoryStore(path)
    await memory.create_session(ConversationSession(id=expected))
    await memory.create_session(ConversationSession(id="wrong"))
    ledger = SQLiteAgentRunRegistry(path)
    with ledger.connect() as conn:
        conn.execute("CREATE TABLE delegation_members(id TEXT PRIMARY KEY, execution_session_id TEXT)")
        conn.execute("INSERT INTO delegation_members VALUES ('member', ?)", (assignment,))
    with pytest.raises(ValueError, match="business_session_mismatch"):
        ledger.register(run_id="bad", session_id="wrong", source="delegation",
                        execution_kind="delegation_worker", process_id="process", owner_token="token",
                        lease_seconds=60, delegation_member_id="member")
    ledger.register(run_id="good", session_id=expected, source="delegation",
                    execution_kind="delegation_worker", process_id="process", owner_token="token",
                    lease_seconds=60, delegation_member_id="member")
    assert ledger.get("good")["session_id"] == expected


@pytest.mark.asyncio
async def test_task_retry_links_previous_attempt_but_goal_continuation_does_not(tmp_path):
    path = tmp_path / "sessions.db"
    memory = SQLiteMemoryStore(path)
    await memory.create_session(ConversationSession(id="session"))
    tasks = SQLiteTaskRegistry(str(path))
    ledger = SQLiteAgentRunRegistry(path)
    await tasks.create_task(Task(id="task", title="goal"))
    first = await tasks.claim_task("task", "claim1", 60)
    service = AgentRunService(ledger)

    async def register(run_id, business_run_id, kind="task_worker"):
        binding = await service.register(run_id=run_id, session_id="session", source="task",
                                         execution_kind=kind, task_run_id=business_run_id)
        await service.finish(binding, "completed", "stop")

    await register("worker1", first.run.id)
    await register("judge1", first.run.id, "task_judge")
    await register("worker2", first.run.id)
    assert ledger.get("worker2")["retry_of_run_id"] is None
    await tasks.update_task("task", {"status": TaskStatus.QUEUED,
        "claim_lock": None, "claim_expires": None}, first.task.version)
    second = await tasks.claim_task("task", "claim2", 60)
    await register("retry-worker", second.run.id)
    assert ledger.get("retry-worker")["retry_of_run_id"] == "worker2"
    await register("retry-judge", second.run.id, "task_judge")
    assert ledger.get("retry-judge")["retry_of_run_id"] is None
    await tasks.update_task("task", {"status": TaskStatus.QUEUED,
        "claim_lock": None, "claim_expires": None}, second.task.version)
    third = await tasks.claim_task("task", "claim3", 60)
    ledger.register(run_id="explicit-no-retry", session_id="session", source="task",
                    execution_kind="task_worker", process_id="process", owner_token="token",
                    lease_seconds=60, task_run_id=third.run.id, retry_of_run_id=None)
    assert ledger.get("explicit-no-retry")["retry_of_run_id"] is None
    await service.close()


@pytest.mark.asyncio
async def test_task_delete_retains_explicit_historical_run_owner_and_removes_trace_link(tmp_path):
    path = tmp_path / "sessions.db"
    memory = SQLiteMemoryStore(path)
    await memory.create_session(ConversationSession(id="session"))
    tasks = SQLiteTaskRegistry(str(path))
    ledger = SQLiteAgentRunRegistry(path)
    await tasks.create_task(Task(id="task", title="goal"))
    claimed = await tasks.claim_task("task", "claim", 60)
    ledger.register(run_id="worker", session_id="session", source="task",
        execution_kind="task_worker", process_id="process", owner_token="token",
        lease_seconds=60, task_run_id=claimed.run.id)
    ledger.add_trace_link("task_run", str(claimed.run.id), "1" * 32)
    assert await tasks.delete_task("task")
    retained = ledger.get("worker")
    assert retained["task_run_id"] == claimed.run.id
    assert retained["business_owner_deleted_at"] is not None
    assert ledger.trace_links("task_run", str(claimed.run.id)) == []
