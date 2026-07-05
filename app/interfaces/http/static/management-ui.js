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

  function buildModalFrame(title) {
    const backdrop = el('div', 'modal-backdrop');
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = el('div', 'modal-header');
    const titleEl = document.createElement('h4');
    titleEl.textContent = title;
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭');
    header.append(titleEl, closeBtn);
    form.appendChild(header);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    return { backdrop, form, closeBtn };
  }

  function confirm(message, options) {
    options = options || {};
    return new Promise((resolve) => {
      const frame = buildModalFrame(options.title || '确认');
      const messageEl = el('p', 'modal-message');
      messageEl.textContent = message;
      frame.form.appendChild(messageEl);
      const actions = el('div', 'providers-form__actions');
      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.className = 'btn';
      cancelBtn.textContent = options.cancelLabel || '取消';
      const confirmBtn = document.createElement('button');
      confirmBtn.type = 'submit';
      confirmBtn.className = 'btn';
      confirmBtn.textContent = options.confirmLabel || '确认';
      actions.append(cancelBtn, confirmBtn);
      frame.form.appendChild(actions);
      let resolved = false;
      function done(value) {
        if (resolved) return;
        resolved = true;
        frame.backdrop.remove();
        resolve(value);
      }
      frame.backdrop.addEventListener('click', (event) => {
        if (event.target === frame.backdrop) done(false);
      });
      frame.closeBtn.addEventListener('click', () => done(false));
      cancelBtn.addEventListener('click', () => done(false));
      frame.form.addEventListener('submit', (event) => {
        event.preventDefault();
        done(true);
      });
      document.body.appendChild(frame.backdrop);
      cancelBtn.focus();
    });
  }

  function alert(message, options) {
    options = options || {};
    return new Promise((resolve) => {
      const frame = buildModalFrame(options.title || '提示');
      const messageEl = el('p', 'modal-message');
      messageEl.textContent = message;
      frame.form.appendChild(messageEl);
      const actions = el('div', 'providers-form__actions');
      const okBtn = document.createElement('button');
      okBtn.type = 'submit';
      okBtn.className = 'btn';
      okBtn.textContent = options.closeLabel || '知道了';
      actions.appendChild(okBtn);
      frame.form.appendChild(actions);
      let resolved = false;
      function done() {
        if (resolved) return;
        resolved = true;
        frame.backdrop.remove();
        resolve();
      }
      frame.backdrop.addEventListener('click', (event) => {
        if (event.target === frame.backdrop) done();
      });
      frame.closeBtn.addEventListener('click', done);
      frame.form.addEventListener('submit', (event) => {
        event.preventDefault();
        done();
      });
      document.body.appendChild(frame.backdrop);
      okBtn.focus();
    });
  }

  global.NAGENT = namespace;
  global.NAGENT.ui = { byId, clear, appendText, appendBadge, renderJson, renderEmpty, renderLoading, renderError, el };
  global.NAGENT.modal = { confirm, alert };
}(window));
