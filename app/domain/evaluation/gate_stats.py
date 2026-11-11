# app/domain/evaluation/gate_stats.py
"""门禁纯统计函数（plan T14，spec 9.2）。

无 IO、无时钟：随机性仅经固定种子引入（``random.Random(seed)`` + 排序
后的 case 顺序），同输入复算完全确定。Application 层的门禁编排
（app/application/evaluation_gate.py）只做数据装配与状态收敛，全部数值
计算委托本模块（与 calibration_stats.py 同一分层模式）。

约定（spec 9.2 语义）：
- Wilson 区间用于 boolean 指标通过率（比例）的 95% 双侧区间，闭式公式；
- 配对 bootstrap 的分位数取 nearest-rank 约定：下界取 bootstrap 分布的
  第 ``ceil(q * replicates)`` 小（1 基）统计量，即 0 基索引
  ``ceil(q * replicates) - 1``（越界收敛到 replicates - 1）；q * n 整除
  时同样取第 q*n 小（例：n=40、q=0.025 取索引 0），相对 ``int(q*n)``
  的取法不会向通过侧偏移（略偏保守）。
"""
from __future__ import annotations

import math
import random
from typing import Any, Mapping, Sequence

# Wilson 95% 双侧 z 值（正态分位数 z_{0.975}）
WILSON_Z_95: float = 1.959963984540054

# 配对 bootstrap 的下侧分位（95% 区间下界 = 2.5% 分位）
_BOOTSTRAP_LOWER_QUANTILE: float = 0.025


def bootstrap_lower_index(replicates: int) -> int:
    """bootstrap 下侧分位的 nearest-rank 索引（0 基，纯函数）。

    nearest-rank 约定：第 ``ceil(q * n)`` 小（1 基）统计量作为分位数，
    即 0 基索引 ``ceil(q * n) - 1``；``q * n`` 整除时仍取第 ``q * n``
    小（n=40、q=0.025 -> 索引 0），不会向通过侧偏移。"""
    if isinstance(replicates, bool) or not isinstance(replicates, int) or (
        replicates < 1
    ):
        raise ValueError(f"replicates 必须是正整数: {replicates!r}")
    return min(
        math.ceil(_BOOTSTRAP_LOWER_QUANTILE * replicates) - 1,
        replicates - 1,
    )


def wilson_interval(
    successes: int, total: int, *, z: float = WILSON_Z_95
) -> tuple[float, float]:
    """比例的 Wilson 95% 区间（闭式公式，纯函数）。

    (successes, total) 必须满足 0 <= successes <= total 且 total > 0，
    否则 ValueError。返回 (lower, high)，两端截断到 [0, 1]。
    """
    if isinstance(total, bool) or not isinstance(total, int) or total <= 0:
        raise ValueError(f"total 必须是正整数: {total!r}")
    if (
        isinstance(successes, bool)
        or not isinstance(successes, int)
        or not 0 <= successes <= total
    ):
        raise ValueError(f"successes 必须在 [0, total] 内: {successes!r}")
    proportion = successes / total
    z_squared = z * z
    denominator = 1.0 + z_squared / total
    center = (proportion + z_squared / (2.0 * total)) / denominator
    half = (
        z
        * math.sqrt(
            proportion * (1.0 - proportion) / total
            + z_squared / (4.0 * total * total)
        )
        / denominator
    )
    return max(0.0, center - half), min(1.0, center + half)


def paired_bootstrap_ci_lower(
    case_differences: Mapping[str, Sequence[float]],
    *,
    seed: int,
    replicates: int,
) -> float:
    """case 配对 bootstrap 的 95% 区间下界（spec 9.2）。

    重采样单位是 case（cluster）：每轮等概率有放回抽出与原 case 数相同
    个数的 cluster，被抽中 case 的全部 replicate 配对差整块进入统计量
    （同 case 的 replicate 不作为独立样本）；统计量为池化均值。固定 seed
    下完全确定（``random.Random(seed)`` + 排序后的 case 顺序）。返回
    bootstrap 分布的 2.5% 分位（95% 区间下界，nearest-rank 约定见模块
    docstring）。``replicates`` 允许为 1（单轮重采样，区间下界即该轮
    统计量）；case_differences 为空或含空 case 时拒绝（ValueError）。
    """
    if not case_differences:
        raise ValueError("case_differences 不能为空")
    bootstrap_lower_index(replicates)  # 复用入参校验（replicates >= 1）
    pool: list[tuple[float, ...]] = []
    for case_id in sorted(case_differences):
        diffs = case_differences[case_id]
        values = tuple(
            _require_finite_diff(diff, case_id) for diff in diffs
        )
        if not values:
            raise ValueError(f"case {case_id!r} 的配对差不能为空")
        pool.append(values)
    rng = random.Random(seed)
    case_count = len(pool)
    statistics: list[float] = []
    for _ in range(replicates):
        total = 0.0
        count = 0
        for _ in range(case_count):
            for diff in pool[rng.randrange(case_count)]:
                total += diff
                count += 1
        statistics.append(total / count)
    statistics.sort()
    return statistics[bootstrap_lower_index(replicates)]


def _require_finite_diff(value: Any, case_id: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"case {case_id!r} 的配对差必须是数值: {value!r}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(
            f"case {case_id!r} 的配对差非有限（NaN/±inf）: {value!r}"
        )
    return number
