# app/domain/evaluation/models.py
"""评估契约模型：目标身份、证据快照、评估器定义与数据集/实验 VO。

本模块是统一评估闭环（spec 6.1-6.3、9.1）的领域契约基础。全部为 frozen
dataclass + 构造期不变量校验，零 IO、零 SDK 依赖，风格对齐 app/domain/cost.py。

身份边界：业务目标身份（EvaluationTarget）、评估作业身份、评估执行 Trace
身份、Langfuse Score 身份四组身份彼此独立；trace_id/span_id、run_id、
session_id、retrieval_id 是五个独立字段，任何字段不得冒充或合并为另一字段。
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from app.domain.evaluation.state import SkipReason

# 评估事实 schema 版本；字段语义变化时递增
SCHEMA_VERSION: int = 1

# 单份证据快照正文上限（spec 13：单 snapshot 2MiB，脱敏先于限长）
EVIDENCE_SNAPSHOT_MAX_BYTES: int = 2 * 1024 * 1024

# EvaluationTarget.kind 允许的目标类型（spec 6.1）
TARGET_KINDS: tuple[str, ...] = (
    "agent_run",
    "retrieval",
    "experiment_item",
    "session_feedback",
)

# EvaluatorDefinition.method 允许的评估方法（spec 6.2）
EVALUATOR_METHODS: tuple[str, ...] = ("deterministic", "llm_judge", "human")

# EvaluatorDefinition.output_type 允许的输出类型（spec 6.2：自由文本只作
# explanation 保存，不是输出类型）
OUTPUT_TYPES: tuple[str, ...] = ("number", "boolean", "categorical")

# 结果行 explanation 正文上限（spec 13 表：单 explanation 2000 UTF-8 字节；
# T8 单一权威——worker 提交与校准模块共用，不得各自复制常量）
EXPLANATION_MAX_BYTES: int = 2000

# 方向词汇锁定为 spec 6.2 唯一声明的 higher_is_better（spec 8 全部 10 个
# 正式指标均为 higher_is_better，不预留 lower_is_better）
EVALUATOR_DIRECTIONS: tuple[str, ...] = ("higher_is_better",)

# material_hash 允许的材料键（rubric / prompt / code 的 hash）
MATERIAL_KEYS: tuple[str, ...] = ("rubric", "prompt", "code")

# 实验运行模式（spec 9.1：两种显式模式）
EXPERIMENT_MODES: tuple[str, ...] = ("execute", "replay")

# 冻结快照中无法冻结的项的统一标记（spec 9.1：外部 Provider 无法冻结的
# 服务版本标为 external_unpinned，不承诺 bitwise reproducibility）
EXTERNAL_UNPINNED: str = "external_unpinned"

# 实验冻结快照的环境指纹字段（spec 9.1：由 Infrastructure 环境指纹提供，
# 缺失项标 external_unpinned；evaluator 版本集 / Judge 配置 / dataset hash /
# seed / replicate_count 由实验编排自行冻结，不在此列）
FROZEN_ENVIRONMENT_FIELDS: tuple[str, ...] = (
    "code_commit",
    "target_model_request_version",
    "target_model_response_version",
    "system_prompt_hash",
    "tool_inventory_hash",
    "skill_inventory_hash",
    "mcp_inventory_hash",
    "kb_corpus_config",
)

# SHA-256 十六进制摘要长度
_SHA256_HEX_LENGTH = 64


class EvaluationValidationError(ValueError):
    """评估契约不变量被违反。"""


class EvidenceSnapshotOversizedError(EvaluationValidationError):
    """证据快照正文超过 EVIDENCE_SNAPSHOT_MAX_BYTES 上限（spec 13）。

    类型化子类：实验编排/采集方据此路由 skip_reason=evidence_oversized，
    禁止按错误消息文本分类。"""


class UnsupportedCapabilityError(EvaluationValidationError):
    """能力目录不含的评估组合（spec 4.2：未注册组合不得宣称可执行）。

    类型化子类：携带机器可读 code，Interfaces 层按 code 映射 HTTP 契约，
    禁止按错误消息文本分类。"""

    code: str = "unsupported_capability"


def _require_non_empty_str(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise EvaluationValidationError(f"{label} 必须是非空字符串")


def _require_sha256_hex(value: Any, label: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_HEX_LENGTH
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise EvaluationValidationError(
            f"{label} 必须是 64 位小写十六进制 SHA-256 摘要: {value!r}"
        )


def _require_valid_kind(value: Any, label: str) -> None:
    if value not in TARGET_KINDS:
        raise EvaluationValidationError(
            f"非法 {label}: {value!r}，仅允许 {TARGET_KINDS}"
        )


def _require_hex_id(value: Any, length: int, label: str) -> None:
    """校验 OTel 真实身份 ID：定长小写十六进制（trace_id 32 位、span_id 16 位）。"""
    if (
        not isinstance(value, str)
        or len(value) != length
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise EvaluationValidationError(
            f"{label} 必须是 {length} 位小写十六进制: {value!r}"
        )


@dataclass(frozen=True)
class EvaluationTarget:
    """被评估目标的身份（spec 6.1）。

    必填 service / kind / subject_id / subject_revision；可选真实
    trace_id / span_id、run_id、session_id、retrieval_id。run_id、
    session_id、retrieval_id 是业务关联身份，不得冒充 Trace ID。
    源服务身份由适配器或认证凭据绑定，不在本对象内自证。
    """

    service: str
    kind: str
    subject_id: str
    subject_revision: str
    trace_id: str | None = None
    span_id: str | None = None
    run_id: str | None = None
    session_id: str | None = None
    retrieval_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.service, "service")
        _require_valid_kind(self.kind, "kind")
        _require_non_empty_str(self.subject_id, "subject_id")
        _require_non_empty_str(self.subject_revision, "subject_revision")
        if self.trace_id is not None:
            _require_hex_id(self.trace_id, 32, "trace_id")
        if self.span_id is not None:
            _require_hex_id(self.span_id, 16, "span_id")
        for label in ("run_id", "session_id", "retrieval_id"):
            value = getattr(self, label)
            if value is not None and (not isinstance(value, str) or not value):
                raise EvaluationValidationError(
                    f"{label} 为 None 或非空字符串，不得以空串占位"
                )


def require_non_negative_int(value: Any, label: str) -> int:
    """非负整数校验（bool 不是整数）；失败抛 EvaluationValidationError。

    producer 消息的路由校验（T17）与 record_producer_state 的应用层
    防御纵深（T17 质量修复轮）共用，避免两处复制漂移。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EvaluationValidationError(f"{label} 必须是非负整数")
    return value


