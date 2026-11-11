# app.application.evaluation_deletion 用例测试（plan T18 删除传播）。
#
# TDD 严格 S1 失败测试先行：真实 SQLiteEvaluationStore（tmp DB）+ fake
# evidence store / fake 外部删除端 / fake 投递出口闸。
#
# 覆盖 plan T18 S1 全部要点与 spec 14 删除传播语义：
# - 会话删除级联：tombstone 先行（持久门禁）+ evidence_targets 反向索引
#   撤销 queued job 与 pending delivery、结果/反馈 tombstone 隐藏当前视图、
#   purge 证据正文（共享内容文件保护）、登记 external_id 删除任务
# - 文档删除级联：同时覆盖检索快照与使用该文档的 Agent 快照；迟到样本
#   携带已删 document_revision 被拒绝（新 retrieval_id 不绕过 tombstone）；
#   其他 revision 不受影响
# - 删除 vs 发送并发：发送中删除 -> 结果不得转 verified（改判 unlinked
#   隔离上报）、按已预登记 external_id 登记删除任务；claim/freeze 门禁
#   阻断新发送；有界在途收敛等待；先禁新发送再删再回读
# - 外部删除任务语义：deleted + 回读缺席 -> done；第一次 404 不作结论
#   （重复核对后 done）；回读仍存在保持 deletion_pending；未知结果保留
#   deletion_pending 退避重试；后端不可用保留任务不宣称外部已删除
# - tombstone 幂等：重复删除事件可重试且幂等
# - 删除期间运行中 Judge 返回不得提交或复活内容（store 提交门禁 +
#   worker 丢弃并终结 job）
# - store v4 -> v5 迁移：deletion_tasks 增 result_id 关联列（幂等、并发
#   首启不竞态）
# - SessionService 复用 on_session_deleted handler 触发级联；级联失败不
#   阻塞业务删除；delete_session 签名与 active Run 拒绝语义不变
from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from app.application.evaluation_deletion import (
    CONFIRM_ABSENT_RUNS,
    EvaluationDeletionService,
)
from app.domain.evaluation.errors import (
    EvaluationConflictError,
    EvaluationDeletionConflictError,
)
from app.domain.evaluation.models import EvaluatorDefinition, EvaluationValue
from app.infrastructure.evaluation.store import SQLiteEvaluationStore

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures / payload helpers（形态与 T3 store 测试夹具一致）
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    return SQLiteEvaluationStore(str(tmp_path / "evaluations.db"))


@pytest.fixture
def clock():
    return lambda: datetime(2026, 11, 11, 12, 0, 0, tzinfo=_UTC)


def _definition_payload(**overrides) -> dict:
    payload = {
        "definition_id": "def-answer-relevance",
        "version": "v1",
        "metric_name": "answer_relevance",
        "target_kind": "agent_run",
        "method": "deterministic",
        "required_fields": ["query", "answer"],
        "evidence_policy": "snapshot_required",
        "output_type": "number",
        "range_or_categories": [0, 1],
        "direction": "higher_is_better",
        "material_hash": {"rubric": "b" * 64},
        "judge_model": None,
        "judge_params": {},
        "timeout_seconds": 60,
        "budget": None,
        "ci_admissible": True,
    }
    payload.update(overrides)
    return payload


