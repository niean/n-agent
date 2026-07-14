(function (global) {
  const namespace = global.NAGENT || {};
  const api = namespace.api;
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
    if (v === null || v === undefined || v === '') return '-';
    if (typeof v === 'boolean') return v ? '是' : '否';
    return String(v);
  }

  function getRoot() { return document.getElementById('tab-observations-modules'); }

  function badgeVariant(status) {
    if (status === 'ok') return 'success';
    if (typeof status === 'string' && (status === 'error' || status.startsWith('error'))) return 'danger';
    if (status === 'warn' || status === 'disabled' || status === 'docker_unavailable') return 'warning';
    return 'warning';
  }

  function isHealthy(status) { return status === 'ok'; }

  function renderLoading(root) {
    clear(root);
    appendText(root, 'div', '加载依赖健康...', 'loading-state');
  }

  function renderError(root) {
    clear(root);
    const wrap = document.createElement('div');
    wrap.className = 'error-state';
    appendText(wrap, 'div', '健康状态加载失败');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn';
    btn.textContent = '重试';
    btn.addEventListener('click', () => refresh());
    wrap.appendChild(btn);
    root.appendChild(wrap);
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

  function renderOverview(service, dependencies) {
    const panel = document.createElement('section');
    panel.className = 'status-panel';
    const head = document.createElement('div');
    head.className = 'panel-header';
    appendText(head, 'span', '整体概览');
    panel.appendChild(head);
    const body = document.createElement('div');
    body.className = 'panel-body';
    const entries = Object.entries(dependencies || {});
    const total = entries.length;
    const healthy = entries.filter(([, info]) => info && isHealthy(info.status)).length;
    const abnormal = total - healthy;
    const bar = document.createElement('div');
    bar.className = 'stats-bar';
    bar.appendChild(statCard(formatValue((service || {}).status), 'Service'));
    bar.appendChild(statCard(String(total), '依赖总数'));
    bar.appendChild(statCard(String(healthy), '健康'));
    bar.appendChild(statCard(String(abnormal), '异常'));
    body.appendChild(bar);
    panel.appendChild(body);
    return panel;
  }

  function renderSector(name, info) {
    const panel = document.createElement('section');
    panel.className = 'status-panel';
    const head = document.createElement('div');
    head.className = 'panel-header';
    appendText(head, 'span', name);
    panel.appendChild(head);
    const body = document.createElement('div');
    body.className = 'panel-body';
    body.appendChild(renderMeta(info));
    body.appendChild(renderCfg(info));
    panel.appendChild(body);
    return panel;
  }

  function renderMeta(info) {
    const grid = document.createElement('div');
    grid.className = 'health-meta';
    const item = document.createElement('div');
    item.className = 'health-item';
    const k = document.createElement('span');
    k.className = 'health-k';
    k.textContent = '状态：';
    item.appendChild(k);
    const status = (info && typeof info === 'object') ? info.status : null;
    if (status) {
      const badge = document.createElement('span');
      badge.className = `badge badge--${badgeVariant(status)}`;
      badge.textContent = String(status);
      item.appendChild(badge);
    } else {
      const v = document.createElement('span');
      v.className = 'health-v';
      v.textContent = '-';
      item.appendChild(v);
    }
    grid.appendChild(item);
    return grid;
  }

  function renderCfg(info) {
    const grid = document.createElement('div');
    grid.className = 'health-cfg';
    const fields = [];
    if (info && typeof info === 'object') {
      Object.entries(info).forEach(([k, v]) => {
        if (k === 'status') return;
        fields.push([labelOf(k), formatValue(v)]);
      });
    }
    if (!fields.length) {
      appendText(grid, 'div', '无额外配置', 'muted');
      return grid;
    }
    fields.forEach(([label, value]) => grid.appendChild(healthItem(label, value)));
    return grid;
  }

  function labelOf(key) {
    const map = {
      base_url: 'Base URL',
      model: '模型',
      path: '路径',
      enabled_count: '启用数',
      total_count: '总数',
      error: '错误',
      tick_seconds: '轮询(秒)',
      timezone: '时区',
      type: '类型',
      docker_available: 'Docker 可用',
      enabled: '启用',
      idle_seconds: '空闲回收(秒)',
    };
    return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : key;
  }

  function healthItem(label, value) {
    const item = document.createElement('div');
    item.className = 'health-item';
    const k = document.createElement('span');
    k.className = 'health-k';
    k.textContent = label + '：';
    const v = document.createElement('span');
    v.className = 'health-v';
    v.textContent = value;
    item.append(k, v);
    return item;
  }

  function render(root) {
    clear(root);
    const service = (state.data && state.data.service) || {};
    const dependencies = (state.data && state.data.dependencies) || {};
    root.appendChild(renderOverview(service, dependencies));
    Object.entries(dependencies).forEach(([name, info]) => {
      root.appendChild(renderSector(name, info));
    });
  }

  async function refresh() {
    const root = getRoot();
    if (!root) return;
    const myToken = ++state.token;
    state.loading = true;
    state.error = '';
    renderLoading(root);
    try {
      const [service, dependencies] = await Promise.all([
        api.getHealth(),
        api.getDependencyHealth(),
      ]);
      if (myToken !== state.token) return;
      state.data = { service: service || {}, dependencies: dependencies || {} };
      state.loading = false;
      render(root);
    } catch (err) {
      if (myToken !== state.token) return;
      state.data = null;
      state.loading = false;
      state.error = 'health_load_failed';
      renderError(root);
    }
  }

  function init() {
    if (initialized) return;
    initialized = true;
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.status = { init, refresh };
}(window));
