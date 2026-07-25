(function (global) {
  'use strict';
  // tasks-security: read-only Task Security page. Mounts at tab-tasks-security.
  // Loads /chat/tasks/security, validates the response contract, then renders
  // 5 sectors by reusing namespace.security.renderers (shared with /security).
  // Pure DOM via renderers/textContent; no unsafe DOM sinks.
  const namespace = global.NAGENT || {};
  const api = namespace.api;
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
        if (!isPlainRecord(c) || !sameKeys(c, ['key', 'label', 'value'])) throw new Error('invalid');
        if (typeof c.key !== 'string' || !c.key || typeof c.label !== 'string' || !c.label) throw new Error('invalid');
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
    node.appendChild(R.overview(data, { countLabel: 'Sector 数量' }));
    data.policies.forEach(function (p) {
      node.appendChild(R.sector(p, { showSourceFiles: true, sourceLabel: '来源文件' }));
    });
  }

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
  }

  namespace.tasksSecurity = { init: init, refresh: refresh, deactivate: deactivate };
  global.NAGENT = namespace;
}(window));
