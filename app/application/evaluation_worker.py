# app/application/evaluation_worker.py
"""EvaluationWorker -- 评估作业执行器（plan T6，spec 5.1 / 6.4 / 13）。

职责：claim（lease 领取 + 续租）→ 预算预留 → evaluator 执行（按定义
method 分派）→ 结果提交（result + 终态 job + deliveries 单事务）。不通过
Task Goal Mode 循环调度；依赖全部构造注入（store / evaluator /
evidence_store / sinks / clock），不 import app.infrastructure（DDD 依赖
方向，knowledge/03-conventions），不 import HTTP/SDK。

生命周期（对齐 app/application/task_runner.py 房规）：
- ``start()`` 启动常驻轮询循环：claim → 派发 asyncio.Task（fire-and-forget
  + 异常隔离），LLM method 的执行受 ``asyncio.Semaphore`` 限并发（默认 1，
  C 类上限 4）；无 job 时按 poll_interval 等待 stop 信号；
- ``stop()`` 停止领取 + 有界等待在途任务（shutdown_grace），超时取消；
- ``run_once()`` 同步领取并处理一个 job（测试与手动驱动入口）；
- ``claim()`` / ``process(job)`` / ``heartbeat(claim)`` 暴露分步原语。
不接入 main.py lifespan（T20 组合根接线）。

重试与期限（spec 6.4）：
- 每次 evaluator 调用写一行 attempt（请求/实际模型/usage/cost/起止/
  error_type），重试不覆盖旧行；
- 网络/限流/超时最多额外重试 2 次，结构化输出格式错误最多修复 1 次，
  共享 3 次调用总上限（按已结束 attempt 行计数，crash 后重领不放大额度）；
  重试类别互斥——由首次可重试失败的类别锁定，任一类额度消耗后另一类
  不得再触发重试；
- 单调用超时取 min(definition.timeout_seconds, job 剩余期限)；
  重试退避 + 下一次调用完整超时的预测值越过 job 总期限（默认 180s）时，
  job 以 error 终结并保留费用与 attempts；
- 低分（boolean False / 低于阈值数值）不重试；取消后即使 evaluator 返回
  也不提交 succeeded（cancel 清空 lease token，提交 fail closed）。

预算（spec 13，第三评审补充）：
- 领取后在 evaluation_budget_ledger 事务预留，预留量覆盖该 job 全部重试
  （3 次调用）；默认全部禁用（无任何上限判定）；
- 启用 USD 日预算必须显式提供单次成本估价（无定价能力的部署必须改用
  token/call ceiling，不视为免费）；启用 token 上限同理必须提供单次
  token 估价；
- deterministic evaluator 不消耗预算（不预留不结算）；并发争抢由存储层
  BEGIN IMMEDIATE 串行化，超限方 job 进入 skipped(budget_exhausted)，
  不调用 Judge；
- job 终态后结算：已知费用按实际计入并释放预留；费用未知（usage 缺失/
  崩溃未结算）按保守语义将整个预留计入消耗，限额永不低估。

attempt 行语义（判定记录）：claim 在 attempts 表追加的 index-0 行是
claim marker（证明"领取过但可能未调用"）；每次真实调用经
``append_call_attempt`` 追加独立完整行。attempt 总上限按"已结束调用行"
计数，claim marker 不消耗调用额度。迟到 worker（丢失租约）的调用留痕
照常追加（费用证据），但其结果提交被 lease CAS 拒绝。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol, Sequence

from app.application.evaluation_service import definition_from_mapping
from app.domain.evaluation.errors import (
    EvaluationBudgetExhaustedError,
    EvaluationDeletionConflictError,
    EvaluationLeaseError,
)
from app.domain.evaluation.metrics_deterministic import (
    ChunkIdentity,
    SideEffectEvidence,
    TaskSuccessInput,
    ToolCallRecord,
    ToolContractInput,
    agent_task_success,
    agent_tool_contract,
    kb_mrr_at_5,
    kb_ndcg_at_5,
    kb_recall_at_5,
)
from app.domain.evaluation.models import (
    EXPLANATION_MAX_BYTES,
    EvaluatorDefinition,
    EvidenceSnapshot,
    EvaluationValue,
    EvaluationValidationError,
    validate_evaluation_value,
)
from app.domain.evaluation.state import EvaluationJobStatus, SkipReason

logger = logging.getLogger(__name__)

# spec 6.4：单 job 的 Judge 调用总上限 3 次；网络/限流/超时最多额外重试
# 2 次；格式错误最多修复 1 次；两类共享总上限
MAX_TOTAL_ATTEMPTS: int = 3
MAX_TRANSIENT_RETRIES: int = 2
MAX_FORMAT_REPAIRS: int = 1

# 可重试的瞬态错误分类（T7 适配器以 .error_type 属性承载分类标签）
TRANSIENT_ERROR_TYPES: frozenset[str] = frozenset(
    {"network", "rate_limit", "timeout"}
)

DEFAULT_LEASE_SECONDS: int = 180
DEFAULT_JOB_TOTAL_DEADLINE_SECONDS: float = 180.0
DEFAULT_LLM_CONCURRENCY: int = 1
# llm_concurrency 上限单一来源：domain validate_evaluation_config
# （C 并发 <= B 类部署上限 evaluation_worker_max_concurrency），本文件
# 不再保留独立数值上限（避免 B_max > 旧 4 上限时 C∈5..B_max 的合法配置
# 在两层校验间被缝隙拒绝）；worker 构造只要求 >= 1。
DEFAULT_CLAIMANT: str = "evaluation-worker"


class EvaluationWorkerError(Exception):
    """评估 worker 用例层错误。"""


class _JudgeFormatError(EvaluationWorkerError):
    """evaluator 输出不符合定义契约（按 format_error 分类，最多修复 1 次）。"""


class EvaluationWorkerStore(Protocol):
    """worker 所需的账本存储契约（T3 SQLiteEvaluationStore + T6 扩展）。

    Application 只依赖本 Protocol 形状，不依赖 Infrastructure 实现。"""

    async def claim_next_job(
        self, claimant: str, lease_seconds: int = 180, *, now: Any = None
    ) -> Mapping[str, Any] | None: ...

    async def heartbeat_job(
        self, job_id: str, lease_token: str, *, lease_seconds: int, now: Any = None
    ) -> bool: ...

    async def cancel_job(self, job_id: str, *, now: Any = None) -> bool: ...

    async def fail_job(
        self,
        job_id: str,
        *,
        lease_token: str,
        error_type: str | None = None,
        now: Any = None,
    ) -> bool: ...

    async def append_call_attempt(
        self, job_id: str, **fields: Any
    ) -> Mapping[str, Any]: ...

    async def get_job(self, job_id: str) -> Mapping[str, Any]: ...

    async def get_definition(
        self, definition_id: str, version: str
    ) -> Mapping[str, Any]: ...

    async def load_sample(self, sample_id: str) -> Mapping[str, Any]: ...

    async def list_attempts(self, job_id: str) -> list[Mapping[str, Any]]: ...

    async def commit_result_with_deliveries(
        self,
        result: Mapping[str, Any],
        deliveries: Sequence[Mapping[str, Any]] = (),
        *,
        lease_token: str | None = None,
        job_terminal_status: str | None = None,
        now: Any = None,
    ) -> str: ...

    async def reserve_daily_budget(
        self,
        date_key: str,
        *,
        usd: Any = Decimal("0"),
        tokens: int = 0,
        calls: int = 0,
        max_usd: Any = None,
        max_tokens: int | None = None,
        max_calls: int | None = None,
    ) -> Mapping[str, Any]: ...

    async def settle_daily_budget(
        self,
        date_key: str,
        *,
        usd: Any = Decimal("0"),
        tokens: int = 0,
        calls: int = 0,
        release_usd: Any = Decimal("0"),
        release_tokens: int = 0,
        release_calls: int = 0,
    ) -> Mapping[str, Any]: ...


class EvidenceSnapshotLoader(Protocol):
    """证据快照读取端口（EvidenceStore.load_snapshot 的最小形状）。"""

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot: ...


class EvaluatorPort(Protocol):
    """evaluator 执行端口（Domain ``Evaluator`` 协议的最小形状）。

    返回受控输出 Mapping：value / status("value"|"skipped") / skip_reason /
    explanation / model（实际模型）/ usage{input,output,total_tokens} /
    cost_usd。异常携带 ``error_type`` 属性时按该分类计（缺省 network）。"""

    async def evaluate(
        self, definition: EvaluatorDefinition, snapshot: EvidenceSnapshot | None
    ) -> Mapping[str, Any]: ...


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断（回退到完整字符边界，不产生残缺字节序列）。"""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# deterministic 指标构建器（metric_name -> snapshot content -> EvaluationValue）
# ---------------------------------------------------------------------------


