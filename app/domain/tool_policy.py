from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from app.domain.policy import PolicyDecision, PolicyOutcome
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolSourceType,
)


class ToolExposurePolicy(str, Enum):
    DEFAULT = "default"
    SAFE_ONLY = "safe_only"


@dataclass(frozen=True)
class ToolPolicyRequest:
    definition: ToolDefinition
    request: ToolCallRequest


class ToolPolicy:
    def can_expose(
        self,
        definition: ToolDefinition,
        exposure_policy: ToolExposurePolicy,
    ) -> bool:
        if not isinstance(exposure_policy, ToolExposurePolicy):
            return False
        if not definition.enabled or definition.risk_level is RiskLevel.DANGEROUS:
            return False
        if exposure_policy is ToolExposurePolicy.DEFAULT:
            return True
        return (
            definition.risk_level is RiskLevel.SAFE
            and definition.source_type is not ToolSourceType.AGENT
        )

    def validate_definition(self, definition: ToolDefinition) -> None:
        if definition.managed and definition.risk_level is not RiskLevel.CONFIRM:
            raise ValueError("managed tools must use confirm risk level")

    def evaluate(
        self,
        request: ToolPolicyRequest,
        context: ToolExecutionContext | None = None,
    ) -> PolicyDecision:
        return self.evaluate_execution(request.definition, request.request, context)

    def evaluate_execution(
        self,
        definition: ToolDefinition,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> PolicyDecision:
        if not definition.enabled:
            return PolicyDecision(PolicyOutcome.DENY, "tool_disabled")
        if definition.risk_level is RiskLevel.DANGEROUS:
            return PolicyDecision(PolicyOutcome.DENY, "dangerous_tool")
        if definition.risk_level is RiskLevel.SAFE:
            return PolicyDecision(PolicyOutcome.ALLOW, "safe_tool")

        if definition.managed:
            if context is not None and request.name in context.permitted_managed_tools:
                return PolicyDecision(PolicyOutcome.ALLOW, "managed_grant")
            return PolicyDecision(
                PolicyOutcome.REQUIRE_APPROVAL,
                "managed_approval_required",
            )

        grant: Any = None
        if context is not None:
            grant = context.allowed_confirm_tools.get(request.name)
        if grant == "session":
            return PolicyDecision(PolicyOutcome.ALLOW, "session_grant")
        if self._argument_grant_matches(grant, request.arguments):
            return PolicyDecision(PolicyOutcome.ALLOW, "argument_grant")
        return PolicyDecision(
            PolicyOutcome.REQUIRE_APPROVAL,
            "confirm_approval_required",
        )

    def authorize_once(
        self,
        definition: ToolDefinition,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecutionContext:
        if context is not None and not isinstance(context, ToolExecutionContext):
            raise ValueError("context must be a ToolExecutionContext")
        if definition.name != request.name:
            raise ValueError("definition and request names must match")
        if definition.risk_level is not RiskLevel.CONFIRM:
            raise ValueError("only confirm tools can receive one-time authorization")

        current_context = context if context is not None else ToolExecutionContext()
        decision = self.evaluate_execution(definition, request, current_context)
        if decision.outcome is not PolicyOutcome.REQUIRE_APPROVAL:
            raise ValueError("tool call does not currently require approval")

        allowed_confirm_tools = self._copy_valid_confirm_grants(
            current_context.allowed_confirm_tools
        )
        permitted_managed_tools = set(current_context.permitted_managed_tools)
        if definition.managed:
            permitted_managed_tools.add(request.name)
        else:
            allowed_confirm_tools[request.name] = dict(request.arguments)

        return replace(
            current_context,
            allowed_confirm_tools=allowed_confirm_tools,
            permitted_managed_tools=permitted_managed_tools,
        )

    @staticmethod
    def _argument_grant_matches(grant: Any, arguments: Any) -> bool:
        if not isinstance(grant, dict) or not isinstance(arguments, dict):
            return False
        if not all(isinstance(key, str) for key in grant):
            return False
        if not all(isinstance(key, str) for key in arguments):
            return False
        if not grant:
            return not arguments
        return all(key in arguments and arguments[key] == value for key, value in grant.items())

    @staticmethod
    def _copy_valid_confirm_grants(grants: Any) -> dict[str, Any]:
        if not isinstance(grants, dict):
            return {}

        copied: dict[str, Any] = {}
        for tool_name, grant in grants.items():
            if not isinstance(tool_name, str) or not tool_name:
                continue
            if grant == "session":
                copied[tool_name] = "session"
            elif isinstance(grant, dict) and all(
                isinstance(key, str) for key in grant
            ):
                copied[tool_name] = dict(grant)
        return copied
