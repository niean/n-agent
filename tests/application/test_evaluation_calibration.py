# app.application.evaluation_calibration 用例测试（plan T8，spec 10）。
#
# S1 失败测试：
# - 校准集与回归集分离读取（不同 manifest 路径），>= 50 双人标注后裁决样本
#   才允许计算；content hash 非重叠；覆盖正/负/注入/边界四类
# - boolean：agreement>=0.85 且 Cohen kappa>=0.6；单类 kappa 不可算 →
#   校准失败（不得以 agreement 替代）；numeric 归一化 MAE<=0.15
# - 重复性：20 样本 x 3 次，boolean agreement>=0.90、numeric 最大差均值<=0.10
# - 未通过 → advisory；CI 必需门禁不得自动 pass（is_gate_eligible 接口，
#   T14 消费）；provenance 语义与 worker 落库端到端（calibration/self_judge）
# - definition hash（judge 模型/rubric/截断/证据 schema）变化 → 缓存键变化
#   → 校准失效重算
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

import pytest

from app.application.evaluation_calibration import (
    ADVISORY_PROVENANCES,
    MIN_CALIBRATION_SAMPLES,
    REQUIRED_COVERAGE_CATEGORIES,
    CalibrationError,
    CalibrationManifestError,
    JudgeCalibrator,
    calibration_cache_key,
    calibration_definition_hash,
    calibration_status_from_store,
    compute_calibration_manifest_hash,
    is_gate_eligible,
    load_calibration_set,
    provenance_is_advisory,
)
from app.application.evaluation_service import (
    DefinitionKey,
    EvaluationService,
    SubmitSampleRequest,
)
from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import (
    EvaluatorDefinition,
    EvaluationTarget,
    EvidenceSnapshot,
)
from app.infrastructure.evaluation.store import SQLiteEvaluationStore

# ---------------------------------------------------------------------------
# Manifest 构造 / 加载
# ---------------------------------------------------------------------------


def _sample(
    sample_id: str,
    category: str,
    value: Any,
    *,
    annotators: tuple[str, ...] = ("annotator-a", "annotator-b"),
    adjudicated: bool = True,
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "content_hash": hashlib.sha256(sample_id.encode()).hexdigest(),
        "category": category,
        "adjudicated_value": value,
        "adjudicated": adjudicated,
        "annotations": [
            {"annotator": a, "value": value} for a in annotators
        ],
    }


def _category_cycle(index: int) -> str:
    return REQUIRED_COVERAGE_CATEGORIES[index % len(REQUIRED_COVERAGE_CATEGORIES)]


def _bool_samples(n: int) -> list[dict[str, Any]]:
    """金标均衡 boolean 样本（i 偶数 True / 奇数 False），类别轮转全覆盖。"""
    return [
        _sample(f"s-{i:03d}", _category_cycle(i), (i % 2 == 0))
        for i in range(n)
    ]


def _numeric_samples(n: int, gold: float = 0.5) -> list[dict[str, Any]]:
    return [
        _sample(f"n-{i:03d}", _category_cycle(i), gold) for i in range(n)
    ]


def _manifest_content(
    role: str,
    samples: list[dict[str, Any]],
    *,
    dual_annotation: bool = True,
    set_id: str | None = None,
) -> dict[str, Any]:
    content: dict[str, Any] = {
        "schema_version": "1",
        "set_id": set_id or f"{role}-set-v1",
        "role": role,
        "dual_annotation": dual_annotation,
        "samples": samples,
    }
    content["manifest_hash"] = compute_calibration_manifest_hash(content)
    return content


def _write_set(
    tmp_path, name: str, role: str, samples: list[dict[str, Any]], **overrides
):
    content = _manifest_content(role, samples, **overrides)
    path = tmp_path / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def _write_calibration_pair(
    tmp_path,
    *,
    calibration_samples: list[dict[str, Any]] | None = None,
    regression_samples: list[dict[str, Any]] | None = None,
):
    calibration_path = _write_set(
        tmp_path,
        "calibration",
        "calibration",
        calibration_samples
        if calibration_samples is not None
        else _bool_samples(MIN_CALIBRATION_SAMPLES),
    )
    regression_path = _write_set(
        tmp_path,
        "regression",
        "regression",
        regression_samples
        if regression_samples is not None
        else [
            _sample(f"r-{i:03d}", _category_cycle(i), (i % 2 == 0))
            for i in range(10)
        ],
    )
    return calibration_path, regression_path


