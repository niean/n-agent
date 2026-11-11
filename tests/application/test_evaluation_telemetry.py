# T10 OTel 评估遥测测试（n-eval-v1）。
#
# 覆盖 spec 12.1/12.3 与 plan T10 全部 S1 要点：
# - gen_ai.evaluation.result LogRecord（OTLP Log 形态，原生 trace_id/span_id
#   上下文字段 + 目标关联 + 评估 Trace 引用 + 独立时间戳）；
# - 字段集锁定 n-eval-v1（_EVAL_SCHEMA_FIELDS + schema hash + 合同夹具）；
# - 独立 evaluation.run Trace + Link；无有效目标 -> target_link=missing；
# - 不修改已结束业务 span；
# - nagent.evaluation.window.* / producer.* ObservableGauge（标签封闭集 +
#   禁止标识哨兵 + metric_profile 注册表 <=16）；
# - worker/投递常规指标；OTel 禁用路径不发布不抛错。
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from importlib.metadata import version as pkg_version
from pathlib import Path

import pytest

from opentelemetry import trace as otel_trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.application import evaluation_telemetry as et
from app.application import telemetry_semantics as ts
from app.domain.evaluation.models import EvaluationTarget
from app.domain.evaluation.state import SkipReason

NOW = datetime(2026, 11, 11, 3, 0, 0, tzinfo=timezone.utc)

TARGET = EvaluationTarget(
    service="n-kb",
    kind="retrieval",
    subject_id="retr-1",
    subject_revision="r1",
    trace_id="1" * 32,
    span_id="2" * 16,
    run_id="run-1",
    session_id="sess-1",
)


def _make_target(trace_id=None, span_id=None, **overrides) -> EvaluationTarget:
    payload = {
        "service": "n-kb",
        "kind": "retrieval",
        "subject_id": "retr-2",
        "subject_revision": "r1",
    }
    if trace_id is not None:
        payload["trace_id"] = trace_id
    if span_id is not None:
        payload["span_id"] = span_id
    payload.update(overrides)
    return EvaluationTarget(**payload)


def _emit_result(target, *, status="succeeded", typed_value=0.75, **overrides):
    kwargs = {
        "result_id": "res-1",
        "metric_name": "kb.recall",
        "definition_id": "kb.recall",
        "definition_version": "1",
        "status": status,
        "target": target,
        "evaluated_at": NOW,
        "target_completed_at": NOW - timedelta(minutes=1),
    }
    if status == "succeeded":
        kwargs["typed_value"] = typed_value
        kwargs["explanation"] = "ok"
    if status == "skipped":
        kwargs["skip_reason"] = SkipReason.missing_reference
    if status == "error":
        kwargs["error_type"] = "timeout"
    kwargs.update(overrides)
    return et.emit_result(**kwargs)


def _install_log_provider(monkeypatch):
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider(resource=Resource.create({"service.name": "n-agent"}))
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    monkeypatch.setattr(
        "opentelemetry._logs._internal._LOGGER_PROVIDER", provider
    )
    return provider, exporter


