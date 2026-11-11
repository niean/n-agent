# app.application.evaluation_worker 用例测试（plan T6）。
#
# S1 失败测试：fake Evaluator + 真实 SQLiteEvaluationStore（tmp DB）。
# 覆盖 plan T6 S1 全部要点与第三评审补充：
# - claim/lease：领取置 running + lease token；heartbeat 续租；过期租约可
#   重新 claim（claim marker 追加）；旧 token 提交被拒（无 result 落库）
# - Judge 重试上限（spec 6.4）：网络/限流/超时最多 +2 次、格式错误最多
#   修复 1 次、共享 3 次调用总上限；退避预计超过 job 总期限 → error 终结；
#   每次调用写 attempt 行（请求/实际模型/usage/cost/起止/error_type）
# - 成功提交：result + 终态 job + 各 sink delivery（pending）同事务；定义
#   版本锁定（job 钉住 definition_version，后注册新版本不影响在途 job）
# - 低分不重试：boolean False / 数值低于阈值仍 succeeded
# - skipped 语义：missing_reference / not_applicable / no_answer /
#   evidence_missing / budget_exhausted 均为 skipped result（非 error 非 0 分）
# - 取消：cancel 后即使 evaluator 返回也不提交 succeeded，job 终态 cancelled
# - 崩溃恢复：新 worker 可 claim 过期租约 job；并发双重处理恰一个 result
#   终态（不承诺 Provider 调用 exactly-once）
# - 并发：LLM 并发默认 1（信号量），C 类上限 4
# - 预算（spec 13）：claim 时事务预留覆盖所有重试；默认禁用；USD 日预算
#   必须显式提供单次估价（无定价改用 token/call ceiling）；未知费用保守
#   结算；并发争抢恰一赢家；重试计入预留；deterministic 不消耗预算；
#   单调用 60s 超时与共享 job 180s 期限分别测试（注入短时限，不 sleep 等待）
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

import pytest

from app.application.evaluation_service import (
    DefinitionKey,
    EvaluationService,
    SubmitSampleRequest,
)
from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import (
    EvaluatorDefinition,
    EvaluationTarget,
    EvidenceSnapshot,
)
from app.application.evaluation_worker import MAX_TOTAL_ATTEMPTS
from app.infrastructure.evaluation.store import SQLiteEvaluationStore

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc


@pytest.fixture
def store(tmp_path):
    return SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))


@pytest.fixture
def service(store):
    return EvaluationService(store, enabled=True)


class FakeEvidenceStore:
    """内存 EvidenceStore fake：按 ref 存取 EvidenceSnapshot。"""

    def __init__(self):
        self._snapshots: dict[str, EvidenceSnapshot] = {}

    def put(self, ref: str, snapshot: EvidenceSnapshot) -> str:
        self._snapshots[ref] = snapshot
        return ref

    async def save_snapshot(self, target, snapshot) -> str:
        ref = f"snapshots/{uuid4().hex}"
        return self.put(ref, snapshot)

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot:
        try:
            return self._snapshots[snapshot_ref]
        except KeyError:
            raise EvaluationNotFoundError(
                f"snapshot not found: {snapshot_ref}"
            ) from None


@pytest.fixture
def evidence():
    return FakeEvidenceStore()


class FakeClock:
    """可手动推进的时钟（lease 过期/重新 claim 测试用，不 sleep）。"""

    def __init__(self, start: datetime):
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        self._now = self._now + timedelta(**kwargs)


class JudgeError(Exception):
    """携带 error_type 分类标签的 evaluator 异常（T7 适配器契约形状）。"""

    def __init__(self, error_type: str):
        super().__init__(error_type)
        self.error_type = error_type


def ok(value: Any = 0.9, **extra) -> dict[str, Any]:
    """成功输出映射：value + 实际模型 + usage + cost。"""
    payload: dict[str, Any] = {
        "value": value,
        "model": "judge-x",
        "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "cost_usd": "0.0200",
        "explanation": "fine",
    }
    payload.update(extra)
    return payload


def skipped_outcome(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "skip_reason": reason}


class FakeEvaluator:
    """Evaluator fake：按序返回 outcomes（异常原样抛出）；记录每次调用。"""

    def __init__(self, outcomes: list[Any]):
        self._outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.on_call: Any = None  # 同步或 async 回调，进入 evaluate 时触发
        self.slow_seconds: float = 0.0
        self.concurrent = 0
        self.max_concurrent = 0

    async def evaluate(self, definition, snapshot) -> Mapping[str, Any]:
        self.calls.append({"definition": definition, "snapshot": snapshot})
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.on_call is not None:
                result = self.on_call()
                if asyncio.iscoroutine(result):
                    await result
            if self.slow_seconds:
                await asyncio.sleep(self.slow_seconds)
            if not self._outcomes:
                raise AssertionError("unexpected extra evaluator call")
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.concurrent -= 1


class HeartbeatCountingStore:
    """store 代理：统计 heartbeat_job 调用次数，其余委托内部 store。"""

    def __init__(self, inner):
        self._inner = inner
        self.heartbeat_calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def heartbeat_job(self, *args, **kwargs):
        self.heartbeat_calls += 1
        return await self._inner.heartbeat_job(*args, **kwargs)


def make_llm_definition(**overrides) -> EvaluatorDefinition:
    defaults: dict[str, Any] = dict(
        definition_id="agent.answer_correctness",
        version="v1",
        metric_name="agent.answer_correctness",
        target_kind="agent_run",
        method="llm_judge",
        required_fields=(),
        evidence_policy="snapshot",
        output_type="number",
        range_or_categories=(0.0, 1.0),
        direction="higher_is_better",
        material_hash={},
        judge_model="judge-model-x",
        judge_params={},
        timeout_seconds=60,
        budget=None,
        ci_admissible=False,
    )
    defaults.update(overrides)
    return EvaluatorDefinition(**defaults)


def make_deterministic_definition(metric_name: str, **overrides) -> EvaluatorDefinition:
    defaults: dict[str, Any] = dict(
        definition_id=metric_name,
        version="v1",
        metric_name=metric_name,
        target_kind="retrieval",
        method="deterministic",
        required_fields=(),
        evidence_policy="snapshot",
        output_type="number",
        range_or_categories=(0.0, 1.0),
        direction="higher_is_better",
        material_hash={},
        judge_model=None,
        judge_params={},
        timeout_seconds=60,
        budget=None,
        ci_admissible=False,
    )
    defaults.update(overrides)
    return EvaluatorDefinition(**defaults)


