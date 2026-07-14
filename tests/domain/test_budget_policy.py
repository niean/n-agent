"""S1: Budget decision table tests for BudgetPolicy.

Tests the pure domain policy decision table:
- LLM/tool/wall-time hard limits
- Nullable token/USD limits (tracked but never deny)
- Nullable Sandbox cumulative limits
- Reserve remaining quota in decisions
- Settle with actual usage (replace estimate)
- Settle with None (conservative: keep estimate)
- SandboxBudgetAllocation composition
"""
from __future__ import annotations

from decimal import Decimal

from app.domain.budget import (
    BudgetActualUsage,
    BudgetConfig,
    BudgetReserveKind,
    BudgetReservationDecision,
    BudgetReserveRequest,
    BudgetState,
    SandboxBudgetAllocation,
    SandboxReserveSpec,
)
from app.domain.budget_policy import BudgetPolicy
from app.domain.policy import PolicyOutcome


def _state(**kwargs) -> BudgetState:
    defaults = dict(
        llm_calls_reserved=0,
        tool_calls_reserved=0,
        elapsed_seconds=0.0,
        token_cost_reserved=0,
        usd_cost_reserved=Decimal("0"),
        sandbox_seconds_reserved=0.0,
        sandbox_cpu_seconds_reserved=0.0,
        sandbox_memory_mb_seconds_reserved=0.0,
        sandbox_callback_calls_reserved=0,
    )
    defaults.update(kwargs)
    return BudgetState(**defaults)


def _config(**kwargs) -> BudgetConfig:
    defaults = dict(
        max_wall_seconds=900,
        max_llm_calls=10,
        max_tool_calls=100,
        max_token_cost=None,
        max_usd_cost=None,
        max_sandbox_seconds=None,
        max_sandbox_cpu_seconds=None,
        max_sandbox_memory_mb_seconds=None,
        max_sandbox_callback_calls=None,
    )
    defaults.update(kwargs)
    return BudgetConfig(**defaults)


_SPEC = SandboxReserveSpec(
    max_seconds=30.0,
    max_cpu_seconds=2.0,
    max_memory_mb_seconds=1024.0,
    max_callback_calls=10,
)


# ---------------------------------------------------------------------------
# LLM call reserve
# ---------------------------------------------------------------------------


