"""T2 contract tests: telemetry_semantics GenAI semconv registry.

Asserts the semantic contract from spec-261024-otel-observability.md:
pinned semconv version, span name helpers with bounded safe mapping,
GenAI attribute/instrument constants, histogram buckets, nagent.*
supplement attributes, no-op-Provider-safe helpers, and the
dimension cardinality guard.
"""
from __future__ import annotations

import inspect

import pytest

from app.application import telemetry_semantics as ts


# ---------------------------------------------------------------------------
# (a) pinned semconv version
# ---------------------------------------------------------------------------


class TestSemconvVersion:
    def test_semconv_version_constant_exists_and_nonempty(self):
        assert isinstance(ts.SEMCONV_VERSION, str)
        assert ts.SEMCONV_VERSION.strip()

    def test_docstring_references_semconv_version(self):
        doc = inspect.getdoc(ts) or ""
        assert ts.SEMCONV_VERSION in doc


# ---------------------------------------------------------------------------
# (b) span name helpers with bounded safe mapping
# ---------------------------------------------------------------------------


class TestSpanNames:
    def test_invoke_agent_span_name(self):
        assert ts.invoke_agent_span_name() == "invoke_agent n-agent"

    def test_chat_span_name_known_model(self):
        assert ts.chat_span_name("gpt-4o") == "chat gpt-4o"

    def test_chat_span_name_unknown_model_collapses_to_other(self):
        assert ts.chat_span_name("not-a-real-model-xyz") == "chat other"

    def test_chat_span_name_maps_registered_aliases(self):
        # registry/fixed set mapping, not raw passthrough
        assert ts.chat_span_name("gpt-4o-2024-08-13") == "chat gpt-4o"

    def test_chat_span_name_rejects_raw_concatenation(self):
        # unknown value must never be concatenated raw into the span name
        name = ts.chat_span_name("totally-unknown-model-with-;inject")
        assert name == "chat other"

    def test_chat_span_name_length_cap(self):
        long_name = "m" * 500
        # unknown -> other; even if mapped, never exceed bounded length
        assert ts.chat_span_name(long_name) == "chat other"

    def test_execute_tool_span_name_known_tool(self):
        assert ts.execute_tool_span_name("web_search") == "execute_tool web_search"

    def test_execute_tool_span_name_unknown_tool(self):
        assert ts.execute_tool_span_name("some_dynamic_tool_zzz") == "execute_tool other"

    def test_execute_tool_span_name_registered_dynamic_tool(self):
        # dynamically registered tool names map through a bounded registry
        ts.register_tool_name("my_custom_tool")
        try:
            assert ts.execute_tool_span_name("my_custom_tool") == "execute_tool my_custom_tool"
        finally:
            ts.unregister_tool_name("my_custom_tool")

    def test_chat_span_name_real_configured_model(self):
        # seed known set: a real low-card model name shows, not "other"
        assert ts.chat_span_name("qwen2.5") == "chat qwen2.5"
        assert ts.chat_span_name("claude-opus-5-5-20261024") == "chat claude-opus-5-5"
        # real Claude ids use a non-dashed YYYYMMDD suffix
        assert ts.chat_span_name("claude-opus-4-20250514") == "chat claude-opus-4"

    def test_chat_span_name_registered_dynamic_model(self):
        # unknown model registers through the bounded registry, then maps real
        ts.register_model_name("my_self_hosted_model")
        try:
            assert ts.chat_span_name("my_self_hosted_model") == "chat my_self_hosted_model"
        finally:
            ts.unregister_model_name("my_self_hosted_model")

    def test_chat_span_name_registered_model_strips_date_suffix(self):
        ts.register_model_name("vllm-llama-8b-20261001")
        try:
            assert ts.chat_span_name("vllm-llama-8b-20261001") == "chat vllm-llama-8b-20261001"
        finally:
            ts.unregister_model_name("vllm-llama-8b-20261001")

    def test_span_names_never_contain_raw_untrusted_input(self):
        for bad in ["x y", "x<span>", "x" * 300, "x\ntail"]:
            chat = ts.chat_span_name(bad)
            tool = ts.execute_tool_span_name(bad)
            assert bad not in chat
            assert bad not in tool


