"""Tests for SQLiteTaskRegistry (T3).

Covers the Manus-aligned 7-state machine migration:
- Schema migration: idempotent, legacy status mapping, task_links drop,
  is_archived column addition, legacy column handling.
- CRUD with optimistic lock; list_tasks hides archived by default.
- Atomic claim (QUEUED + scheduled_at due + not archived).
- Heartbeat CAS.
- Finish run with new outcome->status mapping (COMPLETED->SUCCEEDED,
  WAITING_APPROVAL->WAITING_APPROVAL, CRASHED/TIMED_OUT->EXPIRED,
  TERMINATED->CANCELLED, retryable failure->QUEUED/FAILED).
- Recover run.
- list_queued_due dispatch helper.
- Comments, events, runs, attachments, notify subs.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.task import (
    BulkUpdateCommand,
    BulkUpdateItem,
    FinishRunCommand,
    ProposalResolutionCommand,
    ProposalResolutionResult,
    RecoverRunCommand,
    Task,
    TaskAttachment,
    TaskClaimError,
    TaskComment,
    TaskConflictError,
    TaskExecutionPolicy,
    TaskNotFoundError,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
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


_LEGACY_SCHEMA_SQL = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
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
    lease_seconds INTEGER,
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
CREATE TABLE task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(parent_id, child_id),
    FOREIGN KEY(parent_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(child_id) REFERENCES tasks(id) ON DELETE CASCADE
);
"""


