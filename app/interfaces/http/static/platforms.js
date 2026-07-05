(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;
  const state = {
    platforms: [],
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

  async function refresh() {
    const root = getListRoot();
    if (!root) return;
    state.loading = true;
    state.error = '';
    render();
    try {
      const payload = await api.listPlatforms();
      state.platforms = payload.platforms || [];
    } catch (error) {
      state.error = error.message || 'platforms_load_failed';
    } finally {
      state.loading = false;
      render();
    }
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
    button.textContent = '详情';
    button.addEventListener('click', () => openDetailModal(platform.platform));
    actions.appendChild(button);
    row.appendChild(actions);
    return row;
  }

  function formatConfig(config) {
    const entries = Object.entries(config || {});
    if (!entries.length) return '-';
    return entries.map(([key, value]) => `${key}: ${value}`).join(' / ');
  }

  function closeDetailModal() {
    const existing = document.getElementById('platforms-detail-modal');
    if (existing) existing.remove();
  }

  function field(form, labelText, value) {
    const label = document.createElement('label');
    const span = document.createElement('span');
    span.textContent = labelText;
    const input = document.createElement('input');
    input.type = 'text';
    input.value = value == null || value === '' ? '-' : String(value);
    input.disabled = true;
    label.append(span, input);
    form.appendChild(label);
    return input;
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

  async function openDetailModal(platformId) {
    closeDetailModal();
    const platform = state.platforms.find((p) => p.platform === platformId);

    const backdrop = document.createElement('div');
    backdrop.id = 'platforms-detail-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = document.createElement('div');
    header.className = 'modal-header';
    const title = document.createElement('h4');
    title.textContent = '平台详情: ' + (platform ? text(platform.display_name, platform.platform) : platformId);
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'modal-close';
    close.setAttribute('aria-label', '关闭详情弹框');
    close.textContent = '×';
    close.addEventListener('click', closeDetailModal);
    header.append(title, close);
    form.appendChild(header);

    const loading = document.createElement('div');
    loading.textContent = '加载中...';
    form.appendChild(loading);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeDetailModal();
    });
    document.body.appendChild(backdrop);
    close.focus();

    try {
      const [detail, sessions] = await Promise.all([
        api.getPlatform(platformId),
        api.listPlatformSessions(platformId, 20, 0),
      ]);
      form.textContent = '';
      form.appendChild(header);

      field(form, '平台', detail.platform);
      field(form, '显示名', detail.display_name);
      field(form, '类型', detail.kind);
      field(form, '状态', detail.status);
      field(form, '错误信息', detail.error_message);
      field(form, '会话数', detail.session_count);
      field(form, '总会话', detail.total_sessions);
      field(form, '活跃会话', detail.active_sessions);
      field(form, '最近活跃', formatDate(detail.last_active_at));
      field(form, '配置', formatConfig(detail.config_summary));

      const sessionsLabel = document.createElement('label');
      const sessionsSpan = document.createElement('span');
      sessionsSpan.textContent = '会话列表';
      sessionsLabel.appendChild(sessionsSpan);
      const sessionsWrap = document.createElement('div');
      sessionsWrap.style.maxHeight = '240px';
      sessionsWrap.style.overflowY = 'auto';
      sessionsWrap.appendChild(renderSessionTable(sessions.items || []));
      sessionsLabel.appendChild(sessionsWrap);
      form.appendChild(sessionsLabel);

      const actions = document.createElement('div');
      actions.className = 'providers-form__actions';
      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'btn';
      closeBtn.textContent = '关闭';
      closeBtn.addEventListener('click', closeDetailModal);
      actions.appendChild(closeBtn);
      form.appendChild(actions);
    } catch (err) {
      form.textContent = '';
      form.appendChild(header);
      const error = document.createElement('div');
      error.className = 'badge badge--danger';
      error.textContent = '加载失败: ' + (err && err.message ? err.message : err);
      form.appendChild(error);
      const actions = document.createElement('div');
      actions.className = 'providers-form__actions';
      const closeBtn = document.createElement('button');
      closeBtn.type = 'button';
      closeBtn.className = 'btn';
      closeBtn.textContent = '关闭';
      closeBtn.addEventListener('click', closeDetailModal);
      actions.appendChild(closeBtn);
      form.appendChild(actions);
    }
  }

  function init() {
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.platforms = { init, refresh };
}(window));
