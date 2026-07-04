from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
import shutil
import subprocess

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
        "/memory", "/sandbox",
        "/tools", "/tools/builtin", "/tools/knowledge", "/tools/mcp", "/tools/skill", "/tools/plugin",
        "/tools/external-memory", "/tools/sandbox",
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
    for path in ("/memory", "/sandbox", "/tools/external-memory", "/tools/sandbox"):
        res = client.get(path)
        assert res.status_code == 200
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
        "/static/external-memory.js",
        "/static/favicon.svg",
    )
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, f"missing {path}"


def test_external_memory_static_asset_is_plain_javascript(tmp_path):
    client = _client(tmp_path)
    body = client.get('/static/external-memory.js').text
    assert 'namespace.externalMemory' in body
    assert '/chat/external-memory/memory-providers' in body
    assert 'api.fetchJson' in body
    assert ': string' not in body
    assert '| null' not in body
    node = shutil.which('node')
    if node is not None:
        result = subprocess.run(
            [node, '--check', str(STATIC_DIR / 'external-memory.js')],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_static_assets_contain_expected_logic(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    assert '/chat/external-memory/memory-providers' in chat_js
    assert 'event.shiftKey' in chat_js
    assert 'metadata' in chat_js
    assert "'[DONE]'" in chat_js or 'data: [DONE]' in chat_js
    assert 'if (!text) return' in chat_js
    assert 'renameSession' in chat_js
    assert 'deleteSession' in chat_js
    assert 'window.confirm(' in chat_js
    assert "p.active === true" in chat_js
    assert 'phantom' in chat_js
    assert 'enabledProviders.includes(p.name)' in chat_js
    assert "'removed'" in chat_js
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
    assert 'closeTopModal' in nav_js
    assert "event.key !== 'Escape'" in nav_js
    assert "querySelectorAll('.modal-backdrop')" in nav_js
    assert "modal.querySelector('.modal-close')" in nav_js
    keydown_handler = nav_js[nav_js.index("event.key !== 'Escape'"):]
    assert keydown_handler.index('closeTopModal()') < keydown_handler.index('closeAllPopouts()')
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
    row_start = scheduled_js.index('function renderTaskRow(task)')
    row_end = scheduled_js.index('function openTaskForm(task)', row_start)
    row_body = scheduled_js[row_start:row_end]
    assert "badge(text(task.last_status), statusKind(task.last_status))" in row_body
    assert 'task.last_completed_at' not in row_body
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
    assert 'value="anthropic"' in html


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
    assert 'function formatToolDebugContent(content)' in chat_js
    assert "if (!Array.isArray(content)) return formatDebugJson(content)" in chat_js
    assert "content.map((item, index) => `#${index + 1}\\n${formatDebugJson(item)}`).join('\\n\\n')" in chat_js
    assert "appendToolDebugContent(content, message.content || '')" in chat_js
    assert 'function hasVisibleContent(value)' in chat_js
    assert "if (hasVisibleContent(message.content)) appendText(el, message.content)" in chat_js


def test_assistant_tool_calls_are_not_rendered_as_chat_messages(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    assert 'function shouldRenderMessage(message)' in chat_js
    assert "if (message.role === 'tool') return true" in chat_js
    assert 'function groupToolMessages(messages)' in chat_js
    assert "if (message.role === 'tool' && previous && previous.role === 'tool')" in chat_js
    assert "previous.content.push(message.content || '')" in chat_js
    assert "grouped.push({ ...message, content: [message.content || ''] })" in chat_js
    assert 'const visibleMessages = groupToolMessages((detail.messages || []).filter(shouldRenderMessage))' in chat_js
    assert 'appendDebugJson(content, message.tool_calls)' not in chat_js
    assert "if (Array.isArray(value)) return value.length > 0" in chat_js
    assert "if (typeof value === 'object') return Object.keys(value).length > 0" in chat_js


def test_current_session_refresh_renders_persisted_messages(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    refresh_start = chat_js.index('async function refreshCurrentSession()')
    refresh_end = chat_js.index('async function send()', refresh_start)
    refresh_body = chat_js[refresh_start:refresh_end]
    assert 'renderSessionMessages(detail)' in refresh_body


def test_chat_builtin_memory_is_enabled_and_project_memory_is_disabled_by_default(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    render_start = chat_js.index('function renderExternalMemoryUI()')
    render_end = chat_js.index('function getExternalMemoryEnabled()', render_start)
    render_body = chat_js[render_start:render_end]
    getter_start = chat_js.index('function getExternalMemoryEnabled()')
    getter_end = chat_js.index('function init()', getter_start)
    getter_body = chat_js[getter_start:getter_end]

    assert 'builtin 默认开启' in render_body
    assert '文件记忆最多选择 1 个' in render_body
    assert '首轮发送后锁定' in render_body
    assert '此会话的外部记忆已锁定' in render_body
    assert "cb.checked = p.name === 'builtin'" in render_body
    assert 'cb.disabled = locked' in render_body
    assert "cb.dataset.slot = p.slot" in render_body
    assert 'draftExternalMemoryConfig' in chat_js
    assert 'if (!currentSessionId) return' not in render_body
    assert "cb.checked && cb.dataset.slot === 'multi-project'" in render_body
    assert "checkbox !== cb && checkbox.checked && checkbox.dataset.slot === 'multi-project'" in render_body
    assert 'cb.checked = p.enabled_global' not in render_body
    assert '重置为默认配置' in render_body
    assert "const config = currentSessionId ? sessionExternalMemoryConfig[currentSessionId] : draftExternalMemoryConfig" in getter_body
    assert "if (!config?.modified) return ['builtin']" in getter_body
    assert 'function applySessionExternalMemoryState(detail)' in chat_js


def test_chat_external_memory_draft_selection_is_carried_into_new_session(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    ensure_start = chat_js.index('async function ensureSession()')
    ensure_end = chat_js.index('async function newSession()', ensure_start)
    ensure_body = chat_js[ensure_start:ensure_end]
    render_start = chat_js.index('function renderExternalMemoryUI()')
    render_end = chat_js.index('function getExternalMemoryEnabled()', render_start)
    render_body = chat_js[render_start:render_end]

    assert 'draftExternalMemoryConfig?.modified === true' in ensure_body
    assert 'sessionExternalMemoryConfig[id]' in ensure_body
    assert 'externalMemoryTouched = true' in ensure_body
    assert 'draftExternalMemoryConfig = nextConfig' in render_body


def test_chat_disabled_external_memory_is_not_shown_in_checkboxes(tmp_path):
    """停用状态的外部记忆不会在会话选择界面展示。"""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    render_start = chat_js.index('function renderExternalMemoryUI()')
    render_end = chat_js.index('function getExternalMemoryEnabled()', render_start)
    render_body = chat_js[render_start:render_end]

    # 验证过滤逻辑存在：builtin 始终显示，项目记忆仅在全局启用或会话已选中时显示
    assert "p.name !== 'builtin' && !p.enabled_global && !enabledProviders.includes(p.name)" in render_body
    # 验证过滤在创建 checkbox 之前执行
    filter_idx = render_body.index('p.name !== ' + "'builtin'")
    create_checkbox_idx = render_body.index("const cb = document.createElement('input')")
    assert filter_idx < create_checkbox_idx


def test_chat_deleted_external_memory_uses_legacy_slot_inference(tmp_path):
    """旧会话无 slot 快照时，已删除 provider 仍按历史类型分组展示。"""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    infer_start = chat_js.index('function inferPhantomExternalMemorySlot')
    infer_end = chat_js.index('function renderExternalMemoryUI()', infer_start)
    infer_body = chat_js[infer_start:infer_end]
    render_start = chat_js.index('function renderExternalMemoryUI()')
    render_end = chat_js.index('function getExternalMemoryEnabled()', render_start)
    render_body = chat_js[render_start:render_end]

    assert 'sessionSlots && sessionSlots[name]' in infer_body
    assert "/^external_memory_\\d+$/.test(name)" in infer_body
    assert "/^project_memory_\\d+$/.test(name)" in infer_body
    assert "return 'multi-project'" in infer_body
    assert "const externalQueryPrefixes = ['mem0', 'holographic', 'honcho']" in infer_body
    assert "name === prefix || name.startsWith(prefix + '-') || name.startsWith(prefix + '_')" in infer_body
    assert "return 'external-query'" in infer_body
    assert "return 'removed'" in infer_body
    assert 'slot: inferPhantomExternalMemorySlot(name, sessionSlots)' in render_body


def test_chat_external_memory_collapsed_by_default(tmp_path):
    """外部记忆默认收起，点击展开图标后可编辑，再点击收起。"""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    css = client.get('/static/styles.css').text

    # 验证状态变量存在
    assert 'externalMemoryExpanded = false' in chat_js
    # 验证切换函数存在
    assert 'function toggleExternalMemory()' in chat_js
    # 验证 header 区域和点击事件
    assert "header.className = 'chat-external-memory__header'" in chat_js
    assert 'header.addEventListener' in chat_js
    # 验证展开图标：展开时 ▼，收起时 ▲
    assert 'chat-external-memory__expand-icon' in chat_js
    assert "externalMemoryExpanded ? '▼' : '▲'" in chat_js
    # 验证 content 区域有收起样式
    assert 'chat-external-memory__content--collapsed' in chat_js

    # CSS 验证
    assert '.chat-external-memory__header' in css
    assert 'cursor: pointer' in css  # 可点击
    assert '.chat-external-memory__content--collapsed' in css
    assert 'max-height: 0' in css  # 收起时高度为 0


def test_chat_session_column_width_stays_fixed_when_debug_toggles(tmp_path):
    client = _client(tmp_path)
    css = client.get('/static/styles.css').text
    assert 'grid-template-columns: 280px minmax(360px, 2fr) minmax(280px, 0.9fr)' in css
    assert 'grid-template-columns: 280px minmax(360px, 5fr) 40px' in css


def test_static_assets_use_safe_text_rendering(tmp_path):
    client = _client(tmp_path)
    for path in ('/static/chat.js', '/static/sessions.js', '/static/tools.js',
                 '/static/models.js', '/static/health.js', '/static/summary.js', '/static/scheduled-tasks.js',
                 '/static/skills.js', '/static/knowledge.js', '/static/plugin.js', '/static/external-memory.js'):
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
        '/static/external-memory.js',
        '/static/favicon.svg',
    )
    for asset in assets:
        assert asset in html, f"index.html missing reference to {asset}"
    for tab in ('概览', '对话', '会话', '记忆', '工具', '沙盒', '模型', '观测', '任务', '平台', '知识', 'MCP', 'Skill', 'Plugin', 'Builtin'):
        assert tab in html, f"index.html missing menu label {tab}"
    for path in ('/summary', '/chat', '/sessions', '/memory', '/tools/knowledge', '/tools/mcp', '/tools/skill', '/tools/plugin', '/tools/builtin', '/sandbox', '/models', '/platforms', '/status', '/scheduled-tasks'):
        assert f'href="{path}"' in html, f"index.html missing nav href {path}"
    assert (
        html.index('href="/sessions"')
        < html.index('href="/memory"')
        < html.index('data-tab-group="tools"')
        < html.index('href="/sandbox"')
        < html.index('href="/models"')
        < html.index('href="/platforms"')
        < html.index('href="/status"')
    )
    assert (
        html.index('href="/tools/knowledge"')
        < html.index('href="/tools/mcp"')
        < html.index('href="/tools/skill"')
        < html.index('href="/tools/plugin"')
        < html.index('href="/tools/builtin"')
    )
    assert (
        html.index('id="tab-sessions"')
        < html.index('id="tab-memory"')
        < html.index('id="tab-tools-knowledge"')
        < html.index('id="tab-tools-mcp"')
        < html.index('id="tab-tools-skill"')
        < html.index('id="tab-tools-plugin"')
        < html.index('id="tab-tools-builtin"')
        < html.index('id="tab-sandbox"')
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
    assert '新增' in body
    assert '刷新描述' not in body
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
    assert 'innerHTML =' not in body
    assert 'insertAdjacentHTML' not in body
    assert '.textContent' in body


def test_external_memory_provider_actions_keep_table_cell_layout(tmp_path):
    client = _client(tmp_path)
    memory_js = client.get('/static/external-memory.js').text
    providers_js = client.get('/static/external-memory-providers.js').text
    css_body = client.get('/static/styles.css').text

    assert memory_js.count("actions.className = 'row-actions-cell'") >= 2
    assert memory_js.count("actionGroup.className = 'row-actions row-actions--memory'") >= 2
    assert "actions.className = 'row-actions-cell'" in providers_js
    assert "actionGroup.className = 'row-actions row-actions--memory'" in providers_js
    assert '.row-actions--memory' in css_body
    assert 'min-width: 150px' in css_body


def test_honcho_form_includes_workspace_id(tmp_path):
    client = _client(tmp_path)
    body = client.get('/static/external-memory-providers.js').text
    # honcho 表单应含 workspace_id 字段（v3 API 必需）
    assert "workspace_id" in body
    # honcho 应有独立的 recall_mode select
    assert "recall_mode" in body
    assert "hybrid" in body and "context" in body and "tools" in body
    # honcho 应有 session_strategy select
    assert "session_strategy" in body
    assert "per-session" in body and "persistent" in body
    # honcho 应有 ai_peer_id 字段
    assert "ai_peer_id" in body


def test_retrieval_table_has_recall_mode_column_with_tip(tmp_path):
    client = _client(tmp_path)
    body = client.get('/static/external-memory-providers.js').text
    # 检索记忆列表应有"召回模式"列
    assert '召回模式' in body
    # 列名带 tooltip（复用 panel-title-group / panel-tips 统一样式）
    assert 'panel-tips' in body
    assert 'hybrid' in body and 'context' in body and 'tools' in body
    # 行渲染应调用 recallModeOf
    assert 'recallModeOf' in body