def _seed_legacy(
    db_path,
    statuses=("triage", "todo", "scheduled", "ready", "running",
              "blocked", "review", "done", "archived"),
    assignee="alice",
    links=(("t_p", "t_c"),),
    extra_tasks=(),
):
    """Seed a legacy DB with the old 9-state schema + task_links + assignee."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_LEGACY_SCHEMA_SQL)
    now = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc).isoformat()
    for st in statuses:
        task_id = f"t_{st}"
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, created_at, updated_at, "
            "status, board) VALUES (?, ?, ?, ?, ?, ?, 'default')",
            (task_id, f"title-{st}", assignee, now, now, st),
        )
    for tid, title, st in extra_tasks:
        conn.execute(
            "INSERT INTO tasks (id, title, assignee, created_at, updated_at, "
            "status, board) VALUES (?, ?, ?, ?, ?, ?, 'default')",
            (tid, title, assignee, now, now, st),
        )
    for parent_id, child_id in links:
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id, created_at) "
            "VALUES (?, ?, ?)",
            (parent_id, child_id, now),
        )
    conn.commit()
    conn.close()


def _table_exists(db_path, table_name):
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    conn.close()
    return row is not None


def _has_column(db_path, table, column):
    conn = sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    conn.close()
    return column in cols


# ---------------------------------------------------------------------------
# T3: Schema migration
# ---------------------------------------------------------------------------


def test_schema_creates_tables_without_task_links(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    tables = reg._list_tables()
    for t in [
        "tasks",
        "task_runs",
        "task_comments",
        "task_events",
        "task_attachments",
        "task_notify_subs",
    ]:
        assert t in tables, f"missing table: {t}"
    # task_links must NOT exist in a freshly initialized DB
    assert "task_links" not in tables


def test_schema_idempotent_repeated_init(tmp_path):
    db_path = str(tmp_path / "t.db")
    reg1 = SQLiteTaskRegistry(db_path)
    reg1._ensure_schema()
    # second init should not raise
    reg2 = SQLiteTaskRegistry(db_path)
    reg2._ensure_schema()
    tables = reg2._list_tables()
    assert "tasks" in tables
    assert "task_links" not in tables


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
    assert "idx_tasks_session_id" in indexes
    assert "idx_tasks_idempotency" in indexes
    assert "idx_runs_task" in indexes
    assert "idx_events_task" in indexes
    assert "idx_events_run" in indexes
    assert "idx_attachments_task" in indexes
    assert "idx_notify_task" in indexes
    # legacy indexes must be gone
    assert "idx_links_parent" not in indexes
    assert "idx_links_child" not in indexes
    assert "idx_tasks_assignee_status" not in indexes


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


def test_tasks_table_has_is_archived_column(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    with reg._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "is_archived" in cols
    assert "origin_session_id" in cols
    assert "execution_session_id" in cols
    assert "worker_token" in cols
    assert "version" in cols
    assert "board" in cols
    assert "consecutive_failures" in cols


def test_idempotency_partial_unique_index(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    reg._ensure_schema()
    with reg._connect() as conn:
        idx_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_tasks_idempotency'"
        ).fetchone()
    assert idx_sql is not None
    assert "idempotency_key IS NOT NULL" in idx_sql[0]


# ---------------------------------------------------------------------------
# T3: Legacy status migration (idempotent)
# ---------------------------------------------------------------------------


def test_migration_maps_legacy_status_idempotent(tmp_path):
    db = tmp_path / "t.db"
    _seed_legacy(
        db,
        statuses=["triage", "todo", "scheduled", "ready", "running",
                   "blocked", "review", "done", "archived"],
        assignee="alice",
        links=[("t_p", "t_c")],
        extra_tasks=[("t_p", "parent", "done"), ("t_c", "child", "todo")],
    )
    reg = SQLiteTaskRegistry(str(db))
    reg._ensure_schema()  # first migration
    # second migration must be idempotent (no errors, no re-mapping)
    reg._ensure_schema()

    # Fetch all tasks via the registry (default hides archived)
    page = asyncio.run(reg.list_tasks(limit=100, include_archived=True))
    by_id = {t.id: t for t in page.items}

    # Legacy statuses mapped to new 7-state values
    assert by_id["t_triage"].status == TaskStatus.QUEUED
    assert by_id["t_todo"].status == TaskStatus.QUEUED
    assert by_id["t_scheduled"].status == TaskStatus.QUEUED
    assert by_id["t_ready"].status == TaskStatus.QUEUED
    assert by_id["t_running"].status == TaskStatus.EXPIRED
    assert by_id["t_review"].status == TaskStatus.WAITING_APPROVAL
    assert by_id["t_done"].status == TaskStatus.SUCCEEDED
    assert by_id["t_blocked"].status == TaskStatus.FAILED
    assert by_id["t_archived"].status == TaskStatus.CANCELLED
    assert by_id["t_archived"].is_archived is True

    # Non-archived tasks have is_archived = False
    assert by_id["t_triage"].is_archived is False
    assert by_id["t_running"].is_archived is False

    # task_links table must be dropped
    assert not _table_exists(db, "task_links")


def test_migration_idempotent_repeated_starts(tmp_path):
    """Running _ensure_schema 3 times must not error or re-map statuses."""
    db = tmp_path / "t.db"
    _seed_legacy(db, statuses=["running", "archived"])
    reg = SQLiteTaskRegistry(str(db))
    reg._ensure_schema()
    reg._ensure_schema()
    reg._ensure_schema()
    page = asyncio.run(reg.list_tasks(limit=100, include_archived=True))
    by_id = {t.id: t for t in page.items}
    # running -> expired (only mapped once, stays expired)
    assert by_id["t_running"].status == TaskStatus.EXPIRED
    # archived -> cancelled + is_archived=1
    assert by_id["t_archived"].status == TaskStatus.CANCELLED
    assert by_id["t_archived"].is_archived is True


def test_migration_does_not_touch_new_statuses(tmp_path):
    """A DB already on the new schema should not be re-mapped."""
    db = tmp_path / "t.db"
    reg = SQLiteTaskRegistry(str(db))
    asyncio.run(reg.create_task(_task("t_q", "x", status=TaskStatus.QUEUED)))
    asyncio.run(reg.create_task(_task("t_r", "x", status=TaskStatus.RUNNING,
                                       claim_lock="L")))
    # re-run migration
    reg._ensure_schema()
    got_q = asyncio.run(reg.get_task("t_q"))
    got_r = asyncio.run(reg.get_task("t_r"))
    assert got_q.status == TaskStatus.QUEUED
    assert got_r.status == TaskStatus.RUNNING  # not mapped to expired
    assert got_r.claim_lock == "L"


def test_migration_adds_is_archived_to_legacy_db(tmp_path):
    db = tmp_path / "t.db"
    _seed_legacy(db, statuses=["todo"])
    assert not _has_column(db, "tasks", "is_archived")
    reg = SQLiteTaskRegistry(str(db))
    reg._ensure_schema()
    assert _has_column(db, "tasks", "is_archived")


def test_migration_drops_task_links_table(tmp_path):
    db = tmp_path / "t.db"
    _seed_legacy(db, statuses=["done", "todo"], links=[("t_done", "t_todo")])
    assert _table_exists(db, "task_links")
    reg = SQLiteTaskRegistry(str(db))
    reg._ensure_schema()
    assert not _table_exists(db, "task_links")


def test_migration_handles_legacy_assignee_column(tmp_path):
    """Legacy DBs with assignee column must still work after migration.

    The registry ignores the column (doesn't read/write); if SQLite
    supports DROP COLUMN, the column is dropped; otherwise it remains
    as a legacy unused column.
    """
    db = tmp_path / "t.db"
    _seed_legacy(db, statuses=["todo"], assignee="alice")
    reg = SQLiteTaskRegistry(str(db))
    reg._ensure_schema()
    # Registry can still create/read tasks without touching assignee
    t = _task("t_new", "new")
    asyncio.run(reg.create_task(t))
    got = asyncio.run(reg.get_task("t_new"))
    assert got is not None
    assert got.title == "new"
    assert not hasattr(got, "assignee")  # domain removed the field


# ---------------------------------------------------------------------------
# T3: CRUD + optimistic lock
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
    assert got.is_archived is False


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
        priority=5,
        created_by="user-a",
        created_at=now,
        updated_at=now,
        version=1,
        status=TaskStatus.QUEUED,
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
        is_archived=False,
    )
    await reg.create_task(t)
    got = await reg.get_task("t_full")
    assert got is not None
    assert got.title == "full task"
    assert got.body == "do work"
    assert got.priority == 5
    assert got.created_by == "user-a"
    assert got.status == TaskStatus.QUEUED
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
    assert got.is_archived is False


@pytest.mark.asyncio
async def test_create_task_with_is_archived_true(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    t = _task("t_1", "x", status=TaskStatus.SUCCEEDED, is_archived=True)
    await reg.create_task(t)
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.is_archived is True
    assert got.status == TaskStatus.SUCCEEDED


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
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    await reg.update_task(
        "t_1", {"status": TaskStatus.RUNNING}, expected_version=1
    )
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_update_is_archived_field(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.update_task("t_1", {"is_archived": True}, expected_version=1)
    got = await reg.get_task("t_1")
    assert got is not None
    assert got.is_archived is True


@pytest.mark.asyncio
async def test_update_skills_and_execution_policy(tmp_path):
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
async def test_list_tasks_default_hides_archived(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "visible"))
    await reg.create_task(_task("t_2", "hidden", status=TaskStatus.SUCCEEDED, is_archived=True))
    page = await reg.list_tasks(limit=10)
    ids = {t.id for t in page.items}
    assert "t_1" in ids
    assert "t_2" not in ids


@pytest.mark.asyncio
async def test_list_tasks_include_archived(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "visible"))
    await reg.create_task(_task("t_2", "hidden", status=TaskStatus.SUCCEEDED, is_archived=True))
    page = await reg.list_tasks(limit=10, include_archived=True)
    ids = {t.id for t in page.items}
    assert "t_1" in ids
    assert "t_2" in ids


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
    cmd = BulkUpdateCommand(
        items=(
            BulkUpdateItem(task_id="t_1", fields={"title": "A"}, expected_version=1),
            BulkUpdateItem(task_id="t_2", fields={"title": "B"}, expected_version=99),
        )
    )
    with pytest.raises(TaskConflictError):
        await reg.bulk_update(cmd)
    got1 = await reg.get_task("t_1")
    assert got1.title == "a"
    assert got1.version == 1


@pytest.mark.asyncio
async def test_delete_task(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    assert await reg.delete_task("t_1") is True
    assert await reg.get_task("t_1") is None
    assert await reg.delete_task("t_1") is False


@pytest.mark.asyncio
async def test_delete_task_cascades_children(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    await reg.add_comment("t_1", "worker", "hi")
    await reg.append_event("t_1", "created", {})
    await reg.delete_task("t_1")
    assert await reg.list_comments("t_1") == ()
    assert await reg.list_events("t_1") == ()


@pytest.mark.asyncio
async def test_list_tasks_by_board(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "a", board="default"))
    page = await reg.list_tasks(board="default")
    assert len(page.items) == 1
    page2 = await reg.list_tasks(board="other")
    assert len(page2.items) == 0


# ---------------------------------------------------------------------------
# T3: Atomic claim (QUEUED + scheduled_at due + not archived)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_queued_task_succeeds(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None
    assert result.task.status == TaskStatus.RUNNING
    assert result.task.claim_lock == "L1"
    assert result.task.current_run_id == result.run.id
    assert result.run.status == TaskRunStatus.RUNNING
    assert result.run.claim_lock == "L1"
    assert result.run.task_id == "t_1"


@pytest.mark.asyncio
async def test_claim_non_queued_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    # RUNNING, SUCCEEDED, FAILED, CANCELLED, EXPIRED, WAITING_APPROVAL
    for st in (
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.EXPIRED,
        TaskStatus.WAITING_APPROVAL,
    ):
        await reg.create_task(_task(f"t_{st.value}", "x", status=st))
        result = await reg.claim_task(f"t_{st.value}", claim_lock="L1", lease_seconds=900)
        assert result is None, f"claim should fail for {st.value}"


@pytest.mark.asyncio
async def test_claim_missing_task_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    result = await reg.claim_task("missing", claim_lock="L1", lease_seconds=900)
    assert result is None


@pytest.mark.asyncio
async def test_claim_atomic_single_winner(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None
    events = await reg.list_events("t_1")
    kinds = [e.kind for e in events]
    assert "claimed" in kinds


@pytest.mark.asyncio
async def test_claim_with_scheduled_at_not_due_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    future = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.QUEUED, scheduled_at=future)
    )
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is None


@pytest.mark.asyncio
async def test_claim_with_scheduled_at_due_succeeds(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    past = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.QUEUED, scheduled_at=past)
    )
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None
    assert result.task.status == TaskStatus.RUNNING


@pytest.mark.asyncio
async def test_claim_with_no_scheduled_at_succeeds(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None


@pytest.mark.asyncio
async def test_claim_archived_queued_returns_none(tmp_path):
    """Archived QUEUED tasks must not be claimed."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.QUEUED, is_archived=True)
    )
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is None


