# app.application.evaluation_gate 用例测试（plan T14，spec 9.2 / 10）。
#
# TDD 严格 RED -> GREEN；本文件覆盖 plan T14 S1 全部要点：
# - gate_policy 版本化 JSON 加载与 fail-fast 校验（schema/version/字段/
#   阈值范围/未知字段）；
# - 安全/工具约束用例必须全部通过；
# - reference-dependent 核心指标覆盖率 100%（分母 = manifest 运行前声明
#   适用的 case；运行后证据缺失不从分母剔除）；
# - 阈值门禁：task_success>=0.90（业务 error 计失败）、recall_at_5>=0.80、
#   ndcg_at_5>=0.75、faithfulness>=0.90；按适用 case 宏平均；
# - 配对 bootstrap 95% 区间下界 >= -0.02（同 dataset/evaluator/Judge 配置、
#   预指定 baseline；replicate 按 case 聚类重采样、固定种子确定性）；
# - 比例报告 Wilson 95% 区间；
# - 样本不足（<100 配对或关键切片<10）-> insufficient_data -> inconclusive；
# - 四态 gate_pass/gate_fail/inconclusive/infrastructure_error 与退出码
#   0/1/3/2 映射；
# - 未校准指标仅 advisory、必需门禁不得 pass（T8 联通断言：真实服务路径
#   ——真实 SQLite 账本 + EvaluationService + 账本分数读取 + 门禁编排）；
# - Langfuse 不可用不阻断（delivery pending 单列）；
# - 固定输入复算一致（确定性断言）。
from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from app.application.evaluation_experiment import (
    ExperimentItemRecord,
    ExperimentRunReport,
)
from app.application.evaluation_gate import (
    GATE_FAIL,
    GATE_PASS,
    INFRASTRUCTURE_ERROR,
    INCONCLUSIVE,
    WILSON_Z_95,
    GATE_STATE_EXIT_CODES,
    GateEvaluationRequest,
    GatePolicyError,
    GateScore,
    EvaluationGate,
    collect_experiment_scores,
    default_gate_policy,
    gate_data_from_report,
    gate_policy_content,
    gate_policy_from_content,
    load_gate_policy,
    paired_bootstrap_ci_lower,
    wilson_interval,
    bootstrap_lower_index,
)
from app.application.evaluation_service import (
    DefinitionKey,
    EvaluationService,
)
from app.domain.evaluation.models import (
    DatasetCase,
    DatasetManifest,
    EvaluationValidationError,
    canonical_json_bytes,
)
from app.infrastructure.evaluation.store import SQLiteEvaluationStore


# ---------------------------------------------------------------------------
# 测试材料
# ---------------------------------------------------------------------------


def _policy_content(**overrides) -> dict:
    """默认策略内容（spec 9.2 初始阈值）；overrides 覆盖顶层字段。"""
    content = {
        "schema_version": "1",
        "policy_version": "default-v1",
        "required_metrics": {
            "agent.task_success": 0.90,
            "kb.recall_at_5": 0.80,
            "kb.ndcg_at_5": 0.75,
            "rag.faithfulness": 0.90,
        },
        "coverage_metrics": [
            "agent.task_success",
            "kb.recall_at_5",
            "kb.ndcg_at_5",
            "rag.faithfulness",
        ],
        "safety_slices": [],
        "key_slices": [],
        "non_inferiority_margin": -0.02,
        "min_cases": 100,
        "min_paired_cases": 100,
        "min_key_slice_cases": 10,
        "bootstrap_replicates": 300,
        "bootstrap_seed": 20261111,
    }
    content.update(overrides)
    return content


def _write_policy(tmp_path: Path, content: dict) -> Path:
    path = tmp_path / "gate-policy.json"
    path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return path


def _case(
    case_id: str,
    *,
    slice_: str = "",
    no_answer: bool = False,
    applicability: dict | None = None,
    target_kind: str = "agent_run",
) -> DatasetCase:
    return DatasetCase(
        case_id=case_id,
        input={"message": "hello"},
        target_kind=target_kind,
        slice=slice_,
        expected_output="ok",
        no_answer=no_answer,
        metric_applicability=applicability,
    )


def _manifest(cases: tuple[DatasetCase, ...], dataset_id: str = "ds-1") -> DatasetManifest:
    return DatasetManifest(
        schema_version="1",
        dataset_id=dataset_id,
        dataset_version="v1",
        dataset_hash="a" * 64,
        target_kind=cases[0].target_kind,
        cases=cases,
    )


def _item(
    case_id: str,
    *,
    replicate: int = 0,
    outcome: str = "scored",
    sample_ids: tuple[str, ...] = (),
    item_id: str | None = None,
) -> ExperimentItemRecord:
    return ExperimentItemRecord(
        item_id=item_id or f"{case_id}#r{replicate}",
        case_id=case_id,
        replicate=replicate,
        outcome=outcome,
        analysis_available=False,
        root_trace_id=None,
        root_span_id=None,
        sample_ids=sample_ids,
        attempts=(),
        cleanup_status="not_required",
    )


def _score(
    metric_name: str,
    value,
    *,
    provenance: str = "automatic",
    status: str = "value",
):
    return GateScore(
        metric_name=metric_name,
        status=status,
        value=value,
        passed=value if isinstance(value, bool) and status == "value" else None,
        provenance=provenance,
    )


def _report(
    items: tuple[ExperimentItemRecord, ...],
    metrics: tuple[str, ...],
    *,
    experiment_id: str = "exp-1",
    frozen: dict | None = None,
) -> ExperimentRunReport:
    return ExperimentRunReport(
        experiment_id=experiment_id,
        mode="execute",
        name="gate-it",
        frozen_snapshot=frozen
        if frozen is not None
        else {
            "mode": "execute",
            "dataset_hash": "a" * 64,
            "judge_config_hash": "j" * 64,
            "evaluator_set_hash": "e" * 64,
        },
        metrics=metrics,
        items=items,
    )


def _success_experiment(
    *,
    passed: int = 100,
    failed: int = 0,
    business_error: int = 0,
    metric: str = "agent.task_success",
    experiment_id: str = "exp-1",
):
    """构造 task_success 实验材料：passed 通过 + failed 失败 + business_error
    个业务 error 项（无任何评分）。返回 (report, dataset, scores_by_sample)。"""
    total = passed + failed + business_error
    cases = tuple(_case(f"c{i:03d}") for i in range(total))
    items: list[ExperimentItemRecord] = []
    scores: dict[str, dict] = {}
    index = 0
    for _ in range(passed):
        sample_id = f"s{index:03d}"
        scores[sample_id] = {metric: _score(metric, True)}
        items.append(_item(f"c{index:03d}", sample_ids=(sample_id,)))
        index += 1
    for _ in range(failed):
        sample_id = f"s{index:03d}"
        scores[sample_id] = {metric: _score(metric, False)}
        items.append(_item(f"c{index:03d}", sample_ids=(sample_id,)))
        index += 1
    for _ in range(business_error):
        items.append(_item(f"c{index:03d}", outcome="business_error"))
        index += 1
    report = _report(tuple(items), (metric,), experiment_id=experiment_id)
    return report, _manifest(cases), scores


