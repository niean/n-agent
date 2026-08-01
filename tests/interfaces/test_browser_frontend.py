"""Tests for Browser Dashboard frontend (T16).

Covers:
- browser.js syntax check (node --check)
- browser_frontend_harness.js behavior tests
- browser.js source safety (no innerHTML/insertAdjacentHTML, textContent used)
- browser API namespace registered in management-api.js
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

BROWSER_JS = STATIC_DIR / "browser.js"
HARNESS_JS = Path(__file__).parent / "browser_frontend_harness.js"
API_JS = STATIC_DIR / "management-api.js"
NAV_JS = STATIC_DIR / "management-navigation.js"
APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"


def test_browser_js_exists_and_is_served(tmp_path):
    """browser.js is present in the static directory."""
    assert BROWSER_JS.exists(), "browser.js missing from static dir"
    src = BROWSER_JS.read_text(encoding="utf-8")
    assert "NAGENT.browser" in src
    assert "init" in src and "refresh" in src and "deactivate" in src


def test_browser_js_node_syntax_check():
    """browser.js passes node --check (no syntax errors)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, "--check", str(BROWSER_JS)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_browser_frontend_harness():
    """browser_frontend_harness.js passes all behavior tests."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, str(HARNESS_JS)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK browser frontend harness passed" in result.stdout


def test_browser_js_source_safety():
    """browser.js uses textContent only; no innerHTML/insertAdjacentHTML/document.write."""
    src = BROWSER_JS.read_text(encoding="utf-8")
    assert "innerHTML =" not in src, "innerHTML assignment forbidden"
    assert "innerHTML=" not in src, "innerHTML assignment (no space) forbidden"
    assert ".insertAdjacentHTML(" not in src, "insertAdjacentHTML forbidden"
    assert "document.write(" not in src, "document.write forbidden"
    assert ".outerHTML" not in src, "outerHTML forbidden"
    assert "onclick=" not in src, "inline onclick forbidden"
    assert "textContent" in src, "textContent must be used for safe rendering"


def test_browser_js_no_innerhtml_in_comments():
    """Even in comments, browser.js must not mention innerHTML as a pattern to follow.
    The source safety check in the harness checks for actual usage, but we also
    verify the file doesn't promote innerHTML."""
    src = BROWSER_JS.read_text(encoding="utf-8")
    # The only acceptable mention of innerHTML is in a "do not use" context
    # (comments like "No innerHTML"). This is a soft check.
    assert src.count("innerHTML") <= 2, "too many innerHTML mentions in source"


def test_browser_api_namespace_registered():
    """management-api.js exposes NAGENT.api.browser with the required methods."""
    src = API_JS.read_text(encoding="utf-8")
    assert "browser" in src, "browser namespace not in management-api.js"
    assert "listBrowserSessions" in src
    assert "getBrowserSession" in src
    assert "listBrowserActions" in src
    assert "getBrowserTakeoverView" in src
    assert "browserWrite" in src
    assert "X-Browser-Challenge" in src, "X-Browser-Challenge header not set"


def test_browser_tab_in_navigation_config():
    """management-navigation.js has browser tab config entry."""
    src = NAV_JS.read_text(encoding="utf-8")
    assert "'browser'" in src or '"browser"' in src, "browser tab not in tabConfig"
    assert "/browser" in src, "browser path not in tabConfig"
    assert "浏览器" in src, "browser label not in tabConfig"


def test_browser_lifecycle_in_app_js():
    """app.js integrates browser tab lifecycle (initialized + resolveModule)."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "browser: false" in src, "browser not in initialized map"
    assert "namespace.browser" in src, "browser not in resolveModule"


def test_browser_mount_point_and_script_in_index_html():
    """index.html has #tab-browser mount point and browser.js script tag."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="tab-browser"' in html, "missing #tab-browser mount point"
    assert html.count('id="tab-browser"') == 1, "tab-browser must appear once"
    assert '/static/browser.js' in html, "missing browser.js script tag"
    assert html.count('/static/browser.js') == 1, "browser.js must appear once"
    # script loaded before app.js
    assert html.index('/static/browser.js') < html.index('/static/app.js'), \
        "browser.js must load before app.js"
    # tab-browser after tab-security
    assert html.index('id="tab-security"') < html.index('id="tab-browser"'), \
        "tab-browser must come after tab-security"


def test_browser_styles_in_styles_css():
    """styles.css has Browser Dashboard styles."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    # 水平两列：实时视图 80% | 控制与历史 20%（4fr:1fr）；占满视口高度；窄屏堆叠为单列
    assert "grid-template-columns: minmax(0, 4fr) minmax(0, 1fr)" in css
    assert "align-items: stretch; min-height: calc(100vh - 90px)" in css
    assert ".browser-main, .browser-side { width: 100%; min-width: 0; display: flex; flex-direction: column; margin-bottom: 0; }" in css
    assert ".browser-main > .panel-body, .browser-side > .panel-body { flex: 1; min-height: 0; display: flex; flex-direction: column; }" in css
    assert "@media (max-width: 1100px) { .browser-shell { grid-template-columns: 1fr; min-height: 0; } }" in css
    assert ".browser-screenshot-wrap { position: relative; flex: 0 0 auto; width: 100%; aspect-ratio: 16 / 9;" in css
    assert ".browser-controls .btn { font-size: var(--font-size-md); }" in css
    for selector in (
        ".browser-shell",
        ".browser-main",
        ".browser-side",
        ".browser-screenshot",
        ".browser-screenshot-stale",
        ".browser-takeover",
        ".browser-controls",
        ".browser-actions",
    ):
        assert selector in css, f"styles.css missing {selector}"


def test_browser_js_has_control_matrix():
    """browser.js implements the control matrix for all 6 statuses."""
    src = BROWSER_JS.read_text(encoding="utf-8")
    for status in (
        "pending_authorization",
        "active",
        "paused",
        "takeover",
        "degraded",
        "closed",
    ):
        assert status in src, f"control matrix missing status: {status}"
    # Write ops mapped to control buttons
    assert "host_grant" in src
    assert "pause" in src
    assert "resume" in src
    assert "takeover" in src
    assert "release" in src
    assert "close" in src


def test_browser_js_takeover_view_not_in_localStorage():
    """browser.js source: takeover-view URL must not be written to localStorage.
    Poll frequency is fixed at 1s; no localStorage.setItem calls remain."""
    src = BROWSER_JS.read_text(encoding="utf-8")
    import re
    setitem_calls = re.findall(r'localStorage\.setItem\([^)]+\)', src)
    assert len(setitem_calls) == 0, f"expected no localStorage.setItem calls, found {len(setitem_calls)}: {setitem_calls}"


def test_browser_js_strips_query_fragment_from_url():
    """browser.js must strip query/fragment from URLs before display."""
    src = BROWSER_JS.read_text(encoding="utf-8")
    assert "stripQueryFragment" in src or "stripQuery" in src, \
        "URL sanitization function not found"
    assert "indexOf('?')" in src or "split('?')" in src, \
        "URL query stripping not implemented"