def _install_tracer(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(et, "get_tracer", lambda: provider.get_tracer("eval-test"))
    monkeypatch.setattr(ts, "get_tracer", lambda: provider.get_tracer("eval-test"))
    return provider, exporter


def _install_meter(monkeypatch):
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(et, "get_meter", lambda: provider.get_meter("eval-metrics"))
    monkeypatch.setattr(ts, "get_meter", lambda: provider.get_meter("eval-metrics"))
    return provider, reader


def _records(exporter):
    return [exported.log_record for exported in exporter.get_finished_logs()]


def _ns(dt: datetime) -> int:
    """精确整数 ns 换算（不经 float 乘法，float64 尾数会引入舍入）。"""
    delta = dt.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


_WINDOW_ROW = {
    "target_service": "n-kb",
    "definition_id": "kb.recall",
    "definition_version": "1",
    "mode": "online",
    "sampling_cohort": "random",
    "succeeded_count": 3,
    "skipped_count": 1,
    "error_count": 2,
    "unlinked_count": 1,
    "numeric_sum": 2.5,
    "numeric_count": 3,
    "boolean_pass": 0,
    "boolean_count": 0,
}


# ---------------------------------------------------------------------------
# LogRecord 形态（OTLP Log / n-eval-v1 字段）
# ---------------------------------------------------------------------------


def test_numeric_result_logrecord_native_context_and_fields(monkeypatch):
    """numeric 结果：原生 trace_id/span_id 关联被评估目标，completion_time
    是评估时间，target_completed_at 单独字段，另存 evaluation Trace 引用。"""
    _log_provider, log_exporter = _install_log_provider(monkeypatch)
    provider, _span_exporter = _install_tracer(monkeypatch)
    monkeypatch.setattr(
        ts,
        "_span_attr_sanitizer",
        lambda text: text.replace("token-secret-123", "[REDACTED]"),
    )
    try:
        with et.evaluation_run_scope(
            TARGET, metric_profile="kb.recall@1", mode="online"
        ) as run:
            status = _emit_result(
                TARGET,
                explanation="回答正确 token-secret-123",
                evaluation_trace=(run.trace_id, run.span_id),
            )
        assert status == "emitted"
        record, = _records(log_exporter)
        # 原生上下文字段（不只是同名 attributes）
        assert record.trace_id == int(TARGET.trace_id, 16)
        assert record.span_id == int(TARGET.span_id, 16)
        # completion_time = 评估时间（精确整数 ns）；目标业务结束时间独立字段
        assert record.timestamp == _ns(NOW)
        attrs = dict(record.attributes)
        assert attrs["event.name"] == "gen_ai.evaluation.result"
        assert attrs["gen_ai.evaluation.name"] == "kb.recall"
        assert attrs["gen_ai.evaluation.score.value"] == 0.75
        assert "gen_ai.evaluation.score.label" not in attrs
        assert attrs["nagent.evaluation.status"] == "succeeded"
        assert attrs["nagent.evaluation.target_link"] == "linked"
        assert attrs["nagent.evaluation.result_id"] == "res-1"
        assert attrs["nagent.evaluation.target.service"] == "n-kb"
        assert attrs["nagent.evaluation.target.kind"] == "retrieval"
        assert attrs["nagent.evaluation.target.subject_id"] == "retr-1"
        assert attrs["nagent.evaluation.target_completed_at"] == (
            (NOW - timedelta(minutes=1)).isoformat()
        )
        assert attrs["nagent.evaluation.evaluation_trace_id"] == run.trace_id
        assert attrs["nagent.evaluation.evaluation_span_id"] == run.span_id
        assert attrs["nagent.evaluation.metric_profile"] == "kb.recall@1"
        assert attrs["nagent.evaluation.mode"] == "online"
        # explanation 仅短脱敏摘要
        assert "token-secret-123" not in attrs["gen_ai.evaluation.explanation"]
        # 不用 gen_ai.response.id 冒充 span_id
        assert not any("response.id" in key for key in attrs)
        # Resource.service.name 由组合根 LoggerProvider 提供（n-agent，
        # 表示事件生产者；被评估者 n-kb 用 target.service 表达）
    finally:
        provider.shutdown()


def test_sdk_provider_resource_service_name_is_n_agent(monkeypatch):
    """LoggerProvider Resource.service.name=n-agent（组合根接线契约），
    被评估者 n-kb 仅以 target.service 字段表达。"""
    provider, log_exporter = _install_log_provider(monkeypatch)
    _emit_result(TARGET)
    exported, = log_exporter.get_finished_logs()
    assert exported.resource.attributes["service.name"] == "n-agent"


@pytest.mark.parametrize(
    "typed_value,expected",
    [(True, 1.0), (False, 0.0)],
)
def test_boolean_result_emits_numeric_0_1(monkeypatch, typed_value, expected):
    """boolean 发送 0/1 数值（不发 label）。"""
    _log_provider, log_exporter = _install_log_provider(monkeypatch)
    _emit_result(TARGET, typed_value=typed_value)
    attrs = dict(_records(log_exporter)[-1].attributes)
    assert attrs["gen_ai.evaluation.score.value"] == expected
    assert "gen_ai.evaluation.score.label" not in attrs


def test_categorical_result_emits_label_only(monkeypatch):
    """categorical 发送 label，不发数值。"""
    _log_provider, log_exporter = _install_log_provider(monkeypatch)
    _emit_result(TARGET, typed_value="good")
    attrs = dict(_records(log_exporter)[-1].attributes)
    assert attrs["gen_ai.evaluation.score.label"] == "good"
    assert "gen_ai.evaluation.score.value" not in attrs


@pytest.mark.parametrize(
    "status,extra",
    [
        ("skipped", {"skip_reason": SkipReason.missing_reference}),
        ("error", {"error_type": "timeout"}),
    ],
)
def test_skipped_and_error_have_no_score_fields(monkeypatch, status, extra):
    """skipped/error 无 score 字段，携带 status/skip_reason 与 error.type。"""
    _log_provider, log_exporter = _install_log_provider(monkeypatch)
    target = _make_target(trace_id="3" * 32, span_id="4" * 16)
    if status == "skipped":
        _emit_result(target, status="skipped", skip_reason=extra["skip_reason"])
    else:
        _emit_result(target, status="error", error_type=extra["error_type"])
    attrs = dict(_records(log_exporter)[-1].attributes)
    assert "gen_ai.evaluation.score.value" not in attrs
    assert "gen_ai.evaluation.score.label" not in attrs
    assert attrs["nagent.evaluation.status"] == status
    if status == "skipped":
        assert attrs["nagent.evaluation.skip_reason"] == "missing_reference"
        assert "error.type" not in attrs
    else:
        assert attrs["error.type"] == "timeout"
        assert "nagent.evaluation.skip_reason" not in attrs


def test_explanation_is_short_redacted_summary(monkeypatch):
    """脱敏先于限长；长 explanation 截到短摘要上限。"""
    _log_provider, log_exporter = _install_log_provider(monkeypatch)
    monkeypatch.setattr(
        ts,
        "_span_attr_sanitizer",
        lambda text: text.replace("secret-value", "[REDACTED]"),
    )
    _emit_result(TARGET, explanation="前缀 secret-value " + "长" * 2000)
    attrs = dict(_records(log_exporter)[-1].attributes)
    summary = attrs["gen_ai.evaluation.explanation"]
    assert "secret-value" not in summary
    assert "[REDACTED]" in summary
    assert len(summary.encode("utf-8")) <= et.EVALUATION_EXPLANATION_MAX_BYTES


def test_unregistered_fields_rejected_on_write():
    """未注册字段拒绝写入（n-eval-v1 封闭字段集）。"""
    for forbidden in (
        "gen_ai.response.id",
        "dataset_id",
        "case_id",
        "query",
        "kb.document.id",
        "session_id",
    ):
        with pytest.raises(ValueError):
            et.build_eval_attributes({forbidden: "x"})
    assert "gen_ai.response.id" not in et._EVAL_SCHEMA_FIELDS


def test_schema_hash_and_contract_fixture_locked():
    """schema hash 可复算；合同夹具锁定 n-eval-v1 与官方 GenAI schema 引用、
    内容 hash、Python OTel 1.45.1。"""
    expected_hash = hashlib.sha256(
        "\n".join(sorted(et._EVAL_SCHEMA_FIELDS)).encode("utf-8")
    ).hexdigest()
    assert et.EVAL_SCHEMA_HASH == expected_hash
    fixture_path = (
        Path(__file__).parent / "fixtures" / "evaluation_telemetry_n_eval_v1.json"
    )
    fixture = json.loads(fixture_path.read_text())
    assert fixture["schema_version"] == et.EVAL_SCHEMA_VERSION == "n-eval-v1"
    assert set(fixture["fields"]) == set(et._EVAL_SCHEMA_FIELDS)
    assert fixture["schema_hash"] == et.EVAL_SCHEMA_HASH
    assert fixture["event_name"] == et.EVAL_EVENT_NAME
    assert fixture["official"]["reference"] == et.GENAI_SEMCONV_REFERENCE
    assert fixture["official"]["content_hash"] == et.GENAI_SEMCONV_CONTENT_HASH
    canonical = json.dumps(
        fixture["official"]["content"], sort_keys=True, ensure_ascii=False
    )
    assert (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        == et.GENAI_SEMCONV_CONTENT_HASH
    )
    assert fixture["pinned_otel_sdk"] == pkg_version("opentelemetry-sdk")


# ---------------------------------------------------------------------------
# evaluation.run Trace + Link
# ---------------------------------------------------------------------------


def test_evaluation_run_trace_with_link_and_child_generations(monkeypatch):
    """独立 evaluation.run Trace，Link 指向业务目标 trace_id/span_id；
    Judge LLM 调用（作用域内启动的 span）为其子 generation。"""
    provider, exporter = _install_tracer(monkeypatch)
    try:
        with et.evaluation_run_scope(
            TARGET, metric_profile="kb.recall@1", mode="online"
        ) as run:
            with provider.get_tracer("judge").start_as_current_span(
                "judge.generation"
            ):
                pass
        spans = exporter.get_finished_spans()
        eval_span = next(s for s in spans if s.name == "evaluation.run")
        judge_span = next(s for s in spans if s.name == "judge.generation")
        run_trace_id = format(eval_span.context.trace_id, "032x")
        run_span_id = format(eval_span.context.span_id, "016x")
        assert run.trace_id == run_trace_id
        assert run.span_id == run_span_id
        assert run.is_valid
        # Link 指向真实业务目标身份
        link, = eval_span.links
        assert link.context.trace_id == int(TARGET.trace_id, 16)
        assert link.context.span_id == int(TARGET.span_id, 16)
        assert link.context.span_id != eval_span.context.span_id
        # Judge 调用是 evaluation.run 的子 generation
        assert judge_span.parent.span_id == eval_span.context.span_id
        attrs = dict(eval_span.attributes)
        assert attrs["nagent.evaluation.target.service"] == "n-kb"
        assert attrs["nagent.evaluation.target_link"] == "linked"
        assert attrs["nagent.evaluation.metric_profile"] == "kb.recall@1"
    finally:
        provider.shutdown()


def test_evaluation_run_without_valid_target_records_current_trace(monkeypatch):
    """无有效目标身份：无 Link、target_link=missing；结果 LogRecord 记录
    当前评估 Trace。"""
    log_provider, log_exporter = _install_log_provider(monkeypatch)
    provider, exporter = _install_tracer(monkeypatch)
    target = _make_target()
    try:
        with et.evaluation_run_scope(
            target, metric_profile="kb.recall@1", mode="online"
        ) as run:
            assert run.is_valid
            _emit_result(
                target,
                status="skipped",
                typed_value=None,
                skip_reason=SkipReason.evidence_missing,
                evaluation_trace=(run.trace_id, run.span_id),
            )
        eval_span = next(
            s for s in exporter.get_finished_spans() if s.name == "evaluation.run"
        )
        assert len(eval_span.links) == 0
        assert dict(eval_span.attributes)["nagent.evaluation.target_link"] == (
            "missing"
        )
        record, = _records(log_exporter)
        assert record.trace_id == int(run.trace_id, 16)
        assert record.span_id == int(run.span_id, 16)
        assert dict(record.attributes)["nagent.evaluation.target_link"] == "missing"
    finally:
        provider.shutdown()


def test_finished_business_span_is_never_modified(monkeypatch):
    """异步评分不修改已结束的业务 span（属性集合不变）。"""
    provider, exporter = _install_tracer(monkeypatch)
    try:
        with provider.get_tracer("biz").start_as_current_span(
            "kb.retrieval"
        ) as business:
            business.set_attribute("nagent.evaluation.retrieval_id", "retr-1")
            business.set_attribute("http.response.status_code", 200)
            business_context = business.get_span_context()
        before_attrs = dict(business.attributes)
        before_status = business.status
        with et.evaluation_run_scope(TARGET) as run:
            _emit_result(TARGET, evaluation_trace=(run.trace_id, run.span_id))
        assert dict(business.attributes) == before_attrs
        assert business.status == before_status
        assert business.context.trace_id == business_context.trace_id
        spans = exporter.get_finished_spans()
        assert [s.name for s in spans if s.name != "evaluation.run"] == [
            "kb.retrieval"
        ]
        eval_span = next(s for s in spans if s.name == "evaluation.run")
        # 评估 Trace 独立于业务 Trace（Link 关联而非父子改写）
        assert eval_span.context.trace_id != business.context.trace_id
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# ObservableGauge（24h 窗口 / producer 累计）
# ---------------------------------------------------------------------------


def test_window_and_producer_gauges_values_and_labels(monkeypatch):
    provider, reader = _install_meter(monkeypatch)
    try:
        registry = et.MetricProfileRegistry()
        assert registry.register("kb.recall", "1") == "kb.recall@1"
        holder = et.EvaluationSnapshotHolder()
        holder.update(
            window_rows=[dict(_WINDOW_ROW)],
            producer={
                "eligible": 10,
                "selected": 8,
                "captured": 7,
                "capture_error": 1,
                "queue_full": 0,
            },
        )
        et.EvaluationGauges(snapshot=holder, registry=registry)
        data = reader.get_metrics_data()
        metrics = {
            m.name: m
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
        }
        allowed_label_keys = {
            "target.service", "metric_profile", "mode", "status",
            "sampling_cohort",
        }
        window_values = {}
        for measure in (
            "succeeded",
            "skipped",
            "error",
            "unlinked",
            "numeric_sum",
            "numeric_count",
            "boolean_pass",
            "boolean_count",
        ):
            name = f"nagent.evaluation.window.{measure}"
            metric = metrics[name]
            point, = metric.data.data_points
            window_values[measure] = point.value
            assert set(point.attributes) <= allowed_label_keys, name
            assert dict(point.attributes) == {
                "target.service": "n-kb",
                "metric_profile": "kb.recall@1",
                "mode": "online",
                "sampling_cohort": "random",
                "status": (
                    "succeeded"
                    if measure
                    in {
                        "numeric_sum",
                        "numeric_count",
                        "boolean_pass",
                        "boolean_count",
                    }
                    else measure
                ),
            }, name
        assert window_values == {
            "succeeded": 3,
            "skipped": 1,
            "error": 2,
            "unlinked": 1,
            "numeric_sum": 2.5,
            "numeric_count": 3,
            "boolean_pass": 0,
            "boolean_count": 0,
        }
        for measure, expected in (
            ("eligible", 10),
            ("selected", 8),
            ("captured", 7),
            ("capture_error", 1),
            ("queue_full", 0),
        ):
            metric = metrics[f"nagent.evaluation.producer.{measure}"]
            point, = metric.data.data_points
            assert point.value == expected
            # 累计计数不做质量分组，无标签
            assert dict(point.attributes) == {}
    finally:
        provider.shutdown()


def test_window_gauges_separate_sampling_cohort(monkeypatch):
    """#24 cohort 分离：random/failure/空串各成独立数据点，故障定向
    系列不与随机系列混算（spec 7.1/AC17）。"""
    provider, reader = _install_meter(monkeypatch)
    try:
        registry = et.MetricProfileRegistry()
        registry.register("kb.recall", "1")
        holder = et.EvaluationSnapshotHolder()
        holder.update(
            window_rows=[
                {**_WINDOW_ROW, "sampling_cohort": "random"},
                {**_WINDOW_ROW, "sampling_cohort": "failure"},
                {**_WINDOW_ROW, "sampling_cohort": ""},
            ],
            producer={},
        )
        et.EvaluationGauges(snapshot=holder, registry=registry)
        data = reader.get_metrics_data()
        metric = next(
            m
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "nagent.evaluation.window.succeeded"
        )
        by_cohort = {
            dict(p.attributes)["sampling_cohort"]: p.value
            for p in metric.data.data_points
        }
        assert by_cohort == {"random": 3.0, "failure": 3.0, "other": 3.0}
    finally:
        provider.shutdown()


def test_forbidden_identifiers_never_appear_in_metric_labels(monkeypatch):
    """dataset_id/case_id/result_id/run_id/session_id/trace_id/模型名/query/
    KB 文档 ID 禁止做指标标签（哨兵）。"""
    provider, reader = _install_meter(monkeypatch)
    try:
        registry = et.MetricProfileRegistry()
        registry.register("kb.recall", "1")
        holder = et.EvaluationSnapshotHolder()
        forbidden_row = {
            **_WINDOW_ROW,
            "result_id": "result-SENTINEL01",
            "dataset_id": "dataset-SENTINEL01",
            "case_id": "case-SENTINEL01",
            "job_id": "job-SENTINEL01",
            "run_id": "run-SENTINEL01",
            "session_id": "session-SENTINEL01",
            "trace_id": "trace-SENTINEL01",
            "model_name": "model-SENTINEL01",
            "query": "query-SENTINEL01",
            "document_id": "doc-SENTINEL01",
        }
        holder.update(window_rows=[forbidden_row], producer={})
        et.EvaluationGauges(snapshot=holder, registry=registry)
        data = reader.get_metrics_data()
        serialized = json.dumps(
            [
                {
                    "name": m.name,
                    "points": [
                        {
                            "value": p.value,
                            "labels": {
                                k: str(v) for k, v in p.attributes.items()
                            },
                        }
                        for p in m.data.data_points
                    ],
                }
                for rm in data.resource_metrics
                for sm in rm.scope_metrics
                for m in sm.metrics
            ]
        )
        assert "SENTINEL01" not in serialized
    finally:
        provider.shutdown()


def test_metric_profile_registry_cap_and_exclusion(monkeypatch):
    """metric_profile 来自 <=16 个活跃定义版本注册表；超额注册被拒绝；
    未注册 profile 的分组不导出；显式停用后不再导出。"""
    registry = et.MetricProfileRegistry()
    profiles = [registry.register(f"def-{i}", "v1") for i in range(16)]
    assert all(p is not None for p in profiles)
    assert registry.register("def-overflow", "v1") is None
    assert registry.deactivate("def-0@v1")
    assert registry.register("def-overflow", "v1") == "def-overflow@v1"

    provider, reader = _install_meter(monkeypatch)
    try:
        holder = et.EvaluationSnapshotHolder()
        holder.update(
            window_rows=[
                {**_WINDOW_ROW, "definition_id": "def-1", "definition_version": "v1"},
                {
                    **_WINDOW_ROW,
                    "definition_id": "def-unregistered",
                    "definition_version": "v1",
                },
                {
                    **_WINDOW_ROW,
                    "definition_id": "def-0",
                    "definition_version": "v1",
                },
            ],
            producer={},
        )
        et.EvaluationGauges(snapshot=holder, registry=registry)
        data = reader.get_metrics_data()
        label_values = {
            str(v)
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            for p in m.data.data_points
            for k, v in p.attributes.items()
            if k == "metric_profile"
        }
        assert "def-1@v1" in label_values
        assert "def-unregistered@v1" not in label_values
        assert "def-0@v1" not in label_values
    finally:
        provider.shutdown()


def test_refresh_from_store_updates_snapshot():
    """gauge 后台刷新只读 store（async 查询，window_start 传 datetime 由
    store 归一化），回调读内存快照；快照读取返回行级副本。"""

    class _FakeStore:
        def __init__(self):
            self.window_calls = 0
            self.window_start = None

        async def query_window_snapshot(self, *, window_start):
            self.window_calls += 1
            self.window_start = window_start
            return [dict(_WINDOW_ROW)]

        async def query_producer_snapshot(self):
            return {
                "eligible": 5,
                "selected": 4,
                "captured": 4,
                "capture_error": 0,
                "queue_full": 0,
            }

    store = _FakeStore()
    holder = et.EvaluationSnapshotHolder()
    # 非 UTC 时区调用方：传 datetime，由 store 归一化（此处断言传入的
    # 是 aware datetime 且 UTC 语义正确）
    tz_plus8 = timezone(timedelta(hours=8))
    asyncio.run(
        et.refresh_gauges_from_store(
            holder, store, now=datetime(2026, 11, 11, 11, 0, tzinfo=tz_plus8)
        )
    )
    assert store.window_calls == 1
    # now - 24h 窗口起点，时区语义保持（store 侧负责归一化为 UTC ISO）
    assert store.window_start == datetime(2026, 11, 10, 11, 0, tzinfo=tz_plus8)
    assert store.window_start.astimezone(timezone.utc) == datetime(
        2026, 11, 10, 3, 0, tzinfo=timezone.utc
    )
    snapshot = holder.get()
    assert snapshot["window"][0]["target_service"] == "n-kb"
    assert snapshot["producer"]["eligible"] == 5
    assert snapshot["refresh_failures"] == 0
    # 行级拷贝：调用方篡改副本不影响 Gauge 数据源
    snapshot["window"][0]["target_service"] = "tampered"
    snapshot["producer"]["eligible"] = 999
    fresh = holder.get()
    assert fresh["window"][0]["target_service"] == "n-kb"
    assert fresh["producer"]["eligible"] == 5


def test_producer_only_update_preserves_window_rows():
    """producer-only 合并更新（main._evaluation_gauge_refresh_loop 的
    capture_counters 合并路径）不得清空 window 快照：T20 接线曾因 update
    默认 window_rows=() 无条件覆盖，每个刷新周期清空窗口行，window
    gauge 导出恒空（Prometheus 无 window.* 序列）。清除语义仅由显式
    传入 window_rows 表达。"""
    holder = et.EvaluationSnapshotHolder()
    holder.update(window_rows=[dict(_WINDOW_ROW)], producer={"eligible": 5})
    # producer-only 更新（刷新循环合并本地捕获计数）：window 保持
    holder.update(producer={"eligible": 9})
    snapshot = holder.get()
    assert snapshot["window"][0]["target_service"] == "n-kb"
    assert snapshot["producer"]["eligible"] == 9
    # 显式传空列表仍是清空（store 查出空窗口的真实语义）
    holder.update(window_rows=[], producer={"eligible": 9})
    assert holder.get()["window"] == ()
    assert holder.get()["producer"]["eligible"] == 9


def test_refresh_failure_isolation_keeps_last_good_snapshot():
    """store 查询持续失败：不向调用方抛错，快照保持上一次成功值，
    失败信号经 refresh_failures 计数可观测。"""

    class _BrokenStore:
        async def query_window_snapshot(self, *, window_start):
            raise RuntimeError("db down")

        async def query_producer_snapshot(self):
            raise RuntimeError("db down")

    holder = et.EvaluationSnapshotHolder()
    holder.update(window_rows=[dict(_WINDOW_ROW)], producer={"eligible": 3})
    broken = _BrokenStore()
    asyncio.run(et.refresh_gauges_from_store(holder, broken))
    snapshot = holder.get()
    assert snapshot["window"][0]["target_service"] == "n-kb"
    assert snapshot["producer"]["eligible"] == 3
    assert snapshot["refresh_failures"] == 1
    asyncio.run(et.refresh_gauges_from_store(holder, broken))
    assert holder.get()["refresh_failures"] == 2
    assert holder.get()["producer"]["eligible"] == 3


def test_malformed_window_rows_fall_back_to_zero(monkeypatch):
    """store 行计数畸形（None/非数值）时 _window_value 收敛 0.0，
    Gauge 导出不崩溃、不产出 NaN。"""
    provider, reader = _install_meter(monkeypatch)
    try:
        registry = et.MetricProfileRegistry()
        registry.register("kb.recall", "1")
        holder = et.EvaluationSnapshotHolder()
        holder.update(
            window_rows=[
                {
                    "target_service": "n-kb",
                    "definition_id": "kb.recall",
                    "definition_version": "1",
                    "mode": "online",
                    "succeeded_count": None,
                    "skipped_count": "many",
                    "error_count": True,
                    "unlinked_count": float("nan"),
                    "numeric_sum": None,
                    "numeric_count": None,
                    "boolean_pass": None,
                    "boolean_count": None,
                }
            ],
            producer={"eligible": None, "selected": 3},
        )
        et.EvaluationGauges(snapshot=holder, registry=registry)
        data = reader.get_metrics_data()
        by_name = {}
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    by_name[m.name] = [p.value for p in m.data.data_points]
        # 全部畸形计数收敛 0.0（None/字符串/bool/NaN 一律不产出非有限值）
        for measure in (
            "succeeded",
            "skipped",
            "error",
            "unlinked",
            "numeric_sum",
            "numeric_count",
            "boolean_pass",
            "boolean_count",
        ):
            assert by_name[f"nagent.evaluation.window.{measure}"] == [0.0]
        # producer：None 计数不出序列（SDK 省略空回调 metric），合法值正常导出
        assert "nagent.evaluation.producer.eligible" not in by_name
        assert by_name["nagent.evaluation.producer.selected"] == [3.0]
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# worker / 投递常规指标
# ---------------------------------------------------------------------------


def test_worker_and_delivery_metrics(monkeypatch):
    provider, reader = _install_meter(monkeypatch)
    try:
        monkeypatch.setattr(et, "_instrument_cache", {})
        et.record_worker_call(1.5, "succeeded")
        et.record_worker_call(2.0, "error")
        et.record_worker_call(0.5, "mystery")  # 未知 outcome 收敛 other
        et.record_delivery_failure("langfuse", "network")
        et.record_delivery_failure("mystery-sink", "mystery-error")
        data = reader.get_metrics_data()
        metrics = {
            m.name: m
            for rm in data.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
        }
        calls = metrics["nagent.evaluation.worker.calls"]
        outcomes = {
            dict(p.attributes).get("nagent.evaluation.outcome"): p.value
            for p in calls.data.data_points
        }
        assert outcomes == {"succeeded": 1, "error": 1, "other": 1}
        assert calls.unit == "{call}"
        duration = metrics["nagent.evaluation.worker.duration"]
        assert duration.unit == "s"
        assert sum(p.count for p in duration.data.data_points) == 3
        failures = metrics["nagent.evaluation.delivery.failures"]
        points = {
            (
                dict(p.attributes).get("nagent.evaluation.sink"),
                dict(p.attributes).get("error.type"),
            ): p.value
            for p in failures.data.data_points
        }
        assert points == {("langfuse", "network"): 1, ("other", "other"): 1}
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# OTel 禁用路径
# ---------------------------------------------------------------------------


def test_emit_rejects_blank_metric_name(monkeypatch):
    """spec 12.1：gen_ai.evaluation.name 必填——空/空白/非字符串名拒绝。"""
    _log_provider, log_exporter = _install_log_provider(monkeypatch)
    for bad in ("", "   ", "\t\n", None):
        with pytest.raises(ValueError):
            _emit_result(TARGET, metric_name=bad)
    assert _records(log_exporter) == []


def test_real_proxy_provider_emits_noop_without_get_logger_patch(monkeypatch):
    """真实 api 级 ProxyLoggerProvider（未注册 SDK provider）判定为禁用：
    复位全局 provider 槽（非 monkeypatch get_logger），emit 返回 noop。

    api 内部 ``opentelemetry._logs._internal.ProxyLoggerProvider`` 不在
    公开导出面（上游私有布局耦合，随 opentelemetry 1.45.1 钉住，见
    get_logger docstring）；直接构造测试以 importlib.metadata 版本哨兵
    保护，上游重命名时仅此段跳过/失败，不静默改变判定语义。"""
    monkeypatch.delenv("OTEL_PYTHON_LOGGER_PROVIDER", raising=False)
    monkeypatch.setattr(
        "opentelemetry._logs._internal._LOGGER_PROVIDER", None
    )
    from opentelemetry._logs import get_logger_provider

    # 真实 proxy provider 在场（未注册任何 SDK provider）
    assert type(get_logger_provider()).__name__.startswith("Proxy")
    target = _make_target()
    assert (
        et.emit_result(
            result_id="res-1",
            metric_name="kb.recall",
            definition_id="kb.recall",
            definition_version="1",
            status="skipped",
            skip_reason=SkipReason.evidence_missing,
            target=target,
        )
        == "noop"
    )
    if pkg_version("opentelemetry-api") == "1.45.1":
        from opentelemetry._logs._internal import ProxyLoggerProvider

        monkeypatch.setattr(
            "opentelemetry._logs._internal._LOGGER_PROVIDER",
            ProxyLoggerProvider(),
        )
        assert et.get_logger() is None
        assert (
            et.emit_result(
                result_id="res-1",
                metric_name="kb.recall",
                definition_id="kb.recall",
                definition_version="1",
                status="skipped",
                skip_reason=SkipReason.evidence_missing,
                target=target,
            )
            == "noop"
        )


class _ProxyTracer:
    """模拟禁用路径：start_span 返回无效非记录 span。"""

    def start_span(self, *args, **kwargs):
        return otel_trace.NonRecordingSpan(otel_trace.INVALID_SPAN_CONTEXT)


class _ProxyMeter:
    """模拟禁用路径：所有 instrument 创建均为 no-op。"""

    def create_observable_gauge(self, *args, **kwargs):
        return None

    def create_counter(self, *args, **kwargs):
        return _NoopInstrument()

    def create_histogram(self, *args, **kwargs):
        return _NoopInstrument()


class _NoopInstrument:
    def add(self, *args, **kwargs):
        pass

    def record(self, *args, **kwargs):
        pass


def test_otel_disabled_no_publish_no_error(monkeypatch):
    """OTel 禁用：emit 返回 noop、作用域返回无效句柄，不抛错不发布。"""
    monkeypatch.setattr(et, "get_logger", lambda: None)
    monkeypatch.setattr(et, "get_tracer", lambda: _ProxyTracer())
    monkeypatch.setattr(ts, "get_tracer", lambda: _ProxyTracer())
    monkeypatch.setattr(et, "get_meter", lambda: _ProxyMeter())
    monkeypatch.setattr(ts, "get_meter", lambda: _ProxyMeter())
    monkeypatch.setattr(et, "_instrument_cache", {})
    with et.evaluation_run_scope(TARGET) as run:
        assert not run.is_valid
        assert run.trace_id is None
        assert run.span_id is None
    assert _emit_result(TARGET) == "noop"
    # gauge 注册在禁用路径同样无异常
    holder = et.EvaluationSnapshotHolder()
    holder.update(window_rows=[dict(_WINDOW_ROW)], producer={"eligible": 1})
    et.EvaluationGauges(snapshot=holder, registry=et.MetricProfileRegistry())
    et.record_worker_call(0.1, "succeeded")