# ---------------------------------------------------------------------------
# gate_policy 版本化 JSON 加载与校验
# ---------------------------------------------------------------------------


class TestGatePolicyLoading:
    def test_load_valid_policy_round_trip(self, tmp_path):
        path = _write_policy(tmp_path, _policy_content())
        policy = load_gate_policy(path)
        assert policy.policy_version == "default-v1"
        assert policy.required_metrics["agent.task_success"] == 0.90
        assert policy.required_metrics["kb.recall_at_5"] == 0.80
        assert policy.required_metrics["kb.ndcg_at_5"] == 0.75
        assert policy.required_metrics["rag.faithfulness"] == 0.90
        assert policy.non_inferiority_margin == -0.02
        assert policy.min_cases == 100
        assert policy.min_paired_cases == 100
        assert policy.min_key_slice_cases == 10

    def test_default_policy_matches_spec_thresholds(self):
        content = gate_policy_content(default_gate_policy())
        assert content["required_metrics"] == {
            "agent.task_success": 0.90,
            "kb.recall_at_5": 0.80,
            "kb.ndcg_at_5": 0.75,
            "rag.faithfulness": 0.90,
        }
        assert content["non_inferiority_margin"] == -0.02

    def test_unknown_schema_version_rejected(self, tmp_path):
        path = _write_policy(tmp_path, _policy_content(schema_version="2"))
        with pytest.raises(GatePolicyError):
            load_gate_policy(path)

    def test_missing_required_field_rejected(self, tmp_path):
        content = _policy_content()
        del content["required_metrics"]
        path = _write_policy(tmp_path, content)
        with pytest.raises(GatePolicyError):
            load_gate_policy(path)

    def test_unknown_field_rejected(self, tmp_path):
        path = _write_policy(tmp_path, _policy_content(thresh0ld=0.9))
        with pytest.raises(GatePolicyError):
            load_gate_policy(path)

    def test_threshold_out_of_range_rejected(self, tmp_path):
        content = _policy_content()
        content["required_metrics"]["agent.task_success"] = 1.5
        path = _write_policy(tmp_path, content)
        with pytest.raises(GatePolicyError):
            load_gate_policy(path)

    def test_positive_margin_rejected(self, tmp_path):
        path = _write_policy(tmp_path, _policy_content(non_inferiority_margin=0.01))
        with pytest.raises(GatePolicyError):
            load_gate_policy(path)

    def test_non_object_json_rejected(self, tmp_path):
        path = tmp_path / "gate-policy.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(GatePolicyError):
            load_gate_policy(path)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(GatePolicyError):
            load_gate_policy(tmp_path / "absent.json")


# ---------------------------------------------------------------------------
# 纯统计：Wilson 区间与配对 bootstrap
# ---------------------------------------------------------------------------


class TestWilsonInterval:
    def test_known_value_81_of_90(self):
        # 独立闭式计算核对（z=1.95996, p=0.9, n=90）
        low, high = wilson_interval(81, 90, z=WILSON_Z_95)
        assert low == pytest.approx(0.8207, abs=1e-3)
        assert high == pytest.approx(0.9465, abs=1e-3)

    def test_zero_of_ten(self):
        low, high = wilson_interval(0, 10, z=WILSON_Z_95)
        assert low == 0.0
        # 上界 = z^2/(n+z^2)
        zsq = WILSON_Z_95**2
        assert high == pytest.approx(zsq / (10 + zsq), abs=1e-9)

    def test_all_of_ten(self):
        low, high = wilson_interval(10, 10, z=WILSON_Z_95)
        assert high == pytest.approx(1.0)
        # 下界 = n/(n+z^2)
        zsq = WILSON_Z_95**2
        assert low == pytest.approx(10 / (10 + zsq), abs=1e-9)

    def test_invalid_inputs_rejected(self):
        with pytest.raises(ValueError):
            wilson_interval(-1, 10)
        with pytest.raises(ValueError):
            wilson_interval(3, 0)
        with pytest.raises(ValueError):
            wilson_interval(11, 10)


class TestPairedBootstrap:
    def test_constant_differences_lower_equals_constant(self):
        case_diffs = {f"c{i:03d}": (0.5,) for i in range(100)}
        lower = paired_bootstrap_ci_lower(
            case_diffs, seed=20261111, replicates=200
        )
        assert lower == pytest.approx(0.5)

    def test_deterministic_same_seed(self):
        case_diffs = {
            f"c{i:03d}": (float(i % 3) - 0.5,) for i in range(100)
        }
        first = paired_bootstrap_ci_lower(
            case_diffs, seed=7, replicates=300
        )
        second = paired_bootstrap_ci_lower(
            case_diffs, seed=7, replicates=300
        )
        assert first == second

    def test_replicates_stay_clustered_by_case(self):
        # 单 case 携带 [1.0, -1.0] 两个 replicate 配对差：按 case 聚类重采样
        # 时每次抽到的都是同一个 cluster，统计量恒为 0；若把 replicate 当
        # 独立样本重采样则可能抽到 [1,1] 或 [-1,-1]，下界会偏离 0。
        lower = paired_bootstrap_ci_lower(
            {"only": (1.0, -1.0)}, seed=42, replicates=500
        )
        assert lower == pytest.approx(0.0)

    def test_empty_input_rejected(self):
        with pytest.raises(ValueError):
            paired_bootstrap_ci_lower({}, seed=1, replicates=10)

    def test_non_finite_difference_rejected(self):
        with pytest.raises(ValueError):
            paired_bootstrap_ci_lower({"c": (float("nan"),)}, seed=1, replicates=10)

    def test_lower_index_nearest_rank_convention(self):
        # 分位数取 nearest-rank：索引 = ceil(q*n) - 1（0 基）。n=1000、
        # q=0.025 -> ceil(25)-1 = 24（int 截断会取 25，略偏不保守）；
        # q*n 整除时同样取第 q*n 小：n=40 -> 索引 0（int 截断会取 1）。
        assert bootstrap_lower_index(1000) == 24
        assert bootstrap_lower_index(40) == 0
        assert bootstrap_lower_index(1) == 0

    def test_lower_index_rejects_non_positive_replicates(self):
        with pytest.raises(ValueError):
            bootstrap_lower_index(0)

    def test_single_replicate_returns_that_statistic(self):
        # 单轮重采样（tiny-paired-n 极端）：下界即该轮统计量
        lower = paired_bootstrap_ci_lower(
            {f"c{i:03d}": (0.3,) for i in range(50)},
            seed=11,
            replicates=1,
        )
        assert lower == pytest.approx(0.3)

    def test_bootstrap_lower_uses_nearest_rank_index(self, monkeypatch):
        # 端到端锁定 nearest-rank 约定：replicates=40（q*n=1.0 整除）取
        # 排序后第 1 小（索引 0）；旧 int 截断实现取索引 1。脚本化 RNG
        # 让最小统计量恰好出现一次（首轮抽中簇 b，其余各轮混抽 a/b），
        # 两种索引结果可区分（-1.0 vs 0.0）。
        import app.domain.evaluation.gate_stats as gate_stats

        class _ScriptedRandom:
            def __init__(self, script):
                self._draws = iter(script)

            def randrange(self, n):
                return next(self._draws)

        script = [1, 1] + [0, 1] * 39
        monkeypatch.setattr(
            gate_stats.random, "Random", lambda seed: _ScriptedRandom(script)
        )
        lower = paired_bootstrap_ci_lower(
            {"a": (1.0,), "b": (-1.0,)}, seed=0, replicates=40
        )
        assert lower == -1.0


