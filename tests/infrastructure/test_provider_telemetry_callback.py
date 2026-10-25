"""T5 telemetry wiring tests: OpenAICompatibleProvider telemetry callback.

Covers (plan T5 S1): one chat span per attempt; a retry (second chat call)
starts a new attempt; missing usage is NOT fabricated as 0 (usage_known
false, no token records); input/output knownness judged separately; the
streaming path ends the span at the final chunk and handles usage-only
chunks with empty choices; cancel during stream still records confirmed
usage; no callback -> no-op; SDK transparent retry is disabled
(max_retries=0).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.application import telemetry_semantics as ts
from app.application.telemetry_semantics import SemanticsTelemetry
from app.domain.provider import LLMEventType
from app.infrastructure.llm.openai_compatible import OpenAICompatibleProvider


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


def _usage(prompt=None, completion=None):
    if prompt is None and completion is None:
        return None
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)


def _response(content="hi", finish_reason="stop", usage=None):
    message = SimpleNamespace(role="assistant", content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


class FakeCompletions:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = 0

    async def create(self, **_kwargs):
        self.calls += 1
        behavior = self.behavior[self.calls - 1] if isinstance(self.behavior, list) else self.behavior
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


class FakeClient:
    def __init__(self, behavior):
        self.chat = SimpleNamespace(completions=FakeCompletions(behavior))


def _stream_chunks():
    def chunk(delta_content=None, finish_reason=None, usage=None, with_choice=True):
        choice = (
            SimpleNamespace(
                delta=SimpleNamespace(content=delta_content, tool_calls=None),
                finish_reason=finish_reason,
            )
            if with_choice
            else None
        )
        return SimpleNamespace(choices=[choice] if with_choice else [], usage=usage)

    return [
        chunk(delta_content="he"),
        chunk(delta_content="llo"),
        chunk(finish_reason="stop"),
        chunk(with_choice=False, usage=_usage(prompt=10, completion=5)),  # usage-only
    ]


class FakeStream:
    def __init__(self, chunks, error_at=None):
        self._chunks = chunks
        self._error_at = error_at

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._error_at is not None and len(self._chunks) == self._error_at:
            raise RuntimeError("stream boom")
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class CancellingStream(FakeStream):
    """Yields the prepared chunks, then cancels (consumer-side disconnect)."""

    def __init__(self, chunks, cancel_after):
        super().__init__(chunks)
        self._cancel_after = cancel_after

    async def __anext__(self):
        if not self._chunks:
            raise asyncio.CancelledError()
        return self._chunks.pop(0)


def _provider(behavior, telemetry_callback=None):
    client = FakeClient(behavior)
    provider = OpenAICompatibleProvider(
        "http://localhost:1",
        "k",
        "gpt-4o",
        client=client,
        telemetry_callback=telemetry_callback,
    )
    return provider, client


def _chat_span(tracer):
    spans = [s for s in tracer.spans if s.name == "chat gpt-4o"]
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
    provider, _ = _provider(_response(usage=_usage(10, 5)), telemetry.begin_chat_attempt)

    result = await provider.chat([], [], False, "", {})

    assert result.finish_reason == "stop"
    spans = _chat_span(tracer)
    assert len(spans) == 1
    span = spans[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert span.attributes[ts.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
    assert span.attributes[ts.NAGENT_USAGE_KNOWN] is True
    assert span.attributes[ts.GEN_AI_RESPONSE_FINISH_REASONS] == "stop"
    input_records, output_records = _token_records(meter)
    assert input_records == [
        (10, {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": "gpt-4o"})
    ]
    assert output_records == [
        (5, {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai", "gen_ai.request.model": "gpt-4o"})
    ]
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
async def test_partial_usage_judged_per_component(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, _ = _provider(_response(usage=_usage(prompt=7)), telemetry.begin_chat_attempt)

    await provider.chat([], [], False, "", {})

    span = _chat_span(tracer)[0]
    assert span.attributes[ts.GEN_AI_USAGE_INPUT_TOKENS] == 7
    assert ts.GEN_AI_USAGE_OUTPUT_TOKENS not in span.attributes
    assert span.attributes[ts.NAGENT_USAGE_KNOWN] is False
    input_records, output_records = _token_records(meter)
    assert [v for v, _ in input_records] == [7]
    assert output_records == []


@pytest.mark.asyncio
async def test_each_attempt_gets_a_unique_span(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, client = _provider(
        [RuntimeError("attempt 1 boom"), _response()], telemetry.begin_chat_attempt
    )

    with pytest.raises(RuntimeError):
        await provider.chat([], [], False, "", {})
    result = await provider.chat([], [], False, "", {})
    assert result.finish_reason == "stop"

    assert client.chat.completions.calls == 2
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
async def test_stream_records_usage_from_usage_only_chunk(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, client = _provider(FakeStream(_stream_chunks()), telemetry.begin_chat_attempt)

    events = []
    async for event in await provider.chat([], [], True, "", {}):
        events.append(event)

    assert [e.type for e in events] == [
        LLMEventType.MESSAGE_START,
        LLMEventType.CONTENT_DELTA,
        LLMEventType.CONTENT_DELTA,
        LLMEventType.MESSAGE_DONE,
    ]  # usage-only empty-choices chunk yields no event and no IndexError
    span = _chat_span(tracer)[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_USAGE_INPUT_TOKENS] == 10
    assert span.attributes[ts.GEN_AI_USAGE_OUTPUT_TOKENS] == 5
    input_records, output_records = _token_records(meter)
    assert [v for v, _ in input_records] == [10]
    assert [v for v, _ in output_records] == [5]


@pytest.mark.asyncio
async def test_stream_error_marks_attempt_error(tracer, meter):
    telemetry = SemanticsTelemetry()
    provider, _ = _provider(
        FakeStream(_stream_chunks(), error_at=1), telemetry.begin_chat_attempt
    )

    events = [e async for e in await provider.chat([], [], True, "", {})]

    assert events[-1].type is LLMEventType.ERROR
    span = _chat_span(tracer)[0]
    assert span.ended == 1
    assert span.attributes["error.type"] == "other"


@pytest.mark.asyncio
async def test_stream_cancel_still_records_confirmed_usage(tracer, meter):
    telemetry = SemanticsTelemetry()
    # stream yields content + a usage-only chunk, then the transport is
    # cancelled: usage confirmed before the cancel must still be recorded.
    chunks = _stream_chunks()[:2] + [_stream_chunks()[3]]
    provider, _ = _provider(
        CancellingStream(chunks, cancel_after=len(chunks)),
        telemetry.begin_chat_attempt,
    )

    gen = await provider.chat([], [], True, "", {})
    with pytest.raises(asyncio.CancelledError):
        async for _ in gen:
            pass

    span = _chat_span(tracer)[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_USAGE_INPUT_TOKENS] == 10
    input_records, _ = _token_records(meter)
    assert [v for v, _ in input_records] == [10]


@pytest.mark.asyncio
async def test_no_callback_is_a_noop(tracer, meter):
    provider, _ = _provider(_response(usage=_usage(10, 5)))

    result = await provider.chat([], [], False, "", {})

    assert result.finish_reason == "stop"
    assert tracer.spans == []
    assert _token_records(meter) == ([], [])


def test_default_client_disables_sdk_transparent_retry():
    provider = OpenAICompatibleProvider("http://localhost:1", "k", "gpt-4o")
    assert provider.client.max_retries == 0
