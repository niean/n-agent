"""Tests for LocalEvidenceStore (Infrastructure Layer, plan T4).

File snapshot storage implementing the Domain ``EvidenceStore`` async
Protocol (``app/domain/evaluation/ports.py``) over the local filesystem
plus the T3 SQLite evaluation store as the reference registry.

Covers:
- write order: temp file -> fsync -> atomic rename -> DB registration
  (file exists before reference; registration failure leaves a
  reclaimable orphan)
- SHA-256 consistency with the domain canonical JSON encoding
  (``canonical_json_bytes`` / ``EvidenceSnapshot``); 2MiB mirror limit
- TTL reclamation: unreferenced orphans past retention deleted (bounded
  per sweep); referenced non-expired files never orphan-deleted;
  expired referenced snapshots follow the expiry-revocation flow
  (revoke queued jobs -> clear content references -> purge); results
  are untouched (90-day retention is a ledger concern, not file TTL);
  no implicit evidence re-creation through retries
- path traversal / symlink rejection under the evidence root
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.evaluation.models import (
    EVIDENCE_SNAPSHOT_MAX_BYTES,
    EvaluationTarget,
    EvaluationValidationError,
    EvidenceSnapshot,
    canonical_json_bytes,
)
from app.domain.evaluation.ports import EvidenceStore
from app.infrastructure.evaluation.evidence_store import (
    EvidenceNotFoundError,
    EvidenceStoreError,
    LocalEvidenceStore,
)
from app.infrastructure.evaluation.store import (
    SQLiteEvaluationStore,
    EvaluationConflictError,
)

UTC = timezone.utc
START = datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC)
RETENTION = timedelta(days=7)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _Clock:
    """Injectable deterministic clock (no sleeps in tests)."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def env(tmp_path: Path):
    db_path = tmp_path / "evaluations.db"
    root = tmp_path / "locals" / "evaluations"
    store = SQLiteEvaluationStore(str(db_path))
    clock = _Clock(START)
    evidence = LocalEvidenceStore(root, store, clock=clock)
    return SimpleNamespace(
        tmp_path=tmp_path,
        root=root,
        store=store,
        evidence=evidence,
        clock=clock,
    )


def _target(**overrides) -> EvaluationTarget:
    params = {
        "service": "n-agent",
        "kind": "agent_run",
        "subject_id": "run-1",
        "subject_revision": "rev-1",
    }
    params.update(overrides)
    return EvaluationTarget(**params)


def _snapshot(
    content: dict | None = None, redaction_version: str = "redact-v1"
) -> EvidenceSnapshot:
    if content is None:
        content = {
            "query": "n-agent evaluation",
            "answer": "a1",
            "chunks": [{"chunk_id": "c1", "score": 0.9}],
        }
    return EvidenceSnapshot(content=content, redaction_version=redaction_version)


def _ref_of(snapshot: EvidenceSnapshot) -> str:
    return f"snapshots/{snapshot.content_sha256}.json"


def _snapshot_path(root: Path, snapshot: EvidenceSnapshot) -> Path:
    return root / _ref_of(snapshot)


def _set_mtime(path: Path, mtime: datetime) -> None:
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))


async def _make_orphan(
    env, monkeypatch: pytest.MonkeyPatch, content: dict, *, mtime: datetime
) -> str:
    """Create an on-disk snapshot file whose DB registration failed."""
    snapshot = _snapshot(content)

    async def _fail(payload):
        raise EvaluationConflictError("db registration failed")

    # Patch only for this call: restore the real registration afterwards
    # so later saves in the same test behave normally.
    with monkeypatch.context() as m:
        m.setattr(env.store, "save_sample", _fail)
        with pytest.raises(EvaluationConflictError):
            await env.evidence.save_snapshot(_target(), snapshot)
    ref = _ref_of(snapshot)
    path = env.root / ref
    assert path.is_file()
    _set_mtime(path, mtime)
    return ref


