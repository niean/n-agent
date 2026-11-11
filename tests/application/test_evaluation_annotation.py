# app.application.evaluation_annotation 用例测试（plan Task 6，spec 4.4）。
#
# S1 失败测试（上下文）：注入真实 SQLiteEvaluationStore（tmp DB）+
# EvaluationService（真实投递 sink 清单）+ fake evidence_reader /
# agent_run_lookup / tombstone_reader。覆盖：
# - get_annotation_context：404 / tombstone 410 / 非 agent_run 目标
#   can_submit=False / 证据缺失 unavailable / 真实 Run 不存在与 revision
#   不匹配 / 完整线程 latest 人评与 current_revision（旧线程既有最大
#   revision 不重编号）
# - submit_annotation（S4 补充）：值校验 / request_key 重放（同 payload
#   返回原成功 + is_current，异 payload 409）/ expected_revision CAS /
#   证据重读 / 目标核验 / 单事务落库（human result + supersedes +
#   deliveries）/ 并发语义（真实 SQLite store）
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.application.evaluation_annotation import (
    ANNOTATION_DASHBOARD_ACTOR,
    AnnotationService,
    EvaluationTargetDeletedError,
    EvaluationTargetMissingError,
    EvidenceUnavailableError,
)
from app.application.evaluation_service import (
    EvaluationRevisionConflictError,
    EvaluationService,
    definition_to_mapping,
)
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationNotFoundError,
)
from app.domain.evaluation.models import (
    EvaluatorDefinition,
    EvidenceSnapshot,
    EvaluationValidationError,
)
from app.infrastructure.evaluation.store import SQLiteEvaluationStore
from tests.infrastructure.evaluation._degraded_helpers import (
    drop_feedback_thread_indexes,
    force_schema_v7,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))


@pytest.fixture
def evaluation_service(store):
    return EvaluationService(
        store,
        enabled=True,
        delivery_sink_specs=(
            {"sink": "langfuse", "projection_version": "v1"},
        ),
    )


class FakeEvidenceReader:
    """evidence_reader fake：可注入正文/异常，记录调用次数。"""

    def __init__(self, *, content=None, error=None):
        self._content = content if content is not None else {
            "query": "如何重置密码",
            "request": "如何重置密码",
            "answer": "在设置页点击重置",
        }
        self._error = error
        self.calls = 0

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return EvidenceSnapshot(
            content=dict(self._content), redaction_version="v1"
        )


class FakeRunLookup:
    """agent_run_lookup fake：返回注入的 Run 行或 None，记录调用次数。"""

    def __init__(self, runs):
        self._runs = runs
        self.calls = 0

    async def __call__(self, subject_id: str):
        self.calls += 1
        return self._runs.get(subject_id)


class FakeTombstoneReader:
    """tombstone_reader fake：命中集合内的 (kind, id, revision) 视为已删。"""

    def __init__(self, blocked=()):
        self._blocked = set(blocked)
        self.calls = 0

    async def __call__(self, subject_kind: str, subject_id: str, subject_revision: str):
        self.calls += 1
        return (subject_kind, subject_id, subject_revision) in self._blocked


class _PreReadBarrier:
    """包装 store 的 get_latest_feedback 预读入口：等 ``parties`` 个协程都
    完成真实预读后才统一放行返回（超时兜底放行，避免测试挂死）。

    由于放行前任何调用方都无法进入写事务，所有调用方必然基于同一线程
    revision 快照做预读 CAS，从而把并发测试的目标交织（双方预读后写事务
    竞争、落败方走 except 链重放复核）从事件循环调度偶然变为确定。
    """

    def __init__(self, inner, *, parties: int, timeout: float = 5.0):
        self._inner = inner
        self._parties = parties
        self._timeout = timeout
        self._arrived = 0
        self._release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def get_latest_feedback(self, *args, **kwargs):
        result = await self._inner.get_latest_feedback(*args, **kwargs)
        if self._arrived >= self._parties:
            return result
        self._arrived += 1
        if self._arrived >= self._parties:
            self._release.set()
        else:
            try:
                await asyncio.wait_for(self._release.wait(), self._timeout)
            except asyncio.TimeoutError:
                pass
        return result