# ---------------------------------------------------------------------------
# 阈值门禁（宏平均 / 业务 error 计失败 / 覆盖率分母纪律）
# ---------------------------------------------------------------------------


def _evaluate(report, dataset, scores, *, policy=None, **kwargs):
    gate = EvaluationGate()
    request = GateEvaluationRequest(
        candidate=gate_data_from_report(report, dataset, scores),
        policy=policy or load_gate_policy_from_content(),
        **kwargs,
    )
    return _run_gate(gate.evaluate(request))


def _run_gate(coro):
    """同步执行门禁评估；async 测试内运行于独立线程（仓库 _run_coroutine
    同一模式），避免嵌套事件循环。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def load_gate_policy_from_content(**overrides):
    """从默认内容构造策略对象（绕开文件 IO 的测试辅助）。"""
    return gate_policy_from_content(_policy_content(**overrides))


def _narrow_policy(metric: str, threshold: float = 0.90):
    """单指标窄策略：实验只评估该指标时的合法门禁（必需指标缺位
    修复后，默认 4 指标策略对单指标实验不再可能 gate_pass）。"""
    return load_gate_policy_from_content(
        required_metrics={metric: threshold},
        coverage_metrics=[metric],
    )


class TestThresholdGate:
    def test_all_pass_yields_gate_pass(self):
        report, dataset, scores = _success_experiment(passed=100)
        conclusion = _evaluate(
            report, dataset, scores, policy=_narrow_policy("agent.task_success")
        )
        assert conclusion.state == GATE_PASS
        assert conclusion.reasons == ()
        assert conclusion.insufficient_data is False
        # 比例（boolean 指标通过率）同时报告 Wilson 95% 区间
        metric = conclusion.metrics[0]
        assert metric.wilson_ci is not None
        assert metric.wilson_ci[0] <= 1.0 and metric.wilson_ci[1] <= 1.0
        assert metric.wilson_ci[0] < 1.0
        assert metric.passed_count == 100
        assert metric.coverage == pytest.approx(1.0)

    def test_task_success_below_threshold_fails(self):
        # 89 通过 + 10 失败 + 1 业务 error = 0.89 < 0.90；业务 error 不从
        # 分母剔除、计任务失败（无评分 -> 0 贡献）
        report, dataset, scores = _success_experiment(
            passed=89, failed=10, business_error=1
        )
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.state == GATE_FAIL
        metric = conclusion.metrics[0]
        assert metric.metric_name == "agent.task_success"
        assert metric.macro_value == pytest.approx(0.89)
        assert metric.denominator == 100
        assert metric.business_failures == 1
        assert any(
            reason.startswith("threshold_below") for reason in conclusion.reasons
        )

    def test_task_success_at_threshold_passes(self):
        # 90 通过 + 9 失败 + 1 业务 error = 0.90：恰好达到阈值按通过计
        report, dataset, scores = _success_experiment(
            passed=90, failed=9, business_error=1
        )
        conclusion = _evaluate(
            report, dataset, scores, policy=_narrow_policy("agent.task_success")
        )
        assert conclusion.state == GATE_PASS

    def test_macro_average_over_cases_not_items(self):
        # replicate=3 的单 case：两次成功一次失败 -> case 值 2/3；宏平均按
        # case 计（不是按 item 池化）——配合 99 个全通过 case：
        # 宏平均 = (99 + 2/3) / 100 = 0.9967
        report, dataset, scores = _success_experiment(passed=99)
        items = list(report.items)
        for replicate, value in ((0, True), (1, True), (2, False)):
            sample_id = f"rep-{replicate}"
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", value)
            }
            items.append(
                _item(
                    "c-rep",
                    replicate=replicate,
                    sample_ids=(sample_id,),
                )
            )
        cases = list(dataset.cases) + [_case("c-rep")]
        dataset = _manifest(tuple(cases))
        report = _report(tuple(items), report.metrics)
        conclusion = _evaluate(report, dataset, scores)
        metric = conclusion.metrics[0]
        assert metric.denominator == 100
        assert metric.macro_value == pytest.approx((99 + 2 / 3) / 100)

    def test_kb_metric_thresholds(self):
        # recall_at_5=0.79 < 0.80 -> fail；ndcg_at_5=0.76 >= 0.75 -> 该项通过
        cases = tuple(_case(f"c{i:03d}") for i in range(100))
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "kb.recall_at_5": _score("kb.recall_at_5", 0.79),
                "kb.ndcg_at_5": _score("kb.ndcg_at_5", 0.76),
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("kb.recall_at_5", "kb.ndcg_at_5"))
        conclusion = _evaluate(report, _manifest(cases), scores)
        assert conclusion.state == GATE_FAIL
        by_metric = {m.metric_name: m for m in conclusion.metrics}
        assert by_metric["kb.recall_at_5"].macro_value == pytest.approx(0.79)
        assert by_metric["kb.ndcg_at_5"].macro_value == pytest.approx(0.76)
        assert not any(
            reason.startswith("threshold_below:kb.ndcg_at_5")
            for reason in conclusion.reasons
        )

    def test_faithfulness_threshold(self):
        cases = tuple(_case(f"c{i:03d}") for i in range(100))
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {"rag.faithfulness": _score("rag.faithfulness", 0.91)}
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("rag.faithfulness",))
        conclusion = _evaluate(
            report, _manifest(cases), scores, policy=_narrow_policy("rag.faithfulness")
        )
        assert conclusion.state == GATE_PASS

    def test_policy_metric_absent_from_experiment_inconclusive(self):
        # 实验只测 task_success：policy 其余必需指标无任何结果 -> 不得
        # gate_pass（spec 9.2 必需指标不能缺位，AC15 关键缺失不能显示
        # 通过）；按证据不足阻断，需补齐指标或改用窄策略
        report, dataset, scores = _success_experiment(passed=100)
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.state == INCONCLUSIVE
        assert any(
            reason.startswith("missing_metric:") for reason in conclusion.reasons
        )
        assert [m.metric_name for m in conclusion.metrics] == [
            "agent.task_success"
        ]


class TestCoverageGate:
    def test_evidence_missing_not_excluded_from_denominator(self):
        # 100 个适用 case，1 个运行后证据缺失（无评分）：分母仍为 100，
        # 覆盖率 0.99 -> gate_fail（不得用 skipped 补齐）
        report, dataset, scores = _success_experiment(passed=99)
        items = list(report.items) + [_item("c-missing")]
        cases = list(dataset.cases) + [_case("c-missing")]
        report = _report(tuple(items), report.metrics)
        conclusion = _evaluate(report, _manifest(tuple(cases)), scores)
        assert conclusion.state == GATE_FAIL
        metric = conclusion.metrics[0]
        assert metric.denominator == 100
        assert metric.value_count == 99
        assert metric.coverage == pytest.approx(0.99)
        assert any(
            reason.startswith("coverage_incomplete") for reason in conclusion.reasons
        )

    def test_no_answer_case_excluded_only_when_pre_declared(self):
        # no_answer case 预声明 recall 不适用：不入 recall 分母（运行前声明）
        cases = [_case(f"c{i:03d}") for i in range(100)]
        cases.append(
            _case(
                "c-no-answer",
                no_answer=True,
                applicability={"kb.recall_at_5": False},
                target_kind="retrieval",
            )
        )
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {"kb.recall_at_5": _score("kb.recall_at_5", 0.9)}
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("kb.recall_at_5",))
        conclusion = _evaluate(
            report, _manifest(tuple(cases)), scores,
            policy=_narrow_policy("kb.recall_at_5"),
        )
        metric = conclusion.metrics[0]
        assert metric.denominator == 100
        assert conclusion.state == GATE_PASS


class TestSafetyCases:
    def test_safety_case_failure_blocks_gate(self):
        cases = [_case(f"c{i:03d}") for i in range(100)]
        cases[0] = _case("c000", slice_="safety")
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            value = i != 0
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", value)
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        policy = load_gate_policy_from_content(safety_slices=["safety"])
        conclusion = _evaluate(report, _manifest(tuple(cases)), scores, policy=policy)
        assert conclusion.state == GATE_FAIL
        assert any(
            reason.startswith("safety_case_failed") for reason in conclusion.reasons
        )

    def test_tool_constraint_case_all_pass_required(self):
        # 安全切片 case 全部通过：不产生 safety 原因（整体宏平均仍 >= 阈值）
        cases = [_case(f"c{i:03d}") for i in range(100)]
        for i in range(3):
            cases[i] = _case(f"c{i:03d}", slice_="tool-constraint")
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", True)
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        policy = load_gate_policy_from_content(
            safety_slices=["tool-constraint"],
            required_metrics={"agent.task_success": 0.90},
            coverage_metrics=["agent.task_success"],
        )
        conclusion = _evaluate(report, _manifest(tuple(cases)), scores, policy=policy)
        assert conclusion.state == GATE_PASS
        assert conclusion.reasons == ()


class TestAdvisoryGate:
    def test_advisory_only_metric_cannot_pass_gate(self):
        # 必需指标的全部评分为 advisory（calibration）：即使数值高于阈值，
        # 必需门禁也不得 pass（spec 9.2/10）
        report, dataset, scores = _success_experiment(passed=100)
        scores = {
            sample_id: {
                metric: _score(metric, score.value, provenance="calibration")
                for metric, score in per_sample.items()
            }
            for sample_id, per_sample in scores.items()
        }
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.state != GATE_PASS
        assert conclusion.state == INCONCLUSIVE
        assert any(
            reason.startswith("advisory_only") for reason in conclusion.reasons
        )
        metric = conclusion.metrics[0]
        assert metric.advisory_only is True
        assert metric.gate_eligible is False

    def test_self_judge_provenance_is_advisory(self):
        report, dataset, scores = _success_experiment(passed=100)
        scores = {
            sample_id: {
                metric: _score(metric, score.value, provenance="self_judge")
                for metric, score in per_sample.items()
            }
            for sample_id, per_sample in scores.items()
        }
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.state == INCONCLUSIVE

    def test_gate_eligible_evidence_satisfies_gate(self):
        # 对照：同一材料 provenance=automatic（gate-eligible）-> gate_pass
        report, dataset, scores = _success_experiment(passed=100)
        conclusion = _evaluate(
            report, dataset, scores, policy=_narrow_policy("agent.task_success")
        )
        assert conclusion.state == GATE_PASS

    def test_advisory_safety_case_is_inconclusive_not_fail(self):
        # advisory 指标命中 safety_slices：safety case 不可判定 ->
        # inconclusive（safety_case_advisory），不得伪称硬失败 gate_fail
        cases = [_case(f"c{i:03d}") for i in range(100)]
        cases[0] = _case("c000", slice_="safety")
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "agent.task_success": _score(
                    "agent.task_success", True, provenance="calibration"
                )
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        policy = load_gate_policy_from_content(safety_slices=["safety"])
        conclusion = _evaluate(report, _manifest(tuple(cases)), scores, policy=policy)
        assert conclusion.state == INCONCLUSIVE
        assert conclusion.state != GATE_FAIL
        assert any(
            reason.startswith("safety_case_advisory")
            for reason in conclusion.reasons
        )
        assert not any(
            reason.startswith("safety_case_failed")
            for reason in conclusion.reasons
        )

    def test_eligible_evidence_safety_case_still_fails_gate(self):
        # 对照：gate-eligible 证据下同一 safety case 失败仍是硬失败
        cases = [_case(f"c{i:03d}") for i in range(100)]
        cases[0] = _case("c000", slice_="safety")
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            value = i != 0
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", value)
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        policy = load_gate_policy_from_content(safety_slices=["safety"])
        conclusion = _evaluate(report, _manifest(tuple(cases)), scores, policy=policy)
        assert conclusion.state == GATE_FAIL
        assert any(
            reason.startswith("safety_case_failed")
            for reason in conclusion.reasons
        )


class TestInfrastructureError:
    def test_judge_error_blocks_gate_as_infrastructure_error(self):
        report, dataset, scores = _success_experiment(passed=100)
        sample_id = "s000"
        scores[sample_id]["agent.task_success"] = _score(
            "agent.task_success", None, status="error"
        )
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.state == INFRASTRUCTURE_ERROR
        assert any(
            reason.startswith("judge_error") for reason in conclusion.reasons
        )

    def test_outcome_judge_error_blocks_gate(self):
        report, dataset, scores = _success_experiment(passed=100)
        data = gate_data_from_report(
            report,
            dataset,
            scores,
            evaluation_outcomes={"c000#r0": {"agent.task_success": "judge_error"}},
        )
        gate = EvaluationGate()
        conclusion = _run_gate(
            gate.evaluate(
                GateEvaluationRequest(
                    candidate=data, policy=load_gate_policy_from_content()
                )
            )
        )
        assert conclusion.state == INFRASTRUCTURE_ERROR

    def test_isolated_item_error_is_infrastructure_error(self):
        report, dataset, scores = _success_experiment(passed=99)
        items = list(report.items) + [_item("c-error", outcome="error")]
        cases = list(dataset.cases) + [_case("c-error")]
        report = _report(tuple(items), report.metrics)
        conclusion = _evaluate(report, _manifest(tuple(cases)), scores)
        assert conclusion.state == INFRASTRUCTURE_ERROR

    def test_infrastructure_error_dominates_gate_fail(self):
        # Judge error（infra）与阈值失败（gate_fail）并存：
        # infrastructure_error > gate_fail（状态收敛优先级组合锁定），
        # reasons 中 infra 段先于 fail 段
        report, dataset, scores = _success_experiment(passed=50, failed=49)
        scores["s000"]["agent.task_success"] = _score(
            "agent.task_success", None, status="error"
        )
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.state == INFRASTRUCTURE_ERROR
        reasons = conclusion.reasons
        assert any(r.startswith("threshold_below") for r in reasons)
        assert reasons[0].startswith("judge_error")
        assert reasons.index(
            next(r for r in reasons if r.startswith("judge_error"))
        ) < reasons.index(
            next(r for r in reasons if r.startswith("threshold_below"))
        )


class TestInsufficientData:
    def test_required_metric_zero_denominator_is_inconclusive_not_pass(
        self,
    ):
        # 策略必需指标分母为 0（manifest 预声明全部不适用）：即使其他
        # 指标规模达标且全部通过，也不得 gate_pass（fail-open 禁止），
        # 结论为 inconclusive（no_applicable_cases，默认阻断）
        cases = tuple(
            _case(f"c{i:03d}", applicability={"rag.faithfulness": False})
            for i in range(100)
        )
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", True)
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(
            tuple(items), ("agent.task_success", "rag.faithfulness")
        )
        conclusion = _evaluate(report, _manifest(cases), scores)
        assert conclusion.state == INCONCLUSIVE
        assert conclusion.state != GATE_PASS
        assert conclusion.state != GATE_FAIL
        assert "no_applicable_cases:rag.faithfulness" in conclusion.reasons
        assert conclusion.insufficient_data is False
        rag_report = next(
            m for m in conclusion.metrics
            if m.metric_name == "rag.faithfulness"
        )
        assert rag_report.denominator == 0

    def test_typo_required_metric_name_blocked_by_min_cases_backstop(self):
        # 策略里敲错的必需指标名不在实验指标集内 -> 静默跳过评估（无
        # 指标级 reason 可报）；min_cases 规模门槛兜底：小数据集不得
        # gate_pass（锁 backstop，防止 typo 静默放行"质量已证明"）
        policy = load_gate_policy_from_content(
            required_metrics={"agent.task_succes": 0.90},
            coverage_metrics=[],
        )
        report, dataset, scores = _success_experiment(passed=99)
        conclusion = _evaluate(report, dataset, scores, policy=policy)
        assert conclusion.state == INCONCLUSIVE
        assert conclusion.state != GATE_PASS
        assert conclusion.insufficient_data is True
        assert any(
            reason.startswith("insufficient_data:total_cases")
            for reason in conclusion.reasons
        )

    def test_below_min_cases_is_insufficient_data(self):
        report, dataset, scores = _success_experiment(passed=99)
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.state == INCONCLUSIVE
        assert conclusion.insufficient_data is True
        assert any(
            reason.startswith("insufficient_data") for reason in conclusion.reasons
        )

    def test_key_slice_below_minimum_is_insufficient_data(self):
        # 100 case 但关键切片只有 5 例 -> insufficient_data
        cases = tuple(_case(f"c{i:03d}") for i in range(95)) + tuple(
            _case(f"g{i:03d}", slice_="gold") for i in range(5)
        )
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", True)
            }
            items.append(_item(cases[i].case_id, sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        policy = load_gate_policy_from_content(
            key_slices=["gold"],
            required_metrics={"agent.task_success": 0.90},
            coverage_metrics=["agent.task_success"],
        )
        conclusion = _evaluate(report, _manifest(cases), scores, policy=policy)
        assert conclusion.state == INCONCLUSIVE
        assert conclusion.insufficient_data is True
        assert any(
            reason.startswith("insufficient_data:key_slice")
            for reason in conclusion.reasons
        )

    def test_key_slice_sufficient_passes(self):
        cases = tuple(_case(f"c{i:03d}") for i in range(90)) + tuple(
            _case(f"g{i:03d}", slice_="gold") for i in range(10)
        )
        scores: dict[str, dict] = {}
        items = []
        for i in range(100):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", True)
            }
            items.append(_item(cases[i].case_id, sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        policy = load_gate_policy_from_content(
            key_slices=["gold"],
            required_metrics={"agent.task_success": 0.90},
            coverage_metrics=["agent.task_success"],
        )
        conclusion = _evaluate(report, _manifest(cases), scores, policy=policy)
        assert conclusion.state == GATE_PASS

    def test_fail_reason_dominates_inconclusive_reason(self):
        # 同时存在硬失败（阈值不达）与不可判定（规模不足）：
        # gate_fail > inconclusive（状态收敛优先级锁定）
        cases = tuple(_case(f"c{i:03d}") for i in range(99))
        scores: dict[str, dict] = {}
        items = []
        for i in range(99):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "agent.task_success": _score(
                    "agent.task_success", i % 2 == 0
                )
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        conclusion = _evaluate(report, _manifest(cases), scores)
        assert conclusion.state == GATE_FAIL
        reasons = conclusion.reasons
        assert any(r.startswith("threshold_below") for r in reasons)
        assert any(
            r.startswith("insufficient_data:total_cases") for r in reasons
        )
        assert reasons.index(
            next(r for r in reasons if r.startswith("threshold_below"))
        ) < reasons.index(
            next(
                r
                for r in reasons
                if r.startswith("insufficient_data")
            )
        )


# ---------------------------------------------------------------------------
# 配对 bootstrap 非劣比较（预指定 baseline，同配置）
# ---------------------------------------------------------------------------


class TestBaselineComparison:
    def test_config_mismatch_rejected(self):
        report, dataset, scores = _success_experiment(passed=100)
        baseline = _baseline_data_with_values(0.0)
        from app.domain.evaluation.models import EvaluationValidationError

        drifted = dataclasses.replace(
            baseline,
            frozen_snapshot={
                **baseline.frozen_snapshot,
                "judge_config_hash": "f" * 64,
            },
        )
        with pytest.raises(EvaluationValidationError):
            _evaluate(
                report,
                dataset,
                scores,
                baseline=drifted,
            )

    def test_baseline_missing_judge_config_hash_rejected(self):
        # Judge 配置比较不可跳过：基线快照缺 judge_config_hash ->
        # fail-closed（spec 9.2 "相同 Judge 配置"与 dataset_hash 同一强度）
        from app.application.evaluation_gate import BaselineConfigMismatchError
        from app.domain.evaluation.models import EvaluationValidationError

        report, dataset, scores = _success_experiment(passed=100)
        baseline = _baseline_data_with_values(0.0)
        snapshot = {
            name: value
            for name, value in baseline.frozen_snapshot.items()
            if name != "judge_config_hash"
        }
        drifted = dataclasses.replace(
            baseline, frozen_snapshot=snapshot
        )
        with pytest.raises((BaselineConfigMismatchError, EvaluationValidationError)):
            _evaluate(report, dataset, scores, baseline=drifted)

    def test_candidate_better_passes_non_inferiority(self):
        report, dataset, scores = _success_experiment(passed=100)
        baseline = _baseline_data_with_values(0.0)
        conclusion = _evaluate(
            report, dataset, scores, baseline=baseline,
            policy=_narrow_policy("agent.task_success"),
        )
        assert conclusion.state == GATE_PASS
        metric = conclusion.metrics[0]
        assert metric.paired_cases == 100
        assert metric.non_inferiority_ok is True
        assert metric.bootstrap_ci_lower is not None
        assert metric.bootstrap_ci_lower >= -0.02

    def test_candidate_worse_is_inconclusive_not_fail(self):
        # 候选 90 通过 10 失败、基线全通过：配对差为 0/-1，bootstrap 下界
        # 必然 < -0.02 -> 区间无法证明非劣 -> inconclusive（默认阻断），
        # 不是 gate_fail（spec 9.2）
        report, dataset, scores = _success_experiment(passed=90, failed=10)
        baseline = _baseline_data_with_values(1.0)
        conclusion = _evaluate(report, dataset, scores, baseline=baseline)
        assert conclusion.state == INCONCLUSIVE
        assert any(
            reason.startswith("non_inferiority_unproven")
            for reason in conclusion.reasons
        )
        metric = conclusion.metrics[0]
        assert metric.non_inferiority_ok is False
        assert metric.bootstrap_ci_lower < -0.02

    def test_insufficient_paired_cases(self):
        # 99 个配对 case（< 100）-> insufficient_data
        cases = tuple(_case(f"c{i:03d}") for i in range(99))
        scores: dict[str, dict] = {}
        items = []
        for i in range(99):
            sample_id = f"s{i:03d}"
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", True)
            }
            items.append(_item(f"c{i:03d}", sample_ids=(sample_id,)))
        report = _report(tuple(items), ("agent.task_success",))
        baseline = _baseline_data_with_values(0.0, cases=99)
        conclusion = _evaluate(report, _manifest(cases), scores, baseline=baseline)
        assert conclusion.state == INCONCLUSIVE
        assert conclusion.insufficient_data is True
        assert any(
            reason.startswith("insufficient_data:paired")
            for reason in conclusion.reasons
        )


def _baseline_data_with_values(
    value: float, *, cases: int = 100, experiment_id: str = "exp-base"
):
    """构造基线实验数据：case_id 与候选一致，metric 值恒为 value。"""
    case_tuples = tuple(_case(f"c{i:03d}") for i in range(cases))
    scores: dict[str, dict] = {}
    items = []
    for i in range(cases):
        sample_id = f"base-{i:03d}"
        scores[sample_id] = {
            "agent.task_success": _score("agent.task_success", bool(value))
        }
        items.append(
            _item(
                case_tuples[i].case_id,
                sample_ids=(sample_id,),
                item_id=f"base-{i:03d}#r0",
            )
        )
    report = _report(
        tuple(items),
        ("agent.task_success",),
        experiment_id=experiment_id,
    )
    return gate_data_from_report(report, _manifest(case_tuples, dataset_id="ds-base"), scores)


# ---------------------------------------------------------------------------
# 四态与退出码、Langfuse 投递单列、确定性
# ---------------------------------------------------------------------------


class TestStatesAndExitCodes:
    def test_exit_code_mapping(self):
        assert GATE_STATE_EXIT_CODES == {
            GATE_PASS: 0,
            GATE_FAIL: 1,
            INCONCLUSIVE: 3,
            INFRASTRUCTURE_ERROR: 2,
        }

    def test_states_are_closed_vocabulary(self):
        assert set(GATE_STATE_EXIT_CODES) == {
            "gate_pass",
            "gate_fail",
            "inconclusive",
            "infrastructure_error",
        }

    def test_to_mapping_stable_fields(self):
        report, dataset, scores = _success_experiment(passed=100)
        conclusion = _evaluate(
            report, dataset, scores, policy=_narrow_policy("agent.task_success")
        )
        mapping = conclusion.to_mapping()
        assert mapping["state"] == "gate_pass"
        assert mapping["experiment_id"] == "exp-1"
        assert mapping["policy_version"] == "default-v1"
        assert mapping["insufficient_data"] is False
        assert mapping["delivery_status_counts"] == {}
        assert mapping["denominator_basis"] == "pre_declared"
        metric = mapping["metrics"][0]
        for field in (
            "metric_name",
            "threshold",
            "macro_value",
            "denominator",
            "passed_count",
            "value_count",
            "coverage",
            "wilson_ci",
            "gate_eligible",
            "advisory_only",
            "business_failures",
            "judge_errors",
            "paired_cases",
            "bootstrap_ci_lower",
            "non_inferiority_ok",
        ):
            assert field in metric


class TestDeliveryIndependence:
    def test_langfuse_pending_does_not_block_gate(self):
        # Langfuse 不可用（delivery 全部 pending）：本地实验完整仍得出门禁
        # 结论，投递状态单列展示
        report, dataset, scores = _success_experiment(passed=100)
        conclusion = _evaluate(
            report,
            dataset,
            scores,
            delivery_status_counts={"langfuse": {"pending": 100, "verified": 0}},
            policy=_narrow_policy("agent.task_success"),
        )
        assert conclusion.state == GATE_PASS
        assert conclusion.to_mapping()["delivery_status_counts"] == {
            "langfuse": {"pending": 100, "verified": 0}
        }


class TestReplicateEvidence:
    """复制实验缺证据 replicate 不得被已到证据掩盖（spec 9.1 禁止
    挑选成功结果；24 项三方审阅 #3）。"""

    def test_partial_replicate_evidence_not_masked(self):
        # replicate=3 单 case：仅 1 个 replicate 有成功评分 -> case 值
        # 不得按 1.0 计（修复前宏平均虚高、覆盖率虚满）
        report, dataset, scores = _success_experiment(passed=99)
        items = list(report.items)
        for replicate in range(3):
            if replicate == 0:
                sample_id = "rep-0"
                scores[sample_id] = {
                    "agent.task_success": _score("agent.task_success", True)
                }
                items.append(
                    _item("c-rep", replicate=0, sample_ids=(sample_id,))
                )
            else:
                items.append(_item("c-rep", replicate=replicate))
        cases = list(dataset.cases) + [_case("c-rep")]
        report = _report(tuple(items), report.metrics)
        conclusion = _evaluate(
            report, _manifest(tuple(cases)), scores,
            policy=_narrow_policy("agent.task_success"),
        )
        metric = conclusion.metrics[0]
        assert metric.denominator == 100
        assert metric.macro_value == pytest.approx(0.99)
        assert metric.coverage == pytest.approx(0.99)
        assert conclusion.state == GATE_FAIL
        assert any(
            r.startswith("coverage_incomplete") for r in conclusion.reasons
        )

    def test_full_replicates_still_average(self):
        # 对照：三个 replicate 全部有证据 -> case 值 = 均值（2/3），
        # 既有宏平均语义不变
        report, dataset, scores = _success_experiment(passed=99)
        items = list(report.items)
        for replicate, value in ((0, True), (1, True), (2, False)):
            sample_id = f"rep-{replicate}"
            scores[sample_id] = {
                "agent.task_success": _score("agent.task_success", value)
            }
            items.append(
                _item("c-rep", replicate=replicate, sample_ids=(sample_id,))
            )
        cases = list(dataset.cases) + [_case("c-rep")]
        report = _report(tuple(items), report.metrics)
        conclusion = _evaluate(
            report, _manifest(tuple(cases)), scores,
            policy=_narrow_policy("agent.task_success"),
        )
        metric = conclusion.metrics[0]
        assert metric.macro_value == pytest.approx((99 + 2 / 3) / 100)
        assert metric.coverage == pytest.approx(1.0)


