"""BrowserToolExecutor + tool definitions (T8).

Defines the 6 browser tools (browser_navigate, browser_observe, browser_click,
browser_type, browser_scroll, browser_screenshot) and an executor that
delegates to BrowserService. The executor:

- Requires context.session_id and context.run_id (stable ERROR otherwise).
- Builds action value objects from arguments, rejecting unknown fields.
- Projects BrowserActionResult -> ToolResult: NEVER includes screenshot bytes/
  ref/URL, full type text, URL query/fragment, or raw exception.
- Screenshot tool returns only ``screenshot_captured: true``.
- Does NOT call ApprovalDecider (approval is via ToolPolicy/AgentGraph); the
  executor only depends on BrowserService.
"""
from __future__ import annotations

import logging
from typing import Any

from app.domain.browser import (
    BrowserActionResult,
    ClickAction,
    NavigateAction,
    ObserveAction,
    ScrollAction,
    ScreenshotAction,
    TypeAction,
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

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def browser_tool_definitions() -> list[ToolDefinition]:
    """Return the 6 browser tool definitions.

    All definitions:
    - source_type = AGENT
    - toolset = "browser"
    - additionalProperties = false
    - NO browser_session_id / backend / profile / path params
    """
    return [
        ToolDefinition(
            name="browser_navigate",
            description="Navigate the browser session to a URL. URL is safety-verified.",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "minLength": 1, "maxLength": 2048},
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="browser",
        ),
        ToolDefinition(
            name="browser_observe",
            description="Observe the current page: returns bounded text and a list of interactable elements with opaque element_ref values.",
            input_schema={
                "type": "object",
                "properties": {
                    "max_text_chars": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 4000},
                    "max_elements": {"type": "integer", "minimum": 1, "maximum": 200, "default": 80},
                },
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="browser",
        ),
        ToolDefinition(
            name="browser_click",
            description="Click an element identified by its element_ref (from observe). Requires confirmation.",
            input_schema={
                "type": "object",
                "properties": {
                    "element_ref": {"type": "string", "minLength": 1, "maxLength": 128},
                    "document_revision": {"type": "integer", "minimum": 0},
                },
                "required": ["element_ref", "document_revision"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="browser",
        ),
        ToolDefinition(
            name="browser_type",
            description="Type text into an input element identified by element_ref. Requires confirmation. Sensitive fields (password/secret/token/credit-card) trigger takeover, never type.",
            input_schema={
                "type": "object",
                "properties": {
                    "element_ref": {"type": "string", "minLength": 1, "maxLength": 128},
                    "document_revision": {"type": "integer", "minimum": 0},
                    "text": {"type": "string", "minLength": 1, "maxLength": 10000},
                    "clear_first": {"type": "boolean", "default": False},
                },
                "required": ["element_ref", "document_revision", "text"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.CONFIRM,
            source_type=ToolSourceType.AGENT,
            toolset="browser",
        ),
        ToolDefinition(
            name="browser_scroll",
            description="Scroll the page or a specific element by (dx, dy) pixels.",
            input_schema={
                "type": "object",
                "properties": {
                    "element_ref": {"type": "string", "minLength": 1, "maxLength": 128},
                    "document_revision": {"type": "integer", "minimum": 0},
                    "dx": {"type": "integer", "default": 0},
                    "dy": {"type": "integer", "default": 0},
                },
                "required": ["document_revision"],
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="browser",
        ),
        ToolDefinition(
            name="browser_screenshot",
            description="Capture a screenshot of the current page. Bytes are persisted server-side; the tool result only carries screenshot_captured: true.",
            input_schema={
                "type": "object",
                "properties": {
                    "full_page": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            risk_level=RiskLevel.SAFE,
            source_type=ToolSourceType.AGENT,
            toolset="browser",
        ),
    ]


# ---------------------------------------------------------------------------
# Allowed argument keys per tool (for unknown-field rejection)
# ---------------------------------------------------------------------------


_ALLOWED_ARGUMENTS: dict[str, set[str]] = {
    "browser_navigate": {"url"},
    "browser_observe": {"max_text_chars", "max_elements"},
    "browser_click": {"element_ref", "document_revision"},
    "browser_type": {"element_ref", "document_revision", "text", "clear_first"},
    "browser_scroll": {"element_ref", "document_revision", "dx", "dy"},
    "browser_screenshot": {"full_page"},
}


# Permission-denied-style error codes that should map to
# ToolResultStatus.PERMISSION_DENIED rather than ERROR.
_PERMISSION_ERROR_CODES = frozenset({
    "host_grant_required",
    "session_not_active",
    "takeover_requires_approval",
    "sensitive_field_requires_takeover",
})

_TIMEOUT_ERROR_CODES = frozenset({
    "browser_action_timeout",
})


# ---------------------------------------------------------------------------
# BrowserToolExecutor
# ---------------------------------------------------------------------------


class BrowserToolExecutor(ToolExecutor):
    """Executor that delegates browser actions to BrowserService.

    The executor does NOT call ApprovalDecider; approval is handled by
    ToolPolicy/AgentGraph. The executor only depends on BrowserService.
    """

    def __init__(self, browser_service: Any) -> None:
        # browser_service is BrowserService, but we accept Any to avoid
        # import cycles (BrowserService imports this module for definitions).
        self._service = browser_service

    async def execute(
        self, request: ToolCallRequest, context: ToolExecutionContext | None = None
    ) -> ToolResult:
        tool_call_id = request.id
        tool_name = request.name

        # Validate context.
        if context is None or not context.session_id or not context.run_id:
            missing = []
            if context is None or not context.session_id:
                missing.append("session_id")
            if context is None or not context.run_id:
                missing.append("run_id")
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=ToolResultStatus.ERROR,
                content={"error": f"missing required context: {', '.join(missing)}"},
            )

        # Validate tool name.
        if tool_name not in _ALLOWED_ARGUMENTS:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=ToolResultStatus.ERROR,
                content={"error": f"unknown browser tool: {tool_name}"},
            )

        # Reject unknown fields in arguments.
        allowed = _ALLOWED_ARGUMENTS[tool_name]
        unknown = set(request.arguments.keys()) - allowed
        if unknown:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=ToolResultStatus.ERROR,
                content={"error": f"unknown fields: {sorted(unknown)}"},
            )

        # Build action value object.
        try:
            action = self._build_action(tool_name, request.arguments)
        except ValueError as exc:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=ToolResultStatus.ERROR,
                content={"error": f"invalid arguments: {exc}"},
            )

        # Delegate to BrowserService.
        # Reuse the RunContext from the service module.
        try:
            from app.application.browser_service import RunContext
            run_context = RunContext(
                n_agent_session_id=context.session_id,
                run_id=context.run_id,
                actor_id=context.trusted_metadata.get("actor_id") if context.trusted_metadata else None,
            )
        except ImportError:
            # Fallback if browser_service not available; use a simple object.
            class _Ctx:
                def __init__(self, n_agent_session_id: str, run_id: str, actor_id: str | None = None) -> None:
                    self.n_agent_session_id = n_agent_session_id
                    self.run_id = run_id
                    self.actor_id = actor_id
            run_context = _Ctx(context.session_id, context.run_id)

        try:
            result = await self._service.execute_action(
                context.session_id, action, run_context
            )
        except Exception as exc:
            logger.warning("browser service failed for %s", tool_name, exc_info=True)
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status=ToolResultStatus.ERROR,
                content={"error": "browser_unavailable", "error_code": "service_error"},
            )

        return self._project_result(tool_call_id, tool_name, result)

    # ------------------------------------------------------------------
    # Action construction
    # ------------------------------------------------------------------

    def _build_action(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        if tool_name == "browser_navigate":
            return NavigateAction(url=str(arguments["url"]))
        if tool_name == "browser_observe":
            return ObserveAction(
                max_text_chars=int(arguments.get("max_text_chars", 4000)),
                max_elements=int(arguments.get("max_elements", 80)),
            )
        if tool_name == "browser_click":
            return ClickAction(
                element_ref=str(arguments["element_ref"]),
                document_revision=int(arguments["document_revision"]),
            )
        if tool_name == "browser_type":
            return TypeAction(
                element_ref=str(arguments["element_ref"]),
                document_revision=int(arguments["document_revision"]),
                text=str(arguments["text"]),
                clear_first=bool(arguments.get("clear_first", False)),
            )
        if tool_name == "browser_scroll":
            element_ref = arguments.get("element_ref")
            return ScrollAction(
                element_ref=str(element_ref) if element_ref is not None else None,
                document_revision=int(arguments["document_revision"]),
                dx=int(arguments.get("dx", 0)),
                dy=int(arguments.get("dy", 0)),
            )
        if tool_name == "browser_screenshot":
            return ScreenshotAction(
                full_page=bool(arguments.get("full_page", False))
            )
        raise ValueError(f"unknown tool: {tool_name}")

    # ------------------------------------------------------------------
    # Result projection (security-critical)
    # ------------------------------------------------------------------

    def _project_result(
        self,
        tool_call_id: str,
        tool_name: str,
        result: BrowserActionResult,
    ) -> ToolResult:
        status = self._map_status(result)
        content: dict[str, Any] = {
            "action_type": result.action_type,
            "status": result.status,
            "document_revision": result.document_revision,
        }

        if result.status == "success":
            # Safe URL projection: strip query/fragment.
            if result.url:
                content["url"] = self._safe_url(result.url)
            if result.title:
                content["title"] = result.title[:200]
            if result.text and tool_name == "browser_observe":
                # Observe text is already cleaned by the service.
                content["text"] = result.text
            if result.elements:
                content["elements"] = [
                    {
                        "element_ref": e.element_ref,
                        "role": e.role,
                        "accessible_name": e.accessible_name,
                        "text_excerpt": e.text_excerpt,
                        "disabled": e.disabled,
                    }
                    for e in result.elements
                ]
            if tool_name == "browser_screenshot":
                content["screenshot_captured"] = True
                # NEVER include screenshot_ref, URL, or bytes.
            if result.warning_code:
                content["warning_code"] = result.warning_code
        else:
            # Error path: only error_code, no raw exception text.
            content["error_code"] = result.error_code or "unknown_error"

        # NEVER include: screenshot_ref, screenshot bytes, full type text,
        # URL query/fragment, raw exception. (content dict above deliberately
        # omits these.)

        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status=status,
            content=content,
            duration_ms=result.duration_ms,
        )

    def _map_status(self, result: BrowserActionResult) -> ToolResultStatus:
        if result.status == "success":
            return ToolResultStatus.SUCCESS
        if result.error_code in _PERMISSION_ERROR_CODES:
            return ToolResultStatus.PERMISSION_DENIED
        if result.error_code in _TIMEOUT_ERROR_CODES:
            return ToolResultStatus.TIMEOUT
        return ToolResultStatus.ERROR

    def _safe_url(self, url: str) -> str:
        """Strip query and fragment from URL for safe projection."""
        out = url
        if "?" in out:
            out = out.split("?", 1)[0]
        if "#" in out:
            out = out.split("#", 1)[0]
        return out


__all__ = [
    "BrowserToolExecutor",
    "browser_tool_definitions",
]
