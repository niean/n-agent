(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  function flagText(value) {
    return value === true ? '✓' : value === false ? '-' : '-';
  }

  async function refresh() {
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
    const refreshBtn = ui.byId('models-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', refresh);
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.models = { init, refresh };
}(window));
