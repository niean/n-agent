"""SQLite persistence for the Task subdomain (Infrastructure Layer).

Implements ``TaskRegistry`` async Protocol from ``app/domain/task.py``.
Shares the sessions.db path but opens independent connections. Enables WAL,
foreign_keys, and busy_timeout per connection. Async methods wrap sync
sqlite3 via ``asyncio.to_thread`` (tech debt D018: to_thread wrapping).

Atomicity guarantees:
  - claim_task: BEGIN IMMEDIATE, only QUEUED + no valid claim + version match
    + scheduled_at due + not archived -> insert run + update task to RUNNING
    in one transaction.
  - finish_run / recover_run: CAS on (run_id, claim_lock) in one transaction;
    writes run terminal, transitions task, appends terminal event, releases
    lease. Late/duplicate results raise TaskConflictError.
  - record_heartbeat: CAS on (run_id, claim_lock), renews claim_expires.
  - bulk_update: single transaction, all-or-nothing.

State machine (Manus-aligned 7 states):
  queued / running / waiting_approval / succeeded / failed / cancelled / expired

Legacy migration (idempotent):
  - ADD COLUMN is_archived (PRAGMA-protected)
  - status mapping (WHERE-guarded): triage/todo/scheduled/ready -> queued,
    running -> expired (avoid ghost RUNNING on restart), review ->
    waiting_approval, done -> succeeded, blocked -> failed,
    archived -> cancelled + is_archived=1
  - DROP TABLE IF EXISTS task_links
  - DROP COLUMN assignee/block_kind/block_reason/block_recurrences/
    pre_archive_status when supported (SQLite 3.35+); otherwise the columns
    are left as legacy unused and the registry read/write model ignores them.

datetime<->storage: all datetimes stored as UTC ISO-8601 strings; conversion
happens at the registry boundary (``_dt_to_str`` / ``_str_to_dt``).
JSON fields use ``ensure_ascii=False`` (project convention for user-facing
JSON).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Mapping
from dataclasses import replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.domain.task import (
    BulkUpdateCommand,
    BulkUpdateItem,
    BulkUpdateResult,
    ClaimResult,
    DeliveryResult,
    FinishRunCommand,
    FinishRunResult,
    ProposalResolutionCommand,
    ProposalResolutionResult,
    RecoverRunCommand,
    Task,
    TaskAttachment,
    TaskClaimError,
    TaskComment,
    TaskConflictError,
    TaskEvent,
    TaskExecutionPolicy,
    TaskListCursor,
    TaskListPage,
    TaskNotFoundError,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
    TaskStateError,
    TaskStatus,
    TaskValidationError,
    TaskWorkspaceKind,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS task_runs (
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

CREATE TABLE IF NOT EXISTS task_comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    author TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_attachments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    uploaded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_notify_subs (
    task_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    last_terminal_event_id INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id, platform, chat_id, thread_id),
    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_status_priority ON tasks(status, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(origin_session_id, execution_session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
    ON tasks(board, created_by, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON task_events(run_id);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON task_attachments(task_id);
CREATE INDEX IF NOT EXISTS idx_notify_task ON task_notify_subs(task_id);
"""