# ---------------------------------------------------------------------------
# Port conformance + write/read consistency
# ---------------------------------------------------------------------------


async def test_satisfies_evidence_store_port(env):
    assert isinstance(env.evidence, EvidenceStore)


async def test_save_persists_canonical_bytes_with_matching_sha256(env):
    snapshot = _snapshot()
    ref = await env.evidence.save_snapshot(_target(), snapshot)

    assert ref == _ref_of(snapshot)
    path = _snapshot_path(env.root, snapshot)
    assert path.is_file()
    persisted = path.read_bytes()
    # File bytes are exactly the domain canonical JSON encoding and their
    # hash equals the snapshot's content_sha256.
    assert persisted == canonical_json_bytes(dict(snapshot.content))
    assert hashlib.sha256(persisted).hexdigest() == snapshot.content_sha256

    loaded = await env.evidence.load_snapshot(ref)
    assert isinstance(loaded, EvidenceSnapshot)
    assert dict(loaded.content) == dict(snapshot.content)
    assert loaded.redaction_version == snapshot.redaction_version
    assert loaded.content_sha256 == snapshot.content_sha256
    assert loaded.canonical_bytes == snapshot.canonical_bytes


async def test_save_registers_reference_in_store(env):
    snapshot = _snapshot()
    ref = await env.evidence.save_snapshot(_target(), snapshot)

    rows = await env.store.list_samples_by_snapshot_ref(ref)
    assert len(rows) == 1
    row = rows[0]
    assert row["snapshot_hash"] == snapshot.content_sha256
    assert row["service"] == "n-agent"
    assert row["target"]["redaction_version"] == "redact-v1"
    assert row["target"]["subject_id"] == "run-1"

    # Reference is registered through the T3 evidence_targets reverse index.
    sample_ids = await env.store.list_sample_ids_by_subject(
        "n-agent", "agent_run", "run-1", "rev-1"
    )
    assert sample_ids == [row["sample_id"]]

    # Re-saving identical content for the same target is idempotent: same
    # registration row, same single file.
    ref2 = await env.evidence.save_snapshot(_target(), snapshot)
    assert ref2 == ref
    assert len(await env.store.list_samples_by_snapshot_ref(ref)) == 1


async def test_file_exists_before_db_registration(env, monkeypatch):
    observed: dict = {}
    real_save_sample = env.store.save_sample

    async def _spy(payload):
        observed["file_existed"] = _snapshot_path(
            env.root, _snapshot()
        ).exists()
        return await real_save_sample(payload)

    monkeypatch.setattr(env.store, "save_sample", _spy)
    await env.evidence.save_snapshot(_target(), _snapshot())
    assert observed["file_existed"] is True


async def test_registration_failure_leaves_reclaimable_orphan(
    env, monkeypatch
):
    store = env.store
    snapshot = _snapshot({"q": "orphan"})
    ref = _ref_of(snapshot)

    async def _fail(payload):
        raise EvaluationConflictError("db registration failed")

    monkeypatch.setattr(store, "save_sample", _fail)
    with pytest.raises(EvaluationConflictError):
        await env.evidence.save_snapshot(_target(), snapshot)

    path = _snapshot_path(env.root, snapshot)
    assert path.is_file()  # file survives the failed registration
    assert await store.list_samples_by_snapshot_ref(ref) == []

    # Young orphan is kept.
    _set_mtime(path, START - timedelta(hours=1))
    report = await env.evidence.sweep()
    assert report["orphan_deleted"] == []
    assert path.is_file()

    # Past retention the unreferenced file is reclaimed by the sweeper.
    _set_mtime(path, START - RETENTION - timedelta(hours=1))
    report = await env.evidence.sweep()
    assert report["orphan_deleted"] == [ref]
    assert not path.exists()


