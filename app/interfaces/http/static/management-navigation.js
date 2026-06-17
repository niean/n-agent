(function (global) {
  const namespace = global.NAGENT || {};
  const tabConfig = [
    { tab: 'summary', path: '/summary', label: '概览' },
    { tab: 'chat', path: '/chat', label: '对话' },
    { tab: 'scheduled-tasks', path: '/scheduled-tasks', label: '任务' },
    { tab: 'sessions', path: '/sessions', label: '会话' },
    { tab: 'tools', label: '工具', parent: true, children: ['tools-knowledge', 'tools-mcp', 'tools-skill', 'tools-plugin', 'tools-builtin'] },
    { tab: 'tools-knowledge', path: '/tools/knowledge', label: '知识', parentTab: 'tools' },
    { tab: 'tools-mcp', path: '/tools/mcp', label: 'MCP', parentTab: 'tools' },
    { tab: 'tools-skill', path: '/tools/skill', label: 'Skill', parentTab: 'tools' },
    { tab: 'tools-plugin', path: '/tools/plugin', label: 'Plugin', parentTab: 'tools' },
    { tab: 'tools-builtin', path: '/tools/builtin', label: 'Builtin', parentTab: 'tools' },
    { tab: 'models', path: '/models', label: '模型' },
    { tab: 'platforms', path: '/platforms', label: '平台' },
    { tab: 'status', path: '/status', label: '观测' },
  ];
  const tabNames = tabConfig.map((c) => c.tab);
  const tabByPath = Object.fromEntries(tabConfig.filter((c) => c.path).map((c) => [c.path, c.tab]));
  const labelByTab = Object.fromEntries(tabConfig.map((c) => [c.tab, c.label]));
  const pathByTab = Object.fromEntries(tabConfig.filter((c) => c.path).map((c) => [c.tab, c.path]));
  const parentByChild = Object.fromEntries(
    tabConfig.filter((c) => c.parentTab).map((c) => [c.tab, c.parentTab]),
  );
  const TOOLS_DEFAULT_CHILD = 'tools-knowledge';

  function isParent(name) {
    const cfg = tabConfig.find((c) => c.tab === name);
    return cfg && cfg.parent === true;
  }

  function selectedTabFromPath() {
    const path = window.location.pathname;
    if (tabByPath[path]) return tabByPath[path];
    if (path === '/tools') return TOOLS_DEFAULT_CHILD;
    if (path === '/' || path === '') return 'summary';
    return 'summary';
  }

  function applyTab(name) {
    let next = tabNames.includes(name) ? name : 'summary';
    if (isParent(next)) next = TOOLS_DEFAULT_CHILD;
    document.querySelectorAll('.tab-content').forEach((tab) => tab.classList.remove('active'));
    document.querySelectorAll('.sidebar__item').forEach((item) => {
      item.classList.remove('sidebar__item--active');
    });
    const target = document.getElementById(`tab-${next}`);
    if (target) target.classList.add('active');
    const nav = document.querySelector(`[data-tab="${next}"]`);
    if (nav) nav.classList.add('sidebar__item--active');
    const parentTab = parentByChild[next];
    if (parentTab) {
      const parentNav = document.querySelector(`[data-tab="${parentTab}"]`);
      if (parentNav) parentNav.classList.add('sidebar__item--parent-open');
      const submenu = document.querySelector(`[data-submenu-of="${parentTab}"]`);
      if (submenu) submenu.classList.add('sidebar__submenu--open');
    }
    const title = document.getElementById('topbar-title');
    if (title) title.textContent = labelByTab[next] || next;
    if (global.NAGENT && global.NAGENT.app && typeof global.NAGENT.app.onTabActivated === 'function') {
      global.NAGENT.app.onTabActivated(next);
    }
  }

  function closeAllPopouts() {
    document.querySelectorAll('.sidebar__submenu').forEach((sub) => sub.classList.remove('sidebar__submenu--open'));
    document.querySelectorAll('.sidebar__item--parent').forEach((p) => p.classList.remove('sidebar__item--parent-open'));
  }

  function closeTopModal() {
    const modals = Array.from(document.querySelectorAll('.modal-backdrop')).filter((modal) => !modal.hidden);
    const modal = modals[modals.length - 1];
    if (!modal) return false;
    const close = modal.querySelector('.modal-close');
    if (close) close.click();
    else modal.remove();
    return true;
  }

  function navigateTo(name) {
    if (isParent(name)) {
      if (!document.body.classList.contains('sidebar-expanded')) return;
      const submenu = document.querySelector(`[data-submenu-of="${name}"]`);
      const parentNav = document.querySelector(`[data-tab="${name}"]`);
      if (submenu) submenu.classList.toggle('sidebar__submenu--open');
      if (parentNav) parentNav.classList.toggle('sidebar__item--parent-open');
      return;
    }
    const path = pathByTab[name];
    if (!path) return;
    if (window.location.pathname !== path) {
      history.pushState({ tab: name }, '', path);
    }
    applyTab(name);
  }

  function initNavigation() {
    const toggle = document.getElementById('sidebar-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        const expanded = document.body.classList.toggle('sidebar-expanded');
        toggle.setAttribute('aria-expanded', String(expanded));
        closeAllPopouts();
      });
    }
    document.querySelectorAll('.sidebar__item').forEach((link) => {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        navigateTo(link.dataset.tab);
      });
    });
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (closeTopModal()) return;
      closeAllPopouts();
    });
    window.addEventListener('popstate', () => applyTab(selectedTabFromPath()));
    applyTab(selectedTabFromPath());
  }

  global.NAGENT = namespace;
  global.NAGENT.navigation = { initNavigation, navigateTo, switchTab: navigateTo, tabNames, pathByTab };
}(window));
