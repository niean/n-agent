(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});

  function root() {
    return ui.byId ? ui.byId('tab-tools-knowledge') : document.getElementById('tab-tools-knowledge');
  }

  function buildHeader(node) {
    const headerPanel = ui.el('section', 'status-panel');
    const headerBar = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = '知识检索';
    const refreshBtn = ui.el('button', 'btn');
    refreshBtn.type = 'button';
    refreshBtn.textContent = '刷新';
    refreshBtn.addEventListener('click', load);
    headerBar.append(title, refreshBtn);
    const headerBody = ui.el('div', 'panel-body');
    headerBody.textContent = 'search_knowledge 工具状态与 N-KB 依赖健康';
    headerPanel.append(headerBar, headerBody);
    node.appendChild(headerPanel);
  }

  function buildToolPanel(node) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = 'search_knowledge';
    header.appendChild(title);
    const body = ui.el('div', 'panel-body');
    ui.renderLoading(body, '加载工具定义...');
    panel.append(header, body);
    node.appendChild(panel);
    return body;
  }

  function buildDepPanel(node) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = 'N-KB 依赖';
    header.appendChild(title);
    const body = ui.el('div', 'panel-body');
    ui.renderLoading(body, '加载依赖健康...');
    panel.append(header, body);
    node.appendChild(panel);
    return body;
  }

  async function fillToolPanel(body) {
    try {
      const tools = await api.listTools();
      const tool = (tools || []).find((t) => t.name === 'search_knowledge');
      ui.clear(body);
      if (!tool) {
        ui.renderEmpty(body, '未启用知识检索工具');
        return;
      }
      const desc = ui.el('div');
      desc.textContent = tool.description || '-';
      const meta = ui.el('div', 'muted');
      const enabledLabel = tool.enabled ? '是' : '否';
      meta.textContent = 'risk_level: ' + tool.risk_level + ' · enabled: ' + enabledLabel + ' · source: ' + (tool.source_type || '-');
      body.append(desc, meta);
    } catch (err) {
      ui.clear(body);
      ui.renderError(body, '加载工具失败: ' + (err && err.message ? err.message : err));
    }
  }

  async function fillDepPanel(body) {
    try {
      const deps = await api.getDependencyHealth();
      const kb = (deps || {}).knowledge || {};
      ui.clear(body);
      const fields = Object.keys(kb);
      if (!fields.length) {
        ui.renderEmpty(body, '暂无 N-KB 依赖信息');
        return;
      }
      fields.forEach((key) => {
        const row = ui.el('div', 'row');
        const k = ui.el('span', 'key');
        k.textContent = key;
        const v = ui.el('span', 'val');
        v.textContent = String(kb[key]);
        row.append(k, v);
        body.appendChild(row);
      });
    } catch (err) {
      ui.clear(body);
      ui.renderError(body, '加载健康失败: ' + (err && err.message ? err.message : err));
    }
  }

  async function load() {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    buildHeader(node);
    const toolBody = buildToolPanel(node);
    const depBody = buildDepPanel(node);
    await fillToolPanel(toolBody);
    await fillDepPanel(depBody);
  }

  namespace.knowledge = { init: load, refresh: load, load: load };
  global.NAGENT = namespace;
}(window));
