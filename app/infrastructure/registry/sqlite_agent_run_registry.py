"""Durable execution ledger. Business scheduling/claims remain their own authority."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from app.domain.agent_run import (
    RunBinding, RunOwnershipLost, RunAdmissionConflict, RunPredecessor,
    RunRetryFacts, current_run,
)
from app.infrastructure.sqlite_support import open_sqlite


_ACTIVE = ("pending", "running")
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_BUSINESS_TABLES = {"agent_run": "agent_runs", "task_run": "task_runs", "scheduled_task_execution": "scheduled_task_executions"}
_TOOL_CALL_COUNT_BATCH = 500


def validate_run_session(conn: sqlite3.Connection, run_id: str | None, session_id: str) -> None:
    """Call inside the write transaction; never accept conflicting attribution."""
    if run_id is None:
        return
    row = conn.execute("SELECT session_id,owner_token,status,lease_expires_at FROM agent_runs WHERE id=?", (run_id,)).fetchone()
    if row is None or row[0] != session_id:
        raise ValueError("agent_run_session_mismatch")
    binding = current_run.get()
    if binding is not None and binding.run_id == run_id:
        if row[1] != binding.owner_token or row[2] not in _ACTIVE or row[3] <= time.time():
            raise RunOwnershipLost("agent_run_ownership_lost")


class SQLiteAgentRunRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    execution_kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed','cancelled','interrupted','not_started')),
                    parent_run_id TEXT,
                    retry_of_run_id TEXT,
                    task_run_id INTEGER,
                    schedule_execution_id TEXT,
                    delegation_member_id TEXT,
                    business_owner_deleted_at REAL,
                    foreground INTEGER NOT NULL DEFAULT 0,
                    process_id TEXT NOT NULL,
                    owner_token TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    started_at REAL,
                    ended_at REAL,
                    heartbeat_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    interrupted_at REAL,
                    end_reason TEXT,
                    error TEXT,
                    CHECK((task_run_id IS NOT NULL)+(schedule_execution_id IS NOT NULL)+(delegation_member_id IS NOT NULL)<=1)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_session ON agent_runs(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_lease ON agent_runs(status,lease_expires_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_runs_foreground
                    ON agent_runs(session_id) WHERE foreground=1 AND status IN ('pending','running');
                CREATE TABLE IF NOT EXISTS trace_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    biz_type TEXT NOT NULL CHECK(biz_type IN ('agent_run','task_run','scheduled_task_execution')),
                    biz_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(biz_type,biz_id,trace_id)
                );
                CREATE INDEX IF NOT EXISTS idx_trace_links_trace ON trace_links(trace_id);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_runs)")}
            if "business_owner_deleted_at" not in columns:
                conn.execute("ALTER TABLE agent_runs ADD COLUMN business_owner_deleted_at REAL")

    def connect(self) -> sqlite3.Connection:
        conn = open_sqlite(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def register(self, *, run_id: str, session_id: str, source: str, execution_kind: str,
                 process_id: str, owner_token: str, lease_seconds: float,
                 parent_run_id: str | None = None, retry_of_run_id: str | None = None,
                 task_run_id: int | None = None, schedule_execution_id: str | None = None,
                 delegation_member_id: str | None = None, foreground: bool = False) -> dict:
        if not all((run_id, session_id, source, execution_kind, process_id, owner_token)):
            raise ValueError("agent_run_identity_required")
        if sum(value is not None for value in (task_run_id, schedule_execution_id, delegation_member_id)) > 1:
            raise ValueError("agent_run_business_owner_conflict")
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise ValueError("agent_run_session_missing")
            for reference in (parent_run_id, retry_of_run_id):
                if reference is not None and conn.execute("SELECT 1 FROM agent_runs WHERE id=?", (reference,)).fetchone() is None:
                    raise ValueError("agent_run_reference_missing")
            business_refs = (("task_runs", task_run_id), ("scheduled_task_executions", schedule_execution_id), ("delegation_members", delegation_member_id))
            for table, reference in business_refs:
                if reference is None:
                    continue
                if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (reference,)).fetchone() is None:
                    raise ValueError("agent_run_business_reference_missing")
            if schedule_execution_id is not None:
                execution = conn.execute("SELECT session_id FROM scheduled_task_executions WHERE id=?", (schedule_execution_id,)).fetchone()
                if execution[0] != session_id:
                    raise ValueError("agent_run_business_session_mismatch")
            if delegation_member_id is not None:
                member = conn.execute(
                    "SELECT execution_session_id FROM delegation_members WHERE id=?",
                    (delegation_member_id,),
                ).fetchone()
                expected_session_id = member[0] or f"delegation-{delegation_member_id}"
                if expected_session_id != session_id:
                    raise ValueError("agent_run_business_session_mismatch")
            if foreground and conn.execute("SELECT 1 FROM agent_runs WHERE session_id=? AND foreground=1 AND status IN ('pending','running')", (session_id,)).fetchone():
                raise RunAdmissionConflict("agent_run_foreground_busy")
            conn.execute("""INSERT INTO agent_runs
                (id,session_id,source,execution_kind,status,parent_run_id,retry_of_run_id,task_run_id,schedule_execution_id,delegation_member_id,foreground,process_id,owner_token,created_at,heartbeat_at,lease_expires_at)
                VALUES (?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, session_id, source, execution_kind, parent_run_id, retry_of_run_id, task_run_id,
                 schedule_execution_id, delegation_member_id, int(foreground), process_id, owner_token, now, now, now + lease_seconds))
        return self.get(run_id)

    def retry_facts(self, task_run_id: int | None,
                    delegation_member_id: str | None) -> RunRetryFacts:
        """Read ordered history; execution/retry semantics belong to policy.

        Equal timestamps use SQLite insertion order, never random Run IDs.
        """
        with self.connect() as conn:
            def predecessors(rows) -> tuple[RunPredecessor, ...]:
                return tuple(RunPredecessor(row["id"], row["execution_kind"], row["task_run_id"])
                             for row in rows)

            current = previous = members = ()
            if task_run_id is not None:
                current = predecessors(conn.execute(
                    "SELECT id,execution_kind,task_run_id FROM agent_runs WHERE task_run_id=? ORDER BY created_at DESC,rowid DESC",
                    (task_run_id,),
                ))
                previous = predecessors(conn.execute("""SELECT previous.id,previous.execution_kind,previous.task_run_id
                    FROM agent_runs previous
                    JOIN task_runs old_attempt ON old_attempt.id=previous.task_run_id
                    JOIN task_runs new_attempt ON new_attempt.task_id=old_attempt.task_id
                    WHERE new_attempt.id=? AND old_attempt.id<new_attempt.id
                    ORDER BY old_attempt.id DESC,previous.created_at DESC,previous.rowid DESC""",
                    (task_run_id,),
                ))
            if delegation_member_id is not None:
                members = predecessors(conn.execute(
                    "SELECT id,execution_kind,task_run_id FROM agent_runs WHERE delegation_member_id=? ORDER BY created_at DESC,rowid DESC",
                    (delegation_member_id,),
                ))
            return RunRetryFacts(current, previous, members)

    @staticmethod
    def _condition(binding: RunBinding, now: float) -> tuple:
        return binding.run_id, binding.owner_token, binding.process_id, now

    def activate(self, binding: RunBinding, lease_seconds: float) -> None:
        now = time.time()
        with self.connect() as conn:
            updated = conn.execute("""UPDATE agent_runs SET status='running',started_at=?,heartbeat_at=?,lease_expires_at=?
                WHERE id=? AND owner_token=? AND process_id=? AND lease_expires_at>? AND status='pending'""",
                (now, now, now + lease_seconds, *self._condition(binding, now))).rowcount
            if not updated:
                raise RunOwnershipLost("agent_run_cannot_start")

    def check(self, binding: RunBinding) -> None:
        with self.connect() as conn:
            row = conn.execute("""SELECT 1 FROM agent_runs WHERE id=? AND owner_token=? AND process_id=?
                AND lease_expires_at>? AND status IN ('pending','running')""", self._condition(binding, time.time())).fetchone()
            if row is None:
                raise RunOwnershipLost("agent_run_ownership_lost")

    def heartbeat(self, binding: RunBinding, lease_seconds: float) -> None:
        now = time.time()
        with self.connect() as conn:
            updated = conn.execute("""UPDATE agent_runs SET heartbeat_at=?,lease_expires_at=?
                WHERE id=? AND owner_token=? AND process_id=? AND lease_expires_at>? AND status IN ('pending','running')""",
                (now, now + lease_seconds, *self._condition(binding, now))).rowcount
            if not updated:
                raise RunOwnershipLost("agent_run_ownership_lost")

    def finish(self, binding: RunBinding, status: str, end_reason: str, error: str | None = None) -> None:
        if status not in _TERMINAL:
            raise ValueError("agent_run_invalid_terminal_status")
        now = time.time()
        with self.connect() as conn:
            updated = conn.execute("""UPDATE agent_runs SET status=?,end_reason=?,error=?,ended_at=?
                WHERE id=? AND owner_token=? AND process_id=? AND lease_expires_at>? AND status IN ('pending','running')""",
                (status, end_reason, error, now, *self._condition(binding, now))).rowcount
            if not updated:
                row = conn.execute("SELECT status,owner_token,process_id FROM agent_runs WHERE id=?", (binding.run_id,)).fetchone()
                if row is None or tuple(row) != (status, binding.owner_token, binding.process_id):
                    raise RunOwnershipLost("agent_run_terminal_write_rejected")

    def reconcile_expired(self) -> int:
        now = time.time()
        with self.connect() as conn:
            return conn.execute("""UPDATE agent_runs SET
                status=CASE status WHEN 'pending' THEN 'not_started' ELSE 'interrupted' END,
                interrupted_at=?,end_reason='owner_lost'
                WHERE status IN ('pending','running') AND lease_expires_at<=?""", (now, now)).rowcount

    def abandon_pending(self, binding: RunBinding) -> None:
        with self.connect() as conn:
            conn.execute("""UPDATE agent_runs SET status='not_started',interrupted_at=?,end_reason='shutdown_before_start'
                WHERE id=? AND owner_token=? AND process_id=? AND status='pending'""",
                (time.time(), binding.run_id, binding.owner_token, binding.process_id))

    def _tool_call_counts(self, conn: sqlite3.Connection, session_id: str, run_ids: list[str]) -> dict[str, int]:
        """Count persisted tool calls per run within one session; batches parameters."""
        if not run_ids:
            return {}
        # tool_calls 由 memory store 建表；裸 registry（无该表）兼容降级为全 0。
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tool_calls'").fetchone() is None:
            return {}
        counts: dict[str, int] = {}
        for start in range(0, len(run_ids), _TOOL_CALL_COUNT_BATCH):
            chunk = run_ids[start:start + _TOOL_CALL_COUNT_BATCH]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT run_id, COUNT(*) FROM tool_calls WHERE session_id=? AND run_id IN ({placeholders}) GROUP BY run_id",
                (session_id, *chunk),
            )
            counts.update({row[0]: row[1] for row in rows})
        return counts

    def _project_with_tool_call_counts(self, conn: sqlite3.Connection, session_id: str, rows: list[dict]) -> list[dict]:
        """Shared read projection for get/list: attach per-run tool call counts."""
        counts = self._tool_call_counts(conn, session_id, [row["id"] for row in rows])
        for row in rows:
            row["tool_call_count"] = counts.get(row["id"], 0)
        return rows

    def get(self, run_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM agent_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return None
            return self._project_with_tool_call_counts(conn, row["session_id"], [dict(row)])[0]

    def list_for_session(self, session_id: str) -> list[dict]:
        with self.connect() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM agent_runs WHERE session_id=? ORDER BY created_at,id", (session_id,))]
            return self._project_with_tool_call_counts(conn, session_id, rows)

    def add_trace_link(self, biz_type: str, biz_id: str, trace_id: str) -> None:
        if biz_type not in _BUSINESS_TABLES or not re.fullmatch(r"[0-9a-fA-F]{32}", trace_id) or int(trace_id, 16) == 0:
            raise ValueError("trace_link_invalid_identity")
        table = _BUSINESS_TABLES[biz_type]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (biz_id,)).fetchone() is None:
                raise ValueError("trace_link_business_missing")
            conn.execute("INSERT OR IGNORE INTO trace_links(biz_type,biz_id,trace_id,created_at) VALUES (?,?,?,?)", (biz_type, str(biz_id), trace_id.lower(), time.time()))

    def trace_links(self, biz_type: str, biz_id: str) -> list[dict]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM trace_links WHERE biz_type=? AND biz_id=?", (biz_type, str(biz_id)))]

    def find_by_trace(self, trace_id: str) -> list[dict]:
        """Return only associations whose business identity still exists."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM trace_links WHERE trace_id=? ORDER BY id", (trace_id.lower(),)).fetchall()
            result = []
            for row in rows:
                table = _BUSINESS_TABLES[row["biz_type"]]
                if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (row["biz_id"],)).fetchone() is not None:
                    result.append(dict(row))
            return result
