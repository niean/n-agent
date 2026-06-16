(function (global) {
  const namespace = global.NAGENT || {};

  const byId = (id) => document.getElementById(id);
  const clear = (el) => { if (el) el.replaceChildren(); };

  function appendText(parent, label, value) {
    const row = document.createElement('div');
    row.className = 'row';
    const key = document.createElement('span');
    key.className = 'key';
    key.textContent = label;
    const val = document.createElement('span');
    val.className = 'val';
    val.textContent = value == null || value === '' ? '-' : String(value);
    row.append(key, val);
    parent.appendChild(row);
  }

  function appendBadge(parent, value, variant) {
    const badge = document.createElement('span');
    badge.className = variant ? `badge badge--${variant}` : 'badge';
    badge.textContent = value;
    parent.appendChild(badge);
  }

  function renderJson(parent, value) {
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(value, null, 2);
    parent.appendChild(pre);
  }

  function renderState(parent, message, className) {
    const node = document.createElement('div');
    node.className = className;
    node.textContent = message;
    parent.appendChild(node);
  }

  const renderEmpty = (parent, message) => renderState(parent, message || '暂无数据', 'muted empty-state');
  const renderLoading = (parent, message) => renderState(parent, message || '加载中...', 'muted loading-state');
  const renderError = (parent, message) => renderState(parent, message || '加载失败', 'muted error-state');

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  global.NAGENT = namespace;
  global.NAGENT.ui = { byId, clear, appendText, appendBadge, renderJson, renderEmpty, renderLoading, renderError, el };
}(window));