class TestDenominatorBasis:
    """覆盖率分母口径单列（spec 复审 minor 3）：manifest 预声明 vs 账本
    观测近似必须可被消费方甄别。"""

    def test_collect_gate_data_recovers_manifest_case_universe(self):
        # compare 账本路径：experiment 记录冻结 manifest_json case 全集
        # 时，运行后无结果的 case 不得从分母剔除（24 项三方审阅 #4）
        from app.application.evaluation_gate import collect_gate_data

        class FakeManifestLedger:
            async def query_results_page(self, **kwargs):
                return (
                    [
                        {
                            "result_id": "r1",
                            "job_id": "j1",
                            "metric_name": "agent.task_success",
                            "status": "value",
                            "typed_value": True,
                            "passed": True,
                            "provenance": "automatic",
                        }
                    ],
                    None,
                )

            async def get_job(self, job_id):
                return {
                    "sample_id": "s1",
                    "definition_id": "agent.task_success",
                    "definition_version": "v1",
                }

            async def load_sample(self, sample_id):
                return {
                    "producer_event_id": "experiment:exp-1:c000#r0:root"
                }

            async def get_experiment(self, experiment_id):
                return {
                    "dataset_hash": "a" * 64,
                    "evaluator_set_hash": "e" * 64,
                    "target_config_hash": "t" * 64,
                    "manifest_json": json.dumps(
                        {
                            "schema_version": "1",
                            "dataset_id": "ds-1",
                            "dataset_version": "v1",
                            "dataset_hash": "a" * 64,
                            "target_kind": "agent_run",
                            "case_ids": ["c000", "c001", "c002"],
                        }
                    ),
                }

            async def get_definition(self, definition_id, version):
                return {"definition": {}}

        data = _run_gate(collect_gate_data(FakeManifestLedger(), "exp-1"))
        # 无结果 case（c001/c002）保留在分母
        assert data.applicable_cases["agent.task_success"] == (
            "c000",
            "c001",
            "c002",
        )
        assert data.denominator_basis == "pre_declared"
        assert data.expected_replicates == {"c000": 1}

    def test_collect_gate_data_corrupt_manifest_json_rejected(self):
        from app.application.evaluation_gate import collect_gate_data

        class FakeCorruptLedger:
            async def query_results_page(self, **kwargs):
                return ([], None)

            async def get_job(self, job_id):
                return {}

            async def load_sample(self, sample_id):
                return {}

            async def get_experiment(self, experiment_id):
                return {
                    "dataset_hash": "a" * 64,
                    "manifest_json": "{not-json",
                }

        with pytest.raises(EvaluationValidationError):
            _run_gate(collect_gate_data(FakeCorruptLedger(), "exp-1"))

    def test_collect_gate_data_observed_replicates_detected(self):
        # 复制实验 compare 路径：观测到 3 个 replicate 下标 -> 预期
        # replicate 数为 3，缺证据 replicate 不被掩盖
        from app.application.evaluation_gate import collect_gate_data

        rows = []
        for replicate, (status, value) in enumerate(
            [("value", True), ("error", None)]
        ):
            rows.append(
                {
                    "result_id": f"r{replicate}",
                    "job_id": f"j{replicate}",
                    "metric_name": "agent.task_success",
                    "status": status,
                    "typed_value": value,
                    "passed": None,
                    "provenance": "automatic",
                }
            )

        class FakeReplicaLedger:
            async def query_results_page(self, **kwargs):
                return (rows, None)

            async def get_job(self, job_id):
                index = int(job_id[1:])
                return {
                    "sample_id": f"s{index}",
                    "definition_id": "agent.task_success",
                    "definition_version": "v1",
                }

            async def load_sample(self, sample_id):
                index = int(sample_id[1:])
                return {
                    "producer_event_id": (
                        f"experiment:exp-1:c000#r{index}:root"
                    )
                }

            async def get_experiment(self, experiment_id):
                return {"dataset_hash": "a" * 64, "manifest_json": ""}

            async def get_definition(self, definition_id, version):
                return {"definition": {}}

        data = _run_gate(collect_gate_data(FakeReplicaLedger(), "exp-1"))
        assert data.expected_replicates == {"c000": 2}
        scores = data.scores[("c000", "agent.task_success")]
        assert len(scores) == 2

    def test_report_with_dataset_is_pre_declared(self):
        report, dataset, scores = _success_experiment(passed=100)
        conclusion = _evaluate(report, dataset, scores)
        assert conclusion.to_mapping()["denominator_basis"] == "pre_declared"

    def test_report_without_dataset_is_observed(self):
        report, dataset, scores = _success_experiment(passed=100)
        conclusion = _evaluate(report, None, scores)
        assert conclusion.to_mapping()["denominator_basis"] == "observed"

    def test_collect_gate_data_marks_observed_and_rebuilds_snapshot(self):
        # compare 账本路径：denominator_basis=observed；冻结快照从
        # experiment 记录 + 观测到的定义重建（配置一致性 fail-closed 依赖）
        import hashlib

        from app.application.evaluation_gate import collect_gate_data

        class FakeCompareLedger:
            async def query_results_page(self, **kwargs):
                return (
                    [
                        {
                            "result_id": "r1",
                            "job_id": "j1",
                            "metric_name": "rag.faithfulness",
                            "status": "value",
                            "typed_value": 0.95,
                            "passed": None,
                            "provenance": "automatic",
                        }
                    ],
                    None,
                )

            async def get_job(self, job_id):
                return {
                    "sample_id": "s1",
                    "definition_id": "rag.faithfulness",
                    "definition_version": "v1",
                }

            async def load_sample(self, sample_id):
                return {
                    "producer_event_id": "experiment:exp-1:c000#r0:root"
                }

            async def get_experiment(self, experiment_id):
                return {
                    "dataset_hash": "a" * 64,
                    "evaluator_set_hash": "e" * 64,
                    "target_config_hash": "t" * 64,
                }

            async def get_definition(self, definition_id, version):
                return {
                    "definition": {
                        "judge_model": "judge-model",
                        "judge_params": {"temperature": 0},
                    }
                }

        data = _run_gate(collect_gate_data(FakeCompareLedger(), "exp-1"))
        assert data.denominator_basis == "observed"
        assert data.case_slices == {}
        assert data.applicable_cases["rag.faithfulness"] == ("c000",)
        expected_judge_hash = hashlib.sha256(
            canonical_json_bytes(
                [
                    {
                        "definition_id": "rag.faithfulness",
                        "version": "v1",
                        "judge_model": "judge-model",
                        "judge_params": {"temperature": 0},
                    }
                ]
            )
        ).hexdigest()
        assert data.frozen_snapshot["judge_config_hash"] == expected_judge_hash
        assert data.frozen_snapshot["dataset_hash"] == "a" * 64
        assert data.frozen_snapshot["evaluator_set_hash"] == "e" * 64

    def test_invalid_denominator_basis_rejected(self):
        from app.application.evaluation_gate import GateExperimentData

        with pytest.raises(EvaluationValidationError):
            GateExperimentData(
                experiment_id="e", metrics=(), denominator_basis="guessed"
            )


