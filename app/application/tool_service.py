from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable
from weakref import WeakKeyDictionary

from app.domain.policy import PolicyAuditEvent, PolicyDecisionKind, PolicyDecision, PolicyOutcome
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolDefinition,
    ToolExecutionContext,
    ToolExecutor,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)
from app.domain.tool_policy import ToolExposurePolicy, ToolPolicy
from app.application.host_terminal_capability import (
    is_photo_capability_request,
    is_photo_capability_result,
)

if TYPE_CHECKING:
    from app.application.budget_service import BudgetService
    from app.application.information_flow_service import InformationFlowService
    from app.application.policy_audit_service import PolicyAuditService

logger = logging.getLogger(__name__)


class ToolNotFoundError(ValueError):
    """Raised when an execution policy request names an unknown tool."""


class ToolDefinitionChangedError(ValueError):
    """Raised when an evaluated tool no longer resolves to the same definition."""


class _EvaluationToken:
    pass


@dataclass(frozen=True)
class _EvaluatedExecution:
    definition: ToolDefinition
    request: ToolCallRequest


@dataclass(frozen=True)
class ToolApprovalSnapshot:
    name: str
    description: str
    risk_level: RiskLevel


@dataclass(frozen=True)
class ToolExecutionEvaluation:
    decision: PolicyDecision
    approval: ToolApprovalSnapshot
    _token: _EvaluationToken = field(repr=False, compare=False)


