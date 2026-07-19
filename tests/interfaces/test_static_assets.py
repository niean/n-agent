from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from pathlib import Path
import re
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
        "/models", "/observations/sessions", "/observations/modules", "/scheduled-tasks", "/platforms", "/security",
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


def test_skills_js_renders_format_status(tmp_path):
    client = _client(tmp_path)
    skills_js = client.get("/static/skills.js").text
    # T10: skills.js renders the format_status column badge using safe DOM
    # text (textContent), never innerHTML. (Detail modal does not show
    # format_messages.)
    assert "format_status" in skills_js
    assert "textContent" in skills_js
    assert "innerHTML" not in skills_js



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
    styles_css = client.get('/static/styles.css').text
    tasks_js = client.get('/static/tasks.js').text
    assert '#tab-tasks.active' in styles_css
    assert '#tasks-board-view > .status-panel' in styles_css
    assert '.kanban-board' in styles_css
    assert 'width: calc(100vw - var(--sidebar-width-collapsed))' in styles_css
    assert 'width: calc(100vw - var(--sidebar-width-expanded))' in styles_css
    assert 'min-height: calc(100vh - 179px)' in styles_css
    kanban_board_rule = styles_css[styles_css.index('.kanban-board {'):styles_css.index('.kanban-column {')]
    assert '--kanban-column-count: 5' in kanban_board_rule
    assert '--kanban-column-gap: 12px' in kanban_board_rule
    assert 'container-type: inline-size' in kanban_board_rule
    assert 'repeat(var(--kanban-column-count), minmax(0, 1fr))' in kanban_board_rule
    assert 'width: 100%' in kanban_board_rule
    assert 'min-width: 0' in kanban_board_rule
    assert 'overflow-x: hidden' in kanban_board_rule
    assert 'overflow-x: auto' not in kanban_board_rule
    assert 'overflow-y: auto' not in kanban_board_rule
    kanban_column_rule = styles_css[styles_css.index('.kanban-column {'):styles_css.index('.kanban-column__header {')]
    assert 'width: 100%' in kanban_column_rule
    assert 'min-width: 0' in kanban_column_rule
    assert 'max-width' not in kanban_column_rule
    assert 'repeat(4, minmax(180px, 1fr))' not in styles_css
    assert '.tasks-detail-drawer' not in styles_css
    assert "backdrop.id = 'tasks-detail-modal'" in tasks_js
    assert "el('div', 'modal-backdrop')" in tasks_js
    assert "el('section', 'modal-dialog tasks-modal')" in tasks_js
    assert 'function formatTaskTime' in tasks_js
    assert '8 * 3600 * 1000' in tasks_js
    chat_js = client.get('/static/chat.js').text
    assert '/chat/external-memory/memory-providers' in chat_js
    assert 'event.shiftKey' in chat_js
    assert 'X-Session-ID' in chat_js
    assert "'[DONE]'" in chat_js or 'data: [DONE]' in chat_js
    assert 'if (!text && !pendingImages.length) return' in chat_js
    assert 'renameSession' in chat_js
    assert 'deleteSession' in chat_js
    assert 'modal.confirm(' in chat_js
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
    assert "badge badge--${p.is_active ? 'success' : 'warning'}" in models_js
    assert "p.is_active ? '启用' : '停用'" in models_js
    assert 'td6.appendChild(activeBadge)' in models_js
    assert "model.is_default === true ? '✓' : '-'" in models_js
    nav_js = client.get('/static/management-navigation.js').text
    assert 'pushState' in nav_js
    assert "'/summary'" in nav_js
    assert "'/observations/sessions'" in nav_js
    assert "'/observations/modules'" in nav_js
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
    assert nav_js.index("tab: 'models'") < nav_js.index("tab: 'platforms'") < nav_js.index("tab: 'observations'")
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
    # 确认执行后不再弹出结果弹框（prd: 确认后弹框消失）
    assert 'function renderRunResult' not in scheduled_js
    assert "type: 'run_result'" not in scheduled_js
    row_start = scheduled_js.index('function renderTaskRow(task)')
    row_end = scheduled_js.index('function openTaskForm(task)', row_start)
    row_body = scheduled_js[row_start:row_end]
    assert "badge(text(task.last_status), statusKind(task.last_status))" in row_body
    assert 'task.last_completed_at' not in row_body
    # 详情入口使用本 Tab 导航，不再新建标签页（prd: 详情页面使用本Tab）
    assert "linkButton" not in row_body
    assert "target = '_blank'" not in row_body
    assert "goToDetail(task.id)" in row_body
    # 执行按钮二次确认：点击后先弹确认框，确认后再触发执行（prd: 执行按钮，弹框确认后再触发执行）
    assert "confirmRunTask(task)" in row_body
    assert "function renderRunConfirm" in scheduled_js
    assert "type: 'run_confirm'" in scheduled_js
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
    assert "if (hasVisibleContent(content)) renderMessageText(el, content)" in chat_js


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


