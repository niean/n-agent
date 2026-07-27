"""Browser tool argument audit projection (T9 / D039).

Pure projection helpers that produce a safe copy of browser tool arguments
for persistence, logging, stream events, and approval displays. The original
arguments remain in memory for actual ToolService.execute and one-time
ToolPolicy.authorize_once -- only the persisted/displayed copies are projected.

Security-critical: the browser toolset MUST NOT be enabled before this
projection is in place. Sensitive browser tool arguments include:
- browser_navigate: URL userinfo/query/fragment
- browser_type: typed text (may contain credentials, PII, etc.)

The projection is fail-closed: unknown browser tools or unexpected argument
shapes return an empty dict to prevent raw argument leakage.
"""
from __future__ import annotations

from typing import Any

import urllib.parse


_BROWSER_TOOL_NAMES: frozenset[str] = frozenset({
    "browser_navigate",
    "browser_observe",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_screenshot",
})


def is_browser_tool(tool_name: str) -> bool:
    """Return True if tool_name is one of the 6 browser_* tools."""
    return tool_name in _BROWSER_TOOL_NAMES


def project_browser_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Project browser tool arguments to a safe dict for persistence/display.

    Returns a NEW safe dict, never mutates input. For browser tools, strips
    or redacts sensitive fields (typed text, URL userinfo/query/fragment).
    For non-browser tools, returns the arguments unchanged (passthrough).

    Fail-closed: unknown browser tools or unexpected argument shapes return
    an empty dict to prevent raw argument leakage.

    Args:
        tool_name: The tool name (e.g. "browser_navigate").
        arguments: The parsed arguments dict.

    Returns:
        A safe dict for persistence/display. For browser tools, a new dict
        with sensitive fields stripped/redacted. For non-browser tools, the
        original arguments dict (passthrough).
    """
    if not isinstance(arguments, dict):
        return {}

    if not is_browser_tool(tool_name):
        # Unknown browser_* tool (e.g. browser_foo): fail-closed to prevent
        # raw argument leakage. Non-browser tools are passthrough (caller
        # handles their own argument safety).
        if tool_name.startswith("browser_"):
            return {}
        return arguments

    if tool_name == "browser_navigate":
        return _project_navigate(arguments)
    if tool_name == "browser_type":
        return _project_type(arguments)
    if tool_name == "browser_click":
        return _project_click(arguments)
    if tool_name == "browser_scroll":
        return _project_scroll(arguments)
    if tool_name == "browser_observe":
        return _project_observe(arguments)
    if tool_name == "browser_screenshot":
        return _project_screenshot(arguments)

    # Unreachable: is_browser_tool gates the 6 names above. Defensive.
    return {}


def _sanitize_url(url: str) -> str:
    """Produce a sanitized audit URL: strip userinfo, query, fragment.

    Keeps scheme, host, port, path. Pure stdlib (no infrastructure dependency),
    so this application module does not import app.infrastructure.
    """
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    port = parsed.port
    if ":" in host:  # IPv6 literal
        if port is not None:
            netloc = f"[{host}]:{port}"
        else:
            netloc = f"[{host}]"
    else:
        if host and port is not None:
            netloc = f"{host}:{port}"
        else:
            netloc = host
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _project_navigate(arguments: dict[str, Any]) -> dict[str, Any]:
    """browser_navigate: keep only sanitized url (strip userinfo/query/fragment).

    If url is missing or invalid, return {} with no leak.
    """
    url = arguments.get("url")
    if not isinstance(url, str) or not url:
        return {}
    try:
        sanitized = _sanitize_url(url)
    except (ValueError, TypeError):
        return {}
    if not sanitized:
        return {}
    return {"url": sanitized}


def _project_type(arguments: dict[str, Any]) -> dict[str, Any]:
    """browser_type: replace text with redaction marker, keep safe fields.

    Required fields: element_ref, document_revision, text.
    Optional: clear_first.
    If text is missing or not a string, return {} (unexpected shape).
    """
    text = arguments.get("text")
    if not isinstance(text, str):
        return {}
    result: dict[str, Any] = {
        "text": {"char_count": len(text), "redacted": True},
    }
    element_ref = arguments.get("element_ref")
    if isinstance(element_ref, str) and element_ref:
        result["element_ref"] = element_ref
    document_revision = arguments.get("document_revision")
    if isinstance(document_revision, int):
        result["document_revision"] = document_revision
    clear_first = arguments.get("clear_first")
    if isinstance(clear_first, bool):
        result["clear_first"] = clear_first
    return result


def _project_click(arguments: dict[str, Any]) -> dict[str, Any]:
    """browser_click: keep element_ref, document_revision."""
    result: dict[str, Any] = {}
    element_ref = arguments.get("element_ref")
    if isinstance(element_ref, str) and element_ref:
        result["element_ref"] = element_ref
    document_revision = arguments.get("document_revision")
    if isinstance(document_revision, int):
        result["document_revision"] = document_revision
    return result


def _project_scroll(arguments: dict[str, Any]) -> dict[str, Any]:
    """browser_scroll: keep element_ref, document_revision, dx, dy."""
    result: dict[str, Any] = {}
    element_ref = arguments.get("element_ref")
    if isinstance(element_ref, str) and element_ref:
        result["element_ref"] = element_ref
    document_revision = arguments.get("document_revision")
    if isinstance(document_revision, int):
        result["document_revision"] = document_revision
    dx = arguments.get("dx")
    if isinstance(dx, int):
        result["dx"] = dx
    dy = arguments.get("dy")
    if isinstance(dy, int):
        result["dy"] = dy
    return result


def _project_observe(arguments: dict[str, Any]) -> dict[str, Any]:
    """browser_observe: keep max_text_chars, max_elements."""
    result: dict[str, Any] = {}
    max_text_chars = arguments.get("max_text_chars")
    if isinstance(max_text_chars, int):
        result["max_text_chars"] = max_text_chars
    max_elements = arguments.get("max_elements")
    if isinstance(max_elements, int):
        result["max_elements"] = max_elements
    return result


def _project_screenshot(arguments: dict[str, Any]) -> dict[str, Any]:
    """browser_screenshot: keep full_page."""
    result: dict[str, Any] = {}
    full_page = arguments.get("full_page")
    if isinstance(full_page, bool):
        result["full_page"] = full_page
    return result


__all__ = [
    "is_browser_tool",
    "project_browser_tool_arguments",
]
