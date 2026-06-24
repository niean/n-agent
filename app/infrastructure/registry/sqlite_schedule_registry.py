from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.domain.schedule import (
    DeliveryTarget,
    DeliveryTargetType,
    ScheduledExecutionPolicy,
    ScheduledExecutionPolicyMode,
    ScheduledTask,
    ScheduledTaskClaim,
    ScheduledTaskExecution,
    ScheduledTaskExecutionStatus,
    ScheduledTaskStatus,
    ScheduleCalculator,
    ScheduleExpression,
    ScheduleTimezone,
)


class SQLiteScheduledTaskRegistry:
    def __init__(self, path: Path, calculator: ScheduleCalculator, missed_grace_seconds: int = 300):
        self.path = Path(path)
        self.calculator = calculator
        self.missed_grace_seconds = missed_grace_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def initialize(self) -> None:
        with self._connect() as conn:
            _initialize_schedule_schema(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def create(self, task: ScheduledTask) -> ScheduledTask:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks(id, name, prompt, cron_expression, timezone, enabled, status,
                    session_id, origin_json, delivery_target, delivery_context_json, execution_policy_json,
                    next_run_at, lease_until, lease_owner, claim_id, last_run_at, last_status, last_error,
                    last_delivery_error, unread_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._task_params(task),
            )
        created = await self.get(task.id)
        assert created is not None
        return created

    async def list(self) -> list[ScheduledTask]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM scheduled_tasks WHERE status != ? ORDER BY created_at ASC", (ScheduledTaskStatus.DELETED.value,)).fetchall()
        return [self._row_to_task(row) for row in rows]

    async def get(self, task_id: str) -> ScheduledTask | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ? AND status != ?", (task_id, ScheduledTaskStatus.DELETED.value)).fetchone()
        return self._row_to_task(row) if row else None

    async def update(self, task: ScheduledTask) -> ScheduledTask:
        params = self._task_params(task)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_tasks SET name=?, prompt=?, cron_expression=?, timezone=?, enabled=?, status=?,
                    session_id=?, origin_json=?, delivery_target=?, delivery_context_json=?, execution_policy_json=?,
                    next_run_at=?, lease_until=?, lease_owner=?, claim_id=?, last_run_at=?, last_status=?, last_error=?,
                    last_delivery_error=?, unread_count=?, updated_at=? WHERE id=?
                """,
                (*params[1:21], _iso(_now()), task.id),
            )
        updated = await self.get(task.id)
        assert updated is not None
        return updated

    async def update_status(self, task_id: str, status: ScheduledTaskStatus, enabled: bool) -> ScheduledTask:
        with self._connect() as conn:
            conn.execute(
                "UPDATE scheduled_tasks SET status = ?, enabled = ?, updated_at = ? WHERE id = ?",
                (status.value, int(enabled), _iso(_now()), task_id),
            )
        task = await self.get(task_id)
        assert task is not None
        return task

    async def delete(self, task_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
            return cursor.rowcount > 0

    async def claim_due_tasks(self, now: datetime, limit: int, lease_seconds: int) -> list[ScheduledTaskClaim]:
        now = _aware_utc(now)
        claims: list[ScheduledTaskClaim] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE enabled = 1 AND status = ? AND next_run_at <= ?
                  AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY next_run_at ASC LIMIT ?
                """,
                (ScheduledTaskStatus.ACTIVE.value, _iso(now), _iso(now), limit),
            ).fetchall()
            for row in rows:
                task = self._row_to_task(row)
                next_run = self.calculator.next_after(task.schedule, now, task.timezone)
                skipped = now - task.next_run_at > timedelta(seconds=self.missed_grace_seconds)
                claim = self._claim(task, now, lease_seconds, "due", next_run, skipped)
                cursor = conn.execute(
                    """
                    UPDATE scheduled_tasks SET lease_until=?, lease_owner=?, claim_id=?, next_run_at=?,
                        last_run_at=CASE WHEN ? THEN ? ELSE last_run_at END,
                        last_status=CASE WHEN ? THEN ? ELSE last_status END,
                        updated_at=?
                    WHERE id=? AND enabled=1 AND status=? AND (lease_until IS NULL OR lease_until < ?)
                    """,
                    (
                        _iso(claim.lease_until),
                        claim.lease_owner,
                        claim.claim_id,
                        _iso(claim.next_run_at),
                        int(skipped),
                        _iso(now),
                        int(skipped),
                        ScheduledTaskExecutionStatus.SKIPPED_MISSED.value,
                        _iso(now),
                        task.id,
                        ScheduledTaskStatus.ACTIVE.value,
                        _iso(now),
                    ),
                )
                if cursor.rowcount:
                    claims.append(claim)
            conn.commit()
        return claims

    async def claim_task_for_run_now(self, task_id: str, now: datetime, lease_seconds: int) -> ScheduledTaskClaim | None:
        now = _aware_utc(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE id=? AND enabled=1 AND status=? AND (
                    lease_until IS NULL OR lease_until < ? OR EXISTS(
                        SELECT 1 FROM scheduled_task_executions e
                        WHERE e.task_id=scheduled_tasks.id
                          AND e.claim_id=scheduled_tasks.claim_id
                          AND e.lease_owner=scheduled_tasks.lease_owner
                          AND e.status != ?
                    )
                )
                """,
                (task_id, ScheduledTaskStatus.ACTIVE.value, _iso(now), ScheduledTaskExecutionStatus.RUNNING.value),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task = self._row_to_task(row)
            claim = self._claim(task, now, lease_seconds, "run_now", task.next_run_at, False)
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks SET lease_until=?, lease_owner=?, claim_id=?, updated_at=?
                WHERE id=? AND enabled=1 AND status=? AND (
                    lease_until IS NULL OR lease_until < ? OR EXISTS(
                        SELECT 1 FROM scheduled_task_executions e
                        WHERE e.task_id=scheduled_tasks.id
                          AND e.claim_id=scheduled_tasks.claim_id
                          AND e.lease_owner=scheduled_tasks.lease_owner
                          AND e.status != ?
                    )
                )
                """,
                (
                    _iso(claim.lease_until),
                    claim.lease_owner,
                    claim.claim_id,
                    _iso(now),
                    task.id,
                    ScheduledTaskStatus.ACTIVE.value,
                    _iso(now),
                    ScheduledTaskExecutionStatus.RUNNING.value,
                ),
            )
            conn.commit()
        return claim if cursor.rowcount else None

    async def record_execution_started(self, execution: ScheduledTaskExecution) -> ScheduledTaskExecution:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_task_executions(id, task_id, session_id, claim_id, lease_owner,
                    claimed_next_run_at, started_at, completed_at, status, output, error,
                    delivery_status, delivery_error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._execution_params(execution),
            )
        return execution

    async def record_execution_completed(self, execution: ScheduledTaskExecution) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_task_executions SET completed_at=?, status=?, output=?, error=?
                WHERE id=? AND EXISTS(
                    SELECT 1 FROM scheduled_tasks
                    WHERE id=? AND claim_id=? AND lease_owner=?
                )
                """,
                (
                    _iso(execution.completed_at),
                    execution.status.value,
                    execution.output,
                    execution.error,
                    execution.id,
                    execution.task_id,
                    execution.claim_id,
                    execution.lease_owner,
                ),
            )
            if cursor.rowcount:
                conn.execute(
                    """
                    UPDATE scheduled_tasks SET last_run_at=?, last_status=?, last_error=?, lease_until=NULL, updated_at=?
                    WHERE id=? AND claim_id=? AND lease_owner=?
                    """,
                    (
                        _iso(execution.completed_at),
                        execution.status.value,
                        execution.error,
                        _iso(_now()),
                        execution.task_id,
                        execution.claim_id,
                        execution.lease_owner,
                    ),
                )
            return cursor.rowcount > 0

    async def record_delivery_result(self, execution: ScheduledTaskExecution) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_task_executions SET delivery_status=?, delivery_error=?
                WHERE id=? AND EXISTS(
                    SELECT 1 FROM scheduled_tasks
                    WHERE id=? AND claim_id=? AND lease_owner=?
                )
                """,
                (
                    execution.delivery_status,
                    execution.delivery_error,
                    execution.id,
                    execution.task_id,
                    execution.claim_id,
                    execution.lease_owner,
                ),
            )
            if cursor.rowcount:
                conn.execute(
                    """
                    UPDATE scheduled_tasks SET last_delivery_error=?, updated_at=?
                    WHERE id=? AND claim_id=? AND lease_owner=?
                    """,
                    (
                        execution.delivery_error,
                        _iso(_now()),
                        execution.task_id,
                        execution.claim_id,
                        execution.lease_owner,
                    ),
                )
            return cursor.rowcount > 0

    async def list_executions(self, task_id: str, limit: int) -> list[ScheduledTaskExecution]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_task_executions
                WHERE task_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
        return [self._row_to_execution(row) for row in rows]

    async def mark_dashboard_unread(self, task_id: str, claim_id: str, lease_owner: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks SET unread_count = unread_count + 1, updated_at=?
                WHERE id=? AND claim_id=? AND lease_owner=?
                """,
                (_iso(_now()), task_id, claim_id, lease_owner),
            )
            return cursor.rowcount > 0

    async def clear_dashboard_unread(self, task_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE scheduled_tasks SET unread_count = 0, updated_at=? WHERE id=?", (_iso(_now()), task_id))
            return cursor.rowcount > 0

    async def mark_session_missing(self, session_id: str) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE scheduled_tasks SET enabled=0, status=?, updated_at=?
                WHERE session_id=? AND status != ?
                """,
                (ScheduledTaskStatus.SESSION_MISSING.value, _iso(_now()), session_id, ScheduledTaskStatus.DELETED.value),
            )
            return cursor.rowcount

    async def list_recoverable_origin_tasks(self) -> list[ScheduledTask]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = ? AND delivery_target = ?
                ORDER BY next_run_at ASC
                """,
                (ScheduledTaskStatus.SESSION_MISSING.value, DeliveryTargetType.ORIGIN.value),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def _claim(
        self,
        task: ScheduledTask,
        now: datetime,
        lease_seconds: int,
        reason: str,
        next_run_at: datetime,
        skipped: bool,
    ) -> ScheduledTaskClaim:
        return ScheduledTaskClaim(
            task=task,
            claim_id=uuid4().hex,
            lease_owner=uuid4().hex,
            lease_until=now + timedelta(seconds=lease_seconds),
            claimed_next_run_at=task.next_run_at,
            next_run_at=next_run_at,
            reason=reason,
            skipped_missed=skipped,
        )

    def _task_params(self, task: ScheduledTask) -> tuple:
        now = _now()
        created_at = task.created_at or now
        updated_at = task.updated_at or now
        return (
            task.id,
            task.name,
            task.prompt,
            task.schedule.value,
            task.timezone.value,
            int(task.enabled),
            task.status.value,
            task.session_id,
            json.dumps(task.origin),
            task.delivery_target.target_type.value,
            json.dumps(task.delivery_target.context),
            json.dumps(
                {
                    "mode": task.execution_policy.mode.value,
                    "tool_exposure_policy": task.execution_policy.tool_exposure_policy,
                    "allow_confirm_tools": task.execution_policy.allow_confirm_tools,
                }
            ),
            _iso(task.next_run_at),
            _iso(task.lease_until),
            task.lease_owner,
            task.claim_id,
            _iso(task.last_run_at),
            task.last_status.value if task.last_status else None,
            task.last_error,
            task.last_delivery_error,
            task.unread_count,
            _iso(created_at),
            _iso(updated_at),
        )

    def _execution_params(self, execution: ScheduledTaskExecution) -> tuple:
        return (
            execution.id,
            execution.task_id,
            execution.session_id,
            execution.claim_id,
            execution.lease_owner,
            _iso(execution.claimed_next_run_at),
            _iso(execution.started_at),
            _iso(execution.completed_at),
            execution.status.value,
            execution.output,
            execution.error,
            execution.delivery_status,
            execution.delivery_error,
            _iso(execution.created_at or _now()),
        )

    def _row_to_execution(self, row: sqlite3.Row) -> ScheduledTaskExecution:
        return ScheduledTaskExecution(
            id=row["id"],
            task_id=row["task_id"],
            session_id=row["session_id"],
            claim_id=row["claim_id"],
            lease_owner=row["lease_owner"],
            status=ScheduledTaskExecutionStatus(row["status"]),
            claimed_next_run_at=_parse_optional(row["claimed_next_run_at"]),
            started_at=_parse_optional(row["started_at"]),
            completed_at=_parse_optional(row["completed_at"]),
            output=row["output"],
            error=row["error"],
            delivery_status=row["delivery_status"],
            delivery_error=row["delivery_error"],
            created_at=_parse_optional(row["created_at"]),
        )

    def _row_to_task(self, row: sqlite3.Row) -> ScheduledTask:
        policy_json = json.loads(row["execution_policy_json"] or "{}")
        return ScheduledTask(
            id=row["id"],
            name=row["name"],
            prompt=row["prompt"],
            schedule=ScheduleExpression(row["cron_expression"]),
            timezone=ScheduleTimezone(row["timezone"]),
            enabled=bool(row["enabled"]),
            status=ScheduledTaskStatus(row["status"]),
            session_id=row["session_id"],
            origin=json.loads(row["origin_json"] or "{}"),
            delivery_target=DeliveryTarget(DeliveryTargetType(row["delivery_target"]), json.loads(row["delivery_context_json"] or "{}")),
            execution_policy=ScheduledExecutionPolicy(
                mode=ScheduledExecutionPolicyMode(policy_json.get("mode", ScheduledExecutionPolicyMode.UNATTENDED.value)),
                tool_exposure_policy=policy_json.get("tool_exposure_policy", "safe_only"),
                allow_confirm_tools=bool(policy_json.get("allow_confirm_tools", False)),
            ),
            next_run_at=_parse(row["next_run_at"]),
            lease_until=_parse_optional(row["lease_until"]),
            lease_owner=row["lease_owner"],
            claim_id=row["claim_id"],
            last_run_at=_parse_optional(row["last_run_at"]),
            last_status=ScheduledTaskExecutionStatus(row["last_status"]) if row["last_status"] else None,
            last_error=row["last_error"],
            last_delivery_error=row["last_delivery_error"],
            unread_count=row["unread_count"],
            created_at=_parse_optional(row["created_at"]),
            updated_at=_parse_optional(row["updated_at"]),
        )


def _initialize_schedule_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            cron_expression TEXT NOT NULL,
            timezone TEXT NOT NULL,
            enabled INTEGER NOT NULL,
            status TEXT NOT NULL,
            session_id TEXT NOT NULL,
            origin_json TEXT NOT NULL,
            delivery_target TEXT NOT NULL,
            delivery_context_json TEXT NOT NULL,
            execution_policy_json TEXT NOT NULL,
            next_run_at TEXT NOT NULL,
            lease_until TEXT,
            lease_owner TEXT,
            claim_id TEXT,
            last_run_at TEXT,
            last_status TEXT,
            last_error TEXT,
            last_delivery_error TEXT,
            unread_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scheduled_task_executions (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            lease_owner TEXT NOT NULL,
            claimed_next_run_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL,
            output TEXT,
            error TEXT,
            delivery_status TEXT,
            delivery_error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due ON scheduled_tasks(enabled, status, next_run_at);
        CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_session ON scheduled_tasks(session_id);
        CREATE INDEX IF NOT EXISTS idx_scheduled_executions_task_created ON scheduled_task_executions(task_id, created_at);
        """
    )
    _migrate_origin_source_type_to_platform(conn)


