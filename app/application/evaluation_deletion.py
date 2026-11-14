# app/application/evaluation_deletion.py
"""删除传播服务（plan T18，spec 14）。

会话/文档删除时按 evidence_targets 反向索引把删除传播到评估子域：

1. tombstone 先行：删除事件到达即写持久 tombstone（跨进程可重试且幂
   等，重复事件返回原 tombstone_id）——它是后续所有门禁（迟到样本拒绝、
   claim/freeze/verified 复核、结果提交拒绝）的权威依据；
2. 撤销与隐藏：撤销无法继续执行的 queued job；关联 result/feedback 写
   tombstone 使当前视图不再展示（不可变历史行保留）；无租约的 pending/
   retry_wait 投递改判 unlinked（持租约在途投递由门禁收敛）；
3. 内容清除：purge 关联证据快照正文（内容寻址文件被其他未删主体引用时
   跳过文件删除、仅清引用行），生成不含正文的 tombstone；
4. 外部删除：按投递冻结时预登记的全部 external_id 登记持久删除任务
   （deletion_tasks），由 ``run_external_deletions_once`` / ``start`` 按
   spec 14 顺序执行——先禁止新发送（disable_sink，携带 SWEEP_DISABLE_
   REASON 归因；enable 在 finally 中执行，进程级崩溃由 delivery worker
   启动期按归因自愈），有界等待在途请求收敛（wait_inflight 仅进程内
   可见：超时未收敛本轮任务一律 deletion_pending 延后，不执行删除），
   再逐个外部删除并回读；未知发送结果保留 deletion_pending 重复核对
   （第一次 404 不作结论，连续缺席判定与全局 attempt_count 解耦），
   后端不可用保留删除任务，尚未完成不得宣称外部已删除。

远端文档删除（N-KB）经 T17 认证消息进入后调用 ``handle_document_deleted``；
``KnowledgeService.delete_base`` 仅表示 KB 注册项删除，不冒充远端文档
删除。会话删除接线复用 SessionService 的 on_session_deleted handler 列
表（post-delete 失败不阻塞业务删除；tombstone 先行保证失败后重试仍收敛）。

依赖全部鸭子类型（Application 层禁止 import app.infrastructure）：
- ``store``：SQLiteEvaluationStore 形状的评估账本端口（tombstone/任务/
  级联查询/撤销/purge 引用清除）；
- ``evidence_store``：可选，需 ``purge_snapshot(snapshot_ref) -> bool``；
- ``external_deleters``：sink 名 -> 外部删除端（``delete_external`` 返回
  "deleted"/"not_found"/"unknown"，``read_back`` 返回 Mapping 或 None）；
- ``delivery_worker``：可选投递出口闸（``disable_sink``/``enable_sink``/
  ``wait_inflight``）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable

from app.domain.evaluation.state import SWEEP_DISABLE_REASON

logger = logging.getLogger(__name__)

# 第一次 404 不作结论：not_found 后需重复核对回读缺席到第 N 轮才判 done
# （spec 14：不能因第一次 404 就假定迟到写入永不发生）
CONFIRM_ABSENT_RUNS: int = 2

# 外部删除批处理前等待在途投递收敛的默认有界时长（秒）
DEFAULT_CONVERGE_WAIT_SECONDS: float = 5.0

# deletion_pending 任务的默认重试间隔（秒）
DEFAULT_DELETION_RETRY_DELAY_SECONDS: float = 60.0

# 常驻重试循环的默认轮询间隔（秒；生命周期托管由 T20 组合根决定）
DEFAULT_POLL_INTERVAL_SECONDS: float = 30.0

# 外部删除端 delete_external 的封闭结果集
DELETER_OUTCOMES: frozenset[str] = frozenset(
    {"deleted", "not_found", "unknown"}
)


@runtime_checkable
class ExternalDeleter(Protocol):
    """外部删除端端口（sink 适配器实现，如 LangfuseScoreSink）。"""

    async def delete_external(self, external_id: str) -> str:
        """删除外部对象；返回 "deleted" / "not_found" / "unknown"。"""
        ...

    async def read_back(self, external_id: str) -> Mapping[str, Any] | None:
        """按 external_id 回读外部对象；不存在返回 None。"""
        ...


@runtime_checkable
class DeliveryGate(Protocol):
    """投递出口闸端口（EvaluationDeliveryWorker 子集）。

    ``reason`` 为关闭归因（T18/M3）：删除传播 sweep 传
    SWEEP_DISABLE_REASON，worker 启动期按该标记崩溃自愈。"""

    async def disable_sink(
        self, sink: str, *, reason: str | None = None
    ) -> int: ...

    async def enable_sink(self, sink: str) -> int: ...

    async def wait_inflight(self, timeout: float) -> bool: ...


class EvaluationDeletionService:
    """评估子域删除传播编排（幂等、可重试、跨进程收敛）。"""

    def __init__(
        self,
        *,
        store: Any,
        evidence_store: Any = None,
        external_deleters: Mapping[str, ExternalDeleter] | None = None,
        delivery_worker: DeliveryGate | None = None,
        scenario_resource_ledger: Any = None,
        clock: Callable[[], datetime] | None = None,
        converge_wait_seconds: float = DEFAULT_CONVERGE_WAIT_SECONDS,
        deletion_retry_delay_seconds: float = (
            DEFAULT_DELETION_RETRY_DELAY_SECONDS
        ),
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._evidence_store = evidence_store
        self._external_deleters: dict[str, ExternalDeleter] = dict(
            external_deleters or {}
        )
        self._delivery_worker = delivery_worker
        # plan T4：场景资源账本（可选）。用户删除优先（spec 6.2）——
        # 源会话/文档删除时同步撤销场景资源保留期并立即清理证据
        self._scenario_resource_ledger = scenario_resource_ledger
        self._clock = clock or (
            lambda: datetime.now(timezone.utc)
        )
        self._converge_wait_seconds = converge_wait_seconds
        self._deletion_retry_delay_seconds = deletion_retry_delay_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._loop_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # 删除事件入口（SessionService on_session_deleted handler 兼容）
    # ------------------------------------------------------------------

    async def handle_session_deleted(
        self, session_id: str, *, reason: str = ""
    ) -> Mapping[str, Any]:
        """会话删除传播：tombstone 先行，再按反向索引级联撤销/隐藏/清除。"""
        # tombstone 先行：即使级联中断（进程崩溃/部分失败），门禁即刻
        # 生效，重试事件幂等收敛
        await self._store.record_deletion_tombstone(
            {
                "subject_kind": "session",
                "subject_id": session_id,
                "reason": reason or "session_deleted",
            }
        )
        sample_ids = await self._collect_subject_samples(
            [("n-agent", "session", session_id, None)]
        )
        report = await self._cascade_samples(sample_ids)
        # 反馈级联两路：target 为会话本身的直接反馈，以及该会话名下
        # agent_run 主体的反馈（evaluation_service 的 human 反馈以
        # agent_run 为 target，TARGET_KINDS 允许；会话删除后这些反馈
        # 同样不得出现在当前视图/统计读路径）
        feedback_ids = list(
            await self._store.list_feedback_ids_by_subject(session_id)
        )
        for agent_run_id in await self._store.list_evidence_subject_ids(
            sample_ids, "agent_run"
        ):
            feedback_ids.extend(
                await self._store.list_feedback_ids_by_subject(
                    agent_run_id, target_kind="agent_run"
                )
            )
        feedback_ids = list(dict.fromkeys(feedback_ids))
        # T18/M4：被删会话的 human result 行（feedback_id 关联、job_id
        # NULL，不在样本 job 收集范围）一并 tombstone——否则
        # query_results_page 等分页读路径仍可见
        tombstone_payloads: list[dict[str, Any]] = []
        tombstoned_feedback = 0
        for feedback_id in feedback_ids:
            tombstone_payloads.append(
                {
                    "subject_kind": "feedback",
                    "subject_id": feedback_id,
                    "reason": reason or "session_deleted",
                }
            )
            result_id = await self._store.get_result_id_by_feedback_id(
                feedback_id
            )
            if result_id is not None:
                tombstone_payloads.append(
                    {
                        "subject_kind": "result",
                        "subject_id": result_id,
                        "reason": "subject_deleted",
                    }
                )
        # T18/N1：合并为单事务批量写入（幂等去重），消除 N+1 逐条事务；
        # inserted 标记保证重复删除事件不重复计数
        flags = await self._store.record_deletion_tombstones_batch(
            tombstone_payloads
        )
        report["feedback_tombstones"] = sum(
            1
            for payload, flag in zip(tombstone_payloads, flags)
            if flag and payload["subject_kind"] == "feedback"
        )
        report["result_tombstones"] = (
            report.get("result_tombstones", 0)
            + sum(
                1
                for payload, flag in zip(tombstone_payloads, flags)
                if flag and payload["subject_kind"] == "result"
            )
        )
        # plan T4：场景资源撤销（用户删除优先，保留期立即失效）
        report["scenario_resources"] = await self._revoke_scenario_resources(
            "session", session_id
        )
        return {
            "subject_kind": "session",
            "subject_id": session_id,
            **report,
        }

    async def handle_document_deleted(
        self,
        document_id: str,
        document_revision: str,
        *,
        reason: str = "",
    ) -> Mapping[str, Any]:
        """文档版本删除传播：同时覆盖检索快照与使用该文档的 Agent 快照。

        tombstone 按 (document, document_id, revision) 落库：迟到样本携带
        该文档版本（含新 retrieval_id）被 store 写入门禁拒绝；其他版本
        不受影响。"""
        await self._store.record_deletion_tombstone(
            {
                "subject_kind": "document",
                "subject_id": document_id,
                "subject_revision": document_revision,
                "reason": reason or "document_deleted",
            }
        )
        # 检索快照（n-kb producer）与 Agent 快照（n-agent producer）的
        # document 条目都指向同一文档版本；T3 早期适配器可能写
        # 'document_revision' 类别，一并按词表变体收集
        sample_ids = await self._collect_subject_samples(
            [
                ("n-kb", "document", document_id, document_revision),
                ("n-kb", "document_revision", document_id, document_revision),
                ("n-agent", "document", document_id, document_revision),
                ("n-agent", "document_revision", document_id, document_revision),
                # Unversioned legacy snapshots cannot prove they belong to a
                # different revision: any deletion of this document revokes them.
                ("n-kb", "document", document_id, ""),
                ("n-kb", "document_revision", document_id, ""),
                ("n-agent", "document", document_id, ""),
                ("n-agent", "document_revision", document_id, ""),
            ]
        )
        report = await self._cascade_samples(sample_ids)
        # plan T4：场景资源撤销（按文档版本精确匹配，其他版本不受影响）
        report["scenario_resources"] = await self._revoke_scenario_resources(
            "document", document_id, revision=document_revision
        )
        return {
            "subject_kind": "document",
            "subject_id": document_id,
            "subject_revision": document_revision,
            **report,
        }

    async def _revoke_scenario_resources(
        self, source_kind: str, source_id: str, *, revision: str | None = None,
    ) -> Mapping[str, Any] | None:
        """撤销挂接在该源上的场景资源保留（未接线账本时为 None）。"""
        ledger = self._scenario_resource_ledger
        if ledger is None:
            return None
        return await ledger.revoke_sources(
            source_kind, source_id, revision=revision
        )

    def set_scenario_resource_ledger(self, ledger: Any) -> None:
        """构造后注入场景资源账本（与删除传播互相引用，拆环注入）。"""
        self._scenario_resource_ledger = ledger

    async def _collect_subject_samples(
        self, lookups: Sequence[tuple[str, str, str, str | None]]
    ) -> list[str]:
        """按 (source_service, subject_kind, subject_id, revision) 组合
        收集样本 id 并去重（revision=None 表示任意版本）。"""
        collected: set[str] = set()
        for source_service, kind, subject_id, revision in lookups:
            ids = await self._store.list_sample_ids_by_subject(
                source_service,
                kind,
                subject_id,
                subject_revision=revision,
            )
            collected.update(ids)
        return sorted(collected)

    # ------------------------------------------------------------------
    # 级联（撤销 queued job / 隐藏 result+feedback / 登记删除任务 /
    # 改判无租约投递 / purge 证据正文）
    # ------------------------------------------------------------------

    async def _cascade_samples(
        self, sample_ids: Sequence[str]
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "samples": len(sample_ids),
            "revoked_job_ids": [],
            "result_tombstones": 0,
            "deletion_tasks": 0,
            "unlinked_deliveries": 0,
            "purged_snapshots": [],
            "cleared_sample_ids": [],
            "errors": [],
        }
        ids = list(sample_ids)
        if not ids:
            return report

        revoked = await self._store.revoke_queued_jobs_for_samples(ids)
        report["revoked_job_ids"] = list(revoked)

        # result 级 tombstone：当前视图（list_current_results / 窗口聚合）
        # 经既有 anti-join 不再展示；不可变历史行保留。批量单事务幂等
        # 写入（N1），inserted 标记用于重复删除事件的去重计数
        result_ids = await self._store.list_result_ids_for_samples(ids)
        result_flags = await self._store.record_deletion_tombstones_batch(
            [
                {
                    "subject_kind": "result",
                    "subject_id": result_id,
                    "reason": "subject_deleted",
                }
                for result_id in result_ids
            ]
        )
        report["result_tombstones"] += sum(result_flags)

        # 已预登记 external_id 的投递登记持久外部删除任务（按全部预登记
        # id 删除；尚未发送的投递由 claim/freeze 门禁阻断，任务多余登记
        # 由 not_found + 重复回读核对收敛为 done）。重复删除事件幂等：
        # 按 (sink, external_id, result_id) 精确存在性校验（T18/Mi5——
        # 不经 list_deletion_tasks 全量集合，不受 limit 截断影响）
        deliveries = await self._store.list_deliveries_for_samples(ids)
        for delivery in deliveries:
            external_id = delivery.get("external_id")
            if not external_id:
                continue
            existing = await self._store.find_deletion_task(
                sink=delivery["sink"],
                subject_kind="result",
                subject_id=delivery["result_id"],
                external_id=external_id,
            )
            if existing is not None:
                continue
            await self._store.save_deletion_task(
                {
                    "sink": delivery["sink"],
                    "subject_kind": "result",
                    "subject_id": delivery["result_id"],
                    "external_id": external_id,
                    "result_id": delivery["result_id"],
                }
            )
            report["deletion_tasks"] += 1

        # 无租约的 pending/retry_wait 投递改判 unlinked（持租约在途投递
        # 不动：verified 门禁阻断 + 外部删除任务收敛）
        unlinked = await self._store.unlink_deliveries_for_samples(
            ids, error_type="subject_deleted"
        )
        report["unlinked_deliveries"] = unlinked

        # 逐样本隔离清除证据正文：单样本故障不中断级联（tombstone 已使
        # 其内容不可达；重试删除事件幂等补齐）
        deleted_set = set(ids)
        for sample_id in ids:
            try:
                await self._purge_sample_snapshot(sample_id, deleted_set, report)
            except Exception as exc:
                logger.warning(
                    "evaluation deletion purge failed for sample %s: %s",
                    sample_id,
                    exc,
                )
                report["errors"].append(
                    {"sample_id": sample_id, "error": repr(exc)}
                )
        return report

    async def _purge_sample_snapshot(
        self,
        sample_id: str,
        deleted_set: set[str],
        report: dict[str, Any],
    ) -> None:
        sample = await self._store.load_sample(sample_id)
        snapshot_ref = sample.get("snapshot_ref") or ""
        if not snapshot_ref:
            return
        # 共享内容文件保护：同一内容寻址文件被未删除主体引用时不删文件，
        # 仅清除被删样本的引用行（其余引用行的正文可读性不受影响）。
        # 登记行（evidence_store 经 save_sample 落的 evidence:* 引用行，
        # 24 项三方审阅 #7）不是业务样本：不构成"共享存活"，且随 purge
        # 一并清除引用——否则删除传播后正文被登记行永久钉住（与到期
        # 撤销 sweep 的登记行清除语义一致）
        refs = await self._store.list_samples_by_snapshot_ref(snapshot_ref)

        def _is_registration(row: Mapping[str, Any]) -> bool:
            return str(row.get("producer_event_id") or "").startswith(
                "evidence:"
            )

        shared_with_live = any(
            row["sample_id"] not in deleted_set and not _is_registration(row)
            for row in refs
        )
        # T18/N4：先 CAS 清除引用行再 purge 文件——缩小"文件已删而引用
        # 行未清"的悬挂窗口（load_snapshot 的登记行 fail-closed 校验本就
        # 先于文件读取，此处仅是审计口径一致性优化）
        cleared = await self._store.clear_sample_snapshot_ref(
            sample_id, snapshot_ref
        )
        if cleared:
            report["cleared_sample_ids"].append(sample_id)
        for row in refs:
            if _is_registration(row):
                await self._store.clear_sample_snapshot_ref(
                    row["sample_id"], snapshot_ref
                )
        if not shared_with_live and self._evidence_store is not None:
            purged = await self._evidence_store.purge_snapshot(snapshot_ref)
            if purged:
                report["purged_snapshots"].append(snapshot_ref)

    # ------------------------------------------------------------------
    # 外部删除任务执行（先禁新发送 -> 有界收敛 -> 删除 -> 回读）
    # ------------------------------------------------------------------

    async def run_external_deletions_once(self) -> Mapping[str, Any]:
        """执行一轮外部删除任务（幂等；常驻循环与手动触发共用）。

        按 spec 14 顺序：先禁止新发送（disable_sink），有界等待在途请求
        收敛（wait_inflight），再按全部已预登记 external_id 删除并回读，
        最后恢复出口（enable_sink）。后端不可用（未注册删除端或调用异
        常）保留任务，不宣称外部已删除。

        收敛边界（T18/M1）：wait_inflight 仅进程内可见——超时未收敛或
        未注入出口闸时，本轮对该 sink 的任务一律延后（deletion_pending，
        error_type=inflight_not_converged，下一轮重试）；已删主体的发送
        由 claim/freeze/verified 三处 store 门禁兜底阻断。enable_sink 在
        finally 中执行（处理中崩溃也恢复出口；进程级崩溃由 worker 启动
        期按 SWEEP_DISABLE_REASON 归因自愈）。"""
        report: dict[str, Any] = {
            "done": 0,
            "deletion_pending": 0,
            "deferred": 0,
            "unavailable_sinks": [],
        }
        # 状态过滤 SQL 下推（24 项三方审阅 #10）：扫描窗口（LIMIT 1000）
        # 不得被历史 done 行挤占——否则累计 1000 条完成记录后新 pending
        # 任务永久饥饿
        tasks = await self._store.list_deletion_tasks(
            statuses=("pending", "deletion_pending"), limit=1000
        )
        now = self._clock()
        active = [
            task
            for task in tasks
            if task["status"] in ("pending", "deletion_pending")
        ]
        # T18/N3：仅含未到期（deferred）任务的 sink 不做出口开关翻动
        actionable_by_sink: dict[str, list[Mapping[str, Any]]] = {}
        deferred_by_sink: dict[str, int] = {}
        for task in active:
            if self._task_deferred(task, now):
                deferred_by_sink[task["sink"]] = (
                    deferred_by_sink.get(task["sink"], 0) + 1
                )
                continue
            actionable_by_sink.setdefault(task["sink"], []).append(task)
        report["deferred"] = sum(deferred_by_sink.values())
        for sink, sink_tasks in sorted(actionable_by_sink.items()):
            deleter = self._external_deleters.get(sink)
            if deleter is None:
                # 后端不可用：任务保留原状，等待删除端注册后收敛
                report["unavailable_sinks"].append(sink)
                continue
            if self._delivery_worker is not None:
                try:
                    # disable 纳入 try/finally：disable 中途抛错也保证
                    # enable 兜底（进程级崩溃由启动期归因自愈兜底）
                    await self._delivery_worker.disable_sink(
                        sink, reason=SWEEP_DISABLE_REASON
                    )
                    converged = await self._delivery_worker.wait_inflight(
                        self._converge_wait_seconds
                    )
                    if not converged:
                        # T18/M1：进程内在途未收敛——本轮不执行删除，全部
                        # 延后一轮（复用回读重复核对收敛；已删主体发送由
                        # store 门禁兜底）
                        for task in sink_tasks:
                            await self._store.update_deletion_task(
                                task["task_id"],
                                status="deletion_pending",
                                error_type="inflight_not_converged",
                                next_retry_at=None,
                                bump_attempt=True,
                            )
                            report["deletion_pending"] += 1
                        continue
                    for task in sink_tasks:
                        outcome = await self._process_deletion_task(
                            deleter, task, now
                        )
                        if outcome is not None:
                            report[outcome] = report.get(outcome, 0) + 1
                finally:
                    await self._delivery_worker.enable_sink(sink)
            else:
                # 未注入出口闸（单进程嵌入/测试场景）：直接处理
                for task in sink_tasks:
                    outcome = await self._process_deletion_task(
                        deleter, task, now
                    )
                    if outcome is not None:
                        report[outcome] = report.get(outcome, 0) + 1
        return report

    def _task_deferred(self, task: Mapping[str, Any], now: datetime) -> bool:
        """deletion_pending 任务是否尚未到期（N3：到期判断前移到 runner，
        便于按 sink 预过滤、避免无效出口开关）。"""
        if task["status"] != "deletion_pending" or not task.get(
            "next_retry_at"
        ):
            return False
        try:
            due = datetime.fromisoformat(task["next_retry_at"])
        except ValueError:
            return False
        return due > now

    async def _process_deletion_task(
        self,
        deleter: ExternalDeleter,
        task: Mapping[str, Any],
        now: datetime,
    ) -> str | None:
        """单任务执行；返回报告计数键（done/deletion_pending/deferred）。

        语义（spec 14）：
        - 未知发送结果（网络异常/unknown）保留 deletion_pending 退避重试；
        - 显式删除确认且回读缺席 -> done；
        - 第一次 404 不作结论：以连续缺席轮次收敛（T18/M2——连续性由
          last_error_type==awaiting_repeat_confirmation 判定，与全局
          attempt_count 解耦：read_back_present/delete_unknown 等中间
          状态重置连续性，混合历史下不得绕过重复核对）；
        - 回读仍存在 -> deletion_pending（read_back_present）；
        - 到期判断已前移 runner（N3）。"""
        task_id = task["task_id"]
        external_id = task.get("external_id")
        if not external_id:
            # 预登记空位（external_id 未知）：无法定位外部对象，保留
            # pending 等待投递冻结补全（T18/N2：已标记过则不重复写库——
            # 无退避语义，每轮空转重写只会噪声翻新 updated_at）
            if task.get("last_error_type") != "external_id_missing":
                await self._store.update_deletion_task(
                    task_id, error_type="external_id_missing"
                )
            return None

        error_type: str
        try:
            outcome = await deleter.delete_external(external_id)
            if outcome not in DELETER_OUTCOMES:
                outcome = "unknown"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "external deletion attempt failed (sink task=%s "
                "external_id=%s): %s",
                task_id,
                external_id,
                exc,
            )
            outcome = "unknown"

        if outcome == "unknown":
            # 未知发送结果：保留 deletion_pending 重复核对，绝不宣称完成
            await self._mark_deletion_pending(
                task_id, "delete_unknown", now
            )
            return "deletion_pending"

        try:
            read_back = await deleter.read_back(external_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "external deletion read-back failed (task=%s "
                "external_id=%s): %s",
                task_id,
                external_id,
                exc,
            )
            await self._mark_deletion_pending(
                task_id, "read_back_unknown", now
            )
            return "deletion_pending"

        if read_back is not None:
            # 回读仍存在：删除未生效，保留 deletion_pending 重试
            await self._mark_deletion_pending(
                task_id, "read_back_present", now
            )
            return "deletion_pending"

        if outcome == "deleted":
            # 显式删除被外部确认且回读已缺席
            await self._store.update_deletion_task(
                task_id,
                status="done",
                error_type=None,
                next_retry_at=None,
                bump_attempt=True,
            )
            return "done"

        # not_found：第一次 404 不作结论——迟到写入可能尚未可见，以连续
        # 缺席轮次收敛（T18/M2：仅当上一轮已处于 awaiting_repeat_
        # confirmation 时连续性 +1，否则重置为第一轮；混合历史不得绕过）
        consecutive_absent = (
            1
            if task.get("last_error_type") != "awaiting_repeat_confirmation"
            else 2
        )
        if consecutive_absent >= CONFIRM_ABSENT_RUNS:
            await self._store.update_deletion_task(
                task_id,
                status="done",
                error_type=None,
                next_retry_at=None,
                bump_attempt=True,
            )
            return "done"
        await self._mark_deletion_pending(
            task_id, "awaiting_repeat_confirmation", now
        )
        return "deletion_pending"

    async def _mark_deletion_pending(
        self, task_id: str, error_type: str, now: datetime
    ) -> None:
        next_retry_at = (
            now + timedelta(seconds=self._deletion_retry_delay_seconds)
        ).isoformat()
        await self._store.update_deletion_task(
            task_id,
            status="deletion_pending",
            error_type=error_type,
            next_retry_at=next_retry_at,
            bump_attempt=True,
        )

    # ------------------------------------------------------------------
    # 常驻重试循环（生命周期托管由 T20 组合根决定；手动触发可不用）
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动常驻删除任务重试循环（幂等）。"""
        if self._loop_task is not None:
            return
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._retry_loop())
        logger.info("EvaluationDeletionService retry loop started")

    async def stop(self) -> None:
        """停止常驻循环（幂等；在途一轮执行等待收敛）。"""
        if self._stop_event is not None:
            self._stop_event.set()
        loop_task = self._loop_task
        self._loop_task = None
        if loop_task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(loop_task),
                    timeout=self._poll_interval_seconds + 5,
                )
            except asyncio.TimeoutError:
                loop_task.cancel()
                await asyncio.gather(loop_task, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "EvaluationDeletionService loop stopped with error"
                )
        self._stop_event = None
        logger.info("EvaluationDeletionService retry loop stopped")

    async def _retry_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.run_external_deletions_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 常驻循环单轮失败不退出（下一轮重试），不阻塞业务
                logger.exception("external deletion round failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass
