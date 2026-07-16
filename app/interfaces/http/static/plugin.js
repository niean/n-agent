(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  const modal = (namespace.modal || {});

  function root() {
    return ui.byId ? ui.byId('tab-tools-plugin') : document.getElementById('tab-tools-plugin');
  }

  let plugins = [];

  function render() {
    const node = root();
    if (!node) return;
    node.replaceChildren();

    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = 'Plugins';
    const actions = ui.el('span', 'panel-actions');
    const refreshBtn = ui.el('button', 'btn');
    refreshBtn.type = 'button';
    refreshBtn.textContent = '扫描';
    refreshBtn.addEventListener('click', refresh);
    actions.appendChild(refreshBtn);
    header.append(title, actions);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    if (!plugins.length) {
      ui.renderEmpty(body, '尚未发现 Plugin；请检查 plugins_root 配置或点击扫描');
      panel.appendChild(body);
      node.appendChild(panel);
      return;
    }

    const table = ui.el('table', 'document-table');
    const thead = ui.el('thead');
    const trh = ui.el('tr');
    ['Key', '名称', '版本', '描述', 'Kind', '来源', '启用', '扫描', '操作'].forEach((h) => {
      const th = ui.el('th');
      th.textContent = h;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);

    const tbody = ui.el('tbody');
    plugins.forEach((p) => tbody.appendChild(renderRow(p)));
    table.appendChild(tbody);
    body.appendChild(table);
    panel.appendChild(body);
    node.appendChild(panel);
  }

  function renderRow(p) {
    const tr = ui.el('tr');
    const columns = ['key', 'name', 'version', 'description', 'kind', 'source', 'enabled', 'last_scan_status'];
    columns.forEach((key) => {
      const td = ui.el('td');
      const v = p[key];
      if (key === 'enabled') {
        const badge = ui.el('span', 'badge badge--' + (p.enabled ? 'success' : 'warning'));
        badge.textContent = p.enabled ? '启用' : '停用';
        td.appendChild(badge);
      } else if (key === 'last_scan_status') {
        const isOk = v === 'ok';
        const badge = ui.el('span', 'badge badge--' + (isOk ? 'success' : 'warning'));
        badge.textContent = v == null || v === '' ? '未扫描' : String(v);
        td.appendChild(badge);
      } else {
        td.textContent = v === null || v === undefined ? '' : String(v);
      }
      tr.appendChild(td);
    });
    const td = ui.el('td');
    td.className = 'row-actions-cell';
    const group = ui.el('div', 'row-actions');
    const viewBtn = ui.el('button', 'btn');
    viewBtn.type = 'button';
    viewBtn.textContent = '详情';
    viewBtn.addEventListener('click', () => openDetail(p.key));
    const configBtn = ui.el('button', 'btn');
    configBtn.type = 'button';
    configBtn.textContent = '编辑';
    configBtn.addEventListener('click', () => openConfig(p));
    const toggle = ui.el('button', 'btn');
    toggle.type = 'button';
    toggle.textContent = p.enabled ? '停用' : '启用';
    toggle.addEventListener('click', () => toggleEnabled(p.key, !p.enabled));
    group.append(toggle, configBtn, viewBtn);
    td.appendChild(group);
    tr.appendChild(td);
    return tr;
  }

  async function load() {
    try {
      const data = await api.listPlugins();
      plugins = (data && data.items) || [];
    } catch (exc) {
      plugins = [];
    }
    render();
  }

  async function refresh() {
    try {
      await api.refreshPlugins();
      await load();
    } catch (exc) {
      await modal.alert('Plugin 扫描失败: ' + (exc && exc.message ? exc.message : exc));
    }
  }

  async function toggleEnabled(key, enabled) {
    try {
      await api.setPluginEnabled(key, enabled);
      await load();
    } catch (exc) {
      await modal.alert('切换状态失败: ' + (exc && exc.message ? exc.message : exc));
    }
  }

  function openDetail(key) {
    const plugin = plugins.find((p) => p.key === key);
    if (!plugin) return;
    const backdrop = ui.el('div', 'modal-backdrop');
    const dialog = ui.el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = ui.el('form', 'providers-form');
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = 'Plugin 详情: ' + plugin.key;
    const closeBtn = ui.el('button', 'modal-close');
    closeBtn.type = 'button';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭详情弹框');
    closeBtn.addEventListener('click', () => backdrop.remove());
    header.append(title, closeBtn);
    form.appendChild(header);

    const body = ui.el('div', 'plugin-detail-modal-body');
    const list = ui.el('div');
    const fields = [
      ['Key', plugin.key],
      ['Name', plugin.name],
      ['Version', plugin.version],
      ['Description', plugin.description],
      ['Author', plugin.author],
      ['Kind', plugin.kind],
      ['Source', plugin.source],
      ['Source Path', plugin.source_path],
      ['Enabled', plugin.enabled ? 'true' : 'false'],
      ['Last Scan Status', plugin.last_scan_status || ''],
      ['Last Scan Error', plugin.last_scan_error || ''],
      ['Last Scanned At', plugin.last_scanned_at || ''],
    ];
    fields.forEach(([label, value]) => ui.appendText(list, label, value));
    if (plugin.capabilities && plugin.capabilities.unsupported && plugin.capabilities.unsupported.length) {
      ui.appendText(list, 'Unsupported Capabilities', plugin.capabilities.unsupported.join(', '));
    }
    if (plugin.secret_refs && Object.keys(plugin.secret_refs).length) {
      const present = Object.keys(plugin.secret_refs).map((k) => k + (plugin.secret_refs[k] ? ' (set)' : ' (unset)'));
      ui.appendText(list, 'Secret Fields', present.join(', '));
    }
    body.appendChild(list);
    form.appendChild(body);

    const actions = ui.el('div', 'providers-form__actions');
    const closeAction = ui.el('button', 'btn');
    closeAction.type = 'button';
    closeAction.textContent = '关闭';
    closeAction.addEventListener('click', () => backdrop.remove());
    actions.appendChild(closeAction);
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (ev) => {
      if (ev.target === backdrop) backdrop.remove();
    });
    document.body.appendChild(backdrop);
    closeBtn.focus();
  }

  function openConfig(plugin) {
    const manifest = plugin.manifest || {};
    const configSchema = manifest.config_schema || {};
    const properties = configSchema.properties || {};
    const required = configSchema.required || [];
    const requiresEnv = manifest.requires_env || [];

    const backdrop = ui.el('div', 'modal-backdrop');
    const dialog = ui.el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = ui.el('form', 'providers-form');
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = '编辑配置: ' + plugin.key;
    const closeBtn = ui.el('button', 'modal-close');
    closeBtn.type = 'button';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭配置弹框');
    closeBtn.addEventListener('click', () => backdrop.remove());
    header.append(title, closeBtn);
    form.appendChild(header);

    Object.keys(properties).forEach((fieldName) => {
      const spec = properties[fieldName] || {};
      const isSecret = spec.secret === true;
      const isRequired = required.indexOf(fieldName) >= 0;
      const labelEl = document.createElement('label');
      const labelSpan = document.createElement('span');
      labelSpan.textContent = fieldName + (isRequired ? ' *' : '') + (isSecret ? ' (secret)' : '');
      const input = ui.el('input');
      input.name = isSecret ? ('secret:' + fieldName) : fieldName;
      input.type = isSecret ? 'password' : 'text';
      if (!isSecret && plugin.config && plugin.config[fieldName] !== undefined) {
        input.value = String(plugin.config[fieldName]);
      } else if (isSecret && plugin.secret_refs && plugin.secret_refs[fieldName]) {
        input.placeholder = '已设置，留空保持不变';
      }
      labelEl.append(labelSpan, input);
      if (spec.description) {
        const hint = ui.el('span', 'providers-form__hint muted');
        hint.textContent = spec.description;
        labelEl.appendChild(hint);
      }
      form.appendChild(labelEl);
    });

    requiresEnv.forEach((envSpec) => {
      const envName = (envSpec && envSpec.name) || envSpec;
      const isSecret = (envSpec && envSpec.password) !== false;
      const labelEl = document.createElement('label');
      const labelSpan = document.createElement('span');
      labelSpan.textContent = envName + ' (env)';
      const input = ui.el('input');
      input.name = 'secret:' + envName;
      input.type = isSecret ? 'password' : 'text';
      if (plugin.secret_refs && plugin.secret_refs[envName]) {
        input.placeholder = '已设置，留空保持不变';
      }
      labelEl.append(labelSpan, input);
      if (envSpec && envSpec.description) {
        const hint = ui.el('span', 'providers-form__hint muted');
        hint.textContent = envSpec.description;
        labelEl.appendChild(hint);
      }
      form.appendChild(labelEl);
    });

    const actions = ui.el('div', 'providers-form__actions');
    const cancelBtn = ui.el('button', 'btn');
    cancelBtn.type = 'button';
    cancelBtn.textContent = '取消';
    cancelBtn.addEventListener('click', () => backdrop.remove());
    const submitBtn = ui.el('button', 'btn btn--primary');
    submitBtn.type = 'button';
    submitBtn.textContent = '保存';
    submitBtn.addEventListener('click', async () => {
      const config = {};
      const secretUpdates = {};
      const inputs = form.querySelectorAll('input');
      inputs.forEach((input) => {
        const value = input.value;
        if (input.name.startsWith('secret:')) {
          const fn = input.name.slice('secret:'.length);
          if (value) secretUpdates[fn] = value;
        } else {
          if (value) config[input.name] = value;
        }
      });
      try {
        await api.updatePluginConfig(plugin.key, { config, secret_updates: secretUpdates });
        backdrop.remove();
        await load();
      } catch (exc) {
        await modal.alert('保存失败: ' + (exc && exc.message ? exc.message : exc));
      }
    });
    actions.append(cancelBtn, submitBtn);
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (ev) => {
      if (ev.target === backdrop) backdrop.remove();
    });
    document.body.appendChild(backdrop);
    closeBtn.focus();
  }

  namespace.plugin = { init: load, refresh: load };
  global.NAGENT = namespace;
}(window));
