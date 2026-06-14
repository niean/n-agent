(function (global) {
  const namespace = global.NAGENT || {};
  const tabConfig = [
    { tab: 'summary', path: '/summary', label: '概览' },
    { tab: 'chat', path: '/chat', label: '对话' },
    { tab: 'sessions', path: '/sessions', label: '会话' },
    { tab: 'tools', path: '/tools', label: '工具' },
    { tab: 'models', path: '/models', label: '模型' },
    { tab: 'status', path: '/status', label: '健康' },
  ];
  const tabNames = tabConfig.map((c) => c.tab);
  const tabByPath = Object.fromEntries(tabConfig.map((c) => [c.path, c.tab]));
  const labelByTab = Object.fromEntries(tabConfig.map((c) => [c.tab, c.label]));
  const pathByTab = Object.fromEntries(tabConfig.map((c) => [c.tab, c.path]));

  function selectedTabFromPath() {
    const path = window.location.pathname;
    if (tabByPath[path]) return tabByPath[path];
    if (path === '/' || path === '') return 'summary';
    return 'summary';
  }

  function applyTab(name) {
    const next = tabNames.includes(name) ? name : 'summary';
    document.querySelectorAll('.tab-content').forEach((tab) => tab.classList.remove('active'));
    document.querySelectorAll('.sidebar__item').forEach((item) => item.classList.remove('sidebar__item--active'));
    const target = document.getElementById(`tab-${next}`);
    if (target) target.classList.add('active');
    const nav = document.querySelector(`[data-tab="${next}"]`);
    if (nav) nav.classList.add('sidebar__item--active');
    const title = document.getElementById('topbar-title');
    if (title) title.textContent = labelByTab[next] || next;
    if (global.NAGENT && global.NAGENT.app && typeof global.NAGENT.app.onTabActivated === 'function') {
      global.NAGENT.app.onTabActivated(next);
    }
  }

  function navigateTo(name) {
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
      });
    }
    document.querySelectorAll('.sidebar__item').forEach((link) => {
      link.addEventListener('click', (event) => {
        event.preventDefault();
        navigateTo(link.dataset.tab);
      });
    });
    window.addEventListener('popstate', () => applyTab(selectedTabFromPath()));
    applyTab(selectedTabFromPath());
  }

  global.NAGENT = namespace;
  global.NAGENT.navigation = { initNavigation, navigateTo, switchTab: navigateTo, tabNames };
}(window));