class TestDeterminism:
    def test_same_inputs_same_conclusion(self):
        report, dataset, scores = _success_experiment(
            passed=90, failed=9, business_error=1
        )
        first = _evaluate(report, dataset, scores)
        second = _evaluate(report, dataset, scores)
        assert first.to_mapping() == second.to_mapping()

    def test_same_inputs_same_conclusion_with_baseline(self):
        report, dataset, scores = _success_experiment(passed=100)
        baseline = _baseline_data_with_values(0.0)
        first = _evaluate(report, dataset, scores, baseline=baseline)
        second = _evaluate(report, dataset, scores, baseline=baseline)
        assert first.to_mapping() == second.to_mapping()


# ---------------------------------------------------------------------------
# T8 联通断言（真实服务路径）：advisory 指标不得使必需门禁 pass
# ---------------------------------------------------------------------------


async def _real_path_scores(tmp_path: Path, provenance: str):
    """真实服务路径材料：注册 llm_judge 定义 -> EvaluationService.submit_sample
    展开作业 -> 按 worker 语义写入指定 provenance 的结果行 -> 账本分数。"""
    store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
    service = EvaluationService(store, enabled=True)
    definition = await store.save_definition(
        {
            "definition_id": "rag.faithfulness",
            "version": "v1",
            "metric_name": "rag.faithfulness",
            "target_kind": "experiment_item",
            "method": "llm_judge",
            "required_fields": [],
            "evidence_policy": "snapshot_complete",
            "output_type": "number",
            "range_or_categories": [0.0, 1.0],
            "direction": "higher_is_better",
            "material_hash": {},
            "judge_model": "judge-model",
            "judge_params": {"temperature": 0},
            "timeout_seconds": 30,
            "budget": None,
            "ci_admissible": True,
        }
    )
    assert definition
    from app.application.evaluation_service import SubmitSampleRequest
    from app.domain.evaluation.models import EvaluationTarget

    scores: dict[str, dict] = {}
    sample_ids: list[str] = []
    for i in range(100):
        submitted = await service.submit_sample(
            SubmitSampleRequest(
                producer_event_id=f"experiment:exp-1:c{i:03d}#r0:root",
                service="n-agent",
                target=EvaluationTarget(
                    service="n-agent",
                    kind="experiment_item",
                    subject_id=f"c{i:03d}#r0",
                    subject_revision="v1",
                ),
                definitions=(
                    DefinitionKey(
                        definition_id="rag.faithfulness", definition_version="v1"
                    ),
                ),
                experiment_id="exp-1",
                mode="execute",
                replicate=0,
            )
        )
        sample_ids.append(submitted.sample_id)
        await store.save_result(
            {
                "job_id": submitted.job_ids[0],
                "provenance": provenance,
                "metric_name": "rag.faithfulness",
                "metric_version": "v1",
                "status": "value",
                "skip_reason": None,
                "typed_value": 0.95,
                "passed": None,
                "explanation": "",
                "evidence_hash": "",
            }
        )
    return store, sample_ids


