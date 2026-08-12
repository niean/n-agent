"""Domain Delegation aggregate, value objects, ports, and state transition tests.

Covers plan T1 (Delegation subdomain: enums, frozen dataclasses with version,
state transition tables, join-policy evaluation, port Protocols).

The Delegation subdomain enables a parent Agent (realtime chat or Task worker)
to delegate parallel work to isolated depth-1 child Agents and aggregate
results. Domain is pure: no FastAPI, LangGraph, SQLite, OpenAI SDK, pydantic,
asyncio, or ``app.domain.task`` imports.
"""
from __future__ import annotations

import dataclasses

import pytest

from app.domain.delegation import (
    # Enums
    DelegationStatus,
    DelegationMemberStatus,
    DelegationMemberRole,
    DelegationJoinPolicy,
    DelegationAggregationPolicy,
    MutationOutcome,
    # Aggregates
    Delegation,
    DelegationMember,
    DelegationParentRef,
    # Value objects
    DelegationChildSpec,
    DelegationResult,
    DelegationResultSet,
    DelegationEvent,
    # Join evaluation
    evaluate_join_outcome,
    # Port result types
    ClaimMemberResult,
    FinishMemberResult,
    LedgerResult,
    # Ports
    DelegationRegistry,
    Clock,
    DelegationDispatcher,
    # Exceptions
    DelegationStateError,
    # Constants
    DELEGATION_TRANSITION_TABLE,
    DELEGATION_MEMBER_TRANSITION_TABLE,
    DELEGATION_TERMINAL_STATES,
    DELEGATION_MEMBER_TERMINAL_STATES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parent_ref() -> DelegationParentRef:
    return DelegationParentRef(
        source="task", scope_id="t1", run_id="r1", session_id="s1",
    )


def _new_delegation(
    join_policy: str = "all_completed",
    aggregation: str = "parent",
) -> Delegation:
    return Delegation.new(
        parent=_parent_ref(),
        delegation_key="k1",
        fingerprint="fp1",
        join_policy=join_policy,
        aggregation=aggregation,
        deadline_at="2026-08-12T03:00:00Z",
        policy_snapshot_id="ps1",
        budget_total_tokens=1000,
    )


def _new_member(
    ordinal: int = 0,
    role: DelegationMemberRole = DelegationMemberRole.WORKER,
) -> DelegationMember:
    return DelegationMember.new(
        delegation_id="d1",
        role=role,
        ordinal=ordinal,
        title=f"w{ordinal}",
        instruction="do",
        skills=(),
        allowed_tools=(),
        execution_session_id=f"delegation-sess-{ordinal}",
        deadline_at="2026-08-12T03:00:00Z",
        budget_tokens=500,
    )


def _result(
    status: DelegationMemberStatus,
    ordinal: int = 0,
    summary: str = "",
) -> DelegationResult:
    return DelegationResult(
        status=status,
        summary=summary or f"member-{ordinal}",
        structured_data=None,
        artifact_refs=(),
        error_code=None,
        error_message=None,
        usage_summary={"prompt_tokens": 10, "completion_tokens": 5},
        classification=None,
        checksum=f"sha256:{ordinal}",
        started_at="2026-08-12T02:00:00Z",
        ended_at="2026-08-12T02:30:00Z",
    )


# ---------------------------------------------------------------------------
# Plan S1: status enum and state transition failure tests
# ---------------------------------------------------------------------------


def test_delegation_status_running_to_joining():
    d = _new_delegation()
    assert d.status is DelegationStatus.PENDING
    d.transition(DelegationStatus.RUNNING)
    d.transition(DelegationStatus.JOINING)
    assert d.status is DelegationStatus.JOINING


def test_delegation_terminal_state_immutable():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.transition(DelegationStatus.SUCCEEDED)
    with pytest.raises(DelegationStateError):
        d.transition(DelegationStatus.RUNNING)


def test_member_status_transitions_legal():
    m = _new_member()
    assert m.status is DelegationMemberStatus.PENDING
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.SUCCEEDED)


