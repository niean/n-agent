"""DelegateAgentsToolExecutor + delegate_agents tool definition (T11).

The ``delegate_agents`` tool is the single entry point through which a
parent Agent delegates parallel work to depth-1 child Agents. The tool
executor:

  1. Re-verifies the delegation capability from
     ``context.trusted_metadata["delegation_capability"]`` (server-injected,
     never from untrusted ``context.metadata``). No capability ->
     ``delegation_not_authorized``; DelegationService is never called.
  2. Translates the tool-call arguments into ``DelegationChildSpec`` list +
     policy fields and delegates to ``DelegationService.delegate``.
  3. Returns a bounded, information-flow-filtered ``ToolResult`` containing
     only the parent-safe projection (delegation_id, status, partial,
     member summaries, aggregation result, usage). Internal session ids,
     lease/claim tokens, policy JSON, and unfiltered errors are never
     returned.

Error codes are split into non-retriable (not_authorized, invalid,
budget_exceeded, conflict, create_failed, no_result) and retriable
(timeout, missing-during-execution) so the caller can decide whether to
retry.
"""
from __future__ import annotations

import time
from typing import Any, Mapping

from app.application.delegation_request_parser import DelegationError
from app.domain.delegation import (
    DelegationChildSpec,
    DelegationResultSet,
    DelegationStatus,
)
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolExecutionContext,
    ToolDefinition,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


# ---------------------------------------------------------------------------
# Retriability classification
# ---------------------------------------------------------------------------

#: Non-retriable error codes -- the request itself is invalid or forbidden;
#: retrying with the same arguments will fail the same way.
_NON_RETRIABLE_CODES: frozenset[str] = frozenset(
    {
        "delegation_not_authorized",
        "delegation_disabled",
        "delegation_invalid",
        "delegation_budget_exceeded",
        "delegation_conflict",
        "delegation_create_failed",
        "delegation_no_result",
    }
)


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------


def delegate_agent_tool_definitions() -> list[ToolDefinition]:
    """Return the single ``delegate_agents`` tool definition.

    Closed schema (additionalProperties=False at every level) with five
    required top-level fields and an optional ``aggregator`` constrained by
    if/then: when ``aggregation=agent`` the aggregator is required,
    otherwise it is forbidden. Array/string/byte/depth caps mirror the
    DelegationPolicy limits. The raw-JSON parser (DelegationRequestParser)
    remains responsible for rejecting duplicate keys -- JSON Schema cannot
    detect duplicate keys and this module never claims it can.
    """
    child_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1, "maxLength": 200},
            "instruction": {"type": "string", "minLength": 1, "maxLength": 8000},
            "skills": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            "allowed_tools": {"type": "array", "items": {"type": "string"}},
            "model_override": {"type": ["string", "null"]},
            "max_runtime_seconds": {"type": ["integer", "null"], "minimum": 1},
            "budget_tokens": {"type": "integer", "minimum": 0},
        },
        "required": ["title", "instruction"],
        "additionalProperties": False,
    }
    aggregator_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "minLength": 1, "maxLength": 8000},
            "allowed_tools": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["instruction"],
        "additionalProperties": False,
    }
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "delegation_key": {"type": "string", "minLength": 1, "maxLength": 200},
            "children": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": child_schema,
            },
            "join_policy": {
                "type": "string",
                "enum": ["all_completed", "all_succeeded", "best_effort"],
            },
            "aggregation": {"type": "string", "enum": ["parent", "agent"]},
            "timeout_seconds": {"type": "integer", "minimum": 1},
            "aggregator": aggregator_schema,
        },
        "required": [
            "delegation_key",
            "children",
            "join_policy",
            "aggregation",
            "timeout_seconds",
        ],
        "additionalProperties": False,
        "allOf": [
            {
                "if": {
                    "properties": {"aggregation": {"const": "agent"}},
                    "required": ["aggregation"],
                },
                "then": {"required": ["aggregator"]},
                "else": {"not": {"required": ["aggregator"]}},
            }
        ],
    }
    return [
        ToolDefinition(
            name="delegate_agents",
            description=(
                "Delegate parallel work to isolated depth-1 child agents and "
                "return their aggregated, information-flow-filtered results. "
                "Children cannot delegate, manage tasks, or approve work."
            ),
            input_schema=schema,
            risk_level=RiskLevel.SAFE,
            timeout_seconds=300,
            source_type=ToolSourceType.AGENT,
            toolset="agent",
            managed=False,
            realtime_only=False,
        )
    ]


