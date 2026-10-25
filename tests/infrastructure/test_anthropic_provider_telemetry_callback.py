"""T5 telemetry wiring tests: AnthropicProvider telemetry callback.

Mirrors tests/infrastructure/test_provider_telemetry_callback.py for the
Anthropic adapter: one chat span per attempt (a retry starts a new one),
usage is never fabricated as 0 (usage_known judged per component), the
streaming path captures usage from message_start/message_delta events and
handles a mid-stream cancel (confirmed usage still recorded), error paths
record error.type, and no callback is a no-op.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.application import telemetry_semantics as ts
from app.application.telemetry_semantics import SemanticsTelemetry
from app.domain.provider import LLMEventType
from app.infrastructure.llm.anthropic_provider import AnthropicProvider


class FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.status = None
        self.ended = 0

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


class FakeCounter:
    def __init__(self):
        self.records = []

    def add(self, value, attrs):
        self.records.append((value, attrs))


class FakeHistogram:
    def __init__(self):
        self.records = []

    def record(self, value, attrs):
        self.records.append((value, attrs))


class FakeMeter:
    def __init__(self):
        self.counters = {}
        self.histograms = {}

    def create_counter(self, name, unit=None, **_kwargs):
        return self.counters.setdefault(name, FakeCounter())

    def create_histogram(self, name, unit=None, **_kwargs):
        return self.histograms.setdefault(name, FakeHistogram())


@pytest.fixture
def tracer(monkeypatch):
    fake = FakeTracer()
    monkeypatch.setattr(ts, "get_tracer", lambda: fake)
    return fake


@pytest.fixture
def meter(monkeypatch):
    fake = FakeMeter()
    monkeypatch.setattr(ts, "get_meter", lambda: fake)
    ts._instrument_cache.clear()
    ts._reset_dimension_guard_for_tests()
    yield fake
    ts._instrument_cache.clear()
    ts._reset_dimension_guard_for_tests()


def _usage(input_tokens=None, output_tokens=None):
    if input_tokens is None and output_tokens is None:
        return None
    return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)


def _response(usage=None, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hi")],
        stop_reason=stop_reason,
        usage=usage,
    )


class FakeMessages:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        behavior = self.behavior[self.calls - 1] if isinstance(self.behavior, list) else self.behavior
        if isinstance(behavior, Exception):
            raise behavior
        if kwargs.get("stream"):
            return behavior
        return behavior


def _provider(behavior, telemetry_callback=None):
    client = SimpleNamespace(messages=FakeMessages(behavior))
    provider = AnthropicProvider(
        "http://localhost:1",
        "k",
        "claude-sonnet-4-6",
        client=client,
        telemetry_callback=telemetry_callback,
    )
    return provider, client


def _stream_events():
    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(usage=_usage(input_tokens=10)),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="he"),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(type="message_delta", stop_reason="end_turn"),
            usage=_usage(output_tokens=5),
        ),
    ]


class FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class CancellingStream(FakeStream):
    """Yields the prepared events, then cancels (consumer-side disconnect)."""

    async def __anext__(self):
        if not self._events:
            raise asyncio.CancelledError()
        return self._events.pop(0)


def _chat_span(tracer):
    spans = [s for s in tracer.spans if s.name.startswith("chat ")]
    assert spans
    return spans


def _token_records(meter):
    return (
        meter.counters.get("gen_ai.client.inference.usage.input_tokens", FakeCounter()).records,
        meter.counters.get("gen_ai.client.inference.usage.output_tokens", FakeCounter()).records,
    )


# ---------------------------------------------------------------------------
# non-streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_stream_success_records_usage(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, _ = _provider(
        _response(usage=_usage(input_tokens=10, output_tokens=5)),
        telemetry.begin_chat_attempt,
    )

    result = await provider.chat([], [], False, "", {})

    assert result.finish_reason == "stop"  # end_turn mapped
    spans = _chat_span(tracer)
    assert len(spans) == 1
    span = spans[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert span.attributes[ts.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
    assert span.attributes[ts.NAGENT_USAGE_KNOWN] is True
    assert span.attributes[ts.GEN_AI_RESPONSE_FINISH_REASONS] == "stop"
    input_records, output_records = _token_records(meter)
    assert [v for v, _ in input_records] == [10]
    assert [v for v, _ in output_records] == [5]
    assert meter.histograms["gen_ai.client.inference.duration"].records


@pytest.mark.asyncio
async def test_missing_usage_not_fabricated_as_zero(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, _ = _provider(_response(usage=_usage()), telemetry.begin_chat_attempt)

    result = await provider.chat([], [], False, "", {})

    assert result.usage == {}
    span = _chat_span(tracer)[0]
    assert span.attributes[ts.NAGENT_USAGE_KNOWN] is False
    assert ts.GEN_AI_USAGE_INPUT_TOKENS not in span.attributes
    assert ts.GEN_AI_USAGE_OUTPUT_TOKENS not in span.attributes
    input_records, output_records = _token_records(meter)
    assert input_records == []
    assert output_records == []


@pytest.mark.asyncio
async def test_each_attempt_gets_a_unique_span(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, client = _provider(
        [RuntimeError("attempt 1 boom"), _response(usage=_usage(10, 5))],
        telemetry.begin_chat_attempt,
    )

    with pytest.raises(RuntimeError):
        await provider.chat([], [], False, "", {})
    result = await provider.chat([], [], False, "", {})
    assert result.finish_reason == "stop"

    assert client.messages.calls == 2
    spans = _chat_span(tracer)
    assert len(spans) == 2  # retry started a NEW attempt with a new span
    assert all(s.ended == 1 for s in spans)
    assert spans[0].attributes["error.type"] == "other"
    error_records = [
        dims
        for _v, dims in meter.histograms["gen_ai.client.inference.duration"].records
        if dims.get("error.type") == "other"
    ]
    assert error_records  # failed attempt duration recorded with error.type


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_records_usage_from_message_events(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, _ = _provider(FakeStream(_stream_events()), telemetry.begin_chat_attempt)

    events = []
    async for event in await provider.chat([], [], True, "", {}):
        events.append(event)

    assert [e.type for e in events] == [
        LLMEventType.MESSAGE_START,
        LLMEventType.CONTENT_DELTA,
        LLMEventType.MESSAGE_DONE,
    ]
    span = _chat_span(tracer)[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert span.attributes[ts.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
    assert span.attributes[ts.GEN_AI_RESPONSE_FINISH_REASONS] == "stop"
    input_records, output_records = _token_records(meter)
    assert [v for v, _ in input_records] == [10]
    assert [v for v, _ in output_records] == [5]


@pytest.mark.asyncio
async def test_stream_error_marks_attempt_error(tracer, meter):
    telemetry = SemanticsTelemetry()

    class ExplodingStream(FakeStream):
        async def __anext__(self):
            raise RuntimeError("stream boom")

    provider, _ = _provider(ExplodingStream([]), telemetry.begin_chat_attempt)

    events = [e async for e in await provider.chat([], [], True, "", {})]

    assert events[-1].type is LLMEventType.ERROR
    span = _chat_span(tracer)[0]
    assert span.ended == 1
    assert span.attributes["error.type"] == "other"


@pytest.mark.asyncio
async def test_stream_cancel_still_records_confirmed_usage(tracer, meter):
    telemetry = SemanticsTelemetry()
    # stream yields message_start (input tokens confirmed) and then the
    # transport is cancelled: confirmed usage must still be recorded.
    events = _stream_events()[:1]
    provider, _ = _provider(CancellingStream(events), telemetry.begin_chat_attempt)

    gen = await provider.chat([], [], True, "", {})
    with pytest.raises(asyncio.CancelledError):
        async for _ in gen:
            pass

    span = _chat_span(tracer)[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert span.attributes[ts.NAGENT_USAGE_KNOWN] is False  # output unknown
    input_records, _ = _token_records(meter)
    assert [v for v, _ in input_records] == [10]


@pytest.mark.asyncio
async def test_no_callback_is_a_noop(tracer, meter):
    provider, _ = _provider(_response(usage=_usage(10, 5)))

    result = await provider.chat([], [], False, "", {})

    assert result.finish_reason == "stop"
    assert tracer.spans == []
    assert _token_records(meter) == ([], [])
