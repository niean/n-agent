# app.domain.evaluation.calibration_stats 纯统计函数单元测试（plan T8）。
#
# 纯函数、无 IO：boolean 一致率、Cohen kappa（单类不可算返回 None）、
# 归一化 MAE（Judge skipped/error 按满量程误差保守计）、重复性统计。
from __future__ import annotations

import pytest

from app.domain.evaluation.calibration_stats import (
    boolean_agreement,
    cohen_kappa,
    normalized_mae,
    repeatability_boolean_agreement,
    repeatability_max_diff_mean,
)


class TestBooleanAgreement:
    def test_full_agreement(self):
        assert boolean_agreement([True, False, True], [True, False, True]) == 1.0

    def test_partial_agreement(self):
        assert boolean_agreement([True, True, False, False], [True, False, True, False]) == 0.5

    def test_none_counts_as_disagreement(self):
        # Judge skipped/error（None）按不一致计（保守偏 advisory）
        assert boolean_agreement([True, None], [True, True]) == 0.5

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            boolean_agreement([], [])


class TestCohenKappa:
    def test_perfect_agreement_balanced_classes(self):
        judged = [True, False] * 25
        assert cohen_kappa(judged, list(judged)) == pytest.approx(1.0)

    def test_known_value_balanced(self):
        # po=0.86、pe=0.5 → kappa=0.72
        gold = [True] * 25 + [False] * 25
        judged = (
            [True] * 23 + [False] * 2  # gold True：23 对 2 错
            + [False] * 20 + [True] * 5  # gold False：20 对 5 错
        )
        assert cohen_kappa(judged, gold) == pytest.approx(0.72)

    def test_single_class_gold_returns_none(self):
        # 金标单类：kappa 不可计算，必须返回 None（不得以 agreement 替代）
        assert cohen_kappa([True] * 10, [True] * 10) is None

    def test_single_class_judged_returns_none(self):
        assert cohen_kappa([True] * 10, [True, False] * 5) is None

    def test_none_entries_excluded_pairwise(self):
        # 成对完整样本参与计算；过滤后任一侧单类或全缺失 → None（保守）
        judged = [True, None, False]
        gold = [True, False, False]
        assert cohen_kappa(judged, gold) == pytest.approx(1.0)
        assert cohen_kappa([True, None], [True, False]) is None
        assert cohen_kappa([None, None], [True, False]) is None

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            cohen_kappa([], [])


class TestNormalizedMae:
    def test_zero_error(self):
        assert normalized_mae([0.5] * 4, [0.5] * 4, value_range=(0.0, 1.0)) == 0.0

    def test_known_value(self):
        # mean|err| = (0.15*4)/4 = 0.15 / range 1.0 → 0.15
        judged = [0.35, 0.65, 0.35, 0.65]
        gold = [0.5, 0.5, 0.5, 0.5]
        assert normalized_mae(judged, gold, value_range=(0.0, 1.0)) == pytest.approx(0.15)

    def test_range_normalization(self):
        # 同样绝对误差 5，量程 (0, 10) → 归一化 0.5
        assert normalized_mae([5.0], [0.0], value_range=(0.0, 10.0)) == pytest.approx(0.5)

    def test_none_counts_as_full_range_error(self):
        # Judge skipped/error → 满量程误差（归一化 1.0，保守）
        assert normalized_mae([None], [0.5], value_range=(0.0, 1.0)) == pytest.approx(1.0)

    def test_mixed_none_and_error(self):
        judged = [None, 0.6, 0.5, 0.5]
        gold = [0.5, 0.5, 0.5, 0.5]
        # (1.0 + 0.1 + 0 + 0) / 4 = 0.275
        assert normalized_mae(judged, gold, value_range=(0.0, 1.0)) == pytest.approx(0.275)

    def test_degenerate_range_raises(self):
        with pytest.raises(ValueError):
            normalized_mae([0.5], [0.5], value_range=(1.0, 1.0))


