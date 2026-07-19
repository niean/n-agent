"""TaskPolicy (14th domain Policy) tests.

Covers plan T2: state transition legality for the Manus-aligned 7-state
machine, claim atomicity (QUEUED -> RUNNING), and the retry circuit breaker
(consecutive_failures > max_retries denies RUNNING -> QUEUED auto-retry).

Removed coverage vs prior 9-state machine: BlockKind routing, unblock-loop
breaker, READY/BLOCKED/ARCHIVED transitions, GAVE_UP semantics.
"""
from __future__ import annotations

import pytest

from app.domain.policy import PolicyOutcome
from app.domain.task import TaskStatus
from app.domain.task_policy import TaskPolicy, TaskPolicyRequest


def _req(**overrides):
    base = dict(
        current=TaskStatus.QUEUED,
        target=TaskStatus.RUNNING,
        consecutive_failures=0,
        max_retries=3,
    )
    base.update(overrides)
    return TaskPolicyRequest(**base)


# ---------------------------------------------------------------------------
# S1: Policy evaluation -- 7-state transition legality
# ---------------------------------------------------------------------------


def test_policy_allows_queued_to_running():
    # claim path: QUEUED -> RUNNING
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.QUEUED,
        target=TaskStatus.RUNNING,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_queued_to_cancelled():
    # cancel a queued task
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.QUEUED,
        target=TaskStatus.CANCELLED,
    )) == PolicyOutcome.ALLOW


def test_policy_denies_queued_to_succeeded():
    # SUCCEEDED only reachable from RUNNING; QUEUED -> SUCCEEDED skips execution
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.QUEUED,
        target=TaskStatus.SUCCEEDED,
    )) == PolicyOutcome.DENY


def test_policy_denies_queued_to_failed():
    # FAILED only reachable from RUNNING
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.QUEUED,
        target=TaskStatus.FAILED,
    )) == PolicyOutcome.DENY


def test_policy_denies_queued_to_waiting_approval():
    # propose_change only valid from RUNNING
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.QUEUED,
        target=TaskStatus.WAITING_APPROVAL,
    )) == PolicyOutcome.DENY


def test_policy_denies_queued_to_queued():
    # no self-transition
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.QUEUED,
        target=TaskStatus.QUEUED,
    )) == PolicyOutcome.DENY


def test_policy_allows_running_to_succeeded():
    # task_complete
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.SUCCEEDED,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_running_to_failed():
    # give-up transition
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.FAILED,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_running_to_waiting_approval():
    # propose_change
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.WAITING_APPROVAL,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_running_to_cancelled():
    # cancel RUNNING (coordinated by TaskRunService)
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.CANCELLED,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_running_to_expired():
    # stale / lease timeout
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.EXPIRED,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_waiting_approval_to_queued():
    # approve / reject -> back to QUEUED for next claim
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.WAITING_APPROVAL,
        target=TaskStatus.QUEUED,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_waiting_approval_to_cancelled():
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.WAITING_APPROVAL,
        target=TaskStatus.CANCELLED,
    )) == PolicyOutcome.ALLOW


def test_policy_denies_waiting_approval_to_succeeded():
    # cannot complete from WAITING_APPROVAL; must approve first
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.WAITING_APPROVAL,
        target=TaskStatus.SUCCEEDED,
    )) == PolicyOutcome.DENY


def test_policy_allows_failed_to_queued():
    # user retry
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.FAILED,
        target=TaskStatus.QUEUED,
    )) == PolicyOutcome.ALLOW


def test_policy_allows_failed_to_cancelled():
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.FAILED,
        target=TaskStatus.CANCELLED,
    )) == PolicyOutcome.ALLOW


def test_policy_denies_failed_to_succeeded():
    # cannot complete from FAILED; must retry first
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.FAILED,
        target=TaskStatus.SUCCEEDED,
    )) == PolicyOutcome.DENY


def test_policy_allows_expired_to_queued():
    # user retry of EXPIRED
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.EXPIRED,
        target=TaskStatus.QUEUED,
    )) == PolicyOutcome.ALLOW


def test_policy_denies_expired_to_cancelled():
    # EXPIRED cannot be cancelled; must retry back to QUEUED
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.EXPIRED,
        target=TaskStatus.CANCELLED,
    )) == PolicyOutcome.DENY


def test_policy_denies_succeeded_to_queued():
    # SUCCEEDED is terminal
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.SUCCEEDED,
        target=TaskStatus.QUEUED,
    )) == PolicyOutcome.DENY


def test_policy_denies_succeeded_to_cancelled():
    # SUCCEEDED is terminal; cannot cancel
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.SUCCEEDED,
        target=TaskStatus.CANCELLED,
    )) == PolicyOutcome.DENY


def test_policy_denies_cancelled_to_queued():
    # CANCELLED is terminal
    assert TaskPolicy().evaluate(_req(
        current=TaskStatus.CANCELLED,
        target=TaskStatus.QUEUED,
    )) == PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# S2: claim atomicity -- QUEUED -> RUNNING only
# ---------------------------------------------------------------------------


