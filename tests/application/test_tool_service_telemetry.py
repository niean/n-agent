"""T5 telemetry wiring tests: ToolService.execute.

Covers (plan T5 S1): allow / deny / require_approval / error / cancel
outcomes with correct span attributes and one gen_ai.execute_tool.duration
record per execution; policy denial never reaches the executor; error.type
uses safe bounded values.
"""
from __future__ import annotations

import asyncio

import pytest

from app.application import telemetry_semantics as ts
from app.application.telemetry_semantics import SemanticsTelemetry
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


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


class RecordingExecutor:
    def __init__(self, behavior="success"):
        self.calls = []
        self.behavior = behavior

    async def execute(self, request, context=None):
        self.calls.append(request)
        if self.behavior == "raise":
            raise RuntimeError("executor boom")
        if self.behavior == "cancel":
            raise asyncio.CancelledError()
        if self.behavior == "error_status":
            return ToolResult(
                request.id, request.name, ToolResultStatus.ERROR, {"error": "bad input"}
            )
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {"ok": True})


def _definitions():
    defs = builtin_tool_definitions()
    defs.append(
        ToolDefinition(
            name="confirm_tool",
            description="A CONFIRM-risk tool that is not managed.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="test",
        )
    )
    defs.append(
        ToolDefinition(
            name="disabled_tool",
            description="A tool disabled by policy.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            enabled=False,
            source_type=ToolSourceType.AGENT,
            toolset="test",
        )
    )
    return defs


def _request(name):
    return ToolCallRequest(id=f"call-{name}", name=name, arguments={})


def _service(executor, telemetry):
    return ToolService(executor, _definitions(), telemetry=telemetry)


def _tool_spans(tracer):
    return [s for s in tracer.spans if s.name.startswith("execute_tool ")]


def _duration_records(meter):
    return meter.histograms["gen_ai.execute_tool.duration"].records


@pytest.mark.asyncio
async def test_allow_outcome_success(tracer, meter):
    executor = RecordingExecutor("success")
    service = _service(executor, SemanticsTelemetry())

    result = await service.execute(_request("calculator"))

    assert result.status is ToolResultStatus.SUCCESS
    assert len(executor.calls) == 1
    spans = _tool_spans(tracer)
    assert len(spans) == 1
    span = spans[0]
    assert span.ended == 1
    assert span.attributes[ts.GEN_AI_TOOL_NAME] == "calculator"
    assert span.attributes[ts.GEN_AI_TOOL_TYPE] == "function"
    assert span.attributes[ts.NAGENT_POLICY_OUTCOME] == "allow"
    assert span.attributes[ts.NAGENT_OUTCOME] == "success"
    assert span.attributes[ts.NAGENT_RISK_LEVEL] == "safe"
    assert len(_duration_records(meter)) == 1
    value, dims = _duration_records(meter)[0]
    assert value >= 0
    assert dims == {
        "gen_ai.tool.name": "calculator",
        "gen_ai.tool.type": "function",
        "gen_ai.agent.name": "n-agent",
    }


@pytest.mark.asyncio
async def test_policy_deny_never_reaches_executor(tracer, meter):
    executor = RecordingExecutor("success")
    service = _service(executor, SemanticsTelemetry())

    result = await service.execute(_request("disabled_tool"))

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert executor.calls == []
    spans = _tool_spans(tracer)
    assert len(spans) == 1
    assert spans[0].attributes[ts.NAGENT_POLICY_OUTCOME] == "deny"
    assert spans[0].attributes[ts.NAGENT_OUTCOME] == "denied"
    # counted exactly once
    assert len(_duration_records(meter)) == 1


@pytest.mark.asyncio
async def test_require_approval_outcome(tracer, meter):
    executor = RecordingExecutor("success")
    service = _service(executor, SemanticsTelemetry())

    result = await service.execute(_request("confirm_tool"))

    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert executor.calls == []
    span = _tool_spans(tracer)[0]
    assert span.attributes[ts.NAGENT_POLICY_OUTCOME] == "require_approval"
    assert span.attributes[ts.NAGENT_OUTCOME] == "denied"


@pytest.mark.asyncio
async def test_executor_exception_records_error_and_propagates(tracer, meter):
    executor = RecordingExecutor("raise")
    service = _service(executor, SemanticsTelemetry())

    with pytest.raises(RuntimeError):
        await service.execute(_request("calculator"))

    span = _tool_spans(tracer)[0]
    assert span.ended == 1
    assert span.attributes[ts.NAGENT_OUTCOME] == "error"
    assert span.attributes["error.type"] == "other"  # RuntimeError -> bounded value
    _value, dims = _duration_records(meter)[0]
    assert dims["error.type"] == "other"


@pytest.mark.asyncio
async def test_error_result_outcome_uses_tool_error_sentinel(tracer, meter):
    executor = RecordingExecutor("error_status")
    service = _service(executor, SemanticsTelemetry())

    result = await service.execute(_request("calculator"))

    assert result.status is ToolResultStatus.ERROR
    span = _tool_spans(tracer)[0]
    assert span.attributes[ts.NAGENT_OUTCOME] == "error"
    _value, dims = _duration_records(meter)[0]
    assert dims["error.type"] == "tool_error"  # sentinel, not a fabricated class name


@pytest.mark.asyncio
async def test_cancelled_outcome(tracer, meter):
    executor = RecordingExecutor("cancel")
    service = _service(executor, SemanticsTelemetry())

    with pytest.raises(asyncio.CancelledError):
        await service.execute(_request("calculator"))

    span = _tool_spans(tracer)[0]
    assert span.ended == 1
    assert span.attributes[ts.NAGENT_OUTCOME] == "cancelled"


@pytest.mark.asyncio
async def test_default_telemetry_keeps_constructor_compatible():
    executor = RecordingExecutor("success")
    service = ToolService(executor, _definitions())
    result = await service.execute(_request("calculator"))
    assert result.status is ToolResultStatus.SUCCESS