# ---------------------------------------------------------------------------
# (c) GenAI attribute constants
# ---------------------------------------------------------------------------


class TestGenAiAttributes:
    EXPECTED = {
        "GEN_AI_OPERATION_NAME": "gen_ai.operation.name",
        "GEN_AI_PROVIDER_NAME": "gen_ai.provider.name",
        "GEN_AI_REQUEST_MODEL": "gen_ai.request.model",
        "GEN_AI_RESPONSE_MODEL": "gen_ai.response.model",
        "GEN_AI_USAGE_INPUT_TOKENS": "gen_ai.usage.input_tokens",
        "GEN_AI_USAGE_OUTPUT_TOKENS": "gen_ai.usage.output_tokens",
        "GEN_AI_RESPONSE_FINISH_REASONS": "gen_ai.response.finish_reasons",
        "GEN_AI_REQUEST_STREAM": "gen_ai.request.stream",
        "GEN_AI_CONVERSATION_ID": "gen_ai.conversation.id",
        "GEN_AI_AGENT_NAME": "gen_ai.agent.name",
        "GEN_AI_TOOL_NAME": "gen_ai.tool.name",
        "GEN_AI_TOOL_CALL_ID": "gen_ai.tool.call.id",
        "GEN_AI_TOOL_TYPE": "gen_ai.tool.type",
    }

    def test_attribute_constants_match_genai_draft(self):
        for const, value in self.EXPECTED.items():
            assert getattr(ts, const) == value, const

    def test_no_nagent_equivalents_for_genai_signals(self):
        # one canonical name per signal: no nagent.* duplicates of GenAI signals
        nagent_values = {
            v for k, v in vars(ts).items()
            if isinstance(v, str) and v.startswith("nagent.")
        }
        genai_values = set(self.EXPECTED.values())
        assert nagent_values.isdisjoint(genai_values)


# ---------------------------------------------------------------------------
# (d) instrument constants match the spec metric table
# ---------------------------------------------------------------------------


class TestInstruments:
    EXPECTED = [
        # (name, kind, unit, allowed dimensions sorted)
        ("gen_ai.client.inference.usage.input_tokens", "counter", "{token}",
         {"gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.token.modality",
          "gen_ai.request.model"}),
        ("gen_ai.client.inference.usage.output_tokens", "counter", "{token}",
         {"gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.token.modality",
          "gen_ai.request.model"}),
        ("gen_ai.client.inference.operation.input_tokens", "histogram", "{token}",
         {"gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model",
          "error.type"}),
        ("gen_ai.client.inference.operation.output_tokens", "histogram", "{token}",
         {"gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model",
          "error.type"}),
        ("gen_ai.client.inference.duration", "histogram", "s",
         {"gen_ai.operation.name", "gen_ai.provider.name", "gen_ai.request.model",
          "error.type"}),
        ("gen_ai.execute_tool.duration", "histogram", "s",
         {"gen_ai.tool.name", "gen_ai.tool.type", "gen_ai.agent.name", "error.type"}),
        ("gen_ai.invoke_agent.duration", "histogram", "s",
         {"gen_ai.agent.name", "error.type"}),
        ("gen_ai.invoke_agent.inference_calls", "histogram", "{inference_call}",
         {"gen_ai.agent.name"}),
        ("gen_ai.invoke_agent.tool_calls", "histogram", "{tool_call}",
         {"gen_ai.agent.name"}),
        ("nagent.turns", "counter", "1",
         {"nagent.source", "nagent.end_reason"}),
        ("nagent.context.compressions", "counter", "1",
         {"nagent.outcome"}),
        ("nagent.budget.denied", "counter", "1",
         {"nagent.kind"}),
    ]

    def test_twelve_instruments_declared(self):
        assert len(ts.INSTRUMENTS) == 12

    def test_instruments_match_spec_table(self):
        by_name = {i.name: i for i in ts.INSTRUMENTS.values()}
        assert set(by_name) == {e[0] for e in self.EXPECTED}
        for name, kind, unit, dims in self.EXPECTED:
            inst = by_name[name]
            assert inst.kind == kind, name
            assert inst.unit == unit, name
            assert set(inst.allowed_dimensions) == dims, name

    def test_instrument_constants_exposed(self):
        # individual constants for ergonomic call sites
        assert ts.INSTRUMENT_INFERENCE_INPUT_TOKENS == "gen_ai.client.inference.usage.input_tokens"
        assert ts.INSTRUMENT_INFERENCE_OUTPUT_TOKENS == "gen_ai.client.inference.usage.output_tokens"
        assert ts.INSTRUMENT_OPERATION_INPUT_TOKENS == "gen_ai.client.inference.operation.input_tokens"
        assert ts.INSTRUMENT_OPERATION_OUTPUT_TOKENS == "gen_ai.client.inference.operation.output_tokens"
        assert ts.INSTRUMENT_INFERENCE_DURATION == "gen_ai.client.inference.duration"
        assert ts.INSTRUMENT_EXECUTE_TOOL_DURATION == "gen_ai.execute_tool.duration"
        assert ts.INSTRUMENT_INVOKE_AGENT_DURATION == "gen_ai.invoke_agent.duration"
        assert ts.INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS == "gen_ai.invoke_agent.inference_calls"
        assert ts.INSTRUMENT_INVOKE_AGENT_TOOL_CALLS == "gen_ai.invoke_agent.tool_calls"
        assert ts.INSTRUMENT_TURNS == "nagent.turns"
        assert ts.INSTRUMENT_CONTEXT_COMPRESSIONS == "nagent.context.compressions"
        assert ts.INSTRUMENT_BUDGET_DENIED == "nagent.budget.denied"


