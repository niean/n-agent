# tests/application/test_telemetry_cost.py
"""T4 S1: OTel 成本数据模型（Span 属性 + Counter + 完成日志）测试。

覆盖计划 T4 要点：
- 新属性键在 telemetry_contract 登记（OBJECT_ATTRIBUTES['model'] /
  EXECUTION_ATTRIBUTE_KEYS / _attribute_types）与类型守卫。
- 三个新 Instrument（nagent.model.cost / cost_components / cost_attempts）
  在 semantics 与 contract 双侧登记，维度与单位符合计划表。
- estimator 结果到 Span/Log 同键成本事实的投影：estimated 全量形态、
  unknown 无金额形态、非法形态整组省略。
- Counter 维度与 add 语义：estimated 显式零也 add(0)、unknown 只加 attempts、
  计费身份维度经安全归一、reason 白名单。
- ChatAttempt 桶级有效性追踪：非法标记曾非法、缺席不擦除、合法快照恢复；
  OpenAI cached>prompt 在 telemetry 层判 invalid；Anthropic raw 通道 presence
  保留的直接单测；TTL 拆分事实经 raw_usage 透传。
- finalization 恰一次（finalized 防重入）。
- 采样无关性：真实 SDK 的 ALWAYS_OFF sampler + InMemoryMetricReader 验证
  未采样仍出 Metric；ALWAYS_ON + 内存 Span exporter 核对三信号同源。
"""
from __future__ import annotations

import pytest

from app.application import telemetry_contract as tc
from app.application import telemetry_semantics as ts

# ---------------------------------------------------------------------------
# 测试夹具（与 test_telemetry_semantics 同款 fake，本地定义保持自包含）
# ---------------------------------------------------------------------------


class FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.status = None
        self.ended = 0

    def get_span_context(self):
        return ts.trace.SpanContext(
            trace_id=0x00000000000000000000000000000001,
            span_id=0x0000000000000002,
            is_remote=False,
        )

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status

    def end(self):
        self.ended += 1

    def is_recording(self):
        return True

    def add_event(self, name, attributes=None, timestamp=None):
        self.last_event = (name, dict(attributes or {}))


class FakeTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, kind=None, **_kwargs):
        span = FakeSpan(name)
        self.spans.append(span)
        return span


class FakeCounter:
    def __init__(self):
        self.records = []

    def add(self, value, attrs):
        self.records.append((value, dict(attrs)))


class FakeHistogram:
    def record(self, value, attrs):
        raise AssertionError("cost instruments must be counters")


class FakeMeter:
    def __init__(self):
        self.counters = {}

    def create_counter(self, name, unit=None, **_kwargs):
        return self.counters.setdefault(name, FakeCounter())

    def create_histogram(self, name, unit=None, **_kwargs):
        return self.counters.setdefault(name, FakeHistogram())


# 统一向量 B（OpenAI）：Ti=100、To=20、Tic=50 => 0.00041
_ESTIMATED_RESULT = {
    "amount_usd": 0.00041,
    "status": "estimated",
    "pricing_version": "test-v1@" + "a" * 64,
    "price_id": "p-openai-test",
    "cost_decimal": "0.00041",
    "components": {"input": 0.0002, "output": 0.0002, "cache_read": 0.00001},
    "currency": "USD",
    "reason": None,
    "billing_provider": "openai",
    "billing_model": "gpt-test",
}

_COST_AMOUNT_KEYS = (
    "nagent.model.cost_usd",
    "nagent.model.cost_decimal",
    "nagent.model.cost_input_usd",
    "nagent.model.cost_output_usd",
    "nagent.model.cost_cache_read_usd",
    "nagent.model.cost_cache_write_usd",
)


# ---------------------------------------------------------------------------
# 契约登记
# ---------------------------------------------------------------------------


class TestContractRegistration:
    def test_model_cost_attribute_keys_registered(self):
        fields = (
            "cost_status", "cost_reason", "cost_currency", "cost_decimal",
            "cost_usd", "cost_input_usd", "cost_output_usd",
            "cost_cache_read_usd", "cost_cache_write_usd",
            "pricing_version", "price_id", "billing_provider", "billing_model",
        )
        for field in fields:
            assert f"nagent.model.{field}" in tc.EXECUTION_ATTRIBUTE_KEYS, field
            assert field in tc.OBJECT_ATTRIBUTES["model"], field

    def test_model_cost_attribute_types(self):
        for field in ("cost_status", "cost_reason", "cost_currency", "cost_decimal",
                      "pricing_version", "price_id", "billing_provider", "billing_model"):
            assert tc.ATTRIBUTE_TYPES[f"nagent.model.{field}"] == "string", field
        for field in ("cost_usd", "cost_input_usd", "cost_output_usd",
                      "cost_cache_read_usd", "cost_cache_write_usd"):
            assert tc.ATTRIBUTE_TYPES[f"nagent.model.{field}"] == "number", field

    def test_wrong_typed_cost_values_rejected_by_projection(self):
        attrs = tc.project_execution_attributes({
            "operation": "chat",
            "nagent.model.cost_reason": 7,
            "nagent.model.cost_decimal": 0.5,
            "nagent.model.cost_usd": "0.1",
            "nagent.model.billing_provider": 3,
            "nagent.model.cost_status": "estimated",
        })
        assert attrs["nagent.model.cost_status"] == "estimated"
        for key in ("nagent.model.cost_reason", "nagent.model.cost_decimal",
                    "nagent.model.cost_usd", "nagent.model.billing_provider"):
            assert key not in attrs

    def test_cost_instruments_registered_in_contract(self):
        for name in ("nagent.model.cost", "nagent.model.cost_components",
                     "nagent.model.cost_attempts"):
            assert name in tc._instruments["model"], name
            assert name in ts.INSTRUMENTS, name

    def test_cost_instruments_match_spec_table(self):
        base_dims = {"gen_ai.operation.name", "nagent.model.billing_provider",
                     "nagent.model.billing_model"}
        expected = {
            "nagent.model.cost": ("counter", "USD", base_dims),
            "nagent.model.cost_components": ("counter", "USD", base_dims | {"nagent.model.cost_component"}),
            "nagent.model.cost_attempts": ("counter", "{attempt}", base_dims | {"nagent.model.cost_status", "nagent.model.cost_reason"}),
        }
        for name, (kind, unit, dims) in expected.items():
            inst = ts.INSTRUMENTS[name]
            assert inst.kind == kind, name
            assert inst.unit == unit, name
            assert set(inst.allowed_dimensions) == dims, name

    def test_instrument_constants_exposed(self):
        assert ts.INSTRUMENT_MODEL_COST == "nagent.model.cost"
        assert ts.INSTRUMENT_MODEL_COST_COMPONENTS == "nagent.model.cost_components"
        assert ts.INSTRUMENT_MODEL_COST_ATTEMPTS == "nagent.model.cost_attempts"
        assert ts.NAGENT_MODEL_BILLING_PROVIDER == "nagent.model.billing_provider"
        assert ts.NAGENT_MODEL_BILLING_MODEL == "nagent.model.billing_model"
        assert ts.NAGENT_MODEL_COST_COMPONENT == "nagent.model.cost_component"