class TestRepeatabilityBooleanAgreement:
    def test_all_runs_identical(self):
        runs = [[True, True, True], [False, False, False]]
        assert repeatability_boolean_agreement(runs) == pytest.approx(1.0)

    def test_majority_split_counts_partial(self):
        # 3 次中 2 次与多数一致 → 该样本 2/3；均值 (1.0 + 2/3) / 2
        runs = [[True, True, True], [True, True, False]]
        expected = (1.0 + 2.0 / 3.0) / 2
        assert repeatability_boolean_agreement(runs) == pytest.approx(expected)

    def test_none_counts_against_mode(self):
        # None（skipped）不与多数一致：该样本 2/3
        runs = [[True, True, None]]
        assert repeatability_boolean_agreement(runs) == pytest.approx(2.0 / 3.0)

    def test_all_none_sample_scores_zero(self):
        runs = [[None, None, None], [True, True, True]]
        assert repeatability_boolean_agreement(runs) == pytest.approx(0.5)


class TestRepeatabilityMaxDiffMean:
    def test_identical_runs_zero(self):
        runs = [[0.5, 0.5, 0.5], [0.2, 0.2, 0.2]]
        assert repeatability_max_diff_mean(runs, value_range=(0.0, 1.0)) == 0.0

    def test_known_value_unit_range(self):
        runs = [[0.5, 0.5, 0.9], [0.1, 0.1, 0.1]]
        # 逐样本 diff (0.4, 0.0) / span 1.0 → 均值 0.2
        assert repeatability_max_diff_mean(runs, value_range=(0.0, 1.0)) == pytest.approx(0.2)

    def test_non_unit_range_normalized(self):
        # 非单位量程：raw diff 0.9 对 (0, 10) 量程归一化为 0.09 ——
        # 与一致性 nMAE 同基（raw 阈值 0.10 会隐式判失败，口径漂移）
        runs = [[5.0, 5.0, 5.9]]
        assert repeatability_max_diff_mean(runs, value_range=(0.0, 10.0)) == pytest.approx(0.09)

    def test_any_none_counts_as_full_error(self):
        # 任一 run 缺失 → 该样本按满量程计（归一化 1.0，保守）
        runs = [[0.5, 0.5, None], [0.1, 0.1, 0.1]]
        assert repeatability_max_diff_mean(runs, value_range=(0.0, 1.0)) == pytest.approx(0.5)

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            repeatability_max_diff_mean([], value_range=(0.0, 1.0))

    def test_degenerate_range_raises(self):
        with pytest.raises(ValueError):
            repeatability_max_diff_mean([[0.5, 0.5, 0.5]], value_range=(1.0, 1.0))


class TestRepeatabilityBooleanTieBreak:
    def test_even_runs_tie_takes_first_occurrence(self):
        # 偶数 run 并列（本合同 run 数为 3 不会出现）：取首现类别，确定性
        runs = [[True, False, True, False]]
        assert repeatability_boolean_agreement(runs) == pytest.approx(0.5)

    def test_even_runs_tie_first_occurrence_false(self):
        runs = [[False, True, False, True]]
        assert repeatability_boolean_agreement(runs) == pytest.approx(0.5)


class TestNonFiniteInputRejection:
    """非有限输入（NaN / ±inf，缺失或损坏数据）直接拒绝——NaN 不参与
    均值时 `metric > MAX` 恒为 False，会静默产出"passed"（belt-and-braces：
    上游 app 层已把非有限 Judge 输出映射为 None / manifest 拒绝）。"""

    def test_normalized_mae_rejects_nan_judged(self):
        with pytest.raises(ValueError):
            normalized_mae([float("nan")], [0.5], value_range=(0.0, 1.0))

    def test_normalized_mae_rejects_inf_gold(self):
        with pytest.raises(ValueError):
            normalized_mae([0.5], [float("inf")], value_range=(0.0, 1.0))

    def test_repeatability_rejects_non_finite_run(self):
        with pytest.raises(ValueError):
            repeatability_max_diff_mean(
                [[0.5, float("nan"), 0.5]], value_range=(0.0, 1.0)
            )
