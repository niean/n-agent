# app/application/evaluation_analytics.py
"""分析聚合与只读实验对比（plan Task 7，spec 4.5 / 4.6）。

EvaluationAnalyticsService 是评估工作台的 Application 用例入口（与
EvaluationService / AnnotationService 并列，不改变其既有行为）：
- overview_analytics（spec 4.5 统一口径）：调用 Task 4 聚合查询
  ``store.aggregate_metric_results``（服务端完成过滤后聚合，禁止依赖
  前端最近 50 条）+ Domain ``analytics.py`` 纯函数（时间窗归一、UTC+8
  日分桶）组装概览统计；
- compare_experiments（spec 4.6 只读实验对比）：账本重建两次 +
  正式可比性校验入口 + 可选 gate_policy 四态结论。

DDD：Application 不 import Infrastructure；store 为账本端口形状（真实
SQLiteEvaluationStore 在组合根注入，测试可用 fake/真实库）。

只读边界（spec 4.6）：本服务任何用例不得触发实验执行（runner
execute/replay）或 Judge 调用、不改变实验状态；构造参数 ``runner`` 仅
作为只读断言锚点（测试注入 fake runner 并断言零调用），本服务不消费
该对象。

统计口径要点（spec 4.5）：
- 时间窗 UTC 半开区间 ``[from, to)``（Task 1 ``normalize_analytics_filters``
  归一，默认最近 7 天、上限 90 天）；趋势按 UTC+8 自然日分桶，包含
  零记录日期；
- 聚合键 (definition_id, definition_version, provenance, output_type)
  即 (DefinitionKey, provenance, output_type)，不同版本/来源/输出类型
  不混键；
- number 仅统计成功且值合法的行（count/mean/min/max）；boolean 展示
  true_count/false_count/true_ratio；categorical 展示观测类别计数与
  占比，不求均值；
- error/skipped 分列（status_counts），不把执行失败/无评分当 0；
- 不生成未定义阈值的通过率（number/categorical 无 pass_rate 字段）；
- 无总体分母时 coverage: unknown 附原因"缺少应评估目标总数"。

错误映射（Interfaces 层 Task 9 接线）：
- EvaluationNotFoundError -> 404 not_found；
- IncomparableExperimentsError -> 422 incomparable_experiments（附
  可公开差异维度布尔列表）；
- EvaluationValidationError（含 gate_policy 非法）-> 422 protocol_invalid。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from app.application.evaluation_gate import (
    DEFAULT_BOOTSTRAP_REPLICATES,
    DEFAULT_BOOTSTRAP_SEED,
    GATE_PASS,
    INCONCLUSIVE,
    RESULTS_PAGE_LIMIT,
    EvaluationGate,
    GateConclusion,
    GateEvaluationRequest,
    GateExperimentData,
    GatePolicyError,
    collect_gate_data,
    gate_policy_content,
    gate_policy_from_content,
    validate_baseline_config,
)
from app.domain.evaluation.analytics import (
    normalize_analytics_filters,
    utc8_day_bucket,
)
from app.domain.evaluation.gate_stats import paired_bootstrap_ci_lower
from app.domain.evaluation.models import (
    EvaluationValidationError,
    canonical_json_bytes,
)


# coverage: unknown 的固定原因（spec 4.5：缺少目标总体分母不得宣称
# 采样覆盖率；ScoreAnalytics DTO 契约字符串）
COVERAGE_UNKNOWN_REASON = "缺少应评估目标总数"

# compare 账本重建证据基准（spec 4.6：ExperimentComparison DTO 契约值）
LEDGER_RECONSTRUCTION = "ledger_reconstruction"

# 分析层账本证据保护 reason 前缀（--json reasons 机器可读契约；只追加
# 在 analytics 层，EvaluationGate 原门禁算法与其 reason 前缀不动）
REASON_PREFIX_LEDGER_EVIDENCE_INCOMPLETE = "ledger_evidence_incomplete"

# 数据集身份可比性维度（可公开差异维度布尔映射的字段集，spec 9
# incomparable_experiments 差异列表）
_DATASET_IDENTITY_FIELDS: tuple[str, ...] = (
    "dataset_id",
    "dataset_version",
    "dataset_hash",
)


class IncomparableExperimentsError(EvaluationValidationError):
    """候选/基线实验不可比（spec 4.6/9：HTTP 422 incomparable_experiments，
    Task 9 注册路由映射）。

    ``diff_dimensions`` 为可公开差异维度布尔映射：dataset_id /
    dataset_version / dataset_hash（及 definition_keys）各自是否不一致；
    不携带内部路径、SQL 或凭据。"""

    def __init__(
        self, message: str, *, diff_dimensions: Mapping[str, bool]
    ) -> None:
        super().__init__(message)
        self.diff_dimensions = dict(diff_dimensions)


def validate_dataset_comparability(
    candidate_row: Mapping[str, Any], baseline_row: Mapping[str, Any]
) -> dict[str, bool]:
    """数据集身份只读核验（正式入口，spec 4.6）。

    dataset_id / dataset_version / dataset_hash 任一不一致即拒绝；无
    policy 的描述性对比同样执行（不能只凭名称强行对比）。返回差异
    维度布尔映射（全 False 表示一致）。"""
    diff_dimensions = {
        field: candidate_row.get(field) != baseline_row.get(field)
        for field in _DATASET_IDENTITY_FIELDS
    }
    mismatched = [
        field for field, differs in diff_dimensions.items() if differs
    ]
    if mismatched:
        raise IncomparableExperimentsError(
            "候选/基线实验数据集身份不一致，不可比: "
            + ", ".join(sorted(mismatched)),
            diff_dimensions=diff_dimensions,
        )
    return diff_dimensions


def validate_config_comparability(
    candidate: GateExperimentData, baseline: GateExperimentData
) -> None:
    """冻结配置与 DefinitionKey 只读核验（正式入口，spec 4.6）。

    DefinitionKey（指标名）集不一致 -> IncomparableExperimentsError；
    冻结快照（dataset_hash/evaluator_set_hash/judge_config_hash 等）
    一致性复用 EvaluationGate 的同一权威算法（同包复用公开的
    ``validate_baseline_config``，禁止两份实现分叉）——有 policy 时与
    EvaluationGate.evaluate 内部校验同规则，无 policy 的描述性对比
    同样执行。

    前置条件：本函数不核验数据集身份，diff_dimensions 中 dataset_* 三
    维硬编码为 False，须先执行 ``validate_dataset_comparability``
    （compare_experiments 调用序满足）；独立调用本函数时 dataset 维度
    不反映真实差异。"""
    if set(candidate.metrics) != set(baseline.metrics):
        raise IncomparableExperimentsError(
            "候选/基线 DefinitionKey 集不一致，不可比: "
            f"{sorted(candidate.metrics)} != {sorted(baseline.metrics)}",
            diff_dimensions={
                "dataset_id": False,
                "dataset_version": False,
                "dataset_hash": False,
                "definition_keys": True,
            },
        )
    validate_baseline_config(candidate, baseline)


def _experiment_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    """实验身份投影（DTO：两实验身份；不含内部路径/凭据）。"""
    return {
        field: row.get(field)
        for field in ("experiment_id", *_DATASET_IDENTITY_FIELDS)
    }


async def _collect_delivery_status_counts(
    ledger: Any, experiment_id: str
) -> dict[str, dict[str, int]]:
    """投递状态单列汇总（sink -> status -> 计数；与 CLI gate 命令同一
    形状探测）。Langfuse 不可用（delivery 全部 pending 等）不阻断对比，
    仅单列展示；ledger 缺少投递查询形状时返回空汇总。"""
    counts: dict[str, dict[str, int]] = {}
    list_deliveries = getattr(ledger, "list_deliveries", None)
    if list_deliveries is None:
        return counts
    cursor: str | None = None
    while True:
        rows, next_cursor = await ledger.query_results_page(
            experiment_id=experiment_id,
            cursor=cursor,
            limit=RESULTS_PAGE_LIMIT,
        )
        for row in rows:
            result_id = row.get("result_id")
            if not result_id:
                continue
            for delivery in await list_deliveries(result_id):
                sink = str(delivery.get("sink") or "unknown")
                status = str(delivery.get("status") or "unknown")
                per_sink = counts.setdefault(sink, {})
                per_sink[status] = per_sink.get(status, 0) + 1
        if next_cursor is None:
            break
        cursor = next_cursor
    return counts


def _metric_case_values(
    data: GateExperimentData, metric_name: str
) -> dict[str, float]:
    """case -> 值（同 case 多 replicate 取均值；boolean 按 1/0 映射，
    非有限数值不计入——与门禁 ``EvaluationGate._case_value`` 同口径；
    描述性对比有意包含全部 provenance，不做 is_gate_eligible 过滤，
    与门禁 boolean 通过率口径并不完全等价）。"""
    per_case: dict[str, list[float]] = {}
    for (case_id, metric), scores in data.scores.items():
        if metric != metric_name:
            continue
        values = []
        for score in scores:
            if score.status != "value":
                continue
            raw = score.value
            if isinstance(raw, bool):
                values.append(1.0 if raw else 0.0)
            elif isinstance(raw, (int, float)) and math.isfinite(
                float(raw)
            ):
                values.append(float(raw))
        if values:
            per_case[case_id] = sum(values) / len(values)
    return per_case


def _metric_error_count(data: GateExperimentData, metric_name: str) -> int:
    """异常条目数：score status=error 或 outcome=judge_error 的 case 数。"""
    count = 0
    for (case_id, metric), scores in data.scores.items():
        if metric != metric_name:
            continue
        if any(score.status == "error" for score in scores) or (
            data.outcomes.get((case_id, metric)) == "judge_error"
        ):
            count += 1
    return count


def _side_summary(
    data: GateExperimentData, metric_name: str, values: Mapping[str, float]
) -> dict[str, Any]:
    denominator = len(data.applicable_cases.get(metric_name, ()))
    return {
        "value_count": len(values),
        "macro_value": (
            sum(values.values()) / len(values) if values else None
        ),
        "denominator": denominator,
        "missing_count": max(0, denominator - len(values)),
        "error_count": _metric_error_count(data, metric_name),
    }


def _paired_metric_rows(
    candidate: GateExperimentData,
    baseline: GateExperimentData,
    *,
    bootstrap_seed: int,
    bootstrap_replicates: int,
) -> list[dict[str, Any]]:
    """配对指标行（spec 4.6：按 case 配对的指标、有效样本数、差值/既有
    置信区间、缺失和异常条目）。"""
    rows: list[dict[str, Any]] = []
    shared = sorted(set(candidate.metrics) & set(baseline.metrics))
    for metric_name in shared:
        candidate_values = _metric_case_values(candidate, metric_name)
        baseline_values = _metric_case_values(baseline, metric_name)
        paired = sorted(set(candidate_values) & set(baseline_values))
        paired_diffs = {
            case_id: [candidate_values[case_id] - baseline_values[case_id]]
            for case_id in paired
        }
        diff = (
            sum(
                candidate_values[case_id] - baseline_values[case_id]
                for case_id in paired
            )
            / len(paired)
            if paired
            else None
        )
        ci_lower = None
        if paired_diffs:
            # 差值全部来自 _metric_case_values 的有限值，非法有限差值
            # 已在前置过滤挡住，paired_bootstrap_ci_lower 的 ValueError
            # 不再可达
            ci_lower = paired_bootstrap_ci_lower(
                paired_diffs,
                seed=bootstrap_seed,
                replicates=bootstrap_replicates,
            )
        rows.append(
            {
                "metric_name": metric_name,
                "candidate": _side_summary(
                    candidate, metric_name, candidate_values
                ),
                "baseline": _side_summary(
                    baseline, metric_name, baseline_values
                ),
                "paired_cases": len(paired),
                "diff": diff,
                "paired_ci_lower": ci_lower,
            }
        )
    return rows


def _ledger_evidence_incomplete_reason(
    candidate: GateExperimentData,
) -> str | None:
    """账本重建证据完整性检查（analytics 层新增保护，spec 4.6）。

    collect_gate_data 的 manifest 恢复只有 case_ids（逐指标适用性未
    冻结，按"全部适用"保守放大分母）、分母退回 observed 近似、或无
    观测 replicate 证据时返回原因；证据完整返回 None。

    注意：expected_replicates 来自 collect_gate_data 的观测计数，
    并非实验的预声明 replicate 计划——该检查是"预声明 replicate
    完备证据"的近似语义（以观测计数近似证据完整性），措辞以实际
    检查为准，不宣称更强的保证。"""
    if candidate.denominator_basis != "pre_declared":
        return "denominator_basis=observed（账本无预声明分母）"
    universes = {
        tuple(cases) for cases in candidate.applicable_cases.values()
    }
    if universes and len(universes) == 1:
        # 账本路径所有指标共用同一 case 全集：manifest 仅恢复 case_ids，
        # 逐指标适用性缺失（保守近似放大分母），不得宣称正式通过
        return "manifest 仅恢复 case_ids，缺逐指标适用性"
    if not candidate.expected_replicates:
        # 近似语义：expected_replicates 是观测计数，非预声明计划
        return "无观测 replicate 证据"
    return None


def _apply_ledger_evidence_protection(
    conclusion: GateConclusion, candidate: GateExperimentData
) -> GateConclusion:
    """分析层新增保护（spec 4.6：不得为补页面放宽原门禁算法）。

    仅当原结论 gate_pass 且账本重建证据不完整时降为 inconclusive 并
    追加原因；gate_fail / infrastructure_error / inconclusive 原样保留
    （原 gate_fail/infrastructure_error 算法不动）。"""
    if conclusion.state != GATE_PASS:
        return conclusion
    reason = _ledger_evidence_incomplete_reason(candidate)
    if reason is None:
        return conclusion
    return replace(
        conclusion,
        state=INCONCLUSIVE,
        reasons=conclusion.reasons
        + (f"{REASON_PREFIX_LEDGER_EVIDENCE_INCOMPLETE}:{reason}",),
    )


def _utc8_bucket_dates(window_from: datetime, window_to: datetime) -> list[str]:
    """[from, to) 半开区间覆盖的 UTC+8 自然日序列（含零记录空日）。

    与 Domain ``utc8_day_bucket`` 同口径（复用同一函数，禁止两份实现
    分叉）：桶上界取 ``to - 1µs`` 所在自然日（to 恰为 UTC+8 午夜时
    不含该日）。"""
    start = date.fromisoformat(utc8_day_bucket(window_from.isoformat()))
    end = date.fromisoformat(
        utc8_day_bucket((window_to - timedelta(microseconds=1)).isoformat())
    )
    days: list[str] = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _group_summary(group: Mapping[str, Any]) -> dict[str, Any]:
    """Task 4 聚合组 -> 分析分组统计（spec 4.5 口径）。

    无值分组 count=0 其余统计 null；number/categorical 不生成通过率
    （未定义阈值不造通过率）；boolean 的 true_ratio 是定义语义内的
    比例（true/(true+false)），非门禁通过率。"""
    output_type = group["output_type"]
    entry: dict[str, Any] = {
        "definition_id": group["definition_id"],
        "definition_version": group["definition_version"],
        "provenance": group["provenance"],
        "output_type": output_type,
        "total_count": int(group["total_count"]),
        "status_counts": dict(group["status_counts"]),
        "daily_counts": dict(group["daily_counts"]),
    }
    if output_type == "number":
        count = int(group["numeric_count"])
        entry.update(
            {
                "count": count,
                "mean": (
                    group["numeric_sum"] / count if count else None
                ),
                "min": group["numeric_min"],
                "max": group["numeric_max"],
            }
        )
    elif output_type == "boolean":
        total = int(group["boolean_count"])
        passed = int(group["boolean_pass"])
        entry.update(
            {
                "true_count": passed,
                "false_count": total - passed,
                "true_ratio": (passed / total) if total else None,
            }
        )
    elif output_type == "categorical":
        counts = dict(sorted(group["categorical_counts"].items()))
        total = sum(counts.values())
        entry.update(
            {
                "counts": counts,
                "ratios": {
                    key: (value / total if total else None)
                    for key, value in counts.items()
                },
            }
        )
    else:
        raise EvaluationValidationError(
            f"未知 output_type: {output_type!r}"
        )
    return entry


def build_overview_analytics(
    groups: list[Mapping[str, Any]],
    *,
    window_from: datetime,
    window_to: datetime,
    snapshot_at: datetime,
) -> dict[str, Any]:
    """聚合组 -> 概览分析响应（纯函数，确定性组装）。"""
    bucket_dates = _utc8_bucket_dates(window_from, window_to)
    totals: dict[str, int] = {day: 0 for day in bucket_dates}
    summaries = []
    for group in groups:
        summary = _group_summary(group)
        summaries.append(summary)
        for day, count in summary["daily_counts"].items():
            if day in totals:
                totals[day] += int(count)
    return {
        "snapshot_at": snapshot_at.isoformat(),
        "window": {
            "from": window_from.isoformat(),
            "to": window_to.isoformat(),
        },
        "buckets": [
            {"date": day, "total": totals[day]} for day in bucket_dates
        ],
        "groups": summaries,
        "coverage": {"status": "unknown", "reason": COVERAGE_UNKNOWN_REASON},
    }


class EvaluationAnalyticsService:
    """分析聚合与只读对比用例（spec 4.5 / 4.6）。

    构造：
    - store：账本端口（真实 SQLiteEvaluationStore 在组合根注入）；
    - runner：只读断言锚点——本服务不消费该对象（任何用例禁止触发
      runner execute/replay 或 Judge 调用）；测试注入 fake runner 并
      断言零调用，防止后续演化把执行语义混入只读分析。
    """

    def __init__(self, store: Any, *, runner: Any = None) -> None:
        self._store = store
        # 只读边界锚点：本服务任何路径不得调用 runner（spec 4.6）
        self._runner = runner

    async def overview_analytics(
        self,
        *,
        created_from: str | None = None,
        created_to: str | None = None,
        target_kind: str | None = None,
        definition_id: str | None = None,
        definition_version: str | None = None,
        provenance: str | None = None,
        status: str | None = None,
        now: datetime | None = None,
    ) -> Mapping[str, Any]:
        """概览分析聚合（spec 4.5）。

        过滤校验复用 Task 1 ``normalize_analytics_filters``（UTC 半开
        区间、默认最近 7 天、上限 90 天、from>=to 拒绝）；聚合经 Task 4
        ``aggregate_metric_results`` 在服务端完成全量过滤后统计。
        """
        snapshot_at = now or datetime.now(timezone.utc)
        window_from, window_to = normalize_analytics_filters(
            created_from=created_from,
            created_to=created_to,
            now=snapshot_at,
        )
        groups = await self._store.aggregate_metric_results(
            created_from=window_from.isoformat(),
            created_to=window_to.isoformat(),
            target_kind=target_kind,
            definition_id=definition_id,
            definition_version=definition_version,
            provenance=provenance,
            status=status,
        )
        return build_overview_analytics(
            groups,
            window_from=window_from,
            window_to=window_to,
            snapshot_at=snapshot_at,
        )

    async def _get_experiment_row(self, experiment_id: str) -> Mapping[str, Any]:
        """读取实验记录（不存在 -> EvaluationNotFoundError，Task 9 映射
        404 not_found）。"""
        return await self._store.get_experiment(experiment_id)

    async def compare_experiments(
        self,
        candidate_id: str,
        baseline_id: str,
        policy: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """只读实验对比（spec 4.6）。

        流程：实验存在性（404）-> 数据集身份核验（IncomparableExperimentsError，
        422）-> 可选 gate_policy 解析（非法 -> EvaluationValidationError，422）
        -> ``collect_gate_data`` 账本重建两次 -> 冻结配置与 DefinitionKey
        正式只读校验入口 -> 配对指标/差值/既有置信区间 -> 有 policy 时
        复用 EvaluationGate 四态结论并施加账本证据保护。

        只读：不重跑目标、不启动 Judge、不改变实验状态；响应含
        ``evidence_basis="ledger_reconstruction"`` 与 ``denominator_basis``
        （pre_declared|observed，任一侧 observed 即整体 observed）。
        无 policy 时仅描述性输出，不生成 gate_pass/gate_fail。
        """
        candidate_row = await self._get_experiment_row(candidate_id)
        baseline_row = await self._get_experiment_row(baseline_id)
        validate_dataset_comparability(candidate_row, baseline_row)
        gate_policy = None
        if policy is not None:
            try:
                gate_policy = gate_policy_from_content(policy)
            except GatePolicyError as exc:
                # GatePolicyError 是加载失败细节；对外统一 422 校验通道
                raise EvaluationValidationError(
                    f"gate_policy 非法: {exc}"
                ) from exc
        candidate_data = await collect_gate_data(self._store, candidate_id)
        baseline_data = await collect_gate_data(self._store, baseline_id)
        validate_config_comparability(candidate_data, baseline_data)
        # 投递状态单列（候选实验），不参与质量结论判定
        delivery_status_counts = await _collect_delivery_status_counts(
            self._store, candidate_id
        )
        denominator_basis = (
            "observed"
            if "observed" in (
                candidate_data.denominator_basis,
                baseline_data.denominator_basis,
            )
            else "pre_declared"
        )
        response: dict[str, Any] = {
            "evidence_basis": LEDGER_RECONSTRUCTION,
            "denominator_basis": denominator_basis,
            "candidate": _experiment_identity(candidate_row),
            "baseline": _experiment_identity(baseline_row),
            "metrics": _paired_metric_rows(
                candidate_data,
                baseline_data,
                bootstrap_seed=(
                    gate_policy.bootstrap_seed
                    if gate_policy is not None
                    else DEFAULT_BOOTSTRAP_SEED
                ),
                bootstrap_replicates=(
                    gate_policy.bootstrap_replicates
                    if gate_policy is not None
                    else DEFAULT_BOOTSTRAP_REPLICATES
                ),
            ),
            "delivery_status_counts": delivery_status_counts,
        }
        if gate_policy is None:
            # 描述性输出：不生成 gate_pass/gate_fail（spec 4.6）
            return response
        conclusion = await EvaluationGate().evaluate(
            GateEvaluationRequest(
                candidate=candidate_data,
                policy=gate_policy,
                baseline=baseline_data,
                delivery_status_counts=delivery_status_counts,
            )
        )
        conclusion = _apply_ledger_evidence_protection(
            conclusion, candidate_data
        )
        # 策略指纹（gate_policy_content 规范 JSON 的 sha256）
        response["policy_fingerprint"] = hashlib.sha256(
            canonical_json_bytes(gate_policy_content(gate_policy))
        ).hexdigest()
        response["conclusion"] = conclusion.to_mapping()
        return response