# ---------------------------------------------------------------------------
# 白名单与哨兵
# ---------------------------------------------------------------------------


class TestWhitelistAndSentinels:
    def test_foreign_and_unknown_metadata_rejected(self):
        attrs = tc.project_execution_attributes({
            "operation": "chat",
            "langfuse.observation.model.name": "gpt-4o",
            "langfuse.metadata.injected": "payload",
            "metadata": {"evil": 1},
            "nagent.model.free_text": "injected",
            "nagent.model.cost_status": "estimated",
        })
        assert attrs["nagent.model.cost_status"] == "estimated"
        for key in ("langfuse.observation.model.name", "langfuse.metadata.injected",
                    "metadata", "nagent.model.free_text"):
            assert key not in attrs

    def test_new_keys_survive_log_sanitizer(self):
        from app.infrastructure.observability.sanitizer import LogSanitizer
        sanitizer = LogSanitizer()
        for value in ("0.00041", "p-openai-test", "test-v1@" + "a" * 64, "USD", "openai"):
            assert sanitizer.sanitize(value) == value

    def test_new_keys_inside_log_whitelist(self):
        from app.application.telemetry_contract import EXECUTION_ATTRIBUTE_KEYS, CONTENT_ATTRIBUTE_KEYS
        allowed = EXECUTION_ATTRIBUTE_KEYS - CONTENT_ATTRIBUTE_KEYS
        for field in ("cost_status", "cost_reason", "cost_currency", "cost_decimal",
                      "cost_input_usd", "pricing_version", "price_id",
                      "billing_provider", "billing_model"):
            assert f"nagent.model.{field}" in allowed, field

    def test_billing_identity_projection_is_bounded(self):
        assert ts.map_billing_provider("openai") == "openai"
        assert ts.map_billing_provider("anthropic") == "anthropic"
        assert ts.map_billing_provider("ollama") == "ollama"
        assert ts.map_billing_provider("not-a-provider") == "other"
        assert ts.map_billing_provider(None) == "other"
        assert ts.map_billing_provider("m" * 500) == "other"
        assert ts.map_billing_model("gpt-test") == "gpt-test"
        assert ts.map_billing_model("  gpt-test ") == "gpt-test"
        assert ts.map_billing_model("") == "other"
        assert ts.map_billing_model(123) == "other"
        assert ts.map_billing_model("m" * 500) == "other"


# ---------------------------------------------------------------------------
# estimator 结果 -> 成本事实投影
# ---------------------------------------------------------------------------