class TestCalibrationManifestLoader:
    async def test_load_calibration_set_separate_paths(self, tmp_path):
        """校准集与回归集从不同 manifest 路径独立读取，样本互不相混。"""
        calibration_path, regression_path = _write_calibration_pair(tmp_path)
        assert calibration_path != regression_path
        calibration = load_calibration_set(calibration_path, role="calibration")
        regression = load_calibration_set(regression_path, role="regression")
        assert calibration.role == "calibration"
        assert regression.role == "regression"
        assert len(calibration.samples) == MIN_CALIBRATION_SAMPLES
        assert len(regression.samples) == 10
        calibration_ids = {s.sample_id for s in calibration.samples}
        regression_ids = {s.sample_id for s in regression.samples}
        assert not calibration_ids & regression_ids

    async def test_role_mismatch_rejected(self, tmp_path):
        path = _write_set(tmp_path, "x", "calibration", _bool_samples(5))
        with pytest.raises(CalibrationManifestError, match="role"):
            load_calibration_set(path, role="regression")

    async def test_dual_annotation_declaration_required(self, tmp_path):
        path = _write_set(
            tmp_path,
            "x",
            "calibration",
            _bool_samples(5),
            dual_annotation=False,
        )
        with pytest.raises(CalibrationManifestError, match="dual_annotation"):
            load_calibration_set(path, role="calibration")

    async def test_manifest_hash_tamper_rejected(self, tmp_path):
        path = _write_set(tmp_path, "x", "calibration", _bool_samples(5))
        content = json.loads(path.read_text())
        content["samples"][0]["adjudicated_value"] = not content["samples"][0][
            "adjudicated_value"
        ]
        path.write_text(json.dumps(content))
        with pytest.raises(CalibrationManifestError, match="manifest_hash"):
            load_calibration_set(path, role="calibration")

    async def test_single_annotator_rejected(self, tmp_path):
        samples = [
            _sample(f"s-{i:03d}", _category_cycle(i), True, annotators=("only-a",))
            for i in range(5)
        ]
        path = _write_set(tmp_path, "x", "calibration", samples)
        with pytest.raises(CalibrationManifestError, match="双人"):
            load_calibration_set(path, role="calibration")

    async def test_same_annotator_twice_rejected(self, tmp_path):
        samples = [
            _sample(
                "s-000",
                "positive",
                True,
                annotators=("dup", "dup"),
            )
        ]
        path = _write_set(tmp_path, "x", "calibration", samples)
        with pytest.raises(CalibrationManifestError):
            load_calibration_set(path, role="calibration")

    async def test_not_adjudicated_rejected(self, tmp_path):
        samples = [
            _sample("s-000", "positive", True, adjudicated=False),
            _sample("s-001", "negative", False),
        ]
        path = _write_set(tmp_path, "x", "calibration", samples)
        with pytest.raises(CalibrationManifestError, match="裁决"):
            load_calibration_set(path, role="calibration")

    async def test_unknown_category_rejected(self, tmp_path):
        path = _write_set(
            tmp_path,
            "x",
            "calibration",
            [_sample("s-000", "weird", True)],
        )
        with pytest.raises(CalibrationManifestError, match="category"):
            load_calibration_set(path, role="calibration")

    async def test_duplicate_sample_id_rejected(self, tmp_path):
        samples = [
            _sample("s-000", "positive", True),
            _sample("s-000", "negative", False),
        ]
        path = _write_set(tmp_path, "x", "calibration", samples)
        with pytest.raises(CalibrationManifestError, match="sample_id"):
            load_calibration_set(path, role="calibration")


# ---------------------------------------------------------------------------
# Judge fake 与定义
# ---------------------------------------------------------------------------


class ScriptedJudge:
    """校准 Judge fake：按 (sample_id, 第几次调用) 脚本化输出，记录全部调用。"""

    def __init__(self, script):
        self._script = script
        self.calls: list[dict[str, Any]] = []

    async def evaluate(self, definition, sample):
        call_index = sum(
            1 for c in self.calls if c["sample_id"] == sample.sample_id
        )
        self.calls.append(
            {"sample_id": sample.sample_id, "call_index": call_index}
        )
        outcome = self._script(sample, call_index)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _bool_definition(**overrides) -> EvaluatorDefinition:
    defaults: dict[str, Any] = dict(
        definition_id="agent.answer_correctness",
        version="v1",
        metric_name="agent.answer_correctness",
        target_kind="agent_run",
        method="llm_judge",
        required_fields=(),
        evidence_policy="snapshot",
        output_type="boolean",
        range_or_categories=(),
        direction="higher_is_better",
        material_hash={},
        judge_model="judge-model-x",
        judge_params={},
        timeout_seconds=60,
        budget=None,
        ci_admissible=True,
    )
    defaults.update(overrides)
    return EvaluatorDefinition(**defaults)


def _num_definition(**overrides) -> EvaluatorDefinition:
    fields_override = dict(
        output_type="number",
        range_or_categories=(0.0, 1.0),
        definition_id="rag.faithfulness",
        metric_name="rag.faithfulness",
    )
    fields_override.update(overrides)
    base = _bool_definition()
    fields = dict(
        definition_id=base.definition_id,
        version=base.version,
        metric_name=base.metric_name,
        target_kind=base.target_kind,
        method=base.method,
        required_fields=base.required_fields,
        evidence_policy=base.evidence_policy,
        output_type=base.output_type,
        range_or_categories=base.range_or_categories,
        direction=base.direction,
        material_hash=base.material_hash,
        judge_model=base.judge_model,
        judge_params=base.judge_params,
        timeout_seconds=base.timeout_seconds,
        budget=base.budget,
        ci_admissible=base.ci_admissible,
    )
    fields.update(fields_override)
    return EvaluatorDefinition(**fields)


def _value_outcome(value: Any) -> dict[str, Any]:
    return {"status": "value", "value": value, "explanation": "ok"}


def _matching_bool_judge(gold_by_id: Mapping[str, bool], *, flips: set[str] = frozenset()):
    def script(sample, call_index):
        gold = gold_by_id[sample.sample_id]
        return _value_outcome(not gold if sample.sample_id in flips else gold)

    return ScriptedJudge(script)


def _gold_by_id(samples) -> dict[str, Any]:
    return {s["sample_id"]: s["adjudicated_value"] for s in samples}


