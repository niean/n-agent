(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  let state = { tools: [], bases: [] };

  function root() {
    return ui.byId ? ui.byId('tab-tools-knowledge') : document.getElementById('tab-tools-knowledge');
  }

  function riskBadge(level) {
    if (level === 'dangerous') return 'danger';
    if (level === 'confirm') return 'warning';
    return 'success';
  }

  function statusBadge(status) {
    if (status === 'success') return 'success';
    if (status === 'failed') return 'danger';
    return 'warning';
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

  function buildPage(node) {
    const toolPanel = ui.el('section', 'status-panel');
    const toolHeader = ui.el('div', 'panel-header');
    const toolTitle = document.createElement('span');
    toolTitle.textContent = '知识工具';
    const toolActions = ui.el('span', 'panel-actions');
    toolActions.append(
      button('刷新描述', 'btn', refreshToolDescription),
      button('刷新', 'btn', load),
    );
    toolHeader.append(toolTitle, toolActions);
    const toolBody = ui.el('div', 'panel-body');
    toolBody.id = 'knowledge-tool-card';
    toolPanel.append(toolHeader, toolBody);
    node.appendChild(toolPanel);

    const kbPanel = ui.el('section', 'status-panel');
    const kbHeader = ui.el('div', 'panel-header');
    const kbTitle = document.createElement('span');
    kbTitle.textContent = '知识库管理';
    const actions = ui.el('span', 'panel-actions');
    actions.append(
      button('+ 新增 KB', 'btn', () => openKbForm()),
      button('刷新', 'btn', load),
    );
    kbHeader.append(kbTitle, actions);
    const kbBody = ui.el('div', 'panel-body');
    kbBody.id = 'knowledge-bases-list';
    kbPanel.append(kbHeader, kbBody);
    node.appendChild(kbPanel);

    renderToolTable(toolBody);
    renderKbTable(kbBody);
  }

  function renderToolTable(node) {
    ui.clear(node);
    const tools = (state.tools || []).filter((item) => item.name === 'search_knowledge');
    if (!tools.length) {
      ui.renderEmpty(node, '暂无知识工具');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table knowledge-tool-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['名称', '类型', '分组', '描述', '风险等级', '启用', 'Required'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    tools.forEach((tool) => {
      const tr = document.createElement('tr');
      appendCell(tr, tool.name);
      appendCell(tr, tool.source_type);
      appendCell(tr, tool.toolset);
      appendCell(tr, tool.description || '-');
      const risk = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = 'badge badge--' + riskBadge(tool.risk_level);
      badge.textContent = tool.risk_level || 'safe';
      risk.appendChild(badge);
      tr.appendChild(risk);
      appendCell(tr, tool.enabled ? '是' : '否');
      appendCell(tr, ((tool.input_schema || {}).required || []).join(', ') || '-');
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function renderKbTable(node) {
    ui.clear(node);
    if (!state.bases.length) {
      ui.renderEmpty(node, '暂无 KB，请新建 N-KB 或 Ragflow 后端实例');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table knowledge-bases-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['KB ID', '名称', '类型', 'Endpoint', 'Dataset', '密钥', '启用', '状态', '默认参数', '操作'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    state.bases.forEach((base) => tbody.appendChild(renderKbRow(base)));
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function renderKbRow(base) {
    const tr = document.createElement('tr');
    appendCell(tr, base.id);
    appendCell(tr, base.name);
    appendCell(tr, base.base_type);
    appendCell(tr, base.base_url);
    appendCell(tr, base.dataset_id);
    appendBadgeCell(tr, base.api_key_present ? '已配置' : '未配置', base.api_key_present ? 'success' : 'warning');
    appendBadgeCell(tr, base.enabled ? '启用' : '停用', base.enabled ? 'success' : 'warning');
    appendBadgeCell(tr, base.last_probe_status || 'unknown', statusBadge(base.last_probe_status));
    appendCell(tr, `top_k=${base.default_top_k == null ? '-' : base.default_top_k}, min_score=${base.default_min_score == null ? '-' : base.default_min_score}`);
    const actions = document.createElement('td');
    actions.className = 'row-actions';
    actions.append(
      button('编辑', 'btn', () => openKbForm(base)),
      button(base.enabled ? '停用' : '启用', 'btn', () => toggleKb(base)),
      button('Probe', 'btn', () => probeSaved(base)),
      button('删除', 'btn btn--danger', () => deleteKb(base)),
    );
    tr.appendChild(actions);
    return tr;
  }

  function closeKbForm() {
    const modal = document.getElementById('knowledge-kb-modal');
    if (modal) modal.remove();
  }

  function field(form, name, labelText, value, options) {
    const label = document.createElement('label');
    label.textContent = labelText;
    let input;
    if (options && options.type === 'select') {
      input = document.createElement('select');
      (options.items || []).forEach((item) => {
        const option = document.createElement('option');
        option.value = item.value;
        option.textContent = item.label;
        input.appendChild(option);
      });
    } else if (options && options.type === 'textarea') {
      input = document.createElement('textarea');
    } else {
      input = document.createElement('input');
      input.type = options && options.type ? options.type : 'text';
    }
    input.name = name;
    input.id = 'knowledge-' + name;
    if (value != null) input.value = value;
    if (options && options.placeholder) input.placeholder = options.placeholder;
    if (options && options.step) input.step = options.step;
    if (options && options.min != null) input.min = String(options.min);
    if (options && options.max != null) input.max = String(options.max);
    if (options && options.disabled) input.disabled = true;
    label.appendChild(input);
    form.appendChild(label);
    return input;
  }

  function checkbox(form, name, labelText, checked) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = name;
    input.id = 'knowledge-' + name;
    input.checked = !!checked;
    const span = document.createElement('span');
    span.textContent = labelText;
    label.append(input, span);
    form.appendChild(label);
    return input;
  }

  function openKbForm(base) {
    closeKbForm();
    const isEdit = !!base;
    const backdrop = document.createElement('div');
    backdrop.id = 'knowledge-kb-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog knowledge-modal';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form knowledge-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = isEdit ? '编辑 KB' : '新建 KB';
    const close = button('×', 'modal-close', closeKbForm);
    close.setAttribute('aria-label', '关闭 KB 表单');
    header.append(title, close);
    form.appendChild(header);

    field(form, 'kb_id', 'kb_id', base ? base.id : '', { disabled: isEdit, placeholder: 'engineering-docs' });
    field(form, 'name', 'name', base ? base.name : '');
    field(form, 'description', 'description', base ? base.description : '', { type: 'textarea' });
    field(form, 'base_type', 'base_type', base ? base.base_type : 'n_kb', { type: 'select', items: [
      { value: 'n_kb', label: 'N-KB' },
      { value: 'ragflow', label: 'Ragflow' },
    ] });
    field(form, 'base_url', 'base_url', base ? base.base_url : '', { placeholder: 'https://kb.example.com' });
    field(form, 'dataset_id', 'dataset_id', base ? base.dataset_id : '');
    field(form, 'api_key', 'api_key', '', { type: 'password', placeholder: isEdit ? '留空表示不修改密钥' : '可选' });
    field(form, 'default_top_k', 'default_top_k', base && base.default_top_k != null ? base.default_top_k : '', { type: 'number', min: 1 });
    field(form, 'default_min_score', 'default_min_score', base && base.default_min_score != null ? base.default_min_score : '', { type: 'number', min: 0, max: 1, step: '0.01' });
    checkbox(form, 'enabled', 'enabled', base ? base.enabled : true);

    const message = ui.el('div', 'providers-form__hint muted');
    message.textContent = 'api_key 不会在保存后显示；编辑时留空表示保持原密钥。';
    form.appendChild(message);
    const actions = ui.el('div', 'providers-form__actions');
    actions.append(
      button('Probe', 'btn', () => probeForm(form, message)),
      button('取消', 'btn', closeKbForm),
    );
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'btn btn--primary';
    submit.textContent = '保存';
    actions.appendChild(submit);
    form.appendChild(actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await submitForm(form, base, message);
    });
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeKbForm();
    });
    document.body.appendChild(backdrop);
  }

  function formPayload(form, includeEmptyKey) {
    const data = new FormData(form);
    const payload = {
      id: String(data.get('kb_id') || '').trim(),
      name: String(data.get('name') || '').trim(),
      description: String(data.get('description') || '').trim(),
      base_type: String(data.get('base_type') || 'n_kb'),
      base_url: String(data.get('base_url') || '').trim(),
      dataset_id: String(data.get('dataset_id') || '').trim(),
      enabled: data.get('enabled') === 'on',
    };
    const apiKey = String(data.get('api_key') || '');
    if (includeEmptyKey || apiKey) payload.api_key = apiKey;
    const topK = String(data.get('default_top_k') || '').trim();
    const minScore = String(data.get('default_min_score') || '').trim();
    if (topK) payload.default_top_k = Number(topK);
    if (minScore) payload.default_min_score = Number(minScore);
    return payload;
  }

  async function submitForm(form, base, message) {
    const isEdit = !!base;
    const payload = formPayload(form, !isEdit);
    try {
      if (isEdit) {
        delete payload.id;
        await api.updateKnowledgeBase(base.id, payload);
      } else {
        await api.createKnowledgeBase(payload);
      }
      message.className = 'providers-form__hint badge badge--success';
      message.textContent = '保存成功';
      closeKbForm();
      await load();
    } catch (err) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '保存失败: ' + (err && err.message ? err.message : err);
    }
  }

  async function probeForm(form, message) {
    const payload = formPayload(form, true);
    try {
      await api.probeKnowledgeBase(payload);
      message.className = 'providers-form__hint badge badge--success';
      message.textContent = 'Probe 成功';
    } catch (err) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = 'Probe 失败: ' + (err && err.message ? err.message : err);
    }
  }

  async function toggleKb(base) {
    await api.updateKnowledgeBase(base.id, { enabled: !base.enabled });
    await load();
  }

  async function probeSaved(base) {
    try {
      await api.probeSavedKnowledgeBase(base.id);
      await load();
    } catch (err) {
      window.alert('Probe 失败: ' + (err && err.message ? err.message : err));
    }
  }

  async function deleteKb(base) {
    if (!window.confirm('确认删除 KB ' + base.id + '？')) return;
    await api.deleteKnowledgeBase(base.id);
    await load();
  }

  async function refreshToolDescription() {
    await api.refreshKnowledgeTool();
    await load();
  }

  async function load() {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    ui.renderLoading(node, '加载知识库...');
    try {
      const results = await Promise.all([api.listTools(), api.listKnowledgeBases()]);
      state = {
        tools: (results[0] || []).filter((tool) => tool.source_type === 'knowledge'),
        bases: results[1] || [],
      };
      node.replaceChildren();
      buildPage(node);
    } catch (err) {
      node.replaceChildren();
      ui.renderError(node, '加载知识库失败: ' + (err && err.message ? err.message : err));
    }
  }

  namespace.knowledge = { init: load, refresh: load, load: load };
  global.NAGENT = namespace;
}(window));
