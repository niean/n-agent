"""app.application.evaluation_review 用例测试（质量复审 M3 RED -> GREEN）。

M3：_attach_query 必须按 Mapping 直接读取快照 query 字段——
EvidenceSnapshot.content 构造期冻结为 MappingProxyType（models.py:240），
json.loads(content) 只接受 str/bytes，必然 TypeError 被宽 except 吞掉，
导致 review_queue 样本 Query 恒空（静默功能失效）。"""
from __future__ import annotations

from typing import Any

from app.application.evaluation_review import EvaluationReviewService
from app.domain.evaluation.errors import EvaluationNotFoundError
from app.domain.evaluation.models import EvidenceSnapshot


class FakeStore:
    """get_job/load_sample 最小 fake。"""

    def __init__(self, job: dict, sample: dict) -> None:
        self._job = job
        self._sample = sample

    async def get_job(self, job_id: str) -> dict:
        if job_id != self._job["job_id"]:
            raise EvaluationNotFoundError("job not found")
        return self._job

    async def load_sample(self, sample_id: str) -> dict:
        if sample_id != self._sample["sample_id"]:
            raise EvaluationNotFoundError("sample not found")
        return self._sample


class FakeEvidenceReader:
    """load_snapshot 最小 fake：返回真实 EvidenceSnapshot（content 为
    构造期冻结的 MappingProxyType）。"""

    def __init__(self, snapshot: EvidenceSnapshot) -> None:
        self._snapshot = snapshot

    async def load_snapshot(self, snapshot_ref: str) -> EvidenceSnapshot:
        return self._snapshot


def _service(snapshot: EvidenceSnapshot) -> EvaluationReviewService:
    job = {"job_id": "job-1", "sample_id": "sample-1"}
    sample = {
        "sample_id": "sample-1",
        "snapshot_ref": "snapshots/snap-1",
    }
    return EvaluationReviewService(
        store=FakeStore(job, sample),
        datasets_root="/tmp/does-not-matter",
        evidence_reader=FakeEvidenceReader(snapshot),
    )


async def test_attach_query_reads_mapping_content():
    """快照 content 是 Mapping（冻结）：query 必须直接读取命中，不得因
    json.loads(TypeError) 被吞而恒空。"""
    snapshot = EvidenceSnapshot(
        content={"query": "评估闭环如何对账？", "schema": "evaluation.sample.v1"},
        redaction_version="redaction.v1",
    )
    item: dict[str, Any] = {"job_id": "job-1", "result_id": "res-1"}
    result = await _service(snapshot)._attach_query(item)
    assert result["query"] == "评估闭环如何对账？"


async def test_attach_query_missing_query_field_yields_empty():
    """快照无 query 字段：置空串（证据读取失败不阻塞队列展示）。"""
    snapshot = EvidenceSnapshot(
        content={"schema": "evaluation.sample.v1"},
        redaction_version="redaction.v1",
    )
    item: dict[str, Any] = {"job_id": "job-1", "result_id": "res-1"}
    result = await _service(snapshot)._attach_query(item)
    assert result["query"] == ""