class TestCostFactsProjection:
    def test_estimated_result_projects_full_fact_set(self):
        facts = ts._cost_facts_from_result(_ESTIMATED_RESULT)
        assert facts["nagent.model.cost_status"] == "estimated"
        assert facts["nagent.model.cost_usd"] == 0.00041
        assert facts["nagent.model.cost_decimal"] == "0.00041"
        assert facts["nagent.model.cost_currency"] == "USD"
        assert facts["nagent.model.pricing_version"] == _ESTIMATED_RESULT["pricing_version"]
        assert facts["nagent.model.price_id"] == "p-openai-test"
        assert facts["nagent.model.cost_input_usd"] == 0.0002
        assert facts["nagent.model.cost_output_usd"] == 0.0002
        assert facts["nagent.model.cost_cache_read_usd"] == 0.00001
        assert "nagent.model.cost_cache_write_usd" not in facts  # 不适用桶省略
        assert "nagent.model.cost_reason" not in facts  # estimated 省略
        assert facts["nagent.model.billing_provider"] == "openai"
        assert facts["nagent.model.billing_model"] == "gpt-test"

    def test_estimated_explicit_zero_is_valid(self):
        result = {**_ESTIMATED_RESULT, "amount_usd": 0.0, "cost_decimal": "0.0",
                  "components": {"input": 0.0}}
        facts = ts._cost_facts_from_result(result)
        assert facts["nagent.model.cost_usd"] == 0.0
        assert facts["nagent.model.cost_input_usd"] == 0.0

    @pytest.mark.parametrize("override", [
        {"amount_usd": float("nan")},
        {"amount_usd": -1},
        {"amount_usd": True},
        {"amount_usd": None},
        {"pricing_version": ""},
        {"pricing_version": None},
    ])
    def test_invalid_estimated_shape_projects_nothing(self, override):
        assert ts._cost_facts_from_result({**_ESTIMATED_RESULT, **override}) == {}

    def test_unknown_result_carries_status_reason_and_no_amounts(self):
        result = {"status": "unknown", "reason": "price_not_found", "currency": "USD",
                  "pricing_version": _ESTIMATED_RESULT["pricing_version"],
                  "price_id": None, "amount_usd": 0.0,
                  "billing_provider": "openai", "billing_model": "gpt-test"}
        facts = ts._cost_facts_from_result(result)
        assert facts["nagent.model.cost_status"] == "unknown"
        assert facts["nagent.model.cost_reason"] == "price_not_found"
        assert facts["nagent.model.cost_currency"] == "USD"
        assert facts["nagent.model.pricing_version"] == _ESTIMATED_RESULT["pricing_version"]
        assert facts["nagent.model.billing_provider"] == "openai"
        assert facts["nagent.model.billing_model"] == "gpt-test"
        for key in _COST_AMOUNT_KEYS:
            assert key not in facts

    def test_unknown_pricing_unavailable_omits_version(self):
        facts = ts._cost_facts_from_result(
            {"status": "unknown", "reason": "pricing_unavailable", "pricing_version": None})
        assert facts["nagent.model.cost_status"] == "unknown"
        assert "nagent.model.pricing_version" not in facts

    def test_unknown_price_id_retained_only_when_uniquely_resolved(self):
        """spec：price_id unknown 仅在已唯一定位价格项时保留，否则省略
        （price_id 不是自动推价材料——unknown 不投影 model.name）。"""
        resolved = ts._cost_facts_from_result({
            "status": "unknown", "reason": "amount_unrepresentable",
            "currency": "USD", "pricing_version": _ESTIMATED_RESULT["pricing_version"],
            "price_id": "p-openai-test",
            "billing_provider": "openai", "billing_model": "gpt-test",
        })
        assert resolved["nagent.model.price_id"] == "p-openai-test"
        assert resolved["nagent.model.pricing_version"] == _ESTIMATED_RESULT["pricing_version"]
        unresolved = ts._cost_facts_from_result({
            "status": "unknown", "reason": "price_not_found", "price_id": None})
        assert "nagent.model.price_id" not in unresolved
        assert "nagent.model.pricing_version" not in unresolved

    def test_unknown_reason_outside_vocabulary_omitted(self):
        facts = ts._cost_facts_from_result({"status": "unknown", "reason": "free text"})
        assert facts["nagent.model.cost_status"] == "unknown"
        assert "nagent.model.cost_reason" not in facts

    def test_unrecognized_status_projects_nothing(self):
        assert ts._cost_facts_from_result({"status": "weird"}) == {}
        assert ts._cost_facts_from_result({}) == {}


# ---------------------------------------------------------------------------
# Counter 维度与 add 语义
# ---------------------------------------------------------------------------