# ---------------------------------------------------------------------------
# 校准编排
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))


async def _calibrate(store, definition, tmp_path, judge, **kwargs):
    calibration_path, regression_path = _write_calibration_pair(tmp_path, **kwargs)
    calibrator = JudgeCalibrator(
        definition=definition, judge=judge, report_store=store
    )
    return await calibrator.calibrate(calibration_path, regression_path)


class TestCalibrationPreconditions:
    async def test_below_50_samples_rejected_before_any_judge_call(
        self, store, tmp_path
    ):
        definition = _bool_definition()
        judge = _matching_bool_judge(_gold_by_id(_bool_samples(49)))
        with pytest.raises(CalibrationError, match="50"):
            await _calibrate(
                store, definition, tmp_path, judge,
                calibration_samples=_bool_samples(49),
            )
        assert judge.calls == []

    async def test_same_manifest_path_rejected(self, store, tmp_path):
        definition = _bool_definition()
        judge = _matching_bool_judge({})
        path = _write_set(tmp_path, "both", "calibration", _bool_samples(50))
        calibrator = JudgeCalibrator(
            definition=definition, judge=judge, report_store=store
        )
        with pytest.raises(CalibrationError, match="不同 manifest 路径"):
            await calibrator.calibrate(path, path)
        assert judge.calls == []

    async def test_content_hash_overlap_rejected(self, store, tmp_path):
        definition = _bool_definition()
        shared = _sample("shared-1", "positive", True)
        judge = _matching_bool_judge({})
        with pytest.raises(CalibrationError, match="重叠"):
            await _calibrate(
                store, definition, tmp_path, judge,
                calibration_samples=_bool_samples(50) + [shared],
                regression_samples=[shared],
            )
        assert judge.calls == []

    @pytest.mark.parametrize(
        "missing_category", REQUIRED_COVERAGE_CATEGORIES
    )
    async def test_missing_coverage_category_rejected(
        self, store, tmp_path, missing_category
    ):
        """四类覆盖逐一校验：缺任一类（正/负/注入/边界）都拒绝计算。"""
        definition = _bool_definition()
        judge = _matching_bool_judge({})
        # 其余三类轮转构造 >= 50 样本（先过样本数关，专测覆盖缺失分支）
        others = [
            c for c in REQUIRED_COVERAGE_CATEGORIES if c != missing_category
        ]
        samples = [
            _sample(f"s-{i:03d}", others[i % 3], (i % 2 == 0))
            for i in range(MIN_CALIBRATION_SAMPLES)
        ]
        with pytest.raises(CalibrationError, match=missing_category):
            await _calibrate(
                store, definition, tmp_path, judge, calibration_samples=samples
            )
        assert judge.calls == []

    async def test_regression_set_required(self, store, tmp_path):
        definition = _bool_definition()
        judge = _matching_bool_judge({})
        with pytest.raises(CalibrationError):
            await _calibrate(
                store, definition, tmp_path, judge, regression_samples=[]
            )
        assert judge.calls == []