# ---------------------------------------------------------------------------
# (e) histogram bucket boundaries
# ---------------------------------------------------------------------------


class TestHistogramBuckets:
    def test_token_histogram_boundaries(self):
        assert list(ts.TOKEN_HISTOGRAM_BOUNDARIES) == [
            1, 4, 16, 64, 256, 1024, 4096, 16384, 65536, 262144,
            1048576, 4194304, 16777216, 67108864,
        ]

    def test_duration_histogram_boundaries_declared(self):
        # explicit, not SDK-default-dependent
        assert isinstance(ts.DURATION_HISTOGRAM_BOUNDARIES, (list, tuple))
        assert len(ts.DURATION_HISTOGRAM_BOUNDARIES) > 0
        assert list(ts.DURATION_HISTOGRAM_BOUNDARIES) == sorted(
            ts.DURATION_HISTOGRAM_BOUNDARIES
        )


# ---------------------------------------------------------------------------
# (f) nagent.* supplement attributes
# ---------------------------------------------------------------------------


class TestNagentAttributes:
    def test_supplement_attribute_constants(self):
        assert ts.NAGENT_SOURCE == "nagent.source"
        assert ts.NAGENT_END_REASON == "nagent.end_reason"
        assert ts.NAGENT_ITERATION == "nagent.iteration"
        assert ts.NAGENT_RUN_ID == "nagent.run_id"
        assert ts.NAGENT_POLICY_OUTCOME == "nagent.policy_outcome"
        assert ts.NAGENT_RISK_LEVEL == "nagent.risk_level"
        assert ts.NAGENT_USAGE_KNOWN == "nagent.usage_known"
        assert ts.NAGENT_BEFORE_TOKENS == "nagent.before_tokens"
        assert ts.NAGENT_AFTER_TOKENS == "nagent.after_tokens"
        assert ts.NAGENT_OUTCOME == "nagent.outcome"
        assert ts.NAGENT_KIND == "nagent.kind"

    def test_risk_level_fixed_enum(self):
        assert {v.value for v in ts.RiskLevel} == {"safe", "confirm", "dangerous"}

    def test_risk_level_from_value_rejects_free_text(self):
        assert ts.RiskLevel.from_value("safe") is ts.RiskLevel.SAFE
        assert ts.RiskLevel.from_value("whatever-free-text") is None


# ---------------------------------------------------------------------------
# (g) helpers work under the no-op Provider
# ---------------------------------------------------------------------------