def test_policy_claim_allows_from_queued():
    p = TaskPolicy()
    r = _req(current=TaskStatus.QUEUED, target=TaskStatus.RUNNING)
    assert p.evaluate_claim(r) == PolicyOutcome.ALLOW


def test_policy_claim_denies_from_running():
    # already RUNNING, cannot claim again
    p = TaskPolicy()
    r = _req(current=TaskStatus.RUNNING, target=TaskStatus.RUNNING)
    assert p.evaluate_claim(r) == PolicyOutcome.DENY


def test_policy_claim_denies_from_waiting_approval():
    p = TaskPolicy()
    r = _req(current=TaskStatus.WAITING_APPROVAL, target=TaskStatus.RUNNING)
    assert p.evaluate_claim(r) == PolicyOutcome.DENY


def test_policy_claim_denies_from_failed():
    p = TaskPolicy()
    r = _req(current=TaskStatus.FAILED, target=TaskStatus.RUNNING)
    assert p.evaluate_claim(r) == PolicyOutcome.DENY


def test_policy_claim_denies_from_succeeded():
    p = TaskPolicy()
    r = _req(current=TaskStatus.SUCCEEDED, target=TaskStatus.RUNNING)
    assert p.evaluate_claim(r) == PolicyOutcome.DENY


def test_policy_claim_denies_when_target_not_running():
    # claim must produce RUNNING; e.g. QUEUED -> SUCCEEDED is not a claim
    p = TaskPolicy()
    r = _req(current=TaskStatus.QUEUED, target=TaskStatus.SUCCEEDED)
    assert p.evaluate_claim(r) == PolicyOutcome.DENY


def test_policy_claim_denies_when_target_queued():
    # QUEUED -> QUEUED is not a claim
    p = TaskPolicy()
    r = _req(current=TaskStatus.QUEUED, target=TaskStatus.QUEUED)
    assert p.evaluate_claim(r) == PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# S3: Circuit breaker -- RUNNING -> QUEUED auto-retry
# ---------------------------------------------------------------------------


def test_policy_circuit_breaker_trips_when_exceeding_max_retries():
    # consecutive_failures > max_retries -> DENY auto-retry (force FAILED)
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.QUEUED,
        consecutive_failures=4,
        max_retries=3,
    )
    assert p.evaluate(r) == PolicyOutcome.DENY


def test_policy_circuit_breaker_allows_within_limit():
    # 2 <= 3, auto-retry allowed
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.QUEUED,
        consecutive_failures=2,
        max_retries=3,
    )
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_circuit_breaker_boundary_exact():
    # consecutive_failures == max_retries is still allowed (not >)
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.QUEUED,
        consecutive_failures=3,
        max_retries=3,
    )
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_circuit_breaker_zero_max_retries():
    # max_retries=0 means first failure (1 > 0) trips the breaker
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.QUEUED,
        consecutive_failures=1,
        max_retries=0,
    )
    assert p.evaluate(r) == PolicyOutcome.DENY


def test_policy_circuit_breaker_does_not_apply_to_user_retry_from_failed():
    # FAILED -> QUEUED is user-initiated retry, NOT subject to breaker
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.FAILED,
        target=TaskStatus.QUEUED,
        consecutive_failures=10,  # exceeds max_retries
        max_retries=3,
    )
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_circuit_breaker_does_not_apply_to_user_retry_from_expired():
    # EXPIRED -> QUEUED is user-initiated retry, NOT subject to breaker
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.EXPIRED,
        target=TaskStatus.QUEUED,
        consecutive_failures=10,
        max_retries=3,
    )
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_circuit_breaker_does_not_apply_to_running_to_failed():
    # RUNNING -> FAILED is the give-up transition itself; not subject to breaker
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.FAILED,
        consecutive_failures=10,
        max_retries=3,
    )
    assert p.evaluate(r) == PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# S4: Request shape -- frozen, no BlockKind fields
# ---------------------------------------------------------------------------


def test_policy_request_is_frozen():
    r = _req()
    with pytest.raises(Exception):
        r.current = TaskStatus.SUCCEEDED  # type: ignore[misc]


def test_policy_request_has_no_block_kind_fields():
    """New shape: current/target/consecutive_failures/max_retries only.

    block_kind / block_recurrences / unblock_loop_threshold are gone.
    """
    r = _req()
    fields = set(r.__dataclass_fields__)
    assert fields == {
        "current",
        "target",
        "consecutive_failures",
        "max_retries",
    }


# ---------------------------------------------------------------------------
# S5: Protocol conformance
# ---------------------------------------------------------------------------


def test_policy_evaluate_accepts_context_none():
    """evaluate signature aligns with Policy Protocol (context=None)."""
    p = TaskPolicy()
    assert p.evaluate(_req(), context=None) == PolicyOutcome.ALLOW


def test_policy_evaluate_transition_mirrors_evaluate():
    """evaluate_transition returns the same decision as evaluate."""
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.QUEUED,
        consecutive_failures=4,
        max_retries=3,
    )
    assert p.evaluate_transition(r) == p.evaluate(r) == PolicyOutcome.DENY


def test_policy_evaluate_transition_allows_legal():
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.FAILED,
        target=TaskStatus.QUEUED,
    )
    assert p.evaluate_transition(r) == PolicyOutcome.ALLOW