class TestRecordModelCost:
    @pytest.fixture
    def meter(self, monkeypatch):
        ts._instrument_cache.clear()
        fake = FakeMeter()
        monkeypatch.setattr(ts, "get_meter", lambda: fake)
        yield fake
        ts._instrument_cache.clear()

    def test_estimated_adds_total_components_and_attempts(self, meter):
        try:
            # 组合根登记的配置模型名在 Metric 维度保留真实值（白名单放行）
            assert ts.register_model_name("gpt-test") is True
            ts.record_model_cost("chat", {
                "status": "estimated", "amount_usd": 0.00041,
                "pricing_version": "test-v1@" + "a" * 64,
                "components": {"input": 0.0002, "output": 0.0002, "cache_read": 0.00001},
                "billing_provider": "openai", "billing_model": "gpt-test",
            })
        finally:
            ts.unregister_model_name("gpt-test")
        total = meter.counters[ts.INSTRUMENT_MODEL_COST]
        assert [v for v, _ in total.records] == [0.00041]
        _value, dims = total.records[0]
        assert dims == {
            "gen_ai.operation.name": "chat",
            "nagent.model.billing_provider": "openai",
            "nagent.model.billing_model": "gpt-test",
        }
        components = meter.counters[ts.INSTRUMENT_MODEL_COST_COMPONENTS]
        assert [(v, d["nagent.model.cost_component"]) for v, d in components.records] == [
            (0.0002, "input"), (0.0002, "output"), (0.00001, "cache_read"),
        ]
        attempts = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS]
        assert [v for v, _ in attempts.records] == [1]
        _value, dims = attempts.records[0]
        assert dims["nagent.model.cost_status"] == "estimated"
        assert dims["nagent.model.cost_reason"] == "none"

    def test_cost_counter_model_label_outside_whitelist_is_other(self, meter):
        """三个成本 Counter 的模型维度必须走 map_model_name 白名单
        （未登记 -> other），任意计费模型名不得直接成为指标标签；
        Span/metadata 的计费身份投影（map_billing_model）保留真实名称。"""
        result = {
            "status": "estimated", "amount_usd": 0.00041,
            "pricing_version": "test-v1@" + "a" * 64,
            "components": {"input": 0.0002},
            "billing_provider": "openai", "billing_model": "unlisted-model",
        }
        ts.record_model_cost("chat", result)
        total_dims = meter.counters[ts.INSTRUMENT_MODEL_COST].records[0][1]
        component_dims = meter.counters[ts.INSTRUMENT_MODEL_COST_COMPONENTS].records[0][1]
        attempt_dims = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS].records[0][1]
        for dims in (total_dims, component_dims, attempt_dims):
            assert dims["nagent.model.billing_model"] == "other"
        # Span/metadata 投影不白名单化：保留真实计费模型名
        assert ts._cost_facts_from_result(result)["nagent.model.billing_model"] == "unlisted-model"

    def test_cost_counter_model_label_passes_registered_name(self, meter):
        """登记过的模型名在成本 Counter 维度保留真实值（白名单放行路径）。"""
        try:
            assert ts.register_model_name("registered-model") is True
            ts.record_model_cost("chat", {
                "status": "unknown", "reason": "identity_unresolved",
                "billing_provider": "openai", "billing_model": "registered-model",
            })
            dims = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS].records[0][1]
            assert dims["nagent.model.billing_model"] == "registered-model"
        finally:
            ts.unregister_model_name("registered-model")

    def test_estimated_explicit_zero_still_adds(self, meter):
        ts.record_model_cost("chat", {
            "status": "estimated", "amount_usd": 0.0,
            "pricing_version": "test-v1@" + "a" * 64,
            "components": {"input": 0.0},
            "billing_provider": "openai", "billing_model": "gpt-test",
        })
        assert meter.counters[ts.INSTRUMENT_MODEL_COST].records[0][0] == 0.0
        assert meter.counters[ts.INSTRUMENT_MODEL_COST_COMPONENTS].records[0][0] == 0.0

    def test_unknown_adds_attempts_without_amounts(self, meter):
        ts.record_model_cost("chat", {
            "status": "unknown", "reason": "missing_usage",
            "billing_provider": "openai", "billing_model": "gpt-test",
        })
        assert ts.INSTRUMENT_MODEL_COST not in meter.counters
        assert ts.INSTRUMENT_MODEL_COST_COMPONENTS not in meter.counters
        _value, dims = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS].records[0]
        assert dims["nagent.model.cost_status"] == "unknown"
        assert dims["nagent.model.cost_reason"] == "missing_usage"

    def test_unknown_reason_outside_vocabulary_dropped(self, meter):
        ts.record_model_cost("chat", {"status": "unknown", "reason": "free text"})
        _value, dims = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS].records[0]
        assert "nagent.model.cost_reason" not in dims

    def test_billing_dims_collapsed_through_safe_mapping(self, meter):
        ts.record_model_cost("chat", {
            "status": "unknown", "reason": "identity_unresolved",
            "billing_provider": "evil-provider", "billing_model": "m" * 500,
        })
        _value, dims = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS].records[0]
        assert dims["nagent.model.billing_provider"] == "other"
        assert dims["nagent.model.billing_model"] == "other"

    def test_invalid_amounts_never_added_but_attempts_counted(self, meter):
        for bad in (float("nan"), -1, True, "0.1", None):
            ts.record_model_cost("chat", {
                "status": "estimated", "amount_usd": bad,
                "pricing_version": "test-v1@" + "a" * 64,
                "components": {"input": bad},
            })
        assert ts.INSTRUMENT_MODEL_COST not in meter.counters
        assert ts.INSTRUMENT_MODEL_COST_COMPONENTS not in meter.counters
        records = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS].records
        assert len(records) == 5
        # 畸形 estimated 降级 unknown 计数，金额不出口（amount 非法优先于版本）
        for _value, dims in records:
            assert dims["nagent.model.cost_status"] == "unknown"
            assert dims["nagent.model.cost_reason"] == "amount_unrepresentable"

    def test_malformed_estimated_missing_version_downgrades_to_unknown(self, meter):
        """畸形 estimated（缺 pricing_version）：Metric 侧降级 unknown 且计
        attempts（T1 顺序 pricing_unavailable 先于 amount_unrepresentable），
        任何金额都不出口。"""
        for result in ({"status": "estimated", "amount_usd": 0.5},
                       {"status": "estimated", "amount_usd": 0.5, "pricing_version": "  "}):
            ts.record_model_cost("chat", result)
        records = meter.counters[ts.INSTRUMENT_MODEL_COST_ATTEMPTS].records
        assert len(records) == 2
        for _value, dims in records:
            assert dims["nagent.model.cost_status"] == "unknown"
            assert dims["nagent.model.cost_reason"] == "pricing_unavailable"
        assert ts.INSTRUMENT_MODEL_COST not in meter.counters
        assert ts.INSTRUMENT_MODEL_COST_COMPONENTS not in meter.counters

    def test_non_dict_result_counts_nothing(self, meter):
        for result in (None, "estimated", 42):
            ts.record_model_cost("chat", result)
        assert not meter.counters


# ---------------------------------------------------------------------------
# ChatAttempt 成本生命周期
# ---------------------------------------------------------------------------