class TestNoopProviderHelpers:
    def test_get_tracer_and_meter_noop(self):
        tracer = ts.get_tracer()
        meter = ts.get_meter()
        assert tracer is not None
        assert meter is not None

    def test_get_tracer_not_permanently_cached(self, monkeypatch):
        # T3 contract: proxies must resolve after Provider initialization,
        # so the helper must delegate to trace.get_tracer on every call
        calls = []

        def fake_get_tracer(name):
            calls.append(name)
            return object()

        monkeypatch.setattr(ts.trace, "get_tracer", fake_get_tracer)
        first = ts.get_tracer()
        second = ts.get_tracer()
        assert calls == [ts._TRACER_NAME, ts._TRACER_NAME]
        assert first is not second

    def test_span_contextmanagers_do_not_raise(self):
        with ts.invoke_agent_span(session_id="s1", source="cli", run_id="r1"):
            with ts.chat_span(model="gpt-4o", provider="openai"):
                pass
        with ts.execute_tool_span(tool="web_search", call_id="c1"):
            pass
        with ts.compress_span(before_tokens=10, after_tokens=5):
            pass

    def test_span_contextmanager_propagates_business_exception(self):
        class BizError(RuntimeError):
            pass

        with pytest.raises(BizError):
            with ts.chat_span(model="gpt-4o", provider="openai"):
                raise BizError("business failure")

    def test_span_contextmanager_propagates_cancellation(self):
        import asyncio

        async def run():
            with ts.chat_span(model="gpt-4o", provider="openai"):
                raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())

    def test_span_contextmanager_does_not_swallow_keyboard_interrupt(self):
        with pytest.raises(KeyboardInterrupt):
            with ts.execute_tool_span(tool="web_search", call_id="c1"):
                raise KeyboardInterrupt()

    def test_metric_record_helpers_do_not_raise_under_noop(self):
        # counter + histogram helpers via the API (no-op default meter)
        ts.record_turn(source="cli", end_reason="completed")
        ts.record_context_compression(outcome="compressed")
        ts.record_budget_denied(kind="budget_exhausted")
        ts.record_inference_duration(
            model="gpt-4o", provider="openai", operation="chat",
            seconds=0.5, error_type=None,
        )
        ts.record_execute_tool_duration(
            tool="web_search", tool_type="function",
            agent="n-agent", seconds=0.1, error_type=None,
        )
        ts.record_invoke_agent_durations_and_calls(
            agent="n-agent", seconds=1.0, error_type=None,
            inference_calls=2, tool_calls=1,
        )
        ts.record_inference_tokens(
            model="gpt-4o", provider="openai", operation="chat",
            input_tokens=10, output_tokens=5, error_type=None,
        )


# ---------------------------------------------------------------------------
# (g2) span helpers actually record under a real (fake) tracer
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


class FakeTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, kind=None, **_kwargs):
        span = FakeSpan(name)
        self.spans.append(span)
        return span


