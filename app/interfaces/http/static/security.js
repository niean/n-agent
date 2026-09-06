(function (global) {
  const namespace = global.NAGENT || {};
  const api = namespace.api;
  const EXPECTED_KEYS = [
    'turn', 'context', 'llm', 'tool', 'memory',
    'sandbox', 'gateway', 'schedule', 'budget', 'information_flow',
    'delegation',
  ];
  const POLICY_FIELDS = ['key', 'name', 'display_name', 'dimension', 'execution_point', 'domain_file', 'config'];
  const CFG_FIELDS = ['key', 'label', 'value'];
  const SCOPES = Object.freeze({
    overview: Object.freeze(EXPECTED_KEYS.slice()),
    sessions: Object.freeze(['turn', 'context', 'llm', 'tool', 'budget', 'information_flow', 'delegation']),
    memory: Object.freeze(['memory']),
    sandbox: Object.freeze(['sandbox']),
  });
  const state = { data: null, loading: false, error: '', token: 0, scope: null };
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
    if (!sameKeys(payload, ['profile_version', 'policies'])) throw new Error('policy_load_failed');
    if (typeof payload.profile_version !== 'string' || !payload.profile_version) {
      throw new Error('policy_load_failed');
    }
    const policies = payload.policies;
    if (!Array.isArray(policies) || policies.length !== EXPECTED_KEYS.length) throw new Error('policy_load_failed');
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
    btn.addEventListener('click', () => refresh({ route: { scope: state.scope } }));
    wrap.appendChild(btn);
    root.appendChild(wrap);
  }

  function render(root, data, scope) {
    const keys = SCOPES[scope];
    if (!keys || !keys.length || !data || !Array.isArray(data.policies)
        || data.policies.length !== keys.length) {
      throw new Error('policy_load_failed');
    }
    clear(root);
    // 安全页不再追加 整体概览 sector；overview() 兼容函数保留供 tasks-security.js
    // 预检与既有消费者调用，不在安全页 render() 中渲染。
    data.policies.forEach((p) => root.appendChild(sector(p)));
  }

  // Shared renderers exposed as namespace.security.renderers for reuse by
  // tasks-security.js. All are pure DOM constructors taking params + an
  // optional options object; they never read tasks-security closure state.
  // overview(data, options): options.countLabel overrides the count card label
  //   (default 'Policy 数量'). 兼容导出：安全页 render() 已不再调用；保留供
  //   tasks-security.js 预检 `renderers().need.includes('overview')` 通过，避免
  //   任务安全页报"安全页面渲染器不可用"。移除须另行调整消费者。
  // sector(policy, options) / meta(policy, options): options.showSourceFiles
  //   === true plus policy.source_files array appends a source-files item;
  //   options.sourceLabel overrides its label (default '来源文件').
  function overview(data, options) {
    const countLabel = (options && options.countLabel) || 'Policy 数量';
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
    bar.appendChild(statCard(String(data.policies.length), countLabel));
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

  function sector(p, options) {
    const panel = document.createElement('section');
    panel.className = 'status-panel';
    const head = document.createElement('div');
    head.className = 'panel-header';
    appendText(head, 'span', p.display_name);
    panel.appendChild(head);
    const body = document.createElement('div');
    body.className = 'panel-body';
    body.appendChild(meta(p, options));
    body.appendChild(cfg(p.config));
    panel.appendChild(body);
    return panel;
  }

  function meta(p, options) {
    const grid = document.createElement('div');
    grid.className = 'policy-meta';
    grid.appendChild(policyItem('治理维度', p.dimension));
    grid.appendChild(policyItem('执行点', p.execution_point));
    if (options && options.showSourceFiles === true && Array.isArray(p.source_files) && p.source_files.length) {
      const sourceLabel = (options && options.sourceLabel) || '来源文件';
      grid.appendChild(policyItem(sourceLabel, p.source_files.join(', ')));
    }
    return grid;
  }

  function cfg(config) {
    const grid = document.createElement('div');
    grid.className = 'policy-cfg';
    // editable===false (A/B class) -> value rendered gray (muted) to distinguish
    // from C-class (editable) values. undefined (e.g. /security page) -> normal.
    // *_bytes fields are displayed in MB (bytes / 1024 / 1024).
    config.forEach((c) => grid.appendChild(policyItem(c.label, formatConfigValue(c.key, c.value), c.editable)));
    return grid;
  }

  // True if the config key is a byte-count field (displayed/edited in MB).
  function isBytesField(key) {
    return typeof key === 'string' && key.indexOf('_bytes', key.length - 6) !== -1;
  }
  function bytesToMb(v) {
    var n = Number(v);
    if (!isFinite(n)) return v;
    var mb = n / (1024 * 1024);
    return Math.round(mb * 100) / 100;
  }
  function mbToBytes(v) {
    var n = Number(v);
    if (!isFinite(n)) return v;
    return Math.round(n * 1024 * 1024);
  }
  function formatConfigValue(key, value) {
    if (isBytesField(key) && (typeof value === 'number' || (typeof value === 'string' && value && isFinite(Number(value))))) {
      return String(bytesToMb(value));
    }
    return formatValue(value);
  }

  function policyItem(label, value, editable) {
    const item = document.createElement('div');
    item.className = 'policy-item';
    const k = document.createElement('span');
    k.className = 'policy-k';
    k.textContent = label + '：';
    const v = document.createElement('span');
    v.className = 'policy-v';
    if (editable === false) v.classList.add('policy-v--muted');
    v.textContent = value;
    item.append(k, v);
    return item;
  }

  function scopeFor(routeState) {
    const scope = routeState && routeState.route && routeState.route.scope;
    return Object.prototype.hasOwnProperty.call(SCOPES, scope) ? scope : null;
  }

  function renderFailure(root) {
    state.data = null;
    state.loading = false;
    state.error = 'policy_load_failed';
    renderError(root);
  }

  async function refresh(routeState) {
    const root = getRoot();
    if (!root) return;
    const scope = scopeFor(routeState);
    const myToken = ++state.token;
    if (!scope) {
      state.scope = null;
      renderFailure(root);
      return;
    }
    state.scope = scope;
    state.loading = true;
    state.error = '';
    renderLoading(root);
    try {
      const payload = await api.listPolicies(scope);
      if (myToken !== state.token || scope !== state.scope) return;
      validate(payload);
      const policies = payload.policies.filter((p) => SCOPES[scope].indexOf(p.key) !== -1);
      if (policies.length !== SCOPES[scope].length) throw new Error('policy_load_failed');
      state.data = { profile_version: payload.profile_version, policies: policies };
      state.loading = false;
      render(root, state.data, scope);
    } catch (err) {
      if (myToken !== state.token || scope !== state.scope) return;
      renderFailure(root);
    }
  }

  function init(routeState) {
    if (initialized) return;
    initialized = true;
    return refresh(routeState);
  }

  function activate(routeState) { return refresh(routeState); }

  global.NAGENT = namespace;
  global.NAGENT.security = { init, refresh, activate };
  // Expose shared renderers AFTER the {init, refresh} assignment so the object
  // already exists; setting .renderers does not overwrite init/refresh.
  // Shortened keys mirror namespace.observations.renderers convention.
  global.NAGENT.security.renderers = {
    overview: overview,
    sector: sector,
    meta: meta,
    cfg: cfg,
    policyItem: policyItem,
    statCard: statCard,
    formatValue: formatValue,
    isBytesField: isBytesField,
    bytesToMb: bytesToMb,
    mbToBytes: mbToBytes,
    formatConfigValue: formatConfigValue,
  };
}(window));