# producer-state 消息的五个累计计数字段（spec 7.2；高水位绝对值，
# 接收端不累加）。路由白名单校验与 record_producer_state 防御纵深
# 共用同一键表。
PRODUCER_STATE_COUNT_KEYS: tuple[str, ...] = (
    "eligible_count",
    "selected_count",
    "captured_count",
    "capture_error_count",
    "queue_full_count",
)


def canonical_json_bytes(content: Mapping[str, Any]) -> bytes:
    """规范化 JSON 编码：固定键序、紧凑分隔符、UTF-8、拒绝非有限数。

    只接受 JSON 原生类型；SDK 对象、集合等无法序列化时抛
    EvaluationValidationError，不得静默降级。
    """
    try:
        text = json.dumps(
            content,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationValidationError(
            f"content 无法规范化为 JSON（禁止 SDK 对象与非有限数）: {exc}"
        ) from exc
    return text.encode("utf-8")


@dataclass(frozen=True)
class EvidenceSnapshot:
    """评估证据快照（spec 6.3）。

    content 是脱敏后的受控快照正文（脱敏后内容即实际评估输入），构造时
    复制并冻结为只读 Mapping，避免外部可变字典事后篡改导致 hash 与正文
    分叉；canonical_bytes 为规范化 JSON 编码，content_sha256 为其 SHA-256。
    redaction_version 必填：脱敏版本是快照可复现性的一部分。正文超过
    EVIDENCE_SNAPSHOT_MAX_BYTES（2MiB）拒绝构造。

    注意：JSON 规范化不折叠数值表示，1 与 1.0、0.0 与 -0.0 会产生不同
    hash；跨服务重算/比对 hash 前必须先钉死同一套规范化算法（如
    RFC 8785 JCS 风格），本实现的 canonical_json_bytes 即该契约的锚点。
    """

    content: Mapping[str, Any]
    redaction_version: str
    canonical_bytes: bytes = field(init=False)
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, Mapping):
            raise EvaluationValidationError("content 必须是 Mapping（JSON 对象）")
        _require_non_empty_str(self.redaction_version, "redaction_version")
        # 先用构造期副本算 hash，再整体冻结，防止外部字典事后变更
        content_copy = dict(self.content)
        data = canonical_json_bytes(content_copy)
        if len(data) > EVIDENCE_SNAPSHOT_MAX_BYTES:
            raise EvidenceSnapshotOversizedError(
                f"证据快照超限: {len(data)} 字节 > {EVIDENCE_SNAPSHOT_MAX_BYTES} 字节上限"
            )
        object.__setattr__(self, "content", MappingProxyType(content_copy))
        object.__setattr__(self, "canonical_bytes", data)
        object.__setattr__(self, "content_sha256", hashlib.sha256(data).hexdigest())


