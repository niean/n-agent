(function (global) {
  'use strict';
  // tasks-security: read-only Task Security page. Mounts at tab-tasks-security.
  // Loads /chat/tasks/security, validates the response contract, then renders
  // 5 sectors by reusing namespace.security.renderers (shared with /security).
  // Pure DOM via renderers/textContent; no unsafe DOM sinks.
  const namespace = global.NAGENT || {};
  const api = namespace.api;
  const ui = namespace.ui || {};
  const CONTAINER_ID = 'tab-tasks-security';

  const state = { data: null, loading: false, error: '', token: 0 };
  let inflight = null;
  let initialized = false;

  function root() { return document.getElementById(CONTAINER_ID); }
  function isActive() {
    const node = root();
    return !!(node && node.classList && node.classList.contains('active'));
  }
  function isCurrent(token) { return token === state.token && isActive(); }

  function clear(node) { if (node) node.textContent = ''; }
  function appendText(parent, tag, content, className) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    n.textContent = content;
    parent.appendChild(n);
    return n;
  }

  // A plain non-array object (not null, not array).
  function isPlainRecord(v) {
    return v !== null && typeof v === 'object' && !Array.isArray(v);
  }
  // obj has exactly the expected key set (no more, no less).
  function sameKeys(obj, expected) {
    const got = Object.keys(obj);
    if (got.length !== expected.length) return false;
    return expected.every(function (k) { return Object.prototype.hasOwnProperty.call(obj, k); });
  }

  function renderers() {
    const R = (namespace.security && namespace.security.renderers) || {};
    const need = ['overview', 'sector', 'meta', 'cfg', 'policyItem', 'statCard', 'formatValue'];
    for (let i = 0; i < need.length; i++) {
      if (typeof R[need[i]] !== 'function') return null;
    }
    return R;
  }

  function validate(payload) {
    if (!isPlainRecord(payload)) throw new Error('invalid');
    if (!sameKeys(payload, ['profile_version', 'policies'])) throw new Error('invalid');
    if (typeof payload.profile_version !== 'string' || !payload.profile_version) throw new Error('invalid');
    if (!Array.isArray(payload.policies) || !payload.policies.length) throw new Error('invalid');
    const sectorFields = ['key', 'name', 'display_name', 'dimension', 'execution_point', 'source_files', 'config'];
    const seenSectors = Object.create(null);
    payload.policies.forEach(function (p) {
      if (!isPlainRecord(p) || !sameKeys(p, sectorFields)) throw new Error('invalid');
      for (const f of ['key', 'name', 'display_name', 'dimension', 'execution_point']) {
        if (typeof p[f] !== 'string' || !p[f]) throw new Error('invalid');
      }
      if (seenSectors[p.key]) throw new Error('invalid');
      seenSectors[p.key] = true;
      if (!Array.isArray(p.source_files) || !p.source_files.length) throw new Error('invalid');
      const sf = Object.create(null);
      p.source_files.forEach(function (s) {
        if (typeof s !== 'string' || !s) throw new Error('invalid');
        if (sf[s]) throw new Error('invalid');
        sf[s] = true;
      });
      if (!Array.isArray(p.config) || !p.config.length) throw new Error('invalid');
      const seenCfg = Object.create(null);
      p.config.forEach(function (c) {
        if (!isPlainRecord(c)) throw new Error('invalid');
        // Required: key, label, value. Optional: editable (C-class flag).
        for (const f of ['key', 'label', 'value']) {
          if (!Object.prototype.hasOwnProperty.call(c, f)) throw new Error('invalid');
        }
        for (const k of Object.keys(c)) {
          if (k !== 'key' && k !== 'label' && k !== 'value' && k !== 'editable') throw new Error('invalid');
        }
        if (typeof c.key !== 'string' || !c.key || typeof c.label !== 'string' || !c.label) throw new Error('invalid');
        if ('editable' in c && typeof c.editable !== 'boolean') throw new Error('invalid');
        if (seenCfg[c.key]) throw new Error('invalid');
        seenCfg[c.key] = true;
        const t = typeof c.value;
        if (c.value === null) return;
        if (t === 'number') { if (!Number.isFinite(c.value)) throw new Error('invalid'); return; }
        if (t !== 'string' && t !== 'boolean') throw new Error('invalid');
      });
    });
  }

  function renderError(node, message) {
    clear(node);
    const wrap = document.createElement('div');
    wrap.className = 'error-state';
    appendText(wrap, 'div', message || '任务安全配置加载失败');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = '重试';
    btn.addEventListener('click', function () { refresh(); });
    wrap.appendChild(btn);
    node.appendChild(wrap);
  }

  function renderLoading(node) {
    clear(node);
    appendText(node, 'div', '加载中...', 'loading-state');
  }

  function render(node, data) {
    const R = renderers();
    if (!R) { renderError(node, '安全页面渲染器不可用'); return; }
    clear(node);
    // 整体概览 sector 已移除：页面直接从 5 个 Sector 开始。
    data.policies.forEach(function (p) {
      const sectorEl = R.sector(p, { showSourceFiles: true, sourceLabel: '来源文件' });
      // Attach an edit button if the sector has any editable (C-class) config.
      const hasEditable = Array.isArray(p.config) && p.config.some(function (c) { return c && c.editable === true; });
      if (hasEditable) {
        const header = sectorEl.querySelector ? sectorEl.querySelector('.panel-header') : null;
        if (header) {
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'btn btn--sm';
          btn.textContent = '编辑';
          btn.addEventListener('click', function () { openEditModal(p); });
          header.appendChild(btn);
        }
      }
      node.appendChild(sectorEl);
    });
  }

  // --- Edit modal (C-class Dashboard-editable config) ---
  // Uses the project standard modal structure (modal-backdrop > modal-dialog >
  // providers-form > modal-header + content + providers-form__actions).
  function el(tag, className) {
    if (ui.el) return ui.el(tag, className);
    const n = document.createElement(tag);
    if (className) n.className = className;
    return n;
  }

  function openEditModal(sector) {
    closeEditModal();
    const backdrop = el('div', 'modal-backdrop');
    const dialog = el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';

    const header = el('div', 'modal-header');
    const titleEl = document.createElement('h4');
    titleEl.textContent = '编辑' + (sector.display_name || '');
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭');
    closeBtn.addEventListener('click', closeEditModal);
    header.append(titleEl, closeBtn);
    form.appendChild(header);

    const body = el('div', 'providers-form__body');
    const loadingHint = el('div', 'loading-state');
    loadingHint.textContent = '加载配置...';
    body.appendChild(loadingHint);
    form.appendChild(body);

    const actions = el('div', 'providers-form__actions');
    const status = document.createElement('span');
    status.className = 'providers-form__status muted';
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn';
    cancelBtn.textContent = '取消';
    cancelBtn.addEventListener('click', closeEditModal);
    const saveBtn = document.createElement('button');
    saveBtn.type = 'submit';
    saveBtn.className = 'btn';
    saveBtn.textContent = '保存';
    saveBtn.disabled = true;
    actions.append(status, cancelBtn, saveBtn);
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', function (ev) { if (ev.target === backdrop) closeEditModal(); });
    document.body.appendChild(backdrop);

    editModal = { backdrop: backdrop, form: form, body: body, status: status, saveBtn: saveBtn, inputs: {}, expectedVersion: 0 };
    editModal._keyHandler = function (ev) { if (ev.key === 'Escape') closeEditModal(); };
    document.addEventListener('keydown', editModal._keyHandler);
    // Enter submits the form -> save.
    form.addEventListener('submit', function (ev) { ev.preventDefault(); saveConfig(sector); });

    loadConfigIntoModal(sector);
  }

  async function loadConfigIntoModal(sector) {
    const body = editModal.body;
    const saveBtn = editModal.saveBtn;
    let cfg;
    try {
      cfg = await api.task.securityConfig.get();
    } catch (err) {
      if (!editModal) return;
      body.replaceChildren();
      const e = el('div', 'error-state');
      e.textContent = '加载配置失败';
      body.appendChild(e);
      return;
    }
    if (!editModal) return; // closed
    editModal.expectedVersion = cfg.version;
    body.replaceChildren();
    const R = renderers();
    const isBytes = function (k) { return R && typeof R.isBytesField === 'function' && R.isBytesField(k); };
    const toMb = function (k, v) { return isBytes(k) && R.bytesToMb ? R.bytesToMb(v) : v; };
    const editableItems = (sector.config || []).filter(function (c) { return c && c.editable === true; });
    editableItems.forEach(function (c) {
      const row = el('div', 'form-row');
      const lbl = document.createElement('label');
      lbl.className = 'form-label';
      lbl.textContent = c.label;
      const input = document.createElement('input');
      input.type = 'number';
      input.className = 'form-input';
      const displayVal = toMb(c.key, cfg.config[c.key]);
      input.value = String(displayVal);
      input.min = isBytes(c.key) ? '0.1' : '1';
      if (isBytes(c.key)) input.step = '0.1';
      input.dataset.key = c.key;
      input.dataset.original = String(displayVal);
      input.dataset.isBytes = isBytes(c.key) ? '1' : '0';
      row.append(lbl, input);
      body.appendChild(row);
      editModal.inputs[c.key] = input;
    });
    saveBtn.disabled = false;
  }

  async function saveConfig(sector) {
    if (!editModal) return;
    const status = editModal.status;
    const saveBtn = editModal.saveBtn;
    const partial = {};
    let bad = null;
    const R = renderers();
    Object.keys(editModal.inputs).forEach(function (k) {
      const input = editModal.inputs[k];
      const raw = input.value;
      const n = Number(raw);
      const isBytes = input.dataset.isBytes === '1';
      // bytes fields allow decimals (MB); others require integers.
      if (!Number.isFinite(n) || n < 0) {
        bad = c_label(sector, k) + ' 必须是数字';
        return;
      }
      if (!isBytes && !/^-?\d+$/.test(String(raw).trim())) {
        bad = c_label(sector, k) + ' 必须是整数';
        return;
      }
      if (String(n) !== input.dataset.original) {
        // Convert MB -> bytes for byte fields before sending.
        partial[k] = (isBytes && R && R.mbToBytes) ? R.mbToBytes(n) : n;
      }
    });
    if (bad) { status.textContent = bad; return; }
    if (Object.keys(partial).length === 0) { status.textContent = '无变更'; return; }
    saveBtn.disabled = true;
    status.textContent = '保存中...';
    try {
      await api.task.securityConfig.update(Object.assign({ expected_version: editModal.expectedVersion }, partial));
      closeEditModal();
      refresh();
    } catch (err) {
      if (!editModal) return;
      saveBtn.disabled = false;
      const code = err && err.message ? err.message : '';
      if (code === 'task_config_conflict') status.textContent = '配置已被他人修改，请关闭后重新编辑';
      else if (code === 'task_config_invalid') status.textContent = '校验失败，请检查取值';
      else status.textContent = '保存失败';
    }
  }

  function c_label(sector, key) {
    const item = (sector.config || []).find(function (c) { return c.key === key; });
    return item ? item.label : key;
  }

  function closeEditModal() {
    if (!editModal) return;
    if (editModal._keyHandler) {
      document.removeEventListener('keydown', editModal._keyHandler);
    }
    if (editModal.backdrop && editModal.backdrop.parentNode) {
      editModal.backdrop.parentNode.removeChild(editModal.backdrop);
    }
    editModal = null;
  }

  let editModal = null;


  async function load() {
    const node = root();
    if (!node) return;
    const myToken = ++state.token;
    state.loading = true;
    // Renderer precheck before any API call.
    if (!renderers()) {
      state.loading = false;
      renderError(node, '安全页面渲染器不可用');
      return;
    }
    renderLoading(node);
    try {
      const payload = await api.task.security();
      if (!isCurrent(myToken)) return;
      validate(payload);
      state.data = payload;
      state.loading = false;
      render(node, payload);
    } catch (err) {
      if (!isCurrent(myToken)) return;
      state.data = null;
      state.loading = false;
      renderError(node, '任务安全配置加载失败');
    }
  }

  // Shared load runner with in-flight de-duplication. force=true supersedes
  // the existing in-flight (its result is discarded by the token guard) and
  // starts a new load immediately. The finally callback compares Promise
  // identity so an old request completing cannot clear a newer request's ref.
  function runLoad(force) {
    if (!force && inflight) return inflight;
    if (force) inflight = null;
    const p = (async function () {
      try { await load(); }
      finally { if (inflight === p) inflight = null; }
    })();
    inflight = p;
    return p;
  }

  function init() {
    // Concurrent init() calls in the same cycle share the in-flight Promise
    // (one request). After completion, repeat init() is a no-op (no reload).
    // Regular function (not async) so the same inflight reference is returned
    // directly -- async wrapping would create a new Promise per call.
    if (inflight) return inflight;
    if (initialized) return Promise.resolve();
    initialized = true;
    return runLoad(true);
  }
  function refresh() { return runLoad(true); }
  function deactivate() {
    state.token++;
    state.data = null;
    inflight = null;
    closeEditModal();
  }

  namespace.tasksSecurity = { init: init, refresh: refresh, deactivate: deactivate };
  global.NAGENT = namespace;
}(window));