def _missing_reference_value() -> EvaluationValue:
    return EvaluationValue(
        typed_value=None,
        status="skipped",
        skip_reason=SkipReason.missing_reference,
        explanation="snapshot 未携带 qrels（缺参考答案）",
    )


def _kb_metric_value(metric: str) -> Callable[[Mapping[str, Any]], EvaluationValue]:
    """kb.* 三个检索指标的快照内容构建器（qrels 缺失 → missing_reference）。"""

    def build(content: Mapping[str, Any]) -> EvaluationValue:
        qrels_raw = content.get("qrels")
        if qrels_raw is None:
            return _missing_reference_value()
        qrels = {
            ChunkIdentity(
                corpus_revision=e["corpus_revision"],
                document_revision=e["document_revision"],
                chunk_id=e["chunk_id"],
            ): int(e["grade"])
            for e in qrels_raw
        }
        results = [
            ChunkIdentity(
                corpus_revision=e["corpus_revision"],
                document_revision=e["document_revision"],
                chunk_id=e["chunk_id"],
            )
            for e in content.get("results") or ()
        ]
        if metric == "kb.recall_at_5":
            return kb_recall_at_5(results, qrels)
        if metric == "kb.ndcg_at_5":
            return kb_ndcg_at_5(results, qrels)
        return kb_mrr_at_5(results, qrels)

    return build


def _task_success_value(content: Mapping[str, Any]) -> EvaluationValue:
    side_effect_raw = content.get("side_effect_evidence")
    return agent_task_success(
        TaskSuccessInput(
            completion_reported=bool(content.get("completion_reported", False)),
            side_effect_evidence=(
                SideEffectEvidence(
                    expected=tuple(side_effect_raw.get("expected") or ()),
                    verified=tuple(side_effect_raw.get("verified") or ()),
                )
                if side_effect_raw is not None
                else None
            ),
        )
    )


def _tool_contract_value(content: Mapping[str, Any]) -> EvaluationValue:
    # 无任何工具证据槽（24 项三方审阅 #17）：空集合默认会让契约对缺失
    # 证据虚假通过——无证据按 skipped(evidence_missing) 处理（spec 8）
    if "tool_calls" not in content and "tool_traces" not in content:
        return EvaluationValue(
            typed_value=None,
            status="skipped",
            skip_reason=SkipReason.evidence_missing,
            explanation="no tool evidence slots in snapshot",
        )
    calls = tuple(
        ToolCallRecord(
            tool=c["tool"],
            schema_valid=bool(c["schema_valid"]),
            permission_granted=bool(c.get("permission_granted", True)),
            side_effects=tuple(c.get("side_effects") or ()),
        )
        for c in content.get("tool_calls") or ()
    )
    return agent_tool_contract(
        ToolContractInput(
            calls=calls,
            allowed_tools=tuple(content.get("allowed_tools") or ()),
            expected_side_effects=tuple(
                content.get("expected_side_effects") or ()
            ),
        )
    )


_DETERMINISTIC_HANDLERS: dict[str, Callable[[Mapping[str, Any]], EvaluationValue]] = {
    "kb.recall_at_5": _kb_metric_value("kb.recall_at_5"),
    "kb.ndcg_at_5": _kb_metric_value("kb.ndcg_at_5"),
    "kb.mrr_at_5": _kb_metric_value("kb.mrr_at_5"),
    "agent.task_success": _task_success_value,
    "agent.tool_contract": _tool_contract_value,
}