class ToolService:
    def __init__(
        self,
        executor: ToolExecutor,
        definitions: list[ToolDefinition],
        policy: ToolPolicy | None = None,
        *,
        budget_service: "BudgetService | None" = None,
        information_flow_service: "InformationFlowService | None" = None,
        audit_service: "PolicyAuditService | None" = None,
    ):
        self.policy = policy if policy is not None else ToolPolicy()
        for definition in definitions:
            self.policy.validate_definition(definition)
        self.executor = executor
        self.definitions = {definition.name: definition for definition in definitions}
        self.dynamic_definitions: dict[str, dict[str, ToolDefinition]] = {}
        self._evaluated_executions: WeakKeyDictionary[
            _EvaluationToken,
            _EvaluatedExecution,
        ] = WeakKeyDictionary()
        self._budget_service = budget_service
        self._information_flow_service = information_flow_service
        self._audit_service = audit_service

    def set_dynamic_definitions(self, source_key: str, definitions: list[ToolDefinition]) -> None:
        schema_valid: list[ToolDefinition] = []
        for definition in definitions:
            if not isinstance(definition.input_schema, dict) or definition.input_schema.get("type") != "object":
                continue
            schema_valid.append(definition)

        deduplicated: dict[str, ToolDefinition] = {}
        for definition in schema_valid:
            if definition.name not in deduplicated:
                deduplicated[definition.name] = definition

        for definition in deduplicated.values():
            self.policy.validate_definition(definition)

        static_names = set(self.definitions)
        dynamic = {
            name: definition
            for name, definition in deduplicated.items()
            if name not in static_names
        }
        self.dynamic_definitions[source_key] = dynamic

    def list_definitions(self) -> list[ToolDefinition]:
        definitions = list(self.definitions.values())
        for dynamic in self.dynamic_definitions.values():
            definitions.extend(dynamic.values())
        return definitions

    def get_definition(self, name: str) -> ToolDefinition | None:
        if name in self.definitions:
            return self.definitions[name]
        for dynamic in self.dynamic_definitions.values():
            if name in dynamic:
                return dynamic[name]
        return None

    def _definition(self, name: str) -> ToolDefinition | None:
        return self.get_definition(name)

    def list_openai_tools(
        self,
        risk_level: RiskLevel | ToolExposurePolicy | None = None,
        context: ToolExecutionContext | None = None,
    ) -> list[dict]:
        if risk_level is None:
            exposure_policy = ToolExposurePolicy.DEFAULT
        elif risk_level is RiskLevel.SAFE:
            exposure_policy = ToolExposurePolicy.SAFE_ONLY
        elif isinstance(risk_level, ToolExposurePolicy):
            exposure_policy = risk_level
        else:
            exposure_policy = None

        return [
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": definition.input_schema,
                },
            }
            for definition in self.list_definitions()
            if (
                self.policy.can_expose(
                    definition,
                    exposure_policy,
                    getattr(context, "granted_tools", frozenset()) if context is not None else frozenset(),
                )
                if exposure_policy is not None
                else definition.enabled and definition.risk_level is risk_level
            )
            and isinstance(definition.input_schema, dict)
            and definition.input_schema.get("type") == "object"
        ]

    def evaluate_execution(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
    ) -> ToolExecutionEvaluation:
        definition = self._definition(request.name)
        if definition is None:
            raise ToolNotFoundError(f"tool not found: {request.name}")
        decision = self.policy.evaluate_execution(definition, request, context)
        token = _EvaluationToken()
        evaluation = ToolExecutionEvaluation(
            decision=decision,
            approval=ToolApprovalSnapshot(
                name=definition.name,
                description=definition.description,
                risk_level=definition.risk_level,
            ),
            _token=token,
        )
        self._evaluated_executions[token] = _EvaluatedExecution(
            definition=definition,
            request=request,
        )
        return evaluation

    def authorize_once(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
        *,
        evaluation: ToolExecutionEvaluation | None = None,
    ) -> ToolExecutionContext:
        definition = (
            self._definition_for_evaluation(request, evaluation)
            if evaluation is not None
            else self._required_definition(request.name)
        )
        return self.policy.authorize_once(definition, request, context)

    async def execute(
        self,
        request: ToolCallRequest,
        context: ToolExecutionContext | None = None,
        *,
        evaluation: ToolExecutionEvaluation | None = None,
    ) -> ToolResult:
        if evaluation is not None:
            try:
                definition = self._definition_for_evaluation(request, evaluation)
            except ToolDefinitionChangedError:
                return ToolResult(
                    request.id,
                    request.name,
                    ToolResultStatus.PERMISSION_DENIED,
                    {
                        "error": "permission_denied",
                        "reason": "tool_definition_changed",
                    },
                )
        else:
            definition = self._definition(request.name)
            if definition is None:
                return ToolResult(
                    request.id,
                    request.name,
                    ToolResultStatus.ERROR,
                    {"error": "tool not found"},
                )
        decision = self.policy.evaluate_execution(definition, request, context)
        if decision.outcome is not PolicyOutcome.ALLOW:
            return ToolResult(
                request.id,
                request.name,
                ToolResultStatus.PERMISSION_DENIED,
                {"error": "permission_denied", "reason": decision.reason},
            )

        # --- Tool Budget enforcement (T12 S3) ---
        # Reserve a tool call before any executor. If denied, executor is NOT called.
        # New runtime paths always provide an explicit per-run id.  Keep the
        # session fallback for older direct callers while they migrate.
        run_id = (
            (context.run_id or context.session_id)
            if context is not None
            else ""
        ) or ""
        reservation = None
        if self._budget_service is not None and run_id:
            from app.domain.budget import BudgetReserveKind, BudgetReserveRequest
            reservation = await self._budget_service.reserve(
                run_id,
                BudgetReserveRequest(kind=BudgetReserveKind.TOOL_CALL),
            )
            await self._audit_budget(
                reservation.outcome, reservation.reason, run_id,
            )
            if reservation.outcome is PolicyOutcome.DENY:
                return ToolResult(
                    request.id,
                    request.name,
                    ToolResultStatus.PERMISSION_DENIED,
                    {"error": "permission_denied", "reason": "budget_exhausted"},
                )

        # --- InformationFlow input wrapping (T12 S3) ---
        # Release tool call (name + arguments) to TOOL_MCP_PLUGIN target.
        # If denied, release budget and do NOT call executor.
        working_request = request
        if self._information_flow_service is not None:
            from app.domain.information_flow import (
                Classification,
                ReleaseTarget,
            )
            input_payload = json.dumps(
                {"name": request.name, "arguments": request.arguments},
                default=str, ensure_ascii=False,
            )
            input_release = self._information_flow_service.release(
                input_payload,
                ReleaseTarget.TOOL_MCP_PLUGIN,
                classification=Classification.INTERNAL,
                origin="tool_service",
            )
            if not input_release.allowed:
                # Release budget reservation before returning
                if reservation is not None and self._budget_service is not None:
                    await self._budget_service.release(run_id, reservation)
                return ToolResult(
                    request.id,
                    request.name,
                    ToolResultStatus.PERMISSION_DENIED,
                    {"error": "permission_denied", "reason": "information_flow_denied"},
                )
            # If redaction was applied, use sanitized arguments
            if input_release.content is not None and input_release.content != input_payload:
                try:
                    sanitized = json.loads(input_release.content)
                    if isinstance(sanitized, dict) and "arguments" in sanitized:
                        working_request = ToolCallRequest(
                            id=request.id,
                            name=request.name,
                            arguments=sanitized["arguments"] if isinstance(sanitized["arguments"], dict) else request.arguments,
                        )
                except (json.JSONDecodeError, TypeError):
                    pass  # Keep original request if JSON round-trip fails

        # --- Execute tool ---
        try:
            try:
                result = await self.executor.execute(working_request, context)
            except TypeError:
                result = await self.executor.execute(working_request)
        except asyncio.CancelledError:
            # Release budget on cancel (CancelledError is BaseException, not Exception)
            if reservation is not None and self._budget_service is not None:
                await self._budget_service.release(run_id, reservation)
            raise
        except Exception:
            # Release budget on pre-call exception
            if reservation is not None and self._budget_service is not None:
                await self._budget_service.release(run_id, reservation)
            raise

        # --- Budget settle (success/unknown usage) ---
        if reservation is not None and self._budget_service is not None:
            from app.domain.budget import BudgetActualUsage
            await self._budget_service.settle(
                run_id,
                reservation,
                BudgetActualUsage(),  # conservative: unknown usage keeps estimate
            )

        # --- InformationFlow output wrapping (T12 S3) ---
        # Redact secrets from the tool result content.
        if self._information_flow_service is not None and result.content is not None:
            sanitized_content = self._information_flow_service.redact_structured(
                result.content
            )
            # A strictly parsed successful host photo result intentionally
            # delivers its short-lived signed URL to the model/user. Preserve
            # only that one field; every other field and every failure remains
            # under the normal structured redaction policy.
            if (
                result.tool_name == "host_terminal"
                and result.status is ToolResultStatus.SUCCESS
                and working_request.name == "host_terminal"
                and is_photo_capability_request(working_request.arguments)
                and is_photo_capability_result(result.content)
                and isinstance(sanitized_content, dict)
            ):
                sanitized_content["signed_url"] = result.content["signed_url"]
            result = ToolResult(
                tool_call_id=result.tool_call_id,
                tool_name=result.tool_name,
                status=result.status,
                content=sanitized_content,
                duration_ms=result.duration_ms,
            )

        return result

    async def _audit_budget(
        self,
        outcome: PolicyOutcome,
        reason: str,
        run_id: str,
    ) -> None:
        if self._audit_service is None:
            return
        event = PolicyAuditEvent(
            policy="budget-policy",
            version="system-v1",
            decision_kind=PolicyDecisionKind.ALLOCATION,
            reason=reason,
            run_id=run_id,
            session_id=run_id,
            outcome=outcome,
        )
        try:
            await self._audit_service.record(event)
        except Exception:
            logger.warning(
                "audit service failed for budget policy run_id=%s",
                run_id,
                exc_info=True,
            )

    def _required_definition(self, name: str) -> ToolDefinition:
        definition = self._definition(name)
        if definition is None:
            raise ToolNotFoundError(f"tool not found: {name}")
        return definition

    def _definition_for_evaluation(
        self,
        request: ToolCallRequest,
        evaluation: ToolExecutionEvaluation,
    ) -> ToolDefinition:
        evaluated = (
            self._evaluated_executions.get(evaluation._token)
            if isinstance(evaluation, ToolExecutionEvaluation)
            else None
        )
        if (
            evaluated is None
            or evaluated.request is not request
            or evaluation.approval.name != request.name
            or self._definition(request.name) is not evaluated.definition
        ):
            raise ToolDefinitionChangedError("tool definition changed after evaluation")
        return evaluated.definition