# ---------------------------------------------------------------------------
# Tool executor
# ---------------------------------------------------------------------------


class DelegateAgentsToolExecutor(ToolExecutor):
    """Executes the ``delegate_agents`` tool.

    Wraps ``DelegationService`` and adapts the tool-call surface to the
    service's ``parent_capability`` dict interface. The capability is
    re-verified here (defense in depth) even though the parent adapter
    already signed it -- the schema is never trusted to enforce capability.
    """

    def __init__(self, delegation_service: Any) -> None:
        self._service = delegation_service

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        start = time.monotonic()
        try:
            payload = await self._dispatch(request, context)
            status = ToolResultStatus.SUCCESS
        except _ToolDelegationError as exc:
            payload = {
                "error": exc.code,
                "retriable": exc.code not in _NON_RETRIABLE_CODES,
            }
            if exc.message:
                payload["message"] = exc.message
            status = ToolResultStatus.ERROR
        return ToolResult(
            tool_call_id=request.id,
            tool_name=request.name,
            status=status,
            content=payload,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None,
    ) -> dict[str, Any]:
        capability = self._extract_capability(context)
        children = self._parse_children(request.arguments.get("children"))
        aggregator_instruction = self._parse_aggregator(
            request.arguments.get("aggregator"),
            request.arguments.get("aggregation"),
        )
        join_policy = request.arguments.get("join_policy", "all_completed")
        aggregation = request.arguments.get("aggregation", "parent")
        timeout_seconds = self._coerce_timeout(
            request.arguments.get("timeout_seconds")
        )
        raw_key = request.arguments.get("delegation_key")
        if not isinstance(raw_key, str) or not raw_key.strip():
            # OpenAI-compatible providers do not enforce the tool schema's
            # required fields; a missing key must not fall through as a
            # guaranteed-invalid empty sentinel. Derive a deterministic key
            # from the normalized request so identical logical requests keep
            # the reconnect/idempotency semantics.
            raw_key = self._derive_delegation_key(
                children, join_policy, aggregation, timeout_seconds,
                aggregator_instruction,
            )
        try:
            result_set = await self._service.delegate(
                parent_capability=capability,
                delegation_key=raw_key,
                children=children,
                join_policy=join_policy,
                aggregation=aggregation,
                timeout_seconds=timeout_seconds,
                aggregator_instruction=aggregator_instruction,
            )
        except DelegationError as exc:
            raise _ToolDelegationError(exc.code, exc.message) from exc
        return self._project_result(result_set)

    # ------------------------------------------------------------------
    # capability extraction (server-injected only)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_capability(
        context: ToolExecutionContext | None,
    ) -> dict[str, Any]:
        if context is None:
            raise _ToolDelegationError("delegation_not_authorized")
        trusted = context.trusted_metadata or {}
        capability = trusted.get("delegation_capability")
        if not isinstance(capability, Mapping) or not capability:
            raise _ToolDelegationError("delegation_not_authorized")
        if not capability.get("has_capability", False):
            raise _ToolDelegationError("delegation_not_authorized")
        # Merge context-level run/session ids (authoritative) over the
        # capability dict so a forged capability cannot override them.
        merged: dict[str, Any] = dict(capability)
        if context.run_id:
            merged["run_id"] = context.run_id
        if context.session_id:
            merged["session_id"] = context.session_id
        return merged

    # ------------------------------------------------------------------
    # argument parsing
    # ------------------------------------------------------------------

    #: Fallback timeout when the model omits timeout_seconds. Mirrors the
    #: delegate_agents tool definition's own timeout_seconds.
    _DEFAULT_TIMEOUT_SECONDS: int = 300

    @staticmethod
    def _coerce_timeout(raw: Any) -> int:
        """Coerce the model-supplied timeout into a positive int.

        Missing, blank, or non-numeric values fall back to the default --
        never the guaranteed-invalid 0 sentinel (DelegationPolicy requires a
        positive timeout).
        """
        if isinstance(raw, bool) or raw is None:
            return DelegateAgentsToolExecutor._DEFAULT_TIMEOUT_SECONDS
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DelegateAgentsToolExecutor._DEFAULT_TIMEOUT_SECONDS
        if value <= 0:
            return DelegateAgentsToolExecutor._DEFAULT_TIMEOUT_SECONDS
        return value

    @staticmethod
    def _derive_delegation_key(
        children: list[DelegationChildSpec],
        join_policy: Any,
        aggregation: Any,
        timeout_seconds: int,
        aggregator_instruction: str | None,
    ) -> str:
        """Derive a deterministic delegation_key from the request itself.

        Same logical request (children/policy/timeout/aggregator) -> same
        key, so fingerprint-based reconnect still works when the model
        omits the explicit key.
        """
        import hashlib
        import json

        payload = {
            "c": [
                {
                    "t": c.title,
                    "i": c.instruction,
                    "s": sorted(c.skills),
                    "a": sorted(c.allowed_tools),
                }
                for c in children
            ],
            "j": str(join_policy),
            "g": str(aggregation),
            "t": timeout_seconds,
            "ai": aggregator_instruction,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return "auto-" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _parse_children(raw: Any) -> list[DelegationChildSpec]:
        if not isinstance(raw, list) or not raw:
            raise _ToolDelegationError("delegation_invalid")
        children: list[DelegationChildSpec] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise _ToolDelegationError("delegation_invalid")
            children.append(
                DelegationChildSpec(
                    title=str(item.get("title", "")),
                    instruction=str(item.get("instruction", "")),
                    skills=tuple(item.get("skills", ()) or ()),
                    allowed_tools=tuple(item.get("allowed_tools", ()) or ()),
                    model_override=item.get("model_override"),
                    max_runtime_seconds=item.get("max_runtime_seconds"),
                    budget_tokens=int(item.get("budget_tokens", 0)),
                    output_schema=item.get("output_schema"),
                )
            )
        return children

    @staticmethod
    def _parse_aggregator(raw: Any, aggregation: Any) -> str | None:
        if aggregation != "agent":
            return None
        if not isinstance(raw, Mapping):
            raise _ToolDelegationError("delegation_invalid")
        instruction = str(raw.get("instruction", "")).strip()
        if not instruction:
            raise _ToolDelegationError("delegation_invalid")
        return instruction

    # ------------------------------------------------------------------
    # result projection (parent-safe, bounded)
    # ------------------------------------------------------------------

    @staticmethod
    def _project_result(result_set: DelegationResultSet) -> dict[str, Any]:
        member_projection: list[dict[str, Any]] = []
        for r in result_set.member_results:
            member_projection.append(
                {
                    "status": r.status.value,
                    "summary": r.summary,
                    "error_code": r.error_code,
                }
            )
        payload: dict[str, Any] = {
            "delegation_id": result_set.delegation_id,
            "status": result_set.status.value,
            "partial": result_set.partial,
            "member_results": member_projection,
        }
        if result_set.partial_reason is not None:
            payload["partial_reason"] = result_set.partial_reason
        if result_set.aggregation_result is not None:
            payload["aggregation_result"] = {
                "status": result_set.aggregation_result.status.value,
                "summary": result_set.aggregation_result.summary,
            }
        if result_set.total_usage:
            payload["usage_summary"] = dict(result_set.total_usage)
        return payload


# ---------------------------------------------------------------------------
# Internal error wrapper
# ---------------------------------------------------------------------------


class _ToolDelegationError(Exception):
    """Internal error carrying a stable delegation error code + message."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message
        super().__init__(message or code)