async def test_store_mirrors_domain_2mib_limit(env):
    oversized = SimpleNamespace(
        canonical_bytes=b"x" * (EVIDENCE_SNAPSHOT_MAX_BYTES + 1),
        content_sha256="a" * 64,
        redaction_version="redact-v1",
    )
    with pytest.raises(EvaluationValidationError):
        await env.evidence.save_snapshot(_target(), oversized)
    # Nothing written, nothing registered.
    assert not (env.root / "snapshots" / f"{'a' * 64}.json").exists()

    # Domain gate: a real EvidenceSnapshot over the limit cannot be built.
    big_content = {"blob": "y" * (EVIDENCE_SNAPSHOT_MAX_BYTES + 1)}
    with pytest.raises(EvaluationValidationError):
        _snapshot(big_content)


async def test_forged_declared_sha_rejected_before_write(env):
    # Declared content_sha256 does not match sha256(canonical_bytes):
    # rejected before any file write (poison-ref defense).
    forged = SimpleNamespace(
        canonical_bytes=b'{"q":"forged"}',
        content_sha256="a" * 64,
        redaction_version="redact-v1",
    )
    with pytest.raises(EvaluationValidationError):
        await env.evidence.save_snapshot(_target(), forged)
    # Nothing written at the declared (forged) name nor at the real
    # content hash.
    assert not (env.root / "snapshots" / f"{'a' * 64}.json").exists()
    real_sha = hashlib.sha256(b'{"q":"forged"}').hexdigest()
    assert not (env.root / "snapshots" / f"{real_sha}.json").exists()


async def test_post_expiry_recapture_fails_closed(env):
    evidence, store, clock = env.evidence, env.store, env.clock
    snapshot = _snapshot({"q": "recapture"})
    ref = await evidence.save_snapshot(_target(), snapshot)
    path = _snapshot_path(env.root, snapshot)

    _set_mtime(path, START - RETENTION - timedelta(hours=1))
    clock.now = START + timedelta(days=8)
    await evidence.sweep()
    assert not path.exists()

    # Re-capture identical content after the registration row was
    # expiry-cleared: the idempotent registration replay restores no
    # reference, so the store must fail closed instead of returning a
    # ref that load rejects.
    with pytest.raises(EvidenceStoreError):
        await evidence.save_snapshot(_target(), snapshot)
    # The freshly created file is cleaned up, not left behind as success.
    assert not path.exists()
    with pytest.raises(EvidenceNotFoundError):
        await evidence.load_snapshot(ref)


async def test_reuse_path_refreshes_expired_mtime(env):
    snapshot = _snapshot({"q": "reuse-refresh"})
    ref = _ref_of(snapshot)
    # Expired unreferenced copy already on disk (crash before
    # registration, clock moved past retention).
    path = _snapshot_path(env.root, snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(snapshot.canonical_bytes)
    _set_mtime(path, START - RETENTION - timedelta(hours=1))

    # Re-capture: the reuse path must refresh the mtime via temp+replace
    # so a concurrent sweep between the reuse check and registration
    # cannot delete the file out from under the live registration.
    ref2 = await env.evidence.save_snapshot(_target(), snapshot)
    assert ref2 == ref
    mtime = datetime.fromtimestamp(path.lstat().st_mtime, tz=UTC)
    assert mtime > START - RETENTION  # no longer expired

    loaded = await env.evidence.load_snapshot(ref)
    assert dict(loaded.content) == dict(snapshot.content)
    assert loaded.content_sha256 == snapshot.content_sha256


# ---------------------------------------------------------------------------
# Path safety: traversal, symlinks, unregistered/tampered content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_ref",
    [
        "snapshots/../../secret.json",
        "snapshots/../secret.txt",
        "/etc/passwd",
        "other/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        "snapshots/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.txt",
        "snapshots/ABCDEF.json",
        "snapshots/abc.json",
        "snapshots/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/ nested.json",
        "snapshots\\aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        "",
        "snapshots/",
    ],
)
async def test_load_rejects_path_traversal_refs(env, bad_ref):
    with pytest.raises(EvaluationValidationError):
        await env.evidence.load_snapshot(bad_ref)


