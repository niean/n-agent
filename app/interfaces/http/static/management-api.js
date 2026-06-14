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
    listProviders,
    createProvider,
    updateProvider,
    deleteProvider,
    activateProvider,
  };
}(window));
