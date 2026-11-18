"""真实总数覆盖筛选、游标边界和快照上界。"""
import base64
import hashlib
import json

import pytest

from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from app.domain.evaluation.models import EvaluationValidationError
from tests.infrastructure.evaluation.test_evaluation_store import _seed_auto_result, _seed_human_result


@pytest.mark.asyncio
async def test_result_totals_count_whole_filtered_snapshot(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    for _ in range(5):
        await _seed_auto_result(store)
    await _seed_auto_result(store, status="error")
    first = await store.query_results_page_counted(status="value", limit=2)
    assert (first["total_count"], first["total_pages"]) == (5, 3)
    await _seed_auto_result(store)
    second = await store.query_results_page_counted(
        status="value", limit=2, cursor=first["next_cursor"]
    )
    assert (second["total_count"], second["total_pages"]) == (5, 3)
    assert second["snapshot_at"] == first["snapshot_at"]
    assert not ({r["result_id"] for r in first["items"]}
                & {r["result_id"] for r in second["items"]})
    last = await store.query_results_page_counted(
        status="value", limit=2, cursor=second["next_cursor"]
    )
    assert len(last["items"]) == 1
    assert last["total_pages"] == 3
    assert last["next_cursor"] is None
    empty = await store.query_results_page_counted(status="skipped", limit=2)
    assert (empty["total_count"], empty["total_pages"]) == (0, 1)


@pytest.mark.asyncio
async def test_result_totals_exclude_superseded_and_deleted(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    old = await _seed_auto_result(store)
    current = await _seed_auto_result(store, supersedes_result_id=old)
    page = await store.query_results_page_counted(limit=500)
    assert page["total_count"] == 1
    assert page["total_pages"] == 1
    assert [r["result_id"] for r in page["items"]] == [current]
    await store.record_deletion_tombstone({
        "subject_kind": "result", "subject_id": current,
    })
    deleted = await store.query_results_page_counted(limit=20)
    assert deleted["items"] == []
    assert (deleted["total_count"], deleted["total_pages"]) == (0, 1)


@pytest.mark.asyncio
async def test_evaluation_scene_filters_rows_totals_and_cursor(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    online = await _seed_auto_result(store)
    human = await _seed_human_result(store)
    experiments = {
        await _seed_auto_result(store, experiment_id="experiment-one")
        for _ in range(3)
    }
    page = await store.query_results_page_counted(evaluation_scene="experiment", limit=2)
    assert (page["total_count"], page["total_pages"]) == (3, 2)
    last = await store.query_results_page_counted(
        evaluation_scene="experiment", limit=2, cursor=page["next_cursor"],
    )
    assert {r["result_id"] for r in page["items"] + last["items"]} == experiments
    assert (last["total_count"], last["total_pages"]) == (3, 2)
    online_page = await store.query_results_page_counted(evaluation_scene="online")
    assert {r["result_id"] for r in online_page["items"]} == {online, human}
    assert online_page["total_count"] == 2
    assert (await store.query_results_page_counted())["total_count"] == 5
    assert (await store.query_results_page_counted(
        evaluation_scene="online", experiment_id="experiment-one",
    ))["total_count"] == 0
    with pytest.raises(EvaluationValidationError):
        await store.query_results_page_counted(evaluation_scene="online", cursor=page["next_cursor"])
    with pytest.raises(EvaluationValidationError):
        await store.query_results_page_counted(evaluation_scene="invalid")


@pytest.mark.asyncio
async def test_scene_unset_preserves_existing_cursor_fingerprint(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    for _ in range(2):
        await _seed_auto_result(store)
    page = await store.query_results_page_counted(limit=1)
    token = json.loads(base64.urlsafe_b64decode(page["next_cursor"]))
    old_filters = dict.fromkeys((
        "target_service", "target_kind", "target_subject_id", "target_subject_revision",
        "definition_id", "definition_version", "experiment_id", "status", "provenance",
        "created_from", "created_to",
    ))
    old_fingerprint = hashlib.sha256(json.dumps(
        old_filters, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()[:16]
    assert token["fp"] == old_fingerprint
    next_page = await store.query_results_page_counted(limit=1, cursor=page["next_cursor"])
    assert len(next_page["items"]) == 1


@pytest.mark.asyncio
async def test_result_page_projects_session_and_run_from_actual_target(tmp_path):
    store = SQLiteEvaluationStore(str(tmp_path / "evaluation.db"))
    target = {"service": "n-agent", "kind": "agent_run", "subject_id": "subject-not-session",
              "subject_revision": "v1", "session_id": "session-one", "run_id": "run-one"}
    automatic = await _seed_auto_result(store, target=target)
    human = await _seed_human_result(store, target=target)
    missing = await _seed_auto_result(store)
    rows = {row["result_id"]: row for row in (await store.query_results_page_counted())["items"]}
    for result_id in (automatic, human):
        assert rows[result_id]["session_id"] == "session-one"
        assert rows[result_id]["run_id"] == "run-one"
        assert rows[result_id]["target_service"] == "n-agent"
    assert rows[missing]["session_id"] is None
    assert rows[missing]["run_id"] is None
