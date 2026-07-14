"""Budget service -- Application layer.

Per-run ``RunBudgetAccount`` holds mutable counters + an ``asyncio.Lock``.
All reserve/settle/release operations are serialized under the lock to
guarantee no oversell: concurrent reserves see consistent state.

Lifecycle (per external call):
1. Before LLM/tool/sandbox call: ``reserve`` -> if DENY, call does NOT happen.
2. After success: ``settle`` with actual usage (replaces estimate).
3. On exception/cancel before external effect: ``release`` (undo).
4. On run end/cancel: ``close`` releases all unsettled reservations.

Conservative settle: if ``actual_usage`` is None/unknown (Provider called
but returned no usage), settle KEEPS the reservation's estimated consumption
(does NOT release to 0). Only when actual is explicitly provided does it
replace the estimate.

The account is process-local (in-memory dict keyed by run_id). No SQLite.
UsageService handles persistent observation; Budget is the runtime gate.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

from app.application.policy_snapshot import BudgetPolicyConfig
from app.domain.budget import (
    BudgetActualUsage,
    BudgetConfig,
    BudgetReserveKind,
    BudgetReserveRequest,
    BudgetReservationDecision,
    BudgetState,
)
from app.domain.budget_policy import BudgetPolicy
from app.domain.policy import PolicyAuditEvent, PolicyDecisionKind, PolicyOutcome

if TYPE_CHECKING:
    from app.application.policy_audit_service import PolicyAuditService

logger = logging.getLogger(__name__)


def _to_domain_config(config: BudgetPolicyConfig) -> BudgetConfig:
    """Map the application-level BudgetPolicyConfig to the domain BudgetConfig."""
    return BudgetConfig(
        max_wall_seconds=config.max_wall_seconds,
        max_llm_calls=config.max_llm_calls,
        max_tool_calls=config.max_tool_calls,
        max_token_cost=config.max_token_cost,
        max_usd_cost=config.max_usd_cost,
        max_sandbox_seconds=config.max_sandbox_seconds,
        max_sandbox_cpu_seconds=config.max_sandbox_cpu_seconds,
        max_sandbox_memory_mb_seconds=config.max_sandbox_memory_mb_seconds,
        max_sandbox_callback_calls=config.max_sandbox_callback_calls,
    )


class RunBudgetAccount:
    """Mutable per-run budget account with an asyncio.Lock.

    All operations are serialized under the lock to guarantee no oversell.
    The account holds cumulative counters (reserved + settled) that the
    Policy reads via ``snapshot()`` and the Service adjusts via
    ``apply_reserve`` / ``apply_settle`` / ``apply_release``.
    """

    def __init__(self, policy: BudgetPolicy, config: BudgetConfig) -> None:
        self._policy = policy
        self._config = config
        self._lock = asyncio.Lock()

        # Cumulative counters (reserved + settled)
        self._llm_calls_reserved: int = 0
        self._tool_calls_reserved: int = 0
        self._elapsed_seconds: float = 0.0
        self._token_cost_reserved: int = 0
        self._usd_cost_reserved: Decimal = Decimal("0")
        self._sandbox_seconds_reserved: float = 0.0
        self._sandbox_cpu_seconds_reserved: float = 0.0
        self._sandbox_memory_mb_seconds_reserved: float = 0.0
        self._sandbox_callback_calls_reserved: int = 0

        # Pending (unsettled) reservations keyed by reservation_id
        self._pending: dict[str, BudgetReservationDecision] = {}
        self._next_id: int = 0

        # Monotonic clock start (lazily set on first reserve)
        self._start_time: float = 0.0

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def _update_elapsed(self) -> None:
        """Project elapsed_seconds from the monotonic clock."""
        if self._start_time > 0.0:
            self._elapsed_seconds = time.monotonic() - self._start_time

    def _snapshot(self) -> BudgetState:
        return BudgetState(
            llm_calls_reserved=self._llm_calls_reserved,
            tool_calls_reserved=self._tool_calls_reserved,
            elapsed_seconds=self._elapsed_seconds,
            token_cost_reserved=self._token_cost_reserved,
            usd_cost_reserved=self._usd_cost_reserved,
            sandbox_seconds_reserved=self._sandbox_seconds_reserved,
            sandbox_cpu_seconds_reserved=self._sandbox_cpu_seconds_reserved,
            sandbox_memory_mb_seconds_reserved=self._sandbox_memory_mb_seconds_reserved,
            sandbox_callback_calls_reserved=self._sandbox_callback_calls_reserved,
        )

    def get_state(self) -> BudgetState:
        """Return current state snapshot (lock-free, for observability)."""
        return self._snapshot()

    # ------------------------------------------------------------------
    # Reserve
    # ------------------------------------------------------------------

    async def reserve(self, request: BudgetReserveRequest) -> BudgetReservationDecision:
        async with self._lock:
            if self._start_time == 0.0:
                self._start_time = time.monotonic()
            self._update_elapsed()

            state = self._snapshot()
            decision = self._policy.evaluate_reserve(state, request)

            if decision.outcome is PolicyOutcome.ALLOW:
                reservation_id = self._generate_id()
                decision = replace(decision, reservation_id=reservation_id)
                self._apply_reserve(decision)
                self._pending[reservation_id] = decision

            return decision

    # ------------------------------------------------------------------
    # Settle
    # ------------------------------------------------------------------

    async def settle(
        self,
        reservation: BudgetReservationDecision,
        actual_usage: BudgetActualUsage,
    ) -> None:
        async with self._lock:
            rid = reservation.reservation_id
            if rid not in self._pending:
                logger.warning(
                    "settle called for unknown reservation %s (kind=%s)",
                    rid, reservation.kind,
                )
                return
            self._apply_settle(reservation, actual_usage)
            del self._pending[rid]

    # ------------------------------------------------------------------
    # Release
    # ------------------------------------------------------------------

    async def release(self, reservation: BudgetReservationDecision) -> None:
        async with self._lock:
            rid = reservation.reservation_id
            if rid not in self._pending:
                logger.warning(
                    "release called for unknown reservation %s (kind=%s)",
                    rid, reservation.kind,
                )
                return
            self._apply_release(reservation)
            del self._pending[rid]

    # ------------------------------------------------------------------
    # Close (release all unsettled)
    # ------------------------------------------------------------------

    async def close(self) -> None:
        async with self._lock:
            for reservation in list(self._pending.values()):
                self._apply_release(reservation)
            self._pending.clear()

    # ------------------------------------------------------------------
    # Apply methods (must be called under lock)
    # ------------------------------------------------------------------

    def _apply_reserve(self, decision: BudgetReservationDecision) -> None:
        kind = decision.kind
        if kind is BudgetReserveKind.LLM_CALL:
            self._llm_calls_reserved += 1
            self._token_cost_reserved += decision.estimated_tokens
            self._usd_cost_reserved += decision.estimated_usd_cost
        elif kind is BudgetReserveKind.TOOL_CALL:
            self._tool_calls_reserved += 1
        elif kind is BudgetReserveKind.SANDBOX_RESOURCE:
            alloc = decision.sandbox_allocation
            if alloc is not None:
                self._sandbox_seconds_reserved += alloc.max_seconds
                self._sandbox_cpu_seconds_reserved += alloc.max_cpu_seconds
                self._sandbox_memory_mb_seconds_reserved += alloc.max_memory_mb_seconds
                self._sandbox_callback_calls_reserved += alloc.max_callback_calls
        # WALL_TIME: no counter to increment (time passes naturally)

    def _apply_settle(
        self,
        reservation: BudgetReservationDecision,
        actual_usage: BudgetActualUsage,
    ) -> None:
        """Move reserved -> consumed via the Policy's settle decision.

        The Policy is the single source of truth for consumed values
        (conservative: if actual is None, consumed == estimate, so the
        adjustment is 0 -- equivalent to keeping the reservation).
        The Service applies the mechanical counter adjustment
        (consumed - estimate) derived from the Policy's decision.
        """
        settle_decision = self._policy.evaluate_settle(reservation, actual_usage)
        if not settle_decision.settled:
            return

        kind = reservation.kind

        if kind is BudgetReserveKind.LLM_CALL:
            self._token_cost_reserved += (
                settle_decision.consumed_tokens - reservation.estimated_tokens
            )
            self._usd_cost_reserved += (
                settle_decision.consumed_usd_cost - reservation.estimated_usd_cost
            )
            # Call count stays (the call happened)

        elif kind is BudgetReserveKind.SANDBOX_RESOURCE:
            alloc = reservation.sandbox_allocation
            if alloc is not None:
                self._sandbox_seconds_reserved += (
                    settle_decision.consumed_duration_seconds - alloc.max_seconds
                )
                self._sandbox_callback_calls_reserved += (
                    settle_decision.consumed_sandbox_callbacks - alloc.max_callback_calls
                )
                # CPU-seconds and memory-MB-seconds: no actual available,
                # keep estimate (conservative)

        # TOOL_CALL and WALL_TIME: no adjustment needed

    def _apply_release(self, reservation: BudgetReservationDecision) -> None:
        """Undo a reservation via the Policy's release decision.

        The Policy is the single source of truth for whether to release.
        If released=True, the Service undoes the reservation (decrements
        counters by the reservation's estimated amounts).
        """
        release_decision = self._policy.evaluate_release(reservation)
        if not release_decision.released:
            return

        kind = reservation.kind

        if kind is BudgetReserveKind.LLM_CALL:
            self._llm_calls_reserved -= 1
            self._token_cost_reserved -= reservation.estimated_tokens
            self._usd_cost_reserved -= reservation.estimated_usd_cost
        elif kind is BudgetReserveKind.TOOL_CALL:
            self._tool_calls_reserved -= 1
        elif kind is BudgetReserveKind.SANDBOX_RESOURCE:
            alloc = reservation.sandbox_allocation
            if alloc is not None:
                self._sandbox_seconds_reserved -= alloc.max_seconds
                self._sandbox_cpu_seconds_reserved -= alloc.max_cpu_seconds
                self._sandbox_memory_mb_seconds_reserved -= alloc.max_memory_mb_seconds
                self._sandbox_callback_calls_reserved -= alloc.max_callback_calls
        # WALL_TIME: no counter to decrement

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _generate_id(self) -> str:
        self._next_id += 1
        return f"r-{self._next_id}"


class BudgetService:
    """Application service that manages per-run budget accounts.

    Constructed with a ``BudgetPolicyConfig`` (from ``RunPolicySnapshot``).
    Creates ``RunBudgetAccount`` instances lazily on first ``reserve``.
    The Service does NOT replace UsageService (post-call observation);
    it adds pre-call reservation + hard caps.
    """

    def __init__(
        self,
        config: BudgetPolicyConfig,
        audit_service: "PolicyAuditService | None" = None,
    ) -> None:
        self._domain_config = _to_domain_config(config)
        self._policy = BudgetPolicy(self._domain_config)
        self._accounts: dict[str, RunBudgetAccount] = {}
        self._audit_service = audit_service

    def _get_or_create_account(self, run_id: str) -> RunBudgetAccount:
        account = self._accounts.get(run_id)
        if account is None:
            account = RunBudgetAccount(self._policy, self._domain_config)
            self._accounts[run_id] = account
        return account

    def open(
        self,
        run_id: str,
        config: BudgetPolicyConfig | None = None,
    ) -> RunBudgetAccount:
        """Create the per-run account from the immutable policy snapshot.

        Once opened, later calls cannot replace its config.  This keeps a
        running turn stable even if the profile provider changes meanwhile.
        """
        account = self._accounts.get(run_id)
        if account is not None:
            return account
        domain_config = _to_domain_config(config) if config is not None else self._domain_config
        account = RunBudgetAccount(BudgetPolicy(domain_config), domain_config)
        self._accounts[run_id] = account
        return account

    async def reserve(
        self,
        run_id: str,
        request: BudgetReserveRequest,
    ) -> BudgetReservationDecision:
        """Reserve budget before an external call.

        If DENY, the external call must NOT happen (fail-closed).
        If ALLOW, the reservation is tracked; caller must ``settle`` or
        ``release`` it.
        """
        account = self._get_or_create_account(run_id)
        decision = await account.reserve(request)
        await self._audit_reserve(decision, run_id, request)
        return decision

    async def _audit_reserve(
        self,
        decision: BudgetReservationDecision,
        run_id: str,
        request: BudgetReserveRequest,
    ) -> None:
        if self._audit_service is None:
            return
        event = PolicyAuditEvent(
            policy="budget-policy",
            version="system-v1",
            decision_kind=PolicyDecisionKind.ALLOCATION,
            reason=decision.reason,
            run_id=run_id,
            session_id=run_id,
            outcome=decision.outcome,
        )
        try:
            await self._audit_service.record(event)
        except Exception:
            logger.warning(
                "audit service failed for budget policy run_id=%s",
                run_id,
                exc_info=True,
            )

    async def settle(
        self,
        run_id: str,
        reservation: BudgetReservationDecision,
        actual_usage: BudgetActualUsage,
    ) -> None:
        """Settle a reservation with actual usage.

        Conservative: if actual_usage fields are None, the reservation's
        estimate is kept (not released to 0).
        """
        account = self._accounts.get(run_id)
        if account is None:
            logger.warning("settle called for unknown run %s", run_id)
            return
        await account.settle(reservation, actual_usage)

    async def release(
        self,
        run_id: str,
        reservation: BudgetReservationDecision,
    ) -> None:
        """Release a reservation (cancel/exception before external call).

        Undoes the reservation entirely (decrements counters).
        """
        account = self._accounts.get(run_id)
        if account is None:
            logger.warning("release called for unknown run %s", run_id)
            return
        await account.release(reservation)

    async def close(self, run_id: str) -> None:
        """Release all unsettled reservations for a run and remove the account.

        Called when a run is terminated or cancelled. After close, the
        account is removed from the internal dict to prevent memory leaks
        across runs. Subsequent close/get_state calls on the same run_id
        are safe no-ops (return None).
        """
        account = self._accounts.pop(run_id, None)
        if account is None:
            return
        await account.close()

    def get_state(self, run_id: str) -> BudgetState | None:
        """Return the current budget state for a run, or None if unknown."""
        account = self._accounts.get(run_id)
        if account is None:
            return None
        return account.get_state()
