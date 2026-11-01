import asyncio
import sqlite3

import pytest

from app.application.agent_run_service import AgentRunService
from app.domain.agent_run import RunOwnershipLost, bind_run, check_current_run
from app.infrastructure.registry.sqlite_agent_run_registry import SQLiteAgentRunRegistry


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "runs.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO sessions VALUES ('session')")
        conn.execute("CREATE TABLE task_runs(id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO task_runs VALUES (7)")
        conn.execute("CREATE TABLE scheduled_task_executions(id TEXT PRIMARY KEY,session_id TEXT)")
        conn.execute("INSERT INTO scheduled_task_executions VALUES ('schedule','session')")
        conn.execute("CREATE TABLE delegation_members(id TEXT PRIMARY KEY)")
    return SQLiteAgentRunRegistry(path)


@pytest.mark.asyncio
async def test_expired_pending_cancel_returns_conflict_without_claiming_success(registry):
    import httpx
    from fastapi import FastAPI
    from app.interfaces.http.agent_run_routes import create_agent_run_router

    service = AgentRunService(registry)
    binding = await service.register("expired", "session", "api", "conversation", foreground=True)
    with registry.connect() as conn:
        conn.execute("UPDATE agent_runs SET lease_expires_at=0 WHERE id='expired'")
    app = FastAPI()
    app.include_router(create_agent_run_router(service))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/chat/sessions/session/runs/expired/cancel")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "agent_run_not_locally_cancellable"
        assert service.cancel("expired") is False
        assert service.cancel_session("session") is False
        with pytest.raises(RunOwnershipLost):
            await service.activate(binding)
        assert registry.get("expired")["status"] == "pending"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pending_cancel_does_not_hide_database_failure(registry, monkeypatch):
    service = AgentRunService(registry)
    await service.register("pending", "session", "api", "conversation")

    def unavailable(*args):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(registry, "finish", unavailable)
    try:
        with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
            service.cancel("pending")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_ledger_lifecycle_is_owned_and_terminal_idempotent(registry):
    service = AgentRunService(registry)
    binding = await service.register("one", "session", "api", "conversation", foreground=True)
    assert registry.get("one")["status"] == "pending"
    await service.activate(binding)
    with bind_run(binding):
        await check_current_run()
    await service.finish(binding, "completed", "stop")
    await service.finish(binding, "completed", "stop")
    assert registry.get("one")["status"] == "completed"
    with pytest.raises(RunOwnershipLost):
        await service.check(binding)
    await service.close()


@pytest.mark.asyncio
async def test_foreground_slot_does_not_include_background(registry):
    service = AgentRunService(registry)
    front = await service.register("front", "session", "api", "conversation", foreground=True)
    with pytest.raises(ValueError, match="foreground"):
        await service.register("other", "session", "api", "conversation", foreground=True)
    back = await service.register("back", "session", "task", "worker")
    task = asyncio.create_task(asyncio.sleep(100))
    await service.activate(back, task)
    assert service.cancel_session("session")
    assert registry.get("front")["status"] == "cancelled"
    assert not task.cancelled()
    assert service.cancel("back")
    with pytest.raises(asyncio.CancelledError):
        await task
    await service.finish(back, "cancelled", "cancelled")
    await service.close()


@pytest.mark.asyncio
async def test_expired_owner_is_fenced_and_end_time_remains_unknown(registry):
    service = AgentRunService(registry)
    pending = await service.register("pending", "session", "api", "conversation")
    running = await service.register("running", "session", "task", "worker")
    await service.activate(running)
    with registry.connect() as conn:
        conn.execute("UPDATE agent_runs SET lease_expires_at=0")
    assert registry.reconcile_expired() == 2
    assert registry.get("pending")["status"] == "not_started"
    row = registry.get("running")
    assert row["status"] == "interrupted"
    assert row["ended_at"] is None and row["interrupted_at"] is not None
    assert registry.reconcile_expired() == 0
    with pytest.raises(RunOwnershipLost):
        await service.finish(running, "completed", "stop")
    with pytest.raises(RunOwnershipLost):
        await service.check(pending)
    await service.close()


@pytest.mark.asyncio
async def test_parent_retry_and_business_owners_are_separate(registry):
    service = AgentRunService(registry)
    first = await service.register("first", "session", "api", "conversation")
    await service.finish(first, "failed", "error")
    second = await service.register("second", "session", "task", "worker", parent_run_id="first", retry_of_run_id="first", task_run_id=7)
    row = registry.get("second")
    assert row["parent_run_id"] == row["retry_of_run_id"] == "first"
    assert row["task_run_id"] == 7
    with pytest.raises(ValueError, match="owner"):
        await service.register("invalid", "session", "task", "worker", task_run_id=7, schedule_execution_id="schedule")
    await service.finish(second, "completed", "stop")
    await service.close()


