from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeVar


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("policy decision reason must not be empty")


RequestT = TypeVar("RequestT", contravariant=True)
ContextT = TypeVar("ContextT", contravariant=True)


class Policy(Protocol[RequestT, ContextT]):
    def evaluate(
        self,
        request: RequestT,
        context: ContextT | None = None,
    ) -> PolicyDecision: ...

