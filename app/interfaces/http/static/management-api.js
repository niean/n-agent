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
  const appendSessionMessage = (id, content) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  });
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

  const listPlatforms = () => fetchJson('/chat/gateways');
  const getPlatform = (platform) => fetchJson(`/chat/gateways/${encodeURIComponent(platform)}`);
  const listPlatformSessions = (platform, limit, offset) => fetchJson(`/chat/gateways/${encodeURIComponent(platform)}/sessions?limit=${encodeURIComponent(limit || 20)}&offset=${encodeURIComponent(offset || 0)}`);

  const listPolicies = () => fetchJson('/chat/policies');

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
  const createSkill = (payload) => fetchJson('/chat/skills', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const updateSkill = (name, payload) => fetchJson(`/chat/skills/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });
  const setSkillEnabled = (name, enabled) => fetchJson(`/chat/skills/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !!enabled }),
  });
  const deleteSkill = (name) => fetchJson(`/chat/skills/${encodeURIComponent(name)}`, { method: 'DELETE' });
  const refreshSkills = () => fetchJson('/chat/skills/refresh', { method: 'POST' });
  const listPendingSkills = () => fetchJson('/chat/skills/pending');
  const getPendingDiff = (pendingId) => fetchJson(`/chat/skills/pending/${encodeURIComponent(pendingId)}/diff`);
  const approvePending = (pendingId) => fetchJson(`/chat/skills/pending/${encodeURIComponent(pendingId)}/approve`, { method: 'POST' });
  const rejectPending = (pendingId) => fetchJson(`/chat/skills/pending/${encodeURIComponent(pendingId)}/reject`, { method: 'POST' });
  const approveAllPending = () => fetchJson('/chat/skills/pending/approve-all', { method: 'POST' });
  const rejectAllPending = () => fetchJson('/chat/skills/pending/reject-all', { method: 'POST' });
  const listSkillUsage = () => fetchJson('/chat/skills/usage');

  // Plugin dashboard endpoints
  const listPlugins = () => fetchJson('/chat/plugins');
  const getPlugin = (key) => fetchJson(`/chat/plugins/${encodeURIComponent(key)}`);
  const refreshPlugins = () => fetchJson('/chat/plugins:refresh', { method: 'POST' });
  const setPluginEnabled = (key, enabled) => fetchJson(`/chat/plugins/${encodeURIComponent(key)}/enabled`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled: !!enabled }),
  });
  const updatePluginConfig = (key, payload) => fetchJson(`/chat/plugins/${encodeURIComponent(key)}/config`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload || {}),
  });

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
  const deleteSandboxReleased = (entryId) => fetchJson(
    `/chat/sandbox/released/${encodeURIComponent(entryId)}`, { method: 'DELETE' },
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
    deleteReleased: deleteSandboxReleased,
    releaseSandbox,
  };

  // Host terminal dashboard endpoints - read-only, no write ops (see host_terminal_routes.py)
  const getHostStatus = () => fetchJson('/chat/host/status');
  const getHostPolicy = () => fetchJson('/chat/host/policy');
  const listHostHistory = (sessionId, limit) => {
    const params = new URLSearchParams();
    if (sessionId) params.set('session_id', sessionId);
    // Only send limit when it is a valid positive integer; never silently
    // rewrite an invalid 0 to the default (let the backend 422 it).
    if (Number.isInteger(limit) && limit >= 1) params.set('limit', String(limit));
    const qs = params.toString();
    return fetchJson(`/chat/host/history${qs ? `?${qs}` : ''}`);
  };
  const host = {
    getStatus: getHostStatus,
    getPolicy: getHostPolicy,
    listHistory: listHostHistory,
  };

  // Usage / observation endpoints
  const getUsageOverview = () => fetchJson(`/chat/usage/overview`);
  const listUsageSessions = (page, pageSize) => fetchJson(
    `/chat/usage/sessions?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`,
  );
  const getUsageStats = (sessionId) => fetchJson(`/chat/usage/sessions/${encodeURIComponent(sessionId)}`);
  const getUsageRecords = (sessionId, limit) => fetchJson(
    `/chat/usage/sessions/${encodeURIComponent(sessionId)}/records${limit ? `?limit=${encodeURIComponent(limit)}` : ''}`,
  );
  const getUsageCompressions = (sessionId) => fetchJson(`/chat/usage/sessions/${encodeURIComponent(sessionId)}/compressions`);
  const getUsageBreakdown = (sessionId) => fetchJson(`/chat/usage/sessions/${encodeURIComponent(sessionId)}/breakdown`);
  const usage = {
    getOverview: getUsageOverview,
    listSessions: listUsageSessions,
    getStats: getUsageStats,
    getRecords: getUsageRecords,
    getCompressions: getUsageCompressions,
    getBreakdown: getUsageBreakdown,
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
    appendSessionMessage,
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
    listPolicies,
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
    createSkill,
    updateSkill,
    setSkillEnabled,
    deleteSkill,
    refreshSkills,
    listPendingSkills,
    getPendingDiff,
    approvePending,
    rejectPending,
    approveAllPending,
    rejectAllPending,
    listSkillUsage,
    listPlugins,
    getPlugin,
    refreshPlugins,
    setPluginEnabled,
    updatePluginConfig,
    sandbox,
    host,
    usage,
    task: {
      board: () => fetchJson('/chat/tasks/board'),
      list: (params) => fetchJson('/chat/tasks' + (params && params.status ? ('?status=' + encodeURIComponent(params.status)) : '')),
      get: (id) => fetchJson('/chat/tasks/' + encodeURIComponent(id)),
      create: (body) => fetchJson('/chat/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      patch: (id, body) => fetchJson('/chat/tasks/' + encodeURIComponent(id), { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      remove: (id) => fetchJson('/chat/tasks/' + encodeURIComponent(id), { method: 'DELETE' }),
      bulk: (body) => fetchJson('/chat/tasks/bulk', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      comment: (id, body) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/comments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      proposeChange: (id, proposal) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/propose-change', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ proposal: proposal }) }),
      approve: (id, note) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/approve', note ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: note }) } : { method: 'POST' }),
      reject: (id, note) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/reject', note ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: note }) } : { method: 'POST' }),
      cancel: (id) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/cancel', { method: 'POST' }),
      retry: (id) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/retry', { method: 'POST' }),
      dispatch: () => fetchJson('/chat/tasks/dispatch', { method: 'POST' }),
      inspect: () => fetchJson('/chat/tasks/inspect'),
      runs: (runId) => fetchJson('/chat/tasks/runs/' + encodeURIComponent(runId)),
      terminateRun: (runId) => fetchJson('/chat/tasks/runs/' + encodeURIComponent(runId) + '/terminate', { method: 'POST' }),
      listAttachments: (id) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/attachments'),
    },
  };
}(window));
