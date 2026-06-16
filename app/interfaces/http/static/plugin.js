(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});

  function root() {
    return ui.byId ? ui.byId('tab-tools-plugin') : document.getElementById('tab-tools-plugin');
  }

  function load() {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = 'Plugin';
    header.appendChild(title);
    const body = ui.el('div', 'panel-body');
    body.textContent = 'Plugin 子系统待实现';
    panel.append(header, body);
    node.appendChild(panel);
  }

  namespace.plugin = { init: load, refresh: load };
  global.NAGENT = namespace;
}(window));