def make_worker(store, evaluator, *, evidence_store=None, clock=None, **overrides):
    from app.application.evaluation_worker import EvaluationWorker

    defaults: dict[str, Any] = dict(
        claimant="test-worker",
        lease_seconds=30,
        heartbeat_interval_seconds=None,
        poll_interval_seconds=0.01,
        job_total_deadline_seconds=180.0,
        retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return EvaluationWorker(
        store,
        evaluator=evaluator,
        evidence_store=(
            evidence_store if evidence_store is not None else _FALLBACK_EVIDENCE
        ),
        clock=clock,
        **defaults,
    )


# llm 测试用例未显式给 content 时的兜底快照库（ref 唯一，无跨用例干扰）
_FALLBACK_EVIDENCE = FakeEvidenceStore()


async def create_job(
    store,
    service,
    definition: EvaluatorDefinition,
    *,
    content: Mapping[str, Any] | None = None,
    evidence: FakeEvidenceStore | None = None,
    service_name: str = "n-agent",
    target_kind: str = "agent_run",
) -> str:
    """注册定义并提交一个展开 job 的样本（总是附带证据快照；llm_judge
    无快照会被 worker 跳过为 evidence_missing，因此测试一律有快照），
    返回 job_id。"""
    await service.register_definition(definition)
    target_evidence = evidence if evidence is not None else _FALLBACK_EVIDENCE
    snapshot = EvidenceSnapshot(
        content=dict(content) if content is not None else {},
        redaction_version="redaction.v1",
    )
    snapshot_ref = target_evidence.put(f"snapshots/{uuid4().hex}", snapshot)
    subject_id = f"subject-{uuid4().hex[:6]}"
    target = EvaluationTarget(
        service=service_name,
        kind=target_kind,
        subject_id=subject_id,
        subject_revision="rev-1",
        retrieval_id=subject_id if target_kind == "retrieval" else None,
    )
    outcome = await service.submit_sample(
        SubmitSampleRequest(
            producer_event_id=f"evt-{uuid4().hex[:12]}",
            service=service_name,
            target=target,
            snapshot_ref=snapshot_ref,
            snapshot_hash="a" * 64 if snapshot_ref else "",
            definitions=(
                DefinitionKey(definition.definition_id, definition.version),
            ),
        )
    )
    assert len(outcome.job_ids) == 1
    return outcome.job_ids[0]


async def result_for_job(store, job_id: str) -> Mapping[str, Any] | None:
    rows = await store.list_current_results(limit=1000)
    matches = [r for r in rows if r["job_id"] == job_id]
    return matches[0] if matches else None


async def call_rows(store, job_id: str) -> list[Mapping[str, Any]]:
    """已完成的调用 attempt 行（ended_at 非空；claim marker 行无 ended_at）。"""
    attempts = await store.list_attempts(job_id)
    return [a for a in attempts if a["ended_at"] is not None]


async def wait_for_terminal(store, job_ids, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    terminal = {"succeeded", "skipped", "error", "cancelled"}
    while loop.time() < deadline:
        rows = [await store.get_job(j) for j in job_ids]
        if all(r["status"] in terminal for r in rows):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("jobs did not reach terminal state in time")


# ---------------------------------------------------------------------------
# claim / lease
# ---------------------------------------------------------------------------


class TestClaimLease:
    async def test_claim_sets_running_with_lease_token(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        worker = make_worker(store, FakeEvaluator([]))

        claim = await worker.claim()

        assert claim["job_id"] == job_id
        assert claim["status"] == "running"
        assert claim["lease_token"]
        row = await store.get_job(job_id)
        assert row["status"] == "running"
        assert row["lease_token"] == claim["lease_token"]
        assert row["lease_owner"] == "test-worker"
        assert row["lease_expiry"] is not None

    async def test_heartbeat_renews_lease(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        worker = make_worker(store, FakeEvaluator([]))
        claim = await worker.claim()
        expiry_before = (await store.get_job(job_id))["lease_expiry"]

        assert await worker.heartbeat(claim) is True

        expiry_after = (await store.get_job(job_id))["lease_expiry"]
        assert expiry_after > expiry_before

    async def test_heartbeat_stale_token_rejected(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        start = datetime(2026, 11, 11, tzinfo=_UTC)
        clock = FakeClock(start)
        worker_a = make_worker(store, FakeEvaluator([]), clock=clock, claimant="a")
        claim_a = await worker_a.claim()
        # 时钟推进后租约过期，worker_b 重新领取
        clock.advance(minutes=10)
        worker_b = make_worker(store, FakeEvaluator([]), clock=clock, claimant="b")
        claim_b = await worker_b.claim()
        assert claim_b["job_id"] == job_id
        assert claim_b["lease_token"] != claim_a["lease_token"]

        assert await worker_a.heartbeat(claim_a) is False
        assert await worker_b.heartbeat(claim_b) is True

    async def test_expired_lease_reclaim_appends_claim_marker(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        start = datetime(2026, 11, 11, tzinfo=_UTC)
        clock = FakeClock(start)
        worker_a = make_worker(store, FakeEvaluator([]), clock=clock, claimant="a")
        await worker_a.claim()
        clock.advance(seconds=60)
        worker_b = make_worker(store, FakeEvaluator([]), clock=clock, claimant="b")

        claim_b = await worker_b.claim()

        assert claim_b["job_id"] == job_id
        assert claim_b["attempt_count"] == 2
        attempts = await store.list_attempts(job_id)
        assert [a["attempt_index"] for a in attempts] == [0, 1]

    async def test_stale_token_submission_rejected_no_result(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        start = datetime(2026, 11, 11, tzinfo=_UTC)
        clock = FakeClock(start)
        fake = FakeEvaluator([ok(0.9)])
        worker_a = make_worker(store, fake, clock=clock, claimant="a")
        job = await worker_a.claim()

        def steal():
            # evaluator 调用期间：时钟推进使租约过期，"thief" 重新领取
            clock.advance(minutes=10)
            return store.claim_next_job("thief", 30, now=clock())

        fake.on_call = steal
        await worker_a.process(job)  # 旧 token 提交被拒，worker 不崩溃

        assert await result_for_job(store, job_id) is None
        row = await store.get_job(job_id)
        assert row["status"] == "running"
        assert row["lease_token"] != job["lease_token"]


# ---------------------------------------------------------------------------
# Judge 重试上限（spec 6.4）
# ---------------------------------------------------------------------------


class TestRetryLimits:
    async def test_transient_errors_retry_max_two_then_error(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator(
            [JudgeError("network"), JudgeError("rate_limit"), JudgeError("timeout"), ok()]
        )
        worker = make_worker(store, fake)

        await worker.run_once()

        assert len(fake.calls) == 3  # 首次 + 最多 2 次重试
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        # #14：judge error 有正式 error 结果行（结果列表/分数收集/OTel
        # 出口不再缺失败事实）
        error_result = await result_for_job(store, job_id)
        assert error_result is not None
        assert error_result["status"] == "error"
        rows = await call_rows(store, job_id)
        assert [r["error_type"] for r in rows] == ["network", "rate_limit", "timeout"]

    async def test_format_error_single_repair_then_stop(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([JudgeError("format_error"), JudgeError("format_error"), ok()])
        worker = make_worker(store, fake)

        await worker.run_once()

        assert len(fake.calls) == 2  # 首次 + 1 次格式修复
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        rows = await call_rows(store, job_id)
        assert [r["error_type"] for r in rows] == ["format_error", "format_error"]

    async def test_retry_class_locked_transient_then_format_denied(
        self, store, service, evidence
    ):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([JudgeError("network"), JudgeError("format_error"), ok()])
        worker = make_worker(store, fake)

        await worker.run_once()

        # 首次可重试失败锁定 transient 类：format_error 不再获得修复调用
        assert len(fake.calls) == 2
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        rows = await call_rows(store, job_id)
        assert [r["error_type"] for r in rows] == ["network", "format_error"]

    async def test_retry_class_locked_format_then_transient_denied(
        self, store, service, evidence
    ):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([JudgeError("format_error"), JudgeError("network"), ok()])
        worker = make_worker(store, fake)

        await worker.run_once()

        # 首次可重试失败锁定 format 类（1 次修复即用尽）：transient 不再重试
        assert len(fake.calls) == 2
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        rows = await call_rows(store, job_id)
        assert [r["error_type"] for r in rows] == ["format_error", "network"]

    async def test_success_after_retries_records_each_attempt(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([JudgeError("network"), ok(0.8)])
        worker = make_worker(store, fake)

        await worker.run_once()

        job = await store.get_job(job_id)
        assert job["status"] == "succeeded"
        rows = await call_rows(store, job_id)
        assert len(rows) == 2
        assert [r["error_type"] for r in rows] == ["network", None]
        success_row = rows[1]
        assert success_row["judge_model"] == "judge-x"
        assert success_row["usage_known"] == 1
        assert success_row["input_tokens"] == 10
        assert success_row["output_tokens"] == 5
        assert success_row["total_tokens"] == 15
        assert Decimal(success_row["cost_usd"]) == Decimal("0.0200")
        assert success_row["started_at"] and success_row["ended_at"]
        assert success_row["judge_request"]["definition_id"] == definition.definition_id
        assert success_row["judge_request"]["definition_version"] == "v1"

    async def test_backoff_prediction_exceeds_deadline_errors_without_extra_call(
        self, store, service, evidence
    ):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([JudgeError("network"), ok()])
        worker = make_worker(
            store, fake, retry_backoff_seconds=10.0, job_total_deadline_seconds=1.0
        )

        await worker.run_once()

        assert len(fake.calls) == 1  # 退避预计越过总期限，不再调用
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        assert job["error_type"] == "deadline_exceeded"
        # 费用与 attempts 保留
        assert len(await call_rows(store, job_id)) == 1

    async def test_deadline_prediction_returns_result_tuple_no_unpack_error(
        self, store, service, evidence, caplog
    ):
        """M2（质量复审）：deadline 预测分支必须按契约返回二元组
        (None, None)——`return None` 会在 _execute_claimed 二元解包处抛
        TypeError，被 process 宽捕获记为意外失败日志（误导性堆栈噪声）。"""
        import logging

        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([JudgeError("network"), ok()])
        worker = make_worker(
            store, fake, retry_backoff_seconds=10.0, job_total_deadline_seconds=1.0
        )

        with caplog.at_level(
            logging.ERROR, logger="app.application.evaluation_worker"
        ):
            await worker.run_once()

        job = await store.get_job(job_id)
        assert job["status"] == "error"
        assert job["error_type"] == "deadline_exceeded"
        # process 不得出现意外异常日志（TypeError 解包失败即此处暴露）
        assert caplog.records == []

    async def test_per_call_timeout_classified_and_retried_to_cap(
        self, store, service, evidence
    ):
        definition = make_llm_definition(timeout_seconds=1)
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok(), ok(), ok(), ok()])
        fake.slow_seconds = 1.3  # 单调用超时 1s（与 job 总期限 180s 独立测试）
        worker = make_worker(store, fake)

        await worker.run_once()

        assert len(fake.calls) == 3
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        rows = await call_rows(store, job_id)
        assert [r["error_type"] for r in rows] == ["timeout", "timeout", "timeout"]

    async def test_job_deadline_caps_call_and_terminates_error(self, store, service, evidence):
        definition = make_llm_definition(timeout_seconds=60)  # 单调用超时不触发
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok(), ok()])
        fake.slow_seconds = 1.0
        worker = make_worker(store, fake, job_total_deadline_seconds=0.3)

        await worker.run_once()

        assert len(fake.calls) == 1  # job 总期限封顶调用时长，随后 error 终结
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        assert job["error_type"] == "deadline_exceeded"
        rows = await call_rows(store, job_id)
        assert [r["error_type"] for r in rows] == ["timeout"]

    async def test_attempt_cap_exceeded_without_new_call(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        # 崩溃残留：已记录 3 次调用（crash 循环保护）
        for _ in range(3):
            await store.append_call_attempt(
                job_id, started_at="2026-11-11T00:00:00+00:00",
                ended_at="2026-11-11T00:00:01+00:00", error_type="network",
            )
        fake = FakeEvaluator([ok()])
        worker = make_worker(store, fake)

        await worker.run_once()

        assert len(fake.calls) == 0
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        assert job["error_type"] == "attempt_cap_exceeded"


# ---------------------------------------------------------------------------
# 成功提交：result + 终态 job + deliveries 单事务；定义版本锁定
# ---------------------------------------------------------------------------


class TestSuccessfulSubmission:
    async def test_commit_result_job_deliveries_single_transaction(
        self, store, service, evidence
    ):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok(0.9)])
        worker = make_worker(
            store,
            fake,
            sinks=({"sink": "langfuse", "projection_version": "v1"},),
            # T8 起 llm_judge 结果 provenance 按校准状态判定；本用例注入
            # passed 校准状态以覆盖常规 automatic 路径
            calibration_status_provider=lambda d: "passed",
        )

        await worker.run_once()

        job = await store.get_job(job_id)
        assert job["status"] == "succeeded"
        result = await result_for_job(store, job_id)
        assert result is not None
        assert result["provenance"] == "automatic"
        assert result["metric_name"] == "agent.answer_correctness"
        assert result["metric_version"] == "v1"
        assert result["status"] == "value"
        assert result["typed_value"] == 0.9
        deliveries = await store.list_deliveries(result["result_id"])
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert delivery["sink"] == "langfuse"
        assert delivery["status"] == "pending"
        assert delivery["projection_version"] == "v1"
        assert delivery["payload_hash"]
        # 同 job/metric 恰一个正式 result
        rows = await store.list_current_results(limit=1000)
        assert sum(1 for r in rows if r["job_id"] == job_id) == 1

    async def test_commit_result_persists_evaluation_trace_identity(
        self, store, service, evidence, monkeypatch
    ):
        """#12：result 行冻结当前 evaluation.run Trace 身份，OTel 投影
        evaluation_trace 的数据来源。"""
        from app.application.evaluation_worker import EvaluationWorker

        monkeypatch.setattr(
            EvaluationWorker,
            "_evaluation_trace_identity",
            staticmethod(lambda: ("e" * 32, "f" * 16)),
        )
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        worker = make_worker(store, FakeEvaluator([ok(0.9)]))

        await worker.run_once()

        result = await result_for_job(store, job_id)
        assert result["evaluation_trace_id"] == "e" * 32
        assert result["evaluation_span_id"] == "f" * 16

    async def test_commit_error_result_persists_evaluation_trace_identity(
        self, store, service, evidence, monkeypatch
    ):
        from app.application.evaluation_worker import EvaluationWorker

        monkeypatch.setattr(
            EvaluationWorker,
            "_evaluation_trace_identity",
            staticmethod(lambda: ("e" * 32, "f" * 16)),
        )
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        worker = make_worker(
            store, FakeEvaluator([JudgeError("judge down")])
        )

        await worker.run_once()

        job = await store.get_job(job_id)
        assert job["status"] == "error"
        result = await result_for_job(store, job_id)
        assert result is not None
        assert result["status"] == "error"
        assert result["evaluation_trace_id"] == "e" * 32

    async def test_definition_version_locked_since_claim(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        hash_before = (await store.get_definition("agent.answer_correctness", "v1"))[
            "definition_hash"
        ]
        fake = FakeEvaluator([ok(0.9)])
        worker = make_worker(store, fake)

        # 在途 job 处理前注册新版本定义：job 仍按领取时锁定的 v1 执行
        await service.register_definition(
            make_llm_definition(
                version="v2",
                metric_name="agent.answer_correctness.v2",
                material_hash={},
            )
        )
        await worker.run_once()

        hash_after = (await store.get_definition("agent.answer_correctness", "v1"))[
            "definition_hash"
        ]
        assert hash_before == hash_after
        assert fake.calls[0]["definition"].version == "v1"
        result = await result_for_job(store, job_id)
        assert result["metric_version"] == "v1"

    async def test_low_scores_not_retried(self, store, service, evidence):
        boolean_definition = make_llm_definition(
            definition_id="agent.task_success",
            metric_name="agent.task_success",
            output_type="boolean",
            range_or_categories=(),
        )
        boolean_job = await create_job(store, service, boolean_definition)
        numeric_definition = make_llm_definition(
            definition_id="agent.answer_correctness",
            metric_name="agent.answer_correctness",
        )
        numeric_job = await create_job(store, service, numeric_definition)
        fake = FakeEvaluator([ok(False), ok(0.1)])
        worker = make_worker(store, fake)

        await worker.run_once()
        await worker.run_once()

        for job_id in (boolean_job, numeric_job):
            job = await store.get_job(job_id)
            assert job["status"] == "succeeded"
            assert len(await call_rows(store, job_id)) == 1  # 低分不重试
        boolean_result = await result_for_job(store, boolean_job)
        assert boolean_result["typed_value"] is False
        assert boolean_result["passed"] is False
        numeric_result = await result_for_job(store, numeric_job)
        assert numeric_result["typed_value"] == 0.1


# ---------------------------------------------------------------------------
# skipped 语义（缺数据不是 0 分）
# ---------------------------------------------------------------------------


class TestSkippedSemantics:
    async def test_evaluator_skipped_missing_reference(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([skipped_outcome("missing_reference")])
        worker = make_worker(store, fake)

        await worker.run_once()

        job = await store.get_job(job_id)
        assert job["status"] == "skipped"
        result = await result_for_job(store, job_id)
        assert result["status"] == "skipped"
        assert result["skip_reason"] == "missing_reference"
        assert result["typed_value"] is None
        assert result["passed"] is None

    async def test_evaluator_skipped_not_applicable(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([skipped_outcome("not_applicable")])
        worker = make_worker(store, fake)

        await worker.run_once()

        result = await result_for_job(store, job_id)
        assert result["status"] == "skipped"
        assert result["skip_reason"] == "not_applicable"

    async def test_deterministic_missing_qrels_is_missing_reference(
        self, store, service, evidence
    ):
        definition = make_deterministic_definition("kb.recall_at_5")
        job_id = await create_job(
            store,
            service,
            definition,
            content={"results": []},  # 无 qrels：缺参考答案
            evidence=evidence,
            target_kind="retrieval",
        )
        fake = FakeEvaluator([ok()])
        worker = make_worker(store, fake, evidence_store=evidence)

        await worker.run_once()

        assert fake.calls == []  # deterministic 不经注入 evaluator
        result = await result_for_job(store, job_id)
        assert result["status"] == "skipped"
        assert result["skip_reason"] == "missing_reference"

    async def test_deterministic_no_relevant_item_is_no_answer(
        self, store, service, evidence
    ):
        definition = make_deterministic_definition("kb.recall_at_5")
        job_id = await create_job(
            store,
            service,
            definition,
            content={
                "results": [{"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch1"}],
                "qrels": [
                    {"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch2", "grade": 0}
                ],
            },
            evidence=evidence,
            target_kind="retrieval",
        )
        worker = make_worker(store, FakeEvaluator([]), evidence_store=evidence)

        await worker.run_once()

        result = await result_for_job(store, job_id)
        assert result["status"] == "skipped"
        assert result["skip_reason"] == "no_answer"

    async def test_deterministic_recall_value(self, store, service, evidence):
        definition = make_deterministic_definition("kb.recall_at_5")
        job_id = await create_job(
            store,
            service,
            definition,
            content={
                "results": [
                    {"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch1"},
                    {"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch2"},
                ],
                "qrels": [
                    {"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch1", "grade": 2},
                    {"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch9", "grade": 1},
                ],
            },
            evidence=evidence,
            target_kind="retrieval",
        )
        worker = make_worker(
            store,
            FakeEvaluator([]),
            evidence_store=evidence,
            sinks=({"sink": "langfuse", "projection_version": "v1"},),
        )

        await worker.run_once()

        job = await store.get_job(job_id)
        assert job["status"] == "succeeded"
        result = await result_for_job(store, job_id)
        assert result["status"] == "value"
        assert result["typed_value"] == 0.5
        deliveries = await store.list_deliveries(result["result_id"])
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "pending"

    async def test_deterministic_task_success_missing_evidence(
        self, store, service, evidence
    ):
        definition = make_deterministic_definition(
            "agent.task_success", target_kind="agent_run"
        )
        job_id = await create_job(
            store,
            service,
            definition,
            content={"completion_reported": True},  # 无副作用证据
            evidence=evidence,
        )
        worker = make_worker(store, FakeEvaluator([]), evidence_store=evidence)

        await worker.run_once()

        result = await result_for_job(store, job_id)
        assert result["status"] == "skipped"
        assert result["skip_reason"] == "evidence_missing"

    async def test_deterministic_content_mismatch_is_error_not_crash(
        self, store, service, evidence
    ):
        definition = make_deterministic_definition("kb.recall_at_5")
        job_id = await create_job(
            store,
            service,
            definition,
            content={"qrels": [{"corpus_revision": "c1"}]},  # 条目缺 chunk_id/grade
            evidence=evidence,
            target_kind="retrieval",
        )
        worker = make_worker(store, FakeEvaluator([]), evidence_store=evidence)

        await worker.run_once()

        # 快照内容与指标契约不符：error 终结（非 skipped、非 0 分、不悬挂 running）
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        assert job["error_type"] == "deterministic_error"
        # #14：deterministic error 同样有 error 结果行（契约不符是可
        # 观测的失败事实，不再只留在 job 行）
        error_result = await result_for_job(store, job_id)
        assert error_result is not None
        assert error_result["status"] == "error"
        assert (await store.get_job(job_id))["lease_expiry"] is None


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


class TestCancellation:
    async def test_cancel_during_call_prevents_succeeded(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok(0.9)])
        worker = make_worker(store, fake)
        job = await worker.claim()

        async def cancel_during_call():
            await store.cancel_job(job_id)

        fake.on_call = cancel_during_call
        await worker.process(job)

        assert await result_for_job(store, job_id) is None
        job_row = await store.get_job(job_id)
        assert job_row["status"] == "cancelled"
        assert job_row["lease_token"] is None
        # 已发生的调用与费用保留（attempt 行仍在）
        assert len(await call_rows(store, job_id)) == 1


# ---------------------------------------------------------------------------
# 崩溃恢复
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    async def test_new_worker_claims_expired_lease_job_single_result(
        self, store, service, evidence
    ):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        # 模拟 worker 领取后崩溃：直接 store 领取（历史时间，租约早已过期）
        await store.claim_next_job("dead-worker", 30, now=datetime(2020, 1, 1, tzinfo=_UTC))
        fake = FakeEvaluator([ok(0.7)])
        worker = make_worker(store, fake)  # 新实例（真实当前时钟）

        await worker.run_once()

        job = await store.get_job(job_id)
        assert job["status"] == "succeeded"
        results = await store.list_current_results(limit=1000)
        assert sum(1 for r in results if r["job_id"] == job_id) == 1
        attempts = await store.list_attempts(job_id)
        indexes = [a["attempt_index"] for a in attempts]
        assert indexes == sorted(indexes)  # 崩溃 claim marker + 调用行有序追加

    async def test_concurrent_double_process_yields_single_result(
        self, store, service, evidence
    ):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        start = datetime(2026, 11, 11, tzinfo=_UTC)
        clock_a = FakeClock(start)
        worker_a = make_worker(store, FakeEvaluator([ok(0.5)]), clock=clock_a, claimant="a")
        job_a = await worker_a.claim()
        # 租约过期后 worker_b 重新领取同一 job（不承诺 exactly-once 调用）
        clock_b = FakeClock(start + timedelta(seconds=60))
        job_b = await store.claim_next_job("b", 30, now=clock_b())
        assert job_b["job_id"] == job_id
        worker_b = make_worker(store, FakeEvaluator([ok(0.5)]), clock=clock_b, claimant="b")

        await asyncio.gather(worker_a.process(job_a), worker_b.process(job_b))

        results = await store.list_current_results(limit=1000)
        assert sum(1 for r in results if r["job_id"] == job_id) == 1  # 终态 result 唯一
        job = await store.get_job(job_id)
        assert job["status"] == "succeeded"
        assert len(await call_rows(store, job_id)) == 2  # 两次真实调用都留痕


# ---------------------------------------------------------------------------
# 并发：LLM 并发默认 1，C 类上限 4
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_default_llm_concurrency_one_and_lower_bound(self, store):
        worker = make_worker(store, FakeEvaluator([]))
        assert worker.llm_concurrency == 1
        with pytest.raises(ValueError):
            make_worker(store, FakeEvaluator([]), llm_concurrency=0)
        # 数值上限由 domain validate_evaluation_config 单一来源
        # （C 并发 <= B 类 evaluation_worker_max_concurrency）；worker
        # 构造只要求 >= 1，B_max > 4 时 C∈5..B_max 合法
        high = make_worker(store, FakeEvaluator([]), llm_concurrency=16)
        assert high.llm_concurrency == 16

    async def test_llm_jobs_serialize_with_default_concurrency(self, store, service, evidence):
        definition = make_llm_definition()
        job_ids = [
            await create_job(store, service, definition),
            await create_job(store, service, definition),
        ]
        fake = FakeEvaluator([ok(), ok()])
        fake.slow_seconds = 0.05
        worker = make_worker(store, fake, poll_interval_seconds=0.01)

        await worker.start()
        await wait_for_terminal(store, job_ids)
        await worker.stop()

        assert fake.max_concurrent == 1

    async def test_llm_concurrency_two_runs_in_parallel(self, store, service, evidence):
        definition = make_llm_definition()
        job_ids = [
            await create_job(store, service, definition),
            await create_job(store, service, definition),
        ]

        class RendezvousEvaluator:
            """两个 evaluate 同时在途才放行（并发 1 下会超时失败）。"""

            def __init__(self, expected: int):
                self.expected = expected
                self.arrived = 0
                self.max_concurrent = 0
                self._all_arrived = asyncio.Event()

            async def evaluate(self, definition_arg, snapshot):
                self.arrived += 1
                self.max_concurrent = max(self.max_concurrent, self.arrived)
                if self.arrived >= self.expected:
                    self._all_arrived.set()
                try:
                    await asyncio.wait_for(self._all_arrived.wait(), timeout=2.0)
                finally:
                    self.arrived -= 1
                return ok()

        evaluator = RendezvousEvaluator(expected=2)
        worker = make_worker(
            store, evaluator, llm_concurrency=2, poll_interval_seconds=0.01
        )

        await worker.start()
        await wait_for_terminal(store, job_ids)
        await worker.stop()

        assert evaluator.max_concurrent == 2


# ---------------------------------------------------------------------------
# 预算（spec 13）
# ---------------------------------------------------------------------------


class TestBudget:
    async def test_budget_disabled_by_default(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok()])
        worker = make_worker(store, fake)

        await worker.run_once()

        assert (await store.get_job(job_id))["status"] == "succeeded"
        totals = await store.get_daily_budget("2026-11-11")
        assert totals["reserved_calls"] == 0
        assert totals["settled_calls"] == 0
        assert Decimal(totals["reserved_usd"]) == 0

    async def test_no_price_requires_explicit_ceiling(self, store):
        from decimal import Decimal as D

        with pytest.raises(ValueError):
            make_worker(store, FakeEvaluator([]), daily_budget_usd=D("10.00"))
        with pytest.raises(ValueError):
            make_worker(store, FakeEvaluator([]), daily_token_ceiling=1000)
        ok_worker = make_worker(
            store,
            FakeEvaluator([]),
            daily_budget_usd=D("10.00"),
            per_call_cost_estimate=D("2.00"),
        )
        assert ok_worker is not None

    async def test_call_ceiling_contention_one_wins_one_budget_skipped(
        self, store, service, evidence
    ):
        definition = make_llm_definition()
        job_1 = await create_job(store, service, definition)
        job_2 = await create_job(store, service, definition)
        fake_1 = FakeEvaluator([ok()])
        fake_2 = FakeEvaluator([ok()])
        worker_1 = make_worker(store, fake_1, claimant="w1", daily_call_ceiling=3)
        worker_2 = make_worker(store, fake_2, claimant="w2", daily_call_ceiling=3)

        await asyncio.gather(worker_1.run_once(), worker_2.run_once())

        statuses = {
            job_1: (await store.get_job(job_1))["status"],
            job_2: (await store.get_job(job_2))["status"],
        }
        assert sorted(statuses.values()) == ["skipped", "succeeded"]
        winner = job_1 if statuses[job_1] == "succeeded" else job_2
        loser = job_2 if winner == job_1 else job_1
        loser_result = await result_for_job(store, loser)
        assert loser_result["status"] == "skipped"
        assert loser_result["skip_reason"] == "budget_exhausted"
        # 并发 claim 顺序不定：按各 job 实际被哪个 claimant 领取（lease_owner）
        # 归属 fake，不按 worker 身份假设 claim 顺序
        winner_fake = (
            fake_1
            if (await store.get_job(winner))["lease_owner"] == "w1"
            else fake_2
        )
        loser_fake = fake_2 if winner_fake is fake_1 else fake_1
        assert len(winner_fake.calls) == 1
        assert loser_fake.calls == []  # 超限不调用
        assert await result_for_job(store, winner) is not None
        totals = await store.get_daily_budget(
            datetime.now(tz=_UTC).date().isoformat()
        )
        assert totals["reserved_calls"] == 0
        assert totals["settled_calls"] == 1

    async def test_retries_covered_by_reservation_then_exhausted(self, store, service, evidence):
        definition = make_llm_definition()
        job_1 = await create_job(store, service, definition)
        fake_1 = FakeEvaluator([JudgeError("network"), JudgeError("network"), ok()])
        worker = make_worker(
            store, fake_1, daily_call_ceiling=3, per_call_cost_estimate=Decimal("0.01")
        )

        await worker.run_once()

        assert (await store.get_job(job_1))["status"] == "succeeded"
        totals = await store.get_daily_budget(
            datetime.now(tz=_UTC).date().isoformat()
        )
        assert totals["settled_calls"] == 3  # 3 次调用全部计入预留覆盖范围
        assert totals["reserved_calls"] == 0
        # 预算用尽：下一个 job 领取时预留被拒 → skipped(budget_exhausted)
        job_2 = await create_job(store, service, definition)
        fake_2 = FakeEvaluator([ok()])
        worker_2 = make_worker(store, fake_2, daily_call_ceiling=3)

        await worker_2.run_once()

        assert fake_2.calls == []
        result = await result_for_job(store, job_2)
        assert result["skip_reason"] == "budget_exhausted"

    async def test_usd_budget_settles_known_cost(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok(cost_usd="1.5000")])
        worker = make_worker(
            store,
            fake,
            daily_budget_usd=Decimal("10.00"),
            per_call_cost_estimate=Decimal("2.00"),
        )

        await worker.run_once()

        assert (await store.get_job(job_id))["status"] == "succeeded"
        totals = await store.get_daily_budget(
            datetime.now(tz=_UTC).date().isoformat()
        )
        assert Decimal(totals["reserved_usd"]) == 0
        assert Decimal(totals["settled_usd"]) == Decimal("1.5")

    async def test_usd_budget_exhausted_at_claim(self, store, service, evidence):
        definition = make_llm_definition()
        job_1 = await create_job(store, service, definition)
        fake_1 = FakeEvaluator([ok(cost_usd="1.5000")])
        worker_1 = make_worker(
            store,
            fake_1,
            daily_budget_usd=Decimal("7.00"),
            per_call_cost_estimate=Decimal("2.00"),
        )
        await worker_1.run_once()
        assert (await store.get_job(job_1))["status"] == "succeeded"

        job_2 = await create_job(store, service, definition)
        fake_2 = FakeEvaluator([ok()])
        worker_2 = make_worker(
            store,
            fake_2,
            daily_budget_usd=Decimal("7.00"),
            per_call_cost_estimate=Decimal("2.00"),
        )
        await worker_2.run_once()

        # 已用 1.5 + 预留 6.0 = 7.5 > 7.00 → 领取时预留被拒
        assert fake_2.calls == []
        result = await result_for_job(store, job_2)
        assert result["skip_reason"] == "budget_exhausted"

    async def test_budget_rejected_job_does_not_settle_ledger(
        self, store, service, evidence
    ):
        """C1 回归：预留被拒的 job 终结后不得把预留计入 settled（否则每个
        被拒 job 永久虚增当日账本，最终把预算顶死）。"""
        definition = make_llm_definition()
        job_1 = await create_job(store, service, definition)
        await make_worker(
            store,
            FakeEvaluator([ok(cost_usd="1.5000")]),
            daily_budget_usd=Decimal("7.00"),
            per_call_cost_estimate=Decimal("2.00"),
            daily_token_ceiling=1000,
            per_call_token_estimate=100,
        ).run_once()
        date_key = datetime.now(tz=_UTC).date().isoformat()
        before = await store.get_daily_budget(date_key)

        job_2 = await create_job(store, service, definition)
        await make_worker(
            store,
            FakeEvaluator([ok()]),
            daily_budget_usd=Decimal("7.00"),
            per_call_cost_estimate=Decimal("2.00"),
            daily_token_ceiling=1000,
            per_call_token_estimate=100,
        ).run_once()

        assert (await store.get_job(job_2))["status"] == "skipped"
        after = await store.get_daily_budget(date_key)
        # 被拒 job 不结算：settled 维持 job_1 的实际值，reserved 不被释放
        assert Decimal(after["settled_usd"]) == Decimal(before["settled_usd"])
        assert after["settled_tokens"] == before["settled_tokens"]
        assert after["settled_calls"] == before["settled_calls"]
        assert Decimal(after["reserved_usd"]) == Decimal(before["reserved_usd"])

    async def test_unknown_cost_settles_conservatively(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok(cost_usd=None, usage=None)])
        worker = make_worker(
            store,
            fake,
            daily_budget_usd=Decimal("10.00"),
            per_call_cost_estimate=Decimal("2.00"),
        )

        await worker.run_once()

        assert (await store.get_job(job_id))["status"] == "succeeded"
        totals = await store.get_daily_budget(
            datetime.now(tz=_UTC).date().isoformat()
        )
        # 未知费用保守结算：整个预留视为已消耗，不得低估
        assert Decimal(totals["settled_usd"]) == Decimal("6.00")
        assert totals["reserved_usd"] == "0" or Decimal(totals["reserved_usd"]) == 0

    async def test_token_ceiling_reserve_and_settle(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok()])
        worker = make_worker(
            store,
            fake,
            daily_token_ceiling=100,
            per_call_token_estimate=30,
        )

        await worker.run_once()

        assert (await store.get_job(job_id))["status"] == "succeeded"
        totals = await store.get_daily_budget(
            datetime.now(tz=_UTC).date().isoformat()
        )
        assert totals["reserved_tokens"] == 0
        assert totals["settled_tokens"] == 15

    async def test_deterministic_not_budget_constrained(self, store, service, evidence):
        definition = make_deterministic_definition("kb.recall_at_5")
        job_id = await create_job(
            store,
            service,
            definition,
            content={
                "results": [{"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch1"}],
                "qrels": [
                    {"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch1", "grade": 1}
                ],
            },
            evidence=evidence,
            target_kind="retrieval",
        )
        worker = make_worker(
            store, FakeEvaluator([]), evidence_store=evidence, daily_call_ceiling=0
        )

        await worker.run_once()

        assert (await store.get_job(job_id))["status"] == "succeeded"
        totals = await store.get_daily_budget(
            datetime.now(tz=_UTC).date().isoformat()
        )
        assert totals["reserved_calls"] == 0
        assert totals["settled_calls"] == 0


# ---------------------------------------------------------------------------
# 生命周期 start/stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_heartbeat_interval_must_be_less_than_lease(self, store):
        with pytest.raises(ValueError):
            make_worker(
                store,
                FakeEvaluator([]),
                lease_seconds=30,
                heartbeat_interval_seconds=30,
            )
        with pytest.raises(ValueError):
            make_worker(
                store,
                FakeEvaluator([]),
                lease_seconds=30,
                heartbeat_interval_seconds=60,
            )
        # None（禁用心跳）不受约束
        disabled = make_worker(
            store, FakeEvaluator([]), lease_seconds=30, heartbeat_interval_seconds=None
        )
        assert disabled is not None

    async def test_unexpected_exception_fails_job_as_worker_error(
        self, store, service, evidence
    ):
        """M1 回归：执行前置步骤（定义解析等）抛意外异常时，job 必须兜底
        置为 error 终态，不得悬挂 running 被反复重领。"""
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)

        class FailingDefinitionStore:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                return getattr(self._inner, name)

            async def get_definition(self, *args, **kwargs):
                raise RuntimeError("definition lookup boom")

        fake = FakeEvaluator([ok()])
        worker = make_worker(FailingDefinitionStore(store), fake)

        returned = await worker.run_once()  # 不上抛、循环可继续

        assert returned == job_id
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        assert job["error_type"] == "worker_error"
        assert fake.calls == []
        assert await result_for_job(store, job_id) is None

    async def test_start_processes_jobs_and_stop_is_clean(self, store, service, evidence):
        definition = make_deterministic_definition("kb.recall_at_5")
        content = {
            "results": [{"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch1"}],
            "qrels": [
                {"corpus_revision": "c1", "document_revision": "d1", "chunk_id": "ch1", "grade": 1}
            ],
        }
        job_ids = [
            await create_job(
                store, service, definition, content=content, evidence=evidence,
                target_kind="retrieval",
            ),
            await create_job(
                store, service, definition, content=content, evidence=evidence,
                target_kind="retrieval",
            ),
        ]
        worker = make_worker(store, FakeEvaluator([]), evidence_store=evidence)

        await worker.start()
        await wait_for_terminal(store, job_ids)
        await worker.stop()
        await worker.stop()  # 幂等

        for job_id in job_ids:
            assert (await store.get_job(job_id))["status"] == "succeeded"

    async def test_heartbeat_issued_during_slow_job(self, store, service, evidence):
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition)
        fake = FakeEvaluator([ok()])
        fake.slow_seconds = 0.15
        counting = HeartbeatCountingStore(store)
        worker = make_worker(
            counting, fake, lease_seconds=30, heartbeat_interval_seconds=0.02
        )

        await worker.run_once()

        assert counting.heartbeat_calls >= 1
        assert (await store.get_job(job_id))["status"] == "succeeded"


class TestResultEventEmission:
    async def test_commit_emits_gen_ai_evaluation_result(self, store, service, evidence, monkeypatch):
        """T22 收口（T10 emit_result 接线）：结果落库后发布
        gen_ai.evaluation.result 事件（Loki 消费），此前从未被调用。"""
        import app.application.evaluation_telemetry as telemetry

        emitted: list[dict] = []

        def _capture_emit(**kwargs):
            emitted.append(kwargs)
            return "emitted"

        monkeypatch.setattr(telemetry, "emit_result", _capture_emit)
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition, content={
            "request": "q", "answer": "a",
        })
        worker = make_worker(store, FakeEvaluator([ok(0.9)]))
        await worker.run_once()
        await wait_for_terminal(store, [job_id])
        assert len(emitted) == 1
        payload = emitted[0]
        assert payload["status"] == "succeeded"
        assert payload["metric_name"] == definition.metric_name
        result = await result_for_job(store, job_id)
        assert payload["result_id"] == result["result_id"]
        target = payload["target"]
        assert target["service"] == "n-agent"

    async def test_event_emission_failure_isolated(self, store, service, evidence, monkeypatch):
        """发射抛错被隔离：结果落库不受影响，job 正常终态。"""
        import app.application.evaluation_telemetry as telemetry

        def _boom(**kwargs):
            raise RuntimeError("otel down")

        monkeypatch.setattr(telemetry, "emit_result", _boom)
        definition = make_llm_definition()
        job_id = await create_job(store, service, definition, content={
            "request": "q", "answer": "a",
        })
        worker = make_worker(store, FakeEvaluator([ok(0.9)]))
        await worker.run_once()
        await wait_for_terminal(store, [job_id])
        result = await result_for_job(store, job_id)
        assert result is not None and result["status"] == "value"