def test_member_terminal_immutable_and_no_skip():
    m = _new_member()
    with pytest.raises(DelegationStateError):
        m.transition(DelegationMemberStatus.SUCCEEDED)
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.FAILED)
    with pytest.raises(DelegationStateError):
        m.transition(DelegationMemberStatus.RUNNING)


# ---------------------------------------------------------------------------
# Delegation transition table tests
# ---------------------------------------------------------------------------


def test_delegation_transition_table_is_complete():
    expected_states = {
        DelegationStatus.PENDING,
        DelegationStatus.RUNNING,
        DelegationStatus.JOINING,
        DelegationStatus.SUCCEEDED,
        DelegationStatus.FAILED,
        DelegationStatus.CANCELLING,
        DelegationStatus.CANCELLED,
        DelegationStatus.EXPIRED,
    }
    assert set(DELEGATION_TRANSITION_TABLE.keys()) == expected_states
    for terminal in DELEGATION_TERMINAL_STATES:
        assert DELEGATION_TRANSITION_TABLE[terminal] == frozenset()


def test_delegation_pending_to_running():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    assert d.status is DelegationStatus.RUNNING
    assert d.version == 2


def test_delegation_running_to_joining():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.transition(DelegationStatus.JOINING)
    assert d.status is DelegationStatus.JOINING
    assert d.version == 3


def test_delegation_joining_to_succeeded():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.transition(DelegationStatus.JOINING)
    d.transition(DelegationStatus.SUCCEEDED)
    assert d.status is DelegationStatus.SUCCEEDED


def test_delegation_joining_to_failed():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.transition(DelegationStatus.JOINING)
    d.transition(DelegationStatus.FAILED)
    assert d.status is DelegationStatus.FAILED


def test_delegation_mark_cancelling():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.mark_cancelling()
    assert d.status is DelegationStatus.CANCELLING


def test_delegation_cancelling_to_cancelled():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.mark_cancelling()
    d.transition(DelegationStatus.CANCELLED)
    assert d.status is DelegationStatus.CANCELLED


def test_delegation_any_nonterminal_to_cancelling():
    for start in (DelegationStatus.PENDING, DelegationStatus.RUNNING,
                  DelegationStatus.JOINING):
        d = _new_delegation()
        if start is not DelegationStatus.PENDING:
            d.transition(DelegationStatus.RUNNING)
        if start is DelegationStatus.JOINING:
            d.transition(DelegationStatus.JOINING)
        d.mark_cancelling()
        assert d.status is DelegationStatus.CANCELLING


def test_delegation_deadline_to_expired():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.transition(DelegationStatus.EXPIRED)
    assert d.status is DelegationStatus.EXPIRED


def test_delegation_cancelling_to_expired_on_deadline():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.mark_cancelling()
    d.transition(DelegationStatus.EXPIRED)
    assert d.status is DelegationStatus.EXPIRED


def test_delegation_is_terminal():
    d = _new_delegation()
    assert d.is_terminal is False
    d.transition(DelegationStatus.RUNNING)
    assert d.is_terminal is False
    d.transition(DelegationStatus.JOINING)
    assert d.is_terminal is False
    d.transition(DelegationStatus.SUCCEEDED)
    assert d.is_terminal is True


def test_delegation_all_terminal_states_are_terminal():
    assert DELEGATION_TERMINAL_STATES == frozenset({
        DelegationStatus.SUCCEEDED,
        DelegationStatus.FAILED,
        DelegationStatus.CANCELLED,
        DelegationStatus.EXPIRED,
    })


def test_delegation_version_increments_on_each_transition():
    d = _new_delegation()
    assert d.version == 1
    d.transition(DelegationStatus.RUNNING)
    assert d.version == 2
    d.transition(DelegationStatus.JOINING)
    assert d.version == 3
    d.transition(DelegationStatus.SUCCEEDED)
    assert d.version == 4


def test_delegation_skip_transition_pending_to_joining_illegal():
    d = _new_delegation()
    with pytest.raises(DelegationStateError):
        d.transition(DelegationStatus.JOINING)


def test_delegation_skip_transition_pending_to_succeeded_illegal():
    d = _new_delegation()
    with pytest.raises(DelegationStateError):
        d.transition(DelegationStatus.SUCCEEDED)


