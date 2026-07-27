(function (global) {
  const namespace = global.NAGENT || {};
  const initialized = {
    summary: false,
    chat: false,
    sessions: false,
    models: false,
    'observations-modules': false,
    'observations-sessions': false,
    'scheduled-tasks': false,
    tasks: false,
    'tasks-observations': false,
    'tasks-security': false,
    platforms: false,
    memory: false,
    sandbox: false,
    'executors-host': false,
    security: false,
    browser: false,
    'tools-builtin': false,
    'tools-mcp': false,
    'tools-knowledge': false,
    'tools-skill': false,
    'tools-plugin': false,
  };
  let activeTab = null;

  // In-flight init promises: concurrent onTabActivated calls for the same tab
  // share the same init Promise, preventing duplicate init requests.
  const inflight = {};

  function resolveModule(tab) {
    if (namespace[tab]) return namespace[tab];
    if (tab === 'tasks') return namespace.tasks;
    if (tab === 'tasks-observations') return namespace.tasksObservations;
    if (tab === 'tasks-security') return namespace.tasksSecurity;
    if (tab === 'tools-builtin' || tab === 'tools-mcp') return namespace.tools;
    if (tab === 'tools-skill') return namespace.skills;
    if (tab === 'tools-knowledge') return namespace.knowledge;
    if (tab === 'tools-plugin') return namespace.plugin;
    if (tab === 'memory') return namespace.externalMemory;
    if (tab === 'sandbox') return namespace.sandbox;
    if (tab === 'executors-host') return namespace.host;
    if (tab === 'observations-sessions') return namespace.observations;
    if (tab === 'observations-modules') return namespace.status;
    if (tab === 'security') return namespace.security;
    if (tab === 'browser') return namespace.browser;
    return null;
  }

  // 部分 tab 存在次级模块：主模块完成渲染（可能清空 tab 节点）后再初始化次级模块，
  // 避免次级模块的容器被主模块的清空操作覆盖。
  function resolveSecondaryModule(tab) {
    if (tab === 'memory') return namespace.externalMemoryProviders;
    return null;
  }

  // Normalize input to route state object. Accepts state object (from applyRoute)
  // and plain tab string (legacy callers), normalizing to
  // {renderTab, activeTab, currentSubdomain, sidebarTab, route}.
  function normalizeState(input) {
    if (input && typeof input === 'object') return input;
    const tab = input;
    let currentSubdomain = null;
    if (namespace.navigation && namespace.navigation.topnavConfig) {
      const cfg = namespace.navigation.topnavConfig;
      if (cfg[tab]) {
        currentSubdomain = tab;
      } else {
        for (const sub in cfg) {
          if (cfg[sub] && cfg[sub].some(function (item) { return item && item.tab === tab; })) {
            currentSubdomain = sub;
            break;
          }
        }
      }
    }
    return { renderTab: tab, activeTab: tab, currentSubdomain: currentSubdomain, sidebarTab: tab, route: null };
  }

  // Render or destroy TopNav based on currentSubdomain. Reads topnavConfig from
  // NAGENT.navigation (validated route config). onActivate delegates to
  // NAGENT.navigation.navigatePath. No subdomain -> destroy TopNav + show title.
  function renderTopnav(state) {
    const topnavMount = document.getElementById('topnav-mount');
    const topnav = namespace.topnav;
    const topnavConfig = (namespace.navigation && namespace.navigation.topnavConfig) || {};
    const items = state.currentSubdomain ? topnavConfig[state.currentSubdomain] : null;
    const hasTopnav = items && items.length && topnav && typeof topnav.render === 'function';
    if (hasTopnav) {
      if (topnavMount) topnavMount.hidden = false;
      const titleWrap = document.getElementById('topbar-title-wrap');
      if (titleWrap) titleWrap.hidden = true;
      topnav.render(topnavMount, {
        items: items,
        activeTab: state.activeTab,
        onActivate: function (item) {
          if (namespace.navigation && typeof namespace.navigation.navigatePath === 'function') {
            namespace.navigation.navigatePath(item.path);
          }
        },
      });
    } else {
      if (topnavMount) topnavMount.hidden = true;
      const titleWrap = document.getElementById('topbar-title-wrap');
      if (titleWrap) titleWrap.hidden = false;
      if (topnav && typeof topnav.destroy === 'function') {
        topnav.destroy();
      }
    }
  }

  // Display module error in the module's container. Does not throw, does not
  // block navigation state updates.
  function renderModuleError(tab, error) {
    const container = document.getElementById('tab-' + tab);
    if (!container) return;
    try {
      container.replaceChildren();
      const div = document.createElement('div');
      div.className = 'module-error';
      div.textContent = '模块加载失败: ' + (error && error.message ? error.message : String(error));
      container.appendChild(div);
    } catch (_) { /* ignore render errors */ }
  }

  async function onTabActivated(input) {
    const state = normalizeState(input);
    const tab = state.renderTab;

    if (activeTab && activeTab !== tab) {
      const previousModule = resolveModule(activeTab);
      if (previousModule && typeof previousModule.deactivate === 'function') {
        try { previousModule.deactivate(); } catch (_) { /* deactivation must not block navigation */ }
      }
    }
    activeTab = tab;

    // Update navigation state (TopNav) first; module errors must not block this.
    try {
      renderTopnav(state);
    } catch (_) { /* TopNav errors don't block module init */ }

    const module = resolveModule(tab);
    if (!module) return;
    const secondary = resolveSecondaryModule(tab);

    if (!initialized[tab]) {
      // First activation: init. Share in-flight Promise to prevent duplicate
      // init requests from concurrent onTabActivated calls.
      if (inflight[tab]) return inflight[tab];
      inflight[tab] = (async () => {
        try {
          if (typeof module.init === 'function') {
            await Promise.resolve(module.init());
          }
          if (secondary && typeof secondary.init === 'function') {
            await Promise.resolve(secondary.init());
          }
          initialized[tab] = true;
        } catch (e) {
          renderModuleError(tab, e);
        } finally {
          delete inflight[tab];
        }
      })();
      return inflight[tab];
    }

    // Subsequent activation: refresh only (chat skips refresh on activation).
    if (tab !== 'chat' && typeof module.refresh === 'function') {
      try {
        await Promise.resolve(module.refresh());
        if (secondary && typeof secondary.refresh === 'function') {
          await Promise.resolve(secondary.refresh());
        }
      } catch (e) {
        renderModuleError(tab, e);
      }
    }
  }

  function start() {
    namespace.app = namespace.app || {};
    namespace.app.onTabActivated = onTabActivated;
    if (namespace.navigation && typeof namespace.navigation.initNavigation === 'function') {
      namespace.navigation.initNavigation();
    }
  }

  global.NAGENT = namespace;
  global.NAGENT.app = global.NAGENT.app || {};
  global.NAGENT.app.start = start;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
}(window));