def _real_path_report(sample_ids: tuple[str, ...]) -> ExperimentRunReport:
    items = tuple(
        _item(f"c{i:03d}", sample_ids=(sample_id,))
        for i, sample_id in enumerate(sample_ids)
    )
    return _report(items, ("rag.faithfulness",))


class TestAdvisoryIntegrationRealServicePath:
    """T8 联通断言（plan ~265 行）：未通过校准的指标标 advisory，CI 必需
    门禁不得自动 pass——经真实 SQLite 账本 + EvaluationService + 账本分数
    读取 + 门禁编排端到端验证。"""

    @pytest.mark.asyncio
    async def test_calibration_provenance_blocks_gate(self, tmp_path):
        store, sample_ids = await _real_path_scores(tmp_path, "calibration")
        scores_by_sample = await collect_experiment_scores(store, "exp-1")
        report = _real_path_report(tuple(sample_ids))
        cases = tuple(_case(f"c{i:03d}") for i in range(100))
        conclusion = _evaluate(report, _manifest(cases), scores_by_sample)
        assert conclusion.state != GATE_PASS
        assert conclusion.state == INCONCLUSIVE
        assert any(
            reason.startswith("advisory_only") for reason in conclusion.reasons
        )

    @pytest.mark.asyncio
    async def test_automatic_provenance_passes_same_path(self, tmp_path):
        store, sample_ids = await _real_path_scores(tmp_path, "automatic")
        scores_by_sample = await collect_experiment_scores(store, "exp-1")
        report = _real_path_report(tuple(sample_ids))
        cases = tuple(_case(f"c{i:03d}") for i in range(100))
        conclusion = _evaluate(
            report, _manifest(cases), scores_by_sample,
            policy=_narrow_policy("rag.faithfulness"),
        )
        assert conclusion.state == GATE_PASS
        metric = conclusion.metrics[0]
        assert metric.macro_value == pytest.approx(0.95)
        assert metric.gate_eligible is True
        # faithfulness 宏平均是数值均值而非比例：Wilson 区间仅用于比例
        assert metric.wilson_ci is None

    @pytest.mark.asyncio
    async def test_collect_scores_reads_real_ledger_shape(self, tmp_path):
        store, sample_ids = await _real_path_scores(tmp_path, "automatic")
        scores_by_sample = await collect_experiment_scores(store, "exp-1")
        assert len(scores_by_sample) == 100
        score = scores_by_sample[sample_ids[0]]["rag.faithfulness"]
        assert score.status == "value"
        assert score.value == pytest.approx(0.95)
        assert score.provenance == "automatic"


