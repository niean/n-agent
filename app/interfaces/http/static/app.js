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
    platforms: false,
    memory: false,
    sandbox: false,
    'executors-host': false,
    security: false,
    'tools-builtin': false,
    'tools-mcp': false,
    'tools-knowledge': false,
    'tools-skill': false,
    'tools-plugin': false,
  };

  function resolveModule(tab) {
    if (namespace[tab]) return namespace[tab];
    if (tab === 'tasks') return namespace.tasks;
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
    return null;
  }

  // 部分 tab 存在次级模块：主模块完成渲染（可能清空 tab 节点）后再初始化次级模块，
  // 避免次级模块的容器被主模块的清空操作覆盖。
  function resolveSecondaryModule(tab) {
    if (tab === 'memory') return namespace.externalMemoryProviders;
    return null;
  }

  async function onTabActivated(tab) {
    const module = resolveModule(tab);
    if (!module) return;
    const secondary = resolveSecondaryModule(tab);
    if (!initialized[tab] && typeof module.init === 'function') {
      await Promise.resolve(module.init());
      if (secondary && typeof secondary.init === 'function') {
        await Promise.resolve(secondary.init());
      }
      initialized[tab] = true;
      return;
    }
    if (tab !== 'chat' && typeof module.refresh === 'function') {
      await Promise.resolve(module.refresh());
      if (secondary && typeof secondary.refresh === 'function') {
        await Promise.resolve(secondary.refresh());
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