def test_observations_cache_metrics_split_read_and_write(tmp_path):
    client = _client(tmp_path)
    observations_js = client.get('/static/observations.js').text
    assert 'function formatCacheHitRate(item)' in observations_js
    assert 'return formatPercent(read, input + read + write)' in observations_js
    assert "{ label: '缓存读', value: formatNumber(stats.cache_read_tokens) },\n      { label: '缓存写', value: formatNumber(stats.cache_write_tokens) }" in observations_js
    assert "['时间', '模型', '调起类型', '输入', '输出', '缓存读', '缓存写', '命中率', '归一化', '延迟(ms)', '操作']" in observations_js
    assert "formatNumber(r.cache_read_tokens),\n          formatNumber(r.cache_write_tokens),\n          formatCacheHitRate(r)" in observations_js
    assert '缓存 Token' not in observations_js


def test_fe_list_numeric_columns_are_right_aligned_and_grouped(tmp_path):
    client = _client(tmp_path)
    guidelines = Path('.harness/framework/guides/10-guidelines-fe.md').read_text(encoding='utf-8')
    css_body = client.get('/static/styles.css').text
    observations_js = client.get('/static/observations.js').text
    sandbox_js = client.get('/static/sandbox.js').text

    assert '数字列的列头名称与数据取值均右对齐，数据取值采用千分位法展示' in guidelines
    assert '.document-table th.document-table__numeric, .document-table td.document-table__numeric { text-align: right; white-space: nowrap; }' in css_body
    assert "return n.toLocaleString()" in observations_js
    assert "function appendNumericHeaderCell(row, label)" in observations_js
    assert "function appendNumericCell(row, value)" in observations_js
    assert "if (h === '对话轮数' || h === 'API 调用' || h === '归一化 Token') appendNumericHeaderCell(trh, h)" in observations_js
    assert "['输入', '输出', '缓存读', '缓存写', '命中率', '归一化', '延迟(ms)'].includes(h)" in observations_js
    assert "['压缩前', '压缩后', '节省', '压缩比'].includes(h)" in observations_js
    assert "function openCompressionModal" in observations_js
    assert "openCompressionModal(c)" in observations_js
    assert "压缩前 (Before · 被压缩的原始消息)" in observations_js
    assert "压缩后 (After · 压缩后的摘要消息)" in observations_js
    assert "formatNumber(r.normalized_tokens)" in observations_js
    assert "return n.toLocaleString()" in sandbox_js
    assert "if (['超时(秒)', '最大工具调用', '空闲回收(秒)'].includes(label)) th.className = 'document-table__numeric';" in sandbox_js
    assert "appendNumericCell(tr, r.timeout_seconds == null ? '-' : formatNumber(r.timeout_seconds));" in sandbox_js
    assert "appendNumericCell(tr, r.max_tool_calls == null ? '-' : formatNumber(r.max_tool_calls));" in sandbox_js
    assert "appendNumericCell(tr, isDocker && r.idle_seconds != null ? formatNumber(r.idle_seconds) : '-');" in sandbox_js
    assert "if (label === '耗时(ms)') th.className = 'document-table__numeric';" in sandbox_js
    assert "appendNumericCell(tr, it.duration_ms == null ? '-' : formatNumber(it.duration_ms));" in sandbox_js


