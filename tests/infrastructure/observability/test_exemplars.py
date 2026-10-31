"""Real SDK contract: facade durations keep sampled Trace IDs in exemplars."""
import asyncio

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
        agent_metrics = {m.name: list(m.data.data_points)
                         for r in data.resource_metrics for s in r.scope_metrics
                         for m in s.metrics if m.name in AGENT_HISTOGRAMS}
        assert set(agent_metrics) == set(AGENT_HISTOGRAMS)
        for values in agent_metrics.values():
            assert all(p.attributes[ts.NAGENT_SOURCE] == ("other" if mode == "no-span" else "cli")
                       for p in values)
            if mode != "sampled":
                assert all(not p.exemplars for p in values)
    finally:
        meter.shutdown()
        tracer.shutdown()


AGENT_HISTOGRAMS = (
    ts.INSTRUMENT_INVOKE_AGENT_DURATION,
    ts.INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS,
    ts.INSTRUMENT_INVOKE_AGENT_TOOL_CALLS,
)


@pytest.fixture
def source_sdk(monkeypatch):
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    tracer = TracerProvider()
    monkeypatch.setattr(ts.metrics, "get_meter", meter.get_meter)
    monkeypatch.setattr(ts.trace, "get_tracer", tracer.get_tracer)
    monkeypatch.setattr(ts, "_instrument_cache", {})
    yield reader, ts.SemanticsTelemetry()
    meter.shutdown()
    tracer.shutdown()


def agent_points(reader):
    return {
        metric.name: list(metric.data.data_points)
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name in AGENT_HISTOGRAMS
    }


def assert_agent_point(point, source, context):
    assert point.attributes[ts.NAGENT_SOURCE] == source
    assert set(point.attributes) <= {ts.GEN_AI_AGENT_NAME, ts.NAGENT_SOURCE, "error.type"}
    assert point.exemplars
    assert all(e.trace_id == context.trace_id and e.span_id == context.span_id
               for e in point.exemplars)


@pytest.mark.parametrize("source,expected", [
    *[(value, value) for value in (
        "dashboard", "api", "feishu", "cli", "acp", "dingtalk", "wecom",
        "schedule", "task", "delegation", "chat", "http", "curator", "skill_review",
    )],
    (None, "other"), ("unknown-unique-user-id", "other"),
])
def test_agent_histograms_preserve_bounded_source_and_agent_exemplar(source_sdk, source, expected):
    reader, facade = source_sdk
    with facade.agent_run(source=source, session_id="must-not-be-a-label", run_id="private-run"):
        context = ts.trace.get_current_span().get_span_context()
        # Counts belong to this run, even though child spans become current.
        for _ in range(2):
            with ts.ChatAttempt(model="gpt-4o-mini", stream=False):
                pass
        with facade.tool_execution("calculator") as tool:
            tool["executed"] = True
    points = agent_points(reader)
    assert set(points) == set(AGENT_HISTOGRAMS)
    for name, values in points.items():
        assert len(values) == 1
        assert values[0].count == 1
        assert_agent_point(values[0], expected, context)
        if name == ts.INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS:
            assert values[0].sum == 2
        elif name == ts.INSTRUMENT_INVOKE_AGENT_TOOL_CALLS:
            assert values[0].sum == 1


def test_zero_call_runs_remain_in_mean_denominator(source_sdk):
    reader, facade = source_sdk
    with facade.agent_run(source="task"):
        pass
    with facade.agent_run(source="task"):
        for _ in range(3):
            with ts.ChatAttempt(model="gpt-4o-mini", stream=False):
                pass
        for _ in range(2):
            with facade.tool_execution("calculator") as tool:
                tool["executed"] = True
    points = agent_points(reader)
    for name, expected_sum in ((ts.INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS, 3),
                               (ts.INSTRUMENT_INVOKE_AGENT_TOOL_CALLS, 2)):
        assert len(points[name]) == 1
        assert points[name][0].attributes[ts.NAGENT_SOURCE] == "task"
        assert points[name][0].count == 2
        assert points[name][0].sum == expected_sum


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_failed_agent_histograms_keep_source_and_span(source_sdk, failure):
    reader, facade = source_sdk
    with pytest.raises(failure):
        with facade.agent_run(source="schedule"):
            context = ts.trace.get_current_span().get_span_context()
            raise failure()
    for values in agent_points(reader).values():
        assert len(values) == 1
        assert_agent_point(values[0], "schedule", context)


@pytest.mark.asyncio
async def test_concurrent_and_nested_runs_restore_source_and_exemplar(source_sdk):
    reader, facade = source_sdk
    contexts = {}

    async def execute(source):
        with facade.agent_run(source=source):
            contexts[source] = ts.trace.get_current_span().get_span_context()
            await asyncio.sleep(0)
            if source == "dashboard":
                with facade.agent_run(source="delegation"):
                    contexts["delegation"] = ts.trace.get_current_span().get_span_context()
                    with ts.ChatAttempt(model="gpt-4o-mini", stream=False):
                        pass
                assert ts.trace.get_current_span().get_span_context() == contexts[source]

    await asyncio.gather(execute("dashboard"), execute("task"), execute("schedule"))
    for name, values in agent_points(reader).items():
        assert len(values) == len(contexts)
        for point in values:
            source = point.attributes[ts.NAGENT_SOURCE]
            assert point.count == 1
            assert_agent_point(point, source, contexts[source])
            if name == ts.INSTRUMENT_INVOKE_AGENT_INFERENCE_CALLS:
                assert point.sum == (1 if source == "delegation" else 0)