# ---------------------------------------------------------------------------
# 30 项人工评审修复轮（#10/#11/#14/#26/#27）
# ---------------------------------------------------------------------------


async def test_loop_claim_bounded_by_inflight_concurrency(store, service):
    """#10：在途任务达到执行并发上限时暂停领取——job 租约不再耗在
    信号量排队上（此前 claim 无界，租约过期被反复重领）。"""
    await service.register_definition(make_llm_definition())
    job_ids = []
    for _ in range(3):
        job_id = await create_job(store, service, make_llm_definition())
        job_ids.append(job_id)
    evaluator = FakeEvaluator([ok()] * 3)
    evaluator.slow_seconds = 0.25
    worker = make_worker(store, evaluator, llm_concurrency=1)
    await worker.start()
    try:
        await asyncio.sleep(0.08)
        counts = await store.experiment_job_status_counts("")
        # 第一个 job 已领取执行，其余仍在排队（未被无界预领取）
        statuses = [
            (await store.get_job(j))["status"] for j in job_ids
        ]
        assert statuses.count("running") <= 1
        assert statuses.count("queued") >= 2
    finally:
        await worker.stop()


async def test_call_ceiling_provider_hot_read(store):
    """#11：日调用上限经 provider 热读——预留量随 provider 当前值生效；
    provider 返回 None（显式不设上限）时无需预留。"""
    current = {"ceiling": 5}
    worker = make_worker(
        store,
        FakeEvaluator([]),
        daily_call_ceiling=100,
        daily_call_ceiling_provider=lambda: current["ceiling"],
        per_call_token_estimate=1000,
    )
    definition = make_llm_definition()
    reservation = worker._budget_reservation(definition, call_ceiling=worker._current_call_ceiling())
    assert reservation is not None
    assert reservation[2] == MAX_TOTAL_ATTEMPTS
    current["ceiling"] = 0  # provider 热读返回 0 -> None（不设上限）
    assert worker._current_call_ceiling() is None
    reservation = worker._budget_reservation(
        definition, call_ceiling=worker._current_call_ceiling()
    )
    assert reservation is None


