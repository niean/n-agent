"""Tests for BrowserToolExecutor + tool definitions (T8)."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.application.browser_service import HostGrantApprovalRequired
from app.application.browser_tool_executor import (
    BrowserToolExecutor,
    browser_tool_definitions,
)
from app.domain.browser import (
    BrowserActionResult,
    BrowserBackendType,
    BrowserSessionStatus,
)
from app.domain.tool import (
    RiskLevel,
    ToolCallRequest,
    ToolExecutionContext,
    ToolResult,
    ToolResultStatus,
    ToolSourceType,
)


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def test_returns_six_tool_definitions():
    defs = browser_tool_definitions()
    assert len(defs) == 6
    names = {d.name for d in defs}
    assert names == {
        "browser_navigate",
        "browser_observe",
        "browser_click",
        "browser_type",
        "browser_scroll",
        "browser_screenshot",
    }


def test_all_definitions_source_type_agent_and_toolset_browser():
    defs = browser_tool_definitions()
    for d in defs:
        assert d.source_type is ToolSourceType.AGENT
        assert d.toolset == "browser"


def test_all_schemas_have_additional_properties_false():
    defs = browser_tool_definitions()
    for d in defs:
        assert d.input_schema.get("additionalProperties") is False
        assert d.input_schema.get("type") == "object"


def test_no_session_backend_profile_path_params_in_schema():
    defs = browser_tool_definitions()
    forbidden = {"browser_session_id", "backend", "backend_type", "profile", "profile_ref", "path"}
    for d in defs:
        props = set(d.input_schema.get("properties", {}).keys())
        assert not (props & forbidden), f"{d.name} has forbidden params: {props & forbidden}"


def test_navigate_observe_scroll_screenshot_are_safe_risk():
    defs = browser_tool_definitions()
    by_name = {d.name: d for d in defs}
    assert by_name["browser_navigate"].risk_level is RiskLevel.SAFE
    assert by_name["browser_observe"].risk_level is RiskLevel.SAFE
    assert by_name["browser_scroll"].risk_level is RiskLevel.SAFE
    assert by_name["browser_screenshot"].risk_level is RiskLevel.SAFE


def test_click_type_are_confirm_risk():
    defs = browser_tool_definitions()
    by_name = {d.name: d for d in defs}
    assert by_name["browser_click"].risk_level is RiskLevel.CONFIRM
    assert by_name["browser_type"].risk_level is RiskLevel.CONFIRM


def test_navigate_schema_has_url_required():
    defs = browser_tool_definitions()
    nav = next(d for d in defs if d.name == "browser_navigate")
    assert "url" in nav.input_schema["properties"]
    assert nav.input_schema["required"] == ["url"]


def test_click_schema_has_element_ref_required():
    defs = browser_tool_definitions()
    click = next(d for d in defs if d.name == "browser_click")
    assert "element_ref" in click.input_schema["properties"]
    assert "document_revision" in click.input_schema["properties"]
    assert set(click.input_schema["required"]) >= {"element_ref", "document_revision"}


def test_type_schema_has_text_required():
    defs = browser_tool_definitions()
    type_def = next(d for d in defs if d.name == "browser_type")
    assert "text" in type_def.input_schema["properties"]
    assert set(type_def.input_schema["required"]) >= {"element_ref", "document_revision", "text"}


def test_scroll_schema_optional_element_ref():
    defs = browser_tool_definitions()
    scroll = next(d for d in defs if d.name == "browser_scroll")
    assert "element_ref" in scroll.input_schema["properties"]
    assert "document_revision" in scroll.input_schema["properties"]
    # element_ref optional for whole-page scroll
    assert "element_ref" not in scroll.input_schema.get("required", [])


def test_screenshot_schema_has_full_page_optional():
    defs = browser_tool_definitions()
    shot = next(d for d in defs if d.name == "browser_screenshot")
    assert "full_page" in shot.input_schema["properties"]
    # No required fields for screenshot
    assert shot.input_schema.get("required", []) == []


# ---------------------------------------------------------------------------
# Execute: missing session_id / run_id
# ---------------------------------------------------------------------------


class FakeBrowserService:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, Any, Any]] = []
        self.next_result: BrowserActionResult | None = None
        self.next_exception: Exception | None = None

    async def execute_action(self, n_agent_session_id: str, action: Any, run_context: Any) -> BrowserActionResult:
        self.execute_calls.append((n_agent_session_id, action, run_context))
        if self.next_exception is not None:
            exc = self.next_exception
            self.next_exception = None
            raise exc
        if self.next_result is not None:
            r = self.next_result
            self.next_result = None
            return r
        return BrowserActionResult(
            action_type=type(action).__name__.replace("Action", "").lower(),
            status="success",
            document_revision=0,
        )


@pytest.mark.asyncio
async def test_execute_without_session_id_returns_error():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(id="tc-1", name="browser_navigate", arguments={"url": "https://example.com"})
    result = await executor.execute(request, None)
    assert result.status is ToolResultStatus.ERROR
    assert "session_id" in str(result.content).lower() or "session" in str(result.content).lower()


@pytest.mark.asyncio
async def test_execute_without_run_id_returns_error():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(id="tc-1", name="browser_navigate", arguments={"url": "https://example.com"})
    ctx = ToolExecutionContext(session_id="sess-1")
    result = await executor.execute(request, ctx)
    assert result.status is ToolResultStatus.ERROR
    assert "run_id" in str(result.content).lower() or "run" in str(result.content).lower()


# ---------------------------------------------------------------------------
# Execute: each action maps with boundary values
# ---------------------------------------------------------------------------


def _ctx(session_id: str = "nagent-1", run_id: str = "run-1") -> ToolExecutionContext:
    return ToolExecutionContext(session_id=session_id, run_id=run_id)


@pytest.mark.asyncio
async def test_execute_host_grant_required_converts_to_permission_denied_signal():
    """When BrowserService raises HostGrantApprovalRequired, the executor
    converts it to a PERMISSION_DENIED ToolResult carrying the host-grant
    marker so AgentGraph can route it to the Chat CONFIRM card flow."""
    service = FakeBrowserService()
    service.next_exception = HostGrantApprovalRequired(
        browser_session_id="bsess-1", n_agent_session_id="nagent-1",
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_navigate",
        arguments={"url": "https://example.com"},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    content = result.content
    assert content["error_code"] == "host_grant_required"
    assert content["approval_kind"] == "host_grant"
    assert content["browser_session_id"] == "bsess-1"


@pytest.mark.asyncio
async def test_execute_other_exception_still_maps_to_error():
    """Non-host-grant exceptions still map to ERROR (browser_unavailable),
    not PERMISSION_DENIED -- no regression in the generic failure path."""
    service = FakeBrowserService()
    service.next_exception = RuntimeError("boom")
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_navigate",
        arguments={"url": "https://example.com"},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.ERROR
    assert result.content["error"] == "browser_unavailable"


@pytest.mark.asyncio
async def test_execute_navigate_delegates_to_service():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_navigate",
        arguments={"url": "https://example.com/page"},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    assert len(service.execute_calls) == 1
    nagent_sid, action, run_ctx = service.execute_calls[0]
    assert nagent_sid == "nagent-1"
    from app.domain.browser import NavigateAction
    assert isinstance(action, NavigateAction)
    assert action.url == "https://example.com/page"


@pytest.mark.asyncio
async def test_execute_observe_delegates():
    service = FakeBrowserService()
    service.next_result = BrowserActionResult(
        action_type="observe",
        status="success",
        document_revision=7,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_observe",
        arguments={"max_text_chars": 1000, "max_elements": 50},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    assert result.content.get("document_revision") == 7
    from app.domain.browser import ObserveAction
    assert isinstance(service.execute_calls[0][1], ObserveAction)


@pytest.mark.asyncio
async def test_execute_click_delegates():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_click",
        arguments={"element_ref": "el-abc", "document_revision": 3},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    from app.domain.browser import ClickAction
    assert isinstance(service.execute_calls[0][1], ClickAction)
    assert service.execute_calls[0][1].element_ref == "el-abc"
    assert service.execute_calls[0][1].document_revision == 3


@pytest.mark.asyncio
async def test_execute_type_delegates():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_type",
        arguments={"element_ref": "el-1", "document_revision": 0, "text": "hello"},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    from app.domain.browser import TypeAction
    assert isinstance(service.execute_calls[0][1], TypeAction)
    assert service.execute_calls[0][1].text == "hello"


@pytest.mark.asyncio
async def test_execute_scroll_delegates():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_scroll",
        arguments={"element_ref": None, "document_revision": 0, "dy": 100},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    from app.domain.browser import ScrollAction
    assert isinstance(service.execute_calls[0][1], ScrollAction)
    assert service.execute_calls[0][1].dy == 100


@pytest.mark.asyncio
async def test_execute_screenshot_delegates():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_screenshot",
        arguments={"full_page": True},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    from app.domain.browser import ScreenshotAction
    assert isinstance(service.execute_calls[0][1], ScreenshotAction)
    assert service.execute_calls[0][1].full_page is True


# ---------------------------------------------------------------------------
# Reject unknown fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_rejects_unknown_fields_in_arguments():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_navigate",
        arguments={"url": "https://example.com", "evil_param": "nope"},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.ERROR
    assert "unknown" in str(result.content).lower() or "unexpected" in str(result.content).lower()


# ---------------------------------------------------------------------------
# ToolResult never leaks screenshot ref / type text / URL query
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_result_never_leaks_screenshot_ref():
    service = FakeBrowserService()
    # Even if the service returned a screenshot_ref, the executor must NOT
    # project it into the ToolResult content.
    service.next_result = BrowserActionResult(
        action_type="screenshot", status="success",
        screenshot_ref="ref-leak-attempt", document_revision=0,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(id="tc-1", name="browser_screenshot", arguments={})
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    content_str = str(result.content)
    assert "ref-leak-attempt" not in content_str
    # screenshot_captured flag is the only screenshot-related field.
    if isinstance(result.content, dict):
        assert result.content.get("screenshot_captured") is True
        assert "screenshot_ref" not in result.content


@pytest.mark.asyncio
async def test_tool_result_never_leaks_full_type_text():
    service = FakeBrowserService()
    service.next_result = BrowserActionResult(
        action_type="type", status="success",
        text="the-typed-secret-value", document_revision=0,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_type",
        arguments={"element_ref": "el-1", "document_revision": 0, "text": "hello"},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    assert "the-typed-secret-value" not in str(result.content)


@pytest.mark.asyncio
async def test_tool_result_never_leaks_url_query_or_fragment():
    service = FakeBrowserService()
    service.next_result = BrowserActionResult(
        action_type="navigate", status="success",
        url="https://example.com/page?secret=abc#frag",
        document_revision=1,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_navigate",
        arguments={"url": "https://example.com/page?secret=abc#frag"},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.SUCCESS
    content_str = str(result.content)
    assert "secret=abc" not in content_str
    assert "#frag" not in content_str


@pytest.mark.asyncio
async def test_tool_result_never_leaks_raw_exception():
    service = FakeBrowserService()
    service.next_result = BrowserActionResult(
        action_type="click", status="error",
        error_code="action_outcome_unknown",
        text="RuntimeError: DOM leak <secret>value</secret>",
        document_revision=0,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_click",
        arguments={"element_ref": "el-1", "document_revision": 0},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.ERROR
    assert "DOM leak" not in str(result.content)
    assert "secret" not in str(result.content).lower()


# ---------------------------------------------------------------------------
# Error mapping: error_code -> ToolResultStatus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_code_maps_to_error_status():
    service = FakeBrowserService()
    service.next_result = BrowserActionResult(
        action_type="observe", status="error",
        error_code="stale_element_ref",
        document_revision=7,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_observe", arguments={},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.ERROR
    assert result.content.get("error_code") == "stale_element_ref"
    assert result.content.get("document_revision") == 7


@pytest.mark.asyncio
async def test_permission_denied_error_code_maps_to_permission_denied():
    service = FakeBrowserService()
    service.next_result = BrowserActionResult(
        action_type="observe", status="error",
        error_code="host_grant_required",
        document_revision=0,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_observe", arguments={},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.PERMISSION_DENIED
    assert result.content.get("error_code") == "host_grant_required"


@pytest.mark.asyncio
async def test_timeout_error_code_maps_to_timeout_status():
    service = FakeBrowserService()
    service.next_result = BrowserActionResult(
        action_type="observe", status="error",
        error_code="browser_action_timeout",
        document_revision=0,
    )
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(
        id="tc-1", name="browser_observe", arguments={},
    )
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.TIMEOUT


# ---------------------------------------------------------------------------
# Unknown tool name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_tool_name_returns_error():
    service = FakeBrowserService()
    executor = BrowserToolExecutor(service)
    request = ToolCallRequest(id="tc-1", name="browser_unknown", arguments={})
    result = await executor.execute(request, _ctx())
    assert result.status is ToolResultStatus.ERROR
    assert "unknown" in str(result.content).lower() or "not found" in str(result.content).lower()
