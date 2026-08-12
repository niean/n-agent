"""Tests for DelegationPolicy -- the 17th domain Policy (Domain Layer).

Covers all 9 validation rules from S3:
  1. has_capability=False -> DENY
  2. depth != 1, children < 1 or > max_children -> DENY
  3. forbidden tools in parent or child allowed_tools -> DENY
  4. child tools not subset of parent ∩ system allowlist -> DENY
  5. blank instruction, length limits, duplicate normalized spec -> DENY
  6. budget/runtime positivity and aggregate limits -> DENY
  7. timeout vs parent_deadline, source lifecycle, config cap -> DENY
  8. aggregation=agent without aggregator, role confusion -> DENY
  9. all checks pass -> ALLOW
"""
import pytest
from app.domain.delegation_policy import DelegationPolicy, DelegationPolicyRequest, FORBIDDEN_CHILD_TOOLS
from app.domain.delegation import DelegationChildSpec, DelegationParentRef
from app.domain.policy import PolicyOutcome


def _spec(title="w", instruction="do", budget_tokens=100, allowed_tools=()):
    return DelegationChildSpec(
        title=title,
        instruction=instruction,
        skills=(),
        allowed_tools=allowed_tools,
        model_override=None,
        max_runtime_seconds=300,
        budget_tokens=budget_tokens,
        output_schema=None,
    )


def _make_req(**overrides):
    """Build a valid DelegationPolicyRequest with overridable fields."""
    defaults = dict(
        parent=DelegationParentRef(
            source="task", scope_id="t1", run_id="r", session_id="s"
        ),
        has_capability=True,
        children=[_spec(title="w1"), _spec(title="w2")],
        join_policy="all_completed",
        aggregation="parent",
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
        max_children=8,
        depth=1,
        max_total_tokens=1000,
        timeout_seconds=60,
    )
    defaults.update(overrides)
    return DelegationPolicyRequest(**defaults)


# ---------------------------------------------------------------------------
# Seed tests (from plan, S1)
# ---------------------------------------------------------------------------


def test_policy_denies_missing_capability():
    req = DelegationPolicyRequest(
        parent=DelegationParentRef(
            source="realtime", scope_id="rs", run_id="r", session_id="s"
        ),
        has_capability=False,
        children=[_spec()],
        join_policy="all_completed",
        aggregation="parent",
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
        max_children=8,
        depth=1,
        max_total_tokens=1000,
        timeout_seconds=60,
    )
    outcome = DelegationPolicy().evaluate(req)
    assert outcome is PolicyOutcome.DENY  # 无 capability


