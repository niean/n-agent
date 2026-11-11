# app/domain/evaluation/analytics.py
"""评估统计纯函数：时间过滤归一、UTC+8 日分桶、指标汇总与线程去重。

本模块是统一评估闭环统计口径（spec 4.5）的纯函数实现：零 IO、零
框架/SDK 依赖。约定：

- 时间范围使用 UTC 半开区间 ``[from, to)``；from 缺省为 now-7 天，
  to 缺省为 now，跨度上限 90 天。
- 自然日分桶固定 UTC+8（运营时区），不随部署环境漂移。
- 值的合法性判定统一走 Domain 的 ``validate_evaluation_value``，
  按 definition 的 range_or_categories 校验，不得仅凭 output_type
  自行推断范围/类别。
- skipped / error 行分开计数，不混入任何统计分母（缺数据不是 0）。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Any

from app.domain.evaluation.models import (
    EvaluationValidationError,
    EvaluatorDefinition,
    validate_evaluation_value,
)

# 查询时间范围默认与上限（spec 4.5：默认最近 7 天，最大 90 天）
DEFAULT_RANGE_DAYS: int = 7
MAX_RANGE_DAYS: int = 90

# 结果行 status 允许的取值（store 投影：value / skipped / error）
RESULT_ROW_STATUSES: tuple[str, ...] = ("value", "skipped", "error")

# dedupe_latest 的完整线程身份键（feedback 行投影，与 store.py v8
# uq_feedback_thread_revision_key 唯一索引的线程七元组一致）
DEDUPE_THREAD_KEY_FIELDS: tuple[str, ...] = (
    "actor_ref",
    "target_service",
    "target_kind",
    "target_subject_id",
    "target_subject_revision",
    "definition_id",
    "definition_version",
)

_UTC8_TZ = timezone(timedelta(hours=8))


def _parse_aware_datetime(value: Any, label: str) -> datetime:
    """ISO 字符串或 datetime 解析为带时区 datetime；Naive 拒绝。

    NaN/Infinity 等非时间类型同样拒绝（不是 str/datetime 即抛错），
    统一抛 EvaluationValidationError。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise EvaluationValidationError(
                f"{label} 不是合法 ISO 时间: {value!r}"
            ) from exc
    else:
        raise EvaluationValidationError(
            f"{label} 必须是 ISO 时间字符串或 datetime: {value!r}"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationValidationError(f"{label} 必须携带时区信息: {value!r}")
    return parsed


def normalize_analytics_filters(
    *,
    created_from: str | None,
    created_to: str | None,
    now: datetime,
) -> tuple[datetime, datetime]:
    """归一化查询时间范围为 UTC 半开区间 ``[from, to)``。

    created_from 缺省为 now-7 天，created_to 缺省为 now；``from >= to``
    或跨度超过 MAX_RANGE_DAYS（90 天）拒绝。非法 ISO、Naive 时间与
    NaN/Infinity 等非时间类型抛 EvaluationValidationError。
    """
    now_dt = _parse_aware_datetime(now, "now")
    from_dt = (
        _parse_aware_datetime(created_from, "created_from")
        if created_from is not None
        else now_dt - timedelta(days=DEFAULT_RANGE_DAYS)
    )
    to_dt = (
        _parse_aware_datetime(created_to, "created_to")
        if created_to is not None
        else now_dt
    )
    if from_dt >= to_dt:
        raise EvaluationValidationError(
            f"created_from 不得晚于等于 created_to: [{from_dt.isoformat()}, "
            f"{to_dt.isoformat()})"
        )
    if to_dt - from_dt > timedelta(days=MAX_RANGE_DAYS):
        raise EvaluationValidationError(
            f"时间跨度不得超过 {MAX_RANGE_DAYS} 天: "
            f"[{from_dt.isoformat()}, {to_dt.isoformat()})"
        )
    # 半开区间锚定 UTC：非 UTC 偏移输入统一换算为 UTC
    return (from_dt.astimezone(timezone.utc), to_dt.astimezone(timezone.utc))


def utc8_day_bucket(created_at: str) -> str:
    """把 ISO 时间归入 UTC+8 自然日，返回 ``YYYY-MM-DD``。

    跨日边界：15:59 UTC 是当日（23:59 UTC+8），16:00 UTC 即次日
    （00:00 UTC+8）。Naive 输入拒绝。
    """
    parsed = _parse_aware_datetime(created_at, "created_at")
    return parsed.astimezone(_UTC8_TZ).strftime("%Y-%m-%d")


def summarize_metric_results(
    definition: EvaluatorDefinition,
    rows: Iterable[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """按定义的输出类型汇总结果行（spec 4.5 统一口径）。

    - number：仅统计 ``status == "value"`` 且值通过
      ``validate_evaluation_value`` 的行，输出 count/mean/min/max；
      值非法行计入 invalid_count，不计入也不当 0；
    - boolean：true_count/false_count 与 true_ratio（true/(true+false)），
      两者皆 0 时 true_ratio 为 None；
    - categorical：声明类别的 counts 与 ratios；值不在类别集的行计入
      invalid_count，不进占比分母；
    - skipped/error 行分开计入 skip_count/error_count，不混入分母；
    - 空输入：count/true_count 等为 0，其余统计（mean/min/max/
      true_ratio/各占比）为 None。

    返回只读 Mapping；未知 status 抛 EvaluationValidationError。
    """
    if not isinstance(definition, EvaluatorDefinition):
        raise EvaluationValidationError("definition 必须是 EvaluatorDefinition")
    values: list[float] = []
    true_count = 0
    false_count = 0
    category_counts: dict[str, int] = {
        category: 0
        for category in (definition.range_or_categories or ())
        if definition.output_type == "categorical"
    }
    invalid_count = 0
    skip_count = 0
    error_count = 0

    for row in rows:
        status = row.get("status")
        if status not in RESULT_ROW_STATUSES:
            raise EvaluationValidationError(
                f"非法结果行 status: {status!r}，仅允许 {RESULT_ROW_STATUSES}"
            )
        if status == "skipped":
            skip_count += 1
            continue
        if status == "error":
            error_count += 1
            continue
        value = row.get("typed_value")
        try:
            validate_evaluation_value(definition, value)
        except EvaluationValidationError:
            invalid_count += 1
            continue
        if definition.output_type == "number":
            values.append(float(value))
        elif definition.output_type == "boolean":
            if value:
                true_count += 1
            else:
                false_count += 1
        else:
            category_counts[value] = category_counts.get(value, 0) + 1

    counts = {
        "invalid_count": invalid_count,
        "skip_count": skip_count,
        "error_count": error_count,
    }
    if definition.output_type == "number":
        if values:
            stats: dict[str, Any] = {
                "count": len(values),
                "mean": sum(values) / len(values),
                "min": min(values),
                "max": max(values),
            }
        else:
            stats = {"count": 0, "mean": None, "min": None, "max": None}
    elif definition.output_type == "boolean":
        total = true_count + false_count
        stats = {
            "true_count": true_count,
            "false_count": false_count,
            "true_ratio": (true_count / total) if total else None,
        }
    else:
        valid_total = sum(category_counts.values())
        stats = {
            "counts": MappingProxyType(dict(category_counts)),
            "ratios": MappingProxyType(
                {
                    category: (
                        count / valid_total if valid_total else None
                    )
                    for category, count in category_counts.items()
                }
            ),
        }
    return MappingProxyType({**stats, **counts})


def dedupe_latest(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """按完整线程身份取最高 revision 的行（决定性）。

    线程键为 DEDUPE_THREAD_KEY_FIELDS 七元组；同线程取最高 revision，
    同 revision 并存时按 ``created_at``、``feedback_id`` 依次择大，同一
    输入永远得到同一输出。输出保持线程首次出现顺序。revision 必须是
    int，否则抛 EvaluationValidationError。
    """
    latest: dict[tuple[Any, ...], tuple[int, str, str, Mapping[str, Any]]] = {}
    order: list[tuple[Any, ...]] = []
    for row in rows:
        key = tuple(row.get(field) for field in DEDUPE_THREAD_KEY_FIELDS)
        revision = row.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise EvaluationValidationError(
                f"结果行 revision 必须是整数: {revision!r}"
            )
        created_at = row.get("created_at") or ""
        feedback_id = row.get("feedback_id") or ""
        if not isinstance(created_at, str) or not isinstance(feedback_id, str):
            raise EvaluationValidationError(
                f"结果行 created_at/feedback_id 必须是字符串: {row!r}"
            )
        ranking = (revision, created_at, feedback_id, row)
        if key not in latest:
            order.append(key)
            latest[key] = ranking
        elif ranking[:3] > latest[key][:3]:
            latest[key] = ranking
    return [latest[key][3] for key in order]