def _migrate_origin_source_type_to_platform(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, origin_json, delivery_context_json
        FROM scheduled_tasks
        WHERE origin_json LIKE '%"source_type"%'
           OR delivery_context_json LIKE '%"source_type"%'
        """
    ).fetchall()
    if not rows:
        return
    affected = 0
    for row in rows:
        origin, origin_changed = _normalize_origin_platform(row["origin_json"])
        delivery_context, delivery_changed = _normalize_origin_platform(row["delivery_context_json"])
        if not origin_changed and not delivery_changed:
            continue
        conn.execute(
            "UPDATE scheduled_tasks SET origin_json = ?, delivery_context_json = ? WHERE id = ?",
            (json.dumps(origin), json.dumps(delivery_context), row["id"]),
        )
        affected += 1
    if affected:
        logger.info("scheduled_tasks origin source_type→platform migrated rows=%d", affected)


def _normalize_origin_platform(raw_value: str) -> tuple[dict, bool]:
    try:
        value = json.loads(raw_value or "{}")
    except json.JSONDecodeError:
        return {}, False
    if not isinstance(value, dict) or "source_type" not in value:
        return value if isinstance(value, dict) else {}, False
    value["platform"] = value.pop("source_type")
    return value, True


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _aware_utc(value).isoformat() if value else None


def _parse(value: str) -> datetime:
    return _aware_utc(datetime.fromisoformat(value))


def _parse_optional(value: str | None) -> datetime | None:
    return _parse(value) if value else None
