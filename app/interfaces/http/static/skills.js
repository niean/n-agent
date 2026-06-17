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

  async function openDetail(name) {
    let data;
    try {
      data = await api.getSkill(name);
    } catch (err) {
      window.alert('获取详情失败: ' + (err && err.message ? err.message : err));
      return;
    }
    const drawer = ui.el('div', 'skill-drawer');
    const close = ui.el('button', 'btn');
    close.type = 'button';
    close.textContent = '关闭';
    close.addEventListener('click', () => drawer.remove());
    const heading = ui.el('h3');
    heading.textContent = (data && data.skill && data.skill.name) || name;
    const pre = ui.el('pre');
    pre.textContent = (data && data.content) || '';
    drawer.append(close, heading, pre);
    document.body.appendChild(drawer);
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
