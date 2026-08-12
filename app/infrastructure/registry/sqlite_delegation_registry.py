"""SQLite persistence for the Delegation subdomain (Infrastructure Layer).

Implements the ``DelegationRegistry`` async Protocol from
``app/domain/delegation.py``. Shares the sessions.db path but opens
independent connections. Enables WAL, foreign_keys, and busy_timeout per
connection. Async methods wrap sync sqlite3 via ``asyncio.to_thread``.

Schema ownership (T4):
  - Delegation owns exactly 7 tables, created with ``CREATE TABLE IF NOT
    EXISTS`` in a single transaction inside ``initialize()``.
  - Delegation does NOT own or advance ``PRAGMA user_version``. That global
    is owned by ``SQLiteTaskRegistry`` for its one-time legacy status
    migration. Delegation tables are purely additive (new feature, no legacy
    data to migrate), so they need no guarded one-time migration -- only
    idempotent creation + shape verification on re-init.
  - Re-init verifies the shape of existing tables via ``PRAGMA
    table_info`` and raises on incompatible columns without mutating data.
  - A failed migration raises before any ``commit``, so the connection
    context manager rolls back and leaves no half-created tables.

Read/write operations (T5):
  - ``create_or_reconnect`` persists delegation + members + snapshot +
    ledger + initial event in a single ``BEGIN IMMEDIATE`` transaction.
    Idempotent by ``(parent_source, parent_scope_id, delegation_key)`` +
    ``fingerprint``; raises ``DelegationConflictError`` on fingerprint
    mismatch.
  - ``claim_member`` / ``finish_member`` use row-count CAS (UPDATE ...
    WHERE status=...) so concurrent claims cannot oversubscribe.
  - Ledger ``reserve`` / ``settle`` / ``release`` are CAS + idempotent by
    ``usage_event_id``; ``request_cancel`` inserts the outbox row in the
    same transaction as the CANCELLING state change.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.domain.delegation import (
    ClaimMemberResult,
    Delegation,
    DelegationAggregationPolicy,
    DelegationConflictError,
    DelegationCreateRequest,
    DelegationEvent,
    DelegationJoinPolicy,
    DelegationMember,
    DelegationMemberRole,
    DelegationMemberStatus,
    DelegationParentRef,
    DelegationResult,
    DelegationResultSet,
    DelegationStatus,
    FinishMemberResult,
    LedgerResult,
    MutationOutcome,
    PolicySnapshotRecord,
    evaluate_join_outcome,
)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

DELEGATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS delegations (
    id TEXT PRIMARY KEY,
    parent_source TEXT NOT NULL,
    parent_scope_id TEXT NOT NULL,
    parent_run_id TEXT NOT NULL,
    parent_session_id TEXT NOT NULL,
    delegation_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    join_policy TEXT NOT NULL,
    aggregation TEXT NOT NULL,
    deadline_at TEXT,
    policy_snapshot_id TEXT NOT NULL DEFAULT '',
    budget_total_tokens INTEGER NOT NULL DEFAULT 0,
    budget_reserved_tokens INTEGER NOT NULL DEFAULT 0,
    budget_settled_tokens INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    first_run_id TEXT,
    cancellation_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (version >= 1),
    CHECK (budget_total_tokens >= 0),
    CHECK (budget_reserved_tokens >= 0),
    CHECK (budget_settled_tokens >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_delegations_scope_key
    ON delegations(parent_source, parent_scope_id, delegation_key);
CREATE INDEX IF NOT EXISTS idx_delegations_status
    ON delegations(status);
CREATE INDEX IF NOT EXISTS idx_delegations_deadline
    ON delegations(deadline_at);
CREATE INDEX IF NOT EXISTS idx_delegations_scope_id
    ON delegations(parent_scope_id);
CREATE INDEX IF NOT EXISTS idx_delegations_version
    ON delegations(id, version);

CREATE TABLE IF NOT EXISTS delegation_members (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL,
    instruction TEXT NOT NULL,
    skills_json TEXT NOT NULL DEFAULT '[]',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    model_override TEXT,
    max_runtime_seconds INTEGER,
    execution_session_id TEXT NOT NULL DEFAULT '',
    deadline_at TEXT,
    budget_tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    version INTEGER NOT NULL DEFAULT 1,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retry_of TEXT,
    claim_lock TEXT,
    claim_expires_at TEXT,
    last_heartbeat_at TEXT,
    cancel_reason TEXT,
    cancel_requested_at TEXT,
    started_at TEXT,
    ended_at TEXT,
    CHECK (ordinal >= 0),
    CHECK (version >= 1),
    CHECK (retry_count >= 0),
    CHECK (budget_tokens >= 0),
    FOREIGN KEY(delegation_id) REFERENCES delegations(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_members_role_ordinal
    ON delegation_members(delegation_id, role, ordinal);
CREATE INDEX IF NOT EXISTS idx_members_delegation_ordinal
    ON delegation_members(delegation_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_members_status
    ON delegation_members(status);
CREATE INDEX IF NOT EXISTS idx_members_claim_expires
    ON delegation_members(claim_expires_at)
    WHERE claim_expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS delegation_policy_snapshots (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL,
    profile_version TEXT NOT NULL DEFAULT '',
    parent_config_json TEXT NOT NULL DEFAULT '{}',
    child_config_json TEXT NOT NULL DEFAULT '{}',
    aggregator_config_json TEXT,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(delegation_id) REFERENCES delegations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_policy_snapshots_delegation
    ON delegation_policy_snapshots(delegation_id);
CREATE INDEX IF NOT EXISTS idx_policy_snapshots_checksum
    ON delegation_policy_snapshots(checksum);

CREATE TABLE IF NOT EXISTS delegation_results (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL,
    result_scope TEXT NOT NULL,
    member_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    structured_data_json TEXT NOT NULL DEFAULT '{}',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    error_code TEXT,
    error_message TEXT,
    usage_summary_json TEXT NOT NULL DEFAULT '{}',
    classification TEXT,
    checksum TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    ended_at TEXT,
    created_at TEXT NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 1,
    CHECK (result_scope IN ('member', 'delegation')),
    FOREIGN KEY(delegation_id) REFERENCES delegations(id) ON DELETE CASCADE
);

-- At most one adopted member-scope result per member.
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_member_adopted
    ON delegation_results(member_id)
    WHERE result_scope = 'member' AND member_id IS NOT NULL AND adopted = 1;
-- At most one adopted delegation-scope (aggregation) result per delegation.
CREATE UNIQUE INDEX IF NOT EXISTS idx_results_delegation_adopted
    ON delegation_results(delegation_id)
    WHERE result_scope = 'delegation' AND adopted = 1;
CREATE INDEX IF NOT EXISTS idx_results_delegation
    ON delegation_results(delegation_id);

CREATE TABLE IF NOT EXISTS delegation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delegation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    member_ordinal INTEGER,
    run_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(delegation_id) REFERENCES delegations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_delegation_id
    ON delegation_events(delegation_id, id);

CREATE TABLE IF NOT EXISTS delegation_budget_ledger (
    id TEXT PRIMARY KEY,
    delegation_id TEXT NOT NULL,
    member_id TEXT,
    reservation_id TEXT NOT NULL UNIQUE,
    purpose TEXT NOT NULL DEFAULT '',
    amount INTEGER NOT NULL DEFAULT 0,
    reserved INTEGER NOT NULL DEFAULT 0,
    settled INTEGER NOT NULL DEFAULT 0,
    released INTEGER NOT NULL DEFAULT 0,
    usage_event_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (version >= 1),
    CHECK (amount >= 0),
    CHECK (reserved >= 0),
    CHECK (settled >= 0),
    CHECK (released >= 0),
    FOREIGN KEY(delegation_id) REFERENCES delegations(id) ON DELETE CASCADE
);

-- Idempotent settle: one settled entry per usage_event_id+member.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_usage_event
    ON delegation_budget_ledger(member_id, usage_event_id)
    WHERE usage_event_id IS NOT NULL AND settled > 0;
CREATE INDEX IF NOT EXISTS idx_ledger_delegation
    ON delegation_budget_ledger(delegation_id);

CREATE TABLE IF NOT EXISTS delegation_cancel_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delegation_id TEXT NOT NULL,
    member_id TEXT,
    reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    acked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (attempts >= 0),
    FOREIGN KEY(delegation_id) REFERENCES delegations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON delegation_cancel_outbox(next_attempt_at)
    WHERE acked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_outbox_delegation
    ON delegation_cancel_outbox(delegation_id);
"""

