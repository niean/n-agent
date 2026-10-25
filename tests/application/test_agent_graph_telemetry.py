"""T5 telemetry wiring tests: AgentGraphRunner.

Covers (plan T5 S1): run produces exactly one invoke_agent span; normal /
exception / cancel paths close it exactly once in finally; stream_events
does not double-create; detach semantics (span ends with the actual run);
ContextVar isolation across concurrent sessions; background tasks do not
inherit session context; iteration context reaches the provider callback;
information-release / budget-denial paths produce no chat span and record
budget denial exactly once at the authoritative point.
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from app.application import telemetry_semantics as ts
from app.application.agent_graph import AgentGraphRunner, run_isolated
from app.application.budget_service import BudgetService
from app.application.policy_snapshot import BudgetPolicyConfig
from app.application.telemetry_semantics import SemanticsTelemetry
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.agent import AgentState
from app.domain.budget import BudgetReserveRequest
from app.domain.policy import PolicyOutcome
from app.domain.provider import LLMResult, ModelInfo
from app.domain.session import ConversationSession
from app.infrastructure.memory.heuristic_summarizer import HeuristicSummarizer
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.infrastructure.tools.builtin import build_builtin_tool_executor


class FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.status = None
        self.ended = 0
        self.parent = None

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
    """Fake tracer that records the current (parent) span at start time."""

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


class DirectProvider:
    """One-shot provider; optionally wraps chat in the telemetry callback
    the way the real OpenAICompatibleProvider adapter does."""

    def __init__(self, telemetry_callback=None, fail=False, cancel=False):
        self.calls = 0
        self.iterations_seen = []
        self._callback = telemetry_callback
        self._fail = fail
        self._cancel = cancel

    async def list_models(self):
        return [ModelInfo("gpt-4o", "gpt-4o", "fake")]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        self.iterations_seen.append(ts.current_iteration())
        if self._callback is not None:
            with self._callback(model="gpt-4o", stream=False, provider="openai"):
                if self._cancel:
                    raise asyncio.CancelledError()
                if self._fail:
                    raise RuntimeError("provider boom")
                return self._result()
        if self._cancel:
            raise asyncio.CancelledError()
        if self._fail:
            raise RuntimeError("provider boom")
        return self._result()

    def _result(self):
        return LLMResult(
            message={"role": "assistant", "content": "hello"}, finish_reason="stop"
        )


class LoopThenFinalProvider(DirectProvider):
    """Tool call loop then a final message (exercises execute_tools)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def _result(self):
        if self.calls == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "calculator",
                                "arguments": '{"expression":"1+2"}',
                            },
                        }
                    ],
                },
                finish_reason="tool_calls",
            )
        return LLMResult(
            message={"role": "assistant", "content": "result is 3"},
            finish_reason="stop",
        )


def _make_runner(tmp_path, provider, telemetry=None, **kwargs):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    runner = AgentGraphRunner(
        provider,
        ToolService(
            build_builtin_tool_executor(tmp_path),
            builtin_tool_definitions(),
            telemetry=telemetry,
        ),
        store,
        HeuristicSummarizer(),
        iteration_limit=3,
        telemetry=telemetry,
        **kwargs,
    )
    return runner, store


async def _make_session(store, session_id):
    await store.create_session(ConversationSession(id=session_id))


# ---------------------------------------------------------------------------
# single invoke_agent span + turn metrics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_creates_exactly_one_invoke_agent_span(tmp_path, tracer, meter):
    telemetry = SemanticsTelemetry()
    runner, store = _make_runner(tmp_path, DirectProvider(), telemetry=telemetry)
    await _make_session(store, "s1")

    state = await runner.run(
        AgentState(session_id="s1", input_messages=[{"role": "user", "content": "hi"}]),
        "gpt-4o",
    )

    assert state.final_message["content"] == "hello"
    spans = tracer.by_name("invoke_agent n-agent")
    assert len(spans) == 1
    span = spans[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_CONVERSATION_ID] == "s1"
    assert span.attributes[ts.GEN_AI_AGENT_NAME] == "n-agent"
    assert span.attributes[ts.NAGENT_RUN_ID]  # trusted run_id from state
    # turn metric: end_reason completed; zero-call histograms still recorded
    assert meter.counters["nagent.turns"].records == [
        (1, {"nagent.source": "api", "nagent.end_reason": "completed"})
    ]
    calls_hist = meter.histograms["gen_ai.invoke_agent.inference_calls"]
    assert [v for v, _ in calls_hist.records] == [0]
    tool_hist = meter.histograms["gen_ai.invoke_agent.tool_calls"]
    assert [v for v, _ in tool_hist.records] == [0]
    assert meter.histograms["gen_ai.invoke_agent.duration"].records


