"""Tests for browser tool argument audit projection (T9 / D039).

Covers:
- browser_navigate: URL userinfo/query/fragment stripped via sanitize_url
- browser_type: text replaced with {char_count, redacted}
- browser_click/scroll/observe/screenshot: safe fields kept
- unknown browser tool: fail-closed {}
- non-browser tool: passthrough
- input dict not mutated
- is_browser_tool for the 6 browser_* names
"""
from __future__ import annotations

import copy

from app.application.browser_tool_audit import (
    is_browser_tool,
    project_browser_tool_arguments,
)


# ---------------------------------------------------------------------------
# is_browser_tool
# ---------------------------------------------------------------------------


class TestIsBrowserTool:
    def test_six_browser_tools_are_true(self):
        names = [
            "browser_navigate",
            "browser_observe",
            "browser_click",
            "browser_type",
            "browser_scroll",
            "browser_screenshot",
        ]
        for name in names:
            assert is_browser_tool(name) is True, f"{name} should be a browser tool"

    def test_non_browser_tools_are_false(self):
        for name in ["calculator", "search_knowledge", "", "browser", "browser_foo", "navigate"]:
            assert is_browser_tool(name) is False, f"{name} should not be a browser tool"


# ---------------------------------------------------------------------------
# browser_navigate
# ---------------------------------------------------------------------------


class TestBrowserNavigateProjection:
    def test_strips_query_and_fragment(self):
        args = {"url": "https://example.com/path?secret=token#fragment"}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {"url": "https://example.com/path"}

    def test_strips_userinfo(self):
        args = {"url": "https://user:pass@example.com/path"}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {"url": "https://example.com/path"}

    def test_strips_userinfo_query_and_fragment(self):
        args = {"url": "https://user:pass@example.com/path?q=1#frag"}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {"url": "https://example.com/path"}

    def test_keeps_port_and_path(self):
        args = {"url": "https://example.com:8443/deep/path"}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {"url": "https://example.com:8443/deep/path"}

    def test_keeps_ipv6_host(self):
        args = {"url": "https://[::1]:8080/path"}
        result = project_browser_tool_arguments("browser_navigate", args)
        # sanitize_url preserves IPv6 netloc form
        assert result == {"url": "https://[::1]:8080/path"}

    def test_missing_url_returns_empty(self):
        args = {}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {}

    def test_empty_url_returns_empty(self):
        args = {"url": ""}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {}

    def test_non_string_url_returns_empty(self):
        args = {"url": 123}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {}

    def test_extra_fields_stripped(self):
        args = {"url": "https://example.com/path?q=1", "extra": "leak"}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result == {"url": "https://example.com/path"}
        assert "extra" not in result

    def test_does_not_mutate_input(self):
        args = {"url": "https://example.com/path?q=1#frag"}
        original = copy.deepcopy(args)
        project_browser_tool_arguments("browser_navigate", args)
        assert args == original


# ---------------------------------------------------------------------------
# browser_type
# ---------------------------------------------------------------------------


class TestBrowserTypeProjection:
    def test_redacts_text_to_char_count(self):
        args = {
            "element_ref": "ref-1",
            "document_revision": 3,
            "text": "my secret password",
            "clear_first": True,
        }
        result = project_browser_tool_arguments("browser_type", args)
        assert result == {
            "text": {"char_count": 18, "redacted": True},
            "element_ref": "ref-1",
            "document_revision": 3,
            "clear_first": True,
        }

    def test_text_not_present_returns_empty(self):
        args = {"element_ref": "ref-1", "document_revision": 3}
        result = project_browser_tool_arguments("browser_type", args)
        assert result == {}

    def test_non_string_text_returns_empty(self):
        args = {"element_ref": "ref-1", "document_revision": 3, "text": 123}
        result = project_browser_tool_arguments("browser_type", args)
        assert result == {}

    def test_empty_string_text_still_redacted(self):
        # Empty string is still a string; char_count = 0
        args = {"element_ref": "ref-1", "document_revision": 0, "text": ""}
        result = project_browser_tool_arguments("browser_type", args)
        assert result == {
            "text": {"char_count": 0, "redacted": True},
            "element_ref": "ref-1",
            "document_revision": 0,
        }

    def test_optional_clear_first_omitted(self):
        args = {"element_ref": "ref-1", "document_revision": 1, "text": "hello"}
        result = project_browser_tool_arguments("browser_type", args)
        assert "clear_first" not in result
        assert result["text"] == {"char_count": 5, "redacted": True}

    def test_does_not_mutate_input(self):
        args = {
            "element_ref": "ref-1",
            "document_revision": 1,
            "text": "secret",
            "clear_first": False,
        }
        original = copy.deepcopy(args)
        project_browser_tool_arguments("browser_type", args)
        assert args == original
        # Original text is NOT the redaction marker
        assert args["text"] == "secret"


# ---------------------------------------------------------------------------
# browser_click
# ---------------------------------------------------------------------------


