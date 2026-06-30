(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  let state = { config: null, active: [], released: [], history: [] };

  function root() {
    return ui.byId ? ui.byId('tab-sandbox') : document.getElementById('tab-sandbox');
  }

  function sandboxApi() {
    return (api && api.sandbox) ? api.sandbox : null;
  }

  function shortHash(hash) {
    const s = String(hash || '');
    return s.length > 8 ? `${s.slice(0, 8)}…` : s;
  }

  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    // East-8 (Asia/Shanghai) regardless of browser timezone
    const tz = new Date(d.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${tz.getUTCFullYear()}-${pad(tz.getUTCMonth() + 1)}-${pad(tz.getUTCDate())} ${pad(tz.getUTCHours())}:${pad(tz.getUTCMinutes())}:${pad(tz.getUTCSeconds())}`;
  }

  function appendCell(row, value) {
    const td = document.createElement('td');
    td.textContent = value == null || value === '' ? '-' : String(value);
    row.appendChild(td);
    return td;
  }

  function appendBadgeCell(row, text, kind) {
    const td = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'badge badge--' + kind;
    badge.textContent = text;
    td.appendChild(badge);
    row.appendChild(td);
  }

  function button(label, className, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className || 'btn';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
  }

  function statusBadge(status) {
    if (status === 'success') return 'success';
    if (status === 'timeout' || status === 'error') return 'danger';
    return 'warning';
  }

  function containerStatusBadge(status) {
    if (status === 'running') return 'success';
    if (status === 'local') return 'warning';
    if (!status) return 'warning';
    return 'warning';
  }

  function releaseReasonLabel(reason) {
    if (reason === 'idle') return '空闲到期';
    if (reason === 'manual') return '手动释放';
    if (reason === 'session') return '会话删除';
    return reason || '-';
  }

  function releaseReasonBadge(reason) {
    if (reason === 'manual') return 'success';
    if (reason === 'session') return 'warning';
    return 'warning';
  }

  function closeCodeModal() {
    const modal = document.getElementById('sandbox-code-modal');
    if (modal) modal.remove();
  }

  function openCodeModal(title, code) {
    closeCodeModal();
    const backdrop = document.createElement('div');
    backdrop.id = 'sandbox-code-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const titleEl = document.createElement('h4');
    titleEl.textContent = title;
    const closeBtn = button('×', 'modal-close', closeCodeModal);
    closeBtn.setAttribute('aria-label', '关闭代码弹框');
    header.append(titleEl, closeBtn);
    form.appendChild(header);

    const label = document.createElement('label');
    label.textContent = '代码';
    const textarea = document.createElement('textarea');
    textarea.value = code || '(无代码内容)';
    textarea.readOnly = true;
    textarea.rows = 18;
    label.appendChild(textarea);
    form.appendChild(label);

    const actions = ui.el('div', 'providers-form__actions');
    actions.append(button('关闭', 'btn', closeCodeModal));
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeCodeModal();
    });
    document.body.appendChild(backdrop);
    closeBtn.focus();
  }

  function closeHistoryDetailModal() {
    const modal = document.getElementById('sandbox-history-detail-modal');
    if (modal) modal.remove();
  }

  function openHistoryDetailModal(it) {
    closeHistoryDetailModal();
    const backdrop = document.createElement('div');
    backdrop.id = 'sandbox-history-detail-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const titleEl = document.createElement('h4');
    titleEl.textContent = '执行详情: ' + (it.id || '');
    const closeBtn = button('×', 'modal-close', closeHistoryDetailModal);
    closeBtn.setAttribute('aria-label', '关闭详情弹框');
    header.append(titleEl, closeBtn);
    form.appendChild(header);

    const body = ui.el('div', 'sandbox-detail-modal-body');
    body.appendChild(buildHistoryDetail(it));
    form.appendChild(body);

    const actions = ui.el('div', 'providers-form__actions');
    actions.append(button('关闭', 'btn', closeHistoryDetailModal));
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeHistoryDetailModal();
    });
    document.body.appendChild(backdrop);
    closeBtn.focus();
  }

  function buildPage(node) {
    node.append(
      buildConfigPanel(),
      buildActivePanel(),
      buildReleasedPanel(),
      buildHistoryPanel(),
    );
  }

  function buildPanel(title, bodyId, onRefresh) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const titleSpan = document.createElement('span');
    titleSpan.textContent = title;
    const actions = ui.el('span', 'panel-actions');
    actions.append(button('刷新', 'btn', onRefresh));
    header.append(titleSpan, actions);
    const body = ui.el('div', 'panel-body');
    body.id = bodyId;
    panel.append(header, body);
    return panel;
  }

  function buildConfigPanel() {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const titleSpan = document.createElement('span');
    titleSpan.textContent = '配置';
    const actions = ui.el('span', 'panel-actions');
    actions.append(button('刷新', 'btn', refreshConfig));
    header.append(titleSpan, actions);
    const body = ui.el('div', 'panel-body');
    body.id = 'sandbox-config-card';
    panel.append(header, body);
    renderConfigTable(body);
    return panel;
  }

  function buildActivePanel() {
    const panel = buildPanel('活跃沙盒', 'sandbox-active-card', refreshActive);
    renderActiveTable(panel.querySelector('.panel-body'));
    return panel;
  }

  function buildReleasedPanel() {
    const panel = buildPanel('废弃沙盒', 'sandbox-released-card', refreshReleased);
    renderReleasedTable(panel.querySelector('.panel-body'));
    return panel;
  }

  function buildHistoryPanel() {
    const panel = buildPanel('执行历史', 'sandbox-history-card', refreshHistory);
    renderHistoryTable(panel.querySelector('.panel-body'));
    return panel;
  }

  function renderConfigTable(node) {
    ui.clear(node);
    const cfg = state.config;
    if (!cfg) {
      ui.renderEmpty(node, '未加载配置');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table sandbox-config-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['类型', '启用', '超时(秒)', '最大工具调用', '空闲回收(秒)', '回调工具'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    (cfg.rows || []).forEach((r) => {
      const isDocker = r.sandbox_type === 'docker';
      const tr = document.createElement('tr');
      appendCell(tr, r.sandbox_type);
      appendBadgeCell(tr, r.enabled ? '是' : '否', r.enabled ? 'success' : 'warning');
      appendCell(tr, r.timeout_seconds);
      appendCell(tr, r.max_tool_calls);
      appendCell(tr, isDocker ? r.idle_seconds : '-');
      appendCell(tr, (r.callback_tools || []).join(', '));
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function renderActiveTable(node) {
    ui.clear(node);
    const items = state.active || [];
    if (!items.length) {
      ui.renderEmpty(node, '无活跃沙盒');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table sandbox-active-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['Session', '类型', '沙盒标识', '空闲(秒)', '容器状态', '最后使用', '操作'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    items.forEach((it) => {
      const tr = document.createElement('tr');
      appendCell(tr, it.session_id);
      appendCell(tr, it.sandbox_type);
      appendCell(tr, it.sandbox_id || '-');
      appendCell(tr, it.idle_seconds);
      appendBadgeCell(tr, it.container_status || '-', containerStatusBadge(it.container_status));
      appendCell(tr, formatTime(it.last_used_at));
      const actions = document.createElement('td');
      actions.className = 'row-actions';
      actions.append(button('释放', 'btn', () => releaseSandbox(it.session_id)));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function renderReleasedTable(node) {
    ui.clear(node);
    const items = state.released || [];
    if (!items.length) {
      ui.renderEmpty(node, '无废弃沙盒');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table sandbox-released-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['Session', '类型', '沙盒标识', '创建时间', '废弃时间', '原因'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    items.forEach((it) => {
      const tr = document.createElement('tr');
      appendCell(tr, it.session_id);
      appendCell(tr, it.sandbox_type);
      appendCell(tr, it.sandbox_id || '-');
      appendCell(tr, formatTime(it.created_at));
      appendCell(tr, formatTime(it.released_at));
      appendBadgeCell(tr, releaseReasonLabel(it.reason), releaseReasonBadge(it.reason));
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function renderHistoryTable(node) {
    ui.clear(node);
    const items = state.history || [];
    if (!items.length) {
      ui.renderEmpty(node, '无执行历史');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table sandbox-history-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['时间', 'Session', 'code_hash', '状态', '耗时(ms)', '授权工具', '操作'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    items.forEach((it) => {
      const result = it.result || {};
      const args = it.arguments || {};
      const authorized = (result.authorized_callback_tools || []).join(', ');
      const tr = document.createElement('tr');
      appendCell(tr, formatTime(it.created_at));
      appendCell(tr, it.session_id);
      appendCell(tr, shortHash(args.code_hash || ''));
      appendBadgeCell(tr, it.status || '-', statusBadge(it.status));
      appendCell(tr, it.duration_ms);
      appendCell(tr, authorized);
      const actions = document.createElement('td');
      actions.className = 'row-actions';
      actions.append(button('删除', 'btn', () => deleteHistory(it.id)));
      actions.append(button('详情', 'btn', () => openHistoryDetailModal(it)));
      tr.appendChild(actions);
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function buildHistoryDetail(it) {
    const result = it.result || {};
    const args = it.arguments || {};
    const wrap = document.createElement('div');
    wrap.className = 'sandbox-history-detail';

    wrap.appendChild(buildOutputSection('代码', args.code, 4000));

    const chainSection = document.createElement('div');
    chainSection.className = 'sandbox-detail-section';
    const chainTitle = document.createElement('div');
    chainTitle.className = 'sandbox-detail-section__title';
    const log = result.tool_call_log || [];
    chainTitle.textContent = `执行链路 (callback 调用 ${result.tool_calls_made || 0} 次)`;
    chainSection.appendChild(chainTitle);
    if (log.length) {
      const logTable = document.createElement('table');
      logTable.className = 'document-table sandbox-callback-log-table';
      const logHead = document.createElement('thead');
      const logHr = document.createElement('tr');
      ['序号', '工具', '状态', '错误'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        logHr.appendChild(th);
      });
      logHead.appendChild(logHr);
      const logBody = document.createElement('tbody');
      log.forEach((entry, i) => {
        const ltr = document.createElement('tr');
        appendCell(ltr, String(i + 1));
        appendCell(ltr, entry.name || '-');
        const st = String(entry.status || '-');
        appendBadgeCell(ltr, st, st === 'ok' || st === 'success' ? 'success' : 'danger');
        appendCell(ltr, entry.error || '-');
        logBody.appendChild(ltr);
      });
      logTable.append(logHead, logBody);
      chainSection.appendChild(logTable);
    } else {
      const empty = document.createElement('div');
      empty.className = 'muted';
      empty.textContent = '无 callback 调用';
      chainSection.appendChild(empty);
    }
    wrap.appendChild(chainSection);

    wrap.appendChild(buildOutputSection('stdout', result.stdout, 2000));
    wrap.appendChild(buildOutputSection('stderr', result.stderr, 1000));

    const metaSection = document.createElement('div');
    metaSection.className = 'sandbox-detail-section';
    const metaList = document.createElement('div');
    metaList.className = 'sandbox-detail-meta';
    const metaItems = [
      `returncode: ${result.returncode ?? '-'}`,
      `duration: ${result.duration_seconds ?? 0}s`,
      `tool_calls_made: ${result.tool_calls_made ?? 0}`,
    ];
    metaList.textContent = metaItems.join('  ·  ');
    metaSection.appendChild(metaList);
    wrap.appendChild(metaSection);

    return wrap;
  }

  function buildOutputSection(label, text, max) {
    const section = document.createElement('div');
    section.className = 'sandbox-detail-section';
    const title = document.createElement('div');
    title.className = 'sandbox-detail-section__title';
    const content = String(text || '');
    title.textContent = `${label} (${content.length} bytes)`;
    section.appendChild(title);
    const pre = document.createElement('pre');
    pre.className = 'sandbox-detail-output';
    pre.textContent = content.slice(0, max) || '(空)';
    section.appendChild(pre);
    if (content.length > max) {
      const more = button(`查看完整 ${label}`, 'btn', () => openCodeModal(label, content));
      section.appendChild(more);
    }
    return section;
  }

  async function releaseSandbox(sessionId) {
    if (!window.confirm(`确认释放沙盒 ${sessionId}？容器将被销毁，scratch 被清理。`)) return;
    try {
      await sandboxApi().releaseSandbox(sessionId);
      await load();
    } catch (err) {
      window.alert(`释放失败：${err && err.message ? err.message : err}`);
    }
  }

  async function deleteHistory(toolCallId) {
    if (!toolCallId) {
      window.alert('缺少记录ID');
      return;
    }
    if (!window.confirm(`确认删除执行历史 ${toolCallId}？`)) return;
    try {
      const res = await sandboxApi().deleteHistory(toolCallId);
      if (res && res.ok === false) {
        window.alert(`删除失败：${res.error || '未知错误'}`);
        return;
      }
      await load();
    } catch (err) {
      window.alert(`删除失败：${err && err.message ? err.message : err}`);
    }
  }

  async function refreshPanel(bodyId, fetchFn, stateKey, renderFn, loadingText, errorText) {
    const body = document.getElementById(bodyId);
    if (!body) return;
    const sbx = sandboxApi();
    if (!sbx) return;
    ui.clear(body);
    ui.renderLoading(body, loadingText);
    try {
      const data = await fetchFn(sbx);
      state[stateKey] = data;
      renderFn(body);
    } catch (err) {
      ui.clear(body);
      ui.renderError(body, errorText + ': ' + (err && err.message ? err.message : err));
    }
  }

  function refreshConfig() {
    return refreshPanel('sandbox-config-card', (s) => s.getConfig(), 'config', renderConfigTable, '加载配置...', '加载配置失败');
  }
  function refreshActive() {
    return refreshPanel('sandbox-active-card', (s) => s.listActive(), 'active', renderActiveTable, '加载活跃沙盒...', '加载活跃沙盒失败');
  }
  function refreshReleased() {
    return refreshPanel('sandbox-released-card', (s) => s.listReleased(), 'released', renderReleasedTable, '加载废弃沙盒...', '加载废弃沙盒失败');
  }
  function refreshHistory() {
    return refreshPanel('sandbox-history-card', (s) => s.listHistory(), 'history', renderHistoryTable, '加载执行历史...', '加载执行历史失败');
  }

  async function load() {
    const node = root();
    if (!node) return;
    const sbx = sandboxApi();
    if (!sbx) return;
    node.replaceChildren();
    ui.renderLoading(node, '加载沙盒数据...');
    try {
      const [config, active, released, history] = await Promise.all([
        sbx.getConfig(),
        sbx.listActive(),
        sbx.listReleased(),
        sbx.listHistory(),
      ]);
      state = { config, active, released, history };
      node.replaceChildren();
      buildPage(node);
    } catch (err) {
      node.replaceChildren();
      ui.renderError(node, '加载沙盒数据失败: ' + (err && err.message ? err.message : err));
    }
  }

  namespace.sandbox = { init: load, refresh: load, load: load };
  global.NAGENT = namespace;
}(window));
