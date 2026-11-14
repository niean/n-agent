# app.application.evaluation_service 用例测试（plan T5）。
#
# S1 失败测试：注入真实 T3 SQLiteEvaluationStore（tmp DB）+ 容量探针 fake。
# 覆盖 plan T5 S1 全部要点与第三评审补充：
# - submit_sample：producer_event_id 幂等 / 冲突 409 / 容量满 / evidence_targets
# - KB metric 固定去重键；捕获与 producer 重复到达只生成一个 job；
#   同 Trace 多个 retrieval 分别保留
# - submit_feedback：actor 来自注入可信上下文；expected_revision CAS（含并发）；
#   新 revision 新 feedback_id/result_id + supersedes_id；human result；
#   反馈请求幂等键（同 key 同 payload 复用 / 异 payload 冲突）
# - feedback.helpful 人评定义注册；未反馈不计 false（无提交即无结果行）
# - retry_delivery：仅重试指定 (sink, result_id)；不改 payload、不新建 job
# - 查询：results cursor 分页 + target/definition/experiment/status 过滤；
#   result 详情含 attempts/evidence 摘要
# - start_experiment：只冻结并持久化请求（jobs 展开属 T13，sample 存在前禁止建 job）
# - cancel 用例；disable 短路（submit_* 抛 EvaluationDisabledError）
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.application.evaluation_service import (
    DefinitionKey,
    EvaluationCapacityError,
    EvaluationCapacityUsage,
    EvaluationDisabledError,
    EvaluationRevisionConflictError,
    EvaluationService,
    PRODUCER_HEARTBEAT_STALE_SECONDS,
    ProducerStateResult,
    StartExperimentRequest,
    SubmitFeedbackRequest,
    SubmitSampleRequest,
    definition_from_mapping,
    job_idempotency_key,
)
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationNotFoundError,
    EvaluationStaleSequenceError,
)
from app.domain.evaluation.models import (
    EvaluatorDefinition,
    EvaluationTarget,
    EvaluationValidationError,
    canonical_json_bytes,
)
from app.infrastructure.evaluation.store import SQLiteEvaluationStore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))


class FakeCapacityProbe:
    """容量探针 fake：返回注入的用量快照。"""

    def __init__(self, pending_jobs: int = 0, evidence_bytes: int = 0):
        self.usage = EvaluationCapacityUsage(
            pending_jobs=pending_jobs, evidence_bytes=evidence_bytes
        )
        self.calls = 0

    async def __call__(self) -> EvaluationCapacityUsage:
        self.calls += 1
        return self.usage


def make_target(
    *,
    service: str = "n-kb",
    kind: str = "retrieval",
    subject_id: str = "ret-1",
    subject_revision: str = "rev-1",
    retrieval_id: str | None = "ret-1",
    trace_id: str | None = None,
) -> EvaluationTarget:
    return EvaluationTarget(
        service=service,
        kind=kind,
        subject_id=subject_id,
        subject_revision=subject_revision,
        retrieval_id=retrieval_id,
        trace_id=trace_id,
    )


def make_sample_request(**overrides) -> SubmitSampleRequest:
    defaults = dict(
        producer_event_id="evt-1",
        service="n-kb",
        target=make_target(),
        evidence_targets=(
            {
                "source_service": "n-kb",
                "subject_kind": "document",
                "subject_id": "doc-1",
                "subject_revision": "dr-7",
            },
        ),
        snapshot_ref="snapshots/abc.json",
        snapshot_hash="a" * 64,
        schema_version="evaluation.sample.v1",
        captured_at="2026-11-10T00:00:00+00:00",
        # 默认仅登记样本；需要展开 job 的用例显式传入并先注册定义
        definitions=(),
    )
    defaults.update(overrides)
    return SubmitSampleRequest(**defaults)


def make_feedback_request(**overrides) -> SubmitFeedbackRequest:
    defaults = dict(
        target=make_target(
            service="n-agent",
            kind="agent_run",
            subject_id="run-1",
            retrieval_id=None,
        ),
        definition_id="feedback.helpful",
        definition_version="v1",
        value=True,
        comment_ref=None,
        request_key=None,
        expected_revision=None,
    )
    defaults.update(overrides)
    return SubmitFeedbackRequest(**defaults)


def make_definition(definition_id: str, version: str = "v1"):
    return EvaluatorDefinition(
        definition_id=definition_id,
        version=version,
        metric_name=definition_id,
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
        timeout_seconds=30,
        budget=None,
        ci_admissible=False,
    )


def _expected_job_key(*parts: str) -> str:
    """去重键长度前缀格式的测试锚点（与 job_idempotency_key 同构）。"""
    return "evaljob:v1:" + "".join(f"{len(p)}:{p}" for p in parts)


