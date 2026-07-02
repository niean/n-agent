(function (global) {
  const namespace = global.NAGENT || {};
  const tabConfig = [
    { tab: 'summary', path: '/summary', label: '概览' },
    { tab: 'chat', path: '/chat', label: '对话' },
    { tab: 'scheduled-tasks', path: '/scheduled-tasks', label: '任务' },
    { tab: 'sessions', path: '/sessions', label: '会话' },
    { tab: 'memory', path: '/memory', label: '记忆' },
    { tab: 'tools', label: '工具', parent: true, children: ['tools-knowledge', 'tools-mcp', 'tools-skill', 'tools-plugin', 'tools-builtin'] },
    { tab: 'tools-knowledge', path: '/tools/knowledge', label: '知识', parentTab: 'tools' },
    { tab: 'tools-mcp', path: '/tools/mcp', label: 'MCP', parentTab: 'tools' },
    { tab: 'tools-skill', path: '/tools/skill', label: 'Skill', parentTab: 'tools' },
    { tab: 'tools-plugin', path: '/tools/plugin', label: 'Plugin', parentTab: 'tools' },
    { tab: 'tools-builtin', path: '/tools/builtin', label: 'Builtin', parentTab: 'tools' },
    { tab: 'sandbox', path: '/sandbox', label: '沙盒' },
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
    if (path.startsWith('/scheduled-tasks/')) return 'scheduled-tasks';
    if (path === '/tools/external-memory') return 'memory';
    if (path === '/tools/sandbox') return 'sandbox';
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
      item.classList.remove('sidebar__item--parent-selected');
    });
    const target = document.getElementById(`tab-${next}`);
    if (target) target.classList.add('active');
    const nav = document.querySelector(`[data-tab="${next}"]`);
    if (nav) nav.classList.add('sidebar__item--active');
    const parentTab = parentByChild[next];
    if (parentTab) {
      applySubmenuState(parentTab, readSubmenuPref(parentTab));
      const parentNav = document.querySelector(`[data-tab="${parentTab}"]`);
      if (parentNav) parentNav.classList.add('sidebar__item--parent-selected');
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
      const open = !(submenu && submenu.classList.contains('sidebar__submenu--open'));
      applySubmenuState(name, open);
      writeSubmenuPref(name, open);
      return;
    }
    const path = pathByTab[name];
    if (!path) return;
    // 当前已在目标 tab 的路径或其子路径上时，不覆盖 URL（保留子路径状态，如
    // /scheduled-tasks/{task_id}），只切换 tab 高亮。
    const current = window.location.pathname;
    if (current !== path && !current.startsWith(path + '/')) {
      history.pushState({ tab: name }, '', path);
    }
    applyTab(name);
  }

  const SIDEBAR_PREF_KEY = 'nagent.sidebar.expanded';
  const SUBMENU_PREF_PREFIX = 'nagent.sidebar.submenu.';

  function readSidebarPref() {
    try {
      const value = localStorage.getItem(SIDEBAR_PREF_KEY);
      if (value === '0') return false;
      if (value === '1') return true;
    } catch (_) { /* localStorage 不可用时回落默认 */ }
    return true;
  }

  function writeSidebarPref(expanded) {
    try { localStorage.setItem(SIDEBAR_PREF_KEY, expanded ? '1' : '0'); } catch (_) { /* ignore */ }
  }

  function applySidebarExpanded(expanded) {
    const toggle = document.getElementById('sidebar-toggle');
    document.body.classList.toggle('sidebar-expanded', expanded);
    if (toggle) toggle.setAttribute('aria-expanded', String(expanded));
  }

  function readSubmenuPref(parentTab) {
    try {
      const value = localStorage.getItem(SUBMENU_PREF_PREFIX + parentTab);
      if (value === '0') return false;
      if (value === '1') return true;
    } catch (_) { /* localStorage 不可用时回落默认 */ }
    return true;
  }

  function writeSubmenuPref(parentTab, open) {
    try { localStorage.setItem(SUBMENU_PREF_PREFIX + parentTab, open ? '1' : '0'); } catch (_) { /* ignore */ }
  }

  function applySubmenuState(parentTab, open) {
    const submenu = document.querySelector(`[data-submenu-of="${parentTab}"]`);
    const parentNav = document.querySelector(`[data-tab="${parentTab}"]`);
    if (submenu) submenu.classList.toggle('sidebar__submenu--open', open);
    if (parentNav) parentNav.classList.toggle('sidebar__item--parent-open', open);
  }

  function initNavigation() {
    applySidebarExpanded(readSidebarPref());
    tabConfig.filter((c) => c.parent).forEach((c) => {
      applySubmenuState(c.tab, readSubmenuPref(c.tab));
    });
    const toggle = document.getElementById('sidebar-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => {
        const expanded = document.body.classList.toggle('sidebar-expanded');
        toggle.setAttribute('aria-expanded', String(expanded));
        writeSidebarPref(expanded);
        if (expanded) {
          tabConfig.filter((c) => c.parent).forEach((c) => {
            applySubmenuState(c.tab, readSubmenuPref(c.tab));
          });
        } else {
          closeAllPopouts();
        }
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
