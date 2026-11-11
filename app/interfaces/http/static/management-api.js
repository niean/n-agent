(function (global) {
  const namespace = global.NAGENT || {};

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const data = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      const code = data && data.error && data.error.code ? data.error.code : 'request_failed';
      const error = new Error(code);
      // 业务错误正文挂 error.data（如 422 incomparable_experiments 的
      // diff_dimensions），供消费方展示可公开差异维度
      error.data = data;
      throw error;
    }
    return data;
  }

  // 评估管理面查询串（T21）：跳过空值，值 encodeURIComponent。
  function buildEvaluationQuery(params) {
    if (!params) return '';
    const parts = Object.keys(params).filter((key) => {
      const value = params[key];
      return value !== undefined && value !== null && value !== '';
    }).map((key) => key + '=' + encodeURIComponent(params[key]));
    return parts.length ? ('?' + parts.join('&')) : '';
  }

  const listSessions = () => fetchJson('/chat/sessions');
  const createSession = (id) => fetchJson(`/chat/sessions?session_id=${encodeURIComponent(id)}`, { method: 'POST' });
  const getSessionDetail = (id) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}`);
  const getSessionToolCalls = (id) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}/tool-calls`);
  // T5(Run footer): Run 执行账本读取（数组 DTO 直接消费，不假定 {items:...} 包装）
  const listSessionRuns = (id) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}/runs`);
  const getSessionRun = (id, runId) => fetchJson(`/chat/sessions/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}`);
  const getOtelLinks = () => fetchJson('/chat/observations/otel/links');
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
  // 跨任务执行记录查询（观测页）：无过滤省略 task_id，默认 limit=50。
  const listScheduledTaskExecutionsAll = ({ task_id, limit = 50 } = {}) => {
    const params = new URLSearchParams();
    if (task_id) params.set('task_id', task_id);
    params.set('limit', String(limit));
    const qs = params.toString();
    return fetchJson(`/chat/scheduled-tasks/executions${qs ? `?${qs}` : ''}`);
  };
  const runScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}/run`, { method: 'POST' });
  const pauseScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}/pause`, { method: 'POST' });
  const resumeScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}/resume`, { method: 'POST' });
  const deleteScheduledTask = (id) => fetchJson(`/chat/scheduled-tasks/${encodeURIComponent(id)}`, { method: 'DELETE' });

  const listPlatforms = () => fetchJson('/chat/gateways');
  const getPlatform = (platform) => fetchJson(`/chat/gateways/${encodeURIComponent(platform)}`);
  const listGatewaySessions = (platform, { limit = 20, offset = 0 } = {}) => fetchJson(`/chat/gateways/${encodeURIComponent(platform)}/sessions?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`);
  const getGatewaySession = (platform, id) => fetchJson(`/chat/gateways/${encodeURIComponent(platform)}/sessions/${encodeURIComponent(id)}`);
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
  const setSkillChatSelectable = (name, value) => fetchJson(`/chat/skills/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chat_selectable: !!value }),
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

  // Host terminal dashboard views and automatic grant revocation (see host_terminal_routes.py)
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
    listScriptGrants: () => fetchJson('/chat/host/script-grants'),
    revokeScriptGrant: (revision) => fetchJson(`/chat/host/script-grants/${encodeURIComponent(revision)}/revoke`, { method: 'POST' }),
    getStatus: getHostStatus,
    getPolicy: getHostPolicy,
    listHistory: listHostHistory,
  };

  // Browser dashboard endpoints (T16). Write ops carry X-Browser-Challenge
  // header (one-time token from write_challenges in session detail).
  const listBrowserSessions = (nAgentSessionId) => fetchJson(
    `/chat/browser/sessions?n_agent_session_id=${encodeURIComponent(nAgentSessionId)}`,
  );
  const getBrowserSession = (browserSessionId, nAgentSessionId) => fetchJson(
    `/chat/browser/sessions/${encodeURIComponent(browserSessionId)}?n_agent_session_id=${encodeURIComponent(nAgentSessionId)}`,
  );
  const listBrowserActions = (browserSessionId, nAgentSessionId, limit) => {
    const params = new URLSearchParams();
    params.set('n_agent_session_id', nAgentSessionId);
    if (Number.isInteger(limit) && limit >= 1) params.set('limit', String(limit));
    return fetchJson(`/chat/browser/sessions/${encodeURIComponent(browserSessionId)}/actions?${params.toString()}`);
  };
  const getBrowserTakeoverView = (browserSessionId, nAgentSessionId) => fetchJson(
    `/chat/browser/sessions/${encodeURIComponent(browserSessionId)}/takeover-view?n_agent_session_id=${encodeURIComponent(nAgentSessionId)}`,
  );
  const _BROWSER_OP_MAP = {
    pause: { method: 'POST', suffix: 'pause' },
    resume: { method: 'POST', suffix: 'resume' },
    takeover: { method: 'POST', suffix: 'takeover' },
    release: { method: 'POST', suffix: 'release' },
    close: { method: 'POST', suffix: 'close' },
    host_grant: { method: 'POST', suffix: 'host-grant' },
    revoke_host: { method: 'DELETE', suffix: 'host-grant' },
  };
  const browserWrite = (op, browserSessionId, nAgentSessionId, token, body) => {
    const m = _BROWSER_OP_MAP[op];
    if (!m) throw new Error('invalid_browser_op');
    const url = `/chat/browser/sessions/${encodeURIComponent(browserSessionId)}/${m.suffix}?n_agent_session_id=${encodeURIComponent(nAgentSessionId)}`;
    const opts = {
      method: m.method,
      headers: { 'X-Browser-Challenge': token },
    };
    if (m.method === 'POST' && body) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetchJson(url, opts);
  };
  const browser = {
    listSessions: listBrowserSessions,
    getSession: getBrowserSession,
    listActions: listBrowserActions,
    getTakeoverView: getBrowserTakeoverView,
    write: browserWrite,
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
    listSessionRuns,
    getSessionRun,
    getOtelLinks,
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
    listScheduledTaskExecutionsAll,
    runScheduledTask,
    pauseScheduledTask,
    resumeScheduledTask,
    deleteScheduledTask,
    listPlatforms,
    getPlatform,
    listPlatformSessions,
    listGatewaySessions,
    getGatewaySession,
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
    setSkillChatSelectable,
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
    browser,
    usage,
    // 评估闭环管理面（T21）：/chat/evaluations/* 同源调用
    evaluations: {
      status: () => fetchJson('/chat/evaluations/status'),
      listDatasets: () => fetchJson('/chat/evaluations/datasets'),
      getDataset: (id, version) => fetchJson('/chat/evaluations/datasets/' + encodeURIComponent(id) + '/' + encodeURIComponent(version)),
      importDataset: (body) => fetchJson('/chat/evaluations/datasets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
      overview: () => fetchJson('/chat/evaluations/overview'),
      listExperiments: (params) => fetchJson('/chat/evaluations/experiments' + buildEvaluationQuery(params)),
      getExperiment: (id) => fetchJson('/chat/evaluations/experiments/' + encodeURIComponent(id)),
      createExperiment: (body) => fetchJson('/chat/evaluations/experiments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      listResults: (params) => fetchJson('/chat/evaluations/results' + buildEvaluationQuery(params)),
      getResult: (id) => fetchJson('/chat/evaluations/results/' + encodeURIComponent(id)),
      submitFeedback: (body) => fetchJson('/chat/evaluations/feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      getConfig: () => fetchJson('/chat/evaluations/config'),
      patchConfig: (body) => fetchJson('/chat/evaluations/config', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      retryDelivery: (body) => fetchJson('/chat/evaluations/deliveries/retry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      listReviewQueue: (params) => fetchJson('/chat/evaluations/review-queue' + buildEvaluationQuery(params)),
      exportReviewQueue: (body) => fetchJson('/chat/evaluations/review-queue/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      // 评估器管理（Task 11/spec 4.2-4.3）：能力目录 + 定义列表/详情/
      // 版本创建 + 单版本在线启停（CAS expected_revision）
      listEvaluators: (params) => fetchJson('/chat/evaluations/evaluators' + buildEvaluationQuery(params)),
      getEvaluator: (id, version) => fetchJson('/chat/evaluations/evaluators/' + encodeURIComponent(id) + '/' + encodeURIComponent(version)),
      createEvaluator: (body) => fetchJson('/chat/evaluations/evaluators', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      setEvaluatorUsage: (id, version, body) => fetchJson('/chat/evaluations/evaluators/' + encodeURIComponent(id) + '/' + encodeURIComponent(version) + '/usage', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      getCapabilities: () => fetchJson('/chat/evaluations/evaluators/capabilities'),
      // 人工标注（Task 12/spec 4.4）：标注队列/上下文/受约束提交（request_key
      // 重放 + expected_revision CAS；actor 由服务端注入，不在载荷传递）
      listAnnotations: (params) => fetchJson('/chat/evaluations/annotations' + buildEvaluationQuery(params)),
      getAnnotation: (sourceResultId) => fetchJson('/chat/evaluations/annotations/' + encodeURIComponent(sourceResultId)),
      submitAnnotation: (sourceResultId, body) => fetchJson('/chat/evaluations/annotations/' + encodeURIComponent(sourceResultId), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
      // 概览分析（Task 13/spec 4.5）：时间窗/过滤聚合与 UTC+8 日期桶
      getAnalytics: (params) => fetchJson('/chat/evaluations/analytics' + buildEvaluationQuery(params)),
      // 只读实验对比（Task 13/spec 4.6）：{candidate_id,baseline_id,policy?}
      compareExperiments: (body) => fetchJson('/chat/evaluations/experiments/compare', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
    },
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
      revise: (id, note) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/revise', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: note }) }),
      dispatch: () => fetchJson('/chat/tasks/dispatch', { method: 'POST' }),
      inspect: () => fetchJson('/chat/tasks/inspect'),
      runs: (runId) => fetchJson('/chat/tasks/runs/' + encodeURIComponent(runId)),
      terminateRun: (runId) => fetchJson('/chat/tasks/runs/' + encodeURIComponent(runId) + '/terminate', { method: 'POST' }),
      listAttachments: (id) => fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/attachments'),
      uploadAttachment: (id, file) => { const fd = new FormData(); fd.append('file', file); return fetchJson('/chat/tasks/' + encodeURIComponent(id) + '/attachments', { method: 'POST', body: fd }); },
      security: () => fetchJson('/chat/tasks/security'),
      securityConfig: {
        get: () => fetchJson('/chat/tasks/security/config'),
        update: (payload) => fetchJson('/chat/tasks/security/config', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}) }),
      },
    },
  };
}(window));
