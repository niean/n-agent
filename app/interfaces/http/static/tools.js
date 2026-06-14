(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  function riskBadge(level) {
    if (level === 'dangerous') return 'danger';
    if (level === 'confirm') return 'warning';
    return 'success';
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
      ['名称', '描述', '风险等级', '启用', 'Schema'].forEach((label) => {
        const th = document.createElement('th'); th.textContent = label; headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      const tbody = document.createElement('tbody');
      tools.forEach((tool) => {
        const tr = document.createElement('tr');
        const td1 = document.createElement('td'); td1.textContent = tool.name;
        const td2 = document.createElement('td'); td2.textContent = tool.description || '-';
        const td3 = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = `badge badge--${riskBadge(tool.risk_level)}`;
        badge.textContent = tool.risk_level;
        td3.appendChild(badge);
        const td4 = document.createElement('td'); td4.textContent = tool.enabled ? '是' : '否';
        const td5 = document.createElement('td');
        const details = document.createElement('details');
        const summary = document.createElement('summary'); summary.textContent = '查看 schema';
        const pre = document.createElement('pre'); pre.textContent = JSON.stringify(tool.input_schema || {}, null, 2);
        details.append(summary, pre);
        td5.appendChild(details);
        tr.append(td1, td2, td3, td4, td5);
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
