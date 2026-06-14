(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  const ENTRIES = [
    { tab: 'chat', label: '对话', desc: '发起新一轮对话或恢复会话' },
    { tab: 'sessions', label: '会话', desc: '查看历史会话与详细消息' },
    { tab: 'tools', label: '工具', desc: '查看已注册的工具与风险等级' },
    { tab: 'models', label: '模型', desc: '查看对外暴露的统一模型' },
    { tab: 'status', label: '健康', desc: '检查 Provider/Memory/Knowledge 状态' },
  ];

  function renderStats(stats, service, dependencies, counts) {
    ui.clear(stats);
    const cards = [
      { label: 'Service', value: service.status || 'unknown' },
      { label: 'Provider', value: (dependencies.provider || {}).status || 'unknown' },
      { label: 'Memory', value: (dependencies.memory || {}).status || 'unknown' },
      { label: 'Knowledge', value: (dependencies.knowledge || {}).status || 'unknown' },
      { label: '会话数', value: counts.sessions },
      { label: '工具数', value: counts.tools },
      { label: '模型数', value: counts.models },
    ];
    cards.forEach((s) => {
      const card = document.createElement('div'); card.className = 'stat-card';
      const label = document.createElement('div'); label.className = 'label'; label.textContent = s.label;
      const value = document.createElement('div'); value.className = 'value'; value.textContent = String(s.value);
      card.append(label, value);
      stats.appendChild(card);
    });
  }

  function renderEntries(target) {
    ui.clear(target);
    ENTRIES.forEach((entry) => {
      const card = document.createElement('a');
      card.className = 'summary-entry';
      card.href = `/${entry.tab === 'status' ? 'status' : entry.tab}`;
      card.dataset.tab = entry.tab;
      card.addEventListener('click', (event) => {
        event.preventDefault();
        if (namespace.navigation && typeof namespace.navigation.navigateTo === 'function') {
          namespace.navigation.navigateTo(entry.tab);
        }
      });
      const title = document.createElement('div'); title.className = 'summary-entry__title'; title.textContent = entry.label;
      const desc = document.createElement('div'); desc.className = 'summary-entry__desc'; desc.textContent = entry.desc;
      card.append(title, desc);
      target.appendChild(card);
    });
  }

  async function safeCount(promise) {
    try {
      const value = await promise;
      if (Array.isArray(value)) return value.length;
      if (value && Array.isArray(value.data)) return value.data.length;
      return 0;
    } catch (error) {
      return '-';
    }
  }

  async function refresh() {
    const stats = ui.byId('summary-stats');
    const entries = ui.byId('summary-entries');
    if (!stats || !entries) return;
    ui.clear(stats);
    ui.renderLoading(stats, '加载概览...');
    try {
      const [service, dependencies, sessionsCount, toolsCount, modelsCount] = await Promise.all([
        api.getHealth().catch(() => ({ status: 'error' })),
        api.getDependencyHealth().catch(() => ({})),
        safeCount(api.listSessions()),
        safeCount(api.listTools()),
        safeCount(api.listModels()),
      ]);
      renderStats(stats, service || {}, dependencies || {}, {
        sessions: sessionsCount,
        tools: toolsCount,
        models: modelsCount,
      });
      renderEntries(entries);
    } catch (error) {
      ui.clear(stats);
      ui.renderError(stats, error.message);
    }
  }

  function init() {
    const refreshBtn = ui.byId('summary-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', refresh);
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.summary = { init, refresh };
}(window));
