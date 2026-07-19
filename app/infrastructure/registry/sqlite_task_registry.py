"""SQLite persistence for the Task subdomain (Infrastructure Layer).

Implements ``TaskRegistry`` async Protocol from ``app/domain/task.py``.
Shares the sessions.db path but opens independent connections. Enables WAL,
foreign_keys, and busy_timeout per connection. Async methods wrap sync
sqlite3 via ``asyncio.to_thread`` (tech debt D018: to_thread wrapping).

Atomicity guarantees:
  - claim_task: BEGIN IMMEDIATE, only READY + no valid claim + version match
    -> insert run + update task to RUNNING in one transaction.
  - finish_run / recover_run: CAS on (run_id, claim_lock) in one transaction;
    writes run terminal, transitions task, appends terminal event, releases
    lease. Late/duplicate results raise TaskConflictError.
  - record_heartbeat: CAS on (run_id, claim_lock), renews claim_expires.
  - bulk_update / create_graph: single transaction, all-or-nothing.

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
    BlockKind,
    BulkUpdateCommand,
    BulkUpdateItem,
    BulkUpdateResult,
    ClaimResult,
    CreateGraphCommand,
    CreateGraphResult,
    DeliveryResult,
    FinishRunCommand,
    FinishRunResult,
    RecoverRunCommand,
    Task,
    TaskAttachment,
    TaskClaimError,
    TaskComment,
    TaskConflictError,
    TaskEvent,
    TaskExecutionPolicy,
    TaskLink,
    TaskListCursor,
    TaskListPage,
    TaskNotFoundError,
    TaskRun,
    TaskRunOutcome,
    TaskRunStatus,
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

CREATE TABLE IF NOT EXISTS task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(parent_id, child_id),
    FOREIGN KEY(parent_id) REFERENCES tasks(id) ON DELETE CASCADE,
    FOREIGN KEY(child_id) REFERENCES tasks(id) ON DELETE CASCADE
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
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_session_id ON tasks(origin_session_id, execution_session_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_idempotency
    ON tasks(board, created_by, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_links_parent ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_links_child ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_runs_task ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_run ON task_events(run_id);
CREATE INDEX IF NOT EXISTS idx_attachments_task ON task_attachments(task_id);
CREATE INDEX IF NOT EXISTS idx_notify_task ON task_notify_subs(task_id);
"""

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
    "status", "block_kind", "workspace_kind", "pre_archive_status",
})

# Mapping from Task field name to column name (when they differ)
_FIELD_TO_COLUMN: dict[str, str] = {
    "skills": "skills_json",
    "execution_policy": "execution_policy_json",
}

# Fields that cannot be updated via update_task
_IMMUTABLE_FIELDS: frozenset[str] = frozenset({"id", "created_at"})


# ---------------------------------------------------------------------------
# Outcome -> task status mapping for finish_run / recover_run
# ---------------------------------------------------------------------------

