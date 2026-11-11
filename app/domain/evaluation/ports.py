# app/domain/evaluation/ports.py
"""评估子域端口（spec 5.1）。

Domain 拥有端口契约，Infrastructure 适配实现，组合根注入。全部为
async Protocol；本模块不得 import application / infrastructure /
interfaces，也不依赖任何 SDK。尚未在本任务建模的载荷以 Mapping /
str 表达，由后续任务在保持端口归属不变的前提下细化。
"""
from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from app.domain.evaluation.models import (
    EvidenceSnapshot,
    EvaluationTarget,
    EvaluatorDefinition,
)


@runtime_checkable
class EvaluationStore(Protocol):
    """评估账本端口：sample / job / result 的持久化与查询。"""

    async def save_sample(self, sample: Mapping[str, Any]) -> str:
        """持久化评估样本，返回 sample_id；幂等键冲突由实现声明。"""
        ...

    async def save_job(self, job: Mapping[str, Any]) -> str:
        """持久化评估作业，返回 job_id。"""
        ...

    async def save_result(self, result: Mapping[str, Any]) -> str:
        """持久化评估结果（终态不可变），返回 result_id。"""
        ...

    async def load_result(self, result_id: str) -> Mapping[str, Any]:
        """按 result_id 读取结果；不存在时由实现声明错误语义。"""
        ...


@runtime_checkable
class EvidenceStore(Protocol):
    """证据快照端口：先写文件（临时文件 + fsync + 原子 rename），再在
    DB 事务登记引用；未引用文件由带 TTL 的受限回收处理。"""

    async def save_snapshot(
        self, target: EvaluationTarget, snapshot: EvidenceSnapshot
    ) -> str:
        """持久化快照并登记引用，返回 snapshot_ref。"""
        ...

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot:
        """按 snapshot_ref 读取快照。"""
        ...


@runtime_checkable
class Evaluator(Protocol):
    """评估器端口：deterministic / llm_judge / human 的统一执行入口。

    返回受控输出 Mapping（value / reason / evidence_refs 等），其 value
    必须能通过 validate_evaluation_value 按定义校验。
    """

    async def evaluate(
        self, definition: EvaluatorDefinition, snapshot: EvidenceSnapshot
    ) -> Mapping[str, Any]:
        ...


@runtime_checkable
class EvaluationTargetRunner(Protocol):
    """目标执行端口（execute 模式）：以冻结配置重新调用目标并产出证据。"""

    async def run_target(
        self, target: EvaluationTarget, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """执行目标一次，返回包含真实业务结果与副作用证明的受控载荷。"""
        ...


@runtime_checkable
class ScoreSink(Protocol):
    """分数出口端口（spec 11/13）：Langfuse 等外部 sink 的投影与投递适配。

    投影（project）构造冻结 payload 与 external_id；投递（send）只接受
    已冻结 payload，同一投递重试必须复用同一 external_id 与 payload；
    2xx 仅代表 accepted，verified 需回读完全匹配（ID/目标/类型/value/
    config）。

    分类失败契约（spec 13 表，供可恢复投递调度器识别）：dead_letter 类
    异常必须携带 ``delivery_outcome="dead_letter"``（如 409/4xx schema
    冲突、ScoreConfig 漂移、账本数据缺陷；error_type 区分 schema_conflict/
    auth/non_finite_value/empty_categorical/illegal_value_type），可重试
    类异常必须携带 ``delivery_outcome="retry"`` 与 ``error_type``
    （network/rate_limit/timeout）；两者都不得把密钥写进异常消息。
    project 的 unlinked 判定不抛异常，返回 ``{"outcome": "unlinked"}``
    （目标无真实身份或未采样时不创建悬空 Score，也不建 ScoreConfig）。
    """

    async def project(
        self, context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """构造投递投影。输入为受控 Mapping（result/target/definition/
        linkability/projection_version）；返回::

            {"outcome": "deliverable", "external_id": ..., "payload": {...}}
            {"outcome": "unlinked"}
        """
        ...

    async def send(self, payload: Mapping[str, Any]) -> str:
        """发送已冻结 payload（同 payload 重试幂等），2xx 返回
        external_id（accepted 语义）。"""
        ...

    async def read_back(self, external_id: str) -> Mapping[str, Any] | None:
        """按 external_id 回读 Score；不存在（404）返回 None。"""
        ...

    async def verify(
        self,
        payload: Mapping[str, Any],
        read_back: Mapping[str, Any] | None,
    ) -> bool:
        """回读核验：同 ID、目标、类型、value 与 config 完全匹配才为
        True；read_back 为 None 一律 False。"""
        ...


@runtime_checkable
class DeliveryStore(Protocol):
    """投递调度存储端口（spec 13）：可恢复投递队列的 claim/lease、冻结
    payload 登记与状态迁移。

    实现为 SQLiteEvaluationStore（T9 扩展）；Application/组合根只依赖本
    Protocol 形状，不依赖 Infrastructure 具体类。"""
    async def claim_next_delivery(
        self,
        claimant: str,
        lease_seconds: int = 300,
        *,
        sinks: Sequence[str] | None = None,
        now: Any = None,
    ) -> Mapping[str, Any] | None:
        """CAS 领取一条到期可投递的 delivery（pending 且租约空闲，或
        retry_wait 已到期）；返回含最新 lease_token 的行视图，无则
        None。``sinks`` 限定可领取的 sink 名（未注册适配器的 sink 保持
        pending，OTel 语义独立）。"""
        ...

    async def get_delivery(self, delivery_id: str) -> Mapping[str, Any]:
        """按 delivery_id 读取投递行。"""
        ...

    async def list_deliveries(
        self, result_id: str | None = None
    ) -> list[Mapping[str, Any]]:
        """列出投递（可按 result 过滤）。"""
        ...

    async def load_result(self, result_id: str) -> Mapping[str, Any]:
        """读取结果行（投影输入）。"""
        ...

    async def get_job(self, job_id: str) -> Mapping[str, Any]:
        """读取 job 行（delivery -> sample 定位）。"""
        ...

    async def load_sample(self, sample_id: str) -> Mapping[str, Any]:
        """读取 sample 行（target 身份来源）。"""
        ...

    async def get_definition(
        self, definition_id: str, version: str
    ) -> Mapping[str, Any]:
        """读取定义行（ScoreConfig schema 与 name 语义来源）。"""
        ...

    async def freeze_delivery_payload(
        self,
        delivery_id: str,
        *,
        external_id: str,
        payload: Mapping[str, Any],
        now: Any = None,
    ) -> Mapping[str, Any]:
        """发送前冻结 payload（先登记再发送）：持久化 payload_json 与
        payload_hash 并登记 external_id；重复冻结同内容幂等，内容冲突
        抛 EvaluationConflictError（冻结不可变）。"""
        ...

    async def schedule_delivery_retry(
        self,
        delivery_id: str,
        *,
        error_type: str,
        next_retry_at: Any,
        lease_token: str | None = None,
        now: Any = None,
    ) -> Mapping[str, Any]:
        """记录一次退避重试：状态 -> retry_wait、登记 error_type 与
        next_retry_at、retry_count += 1（真实网络重试计数）。
        ``lease_token`` 提供时执行 CAS（陈旧 worker 的重试调度被拒绝）。"""
        ...

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
    ) -> Mapping[str, Any]:
        """投递状态迁移（经 Domain state.py 校验）。"""
        ...
