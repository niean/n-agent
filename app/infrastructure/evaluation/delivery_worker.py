# app/infrastructure/evaluation/delivery_worker.py
"""EvaluationDeliveryWorker -- 可恢复投递调度器（plan T9，spec 11.1 / 13）。

职责：claim（lease 领取）-> 冻结 payload/external_id（先登记再发送）->
send（2xx = accepted，非 verified）-> 有界回读核验（完全匹配才 verified）->
分类失败调度（409/4xx 冲突 -> dead_letter 隔离上报；429/5xx/网络 -> 指数
退避 1s~5min、自 created_at 起 24h 后 dead_letter）。依赖全部构造注入
（store 依据 Domain ``DeliveryStore`` 端口形状、sinks 依据 Domain
``ScoreSink`` 端口形状、clock/linkability provider），组合根负责装配。

关键不变量：
- 冻结 payload 在第一次发送前经 ``freeze_delivery_payload`` 持久化；之后
  的每次重试（含重启恢复）复用同 ID 同 payload（payload_json 为权威依据，
  不再调用 sink.project 重新投影——避免重试期间 linkability 配置翻转把
  已发送 Score 改判 unlinked，也避免重复 ScoreConfig IO），绝不重新评分、
  不改写冻结内容；重试发送前以 freeze CAS 重断言冻结内容未被改写
  （分叉 -> EvaluationConflictError，投递保留 pending 待人工排查）；
- 一个 sink 的故障只影响该 sink 的投递状态（逐 delivery 异常隔离 +
  分类落账），不阻塞其他 sink，也不触发重新评分；
- 仅注册了适配器的 sink 会被领取（``claim_next_delivery(sinks=...)``）：
  OTel 投递状态独立（pending/emitted/failed 语义由 T10 定义），SDK enqueue
  绝不由本 worker 记录为 delivered/verified；
- 仅 succeeded（status=value）结果投影正式分值；skipped/error/cancelled
  的投递直接落 unlinked（error_type=not_projected），不写 0、不伪造类别；
  目标无真实身份 / 未采样 / OTel 禁用同理 unlinked，不发任何请求；
- 意外异常（非分类失败）不上抛（fire-and-forget 隔离语义，对齐
  EvaluationWorker 房规）：投递保留 pending，租约过期后由任意 worker
  重领恢复，不进 dead_letter。

生命周期（对齐 EvaluationWorker / task_runner 房规）：``start()`` 常驻
轮询循环（claim -> fire-and-forget 派发），``stop()`` 停止领取 + 有界
等待在途任务，``run_once()`` 同步领取并处理一条投递（测试与手动驱动），
``claim()`` / ``process(delivery)`` 暴露分步原语；
``disable_sink(sink)`` / ``enable_sink(sink)`` 关闭/恢复出口（disabled
保留状态；dead_letter 不随出口开关自动补发，仅管理员显式 replay 覆盖；
恢复后仅未 verified 的投递回 pending 补发）。不接入 main.py
lifespan（T20 组合根接线）。

显式补发（补发 dead_letter）：管理员经
``POST /chat/evaluations/deliveries/retry``（EvaluationService.retry_delivery）
把 dead_letter -> pending，本 worker 正常领取并复用冻结 ID/payload 重投。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol

from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationDeletionConflictError,
    EvaluationNotFoundError,
)
from app.domain.evaluation.state import (
    EvaluationDeliveryStatus,
    SWEEP_DISABLE_REASON,
    transition_delivery_status,
)

logger = logging.getLogger(__name__)

# spec 13：指数退避 1s ~ 5min；自 created_at 起 24h 退避窗口
# （不是首次失败起算：窗口锚点为投递创建时间，语义与测试一致）
DEFAULT_RETRY_BACKOFF_BASE_SECONDS: float = 1.0
DEFAULT_RETRY_BACKOFF_MAX_SECONDS: float = 300.0
DEFAULT_RETRY_WINDOW_SECONDS: float = 24 * 3600.0

# accepted 后有界回读核验（spec 13：有界轮询，超窗进入退避重试）
DEFAULT_VERIFY_POLL_ATTEMPTS: int = 5
DEFAULT_VERIFY_POLL_INTERVAL_SECONDS: float = 2.0

DEFAULT_CLAIMANT: str = "delivery-worker"
DEFAULT_LEASE_SECONDS: int = 300


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _iso(now: datetime) -> str:
    """与 store 一致的 UTC ISO-8601 字符串（租约过期比较用）。"""
    return now.astimezone(timezone.utc).isoformat()


def _default_linkability() -> Mapping[str, Any]:
    """缺省可关联状态（保守安全缺省）：未采样且 OTel 禁用 -> 一律
    unlinked 不投递。忘注入 provider 的后果是"不发送"而不是"静默全量
    关联"（fail-safe）；T20 组合根接线时必须注入真实 C 类配置
    （online sample probability 与 OTel 开关），不在本 worker 推断。"""
    return {"otel_enabled": False, "sampled": False}


class DeliveryStore(Protocol):
    """投递调度存储端口（Domain ``DeliveryStore`` 端口形状）。"""

    async def claim_next_delivery(
        self,
        claimant: str,
        lease_seconds: int = 300,
        *,
        sinks: Any = None,
        reclaim_accepted_sinks: Any = None,
        now: Any = None,
    ) -> Mapping[str, Any] | None: ...

    async def get_delivery(self, delivery_id: str) -> Mapping[str, Any]: ...

    async def list_deliveries(
        self, result_id: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    async def load_result(self, result_id: str) -> Mapping[str, Any]: ...

    async def get_feedback_by_result_id(
        self, result_id: str
    ) -> Mapping[str, Any] | None: ...

    async def get_job(self, job_id: str) -> Mapping[str, Any]: ...

    async def load_sample(self, sample_id: str) -> Mapping[str, Any]: ...

    async def get_definition(
        self, definition_id: str, version: str
    ) -> Mapping[str, Any]: ...

    async def freeze_delivery_payload(
        self,
        delivery_id: str,
        *,
        external_id: str,
        payload: Mapping[str, Any],
        now: Any = None,
    ) -> Mapping[str, Any]: ...

    async def schedule_delivery_retry(
        self,
        delivery_id: str,
        *,
        error_type: str,
        next_retry_at: Any,
        lease_token: str | None = None,
        now: Any = None,
    ) -> Mapping[str, Any]: ...

    async def record_delivery_transition(
        self,
        delivery_id: str,
        target_status: str,
        *,
        external_id: str | None = None,
        error_type: str | None = None,
        next_retry_at: str | None = None,
        lease_token: str | None = None,
        now: Any = None,
    ) -> Mapping[str, Any]: ...


class ScoreSink(Protocol):
    """分数出口端口（Domain ``ScoreSink`` 端口形状）。

    分类失败契约：异常携带 ``delivery_outcome``（"dead_letter" | "retry"）
    与 ``error_type`` 属性（spec 13 表）；其他异常视为意外错误（保留
    pending 待租约过期重领）。"""

    async def project(self, context: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def send(self, payload: Mapping[str, Any]) -> str: ...

    async def read_back(self, external_id: str) -> Mapping[str, Any] | None: ...

    async def verify(
        self, payload: Mapping[str, Any], read_back: Mapping[str, Any] | None
    ) -> bool: ...


class EvaluationDeliveryWorker:
    """可恢复投递调度器（claim / 冻结 payload / 发送 / 回读核验 / 退避）。"""

    def __init__(
        self,
        store: DeliveryStore,
        *,
        sinks: Mapping[str, ScoreSink],
        claimant: str = DEFAULT_CLAIMANT,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        poll_interval_seconds: float = 5.0,
        shutdown_grace_seconds: float = 30.0,
        retry_backoff_base_seconds: float = DEFAULT_RETRY_BACKOFF_BASE_SECONDS,
        retry_backoff_max_seconds: float = DEFAULT_RETRY_BACKOFF_MAX_SECONDS,
        retry_window_seconds: float = DEFAULT_RETRY_WINDOW_SECONDS,
        verify_poll_attempts: int = DEFAULT_VERIFY_POLL_ATTEMPTS,
        verify_poll_interval_seconds: float = DEFAULT_VERIFY_POLL_INTERVAL_SECONDS,
        linkability_provider: Callable[[], Mapping[str, Any]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not sinks:
            raise ValueError("delivery worker 需要至少一个 sink 适配器")
        for name, sink in sinks.items():
            if not name or sink is None:
                raise ValueError(f"非法 sink 适配器: {name!r}")
        if verify_poll_attempts < 1:
            raise ValueError("verify_poll_attempts 必须为正整数")
        self._store = store
        self._sinks: dict[str, ScoreSink] = dict(sinks)
        self._claimant = claimant
        self._lease_seconds = lease_seconds
        self._poll_interval = poll_interval_seconds
        self._shutdown_grace = shutdown_grace_seconds
        self._backoff_base = retry_backoff_base_seconds
        self._backoff_max = retry_backoff_max_seconds
        self._retry_window = retry_window_seconds
        self._verify_poll_attempts = verify_poll_attempts
        self._verify_poll_interval = verify_poll_interval_seconds
        self._linkability_provider = linkability_provider or _default_linkability
        self._clock = clock or _default_clock
        self._stop_event: asyncio.Event | None = None
        self._loop_task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 公共原语
    # ------------------------------------------------------------------

    async def claim(self) -> Mapping[str, Any] | None:
        """领取下一条到期可投递的 delivery（含过期租约重领）。

        accepted 过期重领仅限可回读 sink（24 项三方审阅 #11）：无回读
        sink（OTel）的 accepted 是成功终态，重领会让成功投递周期性
        重发、队列永不收敛。"""
        return await self._store.claim_next_delivery(
            self._claimant,
            self._lease_seconds,
            sinks=tuple(self._sinks),
            reclaim_accepted_sinks=tuple(
                name
                for name, sink in self._sinks.items()
                if getattr(sink, "supports_readback", True)
            ),
            now=self._clock(),
        )

    async def process(self, delivery: Mapping[str, Any]) -> None:
        """处理一条已领取的 delivery；意外异常只记录不上抛
        （fire-and-forget 隔离语义，与循环派发路径一致）。"""
        try:
            await self._process_claimed(delivery)
        except Exception:
            logger.exception(
                "evaluation delivery processing failed: delivery=%s sink=%s",
                delivery.get("delivery_id"),
                delivery.get("sink"),
            )

    async def run_once(self) -> str | None:
        """领取并同步处理一条投递；返回 delivery_id，无则 None。"""
        delivery = await self.claim()
        if delivery is None:
            return None
        await self.process(delivery)
        return delivery["delivery_id"]

    # ------------------------------------------------------------------
    # 生命周期（对齐 EvaluationWorker 房规）
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动常驻轮询循环（幂等）。启动期先做 T18/M3 崩溃自愈：恢复
        上一进程删除传播 sweep 关闭出口后未及恢复的 disabled 投递
        （last_error_type 归因标记匹配才恢复；管理员显式 disable 不受
        影响），已删主体行由 claim tombstone 门禁继续阻断。"""
        if self._loop_task is not None:
            return
        self._stop_event = asyncio.Event()
        await self._recover_sweep_disabled()
        self._loop_task = asyncio.create_task(self._loop())
        logger.info(
            "EvaluationDeliveryWorker started (claimant=%s sinks=%s)",
            self._claimant,
            sorted(self._sinks),
        )

    async def _recover_sweep_disabled(self) -> int:
        """启动期自愈（T18/M3）：sweep 归因的 disabled 投递 -> pending。

        删除传播 runner 在 disable -> enable 窗口崩溃时遗留 disabled 行；
        本恢复按 ``last_error_type`` 归因标记（SWEEP_DISABLE_REASON）精确
        恢复该来源的行，管理员显式关闭（无标记）永不自动恢复。"""
        recovered = 0
        for row in await self._store.list_deliveries():
            if (
                row["status"] != EvaluationDeliveryStatus.disabled.value
                or row.get("last_error_type") != SWEEP_DISABLE_REASON
            ):
                continue
            await self._store.record_delivery_transition(
                row["delivery_id"],
                EvaluationDeliveryStatus.pending.value,
                now=self._clock(),
            )
            recovered += 1
        if recovered:
            logger.info(
                "recovered sweep-disabled deliveries at startup: count=%s",
                recovered,
            )
        return recovered

    async def stop(self) -> None:
        """停止领取 + 有界等待在途投递（超时取消），幂等。"""
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
                # 取消后必须回收任务，否则 stop() 返回后留下未取回
                # 异常的任务警告（unretrieved task exception）
                await asyncio.gather(loop_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "EvaluationDeliveryWorker loop stopped with error"
                )
        inflight = [t for t in self._inflight if not t.done()]
        if inflight:
            await asyncio.wait(inflight, timeout=self._shutdown_grace)
            for task in inflight:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*inflight, return_exceptions=True)
        self._inflight.clear()
        self._stop_event = None
        logger.info("EvaluationDeliveryWorker stopped (claimant=%s)", self._claimant)

    async def wait_inflight(self, timeout: float) -> bool:
        """有界等待在途投递收敛（T18 删除传播：先禁新发送后，等待已领取
        的投递处理完再执行外部删除）。

        进程内可见性边界（T18/M1）：本方法只能观察到本进程 ``_inflight``
        集合——多进程部署下其他进程的在途发送不可见，调用方（删除传播
        runner）在返回 False 或跨进程部署时按"未收敛"处理，将外部删除
        延后一轮（deletion_pending 复用回读重复核对收敛）；freeze/
        verified 两处 tombstone 门禁仍兜底阻断已删主体的发送与确认。

        返回 True 表示在途集合已清空；False 表示超时仍有在途任务。"""
        inflight = [t for t in self._inflight if not t.done()]
        if not inflight:
            return True
        await asyncio.wait(inflight, timeout=timeout)
        return all(t.done() for t in self._inflight)

    async def _unlink_deleted(
        self, delivery_id: str, lease_token: str | None
    ) -> None:
        """发送管线命中删除 tombstone：改判 unlinked 隔离上报（不重发、
        不 verified；已发送内容按预登记 external_id 走外部删除任务）。"""
        logger.info(
            "delivery subject deleted (tombstone), unlinking: delivery=%s",
            delivery_id,
        )
        await self._store.record_delivery_transition(
            delivery_id,
            EvaluationDeliveryStatus.unlinked.value,
            error_type="subject_deleted",
            lease_token=lease_token,
            now=self._clock(),
        )

    async def _loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            delivery: Mapping[str, Any] | None = None
            try:
                delivery = await self.claim()
            except Exception:
                logger.exception("evaluation delivery claim failed")
            if delivery is not None:
                task = asyncio.create_task(self.process(delivery))
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
    # 关闭 / 恢复出口（spec 13：关闭出口停止投递而保留状态）
    # ------------------------------------------------------------------

    async def disable_sink(
        self, sink: str, *, reason: str | None = None
    ) -> int:
        """关闭出口：该 sink 全部非终态且非 dead_letter 的投递 ->
        disabled（经状态机校验），保留状态不投递。dead_letter 不随出口
        开关迁移（disabled -> pending 会绕过管理员显式 replay 合同自动
        补发隔离投递），只响应 ``retry_delivery`` 显式补发。返回迁移
        条数。

        T18/Mi7：持有活跃租约的在途投递跳过（不做无 CAS 抢改——其发送
        已不可撤回，改判会在 worker 后续迁移上产生意外异常噪声；租约
        过期后由 claim tombstone 门禁/本轮收敛处理）。``reason`` 作为
        归因标记写入 last_error_type（SWEEP_DISABLE_REASON 供崩溃自愈
        精确恢复；None 表示管理员显式关闭，清空归因）。"""
        now = self._clock()
        count = 0
        for row in await self._store.list_deliveries():
            if row["sink"] != sink or row["status"] in (
                EvaluationDeliveryStatus.verified.value,
                EvaluationDeliveryStatus.unlinked.value,
                EvaluationDeliveryStatus.disabled.value,
                EvaluationDeliveryStatus.dead_letter.value,
            ):
                continue
            # T18/Mi7：活跃租约内的在途投递不抢改（pending=已领取待处理、
            # accepted=发送/回读窗口中；retry_wait 已离开 worker 手中，
            # 其租约为历史残留，不受此限）
            if row["status"] in (
                EvaluationDeliveryStatus.pending.value,
                EvaluationDeliveryStatus.accepted.value,
            ) and (
                row.get("lease_expiry") is not None
                and row["lease_expiry"] > _iso(now)
            ):
                continue
            # 校验迁移合法性（非法状态跳过，不中断整批）
            try:
                transition_delivery_status(
                    row["status"], EvaluationDeliveryStatus.disabled
                )
            except ValueError:
                continue
            # m3（质量复审）：携带 lease CAS——retry_wait/verify_pending 行
            # 携带历史租约 token，列表读取后若已被其他 worker 重领（TOCTOU
            # 窗口），CAS 失效抛 EvaluationConflictError，跳过不抢改；
            # 无租约行（lease_token NULL）退化为无 CAS（与既有行为一致）
            try:
                await self._store.record_delivery_transition(
                    row["delivery_id"],
                    EvaluationDeliveryStatus.disabled.value,
                    error_type=reason,
                    lease_token=row.get("lease_token") or None,
                    now=now,
                )
            except EvaluationConflictError:
                continue
            count += 1
        logger.info(
            "evaluation delivery sink disabled: sink=%s count=%s reason=%s",
            sink,
            count,
            reason,
        )
        return count

    async def enable_sink(self, sink: str) -> int:
        """恢复出口：该 sink 的 disabled 投递 -> pending（补发未 verified
        的结果；verified/unlinked 终态不动）。disable 已排除 dead_letter，
        恢复不会自动补发任何 dead_letter 投递（仅管理员显式 replay）。
        返回迁移条数。"""
        count = 0
        for row in await self._store.list_deliveries():
            if row["sink"] != sink or row["status"] != (
                EvaluationDeliveryStatus.disabled.value
            ):
                continue
            await self._store.record_delivery_transition(
                row["delivery_id"],
                EvaluationDeliveryStatus.pending.value,
                now=self._clock(),
            )
            count += 1
        logger.info(
            "evaluation delivery sink enabled: sink=%s count=%s", sink, count
        )
        return count

    # ------------------------------------------------------------------
    # 单条投递处理
    # ------------------------------------------------------------------

    async def _process_claimed(self, delivery: Mapping[str, Any]) -> None:
        delivery_id = delivery["delivery_id"]
        sink_name = delivery["sink"]
        lease_token = delivery.get("lease_token")
        sink = self._sinks.get(sink_name)
        if sink is None:
            # claim 已按注册 sinks 过滤；防御性兜底：直接释放（无状态
            # 迁移），交还队列
            logger.warning(
                "claimed delivery without registered sink (released): "
                "delivery=%s sink=%s",
                delivery_id,
                sink_name,
            )
            return
        result = await self._store.load_result(delivery["result_id"])
        if result["status"] != "value":
            # 仅 succeeded 结果投影正式分值：skipped/error/cancelled 不写
            # 0、不伪造类别（spec 11.1），落 unlinked 隔离上报
            await self._store.record_delivery_transition(
                delivery_id,
                EvaluationDeliveryStatus.unlinked.value,
                error_type="not_projected",
                lease_token=lease_token,
                now=self._clock(),
            )
            return
        if result.get("job_id") is None:
            # human result：T18 门禁对等——目标主体已删除的人评投递不新
            # 发送（改判 unlinked，不进 dead_letter、不产生重发循环）
            feedback = await self._store.get_feedback_by_result_id(
                result["result_id"]
            )
            if feedback is not None and feedback.get("tombstoned"):
                await self._unlink_deleted(delivery_id, lease_token)
                return
        frozen_json = delivery.get("payload_json")
        if frozen_json:
            # 已冻结（重试/恢复路径）：复用冻结 external_id/payload，
            # 绝不重新投影——重试期间 linkability 配置翻转不得把已发送
            # 的 Score 改判 unlinked，也不重复 ScoreConfig IO。
            # freeze CAS 重断言：声明行与库内冻结内容分叉（异常改写/
            # 并发写）时抛 EvaluationConflictError -> 意外异常语义，
            # 投递保留 pending 待人工排查，不丢失、不进 dead_letter。
            payload = json.loads(frozen_json)
            external_id = delivery.get("external_id") or payload.get("id")
            try:
                frozen = await self._store.freeze_delivery_payload(
                    delivery_id,
                    external_id=external_id,
                    payload=payload,
                    now=self._clock(),
                )
            except EvaluationDeletionConflictError:
                # T18：复用冻结 payload 前复核发现主体已删除——不发送、
                # 改判 unlinked（external_id 已预登记，外部删除任务收敛）
                await self._unlink_deleted(delivery_id, lease_token)
                return
            payload = json.loads(frozen["payload_json"])
        else:
            target, definition, mode = await self._load_projection_inputs(result)
            # 评估 Trace 身份（#12）：result 行冻结的 evaluation.run 身份
            # （v7 前/作用域外为 NULL）；业务身份仍在 target 中，不得混淆
            evaluation_trace = (
                result.get("evaluation_trace_id"),
                result.get("evaluation_span_id"),
            )
            context = {
                "result": result,
                "target": target,
                "definition": definition,
                "linkability": dict(self._linkability_provider()),
                "projection_version": delivery["projection_version"],
                "mode": mode,
                "evaluation_trace": evaluation_trace,
            }
            try:
                projection = await sink.project(context)
                if projection["outcome"] != "deliverable":
                    # unlinked（无真实身份 / 未采样 / OTel 禁用）：不发
                    # 请求、不建悬空 Score（spec 11.1/13）
                    await self._store.record_delivery_transition(
                        delivery_id,
                        EvaluationDeliveryStatus.unlinked.value,
                        lease_token=lease_token,
                        now=self._clock(),
                    )
                    return
                frozen = await self._store.freeze_delivery_payload(
                    delivery_id,
                    external_id=projection["external_id"],
                    payload=projection["payload"],
                    now=self._clock(),
                )
                payload = json.loads(frozen["payload_json"])
                external_id = projection["external_id"]
            except asyncio.CancelledError:
                raise
            except EvaluationDeletionConflictError:
                # T18：freeze 门禁（发布前复核 tombstone）拒绝已删主体的
                # 新发送——改判 unlinked，不进 dead_letter、不重试
                await self._unlink_deleted(delivery_id, lease_token)
                return
            except Exception as exc:
                await self.handle_send_error(
                    delivery_id, exc, lease_token=lease_token
                )
                return
        try:
            # 发送已冻结 payload：2xx -> accepted（非 verified）；分类
            # 失败抛冲突/可重试异常，由下方分类调度
            await sink.send(payload)
            await self._store.record_delivery_transition(
                delivery_id,
                EvaluationDeliveryStatus.accepted.value,
                lease_token=lease_token,
                now=self._clock(),
            )
            if not getattr(sink, "supports_readback", True):
                # 无回读能力（#13 OTel sink）：accepted 即投递成功终态，
                # 不进入 verified 轮询（read_back 恒 None 会让全部投递
                # 误入 verify_pending 退避循环）
                return
            # 有界回读核验：同 ID/目标/类型/value/config 完全匹配才
            # verified
            for _ in range(self._verify_poll_attempts):
                if self._verify_poll_interval > 0:
                    await asyncio.sleep(self._verify_poll_interval)
                read_back = await sink.read_back(external_id)
                if await sink.verify(payload, read_back):
                    await self._store.record_delivery_transition(
                        delivery_id,
                        EvaluationDeliveryStatus.verified.value,
                        lease_token=lease_token,
                        now=self._clock(),
                    )
                    return
            # 回读超窗（如 Score 先于 Trace 到达）：退避重试，重试复用同
            # ID/payload（冻结不可变）
            await self._schedule_retry(
                delivery_id, "verify_pending", lease_token=lease_token
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 分类失败（dead_letter/retry）落账；意外异常经
            # handle_send_error 原样上抛，交由 process 的隔离语义处理
            # （保留 pending 待租约过期重领，不进 dead_letter）
            await self.handle_send_error(delivery_id, exc, lease_token=lease_token)

    async def _load_projection_inputs(
        self, result: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
        """定位投影输入：delivery -> result(job) -> sample(target/mode) 与
        definition（ScoreConfig schema / name 语义来源）。

        human result（job_id=None）经 feedback 行解析：target 与
        definition 身份挂在 feedback 上（T22 人评投递），mode 为 online。
        mode 供 OTel 投影（execute/replay/online 观测语义）。"""
        if result.get("job_id") is None:
            feedback = await self._store.get_feedback_by_result_id(
                result["result_id"]
            )
            if feedback is None:
                raise EvaluationNotFoundError(
                    "human result 缺少关联 feedback，无法解析投影输入: "
                    f"result_id={result.get('result_id')}"
                )
            definition = await self._store.get_definition(
                feedback["definition_id"], feedback["definition_version"]
            )
            return feedback.get("target") or {}, definition, "online"
        job = await self._store.get_job(result["job_id"])
        sample = await self._store.load_sample(job["sample_id"])
        definition = await self._store.get_definition(
            job["definition_id"], job["definition_version"]
        )
        return (
            sample.get("target") or {},
            definition,
            str(sample.get("mode") or "online"),
        )

    async def _schedule_retry(
        self,
        delivery_id: str,
        error_type: str,
        *,
        lease_token: str | None = None,
    ) -> None:
        """分类重试调度：自 created_at 起的 24h 退避窗口超时 -> dead_letter
        （管理员可显式补发）；否则 retry_wait + 指数退避（1s ~ 5min，指数
        取 retry_count——真实网络重试计数）。
        ``lease_token`` 供存储 CAS：投递已被其他 worker 重领时拒绝迁移。
        """
        row = await self._store.get_delivery(delivery_id)
        now = self._clock()
        created = datetime.fromisoformat(row["created_at"])
        if now - created >= timedelta(seconds=self._retry_window):
            await self._store.record_delivery_transition(
                delivery_id,
                EvaluationDeliveryStatus.dead_letter.value,
                error_type=error_type,
                lease_token=lease_token,
                now=now,
            )
            logger.warning(
                "evaluation delivery dead-lettered after retry window: "
                "delivery=%s error_type=%s",
                delivery_id,
                error_type,
            )
            return
        delay = min(
            self._backoff_base * (2 ** int(row["retry_count"])),
            self._backoff_max,
        )
        await self._store.schedule_delivery_retry(
            delivery_id,
            error_type=error_type,
            next_retry_at=now + timedelta(seconds=delay),
            lease_token=lease_token,
            now=now,
        )

    # ------------------------------------------------------------------
    # 分类失败调度（供 process 捕获；spec 13 表）
    # ------------------------------------------------------------------

    async def handle_send_error(
        self,
        delivery_id: str,
        exc: Exception,
        *,
        lease_token: str | None = None,
    ) -> None:
        """把分类失败异常落账：dead_letter 类隔离上报；retry 类退避。
        意外异常（无 delivery_outcome 属性）保留 pending 待租约过期重领，
        不进 dead_letter、不丢失。``lease_token`` 透传存储 CAS（陈旧
        worker 的迁移被拒绝）。"""
        outcome = getattr(exc, "delivery_outcome", None)
        error_type = getattr(exc, "error_type", "unknown")
        if outcome == "dead_letter":
            await self._store.record_delivery_transition(
                delivery_id,
                EvaluationDeliveryStatus.dead_letter.value,
                error_type=error_type,
                lease_token=lease_token,
                now=self._clock(),
            )
            logger.warning(
                "evaluation delivery dead-lettered (isolated): delivery=%s "
                "error_type=%s",
                delivery_id,
                error_type,
            )
            return
        if outcome == "retry":
            await self._schedule_retry(
                delivery_id, error_type, lease_token=lease_token
            )
            return
        raise exc  # 意外异常：交由 process 的隔离语义处理（保留 pending）