# Indexes that reference columns added by migration are created separately
# after the migration completes (avoids "no such column" on legacy DBs where
# the tasks table pre-exists without is_archived).
_POST_MIGRATION_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_tasks_is_archived ON tasks(is_archived);
"""

# ---------------------------------------------------------------------------
# Proposal resolution constants
# ---------------------------------------------------------------------------

# Decision string -> resolution marker event kind.
_DECISION_TO_EVENT_KIND: dict[str, str] = {
    "approved": "change_approved",
    "rejected": "change_rejected",
    "revised": "change_revised",
}

# All resolution marker kinds (used to detect already-resolved proposals).
_RESOLUTION_MARKER_KINDS: frozenset[str] = frozenset(
    _DECISION_TO_EVENT_KIND.values()
)

# The proposal event kind written by TaskService when a worker requests
# user approval (pending until a resolution marker closes it).
_PROPOSAL_KIND = "change_proposed"

# ---------------------------------------------------------------------------
# Field <-> column mapping for update_task / bulk_update
# ---------------------------------------------------------------------------

# Task fields that are datetime (need ISO conversion)
_DATETIME_FIELDS: frozenset[str] = frozenset({
    "created_at", "updated_at", "started_at", "completed_at",
    "scheduled_at", "claim_expires", "last_heartbeat_at",
})

# Task fields that are enums (store .value)
_ENUM_FIELDS: frozenset[str] = frozenset({
    "status", "workspace_kind",
})

# Mapping from Task field name to column name (when they differ)
_FIELD_TO_COLUMN: dict[str, str] = {
    "skills": "skills_json",
    "execution_policy": "execution_policy_json",
}

# Fields that cannot be updated via update_task
_IMMUTABLE_FIELDS: frozenset[str] = frozenset({"id", "created_at"})

# Boolean fields stored as 0/1
_BOOLEAN_FIELDS: frozenset[str] = frozenset({
    "goal_mode", "is_archived",
})

# Legacy columns from the prior 9-state schema. If present on an existing
# DB, the migration tries to DROP them (SQLite 3.35+); otherwise they remain
# as unused legacy columns that the registry never reads or writes.
_LEGACY_COLUMNS: tuple[str, ...] = (
    "assignee",
    "block_kind",
    "block_reason",
    "block_recurrences",
    "pre_archive_status",
)

# Legacy status -> (new_status, is_archived) mapping. Applied with WHERE
# guards so re-running the migration never re-maps new values.
_LEGACY_STATUS_MAP: tuple[tuple[str, str, int], ...] = (
    # legacy_status, new_status, is_archived
    ("triage", "queued", 0),
    ("todo", "queued", 0),
    ("scheduled", "queued", 0),
    ("ready", "queued", 0),
    ("running", "expired", 0),
    ("review", "waiting_approval", 0),
    ("done", "succeeded", 0),
    ("blocked", "failed", 0),
    ("archived", "cancelled", 1),
)


# ---------------------------------------------------------------------------
# Helpers (datetime / json / enum conversion at boundary)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _dt_to_str(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_utc(value).isoformat()


def _str_to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _enum_to_str(value: Enum | None) -> str | None:
    if value is None:
        return None
    return value.value


def _str_to_enum(enum_cls: type[Enum], value: str | None) -> Enum | None:
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default if default is not None else {}


def _field_value_to_storage(field_name: str, value: Any) -> Any:
    """Convert a Task field value to its SQLite storage representation."""
    if value is None:
        return None
    if field_name in _DATETIME_FIELDS:
        return _dt_to_str(value)
    if field_name in _ENUM_FIELDS:
        if isinstance(value, Enum):
            return value.value
        return value
    if field_name in _BOOLEAN_FIELDS:
        return 1 if value else 0
    if field_name == "skills":
        return _json_dumps(list(value))
    if field_name == "execution_policy":
        if isinstance(value, TaskExecutionPolicy):
            return _json_dumps({"allowed_tools": list(value.allowed_tools)})
        return _json_dumps(value)
    return value


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class SQLiteTaskRegistry:
    """SQLite implementation of the TaskRegistry async Protocol.

    Shares the sessions.db path but opens independent connections. Each
    connection enables WAL, foreign_keys, and busy_timeout (5000ms).
    """

    BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Connection / schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.BUSY_TIMEOUT_MS / 1000.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
        return conn

    def _ensure_schema(self) -> None:
        """Idempotent schema creation + legacy migration.

        Safe for empty DBs, repeated startup, and half-migrated DBs.
        Legacy migration is guarded so re-running never re-maps new
        statuses or duplicates columns.

        The legacy 9-state -> 7-state status mapping (including
        ``running -> expired`` to avoid ghost RUNNING on restart) runs
        only once, guarded by ``PRAGMA user_version``. On subsequent
        startups the migration is skipped so newly-created RUNNING
        tasks are not re-mapped; stale RUNNING detection is handled at
        runtime by TaskRunService.
        """
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # --- task_runs lease_seconds migration (legacy) ---
            run_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(task_runs)")
            }
            if "lease_seconds" not in run_cols:
                conn.execute(
                    "ALTER TABLE task_runs ADD COLUMN lease_seconds INTEGER"
                )

            # --- tasks.is_archived migration ---
            task_cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(tasks)")
            }
            if "is_archived" not in task_cols:
                conn.execute(
                    "ALTER TABLE tasks ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0"
                )

            # Now that is_archived is guaranteed to exist, create indexes
            # that reference it. Safe on both new and legacy DBs.
            conn.executescript(_POST_MIGRATION_INDEX_SQL)

            # --- one-time legacy migration (guarded by user_version) ---
            # user_version 0 = uninitialized or pre-migration; 1 = migrated.
            user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            if user_version < 1:
                # Legacy status mapping (WHERE-guarded for idempotency).
                for legacy_st, new_st, is_archived in _LEGACY_STATUS_MAP:
                    conn.execute(
                        "UPDATE tasks SET status = ?, is_archived = ? "
                        "WHERE status = ?",
                        (new_st, is_archived, legacy_st),
                    )

                # Drop task_links table if it exists (dependency graph removed).
                conn.execute("DROP TABLE IF EXISTS task_links")

                # Drop legacy indexes that reference dropped columns/tables.
                conn.execute("DROP INDEX IF EXISTS idx_links_parent")
                conn.execute("DROP INDEX IF EXISTS idx_links_child")
                conn.execute("DROP INDEX IF EXISTS idx_tasks_assignee_status")

                # Attempt to DROP legacy columns (SQLite 3.35+).
                # If not supported, columns remain as unused legacy.
                self._try_drop_legacy_columns(conn)

                # Mark migration as complete so subsequent startups skip
                # the status mapping (avoids re-mapping new RUNNING).
                conn.execute("PRAGMA user_version = 1")

            conn.commit()

    @staticmethod
    def _try_drop_legacy_columns(conn: sqlite3.Connection) -> None:
        """Best-effort DROP COLUMN for legacy 9-state columns.

        Silently skips columns that don't exist or when the SQLite version
        does not support ALTER TABLE DROP COLUMN (pre-3.35). The registry
        read/write model never references these columns, so leaving them
        as legacy is safe.
        """
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(tasks)")
        }
        for col in _LEGACY_COLUMNS:
            if col not in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE tasks DROP COLUMN {col}")
            except sqlite3.OperationalError:
                # Unsupported or blocked (e.g. indexed); leave as legacy.
                pass

    def _list_tables(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        return {row["name"] for row in rows}

    # ------------------------------------------------------------------
    # Row -> domain object conversion
    # ------------------------------------------------------------------

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        skills_raw = _json_loads(row["skills_json"], [])
        policy_raw = _json_loads(row["execution_policy_json"], {})
        return Task(
            id=row["id"],
            title=row["title"],
            body=row["body"],
            priority=row["priority"],
            created_by=row["created_by"],
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
            version=row["version"],
            status=TaskStatus(row["status"]),
            started_at=_str_to_dt(row["started_at"]),
            completed_at=_str_to_dt(row["completed_at"]),
            scheduled_at=_str_to_dt(row["scheduled_at"]),
            claim_lock=row["claim_lock"],
            claim_expires=_str_to_dt(row["claim_expires"]),
            current_run_id=row["current_run_id"],
            workspace_kind=TaskWorkspaceKind(row["workspace_kind"]),
            workspace_path=row["workspace_path"],
            skills=tuple(str(s) for s in skills_raw),
            execution_policy=TaskExecutionPolicy(
                allowed_tools=tuple(
                    str(t) for t in (policy_raw.get("allowed_tools") or [])
                )
            ),
            model_override=row["model_override"],
            max_runtime_seconds=row["max_runtime_seconds"],
            max_retries=row["max_retries"],
            goal_mode=bool(row["goal_mode"]),
            goal_max_turns=row["goal_max_turns"],
            workflow_template_id=row["workflow_template_id"],
            current_step_key=row["current_step_key"],
            project_id=row["project_id"],
            tenant=row["tenant"],
            board=row["board"],
            origin_session_id=row["origin_session_id"],
            execution_session_id=row["execution_session_id"],
            consecutive_failures=row["consecutive_failures"],
            worker_token=row["worker_token"],
            last_failure_error=row["last_failure_error"],
            last_heartbeat_at=_str_to_dt(row["last_heartbeat_at"]),
            result=row["result"],
            idempotency_key=row["idempotency_key"],
            is_archived=bool(row["is_archived"]),
        )

    def _row_to_run(self, row: sqlite3.Row) -> TaskRun:
        return TaskRun(
            id=row["id"],
            task_id=row["task_id"],
            profile=row["profile"],
            status=TaskRunStatus(row["status"]),
            claim_lock=row["claim_lock"],
            claim_expires=_str_to_dt(row["claim_expires"]),
            worker_token=row["worker_token"],
            max_runtime_seconds=row["max_runtime_seconds"],
            lease_seconds=row["lease_seconds"],
            last_heartbeat_at=_str_to_dt(row["last_heartbeat_at"]),
            started_at=_str_to_dt(row["started_at"]),
            ended_at=_str_to_dt(row["ended_at"]),
            outcome=_str_to_enum(TaskRunOutcome, row["outcome"]),
            summary=row["summary"],
            metadata=_json_loads(row["metadata_json"], {}),
            error=row["error"],
        )

    def _row_to_event(self, row: sqlite3.Row) -> TaskEvent:
        return TaskEvent(
            id=row["id"],
            task_id=row["task_id"],
            kind=row["kind"],
            payload=_json_loads(row["payload_json"], {}),
            run_id=row["run_id"],
            created_at=_str_to_dt(row["created_at"]),
        )

    def _row_to_comment(self, row: sqlite3.Row) -> TaskComment:
        return TaskComment(
            id=row["id"],
            task_id=row["task_id"],
            author=row["author"],
            body=row["body"],
            created_at=_str_to_dt(row["created_at"]),
        )

    def _row_to_attachment(self, row: sqlite3.Row) -> TaskAttachment:
        return TaskAttachment(
            id=row["id"],
            task_id=row["task_id"],
            filename=row["filename"],
            stored_name=row["stored_name"],
            content_type=row["content_type"],
            size=row["size"],
            checksum=row["checksum"],
            uploaded_by=row["uploaded_by"],
            created_at=_str_to_dt(row["created_at"]),
        )

    # ------------------------------------------------------------------
    # Task params for INSERT
    # ------------------------------------------------------------------

    def _task_params(self, task: Task) -> tuple:
        now = _now()
        created_at = task.created_at or now
        updated_at = task.updated_at or now
        return (
            task.id,
            task.title,
            task.body,
            task.priority,
            task.created_by,
            _dt_to_str(created_at),
            _dt_to_str(updated_at),
            task.version,
            _enum_to_str(task.status),
            _dt_to_str(task.started_at),
            _dt_to_str(task.completed_at),
            _dt_to_str(task.scheduled_at),
            task.claim_lock,
            _dt_to_str(task.claim_expires),
            task.current_run_id,
            _enum_to_str(task.workspace_kind),
            task.workspace_path,
            _json_dumps(list(task.skills)),
            _json_dumps({"allowed_tools": list(task.execution_policy.allowed_tools)}),
            task.model_override,
            task.max_runtime_seconds,
            task.max_retries,
            1 if task.goal_mode else 0,
            task.goal_max_turns,
            task.workflow_template_id,
            task.current_step_key,
            task.project_id,
            task.tenant,
            task.board,
            task.origin_session_id,
            task.execution_session_id,
            task.consecutive_failures,
            task.worker_token,
            task.last_failure_error,
            _dt_to_str(task.last_heartbeat_at),
            task.result,
            task.idempotency_key,
            1 if task.is_archived else 0,
        )

    _INSERT_TASK_SQL = """
        INSERT INTO tasks (
            id, title, body, priority, created_by, created_at, updated_at,
            version, status, started_at, completed_at, scheduled_at,
            claim_lock, claim_expires, current_run_id, workspace_kind,
            workspace_path, skills_json, execution_policy_json,
            model_override, max_runtime_seconds, max_retries, goal_mode,
            goal_max_turns, workflow_template_id, current_step_key,
            project_id, tenant, board, origin_session_id,
            execution_session_id, consecutive_failures, worker_token,
            last_failure_error, last_heartbeat_at, result, idempotency_key,
            is_archived
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
    """

    # ------------------------------------------------------------------
    # CRUD (sync implementations)
    # ------------------------------------------------------------------

    def _create_task_sync(self, task: Task) -> Task:
        now = _now()
        created_at = task.created_at or now
        updated_at = task.updated_at or now
        # Return a task with the resolved timestamps
        resolved = dataclass_replace(
            task,
            created_at=created_at,
            updated_at=updated_at,
        )
        with self._connect() as conn:
            try:
                conn.execute(self._INSERT_TASK_SQL, self._task_params(resolved))
            except sqlite3.IntegrityError as e:
                conn.rollback()
                raise TaskConflictError(f"create_task integrity error: {e}") from e
        return resolved

    def _get_task_sync(self, task_id: str) -> Task | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def _list_tasks_sync(
        self,
        board: str = "default",
        cursor: TaskListCursor | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> TaskListPage:
        # Clamp limit to a safe range
        limit = max(1, min(limit, 500))
        archived_clause = "" if include_archived else " AND is_archived = 0"
        with self._connect() as conn:
            if cursor is not None:
                rows = conn.execute(
                    f"""
                    SELECT * FROM tasks
                    WHERE board = ?{archived_clause}
                      AND (created_at > ? OR (created_at = ? AND id > ?))
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (
                        board,
                        _dt_to_str(cursor.created_at),
                        _dt_to_str(cursor.created_at),
                        cursor.task_id,
                        limit + 1,
                    ),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"""
                    SELECT * FROM tasks
                    WHERE board = ?{archived_clause}
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """,
                    (board, limit + 1),
                ).fetchall()
        items = [self._row_to_task(r) for r in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = items[-1]
            next_cursor = TaskListCursor(
                created_at=last.created_at, task_id=last.id
            )
        return TaskListPage(items=tuple(items), next_cursor=next_cursor)

    def _build_update_clause(
        self, fields: Mapping[str, Any]
    ) -> tuple[str, list[Any]]:
        """Build SET clause and params from field mapping."""
        set_parts: list[str] = []
        params: list[Any] = []
        for field_name, value in fields.items():
            if field_name in _IMMUTABLE_FIELDS:
                continue
            column = _FIELD_TO_COLUMN.get(field_name, field_name)
            storage_value = _field_value_to_storage(field_name, value)
            set_parts.append(f"{column} = ?")
            params.append(storage_value)
        # Always increment version and update updated_at
        set_parts.append("version = version + 1")
        set_parts.append("updated_at = ?")
        params.append(_dt_to_str(_now()))
        clause = ", ".join(set_parts)
        return clause, params

    def _update_task_sync(
        self,
        task_id: str,
        fields: Mapping[str, Any],
        expected_version: int,
    ) -> Task:
        clause, params = self._build_update_clause(fields)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise TaskNotFoundError(f"task not found: {task_id}")
            if row["version"] != expected_version:
                conn.rollback()
                raise TaskConflictError(
                    f"version conflict: expected {expected_version}, got {row['version']}"
                )
            conn.execute(
                f"UPDATE tasks SET {clause} WHERE id = ? AND version = ?",
                (*params, task_id, expected_version),
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            conn.commit()
        return self._row_to_task(row)

    def _bulk_update_sync(self, command: BulkUpdateCommand) -> BulkUpdateResult:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated: list[Task] = []
            for item in command.items:
                row = conn.execute(
                    "SELECT version FROM tasks WHERE id = ?", (item.task_id,)
                ).fetchone()
                if row is None:
                    conn.rollback()
                    raise TaskNotFoundError(f"task not found: {item.task_id}")
                if row["version"] != item.expected_version:
                    conn.rollback()
                    raise TaskConflictError(
                        f"version conflict for {item.task_id}: "
                        f"expected {item.expected_version}, got {row['version']}"
                    )
                clause, params = self._build_update_clause(item.fields)
                conn.execute(
                    f"UPDATE tasks SET {clause} WHERE id = ? AND version = ?",
                    (*params, item.task_id, item.expected_version),
                )
            for item in command.items:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (item.task_id,)
                ).fetchone()
                updated.append(self._row_to_task(row))
            conn.commit()
        return BulkUpdateResult(updated=tuple(updated))

    def _delete_task_sync(self, task_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM tasks WHERE id = ?", (task_id,)
            )
            deleted = cursor.rowcount > 0
            conn.commit()
        return deleted

    # ------------------------------------------------------------------
    # Atomic claim / heartbeat / finish / recover
    # ------------------------------------------------------------------

    def _claim_task_sync(
        self,
        task_id: str,
        claim_lock: str,
        lease_seconds: int,
    ) -> ClaimResult | None:
        now = _now()
        claim_expires = now + timedelta(seconds=lease_seconds)
        worker_token = uuid4().hex
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # CAS: status=QUEUED + not archived + scheduled_at due
            # + no valid current claim (claim_expires NULL or in the past)
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE id = ? AND status = ? AND is_archived = 0
                  AND (scheduled_at IS NULL OR scheduled_at <= ?)
                  AND (claim_expires IS NULL OR claim_expires < ?)
                """,
                (
                    task_id,
                    TaskStatus.QUEUED.value,
                    _dt_to_str(now),
                    _dt_to_str(now),
                ),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task = self._row_to_task(row)
            # Insert new run
            run_cursor = conn.execute(
                """
                INSERT INTO task_runs (
                    task_id, profile, status, claim_lock, claim_expires,
                    worker_token, max_runtime_seconds, lease_seconds,
                    started_at, created_at
                ) VALUES (?, 'default', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    TaskRunStatus.RUNNING.value,
                    claim_lock,
                    _dt_to_str(claim_expires),
                    worker_token,
                    task.max_runtime_seconds,
                    lease_seconds,
                    _dt_to_str(now),
                    _dt_to_str(now),
                ),
            )
            run_id = run_cursor.lastrowid
            # Update task to RUNNING
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?, claim_lock = ?, claim_expires = ?,
                    current_run_id = ?, worker_token = ?,
                    started_at = COALESCE(started_at, ?),
                    last_heartbeat_at = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                """,
                (
                    TaskStatus.RUNNING.value,
                    claim_lock,
                    _dt_to_str(claim_expires),
                    run_id,
                    worker_token,
                    _dt_to_str(now),
                    _dt_to_str(now),
                    _dt_to_str(now),
                    task_id,
                    task.version,
                ),
            )
            # Append claimed event
            conn.execute(
                """
                INSERT INTO task_events (task_id, run_id, kind, payload_json, created_at)
                VALUES (?, ?, 'claimed', ?, ?)
                """,
                (
                    task_id,
                    run_id,
                    _json_dumps({"claim_lock": claim_lock}),
                    _dt_to_str(now),
                ),
            )
            # Fetch updated task and run
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            run_row = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            conn.commit()
        return ClaimResult(
            task=self._row_to_task(task_row),
            run=self._row_to_run(run_row),
        )

    def _record_heartbeat_sync(
        self,
        task_id: str,
        run_id: int,
        claim_lock: str,
        now: datetime,
    ) -> Task:
        now = _aware_utc(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # CAS: verify claim_lock + run_id match
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE id = ? AND claim_lock = ? AND current_run_id = ?
                """,
                (task_id, claim_lock, run_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise TaskClaimError(
                    f"heartbeat CAS failed: task={task_id} run={run_id} "
                    f"claim_lock mismatch or task not RUNNING this run"
                )
            # Read stored lease_seconds from the run; fall back to 900 for
            # legacy rows (NULL).
            run_row = conn.execute(
                "SELECT lease_seconds FROM task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            stored_lease = (
                run_row["lease_seconds"] if run_row is not None else None
            )
            lease = stored_lease if stored_lease is not None else 900
            new_expires = now + timedelta(seconds=lease)
            # Update task heartbeat + renew lease
            conn.execute(
                """
                UPDATE tasks SET last_heartbeat_at = ?, claim_expires = ?,
                    version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (_dt_to_str(now), _dt_to_str(new_expires), _dt_to_str(now), task_id),
            )
            # Update run heartbeat + renew lease
            conn.execute(
                """
                UPDATE task_runs SET last_heartbeat_at = ?, claim_expires = ?
                WHERE id = ? AND task_id = ?
                """,
                (_dt_to_str(now), _dt_to_str(new_expires), run_id, task_id),
            )
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            conn.commit()
        return self._row_to_task(task_row)

    def _finish_run_sync(self, command: FinishRunCommand) -> FinishRunResult:
        return self._finalize_run(
            task_id=command.task_id,
            run_id=command.run_id,
            claim_lock=command.claim_lock,
            outcome=command.outcome,
            summary=command.summary,
            metadata=command.metadata,
            error=command.error,
            is_recover=False,
            target_task_status=command.target_task_status,
        )

    def _recover_run_sync(self, command: RecoverRunCommand) -> FinishRunResult:
        return self._finalize_run(
            task_id=command.task_id,
            run_id=command.run_id,
            claim_lock=command.claim_lock,
            outcome=command.outcome,
            summary=None,
            metadata={},
            error=command.error,
            is_recover=True,
        )

    def _finalize_run(
        self,
        task_id: str,
        run_id: int,
        claim_lock: str,
        outcome: TaskRunOutcome,
        summary: str | None,
        metadata: Mapping[str, Any],
        error: str | None,
        is_recover: bool,
        target_task_status: TaskStatus | None = None,
    ) -> FinishRunResult:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 1. Look up the run first (to detect late/duplicate finishes)
            run_row = conn.execute(
                "SELECT * FROM task_runs WHERE id = ? AND task_id = ?",
                (run_id, task_id),
            ).fetchone()
            if run_row is None:
                conn.rollback()
                raise TaskNotFoundError(f"run not found: {run_id}")
            existing_run = self._row_to_run(run_row)

            # 2. Late/duplicate finish: run already terminal.
            # Verify the caller's claim_lock matches the run's stored
            # claim_lock (the token used during the run). If it matches,
            # this is a legitimate late worker -- append audit event but
            # don't overwrite. If it doesn't match, reject without audit.
            if existing_run.status != TaskRunStatus.RUNNING:
                if existing_run.claim_lock == claim_lock:
                    conn.execute(
                        """
                        INSERT INTO task_events (task_id, run_id, kind, payload_json, created_at)
                        VALUES (?, ?, 'late_finish_attempt', ?, ?)
                        """,
                        (
                            task_id,
                            run_id,
                            _json_dumps(
                                {
                                    "outcome": outcome.value,
                                    "summary": summary,
                                    "is_recover": is_recover,
                                }
                            ),
                            _dt_to_str(now),
                        ),
                    )
                    conn.commit()
                raise TaskConflictError(
                    f"run {run_id} already terminal: {existing_run.status.value}"
                )

            # 3. CAS: verify task claim_lock + current_run_id match
            task_row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE id = ? AND claim_lock = ? AND current_run_id = ?
                """,
                (task_id, claim_lock, run_id),
            ).fetchone()
            if task_row is None:
                conn.rollback()
                raise TaskConflictError(
                    f"finish CAS failed: task={task_id} run={run_id} "
                    f"claim_lock mismatch or task not RUNNING this run"
                )

            task = self._row_to_task(task_row)

            # Determine whether this outcome increments consecutive_failures.
            # Only retryable worker-failure outcomes increment; CRASHED and
            # TIMED_OUT map to EXPIRED (user-driven retry) and do not
            # participate in the auto-retry counter.
            increment_failures = outcome in (
                TaskRunOutcome.FAILED,
                TaskRunOutcome.SPAWN_FAILED,
            )
            new_consecutive = (
                task.consecutive_failures + 1 if increment_failures else task.consecutive_failures
            )

            # Determine new task status
            if target_task_status is not None:
                new_status = target_task_status
            else:
                new_status = self._outcome_to_task_status(outcome, new_consecutive, task.max_retries)

            # Write run terminal
            run_status = self._outcome_to_run_status(outcome)
            conn.execute(
                """
                UPDATE task_runs SET
                    status = ?, ended_at = ?, outcome = ?, summary = ?,
                    metadata_json = ?, error = ?
                WHERE id = ?
                """,
                (
                    run_status.value,
                    _dt_to_str(now),
                    outcome.value,
                    summary,
                    _json_dumps(dict(metadata)),
                    error,
                    run_id,
                ),
            )

            # Transition task + release lease
            set_parts = [
                "status = ?",
                "claim_lock = NULL",
                "claim_expires = NULL",
                "current_run_id = NULL",
                "worker_token = NULL",
                "version = version + 1",
                "updated_at = ?",
            ]
            params: list[Any] = [new_status.value, _dt_to_str(now)]

            if outcome == TaskRunOutcome.COMPLETED:
                set_parts.append("completed_at = ?")
                set_parts.append("result = ?")
                set_parts.append("consecutive_failures = 0")
                params.append(_dt_to_str(now))
                params.append(summary or "")
            elif increment_failures:
                set_parts.append("consecutive_failures = ?")
                set_parts.append("last_failure_error = ?")
                params.append(new_consecutive)
                params.append(error or summary or outcome.value)

            params.append(task_id)
            params.append(task.version)
            conn.execute(
                f"UPDATE tasks SET {', '.join(set_parts)} WHERE id = ? AND version = ?",
                params,
            )

            # Append terminal event
            event_kind = "finished" if not is_recover else "recovered"
            event_cursor = conn.execute(
                """
                INSERT INTO task_events (task_id, run_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    run_id,
                    event_kind,
                    _json_dumps(
                        {
                            "outcome": outcome.value,
                            "summary": summary,
                            "is_recover": is_recover,
                            "error": error,
                        }
                    ),
                    _dt_to_str(now),
                ),
            )
            event_id = event_cursor.lastrowid

            # Fetch final state
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            run_row = conn.execute(
                "SELECT * FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
            event_row = conn.execute(
                "SELECT * FROM task_events WHERE id = ?", (event_id,)
            ).fetchone()
            conn.commit()

        return FinishRunResult(
            task=self._row_to_task(task_row),
            run=self._row_to_run(run_row),
            terminal_event=self._row_to_event(event_row),
        )

    def _outcome_to_task_status(
        self,
        outcome: TaskRunOutcome,
        new_consecutive_failures: int,
        max_retries: int,
    ) -> TaskStatus:
        """Default outcome -> task status mapping (Manus-aligned).

        Used only when ``target_task_status`` is None; TaskRunService should
        pass ``target_task_status`` explicitly to own the decision.

        Mapping:
          COMPLETED            -> SUCCEEDED
          WAITING_APPROVAL     -> WAITING_APPROVAL
          EXPIRED              -> EXPIRED
          CRASHED / TIMED_OUT  -> EXPIRED  (worker died; user must retry)
          TERMINATED           -> CANCELLED (user cancel)
          FAILED / SPAWN_FAILED -> if consecutive_failures > max_retries:
                                  FAILED; else QUEUED (auto-retry)
        """
        if outcome == TaskRunOutcome.COMPLETED:
            return TaskStatus.SUCCEEDED
        if outcome == TaskRunOutcome.WAITING_APPROVAL:
            return TaskStatus.WAITING_APPROVAL
        if outcome == TaskRunOutcome.EXPIRED:
            return TaskStatus.EXPIRED
        if outcome in (TaskRunOutcome.CRASHED, TaskRunOutcome.TIMED_OUT):
            return TaskStatus.EXPIRED
        if outcome == TaskRunOutcome.TERMINATED:
            return TaskStatus.CANCELLED
        # Retryable: FAILED, SPAWN_FAILED
        if new_consecutive_failures > max_retries:
            return TaskStatus.FAILED
        return TaskStatus.QUEUED

    def _outcome_to_run_status(self, outcome: TaskRunOutcome) -> TaskRunStatus:
        """Map outcome to the terminal TaskRunStatus."""
        mapping = {
            TaskRunOutcome.COMPLETED: TaskRunStatus.COMPLETED,
            TaskRunOutcome.WAITING_APPROVAL: TaskRunStatus.COMPLETED,
            TaskRunOutcome.FAILED: TaskRunStatus.FAILED,
            TaskRunOutcome.CRASHED: TaskRunStatus.CRASHED,
            TaskRunOutcome.TIMED_OUT: TaskRunStatus.TIMED_OUT,
            TaskRunOutcome.TERMINATED: TaskRunStatus.TERMINATED,
            TaskRunOutcome.SPAWN_FAILED: TaskRunStatus.FAILED,
            TaskRunOutcome.EXPIRED: TaskRunStatus.TIMED_OUT,
        }
        return mapping.get(outcome, TaskRunStatus.FAILED)

    # ------------------------------------------------------------------
    # Dispatch helpers (sync)
    # ------------------------------------------------------------------

    def _list_queued_due_sync(
        self,
        now: datetime,
        limit: int = 100,
        board: str = "default",
    ) -> list[Task]:
        """Return QUEUED + due + not-archived tasks for dispatch.

        Ordered by priority DESC, created_at ASC, id ASC.
        """
        limit = max(1, min(limit, 500))
        now_utc = _aware_utc(now)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE board = ? AND status = ? AND is_archived = 0
                  AND (scheduled_at IS NULL OR scheduled_at <= ?)
                ORDER BY priority DESC, created_at ASC, id ASC
                LIMIT ?
                """,
                (
                    board,
                    TaskStatus.QUEUED.value,
                    _dt_to_str(now_utc),
                    limit,
                ),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _list_running_sync(self, board: str = "default") -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE board = ? AND status = ?
                ORDER BY started_at ASC, id ASC
                """,
                (board, TaskStatus.RUNNING.value),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    # ------------------------------------------------------------------
    # Comments (sync)
    # ------------------------------------------------------------------

    def _add_comment_sync(self, task_id: str, author: str, body: str) -> TaskComment:
        comment_id = f"tc_{uuid4().hex[:16]}"
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task not found: {task_id}")
            conn.execute(
                """
                INSERT INTO task_comments (id, task_id, author, body, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (comment_id, task_id, author, body, _dt_to_str(now)),
            )
        return TaskComment(
            id=comment_id,
            task_id=task_id,
            author=author,
            body=body,
            created_at=now,
        )

    def _list_comments_sync(self, task_id: str) -> list[TaskComment]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_comments
                WHERE task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_comment(r) for r in rows]

    # ------------------------------------------------------------------
    # Events (sync)
    # ------------------------------------------------------------------

    def _append_event_sync(
        self,
        task_id: str,
        kind: str,
        payload: Mapping[str, Any],
        run_id: int | None = None,
    ) -> TaskEvent:
        now = _now()
        payload_json = _json_dumps(dict(payload))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task not found: {task_id}")
            cursor = conn.execute(
                """
                INSERT INTO task_events (task_id, run_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, run_id, kind, payload_json, _dt_to_str(now)),
            )
            event_id = cursor.lastrowid
            event_row = conn.execute(
                "SELECT * FROM task_events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._row_to_event(event_row)

    def _list_events_sync(
        self, task_id: str, since: int = 0, limit: int = 100
    ) -> list[TaskEvent]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (task_id, since, limit),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    # ------------------------------------------------------------------
    # Runs (sync)
    # ------------------------------------------------------------------

    def _list_runs_sync(self, task_id: str, limit: int = 50) -> list[TaskRun]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_runs
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [self._row_to_run(r) for r in rows]

    # ------------------------------------------------------------------
    # Proposal resolution (sync) -- single-transaction CAS
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_resolution_command(command: ProposalResolutionCommand) -> None:
        """Entry-level defensive validation (no DB access).

        Checks the decision/event_kind correspondence and the revised-note
        invariant. Raises ``TaskValidationError`` on any violation. Called
        before opening the transaction so no DB side-effects are possible
        when the command itself is malformed.
        """
        if not command.task_id:
            raise TaskValidationError("task_id must not be empty")
        decision = command.decision
        if decision not in _DECISION_TO_EVENT_KIND:
            raise TaskValidationError(
                f"invalid decision: {decision!r}; expected one of "
                f"{sorted(_DECISION_TO_EVENT_KIND)}"
            )
        expected_kind = _DECISION_TO_EVENT_KIND[decision]
        if command.event_kind != expected_kind:
            raise TaskValidationError(
                f"decision/event_kind mismatch: decision={decision!r} "
                f"requires event_kind={expected_kind!r}, "
                f"got {command.event_kind!r}"
            )
        if decision == "revised":
            if not command.note or not command.note.strip():
                raise TaskValidationError(
                    "revised decision requires a non-empty note"
                )

    def _resolve_proposal_sync(
        self, command: ProposalResolutionCommand
    ) -> ProposalResolutionResult:
        """Atomically resolve the latest pending ``change_proposed`` event.

        Single ``BEGIN IMMEDIATE`` transaction:
          1. Entry validation (decision/event_kind, revised note).
          2. Read task; reject if missing/archived/wrong-status/version-mismatch.
          3. Read the FULL event history (no ``_MAX_EVENTS_IN_DETAIL`` cap)
             and locate the latest ``change_proposed`` whose ``id`` is not
             referenced by any resolution marker's ``proposal_event_id``
             payload. This precise payload match (not marker time) correctly
             handles interleaved proposals and markers.
          4. INSERT the decision event with payload
             ``{"decision", "note", "proposal_event_id"}`` (non-null id).
          5. CAS UPDATE the task row: ``WHERE id AND expected_version AND
             status='waiting_approval' AND is_archived=0``; require
             ``rowcount == 1`` else rollback + ``TaskConflictError``.
          6. Commit and return the re-read Task/Event.
        """
        # 1. Entry validation (before any DB access)
        self._validate_resolution_command(command)

        task_id = command.task_id
        expected_version = command.expected_version
        now = _now()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # 2. Read task and validate state
            task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if task_row is None:
                conn.rollback()
                raise TaskNotFoundError(f"task not found: {task_id}")

            task = self._row_to_task(task_row)

            # Version check first: a stale expected_version means a concurrent
            # writer already modified the task; this is a conflict, not a
            # state error, even if the status also changed as a consequence.
            if task.version != expected_version:
                conn.rollback()
                raise TaskConflictError(
                    f"version conflict: expected {expected_version}, "
                    f"got {task.version}"
                )
            if task.is_archived:
                conn.rollback()
                raise TaskStateError(
                    f"task {task_id} is archived; cannot resolve proposal"
                )
            if task.status is not TaskStatus.WAITING_APPROVAL:
                conn.rollback()
                raise TaskStateError(
                    f"resolve_proposal requires WAITING_APPROVAL, "
                    f"got {task.status.value}"
                )

            # 3. Read full event history (no LIMIT cap) and find the latest
            #    unresolved change_proposed by precise proposal_event_id match.
            event_rows = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
            events = [self._row_to_event(r) for r in event_rows]

            resolved_ids: set[int] = set()
            for ev in events:
                if ev.kind in _RESOLUTION_MARKER_KINDS:
                    pid = ev.payload.get("proposal_event_id")
                    if pid is not None:
                        resolved_ids.add(int(pid))

            latest_pending: TaskEvent | None = None
            for ev in reversed(events):
                if ev.kind == _PROPOSAL_KIND and ev.id not in resolved_ids:
                    latest_pending = ev
                    break

            if latest_pending is None:
                conn.rollback()
                raise TaskStateError(
                    f"task {task_id} has no pending change_proposed event"
                )

            proposal_event_id = latest_pending.id

            # 4. INSERT decision event (non-null proposal_event_id)
            decision_payload = {
                "decision": command.decision,
                "note": command.note,
                "proposal_event_id": proposal_event_id,
            }
            event_cursor = conn.execute(
                """
                INSERT INTO task_events (task_id, run_id, kind, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    None,  # resolution markers are not tied to a specific run
                    command.event_kind,
                    _json_dumps(decision_payload),
                    _dt_to_str(now),
                ),
            )
            decision_event_id = event_cursor.lastrowid

            # 5. CAS UPDATE with rowcount check (defensive: within BEGIN
            #    IMMEDIATE the row should always match, but if it doesn't
            #    we must rollback to avoid an orphan decision event).
            cas_cursor = conn.execute(
                """
                UPDATE tasks SET
                    status = ?, version = version + 1, updated_at = ?
                WHERE id = ? AND version = ?
                  AND status = ? AND is_archived = 0
                """,
                (
                    TaskStatus.QUEUED.value,
                    _dt_to_str(now),
                    task_id,
                    expected_version,
                    TaskStatus.WAITING_APPROVAL.value,
                ),
            )
            if cas_cursor.rowcount != 1:
                conn.rollback()
                raise TaskConflictError(
                    f"resolve_proposal CAS failed: task={task_id} "
                    f"expected_version={expected_version} rowcount={cas_cursor.rowcount}"
                )

            # 6. Re-read final state and commit
            final_task_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            decision_event_row = conn.execute(
                "SELECT * FROM task_events WHERE id = ?",
                (decision_event_id,),
            ).fetchone()
            conn.commit()

        return ProposalResolutionResult(
            proposal_event_id=proposal_event_id,
            task=self._row_to_task(final_task_row),
            decision_event=self._row_to_event(decision_event_row),
        )

    def _latest_waiting_approval_in_session_sync(
        self, session_id: str
    ) -> Task | None:
        """Return the latest WAITING_APPROVAL task in a session.

        Filters by ``origin_session_id``, ``status='waiting_approval'``, and
        ``is_archived=0``. Sorts by ``created_at DESC, id DESC`` for a stable
        deterministic order (id breaks created_at ties). Returns ``None``
        when no candidate exists. Raises ``TaskValidationError`` for an
        empty session_id.

        Uses a dedicated single SQL query (not ``list_tasks`` pagination)
        with ``LIMIT 1`` for efficiency.
        """
        if not session_id:
            raise TaskValidationError("session_id must not be empty")
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE origin_session_id = ?
                  AND status = ?
                  AND is_archived = 0
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (session_id, TaskStatus.WAITING_APPROVAL.value),
            ).fetchone()
        return self._row_to_task(row) if row else None

    # ------------------------------------------------------------------
    # Attachments (sync)
    # ------------------------------------------------------------------

    def _add_attachment_sync(
        self,
        task_id: str,
        filename: str,
        stored_name: str,
        content_type: str,
        size: int,
        checksum: str,
        uploaded_by: str,
    ) -> TaskAttachment:
        att_id = f"ta_{uuid4().hex[:16]}"
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task not found: {task_id}")
            conn.execute(
                """
                INSERT INTO task_attachments (
                    id, task_id, filename, stored_name, content_type,
                    size, checksum, uploaded_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    att_id,
                    task_id,
                    filename,
                    stored_name,
                    content_type,
                    size,
                    checksum,
                    uploaded_by,
                    _dt_to_str(now),
                ),
            )
        return TaskAttachment(
            id=att_id,
            task_id=task_id,
            filename=filename,
            stored_name=stored_name,
            content_type=content_type,
            size=size,
            checksum=checksum,
            uploaded_by=uploaded_by,
            created_at=now,
        )

    def _list_attachments_sync(self, task_id: str) -> list[TaskAttachment]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_attachments
                WHERE task_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (task_id,),
            ).fetchall()
        return [self._row_to_attachment(r) for r in rows]

    def _get_attachment_sync(self, attachment_id: str) -> TaskAttachment | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM task_attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
        return self._row_to_attachment(row) if row else None

    def _delete_attachment_sync(self, attachment_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM task_attachments WHERE id = ?", (attachment_id,)
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Notify subs (sync)
    # ------------------------------------------------------------------

    def _subscribe_notify_sync(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool:
        now = _now()
        # Normalize thread_id: NULL is distinct in SQLite PK, so use ''
        tid = thread_id or ""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task not found: {task_id}")
            conn.execute(
                """
                INSERT INTO task_notify_subs (
                    task_id, platform, chat_id, thread_id,
                    last_terminal_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(task_id, platform, chat_id, thread_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    platform,
                    chat_id,
                    tid,
                    _dt_to_str(now),
                    _dt_to_str(now),
                ),
            )
        return True

    def _list_notify_subs_sync(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, platform, chat_id, thread_id, last_terminal_event_id
                FROM task_notify_subs
                WHERE task_id = ?
                ORDER BY created_at ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "task_id": r["task_id"],
                "platform": r["platform"],
                "chat_id": r["chat_id"],
                "thread_id": r["thread_id"] or None,
                "last_terminal_event_id": r["last_terminal_event_id"],
            }
            for r in rows
        ]

    def _unsubscribe_notify_sync(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool:
        tid = thread_id or ""
        with self._connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM task_notify_subs
                WHERE task_id = ? AND platform = ? AND chat_id = ? AND thread_id = ?
                """,
                (task_id, platform, chat_id, tid),
            )
            return cursor.rowcount > 0

    def _update_notify_sub_last_event_sync(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        last_terminal_event_id: int,
    ) -> bool:
        """Update last_terminal_event_id for a notify sub (idempotency cursor).

        Only advances forward; never lowers the watermark (a duplicate or
        out-of-order delivery attempt has no effect). Returns True if the row
        was updated, False if no matching sub exists.
        """
        tid = thread_id or ""
        now = _now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE task_notify_subs
                SET last_terminal_event_id = ?, updated_at = ?
                WHERE task_id = ? AND platform = ? AND chat_id = ?
                  AND thread_id = ? AND last_terminal_event_id < ?
                """,
                (
                    last_terminal_event_id,
                    _dt_to_str(now),
                    task_id,
                    platform,
                    chat_id,
                    tid,
                    last_terminal_event_id,
                ),
            )
            return cursor.rowcount > 0

    # ==================================================================
    # Async public API (wraps sync via asyncio.to_thread)
    # ==================================================================

    async def create_task(self, task: Task) -> Task:
        return await asyncio.to_thread(self._create_task_sync, task)

    async def get_task(self, task_id: str) -> Task | None:
        return await asyncio.to_thread(self._get_task_sync, task_id)

    async def list_tasks(
        self,
        board: str = "default",
        cursor: TaskListCursor | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> TaskListPage:
        return await asyncio.to_thread(
            self._list_tasks_sync, board, cursor, limit, include_archived
        )

    async def update_task(
        self,
        task_id: str,
        fields: Mapping[str, Any],
        expected_version: int,
    ) -> Task:
        return await asyncio.to_thread(
            self._update_task_sync, task_id, fields, expected_version
        )

    async def bulk_update(self, command: BulkUpdateCommand) -> BulkUpdateResult:
        return await asyncio.to_thread(self._bulk_update_sync, command)

    async def delete_task(self, task_id: str) -> bool:
        return await asyncio.to_thread(self._delete_task_sync, task_id)

    async def claim_task(
        self,
        task_id: str,
        claim_lock: str,
        lease_seconds: int,
    ) -> ClaimResult | None:
        return await asyncio.to_thread(
            self._claim_task_sync, task_id, claim_lock, lease_seconds
        )

    async def record_heartbeat(
        self,
        task_id: str,
        run_id: int,
        claim_lock: str,
        now: datetime,
    ) -> Task:
        return await asyncio.to_thread(
            self._record_heartbeat_sync, task_id, run_id, claim_lock, now
        )

    async def finish_run(self, command: FinishRunCommand) -> FinishRunResult:
        return await asyncio.to_thread(self._finish_run_sync, command)

    async def recover_run(self, command: RecoverRunCommand) -> FinishRunResult:
        return await asyncio.to_thread(self._recover_run_sync, command)

    async def resolve_proposal(
        self, command: ProposalResolutionCommand
    ) -> ProposalResolutionResult:
        return await asyncio.to_thread(self._resolve_proposal_sync, command)

    async def latest_waiting_approval_in_session(
        self, session_id: str
    ) -> Task | None:
        return await asyncio.to_thread(
            self._latest_waiting_approval_in_session_sync, session_id
        )

    async def list_queued_due(
        self,
        now: datetime,
        limit: int = 100,
        board: str = "default",
    ) -> tuple[Task, ...]:
        result = await asyncio.to_thread(
            self._list_queued_due_sync, now, limit, board
        )
        return tuple(result)

    async def list_running(self, board: str = "default") -> tuple[Task, ...]:
        result = await asyncio.to_thread(self._list_running_sync, board)
        return tuple(result)

    async def add_comment(
        self, task_id: str, author: str, body: str
    ) -> TaskComment:
        return await asyncio.to_thread(
            self._add_comment_sync, task_id, author, body
        )

    async def list_comments(self, task_id: str) -> tuple[TaskComment, ...]:
        result = await asyncio.to_thread(self._list_comments_sync, task_id)
        return tuple(result)

    async def append_event(
        self,
        task_id: str,
        kind: str,
        payload: Mapping[str, Any],
        run_id: int | None = None,
    ) -> TaskEvent:
        return await asyncio.to_thread(
            self._append_event_sync, task_id, kind, payload, run_id
        )

    async def list_events(
        self, task_id: str, since: int = 0, limit: int = 100
    ) -> tuple[TaskEvent, ...]:
        result = await asyncio.to_thread(
            self._list_events_sync, task_id, since, limit
        )
        return tuple(result)

    async def list_runs(
        self, task_id: str, limit: int = 50
    ) -> tuple[TaskRun, ...]:
        result = await asyncio.to_thread(self._list_runs_sync, task_id, limit)
        return tuple(result)

    async def add_attachment(
        self,
        task_id: str,
        filename: str,
        stored_name: str,
        content_type: str,
        size: int,
        checksum: str,
        uploaded_by: str,
    ) -> TaskAttachment:
        return await asyncio.to_thread(
            self._add_attachment_sync,
            task_id,
            filename,
            stored_name,
            content_type,
            size,
            checksum,
            uploaded_by,
        )

    async def list_attachments(self, task_id: str) -> tuple[TaskAttachment, ...]:
        result = await asyncio.to_thread(self._list_attachments_sync, task_id)
        return tuple(result)

    async def get_attachment(self, attachment_id: str) -> TaskAttachment | None:
        return await asyncio.to_thread(self._get_attachment_sync, attachment_id)

    async def delete_attachment(self, attachment_id: str) -> bool:
        return await asyncio.to_thread(self._delete_attachment_sync, attachment_id)

    async def subscribe_notify(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._subscribe_notify_sync,
            task_id,
            platform,
            chat_id,
            thread_id,
        )

    async def list_notify_subs(self, task_id: str) -> tuple[dict[str, Any], ...]:
        result = await asyncio.to_thread(self._list_notify_subs_sync, task_id)
        return tuple(result)

    async def unsubscribe_notify(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._unsubscribe_notify_sync,
            task_id,
            platform,
            chat_id,
            thread_id,
        )

    async def update_notify_sub_last_event(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        thread_id: str | None,
        last_terminal_event_id: int,
    ) -> bool:
        return await asyncio.to_thread(
            self._update_notify_sub_last_event_sync,
            task_id,
            platform,
            chat_id,
            thread_id,
            last_terminal_event_id,
        )
