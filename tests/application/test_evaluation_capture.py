"""app/application/evaluation_capture 单元测试（plan T16 S1）。

覆盖：
- 确定性抽样：H(policy_version, service, subject_id)/2^256 < p；默认 p=0 不抽样；
  同 subject 同决策（不随重试重抽）；故障定向采样独立 cohort。
- 快照内容契约（spec 6.3/7.1）：必含字段 + 禁含字段（SDK 对象/凭据/完整
  system prompt/受禁截图不进入快照）+ 脱敏版本。
- 捕获/入队异常不回滚业务：capture_error/queue_full 计数递增。
- 有界后台队列：满时拒绝并计数，不阻塞业务；后台消费者持久登记样本。
- 覆盖率计数接口：eligible/selected/captured/capture_error/queue_full。
- Knowledge VO 兼容扩展（可选 evaluation_context 与稳定版本字段）与
  真实消费片段传播（禁止再查询 KB 重建）。
- ToolService 工具轨迹证据（最小附加钩子）。
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from fractions import Fraction
from typing import Any

import httpx
import pytest

from app.domain.agent_run import RunBinding, bind_run
from app.domain.evaluation.models import EvaluationTarget
from app.domain.knowledge import (
    KnowledgeBackendSearchRequest,
    KnowledgeBase,
    KnowledgeBaseType,
    KnowledgeSearchResult,
    KnowledgeSnippet,
)
from app.domain.tool import (
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
)

from app.application.evaluation_capture import (
    CAPTURE_COUNTERS,
    DEFAULT_BUFFER_MAX_RECORDS,
    EvaluationCaptureBuffer,
    EvaluationCaptureCounters,
    PersistingCaptureCounters,
    EvaluationCaptureQueue,
    EvaluationCaptureService,
    EvaluationSamplingPolicy,
    _default_redact,
    capture_session_key,
    derive_agent_run_subject,
    jsonable_excerpt,
    retrieval_record_from_result,
)
from app.application.evaluation_service import DefinitionKey, SubmitSampleResult
from app.application.knowledge_service import KnowledgeService
from app.application.tool_service import ToolService
from app.application.evaluation_telemetry import PRODUCER_MEASURES


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEvidenceStore:
    def __init__(self, fail_first: int = 0):
        self.saved: list[tuple[EvaluationTarget, object, str]] = []
        self._fail_first = fail_first

    async def save_snapshot(self, target, snapshot):
        if self._fail_first > 0:
            self._fail_first -= 1
            raise RuntimeError("evidence store down")
        ref = f"snapshots/{len(self.saved):064x}.json"
        self.saved.append((target, snapshot, ref))
        return ref


class FakeEvaluationService:
    def __init__(self):
        self.requests = []
        # T22：模拟 store 按目标种类的定义解析结果（kind -> keys）
        self.definitions_by_kind: dict[str, tuple] = {}
        self.target_kind_queries: list[str] = []

    async def definitions_for_target(self, target_kind):
        self.target_kind_queries.append(target_kind)
        return self.definitions_by_kind.get(target_kind, ())

    async def submit_sample(self, request):
        self.requests.append(request)
        return SubmitSampleResult(sample_id=f"s-{len(self.requests)}", job_ids=())


class ExplodingQueue:
    """put_nowait 直接抛错的注入桩（异常隔离测试）。"""

    def put_nowait(self, item):
        raise RuntimeError("queue down")


def make_capture_service(
    *,
    rate: float = 1.0,
    failure_rate: float = 0.0,
    maxsize: int = 100,
    evidence_store=None,
    evaluation_service=None,
    redactor=None,
    policy_provider=None,
):
    return EvaluationCaptureService(
        policy=EvaluationSamplingPolicy(rate=rate, failure_rate=failure_rate),
        policy_provider=policy_provider,
        queue_maxsize=maxsize,
        evidence_store=evidence_store,
        evaluation_service=evaluation_service,
        redactor=redactor,
    )


async def wait_until(condition, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.01)
    return False


def make_kb_base() -> KnowledgeBase:
    return KnowledgeBase(
        id="kb-1",
        name="KB One",
        description="",
        base_type=KnowledgeBaseType.N_KB,
        base_url="http://nkb.example.com",
        dataset_id="ds-1",
        api_key_present=False,
        enabled=True,
        default_top_k=None,
        default_min_score=None,
        last_probe_status=None,
        last_probe_error=None,
        last_probed_at=None,
        created_at=None,
        updated_at=None,
    )


class FakeRetriever:
    """统计 search 调用次数的假检索器（断言禁止再查询 KB）。"""

    def __init__(self, result: KnowledgeSearchResult):
        self.result = result
        self.search_calls = 0

    async def probe(self, base, secret=None):
        return None

    async def search(self, base, request, secret=None):
        self.search_calls += 1
        return self.result


def make_kb_result(*, with_context: bool = True) -> KnowledgeSearchResult:
    context = None
    if with_context:
        from app.domain.knowledge import KnowledgeEvaluationContext

        context = KnowledgeEvaluationContext(
            retrieval_id="ret-123",
            corpus_revision="corpus-r1",
            trace_ref={"trace_id": "a" * 32, "span_id": "b" * 16, "sampled": True},
        )
    return KnowledgeSearchResult(
        kb_id="kb-1",
        kb_name="KB One",
        base_type=KnowledgeBaseType.N_KB,
        query="how to deploy",
        results=[
            KnowledgeSnippet(
                id="chunk-1",
                title="Deploy",
                content="deploy content",
                score=0.9,
                source="doc-1",
                metadata={},
                document_id="doc-1",
                document_revision="r2",
                chunk_id="chunk-1",
                chunk_content_hash="c" * 64,
            ),
            KnowledgeSnippet(
                id="chunk-2",
                title="Ops",
                content="ops content",
                score=0.8,
                source="doc-2",
                metadata={},
            ),
        ],
        evaluation_context=context,
    )


# ---------------------------------------------------------------------------
# 抽样
# ---------------------------------------------------------------------------


class TestSampling:
    def test_default_policy_never_selects(self):
        policy = EvaluationSamplingPolicy()
        assert policy.rate == 0.0
        for subject in ("s1", "s2", "s3", "s4"):
            assert policy.cohort_for("n-agent", subject) is None
            assert policy.cohort_for("n-agent", subject, failed=True) is None

    def test_rate_one_selects_all(self):
        policy = EvaluationSamplingPolicy(rate=1.0)
        for subject in ("s1", "s2", "s3"):
            assert policy.cohort_for("n-agent", subject) == "random"

    def test_sampling_is_deterministic_per_subject(self):
        policy = EvaluationSamplingPolicy(rate=0.5)
        decisions = {
            subject: policy.cohort_for("n-agent", subject)
            for subject in ("a", "b", "c", "d", "e", "f", "g", "h")
        }
        # 同 subject 同决策（重试不重抽）：与全新 policy 实例一致
        fresh = EvaluationSamplingPolicy(rate=0.5)
        for subject, decision in decisions.items():
            assert fresh.cohort_for("n-agent", subject) == decision
            # 多次调用同决策
            for _ in range(3):
                assert policy.cohort_for("n-agent", subject) == decision

    def test_sampling_matches_hash_formula(self):
        """锁定 spec 公式：H(policy_version, service, subject_id)/2^256 < p。"""
        policy = EvaluationSamplingPolicy(policy_version="pv-test", rate=0.5)
        for subject in ("x1", "x2", "x3", "x4", "x5", "x6"):
            digest = hashlib.sha256(
                f"pv-test|n-agent|{subject}".encode("utf-8")
            ).digest()
            h = int.from_bytes(digest, "big")
            expected = "random" if Fraction(h, 2**256) < Fraction(0.5) else None
            assert policy.cohort_for("n-agent", subject) == expected

    def test_failure_cohort_is_independent(self):
        policy = EvaluationSamplingPolicy(rate=0.0, failure_rate=1.0)
        assert policy.cohort_for("n-agent", "subj") is None
        assert policy.cohort_for("n-agent", "subj", failed=True) == "failure"

    def test_failure_cohort_precedes_random_label(self):
        # 两阈值都命中时只捕获一次，cohort 标记为 failure（定向优先）
        policy = EvaluationSamplingPolicy(rate=1.0, failure_rate=1.0)
        assert policy.cohort_for("n-agent", "subj", failed=True) == "failure"

    def test_no_resample_on_retry(self):
        """同一 subject 多次重试（多次 finalize）抽样决策不变。"""
        policy = EvaluationSamplingPolicy(rate=0.5)
        first = policy.cohort_for("n-agent", "run-subject", failed=False)
        for attempt in range(5):
            assert policy.cohort_for("n-agent", "run-subject", failed=False) == first

    def test_derive_agent_run_subject_stable_across_retries(self):
        messages = [{"role": "user", "content": "do the thing"}]
        a = derive_agent_run_subject("sess-1", messages)
        b = derive_agent_run_subject("sess-1", messages)
        assert a == b
        assert a.startswith("sess-1:")
        assert derive_agent_run_subject("sess-2", messages) != a


# ---------------------------------------------------------------------------
# 计数接口（对接 T10 producer gauge）
# ---------------------------------------------------------------------------


class TestCounters:
    def test_counter_fields_match_producer_measures(self):
        assert set(CAPTURE_COUNTERS) == set(PRODUCER_MEASURES)
        counters = EvaluationCaptureCounters()
        assert set(counters.snapshot()) == set(PRODUCER_MEASURES)
        assert all(value == 0 for value in counters.snapshot().values())

    def test_counters_are_cumulative(self):
        counters = EvaluationCaptureCounters()
        counters.bump("eligible")
        counters.bump("eligible")
        counters.bump("capture_error")
        snapshot = counters.snapshot()
        assert snapshot["eligible"] == 2
        assert snapshot["capture_error"] == 1

    def test_counter_rejects_unknown_field(self):
        counters = EvaluationCaptureCounters()
        with pytest.raises(ValueError):
            counters.bump("not_a_counter")


# ---------------------------------------------------------------------------
# 快照内容契约
# ---------------------------------------------------------------------------


class TestSnapshotContent:
    def _capture(self, service: EvaluationCaptureService, **overrides):
        service.buffer.record_tool(
            "sess-1",
            {
                "call_id": "call-1",
                "tool": "calculator",
                "status": "success",
                "duration_ms": 5,
                "terminal": False,
                "result_excerpt": "3",
            },
        )
        service.buffer.record_retrieval("sess-1", {
            "kb_id": "kb-1",
            "query": "deploy",
            "retrieval_id": "ret-1",
            "corpus_revision": "corpus-r1",
            "chunks": [{
                "rank": 1,
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "document_revision": "r2",
                "content_hash": "c" * 64,
                "score": 0.9,
                "excerpt": "deploy content",
            }],
        })
        kwargs = dict(
            subject_id="sess-1:abcd",
            session_id="sess-1",
            run_id="run-1",
            query="what is 1+2",
            final_answer="result is 3",
            model="test-model",
            target_versions={"agent": "1.0"},
            truth_refs={"rubric": "must be 3"},
            failed=False,
            error=None,
            finish_reason="stop",
        )
        kwargs.update(overrides)
        service.capture_agent_run(**kwargs)

    def test_snapshot_contains_required_fields(self):
        service = make_capture_service(rate=1.0)
        self._capture(service)
        items = service.queue.queued_items()
        assert len(items) == 1
        content = items[0].snapshot.content
        for key in (
            "schema_version",
            "kind",
            "query",
            "final_answer",
            "tool_traces",
            "retrieval_chunks",
            "truth_refs",
            "model",
            "target_versions",
            "capture_completeness",
            "redaction_version",
            "sampling",
        ):
            assert key in content, f"快照缺少必含字段: {key}"
        assert content["query"] == "what is 1+2"
        assert content["final_answer"] == "result is 3"
        assert content["model"] == "test-model"
        assert content["redaction_version"]
        assert content["sampling"]["cohort"] == "random"
        # 真实消费的检索片段带稳定身份
        chunk = content["retrieval_chunks"][0]["chunks"][0]
        assert chunk["chunk_id"] == "chunk-1"
        assert chunk["document_id"] == "doc-1"
        assert chunk["document_revision"] == "r2"
        assert chunk["content_hash"] == "c" * 64
        # 允许的工具轨迹
        assert content["tool_traces"][0]["tool"] == "calculator"
        # 目标身份
        assert items[0].target.kind == "agent_run"
        assert items[0].target.service == "n-agent"

    def test_snapshot_builtin_patterns_apply_after_injected_redactor(self):
        """m5 回归锁定（T20 质量复审）：组合根注入 LogSanitizer（固定
        规则不含 ghp_/xoxb 等裸凭据形态）时，快照期仍叠加内建
        _default_redact 兜底——query/final_answer/error 三个直接字段不
        经 jsonable_excerpt 的登记层内建正则，裸 token 不得原样回显。"""
        from app.infrastructure.observability.logging import LogSanitizer

        from app.application.evaluation_capture import REDACTION_VERSION

        service = make_capture_service(
            rate=1.0, redactor=LogSanitizer().sanitize
        )
        raw_token = "ghp_" + "a" * 36
        self._capture(
            service,
            query=f"why is {raw_token} failing",
            final_answer=f"done xoxb-1234567890 and {raw_token}",
        )
        content = service.queue.queued_items()[0].snapshot.content
        assert "[redacted]" in content["final_answer"]
        assert "ghp_" not in content["final_answer"]
        assert "xoxb-" not in content["final_answer"]
        assert "[redacted]" in content["query"]
        assert raw_token not in content["query"]
        # 脱敏语义变化 bump 版本（质量复审 Minor）
        assert content["redaction_version"] == REDACTION_VERSION

    def test_snapshot_excludes_forbidden_content(self):
        service = make_capture_service(rate=1.0)
        service.buffer.record_tool("sess-1", {
            "call_id": "call-2",
            "tool": "leaky",
            "status": "success",
            "duration_ms": 1,
            "system_prompt": "you are a helpful assistant ... FULL PROMPT",
            "credentials": {"api_key": "sk-abc"},
            "screenshot": "base64png",
        })
        self._capture(service)
        content = service.queue.queued_items()[0].snapshot.content
        encoded = service.queue.queued_items()[0].snapshot.canonical_bytes.decode("utf-8")
        assert "system_prompt" not in content
        assert "credentials" not in content
        assert "screenshot" not in content
        assert "FULL PROMPT" not in encoded
        assert "sk-abc" not in encoded

    def test_snapshot_is_json_serializable_no_sdk_objects(self):
        service = make_capture_service(rate=1.0)
        service.buffer.record_tool("sess-1", {
            "call_id": "call-3",
            "tool": "weird",
            "status": "success",
            "duration_ms": 1,
            "result_excerpt": {"fallback": object()},
        })
        self._capture(service)
        snapshot = service.queue.queued_items()[0].snapshot
        encoded = snapshot.canonical_bytes.decode("utf-8")
        parsed = json.loads(encoded)
        assert isinstance(parsed, dict)
        assert parsed["kind"] == "agent_run"
        # SDK 对象已投影为稳定类型名（无内存地址，跨进程幂等）
        assert parsed["tool_traces"][0]["result_excerpt"] == {"fallback": "<object>"}
        assert "0x" not in encoded

    def test_snapshot_redacts_credentials(self):
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7r8"
        service = make_capture_service(rate=1.0)
        self._capture(service, final_answer=f"token {secret} leaked")
        content = service.queue.queued_items()[0].snapshot.content
        assert secret not in content["final_answer"]
        assert "[redacted]" in content["final_answer"]

    def test_redaction_failure_counts_capture_error_and_does_not_raise(self):
        def exploding_redactor(text):
            raise RuntimeError("redactor down")

        service = make_capture_service(rate=1.0, redactor=exploding_redactor)
        # 不抛错：捕获异常与业务隔离
        self._capture(service)
        assert service.counters.snapshot()["capture_error"] == 1
        assert service.counters.snapshot()["captured"] == 0

    def test_default_sampling_off_eligible_but_not_selected(self):
        service = make_capture_service(rate=0.0)
        self._capture(service)
        counters = service.counters.snapshot()
        assert counters["eligible"] == 1
        assert counters["selected"] == 0
        assert counters["captured"] == 0
        assert service.queue.queued_items() == []


# ---------------------------------------------------------------------------
# 异常隔离与有界队列
# ---------------------------------------------------------------------------


class TestExceptionIsolation:
    def test_enqueue_exception_never_rolls_back_business(self):
        service = make_capture_service(rate=1.0)
        service.queue = ExplodingQueue()
        service.capture_agent_run(
            subject_id="s:1",
            session_id="sess-1",
            query="q",
            final_answer="a",
            failed=False,
            finish_reason="stop",
        )
        counters = service.counters.snapshot()
        assert counters["capture_error"] == 1
        assert counters["queue_full"] == 0

    def test_queue_full_rejects_and_counts_without_blocking(self):
        service = make_capture_service(rate=1.0, maxsize=1)
        for _ in range(2):
            service.capture_agent_run(
                subject_id="s:1",
                session_id="sess-1",
                query="q",
                final_answer="a",
                failed=False,
                finish_reason="stop",
            )
        counters = service.counters.snapshot()
        assert counters["captured"] == 1
        assert counters["queue_full"] == 1

    async def test_business_hook_never_waits_for_consumer(self):
        started = asyncio.Event()
        release = asyncio.Event()
        handler_finished = asyncio.Event()

        async def blocked_handler(item):
            started.set()
            await release.wait()
            handler_finished.set()

        queue = EvaluationCaptureQueue(maxsize=10, handler=blocked_handler)
        service = make_capture_service(rate=1.0)
        service.queue = queue
        await queue.start()
        try:
            service.capture_agent_run(
                subject_id="s:1",
                session_id="sess-1",
                query="q",
                final_answer="a",
                failed=False,
                finish_reason="stop",
            )
            # capture_agent_run 是同步非阻塞返回；消费者被挂起也不影响调用方
            assert await wait_until(started.is_set)
            assert not handler_finished.is_set()
            assert service.counters.snapshot()["captured"] == 1
        finally:
            release.set()
            # 释放后消费者应恢复并完成该条目处理（单条失败不拖垮消费者）
            assert await wait_until(handler_finished.is_set)
            await queue.stop()
        assert queue.done


class TestBackgroundConsumer:
    async def test_consumer_persists_registration_for_worker(self):
        evidence = FakeEvidenceStore()
        evaluation = FakeEvaluationService()
        service = make_capture_service(
            rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
        )
        await service.start()
        try:
            service.capture_agent_run(
                subject_id="sess-1:abcd",
                session_id="sess-1",
                run_id="run-1",
                query="q",
                final_answer="a",
                model="m",
                failed=False,
                finish_reason="stop",
            )
            assert await wait_until(lambda: len(evaluation.requests) == 1)
            request = evaluation.requests[0]
            assert request.service == "n-agent"
            assert request.target.kind == "agent_run"
            assert request.target.subject_id == "sess-1:abcd"
            assert request.producer_event_id.startswith("agent-run:")
            assert request.sampling_policy_version
            # #24：抽样 cohort 贯通到样本登记（random；rate=1.0 全选随机）
            assert request.cohort == "random"
            assert request.snapshot_ref
            assert request.snapshot_hash
            assert request.schema_version
            # 快照文件已持久化（worker 可恢复）
            assert len(evidence.saved) == 1
        finally:
            await service.stop()

    async def test_consumer_error_is_counted_and_queue_keeps_running(self):
        evidence = FakeEvidenceStore(fail_first=1)
        evaluation = FakeEvaluationService()
        service = make_capture_service(
            rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
        )
        await service.start()
        try:
            for index in range(2):
                service.capture_agent_run(
                    subject_id=f"sess-1:{index}",
                    session_id="sess-1",
                    query="q",
                    final_answer="a",
                    failed=False,
                    finish_reason="stop",
                )
            assert await wait_until(lambda: len(evaluation.requests) == 1)
            counters = service.counters.snapshot()
            # 恰 1 次：第一条持久化失败，第二条成功（M1 后无其他来源）
            assert counters["capture_error"] == 1
            # 第二个样本仍被处理（消费者不因单次失败停摆）
            assert len(evidence.saved) == 1
        finally:
            await service.stop()

    async def test_sample_persisted_with_evidence_targets(self):
        """线上 agent_run 样本必须携带 evidence_targets 反向索引（spec 6.3，
        T18 删除传播依赖）：session + agent_run + 真实消费的 N-KB 文档版本。"""
        evidence = FakeEvidenceStore()
        evaluation = FakeEvaluationService()
        service = make_capture_service(
            rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
        )
        service.buffer.record_retrieval("sess-1", {
            "kb_id": "kb-1",
            "query": "deploy",
            "retrieval_id": "ret-1",
            "corpus_revision": "corpus-r1",
            "chunks": [
                {"rank": 1, "chunk_id": "c1", "document_id": "doc-1",
                 "document_revision": "r2", "content_hash": "c" * 64,
                 "score": 0.9, "excerpt": "x"},
                {"rank": 2, "chunk_id": "c2", "document_id": "doc-1",
                 "document_revision": "r2", "content_hash": "d" * 64,
                 "score": 0.8, "excerpt": "y"},
                {"rank": 3, "chunk_id": "c3", "document_id": "doc-2",
                 "document_revision": "r1", "content_hash": "e" * 64,
                 "score": 0.7, "excerpt": "z"},
            ],
        })
        await service.start()
        try:
            service.capture_agent_run(
                subject_id="sess-1:abcd",
                session_id="sess-1",
                run_id="run-1",
                query="q",
                final_answer="a",
                failed=False,
                finish_reason="stop",
            )
            assert await wait_until(lambda: len(evaluation.requests) == 1)
            targets = evaluation.requests[0].evidence_targets
            kinds = {(t["subject_kind"], t["subject_id"]) for t in targets}
            assert ("session", "sess-1") in kinds
            assert ("agent_run", "run-1") in kinds
            # 同文档同版本去重；文档目标带版本供删除传播
            documents = {
                (t["subject_id"], t["subject_revision"])
                for t in targets if t["subject_kind"] == "document"
            }
            assert documents == {("doc-1", "r2"), ("doc-2", "r1")}
            for target in targets:
                assert target["source_service"]
                assert target["subject_id"]
            # 无 retrieval_id 的检索记录不产 document 目标（非 N-KB 后端）
        finally:
            await service.stop()

    async def test_document_targets_require_retrieval_id(self):
        """缺 retrieval_id 的检索记录（非 N-KB 后端）不产 document 反向索引。"""
        evidence = FakeEvidenceStore()
        evaluation = FakeEvaluationService()
        service = make_capture_service(
            rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
        )
        service.buffer.record_retrieval("sess-1", {
            "kb_id": "kb-rf",
            "query": "q",
            "retrieval_id": None,
            "chunks": [{"rank": 1, "document_id": "doc-9",
                        "document_revision": "r1"}],
        })
        await service.start()
        try:
            service.capture_agent_run(
                subject_id="sess-1:abcd",
                session_id="sess-1",
                run_id="run-1",
                query="q",
                final_answer="a",
                failed=False,
                finish_reason="stop",
            )
            assert await wait_until(lambda: len(evaluation.requests) == 1)
            kinds = {
                t["subject_kind"]
                for t in evaluation.requests[0].evidence_targets
            }
            assert kinds == {"session", "agent_run"}
        finally:
            await service.stop()

    async def test_producer_event_id_idempotency_by_content(self):
        """同 subject 同内容 → 同 producer_event_id（幂等重放）；
        同 subject 异内容 → 不同 producer_event_id（隔离冲突）。"""
        service = make_capture_service(rate=1.0)
        common = dict(
            subject_id="sess-1:abcd",
            session_id="sess-1",
            run_id="run-1",
            query="q",
            failed=False,
            finish_reason="stop",
        )
        service.capture_agent_run(final_answer="answer-a", **common)
        service.capture_agent_run(final_answer="answer-a", **common)
        service.capture_agent_run(final_answer="answer-b", **common)
        items = service.queue.queued_items()
        assert len(items) == 3
        assert items[0].producer_event_id == items[1].producer_event_id
        assert items[0].producer_event_id != items[2].producer_event_id
        assert items[0].producer_event_id.startswith("agent-run:sess-1:abcd:")

    async def test_persist_expands_registered_definitions_into_jobs(self):
        """线上捕获样本按目标种类展开已注册的自动评估定义（T22 E2E 缺口
        回归锁定）：提交时解析当前注册定义，human 方法定义不得展开
        （worker 不执行人评作业，human_method_not_executable）。"""
        evidence = FakeEvidenceStore()
        evaluation = FakeEvaluationService()
        evaluation.definitions_by_kind = {
            "agent_run": (DefinitionKey("agent.answer_relevance", "v1"),),
        }
        service = make_capture_service(
            rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
        )
        await service.start()
        try:
            service.capture_agent_run(
                subject_id="sess-1:abcd",
                session_id="sess-1",
                run_id="run-1",
                query="q",
                final_answer="a",
                failed=False,
                finish_reason="stop",
            )
            assert await wait_until(lambda: len(evaluation.requests) == 1)
            request = evaluation.requests[0]
            assert request.definitions == (
                DefinitionKey("agent.answer_relevance", "v1"),
            )
        finally:
            await service.stop()

    async def test_persist_resolves_definitions_for_target_kind(self):
        """定义解析按样本目标种类查询（agent_run 捕获不展开 retrieval
        定义；human 定义在解析层过滤，不进入提交意图）。"""
        evidence = FakeEvidenceStore()
        evaluation = FakeEvaluationService()
        evaluation.definitions_by_kind = {
            "retrieval": (DefinitionKey("kb.recall_at_5", "v1"),),
        }
        service = make_capture_service(
            rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
        )
        await service.start()
        try:
            service.capture_agent_run(
                subject_id="sess-1:abcd",
                session_id="sess-1",
                query="q",
                final_answer="a",
                failed=False,
                finish_reason="stop",
            )
            assert await wait_until(lambda: len(evaluation.requests) == 1)
            assert evaluation.target_kind_queries == ["agent_run"]
            # fake 内按 target_kind 过滤（registered 为 retrieval 定义）
            assert evaluation.requests[0].definitions == ()
        finally:
            await service.stop()

    async def test_persist_without_registered_definitions_stays_jobless(self):
        """未注册任何适用定义时样本仍持久化、作业意图为空（T16 既有语义：
        定义注册前捕获的样本不回溯补开作业）。"""
        evidence = FakeEvidenceStore()
        evaluation = FakeEvaluationService()
        service = make_capture_service(
            rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
        )
        await service.start()
        try:
            service.capture_agent_run(
                subject_id="sess-1:abcd",
                session_id="sess-1",
                query="q",
                final_answer="a",
                failed=False,
                finish_reason="stop",
            )
            assert await wait_until(lambda: len(evaluation.requests) == 1)
            assert evaluation.requests[0].definitions == ()
        finally:
            await service.stop()


class TestQueueLifecycle:
    async def test_repeated_start_stop_no_task_leak(self):
        handled = []
        started = asyncio.Event()

        async def handler(item):
            handled.append(item)
            started.set()

        queue = EvaluationCaptureQueue(maxsize=10, handler=handler)
        await queue.start()
        first_task = queue.consumer_task
        # 运行中重复 start 不重建任务（无泄漏）
        await queue.start()
        assert queue.consumer_task is first_task
        await queue.stop()
        assert queue.done
        assert queue.consumer_task is None

        # stop 后再次 start 重建消费者并继续处理（单条失败不拖垮后续）
        await queue.start()
        second_task = queue.consumer_task
        assert second_task is not None and second_task is not first_task
        assert queue.put_nowait({"n": 1})
        assert await wait_until(lambda: len(handled) == 1)
        await queue.stop()
        assert queue.done
        # 重复 stop 幂等
        await queue.stop()
        assert queue.done

    async def test_stop_reraises_caller_cancellation(self):
        """stop 吞掉的是消费者自身的取消（task.cancelled()）；取消尚未由
        该 task 承担（如调用方自身被取消）时原样上抛（Mi3）。

        以确定性假任务注入：await 即抛 CancelledError、cancelled() 为
        False——真实消费者任务处理取消的时序存在竞态，不用于锁定语义。
        """

        class FakePendingTask:
            """done()=False、cancel() 后 cancelled() 仍 False 的假任务；
            await 即抛 CancelledError（取消由别处承担的模拟）。"""

            def __init__(self):
                self.cancel_called = False

            def done(self):
                return False

            def cancel(self):
                self.cancel_called = True
                return True

            def cancelled(self):
                return False

            def __await__(self):
                if False:
                    yield  # 使其成为生成器型 awaitable
                raise asyncio.CancelledError

        class FakeCancelledTask(FakePendingTask):
            def cancelled(self):
                return True

        # 取消由本 task 承担 -> 吞掉，不抛
        queue = EvaluationCaptureQueue(maxsize=10)
        queue._task = FakeCancelledTask()
        await queue.stop()
        assert queue.done

        # 取消并非由本 task 承担（调用方被取消）-> 原样上抛
        queue = EvaluationCaptureQueue(maxsize=10)
        fake = FakePendingTask()
        queue._task = fake
        with pytest.raises(asyncio.CancelledError):
            await queue.stop()
        assert fake.cancel_called


# ---------------------------------------------------------------------------
# 有界证据缓冲（真实消费片段在 finalize 汇聚）
# ---------------------------------------------------------------------------


class TestCaptureBuffer:
    def test_buffer_per_key_bound_drops_oldest(self):
        drops = []
        buffer = EvaluationCaptureBuffer(
            max_records=2, on_drop=lambda record: drops.append(record),
        )
        for index in range(3):
            buffer.record_tool("sess-1", {"seq": index})
        drained = buffer.drain("sess-1")
        assert [record["seq"] for record in drained["tools"]] == [1, 2]
        assert len(drops) == 1
        assert buffer.dropped_total == 1
        assert drained["drops"] == 1

    def test_buffer_key_bound_evicts_lru(self):
        drops = []
        buffer = EvaluationCaptureBuffer(max_keys=2, on_drop=lambda record: drops.append(record))
        buffer.record_tool("s1", {"n": 1})
        buffer.record_tool("s2", {"n": 2})
        buffer.record_tool("s3", {"n": 3})
        assert buffer.drain("s1")["tools"] == []
        # 整桶淘汰：恰 1 条记录 + dropped_total 同步
        assert len(drops) == 1
        assert buffer.dropped_total == 1
        assert buffer.drain("s2")["tools"][0]["n"] == 2

    def test_buffer_rejects_non_mapping_records(self):
        buffer = EvaluationCaptureBuffer()
        buffer.record_tool("s1", "not a mapping")
        assert buffer.drain("s1")["tools"] == []
        assert buffer.dropped_total == 1

    def test_buffer_drop_does_not_pollute_capture_error(self):
        """M1 修复语义：默认 p=0（捕获关闭）下登记/淘汰不递增 capture_error。

        capture_error 仅表示"本应捕获却失败"，与抽样率解耦；淘汰量经
        buffer.dropped_total 单独观测，不入 producer gauge 五字段口径。
        """
        service = make_capture_service(rate=0.0)
        for index in range(DEFAULT_BUFFER_MAX_RECORDS + 10):
            service.buffer.record_tool("sess-1", {"seq": index})
        counters = service.counters.snapshot()
        assert counters["capture_error"] == 0
        assert counters["eligible"] == 0
        assert service.buffer.dropped_total == 10
        drained = service.buffer.drain("sess-1")
        assert [record["seq"] for record in drained["tools"]] == list(
            range(10, DEFAULT_BUFFER_MAX_RECORDS + 10)
        )

    def test_capture_error_only_counts_real_failures(self):
        """rate>0 且 selected 的会话中，捕获失败才计 capture_error。"""
        service = make_capture_service(rate=1.0)
        for index in range(DEFAULT_BUFFER_MAX_RECORDS + 5):
            service.buffer.record_tool("sess-1", {"seq": index})
        # 仅登记/淘汰不计 capture_error
        assert service.counters.snapshot()["capture_error"] == 0
        service.capture_agent_run(
            subject_id="s:1",
            session_id="sess-1",
            query="q",
            final_answer="a",
            failed=False,
            finish_reason="stop",
        )
        assert service.counters.snapshot()["capture_error"] == 0
        assert service.counters.snapshot()["captured"] == 1


# ---------------------------------------------------------------------------
# 快照上限边界（Mi6）与脱敏遍历健壮性（Mi4）
# ---------------------------------------------------------------------------


class TestSnapshotBounds:
    def _capture_with(self, service: EvaluationCaptureService, **overrides):
        kwargs = dict(
            subject_id="sess-1:abcd",
            session_id="sess-1",
            run_id="run-1",
            query="q",
            final_answer="a",
            failed=False,
            finish_reason="stop",
        )
        kwargs.update(overrides)
        service.capture_agent_run(**kwargs)

    def test_snapshot_field_char_bound(self):
        from app.application.evaluation_capture import MAX_SNAPSHOT_FIELD_CHARS

        service = make_capture_service(rate=1.0)
        self._capture_with(service, final_answer="x" * (MAX_SNAPSHOT_FIELD_CHARS * 2))
        content = service.queue.queued_items()[0].snapshot.content
        assert len(content["final_answer"]) == MAX_SNAPSHOT_FIELD_CHARS

    def test_snapshot_items_bound(self):
        from app.application.evaluation_capture import MAX_SNAPSHOT_ITEMS

        service = make_capture_service(rate=1.0)
        self._capture_with(
            service,
            truth_refs={f"key_{index}": index for index in range(100)},
        )
        content = service.queue.queued_items()[0].snapshot.content
        assert len(content["truth_refs"]) == MAX_SNAPSHOT_ITEMS

    def test_snapshot_depth_bound_does_not_fail(self):
        deep: Any = "bottom"
        for _ in range(12):
            deep = [deep]
        service = make_capture_service(rate=1.0)
        # 超深嵌套不抛错（超深值投影为字符串），快照正常入队
        self._capture_with(service, truth_refs={"deep": deep})
        counters = service.counters.snapshot()
        assert counters["capture_error"] == 0
        assert counters["captured"] == 1

    def test_non_finite_float_projected_to_none(self):
        service = make_capture_service(rate=1.0)
        self._capture_with(
            service,
            truth_refs={"nan": float("nan"), "inf": float("inf")},
        )
        content = service.queue.queued_items()[0].snapshot.content
        # NaN/Inf 直通会让 domain canonical_json_bytes（allow_nan=False）
        # 拒绝整条快照；脱敏遍历投影为 None 后快照可用
        assert content["truth_refs"] == {"nan": None, "inf": None}

    def test_non_str_key_prefix_no_collision(self):
        service = make_capture_service(rate=1.0)
        self._capture_with(
            service,
            truth_refs={1: "int-one", "1": "str-one"},
        )
        content = service.queue.queued_items()[0].snapshot.content
        assert content["truth_refs"]["int:1"] == "int-one"
        assert content["truth_refs"]["1"] == "str-one"

    def test_evidence_drops_flow_into_completeness(self):
        service = make_capture_service(rate=1.0)
        # 覆写 buffer：每键仅容 1 条，制造登记期淘汰
        service.buffer = EvaluationCaptureBuffer(max_records=1)
        service.buffer.record_tool("sess-1", {"n": 1})
        service.buffer.record_tool("sess-1", {"n": 2})
        self._capture_with(service)
        content = service.queue.queued_items()[0].snapshot.content
        assert content["capture_completeness"]["evidence_drops"] == 1
        assert content["capture_completeness"]["tool_trace_count"] == 1


# ---------------------------------------------------------------------------
# 登记期脱敏先于限长（Mi1）与稳定投影（N7）
# ---------------------------------------------------------------------------


class TestExcerptRedaction:
    def test_excerpt_redacts_before_truncation(self):
        """凭据跨截断边界：先脱敏后截断，劈开的凭据不再逃过正则。"""
        secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7r8"
        # 填充使凭据起点位于截断点之前、终点越过截断点
        filler = "x" * (2048 - 20)
        excerpt = jsonable_excerpt(f"head {filler} {secret}")
        assert secret not in excerpt
        assert "[redacted]" in excerpt

    def test_excerpt_uses_stable_projection_without_memory_address(self):
        from datetime import datetime, timezone

        value = {"ts": datetime(2026, 11, 11, tzinfo=timezone.utc), "obj": object()}
        excerpt = jsonable_excerpt(value)
        assert "0x" not in excerpt
        assert "<object>" in excerpt
        assert "2026-11-11T00:00:00+00:00" in excerpt


# ---------------------------------------------------------------------------
# 登记键归一化（Mi5）
# ---------------------------------------------------------------------------


class TestCaptureSessionKey:
    def test_explicit_session_id_wins(self):
        binding = RunBinding(run_id="r", session_id="bound-sess", owner_token="t", process_id="p")
        with bind_run(binding):
            assert capture_session_key(explicit="ctx-sess") == "ctx-sess"

    def test_fallback_to_run_binding(self):
        binding = RunBinding(run_id="r", session_id="bound-sess", owner_token="t", process_id="p")
        with bind_run(binding):
            assert capture_session_key() == "bound-sess"

    def test_none_without_binding_or_explicit(self):
        assert capture_session_key() is None
        assert capture_session_key(explicit="") is None

    async def test_tool_evidence_uses_binding_when_context_session_missing(self):
        """分叉场景：context.session_id 缺失时回退运行绑定（同一归一化）。"""

        class CaptureStub:
            def __init__(self):
                self.buffer = EvaluationCaptureBuffer()

        capture = CaptureStub()
        tool_service = ToolService(
            EchoExecutor(), [echo_definition()], evaluation_capture=capture,
        )
        binding = RunBinding(run_id="run-1", session_id="bound-sess", owner_token="t", process_id="p")
        with bind_run(binding):
            await tool_service.execute(
                ToolCallRequest(id="call-1", name="echo", arguments={}),
                ToolExecutionContext(session_id=None, run_id="run-1"),
            )
        drained = capture.buffer.drain("bound-sess")
        assert len(drained["tools"]) == 1


# ---------------------------------------------------------------------------
# Knowledge VO 兼容扩展与真实消费片段传播
# ---------------------------------------------------------------------------


class TestKnowledgeVoCompat:
    def test_search_result_optional_fields_default_none(self):
        result = KnowledgeSearchResult(
            kb_id="kb-1",
            kb_name="KB",
            base_type=KnowledgeBaseType.N_KB,
            query="q",
            results=[KnowledgeSnippet(id="1", title="t", content="c", score=1.0, source="s")],
        )
        assert result.evaluation_context is None
        assert result.results[0].document_id is None
        assert result.results[0].document_revision is None
        assert result.results[0].chunk_id is None
        assert result.results[0].chunk_content_hash is None

    async def test_nkb_retriever_old_response_without_context(self):
        from app.infrastructure.knowledge.http_adapters import (
            HttpKnowledgeRetrieverConfig,
            NkbKnowledgeRetriever,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "results": [
                    {"id": "chunk-1", "content": "c1", "score": 0.9},
                ],
            })

        retriever = NkbKnowledgeRetriever(HttpKnowledgeRetrieverConfig(
            transport=httpx.MockTransport(handler),
        ))
        result = await retriever.search(
            make_kb_base(),
            KnowledgeBackendSearchRequest(query="q", top_k=5),
            None,
        )
        assert result.evaluation_context is None
        assert result.results[0].document_id is None
        assert result.results[0].chunk_id is None

    async def test_nkb_retriever_preserves_evaluation_context(self):
        from app.infrastructure.knowledge.http_adapters import (
            HttpKnowledgeRetrieverConfig,
            NkbKnowledgeRetriever,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "results": [
                    {
                        "id": "chunk-1",
                        "content": "c1",
                        "score": 0.9,
                        "document_id": "doc-1",
                        "document_revision": "r2",
                        "chunk_id": "chunk-1",
                        "chunk_content_hash": "c" * 64,
                    },
                ],
                "evaluation_context": {
                    "retrieval_id": "ret-9",
                    "corpus_revision": "corpus-r1",
                    "trace_ref": {"trace_id": "a" * 32, "span_id": "b" * 16, "sampled": True},
                },
            })

        retriever = NkbKnowledgeRetriever(HttpKnowledgeRetrieverConfig(
            transport=httpx.MockTransport(handler),
        ))
        result = await retriever.search(
            make_kb_base(),
            KnowledgeBackendSearchRequest(query="q", top_k=5),
            None,
        )
        assert result.evaluation_context is not None
        assert result.evaluation_context.retrieval_id == "ret-9"
        assert result.evaluation_context.corpus_revision == "corpus-r1"
        assert result.results[0].document_id == "doc-1"
        assert result.results[0].document_revision == "r2"
        assert result.results[0].chunk_id == "chunk-1"
        assert result.results[0].chunk_content_hash == "c" * 64

    async def test_nkb_retriever_malformed_context_ignored(self):
        from app.infrastructure.knowledge.http_adapters import (
            HttpKnowledgeRetrieverConfig,
            NkbKnowledgeRetriever,
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "results": [{"id": "chunk-1", "content": "c1", "score": 0.9}],
                "evaluation_context": {"corpus_revision": "missing retrieval_id"},
            })

        retriever = NkbKnowledgeRetriever(HttpKnowledgeRetrieverConfig(
            transport=httpx.MockTransport(handler),
        ))
        result = await retriever.search(
            make_kb_base(),
            KnowledgeBackendSearchRequest(query="q", top_k=5),
            None,
        )
        assert result.evaluation_context is None


class TestKnowledgeServiceCapture:
    def test_search_signature_unchanged(self):
        parameters = inspect.signature(KnowledgeService.search).parameters
        assert list(parameters) == ["self", "kb_id", "query", "top_k", "min_score"]
        for name in ("kb_id", "query", "top_k", "min_score"):
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY

    def test_retrieval_record_from_result_has_stable_identities(self):
        record = retrieval_record_from_result(make_kb_result())
        assert record["retrieval_id"] == "ret-123"
        assert record["corpus_revision"] == "corpus-r1"
        assert record["kb_id"] == "kb-1"
        assert record["chunks"][0]["document_id"] == "doc-1"
        assert record["chunks"][0]["chunk_id"] == "chunk-1"
        assert record["chunks"][0]["rank"] == 1

    async def test_consumed_chunks_propagated_without_requery(self):
        result = make_kb_result()
        retriever = FakeRetriever(result)

        class CaptureStub:
            def __init__(self):
                self.buffer = EvaluationCaptureBuffer()

        capture = CaptureStub()
        service = KnowledgeService(registry=None, retriever_factory=None, evaluation_capture=capture)

        class FakeRegistry:
            async def get_base(self, kb_id):
                return make_kb_base()

            async def get_secret(self, kb_id):
                return None

        class FakeFactory:
            def __init__(self, retriever):
                self._retriever = retriever

            def get(self, base_type):
                return self._retriever

        service.registry = FakeRegistry()
        service.retriever_factory = FakeFactory(retriever)

        binding = RunBinding(
            run_id="run-1",
            session_id="sess-1",
            owner_token="t",
            process_id="p",
        )
        with bind_run(binding):
            await service.search(kb_id="kb-1", query="how to deploy")

        assert retriever.search_calls == 1, "禁止再查询 KB 重建上下文"
        drained = capture.buffer.drain("sess-1")
        assert len(drained["retrievals"]) == 1
        record = drained["retrievals"][0]
        assert record["retrieval_id"] == "ret-123"
        assert record["chunks"][0]["chunk_id"] == "chunk-1"

    async def test_retrieval_evidence_failure_isolated_from_search_result(self):
        """检索证据登记抛错只告警，不影响检索业务结果（失败注入）。"""
        from app.application.evaluation_capture import EvaluationCaptureBuffer

        class ExplodingBuffer:
            def record_retrieval(self, session_id, record):
                raise RuntimeError("buffer down")

        class CaptureStub:
            buffer = ExplodingBuffer()

        result = make_kb_result()
        retriever = FakeRetriever(result)
        service = KnowledgeService(registry=None, retriever_factory=None, evaluation_capture=CaptureStub())

        class FakeRegistry:
            async def get_base(self, kb_id):
                return make_kb_base()

            async def get_secret(self, kb_id):
                return None

        class FakeFactory:
            def __init__(self, retriever):
                self._retriever = retriever

            def get(self, base_type):
                return self._retriever

        service.registry = FakeRegistry()
        service.retriever_factory = FakeFactory(retriever)
        binding = RunBinding(
            run_id="run-1",
            session_id="sess-1",
            owner_token="t",
            process_id="p",
        )
        with bind_run(binding):
            search_result = await service.search(kb_id="kb-1", query="how to deploy")
        assert search_result.kb_id == "kb-1"
        assert retriever.search_calls == 1

    async def test_no_capture_service_still_works(self):
        result = make_kb_result(with_context=False)
        retriever = FakeRetriever(result)
        service = KnowledgeService(registry=None, retriever_factory=None)

        class FakeRegistry:
            async def get_base(self, kb_id):
                return make_kb_base()

            async def get_secret(self, kb_id):
                return None

        class FakeFactory:
            def __init__(self, retriever):
                self._retriever = retriever

            def get(self, base_type):
                return self._retriever

        service.registry = FakeRegistry()
        service.retriever_factory = FakeFactory(retriever)
        search_result = await service.search(kb_id="kb-1", query="q")
        assert search_result.kb_id == "kb-1"


# ---------------------------------------------------------------------------
# ToolService 工具轨迹证据
# ---------------------------------------------------------------------------


class EchoExecutor:
    async def execute(self, request, context=None):
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=ToolResultStatus.SUCCESS,
            content={"answer": 42},
            duration_ms=7,
        )


def echo_definition() -> ToolDefinition:
    return ToolDefinition(
        name="echo",
        description="echo",
        input_schema={"type": "object", "properties": {}},
    )


class TestToolEvidence:
    async def test_executed_tool_recorded_to_buffer(self):
        class CaptureStub:
            def __init__(self):
                self.buffer = EvaluationCaptureBuffer()

        capture = CaptureStub()
        tool_service = ToolService(
            EchoExecutor(),
            [echo_definition()],
            evaluation_capture=capture,
        )
        context = ToolExecutionContext(session_id="sess-1", run_id="run-1")
        result = await tool_service.execute(
            ToolCallRequest(id="call-1", name="echo", arguments={}),
            context,
        )
        assert result.status is ToolResultStatus.SUCCESS
        drained = capture.buffer.drain("sess-1")
        assert len(drained["tools"]) == 1
        record = drained["tools"][0]
        assert record["call_id"] == "call-1"
        assert record["tool"] == "echo"
        assert record["status"] == "success"
        assert record["duration_ms"] == 7

    async def test_permission_denied_is_recorded_for_contract(self):
        """24 项三方审阅 #17：权限拒绝同样登记（拒绝是工具契约违规事
        实，spec 8 权限拒绝计 False）——只登记允许轨迹会让线上
        agent.tool_contract 对权限拒绝虚假通过。"""
        class DenyExecutor:
            async def execute(self, request, context=None):  # pragma: no cover
                raise AssertionError("executor must not run")

        class CaptureStub:
            def __init__(self):
                self.buffer = EvaluationCaptureBuffer()

        capture = CaptureStub()
        definition = ToolDefinition(
            name="echo",
            description="echo",
            input_schema={"type": "object", "properties": {}},
            enabled=False,
        )
        tool_service = ToolService(
            DenyExecutor(),
            [definition],
            evaluation_capture=capture,
        )
        context = ToolExecutionContext(session_id="sess-1", run_id="run-1")
        result = await tool_service.execute(
            ToolCallRequest(id="call-1", name="echo", arguments={}),
            context,
        )
        assert result.status is ToolResultStatus.PERMISSION_DENIED
        tools = capture.buffer.drain("sess-1")["tools"]
        assert len(tools) == 1
        assert tools[0]["tool"] == "echo"
        assert tools[0]["status"] == "permission_denied"

    async def test_tool_evidence_failure_isolated_from_business_result(self):
        """证据登记抛错只告警，不影响工具业务结果（失败注入）。"""

        class ExplodingBuffer:
            def record_tool(self, session_id, record):
                raise RuntimeError("buffer down")

        class CaptureStub:
            buffer = ExplodingBuffer()

        tool_service = ToolService(
            EchoExecutor(),
            [echo_definition()],
            evaluation_capture=CaptureStub(),
        )
        context = ToolExecutionContext(session_id="sess-1", run_id="run-1")
        result = await tool_service.execute(
            ToolCallRequest(id="call-9", name="echo", arguments={}),
            context,
        )
        assert result.status is ToolResultStatus.SUCCESS
        assert result.content == {"answer": 42}


# ---------------------------------------------------------------------------
# T20：内建凭据正则覆盖面扩展 + C 类 policy_provider 接缝
# ---------------------------------------------------------------------------


class TestBuiltinCredentialCoverageT20:
    """T20 覆盖面确认（T16 待办）：JWT/xox*/ya29./AWS secret key 形态。"""

    def test_jwt_token_redacted(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        assert _default_redact(f"auth {jwt} done") == "auth [redacted] done"

    def test_slack_token_redacted(self):
        for prefix in ("xoxb", "xoxp", "xoxa", "xoxs"):
            token = f"{prefix}-1234-5678-abcd"
            assert _default_redact(f"use {token} ok") == "use [redacted] ok"

    def test_google_oauth_token_redacted(self):
        token = "ya29." + "a" * 30
        assert _default_redact(f"google {token}") == "google [redacted]"

    def test_aws_secret_key_redacted(self):
        # 40 位 base64 形态 secret（AKIA access key id 已有覆盖）
        secret = "aB3cD4eF5gH6iJ7kL8mN9oP0qR1sT2uV3wX4yZ5a"
        assert len(secret) == 40
        assert _default_redact(f"aws {secret}") == "aws [redacted]"

    def test_hex64_hash_not_redacted(self):
        # 64 位十六进制内容哈希不受 40 位 AWS secret 形态误伤
        digest = "a" * 64
        assert _default_redact(f"hash {digest}") == f"hash {digest}"


class TestPolicyProviderSeam:
    """T20：C 类热重载接缝——policy_provider 逐次查询，None = 总闸关闭。"""

    def _make(self, provider=None, **kwargs):
        return make_capture_service(policy_provider=provider, **kwargs)

    def _capture(self, service):
        service.capture_agent_run(
            subject_id="subj",
            session_id="s1",
            query="q",
            final_answer="a",
        )

    def test_provider_policy_used_per_capture(self):
        from app.application.evaluation_capture import EvaluationSamplingPolicy

        policies = [
            EvaluationSamplingPolicy(rate=0.0),
            EvaluationSamplingPolicy(rate=1.0),
        ]
        calls = []

        def provider():
            calls.append(1)
            return policies[len(calls) - 1]

        service = self._make(provider=provider, rate=1.0)
        self._capture(service)
        # 第一次查询返回 rate=0 -> 未选中
        assert service.counters.snapshot()["captured"] == 0
        self._capture(service)
        # 第二次查询返回 rate=1 -> 选中（热重载生效）
        assert service.counters.snapshot()["captured"] == 1

    def test_provider_none_disables_capture(self):
        service = self._make(provider=lambda: None, rate=1.0)
        self._capture(service)
        counters = service.counters.snapshot()
        assert counters["eligible"] == 1
        assert counters["selected"] == 0
        assert counters["captured"] == 0

    def test_provider_failure_falls_back_to_fixed_policy(self):
        def broken_provider():
            raise RuntimeError("config store down")

        service = self._make(provider=broken_provider, rate=1.0)
        self._capture(service)
        # fail-safe 回退构造期 policy，不抛错、正常捕获
        assert service.counters.snapshot()["captured"] == 1


def test_snapshot_carries_judge_evidence_slots(self=None):
    """T22 收口（T16/T9 证据槽接缝）：快照携带 request/answer/chunks
    槽位键——Judge 适配器 _build_evidence_slots 按这些键取证据，此前
    线上 LLM Judge 指标全部 skipped(evidence_missing)。"""
    service = make_capture_service(rate=1.0)
    service.buffer.record_retrieval("sess-1", {
        "kb_id": "kb-1", "query": "what is 1+2", "retrieval_id": "r-1",
        "corpus_revision": "rev-1",
        "chunks": [{"rank": 1, "chunk_id": "c1", "document_id": "d1",
                    "document_revision": "dr1", "content_hash": "h1",
                    "score": 0.9, "excerpt": "deploy content"}],
    })
    service.capture_agent_run(
        subject_id="sess-1:abcd", session_id="sess-1", run_id="run-1",
        query="what is 1+2", final_answer="result is 3",
    )
    items = service.queue.queued_items()
    assert len(items) == 1
    content = items[0].snapshot.content
    assert content["request"] == "what is 1+2"
    assert content["answer"] == "result is 3"
    assert content["chunks"] == ["deploy content"]


# ---------------------------------------------------------------------------
# #28 持久化捕获计数 / #25 target_model 自识别槽
# ---------------------------------------------------------------------------


class _FakeCounterPersistence:
    """内存版 capture counters 持久化 fake（单行语义）。"""

    def __init__(self):
        self.saved: list[tuple[dict, str]] = []
        self.row: dict | None = None
        self.fail = False

    async def load_capture_counters(self):
        return self.row

    async def save_capture_counters(self, values, counter_epoch):
        if self.fail:
            raise RuntimeError("save failed")
        self.saved.append((dict(values), counter_epoch))
        self.row = {"counter_epoch": counter_epoch, "values": dict(values)}


class TestPersistingCaptureCounters:
    async def test_load_restores_counters_and_epoch(self):
        persistence = _FakeCounterPersistence()
        persistence.row = {"counter_epoch": "epoch-1", "values": {"eligible": 5}}
        counters = PersistingCaptureCounters(persistence)
        await counters.load()
        assert counters.snapshot()["eligible"] == 5
        assert counters.counter_epoch == "epoch-1"

    async def test_load_without_row_generates_epoch(self):
        counters = PersistingCaptureCounters(_FakeCounterPersistence())
        await counters.load()
        assert counters.counter_epoch
        assert all(value == 0 for value in counters.snapshot().values())

    async def test_set_values_is_monotonic(self):
        counters = PersistingCaptureCounters(_FakeCounterPersistence())
        counters.set_values({"eligible": 5})
        counters.set_values({"eligible": 2})  # 低水位不回退
        assert counters.snapshot()["eligible"] == 5

    async def test_bump_schedules_flush_and_reload_restores(self):
        persistence = _FakeCounterPersistence()
        counters = PersistingCaptureCounters(persistence)
        await counters.load()
        counters.bump("eligible")
        counters.bump("eligible")
        await counters.flush()
        assert persistence.saved
        values, epoch = persistence.saved[-1]
        assert values["eligible"] == 2
        assert epoch == counters.counter_epoch
        # 重启：新实例同持久化恢复累计值与 epoch
        restored = PersistingCaptureCounters(persistence)
        await restored.load()
        assert restored.snapshot()["eligible"] == 2
        assert restored.counter_epoch == counters.counter_epoch

    async def test_flush_failure_marks_dirty_and_recovers(self):
        persistence = _FakeCounterPersistence()
        persistence.fail = True
        counters = PersistingCaptureCounters(persistence)
        await counters.load()
        counters.bump("captured")
        await counters.flush()  # 失败不抛错，置 dirty
        assert not persistence.saved
        persistence.fail = False
        await counters.flush()
        assert persistence.saved[-1][0]["captured"] == 1

    def test_bump_without_loop_skips_flush(self):
        persistence = _FakeCounterPersistence()
        counters = PersistingCaptureCounters(persistence)
        counters.bump("eligible")  # 无事件循环：不落盘也不抛错
        assert counters.snapshot()["eligible"] == 1
        assert not persistence.saved


def test_snapshot_carries_target_model_slot():
    """#25：线上 self-judge 自动识别读 target_model 键——快照同时携带
    model 与 target_model（同值），被评目标模型可从快照内容直接取。"""
    service = make_capture_service(rate=1.0)
    service.capture_agent_run(
        subject_id="sess-1:abcd", session_id="sess-1", run_id="run-1",
        query="q", final_answer="a", model="test-model",
    )
    items = service.queue.queued_items()
    assert len(items) == 1
    content = items[0].snapshot.content
    assert content["target_model"] == content["model"]
    assert content["target_model"] == "test-model"


async def test_snapshot_contexts_slot_projects_chunks():
    """24 项三方审阅 #23：线上 agent_run 快照携带 contexts 槽位（与
    chunks 同源），faithfulness 不再恒 skipped(evidence_missing)。"""
    evidence = FakeEvidenceStore()
    evaluation = FakeEvaluationService()
    service = make_capture_service(
        rate=1.0, evidence_store=evidence, evaluation_service=evaluation,
    )
    service.buffer.record_retrieval("sess-1", {
        "kb_id": "kb-1",
        "query": "deploy",
        "retrieval_id": "ret-1",
        "corpus_revision": "corpus-r1",
        "chunks": [
            {"rank": 1, "chunk_id": "c1", "document_id": "doc-1",
             "document_revision": "r2", "content_hash": "c" * 64,
             "score": 0.9, "excerpt": "x"},
        ],
    })
    await service.start()
    try:
        service.capture_agent_run(
            subject_id="sess-1:abcd",
            session_id="sess-1",
            run_id="run-1",
            query="q",
            final_answer="a",
            failed=False,
            finish_reason="stop",
        )
        assert await wait_until(lambda: len(evidence.saved) == 1)
        content = evidence.saved[0][1].content
        assert content["contexts"] == content["chunks"]
        assert content["contexts"] == ["x"]
    finally:
        await service.stop()
