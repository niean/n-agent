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
    const actions = ui.el('span', 'panel-actions');
    const addBtn = ui.el('button', 'btn');
    addBtn.type = 'button';
    addBtn.textContent = '新增';
    addBtn.addEventListener('click', () => openForm(null));
    const refreshBtn = ui.el('button', 'btn');
    refreshBtn.type = 'button';
    refreshBtn.textContent = '扫描';
    refreshBtn.addEventListener('click', refresh);
    actions.append(addBtn, refreshBtn);
    header.append(title, actions);
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
    td.className = 'row-actions-cell';
    const group = ui.el('div', 'row-actions');
    const viewBtn = ui.el('button', 'btn');
    viewBtn.type = 'button';
    viewBtn.textContent = '查看';
    viewBtn.addEventListener('click', () => openDetail(s.name));
    const editBtn = ui.el('button', 'btn');
    editBtn.type = 'button';
    editBtn.textContent = '编辑';
    editBtn.addEventListener('click', () => openForm(s));
    const toggle = ui.el('button', 'btn');
    toggle.type = 'button';
    toggle.textContent = s.enabled ? '禁用' : '启用';
    toggle.addEventListener('click', () => toggleEnabled(s.name, !s.enabled));
    const del = ui.el('button', 'btn');
    del.type = 'button';
    del.textContent = '删除';
    del.addEventListener('click', () => deleteSkill(s.name));
    group.append(viewBtn, editBtn, toggle, del);
    td.appendChild(group);
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

  async function deleteSkill(name) {
    if (!window.confirm(`删除 Skill 元数据 ${name}？`)) return;
    try {
      await api.deleteSkill(name);
    } catch (err) {
      window.alert('删除失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  function closeDetail() {
    const existing = ui.byId ? ui.byId('skill-detail-modal') : document.getElementById('skill-detail-modal');
    if (existing) existing.remove();
  }

  function closeForm() {
    const existing = ui.byId ? ui.byId('skill-form-modal') : document.getElementById('skill-form-modal');
    if (existing) existing.remove();
  }

  function field(form, name, labelText, value, options) {
    const label = ui.el('label');
    const span = ui.el('span');
    span.textContent = labelText;
    let input;
    if (options && options.type === 'textarea') {
      input = ui.el('textarea');
      input.rows = options.rows || 4;
    } else if (options && options.type === 'select') {
      input = ui.el('select');
      (options.choices || []).forEach((choice) => {
        const option = ui.el('option');
        option.value = choice;
        option.textContent = choice;
        input.appendChild(option);
      });
    } else {
      input = ui.el('input');
      input.type = (options && options.type) || 'text';
    }
    input.name = name;
    input.value = value || '';
    if (options && options.disabled) input.disabled = true;
    if (options && options.placeholder) input.placeholder = options.placeholder;
    label.append(span, input);
    form.appendChild(label);
    return input;
  }

  function checkbox(form, name, labelText, checked) {
    const label = ui.el('label');
    const input = ui.el('input');
    input.type = 'checkbox';
    input.name = name;
    input.checked = !!checked;
    const span = ui.el('span');
    span.textContent = labelText;
    label.append(input, span);
    form.appendChild(label);
    return input;
  }

  function payloadFromForm(inputs) {
    const platforms = inputs.platforms.value.split(',').map((item) => item.trim()).filter(Boolean);
    let frontmatter = {};
    if (inputs.frontmatter.value.trim()) {
      frontmatter = JSON.parse(inputs.frontmatter.value);
    }
    return {
      name: inputs.name.value.trim(),
      relative_path: inputs.relativePath.value.trim(),
      description: inputs.description.value.trim(),
      platforms,
      enabled: inputs.enabled.checked,
      readiness: inputs.readiness.value,
      frontmatter,
    };
  }

  function openForm(skill) {
    closeForm();
    const isEdit = !!skill;
    const backdrop = ui.el('div', 'modal-backdrop');
    backdrop.id = 'skill-form-modal';
    const dialog = ui.el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = ui.el('form', 'providers-form');
    const header = ui.el('div', 'modal-header');
    const title = ui.el('h4');
    title.textContent = isEdit ? '编辑 Skill' : '新增 Skill';
    const close = ui.el('button', 'modal-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭 Skill 表单');
    close.textContent = '×';
    close.addEventListener('click', closeForm);
    header.append(title, close);
    form.appendChild(header);

    const inputs = {
      name: field(form, 'name', '名称', skill ? skill.name : '', { disabled: isEdit, placeholder: '唯一名称' }),
      relativePath: field(form, 'relative_path', '相对路径', skill ? skill.relative_path : '', { placeholder: 'example/SKILL.md' }),
      description: field(form, 'description', '描述', skill ? skill.description : ''),
      platforms: field(form, 'platforms', '平台', skill && Array.isArray(skill.platforms) ? skill.platforms.join(',') : '', { placeholder: 'linux,darwin' }),
      readiness: field(form, 'readiness', '就绪状态', skill ? skill.readiness : 'available', { type: 'select', choices: ['available', 'unsupported', 'setup_needed', 'scan_error'] }),
      enabled: checkbox(form, 'enabled', '启用', skill ? skill.enabled : true),
      frontmatter: field(form, 'frontmatter', 'Frontmatter JSON', skill && skill.frontmatter ? JSON.stringify(skill.frontmatter, null, 2) : '{}', { type: 'textarea', rows: 8 }),
    };
    const actions = ui.el('div', 'providers-form__actions');
    const cancel = ui.el('button', 'btn');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', closeForm);
    const save = ui.el('button', 'btn primary');
    save.type = 'submit';
    save.textContent = '保存';
    actions.append(cancel, save);
    form.appendChild(actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        const payload = payloadFromForm(inputs);
        if (isEdit) await api.updateSkill(skill.name, payload);
        else await api.createSkill(payload);
        closeForm();
        await load();
      } catch (err) {
        window.alert('保存失败: ' + (err && err.message ? err.message : err));
      }
    });
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeForm();
    });
    document.body.appendChild(backdrop);
    inputs.name.focus();
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
