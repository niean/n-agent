(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;
  const modal = namespace.modal;

  function flagText(value) {
    return value === true ? '✓' : value === false ? '-' : '-';
  }

  let editing = null;

  function fillForm(provider) {
    document.getElementById('provider-id').value = provider ? provider.id : '';
    document.getElementById('provider-name').value = provider ? provider.name : '';
    document.getElementById('provider-type').value = provider ? provider.provider_type : 'openai-compatible';
    document.getElementById('provider-base-url').value = provider ? provider.base_url : '';
    document.getElementById('provider-model').value = provider ? provider.model : '';
    document.getElementById('provider-api-key').value = '';
    const visionCheckbox = document.getElementById('provider-supports-vision');
    if (visionCheckbox) visionCheckbox.checked = provider ? !!provider.supports_vision : true;
    const title = document.getElementById('providers-form-title');
    if (title) title.textContent = provider ? `编辑 Provider: ${provider.name}` : '新增 Provider';
    const hint = document.getElementById('providers-form-hint');
    if (hint) {
      if (provider) {
        hint.textContent = `API Key 状态: ${provider.api_key_present ? '已配置（脱敏不可见）' : '未配置'}。留空=不变；输入空格=清空。`;
      } else {
        hint.textContent = '新增时 API Key 必填。';
      }
    }
  }

  function showForm(provider) {
    editing = provider || null;
    fillForm(provider);
    const modal = document.getElementById('providers-modal');
    if (modal) modal.hidden = false;
    const nameInput = document.getElementById('provider-name');
    if (nameInput) nameInput.focus();
  }

  function hideForm() {
    editing = null;
    const modal = document.getElementById('providers-modal');
    if (modal) modal.hidden = true;
  }

  function makeButton(label, variant, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = variant ? `btn btn--${variant}` : 'btn';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
  }

  function renderProvidersTable(parent, providers) {
    if (!providers.length) { ui.renderEmpty(parent, '暂无 Provider，点击「新增」创建'); return; }
    const table = document.createElement('table');
    table.className = 'document-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['名称', '类型', 'Base URL', 'Model', 'Key', 'Vision', '启用', '操作'].forEach((label) => {
      const th = document.createElement('th'); th.textContent = label; headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    providers.forEach((p) => {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td'); td1.textContent = p.name || '-';
      const td2 = document.createElement('td'); td2.textContent = p.provider_type || '-';
      const td3 = document.createElement('td'); td3.textContent = p.base_url || '-';
      const td4 = document.createElement('td'); td4.textContent = p.model || '-';
      const td5 = document.createElement('td'); td5.textContent = p.api_key_present ? '已配置' : '未配置';
      const tdVision = document.createElement('td'); tdVision.textContent = p.supports_vision ? '✓' : '-';
      const td6 = document.createElement('td');
      const activeBadge = document.createElement('span');
      activeBadge.className = `badge badge--${p.is_active ? 'success' : 'warning'}`;
      activeBadge.textContent = p.is_active ? '启用' : '停用';
      td6.appendChild(activeBadge);
      const actions = document.createElement('td');
      actions.className = 'row-actions';
      const enableBtn = makeButton('启用', 'primary', async () => {
        try { await api.activateProvider(p.id); await refreshProviders(); refreshModels(); }
        catch (err) { await modal.alert(`启用失败：${err.message}`); }
      });
      if (p.is_active) enableBtn.disabled = true;
      actions.appendChild(enableBtn);
      actions.appendChild(makeButton('编辑', '', () => showForm(p)));
      const deleteBtn = makeButton('删除', '', async () => {
        if (!(await modal.confirm(`确认删除 Provider「${p.name}」？`))) return;
        try { await api.deleteProvider(p.id); await refreshProviders(); }
        catch (err) { await modal.alert(`删除失败：${err.message}`); }
      });
      if (p.is_active) deleteBtn.disabled = true;
      actions.appendChild(deleteBtn);
      tr.append(td1, td2, td3, td4, td5, tdVision, td6, actions);
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    parent.appendChild(table);
  }

  async function refreshProviders() {
    const list = ui.byId('providers-list');
    if (!list) return;
    ui.clear(list);
    ui.renderLoading(list, '加载 Provider 列表...');
    try {
      const providers = await api.listProviders();
      ui.clear(list);
      renderProvidersTable(list, providers || []);
    } catch (error) {
      ui.clear(list);
      ui.renderError(list, error.message);
    }
  }

  function buildPayload({ isEdit }) {
    const payload = {
      name: document.getElementById('provider-name').value.trim(),
      provider_type: document.getElementById('provider-type').value,
      base_url: document.getElementById('provider-base-url').value.trim(),
      model: document.getElementById('provider-model').value.trim(),
    };
    const visionCheckbox = document.getElementById('provider-supports-vision');
    if (visionCheckbox) payload.supports_vision = visionCheckbox.checked;
    const rawKey = document.getElementById('provider-api-key').value;
    if (isEdit) {
      if (rawKey === '') {
        // unchanged
      } else if (rawKey.trim() === '') {
        payload.api_key = '';
      } else {
        payload.api_key = rawKey;
      }
    } else {
      payload.api_key = rawKey;
    }
    return payload;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    const isEdit = !!editing;
    const payload = buildPayload({ isEdit });
    try {
      if (isEdit) await api.updateProvider(editing.id, payload);
      else await api.createProvider(payload);
      hideForm();
      await refreshProviders();
      refreshModels();
    } catch (err) {
      await modal.alert(`保存失败：${err.message}`);
    }
  }

  async function refreshModels() {
    const list = ui.byId('models-list');
    if (!list) return;
    ui.clear(list);
    ui.renderLoading(list, '加载模型列表...');
    try {
      const payload = await api.getAdminModels();
      const data = (payload && payload.data) || [];
      ui.clear(list);
      if (!data.length) { ui.renderEmpty(list, '暂无模型'); return; }
      const table = document.createElement('table');
      table.className = 'document-table';
      const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      ['ID', 'Display Name', 'Provider', 'Tools', 'Streaming', 'Default'].forEach((label) => {
        const th = document.createElement('th'); th.textContent = label; headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      const tbody = document.createElement('tbody');
      data.forEach((model) => {
        const tr = document.createElement('tr');
        const td1 = document.createElement('td'); td1.textContent = model.id || '-';
        const td2 = document.createElement('td'); td2.textContent = model.display_name || '-';
        const td3 = document.createElement('td'); td3.textContent = model.provider || '-';
        const td4 = document.createElement('td'); td4.textContent = flagText(model.supports_tools);
        const td5 = document.createElement('td'); td5.textContent = flagText(model.supports_streaming);
        const td6 = document.createElement('td'); td6.textContent = model.is_default === true ? '✓' : '-';
        tr.append(td1, td2, td3, td4, td5, td6);
        tbody.appendChild(tr);
      });
      table.append(thead, tbody);
      list.appendChild(table);
    } catch (error) {
      ui.clear(list);
      ui.renderError(list, error.message);
    }
  }

  function init() {
    const newBtn = ui.byId('providers-new');
    if (newBtn) newBtn.addEventListener('click', () => showForm(null));
    const cancelBtn = ui.byId('providers-form-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', hideForm);
    const closeBtn = ui.byId('providers-form-close');
    if (closeBtn) closeBtn.addEventListener('click', hideForm);
    const modal = ui.byId('providers-modal');
    if (modal) modal.addEventListener('click', (event) => {
      if (event.target === modal) hideForm();
    });
    const form = ui.byId('providers-form');
    if (form) form.addEventListener('submit', handleSubmit);
    refreshProviders();
    refreshModels();
  }

  global.NAGENT = namespace;
  global.NAGENT.models = { init, refresh: refreshModels, refreshProviders };
}(window));
