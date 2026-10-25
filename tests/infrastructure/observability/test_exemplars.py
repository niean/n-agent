"""Real SDK contract: facade durations keep sampled Trace IDs in exemplars."""
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ALWAYS_OFF
from app.application import telemetry_semantics as ts


@pytest.mark.parametrize("mode", ["sampled", "unsampled", "no-span"])
def test_duration_exemplars_keep_current_trace_without_metric_labels(monkeypatch, mode):
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    tracer = TracerProvider(sampler=ALWAYS_ON if mode == "sampled" else ALWAYS_OFF)
    monkeypatch.setattr(ts.metrics, "get_meter", meter.get_meter)
    monkeypatch.setattr(ts.trace, "get_tracer", tracer.get_tracer)
    # Avoid process-global provider publication; use the real facade with local SDKs.
    monkeypatch.setattr(ts, "_instrument_cache", {})
    try:
        facade = ts.SemanticsTelemetry()
        if mode == "no-span":
            ts.record_invoke_agent_durations_and_calls("n-agent", 0.25, None, 1, 1)
            expected = None
        else:
            with facade.agent_run(session_id="exemplar-test", source="cli"):
                with ts.ChatAttempt(model="gpt-4o-mini", stream=False):
                    chat_ctx = ts.trace.get_current_span().get_span_context()
                with facade.tool_execution("calculator"):
                    tool_ctx = ts.trace.get_current_span().get_span_context()
                from opentelemetry import trace
                expected = trace.get_current_span().get_span_context()
        data = reader.get_metrics_data()
        points = [p for r in data.resource_metrics for s in r.scope_metrics
                  for m in s.metrics if m.name in (ts.INSTRUMENT_INVOKE_AGENT_DURATION, ts.INSTRUMENT_INFERENCE_DURATION, ts.INSTRUMENT_EXECUTE_TOOL_DURATION)
                  for p in m.data.data_points]
        assert points
        exemplars = [e for p in points for e in p.exemplars]
        assert all("trace_id" not in p.attributes and "span_id" not in p.attributes for p in points)
        if mode == "sampled":
            assert exemplars
            assert all(e.trace_id == expected.trace_id for e in exemplars)
            by_name = {m.name: [e for p in m.data.data_points for e in p.exemplars]
                       for r in data.resource_metrics for s in r.scope_metrics for m in s.metrics}
            for name, ctx in ((ts.INSTRUMENT_INVOKE_AGENT_DURATION, expected),
                              (ts.INSTRUMENT_INFERENCE_DURATION, chat_ctx),
                              (ts.INSTRUMENT_EXECUTE_TOOL_DURATION, tool_ctx)):
                assert by_name[name], name
                assert all(e.span_id == ctx.span_id for e in by_name[name]), name
        else:
            assert not exemplars
    finally:
        meter.shutdown()
        tracer.shutdown()