def make_service(store, *, enabled: bool = True, probe=None, **kwargs):
    return EvaluationService(
        store,
        enabled=enabled,
        capacity_usage_provider=probe,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# submit_sample
# ---------------------------------------------------------------------------


class TestSubmitSample:
    async def test_persists_sample_with_evidence_targets(self, store):
        service = make_service(store)
        outcome = await service.submit_sample(make_sample_request())

        assert outcome.sample_id
        sample = await store.load_sample(outcome.sample_id)
        assert sample["producer_event_id"] == "evt-1"
        assert sample["service"] == "n-kb"
        assert sample["target"]["kind"] == "retrieval"
        assert sample["snapshot_ref"] == "snapshots/abc.json"
        # 样本落库同时写 evidence_targets 反向索引
        ids = await store.list_sample_ids_by_subject("n-kb", "document", "doc-1", "dr-7")
        assert ids == [outcome.sample_id]

    async def test_idempotent_same_hash_reuses_sample_id_and_jobs(self, store):
        service = make_service(store)
        first = await service.submit_sample(make_sample_request())
        replay = await service.submit_sample(make_sample_request())

        assert replay.sample_id == first.sample_id
        assert replay.job_ids == first.job_ids

    async def test_same_event_different_hash_conflicts_409(self, store):
        service = make_service(store)
        await service.submit_sample(make_sample_request())
        with pytest.raises(EvaluationConflictError):
            await service.submit_sample(
                make_sample_request(captured_at="2026-11-11T00:00:00+00:00")
            )

    async def test_capacity_jobs_full_rejected(self, store):
        probe = FakeCapacityProbe(pending_jobs=10000)
        service = make_service(store, probe=probe, max_pending_jobs=10000)
        with pytest.raises(EvaluationCapacityError):
            await service.submit_sample(make_sample_request())
        assert probe.calls == 1

    async def test_capacity_evidence_full_rejected(self, store):
        probe = FakeCapacityProbe(evidence_bytes=256 * 1024 * 1024)
        service = make_service(store, probe=probe, max_evidence_bytes=256 * 1024 * 1024)
        with pytest.raises(EvaluationCapacityError):
            await service.submit_sample(make_sample_request())

    async def test_creates_jobs_with_fixed_dedup_key(self, store):
        await store.save_definition({
            "definition_id": "kb.context_relevance",
            "version": "v1",
            "metric_name": "kb.context_relevance",
        })
        service = make_service(store)
        outcome = await service.submit_sample(
            make_sample_request(
                definitions=(DefinitionKey("kb.context_relevance", "v1"),)
            )
        )

        assert len(outcome.job_ids) == 1
        job = await store.get_job(outcome.job_ids[0])
        # 固定去重键：service+retrieval_id+definition_id+definition_version
        # +mode+experiment_id(线上空)+replicate，长度前缀复合防碰撞
        assert job["idempotency_key"] == _expected_job_key(
            "n-kb", "ret-1", "kb.context_relevance", "v1", "online", "", "0"
        )
        assert job["status"] == "queued"
        assert job["sample_id"] == outcome.sample_id
        assert job["requested_by"] == "n-kb"
        assert job["experiment_id"] is None

    def test_dedup_key_collision_proof(self):
        # 分量内出现分隔符不得产生跨分量歧义（旧的 ':' 直拼会碰撞）
        first = job_idempotency_key(
            service="s", event_coordinate="a:b", definition_id="c",
            definition_version="v", mode="m", experiment_id=None, replicate=0,
        )
        second = job_idempotency_key(
            service="s", event_coordinate="a", definition_id="b:c",
            definition_version="v", mode="m", experiment_id=None, replicate=0,
        )
        assert first.startswith("evaljob:v1:")
        assert first != second

    async def test_experiment_sample_job_uses_mode_and_experiment_in_key(self, store):
        await store.save_definition({
            "definition_id": "kb.context_relevance",
            "version": "v1",
            "metric_name": "kb.context_relevance",
        })
        service = make_service(store)
        outcome = await service.submit_sample(
            make_sample_request(
                definitions=(DefinitionKey("kb.context_relevance", "v1"),),
                experiment_id="exp-9",
                mode="replay",
                replicate=2,
            )
        )
        job = await store.get_job(outcome.job_ids[0])
        assert job["idempotency_key"] == _expected_job_key(
            "n-kb", "ret-1", "kb.context_relevance", "v1", "replay", "exp-9", "2"
        )
        assert job["experiment_id"] == "exp-9"
        assert job["replicate"] == 2

    async def test_definitions_axis_changes_conflict(self, store):
        """同 event 不同作业展开意图（definitions）→ 409（同 ID 不同内容）。"""
        service = make_service(store)
        for definition_id in ("kb.context_relevance", "kb.recall_at_5"):
            await store.save_definition({
                "definition_id": definition_id,
                "version": "v1",
                "metric_name": definition_id,
            })
        await service.submit_sample(
            make_sample_request(
                definitions=(DefinitionKey("kb.context_relevance", "v1"),)
            )
        )
        with pytest.raises(EvaluationConflictError):
            await service.submit_sample(
                make_sample_request(
                    definitions=(DefinitionKey("kb.recall_at_5", "v1"),)
                )
            )

    async def test_duplicate_definition_keys_rejected(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationValidationError):
            await service.submit_sample(
                make_sample_request(
                    definitions=(
                        DefinitionKey("kb.context_relevance", "v1"),
                        DefinitionKey("kb.context_relevance", "v1"),
                    )
                )
            )

    async def test_duplicate_arrival_yields_single_job(self, store):
        """上下文捕获与 producer 重复到达：不同 producer_event_id、同一检索
        坐标 → 两个 sample 行、但只有一个对应 job（去重键幂等）。"""
        await store.save_definition({
            "definition_id": "kb.context_relevance",
            "version": "v1",
            "metric_name": "kb.context_relevance",
        })
        service = make_service(store)
        capture = await service.submit_sample(
            make_sample_request(
                producer_event_id="evt-capture",
                definitions=(DefinitionKey("kb.context_relevance", "v1"),),
            )
        )
        producer = await service.submit_sample(
            make_sample_request(
                producer_event_id="evt-producer",
                definitions=(DefinitionKey("kb.context_relevance", "v1"),),
            )
        )

        assert capture.sample_id != producer.sample_id
        assert producer.job_ids == capture.job_ids
        jobs = await store.claim_next_job("probe-worker")
        second = await store.claim_next_job("probe-worker")
        assert jobs["job_id"] == capture.job_ids[0]
        assert second is None  # 只有一个 job

    async def test_multiple_retrievals_same_trace_separate_jobs(self, store):
        await store.save_definition({
            "definition_id": "kb.context_relevance",
            "version": "v1",
            "metric_name": "kb.context_relevance",
        })
        service = make_service(store)
        trace_id = "a" * 32
        first = await service.submit_sample(
            make_sample_request(
                producer_event_id="evt-1",
                target=make_target(subject_id="ret-1", retrieval_id="ret-1", trace_id=trace_id),
                definitions=(DefinitionKey("kb.context_relevance", "v1"),),
            )
        )
        second = await service.submit_sample(
            make_sample_request(
                producer_event_id="evt-2",
                target=make_target(subject_id="ret-2", retrieval_id="ret-2", trace_id=trace_id),
                definitions=(DefinitionKey("kb.context_relevance", "v1"),),
            )
        )
        assert first.job_ids != second.job_ids

    async def test_retrieval_sample_requires_retrieval_id(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationValidationError):
            await service.submit_sample(
                make_sample_request(target=make_target(retrieval_id=None))
            )

    async def test_unknown_definition_rejected_before_persist(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationNotFoundError):
            await service.submit_sample(
                make_sample_request(
                    definitions=(DefinitionKey("kb.unknown", "v1"),)
                )
            )

    async def test_disabled_short_circuits(self, store):
        service = make_service(store, enabled=False)
        with pytest.raises(EvaluationDisabledError):
            await service.submit_sample(make_sample_request())


# ---------------------------------------------------------------------------
# submit_feedback
# ---------------------------------------------------------------------------


class TestSubmitFeedback:
    async def test_creates_feedback_and_human_result(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()

        outcome = await service.submit_feedback(
            "user-1", make_feedback_request(value=True)
        )

        assert outcome.revision == 1
        assert outcome.reused is False
        feedback = await store.get_feedback(outcome.feedback_id)
        assert feedback["actor_ref"] == "user-1"
        assert feedback["definition_id"] == "feedback.helpful"
        assert feedback["target"]["kind"] == "agent_run"
        assert feedback["value"] is True
        # human result：UNIQUE(feedback_id) 路径
        result = await store.load_result(outcome.result_id)
        assert result["provenance"] == "human"
        assert result["feedback_id"] == outcome.feedback_id
        assert result["metric_name"] == "feedback.helpful"
        assert result["typed_value"] is True
        assert result["passed"] is True
        assert result["job_id"] is None

    async def test_creates_pending_deliveries_for_sink_specs(self, store):
        """人评 result 同事务登记 sink 投递（spec 145"结果与各 sink
        delivery 同事务提交"/ 262"每个 result 固定 external_id"；无真实
        身份的 unlinked 判定由投递 worker 投影时落）。"""
        service = make_service(
            store,
            delivery_sink_specs=({"sink": "langfuse", "projection_version": "v1"},),
        )
        await service.register_feedback_helpful_definition()
        outcome = await service.submit_feedback(
            "user-1", make_feedback_request(value=True)
        )
        rows = await store.list_deliveries(result_id=outcome.result_id)
        assert [(r["sink"], r["projection_version"], r["status"]) for r in rows] == [
            ("langfuse", "v1", "pending")
        ]

    async def test_delivery_sink_specs_property_returns_readonly_copy(self, store):
        """delivery_sink_specs 属性只读契约：返回内层 dict 拷贝的新 tuple，
        调用方（Task 6 AnnotationService 经该属性复用 sink 清单）修改
        返回值不得穿透到服务内部状态。"""
        specs = ({"sink": "langfuse", "projection_version": "v1"},)
        service = make_service(store, delivery_sink_specs=specs)
        exported = service.delivery_sink_specs
        assert exported == ({"sink": "langfuse", "projection_version": "v1"},)
        # 外层 tuple / 内层 dict 的修改均不得影响服务内部清单
        exported[0]["sink"] = "mutated"
        specs[0]["projection_version"] = "v2"
        assert service.delivery_sink_specs == (
            {"sink": "langfuse", "projection_version": "v1"},
        )

    async def test_without_sink_specs_no_deliveries(self, store):
        """未注入 sink spec（组合根未接线投递）不建投递行，人评结果
        语义与既有行为兼容。"""
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        outcome = await service.submit_feedback(
            "user-1", make_feedback_request(value=True)
        )
        assert await store.list_deliveries(result_id=outcome.result_id) == []

    async def test_value_validated_against_definition(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        with pytest.raises(EvaluationValidationError):
            await service.submit_feedback(
                "user-1", make_feedback_request(value="yes")
            )

    async def test_unknown_definition_not_found(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationNotFoundError):
            await service.submit_feedback("user-1", make_feedback_request())

    async def test_new_revision_new_ids_and_supersedes(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        first = await service.submit_feedback(
            "user-1", make_feedback_request(value=True)
        )
        second = await service.submit_feedback(
            "user-1", make_feedback_request(value=False)
        )

        assert second.revision == 2
        assert second.feedback_id != first.feedback_id
        assert second.result_id != first.result_id
        assert second.superseded_feedback_id == first.feedback_id
        feedback = await store.get_feedback(second.feedback_id)
        assert feedback["supersedes_id"] == first.feedback_id
        result = await store.load_result(second.result_id)
        assert result["supersedes_result_id"] == first.result_id
        assert result["typed_value"] is False
        assert result["passed"] is False
        # current 视图只保留最新 revision 的 human result
        page = await service.list_results(definition_id="feedback.helpful")
        assert [item["result_id"] for item in page.items] == [second.result_id]

    async def test_cas_expected_revision_mismatch(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        await service.submit_feedback("user-1", make_feedback_request(value=True))
        with pytest.raises(EvaluationRevisionConflictError):
            await service.submit_feedback(
                "user-1", make_feedback_request(value=False, expected_revision=5)
            )

    async def test_cas_concurrent_single_winner(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        await service.submit_feedback("user-1", make_feedback_request(value=True))

        outcomes = await asyncio.gather(
            service.submit_feedback(
                "user-1",
                make_feedback_request(value=False, expected_revision=1, request_key="k1"),
            ),
            service.submit_feedback(
                "user-1",
                make_feedback_request(value=True, expected_revision=1, request_key="k2"),
            ),
            return_exceptions=True,
        )
        errors = [o for o in outcomes if isinstance(o, Exception)]
        succeeded = [o for o in outcomes if not isinstance(o, Exception)]
        assert len(succeeded) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], EvaluationRevisionConflictError)

    async def test_idempotency_key_same_payload_reuses(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        first = await service.submit_feedback(
            "user-1", make_feedback_request(value=True, request_key="req-1")
        )
        replay = await service.submit_feedback(
            "user-1", make_feedback_request(value=True, request_key="req-1")
        )

        assert replay.reused is True
        assert replay.feedback_id == first.feedback_id
        assert replay.result_id == first.result_id
        assert replay.revision == 1
        rows = await store.list_current_feedback(definition_id="feedback.helpful")
        assert len(rows) == 1

    async def test_idempotency_key_different_payload_conflicts(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        await service.submit_feedback(
            "user-1", make_feedback_request(value=True, request_key="req-1")
        )
        with pytest.raises(EvaluationConflictError):
            await service.submit_feedback(
                "user-1", make_feedback_request(value=False, request_key="req-1")
            )

    async def test_disabled_short_circuits(self, store):
        service = make_service(store, enabled=False)
        with pytest.raises(EvaluationDisabledError):
            await service.submit_feedback("user-1", make_feedback_request())

    async def test_crash_window_replay_completes_missing_result(self, store):
        """崩溃窗口：feedback 行已落库（request-key 派生 id）但 result 行
        缺失（save_feedback_with_result 之前的历史残留），同 key 重放补齐
        human result（恢复路径），且不新建 revision。"""
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        request_key = "req-crash"
        feedback_id = hashlib.sha256(
            f"feedback-request:{request_key}".encode("utf-8")
        ).hexdigest()[:32]
        await store.save_feedback({
            "feedback_id": feedback_id,
            "actor_ref": "user-1",
            "definition_id": "feedback.helpful",
            "definition_version": "v1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "rev-1",
            },
            "value": True,
            "revision": 1,
        })
        assert await store.get_result_id_by_feedback_id(feedback_id) is None

        outcome = await service.submit_feedback(
            "user-1", make_feedback_request(value=True, request_key=request_key)
        )

        assert outcome.reused is True
        assert outcome.feedback_id == feedback_id
        assert outcome.revision == 1
        result = await store.load_result(outcome.result_id)
        assert result["feedback_id"] == feedback_id
        assert result["provenance"] == "human"
        assert result["typed_value"] is True

    async def test_concurrent_double_replay_creates_single_result(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        first = await service.submit_feedback(
            "user-1", make_feedback_request(value=True, request_key="req-c")
        )
        outcomes = await asyncio.gather(
            service.submit_feedback(
                "user-1", make_feedback_request(value=True, request_key="req-c")
            ),
            service.submit_feedback(
                "user-1", make_feedback_request(value=True, request_key="req-c")
            ),
            return_exceptions=True,
        )
        assert all(not isinstance(o, Exception) for o in outcomes)
        assert all(o.feedback_id == first.feedback_id for o in outcomes)
        # 恰好一个 result：两条重放收敛到同一 result_id
        assert {o.result_id for o in outcomes} == {first.result_id}
        assert await store.get_result_id_by_feedback_id(first.feedback_id) == (
            first.result_id
        )


# ---------------------------------------------------------------------------
# feedback.helpful 人评定义注册
# ---------------------------------------------------------------------------


class TestFeedbackHelpfulDefinition:
    async def test_register_idempotent(self, store):
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        await service.register_feedback_helpful_definition()

        row = await store.get_definition("feedback.helpful", "v1")
        assert row["metric_name"] == "feedback.helpful"
        definition = definition_from_mapping(row["definition"])
        assert definition.method == "human"
        assert definition.output_type == "boolean"

    async def test_unanswered_feedback_never_counts_as_false(self, store):
        """未反馈不计 false：无提交即无 human result 行。"""
        service = make_service(store)
        await service.register_feedback_helpful_definition()
        page = await service.list_results(definition_id="feedback.helpful")
        assert page.items == ()


class TestDefinitionsForTarget:
    """T22 线上样本作业展开的定义解析（按目标种类；T5 新语义：usage 显式
    启用 + method ∈ {deterministic, llm_judge}）。"""

    async def _set_usage(self, store, definition_id, version, online_enabled):
        """显式启停 usage（注册已写 disabled usage 行，CAS 用当前 revision）。"""
        usage = await store.get_definition_usage(definition_id, version)
        return await store.upsert_definition_usage(
            definition_id,
            version,
            online_enabled=online_enabled,
            expected_revision=usage["revision"] if usage else 0,
            updated_by="test",
        )

    async def test_filters_human_and_matches_kind(self, store):
        """usage 启用的自动定义按目标种类展开；human 永不展开。"""
        service = make_service(store)
        await service.register_feedback_helpful_definition()  # human/agent_run
        from app.application.evaluation_service import DefinitionKey

        for def_id, kind, method in (
            ("agent.answer_relevance", "agent_run", "llm_judge"),
            ("kb.recall_at_5", "retrieval", "deterministic"),
        ):
            await store.save_definition(
                {
                    "definition_id": def_id,
                    "version": "v1",
                    "metric_name": def_id,
                    "target_kind": kind,
                    "method": method,
                }
            )
            # T5 新语义：usage 启用后才被选中（save_definition 不写 usage 行）
            await self._set_usage(store, def_id, "v1", True)
        agent_keys = await service.definitions_for_target("agent_run")
        # human 的 feedback.helpful（target agent_run）被过滤，不展开作业
        assert agent_keys == (DefinitionKey("agent.answer_relevance", "v1"),)
        retrieval_keys = await service.definitions_for_target("retrieval")
        assert retrieval_keys == (DefinitionKey("kb.recall_at_5", "v1"),)

    async def test_unregistered_kind_returns_empty(self, store):
        service = make_service(store)
        assert await service.definitions_for_target("agent_run") == ()

    async def test_definition_without_usage_row_not_selected(self, store):
        """T5：无 usage 行的自动定义（模拟迁移前）不被选中——保守 fail-closed，
        迁移由一次性回填兜底。"""
        service = make_service(store)
        await store.save_definition(
            {
                "definition_id": "kb.recall_at_5",
                "version": "v1",
                "metric_name": "kb.recall_at_5",
                "target_kind": "retrieval",
                "method": "deterministic",
            }
        )
        assert await service.definitions_for_target("retrieval") == ()

    async def test_enabled_usage_row_selected_and_disabled_not(self, store):
        """T5：注册默认 disabled 不被选中；显式启用后被选中。"""
        service = make_service(store)
        definition = make_definition("kb.recall_at_5", "v1")
        await service.register_definition(definition)
        assert await service.definitions_for_target("retrieval") == ()
        await self._set_usage(store, "kb.recall_at_5", "v1", True)
        assert await service.definitions_for_target("retrieval") == (
            DefinitionKey("kb.recall_at_5", "v1"),
        )
        # 停用后不再选中
        await self._set_usage(store, "kb.recall_at_5", "v1", False)
        assert await service.definitions_for_target("retrieval") == ()

    async def test_human_never_selected_even_when_usage_enabled(self, store):
        """T5：human 定义即使 usage 被置 enabled 也不展开自动作业。"""
        service = make_service(store)
        definition = EvaluationService.helpful_definition_factory(
            "feedback.helpful", "v1"
        )
        await service.register_definition(definition)
        await self._set_usage(store, "feedback.helpful", "v1", True)
        assert await service.definitions_for_target("agent_run") == ()

    async def test_same_metric_two_enabled_versions_both_returned(self, store):
        """T5：同指标两版本同时启用都返回（不合并，spec 4.3）。"""
        service = make_service(store)
        for version in ("v1", "v2"):
            await service.register_definition(
                make_definition("kb.recall_at_5", version)
            )
            await self._set_usage(store, "kb.recall_at_5", version, True)
        keys = await service.definitions_for_target("retrieval")
        assert set(keys) == {
            DefinitionKey("kb.recall_at_5", "v1"),
            DefinitionKey("kb.recall_at_5", "v2"),
        }


# ---------------------------------------------------------------------------
# retry_delivery
# ---------------------------------------------------------------------------


class TestRetryDelivery:
    async def _committed_result_with_two_sinks(self, store):
        sample_id = await store.save_sample({
            "service": "n-agent",
            "producer_event_id": "evt-r1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "rev-1",
            },
            "snapshot_ref": "snapshots/r1.json",
            "snapshot_hash": "b" * 64,
        })
        job_id = await store.save_job({
            "sample_id": sample_id,
            "definition_id": "kb.context_relevance",
            "definition_version": "v1",
        })
        claimed = await store.claim_next_job("worker-1")
        result_id = await store.commit_result_with_deliveries(
            {
                "job_id": job_id,
                "provenance": "automatic",
                "metric_name": "kb.context_relevance",
                "status": "value",
                "typed_value": 1,
                "passed": True,
            },
            deliveries=[
                {"sink": "langfuse", "projection_version": "v1", "payload": {"score": 1}},
                {
                    "sink": "otel",
                    "projection_version": "v1",
                    "status": "retry_wait",
                    "last_error_type": "timeout",
                    "payload": {"score": 1},
                },
            ],
            lease_token=claimed["lease_token"],
            job_terminal_status="succeeded",
        )
        return job_id, result_id

    async def test_requeues_only_specified_sink_without_payload_or_job_changes(self, store):
        service = make_service(store)
        job_id, result_id = await self._committed_result_with_two_sinks(store)

        updated = await service.retry_delivery("otel", result_id)

        assert updated["status"] == "pending"
        deliveries = await store.list_deliveries(result_id)
        by_sink = {d["sink"]: d for d in deliveries}
        # 只重试指定 sink：otel 回 pending；langfuse 不受影响
        assert by_sink["otel"]["status"] == "pending"
        assert by_sink["langfuse"]["status"] == "pending"
        # 不改已冻结 payload
        assert by_sink["otel"]["payload_hash"] == by_sink["langfuse"]["payload_hash"]
        # 不新建 job：job 仍为终态 succeeded
        job = await store.get_job(job_id)
        assert job["status"] == "succeeded"

    async def test_pending_delivery_retry_is_noop(self, store):
        service = make_service(store)
        _, result_id = await self._committed_result_with_two_sinks(store)
        first = await service.retry_delivery("otel", result_id)
        again = await service.retry_delivery("otel", result_id)
        assert again["status"] == "pending"
        assert again["attempt_count"] == first["attempt_count"]

    async def test_dead_letter_can_be_retried(self, store):
        service = make_service(store)
        _, result_id = await self._committed_result_with_two_sinks(store)
        delivery = next(
            d for d in await store.list_deliveries(result_id) if d["sink"] == "otel"
        )
        await store.record_delivery_transition(
            delivery["delivery_id"], "dead_letter", error_type="exhausted"
        )
        updated = await service.retry_delivery("otel", result_id)
        assert updated["status"] == "pending"

    async def test_verified_terminal_cannot_retry(self, store):
        service = make_service(store)
        _, result_id = await self._committed_result_with_two_sinks(store)
        # retry_wait -> pending -> accepted -> verified（经领域状态机合法路径）
        await service.retry_delivery("otel", result_id)
        delivery = next(
            d for d in await store.list_deliveries(result_id) if d["sink"] == "otel"
        )
        await store.record_delivery_transition(delivery["delivery_id"], "accepted")
        await store.record_delivery_transition(delivery["delivery_id"], "verified")
        with pytest.raises(EvaluationConflictError):
            await service.retry_delivery("otel", result_id)

    async def test_unknown_sink_result_not_found(self, store):
        service = make_service(store)
        _, result_id = await self._committed_result_with_two_sinks(store)
        with pytest.raises(EvaluationNotFoundError):
            await service.retry_delivery("unknown-sink", result_id)

    async def test_disabled_delivery_can_be_retried(self, store):
        service = make_service(store)
        _, result_id = await self._committed_result_with_two_sinks(store)
        delivery = next(
            d for d in await store.list_deliveries(result_id) if d["sink"] == "otel"
        )
        # pending -> disabled（出口关闭保留状态）；恢复重试回 pending
        await store.record_delivery_transition(delivery["delivery_id"], "disabled")
        updated = await service.retry_delivery("otel", result_id)
        assert updated["status"] == "pending"

    async def test_retry_picks_latest_created_delivery_not_lexicographic_projection(
        self, store
    ):
        """v9/v10：字典序 max（v9 > v10）不得决定挑选——必须按
        (created_at, delivery_id) 取最新创建的投递。"""
        import sqlite3 as _sqlite3
        from datetime import datetime, timedelta, timezone

        service = make_service(store)
        _, result_id = await self._committed_result_with_two_sinks(store)
        now = datetime.now(timezone.utc)
        older_id = "delivery-older-v9"
        newer_id = "delivery-newer-v10"
        with _sqlite3.connect(store.path) as conn:
            # 更早创建的 v9（字典序最大）；更晚创建的 v10（字典序更小）
            for delivery_id, projection, status, created_at in (
                (older_id, "v9", "pending", now + timedelta(seconds=10)),
                (newer_id, "v10", "dead_letter", now + timedelta(seconds=20)),
            ):
                conn.execute(
                    "INSERT INTO evaluation_deliveries (delivery_id, result_id, "
                    "sink, projection_version, status, payload_hash, "
                    "created_at, updated_at) VALUES (?, ?, 'langfuse', ?, ?, "
                    "'seeded', ?, ?)",
                    (
                        delivery_id,
                        result_id,
                        projection,
                        status,
                        created_at.isoformat(),
                        created_at.isoformat(),
                    ),
                )
            conn.commit()

        updated = await service.retry_delivery("langfuse", result_id)

        # 挑中最新创建的 v10（dead_letter -> pending），而非字典序最大的 v9
        assert updated["delivery_id"] == newer_id
        assert updated["status"] == "pending"
        deliveries = await store.list_deliveries(result_id)
        by_id = {d["delivery_id"]: d for d in deliveries}
        assert by_id[older_id]["status"] == "pending"  # 旧投影未被触碰


# ---------------------------------------------------------------------------
# 查询：cursor 分页 + 过滤 + 详情
# ---------------------------------------------------------------------------


def _sha(content) -> str:
    return hashlib.sha256(canonical_json_bytes(content)).hexdigest()


class TestQueries:
    async def test_experiment_distribution_preserves_numeric_scores(self, store):
        await store.save_experiment({
            "experiment_id": "exp-scores", "dataset_id": "ds", "dataset_version": "1",
            "dataset_hash": "a" * 64, "target_config_hash": "b" * 64,
            "evaluator_set_hash": "c" * 64, "status": "created",
        })
        cases = [
            ("score", 1.0, None, "value"), ("score", 0.0, None, "value"),
            ("score", 0.5, None, "value"), ("score", None, None, "error"),
            ("bool", True, True, "value"), ("bool", False, False, "value"),
            ("category", "good", None, "value"),
        ]
        for index, (metric, value, passed, status) in enumerate(cases):
            sample = await store.save_sample({"service": "n-agent", "producer_event_id": f"score-{index}"})
            job = await store.save_job({"sample_id": sample, "definition_id": metric,
                                        "definition_version": "1", "experiment_id": "exp-scores"})
            await store.save_result({"job_id": job, "provenance": "automatic",
                                     "metric_name": metric, "status": status,
                                     "typed_value": value, "passed": passed})
        overview = await make_service(store).experiment_overview("exp-scores")
        score = overview["distribution"]["score"]
        assert score["numeric_count"] == 3
        assert score["numeric_mean"] == 0.5
        assert score["numeric_min"] == 0.0
        assert score["numeric_max"] == 1.0
        assert (score["passed"], score["failed"], score["error_or_skipped"]) == (0, 0, 1)
        boolean = overview["distribution"]["bool"]
        assert (boolean["passed"], boolean["failed"]) == (1, 1)
        for metric in ("bool", "category"):
            assert overview["distribution"][metric]["numeric_count"] == 0
            assert overview["distribution"][metric]["numeric_mean"] is None

    async def _seed_results(self, store, service, count: int = 5):
        result_ids = []
        for index in range(count):
            sample_id = await store.save_sample({
                "service": "n-agent",
                "producer_event_id": f"evt-q{index}",
                "target": {
                    "service": "n-agent",
                    "kind": "agent_run",
                    "subject_id": f"run-{index}",
                    "subject_revision": "rev-1",
                },
            })
            job_id = await store.save_job({
                "sample_id": sample_id,
                "definition_id": "kb.context_relevance",
                "definition_version": "v1",
                "experiment_id": "exp-1" if index % 2 == 0 else None,
            })
            result_id = await store.save_result({
                "job_id": job_id,
                "provenance": "automatic",
                "metric_name": "kb.context_relevance",
                "status": "value" if index % 2 == 0 else "error",
                "typed_value": 1.0 if index % 2 == 0 else None,
            })
            result_ids.append(result_id)
        return result_ids

    async def test_cursor_pagination_walks_all_without_dup_or_loss(self, store):
        service = make_service(store)
        seeded = await self._seed_results(store, service)

        collected = []
        cursor = None
        while True:
            page = await service.list_results(cursor=cursor, limit=2)
            collected.extend(item["result_id"] for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        # 时间倒序：收集顺序与写入顺序相反，且不重不漏
        assert collected == seeded[::-1]

    async def test_filter_by_status_and_experiment(self, store):
        service = make_service(store)
        await self._seed_results(store, service)

        page = await service.list_results(status="value")
        assert len(page.items) == 3
        assert all(item["status"] == "value" for item in page.items)

        page = await service.list_results(experiment_id="exp-1")
        assert len(page.items) == 3

        page = await service.list_results(experiment_id="exp-1", status="value")
        assert len(page.items) == 3

        page = await service.list_results(experiment_id="exp-2")
        assert page.items == ()

    async def test_filter_by_target(self, store):
        service = make_service(store)
        await self._seed_results(store, service)

        page = await service.list_results(target_service="n-agent")
        assert len(page.items) == 5
        page = await service.list_results(target_subject_id="run-3")
        assert len(page.items) == 1
        # json_extract 过滤 sample 侧 target_json：kind 与 subject_revision
        page = await service.list_results(target_kind="agent_run")
        assert len(page.items) == 5
        page = await service.list_results(target_subject_revision="rev-1")
        assert len(page.items) == 5
        page = await service.list_results(target_kind="session_feedback")
        assert page.items == ()
        page = await service.list_results(target_service="n-kb")
        assert page.items == ()

    async def test_limit_validated_and_capped(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationValidationError):
            await service.list_results(limit=0)
        with pytest.raises(EvaluationValidationError):
            await service.list_results(limit=-1)
        # 超上限按 200 封顶：不报错、正常返回当前页
        seeded = await self._seed_results(store, service)
        page = await service.list_results(limit=500)
        assert [item["result_id"] for item in page.items] == seeded[::-1]

    async def test_invalid_cursor_rejected(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationValidationError):
            await service.list_results(cursor="not-a-cursor")

    async def test_results_snapshot_returns_triple_and_reuses_snapshot_at(
        self, store,
    ):
        service = make_service(store)
        await self._seed_results(store, service)
        rows, next_token, snapshot_at = await service.list_results_snapshot(
            status="value", limit=2
        )
        assert len(rows) == 2
        assert next_token
        assert snapshot_at
        rows2, next_token2, snapshot_at2 = await service.list_results_snapshot(
            status="value", cursor=next_token
        )
        assert len(rows2) == 1
        assert next_token2 is None
        # 快照上界经 v2 游标固定复用（翻页间不重取 now）
        assert snapshot_at2 == snapshot_at

    async def test_result_detail_includes_attempts_and_evidence_summary(self, store):
        service = make_service(store)
        sample_id = await store.save_sample({
            "service": "n-agent",
            "producer_event_id": "evt-detail",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-9",
                "subject_revision": "rev-1",
            },
            "snapshot_ref": "snapshots/detail.json",
            "snapshot_hash": "c" * 64,
            "schema_version": "evaluation.sample.v1",
        })
        job_id = await store.save_job({
            "sample_id": sample_id,
            "definition_id": "kb.context_relevance",
            "definition_version": "v1",
        })
        claimed = await store.claim_next_job("worker-1")
        result_id = await store.commit_result_with_deliveries(
            {
                "job_id": job_id,
                "provenance": "automatic",
                "metric_name": "kb.context_relevance",
                "status": "value",
                "typed_value": 1,
                "passed": True,
            },
            deliveries=[
                {"sink": "langfuse", "projection_version": "v1", "payload": {"score": 1}},
            ],
            lease_token=claimed["lease_token"],
            job_terminal_status="succeeded",
        )

        detail = await service.get_result_detail(result_id)

        assert detail.result["result_id"] == result_id
        assert detail.job["job_id"] == job_id
        assert len(detail.attempts) == 1  # claim 即追加 attempt
        assert len(detail.deliveries) == 1
        assert detail.sample["sample_id"] == sample_id
        assert detail.evidence_summary["snapshot_ref"] == "snapshots/detail.json"
        assert detail.evidence_summary["snapshot_hash"] == "c" * 64

    async def test_result_detail_not_found(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationNotFoundError):
            await service.get_result_detail("missing")


# ---------------------------------------------------------------------------
# start_experiment：只冻结并持久化请求（jobs 展开属 T13）
# ---------------------------------------------------------------------------


class TestStartExperiment:
    async def test_freezes_hashes_and_persists_without_jobs(self, store):
        service = make_service(store)
        await service.register_definition(make_definition("kb.context_relevance"))
        await service.register_definition(make_definition("kb.recall_at_5"))
        target_config = {"model": "gpt-x", "temperature": 0}
        request = StartExperimentRequest(
            dataset_id="ds-1",
            dataset_version="2026.11",
            dataset_hash="b" * 64,
            target_config=target_config,
            evaluator_set=(
                DefinitionKey("kb.context_relevance", "v1"),
                DefinitionKey("kb.recall_at_5", "v1"),
            ),
            mode="execute",
        )

        outcome = await service.start_experiment(request)

        experiment = await store.get_experiment(outcome.experiment_id)
        assert experiment["dataset_id"] == "ds-1"
        assert experiment["dataset_version"] == "2026.11"
        assert experiment["dataset_hash"] == "b" * 64
        assert experiment["target_config_hash"] == _sha(target_config)
        # evaluator set hash 来自 store 解析出的 definition hash（非调用方自报）
        row1 = await store.get_definition("kb.context_relevance", "v1")
        row2 = await store.get_definition("kb.recall_at_5", "v1")
        expected_set_hash = _sha([
            {
                "definition_id": "kb.context_relevance",
                "version": "v1",
                "definition_hash": row1["definition_hash"],
                "metric_name": row1["metric_name"],
            },
            {
                "definition_id": "kb.recall_at_5",
                "version": "v1",
                "definition_hash": row2["definition_hash"],
                "metric_name": row2["metric_name"],
            },
        ])
        assert experiment["evaluator_set_hash"] == expected_set_hash
        assert experiment["status"] == "created"
        assert experiment["mode"] == "execute"
        # 本阶段只持久化冻结请求：不创建任何 job（sample 存在前禁止展开）
        assert await store.claim_next_job("probe-worker") is None

    async def test_unknown_evaluator_definition_rejected(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationNotFoundError):
            await service.start_experiment(StartExperimentRequest(
                dataset_id="ds-1",
                dataset_version="2026.11",
                dataset_hash="b" * 64,
                target_config={},
                evaluator_set=(DefinitionKey("kb.unknown", "v1"),),
            ))

    async def test_invalid_dataset_hash_rejected(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationValidationError):
            await service.start_experiment(StartExperimentRequest(
                dataset_id="ds-1",
                dataset_version="2026.11",
                dataset_hash="not-a-hash",
                target_config={},
                evaluator_set=(),
            ))

    async def test_disabled_short_circuits(self, store):
        service = make_service(store, enabled=False)
        with pytest.raises(EvaluationDisabledError):
            await service.start_experiment(StartExperimentRequest(
                dataset_id="ds-1",
                dataset_version="2026.11",
                dataset_hash="b" * 64,
                target_config={},
                evaluator_set=(),
            ))


# ---------------------------------------------------------------------------
# cancel 用例
# ---------------------------------------------------------------------------


class TestCancel:
    async def test_cancel_queued_jobs_for_samples_keeps_running(self, store):
        service = make_service(store)
        await store.save_definition({
            "definition_id": "kb.context_relevance",
            "version": "v1",
            "metric_name": "kb.context_relevance",
        })
        # running job（直接经 store 构造）
        running_sample = await store.save_sample({
            "service": "n-agent",
            "producer_event_id": "evt-running",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-r",
                "subject_revision": "rev-1",
            },
        })
        await store.save_job({
            "sample_id": running_sample,
            "definition_id": "kb.context_relevance",
            "definition_version": "v1",
            "idempotency_key": "running-job",
        })
        claimed = await store.claim_next_job("worker-1")
        # queued job（经 service 提交）
        queued = await service.submit_sample(
            make_sample_request(
                producer_event_id="evt-queued",
                definitions=(DefinitionKey("kb.context_relevance", "v1"),),
            )
        )

        cancelled = await service.cancel_sample_jobs(
            [running_sample, queued.sample_id]
        )

        assert cancelled == tuple(queued.job_ids)
        queued_job = await store.get_job(queued.job_ids[0])
        assert queued_job["status"] == "cancelled"
        running_job = await store.get_job(claimed["job_id"])
        assert running_job["status"] == "running"

    async def test_cancel_available_even_when_disabled(self, store):
        service = make_service(store, enabled=False)
        sample_id = await store.save_sample({
            "service": "n-agent",
            "producer_event_id": "evt-cancel-disabled",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-c",
                "subject_revision": "rev-1",
            },
        })
        await store.save_job({
            "sample_id": sample_id,
            "definition_id": "kb.context_relevance",
            "definition_version": "v1",
        })
        cancelled = await service.cancel_sample_jobs([sample_id])
        assert len(cancelled) == 1


# ---------------------------------------------------------------------------
# record_producer_state（T17 producer 接收端；应用层专属，不经路由）
# ---------------------------------------------------------------------------


def _producer_state_record(**overrides):
    record = {
        "producer_id": "n-kb-outbox-1",
        "counter_epoch": "epoch-1",
        "sequence": 5,
        "eligible_count": 100,
        "selected_count": 5,
        "captured_count": 5,
        "capture_error_count": 0,
        "queue_full_count": 0,
        "observed_at": "2026-11-11T00:00:00+00:00",
    }
    record.update(overrides)
    return record


class TestRecordProducerState:
    async def test_first_record_applied_and_persisted(self, store):
        service = make_service(store)
        result = await service.record_producer_state(_producer_state_record())
        assert isinstance(result, ProducerStateResult)
        assert result.applied is True
        row = await store.get_producer_state("n-kb-outbox-1", "epoch-1")
        assert row["sequence"] == 5
        assert row["eligible_count"] == 100

    async def test_same_sequence_resend_idempotent_no_accumulation(self, store):
        service = make_service(store)
        await service.record_producer_state(_producer_state_record())
        result = await service.record_producer_state(_producer_state_record())
        # 同 sequence 重发：幂等无副作用受理，计数不累加
        assert result.applied is False
        row = await store.get_producer_state("n-kb-outbox-1", "epoch-1")
        assert row["eligible_count"] == 100
        assert row["selected_count"] == 5

    async def test_lower_sequence_rejected(self, store):
        service = make_service(store)
        await service.record_producer_state(_producer_state_record())
        with pytest.raises(EvaluationStaleSequenceError):
            await service.record_producer_state(_producer_state_record(sequence=3))

    async def test_epoch_change_accepted(self, store):
        service = make_service(store)
        await service.record_producer_state(_producer_state_record())
        # 重建计数存储换 epoch：新 epoch 序列从 0 重新开始也接受
        result = await service.record_producer_state(
            _producer_state_record(counter_epoch="epoch-2", sequence=0)
        )
        assert result.applied is True
        row = await store.get_producer_state("n-kb-outbox-1", "epoch-2")
        assert row["sequence"] == 0

    async def test_heartbeat_stale_beyond_threshold(self, store):
        service = make_service(store)
        stale_at = (
            datetime.now(timezone.utc)
            - timedelta(seconds=PRODUCER_HEARTBEAT_STALE_SECONDS + 20)
        ).isoformat()
        result = await service.record_producer_state(
            _producer_state_record(observed_at=stale_at)
        )
        assert result.heartbeat_stale is True

    async def test_fresh_heartbeat_not_stale(self, store):
        service = make_service(store)
        fresh_at = (
            datetime.now(timezone.utc) - timedelta(seconds=10)
        ).isoformat()
        result = await service.record_producer_state(
            _producer_state_record(observed_at=fresh_at)
        )
        assert result.heartbeat_stale is False

    async def test_unparseable_observed_at_treated_stale(self, store):
        # 防御纵深：路由层已 422 拦截不可解析时间戳，应用层对漏网输入
        # fail-safe 按过期处理（覆盖率 unknown，缺失区间不补造统计）
        service = make_service(store)
        result = await service.record_producer_state(
            _producer_state_record(observed_at="not-a-timestamp")
        )
        assert result.heartbeat_stale is True

    async def test_missing_required_fields_rejected(self, store):
        service = make_service(store)
        with pytest.raises(EvaluationValidationError):
            await service.record_producer_state({"sequence": 1})
        with pytest.raises(EvaluationValidationError):
            await service.record_producer_state(
                _producer_state_record(sequence=True)
            )
        with pytest.raises(EvaluationValidationError):
            await service.record_producer_state(
                _producer_state_record(observed_at="")
            )


class _StaleRaceStore:
    """模拟 stale 回查竞态窗口（Mi2 契约对齐测试）。

    save 首次抛 EvaluationStaleSequenceError，但此刻 get 查无行——
    服务必须按"重发但无行"重放一次 save，而不是把 NotFound 当 None
    走完判定后误判为低水位拒绝。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.save_calls = 0

    async def save_producer_state(self, record: dict) -> None:
        self.save_calls += 1
        if self.save_calls == 1:
            raise EvaluationStaleSequenceError(
                "race: stale judged, row missing on recheck"
            )
        return await self._inner.save_producer_state(record)

    async def get_producer_state(self, producer_id: str, counter_epoch: str):
        return await self._inner.get_producer_state(producer_id, counter_epoch)


class TestRecordProducerStateContract:
    async def test_stale_with_missing_row_replays_save(self, store):
        race_store = _StaleRaceStore(store)
        service = make_service(race_store)
        result = await service.record_producer_state(_producer_state_record())
        assert result.applied is True
        assert race_store.save_calls == 2
        row = await store.get_producer_state("n-kb-outbox-1", "epoch-1")
        assert row["sequence"] == 5

    async def test_garbage_count_values_rejected(self, store):
        # Mi3：应用层防御纵深——五个计数必须是非负整数（bool/字符串/
        # 负数/None 全部拒绝，与路由共用 domain require_non_negative_int）
        service = make_service(store)
        for bad in (True, "5", -1, None):
            with pytest.raises(EvaluationValidationError):
                await service.record_producer_state(
                    _producer_state_record(eligible_count=bad)
                )
        # 计数缺键同样拒绝：高水位绝对值语义下缺省 0 会掩盖协议错误
        record = _producer_state_record()
        del record["eligible_count"]
        with pytest.raises(EvaluationValidationError):
            await service.record_producer_state(record)


# ---------------------------------------------------------------------------
# 30 项人工评审修复轮（#1/#29）
# ---------------------------------------------------------------------------


async def test_preset_definitions_shapes(store):
    """#1：spec 8 正式指标预设定义形状——11 个（6 deterministic + 5
    judge，plan T3 新增 agent.scenario_assertions/scenario_quality），
    target_kind / method / judge 字段与目录一致。"""
    defs = EvaluationService.preset_definitions("judge-model-a")
    # 11 正式 + 7 experiment_item variant（agent_run 预设镜像，同 metric
    # 名、版本域 experiment-v1；24 项三方审阅 #1）
    assert len(defs) == 20
    by_id = {(d.definition_id, d.version): d for d in defs}
    assert len(by_id) == 20
    defs_v1 = {d.definition_id: d for d in defs if d.version == "v1"}
    variants = [d for d in defs if d.version == "experiment-v1"]
    assert {d.definition_id for d in variants} == {
        "agent.task_success",
        "agent.tool_contract",
        "agent.scenario_assertions",
        "agent.answer_correctness",
        "agent.answer_relevance",
        "rag.faithfulness",
        "agent.scenario_quality",
    }
    for variant in variants:
        base = defs_v1[variant.definition_id]
        assert variant.target_kind == "experiment_item"
        assert variant.metric_name == base.metric_name
        assert variant.method == base.method
        assert variant.judge_model == base.judge_model
    deterministic = {
        "agent.task_success": "agent_run",
        "agent.tool_contract": "agent_run",
        "agent.scenario_assertions": "agent_run",
        "kb.recall_at_5": "retrieval",
        "kb.ndcg_at_5": "retrieval",
        "kb.mrr_at_5": "retrieval",
    }
    for definition_id, kind in deterministic.items():
        d = defs_v1[definition_id]
        assert d.method == "deterministic"
        assert d.target_kind == kind
        assert d.judge_model is None
    judges = {
        "agent.answer_correctness": "agent_run",
        "agent.answer_relevance": "agent_run",
        "rag.faithfulness": "agent_run",
        "agent.scenario_quality": "agent_run",
        "kb.context_relevance": "retrieval",
    }
    for definition_id, kind in judges.items():
        d = defs_v1[definition_id]
        assert d.method == "llm_judge"
        assert d.target_kind == kind
        assert d.judge_model == "judge-model-a"
    # Judge 未配置：只注册 deterministic 预设（llm_judge 定义必须声明
    # judge_model，domain 不变量；注册即死条目，不注册）
    no_judge = EvaluationService.preset_definitions(None)
    # 6 deterministic 正式 + 3 个 agent_run deterministic 镜像 variant
    assert len(no_judge) == 9
    assert all(d.method == "deterministic" for d in no_judge)


async def test_preset_model_change_registers_new_versions_without_overwrite(store):
    service = EvaluationService(store, enabled=True)
    await service.register_preset_definitions("judge-original")
    original = await store.get_definition("agent.answer_correctness", "v1")
    await service.register_preset_definitions("judge-new")
    unchanged = await store.get_definition("agent.answer_correctness", "v1")
    assert unchanged["definition_hash"] == original["definition_hash"]
    definitions = await store.list_definitions(target_kind="experiment_item")
    candidates = [row for row in definitions if row["definition_id"] == "agent.answer_correctness"]
    assert len(candidates) == 2
    new = await store.get_definition("agent.answer_correctness", next(row["version"] for row in candidates if row["version"] != "experiment-v1"))
    assert new["definition"]["judge_model"] == "judge-new"
    before = len(await store.list_definitions())
    await service.register_preset_definitions("judge-new")
    assert len(await store.list_definitions()) == before


async def test_register_preset_definitions_idempotent(store):
    """#1：启动期幂等注册（同版本同内容重放不 409）；T5 新语义：注册默认
    disabled（不写 enabled usage），显式启用后 definitions_for_target 才
    展开（human 不含）；幂等重放不复活已停用状态。"""
    service = make_service(store)
    await service.register_preset_definitions("judge-model-a")
    # 注册默认 disabled：usage 行存在但 online_enabled=False，不展开作业
    usage = await store.get_definition_usage("agent.answer_relevance", "v1")
    assert usage is not None
    assert usage["online_enabled"] is False
    agent_usage = await store.get_definition_usage("feedback.helpful", "v1")
    assert agent_usage is None  # human 定义不经预设注册路径
    assert await service.definitions_for_target("agent_run") == ()

    # 显式启用后按目标种类展开
    for def_id, version in (
        ("agent.answer_relevance", "v1"),
        ("rag.faithfulness", "v1"),
        ("agent.task_success", "experiment-v1"),
    ):
        usage = await store.get_definition_usage(def_id, version)
        await store.upsert_definition_usage(
            def_id, version, online_enabled=True,
            expected_revision=usage["revision"] if usage else 0,
            updated_by="test",
        )
    agent_ids = {
        k.definition_id for k in await service.definitions_for_target("agent_run")
    }
    assert {"agent.answer_relevance", "rag.faithfulness"} <= agent_ids
    assert "feedback.helpful" not in agent_ids  # human 不展开作业
    experiment_ids = {
        (k.definition_id, k.definition_version)
        for k in await service.definitions_for_target("experiment_item")
    }
    assert ("agent.task_success", "experiment-v1") in experiment_ids

    # 幂等重放：同版本同内容不 409，且已启用状态不被复活（不重置 usage）
    await service.register_preset_definitions("judge-model-a")
    usage = await store.get_definition_usage("agent.answer_relevance", "v1")
    assert usage["online_enabled"] is True
    assert usage["revision"] == 2


async def test_feedback_reuse_only_when_latest_revision(store):
    """#29：幂等键命中但已不是最新 revision 时按新增处理——同分钟
    "有用→无用→有用"的第三击产生新 revision（不复活旧值）。"""
    service = make_service(store)
    await service.register_feedback_helpful_definition()
    request = make_feedback_request(
        target=make_target(
            service="n-agent", kind="agent_run", subject_id="run-29",
            retrieval_id=None,
        ),
        request_key="k-29",
    )
    first = await service.submit_feedback("dashboard", request)
    assert first.revision == 1 and first.reused is False
    # 反向反馈：新 revision 2（无幂等键，直接新增）
    second = await service.submit_feedback(
        "dashboard", make_feedback_request(
            target=make_target(
                service="n-agent", kind="agent_run", subject_id="run-29",
                retrieval_id=None,
            ),
            value=False,
        )
    )
    assert second.revision == 2
    # 第三击：同幂等键（value=True 同 payload）但已不是最新 -> 新 revision
    third = await service.submit_feedback("dashboard", request)
    assert third.reused is False
    assert third.revision == 3
    assert third.feedback_id != first.feedback_id
    # 真正的幂等重放（仍是最新）复用
    replay = await service.submit_feedback("dashboard", request)
    assert replay.reused is True
    assert replay.revision == 3