@pytest.mark.asyncio
async def test_run_counts_inference_and_tool_calls(tmp_path, tracer, meter):
    telemetry = SemanticsTelemetry()
    provider = LoopThenFinalProvider(telemetry_callback=telemetry.begin_chat_attempt)
    runner, store = _make_runner(tmp_path, provider, telemetry=telemetry)
    await _make_session(store, "s2")

    await runner.run(
        AgentState(session_id="s2", input_messages=[{"role": "user", "content": "calc"}]),
        "gpt-4o",
    )

    # 2 LLM attempts (callback-wrapped) + 1 tool execution (calculator)
    assert [v for v, _ in meter.histograms["gen_ai.invoke_agent.inference_calls"].records] == [2]
    assert [v for v, _ in meter.histograms["gen_ai.invoke_agent.tool_calls"].records] == [1]
    # each attempt produced exactly one chat span, sibling under invoke_agent
    chat_spans = [s for s in tracer.spans if s.name == "chat gpt-4o"]
    assert len(chat_spans) == 2
    assert all(s.parent_name == "invoke_agent n-agent" for s in chat_spans)
    # iteration context reaches the callback: first call iteration 0, second 1
    assert provider.iterations_seen == [0, 1]
    assert [s.attributes[ts.NAGENT_ITERATION] for s in chat_spans] == [0, 1]
    # execute_tools runs inside the same iteration scope: the tool span
    # carries the same iteration number as the chat span that requested it
    tool_spans = [s for s in tracer.spans if s.name.startswith("execute_tool ")]
    assert len(tool_spans) == 1
    assert tool_spans[0].attributes[ts.NAGENT_ITERATION] == 0
    assert tool_spans[0].attributes[ts.NAGENT_ITERATION] == chat_spans[0].attributes[ts.NAGENT_ITERATION]


# ---------------------------------------------------------------------------
# exception / cancel close the span exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_exception_closes_span_once_and_records_error(tmp_path, tracer, meter):
    telemetry = SemanticsTelemetry()

    class FailingExecutor:
        async def execute(self, request, context=None):
            raise RuntimeError("executor boom")

    runner = AgentGraphRunner(
        LoopThenFinalProvider(),
        ToolService(
            FailingExecutor(),
            builtin_tool_definitions(),
        ),
        SQLiteMemoryStore(tmp_path / "s3.db"),
        HeuristicSummarizer(),
        iteration_limit=3,
        telemetry=telemetry,
    )
    await runner.memory_store.create_session(ConversationSession(id="s3"))

    with pytest.raises(RuntimeError):
        await runner.run(
            AgentState(session_id="s3", input_messages=[{"role": "user", "content": "x"}]),
            "gpt-4o",
        )

    spans = tracer.by_name("invoke_agent n-agent")
    assert len(spans) == 1
    assert spans[0].ended == 1
    assert spans[0].status is not None
    assert spans[0].status.status_code is ts.StatusCode.ERROR
    assert spans[0].attributes["error.type"] == "other"  # RuntimeError -> safe value
    assert meter.counters["nagent.turns"].records[0][1]["nagent.end_reason"] == "error"


@pytest.mark.asyncio
async def test_run_cancel_closes_span_once_and_records_cancelled(tmp_path, tracer, meter):
    telemetry = SemanticsTelemetry()
    provider = DirectProvider(cancel=True)
    runner, store = _make_runner(tmp_path, provider, telemetry=telemetry)
    await _make_session(store, "s4")

    with pytest.raises(asyncio.CancelledError):
        await runner.run(
            AgentState(session_id="s4", input_messages=[{"role": "user", "content": "x"}]),
            "gpt-4o",
        )

    spans = tracer.by_name("invoke_agent n-agent")
    assert len(spans) == 1
    assert spans[0].ended == 1
    assert meter.counters["nagent.turns"].records[0][1]["nagent.end_reason"] == "cancelled"


