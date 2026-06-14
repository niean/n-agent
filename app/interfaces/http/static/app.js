(function (global) {
  const namespace = global.NAGENT || {};
  const initialized = { summary: false, chat: false, sessions: false, tools: false, models: false, status: false };

  function onTabActivated(tab) {
    const module = namespace[tab];
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