class TestLLMCallReserve:

    def test_under_limit_allow(self):
        policy = BudgetPolicy(_config(max_llm_calls=10))
        decision = policy.evaluate_reserve(
            _state(llm_calls_reserved=5),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=100),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_at_limit_deny(self):
        policy = BudgetPolicy(_config(max_llm_calls=10))
        decision = policy.evaluate_reserve(
            _state(llm_calls_reserved=10),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL),
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "llm" in decision.reason.lower()

    def test_one_below_limit_allow(self):
        policy = BudgetPolicy(_config(max_llm_calls=10))
        decision = policy.evaluate_reserve(
            _state(llm_calls_reserved=9),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_token_cost_under_limit_allow(self):
        policy = BudgetPolicy(_config(max_token_cost=1000))
        decision = policy.evaluate_reserve(
            _state(token_cost_reserved=500),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=400),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_token_cost_over_limit_deny(self):
        policy = BudgetPolicy(_config(max_token_cost=1000))
        decision = policy.evaluate_reserve(
            _state(token_cost_reserved=700),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=400),
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "token" in decision.reason.lower()

    def test_token_cost_none_always_allow(self):
        """max_token_cost=None means no token rejection, but still tracked."""
        policy = BudgetPolicy(_config(max_token_cost=None))
        decision = policy.evaluate_reserve(
            _state(token_cost_reserved=999999),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=999999),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.estimated_tokens == 999999

    def test_usd_cost_under_limit_allow(self):
        policy = BudgetPolicy(_config(max_usd_cost=Decimal("1.00")))
        decision = policy.evaluate_reserve(
            _state(usd_cost_reserved=Decimal("0.50")),
            BudgetReserveRequest(
                kind=BudgetReserveKind.LLM_CALL,
                estimated_usd_cost=Decimal("0.30"),
            ),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_usd_cost_over_limit_deny(self):
        policy = BudgetPolicy(_config(max_usd_cost=Decimal("1.00")))
        decision = policy.evaluate_reserve(
            _state(usd_cost_reserved=Decimal("0.80")),
            BudgetReserveRequest(
                kind=BudgetReserveKind.LLM_CALL,
                estimated_usd_cost=Decimal("0.30"),
            ),
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "usd" in decision.reason.lower() or "cost" in decision.reason.lower()

    def test_usd_cost_none_always_allow(self):
        policy = BudgetPolicy(_config(max_usd_cost=None))
        decision = policy.evaluate_reserve(
            _state(usd_cost_reserved=Decimal("999999")),
            BudgetReserveRequest(
                kind=BudgetReserveKind.LLM_CALL,
                estimated_usd_cost=Decimal("999999"),
            ),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_both_call_and_token_checked(self):
        """Either call-count or token-cost exhaustion denies."""
        policy = BudgetPolicy(_config(max_llm_calls=10, max_token_cost=1000))
        decision = policy.evaluate_reserve(
            _state(llm_calls_reserved=5, token_cost_reserved=800),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=300),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_exact_boundary_token_allow(self):
        """token_cost_reserved + estimated == max_token_cost -> ALLOW (not >)."""
        policy = BudgetPolicy(_config(max_token_cost=1000))
        decision = policy.evaluate_reserve(
            _state(token_cost_reserved=500),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=500),
        )
        assert decision.outcome is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Tool call reserve
# ---------------------------------------------------------------------------


class TestToolCallReserve:

    def test_under_limit_allow(self):
        policy = BudgetPolicy(_config(max_tool_calls=100))
        decision = policy.evaluate_reserve(
            _state(tool_calls_reserved=50),
            BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_at_limit_deny(self):
        policy = BudgetPolicy(_config(max_tool_calls=100))
        decision = policy.evaluate_reserve(
            _state(tool_calls_reserved=100),
            BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL),
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "tool" in decision.reason.lower()

    def test_one_below_limit_allow(self):
        policy = BudgetPolicy(_config(max_tool_calls=100))
        decision = policy.evaluate_reserve(
            _state(tool_calls_reserved=99),
            BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL),
        )
        assert decision.outcome is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Wall-time reserve
# ---------------------------------------------------------------------------


class TestWallTimeReserve:

    def test_under_limit_allow(self):
        policy = BudgetPolicy(_config(max_wall_seconds=900))
        decision = policy.evaluate_reserve(
            _state(elapsed_seconds=500.0),
            BudgetReserveRequest(
                kind=BudgetReserveKind.WALL_TIME,
                estimated_duration_seconds=300.0,
            ),
        )
        assert decision.outcome is PolicyOutcome.ALLOW

    def test_over_limit_deny(self):
        policy = BudgetPolicy(_config(max_wall_seconds=900))
        decision = policy.evaluate_reserve(
            _state(elapsed_seconds=700.0),
            BudgetReserveRequest(
                kind=BudgetReserveKind.WALL_TIME,
                estimated_duration_seconds=300.0,
            ),
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "wall" in decision.reason.lower() or "time" in decision.reason.lower()

    def test_exact_boundary_allow(self):
        """elapsed + estimated == max -> ALLOW (not strictly >)."""
        policy = BudgetPolicy(_config(max_wall_seconds=900))
        decision = policy.evaluate_reserve(
            _state(elapsed_seconds=600.0),
            BudgetReserveRequest(
                kind=BudgetReserveKind.WALL_TIME,
                estimated_duration_seconds=300.0,
            ),
        )
        assert decision.outcome is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Sandbox resource reserve
# ---------------------------------------------------------------------------


class TestSandboxResourceReserve:

    def test_under_cumulative_allow(self):
        policy = BudgetPolicy(
            _config(max_sandbox_seconds=100.0, max_sandbox_callback_calls=50)
        )
        decision = policy.evaluate_reserve(
            _state(sandbox_seconds_reserved=30.0, sandbox_callback_calls_reserved=10),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.sandbox_allocation is not None
        assert decision.sandbox_allocation.max_seconds == 30.0
        assert decision.sandbox_allocation.max_callback_calls == 10

    def test_seconds_over_cumulative_deny(self):
        policy = BudgetPolicy(_config(max_sandbox_seconds=100.0))
        decision = policy.evaluate_reserve(
            _state(sandbox_seconds_reserved=80.0),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        assert decision.outcome is PolicyOutcome.DENY
        assert "sandbox" in decision.reason.lower()

    def test_cpu_seconds_over_cumulative_deny(self):
        policy = BudgetPolicy(_config(max_sandbox_cpu_seconds=50.0))
        decision = policy.evaluate_reserve(
            _state(sandbox_cpu_seconds_reserved=49.0),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_memory_over_cumulative_deny(self):
        policy = BudgetPolicy(_config(max_sandbox_memory_mb_seconds=5000.0))
        decision = policy.evaluate_reserve(
            _state(sandbox_memory_mb_seconds_reserved=4000.0),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_callbacks_over_cumulative_deny(self):
        policy = BudgetPolicy(_config(max_sandbox_callback_calls=100))
        decision = policy.evaluate_reserve(
            _state(sandbox_callback_calls_reserved=95),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_all_dimensions_checked(self):
        """Any configured dimension exceeding denies."""
        policy = BudgetPolicy(
            _config(
                max_sandbox_seconds=100.0,
                max_sandbox_cpu_seconds=50.0,
                max_sandbox_memory_mb_seconds=5000.0,
                max_sandbox_callback_calls=100,
            )
        )
        decision = policy.evaluate_reserve(
            _state(
                sandbox_seconds_reserved=30.0,
                sandbox_cpu_seconds_reserved=49.0,
                sandbox_memory_mb_seconds_reserved=1000.0,
                sandbox_callback_calls_reserved=10,
            ),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        assert decision.outcome is PolicyOutcome.DENY

    def test_nullable_limits_always_allow(self):
        """None limits never deny but allocation is still composed."""
        policy = BudgetPolicy(_config())
        decision = policy.evaluate_reserve(
            _state(
                sandbox_seconds_reserved=999999.0,
                sandbox_cpu_seconds_reserved=999999.0,
                sandbox_memory_mb_seconds_reserved=999999.0,
                sandbox_callback_calls_reserved=999999,
            ),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.sandbox_allocation is not None
        assert decision.sandbox_allocation.max_seconds == 30.0

    def test_mixed_null_and_configured(self):
        """Some dimensions configured, some None. Only configured ones deny."""
        policy = BudgetPolicy(
            _config(
                max_sandbox_seconds=100.0,
                max_sandbox_cpu_seconds=None,
                max_sandbox_memory_mb_seconds=None,
                max_sandbox_callback_calls=50,
            )
        )
        decision = policy.evaluate_reserve(
            _state(
                sandbox_seconds_reserved=30.0,
                sandbox_callback_calls_reserved=45,
            ),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=_SPEC,
            ),
        )
        # seconds: 30+30=60 <= 100 OK; callbacks: 45+10=55 > 50 -> DENY
        assert decision.outcome is PolicyOutcome.DENY

    def test_allocation_composed_from_spec(self):
        """SandboxBudgetAllocation matches the request spec."""
        policy = BudgetPolicy(_config())
        spec = SandboxReserveSpec(
            max_seconds=42.0,
            max_cpu_seconds=3.0,
            max_memory_mb_seconds=2048.0,
            max_callback_calls=7,
        )
        decision = policy.evaluate_reserve(
            _state(),
            BudgetReserveRequest(
                kind=BudgetReserveKind.SANDBOX_RESOURCE, sandbox_spec=spec,
            ),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.sandbox_allocation == SandboxBudgetAllocation(
            max_seconds=42.0,
            max_cpu_seconds=3.0,
            max_memory_mb_seconds=2048.0,
            max_callback_calls=7,
        )


# ---------------------------------------------------------------------------
# Remaining quota in decision
# ---------------------------------------------------------------------------


class TestRemainingQuota:

    def test_remaining_llm_calls(self):
        policy = BudgetPolicy(_config(max_llm_calls=10))
        decision = policy.evaluate_reserve(
            _state(llm_calls_reserved=3),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.remaining_llm_calls == 10 - 3 - 1

    def test_remaining_tool_calls(self):
        policy = BudgetPolicy(_config(max_tool_calls=100))
        decision = policy.evaluate_reserve(
            _state(tool_calls_reserved=40),
            BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.remaining_tool_calls == 100 - 40 - 1

    def test_remaining_token_cost_null(self):
        """When max_token_cost is None, remaining is None (unlimited)."""
        policy = BudgetPolicy(_config(max_token_cost=None))
        decision = policy.evaluate_reserve(
            _state(),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=100),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.remaining_token_cost is None

    def test_remaining_token_cost_set(self):
        policy = BudgetPolicy(_config(max_token_cost=1000))
        decision = policy.evaluate_reserve(
            _state(token_cost_reserved=300),
            BudgetReserveRequest(kind=BudgetReserveKind.LLM_CALL, estimated_tokens=200),
        )
        assert decision.outcome is PolicyOutcome.ALLOW
        assert decision.remaining_token_cost == 1000 - 300 - 200


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


class TestSettle:

    def test_actual_replaces_estimate(self):
        policy = BudgetPolicy(_config())
        reservation = BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="ok",
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
            estimated_usd_cost=Decimal("0.01"),
        )
        settle = policy.evaluate_settle(
            reservation,
            BudgetActualUsage(token_cost=800, usd_cost=Decimal("0.008")),
        )
        assert settle.consumed_tokens == 800
        assert settle.consumed_usd_cost == Decimal("0.008")

    def test_none_keeps_estimate(self):
        """Conservative settle: unknown usage keeps reservation estimate."""
        policy = BudgetPolicy(_config())
        reservation = BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="ok",
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
            estimated_usd_cost=Decimal("0.01"),
        )
        settle = policy.evaluate_settle(
            reservation,
            BudgetActualUsage(),
        )
        assert settle.consumed_tokens == 1000
        assert settle.consumed_usd_cost == Decimal("0.01")

    def test_partial_actual(self):
        """Some actuals provided, some None: replace known, keep unknown."""
        policy = BudgetPolicy(_config())
        reservation = BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="ok",
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
            estimated_usd_cost=Decimal("0.01"),
        )
        settle = policy.evaluate_settle(
            reservation,
            BudgetActualUsage(token_cost=800, usd_cost=None),
        )
        assert settle.consumed_tokens == 800
        assert settle.consumed_usd_cost == Decimal("0.01")

    def test_sandbox_actual_duration(self):
        policy = BudgetPolicy(_config())
        reservation = BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="ok",
            kind=BudgetReserveKind.SANDBOX_RESOURCE,
            sandbox_allocation=SandboxBudgetAllocation(
                max_seconds=30.0,
                max_cpu_seconds=2.0,
                max_memory_mb_seconds=1024.0,
                max_callback_calls=10,
            ),
        )
        settle = policy.evaluate_settle(
            reservation,
            BudgetActualUsage(duration_seconds=15.0, sandbox_callback_count=5),
        )
        assert settle.consumed_duration_seconds == 15.0
        assert settle.consumed_sandbox_callbacks == 5

    def test_sandbox_unknown_keeps_upper_bound(self):
        policy = BudgetPolicy(_config())
        reservation = BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="ok",
            kind=BudgetReserveKind.SANDBOX_RESOURCE,
            sandbox_allocation=SandboxBudgetAllocation(
                max_seconds=30.0,
                max_cpu_seconds=2.0,
                max_memory_mb_seconds=1024.0,
                max_callback_calls=10,
            ),
        )
        settle = policy.evaluate_settle(
            reservation,
            BudgetActualUsage(),
        )
        assert settle.consumed_duration_seconds == 30.0
        assert settle.consumed_sandbox_callbacks == 10


# ---------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------


class TestRelease:

    def test_release_decision(self):
        policy = BudgetPolicy(_config())
        reservation = BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="ok",
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=1000,
        )
        release = policy.evaluate_release(reservation)
        assert release.released is True
