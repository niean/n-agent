(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  const ENTRIES = [
    { tab: 'chat', label: '对话', desc: '发起新一轮对话或恢复会话' },
    { tab: 'scheduled-tasks', label: '任务', desc: '管理定时任务与查看最近执行结果' },
    { tab: 'sessions', label: '会话', desc: '查看历史会话与详细消息' },
    { tab: 'tools-knowledge', label: '知识', desc: '查看 search_knowledge 工具与 N-KB 依赖健康' },
    { tab: 'tools-mcp', label: 'MCP', desc: '管理 MCP 站点与远端工具' },
    { tab: 'tools-skill', label: 'Skill', desc: '查看 Skill 列表与启停' },
    { tab: 'tools-plugin', label: 'Plugin', desc: 'Plugin 子系统（待实现）' },
    { tab: 'tools-builtin', label: '内置工具', desc: '查看已注册的工具与风险等级' },
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
      { label: '任务数', value: counts.scheduledTasks },
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
      card.href = namespace.navigation && namespace.navigation.pathByTab && namespace.navigation.pathByTab[entry.tab]
        ? namespace.navigation.pathByTab[entry.tab]
        : `/${entry.tab === 'status' ? 'status' : entry.tab}`;
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
      const [service, dependencies, sessionsCount, toolsCount, modelsCount, scheduledTasksCount] = await Promise.all([
        api.getHealth().catch(() => ({ status: 'error' })),
        api.getDependencyHealth().catch(() => ({})),
        safeCount(api.listSessions()),
        safeCount(api.listTools()),
        safeCount(api.listModels()),
        safeCount(api.listScheduledTasks()),
      ]);
      renderStats(stats, service || {}, dependencies || {}, {
        sessions: sessionsCount,
        tools: toolsCount,
        models: modelsCount,
        scheduledTasks: scheduledTasksCount,
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
