"""TaskPolicy -- the 14th domain Policy (Domain Layer).

Pure domain: imports only stdlib + ``app.domain.policy`` (shared kernel) +
``app.domain.task`` (own domain types). No Application, Infrastructure, or
other Policy imports (AST-enforced by ``tests/architecture/test_policy_boundaries.py``).

Governance scope:
  1. State transition legality per spec contract table.
  2. Claim atomicity: RUNNING may only be produced from READY.
  3. Circuit breaker: when ``consecutive_failures > max_retries``, retry
     transitions (RUNNING -> TODO) are DENY, forcing GAVE_UP / BLOCKED.
  4. Unblock-loop breaker: when ``block_recurrences > unblock_loop_threshold``
     and the block kind is not DEPENDENCY, auto-unblock (BLOCKED -> TODO) is
     DENY, forcing escalation to NEEDS_INPUT.

Returns bare ``PolicyOutcome`` enum members (matches ``skill_policy.py`` and
``curator_policy.py`` contract). Deny reasons are logged by the Application
layer; they are not encoded into the return value.

SQLite transaction atomicity, file IO, and process-worker survival checks
remain in the Registry / TaskRunService; they are NOT part of this Policy.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.policy import Policy, PolicyOutcome
from app.domain.task import BlockKind, TaskStatus, TASK_TRANSITION_TABLE


@dataclass(frozen=True)
class TaskPolicyRequest:
    """TaskPolicy evaluation request.

    Fields:
      current: current TaskStatus
      target: requested target TaskStatus
      block_kind: BlockKind when the transition is a block/unblock, else None
      consecutive_failures: current failure streak (cleared by success or
          manual unblock; dispatcher retry does NOT clear)
      max_retries: retries allowed after the first failure; GAVE_UP triggers
          when ``consecutive_failures > max_retries``
      block_recurrences: how many times the task has entered BLOCKED state
          via a non-DEPENDENCY block kind
      unblock_loop_threshold: max allowed block recurrences before the
          unblock-loop breaker forces NEEDS_INPUT escalation
    """

    current: TaskStatus
    target: TaskStatus
    block_kind: BlockKind | None
    consecutive_failures: int
    max_retries: int
    block_recurrences: int = 0
    unblock_loop_threshold: int = 3


class TaskPolicy(Policy):
    """Task state-transition governance Policy.

    Priority order (deny wins):
      1. Generic state transition legality (contract table)
      2. Circuit breaker (consecutive_failures > max_retries on retry)
      3. Unblock-loop breaker (block_recurrences > threshold on auto-unblock)

    All checks are pure functions of the request; no side effects, no IO.
    """

    def evaluate(
        self,
        request: TaskPolicyRequest,
        context: None = None,
    ) -> PolicyOutcome:
        r = request

        # 1. Generic transition legality per contract table.
        allowed = TASK_TRANSITION_TABLE.get(r.current, frozenset())
        if r.target not in allowed:
            return PolicyOutcome.DENY

        # 2. Circuit breaker: retry from RUNNING -> TODO is denied when
        #    consecutive_failures exceed max_retries. The Application layer
        #    interprets this DENY as "trigger GAVE_UP / BLOCKED(NEEDS_INPUT)"
        #    instead of retrying.
        if (
            r.current == TaskStatus.RUNNING
            and r.target == TaskStatus.TODO
            and r.consecutive_failures > r.max_retries
        ):
            return PolicyOutcome.DENY

        # 3. Unblock-loop breaker: BLOCKED -> TODO with a non-DEPENDENCY block
        #    kind is denied when recurrences exceed the threshold, forcing the
        #    Application to escalate to NEEDS_INPUT instead of auto-unblocking.
        #    DEPENDENCY blocks route back to TODO (waiting on parent), so they
        #    are not subject to the unblock-loop breaker.
        if (
            r.current == TaskStatus.BLOCKED
            and r.target == TaskStatus.TODO
            and r.block_kind != BlockKind.DEPENDENCY
            and r.block_recurrences > r.unblock_loop_threshold
        ):
            return PolicyOutcome.DENY

        return PolicyOutcome.ALLOW
