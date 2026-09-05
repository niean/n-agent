(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  const ENTRIES = [
    { tab: 'chat', label: '对话', desc: '发起新一轮对话或恢复会话' },
    { tab: 'scheduled-tasks', label: '定时任务', desc: '管理定时任务与查看最近执行结果' },
    { tab: 'sessions', label: '会话', desc: '查看历史会话与详细消息' },
    { tab: 'tasks', label: '任务', desc: '提交目标驱动异步任务，看板跟踪进度与产物' },
    { tab: 'memory', label: '记忆', desc: '管理项目记忆，配置全局默认启用' },
    { tab: 'tools-knowledge', label: '知识', desc: '知识管理' },
    { tab: 'tools-mcp', label: 'MCP', desc: '管理 MCP 站点与远端工具' },
    { tab: 'tools-skill', label: 'Skill', desc: '查看 Skill 列表与启停' },
    { tab: 'tools-plugin', label: 'Plugin', desc: 'Plugin 子系统（待实现）' },
    { tab: 'tools-builtin', label: 'Builtin', desc: '查看已注册的工具与风险等级' },
    { tab: 'sandbox', label: '沙盒', desc: '查看沙盒配置、活跃实例与执行历史' },
    { tab: 'executors-host', label: '本机', desc: '查看本机执行器状态、授权策略与执行历史' },
    { tab: 'browser', label: '浏览器', desc: '浏览器自动化，实时视图与操作历史' },
    { tab: 'artifacts', label: '制品', desc: '预览、编辑、导出与发布产出物' },
    { tab: 'models', label: '模型', desc: '查看对外暴露的统一模型' },
    { tab: 'platforms', label: '平台', desc: '查看接入平台与平台会话' },
    { tab: 'observations-sessions', label: '会话观测', desc: '查看会话 Token 用量与 API 调用历史' },
    { tab: 'observations-modules', label: '组件观测', desc: '查看依赖组件健康状态' },
    { tab: 'security-overview', label: '安全', desc: '查看分域安全 Policy 管控策略' },
  ];

  function renderStats(stats, service, dependencies, counts) {
    ui.clear(stats);
    const cards = [
      { label: 'Service', value: service.status || 'unknown' },
      { label: 'Provider', value: (dependencies.provider || {}).status || 'unknown' },
      { label: 'Memory', value: (dependencies.memory || {}).status || 'unknown' },
      { label: 'Knowledge', value: (dependencies.knowledge || {}).status || 'unknown' },
      { label: 'Sandbox', value: (dependencies.sandbox || {}).status || 'unknown' },
      { label: '任务数', value: counts.tasks != null ? counts.tasks : 0 },
      { label: '定时任务数', value: counts.scheduledTasks },
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
      card.href = namespace.navigation && namespace.navigation.pathByTab && namespace.navigation.pathByTab[entry.tab]
        ? namespace.navigation.pathByTab[entry.tab]
        : `/${entry.tab}`;
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
      if (value && Array.isArray(value.items)) return value.items.length;
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
      const [service, dependencies, sessionsCount, toolsCount, modelsCount, tasksCount, scheduledTasksCount] = await Promise.all([
        api.getHealth().catch(() => ({ status: 'error' })),
        api.getDependencyHealth().catch(() => ({})),
        safeCount(api.listSessions()),
        safeCount(api.listTools()),
        safeCount(api.listModels()),
        safeCount(api.task.list()),
        safeCount(api.listScheduledTasks()),
      ]);
      renderStats(stats, service || {}, dependencies || {}, {
        sessions: sessionsCount,
        tools: toolsCount,
        models: modelsCount,
        tasks: tasksCount,
        scheduledTasks: scheduledTasksCount,
      });
      renderEntries(entries);
    } catch (error) {
      ui.clear(stats);
      ui.renderError(stats, error.message);
    }
  }

  function init() {
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.summary = { init, refresh };
}(window));
