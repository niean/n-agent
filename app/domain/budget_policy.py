"""Budget policy -- domain pure decision logic.

Decides whether a budget reserve request is ALLOWed or DENYed based on
the immutable BudgetState snapshot, the request, and the BudgetConfig.
No IO, no asyncio, no side effects. The Policy only produces decisions;
the Application Service applies them to the mutable account.

Decision table:
- LLM call: deny if llm_calls_reserved + 1 > max_llm_calls (exhausted).
  Token cost: deny if max_token_cost is not None and
  token_cost_reserved + estimated_tokens > max_token_cost.
  USD cost: deny if max_usd_cost is not None and
  usd_cost_reserved + estimated_usd_cost > max_usd_cost.
- Tool call: deny if tool_calls_reserved + 1 > max_tool_calls.
- Wall-time: deny if elapsed_seconds + estimated_duration > max_wall_seconds.
- Sandbox resource: deny if any configured (non-None) cumulative limit
  would be exceeded by the per-call max. Composes SandboxBudgetAllocation.
- Nullable limits (None): always ALLOW for that dimension, but the
  reserve/settle/account chain still runs (tracked, never denies).
- HARD limits deny (fail-closed: no external call happens).
- Soft limits (None) never deny but still account.
"""
from __future__ import annotations

from decimal import Decimal

from app.domain.budget import (
    BudgetActualUsage,
    BudgetConfig,
    BudgetReleaseDecision,
    BudgetReserveKind,
    BudgetReserveRequest,
    BudgetReservationDecision,
    BudgetSettleDecision,
    BudgetState,
    SandboxBudgetAllocation,
)
from app.domain.policy import PolicyOutcome