class TestConsistencyCalibration:
    async def test_boolean_pass(self, store, tmp_path):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(store, definition, tmp_path, judge)
        assert report.status == "passed"
        assert report.failures == ()
        assert report.consistency["agreement"] == pytest.approx(1.0)
        assert report.consistency["kappa"] == pytest.approx(1.0)
        # 重复性（前 20 样本 x 3 次全部与金标一致）
        assert report.repeatability["agreement"] == pytest.approx(1.0)
        # 校准样本判定 50 次 + 重复性 20x3 = 110 次；只跑校准集
        assert len(judge.calls) == MIN_CALIBRATION_SAMPLES + 60
        judged_ids = {c["sample_id"] for c in judge.calls}
        assert judged_ids == {s["sample_id"] for s in samples}

    async def test_report_persisted_with_consistency_and_repeatability(
        self, store, tmp_path
    ):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(store, definition, tmp_path, judge)
        stored = await store.get_calibration_report(report.cache_key)
        assert stored["status"] == "passed"
        assert "consistency" in stored
        assert "repeatability" in stored
        latest = await store.get_latest_calibration_report(
            definition.definition_id, calibration_definition_hash(definition)
        )
        assert latest is not None
        assert latest["cache_key"] == report.cache_key
        assert (
            await store.calibration_status(
                definition.definition_id,
                calibration_definition_hash(definition),
            )
            == "passed"
        )

    async def test_boolean_low_agreement_advisory(self, store, tmp_path):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        # 10 个不一致 → agreement 0.80 < 0.85
        flips = {f"s-{i:03d}" for i in range(10)}
        judge = _matching_bool_judge(_gold_by_id(samples), flips=flips)
        report = await _calibrate(store, definition, tmp_path, judge)
        assert report.status == "advisory"
        assert "agreement_below_threshold" in report.failures
        assert report.consistency["agreement"] == pytest.approx(0.80)

    async def test_boolean_low_kappa_advisory(self, store, tmp_path):
        """agreement 达标但 kappa < 0.6 → advisory（金标倾斜拉低 kappa）。"""
        definition = _bool_definition()
        samples = [
            _sample(f"s-{i:03d}", _category_cycle(i), i < 47)
            for i in range(MIN_CALIBRATION_SAMPLES)
        ]
        # gold 47 True / 3 False；judge 在 True 侧错 3、False 侧对 2：
        # agreement = 0.92 >= 0.85，但 pe 高，kappa≈0.46 < 0.6
        gold_by_id = _gold_by_id(samples)
        flips = {"s-000", "s-001", "s-047"}

        def script(sample, call_index):
            gold = gold_by_id[sample.sample_id]
            return _value_outcome(not gold if sample.sample_id in flips else gold)

        judge = ScriptedJudge(script)
        report = await _calibrate(
            store, definition, tmp_path, judge, calibration_samples=samples
        )
        assert report.consistency["agreement"] == pytest.approx(0.94)
        assert report.consistency["kappa"] < 0.6
        assert report.status == "advisory"
        assert "kappa_below_threshold" in report.failures

    async def test_single_class_kappa_not_substituted_by_agreement(
        self, store, tmp_path
    ):
        """金标单类 → kappa 不可算 → 校准失败，即使 agreement=1.0。"""
        definition = _bool_definition()
        samples = [
            _sample(f"s-{i:03d}", _category_cycle(i), True)
            for i in range(MIN_CALIBRATION_SAMPLES)
        ]
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(
            store, definition, tmp_path, judge, calibration_samples=samples
        )
        assert report.consistency["agreement"] == pytest.approx(1.0)
        assert report.consistency["kappa"] is None
        assert report.status == "advisory"
        assert "kappa_not_computable" in report.failures

    async def test_judge_skipped_counts_as_disagreement(self, store, tmp_path):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        gold_by_id = _gold_by_id(samples)
        skipped_ids = {f"s-{i:03d}" for i in range(8)}

        def script(sample, call_index):
            if sample.sample_id in skipped_ids:
                return {"status": "skipped", "skip_reason": "not_applicable"}
            return _value_outcome(gold_by_id[sample.sample_id])

        judge = ScriptedJudge(script)
        report = await _calibrate(store, definition, tmp_path, judge)
        assert report.consistency["agreement"] == pytest.approx(42 / 50)
        assert report.status == "advisory"
        assert "agreement_below_threshold" in report.failures

    async def test_numeric_nmae_pass_and_boundary(self, store, tmp_path):
        definition = _num_definition()
        judge = ScriptedJudge(lambda sample, idx: _value_outcome(0.5))
        report = await _calibrate(
            store, definition, tmp_path, judge,
            calibration_samples=_numeric_samples(MIN_CALIBRATION_SAMPLES),
        )
        assert report.status == "passed"
        assert report.consistency["normalized_mae"] == 0.0

        # 边界：归一化 MAE 恰为 0.15 → 通过（<=）；金标 0.4 + judge 0.25
        # 产生与上一段不同的数据集内容 → 不同缓存键，不命中上一段报告
        boundary_judge = ScriptedJudge(lambda sample, idx: _value_outcome(0.25))
        boundary_report = await _calibrate(
            store, _num_definition(), tmp_path, boundary_judge,
            calibration_samples=_numeric_samples(MIN_CALIBRATION_SAMPLES, gold=0.4),
        )
        assert boundary_report.status == "passed"
        assert boundary_report.consistency["normalized_mae"] == pytest.approx(0.15)

    async def test_numeric_nmae_above_threshold_advisory(self, store, tmp_path):
        definition = _num_definition()
        judge = ScriptedJudge(lambda sample, idx: _value_outcome(0.8))
        report = await _calibrate(
            store, definition, tmp_path, judge,
            calibration_samples=_numeric_samples(MIN_CALIBRATION_SAMPLES),
        )
        assert report.status == "advisory"
        assert report.consistency["normalized_mae"] == pytest.approx(0.30)
        assert "normalized_mae_above_threshold" in report.failures


class TestRepeatability:
    async def test_repeatability_call_pattern_20x3(self, store, tmp_path):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(store, definition, tmp_path, judge)
        assert report.repeatability["samples"] == 20
        assert report.repeatability["runs"] == 3
        per_sample = {}
        for call in judge.calls:
            per_sample.setdefault(call["sample_id"], []).append(call["call_index"])
        first20 = {s["sample_id"] for s in samples[:20]}
        for sample_id in first20:
            assert per_sample[sample_id] == [0, 1, 2, 3]
        for sample_id, indexes in per_sample.items():
            if sample_id not in first20:
                assert indexes == [0]

    async def test_repeatability_boolean_below_threshold_advisory(
        self, store, tmp_path
    ):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        gold_by_id = _gold_by_id(samples)
        # 前 20 样本中 7 个在第 3 次重复时翻转：该样本 2/3 一致
        # 均值 = (13 * 1.0 + 7 * 2/3) / 20 ≈ 0.883 < 0.90
        flaky = {f"s-{i:03d}" for i in range(7)}

        def script(sample, call_index):
            gold = gold_by_id[sample.sample_id]
            if sample.sample_id in flaky and call_index == 2:
                return _value_outcome(not gold)
            return _value_outcome(gold)

        judge = ScriptedJudge(script)
        report = await _calibrate(store, definition, tmp_path, judge)
        expected = (13 * 1.0 + 7 * (2.0 / 3.0)) / 20
        assert report.repeatability["agreement"] == pytest.approx(expected)
        assert report.status == "advisory"
        assert "repeatability_agreement_below_threshold" in report.failures

    async def test_repeatability_numeric_max_diff_mean_above_threshold(
        self, store, tmp_path
    ):
        definition = _num_definition()

        def script(sample, call_index):
            # 前 20 样本第 3 次重复跳到 0.9：max-diff 0.4，均值 0.4 > 0.10
            if call_index == 2:
                return _value_outcome(0.9)
            return _value_outcome(0.5)

        judge = ScriptedJudge(script)
        report = await _calibrate(
            store, definition, tmp_path, judge,
            calibration_samples=_numeric_samples(MIN_CALIBRATION_SAMPLES),
        )
        assert report.repeatability["max_diff_mean"] == pytest.approx(0.4)
        assert report.status == "advisory"
        assert "repeatability_max_diff_mean_above_threshold" in report.failures

    async def test_repeatability_numeric_normalized_on_non_unit_range(
        self, store, tmp_path
    ):
        """非单位量程 (0,10)：重复性最大差按归一化口径（与 nMAE 同基）。
        raw diff 0.9（raw 阈值 0.10 会隐式判失败）归一化后 0.09 → 通过。"""
        definition = _num_definition(range_or_categories=(0.0, 10.0))

        def script(sample, call_index):
            if call_index == 2:
                return _value_outcome(5.9)
            return _value_outcome(5.0)

        judge = ScriptedJudge(script)
        report = await _calibrate(
            store,
            definition,
            tmp_path,
            judge,
            calibration_samples=[
                _sample(f"n-{i:03d}", _category_cycle(i), 5.0)
                for i in range(MIN_CALIBRATION_SAMPLES)
            ],
        )
        assert report.repeatability["max_diff_mean"] == pytest.approx(0.09)
        assert report.status == "passed"


