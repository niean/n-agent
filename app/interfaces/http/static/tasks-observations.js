(function (global) {
  'use strict';
  // tasks-observations scoped tab.
  // Lists sessions associated with any task (task worker sessions with
  // source==='task', plus Dashboard /task create origin sessions whose id
  // matches a task's execution/origin session) and shows per-session
  // observation detail, reusing observations.js renderers to keep the layout
  // identical to the global observations page (/observations/sessions).
  // Mount Point: <div class="tab-content" id="tab-tasks-observations"></div>
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  const R = (namespace.observations && namespace.observations.renderers) || {};

  const CONTAINER_ID = 'tab-tasks-observations';
  const FETCH_PAGE_SIZE = 100; // single large page; client-side filter + paginate

  // Async guards:
  // - renderToken: monotonic counter. Each load cycle captures a snapshot;
  //   late responses whose token != current are discarded.
  // - inflight: shared promise for the current load. Concurrent init/refresh
  //   calls return the same promise instead of starting duplicate requests.
  // - selectedSessionId: currently selected session; refresh re-fetches its
  //   detail, or re-fetches the session list when nothing is selected.
  let renderToken = 0;
  let inflight = null;
  let selectedSessionId = null;
  let currentPage = 1;
  let cachedTaskSessions = null;

  function root() {
    return ui.byId ? ui.byId(CONTAINER_ID) : document.getElementById(CONTAINER_ID);
  }

  function isActive() {
    const node = root();
    return !!(node && node.classList && node.classList.contains('active'));
  }

  function isCurrent(token) {
    return token === renderToken && isActive();
  }

  function buildLoading() {
    const panel = ui.el('section', 'status-panel');
    const body = ui.el('div', 'panel-body');
    ui.renderLoading(body, '加载任务观测数据...');
    panel.appendChild(body);
    return panel;
  }

  function buildError(message, onRetry) {
    const panel = ui.el('section', 'status-panel');
    const body = ui.el('div', 'panel-body');
    const err = ui.el('div', 'error-state');
    err.textContent = message || '加载失败';
    body.appendChild(err);
    if (typeof onRetry === 'function') {
      const retryBtn = ui.el('button', 'btn');
      retryBtn.type = 'button';
      retryBtn.textContent = '重试';
      retryBtn.dataset.action = 'retry';
      retryBtn.addEventListener('click', onRetry);
      body.appendChild(retryBtn);
    }
    panel.appendChild(body);
    return panel;
  }

  // Aggregate overview stats from filtered task sessions (client-side
  // aggregation since /chat/usage/overview returns global stats and the
  // backend does not support source filtering).
  function aggregateOverview(sessions) {
    const stats = {
      session_count: sessions.length,
      input_tokens: 0,
      output_tokens: 0,
      cache_read_tokens: 0,
      cache_write_tokens: 0,
      reasoning_tokens: 0,
      total_tokens: 0,
      normalized_tokens: 0,
      api_call_count: 0,
    };
    sessions.forEach(function (s) {
      stats.input_tokens += Number(s.input_tokens || 0);
      stats.output_tokens += Number(s.output_tokens || 0);
      stats.cache_read_tokens += Number(s.cache_read_tokens || 0);
      stats.cache_write_tokens += Number(s.cache_write_tokens || 0);
      stats.reasoning_tokens += Number(s.reasoning_tokens || 0);
      stats.total_tokens += Number(s.total_tokens || 0);
      stats.normalized_tokens += Number(s.normalized_tokens || 0);
      stats.api_call_count += Number(s.api_call_count || 0);
    });
    return stats;
  }

  function renderContent(children) {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    children.forEach(function (c) { node.appendChild(c); });
  }

  // Collect execution session ids from the task board response. A session
  // belongs to a task when it matches task.execution_session_id (explicit) or
  // task.origin_session_id (Dashboard /task create runs the worker inside the
  // origin chat session, source=dashboard). Tasks with neither (kanban/CLI/
  // feishu) fall back to the deterministic task-{uuid5(task.id)} session,
  // which is caught by source==='task' on the session side.
  function collectTaskSessionIds(boardResp) {
    const ids = new Set();
    const columns = (boardResp && boardResp.columns) || [];
    columns.forEach(function (col) {
      const cards = (col && col.cards) || [];
      cards.forEach(function (task) {
        if (!task) return;
        const execId = task.execution_session_id || task.origin_session_id;
        if (execId) ids.add(execId);
      });
    });
    return ids;
  }

  // Detail view back navigation: returns to /tasks/observations index.
  function detailNav() {
    return {
      backHref: '/tasks/observations',
      onBack: function () { selectedSessionId = null; runLoad(function (t) { return loadList(t); }, true); },
    };
  }

  // Render the index view (overview cards + sessions table) from cached task
  // sessions with client-side pagination. No API calls. No top header -- the
  // index view starts directly with the overview cards and sessions table.
  function renderIndexView(token, page) {
    if (!isCurrent(token)) return;
    const node = root();
    if (!node) return;
    const sessions = cachedTaskSessions || [];
    const total = sessions.length;
    const pageSize = R.PAGE_SIZE || 10;
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    page = Math.max(1, Math.min(page || 1, totalPages));
    currentPage = page;
    const start = (page - 1) * pageSize;
    const end = Math.min(start + pageSize, total);
    const pageItems = sessions.slice(start, end);

    node.replaceChildren();

    const overview = aggregateOverview(sessions);
    R.overviewCards(node, overview);
    R.sessionsTable(node, pageItems, page, pageSize, total, {
      onDetail: function (id) { selectSession(id); },
      onPage: function (p) { renderIndexView(token, p); },
    });
  }

  // Load the task session list. Fetch the task board (to enumerate task
  // execution/origin sessions) and a single large page from the usage API in
  // parallel, then filter client-side: a session belongs here when its
  // source==='task' OR its session_id matches any task's execution session
  // (execution_session_id || origin_session_id). The backend does not support
  // source filtering.
  async function loadList(token) {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    node.appendChild(buildLoading());
    let boardResp, sessionsResp;
    try {
      const results = await Promise.all([
        api.task.board(),
        api.usage.listSessions(1, FETCH_PAGE_SIZE),
      ]);
      boardResp = results[0];
      sessionsResp = results[1];
    } catch (err) {
      if (!isCurrent(token)) return;
      renderContent([buildError('加载任务会话失败: ' + (err && err.message ? err.message : err), function () { refresh(); })]);
      return;
    }
    if (!isCurrent(token)) return;
    const taskSessionIds = collectTaskSessionIds(boardResp);
    const allItems = (sessionsResp && sessionsResp.items) || [];
    cachedTaskSessions = allItems.filter(function (s) {
      if (!s) return false;
      return s.source === 'task' || taskSessionIds.has(s.session_id);
    });
    renderIndexView(token, 1);
  }

  // Load observation detail for a session, reusing observations.js renderers:
  // detail header (back link to /tasks/observations) + stats bar + breakdown +
  // records + compressions.
  async function loadDetail(token, sessionId) {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    R.detailHeader(node, sessionId, detailNav());
    node.appendChild(buildLoading());
    let stats, records, compressions, breakdown;
    try {
      const results = await Promise.all([
        api.usage.getStats(sessionId),
        api.usage.getRecords(sessionId, 50),
        api.usage.getCompressions(sessionId),
        api.usage.getBreakdown(sessionId),
      ]);
      stats = results[0];
      records = results[1];
      compressions = results[2];
      breakdown = results[3];
    } catch (err) {
      if (!isCurrent(token)) return;
      node.replaceChildren();
      R.detailHeader(node, sessionId, detailNav());
      node.appendChild(buildError('加载会话观测失败: ' + (err && err.message ? err.message : err), function () { refresh(); }));
      return;
    }
    if (!isCurrent(token)) return;
    node.replaceChildren();
    R.detailHeader(node, sessionId, detailNav());
    R.statsBar(node, stats || {});
    R.breakdown(node, breakdown || {});
    R.records(node, records || []);
    R.compressions(node, compressions || []);
  }

  // Shared load runner with in-flight de-duplication. When force is true the
  // existing in-flight (if any) is superseded -- its result will be discarded
  // by the token guard -- and a new load starts immediately.
  function runLoad(loader, force) {
    if (!force && inflight) return inflight;
    if (force) inflight = null;
    renderToken++;
    const token = renderToken;
    const p = (async function () {
      try { await loader(token); }
      finally { if (inflight === p) inflight = null; }
    })();
    inflight = p;
    return p;
  }

  function selectSession(id) {
    selectedSessionId = id;
    return runLoad(function (token) { return loadDetail(token, id); }, true);
  }

  async function init() {
    selectedSessionId = null;
    currentPage = 1;
    cachedTaskSessions = null;
    return runLoad(function (token) { return loadList(token); });
  }

  async function refresh() {
    const sid = selectedSessionId;
    if (sid) {
      return runLoad(function (token) { return loadDetail(token, sid); });
    }
    return runLoad(function (token) { return loadList(token); });
  }

  function deactivate() {
    selectedSessionId = null;
    currentPage = 1;
    cachedTaskSessions = null;
    renderToken++;
    inflight = null;
  }

  namespace.tasksObservations = { init: init, refresh: refresh, deactivate: deactivate };
  global.NAGENT = namespace;
}(window));
