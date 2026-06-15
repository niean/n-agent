(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  function riskBadge(level) {
    if (level === 'dangerous') return 'danger';
    if (level === 'confirm') return 'warning';
    return 'success';
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
    title.textContent = `${tool.name} Schema`;
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

  async function refresh() {
    const list = ui.byId('tools-list');
    if (!list) return;
    ui.clear(list);
    ui.renderLoading(list, '加载工具列表...');
    try {
      const tools = await api.listTools();
      ui.clear(list);
      if (!tools.length) { ui.renderEmpty(list, '暂无工具'); return; }
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
        schemaBtn.textContent = '查看 schema';
        schemaBtn.addEventListener('click', () => openSchemaModal(tool));
        td7.appendChild(schemaBtn);
        tr.append(td1, td2, td3, td4, td5, td6, td7);
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
    const refreshBtn = ui.byId('tools-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', refresh);
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.tools = { init, refresh };
}(window));