def test_trace_link_identity_and_idempotence(registry):
    registry.register(run_id="one", session_id="session", source="api", execution_kind="conversation", process_id="process", owner_token="token", lease_seconds=30)
    trace = "a" * 32
    registry.add_trace_link("agent_run", "one", trace)
    registry.add_trace_link("agent_run", "one", trace)
    assert len(registry.trace_links("agent_run", "one")) == 1
    assert registry.find_by_trace(trace)[0]["biz_id"] == "one"
    with pytest.raises(ValueError):
        registry.add_trace_link("agent_run", "missing", trace)
    with pytest.raises(ValueError):
        registry.add_trace_link("agent_run", "one", "0" * 32)


@pytest.mark.asyncio
async def test_foreign_business_owner_is_rejected_before_run_creation(registry):
    service = AgentRunService(registry)
    with pytest.raises(ValueError, match="business"):
        await service.register("bad", "session", "task", "worker", task_run_id=999)
    assert registry.get("bad") is None
    await service.close()


@pytest.mark.asyncio
async def test_other_process_active_run_survives_start_and_pending_close_is_known(registry):
    other = AgentRunService(registry)
    active = await other.register("other", "session", "task", "worker")
    await other.activate(active)
    own = AgentRunService(registry)
    await own.start()
    assert registry.get("other")["status"] == "running"
    await own.register("pending", "session", "api", "conversation")
    await own.close()
    assert registry.get("pending")["status"] == "not_started"
    assert registry.get("pending")["ended_at"] is None
    await other.finish(active, "completed", "stop")
    await other.close()


@pytest.mark.asyncio
async def test_failed_terminal_write_fences_execution_and_does_not_claim_success(registry, monkeypatch):
    service = AgentRunService(registry)
    binding = await service.register("one", "session", "api", "conversation")
    await service.activate(binding)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")
    monkeypatch.setattr(registry, "finish", unavailable)
    with pytest.raises(sqlite3.OperationalError):
        await service.finish(binding, "completed", "stop")
    assert registry.get("one")["status"] == "running"
    assert service.is_cancelled("one")
    with bind_run(binding), pytest.raises(RunOwnershipLost):
        await check_current_run()
    await service.close()


@pytest.mark.asyncio
async def test_failed_heartbeat_cancels_owner_without_fabricating_terminal_result(registry, monkeypatch):
    service = AgentRunService(registry, lease_seconds=1, heartbeat_seconds=0.01)
    binding = await service.register("one", "session", "task", "worker")
    task = asyncio.create_task(asyncio.sleep(100))
    await service.activate(binding, task)
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")
    monkeypatch.setattr(registry, "heartbeat", unavailable)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert service.is_cancelled("one")
    assert registry.get("one")["status"] == "running"
    assert registry.get("one")["ended_at"] is None
    await service.close()


@pytest.mark.asyncio
async def test_shutdown_cleanup_failure_still_stops_monitor_and_other_pending(registry, monkeypatch):
    service = AgentRunService(registry)
    await service.register("broken", "session", "api", "conversation")
    await service.register("other", "session", "task", "worker")
    abandon = registry.abandon_pending

    def fail_one(binding):
        if binding.run_id == "broken":
            raise sqlite3.OperationalError("database unavailable")
        abandon(binding)

    monkeypatch.setattr(registry, "abandon_pending", fail_one)
    await service.close()
    assert service._monitor.done()
    assert registry.get("broken")["status"] == "pending"
    assert registry.get("other")["status"] == "not_started"


@pytest.mark.asyncio
async def test_closed_service_rejects_activation_and_execution_checks(registry):
    service = AgentRunService(registry)
    binding = await service.register("one", "session", "api", "conversation")
    service.close_nowait()
    try:
        with pytest.raises(RuntimeError, match="closed"):
            await service.activate(binding)
        with pytest.raises(RuntimeError, match="closed"):
            await service.check(binding)
        # A result that is already known can still be submitted on shutdown.
        await service.finish(binding, "cancelled", "shutdown_before_start")
    finally:
        if service._monitor is not None:
            try:
                await service._monitor
            except asyncio.CancelledError:
                pass