# ---------------------------------------------------------------------------
# stream_events: no double-create; detach ends with the actual run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_events_does_not_double_create_span(tmp_path, tracer, meter):
    telemetry = SemanticsTelemetry()
    runner, store = _make_runner(tmp_path, DirectProvider(), telemetry=telemetry)
    await _make_session(store, "s5")

    state = AgentState(session_id="s5", input_messages=[{"role": "user", "content": "hi"}])
    events = []
    async for event in runner.stream_events(state, "gpt-4o"):
        events.append(event)

    assert events  # stream consumed to completion
    assert len(tracer.by_name("invoke_agent n-agent")) == 1
    assert tracer.by_name("invoke_agent n-agent")[0].ended == 1


class GateProvider(DirectProvider):
    """Provider that blocks on an external gate before answering, so the
    test can disconnect the stream consumer while the run is mid-flight."""

    def __init__(self, gate, **kwargs):
        super().__init__(**kwargs)
        self._gate = gate

    async def chat(self, messages, tools, stream, model, options):
        self.calls += 1
        self.iterations_seen.append(ts.current_iteration())
        await self._gate.wait()
        return self._result()


@pytest.mark.asyncio
async def test_detached_run_span_ends_with_actual_run(tmp_path, tracer, meter):
    telemetry = SemanticsTelemetry()
    gate = asyncio.Event()
    runner, store = _make_runner(tmp_path, GateProvider(gate), telemetry=telemetry)
    await _make_session(store, "s6")

    state = AgentState(session_id="s6", input_messages=[{"role": "user", "content": "hi"}])
    gen = runner.stream_events(state, "gpt-4o")
    first = await gen.__anext__()
    assert first.type.value == "message_start"
    # advance the generator so the run task is created and starts
    next_task = asyncio.create_task(gen.__anext__())
    for _ in range(500):
        if tracer.by_name("invoke_agent n-agent"):
            break
        await asyncio.sleep(0.01)

    # client disconnect while the run is mid-flight: cancel the pending
    # anext (CancelledError reaches the generator) and close the generator
    next_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await next_task
    await gen.aclose()

    # the run continues detached; release the gate and wait for completion
    gate.set()
    for _ in range(500):
        spans = tracer.by_name("invoke_agent n-agent")
        if spans and spans[0].ended:
            break
        await asyncio.sleep(0.01)

    spans = tracer.by_name("invoke_agent n-agent")
    assert len(spans) == 1
    assert spans[0].ended == 1  # closed by the actual run, not the generator
    # turn metric was still recorded exactly once by the detached run
    assert len(meter.counters["nagent.turns"].records) == 1


# ---------------------------------------------------------------------------
# ContextVar isolation across concurrent sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_sessions_do_not_inherit_each_others_context(
    tmp_path, tracer, meter
):
    telemetry = SemanticsTelemetry()
    providers = {
        "sA": DirectProvider(telemetry_callback=telemetry.begin_chat_attempt),
        "sB": DirectProvider(telemetry_callback=telemetry.begin_chat_attempt),
    }

    async def run_one(session_id):
        store = SQLiteMemoryStore(tmp_path / f"{session_id}.db")
        await store.create_session(ConversationSession(id=session_id))
        runner = AgentGraphRunner(
            providers[session_id],
            ToolService(
                build_builtin_tool_executor(tmp_path),
                builtin_tool_definitions(),
            ),
            store,
            HeuristicSummarizer(),
            telemetry=telemetry,
        )
        return await runner.run(
            AgentState(
                session_id=session_id,
                input_messages=[{"role": "user", "content": "hi"}],
            ),
            "gpt-4o",
        )

    results = await asyncio.gather(run_one("sA"), run_one("sB"))
    assert all(r.final_message["content"] == "hello" for r in results)

    spans = tracer.by_name("invoke_agent n-agent")
    assert len(spans) == 2
    assert {s.attributes[ts.GEN_AI_CONVERSATION_ID] for s in spans} == {"sA", "sB"}
    assert all(s.ended == 1 for s in spans)
    # each run saw its own iteration context (0), never the sibling's
    assert providers["sA"].iterations_seen == [0]
    assert providers["sB"].iterations_seen == [0]
    # per-run inference_calls recorded as 1 each, not 2
    assert sorted(
        v for v, _ in meter.histograms["gen_ai.invoke_agent.inference_calls"].records
    ) == [1, 1]


