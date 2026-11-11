# app/domain/evaluation/calibration_stats.py
"""Judge 校准纯统计函数（plan T8，spec 10）。

无 IO、无时钟、无随机数：给定判定序列返回确定值。Application 层的
校准编排（app/application/evaluation_calibration.py）只做样本装载、
Judge 调用与阈值判定，全部数值计算委托本模块。

约定（spec 10 语义，调用方按此映射 Judge 输出）：
- ``None``（Judge skipped / error / 形态非法 / 非有限数值）一律按
  "不一致 / 满量程误差"保守计——缺数据不得伪装成校准通过；非有限
  输入（NaN / ±inf）在进入统计前直接拒绝（ValueError）——NaN 不参与
  均值时 ``metric > MAX`` 恒为 False，会静默产出"passed"；
- 金标或判定单类时 Cohen kappa 不可计算（返回 ``None``）：调用方必须
  判校准失败，不得以 agreement 替代（spec 10 明文）。
"""
from __future__ import annotations

import math
from typing import Sequence


def boolean_agreement(
    judged: Sequence[bool | None], gold: Sequence[bool]
) -> float:
    """判定与金标的一致率；``None``（Judge skipped/error）按不一致计。"""
    if not gold:
        raise ValueError("gold 序列不得为空")
    if len(judged) != len(gold):
        raise ValueError("judged 与 gold 长度不一致")
    agreed = sum(
        1 for j, g in zip(judged, gold) if j is not None and bool(j) == bool(g)
    )
    return agreed / len(gold)


def cohen_kappa(
    judged: Sequence[bool | None], gold: Sequence[bool]
) -> float | None:
    """Cohen kappa（布尔两分类）。

    任一序列（按成对完整样本过滤后）只剩单一类别时 kappa 不可计算，
    返回 ``None``——调用方必须判校准失败，不得以 agreement 替代
    （spec 10：样本单类导致 kappa 不可算时不得以 agreement 替代）。
    ``None`` 判定项按成对剔除（只计算 Judge 实际给出的样本对）。
    """
    if not gold:
        raise ValueError("gold 序列不得为空")
    if len(judged) != len(gold):
        raise ValueError("judged 与 gold 长度不一致")
    pairs = [
        (bool(j), bool(g))
        for j, g in zip(judged, gold)
        if j is not None
    ]
    n = len(pairs)
    if n == 0:
        return None
    # 保守单类规则（spec 10）：任一序列（按成对完整样本过滤后）只剩单一
    # 类别即判 kappa 不可计算——单类金标无法证明 Judge 有区分力，此时
    # agreement 的高值不得替代校准通过。
    if len({j for j, _ in pairs}) < 2 or len({g for _, g in pairs}) < 2:
        return None
    po = sum(1 for j, g in pairs if j == g) / n
    judged_true = sum(1 for j, _ in pairs if j) / n
    gold_true = sum(1 for _, g in pairs if g) / n
    pe = judged_true * gold_true + (1 - judged_true) * (1 - gold_true)
    if pe == 1:
        # 双侧均单类且同类：kappa 分母为 0，同样不可计算（防御分支）
        return None
    return (po - pe) / (1 - pe)


def normalized_mae(
    judged: Sequence[float | None],
    gold: Sequence[float],
    *,
    value_range: tuple[float, float],
) -> float:
    """归一化平均绝对误差（normalMAE = mean|judge-gold| / (max-min)）。

    ``None``（Judge skipped/error）按满量程误差计（归一化 1.0，保守偏
    advisory）。value_range 必须是有限正跨度；任何非有限输入（NaN /
    ±inf——缺失或损坏数据）直接拒绝（ValueError），缺失数据永不伪装成
    校准通过（NaN 不参与均值：``nmae > MAX`` 对 NaN 恒为 False，会静默
    产出"passed"）。"""
    low, high = value_range
    span = high - low
    if not gold:
        raise ValueError("gold 序列不得为空")
    if len(judged) != len(gold):
        raise ValueError("judged 与 gold 长度不一致")
    if span <= 0:
        raise ValueError(f"value_range 跨度必须为正: {value_range!r}")
    total = 0.0
    for j, g in zip(judged, gold):
        if j is not None and not math.isfinite(float(j)):
            raise ValueError(f"judged 含非有限数值（缺失/损坏数据）: {j!r}")
        if not math.isfinite(float(g)):
            raise ValueError(f"gold 含非有限数值（缺失/损坏数据）: {g!r}")
        total += span if j is None else abs(float(j) - float(g))
    return total / len(gold) / span


def repeatability_boolean_agreement(
    runs: Sequence[Sequence[bool | None]],
) -> float:
    """重复性一致率（spec 10：20 样本 x 3 次，agreement>=0.90）。

    逐样本以多数值（mode）为参照：样本内与多数值一致的 run 占比；偶数
    run 数并列时取首次出现的高计数项（确定性 tie-break；本合同 run 数
    为 3，恒有严格多数，此分支仅为函数一般化保留）。
    ``None`` run 不与多数一致；样本全 ``None`` 记 0。返回逐样本均值。"""
    if not runs:
        raise ValueError("runs 不得为空")
    total = 0.0
    for sample_runs in runs:
        if not sample_runs:
            raise ValueError("样本 run 序列不得为空")
        counts: dict[bool, int] = {}
        first_seen: list[bool] = []
        for run in sample_runs:
            if run is None:
                continue
            value = bool(run)
            if value not in counts:
                counts[value] = 0
                first_seen.append(value)
            counts[value] += 1
        if not counts:
            total += 0.0
            continue
        # 遍历 first_seen（首现顺序）：并列时取先出现的类别
        mode = max(first_seen, key=lambda v: counts[v])
        agreed = sum(
            1 for run in sample_runs if run is not None and bool(run) == mode
        )
        total += agreed / len(sample_runs)
    return total / len(runs)


def repeatability_max_diff_mean(
    runs: Sequence[Sequence[float | None]],
    *,
    value_range: tuple[float, float],
) -> float:
    """重复性数值最大差的归一化均值（spec 10：20 样本 x 3 次，<=0.10）。

    口径判定：与一致性归一化 MAE 同基——逐样本 run 间 max-min 差除以
    定义值域跨度后取均值。spec 10 字面写"最大差的均值"（raw 单位），本
    项目统一取归一化口径：非单位量程定义下 raw 阈值会隐式加严/放宽
    （0.10 raw 对 (0,10) 量程等效归一化 0.01），与 nMAE<=0.15 不可比；
    单位量程 (0,1) 下两者数值等价，阈值语义不放宽。

    样本内任一 run 缺失（Judge skipped/error）按满量程误差计（归一化
    1.0，保守偏 advisory）。非有限 run 值（NaN / ±inf）直接拒绝
    （ValueError）——缺失数据永不伪装成校准通过。"""
    low, high = value_range
    span = high - low
    if span <= 0:
        raise ValueError(f"value_range 跨度必须为正: {value_range!r}")
    if not runs:
        raise ValueError("runs 不得为空")
    total = 0.0
    for sample_runs in runs:
        if not sample_runs:
            raise ValueError("样本 run 序列不得为空")
        present = [float(r) for r in sample_runs if r is not None]
        if any(not math.isfinite(v) for v in present):
            raise ValueError(
                f"runs 含非有限数值（缺失/损坏数据）: {sample_runs!r}"
            )
        if len(present) == len(sample_runs):
            total += (max(present) - min(present)) / span
        else:
            total += 1.0
    return total / len(runs)
