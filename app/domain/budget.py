"""Budget domain types.

Pure domain value objects for budget reservation, settle, and release.
No IO, no asyncio, no pydantic, no infrastructure.

The Budget domain governs single-run hard limits:
- LLM call count, tool call count, wall-time
- Token cost, USD cost (nullable: None = no rejection, still tracked)
- Sandbox cumulative seconds / cpu-seconds / memory-MB-seconds / callback calls (nullable)

The Policy produces decisions; the Application Service applies them to a
mutable per-run account. BudgetState is an immutable snapshot of that account
at evaluation time.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.domain.policy import PolicyOutcome


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------


class BudgetReserveKind(str, Enum):
    """The kind of external call being reserved."""

    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    SANDBOX_RESOURCE = "sandbox_resource"
    WALL_TIME = "wall_time"


# ---------------------------------------------------------------------------
# Config (Domain mirror of application-level BudgetPolicyConfig)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetConfig:
    """Domain-level configuration for the budget policy.

    Mirrors the application-level ``BudgetPolicyConfig`` but lives in Domain
    so the Policy never imports Application. Nullable fields (None) mean the
    reserve always ALLOWs for that dimension, but the reserve/settle/account
    chain still runs (the reserved/consumed count is tracked).
    """

    max_wall_seconds: int = 900
    max_llm_calls: int = 10
    max_tool_calls: int = 100
    max_token_cost: int | None = None
    max_usd_cost: Decimal | None = None
    max_sandbox_seconds: float | None = None
    max_sandbox_cpu_seconds: float | None = None
    max_sandbox_memory_mb_seconds: float | None = None
    max_sandbox_callback_calls: int | None = None


# ---------------------------------------------------------------------------
# State (immutable snapshot of the account)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetState:
    """Immutable snapshot of the budget account at evaluation time.

    The Policy reads this snapshot; the Service applies the decision to the
    mutable account. All counters are cumulative (reserved + settled).
    """

    llm_calls_reserved: int
    tool_calls_reserved: int
    elapsed_seconds: float
    token_cost_reserved: int
    usd_cost_reserved: Decimal
    sandbox_seconds_reserved: float
    sandbox_cpu_seconds_reserved: float
    sandbox_memory_mb_seconds_reserved: float
    sandbox_callback_calls_reserved: int


# ---------------------------------------------------------------------------
# Sandbox types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxReserveSpec:
    """Per-call sandbox resource maximums requested in a reserve.

    Carried in the typed reserve request. If the Policy ALLOWs, these values
    become the ``SandboxBudgetAllocation``.
    """

    max_seconds: float
    max_cpu_seconds: float
    max_memory_mb_seconds: float
    max_callback_calls: int


@dataclass(frozen=True)
class SandboxBudgetAllocation:
    """Granted per-call resource upper bounds for a sandbox execution.

    Composed by the Policy during sandbox reserve (equals the request spec
    when ALLOWed). Consumed later by SandboxPolicy (T10) as the per-call cap.
    Settle uses ACTUAL duration/callbacks, not these upper bounds.
    """

    max_seconds: float
    max_cpu_seconds: float
    max_memory_mb_seconds: float
    max_callback_calls: int


# ---------------------------------------------------------------------------
# Request / Decision types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetReserveRequest:
    """Typed request to reserve budget before an external call.

    Fields are populated based on ``kind``:
    - LLM_CALL: estimated_tokens, estimated_usd_cost
    - TOOL_CALL: (no extra fields)
    - WALL_TIME: estimated_duration_seconds
    - SANDBOX_RESOURCE: sandbox_spec
    """

    kind: BudgetReserveKind
    estimated_tokens: int = 0
    estimated_usd_cost: Decimal = Decimal("0")
    estimated_duration_seconds: float = 0.0
    sandbox_spec: SandboxReserveSpec | None = None


@dataclass(frozen=True)
class BudgetReservationDecision:
    """Decision from a budget reserve request.

    If ``outcome`` is ALLOW, the Service applies the reservation (increments
    counters). The decision carries what was reserved so that ``settle`` and
    ``release`` can reference it. ``reservation_id`` is set by the Service
    (not the Policy) to track individual reservations.
    """

    outcome: PolicyOutcome
    reason: str
    kind: BudgetReserveKind
    estimated_tokens: int = 0
    estimated_usd_cost: Decimal = Decimal("0")
    estimated_duration_seconds: float = 0.0
    sandbox_allocation: SandboxBudgetAllocation | None = None
    reservation_id: str = ""
    remaining_llm_calls: int | None = None
    remaining_tool_calls: int | None = None
    remaining_token_cost: int | None = None
    remaining_usd_cost: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("budget reservation decision reason must not be empty")


@dataclass(frozen=True)
class BudgetActualUsage:
    """Actual usage reported at settle time.

    ``None`` fields mean "unknown" -- the reservation's estimate is kept
    (conservative settle). Only when a field is explicitly provided does it
    replace the estimate.
    """

    token_cost: int | None = None
    usd_cost: Decimal | None = None
    duration_seconds: float | None = None
    sandbox_callback_count: int | None = None


@dataclass(frozen=True)
class BudgetSettleDecision:
    """Decision from a budget settle request.

    Carries the final consumed amounts after settle. Conservative: if actual
    is None, the reservation's estimate is kept.
    """

    settled: bool
    reason: str
    reservation_id: str = ""
    consumed_tokens: int = 0
    consumed_usd_cost: Decimal = Decimal("0")
    consumed_duration_seconds: float = 0.0
    consumed_sandbox_callbacks: int = 0


@dataclass(frozen=True)
class BudgetReleaseDecision:
    """Decision from a budget release request."""

    released: bool
    reason: str
    reservation_id: str = ""