def test_platform_list_session_count_is_numeric_column(tmp_path):
    client = _client(tmp_path)
    platforms_js = client.get('/static/platforms.js').text

    assert "function formatNumber(value)" in platforms_js
    assert "return n.toLocaleString()" in platforms_js
    assert "function appendNumericText(parent, tag, content)" in platforms_js
    assert "if (label === '会话数') appendNumericText(header, 'th', label);" in platforms_js
    assert "appendNumericText(row, 'td', formatNumber(platform.session_count));" in platforms_js


def test_current_session_refresh_renders_persisted_messages(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    refresh_start = chat_js.index('async function refreshCurrentSession()')
    refresh_end = chat_js.index('async function send()', refresh_start)
    refresh_body = chat_js[refresh_start:refresh_end]
    assert 'renderSessionMessages(detail)' in refresh_body


def test_chat_builtin_memory_is_disabled_by_default(tmp_path):
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    render_start = chat_js.index('function renderExternalMemoryUI()')
    render_end = chat_js.index('function getExternalMemoryEnabled()', render_start)
    render_body = chat_js[render_start:render_end]
    getter_start = chat_js.index('function getExternalMemoryEnabled()')
    getter_end = chat_js.index('function init()', getter_start)
    getter_body = chat_js[getter_start:getter_end]

    # 描述文案：未锁定提示默认关闭+互斥+锁定语义；锁定后提示已锁定
    assert '文件记忆最多选择 1 个' in render_body
    assert '首轮发送后锁定' in render_body
    assert '此会话的记忆已锁定' in render_body
    # 药丸默认不激活：active 状态由 useSessionConfig && enabledProviders.includes 决定，
    # 未操作时 useSessionConfig=false，药丸不带 active class
    assert 'const isActive = useSessionConfig && enabledProviders.includes(p.name)' in render_body
    assert 'if (isActive) pill.classList.add(\'active\')' in render_body
    # 锁定时药丸禁用
    assert 'pill.disabled = locked' in render_body
    assert "pill.dataset.slot = p.slot" in render_body
    assert 'draftExternalMemoryConfig' in chat_js
    assert 'if (!currentSessionId) return' not in render_body
    # multi-project slot 互斥：激活时取消其他同 slot 药丸
    assert "nextActive && pill.dataset.slot === 'multi-project'" in render_body
    assert "other !== pill && other.classList.contains('active') && other.dataset.slot === 'multi-project'" in render_body
    # active 状态不直接取 p.enabled_global（仅用于可见性过滤）
    assert 'pill.classList.add(\'active\')' in render_body
    assert "const config = currentSessionId ? sessionExternalMemoryConfig[currentSessionId] : draftExternalMemoryConfig" in getter_body
    assert "if (!config?.modified) return []" in getter_body
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


def test_chat_disabled_external_memory_is_not_shown_as_pills(tmp_path):
    """停用状态的外部记忆不会在记忆药丸行展示。"""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    render_start = chat_js.index('function renderExternalMemoryUI()')
    render_end = chat_js.index('function getExternalMemoryEnabled()', render_start)
    render_body = chat_js[render_start:render_end]

    # 验证过滤逻辑存在：builtin 始终显示，项目记忆仅在全局启用或会话已选中时显示
    assert "p.name !== 'builtin' && !p.enabled_global && !enabledProviders.includes(p.name)" in render_body
    # 验证过滤在创建药丸之前执行
    filter_idx = render_body.index('p.name !== ' + "'builtin'")
    create_pill_idx = render_body.index("const pill = document.createElement('button')")
    assert filter_idx < create_pill_idx


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


def test_chat_memory_uses_toolbar_popover_grouped_picker(tmp_path):
    """记忆入口位于 composer 工具栏；点击"记忆"按钮弹出 Popover，
    Popover 内按系统/文件/检索/已移除分组选择。"""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    css = client.get('/static/styles.css').text

    # 折叠/展开机制已移除
    assert 'externalMemoryExpanded' not in chat_js
    assert 'function toggleExternalMemory()' not in chat_js
    assert "chat-external-memory__header" not in chat_js
    assert "chat-external-memory__content--collapsed" not in chat_js
    assert "externalMemoryExpanded ? '▼' : '▲'" not in chat_js

    # 触发按钮保留图标样式，使用 DOM API 创建 SVG，避免 innerHTML
    assert "function createMemoryTriggerIcon()" in chat_js
    assert "document.createElementNS('http://www.w3.org/2000/svg'" in chat_js
    assert "class: 'chat-memory-trigger__icon'" in chat_js
    assert "triggerLabel.className = 'chat-memory-trigger__label'" in chat_js
    assert "triggerLabel.textContent = '记忆'" in chat_js
    assert "trigger.append(createMemoryTriggerIcon(), triggerLabel)" in chat_js
    assert "trigger.textContent = '记忆'" not in chat_js
    assert "title.textContent = '外部记忆'" not in chat_js

    # 工具栏按钮 + Popover 结构
    assert "const composerBar = document.querySelector('.chat-composer__bar')" in chat_js
    assert 'composerBar.insertBefore(emContainer, sendBtn)' in chat_js
    assert "bar.className = 'chat-memory-bar'" in chat_js
    assert "trigger.className = 'chat-memory-trigger'" in chat_js
    assert "popover.className = 'chat-memory-popover'" in chat_js
    assert "pill.className = 'chat-memory-option'" in chat_js
    assert "memoryPopoverOpen = !memoryPopoverOpen" in chat_js
    assert "document.addEventListener('click', handleMemoryDocumentClick)" in chat_js

    # Popover 分组恢复：系统 / 文件 / 检索 / 已移除
    assert "const GROUP_ORDER = ['builtin', 'multi-project', 'external-query', 'removed']" in chat_js
    assert "'builtin': '系统'" in chat_js
    assert "'multi-project': '文件'" in chat_js
    assert "'external-query': '检索'" in chat_js
    assert "'removed': '已移除'" in chat_js
    assert "group.className = 'chat-memory-popover__group'" in chat_js
    assert "groupTitle.className = 'chat-memory-popover__group-title'" in chat_js

    # CSS：trigger + popover + option active 主色
    assert '.chat-memory-bar' in css
    assert '.chat-memory-trigger' in css
    assert '.chat-memory-trigger__icon' in css
    assert '.chat-memory-trigger__label' in css
    assert '.chat-memory-popover' in css
    assert '.chat-memory-popover__group' in css
    assert '.chat-memory-popover__group-title' in css
    assert '.chat-memory-option' in css
    assert 'border-radius: 999px' in css
    assert '.chat-memory-option.active' in css
    assert 'var(--color-primary)' in css
    assert 'var(--color-on-primary)' in css

    # 旧折叠 CSS 已移除
    assert '.chat-external-memory__header' not in css
    assert '.chat-external-memory__content--collapsed' not in css
    assert '.chat-external-memory__checkboxes' not in css


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


def test_dashboard_uses_shared_modal_instead_of_native_alert():
    """Dashboard feedback must use NAGENT.modal.alert for consistent styling."""
    for path in STATIC_DIR.glob('*.js'):
        if path.name == 'management-ui.js':
            continue  # This module owns the shared modal.alert implementation.
        body = path.read_text()
        assert not re.search(r'(?<![.\w])alert\s*\(', body), f"{path.name} uses native alert"
        assert 'window.alert' not in body, f"{path.name} uses window.alert"
        assert 'globalThis.alert' not in body, f"{path.name} uses globalThis.alert"


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
    for tab in ('概览', '对话', '会话', '记忆', '工具', '沙盒', '模型', '观测', '任务', '平台', '知识', 'MCP', 'Skill', 'Plugin', 'Builtin', '组件'):
        assert tab in html, f"index.html missing menu label {tab}"
    for path in ('/summary', '/chat', '/sessions', '/memory', '/tools/knowledge', '/tools/mcp', '/tools/skill', '/tools/plugin', '/tools/builtin', '/sandbox', '/models', '/observations/sessions', '/observations/modules', '/platforms', '/scheduled-tasks'):
        assert f'href="{path}"' in html, f"index.html missing nav href {path}"
    assert (
        html.index('href="/sessions"')
        < html.index('href="/memory"')
        < html.index('data-tab-group="tools"')
        < html.index('href="/sandbox"')
        < html.index('href="/models"')
        < html.index('href="/platforms"')
        < html.index('data-tab-group="observations"')
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


def test_modal_locks_background_scroll_and_traps_focus(tmp_path):
    """Modal open must lock body scroll (overflow:hidden via body.modal-open)
    and trap Tab focus within the topmost modal."""
    client = _client(tmp_path)
    css = client.get('/static/styles.css').text
    assert 'body.modal-open' in css
    assert 'overflow: hidden' in css

    js = client.get('/static/management-ui.js').text
    # body class synced via MutationObserver on document.body
    assert 'MutationObserver' in js
    assert 'modal-open' in js
    assert 'openModalBackdrops' in js or "querySelectorAll('.modal-backdrop')" in js
    # Tab key focus trap
    assert "event.key !== 'Tab'" in js
    assert 'focusableElements' in js
    assert 'focusTopModal' in js


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
    css = client.get('/static/styles.css').text
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
    assert "sidebar__item--parent-selected" not in nav_js
    assert "sidebar__item--parent-selected" not in css
    child_active_rule = css[
        css.index(".sidebar__item--child.sidebar__item--active"):
        css.index("/* 收起态：二级菜单浮层弹出，不展开左导 */")
    ]
    assert "border-left-color" not in child_active_rule


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


def test_plugin_js_open_detail_calls_api_get_plugin(tmp_path):
    """T13 S1/S2: openDetail(key) must call api.getPlugin(key) to fetch detail,
    not use the cached list row. Must be async and handle fetch failure."""
    client = _client(tmp_path)
    body = client.get('/static/plugin.js').text

    # locate openDetail function body (may be 'async function openDetail(' or 'function openDetail(')
    start = body.index('function openDetail(')
    # back up to include 'async ' prefix if present
    if start >= 6 and body[start - 6:start] == 'async ':
        start = start - 6
    # the function body ends at the next top-level "function " declaration
    end = body.index('function openConfig(', start)
    detail_body = body[start:end]

    # MUST be async so it can await api.getPlugin
    assert detail_body.startswith('async function openDetail(')
    # MUST call api.getPlugin(key) -- not plugins.find
    assert 'api.getPlugin(' in detail_body
    # MUST NOT use list row as detail source (the old behavior)
    assert 'plugins.find' not in detail_body


def test_plugin_js_open_detail_handles_fetch_failure_without_stale_data(tmp_path):
    """T13 S4: when api.getPlugin fails, modal must show safe error and not
    open with stale list-row data."""
    client = _client(tmp_path)
    body = client.get('/static/plugin.js').text

    start = body.index('function openDetail(')
    end = body.index('function openConfig(', start)
    detail_body = body[start:end]

    # try/catch around the fetch
    assert 'try' in detail_body
    assert 'catch' in detail_body
    # On failure: alert via modal.alert (safe error message), then return without
    # opening a modal with stale data
    assert 'modal.alert' in detail_body
    # Must NOT proceed to build the modal from list row on failure: the modal
    # build (ui.el('div', 'modal-backdrop')) should only appear AFTER the
    # successful fetch, inside the try block.
    try_idx = detail_body.index('try')
    backdrop_idx = detail_body.index("ui.el('div', 'modal-backdrop')")
    assert backdrop_idx > try_idx, "modal backdrop must be built after try (only on fetch success)"


def test_plugin_js_detail_modal_renders_four_dependency_sections(tmp_path):
    """T13 S2: modal adds Pip, External, Required Plugins, Warnings four
    read-only sections from dependency_status. Empty category shows None."""
    client = _client(tmp_path)
    body = client.get('/static/plugin.js').text

    # 4 read-only section titles present in plugin.js source
    # (labels rendered via textContent)
    for label in ('Pip', 'External', 'Required Plugins', 'Warnings'):
        assert label in body, f"plugin.js missing detail section label: {label}"

    # Empty category displays 'None'
    assert "'None'" in body or '"None"' in body or '`None`' in body

    # Reads dependency_status fields: pip / external / requires_plugins / warnings
    assert 'dependency_status' in body
    assert 'pip' in body
    assert 'external' in body
    assert 'requires_plugins' in body
    assert 'warnings' in body


def test_plugin_js_detail_renders_install_strings_as_text_only(tmp_path):
    """T13 S2: install/check/diagnostic/name rendered via textContent / ui.appendText.
    No clickable install action, no command execution, no innerHTML."""
    client = _client(tmp_path)
    body = client.get('/static/plugin.js').text

    # No command execution primitives
    for forbidden in (
        'eval(',
        'new Function(',
        'window.open(',
        'location.href =',
        'document.createElement(\'a\')',
        "document.createElement(\"a\")",
    ):
        assert forbidden not in body, f"plugin.js uses forbidden primitive: {forbidden}"

    # No install-related clickable action label (would indicate install button)
    assert "'安装'" not in body
    assert '"安装"' not in body
    assert "'执行'" not in body
    assert '"执行"' not in body

    # install/check/diagnostic must be rendered via ui.appendText (the project's
    # safe text helper) or direct textContent assignment -- never innerHTML.
    # We already assert no innerHTML above; ensure ui.appendText is used for
    # the detail rendering.
    assert 'ui.appendText' in body


def test_plugin_js_only_uses_documented_ui_helpers(tmp_path):
    """T13 S2: plugin.js must only use ui helpers documented in management-ui.js
    (appendText/el/renderEmpty/renderError etc.), no undocumented helpers."""
    client = _client(tmp_path)
    ui_body = client.get('/static/management-ui.js').text
    body = client.get('/static/plugin.js').text
    import re
    used = set(re.findall(r"\bui\.([A-Za-z_][A-Za-z0-9_]*)", body))
    documented = {
        'byId', 'clear', 'appendText', 'appendBadge', 'renderJson',
        'renderEmpty', 'renderLoading', 'renderError', 'el',
    }
    missing = used - documented
    assert not missing, f"plugin.js uses undocumented ui helpers: {missing}"
    for name in used:
        assert name in ui_body, f"ui.{name} referenced but not defined in management-ui.js"


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


def test_chat_supports_image_upload_paste_and_rendering(tmp_path):
    """S1-S4: Dashboard chat supports image upload, paste, preview, image-only sends,
    list content rendering with img elements, and fetch error handling."""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    html = client.get('/chat').text

    # S1: image upload/paste infrastructure
    assert 'pendingImages' in chat_js
    assert 'FileReader' in chat_js
    assert 'readAsDataURL' in chat_js
    assert "addEventListener('paste'" in chat_js
    assert 'chat-image-input' in chat_js
    assert 'image_url' in chat_js

    # S2: image-only not blocked (old `if (!text) return` replaced)
    assert 'if (!text && !pendingImages.length) return' in chat_js
    assert 'buildChatRequestBody(text, sentImages)' in chat_js
    assert 'const outgoingImages = images || pendingImages' in chat_js

    # S3: list content rendering uses img element, no innerHTML
    assert "document.createElement('img')" in chat_js
    assert 'innerHTML =' not in chat_js

    # S4: fetch checks res.ok and handles non-2xx errors
    assert 'res.ok' in chat_js

    # HTML: composer has image button, hidden file input, preview container
    assert 'id="chat-image-button"' in html
    assert 'id="chat-image-input"' in html
    assert 'id="chat-image-previews"' in html


def test_provider_form_includes_supports_vision_checkbox(tmp_path):
    """S5: Provider form includes supports_vision checkbox."""
    client = _client(tmp_path)
    html = client.get('/models').text
    assert 'id="provider-supports-vision"' in html


def test_models_js_reads_writes_supports_vision(tmp_path):
    """S9: models.js reads/writes supports_vision and shows Vision column."""
    client = _client(tmp_path)
    models_js = client.get('/static/models.js').text
    assert 'provider-supports-vision' in models_js
    assert 'supports_vision' in models_js
    assert 'Vision' in models_js


def test_chat_composer_uses_doubao_style_rounded_container(tmp_path):
    """Chat composer redesigned to Doubao style: rounded container holding
    previews + textarea + icon button bar. Memory mode switcher lives in the toolbar."""
    client = _client(tmp_path)
    html = client.get('/chat').text
    css = client.get('/static/styles.css').text
    chat_js = client.get('/static/chat.js').text

    # HTML: composer wrapped in .chat-composer rounded container
    assert 'class="chat-composer"' in html or 'class="chat-composer ' in html
    assert 'id="chat-composer"' in html
    # Image previews live inside the composer container, above textarea
    composer_start = html.index('id="chat-composer"')
    previews_idx = html.index('id="chat-image-previews"', composer_start)
    textarea_idx = html.index('id="chat-input"', composer_start)
    bar_idx = html.index('chat-composer__bar', composer_start)
    assert previews_idx < textarea_idx < bar_idx

    # HTML: image button is icon button (no text label), send button is icon (no "Send" text)
    assert 'chat-composer__icon-btn' in html
    assert 'chat-composer__send' in html
    assert '<svg' in html
    # Old text labels gone
    assert '>图片<' not in html
    assert '>Send<' not in html

    # CSS: composer has rounded border, focus state
    assert '.chat-composer {' in css or '.chat-composer{' in css
    assert 'border-radius: 18px' in css
    assert '.chat-composer:focus-within' in css
    # CSS: icon buttons are circular
    assert '.chat-composer__icon-btn' in css
    assert '.chat-composer__send' in css
    assert 'border-radius: 50%' in css
    assert '.chat-composer__bar' in css
    assert 'gap: 8px' in css
    # CSS: send button uses primary color (standalone rule, not the combined selector)
    # Find the standalone `.chat-composer__send {` rule (preceded by `}` or start of line)
    import re as _re
    send_matches = list(_re.finditer(r'\.chat-composer__send\s*\{[^}]*\}', css))
    send_rules = [m.group(0) for m in send_matches]
    standalone = [r for r in send_rules if 'var(--color-primary)' in r]
    assert standalone, f"no .chat-composer__send rule with var(--color-primary): {send_rules}"
    assert any('var(--color-on-primary)' in r for r in standalone)
    # CSS: image previews styled as thumbnails with remove button
    assert '.chat-image-preview {' in css or '.chat-image-preview{' in css
    assert '.chat-image-preview__remove' in css
    # CSS: messages have Doubao-style rounded bubbles, images constrained
    assert '.msg img {' in css or '.msg img{' in css
    assert 'max-width' in css
    assert 'border-bottom-right-radius: 4px' in css  # user bubble tail
    assert 'border-bottom-left-radius: 4px' in css   # assistant bubble tail

    # JS: setSending no longer swaps textContent (icon button keeps SVG)
    setSending_start = chat_js.index('function setSending(next)')
    setSending_end = chat_js.index('function setStatusMessage', setSending_start)
    setSending_body = chat_js[setSending_start:setSending_end]
    assert 'textContent' not in setSending_body
    assert 'btn.disabled = next' in setSending_body


def test_chat_message_images_render_after_refresh(tmp_path):
    """Bug fix (PRD 03-prd-specs line 295): user-sent images must remain visible
    after AI replies. renderSessionMessages re-renders all messages from server
    data, including user messages whose content is a list with image_url parts."""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text

    # createMessageElement handles array content with image_url parts
    create_start = chat_js.index('function createMessageElement(message)')
    create_end = chat_js.index('function shouldRenderMessage(message)', create_start)
    create_body = chat_js[create_start:create_end]
    assert "Array.isArray(content)" in create_body
    assert "part.type === 'image_url'" in create_body
    assert "part.image_url.url" in create_body
    assert "document.createElement('img')" in create_body
    assert "imgEl.src = part.image_url.url" in create_body

    # renderSessionMessages clears and re-renders from server detail.messages
    render_start = chat_js.index('function renderSessionMessages(detail)')
    render_end = chat_js.index('async function loadSessions()', render_start)
    render_body = chat_js[render_start:render_end]
    assert 'clearNode(stack)' in render_body
    assert 'detail.messages' in render_body
    assert 'createMessageElement(message)' in render_body

    # refreshCurrentSession calls renderSessionMessages(detail) after stream ends
    refresh_start = chat_js.index('async function refreshCurrentSession()')
    refresh_end = chat_js.index('async function send()', refresh_start)
    refresh_body = chat_js[refresh_start:refresh_end]
    assert 'renderSessionMessages(detail)' in refresh_body


def test_chat_js_renders_summary_messages_specially(tmp_path):
    """is_summary 消息渲染为折叠的 details 卡片，标题"对话摘要"，剥离 [CONTEXT SUMMARY]: 前缀后展示正文。"""
    from app.domain.context import CONTEXT_SUMMARY_PREFIX

    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text

    create_start = chat_js.index('function createMessageElement(message)')
    create_end = chat_js.index('function shouldRenderMessage(message)', create_start)
    create_body = chat_js[create_start:create_end]
    assert 'message.is_summary' in create_body
    assert 'msg--summary' in create_body
    assert "createElement('details')" in create_body
    assert "createElement('summary')" in create_body
    assert "createElement('pre')" in create_body
    assert '对话摘要' in create_body
    assert CONTEXT_SUMMARY_PREFIX in create_body
    assert 'startsWith(prefix)' in create_body
    assert 'slice(prefix.length)' in create_body


def test_chat_js_keeps_summary_messages_renderable(tmp_path):
    """shouldRenderMessage 对 is_summary 消息直接返回 true，不被普通过滤逻辑排除。"""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text

    should_start = chat_js.index('function shouldRenderMessage(message)')
    should_end = chat_js.index('function groupToolMessages', should_start)
    should_body = chat_js[should_start:should_end]
    assert 'message.is_summary' in should_body
    assert 'return true' in should_body


def test_styles_css_contains_summary_message_styles(tmp_path):
    """styles.css 包含摘要卡片的折叠样式（msg--summary + details/summary/pre，黄底色）。"""
    client = _client(tmp_path)
    css = client.get('/static/styles.css').text
    assert '.msg--summary' in css
    assert '.msg--summary summary' in css
    assert '.msg--summary pre' in css
    assert '--color-warning-bg' in css


def test_chat_message_hover_reveals_timestamp_feishu_style(tmp_path):
    """PRD 03-prd-specs: Chat框鼠标 Hover 展示消息时间，样式、格式参考飞书消息。
    chat.js 读取服务端 created_at 并按 今天/昨天/今年/往年 分级格式化，写入 data-time；
    styles.css 在 .msg:hover 时通过 attr(data-time) 在气泡上方展示小号灰色时间。"""
    client = _client(tmp_path)
    chat_js = client.get('/static/chat.js').text
    css = client.get('/static/styles.css').text

    # JS: 时间格式化函数，消费服务端 created_at，分级输出（今天 HH:mm / 昨天 / 月日 / 年月日）
    assert 'function formatMessageTime' in chat_js
    assert 'formatMessageTime(message.created_at)' in chat_js
    assert "el.dataset.time = timeLabel" in chat_js
    assert "'昨天 '" in chat_js
    assert '86400000' in chat_js
    # 发送用户消息时携带客户端时间戳，Hover 即刻可见（流式结束后由 refresh 用服务端时间覆盖）
    assert "appendMessage('user', userContent, new Date().toISOString())" in chat_js

    # CSS: hover 时通过 attr(data-time) 展示小号灰色时间，不抢占气泡空间
    assert 'content: attr(data-time)' in css
    assert '.msg[data-time]::before' in css
    assert '.msg[data-time]:hover::before' in css
    assert 'pointer-events: none' in css
