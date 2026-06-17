(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;
  let selectedSite = null;

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

  function closeSchemaModal() {
    const existing = ui.byId('tools-schema-modal');
    if (existing) existing.remove();
  }

  function openSchemaModal(tool) {
    closeSchemaModal();
    const backdrop = document.createElement('div');
    backdrop.id = 'tools-schema-modal';
    backdrop.className = 'modal-backdrop';
    backdrop.setAttribute('role', 'presentation');

    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog tools-schema-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'tools-schema-title');

    const content = document.createElement('div');
    content.className = 'tools-schema-content';

    const header = document.createElement('div');
    header.className = 'modal-header';
    const title = document.createElement('h4');
    title.id = 'tools-schema-title';
    title.textContent = `${tool.name || tool.local_name || tool.remote_name} Schema`;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'modal-close';
    close.setAttribute('aria-label', '关闭 Schema 弹出框');
    close.textContent = '×';
    close.addEventListener('click', closeSchemaModal);
    header.append(title, close);

    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(tool.input_schema || {}, null, 2);
    content.append(header, pre);
    dialog.appendChild(content);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeSchemaModal();
    });
    document.body.appendChild(backdrop);
    close.focus();
  }

  async function refreshTools() {
    const list = ui.byId('tools-list');
    if (!list) return;
    ui.clear(list);
    ui.renderLoading(list, '加载工具列表...');
    try {
      const tools = await api.listTools();
      const allowedSources = ['builtin', 'agent'];
      const builtinTools = (tools || []).filter((tool) => allowedSources.includes(tool.source_type));
      ui.clear(list);
      renderToolsTable(list, builtinTools, '暂无内置工具');
    } catch (error) {
      ui.clear(list);
      ui.renderError(list, error.message);
    }
  }

  async function refreshMcpTools() {
    const list = ui.byId('mcp-tools-list');
    if (!list) return;
    ui.clear(list);
    ui.renderLoading(list, '加载 MCP 工具...');
    try {
      const tools = await api.listTools();
      const mcpTools = (tools || []).filter((tool) => tool.source_type === 'mcp');
      ui.clear(list);
      renderToolsTable(list, mcpTools, '暂无 MCP 工具');
    } catch (error) {
      ui.clear(list);
      ui.renderError(list, error.message);
    }
  }

  function renderToolsTable(container, tools, emptyText) {
    if (!tools.length) { ui.renderEmpty(container, emptyText); return; }
    const table = document.createElement('table');
    table.className = 'document-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['名称', '类型', '分组', '描述', '风险等级', '启用', 'Schema'].forEach((label) => {
      const th = document.createElement('th'); th.textContent = label; headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    tools.forEach((tool) => {
      const tr = document.createElement('tr');
      const td1 = document.createElement('td'); td1.textContent = tool.name;
      const td2 = document.createElement('td'); td2.textContent = tool.source_type || '-';
      const td3 = document.createElement('td'); td3.textContent = tool.toolset || '-';
      const td4 = document.createElement('td'); td4.textContent = tool.description || '-';
      const td5 = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = `badge badge--${riskBadge(tool.risk_level)}`;
      badge.textContent = tool.risk_level;
      td5.appendChild(badge);
      const td6 = document.createElement('td'); td6.textContent = tool.enabled ? '是' : '否';
      const td7 = document.createElement('td');
      const schemaBtn = document.createElement('button');
      schemaBtn.type = 'button';
      schemaBtn.className = 'btn';
      schemaBtn.textContent = '查看';
      schemaBtn.addEventListener('click', () => openSchemaModal(tool));
      td7.appendChild(schemaBtn);
      tr.append(td1, td2, td3, td4, td5, td6, td7);
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    container.appendChild(table);
  }

  async function refreshMcpSites() {
    const list = ui.byId('mcp-sites-list');
    if (!list) return;
    ui.clear(list);
    ui.renderLoading(list, '加载 MCP 站点...');
    try {
      const sites = await api.listMcpSites();
      ui.clear(list);
      if (!sites.length) { ui.renderEmpty(list, '暂无 MCP 站点'); return; }
      const table = document.createElement('table');
      table.className = 'document-table';
      const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      ['名称', '传输', 'Endpoint', '启用', '状态', '最近探测', '操作'].forEach((label) => {
        const th = document.createElement('th'); th.textContent = label; headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      const tbody = document.createElement('tbody');
      sites.forEach((site) => tbody.appendChild(siteRow(site)));
      table.append(thead, tbody);
      list.appendChild(table);
    } catch (error) {
      ui.clear(list);
      ui.renderError(list, error.message);
    }
  }

  function siteRow(site) {
    const tr = document.createElement('tr');
    const name = document.createElement('td'); name.textContent = site.name;
    const transport = document.createElement('td'); transport.textContent = site.transport_type;
    const url = document.createElement('td'); url.textContent = site.transport_type === 'stdio' ? `${site.command || '-'} (${(site.args || []).length} args)` : site.url;
    const enabled = document.createElement('td'); enabled.textContent = site.enabled ? '是' : '否';
    const status = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `badge badge--${statusBadge(site.last_probe_status)}`;
    badge.textContent = site.last_probe_status;
    status.appendChild(badge);
    if (site.last_probe_error) {
      const err = document.createElement('div');
      err.className = 'muted';
      err.textContent = site.last_probe_error;
      status.appendChild(err);
    }
    const probed = document.createElement('td'); probed.textContent = site.last_probed_at || '-';
    const actions = document.createElement('td');
    const group = document.createElement('div');
    group.className = 'row-actions';
    const edit = actionButton('编辑', () => openSiteModal(site));
    const refresh = actionButton('刷新', async () => { await api.refreshMcpSite(site.id); await refreshMcpSites(); await refreshTools(); await refreshMcpTools(); });
    const tools = actionButton('查看', () => openSiteTools(site));
    const remove = actionButton('删除', async () => {
      if (!window.confirm(`删除 MCP 站点 ${site.name}？`)) return;
      await api.deleteMcpSite(site.id);
      await refreshMcpSites();
      await refreshTools();
      await refreshMcpTools();
    });
    group.append(edit, refresh, tools, remove);
    actions.appendChild(group);
    tr.append(name, transport, url, enabled, status, probed, actions);
    return tr;
  }

  function actionButton(label, handler) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn';
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
  }

  function openSiteModal(site) {
    closeSchemaModal();
    selectedSite = site || null;
    const backdrop = document.createElement('div');
    backdrop.id = 'tools-schema-modal';
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
    title.textContent = site ? '编辑 MCP 站点' : '新增 MCP 站点';
    const close = actionButton('×', closeSchemaModal);
    close.className = 'modal-close';
    header.append(title, close);
    const name = field('名称', 'text', site ? site.name : '');
    const url = field('URL', 'text', site ? site.url : '');
    const command = field('Command', 'text', site ? (site.command || '') : '');
    const args = textareaField('Args', site ? (site.args || []).join('\n') : '');
    const env = textareaField('Env', site ? Object.entries(site.env || {}).map(([key, value]) => `${key}=${value}`).join('\n') : '');
    const transport = document.createElement('label');
    transport.textContent = '传输类型';
    const select = document.createElement('select');
    ['streamable_http', 'sse', 'stdio'].forEach((value) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      if ((site ? site.transport_type : 'streamable_http') === value) option.selected = true;
      select.appendChild(option);
    });
    transport.appendChild(select);
    const enabled = document.createElement('label');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = site ? site.enabled : true;
    enabled.textContent = '启用';
    enabled.appendChild(checkbox);
    const updateTransportFields = () => {
      const isStdio = select.value === 'stdio';
      url.label.style.display = isStdio ? 'none' : '';
      command.label.style.display = isStdio ? '' : 'none';
      args.label.style.display = isStdio ? '' : 'none';
      env.label.style.display = isStdio ? '' : 'none';
    };
    select.addEventListener('change', updateTransportFields);
    updateTransportFields();
    const actions = document.createElement('div');
    actions.className = 'providers-form__actions';
    const probe = actionButton('探测', async () => {
      const result = await api.probeMcpSite(sitePayload(name.input, url.input, select, checkbox, command.input, args.input, env.input));
      openProbeResult(result.tools || []);
    });
    const save = document.createElement('button');
    save.type = 'submit';
    save.className = 'btn btn--primary';
    save.textContent = '保存';
    actions.append(probe, save, actionButton('取消', closeSchemaModal));
    form.append(header, name.label, transport, url.label, command.label, args.label, env.label, enabled, actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (selectedSite) await api.updateMcpSite(selectedSite.id, sitePayload(name.input, url.input, select, checkbox, command.input, args.input, env.input));
      else await api.createMcpSite(sitePayload(name.input, url.input, select, checkbox, command.input, args.input, env.input));
      closeSchemaModal();
      await refreshMcpSites();
      await refreshTools();
      await refreshMcpTools();
    });
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    name.input.focus();
  }

  function field(labelText, type, value) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const input = document.createElement('input');
    input.type = type;
    input.value = value;
    label.appendChild(input);
    return { label, input };
  }

  function textareaField(labelText, value) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const input = document.createElement('textarea');
    input.value = value;
    label.appendChild(input);
    return { label, input };
  }

  function sitePayload(name, url, transport, enabled, command, args, env) {
    const payload = { name: name.value, url: url.value, transport_type: transport.value, enabled: enabled.checked };
    if (transport.value === 'stdio') {
      payload.command = command.value;
      payload.args = args.value.split('\n').map((item) => item.trim()).filter(Boolean);
      payload.env = parseEnv(env.value);
    }
    return payload;
  }

  function parseEnv(value) {
    const result = {};
    value.split('\n').forEach((line) => {
      const index = line.indexOf('=');
      if (index <= 0) return;
      const key = line.slice(0, index).trim();
      if (!key) return;
      result[key] = line.slice(index + 1);
    });
    return result;
  }

  function openProbeResult(tools) {
    closeSchemaModal();
    const backdrop = document.createElement('div');
    backdrop.id = 'tools-schema-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog tools-schema-dialog';
    const content = document.createElement('div');
    content.className = 'tools-schema-content';
    const header = document.createElement('div');
    header.className = 'modal-header';
    const title = document.createElement('h4');
    title.textContent = '探测到的 MCP 工具';
    header.append(title, actionButton('×', closeSchemaModal));
    content.appendChild(header);
    if (!tools.length) ui.renderEmpty(content, '未探测到工具');
    tools.forEach((tool) => {
      const row = document.createElement('div');
      row.className = 'mcp-tool-row';
      const name = document.createElement('strong'); name.textContent = tool.name;
      const desc = document.createElement('span'); desc.textContent = tool.description || '-';
      const schema = actionButton('Schema', () => openSchemaModal(tool));
      row.append(name, desc, schema);
      content.appendChild(row);
    });
    dialog.appendChild(content);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
  }

  async function openSiteTools(site) {
    const tools = await api.listMcpSiteTools(site.id);
    closeSchemaModal();
    const backdrop = document.createElement('div');
    backdrop.id = 'tools-schema-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog tools-schema-dialog';
    const content = document.createElement('div');
    content.className = 'tools-schema-content';
    const header = document.createElement('div');
    header.className = 'modal-header';
    const title = document.createElement('h4');
    title.textContent = `${site.name} 工具`;
    header.append(title, actionButton('×', closeSchemaModal));
    content.appendChild(header);
    if (!tools.length) ui.renderEmpty(content, '暂无工具');
    tools.forEach((tool) => {
      const row = document.createElement('div');
      row.className = 'mcp-tool-row';
      const name = document.createElement('strong'); name.textContent = tool.local_name;
      const remote = document.createElement('span'); remote.textContent = tool.remote_name;
      const enabled = document.createElement('label');
      const toggle = document.createElement('input');
      toggle.type = 'checkbox';
      toggle.checked = tool.enabled;
      toggle.addEventListener('change', async () => {
        await api.updateMcpTool(site.id, tool.id, { enabled: toggle.checked });
        await refreshTools();
        await refreshMcpTools();
      });
      enabled.textContent = '启用';
      enabled.appendChild(toggle);
      row.append(name, remote, enabled, actionButton('Schema', () => openSchemaModal(tool)));
      content.appendChild(row);
    });
    dialog.appendChild(content);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
  }

  async function refresh() {
    await refreshTools();
    await refreshMcpSites();
    await refreshMcpTools();
  }

  function init() {
    const refreshBtn = ui.byId('tools-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', refreshTools);
    const mcpRefresh = ui.byId('mcp-sites-refresh');
    if (mcpRefresh) mcpRefresh.addEventListener('click', refreshMcpSites);
    const mcpToolsRefresh = ui.byId('mcp-tools-refresh');
    if (mcpToolsRefresh) mcpToolsRefresh.addEventListener('click', refreshMcpTools);
    const mcpNew = ui.byId('mcp-site-new');
    if (mcpNew) mcpNew.addEventListener('click', () => openSiteModal(null));
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.tools = { init, refresh };
}(window));