async def test_load_traversal_ref_touches_nothing_outside_root(env, tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_bytes(b"{}")
    with pytest.raises(EvaluationValidationError):
        await env.evidence.load_snapshot("snapshots/../secret.json")
    assert secret.read_bytes() == b"{}"


async def test_symlink_leaf_rejected_and_never_swept(env):
    snapshot = _snapshot({"q": "symlink-victim"})
    ref = await env.evidence.save_snapshot(_target(), snapshot)
    path = _snapshot_path(env.root, snapshot)
    outside = env.tmp_path / "outside.json"
    outside.write_bytes(b"{}")
    path.unlink()
    os.symlink(outside, path)

    with pytest.raises(EvaluationValidationError):
        await env.evidence.load_snapshot(ref)

    # Aged symlink is skipped by the sweeper: never deleted through and
    # never followed (entries are lstat-classified; symlinks are not
    # candidates at all).
    report = await env.evidence.sweep()
    assert report["orphan_deleted"] == []
    assert report["expired_purged"] == []
    assert path.name in report["skipped"]
    assert path.is_symlink()
    assert outside.exists()


async def test_sweep_reports_skipped_foreign_entries(env):
    snapshots = env.root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    foreign = snapshots / "not-a-snapshot.txt"
    foreign.write_bytes(b"foreign")
    subdir = snapshots / "subdir"
    subdir.mkdir()

    report = await env.evidence.sweep()
    assert "not-a-snapshot.txt" in report["skipped"]
    assert "subdir" in report["skipped"]
    assert foreign.exists()
    assert subdir.is_dir()


async def test_sweep_reclaims_expired_temp_files(env):
    snapshots = env.root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    temp_old = snapshots / ".__tmp_old.tmp"
    temp_old.write_bytes(b"partial")
    temp_young = snapshots / ".__tmp_new.tmp"
    temp_young.write_bytes(b"partial")
    _set_mtime(temp_old, START - RETENTION - timedelta(hours=1))
    _set_mtime(temp_young, START - timedelta(hours=1))

    report = await env.evidence.sweep()
    assert report["temp_deleted"] == [".__tmp_old.tmp"]
    assert not temp_old.exists()
    assert temp_young.exists()


async def test_sweep_temp_files_share_max_files_budget(env):
    snapshots = env.root / "snapshots"
    snapshots.mkdir(parents=True, exist_ok=True)
    temp_a = snapshots / ".__tmp_a.tmp"
    temp_a.write_bytes(b"partial")
    temp_b = snapshots / ".__tmp_b.tmp"
    temp_b.write_bytes(b"partial")
    _set_mtime(temp_a, START - RETENTION - timedelta(hours=1))
    _set_mtime(temp_b, START - RETENTION - timedelta(hours=1))

    report = await env.evidence.sweep(max_files=1)
    assert report["temp_deleted"] == [".__tmp_a.tmp"]  # name-sorted order
    assert temp_b.exists()


async def test_sweep_rejects_naive_now(env):
    with pytest.raises(EvaluationValidationError):
        await env.evidence.sweep(now=datetime(2026, 11, 10, 12, 0, 0))


async def test_load_requires_row_hash_equality(env):
    snapshot = _snapshot({"q": "empty-row-hash"})
    # Plant the file and register a broken/legacy row with an empty
    # snapshot_hash: must NOT silently skip the registered-hash check.
    path = _snapshot_path(env.root, snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(snapshot.canonical_bytes)
    await env.store.save_sample(
        {
            "service": "n-agent",
            "producer_event_id": "legacy-row",
            "snapshot_ref": _ref_of(snapshot),
            "snapshot_hash": "",
            "target": {"redaction_version": "redact-v1"},
        }
    )
    with pytest.raises(EvidenceStoreError):
        await env.evidence.load_snapshot(_ref_of(snapshot))


async def test_load_maps_concurrent_deletion_to_not_found(env, monkeypatch):
    snapshot = _snapshot({"q": "toctou"})
    ref = await env.evidence.save_snapshot(_target(), snapshot)
    real_resolve = env.evidence._resolve_existing

    def _resolve_and_delete(sha):
        target = real_resolve(sha)
        os.unlink(target)  # concurrent deletion after resolve, before open
        return target

    monkeypatch.setattr(env.evidence, "_resolve_existing", _resolve_and_delete)
    with pytest.raises(EvidenceNotFoundError):
        await env.evidence.load_snapshot(ref)


async def test_load_unregistered_ref_raises(env):
    snapshot = _snapshot({"q": "no-registration"})
    # Simulate a crash before registration: file exists, no DB row.
    path = _snapshot_path(env.root, snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(snapshot.canonical_bytes)
    with pytest.raises(EvidenceNotFoundError):
        await env.evidence.load_snapshot(_ref_of(snapshot))


async def test_load_detects_hash_mismatch(env):
    snapshot = _snapshot({"q": "tamper"})
    ref = await env.evidence.save_snapshot(_target(), snapshot)
    path = _snapshot_path(env.root, snapshot)
    path.write_bytes(
        json.dumps({"q": "tampered"}, sort_keys=True).encode("utf-8")
    )
    with pytest.raises(EvidenceStoreError):
        await env.evidence.load_snapshot(ref)


# ---------------------------------------------------------------------------
# TTL reclamation
# ---------------------------------------------------------------------------


async def test_sweep_reclaims_only_expired_orphans(env, monkeypatch):
    expired_orphan = await _make_orphan(
        env,
        monkeypatch,
        {"q": "old-orphan"},
        mtime=START - RETENTION - timedelta(hours=1),
    )
    young_orphan = await _make_orphan(
        env, monkeypatch, {"q": "young-orphan"}, mtime=START - timedelta(hours=1)
    )
    # Referenced and young: never orphan-deleted.
    ref = await env.evidence.save_snapshot(
        _target(), _snapshot({"q": "referenced"})
    )

    report = await env.evidence.sweep()
    assert report["orphan_deleted"] == [expired_orphan]
    assert not (env.root / expired_orphan).exists()
    assert (env.root / young_orphan).is_file()
    assert (env.root / ref).is_file()


async def test_sweep_is_bounded_per_round(env, monkeypatch):
    refs = [
        await _make_orphan(
            env,
            monkeypatch,
            {"q": f"o{i}"},
            mtime=START - RETENTION - timedelta(hours=1),
        )
        for i in range(3)
    ]
    # The sweeper enumerates in server filename order (content-addressed
    # sha names), so the bounded first round deletes the two
    # lexicographically smallest names.
    ordered = sorted(refs)
    report = await env.evidence.sweep(max_files=2)
    assert report["orphan_deleted"] == ordered[:2]
    assert (env.root / ordered[2]).is_file()

    report2 = await env.evidence.sweep(max_files=2)
    assert report2["orphan_deleted"] == [ordered[2]]
    assert not (env.root / ordered[2]).exists()


async def test_sweep_without_snapshot_dir_is_noop(env):
    report = await env.evidence.sweep()
    assert report["orphan_deleted"] == []
    assert report["expired_purged"] == []
    assert report["revoked_job_ids"] == []


# ---------------------------------------------------------------------------
# Expiry-revocation of referenced snapshots (7-day default, spec 13)
# ---------------------------------------------------------------------------


async def test_expired_referenced_snapshot_revokes_jobs_and_purges(env):
    evidence, store, clock = env.evidence, env.store, env.clock
    snapshot = _snapshot({"q": "expire-me"})
    ref = await evidence.save_snapshot(_target(), snapshot)
    rows = await store.list_samples_by_snapshot_ref(ref)
    sample_id = rows[0]["sample_id"]

    # A finished job with a committed result (90-day retention boundary:
    # result rows must survive the evidence purge untouched).
    job_ok = await store.save_job(
        {
            "sample_id": sample_id,
            "definition_id": "d1",
            "definition_version": "v1",
            "idempotency_key": "ok-1",
        }
    )
    claimed = await store.claim_next_job("worker-1")
    assert claimed["job_id"] == job_ok
    result_id = await store.commit_result_with_deliveries(
        {
            "job_id": job_ok,
            "provenance": "automatic",
            "metric_name": "m1",
            "status": "succeeded",
            "typed_value": 0.9,
            "passed": True,
        },
        lease_token=claimed["lease_token"],
        job_terminal_status="succeeded",
    )

    # A queued job that can no longer execute once the evidence expires.
    job_queued = await store.save_job(
        {
            "sample_id": sample_id,
            "definition_id": "d1",
            "definition_version": "v1",
            "idempotency_key": "q-1",
        }
    )

    path = _snapshot_path(env.root, snapshot)
    _set_mtime(path, START - RETENTION - timedelta(hours=1))
    clock.now = START + timedelta(days=8)

    report = await evidence.sweep()
    assert report["expired_purged"] == [ref]
    assert report["revoked_job_ids"] == [job_queued]
    assert report["orphan_deleted"] == []

    # Queued job revoked (queued -> cancelled via the domain state machine)
    # and no longer claimable: retries cannot run against purged evidence.
    assert (await store.get_job(job_queued))["status"] == "cancelled"
    assert await store.claim_next_job("worker-2") is None

    # Content references cleared on the sample registration row.
    sample = await store.load_sample(sample_id)
    assert sample["snapshot_ref"] == ""
    assert sample["snapshot_hash"] == ""

    # Results are NOT touched by evidence expiry (90-day retention).
    result = await store.load_result(result_id)
    assert result["status"] == "succeeded"
    assert result["typed_value"] == 0.9

    # The file is purged and can no longer be loaded.
    assert not path.exists()
    with pytest.raises(EvidenceNotFoundError):
        await evidence.load_snapshot(ref)

    # Second sweep is a stable no-op.
    report2 = await evidence.sweep()
    assert report2["expired_purged"] == []
    assert report2["orphan_deleted"] == []
    assert report2["revoked_job_ids"] == []


async def test_no_implicit_evidence_recreation_after_expiry(env):
    evidence, store, clock = env.evidence, env.store, env.clock
    snapshot = _snapshot({"q": "late-producer"})
    ref = await evidence.save_snapshot(_target(), snapshot)
    rows = await store.list_samples_by_snapshot_ref(ref)
    sample_id = rows[0]["sample_id"]

    path = _snapshot_path(env.root, snapshot)
    _set_mtime(path, START - RETENTION - timedelta(hours=1))
    clock.now = START + timedelta(days=8)
    await evidence.sweep()

    # Late producer replays the sample registration with the identical
    # payload (the documented registration convention of this store):
    # idempotent replay returns the original sample_id but does NOT
    # re-create the file, restore the content reference, or make the
    # snapshot loadable again.
    replayed_sample_id = await store.save_sample(
        {
            "service": "n-agent",
            "producer_event_id": (
                f"evidence:agent_run:run-1:rev-1:{snapshot.content_sha256}"
            ),
            "snapshot_ref": ref,
            "snapshot_hash": snapshot.content_sha256,
            "schema_version": "evidence.v1",
            "sample_probability": 1.0,
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "rev-1",
                "redaction_version": "redact-v1",
            },
            "evidence_targets": [
                {
                    "source_service": "n-agent",
                    "subject_kind": "agent_run",
                    "subject_id": "run-1",
                    "subject_revision": "rev-1",
                }
            ],
        }
    )
    assert replayed_sample_id == sample_id
    assert not path.exists()
    sample = await store.load_sample(sample_id)
    assert sample["snapshot_ref"] == ""
    with pytest.raises(EvidenceNotFoundError):
        await evidence.load_snapshot(ref)
