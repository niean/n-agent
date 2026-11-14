"""T5：场景执行编排、预检、预算与完成条件测试（plan T5 S1）。

覆盖：
- valid != runnable：数据集有效但保留期未配置 / 通道未接入 -> blocked；
- 预检只读无副作用；
- blocked 整单拒绝（submit 复核预检）；
- 显式 case_ids 子集固定分母；必需指标遗漏拒绝；绑定漂移逐题原因；
- 幂等重试；12 题 × 10 次超项数上限拒绝、12 题最大 8 次；
- 持久绝对预算：deadline 在 submit 冻结，重启/重试不刷新；
- completed 前置条件：评分终态 + 活跃资源释放 + 保留登记；释放失败
  进入 failed 并保留可恢复账目。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.application.evaluation_execution import EvaluationExecutionService
from app.application.evaluation_preflight import EvaluationPreflightService
from app.application.evaluation_service import EvaluationService
from app.domain.evaluation.scenario import ScenarioCase
from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from app.infrastructure.evaluation.execution_store import SQLiteExecutionStore


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def scenario_payload(
    *, profile: str = "agent", assertions=None, required=None, bindings=None
) -> dict:
    return {
        "execution": {
            "profile": profile,
            "steps": [
                {"step_id": "turn-1", "action": "chat", "payload": {"message": "hi"}}
            ],
            "timeout_seconds": 600,
        },
        "requirements": {},
        "options": {},
        "assertions": assertions or [
            {"assertion_type": "result_contains", "params": {"text": "hi"},
             "step_id": "turn-1"},
        ],
        "expected_output": {"steps": {"turn-1": {"rubric": "回答包含问候"}}},
        "bindings": bindings or {},
        "lifecycle": {
            "release_rule": "release_active_resources",
            "retention_rule": "retain_evidence_default",
        },
        "required_metrics": required or {"语义正确": "agent.scenario_quality"},
        "source": {
            "repo_path": "tests/e2e/conversations/x.json",
            "source_case_id": "x",
            "source_sha256": "b" * 64,
            "rules_version": "e2e-conversion-v1",
            "transformations": [],
        },
        "comparison_scope": "fixed_fixture",
    }


def scenario_case(case_id: str, **kwargs) -> dict:
    return {
        "case_id": case_id,
        "target_kind": "agent_run",
        "input": {"message": "hi"},
        "expected_output": "hi",
        "scenario": scenario_payload(**kwargs),
    }


def schema2_dataset(count: int = 12) -> dict:
    return {
        "schema_version": "2",
        "dataset_id": "agent-e2e-scenarios",
        "dataset_version": "1",
        "dataset_hash": "c" * 64,
        "target_kind": "agent_run",
        "cases": [scenario_case(f"case-{i:02d}") for i in range(count)],
    }


class Review:
    def __init__(self, raw):
        self._raw = raw

    async def get_dataset(self, dataset_id, version):
        return self._raw


class Runner:
    def __init__(self):
        self.calls = 0

    async def execute(self, request, *, on_started=None):
        self.calls += 1
        await on_started(SimpleNamespace(experiment_id="exp-scenario"))
        return SimpleNamespace(items=[], cleanup_failures=0)


class FakeLedger:
    """completed 前置释放/保留的资源账本替身。"""

    def __init__(self, *, cleanup_failed=0):
        self.calls = []
        self._cleanup_failed = cleanup_failed

    async def register_retention(self, execution_id, *, now=None):
        self.calls.append(("retention", execution_id))
        return 1

    async def release_execution(self, execution_id):
        self.calls.append(("release", execution_id))
        if self._cleanup_failed:
            return {"released": 0, "retained": 1, "cleanup_failed": self._cleanup_failed}
        return {"released": 1, "retained": 1, "cleanup_failed": 0}


async def _ready():
    return {"ready": True, "judge_ready": True, "worker_ready": True}


async def _build(tmp_path, raw, *, preflight=None, resource_ledger=None,
                 replicates_cap=10):
    ledger = SQLiteEvaluationStore(str(tmp_path / "eval.db"))
    await EvaluationService(ledger, enabled=True).register_preset_definitions(
        judge_model="judge"
    )
    store = SQLiteExecutionStore(str(tmp_path / "eval.db"))

    async def readiness():
        return {"ready": True, "judge_ready": True, "worker_ready": True,
                "config_fingerprint": "fixed", "target_model": "target",
                "judge_model": "judge"}

    service = EvaluationExecutionService(
        store=store, ledger=ledger, review=Review(raw), runner=Runner(),
        readiness=readiness, poll_interval=0.01, scoring_timeout=0.03,
        preflight=preflight, resource_ledger=resource_ledger,
        admitted_profiles=("agent",),
    )
    return service, store


def _preflight_evaluators():
    return [
        {"definition_id": "agent.scenario_quality",
         "definition_version": "experiment-v1",
         "metric_name": "agent.scenario_quality", "method": "llm_judge"},
        {"definition_id": "agent.scenario_assertions",
         "definition_version": "experiment-v1",
         "metric_name": "agent.scenario_assertions", "method": "deterministic"},
    ]


def _payload(raw, **overrides):
    body = {
        "name": "scenario run", "dataset_id": raw["dataset_id"],
        "dataset_version": raw["dataset_version"],
        "dataset_hash": raw["dataset_hash"],
        "request_key": "submission-scenario", "replicates": 1,
        "evaluator_set": [
            {"definition_id": "agent.scenario_quality",
             "definition_version": "experiment-v1"},
            {"definition_id": "agent.scenario_assertions",
             "definition_version": "experiment-v1"},
        ],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 预检（valid != runnable、只读、逐题原因、绑定漂移、容量）
# ---------------------------------------------------------------------------


async def test_preflight_valid_but_not_runnable_without_retention(tmp_path):
    raw = schema2_dataset(count=2)
    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=0.0,
    )
    report = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(), replicates=1,
    )
    assert report["runnable"] is False
    assert any("保留期" in reason for reason in report["blocked"])


async def test_preflight_readonly_no_side_effects(tmp_path):
    raw = schema2_dataset(count=2)
    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=3600.0,
    )
    before = len(raw["cases"])
    report = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(), replicates=8,
    )
    # 只读：manifest 原样、无 store 依赖（构造即无写路径）
    assert len(raw["cases"]) == before
    assert report["runnable"] is True
    assert report["item_count"] == 16
    assert report["judge_calls"] == 16  # 1 个 llm_judge × 项数
    assert all(entry["runnable"] for entry in report["cases"])


async def test_preflight_binding_drift_per_case_reason(tmp_path):
    raw = schema2_dataset(count=2)
    raw["cases"][1]["scenario"]["bindings"] = {
        "mem0:facts": {"material": "mem0-namespace:e2e", "sha256": "d" * 64},
    }

    async def material_hashes(dataset_id, version):
        return {"mem0:facts": "e" * 64}  # 与声明 sha256 不一致 -> 漂移

    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=3600.0,
        material_hashes_reader=material_hashes,
    )
    report = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(), replicates=1,
    )
    assert report["runnable"] is False
    by_id = {entry["case_id"]: entry for entry in report["cases"]}
    assert by_id["case-00"]["runnable"] is True
    assert by_id["case-01"]["runnable"] is False
    assert any("绑定漂移" in r for r in by_id["case-01"]["reasons"])


async def test_preflight_missing_required_metric_blocks_case(tmp_path):
    raw = schema2_dataset(count=1)
    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=3600.0,
    )
    report = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=[
            {"definition_id": "agent.scenario_assertions",
             "definition_version": "experiment-v1",
             "metric_name": "agent.scenario_assertions",
             "method": "deterministic"},
        ],
        replicates=1,
    )
    assert report["runnable"] is False
    assert any("必需指标未覆盖" in r for r in report["cases"][0]["reasons"])


async def test_preflight_explicit_subset_fixes_denominator(tmp_path):
    raw = schema2_dataset(count=12)
    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=3600.0,
    )
    report = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(), replicates=8,
        case_ids=["case-00", "case-03"],
    )
    assert report["selected_case_ids"] == ["case-00", "case-03"]
    assert report["item_count"] == 16
    assert report["max_replicates"] == 50  # 100 // 2


async def test_preflight_capacity_12_cases_x10_rejected_x8_allowed(tmp_path):
    raw = schema2_dataset(count=12)
    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=3600.0,
    )
    rejected = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(), replicates=10,
    )
    assert rejected["runnable"] is False
    assert any("超过服务器上限" in r for r in rejected["blocked"])
    allowed = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(), replicates=8,
    )
    assert allowed["runnable"] is True
    assert allowed["item_count"] == 96


async def test_preflight_rejects_unwired_profile(tmp_path):
    raw = schema2_dataset(count=1)
    raw["cases"][0]["scenario"]["execution"]["profile"] = "cli"
    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=3600.0,
    )
    report = await service.preflight(
        dataset_id=raw["dataset_id"], dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(), replicates=1,
    )
    assert report["runnable"] is False
    assert any("cli" in r for r in report["blocked"])


async def test_preflight_subset_not_blocked_by_unselected_profile(tmp_path):
    """显式子集固定分母（spec：任一"选中" case blocked 才整单拒绝）：
    未选中 case 的未接入 profile 不阻塞子集运行；全选时仍阻塞。"""
    raw = schema2_dataset(count=2)
    raw["cases"][0]["scenario"]["execution"]["profile"] = "agent"
    raw["cases"][1]["scenario"]["execution"]["profile"] = "acp"
    service = EvaluationPreflightService(
        review=Review(raw), readiness=_ready, evidence_retention_seconds=3600.0,
    )
    base = dict(
        dataset_id=raw["dataset_id"],
        dataset_version=raw["dataset_version"],
        dataset_hash=raw["dataset_hash"],
        evaluator_set=_preflight_evaluators(),
    )
    subset = await service.preflight(replicates=1, case_ids=["case-00"], **base)
    assert subset["runnable"] is True
    full = await service.preflight(replicates=1, **base)
    assert full["runnable"] is False
    assert any("acp" in r for r in full["blocked"])


# ---------------------------------------------------------------------------
# submit：blocked 整单拒绝、子集固定分母、必需指标、幂等、预算冻结
# ---------------------------------------------------------------------------


async def test_submit_rejects_when_preflight_blocked(tmp_path):
    raw = schema2_dataset(count=2)

    class BlockedPreflight:
        async def preflight(self, **kwargs):
            return {"runnable": False, "blocked": ["证据保留期未配置（retention），拒绝运行场景实验"]}

    service, store = await _build(
        tmp_path, raw, preflight=BlockedPreflight(),
    )
    from app.domain.evaluation.models import EvaluationValidationError

    with pytest.raises(EvaluationValidationError) as exc_info:
        await service.submit(_payload(raw))
    assert "preflight blocked" in str(exc_info.value)


async def test_submit_missing_required_metric_rejected(tmp_path):
    raw = schema2_dataset(count=1)
    service, store = await _build(tmp_path, raw)
    from app.domain.evaluation.models import EvaluationValidationError

    with pytest.raises(EvaluationValidationError) as exc_info:
        await service.submit(_payload(raw, evaluator_set=[
            {"definition_id": "agent.scenario_assertions",
             "definition_version": "experiment-v1"},
        ]))
    assert "必需指标未覆盖" in str(exc_info.value)


async def test_submit_unwired_profile_rejected(tmp_path):
    raw = schema2_dataset(count=1)
    raw["cases"][0]["scenario"]["execution"]["profile"] = "acp"
    service, store = await _build(tmp_path, raw)
    from app.domain.evaluation.models import EvaluationValidationError

    with pytest.raises(EvaluationValidationError) as exc_info:
        await service.submit(_payload(raw))
    assert "acp" in str(exc_info.value)


async def test_submit_subset_not_rejected_by_unselected_profile(tmp_path):
    """submit 准入同源：显式子集运行不得被未选中 case 的未接入 profile
    拒绝（固定分母纪律）。"""
    raw = schema2_dataset(count=2)
    raw["cases"][1]["scenario"]["execution"]["profile"] = "acp"
    service, _store = await _build(tmp_path, raw)
    result = await service.submit(_payload(raw, case_ids=["case-00"]))
    assert result["execution_id"]


async def test_submit_capacity_12x10_rejected_12x8_accepted(tmp_path):
    raw = schema2_dataset(count=12)
    service, store = await _build(tmp_path, raw)
    from app.domain.evaluation.models import EvaluationValidationError

    with pytest.raises(EvaluationValidationError):
        await service.submit(_payload(raw, replicates=10))
    # 12 题最大 8 次（96 <= 100）
    result = await service.submit(_payload(raw, replicates=8))
    assert result["execution_id"]


async def test_submit_explicit_subset_and_frozen_budget(tmp_path):
    raw = schema2_dataset(count=12)
    service, store = await _build(tmp_path, raw)
    body = _payload(raw, replicates=2, case_ids=["case-00", "case-05"])
    result = await service.submit(body)
    row = await store.get(result["execution_id"])
    frozen = row["payload"]
    # 分母 = 显式子集 × replicates
    assert frozen["selected_case_ids"] == ["case-00", "case-05"]
    # 持久绝对预算：deadline 在 submit 冻结（绝对 epoch）
    budget = frozen["budget"]
    assert budget["run_deadline"] > budget["submitted_at"]
    assert budget["scoring_window"] == 900.0
    assert budget["release_window"] == 300.0


async def test_submit_idempotent_same_key_same_payload(tmp_path):
    raw = schema2_dataset(count=1)
    service, store = await _build(tmp_path, raw)
    one = await service.submit(_payload(raw))
    again = await service.submit(_payload(raw))
    assert one["execution_id"] == again["execution_id"]


async def test_case_ids_unknown_or_empty_rejected(tmp_path):
    raw = schema2_dataset(count=2)
    service, store = await _build(tmp_path, raw)
    from app.domain.evaluation.models import EvaluationValidationError

    with pytest.raises(EvaluationValidationError):
        await service.submit(_payload(raw, case_ids=["ghost"]))
    with pytest.raises(EvaluationValidationError):
        await service.submit(_payload(raw, case_ids=[]))


async def test_schema2_dataset_parses_scenario_payload(tmp_path):
    raw = schema2_dataset(count=1)
    service, store = await _build(tmp_path, raw)
    result = await service.submit(_payload(raw))
    row = await store.get(result["execution_id"])
    # frozen manifest 中 scenario 保留（_dataset 不再过滤 schema 2 字段）
    assert row["payload"]["manifest"]["cases"][0]["scenario"]["execution"]["profile"] == "agent"
    # domain VO 构造期校验生效
    ScenarioCase.from_mapping(raw["cases"][0]["scenario"])


# ---------------------------------------------------------------------------
# 完成条件：评分终态 + 释放/保留；释放失败 -> failed（账目可恢复）
# ---------------------------------------------------------------------------


async def test_completion_requires_release_and_retention(tmp_path):
    raw = schema2_dataset(count=1)
    resource_ledger = FakeLedger()
    service, store = await _build(tmp_path, raw, resource_ledger=resource_ledger)
    result = await service.submit(_payload(raw))
    await service.run_once()
    row = await store.get(result["execution_id"])
    assert row["phase"] == "completed"
    assert row["report"]["release"]["state"] == "retention_registered"
    assert ("retention", result["execution_id"]) in resource_ledger.calls
    assert ("release", result["execution_id"]) in resource_ledger.calls


async def test_release_failure_fails_execution_with_recoverable_account(tmp_path):
    raw = schema2_dataset(count=1)
    resource_ledger = FakeLedger(cleanup_failed=1)
    service, store = await _build(tmp_path, raw, resource_ledger=resource_ledger)
    result = await service.submit(_payload(raw))
    await service.run_once()
    row = await store.get(result["execution_id"])
    # 释放失败：failed + 可恢复账目（cleanup_failed 数量在报告中）
    assert row["phase"] == "failed"
    assert row["error_code"] == "resource_release_failed"
    assert row["report"]["release"]["cleanup_failed"] == 1


def test_scenario_payload_roundtrip_through_domain():
    payload = scenario_payload()
    parsed = ScenarioCase.from_mapping(payload)
    assert parsed.execution.profile == "agent"
    assert parsed.required_metrics.coverages == {"语义正确": "agent.scenario_quality"}


async def test_settle_report_does_not_clobber_retained_count(tmp_path):
    """T10 S4 实测：register_retention 先把存储类资源转为 retained，
    release_execution 对已 retained 记录计 0——_settle_resources 的
    ``**outcome`` 展开覆盖了 register_retention 的 retained 计数，报告
    误报 retained=0/state=released。报告必须如实合并两阶段计数。"""

    class ProductionShapeLedger(FakeLedger):
        async def register_retention(self, execution_id, *, now=None):
            self.calls.append(("retention", execution_id))
            return 3

        async def release_execution(self, execution_id):
            self.calls.append(("release", execution_id))
            # 真实账本：retained 已由 register_retention 迁移，此处计 0
            return {"released": 3, "retained": 0, "cleanup_failed": 0}

    raw = schema2_dataset(count=1)
    resource_ledger = ProductionShapeLedger()
    service, store = await _build(tmp_path, raw, resource_ledger=resource_ledger)
    result = await service.submit(_payload(raw))
    await service.run_once()
    row = await store.get(result["execution_id"])
    assert row["phase"] == "completed"
    release = row["report"]["release"]
    assert release["state"] == "retention_registered"
    assert release["retained"] == 3
    assert release["released"] == 3
    assert release["cleanup_failed"] == 0
