(function (global) {
  const namespace = global.NAGENT || {};
  const api = namespace.api;
  const state = {
    platforms: [],
    selected: '',
    detail: null,
    sessions: null,
    loading: false,
    error: '',
  };

  function text(value, fallback) {
    if (value === null || value === undefined || value === '') return fallback || '-';
    return String(value);
  }

  function formatDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString();
  }

  function clear(node) {
    if (node) node.textContent = '';
  }

  function appendText(parent, tag, content, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = content;
    parent.appendChild(node);
    return node;
  }

  function badge(value, kind) {
    const node = document.createElement('span');
    node.className = `badge${kind ? ` badge--${kind}` : ''}`;
    node.textContent = value;
    return node;
  }

  function statusKind(value) {
    if (value === 'connected' || value === 'configured') return 'success';
    if (value === 'disconnected') return 'warning';
    if (value === 'fatal') return 'danger';
    return '';
  }

  function getListRoot() {
    return document.getElementById('platforms-list');
  }

  function getSessionsRoot() {
    return document.getElementById('platforms-sessions');
  }

  async function refresh() {
    const root = getListRoot();
    if (!root) return;
    state.loading = true;
    state.error = '';
    render();
    try {
      const payload = await api.listPlatforms(true);
      state.platforms = payload.platforms || [];
    } catch (error) {
      state.error = error.message || 'platforms_load_failed';
    } finally {
      state.loading = false;
      render();
    }
  }

  async function selectPlatform(platform) {
    state.selected = platform;
    state.detail = null;
    state.sessions = null;
    renderSessions();
    try {
      state.detail = await api.getPlatform(platform);
      state.sessions = await api.listPlatformSessions(platform, 20, 0);
    } catch (error) {
      state.error = error.message || 'platform_load_failed';
    }
    render();
    renderSessions();
  }

  function render() {
    const root = getListRoot();
    if (!root) return;
    clear(root);
    if (state.error) appendText(root, 'div', state.error, 'error-state');
    if (state.loading) {
      appendText(root, 'div', '加载中...', 'loading-state');
      return;
    }
    if (!state.platforms.length) {
      appendText(root, 'div', '暂无平台', 'empty-state');
      return;
    }
    root.appendChild(renderPlatformTable());
  }

  function renderPlatformTable() {
    const table = document.createElement('table');
    table.className = 'document-table';
    const thead = document.createElement('thead');
    const header = document.createElement('tr');
    ['平台', '类型', '状态', '会话数', '最近活跃', '配置', '操作'].forEach((label) => appendText(header, 'th', label));
    thead.appendChild(header);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    state.platforms.forEach((platform) => tbody.appendChild(renderPlatformRow(platform)));
    table.appendChild(tbody);
    return table;
  }

  function renderPlatformRow(platform) {
    const row = document.createElement('tr');
    const title = document.createElement('td');
    appendText(title, 'strong', text(platform.display_name, platform.platform));
    appendText(title, 'div', text(platform.platform), 'muted');
    row.appendChild(title);
    appendText(row, 'td', text(platform.kind));
    const status = document.createElement('td');
    status.appendChild(badge(text(platform.status), statusKind(platform.status)));
    if (platform.error_message) appendText(status, 'div', platform.error_message, 'muted');
    row.appendChild(status);
    appendText(row, 'td', text(platform.session_count, '0'));
    appendText(row, 'td', formatDate(platform.last_active_at));
    appendText(row, 'td', formatConfig(platform.config_summary));
    const actions = document.createElement('td');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn';
    button.textContent = '查看';
    button.addEventListener('click', () => selectPlatform(platform.platform));
    actions.appendChild(button);
    row.appendChild(actions);
    return row;
  }

  function formatConfig(config) {
    const entries = Object.entries(config || {});
    if (!entries.length) return '-';
    return entries.map(([key, value]) => `${key}: ${value}`).join(' / ');
  }

  function renderSessions() {
    const root = getSessionsRoot();
    if (!root) return;
    clear(root);
    if (!state.selected) {
      appendText(root, 'div', '点击平台查看会话', 'empty-state');
      return;
    }
    appendText(root, 'h3', state.selected);
    if (!state.detail || !state.sessions) {
      appendText(root, 'div', '加载中...', 'loading-state');
      return;
    }
    const detail = document.createElement('div');
    detail.className = 'scheduled-stats';
    [['总会话', state.detail.total_sessions], ['活跃会话', state.detail.active_sessions]].forEach(([label, value]) => {
      const card = document.createElement('div');
      card.className = 'stat-card';
      appendText(card, 'div', label, 'label');
      appendText(card, 'div', value, 'value');
      detail.appendChild(card);
    });
    root.appendChild(detail);
    root.appendChild(renderSessionTable(state.sessions.items || []));
  }

  function renderSessionTable(items) {
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'empty-state';
      empty.textContent = '暂无会话';
      return empty;
    }
    const table = document.createElement('table');
    table.className = 'document-table';
    const thead = document.createElement('thead');
    const header = document.createElement('tr');
    ['会话', '线程', '显示名', '当前会话', '更新时间'].forEach((label) => appendText(header, 'th', label));
    thead.appendChild(header);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    items.forEach((item) => {
      const row = document.createElement('tr');
      appendText(row, 'td', text(item.platform_session_id));
      appendText(row, 'td', text(item.thread_id));
      appendText(row, 'td', text(item.display_name));
      appendText(row, 'td', text(item.active_session_id));
      appendText(row, 'td', formatDate(item.updated_at));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    return table;
  }

  function init() {
    const refreshButton = document.getElementById('platforms-refresh');
    if (refreshButton) refreshButton.addEventListener('click', refresh);
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.platforms = { init, refresh };
}(window));