@dataclass(frozen=True)
class EvaluatorDefinition:
    """不可变评估器定义（spec 6.2）。

    定义变化创建新版本（definition_id + version 唯一），实验开始时锁定
    版本。material_hash 记录 rubric / prompt / code 材料的 SHA-256；
    llm_judge 方法必须声明 judge_model。预算为该定义的 USD 上限
    （Decimal），None 表示未设上限。
    """

    definition_id: str
    version: str
    metric_name: str
    target_kind: str
    method: str
    required_fields: tuple[str, ...]
    evidence_policy: str
    output_type: str
    range_or_categories: tuple[Any, ...] | None
    direction: str
    material_hash: Mapping[str, str]
    judge_model: str | None
    judge_params: Mapping[str, Any]
    timeout_seconds: int
    budget: Decimal | None
    ci_admissible: bool

    def __post_init__(self) -> None:
        _require_non_empty_str(self.definition_id, "definition_id")
        _require_non_empty_str(self.version, "version")
        _require_non_empty_str(self.metric_name, "metric_name")
        _require_valid_kind(self.target_kind, "target_kind")
        if self.method not in EVALUATOR_METHODS:
            raise EvaluationValidationError(
                f"非法 method: {self.method!r}，仅允许 {EVALUATOR_METHODS}"
            )
        required_fields = tuple(self.required_fields)
        for name in required_fields:
            _require_non_empty_str(name, "required_fields 条目")
        object.__setattr__(self, "required_fields", required_fields)
        _require_non_empty_str(self.evidence_policy, "evidence_policy")
        if self.output_type not in OUTPUT_TYPES:
            raise EvaluationValidationError(
                f"非法 output_type: {self.output_type!r}，仅允许 {OUTPUT_TYPES}"
            )
        self._validate_range_or_categories()
        if self.direction not in EVALUATOR_DIRECTIONS:
            raise EvaluationValidationError(
                f"非法 direction: {self.direction!r}，仅允许 {EVALUATOR_DIRECTIONS}"
            )
        self._validate_material_hash()
        if self.judge_model is not None:
            _require_non_empty_str(self.judge_model, "judge_model")
        if self.method == "llm_judge" and not self.judge_model:
            raise EvaluationValidationError("llm_judge 方法必须声明 judge_model")
        object.__setattr__(self, "judge_params", MappingProxyType(dict(self.judge_params)))
        if not isinstance(self.timeout_seconds, int) or isinstance(
            self.timeout_seconds, bool
        ) or self.timeout_seconds <= 0:
            raise EvaluationValidationError("timeout_seconds 必须是正整数")
        if self.budget is not None:
            if not isinstance(self.budget, Decimal) or not self.budget.is_finite():
                raise EvaluationValidationError("budget 必须是有限 Decimal 或 None")
            if self.budget < 0:
                raise EvaluationValidationError("budget 不得为负")
        if not isinstance(self.ci_admissible, bool):
            raise EvaluationValidationError("ci_admissible 必须是 bool")

    def _validate_range_or_categories(self) -> None:
        value = self.range_or_categories
        if self.output_type == "number":
            # number：None 表示不设显式范围；否则必须是 (low, high) 有限
            # 数值二元组，bool 不得充当端点
            if value is None:
                return
            if not isinstance(value, tuple) or len(value) != 2:
                raise EvaluationValidationError(
                    "output_type=number 的 range_or_categories 必须是 (low, high) 二元组或 None"
                )
            low, high = value
            for endpoint, label in ((low, "low"), (high, "high")):
                if isinstance(endpoint, bool) or not isinstance(
                    endpoint, (int, float)
                ) or not math.isfinite(endpoint):
                    raise EvaluationValidationError(
                        f"范围端点 {label} 必须是有限数值（bool 拒绝）: {endpoint!r}"
                    )
            if low > high:
                raise EvaluationValidationError(
                    f"范围下界 {low} 不得大于上界 {high}"
                )
        elif self.output_type == "boolean":
            # boolean：合法值固定为 True/False，不接受额外范围或类别
            if value not in (None, ()):
                raise EvaluationValidationError(
                    "output_type=boolean 的 range_or_categories 必须为空"
                )
        else:
            # categorical：必须声明非空、无重复的受限类别集合
            if not isinstance(value, tuple) or not value:
                raise EvaluationValidationError(
                    "output_type=categorical 必须声明非空类别集合"
                )
            for category in value:
                _require_non_empty_str(category, "类别")
            if len(set(value)) != len(value):
                raise EvaluationValidationError("类别集合不得有重复项")

    def _validate_material_hash(self) -> None:
        material_hash = dict(self.material_hash)
        for key, digest in material_hash.items():
            if key not in MATERIAL_KEYS:
                raise EvaluationValidationError(
                    f"非法 material_hash 键: {key!r}，仅允许 {MATERIAL_KEYS}"
                )
            _require_sha256_hex(digest, f"material_hash[{key!r}]")
        object.__setattr__(self, "material_hash", MappingProxyType(material_hash))


