"""T15: Artifact workbench frontend (artifacts.js) source/static tests.

Covers:
- artifacts.js present, served, node --check passes
- index.html: artifacts.js script tag BEFORE app.js; #tab-artifacts container
  unique and default-hidden
- management-navigation.js: '制品' tabConfig entry AFTER the complete executors
  group, BEFORE models
- app.js: artifacts integrated into initialized map + resolveModule
- nav item hidden by default; artifacts.js probes API and only shows nav
  on success (disabled/missing API -> nav stays hidden)
- source safety: NO innerHTML / insertAdjacentHTML / document.write / inline
  handlers (onclick=) / iframe allow-* tokens
- styles.css: two-column workbench + responsive
- Node behavior harness runs (skip gracefully if Node absent)
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.interfaces.http.dashboard import STATIC_DIR

ARTIFACTS_JS = STATIC_DIR / "artifacts.js"
HARNESS_JS = Path(__file__).parent / "artifacts_frontend_harness.js"
NAV_JS = STATIC_DIR / "management-navigation.js"
APP_JS = STATIC_DIR / "app.js"
INDEX_HTML = STATIC_DIR / "index.html"
STYLES_CSS = STATIC_DIR / "styles.css"


# ---------------------------------------------------------------------------
# Source / static assertions
# ---------------------------------------------------------------------------


def test_artifacts_js_exists_and_served(tmp_path):
    """artifacts.js is present in the static directory and served."""
    assert ARTIFACTS_JS.exists(), "artifacts.js missing from static dir"
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    assert "NAGENT.artifacts" in src, "artifacts.js must register NAGENT.artifacts"
    assert "init" in src and "refresh" in src and "deactivate" in src


def test_artifacts_js_node_syntax_check():
    """artifacts.js passes node --check (no syntax errors)."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run(
        [node, "--check", str(ARTIFACTS_JS)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_artifacts_js_script_tag_before_app_js_in_index_html():
    """index.html loads artifacts.js BEFORE app.js (module ready before boot)."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "/static/artifacts.js" in html, "missing artifacts.js script tag"
    assert html.count("/static/artifacts.js") == 1, "artifacts.js must appear once"
    assert html.index("/static/artifacts.js") < html.index("/static/app.js"), \
        "artifacts.js must load before app.js"


def test_artifacts_tab_container_unique_and_default_hidden():
    """#tab-artifacts container appears exactly once and is hidden by default."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="tab-artifacts"' in html, "missing #tab-artifacts mount point"
    assert html.count('id="tab-artifacts"') == 1, "tab-artifacts must appear once"
    # The container must be hidden by default so a flash of empty content does
    # not appear before artifacts.js probes the API.
    tab_match = re.search(r'<div class="tab-content"[^>]*id="tab-artifacts"[^>]*>', html)
    assert tab_match is not None, "tab-artifacts container tag not found"
    opening = tab_match.group(0)
    assert "hidden" in opening, "tab-artifacts must be hidden by default"


def test_nav_order_artifacts_below_executors_in_tabconfig():
    """management-navigation.js: 制品是执行器完整分组之后的一级入口。"""
    src = NAV_JS.read_text(encoding="utf-8")
    assert "'artifacts'" in src or '"artifacts"' in src, "artifacts tab not in tabConfig"
    assert "/artifacts" in src, "artifacts path not in tabConfig"
    assert "制品" in src, "artifacts label '制品' not in tabConfig"
    assert src.index("tab: 'executors'") < src.index("tab: 'sandbox'") \
        < src.index("tab: 'executors-host'") < src.index("tab: 'browser'") \
        < src.index("tab: 'artifacts'") < src.index("tab: 'models'"), \
        "tabConfig order must be executors -> sandbox -> executors-host -> browser -> artifacts -> models"
    for tab in ("sandbox", "executors-host", "browser"):
        start = src.index("tab: '" + tab + "'")
        end = src.index("},", start)
        assert "parentTab: 'executors'" in src[start:end], f"{tab} must remain an executors child"
    artifacts_start = src.index("tab: 'artifacts'")
    artifacts_end = src.index("},", artifacts_start)
    assert "parentTab" not in src[artifacts_start:artifacts_end], "artifacts must remain a top-level entry"


def test_nav_order_artifacts_below_executors_in_index_html_sidebar():
    """index.html sidebar: 制品位于完整执行器分组之后、模型之前。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    group_start = html.index('data-tab-group="executors"')
    group_open = html.rfind('<div', 0, group_start)
    depth = 0
    group_end = None
    for match in re.finditer(r'</?div\b[^>]*>', html[group_open:]):
        if match.group(0).startswith('</'):
            depth -= 1
            if depth == 0:
                group_end = group_open + match.end()
                break
        else:
            depth += 1
    assert group_end is not None, "executors group must have a matching closing div"
    artifacts_index = html.index('data-tab="artifacts"')
    assert group_start < group_end < artifacts_index < html.index('data-tab="models"'), \
        "sidebar order must be executors group -> artifacts -> models"
    group_html = html[group_open:group_end]
    assert group_html.index('data-tab="sandbox"') < group_html.index('data-tab="executors-host"') \
        < group_html.index('data-tab="browser"')
    assert 'data-tab="artifacts"' not in group_html


def test_artifacts_nav_item_hidden_by_default_in_index_html():
    """The artifacts sidebar nav item is hidden by default in index.html;
    artifacts.js reveals it only after a successful API probe."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    # Find the sidebar anchor with data-tab="artifacts".
    nav_match = re.search(
        r'<a class="sidebar__item"[^>]*data-tab="artifacts"[^>]*>.*?</a>',
        html, re.DOTALL,
    )
    assert nav_match is not None, "artifacts sidebar nav item not found in index.html"
    opening = nav_match.group(0)
    assert "hidden" in opening, \
        "artifacts nav item must be hidden by default (probe-gated reveal)"


def test_artifacts_lifecycle_in_app_js():
    """app.js integrates artifacts tab lifecycle (initialized + resolveModule)."""
    src = APP_JS.read_text(encoding="utf-8")
    assert "artifacts: false" in src, "artifacts not in initialized map"
    assert "namespace.artifacts" in src, "artifacts not in resolveModule"


def test_artifacts_js_probes_api_and_gates_nav_visibility():
    """artifacts.js probes /chat/artifacts and only shows the nav item on success;
    on failure (disabled service / missing API) the nav stays hidden."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    assert "/chat/artifacts" in src, "artifacts.js must call /chat/artifacts API"
    # Probe + gate logic: hidden removed only on success.
    assert "hidden" in src, "artifacts.js must manipulate hidden attribute on nav"
    assert "probe" in src.lower() or "disabled" in src.lower() \
        or "unavailable" in src.lower(), \
        "artifacts.js must implement probe/disabled gate for nav visibility"


def test_artifacts_js_source_safety_no_unsafe_dom_sinks():
    """artifacts.js FORBIDS innerHTML, insertAdjacentHTML, document.write,
    inline event handlers (onclick=), and any iframe allow-* tokens."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    assert "innerHTML" not in src, "innerHTML forbidden in artifacts.js"
    assert ".insertAdjacentHTML(" not in src, "insertAdjacentHTML forbidden"
    assert "document.write(" not in src, "document.write forbidden"
    assert ".outerHTML" not in src, "outerHTML forbidden"
    # Inline handlers like onclick=, onload= as attribute assignments.
    assert re.search(r"\bonclick\s*=", src) is None, "inline onclick forbidden"
    assert re.search(r"\bonload\s*=", src) is None, "inline onload forbidden"
    # iframe allow-* tokens (allow-scripts/allow-same-origin/etc.) forbidden:
    # the ONLY display surface for untrusted HTML is a sandbox="" iframe.
    allow_tokens = re.findall(r"allow-(scripts|same-origin|forms|popups|top-navigation|popup|presentation)", src)
    assert not allow_tokens, \
        "iframe allow-* tokens forbidden in artifacts.js, found: " + str(allow_tokens)
    assert "textContent" in src, "textContent must be used for safe rendering"


def test_artifacts_js_uses_sandbox_iframe_without_allow_tokens():
    """HTML-rendering kinds (markdown/document/html/pdf) use sandbox iframe
    with NO allow-* tokens."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    # sandbox attribute must be set as empty string (sandbox="") for untrusted HTML.
    assert "sandbox" in src, "artifacts.js must use sandbox iframe for HTML rendering"
    # Verify the sandbox attribute is set to empty string, not a token list.
    sandbox_assigns = re.findall(r"setAttribute\(\s*['\"]sandbox['\"]\s*,\s*['\"]([^'\"]*)['\"]\s*\)", src)
    for val in sandbox_assigns:
        assert val == "", \
            "sandbox attribute must be empty string (no allow-* tokens), got: " + repr(val)


def test_artifacts_js_blob_cleanup_on_switch_and_destroy():
    """artifacts.js revokes object URLs on artifact switch and on deactivate."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    assert "URL.createObjectURL" in src, "must create object URLs for blobs"
    assert "URL.revokeObjectURL" in src, "must revoke object URLs"
    assert "deactivate" in src, "must expose deactivate for cleanup"


def test_artifacts_js_request_race_cancellation():
    """artifacts.js aborts in-flight fetch on new selection (race cancellation)."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    # AbortController-based cancellation.
    assert "AbortController" in src or "generation" in src or "abort" in src.lower(), \
        "must implement request race cancellation (AbortController/generation guard)"


def test_artifacts_js_explicit_states():
    """artifacts.js has explicit empty/error state text for all required states."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    # Distinct text states (no blank panel for errors).
    for keyword in (
        "loading",       # loading state
        "empty",         # empty list
        "filter",        # filter-empty / filter-no-results
        "unavailable",   # content-unavailable
        "publish",       # publish-blocked
        "revoked",       # revoked
    ):
        assert keyword in src.lower(), f"artifacts.js missing explicit state handling for: {keyword}"


def test_artifacts_js_export_dropdown_by_capabilities():
    """Export dropdown shows formats by server capabilities: original always;
    html only for markdown/document kinds."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    assert "export" in src.lower(), "artifacts.js must implement export"
    assert "original" in src, "export must always offer original format"
    assert "html" in src.lower(), "export must offer html for markdown/document"
    assert "markdown" in src and "document" in src, \
        "export html capability tied to markdown/document kinds"


def test_export_modal_format_options_are_single_choice_compact_grid():
    """Export modal labels the first section 格式 and lays radios out 4-up."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    assert "formatTitle.textContent = '格式'" in src
    assert "radio.type = 'radio'" in src
    assert "radio.name = 'export-format'" in src
    assert ".export-modal__options" in css
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    assert ".export-modal__option { display: flex" in css
    assert "gap: var(--space-2)" in css
    assert "input[type=\"radio\"] { flex: 0 0 auto; width: auto;" in css


def test_export_download_filename_matches_converted_format():
    """Converted artifact downloads replace the source filename extension."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    assert "const extensions = { html: 'html', docx: 'docx', pptx: 'pptx', xlsx: 'xlsx' }" in src
    assert "return stem + '.' + extension;" in src


def test_artifacts_js_publish_flow():
    """Publish: POST -> show share_url + copy + revoke; binary publish shows
    explicit-PUBLIC confirmation."""
    src = ARTIFACTS_JS.read_text(encoding="utf-8")
    assert "/publish" in src, "must call publish endpoint"
    assert "share_url" in src or "share_path" in src, "must show share url after publish"
    assert "revoke" in src.lower(), "must support revoke"
    assert "PUBLIC" in src or "public" in src.lower(), \
        "binary publish must show explicit-PUBLIC confirmation"


def test_artifacts_styles_in_styles_css():
    """styles.css has two-column workbench styles + responsive breakpoint."""
    css = STYLES_CSS.read_text(encoding="utf-8")
    for selector in (
        ".artifacts-shell",
        ".artifacts-list",
        ".artifacts-detail",
    ):
        assert selector in css, f"styles.css missing {selector}"
    # Two-column layout.
    assert "grid-template-columns" in css and "artifacts" in css, \
        "artifacts two-column grid not in styles.css"
    # Responsive breakpoint stacks to single column.
    assert re.search(r"@media[^{]*\{[^}]*\.artifacts-shell[^}]*grid-template-columns:\s*1fr", css, re.DOTALL), \
        "artifacts responsive single-column breakpoint missing"


# ---------------------------------------------------------------------------
# Node behavior harness
# ---------------------------------------------------------------------------


def test_artifacts_frontend_harness():
    """artifacts_frontend_harness.js passes all behavior tests.

    Skips gracefully when Node is unavailable; the source/static assertions
    above still run on the host.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    assert HARNESS_JS.exists(), "artifacts_frontend_harness.js missing"
    result = subprocess.run(
        [node, str(HARNESS_JS)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all tests passed" in result.stdout, \
        "harness did not report success: " + result.stdout + result.stderr
