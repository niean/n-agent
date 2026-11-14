"""schema 2 场景数据集的编排辅助（plan T5）。

preflight 与执行 admission 共用的纯函数：profile 准入、必需指标覆盖、
绑定漂移、分母选择。不做 IO、不写状态；数据集原始 dict 来自
ReviewService 权威 loader（已通过校验），这里只做场景语义判定。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.domain.evaluation.scenario import (
    SCENARIO_COMPARISON_SCOPES,
    SCENARIO_PROFILES,
)


def case_scenario(case: Mapping[str, Any]) -> Mapping[str, Any] | None:
    scenario = case.get("scenario")
    return scenario if isinstance(scenario, Mapping) else None


def dataset_profiles(raw: Mapping[str, Any]) -> set[str]:
    """数据集内出现的全部场景 profile（schema 1 返回空集）。"""
    profiles: set[str] = set()
    for case in raw.get("cases") or ():
        scenario = case_scenario(case)
        if scenario:
            execution = scenario.get("execution") or {}
            profile = execution.get("profile")
            if isinstance(profile, str):
                profiles.add(profile)
    return profiles


def unsupported_profiles(
    raw: Mapping[str, Any],
    admitted: Iterable[str],
    selected_ids: Iterable[str] | None = None,
) -> list[str]:
    """未接入执行适配的 profile（准入集合之外），preflight blocked 依据。

    固定分母纪律（spec：任一"选中" case blocked 才整单拒绝）：显式子集
    时只核验选中 case 的 profile；全选（selected_ids=None）核验全部。"""
    admitted_set = set(admitted)
    if selected_ids is None:
        return sorted(dataset_profiles(raw) - admitted_set)
    selected = set(str(cid) for cid in selected_ids)
    profiles: set[str] = set()
    for case in raw.get("cases") or ():
        if str(case.get("case_id")) not in selected:
            continue
        scenario = case_scenario(case)
        if not scenario:
            continue
        profile = (scenario.get("execution") or {}).get("profile")
        if isinstance(profile, str):
            profiles.add(profile)
    return sorted(profiles - admitted_set)


def required_metric_names(case: Mapping[str, Any]) -> set[str]:
    """单 case 必需指标名集合（required_metrics clause -> metric 的值域）。"""
    scenario = case_scenario(case)
    if not scenario:
        return set()
    required = scenario.get("required_metrics")
    names: set[str] = set()
    if isinstance(required, Mapping):
        for metric in required.values():
            if isinstance(metric, str):
                names.add(metric)
    return names


def uncovered_metric_reasons(
    case: Mapping[str, Any], selected_metrics: Iterable[str]
) -> list[str]:
    """必需指标未被评分器集合覆盖的原因清单（分母纪律：必需指标遗漏
    不得进入运行，缺失/跳过不算通过）。"""
    selected = set(selected_metrics)
    missing = sorted(required_metric_names(case) - selected)
    return [
        f"必需指标未覆盖: {name}（case {case.get('case_id')}）"
        for name in missing
    ]


def binding_drift_reasons(
    case: Mapping[str, Any], material_hashes: Mapping[str, str]
) -> list[str]:
    """绑定漂移检查：绑定声明的 sha256 与权威材料摘要不一致即漂移。

    material_hashes 为逻辑材料别名 -> 实际内容 sha256（由数据集目录
    读取）；绑定引用了不存在的材料同样视为漂移（fail-closed）。"""
    scenario = case_scenario(case)
    if not scenario:
        return []
    bindings = scenario.get("bindings")
    if not isinstance(bindings, Mapping) or not bindings:
        return []
    reasons: list[str] = []
    case_id = case.get("case_id")
    for alias, binding in sorted(bindings.items()):
        if not isinstance(binding, Mapping):
            continue
        expected = binding.get("sha256")
        actual = material_hashes.get(str(alias))
        if actual is None:
            reasons.append(f"绑定材料缺失: {alias}（case {case_id}）")
        elif expected != actual:
            reasons.append(f"绑定漂移: {alias}（case {case_id}）")
    return reasons


def select_case_ids(
    raw: Mapping[str, Any], case_ids: Iterable[str] | None
) -> list[str]:
    """显式子集固定分母：case_ids 必须是数据集 case 的非空子集。"""
    available = [str(c.get("case_id")) for c in raw.get("cases") or ()]
    if case_ids is None:
        return available
    requested = list(case_ids)
    if not requested:
        raise ValueError("case_ids 显式子集不能为空")
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ValueError(f"case_ids 含未知 case: {unknown}")
    if len(requested) != len(set(requested)):
        raise ValueError("case_ids 存在重复项")
    # 保持 manifest 顺序，保证分母确定性
    return [cid for cid in available if cid in set(requested)]


def comparison_scope_of(case: Mapping[str, Any]) -> str | None:
    scenario = case_scenario(case)
    if not scenario:
        return None
    scope = scenario.get("comparison_scope")
    return scope if scope in SCENARIO_COMPARISON_SCOPES else None
