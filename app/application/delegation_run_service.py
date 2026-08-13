"""DelegationRunService -- persistence-driven scheduling, recovery, cancel.

Application Layer. The Registry is the state authority; this service owns
the process-internal tick loop that:

  1. Expires delegations past their deadline.
  2. Reclaims stale (expired-lease) RUNNING members back to PENDING.
  3. Delivers cancel-outbox entries at-least-once.
  4. Claims PENDING members (round-robin across delegations) and executes
     them in parallel within the tick, respecting the global concurrency
     cap.
  5. Advances delegations whose members are all terminal: evaluates the
     join policy and CASes the delegation to a terminal status.

The kill switch, when activated, prevents new spawns and cancels pending
members on the next tick -- closing the race between claim and kill.
"""
from __future__ import annotations

import asyncio
from typing import Any, Mapping, Protocol

from app.domain.delegation import (
    DelegationMember,
    DelegationMemberStatus,
    DelegationResult,
    DelegationStatus,
    MutationOutcome,
)


class _ChildExecutorLike(Protocol):
    async def execute(self, *, member: DelegationMember, model: str,
                      parent_capability: Mapping[str, Any],
                      deadline_at: str | None) -> DelegationResult: ...


class _ClockLike(Protocol):
    def now_iso(self) -> str: ...


class DelegationRunService:
    """Persistence-driven delegation scheduler."""

    def __init__(
        self,
        registry: Any,
        child_executor: _ChildExecutorLike,
        clock: _ClockLike,
        config: Any,
    ) -> None:
        self._registry = registry
        self._child_executor = child_executor
        self._clock = clock
        self._config = config
        self._kill_switch = False
        self._parent_caps: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # kill switch
    # ------------------------------------------------------------------

    def activate_kill_switch(self) -> None:
        self._kill_switch = True

    def deactivate_kill_switch(self) -> None:
        self._kill_switch = False

    def set_parent_capability(self, delegation_id: str, cap: Mapping[str, Any]) -> None:
        self._parent_caps[delegation_id] = dict(cap)

    # ------------------------------------------------------------------
    # tick
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """One scheduling pass. Idempotent and safe to call repeatedly."""
        await self._registry.mark_expired_delegations()
        await self._registry.mark_stale_members()
        await self._deliver_cancel_outbox()
        if self._kill_switch:
            await self._cancel_under_kill_switch()
        else:
            await self._claim_and_execute_pending()
        await self._try_advance_joins()

    # ------------------------------------------------------------------
    # cancel outbox delivery (at-least-once)
    # ------------------------------------------------------------------

    async def _deliver_cancel_outbox(self) -> None:
        pending = await self._registry.list_outbox_pending(limit=50)
        for entry in pending:
            # Cancel any non-terminal members so finalize can proceed.
            await self._registry.cancel_pending_members(
                entry.delegation_id, reason=entry.reason
            )
            await self._registry.finalize_cancelled(entry.delegation_id)
            await self._registry.ack_outbox(entry.id)

    # ------------------------------------------------------------------
    # claim + execute (round-robin, concurrency-capped, parallel)
    # ------------------------------------------------------------------

    async def _claim_and_execute_pending(self) -> None:
        max_concurrency = getattr(self._config, "max_concurrency", 8)
        pending = await self._registry.list_pending_members(limit=max_concurrency)
        if not pending:
            return
        # Round-robin: interleave across delegations for fairness.
        by_delegation: dict[str, list[DelegationMember]] = {}
        for m in pending:
            by_delegation.setdefault(m.delegation_id, []).append(m)
        interleaved: list[DelegationMember] = []
        while by_delegation:
            for did in list(by_delegation.keys()):
                group = by_delegation[did]
                interleaved.append(group.pop(0))
                if not group:
                    del by_delegation[did]
        # Claim + execute concurrently.
        tasks = [self._claim_and_run(m) for m in interleaved]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _claim_and_run(self, member: DelegationMember) -> None:
        claim_lock = f"run-{member.id}"
        lease = getattr(self._config, "member_max_runtime_seconds", 900)
        claim_result = await self._registry.claim_member(
            member.delegation_id, member.ordinal, claim_lock, lease
        )
        if claim_result.outcome is not MutationOutcome.SUCCESS:
            return
        # Start the delegation (PENDING -> RUNNING) on first claim.
        await self._registry.start_delegation(member.delegation_id)
        claimed = claim_result.member
        # Re-check kill switch after claim (race closure).
        if self._kill_switch:
            await self._registry.cancel_pending_members(
                member.delegation_id, reason="kill_switch"
            )
            return
        result = await self._execute_member(claimed)
        await self._registry.finish_member(
            member.delegation_id, member.ordinal,
            claim_lock=claim_lock, result=result,
            expected_version=claimed.version,
        )

    async def _execute_member(self, member: DelegationMember) -> DelegationResult:
        try:
            delegation = await self._registry.get(member.delegation_id)
            deadline_at = delegation.deadline_at if delegation else None
            parent_cap = self._parent_caps.get(
                member.delegation_id,
                {"source": "delegation", "run_id": "", "session_id": "",
                 "scope_id": "", "actor_id": None},
            )
            return await self._child_executor.execute(
                member=member, model="default",
                parent_capability=parent_cap, deadline_at=deadline_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return DelegationResult(
                status=DelegationMemberStatus.FAILED,
                error_code="delegation_run_error",
                error_message="member execution failed",
            )

    # ------------------------------------------------------------------
    # kill switch: cancel pending members
    # ------------------------------------------------------------------

    async def _cancel_under_kill_switch(self) -> None:
        pending = await self._registry.list_pending_members(limit=100)
        cancelled_delegations: set[str] = set()
        for member in pending:
            if member.delegation_id in cancelled_delegations:
                continue
            delegation = await self._registry.get(member.delegation_id)
            if delegation is None or delegation.is_terminal:
                continue
            if delegation.status is not DelegationStatus.CANCELLING:
                await self._registry.request_cancel(
                    member.delegation_id, reason="kill_switch"
                )
            await self._registry.cancel_pending_members(
                member.delegation_id, reason="kill_switch"
            )
            cancelled_delegations.add(member.delegation_id)

    # ------------------------------------------------------------------
    # join advancement
    # ------------------------------------------------------------------

    async def _try_advance_joins(self) -> None:
        active = await self._registry.list_active_delegations(limit=100)
        for delegation in active:
            members = await self._registry.list_members(delegation.id)
            if not members:
                continue
            if not all(m.is_terminal for m in members):
                continue
            if delegation.status is DelegationStatus.CANCELLING:
                # All members terminal + cancelling -> finalize to CANCELLED.
                await self._registry.finalize_cancelled(delegation.id)
                continue
            if delegation.status is DelegationStatus.RUNNING:
                await self._registry.advance_to_joining(delegation.id)
            rs = await self._registry.get_result_set(delegation.id)
            if rs is None:
                continue
            await self._registry.finalize_result_set(
                delegation.id, rs.status.value
            )

    # ------------------------------------------------------------------
    # public cancel
    # ------------------------------------------------------------------

    async def request_cancel(self, delegation_id: str, reason: str):
        return await self._registry.request_cancel(delegation_id, reason)
