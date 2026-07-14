"""Schedule claim constraints and post-claim run decision (Domain).

Two-segment schedule enforcement:

1. **ScheduleClaimConstraints** -- in-transaction invariants (status, due,
   concurrency, lease) applied INSIDE the SQL transaction (CAS) by the
   Registry.  NOT a pre-check; the conditions are evaluated atomically
   with the claim UPDATE.

2. **ScheduleRunDecision / SchedulePolicy** -- post-claim decision evaluated
   AFTER a successful claim.  Determines missed_action (run_now /
   skipped_missed), retry_plan, delivery_mode, and run_overrides.

Pure Domain: imports only stdlib + app.domain.schedule + app.domain.policy.
No sqlite, no pydantic, no Infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from app.domain.policy import PolicyOutcome
from app.domain.schedule import (
    DeliveryTargetType,
    ScheduledExecutionPolicyMode,
    ScheduledTask,
    ScheduledTaskClaim,
    ScheduledTaskStatus,
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MissedAction(str, Enum):
    RUN_NOW = "run_now"
    SKIPPED_MISSED = "skipped_missed"


class DeliveryMode(str, Enum):
    DELIVER = "deliver"
    SILENT = "silent"


class RetryPlan(str, Enum):
    NONE = "none"
    RETRY = "retry"


# ---------------------------------------------------------------------------
# ScheduleClaimConstraints (in-transaction specification)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleClaimConstraints:
    """In-transaction invariants for claiming a scheduled task.

    Defines the conditions that must hold atomically (within the SQL
    transaction / CAS) for a claim to succeed.  The Registry applies
    these as WHERE-clause conditions in the UPDATE statement so that
    the check and the claim happen in a single atomic operation.

    NOT a pre-check -- these are evaluated inside the transaction.

    Authoritative enforcement is the SQL WHERE clause in
    ``sqlite_schedule_registry.py``.  This dataclass is a Domain
    specification for testing and documentation; it is NOT imported
    by the Registry and does not control the SQL.  If the SQL changes,
    update ``is_satisfied_by`` to match.

    ``is_satisfied_by`` models the PRIMARY claim path only (status +
    due + lease).  The ``claim_task_for_run_now`` SQL has an additional
    stale-reclaim ``EXISTS`` subquery (allows reclaiming a lease whose
    execution is no longer RUNNING) that is NOT modeled here.  Therefore
    ``is_satisfied_by`` may return ``False`` for tasks that the SQL
    would actually claim via the stale-reclaim path.
    """

    status: ScheduledTaskStatus = ScheduledTaskStatus.ACTIVE
    enabled: bool = True
    due_at_or_before: datetime | None = None
    lease_free_at: datetime | None = None

    @classmethod
    def for_due_claim(cls, now: datetime) -> ScheduleClaimConstraints:
        """Constraints for a due-task claim (scheduler tick)."""
        return cls(
            status=ScheduledTaskStatus.ACTIVE,
            enabled=True,
            due_at_or_before=now,
            lease_free_at=now,
        )

    @classmethod
    def for_run_now_claim(cls, now: datetime) -> ScheduleClaimConstraints:
        """Constraints for a manual run-now claim (no due check)."""
        return cls(
            status=ScheduledTaskStatus.ACTIVE,
            enabled=True,
            due_at_or_before=None,
            lease_free_at=now,
        )

    def is_satisfied_by(self, task: ScheduledTask, now: datetime) -> bool:
        """Domain-level check (for testing/documentation).

        The actual enforcement is the SQL WHERE clause in the Registry,
        which implements the same logic atomically.
        """
        if task.enabled != self.enabled:
            return False
        if task.status is not self.status:
            return False
        if self.due_at_or_before is not None and task.next_run_at > self.due_at_or_before:
            return False
        if self.lease_free_at is not None:
            if task.lease_until is not None and task.lease_until >= self.lease_free_at:
                return False
        return True


# ---------------------------------------------------------------------------
# Run overrides + decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOverrides:
    """Execution-mode overrides derived from the task's execution policy."""

    execution_mode: str = ScheduledExecutionPolicyMode.UNATTENDED.value
    tool_exposure_policy: str = "safe_only"
    allow_confirm_tools: bool = False