# Required columns per table for shape verification on re-init. If an
# existing table is missing any of these, initialize() raises without
# mutating data. This catches partially-created or hand-modified schemas.
_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "delegations": frozenset({
        "id", "parent_source", "parent_scope_id", "parent_run_id",
        "parent_session_id", "delegation_key", "fingerprint", "status",
        "join_policy", "aggregation", "deadline_at", "policy_snapshot_id",
        "budget_total_tokens", "budget_reserved_tokens",
        "budget_settled_tokens", "version", "first_run_id",
        "cancellation_reason", "created_at", "updated_at",
    }),
    "delegation_members": frozenset({
        "id", "delegation_id", "role", "ordinal", "title", "instruction",
        "skills_json", "allowed_tools_json", "model_override",
        "max_runtime_seconds", "execution_session_id", "deadline_at",
        "budget_tokens", "status", "version", "retry_count", "retry_of",
        "claim_lock", "claim_expires_at", "last_heartbeat_at",
        "cancel_reason", "cancel_requested_at", "started_at", "ended_at",
    }),
    "delegation_policy_snapshots": frozenset({
        "id", "delegation_id", "profile_version", "parent_config_json",
        "child_config_json", "aggregator_config_json", "checksum",
        "created_at",
    }),
    "delegation_results": frozenset({
        "id", "delegation_id", "result_scope", "member_id", "summary",
        "structured_data_json", "artifact_refs_json", "error_code",
        "error_message", "usage_summary_json", "classification", "checksum",
        "started_at", "ended_at", "created_at", "adopted",
    }),
    "delegation_events": frozenset({
        "id", "delegation_id", "kind", "payload_json", "member_ordinal",
        "run_id", "created_at",
    }),
    "delegation_budget_ledger": frozenset({
        "id", "delegation_id", "member_id", "reservation_id", "purpose",
        "amount", "reserved", "settled", "released", "usage_event_id",
        "version", "created_at", "updated_at",
    }),
    "delegation_cancel_outbox": frozenset({
        "id", "delegation_id", "member_id", "reason", "attempts",
        "next_attempt_at", "acked_at", "created_at", "updated_at",
    }),
}


