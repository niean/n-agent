"""后台执行请求的幂等、入队前校验、配置冻结和恢复合同。"""
import importlib.util
from types import SimpleNamespace

import pytest

from app.application.evaluation_service import EvaluationService
from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from app.infrastructure.evaluation.execution_store import SQLiteExecutionStore


class Review:
    async def get_dataset(self, dataset_id, version):
        return {"schema_version": "1", "dataset_id": "demo", "dataset_version": "1",
                "dataset_hash": "a" * 64, "target_kind": "agent_run", "cases": [
                    {"case_id": "one", "target_kind": "agent_run", "input": {"message": "hello"},
                     "expected_output": "hello"}]}


class Runner:
    def __init__(self):
        self.calls = 0

    async def execute(self, request, *, on_started=None):
        self.calls += 1
        await on_started(SimpleNamespace(experiment_id="exp-one"))
        return SimpleNamespace(items=[], cleanup_failures=0)


@pytest.fixture
async def configured(tmp_path):
    assert importlib.util.find_spec("app.application.evaluation_execution"), "execution service capability missing"
    from app.application.evaluation_execution import EvaluationExecutionService
    ledger = SQLiteEvaluationStore(str(tmp_path / "eval.db"))
    await EvaluationService(ledger, enabled=True).register_preset_definitions(judge_model="judge")
    store = SQLiteExecutionStore(str(tmp_path / "eval.db"))
    ready = {"ready": True, "judge_ready": True, "worker_ready": True,
             "config_fingerprint": "fixed", "target_model": "target", "judge_model": "judge"}

    async def readiness():
        return dict(ready)

    runner = Runner()
    service = EvaluationExecutionService(store=store, ledger=ledger, review=Review(), runner=runner,
                                         readiness=readiness, poll_interval=0.01, scoring_timeout=0.03)
    return service, runner, store, ready


def payload():
    return {"name": "demo run", "dataset_id": "demo", "dataset_version": "1", "dataset_hash": "a" * 64,
            "request_key": "submission-one", "replicates": 1,
            "evaluator_set": [{"definition_id": "agent.answer_correctness", "definition_version": "experiment-v1"}]}


async def test_submit_is_idempotent_and_projects_private_state(configured):
    service, runner, store, ready = configured
    one = await service.submit(payload())
    ready["ready"] = False
    again = await service.submit(payload())
    assert one["execution_id"] == again["execution_id"]
    assert "payload" not in one and "owner_token" not in one and "config_fingerprint" not in str(one)
    assert runner.calls == 0
    from app.domain.evaluation.errors import EvaluationConflictError
    with pytest.raises(EvaluationConflictError):
        await service.submit({**payload(), "name": "changed"})


@pytest.mark.parametrize("change", [{"replicates": True}, {"replicates": 11}, {"dataset_hash": "b" * 64},
                                    {"evaluator_set": []}, {"target_config": {"url": "evil"}}])
async def test_invalid_admission_never_executes(configured, change):
    service, runner, store, _ = configured
    from app.domain.evaluation.models import EvaluationValidationError
    with pytest.raises(EvaluationValidationError):
        await service.submit({**payload(), **change})
    assert not (await service.list())["items"]
    assert runner.calls == 0


async def test_run_completes_without_claiming_quality_pass(configured):
    service, runner, store, _ = configured
    one = await service.submit(payload())
    await service.run_once()
    result = await service.get(one["execution_id"])
    assert result["phase"] == "completed" and result["experiment_id"] == "exp-one"
    assert result["quality_counts"]["value"] == 0
    assert "passed" not in result
    await service.run_once()
    assert runner.calls == 1