class TestCalibrationInvalidation:
    async def test_cache_key_covers_definition_hash(self, tmp_path):
        definition = _bool_definition()
        key = calibration_cache_key(
            definition=definition,
            calibration_set_hash="a" * 64,
            regression_set_hash="b" * 64,
        )
        same = calibration_cache_key(
            definition=_bool_definition(),
            calibration_set_hash="a" * 64,
            regression_set_hash="b" * 64,
        )
        assert key == same
        # Judge 模型 / rubric（material_hash）/ 截断策略与证据 schema
        #（judge_params / evidence_policy）任一变化 → 失效（新缓存键）
        for changed in (
            dict(judge_model="judge-model-y"),
            dict(material_hash={"rubric": "c" * 64}),
            dict(judge_params={"truncation": "head"}),
            dict(evidence_policy="snapshot_v2"),
        ):
            other = calibration_cache_key(
                definition=_bool_definition(**changed),
                calibration_set_hash="a" * 64,
                regression_set_hash="b" * 64,
            )
            assert other != key, changed
        # 运行参数（timeout/budget）不触发重校准
        runtime = calibration_cache_key(
            definition=_bool_definition(timeout_seconds=30, budget=None),
            calibration_set_hash="a" * 64,
            regression_set_hash="b" * 64,
        )
        assert runtime == key

    async def test_definition_hash_change_invalidates_cached_report(
        self, store, tmp_path
    ):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(store, definition, tmp_path, judge)
        assert report.status == "passed"

        # rubric 变化 → 新 definition hash → 新缓存键 → 重新判定
        changed = _bool_definition(material_hash={"rubric": "d" * 64})
        assert calibration_definition_hash(changed) != calibration_definition_hash(
            definition
        )
        with pytest.raises(EvaluationNotFoundError):
            await store.get_calibration_report(
                calibration_cache_key(
                    definition=changed,
                    calibration_set_hash=report.calibration_set_hash,
                    regression_set_hash=report.regression_set_hash,
                )
            )
        judge2 = _matching_bool_judge(_gold_by_id(samples))
        report2 = await _calibrate(store, changed, tmp_path, judge2)
        assert len(judge2.calls) == MIN_CALIBRATION_SAMPLES + 60
        assert report2.cache_key != report.cache_key

    async def test_same_cache_key_reuses_report_without_rerun(
        self, store, tmp_path
    ):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(store, definition, tmp_path, judge)
        calls_after_first = len(judge.calls)
        calibration_path, regression_path = _write_calibration_pair(tmp_path)
        calibrator = JudgeCalibrator(
            definition=definition, judge=judge, report_store=store
        )
        reused = await calibrator.calibrate(calibration_path, regression_path)
        assert reused.cache_key == report.cache_key
        assert reused.status == "passed"
        assert len(judge.calls) == calls_after_first


# ---------------------------------------------------------------------------
# Advisory 语义与 CI 门禁接口（T14 消费）
# ---------------------------------------------------------------------------


class TestGateEligibilityInterface:
    def test_advisory_provenances_defined(self):
        assert ADVISORY_PROVENANCES == frozenset({"calibration", "self_judge"})

    def test_gate_eligible_provenances(self):
        assert is_gate_eligible("automatic")
        assert is_gate_eligible("human")
        assert not is_gate_eligible("calibration")
        assert not is_gate_eligible("self_judge")

    def test_provenance_is_advisory(self):
        assert provenance_is_advisory("calibration")
        assert provenance_is_advisory("self_judge")
        assert not provenance_is_advisory("automatic")
        assert not provenance_is_advisory("human")

    async def test_failed_calibration_report_marks_advisory_status(
        self, store, tmp_path
    ):
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        flips = {f"s-{i:03d}" for i in range(10)}
        judge = _matching_bool_judge(_gold_by_id(samples), flips=flips)
        report = await _calibrate(store, definition, tmp_path, judge)
        assert report.status == "advisory"
        assert (
            await store.calibration_status(
                definition.definition_id,
                calibration_definition_hash(definition),
            )
            == "advisory"
        )