class TestBrowserClickProjection:
    def test_keeps_element_ref_and_revision(self):
        args = {"element_ref": "btn-1", "document_revision": 5}
        result = project_browser_tool_arguments("browser_click", args)
        assert result == {"element_ref": "btn-1", "document_revision": 5}

    def test_missing_fields_omitted(self):
        args = {"element_ref": "btn-1"}
        result = project_browser_tool_arguments("browser_click", args)
        assert result == {"element_ref": "btn-1"}

    def test_empty_dict(self):
        result = project_browser_tool_arguments("browser_click", {})
        assert result == {}


# ---------------------------------------------------------------------------
# browser_scroll
# ---------------------------------------------------------------------------


class TestBrowserScrollProjection:
    def test_keeps_all_fields(self):
        args = {"element_ref": "el-1", "document_revision": 2, "dx": 10, "dy": -5}
        result = project_browser_tool_arguments("browser_scroll", args)
        assert result == {
            "element_ref": "el-1",
            "document_revision": 2,
            "dx": 10,
            "dy": -5,
        }

    def test_optional_element_ref(self):
        args = {"document_revision": 2, "dx": 0, "dy": 100}
        result = project_browser_tool_arguments("browser_scroll", args)
        assert result == {"document_revision": 2, "dx": 0, "dy": 100}

    def test_missing_dx_dy(self):
        args = {"document_revision": 1}
        result = project_browser_tool_arguments("browser_scroll", args)
        assert result == {"document_revision": 1}


# ---------------------------------------------------------------------------
# browser_observe
# ---------------------------------------------------------------------------


class TestBrowserObserveProjection:
    def test_keeps_bounds(self):
        args = {"max_text_chars": 8000, "max_elements": 100}
        result = project_browser_tool_arguments("browser_observe", args)
        assert result == {"max_text_chars": 8000, "max_elements": 100}

    def test_empty_dict(self):
        result = project_browser_tool_arguments("browser_observe", {})
        assert result == {}

    def test_extra_fields_stripped(self):
        args = {"max_text_chars": 4000, "secret": "leak"}
        result = project_browser_tool_arguments("browser_observe", args)
        assert result == {"max_text_chars": 4000}
        assert "secret" not in result


# ---------------------------------------------------------------------------
# browser_screenshot
# ---------------------------------------------------------------------------


class TestBrowserScreenshotProjection:
    def test_keeps_full_page(self):
        args = {"full_page": True}
        result = project_browser_tool_arguments("browser_screenshot", args)
        assert result == {"full_page": True}

    def test_full_page_false(self):
        args = {"full_page": False}
        result = project_browser_tool_arguments("browser_screenshot", args)
        assert result == {"full_page": False}

    def test_empty_dict(self):
        result = project_browser_tool_arguments("browser_screenshot", {})
        assert result == {}


# ---------------------------------------------------------------------------
# Unknown browser tool / fail-closed
# ---------------------------------------------------------------------------


class TestUnknownBrowserTool:
    def test_unknown_browser_tool_returns_empty(self):
        args = {"url": "https://example.com/secret", "text": "leak"}
        result = project_browser_tool_arguments("browser_unknown", args)
        assert result == {}

    def test_unknown_browser_tool_empty_args(self):
        result = project_browser_tool_arguments("browser_foo", {})
        assert result == {}


# ---------------------------------------------------------------------------
# Non-browser tool passthrough
# ---------------------------------------------------------------------------


class TestNonBrowserPassthrough:
    def test_calculator_passthrough(self):
        args = {"expression": "1+2"}
        result = project_browser_tool_arguments("calculator", args)
        assert result is args  # same object (passthrough)

    def test_search_knowledge_passthrough(self):
        args = {"query": "python secrets"}
        result = project_browser_tool_arguments("search_knowledge", args)
        assert result is args

    def test_empty_tool_name_passthrough(self):
        args = {"x": 1}
        result = project_browser_tool_arguments("", args)
        assert result is args


# ---------------------------------------------------------------------------
# Input not mutated (cross-cutting)
# ---------------------------------------------------------------------------


class TestNoMutation:
    def test_navigate_not_mutated(self):
        args = {"url": "https://user:pass@example.com/p?q=1#f"}
        original = copy.deepcopy(args)
        project_browser_tool_arguments("browser_navigate", args)
        assert args == original

    def test_type_not_mutated(self):
        args = {"element_ref": "r", "document_revision": 1, "text": "secret"}
        original = copy.deepcopy(args)
        project_browser_tool_arguments("browser_type", args)
        assert args == original
        assert args["text"] == "secret"

    def test_returns_new_dict_for_browser_tools(self):
        args = {"url": "https://example.com/path"}
        result = project_browser_tool_arguments("browser_navigate", args)
        assert result is not args  # new dict

    def test_arguments_not_dict_returns_empty(self):
        result = project_browser_tool_arguments("browser_navigate", "not a dict")
        assert result == {}
        result = project_browser_tool_arguments("browser_type", None)
        assert result == {}
        result = project_browser_tool_arguments("browser_type", 123)
        assert result == {}
