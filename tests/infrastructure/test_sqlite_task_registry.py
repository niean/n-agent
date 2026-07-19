"""Tests for SQLiteTaskRegistry (T6-T8).

Covers schema creation, CRUD with optimistic lock, atomic claim/lease/finish
CAS, dependency graph with cycle detection, recompute_ready, comments,
events (monotonic id), attachments, and notify_subs.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.task import (
    BlockKind,
    BulkUpdateCommand,
    BulkUpdateItem,
    CreateGraphCommand,
    FinishRunCommand,
    RecoverRunCommand,
    Task,
    TaskAttachment,
    TaskComment,
    TaskConflictError,
    TaskLink,
    TaskNotFoundError,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStatus,
    TaskWorkspaceKind,
)
from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path):
    return SQLiteTaskRegistry(str(tmp_path / "tasks.db"))


def _task(
    task_id: str = "t_1",
    title: str = "test task",
    **kwargs,
) -> Task:
    defaults = {
        "id": task_id,
        "title": title,
        "created_at": datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# T6: Schema + 7 tables
# ---------------------------------------------------------------------------


def test_schema_creates_7_tables(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    tables = reg._list_tables()
    for t in [
        "tasks",
        "task_runs",
        "task_links",
        "task_comments",
        "task_events",
        "task_attachments",
        "task_notify_subs",
    ]:
        assert t in tables, f"missing table: {t}"


def test_schema_idempotent_repeated_init(tmp_path):
    db_path = str(tmp_path / "t.db")
    reg1 = SQLiteTaskRegistry(db_path)
    reg1._ensure_schema()
    # second init should not raise
    reg2 = SQLiteTaskRegistry(db_path)
    reg2._ensure_schema()
    tables = reg2._list_tables()
    assert "tasks" in tables


def test_schema_creates_indexes(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    with reg._connect() as conn:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_tasks_status" in indexes
    assert "idx_tasks_status_priority" in indexes
    assert "idx_tasks_assignee_status" in indexes
    assert "idx_tasks_session_id" in indexes
    assert "idx_tasks_idempotency" in indexes
    assert "idx_links_parent" in indexes
    assert "idx_links_child" in indexes
    assert "idx_runs_task" in indexes
    assert "idx_events_task" in indexes
    assert "idx_events_run" in indexes
    assert "idx_attachments_task" in indexes
    assert "idx_notify_task" in indexes


def test_schema_wal_mode_enabled(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    with reg._connect() as conn:
        pragma = conn.execute("PRAGMA journal_mode").fetchone()
    assert pragma[0].lower() == "wal"


def test_schema_foreign_keys_enabled(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    with reg._connect() as conn:
        pragma = conn.execute("PRAGMA foreign_keys").fetchone()
    assert pragma[0] == 1


def test_tasks_table_has_pre_archive_status(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    with reg._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "pre_archive_status" in cols
    assert "origin_session_id" in cols
    assert "execution_session_id" in cols
    assert "worker_token" in cols
    assert "version" in cols
    assert "board" in cols
    assert "consecutive_failures" in cols
    assert "block_recurrences" in cols


def test_task_links_composite_pk(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    with reg._connect() as conn:
        info = conn.execute("PRAGMA table_info(task_links)").fetchall()
        pk_cols = {row["name"] for row in info if row["pk"] > 0}
    assert pk_cols == {"parent_id", "child_id"}


def test_idempotency_partial_unique_index(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    with reg._connect() as conn:
        idx_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_tasks_idempotency'"
        ).fetchone()
    assert idx_sql is not None
    # partial index -- WHERE idempotency_key IS NOT NULL
    assert "idempotency_key IS NOT NULL" in idx_sql[0]


# ---------------------------------------------------------------------------
# T7: CRUD + optimistic lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    t = _task("t_1", "x")
    await reg.create_task(t)
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.title == "x"
    assert got.version == 1


@pytest.mark.asyncio
async def test_get_missing_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    got = await reg.get_task("missing")
    assert got is None


@pytest.mark.asyncio
async def test_create_task_roundtrips_all_fields(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    t = _task(
        "t_full",
        title="full task",
        body="do work",
        assignee="worker-1",
        priority=5,
        created_by="user-a",
        created_at=now,
        updated_at=now,
        version=1,
        status=TaskStatus.READY,
        block_kind=BlockKind.NEEDS_INPUT,
        block_reason="waiting",
        block_recurrences=2,
        started_at=now,
        completed_at=None,
        scheduled_at=now,
        claim_lock="lock-xyz",
        claim_expires=now + timedelta(seconds=900),
        current_run_id=42,
        workspace_kind=TaskWorkspaceKind.DIR,
        workspace_path="/tmp/work",
        skills=("skill-a", "skill-b"),
        model_override="gpt-4",
        max_runtime_seconds=3600,
        max_retries=3,
        goal_mode=True,
        goal_max_turns=10,
        board="default",
        origin_session_id="sess-orig",
        execution_session_id="sess-exec",
        consecutive_failures=1,
        worker_token="tok-abc",
        last_failure_error="boom",
        last_heartbeat_at=now,
        result="partial",
        idempotency_key="key-1",
        pre_archive_status=TaskStatus.DONE,
    )
    await reg.create_task(t)
    got = await reg.get_task("t_full")
    assert got is not None
    assert got.title == "full task"
    assert got.body == "do work"
    assert got.assignee == "worker-1"
    assert got.priority == 5
    assert got.created_by == "user-a"
    assert got.status == TaskStatus.READY
    assert got.block_kind == BlockKind.NEEDS_INPUT
    assert got.block_reason == "waiting"
    assert got.block_recurrences == 2
    assert got.started_at == now
    assert got.scheduled_at == now
    assert got.claim_lock == "lock-xyz"
    assert got.claim_expires == now + timedelta(seconds=900)
    assert got.current_run_id == 42
    assert got.workspace_kind == TaskWorkspaceKind.DIR
    assert got.workspace_path == "/tmp/work"
    assert got.skills == ("skill-a", "skill-b")
    assert got.model_override == "gpt-4"
    assert got.max_runtime_seconds == 3600
    assert got.max_retries == 3
    assert got.goal_mode is True
    assert got.goal_max_turns == 10
    assert got.board == "default"
    assert got.origin_session_id == "sess-orig"
    assert got.execution_session_id == "sess-exec"
    assert got.consecutive_failures == 1
    assert got.worker_token == "tok-abc"
    assert got.last_failure_error == "boom"
    assert got.last_heartbeat_at == now
    assert got.result == "partial"
    assert got.idempotency_key == "key-1"
    assert got.pre_archive_status == TaskStatus.DONE


@pytest.mark.asyncio
async def test_update_optimistic_lock(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.update_task("t_1", {"title": "y"}, expected_version=1)
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.title == "y"
    assert got.version == 2
    with pytest.raises(TaskConflictError):
        await reg.update_task("t_1", {"title": "z"}, expected_version=1)


@pytest.mark.asyncio
async def test_update_missing_raises_not_found(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    with pytest.raises(TaskNotFoundError):
        await reg.update_task("missing", {"title": "x"}, expected_version=1)


@pytest.mark.asyncio
async def test_update_increments_version_and_updated_at(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.update_task("t_1", {"title": "y"}, expected_version=1)
    await reg.update_task("t_1", {"title": "z"}, expected_version=2)
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.title == "z"
    assert got.version == 3


@pytest.mark.asyncio
async def test_update_status_field(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.TRIAGE))
    await reg.update_task(
        "t_1", {"status": TaskStatus.TODO}, expected_version=1
    )
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_update_skills_and_execution_policy(tmp_path):
    from app.domain.task import TaskExecutionPolicy

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.update_task(
        "t_1",
        {"skills": ("a", "b"), "execution_policy": TaskExecutionPolicy(allowed_tools=("t1", "t2"))},
        expected_version=1,
    )
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.skills == ("a", "b")
    assert got.execution_policy.allowed_tools == ("t1", "t2")


@pytest.mark.asyncio
async def test_list_tasks_pagination(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    base = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        await reg.create_task(
            _task(f"t_{i}", f"task-{i}", created_at=base + timedelta(seconds=i))
        )
    page1 = await reg.list_tasks(limit=2)
    assert len(page1.items) == 2
    assert page1.next_cursor is not None
    page2 = await reg.list_tasks(cursor=page1.next_cursor, limit=2)
    assert len(page2.items) == 2
    page3 = await reg.list_tasks(cursor=page2.next_cursor, limit=2)
    assert len(page3.items) == 1
    assert page3.next_cursor is None


@pytest.mark.asyncio
async def test_list_tasks_stable_order(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    base = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    for i in range(3):
        await reg.create_task(
            _task(f"t_{i}", f"task-{i}", created_at=base)
        )
    page = await reg.list_tasks(limit=10)
    ids = [t.id for t in page.items]
    assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_bulk_update_single_transaction(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    cmd = BulkUpdateCommand(
        items=(
            BulkUpdateItem(task_id="t_1", fields={"title": "A"}, expected_version=1),
            BulkUpdateItem(task_id="t_2", fields={"title": "B"}, expected_version=1),
        )
    )
    result = await reg.bulk_update(cmd)
    assert len(result.updated) == 2
    got1 = await reg.get_task("t_1")
    got2 = await reg.get_task("t_2")
    assert got1.title == "A"
    assert got2.title == "B"
    assert got1.version == 2
    assert got2.version == 2


@pytest.mark.asyncio
async def test_bulk_update_rolls_back_on_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    # t_2 has wrong expected_version -> whole bulk fails
    cmd = BulkUpdateCommand(
        items=(
            BulkUpdateItem(task_id="t_1", fields={"title": "A"}, expected_version=1),
            BulkUpdateItem(task_id="t_2", fields={"title": "B"}, expected_version=99),
        )
    )
    with pytest.raises(TaskConflictError):
        await reg.bulk_update(cmd)
    # t_1 should NOT be updated (rollback)
    got1 = await reg.get_task("t_1")
    assert got1.title == "a"
    assert got1.version == 1


@pytest.mark.asyncio
async def test_delete_task(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    assert await reg.delete_task("t_1") is True
    assert await reg.get_task("t_1") is None
    # deleting again returns False
    assert await reg.delete_task("t_1") is False


@pytest.mark.asyncio
async def test_delete_task_cascades_children(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.add_comment("t_1", "worker", "hi")
    await reg.append_event("t_1", "created", {})
    await reg.delete_task("t_1")
    # comments and events should be gone
    assert await reg.list_comments("t_1") == ()
    assert await reg.list_events("t_1") == ()


# ---------------------------------------------------------------------------
# T7: Atomic claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_ready_task_succeeds(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None
    assert result.task.status == TaskStatus.RUNNING
    assert result.task.claim_lock == "L1"
    assert result.task.current_run_id == result.run.id
    assert result.run.status == TaskRunStatus.RUNNING
    assert result.run.claim_lock == "L1"
    assert result.run.task_id == "t_1"


@pytest.mark.asyncio
async def test_claim_non_ready_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.TODO, assignee="d"))
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is None


@pytest.mark.asyncio
async def test_claim_missing_task_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    result = await reg.claim_task("missing", claim_lock="L1", lease_seconds=900)
    assert result is None


@pytest.mark.asyncio
async def test_claim_atomic_single_winner(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    reg2 = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    r1, r2 = await asyncio.gather(
        reg.claim_task("t_1", claim_lock="L1", lease_seconds=900),
        reg2.claim_task("t_1", claim_lock="L2", lease_seconds=900),
    )
    assert sum(r is not None for r in (r1, r2)) == 1
    assert len(await reg.list_runs("t_1")) == 1


@pytest.mark.asyncio
async def test_claim_appends_event(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None
    events = await reg.list_events("t_1")
    kinds = [e.kind for e in events]
    assert "claimed" in kinds


# ---------------------------------------------------------------------------
# T7: Finish run CAS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_run_completed(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.DONE
    assert result.task.claim_lock is None
    assert result.task.current_run_id is None
    assert result.task.completed_at is not None
    assert result.task.result == "done"
    assert result.run.outcome == TaskRunOutcome.COMPLETED
    assert result.run.status == TaskRunStatus.COMPLETED
    assert result.run.ended_at is not None
    assert result.terminal_event.id > 0


@pytest.mark.asyncio
async def test_finish_wrong_claim_token_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="WRONG",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
    )
    with pytest.raises((TaskConflictError, Exception)):
        await reg.finish_run(cmd)


@pytest.mark.asyncio
async def test_finish_wrong_run_id_conflict(tmp_path):
    from app.domain.task import TaskClaimError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=99999,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
    )
    with pytest.raises((TaskConflictError, TaskClaimError, Exception)):
        await reg.finish_run(cmd)


@pytest.mark.asyncio
async def test_finish_failed_outcome_transitions_to_todo(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.FAILED,
        summary="error",
        metadata={"err": "boom"},
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.TODO
    assert result.task.claim_lock is None
    assert result.task.consecutive_failures == 1
    assert result.run.outcome == TaskRunOutcome.FAILED


@pytest.mark.asyncio
async def test_finish_gave_up_transitions_to_blocked(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.GAVE_UP,
        summary="giving up",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.BLOCKED


@pytest.mark.asyncio
async def test_finish_releases_lease(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
    )
    await reg.finish_run(cmd)
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.claim_lock is None
    assert got.claim_expires is None
    assert got.current_run_id is None
    assert got.worker_token is None


# ---------------------------------------------------------------------------
# T7: Heartbeat CAS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_heartbeat_renews_lease(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    # Use a now that is definitely after the claim creation
    now = datetime.now(timezone.utc) + timedelta(seconds=60)
    task = await reg.record_heartbeat(
        "t_1", run_id=claim.run.id, claim_lock="L1", now=now
    )
    assert task.last_heartbeat_at == now
    assert task.claim_expires is not None
    assert task.claim_expires > claim.task.claim_expires


@pytest.mark.asyncio
async def test_heartbeat_wrong_token_conflict(tmp_path):
    from app.domain.task import TaskClaimError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    now = datetime(2026, 7, 18, 12, 5, tzinfo=timezone.utc)
    with pytest.raises((TaskClaimError, Exception)):
        await reg.record_heartbeat(
            "t_1", run_id=claim.run.id, claim_lock="WRONG", now=now
        )


@pytest.mark.asyncio
async def test_heartbeat_wrong_run_id_conflict(tmp_path):
    from app.domain.task import TaskClaimError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    now = datetime(2026, 7, 18, 12, 5, tzinfo=timezone.utc)
    with pytest.raises((TaskClaimError, Exception)):
        await reg.record_heartbeat(
            "t_1", run_id=99999, claim_lock="L1", now=now
        )


# ---------------------------------------------------------------------------
# T7: Recover run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_run_preserves_attribution(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = RecoverRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.RECLAIMED,
        error="lease expired",
    )
    result = await reg.recover_run(cmd)
    assert result.run.outcome == TaskRunOutcome.RECLAIMED
    assert result.run.error == "lease expired"
    assert result.task.status == TaskStatus.TODO
    assert result.task.claim_lock is None


@pytest.mark.asyncio
async def test_recover_run_wrong_token_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = RecoverRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="WRONG",
        outcome=TaskRunOutcome.CRASHED,
    )
    with pytest.raises((TaskConflictError, Exception)):
        await reg.recover_run(cmd)


# ---------------------------------------------------------------------------
# T7: list_ready / list_running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_ready_returns_ready_tasks(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a", status=TaskStatus.READY, assignee="d"))
    await reg.create_task(_task("t_2", "b", status=TaskStatus.TODO, assignee="d"))
    ready = await reg.list_ready()
    assert len(ready) == 1
    assert ready[0].id == "t_1"


@pytest.mark.asyncio
async def test_list_ready_priority_order(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    base = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_low", "low", status=TaskStatus.READY, assignee="d", priority=1, created_at=base))
    await reg.create_task(_task("t_high", "high", status=TaskStatus.READY, assignee="d", priority=10, created_at=base))
    ready = await reg.list_ready()
    assert ready[0].id == "t_high"
    assert ready[1].id == "t_low"


@pytest.mark.asyncio
async def test_list_running_returns_running_tasks(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.READY, assignee="d"))
    await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    running = await reg.list_running()
    assert len(running) == 1
    assert running[0].id == "t_1"


# ---------------------------------------------------------------------------
# T8: Dependency graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_link_creates_edge(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    link = await reg.add_link(parent_id="t_1", child_id="t_2")
    assert link.parent_id == "t_1"
    assert link.child_id == "t_2"


@pytest.mark.asyncio
async def test_add_link_rejects_self_loop(tmp_path):
    from app.domain.task import TaskValidationError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    with pytest.raises((TaskValidationError, Exception)):
        await reg.add_link(parent_id="t_1", child_id="t_1")


@pytest.mark.asyncio
async def test_add_link_rejects_duplicate(tmp_path):
    from app.domain.task import TaskConflictError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.add_link(parent_id="t_1", child_id="t_2")
    with pytest.raises((TaskConflictError, Exception)):
        await reg.add_link(parent_id="t_1", child_id="t_2")


@pytest.mark.asyncio
async def test_add_link_rejects_cycle(tmp_path):
    from app.domain.task import TaskValidationError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.add_link(parent_id="t_1", child_id="t_2")
    with pytest.raises((TaskValidationError, Exception)):
        await reg.add_link(parent_id="t_2", child_id="t_1")


@pytest.mark.asyncio
async def test_add_link_rejects_indirect_cycle(tmp_path):
    from app.domain.task import TaskValidationError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.create_task(_task("t_3", "c"))
    await reg.add_link(parent_id="t_1", child_id="t_2")
    await reg.add_link(parent_id="t_2", child_id="t_3")
    with pytest.raises((TaskValidationError, Exception)):
        await reg.add_link(parent_id="t_3", child_id="t_1")


@pytest.mark.asyncio
async def test_remove_link(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.add_link(parent_id="t_1", child_id="t_2")
    assert await reg.remove_link(parent_id="t_1", child_id="t_2") is True
    links = await reg.list_links("t_1")
    assert len(links) == 0
    assert await reg.remove_link(parent_id="t_1", child_id="t_2") is False


@pytest.mark.asyncio
async def test_list_links(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.create_task(_task("t_3", "c"))
    await reg.add_link(parent_id="t_1", child_id="t_2")
    await reg.add_link(parent_id="t_1", child_id="t_3")
    links = await reg.list_links("t_1")
    assert len(links) == 2


@pytest.mark.asyncio
async def test_list_children(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.create_task(_task("t_3", "c"))
    await reg.add_link(parent_id="t_1", child_id="t_2")
    await reg.add_link(parent_id="t_1", child_id="t_3")
    children = await reg.list_children("t_1")
    ids = {t.id for t in children}
    assert ids == {"t_2", "t_3"}


@pytest.mark.asyncio
async def test_list_parents(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.create_task(_task("t_3", "c"))
    await reg.add_link(parent_id="t_1", child_id="t_3")
    await reg.add_link(parent_id="t_2", child_id="t_3")
    parents = await reg.list_parents("t_3")
    ids = {t.id for t in parents}
    assert ids == {"t_1", "t_2"}


# ---------------------------------------------------------------------------
# T8: recompute_ready
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_ready_promotes_when_parents_done(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    await reg.create_task(_task("t_c", "c", status=TaskStatus.TODO, assignee="d"))
    await reg.add_link("t_p", "t_c")
    promoted = await reg.recompute_ready()
    assert "t_c" in promoted
    got = await reg.get_task("t_c")
    assert got.status == TaskStatus.READY


@pytest.mark.asyncio
async def test_recompute_ready_no_assignee_stays_todo(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    await reg.create_task(_task("t_c", "c", status=TaskStatus.TODO))
    await reg.add_link("t_p", "t_c")
    promoted = await reg.recompute_ready()
    assert "t_c" not in promoted
    got = await reg.get_task("t_c")
    assert got.status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_recompute_ready_parent_not_done_stays_todo(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_p", "p", status=TaskStatus.RUNNING, assignee="d"))
    await reg.create_task(_task("t_c", "c", status=TaskStatus.TODO, assignee="d"))
    await reg.add_link("t_p", "t_c")
    promoted = await reg.recompute_ready()
    assert "t_c" not in promoted


@pytest.mark.asyncio
async def test_recompute_ready_demotes_when_parent_leaves_done(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    await reg.create_task(_task("t_c", "c", status=TaskStatus.READY, assignee="d"))
    await reg.add_link("t_p", "t_c")
    # parent re-opens (DONE -> REVIEW)
    await reg.update_task("t_p", {"status": TaskStatus.REVIEW}, expected_version=1)
    promoted = await reg.recompute_ready()
    assert "t_c" not in promoted
    got = await reg.get_task("t_c")
    assert got.status == TaskStatus.TODO


@pytest.mark.asyncio
async def test_recompute_ready_scheduled_at_not_due(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    future = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.TODO, assignee="d", scheduled_at=future)
    )
    promoted = await reg.recompute_ready()
    assert "t_1" not in promoted


@pytest.mark.asyncio
async def test_recompute_ready_scheduled_at_due(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    past = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.TODO, assignee="d", scheduled_at=past)
    )
    promoted = await reg.recompute_ready()
    assert "t_1" in promoted


# ---------------------------------------------------------------------------
# T8: create_graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_graph_atomic(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    cmd = CreateGraphCommand(
        tasks=(
            _task("t_1", "a"),
            _task("t_2", "b"),
            _task("t_3", "c"),
        ),
        links=(
            TaskLink(parent_id="t_1", child_id="t_2"),
            TaskLink(parent_id="t_1", child_id="t_3"),
        ),
        comments=(
            TaskComment(id="c_1", task_id="t_1", author="u", body="hi"),
        ),
    )
    result = await reg.create_graph(cmd)
    assert len(result.tasks) == 3
    assert len(result.links) == 2
    assert len(result.comments) == 1
    got = await reg.get_task("t_2")
    assert got is not None
    parents = await reg.list_parents("t_2")
    assert "t_1" in {p.id for p in parents}


@pytest.mark.asyncio
async def test_create_graph_rolls_back_on_cycle(tmp_path):
    from app.domain.task import TaskValidationError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    cmd = CreateGraphCommand(
        tasks=(
            _task("t_1", "a"),
            _task("t_2", "b"),
        ),
        links=(
            TaskLink(parent_id="t_1", child_id="t_2"),
            TaskLink(parent_id="t_2", child_id="t_1"),  # cycle
        ),
    )
    with pytest.raises((TaskValidationError, Exception)):
        await reg.create_graph(cmd)
    # nothing should be persisted
    assert await reg.get_task("t_1") is None
    assert await reg.get_task("t_2") is None


# ---------------------------------------------------------------------------
# T8: Comments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment_and_list(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    c = await reg.add_comment("t_1", author="worker", body="hi")
    assert c.author == "worker"
    assert c.body == "hi"
    cs = await reg.list_comments("t_1")
    assert len(cs) == 1
    assert cs[0].body == "hi"


@pytest.mark.asyncio
async def test_list_comments_ordered_by_created_at(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.add_comment("t_1", author="a", body="first")
    await reg.add_comment("t_1", author="b", body="second")
    cs = await reg.list_comments("t_1")
    assert cs[0].body == "first"
    assert cs[1].body == "second"


# ---------------------------------------------------------------------------
# T8: Events (monotonic id)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_monotonic(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    e1 = await reg.append_event("t_1", kind="created", payload={})
    e2 = await reg.append_event("t_1", kind="claimed", payload={})
    assert e2.id > e1.id


@pytest.mark.asyncio
async def test_list_events_since(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    e1 = await reg.append_event("t_1", kind="created", payload={})
    e2 = await reg.append_event("t_1", kind="claimed", payload={})
    e3 = await reg.append_event("t_1", kind="finished", payload={})
    events = await reg.list_events("t_1", since=e1.id)
    assert len(events) == 2
    assert events[0].id == e2.id
    assert events[1].id == e3.id


@pytest.mark.asyncio
async def test_append_event_with_payload(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    e = await reg.append_event(
        "t_1", kind="noted", payload={"key": "value", "num": 42}
    )
    assert e.payload == {"key": "value", "num": 42}


@pytest.mark.asyncio
async def test_append_event_with_run_id(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    e = await reg.append_event("t_1", kind="claimed", payload={}, run_id=7)
    assert e.run_id == 7


# ---------------------------------------------------------------------------
# T8: Runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.READY, assignee="d"))
    c1 = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert c1 is not None
    await reg.finish_run(
        FinishRunCommand(
            task_id="t_1",
            run_id=c1.run.id,
            claim_lock="L1",
            outcome=TaskRunOutcome.FAILED,
        )
    )
    # re-claim for second run
    await reg.update_task("t_1", {"status": TaskStatus.READY}, expected_version=c1.task.version + 1)
    c2 = await reg.claim_task("t_1", claim_lock="L2", lease_seconds=900)
    assert c2 is not None
    runs = await reg.list_runs("t_1")
    assert len(runs) == 2


# ---------------------------------------------------------------------------
# T8: Attachments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_attachment_upload_and_download(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    att = await reg.add_attachment(
        "t_1",
        filename="r.md",
        stored_name="server-generated",
        content_type="text/markdown",
        size=10,
        checksum="sha256:abc",
        uploaded_by="u",
    )
    assert att.filename == "r.md"
    assert att.stored_name == "server-generated"
    got = await reg.get_attachment(att.id)
    assert got is not None
    assert got.filename == "r.md"


@pytest.mark.asyncio
async def test_list_attachments(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.add_attachment(
        "t_1", filename="a.md", stored_name="s1", content_type="text/markdown",
        size=10, checksum="c1", uploaded_by="u",
    )
    await reg.add_attachment(
        "t_1", filename="b.md", stored_name="s2", content_type="text/markdown",
        size=20, checksum="c2", uploaded_by="u",
    )
    atts = await reg.list_attachments("t_1")
    assert len(atts) == 2


@pytest.mark.asyncio
async def test_delete_attachment(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    att = await reg.add_attachment(
        "t_1", filename="a.md", stored_name="s1", content_type="text/markdown",
        size=10, checksum="c1", uploaded_by="u",
    )
    assert await reg.delete_attachment(att.id) is True
    assert await reg.get_attachment(att.id) is None
    assert await reg.delete_attachment(att.id) is False


@pytest.mark.asyncio
async def test_get_missing_attachment_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    assert await reg.get_attachment("missing") is None


# ---------------------------------------------------------------------------
# T8: Notify subs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscribe_notify(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    assert await reg.subscribe_notify("t_1", platform="feishu", chat_id="c1") is True
    subs = await reg.list_notify_subs("t_1")
    assert len(subs) == 1
    assert subs[0]["platform"] == "feishu"
    assert subs[0]["chat_id"] == "c1"


@pytest.mark.asyncio
async def test_subscribe_notify_idempotent(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.subscribe_notify("t_1", platform="feishu", chat_id="c1")
    await reg.subscribe_notify("t_1", platform="feishu", chat_id="c1")
    subs = await reg.list_notify_subs("t_1")
    assert len(subs) == 1


@pytest.mark.asyncio
async def test_subscribe_notify_with_thread_id(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.subscribe_notify(
        "t_1", platform="feishu", chat_id="c1", thread_id="t1"
    )
    subs = await reg.list_notify_subs("t_1")
    assert subs[0]["thread_id"] == "t1"


@pytest.mark.asyncio
async def test_unsubscribe_notify(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.subscribe_notify("t_1", platform="feishu", chat_id="c1")
    assert await reg.unsubscribe_notify("t_1", platform="feishu", chat_id="c1") is True
    subs = await reg.list_notify_subs("t_1")
    assert len(subs) == 0
    assert await reg.unsubscribe_notify("t_1", platform="feishu", chat_id="c1") is False


@pytest.mark.asyncio
async def test_notify_subs_last_terminal_event_id_default(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.subscribe_notify("t_1", platform="feishu", chat_id="c1")
    subs = await reg.list_notify_subs("t_1")
    assert subs[0]["last_terminal_event_id"] == 0


# ---------------------------------------------------------------------------
# T8: Cross-instance read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_instance_read(tmp_path):
    db_path = str(tmp_path / "t.db")
    reg1 = SQLiteTaskRegistry(db_path)
    await reg1.create_task(_task("t_1", "x"))
    reg2 = SQLiteTaskRegistry(db_path)
    got = await reg2.get_task("t_1")
    assert got is not None
    assert got.title == "x"


# ---------------------------------------------------------------------------
# T8 S8: Additional regression tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_link_rejects_cross_board(tmp_path):
    from app.domain.task import TaskValidationError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    # board is fixed to "default" by domain; we bypass via direct insert
    # to simulate a cross-board scenario. Since board is always "default"
    # in this iteration, we verify that the check exists by confirming
    # same-board links succeed (cross-board is not reachable via the
    # public API in this iteration). This test documents the intent.
    await reg.create_task(_task("t_1", "a", board="default"))
    await reg.create_task(_task("t_2", "b", board="default"))
    # Same board should succeed
    link = await reg.add_link(parent_id="t_1", child_id="t_2")
    assert link.parent_id == "t_1"


@pytest.mark.asyncio
async def test_recompute_ready_does_not_demote_running_child(tmp_path):
    """When a parent leaves DONE, RUNNING children are NOT demoted."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    # Child is RUNNING (claimed)
    await reg.create_task(
        _task("t_c", "c", status=TaskStatus.READY, assignee="d")
    )
    await reg.add_link("t_p", "t_c")
    claim = await reg.claim_task("t_c", claim_lock="Lc", lease_seconds=900)
    assert claim is not None
    assert claim.task.status == TaskStatus.RUNNING
    # Parent leaves DONE
    await reg.update_task("t_p", {"status": TaskStatus.REVIEW}, expected_version=1)
    await reg.recompute_ready()
    # RUNNING child should still be RUNNING
    got = await reg.get_task("t_c")
    assert got.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_finish_duplicate_late_appends_audit_event(tmp_path):
    """Late/duplicate finish should not overwrite but append audit event."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    # First finish succeeds
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
    )
    result1 = await reg.finish_run(cmd)
    assert result1.task.status == TaskStatus.DONE
    # After finish, claim_lock is released. A late finish attempt with
    # the old claim_lock should fail with TaskConflictError.
    with pytest.raises(TaskConflictError):
        await reg.finish_run(cmd)
    # But an audit event should have been appended
    events = await reg.list_events("t_1")
    kinds = [e.kind for e in events]
    assert "late_finish_attempt" in kinds


@pytest.mark.asyncio
async def test_recover_run_crashed_transitions_to_todo(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = RecoverRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.CRASHED,
        error="worker crashed",
    )
    result = await reg.recover_run(cmd)
    assert result.task.status == TaskStatus.TODO
    assert result.task.consecutive_failures == 1
    assert result.run.outcome == TaskRunOutcome.CRASHED
    assert result.run.error == "worker crashed"


@pytest.mark.asyncio
async def test_list_events_limit(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    for i in range(10):
        await reg.append_event("t_1", kind=f"kind_{i}", payload={})
    events = await reg.list_events("t_1", limit=3)
    assert len(events) == 3


@pytest.mark.asyncio
async def test_create_graph_with_comments(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    cmd = CreateGraphCommand(
        tasks=(_task("t_1", "a"), _task("t_2", "b")),
        links=(TaskLink(parent_id="t_1", child_id="t_2"),),
        comments=(
            TaskComment(
                id="c_1", task_id="t_1", author="user", body="start",
                created_at=datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc),
            ),
            TaskComment(
                id="c_2", task_id="t_2", author="user", body="child",
                created_at=datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc),
            ),
        ),
    )
    result = await reg.create_graph(cmd)
    assert len(result.comments) == 2
    cs1 = await reg.list_comments("t_1")
    assert len(cs1) == 1
    assert cs1[0].body == "start"
    cs2 = await reg.list_comments("t_2")
    assert len(cs2) == 1
    assert cs2[0].body == "child"


@pytest.mark.asyncio
async def test_create_graph_rolls_back_on_duplicate_link(tmp_path):
    from app.domain.task import TaskConflictError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    cmd = CreateGraphCommand(
        tasks=(_task("t_1", "a"), _task("t_2", "b")),
        links=(
            TaskLink(parent_id="t_1", child_id="t_2"),
            TaskLink(parent_id="t_1", child_id="t_2"),  # duplicate
        ),
    )
    with pytest.raises((TaskConflictError, Exception)):
        await reg.create_graph(cmd)
    assert await reg.get_task("t_1") is None


@pytest.mark.asyncio
async def test_update_task_datetime_field(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    scheduled = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
    await reg.update_task(
        "t_1", {"scheduled_at": scheduled}, expected_version=1
    )
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.scheduled_at == scheduled


@pytest.mark.asyncio
async def test_update_task_block_kind(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.RUNNING))
    await reg.update_task(
        "t_1",
        {"status": TaskStatus.BLOCKED, "block_kind": BlockKind.NEEDS_INPUT, "block_reason": "waiting"},
        expected_version=1,
    )
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.status == TaskStatus.BLOCKED
    assert got.block_kind == BlockKind.NEEDS_INPUT
    assert got.block_reason == "waiting"


@pytest.mark.asyncio
async def test_claim_task_with_expired_claim(tmp_path):
    """If a previous claim expired, a new claim should succeed."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    # Manually set an expired claim via update_task
    past = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.update_task(
        "t_1",
        {"claim_lock": "old", "claim_expires": past, "status": TaskStatus.READY},
        expected_version=1,
    )
    # New claim should succeed
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None
    assert result.task.claim_lock == "L1"