# ---------------------------------------------------------------------------
# Worker 透传端到端（adapter 输出 → worker commit → store 行）
# ---------------------------------------------------------------------------


class FakeEvidenceStore:
    def __init__(self):
        self._snapshots: dict[str, EvidenceSnapshot] = {}

    def put(self, ref: str, snapshot: EvidenceSnapshot) -> str:
        self._snapshots[ref] = snapshot
        return ref

    async def save_snapshot(self, target, snapshot) -> str:
        ref = f"snapshots/{uuid4().hex}"
        return self.put(ref, snapshot)

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot:
        try:
            return self._snapshots[snapshot_ref]
        except KeyError:
            raise EvaluationNotFoundError(
                f"snapshot not found: {snapshot_ref}"
            ) from None


class QueueEvaluator:
    """按序返回输出映射的 evaluator fake（对齐 T7 适配器输出形状）。"""

    def __init__(self, outcomes: list[Mapping[str, Any]]):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def evaluate(self, definition, snapshot):
        self.calls += 1
        return self._outcomes.pop(0)


def _judge_output(value=0.9, *, self_judge=False, explanation="judge reason"):
    return {
        "status": "value",
        "value": value,
        "explanation": explanation,
        "model": "judge-model-x",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "cost_usd": None,
        "provenance": {
            "self_judge": self_judge,
            "judge_model": "judge-model-x",
            "metric_name": "agent.answer_correctness",
        },
    }


def _worker_definition(**overrides) -> EvaluatorDefinition:
    defaults: dict[str, Any] = dict(
        definition_id="agent.answer_correctness",
        version="v1",
        metric_name="agent.answer_correctness",
        target_kind="agent_run",
        method="llm_judge",
        required_fields=(),
        evidence_policy="snapshot",
        output_type="number",
        range_or_categories=(0.0, 1.0),
        direction="higher_is_better",
        material_hash={},
        judge_model="judge-model-x",
        judge_params={},
        timeout_seconds=60,
        budget=None,
        ci_admissible=True,
    )
    defaults.update(overrides)
    return EvaluatorDefinition(**defaults)


async def _submit_one_job(store, service, definition, evidence) -> str:
    await service.register_definition(definition)
    snapshot = EvidenceSnapshot(
        content={"request": "q", "answer": "a"},
        redaction_version="redaction.v1",
    )
    snapshot_ref = evidence.put(f"snapshots/{uuid4().hex}", snapshot)
    subject_id = f"subject-{uuid4().hex[:6]}"
    outcome = await service.submit_sample(
        SubmitSampleRequest(
            producer_event_id=f"evt-{uuid4().hex[:12]}",
            service="n-agent",
            target=EvaluationTarget(
                service="n-agent",
                kind="agent_run",
                subject_id=subject_id,
                subject_revision="rev-1",
            ),
            snapshot_ref=snapshot_ref,
            snapshot_hash="a" * 64 if snapshot_ref else "",
            definitions=(
                DefinitionKey(definition.definition_id, definition.version),
            ),
        )
    )
    assert len(outcome.job_ids) == 1
    return outcome.job_ids[0]


async def _result_for_job(store, job_id):
    rows = await store.list_current_results(limit=1000)
    matches = [r for r in rows if r["job_id"] == job_id]
    return matches[0] if matches else None


def _make_worker(store, evaluator, *, evidence_store=None, **overrides):
    from app.application.evaluation_worker import EvaluationWorker

    return EvaluationWorker(
        store,
        evaluator=evaluator,
        evidence_store=(
            evidence_store if evidence_store is not None else FakeEvidenceStore()
        ),
        claimant="test-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=None,
        **overrides,
    )