def validate_evaluation_value(
    definition: EvaluatorDefinition, value: Any
) -> None:
    """按定义的输出类型校验评估值（spec 6.2）。

    - number：拒绝 bool、非有限数、非数值类型；声明范围时拒绝越界值；
    - boolean：只接受 True / False；
    - categorical：只接受已声明类别中的字符串。
    非法值抛 EvaluationValidationError；缺失值（skipped/error）不走本
    函数，由作业状态与 reason 表达。
    """
    if not isinstance(definition, EvaluatorDefinition):
        raise EvaluationValidationError("definition 必须是 EvaluatorDefinition")
    if definition.output_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvaluationValidationError(
                f"number 输出拒绝 bool 与非数值: {value!r}"
            )
        if not math.isfinite(value):
            raise EvaluationValidationError(f"number 输出必须有限: {value!r}")
        bounds = definition.range_or_categories
        if bounds is not None:
            low, high = bounds
            if value < low or value > high:
                raise EvaluationValidationError(
                    f"值 {value!r} 超出声明范围 [{low}, {high}]"
                )
    elif definition.output_type == "boolean":
        if not isinstance(value, bool):
            raise EvaluationValidationError(
                f"boolean 输出只接受 True/False: {value!r}"
            )
    else:
        if not isinstance(value, str) or value not in (
            definition.range_or_categories or ()
        ):
            raise EvaluationValidationError(
                f"categorical 输出必须是已声明类别之一: {value!r}"
            )


# EvaluationValue.status 允许的状态：有值 / skipped（skipped 必须携带
# SkipReason，缺证据、无相关项等不是数值 0）
EVALUATION_VALUE_STATUSES: tuple[str, ...] = ("value", "skipped")