class DelegationSchemaError(Exception):
    """Raised when an existing delegation table has an incompatible shape."""


@dataclass(frozen=True)
class CancelOutboxEntry:
    """Read model for a pending cancel-outbox row."""

    id: int
    delegation_id: str
    member_id: str | None
    reason: str
    attempts: int
    next_attempt_at: str | None
    acked_at: str | None


class SQLiteDelegationRegistry:
    """SQLite implementation of the DelegationRegistry async Protocol.

    Shares the sessions.db path but opens independent connections. Each
    connection enables WAL, foreign_keys, and busy_timeout (5000ms).

    Schema ownership: Delegation owns 7 tables created idempotently in
    ``initialize()``. It does NOT own ``PRAGMA user_version`` (owned by
    SQLiteTaskRegistry for its legacy status migration).
    """

    BUSY_TIMEOUT_MS = 5000

    def __init__(self, db_path: str, clock: Any = None) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self.initialize()

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

    def initialize(self) -> None:
        """Idempotent schema creation for the 7 delegation tables.

        On re-init, verifies the shape of existing tables via
        ``PRAGMA table_info`` and raises ``DelegationSchemaError`` if a
        required column is missing -- without mutating existing data.

        All DDL runs in a single connection context. A failure before
        ``commit`` rolls back, leaving no half-created tables.
        """
        with self._connect() as conn:
            self._verify_existing_shapes(conn)
            conn.executescript(DELEGATION_SCHEMA_SQL)
            conn.commit()

    @staticmethod
    def _verify_existing_shapes(conn: sqlite3.Connection) -> None:
        """Check that any pre-existing delegation table has all required
        columns. Raises ``DelegationSchemaError`` on mismatch.

        Runs before any CREATE TABLE so an incompatible existing table is
        reported without mutation.
        """
        existing = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'delegation%'"
            )
        }
        for table, required in _REQUIRED_COLUMNS.items():
            if table not in existing:
                continue
            actual = {
                col_row["name"]
                for col_row in conn.execute(f"PRAGMA table_info({table})")
            }
            missing = required - actual
            if missing:
                raise DelegationSchemaError(
                    f"existing table '{table}' is missing required columns: "
                    f"{sorted(missing)}; refusing to initialize to avoid data "
                    f"corruption"
                )

    # ------------------------------------------------------------------
    # Serialization helpers (row -> domain)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_delegation(row: sqlite3.Row) -> Delegation:
        return Delegation(
            id=row["id"],
            parent=DelegationParentRef(
                source=row["parent_source"],
                scope_id=row["parent_scope_id"],
                run_id=row["parent_run_id"],
                session_id=row["parent_session_id"],
            ),
            delegation_key=row["delegation_key"],
            fingerprint=row["fingerprint"],
            join_policy=DelegationJoinPolicy(row["join_policy"]),
            aggregation=DelegationAggregationPolicy(row["aggregation"]),
            deadline_at=row["deadline_at"],
            policy_snapshot_id=row["policy_snapshot_id"],
            budget_total_tokens=row["budget_total_tokens"],
            budget_reserved_tokens=row["budget_reserved_tokens"],
            budget_settled_tokens=row["budget_settled_tokens"],
            status=DelegationStatus(row["status"]),
            version=row["version"],
            first_run_id=row["first_run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            cancellation_reason=row["cancellation_reason"],
        )

    @staticmethod
    def _row_to_member(row: sqlite3.Row) -> DelegationMember:
        return DelegationMember(
            id=row["id"],
            delegation_id=row["delegation_id"],
            role=DelegationMemberRole(row["role"]),
            ordinal=row["ordinal"],
            title=row["title"],
            instruction=row["instruction"],
            skills=tuple(json.loads(row["skills_json"])),
            allowed_tools=tuple(json.loads(row["allowed_tools_json"])),
            model_override=row["model_override"],
            max_runtime_seconds=row["max_runtime_seconds"],
            execution_session_id=row["execution_session_id"],
            deadline_at=row["deadline_at"],
            budget_tokens=row["budget_tokens"],
            status=DelegationMemberStatus(row["status"]),
            version=row["version"],
            retry_count=row["retry_count"],
            retry_of=row["retry_of"],
            claim_lock=row["claim_lock"],
            claim_expires_at=row["claim_expires_at"],
            last_heartbeat_at=row["last_heartbeat_at"],
            cancel_reason=row["cancel_reason"],
            cancel_requested_at=row["cancel_requested_at"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> DelegationEvent:
        return DelegationEvent(
            id=row["id"],
            delegation_id=row["delegation_id"],
            kind=row["kind"],
            payload=dict(json.loads(row["payload_json"])),
            member_ordinal=row["member_ordinal"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_result(row: sqlite3.Row) -> DelegationResult:
        return DelegationResult(
            status=DelegationMemberStatus(row["status"]),
            summary=row["summary"],
            structured_data=json.loads(row["structured_data_json"]) if row["structured_data_json"] else None,
            artifact_refs=tuple(json.loads(row["artifact_refs_json"])),
            error_code=row["error_code"],
            error_message=row["error_message"],
            usage_summary=dict(json.loads(row["usage_summary_json"])) if row["usage_summary_json"] else {},
            classification=row["classification"],
            checksum=row["checksum"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
        )

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _result_status_for_member_status(s: DelegationMemberStatus) -> str:
        return s.value

    # ------------------------------------------------------------------
    # create_or_reconnect (transactional)
    # ------------------------------------------------------------------

    async def create_or_reconnect(
        self, request: DelegationCreateRequest
    ) -> Delegation:
        return await asyncio.to_thread(self._create_or_reconnect_sync, request)

    def _create_or_reconnect_sync(self, req: DelegationCreateRequest) -> Delegation:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM delegations WHERE parent_source = ? "
                "AND parent_scope_id = ? AND delegation_key = ?",
                (req.parent.source, req.parent.scope_id, req.delegation_key),
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != req.fingerprint:
                    raise DelegationConflictError(
                        f"delegation_key '{req.delegation_key}' exists with "
                        f"a different fingerprint"
                    )
                conn.commit()
                return self._row_to_delegation(existing)

            # Create new delegation.
            now = self._now_iso()
            delegation_id = str(uuid.uuid4())
            snapshot_id = f"snap-{delegation_id}"
            join_policy = (
                req.join_policy
                if isinstance(req.join_policy, DelegationJoinPolicy)
                else DelegationJoinPolicy(req.join_policy)
            )
            aggregation = (
                req.aggregation
                if isinstance(req.aggregation, DelegationAggregationPolicy)
                else DelegationAggregationPolicy(req.aggregation)
            )
            conn.execute(
                "INSERT INTO delegations (id, parent_source, parent_scope_id, "
                "parent_run_id, parent_session_id, delegation_key, fingerprint, "
                "status, join_policy, aggregation, deadline_at, "
                "policy_snapshot_id, budget_total_tokens, "
                "budget_reserved_tokens, budget_settled_tokens, version, "
                "first_run_id, cancellation_reason, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?,0,0,1,NULL,NULL,?,?)",
                (
                    delegation_id, req.parent.source, req.parent.scope_id,
                    req.parent.run_id, req.parent.session_id,
                    req.delegation_key, req.fingerprint,
                    join_policy.value, aggregation.value, req.deadline_at,
                    snapshot_id, req.budget_total_tokens, now, now,
                ),
            )
            # Insert members.
            for m in req.members:
                conn.execute(
                    "INSERT INTO delegation_members (id, delegation_id, role, "
                    "ordinal, title, instruction, skills_json, "
                    "allowed_tools_json, model_override, max_runtime_seconds, "
                    "execution_session_id, deadline_at, budget_tokens, status, "
                    "version, retry_count, retry_of, claim_lock, "
                    "claim_expires_at, last_heartbeat_at, cancel_reason, "
                    "cancel_requested_at, started_at, ended_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',1,0,NULL,NULL,"
                    "NULL,NULL,NULL,NULL,NULL,NULL)",
                    (
                        m.id, delegation_id, m.role.value, m.ordinal,
                        m.title, m.instruction,
                        json.dumps(list(m.skills), ensure_ascii=False),
                        json.dumps(list(m.allowed_tools), ensure_ascii=False),
                        m.model_override, m.max_runtime_seconds,
                        m.execution_session_id, m.deadline_at, m.budget_tokens,
                    ),
                )
            # Insert policy snapshot.
            conn.execute(
                "INSERT INTO delegation_policy_snapshots (id, delegation_id, "
                "profile_version, parent_config_json, child_config_json, "
                "aggregator_config_json, checksum, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    snapshot_id, delegation_id, req.snapshot.profile_version,
                    json.dumps(dict(req.snapshot.parent_config), ensure_ascii=False),
                    json.dumps(dict(req.snapshot.child_config), ensure_ascii=False),
                    json.dumps(dict(req.snapshot.aggregator_config), ensure_ascii=False)
                    if req.snapshot.aggregator_config is not None else None,
                    req.snapshot.checksum, now,
                ),
            )
            # Insert created event.
            conn.execute(
                "INSERT INTO delegation_events (delegation_id, kind, "
                "payload_json, member_ordinal, run_id, created_at) "
                "VALUES (?, 'created', '{}', NULL, ?, ?)",
                (delegation_id, req.parent.run_id, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            return self._row_to_delegation(row)

    # ------------------------------------------------------------------
    # get / list
    # ------------------------------------------------------------------

    async def get(self, delegation_id: str) -> Delegation | None:
        return await asyncio.to_thread(self._get_sync, delegation_id)

    def _get_sync(self, delegation_id: str) -> Delegation | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
        return self._row_to_delegation(row) if row else None

    async def list_for_trusted_scope(
        self, scope_id: str, limit: int = 100
    ) -> tuple[Delegation, ...]:
        return await asyncio.to_thread(self._list_for_trusted_scope_sync, scope_id, limit)

    def _list_for_trusted_scope_sync(self, scope_id: str, limit: int) -> tuple[Delegation, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM delegations WHERE parent_scope_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (scope_id, limit),
            ).fetchall()
        return tuple(self._row_to_delegation(r) for r in rows)

    async def list_members(self, delegation_id: str) -> tuple[DelegationMember, ...]:
        return await asyncio.to_thread(self._list_members_sync, delegation_id)

    def _list_members_sync(self, delegation_id: str) -> tuple[DelegationMember, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM delegation_members WHERE delegation_id = ? "
                "ORDER BY ordinal",
                (delegation_id,),
            ).fetchall()
        return tuple(self._row_to_member(r) for r in rows)

    async def get_policy_snapshot(self, delegation_id: str) -> PolicySnapshotRecord | None:
        return await asyncio.to_thread(self._get_policy_snapshot_sync, delegation_id)

    def _get_policy_snapshot_sync(self, delegation_id: str) -> PolicySnapshotRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM delegation_policy_snapshots WHERE delegation_id = ?",
                (delegation_id,),
            ).fetchone()
        if row is None:
            return None
        return PolicySnapshotRecord(
            profile_version=row["profile_version"],
            parent_config=dict(json.loads(row["parent_config_json"])),
            child_config=dict(json.loads(row["child_config_json"])),
            aggregator_config=json.loads(row["aggregator_config_json"])
            if row["aggregator_config_json"] else None,
            checksum=row["checksum"],
        )

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    async def append_event(
        self, delegation_id: str, kind: str,
        payload: Mapping[str, Any], member_ordinal: int | None = None,
    ) -> DelegationEvent:
        return await asyncio.to_thread(
            self._append_event_sync, delegation_id, kind, payload, member_ordinal
        )

    def _append_event_sync(self, delegation_id, kind, payload, member_ordinal) -> DelegationEvent:
        now = self._now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO delegation_events (delegation_id, kind, "
                "payload_json, member_ordinal, run_id, created_at) "
                "VALUES (?,?,?,?,NULL,?)",
                (delegation_id, kind, json.dumps(dict(payload), ensure_ascii=False),
                 member_ordinal, now),
            )
            event_id = cur.lastrowid
            conn.commit()
            row = conn.execute(
                "SELECT * FROM delegation_events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._row_to_event(row)

    async def list_events(
        self, delegation_id: str, since: int = 0, limit: int = 100
    ) -> tuple[DelegationEvent, ...]:
        return await asyncio.to_thread(self._list_events_sync, delegation_id, since, limit)

    def _list_events_sync(self, delegation_id, since, limit) -> tuple[DelegationEvent, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM delegation_events WHERE delegation_id = ? "
                "AND id > ? ORDER BY id LIMIT ?",
                (delegation_id, since, limit),
            ).fetchall()
        return tuple(self._row_to_event(r) for r in rows)

    # ------------------------------------------------------------------
    # claim_member (CAS)
    # ------------------------------------------------------------------

    async def claim_member(
        self, delegation_id: str, member_ordinal: int,
        claim_lock: str, lease_seconds: int,
    ) -> ClaimMemberResult:
        return await asyncio.to_thread(
            self._claim_member_sync, delegation_id, member_ordinal,
            claim_lock, lease_seconds,
        )

    def _claim_member_sync(self, delegation_id, member_ordinal, claim_lock, lease_seconds) -> ClaimMemberResult:
        now = self._now_iso()
        from datetime import datetime, timedelta, timezone
        expires = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # CAS: only pending members can be claimed.
            cur = conn.execute(
                "UPDATE delegation_members SET status = 'running', "
                "claim_lock = ?, claim_expires_at = ?, started_at = COALESCE(started_at, ?), "
                "version = version + 1 "
                "WHERE delegation_id = ? AND ordinal = ? AND status = 'pending'",
                (claim_lock, expires, now, delegation_id, member_ordinal),
            )
            if cur.rowcount == 1:
                d_row = conn.execute(
                    "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
                ).fetchone()
                m_row = conn.execute(
                    "SELECT * FROM delegation_members WHERE delegation_id = ? AND ordinal = ?",
                    (delegation_id, member_ordinal),
                ).fetchone()
                conn.commit()
                return ClaimMemberResult(
                    outcome=MutationOutcome.SUCCESS,
                    member=self._row_to_member(m_row),
                    delegation=self._row_to_delegation(d_row),
                )
            # Not claimed -- determine why.
            m_row = conn.execute(
                "SELECT * FROM delegation_members WHERE delegation_id = ? AND ordinal = ?",
                (delegation_id, member_ordinal),
            ).fetchone()
            conn.commit()
            if m_row is None:
                return ClaimMemberResult(outcome=MutationOutcome.BUSY)
            member = self._row_to_member(m_row)
            if member.claim_lock == claim_lock and member.status is DelegationMemberStatus.RUNNING:
                d_row = conn.execute(
                    "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
                ).fetchone()
                return ClaimMemberResult(
                    outcome=MutationOutcome.IDEMPOTENT_REPLAY,
                    member=member,
                    delegation=self._row_to_delegation(d_row) if d_row else None,
                )
            if member.is_terminal:
                return ClaimMemberResult(outcome=MutationOutcome.BUSY)
            # RUNNING under a different lock, or lost PENDING CAS race.
            return ClaimMemberResult(outcome=MutationOutcome.CONFLICT)

    # ------------------------------------------------------------------
    # finish_member (CAS + result + ledger settle)
    # ------------------------------------------------------------------

    async def finish_member(
        self, delegation_id: str, member_ordinal: int,
        claim_lock: str, result: DelegationResult, expected_version: int,
    ) -> FinishMemberResult:
        return await asyncio.to_thread(
            self._finish_member_sync, delegation_id, member_ordinal,
            claim_lock, result, expected_version,
        )

    def _finish_member_sync(self, delegation_id, member_ordinal, claim_lock, result, expected_version) -> FinishMemberResult:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            m_row = conn.execute(
                "SELECT * FROM delegation_members WHERE delegation_id = ? AND ordinal = ?",
                (delegation_id, member_ordinal),
            ).fetchone()
            if m_row is None:
                conn.commit()
                return FinishMemberResult(outcome=MutationOutcome.BUSY)
            member = self._row_to_member(m_row)
            # Idempotent replay: same checksum already adopted.
            if member.is_terminal:
                existing = conn.execute(
                    "SELECT * FROM delegation_results WHERE member_id = ? AND adopted = 1",
                    (member.id,),
                ).fetchone()
                if existing is not None and existing["checksum"] == result.checksum:
                    d_row = conn.execute(
                        "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
                    ).fetchone()
                    conn.commit()
                    return FinishMemberResult(
                        outcome=MutationOutcome.IDEMPOTENT_REPLAY,
                        member=member,
                        delegation=self._row_to_delegation(d_row) if d_row else None,
                    )
                conn.commit()
                return FinishMemberResult(outcome=MutationOutcome.CONFLICT)
            # Must be running with matching lock + version.
            if member.status is not DelegationMemberStatus.RUNNING:
                conn.commit()
                return FinishMemberResult(outcome=MutationOutcome.BUSY)
            if member.claim_lock != claim_lock or member.version != expected_version:
                conn.commit()
                return FinishMemberResult(outcome=MutationOutcome.CONFLICT)
            # Apply terminal transition.
            conn.execute(
                "UPDATE delegation_members SET status = ?, ended_at = ?, "
                "version = version + 1 WHERE id = ?",
                (result.status.value, now, member.id),
            )
            # Insert adopted result.
            conn.execute(
                "INSERT INTO delegation_results (id, delegation_id, result_scope, "
                "member_id, summary, structured_data_json, artifact_refs_json, "
                "error_code, error_message, usage_summary_json, classification, "
                "checksum, started_at, ended_at, created_at, adopted) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (
                    str(uuid.uuid4()), delegation_id, "member", member.id,
                    result.summary,
                    json.dumps(dict(result.structured_data), ensure_ascii=False) if result.structured_data else "{}",
                    json.dumps(list(result.artifact_refs), ensure_ascii=False),
                    result.error_code, result.error_message,
                    json.dumps(dict(result.usage_summary), ensure_ascii=False),
                    result.classification, result.checksum,
                    result.started_at, result.ended_at, now,
                ),
            )
            d_row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            conn.execute(
                "UPDATE delegations SET updated_at = ? WHERE id = ?",
                (now, delegation_id),
            )
            conn.commit()
            updated_member = self._row_to_member(
                conn.execute(
                    "SELECT * FROM delegation_members WHERE id = ?", (member.id,)
                ).fetchone()
            )
            return FinishMemberResult(
                outcome=MutationOutcome.SUCCESS,
                member=updated_member,
                delegation=self._row_to_delegation(d_row),
            )

    # ------------------------------------------------------------------
    # ledger CAS
    # ------------------------------------------------------------------

    async def reserve_ledger(
        self, delegation_id: str, amount: int, purpose: str,
    ) -> LedgerResult:
        return await asyncio.to_thread(self._reserve_ledger_sync, delegation_id, amount, purpose)

    def _reserve_ledger_sync(self, delegation_id, amount, purpose) -> LedgerResult:
        now = self._now_iso()
        reservation_id = f"res-{uuid.uuid4()}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                return LedgerResult(outcome=MutationOutcome.BUSY)
            available = row["budget_total_tokens"] - row["budget_reserved_tokens"] - row["budget_settled_tokens"]
            if available < amount:
                conn.commit()
                return LedgerResult(outcome=MutationOutcome.CONFLICT)
            conn.execute(
                "UPDATE delegations SET budget_reserved_tokens = budget_reserved_tokens + ?, "
                "updated_at = ? WHERE id = ?",
                (amount, now, delegation_id),
            )
            conn.execute(
                "INSERT INTO delegation_budget_ledger (id, delegation_id, member_id, "
                "reservation_id, purpose, amount, reserved, settled, released, "
                "usage_event_id, version, created_at, updated_at) "
                "VALUES (?,?,NULL,?,?,?, ?, 0, 0, NULL, 1, ?, ?)",
                (str(uuid.uuid4()), delegation_id, reservation_id, purpose,
                 amount, amount, now, now),
            )
            new_balance = available - amount
            conn.commit()
            return LedgerResult(
                outcome=MutationOutcome.SUCCESS,
                reservation_id=reservation_id,
                balance=new_balance,
            )

    async def settle_ledger(
        self, delegation_id: str, reservation_id: str,
        actual: int, usage_event_id: str | None = None,
    ) -> LedgerResult:
        return await asyncio.to_thread(
            self._settle_ledger_sync, delegation_id, reservation_id, actual, usage_event_id
        )

    def _settle_ledger_sync(self, delegation_id, reservation_id, actual, usage_event_id) -> LedgerResult:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Idempotent: if usage_event_id already settled, return existing.
            if usage_event_id is not None:
                existing = conn.execute(
                    "SELECT * FROM delegation_budget_ledger WHERE usage_event_id = ? "
                    "AND settled > 0",
                    (usage_event_id,),
                ).fetchone()
                if existing is not None:
                    d_row = conn.execute(
                        "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
                    ).fetchone()
                    conn.commit()
                    balance = d_row["budget_total_tokens"] - d_row["budget_reserved_tokens"] - d_row["budget_settled_tokens"] if d_row else None
                    return LedgerResult(
                        outcome=MutationOutcome.IDEMPOTENT_REPLAY,
                        reservation_id=existing["reservation_id"],
                        balance=balance,
                    )
            row = conn.execute(
                "SELECT * FROM delegation_budget_ledger WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return LedgerResult(outcome=MutationOutcome.CONFLICT)
            if row["settled"] > 0:
                conn.commit()
                return LedgerResult(outcome=MutationOutcome.IDEMPOTENT_REPLAY, reservation_id=reservation_id)
            reserved = row["reserved"]
            conn.execute(
                "UPDATE delegation_budget_ledger SET settled = ?, usage_event_id = ?, "
                "updated_at = ? WHERE reservation_id = ?",
                (actual, usage_event_id, now, reservation_id),
            )
            # Release the difference (reserved - actual) back, settle actual.
            conn.execute(
                "UPDATE delegations SET budget_reserved_tokens = budget_reserved_tokens - ?, "
                "budget_settled_tokens = budget_settled_tokens + ?, updated_at = ? "
                "WHERE id = ?",
                (reserved, actual, now, delegation_id),
            )
            d_row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            balance = d_row["budget_total_tokens"] - d_row["budget_reserved_tokens"] - d_row["budget_settled_tokens"]
            conn.commit()
            return LedgerResult(
                outcome=MutationOutcome.SUCCESS,
                reservation_id=reservation_id,
                balance=balance,
            )

    async def release_ledger(
        self, delegation_id: str, reservation_id: str,
    ) -> LedgerResult:
        return await asyncio.to_thread(self._release_ledger_sync, delegation_id, reservation_id)

    def _release_ledger_sync(self, delegation_id, reservation_id) -> LedgerResult:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM delegation_budget_ledger WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                conn.commit()
                return LedgerResult(outcome=MutationOutcome.CONFLICT)
            if row["released"] > 0 or row["settled"] > 0:
                conn.commit()
                return LedgerResult(outcome=MutationOutcome.IDEMPOTENT_REPLAY, reservation_id=reservation_id)
            reserved = row["reserved"]
            conn.execute(
                "UPDATE delegation_budget_ledger SET released = ?, updated_at = ? "
                "WHERE reservation_id = ?",
                (reserved, now, reservation_id),
            )
            conn.execute(
                "UPDATE delegations SET budget_reserved_tokens = budget_reserved_tokens - ?, "
                "updated_at = ? WHERE id = ?",
                (reserved, now, delegation_id),
            )
            d_row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            balance = d_row["budget_total_tokens"] - d_row["budget_reserved_tokens"] - d_row["budget_settled_tokens"]
            conn.commit()
            return LedgerResult(
                outcome=MutationOutcome.SUCCESS,
                reservation_id=reservation_id,
                balance=balance,
            )

    # ------------------------------------------------------------------
    # cancel outbox
    # ------------------------------------------------------------------

    async def request_cancel(self, delegation_id: str, reason: str) -> Delegation:
        return await asyncio.to_thread(self._request_cancel_sync, delegation_id, reason)

    def _request_cancel_sync(self, delegation_id, reason) -> Delegation:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            if row is None:
                conn.commit()
                raise DelegationConflictError(f"delegation {delegation_id} not found")
            delegation = self._row_to_delegation(row)
            if delegation.is_terminal:
                conn.commit()
                return delegation
            # If already CANCELLING with an existing outbox entry, keep first reason.
            existing_outbox = conn.execute(
                "SELECT * FROM delegation_cancel_outbox WHERE delegation_id = ? "
                "ORDER BY id LIMIT 1",
                (delegation_id,),
            ).fetchone()
            if delegation.status is DelegationStatus.CANCELLING and existing_outbox is not None:
                conn.commit()
                return delegation
            conn.execute(
                "UPDATE delegations SET status = 'cancelling', updated_at = ? "
                "WHERE id = ?",
                (now, delegation_id),
            )
            conn.execute(
                "UPDATE delegation_members SET cancel_requested_at = ? "
                "WHERE delegation_id = ? AND status NOT IN ('succeeded','failed','cancelled','expired')",
                (now, delegation_id),
            )
            conn.execute(
                "INSERT INTO delegation_cancel_outbox (delegation_id, member_id, "
                "reason, attempts, next_attempt_at, acked_at, created_at, updated_at) "
                "VALUES (?, NULL, ?, 0, ?, NULL, ?, ?)",
                (delegation_id, reason, now, now, now),
            )
            conn.execute(
                "INSERT INTO delegation_events (delegation_id, kind, "
                "payload_json, member_ordinal, run_id, created_at) "
                "VALUES (?, 'cancel_requested', ?, NULL, NULL, ?)",
                (delegation_id, json.dumps({"reason": reason}, ensure_ascii=False), now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            return self._row_to_delegation(row)

    async def list_outbox_pending(self, limit: int = 100) -> tuple[CancelOutboxEntry, ...]:
        return await asyncio.to_thread(self._list_outbox_pending_sync, limit)

    def _list_outbox_pending_sync(self, limit) -> tuple[CancelOutboxEntry, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM delegation_cancel_outbox WHERE acked_at IS NULL "
                "ORDER BY next_attempt_at LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            CancelOutboxEntry(
                id=r["id"], delegation_id=r["delegation_id"],
                member_id=r["member_id"], reason=r["reason"],
                attempts=r["attempts"], next_attempt_at=r["next_attempt_at"],
                acked_at=r["acked_at"],
            )
            for r in rows
        )

    async def ack_outbox(self, entry_id: int) -> None:
        await asyncio.to_thread(self._ack_outbox_sync, entry_id)

    def _ack_outbox_sync(self, entry_id: int) -> None:
        now = self._now_iso()
        with self._connect() as conn:
            conn.execute(
                "UPDATE delegation_cancel_outbox SET acked_at = ?, updated_at = ? "
                "WHERE id = ? AND acked_at IS NULL",
                (now, now, entry_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # result set
    # ------------------------------------------------------------------

    async def get_result_set(self, delegation_id: str) -> DelegationResultSet | None:
        return await asyncio.to_thread(self._get_result_set_sync, delegation_id)

    def _get_result_set_sync(self, delegation_id: str) -> DelegationResultSet | None:
        with self._connect() as conn:
            d_row = conn.execute(
                "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
            ).fetchone()
            if d_row is None:
                return None
            delegation = self._row_to_delegation(d_row)
            m_rows = conn.execute(
                "SELECT * FROM delegation_members WHERE delegation_id = ? ORDER BY ordinal",
                (delegation_id,),
            ).fetchall()
            members = [self._row_to_member(r) for r in m_rows]
            # Only build a result set when all members are terminal.
            if not all(m.is_terminal for m in members):
                return None
            member_results: list[DelegationResult] = []
            for m in members:
                r_row = conn.execute(
                    "SELECT * FROM delegation_results WHERE member_id = ? AND adopted = 1",
                    (m.id,),
                ).fetchone()
                if r_row is not None:
                    # Override status with member status (result status mirrors it).
                    member_results.append(self._row_to_result(r_row))
                else:
                    member_results.append(DelegationResult(
                        status=m.status, summary="", checksum="",
                    ))
            agg_row = conn.execute(
                "SELECT * FROM delegation_results WHERE delegation_id = ? "
                "AND result_scope = 'delegation' AND adopted = 1",
                (delegation_id,),
            ).fetchone()
            aggregation_result = self._row_to_result(agg_row) if agg_row else None
            return evaluate_join_outcome(
                delegation_id=delegation_id,
                join_policy=delegation.join_policy,
                aggregation=delegation.aggregation,
                member_results=tuple(member_results),
                aggregation_result=aggregation_result,
            )
