# 评估统计纯函数测试：对照 spec 4.5 统一口径校验时间过滤归一、UTC+8
# 日分桶、指标汇总与线程去重；并断言 analytics 模块保持纯 Domain。
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.domain.evaluation.analytics import (
    dedupe_latest,
    normalize_analytics_filters,
    summarize_metric_results,
    utc8_day_bucket,
)
from app.domain.evaluation.models import (
    EvaluationValidationError,
    EvaluatorDefinition,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

_NOW = "2026-11-12T00:00:00+00:00"


# ---------------------------------------------------------------------------
# normalize_analytics_filters（UTC 半开区间 [from, to)）
# ---------------------------------------------------------------------------


class TestNormalizeAnalyticsFilters:
    def test_defaults_are_last_seven_days(self):
        from_dt, to_dt = normalize_analytics_filters(
            created_from=None, created_to=None, now=_NOW
        )
        assert to_dt.isoformat() == _NOW
        assert from_dt.isoformat() == "2026-11-05T00:00:00+00:00"

    def test_explicit_range_parsed_to_utc(self):
        from_dt, to_dt = normalize_analytics_filters(
            created_from="2026-11-01T08:00:00+00:00",
            created_to="2026-11-10T09:00:00+00:00",
            now=_NOW,
        )
        assert from_dt.isoformat() == "2026-11-01T08:00:00+00:00"
        assert to_dt.isoformat() == "2026-11-10T09:00:00+00:00"

    def test_explicit_range_converted_to_utc(self):
        # 非 UTC 偏移输入统一换算为 UTC
        from_dt, _ = normalize_analytics_filters(
            created_from="2026-11-01T16:00:00+08:00",
            created_to="2026-11-10T00:00:00+00:00",
            now=_NOW,
        )
        assert from_dt.utcoffset().total_seconds() == 0
        assert from_dt.isoformat() == "2026-11-01T08:00:00+00:00"

    def test_empty_range_rejected(self):
        with pytest.raises(EvaluationValidationError):
            normalize_analytics_filters(
                created_from="2026-11-12T00:00:00+00:00",
                created_to="2026-11-12T00:00:00+00:00",
                now=_NOW,
            )
        with pytest.raises(EvaluationValidationError):
            normalize_analytics_filters(
                created_from="2026-11-13T00:00:00+00:00",
                created_to="2026-11-12T00:00:00+00:00",
                now=_NOW,
            )

    def test_range_over_ninety_days_rejected(self):
        # 91 天超限；恰好 90 天合法（半开区间跨度上限）
        with pytest.raises(EvaluationValidationError):
            normalize_analytics_filters(
                created_from="2026-08-11T00:00:00+00:00",
                created_to="2026-11-11T00:00:00+00:00",
                now=_NOW,
            )
        from_dt, to_dt = normalize_analytics_filters(
            created_from="2026-08-12T00:00:00+00:00",
            created_to="2026-11-10T00:00:00+00:00",
            now=_NOW,
        )
        assert (to_dt - from_dt).days == 90

    @pytest.mark.parametrize(
        "bad_from",
        [
            "not-a-time",
            "2026-11-12T00:00:00",  # Naive 与 aware now 混用
            float("nan"),
            float("inf"),
            123,
            object(),
        ],
    )
    def test_invalid_created_from_rejected(self, bad_from):
        with pytest.raises((EvaluationValidationError, TypeError)):
            normalize_analytics_filters(
                created_from=bad_from, created_to=None, now=_NOW
            )

    def test_naive_created_to_rejected(self):
        with pytest.raises(EvaluationValidationError):
            normalize_analytics_filters(
                created_from="2026-11-01T00:00:00+00:00",
                created_to="2026-11-10T00:00:00",
                now=_NOW,
            )

    def test_naive_now_rejected(self):
        with pytest.raises(EvaluationValidationError):
            normalize_analytics_filters(
                created_from=None,
                created_to=None,
                now="2026-11-12T00:00:00",
            )

    def test_accepts_datetime_inputs(self):
        from datetime import datetime, timezone

        from_dt, to_dt = normalize_analytics_filters(
            created_from=datetime(2026, 11, 1, tzinfo=timezone.utc),
            created_to=None,
            now=datetime(2026, 11, 12, tzinfo=timezone.utc),
        )
        assert to_dt.isoformat() == "2026-11-12T00:00:00+00:00"
        assert from_dt.isoformat() == "2026-11-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# utc8_day_bucket（UTC+8 自然日分桶）
# ---------------------------------------------------------------------------


class TestUtc8DayBucket:
    def test_cross_day_boundary_at_16_utc(self):
        # 15:59 UTC 仍是当日（23:59 UTC+8）；16:00 UTC 即次日（00:00 UTC+8）
        assert utc8_day_bucket("2026-11-12T15:59:00+00:00") == "2026-11-12"
        assert utc8_day_bucket("2026-11-12T16:00:00+00:00") == "2026-11-13"

    def test_utc8_offset_input_keeps_local_day(self):
        assert utc8_day_bucket("2026-11-12T23:30:00+08:00") == "2026-11-12"
        assert utc8_day_bucket("2026-11-12T00:30:00+08:00") == "2026-11-12"

    def test_invalid_or_naive_input_rejected(self):
        with pytest.raises(EvaluationValidationError):
            utc8_day_bucket("not-a-time")
        with pytest.raises(EvaluationValidationError):
            utc8_day_bucket("2026-11-12T15:59:00")


# ---------------------------------------------------------------------------
# summarize_metric_results（spec 4.5 统一口径）
# ---------------------------------------------------------------------------


def _number_definition() -> EvaluatorDefinition:
    return EvaluatorDefinition(
        definition_id="kb.recall_at_5",
        version="1",
        metric_name="kb.recall_at_5",
        target_kind="retrieval",
        method="deterministic",
        required_fields=("retrieved_ids", "qrels"),
        evidence_policy="require_frozen_corpus",
        output_type="number",
        range_or_categories=(0.0, 1.0),
        direction="higher_is_better",
        material_hash={"code": "a" * 64},
        judge_model=None,
        judge_params={},
        timeout_seconds=60,
        budget=None,
        ci_admissible=True,
    )


def _boolean_definition() -> EvaluatorDefinition:
    kwargs = dict(
        definition_id="agent.task_success",
        version="2",
        metric_name="agent.task_success",
        target_kind="agent_run",
        method="deterministic",
        required_fields=(),
        evidence_policy="require_snapshot",
        output_type="boolean",
        range_or_categories=(),
        direction="higher_is_better",
        material_hash={"code": "b" * 64},
        judge_model=None,
        judge_params={},
        timeout_seconds=60,
        budget=None,
        ci_admissible=True,
    )
    return EvaluatorDefinition(**kwargs)


def _categorical_definition() -> EvaluatorDefinition:
    kwargs = dict(
        definition_id="answer.grade",
        version="1",
        metric_name="answer.grade",
        target_kind="agent_run",
        method="llm_judge",
        required_fields=(),
        evidence_policy="require_snapshot",
        output_type="categorical",
        range_or_categories=("pass", "fail", "partial"),
        direction="higher_is_better",
        material_hash={"rubric": "c" * 64},
        judge_model="gpt-4o-mini",
        judge_params={},
        timeout_seconds=60,
        budget=None,
        ci_admissible=True,
    )
    return EvaluatorDefinition(**kwargs)


def _row(status: str, typed_value=None, **extra):
    row = {"status": status, "typed_value": typed_value}
    row.update(extra)
    return row


class TestSummarizeMetricResultsNumber:
    def test_valid_rows_only_in_statistics(self):
        definition = _number_definition()
        rows = [
            _row("value", 0.5),
            _row("value", 1.0),
            _row("skipped"),
            _row("error"),
        ]
        result = summarize_metric_results(definition, rows)
        assert result["count"] == 2
        assert result["mean"] == pytest.approx(0.75)
        assert result["min"] == pytest.approx(0.5)
        assert result["max"] == pytest.approx(1.0)
        assert result["skip_count"] == 1
        assert result["error_count"] == 1
        assert result["invalid_count"] == 0

    def test_invalid_values_excluded_not_zero(self):
        definition = _number_definition()
        rows = [
            _row("value", 0.5),
            # 越界值与 bool 值非法：不计入统计，也不当 0
            _row("value", 2.0),
            _row("value", True),
            _row("value", "0.5"),
            _row("value", float("nan")),
        ]
        result = summarize_metric_results(definition, rows)
        assert result["count"] == 1
        assert result["mean"] == pytest.approx(0.5)
        assert result["invalid_count"] == 4

    def test_empty_rows(self):
        definition = _number_definition()
        result = summarize_metric_results(definition, [])
        assert result["count"] == 0
        assert result["mean"] is None
        assert result["min"] is None
        assert result["max"] is None
        assert result["invalid_count"] == 0
        assert result["skip_count"] == 0
        assert result["error_count"] == 0


class TestSummarizeMetricResultsBoolean:
    def test_true_false_counts_and_ratio(self):
        definition = _boolean_definition()
        rows = [
            _row("value", True),
            _row("value", True),
            _row("value", False),
            _row("skipped"),
            _row("error"),
        ]
        result = summarize_metric_results(definition, rows)
        assert result["true_count"] == 2
        assert result["false_count"] == 1
        assert result["true_ratio"] == pytest.approx(2 / 3)
        assert result["skip_count"] == 1
        assert result["error_count"] == 1

    def test_invalid_boolean_values_counted_invalid(self):
        definition = _boolean_definition()
        rows = [
            _row("value", True),
            _row("value", 1),  # int 不是 boolean 合法值
            _row("value", "true"),
        ]
        result = summarize_metric_results(definition, rows)
        assert result["true_count"] == 1
        assert result["false_count"] == 0
        assert result["invalid_count"] == 2
        assert result["true_ratio"] == pytest.approx(1.0)

    def test_both_zero_ratio_is_none(self):
        definition = _boolean_definition()
        result = summarize_metric_results(definition, [_row("skipped"), _row("error")])
        assert result["true_count"] == 0
        assert result["false_count"] == 0
        assert result["true_ratio"] is None

    def test_empty_rows(self):
        definition = _boolean_definition()
        result = summarize_metric_results(definition, [])
        assert result["true_count"] == 0
        assert result["false_count"] == 0
        assert result["true_ratio"] is None


class TestSummarizeMetricResultsCategorical:
    def test_counts_and_ratios_over_valid_rows(self):
        definition = _categorical_definition()
        rows = [
            _row("value", "pass"),
            _row("value", "pass"),
            _row("value", "fail"),
            _row("value", "partial"),
            _row("skipped"),
            _row("error"),
        ]
        result = summarize_metric_results(definition, rows)
        assert result["counts"] == {"pass": 2, "fail": 1, "partial": 1}
        assert result["ratios"]["pass"] == pytest.approx(0.5)
        assert result["ratios"]["fail"] == pytest.approx(0.25)
        assert result["skip_count"] == 1
        assert result["error_count"] == 1

    def test_out_of_set_value_goes_to_invalid_not_denominator(self):
        definition = _categorical_definition()
        rows = [
            _row("value", "pass"),
            _row("value", "unknown"),
            _row("value", 1),
        ]
        result = summarize_metric_results(definition, rows)
        assert result["counts"] == {"pass": 1, "fail": 0, "partial": 0}
        assert result["invalid_count"] == 2
        # 占比分母只含合法行
        assert result["ratios"]["pass"] == pytest.approx(1.0)

    def test_empty_rows(self):
        definition = _categorical_definition()
        result = summarize_metric_results(definition, [])
        assert result["counts"] == {"pass": 0, "fail": 0, "partial": 0}
        assert result["ratios"] == {"pass": None, "fail": None, "partial": None}
        assert result["invalid_count"] == 0

    def test_unknown_status_rejected(self):
        definition = _number_definition()
        with pytest.raises(EvaluationValidationError):
            summarize_metric_results(definition, [_row("boom")])


# ---------------------------------------------------------------------------
# dedupe_latest（线程去重，决定性）
# ---------------------------------------------------------------------------


def _feedback_row(
    feedback_id: str,
    revision: int,
    created_at: str,
    *,
    actor_ref: str = "user-1",
    target_service: str = "n-agent",
    target_kind: str = "agent_run",
    target_subject_id: str = "run-1",
    target_subject_revision: str = "rev-1",
    definition_id: str = "agent.task_success",
    definition_version: str = "1",
    status: str = "value",
    typed_value=True,
):
    return {
        "feedback_id": feedback_id,
        "actor_ref": actor_ref,
        "target_service": target_service,
        "target_kind": target_kind,
        "target_subject_id": target_subject_id,
        "target_subject_revision": target_subject_revision,
        "definition_id": definition_id,
        "definition_version": definition_version,
        "revision": revision,
        "created_at": created_at,
        "status": status,
        "typed_value": typed_value,
    }


class TestDedupeLatest:
    def test_keeps_highest_revision_per_thread(self):
        rows = [
            _feedback_row("fb-1", 1, "2026-11-10T00:00:00+00:00", typed_value=False),
            _feedback_row("fb-2", 2, "2026-11-11T00:00:00+00:00"),
            _feedback_row("fb-3", 3, "2026-11-11T01:00:00+00:00", typed_value=False),
        ]
        deduped = dedupe_latest(rows)
        assert len(deduped) == 1
        assert deduped[0]["feedback_id"] == "fb-3"
        assert deduped[0]["revision"] == 3

    def test_same_revision_tie_broken_by_created_then_feedback_id(self):
        # 同 revision：created_at 更晚者优先；created_at 相同则 feedback_id 大者优先
        rows = [
            _feedback_row("fb-a", 2, "2026-11-10T00:00:00+00:00", typed_value=False),
            _feedback_row("fb-b", 2, "2026-11-11T00:00:00+00:00"),
        ]
        deduped = dedupe_latest(rows)
        assert [r["feedback_id"] for r in deduped] == ["fb-b"]

        rows = [
            _feedback_row("fb-b", 2, "2026-11-11T00:00:00+00:00"),
            _feedback_row("fb-a", 2, "2026-11-10T00:00:00+00:00", typed_value=False),
        ]
        deduped = dedupe_latest(rows)
        assert [r["feedback_id"] for r in deduped] == ["fb-b"]

        rows = [
            _feedback_row("fb-a", 2, "2026-11-11T00:00:00+00:00", typed_value=False),
            _feedback_row("fb-b", 2, "2026-11-11T00:00:00+00:00"),
        ]
        deduped = dedupe_latest(rows)
        assert [r["feedback_id"] for r in deduped] == ["fb-b"]

    def test_separate_threads_both_kept(self):
        rows = [
            _feedback_row("fb-1", 1, "2026-11-10T00:00:00+00:00"),
            _feedback_row(
                "fb-2",
                1,
                "2026-11-10T00:00:00+00:00",
                target_subject_id="run-2",
                definition_version="2",
            ),
            _feedback_row(
                "fb-3",
                1,
                "2026-11-10T00:00:00+00:00",
                actor_ref="user-2",
            ),
        ]
        deduped = dedupe_latest(rows)
        assert {r["feedback_id"] for r in deduped} == {"fb-1", "fb-2", "fb-3"}

    def test_definition_version_participates_in_thread_key(self):
        rows = [
            _feedback_row("fb-1", 1, "2026-11-10T00:00:00+00:00"),
            _feedback_row("fb-2", 1, "2026-11-11T00:00:00+00:00", definition_version="2"),
        ]
        deduped = dedupe_latest(rows)
        assert {r["feedback_id"] for r in deduped} == {"fb-1", "fb-2"}

    def test_deterministic_same_input_same_output(self):
        rows = [
            _feedback_row("fb-a", 2, "2026-11-11T00:00:00+00:00"),
            _feedback_row("fb-b", 2, "2026-11-11T00:00:00+00:00", typed_value=False),
            _feedback_row("fb-c", 1, "2026-11-09T00:00:00+00:00", typed_value=False),
        ]
        first = dedupe_latest(rows)
        second = dedupe_latest(list(reversed(rows)))
        assert [r["feedback_id"] for r in first] == [r["feedback_id"] for r in second]
        assert first[0]["feedback_id"] == "fb-b"

    def test_invalid_revision_rejected(self):
        rows = [_feedback_row("fb-1", "2", "2026-11-10T00:00:00+00:00")]
        with pytest.raises(EvaluationValidationError):
            dedupe_latest(rows)

    def test_empty_input(self):
        assert dedupe_latest([]) == []


# ---------------------------------------------------------------------------
# 纯度断言（照 tests/application/test_evaluation_experiment.py 写法）
# ---------------------------------------------------------------------------


def test_analytics_module_stays_pure_no_framework_or_infrastructure():
    source = (REPO_ROOT / "app" / "domain" / "evaluation" / "analytics.py").read_text()
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    forbidden = ("fastapi", "sqlite3", "opentelemetry", "langfuse", "pydantic")
    for prefix in forbidden:
        assert not any(m == prefix or m.startswith(prefix + ".") for m in modules)
    assert not any(m.startswith("app.infrastructure") for m in modules)
