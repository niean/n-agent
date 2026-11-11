# app/application/evaluation_gate.py
"""评估门禁与四态结论（plan T14，spec 9.2）。

EvaluationGate 是离线实验的 CI 门禁编排器：结合实验报告、数据集适用性
声明与账本分数，按版本化 gate_policy 计算四态结论
（gate_pass / gate_fail / inconclusive / infrastructure_error），CLI 按
GATE_STATE_EXIT_CODES 映射退出码 0 / 1 / 3 / 2。

门禁语义（spec 9.2，权威定义）：
- 关键安全/工具约束用例（case.slice 命中 policy.safety_slices）必须全部
  达到对应必需指标阈值（boolean 指标阈值 0.90 等价于必须全部通过）；
- reference-dependent 核心指标覆盖率 100%：分母 = manifest 运行前声明
  适用该指标的 case（``metric_applicability`` 覆盖声明优先，no_answer
  case 未声明适用不计入）；运行后发生的证据缺失、业务 error、传输耗尽
  一律留在分母内（skipped 不补齐），只降覆盖率、不降分母；
- 必需指标阈值按适用 case 宏平均比较（agent.task_success>=0.90 业务
  error 计失败、kb.recall_at_5>=0.80、kb.ndcg_at_5>=0.75、
  rag.faithfulness>=0.90 为默认策略；缺失证据按失败保守计）；
- 策略必需指标分母为 0（预声明全部不适用 / 账本重建无 case 行）->
  inconclusive（no_applicable_cases，默认阻断——不得因其他指标达标
  而对未评估的必需指标宣称质量已证明）；
- 配对非劣比较：候选与基线必须同 dataset/evaluator 配置（冻结快照语义
  字段一致，构造期校验，不一致属调用方契约违规直接拒绝）；每项必需
  指标的 case 配对 bootstrap 95% 区间下界不得低于 policy 非劣边际（默认
  -0.02）；bootstrap 以固定种子确定性执行，replicate 按 case 聚类重采样
  （同 case 的 replicate 不作为独立样本）；分位数取 nearest-rank 约定
  （见 domain/evaluation/gate_stats.py）；配对 case 数 < min_paired_cases
  或关键切片 case 数 < min_key_slice_cases 或总 case 数 < min_cases ->
  insufficient_data（inconclusive，默认阻断"质量已证明"）；
- 比例（boolean 指标通过率）同时报告 Wilson 95% 区间（纯函数，
  闭式公式）；
- 未校准 Judge 指标仅 advisory（复用 T8 ``is_gate_eligible``：仅
  automatic / human 可满足必需门禁）：必需指标只有 advisory 证据时门禁
  不得 pass -> inconclusive（advisory_only_metric）；混合证据时只采用
  gate-eligible 结果，advisory 结果不参与、不阻塞；
- Judge error / 单项隔离 error -> infrastructure_error（评估基础设施
  故障阻断门禁，spec 9.2"Judge error 单列并阻断门禁"）；
- 状态收敛优先级：infrastructure_error > gate_fail > inconclusive >
  gate_pass（硬失败优先于不可判定，不可判定优先于通过）；
- Langfuse 不可用不阻断：投递状态由调用方经
  ``delivery_status_counts`` 传入并在结论中单列展示，不参与状态判定。

compare 的账本路径说明：``collect_gate_data`` 从账本重建实验数据
（execute 实验项 producer_event_id 解析 case 身份）；分母优先用
experiment 记录冻结的 manifest_json case 全集恢复（运行后无结果的
case 不剔除，spec 9.2；逐指标适用性未冻结，按"全部适用"保守近似），
无记录时退回"出现过的 case"近似。业务 error 结局无法从账本还原。
生产 compare 应优先复用 run 期 ``gate_data_from_report`` 产物。

CLI 注册见 app/interfaces/cli/commands/evaluate.py 与
app/interfaces/cli/main.py（build_parser + _DISPATCH）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from app.application.evaluation_calibration import is_gate_eligible
from app.application.evaluation_experiment import (
    ExperimentRunReport,
)

logger = logging.getLogger(__name__)
from app.application.evaluation_io import read_json_object
from app.domain.evaluation.gate_stats import (
    WILSON_Z_95,
    bootstrap_lower_index,
    paired_bootstrap_ci_lower,
    wilson_interval,
)
from app.domain.evaluation.models import (
    DatasetManifest,
    EvaluationValidationError,
    canonical_json_bytes,
    metric_applicable,
)

# gate_policy schema 版本（版本化 JSON；未知版本拒绝）
GATE_POLICY_SCHEMA_VERSION = "1"

# 四态（spec 9.2 CI 输出）与 CLI 退出码
GATE_PASS = "gate_pass"
GATE_FAIL = "gate_fail"
INCONCLUSIVE = "inconclusive"
INFRASTRUCTURE_ERROR = "infrastructure_error"
GATE_STATES: tuple[str, ...] = (
    GATE_PASS,
    GATE_FAIL,
    INCONCLUSIVE,
    INFRASTRUCTURE_ERROR,
)
GATE_STATE_EXIT_CODES: Mapping[str, int] = {
    GATE_PASS: 0,
    GATE_FAIL: 1,
    INFRASTRUCTURE_ERROR: 2,
    INCONCLUSIVE: 3,
}

# spec 9.2 初始阈值（版本化策略的默认内容）
DEFAULT_REQUIRED_METRICS: Mapping[str, float] = {
    "agent.task_success": 0.90,
    "kb.recall_at_5": 0.80,
    "kb.ndcg_at_5": 0.75,
    "rag.faithfulness": 0.90,
}
DEFAULT_NON_INFERIORITY_MARGIN: float = -0.02
DEFAULT_MIN_CASES: int = 100
DEFAULT_MIN_PAIRED_CASES: int = 100
DEFAULT_MIN_KEY_SLICE_CASES: int = 10
DEFAULT_BOOTSTRAP_REPLICATES: int = 1000
DEFAULT_BOOTSTRAP_SEED: int = 20261111

# 账本结果分页大小（门禁分数收集与投递状态汇总共用同一常量）
RESULTS_PAGE_LIMIT: int = 200

# 结论 reason 前缀（--json reasons 字符串是机器可读契约；构造与消费
# 两侧共用本组常量，禁止散落字面量——消费侧语义判定只做前缀匹配）
REASON_PREFIX_INSUFFICIENT_DATA = "insufficient_data"
REASON_PREFIX_ADVISORY_ONLY = "advisory_only_metric"
REASON_PREFIX_NO_APPLICABLE_CASES = "no_applicable_cases"
REASON_PREFIX_MISSING_METRIC = "missing_metric"
REASON_PREFIX_JUDGE_ERROR = "judge_error"
REASON_PREFIX_ITEM_ERROR = "item_error"
REASON_PREFIX_THRESHOLD_BELOW = "threshold_below"
REASON_PREFIX_COVERAGE_INCOMPLETE = "coverage_incomplete"
REASON_PREFIX_SAFETY_CASE_FAILED = "safety_case_failed"
REASON_PREFIX_SAFETY_CASE_ADVISORY = "safety_case_advisory"
REASON_PREFIX_NON_INFERIORITY_UNPROVEN = "non_inferiority_unproven"

# 阈值比较浮点容差（对齐 T8：恰在阈值上按达到计，不放大容忍度）
_THRESHOLD_TOLERANCE: float = 1e-9

# 候选/基线配置一致性：必查字段（缺一即拒绝，fail-closed——spec 9.2 要求
# 同 dataset/evaluator/Judge 配置，缺字段不得静默放行）
# 与语义比较字段（双方都有才比）
_BASELINE_REQUIRED_MATCH_FIELDS: tuple[str, ...] = (
    "dataset_hash",
    "evaluator_set_hash",
    "judge_config_hash",
)
_BASELINE_SEMANTIC_MATCH_FIELDS: tuple[str, ...] = (
    "dataset_hash",
    "dataset_version",
    "evaluator_set_hash",
    "target_config_hash",
    "judge_config_hash",
)

# item 结局 -> 门禁 outcome 词汇映射（T13 ITEM_OUTCOMES 子集）
_ITEM_OUTCOME_MAP: Mapping[str, str] = {
    "business_error": "business_error",
    "transport_exhausted": "transport_exhausted",
    "error": "item_error",
}

_GATE_POLICY_FIELDS: tuple[str, ...] = (
    "schema_version",
    "policy_version",
    "required_metrics",
    "coverage_metrics",
    "safety_slices",
    "key_slices",
    "non_inferiority_margin",
    "min_cases",
    "min_paired_cases",
    "min_key_slice_cases",
    "bootstrap_replicates",
    "bootstrap_seed",
)

__all__ = [
    "DEFAULT_BOOTSTRAP_REPLICATES",
    "DEFAULT_BOOTSTRAP_SEED",
    "DEFAULT_MIN_CASES",
    "DEFAULT_MIN_KEY_SLICE_CASES",
    "DEFAULT_MIN_PAIRED_CASES",
    "DEFAULT_NON_INFERIORITY_MARGIN",
    "DEFAULT_REQUIRED_METRICS",
    "GATE_FAIL",
    "GATE_PASS",
    "GATE_POLICY_SCHEMA_VERSION",
    "GATE_STATE_EXIT_CODES",
    "GATE_STATES",
    "INFRASTRUCTURE_ERROR",
    "INCONCLUSIVE",
    "RESULTS_PAGE_LIMIT",
    "WILSON_Z_95",
    "BaselineConfigMismatchError",
    "EvaluationGate",
    "GateConclusion",
    "GateEvaluationRequest",
    "GateExperimentData",
    "GateMetricReport",
    "GatePolicy",
    "GatePolicyError",
    "GateResultsLedger",
    "GateSampleLedger",
    "GateScore",
    "GateSnapshotLedger",
    "collect_experiment_scores",
    "collect_gate_data",
    "default_gate_policy",
    "gate_data_from_report",
    "gate_policy_content",
    "gate_policy_from_content",
    "load_gate_policy",
    "paired_bootstrap_ci_lower",
    "validate_baseline_config",
    "wilson_interval",
    "bootstrap_lower_index",
]


class GatePolicyError(ValueError):
    """gate_policy 加载或校验失败（fail-fast，立即终止）。"""


class BaselineConfigMismatchError(EvaluationValidationError):
    """候选与基线的 dataset/evaluator/Judge 配置不一致（spec 9.2 前置
    条件，调用方契约违规，直接拒绝）。"""


# ---------------------------------------------------------------------------
# gate_policy（版本化 JSON）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatePolicy:
    """版本化门禁策略（spec 9.2 初始阈值的可演进载体）。"""

    policy_version: str
    required_metrics: Mapping[str, float]
    coverage_metrics: tuple[str, ...]
    safety_slices: tuple[str, ...]
    key_slices: tuple[str, ...]
    non_inferiority_margin: float
    min_cases: int
    min_paired_cases: int
    min_key_slice_cases: int
    bootstrap_replicates: int
    bootstrap_seed: int


def default_gate_policy() -> GatePolicy:
    """spec 9.2 初始默认策略。"""
    return GatePolicy(
        policy_version="default-v1",
        required_metrics=dict(DEFAULT_REQUIRED_METRICS),
        coverage_metrics=tuple(DEFAULT_REQUIRED_METRICS),
        safety_slices=(),
        key_slices=(),
        non_inferiority_margin=DEFAULT_NON_INFERIORITY_MARGIN,
        min_cases=DEFAULT_MIN_CASES,
        min_paired_cases=DEFAULT_MIN_PAIRED_CASES,
        min_key_slice_cases=DEFAULT_MIN_KEY_SLICE_CASES,
        bootstrap_replicates=DEFAULT_BOOTSTRAP_REPLICATES,
        bootstrap_seed=DEFAULT_BOOTSTRAP_SEED,
    )


def gate_policy_content(policy: GatePolicy) -> dict[str, Any]:
    """策略 -> 可持久化 JSON 内容（作者侧与 load_gate_policy 同一往返）。"""
    return {
        "schema_version": GATE_POLICY_SCHEMA_VERSION,
        "policy_version": policy.policy_version,
        "required_metrics": dict(policy.required_metrics),
        "coverage_metrics": list(policy.coverage_metrics),
        "safety_slices": list(policy.safety_slices),
        "key_slices": list(policy.key_slices),
        "non_inferiority_margin": policy.non_inferiority_margin,
        "min_cases": policy.min_cases,
        "min_paired_cases": policy.min_paired_cases,
        "min_key_slice_cases": policy.min_key_slice_cases,
        "bootstrap_replicates": policy.bootstrap_replicates,
        "bootstrap_seed": policy.bootstrap_seed,
    }


def _require_finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GatePolicyError(f"{label} 必须是数值: {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise GatePolicyError(f"{label} 必须是有限数值: {value!r}")
    return number


def _require_positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GatePolicyError(f"{label} 必须是正整数: {value!r}")
    return int(value)


def _require_str_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GatePolicyError(f"{label} 必须是字符串数组: {value!r}")
    items = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise GatePolicyError(
                f"{label} 条目必须是非空字符串: {item!r}"
            )
        items.append(item)
    return tuple(items)


def gate_policy_from_content(content: Any) -> GatePolicy:
    """校验并还原策略对象（fail-fast：字段集封闭、类型与值域校验）。"""
    if not isinstance(content, dict):
        raise GatePolicyError("gate_policy 顶层必须是 JSON 对象")
    unknown = sorted(set(content) - set(_GATE_POLICY_FIELDS))
    if unknown:
        raise GatePolicyError(f"gate_policy 存在未知字段（立即终止）: {unknown}")
    missing = [name for name in _GATE_POLICY_FIELDS if name not in content]
    if missing:
        raise GatePolicyError(f"gate_policy 缺少必填字段: {missing}")
    if content["schema_version"] != GATE_POLICY_SCHEMA_VERSION:
        raise GatePolicyError(
            f"不支持的 schema_version: {content['schema_version']!r}，"
            f"仅支持 {GATE_POLICY_SCHEMA_VERSION!r}"
        )
    policy_version = content["policy_version"]
    if not isinstance(policy_version, str) or not policy_version:
        raise GatePolicyError(
            f"policy_version 必须是非空字符串: {policy_version!r}"
        )
    raw_metrics = content["required_metrics"]
    if not isinstance(raw_metrics, dict) or not raw_metrics:
        raise GatePolicyError("required_metrics 必须是非空 JSON 对象")
    required_metrics: dict[str, float] = {}
    for metric, threshold in raw_metrics.items():
        if not isinstance(metric, str) or not metric:
            raise GatePolicyError(
                f"required_metrics 键必须是非空字符串: {metric!r}"
            )
        threshold_value = _require_finite_float(
            threshold, f"required_metrics[{metric!r}]"
        )
        if not 0.0 <= threshold_value <= 1.0:
            raise GatePolicyError(
                f"required_metrics[{metric!r}] 阈值必须在 [0, 1]: "
                f"{threshold!r}"
            )
        required_metrics[metric] = threshold_value
    margin = _require_finite_float(
        content["non_inferiority_margin"], "non_inferiority_margin"
    )
    if margin > 0:
        raise GatePolicyError(
            f"non_inferiority_margin 必须 <= 0（非劣边际）: {margin!r}"
        )
    return GatePolicy(
        policy_version=policy_version,
        required_metrics=required_metrics,
        coverage_metrics=_require_str_list(content["coverage_metrics"], "coverage_metrics"),
        safety_slices=_require_str_list(content["safety_slices"], "safety_slices"),
        key_slices=_require_str_list(content["key_slices"], "key_slices"),
        non_inferiority_margin=margin,
        min_cases=_require_positive_int(content["min_cases"], "min_cases"),
        min_paired_cases=_require_positive_int(
            content["min_paired_cases"], "min_paired_cases"
        ),
        min_key_slice_cases=_require_positive_int(
            content["min_key_slice_cases"], "min_key_slice_cases"
        ),
        bootstrap_replicates=_require_positive_int(
            content["bootstrap_replicates"], "bootstrap_replicates"
        ),
        bootstrap_seed=_require_positive_int(
            content["bootstrap_seed"], "bootstrap_seed"
        ),
    )


def load_gate_policy(path: str) -> GatePolicy:
    """从版本化 JSON 文件加载门禁策略（fail-fast，不返回部分策略）。"""
    content = read_json_object(path, error=GatePolicyError)
    return gate_policy_from_content(content)


# ---------------------------------------------------------------------------
# 账本读取端口（与 T8 校准端口同模式：Application 内 Protocol，组合根
# 注入 SQLiteEvaluationStore 或测试 fake；结构性类型仅作契约说明，不做
# 运行时 isinstance 检查）
# ---------------------------------------------------------------------------


@runtime_checkable
class GateResultsLedger(Protocol):
    """门禁结果账本端口：分页读取实验结果行与 job 定位（最小形状）。"""

    async def query_results_page(
        self, **kwargs: Any
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        """分页读取结果行（experiment_id/cursor/limit 关键字入参）。"""
        ...

    async def get_job(self, job_id: str) -> Mapping[str, Any]:
        """按 job_id 读取作业行（sample 与定义引用定位）。"""
        ...


@runtime_checkable
class GateSampleLedger(GateResultsLedger, Protocol):
    """门禁样本账本端口：compare 路径还需按 sample_id 回读样本行
    （producer_event_id 解析 case 身份）。"""

    async def load_sample(self, sample_id: str) -> Mapping[str, Any]:
        """按 sample_id 读取样本行。"""
        ...


@runtime_checkable
class GateSnapshotLedger(Protocol):
    """门禁冻结快照还原端口（compare 路径可选形状）。

    账本实现可以只满足 :class:`GateResultsLedger`：此时快照还原跳过，
    冻结快照为空，候选/基线配置校验 fail-closed 拒绝（不静默放行）。"""

    async def get_experiment(self, experiment_id: str) -> Mapping[str, Any]:
        """按 experiment_id 读取实验记录（冻结快照字段来源）。"""
        ...

    async def get_definition(
        self, definition_id: str, version: str
    ) -> Mapping[str, Any]:
        """读取定义行（judge 模型/参数来源，重算 judge_config_hash）。"""
        ...


# ---------------------------------------------------------------------------
# 门禁输入数据
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateScore:
    """账本单条评分（job -> result 行的门禁视角投影）。

    ``passed`` 是结果行原始通过标记，仅作投影保留：门禁按 ``value``
    结合阈值重算通过（阈值随 policy 演进，行内快照不作为判定依据），
    判定路径不直接消费本字段。"""

    metric_name: str
    status: str  # value / skipped / error
    value: Any
    passed: bool | None  # 结果行原始通过标记（投影保留，判定不消费）
    provenance: str  # automatic / human / calibration / self_judge


@dataclass(frozen=True)
class GateExperimentData:
    """门禁视角的单实验数据（case 粒度，replicate 评分保留）。

    applicable_cases 是"运行前声明适用"的分母坐标（manifest 预声明；
    账本重建路径以出现过的 case 近似，见模块 docstring）。scores /
    outcomes 以 (case_id, metric) 为键。denominator_basis 声明分母来源：
    ``pre_declared`` = manifest 运行前声明；``observed`` = 账本出现过的
    case 近似（compare 账本路径 / 无 manifest 的 replay），该口径进入
    结论输出供消费方甄别。
    """

    experiment_id: str
    metrics: tuple[str, ...]
    frozen_snapshot: Mapping[str, Any] = field(default_factory=dict)
    case_slices: Mapping[str, str] = field(default_factory=dict)
    applicable_cases: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    scores: Mapping[tuple[str, str], tuple[GateScore, ...]] = field(
        default_factory=dict
    )
    outcomes: Mapping[tuple[str, str], str] = field(default_factory=dict)
    denominator_basis: str = "pre_declared"
    # 每 case 预期 replicate 数（case_id -> N）：复制实验中已到证据不得
    # 代表全部 replicate（spec 9.1 禁止挑选成功结果）。空映射 = 未知
    # （单次执行语义，与既有行为一致）
    expected_replicates: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.denominator_basis not in ("pre_declared", "observed"):
            raise EvaluationValidationError(
                f"非法 denominator_basis: {self.denominator_basis!r}，"
                "仅允许 'pre_declared' / 'observed'"
            )


def gate_data_from_report(
    report: ExperimentRunReport,
    dataset: DatasetManifest | None,
    scores_by_sample: Mapping[str, Mapping[str, GateScore]],
    *,
    evaluation_outcomes: Mapping[str, Mapping[str, str]] | None = None,
) -> GateExperimentData:
    """从实验报告 + 数据集 + 账本分数构造门禁数据（run/replay 路径）。

    - case 身份取实验项 case_id（case x replicate 粒度的 item 归并到
      case）；sample -> item 经 ``ExperimentItemRecord.sample_ids`` 关联；
    - 分母坐标 = manifest 预声明适用该指标的 case（dataset=None 时以
      出现过的 case 近似，replay 场景无 manifest 预声明可用）；
    - 结局：item 结局 business_error/transport_exhausted/error 映射为
      case 级 outcome（error -> item_error）；Judge error 来自额外传入的
      evaluation_outcomes（item_id -> metric -> judge_error）与评分行
      status=error。
    """
    extra_outcomes = evaluation_outcomes or {}
    # 每 case 预期 replicate 数 = 该 case 的实验项数（execute 前一次性
    # 创建全部 case x replicate 项，与是否产出证据无关）
    expected_replicates: dict[str, int] = {}
    for item in report.items:
        if item.case_id:
            expected_replicates[item.case_id] = (
                expected_replicates.get(item.case_id, 0) + 1
            )
    case_slices: dict[str, str] = {}
    applicable: dict[str, set[str]] = {m: set() for m in report.metrics}
    if dataset is not None:
        for case in dataset.cases:
            case_slices[case.case_id] = case.slice
            for metric_name in report.metrics:
                if metric_applicable(case, metric_name):
                    applicable[metric_name].add(case.case_id)
    else:
        for item in report.items:
            if item.case_id:
                for metric_name in report.metrics:
                    applicable[metric_name].add(item.case_id)

    case_scores: dict[tuple[str, str], list[GateScore]] = {}
    case_outcomes: dict[tuple[str, str], str] = {}
    for item in report.items:
        outcome = _ITEM_OUTCOME_MAP.get(item.outcome)
        metric_scores: dict[str, GateScore] = {}
        for sample_id in item.sample_ids:
            for metric_name, score in (
                scores_by_sample.get(sample_id) or {}
            ).items():
                if metric_name in applicable:
                    metric_scores[metric_name] = score
        for metric_name in report.metrics:
            if item.case_id not in applicable[metric_name]:
                continue
            key = (item.case_id, metric_name)
            if metric_name in metric_scores:
                case_scores.setdefault(key, []).append(
                    metric_scores[metric_name]
                )
                if metric_scores[metric_name].status == "error":
                    case_outcomes[key] = "judge_error"
            if outcome is not None:
                case_outcomes.setdefault(key, outcome)
            judge_error = (extra_outcomes.get(item.item_id) or {}).get(
                metric_name
            )
            if judge_error == "judge_error":
                case_outcomes[key] = "judge_error"
    return GateExperimentData(
        experiment_id=report.experiment_id,
        metrics=tuple(report.metrics),
        frozen_snapshot=dict(report.frozen_snapshot),
        case_slices=case_slices,
        applicable_cases={
            metric: tuple(sorted(cases))
            for metric, cases in applicable.items()
        },
        scores={
            key: tuple(entry) for key, entry in case_scores.items()
        },
        outcomes=dict(case_outcomes),
        denominator_basis=(
            "pre_declared" if dataset is not None else "observed"
        ),
        expected_replicates=expected_replicates,
    )


# ---------------------------------------------------------------------------
# 账本读取（真实服务路径）
# ---------------------------------------------------------------------------


async def collect_experiment_scores(
    ledger: GateResultsLedger, experiment_id: str
) -> dict[str, dict[str, GateScore]]:
    """分页读取实验的 current 视图结果，按 sample_id 分组为 GateScore。

    ledger 契约见 :class:`GateResultsLedger`（SQLiteEvaluationStore /
    fake 均可）。human 结果（feedback 路径，无 job）不参与实验门禁。"""
    scores, _ = await _collect_scores_and_jobs(ledger, experiment_id)
    return scores


async def _collect_scores_and_jobs(
    ledger: GateResultsLedger, experiment_id: str
) -> tuple[dict[str, dict[str, GateScore]], set[tuple[str, str]]]:
    """collect_experiment_scores 的内部形态：同时返回结果行涉及的
    (definition_id, version) 集合（compare 路径重算 judge hash 用）。"""
    scores: dict[str, dict[str, GateScore]] = {}
    definition_versions: set[tuple[str, str]] = set()
    cursor: str | None = None
    while True:
        rows, next_cursor = await ledger.query_results_page(
            experiment_id=experiment_id, cursor=cursor,
            limit=RESULTS_PAGE_LIMIT,
        )
        for row in rows:
            job_id = row.get("job_id")
            if not job_id:
                continue
            job = await ledger.get_job(job_id)
            sample_id = str(job.get("sample_id") or "")
            metric_name = str(row.get("metric_name") or "")
            if not sample_id or not metric_name:
                continue
            definition_id = str(job.get("definition_id") or "")
            definition_version = str(job.get("definition_version") or "")
            if definition_id and definition_version:
                definition_versions.add((definition_id, definition_version))
            scores.setdefault(sample_id, {})[metric_name] = GateScore(
                metric_name=metric_name,
                status=str(row.get("status") or ""),
                value=row.get("typed_value"),
                passed=row.get("passed"),
                provenance=str(row.get("provenance") or ""),
            )
        if next_cursor is None:
            break
        cursor = next_cursor
    return scores, definition_versions


async def collect_gate_data(
    ledger: GateSampleLedger, experiment_id: str
) -> GateExperimentData:
    """从账本重建实验的门禁数据（compare 路径，execute 实验项）。

    case 身份从 sample 的 producer_event_id 解析
    （``experiment:{experiment_id}:{item_id}:...`` -> item_id ->
    ``case_id#r{replicate}``）；业务结局不可从账本还原（Judge error
    以结果行 status=error 保留）。

    分母恢复：experiment 记录冻结的 manifest_json 含预声明 case 全集
    （仅 case_ids，逐指标适用性未冻结）——有记录时以该全集作为每个
    观测指标的分母（运行后无结果的 case 不从分母剔除，spec 9.2；逐
    指标适用性按"全部适用"保守近似，只会放大分母不会缩小，fail-closed
    方向）。无 manifest 记录（历史实验）时退回"出现过的 case"近似
    （denominator_basis=observed）。预期 replicate 数取各 case 观测到
    的去重 replicate 下标数（复制实验部分 replicate 缺证据不掩盖）。

    冻结快照从 experiment 记录还原（dataset_hash / evaluator_set_hash /
    target_config_hash）并按 T13 同一公式从 jobs 观测到的定义重算
    judge_config_hash——候选/基线配置一致性校验（fail-closed）依赖这三个
    hash；账本无 experiment 记录形状时快照为空，后续比较一律拒绝。
    只统计产生了结果的定义：无任何结果行的定义不进 judge hash（与候选
    侧快照可能不一致 -> 配置校验 fail-closed 拒绝，不静默放行）。"""
    scores_by_sample, definition_versions = await _collect_scores_and_jobs(
        ledger, experiment_id
    )
    experiment_row = await _load_experiment_row(ledger, experiment_id)
    item_cases: dict[str, str] = {}
    case_scores: dict[tuple[str, str], list[GateScore]] = {}
    case_outcomes: dict[tuple[str, str], str] = {}
    metric_names: set[str] = set()
    case_ids: set[str] = set()
    case_replicates: dict[str, set[int]] = {}
    for sample_id, per_metric in scores_by_sample.items():
        # Phase 5 扫描修复（Blocking）：账本读失败属系统异常，必须上抛
        # （CLI 操作失败统一 exit 2 = infrastructure_error 方向）——静默
        # continue 会把该 sample 从门禁分母中无声移除，瞬时存储故障会
        # 虚假抬高覆盖率/通过率（fail-open）。"证据缺失留分母"原则只
        # 适用于证据缺失，不适用于系统异常吞咽。
        sample = await ledger.load_sample(sample_id)
        item_id = _item_id_from_producer_event(
            str(sample.get("producer_event_id") or "")
        )
        if item_id is None:
            continue
        case_id, replicate = _case_and_replicate_from_item_id(item_id)
        item_cases[item_id] = case_id
        case_ids.add(case_id)
        case_replicates.setdefault(case_id, set()).add(replicate)
        for metric_name, score in per_metric.items():
            metric_names.add(metric_name)
            key = (case_id, metric_name)
            case_scores.setdefault(key, []).append(score)
            if score.status == "error":
                case_outcomes[key] = "judge_error"
    metrics = tuple(sorted(metric_names))
    expected_replicates = {
        case_id: len(replicates)
        for case_id, replicates in case_replicates.items()
    }
    manifest_cases = _manifest_case_universe(experiment_row)
    if manifest_cases is not None:
        universe = manifest_cases | case_ids
        applicable_cases = {
            metric: tuple(sorted(universe)) for metric in metrics
        }
        denominator_basis = "pre_declared"
    else:
        applicable_cases = {
            metric: tuple(sorted(case_ids)) for metric in metrics
        }
        denominator_basis = "observed"
    frozen_snapshot = await _rebuild_frozen_snapshot(
        ledger,
        experiment_id,
        definition_versions,
        experiment_row=experiment_row,
    )
    return GateExperimentData(
        experiment_id=experiment_id,
        metrics=metrics,
        frozen_snapshot=frozen_snapshot,
        case_slices={},
        applicable_cases=applicable_cases,
        scores={key: tuple(entry) for key, entry in case_scores.items()},
        outcomes=dict(case_outcomes),
        denominator_basis=denominator_basis,
        expected_replicates=expected_replicates,
    )


async def _load_experiment_row(
    ledger: GateResultsLedger | GateSnapshotLedger, experiment_id: str
) -> Mapping[str, Any]:
    """compare 路径读取 experiment 记录；端口缺失或读失败返回空映射
    （manifest 恢复降级为 observed 近似，快照校验 fail-closed）。"""
    get_experiment = getattr(ledger, "get_experiment", None)
    if get_experiment is None:
        return {}
    try:
        return await get_experiment(experiment_id)
    except Exception:
        logger.warning(
            "gate compare experiment read failed; manifest recovery "
            "degraded to observed (fail-closed)",
            exc_info=True,
        )
        return {}


def _manifest_case_universe(
    experiment_row: Mapping[str, Any],
) -> set[str] | None:
    """experiment 记录 manifest_json -> 预声明 case 全集；不可用返回
    None（manifest_json 缺失/为空按历史实验处理，损坏则拒绝——冻结
    数据损坏不能静默降级为更小的分母）。"""
    raw = str(experiment_row.get("manifest_json") or "")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        case_ids = parsed["case_ids"]
        if not isinstance(case_ids, list) or not case_ids or not all(
            isinstance(case_id, str) and case_id for case_id in case_ids
        ):
            raise ValueError("case_ids 必须是非空字符串列表")
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise EvaluationValidationError(
            f"experiment manifest_json 损坏，无法恢复预声明分母: {exc}"
        ) from exc
    return set(case_ids)


def _case_and_replicate_from_item_id(item_id: str) -> tuple[str, int]:
    """item_id（``case_id#r{replicate}``）-> (case_id, replicate)；无
    replicate 后缀按单次执行（r0）处理。"""
    head, sep, tail = item_id.rpartition("#r")
    if sep and tail.isdigit():
        return head, int(tail)
    return item_id, 0


async def _rebuild_frozen_snapshot(
    ledger: GateResultsLedger | GateSnapshotLedger, experiment_id: str,
    definition_versions: set[tuple[str, str]],
    *,
    experiment_row: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """从 experiment 记录 + 观测到的定义重建配置一致性所需快照字段。

    快照端口为可选形状（:class:`GateSnapshotLedger`）：实现未提供
    get_experiment / get_definition 时（getattr 探测）返回空快照，比较
    在配置校验处 fail-closed。experiment_row 由调用方预先读取（compare
    路径复用同一行做 manifest 恢复）；未提供时按端口自行探测。"""
    snapshot: dict[str, Any] = {}
    if experiment_row is None:
        experiment_row = await _load_experiment_row(ledger, experiment_id)
    row = experiment_row
    for name in (
            "dataset_hash",
            "evaluator_set_hash",
            "target_config_hash",
        ):
            if row.get(name):
                snapshot[name] = str(row[name])
    get_definition = getattr(ledger, "get_definition", None)
    if get_definition is None or not definition_versions:
        return snapshot
    judge_entries = []
    for definition_id, version in sorted(definition_versions):
        try:
            row = await get_definition(definition_id, version)
        except Exception:
            # Phase 5 扫描修复（Warning）：fail-closed 方向（缺 definition
            # -> judge hash 变化 -> 配置校验拒绝），留日志区分瞬时故障
            # 与真实缺失
            logger.warning(
                "gate frozen snapshot definition read failed: "
                "definition=%s version=%s (fail-closed)",
                definition_id,
                version,
                exc_info=True,
            )
            continue
        definition = row.get("definition") or {}
        judge_entries.append(
            {
                "definition_id": definition_id,
                "version": version,
                "judge_model": definition.get("judge_model"),
                "judge_params": dict(definition.get("judge_params") or {}),
            }
        )
    if judge_entries:
        snapshot["judge_config_hash"] = hashlib.sha256(
            canonical_json_bytes(judge_entries)
        ).hexdigest()
    return snapshot


def _item_id_from_producer_event(producer_event_id: str) -> str | None:
    """execute 实验 sample 的 producer_event_id -> item_id（非实验样本
    或线上样本返回 None，不参与门禁）。"""
    parts = producer_event_id.split(":")
    if len(parts) < 3 or parts[0] != "experiment":
        return None
    item_id = parts[2]
    if not item_id or "#r" not in item_id:
        return None
    return item_id


# ---------------------------------------------------------------------------
# 门禁编排
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateMetricReport:
    """单指标门禁明细（--json 稳定字段）。"""

    metric_name: str
    threshold: float
    macro_value: float
    denominator: int
    passed_count: int
    value_count: int
    coverage: float
    wilson_ci: tuple[float, float] | None
    gate_eligible: bool
    advisory_only: bool
    business_failures: int
    judge_errors: int
    paired_cases: int | None
    bootstrap_ci_lower: float | None
    non_inferiority_ok: bool | None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "metric_name": self.metric_name,
            "threshold": self.threshold,
            "macro_value": self.macro_value,
            "denominator": self.denominator,
            "passed_count": self.passed_count,
            "value_count": self.value_count,
            "coverage": self.coverage,
            "wilson_ci": (
                None if self.wilson_ci is None else list(self.wilson_ci)
            ),
            "gate_eligible": self.gate_eligible,
            "advisory_only": self.advisory_only,
            "business_failures": self.business_failures,
            "judge_errors": self.judge_errors,
            "paired_cases": self.paired_cases,
            "bootstrap_ci_lower": self.bootstrap_ci_lower,
            "non_inferiority_ok": self.non_inferiority_ok,
        }


@dataclass(frozen=True)
class GateConclusion:
    """门禁四态结论（spec 9.2；to_mapping 字段名即 --json 契约）。

    denominator_basis 单列覆盖率分母来源：``pre_declared``（manifest
    运行前声明）或 ``observed``（账本出现过的 case 近似——compare 账本
    路径 / 无 manifest 的 replay），消费方据此甄别覆盖率口径。"""

    state: str
    reasons: tuple[str, ...]
    metrics: tuple[GateMetricReport, ...]
    policy_version: str
    insufficient_data: bool
    experiment_id: str
    baseline_experiment_id: str | None
    delivery_status_counts: Mapping[str, Mapping[str, int]]
    denominator_basis: str = "pre_declared"

    def __post_init__(self) -> None:
        if self.state not in GATE_STATES:
            raise EvaluationValidationError(
                f"非法门禁状态: {self.state!r}，仅允许 {GATE_STATES}"
            )
        if self.denominator_basis not in ("pre_declared", "observed"):
            raise EvaluationValidationError(
                f"非法 denominator_basis: {self.denominator_basis!r}，"
                "仅允许 'pre_declared' / 'observed'"
            )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "reasons": list(self.reasons),
            "metrics": [metric.to_mapping() for metric in self.metrics],
            "policy_version": self.policy_version,
            "insufficient_data": self.insufficient_data,
            "experiment_id": self.experiment_id,
            "baseline_experiment_id": self.baseline_experiment_id,
            "delivery_status_counts": {
                sink: dict(counts)
                for sink, counts in self.delivery_status_counts.items()
            },
            "denominator_basis": self.denominator_basis,
        }


@dataclass(frozen=True)
class GateEvaluationRequest:
    """门禁评估输入（候选必填，基线仅配对非劣比较需要）。"""

    candidate: GateExperimentData
    policy: GatePolicy
    baseline: GateExperimentData | None = None
    delivery_status_counts: Mapping[str, Mapping[str, int]] = field(
        default_factory=dict
    )


def validate_baseline_config(
    candidate: GateExperimentData, baseline: GateExperimentData
) -> None:
    """候选/基线配置一致性核验（spec 9.2 前置条件；自 EvaluationGate
    的门禁前置校验公开为模块级函数，供门禁与 analytics 可比性校验共用
    同一权威算法，禁止两份实现分叉）。"""
    candidate_snapshot = candidate.frozen_snapshot
    baseline_snapshot = baseline.frozen_snapshot
    for name in _BASELINE_REQUIRED_MATCH_FIELDS:
        if (
            name not in candidate_snapshot
            or name not in baseline_snapshot
        ):
            raise BaselineConfigMismatchError(
                f"候选/基线冻结快照缺少配置字段 {name!r}，无法核验"
                "同 dataset/evaluator/Judge 配置（spec 9.2 前置条件）"
            )
    for name in _BASELINE_SEMANTIC_MATCH_FIELDS:
        if (
            name not in candidate_snapshot
            or name not in baseline_snapshot
        ):
            continue
        if candidate_snapshot[name] != baseline_snapshot[name]:
            raise BaselineConfigMismatchError(
                f"候选/基线配置不一致: {name} "
                f"({candidate_snapshot[name]!r} != "
                f"{baseline_snapshot[name]!r})"
            )


class EvaluationGate:
    """CI 门禁编排器（spec 9.2；无 IO，全部数据经请求注入）。"""

    async def evaluate(self, request: GateEvaluationRequest) -> GateConclusion:
        candidate = request.candidate
        policy = request.policy
        baseline = request.baseline
        if baseline is not None:
            validate_baseline_config(candidate, baseline)
        evaluated_metrics = tuple(
            metric
            for metric in policy.required_metrics
            if metric in candidate.metrics
        )
        # 策略必需指标缺位于实验指标集（无任何结果）：不得因交集外指标
        # 被忽略而 gate_pass（spec 9.2 必需指标不能缺位、AC15 关键缺失
        # 不能显示通过）——按证据不足阻断，需实验补齐指标或改用窄策略
        fail_reasons: list[str] = []
        inconclusive_reasons: list[str] = []
        infra_reasons: list[str] = []
        for metric_name in policy.required_metrics:
            if metric_name not in candidate.metrics:
                inconclusive_reasons.append(
                    f"{REASON_PREFIX_MISSING_METRIC}:{metric_name}"
                )
        metric_reports: list[GateMetricReport] = []
        case_values: dict[str, dict[str, float | None]] = {}
        advisory_metrics: set[str] = set()
        total_cases: set[str] = set()

        for metric_name in evaluated_metrics:
            report = self._evaluate_metric(
                metric_name=metric_name,
                policy=policy,
                candidate=candidate,
                baseline=baseline,
                fail_reasons=fail_reasons,
                inconclusive_reasons=inconclusive_reasons,
                infra_reasons=infra_reasons,
                case_values=case_values,
            )
            metric_reports.append(report)
            if report.advisory_only:
                advisory_metrics.add(metric_name)
            total_cases.update(
                candidate.applicable_cases.get(metric_name, ())
            )

        self._evaluate_safety_cases(
            policy=policy,
            candidate=candidate,
            evaluated_metrics=evaluated_metrics,
            case_values=case_values,
            advisory_metrics=advisory_metrics,
            fail_reasons=fail_reasons,
            inconclusive_reasons=inconclusive_reasons,
        )
        self._evaluate_scale(
            policy=policy,
            candidate=candidate,
            total_cases=total_cases,
            inconclusive_reasons=inconclusive_reasons,
        )

        insufficient_data = any(
            reason.startswith(REASON_PREFIX_INSUFFICIENT_DATA)
            for reason in inconclusive_reasons
        )
        if infra_reasons:
            state = INFRASTRUCTURE_ERROR
        elif fail_reasons:
            state = GATE_FAIL
        elif inconclusive_reasons:
            state = INCONCLUSIVE
        else:
            state = GATE_PASS
        # 去重保序（同一指标多 case 的 judge_error 只报告一次）
        reasons = tuple(dict.fromkeys(
            infra_reasons + fail_reasons + inconclusive_reasons
        ))
        return GateConclusion(
            state=state,
            reasons=reasons,
            metrics=tuple(metric_reports),
            policy_version=policy.policy_version,
            insufficient_data=insufficient_data,
            experiment_id=candidate.experiment_id,
            baseline_experiment_id=(
                baseline.experiment_id if baseline is not None else None
            ),
            delivery_status_counts={
                sink: dict(counts)
                for sink, counts in request.delivery_status_counts.items()
            },
            denominator_basis=candidate.denominator_basis,
        )

    # ------------------------------------------------------------------
    # 单指标评估
    # ------------------------------------------------------------------

    def _evaluate_metric(
        self,
        *,
        metric_name: str,
        policy: GatePolicy,
        candidate: GateExperimentData,
        baseline: GateExperimentData | None,
        fail_reasons: list[str],
        inconclusive_reasons: list[str],
        infra_reasons: list[str],
        case_values: dict[str, dict[str, float | None]],
    ) -> GateMetricReport:
        threshold = policy.required_metrics[metric_name]
        applicable = candidate.applicable_cases.get(metric_name, ())
        denominator = len(applicable)
        if not denominator:
            # 策略必需指标分母为 0（manifest 预声明全部不适用，或账本
            # 重建路径无任何 case 行）：无证据可评估，默认阻断——不得因
            # 其他指标达标而 pass（fail-open 禁止，spec 9.2 覆盖率 100%
            # 与"质量已证明"的分母纪律）
            inconclusive_reasons.append(
                f"{REASON_PREFIX_NO_APPLICABLE_CASES}:{metric_name}"
            )
        all_value_scores: list[GateScore] = []
        values: dict[str, float | None] = {}
        business_failures = 0
        judge_errors = 0
        gate_eligible_any = False
        any_scores = False
        for case_id in applicable:
            scores = candidate.scores.get((case_id, metric_name), ())
            if scores:
                any_scores = True
            outcome = candidate.outcomes.get((case_id, metric_name))
            if outcome == "business_error":
                business_failures += 1
            if outcome == "judge_error" or any(
                score.status == "error" for score in scores
            ):
                judge_errors += 1
                infra_reasons.append(
                    f"{REASON_PREFIX_JUDGE_ERROR}:{metric_name}"
                )
            if outcome == "item_error":
                infra_reasons.append(
                    f"{REASON_PREFIX_ITEM_ERROR}:{metric_name}"
                )
            eligible = [
                score
                for score in scores
                if is_gate_eligible(score.provenance)
            ]
            if eligible:
                gate_eligible_any = True
            case_value = self._case_value(eligible)
            expected_replicates = candidate.expected_replicates.get(case_id)
            if expected_replicates is not None and (
                len(eligible) < expected_replicates
            ):
                # 复制实验存在缺证据 replicate（评分不足或业务 error）：
                # 已到证据不得代表全部 replicate（spec 9.1 禁止挑选成功
                # 结果）——case 值按缺失证据处理，留在分母不补齐
                case_value = None
            values[case_id] = case_value
            if case_value is not None:
                all_value_scores.extend(eligible)
        case_values[metric_name] = values
        # 覆盖 = 达成确定门禁结论的 case：有效评分，或业务 error（已判定
        # 失败，spec 9.2 计任务失败）；证据缺失/传输耗尽不得用 skipped 补齐
        value_count = sum(
            1
            for case_id, value in values.items()
            if value is not None
            or candidate.outcomes.get((case_id, metric_name)) == "business_error"
        )
        coverage = value_count / denominator if denominator else 0.0
        # 宏平均：缺失证据按失败保守计（分母不剔除，spec 9.2）
        macro_value = (
            sum(value for value in values.values() if value is not None)
            / denominator
            if denominator
            else 0.0
        )
        passed_count = (
            sum(
                1
                for value in values.values()
                if value is not None
                and value >= threshold - _THRESHOLD_TOLERANCE
            )
            if denominator
            else 0
        )
        advisory_only = any_scores and not gate_eligible_any
        if advisory_only:
            # 未校准指标仅 advisory：必需门禁不得自动 pass（spec 9.2/10）；
            # advisory 证据不可用于判定，覆盖率/阈值检验对其无意义（缺失
            # 的是可采信证据，不是被评估的证据）
            inconclusive_reasons.append(
                f"{REASON_PREFIX_ADVISORY_ONLY}:{metric_name}"
            )
        if (
            not advisory_only
            and denominator
            and metric_name in policy.coverage_metrics
            and coverage < 1.0 - _THRESHOLD_TOLERANCE
        ):
            fail_reasons.append(
                f"{REASON_PREFIX_COVERAGE_INCOMPLETE}:{metric_name}"
                f"（{value_count}/{denominator}）"
            )
        if (
            not advisory_only
            and denominator
            and macro_value < threshold - _THRESHOLD_TOLERANCE
        ):
            fail_reasons.append(
                f"{REASON_PREFIX_THRESHOLD_BELOW}:{metric_name}"
                f"（{macro_value:.4f} < {threshold}）"
            )
        wilson_ci: tuple[float, float] | None = None
        if (
            denominator
            and all_value_scores
            and all(
                isinstance(score.value, bool)
                for score in all_value_scores
            )
        ):
            wilson_ci = wilson_interval(passed_count, denominator)
        paired_cases: int | None = None
        bootstrap_ci_lower: float | None = None
        non_inferiority_ok: bool | None = None
        if baseline is not None and metric_name in baseline.metrics:
            (
                paired_cases,
                bootstrap_ci_lower,
                non_inferiority_ok,
            ) = self._evaluate_non_inferiority(
                metric_name=metric_name,
                policy=policy,
                candidate=candidate,
                baseline=baseline,
                candidate_values=values,
                inconclusive_reasons=inconclusive_reasons,
            )
        return GateMetricReport(
            metric_name=metric_name,
            threshold=threshold,
            macro_value=macro_value,
            denominator=denominator,
            passed_count=passed_count,
            value_count=value_count,
            coverage=coverage,
            wilson_ci=wilson_ci,
            gate_eligible=gate_eligible_any,
            advisory_only=advisory_only,
            business_failures=business_failures,
            judge_errors=judge_errors,
            paired_cases=paired_cases,
            bootstrap_ci_lower=bootstrap_ci_lower,
            non_inferiority_ok=non_inferiority_ok,
        )

    @staticmethod
    def _case_value(scores: Sequence[GateScore]) -> float | None:
        """case 值 = 该 case 全部 replicate 的 gate-eligible value 评分
        均值（boolean -> 1/0）；无有效评分为 None（缺失证据，留在分母）。"""
        values: list[float] = []
        for score in scores:
            if score.status != "value":
                continue
            value = score.value
            if isinstance(value, bool):
                values.append(1.0 if value else 0.0)
            elif isinstance(value, (int, float)) and math.isfinite(
                float(value)
            ):
                values.append(float(value))
        if not values:
            return None
        return sum(values) / len(values)

    def _evaluate_non_inferiority(
        self,
        *,
        metric_name: str,
        policy: GatePolicy,
        candidate: GateExperimentData,
        baseline: GateExperimentData,
        candidate_values: Mapping[str, float | None],
        inconclusive_reasons: list[str],
    ) -> tuple[int, float | None, bool | None]:
        """case 配对非劣比较：候选减基线逐 case 配对，bootstrap 95% 区间
        下界不得低于非劣边际；配对数不足报 insufficient_data。"""
        baseline_applicable = set(
            baseline.applicable_cases.get(metric_name, ())
        )
        diffs: dict[str, tuple[float, ...]] = {}
        for case_id, candidate_value in candidate_values.items():
            if case_id not in baseline_applicable:
                continue
            baseline_scores = baseline.scores.get(
                (case_id, metric_name), ()
            )
            baseline_value = self._case_value(
                [
                    score
                    for score in baseline_scores
                    if is_gate_eligible(score.provenance)
                ]
            )
            if candidate_value is None or baseline_value is None:
                # 缺失证据的 case 不构造伪造配对：直接减少配对数
                #（可能触发 insufficient_data，不得凭空补 0）
                continue
            diffs[case_id] = (candidate_value - baseline_value,)
        paired_cases = len(diffs)
        if paired_cases < policy.min_paired_cases:
            inconclusive_reasons.append(
                f"{REASON_PREFIX_INSUFFICIENT_DATA}:paired:{metric_name}"
                f"（{paired_cases} < {policy.min_paired_cases}）"
            )
            return paired_cases, None, None
        lower = paired_bootstrap_ci_lower(
            diffs,
            seed=policy.bootstrap_seed,
            replicates=policy.bootstrap_replicates,
        )
        non_inferiority_ok = (
            lower >= policy.non_inferiority_margin - _THRESHOLD_TOLERANCE
        )
        if not non_inferiority_ok:
            # 区间无法证明非劣：inconclusive（默认阻断，人工豁免需留痕）
            inconclusive_reasons.append(
                f"{REASON_PREFIX_NON_INFERIORITY_UNPROVEN}:{metric_name}"
                f"（CI 下界 {lower:.4f} < {policy.non_inferiority_margin}）"
            )
        return paired_cases, lower, non_inferiority_ok

    # ------------------------------------------------------------------
    # 安全/工具约束用例与规模
    # ------------------------------------------------------------------

    @staticmethod
    def _evaluate_safety_cases(
        *,
        policy: GatePolicy,
        candidate: GateExperimentData,
        evaluated_metrics: Sequence[str],
        case_values: Mapping[str, Mapping[str, float | None]],
        advisory_metrics: set[str],
        fail_reasons: list[str],
        inconclusive_reasons: list[str],
    ) -> None:
        if not policy.safety_slices:
            return
        for case_id, slice_name in candidate.case_slices.items():
            if slice_name not in policy.safety_slices:
                continue
            for metric_name in evaluated_metrics:
                if case_id not in candidate.applicable_cases.get(
                    metric_name, ()
                ):
                    continue
                case_value = case_values[metric_name].get(case_id)
                threshold = policy.required_metrics[metric_name]
                if (
                    case_value is None
                    or case_value < threshold - _THRESHOLD_TOLERANCE
                ):
                    if metric_name in advisory_metrics:
                        # advisory 指标的 safety case 同样不可判定：
                        # 必需门禁不得 pass，但也不得伪称硬失败（与
                        # 覆盖率/阈值检查的 advisory 语义一致）
                        inconclusive_reasons.append(
                            f"{REASON_PREFIX_SAFETY_CASE_ADVISORY}"
                            f":{case_id}:{metric_name}"
                        )
                    else:
                        fail_reasons.append(
                            f"{REASON_PREFIX_SAFETY_CASE_FAILED}"
                            f":{case_id}:{metric_name}"
                        )

    @staticmethod
    def _evaluate_scale(
        *,
        policy: GatePolicy,
        candidate: GateExperimentData,
        total_cases: set[str],
        inconclusive_reasons: list[str],
    ) -> None:
        if len(total_cases) < policy.min_cases:
            inconclusive_reasons.append(
                f"{REASON_PREFIX_INSUFFICIENT_DATA}:total_cases"
                f"（{len(total_cases)} < {policy.min_cases}）"
            )
        for slice_name in policy.key_slices:
            count = sum(
                1
                for case_id in total_cases
                if candidate.case_slices.get(case_id, "") == slice_name
            )
            if count < policy.min_key_slice_cases:
                inconclusive_reasons.append(
                    f"{REASON_PREFIX_INSUFFICIENT_DATA}:key_slice:{slice_name}"
                    f"（{count} < {policy.min_key_slice_cases}）"
                )
