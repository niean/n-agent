"""实验预检（plan T5，spec 7）：只读、无副作用、valid != runnable。

预检回答"这个提交现在能不能跑、为什么不能"，不做任何登记或写操作：
- 返回 profile/能力/逐题原因、所选 case_ids 与 hash、最大 replicates、
  目标项数和 Judge 调用数；
- blocked 整单拒绝：submit 前必须 preflight 通过；
- 无 TTL 配置或生命周期能力时 blocked（不临时无限保留，spec 6.2）；
- 显式 case_ids 子集固定分母；12 题 × 10 次超项数上限拒绝（12 题最大
  8 次）。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.application.evaluation_scenario import (
    binding_drift_reasons,
    case_scenario,
    required_metric_names,
    select_case_ids,
    unsupported_profiles,
)
from app.domain.evaluation.execution import (
    MAX_ITEMS,
    MAX_REPLICATES,
    scenario_total_budget_seconds,
)


class EvaluationPreflightService:
    """只读预检（无 store 写、无目标调用）。"""

    def __init__(
        self,
        *,
        review,
        readiness,
        ledger=None,
        material_hashes_reader=None,
        evidence_retention_seconds: float = 0.0,
        admitted_profiles: Iterable[str] = ("agent",),
    ) -> None:
        self._review = review
        self._readiness = readiness
        self._ledger = ledger
        self._material_hashes_reader = material_hashes_reader
        self._evidence_retention_seconds = float(evidence_retention_seconds)
        self._admitted_profiles = frozenset(admitted_profiles)

    async def _resolve_evaluator_set(
        self, evaluator_set: Iterable[Mapping[str, str]]
    ) -> list[dict[str, str]]:
        """归一评分器集合为 {metric_name, method}（账本只读解析）。

        载荷条目允许携带 metric_name/method（capabilities 形状）或仅
        definition_id/definition_version（submit 形状，经账本解析）。"""
        resolved: list[dict[str, str]] = []
        for item in evaluator_set:
            if not isinstance(item, Mapping):
                continue
            if item.get("metric_name") and item.get("method"):
                resolved.append(
                    {
                        "metric_name": str(item["metric_name"]),
                        "method": str(item["method"]),
                    }
                )
                continue
            if self._ledger is None:
                continue
            row = await self._ledger.get_definition(
                item["definition_id"], item["definition_version"]
            )
            resolved.append(
                {
                    "metric_name": str(row["metric_name"]),
                    "method": str(row["method"]),
                }
            )
        return resolved

    async def preflight(
        self,
        *,
        dataset_id: str,
        dataset_version: str,
        dataset_hash: str,
        evaluator_set: Iterable[Mapping[str, str]],
        replicates: int,
        case_ids: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        blocked: list[str] = []
        ready = await self._readiness() if self._readiness else {}
        if not ready.get("ready") or not ready.get("worker_ready"):
            blocked.append("执行服务或评分 worker 未就绪")

        raw = await self._review.get_dataset(dataset_id, dataset_version)
        if raw.get("dataset_hash") != dataset_hash:
            blocked.append("dataset_hash 与数据集版本不一致")
        resolved_evaluators = await self._resolve_evaluator_set(evaluator_set)
        metrics = {item["metric_name"] for item in resolved_evaluators}
        if not metrics:
            blocked.append("evaluator_set 为空")

        # 生命周期前提：无 TTL 配置即 blocked（不临时无限保留）
        if self._evidence_retention_seconds <= 0:
            blocked.append("证据保留期未配置（retention），拒绝运行场景实验")

        # 显式子集固定分母
        try:
            selected = select_case_ids(raw, list(case_ids) if case_ids else None)
        except ValueError as exc:
            blocked.append(str(exc))
            selected = []

        # profile 准入：未接入适配的通道整单拒绝（仅核验选中子集）
        for profile in unsupported_profiles(
            raw, self._admitted_profiles, selected
        ):
            blocked.append(f"场景通道未接入执行适配: {profile}")

        selected_cases = [
            case
            for case in raw.get("cases") or ()
            if str(case.get("case_id")) in set(selected)
        ]
        if not selected_cases:
            blocked.append("无可运行 case")

        material_hashes = (
            await self._material_hashes_reader(dataset_id, dataset_version)
            if self._material_hashes_reader
            else {}
        )
        per_case: list[dict[str, Any]] = []
        for case in selected_cases:
            reasons: list[str] = []
            case_id = str(case.get("case_id"))
            scenario = case_scenario(case)
            if scenario:
                reasons.extend(
                    f"必需指标未覆盖: {name}"
                    for name in sorted(required_metric_names(case) - metrics)
                )
                reasons.extend(binding_drift_reasons(case, material_hashes))
            per_case.append(
                {
                    "case_id": case_id,
                    "runnable": not reasons,
                    "reasons": reasons,
                }
            )

        item_count = len(selected_cases) * replicates
        if replicates > MAX_REPLICATES:
            blocked.append(f"replicates 超过上限 {MAX_REPLICATES}")
        if item_count > MAX_ITEMS:
            blocked.append(
                f"目标项数 {item_count} 超过服务器上限 {MAX_ITEMS}"
                f"（{len(selected_cases)} 题最大 "
                f"{MAX_ITEMS // max(len(selected_cases), 1)} 次）"
            )

        judge_metrics = [
            item for item in resolved_evaluators if item["method"] == "llm_judge"
        ]
        if judge_metrics and not ready.get("judge_ready"):
            blocked.append("Judge 未配置，llm_judge 评分器不可执行")

        runnable = (
            not blocked
            and bool(per_case)
            and all(entry["runnable"] for entry in per_case)
        )
        return {
            "runnable": runnable,
            "blocked": sorted(set(blocked)),
            "cases": per_case,
            "selected_case_ids": selected,
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
            "dataset_hash": raw.get("dataset_hash"),
            "item_count": item_count,
            "max_replicates": (
                MAX_ITEMS // max(len(selected_cases), 1)
                if selected_cases
                else 0
            ),
            "judge_calls": item_count * len(judge_metrics),
            "total_budget_seconds": scenario_total_budget_seconds(item_count),
        }