class BudgetPolicy:
    """Domain policy that decides budget reserve/settle/release verdicts.

    Constructed with an immutable ``BudgetConfig``. All evaluate methods are
    pure: they inspect the state, request, and config to produce decisions.
    The Policy never modifies any account or performs IO.
    """

    def __init__(self, config: BudgetConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Reserve
    # ------------------------------------------------------------------

    def evaluate_reserve(
        self,
        state: BudgetState,
        request: BudgetReserveRequest,
    ) -> BudgetReservationDecision:
        if request.kind is BudgetReserveKind.LLM_CALL:
            return self._evaluate_llm_call(state, request)
        if request.kind is BudgetReserveKind.TOOL_CALL:
            return self._evaluate_tool_call(state, request)
        if request.kind is BudgetReserveKind.WALL_TIME:
            return self._evaluate_wall_time(state, request)
        if request.kind is BudgetReserveKind.SANDBOX_RESOURCE:
            return self._evaluate_sandbox(state, request)
        return BudgetReservationDecision(
            outcome=PolicyOutcome.DENY,
            reason=f"unknown_reserve_kind:{request.kind}",
            kind=request.kind,
        )

    def _evaluate_llm_call(
        self,
        state: BudgetState,
        request: BudgetReserveRequest,
    ) -> BudgetReservationDecision:
        cfg = self._config

        # Call count hard limit
        if state.llm_calls_reserved + 1 > cfg.max_llm_calls:
            return BudgetReservationDecision(
                outcome=PolicyOutcome.DENY,
                reason="llm_calls_exhausted",
                kind=BudgetReserveKind.LLM_CALL,
                remaining_llm_calls=0,
            )

        # Token cost hard limit (nullable)
        remaining_token: int | None = None
        if cfg.max_token_cost is not None:
            projected = state.token_cost_reserved + request.estimated_tokens
            if projected > cfg.max_token_cost:
                return BudgetReservationDecision(
                    outcome=PolicyOutcome.DENY,
                    reason="token_cost_exhausted",
                    kind=BudgetReserveKind.LLM_CALL,
                    remaining_llm_calls=cfg.max_llm_calls - state.llm_calls_reserved - 1,
                    remaining_token_cost=max(cfg.max_token_cost - state.token_cost_reserved, 0),
                )
            remaining_token = cfg.max_token_cost - projected

        # USD cost hard limit (nullable)
        remaining_usd: Decimal | None = None
        if cfg.max_usd_cost is not None:
            projected_usd = state.usd_cost_reserved + request.estimated_usd_cost
            if projected_usd > cfg.max_usd_cost:
                return BudgetReservationDecision(
                    outcome=PolicyOutcome.DENY,
                    reason="usd_cost_exhausted",
                    kind=BudgetReserveKind.LLM_CALL,
                    remaining_llm_calls=cfg.max_llm_calls - state.llm_calls_reserved - 1,
                    remaining_token_cost=remaining_token,
                )
            remaining_usd = cfg.max_usd_cost - projected_usd

        return BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="llm_call_allowed",
            kind=BudgetReserveKind.LLM_CALL,
            estimated_tokens=request.estimated_tokens,
            estimated_usd_cost=request.estimated_usd_cost,
            remaining_llm_calls=cfg.max_llm_calls - state.llm_calls_reserved - 1,
            remaining_token_cost=remaining_token,
            remaining_usd_cost=remaining_usd,
        )

    def _evaluate_tool_call(
        self,
        state: BudgetState,
        request: BudgetReserveRequest,
    ) -> BudgetReservationDecision:
        cfg = self._config
        if state.tool_calls_reserved + 1 > cfg.max_tool_calls:
            return BudgetReservationDecision(
                outcome=PolicyOutcome.DENY,
                reason="tool_calls_exhausted",
                kind=BudgetReserveKind.TOOL_CALL,
                remaining_tool_calls=0,
            )
        return BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="tool_call_allowed",
            kind=BudgetReserveKind.TOOL_CALL,
            remaining_tool_calls=cfg.max_tool_calls - state.tool_calls_reserved - 1,
        )

    def _evaluate_wall_time(
        self,
        state: BudgetState,
        request: BudgetReserveRequest,
    ) -> BudgetReservationDecision:
        cfg = self._config
        if state.elapsed_seconds + request.estimated_duration_seconds > cfg.max_wall_seconds:
            return BudgetReservationDecision(
                outcome=PolicyOutcome.DENY,
                reason="wall_time_exceeded",
                kind=BudgetReserveKind.WALL_TIME,
                estimated_duration_seconds=request.estimated_duration_seconds,
            )
        return BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="wall_time_allowed",
            kind=BudgetReserveKind.WALL_TIME,
            estimated_duration_seconds=request.estimated_duration_seconds,
        )

    def _evaluate_sandbox(
        self,
        state: BudgetState,
        request: BudgetReserveRequest,
    ) -> BudgetReservationDecision:
        cfg = self._config
        spec = request.sandbox_spec
        if spec is None:
            return BudgetReservationDecision(
                outcome=PolicyOutcome.DENY,
                reason="sandbox_spec_missing",
                kind=BudgetReserveKind.SANDBOX_RESOURCE,
            )

        # Check each configured (non-None) cumulative limit
        if cfg.max_sandbox_seconds is not None:
            if state.sandbox_seconds_reserved + spec.max_seconds > cfg.max_sandbox_seconds:
                return BudgetReservationDecision(
                    outcome=PolicyOutcome.DENY,
                    reason="sandbox_seconds_exhausted",
                    kind=BudgetReserveKind.SANDBOX_RESOURCE,
                )

        if cfg.max_sandbox_cpu_seconds is not None:
            if state.sandbox_cpu_seconds_reserved + spec.max_cpu_seconds > cfg.max_sandbox_cpu_seconds:
                return BudgetReservationDecision(
                    outcome=PolicyOutcome.DENY,
                    reason="sandbox_cpu_seconds_exhausted",
                    kind=BudgetReserveKind.SANDBOX_RESOURCE,
                )

        if cfg.max_sandbox_memory_mb_seconds is not None:
            if state.sandbox_memory_mb_seconds_reserved + spec.max_memory_mb_seconds > cfg.max_sandbox_memory_mb_seconds:
                return BudgetReservationDecision(
                    outcome=PolicyOutcome.DENY,
                    reason="sandbox_memory_exhausted",
                    kind=BudgetReserveKind.SANDBOX_RESOURCE,
                )

        if cfg.max_sandbox_callback_calls is not None:
            if state.sandbox_callback_calls_reserved + spec.max_callback_calls > cfg.max_sandbox_callback_calls:
                return BudgetReservationDecision(
                    outcome=PolicyOutcome.DENY,
                    reason="sandbox_callbacks_exhausted",
                    kind=BudgetReserveKind.SANDBOX_RESOURCE,
                )

        # All configured limits passed -- compose allocation from spec
        allocation = SandboxBudgetAllocation(
            max_seconds=spec.max_seconds,
            max_cpu_seconds=spec.max_cpu_seconds,
            max_memory_mb_seconds=spec.max_memory_mb_seconds,
            max_callback_calls=spec.max_callback_calls,
        )
        return BudgetReservationDecision(
            outcome=PolicyOutcome.ALLOW,
            reason="sandbox_resource_allowed",
            kind=BudgetReserveKind.SANDBOX_RESOURCE,
            sandbox_allocation=allocation,
        )

    # ------------------------------------------------------------------
    # Settle
    # ------------------------------------------------------------------

    def evaluate_settle(
        self,
        reservation: BudgetReservationDecision,
        actual_usage: BudgetActualUsage,
    ) -> BudgetSettleDecision:
        """Compute final consumed amounts after settle.

        Conservative: if actual is None, the reservation's estimate is kept.
        Only when actual is explicitly provided does it replace the estimate.
        """
        consumed_tokens = (
            actual_usage.token_cost
            if actual_usage.token_cost is not None
            else reservation.estimated_tokens
        )
        consumed_usd = (
            actual_usage.usd_cost
            if actual_usage.usd_cost is not None
            else reservation.estimated_usd_cost
        )
        consumed_duration = (
            actual_usage.duration_seconds
            if actual_usage.duration_seconds is not None
            else reservation.estimated_duration_seconds
        )
        # For sandbox, if duration was not in estimated_duration_seconds,
        # fall back to the allocation's max_seconds.
        if (
            actual_usage.duration_seconds is None
            and reservation.sandbox_allocation is not None
            and reservation.estimated_duration_seconds == 0.0
        ):
            consumed_duration = reservation.sandbox_allocation.max_seconds

        consumed_callbacks = 0
        if reservation.sandbox_allocation is not None:
            consumed_callbacks = (
                actual_usage.sandbox_callback_count
                if actual_usage.sandbox_callback_count is not None
                else reservation.sandbox_allocation.max_callback_calls
            )

        return BudgetSettleDecision(
            settled=True,
            reason="settled",
            reservation_id=reservation.reservation_id,
            consumed_tokens=consumed_tokens,
            consumed_usd_cost=consumed_usd,
            consumed_duration_seconds=consumed_duration,
            consumed_sandbox_callbacks=consumed_callbacks,
        )

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    def evaluate_release(
        self,
        reservation: BudgetReservationDecision,
    ) -> BudgetReleaseDecision:
        """Produce a release decision (undo reservation)."""
        return BudgetReleaseDecision(
            released=True,
            reason="released",
            reservation_id=reservation.reservation_id,
        )
