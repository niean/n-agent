"""Tests for SQLiteDelegationRegistry schema (Infrastructure Layer).

T4: idempotent migration + 7-table creation + shape verification +
order-independence from Task registry + rollback safety + no competing
schema-version owner.

These tests cover ONLY schema/migration (T4). Read/write CRUD/CAS/ledger/
outbox operations are covered in T5 (appended to this file).
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.domain.delegation import (
    DelegationAggregationPolicy,
    DelegationConflictError,
    DelegationJoinPolicy,
    DelegationMember,
    DelegationMemberRole,
    DelegationMemberStatus,
    DelegationParentRef,
    DelegationResult,
    DelegationCreateRequest,
    PolicySnapshotRecord,
)
from app.infrastructure.registry.sqlite_delegation_registry import (
    SQLiteDelegationRegistry,
)

# The 7 delegation tables mandated by the spec Persistence section.
DELEGATION_TABLES = (
    "delegations",
    "delegation_members",
    "delegation_policy_snapshots",
    "delegation_results",
    "delegation_events",
    "delegation_budget_ledger",
    "delegation_cancel_outbox",
)


def _table_names(db_path: str | Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r[0] for r in rows}


def _columns(db_path: str | Path, table: str) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _index_names(db_path: str | Path, table: str) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _read_user_version(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _insert_delegation(conn, *, id="d1", parent_source="task",
                       parent_scope_id="t1", delegation_key="k1",
                       fingerprint="fp1", status="pending") -> None:
    conn.execute(
        "INSERT INTO delegations (id, parent_source, parent_scope_id, "
        "parent_run_id, parent_session_id, delegation_key, fingerprint, "
        "status, join_policy, aggregation, policy_snapshot_id, "
        "budget_total_tokens, budget_reserved_tokens, budget_settled_tokens, "
        "version, created_at, updated_at) "
        "VALUES (:id, :ps, :psi, 'r1', 's1', :dk, :fp, :st, "
        "'all_completed', 'parent', 'ps1', 1000, 0, 0, 1, "
        "'2026-08-12T00:00:00Z', '2026-08-12T00:00:00Z')",
        dict(id=id, ps=parent_source, psi=parent_scope_id, dk=delegation_key,
             fp=fingerprint, st=status),
    )


def _insert_member(conn, *, id="m1", delegation_id="d1", role="worker",
                   ordinal=0, status="pending") -> None:
    conn.execute(
        "INSERT INTO delegation_members (id, delegation_id, role, ordinal, "
        "title, instruction, skills_json, allowed_tools_json, "
        "model_override, max_runtime_seconds, execution_session_id, "
        "deadline_at, budget_tokens, status, version, retry_count, "
        "retry_of, claim_lock, claim_expires_at, last_heartbeat_at, "
        "cancel_reason, cancel_requested_at, started_at, ended_at) "
        "VALUES (:id, :did, :role, :ord, 'w0', 'do', '[]', '[]', NULL, 300, "
        "'delegation-sess-0', NULL, 500, :st, 1, 0, NULL, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL)",
        dict(id=id, did=delegation_id, role=role, ord=ordinal, st=status),
    )


# ---------------------------------------------------------------------------
# S1: idempotent migration + 7-table creation
# ---------------------------------------------------------------------------


def test_migration_creates_all_seven_tables(tmp_path):
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    tables = _table_names(db)
    for t in DELEGATION_TABLES:
        assert t in tables, f"missing table: {t}"


def test_migration_idempotent_reinit_no_error_no_data_loss(tmp_path):
    db = tmp_path / "deleg.db"
    reg = SQLiteDelegationRegistry(str(db))
    reg.initialize()
    # Insert a row manually to prove re-init preserves data.
    with sqlite3.connect(str(db)) as conn:
        _insert_delegation(conn)
        conn.commit()
    before = _table_names(db)

    reg.initialize()  # second init, idempotent
    after = _table_names(db)
    assert before == after
    # Data preserved.
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT id FROM delegations WHERE id = 'd1'"
        ).fetchone()
    assert row is not None and row[0] == "d1"


def test_delegations_unique_constraint_on_scope_key(tmp_path):
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    with sqlite3.connect(str(db)) as conn:
        _insert_delegation(conn, id="d1")
        conn.commit()
    # Same (parent_source, parent_scope_id, delegation_key) must conflict.
    with sqlite3.connect(str(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_delegation(conn, id="d2", fingerprint="fp2")
            conn.commit()


def test_members_unique_role_ordinal(tmp_path):
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    with sqlite3.connect(str(db)) as conn:
        _insert_delegation(conn)
        _insert_member(conn, id="m1", ordinal=0)
        conn.commit()
    # Same (delegation_id, role, ordinal) must conflict.
    with sqlite3.connect(str(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_member(conn, id="m2", ordinal=0)
            conn.commit()


def test_results_partial_unique_index_per_member(tmp_path):
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    with sqlite3.connect(str(db)) as conn:
        _insert_delegation(conn)
        _insert_member(conn, status="succeeded")
        conn.execute(
            "INSERT INTO delegation_results (id, delegation_id, result_scope, "
            "member_id, summary, structured_data_json, artifact_refs_json, "
            "error_code, error_message, usage_summary_json, classification, "
            "checksum, started_at, ended_at, created_at, adopted) "
            "VALUES ('r1','d1','member','m1','ok','{}','[]',NULL,NULL,'{}',"
            "NULL,'c1',NULL,NULL,'2026-08-12T00:00:00Z',1)"
        )
        conn.commit()
    # A second adopted member-scope result for the same member must conflict.
    with sqlite3.connect(str(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO delegation_results (id, delegation_id, result_scope, "
                "member_id, summary, structured_data_json, artifact_refs_json, "
                "error_code, error_message, usage_summary_json, classification, "
                "checksum, started_at, ended_at, created_at, adopted) "
                "VALUES ('r2','d1','member','m1','late','{}','[]',NULL,NULL,"
                "'{}',NULL,'c2',NULL,NULL,'2026-08-12T00:00:01Z',1)"
            )
            conn.commit()


def test_results_non_adopted_does_not_conflict(tmp_path):
    """Non-adopted (adopted=0) results do not trigger the partial unique
    index, allowing multiple late/audit results per member."""
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    with sqlite3.connect(str(db)) as conn:
        _insert_delegation(conn)
        _insert_member(conn, status="succeeded")
        conn.execute(
            "INSERT INTO delegation_results (id, delegation_id, result_scope, "
            "member_id, summary, structured_data_json, artifact_refs_json, "
            "error_code, error_message, usage_summary_json, classification, "
            "checksum, started_at, ended_at, created_at, adopted) "
            "VALUES ('r1','d1','member','m1','ok','{}','[]',NULL,NULL,'{}',"
            "NULL,'c1',NULL,NULL,'2026-08-12T00:00:00Z',1)"
        )
        # Late non-adopted result: allowed (audit only).
        conn.execute(
            "INSERT INTO delegation_results (id, delegation_id, result_scope, "
            "member_id, summary, structured_data_json, artifact_refs_json, "
            "error_code, error_message, usage_summary_json, classification, "
            "checksum, started_at, ended_at, created_at, adopted) "
            "VALUES ('r2','d1','member','m1','late','{}','[]',NULL,NULL,'{}',"
            "NULL,'c2',NULL,NULL,'2026-08-12T00:00:01Z',0)"
        )
        conn.commit()


def test_events_append_only_monotonic_id(tmp_path):
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    with sqlite3.connect(str(db)) as conn:
        _insert_delegation(conn)
        conn.execute(
            "INSERT INTO delegation_events (delegation_id, kind, payload_json, "
            "member_ordinal, run_id, created_at) "
            "VALUES ('d1','created','{}',NULL,'r1','2026-08-12T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO delegation_events (delegation_id, kind, payload_json, "
            "member_ordinal, run_id, created_at) "
            "VALUES ('d1','member_claimed','{}',0,'r1','2026-08-12T00:00:01Z')"
        )
        conn.commit()
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM delegation_events ORDER BY id"
        )]
    assert ids == sorted(ids)
    assert ids[0] < ids[1]


def test_cancel_outbox_has_retry_columns(tmp_path):
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    cols = _columns(db, "delegation_cancel_outbox")
    for c in ("attempts", "next_attempt_at", "acked_at"):
        assert c in cols


def test_ledger_reservation_id_unique(tmp_path):
    db = tmp_path / "deleg.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    with sqlite3.connect(str(db)) as conn:
        _insert_delegation(conn)
        conn.execute(
            "INSERT INTO delegation_budget_ledger (id, delegation_id, member_id, "
            "reservation_id, purpose, amount, reserved, settled, released, "
            "usage_event_id, version, created_at, updated_at) "
            "VALUES ('l1','d1','m1','res1','claim',500,500,0,0,NULL,1,"
            "'2026-08-12T00:00:00Z','2026-08-12T00:00:00Z')"
        )
        conn.commit()
    with sqlite3.connect(str(db)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO delegation_budget_ledger (id, delegation_id, "
                "member_id, reservation_id, purpose, amount, reserved, settled, "
                "released, usage_event_id, version, created_at, updated_at) "
                "VALUES ('l2','d1','m2','res1','claim',300,300,0,0,NULL,1,"
                "'2026-08-12T00:00:00Z','2026-08-12T00:00:00Z')"
            )
            conn.commit()


# ---------------------------------------------------------------------------
# S1: order-independence from Task registry migration
# ---------------------------------------------------------------------------


def test_migration_compatible_with_task_registry_either_order(tmp_path):
    """Delegation and Task migrations use disjoint table sets and must not
    interfere regardless of initialization order."""
    from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry

    # Order 1: delegation first, then task.
    db1 = tmp_path / "a.db"
    SQLiteDelegationRegistry(str(db1)).initialize()
    SQLiteTaskRegistry(str(db1))
    tables1 = _table_names(db1)
    assert {"delegations", "tasks"}.issubset(tables1)

    # Order 2: task first, then delegation.
    db2 = tmp_path / "b.db"
    SQLiteTaskRegistry(str(db2))
    SQLiteDelegationRegistry(str(db2)).initialize()
    tables2 = _table_names(db2)
    assert {"delegations", "tasks"}.issubset(tables2)

    # Both orders yield the same delegation tables.
    assert {t for t in tables1 if t.startswith("delegation")} == {
        t for t in tables2 if t.startswith("delegation")
    }


def test_delegation_does_not_advance_user_version(tmp_path):
    """The Task registry owns PRAGMA user_version for its one-time legacy
    status migration. Delegation must NOT advance or read user_version to
    avoid colliding with that guarded migration."""
    from app.infrastructure.registry.sqlite_task_registry import SQLiteTaskRegistry

    db = tmp_path / "c.db"
    SQLiteTaskRegistry(str(db))
    task_user_version = _read_user_version(db)
    SQLiteDelegationRegistry(str(db)).initialize()
    after_user_version = _read_user_version(db)
    assert after_user_version == task_user_version


# ---------------------------------------------------------------------------
# S1: rollback safety + incompatible shape detection
# ---------------------------------------------------------------------------


def test_migration_rollback_leaves_no_half_tables(tmp_path):
    """An incompatible pre-existing delegations table must cause
    initialize() to fail (raised during __init__) without creating any
    other delegation tables."""
    db = tmp_path / "d.db"
    # Pre-create an incompatible delegations table (missing required cols).
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE delegations (id TEXT PRIMARY KEY, status TEXT)"
        )
        conn.commit()
    with pytest.raises(Exception):
        SQLiteDelegationRegistry(str(db))
    tables = _table_names(db)
    # The other 6 delegation tables must not exist.
    for t in DELEGATION_TABLES:
        if t == "delegations":
            continue
        assert t not in tables, f"unexpected table created despite failure: {t}"


def test_existing_incompatible_table_shape_fails_without_mutation(tmp_path):
    """If an existing delegation table has an incompatible shape (missing
    required column), initialize() must fail and leave existing data
    unchanged."""
    db = tmp_path / "e.db"
    # Pre-create delegations missing the budget_total_tokens column.
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE delegations (id TEXT PRIMARY KEY, parent_source TEXT, "
            "parent_scope_id TEXT, delegation_key TEXT, fingerprint TEXT)"
        )
        conn.execute(
            "INSERT INTO delegations (id, parent_source, parent_scope_id, "
            "delegation_key, fingerprint) VALUES ('x','task','t','k','f')"
        )
        conn.commit()
    with pytest.raises(Exception):
        SQLiteDelegationRegistry(str(db))
    # Existing row preserved.
    with sqlite3.connect(str(db)) as conn:
        row = conn.execute(
            "SELECT id FROM delegations WHERE id = 'x'"
        ).fetchone()
    assert row is not None and row[0] == "x"
    # No new tables created.
    assert "delegation_members" not in _table_names(db)


def test_delegation_schema_has_required_indexes(tmp_path):
    """Verify key indexes exist for query performance and constraints."""
    db = tmp_path / "f.db"
    SQLiteDelegationRegistry(str(db)).initialize()
    delegations_idx = _index_names(db, "delegations")
    # Scope-key unique index + status/deadline indexes.
    assert any("scope_key" in idx for idx in delegations_idx)
    assert any("status" in idx for idx in delegations_idx)
    members_idx = _index_names(db, "delegation_members")
    assert any("role_ordinal" in idx for idx in members_idx)
    outbox_idx = _index_names(db, "delegation_cancel_outbox")
    assert any("pending" in idx for idx in outbox_idx)


# ===========================================================================
# T5: read/write operations (CRUD / CAS / idempotent / ledger / outbox)
# ===========================================================================


def _make_parent(source="task", scope_id="t1", run_id="r1", session_id="s1"):
    return DelegationParentRef(
        source=source, scope_id=scope_id, run_id=run_id, session_id=session_id
    )


def _make_member(ordinal, *, role=DelegationMemberRole.WORKER, title="w",
                 budget_tokens=100, execution_session_id=""):
    return DelegationMember.new(
        delegation_id="",  # set by registry on create
        role=role,
        ordinal=ordinal,
        title=f"{title}{ordinal}",
        instruction="do the work",
        skills=(),
        allowed_tools=(),
        execution_session_id=execution_session_id or f"delegation-sess-{ordinal}",
        deadline_at="2026-08-12T03:00:00Z",
        budget_tokens=budget_tokens,
    )


def _make_request(parent=None, delegation_key="k1", fingerprint="fp1",
                  members=None, budget_total_tokens=1000):
    return DelegationCreateRequest(
        parent=parent or _make_parent(),
        delegation_key=delegation_key,
        fingerprint=fingerprint,
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.PARENT,
        deadline_at="2026-08-12T03:00:00Z",
        budget_total_tokens=budget_total_tokens,
        members=members or (_make_member(0), _make_member(1)),
        snapshot=PolicySnapshotRecord(
            profile_version="v1",
            parent_config={"turn": {"iteration_limit": 10}},
            child_config={"turn": {"iteration_limit": 5}},
            aggregator_config=None,
            checksum="snap-checksum-1",
        ),
    )


@pytest.fixture
def registry(tmp_path):
    reg = SQLiteDelegationRegistry(str(tmp_path / "deleg.db"))
    return reg


# ---------------------------------------------------------------------------
# create_or_reconnect: idempotent + conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_or_reconnect_creates_delegation_and_members(registry):
    req = _make_request()
    d = await registry.create_or_reconnect(req)
    assert d.delegation_key == "k1"
    assert d.fingerprint == "fp1"
    assert d.status.value == "pending"
    # Members persisted.
    members = await registry.list_members(d.id)
    assert len(members) == 2
    assert [m.ordinal for m in members] == [0, 1]
    # Snapshot persisted.
    snap = await registry.get_policy_snapshot(d.id)
    assert snap is not None
    assert snap.checksum == "snap-checksum-1"
    # Initial event recorded.
    events = await registry.list_events(d.id)
    assert len(events) >= 1
    assert events[0].kind == "created"


@pytest.mark.asyncio
async def test_create_or_reconnect_idempotent_same_fingerprint(registry):
    req = _make_request()
    d1 = await registry.create_or_reconnect(req)
    d2 = await registry.create_or_reconnect(req)
    assert d1.id == d2.id
    members = await registry.list_members(d1.id)
    assert len(members) == 2  # no duplicate members


@pytest.mark.asyncio
async def test_create_conflict_different_fingerprint(registry):
    await registry.create_or_reconnect(_make_request(fingerprint="fp1"))
    with pytest.raises(DelegationConflictError):
        await registry.create_or_reconnect(_make_request(fingerprint="fp2"))


@pytest.mark.asyncio
async def test_get_returns_none_for_missing(registry):
    assert await registry.get("nonexistent") is None


@pytest.mark.asyncio
async def test_list_for_trusted_scope_filters_by_scope(registry):
    await registry.create_or_reconnect(
        _make_request(parent=_make_parent(scope_id="t1"), delegation_key="k1")
    )
    await registry.create_or_reconnect(
        _make_request(parent=_make_parent(scope_id="t2"), delegation_key="k2")
    )
    t1_delegations = await registry.list_for_trusted_scope("t1")
    assert len(t1_delegations) == 1
    assert t1_delegations[0].delegation_key == "k1"


# ---------------------------------------------------------------------------
# claim_member: CAS atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_member_succeeds_for_pending(registry):
    d = await registry.create_or_reconnect(_make_request())
    result = await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    assert result.outcome.value == "success"
    assert result.member is not None
    assert result.member.status.value == "running"
    assert result.member.claim_lock == "lock-A"
    assert result.delegation is not None


@pytest.mark.asyncio
async def test_claim_member_idempotent_same_lock(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    result = await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    assert result.outcome.value == "idempotent_replay"


@pytest.mark.asyncio
async def test_claim_member_conflict_different_lock(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    result = await registry.claim_member(d.id, 0, "lock-B", lease_seconds=60)
    assert result.outcome.value == "conflict"


@pytest.mark.asyncio
async def test_claim_member_busy_for_terminal(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    await registry.finish_member(
        d.id, 0, "lock-A",
        DelegationResult(status=DelegationMemberStatus.SUCCEEDED,
                         summary="done", checksum="c1"),
        expected_version=2,
    )
    result = await registry.claim_member(d.id, 0, "lock-B", lease_seconds=60)
    assert result.outcome.value == "busy"


# ---------------------------------------------------------------------------
# finish_member: CAS + late overwrite rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finish_member_succeeds_and_writes_result(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    result = await registry.finish_member(
        d.id, 0, "lock-A",
        DelegationResult(
            status=DelegationMemberStatus.SUCCEEDED,
            summary="done", checksum="c1",
        ),
        expected_version=2,
    )
    assert result.outcome.value == "success"
    assert result.member.status.value == "succeeded"


@pytest.mark.asyncio
async def test_finish_member_rejects_late_overwrite(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    await registry.finish_member(
        d.id, 0, "lock-A",
        DelegationResult(
            status=DelegationMemberStatus.SUCCEEDED,
            summary="done", checksum="c1",
        ),
        expected_version=2,
    )
    # Late finish attempt with different checksum -> conflict, no overwrite.
    late = await registry.finish_member(
        d.id, 0, "lock-A",
        DelegationResult(
            status=DelegationMemberStatus.SUCCEEDED,
            summary="late", checksum="c2",
        ),
        expected_version=3,
    )
    assert late.outcome.value == "conflict"


@pytest.mark.asyncio
async def test_finish_member_idempotent_same_checksum(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    r1 = await registry.finish_member(
        d.id, 0, "lock-A",
        DelegationResult(
            status=DelegationMemberStatus.SUCCEEDED,
            summary="done", checksum="c1",
        ),
        expected_version=2,
    )
    r2 = await registry.finish_member(
        d.id, 0, "lock-A",
        DelegationResult(
            status=DelegationMemberStatus.SUCCEEDED,
            summary="done", checksum="c1",
        ),
        expected_version=2,
    )
    assert r1.outcome.value == "success"
    assert r2.outcome.value == "idempotent_replay"


# ---------------------------------------------------------------------------
# ledger: reserve / settle (idempotent) / release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserve_ledger_success(registry):
    d = await registry.create_or_reconnect(_make_request())
    result = await registry.reserve_ledger(d.id, amount=200, purpose="claim")
    assert result.outcome.value == "success"
    assert result.reservation_id is not None
    assert result.balance == 800  # 1000 - 200


@pytest.mark.asyncio
async def test_reserve_ledger_conflict_insufficient(registry):
    d = await registry.create_or_reconnect(_make_request(budget_total_tokens=100))
    result = await registry.reserve_ledger(d.id, amount=200, purpose="claim")
    assert result.outcome.value == "conflict"


@pytest.mark.asyncio
async def test_settle_ledger_idempotent_by_usage_event(registry):
    d = await registry.create_or_reconnect(_make_request())
    res = await registry.reserve_ledger(d.id, amount=200, purpose="claim")
    s1 = await registry.settle_ledger(
        d.id, res.reservation_id, actual=150, usage_event_id="ue-1"
    )
    assert s1.outcome.value == "success"
    # Duplicate settle with same usage_event_id -> idempotent, no double deduct.
    s2 = await registry.settle_ledger(
        d.id, res.reservation_id, actual=150, usage_event_id="ue-1"
    )
    assert s2.outcome.value == "idempotent_replay"
    # Balance reflects single settle: 1000 - 150 = 850.
    assert s2.balance == 850


@pytest.mark.asyncio
async def test_release_ledger_returns_unused(registry):
    d = await registry.create_or_reconnect(_make_request())
    res = await registry.reserve_ledger(d.id, amount=200, purpose="claim")
    result = await registry.release_ledger(d.id, res.reservation_id)
    assert result.outcome.value == "success"
    # Balance restored: 1000 - 0 = 1000 (released before settle).
    assert result.balance == 1000


# ---------------------------------------------------------------------------
# cancel outbox: same-transaction state change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_cancel_inserts_outbox_and_sets_cancelling(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    await registry.request_cancel(d.id, reason="parent_requested")
    updated = await registry.get(d.id)
    assert updated.status.value == "cancelling"
    pending = await registry.list_outbox_pending(limit=10)
    assert len(pending) >= 1
    assert pending[0].reason == "parent_requested"


@pytest.mark.asyncio
async def test_ack_outbox_marks_acked(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    await registry.request_cancel(d.id, reason="parent_requested")
    pending = await registry.list_outbox_pending(limit=10)
    entry_id = pending[0].id
    await registry.ack_outbox(entry_id)
    pending_after = await registry.list_outbox_pending(limit=10)
    assert all(e.id != entry_id for e in pending_after)


@pytest.mark.asyncio
async def test_request_cancel_idempotent_reason(registry):
    d = await registry.create_or_reconnect(_make_request())
    await registry.claim_member(d.id, 0, "lock-A", lease_seconds=60)
    await registry.request_cancel(d.id, reason="parent_requested")
    await registry.request_cancel(d.id, reason="other_reason")
    updated = await registry.get(d.id)
    # First trusted reason is not overwritten.
    pending = await registry.list_outbox_pending(limit=10)
    assert len(pending) == 1
    assert pending[0].reason == "parent_requested"


# ---------------------------------------------------------------------------
# result set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_result_set_returns_none_before_completion(registry):
    d = await registry.create_or_reconnect(_make_request())
    assert await registry.get_result_set(d.id) is None