def test_policy_denies_child_count_exceeds_max():
    req = DelegationPolicyRequest(
        parent=DelegationParentRef(
            source="task", scope_id="t1", run_id="r", session_id="s"
        ),
        has_capability=True,
        children=[_spec() for _ in range(9)],
        join_policy="all_completed",
        aggregation="parent",
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
        max_children=8,
        depth=1,
        max_total_tokens=1000,
        timeout_seconds=60,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_tool_not_in_intersection():
    # Rule 4: child allowed_tools must be subset of parent ∩ system.
    # "search_web" is in parent_allowed_tools but NOT in system_child_allowlist,
    # so it is not in the intersection. The child requests it -> DENY.
    child = _spec(title="w1", allowed_tools=("search_web",))
    req = DelegationPolicyRequest(
        parent=DelegationParentRef(
            source="task", scope_id="t1", run_id="r", session_id="s"
        ),
        has_capability=True,
        children=[child],
        join_policy="all_completed",
        aggregation="parent",
        parent_allowed_tools=frozenset({"get_current_time", "search_web"}),
        system_child_allowlist=frozenset({"get_current_time"}),
        max_children=8,
        depth=1,
        max_total_tokens=1000,
        timeout_seconds=60,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_budget_overrun():
    req = DelegationPolicyRequest(
        parent=DelegationParentRef(
            source="task", scope_id="t1", run_id="r", session_id="s"
        ),
        has_capability=True,
        children=[
            _spec(title="w1", budget_tokens=600),
            _spec(title="w2", budget_tokens=600),
        ],
        join_policy="all_completed",
        aggregation="parent",
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
        max_children=8,
        depth=1,
        max_total_tokens=1000,
        timeout_seconds=60,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_valid_request():
    req = DelegationPolicyRequest(
        parent=DelegationParentRef(
            source="task", scope_id="t1", run_id="r", session_id="s"
        ),
        has_capability=True,
        children=[_spec(title="w1"), _spec(title="w2")],
        join_policy="all_completed",
        aggregation="parent",
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
        max_children=8,
        depth=1,
        max_total_tokens=1000,
        timeout_seconds=60,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Rule 1: capability
# ---------------------------------------------------------------------------


def test_policy_allows_with_capability():
    assert DelegationPolicy().evaluate(_make_req(has_capability=True)) is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Rule 2: depth and child count
# ---------------------------------------------------------------------------


def test_policy_denies_depth_not_one():
    assert DelegationPolicy().evaluate(_make_req(depth=2)) is PolicyOutcome.DENY


def test_policy_denies_depth_zero():
    assert DelegationPolicy().evaluate(_make_req(depth=0)) is PolicyOutcome.DENY


def test_policy_denies_no_children():
    assert DelegationPolicy().evaluate(_make_req(children=[])) is PolicyOutcome.DENY


def test_policy_denies_children_exceeds_max():
    children = [_spec(title=f"w{i}") for i in range(9)]
    assert (
        DelegationPolicy().evaluate(_make_req(children=children, max_children=8))
        is PolicyOutcome.DENY
    )


def test_policy_allows_children_at_max():
    children = [_spec(title=f"w{i}") for i in range(8)]
    assert (
        DelegationPolicy().evaluate(_make_req(children=children, max_children=8))
        is PolicyOutcome.ALLOW
    )


# ---------------------------------------------------------------------------
# Rule 3: forbidden tools
# ---------------------------------------------------------------------------


def test_policy_denies_forbidden_tool_in_child():
    child = _spec(
        title="w1",
        allowed_tools=("delegate_agents",),  # forbidden
    )
    req = _make_req(
        children=[child, _spec(title="w2")],
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_forbidden_tool_in_parent_allowed_tools():
    req = _make_req(
        parent_allowed_tools=frozenset({"get_current_time", "create_task"}),
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_each_forbidden_tool_individually():
    """Every tool in FORBIDDEN_CHILD_TOOLS must be denied in parent_allowed_tools."""
    assert len(FORBIDDEN_CHILD_TOOLS) == 15
    for tool in FORBIDDEN_CHILD_TOOLS:
        req = _make_req(
            parent_allowed_tools=frozenset({"get_current_time", tool}),
        )
        assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY, (
            f"tool {tool!r} should be forbidden"
        )


# ---------------------------------------------------------------------------
# Rule 4: child tools subset of parent ∩ system allowlist
# ---------------------------------------------------------------------------


def test_policy_denies_child_tool_not_in_parent():
    child = _spec(
        title="w1",
        allowed_tools=("get_current_time", "unknown_tool"),
    )
    req = _make_req(
        children=[child, _spec(title="w2")],
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time", "unknown_tool"}),
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_child_tool_not_in_system():
    child = _spec(
        title="w1",
        allowed_tools=("get_current_time", "unknown_tool"),
    )
    req = _make_req(
        children=[child, _spec(title="w2")],
        parent_allowed_tools=frozenset({"get_current_time", "unknown_tool"}),
        system_child_allowlist=frozenset({"get_current_time"}),
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_child_tool_in_intersection():
    child = _spec(
        title="w1",
        allowed_tools=("get_current_time",),
    )
    req = _make_req(
        children=[child, _spec(title="w2")],
        parent_allowed_tools=frozenset({"get_current_time", "extra_parent_tool"}),
        system_child_allowlist=frozenset({"get_current_time", "extra_system_tool"}),
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Rule 5: instruction/skills/schema length, blank instruction, duplicate spec
# ---------------------------------------------------------------------------


def test_policy_denies_blank_instruction():
    child = _spec(title="w1", instruction="   ")
    req = _make_req(children=[child, _spec(title="w2")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_empty_instruction():
    child = _spec(title="w1", instruction="")
    req = _make_req(children=[child, _spec(title="w2")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_instruction_too_long():
    child = _spec(title="w1", instruction="x" * 8001)
    req = _make_req(children=[child, _spec(title="w2")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_title_too_long():
    child = _spec(title="T" * 201, instruction="do")
    req = _make_req(children=[child, _spec(title="w2")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_too_many_skills():
    child = _spec(title="w1")
    child = DelegationChildSpec(
        title=child.title,
        instruction=child.instruction,
        skills=tuple(f"skill_{i}" for i in range(21)),
        allowed_tools=(),
        model_override=None,
        max_runtime_seconds=300,
        budget_tokens=100,
        output_schema=None,
    )
    req = _make_req(children=[child, _spec(title="w2")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_duplicate_normalized_spec():
    """Two children with same (title, instruction, skills, tools, model) -> DENY."""
    child1 = _spec(title="w", instruction="do")
    child2 = _spec(title="w", instruction="do")
    req = _make_req(children=[child1, child2])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_duplicate_spec_different_case():
    """Normalization lowercases title, so 'W' and 'w' are duplicates."""
    child1 = _spec(title="W", instruction="do")
    child2 = _spec(title="w", instruction="do")
    req = _make_req(children=[child1, child2])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_different_specs():
    child1 = _spec(title="w1", instruction="do task 1")
    child2 = _spec(title="w2", instruction="do task 2")
    req = _make_req(children=[child1, child2])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Rule 6: budget and runtime
# ---------------------------------------------------------------------------


def test_policy_denies_negative_budget():
    child = _spec(title="w1", budget_tokens=-1)
    req = _make_req(children=[child, _spec(title="w2")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_child_budget_exceeds_per_child_limit():
    child = _spec(title="w1", budget_tokens=500)
    req = _make_req(
        children=[child, _spec(title="w2", budget_tokens=100)],
        max_tokens_per_child=200,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_child_runtime_not_positive():
    child = DelegationChildSpec(
        title="w1",
        instruction="do",
        skills=(),
        allowed_tools=(),
        model_override=None,
        max_runtime_seconds=0,
        budget_tokens=100,
        output_schema=None,
    )
    req = _make_req(children=[child, _spec(title="w2")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_child_runtime_exceeds_member_limit():
    child = _spec(title="w1")  # max_runtime_seconds=300
    req = _make_req(
        children=[child, _spec(title="w2")],
        member_max_runtime_seconds=200,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_child_runtime_within_member_limit():
    child = _spec(title="w1")  # max_runtime_seconds=300
    req = _make_req(
        children=[child, _spec(title="w2")],
        member_max_runtime_seconds=300,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_denies_aggregate_budget_exceeds_max_total():
    children = [
        _spec(title="w1", budget_tokens=600),
        _spec(title="w2", budget_tokens=600),
    ]
    req = _make_req(children=children, max_total_tokens=1000)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_aggregate_budget_at_limit():
    children = [
        _spec(title="w1", budget_tokens=500),
        _spec(title="w2", budget_tokens=500),
    ]
    req = _make_req(children=children, max_total_tokens=1000)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_allows_parent_aggregation_ignores_aggregator_budget():
    """PARENT aggregation: aggregator does not run, so its budget is not
    counted against max_total_tokens. children=[300, 300]=600 <= 1000 -> ALLOW
    even though aggregator_spec.budget_tokens=500 would push total to 1100."""
    children = [
        _spec(title="w1", budget_tokens=300),
        _spec(title="w2", budget_tokens=300),
    ]
    agg = _spec(title="agg", budget_tokens=500)
    req = _make_req(
        children=children,
        aggregation="parent",
        aggregator_spec=agg,
        max_total_tokens=1000,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_denies_agent_aggregation_counts_aggregator_budget():
    """AGENT aggregation: aggregator runs, so its budget IS counted.
    children=[300, 300] + aggregator=500 = 1100 > 1000 -> DENY."""
    children = [
        _spec(title="w1", budget_tokens=300),
        _spec(title="w2", budget_tokens=300),
    ]
    agg = _spec(title="agg", budget_tokens=500)
    req = _make_req(
        children=children,
        aggregation="agent",
        aggregator_spec=agg,
        max_total_tokens=1000,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# Rule 7: timeout
# ---------------------------------------------------------------------------


def test_policy_denies_timeout_exceeds_parent_deadline():
    req = _make_req(timeout_seconds=120, parent_deadline=60.0)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_timeout_within_parent_deadline():
    req = _make_req(timeout_seconds=60, parent_deadline=120.0)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_denies_realtime_timeout_exceeds_limit():
    req = _make_req(
        parent=DelegationParentRef(
            source="realtime", scope_id="rs", run_id="r", session_id="s"
        ),
        timeout_seconds=301,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_realtime_timeout_within_limit():
    req = _make_req(
        parent=DelegationParentRef(
            source="realtime", scope_id="rs", run_id="r", session_id="s"
        ),
        timeout_seconds=300,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_denies_task_timeout_exceeds_limit():
    req = _make_req(timeout_seconds=3601)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_timeout_exceeds_max_runtime():
    req = _make_req(timeout_seconds=120, max_runtime_seconds=60)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_no_timeout():
    req = _make_req(timeout_seconds=None)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_denies_timeout_zero():
    req = _make_req(timeout_seconds=0)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_timeout_negative():
    req = _make_req(timeout_seconds=-1)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


# ---------------------------------------------------------------------------
# Rule 8: aggregator validation
# ---------------------------------------------------------------------------


def test_policy_denies_agent_aggregation_without_aggregator():
    req = _make_req(aggregation="agent", aggregator_spec=None)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_aggregator_with_forbidden_tool():
    agg = DelegationChildSpec(
        title="agg",
        instruction="aggregate results",
        skills=(),
        allowed_tools=("delegate_agents",),
        model_override=None,
        max_runtime_seconds=300,
        budget_tokens=100,
        output_schema=None,
    )
    req = _make_req(
        aggregation="agent",
        aggregator_spec=agg,
        parent_allowed_tools=frozenset({"get_current_time", "delegate_agents"}),
        system_child_allowlist=frozenset({"get_current_time", "delegate_agents"}),
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_aggregator_tool_not_in_intersection():
    agg = DelegationChildSpec(
        title="agg",
        instruction="aggregate results",
        skills=(),
        allowed_tools=("get_current_time", "unknown_tool"),
        model_override=None,
        max_runtime_seconds=300,
        budget_tokens=100,
        output_schema=None,
    )
    req = _make_req(
        aggregation="agent",
        aggregator_spec=agg,
        parent_allowed_tools=frozenset({"get_current_time"}),
        system_child_allowlist=frozenset({"get_current_time"}),
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_aggregator_duplicate_of_child():
    """Aggregator spec must not be a duplicate of any child (role confusion)."""
    agg = _spec(title="w1", instruction="do")  # same as child w1
    req = _make_req(
        aggregation="agent",
        aggregator_spec=agg,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_aggregator_blank_instruction():
    agg = _spec(title="agg", instruction="   ")
    req = _make_req(
        aggregation="agent",
        aggregator_spec=agg,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_aggregator_negative_budget():
    agg = _spec(title="agg", budget_tokens=-1)
    req = _make_req(
        aggregation="agent",
        aggregator_spec=agg,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_denies_aggregator_budget_exceeds_total():
    children = [
        _spec(title="w1", budget_tokens=500),
        _spec(title="w2", budget_tokens=500),
    ]
    agg = _spec(title="agg", budget_tokens=200)
    req = _make_req(
        children=children,
        aggregation="agent",
        aggregator_spec=agg,
        max_total_tokens=1000,
    )
    # 500 + 500 + 200 = 1200 > 1000
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.DENY


def test_policy_allows_agent_aggregation_valid():
    agg = DelegationChildSpec(
        title="agg",
        instruction="aggregate results",
        skills=(),
        allowed_tools=(),
        model_override=None,
        max_runtime_seconds=300,
        budget_tokens=100,
        output_schema=None,
    )
    req = _make_req(
        aggregation="agent",
        aggregator_spec=agg,
        max_total_tokens=1000,
    )
    # children: 100 + 100 = 200, aggregator: 100, total: 300 <= 1000
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_allows_parent_aggregation_no_artifact():
    """Parent aggregation does not require aggregator_spec or artifact."""
    req = _make_req(aggregation="parent", aggregator_spec=None)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


# ---------------------------------------------------------------------------
# Rule 9: all checks pass -> ALLOW (additional edge cases)
# ---------------------------------------------------------------------------


def test_policy_allows_single_child():
    req = _make_req(children=[_spec(title="w1")])
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_allows_no_total_token_limit():
    """When max_total_tokens is None, budget sum check is skipped."""
    children = [
        _spec(title="w1", budget_tokens=999999),
        _spec(title="w2", budget_tokens=999999),
    ]
    req = _make_req(children=children, max_total_tokens=None)
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW


def test_policy_allows_no_member_runtime_limit():
    """When member_max_runtime_seconds is None, per-member runtime check skipped."""
    child = _spec(title="w1", )  # max_runtime_seconds=300
    req = _make_req(
        children=[child, _spec(title="w2")],
        member_max_runtime_seconds=None,
    )
    assert DelegationPolicy().evaluate(req) is PolicyOutcome.ALLOW