@pytest.mark.asyncio
async def test_claim_task_with_expired_previous_claim(tmp_path):
    """If a previous claim expired, a new claim should succeed."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    past = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.update_task(
        "t_1",
        {"claim_lock": "old", "claim_expires": past},
        expected_version=1,
    )
    result = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert result is not None
    assert result.task.claim_lock == "L1"


# ---------------------------------------------------------------------------
# T3: Heartbeat CAS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_heartbeat_renews_lease(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    now = datetime.now(timezone.utc) + timedelta(seconds=60)
    task = await reg.record_heartbeat(
        "t_1", run_id=claim.run.id, claim_lock="L1", now=now
    )
    assert task.last_heartbeat_at == now
    assert task.claim_expires is not None
    assert task.claim_expires > claim.task.claim_expires


@pytest.mark.asyncio
async def test_heartbeat_wrong_token_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    now = datetime(2026, 7, 18, 12, 5, tzinfo=timezone.utc)
    with pytest.raises((TaskClaimError, Exception)):
        await reg.record_heartbeat(
            "t_1", run_id=claim.run.id, claim_lock="WRONG", now=now
        )


@pytest.mark.asyncio
async def test_heartbeat_wrong_run_id_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    now = datetime(2026, 7, 18, 12, 5, tzinfo=timezone.utc)
    with pytest.raises((TaskClaimError, Exception)):
        await reg.record_heartbeat(
            "t_1", run_id=99999, claim_lock="L1", now=now
        )


@pytest.mark.asyncio
async def test_heartbeat_uses_stored_lease_seconds(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=300)
    assert claim is not None
    assert claim.run.lease_seconds == 300
    hb_now = datetime.now(timezone.utc) + timedelta(seconds=60)
    task = await reg.record_heartbeat(
        "t_1", run_id=claim.run.id, claim_lock="L1", now=hb_now
    )
    expected_expires = hb_now + timedelta(seconds=300)
    assert task.claim_expires is not None
    delta = abs((task.claim_expires - expected_expires).total_seconds())
    assert delta < 2


@pytest.mark.asyncio
async def test_heartbeat_falls_back_to_900_for_legacy_null_lease(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=300)
    assert claim is not None
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
    expected_expires = hb_now + timedelta(seconds=900)
    assert task.claim_expires is not None
    delta = abs((task.claim_expires - expected_expires).total_seconds())
    assert delta < 2


# ---------------------------------------------------------------------------
# T3: Finish run outcome mapping
# ---------------------------------------------------------------------------


async def _claim(tmp_path, task_id="t_1", status=TaskStatus.QUEUED, **kw):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task(task_id, "x", status=status, **kw))
    claim = await reg.claim_task(task_id, claim_lock="L1", lease_seconds=900)
    assert claim is not None
    return reg, claim


@pytest.mark.asyncio
async def test_finish_run_completed(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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
    assert result.task.status == TaskStatus.SUCCEEDED
    assert result.task.claim_lock is None
    assert result.task.current_run_id is None
    assert result.task.completed_at is not None
    assert result.task.result == "done"
    assert result.task.consecutive_failures == 0
    assert result.run.outcome == TaskRunOutcome.COMPLETED
    assert result.run.status == TaskRunStatus.COMPLETED
    assert result.run.ended_at is not None
    assert result.terminal_event.id > 0


@pytest.mark.asyncio
async def test_finish_wrong_claim_token_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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
async def test_finish_failed_outcome_under_max_retries_transitions_to_queued(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.QUEUED, max_retries=2,
              consecutive_failures=0)
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
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.claim_lock is None
    assert result.task.consecutive_failures == 1
    assert result.run.outcome == TaskRunOutcome.FAILED


@pytest.mark.asyncio
async def test_finish_failed_outcome_over_max_retries_transitions_to_failed(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.QUEUED, max_retries=1,
              consecutive_failures=1)
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.FAILED,
        summary="error",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.FAILED
    assert result.task.consecutive_failures == 2


@pytest.mark.asyncio
async def test_finish_spawn_failed_under_max_retries_transitions_to_queued(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.QUEUED, max_retries=1)
    )
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.SPAWN_FAILED,
        error="could not spawn",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.consecutive_failures == 1


@pytest.mark.asyncio
async def test_finish_waiting_approval_transitions_to_waiting_approval(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.WAITING_APPROVAL,
        summary="need user confirm",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.WAITING_APPROVAL
    assert result.task.claim_lock is None
    assert result.task.current_run_id is None
    # WAITING_APPROVAL is not a failure; counter unchanged
    assert result.task.consecutive_failures == 0
    assert result.run.outcome == TaskRunOutcome.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_finish_terminated_transitions_to_cancelled(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.TERMINATED,
        summary="user cancelled",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.CANCELLED
    assert result.task.claim_lock is None
    assert result.task.consecutive_failures == 0


@pytest.mark.asyncio
async def test_finish_expired_transitions_to_expired(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.EXPIRED,
        error="lease expired",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.EXPIRED
    assert result.task.claim_lock is None


@pytest.mark.asyncio
async def test_finish_crashed_transitions_to_expired(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.CRASHED,
        error="worker crashed",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.EXPIRED
    assert result.run.outcome == TaskRunOutcome.CRASHED


@pytest.mark.asyncio
async def test_finish_timed_out_transitions_to_expired(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.TIMED_OUT,
        error="run exceeded max_runtime",
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.EXPIRED


@pytest.mark.asyncio
async def test_finish_releases_lease(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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


@pytest.mark.asyncio
async def test_finish_run_appends_terminal_event(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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
async def test_finish_run_with_target_task_status_override(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
        target_task_status=TaskStatus.WAITING_APPROVAL,
    )
    result = await reg.finish_run(cmd)
    assert result.task.status == TaskStatus.WAITING_APPROVAL
    assert result.run.outcome == TaskRunOutcome.COMPLETED
    assert result.run.status == TaskRunStatus.COMPLETED


@pytest.mark.asyncio
async def test_finish_duplicate_late_appends_audit_event(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = FinishRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.COMPLETED,
        summary="done",
    )
    result1 = await reg.finish_run(cmd)
    assert result1.task.status == TaskStatus.SUCCEEDED
    with pytest.raises(TaskConflictError):
        await reg.finish_run(cmd)
    events = await reg.list_events("t_1")
    kinds = [e.kind for e in events]
    assert "late_finish_attempt" in kinds


# ---------------------------------------------------------------------------
# T3: Recover run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_run_with_expired_outcome(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    cmd = RecoverRunCommand(
        task_id="t_1",
        run_id=claim.run.id,
        claim_lock="L1",
        outcome=TaskRunOutcome.EXPIRED,
        error="lease expired",
    )
    result = await reg.recover_run(cmd)
    assert result.run.outcome == TaskRunOutcome.EXPIRED
    assert result.run.error == "lease expired"
    assert result.task.status == TaskStatus.EXPIRED
    assert result.task.claim_lock is None


@pytest.mark.asyncio
async def test_recover_run_with_crashed_outcome(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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
    assert result.task.status == TaskStatus.EXPIRED
    assert result.task.consecutive_failures == 0  # CRASHED doesn't increment
    assert result.run.outcome == TaskRunOutcome.CRASHED


@pytest.mark.asyncio
async def test_recover_run_wrong_token_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
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


@pytest.mark.asyncio
async def test_recover_run_appends_recovered_event(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    claim = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert claim is not None
    await reg.recover_run(
        RecoverRunCommand(
            task_id="t_1",
            run_id=claim.run.id,
            claim_lock="L1",
            outcome=TaskRunOutcome.EXPIRED,
        )
    )
    events = await reg.list_events("t_1")
    kinds = [e.kind for e in events]
    assert "recovered" in kinds


# ---------------------------------------------------------------------------
# T3: list_queued_due dispatch helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_queued_due_returns_due_tasks(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    past = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_1", "a", status=TaskStatus.QUEUED))
    await reg.create_task(_task("t_2", "b", status=TaskStatus.QUEUED, scheduled_at=past))
    result = await reg.list_queued_due(now=now, limit=100)
    ids = {t.id for t in result}
    assert "t_1" in ids
    assert "t_2" in ids


@pytest.mark.asyncio
async def test_list_queued_due_excludes_future_scheduled(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    future = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_1", "a", status=TaskStatus.QUEUED))
    await reg.create_task(_task("t_2", "b", status=TaskStatus.QUEUED, scheduled_at=future))
    result = await reg.list_queued_due(now=now, limit=100)
    ids = {t.id for t in result}
    assert "t_1" in ids
    assert "t_2" not in ids


@pytest.mark.asyncio
async def test_list_queued_due_excludes_archived(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_1", "a", status=TaskStatus.QUEUED))
    await reg.create_task(
        _task("t_2", "b", status=TaskStatus.QUEUED, is_archived=True)
    )
    result = await reg.list_queued_due(now=now, limit=100)
    ids = {t.id for t in result}
    assert "t_1" in ids
    assert "t_2" not in ids


@pytest.mark.asyncio
async def test_list_queued_due_excludes_non_queued(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_1", "a", status=TaskStatus.QUEUED))
    await reg.create_task(_task("t_2", "b", status=TaskStatus.RUNNING))
    await reg.create_task(_task("t_3", "c", status=TaskStatus.FAILED))
    result = await reg.list_queued_due(now=now, limit=100)
    ids = {t.id for t in result}
    assert ids == {"t_1"}


@pytest.mark.asyncio
async def test_list_queued_due_priority_order(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    base = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_low", "low", status=TaskStatus.QUEUED, priority=1, created_at=base))
    await reg.create_task(_task("t_high", "high", status=TaskStatus.QUEUED, priority=10, created_at=base))
    result = await reg.list_queued_due(now=base, limit=100)
    assert result[0].id == "t_high"
    assert result[1].id == "t_low"


@pytest.mark.asyncio
async def test_list_queued_due_by_board(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    await reg.create_task(_task("t_1", "a", status=TaskStatus.QUEUED, board="default"))
    await reg.create_task(_task("t_2", "b", status=TaskStatus.QUEUED, board="other"))
    result = await reg.list_queued_due(now=now, limit=100, board="default")
    ids = {t.id for t in result}
    assert ids == {"t_1"}


@pytest.mark.asyncio
async def test_list_queued_due_respects_limit(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    base = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        await reg.create_task(_task(f"t_{i}", f"task-{i}", status=TaskStatus.QUEUED, created_at=base))
    result = await reg.list_queued_due(now=now, limit=3)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# T3: list_running
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_running_returns_running_tasks(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    running = await reg.list_running()
    assert len(running) == 1
    assert running[0].id == "t_1"


# ---------------------------------------------------------------------------
# T3: Comments
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
# T3: Events (monotonic id)
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


@pytest.mark.asyncio
async def test_list_events_limit(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x"))
    for i in range(10):
        await reg.append_event("t_1", kind=f"kind_{i}", payload={})
    events = await reg.list_events("t_1", limit=3)
    assert len(events) == 3


# ---------------------------------------------------------------------------
# T3: Runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_runs_ordered_desc(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED, max_retries=5))
    c1 = await reg.claim_task("t_1", claim_lock="L1", lease_seconds=900)
    assert c1 is not None
    await reg.finish_run(
        FinishRunCommand(
            task_id="t_1", run_id=c1.run.id, claim_lock="L1",
            outcome=TaskRunOutcome.FAILED,
        )
    )
    # re-claim for second run (task auto-retry -> QUEUED)
    c2 = await reg.claim_task("t_1", claim_lock="L2", lease_seconds=900)
    assert c2 is not None
    runs = await reg.list_runs("t_1")
    assert len(runs) == 2
    assert runs[0].id > runs[1].id


# ---------------------------------------------------------------------------
# T3: Attachments
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


# ---------------------------------------------------------------------------
# T3: Notify subs
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
# T3: Idempotency / cross-instance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotency_key_uniqueness(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "a", created_by="user1", idempotency_key="key1")
    )
    with pytest.raises((TaskConflictError, Exception)):
        await reg.create_task(
            _task("t_2", "b", created_by="user1", idempotency_key="key1")
        )
    await reg.create_task(
        _task("t_3", "c", created_by="user2", idempotency_key="key1")
    )


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
# T3: lease_seconds column migration (legacy DBs)
# ---------------------------------------------------------------------------


def test_lease_seconds_column_migration(tmp_path):
    """Existing task_runs tables without lease_seconds get the column via migration."""
    db_path = str(tmp_path / "t.db")
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            body TEXT NOT NULL DEFAULT '',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'queued',
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
            is_archived INTEGER NOT NULL DEFAULT 0
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
    reg = SQLiteTaskRegistry(db_path)
    with reg._connect() as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(task_runs)")}
    assert "lease_seconds" in cols


# ---------------------------------------------------------------------------
# T2: Proposal resolution (resolve_proposal)
# ---------------------------------------------------------------------------


_PROPOSAL_KIND = "change_proposed"
_APPROVED_KIND = "change_approved"
_REJECTED_KIND = "change_rejected"
_REVISED_KIND = "change_revised"
_RESOLUTION_KINDS = (_APPROVED_KIND, _REJECTED_KIND, _REVISED_KIND)


async def _seed_pending_proposal(
    reg: SQLiteTaskRegistry,
    task_id: str = "t_1",
    session_id: str | None = None,
    proposal_text: str = "do X",
    **kw,
):
    """Create a WAITING_APPROVAL task with a pending change_proposed event.

    Returns the change_proposed TaskEvent.
    """
    kwargs: dict = {"status": TaskStatus.WAITING_APPROVAL}
    if session_id is not None:
        kwargs["origin_session_id"] = session_id
    kwargs.update(kw)
    await reg.create_task(_task(task_id, "x", **kwargs))
    proposal = await reg.append_event(
        task_id,
        kind=_PROPOSAL_KIND,
        payload={"proposal": proposal_text},
    )
    return proposal


class _CasFaultConnection:
    """Connection wrapper that forces the resolve_proposal CAS UPDATE to rowcount=0.

    Intercepts the CAS UPDATE (identified by the unique ``is_archived = 0``
    in the WHERE clause combined with ``version = version + 1`` in SET) and
    replaces the task_id parameter with a non-existent value, so the UPDATE
    matches zero rows. All other SQL passes through unchanged.

    Used to test the defensive rollback path when CAS rowcount != 1.
    """

    def __init__(self, real_conn: sqlite3.Connection, task_id_to_fail: str):
        self._real = real_conn
        self._task_id = task_id_to_fail

    def execute(self, sql, params=()):
        if (
            "UPDATE tasks SET" in sql
            and "is_archived = 0" in sql
            and "version = version + 1" in sql
        ):
            new_params = tuple(
                "__cas_fault_nonexistent__" if p == self._task_id else p
                for p in params
            )
            return self._real.execute(sql, new_params)
        return self._real.execute(sql, params)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()

    def __enter__(self):
        self._real.__enter__()
        return self

    def __exit__(self, *args):
        return self._real.__exit__(*args)

    @property
    def row_factory(self):
        return self._real.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._real.row_factory = value


# --- Point 1: Three legal decisions write marker + transition to QUEUED ---


@pytest.mark.asyncio
async def test_proposal_resolution_approved_writes_marker_and_transitions(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    proposal = await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    result = await reg.resolve_proposal(cmd)

    assert isinstance(result, ProposalResolutionResult)
    assert result.proposal_event_id == proposal.id
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.version == 2  # exactly +1
    assert result.decision_event.kind == _APPROVED_KIND
    assert result.decision_event.payload["proposal_event_id"] == proposal.id
    assert result.decision_event.payload["decision"] == "approved"
    assert result.decision_event.payload["note"] is None
    # Exactly one resolution marker in the event history
    events = await reg.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 1
    assert markers[0].id == result.decision_event.id


@pytest.mark.asyncio
async def test_proposal_resolution_rejected_writes_marker_and_transitions(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    proposal = await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="rejected",
        event_kind=_REJECTED_KIND,
        note="not needed",
    )
    result = await reg.resolve_proposal(cmd)

    assert result.proposal_event_id == proposal.id
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.version == 2
    assert result.decision_event.kind == _REJECTED_KIND
    assert result.decision_event.payload["proposal_event_id"] == proposal.id
    assert result.decision_event.payload["decision"] == "rejected"
    assert result.decision_event.payload["note"] == "not needed"


@pytest.mark.asyncio
async def test_proposal_resolution_revised_writes_marker_with_note_and_transitions(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    proposal = await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="revised",
        event_kind=_REVISED_KIND,
        note="please redo with option B",
    )
    result = await reg.resolve_proposal(cmd)

    assert result.proposal_event_id == proposal.id
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.version == 2
    assert result.decision_event.kind == _REVISED_KIND
    assert result.decision_event.payload["proposal_event_id"] == proposal.id
    assert result.decision_event.payload["decision"] == "revised"
    assert result.decision_event.payload["note"] == "please redo with option B"


# --- Point 2: Defensive rejection (no events written) ---


@pytest.mark.asyncio
async def test_proposal_resolution_rejects_mismatched_decision_event_kind(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="approved",
        event_kind=_REJECTED_KIND,  # mismatched
    )
    with pytest.raises(TaskValidationError):
        await reg.resolve_proposal(cmd)
    # No resolution marker written
    events = await reg.list_events("t_1")
    assert all(e.kind not in _RESOLUTION_KINDS for e in events)


@pytest.mark.asyncio
async def test_proposal_resolution_rejects_empty_task_id(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="",
        expected_version=1,
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    with pytest.raises(TaskValidationError):
        await reg.resolve_proposal(cmd)
    # No event written for the empty task_id (and no marker on t_1)
    events = await reg.list_events("t_1")
    assert all(e.kind not in _RESOLUTION_KINDS for e in events)


@pytest.mark.asyncio
async def test_proposal_resolution_rejects_invalid_decision(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="maybe",
        event_kind="change_maybe",
    )
    with pytest.raises(TaskValidationError):
        await reg.resolve_proposal(cmd)
    events = await reg.list_events("t_1")
    assert all(e.kind not in _RESOLUTION_KINDS for e in events)


@pytest.mark.asyncio
async def test_proposal_resolution_rejects_revised_with_empty_note(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="revised",
        event_kind=_REVISED_KIND,
        note="",
    )
    with pytest.raises(TaskValidationError):
        await reg.resolve_proposal(cmd)
    events = await reg.list_events("t_1")
    assert all(e.kind not in _RESOLUTION_KINDS for e in events)


@pytest.mark.asyncio
async def test_proposal_resolution_rejects_revised_with_whitespace_note(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="revised",
        event_kind=_REVISED_KIND,
        note="   ",
    )
    with pytest.raises(TaskValidationError):
        await reg.resolve_proposal(cmd)
    events = await reg.list_events("t_1")
    assert all(e.kind not in _RESOLUTION_KINDS for e in events)


@pytest.mark.asyncio
async def test_proposal_resolution_rejects_revised_with_none_note(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="revised",
        event_kind=_REVISED_KIND,
        note=None,
    )
    with pytest.raises(TaskValidationError):
        await reg.resolve_proposal(cmd)
    events = await reg.list_events("t_1")
    assert all(e.kind not in _RESOLUTION_KINDS for e in events)


# --- Point 3: Task state guards ---


@pytest.mark.asyncio
async def test_proposal_resolution_task_not_found_raises_not_found(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))

    cmd = ProposalResolutionCommand(
        task_id="t_missing",
        expected_version=1,
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    with pytest.raises(TaskNotFoundError):
        await reg.resolve_proposal(cmd)


@pytest.mark.asyncio
async def test_proposal_resolution_archived_task_raises_state_error(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    proposal = await _seed_pending_proposal(reg, "t_1")
    # Archive the task (keep status WAITING_APPROVAL, set is_archived)
    await reg.update_task("t_1", {"is_archived": True}, expected_version=1)

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=2,  # version incremented by update_task
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    with pytest.raises(TaskStateError):
        await reg.resolve_proposal(cmd)
    # No marker written
    events = await reg.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 0


@pytest.mark.asyncio
async def test_proposal_resolution_non_waiting_approval_raises_state_error(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.QUEUED))
    await reg.append_event("t_1", kind=_PROPOSAL_KIND, payload={"proposal": "X"})

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    with pytest.raises(TaskStateError):
        await reg.resolve_proposal(cmd)
    events = await reg.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 0


@pytest.mark.asyncio
async def test_proposal_resolution_no_pending_proposal_raises_state_error(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.WAITING_APPROVAL))
    # No change_proposed event

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    with pytest.raises(TaskStateError):
        await reg.resolve_proposal(cmd)
    events = await reg.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 0


@pytest.mark.asyncio
async def test_proposal_resolution_version_conflict_raises_conflict(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=99,  # stale
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    with pytest.raises(TaskConflictError):
        await reg.resolve_proposal(cmd)
    events = await reg.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 0


# --- Point 4: Concurrent resolution ---


@pytest.mark.asyncio
async def test_proposal_resolution_concurrent_different_decisions_only_one_wins(tmp_path):
    db_path = str(tmp_path / "t.db")
    reg1 = SQLiteTaskRegistry(db_path)
    reg2 = SQLiteTaskRegistry(db_path)
    await _seed_pending_proposal(reg1, "t_1")

    cmd1 = ProposalResolutionCommand(
        task_id="t_1", expected_version=1,
        decision="approved", event_kind=_APPROVED_KIND,
    )
    cmd2 = ProposalResolutionCommand(
        task_id="t_1", expected_version=1,
        decision="rejected", event_kind=_REJECTED_KIND,
    )
    results = await asyncio.gather(
        reg1.resolve_proposal(cmd1),
        reg2.resolve_proposal(cmd2),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], TaskConflictError)
    # Only one resolution marker
    events = await reg1.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 1
    # Task is QUEUED with version 2
    task = await reg1.get_task("t_1")
    assert task.status == TaskStatus.QUEUED
    assert task.version == 2


@pytest.mark.asyncio
async def test_proposal_resolution_concurrent_same_decision_only_one_wins(tmp_path):
    db_path = str(tmp_path / "t.db")
    reg1 = SQLiteTaskRegistry(db_path)
    reg2 = SQLiteTaskRegistry(db_path)
    await _seed_pending_proposal(reg1, "t_1")

    cmd1 = ProposalResolutionCommand(
        task_id="t_1", expected_version=1,
        decision="approved", event_kind=_APPROVED_KIND,
    )
    cmd2 = ProposalResolutionCommand(
        task_id="t_1", expected_version=1,
        decision="approved", event_kind=_APPROVED_KIND,
    )
    results = await asyncio.gather(
        reg1.resolve_proposal(cmd1),
        reg2.resolve_proposal(cmd2),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], TaskConflictError)
    events = await reg1.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 1


# --- Point 5: High event count (proposal outside display window) ---


@pytest.mark.asyncio
async def test_proposal_resolution_finds_proposal_outside_display_window(tmp_path):
    """51+ events so the latest pending proposal falls outside a 50-event
    display window. The atomic query must still find it, proving the
    Registry does NOT reuse _MAX_EVENTS_IN_DETAIL (=50).
    """
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.WAITING_APPROVAL))

    # Append 50 filler events first
    for i in range(50):
        await reg.append_event("t_1", kind="noted", payload={"seq": i})
    # The change_proposed is event #51 (outside a 50-event window)
    proposal = await reg.append_event(
        "t_1", kind=_PROPOSAL_KIND, payload={"proposal": "do X"}
    )
    assert proposal.id > 50

    # Verify the proposal is NOT in the first 50 events (display window)
    display = await reg.list_events("t_1", limit=50)
    assert all(e.kind != _PROPOSAL_KIND for e in display)

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    result = await reg.resolve_proposal(cmd)
    assert result.proposal_event_id == proposal.id
    assert result.task.status == TaskStatus.QUEUED


# --- Point 6: Precise proposal matching by payload ---


@pytest.mark.asyncio
async def test_proposal_resolution_matches_by_payload_not_marker_time(tmp_path):
    """Multiple proposals and resolution markers interleaved: only the
    proposal whose id matches the marker's proposal_event_id payload is
    considered resolved. Marker time alone must not determine closure.
    """
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(_task("t_1", "x", status=TaskStatus.WAITING_APPROVAL))

    # Proposal A (id=1)
    proposal_a = await reg.append_event(
        "t_1", kind=_PROPOSAL_KIND, payload={"proposal": "A"}
    )
    # Proposal B (id=2) -- later than A
    proposal_b = await reg.append_event(
        "t_1", kind=_PROPOSAL_KIND, payload={"proposal": "B"}
    )
    # Marker resolving A (id=3) -- manually appended, NOT via resolve_proposal.
    # Its proposal_event_id points to A, NOT B.
    await reg.append_event(
        "t_1",
        kind=_APPROVED_KIND,
        payload={"decision": "approved", "proposal_event_id": proposal_a.id},
    )
    # Task is still WAITING_APPROVAL (manual append, no state transition)

    # The latest UNRESOLVED proposal is B (id=2), not A.
    # A marker-time-based algorithm would wrongly think both A and B are
    # resolved (marker id=3 is after both), raising "no pending proposal".
    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="rejected",
        event_kind=_REJECTED_KIND,
    )
    result = await reg.resolve_proposal(cmd)

    # Must resolve B, not A
    assert result.proposal_event_id == proposal_b.id
    assert result.decision_event.payload["proposal_event_id"] == proposal_b.id
    assert result.decision_event.kind == _REJECTED_KIND
    assert result.task.status == TaskStatus.QUEUED
    assert result.task.version == 2


# --- Point 7: CAS rowcount=0 rollback ---


@pytest.mark.asyncio
async def test_proposal_resolution_cas_rowcount_zero_rolls_back_no_orphan(tmp_path):
    """Force the CAS UPDATE to affect 0 rows; verify the transaction rolls
    back and no orphan decision event is left behind.
    """
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await _seed_pending_proposal(reg, "t_1")

    original_connect = reg._connect

    def faulty_connect():
        return _CasFaultConnection(original_connect(), "t_1")

    reg._connect = faulty_connect  # type: ignore[assignment]

    cmd = ProposalResolutionCommand(
        task_id="t_1",
        expected_version=1,
        decision="approved",
        event_kind=_APPROVED_KIND,
    )
    with pytest.raises(TaskConflictError):
        await reg.resolve_proposal(cmd)

    # Restore original connect for verification queries
    reg._connect = original_connect  # type: ignore[assignment]

    # No orphan decision event
    events = await reg.list_events("t_1")
    markers = [e for e in events if e.kind in _RESOLUTION_KINDS]
    assert len(markers) == 0
    # Task unchanged
    task = await reg.get_task("t_1")
    assert task is not None
    assert task.status == TaskStatus.WAITING_APPROVAL
    assert task.version == 1


# ---------------------------------------------------------------------------
# T2: latest_waiting_approval_in_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_waiting_approval_returns_matching_task(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.WAITING_APPROVAL, origin_session_id="sess-A")
    )
    result = await reg.latest_waiting_approval_in_session("sess-A")
    assert result is not None
    assert result.id == "t_1"
    assert result.status == TaskStatus.WAITING_APPROVAL
    assert result.origin_session_id == "sess-A"


@pytest.mark.asyncio
async def test_latest_waiting_approval_excludes_other_sessions(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "a", status=TaskStatus.WAITING_APPROVAL, origin_session_id="sess-A")
    )
    await reg.create_task(
        _task("t_2", "b", status=TaskStatus.WAITING_APPROVAL, origin_session_id="sess-B")
    )
    result = await reg.latest_waiting_approval_in_session("sess-A")
    assert result is not None
    assert result.id == "t_1"


@pytest.mark.asyncio
async def test_latest_waiting_approval_excludes_archived(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "visible", status=TaskStatus.WAITING_APPROVAL,
              origin_session_id="sess-A")
    )
    await reg.create_task(
        _task("t_2", "archived", status=TaskStatus.WAITING_APPROVAL,
              origin_session_id="sess-A", is_archived=True)
    )
    result = await reg.latest_waiting_approval_in_session("sess-A")
    assert result is not None
    assert result.id == "t_1"


@pytest.mark.asyncio
async def test_latest_waiting_approval_excludes_non_waiting_approval(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "queued", status=TaskStatus.QUEUED, origin_session_id="sess-A")
    )
    await reg.create_task(
        _task("t_2", "waiting", status=TaskStatus.WAITING_APPROVAL,
              origin_session_id="sess-A")
    )
    result = await reg.latest_waiting_approval_in_session("sess-A")
    assert result is not None
    assert result.id == "t_2"


@pytest.mark.asyncio
async def test_latest_waiting_approval_sorts_created_desc_id_desc(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    base = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    # t_1 created earlier, t_2 created later
    await reg.create_task(
        _task("t_1", "old", status=TaskStatus.WAITING_APPROVAL,
              origin_session_id="sess-A", created_at=base)
    )
    await reg.create_task(
        _task("t_2", "new", status=TaskStatus.WAITING_APPROVAL,
              origin_session_id="sess-A", created_at=base + timedelta(seconds=10))
    )
    result = await reg.latest_waiting_approval_in_session("sess-A")
    assert result is not None
    assert result.id == "t_2"  # latest created_at wins


@pytest.mark.asyncio
async def test_latest_waiting_approval_same_created_at_higher_id_wins(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    base = datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc)
    # Same created_at; id DESC means "t_z" > "t_a"
    await reg.create_task(
        _task("t_a", "a", status=TaskStatus.WAITING_APPROVAL,
              origin_session_id="sess-A", created_at=base)
    )
    await reg.create_task(
        _task("t_z", "z", status=TaskStatus.WAITING_APPROVAL,
              origin_session_id="sess-A", created_at=base)
    )
    result = await reg.latest_waiting_approval_in_session("sess-A")
    assert result is not None
    assert result.id == "t_z"  # higher id wins on tie


@pytest.mark.asyncio
async def test_latest_waiting_approval_empty_session_raises_validation_error(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    with pytest.raises(TaskValidationError):
        await reg.latest_waiting_approval_in_session("")
    with pytest.raises(TaskValidationError):
        await reg.latest_waiting_approval_in_session(None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_latest_waiting_approval_no_candidates_returns_none(tmp_path):
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    await reg.create_task(
        _task("t_1", "x", status=TaskStatus.QUEUED, origin_session_id="sess-A")
    )
    result = await reg.latest_waiting_approval_in_session("sess-A")
    assert result is None


@pytest.mark.asyncio
async def test_latest_waiting_approval_query_plan_uses_index(tmp_path):
    """EXPLAIN QUERY PLAN for the session query must not show a full table scan."""
    reg = SQLiteTaskRegistry(str(tmp_path / "t.db"))
    with reg._connect() as conn:
        plan_rows = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM tasks "
            "WHERE origin_session_id = ? AND status = ? AND is_archived = 0 "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            ("sess-A", TaskStatus.WAITING_APPROVAL.value),
        ).fetchall()
    plan_text = " ".join(row[3] for row in plan_rows)
    # Must use an index (SEARCH/USING INDEX), not a full SCAN
    assert "SCAN" not in plan_text.upper() or "USING INDEX" in plan_text.upper(), (
        f"expected index usage, got: {plan_text}"
    )
