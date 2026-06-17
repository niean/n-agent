(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});

  function root() {
    return ui.byId ? ui.byId('tab-tools-skill') : document.getElementById('tab-tools-skill');
  }

  let skills = [];

  function render() {
    const node = root();
    if (!node) return;
    node.replaceChildren();

    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = 'Skills';
    const refreshBtn = ui.el('button', 'btn');
    refreshBtn.type = 'button';
    refreshBtn.textContent = '重扫描';
    refreshBtn.addEventListener('click', refresh);
    header.append(title, refreshBtn);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    if (!skills.length) {
      ui.renderEmpty(body, '尚未发现 Skill；请检查 skills_root 配置或点击重扫描');
      panel.appendChild(body);
      node.appendChild(panel);
      return;
    }

    const table = ui.el('table', 'document-table');
    const thead = ui.el('thead');
    const trh = ui.el('tr');
    ['名称', '描述', '平台', '就绪状态', '启用', '扫描状态', '操作'].forEach((h) => {
      const th = ui.el('th');
      th.textContent = h;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);

    const tbody = ui.el('tbody');
    skills.forEach((s) => tbody.appendChild(renderRow(s)));
    table.appendChild(tbody);
    body.appendChild(table);
    panel.appendChild(body);
    node.appendChild(panel);
  }

  function renderRow(s) {
    const tr = ui.el('tr');
    ['name', 'description', 'platforms', 'readiness', 'enabled', 'last_scan_status'].forEach((key) => {
      const td = ui.el('td');
      const v = s[key];
      td.textContent = Array.isArray(v) ? v.join(',') : (v === null || v === undefined ? '' : String(v));
      tr.appendChild(td);
    });
    const td = ui.el('td');
    td.className = 'row-actions';
    const viewBtn = ui.el('button', 'btn');
    viewBtn.type = 'button';
    viewBtn.textContent = '查看';
    viewBtn.addEventListener('click', () => openDetail(s.name));
    const toggle = ui.el('button', 'btn');
    toggle.type = 'button';
    toggle.textContent = s.enabled ? '禁用' : '启用';
    toggle.addEventListener('click', () => toggleEnabled(s.name, !s.enabled));
    td.append(viewBtn, toggle);
    tr.appendChild(td);
    return tr;
  }

  async function refresh() {
    try {
      const res = await api.refreshSkills();
      if (res && res.warnings && res.warnings.length) {
        const lines = res.warnings.map((w) => (w.reason || 'warn') + ': ' + (w.relative_path || ''));
        window.alert('扫描完成，警告:\n' + lines.join('\n'));
      }
    } catch (err) {
      window.alert('扫描失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  async function toggleEnabled(name, enabled) {
    try {
      await api.setSkillEnabled(name, enabled);
    } catch (err) {
      window.alert('更新失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  function closeDetail() {
    const existing = ui.byId ? ui.byId('skill-detail-modal') : document.getElementById('skill-detail-modal');
    if (existing) existing.remove();
  }

  async function openDetail(name) {
    let data;
    try {
      data = await api.getSkill(name);
    } catch (err) {
      window.alert('获取详情失败: ' + (err && err.message ? err.message : err));
      return;
    }
    closeDetail();
    const backdrop = ui.el('div', 'modal-backdrop');
    backdrop.id = 'skill-detail-modal';
    backdrop.setAttribute('role', 'presentation');

    const dialog = ui.el('section', 'modal-dialog tools-schema-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'skill-detail-title');

    const content = ui.el('div', 'tools-schema-content');
    const header = ui.el('div', 'modal-header');
    const title = ui.el('h4');
    title.id = 'skill-detail-title';
    title.textContent = (data && data.skill && data.skill.name) || name;
    const close = ui.el('button', 'modal-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭 Skill 详情弹出框');
    close.textContent = '×';
    close.addEventListener('click', closeDetail);
    header.append(title, close);

    const pre = ui.el('pre');
    pre.textContent = (data && data.content) || '';
    content.append(header, pre);
    dialog.appendChild(content);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeDetail();
    });
    document.body.appendChild(backdrop);
    close.focus();
  }

  async function load() {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    const loading = ui.el('div');
    ui.renderLoading(loading);
    node.appendChild(loading);
    try {
      const data = await api.listSkills();
      skills = (data && Array.isArray(data.skills)) ? data.skills : [];
      render();
    } catch (err) {
      node.replaceChildren();
      ui.renderError(node, '加载 Skill 失败: ' + (err && err.message ? err.message : err));
    }
  }

  namespace.skills = { init: load, refresh: load, load: load };
  global.NAGENT = namespace;
}(window));