def make_annotation_service(
    store,
    evaluation_service,
    *,
    evidence_reader=None,
    agent_run_lookup=None,
    tombstone_reader=None,
):
    return AnnotationService(
        store,
        evaluation_service,
        evidence_reader=evidence_reader,
        agent_run_lookup=agent_run_lookup,
        tombstone_reader=tombstone_reader,
    )


def make_definition(**overrides) -> dict:
    """完整定义映射（经 Domain VO 投影，保证 definition_from_mapping 可还原）。"""
    definition = EvaluatorDefinition(
        definition_id="quality.judge",
        version="v1",
        metric_name="quality.judge",
        target_kind="agent_run",
        method="llm_judge",
        required_fields=("query", "answer"),
        evidence_policy="snapshot_required",
        output_type="number",
        range_or_categories=(0.0, 1.0),
        direction="higher_is_better",
        material_hash={"rubric": "b" * 64},
        judge_model="gpt-judge",
        judge_params={"rubric": "按相关性 0-1 打分"},
        timeout_seconds=60,
        budget=None,
        ci_admissible=True,
    )
    payload = definition_to_mapping(definition)
    payload.update(overrides)
    return payload


async def seed_agent_run_result(
    store,
    *,
    subject_id="run-1",
    subject_revision="stop",
    event="evt-1",
    snapshot_ref="snapshots/abc.json",
    typed_value=0.2,
    definition=None,
) -> str:
    """播种一条 agent_run 自动结果（sample + job + result），返回 result_id。"""
    await store.save_definition(definition or make_definition())
    sample_id = await store.save_sample({
        "service": "n-agent",
        "producer_event_id": event,
        "target": {
            "service": "n-agent",
            "kind": "agent_run",
            "subject_id": subject_id,
            "subject_revision": subject_revision,
            "run_id": subject_id,
            "session_id": "s-1",
        },
        "snapshot_ref": snapshot_ref,
        "snapshot_hash": "c" * 64,
    })
    job_id = await store.save_job({
        "sample_id": sample_id,
        "definition_id": (definition or make_definition())["definition_id"],
        "definition_version": (definition or make_definition())["version"],
    })
    return await store.save_result({
        "job_id": job_id,
        "provenance": "automatic",
        "metric_name": (definition or make_definition())["metric_name"],
        "status": "value",
        "typed_value": typed_value,
    })


# ---------------------------------------------------------------------------
# list_queue（Task 9 审查修复：队列只读投影经 Application 用例直通）
# ---------------------------------------------------------------------------


