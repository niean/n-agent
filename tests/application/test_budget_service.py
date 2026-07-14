"""S4/S6: BudgetService concurrent no-oversell and conservative settle tests.

S4: concurrent reserve does not oversell (asyncio.Lock serializes).
S6: unknown usage conservative settle (keep estimate, not 0).

Also covers:
- Basic reserve/settle/release lifecycle
- close(run_id) releases all unsettled reservations
- Separate runs have separate accounts
- Sandbox settle with actual duration/callbacks
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
from decimal import Decimal

from app.application.budget_service import BudgetService, _to_domain_config
from app.application.policy_snapshot import BudgetPolicyConfig
from app.domain.budget import (
    BudgetActualUsage,
    BudgetConfig,
    BudgetReserveKind,
    BudgetReserveRequest,
    SandboxReserveSpec,
)
from app.domain.policy import PolicyOutcome


_SPEC = SandboxReserveSpec(
    max_seconds=15.0,
    max_cpu_seconds=1.0,
    max_memory_mb_seconds=512.0,
    max_callback_calls=10,
)


# ---------------------------------------------------------------------------
# S4: Concurrent no-oversell
# ---------------------------------------------------------------------------


class TestConcurrentNoOversell:

    async def test_tool_reserve_limit_10_grants_exactly_10(self):
        """20 concurrent tool reserves, limit=10 -> exactly 10 ALLOW, 10 DENY."""
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=10))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
            for _ in range(20)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        denied = [d for d in decisions if d.outcome is PolicyOutcome.DENY]
        assert len(allowed) == 10
        assert len(denied) == 10

    async def test_llm_reserve_limit_5_grants_exactly_5(self):
        service = BudgetService(BudgetPolicyConfig(max_llm_calls=5))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL))
            for _ in range(15)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        denied = [d for d in decisions if d.outcome is PolicyOutcome.DENY]
        assert len(allowed) == 5
        assert len(denied) == 10

    async def test_sandbox_seconds_grant_and_deny(self):
        """Sandbox seconds: 100 total, 15 per-call -> 6 grants (90), 4 deny."""
        service = BudgetService(BudgetPolicyConfig(max_sandbox_seconds=100.0))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ))
            for _ in range(10)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        denied = [d for d in decisions if d.outcome is PolicyOutcome.DENY]
        assert len(allowed) == 6
        assert len(denied) == 4

    async def test_sandbox_callbacks_grant_and_deny(self):
        """Sandbox callbacks: 30 total, 10 per-call -> 3 grants, 2 deny."""
        service = BudgetService(BudgetPolicyConfig(max_sandbox_callback_calls=30))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ))
            for _ in range(5)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        denied = [d for d in decisions if d.outcome is PolicyOutcome.DENY]
        assert len(allowed) == 3
        assert len(denied) == 2

    async def test_settle_after_concurrent_no_leak(self):
        """After concurrent reserves + settle, account state is consistent."""
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=10))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
            for _ in range(10)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        assert len(allowed) == 10
        for d in allowed:
            await service.settle("run-1", d, BudgetActualUsage())
        state = service.get_state("run-1")
        assert state.tool_calls_reserved == 10

    async def test_release_after_concurrent_no_leak(self):
        """Cancel/release after concurrent reserves: counters return to 0."""
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=10))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
            for _ in range(10)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        assert len(allowed) == 10
        for d in allowed:
            await service.release("run-1", d)
        state = service.get_state("run-1")
        assert state.tool_calls_reserved == 0
        # Can reserve again after release
        decision = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        assert decision.outcome is PolicyOutcome.ALLOW

    async def test_concurrent_sandbox_settle_actual(self):
        """Sandbox reserves within limit, settle uses actual duration."""
        service = BudgetService(BudgetPolicyConfig(max_sandbox_seconds=100.0))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ))
            for _ in range(6)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        assert len(allowed) == 6
        # Settle each with actual duration=5s (estimated was 15s)
        for d in allowed:
            await service.settle("run-1", d, BudgetActualUsage(duration_seconds=5.0))
        state = service.get_state("run-1")
        assert state.sandbox_seconds_reserved == 30.0  # 6 * 5.0 actual

    async def test_concurrent_sandbox_unknown_keeps_reserved(self):
        """Sandbox settle with unknown usage keeps reserved upper bound."""
        service = BudgetService(BudgetPolicyConfig(max_sandbox_seconds=100.0))
        decisions = await asyncio.gather(*[
            service.reserve("run-1", BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ))
            for _ in range(6)
        ])
        allowed = [d for d in decisions if d.outcome is PolicyOutcome.ALLOW]
        for d in allowed:
            await service.settle("run-1", d, BudgetActualUsage())
        state = service.get_state("run-1")
        assert state.sandbox_seconds_reserved == 90.0  # 6 * 15.0 kept


# ---------------------------------------------------------------------------
# S6: Conservative settle
# ---------------------------------------------------------------------------


class TestConservativeSettle:

    async def test_none_keeps_reservation(self):
        """Reserve 1000 tokens / $0.01, settle None -> consumed stays."""
        service = BudgetService(BudgetPolicyConfig(
            max_token_cost=2000, max_usd_cost=Decimal("0.02"),
        ))
        reservation = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
            estimated_usd_cost=Decimal("0.01"),
        ))
        assert reservation.outcome is PolicyOutcome.ALLOW
        await service.settle("run-1", reservation, BudgetActualUsage())
        state = service.get_state("run-1")
        assert state.token_cost_reserved == 1000
        assert state.usd_cost_reserved == Decimal("0.01")

    async def test_after_conservative_settle_cannot_break_limit(self):
        """After conservative settle, new reserve cannot break limit."""
        service = BudgetService(BudgetPolicyConfig(max_token_cost=1000))
        reservation = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
        ))
        assert reservation.outcome is PolicyOutcome.ALLOW
        await service.settle("run-1", reservation, BudgetActualUsage())
        decision = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1,
        ))
        assert decision.outcome is PolicyOutcome.DENY

    async def test_cancel_before_call_releases(self):
        """Exception/cancel before external call -> release (undo)."""
        service = BudgetService(BudgetPolicyConfig(max_token_cost=1000))
        reservation = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
        ))
        assert reservation.outcome is PolicyOutcome.ALLOW
        await service.release("run-1", reservation)
        state = service.get_state("run-1")
        assert state.token_cost_reserved == 0
        assert state.llm_calls_reserved == 0
        # Can reserve again after release
        decision = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
        ))
        assert decision.outcome is PolicyOutcome.ALLOW

    async def test_provider_called_no_usage_conservative_settle(self):
        """Provider called but usage missing -> conservative settle (keep)."""
        service = BudgetService(BudgetPolicyConfig(max_token_cost=1000))
        reservation = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
        ))
        assert reservation.outcome is PolicyOutcome.ALLOW
        await service.settle("run-1", reservation, BudgetActualUsage(token_cost=None))
        state = service.get_state("run-1")
        assert state.token_cost_reserved == 1000
        decision = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1,
        ))
        assert decision.outcome is PolicyOutcome.DENY

    async def test_settle_actual_replaces_estimate(self):
        """Settle with actual usage replaces the estimate."""
        service = BudgetService(BudgetPolicyConfig(max_token_cost=1000))
        reservation = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=800,
        ))
        assert reservation.outcome is PolicyOutcome.ALLOW
        await service.settle("run-1", reservation, BudgetActualUsage(token_cost=500))
        state = service.get_state("run-1")
        assert state.token_cost_reserved == 500
        # Can reserve more (500 + 500 = 1000 <= 1000)
        decision = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=500,
        ))
        assert decision.outcome is PolicyOutcome.ALLOW

    async def test_settle_sandbox_unknown_keeps_upper_bound(self):
        """Sandbox settle with unknown usage keeps reserved upper bound."""
        service = BudgetService(BudgetPolicyConfig(max_sandbox_seconds=100.0))
        reservation = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
        ))
        assert reservation.outcome is PolicyOutcome.ALLOW
        await service.settle("run-1", reservation, BudgetActualUsage())
        state = service.get_state("run-1")
        assert state.sandbox_seconds_reserved == 15.0

    async def test_settle_sandbox_actual_duration(self):
        """Sandbox settle with actual duration replaces estimate."""
        service = BudgetService(BudgetPolicyConfig(max_sandbox_seconds=100.0))
        reservation = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
        ))
        assert reservation.outcome is PolicyOutcome.ALLOW
        await service.settle("run-1", reservation, BudgetActualUsage(
            duration_seconds=10.0,
            sandbox_callback_count=3,
        ))
        state = service.get_state("run-1")
        assert state.sandbox_seconds_reserved == 10.0
        assert state.sandbox_callback_calls_reserved == 3


# ---------------------------------------------------------------------------
# close(run_id)
# ---------------------------------------------------------------------------


class TestBudgetServiceClose:

    async def test_close_releases_all_unsettled(self):
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=10))
        for _ in range(5):
            await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        assert service.get_state("run-1").tool_calls_reserved == 5
        await service.close("run-1")
        # Account removed after close -> get_state returns None
        assert service.get_state("run-1") is None

    async def test_close_after_partial_settle(self):
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=10))
        d1 = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        d2 = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        d3 = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        await service.settle("run-1", d1, BudgetActualUsage())
        assert service.get_state("run-1").tool_calls_reserved == 3
        await service.close("run-1")
        # Account removed after close -> get_state returns None
        assert service.get_state("run-1") is None

    async def test_close_idempotent(self):
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=10))
        await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        await service.close("run-1")
        # Second close on removed account is a safe no-op
        await service.close("run-1")
        assert service.get_state("run-1") is None


# ---------------------------------------------------------------------------
# Basic service operations
# ---------------------------------------------------------------------------


class TestBudgetServiceBasic:

    async def test_reserve_llm_allow(self):
        service = BudgetService(BudgetPolicyConfig(max_llm_calls=10))
        decision = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL, estimated_tokens=100,
        ))
        assert decision.outcome is PolicyOutcome.ALLOW

    async def test_reserve_tool_deny_at_limit(self):
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=2))
        d1 = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        d2 = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        d3 = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        assert d1.outcome is PolicyOutcome.ALLOW
        assert d2.outcome is PolicyOutcome.ALLOW
        assert d3.outcome is PolicyOutcome.DENY

    async def test_reserve_wall_time(self):
        service = BudgetService(BudgetPolicyConfig(max_wall_seconds=10))
        d1 = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.WALL_TIME, estimated_duration_seconds=5.0,
        ))
        assert d1.outcome is PolicyOutcome.ALLOW
        d2 = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.WALL_TIME, estimated_duration_seconds=11.0,
        ))
        assert d2.outcome is PolicyOutcome.DENY

    async def test_get_state_unknown_run(self):
        service = BudgetService(BudgetPolicyConfig())
        assert service.get_state("unknown") is None

    async def test_sandbox_allocation_in_decision(self):
        service = BudgetService(BudgetPolicyConfig(max_sandbox_seconds=100.0))
        decision = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
        ))
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.sandbox_allocation is not None
        assert decision.sandbox_allocation.max_seconds == 15.0

    async def test_separate_runs_independent(self):
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=5))
        d1 = await service.reserve("run-1", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        d2 = await service.reserve("run-2", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL))
        assert d1.outcome is PolicyOutcome.ALLOW
        assert d2.outcome is PolicyOutcome.ALLOW
        assert service.get_state("run-1").tool_calls_reserved == 1
        assert service.get_state("run-2").tool_calls_reserved == 1

    async def test_open_uses_run_snapshot_config_without_affecting_other_runs(self):
        service = BudgetService(BudgetPolicyConfig(max_tool_calls=100))
        service.open("restricted", BudgetPolicyConfig(max_tool_calls=1))
        service.open("default")

        first = await service.reserve(
            "restricted", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL)
        )
        denied = await service.reserve(
            "restricted", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL)
        )
        other = await service.reserve(
            "default", BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL)
        )

        assert first.outcome is PolicyOutcome.ALLOW
        assert denied.outcome is PolicyOutcome.DENY
        assert other.outcome is PolicyOutcome.ALLOW

    async def test_nullable_limits_tracked_but_never_deny(self):
        """None limits: reserve always ALLOWs but counters are tracked."""
        service = BudgetService(BudgetPolicyConfig(
            max_token_cost=None,
            max_usd_cost=None,
            max_sandbox_seconds=None,
        ))
        d1 = await service.reserve("run-1", BudgetReserveRequest(
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=999999,
            estimated_usd_cost=Decimal("999"),
        ))
        assert d1.outcome is PolicyOutcome.ALLOW
        state = service.get_state("run-1")
        assert state.token_cost_reserved == 999999
        assert state.usd_cost_reserved == Decimal("999")


# ---------------------------------------------------------------------------
# Config parity: BudgetPolicyConfig (Application) <-> BudgetConfig (Domain)
# ---------------------------------------------------------------------------


class TestConfigParity:
    """Ensure _to_domain_config stays in sync with both config dataclasses."""

    def test_field_names_match(self):
        """BudgetConfig and BudgetPolicyConfig must have identical field names."""
        domain_names = {f.name for f in fields(BudgetConfig)}
        app_names = {f.name for f in fields(BudgetPolicyConfig)}
        assert domain_names == app_names, (
            f"field name mismatch: domain={domain_names} app={app_names}"
        )

    def test_to_domain_config_copies_all_fields(self):
        """Non-default values in BudgetPolicyConfig are copied to BudgetConfig."""
        config = BudgetPolicyConfig(
            max_wall_seconds=600,
            max_llm_calls=5,
            max_tool_calls=50,
            max_token_cost=10000,
            max_usd_cost=Decimal("2.50"),
            max_sandbox_seconds=200.0,
            max_sandbox_cpu_seconds=100.0,
            max_sandbox_memory_mb_seconds=10000.0,
            max_sandbox_callback_calls=200,
        )
        domain = _to_domain_config(config)
        for f in fields(BudgetConfig):
            assert getattr(domain, f.name) == getattr(config, f.name), (
                f"field {f.name} mismatch: domain={getattr(domain, f.name)!r} "
                f"config={getattr(config, f.name)!r}"
            )

    def test_to_domain_config_none_values_preserved(self):
        """Nullable fields default to None and are preserved through mapping."""
        config = BudgetPolicyConfig()
        domain = _to_domain_config(config)
        assert domain.max_token_cost is None
        assert domain.max_usd_cost is None
        assert domain.max_sandbox_seconds is None
        assert domain.max_sandbox_cpu_seconds is None
        assert domain.max_sandbox_memory_mb_seconds is None
        assert domain.max_sandbox_callback_calls is None
