"""Delegation aggregate, value objects, ports, and exceptions (Domain Layer).

Implements the Delegation subdomain: a parent Agent (realtime chat or Task
worker) delegates parallel work to isolated depth-1 child Agents and
aggregates results. Pure domain -- no FastAPI, LangGraph, SQLite, OpenAI SDK,
pydantic, asyncio, or ``app.domain.task`` imports. Matches the frozen-dataclass
+ enum + async Protocol pattern of ``task.py`` / ``session.py``.

State transition contract (Delegation aggregate root):
  PENDING     -> RUNNING / CANCELLING / EXPIRED
  RUNNING     -> JOINING / SUCCEEDED / FAILED / CANCELLING / EXPIRED
  JOINING     -> SUCCEEDED / FAILED / CANCELLING / EXPIRED
  CANCELLING  -> CANCELLED / EXPIRED
  SUCCEEDED   -> (terminal)
  FAILED      -> (terminal)
  CANCELLED   -> (terminal)
  EXPIRED     -> (terminal)

RUNNING may transition directly to SUCCEEDED / FAILED when the join
evaluation completes inline (PARENT aggregation, all members done)
without a separate JOINING phase.

Member state transition contract:
  PENDING     -> RUNNING / CANCELLED / EXPIRED
  RUNNING     -> SUCCEEDED / FAILED / CANCELLED / EXPIRED / PENDING
                 (PENDING reserved for stale-recovery only)
  SUCCEEDED   -> (terminal)
  FAILED      -> (terminal)
  CANCELLED   -> (terminal)
  EXPIRED     -> (terminal)

Frozen dataclasses use ``object.__setattr__`` inside ``transition`` to
support controlled in-place mutation while preserving immutability for
external code. This matches the test's in-place-call pattern (``d.transition(X)``
then ``assert d.status is X``) while keeping frozen semantics elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol
from uuid import uuid4


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DelegationStatus(str, Enum):
    """Delegation aggregate root lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    JOINING = "joining"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DelegationMemberStatus(str, Enum):
    """DelegationMember lifecycle states."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class DelegationMemberRole(str, Enum):
    """Role of a DelegationMember within the delegation."""

    WORKER = "worker"
    AGGREGATOR = "aggregator"


class DelegationJoinPolicy(str, Enum):
    """Policy governing when the delegation transitions to JOINING and how
    member outcomes determine the final delegation status.

    ALL_COMPLETED  -- all members must reach a terminal state before JOINING;
                      delegation SUCCEEDED only if every member SUCCEEDED.
    ALL_SUCCEEDED  -- delegation fails early if any member fails (does not
                      wait for remaining members); SUCCEEDED only if every
                      member SUCCEEDED.
    BEST_EFFORT    -- delegation SUCCEEDED if at least one member SUCCEEDED,
                      regardless of other member outcomes.
    """

    ALL_COMPLETED = "all_completed"
    ALL_SUCCEEDED = "all_succeeded"
    BEST_EFFORT = "best_effort"


class DelegationAggregationPolicy(str, Enum):
    """How aggregated results are produced after members finish.

    PARENT -- the parent agent aggregates results inline (no separate
              aggregator member).
    AGENT  -- a dedicated AGGREGATOR member runs after all workers and
              produces the final aggregated result.
    """

    PARENT = "parent"
    AGENT = "agent"


class MutationOutcome(str, Enum):
    """Outcome of a CAS-based mutation on the registry.

    SUCCESS           -- mutation applied, state changed.
    IDEMPOTENT_REPLAY -- mutation is a no-op (same request already applied).
    CONFLICT          -- optimistic-lock version mismatch or claim held by
                         another caller.
    BUSY              -- resource is in a non-actionable state (e.g. member
                         not PENDING for claim).
    """

    SUCCESS = "success"
    IDEMPOTENT_REPLAY = "idempotent_replay"
    CONFLICT = "conflict"
    BUSY = "busy"


# ---------------------------------------------------------------------------
# State transition tables (authoritative source of truth)
# ---------------------------------------------------------------------------


DELEGATION_TERMINAL_STATES: frozenset[DelegationStatus] = frozenset(
    {
        DelegationStatus.SUCCEEDED,
        DelegationStatus.FAILED,
        DelegationStatus.CANCELLED,
        DelegationStatus.EXPIRED,
    }
)

DELEGATION_TRANSITION_TABLE: dict[DelegationStatus, frozenset[DelegationStatus]] = {
    DelegationStatus.PENDING: frozenset(
        {
            DelegationStatus.RUNNING,
            DelegationStatus.CANCELLING,
            DelegationStatus.EXPIRED,
        }
    ),
    DelegationStatus.RUNNING: frozenset(
        {
            DelegationStatus.JOINING,
            DelegationStatus.SUCCEEDED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELLING,
            DelegationStatus.EXPIRED,
        }
    ),
    DelegationStatus.JOINING: frozenset(
        {
            DelegationStatus.SUCCEEDED,
            DelegationStatus.FAILED,
            DelegationStatus.CANCELLING,
            DelegationStatus.EXPIRED,
        }
    ),
    DelegationStatus.CANCELLING: frozenset(
        {
            DelegationStatus.CANCELLED,
            DelegationStatus.EXPIRED,
        }
    ),
    DelegationStatus.SUCCEEDED: frozenset(),
    DelegationStatus.FAILED: frozenset(),
    DelegationStatus.CANCELLED: frozenset(),
    DelegationStatus.EXPIRED: frozenset(),
}


DELEGATION_MEMBER_TERMINAL_STATES: frozenset[DelegationMemberStatus] = frozenset(
    {
        DelegationMemberStatus.SUCCEEDED,
        DelegationMemberStatus.FAILED,
        DelegationMemberStatus.CANCELLED,
        DelegationMemberStatus.EXPIRED,
    }
)

DELEGATION_MEMBER_TRANSITION_TABLE: dict[
    DelegationMemberStatus, frozenset[DelegationMemberStatus]
] = {
    DelegationMemberStatus.PENDING: frozenset(
        {
            DelegationMemberStatus.RUNNING,
            DelegationMemberStatus.CANCELLED,
            DelegationMemberStatus.EXPIRED,
        }
    ),
    DelegationMemberStatus.RUNNING: frozenset(
        {
            DelegationMemberStatus.SUCCEEDED,
            DelegationMemberStatus.FAILED,
            DelegationMemberStatus.CANCELLED,
            DelegationMemberStatus.EXPIRED,
            # Stale-recovery only: registry detects a dead worker and
            # resets the member to PENDING for re-claim.
            DelegationMemberStatus.PENDING,
        }
    ),
    DelegationMemberStatus.SUCCEEDED: frozenset(),
    DelegationMemberStatus.FAILED: frozenset(),
    DelegationMemberStatus.CANCELLED: frozenset(),
    DelegationMemberStatus.EXPIRED: frozenset(),
}


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DelegationParentRef:
    """Reference to the parent context that created the delegation.

    ``source`` is the SessionSource value of the parent (e.g. "task",
    "dashboard"). ``scope_id`` is the trust-scope identifier (task id for
    task source, session id for realtime). ``run_id`` is the parent's
    current run identifier. ``session_id`` is the parent's execution
    session id.
    """

    source: str
    scope_id: str
    run_id: str
    session_id: str


@dataclass(frozen=True)
class DelegationChildSpec:
    """Specification for a child agent (worker or aggregator).

    The registry creates a ``DelegationMember`` from this spec plus
    delegation-specific fields (delegation_id, role, ordinal,
    execution_session_id, deadline_at).
    """

    title: str
    instruction: str
    skills: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    model_override: str | None = None
    max_runtime_seconds: int | None = None
    budget_tokens: int = 0
    output_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DelegationResult:
    """Terminal result of a single delegation member's execution.

    ``error_message`` is model-safe (sanitized for LLM context). ``checksum``
    is a content hash for integrity verification. ``usage_summary`` carries
    token-usage metrics. ``classification`` is an optional domain-specific
    result classification string.
    """

    status: DelegationMemberStatus
    summary: str = ""
    structured_data: Mapping[str, Any] | None = None
    artifact_refs: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    usage_summary: Mapping[str, int] = field(default_factory=dict)
    classification: str | None = None
    checksum: str = ""
    started_at: str | None = None
    ended_at: str | None = None

    def __post_init__(self) -> None:
        if self.structured_data is not None:
            object.__setattr__(
                self, "structured_data", MappingProxyType(dict(self.structured_data))
            )
        object.__setattr__(
            self, "usage_summary", MappingProxyType(dict(self.usage_summary))
        )


@dataclass(frozen=True)
class DelegationResultSet:
    """Aggregated result set for a delegation.

    ``member_results`` is ordered by member ordinal. ``aggregation_result``
    is present only when ``aggregation`` policy is AGENT. ``partial`` is
    True when the join was forced (deadline, cancel, early failure) before
    all members reached a terminal state. ``filter_notes`` contains
    audit-only notes (e.g. late-arriving successes that cannot change the
    terminal status).
    """

    delegation_id: str
    status: DelegationStatus
    member_results: tuple[DelegationResult, ...] = ()
    aggregation_result: DelegationResult | None = None
    partial: bool = False
    partial_reason: str | None = None
    filter_notes: tuple[str, ...] = ()
    total_usage: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        if self.total_usage is not None:
            object.__setattr__(
                self, "total_usage", MappingProxyType(dict(self.total_usage))
            )


@dataclass(frozen=True)
class DelegationEvent:
    """Append-only audit event for a Delegation.

    ``id`` is monotonically increasing per registry. ``member_ordinal``
    links the event to a specific member when applicable.
    """

    id: int
    delegation_id: str
    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    member_ordinal: int | None = None
    created_at: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


# ---------------------------------------------------------------------------
# Aggregates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Delegation:
    """Delegation aggregate root (frozen dataclass with controlled mutation).

    The ``transition`` method mutates ``status`` and ``version`` in place
    via ``object.__setattr__`` to support the in-place-call pattern while
    preserving frozen semantics for external code.

    Field grouping:
      - identity: id, parent, delegation_key, fingerprint
      - policy: join_policy, aggregation, deadline_at, policy_snapshot_id
      - budget: budget_total_tokens
      - state: status, version, first_run_id
      - timestamps: created_at, updated_at
      - cancel: cancellation_reason
    """

    # identity
    id: str
    parent: DelegationParentRef
    delegation_key: str
    fingerprint: str

    # policy
    join_policy: DelegationJoinPolicy
    aggregation: DelegationAggregationPolicy
    deadline_at: str | None = None
    policy_snapshot_id: str = ""

    # budget
    budget_total_tokens: int = 0
    budget_reserved_tokens: int = 0
    budget_settled_tokens: int = 0

    # state
    status: DelegationStatus = DelegationStatus.PENDING
    version: int = 1
    first_run_id: str | None = None

    # timestamps
    created_at: str | None = None
    updated_at: str | None = None

    # cancel
    cancellation_reason: str | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def new(
        cls,
        *,
        parent: DelegationParentRef,
        delegation_key: str,
        fingerprint: str,
        join_policy: DelegationJoinPolicy | str,
        aggregation: DelegationAggregationPolicy | str,
        deadline_at: str | None,
        policy_snapshot_id: str,
        budget_total_tokens: int,
    ) -> Delegation:
        """Create a new Delegation in PENDING state with version 1."""
        if isinstance(join_policy, str):
            join_policy = DelegationJoinPolicy(join_policy)
        if isinstance(aggregation, str):
            aggregation = DelegationAggregationPolicy(aggregation)
        return cls(
            id=str(uuid4()),
            parent=parent,
            delegation_key=delegation_key,
            fingerprint=fingerprint,
            join_policy=join_policy,
            aggregation=aggregation,
            deadline_at=deadline_at,
            policy_snapshot_id=policy_snapshot_id,
            budget_total_tokens=budget_total_tokens,
            status=DelegationStatus.PENDING,
            version=1,
            first_run_id=None,
        )

    # ------------------------------------------------------------------
    # State transition contract
    # ------------------------------------------------------------------

    def transition(self, target: DelegationStatus) -> None:
        """Validate and apply a state transition in place.

        Raises ``DelegationStateError`` if the transition is not in the
        legal transition table. ``version`` is incremented atomically.
        """
        legal = DELEGATION_TRANSITION_TABLE.get(self.status, frozenset())
        if target not in legal:
            raise DelegationStateError(
                f"illegal delegation transition: "
                f"{self.status.value} -> {target.value}"
            )
        object.__setattr__(self, "status", target)
        object.__setattr__(self, "version", self.version + 1)

    def mark_cancelling(self) -> None:
        """Transition to CANCELLING from any non-terminal state.

        Convenience method; equivalent to
        ``self.transition(DelegationStatus.CANCELLING)``.
        """
        self.transition(DelegationStatus.CANCELLING)

    @property
    def is_terminal(self) -> bool:
        """True if the delegation is in a terminal state."""
        return self.status in DELEGATION_TERMINAL_STATES


@dataclass(frozen=True)
class DelegationMember:
    """A single child agent (worker or aggregator) within a delegation.

    Frozen dataclass with controlled in-place mutation via
    ``object.__setattr__`` in ``transition``.

    Field grouping:
      - identity: id, delegation_id, role, ordinal
      - spec: title, instruction, skills, allowed_tools, model_override,
        max_runtime_seconds
      - execution: execution_session_id, deadline_at, budget_tokens
      - state: status, version
      - retry: retry_count, retry_of
      - lease: claim_lock, claim_expires_at, last_heartbeat_at
      - cancel: cancel_reason
      - timestamps: started_at, ended_at
    """

    # identity
    id: str
    delegation_id: str
    role: DelegationMemberRole
    ordinal: int

    # spec
    title: str
    instruction: str
    skills: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    model_override: str | None = None
    max_runtime_seconds: int | None = None

    # execution
    execution_session_id: str = ""
    deadline_at: str | None = None
    budget_tokens: int = 0

    # state
    status: DelegationMemberStatus = DelegationMemberStatus.PENDING
    version: int = 1

    # retry metadata
    retry_count: int = 0
    retry_of: str | None = None

    # lease metadata
    claim_lock: str | None = None
    claim_expires_at: str | None = None
    last_heartbeat_at: str | None = None

    # cancel metadata
    cancel_reason: str | None = None
    cancel_requested_at: str | None = None

    # timestamps
    started_at: str | None = None
    ended_at: str | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def new(
        cls,
        *,
        delegation_id: str,
        role: DelegationMemberRole,
        ordinal: int,
        title: str,
        instruction: str,
        skills: tuple[str, ...] = (),
        allowed_tools: tuple[str, ...] = (),
        execution_session_id: str,
        deadline_at: str | None,
        budget_tokens: int,
        model_override: str | None = None,
        max_runtime_seconds: int | None = None,
    ) -> DelegationMember:
        """Create a new member in PENDING state with version 1."""
        return cls(
            id=str(uuid4()),
            delegation_id=delegation_id,
            role=role,
            ordinal=ordinal,
            title=title,
            instruction=instruction,
            skills=skills,
            allowed_tools=allowed_tools,
            model_override=model_override,
            max_runtime_seconds=max_runtime_seconds,
            execution_session_id=execution_session_id,
            deadline_at=deadline_at,
            budget_tokens=budget_tokens,
            status=DelegationMemberStatus.PENDING,
            version=1,
        )

    # ------------------------------------------------------------------
    # State transition contract
    # ------------------------------------------------------------------

    def transition(self, target: DelegationMemberStatus) -> None:
        """Validate and apply a state transition in place.

        Raises ``DelegationStateError`` if the transition is not in the
        legal transition table. ``version`` is incremented atomically.

        The RUNNING -> PENDING transition is reserved for stale-recovery
        (registry detects a dead worker and resets the member for
        re-claim); it is legal in the table but should only be used by
        the registry's recovery path. On stale-recovery, ``retry_count``
        is incremented and ``retry_of`` is set to the member's own ``id``
        (if not already set) so the retry chain is traceable.
        """
        legal = DELEGATION_MEMBER_TRANSITION_TABLE.get(self.status, frozenset())
        if target not in legal:
            raise DelegationStateError(
                f"illegal member transition: "
                f"{self.status.value} -> {target.value}"
            )
        # Stale-recovery: bump retry metadata before status/version.
        if (
            target is DelegationMemberStatus.PENDING
            and self.status is DelegationMemberStatus.RUNNING
        ):
            object.__setattr__(self, "retry_count", self.retry_count + 1)
            if self.retry_of is None:
                object.__setattr__(self, "retry_of", self.id)
        object.__setattr__(self, "status", target)
        object.__setattr__(self, "version", self.version + 1)

    @property
    def is_terminal(self) -> bool:
        """True if the member is in a terminal state."""
        return self.status in DELEGATION_MEMBER_TERMINAL_STATES


# ---------------------------------------------------------------------------
# Join-policy evaluation (pure function)
# ---------------------------------------------------------------------------


def evaluate_join_outcome(
    delegation_id: str,
    join_policy: DelegationJoinPolicy,
    aggregation: DelegationAggregationPolicy,
    member_results: tuple[DelegationResult, ...],
    aggregation_result: DelegationResult | None = None,
    *,
    partial: bool = False,
    partial_reason: str | None = None,
    filter_notes: tuple[str, ...] = (),
    total_usage: Mapping[str, int] | None = None,
) -> DelegationResultSet:
    """Evaluate the join policy against member results and produce a
    ``DelegationResultSet``.

    This is a pure function: given the member results and policy, it
    determines the delegation's terminal status and builds the result set.
    The caller (registry) is responsible for:

    - Determining WHEN to join (based on ``join_policy`` and member states).
    - Setting ``partial`` / ``partial_reason`` when join is forced
      (deadline, cancel, early failure).
    - Providing ``filter_notes`` for audit (e.g. late successes).

    Status determination order:

    1. ``partial_reason == "cancelled"`` -> ``CANCELLED`` (parent cancel
       overrides all outcomes).
    2. ``aggregation == AGENT`` and ``aggregation_result`` is provided ->
       aggregator outcome overrides worker outcomes (SUCCEEDED -> SUCCEEDED,
       else FAILED).
    3. ``join_policy`` in ``{ALL_COMPLETED, ALL_SUCCEEDED}`` -> SUCCEEDED
       only if NOT partial AND every member result is SUCCEEDED; otherwise
       FAILED.
    4. ``join_policy == BEST_EFFORT`` -> SUCCEEDED if at least one member
       result is SUCCEEDED; otherwise FAILED.
    """
    # 1. Parent cancel overrides everything.
    if partial_reason == "cancelled":
        status = DelegationStatus.CANCELLED
    # 2. Aggregator outcome overrides worker outcomes.
    elif (
        aggregation is DelegationAggregationPolicy.AGENT
        and aggregation_result is not None
    ):
        if aggregation_result.status is DelegationMemberStatus.SUCCEEDED:
            status = DelegationStatus.SUCCEEDED
        else:
            status = DelegationStatus.FAILED
    # 3. ALL_COMPLETED / ALL_SUCCEEDED.
    elif join_policy in (
        DelegationJoinPolicy.ALL_COMPLETED,
        DelegationJoinPolicy.ALL_SUCCEEDED,
    ):
        if partial:
            # Cannot confirm all members succeeded -- some did not finish.
            status = DelegationStatus.FAILED
        else:
            status = (
                DelegationStatus.SUCCEEDED
                if member_results
                and all(
                    r.status is DelegationMemberStatus.SUCCEEDED
                    for r in member_results
                )
                else DelegationStatus.FAILED
            )
    # 4. BEST_EFFORT.
    else:
        any_succeeded = any(
            r.status is DelegationMemberStatus.SUCCEEDED for r in member_results
        )
        status = (
            DelegationStatus.SUCCEEDED if any_succeeded else DelegationStatus.FAILED
        )

    return DelegationResultSet(
        delegation_id=delegation_id,
        status=status,
        member_results=member_results,
        aggregation_result=aggregation_result,
        partial=partial,
        partial_reason=partial_reason,
        filter_notes=filter_notes,
        total_usage=total_usage,
    )


# ---------------------------------------------------------------------------
# Port result types (CAS outcomes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimMemberResult:
    """Result of an atomic ``claim_member`` CAS.

    - SUCCESS: member was PENDING and is now RUNNING under ``claim_lock``.
    - IDEMPOTENT_REPLAY: member already claimed by the same ``claim_lock``.
    - CONFLICT: member already claimed by a different ``claim_lock``.
    - BUSY: member is not in a claimable state (not PENDING).

    ``member`` is the post-claim member snapshot (None on CONFLICT / BUSY).
    ``delegation`` is the delegation snapshot at claim time.
    """

    outcome: MutationOutcome
    member: DelegationMember | None = None
    delegation: Delegation | None = None


@dataclass(frozen=True)
class FinishMemberResult:
    """Result of an atomic ``finish_member`` CAS.

    - SUCCESS: member was RUNNING and is now terminal with the provided
      result.
    - IDEMPOTENT_REPLAY: member already finished with the same result
      (same checksum).
    - CONFLICT: version mismatch or claim held by a different lock.
    - BUSY: member is not in a finishable state (not RUNNING).

    ``result_set`` is non-None when this finish triggered the delegation's
    join evaluation (i.e. this was the last member to finish).
    """

    outcome: MutationOutcome
    member: DelegationMember | None = None
    delegation: Delegation | None = None
    result_set: DelegationResultSet | None = None


@dataclass(frozen=True)
class LedgerResult:
    """Result of a ledger reserve / settle / release CAS.

    - SUCCESS: ledger operation applied.
    - IDEMPOTENT_REPLAY: operation already applied (same reservation_id).
    - CONFLICT: insufficient balance (reserve) or reservation not found.
    - BUSY: reservation is in a non-actionable state.
    """

    outcome: MutationOutcome
    reservation_id: str | None = None
    balance: int | None = None


# ---------------------------------------------------------------------------
# Ports (async Protocols)
# ---------------------------------------------------------------------------


class Clock(Protocol):
    """Port for obtaining the current time as an ISO-8601 string."""

    def now_iso(self) -> str: ...


class DelegationRegistry(Protocol):
    """Async port for Delegation persistence with CAS mutations.

    Each mutation returns a typed result indicating success, idempotent
    replay, CAS conflict, or busy -- never a bare ``...``.
    """

    # --- lifecycle ---

    async def create_or_reconnect(
        self,
        request: DelegationCreateRequest,
    ) -> Delegation:
        """Create a new delegation (with members + snapshot + ledger) or
        reconnect to an existing one matched by ``delegation_key`` +
        ``fingerprint``. Returns the persisted (or existing) delegation.

        Raises ``DelegationConflictError`` if the key exists with a
        different fingerprint (non-idempotent replay).
        """
        ...

    async def get(self, delegation_id: str) -> Delegation | None: ...

    async def list_for_trusted_scope(
        self,
        scope_id: str,
        limit: int = 100,
    ) -> tuple[Delegation, ...]: ...

    # --- events ---

    async def append_event(
        self,
        delegation_id: str,
        kind: str,
        payload: Mapping[str, Any],
        member_ordinal: int | None = None,
    ) -> DelegationEvent: ...

    async def list_events(
        self,
        delegation_id: str,
        since: int = 0,
        limit: int = 100,
    ) -> tuple[DelegationEvent, ...]: ...

    # --- member CAS ---

    async def claim_member(
        self,
        delegation_id: str,
        member_ordinal: int,
        claim_lock: str,
        lease_seconds: int,
    ) -> ClaimMemberResult: ...

    async def finish_member(
        self,
        delegation_id: str,
        member_ordinal: int,
        claim_lock: str,
        result: DelegationResult,
        expected_version: int,
    ) -> FinishMemberResult: ...

    # --- ledger CAS ---

    async def reserve_ledger(
        self,
        delegation_id: str,
        amount: int,
        purpose: str,
    ) -> LedgerResult: ...

    async def settle_ledger(
        self,
        delegation_id: str,
        reservation_id: str,
        actual: int,
    ) -> LedgerResult: ...

    async def release_ledger(
        self,
        delegation_id: str,
        reservation_id: str,
    ) -> LedgerResult: ...

    # --- result set ---

    async def get_result_set(
        self,
        delegation_id: str,
    ) -> DelegationResultSet | None: ...


class DelegationDispatcher(Protocol):
    """Async port for process-internal child-agent management.

    Production implementation holds asyncio.Task handles keyed by a
    process-generated ``worker_token``; tests may substitute a fake.
    ``worker_token`` is opaque -- never a serialized asyncio.Task or
    memory address.
    """

    async def spawn(
        self,
        delegation: Delegation,
        member: DelegationMember,
    ) -> str:
        """Spawn a child agent for ``member``. Returns ``worker_token``."""
        ...

    async def cancel(self, worker_token: str) -> bool: ...

    async def inspect(self) -> Mapping[str, Any]: ...


# ---------------------------------------------------------------------------
# Create request (transactional create payload)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicySnapshotRecord:
    """Immutable projection of the parent/child/aggregator policy configs
    captured at delegation creation time.

    Stored in ``delegation_policy_snapshots``. Only execution-required
    whitelisted fields are projected -- never secrets or executable objects.
    """

    profile_version: str
    parent_config: Mapping[str, Any]
    child_config: Mapping[str, Any]
    aggregator_config: Mapping[str, Any] | None = None
    checksum: str = ""


@dataclass(frozen=True)
class DelegationCreateRequest:
    """Full transactional payload for ``create_or_reconnect``.

    Bundles the delegation aggregate, its members (with continuous worker
    ordinals + at most one aggregator), the immutable policy snapshot, and
    the total budget allocation. The registry persists all of these in a
    single ``BEGIN IMMEDIATE`` transaction.
    """

    parent: DelegationParentRef
    delegation_key: str
    fingerprint: str
    join_policy: DelegationJoinPolicy | str
    aggregation: DelegationAggregationPolicy | str
    deadline_at: str | None
    budget_total_tokens: int
    members: tuple[DelegationMember, ...]
    snapshot: PolicySnapshotRecord


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DelegationStateError(Exception):
    """Raised when a state transition violates the contract table."""


class DelegationConflictError(Exception):
    """Raised when a delegation_key already exists with a different
    fingerprint (non-idempotent replay)."""