async def test_format_error_attempt_row_keeps_usage_model(service, store):
    """#27：格式错误同样落完整 attempt 行（usage/实际模型来自异常
    携带），费用结算不因解析失败丢账。"""
    from app.infrastructure.evaluation.judge_provider import JudgeFormatError

    await service.register_definition(make_llm_definition())
    job_id = await create_job(store, service, make_llm_definition())
    exc = JudgeFormatError(
        "Judge 输出不是合法 JSON: bad",
        usage={"input_tokens": 9, "output_tokens": 3, "total_tokens": 12},
        model="actual-judge-model",
    )
    evaluator = FakeEvaluator([exc, ok()])
    worker = make_worker(store, evaluator)
    await worker.run_once()
    attempts = await store.list_attempts(job_id)
    real_calls = [a for a in attempts if a["ended_at"] is not None]
    first = real_calls[0]
    assert first["error_type"] == "format_error"
    assert first["usage_known"]
    assert first["judge_model"] == "actual-judge-model"
    assert first["total_tokens"] == 12


async def test_retry_after_pulls_deadline_forward(service, store):
    """#26：Judge 限流 Retry-After 参与退避与超期预测——Retry-After 大于
    job 总期限时立即 deadline_exceeded 终结（不空等长退避）。"""
    from app.infrastructure.evaluation.judge_provider import JudgeTransientError

    await service.register_definition(make_llm_definition())
    job_id = await create_job(store, service, make_llm_definition())
    exc = JudgeTransientError(
        "Judge 触发限流（HTTP 429）", error_type="rate_limit", retry_after=3600
    )
    evaluator = FakeEvaluator([exc, ok()])
    worker = make_worker(
        store, evaluator, job_total_deadline_seconds=5.0, retry_backoff_seconds=0.0
    )
    started = time.monotonic()
    await worker.run_once()
    elapsed = time.monotonic() - started
    assert (await store.get_job(job_id))["status"] == "error"
    assert (await store.get_job(job_id))["error_type"] == "deadline_exceeded"
    assert elapsed < 30  # 未真的 sleep 3600s


