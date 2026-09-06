(function (global) {
  const namespace = global.NAGENT || {};
  const tabConfig = [
    { tab: 'summary', path: '/summary', label: '概览' },
    { tab: 'chat', path: '/chat', label: '对话' },
    { tab: 'sessions', path: '/sessions', label: '会话' },
    { tab: 'scheduled-tasks', path: '/scheduled-tasks', label: '定时任务' },
    { tab: 'tasks', path: '/tasks', label: '任务' },
    { tab: 'memory', path: '/memory', label: '记忆' },
    { tab: 'tools', label: '工具', parent: true, children: ['tools-knowledge', 'tools-mcp', 'tools-skill', 'tools-plugin', 'tools-builtin'] },
    { tab: 'tools-knowledge', path: '/tools/knowledge', label: '知识', parentTab: 'tools' },
    { tab: 'tools-mcp', path: '/tools/mcp', label: 'MCP', parentTab: 'tools' },
    { tab: 'tools-skill', path: '/tools/skill', label: 'Skill', parentTab: 'tools' },
    { tab: 'tools-plugin', path: '/tools/plugin', label: 'Plugin', parentTab: 'tools' },
    { tab: 'tools-builtin', path: '/tools/builtin', label: 'Builtin', parentTab: 'tools' },
    { tab: 'executors', label: '执行器', parent: true, children: ['sandbox', 'executors-host', 'browser'] },
    { tab: 'sandbox', path: '/sandbox', label: '沙盒', parentTab: 'executors' },
    { tab: 'executors-host', path: '/executors/host', label: '本机', parentTab: 'executors' },
    { tab: 'browser', path: '/browser', label: '浏览器', parentTab: 'executors' },
    { tab: 'artifacts', path: '/artifacts', label: '制品' },
    { tab: 'models', path: '/models', label: '模型' },
    { tab: 'platforms', path: '/platforms', label: '平台' },
    { tab: 'observations', label: '观测', parent: true, children: ['observations-sessions', 'observations-modules'] },
    { tab: 'observations-sessions', path: '/observations/sessions', label: '会话', parentTab: 'observations' },
    { tab: 'observations-modules', path: '/observations/modules', label: '组件', parentTab: 'observations' },
    { tab: 'security', label: '安全', parent: true, children: ['security-overview', 'security-sessions', 'security-memory', 'security-sandbox'] },
    { tab: 'security-overview', path: '/security', label: '概览', parentTab: 'security' },
    { tab: 'security-sessions', path: '/security/sessions', label: '会话', parentTab: 'security' },
    { tab: 'security-memory', path: '/security/memory', label: '记忆', parentTab: 'security' },
    { tab: 'security-sandbox', path: '/security/sandbox', label: '沙盒', parentTab: 'security' },
  ];
  const tabNames = tabConfig.map((c) => c.tab);
  const tabByPath = Object.fromEntries(tabConfig.filter((c) => c.path).map((c) => [c.path, c.tab]));
  const labelByTab = Object.fromEntries(tabConfig.map((c) => [c.tab, c.label]));
  const pathByTab = Object.fromEntries(tabConfig.filter((c) => c.path).map((c) => [c.tab, c.path]));
  const parentByChild = Object.fromEntries(
    tabConfig.filter((c) => c.parentTab).map((c) => [c.tab, c.parentTab]),
  );

  const topnavConfig = {
    tasks: [
      { tab: 'tasks', path: '/tasks', label: '管理', concern: 'management', scope: 'tasks', topnavParent: 'tasks' },
      { tab: 'tasks-observations', path: '/tasks/observations', label: '观测', concern: 'observation', scope: 'tasks', topnavParent: 'tasks' },
      { tab: 'tasks-security', path: '/tasks/security', label: '安全', concern: 'security', scope: 'tasks', topnavParent: 'tasks' },
    ],
    sessions: [
      { tab: 'sessions', path: '/sessions', label: '管理', concern: 'management', scope: 'sessions', topnavParent: 'sessions' },
      { tab: 'observations-sessions', path: '/sessions/observations', label: '观测', concern: 'observation', scope: 'sessions', topnavParent: 'sessions' },
      { tab: 'security-sessions', path: '/sessions/security', label: '安全', concern: 'security', scope: 'sessions', topnavParent: 'sessions' },
    ],
    observations: [
      { tab: 'observations-sessions', path: '/observations/sessions', label: '会话', concern: 'observation', scope: 'observations', topnavParent: 'observations' },
    ],
    'observations-modules': [
      { tab: 'observations-modules', path: '/observations/modules', label: '组件', concern: 'observation', scope: 'observations-modules', topnavParent: 'observations-modules' },
    ],
    'security-overview': [
      { tab: 'security-overview', path: '/security', label: '概览', concern: 'security', scope: 'overview', topnavParent: 'security' },
    ],
    'security-sessions': [
      { tab: 'security-sessions', path: '/security/sessions', label: '会话', concern: 'security', scope: 'sessions', topnavParent: 'security' },
    ],
    'security-memory': [
      { tab: 'security-memory', path: '/security/memory', label: '记忆', concern: 'security', scope: 'memory', topnavParent: 'security' },
    ],
    'security-sandbox': [
      { tab: 'security-sandbox', path: '/security/sandbox', label: '沙盒', concern: 'security', scope: 'sandbox', topnavParent: 'security' },
    ],
  };
  const routeConfig = [
    { paths: ['/tasks/observations', '/observations/tasks'], tab: 'tasks-observations', renderTab: 'tasks-observations', sidebarTab: 'tasks', topnavParent: 'tasks', scope: 'tasks' },
    { paths: ['/tasks/security'], tab: 'tasks-security', renderTab: 'tasks-security', sidebarTab: 'tasks', topnavParent: 'tasks', scope: 'tasks' },
    // /sessions/observations 是"会话观测"页面的独立 routeConfig entry，
    // 顶导与左导均归左导一级"会话"语义（顶导 `管理 | 观测`，左导 `会话` 高亮），
    // 与 /observations/sessions 共享 observations.js renderer（renderTab='observations-sessions'）。
    { paths: ['/sessions/observations'], tab: 'observations-sessions', renderTab: 'observations-sessions', sidebarTab: 'sessions', topnavParent: 'sessions', scope: 'sessions' },
    // /observations/sessions 是左导二级"观测-会话"标准入口，左导高亮"观测-会话"，顶导进 observations 子域。
    { paths: ['/observations/sessions'], tab: 'observations-sessions', renderTab: 'observations-sessions', sidebarTab: 'observations-sessions', topnavParent: 'observations', scope: 'observations' },
    // /observations/modules 是左导二级"观测-组件"入口，使用组件私有顶导作用域。
    { paths: ['/observations/modules'], tab: 'observations-modules', renderTab: 'observations-modules', sidebarTab: 'observations-modules', topnavParent: 'observations-modules', scope: 'observations-modules' },
    { paths: ['/security'], tab: 'security-overview', renderTab: 'security', sidebarTab: 'security-overview', topnavParent: 'security', scope: 'overview' },
    { paths: ['/security/sessions'], tab: 'security-sessions', renderTab: 'security', sidebarTab: 'security-sessions', topnavParent: 'security', scope: 'sessions' },
    { paths: ['/security/memory'], tab: 'security-memory', renderTab: 'security', sidebarTab: 'security-memory', topnavParent: 'security', scope: 'memory' },
    { paths: ['/security/sandbox'], tab: 'security-sandbox', renderTab: 'security', sidebarTab: 'security-sandbox', topnavParent: 'security', scope: 'sandbox' },
    // /sessions/security 是"会话-安全"页面的独立 routeConfig entry，
    // 顶导归 sessions 子域（左导一级"会话"父项高亮），与 /security/sessions 共享
    // security.js renderer（renderTab='security'），与 /tasks/security 同款范式。
    { paths: ['/sessions/security'], tab: 'security-sessions', renderTab: 'security', sidebarTab: 'sessions', topnavParent: 'sessions', scope: 'sessions' },
  ];
  const sidebarOverride = { '/observations/tasks': 'observations-sessions' };

  function buildRouteByPath(config) {
    const required = ['tab', 'renderTab', 'sidebarTab', 'topnavParent', 'scope'];
    const map = {};
    for (const entry of config) {
      for (const f of required) {
        if (!entry[f]) throw new Error('routeConfig: missing or empty field "' + f + '"');
      }
      if (!Array.isArray(entry.paths) || !entry.paths.length) {
        throw new Error('routeConfig: paths must be a non-empty array');
      }
      for (const p of entry.paths) {
        if (typeof p !== 'string' || p[0] !== '/') {
          throw new Error('routeConfig: path must start with / (got ' + JSON.stringify(p) + ')');
        }
        if (Object.prototype.hasOwnProperty.call(map, p)) {
          throw new Error('routeConfig: duplicate path "' + p + '"');
        }
        map[p] = entry;
      }
    }
    return map;
  }
  const routeByPath = buildRouteByPath(routeConfig);

  const DEFAULT_CHILD = {
    tools: 'tools-knowledge',
    observations: 'observations-sessions',
    executors: 'sandbox',
    security: 'security-overview',
  };

  function isParent(name) {
    const cfg = tabConfig.find((c) => c.tab === name);
    return cfg && cfg.parent === true;
  }

  function selectedTabFromPath(pathname) {
    const path = pathname || window.location.pathname;
    if (tabByPath[path]) return tabByPath[path];
    if (path.startsWith('/scheduled-tasks/')) return 'scheduled-tasks';
    if (path.startsWith('/tasks/')) return 'tasks';
    if (path.startsWith('/artifacts/')) return 'artifacts';
    if (path.startsWith('/observations/sessions/')) return 'observations-sessions';
    if (path === '/tools/external-memory') return 'memory';
    if (path === '/tools/sandbox') return 'sandbox';
    if (path === '/executors') return DEFAULT_CHILD.executors;
    if (path === '/executors/host') return 'executors-host';
    if (path.startsWith('/browser/')) return 'browser';
    if (path === '/tools') return DEFAULT_CHILD.tools;
    if (path === '/observations') return DEFAULT_CHILD.observations;
    if (path === '/' || path === '') return 'summary';
    return 'summary';
  }

  function resolveRoute(pathname) {
    const route = routeByPath[pathname];
    if (route) {
      const sidebarTab = sidebarOverride[pathname] || route.sidebarTab;
      return { activeTab: route.tab, renderTab: route.renderTab, sidebarTab, currentSubdomain: route.topnavParent, route };
    }
    const tab = tabByPath[pathname] || selectedTabFromPath(pathname);
    const currentSubdomain = topnavConfig[tab] ? tab : (parentByChild[tab] || null);
    return { activeTab: tab, renderTab: tab, sidebarTab: tab, currentSubdomain, route: null };
  }

  function activeTopnavItem(items, activeTab) {
    return items.find((i) => i.tab === activeTab) || null;
  }

  function resolveTitle(activeTab, currentSubdomain) {
    if (labelByTab[activeTab]) return labelByTab[activeTab];
    if (currentSubdomain && topnavConfig[currentSubdomain]) {
      const item = topnavConfig[currentSubdomain].find((i) => i.tab === activeTab);
      if (item) return item.label;
    }
    return activeTab;
  }

  function applyRoute(state) {
    document.querySelectorAll('.tab-content').forEach((tab) => tab.classList.remove('active'));
    const target = document.getElementById('tab-' + state.renderTab);
    if (target) {
      target.classList.add('active');
    } else if (state.route) {
      if (global.NAGENT && global.NAGENT.modal && typeof global.NAGENT.modal.alert === 'function') {
        global.NAGENT.modal.alert('页面模块未就绪: ' + state.renderTab);
      }
    }
    document.querySelectorAll('.sidebar__item').forEach((item) => {
      item.classList.remove('sidebar__item--active');
    });
    const navItem = document.querySelector('[data-tab="' + state.sidebarTab + '"]');
    if (navItem) navItem.classList.add('sidebar__item--active');
    const parentTab = parentByChild[state.sidebarTab];
    if (parentTab) {
      applySubmenuState(parentTab, readSubmenuPref(parentTab));
    }
    const title = document.getElementById('topbar-title');
    if (title) title.textContent = resolveTitle(state.activeTab, state.currentSubdomain);
    // TopNav render/destroy and mount visibility handled by onTabActivated (app.js)
    if (global.NAGENT && global.NAGENT.app && typeof global.NAGENT.app.onTabActivated === 'function') {
      global.NAGENT.app.onTabActivated(state);
    }
  }

  function navigatePath(path) {
    let target;
    const base = window.location.href || (window.location.protocol + '//' + window.location.host + window.location.pathname);
    try { target = new URL(path, base); } catch (_) { return; }
    if (target.origin !== new URL(base).origin) return;
    const historyTarget = target.pathname + target.search + target.hash;
    const currentTarget = window.location.pathname + (window.location.search || '') + (window.location.hash || '');
    if (currentTarget !== historyTarget) history.pushState({ path: historyTarget }, '', historyTarget);
    const state = resolveRoute(target.pathname);
    applyRoute(state);
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
    navigatePath(path);
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
    window.addEventListener('popstate', () => applyRoute(resolveRoute(window.location.pathname)));
    navigatePath(window.location.pathname + (window.location.search || '') + (window.location.hash || ''));
  }

  global.NAGENT = namespace;
  global.NAGENT.navigation = {
    initNavigation, navigateTo, switchTab: navigateTo, tabNames, pathByTab,
    resolveRoute, applyRoute, navigatePath, activeTopnavItem, buildRouteByPath,
    topnavConfig, selectedTabFromPath, DEFAULT_CHILD,
  };
}(window));