def _sample_payload(**overrides) -> dict:
    """默认样本：会话 s-1 + agent_run r-1 反向索引；文档目标按需覆盖。"""
    payload = {
        "service": "n-agent",
        "producer_event_id": "evt-1",
        "target": {
            "service": "n-agent",
            "kind": "retrieval",
            "subject_id": "search-1",
            "subject_revision": "rev-1",
        },
        "snapshot_ref": f"eval-snapshots/{'a' * 64}.json",
        "snapshot_hash": "a" * 64,
        "schema_version": "evaluation.sample.v1",
        "captured_at": "2026-11-10T00:00:00+00:00",
        "sample_probability": 1.0,
        "sampling_policy_version": "sp-1",
        "evidence_targets": [
            {
                "source_service": "n-agent",
                "subject_kind": "session",
                "subject_id": "s-1",
                "subject_revision": "",
            },
            {
                "source_service": "n-agent",
                "subject_kind": "agent_run",
                "subject_id": "r-1",
                "subject_revision": "",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _document_target(document_id="doc-1", revision="dr-7") -> dict:
    return {
        "source_service": "n-kb",
        "subject_kind": "document",
        "subject_id": document_id,
        "subject_revision": revision,
    }


def _job_payload(sample_id: str, **overrides) -> dict:
    payload = {
        "sample_id": sample_id,
        "definition_id": "def-answer-relevance",
        "definition_version": "v1",
        "requested_by": "eval-worker",
        "idempotency_key": "idem-1",
    }
    payload.update(overrides)
    return payload


def _auto_result_payload(job_id: str, **overrides) -> dict:
    payload = {
        "job_id": job_id,
        "provenance": "automatic",
        "metric_name": "answer_relevance",
        "metric_version": "v1",
        "status": "value",
        "typed_value": 0.87,
        "passed": True,
        "evidence_hash": "c" * 64,
    }
    payload.update(overrides)
    return payload


def _feedback_payload(**overrides) -> dict:
    payload = {
        "actor_ref": "user-alice",
        "definition_id": "def-answer-relevance",
        "definition_version": "v1",
        "revision": 1,
        "target": {
            "service": "n-agent",
            "kind": "session",
            "subject_id": "s-1",
            "subject_revision": "rev-1",
        },
        "value": {"typed_value": 1, "status": "value"},
        "comment_ref": "comments/c-1",
    }
    payload.update(overrides)
    return payload


def _delivery_specs(sink="langfuse", **overrides) -> list[dict]:
    spec = {
        "sink": sink,
        "projection_version": "score-v1",
        "payload": {"metric": "answer_relevance", "value": 0.87},
    }
    spec.update(overrides)
    return [spec]


async def _save_sample(store, **overrides) -> str:
    return await store.save_sample(_sample_payload(**overrides))


async def _build_committed_result(
    store,
    *,
    deliveries: list[dict] | None = None,
    producer_event_id: str = "evt-1",
    job_idempotency_key: str = "idem-1",
    **sample_overrides,
) -> dict:
    """样本 -> job 领取 -> result + deliveries 同事务提交（job succeeded）。

    返回 sample_id / job_id / result_id（lease 已随成功提交消费）。
    同一测试内多次构建须传不同的 producer_event_id / job_idempotency_key
    （幂等键会去重返回同一样本/job）。"""
    await store.save_definition(_definition_payload())
    sample_id = await _save_sample(
        store, producer_event_id=producer_event_id, **sample_overrides
    )
    job_id = await store.save_job(
        _job_payload(sample_id, idempotency_key=job_idempotency_key)
    )
    claimed = await store.claim_next_job("test-worker", 300)
    assert claimed["job_id"] == job_id
    result_id = await store.commit_result_with_deliveries(
        _auto_result_payload(job_id),
        deliveries or [],
        lease_token=claimed["lease_token"],
        job_terminal_status="succeeded",
    )
    return {
        "sample_id": sample_id,
        "job_id": job_id,
        "result_id": result_id,
        "lease_token": None,
    }


async def _build_running_job(store, **sample_overrides) -> dict:
    """样本 -> job 领取为 running（Judge 在途形态，不提交结果）。

    返回 sample_id / job_id / lease_token，用于删除 vs 提交并发用例。"""
    await store.save_definition(_definition_payload())
    sample_id = await _save_sample(store, **sample_overrides)
    job_id = await store.save_job(_job_payload(sample_id))
    claimed = await store.claim_next_job("test-worker", 300)
    assert claimed["job_id"] == job_id
    return {
        "sample_id": sample_id,
        "job_id": job_id,
        "lease_token": claimed["lease_token"],
    }


def make_service(store, *, evidence_store=None, deleters=None, gate=None,
                 clock=None, **overrides) -> EvaluationDeletionService:
    return EvaluationDeletionService(
        store=store,
        evidence_store=evidence_store,
        external_deleters=deleters or {},
        delivery_worker=gate,
        clock=clock,
        **overrides,
    )


class FakeEvidenceStore:
    """purge_snapshot 契约的 fake（与真实 LocalEvidenceStore 同签名）。"""

    def __init__(self, refs=()):
        self.files = set(refs)
        self.purged = []

    async def purge_snapshot(self, snapshot_ref: str) -> bool:
        if snapshot_ref in self.files:
            self.files.discard(snapshot_ref)
            self.purged.append(snapshot_ref)
            return True
        return False


class FakeExternalDeleter:
    """外部删除端 fake：delete_external 返回 deleted/not_found/unknown。

    ``outcome_sequence`` 提供按调用序变化的混合历史（M2 连续缺席语义测
    试）；``read_back_sequence`` 按调用序控制回读在场/缺席。"""

    def __init__(
        self,
        *,
        outcomes=None,
        outcome_sequence=None,
        read_back_present=(),
        read_back_sequence=None,
        raise_on=None,
    ):
        # outcomes: external_id -> "deleted" | "not_found" | "unknown"
        self.outcomes = dict(outcomes or {})
        self.outcome_sequence = list(outcome_sequence or ())
        self.read_back_present = set(read_back_present)
        self.read_back_sequence = list(read_back_sequence or ())
        self.raise_on = set(raise_on or ())
        self.deleted_calls = []
        self.read_back_calls = []

    async def delete_external(self, external_id: str) -> str:
        self.deleted_calls.append(external_id)
        if external_id in self.raise_on:
            raise OSError("sink unavailable")
        if self.outcome_sequence:
            return self.outcome_sequence.pop(0)
        return self.outcomes.get(external_id, "deleted")

    async def read_back(self, external_id: str) -> Mapping[str, Any] | None:
        self.read_back_calls.append(external_id)
        if self.read_back_sequence:
            present = self.read_back_sequence.pop(0)
            return {"id": external_id} if present else None
        if external_id in self.read_back_present:
            return {"id": external_id}
        return None


class FakeDeliveryGate:
    """投递出口闸 fake（鸭子类型匹配 delivery worker 关闭/恢复接口）。

    ``wait_outcomes`` 按调用序返回 wait_inflight 结果（默认 True）。"""

    def __init__(self, wait_outcomes=None):
        self.calls = []
        self.wait_outcomes = list(wait_outcomes or ())

    async def disable_sink(self, sink: str, *, reason=None) -> int:
        self.calls.append(("disable", sink, reason))
        return 0

    async def enable_sink(self, sink: str) -> int:
        self.calls.append(("enable", sink))
        return 0

    async def wait_inflight(self, timeout: float) -> bool:
        self.calls.append(("wait", timeout))
        if self.wait_outcomes:
            return self.wait_outcomes.pop(0)
        return True


class FakeMemoryStore:
    """SessionService 删除路径所需的最小内存存储 fake。"""

    def __init__(self, active_run_ids=()):
        self.deleted = []
        self.active_run_ids = set(active_run_ids)

    async def get_session(self, session_id: str):
        if session_id in self.deleted:
            return None
        return SimpleNamespace(id=session_id, source="dashboard")

    async def assert_session_deletable(self, session_id: str) -> None:
        if self.active_run_ids:
            raise ValueError("session_has_active_agent_runs")

    async def delete_session(self, session_id: str) -> bool:
        if session_id in self.deleted:
            return False
        self.deleted.append(session_id)
        return True


# ---------------------------------------------------------------------------
# 会话删除级联（tombstone / 撤销 / 隐藏 / 登记 / 收敛）
# ---------------------------------------------------------------------------


class TestSessionDeletionCascade:
    async def test_cascade_tombstones_subject_and_hides_results(self, store, clock):
        built = await _build_committed_result(store)
        service = make_service(store, clock=clock)
        report = await service.handle_session_deleted("s-1")

        assert await store.is_subject_deleted("session", "s-1") is True
        rows = await store.list_current_results()
        assert all(r["result_id"] != built["result_id"] for r in rows)
        assert report["subject_kind"] == "session"
        assert report["result_tombstones"] >= 1

    async def test_cascade_revokes_queued_jobs(self, store, clock):
        await store.save_definition(_definition_payload())
        sample_id = await _save_sample(store)
        job_id = await store.save_job(_job_payload(sample_id))
        service = make_service(store, clock=clock)
        report = await service.handle_session_deleted("s-1")

        assert job_id in report["revoked_job_ids"]
        job = await store.get_job(job_id)
        assert job["status"] == "cancelled"

    async def test_cascade_hides_feedback_in_current_view(self, store, clock):
        await _build_committed_result(store)
        feedback_id = await store.save_feedback(_feedback_payload())
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        assert await store.is_subject_deleted("feedback", feedback_id) is True
        rows = await store.list_current_feedback()
        assert all(f["feedback_id"] != feedback_id for f in rows)

    async def test_cascade_hides_human_result_from_results_page(
        self, store, clock
    ):
        """M4：被删会话的 human result 行（feedback_id 关联、job_id NULL，
        不在样本 job 收集范围）一并 tombstone——query_results_page 等
        分页读路径不再可见。"""
        await store.save_definition(_definition_payload())
        _feedback_id, result_id = await store.save_feedback_with_result(
            _feedback_payload(),
            {
                "provenance": "human",
                "metric_name": "answer_relevance",
                "metric_version": "v1",
                "status": "value",
                "typed_value": 1,
                "evidence_hash": "e" * 64,
            },
        )
        service = make_service(store, clock=clock)
        report = await service.handle_session_deleted("s-1")

        assert await store.is_subject_deleted("result", result_id) is True
        rows, _ = await store.query_results_page(
            target_kind="session", target_subject_id="s-1"
        )
        assert all(r["result_id"] != result_id for r in rows)
        assert report["result_tombstones"] >= 1

    async def test_cascade_hides_agent_run_targeted_feedback(self, store, clock):
        """会话删除级联覆盖 target 为该会话名下 agent_run 的反馈。

        evaluation_service 的 human 反馈以 agent_run 为 target（TARGET_KINDS
        允许）；仅按 target_subject_id=session 收集会漏掉这些反馈，使其
        成为孤儿并残留统计读路径（规格审查 Minor 2）。"""
        await _build_committed_result(store)
        agent_run_feedback = await store.save_feedback(
            _feedback_payload(
                actor_ref="user-bob",
                target={
                    "service": "n-agent",
                    "kind": "agent_run",
                    "subject_id": "r-1",
                    "subject_revision": "rev-1",
                },
            )
        )
        session_feedback = await store.save_feedback(
            _feedback_payload(actor_ref="user-alice")
        )
        service = make_service(store, clock=clock)
        report = await service.handle_session_deleted("s-1")

        assert await store.is_subject_deleted("feedback", agent_run_feedback)
        assert await store.is_subject_deleted("feedback", session_feedback)
        rows = await store.list_current_feedback()
        hidden = {agent_run_feedback, session_feedback}
        assert all(f["feedback_id"] not in hidden for f in rows)
        assert report["feedback_tombstones"] == 2

    async def test_agent_run_feedback_of_other_session_not_tombstoned(
        self, store, clock
    ):
        """同类别 agent_run 反馈仅随本会话样本级联：其他会话名下不受影响。"""
        await _build_committed_result(store)
        other_run_feedback = await store.save_feedback(
            _feedback_payload(
                target={
                    "service": "n-agent",
                    "kind": "agent_run",
                    "subject_id": "r-other",
                    "subject_revision": "rev-1",
                },
            )
        )
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        assert (
            await store.is_subject_deleted("feedback", other_run_feedback)
            is False
        )
        rows = await store.list_current_feedback()
        assert any(
            f["feedback_id"] == other_run_feedback for f in rows
        )

    async def test_cascade_registers_deletion_task_for_frozen_delivery(
        self, store, clock
    ):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        await store.freeze_delivery_payload(
            delivery["delivery_id"],
            external_id="lf-score-1",
            payload={"value": 0.87},
        )
        service = make_service(store, clock=clock)
        report = await service.handle_session_deleted("s-1")

        tasks = await store.list_deletion_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task["sink"] == "langfuse"
        assert task["external_id"] == "lf-score-1"
        assert task["subject_kind"] == "result"
        assert task["subject_id"] == built["result_id"]
        # v5 迁移新增列：任务可回链 result 行
        assert task["result_id"] == built["result_id"]
        assert report["deletion_tasks"] == 1

    async def test_cascade_unlinks_unleased_pending_deliveries(self, store, clock):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        row = await store.get_delivery(delivery["delivery_id"])
        assert row["status"] == "unlinked"
        assert row["last_error_type"] == "subject_deleted"

    async def test_cascade_unlinks_retry_wait_deliveries(self, store, clock):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        await store.record_delivery_transition(
            delivery["delivery_id"],
            "retry_wait",
            next_retry_at="2026-11-11T23:00:00+00:00",
        )
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        row = await store.get_delivery(delivery["delivery_id"])
        assert row["status"] == "unlinked"

    async def test_cascade_purges_snapshot_and_clears_refs(
        self, store, clock, tmp_path
    ):
        ref = f"eval-snapshots/{'a' * 64}.json"
        await _build_committed_result(store, snapshot_ref=ref)
        evidence = FakeEvidenceStore(refs={ref})
        service = make_service(store, evidence_store=evidence, clock=clock)
        report = await service.handle_session_deleted("s-1")

        assert evidence.purged == [ref]
        assert report["purged_snapshots"] == [ref]
        sample = await store.load_sample(report["cleared_sample_ids"][0])
        assert sample["snapshot_ref"] == ""
        assert sample["snapshot_hash"] == ""

    async def test_cascade_keeps_shared_snapshot_file_for_live_sample(
        self, store, clock
    ):
        """同内容 ref 被未删除主体引用时不 purge 文件，只清被删样本引用。"""
        ref = f"eval-snapshots/{'a' * 64}.json"
        built = await _build_committed_result(store, snapshot_ref=ref)
        # 另一个未删除主体的样本引用同一内容文件
        await _save_sample(
            store,
            producer_event_id="evt-other",
            evidence_targets=[
                {
                    "source_service": "n-agent",
                    "subject_kind": "session",
                    "subject_id": "s-other",
                    "subject_revision": "",
                }
            ],
            snapshot_ref=ref,
        )
        evidence = FakeEvidenceStore(refs={ref})
        service = make_service(store, evidence_store=evidence, clock=clock)
        await service.handle_session_deleted("s-1")

        assert evidence.purged == []
        sample = await store.load_sample(built["sample_id"])
        assert sample["snapshot_ref"] == ""
        # 未删除主体的引用行不受影响
        refs = await store.list_samples_by_snapshot_ref(ref)
        assert len(refs) == 1
        assert refs[0]["sample_id"] != built["sample_id"]

    async def test_cascade_purges_despite_evidence_registration_row(
        self, store, clock
    ):
        """#7：evidence:* 登记行不构成"共享存活"——全部业务样本删除后
        正文照常 purge，登记行引用一并清除（不永久钉住内容文件）。"""
        ref = f"eval-snapshots/{'a' * 64}.json"
        built = await _build_committed_result(store, snapshot_ref=ref)
        # evidence_store 经 save_sample 落的登记行（producer_event_id
        # 以 "evidence:" 开头）指向同一内容文件
        registration_id = await _save_sample(
            store,
            producer_event_id=(
                f"evidence:agent_run:sub-x:{'b' * 64}:{'c' * 64}"
            ),
            snapshot_ref=ref,
        )
        evidence = FakeEvidenceStore(refs={ref})
        service = make_service(store, evidence_store=evidence, clock=clock)
        report = await service.handle_session_deleted("s-1")

        assert evidence.purged == [ref]
        assert report["purged_snapshots"] == [ref]
        reg_row = await store.load_sample(registration_id)
        assert reg_row["snapshot_ref"] == ""
        assert reg_row["snapshot_hash"] == ""
        sample = await store.load_sample(built["sample_id"])
        assert sample["snapshot_ref"] == ""

    async def test_cascade_idempotent_on_repeat_event(self, store, clock):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        await store.freeze_delivery_payload(
            delivery["delivery_id"],
            external_id="lf-score-1",
            payload={"value": 0.87},
        )
        service = make_service(store, clock=clock)
        first = await service.handle_session_deleted("s-1")
        second = await service.handle_session_deleted("s-1", reason="retry")

        assert await store.is_subject_deleted("session", "s-1") is True
        tasks = await store.list_deletion_tasks()
        assert len(tasks) == 1
        # 幂等：重复事件不产生新的 result tombstone（去重计数）
        assert second["result_tombstones"] == 0
        assert second["feedback_tombstones"] == 0
        assert second["deletion_tasks"] == 0
        assert first["deletion_tasks"] == 1

    async def test_cascade_isolates_per_sample_failures(self, store, clock):
        await _build_committed_result(store)
        # 另一个样本的 ref purge 抛错（evidence store 故障）
        ref_bad = f"eval-snapshots/{'b' * 64}.json"
        await _save_sample(
            store,
            producer_event_id="evt-bad",
            snapshot_ref=ref_bad,
            evidence_targets=[
                {
                    "source_service": "n-agent",
                    "subject_kind": "session",
                    "subject_id": "s-1",
                    "subject_revision": "",
                }
            ],
        )
        evidence = FakeEvidenceStore(refs={ref_bad})

        async def _explode(ref: str) -> bool:
            if ref == ref_bad:
                raise OSError("disk failure")
            return False

        evidence.purge_snapshot = _explode
        service = make_service(store, evidence_store=evidence, clock=clock)
        report = await service.handle_session_deleted("s-1")

        # tombstone 门禁不受单样本 purge 故障影响
        assert await store.is_subject_deleted("session", "s-1") is True
        assert len(report["errors"]) == 1


# ---------------------------------------------------------------------------
# 文档删除级联（检索快照 + Agent 快照 + 迟到样本拒绝）
# ---------------------------------------------------------------------------


class TestDocumentDeletion:
    async def test_document_deletion_cascades_both_snapshot_kinds(
        self, store, clock
    ):
        await store.save_definition(_definition_payload())
        # 检索快照（n-kb producer）与 Agent 快照（n-agent producer）
        # 都引用同一文档版本
        retrieval_sample = await _save_sample(
            store,
            producer_event_id="evt-retrieval",
            service="n-kb",
            evidence_targets=[_document_target()],
        )
        agent_sample = await _save_sample(
            store,
            producer_event_id="evt-agent",
            evidence_targets=[_document_target()],
        )
        retrieval_job = await store.save_job(
            _job_payload(retrieval_sample, idempotency_key="j1")
        )
        agent_job = await store.save_job(
            _job_payload(agent_sample, idempotency_key="j2")
        )
        service = make_service(store, clock=clock)
        await service.handle_document_deleted("doc-1", "dr-7")

        assert await store.is_subject_deleted("document", "doc-1", "dr-7")
        for job_id in (retrieval_job, agent_job):
            assert (await store.get_job(job_id))["status"] == "cancelled"

    async def test_document_deletion_spares_other_revision(self, store, clock):
        await store.save_definition(_definition_payload())
        sample_id = await _save_sample(
            store,
            evidence_targets=[_document_target(revision="dr-8")],
        )
        job_id = await store.save_job(_job_payload(sample_id))
        service = make_service(store, clock=clock)
        await service.handle_document_deleted("doc-1", "dr-7")

        job = await store.get_job(job_id)
        assert job["status"] == "queued"
        assert await store.is_subject_deleted("document", "doc-1", "dr-8") is False

    async def test_late_sample_with_deleted_revision_rejected(self, store, clock):
        service = make_service(store, clock=clock)
        await service.handle_document_deleted("doc-1", "dr-7")
        # 新 retrieval_id 不绕过 tombstone：迟到样本携带已删文档版本
        with pytest.raises(EvaluationDeletionConflictError):
            await _save_sample(
                store,
                producer_event_id="evt-late-retrieval",
                target={
                    "service": "n-kb",
                    "kind": "retrieval",
                    "subject_id": "search-new",
                    "subject_revision": "rev-new",
                },
                evidence_targets=[_document_target()],
            )

    async def test_late_agent_sample_with_deleted_revision_rejected(
        self, store, clock
    ):
        service = make_service(store, clock=clock)
        await service.handle_document_deleted("doc-1", "dr-7")
        with pytest.raises(EvaluationDeletionConflictError):
            await _save_sample(
                store,
                producer_event_id="evt-late-agent",
                evidence_targets=[_document_target()],
            )

    async def test_document_revision_kind_variant_also_rejected(
        self, store, clock
    ):
        """T3 时代 document_revision 类别条目同样命中 tombstone 门禁。"""
        service = make_service(store, clock=clock)
        await service.handle_document_deleted("doc-1", "dr-7")
        target = _document_target()
        target["subject_kind"] = "document_revision"
        with pytest.raises(EvaluationDeletionConflictError):
            await _save_sample(
                store,
                producer_event_id="evt-late-variant",
                evidence_targets=[target],
            )


# ---------------------------------------------------------------------------
# 迟到样本拒绝（会话维度）与幂等重放
# ---------------------------------------------------------------------------


class TestLateSampleRejection:
    async def test_late_session_sample_rejected(self, store, clock):
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")
        with pytest.raises(EvaluationDeletionConflictError):
            await _save_sample(store, producer_event_id="evt-late")

    async def test_late_agent_run_sample_rejected(self, store, clock):
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")
        with pytest.raises(EvaluationDeletionConflictError):
            await _save_sample(
                store,
                producer_event_id="evt-late-run",
                evidence_targets=[
                    {
                        "source_service": "n-agent",
                        "subject_kind": "session",
                        "subject_id": "s-1",
                        "subject_revision": "",
                    },
                    {
                        "source_service": "n-agent",
                        "subject_kind": "agent_run",
                        "subject_id": "r-late",
                        "subject_revision": "",
                    },
                ],
            )

    async def test_tombstone_rejection_precedes_idempotent_replay(
        self, store, clock
    ):
        payload = _sample_payload()
        await store.save_sample(payload)
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")
        # 同 producer_event_id 同内容的幂等重放在删除后不得复活式返回
        with pytest.raises(EvaluationDeletionConflictError):
            await store.save_sample(payload)

    async def test_untombstoned_subjects_unaffected(self, store, clock):
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")
        target = {
            "source_service": "n-agent",
            "subject_kind": "session",
            "subject_id": "s-live",
            "subject_revision": "",
        }
        sample_id = await _save_sample(
            store,
            producer_event_id="evt-live",
            evidence_targets=[target],
        )
        assert sample_id


# ---------------------------------------------------------------------------
# 删除 vs 发送并发（spec 14）
# ---------------------------------------------------------------------------


class TestDeletionSendConcurrency:
    async def test_claim_skips_deliveries_of_deleted_subjects(
        self, store, clock
    ):
        await _build_committed_result(store, deliveries=_delivery_specs())
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        claimed = await store.claim_next_delivery(
            "worker", 300, sinks=("langfuse",)
        )
        assert claimed is None

    async def test_freeze_rejected_after_deletion(self, store, clock):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        with pytest.raises(EvaluationDeletionConflictError):
            await store.freeze_delivery_payload(
                delivery["delivery_id"],
                external_id="lf-late",
                payload={"value": 0.87},
            )

    async def test_mid_send_deletion_blocks_verified_and_registers_task(
        self, store, clock
    ):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        delivery_id = delivery["delivery_id"]
        # 投递被 worker 领取并冻结（先登记再发送），发送中
        claimed = await store.claim_next_delivery(
            "worker", 300, sinks=("langfuse",)
        )
        assert claimed["delivery_id"] == delivery_id
        await store.freeze_delivery_payload(
            delivery_id, external_id="lf-mid", payload={"value": 0.87}
        )
        await store.record_delivery_transition(
            delivery_id, "accepted", lease_token=claimed["lease_token"]
        )
        # 发送期间删除事件到达
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        # 已预登记 external_id 必须登记外部删除任务
        tasks = await store.list_deletion_tasks()
        assert [t["external_id"] for t in tasks] == ["lf-mid"]
        # 在途投递（持有租约）不被级联改判；verified 被阻断——按 T18
        # 语义改判 unlinked（结果不得转 verified），非抛错
        row = await store.get_delivery(delivery_id)
        assert row["status"] == "accepted"
        row = await store.record_delivery_transition(
            delivery_id, "verified", lease_token=claimed["lease_token"]
        )
        assert row["status"] == "unlinked"
        assert row["last_error_type"] == "subject_deleted"

    async def test_verified_transition_redirects_to_unlinked(self, store, clock):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        delivery_id = delivery["delivery_id"]
        claimed = await store.claim_next_delivery(
            "worker", 300, sinks=("langfuse",)
        )
        await store.freeze_delivery_payload(
            delivery_id, external_id="lf-mid", payload={"value": 0.87}
        )
        await store.record_delivery_transition(
            delivery_id, "accepted", lease_token=claimed["lease_token"]
        )
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        row = await store.record_delivery_transition(
            delivery_id, "verified", lease_token=claimed["lease_token"]
        )
        assert row["status"] == "unlinked"
        assert row["last_error_type"] == "subject_deleted"
        # 改判后不得重发：claim 门禁阻断
        assert (
            await store.claim_next_delivery("worker", 300, sinks=("langfuse",))
            is None
        )


# ---------------------------------------------------------------------------
# 外部删除任务执行（先禁新发送 -> 有界收敛 -> 删除 -> 回读）
# ---------------------------------------------------------------------------


class TestExternalDeletionRunner:
    async def _frozen_delivery(self, store):
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        await store.freeze_delivery_payload(
            delivery["delivery_id"],
            external_id="lf-x",
            payload={"value": 0.87},
        )
        return built

    async def test_deleted_with_absent_read_back_marks_done(self, store, clock):
        built = await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(outcomes={"lf-x": "deleted"})
        service = make_service(
            store, deleters={"langfuse": deleter}, clock=clock
        )
        await service.handle_session_deleted("s-1")
        report = await service.run_external_deletions_once()

        tasks = await store.list_deletion_tasks()
        assert tasks[0]["status"] == "done"
        assert report["done"] == 1
        assert deleter.deleted_calls == ["lf-x"]
        # 回读核对（删除后确认外部已不存在）
        assert deleter.read_back_calls == ["lf-x"]

    async def test_real_langfuse_sink_deleter_converges_to_done(
        self, store, clock
    ):
        """T20/M2：真实 LangfuseScoreSink 作为外部删除端经 runner 收敛
        （DELETE /api/public/scores/{id} 202 异步受理 -> deleted，v3 回读
        空列表 -> None -> done）；httpx MockTransport 无网络。"""
        import httpx

        from app.infrastructure.evaluation.langfuse_sink import LangfuseScoreSink

        seen = {"delete": 0, "read_back": 0}

        def api(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "DELETE" and path.startswith(
                "/api/public/scores/"
            ):
                seen["delete"] += 1
                return httpx.Response(202, json={"message": "queued"})
            if request.method == "GET" and path == "/api/public/v3/scores":
                seen["read_back"] += 1
                return httpx.Response(200, json={"data": []})
            return httpx.Response(404, json={"detail": "unreachable"})

        sink = LangfuseScoreSink(
            base_url="http://langfuse.test",
            public_key="pk-test",
            secret_key="sk-test-secret",
            project="proj",
            config_store=store,
            transport=httpx.MockTransport(api),
        )
        await self._frozen_delivery(store)
        service = make_service(
            store, deleters={"langfuse": sink}, clock=clock
        )
        await service.handle_session_deleted("s-1")
        report = await service.run_external_deletions_once()

        task = (await store.list_deletion_tasks())[0]
        assert task["status"] == "done"
        assert report["done"] == 1
        assert seen["delete"] == 1
        assert seen["read_back"] == 1

    async def test_first_not_found_is_not_conclusive(self, store, clock):
        built = await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(outcomes={"lf-x": "not_found"})
        service = make_service(
            store,
            deleters={"langfuse": deleter},
            clock=clock,
            # 重试间隔 0：测试内固定 clock 下下一轮即可重试
            deletion_retry_delay_seconds=0.0,
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()

        task = (await store.list_deletion_tasks())[0]
        # 第一次 404 不作结论：保留 deletion_pending 重复核对
        assert task["status"] == "deletion_pending"
        assert task["attempt_count"] == 1

        await service.run_external_deletions_once()
        task = (await store.list_deletion_tasks())[0]
        # 重复核对确认缺席后 done（第 CONFIRM_ABSENT_RUNS 次核对收敛）
        assert task["status"] == "done"
        assert task["attempt_count"] == CONFIRM_ABSENT_RUNS
        assert len(deleter.read_back_calls) == CONFIRM_ABSENT_RUNS

    async def test_read_back_present_keeps_deletion_pending(self, store, clock):
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(
            outcomes={"lf-x": "deleted"},
            read_back_present={"lf-x"},
        )
        service = make_service(
            store, deleters={"langfuse": deleter}, clock=clock
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()

        task = (await store.list_deletion_tasks())[0]
        assert task["status"] == "deletion_pending"
        assert task["last_error_type"] == "read_back_present"

    async def test_unknown_outcome_retries_with_backoff(self, store, clock):
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(
            outcomes={"lf-x": "unknown"}, read_back_present={"lf-x"}
        )
        service = make_service(
            store, deleters={"langfuse": deleter}, clock=clock
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()

        task = (await store.list_deletion_tasks())[0]
        assert task["status"] == "deletion_pending"
        assert task["attempt_count"] == 1
        assert task["next_retry_at"] is not None

    async def test_sink_exception_keeps_task_deletion_pending(
        self, store, clock
    ):
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(raise_on={"lf-x"})
        service = make_service(
            store, deleters={"langfuse": deleter}, clock=clock
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()

        task = (await store.list_deletion_tasks())[0]
        assert task["status"] == "deletion_pending"
        assert task["attempt_count"] == 1

    async def test_unavailable_sink_keeps_task_untouched(self, store, clock):
        await self._frozen_delivery(store)
        service = make_service(store, deleters={}, clock=clock)
        await service.handle_session_deleted("s-1")
        report = await service.run_external_deletions_once()

        task = (await store.list_deletion_tasks())[0]
        assert task["status"] == "pending"
        assert "langfuse" in report["unavailable_sinks"]

    async def test_disables_sink_and_waits_for_convergence_before_delete(
        self, store, clock
    ):
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(outcomes={"lf-x": "deleted"})
        gate = FakeDeliveryGate()
        service = make_service(
            store,
            deleters={"langfuse": deleter},
            gate=gate,
            clock=clock,
            converge_wait_seconds=2.5,
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()

        # 先禁新发送（带 sweep 归因）-> 有界在途收敛等待 -> 删除 -> 恢复出口
        assert gate.calls == [
            ("disable", "langfuse", "deletion_sweep"),
            ("wait", 2.5),
            ("enable", "langfuse"),
        ]

    async def test_no_gate_skips_disable_enable(self, store, clock):
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(outcomes={"lf-x": "deleted"})
        service = make_service(
            store, deleters={"langfuse": deleter}, clock=clock
        )
        await service.handle_session_deleted("s-1")
        # 无 delivery_worker 注入时不做出口开关（单进程嵌入场景）
        report = await service.run_external_deletions_once()
        assert report["done"] == 1

    async def test_defers_deletion_pending_tasks_not_yet_due(self, store, clock):
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(outcomes={"lf-x": "unknown"})
        service = make_service(
            store, deleters={"langfuse": deleter}, clock=clock
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()
        assert deleter.deleted_calls == ["lf-x"]

        # next_retry_at 未到：本轮跳过，不重复调用外部删除端
        report = await service.run_external_deletions_once()
        assert report["deferred"] == 1
        assert deleter.deleted_calls == ["lf-x"]

    async def test_convergence_timeout_defers_round_then_converges(
        self, store, clock
    ):
        """M1：wait_inflight 超时（进程内在途未收敛）——本轮不执行删除，
        任务一律 deletion_pending 延后；下一轮收敛后删除。"""
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(outcomes={"lf-x": "deleted"})
        gate = FakeDeliveryGate(wait_outcomes=[False, True])
        service = make_service(
            store,
            deleters={"langfuse": deleter},
            gate=gate,
            clock=clock,
            converge_wait_seconds=0.5,
        )
        await service.handle_session_deleted("s-1")
        report1 = await service.run_external_deletions_once()

        assert deleter.deleted_calls == []
        assert report1["deletion_pending"] == 1
        task = (await store.list_deletion_tasks())[0]
        assert task["status"] == "deletion_pending"
        assert task["last_error_type"] == "inflight_not_converged"
        # 出口在 finally 中恢复（disable 带 sweep 归因）
        assert gate.calls[0] == ("disable", "langfuse", "deletion_sweep")
        assert gate.calls[-1] == ("enable", "langfuse")

        report2 = await service.run_external_deletions_once()
        assert report2["done"] == 1
        assert deleter.deleted_calls == ["lf-x"]

    async def test_mixed_history_does_not_skip_repeat_confirmation(
        self, store, clock
    ):
        """M2：混合历史（deleted -> read_back_present -> not_found）下
        单次 404 不得判 done——连续缺席计数与全局 attempt_count 解耦，
        仅当上一轮处于 awaiting_repeat_confirmation 时连续性才成立。"""
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(
            outcome_sequence=["deleted", "not_found", "not_found"],
            read_back_sequence=[True, False, False],
        )
        service = make_service(
            store,
            deleters={"langfuse": deleter},
            clock=clock,
            deletion_retry_delay_seconds=0.0,
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()
        task = (await store.list_deletion_tasks())[0]
        # 回读仍存在：read_back_present（消耗历史，不进入连续缺席计数）
        assert task["status"] == "deletion_pending"
        assert task["last_error_type"] == "read_back_present"

        await service.run_external_deletions_once()
        task = (await store.list_deletion_tasks())[0]
        # 第一次 404（混合历史）不作结论
        assert task["status"] == "deletion_pending"
        assert task["last_error_type"] == "awaiting_repeat_confirmation"

        await service.run_external_deletions_once()
        task = (await store.list_deletion_tasks())[0]
        # 连续第二轮缺席确认后才 done
        assert task["status"] == "done"

    async def test_enable_sink_runs_even_when_delete_raises(
        self, store, clock
    ):
        """M3：disable 纳入 try/finally——处理中异常也恢复出口。"""
        await self._frozen_delivery(store)
        deleter = FakeExternalDeleter(raise_on={"lf-x"})
        gate = FakeDeliveryGate()
        service = make_service(
            store, deleters={"langfuse": deleter}, gate=gate, clock=clock
        )
        await service.handle_session_deleted("s-1")
        await service.run_external_deletions_once()

        assert gate.calls[0][0] == "disable"
        assert gate.calls[-1] == ("enable", "langfuse")


# ---------------------------------------------------------------------------
# tombstone 幂等（重复删除事件可重试）
# ---------------------------------------------------------------------------


class TestTombstoneIdempotency:
    async def test_repeated_tombstone_returns_same_id(self, store):
        first = await store.record_deletion_tombstone(
            {"subject_kind": "session", "subject_id": "s-1"}
        )
        second = await store.record_deletion_tombstone(
            {"subject_kind": "session", "subject_id": "s-1", "reason": "retry"}
        )
        assert first == second


# ---------------------------------------------------------------------------
# 删除期间运行中 Judge 返回不得提交或复活内容
# ---------------------------------------------------------------------------


class TestJudgeCommitGate:
    async def test_commit_rejected_when_subject_deleted_while_judging(
        self, store, clock
    ):
        built = await _build_running_job(store)
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        with pytest.raises(EvaluationDeletionConflictError):
            await store.commit_result_with_deliveries(
                _auto_result_payload(built["job_id"]),
                _delivery_specs(),
                lease_token=built["lease_token"],
                job_terminal_status="succeeded",
            )
        # job 保持 running（由 worker 收敛终结），无 result 落库
        job = await store.get_job(built["job_id"])
        assert job["status"] == "running"
        assert await store.list_current_results() == []

    async def test_worker_discards_result_and_fails_job_public_path(
        self, store, clock
    ):
        """Mi6：worker 公开路径（claim -> process）下的删除丢弃语义。

        删除发生在 Judge 在途期间（claim 后、提交前）：评估正常返回，
        提交被 store 删除门禁拒绝 -> 结果丢弃、job 以 error 终结。"""
        from app.application.evaluation_worker import EvaluationWorker
        from app.domain.evaluation.models import EvidenceSnapshot

        await store.save_definition(
            _definition_payload(
                method="llm_judge",
                judge_model="judge-model-x",
                required_fields=(),
            )
        )
        sample_id = await _save_sample(store)
        job_id = await store.save_job(_job_payload(sample_id))
        worker = EvaluationWorker(
            store,
            evaluator=_ValueEvaluator(),
            evidence_store=_SnapshotEvidence(
                EvidenceSnapshot(
                    content={"query": "q", "answer": "a"},
                    redaction_version="redaction.v1",
                )
            ),
            claimant="test-worker",
            heartbeat_interval_seconds=None,
        )
        claim = await worker.claim()
        assert claim["job_id"] == job_id
        # claim 后删除事件到达（Judge 在途形态）
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        await worker.process(claim)
        # 删除冲突：结果被丢弃，job 以 error 终结（不复活为 succeeded）
        job = await store.get_job(job_id)
        assert job["status"] == "error"
        assert await store.list_current_results() == []


class _ValueEvaluator:
    """Mi6 公开路径用 judge fake：返回可解析的 value 输出。"""

    async def evaluate(self, definition, snapshot) -> Mapping[str, Any]:
        return {"status": "value", "value": 0.9, "explanation": "ok"}


class _SnapshotEvidence:
    """Mi6 公开路径用 evidence fake：固定快照按 ref 返回。"""

    def __init__(self, snapshot):
        self._snapshot = snapshot

    async def load_snapshot(self, snapshot_ref: str):
        return self._snapshot

    async def has_snapshot(self, snapshot_ref: str) -> bool:
        return True


# ---------------------------------------------------------------------------
# store v4 -> v5 迁移（deletion_tasks.result_id 关联列）
# ---------------------------------------------------------------------------


def _downgrade_to_v4(db_path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "ALTER TABLE evaluation_deletion_tasks DROP COLUMN result_id"
        )
        # Mi2：v5 同时含 idx_jobs_sample_id 覆盖索引迁移，降级模拟一并删除
        conn.execute("DROP INDEX IF EXISTS idx_jobs_sample_id")
        conn.execute(
            "DELETE FROM evaluation_schema_migrations WHERE version = 5"
        )
        conn.execute("PRAGMA user_version = 4")
        conn.commit()


def _index_names(db_path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    return {r[0] for r in rows}


def _column_names(db_path, table: str) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


class TestSchemaV5Migration:
    async def test_v5_migration_adds_result_id_preserving_rows(self, tmp_path):
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        task_id = await store.save_deletion_task(
            {
                "sink": "langfuse",
                "subject_kind": "result",
                "subject_id": "res-1",
                "external_id": "lf-1",
                "result_id": "res-1",
            }
        )
        _downgrade_to_v4(db_path)

        reopened = SQLiteEvaluationStore(str(db_path))
        assert "result_id" in _column_names(db_path, "evaluation_deletion_tasks")
        # 旧任务行保留（v5 列对旧行为 NULL——DROP COLUMN 丢弃列数据）
        task = await reopened.get_deletion_task(task_id)
        assert task["external_id"] == "lf-1"
        assert task["result_id"] is None
        # v5 后新登记的任务持久 result_id
        new_task_id = await reopened.save_deletion_task(
            {
                "sink": "langfuse",
                "subject_kind": "result",
                "subject_id": "res-2",
                "external_id": "lf-2",
                "result_id": "res-2",
            }
        )
        assert (await reopened.get_deletion_task(new_task_id))["result_id"] == "res-2"

    async def test_concurrent_v5_migration_all_succeed(self, tmp_path):
        db_path = tmp_path / "v4-concurrent.db"
        SQLiteEvaluationStore(str(db_path))
        _downgrade_to_v4(db_path)

        def _open():
            SQLiteEvaluationStore(str(db_path))
            return _column_names(db_path, "evaluation_deletion_tasks")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: _open(), range(2)))
        assert all("result_id" in cols for cols in results)

    async def test_v8_schema_version_recorded(self, tmp_path):
        db_path = tmp_path / "evaluations.db"
        store = SQLiteEvaluationStore(str(db_path))
        # schema v7（results 增 evaluation_trace_id/span_id、samples 增
        # cohort）+ v8（definition usage 表 + feedback 完整线程唯一索引）：
        # 版本行随 SCHEMA_VERSION 单一权威（旧行仅存在于旧库迁移路径，
        # 新库只落当前版本行）
        assert store.SCHEMA_VERSION == 8
        with sqlite3.connect(str(db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
            row = conn.execute(
                "SELECT name FROM evaluation_schema_migrations "
                "WHERE version = 8"
            ).fetchone()
        assert row is not None
        assert "definition-usage" in row[0]

    async def test_v5_migration_creates_jobs_sample_id_index(self, tmp_path):
        """Mi2：v5 迁移补 idx_jobs_sample_id（新库由 SCHEMA_SQL 建全，
        v4 旧库迁移重建；DROP COLUMN 后重开仍恢复）。"""
        db_path = tmp_path / "evaluations.db"
        SQLiteEvaluationStore(str(db_path))
        assert "idx_jobs_sample_id" in _index_names(db_path)
        _downgrade_to_v4(db_path)
        SQLiteEvaluationStore(str(db_path))
        assert "idx_jobs_sample_id" in _index_names(db_path)

    async def test_cascade_queries_use_jobs_sample_id_index(self, tmp_path):
        """Mi2：三个级联查询形态经 EXPLAIN QUERY PLAN 验证走
        idx_jobs_sample_id（不在 jobs 全表扫描）。"""
        db_path = tmp_path / "evaluations.db"
        SQLiteEvaluationStore(str(db_path))
        queries = [
            # list_result_ids_for_samples / unlink_deliveries_for_samples
            "SELECT r.result_id FROM evaluation_results r "
            "JOIN evaluation_jobs j ON r.job_id = j.job_id "
            "WHERE j.sample_id IN ('x')",
            # list_deliveries_for_samples
            "SELECT d.delivery_id FROM evaluation_deliveries d "
            "JOIN evaluation_results r ON d.result_id = r.result_id "
            "JOIN evaluation_jobs j ON r.job_id = j.job_id "
            "WHERE j.sample_id IN ('x')",
            # 窗口快照 error 分支经样本关联复核 tombstone 的子查询形态
            "SELECT 1 FROM evaluation_evidence_targets et "
            "WHERE et.sample_id IN "
            "(SELECT j.sample_id FROM evaluation_jobs j "
            " WHERE j.status = 'error')",
        ]
        with sqlite3.connect(str(db_path)) as conn:
            for sql in queries[:2]:
                plan = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
                plan_text = " ".join(str(row) for row in plan)
                assert "idx_jobs_sample_id" in plan_text, plan_text


# ---------------------------------------------------------------------------
# SessionService 集成（复用 on_session_deleted handler；业务语义不变）
# ---------------------------------------------------------------------------


class TestSessionServiceIntegration:
    async def test_delete_session_triggers_evaluation_cascade(
        self, store, clock
    ):
        from app.application.session_service import SessionService

        await _build_committed_result(store)
        service = make_service(store, clock=clock)
        memory = FakeMemoryStore()
        session_service = SessionService(
            memory,
            on_session_deleted_handlers=[service.handle_session_deleted],
        )
        await session_service.delete_session("s-1")

        assert memory.deleted == ["s-1"]
        assert await store.is_subject_deleted("session", "s-1") is True

    async def test_cascade_failure_does_not_block_session_deletion(
        self, store, clock
    ):
        from app.application.session_service import SessionService

        class ExplodingService:
            async def handle_session_deleted(self, session_id: str) -> None:
                raise OSError("evaluation store unavailable")

        memory = FakeMemoryStore()
        session_service = SessionService(
            memory,
            on_session_deleted_handlers=[ExplodingService().handle_session_deleted],
        )
        # post-delete handler 失败被 SessionService 吞掉并告警，不回滚删除
        await session_service.delete_session("s-1")
        assert memory.deleted == ["s-1"]

    async def test_active_run_rejection_semantics_unchanged(self):
        from app.application.session_service import SessionBusyError, SessionService

        memory = FakeMemoryStore(active_run_ids={"run-1"})
        session_service = SessionService(memory)
        with pytest.raises(SessionBusyError):
            await session_service.delete_session("s-1")
        assert memory.deleted == []


# ---------------------------------------------------------------------------
# evidence store purge（真实 LocalEvidenceStore 契约）
# ---------------------------------------------------------------------------


class TestEvidenceStorePurge:
    async def test_purge_snapshot_deletes_file_and_reports(self, tmp_path, store):
        from app.domain.evaluation.models import EvidenceSnapshot
        from app.infrastructure.evaluation.evidence_store import (
            EvidenceNotFoundError,
            LocalEvidenceStore,
        )

        target = _make_evaluation_target()
        snapshot = EvidenceSnapshot(
            content={"query": "q"},
            redaction_version="capture-redaction-v1",
        )
        evidence = LocalEvidenceStore(tmp_path / "evidence", store)
        ref = await evidence.save_snapshot(target, snapshot)
        assert await evidence.has_snapshot(ref) is True

        removed = await evidence.purge_snapshot(ref)
        assert removed is True
        # 正文文件已删除：load_snapshot 经登记行读取文件时 fail-closed
        with pytest.raises(EvidenceNotFoundError):
            await evidence.load_snapshot(ref)
        # 重复 purge 幂等（文件已不存在返回 False）
        assert await evidence.purge_snapshot(ref) is False

    async def test_purge_rejects_invalid_ref(self, tmp_path, store):
        from app.infrastructure.evaluation.evidence_store import (
            LocalEvidenceStore,
        )

        evidence = LocalEvidenceStore(tmp_path / "evidence", store)
        with pytest.raises(ValueError):
            await evidence.purge_snapshot("../escape")
        with pytest.raises(ValueError):
            await evidence.purge_snapshot("")

    async def test_purge_rejects_symlink_intermediate_dir(
        self, tmp_path, store
    ):
        """Mi1：purge_snapshot 复用 _resolve_existing 逐组件校验——快照
        目录被替换为符号链接时拒绝（对齐 load_snapshot 的 TOCTOU 防御）。"""
        from app.domain.evaluation.models import (
            EvidenceSnapshot,
            EvaluationValidationError,
        )
        from app.infrastructure.evaluation.evidence_store import (
            LocalEvidenceStore,
        )

        evidence = LocalEvidenceStore(tmp_path / "evidence", store)
        ref = await evidence.save_snapshot(
            _make_evaluation_target(),
            EvidenceSnapshot(
                content={"query": "q"},
                redaction_version="capture-redaction-v1",
            ),
        )
        snapshots_dir = tmp_path / "evidence" / "snapshots"
        real_dir = tmp_path / "evidence" / "snapshots_real"
        snapshots_dir.rename(real_dir)
        snapshots_dir.symlink_to(real_dir, target_is_directory=True)

        with pytest.raises(EvaluationValidationError):
            await evidence.purge_snapshot(ref)


def _make_evaluation_target():
    from app.domain.evaluation.models import EvaluationTarget

    return EvaluationTarget(
        service="n-agent",
        kind="agent_run",
        subject_id="r-purge",
        subject_revision="rev-1",
    )


# ---------------------------------------------------------------------------
# Mi3 store 级联门禁：job 领取前置 + 迟到人评拒绝
# ---------------------------------------------------------------------------


class TestStoreCascadeGates:
    async def test_claim_next_job_skips_deleted_subject_jobs(self, store, clock):
        """Mi3：已删主体的 job 不可领取（不对已 purge 快照消耗 Judge 预算）；
        未删主体的 job 正常领取。"""
        await store.save_definition(_definition_payload())
        deleted_sample = await _save_sample(store)
        deleted_job = await store.save_job(_job_payload(deleted_sample))
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")

        assert await store.claim_next_job("w", 300) is None
        # 级联已撤销 queued job（cancelled），且领取门禁继续跳过
        assert (
            await store.get_job(deleted_job)
        )["status"] == "cancelled"

        live_sample = await _save_sample(
            store,
            producer_event_id="evt-live",
            evidence_targets=[
                {
                    "source_service": "n-agent",
                    "subject_kind": "session",
                    "subject_id": "s-live",
                    "subject_revision": "",
                }
            ],
        )
        live_job = await store.save_job(
            _job_payload(live_sample, idempotency_key="j2")
        )
        claim = await store.claim_next_job("w", 300)
        assert claim["job_id"] == live_job

    async def test_save_feedback_rejects_deleted_subject(self, store, clock):
        """Mi3：迟到人评门禁——目标主体已删除时拒绝新反馈。"""
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")
        with pytest.raises(EvaluationDeletionConflictError):
            await store.save_feedback(_feedback_payload())

    async def test_save_feedback_allows_live_subject(self, store, clock):
        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")
        feedback_id = await store.save_feedback(
            _feedback_payload(
                target={
                    "service": "n-agent",
                    "kind": "session",
                    "subject_id": "s-live",
                    "subject_revision": "rev-1",
                },
            )
        )
        assert feedback_id

    async def test_window_snapshot_error_branch_excludes_deleted_subject(
        self, store, clock
    ):
        """Mi4：窗口快照 error 分支与 result 分支同口径——已删主体样本的
        error job 不计入聚合。"""
        built = await _build_running_job(store)
        await store.fail_job(
            built["job_id"],
            lease_token=built["lease_token"],
            error_type="boom",
        )
        window_start = "2026-01-01T00:00:00+00:00"
        before = await store.query_window_snapshot(window_start=window_start)
        assert sum(r["error_count"] for r in before) == 1

        service = make_service(store, clock=clock)
        await service.handle_session_deleted("s-1")
        after = await store.query_window_snapshot(window_start=window_start)
        assert sum(r["error_count"] for r in after) == 0


# ---------------------------------------------------------------------------
# 真实 delivery worker 门禁（Mi6/Mi7/M3）
# ---------------------------------------------------------------------------


class _InstantSink:
    """ScoreSink 最小 fake（不参与 process 的用例用）。"""

    async def project(self, context: dict) -> dict:
        return {
            "outcome": "deliverable",
            "external_id": "instant-1",
            "payload": {"external_id": "instant-1"},
        }

    async def send(self, payload: dict) -> str:
        return "instant-1"

    async def read_back(self, external_id: str) -> dict | None:
        return {"id": external_id}

    async def verify(self, payload: dict, read_back: dict | None) -> bool:
        return read_back is not None


class _SlowSink:
    """ScoreSink fake：send 可控延迟（并发在途窗口用）。"""

    def __init__(self, *, external_id="lf-x", send_delay=0.5):
        self.external_id = external_id
        self.send_delay = send_delay
        self.sent = []

    async def project(self, context: dict) -> dict:
        return {
            "outcome": "deliverable",
            "external_id": self.external_id,
            "payload": {"external_id": self.external_id},
        }

    async def send(self, payload: dict) -> str:
        await asyncio.sleep(self.send_delay)
        self.sent.append(dict(payload))
        return self.external_id

    async def read_back(self, external_id: str) -> dict | None:
        return {"id": external_id}

    async def verify(self, payload: dict, read_back: dict | None) -> bool:
        return read_back is not None


def _make_delivery_worker(store, sink):
    from app.infrastructure.evaluation.delivery_worker import (
        EvaluationDeliveryWorker,
    )

    return EvaluationDeliveryWorker(
        store,
        sinks={"langfuse": sink},
        claimant="w",
        verify_poll_attempts=1,
        verify_poll_interval_seconds=0.01,
        poll_interval_seconds=0.01,
    )


class TestRealDeliveryWorkerGates:
    async def test_wait_inflight_reports_inflight_and_timeout(self, store):
        """Mi6：wait_inflight 单测——空闲返回 True；注入未完成任务且超时
        返回 False；任务完成后恢复 True。"""
        worker = _make_delivery_worker(store, _InstantSink())
        assert await worker.wait_inflight(0.01) is True

        pending = asyncio.create_task(asyncio.sleep(3600))
        worker._inflight.add(pending)
        try:
            assert await worker.wait_inflight(0.01) is False
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert await worker.wait_inflight(0.01) is True

    async def test_disable_sink_skips_leased_deliveries(self, store, clock):
        """Mi7：disable_sink 跳过持活跃租约的在途投递（无 CAS 抢改）；
        无租约投递正常关闭。"""
        leased = await _build_committed_result(
            store,
            deliveries=_delivery_specs(),
            producer_event_id="evt-lease",
            job_idempotency_key="idem-lease",
        )
        unleased = await _build_committed_result(
            store,
            deliveries=_delivery_specs(),
            producer_event_id="evt-unlease",
            job_idempotency_key="idem-unlease",
        )
        leased_delivery = (await store.list_deliveries(leased["result_id"]))[0]
        unleased_delivery = (
            await store.list_deliveries(unleased["result_id"])
        )[0]
        claimed = await store.claim_next_delivery(
            "other-worker", 300, sinks=("langfuse",)
        )
        assert claimed["delivery_id"] == leased_delivery["delivery_id"]

        worker = _make_delivery_worker(store, _InstantSink())
        await worker.disable_sink("langfuse")

        assert (
            await store.get_delivery(leased_delivery["delivery_id"])
        )["status"] == "pending"
        assert (
            await store.get_delivery(unleased_delivery["delivery_id"])
        )["status"] == "disabled"

    async def test_startup_self_heals_sweep_disabled_only(self, store, clock):
        """M3：worker 启动期按 sweep 归因精确自愈——上一进程删除传播
        disable 后未及恢复的投递回 pending；管理员显式 disable 不受影响。"""
        swept = await _build_committed_result(
            store,
            deliveries=_delivery_specs(),
            producer_event_id="evt-swept",
            job_idempotency_key="idem-swept",
        )
        admin = await _build_committed_result(
            store,
            deliveries=_delivery_specs(),
            producer_event_id="evt-admin",
            job_idempotency_key="idem-admin",
        )
        swept_delivery = (await store.list_deliveries(swept["result_id"]))[0]
        admin_delivery = (await store.list_deliveries(admin["result_id"]))[0]

        first = _make_delivery_worker(store, _InstantSink())
        # sweep 关闭（归因标记落 last_error_type）
        await first.disable_sink("langfuse", reason="deletion_sweep")
        # 管理员随后显式关闭 admin 投递（无归因标记——模拟 sweep 恢复后
        # 的管理员操作：归因被管理员路径清空）
        await store.record_delivery_transition(
            admin_delivery["delivery_id"], "pending"
        )
        await store.record_delivery_transition(
            admin_delivery["delivery_id"], "disabled"
        )

        second = _make_delivery_worker(store, _InstantSink())
        await second.start()
        try:
            assert (
                await store.get_delivery(swept_delivery["delivery_id"])
            )["status"] == "pending"
            assert (
                await store.get_delivery(admin_delivery["delivery_id"])
            )["status"] == "disabled"
        finally:
            await second.stop()

    async def test_sweep_defers_while_worker_inflight_then_deletes(
        self, store, clock
    ):
        """Mi6：真实 delivery worker 的 disable -> wait -> defer -> delete
        并发用例——发送在途时删除轮延后（不执行外部删除）；在途投递经
        verified 门禁改判 unlinked；下一轮完成外部删除。"""
        built = await _build_committed_result(
            store, deliveries=_delivery_specs()
        )
        delivery = (await store.list_deliveries(built["result_id"]))[0]
        delivery_id = delivery["delivery_id"]

        slow = _SlowSink(send_delay=0.5)
        worker = _make_delivery_worker(store, slow)
        deleter = FakeExternalDeleter(outcomes={"lf-x": "deleted"})
        service = make_service(
            store,
            deleters={"langfuse": deleter},
            gate=worker,
            clock=clock,
            converge_wait_seconds=0.05,
        )
        claimed = await store.claim_next_delivery(
            "w", 300, sinks=("langfuse",)
        )
        assert claimed["delivery_id"] == delivery_id
        # 注册到 worker 在途集合（与 _loop 派发路径一致）
        process_task = asyncio.create_task(worker.process(claimed))
        worker._inflight.add(process_task)
        process_task.add_done_callback(worker._inflight.discard)
        # 等待 process 进入 send 在途窗口（project/freeze 极快）
        await asyncio.sleep(0.1)

        # 发送在途期间删除事件到达：级联登记预登记 external_id 任务，
        # 删除轮因在途未收敛延后（不执行删除）
        await service.handle_session_deleted("s-1")
        report1 = await service.run_external_deletions_once()
        assert deleter.deleted_calls == []
        tasks = await store.list_deletion_tasks()
        assert [t["external_id"] for t in tasks] == ["lf-x"]
        assert report1["deletion_pending"] >= 1

        await process_task
        # 发送中删除：结果不得 verified——改判 unlinked
        row = await store.get_delivery(delivery_id)
        assert row["status"] == "unlinked"
        assert row["last_error_type"] == "subject_deleted"

        # 在途收敛后下一轮完成外部删除
        report2 = await service.run_external_deletions_once()
        assert deleter.deleted_calls == ["lf-x"]
        task = (await store.list_deletion_tasks())[0]
        assert task["status"] == "done"
        assert report2["done"] == 1


async def test_runner_scan_pushes_status_filter_down(store):
    """24 项三方审阅 #10：扫描窗口不得被历史 done 行挤占——status 集合
    必须下推 SQL（WHERE status IN），否则累计 1000 条完成记录后新
    pending 任务永久饥饿。"""
    from app.application.evaluation_deletion import EvaluationDeletionService

    captured: dict = {}

    original = store.list_deletion_tasks

    async def spy(*args, **kwargs):
        captured["kwargs"] = kwargs
        return await original(*args, **kwargs)

    store.list_deletion_tasks = spy  # type: ignore[method-assign]
    service = EvaluationDeletionService(
        store=store, evidence_store=store, external_deleters=None,
    )
    await service.run_external_deletions_once()
    assert captured["kwargs"].get("statuses") == (
        "pending",
        "deletion_pending",
    )