class TestRealTracerContract:
    """With a functioning tracer, spans must actually be started, attributed,
    statused and ended exactly once -- not silently dead under no-op fallback."""

    @pytest.fixture
    def tracer(self, monkeypatch):
        fake = FakeTracer()
        monkeypatch.setattr(ts, "get_tracer", lambda: fake)
        return fake

    def test_chat_span_records_attributes_and_ends(self, tracer):
        with ts.chat_span(model="gpt-4o", provider="openai", stream=True, iteration=2):
            pass
        assert len(tracer.spans) == 1
        span = tracer.spans[0]
        assert span.name == "chat gpt-4o"
        assert span.attributes[ts.GEN_AI_OPERATION_NAME] == "chat"
        assert span.attributes[ts.GEN_AI_PROVIDER_NAME] == "openai"
        assert span.attributes[ts.GEN_AI_REQUEST_MODEL] == "gpt-4o"
        assert span.attributes[ts.GEN_AI_REQUEST_STREAM] is True
        assert span.attributes[ts.NAGENT_ITERATION] == 2
        assert span.ended == 1

    def test_span_error_path_sets_safe_error_type_and_status(self, tracer):
        class BizError(RuntimeError):
            pass

        with pytest.raises(BizError):
            with ts.execute_tool_span(tool="web_search", call_id="c1"):
                raise BizError("boom")
        span = tracer.spans[0]
        assert span.name == "execute_tool web_search"
        assert span.attributes["error.type"] == "other"  # unknown class -> other
        assert span.status is not None
        assert span.status.status_code is ts.StatusCode.ERROR
        assert span.ended == 1

    def test_invoke_agent_span_attributes(self, tracer):
        with ts.invoke_agent_span(session_id="s1", source="cli", run_id="r1"):
            pass
        span = tracer.spans[0]
        assert span.name == "invoke_agent n-agent"
        assert span.attributes[ts.GEN_AI_CONVERSATION_ID] == "s1"
        assert span.attributes[ts.NAGENT_SOURCE] == "cli"
        assert span.attributes[ts.NAGENT_RUN_ID] == "r1"
        assert span.attributes[ts.GEN_AI_AGENT_NAME] == "n-agent"
        assert span.ended == 1

    def test_compress_span_attributes(self, tracer):
        with ts.compress_span(before_tokens=100, after_tokens=40):
            pass
        span = tracer.spans[0]
        assert span.name == "nagent.context.compress"
        assert span.attributes[ts.NAGENT_BEFORE_TOKENS] == 100
        assert span.attributes[ts.NAGENT_AFTER_TOKENS] == 40
        assert span.ended == 1

    def test_agent_name_centralized_constant(self):
        assert ts.DEFAULT_AGENT_NAME == "n-agent"
        assert ts._TRACER_NAME.startswith(ts.DEFAULT_AGENT_NAME)
        assert ts._METER_NAME.startswith(ts.DEFAULT_AGENT_NAME)


# ---------------------------------------------------------------------------
# (g3) metric helpers under a fake meter: valid attrs, cached handles
# ---------------------------------------------------------------------------


class FakeCounter:
    def __init__(self):
        self.records = []

    def add(self, value, attrs):
        self.records.append((value, attrs))


class FakeHistogram:
    def __init__(self, boundaries):
        self.boundaries = boundaries
        self.records = []

    def record(self, value, attrs):
        for k, v in attrs.items():
            assert v is not None, f"invalid None attribute value for {k}"
        self.records.append((value, attrs))


class FakeMeter:
    def __init__(self):
        self.counters = {}
        self.histograms = {}
        self.create_counter_calls = 0
        self.create_histogram_calls = 0

    def create_counter(self, name, unit=None, **_kwargs):
        self.create_counter_calls += 1
        return self.counters.setdefault(name, FakeCounter())

    def create_histogram(
        self,
        name,
        unit=None,
        explicit_bucket_boundaries_advisory=None,
    ):
        # Explicit signature (no **kwargs): a future kwarg-name mismatch in
        # telemetry_semantics must fail here instead of being swallowed.
        self.create_histogram_calls += 1
        return self.histograms.setdefault(
            name, FakeHistogram(explicit_bucket_boundaries_advisory)
        )