class TestWorkerProvenancePassthrough:
    async def test_uncalibrated_judge_result_is_advisory_provenance(
        self, tmp_path
    ):
        """未注入校准状态（未校准）→ llm_judge 结果 provenance=calibration。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        service = EvaluationService(store, enabled=True)
        evidence = FakeEvidenceStore()
        definition = _worker_definition()
        job_id = await _submit_one_job(store, service, definition, evidence)
        evaluator = QueueEvaluator([_judge_output()])
        worker = _make_worker(store, evaluator, evidence_store=evidence)

        await worker.run_once()

        result = await _result_for_job(store, job_id)
        assert result is not None
        assert result["provenance"] == "calibration"

    async def test_passed_calibration_yields_automatic_provenance(
        self, tmp_path
    ):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        service = EvaluationService(store, enabled=True)
        evidence = FakeEvidenceStore()
        definition = _worker_definition()
        job_id = await _submit_one_job(store, service, definition, evidence)
        evaluator = QueueEvaluator([_judge_output()])
        worker = _make_worker(
            store,
            evaluator,
            evidence_store=evidence,
            calibration_status_provider=lambda d: calibration_status_from_store(
                store, d
            ),
        )
        # 预置一条 passed 校准报告（与本 definition hash 对应）
        await store.save_calibration_report(
            {
                "cache_key": calibration_cache_key(
                    definition=definition,
                    calibration_set_hash="a" * 64,
                    regression_set_hash="b" * 64,
                ),
                "metric_name": definition.metric_name,
                "definition_id": definition.definition_id,
                "definition_hash": calibration_definition_hash(definition),
                "status": "passed",
                "failures": [],
                "consistency": {"agreement": 1.0, "kappa": 1.0},
                "repeatability": {"agreement": 1.0, "max_diff_mean": 0.0},
            }
        )

        await worker.run_once()

        result = await _result_for_job(store, job_id)
        assert result is not None
        assert result["provenance"] == "automatic"

    async def test_self_judge_provenance_passthrough(self, tmp_path):
        """self_judge 标记到达 result 行，且优先于校准通过（不得作为唯一
        发布证据）。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        service = EvaluationService(store, enabled=True)
        evidence = FakeEvidenceStore()
        definition = _worker_definition()
        job_id = await _submit_one_job(store, service, definition, evidence)
        evaluator = QueueEvaluator([_judge_output(self_judge=True)])
        worker = _make_worker(
            store,
            evaluator,
            evidence_store=evidence,
            calibration_status_provider=lambda d: calibration_status_from_store(
                store, d
            ),
        )
        await store.save_calibration_report(
            {
                "cache_key": calibration_cache_key(
                    definition=definition,
                    calibration_set_hash="a" * 64,
                    regression_set_hash="b" * 64,
                ),
                "metric_name": definition.metric_name,
                "definition_id": definition.definition_id,
                "definition_hash": calibration_definition_hash(definition),
                "status": "passed",
                "failures": [],
                "consistency": {"agreement": 1.0, "kappa": 1.0},
                "repeatability": {"agreement": 1.0, "max_diff_mean": 0.0},
            }
        )

        await worker.run_once()

        result = await _result_for_job(store, job_id)
        assert result is not None
        assert result["provenance"] == "self_judge"
        assert not is_gate_eligible(result["provenance"])

    async def test_explanation_persisted_in_result_row(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        service = EvaluationService(store, enabled=True)
        evidence = FakeEvidenceStore()
        definition = _worker_definition()
        job_id = await _submit_one_job(store, service, definition, evidence)
        evaluator = QueueEvaluator([_judge_output(explanation="答案与参考一致")])
        worker = _make_worker(
            store,
            evaluator,
            evidence_store=evidence,
            calibration_status_provider=lambda d: "passed",
        )

        await worker.run_once()

        result = await _result_for_job(store, job_id)
        assert result is not None
        assert result["explanation"] == "答案与参考一致"

    async def test_explanation_capped_at_2000_utf8_bytes(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        service = EvaluationService(store, enabled=True)
        evidence = FakeEvidenceStore()
        definition = _worker_definition()
        job_id = await _submit_one_job(store, service, definition, evidence)
        long_explanation = "检" * 3000  # 9000 UTF-8 字节
        evaluator = QueueEvaluator(
            [_judge_output(explanation=long_explanation)]
        )
        worker = _make_worker(
            store,
            evaluator,
            evidence_store=evidence,
            calibration_status_provider=lambda d: "passed",
        )

        await worker.run_once()

        result = await _result_for_job(store, job_id)
        assert result is not None
        assert len(result["explanation"].encode("utf-8")) <= 2000

    async def test_deterministic_results_stay_automatic(self, tmp_path):
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        service = EvaluationService(store, enabled=True)
        evidence = FakeEvidenceStore()
        definition = _worker_definition(
            definition_id="kb.recall_at_5",
            metric_name="kb.recall_at_5",
            method="deterministic",
            judge_model=None,
        )
        job_id = await _submit_one_job(
            store,
            service,
            definition,
            evidence,
        )
        evaluator = QueueEvaluator([])
        worker = _make_worker(store, evaluator, evidence_store=evidence)

        await worker.run_once()

        result = await _result_for_job(store, job_id)
        assert result is not None
        assert result["provenance"] == "automatic"


# ---------------------------------------------------------------------------
# 质量评审补强：非有限数据 / 损坏缓存 / 金标形态 / skip provenance
# ---------------------------------------------------------------------------


class TestNonFiniteDataRejection:
    async def test_nan_judge_value_treated_as_missing(self, store, tmp_path):
        """Judge 输出 NaN → 缺失数据：按满量程误差 1.0 计（nMAE = 1/50），
        绝不静默进入均值（NaN 使 `nmae > MAX` 恒 False 会伪装成通过）。"""
        definition = _num_definition()

        def script(sample, idx):
            if sample.sample_id == "n-000":
                return _value_outcome(float("nan"))
            return _value_outcome(0.5)

        judge = ScriptedJudge(script)
        report = await _calibrate(
            store, definition, tmp_path, judge,
            calibration_samples=_numeric_samples(MIN_CALIBRATION_SAMPLES),
        )
        assert report.consistency["normalized_mae"] == pytest.approx(1.0 / 50)

    async def test_nan_repeatability_run_counts_as_full_error(
        self, store, tmp_path
    ):
        definition = _num_definition()

        def script(sample, idx):
            if sample.sample_id == "n-000" and idx == 1:
                return _value_outcome(float("inf"))
            return _value_outcome(0.5)

        judge = ScriptedJudge(script)
        report = await _calibrate(
            store, definition, tmp_path, judge,
            calibration_samples=_numeric_samples(MIN_CALIBRATION_SAMPLES),
        )
        # 20 个重复性样本中 1 个含 NaN run → 该样本按满量程 1.0 计
        assert report.repeatability["max_diff_mean"] == pytest.approx(1.0 / 20)

    async def test_nan_adjudicated_value_manifest_rejected(self, tmp_path):
        """manifest 金标含 NaN（json.loads 默认接受 NaN 字面量的洞）：
        canonical hash allow_nan=False + 显式 isfinite 双封堵，加载期
        fail-fast 拒绝。"""
        content = _manifest_content(
            "calibration", [_sample("s-000", "positive", True)]
        )
        content["samples"][0]["adjudicated_value"] = float("nan")
        content["manifest_hash"] = "a" * 64  # 伪造合法形态 hash
        path = tmp_path / "nan.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        with pytest.raises(CalibrationManifestError):
            load_calibration_set(path, role="calibration")


class TestCorruptCacheHandling:
    async def test_corrupt_report_json_recomputes(self, store, tmp_path):
        """账本内 report_json 损坏 → 视为缓存未命中（log + 重算覆盖），
        绝不永久阻断重校准；calibration_status 损坏行按 advisory。"""
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(store, definition, tmp_path, judge)
        assert report.status == "passed"

        with sqlite3.connect(str(tmp_path / "evaluations.db")) as conn:
            conn.execute(
                "UPDATE evaluation_calibrations SET report_json = '{oops'"
            )
            conn.commit()

        # 损坏缓存：get 抛未找到（视为未命中），status 保守 advisory
        with pytest.raises(EvaluationNotFoundError):
            await store.get_calibration_report(report.cache_key)
        assert (
            await store.calibration_status(
                definition.definition_id,
                calibration_definition_hash(definition),
            )
            == "advisory"
        )

        # 同缓存键重算：重跑 Judge 并覆盖损坏行
        judge2 = _matching_bool_judge(_gold_by_id(samples))
        calibration_path, regression_path = _write_calibration_pair(tmp_path)
        calibrator = JudgeCalibrator(
            definition=definition, judge=judge2, report_store=store
        )
        recomputed = await calibrator.calibrate(
            calibration_path, regression_path
        )
        assert len(judge2.calls) == MIN_CALIBRATION_SAMPLES + 60
        assert recomputed.status == "passed"
        assert (
            await store.calibration_status(
                definition.definition_id,
                calibration_definition_hash(definition),
            )
            == "passed"
        )

    async def test_malformed_cached_report_shape_recomputes(
        self, store, tmp_path
    ):
        """合法 JSON 但缺字段的缓存报告 → 视为未命中重算（不 KeyError）。"""
        definition = _bool_definition()
        samples = _bool_samples(MIN_CALIBRATION_SAMPLES)
        judge = _matching_bool_judge(_gold_by_id(samples))
        report = await _calibrate(store, definition, tmp_path, judge)
        with sqlite3.connect(str(tmp_path / "evaluations.db")) as conn:
            conn.execute(
                "UPDATE evaluation_calibrations SET report_json = '{\"a\": 1}'"
            )
            conn.commit()
        judge2 = _matching_bool_judge(_gold_by_id(samples))
        calibration_path, regression_path = _write_calibration_pair(tmp_path)
        calibrator = JudgeCalibrator(
            definition=definition, judge=judge2, report_store=store
        )
        recomputed = await calibrator.calibrate(
            calibration_path, regression_path
        )
        assert recomputed.cache_key == report.cache_key
        assert recomputed.status == "passed"


class TestGoldShapeValidation:
    async def test_boolean_calibration_rejects_numeric_gold(
        self, store, tmp_path
    ):
        """boolean 校准的金标必须全为 bool：数值金标（bool() 会静默变
        True，如 0.7→True）fail-fast 拒绝。"""
        definition = _bool_definition()
        samples = [
            _sample(f"s-{i:03d}", _category_cycle(i), 0.7)
            for i in range(MIN_CALIBRATION_SAMPLES)
        ]
        judge = _matching_bool_judge({})
        with pytest.raises(CalibrationError, match="金标"):
            await _calibrate(
                store, definition, tmp_path, judge, calibration_samples=samples
            )
        assert judge.calls == []

    async def test_numeric_calibration_rejects_bool_gold(
        self, store, tmp_path
    ):
        definition = _num_definition()
        samples = [
            _sample(f"s-{i:03d}", _category_cycle(i), True)
            for i in range(MIN_CALIBRATION_SAMPLES)
        ]
        judge = ScriptedJudge(lambda s, idx: _value_outcome(0.5))
        with pytest.raises(CalibrationError, match="金标"):
            await _calibrate(
                store, definition, tmp_path, judge, calibration_samples=samples
            )
        assert judge.calls == []


class TestSkipProvenanceDocumentedValue:
    async def test_llm_judge_skip_row_records_calibration(self, tmp_path):
        """skip 行（无分值）不做校准状态查询：llm_judge skip 记载
        calibration（保守 advisory 语义，documented value）。"""
        store = SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))
        service = EvaluationService(store, enabled=True)
        evidence = FakeEvidenceStore()
        definition = _worker_definition()
        job_id = await _submit_one_job(store, service, definition, evidence)
        evaluator = QueueEvaluator([])
        # worker 持有另一个空证据库 → 快照加载失败 → skipped(evidence_missing)
        worker = _make_worker(store, evaluator)

        await worker.run_once()

        result = await _result_for_job(store, job_id)
        assert result is not None
        assert result["status"] == "skipped"
        assert result["skip_reason"] == "evidence_missing"
        assert result["provenance"] == "calibration"
