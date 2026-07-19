"""TaskPolicy (14th domain Policy) tests.

Covers plan T3: state transition legality, circuit breaker, unblock-loop
breaker, claim atomicity, and block routing.
"""
from __future__ import annotations

import pytest

from app.domain.policy import PolicyOutcome
from app.domain.task import BlockKind, TaskStatus
from app.domain.task_policy import TaskPolicy, TaskPolicyRequest


def _req(**overrides):
    base = dict(
        current=TaskStatus.TRIAGE,
        target=TaskStatus.TODO,
        block_kind=None,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=0,
        unblock_loop_threshold=3,
    )
    base.update(overrides)
    return TaskPolicyRequest(**base)


# ---------------------------------------------------------------------------
# S1: Policy evaluation -- state transition legality
# ---------------------------------------------------------------------------


def test_policy_allows_legal_transition():
    p = TaskPolicy()
    r = TaskPolicyRequest(
        current=TaskStatus.TRIAGE,
        target=TaskStatus.TODO,
        block_kind=None,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=0,
    )
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_denies_illegal_transition():
    r = TaskPolicyRequest(
        current=TaskStatus.TRIAGE,
        target=TaskStatus.RUNNING,
        block_kind=None,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=0,
    )
    assert TaskPolicy().evaluate(r) == PolicyOutcome.DENY


def test_policy_gave_up_on_failure_limit():
    r = TaskPolicyRequest(
        current=TaskStatus.RUNNING,
        target=TaskStatus.TODO,
        block_kind=None,
        consecutive_failures=4,
        max_retries=3,
        block_recurrences=0,
    )
    # consecutive_failures > max_retries -> GAVE_UP (deny retry, require block)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.DENY


def test_policy_allows_retry_within_limit():
    r = TaskPolicyRequest(
        current=TaskStatus.RUNNING,
        target=TaskStatus.TODO,
        block_kind=None,
        consecutive_failures=2,
        max_retries=3,
        block_recurrences=0,
    )
    # 2 <= 3, retry allowed
    assert TaskPolicy().evaluate(r) == PolicyOutcome.ALLOW


def test_policy_allows_legal_done_to_review():
    r = _req(current=TaskStatus.DONE, target=TaskStatus.REVIEW)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.ALLOW


def test_policy_denies_done_to_running():
    r = _req(current=TaskStatus.DONE, target=TaskStatus.RUNNING)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.DENY


def test_policy_denies_archived_generic_transition():
    # ARCHIVED has no generic outgoing edges (unarchive is explicit)
    r = _req(current=TaskStatus.ARCHIVED, target=TaskStatus.TODO)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# S5: claim atomicity + unblock-loop breaker
# ---------------------------------------------------------------------------


def test_policy_claim_requires_ready():
    p = TaskPolicy()
    # claim 只能从 READY 产生 RUNNING；从 TODO 直接 RUNNING -> DENY
    r = TaskPolicyRequest(
        current=TaskStatus.TODO,
        target=TaskStatus.RUNNING,
        block_kind=None,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=0,
        unblock_loop_threshold=3,
    )
    assert p.evaluate(r) == PolicyOutcome.DENY


def test_policy_claim_allows_from_ready():
    p = TaskPolicy()
    r = _req(current=TaskStatus.READY, target=TaskStatus.RUNNING)
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_unblock_loop_breaker():
    p = TaskPolicy()
    # block_recurrences 超阈 -> DENY（强制升级 NEEDS_INPUT，禁止自动 unblock）
    r = TaskPolicyRequest(
        current=TaskStatus.BLOCKED,
        target=TaskStatus.TODO,
        block_kind=BlockKind.TRANSIENT,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=5,
        unblock_loop_threshold=3,
    )
    assert p.evaluate(r) == PolicyOutcome.DENY


def test_policy_unblock_loop_within_threshold_allowed():
    p = TaskPolicy()
    r = TaskPolicyRequest(
        current=TaskStatus.BLOCKED,
        target=TaskStatus.TODO,
        block_kind=BlockKind.TRANSIENT,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=2,
        unblock_loop_threshold=3,
    )
    # 2 <= 3, unblock allowed
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_unblock_loop_breaker_zero_threshold():
    """When threshold is 0, any non-zero recurrence denies auto-unblock."""
    p = TaskPolicy()
    r = TaskPolicyRequest(
        current=TaskStatus.BLOCKED,
        target=TaskStatus.TODO,
        block_kind=BlockKind.TRANSIENT,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=1,
        unblock_loop_threshold=0,
    )
    assert p.evaluate(r) == PolicyOutcome.DENY


def test_policy_unblock_loop_does_not_apply_to_dependency_block():
    """DEPENDENCY block kind routes to TODO (not an unblock-loop)."""
    p = TaskPolicy()
    r = TaskPolicyRequest(
        current=TaskStatus.BLOCKED,
        target=TaskStatus.TODO,
        block_kind=BlockKind.DEPENDENCY,
        consecutive_failures=0,
        max_retries=3,
        block_recurrences=5,  # exceeds threshold
        unblock_loop_threshold=3,
    )
    # DEPENDENCY is a re-route to TODO, not an unblock-loop; should be allowed
    # (still subject to the generic transition table, which allows BLOCKED->TODO)
    assert p.evaluate(r) == PolicyOutcome.ALLOW


def test_policy_allows_blocked_to_archived():
    r = _req(current=TaskStatus.BLOCKED, target=TaskStatus.ARCHIVED)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.ALLOW


def test_policy_denies_blocked_to_running():
    r = _req(current=TaskStatus.BLOCKED, target=TaskStatus.RUNNING)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.DENY


def test_policy_allows_running_to_done():
    r = _req(current=TaskStatus.RUNNING, target=TaskStatus.DONE)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.ALLOW


def test_policy_denies_running_to_ready():
    r = _req(current=TaskStatus.RUNNING, target=TaskStatus.READY)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.DENY


def test_policy_evaluate_accepts_context_none():
    """evaluate signature aligns with Policy Protocol (context=None)."""
    p = TaskPolicy()
    assert p.evaluate(_req(), context=None) == PolicyOutcome.ALLOW


def test_policy_allows_running_to_review():
    r = _req(current=TaskStatus.RUNNING, target=TaskStatus.REVIEW)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.ALLOW


def test_policy_allows_review_to_done():
    r = _req(current=TaskStatus.REVIEW, target=TaskStatus.DONE)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.ALLOW


def test_policy_denies_review_to_running():
    r = _req(current=TaskStatus.REVIEW, target=TaskStatus.RUNNING)
    assert TaskPolicy().evaluate(r) == PolicyOutcome.DENY


def test_policy_request_is_frozen():
    r = _req()
    with pytest.raises(Exception):
        r.current = TaskStatus.DONE  # type: ignore[misc]


def test_policy_no_retries_when_max_retries_zero():
    """max_retries=0 means first failure triggers GAVE_UP (1 > 0)."""
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.TODO,
        consecutive_failures=1,
        max_retries=0,
    )
    assert p.evaluate(r) == PolicyOutcome.DENY


def test_policy_gave_up_boundary_exact():
    """consecutive_failures == max_retries is still allowed (not >)."""
    p = TaskPolicy()
    r = _req(
        current=TaskStatus.RUNNING,
        target=TaskStatus.TODO,
        consecutive_failures=3,
        max_retries=3,
    )
    # 3 == 3, not exceeding; retry allowed
    assert p.evaluate(r) == PolicyOutcome.ALLOW