def _require_utc_iso(value: Any, label: str) -> None:
    """UTC ISO 时间字符串校验：必须可解析且偏移为零（UTC）。"""
    if not isinstance(value, str):
        raise EvaluationValidationError(f"{label} 必须是 ISO 时间字符串: {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvaluationValidationError(
            f"{label} 不是合法 ISO 时间: {value!r}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset() != timedelta(0):
        raise EvaluationValidationError(
            f"{label} 必须携带 UTC 时区偏移: {value!r}"
        )


@dataclass(frozen=True)
class DefinitionUsage:
    """评估器定义的线上使用状态 VO。

    online_enabled 是 Dashboard 维护的运行时线上开关；human 方法禁止
    线上启用（人工评估不进在线统计，spec 6.2）。revision 从 1 起单调
    递增，每次状态变更 +1；updated_at（UTC ISO）与 updated_by 记录
    最近一次变更。纯 Domain 值对象，无 IO。
    """

    definition_id: str
    definition_version: str
    online_enabled: bool
    revision: int
    updated_at: str
    updated_by: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.definition_id, "definition_id")
        _require_non_empty_str(self.definition_version, "definition_version")
        if not isinstance(self.online_enabled, bool):
            raise EvaluationValidationError(
                f"online_enabled 必须是 bool: {self.online_enabled!r}"
            )
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise EvaluationValidationError(
                f"revision 必须是 >= 1 的整数: {self.revision!r}"
            )
        _require_utc_iso(self.updated_at, "updated_at")
        _require_non_empty_str(self.updated_by, "updated_by")

    @classmethod
    def for_definition(
        cls,
        definition: "EvaluatorDefinition",
        *,
        online_enabled: bool,
        revision: int,
        updated_at: str,
        updated_by: str,
    ) -> "DefinitionUsage":
        """从评估器定义投影使用状态；内置 human 禁启用不变量。"""
        if not isinstance(definition, EvaluatorDefinition):
            raise EvaluationValidationError("definition 必须是 EvaluatorDefinition")
        if definition.method == "human" and online_enabled:
            raise EvaluationValidationError(
                "human 方法禁止线上启用（人工评估不进在线统计）"
            )
        return cls(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            online_enabled=online_enabled,
            revision=revision,
            updated_at=updated_at,
            updated_by=updated_by,
        )

    def to_mapping(self) -> dict[str, Any]:
        """投影为存储/传输映射（键序与字段声明一致）。"""
        return {
            "definition_id": self.definition_id,
            "definition_version": self.definition_version,
            "online_enabled": self.online_enabled,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "DefinitionUsage":
        """从存储/传输映射还原；缺字段或非法内容拒绝构造。"""
        if not isinstance(mapping, Mapping):
            raise EvaluationValidationError(
                f"usage mapping 必须是 Mapping: {mapping!r}"
            )
        try:
            return cls(
                definition_id=mapping["definition_id"],
                definition_version=mapping["definition_version"],
                online_enabled=mapping["online_enabled"],
                revision=mapping["revision"],
                updated_at=mapping["updated_at"],
                updated_by=mapping["updated_by"],
            )
        except KeyError as exc:
            raise EvaluationValidationError(
                f"usage mapping 缺少字段: {exc}"
            ) from exc


@dataclass(frozen=True)
class EvaluationValue:
    """单指标统一输出值（spec 8 / 11.1 typed_value）。

    有值状态携带通过 validate_evaluation_value 校验的 typed_value；
    skipped 状态 typed_value 必为 None 且必须携带 SkipReason，不伪造
    0 分污染质量分布（spec 11.1：skipped / error 不写 value=0）。
    explanation 为解释占位，投影前必须另行脱敏。
    """

    typed_value: bool | int | float | str | None
    status: str
    skip_reason: SkipReason | None = None
    explanation: str = ""

    def __post_init__(self) -> None:
        if self.status not in EVALUATION_VALUE_STATUSES:
            raise EvaluationValidationError(
                f"非法 status: {self.status!r}，仅允许 {EVALUATION_VALUE_STATUSES}"
            )
        if self.status == "value":
            # 构造期无 definition 可依：接受 bool / int / 有限 float / str
            # 四类标量，拒绝 None、NaN/inf 及 Mapping/list 等一切按引用
            # 传递的可变载荷（typed_value 必须是可安全冻结的标量快照）；
            # 语义级校验（范围、类别）由 validate_evaluation_value 按定义执行
            if not isinstance(self.typed_value, (bool, int, float, str)):
                raise EvaluationValidationError(
                    f"typed_value 必须是 bool/int/有限 float/str 标量: {self.typed_value!r}"
                )
            if isinstance(self.typed_value, float) and not math.isfinite(
                self.typed_value
            ):
                raise EvaluationValidationError(
                    f"typed_value float 必须有限: {self.typed_value!r}"
                )
            if self.skip_reason is not None:
                raise EvaluationValidationError(
                    "value 状态不得携带 skip_reason"
                )
        else:
            if self.typed_value is not None:
                raise EvaluationValidationError(
                    "skipped 状态不得携带 typed_value（缺数据不是 0）"
                )
            if not isinstance(self.skip_reason, SkipReason):
                raise EvaluationValidationError(
                    f"skipped 状态必须携带 SkipReason: {self.skip_reason!r}"
                )
        if not isinstance(self.explanation, str):
            raise EvaluationValidationError("explanation 必须是字符串")


@dataclass(frozen=True)
class DatasetCase:
    """数据集单条 case（spec 9.1）。

    expected_output 与 qrels 是两种真值形态（问答真值 / 检索分级标注）；
    rubric_ref 供 Judge 类指标引用评审标准；fixture 指向仓库内固定附件，
    内容完整性由 manifest 的 dataset_hash 整体冻结。corpus_revision 冻结
    检索基准语料版本。

    no_answer 与 metric_applicability 承载 spec 8 的 skipped(no_answer)
    语义：无相关项 query 预声明 no_answer 及对应 no_answer 指标适用性，
    实验运行（T13）据此确定指标分母与 skipped 归因。
    fixture_sha256 / rubric_sha256 是加载期校验字段，不进入本 VO：附件
    完整性由 dataset_hash 与加载校验保证，校验通过后无需随行。
    """

    case_id: str
    input: Mapping[str, Any]
    target_kind: str
    slice: str = ""
    expected_output: Any = None
    qrels: Mapping[str, int] | None = None
    rubric_ref: str | None = None
    fixture: str | None = None
    corpus_revision: str | None = None
    allowed_side_effects: tuple[str, ...] = ()
    cleanup_rule: str = ""
    no_answer: bool = False
    metric_applicability: Mapping[str, bool] | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.case_id, "case_id")
        if not isinstance(self.input, Mapping):
            raise EvaluationValidationError("input 必须是 Mapping")
        _require_valid_kind(self.target_kind, "target_kind")
        if not isinstance(self.slice, str):
            raise EvaluationValidationError("slice 必须是字符串")
        if self.rubric_ref is not None:
            _require_non_empty_str(self.rubric_ref, "rubric_ref")
        if self.fixture is not None:
            _require_non_empty_str(self.fixture, "fixture")
        if self.corpus_revision is not None:
            _require_non_empty_str(self.corpus_revision, "corpus_revision")
        side_effects = tuple(self.allowed_side_effects)
        for item in side_effects:
            _require_non_empty_str(item, "allowed_side_effects 条目")
        object.__setattr__(self, "allowed_side_effects", side_effects)
        if isinstance(self.expected_output, Mapping):
            # 真值若是映射（如结构化答案），同样冻结防止事后篡改
            object.__setattr__(
                self,
                "expected_output",
                MappingProxyType(dict(self.expected_output)),
            )
        if self.qrels is not None:
            if not isinstance(self.qrels, Mapping):
                raise EvaluationValidationError("qrels 必须是 Mapping")
            object.__setattr__(self, "qrels", MappingProxyType(dict(self.qrels)))
        if not isinstance(self.no_answer, bool):
            raise EvaluationValidationError(
                f"no_answer 必须是 bool: {self.no_answer!r}"
            )
        if self.metric_applicability is not None:
            if not isinstance(self.metric_applicability, Mapping):
                raise EvaluationValidationError(
                    "metric_applicability 必须是 Mapping（指标名 -> bool）"
                )
            frozen: dict[str, bool] = {}
            for metric_name, applicable in dict(self.metric_applicability).items():
                _require_non_empty_str(metric_name, "metric_applicability 键")
                if not isinstance(applicable, bool):
                    raise EvaluationValidationError(
                        f"metric_applicability[{metric_name!r}] 必须是 bool: "
                        f"{applicable!r}"
                    )
                frozen[metric_name] = applicable
            object.__setattr__(
                self, "metric_applicability", MappingProxyType(frozen)
            )
        object.__setattr__(self, "input", MappingProxyType(dict(self.input)))