class TestListQueue:
    async def test_list_queue_returns_pending_rows(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(store, evaluation_service)
        rows, next_cursor, snapshot_at = await service.list_queue(
            state="all", reason="low_score"
        )
        assert [row["source_result_id"] for row in rows] == [result_id]
        assert next_cursor is None
        # snapshot_at 口径为"响应生成时刻"（用例层生成，ISO-8601 带时区）
        assert snapshot_at
        assert snapshot_at.endswith("+00:00")

    async def test_list_queue_pagination_walks_all(self, store, evaluation_service):
        first = await seed_agent_run_result(
            store, subject_id="run-1", event="evt-q1"
        )
        second = await seed_agent_run_result(
            store, subject_id="run-2", event="evt-q2"
        )
        service = make_annotation_service(store, evaluation_service)
        page1_rows, cursor, _ = await service.list_queue(
            state="all", reason="low_score", limit=1
        )
        # 队列按创建时间倒序（最新在前）
        assert [row["source_result_id"] for row in page1_rows] == [second]
        assert cursor
        page2_rows, next_cursor, _ = await service.list_queue(
            state="all", reason="low_score", limit=1, cursor=cursor
        )
        assert [row["source_result_id"] for row in page2_rows] == [first]
        assert next_cursor is None

    async def test_list_queue_invalid_state_raises_validation_error(
        self, store, evaluation_service,
    ):
        service = make_annotation_service(store, evaluation_service)
        with pytest.raises(EvaluationValidationError):
            await service.list_queue(state="wat", reason="low_score")


# ---------------------------------------------------------------------------
# get_annotation_context（S1 上下文）
# ---------------------------------------------------------------------------


class TestGetAnnotationContext:
    async def test_missing_source_result_raises_not_found(self, store, evaluation_service):
        service = make_annotation_service(store, evaluation_service)
        with pytest.raises(EvaluationNotFoundError):
            await service.get_annotation_context("no-such-result")

    async def test_tombstoned_target_raises_target_deleted(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            tombstone_reader=FakeTombstoneReader(
                blocked={("agent_run", "run-1", "stop")}
            ),
        )
        with pytest.raises(EvaluationTargetDeletedError):
            await service.get_annotation_context(result_id)

    async def test_non_agent_run_target_returns_context_without_submit(self, store, evaluation_service):
        await store.save_definition(make_definition())
        sample_id = await store.save_sample({
            "service": "n-kb",
            "producer_event_id": "evt-ret",
            "target": {
                "service": "n-kb",
                "kind": "retrieval",
                "subject_id": "ret-1",
                "subject_revision": "rev-1",
            },
            "snapshot_ref": "snapshots/ret.json",
            "snapshot_hash": "d" * 64,
        })
        job_id = await store.save_job({
            "sample_id": sample_id,
            "definition_id": "quality.judge",
            "definition_version": "v1",
        })
        result_id = await store.save_result({
            "job_id": job_id,
            "provenance": "automatic",
            "metric_name": "quality.judge",
            "status": "value",
            "typed_value": 0.5,
        })
        service = make_annotation_service(store, evaluation_service)

        context = await service.get_annotation_context(result_id)

        assert context["can_submit"] is False
        assert context["reject_reason"] == "target_kind_not_supported"
        assert context["target"] == {
            "service": "n-kb",
            "kind": "retrieval",
            "subject_id": "ret-1",
            "subject_revision": "rev-1",
        }

    async def test_human_source_result_not_submittable(self, store, evaluation_service):
        await store.save_definition(make_definition())
        feedback_id = await store.save_feedback({
            "actor_ref": ANNOTATION_DASHBOARD_ACTOR,
            "definition_id": "quality.judge",
            "definition_version": "v1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "stop",
            },
            "value": 0.9,
            "revision": 1,
        })
        human_result_id = await store.save_result({
            "feedback_id": feedback_id,
            "provenance": "human",
            "metric_name": "quality.judge",
            "status": "value",
            "typed_value": 0.9,
        })

        service = make_annotation_service(store, evaluation_service)
        context = await service.get_annotation_context(human_result_id)
        assert context["can_submit"] is False
        assert context["reject_reason"] == "target_kind_not_supported"

    async def test_full_context_for_agent_run_result(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        evidence_reader = FakeEvidenceReader()
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=evidence_reader,
            agent_run_lookup=FakeRunLookup(
                {"run-1": {"session_id": "s-1", "end_reason": "stop"}}
            ),
        )

        context = await service.get_annotation_context(result_id)

        assert context["source_result_id"] == result_id
        assert context["target"] == {
            "service": "n-agent",
            "kind": "agent_run",
            "subject_id": "run-1",
            "subject_revision": "stop",
        }
        definition = context["definition"]
        assert definition["definition_id"] == "quality.judge"
        assert definition["version"] == "v1"
        assert definition["metric_name"] == "quality.judge"
        assert definition["output_type"] == "number"
        assert definition["range_or_categories"] == [0.0, 1.0]
        assert definition["rubric_summary"] == "按相关性 0-1 打分"
        assert context["evidence_status"] == "available"
        assert evidence_reader.calls == 1
        assert context["latest_human_feedback"] is None
        assert context["current_revision"] == 0
        assert context["can_submit"] is True
        assert context["reject_reason"] is None

    async def test_evidence_missing_marks_unavailable_and_blocks_submit(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=FakeEvidenceReader(error=RuntimeError("snapshot gone")),
        )

        context = await service.get_annotation_context(result_id)

        assert context["evidence_status"] == "unavailable"
        assert context["can_submit"] is False
        assert context["reject_reason"] == "evidence_unavailable"

    async def test_context_includes_sanitized_evidence_body(self, store, evaluation_service):
        """Task 12 审查修复项 3（spec 4.4/6.2、verify 3.1）：证据可用时
        上下文随附受控投影 evidence（仅 request/answer 两字段，来自脱敏
        快照正文），供标注详情并排展示与导出取真实样本 query。"""
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=FakeEvidenceReader(),
        )

        context = await service.get_annotation_context(result_id)

        assert context["evidence_status"] == "available"
        assert context["evidence"] == {
            "request": "如何重置密码",
            "answer": "在设置页点击重置",
        }

    async def test_evidence_body_absent_when_unavailable(self, store, evaluation_service):
        """证据不可用（缺失/过期/读取失败）时上下文不含 evidence 正文，
        仅 evidence_status=unavailable（fail-closed，不补造正文）。"""
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=FakeEvidenceReader(error=RuntimeError("snapshot gone")),
        )

        context = await service.get_annotation_context(result_id)

        assert "evidence" not in context

    async def test_evidence_body_without_request_or_answer_marks_unavailable(self, store, evaluation_service):
        """审查修复项 3：快照正文既无 request/query 也无 answer（如
        {"foo": "bar"}）时按"正文不足"fail-closed——unavailable 且不携带
        evidence 投影（投影前充分性判定，不返回空正文投影）。"""
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=FakeEvidenceReader(content={"foo": "bar"}),
        )

        context = await service.get_annotation_context(result_id)

        assert context["evidence_status"] == "unavailable"
        assert "evidence" not in context

    async def test_legacy_snapshot_query_alias_used_as_request(self, store, evaluation_service):
        """T22 之前的旧快照正文只有 query 别名：受控投影 request 回退取
        query 同源值（同属脱敏快照正文，不补造）。"""
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=FakeEvidenceReader(
                content={"query": "旧快照query", "answer": "旧回答"}
            ),
        )

        context = await service.get_annotation_context(result_id)

        assert context["evidence_status"] == "available"
        assert context["evidence"] == {
            "request": "旧快照query",
            "answer": "旧回答",
        }

    async def test_agent_run_not_found_blocks_submit(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=FakeEvidenceReader(),
            agent_run_lookup=FakeRunLookup({}),
        )

        context = await service.get_annotation_context(result_id)

        assert context["can_submit"] is False
        assert context["reject_reason"] == "target_not_found"

    async def test_subject_revision_mismatch_blocks_submit(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=FakeEvidenceReader(),
            agent_run_lookup=FakeRunLookup(
                {"run-1": {"session_id": "s-1", "end_reason": "error"}}
            ),
        )

        context = await service.get_annotation_context(result_id)

        assert context["can_submit"] is False
        assert context["reject_reason"] == "target_revision_mismatch"

    async def test_latest_human_feedback_and_current_revision(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        evidence_reader = FakeEvidenceReader()
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=evidence_reader,
        )
        feedback_id = await store.save_feedback({
            "actor_ref": ANNOTATION_DASHBOARD_ACTOR,
            "definition_id": "quality.judge",
            "definition_version": "v1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "stop",
            },
            "value": 0.9,
            "revision": 1,
        })
        human_result_id = await store.save_result({
            "feedback_id": feedback_id,
            "provenance": "human",
            "metric_name": "quality.judge",
            "status": "value",
            "typed_value": 0.9,
        })

        context = await service.get_annotation_context(result_id)

        latest = context["latest_human_feedback"]
        assert latest is not None
        assert latest["revision"] == 1
        assert latest["value"] == 0.9
        assert latest["feedback_id"] == feedback_id
        assert latest["result_id"] == human_result_id
        assert context["current_revision"] == 1
        assert context["can_submit"] is True

    async def test_migrated_thread_keeps_existing_max_revision(self, store, evaluation_service):
        """旧线程迁移后取既有最大 revision，不重新编号。"""
        result_id = await seed_agent_run_result(store)
        evidence_reader = FakeEvidenceReader()
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=evidence_reader,
        )
        # 直播种出 revision=3 的既有线程（模拟迁移前历史，跳过逐级 CAS）
        await store.save_feedback({
            "actor_ref": ANNOTATION_DASHBOARD_ACTOR,
            "definition_id": "quality.judge",
            "definition_version": "v1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "stop",
            },
            "value": 0.1,
            "revision": 3,
        })

        context = await service.get_annotation_context(result_id)

        assert context["current_revision"] == 3
        assert context["latest_human_feedback"]["revision"] == 3

    async def test_actor_isolation_per_full_thread(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        evidence_reader = FakeEvidenceReader()
        service = make_annotation_service(
            store,
            evaluation_service,
            evidence_reader=evidence_reader,
        )
        await store.save_feedback({
            "actor_ref": "reviewer-a",
            "definition_id": "quality.judge",
            "definition_version": "v1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "stop",
            },
            "value": 0.9,
            "revision": 1,
        })

        context = await service.get_annotation_context(result_id)

        # dashboard 视角：reviewer-a 的线程不影响 dashboard current_revision
        assert context["latest_human_feedback"] is None
        assert context["current_revision"] == 0