class TestFakeMeterContract:
    @pytest.fixture
    def meter(self, monkeypatch):
        ts._instrument_cache.clear()
        fake = FakeMeter()
        monkeypatch.setattr(ts, "get_meter", lambda: fake)
        yield fake
        ts._instrument_cache.clear()

    def test_success_path_omits_error_type_key(self, meter):
        ts.record_inference_duration(
            model="gpt-4o", provider="openai", operation="chat",
            seconds=0.5, error_type=None,
        )
        hist = meter.histograms[ts.INSTRUMENT_INFERENCE_DURATION]
        assert len(hist.records) == 1
        _value, attrs = hist.records[0]
        assert "error.type" not in attrs

    def test_all_recorded_attribute_values_valid(self, meter):
        ts.record_inference_duration(
            model="gpt-4o", provider="openai", operation="chat",
            seconds=0.5, error_type="TimeoutError",
        )
        ts.record_execute_tool_duration(
            tool="web_search", tool_type="function",
            agent="n-agent", seconds=0.1, error_type=None,
        )
        ts.record_inference_tokens(
            model="gpt-4o", provider="openai", operation="chat",
            input_tokens=10, output_tokens=5, error_type=None,
        )
        # FakeHistogram asserts non-None on every record; reaching here means
        # no invalid None attribute values were passed.

    def test_error_path_includes_mapped_error_type(self, meter):
        ts.record_execute_tool_duration(
            tool="web_search", tool_type="function",
            agent="n-agent", seconds=0.1, error_type="TimeoutError",
        )
        hist = meter.histograms[ts.INSTRUMENT_EXECUTE_TOOL_DURATION]
        _value, attrs = hist.records[0]
        assert attrs["error.type"] == "TimeoutError"

    def test_instrument_handles_cached(self, meter):
        ts.record_turn(source="cli", end_reason="completed")
        ts.record_turn(source="cli", end_reason="completed")
        assert meter.create_counter_calls == 1
        ts.record_inference_duration(
            model="gpt-4o", provider="openai", operation="chat",
            seconds=1.0, error_type=None,
        )
        ts.record_inference_duration(
            model="gpt-4o", provider="openai", operation="chat",
            seconds=2.0, error_type=None,
        )
        assert meter.create_histogram_calls == 1

    def test_histograms_get_explicit_bucket_boundaries(self, meter):
        ts.record_inference_duration(
            model="gpt-4o", provider="openai", operation="chat",
            seconds=0.5, error_type=None,
        )
        hist = meter.histograms[ts.INSTRUMENT_INFERENCE_DURATION]
        assert list(hist.boundaries) == list(ts.DURATION_HISTOGRAM_BOUNDARIES)
        ts.record_inference_tokens(
            model="gpt-4o", provider="openai", operation="chat",
            input_tokens=10, output_tokens=None, error_type=None,
        )
        token_hist = meter.histograms[ts.INSTRUMENT_OPERATION_INPUT_TOKENS]
        assert list(token_hist.boundaries) == list(ts.TOKEN_HISTOGRAM_BOUNDARIES)

    def test_turn_end_reason_has_own_vocabulary(self, meter):
        ts.record_turn(source="cli", end_reason="completed")
        counter = meter.counters[ts.INSTRUMENT_TURNS]
        _value, attrs = counter.records[0]
        assert attrs[ts.NAGENT_END_REASON] == "completed"
        ts.record_turn(source="cli", end_reason="mystery-reason")
        _value, attrs = counter.records[1]
        assert attrs[ts.NAGENT_END_REASON] == "other"


# ---------------------------------------------------------------------------
# (h) dimension cardinality guard
# ---------------------------------------------------------------------------


