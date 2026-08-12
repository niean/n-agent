"""DelegationPolicy -- the 17th domain Policy (Domain Layer).

Pure domain: imports only stdlib + ``app.domain.policy`` (shared kernel) +
``app.domain.delegation`` (own domain types). No Application, Infrastructure,
or other Policy imports (AST-enforced by ``tests/architecture/test_policy_boundaries.py``).

Governance scope (delegation admission control):
  1. Parent capability check (has_capability=False -> DENY).
  2. Depth must be 1; child count must be within [1, max_children].
  3. Forbidden-tool enforcement on parent_allowed_tools and each child's
     allowed_tools.
  4. Each child's allowed_tools must be a subset of
     parent_allowed_tools ∩ system_child_allowlist.
  5. Instruction/title/skills/schema length limits, blank instruction,
     duplicate normalized spec.
  6. Budget/runtime positivity and aggregate token limits.
  7. Timeout vs parent_deadline, source lifecycle cap, config cap.
  8. Aggregator spec validation for AGENT aggregation (required, passes
     same tool/budget/length checks, no role confusion with workers).
  9. All checks pass -> ALLOW.

Returns bare ``PolicyOutcome`` enum members (matches ``task_policy.py`` and
``skill_policy.py`` contract). Deny reasons are logged by the Application
layer; they are not encoded into the return value.

This Policy does NOT query the database, call LLMs, or import other domain
Policies. It is a pure function of the request.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.delegation import (
    DelegationAggregationPolicy,
    DelegationChildSpec,
    DelegationJoinPolicy,
    DelegationParentRef,
)
from app.domain.policy import Policy, PolicyOutcome


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Tools that must never be granted to child agents. These are parent-level
#: or system-level capabilities that would allow a child to escape its
#: sandbox, create recursive delegations, or manage task lifecycle.
FORBIDDEN_CHILD_TOOLS: frozenset[str] = frozenset(
    {
        "delegate_agents",
        "create_task",
        "list_tasks",
        "approve_task",
        "reject_task",
        "revise_task",
        "task_show",
        "task_complete",
        "task_heartbeat",
        "task_propose_change",
        "task_fail",
        "manage_schedule",
        "schedule_query",
        "skill_manage",
        "manage_plugin",
    }
)

#: Maximum delegation timeout for realtime source (turn/stream deadline).
_MAX_REALTIME_TIMEOUT_SECONDS: int = 300

#: Maximum delegation timeout for task source (task lifecycle cap).
_MAX_TASK_TIMEOUT_SECONDS: int = 3600

#: Length / count limits for child specs.
_MAX_TITLE_LENGTH: int = 200
_MAX_INSTRUCTION_LENGTH: int = 8000
_MAX_SKILLS_COUNT: int = 20
_MAX_SCHEMA_REPR_LENGTH: int = 16384


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelegationPolicyRequest:
    """DelegationPolicy evaluation request.

    Fields:
      parent: reference to the parent context (source, scope_id, run_id,
          session_id).
      has_capability: whether the parent has the delegation capability
          granted.
      children: tuple of DelegationChildSpec for worker agents.
      join_policy: when the delegation joins and how member outcomes
          determine final status.
      aggregation: how aggregated results are produced (PARENT or AGENT).
      depth: delegation depth -- must be 1 (children are depth-0 leaf
          agents; no recursive delegation).
      aggregator_spec: spec for the aggregator member when aggregation is
          AGENT; None for PARENT aggregation.
      parent_allowed_tools: tools the parent is allowed to grant to children.
      system_child_allowlist: system-level allowlist of tools that child
          agents may use.
      max_children: maximum number of worker children allowed.
      max_runtime_seconds: delegation-level max runtime (None = no limit).
      member_max_runtime_seconds: per-member max runtime (None = no limit).
      max_total_tokens: total token budget for all children + aggregator
          (None = no limit).
      max_tokens_per_child: per-child token budget cap (None = no limit).
      timeout_seconds: delegation timeout in seconds (None = no timeout).
      parent_deadline: remaining seconds until the parent's deadline
          (None = no deadline; float for pure comparison without IO).
      classification: optional domain-specific classification string.
          Used by the Application layer for audit/routing; NOT evaluated
          by this Policy.
    """

    parent: DelegationParentRef
    has_capability: bool
    children: tuple[DelegationChildSpec, ...]
    join_policy: DelegationJoinPolicy | str = DelegationJoinPolicy.ALL_COMPLETED
    aggregation: DelegationAggregationPolicy | str = DelegationAggregationPolicy.PARENT
    depth: int = 1
    parent_allowed_tools: frozenset[str] = frozenset()
    system_child_allowlist: frozenset[str] = frozenset()
    max_children: int = 8
    max_runtime_seconds: int | None = None
    member_max_runtime_seconds: int | None = None
    max_total_tokens: int | None = None
    max_tokens_per_child: int | None = None
    timeout_seconds: int | None = None
    parent_deadline: float | None = None
    aggregator_spec: DelegationChildSpec | None = None
    classification: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))
        object.__setattr__(
            self, "parent_allowed_tools", frozenset(self.parent_allowed_tools)
        )
        object.__setattr__(
            self, "system_child_allowlist", frozenset(self.system_child_allowlist)
        )
        if isinstance(self.join_policy, str):
            object.__setattr__(
                self, "join_policy", DelegationJoinPolicy(self.join_policy)
            )
        if isinstance(self.aggregation, str):
            object.__setattr__(
                self, "aggregation", DelegationAggregationPolicy(self.aggregation)
            )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class DelegationPolicy(Policy):
    """Delegation admission governance Policy (pure function).

    Evaluates whether a parent Agent's request to delegate work to child
    Agents is allowed. All checks are pure functions of the request; no
    side effects, no IO, no database queries.

    Check order (deny wins, first denial short-circuits):
      1. Capability
      2. Depth and child count
      3. Forbidden tools (parent + children)
      4. Child tool subset of intersection
      5. Spec validation (length, blank, positivity, duplicates)
      6. Budget and runtime aggregate limits
      7. Timeout constraints
      8. Aggregator validation (for AGENT aggregation)
    """

    def evaluate(
        self,
        request: DelegationPolicyRequest,
        context: None = None,
    ) -> PolicyOutcome:
        r = request

        # 1. Capability check
        if not r.has_capability:
            return PolicyOutcome.DENY

        # 2. Depth and child-count
        if r.depth != 1:
            return PolicyOutcome.DENY
        if len(r.children) < 1 or len(r.children) > r.max_children:
            return PolicyOutcome.DENY

        # 3. Forbidden tools
        # (a) parent_allowed_tools must not contain forbidden tools
        if r.parent_allowed_tools & FORBIDDEN_CHILD_TOOLS:
            return PolicyOutcome.DENY
        # (b) each child's allowed_tools must not contain forbidden tools
        for child in r.children:
            if frozenset(child.allowed_tools) & FORBIDDEN_CHILD_TOOLS:
                return PolicyOutcome.DENY

        # 4. Child tools subset of parent ∩ system allowlist
        effective_tools = r.parent_allowed_tools & r.system_child_allowlist
        for child in r.children:
            if not frozenset(child.allowed_tools).issubset(effective_tools):
                return PolicyOutcome.DENY

        # 5. Spec validation: length, blank, positivity, duplicates
        for child in r.children:
            if not self._validate_spec(child, r):
                return PolicyOutcome.DENY
        seen: set[tuple] = set()
        for child in r.children:
            key = self._normalize_spec(child)
            if key in seen:
                return PolicyOutcome.DENY
            seen.add(key)

        # 6. Budget and runtime aggregate limits
        if not self._validate_budget_runtime(r):
            return PolicyOutcome.DENY

        # 7. Timeout constraints
        if not self._validate_timeout(r):
            return PolicyOutcome.DENY

        # 8. Aggregator validation
        if not self._validate_aggregator(r):
            return PolicyOutcome.DENY

        # 9. All checks pass
        return PolicyOutcome.ALLOW

    # ------------------------------------------------------------------
    # Spec validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_spec(
        spec: DelegationChildSpec,
        r: DelegationPolicyRequest,
    ) -> bool:
        """Validate a single child/aggregator spec.

        Returns False if the spec violates any length, blank, or
        per-member limit constraint.
        """
        # Title length
        if len(spec.title) > _MAX_TITLE_LENGTH:
            return False
        # Instruction: non-blank, length limit
        if not spec.instruction.strip():
            return False
        if len(spec.instruction) > _MAX_INSTRUCTION_LENGTH:
            return False
        # Skills count
        if len(spec.skills) > _MAX_SKILLS_COUNT:
            return False
        # Schema size (guard against oversized output schemas)
        if spec.output_schema is not None:
            if len(repr(spec.output_schema)) > _MAX_SCHEMA_REPR_LENGTH:
                return False
        # Runtime positivity (if set)
        if spec.max_runtime_seconds is not None and spec.max_runtime_seconds <= 0:
            return False
        # Budget non-negative
        if spec.budget_tokens < 0:
            return False
        # Per-child token limit
        if (
            r.max_tokens_per_child is not None
            and spec.budget_tokens > r.max_tokens_per_child
        ):
            return False
        # Per-child runtime limit
        if (
            r.member_max_runtime_seconds is not None
            and spec.max_runtime_seconds is not None
            and spec.max_runtime_seconds > r.member_max_runtime_seconds
        ):
            return False
        return True

    @staticmethod
    def _normalize_spec(spec: DelegationChildSpec) -> tuple:
        """Normalize a child spec for duplicate detection.

        Two specs with the same normalized form are considered duplicates
        (redundant work). Title is lowercased and stripped; instruction
        is stripped; skills and allowed_tools are sorted tuples; model_override
        is included as-is.
        """
        return (
            spec.title.strip().lower(),
            spec.instruction.strip(),
            tuple(sorted(spec.skills)),
            tuple(sorted(spec.allowed_tools)),
            spec.model_override,
        )

    # ------------------------------------------------------------------
    # Budget and runtime validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_budget_runtime(r: DelegationPolicyRequest) -> bool:
        """Validate budget and runtime aggregate constraints.

        - Delegation-level max_runtime_seconds must be positive if set.
        - Sum of explicit child budget_tokens (plus aggregator, only when
          aggregation=AGENT) must not exceed max_total_tokens (if set).
        """
        # Delegation-level runtime config must be positive if set
        if r.max_runtime_seconds is not None and r.max_runtime_seconds <= 0:
            return False
        # Member-level runtime config must be positive if set
        if (
            r.member_max_runtime_seconds is not None
            and r.member_max_runtime_seconds <= 0
        ):
            return False

        # Aggregate token budget
        if r.max_total_tokens is not None:
            total = sum(c.budget_tokens for c in r.children)
            # Only count aggregator budget when aggregation=AGENT; in PARENT
            # mode the aggregator does not run so its budget is not consumed.
            if (
                r.aggregation is DelegationAggregationPolicy.AGENT
                and r.aggregator_spec is not None
            ):
                total += r.aggregator_spec.budget_tokens
            if total > r.max_total_tokens:
                return False

        return True

    # ------------------------------------------------------------------
    # Timeout validation
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_timeout(r: DelegationPolicyRequest) -> bool:
        """Validate timeout constraints.

        - timeout_seconds must be positive if set.
        - Must not exceed parent_deadline (remaining seconds) if set.
        - Must not exceed source-specific lifecycle cap (realtime vs task).
        - Must not exceed max_runtime_seconds (config cap) if set.
        - realtime source cannot use delegation timeout to break
          turn/stream deadline.
        """
        if r.timeout_seconds is None:
            return True  # no timeout = no limit

        if r.timeout_seconds <= 0:
            return False

        # Parent deadline (remaining seconds)
        if r.parent_deadline is not None and r.timeout_seconds > r.parent_deadline:
            return False

        # Source lifecycle cap
        source = r.parent.source
        if source == "realtime":
            if r.timeout_seconds > _MAX_REALTIME_TIMEOUT_SECONDS:
                return False
        elif source == "task":
            if r.timeout_seconds > _MAX_TASK_TIMEOUT_SECONDS:
                return False

        # Config cap (delegation-level max runtime)
        if (
            r.max_runtime_seconds is not None
            and r.timeout_seconds > r.max_runtime_seconds
        ):
            return False

        return True

    # ------------------------------------------------------------------
    # Aggregator validation
    # ------------------------------------------------------------------

    def _validate_aggregator(self, r: DelegationPolicyRequest) -> bool:
        """Validate aggregator spec for AGENT aggregation.

        - aggregation=AGENT: aggregator_spec must be provided, pass the same
          spec/tool/budget validation as children, and must not duplicate any
          child spec (no role confusion).
        - aggregation=PARENT: no aggregator required, no artifact required.
        """
        if r.aggregation is DelegationAggregationPolicy.AGENT:
            if r.aggregator_spec is None:
                return False

            agg = r.aggregator_spec

            # Same spec validation as children
            if not self._validate_spec(agg, r):
                return False

            # Forbidden tools
            if frozenset(agg.allowed_tools) & FORBIDDEN_CHILD_TOOLS:
                return False

            # Tool subset of intersection
            effective_tools = r.parent_allowed_tools & r.system_child_allowlist
            if not frozenset(agg.allowed_tools).issubset(effective_tools):
                return False

            # No role confusion: aggregator must not duplicate any child
            agg_key = self._normalize_spec(agg)
            for child in r.children:
                if self._normalize_spec(child) == agg_key:
                    return False

        return True