def knowledge_tool_definitions(enabled: bool = True) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="search_knowledge",
            description="Search a configured knowledge base by kb_id.",
            input_schema={
                "type": "object",
                "properties": {
                    "kb_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "top_k": {"type": "integer", "minimum": 1, "maximum": 50},
                    "min_score": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["kb_id", "query"],
                "additionalProperties": False,
            },
            enabled=enabled,
            source_type=ToolSourceType.KNOWLEDGE,
            toolset="knowledge",
        )
    ]


def builtin_tool_definitions(web_fetch_enabled: bool = True) -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_current_time",
            description="Get the current UTC time.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source_type=ToolSourceType.BUILTIN,
            toolset="system",
        ),
        ToolDefinition(
            name="calculator",
            description="Evaluate a safe arithmetic expression.",
            input_schema={
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
                "additionalProperties": False,
            },
            source_type=ToolSourceType.BUILTIN,
            toolset="math",
        ),
        ToolDefinition(
            name="list_directory",
            description="List entries in a directory under the configured workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            source_type=ToolSourceType.BUILTIN,
            toolset="file",
        ),
        ToolDefinition(
            name="read_text_file",
            description="Read a text file under the configured workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            source_type=ToolSourceType.BUILTIN,
            toolset="file",
        ),
        ToolDefinition(
            name="web_fetch",
            description="Fetch a public HTTP/HTTPS URL with SSRF protection and return text or parsed JSON content.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1},
                    "format": {"type": "string", "enum": ["text", "json"]},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            enabled=web_fetch_enabled,
            source_type=ToolSourceType.BUILTIN,
            toolset="web",
        ),
        ToolDefinition(
            name="vision_analyze",
            description="Analyze an image using the active provider's vision capability. Accepts data URL or http(s) URL.",
            input_schema={
                "type": "object",
                "properties": {
                    "image_url": {"type": "string", "minLength": 1},
                    "question": {"type": "string", "minLength": 1},
                },
                "required": ["image_url", "question"],
                "additionalProperties": False,
            },
            source_type=ToolSourceType.BUILTIN,
            toolset="vision",
        ),
    ]


def schedule_tool_definitions() -> list[ToolDefinition]:
    manage_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "pause", "resume", "run", "remove"],
            },
            "task_id": {"type": "string"},
            "name": {"type": "string"},
            "prompt": {"type": "string"},
            "cron_expression": {"type": "string"},
            "timezone": {"type": "string"},
            "delivery_target": {
                "type": "string",
                "enum": ["origin", "dashboard", "silent"],
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    query_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get"]},
            "task_id": {"type": "string"},
        },
        "required": ["action"],
        "additionalProperties": False,
    }
    return [
        ToolDefinition(
            name="manage_schedule",
            description=(
                "Manage scheduled tasks (create/update/pause/resume/run/remove). "
                "Consult skill_view('n-agent') for cron syntax and self-contained prompt rules."
            ),
            input_schema=manage_schema,
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="schedule",
            managed=True,
        ),
        ToolDefinition(
            name="schedule_query",
            description="Query scheduled tasks visible to the current Feishu origin or session.",
            input_schema=query_schema,
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="schedule",
        ),
    ]
