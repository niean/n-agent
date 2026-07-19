"""TaskPolicy -- the 14th domain Policy (Domain Layer).

Pure domain: imports only stdlib + ``app.domain.policy`` (shared kernel) +
``app.domain.task`` (own domain types). No Application, Infrastructure, or
other Policy imports (AST-enforced by ``tests/architecture/test_policy_boundaries.py``).

Governance scope (Manus-aligned 7-state machine):
  1. State transition legality per ``TASK_TRANSITION_TABLE``.
  2. Claim atomicity: ``RUNNING`` may only be produced from ``QUEUED``
     (``evaluate_claim``).
  3. Circuit breaker: when ``consecutive_failures > max_retries``, the
     retryable-failure auto-retry (``RUNNING -> QUEUED``) is DENY, forcing
     the Application layer to use ``FAILED`` instead. User-initiated retry
     (``FAILED -> QUEUED`` / ``EXPIRED -> QUEUED``) is NOT subject to the
     breaker: the user explicitly retries.

Removed vs prior 9-state policy:
  - ``BlockKind`` routing and the unblock-loop breaker. The new state
    machine has no ``BLOCKED`` state, no dependency graph, and no swarm
    concepts, so there is no unblock loop to break.
  - ``block_kind`` / ``block_recurrences`` / ``unblock_loop_threshold``
    fields on the request.

Returns bare ``PolicyOutcome`` enum members (matches ``skill_policy.py`` and
``curator_policy.py`` contract). Deny reasons are logged by the Application
layer; they are not encoded into the return value.

SQLite transaction atomicity, file IO, and process-worker survival checks
remain in the Registry / TaskRunService; they are NOT part of this Policy.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.policy import Policy, PolicyOutcome
from app.domain.task import TASK_TRANSITION_TABLE, TaskStatus


@dataclass(frozen=True)
class TaskPolicyRequest:
    """TaskPolicy evaluation request.

    Fields:
      current: current TaskStatus
      target: requested target TaskStatus
      consecutive_failures: current failure streak (cleared by SUCCEEDED;
          dispatcher retry does NOT clear)
      max_retries: retries allowed after the first failure; the circuit
          breaker trips (``RUNNING -> QUEUED`` denied) when
          ``consecutive_failures > max_retries``

    Removed fields (vs prior 9-state policy): ``block_kind``,
    ``block_recurrences``, ``unblock_loop_threshold``. The new state machine
    has no ``BLOCKED`` state, no ``BlockKind``, and no unblock loop.
    """

    current: TaskStatus
    target: TaskStatus
    consecutive_failures: int = 0
    max_retries: int = 0


class TaskPolicy(Policy):
    """Task state-transition governance Policy (Manus-aligned 7-state).

    Priority order (deny wins):
      1. Generic state transition legality (contract table)
      2. Circuit breaker (``consecutive_failures > max_retries`` on
         ``RUNNING -> QUEUED`` auto-retry)

    The unblock-loop breaker has been removed: no ``BLOCKED`` state, no
    ``BlockKind``, no dependency graph.

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

        # 2. Circuit breaker: auto-retry (RUNNING -> QUEUED) is denied when
        #    consecutive_failures exceed max_retries. The Application layer
        #    interprets this DENY as "use FAILED instead of QUEUED" for the
        #    run-finalization target status. User-initiated retry
        #    (FAILED -> QUEUED, EXPIRED -> QUEUED) is NOT subject to this
        #    breaker: the user explicitly retries, and FAILED is itself a
        #    retryable state.
        if (
            r.current is TaskStatus.RUNNING
            and r.target is TaskStatus.QUEUED
            and r.consecutive_failures > r.max_retries
        ):
            return PolicyOutcome.DENY

        return PolicyOutcome.ALLOW

    def evaluate_transition(
        self,
        request: TaskPolicyRequest,
    ) -> PolicyOutcome:
        """Named alias for ``evaluate``: explicit transition-legality check.

        Callers that want to signal "I am evaluating a transition" use this
        method; behavior is identical to ``evaluate``. Kept as a named
        entry point so dispatcher / run-finalization code reads naturally.
        """
        return self.evaluate(request)

    def evaluate_claim(
        self,
        request: TaskPolicyRequest,
    ) -> PolicyOutcome:
        """Claim-specific check: ``QUEUED -> RUNNING`` only.

        The dispatcher claim path produces ``RUNNING`` exclusively from
        ``QUEUED``. Any other current state (including ``RUNNING`` itself,
        ``WAITING_APPROVAL``, ``FAILED``, etc.) or any other target is
        denied. The due-time check (``scheduled_at <= now``) is handled by
        the Registry via ``list_queued_due``, NOT by this Policy: claim
        atomicity here is purely about the state-pair contract.
        """
        if (
            request.current is not TaskStatus.QUEUED
            or request.target is not TaskStatus.RUNNING
        ):
            return PolicyOutcome.DENY
        return self.evaluate(request)
