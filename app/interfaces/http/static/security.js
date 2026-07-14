(function (global) {
  const namespace = global.NAGENT || {};
  const api = namespace.api;
  const EXPECTED_KEYS = [
    'turn', 'context', 'llm', 'tool', 'memory',
    'sandbox', 'gateway', 'schedule', 'budget', 'information_flow',
  ];
  const POLICY_FIELDS = ['key', 'name', 'display_name', 'dimension', 'execution_point', 'domain_file', 'config'];
  const CFG_FIELDS = ['key', 'label', 'value'];
  const state = { data: null, loading: false, error: '', token: 0 };
  let initialized = false;

  function clear(node) { if (node) node.textContent = ''; }

  function appendText(parent, tag, content, className) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    n.textContent = content;
    parent.appendChild(n);
    return n;
  }

  function formatValue(v) {
    if (v === null || v === undefined) return '-';
    if (typeof v === 'boolean') return v ? '是' : '否';
    return String(v);
  }

  function getRoot() { return document.getElementById('tab-security'); }

  function sameKeys(obj, expected) {
    const got = Object.keys(obj);
    if (got.length !== expected.length) return false;
    return expected.every((k) => Object.prototype.hasOwnProperty.call(obj, k));
  }

  function validate(payload) {
    if (!payload || typeof payload !== 'object') throw new Error('policy_load_failed');
    if (typeof payload.profile_version !== 'string' || !payload.profile_version) {
      throw new Error('policy_load_failed');
    }
    const policies = payload.policies;
    if (!Array.isArray(policies) || policies.length !== 10) throw new Error('policy_load_failed');
    const keys = policies.map((p) => (p && typeof p === 'object') ? p.key : null);
    if (keys.join(',') !== EXPECTED_KEYS.join(',')) throw new Error('policy_load_failed');
    policies.forEach((p) => {
      if (!p || typeof p !== 'object') throw new Error('policy_load_failed');
      if (!sameKeys(p, POLICY_FIELDS)) throw new Error('policy_load_failed');
      if (typeof p.key !== 'string' || typeof p.name !== 'string'
          || typeof p.display_name !== 'string' || typeof p.dimension !== 'string'
          || typeof p.execution_point !== 'string' || typeof p.domain_file !== 'string') {
        throw new Error('policy_load_failed');
      }
      if (!Array.isArray(p.config)) throw new Error('policy_load_failed');
      const seen = Object.create(null);
      p.config.forEach((c) => {
        if (!c || typeof c !== 'object') throw new Error('policy_load_failed');
        if (!sameKeys(c, CFG_FIELDS)) throw new Error('policy_load_failed');
        if (typeof c.key !== 'string' || !c.key || typeof c.label !== 'string') {
          throw new Error('policy_load_failed');
        }
        if (Object.prototype.hasOwnProperty.call(seen, c.key)) throw new Error('policy_load_failed');
        seen[c.key] = true;
        const t = typeof c.value;
        if (c.value === null) return;
        if (t === 'number') {
          if (!Number.isFinite(c.value)) throw new Error('policy_load_failed');
          return;
        }
        if (t !== 'string' && t !== 'boolean') throw new Error('policy_load_failed');
      });
    });
  }

  function renderLoading(root) {
    clear(root);
    appendText(root, 'div', '加载中...', 'loading-state');
  }

  function renderError(root) {
    clear(root);
    const wrap = document.createElement('div');
    wrap.className = 'error-state';
    appendText(wrap, 'div', '策略加载失败');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = '重试';
    btn.addEventListener('click', () => refresh());
    wrap.appendChild(btn);
    root.appendChild(wrap);
  }

  function render(root) {
    clear(root);
    root.appendChild(renderOverview(state.data));
    state.data.policies.forEach((p) => root.appendChild(renderSector(p)));
  }

  function renderOverview(data) {
    const panel = document.createElement('section');
    panel.className = 'status-panel';
    const head = document.createElement('div');
    head.className = 'panel-header';
    appendText(head, 'span', '整体概览');
    panel.appendChild(head);
    const body = document.createElement('div');
    body.className = 'panel-body';
    const bar = document.createElement('div');
    bar.className = 'stats-bar';
    bar.appendChild(statCard(data.profile_version || '', 'Profile 版本'));
    bar.appendChild(statCard(String(data.policies.length), 'Policy 数量'));
    body.appendChild(bar);
    panel.appendChild(body);
    return panel;
  }

  function statCard(value, label) {
    const card = document.createElement('div');
    card.className = 'stat-card';
    const l = document.createElement('div');
    l.className = 'label';
    l.textContent = label;
    const v = document.createElement('div');
    v.className = 'value';
    v.textContent = value;
    card.append(l, v);
    return card;
  }

  function renderSector(p) {
    const panel = document.createElement('section');
    panel.className = 'status-panel';
    const head = document.createElement('div');
    head.className = 'panel-header';
    appendText(head, 'span', p.display_name);
    panel.appendChild(head);
    const body = document.createElement('div');
    body.className = 'panel-body';
    body.appendChild(renderMeta(p));
    body.appendChild(renderCfg(p.config));
    panel.appendChild(body);
    return panel;
  }

  function renderMeta(p) {
    const grid = document.createElement('div');
    grid.className = 'policy-meta';
    grid.appendChild(policyItem('治理维度', p.dimension));
    grid.appendChild(policyItem('执行点', p.execution_point));
    return grid;
  }

  function renderCfg(config) {
    const grid = document.createElement('div');
    grid.className = 'policy-cfg';
    config.forEach((c) => grid.appendChild(policyItem(c.label, formatValue(c.value))));
    return grid;
  }

  function policyItem(label, value) {
    const item = document.createElement('div');
    item.className = 'policy-item';
    const k = document.createElement('span');
    k.className = 'policy-k';
    k.textContent = label + '：';
    const v = document.createElement('span');
    v.className = 'policy-v';
    v.textContent = value;
    item.append(k, v);
    return item;
  }

  async function refresh() {
    const root = getRoot();
    if (!root) return;
    const myToken = ++state.token;
    state.loading = true;
    state.error = '';
    renderLoading(root);
    try {
      const payload = await api.listPolicies();
      if (myToken !== state.token) return;
      validate(payload);
      state.data = payload;
      state.loading = false;
      render(root);
    } catch (err) {
      if (myToken !== state.token) return;
      state.data = null;
      state.loading = false;
      state.error = 'policy_load_failed';
      renderError(root);
    }
  }

  function init() {
    if (initialized) return;
    initialized = true;
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.security = { init, refresh };
}(window));
