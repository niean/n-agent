"""同一评估账本中的受租约保护的执行队列；不拥有 PRAGMA user_version。"""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from app.domain.evaluation.errors import EvaluationConflictError, EvaluationLeaseError, EvaluationNotFoundError
from app.domain.evaluation.execution import ExecutionCapacityError, LEASE_SECONDS, MAX_QUEUED, TERMINAL_PHASES, validate_transition
from app.domain.evaluation.models import EvaluationValidationError
from app.infrastructure.sqlite_support import apply_journal_mode, open_sqlite


def _iso(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _row(row):
    if row is None:
        return None
    value = dict(row)
    value["payload"] = json.loads(value.pop("frozen_request_json"))
    value["report"] = json.loads(value.pop("report_json"))
    return value


class SQLiteExecutionStore:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""CREATE TABLE IF NOT EXISTS evaluation_executions (
                execution_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
                request_fingerprint TEXT NOT NULL, name TEXT NOT NULL,
                dataset_id TEXT NOT NULL, dataset_version TEXT NOT NULL, dataset_hash TEXT NOT NULL,
                frozen_request_json TEXT NOT NULL, phase TEXT NOT NULL,
                experiment_id TEXT, owner_token TEXT, lease_until REAL,
                report_json TEXT NOT NULL DEFAULT '{}', error_code TEXT,
                started_at REAL, finished_at REAL, scoring_started_at REAL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX IF NOT EXISTS execution_phase_lease ON evaluation_executions(phase,lease_until)")
            conn.execute("CREATE INDEX IF NOT EXISTS execution_page ON evaluation_executions(created_at DESC,execution_id DESC)")
            conn.execute("CREATE TABLE IF NOT EXISTS evaluation_schema_migrations(version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO evaluation_schema_migrations VALUES(1002,'dashboard-execution',?)", (_iso(0),))
            # Idempotent data repair, not a schema version change. Old schema 2
            # reports cached answers outside the source deletion/TTL boundary.
            conn.execute("""UPDATE evaluation_executions
                SET report_json = json_set(report_json, '$.items', json((
                    SELECT json_group_array(json_remove(value, '$.answer', '$.answer_truncated'))
                    FROM json_each(report_json, '$.items')
                )))
                WHERE json_extract(frozen_request_json, '$.manifest.schema_version') = '2'
                AND EXISTS (SELECT 1 FROM json_each(report_json, '$.items')
                    WHERE json_type(value, '$.answer') IS NOT NULL)
            """)

    def _connect(self):
        conn = open_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        apply_journal_mode(conn)
        return conn

    async def submit(self, request, *, now):
        return await asyncio.to_thread(self._submit, request, now)

    def _submit(self, request, now):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM evaluation_executions WHERE request_key=?", (request["request_key"],)).fetchone()
            if previous:
                if previous["request_fingerprint"] != request["request_fingerprint"]:
                    raise EvaluationConflictError("request_key payload conflict")
                return _row(previous)
            count = conn.execute("SELECT COUNT(*) FROM evaluation_executions WHERE phase='queued'").fetchone()[0]
            if count >= MAX_QUEUED:
                raise ExecutionCapacityError("execution queue full")
            execution_id = uuid.uuid4().hex
            conn.execute("""INSERT INTO evaluation_executions(
                execution_id,request_key,request_fingerprint,name,dataset_id,dataset_version,dataset_hash,
                frozen_request_json,phase,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (execution_id, request["request_key"], request["request_fingerprint"], request["name"],
                 request["dataset_id"], request["dataset_version"], request["dataset_hash"],
                 json.dumps(request["payload"], ensure_ascii=False), "queued", _iso(now), _iso(now)))
            return _row(conn.execute("SELECT * FROM evaluation_executions WHERE execution_id=?", (execution_id,)).fetchone())

    async def find_request(self, request_key):
        return await asyncio.to_thread(self._find_request, request_key)

    def _find_request(self, request_key):
        with self._connect() as conn:
            return _row(conn.execute("SELECT * FROM evaluation_executions WHERE request_key=?", (request_key,)).fetchone())

    async def get(self, execution_id):
        return await asyncio.to_thread(self._get, execution_id)

    def _get(self, execution_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM evaluation_executions WHERE execution_id=?", (execution_id,)).fetchone()
            if not row:
                raise EvaluationNotFoundError("execution not found")
            return _row(row)

    async def claim(self, *, now):
        return await asyncio.to_thread(self._claim, now)

    def _claim(self, now):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE evaluation_executions SET phase='interrupted',error_code='execution_lease_expired',
                owner_token=NULL,lease_until=NULL,finished_at=?,updated_at=?
                WHERE phase='running' AND lease_until<=?""", (now, _iso(now), now))
            active = conn.execute("SELECT 1 FROM evaluation_executions WHERE phase IN ('running','scoring') AND lease_until>? LIMIT 1", (now,)).fetchone()
            if active:
                return None
            row = conn.execute("""SELECT * FROM evaluation_executions WHERE phase='queued'
                OR (phase='scoring' AND (lease_until IS NULL OR lease_until<=?))
                ORDER BY CASE phase WHEN 'scoring' THEN 0 ELSE 1 END,created_at,execution_id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            phase = "running" if row["phase"] == "queued" else "scoring"
            validate_transition(row["phase"], phase)
            conn.execute("""UPDATE evaluation_executions SET phase=?,owner_token=?,lease_until=?,
                started_at=COALESCE(started_at,?),updated_at=? WHERE execution_id=?""",
                (phase, uuid.uuid4().hex, now+LEASE_SECONDS, now, _iso(now), row["execution_id"]))
            return _row(conn.execute("SELECT * FROM evaluation_executions WHERE execution_id=?", (row["execution_id"],)).fetchone())

    async def heartbeat(self, execution_id, token, *, now):
        await self.update(execution_id, token, now=now)

    async def update(self, execution_id, token, *, now, **changes):
        return await asyncio.to_thread(self._update, execution_id, token, now, changes)

    def _update(self, execution_id, token, now, changes):
        if set(changes) - {"phase", "experiment_id", "report", "error_code"}:
            raise EvaluationValidationError("unsupported execution update")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM evaluation_executions WHERE execution_id=? AND owner_token=? AND lease_until>? AND phase IN ('running','scoring')", (execution_id, token, now)).fetchone()
            if row is None:
                raise EvaluationLeaseError("execution lease lost")
            phase = changes.get("phase", row["phase"])
            validate_transition(row["phase"], phase)
            values = {"phase": phase, "lease_until": now+LEASE_SECONDS, "updated_at": _iso(now)}
            for key, value in changes.items():
                values["report_json" if key == "report" else key] = json.dumps(value, ensure_ascii=False) if key == "report" else value
            if phase == "scoring" and row["scoring_started_at"] is None:
                values["scoring_started_at"] = now
            if phase in TERMINAL_PHASES:
                values.update(finished_at=now, lease_until=None, owner_token=None)
            columns = ",".join(key+"=?" for key in values)
            conn.execute("UPDATE evaluation_executions SET "+columns+" WHERE execution_id=?", (*values.values(), execution_id))
            return _row(conn.execute("SELECT * FROM evaluation_executions WHERE execution_id=?", (execution_id,)).fetchone())

    async def release_scoring(self, execution_id, token, *, now):
        await asyncio.to_thread(self._release, execution_id, token, now)

    def _release(self, execution_id, token, now):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = conn.execute("UPDATE evaluation_executions SET owner_token=NULL,lease_until=NULL,updated_at=? WHERE execution_id=? AND owner_token=? AND lease_until>? AND phase='scoring'", (_iso(now), execution_id, token, now))
            if not result.rowcount:
                raise EvaluationLeaseError("execution lease lost")

    async def list_page(self, *, limit=50, cursor=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise EvaluationValidationError("limit must be 1..100")
        boundary = None
        if cursor:
            try:
                boundary = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                if not isinstance(boundary, list) or len(boundary) != 2 or not all(isinstance(x, str) and x for x in boundary):
                    raise ValueError()
            except Exception:
                raise EvaluationValidationError("cursor_invalid") from None
        return await asyncio.to_thread(self._list, limit, boundary)

    def _list(self, limit, boundary):
        with self._connect() as conn:
            where = "WHERE (created_at,execution_id)<(?,?)" if boundary else ""
            rows = conn.execute("SELECT * FROM evaluation_executions "+where+" ORDER BY created_at DESC,execution_id DESC LIMIT ?", (*boundary, limit+1) if boundary else (limit+1,)).fetchall()
            items = [_row(row) for row in rows[:limit]]
            cursor = None
            if len(rows) > limit:
                last = rows[limit-1]
                cursor = base64.urlsafe_b64encode(json.dumps([last["created_at"], last["execution_id"]]).encode()).decode()
            return {"items": items, "next_cursor": cursor}
