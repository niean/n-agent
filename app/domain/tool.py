from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Protocol, Union


class RiskLevel(str, Enum):
    SAFE = "safe"
    CONFIRM = "confirm"
    DANGEROUS = "dangerous"


class ToolSourceType(str, Enum):
    BUILTIN = "builtin"
    KNOWLEDGE = "knowledge"
    SKILL = "skill"
    MCP = "mcp"
    PLUGIN = "plugin"
    AGENT = "agent"


class ToolResultStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    PERMISSION_DENIED = "permission_denied"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    risk_level: RiskLevel = RiskLevel.SAFE
    permissions: tuple[str, ...] = ()
    timeout_seconds: int = 10
    enabled: bool = True
    source_type: ToolSourceType = ToolSourceType.BUILTIN
    toolset: str = "builtin"
    managed: bool = False


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovalRequest:
    session_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    description: str
    risk_level: RiskLevel
    # Optional metadata carried alongside the approval request. NOT part of
    # the 5-field approval card envelope whitelist (confirmation_id /
    # tool_name / description / arguments_summary / expires_at); used only
    # internally by claim path and AgentGraph to route host-grant approvals.
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class ApprovalDecision:
    allowed: bool
    scope: str = "once"  # "once" | "session" | "deny"
    reason: str = ""


ApprovalDecider = Callable[[ApprovalRequest], Union[ApprovalDecision, Awaitable[ApprovalDecision]]]

ConfirmToolGrant = dict[str, Any] | Literal["session"]


@dataclass(frozen=True)
class ToolExecutionContext:
    allowed_confirm_tools: dict[str, ConfirmToolGrant] = field(default_factory=dict)
    session_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    trusted_metadata: dict[str, Any] = field(default_factory=dict)
    execution_context_mode: str = "realtime"
    permitted_managed_tools: set[str] = field(default_factory=set)
    enabled_override: list[str] | None = None
    approval_decider: ApprovalDecider | None = None
    # Tools explicitly granted for exposure in safe_only/unattended mode. A
    # task may grant specific SAFE tools (e.g. host_terminal) so the unattended
    # run can see them despite their AGENT source_type; grants never override
    # DANGEROUS or CONFIRM risk gating.
    granted_tools: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    tool_name: str
    status: ToolResultStatus
    content: Any
    duration_ms: int = 0
    # A successful terminal intent (for example task_complete/task_fail) ends
    # the current Agent turn after its tool result has been persisted. This is
    # executor-controlled flow metadata, not model-provided content.
    terminal: bool = False


class ToolExecutor(Protocol):
    async def execute(self, request: ToolCallRequest, context: ToolExecutionContext | None = None) -> ToolResult:
        ...
