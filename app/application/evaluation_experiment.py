# app/application/evaluation_experiment.py
"""实验编排：execute / replay（plan T13，spec 9.1 / 11.1 / 11.2）。

EvaluationExperimentRunner 是离线实验的用例编排器，复用 EvaluationService
的 start_experiment（冻结请求）与 submit_sample（幂等样本 + job 展开）：
- execute：逐 case 冻结配置重新调用目标。目标执行经 Domain 端口
  ``EvaluationTargetRunner``（infrastructure/evaluation/target_runner.py 提供的
  适配器包装注入的 ChatCompletionService / N-KB 直调 adapter，本模块不
  import app.infrastructure，也不使用 httpx）。每 case 独立会话与隔离
  namespace；多轮 case 在同一 case 内共享会话；每 replicate 独立业务执行，
  且每 attempt 分配独立 session_id（``namespace-r{replicate}-a{attempt}``），
  传输失败重试不会把已执行轮次重放进同一会话（防跨 attempt 的
  会话污染）；
  实验项 ID = ``{case_id}#r{replicate}``；所有 attempt 记录在报告中完整
  保留。
- replay：只对现有 EvidenceSnapshot 重新评分，绝不调用业务目标；生成新的
  评分实验项（mode=replay 的 sample/job），经根 scope 的 links 关联原目标
  真实身份；实验名称标 replay。replay 实验项 ID 固定为
  ``replay:{source_sample_id}``：重评对象是既有 sample 行（execute 实验项
  与线上样本皆可作源），原 case_id/replicate 无法从任意 sample 行无损还原
  （线上样本无实验谱系），故以源 sample_id 为唯一稳定坐标；原目标
  业务身份
  保留在该项的 case_id（源 sample target subject_id）、evidence_targets 与
  根 scope links 中。源 sample 业务目标类型到评分定义类型的映射固定为
  ``agent_run -> experiment_item``、``retrieval -> retrieval``
  （REPLAY_SOURCE_KIND_MAP）；session_feedback 源不接受（人评重修不是
  自动重评，须走 feedback 修订通道），显式拒绝。
- 单项异常隔离：单实验项内非契约异常（存储故障、适配器意外崩溃等）
  记为该项 ``error`` 结局（failure_reason 记异常类型名，不携带载荷材料），
  运行继续、已完成项的报告保留；error 结局不豁免副作用清理——业务
  执行已发生的项仍按全部 attempt 副作用执行清理；契约违规（未知 status、
  非法输入等 EvaluationValidationError）保持 fail-fast 中止。

冻结快照（spec 9.1）：环境指纹（代码 commit、模型请求/响应版本、system
prompt hash、工具/Skill/MCP 清单 hash、KB/corpus 配置）由注入的
``environment_fingerprint`` 提供者（Infrastructure，仅接受 callable 注入
形态）给出：字段值必须为非空字符串（结构化配置由指纹方先做
canonical-JSON hash 成字符串，保留结构可校验性），非字符串值是契约违规
（拒绝），缺失/空
标 ``external_unpinned``；evaluator 版本集 hash / Judge 配置 hash / dataset
hash / seed / replicate_count 由本模块冻结。

失败重试边界（spec 9.1）：质量口径按首次业务执行；仅"无业务副作用的
传输失败"允许安全重试（``MAX_BUSINESS_ATTEMPTS`` 上界）；业务 error 不重试、
计入 task_success 失败分母；Judge error 单列（summarize_quality 的
``evaluation_outcomes`` 入参，由 T14 门禁从账本读取后传入）。重试门只
约束上报副作用证据的适配器：不产出副作用证据的适配器（如 ChatCompletion
适配，ChatCompletionResult 契约不含副作用证明）其 transport 失败按无
副作用处理——副作用门禁对这类适配器不生效，不伪造证据。

副作用清理：复用 E2E 通道的 manifest 所有权与清理校验思路——case 声明
allowed_side_effects/cleanup_rule 时，在评分提交完成后执行注入的
``cleanup_executor``；清理清单汇总全部 attempt 观测到的副作用（含重试
被副作用阻断的失败 attempt），清理失败作为独立失败上报
（cleanup_failures），不影响已完成评分。

实验项根采样（spec 11.1）：每个 case/replicate 的 experiment-item 根 Trace
由注入的 ``experiment_root_scope``（Infrastructure 受信实现）创建；Infrastructure
受信 sampler 仅对服务端私有可信实验上下文中的根 span 强制采样（覆盖
ParentBased），客户端 header/baggage 不能开启该覆盖；子 span 继续
ParentBased，远端 N-KB 继承真实父级决定。OTel 禁用时句柄无效，实验分析
标 unavailable（analysis_available=False），不补造采样、不替换全局
TracerProvider。

指标附着（spec 6.1/11.2）：综合指标的 sample 以 experiment_item 为目标并
携带根 trace/span（根 observation）；子检索指标以真实 retrieval 为目标
（retrieval_id + 真实 trace/span），不冒充、不合并。已知边界：ChatCompletion
适配（N-Agent agent_run 目标）为证据无关适配——其契约（ChatCompletionResult）
不携带副作用证明，因此 agent_run 项的 agent.task_success 在证据缺失时按
skipped(evidence_missing) 处理（不伪造 false/0），待 T16/T20 接线真实
副作用证据提取后恢复计分。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import asyncio

from app.application.evaluation_service import (
    DefinitionKey,
    EvaluationService,
    StartExperimentRequest,
    SubmitSampleRequest,
)
from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import (
    DatasetCase,
    DatasetManifest,
    EvidenceSnapshot,
    EvidenceSnapshotOversizedError,
    EvaluationTarget,
    EvaluationValidationError,
    EXTERNAL_UNPINNED,
    FROZEN_ENVIRONMENT_FIELDS,
    SCHEMA_VERSION,
    canonical_json_bytes,
    metric_applicable,
)
from app.domain.evaluation.state import SkipReason

LOGGER_NAME = "nagent.application.evaluation_experiment"

logger = logging.getLogger(LOGGER_NAME)

# 实验评分定义允许的目标类型：综合指标附实验项根（experiment_item）、
# 子检索指标附真实 retrieval（spec 6.1/11.2）；session_feedback 不属于实验
EXPERIMENT_DEFINITION_KINDS: tuple[str, ...] = ("experiment_item", "retrieval")

# 实验业务目标允许的 case 目标类型（agent_run -> n-agent / retrieval -> n-kb）
EXPERIMENT_CASE_KINDS: tuple[str, ...] = ("agent_run", "retrieval")

# 业务执行重试上界（含首次）：仅无业务副作用的传输失败允许重试
MAX_BUSINESS_ATTEMPTS: int = 2


class _BusinessAttemptCrashError(Exception):
    """业务 attempt 非契约崩溃的内部信封（m1）：携带已完成 attempts，
    供单项隔离路径保留 attempt 记录并执行副作用清理。cause 为原始异常，
    契约违规（EvaluationValidationError）不使用此信封（保持 fail-fast）。"""

    def __init__(
        self, cause: Exception, attempts: list["AttemptRecord"]
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.attempts = tuple(attempts)

# 可重试传输错误词汇（与 target runner 契约一致；其余错误一律不重试）
TRANSPORT_ERROR_STATUSES: frozenset[str] = frozenset({"transport_error"})

# 目标执行返回的 status 封闭词汇（target runner 契约；未知值 fail-fast，
# 不静默归入 transport_exhausted）
TARGET_RESULT_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "business_error", "transport_error"}
)

# 实验项快照脱敏版本：数据集内容按 spec 9.1 预脱敏，目标输出脱敏由冻结
# 配置的目标适配负责；快照脱敏版本标识实验证据通道
EXPERIMENT_REDACTION_VERSION: str = "n-eval-experiment-v1"

# 实验项快照内容 schema 标识
EXPERIMENT_SNAPSHOT_SCHEMA: str = "evaluation.experiment.item.v1"

# 实验项结果结局词汇（error = 单项非契约异常被隔离后的失败结局）
ITEM_OUTCOMES: tuple[str, ...] = (
    "scored",
    "business_error",
    "transport_exhausted",
    "skipped",
    "evidence_missing",
    "replay_rescored",
    "error",
)

# item 旁路清理状态词汇
CLEANUP_STATUSES: tuple[str, ...] = ("ok", "failed", "skipped", "not_required")

# replay 源 sample 业务目标类型 -> 评分定义 target_kind 的固定映射
# （agent_run/experiment_item 的综合指标都附实验项根）；session_feedback
# 源不接受（人评重修走 feedback 修订通道，不是自动重评）
REPLAY_SOURCE_KIND_MAP: Mapping[str, str] = {
    "agent_run": "experiment_item",
    "experiment_item": "experiment_item",
    "retrieval": "retrieval",
}


# ---------------------------------------------------------------------------
# 注入端口与句柄
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentRootHandle:
    """实验项根 Trace 句柄（Infrastructure experiment_root_scope 产出）。

    is_valid=False 表示 OTel 禁用或根未采样成功：不伪造身份，实验分析
    标 unavailable（spec 11.1）。
    """

    trace_id: str | None = None
    span_id: str | None = None
    is_valid: bool = False


# 根 scope 契约：Callable[identity Mapping, ContextManager[ExperimentRootHandle]]；
# identity 键：experiment_id / experiment_name / dataset_id / item_id /
# links（((trace_id, span_id), ...)，replay 关联原目标真实身份）。
ExperimentRootScope = Callable[
    [Mapping[str, Any]], AbstractContextManager[ExperimentRootHandle]
]


@dataclass(frozen=True)
class AttemptRecord:
    """单次业务执行 attempt（全部保留，重试不覆盖）。"""

    attempt_index: int
    status: str  # succeeded / business_error / transport_error
    error_type: str | None
    side_effects: tuple[str, ...]
    started_at: str
    ended_at: str


@dataclass(frozen=True)
class ExperimentItemRecord:
    """单个实验项（case x replicate）的执行记录。"""

    item_id: str
    case_id: str
    replicate: int
    outcome: str
    analysis_available: bool
    root_trace_id: str | None
    root_span_id: str | None
    sample_ids: tuple[str, ...]
    attempts: tuple[AttemptRecord, ...]
    cleanup_status: str
    skip_reason: str | None = None
    # 单项隔离失败的原因（异常类型名，不携带载荷材料）；仅 outcome=error
    failure_reason: str | None = None


@dataclass(frozen=True)
class ExperimentRunReport:
    """实验运行报告（调用方 T14/T16 持久化与展示）。"""

    experiment_id: str
    mode: str
    name: str
    frozen_snapshot: Mapping[str, Any]
    metrics: tuple[str, ...]
    items: tuple[ExperimentItemRecord, ...]
    cleanup_failures: int = 0


@dataclass(frozen=True)
class ExecuteExperimentRequest:
    """execute 模式输入。"""

    dataset: DatasetManifest
    name: str
    target_config: Mapping[str, Any]
    evaluator_set: tuple[DefinitionKey, ...]
    replicates: int = 1
    seed: str | None = None
    baseline_id: str | None = None
    # 执行单身份：场景资源账目（memory_binding intent）的归属键；host-side
    # 直调（无执行单）为空串，prepare 只返回 receipt 不登记
    execution_id: str = ""
    # 显式 case 子集（plan T5/T8）：None 执行全量；子集必须是数据集
    # case 的真子集（未知 case_id 契约违规 fail-fast）
    case_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ReplayExperimentRequest:
    """replay 模式输入：仅引用现有 sample 的证据重新评分。"""

    name: str
    source_sample_ids: tuple[str, ...]
    evaluator_set: tuple[DefinitionKey, ...]
    dataset_id: str
    dataset_version: str
    dataset_hash: str
    target_config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricQualitySummary:
    """单指标质量口径摘要（T14 门禁在此基础上结合账本分数计算结论）。

    denominator 为 manifest 预声明适用的实验项数（spec 9.2：运行后发生的
    证据缺失/预算/Judge 故障不从分母剔除）；业务 error 计入 business_failures
    （task_success 失败分母）；Judge error 单列，不混入业务失败、不从分母
    剔除；evaluated 为已提交评分证据的实验项数。
    """

    metric_name: str
    denominator: int
    business_failures: int
    judge_errors: int
    evaluated: int


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------




_MAX_EXPERIMENT_REDACTION_DEPTH = 8


def _redact_experiment_value(
    value: Any,
    redactor: Callable[[str], str],
    *,
    depth: int = 0,
    forbidden_fragments: frozenset[str] = frozenset(),
) -> Any:
    """实验快照递归脱敏（结构保持，不截断不丢条目）。

    与 capture 的 _redact_capture_value 同构，但不做逐字段截断与
    MAX_SNAPSHOT_ITEMS 裁剪——实验路径的超限处理是 evidence_oversized
    显式 skip，静默截断会让缺失证据伪装成完整证据（spec 8）。非有限
    float 投影 None（canonical_json_bytes allow_nan=False），禁含键剔除
    与非 JSON 原生类型投影与 capture 保持一致。
    """
    if depth > _MAX_EXPERIMENT_REDACTION_DEPTH:
        return f"<depth:{type(value).__name__}>"
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = (
                key if isinstance(key, str) else f"{type(key).__name__}:{key}"
            )
            lowered = key_text.lower()
            if any(
                fragment in lowered for fragment in forbidden_fragments
            ):
                continue
            redacted[key_text] = _redact_experiment_value(
                item, redactor, depth=depth + 1,
                forbidden_fragments=forbidden_fragments,
            )
        return redacted
    if isinstance(value, (list, tuple)):
        return [
            _redact_experiment_value(item, redactor, depth=depth + 1,
                                     forbidden_fragments=forbidden_fragments)
            for item in value
        ]
    # 非 JSON 原生：实验路径不做类型名投影静默降级——上抛交由证据
    # 持久化失败路径 skip(evidence_missing)，不把缺失证据伪装成完整
    raise TypeError(
        f"experiment snapshot 包含不可 JSON 序列化值: {type(value).__name__}"
    )


class EvaluationExperimentRunner:
    """实验编排用例入口（execute / replay）。"""

    def __init__(
        self,
        *,
        service: EvaluationService,
        ledger: Any,
        target_runner: Any,
        evidence: Any,
        experiment_root_scope: ExperimentRootScope,
        environment_fingerprint: Callable[[], Mapping[str, Any]] | None = None,
        cleanup_executor: Callable[[Mapping[str, Any]], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        redactor: Callable[[str], str] | None = None,
    ) -> None:
        self._service = service
        self._ledger = ledger
        self._target_runner = target_runner
        self._evidence = evidence
        self._root_scope = experiment_root_scope
        self._environment_fingerprint = environment_fingerprint
        self._cleanup_executor = cleanup_executor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # 快照期脱敏（24 项三方审阅 #6）：数据集预审核不覆盖运行时目标
        # 输出——敏感正文不得未脱敏落盘并进入外部 Judge。组合根注入
        # LogSanitizer；内建 _default_redact 兜底不可关闭（与线上 capture
        # 同一双层结构，先精确匹配后形态规则）
        self._redactor = redactor or (lambda text: text)

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    async def execute(
        self, request: ExecuteExperimentRequest, *, on_started: Any = None
    ) -> ExperimentRunReport:
        """execute 模式：冻结配置逐 case 重新调用目标并生成评分样本。"""
        self._validate_replicates(request.replicates)
        definitions, metric_names = await self._resolve_definitions(
            request.evaluator_set
        )
        evaluator_keys = self._definition_keys(definitions)
        dataset = request.dataset
        self._validate_case_kinds(dataset)
        # 显式子集（plan T8）：执行单 case_ids 必须是数据集 case 的子集；
        # 未知 case_id 契约违规 fail-fast（与 submit 侧 select_case_ids 同语义）
        if request.case_ids is not None:
            known = {case.case_id for case in dataset.cases}
            unknown = sorted(set(request.case_ids) - known)
            if unknown:
                raise EvaluationValidationError(
                    f"case_ids 含未知 case: {unknown}"
                )
            selected = tuple(
                case for case in dataset.cases if case.case_id in set(request.case_ids)
            )
        else:
            selected = tuple(dataset.cases)
        root_defs, retrieval_defs = self._split_definitions(definitions)
        started = await self._service.start_experiment(
            StartExperimentRequest(
                dataset_id=dataset.dataset_id,
                dataset_version=dataset.dataset_version,
                dataset_hash=dataset.dataset_hash,
                target_config=dict(request.target_config),
                evaluator_set=evaluator_keys,
                mode="execute",
                baseline_id=request.baseline_id,
                manifest_json=self._manifest_json(dataset),
            )
        )
        if on_started is not None:
            await on_started(started)
        frozen = self._build_frozen_snapshot(
            mode="execute",
            started=started,
            dataset_hash=dataset.dataset_hash,
            dataset_version=dataset.dataset_version,
            definitions=definitions,
            seed=request.seed,
            replicates=request.replicates,
        )
        logger.info(
            "evaluation experiment execute started: experiment_id=%s "
            "dataset_id=%s cases=%d replicates=%d metrics=%d",
            started.experiment_id,
            dataset.dataset_id,
            len(selected),
            request.replicates,
            len(metric_names),
        )
        items: list[ExperimentItemRecord] = []
        cleanup_failures = 0
        for case in selected:
            for replicate in range(request.replicates):
                # 单项异常隔离：非契约异常（存储故障、适配器意外崩溃）
                # 记为该项 error 结局，运行继续；契约违规保持 fail-fast
                try:
                    item = await self._execute_item(
                        experiment_id=started.experiment_id,
                        name=request.name,
                        dataset=dataset,
                        case=case,
                        replicate=replicate,
                        target_config=dict(request.target_config),
                        root_defs=root_defs,
                        retrieval_defs=retrieval_defs,
                        execution_id=request.execution_id,
                    )
                except EvaluationValidationError:
                    raise
                except _BusinessAttemptCrashError as crash:
                    # m1（质量复审）：业务 attempt 崩溃（run_target 非契约
                    # 异常）时业务可能已部分执行——隔离失败记录保留崩溃
                    # attempt，清理按 docstring 契约"error 结局不豁免副作用
                    # 清理"执行，cleanup_status 如实上报
                    cleanup_status = await self._run_cleanup(
                        case, crash.attempts
                    )
                    item = self._isolated_failure_record(
                        experiment_item_id(case.case_id, replicate),
                        case.case_id,
                        replicate,
                        crash.cause,
                        attempts=crash.attempts,
                        cleanup_status=cleanup_status,
                    )
                except Exception as exc:  # noqa: BLE001 - 隔离后继续运行
                    item = self._isolated_failure_record(
                        experiment_item_id(case.case_id, replicate),
                        case.case_id,
                        replicate,
                        exc,
                    )
                items.append(item)
                logger.info(
                    "evaluation experiment item finished: "
                    "experiment_id=%s item_id=%s outcome=%s samples=%d",
                    started.experiment_id,
                    item.item_id,
                    item.outcome,
                    len(item.sample_ids),
                )
                if item.cleanup_status == "failed":
                    cleanup_failures += 1
        logger.info(
            "evaluation experiment execute finished: experiment_id=%s "
            "items=%d cleanup_failures=%d",
            started.experiment_id,
            len(items),
            cleanup_failures,
        )
        return ExperimentRunReport(
            experiment_id=started.experiment_id,
            mode="execute",
            name=request.name,
            frozen_snapshot=frozen,
            metrics=tuple(metric_names),
            items=tuple(items),
            cleanup_failures=cleanup_failures,
        )

    def _isolated_failure_record(
        self,
        item_id: str,
        case_id: str,
        replicate: int,
        exc: Exception,
        *,
        attempts: tuple[AttemptRecord, ...] = (),
        cleanup_status: str = "not_required",
    ) -> ExperimentItemRecord:
        """单项隔离失败记录：只保留异常类型名，不携带载荷材料。

        ``attempts``/``cleanup_status`` 供业务执行已发生的崩溃路径（m1）：
        崩溃 attempt 与清理结果一并入记录；未进入业务执行的隔离失败
        （装配段崩溃等）无 attempt、清理无对象，保持默认 not_required。"""
        logger.warning(
            "evaluation experiment item failed: item_id=%s error_type=%s",
            item_id,
            type(exc).__name__,
        )
        return ExperimentItemRecord(
            item_id=item_id,
            case_id=case_id,
            replicate=replicate,
            outcome="error",
            analysis_available=False,
            root_trace_id=None,
            root_span_id=None,
            sample_ids=(),
            attempts=tuple(attempts),
            cleanup_status=cleanup_status,
            failure_reason=type(exc).__name__,
        )

    @staticmethod
    def _definition_keys(
        definitions: Mapping[tuple[str, str], Mapping[str, Any]]
    ) -> tuple[DefinitionKey, ...]:
        """从已解析定义构造 DefinitionKey（不重复归一化请求引用）。"""
        return tuple(
            DefinitionKey(definition_id=definition_id, definition_version=version)
            for definition_id, version in definitions
        )

    def _validate_replicates(self, replicates: int) -> None:
        if isinstance(replicates, bool) or not isinstance(replicates, int) or (
            replicates < 1
        ):
            raise EvaluationValidationError(
                "replicates 必须是正整数（默认 1，显式重复实验建议 3）: "
                f"{replicates!r}"
            )

    def _validate_case_kinds(self, dataset: DatasetManifest) -> None:
        for case in dataset.cases:
            if case.target_kind not in EXPERIMENT_CASE_KINDS:
                raise EvaluationValidationError(
                    f"case {case.case_id!r} target_kind {case.target_kind!r} "
                    f"不属于可执行实验目标 {EXPERIMENT_CASE_KINDS}"
                )

    async def _execute_item(
        self,
        *,
        experiment_id: str,
        name: str,
        dataset: DatasetManifest,
        case: DatasetCase,
        replicate: int,
        target_config: Mapping[str, Any],
        root_defs: tuple[DefinitionKey, ...],
        retrieval_defs: tuple[DefinitionKey, ...],
        execution_id: str = "",
    ) -> ExperimentItemRecord:
        item_id = experiment_item_id(case.case_id, replicate)
        namespace = session_namespace(experiment_id, case.case_id)
        turns = extract_turns(case)
        target_service = "n-agent" if case.target_kind == "agent_run" else "n-kb"
        business_target = EvaluationTarget(
            service=target_service,
            kind=case.target_kind,
            subject_id=item_id,
            subject_revision=dataset.dataset_version,
        )
        sample_ids: list[str] = []
        skip_reason: str | None = None
        failure_reason: str | None = None
        outcome = "scored"
        with self._root_scope({
            "experiment_id": experiment_id,
            "experiment_name": name,
            "dataset_id": dataset.dataset_id,
            "item_id": item_id,
        }) as root:
            # 业务执行：仅无业务副作用的传输失败允许安全重试；每 attempt
            # 分配独立 session_id（namespace-r{replicate}-a{attempt}），重试
            # 不把已执行轮次重放进同一会话（防跨 attempt 会话污染）
            final_result, attempts = await self._run_business_attempts(
                business_target=business_target,
                namespace=namespace,
                replicate=replicate,
                target_config=target_config,
                turns=turns,
                profile=(
                    case.scenario.execution.profile
                    if case.scenario is not None
                    else None
                ),
                scenario_options=(
                    {
                        key: value
                        for key, value in (
                            ("external_memory_enabled", case.scenario.options.external_memory_enabled),
                        )
                        if value is not None
                    }
                    if case.scenario is not None
                    else None
                ),
                scenario_context=(
                    {
                        "execution_id": execution_id,
                        "case_id": case.case_id,
                    }
                    if case.scenario is not None
                    else None
                ),
                scenario_files=_scenario_file_paths(case),
            )
            if final_result is None:
                outcome = (
                    "business_error"
                    if attempts[-1].status == "business_error"
                    else "transport_exhausted"
                )
            else:
                # 证据快照构造失败按异常类型路由 skip_reason（类型化分类，
                # 禁止按错误消息文本分类）：超限 -> evidence_oversized，
                # 其余契约违规（不可序列化等）-> evidence_missing；非契约
                # 异常（存储故障等）记单项 error，但已发生的业务执行仍须
                # 走清理（error 结局不豁免副作用清理）
                snapshot_ref: str | None = None
                snapshot_hash: str | None = None
                try:
                    snapshot_ref, snapshot_hash = await self._persist_evidence(
                        experiment_id=experiment_id,
                        dataset=dataset,
                        case=case,
                        replicate=replicate,
                        item_id=item_id,
                        result=final_result,
                        root=root,
                        frozen_target_model=target_config.get("target_model"),
                        execution_fingerprint=target_config.get("execution_fingerprint"),
                    )
                except EvidenceSnapshotOversizedError:
                    outcome = "skipped"
                    skip_reason = SkipReason.evidence_oversized.value
                except EvaluationValidationError:
                    outcome = "skipped"
                    skip_reason = SkipReason.evidence_missing.value
                except Exception as exc:  # noqa: BLE001 - 隔离后继续运行
                    outcome = "error"
                    failure_reason = type(exc).__name__
                    logger.warning(
                        "evaluation experiment item errored after business "
                        "execution: experiment_id=%s item_id=%s "
                        "failure_reason=%s",
                        experiment_id,
                        item_id,
                        failure_reason,
                    )
                if outcome == "scored" and snapshot_ref is not None:
                    try:
                        sample_ids.extend(
                            await self._submit_scoring_samples(
                                experiment_id=experiment_id,
                                item_id=item_id,
                                dataset=dataset,
                                replicate=replicate,
                                root=root,
                                snapshot_ref=snapshot_ref,
                                snapshot_hash=snapshot_hash,
                                result=final_result,
                                root_defs=root_defs,
                                retrieval_defs=retrieval_defs,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - 同上
                        outcome = "error"
                        failure_reason = type(exc).__name__
                        logger.warning(
                            "evaluation experiment item errored during "
                            "sample submission: experiment_id=%s "
                            "item_id=%s failure_reason=%s",
                            experiment_id,
                            item_id,
                            failure_reason,
                        )
            # 清理覆盖全部 attempt 观测到的副作用（error 结局同样不豁免）
            cleanup_status = await self._run_cleanup(case, attempts)
            return ExperimentItemRecord(
                item_id=item_id,
                case_id=case.case_id,
                replicate=replicate,
                outcome=outcome,
                analysis_available=bool(root.is_valid),
                root_trace_id=root.trace_id,
                root_span_id=root.span_id,
                sample_ids=tuple(sample_ids),
                attempts=tuple(attempts),
                cleanup_status=cleanup_status,
                skip_reason=skip_reason,
                failure_reason=failure_reason,
            )

    async def _run_business_attempts(
        self,
        *,
        business_target: EvaluationTarget,
        namespace: str,
        replicate: int,
        target_config: Mapping[str, Any],
        turns: tuple[Mapping[str, Any], ...],
        profile: str | None = None,
        scenario_options: Mapping[str, Any] | None = None,
        scenario_context: Mapping[str, Any] | None = None,
        scenario_files: tuple[str, ...] = (),
    ) -> tuple[Mapping[str, Any] | None, list[AttemptRecord]]:
        """业务执行 attempt 循环（从 _execute_item 抽出）：返回最终成功结果
        （无成功为 None）与全部 attempt 记录（重试不覆盖）。"""
        attempts: list[AttemptRecord] = []
        for attempt_index in range(MAX_BUSINESS_ATTEMPTS):
            # 每 attempt 独立会话：重试在全新会话内完整重放全部轮次
            session_id = f"{namespace}-r{replicate}-a{attempt_index}"
            started_at = self._clock().astimezone(timezone.utc).isoformat()
            # 场景 case 携带执行 profile：基础设施按 profile 选择正式适配器
            # （spec 101：两个 runner 按 profile 选择并存）；非场景 case 不携带
            request: dict[str, Any] = {
                "mode": "execute",
                "target_config": dict(target_config),
                "session": {
                    "namespace": namespace,
                    "session_id": session_id,
                },
                "turns": [dict(turn) for turn in turns],
            }
            if profile is not None:
                request["profile"] = profile
            if scenario_options:
                request["scenario_options"] = dict(scenario_options)
            if scenario_context:
                request["scenario_context"] = dict(scenario_context)
            if scenario_files:
                # T10：场景声明的工作区文件清单（适配器按此收集文件证据）
                request["scenario_files"] = list(scenario_files)
            try:
                result = await self._target_runner.run_target(
                    business_target, request
                )
            except EvaluationValidationError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 由单项隔离接管
                # m1（质量复审）：run_target 非契约崩溃时业务可能已部分
                # 执行——记崩溃 attempt（business_error、无副作用观测）
                # 并携带已完成 attempts 上抛，由单项隔离路径执行清理
                attempts.append(
                    AttemptRecord(
                        attempt_index=attempt_index,
                        status="business_error",
                        error_type=type(exc).__name__,
                        side_effects=(),
                        started_at=started_at,
                        ended_at=self._clock()
                        .astimezone(timezone.utc)
                        .isoformat(),
                    )
                )
                raise _BusinessAttemptCrashError(exc, attempts) from exc
            ended_at = self._clock().astimezone(timezone.utc).isoformat()
            status = str(result.get("status") or "")
            if status not in TARGET_RESULT_STATUSES:
                # 适配器返回未知结局词汇属契约违规：fail-fast（不静默
                # 归入 transport_exhausted，避免污染质量口径）
                raise EvaluationValidationError(
                    f"target runner 返回未知 status: {status!r}，"
                    f"仅允许 {sorted(TARGET_RESULT_STATUSES)}"
                )
            side_effects = tuple(
                str(item) for item in result.get("side_effects") or ()
            )
            attempts.append(
                AttemptRecord(
                    attempt_index=attempt_index,
                    status=status,
                    error_type=result.get("error_type"),
                    side_effects=side_effects,
                    started_at=started_at,
                    ended_at=ended_at,
                )
            )
            if status == "succeeded":
                return result, attempts
            # 失败尝试的证据摘要必须可见（不留诊断盲区，T11/T12 教训）：
            # transport/business 失败时适配器可能随错误返回部分证据
            evidence = (result.get("output") or {}).get("scenario_evidence") or {}
            acp_block = evidence.get("acp") or {}
            logger.warning(
                "evaluation target attempt failed: profile=%s status=%s "
                "error_type=%s reason=%s stages=%s stderr=%s",
                profile, status, result.get("error_type"),
                str(evidence.get("reason") or "")[:300],
                json.dumps(
                    acp_block.get("stages")
                    or (evidence.get("cli") or {}).get("steps") or [],
                    ensure_ascii=False,
                )[:600],
                str(acp_block.get("stderr") or "")[-1500:],
            )
            if not self._retry_allowed(result):
                return None, attempts
        return None, attempts

    @staticmethod
    def _retry_allowed(result: Mapping[str, Any]) -> bool:
        """重试边界：仅无业务副作用的传输失败允许安全重试。

        ``side_effects_unobserved``（24 项三方审阅 #16）：适配器显式声明
        副作用不可观测（HTTP 传输失败时服务端可能已执行写工具，空列表
        只是未采集）——不满足"没有产生业务副作用的证据"前提，不自动重
        跑整个 case。"""
        status = str(result.get("status") or "")
        if status not in TRANSPORT_ERROR_STATUSES:
            return False
        if result.get("side_effects_unobserved"):
            return False
        return not tuple(result.get("side_effects") or ())

    async def _persist_evidence(
        self,
        *,
        experiment_id: str,
        dataset: DatasetManifest,
        case: DatasetCase,
        replicate: int,
        item_id: str,
        result: Mapping[str, Any],
        root: ExperimentRootHandle,
        frozen_target_model: str | None = None,
        execution_fingerprint: str | None = None,
    ) -> tuple[str, str]:
        output = result.get("output") or {}
        turns = extract_turns(case)
        retrievals = [
            {
                "retrieval_id": str(entry.get("retrieval_id") or ""),
                "corpus_revision": entry.get("corpus_revision"),
                "query": entry.get("query"),
                "results": list(entry.get("results") or []),
                "chunks": list(entry.get("chunks") or []),
            }
            for entry in result.get("retrievals") or ()
            if entry.get("retrieval_id")
        ]
        # Judge 槽位投影（spec 6.2 契约对齐）：
        # - answer <- final_answer 或最后一个 assistant 轮
        # - chunks/contexts <- 检索条目 chunks/results 文本展平
        # - query <- 检索条目 query（多检索取首条非空）
        answer_text = output.get("final_answer") or next(
            (
                str(turn.get("content") or "")
                for turn in reversed(turns)
                if str(turn.get("role") or "") == "assistant"
            ),
            "",
        )
        chunk_texts: list[str] = []
        for entry in retrievals:
            for chunk in entry["chunks"] or entry["results"]:
                if isinstance(chunk, Mapping):
                    text = chunk.get("text") or chunk.get("excerpt") or ""
                else:
                    text = str(chunk)
                text = str(text or "").strip()
                if text:
                    chunk_texts.append(text)
        query_text = next(
            (
                str(entry.get("query") or "")
                for entry in retrievals
                if entry.get("query")
            ),
            "",
        )
        expected_output = case.expected_output
        # #22：多轮 case 评价对象是最后一轮问答——request 取最后一个
        # user 轮（首轮陈述会让 relevance 评价"陈述 vs 末轮回答"）；
        # 无显式 role 的轮（{"message": ...} 形态）按 user 轮计
        request_text = ""
        for turn in reversed(turns):
            role = str(turn.get("role") or "")
            if role and role != "user":
                continue
            text = next(
                (
                    str(turn.get(key) or "")
                    for key in ("content", "message")
                    if str(turn.get(key) or "")
                ),
                "",
            )
            if text:
                request_text = text
                break
        # #22：expected_output 为 Mapping（内置数据集形态）时派生字符串
        # reference——只接受 str 会让 correctness 全部 skipped(
        # missing_reference)，reference-dependent 覆盖率无法达标
        reference_text: str | None = None
        if isinstance(expected_output, str) and expected_output:
            reference_text = expected_output
        elif isinstance(expected_output, Mapping):
            for key in ("expectation", "expected", "answer"):
                value = expected_output.get(key)
                if isinstance(value, str) and value:
                    reference_text = value
                    break
        content: dict[str, Any] = {
            "schema": EXPERIMENT_SNAPSHOT_SCHEMA,
            "experiment_id": experiment_id,
            "item_id": item_id,
            "case_id": case.case_id,
            "replicate": replicate,
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.dataset_version,
            "turns": turns,
            "final_answer": output.get("final_answer"),
            "completion_reported": bool(output.get("completion_reported", False)),
            "execution_fingerprint": execution_fingerprint,
            "target_model": (
                output.get("model_response_version")
                if output.get("model_response_version") not in (None, "", "N-Agent", "n-agent")
                else frozen_target_model
            ),
            "tool_calls": list(output.get("tool_calls") or []),
            "side_effect_evidence": output.get("side_effect_evidence"),
            "allowed_tools": list(output.get("allowed_tools") or []),
            "expected_side_effects": list(case.allowed_side_effects),
            # MappingProxyType 不是 JSON 原生：投影为 plain dict（否则
            # 序列化预检整体拒绝 -> evidence_missing，reference 派生失效）
            "expected_output": (
                dict(case.expected_output)
                if isinstance(case.expected_output, Mapping)
                else case.expected_output
            ),
            # #24：qrels 语义区分——case 未声明金标（None）时快照保留
            # None（worker kb 指标按 missing_reference 跳过，不运行）；
            # 显式空 dict = 查询无相关项（no_answer 口径），转空列表
            "qrels": (
                qrels_entries(case.qrels) if case.qrels is not None else None
            ),
            "results": list(output.get("results") or []),
            "retrievals": retrievals,
            "business_run_id": result.get("run_id"),
            # -- Judge 槽位派生字段（#6）--
            "request": request_text or None,
            "answer": str(answer_text) if answer_text else None,
            "reference": reference_text,
            "chunks": chunk_texts or None,
            "contexts": chunk_texts or None,
            "query": query_text or None,
        }
        # plan T3/T6：schema 2 场景负载落快照——assertions 来自 case 冻结
        # 契约（JSON 形态），scenario_evidence 来自目标适配器经正式端口
        # 采集的证据（缺证据时适配器上报 unavailable/incomplete，此处
        # 原样透传，不合成不补齐）
        if case.scenario is not None:
            content["assertions"] = [
                {
                    "assertion_type": assertion.assertion_type,
                    "params": dict(assertion.params),
                    "step_id": assertion.step_id,
                }
                for assertion in case.scenario.assertions
            ]
            content["scenario_evidence"] = output.get("scenario_evidence")
            # 逐轮 Judge 槽位（agent.scenario_quality）：request=输入消息
            # 文本、answer=正式端口归属的真实轮次回答（scenario_evidence
            # steps 按 step_id 对齐）、rubric=冻结契约逐轮 rubric。扩展
            # 输入 turn 而非替换——原始输入（含内联图片 messages）保留
            # 在快照中；某轮缺真实回答时 answer 为 None，Judge 按缺证据
            # 本地 skipped，不合成文本
            answers = {
                str(step.get("step_id")): step.get("answer")
                for step in (
                    (output.get("scenario_evidence") or {}).get("steps") or []
                )
                if isinstance(step, Mapping)
            }
            rubrics = case.scenario.expected_output.steps
            projected_turns: list[dict[str, Any]] = []
            for turn in turns:
                projected = dict(turn)
                step_id = str(projected.get("step_id") or "")
                projected["request"] = str(projected.get("message") or "")
                if projected.get("action") == "task_create":
                    projected["request"] = json.dumps(projected.get("task") or {}, ensure_ascii=False)
                projected["answer"] = answers.get(step_id)
                expected_step = rubrics.get(step_id) or {}
                projected["rubric"] = expected_step.get("rubric")
                projected_turns.append(projected)
            content["turns"] = projected_turns
        # 快照期脱敏双层（24 项三方审阅 #6）：先注入 redactor（组合根
        # 注入的 LogSanitizer），后内建 _default_redact 兜底——与线上
        # capture 同一结构；运行时目标输出同样可能携带敏感正文。不采用
        # capture 的逐字段截断/条数上限：静默截断后仍评分正是 spec 8
        # 禁止的"截断后判完整"，超限交由 evidence_oversized 显式 skip
        from app.application.evaluation_capture import (
            _FORBIDDEN_KEY_FRAGMENTS,
            _default_redact,
        )

        def _snapshot_redactor(text: str) -> str:
            return _default_redact(self._redactor(text))

        # 先做序列化预检（与 save_snapshot 的 canonical_json_bytes 同一
        # 判据）：不可序列化的证据按 evidence_missing 显式 skip，不得被
        # 脱敏投影或禁含键剔除静默吞掉后伪装成完整证据
        canonical_json_bytes(content)
        redacted = _redact_experiment_value(
            content,
            _snapshot_redactor,
            forbidden_fragments=_FORBIDDEN_KEY_FRAGMENTS,
        )
        if not isinstance(redacted, dict):  # pragma: no cover - 防御
            raise EvaluationValidationError("实验快照脱敏后退化失败")
        snapshot = EvidenceSnapshot(
            content=redacted,
            redaction_version=EXPERIMENT_REDACTION_VERSION,
        )
        ref = await self._evidence.save_snapshot(
            EvaluationTarget(
                service="n-agent",
                kind="experiment_item",
                subject_id=item_id,
                subject_revision=dataset.dataset_version,
                trace_id=root.trace_id,
                span_id=root.span_id,
            ),
            snapshot,
        )
        return ref, snapshot.content_sha256

    async def _submit_scoring_samples(
        self,
        *,
        experiment_id: str,
        item_id: str,
        dataset: DatasetManifest,
        replicate: int,
        root: ExperimentRootHandle,
        snapshot_ref: str,
        snapshot_hash: str,
        result: Mapping[str, Any],
        root_defs: tuple[DefinitionKey, ...],
        retrieval_defs: tuple[DefinitionKey, ...],
    ) -> list[str]:
        sample_ids: list[str] = []
        captured_at = self._clock().astimezone(timezone.utc).isoformat()
        if root_defs:
            # 综合指标附实验项根 observation（真实根 trace/span）
            result_id = await self._service.submit_sample(
                SubmitSampleRequest(
                    producer_event_id=f"experiment:{experiment_id}:{item_id}:root",
                    service="n-agent",
                    target=EvaluationTarget(
                        service="n-agent",
                        kind="experiment_item",
                        subject_id=item_id,
                        subject_revision=dataset.dataset_version,
                        trace_id=root.trace_id,
                        span_id=root.span_id,
                    ),
                    evidence_targets=self._evidence_targets(result, dataset),
                    snapshot_ref=snapshot_ref,
                    snapshot_hash=snapshot_hash,
                    schema_version=SCHEMA_VERSION,
                    captured_at=captured_at,
                    sampling_policy_version="experiment",
                    definitions=tuple(root_defs),
                    experiment_id=experiment_id,
                    mode="execute",
                    replicate=replicate,
                )
            )
            sample_ids.append(result_id.sample_id)
        # 子检索指标附真实 retrieval 目标（retrieval_id + 真实 trace/span）
        for entry in result.get("retrievals") or ():
            retrieval_id = str(entry.get("retrieval_id") or "")
            if not retrieval_id or not retrieval_defs:
                continue
            retrieval_result = await self._service.submit_sample(
                SubmitSampleRequest(
                    producer_event_id=(
                        f"experiment:{experiment_id}:{item_id}"
                        f":retrieval:{retrieval_id}"
                    ),
                    service="n-kb",
                    target=EvaluationTarget(
                        service="n-kb",
                        kind="retrieval",
                        subject_id=retrieval_id,
                        subject_revision=(
                            str(entry.get("corpus_revision") or "")
                            or dataset.dataset_version
                        ),
                        retrieval_id=retrieval_id,
                        trace_id=entry.get("trace_id"),
                        span_id=entry.get("span_id"),
                    ),
                    # #8：retrieval 样本同登记真实消费的文档版本——纯 KB
                    # 实验没有根样本，缺登记会让文档删除无法撤销对应
                    # jobs/results/投递（AC19 删除传播静默失效）
                    evidence_targets=(
                        {
                            "source_service": "n-kb",
                            "subject_kind": "retrieval",
                            "subject_id": retrieval_id,
                            "subject_revision": (
                                str(entry.get("corpus_revision") or "")
                            ),
                        },
                        *self._document_targets((entry,)),
                    ),
                    snapshot_ref=snapshot_ref,
                    snapshot_hash=snapshot_hash,
                    schema_version=SCHEMA_VERSION,
                    captured_at=captured_at,
                    sampling_policy_version="experiment",
                    definitions=tuple(retrieval_defs),
                    experiment_id=experiment_id,
                    mode="execute",
                    replicate=replicate,
                )
            )
            sample_ids.append(retrieval_result.sample_id)
        return sample_ids

    @staticmethod
    def _evidence_targets(
        result: Mapping[str, Any], dataset: DatasetManifest
    ) -> tuple[Mapping[str, Any], ...]:
        targets: list[Mapping[str, Any]] = []
        if result.get("run_id"):
            targets.append(
                {
                    "source_service": "n-agent",
                    "subject_kind": "agent_run",
                    "subject_id": str(result["run_id"]),
                    "subject_revision": dataset.dataset_version,
                }
            )
        targets.extend(
            EvaluationExperimentRunner._document_targets(
                result.get("retrievals") or ()
            )
        )
        observed = (result.get("output") or {}).get("scenario_evidence") or {}
        seen = {(t["subject_kind"], t["subject_id"]) for t in targets}

        def add_identity(kind: str, subject_id: Any) -> None:
            if not isinstance(subject_id, str) or not subject_id or (kind, subject_id) in seen:
                return
            seen.add((kind, subject_id))
            targets.append({"source_service": "n-agent", "subject_kind": kind,
                            "subject_id": subject_id, "subject_revision": ""})

        add_identity("session", (result.get("output") or {}).get("session_id"))
        if isinstance(observed, Mapping):
            for step in observed.get("steps") or ():
                if isinstance(step, Mapping):
                    add_identity("agent_run", step.get("run_id"))
            for task in observed.get("tasks") or ():
                if not isinstance(task, Mapping):
                    continue
                add_identity("session", task.get("session_id"))
                for run in task.get("runs") or ():
                    if not isinstance(run, Mapping):
                        continue
                    # TaskRun.id is an integer, not an AgentRun identity.
                    for agent_run in run.get("agent_runs") or ():
                        if isinstance(agent_run, Mapping):
                            add_identity("agent_run", agent_run.get("run_id"))
                            add_identity("session", agent_run.get("session_id"))
            targets.extend(EvaluationExperimentRunner._document_targets(
                [entry for entry in observed.get("retrievals") or ()
                 if isinstance(entry, Mapping) and entry.get("backend") == "n_kb"]
            ))
        return tuple(targets)

    @staticmethod
    def _document_targets(
        entries: Any,
    ) -> tuple[Mapping[str, Any], ...]:
        """检索条目 -> 真实消费的 N-KB 文档版本反向索引（#17/#8）。

        缺失会让 T18 删除传播静默范围缩减（已删文档的实验样本/投递不
        被撤销）。去重键 (document_id, revision)，与线上捕获同形态。"""
        targets: list[Mapping[str, Any]] = []
        seen_documents: set[tuple[str, str]] = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            if not entry.get("retrieval_id") and entry.get("backend") != "n_kb":
                continue
            for chunk in entry.get("chunks") or entry.get("results") or ():
                if not isinstance(chunk, Mapping):
                    continue
                document_id = chunk.get("document_id")
                if not document_id:
                    continue
                revision = chunk.get("document_revision") or ""
                key = (str(document_id), str(revision))
                if key in seen_documents:
                    continue
                seen_documents.add(key)
                targets.append(
                    {
                        "source_service": "n-kb",
                        "subject_kind": "document",
                        "subject_id": str(document_id),
                        "subject_revision": str(revision),
                    }
                )
        return tuple(targets)

    async def _run_cleanup(
        self, case: DatasetCase, attempts: Sequence[AttemptRecord]
    ) -> str:
        """副作用清理：复用 E2E 通道所有权与清理校验思路；清理清单汇总
        全部 attempt 观测到的副作用（含重试被副作用阻断的失败 attempt）；
        清理失败独立上报，不影响已完成评分。

        清理触发：case 声明 cleanup_rule 且发生了任意业务执行（succeeded /
        business_error），或任一 attempt 观测到副作用；纯传输耗尽且无任何
        副作用观测（无业务执行发生）无可清理对象。"""
        if not case.cleanup_rule:
            return "not_required"
        side_effects = sorted({
            effect
            for attempt in attempts
            for effect in attempt.side_effects
        })
        ran_business = any(
            attempt.status in ("succeeded", "business_error")
            for attempt in attempts
        )
        if not ran_business and not side_effects:
            return "not_required"
        if self._cleanup_executor is None:
            return "skipped"
        try:
            await self._cleanup_executor(
                {
                    "cleanup_rule": case.cleanup_rule,
                    "case_id": case.case_id,
                    "allowed_side_effects": list(case.allowed_side_effects),
                    "side_effects": side_effects,
                }
            )
        except Exception:
            return "failed"
        return "ok"

    # ------------------------------------------------------------------
    # replay
    # ------------------------------------------------------------------

    async def replay(
        self, request: ReplayExperimentRequest
    ) -> ExperimentRunReport:
        """replay 模式：仅对现有 EvidenceSnapshot 重新评分，不调用业务目标。"""
        if not request.source_sample_ids:
            raise EvaluationValidationError(
                "replay 必须提供至少一个源 sample（仅重评现有证据）"
            )
        # 重复源 sample 去重（保序）：同一源只重评一次
        source_ids = list(dict.fromkeys(request.source_sample_ids))
        definitions, metric_names = await self._resolve_definitions(
            request.evaluator_set
        )
        evaluator_keys = self._definition_keys(definitions)
        name = replay_experiment_name(request.name)
        started = await self._service.start_experiment(
            StartExperimentRequest(
                dataset_id=request.dataset_id,
                dataset_version=request.dataset_version,
                dataset_hash=request.dataset_hash,
                target_config=dict(request.target_config),
                evaluator_set=evaluator_keys,
                mode="replay",
                manifest_json="",
            )
        )
        frozen = self._build_frozen_snapshot(
            mode="replay",
            started=started,
            dataset_hash=request.dataset_hash,
            dataset_version=request.dataset_version,
            definitions=definitions,
            seed=None,
            replicates=1,
        )
        logger.info(
            "evaluation experiment replay started: experiment_id=%s "
            "sources=%d metrics=%d",
            started.experiment_id,
            len(source_ids),
            len(metric_names),
        )
        items: list[ExperimentItemRecord] = []
        for source_sample_id in source_ids:
            # 单项异常隔离：源缺失/存储故障记为该项 error，运行继续
            try:
                item = await self._replay_item(
                    experiment_id=started.experiment_id,
                    name=name,
                    source_sample_id=source_sample_id,
                    dataset_id=request.dataset_id,
                    definitions=definitions,
                )
            except EvaluationValidationError:
                raise
            except Exception as exc:  # noqa: BLE001 - 隔离后继续运行
                item = self._isolated_failure_record(
                    f"replay:{source_sample_id}",
                    "",
                    0,
                    exc,
                )
            items.append(item)
            logger.info(
                "evaluation experiment replay item finished: "
                "experiment_id=%s item_id=%s outcome=%s samples=%d",
                started.experiment_id,
                item.item_id,
                item.outcome,
                len(item.sample_ids),
            )
        logger.info(
            "evaluation experiment replay finished: experiment_id=%s "
            "items=%d",
            started.experiment_id,
            len(items),
        )
        return ExperimentRunReport(
            experiment_id=started.experiment_id,
            mode="replay",
            name=name,
            frozen_snapshot=frozen,
            metrics=tuple(metric_names),
            items=tuple(items),
            cleanup_failures=0,
        )

    async def _replay_item(
        self,
        *,
        experiment_id: str,
        name: str,
        source_sample_id: str,
        dataset_id: str,
        definitions: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> ExperimentItemRecord:
        item_id = f"replay:{source_sample_id}"
        # 源 sample 不存在（含拼写错误/已清理）按单项失败隔离，不中止运行
        try:
            source = await self._ledger.load_sample(source_sample_id)
        except EvaluationNotFoundError:
            return ExperimentItemRecord(
                item_id=item_id,
                case_id="",
                replicate=0,
                outcome="error",
                analysis_available=False,
                root_trace_id=None,
                root_span_id=None,
                sample_ids=(),
                attempts=(),
                cleanup_status="not_required",
                failure_reason="EvaluationNotFoundError",
            )
        snapshot_ref = source.get("snapshot_ref") or ""
        if not snapshot_ref:
            # 无证据可重评：明确跳过，不伪造评分、不调用业务目标
            return self._replay_skipped_record(
                item_id, source, SkipReason.evidence_missing.value
            )
        # 存在性检查（has_snapshot）替代整份 2MiB 快照加载
        checker = getattr(self._evidence, "has_snapshot", None)
        if checker is not None:
            snapshot_exists = bool(await checker(snapshot_ref))
        else:
            try:
                await self._evidence.load_snapshot(snapshot_ref)
                snapshot_exists = True
            except Exception:
                snapshot_exists = False
        if not snapshot_exists:
            return self._replay_skipped_record(
                item_id, source, SkipReason.evidence_missing.value
            )
        original_target = source.get("target") or {}
        links = self._original_links(original_target)
        with self._root_scope({
            "experiment_id": experiment_id,
            "experiment_name": name,
            "dataset_id": dataset_id,
            "item_id": item_id,
            "links": links,
        }) as root:
            source_kind = str(original_target.get("kind") or "agent_run")
            scoring_kind = REPLAY_SOURCE_KIND_MAP.get(source_kind)
            if scoring_kind is None:
                raise EvaluationValidationError(
                    f"replay 源 sample 目标类型 {source_kind!r} 不支持自动"
                    f"重评（session_feedback 修订走 feedback 通道）: "
                    f"source_sample_id={source_sample_id}"
                )
            scoring_defs = tuple(
                DefinitionKey(
                    definition_id=definition_id, definition_version=version
                )
                for (definition_id, version), row in definitions.items()
                if row.get("target_kind") == scoring_kind
            )
            submitted = await self._service.submit_sample(
                SubmitSampleRequest(
                    producer_event_id=f"replay:{experiment_id}:{source_sample_id}",
                    service="n-agent",
                    target=EvaluationTarget(
                        service="n-agent",
                        kind="experiment_item",
                        subject_id=item_id,
                        subject_revision=str(
                            original_target.get("subject_revision") or ""
                        ),
                        trace_id=root.trace_id,
                        span_id=root.span_id,
                    ),
                    evidence_targets=tuple(
                        dict(t) for t in source.get("evidence_targets") or ()
                    ),
                    # 指向既有 snapshot：重评不产生新业务证据
                    snapshot_ref=snapshot_ref,
                    snapshot_hash=str(source.get("snapshot_hash") or ""),
                    schema_version=str(source.get("schema_version") or ""),
                    captured_at=self._clock().astimezone(timezone.utc).isoformat(),
                    sampling_policy_version="experiment",
                    definitions=scoring_defs,
                    experiment_id=experiment_id,
                    mode="replay",
                    replicate=0,
                )
            )
            return ExperimentItemRecord(
                item_id=item_id,
                case_id=str(original_target.get("subject_id") or ""),
                replicate=0,
                outcome="replay_rescored",
                analysis_available=bool(root.is_valid),
                root_trace_id=root.trace_id,
                root_span_id=root.span_id,
                sample_ids=(submitted.sample_id,),
                attempts=(),
                cleanup_status="not_required",
            )

    @staticmethod
    def _replay_skipped_record(
        item_id: str,
        source: Mapping[str, Any],
        skip_reason: str,
    ) -> ExperimentItemRecord:
        return ExperimentItemRecord(
            item_id=item_id,
            case_id=str((source.get("target") or {}).get("subject_id") or ""),
            replicate=0,
            outcome="evidence_missing",
            analysis_available=False,
            root_trace_id=None,
            root_span_id=None,
            sample_ids=(),
            attempts=(),
            cleanup_status="not_required",
            skip_reason=skip_reason,
        )

    @staticmethod
    def _original_links(
        original_target: Mapping[str, Any]
    ) -> tuple[tuple[str, str], ...]:
        trace_id = original_target.get("trace_id")
        span_id = original_target.get("span_id")
        if (
            isinstance(trace_id, str)
            and len(trace_id) == 32
            and isinstance(span_id, str)
            and len(span_id) == 16
        ):
            return ((trace_id, span_id),)
        return ()

    # ------------------------------------------------------------------
    # 公共：定义解析 / 冻结快照 / manifest 投影
    # ------------------------------------------------------------------

    async def _resolve_definitions(
        self, evaluator_set: Sequence[DefinitionKey]
    ) -> tuple[dict[tuple[str, str], Mapping[str, Any]], list[str]]:
        if not evaluator_set:
            raise EvaluationValidationError("evaluator_set 不能为空")
        resolved: dict[tuple[str, str], Mapping[str, Any]] = {}
        metric_names: list[str] = []
        for key in evaluator_set:
            definition_id, definition_version = normalize_definition_key(key)
            tag = (definition_id, definition_version)
            if tag in resolved:
                raise EvaluationValidationError(
                    f"evaluator_set 存在重复项: {definition_id}"
                    f"@{definition_version}"
                )
            row = await self._ledger.get_definition(*tag)
            if row.get("target_kind") not in EXPERIMENT_DEFINITION_KINDS:
                raise EvaluationValidationError(
                    f"定义 {definition_id}@{definition_version} "
                    f"target_kind {row.get('target_kind')!r} 不属于实验评分定义 "
                    f"{EXPERIMENT_DEFINITION_KINDS}"
                )
            resolved[tag] = row
            metric_names.append(str(row.get("metric_name") or definition_id))
        return resolved, metric_names

    @staticmethod
    def _split_definitions(
        definitions: Mapping[tuple[str, str], Mapping[str, Any]]
    ) -> tuple[tuple[DefinitionKey, ...], tuple[DefinitionKey, ...]]:
        root_defs: list[DefinitionKey] = []
        retrieval_defs: list[DefinitionKey] = []
        for (definition_id, version), row in definitions.items():
            key = DefinitionKey(definition_id=definition_id, definition_version=version)
            if row.get("target_kind") == "retrieval":
                retrieval_defs.append(key)
            else:
                root_defs.append(key)
        return tuple(root_defs), tuple(retrieval_defs)

    def _build_frozen_snapshot(
        self,
        *,
        mode: str,
        started: Any,
        dataset_hash: str | None,
        dataset_version: str | None,
        definitions: Mapping[tuple[str, str], Mapping[str, Any]],
        seed: str | None,
        replicates: int,
    ) -> dict[str, Any]:
        """冻结快照（spec 9.1）：环境指纹缺失项标 external_unpinned。

        指纹注入仅接受可调用对象（延迟求值，避免构造期快照）；应用层
        契约要求指纹值为非空字符串，非字符串值视为契约违规直接报错。
        结构化值的哈希折叠由基础设施指纹器负责（见
        target_runner.build_frozen_environment），不在本层伪造兼容。
        Judge 配置 hash 直接使用 ``_resolve_definitions`` 已解析的定义行，
        不重复查询账本。"""
        env: Mapping[str, Any] = {}
        if self._environment_fingerprint is not None:
            env = dict(self._environment_fingerprint() or {})
        snapshot: dict[str, Any] = {}
        for field_name in FROZEN_ENVIRONMENT_FIELDS:
            value = env.get(field_name)
            if value is not None and not isinstance(value, str):
                raise EvaluationValidationError(
                    f"环境指纹字段 {field_name} 必须为字符串或缺失，"
                    f"得到 {type(value).__name__}；结构化值应先由基础设施"
                    "指纹器折叠为哈希"
                )
            snapshot[field_name] = (
                value if value not in (None, "") else EXTERNAL_UNPINNED
            )
        judge_entries = []
        for row in definitions.values():
            definition = row.get("definition") or {}
            judge_entries.append(
                {
                    "definition_id": row.get("definition_id"),
                    "version": row.get("version"),
                    "judge_model": definition.get("judge_model"),
                    "judge_params": dict(definition.get("judge_params") or {}),
                }
            )
        # 稳定键序：judge hash 不依赖解析顺序
        judge_entries.sort(key=lambda entry: (entry["definition_id"], entry["version"]))
        snapshot.update(
            {
                "mode": mode,
                "evaluator_set_hash": started.evaluator_set_hash,
                "target_config_hash": started.target_config_hash,
                "judge_config_hash": hashlib.sha256(
                    canonical_json_bytes(judge_entries)
                ).hexdigest(),
                "dataset_hash": (
                    dataset_hash if dataset_hash else EXTERNAL_UNPINNED
                ),
                "dataset_version": (
                    dataset_version
                    if dataset_version
                    else EXTERNAL_UNPINNED
                ),
                "seed": seed,
                "replicate_count": replicates,
            }
        )
        return snapshot

    @staticmethod
    def _manifest_json(dataset: DatasetManifest) -> str:
        return canonical_json_bytes(
            {
                "schema_version": dataset.schema_version,
                "dataset_id": dataset.dataset_id,
                "dataset_version": dataset.dataset_version,
                "dataset_hash": dataset.dataset_hash,
                "target_kind": dataset.target_kind,
                "case_ids": [case.case_id for case in dataset.cases],
            }
        ).decode("utf-8")


# ---------------------------------------------------------------------------
# 纯函数辅助（case 输入投影 / 质量口径）
# ---------------------------------------------------------------------------


def normalize_definition_key(key: Any) -> tuple[str, str]:
    """定义引用归一化：DefinitionKey 或 (definition_id, version) 二元组。"""
    if isinstance(key, DefinitionKey):
        return key.definition_id, key.definition_version
    if isinstance(key, tuple) and len(key) == 2 and all(
        isinstance(part, str) and part for part in key
    ):
        return key[0], key[1]
    raise EvaluationValidationError(
        f"非法定义引用（须为 DefinitionKey 或 (definition_id, version)）: {key!r}"
    )


def experiment_item_id(case_id: str, replicate: int) -> str:
    """实验项 ID = case_id + replicate（分隔符固定 ``#r``，replicate 从 0 起，
    与账本 job 的 replicate 整数一致）。"""
    return f"{case_id}#r{replicate}"


def session_namespace(experiment_id: str, case_id: str) -> str:
    """每 case 独立会话 namespace（含实验身份，跨实验隔离）。"""
    return f"eval-experiment-{experiment_id}-{case_id}"


def replay_experiment_name(name: str) -> str:
    """replay 实验名称标记：未带 replay 后缀的名称强制追加。"""
    if not name:
        raise EvaluationValidationError("实验名称不能为空")
    return name if name.endswith("-replay") else f"{name}-replay"


def _scenario_file_paths(case: DatasetCase) -> tuple[str, ...]:
    """场景 case 声明的工作区文件路径（file_content 断言投影，T10）。

    适配器按此清单收集文件证据（受控端口安全读取），证据读取后按
    实际 hash 登记资源 intent；非场景 case 返回空。"""
    if case.scenario is None:
        return ()
    paths: list[str] = []
    for assertion in case.scenario.assertions:
        if assertion.assertion_type != "file_content":
            continue
        filename = assertion.params.get("filename")
        if isinstance(filename, str) and filename and filename not in paths:
            paths.append(filename)
    return tuple(paths)


def extract_turns(case: DatasetCase) -> tuple[Mapping[str, Any], ...]:
    """case 输入投影：多轮 case（input.turns 数组）共享会话；单轮 case 的
    整个 input 视为一轮。"""
    turns = case.input.get("turns")
    if turns is not None:
        if not isinstance(turns, Sequence) or isinstance(turns, (str, bytes)):
            raise EvaluationValidationError(
                f"case {case.case_id!r} input.turns 必须是数组"
            )
        if not turns:
            raise EvaluationValidationError(
                f"case {case.case_id!r} input.turns 不能为空"
            )
        return tuple(
            dict(turn) if isinstance(turn, Mapping) else {"message": turn}
            for turn in turns
        )
    return (dict(case.input),)


def qrels_entries(qrels: Mapping[str, int] | None) -> list[dict[str, Any]]:
    """qrels 身份键 -> 快照条目（worker kb 指标构建器的输入形态）。"""
    if not qrels:
        return []
    entries: list[dict[str, Any]] = []
    for identity, grade in qrels.items():
        corpus_revision, document_revision, chunk_id = identity.split(":", 2)
        entries.append(
            {
                "corpus_revision": corpus_revision,
                "document_revision": document_revision,
                "chunk_id": chunk_id,
                "grade": int(grade),
            }
        )
    return entries


def summarize_quality(
    report: ExperimentRunReport,
    dataset: DatasetManifest | None,
    *,
    evaluation_outcomes: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[MetricQualitySummary, ...]:
    """实验质量口径摘要（spec 9.2 分母纪律，T14 门禁在此基础上结合账本
    分数与阈值计算结论）。

    - 分母 = manifest 预声明适用的实验项（含业务 error、传输耗尽、证据
      缺失——运行后发生的问题不从分母剔除）；no_answer case 仅适用声明的
      no_answer 指标。
    - 业务 error 计入 business_failures（task_success 失败分母）。
    - Judge error 由 evaluation_outcomes（T14 从账本读取）传入，单列计数，
      不混入业务失败、不从分母剔除。
    - replay 报告（dataset=None）只按 item 结局计数，不引用 manifest 适用性。
    """
    outcomes = evaluation_outcomes or {}
    metrics = report.metrics
    summaries: dict[str, MetricQualitySummary] = {}
    for metric_name in metrics:
        summaries[metric_name] = MetricQualitySummary(
            metric_name=metric_name,
            denominator=0,
            business_failures=0,
            judge_errors=0,
            evaluated=0,
        )
    for item in report.items:
        applicable: list[str]
        if dataset is not None:
            case = next(
                (c for c in dataset.cases if c.case_id == item.case_id), None
            )
            applicable = (
                [m for m in metrics if metric_applicable(case, m)]
                if case is not None
                else list(metrics)
            )
        else:
            applicable = list(metrics)
        for metric_name in applicable:
            if metric_name not in summaries:
                continue
            summary = summaries[metric_name]
            judge_error = (outcomes.get(item.item_id) or {}).get(metric_name)
            summary = replace(
                summary,
                denominator=summary.denominator + 1,
                business_failures=summary.business_failures + (
                    1 if item.outcome == "business_error" else 0
                ),
                judge_errors=summary.judge_errors + (
                    1 if judge_error == "judge_error" else 0
                ),
                evaluated=summary.evaluated + (
                    1
                    if item.outcome in ("scored", "replay_rescored")
                    and item.sample_ids
                    else 0
                ),
            )
            summaries[metric_name] = summary
    return tuple(summaries.values())
