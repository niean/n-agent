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
  const listTools = () => fetchJson('/chat/tools');
  const listModels = () => fetchJson('/v1/models');
  const getAdminModels = () => fetchJson('/chat/models');
  const getHealth = () => fetchJson('/health');
  const getDependencyHealth = () => fetchJson('/chat/health/dependencies');

  global.NAGENT = namespace;
  global.NAGENT.api = {
    fetchJson,
    listSessions,
    createSession,
    getSessionDetail,
    getSessionToolCalls,
    listTools,
    listModels,
    getAdminModels,
    getHealth,
    getDependencyHealth,
  };
}(window));
