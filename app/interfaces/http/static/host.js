(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});

  function root() {
    return ui.byId ? ui.byId('tab-executors-host') : document.getElementById('tab-executors-host');
  }

  function hostApi() {
    return (api && api.host) ? api.host : null;
  }

  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    // East-8 (Asia/Shanghai) regardless of browser timezone, same as sandbox.js
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

  function statusKind(code) {
    if (code === 'ok') return 'success';
    if (code && code !== 'host_bridge_not_checked') return 'danger';
    return 'warning';
  }

  function buildPanel(title, bodyId) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const titleSpan = document.createElement('span');
    titleSpan.textContent = title;
    header.append(titleSpan);
    const body = ui.el('div', 'panel-body');
    body.id = bodyId;
    panel.append(header, body);
    return panel;
  }

  function buildPanels(node) {
    node.replaceChildren();
    const statusPanel = buildPanel('执行器状态', 'host-status-card');
    const policyPanel = buildPanel('授权策略', 'host-policy-card');
    const historyPanel = buildPanel('执行历史', 'host-history-card');
    node.append(statusPanel, policyPanel, historyPanel);
    ui.renderLoading(statusPanel.querySelector('.panel-body'), '加载状态...');
    ui.renderLoading(policyPanel.querySelector('.panel-body'), '加载策略...');
    ui.renderLoading(historyPanel.querySelector('.panel-body'), '加载历史...');
    return {
      status: statusPanel.querySelector('.panel-body'),
      policy: policyPanel.querySelector('.panel-body'),
      history: historyPanel.querySelector('.panel-body'),
    };
  }

  function renderResult(node, result, renderFn, errMsg) {
    ui.clear(node);
    if (result.status === 'rejected') {
      ui.renderError(node, errMsg + (result.reason && result.reason.message ? result.reason.message : result.reason));
      return;
    }
    renderFn(node, result.value);
  }

  function policyItem(label, value) {
    // Mirror security page .policy-item/.policy-k/.policy-v so similar
    // key-value content shares the same FE style and font-size (--font-size-md).
    const item = ui.el('div', 'policy-item');
    const k = document.createElement('span'); k.className = 'policy-k'; k.textContent = label + '：';
    const v = document.createElement('span'); v.className = 'policy-v';
    v.textContent = value == null || value === '' ? '-' : String(value);
    item.append(k, v);
    return item;
  }

  function renderStatus(node, status) {
    ui.clear(node);
    if (!status) { ui.renderEmpty(node, '未加载状态'); return; }
    const grid = ui.el('div', 'policy-cfg');
    const rows = [
      ['Bridge 健康', status.health_code || '-'],
      ['Policy 版本', status.policy_version || '-'],
      ['加载时间', formatTime(status.policy_loaded_at)],
      ['内容摘要', status.policy_content_digest || '-'],
      ['加载错误', status.policy_last_error || '-'],
    ];
    rows.forEach(([k, v]) => grid.appendChild(policyItem(k, v)));
    if (status.limits_summary) {
      const L = status.limits_summary;
      grid.appendChild(policyItem(
        '资源上限',
        `超时${L.default_timeout_seconds}s / 并发${L.max_concurrency} / stdout ${L.max_stdout_bytes}B / stderr ${L.max_stderr_bytes}B`,
      ));
    }
    node.appendChild(grid);
  }

  function renderPolicy(node, policy) {
    ui.clear(node);
    if (!policy || !policy.enabled) {
      ui.renderEmpty(node, policy && policy.policy_last_error ? '策略未加载: ' + policy.policy_last_error : '策略未加载');
      return;
    }
    // 资源限制 - mirror security page .policy-cfg k:v grid (相似功能相同 FE 样式)
    const limitsTitle = ui.el('div', 'host-subsection-title'); limitsTitle.textContent = '资源限制'; node.appendChild(limitsTitle);
    const limitsGrid = ui.el('div', 'policy-cfg');
    const L = policy.limits || {};
    [
      ['默认超时', L.default_timeout_seconds],
      ['最大超时', L.max_timeout_seconds],
      ['最大stdout', L.max_stdout_bytes],
      ['最大stderr', L.max_stderr_bytes],
      ['最大参数数', L.max_args],
      ['最大参数长度', L.max_arg_length],
      ['最大并发', L.max_concurrency],
    ].forEach(([k, v]) => limitsGrid.appendChild(policyItem(k, v)));
    node.appendChild(limitsGrid);
    node.appendChild(renderRulesTable(
      '命令授权 (command_rules)',
      ['rule_id', 'executable', '参数规则'],
      (policy.command_rules || []).map((r) => [r.rule_id, r.executable, (r.positional_args || []).join(', ')]),
      '无命令授权',
    ));
    node.appendChild(renderRulesTable(
      'Skill脚本授权 (skill_script_rules)',
      ['rule_id', 'skill', 'script', 'sha256', '参数规则'],
      (policy.skill_script_rules || []).map((r) => [r.rule_id, r.skill_name, r.script_relative_path, r.sha256, (r.positional_args || []).join(', ')]),
      '无 Skill 脚本授权',
    ));
  }

  function renderRulesTable(title, headers, rows, emptyMsg) {
    const wrap = ui.el('div', 'host-rules-section');
    const t = ui.el('div', 'host-subsection-title'); t.textContent = title; wrap.appendChild(t);
    const table = document.createElement('table'); table.className = 'document-table host-rules-table';
    const thead = document.createElement('thead'); const hr = document.createElement('tr');
    headers.forEach((label) => { const th = document.createElement('th'); th.textContent = label; hr.appendChild(th); });
    thead.appendChild(hr); table.appendChild(thead);
    const tbody = document.createElement('tbody');
    if (!rows.length) {
      const tr = document.createElement('tr'); const td = document.createElement('td');
      td.textContent = emptyMsg; td.colSpan = headers.length; tr.appendChild(td); tbody.appendChild(tr);
    } else {
      rows.forEach((r) => { const tr = document.createElement('tr'); r.forEach((v) => appendCell(tr, v)); tbody.appendChild(tr); });
    }
    table.appendChild(tbody); wrap.appendChild(table);
    return wrap;
  }

  function renderHistory(node, history) {
    ui.clear(node);
    const items = history || [];
    if (!items.length) { ui.renderEmpty(node, '无执行历史'); return; }
    const table = document.createElement('table'); table.className = 'document-table host-history-table';
    const thead = document.createElement('thead'); const hr = document.createElement('tr');
    ['时间', 'Session', '目标类型', '目标', '状态', '耗时(ms)', '操作'].forEach((label) => {
      const th = document.createElement('th'); th.textContent = label; hr.appendChild(th);
    });
    thead.appendChild(hr); table.appendChild(thead);
    const tbody = document.createElement('tbody');
    items.forEach((it) => {
      const tr = document.createElement('tr');
      appendCell(tr, formatTime(it.created_at));
      appendCell(tr, it.session_id);
      appendCell(tr, it.target_type);
      appendCell(tr, it.target);
      appendBadgeCell(tr, it.status || '-', it.status === 'success' ? 'success' : (it.status === 'error' || it.status === 'timeout' ? 'danger' : 'warning'));
      appendCell(tr, it.duration_ms);
      const actions = document.createElement('td'); actions.className = 'row-actions';
      const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'btn'; btn.textContent = '详情';
      btn.addEventListener('click', () => openDetailModal(it));
      actions.appendChild(btn); tr.appendChild(actions);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); node.appendChild(table);
  }

  function openDetailModal(it) {
    const existing = document.getElementById('host-detail-modal');
    if (existing) existing.remove();
    const backdrop = document.createElement('div'); backdrop.id = 'host-detail-modal'; backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section'); dialog.className = 'modal-dialog'; dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form'); form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const titleEl = document.createElement('h4'); titleEl.textContent = '执行详情: ' + (it.id || '');
    const closeBtn = document.createElement('button'); closeBtn.type = 'button'; closeBtn.className = 'modal-close'; closeBtn.textContent = '×'; closeBtn.setAttribute('aria-label', '关闭详情弹框');
    closeBtn.addEventListener('click', () => backdrop.remove());
    header.append(titleEl, closeBtn); form.appendChild(header);
    const body = ui.el('div', 'sandbox-detail-modal-body');
    // Only the 8 desensitized fields from the API; never stdout/stderr/signed_url/exception text.
    const fields = [
      ['目标类型', it.target_type],
      ['目标', it.target],
      ['参数', JSON.stringify(it.arguments || {})],
      ['状态', it.status],
      ['耗时(ms)', it.duration_ms],
      ['结果摘要', it.result_summary],
      ['时间', formatTime(it.created_at)],
      ['Session', it.session_id],
    ];
    fields.forEach(([k, v]) => {
      const line = ui.el('div', 'sandbox-detail-meta');
      line.textContent = `${k}: ${v == null || v === '' ? '-' : v}`;
      body.appendChild(line);
    });
    form.appendChild(body);
    const actions = ui.el('div', 'providers-form__actions');
    const close2 = document.createElement('button'); close2.type = 'button'; close2.className = 'btn'; close2.textContent = '关闭'; close2.addEventListener('click', () => backdrop.remove());
    actions.appendChild(close2); form.appendChild(actions);
    dialog.appendChild(form); backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) backdrop.remove(); });
    document.body.appendChild(backdrop); closeBtn.focus();
  }

  async function load() {
    const node = root();
    if (!node) return;
    const host = hostApi();
    if (!host) { node.replaceChildren(); ui.renderError(node, '本机执行器 API 未就绪'); return; }
    const panels = buildPanels(node);
    const results = await Promise.allSettled([
      host.getStatus(),
      host.getPolicy(),
      host.listHistory(),
    ]);
    const statusRes = results[0];
    // Only an explicit enabled:false shows the full-page unavailable placeholder.
    // A failed status request (404/network) is a deployment version mismatch, not "未启用".
    if (statusRes.status === 'fulfilled' && statusRes.value && statusRes.value.enabled === false) {
      node.replaceChildren();
      const ph = ui.el('div', 'host-unavailable');
      ph.textContent = '本机执行器未启用' + (statusRes.value.health_code ? `（${statusRes.value.health_code}）` : '');
      node.appendChild(ph);
      return;
    }
    renderResult(panels.status, statusRes, renderStatus, '加载状态失败（部署版本可能不匹配）: ');
    renderResult(panels.policy, results[1], renderPolicy, '加载策略失败: ');
    renderResult(panels.history, results[2], renderHistory, '加载历史失败: ');
  }

  namespace.host = { init: load, refresh: load, load: load };
  global.NAGENT = namespace;
}(window));