async def test_schema2_detail_never_returns_cached_answer_after_source_deletion():
    from app.application.evaluation_execution import EvaluationExecutionService
    class Store:
        async def get(self, execution_id):
            return {"execution_id": execution_id, "experiment_id": None,
                    "payload": {"manifest": {"schema_version": "2"}},
                    "report": {"items": [{"answer": "stale cached secret", "evidence_sample_ids": ["s1"],
                                           "evidence_expires_at": 200}]}}
    class Ledger:
        deleted = False
        async def is_sample_evidence_deleted(self, sample_id): return self.deleted
        async def load_sample(self, sample_id):
            return {"snapshot_ref": "ev1", "captured_at": "1970-01-01T00:01:20+00:00"}
    class Evidence:
        after_read = None
        async def load_snapshot(self, ref):
            if self.after_read:
                self.after_read()
            return SimpleNamespace(content={"answer": "live answer"})
    ledger = Ledger()
    evidence = Evidence()
    service = EvaluationExecutionService(store=Store(), ledger=ledger, evidence=evidence, review=None,
                                         runner=None, readiness=None, clock=lambda: 100)
    assert (await service.get("e"))["report"]["items"][0]["answer"] == "live answer"
    ledger.deleted = True
    assert (await service.get("e"))["report"]["items"][0]["answer"] is None
    ledger.deleted = False
    service.clock = lambda: 201
    assert (await service.get("e"))["report"]["items"][0]["answer"] is None
    service.clock = lambda: 100
    service.evidence_retention_seconds = 10
    assert (await service.get("e"))["report"]["items"][0]["answer"] is None
    service.evidence_retention_seconds = 7 * 86400
    evidence.after_read = lambda: setattr(service, "clock", lambda: 201)
    assert (await service.get("e"))["report"]["items"][0]["answer"] is None


async def test_config_change_does_not_run_target(configured):
    service, runner, store, ready = configured
    one = await service.submit(payload())
    ready["config_fingerprint"] = "changed"
    await service.run_once()
    result = await service.get(one["execution_id"])
    assert result["phase"] == "failed" and result["error_code"] == "configuration_changed"
    assert runner.calls == 0


async def test_scoring_recovery_never_runs_target(configured):
    service, runner, store, _ = configured
    import time
    one = await service.submit(payload())
    row = await store.claim(now=time.time())
    await store.update(one["execution_id"], row["owner_token"], now=time.time(), phase="scoring", experiment_id="exp-one")
    await store.release_scoring(one["execution_id"], row["owner_token"], now=time.time())
    await service.run_once()
    assert (await service.get(one["execution_id"]))["phase"] == "completed"
    assert runner.calls == 0


async def test_unsupported_metric_is_rejected_before_target(configured):
    from dataclasses import replace
    from app.application.evaluation_service import definition_from_mapping
    from app.domain.evaluation.models import EvaluationValidationError
    service, runner, _, _ = configured
    row = await service.ledger.get_definition("agent.answer_correctness", "experiment-v1")
    definition = replace(definition_from_mapping(row["definition"]), definition_id="unknown", metric_name="unknown",
                         method="deterministic", judge_model=None, judge_params={}, output_type="boolean", range_or_categories=None)
    await EvaluationService(service.ledger, enabled=True).register_definition(definition)
    assert not any(d["definition_id"] == "unknown" for d in (await service.capabilities())["definitions"])
    with pytest.raises(EvaluationValidationError):
        await service.submit({**payload(), "evaluator_set": [{"definition_id": "unknown", "definition_version": "experiment-v1"}]})
    assert runner.calls == 0


async def test_shutdown_interrupts_owned_target(configured):
    import asyncio
    service, runner, _, _ = configured
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    runner.execute = blocked
    one = await service.submit(payload())
    await service.start()
    await asyncio.wait_for(entered.wait(), 1)
    await service.stop()
    assert (await service.get(one["execution_id"]))["phase"] == "interrupted"


async def test_scoring_timeout_keeps_experiment_and_fails(configured, monkeypatch):
    service, runner, _, _ = configured

    async def pending(experiment_id):
        return {"queued": 1}

    service.ledger.experiment_job_status_counts = pending
    # T5 冻结预算：scoring_window 在 submit 时刻按场景常量冻结进预算，
    # 服务级 scoring_timeout 仅作缺省——测试缩短的是预算常量本身
    monkeypatch.setattr(
        "app.application.evaluation_execution.SCENARIO_SCORING_WINDOW_SECONDS",
        0.03,
    )
    one = await service.submit(payload())
    await service.run_once()
    result = await service.get(one["execution_id"])
    assert result["phase"] == "failed" and result["error_code"] == "execution_timeout"
    assert result["experiment_id"] == "exp-one" and result["quality_counts"]["pending"] == 1


async def test_cancellation_during_claim_settles_committed_claim(configured):
    import asyncio
    service, _, store, _ = configured
    one = await service.submit(payload())
    committed, release = asyncio.Event(), asyncio.Event()
    original = store.claim

    async def delayed(**kwargs):
        row = await original(**kwargs)
        committed.set()
        await release.wait()
        return row

    store.claim = delayed
    task = asyncio.create_task(service.run_once())
    await committed.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.get(one["execution_id"]))["phase"] == "interrupted"