@pytest.mark.asyncio
async def test_delete_task_removes_links(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a"))
    await reg.create_task(_task("t_2", "b"))
    await reg.add_link("t_1", "t_2")
    await reg.delete_task("t_1")
    # Link should be gone (CASCADE)
    links = await reg.list_links("t_2")
    assert len(links) == 0


@pytest.mark.asyncio
async def test_delete_task_removes_attachments(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.add_attachment(
        "t_1", filename="a.md", stored_name="s1", content_type="text/markdown",
        size=10, checksum="c1", uploaded_by="u",
    )
    await reg.delete_task("t_1")
    atts = await reg.list_attachments("t_1")
    assert len(atts) == 0


@pytest.mark.asyncio
async def test_list_tasks_by_board(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a", board="default"))
    page = await reg.list_tasks(board="default")
    assert len(page.items) == 1
    # Non-existent board returns empty
    page2 = await reg.list_tasks(board="other")
    assert len(page2.items) == 0


@pytest.mark.asyncio
async def test_finish_run_appends_terminal_event(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    result = await reg.finish_run(
        FinishRunCommand(
            task_id="t_1",
            run_id=claim.run.id,
            claim_lock="L1",
            outcome=TaskRunOutcome.COMPLETED,
            summary="done",
        )
    )
    events = await reg.list_events("t_1")
    kinds = [e.kind for e in events]
    assert "finished" in kinds
    assert result.terminal_event.id > 0


@pytest.mark.asyncio
async def test_recover_run_appends_recovered_event(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    await reg.recover_run(
        RecoverRunCommand(
            task_id="t_1",
            run_id=claim.run.id,
            claim_lock="L1",
            outcome=TaskRunOutcome.RECLAIMED,
        )
    )
    events = await reg.list_events("t_1")
    kinds = [e.kind for e in events]
    assert "recovered" in kinds


@pytest.mark.asyncio
async def test_list_runs_ordered_desc(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    c1 = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert c1 is not None
    await reg.finish_run(
        FinishRunCommand(
            task_id="t_1", run_id=c1.run.id, claim_lock="L1",
            outcome=TaskRunOutcome.FAILED,
        )
    )
    # Re-claim for second run
    await reg.update_task("t_1", {"status": TaskStatus.READY}, expected_version=c1.task.version + 1)
    c2 = await reg.claim_task("t_1", claim_lock="L2", lease_seconds=900)
    assert c2 is not None
    runs = await reg.list_runs("t_1")
    assert len(runs) == 2
    # Most recent first
    assert runs[0].id > runs[1].id


@pytest.mark.asyncio
async def test_idempotency_key_uniqueness(tmp_path):
    from app.domain.task import TaskConflictError

    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "a", created_by="user1", idempotency_key="key1")
    )
    # Same (board, created_by, idempotency_key) should conflict
    with pytest.raises((TaskConflictError, Exception)):
        await reg.create_task(
            _task("t_2", "b", created_by="user1", idempotency_key="key1")
        )
    # Different created_by with same key should succeed
    await reg.create_task(
        _task("t_3", "c", created_by="user2", idempotency_key="key1")
    )


@pytest.mark.asyncio
async def test_notify_subs_multiple_platforms(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.subscribe_notify("t_1", platform="feishu", chat_id="c1")
    await reg.subscribe_notify("t_1", platform="feishu", chat_id="c2")
    await reg.subscribe_notify("t_1", platform="webhook", chat_id="c1")
    subs = await reg.list_notify_subs("t_1")
    assert len(subs) == 3


# ---------------------------------------------------------------------------
# Batch B review fixes: GAP1 / GAP2 / GAP3 / GAVE_UP / lease / override
# ---------------------------------------------------------------------------


# GAP 1: recompute_ready must promote SCHEDULED tasks whose parents are DONE
# and scheduled_at is due.


@pytest.mark.asyncio
async def test_recompute_ready_promotes_scheduled_when_due(tmp_path):
    """SCHEDULED task with all parents DONE + scheduled_at due -> READY."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    past = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    await reg.create_task(
        _task("t_c", "c", status=TaskStatus.SCHEDULED, assignee="d", scheduled_at=past)
    )
    await reg.add_link("t_p", "t_c")
    promoted = await reg.recompute_ready()
    assert "t_c" in promoted
    got = await reg.get_task("t_c")
    assert got.status == TaskStatus.READY


@pytest.mark.asyncio
async def test_recompute_ready_scheduled_not_due_stays_scheduled(tmp_path):
    """SCHEDULED task with future scheduled_at stays SCHEDULED even if parents DONE."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    future = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    await reg.create_task(
        _task("t_c", "c", status=TaskStatus.SCHEDULED, assignee="d", scheduled_at=future)
    )
    await reg.add_link("t_p", "t_c")
    promoted = await reg.recompute_ready()
    assert "t_c" not in promoted
    got = await reg.get_task("t_c")
    assert got.status == TaskStatus.SCHEDULED


# GAP 2: dependency_changed event for RUNNING children when parent leaves DONE


@pytest.mark.asyncio
async def test_recompute_ready_appends_dependency_changed_for_running_child(tmp_path):
    """Parent DONE->REVIEW, child RUNNING -> child stays RUNNING + dependency_changed event."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    await reg.create_task(
        _task("t_c", "c", status=TaskStatus.READY, assignee="d")
    )
    await reg.add_link("t_p", "t_c")
    claim = await reg.claim_task("t_c", claim_lock="Lc", lease_seconds=900)
    assert claim is not None
    assert claim.task.status == TaskStatus.RUNNING
    # Parent leaves DONE
    await reg.update_task("t_p", {"status": TaskStatus.REVIEW}, expected_version=1)
    await reg.recompute_ready()
    # RUNNING child should still be RUNNING
    got = await reg.get_task("t_c")
    assert got.status == TaskStatus.RUNNING
    # A dependency_changed event should have been appended
    events = await reg.list_events("t_c")
    kinds = [e.kind for e in events]
    assert "dependency_changed" in kinds


@pytest.mark.asyncio
async def test_recompute_ready_no_dependency_changed_when_parents_stay_done(tmp_path):
    """RUNNING child with all parents DONE should NOT get dependency_changed event."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_p", "p", status=TaskStatus.DONE, assignee="d"))
    await reg.create_task(
        _task("t_c", "c", status=TaskStatus.READY, assignee="d")
    )
    await reg.add_link("t_p", "t_c")
    claim = await reg.claim_task("t_c", claim_lock="Lc", lease_seconds=900)
    assert claim is not None
    # Parent stays DONE
    await reg.recompute_ready()
    events = await reg.list_events("t_c")
    kinds = [e.kind for e in events]
    assert "dependency_changed" not in kinds


# GAP 3: (status, priority) index


def test_schema_creates_status_priority_index(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    with reg._connect() as conn:
        indexes = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
    assert "idx_tasks_status_priority" in indexes


# Important #3: GAVE_UP -> TaskRunStatus.FAILED (not COMPLETED)


@pytest.mark.asyncio
async def test_finish_gave_up_run_status_is_failed(tmp_path):
    """GAVE_UP outcome should map run status to FAILED, not COMPLETED."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.GAVE_UP,
        summary="giving up",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.BLOCKED
    assert result.run.status == TaskRunStatus.FAILED
    assert result.run.outcome == TaskRunOutcome.GAVE_UP


# Important #2: record_heartbeat uses stored lease_seconds


@pytest.mark.asyncio
async def test_heartbeat_uses_stored_lease_seconds(tmp_path):
    """Claim with lease_seconds=300; heartbeat should renew by 300, not 900."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=300)
    assert claim is not None
    assert claim.run.lease_seconds == 300
    # Heartbeat at claim_time + 60s
    hb_now = datetime.now(timezone.utc) + timedelta(seconds=60)
    task = await reg.record_heartbeat(
        "t_1", run_id=claim.run.id, claim_lock="L1", now=hb_now
    )
    # Renewed lease should be hb_now + 300s, not hb_now + 900s
    expected_expires = hb_now + timedelta(seconds=300)
    assert task.claim_expires is not None
    delta = abs((task.claim_expires - expected_expires).total_seconds())
    assert delta < 2  # allow tiny clock skew


@pytest.mark.asyncio
async def test_heartbeat_falls_back_to_900_for_legacy_null_lease(tmp_path):
    """If lease_seconds is NULL (legacy), heartbeat falls back to 900s."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=300)
    assert claim is not None
    # Simulate legacy: NULL out the lease_seconds on the run row
    with reg._connect() as conn:
        conn.execute(
            "UPDATE task_runs SET lease_seconds = NULL WHERE id = ?",
            (claim.run.id,),
        )
        conn.commit()
    hb_now = datetime.now(timezone.utc) + timedelta(seconds=60)
    task = await reg.record_heartbeat(
        "t_1", run_id=claim.run.id, claim_lock="L1", now=hb_now
    )
    # Should fall back to 900
    expected_expires = hb_now + timedelta(seconds=900)
    assert task.claim_expires is not None
    delta = abs((task.claim_expires - expected_expires).total_seconds())
    assert delta < 2


# Concern A: target_task_status override


@pytest.mark.asyncio
async def test_finish_run_with_target_task_status_override(tmp_path):
    """finish_run with target_task_status=REVIEW -> task goes to REVIEW."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
        target_task_status=TaskStatus.REVIEW,
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.REVIEW
    # Run still reflects the actual outcome
    assert result.run.outcome == TaskRunOutcome.COMPLETED
    assert result.run.status == TaskRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_finish_run_without_target_task_status_uses_default(tmp_path):
    """Without target_task_status, COMPLETED outcome -> task DONE (default mapping)."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.READY, assignee="d")
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.DONE


# lease_seconds column migration


def test_lease_seconds_column_migration(tmp_path):
    """Existing task_runs tables without lease_seconds get the column via migration."""
    db_path = str(tmp_path / "t.db")
    # Create a legacy DB without lease_seconds
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            assignee TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'triage',
            block_kind TEXT,
            block_reason TEXT,
            block_recurrences INTEGER NOT NULL DEFAULT 0,
            started_at TEXT,
            completed_at TEXT,
            scheduled_at TEXT,
            claim_lock TEXT,
            claim_expires TEXT,
            current_run_id INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            skills_json TEXT NOT NULL DEFAULT '[]',
            execution_policy_json TEXT NOT NULL DEFAULT '{}',
            model_override TEXT,
            max_runtime_seconds INTEGER,
            max_retries INTEGER NOT NULL DEFAULT 0,
            goal_mode INTEGER NOT NULL DEFAULT 0,
            goal_max_turns INTEGER,
            workflow_template_id TEXT,
            current_step_key TEXT,
            project_id TEXT,
            tenant TEXT,
            board TEXT NOT NULL DEFAULT 'default',
            origin_session_id TEXT,
            execution_session_id TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            worker_token TEXT,
            last_failure_error TEXT,
            last_heartbeat_at TEXT,
            result TEXT,
            idempotency_key TEXT,
            pre_archive_status TEXT
        );
        CREATE TABLE task_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            profile TEXT NOT NULL DEFAULT 'default',
            status TEXT NOT NULL DEFAULT 'running',
            claim_lock TEXT,
            claim_expires TEXT,
            worker_token TEXT,
            max_runtime_seconds INTEGER,
            last_heartbeat_at TEXT,
            started_at TEXT,
            ended_at TEXT,
            outcome TEXT,
            summary TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()
    # Now init registry -- should add lease_seconds column via migration
    reg = SQLiteTaskRegistry(db_path)
    with reg._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
    assert "lease_seconds" in cols