# ---------------------------------------------------------------------------
# Phase 5 扫描修复（维度 5）：账本读失败不得 fail-open 缩小门禁分母
# ---------------------------------------------------------------------------


class _FailingLoadSampleLedger:
    """最小 compare 账本 fake：load_sample 系统性失败（瞬时存储故障）。"""

    async def query_results_page(self, **kwargs):
        return (
            [
                {
                    "result_id": "r1",
                    "job_id": "j1",
                    "metric_name": "rag.faithfulness",
                    "status": "value",
                    "typed_value": 0.95,
                    "passed": None,
                    "provenance": "automatic",
                }
            ],
            None,
        )

    async def get_job(self, job_id):
        return {
            "sample_id": "s1",
            "definition_id": "rag.faithfulness",
            "definition_version": "v1",
        }

    async def load_sample(self, sample_id):
        raise RuntimeError("transient store failure")


def test_collect_gate_data_sample_read_failure_propagates():
    """Blocking（Phase 5 扫描）：collect_gate_data 的 load_sample 读失败
    必须上抛（CLI 操作失败统一 exit 2 = infrastructure_error 方向），
    不得静默 continue 缩小分母制造虚假 pass——"证据缺失留分母"原则
    只适用于证据缺失，不适用于系统异常吞咽。"""
    import pytest

    from app.application.evaluation_gate import collect_gate_data

    with pytest.raises(RuntimeError, match="transient store failure"):
        _run_gate(collect_gate_data(_FailingLoadSampleLedger(), "exp-1"))
