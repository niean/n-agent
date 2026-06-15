from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app.application.model_service import ModelService
from app.application.session_service import SessionService
from app.application.tool_service import ToolService, builtin_tool_definitions
from app.domain.provider import ModelInfo
from app.domain.tool import ToolCallRequest, ToolExecutor, ToolResult, ToolResultStatus
from app.infrastructure.memory.sqlite_store import SQLiteMemoryStore
from app.interfaces.http.dashboard import STATIC_DIR, create_dashboard_router


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
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(create_dashboard_router(
        SessionService(store),
        tool_service,
        model_service,
        lambda: {
            "provider": {"status": "ok"},
            "memory": {"status": "ok"},
            "knowledge": {"status": "disabled", "enabled": False},
        },
    ))
    return TestClient(app)


def test_chat_returns_index_html(tmp_path):
    response = _client(tmp_path).get("/chat")
    assert response.status_code == 200
    assert "<aside" in response.text
    assert 'id="app-sidebar"' in response.text


def test_all_tab_paths_return_shell(tmp_path):
    client = _client(tmp_path)
    for path in ("/", "/summary", "/chat", "/sessions", "/tools", "/models", "/status"):
        response = client.get(path)
        assert response.status_code == 200, f"missing shell at {path}"
        assert 'id="app-sidebar"' in response.text, f"shell incomplete at {path}"


def test_static_assets_served(tmp_path):
    client = _client(tmp_path)
    paths = (
        "/static/styles.css",
        "/static/app.js",
        "/static/management-api.js",
        "/static/management-ui.js",
        "/static/management-navigation.js",
        "/static/summary.js",
        "/static/chat.js",
        "/static/sessions.js",
        "/static/tools.js",
        "/static/models.js",
        "/static/health.js",
        "/static/favicon.svg",
    )
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, f"missing {path}"


def test_static_assets_contain_expected_logic(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    assert 'event.shiftKey' in chat_js
    assert 'metadata' in chat_js
    assert "'[DONE]'" in chat_js or 'data: [DONE]' in chat_js
    assert 'if (!text) return' in chat_js
    assert 'renameSession' in chat_js
    assert 'deleteSession' in chat_js
    assert 'window.confirm(' in chat_js
    api_js = client.get('/static/management-api.js').text
    assert '/chat/tools' in api_js
    assert '/chat/health/dependencies' in api_js
    assert '/v1/models' in api_js
    assert '/chat/models' in api_js
    assert '/chat/providers' in api_js
    assert 'activateProvider' in api_js
    assert 'deleteProvider' in api_js
    assert 'renameSession' in api_js
    assert 'deleteSession' in api_js
    tools_js = client.get('/static/tools.js').text
    assert "'类型'" in tools_js
    assert "'分组'" in tools_js
    assert 'tool.source_type' in tools_js
    assert 'tool.toolset' in tools_js
    assert "'stdio'" in tools_js
    assert 'Command' in tools_js
    assert 'Args' in tools_js
    assert 'Env' in tools_js
    assert 'Endpoint' in tools_js
    models_js = client.get('/static/models.js').text
    assert '/chat/models' in models_js or 'getAdminModels' in models_js
    assert '/v1/models' not in models_js
    assert 'Display Name' in models_js
    assert 'Default' in models_js
    assert 'listProviders' in models_js
    assert 'activateProvider' in models_js
    assert 'api_key_present' in models_js
    assert "document.createElement('span')" in models_js
    assert "badge.className = 'badge badge--success'" in models_js
    assert "badge.textContent = '✓'" in models_js
    assert 'td6.appendChild(badge)' in models_js
    assert "td6.textContent = '-'" in models_js
    assert "model.is_default === true ? '✓' : '-'" in models_js
    nav_js = client.get('/static/management-navigation.js').text
    assert 'pushState' in nav_js
    assert "'/summary'" in nav_js
    assert "'/status'" in nav_js


def test_models_page_renders_provider_admin_controls(tmp_path):
    client = _client(tmp_path)
    html = client.get('/models').text
    assert 'id="providers-list"' in html
    assert 'id="providers-modal"' in html
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert 'id="providers-form"' in html
    assert 'id="providers-form-close"' in html
    assert 'id="provider-name"' in html
    assert 'id="provider-base-url"' in html
    assert 'id="provider-model"' in html
    assert 'id="provider-api-key"' in html
    assert 'id="providers-new"' in html


def test_chat_debug_panel_is_collapsed_by_default(tmp_path):
    client = _client(tmp_path)
    html = client.get('/chat').text
    assert 'chat-shell chat-shell--debug-collapsed' in html
    assert 'status-panel status-panel--collapsible chat-debug-panel collapsed' in html
    assert 'id="chat-debug-toggle" aria-expanded="false" aria-controls="chat-debug-body"' in html


def test_tool_messages_are_collapsed_by_default(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    assert "message.role === 'tool'" in chat_js
    assert "document.createElement('details')" in chat_js
    assert "document.createElement('summary')" in chat_js
    assert "工具调用调试信息" in chat_js


def test_tool_debug_json_strings_are_formatted(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    assert 'function formatDebugJson(value)' in chat_js
    assert 'JSON.parse(value)' in chat_js
    assert 'JSON.stringify(parsed, null, 2)' in chat_js
    assert 'appendDebugJson(content, message.content || \'\')' in chat_js


def test_current_session_refresh_renders_persisted_messages(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    refresh_start = chat_js.index('async function refreshCurrentSession()')
    refresh_end = chat_js.index('async function send()', refresh_start)
    refresh_body = chat_js[refresh_start:refresh_end]
    assert 'renderSessionMessages(detail)' in refresh_body


def test_chat_session_column_width_stays_fixed_when_debug_toggles(tmp_path):
    client = _client(tmp_path)
    css = client.get('/static/styles.css').text
    assert 'grid-template-columns: 280px minmax(360px, 2fr) minmax(280px, 0.9fr)' in css
    assert 'grid-template-columns: 280px minmax(360px, 5fr) 40px' in css


def test_static_assets_use_safe_text_rendering(tmp_path):
    client = _client(tmp_path)
    for path in ('/static/chat.js', '/static/sessions.js', '/static/tools.js',
                 '/static/models.js', '/static/health.js', '/static/summary.js'):
        body = client.get(path).text
        assert 'innerHTML =' not in body, f"{path} contains innerHTML assignment"
        assert 'insertAdjacentHTML' not in body, f"{path} uses insertAdjacentHTML"
        assert '.textContent' in body, f"{path} lacks textContent rendering"


def test_index_html_links_assets(tmp_path):
    client = _client(tmp_path)
    html = client.get('/chat').text
    assets = (
        '/static/styles.css',
        '/static/app.js',
        '/static/management-api.js',
        '/static/management-ui.js',
        '/static/management-navigation.js',
        '/static/summary.js',
        '/static/chat.js',
        '/static/sessions.js',
        '/static/tools.js',
        '/static/models.js',
        '/static/health.js',
        '/static/favicon.svg',
    )
    for asset in assets:
        assert asset in html, f"index.html missing reference to {asset}"
    for tab in ('概览', '对话', '会话', '工具', '模型', '健康'):
        assert tab in html, f"index.html missing menu label {tab}"
    for path in ('/summary', '/chat', '/sessions', '/tools', '/models', '/status'):
        assert f'href="{path}"' in html, f"index.html missing nav href {path}"
