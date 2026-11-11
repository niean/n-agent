# app/application/evaluation_calibration.py
"""Judge 校准与 advisory 语义（plan T8，spec 10）。

职责：
- 校准集 / 回归集 manifest 的分离加载与 fail-fast 校验（本模块定义的
  校准集 schema，校验纪律对齐 T12 DatasetManifest，IO 原语共用
  app/application/evaluation_io.py：canonical JSON 内容 hash + stdlib
  声明式解析，无网络无脚本执行）；
- 校准编排：经注入的 Judge 端口重放校准样本（无 IO 混排——统计计算
  全部委托 app/domain/evaluation/calibration_stats.py 纯函数）；
- 一致性（boolean agreement>=0.85 且 Cohen kappa>=0.6；numeric 归一化
  MAE<=0.15）与重复性（20 样本 x 3 次，boolean agreement>=0.90、numeric
  最大差均值<=0.10，归一化口径与 nMAE 同基——非单位量程定义下 raw 阈值
  会隐式加严/放宽，单位量程 (0,1) 下两者数值等价）阈值判定（spec 10）；
- 单类样本 kappa 不可计算 → 校准失败，不得以 agreement 替代；
- 一致性与重复性报告持久化（evaluation_calibrations 表，经注入端口）；
- 缓存键覆盖 definition hash：Judge 模型（judge_model）、rubric
  （material_hash）、截断策略与证据 schema（judge_params /
  evidence_policy）任一变化 → 新缓存键 → 校准失效重算；运行参数
  （timeout/budget）不触发重校准；
- advisory 语义与 CI 必需门禁接口：``calibration`` / ``self_judge``
  provenance 为 advisory，``is_gate_eligible`` 为 False——T14 gate
  policy 据此保证"未校准指标仅 advisory、必需门禁不得 pass"。

缺失数据纪律（spec 10 的保守解释，全链路一致）：
Judge 输出与金标中的非有限数值（NaN / ±inf）一律按"缺失数据"处理——
Judge 输出非有限 → None（按不一致 / 满量程误差保守计）；manifest 金标
非有限 → 加载期 fail-fast 拒绝（json.loads 默认接受 NaN 字面量的洞由
canonical hash allow_nan=False + 显式 isfinite 校验双封堵）。缺失数据
永不伪装成校准通过。

缓存重用边界（判定记录）：缓存键覆盖 definition 的声明性语义字段与
数据集 hash；同名 Judge 模型在服务端被静默更换/更新（部署漂移）不在
hash 覆盖范围内——缓存命中即复用既有报告，不重跑 Judge。该失效局限
是已知设计边界，重校准节奏属运维纪律（人工触发）。

数据要求边界（双人标注裁决是数据集编写纪律，代码无法核验人工过程）：
本模块在 manifest 层校验"每样本 >= 2 个不同标注人 + 已裁决声明 + 覆盖
正/负/注入/边界四类"，并对校准/回归集做 sample content hash 非重叠
校验（隔离不由 manifest 路径单独保证）；标注人是否真实独立标注由数据
集作者负责，超出代码可验证范围。

Provenance 语义（判定记录，store CHECK 同步扩展）：
- ``automatic``：deterministic 结果，或校准状态为 passed 的 llm_judge
  结果（且非 self_judge）；
- ``calibration``：llm_judge 结果未校准 / 校准 advisory（含 worker 未接
  校准状态查询的保守缺省——忘注入按 advisory 处理，fail-safe，对齐 T9
  m12 模式）；
- ``self_judge``：Judge 适配器输出的 ``provenance.self_judge`` 标记，
  优先于 calibration 状态（即使校准通过也不得作为唯一发布证据）。
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from app.application.evaluation_io import (
    compute_content_hash,
    read_json_object,
    require_sha256_hex,
)
from app.domain.evaluation.calibration_stats import (
    boolean_agreement,
    cohen_kappa,
    normalized_mae,
    repeatability_boolean_agreement,
    repeatability_max_diff_mean,
)
from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import (
    EXPLANATION_MAX_BYTES,
    EvaluatorDefinition,
    canonical_json_bytes,
)

logger = logging.getLogger(__name__)

# 校准集 manifest schema 版本
CALIBRATION_SCHEMA_VERSION = "1"

# spec 10 阈值
MIN_CALIBRATION_SAMPLES: int = 50
BOOLEAN_AGREEMENT_MIN: float = 0.85
COHEN_KAPPA_MIN: float = 0.6
NUMERIC_NMAE_MAX: float = 0.15
REPEATABILITY_SAMPLES: int = 20
REPEATABILITY_RUNS: int = 3
REPEATABILITY_BOOLEAN_AGREEMENT_MIN: float = 0.90
REPEATABILITY_NUMERIC_MAX_DIFF_MEAN_MAX: float = 0.10

# 阈值比较的浮点容差（对称）：恰在阈值上的统计量（如归一化 MAE == 0.15）
# 按"达到阈值"计；二进制浮点均值可能出现 1e-16 量级表示误差，不得据此
# 把边界合格值判为失败（也绝不放大容忍度——阈值语义不变）
_THRESHOLD_TOLERANCE: float = 1e-9

# EXPLANATION_MAX_BYTES 从 domain models 导入再导出（T8 历史导入路径
# 兼容；单一权威定义在 app/domain/evaluation/models.py）

# 覆盖类别（spec 10：正例、负例、注入与边界）
REQUIRED_COVERAGE_CATEGORIES: tuple[str, ...] = (
    "positive",
    "negative",
    "injection",
    "boundary",
)

_CALIBRATION_ROLES: tuple[str, ...] = ("calibration", "regression")

# advisory provenance 集合与 CI 必需门禁资格（T14 消费接口）
ADVISORY_PROVENANCES: frozenset[str] = frozenset({"calibration", "self_judge"})
GATE_ELIGIBLE_PROVENANCES: frozenset[str] = frozenset({"automatic", "human"})

# 校准缓存键覆盖的 definition 字段：Judge 行为语义（模型 / rubric /
# 截断策略 / 证据 schema / 输出契约）。timeout / budget 等运行参数不参与。
_CALIBRATION_DEFINITION_FIELDS: tuple[str, ...] = (
    "metric_name",
    "target_kind",
    "method",
    "required_fields",
    "evidence_policy",
    "output_type",
    "range_or_categories",
    "direction",
    "material_hash",
    "judge_model",
    "judge_params",
)

_MANIFEST_REQUIRED_FIELDS = (
    "schema_version",
    "set_id",
    "role",
    "manifest_hash",
    "dual_annotation",
    "samples",
)
_MANIFEST_HASH_FIELD = "manifest_hash"

__all__ = [
    "ADVISORY_PROVENANCES",
    "BOOLEAN_AGREEMENT_MIN",
    "CALIBRATION_SCHEMA_VERSION",
    "COHEN_KAPPA_MIN",
    "EXPLANATION_MAX_BYTES",
    "GATE_ELIGIBLE_PROVENANCES",
    "MIN_CALIBRATION_SAMPLES",
    "NUMERIC_NMAE_MAX",
    "REPEATABILITY_BOOLEAN_AGREEMENT_MIN",
    "REPEATABILITY_MAX_DIFF_MEAN_MAX",
    "REPEATABILITY_NUMERIC_MAX_DIFF_MEAN_MAX",
    "REPEATABILITY_RUNS",
    "REPEATABILITY_SAMPLES",
    "REQUIRED_COVERAGE_CATEGORIES",
    "CalibrationError",
    "CalibrationJudgePort",
    "CalibrationManifestError",
    "CalibrationReport",
    "CalibrationReportStore",
    "CalibrationSample",
    "CalibrationSet",
    "JudgeCalibrator",
    "calibration_cache_key",
    "calibration_definition_hash",
    "calibration_status_from_store",
    "compute_calibration_manifest_hash",
    "is_gate_eligible",
    "load_calibration_set",
    "provenance_is_advisory",
]

# 阈值别名（重复性 numeric 归一化阈值；与 spec 10 字面 raw 口径的判定
# 见 domain repeatability_max_diff_mean docstring）
REPEATABILITY_MAX_DIFF_MEAN_MAX = REPEATABILITY_NUMERIC_MAX_DIFF_MEAN_MAX


class CalibrationError(ValueError):
    """校准编排前置条件或执行失败（fail-fast，不产出部分报告）。"""


class CalibrationManifestError(CalibrationError):
    """校准集 manifest 加载或校验失败（fail-fast，立即终止）。"""


# ---------------------------------------------------------------------------
# Hash / advisory 接口
# ---------------------------------------------------------------------------


def compute_calibration_manifest_hash(content: Mapping[str, Any]) -> str:
    """校准集 manifest 内容 hash：canonical JSON（去掉 manifest_hash 字段）
    的 SHA-256。作者侧生成与加载侧校验必须使用同一函数。

    canonical_json_bytes 禁止非有限数值：含 NaN/Infinity 字面量的内容
    抛 EvaluationValidationError（json.loads 默认接受 NaN 字面量的洞
    在内容 hash 处即封堵）。"""
    return compute_content_hash(content, exclude_field=_MANIFEST_HASH_FIELD)


def calibration_definition_hash(definition: EvaluatorDefinition) -> str:
    """校准视角的 definition hash：仅覆盖 Judge 行为语义字段（spec 10）。

    Judge 模型、rubric（material_hash）、截断策略与证据 schema
    （judge_params / evidence_policy）变化均改变本 hash → 缓存键变化 →
    校准结果失效；timeout / budget 等运行参数不参与（不触发重校准）。"""
    content: dict[str, Any] = {}
    for name in _CALIBRATION_DEFINITION_FIELDS:
        value = getattr(definition, name)
        if isinstance(value, Mapping):
            # EvaluatorDefinition 构造期将 material_hash / judge_params
            # 冻结为 MappingProxyType：canonical JSON 需要普通 dict
            value = {k: list(v) if isinstance(v, tuple) else v
                     for k, v in dict(value).items()}
        elif isinstance(value, tuple):
            value = list(value)
        content[name] = value
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def calibration_cache_key(
    *,
    definition: EvaluatorDefinition,
    calibration_set_hash: str,
    regression_set_hash: str,
) -> str:
    """校准报告缓存键（definition hash + 两个数据集 hash 全部参与）。"""
    content = {
        "schema_version": CALIBRATION_SCHEMA_VERSION,
        "metric_name": definition.metric_name,
        "definition_id": definition.definition_id,
        "definition_hash": calibration_definition_hash(definition),
        "calibration_set_hash": calibration_set_hash,
        "regression_set_hash": regression_set_hash,
    }
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def provenance_is_advisory(provenance: str) -> bool:
    """provenance 是否 advisory（不能作为 CI 必需门禁的发布证据）。"""
    return provenance in ADVISORY_PROVENANCES


def is_gate_eligible(provenance: str) -> bool:
    """CI 必需门禁资格（T14 gate policy 消费）：仅 automatic / human
    结果可满足必需门禁；calibration / self_judge 一律 advisory，必需
    门禁不得据此自动 pass（spec 10）。"""
    return provenance in GATE_ELIGIBLE_PROVENANCES


async def calibration_status_from_store(
    store: Any, definition: EvaluatorDefinition
) -> str:
    """从校准报告账本查询 definition 的校准状态（worker 组合根适配）。

    返回 "passed" / "advisory"；无报告、definition hash 变化（缓存键
    失效）或账本记录损坏均为 "advisory"。"""
    return await store.calibration_status(
        definition.definition_id, calibration_definition_hash(definition)
    )


# ---------------------------------------------------------------------------
# 校准集 manifest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationSample:
    """校准/回归集单样本（已双人标注并裁决的金标）。"""

    sample_id: str
    content_hash: str
    category: str
    adjudicated_value: bool | float

    def __post_init__(self) -> None:
        if not isinstance(self.sample_id, str) or not self.sample_id:
            raise CalibrationManifestError(
                f"sample_id 必须是非空字符串: {self.sample_id!r}"
            )


@dataclass(frozen=True)
class CalibrationSet:
    """加载后的校准/回归集（样本顺序即 manifest 顺序）。"""

    set_id: str
    role: str
    manifest_hash: str
    samples: tuple[CalibrationSample, ...]


def load_calibration_set(path: str | Path, *, role: str) -> CalibrationSet:
    """从 manifest 文件加载校准/回归集（fail-fast，任一校验失败终止）。

    校验纪律：manifest_hash 内容校验（含非有限字面量拒绝——json.loads
    默认接受 NaN，canonical hash allow_nan=False 封堵）、role 匹配、
    dual_annotation 显式声明、每样本 >= 2 个不同标注人 + 已裁决 +
    类别合法 + sample_id 唯一 + 金标非有限拒绝。
    """
    if role not in _CALIBRATION_ROLES:
        raise CalibrationManifestError(
            f"非法 role: {role!r}，仅允许 {_CALIBRATION_ROLES}"
        )
    content = read_json_object(path, error=CalibrationManifestError)
    if not isinstance(content, dict):
        raise CalibrationManifestError("manifest 顶层必须是 JSON 对象")
    for name in _MANIFEST_REQUIRED_FIELDS:
        if name not in content:
            raise CalibrationManifestError(f"manifest 缺少必填字段: {name}")
    if content["schema_version"] != CALIBRATION_SCHEMA_VERSION:
        raise CalibrationManifestError(
            f"不支持的 schema_version: {content['schema_version']!r}，"
            f"仅支持 {CALIBRATION_SCHEMA_VERSION!r}"
        )
    if content["role"] != role:
        raise CalibrationManifestError(
            f"manifest role 与请求不一致: 声明 {content['role']!r}，"
            f"请求 {role!r}"
        )
    declared_hash = require_sha256_hex(
        content[_MANIFEST_HASH_FIELD],
        "manifest_hash",
        error=CalibrationManifestError,
    )
    try:
        actual_hash = compute_calibration_manifest_hash(content)
    except (TypeError, ValueError) as exc:
        # 含 NaN/Infinity 等非有限字面量（或不可序列化内容）：canonical
        # hash 拒绝（EvaluationValidationError），包装为本模块错误类型
        raise CalibrationManifestError(
            f"manifest 内容无法做 canonical hash（立即终止）: {exc}"
        ) from exc
    if actual_hash != declared_hash:
        raise CalibrationManifestError(
            f"manifest_hash 校验失败（立即终止）: 声明 {declared_hash!r} "
            f"与内容重算 {actual_hash!r} 不一致"
        )
    if content["dual_annotation"] is not True:
        raise CalibrationManifestError(
            "dual_annotation 必须显式声明为 true（spec 10：双人独立标注"
            "后裁决是样本准入前提）"
        )
    set_id = content["set_id"]
    if not isinstance(set_id, str) or not set_id:
        raise CalibrationManifestError(f"set_id 必须是非空字符串: {set_id!r}")
    samples_raw = content["samples"]
    if not isinstance(samples_raw, list) or not samples_raw:
        raise CalibrationManifestError("samples 必须是非空数组")
    samples = tuple(
        _validate_sample(sample, index=index)
        for index, sample in enumerate(samples_raw)
    )
    _require_unique_sample_ids(samples)
    return CalibrationSet(
        set_id=set_id,
        role=role,
        manifest_hash=declared_hash,
        samples=samples,
    )


def _validate_sample(sample: Any, *, index: int) -> CalibrationSample:
    where = f"samples[{index}]"
    if not isinstance(sample, dict):
        raise CalibrationManifestError(f"{where} 必须是 JSON 对象")
    for name in (
        "sample_id",
        "content_hash",
        "category",
        "adjudicated_value",
        "adjudicated",
        "annotations",
    ):
        if name not in sample:
            raise CalibrationManifestError(f"{where} 缺少必填字段: {name}")
    sample_id = sample["sample_id"]
    if not isinstance(sample_id, str) or not sample_id:
        raise CalibrationManifestError(
            f"{where}.sample_id 必须是非空字符串: {sample_id!r}"
        )
    content_hash = require_sha256_hex(
        sample["content_hash"],
        f"{where}（{sample_id!r}）content_hash",
        error=CalibrationManifestError,
    )
    category = sample["category"]
    if category not in REQUIRED_COVERAGE_CATEGORIES:
        raise CalibrationManifestError(
            f"{where}（{sample_id!r}）category 非法: {category!r}，"
            f"仅允许 {REQUIRED_COVERAGE_CATEGORIES}"
        )
    value = sample["adjudicated_value"]
    if isinstance(value, bool):
        pass
    elif isinstance(value, (int, float)) and math.isfinite(float(value)):
        # 数值金标必须有限：NaN/±inf 是缺失/损坏数据，加载期拒绝
        #（缺失数据永不伪装成校准通过）
        value = float(value)
    else:
        raise CalibrationManifestError(
            f"{where}（{sample_id!r}）adjudicated_value 必须是 bool 或有限"
            f"数值: {value!r}"
        )
    if sample["adjudicated"] is not True:
        raise CalibrationManifestError(
            f"{where}（{sample_id!r}）必须声明 adjudicated=true"
            f"（双人独立标注后裁决）"
        )
    annotations = sample["annotations"]
    if not isinstance(annotations, list) or len(annotations) < 2:
        raise CalibrationManifestError(
            f"{where}（{sample_id!r}）annotations 必须是至少 2 条"
            f"（双人独立标注）: {annotations!r}"
        )
    annotators = []
    for position, annotation in enumerate(annotations):
        if not isinstance(annotation, dict):
            raise CalibrationManifestError(
                f"{where}（{sample_id!r}）annotations[{position}] 必须是 JSON 对象"
            )
        annotator = annotation.get("annotator")
        if not isinstance(annotator, str) or not annotator:
            raise CalibrationManifestError(
                f"{where}（{sample_id!r}）annotations[{position}].annotator "
                f"必须是非空字符串: {annotator!r}"
            )
        annotators.append(annotator)
    if len(set(annotators)) < 2:
        raise CalibrationManifestError(
            f"{where}（{sample_id!r}）标注人必须为 2 个不同的人"
            f"（双人独立标注）: {annotators!r}"
        )
    return CalibrationSample(
        sample_id=sample_id,
        content_hash=content_hash,
        category=category,
        adjudicated_value=value,
    )


def _require_unique_sample_ids(samples: tuple[CalibrationSample, ...]) -> None:
    seen: set[str] = set()
    for sample in samples:
        if sample.sample_id in seen:
            raise CalibrationManifestError(
                f"sample_id 重复（立即终止）: {sample.sample_id!r}"
            )
        seen.add(sample.sample_id)


# ---------------------------------------------------------------------------
# 校准报告
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationReport:
    """校准报告（一致性与重复性，spec 10；持久化到校准报告账本）。"""

    cache_key: str
    metric_name: str
    definition_id: str
    definition_hash: str
    status: str  # "passed" | "advisory"
    failures: tuple[str, ...]
    calibration_set_id: str
    calibration_set_hash: str
    regression_set_id: str
    regression_set_hash: str
    sample_count: int
    consistency: Mapping[str, Any]
    repeatability: Mapping[str, Any]
    repeatability_samples: int
    repeatability_runs: int
    created_at: str
    VALID_STATUS = ("passed", "advisory")

    def __post_init__(self) -> None:
        if self.status not in self.VALID_STATUS:
            raise CalibrationError(f"非法校准状态: {self.status!r}")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "cache_key": self.cache_key,
            "metric_name": self.metric_name,
            "definition_id": self.definition_id,
            "definition_hash": self.definition_hash,
            "status": self.status,
            "failures": list(self.failures),
            "calibration_set_id": self.calibration_set_id,
            "calibration_set_hash": self.calibration_set_hash,
            "regression_set_id": self.regression_set_id,
            "regression_set_hash": self.regression_set_hash,
            "sample_count": self.sample_count,
            "consistency": dict(self.consistency),
            "repeatability": dict(self.repeatability),
            "repeatability_samples": self.repeatability_samples,
            "repeatability_runs": self.repeatability_runs,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "CalibrationReport":
        return cls(
            cache_key=mapping["cache_key"],
            metric_name=mapping["metric_name"],
            definition_id=mapping["definition_id"],
            definition_hash=mapping["definition_hash"],
            status=mapping["status"],
            failures=tuple(mapping.get("failures") or ()),
            calibration_set_id=mapping.get("calibration_set_id", ""),
            calibration_set_hash=mapping.get("calibration_set_hash", ""),
            regression_set_id=mapping.get("regression_set_id", ""),
            regression_set_hash=mapping.get("regression_set_hash", ""),
            sample_count=int(mapping.get("sample_count", 0)),
            consistency=dict(mapping.get("consistency") or {}),
            repeatability=dict(mapping.get("repeatability") or {}),
            repeatability_samples=int(mapping.get("repeatability_samples", 0)),
            repeatability_runs=int(mapping.get("repeatability_runs", 0)),
            created_at=mapping.get("created_at", ""),
        )


class CalibrationJudgePort(Protocol):
    """校准 Judge 执行端口（组合根适配真实 Judge 适配器 + 样本快照装配）。

    校准模块不自行装配证据快照、不做网络 IO：样本内容到 Judge 输入的
    映射由本端口的实现负责（测试注入 fake，无网络）。"""

    async def evaluate(
        self, definition: EvaluatorDefinition, sample: CalibrationSample
    ) -> Mapping[str, Any]: ...


class CalibrationReportStore(Protocol):
    """校准报告账本端口（SQLiteEvaluationStore 的最小形状）。

    读取契约（``calibrate`` 的缓存检测依赖）：``get_calibration_report``
    缓存未命中（含账本内 report_json 损坏不可解析）抛
    ``EvaluationNotFoundError``——调用方将其视为"未校准/已失效"并重算
    覆盖；``save_calibration_report`` 对同 cache_key 幂等覆盖（最新权威）。"""

    async def save_calibration_report(self, report: Mapping[str, Any]) -> str: ...

    async def get_calibration_report(self, cache_key: str) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------
# 校准编排
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


class JudgeCalibrator:
    """Judge 校准编排器（spec 10）。

    前置校验（全部 fail-fast，不产出部分报告）：校准/回归集分离读取
    （不同 manifest 路径）、content hash 非重叠、>= 50 样本、覆盖四类、
    双人标注裁决声明、金标形态与定义输出类型一致。统计计算委托 domain
    纯函数，本类无 IO 混排（仅经端口做 Judge 调用与报告持久化）。
    """

    def __init__(
        self,
        *,
        definition: EvaluatorDefinition,
        judge: CalibrationJudgePort,
        report_store: CalibrationReportStore,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._definition = definition
        self._judge = judge
        self._report_store = report_store
        self._clock = clock or _default_clock

    async def calibrate(
        self, calibration_path: str | Path, regression_path: str | Path
    ) -> CalibrationReport:
        definition = self._definition
        if definition.method != "llm_judge":
            raise CalibrationError(
                f"校准仅适用于 llm_judge 定义: {definition.method!r}"
            )
        if definition.output_type not in ("boolean", "number"):
            raise CalibrationError(
                f"校准仅支持 boolean/number 输出类型: "
                f"{definition.output_type!r}"
            )
        calibration_dir = Path(calibration_path).resolve()
        regression_dir = Path(regression_path).resolve()
        if calibration_dir == regression_dir:
            raise CalibrationError(
                "校准集与回归集必须是不同 manifest 路径（spec 10 分离读取）"
            )
        calibration_set = load_calibration_set(
            calibration_path, role="calibration"
        )
        regression_set = load_calibration_set(regression_path, role="regression")
        self._validate_set_separation(calibration_set, regression_set)
        if len(calibration_set.samples) < MIN_CALIBRATION_SAMPLES:
            raise CalibrationError(
                f"校准集至少需要 {MIN_CALIBRATION_SAMPLES} 个双人标注后"
                f"裁决的样本才允许计算（spec 10）: 实际 "
                f"{len(calibration_set.samples)}"
            )
        covered = {s.category for s in calibration_set.samples}
        missing = [
            c for c in REQUIRED_COVERAGE_CATEGORIES if c not in covered
        ]
        if missing:
            raise CalibrationError(
                f"校准集必须覆盖正/负/注入/边界四类（spec 10），缺少: {missing}"
            )
        self._validate_gold_shape(calibration_set.samples, definition)

        cache_key = calibration_cache_key(
            definition=definition,
            calibration_set_hash=calibration_set.manifest_hash,
            regression_set_hash=regression_set.manifest_hash,
        )
        cached = await self._load_cached_report(cache_key)
        if cached is not None:
            # 缓存命中（definition hash + 数据集 hash 均未变）：复用既有
            # 报告，不重跑 Judge（同名 Judge 模型的部署漂移不在缓存键
            # 覆盖范围，见模块 docstring"缓存重用边界"）
            return cached

        samples = calibration_set.samples
        if definition.output_type == "boolean":
            consistency, failures = await self._consistency_boolean(samples)
            value_range: tuple[float, float] | None = None
        else:
            value_range = self._numeric_value_range(definition)
            consistency, failures = await self._consistency_numeric(
                samples, value_range
            )

        repeatability, rep_failures = await self._repeatability(
            samples[:REPEATABILITY_SAMPLES], value_range
        )
        failures.extend(rep_failures)
        report = CalibrationReport(
            cache_key=cache_key,
            metric_name=definition.metric_name,
            definition_id=definition.definition_id,
            definition_hash=calibration_definition_hash(definition),
            status="passed" if not failures else "advisory",
            failures=tuple(failures),
            calibration_set_id=calibration_set.set_id,
            calibration_set_hash=calibration_set.manifest_hash,
            regression_set_id=regression_set.set_id,
            regression_set_hash=regression_set.manifest_hash,
            sample_count=len(samples),
            consistency=consistency,
            repeatability=repeatability,
            repeatability_samples=min(REPEATABILITY_SAMPLES, len(samples)),
            repeatability_runs=REPEATABILITY_RUNS,
            created_at=self._clock().isoformat(),
        )
        await self._report_store.save_calibration_report(report.to_mapping())
        return report

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _load_cached_report(
        self, cache_key: str
    ) -> CalibrationReport | None:
        """读取缓存报告；未命中（含账本内记录损坏不可解析）返回 None 并
        重算覆盖——损坏缓存绝不永久阻断重校准。意外账本错误 fail-fast。"""
        try:
            stored = await self._report_store.get_calibration_report(cache_key)
        except EvaluationNotFoundError:
            return None
        try:
            return CalibrationReport.from_mapping(stored)
        except (KeyError, TypeError, ValueError):
            logger.warning(
                "corrupt calibration report ignored (recomputing): "
                "cache_key=%s",
                cache_key,
            )
            return None

    @staticmethod
    def _validate_gold_shape(
        samples: tuple[CalibrationSample, ...],
        definition: EvaluatorDefinition,
    ) -> None:
        """金标形态与定义输出类型一致性（fail-fast）：boolean 校准的
        金标必须全为 bool（数值 0.7 经 bool() 会静默变 True——禁止）；
        numeric 校准的金标必须全为有限数值。"""
        if definition.output_type == "boolean":
            bad = [
                s.sample_id
                for s in samples
                if not isinstance(s.adjudicated_value, bool)
            ]
            if bad:
                raise CalibrationError(
                    f"boolean 校准的金标必须全为 bool，数值金标疑似"
                    f"形态错误（立即终止）: 示例 {bad[0]!r}"
                )
        else:
            bad = [
                s.sample_id
                for s in samples
                if isinstance(s.adjudicated_value, bool)
                or not isinstance(s.adjudicated_value, (int, float))
            ]
            if bad:
                raise CalibrationError(
                    f"numeric 校准的金标必须全为数值，bool 金标疑似"
                    f"形态错误（立即终止）: 示例 {bad[0]!r}"
                )

    async def _consistency_boolean(
        self, samples: tuple[CalibrationSample, ...]
    ) -> tuple[dict[str, Any], list[str]]:
        """一致性（boolean）：agreement>=0.85 且 Cohen kappa>=0.6；
        单类 kappa 不可计算 → 失败，不得以 agreement 替代（spec 10）。"""
        definition = self._definition
        gold = [bool(s.adjudicated_value) for s in samples]
        judged = [
            self._judge_bool_value(await self._judge.evaluate(definition, sample))
            for sample in samples
        ]
        agreement = boolean_agreement(judged, gold)
        kappa = cohen_kappa(judged, gold)
        failures: list[str] = []
        if agreement < BOOLEAN_AGREEMENT_MIN - _THRESHOLD_TOLERANCE:
            failures.append("agreement_below_threshold")
        if kappa is None:
            # 单类 kappa 不可计算：校准失败，不得以 agreement 替代
            failures.append("kappa_not_computable")
        elif kappa < COHEN_KAPPA_MIN - _THRESHOLD_TOLERANCE:
            failures.append("kappa_below_threshold")
        return {"agreement": agreement, "kappa": kappa}, failures

    async def _consistency_numeric(
        self,
        samples: tuple[CalibrationSample, ...],
        value_range: tuple[float, float],
    ) -> tuple[dict[str, Any], list[str]]:
        """一致性（numeric）：归一化 MAE<=0.15（Judge 缺失输出按满量程
        误差保守计）。"""
        definition = self._definition
        gold_values = [float(s.adjudicated_value) for s in samples]
        judged_values = [
            self._judge_numeric_value(
                await self._judge.evaluate(definition, sample)
            )
            for sample in samples
        ]
        nmae = normalized_mae(
            judged_values, gold_values, value_range=value_range
        )
        failures: list[str] = []
        if nmae > NUMERIC_NMAE_MAX + _THRESHOLD_TOLERANCE:
            failures.append("normalized_mae_above_threshold")
        return {"normalized_mae": nmae}, failures

    @staticmethod
    def _validate_set_separation(
        calibration_set: CalibrationSet, regression_set: CalibrationSet
    ) -> None:
        """隔离不由 manifest 路径单独保证：set 身份不同且样本 content
        hash 无重叠（同一内容不得同时充当校准与回归样本）。"""
        if calibration_set.manifest_hash == regression_set.manifest_hash:
            raise CalibrationError(
                "校准集与回归集 manifest 内容相同（立即终止）"
            )
        calibration_hashes = {s.content_hash for s in calibration_set.samples}
        regression_hashes = {s.content_hash for s in regression_set.samples}
        overlap = calibration_hashes & regression_hashes
        if overlap:
            raise CalibrationError(
                f"校准集与回归集样本 content hash 重叠（隔离失效，立即终止）: "
                f"{len(overlap)} 个（示例 {sorted(overlap)[0][:16]}...）"
            )

    @staticmethod
    def _numeric_value_range(
        definition: EvaluatorDefinition,
    ) -> tuple[float, float]:
        raw = definition.range_or_categories or ()
        if (
            len(raw) == 2
            and all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in raw
            )
            and float(raw[1]) > float(raw[0])
        ):
            return float(raw[0]), float(raw[1])
        raise CalibrationError(
            f"numeric 校准要求定义声明有限数值范围 (min, max): "
            f"{definition.range_or_categories!r}"
        )

    @staticmethod
    def _judge_bool_value(outcome: Mapping[str, Any]) -> bool | None:
        """Judge 输出 → bool；skipped / error / 形态非法 → None（按不
        一致保守计）。"""
        if not isinstance(outcome, Mapping):
            return None
        if outcome.get("status", "value") != "value":
            return None
        value = outcome.get("value")
        if not isinstance(value, bool):
            return None
        return value

    @staticmethod
    def _judge_numeric_value(outcome: Mapping[str, Any]) -> float | None:
        """Judge 输出 → 有限 float；skipped / error / 形态非法 / 非有限
        （NaN / ±inf——缺失或损坏数据）→ None，按满量程误差保守计，
        绝不进入统计（缺失数据永不伪装成校准通过）。"""
        if not isinstance(outcome, Mapping):
            return None
        if outcome.get("status", "value") != "value":
            return None
        value = outcome.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        numeric = float(value)
        if not math.isfinite(numeric):
            return None
        return numeric

    async def _repeatability(
        self,
        samples: tuple[CalibrationSample, ...],
        value_range: tuple[float, float] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """重复性：前 20 个校准样本各重复 Judge 3 次（spec 10；样本顺序
        即 manifest 顺序，选取确定性）。"""
        definition = self._definition
        runs: list[list[Any]] = []
        for sample in samples:
            sample_runs: list[Any] = []
            for _ in range(REPEATABILITY_RUNS):
                outcome = await self._judge.evaluate(definition, sample)
                if definition.output_type == "boolean":
                    sample_runs.append(self._judge_bool_value(outcome))
                else:
                    sample_runs.append(self._judge_numeric_value(outcome))
            runs.append(sample_runs)
        failures: list[str] = []
        if definition.output_type == "boolean":
            agreement = repeatability_boolean_agreement(runs)
            if agreement < (
                REPEATABILITY_BOOLEAN_AGREEMENT_MIN - _THRESHOLD_TOLERANCE
            ):
                failures.append("repeatability_agreement_below_threshold")
            repeatability = {
                "agreement": agreement,
                "samples": len(samples),
                "runs": REPEATABILITY_RUNS,
            }
        else:
            # 归一化口径（与一致性 nMAE 同基，spec 10 阈值 0.10 按归一化
            # 值比较；口径判定见 domain repeatability_max_diff_mean）
            max_diff_mean = repeatability_max_diff_mean(
                runs,
                value_range=value_range,  # type: ignore[arg-type]
            )
            if max_diff_mean > (
                REPEATABILITY_NUMERIC_MAX_DIFF_MEAN_MAX + _THRESHOLD_TOLERANCE
            ):
                failures.append("repeatability_max_diff_mean_above_threshold")
            repeatability = {
                "max_diff_mean": max_diff_mean,
                "samples": len(samples),
                "runs": REPEATABILITY_RUNS,
            }
        return repeatability, failures