class EvaluationWorker:
    """评估作业执行器（claim / lease / 预算预留 / 执行 / 结果提交）。"""

    def __init__(
        self,
        store: EvaluationWorkerStore,
        *,
        evaluator: EvaluatorPort,
        evidence_store: EvidenceSnapshotLoader,
        sinks: Sequence[Mapping[str, str]] = (),
        claimant: str = DEFAULT_CLAIMANT,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        heartbeat_interval_seconds: float | None = 30.0,
        poll_interval_seconds: float = 5.0,
        job_total_deadline_seconds: float = DEFAULT_JOB_TOTAL_DEADLINE_SECONDS,
        retry_backoff_seconds: float = 1.0,
        llm_concurrency: int = DEFAULT_LLM_CONCURRENCY,
        shutdown_grace_seconds: float = 30.0,
        calibration_status_provider: (
            Callable[[EvaluatorDefinition], Any] | None
        ) = None,
        daily_budget_usd: Decimal | str | None = None,
        per_call_cost_estimate: Decimal | str | None = None,
        daily_token_ceiling: int | None = None,
        per_call_token_estimate: int | None = None,
        daily_call_ceiling: int | None = None,
        daily_call_ceiling_provider: Callable[[], int | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(llm_concurrency, int) or isinstance(
            llm_concurrency, bool
        ):
            raise ValueError("llm_concurrency 必须是整数")
        if llm_concurrency < 1:
            raise ValueError(
                f"llm_concurrency 必须 >= 1（上限由 domain 配置校验单一"
                f"来源）: {llm_concurrency}"
            )
        if daily_budget_usd is not None and per_call_cost_estimate is None:
            raise ValueError(
                "启用 USD 日预算必须提供 per_call_cost_estimate；"
                "无定价能力时必须显式指定 token/call ceiling（不视为免费）"
            )
        if daily_token_ceiling is not None and per_call_token_estimate is None:
            raise ValueError(
                "启用 token 日上限必须提供 per_call_token_estimate；"
                "无定价能力时必须显式指定 token/call ceiling（不视为免费）"
            )
        for spec in sinks:
            if not spec.get("sink") or not spec.get("projection_version"):
                raise ValueError(
                    f"sink 配置需要 sink 与 projection_version: {dict(spec)!r}"
                )
        if (
            heartbeat_interval_seconds is not None
            and heartbeat_interval_seconds >= lease_seconds
        ):
            raise ValueError(
                "heartbeat_interval_seconds 必须小于 lease_seconds"
                "（否则续租永远赶不上租约过期）: "
                f"heartbeat={heartbeat_interval_seconds} lease={lease_seconds}"
            )
        self._store = store
        self._evaluator = evaluator
        self._evidence_store = evidence_store
        self._sinks: tuple[Mapping[str, str], ...] = tuple(
            {"sink": s["sink"], "projection_version": s["projection_version"]}
            for s in sinks
        )
        self._claimant = claimant
        self._lease_seconds = lease_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._poll_interval = poll_interval_seconds
        self._job_total_deadline = job_total_deadline_seconds
        self._retry_backoff = retry_backoff_seconds
        self._shutdown_grace = shutdown_grace_seconds
        self._llm_concurrency = llm_concurrency
        self._llm_semaphore = asyncio.Semaphore(llm_concurrency)
        self._daily_budget_usd = (
            Decimal(str(daily_budget_usd)) if daily_budget_usd is not None else None
        )
        self._per_call_cost_estimate = (
            Decimal(str(per_call_cost_estimate))
            if per_call_cost_estimate is not None
            else None
        )
        self._daily_token_ceiling = daily_token_ceiling
        self._per_call_token_estimate = per_call_token_estimate
        self._daily_call_ceiling = daily_call_ceiling
        # C 类日调用上限热读 provider（#11）：每次预留时读取当前 C 类配置
        # （Dashboard 编辑+热重载语义）；None 时退回构造期静态值（并发
        # 仍为启动期绑定，见 Dashboard 标签"重启后生效"）
        self._daily_call_ceiling_provider = daily_call_ceiling_provider
        self._clock = clock or _default_clock
        # 校准状态查询（T8）：async 可调用，返回 "passed" / 其他（含
        # None）。None = 未接校准状态 → llm_judge 结果一律按 advisory
        # （provenance=calibration）落库，fail-safe（忘接线不静默放行，
        # 对齐 T9 m12 模式）。
        self._calibration_status_provider = calibration_status_provider
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 公共属性 / 原语
    # ------------------------------------------------------------------

    @property
    def llm_concurrency(self) -> int:
        return self._llm_concurrency

    async def claim(self) -> Mapping[str, Any] | None:
        """领取下一个可执行 job（含过期租约重领），返回含 lease_token 的
        job 视图；无可领取 job 返回 None。"""
        return await self._store.claim_next_job(
            self._claimant, self._lease_seconds, now=self._clock()
        )

    async def heartbeat(self, claim: Mapping[str, Any]) -> bool:
        """按 claim 携带的 lease token 续租；丢失租约返回 False。"""
        return await self._store.heartbeat_job(
            claim["job_id"],
            claim["lease_token"],
            lease_seconds=self._lease_seconds,
            now=self._clock(),
        )

    async def process(self, job: Mapping[str, Any]) -> None:
        """处理一个已领取的 job（含预算预留/执行/提交/结算），异常只记录
        不上抛（fire-and-forget 隔离语义，与循环派发路径一致）；意外异常
        兜底把 job 置为 error 终态，避免悬挂 running 被反复重领。"""
        try:
            definition = await self._load_definition(job)
            if definition.method == "llm_judge":
                async with self._llm_semaphore:
                    await self._execute_claimed(job, definition)
            else:
                await self._execute_claimed(job, definition)
        except Exception:
            logger.exception(
                "evaluation job processing failed: job=%s", job.get("job_id")
            )
            await self._fail_best_effort(job)

    async def _fail_best_effort(self, job: Mapping[str, Any]) -> None:
        """意外异常兜底终结：CAS 限定最新租约，丢失租约或已终态时不动
        （新持有者/已落终态的决定优先），不向调用方传播失败。"""
        token = job.get("lease_token")
        job_id = job.get("job_id")
        if not token or not job_id:
            return
        try:
            updated = await self._store.fail_job(
                job_id,
                lease_token=token,
                error_type="worker_error",
                now=self._clock(),
            )
        except Exception:
            logger.exception(
                "evaluation best-effort fail failed: job=%s", job_id
            )
            return
        if updated:
            logger.warning(
                "evaluation job terminated by worker error: job=%s", job_id
            )

    async def run_once(self) -> str | None:
        """领取并同步处理一个 job；返回 job_id，无可领取 job 返回 None。"""
        job = await self.claim()
        if job is None:
            return None
        await self.process(job)
        return job["job_id"]

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动常驻轮询循环（幂等）。"""
        if self._loop_task is not None:
            return
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._loop())
        logger.info(
            "EvaluationWorker started (claimant=%s poll=%ss llm_concurrency=%s)",
            self._claimant,
            self._poll_interval,
            self._llm_concurrency,
        )

    async def stop(self) -> None:
        """停止领取 + 有界等待在途 job（超时取消），幂等。"""
        if self._stop_event is not None:
            self._stop_event.set()
        loop_task = self._loop_task
        self._loop_task = None
        if loop_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(loop_task),
                    timeout=self._poll_interval + self._shutdown_grace + 1,
                )
            except asyncio.TimeoutError:
                loop_task.cancel()
                # 与 delivery_worker.stop / deletion 服务 stop 对齐：取消后
                # 必须 await 回收，防 unretrieved task exception 噪声
                await asyncio.gather(loop_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("EvaluationWorker loop stopped with error")
        inflight = [t for t in self._inflight if not t.done()]
        if inflight:
            await asyncio.wait(
                inflight, timeout=self._shutdown_grace
            )
            for task in inflight:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*inflight, return_exceptions=True)

    async def close_evaluator(self) -> None:
        """释放 evaluator 持有的外部资源（T20/m2：HTTP 客户端生命周期
        归组合根管理，经本公共方法收口而非私有属性穿透；evaluator 无
        aclose 能力时幂等无害）。本方法只负责 evaluator 资源释放，
        worker 停止职责由 stop() 承担。"""
        aclose = getattr(self._evaluator, "aclose", None)
        if aclose is None:
            return
        await aclose()

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            # 领取上限（#10）：在途任务数达到执行并发上限时暂停领取——
            # 否则持续 claim 会把 job 租约耗在信号量排队上（心跳在
            # _execute_claimed 内才启动），租约过期被反复重领。
            if len(self._inflight) >= self._llm_concurrency:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            job: Mapping[str, Any] | None = None
            try:
                job = await self.claim()
            except Exception:
                logger.exception("evaluation job claim failed")
            if job is not None:
                task = asyncio.create_task(self.process(job))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
                continue
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------
    # 单 job 执行
    # ------------------------------------------------------------------

    async def _load_definition(self, job: Mapping[str, Any]) -> EvaluatorDefinition:
        row = await self._store.get_definition(
            job["definition_id"], job["definition_version"]
        )
        return definition_from_mapping(row["definition"])

    async def _load_snapshot(self, job: Mapping[str, Any]) -> EvidenceSnapshot | None:
        sample = await self._store.load_sample(job["sample_id"])
        ref = sample.get("snapshot_ref") or ""
        if not ref:
            return None
        try:
            return await self._evidence_store.load_snapshot(ref)
        except Exception:
            logger.warning(
                "evaluation snapshot load failed (treated as missing): "
                "job=%s ref=%s",
                job.get("job_id"),
                ref,
                exc_info=True,
            )
            return None

    async def _execute_claimed(
        self, job: Mapping[str, Any], definition: EvaluatorDefinition
    ) -> None:
        job_id = job["job_id"]
        token = job["lease_token"]
        if definition.method == "human":
            # 人评不经作业执行（result 走 submit_feedback 用例）
            await self._fail(
                job, token, "human_method_not_executable", definition=definition
            )
            return
        snapshot = await self._load_snapshot(job)
        # evaluation.run Trace（#14 生产接线，spec 12.1）：整个 job 执行
        # （含 Judge LLM 子调用与结果提交）在独立 evaluation.run Trace 内；
        # 样本目标携带真实 trace_id/span_id 时经 Link 指向业务目标。
        # emit_result 在作用域内调用，evaluation_trace_id/span_id 自动取
        # 当前上下文，无需参数穿线。OTel 禁用时为 no-op 句柄。
        sample_target, sample_mode = await self._sample_target_and_mode(job)
        from app.application.evaluation_telemetry import (
            MetricProfileRegistry,
            evaluation_run_scope,
        )

        metric_profile = MetricProfileRegistry.profile(
            definition.definition_id, definition.version
        )
        with evaluation_run_scope(
            sample_target,
            metric_profile=metric_profile,
            mode=sample_mode,
        ) as _run_handle:
            await self._execute_scoped(job, definition, snapshot)
        # scope 结束后 handle.trace_id 仍可读（span 已 end）；评估 Trace
        # 身份已由提交路径经 current_evaluation_ids() 冻结到 result 行
        #（#12 OTel 投影 evaluation_trace 来源），句柄仅备诊断。

    async def _sample_target_and_mode(
        self, job: Mapping[str, Any]
    ) -> tuple[Any, str]:
        sample_id = job.get("sample_id")
        if not sample_id:
            return None, "online"
        try:
            sample = await self._store.load_sample(sample_id)
        except Exception:
            logger.warning(
                "evaluation sample load for trace scope failed: job=%s",
                job.get("job_id"),
                exc_info=True,
            )
            return None, "online"
        return sample.get("target") or None, str(sample.get("mode") or "online")

    async def _execute_scoped(
        self,
        job: Mapping[str, Any],
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
    ) -> None:
        job_id = job["job_id"]
        token = job["lease_token"]
        date_key = self._clock().date().isoformat()
        # C 类日调用上限同读数贯通预留量与预留上限（#11 热重载；同一
        # 次执行内读一次，避免两次读取间配置翻转造成量/限不一致）
        call_ceiling = self._current_call_ceiling()
        reservation = self._budget_reservation(
            definition, call_ceiling=call_ceiling
        )
        # 预留仅在 reserve_daily_budget 真正成功后才有结算资格：预留被拒
        # （预算耗尽）路径账本未变，绝不能进入保守结算（C1）
        reserved = False
        if reservation is not None:
            reserved = await self._reserve_budget(
                job, date_key, reservation, call_ceiling=call_ceiling
            )
            if not reserved:
                await self._commit_budget_skipped(job, definition, snapshot)
                return
        heartbeat_task: asyncio.Task | None = None
        if self._heartbeat_interval is not None:
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(job_id, token)
            )
        try:
            value, judge_provenance = await self._evaluate_claimed(
                job, definition, snapshot
            )
            if value is None:
                return  # skipped 已提交，或 error 终态已由 _fail 落库
            await self._commit_if_still_running(
                job, definition, snapshot, value, judge_provenance
            )
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                outcomes = await asyncio.gather(
                    heartbeat_task, return_exceptions=True
                )
                for outcome in outcomes:
                    if isinstance(outcome, BaseException) and not isinstance(
                        outcome, asyncio.CancelledError
                    ):
                        logger.warning(
                            "evaluation heartbeat task failed: job=%s",
                            job_id,
                            exc_info=outcome,
                        )
            if reservation is not None and reserved:
                try:
                    await self._settle_budget(job_id, date_key, reservation)
                except Exception:
                    logger.exception(
                        "evaluation budget settlement failed: job=%s", job_id
                    )

    async def _evaluate_claimed(
        self,
        job: Mapping[str, Any],
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
    ) -> tuple[EvaluationValue | None, Mapping[str, Any] | None]:
        """按定义 method 执行评估；返回 (value, judge_provenance)。
        value 为 None 表示已直接落终态（skipped 提交或 error 终结），
        调用方无需再提交；judge_provenance 是 Judge 适配器输出携带的
        provenance 信息（self_judge 等），供结果行 provenance 判定。"""
        job_id = job["job_id"]
        token = job["lease_token"]
        if definition.method == "llm_judge" and snapshot is None:
            await self._commit_result(
                job,
                definition,
                snapshot,
                EvaluationValue(
                    typed_value=None,
                    status="skipped",
                    skip_reason=SkipReason.evidence_missing,
                    explanation="evidence snapshot unavailable for llm judge",
                ),
                terminal_status=EvaluationJobStatus.skipped.value,
                provenance=self._skip_provenance(definition),
            )
            return None, None
        if definition.method == "deterministic":
            try:
                return self._evaluate_deterministic(definition, snapshot), None
            except Exception:
                # 快照内容与指标契约不符等：error 终结，不留悬挂 running
                logger.exception(
                    "deterministic evaluation failed: job=%s", job_id
                )
                await self._fail(
                    job, token, "deterministic_error",
                    definition=definition, snapshot=snapshot,
                )
                return None, None
        return await self._evaluate_with_retries(job, definition, snapshot)

    async def _reserve_budget(
        self,
        job: Mapping[str, Any],
        date_key: str,
        reservation: tuple[Decimal, int, int],
        *,
        call_ceiling: int | None,
    ) -> bool:
        """claim 后事务预留（覆盖全部重试）；预留被拒返回 False（账本
        未变，调用方直接提交 skipped(budget_exhausted)，不得结算）。
        call_ceiling 与预留量同源（_current_call_ceiling 同一读数）。"""
        usd_r, tok_r, call_r = reservation
        try:
            await self._store.reserve_daily_budget(
                date_key,
                usd=usd_r,
                tokens=tok_r,
                calls=call_r,
                max_usd=self._daily_budget_usd,
                max_tokens=self._daily_token_ceiling,
                max_calls=call_ceiling,
            )
        except EvaluationBudgetExhaustedError:
            logger.info(
                "evaluation job budget exhausted: job=%s date=%s",
                job["job_id"],
                date_key,
            )
            return False
        return True

    async def _commit_budget_skipped(
        self,
        job: Mapping[str, Any],
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
    ) -> None:
        await self._commit_result(
            job,
            definition,
            snapshot,
            EvaluationValue(
                typed_value=None,
                status="skipped",
                skip_reason=SkipReason.budget_exhausted,
                explanation="daily evaluation budget exhausted",
            ),
            terminal_status=EvaluationJobStatus.skipped.value,
            provenance=self._skip_provenance(definition),
        )

    async def _commit_if_still_running(
        self,
        job: Mapping[str, Any],
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
        value: EvaluationValue,
        judge_provenance: Mapping[str, Any] | None = None,
    ) -> None:
        """取消检查 + 提交：cancel 清空 lease token，即使 evaluator 已返回
        也不得提交 succeeded；已发生的费用与 attempts 保留。"""
        job_id = job["job_id"]
        current = await self._store.get_job(job_id)
        if current["status"] != EvaluationJobStatus.running.value:
            logger.info(
                "evaluation job no longer running (status=%s), discard "
                "result: job=%s",
                current["status"],
                job_id,
            )
            return
        provenance = await self._resolve_provenance(
            definition, judge_provenance
        )
        await self._commit_result(
            job,
            definition,
            snapshot,
            value,
            terminal_status=(
                EvaluationJobStatus.succeeded.value
                if value.status == "value"
                else EvaluationJobStatus.skipped.value
            ),
            provenance=provenance,
        )

    async def _heartbeat_loop(self, job_id: str, lease_token: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                renewed = await self._store.heartbeat_job(
                    job_id,
                    lease_token,
                    lease_seconds=self._lease_seconds,
                    now=self._clock(),
                )
                if not renewed:
                    return  # 丢失租约：停止续租，提交将被 CAS 拒绝
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # 重试执行（spec 6.4）
    # ------------------------------------------------------------------

    async def _evaluate_with_retries(
        self,
        job: Mapping[str, Any],
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
    ) -> tuple[EvaluationValue | None, Mapping[str, Any] | None]:
        """执行 Judge 调用（含重试）；返回 (value, judge_provenance)。
        value 为 None 表示 job 已被 _fail 终结。"""
        job_id = job["job_id"]
        token = job["lease_token"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._job_total_deadline
        attempts = await self._store.list_attempts(job_id)
        calls_done = sum(1 for a in attempts if a["ended_at"] is not None)
        max_calls = MAX_TOTAL_ATTEMPTS - calls_done
        if max_calls <= 0:
            await self._fail(
                job, token, "attempt_cap_exceeded",
                definition=definition, snapshot=snapshot,
            )
            return None, None
        transient_used = 0
        format_used = 0
        for call_no in range(max_calls):
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self._fail(
                    job, token, "deadline_exceeded",
                    definition=definition, snapshot=snapshot,
                )
                return None, None
            call_timeout = min(float(definition.timeout_seconds), remaining)
            started_at = self._clock()
            error_type: str | None = None
            retry_after: float | None = None
            error_usage: Mapping[str, Any] | None = None
            error_model: str | None = None
            raw: Any = None
            try:
                raw = await asyncio.wait_for(
                    self._evaluator.evaluate(definition, snapshot),
                    timeout=call_timeout,
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                error_type = "timeout"
            except Exception as exc:
                error_type = getattr(exc, "error_type", None) or "network"
                # Retry-After（#26）：服务端显式退避指示参与退避与超期预测
                retry_after = getattr(exc, "retry_after", None)
                # 格式错误同样保留本次调用的 usage 与实际模型（#27）：
                # attempt 行必须完整，保守费用结算不因解析失败丢账
                error_usage = getattr(exc, "usage", None)
                error_model = getattr(exc, "model", None)
            value: EvaluationValue | None = None
            judge_provenance: Mapping[str, Any] | None = None
            if error_type is None:
                if isinstance(raw, Mapping) and isinstance(
                    raw.get("provenance"), Mapping
                ):
                    judge_provenance = raw["provenance"]
                try:
                    value = self._parse_output(definition, raw)
                except _JudgeFormatError:
                    error_type = "format_error"
            await self._record_call(
                job,
                definition,
                snapshot,
                started_at=started_at,
                ended_at=self._clock(),
                error_type=error_type,
                raw=raw,
                call_no=call_no,
                error_usage=error_usage,
                error_model=error_model,
            )
            if value is not None:
                return value, judge_provenance
            assert error_type is not None
            # 类别互斥（spec 6.4"任一类用尽后另一类不得再调用"）：一旦任一
            # 类的重试额度已被消耗，另一类不得再触发重试——重试类别由首次
            # 可重试失败的类别锁定。
            if error_type == "format_error":
                if format_used >= MAX_FORMAT_REPAIRS or transient_used > 0:
                    break
                format_used += 1
            elif error_type in TRANSIENT_ERROR_TYPES:
                if transient_used >= MAX_TRANSIENT_RETRIES or format_used > 0:
                    break
                transient_used += 1
            else:
                break
            # 退避预测：退避等待（服务端 Retry-After 显式指示优先，#26）
            # + 下一次调用的完整超时若越过 job 总期限，本 job error 终结
            # 并保留费用与 attempts（spec 6.4）。
            backoff = self._retry_backoff * (2**call_no)
            if retry_after is not None:
                backoff = max(backoff, float(retry_after))
            if loop.time() + backoff + float(definition.timeout_seconds) > deadline:
                await self._fail(
                    job, token, "deadline_exceeded",
                    definition=definition, snapshot=snapshot,
                )
                return None, None
            if backoff > 0:
                await asyncio.sleep(backoff)
        await self._fail(
            job, token, error_type or "unknown",
            definition=definition, snapshot=snapshot,
        )
        return None, None

    async def _record_call(
        self,
        job: Mapping[str, Any],
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
        *,
        started_at: datetime,
        ended_at: datetime,
        error_type: str | None,
        raw: Any,
        call_no: int,
        error_usage: Mapping[str, Any] | None = None,
        error_model: str | None = None,
    ) -> None:
        """每次真实调用追加一行完整 attempt（请求/实际模型/usage/cost/
        起止/error_type），不覆盖任何旧行。

        judge_request.call_seq_in_run 是本次 worker 运行内的调用序号
        （0 起），不是全局 attempt_index（后者含 claim marker 与跨 claim
        的所有行）；证据消费方不得把两者混用。"""
        request = {
            "definition_id": definition.definition_id,
            "definition_version": definition.version,
            "metric_name": definition.metric_name,
            "judge_model": definition.judge_model,
            "snapshot_sha256": snapshot.content_sha256 if snapshot else None,
            "call_seq_in_run": call_no,
        }
        judge_model = definition.judge_model
        usage_known = False
        input_tokens = output_tokens = total_tokens = None
        cost_usd = None
        if isinstance(raw, Mapping):
            judge_model = raw.get("model") or definition.judge_model
            usage = raw.get("usage") or {}
            if isinstance(usage, Mapping):
                input_tokens = usage.get("input_tokens")
                output_tokens = usage.get("output_tokens")
                total_tokens = usage.get("total_tokens")
                usage_known = any(
                    v is not None
                    for v in (input_tokens, output_tokens, total_tokens)
                )
            if raw.get("cost_usd") is not None:
                cost_usd = str(raw["cost_usd"])
        elif error_type is not None and (error_usage is not None or error_model):
            # 异常路径（raw=None）：格式错误等异常携带的 usage / 实际模型
            # 仍须落完整 attempt 行（费用结算依赖，#27）
            if error_model:
                judge_model = error_model
            usage = error_usage or {}
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            total_tokens = usage.get("total_tokens")
            usage_known = any(
                v is not None
                for v in (input_tokens, output_tokens, total_tokens)
            )
        await self._store.append_call_attempt(
            job["job_id"],
            judge_request=request,
            judge_model=judge_model,
            usage_known=usage_known,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            started_at=started_at.isoformat(),
            ended_at=ended_at.isoformat(),
            error_type=error_type,
        )

    def _parse_output(
        self, definition: EvaluatorDefinition, raw: Any
    ) -> EvaluationValue:
        if not isinstance(raw, Mapping):
            raise _JudgeFormatError(f"evaluator 输出必须是 Mapping: {type(raw)!r}")
        status = raw.get("status", "value")
        explanation = raw.get("explanation") or ""
        if not isinstance(explanation, str):
            explanation = str(explanation)
        if status == "skipped":
            reason = raw.get("skip_reason")
            try:
                skip_reason = SkipReason(reason)
            except ValueError:
                raise _JudgeFormatError(f"非法 skip_reason: {reason!r}") from None
            return EvaluationValue(
                typed_value=None,
                status="skipped",
                skip_reason=skip_reason,
                explanation=explanation,
            )
        if "value" not in raw:
            raise _JudgeFormatError("evaluator 输出缺少 value 字段")
        value = raw["value"]
        try:
            validate_evaluation_value(definition, value)
        except EvaluationValidationError as exc:
            raise _JudgeFormatError(str(exc)) from exc
        return EvaluationValue(
            typed_value=value, status="value", explanation=explanation
        )

    # ------------------------------------------------------------------
    # deterministic 分派（预算不消耗）
    # ------------------------------------------------------------------

    def _evaluate_deterministic(
        self, definition: EvaluatorDefinition, snapshot: EvidenceSnapshot | None
    ) -> EvaluationValue:
        content: dict[str, Any] = dict(snapshot.content) if snapshot else {}
        handler = _DETERMINISTIC_HANDLERS.get(definition.metric_name)
        if handler is None:
            raise EvaluationWorkerError(
                f"未知 deterministic metric: {definition.metric_name!r}"
            )
        return handler(content)

    # ------------------------------------------------------------------
    # 预算预留 / 结算（spec 13）
    # ------------------------------------------------------------------

    def _current_call_ceiling(self) -> int | None:
        """当前生效的日调用上限（C 类热读；provider 失败回退静态值）。
        0 = 显式不设上限（domain 配置语义）-> None（无需预留）。"""
        if self._daily_call_ceiling_provider is not None:
            try:
                ceiling = self._daily_call_ceiling_provider()
                return ceiling if ceiling else None
            except Exception:
                logger.exception(
                    "evaluation call ceiling provider failed (fallback to "
                    "static value)"
                )
        return self._daily_call_ceiling

    def _budget_reservation(
        self, definition: EvaluatorDefinition, *,
        call_ceiling: int | None,
    ) -> tuple[Decimal, int, int] | None:
        """claim 时的预留量（USD, tokens, calls），覆盖全部重试；非 llm
        方法不消耗预算。返回 None 表示无需预留（预算全禁用或非 llm）。
        日调用上限由调用方先经 ``_current_call_ceiling`` 热读后传入，
        保证预留量与预留上限同值（C 类配置热重载，#11）。"""
        if definition.method != "llm_judge":
            return None
        usd = (
            self._per_call_cost_estimate * MAX_TOTAL_ATTEMPTS
            if self._daily_budget_usd is not None
            else Decimal("0")
        )
        tokens = (
            self._per_call_token_estimate * MAX_TOTAL_ATTEMPTS
            if self._daily_token_ceiling is not None
            else 0
        )
        calls = MAX_TOTAL_ATTEMPTS if call_ceiling is not None else 0
        if usd == 0 and tokens == 0 and calls == 0:
            return None
        return usd, tokens, calls

    async def _settle_budget(
        self,
        job_id: str,
        date_key: str,
        reservation: tuple[Decimal, int, int],
    ) -> None:
        """job 终态后结算：已知费用按实际计入并释放预留；任一维度费用
        未知（usage 缺失或崩溃未结算）时按保守语义将整个预留计入消耗，
        限额永不低估。"""
        reserved_usd, reserved_tokens, reserved_calls = reservation
        attempts = await self._store.list_attempts(job_id)
        completed = [a for a in attempts if a["ended_at"] is not None]
        settle_usd = Decimal("0")
        if self._daily_budget_usd is not None:
            costs = [a["cost_usd"] for a in completed]
            if completed and all(c is not None for c in costs):
                settle_usd = sum((Decimal(str(c)) for c in costs), Decimal("0"))
            else:
                settle_usd = reserved_usd  # 保守结算
        settle_tokens = 0
        if self._daily_token_ceiling is not None:
            token_totals = [a["total_tokens"] for a in completed]
            if completed and all(t is not None for t in token_totals):
                settle_tokens = sum(int(t) for t in token_totals)
            else:
                settle_tokens = reserved_tokens  # 保守结算
        settle_calls = len(completed)
        await self._store.settle_daily_budget(
            date_key,
            usd=settle_usd,
            tokens=settle_tokens,
            calls=settle_calls,
            release_usd=reserved_usd,
            release_tokens=reserved_tokens,
            release_calls=reserved_calls,
        )

    # ------------------------------------------------------------------
    # 提交 / 终结
    # ------------------------------------------------------------------

    @staticmethod
    def _skip_provenance(definition: EvaluatorDefinition) -> str:
        """skip 路径（evidence_missing / budget_exhausted 等无分值行）的
        结果行 provenance 记载值：不做校准状态查询（skip 行不参与分值
        投影与门禁统计，无需账本 roundtrip）——llm_judge → ``calibration``
        （保守 advisory 语义），其余 → ``automatic``。"""
        return "calibration" if definition.method == "llm_judge" else "automatic"

    async def _resolve_provenance(
        self,
        definition: EvaluatorDefinition,
        judge_provenance: Mapping[str, Any] | None,
    ) -> str:
        """结果行 provenance 判定（仅 value/skipped-with-output 的结果
        提交路径调用；skip 路径用 ``_skip_provenance``，T8，spec 10；
        语义权威见 app/application/evaluation_calibration.py 模块
        docstring）：

        - deterministic（及一切非 llm_judge）→ ``automatic``；
        - Judge 适配器输出 ``provenance.self_judge`` → ``self_judge``
          （优先于校准状态：即使校准通过也不得作为唯一发布证据）；
        - llm_judge 其余：校准状态为 "passed" → ``automatic``，否则
          （未校准 / advisory / 查询失败 / 未接查询）→ ``calibration``
          （保守 advisory；provider 异常按 advisory 处理并告警）。"""
        if definition.method != "llm_judge":
            return "automatic"
        if (
            isinstance(judge_provenance, Mapping)
            and judge_provenance.get("self_judge")
        ):
            return "self_judge"
        if self._calibration_status_provider is None:
            return "calibration"
        try:
            status = self._calibration_status_provider(definition)
            if hasattr(status, "__await__"):
                status = await status
        except Exception:
            logger.exception(
                "calibration status lookup failed (treated as advisory): "
                "metric=%s",
                definition.metric_name,
            )
            return "calibration"
        return "automatic" if status == "passed" else "calibration"

    @staticmethod
    def _evaluation_trace_identity() -> tuple[str | None, str | None]:
        """当前上下文的 evaluation.run Trace 身份（#12）：在
        evaluation_run_scope 作用域内提交时取到本 job 的评估 Trace hex；
        作用域外（如 human_method_not_executable 前置失败）或 OTel 禁用
        时为 (None, None)，result 行落 NULL（投影语义诚实）。"""
        try:
            from app.application.evaluation_telemetry import (
                current_evaluation_ids,
            )

            return current_evaluation_ids()
        except Exception:
            return None, None

    async def _commit_result(
        self,
        job: Mapping[str, Any],
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
        value: EvaluationValue,
        *,
        terminal_status: str,
        provenance: str,
    ) -> None:
        """result + 终态 job + 各 sink delivery（pending）单事务提交。

        provenance 按 ``_resolve_provenance`` 判定结果落库（T8：未校准
        judge 结果为 calibration、self_judge 透传为 self_judge）；
        explanation（Judge 解释正文）按 spec 13 限长 2000 UTF-8 字节落
        结果行（Langfuse 投影经 T9 comment 路径脱敏后消费）。

        携带领取时的 lease token：租约丢失（被重新领取或取消）时存储层
        CAS 拒绝，本 worker 丢弃结果不重试（新持有者负责终态）。"""
        job_id = job["job_id"]
        token = job["lease_token"]
        result: dict[str, Any] = {
            "job_id": job_id,
            "provenance": provenance,
            "metric_name": definition.metric_name,
            "metric_version": definition.version,
            "status": value.status,
            "skip_reason": (
                value.skip_reason.value if value.skip_reason else None
            ),
            "typed_value": value.typed_value,
            "passed": (
                value.typed_value
                if value.status == "value"
                and isinstance(value.typed_value, bool)
                else None
            ),
            "explanation": _truncate_utf8(
                value.explanation, EXPLANATION_MAX_BYTES
            ),
            "evidence_hash": snapshot.content_sha256 if snapshot else "",
        }
        # 评估 Trace 身份冻结（#12）：OTel 投影的 evaluation_trace 来源
        eval_trace_id, eval_span_id = self._evaluation_trace_identity()
        result["evaluation_trace_id"] = eval_trace_id
        result["evaluation_span_id"] = eval_span_id
        payload = {
            "job_id": job_id,
            "sample_id": job.get("sample_id"),
            "metric_name": definition.metric_name,
            "metric_version": definition.version,
            "status": value.status,
            "skip_reason": result["skip_reason"],
            "typed_value": value.typed_value,
            "evidence_hash": result["evidence_hash"],
        }
        deliveries = [
            {
                "sink": spec["sink"],
                "projection_version": spec["projection_version"],
                "payload": dict(payload),
            }
            for spec in self._sinks
        ]
        try:
            result_id = await self._store.commit_result_with_deliveries(
                result,
                deliveries,
                lease_token=token,
                job_terminal_status=terminal_status,
                now=self._clock(),
            )
        except EvaluationLeaseError:
            logger.warning(
                "evaluation result submission rejected (stale lease), "
                "discarding: job=%s",
                job_id,
            )
            return
        except EvaluationDeletionConflictError:
            # T18 删除传播：删除期间运行中 Judge 返回不得提交或复活内容。
            # 结果整体丢弃（存储层拒绝，无任何行落库），job 以 error 终结
            # （租约 CAS 收敛，避免删除后反复重跑）。
            logger.warning(
                "evaluation result submission rejected (subject deleted), "
                "discarding: job=%s",
                job_id,
            )
            try:
                await self._fail(job, token, "subject_deleted")
            except Exception:
                logger.exception(
                    "evaluation job fail after deletion conflict: job=%s",
                    job_id,
                )
            return
        logger.info(
            "evaluation result committed: job=%s metric=%s status=%s "
            "result_id=%s",
            job_id,
            definition.metric_name,
            terminal_status,
            result_id,
        )
        # T22 收口（T10 emit_result 接线，此前从未被调用）：结果落库后
        # 发布 gen_ai.evaluation.result LogRecord（Loki 消费）。best-effort：
        # 目标身份缺失（producer 快照无 trace 身份）时事件不发射（emit
        # 侧 fail-safe），任何异常隔离为 debug 日志，绝不影响评估主流程。
        try:
            from app.application.evaluation_telemetry import emit_result

            target = None
            if job.get("sample_id"):
                sample = await self._store.load_sample(job["sample_id"])
                target = sample.get("target")
            emit_result(
                result_id=result_id,
                metric_name=definition.metric_name,
                definition_id=definition.definition_id,
                definition_version=definition.version,
                # 遥测词汇 succeeded/skipped/error 与账本 value/skipped/error
                # 映射（spec 12.1 LogRecord 契约）
                status=(
                    "succeeded" if value.status == "value" else value.status
                ),
                target=target or {},
                typed_value=value.typed_value,
                skip_reason=value.skip_reason,
                explanation=value.explanation,
            )
        except Exception:  # noqa: BLE001 - 遥测发射绝不阻塞评估主流程
            logger.debug(
                "evaluation result event emission failed: result=%s",
                result_id,
                exc_info=True,
            )

    async def _fail(
        self,
        job: Mapping[str, Any],
        token: str,
        error_type: str,
        *,
        definition: EvaluatorDefinition | None = None,
        snapshot: EvidenceSnapshot | None = None,
    ) -> None:
        """job error 终结 + 失败事实链（24 项三方审阅 #14）。

        结果、终态 job 与 delivery 同事务提交（spec 6.3）：error 结果行
        与各 sink delivery 经 commit_result_with_deliveries 一次落库，
        结果列表/实验分数收集/OTel 结果出口不再缺失败事实。删除冲突
        （subject_deleted）不落任何行——结果整体丢弃是删除传播语义的
        一部分，job 仅以 error 终结。"""
        if error_type != "subject_deleted" and definition is not None:
            if await self._commit_error_result(
                job, token, error_type, definition, snapshot
            ):
                return
        updated = await self._store.fail_job(
            job["job_id"], lease_token=token, error_type=error_type, now=self._clock()
        )
        if not updated:
            logger.warning(
                "evaluation job fail CAS lost (lease changed): job=%s "
                "error_type=%s",
                job.get("job_id"),
                error_type,
            )

    async def _commit_error_result(
        self,
        job: Mapping[str, Any],
        token: str,
        error_type: str,
        definition: EvaluatorDefinition,
        snapshot: EvidenceSnapshot | None,
    ) -> bool:
        """error 结果 + 终态 job + deliveries 单事务提交（#14）。

        返回 True 表示 job 已终态（无需再走 fail_job）；提交冲突/租约
        丢失/状态机拒绝时回退 fail_job 路径，不阻塞终结。"""
        job_id = job["job_id"]
        result: dict[str, Any] = {
            "job_id": job_id,
            "provenance": "automatic",
            "metric_name": definition.metric_name,
            "metric_version": definition.version,
            "status": "error",
            "skip_reason": None,
            "typed_value": None,
            "passed": None,
            "explanation": _truncate_utf8(
                f"job failed: {error_type}", EXPLANATION_MAX_BYTES
            ),
            "evidence_hash": snapshot.content_sha256 if snapshot else "",
        }
        # 评估 Trace 身份冻结（#12）：error 结果同样携带（若在作用域内）
        eval_trace_id, eval_span_id = self._evaluation_trace_identity()
        result["evaluation_trace_id"] = eval_trace_id
        result["evaluation_span_id"] = eval_span_id
        payload = {
            "job_id": job_id,
            "sample_id": job.get("sample_id"),
            "metric_name": definition.metric_name,
            "metric_version": definition.version,
            "status": "error",
            "skip_reason": None,
            "typed_value": None,
            "evidence_hash": result["evidence_hash"],
        }
        deliveries = [
            {
                "sink": spec["sink"],
                "projection_version": spec["projection_version"],
                "payload": dict(payload),
            }
            for spec in self._sinks
        ]
        try:
            result_id = await self._store.commit_result_with_deliveries(
                result,
                deliveries,
                lease_token=token,
                job_terminal_status=EvaluationJobStatus.error.value,
                job_error_type=error_type,
                now=self._clock(),
            )
        except EvaluationLeaseError:
            logger.warning(
                "evaluation error result submission rejected (stale lease), "
                "discarding: job=%s",
                job_id,
            )
            return True
        except Exception:
            logger.exception(
                "evaluation error result commit failed, falling back to "
                "fail_job: job=%s error_type=%s",
                job_id,
                error_type,
            )
            return False
        logger.info(
            "evaluation error result committed: job=%s metric=%s error=%s",
            job_id,
            definition.metric_name,
            error_type,
        )
        try:
            from app.application.evaluation_telemetry import emit_result

            target = None
            if job.get("sample_id"):
                sample = await self._store.load_sample(job["sample_id"])
                target = sample.get("target")
            emit_result(
                result_id=result_id,
                metric_name=definition.metric_name,
                definition_id=definition.definition_id,
                definition_version=definition.version,
                status="error",
                target=target or {},
                typed_value=None,
                skip_reason=None,
                explanation=None,
                error_type=error_type,
            )
        except Exception:  # noqa: BLE001 - 遥测发射绝不阻塞评估主流程
            logger.debug(
                "evaluation error result event emission failed: job=%s",
                job_id,
                exc_info=True,
            )
        return True
