(function (global) {
  const namespace = global.NAGENT || {};
  const initialized = {
    summary: false,
    chat: false,
    sessions: false,
    models: false,
    status: false,
    'scheduled-tasks': false,
    platforms: false,
    'tools-builtin': false,
    'tools-mcp': false,
    'tools-knowledge': false,
    'tools-skill': false,
    'tools-plugin': false,
    'tools-external-memory': false,
  };

  function resolveModule(tab) {
    if (namespace[tab]) return namespace[tab];
    if (tab === 'tools-builtin' || tab === 'tools-mcp') return namespace.tools;
    if (tab === 'tools-skill') return namespace.skills;
    if (tab === 'tools-knowledge') return namespace.knowledge;
    if (tab === 'tools-plugin') return namespace.plugin;
    if (tab === 'tools-external-memory') return namespace.externalMemory;
    return null;
  }

  function onTabActivated(tab) {
    const module = resolveModule(tab);
    if (!module) return;
    if (!initialized[tab] && typeof module.init === 'function') {
      module.init();
      initialized[tab] = true;
      return;
    }
    if (tab !== 'chat' && typeof module.refresh === 'function') {
      module.refresh();
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