class TestChatAttemptCostLifecycle:
    @pytest.fixture
    def captured(self, monkeypatch):
        records = []
        monkeypatch.setattr(
            ts, "_record", lambda name, value, dims: records.append((name, value, dims))
        )
        tracer = FakeTracer()
        monkeypatch.setattr(ts, "get_tracer", lambda: tracer)
        return records, tracer

    def _telemetry(self, estimator):
        tel = ts.SemanticsTelemetry()
        tel.set_cost_estimator(estimator)
        return tel

    def test_estimated_form_on_span_event_and_counters(self, captured):
        records, tracer = captured
        calls = []

        def estimator(raw, model, provider, buckets, states):
            calls.append((raw, model, provider, buckets, states))
            return dict(_ESTIMATED_RESULT)

        with self._telemetry(estimator).begin_chat_attempt(
                model="gpt-test", stream=False, provider="openai") as attempt:
            attempt.set_raw_usage({"prompt_tokens": 150, "completion_tokens": 20,
                                   "prompt_tokens_details": {"cached_tokens": 50}})
            attempt.set_usage(100, 20)
            attempt.set_cache_usage(cache_read_tokens=50, cache_write_tokens=0)
        span = tracer.spans[0]
        attrs = span.attributes
        assert attrs["nagent.model.cost_status"] == "estimated"
        assert attrs["nagent.model.cost_usd"] == 0.00041
        assert attrs["nagent.model.cost_decimal"] == "0.00041"
        assert attrs["nagent.model.cost_currency"] == "USD"
        assert attrs["nagent.model.pricing_version"] == _ESTIMATED_RESULT["pricing_version"]
        assert attrs["nagent.model.price_id"] == "p-openai-test"
        assert attrs["nagent.model.cost_input_usd"] == 0.0002
        assert attrs["nagent.model.cost_cache_read_usd"] == 0.00001
        assert "nagent.model.cost_cache_write_usd" not in attrs
        assert attrs["nagent.model.billing_provider"] == "openai"
        assert attrs["nagent.model.billing_model"] == "gpt-test"
        # 完成事件与 Span 属性同键同值
        event_name, event_attrs = span.last_event
        assert event_name == "nagent.model.completed"
        for key, value in attrs.items():
            if key.startswith("nagent.model.cost") or key in (
                    "nagent.model.pricing_version", "nagent.model.price_id",
                    "nagent.model.billing_provider", "nagent.model.billing_model"):
                assert event_attrs[key] == value, key
        # Counter 恰好发一次
        names = [name for name, _, _ in records]
        assert names.count(ts.INSTRUMENT_MODEL_COST) == 1
        assert names.count(ts.INSTRUMENT_MODEL_COST_ATTEMPTS) == 1
        # 桶状态传给 estimator
        assert calls == [(
            {"prompt_tokens": 150, "completion_tokens": 20,
             "prompt_tokens_details": {"cached_tokens": 50}},
            "gpt-test", "openai", (100, 20, 50, 0),
            ("known", "known", "known", "known"),
        )]

    def test_unknown_form_omits_amounts_but_counts_attempt(self, captured):
        records, tracer = captured

        def estimator(raw, model, provider, buckets, states):
            return {"status": "unknown", "reason": "price_not_found",
                    "currency": "USD", "pricing_version": _ESTIMATED_RESULT["pricing_version"],
                    "price_id": None, "amount_usd": 0.0,
                    "billing_provider": "openai", "billing_model": "gpt-test"}

        with self._telemetry(estimator).begin_chat_attempt(
                model="gpt-test", stream=False, provider="openai") as attempt:
            attempt.set_usage(100, 20)
        attrs = tracer.spans[0].attributes
        assert attrs["nagent.model.cost_status"] == "unknown"
        assert attrs["nagent.model.cost_reason"] == "price_not_found"
        assert attrs["nagent.model.cost_currency"] == "USD"
        assert attrs["nagent.model.pricing_version"] == _ESTIMATED_RESULT["pricing_version"]
        for key in _COST_AMOUNT_KEYS:
            assert key not in attrs
        names = [name for name, _, _ in records]
        assert names.count(ts.INSTRUMENT_MODEL_COST_ATTEMPTS) == 1
        assert ts.INSTRUMENT_MODEL_COST not in names
        assert ts.INSTRUMENT_MODEL_COST_COMPONENTS not in names

    def test_unknown_pricing_unavailable_omits_version(self, captured):
        records, tracer = captured
        with self._telemetry(
                lambda *args: {"status": "unknown", "reason": "pricing_unavailable",
                               "pricing_version": None}).begin_chat_attempt(
                model="gpt-4o", stream=False) as attempt:
            attempt.set_usage(1, 2)
        attrs = tracer.spans[0].attributes
        assert attrs["nagent.model.cost_status"] == "unknown"
        assert attrs["nagent.model.cost_reason"] == "pricing_unavailable"
        assert "nagent.model.pricing_version" not in attrs

    def test_finalization_emits_exactly_once(self, captured):
        records, tracer = captured
        calls = []

        def estimator(raw, model, provider, buckets, states):
            calls.append(buckets)
            return dict(_ESTIMATED_RESULT)

        tel = self._telemetry(estimator)
        cm = tel.begin_chat_attempt(model="gpt-test", stream=False, provider="openai")
        attempt = cm.__enter__()
        attempt.set_usage(100, 20)
        cm.__exit__(None, None, None)
        cm.__exit__(None, None, None)  # 重入不得再次收口
        assert len(calls) == 1
        names = [name for name, _, _ in records]
        assert names.count(ts.INSTRUMENT_MODEL_COST_ATTEMPTS) == 1
        assert len(tracer.spans) == 1

    def test_no_estimator_produces_no_cost_output(self, captured):
        records, tracer = captured
        with ts.SemanticsTelemetry().begin_chat_attempt(model="gpt-4o", stream=False) as attempt:
            attempt.set_usage(1, 2)
        assert "nagent.model.cost_status" not in tracer.spans[0].attributes
        assert not [name for name, _, _ in records if name.startswith("nagent.model.cost")]

    def test_malformed_estimated_counts_attempt_but_omits_span_facts(self, captured):
        """畸形 estimated（缺 pricing_version）：Metric 仍计 attempts（降级
        unknown/pricing_unavailable），Span/Log 成本事实整组省略。"""
        records, tracer = captured
        with self._telemetry(
                lambda *args: {"status": "estimated", "amount_usd": 0.5}
                ).begin_chat_attempt(model="gpt-test", stream=False) as attempt:
            attempt.set_usage(1, 2)
        attrs = tracer.spans[0].attributes
        for key in _COST_AMOUNT_KEYS + ("nagent.model.cost_status", "nagent.model.pricing_version"):
            assert key not in attrs
        attempts = [d for name, _, d in records if name == ts.INSTRUMENT_MODEL_COST_ATTEMPTS]
        assert len(attempts) == 1
        assert attempts[0]["nagent.model.cost_status"] == "unknown"
        assert attempts[0]["nagent.model.cost_reason"] == "pricing_unavailable"
        assert ts.INSTRUMENT_MODEL_COST not in [name for name, _, _ in records]
        assert attempt._cost_facts is None  # Span/Log 投影被门禁

    def test_estimator_none_result_produces_no_cost_output(self, captured):
        records, tracer = captured
        with self._telemetry(lambda *args: None).begin_chat_attempt(
                model="gpt-4o", stream=False) as attempt:
            attempt.set_usage(1, 2)
        assert "nagent.model.cost_status" not in tracer.spans[0].attributes
        assert not [name for name, _, _ in records if name.startswith("nagent.model.cost")]

    def test_estimator_exception_isolated(self, captured):
        records, tracer = captured

        def boom(*args):
            raise RuntimeError("pricing unavailable")

        with pytest.raises(RuntimeError):
            with self._telemetry(boom).begin_chat_attempt(model="gpt-4o", stream=False) as attempt:
                attempt.set_usage(1, 2)
                raise RuntimeError("business failure")
        assert "nagent.model.cost_status" not in tracer.spans[0].attributes
        assert not [name for name, _, _ in records if name.startswith("nagent.model.cost")]

    def test_billing_identity_frozen_at_request_start_and_carried_to_estimator(
            self, captured):
        """请求启动（__enter__）时冻结受信计价身份并原样传递给 estimator：
        finalization 不再向身份源重查询（provider 切换并发隔离）。"""
        records, tracer = captured
        calls = []

        def estimator(raw, model, provider, buckets, states, billing_identity=None):
            calls.append(billing_identity)
            return dict(_ESTIMATED_RESULT)

        tel = ts.SemanticsTelemetry()
        tel.set_cost_estimator(estimator)
        tel.set_billing_identity_resolver(
            lambda model, provider: ("openai", "gpt-test"))
        with tel.begin_chat_attempt(model="gpt-test", stream=False, provider="openai"):
            pass
        assert calls == [("openai", "gpt-test")]

    def test_unresolved_identity_at_start_not_retroactively_resolved(self, captured):
        """启动时身份未解析（resolver 返回 None）：即使此后身份源可解析，
        finalization 收到的仍是冻结的 None，不追溯重解析。"""
        records, tracer = captured
        calls = []

        def estimator(raw, model, provider, buckets, states, billing_identity=None):
            calls.append(billing_identity)
            return dict(_ESTIMATED_RESULT)

        tel = ts.SemanticsTelemetry()
        tel.set_cost_estimator(estimator)
        tel.set_billing_identity_resolver(lambda model, provider: None)
        with tel.begin_chat_attempt(model="gpt-test", stream=False, provider="openai"):
            pass
        assert calls == [None]

    def test_identity_resolver_failure_freezes_unresolved(self, captured):
        """身份解析异常：冻结为 None（观测侧隔离失败），不影响业务请求。"""
        records, tracer = captured
        calls = []

        def estimator(raw, model, provider, buckets, states, billing_identity=None):
            calls.append(billing_identity)
            return dict(_ESTIMATED_RESULT)

        def boom(model, provider):
            raise RuntimeError("identity source down")

        tel = ts.SemanticsTelemetry()
        tel.set_cost_estimator(estimator)
        tel.set_billing_identity_resolver(boom)
        with tel.begin_chat_attempt(model="gpt-test", stream=False, provider="openai"):
            pass
        assert calls == [None]

    def test_metric_survives_always_off_sampling(self, monkeypatch):
        """未采样（ALWAYS_OFF）下 Span 不记录，但 Counter 仍必须产出。"""
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[reader])
        trace_provider = TracerProvider(sampler=ALWAYS_OFF)
        monkeypatch.setattr(ts, "get_tracer", lambda: trace_provider.get_tracer(ts._TRACER_NAME))
        monkeypatch.setattr(ts, "get_meter", lambda: meter_provider.get_meter(ts._METER_NAME))
        monkeypatch.setattr(ts, "_instrument_cache", {})
        tel = ts.SemanticsTelemetry()
        tel.set_cost_estimator(lambda *args: dict(_ESTIMATED_RESULT))
        try:
            with tel.begin_chat_attempt(model="gpt-test", stream=False, provider="openai") as attempt:
                attempt.set_usage(100, 20)
            names = {
                metric.name
                for resource in reader.get_metrics_data().resource_metrics
                for scope in resource.scope_metrics
                for metric in scope.metrics
            }
            assert {"nagent.model.cost", "nagent.model.cost_components",
                    "nagent.model.cost_attempts"} <= names
        finally:
            meter_provider.shutdown()
            trace_provider.shutdown()

    def test_three_signals_share_one_attempt_cost(self, monkeypatch):
        """ALWAYS_ON + 内存 Span exporter + 完成日志快照：三信号同源。"""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        monkeypatch.setattr(ts, "get_tracer", lambda: provider.get_tracer(ts._TRACER_NAME))
        logs = []
        monkeypatch.setattr(ts.logger, "info", lambda _: logs.append(ts.execution_log_snapshot()))
        tel = ts.SemanticsTelemetry()
        tel.set_cost_estimator(lambda *args: dict(_ESTIMATED_RESULT))
        with tel.begin_chat_attempt(model="gpt-test", stream=False, provider="openai") as attempt:
            attempt.set_usage(100, 20)
        span = exporter.get_finished_spans()[0]
        cost_keys = [key for key in span.attributes if key.startswith("nagent.model.cost")]
        assert "nagent.model.cost_status" in cost_keys
        event = span.events[-1]
        assert event.name == "nagent.model.completed"
        for key in cost_keys:
            assert span.attributes[key] == event.attributes[key], key
            assert logs[-1][key] == span.attributes[key], key
        provider.shutdown()


