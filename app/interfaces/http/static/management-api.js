(function (global) {
  const namespace = global.NAGENT || {};

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      const code = data && data.error && data.error.code ? data.error.code : 'request_failed';
      throw new Error(code);
    }
    return data;
  }

  const listSessions = () => fetchJson('/chat/sessions');
  const createSession = (id) => fetchJson(`/chat/sessions?session_id=${encodeURIComponent(id)}`, { method: 'POST' });
  const getSessionDetail = (id) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}`);
  const getSessionToolCalls = (id) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}/tool-calls`);
  const renameSession = (id, title) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  const deleteSession = (id) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const listTools = () => fetchJson('/chat/tools');
  const listModels = () => fetchJson('/v1/models');
  const getAdminModels = () => fetchJson('/chat/models');
  const getHealth = () => fetchJson('/health');
  const getDependencyHealth = () => fetchJson('/chat/health/dependencies');

  const listKnowledgeBases = () => fetchJson('/chat/knowledge/bases');
  const createKnowledgeBase = (payload) => fetchJson('/chat/knowledge/bases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const updateKnowledgeBase = (id, payload) => fetchJson(`/chat/knowledge/bases/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const deleteKnowledgeBase = (id) => fetchJson(`/chat/knowledge/bases/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const probeKnowledgeBase = (payload) => fetchJson('/chat/knowledge/bases/probe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const probeSavedKnowledgeBase = (id) => fetchJson(`/chat/knowledge/bases/${encodeURIComponent(id)}/probe`, { method: 'POST' });
  const refreshKnowledgeTool = () => fetchJson('/chat/knowledge/tools/refresh', { method: 'POST' });

  const listProviders = () => fetchJson('/chat/providers');
  const createProvider = (payload) => fetchJson('/chat/providers', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const updateProvider = (id, payload) => fetchJson(`/chat/providers/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const deleteProvider = (id) => fetchJson(`/chat/providers/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const activateProvider = (id) => fetchJson(`/chat/providers/${encodeURIComponent(id)}/activate`, { method: 'POST' });

  const listScheduledTasks = () => fetchJson('/chat/scheduled-tasks');
  const getScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}`);
  const createScheduledTask = (payload) => fetchJson('/chat/scheduled-tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const updateScheduledTask = (id, payload) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const listScheduledTaskExecutions = (id, limit) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}/executions?limit=${encodeURIComponent(limit || 10)}`);
  const runScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}/run`, { method: 'POST' });
  const pauseScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}/pause`, { method: 'POST' });
  const resumeScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}/resume`, { method: 'POST' });
  const deleteScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });

  const listPlatforms = (includeLocal) => fetchJson(`/chat/gateways?include_local=${includeLocal ? 'true' : 'false'}`);
  const getPlatform = (platform) => fetchJson(`/chat/gateways/${encodeURIComponent(platform)}`);
  const listPlatformSessions = (platform, limit, offset) => fetchJson(`/chat/gateways/${encodeURIComponent(platform)}/sessions?limit=${encodeURIComponent(limit || 20)}&offset=${encodeURIComponent(offset || 0)}`);

  const listMcpSites = () => fetchJson('/chat/mcp/sites');
  const probeMcpSite = (payload) => fetchJson('/chat/mcp/sites/probe', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const createMcpSite = (payload) => fetchJson('/chat/mcp/sites', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const updateMcpSite = (id, payload) => fetchJson(`/chat/mcp/sites/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const deleteMcpSite = (id) => fetchJson(`/chat/mcp/sites/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const refreshMcpSite = (id) => fetchJson(`/chat/mcp/sites/${encodeURIComponent(id)}/refresh`, { method: 'POST' });
  const listMcpSiteTools = (id) => fetchJson(`/chat/mcp/sites/${encodeURIComponent(id)}/tools`);
  const updateMcpTool = (siteId, toolId, payload) => fetchJson(`/chat/mcp/sites/${encodeURIComponent(siteId)}/tools/${encodeURIComponent(toolId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });

  const listSkills = () => fetchJson('/chat/skills');
  const getSkill = (name) => fetchJson(`/chat/skills/${encodeURIComponent(name)}`);
  const setSkillEnabled = (name, enabled) => fetchJson(`/chat/skills/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !!enabled }),
  });
  const refreshSkills = () => fetchJson('/chat/skills/refresh', { method: 'POST' });

  // Sandbox dashboard endpoints — paths align with sandbox_routes.py / spec
  const getSandboxConfig = () => fetchJson('/chat/sandbox/config');
  const listSandboxActive = () => fetchJson('/chat/sandbox/active');
  const listSandboxReleased = () => fetchJson('/chat/sandbox/released');
  const listSandboxHistory = (sessionId, limit) => fetchJson(
    `/chat/sandbox/execute-code-history?${new URLSearchParams({ ...(sessionId ? { session_id: sessionId } : {}), ...(limit ? { limit: String(limit) } : {}) })}`,
  );
  const deleteSandboxHistory = (toolCallId) => fetchJson(
    `/chat/sandbox/execute-code-history/${encodeURIComponent(toolCallId)}`, { method: 'DELETE' },
  );
  const releaseSandbox = (sessionId) => fetchJson(
    `/chat/sandbox/active/${encodeURIComponent(sessionId)}/release`, { method: 'POST' },
  );
  const sandbox = {
    getConfig: getSandboxConfig,
    listActive: listSandboxActive,
    listReleased: listSandboxReleased,
    listHistory: listSandboxHistory,
    deleteHistory: deleteSandboxHistory,
    releaseSandbox,
  };

  global.NAGENT = namespace;
  global.NAGENT.api = {
    fetchJson,
    listSessions,
    createSession,
    getSessionDetail,
    getSessionToolCalls,
    renameSession,
    deleteSession,
    listTools,
    listModels,
    getAdminModels,
    getHealth,
    getDependencyHealth,
    listKnowledgeBases,
    createKnowledgeBase,
    updateKnowledgeBase,
    deleteKnowledgeBase,
    probeKnowledgeBase,
    probeSavedKnowledgeBase,
    refreshKnowledgeTool,
    listProviders,
    createProvider,
    updateProvider,
    deleteProvider,
    activateProvider,
    listScheduledTasks,
    getScheduledTask,
    createScheduledTask,
    updateScheduledTask,
    listScheduledTaskExecutions,
    runScheduledTask,
    pauseScheduledTask,
    resumeScheduledTask,
    deleteScheduledTask,
    listPlatforms,
    getPlatform,
    listPlatformSessions,
    listMcpSites,
    probeMcpSite,
    createMcpSite,
    updateMcpSite,
    deleteMcpSite,
    refreshMcpSite,
    listMcpSiteTools,
    updateMcpTool,
    listSkills,
    getSkill,
    setSkillEnabled,
    refreshSkills,
    sandbox,
  };
}(window));
