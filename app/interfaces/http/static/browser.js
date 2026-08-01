(function (global) {
  'use strict';
  // Browser Dashboard page controller (T16).
  // 2-column detail layout: main view (screenshot / safe URL / title) |
  // side panel (control matrix by status + cleaned action history).
  //
  // Security:
  // - ALL untrusted text (URL/title/action summary/errors) rendered via
  //   textContent ONLY. No innerHTML / insertAdjacentHTML / document.write.
  // - takeover-view URL fetched on demand, used only as iframe src; NEVER
  //   written to localStorage or rendered as visible text.
  // - Write ops carry X-Browser-Challenge header from write_challenges.
  // - Polling stops on unmount (deactivate) and on takeover; resumes on release.
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  const modal = (namespace.modal || {});

  const DEFAULT_POLL_MS = 1000;
  const STALE_THRESHOLD_MS = 60000;

  let state = {
    nAgentSessions: [],
    browserSessionsByNagent: {},
    selectedBrowserSessionId: null,
    selectedNagentId: null,
    sessionDetail: null,
    actions: [],
    takeoverView: null,
    lastError: null,
    pollMs: DEFAULT_POLL_MS,
    // Control-relevant signature last rendered into the side panel
    // (status + available write-challenge keys) and the last rendered
    // action-history signature (count + latest action id). The side panel is
    // re-rendered only when one of these changes.
    lastControlSig: null,
    lastActionSig: null,
  };

  let pollTimer = null;
  let pollGeneration = 0;
  let pollInFlight = false;
  let screenshotBust = 0;

  function root() {
    return ui.byId ? ui.byId('tab-browser') : document.getElementById('tab-browser');
  }

  function browserApi() {
    return (api && api.browser) ? api.browser : null;
  }

  function queryParam(name) {
    try {
      const search = global.location && global.location.search;
      const match = new RegExp('(?:[?&])' + name + '=([^&]*)').exec(search || '');
      return match ? decodeURIComponent(match[1]) : '';
    } catch (_) {
      return '';
    }
  }

  // ---- helpers ----

  function stripQueryFragment(url) {
    if (typeof url !== 'string' || !url) return '';
    let u = url;
    const qIdx = u.indexOf('?');
    if (qIdx !== -1) u = u.slice(0, qIdx);
    const fIdx = u.indexOf('#');
    if (fIdx !== -1) u = u.slice(0, fIdx);
    return u;
  }

  function statusLabel(status) {
    const map = {
      pending_authorization: '待授权',
      active: '运行中',
      paused: '已暂停',
      takeover: '已接管',
      degraded: '降级',
      closed: '已关闭',
    };
    return map[status] || status || '-';
  }

  function backendLabel(backend) {
    if (backend === 'container') return '容器';
    if (backend === 'host_cdp') return '本机';
    return backend || '-';
  }

  // 实时视图标题：实时视图(状态|后端)；无会话时回退为 实时视图
  function mainTitleText(detail) {
    if (!detail) return '实时视图';
    return '实时视图(' + statusLabel(detail.status) + '|' + backendLabel(detail.backend_type) + ')';
  }

  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    const tz = new Date(d.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return tz.getUTCFullYear() + '-' + pad(tz.getUTCMonth() + 1) + '-' + pad(tz.getUTCDate())
      + ' ' + pad(tz.getUTCHours()) + ':' + pad(tz.getUTCMinutes()) + ':' + pad(tz.getUTCSeconds());
  }

  function formatDuration(ms) {
    const n = Number(ms || 0);
    if (!isFinite(n) || n <= 0) return '-';
    if (n < 1000) return n + 'ms';
    return (n / 1000).toFixed(1) + 's';
  }

  function formatNumber(value) {
    const n = Number(value || 0);
    if (!isFinite(n)) return '0';
    return n.toLocaleString();
  }

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function button(label, className, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className || 'btn';
    btn.textContent = label;
    if (typeof onClick === 'function') btn.addEventListener('click', onClick);
    return btn;
  }

  // ---- main view ----

  function buildMainPanel() {
    const panel = el('section', 'status-panel browser-main');
    const header = el('div', 'panel-header');
    const title = el('span');
    title.id = 'browser-main-title';
    title.textContent = '实时视图';
    header.appendChild(title);
    const body = el('div', 'panel-body');
    body.id = 'browser-main-body';
    panel.append(header, body);
    return panel;
  }

  function renderMain() {
    const body = ui.byId('browser-main-body');
    if (!body) return;
    const detail = state.sessionDetail;
    const titleEl = ui.byId('browser-main-title');
    if (titleEl) titleEl.textContent = mainTitleText(detail);
    const mode = mainViewMode(detail);
    if (body.dataset.renderMode === mode && updateMainInPlace(detail, mode)) return;

    body.replaceChildren();
    body.dataset.renderMode = mode;
    if (!detail) {
      ui.renderEmpty(body, '未找到浏览器会话');
      return;
    }
    const status = detail.status;
    if (status === 'takeover') {
      body.appendChild(renderTakeoverView(detail));
    } else if (status === 'closed') {
      const msg = el('div', 'browser-screenshot-placeholder');
      msg.textContent = '会话已关闭，无实时截图';
      body.appendChild(msg);
    } else {
      body.appendChild(renderScreenshot(detail));
      body.appendChild(renderPageInfo());
    }
  }

  function mainViewMode(detail) {
    if (!detail) return 'empty';
    if (detail.status === 'takeover') return 'takeover';
    if (detail.status === 'closed') return 'closed';
    return 'live';
  }

  function updateMainInPlace(detail, mode) {
    if (!detail || mode === 'empty') return false;

    // A takeover iframe owns its own focus/caret state. Keeping the same node
    // is essential: rebuilding it reloads the remote page and loses the caret.
    if (mode !== 'live') return true;
    const screenshot = ui.byId('browser-live-screenshot');
    if (!screenshot) return false;
    screenshot.src = screenshotUrl(detail);
    return true;
  }

  function renderScreenshot(detail) {
    const wrap = el('div', 'browser-screenshot-wrap');
    const status = detail.status;
    if (status === 'closed' || status === 'takeover') {
      const placeholder = el('div', 'browser-screenshot-placeholder');
      placeholder.textContent = '此状态下无截图';
      wrap.appendChild(placeholder);
      return wrap;
    }
    const img = document.createElement('img');
    img.className = 'browser-screenshot';
    img.id = 'browser-live-screenshot';
    img.alt = '浏览器截图';
    img.src = screenshotUrl(detail);
    let fallback = null;
    img.addEventListener('load', () => {
      wrap.classList.remove('browser-screenshot-wrap--errored');
      if (fallback) {
        fallback.remove();
        fallback = null;
      }
    });
    img.addEventListener('error', () => {
      if (wrap.classList.contains('browser-screenshot-wrap--errored')) return;
      wrap.classList.add('browser-screenshot-wrap--errored');
      fallback = el('div', 'browser-screenshot-placeholder');
      fallback.textContent = '截图暂不可用';
      wrap.appendChild(fallback);
    });
    wrap.appendChild(img);
    // stale marker
    const latestAction = latestActionWithTime();
    if (latestAction && latestAction.created_at) {
      const actionTime = new Date(latestAction.created_at).getTime();
      const now = Date.now();
      if (now - actionTime > STALE_THRESHOLD_MS) {
        const stale = el('div', 'browser-screenshot-stale');
        stale.textContent = '截图可能已过期 (最后活动: ' + formatTime(latestAction.created_at) + ')';
        wrap.appendChild(stale);
      }
    }
    return wrap;
  }

  function screenshotUrl(detail) {
    const sid = encodeURIComponent(detail.id);
    const nid = encodeURIComponent(state.selectedNagentId || '');
    screenshotBust++;
    return '/chat/browser/sessions/' + sid + '/screenshot?n_agent_session_id=' + nid + '&_t=' + screenshotBust;
  }

  function renderPageInfo() {
    const wrap = el('div', 'browser-page-info');
    const latest = latestActionWithUrl();
    const urlRow = el('div', 'browser-page-info__row');
    const urlLabel = el('span', 'browser-page-info__label');
    urlLabel.textContent = 'URL';
    const urlVal = el('span', 'browser-page-info__value');
    urlVal.textContent = stripQueryFragment(latest && latest.safe_url);
    urlRow.append(urlLabel, urlVal);
    const titleRow = el('div', 'browser-page-info__row');
    const titleLabel = el('span', 'browser-page-info__label');
    titleLabel.textContent = '标题';
    const titleVal = el('span', 'browser-page-info__value');
    titleVal.textContent = (latest && latest.title) || '-';
    titleRow.append(titleLabel, titleVal);
    wrap.append(urlRow, titleRow);
    return wrap;
  }

  function renderTakeoverView(detail) {
    const wrap = el('div', 'browser-takeover');
    const tv = state.takeoverView;
    if (detail.backend_type === 'container' && tv && tv.url) {
      // Container: show interactive remote view via iframe.
      // URL is NOT rendered as text, NOT stored in localStorage.
      const iframe = document.createElement('iframe');
      iframe.className = 'browser-takeover__iframe';
      iframe.src = tv.url;
      iframe.setAttribute('title', '接管交互视图');
      iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-forms');
      wrap.appendChild(iframe);
      const notice = el('div', 'browser-takeover__notice muted');
      if (tv.expires_at) {
        notice.textContent = '交互视图有效期至: ' + formatTime(tv.expires_at);
      } else {
        notice.textContent = '交互视图已就绪';
      }
      wrap.appendChild(notice);
    } else {
      // Host CDP or no URL: prompt to use managed Chrome directly.
      const msg = el('div', 'browser-takeover__message');
      msg.textContent = (tv && tv.message) || '本机接管: 请直接使用受管理的 Chrome 浏览器';
      wrap.appendChild(msg);
    }
    return wrap;
  }

  function latestActionWithUrl() {
    const actions = state.actions || [];
    for (let i = actions.length - 1; i >= 0; i--) {
      const a = actions[i];
      if (a && (a.safe_url || a.title)) return a;
    }
    return actions.length > 0 ? actions[actions.length - 1] : null;
  }

  function latestActionWithTime() {
    const actions = state.actions || [];
    for (let i = actions.length - 1; i >= 0; i--) {
      if (actions[i] && actions[i].created_at) return actions[i];
    }
    return null;
  }

  // ---- controls + action history (below the main view) ----

  function buildSidePanel() {
    const panel = el('section', 'status-panel browser-side');
    const header = el('div', 'panel-header');
    const title = el('span');
    title.textContent = '控制与历史';
    header.appendChild(title);
    const body = el('div', 'panel-body');
    body.id = 'browser-side-body';
    panel.append(header, body);
    return panel;
  }

  // The side panel has no manual refresh button: it is driven by the
  // realtime-view poll timer. Action history is fetched every tick; the panel
  // is re-rendered only when a signature changes, and only the changed section
  // (controls or action history) is rebuilt so unchanged DOM (scroll position,
  // focus) is preserved -- no full-panel flicker.
  function controlSignature(detail) {
    if (!detail) return '';
    const challenges = detail.write_challenges || {};
    const keys = Object.keys(challenges).sort();
    return (detail.status || '-') + '|' + keys.join(',');
  }

  function actionSignature(actions) {
    const list = actions || [];
    if (list.length === 0) return '0|';
    const last = list[list.length - 1];
    return list.length + '|' + (last && last.id ? last.id : '');
  }

  // Partial in-place refresh of the side panel body. Only sections whose flag
  // is true are rebuilt; the others are reused as-is (re-attached, which in a
  // real browser preserves the existing node, its descendants and scroll).
  function renderSidePartial(rebuildControls, rebuildActions) {
    const body = ui.byId('browser-side-body');
    if (!body) return;
    const detail = state.sessionDetail;
    if (!detail) {
      body.replaceChildren();
      ui.renderEmpty(body, '无会话');
      return;
    }
    const cur = body.children || [];
    const hasControls = cur[0] && String(cur[0].className || '').indexOf('browser-controls') !== -1;
    const hasActions = cur[1] && String(cur[1].className || '').indexOf('browser-actions') !== -1;
    const controlsNode = (!rebuildControls && hasControls) ? cur[0] : renderControls(detail);
    const actionsNode = (!rebuildActions && hasActions) ? cur[1] : renderActionHistory();
    body.replaceChildren(controlsNode, actionsNode);
  }

  function renderSide() {
    const body = ui.byId('browser-side-body');
    if (!body) return;
    body.replaceChildren();
    const detail = state.sessionDetail;
    if (!detail) {
      ui.renderEmpty(body, '无会话');
      return;
    }
    body.appendChild(renderControls(detail));
    body.appendChild(renderActionHistory());
  }

  function renderControls(detail) {
    const wrap = el('div', 'browser-controls');
    const title = el('div', 'browser-controls__title');
    title.textContent = '操作';
    wrap.appendChild(title);
    const controls = controlsForStatus(detail);
    if (controls.length === 0) {
      const empty = el('div', 'muted');
      empty.textContent = '当前状态无可用操作';
      wrap.appendChild(empty);
      return wrap;
    }
    const btnRow = el('div', 'row-actions');
    controls.forEach((c) => {
      const cls = c.danger ? 'btn btn--danger' : 'btn';
      const btn = button(c.label, cls, () => executeWrite(c.op));
      btnRow.appendChild(btn);
    });
    wrap.appendChild(btnRow);
    return wrap;
  }

  function controlsForStatus(detail) {
    const status = detail.status;
    const challenges = detail.write_challenges || {};
    const controls = [];
    if (status === 'pending_authorization') {
      if (challenges.host_grant) controls.push({ op: 'host_grant', label: '主机授权' });
      if (challenges.close) controls.push({ op: 'close', label: '关闭', danger: true });
    } else if (status === 'active') {
      if (challenges.pause) controls.push({ op: 'pause', label: '暂停' });
      if (challenges.takeover) controls.push({ op: 'takeover', label: '接管', danger: true });
      if (challenges.close) controls.push({ op: 'close', label: '关闭', danger: true });
    } else if (status === 'paused') {
      if (challenges.resume) controls.push({ op: 'resume', label: '恢复' });
      if (challenges.takeover) controls.push({ op: 'takeover', label: '接管', danger: true });
      if (challenges.close) controls.push({ op: 'close', label: '关闭', danger: true });
    } else if (status === 'takeover') {
      if (challenges.release) controls.push({ op: 'release', label: '释放' });
      if (challenges.close) controls.push({ op: 'close', label: '关闭', danger: true });
    } else if (status === 'degraded') {
      if (challenges.close) controls.push({ op: 'close', label: '关闭', danger: true });
    }
    // closed: no controls
    return controls;
  }

  function renderActionHistory() {
    const wrap = el('div', 'browser-actions');
    const title = el('div', 'browser-actions__title');
    title.textContent = '操作历史';
    wrap.appendChild(title);
    const actions = state.actions || [];
    if (actions.length === 0) {
      const empty = el('div', 'muted');
      empty.textContent = '暂无操作记录';
      wrap.appendChild(empty);
      return wrap;
    }
    // 侧列仅 20% 宽度，5 列表格不再合适；改用紧凑纵向列表，最新在前。
    // 仅展示清洗后的摘要（action_type/status/duration/error_code），全部 textContent 渲染。
    const list = el('div', 'browser-actions-list');
    for (let i = actions.length - 1; i >= 0; i--) {
      const a = actions[i];
      if (!a) continue;
      list.appendChild(renderActionItem(a));
    }
    wrap.appendChild(list);
    return wrap;
  }

  function renderActionItem(a) {
    const item = el('div', 'browser-actions-item');
    const head = el('div', 'browser-actions-item__head');
    const type = el('span', 'browser-actions-item__type');
    type.textContent = a.action_type || '-';
    const badge = el('span', 'badge badge--' + actionStatusBadge(a.status));
    badge.textContent = a.status || '-';
    head.append(type, badge);
    item.appendChild(head);
    const time = el('div', 'browser-actions-item__time muted');
    time.textContent = formatTime(a.created_at);
    item.appendChild(time);
    const parts = [];
    const dur = formatDuration(a.duration_ms);
    if (dur && dur !== '-') parts.push(dur);
    if (a.error_code) parts.push(a.error_code);
    if (parts.length > 0) {
      const extra = el('div', 'browser-actions-item__extra muted');
      extra.textContent = parts.join(' · ');
      item.appendChild(extra);
    }
    return item;
  }

  function actionStatusBadge(status) {
    if (status === 'success') return 'success';
    if (status === 'error' || status === 'timeout') return 'danger';
    return 'warning';
  }

  // ---- polling ----

  function startPolling() {
    stopPolling();
    if (!state.selectedBrowserSessionId) return;
    pollGeneration++;
    const gen = pollGeneration;
    pollTimer = setInterval(() => { pollTick(gen); }, state.pollMs);
    pollTick(gen);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    pollGeneration++;
    pollInFlight = false;
  }

  async function pollTick(gen) {
    if (gen !== pollGeneration) return;
    if (!state.selectedBrowserSessionId || pollInFlight) return;
    pollInFlight = true;
    const sid = state.selectedBrowserSessionId;
    const nid = state.selectedNagentId;
    const bapi = browserApi();
    if (!bapi) { pollInFlight = false; return; }
    try {
      const detail = await bapi.getSession(sid, nid);
      if (gen !== pollGeneration) return;
      state.sessionDetail = detail;
      // Fetch action history every tick so any new action (click/type/navigate)
      // is detected promptly. The side panel is re-rendered only when a
      // signature actually changes -- controls when status/available write
      // challenges change, history when count+latest-id change -- so idle ticks
      // skip the render entirely (no flicker). Fetching before renderMain keeps
      // the main view's page info in sync with the latest action.
      const actionsResult = await bapi.listActions(sid, nid);
      if (gen !== pollGeneration) return;
      state.actions = (actionsResult && actionsResult.actions) || [];
      const cSig = controlSignature(detail);
      const aSig = actionSignature(state.actions);
      const rebuildControls = cSig !== state.lastControlSig;
      const rebuildActions = aSig !== state.lastActionSig;
      // Fetch takeover-view URL on-demand when entering takeover
      if (detail.status === 'takeover' && !state.takeoverView) {
        try {
          state.takeoverView = await bapi.getTakeoverView(sid, nid);
        } catch (_) {
          state.takeoverView = { url: null, message: '接管视图不可用', expires_at: null };
        }
      } else if (detail.status !== 'takeover') {
        state.takeoverView = null;
      }
      if (gen !== pollGeneration) return;
      renderMain();
      if (rebuildControls || rebuildActions) {
        renderSidePartial(rebuildControls, rebuildActions);
      }
      state.lastControlSig = cSig;
      state.lastActionSig = aSig;
      if (detail.status === 'closed') stopPolling();
    } catch (e) {
      if (gen !== pollGeneration) return;
      state.lastError = e;
      // If session not found, stop polling and refresh list
      if (e && e.message === 'browser_session_not_found') {
        stopPolling();
        state.sessionDetail = null;
        state.selectedBrowserSessionId = null;
        state.lastControlSig = null;
        state.lastActionSig = null;
        renderMain();
        renderSide();
      }
    } finally {
      if (gen === pollGeneration) pollInFlight = false;
    }
  }

  // ---- write actions ----

  async function executeWrite(op) {
    const detail = state.sessionDetail;
    if (!detail || !detail.write_challenges) return;
    const token = detail.write_challenges[op];
    if (!token) return;
    const bapi = browserApi();
    if (!bapi) return;
    try {
      await bapi.write(op, detail.id, state.selectedNagentId, token);
    } catch (e) {
      if (modal && typeof modal.alert === 'function') {
        await modal.alert('操作失败: ' + (e && e.message ? e.message : e));
      }
    }
    // A write (success or failure) invalidates challenges and may change
    // status. Reset the side-panel signatures so the next poll forces a
    // partial rebuild of the changed sections. A write also invalidates the
    // current generation; recreate the interval (merely incrementing
    // pollGeneration leaves the old timer alive but permanently inert after
    // Release) -- startPolling fires an immediate pollTick for the refresh.
    state.lastControlSig = null;
    state.lastActionSig = null;
    startPolling();
  }

  // ---- session selection ----

  function selectSession(browserSessionId, nagentId) {
    if (state.selectedBrowserSessionId === browserSessionId) return;
    state.selectedBrowserSessionId = browserSessionId;
    state.selectedNagentId = nagentId;
    state.sessionDetail = null;
    state.actions = [];
    state.takeoverView = null;
    state.lastError = null;
    state.lastControlSig = null;
    state.lastActionSig = null;
    renderMain();
    renderSide();
    startPolling();
  }

  // ---- lifecycle ----

  async function load() {
    const node = root();
    if (!node) return;
    const bapi = browserApi();
    if (!bapi) {
      node.replaceChildren();
      ui.renderError(node, '浏览器 Dashboard API 不可用');
      return;
    }
    node.replaceChildren();
    ui.renderLoading(node, '加载浏览器会话...');
    try {
      const sessions = await api.listSessions();
      state.nAgentSessions = sessions || [];
      const byNagent = {};
      await Promise.all((sessions || []).map(async (s) => {
        const nid = s.id || s.session_id || '';
        if (!nid) return;
        try {
          const result = await bapi.listSessions(nid);
          byNagent[nid] = (result && result.sessions) || [];
        } catch (_) {
          byNagent[nid] = [];
        }
      }));
      state.browserSessionsByNagent = byNagent;
      node.replaceChildren();
      const urlNagent = queryParam('nagent');
      // 执行器入口显示跨会话的浏览器工具执行历史；详情页根据 URL 定位会话。
      if (!urlNagent) {
        const history = await loadExecutionHistory(byNagent);
        buildExecutionHistoryPage(node, history);
      } else {
        buildPage(node);
        renderMain();
        renderSide();
        const matched = (sessions || []).find((s) => {
          const nid = s.id || s.session_id || '';
          return nid === urlNagent && (byNagent[nid] || []).length > 0;
        });
        if (matched) {
          const nid = matched.id || matched.session_id;
          const requestedBrowserSessionId = queryParam('browser_session_id');
          const selected = (byNagent[nid] || []).find((session) => (
            session.id === requestedBrowserSessionId
          )) || byNagent[nid][0];
          selectSession(selected.id, nid);
        }
      }
    } catch (err) {
      node.replaceChildren();
      ui.renderError(node, '加载浏览器会话失败: ' + (err && err.message ? err.message : err));
    }
  }

  function buildPage(node) {
    const shell = el('div', 'browser-shell');
    shell.append(buildMainPanel(), buildSidePanel());
    node.appendChild(shell);
  }

  function openBrowserDetail(nagentId, browserSessionId) {
    const query = 'nagent=' + encodeURIComponent(nagentId)
      + '&browser_session_id=' + encodeURIComponent(browserSessionId);
    global.location.href = '/browser/session?' + query;
  }

  function buildExecutionHistoryPage(node, history) {
    const panel = el('section', 'status-panel browser-history');
    const header = el('div', 'panel-header');
    const title = el('span');
    title.textContent = '执行历史';
    header.appendChild(title);
    const body = el('div', 'panel-body');
    panel.append(header, body);
    node.appendChild(panel);

    if (!history.length) {
      ui.renderEmpty(body, '无浏览器执行历史');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table browser-history-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['Session', '浏览器 Session', '类型', '操作次数', '状态', '最后操作', '操作'].forEach((label) => {
      const th = document.createElement('th');
      if (label === '操作次数') th.className = 'document-table__numeric';
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    history.forEach((entry) => {
      const row = document.createElement('tr');
      [entry.n_agent_session_id || '-', entry.browser_session_id || '-'].forEach((value) => {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.appendChild(cell);
      });
      const backendCell = document.createElement('td');
      backendCell.textContent = backendLabel(entry.backend_type);
      row.appendChild(backendCell);
      const actionCount = document.createElement('td');
      actionCount.className = 'document-table__numeric';
      actionCount.textContent = formatNumber(entry.action_count);
      row.appendChild(actionCount);
      [entry.status || '-', formatTime(entry.updated_at || entry.created_at)].forEach((value) => {
        const cell = document.createElement('td');
        cell.textContent = value;
        row.appendChild(cell);
      });
      const actions = document.createElement('td');
      actions.className = 'row-actions';
      actions.appendChild(button('详情', 'btn', () => openBrowserDetail(
        entry.n_agent_session_id, entry.browser_session_id,
      )));
      row.appendChild(actions);
      tbody.appendChild(row);
    });
    table.append(thead, tbody);
    body.appendChild(table);
  }

  function loadExecutionHistory(byNagent) {
    return Object.keys(byNagent).flatMap((nagentId) => (
      (byNagent[nagentId] || []).map((session) => ({
        n_agent_session_id: nagentId,
        browser_session_id: session.id,
        backend_type: session.backend_type,
        status: session.status,
        action_count: session.action_count,
        created_at: session.created_at,
        updated_at: session.updated_at,
      }))
    )).sort((left, right) => (
      String(right.updated_at || right.created_at || '').localeCompare(
        String(left.updated_at || left.created_at || ''),
      )
    ));
  }

  function deactivate() {
    stopPolling();
  }

  namespace.browser = { init: load, refresh: load, deactivate: deactivate, selectSession: selectSession };
  global.NAGENT = namespace;
  global.NAGENT.browser = namespace.browser;
}(window));