@dataclass(frozen=True)
class DatasetManifest:
    """冻结数据集 manifest VO（spec 9.1）。

    仓库受版本管理的 JSON/JSONL 文件的领域投影；dataset_hash 冻结全部
    内容，hash 校验失败立即终止。生产样本提升为数据集必须显式审核、
    脱敏并生成新 manifest。
    """

    schema_version: str
    dataset_id: str
    dataset_version: str
    dataset_hash: str
    target_kind: str
    cases: tuple[DatasetCase, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.schema_version, "schema_version")
        _require_non_empty_str(self.dataset_id, "dataset_id")
        _require_non_empty_str(self.dataset_version, "dataset_version")
        _require_sha256_hex(self.dataset_hash, "dataset_hash")
        _require_valid_kind(self.target_kind, "target_kind")
        cases = tuple(self.cases)
        if not cases:
            raise EvaluationValidationError("manifest 必须至少包含一条 case")
        for case in cases:
            if not isinstance(case, DatasetCase):
                raise EvaluationValidationError("cases 条目必须是 DatasetCase")
        object.__setattr__(self, "cases", cases)


def metric_applicable(case: DatasetCase, metric_name: str) -> bool:
    """manifest 适用性规则（spec 9.2 分母纪律的唯一权威定义）。

    ``metric_applicability`` 覆盖声明优先；未显式声明时 no_answer case
    不计入任何指标分母。T13 summarize_quality 与 T14 门禁共用本函数，
    禁止在消费方复制本地实现。"""
    applicability = case.metric_applicability
    if applicability is not None and metric_name in applicability:
        return bool(applicability[metric_name])
    return not case.no_answer


