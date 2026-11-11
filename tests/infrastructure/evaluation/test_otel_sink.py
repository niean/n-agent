# tests/infrastructure/evaluation/test_otel_sink.py
"""#13 OTel ScoreSink 出口适配测试。

覆盖：
- project：linkability.otel_enabled 关闭 -> unlinked（不发事件）；
  deliverable 路径携带 result_id/metric_name/evaluation_trace/mode，
  external_id 为确定性 UUIDv5（同 result 同版本幂等）；
- send：emit_result 返回 emitted/noop/failed 三态映射（emitted ->
  external_id；noop -> dead_letter 类；failed -> retry 类）；
- supports_readback=False：delivery 调度器 accepted 即终态，不进
  verify_pending 退避循环（集成到 delivery worker 测试）。
"""
from __future__ import annotations

import pytest

from app.infrastructure.evaluation.otel_sink import (
    OtelSinkDeadLetterError,
    OtelSinkRetryableError,
    OtelScoreSink,
    compute_otel_external_id,
)


def _context(**overrides):
    context = {
        "result": {
            "result_id": "res-1",
            "status": "value",
            "typed_value": 0.8,
            "skip_reason": None,
            "explanation": "ok",
            "metric_version": "v1",
        },
        "target": {
            "service": "n-agent",
            "kind": "retrieval",
            "subject_id": "sub-1",
            "subject_revision": "r1",
            "trace_id": "a" * 32,
            "span_id": "b" * 16,
        },
        "definition": {
            "definition_id": "def-recall",
            "metric_name": "kb.recall_at_5",
            "version": "v1",
        },
        "linkability": {"otel_enabled": True},
        "projection_version": "v1",
        "mode": "experiment",
        # #12：评估 Trace 身份来自 result 行冻结的 evaluation.run 身份，
        # 与目标业务身份（"a"*32/"b"*16）是两条独立 Trace
        "evaluation_trace": ("c" * 32, "d" * 16),
    }
    context.update(overrides)
    return context


def _sink() -> OtelScoreSink:
    return OtelScoreSink()


class TestProject:
    async def test_unlinked_when_otel_disabled(self):
        projection = await _sink().project(
            _context(linkability={"otel_enabled": False})
        )
        assert projection["outcome"] == "unlinked"

    async def test_unlinked_when_target_has_no_trace_identity(self):
        projection = await _sink().project(
            _context(
                target={
                    "service": "n-agent",
                    "kind": "retrieval",
                    "subject_id": "sub-1",
                    "subject_revision": "r1",
                    "trace_id": None,
                    "span_id": None,
                },
                evaluation_trace=(None, None),
            )
        )
        # 目标无真实 trace 身份时仍 deliverable，但 evaluation_trace 为
        # 空身份（事件由 emit_result 落 target_link=missing 语义）
        assert projection["outcome"] == "deliverable"
        assert projection["payload"]["evaluation_trace"] == [None, None]

    async def test_deliverable_payload_and_deterministic_external_id(self):
        sink = _sink()
        projection = await sink.project(_context())
        assert projection["outcome"] == "deliverable"
        payload = projection["payload"]
        assert payload["result_id"] == "res-1"
        assert payload["metric_name"] == "kb.recall_at_5"
        assert payload["definition_id"] == "def-recall"
        assert payload["status"] == "succeeded"
        assert payload["typed_value"] == 0.8
        # #12：evaluation_trace 取评估 Trace 身份，不回退目标业务身份
        assert payload["evaluation_trace"] == ["c" * 32, "d" * 16]
        # 目标业务身份仅在 target 字段承载（Link 关联语义）
        assert payload["target"]["trace_id"] == "a" * 32
        assert payload["mode"] == "experiment"
        assert payload["metric_profile"] == "def-recall@v1"
        expected = compute_otel_external_id("n-agent", "res-1", "v1")
        assert projection["external_id"] == expected
        again = await sink.project(_context())
        assert again["external_id"] == expected  # 同 result 同版本幂等

    async def test_non_value_status_maps_error_type(self):
        projection = await _sink().project(
            _context(
                result={
                    "result_id": "res-2",
                    "status": "skipped",
                    "typed_value": None,
                    "skip_reason": "missing_reference",
                    "explanation": "",
                    "metric_version": "v1",
                }
            )
        )
        payload = projection["payload"]
        assert payload["status"] == "skipped"
        assert payload["error_type"] == "worker_error"
        assert payload["skip_reason"] == "missing_reference"


class TestSend:
    async def test_emitted_returns_external_id(self, monkeypatch):
        captured = {}

        def fake_emit_result(**kwargs):
            captured.update(kwargs)
            return "emitted"

        monkeypatch.setattr(
            "app.infrastructure.evaluation.otel_sink.emit_result",
            fake_emit_result,
        )
        projection = await _sink().project(_context())
        result = await _sink().send(projection["payload"])
        assert result == projection["external_id"]
        assert captured["result_id"] == "res-1"
        assert captured["evaluation_trace"] == ("c" * 32, "d" * 16)
        assert captured["mode"] == "experiment"
        assert captured["metric_profile"] == "def-recall@v1"

    async def test_noop_raises_dead_letter(self, monkeypatch):
        monkeypatch.setattr(
            "app.infrastructure.evaluation.otel_sink.emit_result",
            lambda **kwargs: "noop",
        )
        projection = await _sink().project(_context())
        with pytest.raises(OtelSinkDeadLetterError) as excinfo:
            await _sink().send(projection["payload"])
        assert excinfo.value.delivery_outcome == "dead_letter"
        assert excinfo.value.error_type == "otel_disabled"

    async def test_failed_raises_retryable(self, monkeypatch):
        monkeypatch.setattr(
            "app.infrastructure.evaluation.otel_sink.emit_result",
            lambda **kwargs: "failed",
        )
        projection = await _sink().project(_context())
        with pytest.raises(OtelSinkRetryableError) as excinfo:
            await _sink().send(projection["payload"])
        assert excinfo.value.delivery_outcome == "retry"
