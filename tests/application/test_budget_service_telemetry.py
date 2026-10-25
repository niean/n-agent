"""T5 telemetry wiring tests: BudgetService denial accounting.

Covers (plan T5 S1): nagent.budget.denied is recorded by kind, only at the
authoritative denial point (BudgetService.reserve); allowed reserves,
settle and release record nothing; the per-instrument dimension-cardinality
cap merges overflow series into the fixed overflow dimension set.
"""
from __future__ import annotations

import pytest

from app.application import telemetry_semantics as ts
from app.application.budget_service import BudgetService
from app.application.policy_snapshot import BudgetPolicyConfig
from app.application.telemetry_semantics import SemanticsTelemetry
from app.domain.budget import BudgetActualUsage, BudgetReserveKind, BudgetReserveRequest


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
def meter(monkeypatch):
    fake = FakeMeter()
    monkeypatch.setattr(ts, "get_meter", lambda: fake)
    ts._instrument_cache.clear()
    ts._reset_dimension_guard_for_tests()
    yield fake
    ts._instrument_cache.clear()
    ts._reset_dimension_guard_for_tests()


def _denied_records(meter):
    return meter.counters["nagent.budget.denied"].records


@pytest.mark.asyncio
async def test_llm_call_denial_recorded_once_by_kind(meter):
    service = BudgetService(BudgetPolicyConfig(max_llm_calls=1), telemetry=SemanticsTelemetry())
    request = BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=10)

    first = await service.reserve("run-1", request)
    second = await service.reserve("run-1", request)

    from app.domain.policy import PolicyOutcome

    assert first.outcome is PolicyOutcome.ALLOW
    assert second.outcome is PolicyOutcome.DENY
    # allowed reserve records nothing; denial counted exactly once
    assert _denied_records(meter) == [(1, {"nagent.kind": "llm_denied"})]


@pytest.mark.asyncio
async def test_tool_call_denial_recorded_by_kind(meter):
    service = BudgetService(BudgetPolicyConfig(max_tool_calls=0), telemetry=SemanticsTelemetry())
    decision = await service.reserve(
        "run-2", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL)
    )

    from app.domain.policy import PolicyOutcome

    assert decision.outcome is PolicyOutcome.DENY
    assert _denied_records(meter) == [(1, {"nagent.kind": "budget_exhausted"})]


def test_kind_label_map_covers_all_reserve_kinds():
    labels = BudgetService._BUDGET_DENIED_KIND_LABELS
    assert labels[BudgetReserveKind.LLM_CALL] == "llm_denied"
    assert labels[BudgetReserveKind.TOOL_CALL] == "budget_exhausted"
    assert labels[BudgetReserveKind.SANDBOX_RESOURCE] == "sandbox_denied"
    assert labels[BudgetReserveKind.WALL_TIME] == "budget_exhausted"


@pytest.mark.asyncio
async def test_settle_and_release_record_nothing(meter):
    service = BudgetService(BudgetPolicyConfig(max_llm_calls=10), telemetry=SemanticsTelemetry())
    request = BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=10)
    reservation = await service.reserve("run-3", request)

    await service.settle("run-3", reservation, BudgetActualUsage())
    reservation2 = await service.reserve("run-3", request)
    await service.release("run-3", reservation2)
    await service.close("run-3")

    assert "nagent.budget.denied" not in meter.counters


@pytest.mark.asyncio
async def test_default_telemetry_keeps_constructor_compatible():
    service = BudgetService(BudgetPolicyConfig(max_llm_calls=0))
    decision = await service.reserve(
        "run-4", BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL)
    )
    from app.domain.policy import PolicyOutcome

    assert decision.outcome is PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# dimension-cardinality cap + overflow merge (integration)
# ---------------------------------------------------------------------------


def test_execute_tool_duration_series_overflow_merges(monkeypatch):
    meter = FakeMeter()
    monkeypatch.setattr(ts, "get_meter", lambda: meter)
    ts._instrument_cache.clear()
    ts._reset_registries_for_tests()
    ts._reset_dimension_guard_for_tests()
    try:
        # 256 registered dynamic tool names x 4 error.type values (plus the
        # no-error variant) exceeds the 999-normal-series cap per instrument,
        # so later combinations must merge into the fixed overflow series.
        # Registries are reset up-front so the 256-name capacity is fully
        # available (live ToolService registrations already hold slots).
        names = [f"tool_{i:03d}" for i in range(256)]
        for name in names:
            assert ts.register_tool_name(name)
        error_types = [None, "TimeoutError", "CancelledError", "APITimeoutError"]
        for name in names:
            for error_type in error_types:
                ts.record_execute_tool_duration(
                    name, "function", "n-agent", 0.01, error_type
                )

        records = meter.histograms["gen_ai.execute_tool.duration"].records
        overflow = [
            (value, dims)
            for value, dims in records
            if ts.OVERFLOW_DIMENSIONS.items() <= dims.items()
        ]
        assert overflow, "expected overflow-merged records past the 999-series cap"
        # the guard itself has switched to the fixed overflow series
        assert ts.dimension_series_count("gen_ai.execute_tool.duration") <= 1000
        assert (
            ts._OVERFLOW_SERIES_KEY
            in ts._dimension_series["gen_ai.execute_tool.duration"]
        )
    finally:
        for name in names:
            ts.unregister_tool_name(name)
        ts._instrument_cache.clear()
        ts._reset_registries_for_tests()
        ts._reset_dimension_guard_for_tests()
