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
    paths = (
        "/", "/summary", "/chat", "/sessions",
        "/tools", "/tools/builtin", "/tools/knowledge", "/tools/mcp", "/tools/skill", "/tools/plugin",
        "/models", "/status", "/scheduled-tasks", "/platforms",
    )
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, f"missing shell at {path}"
        assert 'id="app-sidebar"' in response.text, f"shell incomplete at {path}"


def test_tools_submenu_url_routing(tmp_path):
    client = _client(tmp_path)
    for sub in ("builtin", "knowledge", "mcp", "skill", "plugin"):
        res = client.get(f"/tools/{sub}")
        assert res.status_code == 200, f"missing /tools/{sub}"
        assert 'id="app-sidebar"' in res.text


def test_old_skills_path_removed(tmp_path):
    client = _client(tmp_path)
    res = client.get("/skills")
    assert res.status_code == 404


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
        "/static/scheduled-tasks.js",
        "/static/platforms.js",
        "/static/skills.js",
        "/static/knowledge.js",
        "/static/plugin.js",
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
    for fn in ('listKnowledgeBases', 'createKnowledgeBase', 'updateKnowledgeBase', 'deleteKnowledgeBase', 'probeKnowledgeBase', 'refreshKnowledgeTool'):
        assert fn in api_js
    for fn in ('listPlatforms', 'getPlatform', 'listPlatformSessions'):
        assert fn in api_js
    tools_js = client.get('/static/tools.js').text
    assert "'类型'" in tools_js
    assert "'分组'" in tools_js
    assert 'tool.source_type' in tools_js
    assert 'tool.toolset' in tools_js
    assert "'builtin', 'agent'" in tools_js
    assert "tool.source_type === 'builtin'" not in tools_js
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
    assert "'/scheduled-tasks'" in nav_js
    assert "'/platforms'" in nav_js
    assert "'scheduled-tasks'" in nav_js
    assert "'platforms'" in nav_js
    assert 'pathByTab' in nav_js
    assert nav_js.index("tab: 'models'") < nav_js.index("tab: 'platforms'") < nav_js.index("tab: 'status'")
    summary_js = client.get('/static/summary.js').text
    assert "'scheduled-tasks'" in summary_js
    assert 'listScheduledTasks' in summary_js
    assert '任务数' in summary_js
    assert summary_js.index("tab: 'chat'") < summary_js.index("tab: 'scheduled-tasks'") < summary_js.index("tab: 'sessions'")
    assert (
        summary_js.index("tab: 'tools-knowledge'")
        < summary_js.index("tab: 'tools-mcp'")
        < summary_js.index("tab: 'tools-skill'")
        < summary_js.index("tab: 'tools-plugin'")
        < summary_js.index("tab: 'tools-builtin'")
    )


def test_platforms_static_assets_contain_readonly_ui(tmp_path):
    client = _client(tmp_path)
    html = client.get('/platforms').text
    assert 'data-tab="platforms"' in html
    assert 'id="platforms-list"' in html
    assert 'id="platforms-sessions"' in html

    platforms_js = client.get('/static/platforms.js').text
    for expected in (
        'listPlatforms',
        'getPlatform',
        'listPlatformSessions',
        'platform_session_id',
        'document.createElement',
        '.textContent',
    ):
        assert expected in platforms_js
    assert 'innerHTML =' not in platforms_js
    assert 'insertAdjacentHTML' not in platforms_js


def test_scheduled_tasks_static_assets_contain_management_ui(tmp_path):
    client = _client(tmp_path)
    api_js = client.get('/static/management-api.js').text
    for expected in (
        'getScheduledTask',
        'createScheduledTask',
        'updateScheduledTask',
        'listScheduledTaskExecutions',
    ):
        assert expected in api_js

    scheduled_js = client.get('/static/scheduled-tasks.js').text
    for expected in (
        'scheduled-modal-grid',
        'openTaskForm',
        'openTaskDetail',
        'listScheduledTaskExecutions',
        'confirmDeleteTask',
        'document.createElement',
        '.textContent',
    ):
        assert expected in scheduled_js
    assert 'innerHTML =' not in scheduled_js
    assert 'insertAdjacentHTML' not in scheduled_js


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
                 '/static/models.js', '/static/health.js', '/static/summary.js', '/static/scheduled-tasks.js',
                 '/static/skills.js', '/static/knowledge.js', '/static/plugin.js'):
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
        '/static/scheduled-tasks.js',
        '/static/skills.js',
        '/static/knowledge.js',
        '/static/plugin.js',
        '/static/favicon.svg',
    )
    for asset in assets:
        assert asset in html, f"index.html missing reference to {asset}"
    for tab in ('概览', '对话', '会话', '工具', '模型', '观测', '任务', '平台', '知识', 'MCP', 'Skill', 'Plugin', 'Builtin'):
        assert tab in html, f"index.html missing menu label {tab}"
    for path in ('/summary', '/chat', '/sessions', '/tools/knowledge', '/tools/mcp', '/tools/skill', '/tools/plugin', '/tools/builtin', '/models', '/platforms', '/status', '/scheduled-tasks'):
        assert f'href="{path}"' in html, f"index.html missing nav href {path}"
    assert html.index('href="/models"') < html.index('href="/platforms"') < html.index('href="/status"')
    assert (
        html.index('href="/tools/knowledge"')
        < html.index('href="/tools/mcp"')
        < html.index('href="/tools/skill"')
        < html.index('href="/tools/plugin"')
        < html.index('href="/tools/builtin"')
    )
    assert (
        html.index('id="tab-tools-knowledge"')
        < html.index('id="tab-tools-mcp"')
        < html.index('id="tab-tools-skill"')
        < html.index('id="tab-tools-plugin"')
        < html.index('id="tab-tools-builtin"')
    )
    assert html.index('id="tab-chat"') < html.index('id="tab-scheduled-tasks"') < html.index('id="tab-sessions"')