# ---------------------------------------------------------------------------
# submit_annotation（S4 提交）
# ---------------------------------------------------------------------------


def make_submittable_service(
    store,
    evaluation_service,
    *,
    evidence_reader=None,
    agent_run_lookup=None,
    tombstone_reader=None,
):
    """可提交链路的默认装配：证据可用 + 真实 Run 存在且 revision 匹配。"""
    return AnnotationService(
        store,
        evaluation_service,
        evidence_reader=(
            evidence_reader if evidence_reader is not None else FakeEvidenceReader()
        ),
        agent_run_lookup=(
            agent_run_lookup
            if agent_run_lookup is not None
            else FakeRunLookup({"run-1": {"session_id": "s-1", "end_reason": "stop"}})
        ),
        tombstone_reader=tombstone_reader,
    )


def _thread_feedback_count(store) -> int:
    # dashboard@run-1@stop@quality.judge@v1 完整线程的行数
    with sqlite3.connect(store.path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM evaluation_feedback WHERE actor_ref = ? "
            "AND definition_id = ? AND target_subject_id = ?",
            (ANNOTATION_DASHBOARD_ACTOR, "quality.judge", "run-1"),
        ).fetchone()[0]


class TestSubmitAnnotation:
    async def test_value_out_of_range_rejected(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        with pytest.raises(EvaluationValidationError):
            await service.submit_annotation(
                result_id,
                value=1.5,
                request_key="rk-range",
                expected_revision=0,
            )

    async def test_actor_must_be_server_injected(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        with pytest.raises(EvaluationValidationError):
            await service.submit_annotation(
                result_id,
                value=0.8,
                request_key="rk-actor",
                expected_revision=0,
                actor="",
            )

    async def test_request_key_required(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        with pytest.raises(EvaluationValidationError):
            await service.submit_annotation(
                result_id, value=0.8, request_key="", expected_revision=0
            )

    async def test_submit_creates_human_result_deliveries_and_keeps_auto_result(
        self, store, evaluation_service
    ):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)

        outcome = await service.submit_annotation(
            result_id, value=0.8, request_key="rk-create", expected_revision=0
        )

        assert outcome.duplicate is False
        assert outcome.is_current is True
        assert outcome.revision == 1
        assert outcome.superseded_feedback_id is None
        feedback = await store.get_feedback(outcome.feedback_id)
        assert feedback["actor_ref"] == ANNOTATION_DASHBOARD_ACTOR
        assert feedback["revision"] == 1
        assert feedback["value"] == 0.8
        assert feedback["supersedes_id"] is None
        assert feedback["target"]["subject_id"] == "run-1"
        assert feedback["target"]["subject_revision"] == "stop"
        human_result = await store.load_result(outcome.result_id)
        assert human_result["provenance"] == "human"
        assert human_result["feedback_id"] == outcome.feedback_id
        assert human_result["typed_value"] == 0.8
        # 原自动结果不变
        auto_result = await store.load_result(result_id)
        assert auto_result["provenance"] == "automatic"
        assert auto_result["typed_value"] == 0.2
        # sink 投递经既有 _delivery_sink_specs 机制同事务登记
        deliveries = await store.list_deliveries(result_id=outcome.result_id)
        assert [d["sink"] for d in deliveries] == ["langfuse"]

    async def test_second_revision_supersedes_same_thread(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        first = await service.submit_annotation(
            result_id, value=0.8, request_key="rk-1", expected_revision=0
        )
        second = await service.submit_annotation(
            result_id, value=0.6, request_key="rk-2", expected_revision=1
        )

        assert second.revision == 2
        assert second.superseded_feedback_id == first.feedback_id
        second_result = await store.load_result(second.result_id)
        assert second_result["supersedes_result_id"] == first.result_id
        assert _thread_feedback_count(store) == 2

    async def test_replay_same_key_same_payload_returns_original_success(
        self, store, evaluation_service
    ):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        first = await service.submit_annotation(
            result_id, value=0.8, request_key="rk-replay", expected_revision=0
        )
        replay = await service.submit_annotation(
            result_id, value=0.8, request_key="rk-replay", expected_revision=1
        )

        assert replay.duplicate is True
        assert replay.feedback_id == first.feedback_id
        assert replay.result_id == first.result_id
        assert replay.revision == 1
        assert replay.is_current is True
        # 重放不新建 revision
        assert _thread_feedback_count(store) == 1

    async def test_replay_of_superseded_submission_returns_original_without_revival(
        self, store, evaluation_service
    ):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        first = await service.submit_annotation(
            result_id, value=0.8, request_key="rk-old", expected_revision=0
        )
        await service.submit_annotation(
            result_id, value=0.6, request_key="rk-new", expected_revision=1
        )
        replay = await service.submit_annotation(
            result_id, value=0.8, request_key="rk-old", expected_revision=2
        )

        assert replay.duplicate is True
        assert replay.feedback_id == first.feedback_id
        assert replay.revision == 1
        # 已被取代：返回原成功但 is_current=False，不创建新 revision、
        # 不复活旧值
        assert replay.is_current is False
        assert _thread_feedback_count(store) == 2
        context = await service.get_annotation_context(result_id)
        assert context["current_revision"] == 2

    async def test_same_key_different_payload_conflicts(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        await service.submit_annotation(
            result_id, value=0.8, request_key="rk-bind", expected_revision=0
        )
        with pytest.raises(EvaluationConflictError):
            await service.submit_annotation(
                result_id, value=0.5, request_key="rk-bind", expected_revision=1
            )

    async def test_expected_revision_mismatch_rejected(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        await service.submit_annotation(
            result_id, value=0.8, request_key="rk-cas", expected_revision=0
        )
        with pytest.raises(EvaluationRevisionConflictError):
            await service.submit_annotation(
                result_id, value=0.6, request_key="rk-cas-2", expected_revision=5
            )

    async def test_evidence_reverified_on_new_submission(self, store, evaluation_service):
        """未命中新提交重新读取证据，不依赖先前 GET can_submit。"""
        result_id = await seed_agent_run_result(store)
        evidence_reader = FakeEvidenceReader()
        service = make_submittable_service(
            store, evaluation_service, evidence_reader=evidence_reader
        )
        assert evidence_reader.calls == 0
        with pytest.raises(EvidenceUnavailableError):
            # 先 GET 确认可提交，随后证据到期 -> 提交仍被拒
            context = await service.get_annotation_context(result_id)
            assert context["can_submit"] is True
            evidence_reader._error = RuntimeError("snapshot expired")
            await service.submit_annotation(
                result_id, value=0.8, request_key="rk-evidence", expected_revision=0
            )
        assert evidence_reader.calls >= 2

    async def test_run_not_found_rejected(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(
            store, evaluation_service, agent_run_lookup=FakeRunLookup({})
        )
        with pytest.raises(EvaluationTargetMissingError):
            await service.submit_annotation(
                result_id, value=0.8, request_key="rk-run", expected_revision=0
            )

    async def test_subject_revision_mismatch_with_real_run_rejected(
        self, store, evaluation_service
    ):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(
            store,
            evaluation_service,
            agent_run_lookup=FakeRunLookup(
                {"run-1": {"session_id": "s-1", "end_reason": "error"}}
            ),
        )
        with pytest.raises(EvaluationRevisionConflictError):
            await service.submit_annotation(
                result_id, value=0.8, request_key="rk-rev", expected_revision=0
            )

    async def test_tombstoned_target_rejected(self, store, evaluation_service):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(
            store,
            evaluation_service,
            tombstone_reader=FakeTombstoneReader(
                blocked={("agent_run", "run-1", "stop")}
            ),
        )
        with pytest.raises(EvaluationTargetDeletedError):
            await service.submit_annotation(
                result_id, value=0.8, request_key="rk-tomb", expected_revision=0
            )

    async def test_store_tombstone_blocks_late_annotation(self, store, evaluation_service):
        """未注入 tombstone_reader 时，store 写侧门禁兜底拒绝迟到人评。"""
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)
        await store.record_deletion_tombstone({
            "subject_kind": "agent_run",
            "subject_id": "run-1",
            "subject_revision": "stop",
            "reason": "session_deleted",
        })
        with pytest.raises(EvaluationTargetDeletedError):
            await service.submit_annotation(
                result_id, value=0.8, request_key="rk-late", expected_revision=0
            )

    async def test_concurrent_same_key_same_payload_returns_same_success(
        self, store, evaluation_service
    ):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)

        # 屏障：强制"双方都完成预读后才放行写入"的目标交织。否则存在
        # 可达罕见交织——落败方在 request_key 重放未命中后，预读 fast-fail
        # 抛 EvaluationRevisionConflictError，不经 except 链重放复核，
        # errors == [] 断言低概率失败（store 每次 store 调用经
        # asyncio.to_thread 真实挂起，交织完全由事件循环调度决定）。
        barrier = _PreReadBarrier(store, parties=2, timeout=5.0)
        service._store = barrier

        outcomes = await asyncio.gather(
            service.submit_annotation(
                result_id, value=0.8, request_key="rk-dup", expected_revision=0
            ),
            service.submit_annotation(
                result_id, value=0.8, request_key="rk-dup", expected_revision=0
            ),
            return_exceptions=True,
        )
        errors = [o for o in outcomes if isinstance(o, Exception)]
        succeeded = [o for o in outcomes if not isinstance(o, Exception)]
        assert errors == []
        # 同 key 同 payload 并发返回同一成功
        assert len(succeeded) == 2
        assert succeeded[0].feedback_id == succeeded[1].feedback_id
        assert succeeded[0].result_id == succeeded[1].result_id
        assert _thread_feedback_count(store) == 1

    async def test_concurrent_different_keys_same_revision_single_winner(
        self, store, evaluation_service
    ):
        result_id = await seed_agent_run_result(store)
        service = make_submittable_service(store, evaluation_service)

        outcomes = await asyncio.gather(
            service.submit_annotation(
                result_id, value=0.8, request_key="rk-w1", expected_revision=0
            ),
            service.submit_annotation(
                result_id, value=0.6, request_key="rk-w2", expected_revision=0
            ),
            return_exceptions=True,
        )
        errors = [o for o in outcomes if isinstance(o, Exception)]
        succeeded = [o for o in outcomes if not isinstance(o, Exception)]
        # 不同 key 同旧 revision 仅一成功，落败方 409 要求显式重载
        assert len(succeeded) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], EvaluationRevisionConflictError)
        assert _thread_feedback_count(store) == 1


# ---------------------------------------------------------------------------
# 降级索引库（v8 迁移降级分支遗留状态）上的事务内 CAS
# ---------------------------------------------------------------------------


async def _make_degraded_index_store(tmp_path):
    """构造降级非唯一索引库：先建正常库 -> 拆线程索引 -> 插同线程同
    revision 历史冲突行 -> 降回 v7 -> 重开触发 v8 迁移降级。返回
    (store, result_id)，result_id 为可标注的 agent_run 自动结果。
    索引降级 helpers 与 store 测试共用（_degraded_helpers）；播种数据
    差异属测试意图，保留在本文件。"""
    db_path = Path(str(tmp_path)) / "evaluations-degraded.db"
    store = SQLiteEvaluationStore(str(db_path))
    result_id = await seed_agent_run_result(store)
    drop_feedback_thread_indexes(db_path)
    for feedback_id in ("fb-dup-a", "fb-dup-b"):
        await store.save_feedback({
            "feedback_id": feedback_id,
            "actor_ref": ANNOTATION_DASHBOARD_ACTOR,
            "definition_id": "quality.judge",
            "definition_version": "v1",
            "target": {
                "service": "n-agent",
                "kind": "agent_run",
                "subject_id": "run-1",
                "subject_revision": "stop",
            },
            "value": 0.1,
            "revision": 1,
        })
    force_schema_v7(db_path)
    store = SQLiteEvaluationStore(str(db_path))
    return store, result_id


def _thread_revision_row_counts(store) -> dict:
    """dashboard@run-1@stop@quality.judge@v1 线程内各 revision 的行数。"""
    with sqlite3.connect(str(store.path)) as conn:
        rows = conn.execute(
            "SELECT revision, COUNT(*) FROM evaluation_feedback "
            "WHERE actor_ref = ? AND definition_id = ? "
            "AND target_subject_id = ? GROUP BY revision",
            (ANNOTATION_DASHBOARD_ACTOR, "quality.judge", "run-1"),
        ).fetchall()
    return {int(revision): count for revision, count in rows}


class TestSubmitAnnotationDegradedIndex:
    async def test_concurrent_same_revision_on_degraded_index_single_winner(
        self, tmp_path
    ):
        """降级非唯一索引库上并发同 revision 标注提交：权威 CAS 必须在
        BEGIN IMMEDIATE 写事务内复查（expected == MAX(revision)，唯一索引
        与降级索引统一生效），仅一成功、落败方 409，且不产生同线程同
        revision 重复行（spec 4.4 / plan Task 6 S4 硬性要求：降级库禁止
        重复 revision）。"""
        store, result_id = await _make_degraded_index_store(tmp_path)
        evaluation_service = EvaluationService(
            store,
            enabled=True,
            delivery_sink_specs=(
                {"sink": "langfuse", "projection_version": "v1"},
            ),
        )
        service = make_submittable_service(store, evaluation_service)

        outcomes = await asyncio.gather(
            service.submit_annotation(
                result_id, value=0.8, request_key="rk-dg1", expected_revision=1
            ),
            service.submit_annotation(
                result_id, value=0.6, request_key="rk-dg2", expected_revision=1
            ),
            return_exceptions=True,
        )
        errors = [o for o in outcomes if isinstance(o, Exception)]
        succeeded = [o for o in outcomes if not isinstance(o, Exception)]
        assert len(succeeded) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], EvaluationRevisionConflictError)
        # 降级库禁止重复 revision：本次提交产生的 revision（>=2）至多
        # 一行（revision=1 的两行是构造降级库的历史冲突 fixture，修复不
        # 追溯清理既有重复行）
        counts = _thread_revision_row_counts(store)
        new_counts = {r: c for r, c in counts.items() if r >= 2}
        assert all(count == 1 for count in new_counts.values()), new_counts
        assert counts.get(2) == 1