# ---------------------------------------------------------------------------
# background tasks do not inherit session context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_isolated_clears_inherited_session_context():
    async def capture():
        return ts.current_iteration(), ts.current_run_stats()

    with ts.iteration_scope(7):
        stats = ts._RunStats()
        with ts.run_stats_scope(stats):
            task = asyncio.create_task(run_isolated(capture()))
            assert await task == (None, None)
            # the parent context is untouched
            assert ts.current_iteration() == 7
            assert ts.current_run_stats() is stats


# ---------------------------------------------------------------------------
# information-release / budget denial: no chat span, budget denied once
# ---------------------------------------------------------------------------


class _DeniedRelease:
    allowed = False
    content = None
    decision = SimpleNamespace(transform=None)


class DenyingInformationFlow:
    def release(self, _payload, _target, origin=None, **_kwargs):
        return _DeniedRelease()

    def redact_structured(self, value):
        return value

    def create_stream_guard(self):
        raise AssertionError("not used on the run() path")


class DenyingBudget:
    def __init__(self):
        self.reserves = 0

    async def open(self, run_id, config=None):
        return None

    async def close(self, run_id):
        return None

    async def reserve(self, run_id, request):
        self.reserves += 1
        from app.domain.budget import BudgetReservationDecision

        return BudgetReservationDecision(
            outcome=PolicyOutcome.DENY,
            reason="budget_exhausted",
            kind=request.kind,
        )

    async def settle(self, *args, **kwargs):
        return None

    async def release(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_information_release_denial_produces_no_chat_span(tmp_path, tracer, meter):
    telemetry = SemanticsTelemetry()
    provider = DirectProvider(telemetry_callback=telemetry.begin_chat_attempt)
    runner, store = _make_runner(
        tmp_path,
        provider,
        telemetry=telemetry,
        information_flow_service=DenyingInformationFlow(),
    )
    await _make_session(store, "s7")

    state = await runner.run(
        AgentState(session_id="s7", input_messages=[{"role": "user", "content": "x"}]),
        "gpt-4o",
    )

    assert state.error == "information_release_denied"
    assert provider.calls == 0  # provider.chat never invoked
    assert [s for s in tracer.spans if s.name.startswith("chat ")] == []
    assert len(tracer.by_name("invoke_agent n-agent")) == 1


@pytest.mark.asyncio
async def test_budget_denial_produces_no_chat_span_and_counts_denied_once(
    tmp_path, tracer, meter
):
    telemetry = SemanticsTelemetry()
    provider = DirectProvider(telemetry_callback=telemetry.begin_chat_attempt)
    # Real BudgetService (the authoritative denial point) with a zero LLM cap.
    budget = BudgetService(BudgetPolicyConfig(max_llm_calls=0), telemetry=telemetry)
    runner, store = _make_runner(
        tmp_path, provider, telemetry=telemetry, budget_service=budget
    )
    await _make_session(store, "s8")

    state = await runner.run(
        AgentState(session_id="s8", input_messages=[{"role": "user", "content": "x"}]),
        "gpt-4o",
    )

    assert state.budget_exhausted is True
    assert provider.calls == 0
    assert [s for s in tracer.spans if s.name.startswith("chat ")] == []
    # counted exactly once at the authoritative denial point (BudgetService)
    assert meter.counters["nagent.budget.denied"].records == [
        (1, {"nagent.kind": "llm_denied"})
    ]


# ---------------------------------------------------------------------------
# default telemetry (None) keeps constructor compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_without_telemetry_still_works(tmp_path):
    runner, store = _make_runner(tmp_path, DirectProvider(), telemetry=None)
    await _make_session(store, "s9")
    state = await runner.run(
        AgentState(session_id="s9", input_messages=[{"role": "user", "content": "hi"}]),
        "gpt-4o",
    )
    assert state.final_message["content"] == "hello"
