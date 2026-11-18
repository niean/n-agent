"""Recovered identities enrich reads without rewriting immutable sample facts."""
import json
import sqlite3

import pytest

from app.domain.evaluation.errors import EvaluationDeletionConflictError
from app.domain.evaluation.models import EvaluationValidationError
from app.infrastructure.evaluation.store import (
    SQLiteEvaluationStore, EvaluationConflictError, EvaluationNotFoundError,
)
from tests.infrastructure.evaluation.test_evaluation_store import (
    _sample_payload, _seed_auto_result,
)


async def _seed(store, target=None):
    result_id = await _seed_auto_result(store, target=target or {
        "service": "n-agent", "kind": "agent_run", "subject_id": "old-run",
        "subject_revision": "", "session_id": "", "run_id": None,
    })
    result = await store.load_result(result_id)
    job = await store.get_job(result["job_id"])
    return result_id, job["sample_id"]


async def test_identity_enriches_load_and_results_without_changing_raw_sample(tmp_path):
    path = tmp_path / "evaluation.db"
    store = SQLiteEvaluationStore(str(path))
    result_id, sample_id = await _seed(store)
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM evaluation_samples WHERE sample_id=?", (sample_id,)).fetchone()
    evidence = {"source": "retained-run", "sha256": "a" * 64}
    assert await store.save_sample_identity(sample_id, "session-1", "run-1", "audit", evidence) == sample_id
    assert await store.save_sample_identity(sample_id, "session-1", "run-1", "audit", evidence) == sample_id
    loaded = await store.load_sample(sample_id)
    assert (loaded["target"]["session_id"], loaded["target"]["run_id"]) == ("session-1", "run-1")
    page = await store.query_results_page_counted(limit=1)
    assert page["total_count"] == 1
    item = page["items"][0]
    assert item["result_id"] == result_id
    assert (item["session_id"], item["run_id"]) == ("session-1", "run-1")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM evaluation_samples WHERE sample_id=?", (sample_id,)).fetchone() == before
        identity = conn.execute("SELECT evidence_json FROM evaluation_sample_identities").fetchone()
        assert json.loads(identity[0]) == evidence
        assert conn.execute("SELECT COUNT(*) FROM evaluation_sample_identities").fetchone()[0] == 1
    assert await store.list_sample_ids_by_subject("n-agent", "session", "session-1") == [sample_id]
    assert (await SQLiteEvaluationStore(str(path)).load_sample(sample_id))["target"]["run_id"] == "run-1"


async def test_identity_conflicts_are_rejected_and_original_identity_wins(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    _, sample_id = await _seed(store, {
        "kind": "agent_run", "subject_id": "run-1", "subject_revision": "",
        "session_id": "original-session", "run_id": "run-1",
    })
    with pytest.raises(EvaluationConflictError):
        await store.save_sample_identity(sample_id, "other-session", "run-1", "audit", {})
    await store.save_sample_identity(sample_id, "original-session", "run-1", "audit", {})
    with pytest.raises(EvaluationConflictError):
        await store.save_sample_identity(sample_id, "original-session", "other-run", "audit", {})
    assert (await store.load_sample(sample_id))["target"]["session_id"] == "original-session"
    _, missing_identity_sample = await _seed(store)
    await store.save_sample_identity(missing_identity_sample, "session-2", "run-2", "audit", {})
    with pytest.raises(EvaluationConflictError):
        await store.save_sample_identity(missing_identity_sample, "session-2", "wrong-run", "audit", {})


async def test_partial_identity_preserves_original_and_does_not_restore_deleted_association(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    _, sample_id = await _seed(store, {
        "kind": "agent_run", "subject_id": "old-run", "subject_revision": "",
        "session_id": "original-session", "run_id": None,
    })
    await store.save_sample_identity(sample_id, None, "recovered-run", "audit", {})
    target = (await store.load_sample(sample_id))["target"]
    assert (target["session_id"], target["run_id"]) == ("original-session", "recovered-run")
    await store.record_deletion_tombstone({"subject_kind": "session", "subject_id": "original-session"})
    assert (await store.load_sample(sample_id))["target"]["run_id"] is None
    assert (await store.query_results_page_counted())["items"][0]["run_id"] is None


@pytest.mark.parametrize("session_id,run_id,source", [(None, None, "audit"), ("", "run", "audit"), ("session", " ", "audit"), ("session", "run", ""), (42, "run", "audit")])
async def test_identity_validates_supplied_values(tmp_path, session_id, run_id, source):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    sample_id = await store.save_sample(_sample_payload())
    with pytest.raises(EvaluationValidationError):
        await store.save_sample_identity(sample_id, session_id, run_id, source, {})


@pytest.mark.parametrize("kind,subject", [("session", "recovered-session"), ("agent_run", "recovered-run"), ("session", "s-1")])
async def test_identity_missing_sample_and_tombstones_do_not_restore_associations(tmp_path, kind, subject):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    with pytest.raises(EvaluationNotFoundError):
        await store.save_sample_identity("missing", "session", "run", "audit", {})
    _, sample_id = await _seed(store)
    _, second_sample = await _seed(store)
    await store.save_sample_identity(sample_id, "recovered-session", "recovered-run", "audit", {})
    await store.record_deletion_tombstone({"subject_kind": kind, "subject_id": subject})
    with pytest.raises(EvaluationDeletionConflictError):
        await store.save_sample_identity(sample_id, "recovered-session", "recovered-run", "audit", {})
    target = (await store.load_sample(sample_id))["target"]
    assert not target.get("session_id") and not target.get("run_id")
    item = (await store.query_results_page_counted())["items"][0]
    assert not item.get("session_id") and not item.get("run_id")
    with pytest.raises(EvaluationDeletionConflictError):
        await store.save_sample_identity(second_sample, "recovered-session", "recovered-run", "audit", {})