# ---------------------------------------------------------------------------
# DelegationMember transition table tests
# ---------------------------------------------------------------------------


def test_member_transition_table_is_complete():
    expected_states = {
        DelegationMemberStatus.PENDING,
        DelegationMemberStatus.RUNNING,
        DelegationMemberStatus.SUCCEEDED,
        DelegationMemberStatus.FAILED,
        DelegationMemberStatus.CANCELLED,
        DelegationMemberStatus.EXPIRED,
    }
    assert set(DELEGATION_MEMBER_TRANSITION_TABLE.keys()) == expected_states
    for terminal in DELEGATION_MEMBER_TERMINAL_STATES:
        assert DELEGATION_MEMBER_TRANSITION_TABLE[terminal] == frozenset()


def test_member_pending_to_running():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    assert m.status is DelegationMemberStatus.RUNNING
    assert m.version == 2


def test_member_running_to_succeeded():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.SUCCEEDED)
    assert m.status is DelegationMemberStatus.SUCCEEDED


def test_member_running_to_failed():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.FAILED)
    assert m.status is DelegationMemberStatus.FAILED


def test_member_pending_to_cancelled():
    m = _new_member()
    m.transition(DelegationMemberStatus.CANCELLED)
    assert m.status is DelegationMemberStatus.CANCELLED


def test_member_running_to_cancelled():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.CANCELLED)
    assert m.status is DelegationMemberStatus.CANCELLED


def test_member_pending_to_expired():
    m = _new_member()
    m.transition(DelegationMemberStatus.EXPIRED)
    assert m.status is DelegationMemberStatus.EXPIRED


def test_member_running_to_expired():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.EXPIRED)
    assert m.status is DelegationMemberStatus.EXPIRED


def test_member_stale_recovery_running_to_pending():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.PENDING)
    assert m.status is DelegationMemberStatus.PENDING
    assert m.version == 3


def test_member_stale_recovery_increments_retry_count():
    m = _new_member()
    assert m.retry_count == 0
    assert m.retry_of is None
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.PENDING)
    assert m.retry_count == 1
    assert m.retry_of == m.id
    # Second stale recovery: retry_count keeps incrementing, retry_of stays.
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.PENDING)
    assert m.retry_count == 2
    assert m.retry_of == m.id


def test_member_is_terminal():
    m = _new_member()
    assert m.is_terminal is False
    m.transition(DelegationMemberStatus.RUNNING)
    assert m.is_terminal is False
    m.transition(DelegationMemberStatus.SUCCEEDED)
    assert m.is_terminal is True


def test_member_all_terminal_states_are_terminal():
    for s in (DelegationMemberStatus.SUCCEEDED, DelegationMemberStatus.FAILED,
              DelegationMemberStatus.CANCELLED, DelegationMemberStatus.EXPIRED):
        assert s in DELEGATION_MEMBER_TERMINAL_STATES


def test_member_succeeded_is_terminal_cannot_transition():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.SUCCEEDED)
    for target in DelegationMemberStatus:
        with pytest.raises(DelegationStateError):
            m.transition(target)


def test_member_cancelled_is_terminal_cannot_transition():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.CANCELLED)
    for target in DelegationMemberStatus:
        with pytest.raises(DelegationStateError):
            m.transition(target)


def test_member_expired_is_terminal_cannot_transition():
    m = _new_member()
    m.transition(DelegationMemberStatus.RUNNING)
    m.transition(DelegationMemberStatus.EXPIRED)
    for target in DelegationMemberStatus:
        with pytest.raises(DelegationStateError):
            m.transition(target)


def test_member_version_increments_on_each_transition():
    m = _new_member()
    assert m.version == 1
    m.transition(DelegationMemberStatus.RUNNING)
    assert m.version == 2
    m.transition(DelegationMemberStatus.FAILED)
    assert m.version == 3