def test_management_ui_exports_el_helper(tmp_path):
    client = _client(tmp_path)
    body = client.get('/static/management-ui.js').text
    assert 'el,' in body or 'el }' in body or 'el:' in body
    assert 'createElement' in body


def test_skills_js_present_and_safe(tmp_path):
    client = _client(tmp_path)
    res = client.get('/static/skills.js')
    assert res.status_code == 200
    body = res.text
    assert '/chat/skills' in body or 'listSkills' in body
    assert 'innerHTML =' not in body
    assert '.textContent' in body


def test_skills_js_only_uses_documented_ui_helpers(tmp_path):
    client = _client(tmp_path)
    ui_body = client.get('/static/management-ui.js').text
    skills_body = client.get('/static/skills.js').text
    import re
    used = set(re.findall(r"\bui\.([A-Za-z_][A-Za-z0-9_]*)", skills_body))
    documented = {
        'byId', 'clear', 'appendText', 'appendBadge', 'renderJson',
        'renderEmpty', 'renderLoading', 'renderError', 'el',
    }
    missing = used - documented
    assert not missing, f"skills.js uses undocumented ui helpers: {missing}"
    for name in used:
        assert name in ui_body, f"ui.{name} referenced but not defined in management-ui.js"


def test_index_links_skills_module(tmp_path):
    client = _client(tmp_path)
    html = client.get('/chat').text
    assert 'skills.js' in html
    assert 'tab-tools-skill' in html
    assert 'href="/tools/skill"' in html


def test_skills_static_served(tmp_path):
    client = _client(tmp_path)
    api_js = client.get('/static/management-api.js').text
    for fn in ('listSkills', 'getSkill', 'setSkillEnabled', 'refreshSkills'):
        assert fn in api_js
    nav_js = client.get('/static/management-navigation.js').text
    assert "tab: 'tools-skill'" in nav_js
    assert "'/tools/skill'" in nav_js


def test_tools_submenu_nav(tmp_path):
    client = _client(tmp_path)
    nav_js = client.get('/static/management-navigation.js').text
    assert "tab: 'tools'" in nav_js
    assert "parent: true" in nav_js
    assert "children:" in nav_js
    order_keys = ["'tools-knowledge'", "'tools-mcp'", "'tools-skill'", "'tools-plugin'", "'tools-builtin'"]
    last = -1
    for key in order_keys:
        idx = nav_js.index(key)
        assert idx > last, f"order broken at {key}"
        last = idx
    for path in ("'/tools/knowledge'", "'/tools/mcp'", "'/tools/skill'", "'/tools/plugin'", "'/tools/builtin'"):
        assert path in nav_js, f"missing {path}"
    assert "tab: 'tools', path: '/tools'" not in nav_js


def test_knowledge_js_present_and_safe(tmp_path):
    client = _client(tmp_path)
    res = client.get('/static/knowledge.js')
    assert res.status_code == 200
    body = res.text
    assert 'NAGENT.knowledge' in body or 'namespace.knowledge' in body
    assert 'listTools' in body
    assert 'listKnowledgeBases' in body
    assert "source_type === 'knowledge'" in body
    assert 'document-table' in body
    assert '知识工具' in body
    assert '知识库管理' in body
    assert '+ 新增 KB' in body
    assert "step: '0.01'" in body
    assert 'max: 1' in body
    for field in ('kb_id', 'name', 'description', 'base_type', 'base_url', 'dataset_id', 'api_key', 'default_top_k', 'default_min_score', 'enabled'):
        assert field in body
    assert 'getDependencyHealth' not in body
    assert '/chat/health/dependencies' not in body
    assert 'innerHTML =' not in body
    assert 'insertAdjacentHTML' not in body
    assert '.textContent' in body


def test_knowledge_js_only_uses_documented_ui_helpers(tmp_path):
    client = _client(tmp_path)
    ui_body = client.get('/static/management-ui.js').text
    body = client.get('/static/knowledge.js').text
    import re
    used = set(re.findall(r"\bui\.([A-Za-z_][A-Za-z0-9_]*)", body))
    documented = {'byId', 'clear', 'appendText', 'appendBadge', 'renderJson', 'renderEmpty', 'renderLoading', 'renderError', 'el'}
    assert not (used - documented), f"undocumented: {used - documented}"
    for name in used:
        assert name in ui_body


def test_plugin_js_present_and_safe(tmp_path):
    client = _client(tmp_path)
    res = client.get('/static/plugin.js')
    assert res.status_code == 200
    body = res.text
    assert 'NAGENT.plugin' in body or 'namespace.plugin' in body
    assert 'Plugin' in body
    assert '待实现' in body
    assert 'innerHTML =' not in body
    assert 'insertAdjacentHTML' not in body
    assert '.textContent' in body
