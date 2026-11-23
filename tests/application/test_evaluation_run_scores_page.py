"""T4: Application 层 ``list_run_scores_page`` 直通用例测试。

run 粒度评分分页（管理面 GET /chat/evaluations/run-scores）：
scene=online 与 target_kind=agent_run 由 store 内固定，service 签名
不暴露 scene/target_kind 等键，非法过滤在调用侧即 TypeError；store
侧 ``EvaluationValidationError`` 不被 service 吞掉。
"""

import pytest

from app.application.evaluation_service import EvaluationService
from app.domain.evaluation.models import EvaluationValidationError


class RecordingStore:
    """记录 ``query_run_scores_page_counted`` 调用入参的最小桩。"""

    def __init__(self, *, error: Exception | None = None):
        self.calls = []
        self.error = error
        self.reply = {
            "items": [],
            "next_cursor": None,
            "snapshot_at": "s",
            "total_count": 0,
            "total_pages": 1,
        }

    async def query_run_scores_page_counted(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.reply


def _make_service(store) -> EvaluationService:
    return EvaluationService(store, enabled=True)


async def test_list_run_scores_page_forwards_core_params():
    store = RecordingStore()
    service = _make_service(store)
    page = await service.list_run_scores_page(
        definition_id="d1", status="value", cursor="tok", limit=10,
    )
    assert page == store.reply
    call = store.calls[0]
    assert call["definition_id"] == "d1"
    assert call["status"] == "value"
    assert call["cursor"] == "tok"
    assert call["limit"] == 10


async def test_list_run_scores_page_forwards_all_allowed_params():
    store = RecordingStore()
    service = _make_service(store)
    await service.list_run_scores_page(
        definition_id="d1",
        definition_version="v2",
        provenance="online",
        status="value",
        created_from="2026-01-01T00:00:00Z",
        created_to="2026-01-31T23:59:59Z",
        cursor="tok",
        limit=7,
    )
    assert store.calls == [
        {
            "definition_id": "d1",
            "definition_version": "v2",
            "provenance": "online",
            "status": "value",
            "created_from": "2026-01-01T00:00:00Z",
            "created_to": "2026-01-31T23:59:59Z",
            "cursor": "tok",
            "limit": 7,
        }
    ]


async def test_list_run_scores_page_rejects_forbidden_filters():
    store = RecordingStore()
    service = _make_service(store)
    with pytest.raises(TypeError):
        await service.list_run_scores_page(target_kind="session_feedback")
    assert store.calls == []


async def test_list_run_scores_page_propagates_store_validation_error():
    error = EvaluationValidationError("bad filter")
    store = RecordingStore(error=error)
    service = _make_service(store)
    with pytest.raises(EvaluationValidationError) as exc_info:
        await service.list_run_scores_page(definition_id="d1")
    assert exc_info.value is error