_RETRYABLE_OUTCOMES: frozenset[TaskRunOutcome] = frozenset({
    TaskRunOutcome.FAILED,
    TaskRunOutcome.CRASHED,
    TaskRunOutcome.TIMED_OUT,
    TaskRunOutcome.SPAWN_FAILED,
    TaskRunOutcome.RECLAIMED,
})


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
    if field_name == "skills":
        return _json_dumps(list(value))
    if field_name == "execution_policy":
        if isinstance(value, TaskExecutionPolicy):
            return _json_dumps({"allowed_tools": list(value.allowed_tools)})
        return _json_dumps(value)
    if field_name == "goal_mode":
        return 1 if value else 0
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
        """Idempotent schema creation. Safe for empty DBs, repeated startup,
        and half-migrated DBs (CREATE IF NOT EXISTS)."""
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # Migration: add lease_seconds to task_runs if missing (legacy DBs)
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(task_runs)")
            }
            if "lease_seconds" not in cols:
                conn.execute(
                    "ALTER TABLE task_runs ADD COLUMN lease_seconds INTEGER"
                )

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
            assignee=row["assignee"],
            priority=row["priority"],
            created_by=row["created_by"],
            created_at=_str_to_dt(row["created_at"]),
            updated_at=_str_to_dt(row["updated_at"]),
            version=row["version"],
            status=TaskStatus(row["status"]),
            block_kind=_str_to_enum(BlockKind, row["block_kind"]),
            block_reason=row["block_reason"],
            block_recurrences=row["block_recurrences"],
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
            pre_archive_status=_str_to_enum(TaskStatus, row["pre_archive_status"]),
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

    def _row_to_link(self, row: sqlite3.Row) -> TaskLink:
        return TaskLink(parent_id=row["parent_id"], child_id=row["child_id"])

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
            task.assignee,
            task.priority,
            task.created_by,
            _dt_to_str(created_at),
            _dt_to_str(updated_at),
            task.version,
            _enum_to_str(task.status),
            _enum_to_str(task.block_kind),
            task.block_reason,
            task.block_recurrences,
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
            _enum_to_str(task.pre_archive_status),
        )

    _INSERT_TASK_SQL = """
        INSERT INTO tasks (
            id, title, body, assignee, priority, created_by, created_at, updated_at,
            version, status, block_kind, block_reason, block_recurrences,
            started_at, completed_at, scheduled_at, claim_lock, claim_expires,
            current_run_id, workspace_kind, workspace_path, skills_json,
            execution_policy_json, model_override, max_runtime_seconds, max_retries,
            goal_mode, goal_max_turns, workflow_template_id, current_step_key,
            project_id, tenant, board, origin_session_id, execution_session_id,
            consecutive_failures, worker_token, last_failure_error, last_heartbeat_at,
            result, idempotency_key, pre_archive_status
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
        resolved = Task(
            id=task.id,
            title=task.title,
            body=task.body,
            assignee=task.assignee,
            priority=task.priority,
            created_by=task.created_by,
            created_at=created_at,
            updated_at=updated_at,
            version=task.version,
            status=task.status,
            block_kind=task.block_kind,
            block_reason=task.block_reason,
            block_recurrences=task.block_recurrences,
            started_at=task.started_at,
            completed_at=task.completed_at,
            scheduled_at=task.scheduled_at,
            claim_lock=task.claim_lock,
            claim_expires=task.claim_expires,
            current_run_id=task.current_run_id,
            workspace_kind=task.workspace_kind,
            workspace_path=task.workspace_path,
            skills=task.skills,
            execution_policy=task.execution_policy,
            model_override=task.model_override,
            max_runtime_seconds=task.max_runtime_seconds,
            max_retries=task.max_retries,
            goal_mode=task.goal_mode,
            goal_max_turns=task.goal_max_turns,
            workflow_template_id=task.workflow_template_id,
            current_step_key=task.current_step_key,
            project_id=task.project_id,
            tenant=task.tenant,
            board=task.board,
            origin_session_id=task.origin_session_id,
            execution_session_id=task.execution_session_id,
            consecutive_failures=task.consecutive_failures,
            worker_token=task.worker_token,
            last_failure_error=task.last_failure_error,
            last_heartbeat_at=task.last_heartbeat_at,
            result=task.result,
            idempotency_key=task.idempotency_key,
            pre_archive_status=task.pre_archive_status,
        )
        with self._connect() as conn:
            try:
                conn.execute(self._INSERT_TASK_SQL, self._task_params(resolved))
            except sqlite3.IntegrityError as e:
                # Could be duplicate PK or idempotency key conflict
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
    ) -> TaskListPage:
        # Clamp limit to a safe range
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            if cursor is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE board = ?
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
                    """
                    SELECT * FROM tasks
                    WHERE board = ?
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
            # Fetch all updated rows
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
            # CAS: status=READY and no valid claim
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE id = ? AND status = ?
                  AND (claim_expires IS NULL OR claim_expires < ?)
                """,
                (task_id, TaskStatus.READY.value, _dt_to_str(now)),
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
                    f"claim_lock mismatch or task not RUNNING"
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
            error=None,
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

            # Determine new task status
            task = self._row_to_task(task_row)
            # Increment failures for any outcome that is not COMPLETED,
            # BLOCKED, or GAVE_UP (matches the previous default-branch logic).
            increment_failures = outcome not in (
                TaskRunOutcome.COMPLETED,
                TaskRunOutcome.BLOCKED,
                TaskRunOutcome.GAVE_UP,
            )
            if target_task_status is not None:
                new_status = target_task_status
            else:
                new_status = self._outcome_to_task_status(outcome)

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
                set_parts.append("consecutive_failures = consecutive_failures + 1")
                set_parts.append("last_failure_error = ?")
                params.append(error or summary or outcome.value)
            elif outcome == TaskRunOutcome.GAVE_UP:
                set_parts.append("block_kind = ?")
                set_parts.append("block_reason = ?")
                params.append(BlockKind.NEEDS_INPUT.value)
                params.append("circuit breaker: max_retries exceeded")

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

    def _outcome_to_task_status(self, outcome: TaskRunOutcome) -> TaskStatus:
        """Default outcome -> task status mapping.

        This is the registry's standalone default. TaskRunService (Batch D)
        should pass ``target_task_status`` in ``FinishRunCommand`` to own
        the decision; this fallback exists so the registry works standalone.
        """
        if outcome == TaskRunOutcome.COMPLETED:
            return TaskStatus.DONE
        if outcome in (TaskRunOutcome.BLOCKED, TaskRunOutcome.GAVE_UP):
            return TaskStatus.BLOCKED
        # Retryable outcomes -> TODO
        return TaskStatus.TODO

    def _outcome_to_run_status(self, outcome: TaskRunOutcome) -> TaskRunStatus:
        """Map outcome to the terminal TaskRunStatus."""
        mapping = {
            TaskRunOutcome.COMPLETED: TaskRunStatus.COMPLETED,
            TaskRunOutcome.BLOCKED: TaskRunStatus.BLOCKED,
            TaskRunOutcome.FAILED: TaskRunStatus.FAILED,
            TaskRunOutcome.CRASHED: TaskRunStatus.CRASHED,
            TaskRunOutcome.TIMED_OUT: TaskRunStatus.TIMED_OUT,
            TaskRunOutcome.TERMINATED: TaskRunStatus.TERMINATED,
            TaskRunOutcome.SPAWN_FAILED: TaskRunStatus.FAILED,
            TaskRunOutcome.GAVE_UP: TaskRunStatus.FAILED,
            TaskRunOutcome.RECLAIMED: TaskRunStatus.RECLAIMED,
        }
        return mapping.get(outcome, TaskRunStatus.FAILED)

    # ------------------------------------------------------------------
    # Dispatch helpers (sync)
    # ------------------------------------------------------------------

    def _list_ready_sync(self, board: str = "default", limit: int = 100) -> list[Task]:
        limit = max(1, min(limit, 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE board = ? AND status = ?
                ORDER BY priority DESC, created_at ASC, id ASC
                LIMIT ?
                """,
                (board, TaskStatus.READY.value, limit),
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

    def _recompute_ready_sync(self, board: str = "default") -> list[str]:
        now = _now()
        promoted: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")

            # 1. Demote READY tasks whose parents are not all DONE -> TODO
            ready_rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE board = ? AND status = ?
                """,
                (board, TaskStatus.READY.value),
            ).fetchall()
            for row in ready_rows:
                task_id = row["id"]
                non_done_parents = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM task_links l
                    JOIN tasks t ON t.id = l.parent_id
                    WHERE l.child_id = ? AND t.status != ?
                    """,
                    (task_id, TaskStatus.DONE.value),
                ).fetchone()
                if non_done_parents["cnt"] > 0:
                    conn.execute(
                        """
                        UPDATE tasks SET status = ?, version = version + 1, updated_at = ?
                        WHERE id = ?
                        """,
                        (TaskStatus.TODO.value, _dt_to_str(now), task_id),
                    )

            # 2. Notify RUNNING tasks whose parents are not all DONE.
            #    RUNNING tasks are NOT demoted (not preempted); a
            #    dependency_changed event is appended so the running worker
            #    can react.
            running_rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE board = ? AND status = ?
                """,
                (board, TaskStatus.RUNNING.value),
            ).fetchall()
            for row in running_rows:
                task_id = row["id"]
                non_done_parents = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM task_links l
                    JOIN tasks t ON t.id = l.parent_id
                    WHERE l.child_id = ? AND t.status != ?
                    """,
                    (task_id, TaskStatus.DONE.value),
                ).fetchone()
                if non_done_parents["cnt"] > 0:
                    conn.execute(
                        """
                        INSERT INTO task_events (task_id, run_id, kind, payload_json, created_at)
                        VALUES (?, ?, 'dependency_changed', ?, ?)
                        """,
                        (
                            task_id,
                            row["current_run_id"],
                            _json_dumps({"reason": "parent left DONE"}),
                            _dt_to_str(now),
                        ),
                    )

            # 3. Promote TODO/SCHEDULED tasks whose parents are all DONE +
            #    assignee + scheduled -> READY
            candidate_rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE board = ? AND status IN (?, ?) AND assignee IS NOT NULL
                """,
                (board, TaskStatus.TODO.value, TaskStatus.SCHEDULED.value),
            ).fetchall()
            for row in candidate_rows:
                task = self._row_to_task(row)
                # Check all parents DONE
                non_done_parents = conn.execute(
                    """
                    SELECT COUNT(*) AS cnt FROM task_links l
                    JOIN tasks t ON t.id = l.parent_id
                    WHERE l.child_id = ? AND t.status != ?
                    """,
                    (task.id, TaskStatus.DONE.value),
                ).fetchone()
                if non_done_parents["cnt"] > 0:
                    continue
                # Check scheduled_at (applies to both TODO and SCHEDULED)
                if task.scheduled_at is not None and task.scheduled_at > now:
                    continue
                # Promote
                conn.execute(
                    """
                    UPDATE tasks SET status = ?, version = version + 1, updated_at = ?
                    WHERE id = ?
                    """,
                    (TaskStatus.READY.value, _dt_to_str(now), task.id),
                )
                promoted.append(task.id)

            conn.commit()
        return promoted

    # ------------------------------------------------------------------
    # Dependency graph (sync)
    # ------------------------------------------------------------------

    def _has_path_sync(self, conn: sqlite3.Connection, start_id: str, target_id: str) -> bool:
        """DFS: is there a path start_id -> ... -> target_id in the dependency graph?

        Edges: parent_id -> child_id (parent must complete before child).
        """
        if start_id == target_id:
            return True
        visited: set[str] = set()
        stack: list[str] = [start_id]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            children = conn.execute(
                "SELECT child_id FROM task_links WHERE parent_id = ?", (node,)
            ).fetchall()
            for child in children:
                child_id = child["child_id"]
                if child_id == target_id:
                    return True
                stack.append(child_id)
        return False

    def _add_link_sync(self, parent_id: str, child_id: str) -> TaskLink:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Self-loop
            if parent_id == child_id:
                conn.rollback()
                raise TaskValidationError("self-loop not allowed")
            # Both tasks must exist
            parent_row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (parent_id,)
            ).fetchone()
            child_row = conn.execute(
                "SELECT board FROM tasks WHERE id = ?", (child_id,)
            ).fetchone()
            if parent_row is None or child_row is None:
                conn.rollback()
                raise TaskNotFoundError("parent or child task not found")
            # Cross-board check
            if parent_row["board"] != child_row["board"]:
                conn.rollback()
                raise TaskValidationError(
                    f"cross-board link not allowed: {parent_row['board']} vs {child_row['board']}"
                )
            # Duplicate check
            existing = conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
                (parent_id, child_id),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                raise TaskConflictError(
                    f"duplicate link: {parent_id} -> {child_id}"
                )
            # Cycle check: adding parent->child creates a cycle if there's
            # already a path from child to parent
            if self._has_path_sync(conn, child_id, parent_id):
                conn.rollback()
                raise TaskValidationError(
                    f"cycle detected: {parent_id} -> {child_id}"
                )
            conn.execute(
                """
                INSERT INTO task_links (parent_id, child_id, created_at)
                VALUES (?, ?, ?)
                """,
                (parent_id, child_id, _dt_to_str(now)),
            )
            conn.commit()
        return TaskLink(parent_id=parent_id, child_id=child_id)

    def _remove_link_sync(self, parent_id: str, child_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
                (parent_id, child_id),
            )
            return cursor.rowcount > 0

    def _list_links_sync(self, task_id: str) -> list[TaskLink]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM task_links
                WHERE parent_id = ? OR child_id = ?
                ORDER BY created_at ASC
                """,
                (task_id, task_id),
            ).fetchall()
        return [self._row_to_link(r) for r in rows]

    def _list_children_sync(self, parent_id: str) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM tasks t
                JOIN task_links l ON l.child_id = t.id
                WHERE l.parent_id = ?
                ORDER BY t.created_at ASC, t.id ASC
                """,
                (parent_id,),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _list_parents_sync(self, child_id: str) -> list[Task]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.* FROM tasks t
                JOIN task_links l ON l.parent_id = t.id
                WHERE l.child_id = ?
                ORDER BY t.created_at ASC, t.id ASC
                """,
                (child_id,),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def _create_graph_sync(self, command: CreateGraphCommand) -> CreateGraphResult:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # 1. Insert all tasks
            for task in command.tasks:
                resolved = dataclass_replace(
                    task,
                    created_at=task.created_at or now,
                    updated_at=task.updated_at or now,
                )
                try:
                    conn.execute(self._INSERT_TASK_SQL, self._task_params(resolved))
                except sqlite3.IntegrityError as e:
                    conn.rollback()
                    raise TaskConflictError(
                        f"create_graph task integrity error: {e}"
                    ) from e
            # 2. Validate and insert links
            for link in command.links:
                if link.parent_id == link.child_id:
                    conn.rollback()
                    raise TaskValidationError("self-loop not allowed")
                parent_row = conn.execute(
                    "SELECT board FROM tasks WHERE id = ?", (link.parent_id,)
                ).fetchone()
                child_row = conn.execute(
                    "SELECT board FROM tasks WHERE id = ?", (link.child_id,)
                ).fetchone()
                if parent_row is None or child_row is None:
                    conn.rollback()
                    raise TaskNotFoundError("parent or child task not found")
                if parent_row["board"] != child_row["board"]:
                    conn.rollback()
                    raise TaskValidationError("cross-board link not allowed")
                existing = conn.execute(
                    "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
                    (link.parent_id, link.child_id),
                ).fetchone()
                if existing is not None:
                    conn.rollback()
                    raise TaskConflictError(
                        f"duplicate link: {link.parent_id} -> {link.child_id}"
                    )
                # Cycle check (within graph being created)
                if self._has_path_sync(conn, link.child_id, link.parent_id):
                    conn.rollback()
                    raise TaskValidationError(
                        f"cycle detected: {link.parent_id} -> {link.child_id}"
                    )
                conn.execute(
                    """
                    INSERT INTO task_links (parent_id, child_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (link.parent_id, link.child_id, _dt_to_str(now)),
                )
            # 3. Insert comments
            for comment in command.comments:
                conn.execute(
                    """
                    INSERT INTO task_comments (id, task_id, author, body, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        comment.id,
                        comment.task_id,
                        comment.author,
                        comment.body,
                        _dt_to_str(comment.created_at or now),
                    ),
                )
            conn.commit()
        return CreateGraphResult(
            tasks=command.tasks,
            links=command.links,
            comments=command.comments,
        )

    # ------------------------------------------------------------------
    # Comments (sync)
    # ------------------------------------------------------------------

    def _add_comment_sync(self, task_id: str, author: str, body: str) -> TaskComment:
        comment_id = f"tc_{uuid4().hex[:16]}"
        now = _now()
        with self._connect() as conn:
            # Verify task exists
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
            # Verify task exists
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
                # Normalize '' back to None for the domain
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
        # Normalize thread_id to match stored '' convention
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
    ) -> TaskListPage:
        return await asyncio.to_thread(
            self._list_tasks_sync, board, cursor, limit
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

    async def list_ready(
        self, board: str = "default", limit: int = 100
    ) -> tuple[Task, ...]:
        result = await asyncio.to_thread(self._list_ready_sync, board, limit)
        return tuple(result)

    async def list_running(self, board: str = "default") -> tuple[Task, ...]:
        result = await asyncio.to_thread(self._list_running_sync, board)
        return tuple(result)

    async def recompute_ready(self, board: str = "default") -> tuple[str, ...]:
        result = await asyncio.to_thread(self._recompute_ready_sync, board)
        return tuple(result)

    async def create_graph(self, command: CreateGraphCommand) -> CreateGraphResult:
        return await asyncio.to_thread(self._create_graph_sync, command)

    async def add_link(self, parent_id: str, child_id: str) -> TaskLink:
        return await asyncio.to_thread(self._add_link_sync, parent_id, child_id)

    async def remove_link(self, parent_id: str, child_id: str) -> bool:
        return await asyncio.to_thread(self._remove_link_sync, parent_id, child_id)

    async def list_links(self, task_id: str) -> tuple[TaskLink, ...]:
        result = await asyncio.to_thread(self._list_links_sync, task_id)
        return tuple(result)

    async def list_children(self, parent_id: str) -> tuple[Task, ...]:
        result = await asyncio.to_thread(self._list_children_sync, parent_id)
        return tuple(result)

    async def list_parents(self, child_id: str) -> tuple[Task, ...]:
        result = await asyncio.to_thread(self._list_parents_sync, child_id)
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
