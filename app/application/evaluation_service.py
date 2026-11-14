# app/application/evaluation_service.py
"""统一评估闭环用例入口（plan T5，spec 5.1 / 6 / 7 / 15）。

EvaluationService 是评估子域唯一正式用例入口：submit_sample、
submit_feedback、start_experiment、retry_delivery、cancel、查询。依赖全部
构造注入（T3 SQLiteEvaluationStore 适配 Domain 存储契约），不 import
app.infrastructure（DDD 依赖方向，knowledge/03-conventions），不 import
HTTP/FastAPI（协议适配属 Interfaces，T17/T21 接线）。

身份与信任边界（spec 6.1/14，第三评审补充）：
- ``submit_sample`` 的 service 身份必须来自服务端可信来源（认证 producer
  凭据绑定或服务端适配器），禁止客户端自报 service/source；本服务按传入值
  持久化，绑定校验由 Interfaces 层完成（T17）。
- ``submit_feedback`` 的 actor 来自注入的可信上下文参数（服务端会话身份），
  不从任何请求载荷读取。
- retrieval 身份只能来自认证 producer/adapter 的 ``retrieval_id``；同一
  Trace 上的多个 retrieval 各自独立成 sample/job，绝不按 trace_id 合并。
- KB metric job 的幂等（去重）键固定为
  ``service + retrieval_id + definition_id + definition_version + mode +
  experiment_id(线上为空) + replicate``（spec 7.2）；上下文捕获与 producer
  重复到达只生成一个对应 job。非 retrieval 目标以 producer_event_id 充当
  事件坐标（同一扩展格式的唯一事件粒度键）。

错误层次（文档化决策）：存储契约错误（EvaluationConflictError 409 语义、
EvaluationNotFoundError 404 语义等）权威定义在
app/domain/evaluation/errors.py，Infrastructure 适配 re-export，本服务原样
传播；用例语义错误（disabled / capacity / feedback CAS）定义在本模块。
该布局对齐 app.domain.artifact 的既有房规（Domain 拥有错误词汇，
Application 定义用例错误），避免同名错误双头维护。

start_experiment（第三评审补充）：本阶段只冻结并持久化实验请求（dataset
version/hash、evaluator set hash、target config hash）；case 业务执行与
sample 生成属 T13，取得 sample 后才按 case×replicate×definition 展开 jobs，
因此本用例禁止创建任何指向不存在 sample 的 job。
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationNotFoundError,
    EvaluationStaleSequenceError,
)
from app.domain.evaluation.models import (
    DefinitionUsage,
    EvaluatorDefinition,
    EvaluationTarget,
    EvaluationValidationError,
    Experiment,
    PRODUCER_STATE_COUNT_KEYS,
    canonical_json_bytes,
    require_non_negative_int,
    validate_evaluation_value,
)

# ---------------------------------------------------------------------------
# 容量上限默认值（spec 13：待执行作业 / 证据总容量 10000 / 256MiB；
# 组合根 T20 注入真实配置）
# ---------------------------------------------------------------------------

DEFAULT_MAX_PENDING_JOBS: int = 10000
DEFAULT_MAX_EVIDENCE_BYTES: int = 256 * 1024 * 1024

# 结果分页单页上限（请求超过按此封顶；非正值拒绝）。评估器管理列表分页
# 共用同一封顶常量（evaluation_admin），禁止两份手工拷贝
RESULTS_PAGE_LIMIT_CAP: int = 200

# producer 心跳过期判定阈值（spec 7.2：心跳过期 180 秒则覆盖率显示
# unknown）。observed_at 与服务端当前时刻的差超过该值视为过期。
PRODUCER_HEARTBEAT_STALE_SECONDS: float = 180.0


# ---------------------------------------------------------------------------
# 用例语义错误（存储契约错误见 app/domain/evaluation/errors.py）
# ---------------------------------------------------------------------------


class EvaluationServiceError(Exception):
    """评估用例层错误基类。"""


class EvaluationDisabledError(EvaluationServiceError):
    """评估闭环未启用时 submit_* 用例短路（spec：禁用时不产生评估负载）。"""


class EvaluationCapacityError(EvaluationServiceError):
    """待执行作业或证据容量达到上限，拒绝新评估捕获（映射 503）。"""


class EvaluationRevisionConflictError(EvaluationServiceError):
    """人评 revision CAS 失败：expected_revision 与当前线程 revision 不一致，
    或并发修订竞态落败（映射 409）。"""


# ---------------------------------------------------------------------------
# 注入依赖与输入/输出契约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationCapacityUsage:
    """容量探针快照：pending 作业数与证据正文总字节。"""

    pending_jobs: int
    evidence_bytes: int


CapacityUsageProvider = Callable[[], Awaitable[EvaluationCapacityUsage]]


@dataclass(frozen=True)
class DefinitionKey:
    """定义引用（definition_id + version；定义本体经 store 解析）。"""

    definition_id: str
    definition_version: str


class EvaluationLedgerStore(Protocol):
    """评估账本存储契约（T3 SQLiteEvaluationStore 适配；含 T5 最小扩展）。

    Application 只依赖本 Protocol 形状，不依赖 Infrastructure 实现。
    """

    async def save_sample(self, sample: Mapping[str, Any]) -> str: ...

    async def save_job(self, job: Mapping[str, Any]) -> str: ...

    async def save_definition(self, definition: Mapping[str, Any]) -> str: ...

    async def get_definition(
        self, definition_id: str, version: str
    ) -> Mapping[str, Any]: ...

    async def list_definitions(
        self, target_kind: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    async def list_definitions_with_usage(
        self, *, target_kind: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    async def get_definition_usage(
        self, definition_id: str, definition_version: str
    ) -> Mapping[str, Any] | None: ...

    async def upsert_definition_usage(
        self,
        definition_id: str,
        definition_version: str,
        *,
        online_enabled: bool,
        expected_revision: int,
        updated_by: str,
    ) -> Mapping[str, Any]: ...

    async def save_definition_with_usage(
        self, definition: Mapping[str, Any], usage: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    async def backfill_definition_usage(
        self, *, updated_by: str
    ) -> int:
        """非幂等显式回填契约（保留方法）。生产启动路径使用
        backfill_definition_usage_once（migrations 账本一次性标记）；
        本方法保留用于非幂等显式回填/单测契约，实现见
        SQLiteEvaluationStore.backfill_definition_usage。"""

    async def backfill_definition_usage_once(
        self, *, updated_by: str
    ) -> int: ...

    async def get_latest_calibration_report(
        self, definition_id: str, definition_hash: str
    ) -> Mapping[str, Any] | None: ...

    async def calibration_status(
        self, definition_id: str, definition_hash: str
    ) -> str: ...

    async def save_experiment(self, experiment: Mapping[str, Any]) -> str: ...

    async def save_feedback(self, feedback: Mapping[str, Any]) -> str: ...

    async def save_feedback_with_result(
        self,
        feedback: Mapping[str, Any],
        result: Mapping[str, Any],
        deliveries: Sequence[Mapping[str, Any]] = (),
    ) -> tuple[str, str]: ...

    async def get_feedback(self, feedback_id: str) -> Mapping[str, Any]: ...

    async def get_latest_feedback(
        self,
        actor_ref: str,
        target_service: str,
        target_kind: str,
        target_subject_id: str,
        definition_id: str,
        *,
        target_subject_revision: str,
        definition_version: str,
    ) -> Mapping[str, Any] | None: ...

    async def get_result_id_by_feedback_id(
        self, feedback_id: str
    ) -> str | None: ...

    async def save_result(self, result: Mapping[str, Any]) -> str: ...

    async def load_result(self, result_id: str) -> Mapping[str, Any]: ...

    async def get_job(self, job_id: str) -> Mapping[str, Any]: ...

    async def load_sample(self, sample_id: str) -> Mapping[str, Any]: ...

    async def list_attempts(self, job_id: str) -> list[Mapping[str, Any]]: ...

    async def list_deliveries(
        self, result_id: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    async def record_delivery_transition(
        self, delivery_id: str, target_status: str
    ) -> Mapping[str, Any]: ...

    async def query_results_page(
        self,
        *,
        target_service: str | None = None,
        target_kind: str | None = None,
        target_subject_id: str | None = None,
        target_subject_revision: str | None = None,
        definition_id: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        definition_version: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
        provenance: str | None = None,
        snapshot_at: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[Mapping[str, Any]], str | None]: ...

    async def query_results_page_snapshot(
        self,
        *,
        target_service: str | None = None,
        target_kind: str | None = None,
        target_subject_id: str | None = None,
        target_subject_revision: str | None = None,
        definition_id: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        definition_version: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
        provenance: str | None = None,
        snapshot_at: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[Mapping[str, Any]], str | None, str | None]: ...

    async def aggregate_metric_results(
        self,
        *,
        created_from: str | None = None,
        created_to: str | None = None,
        target_kind: str | None = None,
        definition_id: str | None = None,
        definition_version: str | None = None,
        provenance: str | None = None,
        status: str | None = None,
    ) -> list[Mapping[str, Any]]: ...

    async def list_annotation_queue(
        self,
        *,
        state: str,
        reason: str,
        threshold: float = 0.5,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Mapping[str, Any]], str | None]: ...

    async def revoke_queued_jobs_for_samples(
        self, sample_ids: Sequence[str]
    ) -> list[str]: ...


@dataclass(frozen=True)
class SubmitSampleRequest:
    """样本提交用例输入（协议载荷由 Interfaces 层校验后转入）。"""

    producer_event_id: str
    service: str  # 受信：认证 producer 绑定或服务端适配器，禁止客户端自报
    target: EvaluationTarget
    evidence_targets: tuple[Mapping[str, Any], ...] = ()
    snapshot_ref: str = ""
    snapshot_hash: str = ""
    schema_version: str = ""
    captured_at: str | None = None
    sample_probability: float = 1.0
    sampling_policy_version: str = ""
    # 抽样 cohort（random/failure；非捕获路径为空串）。落样本行 cohort
    # 列，窗口快照按其分组（#24：故障定向 cohort 不混入总体质量统计）
    cohort: str = ""
    # 需要展开 job 的定义；空元组表示仅登记样本（实验样本的 job 由 T13 展开）
    definitions: tuple[DefinitionKey, ...] = ()
    experiment_id: str | None = None
    # 实验样本的运行模式（execute/replay），进入去重键的 mode 分量；线上样本
    # experiment_id 为空，mode 分量由服务固定为 "online"
    mode: str = "execute"
    replicate: int = 0


@dataclass(frozen=True)
class SubmitSampleResult:
    sample_id: str
    job_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProducerStateResult:
    """producer-state 消息受理结果（T17 producer 接收端）。

    applied：本次是否实际更新高水位（False = 同 sequence 重发的幂等
    无副作用受理）；heartbeat_stale：observed_at 距服务端时刻超过
    PRODUCER_HEARTBEAT_STALE_SECONDS，覆盖率按 unknown 报告。
    """

    applied: bool
    heartbeat_stale: bool


@dataclass(frozen=True)
class SubmitFeedbackRequest:
    """人评提交用例输入；actor 由 submit_feedback 的可信上下文参数单独传入。"""

    target: EvaluationTarget
    definition_id: str
    definition_version: str
    value: bool | int | float | str
    comment_ref: str | None = None
    # 反馈请求幂等键：同 key 同 payload 复用、异 payload 冲突（第三评审补充）
    request_key: str | None = None
    # revision CAS：期望的当前线程 revision；None 表示不校验、直接追加
    expected_revision: int | None = None


@dataclass(frozen=True)
class SubmitFeedbackResult:
    feedback_id: str
    result_id: str
    revision: int
    superseded_feedback_id: str | None
    reused: bool  # True：幂等键命中复用（未新建 revision）


@dataclass(frozen=True)
class StartExperimentRequest:
    """实验启动用例输入；hash 冻结值以本服务解析/计算结果为准。"""

    dataset_id: str
    dataset_version: str
    dataset_hash: str  # 数据集 manifest 整体冻结 hash（SHA-256 hex）
    target_config: Mapping[str, Any]
    evaluator_set: tuple[DefinitionKey, ...]
    mode: str = "execute"
    baseline_id: str | None = None
    manifest_json: str = ""


@dataclass(frozen=True)
class ExperimentStarted:
    experiment_id: str
    dataset_hash: str
    target_config_hash: str
    evaluator_set_hash: str


@dataclass(frozen=True)
class ResultsPage:
    items: tuple[Mapping[str, Any], ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ResultDetail:
    result: Mapping[str, Any]
    job: Mapping[str, Any] | None
    feedback: Mapping[str, Any] | None
    sample: Mapping[str, Any] | None
    # 证据摘要（来自 sample 行；无 job 的 human result 为空映射）
    evidence_summary: Mapping[str, Any]
    attempts: tuple[Mapping[str, Any], ...]
    deliveries: tuple[Mapping[str, Any], ...]


# ---------------------------------------------------------------------------
# 定义序列化（EvaluatorDefinition <-> store 映射；T13 复用）
# ---------------------------------------------------------------------------


def definition_to_mapping(definition: EvaluatorDefinition) -> dict[str, Any]:
    """把领域定义 VO 投影为可 JSON 持久化的映射（budget 以字符串保真）。"""
    return {
        "definition_id": definition.definition_id,
        "version": definition.version,
        "metric_name": definition.metric_name,
        "target_kind": definition.target_kind,
        "method": definition.method,
        "required_fields": list(definition.required_fields),
        "evidence_policy": definition.evidence_policy,
        "output_type": definition.output_type,
        "range_or_categories": (
            list(definition.range_or_categories)
            if definition.range_or_categories is not None
            else None
        ),
        "direction": definition.direction,
        "material_hash": dict(definition.material_hash),
        "judge_model": definition.judge_model,
        "judge_params": dict(definition.judge_params),
        "timeout_seconds": definition.timeout_seconds,
        "budget": None if definition.budget is None else str(definition.budget),
        "ci_admissible": definition.ci_admissible,
    }


def definition_from_mapping(mapping: Mapping[str, Any]) -> EvaluatorDefinition:
    """从 store 保存的定义映射还原领域 VO（构造期不变量重新校验）。"""
    try:
        return EvaluatorDefinition(
            definition_id=mapping["definition_id"],
            version=mapping["version"],
            metric_name=mapping["metric_name"],
            target_kind=mapping["target_kind"],
            method=mapping["method"],
            required_fields=tuple(mapping["required_fields"]),
            evidence_policy=mapping["evidence_policy"],
            output_type=mapping["output_type"],
            range_or_categories=(
                tuple(mapping["range_or_categories"])
                if mapping["range_or_categories"] is not None
                else None
            ),
            direction=mapping["direction"],
            material_hash=dict(mapping["material_hash"]),
            judge_model=mapping["judge_model"],
            judge_params=dict(mapping["judge_params"]),
            timeout_seconds=mapping["timeout_seconds"],
            budget=(
                Decimal(mapping["budget"])
                if mapping["budget"] is not None
                else None
            ),
            ci_admissible=mapping["ci_admissible"],
        )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise EvaluationValidationError(
            f"definition 映射无法还原为领域 VO（须由 register_definition 写入）: {exc}"
        ) from exc


def target_to_mapping(target: EvaluationTarget) -> dict[str, Any]:
    """领域目标 VO -> 持久化映射（含可选 trace 身份字段）。

    被 evaluation_annotation（AnnotationService）复用，作为目标投影的
    单一权威实现，禁止两份手工拷贝导致算法分叉。"""
    mapping: dict[str, Any] = {
        "service": target.service,
        "kind": target.kind,
        "subject_id": target.subject_id,
        "subject_revision": target.subject_revision,
    }
    for name in ("trace_id", "span_id", "run_id", "session_id", "retrieval_id"):
        value = getattr(target, name)
        if value is not None:
            mapping[name] = value
    return mapping


def _sha256_hex(content: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


def _utc_now_iso() -> str:
    """当前 UTC 时刻的 ISO 字符串（usage.updated_at 等审计字段）。"""
    return datetime.now(timezone.utc).isoformat()


def job_idempotency_key(
    *,
    service: str,
    event_coordinate: str,
    definition_id: str,
    definition_version: str,
    mode: str,
    experiment_id: str | None,
    replicate: int,
) -> str:
    """评估 job 幂等（去重）键。

    KB metric 固定契约（spec 7.2）：service + retrieval_id +
    definition_id + definition_version + mode + experiment_id（线上固定为
    空）+ replicate。event_coordinate 对 retrieval 目标必须是 retrieval_id
    （上下文捕获与 producer 重复到达由此收敛为一个 job），非 retrieval 目标
    使用 producer_event_id。

    复合防碰撞：各分量按 ``{len}:{component}`` 长度前缀拼接，分量内出现
    分隔符不再产生跨分量歧义（如 event="a:b"/definition="c" 与
    event="a"/definition="b:c" 必然得到不同键）。键对存储层不透明，
    ``evaljob:v1:`` 前缀仅用于运维可读。
    """
    parts = (
        service,
        event_coordinate,
        definition_id,
        definition_version,
        mode,
        experiment_id or "",
        str(replicate),
    )
    return "evaljob:v1:" + "".join(f"{len(part)}:{part}" for part in parts)


def feedback_id_from_request_key(request_key: str) -> str:
    """反馈请求幂等键 -> 确定性 feedback_id（UNIQUE(feedback_id) PK 承载
    幂等语义，无需新增存储结构）。

    被 evaluation_annotation（AnnotationService submit_annotation 重放
    优先）复用，作为幂等键派生的单一权威实现。"""
    digest = hashlib.sha256(
        f"feedback-request:{request_key}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


def feedback_payload_hash(
    actor: str,
    target: EvaluationTarget,
    definition_id: str,
    definition_version: str,
    value: Any,
    comment_ref: str | None,
) -> str:
    """反馈 payload hash（完整目标身份 + 定义身份 + typed value + actor +
    comment_ref）。被 evaluation_annotation（AnnotationService 重放一致性
    判定）复用，作为 hash 算法的单一权威实现。"""
    content = {
        "actor": actor,
        "definition_id": definition_id,
        "definition_version": definition_version,
        "value": value,
        "comment_ref": comment_ref,
        "target": {
            "service": target.service,
            "kind": target.kind,
            "subject_id": target.subject_id,
            "subject_revision": target.subject_revision,
        },
    }
    return _sha256_hex(content)


def feedback_payload_hash_from_row(row: Mapping[str, Any]) -> str:
    """feedback 行 -> payload hash（复用 feedback_payload_hash 同源算法，
    供幂等键重放一致性判定；被 evaluation_annotation 复用）。"""
    target = row.get("target") or {}
    return feedback_payload_hash(
        row["actor_ref"],
        EvaluationTarget(
            service=target.get("service") or "",
            kind=target.get("kind") or "",
            subject_id=target.get("subject_id") or "",
            subject_revision=target.get("subject_revision") or "",
        ),
        row["definition_id"],
        row.get("definition_version") or "",
        row.get("value"),
        row.get("comment_ref"),
    )


def human_result_payload(
    definition_row: Mapping[str, Any],
    value: Any,
    supersedes_result_id: str | None,
    *,
    feedback_id: str | None = None,
) -> dict[str, Any]:
    """人评 result payload 的单一权威构造（provenance 恒 human，passed
    仅布尔值回填）。

    submit_feedback 正常路径（单事务）、_ensure_human_result 幂等补齐与
    evaluation_annotation（AnnotationService 提交 / request_key 重放补齐）
    共用本函数；``feedback_id`` 仅在独立 save_result 补齐路径需要（单事务
    路径的关联由 store save_feedback_with_result 维护，payload 不携带）。
    提交流程语义差异（服务端解析 vs 客户端自报、重放顺序）由调用方各自
    编排，本函数只收敛 payload/deliveries 形状。"""
    payload: dict[str, Any] = {
        "provenance": "human",
        "metric_name": definition_row["metric_name"],
        "metric_version": definition_row["version"],
        "status": "value",
        "typed_value": value,
        "passed": value if isinstance(value, bool) else None,
        "supersedes_result_id": supersedes_result_id,
    }
    if feedback_id is not None:
        payload["feedback_id"] = feedback_id
    return payload


def human_result_deliveries(
    sink_specs: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    definition_row: Mapping[str, Any],
    value: Any,
) -> list[dict[str, Any]]:
    """人评 result 的 sink 投递登记的单一权威构造（spec 145/262：每个
    result 都有投递；投影所需 target/definition 身份由投递 worker 领取时
    经 feedback 行解析，无真实身份 -> unlinked 由投影判定）。

    submit_feedback 与 evaluation_annotation（AnnotationService 提交）
    共用本函数，sink 清单统一经 EvaluationService.delivery_sink_specs
    取得（deepcopy 快照）。"""
    return [
        {
            "sink": spec["sink"],
            "projection_version": spec["projection_version"],
            "payload": {
                "job_id": None,
                "sample_id": None,
                "metric_name": definition_row["metric_name"],
                "metric_version": definition_row["version"],
                "status": "value",
                "typed_value": value,
                "evidence_hash": "",
            },
        }
        for spec in sink_specs
    ]


def _encode_result_cursor(created_at: str, result_id: str) -> str:
    raw = json.dumps(
        {"c": created_at, "r": result_id}, sort_keys=True
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_result_cursor(cursor: str | None) -> str | None:
    if cursor is None or cursor == "":
        return None
    try:
        raw = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        return f"{raw['c']}|{raw['r']}"
    except Exception as exc:
        raise EvaluationValidationError(
            f"非法 results 游标: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EvaluationService:
    """评估子域用例入口（唯一正式入口，spec 5.1）。"""

    def __init__(
        self,
        store: EvaluationLedgerStore,
        *,
        enabled: bool = False,
        capacity_usage_provider: CapacityUsageProvider | None = None,
        max_pending_jobs: int = DEFAULT_MAX_PENDING_JOBS,
        max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
        delivery_sink_specs: tuple[Mapping[str, Any], ...] = (),
    ) -> None:
        self._store = store
        self._enabled = enabled
        # 容量探针 None = 不做容量判定（T20 组合根接线真实探针：store
        # pending 作业计数 + 证据目录字节统计）
        self._capacity_usage_provider = capacity_usage_provider
        self._max_pending_jobs = max_pending_jobs
        self._max_evidence_bytes = max_evidence_bytes
        # 人评投递登记的 sink 清单（T20 组合根与投递 worker 共用同一
        # sink/projection_version 清单；空 = 不为人评建投递，与既有
        # 行为兼容）。unlinked 判定由投递 worker 投影时落，不在提交期
        # 伪造链路状态。
        self._delivery_sink_specs = tuple(
            dict(spec) for spec in delivery_sink_specs
        )

    @property
    def delivery_sink_specs(self) -> tuple[Mapping[str, Any], ...]:
        """人评投递 sink 清单（只读契约：返回深拷贝，调用方修改返回值
        不影响服务内部状态；Task 6 AnnotationService 与 submit_feedback
        共用同一 ``_delivery_sink_specs`` 机制，禁止两份手工拷贝）。"""
        return tuple(copy.deepcopy(spec) for spec in self._delivery_sink_specs)

    # ------------------------------------------------------------------
    # 定义注册
    # ------------------------------------------------------------------

    async def register_definition(
        self, definition: EvaluatorDefinition, *, updated_by: str = "system"
    ) -> Mapping[str, Any]:
        """注册（或幂等重放）不可变评估器定义；同版本异内容 409 冲突。

        T5：定义与初始 usage 同事务写入（新定义 enabled=False，spec 4.3：
        新增定义不自动用于线上）；幂等命中既有定义时不写 usage——用户
        停用状态不被重注册复活。返回定义行（含 definition 契约映射）。"""
        usage = DefinitionUsage.for_definition(
            definition,
            online_enabled=False,
            revision=1,
            updated_at=_utc_now_iso(),
            updated_by=updated_by,
        )
        return await self._store.save_definition_with_usage(
            definition_to_mapping(definition), usage.to_mapping()
        )

    @staticmethod
    def helpful_definition_factory(
        definition_id: str, version: str
    ) -> EvaluatorDefinition:
        """构造 human 方法定义 VO（feedback.helpful 及同形态定义复用）。"""
        return EvaluatorDefinition(
            definition_id=definition_id,
            version=version,
            metric_name=definition_id,
            target_kind="agent_run",
            method="human",
            required_fields=(),
            evidence_policy="actor_submission",
            output_type="boolean",
            range_or_categories=(),
            direction="higher_is_better",
            material_hash={},
            judge_model=None,
            judge_params={},
            timeout_seconds=60,
            budget=None,
            ci_admissible=False,
        )

    async def register_feedback_helpful_definition(self) -> None:
        """注册 ``feedback.helpful``（boolean 人评，目标是 AgentRun）。

        口径（spec 8/10）：未反馈不计 false——本服务只在 actor 真实提交时
        创建 human result，缺席不产生任何结果行，不伪造负反馈。
        """
        await self.register_definition(
            self.helpful_definition_factory("feedback.helpful", "v1")
        )

    # ------------------------------------------------------------------
    # 正式指标预设定义（spec 8 Score 目录）
    # ------------------------------------------------------------------

    _PRESET_DETERMINISTIC: tuple[dict[str, Any], ...] = (
        {
            "definition_id": "agent.task_success",
            "target_kind": "agent_run",
            "output_type": "boolean",
        },
        {
            "definition_id": "agent.tool_contract",
            "target_kind": "agent_run",
            "output_type": "boolean",
        },
        {
            "definition_id": "agent.scenario_assertions",
            "target_kind": "agent_run",
            "output_type": "boolean",
        },
        {
            "definition_id": "kb.recall_at_5",
            "target_kind": "retrieval",
            "output_type": "number",
        },
        {
            "definition_id": "kb.ndcg_at_5",
            "target_kind": "retrieval",
            "output_type": "number",
        },
        {
            "definition_id": "kb.mrr_at_5",
            "target_kind": "retrieval",
            "output_type": "number",
        },
    )

    _PRESET_JUDGE: tuple[dict[str, Any], ...] = (
        {
            "definition_id": "agent.answer_correctness",
            "target_kind": "agent_run",
            "required_fields": ("request", "answer", "reference"),
        },
        {
            "definition_id": "agent.answer_relevance",
            "target_kind": "agent_run",
            "required_fields": ("request", "answer"),
        },
        {
            "definition_id": "rag.faithfulness",
            "target_kind": "agent_run",
            "required_fields": ("answer", "contexts"),
        },
        {
            "definition_id": "kb.context_relevance",
            "target_kind": "retrieval",
            "required_fields": ("query", "chunks"),
        },
        {
            # plan T3：场景语义质量（逐轮 0/0.5/1，最终分取最低档）；
            # turns 证据槽由 judge_provider 按轮展开（request/answer/rubric）
            "definition_id": "agent.scenario_quality",
            "target_kind": "agent_run",
            "required_fields": ("turns", "scenario_evidence"),
        },
    )

    # 离线实验 variant 版本（24 项三方审阅 #1）：实验 runner 只接受
    # target_kind=experiment_item/retrieval 的评分定义（T13），而线上
    # 正式预设是 agent_run——同 metric 增设 experiment_item 镜像定义
    # （definition_id 不变、版本域隔离），metric_name 保持正式目录名，
    # 门禁策略 required_metrics 无需感知 variant
    EXPERIMENT_PRESET_VERSION = "experiment-v1"

    @classmethod
    def preset_definitions(
        cls, judge_model: str | None
    ) -> tuple[EvaluatorDefinition, ...]:
        """spec 8 正式指标目录的预设定义（v1，不可变；启动期幂等注册）。

        - deterministic 五个：worker 内建 handler 执行；
        - llm_judge 四个：judge_model 取组合根 Judge Provider 配置；
          domain 不变量要求 llm_judge 定义必须声明 judge_model，Judge
          未配置（None）时只注册 deterministic 预设（llm_judge 定义无
          法执行，注册即死条目）；
        - human（feedback.helpful）单独注册，不在此清单。
        """
        defs: list[EvaluatorDefinition] = []
        for preset in cls._PRESET_DETERMINISTIC:
            defs.append(
                EvaluatorDefinition(
                    definition_id=preset["definition_id"],
                    version="v1",
                    metric_name=preset["definition_id"],
                    target_kind=preset["target_kind"],
                    method="deterministic",
                    required_fields=(),
                    evidence_policy="snapshot",
                    output_type=preset["output_type"],
                    # boolean 的 range_or_categories 必须为空 tuple 之外的
                    # None（domain VO：boolean 不设范围）；number 用 (0,1)
                    range_or_categories=(
                        (0.0, 1.0)
                        if preset["output_type"] == "number"
                        else None
                    ),
                    direction="higher_is_better",
                    material_hash={},
                    judge_model=None,
                    judge_params={},
                    timeout_seconds=30,
                    budget=None,
                    ci_admissible=True,
                )
            )
        if not judge_model:
            # Judge 未配置：llm_judge 定义必须声明 judge_model（domain
            # 不变量），只注册 deterministic 预设
            return cls._with_experiment_variants(tuple(defs))
        for preset in cls._PRESET_JUDGE:
            defs.append(
                EvaluatorDefinition(
                    definition_id=preset["definition_id"],
                    version="v2" if preset["definition_id"] == "agent.scenario_quality" else "v1",
                    metric_name=preset["definition_id"],
                    target_kind=preset["target_kind"],
                    method="llm_judge",
                    required_fields=preset["required_fields"],
                    evidence_policy="snapshot",
                    output_type="number",
                    # 三态分值 0/0.5/1 不是连续区间：number 的
                    # range_or_categories 仅支持 (low, high) 二元组或
                    # None（domain VO 不变量），三态离散值集由 Judge
                    # 解析器单源约束（_TRI_STATE_VALUES）
                    range_or_categories=None,
                    direction="higher_is_better",
                    material_hash={},
                    judge_model=judge_model,
                    judge_params={"temperature": 0},
                    timeout_seconds=60,
                    budget=None,
                    ci_admissible=True,
                )
            )
        # Keep the immutable v1 preset available for existing requests/replays;
        # only v2 opts into observed execution evidence.
        scenario_v2 = next((d for d in defs if d.metric_name == "agent.scenario_quality"), None)
        if scenario_v2 is not None:
            defs.append(replace(scenario_v2, version="v1", required_fields=("turns",)))
        return cls._with_experiment_variants(tuple(defs))

    @classmethod
    def _with_experiment_variants(
        cls, definitions: tuple[EvaluatorDefinition, ...]
    ) -> tuple[EvaluatorDefinition, ...]:
        """agent_run 正式预设 -> experiment_item 镜像 variant（同内容、
        版本域隔离）：定义不可变约束（同 id+version 必须同内容）要求
        variant 走独立版本，target_kind 是内容的一部分。"""
        variants = tuple(
            replace(
                definition,
                version=("experiment-v2" if definition.metric_name == "agent.scenario_quality"
                         and definition.version == "v2" else cls.EXPERIMENT_PRESET_VERSION),
                target_kind="experiment_item",
            )
            for definition in definitions
            if definition.target_kind == "agent_run"
        )
        return definitions + variants

    async def register_preset_definitions(self, judge_model: str | None) -> None:
        """启动期幂等注册 spec 8 正式指标预设定义（同版本同内容重放）。

        线上样本作业展开（definitions_for_target）与离线实验的指标集都
        以已注册定义为准：生产不注册则自动 evaluator 永不执行（此前只有
        feedback.helpful 与测试内注册的定义存在，#1）。"""
        for definition in self.preset_definitions(judge_model):
            try:
                await self.register_definition(definition)
            except EvaluationConflictError:
                # 启用/更换 Judge 不覆盖既有不可变定义，也不让一个旧预设
                # 阻断后续 experiment_item 镜像的注册。新内容得到稳定版本。
                content_hash = _sha256_hex(definition_to_mapping(definition))
                await self.register_definition(replace(
                    definition, version=definition.version + "-" + content_hash[:16],
                ))

    # ------------------------------------------------------------------
    # submit_sample
    # ------------------------------------------------------------------

    async def definitions_for_target(
        self, target_kind: str
    ) -> tuple[DefinitionKey, ...]:
        """按目标种类解析当前线上启用的自动评估定义（T22 线上样本作业展开）。

        T5 新语义（spec 4.3）：只选择显式启用的版本——
        - usage 行存在且 online_enabled=True；无 usage 行一律不选
          （保守 fail-closed，存量自动定义由一次性回填迁移兜底启用）；
        - 仅返回 automatic 方法（deterministic/llm_judge）：human 方法定义
          不展开作业（worker 不执行人评作业，human_method_not_executable），
          人评经 submit_feedback 独立路径进入账本；
        - 允许同指标多版本同时启用（都返回，不合并）；
        - 未注册任何适用定义时返回空元组（样本仍持久化，T16 既有语义：
          定义注册前捕获的样本不回溯补开作业）；
        - 返回顺序稳定（store 按 definition_id/version 排序），保证同
          注册集合下 submit_sample 的幂等 hash 稳定。"""
        rows = await self._store.list_definitions_with_usage(
            target_kind=target_kind
        )
        keys = []
        for row in rows:
            usage = row.get("usage")
            if usage is None or not usage.get("online_enabled"):
                continue
            if row.get("method") not in ("deterministic", "llm_judge"):
                continue
            keys.append(
                DefinitionKey(
                    definition_id=str(row["definition_id"]),
                    definition_version=str(row["version"]),
                )
            )
        return tuple(keys)

    async def submit_sample(self, request: SubmitSampleRequest) -> SubmitSampleResult:
        """持久化评估样本（幂等键 service+producer_event_id）并按需展开 job。

        - 定义先解析后写入：任一 definition 不存在整体拒绝，不落半截样本；
          重复的 DefinitionKey 视为非法请求；
        - 作业展开意图（definitions/mode/experiment_id/replicate）纳入样本
          幂等 hash：同 (service, producer_event_id) 不同展开意图按"同 ID
          不同内容"409 冲突（spec 7.2）；
        - 容量（pending 作业 / 证据字节）达上限拒绝新捕获（映射 503）；
          饱和期间的幂等重放同样被拒，producer 退避后重试（保守语义）；
        - 同 (service, producer_event_id) 同内容重放返回原 sample_id 与原
          job_id，异内容 409 冲突（EvaluationConflictError）。
        """
        self._ensure_enabled()
        self._validate_sample_request(request)
        event_coordinate = (
            request.target.retrieval_id
            if request.target.kind == "retrieval"
            else request.producer_event_id
        )
        mode_component = (
            "online" if request.experiment_id is None else request.mode
        )
        # 定义先解析后写入：任一 definition 不存在整体拒绝，不落半截样本
        for key in request.definitions:
            await self._store.get_definition(
                key.definition_id, key.definition_version
            )
        await self._check_capacity()

        sample_id = await self._store.save_sample(
            self._sample_payload(request, event_coordinate, mode_component)
        )
        job_ids = []
        for key in request.definitions:
            job_ids.append(
                await self._store.save_job({
                    "sample_id": sample_id,
                    "definition_id": key.definition_id,
                    "definition_version": key.definition_version,
                    "experiment_id": request.experiment_id,
                    "replicate": request.replicate,
                    "requested_by": request.service,
                    "status": "queued",
                    "idempotency_key": job_idempotency_key(
                        service=request.service,
                        event_coordinate=event_coordinate,
                        definition_id=key.definition_id,
                        definition_version=key.definition_version,
                        mode=mode_component,
                        experiment_id=request.experiment_id,
                        replicate=request.replicate,
                    ),
                })
            )
        return SubmitSampleResult(sample_id=sample_id, job_ids=tuple(job_ids))

    def _validate_sample_request(self, request: SubmitSampleRequest) -> None:
        if not request.producer_event_id:
            raise EvaluationValidationError("producer_event_id 必须非空")
        if not request.service:
            raise EvaluationValidationError(
                "service 必须来自服务端可信来源（认证 producer 绑定或适配器）"
            )
        if request.target.kind == "retrieval" and not request.target.retrieval_id:
            # 每次真实检索唯一 ID 是 KB metric 去重键的坐标（spec 7.2）
            raise EvaluationValidationError(
                "retrieval 目标样本必须携带 retrieval_id"
            )
        seen: set[tuple[str, str]] = set()
        for key in request.definitions:
            tag = (key.definition_id, key.definition_version)
            if tag in seen:
                raise EvaluationValidationError(
                    "definitions 存在重复项: "
                    f"{key.definition_id}@{key.definition_version}"
                )
            seen.add(tag)

    def _sample_payload(
        self,
        request: SubmitSampleRequest,
        event_coordinate: str,
        mode_component: str,
    ) -> dict[str, Any]:
        return {
            "producer_event_id": request.producer_event_id,
            "service": request.service,
            "target": target_to_mapping(request.target),
            "evidence_targets": [dict(t) for t in request.evidence_targets],
            "snapshot_ref": request.snapshot_ref,
            "snapshot_hash": request.snapshot_hash,
            "schema_version": request.schema_version,
            "captured_at": request.captured_at,
            "sample_probability": request.sample_probability,
            "sampling_policy_version": request.sampling_policy_version,
            "cohort": request.cohort,
            # 作业展开意图纳入幂等 hash：同 event 不同 definitions/mode/
            # experiment/replicate 必须冲突，不得静默换意图复用样本
            "job_intent": {
                "definitions": sorted(
                    (
                        {
                            "definition_id": key.definition_id,
                            "definition_version": key.definition_version,
                        }
                        for key in request.definitions
                    ),
                    key=lambda d: (
                        d["definition_id"],
                        d["definition_version"],
                    ),
                ),
                "mode": mode_component,
                "experiment_id": request.experiment_id,
                "replicate": request.replicate,
            },
        }

    # ------------------------------------------------------------------
    # producer-state（T17 producer 接收端：高水位幂等）
    # ------------------------------------------------------------------

    async def record_producer_state(
        self, record: Mapping[str, Any]
    ) -> ProducerStateResult:
        """producer 高水位累计计数落库（spec 7.2 evaluation.producer-state.v1）。

        - store 层按 (producer_id, counter_epoch) UPSERT 绝对值覆盖，
          绝不对重发计数做累加；epoch 变更写入新行（重建计数存储换
          epoch 的合法路径）；
        - 同 sequence 重发按幂等无副作用受理（applied=False）；低
          sequence 以 EvaluationStaleSequenceError 拒绝（接口层映射
          409）；
        - observed_at 距服务端时刻超过 PRODUCER_HEARTBEAT_STALE_SECONDS
          视为心跳过期，覆盖率按 unknown 报告（缺失区间不补造统计）。
        """
        producer_id = record.get("producer_id")
        counter_epoch = record.get("counter_epoch")
        sequence = record.get("sequence")
        observed_at = record.get("observed_at")
        if not isinstance(producer_id, str) or not producer_id:
            raise EvaluationValidationError(
                "producer_state 需要 producer_id"
            )
        if not isinstance(counter_epoch, str) or not counter_epoch:
            raise EvaluationValidationError(
                "producer_state 需要 counter_epoch"
            )
        require_non_negative_int(sequence, "producer_state sequence")
        if not isinstance(observed_at, str) or not observed_at:
            raise EvaluationValidationError(
                "producer_state 需要 observed_at"
            )
        # 防御纵深：五个累计计数必须是非负整数（与路由校验共用 domain
        # 助手；缺键同样拒绝——高水位绝对值语义下缺省 0 会掩盖 producer
        # 侧协议错误）
        normalized = dict(record)
        for key in PRODUCER_STATE_COUNT_KEYS:
            normalized[key] = require_non_negative_int(
                record.get(key), f"producer_state {key}"
            )
        try:
            await self._store.save_producer_state(normalized)
            applied = True
        except EvaluationStaleSequenceError:
            # 回查现有行区分"同 sequence 重发"与"低 sequence"；
            # 契约对齐：store 缺行抛 EvaluationNotFoundError 而非返回
            # None（并发首写被判 stale 后行又被移除的罕见窗口）按
            # "重发但无行"重放一次写入
            try:
                existing = await self._store.get_producer_state(
                    producer_id, counter_epoch
                )
            except EvaluationNotFoundError:
                existing = None
            if (
                existing is not None
                and int(existing.get("sequence") or -1) == sequence
            ):
                # 同 sequence 重发：高水位幂等无副作用（不累加、不报错）
                applied = False
            elif existing is None:
                await self._store.save_producer_state(normalized)
                applied = True
            else:
                raise
        return ProducerStateResult(
            applied=applied,
            heartbeat_stale=self._producer_heartbeat_stale(observed_at),
        )

    def _producer_heartbeat_stale(self, observed_at: str) -> bool:
        """心跳过期判定：observed_at 解析失败或距今超过阈值均视为过期。"""
        try:
            observed = datetime.fromisoformat(observed_at)
        except (TypeError, ValueError):
            return True
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - observed).total_seconds() > (
            PRODUCER_HEARTBEAT_STALE_SECONDS
        )

    # ------------------------------------------------------------------
    # submit_feedback
    # ------------------------------------------------------------------

    async def submit_feedback(
        self, actor: str, request: SubmitFeedbackRequest
    ) -> SubmitFeedbackResult:
        """人工反馈用例（spec 10：Dashboard Run 详情有用/无用与评论）。

        - actor 来自服务端可信上下文参数（不从请求载荷读取）；
        - 同 (actor, target, definition) 追加 revision；``expected_revision``
          CAS 以线程当前 revision 校验，并发落败抛
          EvaluationRevisionConflictError；
        - 新 revision 生成新 feedback_id / result_id 并以 supersedes 关联
          旧版本；human result 走 store UNIQUE(feedback_id) 路径；
        - 幂等键：同 key 同 payload 复用（含崩溃窗口补齐 result），异
          payload 409 冲突。
        """
        self._ensure_enabled()
        if not isinstance(actor, str) or not actor:
            raise EvaluationValidationError(
                "actor 必须来自服务端可信上下文（非空字符串）"
            )
        definition_row = await self._store.get_definition(
            request.definition_id, request.definition_version
        )
        definition = definition_from_mapping(definition_row["definition"])
        validate_evaluation_value(definition, request.value)

        payload_hash = feedback_payload_hash(
            actor,
            request.target,
            request.definition_id,
            request.definition_version,
            request.value,
            request.comment_ref,
        )
        candidate_id = (
            feedback_id_from_request_key(request.request_key)
            if request.request_key
            else None
        )
        # #29 复用语义（最新 revision 权威）：
        # 1) 请求 payload 与当前最新 revision 相同 -> 复用最新（同分钟
        #    "有用→无用→有用"的第三击产生新 revision 后，携带旧幂等键
        #    的重放也复用最新值，不复活旧 revision）；
        # 2) 幂等键命中但 payload 不同 -> 409（键绑定冲突不变）；
        # 3) 幂等键命中、payload 相同但行已陈旧 -> 丢弃键走新增（新
        #    feedback_id，避免 PK 冲突）。
        if request.expected_revision is None:
            # 显式 CAS（expected_revision 非空）不走复用快捷路径——调用
            # 方声明了"当前 revision 必须是 N"的写意图，按完整 CAS 语义
            # 处理（并发单赢契约不变）
            latest_for_reuse = await self._store.get_latest_feedback(
                actor,
                request.target.service,
                request.target.kind,
                request.target.subject_id,
                request.definition_id,
                target_subject_revision=request.target.subject_revision,
                definition_version=request.definition_version,
            )
            if (
                latest_for_reuse is not None
                and feedback_payload_hash_from_row(latest_for_reuse)
                == payload_hash
            ):
                return await self._reuse_feedback(
                    latest_for_reuse, payload_hash
                )
        if candidate_id is not None:
            existing = await self._get_feedback_optional(candidate_id)
            if existing is not None:
                if feedback_payload_hash_from_row(existing) != payload_hash:
                    raise EvaluationConflictError(
                        "feedback 幂等键已绑定不同 payload: "
                        f"feedback_id={existing['feedback_id']}"
                    )
                # 键命中同 payload 但该行已非最新：正常新增路径（下方），
                # 旧键不再作为 feedback_id（避免 PK 冲突）
                candidate_id = None

        latest = await self._store.get_latest_feedback(
            actor,
            request.target.service,
            request.target.kind,
            request.target.subject_id,
            request.definition_id,
            target_subject_revision=request.target.subject_revision,
            definition_version=request.definition_version,
        )
        current_revision = int(latest["revision"]) if latest is not None else 0
        if (
            request.expected_revision is not None
            and request.expected_revision != current_revision
        ):
            raise EvaluationRevisionConflictError(
                f"feedback revision CAS 失败: expected="
                f"{request.expected_revision} current={current_revision}"
            )
        previous_result_id = (
            await self._store.get_result_id_by_feedback_id(latest["feedback_id"])
            if latest is not None
            else None
        )
        new_revision = current_revision + 1
        feedback_payload: dict[str, Any] = {
            "actor_ref": actor,
            "definition_id": request.definition_id,
            "definition_version": request.definition_version,
            "target": target_to_mapping(request.target),
            "value": request.value,
            "comment_ref": request.comment_ref,
            "revision": new_revision,
            "supersedes_id": (
                latest["feedback_id"] if latest is not None else None
            ),
        }
        if candidate_id is not None:
            feedback_payload["feedback_id"] = candidate_id
        # 正常路径单事务提交（spec 6.3 同事务语义）：feedback + human result
        # 原子落库，不存在"feedback 已落库、result 未落库"的可见窗口；
        # 该窗口只可能出现在历史崩溃残留上，由下方幂等键重放恢复补齐。
        # 人评 result payload/deliveries 经模块级纯函数构造（单一权威源，
        # 与 evaluation_annotation 共用，禁止两份手工拷贝导致形状分叉）
        result_payload = human_result_payload(
            definition_row, request.value, previous_result_id
        )
        # 人评 result 的 sink 投递同事务登记（spec 145/262：每个 result
        # 都有投递；投影所需 target/definition 身份由投递 worker 在领取
        # 时经 feedback 行解析，无真实身份 -> unlinked 由投影判定）
        deliveries = human_result_deliveries(
            self._delivery_sink_specs, definition_row, request.value
        )
        try:
            feedback_id, result_id = await self._store.save_feedback_with_result(
                feedback_payload, result_payload, deliveries
            )
        except EvaluationConflictError as exc:
            # 并发竞态：同 revision 唯一约束或同幂等键 PK 冲突的落败方。
            # 赢方结果按"最新 revision 同 payload 复用"语义返回；无最新
            # 同 payload（并发窗口内又被更新）按 CAS 冲突上抛。
            if candidate_id is not None:
                reused = await self._reuse_current_feedback(
                    actor,
                    payload_hash,
                    request.target,
                    request.definition_id,
                    request.definition_version,
                )
                if reused is not None:
                    return reused
            raise EvaluationRevisionConflictError(
                f"feedback revision 并发冲突: actor={actor!r} "
                f"definition={request.definition_id} revision={new_revision}"
            ) from exc
        return SubmitFeedbackResult(
            feedback_id=feedback_id,
            result_id=result_id,
            revision=new_revision,
            superseded_feedback_id=(
                latest["feedback_id"] if latest is not None else None
            ),
            reused=False,
        )

    async def _get_feedback_optional(
        self, feedback_id: str
    ) -> Mapping[str, Any] | None:
        try:
            return await self._store.get_feedback(feedback_id)
        except EvaluationNotFoundError:
            return None

    async def _reuse_current_feedback(
        self,
        actor: str,
        payload_hash: str,
        target: EvaluationTarget,
        definition_id: str,
        definition_version: str,
    ) -> SubmitFeedbackResult | None:
        """并发落败方的复用判定（#29）：当前最新 revision 与请求 payload
        一致时复用；否则 None（调用方按冲突处理）。v8 起按完整线程
        （target_subject_revision + definition_version）读取最新行。"""
        latest = await self._store.get_latest_feedback(
            actor,
            target.service,
            target.kind,
            target.subject_id,
            definition_id,
            target_subject_revision=target.subject_revision,
            definition_version=definition_version,
        )
        if latest is None:
            return None
        if feedback_payload_hash_from_row(latest) != payload_hash:
            return None
        return await self._reuse_feedback(latest, payload_hash)

    async def _reuse_feedback(
        self, existing: Mapping[str, Any], payload_hash: str
    ) -> SubmitFeedbackResult:
        if feedback_payload_hash_from_row(existing) != payload_hash:
            raise EvaluationConflictError(
                "feedback 幂等键已绑定不同 payload: "
                f"feedback_id={existing['feedback_id']}"
            )
        definition_row = await self._store.get_definition(
            existing["definition_id"], existing["definition_version"]
        )
        previous_result_id = None
        if existing.get("supersedes_id"):
            previous_result_id = await self._store.get_result_id_by_feedback_id(
                existing["supersedes_id"]
            )
        result_id = await self._ensure_human_result(
            existing["feedback_id"], definition_row, existing["value"],
            previous_result_id,
        )
        return SubmitFeedbackResult(
            feedback_id=existing["feedback_id"],
            result_id=result_id,
            revision=int(existing["revision"]),
            superseded_feedback_id=existing.get("supersedes_id"),
            reused=True,
        )

    async def _ensure_human_result(
        self,
        feedback_id: str,
        definition_row: Mapping[str, Any],
        value: Any,
        supersedes_result_id: str | None,
    ) -> str:
        """幂等创建 human result —— 仅幂等键重放的崩溃恢复路径。

        正常路径经 ``save_feedback_with_result`` 单事务原子落库，不会走到
        本方法；本方法只处理历史崩溃残留（feedback 行已存在、result 行
        缺失）的重放补齐，UNIQUE(feedback_id) 兜底并发重复创建。
        """
        payload = human_result_payload(
            definition_row,
            value,
            supersedes_result_id,
            feedback_id=feedback_id,
        )
        try:
            return await self._store.save_result(payload)
        except EvaluationConflictError:
            existing = await self._store.get_result_id_by_feedback_id(feedback_id)
            if existing is None:
                raise
            return existing

    # ------------------------------------------------------------------
    # start_experiment（本阶段仅冻结并持久化；jobs 展开属 T13）
    # ------------------------------------------------------------------

    async def start_experiment(
        self, request: StartExperimentRequest
    ) -> ExperimentStarted:
        """冻结实验请求：dataset version/hash、evaluator set hash、目标配置
        hash，持久化 experiment 记录。

        evaluator set hash 以 store 解析出的 definition hash 为准（不信任
        调用方自报）；任一定义不存在 404。按 plan 第三评审补充，本用例不
        创建任何 job：case 业务执行与 sample 生成属 T13，取得 sample 后才
        展开 case×replicate×definition jobs。
        """
        self._ensure_enabled()
        entries = []
        for key in request.evaluator_set:
            row = await self._store.get_definition(
                key.definition_id, key.definition_version
            )
            entries.append({
                "definition_id": row["definition_id"],
                "version": row["version"],
                "definition_hash": row["definition_hash"],
                "metric_name": row["metric_name"],
            })
        evaluator_set_hash = _sha256_hex(entries)
        target_config_hash = _sha256_hex(dict(request.target_config))
        experiment_id = uuid.uuid4().hex
        # 领域 VO 构造期校验（hash 形态 / mode 封闭集 / 非空约束）
        experiment = Experiment(
            experiment_id=experiment_id,
            dataset_id=request.dataset_id,
            dataset_version=request.dataset_version,
            dataset_hash=request.dataset_hash,
            target_config_hash=target_config_hash,
            evaluator_set_hash=evaluator_set_hash,
            mode=request.mode,
            status="created",
            baseline_id=request.baseline_id,
            manifest_json=request.manifest_json,
        )
        await self._store.save_experiment({
            "experiment_id": experiment.experiment_id,
            "dataset_id": experiment.dataset_id,
            "dataset_version": experiment.dataset_version,
            "dataset_hash": experiment.dataset_hash,
            "target_config_hash": experiment.target_config_hash,
            "baseline_id": experiment.baseline_id,
            "evaluator_set_hash": experiment.evaluator_set_hash,
            "manifest_json": experiment.manifest_json,
            "mode": experiment.mode,
            "status": experiment.status,
        })
        return ExperimentStarted(
            experiment_id=experiment_id,
            dataset_hash=experiment.dataset_hash,
            target_config_hash=target_config_hash,
            evaluator_set_hash=evaluator_set_hash,
        )

    # ------------------------------------------------------------------
    # retry_delivery
    # ------------------------------------------------------------------

    async def retry_delivery(
        self, sink: str, result_id: str
    ) -> Mapping[str, Any]:
        """重试指定 (sink, result_id) 的投递（spec 15）。

        仅把该 sink 最新 projection 的非终态投递回 pending
        （retry_wait/dead_letter/disabled -> pending）；pending 视为已在
        队列（幂等 no-op）；accepted 在途等待回读核实、verified/unlinked
        终态均不可重试（409）。已冻结 payload_hash 与既有 job 一律不动，
        不重新调用 Judge、不新建投递或作业。
        """
        deliveries = await self._store.list_deliveries(result_id)
        matching = [d for d in deliveries if d["sink"] == sink]
        if not matching:
            raise EvaluationNotFoundError(
                f"delivery not found: sink={sink!r} result={result_id}"
            )
        # store 列表已按 (created_at, delivery_id) 稳定排序：取最后一条即
        # 最新创建的投递。禁止按 projection_version 字典序挑选——版本号
        # 字典序与创建顺序无关（v10 < v9），会造成重投旧投影。
        delivery = matching[-1]
        status = delivery["status"]
        if status in ("retry_wait", "dead_letter", "disabled"):
            return await self._store.record_delivery_transition(
                delivery["delivery_id"], "pending"
            )
        if status == "pending":
            return delivery
        raise EvaluationConflictError(
            f"delivery 不可重试: sink={sink!r} result={result_id} "
            f"status={status}"
        )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    async def list_results(
        self,
        *,
        target_service: str | None = None,
        target_kind: str | None = None,
        target_subject_id: str | None = None,
        target_subject_revision: str | None = None,
        definition_id: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ResultsPage:
        """结果 current 视图分页查询（automatic + human，排除被取代/已撤销）。

        cursor 为上一页返回的不透明 token；target 过滤按 provenance 匹配
        （automatic 经 sample、human 经 feedback），definition/experiment/
        status 过滤语义见 store.query_results_page。``limit`` 必须为正整数，
        超过 200 按 200 封顶（分页大小上限，防止单页超载）。
        """
        if limit <= 0:
            raise EvaluationValidationError("limit 必须为正整数")
        limit = min(limit, RESULTS_PAGE_LIMIT_CAP)
        rows, next_token = await self._store.query_results_page(
            target_service=target_service,
            target_kind=target_kind,
            target_subject_id=target_subject_id,
            target_subject_revision=target_subject_revision,
            definition_id=definition_id,
            experiment_id=experiment_id,
            status=status,
            cursor=_decode_result_cursor(cursor),
            limit=limit,
        )
        next_cursor = (
            _encode_result_cursor(*next_token.split("|", 1))
            if next_token is not None
            else None
        )
        return ResultsPage(items=tuple(rows), next_cursor=next_cursor)

    async def list_results_snapshot(
        self,
        *,
        target_service: str | None = None,
        target_kind: str | None = None,
        target_subject_id: str | None = None,
        target_subject_revision: str | None = None,
        definition_id: str | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        definition_version: str | None = None,
        experiment_id: str | None = None,
        status: str | None = None,
        provenance: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[Mapping[str, Any]], str | None, str | None]:
        """结果快照分页查询（Task 9 审查修复：Interfaces 只读投影的
        Application 正式用例包装）。

        直通 ``store.query_results_page_snapshot``：参数与返回形状保持
        账本端口三元组 ``(rows, next_token, snapshot_at)``；过滤校验、
        limit 封顶与 v2 游标 fingerprint 语义由账本端口实现承担（与
        ``list_results`` 不同，本用例不额外做 limit 校验/游标转码）。
        """
        return await self._store.query_results_page_snapshot(
            target_service=target_service,
            target_kind=target_kind,
            target_subject_id=target_subject_id,
            target_subject_revision=target_subject_revision,
            definition_id=definition_id,
            created_from=created_from,
            created_to=created_to,
            definition_version=definition_version,
            experiment_id=experiment_id,
            status=status,
            provenance=provenance,
            cursor=cursor,
            limit=limit,
        )

    async def get_result_detail(self, result_id: str) -> ResultDetail:
        """结果详情：result + job/attempts（automatic）+ feedback（human）
        + sample 证据摘要 + deliveries。"""
        result = await self._store.load_result(result_id)
        job = (
            await self._store.get_job(result["job_id"])
            if result["job_id"] is not None
            else None
        )
        attempts = (
            tuple(await self._store.list_attempts(result["job_id"]))
            if job is not None
            else ()
        )
        feedback = (
            await self._store.get_feedback(result["feedback_id"])
            if result["feedback_id"] is not None
            else None
        )
        sample = (
            await self._store.load_sample(job["sample_id"])
            if job is not None
            else None
        )
        evidence_summary: dict[str, Any] = (
            {
                "snapshot_ref": sample.get("snapshot_ref") or "",
                "snapshot_hash": sample.get("snapshot_hash") or "",
                "schema_version": sample.get("schema_version") or "",
                "captured_at": sample.get("captured_at") or "",
                "target": sample.get("target") or {},
            }
            if sample is not None
            else {}
        )
        deliveries = tuple(await self._store.list_deliveries(result_id))
        return ResultDetail(
            result=result,
            job=job,
            feedback=feedback,
            sample=sample,
            evidence_summary=evidence_summary,
            attempts=attempts,
            deliveries=deliveries,
        )

    # ------------------------------------------------------------------
    # 实验查询（T21 管理面）
    # ------------------------------------------------------------------

    async def list_experiments(
        self, *, limit: int = 100
    ) -> list[Mapping[str, Any]]:
        """实验列表（管理面 GET /chat/evaluations/experiments）。"""
        return await self._store.list_experiments(limit=limit)

    async def capacity_usage(self) -> dict[str, Any]:
        """容量/积压快照（T21 管理面概览只读聚合；不做准入判定）。

        探针未装配（如测试环境）时 pending/evidence 计 0 并标注
        probe_available=False，管理面展示"容量探针不可用"而非伪造 0。"""
        if self._capacity_usage_provider is None:
            return {
                "probe_available": False,
                "pending_jobs": 0,
                "evidence_bytes": 0,
                "max_pending_jobs": self._max_pending_jobs,
                "max_evidence_bytes": self._max_evidence_bytes,
            }
        usage = await self._capacity_usage_provider()
        return {
            "probe_available": True,
            "pending_jobs": usage.pending_jobs,
            "evidence_bytes": usage.evidence_bytes,
            "max_pending_jobs": self._max_pending_jobs,
            "max_evidence_bytes": self._max_evidence_bytes,
        }

    async def daily_budget_usage(self) -> dict[str, Any]:
        """当日预算用量（T21 管理面概览；UTC 日为账期键，与 worker
        reserve/settle 同一 date_key 口径）。无账期行时返回零值。"""
        date_key = datetime.now(timezone.utc).date().isoformat()
        ledger = await self._store.get_daily_budget(date_key)
        return dict(ledger)

    async def list_producer_states(
        self, *, limit: int = 50
    ) -> list[Mapping[str, Any]]:
        """producer 状态高水位列表（管理面概览：心跳新鲜度与累计计数）。"""
        return await self._store.list_producer_states(limit=limit)

    async def experiment_result_counts(self) -> dict[str, int]:
        """按实验聚合 current 视图结果数（管理面低覆盖提示数据源）。"""
        return await self._store.count_results_by_experiment()

    async def experiment_overview(self, experiment_id: str) -> dict[str, Any]:
        """实验概览（状态/覆盖率/分布/投递状态/baseline/gate 引用）。

        覆盖率、分布与投递状态按账本实时聚合（不落快照）：runner 不回
        写 experiment 状态（T20 起 runner 延期），status 保持 ``created``
        的诚实语义；聚合结果随 job 执行推进实时变化。覆盖率/分布的扫描
        上限 5000 行，超出按已扫描部分聚合（管理面概览，非账目口径）；
        投递状态经 store 批量聚合（aggregate_experiment_deliveries），
        不受该扫描帽约束。"""
        experiment = await self._store.get_experiment(experiment_id)
        results: list[Mapping[str, Any]] = []
        cursor: str | None = None
        scan_truncated = False
        while len(results) < 5000:
            rows, next_token = await self._store.query_results_page(
                experiment_id=experiment_id, cursor=cursor, limit=200
            )
            results.extend(rows)
            cursor = next_token
            if next_token is None:
                break
        else:
            # #20：命中 5000 行扫描帽时显式标记——覆盖率/分布按已扫描
            # 部分聚合，消费方（Dashboard/导出）不得把它当全量口径
            scan_truncated = True
        distribution: dict[str, dict[str, Any]] = {}
        value_count = 0
        for row in results:
            metric = row.get("metric_name") or ""
            bucket = distribution.setdefault(
                metric,
                {
                    "results_total": 0,
                    "passed": 0,
                    "failed": 0,
                    "error_or_skipped": 0,
                    "numeric_count": 0,
                    "numeric_mean": None,
                    "numeric_min": None,
                    "numeric_max": None,
                },
            )
            bucket["results_total"] += 1
            if row.get("status") == "value":
                value_count += 1
                if row.get("passed") is True:
                    bucket["passed"] += 1
                elif row.get("passed") is False:
                    bucket["failed"] += 1
                value = row.get("typed_value")
                # bool is an int subclass; it is a verdict, not a numeric score.
                if type(value) in (int, float) and math.isfinite(value):
                    bucket["numeric_count"] += 1
                    count = bucket["numeric_count"]
                    previous = bucket["numeric_mean"]
                    bucket["numeric_mean"] = value if count == 1 else previous + (value - previous) / count
                    bucket["numeric_min"] = value if count == 1 else min(bucket["numeric_min"], value)
                    bucket["numeric_max"] = value if count == 1 else max(bucket["numeric_max"], value)
            else:
                bucket["error_or_skipped"] += 1
        # M4：投递聚合下沉到 store（单次 JOIN + GROUP BY），代替逐行
        # list_deliveries 的最坏 5000 次串行查询
        delivery_status: dict[str, dict[str, int]] = {}
        for row in await self._store.aggregate_experiment_deliveries(
            experiment_id
        ):
            sink = row["sink"] or ""
            status = row["status"] or ""
            sink_bucket = delivery_status.setdefault(
                sink,
                {
                    "pending": 0,
                    "retry_wait": 0,
                    "dead_letter": 0,
                    "accepted": 0,
                    "verified": 0,
                    "disabled": 0,
                    "unlinked": 0,
                    "total": 0,
                },
            )
            sink_bucket["total"] += row["count"]
            if status in sink_bucket:
                sink_bucket[status] += row["count"]
        baseline_id = experiment.get("baseline_id")
        return {
            "experiment": dict(experiment),
            "coverage": {
                "results_total": len(results),
                "value_results": value_count,
                "error_or_skipped_results": len(results) - value_count,
                # #20：扫描帽截断标记（true = 覆盖率/分布按前 5000 行
                # 聚合，非全量口径）
                "scan_truncated": scan_truncated,
            },
            "distribution": distribution,
            "delivery_status": delivery_status,
            "baseline": (
                {"baseline_id": baseline_id} if baseline_id else None
            ),
            "gate_report_ref": experiment.get("gate_report_ref"),
        }

    # ------------------------------------------------------------------
    # cancel（删除传播卫生操作，不受 enabled 短路约束）
    # ------------------------------------------------------------------

    async def cancel_sample_jobs(self, sample_ids: Sequence[str]) -> tuple[str, ...]:
        """撤销样本对应的排队 job（queued -> cancelled，经领域状态机校验）。

        running/终态 job 不受影响（running 由 lease 约束自然收敛，终态不可
        迁移）。评估闭环禁用时仍可用：删除传播不被业务开关阻塞。
        """
        if not sample_ids:
            return ()
        return tuple(
            await self._store.revoke_queued_jobs_for_samples(sample_ids)
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ensure_enabled(self) -> None:
        if not self._enabled:
            raise EvaluationDisabledError(
                "evaluation closed loop is disabled: submit use cases are "
                "short-circuited"
            )

    async def _check_capacity(self) -> None:
        """容量判定（映射 503）。

        并发下探针读数与实际写入之间存在竞态：探针返回后、落库前的并发
        提交可能造成有界超量准入（bounded over-admission），与"饱和期
        幂等重放同样保守拒绝"的策略一致，属接受的设计余量而非精确上限。
        """
        if self._capacity_usage_provider is None:
            return
        usage = await self._capacity_usage_provider()
        if usage.pending_jobs >= self._max_pending_jobs:
            raise EvaluationCapacityError(
                f"pending job capacity reached: {usage.pending_jobs} "
                f">= {self._max_pending_jobs}"
            )
        if usage.evidence_bytes >= self._max_evidence_bytes:
            raise EvaluationCapacityError(
                f"evidence capacity reached: {usage.evidence_bytes} bytes "
                f">= {self._max_evidence_bytes} bytes"
            )
