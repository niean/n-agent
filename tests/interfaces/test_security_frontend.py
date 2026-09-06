import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.provider import ModelInfo
from app.domain.tool import ToolCallRequest, ToolExecutor, ToolResult, ToolResultStatus
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router

SECURITY_JS = STATIC_DIR / "security.js"
HARNESS_JS = Path(__file__).parent / "security_frontend_harness.js"
STYLES_CSS = STATIC_DIR / "styles.css"


class _SubmenuTabsParser(HTMLParser):
    """Collect data-tab descendants of one named sidebar submenu."""

    def __init__(self, submenu_of: str):
        super().__init__()
        self._submenu_of = submenu_of
        self._stack: list[bool] = []
        self.tabs: set[str] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if any(self._stack) and values.get("data-tab"):
            self.tabs.add(values["data-tab"])
        self._stack.append(values.get("data-submenu-of") == self._submenu_of)

    def handle_endtag(self, tag):
        if self._stack:
            self._stack.pop()


def _submenu_tabs(html: str, submenu_of: str) -> set[str]:
    parser = _SubmenuTabsParser(submenu_of)
    parser.feed(html)
    return parser.tabs


class _StubExecutor(ToolExecutor):
    async def execute(self, request: ToolCallRequest) -> ToolResult:
        return ToolResult(request.id, request.name, ToolResultStatus.SUCCESS, {})


class _StubProvider:
    async def list_models(self):
        return [ModelInfo("real-1", "Real 1", "openai-compatible", True, True)]

    async def supports_tools(self, model: str):
        return True

    async def chat(self, *args, **kwargs):
        raise NotImplementedError


def _client(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "sessions.db")
    tool_service = ToolService(_StubExecutor(), builtin_tool_definitions())
    model_service = ModelService(_StubProvider(), "real-1")
    app = FastAPI()
    app.include_router(create_dashboard_router(
        SessionService(store), tool_service, model_service,
        lambda: {"provider": {"status": "ok"}},
    ))
    return TestClient(app)


def test_security_js_static_contract():
    src = SECURITY_JS.read_text(encoding="utf-8")
    assert "NAGENT.security" in src
    assert "api.listPolicies" in src
    # fixed 11-key order (regex across whitespace)
    m = re.search(r"const\s+EXPECTED_KEYS\s*=\s*\[([^\]]+)\]", src)
    assert m, "EXPECTED_KEYS 数组未找到"
    keys = re.findall(r"'([^']+)'", m.group(1))
    assert keys == [
        'turn', 'context', 'llm', 'tool', 'memory',
        'sandbox', 'gateway', 'schedule', 'budget', 'information_flow',
        'delegation',
    ], f"EXPECTED_KEYS 顺序不符: {keys}"
    # 数量校验以 EXPECTED_KEYS.length 为唯一来源（跨空白正则）
    assert re.search(r"policies\.length\s*!==\s*EXPECTED_KEYS\.length", src), \
        "validate() 必须以 EXPECTED_KEYS.length 校验数量"
    # 顶层 envelope 严格校验存在（跨空白正则），必须位于读取 payload 字段前
    assert re.search(r"sameKeys\s*\(\s*payload\s*,\s*\[\s*'profile_version'\s*,\s*'policies'\s*\]\s*\)", src), \
        "顶层 envelope 校验必须精确为 sameKeys(payload, ['profile_version', 'policies'])"
    # race guard + single init
    assert "state.token" in src and "myToken" in src
    assert "if (initialized) return" in src
    # error + retry, fixed message
    assert "策略加载失败" in src
    assert "重试" in src
    # header renders display_name (name/domain_file are validated but not rendered;
    # the Node harness verifies they do not leak into the DOM)
    assert "p.display_name" in src
    # no modal / detail button
    assert "详情" not in src
    assert "modal" not in src.lower()
    # namespaced layout classes, no generic cfg-item
    assert "policy-meta" in src
    assert "policy-cfg" in src
    assert "policy-item" in src
    assert "cfg-item" not in src
    # overview sector (stats-bar + stat-card) no longer rendered in security page
    # render(); overview() 兼容函数仍存在供 tasks-security.js 预检，函数体保留
    # `整体概览` 字面与 stat-card 引用。render() 必须不再调用 overview。
    assert "整体概览" in src, "overview() 兼容导出应保留字面量"
    assert "root.appendChild(overview(data))" not in src, \
        "render() 必须不再追加 overview sector"
    assert "stat-card" in src, "overview() 仍依赖 stat-card"
    assert "last-update" not in src
    # safe rendering only
    assert "document.createElement" in src
    assert ".textContent" in src
    for forbidden in ("innerHTML =", "outerHTML", "insertAdjacentHTML", "document.write", "onclick="):
        assert forbidden not in src, f"security.js contains {forbidden}"


