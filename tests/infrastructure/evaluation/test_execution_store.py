"""Dashboard 执行账本的持久幂等、并发与租约合同。"""
import asyncio
import importlib.util

import pytest


def _store(tmp_path):
    assert importlib.util.find_spec("app.infrastructure.evaluation.execution_store"), "execution store capability missing"
    from app.infrastructure.evaluation.execution_store import SQLiteExecutionStore
    return SQLiteExecutionStore(str(tmp_path / "eval.db"))


def _request(key="key-1", fingerprint="fingerprint"):
    return {"request_key": key, "request_fingerprint": fingerprint, "name": "experiment",
            "dataset_id": "dataset", "dataset_version": "1", "dataset_hash": "a" * 64,
            "payload": {"example": True}}


async def test_idempotency_and_conflict(tmp_path):
    store = _store(tmp_path)
    one = await store.submit(_request(), now=10)
    assert (await store.submit(_request(), now=11))["execution_id"] == one["execution_id"]
    from app.domain.evaluation.errors import EvaluationConflictError
    with pytest.raises(EvaluationConflictError):
        await store.submit(_request(fingerprint="different"), now=11)
    assert (await store.get(one["execution_id"]))["phase"] == "queued"


async def test_claim_cas_heartbeat_and_expired_target_is_not_replayed(tmp_path):
    store = _store(tmp_path)
    first = await store.submit(_request(), now=10)
    await store.submit(_request("key-2"), now=11)
    claims = await asyncio.gather(store.claim(now=12), store.claim(now=12))
    claimed = next(row for row in claims if row)
    assert sum(row is not None for row in claims) == 1
    from app.domain.evaluation.errors import EvaluationLeaseError
    with pytest.raises(EvaluationLeaseError):
        await store.update(first["execution_id"], "wrong", now=13, phase="failed")
    await store.heartbeat(first["execution_id"], claimed["owner_token"], now=13)
    assert await store.claim(now=14) is None
    second = await store.claim(now=100)
    assert second["execution_id"] != first["execution_id"]
    assert (await store.get(first["execution_id"]))["phase"] == "interrupted"
    with pytest.raises(EvaluationLeaseError):
        await store.update(first["execution_id"], claimed["owner_token"], now=100, phase="completed")


async def test_scoring_recovery_and_pagination(tmp_path):
    store = _store(tmp_path)
    first = await store.submit(_request(), now=10)
    claim = await store.claim(now=11)
    await store.update(first["execution_id"], claim["owner_token"], now=12,
                       phase="scoring", experiment_id="exp-1", report={"items": []})
    recovered = await store.claim(now=100)
    assert recovered["phase"] == "scoring" and recovered["experiment_id"] == "exp-1"
    assert recovered["owner_token"] != claim["owner_token"]
    await store.submit(_request("key-2"), now=101)
    page = await store.list_page(limit=1)
    assert len(page["items"]) == 1 and page["next_cursor"]
    next_page = await store.list_page(limit=1, cursor=page["next_cursor"])
    assert next_page["items"][0]["execution_id"] != page["items"][0]["execution_id"]


@pytest.mark.parametrize("schema", ["1", "2"])
async def test_reopen_scrubs_only_legacy_schema2_answer_cache(tmp_path, schema):
    store = _store(tmp_path)
    request = _request()
    request["payload"] = {"manifest": {"schema_version": schema}}
    row = await store.submit(request, now=10)
    claimed = await store.claim(now=11)
    await store.update(row["execution_id"], claimed["owner_token"], now=12,
                       report={"items": [{"case_id": "case-1", "answer": "cached answer", "answer_truncated": False}]})
    reopened = _store(tmp_path)
    item = (await reopened.get(row["execution_id"]))["report"]["items"][0]
    assert ("answer" in item) is (schema == "1")
    assert item["case_id"] == "case-1"


async def test_admission_capacity_and_reopen(tmp_path):
    store = _store(tmp_path)
    for i in range(20):
        await store.submit(_request(f"key-{i}"), now=10+i)
    from app.domain.evaluation.execution import ExecutionCapacityError
    with pytest.raises(ExecutionCapacityError):
        await store.submit(_request("overflow"), now=40)
    assert (await store.submit(_request("key-0"), now=40))["phase"] == "queued"
    reopened = _store(tmp_path)
    assert len((await reopened.list_page())["items"]) == 20