def test_member_skip_pending_to_cancelled_via_succeeded_illegal():
    m = _new_member()
    with pytest.raises(DelegationStateError):
        m.transition(DelegationMemberStatus.SUCCEEDED)


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_delegation_new_sets_initial_state():
    d = _new_delegation()
    assert d.status is DelegationStatus.PENDING
    assert d.version == 1
    assert d.first_run_id is None
    assert d.join_policy is DelegationJoinPolicy.ALL_COMPLETED
    assert d.aggregation is DelegationAggregationPolicy.PARENT
    assert d.parent.source == "task"
    assert d.parent.scope_id == "t1"
    assert d.parent.run_id == "r1"
    assert d.parent.session_id == "s1"
    assert d.delegation_key == "k1"
    assert d.fingerprint == "fp1"
    assert d.policy_snapshot_id == "ps1"
    assert d.budget_total_tokens == 1000
    assert d.id  # generated non-empty


def test_delegation_new_accepts_enum_for_policy():
    d = Delegation.new(
        parent=_parent_ref(),
        delegation_key="k1",
        fingerprint="fp1",
        join_policy=DelegationJoinPolicy.BEST_EFFORT,
        aggregation=DelegationAggregationPolicy.AGENT,
        deadline_at="2026-08-12T03:00:00Z",
        policy_snapshot_id="ps1",
        budget_total_tokens=1000,
    )
    assert d.join_policy is DelegationJoinPolicy.BEST_EFFORT
    assert d.aggregation is DelegationAggregationPolicy.AGENT


def test_delegation_parent_ref_is_frozen():
    ref = _parent_ref()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.source = "other"  # type: ignore[misc]


def test_member_new_sets_initial_state():
    m = _new_member(ordinal=2)
    assert m.status is DelegationMemberStatus.PENDING
    assert m.version == 1
    assert m.role is DelegationMemberRole.WORKER
    assert m.ordinal == 2
    assert m.delegation_id == "d1"
    assert m.title == "w2"
    assert m.execution_session_id == "delegation-sess-2"
    assert m.budget_tokens == 500
    assert m.id  # generated non-empty
    assert m.retry_count == 0
    assert m.claim_lock is None


def test_member_new_aggregator_role():
    m = _new_member(role=DelegationMemberRole.AGGREGATOR)
    assert m.role is DelegationMemberRole.AGGREGATOR


# ---------------------------------------------------------------------------
# DelegationChildSpec
# ---------------------------------------------------------------------------


def test_child_spec_construction():
    spec = DelegationChildSpec(
        title="worker-0",
        instruction="do the thing",
        skills=("search",),
        allowed_tools=("browser",),
        model_override="gpt-4",
        max_runtime_seconds=300,
        budget_tokens=500,
        output_schema=None,
    )
    assert spec.title == "worker-0"
    assert spec.skills == ("search",)
    assert spec.allowed_tools == ("browser",)
    assert spec.model_override == "gpt-4"
    assert spec.max_runtime_seconds == 300
    assert spec.budget_tokens == 500


def test_child_spec_defaults():
    spec = DelegationChildSpec(
        title="w", instruction="do",
    )
    assert spec.skills == ()
    assert spec.allowed_tools == ()
    assert spec.model_override is None
    assert spec.max_runtime_seconds is None
    assert spec.budget_tokens == 0
    assert spec.output_schema is None


# ---------------------------------------------------------------------------
# Join policy evaluation: all_completed
# ---------------------------------------------------------------------------