def test_security_shell_menu_and_assets(tmp_path):
    client = _client(tmp_path)
    html = client.get("/security").text
    assert client.get("/security").status_code == 200
    assert 'id="app-sidebar"' in html
    # exactly one security menu link + tab + script
    assert html.count('data-tab="security"') == 1
    assert html.count('href="/security"') == 1
    assert html.count('id="tab-security"') == 1
    assert html.count('/static/security.js') == 1
    # security is a parent group after the full observations group
    assert html.index('data-tab-group="observations"') < html.index('href="/security"')
    assert html.count('data-tab-group="security"') == 1
    assert html.count('data-submenu-of="security"') == 1
    security_parent = re.search(
        r'<button\b(?=[^>]*\bdata-tab="security")'
        r'(?=[^>]*\bclass="[^"]*\bsidebar__item--parent\b[^"]*")'
        r'(?=[^>]*\btype="button")(?=[^>]*\baria-expanded="false")[^>]*>',
        html,
    )
    assert security_parent, "security root must be an expandable sidebar parent button"
    for tab in ('security-overview', 'security-sessions', 'security-memory', 'security-sandbox'):
        assert html.count(f'data-tab="{tab}"') == 1
    assert _submenu_tabs(html, "security") >= {
        "security-overview", "security-sessions", "security-memory", "security-sandbox",
    }, "all security child tabs must be contained by the security submenu"
    for href in ('/security/sessions', '/security/memory', '/security/sandbox'):
        assert html.count(f'href="{href}"') == 1
    # tab container after observations tab contents
    assert html.index('id="tab-observations-sessions"') < html.index('id="tab-security"')
    # script loaded after management modules, before app.js
    assert html.index('/static/management-navigation.js') < html.index('/static/security.js')
    assert html.index('/static/security.js') < html.index('/static/app.js')
    # label present
    assert "安全" in html


@pytest.mark.parametrize("path", ["/security", "/security/sessions", "/security/memory", "/security/sandbox"])
def test_security_shell_routes_return_dashboard_shell(tmp_path, path):
    response = _client(tmp_path).get(path)
    assert response.status_code == 200
    assert 'id="app-sidebar"' in response.text
    assert 'id="tab-security"' in response.text


def test_summary_has_security_entry():
    summary_js = (STATIC_DIR / "summary.js").read_text(encoding="utf-8")
    security_entry = re.search(
        r"\{\s*tab\s*:\s*['\"]security-overview['\"]\s*,\s*"
        r"label\s*:\s*['\"]安全['\"]",
        summary_js,
    )
    assert security_entry, "summary must contain a security-overview entry labelled 安全"
    # placed after the observations entries
    assert summary_js.index("tab: 'observations-modules'") < security_entry.start()


def test_security_styles_are_namespaced():
    css = STYLES_CSS.read_text(encoding="utf-8")
    for selector in (".policy-meta", ".policy-cfg", ".policy-item", ".policy-k", ".policy-v"):
        assert selector in css, f"styles.css missing {selector}"
    # no generic cfg-item rule introduced
    assert ".cfg-item" not in css


def test_security_js_node_syntax_check():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run([node, "--check", str(SECURITY_JS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_security_frontend_harness():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    result = subprocess.run([node, str(HARNESS_JS)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK security frontend harness passed" in result.stdout