# ---------------------------------------------------------------------------
# ChatAttempt 桶级有效性追踪（T3 移交补充范围）
# ---------------------------------------------------------------------------


class TestChatAttemptBucketValidity:
    def test_snapshot_channels_track_validity(self):
        # 必须走上下文管理器：set_usage 会发布 scene 事实，依赖 __exit__ 复位
        with ts.ChatAttempt(model="gpt-4o", stream=False, provider="openai") as attempt:
            assert attempt._cost_bucket_states == {
                "input": "unknown", "output": "unknown",
                "cache_read": "unknown", "cache_write": "unknown",
            }
            attempt.set_usage("x", 2)
            assert attempt._cost_bucket_states["input"] == "invalid"
            assert attempt._cost_bucket_states["output"] == "known"
            attempt.set_usage(1, None)  # 缺席不擦除
            assert attempt._cost_bucket_states["input"] == "known"
            attempt.set_usage(True, 2)  # 非法再次标记
            assert attempt._cost_bucket_states["input"] == "invalid"
            attempt.set_usage(3, 2)  # 合法快照恢复
            assert attempt._cost_bucket_states["input"] == "known"

    def test_cache_usage_none_never_erases(self):
        with ts.ChatAttempt(model="gpt-4o", stream=False, provider="anthropic") as attempt:
            attempt.set_cache_usage(cache_read_tokens=50, cache_write_tokens=20)
            attempt.set_cache_usage(cache_read_tokens=None)
            assert attempt._cost_bucket_states["cache_read"] == "known"
            assert attempt._cost_bucket_states["cache_write"] == "known"
            attempt.set_cache_usage(cache_read_tokens=-1, cache_write_tokens=1.5)
            assert attempt._cost_bucket_states["cache_read"] == "invalid"
            assert attempt._cost_bucket_states["cache_write"] == "invalid"

    def test_anthropic_raw_invalid_field_marks_bucket(self):
        """Anthropic raw 通道 presence 保留的直接单测：存在但非法不与缺席混淆。"""
        attempt = ts.ChatAttempt(model="claude-test", stream=False, provider="anthropic")
        attempt.set_raw_usage({"input_tokens": 100, "output_tokens": "x"})
        assert attempt._cost_bucket_states["output"] == "invalid"
        assert "output_tokens" not in attempt._raw_usage  # 非法值不进入累计视图
        assert attempt._raw_usage["input_tokens"] == 100
        # 后续合法累计快照恢复有效性
        attempt.set_raw_usage({"output_tokens": 25})
        assert attempt._cost_bucket_states["output"] == "unknown"
        assert attempt._raw_usage["output_tokens"] == 25

    def test_anthropic_raw_valid_cumulative_fields_recover(self):
        with ts.ChatAttempt(model="claude-test", stream=False, provider="anthropic") as attempt:
            attempt.set_raw_usage({"input_tokens": 100, "output_tokens": "x",
                                   "cache_read_input_tokens": 50,
                                   "cache_creation_input_tokens": 10})
            attempt.set_usage(100, None)
            attempt.set_cache_usage(cache_read_tokens=50, cache_write_tokens=10)
            attempt.set_raw_usage({"output_tokens": 25})
            attempt.set_usage(None, 25)
            assert attempt._cost_bucket_states == {
                "input": "known", "output": "known",
                "cache_read": "known", "cache_write": "known",
            }

    def test_ttl_subfield_invalid_marks_cache_write_and_recovers(self):
        """TTL 子字段存在但非法：标记 cache_write 曾非法（收口 invalid_usage，
        不退化成缺失事实的 unsupported_pricing）；非法值不进入累计视图、已保
        留合法值不擦除。子字段级状态独立维护（评审修复）：每个子字段自身
        恢复合法值才解除（整份报告不再出现非法值不足以恢复仍非法的子字段）。"""
        with ts.ChatAttempt(model="claude-test", stream=False, provider="anthropic") as attempt:
            attempt.set_raw_usage({"cache_creation": {"ephemeral_1h_input_tokens": "x"}})
            assert attempt._cost_bucket_states["cache_write"] == "invalid"
            assert attempt._raw_usage["cache_creation"] == {}  # 非法值不进入累计视图
            # 已保留合法值不被后续非法值擦除
            attempt.set_raw_usage({"cache_creation": {
                "ephemeral_1h_input_tokens": 10, "ephemeral_5m_input_tokens": "y"}})
            assert attempt._cost_bucket_states["cache_write"] == "invalid"
            assert attempt._raw_usage["cache_creation"] == {"ephemeral_1h_input_tokens": 10}
            # 曾非法的 1h 自身恢复，但 5m 仍非法：桶保持 invalid
            attempt.set_raw_usage({"cache_creation": {"ephemeral_1h_input_tokens": "x"}})
            attempt.set_raw_usage({"cache_creation": {"ephemeral_1h_input_tokens": 10}})
            assert attempt._cost_bucket_states["cache_write"] == "invalid"
            # 两个曾非法子字段均恢复合法值后才解除（曾非法 -> unknown）
            attempt.set_raw_usage({"cache_creation": {"ephemeral_5m_input_tokens": 5}})
            assert attempt._cost_bucket_states["cache_write"] == "unknown"
            assert attempt._raw_usage["cache_creation"] == {
                "ephemeral_1h_input_tokens": 10, "ephemeral_5m_input_tokens": 5}
            # 缺席不改状态
            attempt.set_raw_usage({"cache_creation": {}})
            assert attempt._cost_bucket_states["cache_write"] == "unknown"

    def test_ttl_invalid_survives_legal_cache_write_totals_until_subfield_recovers(self):
        """TTL 子字段非法状态独立维护：同一响应内后续到达的合法 cache_write
        总额（快照通道 set_cache_usage、raw 平铺通道 cache_creation_input_tokens）
        不得解除仍非法 TTL 子字段造成的 invalid 标记；只有该子字段自身恢复
        合法值后才按 no-erase 规则解除（finalization 才不会用陈旧 TTL 事实
        产出 estimated）。"""
        with ts.ChatAttempt(model="claude-test", stream=False, provider="anthropic") as attempt:
            # message_start：合法 TTL 先被保留
            attempt.set_raw_usage({"cache_creation": {"ephemeral_1h_input_tokens": 10}})
            assert attempt._cost_bucket_states["cache_write"] == "unknown"
            # 同响应后续报告：TTL 子字段存在但非法
            attempt.set_raw_usage({"cache_creation": {"ephemeral_1h_input_tokens": "x"}})
            assert attempt._cost_bucket_states["cache_write"] == "invalid"
            # 快照通道的合法 cache_write 总额不得解除 TTL 侧非法标记
            attempt.set_cache_usage(cache_write_tokens=10)
            assert attempt._cost_bucket_states["cache_write"] == "invalid"
            # raw 平铺通道的合法总额同样不得解除
            attempt.set_raw_usage({"cache_creation_input_tokens": 10})
            assert attempt._cost_bucket_states["cache_write"] == "invalid"
            # 该子字段自身恢复合法值后才解除（raw 视图按合法累计值重新裁决）
            attempt.set_raw_usage({"cache_creation": {"ephemeral_1h_input_tokens": 10}})
            assert attempt._cost_bucket_states["cache_write"] == "unknown"

    def test_openai_raw_invalid_prompt_marks_input(self):
        attempt = ts.ChatAttempt(model="gpt-4o", stream=False, provider="openai")
        attempt.set_raw_usage({"prompt_tokens": "x", "completion_tokens": 4})
        assert attempt._cost_bucket_states["input"] == "invalid"

    def test_openai_cached_exceeding_prompt_marks_input_invalid(self):
        """OpenAI cached>prompt 在 telemetry 层判 invalid（组合断言）。"""
        attempt = ts.ChatAttempt(model="gpt-4o", stream=False, provider="openai")
        attempt.set_raw_usage({"prompt_tokens": 150, "completion_tokens": 20,
                               "prompt_tokens_details": {"cached_tokens": 200}})
        assert attempt._cost_bucket_states["input"] == "invalid"

    def test_openai_cached_recovering_snapshot_clears_invalid(self):
        attempt = ts.ChatAttempt(model="gpt-4o", stream=False, provider="openai")
        attempt.set_raw_usage({"prompt_tokens": 150, "completion_tokens": 20,
                               "prompt_tokens_details": {"cached_tokens": 200}})
        attempt.set_raw_usage({"prompt_tokens": 150,
                               "prompt_tokens_details": {"cached_tokens": 50}})
        assert attempt._cost_bucket_states["input"] == "unknown"

    def test_openai_missing_cached_leaves_input_unknown(self):
        """缺 cached 不得把 prompt 当作 input（与 attempt_usage_from_raw 同语义）。"""
        attempt = ts.ChatAttempt(model="gpt-4o", stream=False, provider="openai")
        attempt.set_raw_usage({"prompt_tokens": 150, "completion_tokens": 20})
        assert attempt._cost_bucket_states["input"] == "unknown"

    def test_ttl_breakdown_retained_and_passed_to_estimator(self, monkeypatch):
        records = []
        monkeypatch.setattr(ts, "_record", lambda name, value, dims: records.append((name, value, dims)))
        tracer = FakeTracer()
        monkeypatch.setattr(ts, "get_tracer", lambda: tracer)
        calls = []

        def estimator(raw, model, provider, buckets, states):
            calls.append(raw)
            return dict(_ESTIMATED_RESULT)

        tel = ts.SemanticsTelemetry()
        tel.set_cost_estimator(estimator)
        with tel.begin_chat_attempt(model="claude-test", stream=False,
                                    provider="anthropic") as attempt:
            attempt.set_raw_usage({
                "input_tokens": 100, "output_tokens": 20,
                "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10,
                "cache_creation": {"ephemeral_1h_input_tokens": 10,
                                   "ephemeral_5m_input_tokens": None},
            })
            attempt.set_usage(100, 20)
            attempt.set_cache_usage(cache_read_tokens=50, cache_write_tokens=10)
        assert calls[0]["cache_creation"] == {"ephemeral_1h_input_tokens": 10}
        assert attempt_raw_ttl(calls[0]) == "1h"


def attempt_raw_ttl(raw):
    """模拟组合根 TTL 事实派生：单一非零 TTL 时产出 cache_ttl 条件事实。"""
    breakdown = raw.get("cache_creation") if isinstance(raw, dict) else None
    if not isinstance(breakdown, dict):
        return None
    ttls = set()
    for key, ttl in (("ephemeral_1h_input_tokens", "1h"),
                     ("ephemeral_5m_input_tokens", "5m")):
        value = breakdown.get(key)
        if type(value) is int and value > 0:
            ttls.add(ttl)
    return next(iter(ttls)) if len(ttls) == 1 else None
