from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol, TypeVar


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyDecisionKind(str, Enum):
    ADMISSION = "admission"
    PLAN = "plan"
    SELECTION = "selection"
    ALLOCATION = "allocation"


class ExecutionMode(str, Enum):
    REALTIME = "realtime"
    UNATTENDED = "unattended"
    DELEGATED = "delegated"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str
    policy: str = "unknown"
    version: str = "unknown"

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("policy decision reason must not be empty")


@dataclass(frozen=True)
class RunPolicyContext:
    run_id: str
    session_id: str
    source: str
    actor_id: str | None
    execution_mode: ExecutionMode
    trusted_claims: Mapping[str, Any]
    policy_scope: str = "system"

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "trusted_claims", MappingProxyType(dict(self.trusted_claims))
        )


@dataclass(frozen=True)
class PolicyAuditEvent:
    policy: str
    version: str
    decision_kind: PolicyDecisionKind
    reason: str
    run_id: str
    session_id: str
    policy_scope: str = "system"
    outcome: PolicyOutcome | None = None
    occurred_at: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("policy audit event reason must not be empty")


RequestT = TypeVar("RequestT", contravariant=True)
ContextT = TypeVar("ContextT", contravariant=True)
DecisionT = TypeVar("DecisionT", covariant=True)


class Policy(Protocol[RequestT, ContextT, DecisionT]):
    def evaluate(
        self,
        request: RequestT,
        context: ContextT | None = None,
    ) -> DecisionT: ...


class PolicyAuditSink(Protocol):
    async def record(self, event: PolicyAuditEvent) -> None: ...
