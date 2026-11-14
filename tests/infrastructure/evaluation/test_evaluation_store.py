"""Tests for SQLiteEvaluationStore (Infrastructure Layer).

T3: spec 6.3 11 core tables (all prefixed evaluation_) plus a persistent
deletion-tombstone table required by the plan as an 18 prerequisite.

Covers:
- schema init/migration idempotency + schema version sequence
- sample idempotency UNIQUE(service, producer_event_id) + payload hash
- definitions UNIQUE(definition_id, version) + write-once immutability
- results exactly-one-owner CHECK + partial unique indexes + immutability
- jobs idempotency key + lease fields + claim_next_job CAS concurrency
- attempts append-only; retries reuse the same job
- deliveries UNIQUE(result_id, sink, projection_version) + state.py transitions
- producer_state UNIQUE(producer_id, counter_epoch) + higher-sequence-only
- evidence_targets reverse index write + query by subject
- commit_result_with_deliveries single-transaction atomicity + lease token CAS
- current-view queries (superseded / latest-revision / deletion-revoked)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
from contextlib import contextmanager
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.infrastructure.evaluation.store as _evaluation_store_module
from app.domain.evaluation.errors import EvaluationBudgetExhaustedError
from app.domain.evaluation.models import EvaluationValidationError
from app.domain.evaluation.ports import EvaluationStore
from app.domain.evaluation.state import IllegalEvaluationTransition
from app.infrastructure.evaluation.store import (
    SQLiteEvaluationStore,
    EvaluationConflictError,
    EvaluationLeaseError,
    EvaluationNotFoundError,
    EvaluationStaleSequenceError,
)
from tests.infrastructure.evaluation._degraded_helpers import (
    drop_feedback_thread_indexes,
    force_schema_v7,
)

# The 11 spec-6.3 tables plus the two store-owned auxiliary tables:
# deletion tombstones (T18 prerequisite) and schema migration log.
EVALUATION_TABLES = (
    "evaluation_samples",
    "evaluation_definitions",
    "evaluation_experiments",
    "evaluation_jobs",
    "evaluation_attempts",
    "evaluation_results",
    "evaluation_feedback",
    "evaluation_deliveries",
    "evaluation_sink_configs",
    "evaluation_producer_state",
    "evaluation_evidence_targets",
    "evaluation_deletion_tombstones",
    "evaluation_deletion_tasks",
    "evaluation_calibrations",
    "evaluation_schema_migrations",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _table_names(db_path: str | Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r[0] for r in rows}


def _count(db_path: str | Path, table: str) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _read_user_version(db_path: str | Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _sample_payload(**overrides) -> dict:
    payload = {
        "service": "n-agent",
        "producer_event_id": "evt-1",
        "target": {
            "service": "n-agent",
            "kind": "retrieval",
            "subject_id": "search-1",
            "subject_revision": "rev-1",
        },
        "snapshot_ref": "eval-snapshots/abc.json",
        "snapshot_hash": "a" * 64,
        "schema_version": "evaluation.sample.v1",
        "captured_at": "2026-11-10T00:00:00+00:00",
        "sample_probability": 1.0,
        "sampling_policy_version": "sp-1",
        "evidence_targets": [
            {
                "source_service": "n-agent",
                "subject_kind": "session",
                "subject_id": "s-1",
                "subject_revision": "sr-1",
            },
            {
                "source_service": "n-agent",
                "subject_kind": "document_revision",
                "subject_id": "doc-1",
                "subject_revision": "dr-7",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _definition_payload(**overrides) -> dict:
    payload = {
        "definition_id": "def-answer-relevance",
        "version": "v1",
        "metric_name": "answer_relevance",
        "target_kind": "agent_run",
        "method": "deterministic",
        "required_fields": ["query", "answer"],
        "evidence_policy": "snapshot_required",
        "output_type": "number",
        "range_or_categories": [0, 1],
        "direction": "higher_is_better",
        "material_hash": {"rubric": "b" * 64},
        "judge_model": None,
        "judge_params": {},
        "timeout_seconds": 60,
        "budget": None,
        "ci_admissible": True,
    }
    payload.update(overrides)
    return payload


def _job_payload(sample_id: str, **overrides) -> dict:
    payload = {
        "sample_id": sample_id,
        "definition_id": "def-answer-relevance",
        "definition_version": "v1",
        "requested_by": "eval-worker",
        "idempotency_key": "idem-1",
    }
    payload.update(overrides)
    return payload


def _auto_result_payload(job_id: str, **overrides) -> dict:
    payload = {
        "job_id": job_id,
        "provenance": "automatic",
        "metric_name": "answer_relevance",
        "metric_version": "v1",
        "status": "value",
        "typed_value": 0.87,
        "passed": True,
        "evidence_hash": "c" * 64,
    }
    payload.update(overrides)
    return payload


def _human_result_payload(feedback_id: str, **overrides) -> dict:
    payload = {
        "feedback_id": feedback_id,
        "provenance": "human",
        "metric_name": "answer_relevance",
        "metric_version": "v1",
        "status": "value",
        "typed_value": 0.5,
        "evidence_hash": "d" * 64,
    }
    payload.update(overrides)
    return payload


def _feedback_payload(**overrides) -> dict:
    payload = {
        "actor_ref": "user-alice",
        "definition_id": "def-answer-relevance",
        "definition_version": "v1",
        "target": {
            "service": "n-agent",
            "kind": "session_feedback",
            "subject_id": "turn-1",
            "subject_revision": "rev-1",
        },
        "value": {"typed_value": 1, "status": "value"},
        "comment_ref": "comments/c-1",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Schema init / migration
# ---------------------------------------------------------------------------


async def test_schema_init_idempotent_and_version_sequence(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    assert _table_names(db_path) >= set(EVALUATION_TABLES)
    assert _read_user_version(db_path) == SQLiteEvaluationStore.SCHEMA_VERSION

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT version, name FROM evaluation_schema_migrations ORDER BY version"
        ).fetchall()
    assert [r[0] for r in rows] == [SQLiteEvaluationStore.SCHEMA_VERSION]

    sample_id = await store.save_sample(_sample_payload())

    # Re-init must not touch schema version, migration rows, or data.
    SQLiteEvaluationStore(str(db_path))
    assert _read_user_version(db_path) == SQLiteEvaluationStore.SCHEMA_VERSION
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT version FROM evaluation_schema_migrations ORDER BY version"
        ).fetchall()
    assert [r[0] for r in rows] == [SQLiteEvaluationStore.SCHEMA_VERSION]
    assert _count(db_path, "evaluation_samples") == 1
    assert (await store.load_sample(sample_id))["sample_id"] == sample_id


async def test_store_satisfies_evaluation_store_port(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    assert isinstance(store, EvaluationStore)


async def test_connections_use_house_journal_mode(tmp_path):
    # House convention (app/infrastructure/sqlite_support.py) mandates DELETE
    # journal mode on every writable connection; WAL is deliberately not used
    # (macOS Docker bind-mount mmap SIGBUS). The store routes through it.
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    conn = store._connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "delete"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Samples + evidence_targets reverse index
# ---------------------------------------------------------------------------


async def test_save_sample_idempotent_same_hash_conflict_different_hash(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    payload = _sample_payload()
    sample_id_1 = await store.save_sample(payload)
    sample_id_2 = await store.save_sample(dict(payload))
    assert sample_id_1 == sample_id_2
    assert _count(tmp_path / "evaluations.db", "evaluation_samples") == 1

    conflicting = dict(payload)
    conflicting["snapshot_hash"] = "e" * 64
    with pytest.raises(EvaluationConflictError):
        await store.save_sample(conflicting)


async def test_sample_roundtrip_and_timestamps_are_utc_iso(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    sample_id = await store.save_sample(_sample_payload())
    loaded = await store.load_sample(sample_id)
    assert loaded["service"] == "n-agent"
    assert loaded["producer_event_id"] == "evt-1"
    assert loaded["target"]["subject_id"] == "search-1"
    assert loaded["snapshot_ref"] == "eval-snapshots/abc.json"
    # created_at is store-generated UTC ISO-8601
    parsed = datetime.fromisoformat(loaded["created_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)

    with pytest.raises(EvaluationNotFoundError):
        await store.load_sample("missing")


async def test_evidence_targets_reverse_index_write_and_query(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    sample_id = await store.save_sample(_sample_payload())

    assert await store.list_sample_ids_by_subject("n-agent", "session", "s-1") == [
        sample_id
    ]
    assert await store.list_sample_ids_by_subject(
        "n-agent", "document_revision", "doc-1", subject_revision="dr-7"
    ) == [sample_id]
    assert (
        await store.list_sample_ids_by_subject(
            "n-agent", "document_revision", "doc-1", subject_revision="dr-8"
        )
        == []
    )
    assert await store.list_sample_ids_by_subject("other", "session", "s-1") == []


# ---------------------------------------------------------------------------
# Definitions: unique + immutable
# ---------------------------------------------------------------------------


async def test_definitions_unique_and_write_once(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    payload = _definition_payload()
    definition_id_1 = await store.save_definition(payload)
    definition_id_2 = await store.save_definition(dict(payload))
    assert definition_id_1 == definition_id_2
    assert _count(db_path, "evaluation_definitions") == 1

    conflicting = dict(payload)
    conflicting["material_hash"] = {"rubric": "f" * 64}
    with pytest.raises(EvaluationConflictError):
        await store.save_definition(conflicting)

    # Immutable after write: even a raw UPDATE must be rejected by trigger.
    with sqlite3.connect(str(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evaluation_definitions SET metric_name = 'hacked'"
            )


# ---------------------------------------------------------------------------
# Results: exactly-one-owner CHECK, partial unique indexes, immutability
# ---------------------------------------------------------------------------


async def test_result_owner_exactly_one_non_null(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))

    both = _auto_result_payload("job-1", feedback_id="fb-1")
    with pytest.raises(sqlite3.IntegrityError):
        await store.save_result(both)

    neither = _auto_result_payload("job-1")
    neither.pop("job_id")
    with pytest.raises(sqlite3.IntegrityError):
        await store.save_result(neither)

    mismatched = _human_result_payload("fb-1", job_id="job-1")
    with pytest.raises(sqlite3.IntegrityError):
        await store.save_result(mismatched)


async def test_results_unique_constraints_and_terminal_immutability(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))

    first = await store.save_result(_auto_result_payload("job-1"))
    assert first
    # Same job + same metric: at most one automatic result.
    with pytest.raises(EvaluationConflictError):
        await store.save_result(_auto_result_payload("job-1", typed_value=0.1))
    # Different job, same metric: allowed.
    assert await store.save_result(_auto_result_payload("job-2"))

    # Human results: UNIQUE(feedback_id).
    await store.save_result(_human_result_payload("fb-1"))
    with pytest.raises(EvaluationConflictError):
        await store.save_result(_human_result_payload("fb-1", typed_value=0.9))

    # Terminal results are immutable: no UPDATE, revisions create new results.
    with sqlite3.connect(str(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evaluation_results SET typed_value_json = '99'"
            )


async def test_load_result_roundtrip(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    result_id = await store.save_result(_auto_result_payload("job-1"))
    loaded = await store.load_result(result_id)
    assert loaded["job_id"] == "job-1"
    assert loaded["metric_name"] == "answer_relevance"
    assert loaded["typed_value"] == 0.87
    assert loaded["passed"] is True
    assert loaded["provenance"] == "automatic"

    skipped = _auto_result_payload(
        "job-2", status="skipped", typed_value=None, skip_reason="missing_reference"
    )
    skipped_id = await store.save_result(skipped)
    loaded = await store.load_result(skipped_id)
    assert loaded["typed_value"] is None
    assert loaded["skip_reason"] == "missing_reference"

    with pytest.raises(EvaluationNotFoundError):
        await store.load_result("missing")


# ---------------------------------------------------------------------------
# Jobs: idempotency key, lease claim CAS, retries reuse job
# ---------------------------------------------------------------------------


async def test_save_job_idempotency_key_unique(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    job_id_1 = await store.save_job(_job_payload("smp-1"))
    job_id_2 = await store.save_job(_job_payload("smp-2"))
    assert job_id_1 == job_id_2
    assert _count(db_path, "evaluation_jobs") == 1

    job_id_3 = await store.save_job(_job_payload("smp-3", idempotency_key="idem-2"))
    assert job_id_3 != job_id_1


async def test_claim_next_job_lease_and_reclaim_after_expiry(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    sample_id = await store.save_sample(_sample_payload())
    job_id = await store.save_job(_job_payload(sample_id, idempotency_key="k1"))

    now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
    claimed = await store.claim_next_job("worker-a", lease_seconds=180, now=now)
    assert claimed is not None
    assert claimed["job_id"] == job_id
    assert claimed["status"] == "running"
    assert claimed["lease_owner"] == "worker-a"
    assert claimed["lease_token"]
    assert claimed["attempt_count"] == 1
    assert claimed["lease_expiry"] == "2026-11-10T12:03:00+00:00"

    # Still leased: nothing to claim.
    still_leased = await store.claim_next_job(
        "worker-b", lease_seconds=180, now=now + timedelta(seconds=1)
    )
    assert still_leased is None

    # Lease expired: reclaim returns the SAME job (retry never creates a new
    # job) with a fresh lease token and an appended attempt.
    reclaimed = await store.claim_next_job(
        "worker-b", lease_seconds=180, now=now + timedelta(seconds=181)
    )
    assert reclaimed is not None
    assert reclaimed["job_id"] == job_id
    assert reclaimed["lease_owner"] == "worker-b"
    assert reclaimed["lease_token"] != claimed["lease_token"]
    assert reclaimed["attempt_count"] == 2
    assert reclaimed["attempt_id"] != claimed["attempt_id"]

    attempts = await store.list_attempts(job_id)
    assert [a["attempt_index"] for a in attempts] == [0, 1]

    # Nothing left: drained.
    drained = await store.claim_next_job(
        "worker-b", lease_seconds=180, now=now + timedelta(seconds=182)
    )
    assert drained is None


async def test_claim_next_job_concurrent_exactly_one_winner_per_job(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store_a = SQLiteEvaluationStore(str(db_path))
    store_b = SQLiteEvaluationStore(str(db_path))

    job_ids = set()
    for i in range(10):
        sample_id = await store_a.save_sample(
            _sample_payload(producer_event_id=f"evt-{i}")
        )
        job_ids.add(
            await store_a.save_job(_job_payload(sample_id, idempotency_key=f"k-{i}"))
        )

    claims = await asyncio.gather(
        *[
            (
                store_a if i % 2 == 0 else store_b
            ).claim_next_job(f"worker-{i}", lease_seconds=180)
            for i in range(20)
        ],
        return_exceptions=True,
    )
    winners = [c for c in claims if c is not None and not isinstance(c, Exception)]
    failures = [c for c in claims if isinstance(c, Exception)]
    assert failures == []
    assert len(winners) == 10
    assert {w["job_id"] for w in winners} == job_ids
    # Every job claimed exactly once: attempt_count == 1 per job.
    for job_id in job_ids:
        job = await store_a.get_job(job_id)
        assert job["status"] == "running"
        assert job["attempt_count"] == 1
    assert _count(db_path, "evaluation_attempts") == 10


# ---------------------------------------------------------------------------
# commit_result_with_deliveries: one transaction, lease-token CAS
# ---------------------------------------------------------------------------


async def _claim_job(store: SQLiteEvaluationStore, job_id: str) -> dict:
    claimed = await store.claim_next_job("worker-a", lease_seconds=180)
    assert claimed is not None and claimed["job_id"] == job_id
    return claimed


async def test_commit_result_with_deliveries_single_transaction(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    sample_id = await store.save_sample(_sample_payload())
    job_id = await store.save_job(_job_payload(sample_id, idempotency_key="k1"))
    claimed = await _claim_job(store, job_id)

    deliveries = [
        {"sink": "langfuse", "projection_version": "v1", "payload": {"score": 0.87}},
        {"sink": "otel", "projection_version": "v1", "payload": {"score": 0.87}},
    ]
    result_id = await store.commit_result_with_deliveries(
        _auto_result_payload(job_id),
        deliveries,
        lease_token=claimed["lease_token"],
        job_terminal_status="succeeded",
    )
    assert result_id

    job = await store.get_job(job_id)
    assert job["status"] == "succeeded"
    assert job["lease_expiry"] is None

    result = await store.load_result(result_id)
    assert result["job_id"] == job_id

    delivery_rows = await store.list_deliveries(result_id)
    assert {d["sink"] for d in delivery_rows} == {"langfuse", "otel"}
    assert all(d["status"] == "pending" for d in delivery_rows)
    assert all(d["payload_hash"] for d in delivery_rows)
    assert _count(db_path, "evaluation_deliveries") == 2


async def test_commit_result_stale_token_rolls_back_everything(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    sample_id = await store.save_sample(_sample_payload())
    job_id = await store.save_job(_job_payload(sample_id, idempotency_key="k1"))
    claimed = await _claim_job(store, job_id)

    with pytest.raises(EvaluationLeaseError):
        await store.commit_result_with_deliveries(
            _auto_result_payload(job_id),
            [{"sink": "langfuse", "projection_version": "v1", "payload": {}}],
            lease_token="stale-token",
            job_terminal_status="succeeded",
        )

    # Nothing persisted: job still running with original lease, no result,
    # no delivery (single-transaction rollback).
    job = await store.get_job(job_id)
    assert job["status"] == "running"
    assert job["lease_token"] == claimed["lease_token"]
    assert _count(db_path, "evaluation_results") == 0
    assert _count(db_path, "evaluation_deliveries") == 0

    # The correct token still commits afterwards.
    result_id = await store.commit_result_with_deliveries(
        _auto_result_payload(job_id),
        [],
        lease_token=claimed["lease_token"],
        job_terminal_status="succeeded",
    )
    assert result_id


async def test_commit_result_unknown_job_not_found(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    with pytest.raises(EvaluationNotFoundError):
        await store.commit_result_with_deliveries(
            _auto_result_payload("missing-job"),
            [],
            lease_token="t",
            job_terminal_status="succeeded",
        )


# ---------------------------------------------------------------------------
# Attempts: append-only
# ---------------------------------------------------------------------------


async def test_attempts_append_only(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    attempt_id_1 = await store.append_attempt(
        {"job_id": "job-1", "attempt_index": 0, "judge_model": "gpt-x"}
    )
    attempt_id_2 = await store.append_attempt(
        {"job_id": "job-1", "attempt_index": 1}
    )
    assert attempt_id_1 != attempt_id_2

    # Same (job_id, attempt_index): no overwrite.
    with pytest.raises(EvaluationConflictError):
        await store.append_attempt({"job_id": "job-1", "attempt_index": 0})

    with sqlite3.connect(str(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE evaluation_attempts SET error_type = 'hacked'"
            )


# ---------------------------------------------------------------------------
# Deliveries: unique + state.py validated transitions
# ---------------------------------------------------------------------------


async def _store_with_committed_delivery(tmp_path) -> tuple[SQLiteEvaluationStore, str, str]:
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    sample_id = await store.save_sample(_sample_payload())
    job_id = await store.save_job(_job_payload(sample_id, idempotency_key="k1"))
    claimed = await _claim_job(store, job_id)
    result_id = await store.commit_result_with_deliveries(
        _auto_result_payload(job_id),
        [{"sink": "langfuse", "projection_version": "v1", "payload": {"score": 1}}],
        lease_token=claimed["lease_token"],
        job_terminal_status="succeeded",
    )
    deliveries = await store.list_deliveries(result_id)
    assert len(deliveries) == 1
    return store, db_path, deliveries[0]["delivery_id"]


async def test_deliveries_unique_key(tmp_path):
    store, db_path, delivery_id = await _store_with_committed_delivery(tmp_path)
    with sqlite3.connect(str(db_path)) as conn:
        row = conn.execute(
            "SELECT result_id, sink, projection_version FROM evaluation_deliveries "
            "WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO evaluation_deliveries (delivery_id, result_id, sink, "
                "projection_version, status, payload_hash, created_at, updated_at) "
                "VALUES ('dup-1', ?, ?, ?, 'pending', '', '2026-11-10T00:00:00+00:00', "
                "'2026-11-10T00:00:00+00:00')",
                row,
            )


async def test_delivery_transitions_validated_by_domain_state_machine(tmp_path):
    store, _, delivery_id = await _store_with_committed_delivery(tmp_path)

    updated = await store.record_delivery_transition(
        delivery_id, "accepted", external_id="ext-1"
    )
    assert updated["status"] == "accepted"
    assert updated["external_id"] == "ext-1"

    updated = await store.record_delivery_transition(delivery_id, "verified")
    assert updated["status"] == "verified"
    assert updated["attempt_count"] == 2

    # Terminal verified state cannot move again.
    with pytest.raises(IllegalEvaluationTransition):
        await store.record_delivery_transition(delivery_id, "pending")

    # Cannot skip accepted: pending -> verified is illegal.
    sample_id = await store.save_sample(_sample_payload(producer_event_id="evt-2"))
    job_id = await store.save_job(_job_payload(sample_id, idempotency_key="k2"))
    claimed = await store.claim_next_job("worker-a", lease_seconds=180)
    result_id = await store.commit_result_with_deliveries(
        _auto_result_payload(job_id, metric_name="other_metric"),
        [{"sink": "otel", "projection_version": "v1", "payload": {}}],
        lease_token=claimed["lease_token"],
        job_terminal_status="succeeded",
    )
    delivery = (await store.list_deliveries(result_id))[0]
    with pytest.raises(IllegalEvaluationTransition):
        await store.record_delivery_transition(delivery["delivery_id"], "verified")


# ---------------------------------------------------------------------------
# producer_state: high-watermark per (producer_id, counter_epoch)
# ---------------------------------------------------------------------------


async def test_producer_state_only_higher_sequence_accepted(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))

    await store.save_producer_state(
        {
            "producer_id": "n-kb-1",
            "counter_epoch": "epoch-1",
            "sequence": 10,
            "eligible_count": 100,
            "selected_count": 10,
            "captured_count": 10,
        }
    )
    state = await store.get_producer_state("n-kb-1", "epoch-1")
    assert state["sequence"] == 10

    with pytest.raises(EvaluationStaleSequenceError):
        await store.save_producer_state(
            {"producer_id": "n-kb-1", "counter_epoch": "epoch-1", "sequence": 5}
        )
    with pytest.raises(EvaluationStaleSequenceError):
        await store.save_producer_state(
            {"producer_id": "n-kb-1", "counter_epoch": "epoch-1", "sequence": 10}
        )

    await store.save_producer_state(
        {
            "producer_id": "n-kb-1",
            "counter_epoch": "epoch-1",
            "sequence": 11,
            "eligible_count": 110,
            "selected_count": 11,
            "captured_count": 11,
        }
    )
    state = await store.get_producer_state("n-kb-1", "epoch-1")
    assert state["sequence"] == 11
    assert state["eligible_count"] == 110
    assert _count(db_path, "evaluation_producer_state") == 1

    # Epoch change is accepted as a new row.
    await store.save_producer_state(
        {"producer_id": "n-kb-1", "counter_epoch": "epoch-2", "sequence": 1}
    )
    assert _count(db_path, "evaluation_producer_state") == 2

    with pytest.raises(EvaluationNotFoundError):
        await store.get_producer_state("n-kb-1", "epoch-3")


# ---------------------------------------------------------------------------
# Sink configs mapping
# ---------------------------------------------------------------------------


async def test_sink_configs_mapping_upsert(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    config = {
        "sink": "langfuse",
        "project": "n-agent",
        "definition_version": "def-answer-relevance@v1",
        "external_score_config_id": "sc-1",
        "schema_hash": "a" * 64,
    }
    await store.save_sink_config(config)
    loaded = await store.get_sink_config(
        "langfuse", "n-agent", "def-answer-relevance@v1"
    )
    assert loaded["external_score_config_id"] == "sc-1"

    updated = dict(config, external_score_config_id="sc-2")
    await store.save_sink_config(updated)
    loaded = await store.get_sink_config(
        "langfuse", "n-agent", "def-answer-relevance@v1"
    )
    assert loaded["external_score_config_id"] == "sc-2"

    with pytest.raises(EvaluationNotFoundError):
        await store.get_sink_config("langfuse", "other", "def-answer-relevance@v1")


# ---------------------------------------------------------------------------
# Feedback + current views (superseded / latest revision / revoked)
# ---------------------------------------------------------------------------


async def test_current_results_exclude_superseded_and_revoked(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    sample_id = await store.save_sample(_sample_payload())
    job_1 = await store.save_job(_job_payload(sample_id, idempotency_key="k1"))
    result_1 = await store.save_result(_auto_result_payload(job_1))

    current = await store.list_current_results(metric_name="answer_relevance")
    assert [r["result_id"] for r in current] == [result_1]

    # Re-evaluation supersedes: new result references the old one.
    job_2 = await store.save_job(_job_payload(sample_id, idempotency_key="k2"))
    result_2 = await store.save_result(
        _auto_result_payload(job_2, supersedes_result_id=result_1, typed_value=0.9)
    )

    current = await store.list_current_results(metric_name="answer_relevance")
    assert [r["result_id"] for r in current] == [result_2]
    # Immutable history retained.
    assert (await store.load_result(result_1))["typed_value"] == 0.87

    # Deletion revocation removes from current view via tombstone.
    await store.record_deletion_tombstone(
        {"subject_kind": "result", "subject_id": result_2, "reason": "session_deleted"}
    )
    current = await store.list_current_results(metric_name="answer_relevance")
    assert current == []
    assert (await store.load_result(result_2)) is not None


async def test_current_feedback_latest_revision_per_actor_target_definition(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))

    feedback_1 = await store.save_feedback(_feedback_payload())
    result_1 = await store.save_result(_human_result_payload(feedback_1))
    assert result_1

    current = await store.list_current_feedback()
    assert [f["feedback_id"] for f in current] == [feedback_1]
    assert current[0]["revision"] == 1

    # A revision from the same actor on the same target + definition supersedes.
    feedback_2 = await store.save_feedback(
        _feedback_payload(value={"typed_value": 0, "status": "value"})
    )
    assert feedback_2 != feedback_1
    current = await store.list_current_feedback()
    assert [f["feedback_id"] for f in current] == [feedback_2]
    assert current[0]["revision"] == 2

    # Different actor: independent current row.
    feedback_3 = await store.save_feedback(
        _feedback_payload(actor_ref="user-bob")
    )
    current = await store.list_current_feedback()
    assert {f["feedback_id"] for f in current} == {feedback_2, feedback_3}

    # Revoked (deleted) feedback leaves the current view.
    await store.record_deletion_tombstone(
        {
            "subject_kind": "feedback",
            "subject_id": feedback_2,
            "reason": "session_deleted",
        }
    )
    current = await store.list_current_feedback()
    assert [f["feedback_id"] for f in current] == [feedback_3]
    assert _count(db_path, "evaluation_feedback") == 3


async def test_human_result_roundtrip_links_feedback(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    feedback_id = await store.save_feedback(_feedback_payload())
    result_id = await store.save_result(_human_result_payload(feedback_id))
    loaded = await store.load_result(result_id)
    assert loaded["feedback_id"] == feedback_id
    assert loaded["job_id"] is None
    assert loaded["provenance"] == "human"


async def test_feedback_roundtrip_and_missing(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    feedback_id = await store.save_feedback(_feedback_payload())
    loaded = await store.get_feedback(feedback_id)
    assert loaded["actor_ref"] == "user-alice"
    assert loaded["target"]["subject_id"] == "turn-1"
    assert loaded["value"] == {"typed_value": 1, "status": "value"}
    with pytest.raises(EvaluationNotFoundError):
        await store.get_feedback("missing")


# ---------------------------------------------------------------------------
# Deletion tombstones (T18 prerequisite): idempotent
# ---------------------------------------------------------------------------


async def test_experiments_roundtrip(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    experiment_id = await store.save_experiment(
        {
            "experiment_id": "exp-1",
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "a" * 64,
            "target_config_hash": "b" * 64,
            "evaluator_set_hash": "c" * 64,
            "mode": "replay",
            "status": "running",
            "manifest_json": '{"cases": 3}',
        }
    )
    assert experiment_id == "exp-1"
    loaded = await store.get_experiment("exp-1")
    assert loaded["dataset_id"] == "ds-1"
    assert loaded["mode"] == "replay"
    # Idempotent repeat insert returns the same experiment without a dup row.
    await store.save_experiment(
        {
            "experiment_id": "exp-1",
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "a" * 64,
            "target_config_hash": "b" * 64,
            "evaluator_set_hash": "c" * 64,
            "status": "running",
        }
    )
    assert _count(tmp_path / "evaluations.db", "evaluation_experiments") == 1
    with pytest.raises(EvaluationNotFoundError):
        await store.get_experiment("missing")


async def test_feedback_revision_uniqueness_db_enforced(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    payload = _feedback_payload()

    await store.save_feedback(dict(payload, revision=1))

    # 同 actor/target/definition/revision 唯一（spec 10）：store 方法路径
    # surfaced as EvaluationConflictError（服务层映射 409）。
    with pytest.raises(EvaluationConflictError):
        await store.save_feedback(dict(payload, revision=1))

    # Raw SQL second insert with the same key must hit the UNIQUE index.
    with sqlite3.connect(str(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO evaluation_feedback (feedback_id, actor_ref, "
                "definition_id, definition_version, target_service, "
                "target_kind, target_subject_id, target_subject_revision, "
                "target_json, value_json, comment_ref, revision, "
                "supersedes_id, created_at) VALUES ('dup-1', 'user-alice', "
                "'def-answer-relevance', 'v1', 'n-agent', 'session_feedback', "
                "'turn-1', 'rev-1', '{}', '{}', NULL, 1, NULL, "
                "'2026-11-10T00:00:00+00:00')"
            )

    # Computed revision flow unaffected: next save without revision is 2.
    next_id = await store.save_feedback(dict(payload))
    loaded = await store.get_feedback(next_id)
    assert loaded["revision"] == 2


async def test_deletion_tombstones_idempotent(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))

    tombstone_id_1 = await store.record_deletion_tombstone(
        {
            "subject_kind": "session",
            "subject_id": "s-1",
            "subject_revision": "sr-1",
            "reason": "session_deleted",
        }
    )
    tombstone_id_2 = await store.record_deletion_tombstone(
        {
            "subject_kind": "session",
            "subject_id": "s-1",
            "subject_revision": "sr-1",
            "reason": "session_deleted",
        }
    )
    assert tombstone_id_1 == tombstone_id_2
    assert _count(db_path, "evaluation_deletion_tombstones") == 1

    assert await store.is_subject_deleted("session", "s-1", "sr-1") is True
    assert await store.is_subject_deleted("session", "s-1", "sr-2") is False
    assert await store.is_subject_deleted("session", "s-2") is False


# ---------------------------------------------------------------------------
# Deletion tasks (persistent, T18 consumer; minimal T3 scope)
# ---------------------------------------------------------------------------


def _deletion_task_payload(**overrides) -> dict:
    payload = {
        "sink": "langfuse",
        "subject_kind": "external_score",
        "subject_id": "ext-score-1",
        "external_id": "lf-score-1",
    }
    payload.update(overrides)
    return payload


async def test_deletion_tasks_insert_idempotent_and_list_by_status(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))

    task_id_1 = await store.save_deletion_task(_deletion_task_payload())
    task_id_2 = await store.save_deletion_task(_deletion_task_payload())
    assert task_id_1 == task_id_2
    assert _count(db_path, "evaluation_deletion_tasks") == 1

    # Different external_id: a distinct task.
    task_id_3 = await store.save_deletion_task(
        _deletion_task_payload(external_id="lf-score-2")
    )
    assert task_id_3 != task_id_1

    # NULL external_id (pre-registration): idempotent repeat via store.
    pre_1 = await store.save_deletion_task(
        _deletion_task_payload(external_id=None)
    )
    pre_2 = await store.save_deletion_task(
        _deletion_task_payload(external_id=None)
    )
    assert pre_1 == pre_2
    assert _count(db_path, "evaluation_deletion_tasks") == 3

    pending = await store.list_deletion_tasks(status="pending")
    assert {t["task_id"] for t in pending} == {task_id_1, task_id_3, pre_1}
    done = await store.list_deletion_tasks(status="done")
    assert done == []
    assert len(await store.list_deletion_tasks()) == 3


async def test_deletion_tasks_update_status_and_attempts(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    task_id = await store.save_deletion_task(_deletion_task_payload())

    updated = await store.update_deletion_task(
        task_id,
        status="deletion_pending",
        error_type="backend_unavailable",
        bump_attempt=True,
    )
    assert updated["status"] == "deletion_pending"
    assert updated["last_error_type"] == "backend_unavailable"
    assert updated["attempt_count"] == 1

    # Repeated update bumps again; None params keep current values.
    updated = await store.update_deletion_task(
        task_id, status="done", external_id=None, bump_attempt=True
    )
    # external_id was already set and COALESCE keeps it (None = keep).
    assert updated["external_id"] == "lf-score-1"
    assert updated["status"] == "done"
    assert updated["attempt_count"] == 2

    assert await store.list_deletion_tasks(status="deletion_pending") == []
    assert [t["task_id"] for t in await store.list_deletion_tasks(status="done")] == [
        task_id
    ]

    with pytest.raises(EvaluationNotFoundError):
        await store.update_deletion_task("missing", status="done")

    # Closed status vocabulary enforced by table CHECK.
    with pytest.raises(sqlite3.IntegrityError):
        await store.update_deletion_task(task_id, status="bogus")


# ---------------------------------------------------------------------------
# Quality-review fixes: concurrent init, job PK collision, evidence validation
# ---------------------------------------------------------------------------


async def test_concurrent_schema_init_all_succeed(tmp_path):
    db_path = tmp_path / "evaluations.db"
    stores = await asyncio.gather(
        *[
            asyncio.to_thread(SQLiteEvaluationStore, str(db_path))
            for _ in range(8)
        ]
    )
    assert len(stores) == 8
    assert _read_user_version(db_path) == SQLiteEvaluationStore.SCHEMA_VERSION
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT version FROM evaluation_schema_migrations"
        ).fetchall()
    assert [r[0] for r in rows] == [SQLiteEvaluationStore.SCHEMA_VERSION]


async def test_save_job_pk_collision_maps_to_conflict(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    sample_id = await store.save_sample(_sample_payload())
    job_id = await store.save_job(
        _job_payload(sample_id, idempotency_key="k1", job_id="job-fixed")
    )
    assert job_id == "job-fixed"

    # Same job_id, different idempotency key: PK collision -> store error,
    # not a raw sqlite3.IntegrityError leak.
    with pytest.raises(EvaluationConflictError):
        await store.save_job(
            _job_payload(sample_id, idempotency_key="k2", job_id="job-fixed")
        )
    assert _count(db_path, "evaluation_jobs") == 1


async def test_evidence_targets_missing_key_rejected_atomically(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    malformed = _sample_payload(
        evidence_targets=[
            {"subject_kind": "session", "subject_id": "s-1"},
        ]
    )
    with pytest.raises(EvaluationValidationError):
        await store.save_sample(malformed)
    # Atomic: no partial sample row without its evidence-target entries.
    assert _count(db_path, "evaluation_samples") == 0
    assert _count(db_path, "evaluation_evidence_targets") == 0


# ---------------------------------------------------------------------------
# T4 support: snapshot reference registry + queued-job revocation
# (minimal explicit extension consumed by LocalEvidenceStore)
# ---------------------------------------------------------------------------


async def test_list_samples_by_snapshot_ref(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    ref = "snapshots/" + "a" * 64 + ".json"
    first = await store.save_sample(_sample_payload(producer_event_id="e1",
                                                   snapshot_ref=ref))
    second = await store.save_sample(_sample_payload(producer_event_id="e2",
                                                     snapshot_ref=ref))
    await store.save_sample(_sample_payload(producer_event_id="e3",
                                            snapshot_ref="snapshots/" + "b" * 64 + ".json"))

    rows = await store.list_samples_by_snapshot_ref(ref)
    assert [r["sample_id"] for r in rows] == [first, second]
    for row in rows:
        # Same shape as load_sample: target parsed, internal hash dropped.
        assert row["snapshot_hash"] == _sample_payload()["snapshot_hash"]
        assert "payload_hash" not in row
        assert row["target"]["subject_id"] == "search-1"
    assert await store.list_samples_by_snapshot_ref("snapshots/missing") == []


async def test_clear_sample_snapshot_ref_cas(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    ref = "snapshots/" + "a" * 64 + ".json"
    sample_id = await store.save_sample(_sample_payload(snapshot_ref=ref))

    # CAS miss: wrong ref / unknown sample -> False, no change.
    assert await store.clear_sample_snapshot_ref(sample_id, "snapshots/x") is False
    assert await store.clear_sample_snapshot_ref("no-such", ref) is False
    assert (await store.load_sample(sample_id))["snapshot_ref"] == ref

    assert await store.clear_sample_snapshot_ref(sample_id, ref) is True
    cleared = await store.load_sample(sample_id)
    assert cleared["snapshot_ref"] == ""
    assert cleared["snapshot_hash"] == ""
    # Second clear: ref no longer matches -> False.
    assert await store.clear_sample_snapshot_ref(sample_id, ref) is False


async def test_revoke_queued_jobs_for_samples(tmp_path):
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    sample_id = await store.save_sample(_sample_payload())
    other_id = await store.save_sample(
        _sample_payload(producer_event_id="evt-other")
    )
    job_running = await store.save_job(
        _job_payload(sample_id, idempotency_key="k-running")
    )
    claimed = await store.claim_next_job("worker-1")
    assert claimed["job_id"] == job_running
    job_queued = await store.save_job(
        _job_payload(sample_id, idempotency_key="k-queued")
    )
    job_other = await store.save_job(
        _job_payload(other_id, idempotency_key="k-other")
    )

    revoked = await store.revoke_queued_jobs_for_samples([sample_id])
    assert revoked == [job_queued]
    assert (await store.get_job(job_queued))["status"] == "cancelled"
    # running / other-sample jobs untouched.
    assert (await store.get_job(job_running))["status"] == "running"
    assert (await store.get_job(job_other))["status"] == "queued"

    # Idempotent: nothing left to revoke.
    assert await store.revoke_queued_jobs_for_samples([sample_id]) == []


# ---------------------------------------------------------------------------
# T5 服务层支撑扩展：get_latest_feedback / get_result_id_by_feedback_id /
# query_results_page；以及 feedback value 假值保真（False/0 不得被吞成 {}）
# ---------------------------------------------------------------------------


async def test_feedback_value_falsy_roundtrip(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    feedback_id = await store.save_feedback(_feedback_payload(value=False))
    feedback = await store.get_feedback(feedback_id)
    assert feedback["value"] is False


async def test_get_latest_feedback_returns_highest_revision_or_none(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    assert (
        await store.get_latest_feedback(
            "user-alice",
            "n-agent",
            "session_feedback",
            "turn-1",
            "def-1",
            target_subject_revision="rev-1",
            definition_version="v1",
        )
        is None
    )
    first = await store.save_feedback(
        _feedback_payload(actor_ref="user-alice", definition_id="def-1")
    )
    second = await store.save_feedback(
        _feedback_payload(
            actor_ref="user-alice", definition_id="def-1", value={"v": 2}
        )
    )
    latest = await store.get_latest_feedback(
        "user-alice",
        "n-agent",
        "session_feedback",
        "turn-1",
        "def-1",
        target_subject_revision="rev-1",
        definition_version="v1",
    )
    assert latest is not None
    assert latest["feedback_id"] == second
    assert latest["revision"] == 2
    assert latest["feedback_id"] != first


async def test_get_result_id_by_feedback_id(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    feedback_id = await store.save_feedback(_feedback_payload())
    result_id = await store.save_result(
        _human_result_payload(feedback_id, typed_value=True)
    )
    assert await store.get_result_id_by_feedback_id(feedback_id) == result_id
    assert await store.get_result_id_by_feedback_id("missing") is None


async def test_query_results_page_pagination_filters_and_cursor(tmp_path):
    from app.domain.evaluation.models import EvaluationValidationError

    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    # 2 个 def-A 自动结果（一个挂 exp-1）、1 个 def-B 自动结果、1 个人评结果
    result_ids = []
    for index in range(3):
        sample_id = await store.save_sample(
            _sample_payload(
                producer_event_id=f"evt-{index}",
                target={
                    "service": "n-agent",
                    "kind": "agent_run",
                    "subject_id": f"run-{index}",
                    "subject_revision": "rev-1",
                },
            )
        )
        job_id = await store.save_job(
            _job_payload(
                sample_id,
                definition_id="def-A" if index < 2 else "def-B",
                definition_version="v1",
                idempotency_key=f"idem-{index}",
                experiment_id="exp-1" if index == 0 else None,
            )
        )
        result_ids.append(
            await store.save_result(
                _auto_result_payload(
                    job_id,
                    status="value" if index != 1 else "error",
                    typed_value=0.5 if index != 1 else None,
                )
            )
        )
    feedback_id = await store.save_feedback(
        _feedback_payload(
            actor_ref="user-alice", definition_id="def-feedback"
        )
    )
    human_result_id = await store.save_result(
        _human_result_payload(feedback_id, typed_value=True)
    )

    # keyset 分页：limit=2 走完全部 4 条，不重不漏；时间倒序（最新在前）
    collected = []
    cursor = None
    while True:
        rows, next_cursor = await store.query_results_page(limit=2, cursor=cursor)
        collected.extend(r["result_id"] for r in rows)
        if next_cursor is None:
            break
        cursor = next_cursor
    assert collected == [human_result_id] + result_ids[::-1]

    # 首页第一条即最新写入的结果
    first_page, _ = await store.query_results_page(limit=1)
    assert [r["result_id"] for r in first_page] == [human_result_id]

    # definition 过滤：automatic 经 jobs、human 经 feedback；同样倒序
    rows, _ = await store.query_results_page(definition_id="def-A")
    assert [r["result_id"] for r in rows] == result_ids[:2][::-1]
    rows, _ = await store.query_results_page(definition_id="def-feedback")
    assert [r["result_id"] for r in rows] == [human_result_id]

    # experiment / status 过滤
    rows, _ = await store.query_results_page(experiment_id="exp-1")
    assert [r["result_id"] for r in rows] == [result_ids[0]]
    rows, _ = await store.query_results_page(status="error")
    assert [r["result_id"] for r in rows] == [result_ids[1]]

    # target 过滤按 provenance 匹配：automatic 走 sample target，human 走 feedback
    rows, _ = await store.query_results_page(target_subject_id="run-2")
    assert [r["result_id"] for r in rows] == [result_ids[2]]
    rows, _ = await store.query_results_page(target_kind="session_feedback")
    assert [r["result_id"] for r in rows] == [human_result_id]
    rows, _ = await store.query_results_page(target_service="n-kb")
    assert rows == []

    # 非法游标拒绝
    with pytest.raises(EvaluationValidationError):
        await store.query_results_page(cursor="garbage")


async def test_query_results_page_desc_tie_breaks_by_result_id(
    tmp_path, monkeypatch
):
    """同 created_at 时按 result_id 倒序兜底，保证 keyset 排序稳定不重不漏。"""
    from app.infrastructure.evaluation import store as store_module

    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    fixed_now = "2026-11-14T08:30:00+00:00"
    monkeypatch.setattr(store_module, "_iso", lambda dt=None: fixed_now)

    result_ids = []
    for index, result_id in enumerate(("a" * 32, "z" * 32, "m" * 32)):
        sample_id = await store.save_sample(
            _sample_payload(producer_event_id=f"evt-tie-{index}")
        )
        job_id = await store.save_job(
            _job_payload(
                sample_id,
                idempotency_key=f"idem-tie-{index}",
            )
        )
        result_ids.append(
            await store.save_result(
                _auto_result_payload(job_id, result_id=result_id)
            )
        )

    rows, next_cursor = await store.query_results_page(limit=2)
    assert [r["result_id"] for r in rows] == ["z" * 32, "m" * 32]
    page2, next_cursor = await store.query_results_page(
        limit=2, cursor=next_cursor
    )
    assert [r["result_id"] for r in page2] == ["a" * 32]
    assert next_cursor is None


async def test_save_feedback_with_result_rolls_back_on_result_insert_failure(
    tmp_path, monkeypatch
):
    """故障注入：result 插入失败时单事务整体回滚——feedback 行必须缺席、
    不产生任何 result 行（spec 6.3 同事务语义，服务层双写窗口关闭的证据）。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    feedback_payload = _feedback_payload(revision=1)
    result_payload = {
        "provenance": "human",
        "metric_name": "answer_relevance",
        "status": "value",
        "typed_value": 0.5,
    }

    def _inject_failure(conn, result, now):
        raise RuntimeError("injected result-insert failure")

    with monkeypatch.context() as m:
        m.setattr(store, "_insert_result", _inject_failure)
        with pytest.raises(RuntimeError, match="injected result-insert failure"):
            await store.save_feedback_with_result(feedback_payload, result_payload)

    with sqlite3.connect(str(tmp_path / "evaluations.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evaluation_feedback").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evaluation_results").fetchone()[0] == 0

    # 回滚后存储可用：同 payload 正常提交成功，且确认没有留下半截状态
    feedback_id, result_id = await store.save_feedback_with_result(
        feedback_payload, result_payload
    )
    assert await store.get_result_id_by_feedback_id(feedback_id) == result_id


# ---------------------------------------------------------------------------
# T6 worker extensions: heartbeat / cancel / fail / call attempts / budget
# ---------------------------------------------------------------------------


class TestT6WorkerExtensions:
    async def test_heartbeat_renews_and_cas_rejects_stale_token(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        sample_id = await store.save_sample(_sample_payload())
        await store.save_job(_job_payload(sample_id, idempotency_key="hb-1"))
        claim = await store.claim_next_job("w1", 60, now=datetime(2026, 11, 11, tzinfo=timezone.utc))
        expiry_before = claim["lease_expiry"]

        assert await store.heartbeat_job(
            claim["job_id"], claim["lease_token"], lease_seconds=60,
            now=datetime(2026, 11, 11, 0, 2, tzinfo=timezone.utc),
        ) is True
        renewed = await store.get_job(claim["job_id"])
        assert renewed["lease_expiry"] > expiry_before

        # 旧 token（伪造）续租被 CAS 拒绝
        assert await store.heartbeat_job(
            claim["job_id"], "forged-token", lease_seconds=60
        ) is False
        # 终态 job 不可续租
        await store.fail_job(
            claim["job_id"], lease_token=claim["lease_token"], error_type="x"
        )
        assert await store.heartbeat_job(
            claim["job_id"], claim["lease_token"], lease_seconds=60
        ) is False

    async def test_cancel_job_cas_and_lease_clearing(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        sample_id = await store.save_sample(_sample_payload())
        await store.save_job(_job_payload(sample_id, idempotency_key="cx-1"))
        claim = await store.claim_next_job("w1", 60, now=datetime(2026, 11, 11, tzinfo=timezone.utc))

        assert await store.cancel_job(claim["job_id"]) is True
        job = await store.get_job(claim["job_id"])
        assert job["status"] == "cancelled"
        assert job["lease_token"] is None
        assert job["lease_expiry"] is None
        # 终态不可再取消
        assert await store.cancel_job(claim["job_id"]) is False
        with pytest.raises(EvaluationNotFoundError):
            await store.cancel_job("missing-job")

    async def test_fail_job_cas_on_latest_token(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        sample_id = await store.save_sample(_sample_payload())
        await store.save_job(_job_payload(sample_id, idempotency_key="fl-1"))
        start = datetime(2026, 11, 11, tzinfo=timezone.utc)
        first = await store.claim_next_job("w1", 30, now=start)
        # 租约过期后被重新领取：旧 token 无法置 error
        second = await store.claim_next_job(
            "w2", 30, now=start + timedelta(seconds=60)
        )
        assert await store.fail_job(
            first["job_id"], lease_token=first["lease_token"]
        ) is False
        assert await store.fail_job(
            second["job_id"], lease_token=second["lease_token"],
            error_type="deadline_exceeded",
        ) is True
        job = await store.get_job(first["job_id"])
        assert job["status"] == "error"
        assert job["error_type"] == "deadline_exceeded"
        assert job["lease_token"] is not None  # 保留最后持有者痕迹

    async def test_append_call_attempt_indexes_and_concurrency(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        sample_id = await store.save_sample(_sample_payload())
        await store.save_job(_job_payload(sample_id, idempotency_key="ca-1"))
        claim = await store.claim_next_job("w1", 60, now=datetime(2026, 11, 11, tzinfo=timezone.utc))
        job_id = claim["job_id"]

        async def call(i):
            return await store.append_call_attempt(
                job_id,
                judge_request={"call": i},
                judge_model="m",
                usage_known=True,
                total_tokens=10,
                started_at="2026-11-11T00:00:00+00:00",
                ended_at="2026-11-11T00:00:01+00:00",
                error_type=None if i == 0 else "network",
            )

        rows = await asyncio.gather(call(0), call(1))
        # 并发追加串行化：index 唯一且连续（gather 完成顺序不定，按内容断言）
        assert sorted(r["attempt_index"] for r in rows) == [1, 2]
        job = await store.get_job(job_id)
        assert job["attempt_count"] == 3  # claim marker + 2 次调用
        attempts = await store.list_attempts(job_id)
        assert [a["attempt_index"] for a in attempts] == [0, 1, 2]
        call_rows_by_index = {a["judge_request"].get("call"): a for a in attempts[1:]}
        assert call_rows_by_index[0]["error_type"] is None
        assert call_rows_by_index[1]["error_type"] == "network"
        assert all(a["usage_known"] == 1 for a in attempts[1:])
        # 已完成的调用行仍不可改写（append-only 触发器保持有效）
        with sqlite3.connect(str(tmp_path / "evaluations.db")) as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE evaluation_attempts SET error_type = 'hacked' "
                    "WHERE attempt_index = 1"
                )
        with pytest.raises(EvaluationNotFoundError):
            await store.append_call_attempt("missing-job", ended_at="x")

    async def test_budget_ledger_reserve_settle_and_exhausted(self, tmp_path):
        from decimal import Decimal

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        date_key = "2026-11-11"
        # 无行时全零快照（默认禁用预算即无行）
        totals = await store.get_daily_budget(date_key)
        assert totals["reserved_usd"] == "0" and totals["settled_calls"] == 0

        reserved = await store.reserve_daily_budget(
            date_key, usd=Decimal("6.00"), calls=3, max_usd=Decimal("10.00"),
            max_calls=3,
        )
        assert Decimal(reserved["reserved_usd"]) == Decimal("6.00")
        assert reserved["reserved_calls"] == 3

        with pytest.raises(EvaluationBudgetExhaustedError):
            await store.reserve_daily_budget(
                date_key, calls=1, max_calls=3
            )
        # 未配置上限的维度不判定
        await store.reserve_daily_budget(date_key, tokens=999999)

        settled = await store.settle_daily_budget(
            date_key, usd=Decimal("1.50"), calls=1,
            release_usd=Decimal("6.00"), release_calls=3,
        )
        assert Decimal(settled["reserved_usd"]) == 0
        assert Decimal(settled["settled_usd"]) == Decimal("1.50")
        assert settled["reserved_calls"] == 0
        assert settled["settled_calls"] == 1
        # release 超出预留按 0 封底
        over = await store.settle_daily_budget(date_key, release_usd=Decimal("99"))
        assert Decimal(over["reserved_usd"]) == 0


# ---------------------------------------------------------------------------
# T9 扩展：delivery claim/lease + 冻结 payload + 重试调度
# ---------------------------------------------------------------------------


def _create_v1_delivery_table(db_path: Path) -> None:
    """手工建 v1 形态的 evaluation_deliveries（无 payload_json/retry_count）。"""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE evaluation_deliveries ("
        "delivery_id TEXT PRIMARY KEY, result_id TEXT NOT NULL, "
        "sink TEXT NOT NULL, projection_version TEXT NOT NULL, "
        "external_id TEXT, status TEXT NOT NULL DEFAULT 'pending', "
        "attempt_count INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT, "
        "last_error_type TEXT, payload_hash TEXT NOT NULL DEFAULT '', "
        "lease_owner TEXT, lease_token TEXT, lease_expiry TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "UNIQUE (result_id, sink, projection_version))"
    )
    conn.commit()
    conn.close()


class TestDeliverySchedulingT9:
    async def _seed_delivery(self, store, tmp_path, *, result_id="res-1"):
        await store.save_definition(
            {
                "definition_id": "def-1",
                "version": "v1",
                "metric_name": "kb.recall_at_5",
                "target_kind": "retrieval",
                "method": "deterministic",
                "definition": {"definition_id": "def-1", "version": "v1"},
            }
        )
        sample_id = await store.save_sample(
            {
                "service": "n-agent",
                "producer_event_id": f"event-{result_id}",
                "target": {"service": "n-agent", "kind": "retrieval",
                           "subject_id": "s", "subject_revision": "r"},
            }
        )
        job_id = await store.save_job(
            {
                "sample_id": sample_id,
                "definition_id": "def-1",
                "definition_version": "v1",
            }
        )
        claim = await store.claim_next_job("seed", 300)
        result = {
            "job_id": job_id,
            "provenance": "automatic",
            "metric_name": "kb.recall_at_5",
            "metric_version": "v1",
            "status": "value",
            "typed_value": 0.8,
        }
        deliveries = [
            {"sink": "langfuse", "projection_version": "v1", "payload": {"a": 1}},
        ]
        return await store.commit_result_with_deliveries(
            result,
            deliveries,
            lease_token=claim["lease_token"],
            job_terminal_status="succeeded",
        )

    async def test_claim_next_delivery_lease_and_ordering(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        claim = await store.claim_next_delivery("worker-a", 300)
        assert claim is not None
        assert claim["status"] == "pending"
        assert claim["lease_owner"] == "worker-a"
        assert claim["lease_token"]
        # 租约未过期：其他 worker 领取不到
        assert await store.claim_next_delivery("worker-b", 300) is None

    async def test_claim_next_delivery_expired_lease_reclaimable(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        claim = await store.claim_next_delivery(
            "worker-a", 60, now=datetime.now(timezone.utc)
        )
        assert claim is not None
        # 模拟崩溃后租约过期：时钟前移可重领
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        reclaimed = await store.claim_next_delivery("worker-b", 60, now=later)
        assert reclaimed is not None
        assert reclaimed["delivery_id"] == claim["delivery_id"]
        assert reclaimed["lease_owner"] == "worker-b"

    async def test_claim_next_delivery_transitions_due_retry_wait(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        result_id = await self._seed_delivery(store, tmp_path)
        rows = await store.list_deliveries(result_id)
        delivery_id = rows[0]["delivery_id"]
        # 进入 retry_wait，next_retry_at 在过去
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        await store.schedule_delivery_retry(
            delivery_id,
            error_type="network",
            next_retry_at=past,
        )
        claim = await store.claim_next_delivery("worker-a", 300)
        assert claim is not None
        assert claim["delivery_id"] == delivery_id
        assert claim["status"] == "pending"
        assert claim["next_retry_at"] is None

    async def test_claim_next_delivery_ignores_future_retry_wait(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        result_id = await self._seed_delivery(store, tmp_path)
        rows = await store.list_deliveries(result_id)
        delivery_id = rows[0]["delivery_id"]
        future = datetime.now(timezone.utc) + timedelta(seconds=3600)
        await store.schedule_delivery_retry(
            delivery_id, error_type="network", next_retry_at=future
        )
        assert await store.claim_next_delivery("worker-a", 300) is None

    async def test_claim_next_delivery_sink_filter(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        assert await store.claim_next_delivery(
            "worker-a", 300, sinks=("otel",)
        ) is None
        claim = await store.claim_next_delivery(
            "worker-a", 300, sinks=("langfuse",)
        )
        assert claim is not None
        assert claim["sink"] == "langfuse"

    async def test_freeze_delivery_payload_idempotent_and_conflict(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        rows = await store.list_deliveries()
        delivery_id = rows[0]["delivery_id"]
        payload = {"id": "ext-1", "value": 0.8}
        frozen = await store.freeze_delivery_payload(
            delivery_id, external_id="ext-1", payload=payload
        )
        assert frozen["external_id"] == "ext-1"
        assert frozen["payload_hash"]
        # 幂等：同 ID 同 payload 再冻结返回原行
        again = await store.freeze_delivery_payload(
            delivery_id, external_id="ext-1", payload=payload
        )
        assert again["payload_hash"] == frozen["payload_hash"]
        # 冲突：同 delivery 不同 payload 拒绝（冻结不可变）
        from app.infrastructure.evaluation.store import EvaluationConflictError

        with pytest.raises(EvaluationConflictError):
            await store.freeze_delivery_payload(
                delivery_id, external_id="ext-1", payload={"id": "ext-1", "value": 0.9}
            )

    async def test_schedule_delivery_retry_increments_retry_count(self, tmp_path):
        from datetime import datetime, timedelta, timezone

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        rows = await store.list_deliveries()
        delivery_id = rows[0]["delivery_id"]
        future = datetime.now(timezone.utc) + timedelta(seconds=5)
        row = await store.schedule_delivery_retry(
            delivery_id, error_type="network", next_retry_at=future
        )
        assert row["status"] == "retry_wait"
        assert row["retry_count"] == 1
        assert row["last_error_type"] == "network"
        # 非法迁移经状态机拒绝（verified 终态不可重试）
        await store.record_delivery_transition(delivery_id, "pending")
        await store.record_delivery_transition(delivery_id, "accepted")
        await store.record_delivery_transition(delivery_id, "verified")
        with pytest.raises(IllegalEvaluationTransition):
            await store.schedule_delivery_retry(
                delivery_id, error_type="network", next_retry_at=future
            )

    async def test_schema_v2_migration_adds_delivery_columns(self, tmp_path):
        """v1 库（无 payload_json/retry_count 列）初始化时自动迁移。"""
        db_path = tmp_path / "legacy.db"
        _create_v1_delivery_table(db_path)
        store = SQLiteEvaluationStore(str(db_path))
        check_conn = sqlite3.connect(db_path)
        check_conn.row_factory = sqlite3.Row
        columns = {
            row["name"]
            for row in check_conn.execute(
                "PRAGMA table_info(evaluation_deliveries)"
            ).fetchall()
        }
        check_conn.close()
        assert {"payload_json", "retry_count"} <= columns
        # v1 库一次性迁移到当前版本（T9 v2 + T10 v3 索引 + T8 v4 重建）
        assert store.SCHEMA_VERSION == SQLiteEvaluationStore.SCHEMA_VERSION
        assert _read_user_version(db_path) == store.SCHEMA_VERSION

    async def test_concurrent_v2_migration_all_succeed(self, tmp_path):
        """并发首启 v1 -> v2 迁移不竞态：列检查与 ALTER 在 BEGIN
        IMMEDIATE 内串行化，并发构造 store 全部成功且列只加一次。"""
        import threading

        db_path = tmp_path / "legacy-concurrent.db"
        _create_v1_delivery_table(db_path)
        errors: list[Exception] = []

        def _init() -> None:
            try:
                SQLiteEvaluationStore(str(db_path))
            except Exception as exc:  # pragma: no cover - 失败时收集断言
                errors.append(exc)

        threads = [threading.Thread(target=_init) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        check_conn = sqlite3.connect(db_path)
        check_conn.row_factory = sqlite3.Row
        columns = [
            row["name"]
            for row in check_conn.execute(
                "PRAGMA table_info(evaluation_deliveries)"
            ).fetchall()
        ]
        versions = check_conn.execute(
            "SELECT COUNT(*) AS n FROM evaluation_schema_migrations"
        ).fetchone()["n"]
        check_conn.close()
        # 列只加一次，迁移日志只记当前版本一条（沿用既有单行 baseline 行为）
        assert columns.count("payload_json") == 1
        assert columns.count("retry_count") == 1
        assert versions == 1

    async def test_delivery_transition_lease_token_cas(self, tmp_path):
        """record_delivery_transition 携带 lease_token 时执行 CAS：陈旧
        worker（token 漂移/未持有租约）的迁移被拒绝，持票 worker 正常。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        rows = await store.list_deliveries()
        delivery_id = rows[0]["delivery_id"]
        claim = await store.claim_next_delivery("worker-a", 300)
        token = claim["lease_token"]
        # 无 token（管理员路径）不受 CAS 限制
        # 陈旧 token 被拒绝
        with pytest.raises(EvaluationConflictError):
            await store.record_delivery_transition(
                delivery_id, "accepted", lease_token="stale-token"
            )
        # 持票 worker 正常迁移
        row = await store.record_delivery_transition(
            delivery_id, "accepted", lease_token=token
        )
        assert row["status"] == "accepted"

    async def test_schedule_delivery_retry_lease_token_cas(self, tmp_path):
        """schedule_delivery_retry 携带 lease_token 时执行 CAS：token 漂移
        抛 EvaluationConflictError 且不累计 retry_count。"""
        from datetime import datetime, timedelta, timezone

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        rows = await store.list_deliveries()
        delivery_id = rows[0]["delivery_id"]
        claim = await store.claim_next_delivery("worker-a", 300)
        future = datetime.now(timezone.utc) + timedelta(seconds=5)
        with pytest.raises(EvaluationConflictError):
            await store.schedule_delivery_retry(
                delivery_id,
                error_type="network",
                next_retry_at=future,
                lease_token="stale-token",
            )
        row = await store.get_delivery(delivery_id)
        assert row["retry_count"] == 0  # CAS 拒绝：不落任何变更
        row = await store.schedule_delivery_retry(
            delivery_id,
            error_type="network",
            next_retry_at=future,
            lease_token=claim["lease_token"],
        )
        assert row["status"] == "retry_wait"
        assert row["retry_count"] == 1

    async def test_claim_next_delivery_recovers_expired_accepted(self, tmp_path):
        """accepted 且租约过期（回读窗口内崩溃）可重领：经状态机
        accepted -> retry_wait(即期) -> pending，retry_count 不计。"""
        from datetime import datetime, timedelta, timezone

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await self._seed_delivery(store, tmp_path)
        rows = await store.list_deliveries()
        delivery_id = rows[0]["delivery_id"]
        # 模拟发送后崩溃：领取（持有租约）-> accepted
        claim = await store.claim_next_delivery("worker-a", 60)
        assert claim is not None
        await store.record_delivery_transition(delivery_id, "accepted")
        # 租约未过期：其他 worker 不能重领 accepted 行
        assert await store.claim_next_delivery("worker-b", 60) is None
        # 时钟推进越过租约：重领成功并回到 pending（#11 起 accepted 过期
        # 重领仅对回读 sink 开放，须显式声明 reclaim_accepted_sinks）
        later = datetime.now(timezone.utc) + timedelta(seconds=120)
        reclaimed = await store.claim_next_delivery(
            "worker-b", 60, now=later, reclaim_accepted_sinks=("langfuse",)
        )
        assert reclaimed is not None
        assert reclaimed["delivery_id"] == delivery_id
        assert reclaimed["status"] == "pending"
        assert reclaimed["lease_owner"] == "worker-b"
        row = await store.get_delivery(delivery_id)
        assert row["status"] == "pending"
        assert row["last_error_type"] == "verify_pending"
        assert row["retry_count"] == 0  # 崩溃恢复不计网络失败


# ---------------------------------------------------------------------------
# T10 gauge snapshot read-only queries（评估遥测 Gauge 数据源）
# ---------------------------------------------------------------------------


def _window_sample_payload(**overrides) -> dict:
    """窗口快照测试样本：目标携带真实 trace 身份（unlinked=0）。"""
    payload = _sample_payload(
        service="n-kb",
        producer_event_id=f"evt-{overrides.get('producer_event_id', 'w1')}",
        target={
            "service": "n-kb",
            "kind": "retrieval",
            "subject_id": "retr-1",
            "subject_revision": "r1",
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
        },
    )
    payload.update(overrides)
    return payload


async def test_query_window_snapshot_grouped_aggregates(tmp_path):
    """窗口快照：按 service/definition/mode 分组聚合 succeeded/skipped/
    error/unlinked 与 numeric/boolean 统计；current 视图排除被取代与
    tombstone 撤销的结果；窗口外不计入。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    now = datetime.now(timezone.utc)
    outside = now - timedelta(hours=25)
    window_start = (now - timedelta(hours=24)).isoformat()

    # 分组 1：n-kb online（定义 v1）
    sample_id = await store.save_sample(_window_sample_payload())
    job_id = await store.save_job(_job_payload(sample_id, idempotency_key="j1"))
    await store.save_result(
        _auto_result_payload(job_id, result_id="r-num", metric_name="m1", typed_value=0.5,
                             evaluated_at=now.isoformat())
    )
    await store.save_result(
        _auto_result_payload(job_id, result_id="r-skip", metric_name="m2", status="skipped",
                             skip_reason="missing_reference", typed_value=None,
                             evaluated_at=now.isoformat())
    )
    await store.save_result(
        _auto_result_payload(job_id, result_id="r-bool", metric_name="m3",
                             typed_value=True, evaluated_at=now.isoformat())
    )
    # 被取代（current 视图排除）
    await store.save_result(
        _auto_result_payload(job_id, result_id="r-old", metric_name="m4", typed_value=0.1,
                             evaluated_at=now.isoformat())
    )
    await store.save_result(
        _auto_result_payload(job_id, result_id="r-new", metric_name="m5", typed_value=0.9,
                             supersedes_result_id="r-old",
                             evaluated_at=now.isoformat())
    )
    # 删除撤销（排除）
    await store.save_result(
        _auto_result_payload(job_id, result_id="r-tomb", metric_name="m6", typed_value=0.2,
                             evaluated_at=now.isoformat())
    )
    await store.record_deletion_tombstone(
        {"subject_kind": "result", "subject_id": "r-tomb"}
    )
    # 窗口外（不计入）
    await store.save_result(
        _auto_result_payload(job_id, result_id="r-outside", metric_name="m7", typed_value=0.3,
                             evaluated_at=outside.isoformat())
    )

    # 分组 2：n-kb online（定义 v2，独立分组）
    job_v2 = await store.save_job(
        _job_payload(sample_id, definition_version="v2", idempotency_key="j2")
    )
    await store.save_result(
        _auto_result_payload(job_v2, result_id="r-v2", typed_value=0.7,
                             evaluated_at=now.isoformat())
    )

    # 分组 3：n-agent 目标无 trace 身份（unlinked=1）
    unlinked_sample = await store.save_sample(
        _sample_payload(producer_event_id="evt-unlinked")
    )
    unlinked_job = await store.save_job(
        _job_payload(unlinked_sample, idempotency_key="j3")
    )
    await store.save_result(
        _auto_result_payload(unlinked_job, result_id="r-unlinked",
                             typed_value=0.4, evaluated_at=now.isoformat())
    )

    # 分组 4：error job（无 result 行，按 job 终态 updated_at 入窗）
    error_sample = await store.save_sample(
        _sample_payload(producer_event_id="evt-error")
    )
    await store.save_job(
        _job_payload(error_sample, idempotency_key="j4", status="error")
    )

    # 分组 5：experiment replay 模式
    experiment_id = "exp-1"
    await store.save_experiment(
        {
            "experiment_id": experiment_id,
            "dataset_id": "ds-1",
            "dataset_version": "v1",
            "dataset_hash": "e" * 64,
            "target_config_hash": "f" * 64,
            "evaluator_set_hash": "0" * 64,
            "mode": "replay",
            "status": "created",
        }
    )
    exp_sample = await store.save_sample(
        _window_sample_payload(producer_event_id="evt-exp")
    )
    exp_job = await store.save_job(
        _job_payload(exp_sample, idempotency_key="j5", experiment_id=experiment_id)
    )
    await store.save_result(
        _auto_result_payload(exp_job, result_id="r-exp", typed_value=1.0,
                             evaluated_at=now.isoformat())
    )

    rows = await store.query_window_snapshot(window_start=window_start)
    by_group = {
        (r["target_service"], r["definition_id"], r["definition_version"], r["mode"]): r
        for r in rows
    }

    g1 = by_group[("n-kb", "def-answer-relevance", "v1", "online")]
    # succeeded: r-num + r-bool + r-new（r-old 被取代、r-tomb 撤销、
    # r-outside 窗口外）
    assert g1["succeeded_count"] == 3
    assert g1["skipped_count"] == 1
    assert g1["error_count"] == 0
    assert g1["unlinked_count"] == 0
    assert g1["numeric_sum"] == pytest.approx(0.5 + 0.9)
    assert g1["numeric_count"] == 2
    assert g1["boolean_pass"] == 1
    assert g1["boolean_count"] == 1

    g2 = by_group[("n-kb", "def-answer-relevance", "v2", "online")]
    assert g2["succeeded_count"] == 1
    assert g2["numeric_sum"] == pytest.approx(0.7)

    g3 = by_group[("n-agent", "def-answer-relevance", "v1", "online")]
    assert g3["succeeded_count"] == 1
    assert g3["unlinked_count"] == 1

    g4 = by_group[("n-agent", "def-answer-relevance", "v1", "online")]
    # 分组 3 与分组 4 同 (service, definition, mode)：error 计入同组
    assert g4["error_count"] == 1

    g5 = by_group[("n-kb", "def-answer-relevance", "v1", "replay")]
    assert g5["succeeded_count"] == 1


async def test_query_window_snapshot_separates_sampling_cohort(tmp_path):
    """#24 cohort 分离：窗口快照按 sampling_cohort 独立分组——故障定向
    cohort 的偏置分布不与随机/无 cohort 样本混算（spec 7.1/AC17）。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(hours=24)).isoformat()
    for event, cohort in (
        ("evt-random", "random"),
        ("evt-failure", "failure"),
        ("evt-none", ""),
    ):
        sid = await store.save_sample(
            _window_sample_payload(
                producer_event_id=event, cohort=cohort
            )
        )
        jid = await store.save_job(
            _job_payload(sid, idempotency_key=f"j-{event}")
        )
        await store.save_result(
            _auto_result_payload(
                jid, result_id=f"r-{event}", typed_value=0.5,
                evaluated_at=now.isoformat(),
            )
        )

    rows = await store.query_window_snapshot(window_start=window_start)
    by_cohort = {
        r["sampling_cohort"]: r
        for r in rows
        if r["definition_id"] == "def-answer-relevance"
        and r["definition_version"] == "v1"
    }
    # 三个 cohort 各自成组，互不混算
    assert set(by_cohort) == {"random", "failure", ""}
    assert by_cohort["random"]["succeeded_count"] == 1
    assert by_cohort["failure"]["succeeded_count"] == 1
    assert by_cohort[""]["succeeded_count"] == 1


async def test_query_window_snapshot_empty(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    assert await store.query_window_snapshot(
        window_start=datetime.now(timezone.utc).isoformat()
    ) == []


async def test_query_producer_snapshot_sums_across_producers(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await store.save_producer_state(
        {
            "producer_id": "p1",
            "counter_epoch": "epoch-1",
            "sequence": 1,
            "eligible_count": 10,
            "selected_count": 8,
            "captured_count": 7,
            "capture_error_count": 1,
            "queue_full_count": 0,
        }
    )
    await store.save_producer_state(
        {
            "producer_id": "p2",
            "counter_epoch": "epoch-1",
            "sequence": 2,
            "eligible_count": 5,
            "selected_count": 4,
            "captured_count": 3,
            "capture_error_count": 1,
            "queue_full_count": 2,
        }
    )
    snapshot = await store.query_producer_snapshot()
    assert snapshot == {
        "eligible": 15,
        "selected": 12,
        "captured": 10,
        "capture_error": 2,
        "queue_full": 2,
    }


def _index_names(db_path: str | Path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {r[0] for r in rows}


async def test_schema_v3_gauge_indexes_migration_idempotent(tmp_path):
    """T10 v2->v3 迁移：Gauge 窗口快照覆盖索引（results.evaluated_at /
    jobs.updated_at / tombstones(kind,id)）；旧库重开幂等重建且版本行
    恰一条；results 账本无界增长，刷新路径不得全表扫描。"""
    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    expected_indexes = {
        "idx_results_evaluated_at",
        "idx_jobs_updated_at",
        "idx_tombstones_kind_id",
    }
    assert expected_indexes <= _index_names(db_path)
    current_version = SQLiteEvaluationStore.SCHEMA_VERSION
    assert _read_user_version(db_path) == current_version

    # 模拟 v2 旧库：删掉 v3 索引并回退版本（迁移行同步移除）
    with sqlite3.connect(str(db_path)) as conn:
        for name in sorted(expected_indexes):
            conn.execute(f"DROP INDEX {name}")
        conn.execute(
            "DELETE FROM evaluation_schema_migrations WHERE version >= 3"
        )
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
    assert not (expected_indexes & _index_names(db_path))

    # 重开 store：索引幂等重建、版本推进到当前、迁移日志恰一条
    SQLiteEvaluationStore(str(db_path))
    assert expected_indexes <= _index_names(db_path)
    assert _read_user_version(db_path) == current_version
    with sqlite3.connect(str(db_path)) as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM evaluation_schema_migrations ORDER BY version"
            ).fetchall()
        ]
    # 迁移日志沿用单行 baseline 行为：只记初始化时的当前版本
    assert versions == [current_version]


async def test_query_window_snapshot_accepts_non_utc_datetime(tmp_path):
    """窗口起点接受 datetime（含非 UTC 时区），store 以 _iso 归一化为
    UTC ISO 字符串——调用方不得自拼本地时区字符串。"""
    from datetime import timedelta as _td, timezone as _tz

    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    now = datetime.now(_tz.utc)
    rows = await store.query_window_snapshot(
        window_start=now.astimezone(_tz(_td(hours=8)))
    )
    assert rows == []
    # 等价的 UTC 字符串入参返回同一结果（字典序窗口边界一致）
    rows_str = await store.query_window_snapshot(
        window_start=(now - _td(hours=24)).isoformat()
    )
    assert rows_str == []


# ---------------------------------------------------------------------------
# T8: provenance 扩展（calibration / self_judge）+ explanation 列 + 校准报告
# ---------------------------------------------------------------------------

# v3 时代的 evaluation_results 建表 SQL（迁移测试夹具：与 v4 前的
# SCHEMA_SQL 逐字一致，provenance 仅 automatic/human、无 explanation 列）
_V3_RESULTS_TABLE_SQL = """
CREATE TABLE evaluation_results (
    result_id TEXT PRIMARY KEY,
    job_id TEXT,
    feedback_id TEXT,
    provenance TEXT NOT NULL CHECK (provenance IN ('automatic', 'human')),
    metric_name TEXT NOT NULL,
    metric_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    skip_reason TEXT,
    typed_value_json TEXT,
    passed INTEGER,
    explanation_ref TEXT,
    evidence_hash TEXT NOT NULL DEFAULT '',
    evaluated_at TEXT NOT NULL,
    supersedes_result_id TEXT,
    created_at TEXT NOT NULL,
    CHECK (
        (job_id IS NOT NULL AND feedback_id IS NULL)
        OR (job_id IS NULL AND feedback_id IS NOT NULL)
    ),
    CHECK (
        (provenance = 'automatic' AND job_id IS NOT NULL)
        OR (provenance = 'human' AND feedback_id IS NOT NULL)
    )
)
"""


def _automatic_result_payload(**overrides) -> dict:
    payload = {
        "job_id": f"job-{uuid4().hex[:8]}",
        "provenance": "automatic",
        "metric_name": "agent.answer_correctness",
        "metric_version": "v1",
        "status": "value",
        "typed_value": 0.9,
        "evidence_hash": "e" * 64,
    }
    payload.update(overrides)
    return payload


def _result_row_count(db_path, provenance: str) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM evaluation_results WHERE provenance = ?",
            (provenance,),
        ).fetchone()[0]


async def test_schema_v4_migration_rebuilds_results_preserving_rows(tmp_path):
    """T8 v3->v4 迁移：v3 results 表（provenance 仅 automatic/human、无
    explanation 列）重开时幂等重建——旧行保留、新 provenance 值可写入、
    explanation 列存在、索引/触发器重立、版本推进且迁移行恰一条。"""
    db_path = tmp_path / "evaluations.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(_V3_RESULTS_TABLE_SQL)
        conn.execute(
            "INSERT INTO evaluation_results (result_id, job_id, provenance, "
            "metric_name, metric_version, status, typed_value_json, "
            "evidence_hash, evaluated_at, created_at) "
            "VALUES ('r-legacy', 'job-legacy', 'automatic', 'm', 'v1', "
            "'value', '0.9', 'e', '2026-11-10T00:00:00+00:00', "
            "'2026-11-10T00:00:00+00:00')"
        )
        conn.commit()

    store = SQLiteEvaluationStore(str(db_path))

    # T18 起 SCHEMA_VERSION=5（v4 -> v5 增列迁移与 v4 重建链式幂等）
    assert _read_user_version(db_path) == store.SCHEMA_VERSION
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        columns = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(evaluation_results)")
        }
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'evaluation_results'"
        ).fetchone()["sql"]
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'evaluation_results'"
            )
        }
    assert "explanation" in columns
    assert "'calibration'" in sql and "'self_judge'" in sql
    assert {"uq_results_job_metric", "uq_results_feedback",
            "idx_results_metric", "idx_results_evaluated_at",
            "idx_results_supersedes"} <= indexes
    triggers = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'evaluation_results'"
        )
    }
    assert "trg_evaluation_results_no_update" in triggers
    # 旧行保留
    legacy = await store.load_result("r-legacy")
    assert legacy["provenance"] == "automatic"
    assert legacy["typed_value"] == 0.9
    # 新 provenance 值可写入；重开幂等（再开一次不重复迁移）
    await store.save_result(
        _automatic_result_payload(provenance="calibration")
    )
    await store.save_result(
        _automatic_result_payload(job_id="job-x2", provenance="self_judge")
    )
    SQLiteEvaluationStore(str(db_path))
    assert _result_row_count(db_path, "calibration") == 1
    assert _result_row_count(db_path, "self_judge") == 1
    # append-only 触发器在重建后重立且生效：UPDATE 必须被 RAISE(ABORT) 拒绝
    rid = await store.save_result(
        _automatic_result_payload(job_id="job-x3", provenance="automatic")
    )
    with sqlite3.connect(str(db_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE evaluation_results SET status = 'value2' "
                "WHERE result_id = ?",
                (rid,),
            )


async def test_provenance_check_enforces_extended_value_set(tmp_path):
    """provenance 封闭集扩展后仍封闭：calibration/self_judge 仅允许
    job 侧自动结果，human 仅允许 feedback 侧，未知值拒绝。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    # calibration / self_judge 自动结果（job 侧）合法
    await store.save_result(_automatic_result_payload(provenance="calibration"))
    await store.save_result(_automatic_result_payload(provenance="self_judge"))
    # unknown provenance 拒绝
    with pytest.raises(sqlite3.IntegrityError):
        await store.save_result(
            _automatic_result_payload(provenance="bogus")
        )
    # calibration 结果不允许挂在 feedback 侧（job_id/feedback_id 互斥 CHECK）
    with pytest.raises(sqlite3.IntegrityError):
        await store.save_result(_automatic_result_payload(
            provenance="calibration", job_id=None, feedback_id="fb-1",
        ))


async def test_explanation_column_roundtrip(tmp_path):
    """结果行 explanation 正文持久化（worker 提交时限长 2000 UTF-8 字节）。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    await store.save_result(
        _automatic_result_payload(explanation="答案与参考一致")
    )
    rows = await store.list_current_results(limit=10)
    assert rows[0]["explanation"] == "答案与参考一致"


class TestCalibrationReportsT8:
    async def test_save_get_and_missing(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        report = {
            "cache_key": "k-1",
            "metric_name": "agent.answer_correctness",
            "definition_id": "agent.answer_correctness",
            "definition_hash": "a" * 64,
            "status": "passed",
            "failures": [],
            "consistency": {"agreement": 1.0, "kappa": 1.0},
            "repeatability": {"agreement": 1.0, "max_diff_mean": 0.0},
            "created_at": "2026-11-11T00:00:00+00:00",
        }
        assert await store.save_calibration_report(report) == "k-1"
        stored = await store.get_calibration_report("k-1")
        assert stored["status"] == "passed"
        assert stored["consistency"]["kappa"] == 1.0
        assert "repeatability" in stored
        with pytest.raises(EvaluationNotFoundError):
            await store.get_calibration_report("missing")

    async def test_same_key_overwrites_latest_wins(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        base = {
            "cache_key": "k-1",
            "metric_name": "m",
            "definition_id": "d",
            "definition_hash": "a" * 64,
            "status": "advisory",
            "failures": ["kappa_not_computable"],
            "created_at": "2026-11-11T00:00:00+00:00",
        }
        await store.save_calibration_report(base)
        await store.save_calibration_report({**base, "status": "passed"})
        assert (await store.get_calibration_report("k-1"))["status"] == "passed"

    async def test_find_latest_and_status(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        definition_id, definition_hash = "d", "a" * 64
        # 无报告 → advisory（未校准 / 缓存键失效同语义）
        assert (
            await store.calibration_status(definition_id, definition_hash)
            == "advisory"
        )
        await store.save_calibration_report({
            "cache_key": "k-1",
            "metric_name": "m",
            "definition_id": definition_id,
            "definition_hash": definition_hash,
            "status": "advisory",
            "failures": ["agreement_below_threshold"],
            "created_at": "2026-11-11T00:00:00+00:00",
        })
        assert (
            await store.calibration_status(definition_id, definition_hash)
            == "advisory"
        )
        await store.save_calibration_report({
            "cache_key": "k-2",
            "metric_name": "m",
            "definition_id": definition_id,
            "definition_hash": definition_hash,
            "status": "passed",
            "failures": [],
            "created_at": "2026-11-11T01:00:00+00:00",
        })
        latest = await store.get_latest_calibration_report(
            definition_id, definition_hash
        )
        assert latest["cache_key"] == "k-2"
        assert (
            await store.calibration_status(definition_id, definition_hash)
            == "passed"
        )
        # definition hash 变化（缓存键失效语义）→ advisory
        assert (
            await store.calibration_status(definition_id, "b" * 64)
            == "advisory"
        )


async def test_concurrent_v4_migration_all_succeed(tmp_path):
    """并发首启 v3 -> v4 迁移不竞态：results 表重建（CHECK 扩展 +
    explanation 列）在 BEGIN IMMEDIATE 内串行化，并发构造 store 全部
    成功、旧行保留恰一份、迁移日志只记当前版本一条。"""
    import threading

    db_path = tmp_path / "v3-concurrent.db"
    # 构造 v3 形态库：现版本建库后将 results 表降级为 v3 DDL（无
    # explanation 列、provenance 仅 automatic/human）并回插一行旧行
    store = SQLiteEvaluationStore(str(db_path))
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE evaluation_results")
        conn.execute(_V3_RESULTS_TABLE_SQL)
        conn.execute(
            "INSERT INTO evaluation_results (result_id, job_id, provenance, "
            "metric_name, metric_version, status, typed_value_json, "
            "evidence_hash, evaluated_at, created_at) "
            "VALUES ('r-legacy', 'job-legacy', 'automatic', 'm', 'v1', "
            "'value', '0.9', 'e', '2026-11-10T00:00:00+00:00', "
            "'2026-11-10T00:00:00+00:00')"
        )
        conn.commit()
    assert "explanation" not in {
        r[1] for r in sqlite3.connect(str(db_path)).execute(
            "PRAGMA table_info(evaluation_results)"
        )
    }

    errors: list[Exception] = []

    def _init() -> None:
        try:
            SQLiteEvaluationStore(str(db_path))
        except Exception as exc:  # pragma: no cover - 失败时收集断言
            errors.append(exc)

    threads = [threading.Thread(target=_init) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    check_conn = sqlite3.connect(str(db_path))
    check_conn.row_factory = sqlite3.Row
    columns = [
        row["name"]
        for row in check_conn.execute("PRAGMA table_info(evaluation_results)")
    ]
    sql = check_conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'evaluation_results'"
    ).fetchone()["sql"]
    legacy_rows = check_conn.execute(
        "SELECT COUNT(*) AS n FROM evaluation_results "
        "WHERE result_id = 'r-legacy'"
    ).fetchone()["n"]
    total_rows = check_conn.execute(
        "SELECT COUNT(*) AS n FROM evaluation_results"
    ).fetchone()["n"]
    versions = check_conn.execute(
        "SELECT COUNT(*) AS n FROM evaluation_schema_migrations"
    ).fetchone()["n"]
    check_conn.close()
    # explanation 列只加一次（表重建一次），CHECK 含新 provenance 值
    assert columns.count("explanation") == 1
    assert "'calibration'" in sql and "'self_judge'" in sql
    # 旧行保留恰一份，无重复拷贝
    assert legacy_rows == 1
    assert total_rows == 1
    # 迁移日志只记当前版本一条（沿用既有单行 baseline 行为）
    assert versions == 1
    assert _read_user_version(db_path) == SQLiteEvaluationStore.SCHEMA_VERSION


async def test_schema_v7_migration_adds_evaluation_trace_columns(tmp_path):
    """v6 -> v7 迁移：results 表（无 evaluation_trace_id/span_id 列）与
    samples 表（无 cohort 列）重开时幂等增列——旧行 NULL/空串、版本推进
    且迁移日志收敛到当前版本。"""
    import sqlite3

    db_path = tmp_path / "evaluations.db"
    store = SQLiteEvaluationStore(str(db_path))
    result_id = await store.save_result(
        _automatic_result_payload(job_id="job-v7")
    )
    sample_id = await store.save_sample(_sample_payload())
    assert result_id

    # 降级模拟 v6 库：删三列 + 回退版本与迁移日志
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "ALTER TABLE evaluation_results DROP COLUMN evaluation_trace_id"
        )
        conn.execute(
            "ALTER TABLE evaluation_results DROP COLUMN evaluation_span_id"
        )
        conn.execute(
            "ALTER TABLE evaluation_samples DROP COLUMN cohort"
        )
        conn.execute(
            "DELETE FROM evaluation_schema_migrations WHERE version >= 7"
        )
        conn.execute("PRAGMA user_version = 6")
        conn.commit()

    SQLiteEvaluationStore(str(db_path))

    assert _read_user_version(db_path) == SQLiteEvaluationStore.SCHEMA_VERSION
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        result_columns = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(evaluation_results)")
        }
        sample_columns = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(evaluation_samples)")
        }
        legacy = conn.execute(
            "SELECT evaluation_trace_id, evaluation_span_id "
            "FROM evaluation_results WHERE result_id = ?",
            (result_id,),
        ).fetchone()
        legacy_sample = conn.execute(
            "SELECT cohort FROM evaluation_samples WHERE sample_id = ?",
            (sample_id,),
        ).fetchone()
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM evaluation_schema_migrations "
                "ORDER BY version"
            )
        ]
    assert {"evaluation_trace_id", "evaluation_span_id"} <= result_columns
    assert "cohort" in sample_columns
    # 旧行评估身份为 NULL（投影语义诚实，不回退业务身份）、cohort 空串
    assert legacy["evaluation_trace_id"] is None
    assert legacy["evaluation_span_id"] is None
    assert legacy_sample["cohort"] == ""
    assert versions == [SQLiteEvaluationStore.SCHEMA_VERSION]


# ---------------------------------------------------------------------------
# Definition usage（online on/off 登记，schema 表 v8 预置——v8 版本行与
# feedback 完整线程索引迁移同事务提交，见 TestFeedbackFullThread）
# ---------------------------------------------------------------------------


class TestDefinitionUsageStore:

    async def test_new_db_has_usage_table_and_version_is_v8(self, tmp_path):
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        assert "evaluation_definition_usage" in _table_names(db_path)
        # v8 完整：版本常量、迁移日志、user_version 均为 8
        assert SQLiteEvaluationStore.SCHEMA_VERSION == 8
        assert _read_user_version(db_path) == 8
        with sqlite3.connect(str(db_path)) as conn:
            versions = [
                r[0]
                for r in conn.execute(
                    "SELECT version FROM evaluation_schema_migrations "
                    "ORDER BY version"
                ).fetchall()
            ]
            columns = {
                r[1]
                for r in conn.execute(
                    "PRAGMA table_info(evaluation_definition_usage)"
                ).fetchall()
            }
        assert versions == [8]
        assert columns == {
            "definition_id",
            "definition_version",
            "online_enabled",
            "revision",
            "updated_at",
            "updated_by",
        }

    async def test_v7_db_upgrade_creates_usage_table_and_preserves_data(
        self, tmp_path
    ):
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        definition_id = await store.save_definition(_definition_payload())
        sample_id = await store.save_sample(_sample_payload())

        # 模拟旧 v7 库（尚无 usage 表）：删表 + 版本停在 7
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("DROP TABLE evaluation_definition_usage")
            conn.execute("PRAGMA user_version = 7")
            conn.commit()

        SQLiteEvaluationStore(str(db_path))

        assert "evaluation_definition_usage" in _table_names(db_path)
        # 版本随 v8 迁移推进（usage 表重建 + feedback 线程索引迁移同事务）
        assert _read_user_version(db_path) == 8
        assert (await store.load_sample(sample_id))["sample_id"] == sample_id
        definition = await store.get_definition(definition_id, "v1")
        assert definition["definition_id"] == definition_id
        # usage 表为空：旧定义未自动获得 usage 行（backfill 是显式操作）
        assert (
            _count(db_path, "evaluation_definition_usage") == 0
        )

    async def test_get_definition_usage_missing_returns_none(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        assert (
            await store.get_definition_usage("def-missing", "v1") is None
        )

    async def test_upsert_definition_usage_create_update_and_cas(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await store.save_definition(_definition_payload())

        # 创建：expected_revision=0 表示无行，写入 revision=1
        created = await store.upsert_definition_usage(
            "def-answer-relevance",
            "v1",
            online_enabled=True,
            expected_revision=0,
            updated_by="admin-1",
        )
        assert created["definition_id"] == "def-answer-relevance"
        assert created["definition_version"] == "v1"
        assert created["online_enabled"] is True
        assert created["revision"] == 1
        assert created["updated_by"] == "admin-1"
        assert datetime.fromisoformat(created["updated_at"]).utcoffset() == (
            timedelta(0)
        )
        loaded = await store.get_definition_usage(
            "def-answer-relevance", "v1"
        )
        assert loaded == created

        # CAS 更新：revision 匹配则原子推进
        updated = await store.upsert_definition_usage(
            "def-answer-relevance",
            "v1",
            online_enabled=False,
            expected_revision=1,
            updated_by="admin-2",
        )
        assert updated["online_enabled"] is False
        assert updated["revision"] == 2
        assert updated["updated_by"] == "admin-2"
        assert updated["updated_at"] >= created["updated_at"]

        # CAS 不匹配：抛冲突，行内容不被改写
        with pytest.raises(EvaluationConflictError):
            await store.upsert_definition_usage(
                "def-answer-relevance",
                "v1",
                online_enabled=True,
                expected_revision=1,
                updated_by="admin-3",
            )
        after = await store.get_definition_usage(
            "def-answer-relevance", "v1"
        )
        assert after["online_enabled"] is False
        assert after["revision"] == 2
        assert after["updated_by"] == "admin-2"

    async def test_upsert_definition_usage_concurrent_one_wins_one_conflict(
        self, tmp_path
    ):
        import threading

        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await store.save_definition(_definition_payload())

        results: list = []
        errors: list = []
        lock = threading.Lock()

        def _upsert() -> None:
            try:
                row = asyncio.run(
                    store.upsert_definition_usage(
                        "def-answer-relevance",
                        "v1",
                        online_enabled=True,
                        expected_revision=0,
                        updated_by="racer",
                    )
                )
                with lock:
                    results.append(row)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_upsert) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # BEGIN IMMEDIATE 串行：一个创建成功，另一个见到新 revision 抛冲突
        assert len(results) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], EvaluationConflictError)
        assert results[0]["revision"] == 1
        row = await store.get_definition_usage("def-answer-relevance", "v1")
        assert row["revision"] == 1

    async def test_upsert_definition_usage_rejects_missing_definition(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        with pytest.raises(EvaluationNotFoundError):
            await store.upsert_definition_usage(
                "def-missing",
                "v1",
                online_enabled=True,
                expected_revision=0,
                updated_by="admin-1",
            )
        assert (
            _count(tmp_path / "evaluations.db", "evaluation_definition_usage")
            == 0
        )

    async def test_save_definition_with_usage_single_transaction(
        self, tmp_path
    ):
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        payload = _definition_payload()
        definition = await store.save_definition_with_usage(
            payload, {"updated_by": "registrar-1"}
        )
        assert definition["definition_id"] == "def-answer-relevance"
        assert definition["version"] == "v1"
        assert _count(db_path, "evaluation_definitions") == 1
        assert _count(db_path, "evaluation_definition_usage") == 1
        usage = await store.get_definition_usage(
            "def-answer-relevance", "v1"
        )
        # 注册即落 usage：默认停用（上线是显式 CAS 操作），revision=1
        assert usage["online_enabled"] is False
        assert usage["revision"] == 1
        assert usage["updated_by"] == "registrar-1"

    async def test_save_definition_with_usage_idempotent_keeps_usage_state(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        payload = _definition_payload()
        await store.save_definition_with_usage(
            payload, {"updated_by": "registrar-1"}
        )
        # 用户手动启用（revision 1 -> 2）
        await store.upsert_definition_usage(
            "def-answer-relevance",
            "v1",
            online_enabled=True,
            expected_revision=1,
            updated_by="admin-1",
        )

        # 幂等重注册：返回既有定义，停用/启用状态不被复活改写
        definition = await store.save_definition_with_usage(
            dict(payload), {"updated_by": "registrar-2"}
        )
        assert definition["definition_id"] == "def-answer-relevance"
        usage = await store.get_definition_usage(
            "def-answer-relevance", "v1"
        )
        assert usage["online_enabled"] is True
        assert usage["revision"] == 2
        assert usage["updated_by"] == "admin-1"

    async def test_save_definition_with_usage_conflict_on_different_content(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await store.save_definition_with_usage(
            _definition_payload(), {"updated_by": "registrar-1"}
        )
        conflicting = _definition_payload(timeout_seconds=120)
        with pytest.raises(EvaluationConflictError):
            await store.save_definition_with_usage(
                conflicting, {"updated_by": "registrar-2"}
            )
        # 冲突路径不产生半提交：usage 行未被改写
        usage = await store.get_definition_usage(
            "def-answer-relevance", "v1"
        )
        assert usage["revision"] == 1
        assert usage["updated_by"] == "registrar-1"

    async def test_backfill_definition_usage_skips_human_and_existing(
        self, tmp_path
    ):
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        # 确定性定义，已有 usage 行（注册默认停用，后经 CAS 仍保持停用）
        await store.save_definition_with_usage(
            _definition_payload(definition_id="def-auto-1", version="v1"),
            {"updated_by": "registrar-1"},
        )
        await store.upsert_definition_usage(
            "def-auto-1",
            "v1",
            online_enabled=False,
            expected_revision=1,
            updated_by="admin-1",
        )
        # human 定义：无 usage 行，backfill 跳过
        await store.save_definition(
            _definition_payload(
                definition_id="def-human", version="v1", method="human"
            )
        )

        inserted = await store.backfill_definition_usage(
            updated_by="backfill-actor"
        )

        # 本库全部确定性定义已有 usage 行、human 定义被跳过：零插入
        assert inserted == 0

        # 新增一个无 usage 行的确定性定义：backfill 恰插入一行
        await store.save_definition(
            _definition_payload(definition_id="def-auto-2", version="v1")
        )
        inserted = await store.backfill_definition_usage(
            updated_by="backfill-actor"
        )
        assert inserted == 1
        usage = await store.get_definition_usage("def-auto-2", "v1")
        assert usage["online_enabled"] is True
        assert usage["revision"] == 1
        assert usage["updated_by"] == "backfill-actor"

        # 幂等：二次调用零插入
        assert (
            await store.backfill_definition_usage(updated_by="backfill-actor")
            == 0
        )
        # 已有行（含停用行）未被触碰
        untouched = await store.get_definition_usage("def-auto-1", "v1")
        assert untouched["online_enabled"] is False
        assert untouched["revision"] == 2
        assert untouched["updated_by"] == "admin-1"
        # human 定义仍无 usage 行
        assert (
            await store.get_definition_usage("def-human", "v1") is None
        )

    async def test_list_definitions_with_usage_join(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await store.save_definition(
            _definition_payload(definition_id="def-a", version="v1")
        )
        await store.save_definition(
            _definition_payload(
                definition_id="def-b", version="v1", target_kind="session"
            )
        )
        await store.upsert_definition_usage(
            "def-a", "v1", online_enabled=True, expected_revision=0,
            updated_by="admin-1",
        )

        rows = await store.list_definitions_with_usage()
        assert [r["definition_id"] for r in rows] == ["def-a", "def-b"]
        assert rows[0]["usage"] == {
            "online_enabled": True,
            "revision": 1,
            "updated_at": rows[0]["usage"]["updated_at"],
            "updated_by": "admin-1",
        }
        assert isinstance(rows[0]["usage"]["online_enabled"], bool)
        # 无 usage 行的定义：usage 键为 None
        assert rows[1]["usage"] is None

        # target_kind 过滤
        filtered = await store.list_definitions_with_usage(
            target_kind="session"
        )
        assert [r["definition_id"] for r in filtered] == ["def-b"]
        assert filtered[0]["usage"] is None
        empty = await store.list_definitions_with_usage(target_kind="nope")
        assert empty == []


# ---------------------------------------------------------------------------
# v8 迁移：feedback 完整线程唯一索引（7 元组线程身份 + revision）与
# 查询按完整线程过滤/分组；supersedes 同线程写入门禁
# ---------------------------------------------------------------------------

# v8 线程唯一索引列序（7 元组线程身份 + revision）
_FEEDBACK_THREAD_INDEX_COLUMNS = (
    "actor_ref",
    "target_service",
    "target_kind",
    "target_subject_id",
    "target_subject_revision",
    "definition_id",
    "definition_version",
    "revision",
)

_STORE_LOGGER = "app.infrastructure.evaluation.store"


def _feedback_thread_index_info(
    db_path: str | Path,
) -> dict[str, tuple[bool, tuple[str, ...]]]:
    """feedback 表索引名 -> (是否唯一, 列序列)。"""
    with sqlite3.connect(str(db_path)) as conn:
        info: dict[str, tuple[bool, tuple[str, ...]]] = {}
        for row in conn.execute(
            "PRAGMA index_list(evaluation_feedback)"
        ).fetchall():
            # index_list 列：seq/name/unique/origin/partial
            name = row[1]
            columns = tuple(
                r[2]
                for r in conn.execute(
                    f"PRAGMA index_info('{name}')"
                ).fetchall()
            )
            info[name] = (bool(row[2]), columns)
        return info


# force_schema_v7 / drop_feedback_thread_indexes 见
# tests/infrastructure/evaluation/_degraded_helpers.py（与 annotation
# 测试共用，统一 str | Path 签名）


class TestFeedbackFullThread:
    """v8：feedback 线程身份补全（subject_revision + definition_version）。

    旧 uq_feedback_revision_key 是六元组（缺 target_subject_revision 与
    definition_version），同 subject 不同 revision / 同 definition 不同
    version 的评分互相占用 revision 槽位；v8 迁移为完整线程八列唯一索引，
    查询（latest/current/revision 自增）同步按完整线程过滤。"""

    async def test_migration_replaces_legacy_index_with_thread_unique_index(
        self, tmp_path
    ):
        db_path = tmp_path / "evaluations.db"
        SQLiteEvaluationStore(str(db_path))
        indexes = _feedback_thread_index_info(db_path)
        assert "uq_feedback_revision_key" not in indexes
        assert indexes["uq_feedback_thread_revision_key"] == (
            True,
            _FEEDBACK_THREAD_INDEX_COLUMNS,
        )

        # 旧库模拟：手工建旧六元组索引（空表可建），降回 v7 后重开触发迁移
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("DROP INDEX uq_feedback_thread_revision_key")
            conn.execute(
                "CREATE UNIQUE INDEX uq_feedback_revision_key "
                "ON evaluation_feedback(actor_ref, target_service, "
                "target_kind, target_subject_id, definition_id, revision)"
            )
            conn.commit()
        force_schema_v7(db_path)
        SQLiteEvaluationStore(str(db_path))

        indexes = _feedback_thread_index_info(db_path)
        assert "uq_feedback_revision_key" not in indexes
        assert indexes["uq_feedback_thread_revision_key"] == (
            True,
            _FEEDBACK_THREAD_INDEX_COLUMNS,
        )
        assert _read_user_version(db_path) == SQLiteEvaluationStore.SCHEMA_VERSION
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT version, name FROM evaluation_schema_migrations "
                "WHERE version = 8"
            ).fetchall()
        assert rows == [(8, "v8-definition-usage")]

    async def test_migration_degrades_to_nonunique_index_on_conflict_rows(
        self, tmp_path, caplog
    ):
        """旧库遗留同线程同 revision 两行时：迁移不失败，索引降级为同列
        集非唯一索引，并输出只含冲突计数的结构化 warning。"""
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        # 旧数据 fixture：唯一索引已拆除后手工插入同线程同 revision 两行
        #（仅 feedback_id 不同，模拟旧索引时代遗留的纯重复行）
        drop_feedback_thread_indexes(db_path)
        await store.save_feedback(
            _feedback_payload(feedback_id="fb-dup-a", revision=1)
        )
        await store.save_feedback(
            _feedback_payload(feedback_id="fb-dup-b", revision=1)
        )
        force_schema_v7(db_path)

        with caplog.at_level(logging.WARNING, logger=_STORE_LOGGER):
            SQLiteEvaluationStore(str(db_path))

        indexes = _feedback_thread_index_info(db_path)
        assert "uq_feedback_revision_key" not in indexes
        # 降级：同列集非唯一索引
        assert indexes["ix_feedback_thread_revision_key"] == (
            False,
            _FEEDBACK_THREAD_INDEX_COLUMNS,
        )
        # 迁移不失败：版本仍推进到 v8
        assert _read_user_version(db_path) == SQLiteEvaluationStore.SCHEMA_VERSION
        # 结构化诊断日志：只含冲突计数，不含 feedback 行内容
        records = [
            r
            for r in caplog.records
            if "ix_feedback_thread_revision_key" in r.getMessage()
        ]
        assert records, "降级迁移必须输出结构化 warning 日志"
        message = records[-1].getMessage()
        # duplicate_rows = 分组保留 1 行之外的多余行数（1 组 2 行 -> 1）
        assert "duplicate_rows=1" in message
        assert "fb-dup" not in message

    async def test_same_thread_cross_definition_version_same_revision_coexist(
        self, tmp_path
    ):
        """同线程跨 definition_version 的同 revision 两条反馈可并存（旧
        六元组索引下第二条唯一冲突）。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        fb_v1 = await store.save_feedback(
            _feedback_payload(definition_version="v1", revision=1)
        )
        fb_v2 = await store.save_feedback(
            _feedback_payload(definition_version="v2", revision=1)
        )
        assert fb_v1 != fb_v2

    async def test_revision_autoincrement_scopes_to_full_thread(self, tmp_path):
        """revision 缺省自增（MAX+1）按完整线程计算：跨 definition_version
        互不占位，同线程内正常递增。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        fb_v1 = await store.save_feedback(
            _feedback_payload(definition_version="v1")
        )
        assert (await store.get_feedback(fb_v1))["revision"] == 1
        # 不同 definition_version 同线程：revision 从 1 重新计
        fb_v2 = await store.save_feedback(
            _feedback_payload(definition_version="v2")
        )
        assert (await store.get_feedback(fb_v2))["revision"] == 1
        # 同 v1 线程续增：revision 2
        fb_v1b = await store.save_feedback(
            _feedback_payload(definition_version="v1")
        )
        assert (await store.get_feedback(fb_v1b))["revision"] == 2

    async def test_get_latest_feedback_filters_by_full_thread(self, tmp_path):
        """get_latest_feedback 按 7 元组线程过滤：同 subject 同 definition_id
        不同 version / 不同 subject_revision 互不命中。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        for value in (1, 2):
            await store.save_feedback(
                _feedback_payload(
                    definition_version="v1",
                    value={"typed_value": value, "status": "value"},
                )
            )
        await store.save_feedback(
            _feedback_payload(
                definition_version="v2",
                value={"typed_value": 3, "status": "value"},
            )
        )
        await store.save_feedback(
            _feedback_payload(
                target={
                    "service": "n-agent",
                    "kind": "session_feedback",
                    "subject_id": "turn-1",
                    "subject_revision": "rev-2",
                },
                value={"typed_value": 4, "status": "value"},
            )
        )
        # v1 线程最新 revision=2
        latest_v1 = await store.get_latest_feedback(
            "user-alice",
            "n-agent",
            "session_feedback",
            "turn-1",
            "def-answer-relevance",
            target_subject_revision="rev-1",
            definition_version="v1",
        )
        assert latest_v1 is not None
        assert latest_v1["revision"] == 2
        assert latest_v1["value"]["typed_value"] == 2
        # v2 线程最新 revision=1
        latest_v2 = await store.get_latest_feedback(
            "user-alice",
            "n-agent",
            "session_feedback",
            "turn-1",
            "def-answer-relevance",
            target_subject_revision="rev-1",
            definition_version="v2",
        )
        assert latest_v2 is not None
        assert latest_v2["revision"] == 1
        assert latest_v2["value"]["typed_value"] == 3
        # rev-2 线程互不命中
        latest_rev2 = await store.get_latest_feedback(
            "user-alice",
            "n-agent",
            "session_feedback",
            "turn-1",
            "def-answer-relevance",
            target_subject_revision="rev-2",
            definition_version="v1",
        )
        assert latest_rev2 is not None
        assert latest_rev2["value"]["typed_value"] == 4

    async def test_get_latest_feedback_requires_thread_identity(self, tmp_path):
        """线程身份参数必填（keyword-only），禁止静默回退宽线程查询。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        with pytest.raises(TypeError):
            await store.get_latest_feedback(
                "user-alice",
                "n-agent",
                "session_feedback",
                "turn-1",
                "def-answer-relevance",
            )

    async def test_list_current_feedback_groups_by_full_thread(self, tmp_path):
        """list_current_feedback GROUP BY 完整线程：同线程跨 definition_version
        各自出现 latest，互不隐藏。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await store.save_feedback(
            _feedback_payload(
                definition_version="v1",
                value={"typed_value": 1, "status": "value"},
            )
        )
        await store.save_feedback(
            _feedback_payload(
                definition_version="v1",
                value={"typed_value": 2, "status": "value"},
            )
        )
        await store.save_feedback(
            _feedback_payload(
                definition_version="v2",
                value={"typed_value": 3, "status": "value"},
            )
        )
        rows = await store.list_current_feedback()
        by_version = {r["definition_version"]: r for r in rows}
        assert set(by_version) == {"v1", "v2"}
        assert by_version["v1"]["revision"] == 2
        assert by_version["v1"]["value"]["typed_value"] == 2
        assert by_version["v2"]["revision"] == 1
        assert by_version["v2"]["value"]["typed_value"] == 3

    async def test_list_current_feedback_dedupes_same_thread_same_revision(
        self, tmp_path, caplog
    ):
        """降级索引下同线程同 revision 两行并存：current 视图按
        created_at/feedback_id 决定性择一，且不因旧行隐藏另一线程评分；
        降级索引存在时输出诊断计数日志。"""
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        drop_feedback_thread_indexes(db_path)
        await store.save_feedback(
            _feedback_payload(feedback_id="fb-dup-a", revision=1)
        )
        await store.save_feedback(
            _feedback_payload(feedback_id="fb-dup-b", revision=1)
        )
        # 另一线程（不同 definition_version）评分不被重复行隐藏
        fb_other = await store.save_feedback(
            _feedback_payload(definition_version="v2", revision=1)
        )
        # 重开 store：迁移降级为非唯一索引
        SQLiteEvaluationStore(str(db_path))

        with caplog.at_level(logging.WARNING, logger=_STORE_LOGGER):
            rows = await store.list_current_feedback()

        ids = [r["feedback_id"] for r in rows]
        # 同线程同 revision 只保留一行（created_at DESC, feedback_id DESC）
        assert len([i for i in ids if i in ("fb-dup-a", "fb-dup-b")]) == 1
        assert fb_other in ids
        # 诊断计数日志：只含计数，不含 feedback 内容
        records = [
            r
            for r in caplog.records
            if "ix_feedback_thread_revision_key" in r.getMessage()
        ]
        assert records, "降级索引存在时 list_current 必须输出诊断计数日志"
        message = records[-1].getMessage()
        assert "duplicate_rows=1" in message
        assert "fb-dup" not in message

    async def test_cross_thread_supersedes_rejected(self, tmp_path):
        """supersedes 写入约束：跨线程取代拒绝（EvaluationConflictError），
        同线程取代正常写入。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        other_thread = await store.save_feedback(
            _feedback_payload(definition_version="v2", revision=1)
        )
        with pytest.raises(EvaluationConflictError):
            await store.save_feedback(
                _feedback_payload(supersedes_id=other_thread, revision=2)
            )
        # 同线程 supersedes 允许
        first = await store.save_feedback(
            _feedback_payload(definition_version="v1", revision=1)
        )
        second = await store.save_feedback(
            _feedback_payload(
                definition_version="v1", supersedes_id=first, revision=2
            )
        )
        assert second != first

    async def test_save_feedback_with_result_rejects_cross_thread_supersedes(
        self, tmp_path
    ):
        """单事务路径同样执行 supersedes 同线程校验，冲突时整体回滚。"""
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        other_thread = await store.save_feedback(
            _feedback_payload(definition_version="v2", revision=1)
        )
        with pytest.raises(EvaluationConflictError):
            await store.save_feedback_with_result(
                _feedback_payload(supersedes_id=other_thread, revision=2),
                _human_result_payload("placeholder"),
            )
        # 无半提交：feedback 未落库
        assert _count(db_path, "evaluation_feedback") == 1

    async def test_expected_revision_cas_reevaluated_inside_write_transaction(
        self, tmp_path
    ):
        """事务内 CAS 复查（expected_revision 参数）：预读后另一连接已插入
        更高 revision 时，携带过期 expected_revision 的提交在 BEGIN
        IMMEDIATE 写事务内被拒——不依赖唯一索引冲突路径（唯一索引库上
        revision 槽位未被占用时同样必须拒绝）。"""
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        await store.save_feedback(_feedback_payload(revision=1))
        # 预读竞态窗口：写事务前另一连接已插入 revision=2
        await store.save_feedback(
            _feedback_payload(feedback_id="fb-race", revision=2)
        )
        with pytest.raises(EvaluationConflictError):
            await store.save_feedback_with_result(
                _feedback_payload(feedback_id="fb-late", revision=3),
                _human_result_payload("fb-late"),
                expected_revision=1,
            )
        # 拒绝后无残留：revision>=3 未产生新行
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM evaluation_feedback "
                "WHERE actor_ref = 'user-alice' AND revision >= 3"
            ).fetchone()[0]
        assert rows == 0

    async def test_expected_revision_none_keeps_max_plus_one_and_explicit_paths(
        self, tmp_path
    ):
        """expected_revision 缺省（None）保持现状：显式 revision 直插
        （submit_feedback 既有显式 CAS 路径语义）与 save_feedback 直接
        调用方的 MAX+1 回退均不受影响。"""
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        feedback_id, result_id = await store.save_feedback_with_result(
            _feedback_payload(revision=1), _human_result_payload("placeholder")
        )
        assert (await store.get_feedback(feedback_id))["revision"] == 1
        explicit_id, _ = await store.save_feedback_with_result(
            _feedback_payload(feedback_id="fb-explicit", revision=2),
            _human_result_payload("fb-explicit"),
        )
        assert (await store.get_feedback(explicit_id))["revision"] == 2
        # MAX+1 回退（_insert_feedback 直接调用方路径，save_feedback 透传）
        auto_id = await store.save_feedback(
            _feedback_payload(feedback_id="fb-auto")
        )
        assert (await store.get_feedback(auto_id))["revision"] == 3

    async def test_expected_revision_cas_blocks_concurrent_duplicate_on_degraded_index(
        self, tmp_path
    ):
        """降级非唯一索引库（v8 迁移降级分支遗留状态）上并发同 revision
        提交：BEGIN IMMEDIATE 事务内 expected == MAX(revision) 复查统一
        生效，仅一成功且不产生同线程同 revision 重复行（spec 4.4 / plan
        Task 6 S4 硬性要求：降级库禁止重复 revision——唯一索引不在，
        降级索引本身无法兜底）。"""
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        # 构造降级库：拆线程索引 -> 插同线程同 revision 历史冲突行 ->
        # 降回 v7 -> 重开触发 v8 迁移降级为非唯一索引
        drop_feedback_thread_indexes(db_path)
        await store.save_feedback(
            _feedback_payload(feedback_id="fb-dup-a", revision=1)
        )
        await store.save_feedback(
            _feedback_payload(feedback_id="fb-dup-b", revision=1)
        )
        force_schema_v7(db_path)
        store = SQLiteEvaluationStore(str(db_path))
        indexes = _feedback_thread_index_info(db_path)
        assert indexes["ix_feedback_thread_revision_key"] == (
            False,
            _FEEDBACK_THREAD_INDEX_COLUMNS,
        )

        # 两个并发提交方都基于旧预读（MAX=1）携带 expected_revision=1，
        # 各自准备 revision=2 新行（仅 feedback_id 不同）
        outcomes = await asyncio.gather(
            store.save_feedback_with_result(
                _feedback_payload(feedback_id="fb-cas-w1", revision=2),
                _human_result_payload("fb-cas-w1"),
                expected_revision=1,
            ),
            store.save_feedback_with_result(
                _feedback_payload(feedback_id="fb-cas-w2", revision=2),
                _human_result_payload("fb-cas-w2"),
                expected_revision=1,
            ),
            return_exceptions=True,
        )
        errors = [o for o in outcomes if isinstance(o, Exception)]
        succeeded = [o for o in outcomes if not isinstance(o, Exception)]
        assert len(succeeded) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], EvaluationConflictError)
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute(
                "SELECT COUNT(*) FROM evaluation_feedback "
                "WHERE actor_ref = 'user-alice' AND target_subject_id = 'turn-1' "
                "AND revision = 2"
            ).fetchone()[0]
        assert rows == 1, "降级库上并发同 revision 提交禁止产生重复行"


# ---------------------------------------------------------------------------
# Task 4: 结果查询扩展（created_from/created_to/definition_version/provenance
# + snapshot_at 上界 + 游标 v2）与聚合/标注队列查询
# ---------------------------------------------------------------------------


@contextmanager
def _fixed_store_now(value: str):
    """临时固定 store 的 _iso()（UTC ISO 字符串），精确控制 created_at。"""
    real = _evaluation_store_module._iso
    _evaluation_store_module._iso = lambda dt=None: value
    try:
        yield
    finally:
        _evaluation_store_module._iso = real


_DEFAULT_TARGET = {
    "service": "n-agent",
    "kind": "agent_run",
    "subject_id": "run-1",
    "subject_revision": "rev-1",
}


async def _seed_auto_result(
    store,
    *,
    definition_id="def-A",
    definition_version="v1",
    metric_name="answer_relevance",
    output_type="number",
    target=None,
    status="value",
    typed_value=0.87,
    provenance="automatic",
    experiment_id=None,
    supersedes_result_id=None,
) -> str:
    """种子一条自动结果（definition + sample + job + result），返回 result_id。"""
    await store.save_definition(
        _definition_payload(
            definition_id=definition_id,
            version=definition_version,
            metric_name=metric_name,
            output_type=output_type,
        )
    )
    sample_id = await store.save_sample(
        _sample_payload(
            producer_event_id=uuid4().hex,
            target=target or _DEFAULT_TARGET,
        )
    )
    job_id = await store.save_job(
        _job_payload(
            sample_id,
            definition_id=definition_id,
            definition_version=definition_version,
            idempotency_key=uuid4().hex,
            experiment_id=experiment_id,
        )
    )
    return await store.save_result(
        _auto_result_payload(
            job_id,
            provenance=provenance,
            metric_name=metric_name,
            metric_version=definition_version,
            status=status,
            typed_value=typed_value,
            supersedes_result_id=supersedes_result_id,
        )
    )


async def _seed_human_result(
    store,
    *,
    actor_ref="user-alice",
    definition_id="def-H",
    definition_version="v1",
    metric_name="answer_relevance",
    output_type="number",
    target=None,
    typed_value=0.5,
) -> str:
    """种子一条人评结果（definition + feedback + human result）。"""
    await store.save_definition(
        _definition_payload(
            definition_id=definition_id,
            version=definition_version,
            metric_name=metric_name,
            output_type=output_type,
        )
    )
    feedback_id = await store.save_feedback(
        _feedback_payload(
            actor_ref=actor_ref,
            definition_id=definition_id,
            definition_version=definition_version,
            target=target
            or {
                "service": "n-agent",
                "kind": "session_feedback",
                "subject_id": "turn-1",
                "subject_revision": "rev-1",
            },
            value={"typed_value": typed_value, "status": "value"},
        )
    )
    return await store.save_result(
        _human_result_payload(
            feedback_id,
            metric_name=metric_name,
            metric_version=definition_version,
            typed_value=typed_value,
        )
    )


class TestResultsQueryExtended:
    """query_results_page 扩展过滤 + snapshot_at + 游标 v2。"""

    async def test_extended_filters_orthogonal_combination(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        r_v1 = await _seed_auto_result(store, definition_id="def-A",
                                       definition_version="v1")
        r_v2 = await _seed_auto_result(store, definition_id="def-A",
                                       definition_version="v2")
        r_hum = await _seed_human_result(store, definition_id="def-H",
                                         definition_version="v1")
        r_cal = await _seed_auto_result(store, definition_id="def-C",
                                        definition_version="v2",
                                        provenance="calibration")

        # definition_version 双侧匹配（automatic 经 jobs、human 经 feedback）
        rows, _ = await store.query_results_page(definition_version="v1")
        assert {r["result_id"] for r in rows} == {r_v1, r_hum}
        rows, _ = await store.query_results_page(definition_version="v2")
        assert {r["result_id"] for r in rows} == {r_v2, r_cal}

        # provenance 过滤（封闭集：automatic/human/calibration/self_judge）
        rows, _ = await store.query_results_page(provenance="calibration")
        assert [r["result_id"] for r in rows] == [r_cal]
        rows, _ = await store.query_results_page(provenance="human")
        assert [r["result_id"] for r in rows] == [r_hum]

        # 与既有过滤正交组合
        rows, _ = await store.query_results_page(
            definition_version="v1", provenance="human",
            target_kind="session_feedback",
        )
        assert [r["result_id"] for r in rows] == [r_hum]
        rows, _ = await store.query_results_page(
            definition_version="v2", provenance="automatic",
            target_kind="agent_run", status="value",
        )
        assert [r["result_id"] for r in rows] == [r_v2]
        rows, _ = await store.query_results_page(
            definition_version="v2", provenance="human"
        )
        assert rows == []

    async def test_created_range_half_open(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        with _fixed_store_now("2026-03-01T00:00:00+00:00"):
            r_old = await _seed_auto_result(store)
        with _fixed_store_now("2026-04-01T00:00:00+00:00"):
            r_mid = await _seed_auto_result(store)
        with _fixed_store_now("2026-06-01T00:00:00+00:00"):
            r_new = await _seed_auto_result(store)

        # 半开区间 [from, to)：from 含端点、to 排端点
        rows, _ = await store.query_results_page(
            created_from="2026-04-01T00:00:00+00:00"
        )
        assert {r["result_id"] for r in rows} == {r_mid, r_new}
        rows, _ = await store.query_results_page(
            created_to="2026-04-01T00:00:00+00:00"
        )
        assert [r["result_id"] for r in rows] == [r_old]
        rows, _ = await store.query_results_page(
            created_from="2026-04-01T00:00:00+00:00",
            created_to="2026-04-01T00:00:00+00:00",
        )
        assert rows == []
        rows, _ = await store.query_results_page(
            created_from="2026-03-15T00:00:00+00:00",
            created_to="2026-05-15T00:00:00+00:00",
        )
        assert [r["result_id"] for r in rows] == [r_mid]

    async def test_new_filters_emit_v2_cursor_and_paginate(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        seed_ids = [
            await _seed_auto_result(store, definition_id="def-A")
            for _ in range(3)
        ]
        rows, token = await store.query_results_page(
            definition_version="v1", limit=2
        )
        assert len(rows) == 2
        assert token is not None and "|" not in token  # v2 不透明 token
        rows2, token2 = await store.query_results_page(
            definition_version="v1", limit=2, cursor=token
        )
        assert token2 is None
        assert len(rows + rows2) == len(set(seed_ids))
        assert {r["result_id"] for r in rows + rows2} == set(seed_ids)

        # 过滤变更后携带旧游标 → fingerprint 不匹配拒绝
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page(
                definition_version="v2", cursor=token
            )

    async def test_snapshot_pagination_stable_across_interleaved_inserts(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        with _fixed_store_now("2026-01-01T00:00:00+00:00"):
            seed_ids = [
                await _seed_auto_result(store, definition_id="def-A")
                for _ in range(5)
            ]
        rows, next_cursor, snap = await store.query_results_page_snapshot(
            limit=2
        )
        assert len(rows) == 2 and next_cursor is not None
        assert snap is not None and snap.endswith("+00:00")
        # 翻页间插入新行（created_at 远晚于快照上界）
        with _fixed_store_now("2099-01-01T00:00:00+00:00"):
            late_id = await _seed_auto_result(store, definition_id="def-A")
        rows2, next_cursor2, snap2 = await store.query_results_page_snapshot(
            limit=2, cursor=next_cursor
        )
        assert snap2 == snap  # 后续页固定复用首次快照上界
        collected = [r["result_id"] for r in rows + rows2]
        rows3, next_cursor3, _ = await store.query_results_page_snapshot(
            limit=2, cursor=next_cursor2
        )
        collected += [r["result_id"] for r in rows3]
        assert next_cursor3 is None
        assert len(collected) == 5
        assert set(collected) == set(seed_ids)
        assert late_id not in collected  # 快照上界后的插入不漏进当前分页

    async def test_snapshot_at_param_pins_upper_bound(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        with _fixed_store_now("2026-01-01T00:00:00+00:00"):
            early_a = await _seed_auto_result(store)
            early_b = await _seed_auto_result(store)
        with _fixed_store_now("2026-06-01T00:00:00+00:00"):
            late_id = await _seed_auto_result(store)
        rows, next_cursor, snap = await store.query_results_page_snapshot(
            snapshot_at="2026-03-01T00:00:00+00:00"
        )
        assert snap == "2026-03-01T00:00:00+00:00"
        assert {r["result_id"] for r in rows} == {early_a, early_b}
        # 快照上界与游标内快照不一致 → 拒绝
        rows2, token2, snap2 = await store.query_results_page_snapshot(
            snapshot_at="2026-03-01T00:00:00+00:00", limit=1
        )
        assert token2 is not None and snap2 == snap
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page_snapshot(
                snapshot_at="2099-01-01T00:00:00+00:00", cursor=token2
            )
        assert late_id not in {r["result_id"] for r in rows}

    async def test_v2_cursor_fingerprint_mismatch_rejected(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        for _ in range(3):
            await _seed_auto_result(store, definition_id="def-A")
        await _seed_auto_result(store, definition_id="def-B")
        _, token, _ = await store.query_results_page_snapshot(
            definition_id="def-A", limit=1
        )
        # 同过滤重复翻页成功
        rows, next_token, _ = await store.query_results_page_snapshot(
            definition_id="def-A", limit=1, cursor=token
        )
        assert len(rows) == 1
        assert next_token is not None
        rows2, next_token2, _ = await store.query_results_page_snapshot(
            definition_id="def-A", limit=1, cursor=next_token
        )
        assert next_token2 is None
        # 过滤变更后携带旧游标 → 拒绝
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page_snapshot(
                definition_id="def-B", cursor=token
            )
        # 非法 token：非 base64 / base64 但非 JSON
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page_snapshot(cursor="garbage")
        bad = base64.urlsafe_b64encode(b"not-json").decode("ascii")
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page_snapshot(cursor=bad)

    async def test_legacy_cursor_compat_branch(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        seed_ids = [
            await _seed_auto_result(store, definition_id="def-A")
            for _ in range(3)
        ]
        # 旧接口无新增过滤：两段式游标仍可用
        rows, token = await store.query_results_page(limit=2)
        assert token is not None and token.count("|") == 1
        rows2, _ = await store.query_results_page(limit=2, cursor=token)
        assert {r["result_id"] for r in rows + rows2} == set(seed_ids)
        # 旧游标 + 新增过滤 → 拒绝
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page(
                definition_version="v1", cursor=token
            )
        # v2 方法不接受旧两段式游标
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page_snapshot(cursor=token)
        # 旧接口无新增过滤 + v2 游标（全空过滤指纹）→ 兼容接受，输出新 token
        _, v2_token, _ = await store.query_results_page_snapshot(limit=1)
        rows3, token3 = await store.query_results_page(
            limit=1, cursor=v2_token
        )
        assert token3 is not None and "|" not in token3

    async def test_same_created_at_no_dup_no_miss(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        with _fixed_store_now("2026-05-05T00:00:00+00:00"):
            seed_ids = [
                await _seed_auto_result(store, definition_id="def-A")
                for _ in range(5)
            ]
        collected: list[str] = []
        cursor = None
        while True:
            rows, cursor, _ = await store.query_results_page_snapshot(
                limit=2, cursor=cursor
            )
            collected.extend(r["result_id"] for r in rows)
            if cursor is None:
                break
        assert len(collected) == 5
        assert len(set(collected)) == 5
        assert set(collected) == set(seed_ids)

    async def test_limit_default_cap_and_validation(self, tmp_path,
                                                    monkeypatch):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        for _ in range(55):
            await _seed_auto_result(store, definition_id="def-A")
        rows, next_cursor, _ = await store.query_results_page_snapshot()
        assert len(rows) == 50  # 默认 50
        assert next_cursor is not None
        monkeypatch.setattr(_evaluation_store_module,
                            "_RESULTS_PAGE_MAX_LIMIT", 3)
        rows, _, _ = await store.query_results_page_snapshot(limit=500)
        assert len(rows) == 3  # 上限封顶
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page_snapshot(limit=0)
        with pytest.raises(EvaluationValidationError):
            await store.query_results_page(limit=0)


class TestAnnotationQueries:
    """aggregate_metric_results 与 list_annotation_queue（标注队列）。"""

    async def test_aggregate_groups_measures_and_no_key_mixing(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        await _seed_auto_result(store, definition_id="def-A",
                                definition_version="v1", typed_value=0.9)
        await _seed_auto_result(store, definition_id="def-A",
                                definition_version="v1", typed_value=0.3)
        await _seed_auto_result(store, definition_id="def-A",
                                definition_version="v1", status="skipped",
                                typed_value=None)
        await _seed_auto_result(store, definition_id="def-A",
                                definition_version="v1", status="error",
                                typed_value=None)
        await _seed_auto_result(store, definition_id="def-A",
                                definition_version="v2", typed_value=0.5)
        await _seed_human_result(store, definition_id="def-A",
                                 definition_version="v1", typed_value=0.8)
        await _seed_auto_result(store, definition_id="def-B",
                                metric_name="helpful", output_type="boolean",
                                typed_value=True)
        await _seed_auto_result(store, definition_id="def-B",
                                metric_name="helpful", output_type="boolean",
                                typed_value=False)
        await _seed_auto_result(store, definition_id="def-C",
                                metric_name="tone", output_type="categorical",
                                typed_value="good")
        await _seed_auto_result(store, definition_id="def-C",
                                metric_name="tone", output_type="categorical",
                                typed_value="bad")

        rows = await store.aggregate_metric_results()
        by_key = {
            (r["definition_id"], r["definition_version"], r["provenance"],
             r["output_type"]): r
            for r in rows
        }
        # 不同 version / provenance 分行返回（聚合键不混合）
        assert ("def-A", "v2", "automatic", "number") in by_key
        assert ("def-A", "v1", "human", "number") in by_key

        g = by_key[("def-A", "v1", "automatic", "number")]
        assert g["numeric_sum"] == pytest.approx(1.2)
        assert g["numeric_count"] == 2
        assert g["numeric_min"] == pytest.approx(0.3)
        assert g["numeric_max"] == pytest.approx(0.9)
        assert g["status_counts"] == {"value": 2, "skipped": 1, "error": 1}
        assert g["total_count"] == 4

        assert by_key[("def-A", "v2", "automatic", "number")][
            "numeric_sum"] == pytest.approx(0.5)
        assert by_key[("def-A", "v1", "human", "number")][
            "numeric_sum"] == pytest.approx(0.8)

        gb = by_key[("def-B", "v1", "automatic", "boolean")]
        assert gb["boolean_pass"] == 1
        assert gb["boolean_count"] == 2

        gc = by_key[("def-C", "v1", "automatic", "categorical")]
        assert gc["categorical_counts"] == {"good": 1, "bad": 1}

    async def test_aggregate_time_range_filters_and_daily_counts(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        # UTC+8 日分桶：15:59 UTC 当日、16:00 UTC 次日
        with _fixed_store_now("2026-03-01T15:59:59+00:00"):
            await _seed_auto_result(store, definition_id="def-A",
                                    typed_value=0.9)
        with _fixed_store_now("2026-03-01T16:00:01+00:00"):
            await _seed_auto_result(store, definition_id="def-A",
                                    typed_value=0.3)
        with _fixed_store_now("2026-05-01T00:00:00+00:00"):
            await _seed_auto_result(store, definition_id="def-A",
                                    typed_value=0.7)

        rows = await store.aggregate_metric_results()
        assert len(rows) == 1
        g = rows[0]
        assert g["numeric_sum"] == pytest.approx(1.9)
        assert g["daily_counts"] == {"2026-03-01": 1, "2026-03-02": 1,
                                     "2026-05-01": 1}

        # 半开区间 [from, to) 服务端过滤（不依赖前端最近 50 条）
        rows = await store.aggregate_metric_results(
            created_from="2026-04-01T00:00:00+00:00"
        )
        assert rows[0]["numeric_sum"] == pytest.approx(0.7)
        rows = await store.aggregate_metric_results(
            created_to="2026-04-01T00:00:00+00:00"
        )
        assert rows[0]["numeric_sum"] == pytest.approx(1.2)
        rows = await store.aggregate_metric_results(
            created_from="2026-03-01T16:00:00+00:00",
            created_to="2026-04-01T00:00:00+00:00",
        )
        assert rows[0]["numeric_sum"] == pytest.approx(0.3)
        assert rows[0]["daily_counts"] == {"2026-03-02": 1}

        # target_kind / provenance / status 过滤
        human_id = await _seed_human_result(
            store, definition_id="def-H", typed_value=0.8
        )
        rows = await store.aggregate_metric_results(
            target_kind="session_feedback"
        )
        assert len(rows) == 1
        assert rows[0]["provenance"] == "human"
        rows = await store.aggregate_metric_results(provenance="human")
        assert len(rows) == 1
        assert rows[0]["numeric_sum"] == pytest.approx(0.8)
        rows = await store.aggregate_metric_results(status="skipped")
        assert rows == []

    async def test_aggregate_human_current_uses_thread_latest(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        target = {
            "service": "n-agent",
            "kind": "session_feedback",
            "subject_id": "turn-9",
            "subject_revision": "rev-1",
        }
        with _fixed_store_now("2026-02-01T00:00:00+00:00"):
            await _seed_human_result(store, definition_id="def-H",
                                     target=target, typed_value=0.2)
        with _fixed_store_now("2026-03-01T00:00:00+00:00"):
            latest_id = await _seed_human_result(
                store, definition_id="def-H", target=target, typed_value=0.9
            )
        # 完整线程先取最高 revision 再过滤时间：窗外旧修订不复活
        rows = await store.aggregate_metric_results(definition_id="def-H")
        assert len(rows) == 1
        assert rows[0]["numeric_sum"] == pytest.approx(0.9)
        rows = await store.aggregate_metric_results(
            definition_id="def-H",
            created_to="2026-02-15T00:00:00+00:00",
        )
        assert rows == []

        # 旧跨线程 supersedes 不能隐藏另一线程的最新值
        await _seed_auto_result(store, definition_id="def-X",
                                supersedes_result_id=latest_id)
        rows = await store.aggregate_metric_results(definition_id="def-H")
        assert len(rows) == 1
        assert rows[0]["numeric_sum"] == pytest.approx(0.9)

    async def test_aggregate_excludes_superseded_and_tombstoned(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        old_id = await _seed_auto_result(store, definition_id="def-A",
                                         typed_value=0.5)
        new_id = await _seed_auto_result(store, definition_id="def-A",
                                         typed_value=0.7,
                                         supersedes_result_id=old_id)
        rows = await store.aggregate_metric_results()
        assert rows[0]["numeric_sum"] == pytest.approx(0.7)
        await store.record_deletion_tombstone(
            {"subject_kind": "result", "subject_id": new_id}
        )
        rows = await store.aggregate_metric_results()
        assert rows == []

    async def test_annotation_queue_low_score(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        low = await _seed_auto_result(store, typed_value=0.3)
        high = await _seed_auto_result(store, typed_value=0.8)
        false_bool = await _seed_auto_result(store, definition_id="def-B",
                                             metric_name="helpful",
                                             output_type="boolean",
                                             typed_value=False)
        true_bool = await _seed_auto_result(store, definition_id="def-B",
                                            metric_name="helpful",
                                            output_type="boolean",
                                            typed_value=True)
        await _seed_auto_result(store, status="skipped", typed_value=None)

        rows, next_cursor = await store.list_annotation_queue(
            state="all", reason="low_score"
        )
        assert next_cursor is None
        assert {r["source_result_id"] for r in rows} == {low, false_bool}
        # threshold 参数化
        rows, _ = await store.list_annotation_queue(
            state="all", reason="low_score", threshold=0.9
        )
        assert {r["source_result_id"] for r in rows} == {
            low, high, false_bool
        }

    async def test_annotation_queue_anomalous_and_disputed(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        err = await _seed_auto_result(store, status="error", typed_value=None)
        skip = await _seed_auto_result(store, status="skipped",
                                       typed_value=None)
        await _seed_auto_result(store)
        rows, _ = await store.list_annotation_queue(
            state="all", reason="anomalous"
        )
        assert {r["source_result_id"] for r in rows} == {err, skip}

        # disputed：同 (metric, target subject) 上自动与人工值冲突
        disputed_target = {
            "service": "n-agent",
            "kind": "session_feedback",
            "subject_id": "turn-1",
            "subject_revision": "rev-1",
        }
        agree_target = dict(disputed_target, subject_id="turn-3")
        d_auto = await _seed_auto_result(store, definition_id="def-D",
                                         target=disputed_target,
                                         typed_value=0.9)
        await _seed_human_result(store, definition_id="def-D",
                                 target=disputed_target, typed_value=0.5)
        await _seed_auto_result(store, definition_id="def-D",
                                target=agree_target, typed_value=0.9)
        await _seed_human_result(store, definition_id="def-D",
                                 target=agree_target, typed_value=0.9)
        rows, _ = await store.list_annotation_queue(
            state="all", reason="disputed"
        )
        assert {r["source_result_id"] for r in rows} == {d_auto}

    async def test_annotation_queue_state_annotated_pending(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        target_annotated = {
            "service": "n-agent",
            "kind": "session_feedback",
            "subject_id": "turn-1",
            "subject_revision": "rev-1",
        }
        target_pending = dict(target_annotated, subject_id="turn-2")
        r_annot = await _seed_auto_result(store, definition_id="def-S",
                                          target=target_annotated,
                                          typed_value=0.3)
        await store.save_feedback(
            _feedback_payload(
                actor_ref="dashboard",
                definition_id="def-S",
                definition_version="v1",
                target=target_annotated,
                value={"typed_value": 1, "status": "value"},
            )
        )
        r_pend = await _seed_auto_result(store, definition_id="def-S",
                                         target=target_pending,
                                         typed_value=0.3)

        rows, _ = await store.list_annotation_queue(
            state="pending", reason="low_score"
        )
        assert {r["source_result_id"] for r in rows} == {r_pend}
        rows, _ = await store.list_annotation_queue(
            state="annotated", reason="low_score"
        )
        assert {r["source_result_id"] for r in rows} == {r_annot}
        rows, _ = await store.list_annotation_queue(
            state="all", reason="low_score"
        )
        assert {r["source_result_id"] for r in rows} == {r_annot, r_pend}

        # 行级事实：四元组 / definition / output_type / typed_value /
        # evaluated_at / latest_human_feedback（含 revision, created_at, value）
        row = next(r for r in rows if r["source_result_id"] == r_annot)
        assert row["target_service"] == "n-agent"
        assert row["target_kind"] == "session_feedback"
        assert row["target_subject_id"] == "turn-1"
        assert row["target_subject_revision"] == "rev-1"
        assert row["definition_id"] == "def-S"
        assert row["definition_version"] == "v1"
        assert row["output_type"] == "number"
        assert row["typed_value"] == pytest.approx(0.3)
        assert row["status"] == "value"
        assert row["evaluated_at"]
        latest = row["latest_human_feedback"]
        assert latest["actor_ref"] == "dashboard"
        assert latest["revision"] == 1
        assert latest["created_at"]
        assert latest["value"]["typed_value"] == 1
        pend_row = next(r for r in rows if r["source_result_id"] == r_pend)
        assert pend_row["latest_human_feedback"] is None

    async def test_annotation_queue_pagination_order_and_validation(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        seed_ids: list[str] = []
        stamps = [
            f"2026-01-0{day}T00:00:00+00:00" for day in range(1, 6)
        ]
        for stamp in stamps:
            with _fixed_store_now(stamp):
                seed_ids.append(await _seed_auto_result(
                    store, definition_id="def-A", typed_value=0.2
                ))
        collected: list[str] = []
        cursor = None
        while True:
            rows, cursor = await store.list_annotation_queue(
                state="all", reason="low_score", limit=2, cursor=cursor
            )
            collected.extend(r["source_result_id"] for r in rows)
            if cursor is None:
                break
        # created_at DESC 稳定排序，不重不漏
        assert collected == list(reversed(seed_ids))

        # 过滤变更后携带旧游标 → 拒绝
        _, token = await store.list_annotation_queue(
            state="all", reason="low_score", limit=2
        )
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(
                state="all", reason="anomalous", cursor=token
            )
        # 非法 state / reason / limit / 旧格式游标
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(state="bogus",
                                              reason="low_score")
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(state="all", reason="bogus")
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(state="all",
                                              reason="low_score", limit=0)
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(state="all",
                                              reason="low_score",
                                              cursor="a|b")

    async def test_annotation_queue_threshold_validation(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        low = await _seed_auto_result(store, typed_value=0.3)
        high = await _seed_auto_result(store, typed_value=0.9)
        # bool 拒绝（禁止静默转 1.0）
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(
                state="all", reason="low_score", threshold=True
            )
        # 非数值字符串拒绝（禁止裸 ValueError 泄漏）
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(
                state="all", reason="low_score", threshold="abc"
            )
        # 非有限浮点拒绝
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(
                state="all", reason="low_score", threshold=float("nan")
            )
        with pytest.raises(EvaluationValidationError):
            await store.list_annotation_queue(
                state="all", reason="low_score", threshold=float("inf")
            )
        # 合法数字字符串 "0.8" 与 0.8 行为一致（含游标 fingerprint）
        rows_num, cursor_num = await store.list_annotation_queue(
            state="all", reason="low_score", threshold=0.8
        )
        rows_str, cursor_str = await store.list_annotation_queue(
            state="all", reason="low_score", threshold="0.8"
        )
        assert {r["source_result_id"] for r in rows_num} == {low}
        assert {r["source_result_id"] for r in rows_str} == {low}
        assert (cursor_num is None) == (cursor_str is None)

    async def test_annotation_queue_pending_when_feedback_older_than_result(
        self, tmp_path
    ):
        """dashboard 人评 created_at 早于 source_result.created_at 时：
        annotated EXISTS 的 ``df.created_at >= c.created_at`` 条件不满足，
        该条目仍为 pending（不得误判为已标注）。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        target = {
            "service": "n-agent",
            "kind": "session_feedback",
            "subject_id": "turn-7",
            "subject_revision": "rev-1",
        }
        # 人评先于自动结果产生（created_at 更早）
        with _fixed_store_now("2026-01-01T00:00:00+00:00"):
            await store.save_feedback(
                _feedback_payload(
                    actor_ref="dashboard",
                    definition_id="def-O",
                    definition_version="v1",
                    target=target,
                    value={"typed_value": 1, "status": "value"},
                )
            )
        with _fixed_store_now("2026-01-02T00:00:00+00:00"):
            result_id = await _seed_auto_result(
                store, definition_id="def-O", target=target, typed_value=0.3
            )

        rows, _ = await store.list_annotation_queue(
            state="pending", reason="low_score"
        )
        assert {r["source_result_id"] for r in rows} == {result_id}
        rows, _ = await store.list_annotation_queue(
            state="annotated", reason="low_score"
        )
        assert rows == []