class TestJoinPolicyAllCompleted:
    """all_completed: delegation SUCCEEDED only if all members succeeded."""

    POLICY = DelegationJoinPolicy.ALL_COMPLETED

    def test_all_succeed(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
            _result(DelegationMemberStatus.SUCCEEDED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.SUCCEEDED
        assert rs.partial is False
        assert rs.partial_reason is None
        assert len(rs.member_results) == 2

    def test_partial_failure(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
            _result(DelegationMemberStatus.FAILED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.FAILED
        assert rs.partial is False

    def test_all_fail(self):
        results = (
            _result(DelegationMemberStatus.FAILED, 0),
            _result(DelegationMemberStatus.FAILED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.FAILED

    def test_deadline_partial(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
            partial=True,
            partial_reason="deadline",
        )
        assert rs.status is DelegationStatus.FAILED
        assert rs.partial is True
        assert rs.partial_reason == "deadline"

    def test_parent_cancel(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
            partial=True,
            partial_reason="cancelled",
        )
        assert rs.status is DelegationStatus.CANCELLED
        assert rs.partial is True
        assert rs.partial_reason == "cancelled"


# ---------------------------------------------------------------------------
# Join policy evaluation: all_succeeded
# ---------------------------------------------------------------------------


class TestJoinPolicyAllSucceeded:
    """all_succeeded: same outcome logic as all_completed; the difference
    is in WHEN to join (caller fails early on first failure)."""

    POLICY = DelegationJoinPolicy.ALL_SUCCEEDED

    def test_all_succeed(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
            _result(DelegationMemberStatus.SUCCEEDED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.SUCCEEDED

    def test_partial_failure(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
            _result(DelegationMemberStatus.FAILED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
            partial=True,
            partial_reason="early_failure",
        )
        assert rs.status is DelegationStatus.FAILED
        assert rs.partial is True
        assert rs.partial_reason == "early_failure"

    def test_all_fail(self):
        results = (
            _result(DelegationMemberStatus.FAILED, 0),
            _result(DelegationMemberStatus.FAILED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.FAILED

    def test_deadline_partial(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
            partial=True,
            partial_reason="deadline",
        )
        assert rs.status is DelegationStatus.FAILED
        assert rs.partial is True

    def test_parent_cancel(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
            partial=True,
            partial_reason="cancelled",
        )
        assert rs.status is DelegationStatus.CANCELLED
        assert rs.partial is True


# ---------------------------------------------------------------------------
# Join policy evaluation: best_effort
# ---------------------------------------------------------------------------


class TestJoinPolicyBestEffort:
    """best_effort: delegation SUCCEEDED if at least one member succeeded."""

    POLICY = DelegationJoinPolicy.BEST_EFFORT

    def test_all_succeed(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
            _result(DelegationMemberStatus.SUCCEEDED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.SUCCEEDED

    def test_partial_failure(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
            _result(DelegationMemberStatus.FAILED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.SUCCEEDED

    def test_all_fail(self):
        results = (
            _result(DelegationMemberStatus.FAILED, 0),
            _result(DelegationMemberStatus.FAILED, 1),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
        )
        assert rs.status is DelegationStatus.FAILED

    def test_deadline_partial(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
            partial=True,
            partial_reason="deadline",
        )
        assert rs.status is DelegationStatus.SUCCEEDED
        assert rs.partial is True
        assert rs.partial_reason == "deadline"

    def test_parent_cancel(self):
        results = (
            _result(DelegationMemberStatus.SUCCEEDED, 0),
        )
        rs = evaluate_join_outcome(
            delegation_id="d1",
            join_policy=self.POLICY,
            aggregation=DelegationAggregationPolicy.PARENT,
            member_results=results,
            partial=True,
            partial_reason="cancelled",
        )
        assert rs.status is DelegationStatus.CANCELLED
        assert rs.partial is True


# ---------------------------------------------------------------------------
# Aggregator failure
# ---------------------------------------------------------------------------


def test_aggregator_failure_delegation_failed():
    results = (
        _result(DelegationMemberStatus.SUCCEEDED, 0),
        _result(DelegationMemberStatus.SUCCEEDED, 1),
    )
    agg_result = DelegationResult(
        status=DelegationMemberStatus.FAILED,
        summary="aggregator failed",
        structured_data=None,
        artifact_refs=(),
        error_code="AGGREGATOR_ERROR",
        error_message="aggregation step failed",
        usage_summary={},
        classification=None,
        checksum="sha256:agg",
        started_at="2026-08-12T02:30:00Z",
        ended_at="2026-08-12T02:40:00Z",
    )
    rs = evaluate_join_outcome(
        delegation_id="d1",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.AGENT,
        member_results=results,
        aggregation_result=agg_result,
    )
    assert rs.status is DelegationStatus.FAILED
    assert rs.aggregation_result is not None
    assert rs.aggregation_result.error_code == "AGGREGATOR_ERROR"


def test_aggregator_success_delegation_succeeded():
    results = (
        _result(DelegationMemberStatus.SUCCEEDED, 0),
        _result(DelegationMemberStatus.FAILED, 1),
    )
    agg_result = DelegationResult(
        status=DelegationMemberStatus.SUCCEEDED,
        summary="aggregator ok",
        structured_data=None,
        artifact_refs=(),
        error_code=None,
        error_message=None,
        usage_summary={},
        classification=None,
        checksum="sha256:agg",
        started_at="2026-08-12T02:30:00Z",
        ended_at="2026-08-12T02:40:00Z",
    )
    rs = evaluate_join_outcome(
        delegation_id="d1",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.AGENT,
        member_results=results,
        aggregation_result=agg_result,
    )
    assert rs.status is DelegationStatus.SUCCEEDED


def test_aggregator_cancelled_delegation_failed():
    results = (
        _result(DelegationMemberStatus.SUCCEEDED, 0),
        _result(DelegationMemberStatus.SUCCEEDED, 1),
    )
    agg_result = DelegationResult(
        status=DelegationMemberStatus.CANCELLED,
        summary="aggregator cancelled",
        structured_data=None,
        artifact_refs=(),
        error_code=None,
        error_message=None,
        usage_summary={},
        classification=None,
        checksum="sha256:agg",
        started_at="2026-08-12T02:30:00Z",
        ended_at="2026-08-12T02:40:00Z",
    )
    rs = evaluate_join_outcome(
        delegation_id="d1",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.AGENT,
        member_results=results,
        aggregation_result=agg_result,
    )
    assert rs.status is DelegationStatus.FAILED


def test_aggregator_expired_delegation_failed():
    results = (
        _result(DelegationMemberStatus.SUCCEEDED, 0),
        _result(DelegationMemberStatus.SUCCEEDED, 1),
    )
    agg_result = DelegationResult(
        status=DelegationMemberStatus.EXPIRED,
        summary="aggregator expired",
        structured_data=None,
        artifact_refs=(),
        error_code=None,
        error_message=None,
        usage_summary={},
        classification=None,
        checksum="sha256:agg",
        started_at="2026-08-12T02:30:00Z",
        ended_at="2026-08-12T02:40:00Z",
    )
    rs = evaluate_join_outcome(
        delegation_id="d1",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.AGENT,
        member_results=results,
        aggregation_result=agg_result,
    )
    assert rs.status is DelegationStatus.FAILED


# ---------------------------------------------------------------------------
# Worker result preservation and late success audit-only
# ---------------------------------------------------------------------------


def test_worker_results_preserved_in_ordinal_order():
    results = (
        _result(DelegationMemberStatus.SUCCEEDED, 0, "r0"),
        _result(DelegationMemberStatus.FAILED, 1, "r1"),
        _result(DelegationMemberStatus.SUCCEEDED, 2, "r2"),
    )
    rs = evaluate_join_outcome(
        delegation_id="d1",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.PARENT,
        member_results=results,
    )
    assert len(rs.member_results) == 3
    assert rs.member_results[0].summary == "r0"
    assert rs.member_results[1].summary == "r1"
    assert rs.member_results[2].summary == "r2"


def test_late_success_cannot_change_terminal_state():
    d = _new_delegation()
    d.transition(DelegationStatus.RUNNING)
    d.transition(DelegationStatus.JOINING)
    d.transition(DelegationStatus.FAILED)
    assert d.is_terminal is True
    with pytest.raises(DelegationStateError):
        d.transition(DelegationStatus.SUCCEEDED)


def test_late_success_recorded_in_filter_notes():
    results = (
        _result(DelegationMemberStatus.SUCCEEDED, 0),
        _result(DelegationMemberStatus.FAILED, 1),
    )
    notes = ("member 2 succeeded after terminal (audit only)",)
    rs = evaluate_join_outcome(
        delegation_id="d1",
        join_policy=DelegationJoinPolicy.ALL_SUCCEEDED,
        aggregation=DelegationAggregationPolicy.PARENT,
        member_results=results,
        filter_notes=notes,
    )
    assert rs.status is DelegationStatus.FAILED
    assert "member 2 succeeded after terminal (audit only)" in rs.filter_notes


def test_result_set_total_usage_aggregated():
    results = (
        _result(DelegationMemberStatus.SUCCEEDED, 0),
        _result(DelegationMemberStatus.SUCCEEDED, 1),
    )
    rs = evaluate_join_outcome(
        delegation_id="d1",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.PARENT,
        member_results=results,
        total_usage={"prompt_tokens": 20, "completion_tokens": 10},
    )
    assert rs.total_usage is not None
    assert rs.total_usage["prompt_tokens"] == 20
    assert rs.total_usage["completion_tokens"] == 10


def test_result_set_delegation_id():
    results = (_result(DelegationMemberStatus.SUCCEEDED, 0),)
    rs = evaluate_join_outcome(
        delegation_id="del-42",
        join_policy=DelegationJoinPolicy.ALL_COMPLETED,
        aggregation=DelegationAggregationPolicy.PARENT,
        member_results=results,
    )
    assert rs.delegation_id == "del-42"


# ---------------------------------------------------------------------------
# Port Protocols
# ---------------------------------------------------------------------------


def test_port_protocols_define_expected_methods():
    # DelegationRegistry
    for method in (
        "create_or_reconnect", "get", "list_for_trusted_scope",
        "append_event", "list_events", "claim_member", "finish_member",
        "reserve_ledger", "settle_ledger", "release_ledger", "get_result_set",
    ):
        assert hasattr(DelegationRegistry, method), f"missing {method}"
    # Clock
    assert hasattr(Clock, "now_iso")
    # DelegationDispatcher
    for method in ("spawn", "cancel", "inspect"):
        assert hasattr(DelegationDispatcher, method), f"missing {method}"


def test_mutation_outcome_enum_values():
    assert MutationOutcome.SUCCESS == "success"
    assert MutationOutcome.IDEMPOTENT_REPLAY == "idempotent_replay"
    assert MutationOutcome.CONFLICT == "conflict"
    assert MutationOutcome.BUSY == "busy"


def test_claim_member_result_construction():
    m = _new_member()
    r = ClaimMemberResult(
        outcome=MutationOutcome.SUCCESS,
        member=m,
    )
    assert r.outcome is MutationOutcome.SUCCESS
    assert r.member is not None
    assert r.delegation is None


def test_finish_member_result_construction():
    m = _new_member()
    r = FinishMemberResult(
        outcome=MutationOutcome.IDEMPOTENT_REPLAY,
        member=m,
    )
    assert r.outcome is MutationOutcome.IDEMPOTENT_REPLAY
    assert r.result_set is None


def test_ledger_result_construction():
    r = LedgerResult(
        outcome=MutationOutcome.CONFLICT,
    )
    assert r.outcome is MutationOutcome.CONFLICT
    assert r.reservation_id is None
    assert r.balance is None


def test_delegation_event_construction():
    e = DelegationEvent(
        id=1,
        delegation_id="d1",
        kind="member_started",
        payload={"ordinal": 0},
    )
    assert e.id == 1
    assert e.delegation_id == "d1"
    assert e.kind == "member_started"
    assert e.payload == {"ordinal": 0}


# ---------------------------------------------------------------------------
# Immutability regression tests (MappingProxyType wrapping)
# ---------------------------------------------------------------------------


def test_delegation_event_payload_is_immutable():
    e = DelegationEvent(
        id=1,
        delegation_id="d1",
        kind="member_started",
        payload={"ordinal": 0},
    )
    with pytest.raises(TypeError):
        e.payload["ordinal"] = 99  # type: ignore[index]


def test_delegation_result_usage_summary_is_immutable():
    r = _result(DelegationMemberStatus.SUCCEEDED, 0)
    with pytest.raises(TypeError):
        r.usage_summary["prompt_tokens"] = 999  # type: ignore[index]


def test_delegation_result_structured_data_is_immutable():
    r = DelegationResult(
        status=DelegationMemberStatus.SUCCEEDED,
        summary="x",
        structured_data={"key": "value"},
    )
    with pytest.raises(TypeError):
        r.structured_data["key"] = "other"  # type: ignore[index]


def test_delegation_result_set_total_usage_is_immutable():
    rs = DelegationResultSet(
        delegation_id="d1",
        status=DelegationStatus.SUCCEEDED,
        total_usage={"prompt_tokens": 20},
    )
    with pytest.raises(TypeError):
        rs.total_usage["prompt_tokens"] = 999  # type: ignore[index]