@dataclass(frozen=True)
class Experiment:
    """评估实验 VO（spec 6.3 evaluation_experiments / 9.1）。

    实验开始时冻结数据集版本与 hash、目标配置 hash、evaluator 版本集
    hash；mode 只允许 execute（重新调用冻结配置的目标）或 replay（只对
    现有 EvidenceSnapshot 重新评分）。baseline_id 用于配对非劣比较。
    """

    experiment_id: str
    dataset_id: str
    dataset_version: str
    dataset_hash: str
    target_config_hash: str
    evaluator_set_hash: str
    mode: str
    status: str
    baseline_id: str | None = None
    manifest_json: str = ""
    gate_report_ref: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty_str(self.experiment_id, "experiment_id")
        _require_non_empty_str(self.dataset_id, "dataset_id")
        _require_non_empty_str(self.dataset_version, "dataset_version")
        _require_sha256_hex(self.dataset_hash, "dataset_hash")
        _require_sha256_hex(self.target_config_hash, "target_config_hash")
        _require_sha256_hex(self.evaluator_set_hash, "evaluator_set_hash")
        if self.mode not in EXPERIMENT_MODES:
            raise EvaluationValidationError(
                f"非法 mode: {self.mode!r}，仅允许 {EXPERIMENT_MODES}"
            )
        _require_non_empty_str(self.status, "status")
        if self.baseline_id is not None:
            _require_non_empty_str(self.baseline_id, "baseline_id")
        if self.gate_report_ref is not None:
            _require_non_empty_str(self.gate_report_ref, "gate_report_ref")