class TestCardinalityGuard:
    def setup_method(self):
        ts._reset_dimension_guard_for_tests()

    def teardown_method(self):
        ts._reset_dimension_guard_for_tests()

    def test_fixed_set_mappings_unknown_to_other(self):
        assert ts.map_provider_name("openai") == "openai"
        assert ts.map_provider_name("weird-provider") == "other"
        assert ts.map_error_type("TimeoutError") == "TimeoutError"
        assert ts.map_error_type("some unique failure message text") == "other"
        assert ts.map_source("cli") == "cli"
        assert ts.map_source("mystery-source") == "other"
        assert ts.map_outcome("compressed") == "compressed"
        assert ts.map_outcome("mystery-outcome") == "other"
        # end_reason has its own bounded vocabulary (turn-end values),
        # including the Domain EndReason vocabulary emitted by the graph
        # (_END_REASON_TO_TELEMETRY) that finish_reason cannot distinguish
        assert ts.map_end_reason("completed") == "completed"
        assert ts.map_end_reason("max_iterations") == "max_iterations"
        assert ts.map_end_reason("length") == "length"
        assert ts.map_end_reason("deadline") == "deadline"
        assert ts.map_end_reason("tool_calls") == "tool_calls"
        assert ts.map_end_reason("mystery-end") == "other"

    def test_guard_caps_series_at_1000_per_instrument(self):
        inst = ts.INSTRUMENT_INVOKE_AGENT_DURATION
        # 999 distinct normal series
        for i in range(999):
            dims = ts.guard_dimensions(inst, {ts.GEN_AI_AGENT_NAME: f"agent-{i}"})
            assert dims[ts.GEN_AI_AGENT_NAME] == f"agent-{i}"
        # 1000th distinct combination -> fixed overflow series
        overflow = ts.guard_dimensions(inst, {ts.GEN_AI_AGENT_NAME: "agent-overflow"})
        assert overflow == ts.OVERFLOW_DIMENSIONS
        # and every further distinct combination merges into the same series
        again = ts.guard_dimensions(inst, {ts.GEN_AI_AGENT_NAME: "agent-more"})
        assert again == ts.OVERFLOW_DIMENSIONS
        # total distinct series (all readers combined, process-wide) <= 1000
        assert ts.dimension_series_count(inst) == 1000

    def test_guard_counts_across_readers_and_calls(self):
        inst = ts.INSTRUMENT_TURNS
        for i in range(1200):
            ts.guard_dimensions(inst, {ts.NAGENT_SOURCE: f"src-{i}"})
        assert ts.dimension_series_count(inst) == 1000

    def test_guard_is_per_instrument(self):
        inst_a = ts.INSTRUMENT_TURNS
        inst_b = ts.INSTRUMENT_BUDGET_DENIED
        for i in range(999):
            ts.guard_dimensions(inst_a, {ts.NAGENT_SOURCE: f"src-{i}"})
        # a different instrument has an independent budget
        dims = ts.guard_dimensions(inst_b, {ts.NAGENT_KIND: "kind-new"})
        assert dims[ts.NAGENT_KIND] == "kind-new"

    def test_guard_reuses_known_series(self):
        inst = ts.INSTRUMENT_TURNS
        for i in range(999):
            ts.guard_dimensions(inst, {ts.NAGENT_SOURCE: f"src-{i}"})
        # repeating an existing series never overflows
        dims = ts.guard_dimensions(inst, {ts.NAGENT_SOURCE: "src-0"})
        assert dims[ts.NAGENT_SOURCE] == "src-0"


# ---------------------------------------------------------------------------
# concurrency
# ---------------------------------------------------------------------------