@dataclass(frozen=True)
class ScheduleRunDecision:
    """Post-claim decision for a scheduled task run.

    verdict: ALLOW (proceed) or DENY (blocked).
    missed_action: run_now (execute) or skipped_missed (record, don't run).
    retry_plan: Agent fail default is NONE (no auto-retry).
    delivery_mode: deliver (send to target) or silent (skip delivery).
    run_overrides: unattended mode, safe_only, no_confirm defaults.
    reason: human-readable explanation.
    """

    verdict: PolicyOutcome
    missed_action: MissedAction = MissedAction.RUN_NOW
    retry_plan: RetryPlan = RetryPlan.NONE
    delivery_mode: DeliveryMode = DeliveryMode.DELIVER
    run_overrides: RunOverrides = field(default_factory=RunOverrides)
    reason: str = ""

    @property
    def should_run(self) -> bool:
        return self.verdict is PolicyOutcome.ALLOW and self.missed_action is MissedAction.RUN_NOW

    @property
    def should_deliver(self) -> bool:
        return self.should_run and self.delivery_mode is DeliveryMode.DELIVER


# ---------------------------------------------------------------------------
# SchedulePolicy (post-claim evaluation)
# ---------------------------------------------------------------------------


class SchedulePolicy:
    """Post-claim policy for scheduled task execution.

    Called AFTER a successful claim (Registry CAS).  Determines whether
    the claimed task should run, be skipped (past grace), or denied
    (disabled/deleted/session-missing).

    Decision table:
    - disabled / deleted / session-missing / paused -> DENY.
    - past missed_grace_seconds -> ALLOW + skipped_missed (silent).
    - otherwise -> ALLOW + run_now.
    - Agent fail -> default NO retry (retry_plan=NONE).
    - silent delivery target -> delivery_mode=SILENT.
    """

    def __init__(self, missed_grace_seconds: int = 300) -> None:
        self._missed_grace = timedelta(seconds=missed_grace_seconds)

    def evaluate(self, claim: ScheduledTaskClaim, now: datetime) -> ScheduleRunDecision:
        task = claim.task

        # disabled / deleted / session-missing / paused -> DENY
        if not task.enabled or task.status in (
            ScheduledTaskStatus.DELETED,
            ScheduledTaskStatus.SESSION_MISSING,
            ScheduledTaskStatus.PAUSED,
        ):
            return ScheduleRunDecision(
                verdict=PolicyOutcome.DENY,
                reason=f"task not runnable: status={task.status.value} enabled={task.enabled}",
            )

        # past missed grace -> skipped_missed
        due_at = claim.claimed_next_run_at or task.next_run_at
        if due_at and (now - due_at) > self._missed_grace:
            return ScheduleRunDecision(
                verdict=PolicyOutcome.ALLOW,
                missed_action=MissedAction.SKIPPED_MISSED,
                delivery_mode=DeliveryMode.SILENT,
                reason="task past missed grace window",
            )

        # delivery mode from target type
        delivery_mode = (
            DeliveryMode.SILENT
            if task.delivery_target.target_type is DeliveryTargetType.SILENT
            else DeliveryMode.DELIVER
        )

        # run overrides from execution policy
        run_overrides = RunOverrides(
            execution_mode=task.execution_policy.mode.value,
            tool_exposure_policy=task.execution_policy.tool_exposure_policy,
            allow_confirm_tools=task.execution_policy.allow_confirm_tools,
        )

        return ScheduleRunDecision(
            verdict=PolicyOutcome.ALLOW,
            missed_action=MissedAction.RUN_NOW,
            delivery_mode=delivery_mode,
            run_overrides=run_overrides,
            reason="task due for execution",
        )