async def test_evaluation_run_scope_wraps_job_execution(store, service, monkeypatch):
    """#14：job 执行在 evaluation.run Trace 作用域内（Link 指向样本目标
    身份；emit_result 作用域内自动携带 evaluation_trace_id）。"""
    from app.application import evaluation_telemetry

    calls: list[dict] = []

    real_scope = evaluation_telemetry.evaluation_run_scope

    @contextlib.contextmanager
    def recording_scope(target=None, *, metric_profile=None, mode="online"):
        calls.append(
            {
                "target": target,
                "metric_profile": metric_profile,
                "mode": mode,
            }
        )
        with real_scope(target, metric_profile=metric_profile, mode=mode) as handle:
            yield handle

    monkeypatch.setattr(
        evaluation_telemetry, "evaluation_run_scope", recording_scope
    )

    await service.register_definition(make_llm_definition())
    target = EvaluationTarget(
        service="n-agent",
        kind="agent_run",
        subject_id="run-scope",
        subject_revision="rev-1",
        trace_id=None,
        span_id=None,
    )
    snapshot = EvidenceSnapshot(content={}, redaction_version="redact-v1")
    ref = await _FALLBACK_EVIDENCE.save_snapshot(target, snapshot)
    sample = await service.submit_sample(
        SubmitSampleRequest(
            producer_event_id="evt-scope-1",
            service="n-agent",
            target=target,
            snapshot_ref=ref,
            snapshot_hash=snapshot.content_sha256,
            definitions=(DefinitionKey("agent.answer_correctness", "v1"),),
        )
    )
    evaluator = FakeEvaluator([ok()])
    worker = make_worker(store, evaluator)
    await worker.run_once()
    assert len(calls) == 1
    assert calls[0]["target"]["subject_id"] == "run-scope"
    assert calls[0]["mode"] == "online"
    assert calls[0]["metric_profile"] == "agent.answer_correctness@v1"


async def test_tool_contract_without_evidence_slots_is_skipped(
    store, service, evidence
):
    """24 项三方审阅 #17：快照无 tool_calls/tool_traces 槽位时，
    agent.tool_contract 按 skipped(evidence_missing) 处理，不得因字段
    缺省空集合虚假通过。"""
    from app.application.evaluation_worker import _tool_contract_value
    from app.domain.evaluation.models import EvaluationValue

    value = _tool_contract_value({"metric": "agent.tool_contract"})
    assert value.status == "skipped"
    assert value.skip_reason is not None
    # 有 tool_calls 槽位（即使为空）：既有空契约语义不变
    ok_value = _tool_contract_value({"tool_calls": [], "allowed_tools": []})
    assert ok_value.status == "value"