class TestConcurrency:
    def setup_method(self):
        ts._reset_dimension_guard_for_tests()

    def teardown_method(self):
        ts._reset_dimension_guard_for_tests()

    def test_guard_dimensions_thread_safe_and_capped(self):
        import threading

        errors = []

        def worker(worker_id):
            try:
                for i in range(400):
                    ts.guard_dimensions(
                        ts.INSTRUMENT_TURNS,
                        {ts.NAGENT_SOURCE: f"src-{worker_id}-{i}"},
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert ts.dimension_series_count(ts.INSTRUMENT_TURNS) == 1000

    def test_register_tool_name_thread_safe_and_capped(self):
        import threading

        ts.unregister_tool_name("does-not-exist")
        errors = []

        def worker(worker_id):
            try:
                for i in range(100):
                    ts.register_tool_name(f"dyn_tool_{worker_id}_{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        with ts._registered_tools_lock:
            registered = len(ts._registered_tools)
        assert registered <= ts._MAX_REGISTERED_TOOLS
        # cleanup dynamically registered test tools
        with ts._registered_tools_lock:
            ts._registered_tools.clear()

    def test_overflow_dimension_key_is_reserved(self):
        assert set(ts.OVERFLOW_DIMENSIONS) == {"__nagent.overflow__"}


# ---------------------------------------------------------------------------
# purity: no business-module imports
# ---------------------------------------------------------------------------


class TestModulePurity:
    def test_no_business_module_imports(self):
        src = inspect.getsource(ts)
        for forbidden in ("app.domain", "app.infrastructure", "app.interfaces",
                          "app.main", "opentelemetry.sdk", "opentelemetry.exporter",
                          "opentelemetry.instrumentation"):
            assert forbidden not in src, forbidden

    def test_only_stdlib_and_otel_api_imports(self):
        import ast
        import sys

        tree = ast.parse(inspect.getsource(ts))
        allowed_top = {"app", "opentelemetry", "__future__"} | sys.stdlib_module_names
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in allowed_top | {"__future__"}, alias.name
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root in allowed_top | {"__future__"}, node.module


# ---------------------------------------------------------------------------
# T3 (spec-261027): 实时执行现场 ContextVar + 作用域发布与隔离
# ---------------------------------------------------------------------------


class TestLiveSceneSnapshot:
    def test_snapshot_merges_live_scene_and_isolation(self):
        telemetry = ts.SemanticsTelemetry()
        with telemetry.agent_run(session_id="s1", source="dashboard", run_id="r1", message_count=3) as h:
            with telemetry.iteration(2, message_count=5):
                snap = ts.execution_log_snapshot()
                assert snap["operation"] == "invoke_agent"
                assert snap["iteration"] == 2 and snap["message_count"] == 5
            with telemetry.tool_execution("calculator", call_id="c1", risk_level="safe") as th:
                th["executed"] = True
                snap = ts.execution_log_snapshot()
                assert snap["operation"] == "execute_tool" and snap["tool"] == "calculator"
                assert snap["call_id"] == "c1"
            snap = ts.execution_log_snapshot()
            assert snap["operation"] == "invoke_agent"  # restored parent
        snap = ts.execution_log_snapshot()
        assert "operation" not in snap  # no leakage out of scope


# ---------------------------------------------------------------------------
# T4 (spec-261027): span 场景属性安全转换注入 (_set_attr 经 sanitizer)
# ---------------------------------------------------------------------------


class TestSpanAttributeSanitizer:
    _SECRET = "E2EOTELVERIFY-7f3a-secret"
    _REDACTED = "[REDACTED]"

    @pytest.fixture
    def sanitizer(self, monkeypatch):
        """Install a fixed secret-pattern callable; always teardown."""
        ts.configure_span_attribute_sanitizer(
            lambda text: text.replace(self._SECRET, self._REDACTED)
        )
        yield
        ts.configure_span_attribute_sanitizer(None)

    @pytest.fixture
    def tracer(self, monkeypatch):
        fake = FakeTracer()
        monkeypatch.setattr(ts, "get_tracer", lambda: fake)
        return fake

    def test_summary_passes_through_sanitizer(self, sanitizer, tracer):
        with ts.invoke_agent_span(session_id="s1", source="cli", run_id="r1") as span:
            ts._complete(span, "invoke_agent", span_start := 0.0,
                         message_count=2, inference_calls=1, tool_calls=0,
                         end_reason="completed", outcome="success")
        summary = tracer.spans[0].attributes[ts.NAGENT_SUMMARY]
        assert self._SECRET not in summary

    def test_dynamic_string_attr_sanitized(self, sanitizer, tracer):
        with ts.invoke_agent_span(session_id="s1", source="cli", run_id="r1") as span:
            # inject a raw secret-bearing string via _set_attr directly
            span.attributes[ts.NAGENT_SUMMARY] = "probe"
            ts._set_attr(span, ts.NAGENT_SUMMARY, f"prefix {self._SECRET} suffix")
        assert "prefix [REDACTED] suffix" == tracer.spans[0].attributes[ts.NAGENT_SUMMARY]

    def test_uninstalled_sanitizer_still_writes_dynamic(self, tracer):
        with ts.invoke_agent_span(session_id="s1", source="cli", run_id="r1") as span:
            ts._set_attr(span, ts.NAGENT_SUMMARY, f"pre {self._SECRET} post")
        assert f"pre {self._SECRET} post" == tracer.spans[0].attributes[ts.NAGENT_SUMMARY]

    def test_sanitizer_raise_omits_dynamic_attr(self, monkeypatch, tracer):
        def boom(text):
            raise RuntimeError("sanitizer failed")
        ts.configure_span_attribute_sanitizer(boom)
        try:
            with ts.invoke_agent_span(session_id="s1", source="cli", run_id="r1") as span:
                ts._set_attr(span, ts.NAGENT_SUMMARY, "anything")
            assert ts.NAGENT_SUMMARY not in tracer.spans[0].attributes
        finally:
            ts.configure_span_attribute_sanitizer(None)
