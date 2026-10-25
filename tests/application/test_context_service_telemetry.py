"""T5 telemetry wiring tests: ContextService compression.

Covers (plan T5 S1): compress span + nagent.context.compressions counter
with compressed / skipped / failed outcomes; the compression LLM chat span
(parent of the compress span) nests under it via the current span context.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application import telemetry_semantics as ts
from app.application.context_service import ContextService
from app.application.telemetry_semantics import SemanticsTelemetry
from app.domain.agent import AgentState
from app.domain.context import CONTEXT_SUMMARY_PREFIX


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


# Register as a virtual Span subclass so the OTel context API treats fake
# spans as current/parent spans (get_current_span isinstance-checks Span).
ts.trace.Span.register(FakeSpan)


class ParentTrackingTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, kind=None, **_kwargs):
        span = FakeSpan(name)
        span.parent = ts.trace.get_current_span()
        span.parent_name = getattr(span.parent, "name", None)
        self.spans.append(span)
        return span

    def by_name(self, name):
        return [s for s in self.spans if s.name == name]


class FakeCounter:
    def __init__(self):
        self.records = []

    def add(self, value, attrs):
        self.records.append((value, attrs))


class FakeMeter:
    def __init__(self):
        self.counters = {}
        self.histograms = {}

    def create_counter(self, name, unit=None, **_kwargs):
        return self.counters.setdefault(name, FakeCounter())

    def create_histogram(self, name, unit=None, **_kwargs):
        from collections import defaultdict

        return self.histograms.setdefault(name, FakeCounter())


@pytest.fixture
def tracer(monkeypatch):
    fake = ParentTrackingTracer()
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


class FakeMemoryStore:
    """Minimal in-memory store (no persistence interactions needed)."""

    def __init__(self):
        self.messages = []
        self.summaries = {}

    async def create_session(self, session):
        return session

    async def append_message(self, session_id, message):
        self.messages.append(message)
        return message

    async def get_messages(self, session_id):
        return list(self.messages)

    async def save_summary(self, summary):
        self.summaries[summary.session_id] = summary

    async def get_summary(self, session_id):
        return self.summaries.get(session_id)


def _compressed_result(compressed=True):
    summary_text = f"{CONTEXT_SUMMARY_PREFIX} summarized history"
    return SimpleNamespace(
        compressed=compressed,
        summary=summary_text,
        messages=[{"role": "user", "content": summary_text}],
        original_tokens=1200,
        compressed_tokens=200,
        summarized_message_indices=[0],
        skipped_reason=None,
    )


class FakeEngine:
    """ContextEngine double whose compress() simulates the real compressor's
    provider call by entering a chat attempt under the telemetry callback."""

    def __init__(self, telemetry, result=None, error=None):
        self._telemetry = telemetry
        self._result = result if result is not None else _compressed_result()
        self._error = error

    async def compress(self, non_system, existing_summary="", force=False):
        if self._error is not None:
            raise self._error
        if self._telemetry is not None:
            with self._telemetry.begin_chat_attempt(
                model="gpt-4o", stream=False, provider="openai"
            ):
                pass  # simulated provider round-trip
        return self._result


def _state():
    return AgentState(
        session_id="s-compress",
        input_messages=[{"role": "user", "content": "hello"}],
        working_messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "old message 1"},
            {"role": "assistant", "content": "reply 1"},
            {"role": "user", "content": "hello"},
        ],
        run_options={"force_compress": True},
        persist_messages=False,
    )


def _service(engine, telemetry):
    return ContextService(
        FakeMemoryStore(),
        context_engine=engine,
        telemetry=telemetry,
    )


def _compressions(meter):
    return meter.counters["nagent.context.compressions"].records


@pytest.mark.asyncio
async def test_compression_compressed_outcome_and_parent_chat_span(tracer, meter):
    telemetry = SemanticsTelemetry()
    engine = FakeEngine(telemetry)
    service = _service(engine, telemetry)

    state = await service.compress_prepared_context(_state())

    assert state.summary  # compression applied
    spans = tracer.by_name("nagent.context.compress")
    assert len(spans) == 1
    span = spans[0]
    assert span.ended == 1
    assert span.attributes[ts.NAGENT_OUTCOME] == "compressed"
    assert span.attributes[ts.NAGENT_BEFORE_TOKENS] == 1200
    assert span.attributes[ts.NAGENT_AFTER_TOKENS] == 200
    assert _compressions(meter) == [(1, {"nagent.outcome": "compressed"})]
    # the compression chat span is a child of the compress span
    chat_spans = tracer.by_name("chat gpt-4o")
    assert len(chat_spans) == 1
    assert chat_spans[0].parent_name == "nagent.context.compress"


@pytest.mark.asyncio
async def test_compression_skipped_outcome(tracer, meter):
    telemetry = SemanticsTelemetry()
    engine = FakeEngine(None, result=_compressed_result(compressed=False))
    service = _service(engine, telemetry)

    state = await service.compress_prepared_context(_state())

    span = tracer.by_name("nagent.context.compress")[0]
    assert span.attributes[ts.NAGENT_OUTCOME] == "skipped"
    assert _compressions(meter) == [(1, {"nagent.outcome": "skipped"})]


@pytest.mark.asyncio
async def test_compression_failed_outcome_on_engine_error(tracer, meter):
    telemetry = SemanticsTelemetry()
    engine = FakeEngine(None, error=RuntimeError("compressor boom"))
    service = _service(engine, telemetry)

    with pytest.raises(RuntimeError):
        await service.compress_prepared_context(_state())

    span = tracer.by_name("nagent.context.compress")[0]
    assert span.ended == 1
    assert span.attributes[ts.NAGENT_OUTCOME] == "failed"
    assert _compressions(meter) == [(1, {"nagent.outcome": "failed"})]


@pytest.mark.asyncio
async def test_no_compression_plan_records_nothing(tracer, meter):
    telemetry = SemanticsTelemetry()
    engine = FakeEngine(None)
    service = _service(engine, telemetry)

    state = _state()
    state.run_options = {}  # no force; short context -> plan.compression is None
    await service.compress_prepared_context(state)

    assert tracer.by_name("nagent.context.compress") == []
    assert "nagent.context.compressions" not in meter.counters


@pytest.mark.asyncio
async def test_default_telemetry_keeps_constructor_compatible():
    engine = FakeEngine(None)
    service = ContextService(FakeMemoryStore(), context_engine=engine)
    state = await service.compress_prepared_context(_state())
    assert state.summary
