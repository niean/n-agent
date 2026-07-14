import shutil
import subprocess
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
    # fixed 10-key order
    assert "'turn', 'context', 'llm', 'tool', 'memory'" in src
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
    # overview sector (stats-bar + stat-card), no topbar last-update usage
    assert "整体概览" in src
    assert "stat-card" in src
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
    # security menu is a top-level leaf after the full observations group
    assert html.index('data-tab-group="observations"') < html.index('href="/security"')
    # tab container after observations tab contents
    assert html.index('id="tab-observations-sessions"') < html.index('id="tab-security"')
    # script loaded after management modules, before app.js
    assert html.index('/static/management-navigation.js') < html.index('/static/security.js')
    assert html.index('/static/security.js') < html.index('/static/app.js')
    # label present
    assert "安全" in html


def test_summary_has_security_entry():
    summary_js = (STATIC_DIR / "summary.js").read_text(encoding="utf-8")
    assert "{ tab: 'security', label: '安全', desc: '查看各领域 Policy 管控策略' }" in summary_js
    # placed after the observations entries
    assert summary_js.index("tab: 'observations-modules'") < summary_js.index("tab: 'security'")


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
