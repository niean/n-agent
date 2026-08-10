/* artifacts.js -- NAGENT.artifacts Artifact workbench (T15).
 *
 * Two-column workbench at /artifacts:
 *   left  -> artifact list (kind icon, name, source label, updated time) +
 *            search + source_kind/kind/status filters + load-more (cursor)
 *   right -> header (name, valid source backlink, [编辑][导出][发布]) +
 *            preview by kind; edit mode switches to editor
 *
 * Security (CRITICAL):
 * - ALL untrusted text rendered via textContent ONLY. No unsafe DOM sinks
 *   (no raw HTML assignment, no insertAdjacentHTML, no document.write,
 *   no inline handlers).
 * - The ONLY HTML display surface is a sandbox="" iframe (NO allow-*
 *   tokens). markdown/document use server-side safe HTML via
 *   /export?format=html in a sandbox iframe; html uses srcdoc.
 * - code/text/json/csv -> textContent + pre/code/table.
 * - image/pdf -> blob URL from /content (revoked on switch/destroy).
 *
 * Lifecycle:
 * - Nav item DEFAULT HIDDEN in index.html; a probe to /chat/artifacts at
 *   load reveals it only on success (disabled service -> no flash).
 * - Request race cancellation: in-flight fetch aborted on new selection.
 * - Blob cleanup: revokeObjectURL on artifact switch + deactivate.
 * - Explicit states: loading/empty/filter-empty/unavailable/error/
 *   publish-blocked/revoked (no blank panel for errors).
 */
(function (global) {
  'use strict';
  const namespace = global.NAGENT || (global.NAGENT = {});
  const ui = namespace.ui || {};
  // Resolve modal dynamically so test harnesses / late-loaded UI helpers
  // are picked up even if assigned after this module loads.
  function getModal() { return namespace.modal || {}; }

  // ---- constants ----
  const API_BASE = '/chat/artifacts';
  const TEXT_KINDS = ['document', 'markdown', 'code', 'html', 'data', 'csv', 'json', 'text'];
  const BINARY_KINDS = ['image', 'pdf', 'other'];
  const CSV_MAX_ROWS = 200;
  const CSV_MAX_COLS = 50;
  const TASK_ID_RE = /^[A-Za-z0-9_-]+$/;
  // T10: publish_sync_state -> badge label (chat card + workbench share this).
  const ARTIFACT_SYNC_LABELS = {
    unpublished: '未发布',
    current: '已发布',
    outdated: '已过期',
  };

  // ---- state ----
  let state = {
    items: [],
    nextCursor: null,
    loading: false,
    loadingMore: false,
    filters: { q: '', source_kind: '', kind: '', status: '' },
    selectedId: null,
    detail: null,
    content: null,       // {text|blob, kind, mime}
    view: 'preview',     // 'preview' | 'edit'
    publish: null,       // current publish state
    error: null,
    // T10: revision management + capabilities-driven export
    revisions: null,     // list[{id,revision_number,change_summary,created_at,is_current,is_published,...}] | null
    capabilities: null,  // string[] from GET /export/capabilities | null
    diffView: null,      // {text, fromLabel, toLabel} | null
  };
  let inflight = null;   // AbortController for in-flight detail/content fetch
  let activeBlobs = [];  // created object URLs to revoke on switch/destroy
  let navProbed = false;

  // ---- helpers ----
  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }
  function byId(id) { return document.getElementById(id); }
  function clear(node) { if (node) node.replaceChildren(); }
  function fmtTime(v) {
    if (!v) return '-';
    const d = new Date(v);
    if (isNaN(d.getTime())) return String(v);
    const tz = new Date(d.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return tz.getUTCFullYear() + '-' + pad(tz.getUTCMonth() + 1) + '-' + pad(tz.getUTCDate())
      + ' ' + pad(tz.getUTCHours()) + ':' + pad(tz.getUTCMinutes()) + ':' + pad(tz.getUTCSeconds());
  }
  function kindLabel(kind) {
    const map = {
      document: '文档', markdown: 'Markdown', code: '代码', html: 'HTML',
      data: '数据', csv: 'CSV', json: 'JSON', image: '图片', pdf: 'PDF',
      text: '文本', other: '其他',
    };
    return map[kind] || kind || '-';
  }
  function sourceLabel(src) {
    const map = { task_attachment: '任务附件', task_artifact: '任务产物', session: '会话', manual: '手动' };
    return map[src] || src || '-';
  }

  // Build a safe backlink href for the source. Only task sources with a
  // validated task id get a backlink; unsafe ids render no link (no open
  // redirect / path traversal surface).
  function sourceBacklink(detail) {
    if (!detail) return null;
    const sk = detail.source_kind;
    const ctx = detail.source_context_ref;
    if (sk === 'task_attachment' || sk === 'task_artifact') {
      if (typeof ctx === 'string' && TASK_ID_RE.test(ctx)) {
        return '/tasks/' + encodeURIComponent(ctx);
      }
      return null;
    }
    if (sk === 'session') {
      if (typeof ctx === 'string' && TASK_ID_RE.test(ctx)) {
        return '/sessions';
      }
      return null;
    }
    return null; // manual -> no backlink
  }

  // ---- API (self-contained fetch; no management-api.js dependency) ----
  async function apiRequest(url, options) {
    options = options || {};
    const opts = { method: options.method || 'GET', headers: options.headers || {} };
    if (options.signal) opts.signal = options.signal;
    if (options.body != null) { opts.body = options.body; }
    const resp = await fetch(url, opts);
    const ctype = (resp.headers.get('content-type') || '').toLowerCase();
    if (ctype.indexOf('application/json') !== -1) {
      const data = resp.status === 204 ? null : await resp.json();
      if (!resp.ok) {
        const code = data && data.error && data.error.code ? data.error.code : 'request_failed';
        const err = new Error(code);
        err.code = code;
        err.status = resp.status;
        throw err;
      }
      return data;
    }
    // non-json: return response for caller to handle (blob/text)
    if (!resp.ok) {
      let code = 'request_failed';
      try { const j = await resp.json(); if (j && j.error && j.error.code) code = j.error.code; } catch (_) {}
      const err = new Error(code);
      err.code = code; err.status = resp.status;
      throw err;
    }
    return resp;
  }

  function listUrl(params) {
    const sp = new URLSearchParams();
    if (params.q) sp.set('q', params.q);
    if (params.source_kind) sp.set('source_kind', params.source_kind);
    if (params.kind) sp.set('kind', params.kind);
    if (params.status) sp.set('status', params.status);
    if (params.cursor) sp.set('cursor', JSON.stringify(params.cursor));
    sp.set('limit', String(params.limit || 50));
    const qs = sp.toString();
    return API_BASE + (qs ? '?' + qs : '');
  }

  async function fetchList(params, signal) {
    return apiRequest(listUrl(params), { signal });
  }
  async function fetchDetail(id, signal) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id), { signal });
  }
  async function fetchContent(id, signal) {
    // returns {resp, mime} for blob/text handling
    const resp = await fetch(API_BASE + '/' + encodeURIComponent(id) + '/content', { signal });
    if (!resp.ok) {
      let code = 'request_failed';
      try { const j = await resp.json(); if (j && j.error && j.error.code) code = j.error.code; } catch (_) {}
      const err = new Error(code); err.code = code; err.status = resp.status;
      throw err;
    }
    return resp;
  }
  async function fetchExport(id, format, signal) {
    const url = API_BASE + '/' + encodeURIComponent(id) + '/export?format=' + encodeURIComponent(format);
    return fetch(url, { signal });
  }
  async function patchArtifact(id, body, signal) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
  }
  async function patchArtifactMultipart(id, formData, signal, expectedRevisionId) {
    const headers = {};
    // T10: binary content replace carries the CAS token via If-Match
    if (expectedRevisionId) { headers['If-Match'] = expectedRevisionId; }
    return fetch(API_BASE + '/' + encodeURIComponent(id), {
      method: 'PATCH', headers, body: formData, signal,
    }).then(async (resp) => {
      const data = await resp.json();
      if (!resp.ok) {
        const code = (data && data.error && data.error.code) || 'request_failed';
        const e = new Error(code); e.code = code; throw e;
      }
      return data;
    });
  }
  async function publishArtifact(id) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id) + '/publish', { method: 'POST' });
  }
  async function getPublish(id, signal) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id) + '/publish', { signal });
  }
  async function revokePublish(id) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id) + '/publish', { method: 'DELETE' });
  }
  async function fetchDeleteArtifact(id) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id), { method: 'DELETE' });
  }

  // ---- T10: revision management + capabilities-driven export ----
  async function fetchExportCapabilities(id, signal) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id) + '/export/capabilities', { signal });
  }
  async function fetchRevisions(id, signal) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id) + '/revisions?limit=100', { signal });
  }
  async function fetchDiff(id, fromId, toId, signal) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id) + '/diff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from_revision_id: fromId, to_revision_id: toId, context_lines: 3 }),
      signal,
    });
  }
  async function rollbackArtifact(id, targetRevisionId, expectedRevisionId) {
    return apiRequest(API_BASE + '/' + encodeURIComponent(id) + '/rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        target_revision_id: targetRevisionId,
        expected_revision_id: expectedRevisionId,
        change_summary: '从版本历史回滚',
      }),
    });
  }

  // ---- blob management ----
  function trackBlob(url) { if (url) activeBlobs.push(url); return url; }
  function revokeActiveBlobs() {
    for (const u of activeBlobs) {
      try { URL.revokeObjectURL(u); } catch (_) {}
    }
    activeBlobs = [];
  }

  // ---- nav probe (reveal nav only when API is available) ----
  async function probeAndToggleNav() {
    if (navProbed) return;
    navProbed = true;
    const navItem = byId('nav-artifacts') || document.querySelector('[data-tab="artifacts"]');
    if (!navItem) return;
    try {
      await apiRequest(API_BASE + '?limit=1');
      // success -> reveal nav
      navItem.removeAttribute('hidden');
    } catch (_) {
      // disabled service / missing API -> keep nav hidden
    }
  }

  // ---- shell ----
  function buildShell(root) {
    clear(root);
    const shell = el('div', 'artifacts-shell');
    const listCol = el('section', 'status-panel artifacts-list');
    listCol.id = 'artifacts-list-panel';
    const detailCol = el('section', 'status-panel artifacts-detail');
    detailCol.id = 'artifacts-detail-panel';
    shell.append(listCol, detailCol);
    root.appendChild(shell);
    buildFilters(listCol);
    const listBody = el('div', 'panel-body artifacts-list__body');
    listBody.id = 'artifacts-list-body';
    listCol.appendChild(listBody);
    const detailBody = el('div', 'panel-body artifacts-detail__body');
    detailBody.id = 'artifacts-detail-body';
    detailCol.appendChild(detailBody);
  }

  function buildFilters(listCol) {
    const header = el('div', 'panel-header');
    const title = el('span'); title.textContent = '制品'; header.appendChild(title);
    listCol.appendChild(header);
    const filterBar = el('div', 'artifacts-filter');
    const search = el('input', 'artifacts-filter__q');
    search.type = 'search';
    search.placeholder = '搜索名称...';
    search.addEventListener('input', () => {
      state.filters.q = search.value;
      debounceLoad();
    });
    filterBar.appendChild(search);

    const sk = el('select', 'artifacts-filter__source_kind');
    sk.appendChild(makeOption('', '全部来源'));
    sk.appendChild(makeOption('manual', '手动'));
    sk.appendChild(makeOption('task_attachment', '任务附件'));
    sk.appendChild(makeOption('task_artifact', '任务产物'));
    sk.appendChild(makeOption('session', '会话'));
    sk.addEventListener('change', () => { state.filters.source_kind = sk.value; reloadList(); });
    filterBar.appendChild(sk);

    const kd = el('select', 'artifacts-filter__kind');
    kd.appendChild(makeOption('', '全部类型'));
    ['document', 'markdown', 'code', 'html', 'data', 'csv', 'json', 'text', 'image', 'pdf', 'other'].forEach((k) => {
      kd.appendChild(makeOption(k, kindLabel(k)));
    });
    kd.addEventListener('change', () => { state.filters.kind = kd.value; reloadList(); });
    filterBar.appendChild(kd);

    const st = el('select', 'artifacts-filter__status');
    st.appendChild(makeOption('', '全部状态'));
    st.appendChild(makeOption('draft', '草稿'));
    st.appendChild(makeOption('published', '已发布'));
    st.appendChild(makeOption('archived', '已归档'));
    st.addEventListener('change', () => { state.filters.status = st.value; reloadList(); });
    filterBar.appendChild(st);
    listCol.appendChild(filterBar);
  }
  function makeOption(value, label) {
    const o = el('option'); o.value = value; o.textContent = label; return o;
  }

  // ---- list ----
  let debounceTimer = null;
  function debounceLoad() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => reloadList(), 250);
  }

  async function reloadList() {
    state.items = [];
    state.nextCursor = null;
    await loadList(false);
  }

  async function loadList(more) {
    const body = byId('artifacts-list-body');
    if (!body) return;
    if (more) { state.loadingMore = true; }
    else { state.loading = true; renderList(); }
    try {
      const params = Object.assign({}, state.filters, { limit: 50, cursor: more ? state.nextCursor : null });
      const page = await fetchList(params);
      const items = (page && page.items) || [];
      if (more) state.items = state.items.concat(items);
      else state.items = items;
      state.nextCursor = (page && page.next_cursor) || null;
      state.loading = false;
      state.loadingMore = false;
      renderList();
    } catch (e) {
      state.loading = false;
      state.loadingMore = false;
      state.error = e;
      renderListError(e);
    }
  }

  function renderList() {
    const body = byId('artifacts-list-body');
    if (!body) return;
    clear(body);
    if (state.loading) {
      renderState(body, '加载中...', 'muted loading-state');
      return;
    }
    const items = state.items || [];
    if (!items.length) {
      const hasFilter = state.filters.q || state.filters.source_kind || state.filters.kind || state.filters.status;
      const msg = hasFilter ? '无匹配结果（调整筛选条件）' : '暂无制品';
      renderState(body, msg, 'muted empty-state artifacts-list__empty');
      return;
    }
    const list = el('div', 'artifacts-list__items');
    items.forEach((a) => list.appendChild(renderListItem(a)));
    body.appendChild(list);
    if (state.nextCursor) {
      const more = el('button', 'btn artifacts-list__more');
      more.type = 'button';
      more.textContent = '加载更多';
      more.addEventListener('click', () => loadList(true));
      body.appendChild(more);
    }
  }

  function renderListItem(a, onClick) {
    const item = el('div', 'artifacts-list__item');
    const useCallback = typeof onClick === 'function';
    if (!useCallback && state.selectedId === a.id) item.classList.add('artifacts-list__item--active');
    if (useCallback) {
      item.addEventListener('click', function () { onClick(a); });
    } else {
      item.addEventListener('click', () => selectArtifact(a.id));
    }
    const icon = el('span', 'artifacts-list__icon');
    icon.textContent = kindLabel(a.kind).slice(0, 2);
    item.appendChild(icon);
    const meta = el('div', 'artifacts-list__meta');
    const name = el('div', 'artifacts-list__name');
    name.textContent = a.name || a.id;
    meta.appendChild(name);
    const sub = el('div', 'artifacts-list__sub muted');
    sub.textContent = sourceLabel(a.source_kind) + ' · ' + fmtTime(a.updated_at);
    meta.appendChild(sub);
    item.appendChild(meta);
    return item;
  }

  function renderListError(e) {
    const body = byId('artifacts-list-body');
    if (!body) return;
    clear(body);
    renderState(body, '加载失败：' + (e && e.message ? e.message : e), 'muted error-state');
  }

  function renderState(parent, msg, cls) {
    const node = el('div', cls);
    node.textContent = msg;
    parent.appendChild(node);
  }

  // ---- detail selection (race cancellation + blob cleanup) ----
  function selectArtifact(id) {
    if (state.selectedId === id) return;
    // sync URL: /artifacts/{id}
    const path = '/artifacts/' + encodeURIComponent(id);
    if (window.location.pathname !== path) {
      history.pushState({ tab: 'artifacts' }, '', path);
    }
    // abort in-flight fetch on new selection
    if (inflight) { try { inflight.abort(); } catch (_) {} inflight = null; }
    // revoke previous blobs
    revokeActiveBlobs();
    state.selectedId = id;
    state.detail = null;
    state.content = null;
    state.view = 'preview';
    state.publish = null;
    state.error = null;
    // re-render list to update active highlight
    renderList();
    renderDetailLoading();
    inflight = new AbortController();
    const sig = inflight.signal;
    loadDetail(id, sig);
  }

  // ---- deep-link routing (/artifacts/{id}) ----
  function pendingArtifactIdFromPath() {
    const match = window.location.pathname.match(/^\/artifacts\/([^/]+)$/);
    if (!match) return null;
    try { return decodeURIComponent(match[1]); } catch (_) { return null; }
  }

  function handlePathChange() {
    const pendingId = pendingArtifactIdFromPath();
    if (pendingId) {
      if (state.selectedId !== pendingId) selectArtifact(pendingId);
      return;
    }
    // back to /artifacts list: clear selection
    if (state.selectedId) {
      if (inflight) { try { inflight.abort(); } catch (_) {} inflight = null; }
      revokeActiveBlobs();
      state.selectedId = null;
      state.detail = null;
      state.content = null;
      state.publish = null;
      state.error = null;
      // T10: clear revision/capabilities/diff state with the selection.
      state.revisions = null;
      state.capabilities = null;
      state.diffView = null;
      renderList();
      renderDetail();
    }
  }

  async function loadDetail(id, sig) {
    try {
      const [detail, contentResp] = await Promise.all([
        fetchDetail(id, sig),
        fetchContent(id, sig).catch((e) => { if (e.code === 'artifact_content_unavailable') return { unavailable: true }; throw e; }),
      ]);
      if (state.selectedId !== id) return; // raced
      state.detail = detail;
      state.content = await parseContent(contentResp, detail);
      // T10: reset revision/diff/capabilities state for the new selection.
      state.revisions = null;
      state.capabilities = null;
      state.diffView = null;
      // load publish status + capabilities + revisions in parallel (non-blocking)
      loadPublishStatus(id, sig);
      loadCapabilities(id, sig);
      loadRevisions(id, sig);
      renderDetail();
    } catch (e) {
      if (state.selectedId !== id) return;
      if (e && e.name === 'AbortError') return;
      state.error = e;
      renderDetailError(e);
    }
  }

  async function parseContent(resp, detail) {
    if (!resp || resp.unavailable) return { unavailable: true };
    const kind = detail.kind;
    const mime = detail.mime || '';
    if (BINARY_KINDS.indexOf(kind) !== -1) {
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      trackBlob(url);
      return { blob: url, kind, mime };
    }
    // text kinds
    const text = await resp.text();
    return { text, kind, mime };
  }

  async function loadPublishStatus(id, sig) {
    try {
      const pub = await getPublish(id, sig);
      if (state.selectedId !== id) return;
      state.publish = pub;
      refreshPublishState();
    } catch (_) { /* publish status optional */ }
  }

  // ---- detail render ----
  function renderDetailLoading() {
    const body = byId('artifacts-detail-body');
    if (!body) return;
    clear(body);
    renderState(body, '加载中...', 'muted loading-state');
  }

  function renderDetailError(e) {
    const body = byId('artifacts-detail-body');
    if (!body) return;
    clear(body);
    const code = e && e.code ? e.code : '';
    let msg = '加载失败：' + (e && e.message ? e.message : e);
    if (code === 'artifact_content_unavailable') msg = '内容不可用：该制品内容暂无法访问';
    renderState(body, msg, 'muted error-state artifacts-detail__unavailable');
  }

  function renderDetail() {
    const body = byId('artifacts-detail-body');
    if (!body) return;
    clear(body);
    const detail = state.detail;
    if (!detail) { renderState(body, '未选择制品', 'muted empty-state'); return; }

    // content-unavailable state
    if (state.content && state.content.unavailable) {
      renderState(body, '内容不可用：该制品内容暂无法访问', 'muted error-state artifacts-detail__unavailable');
      renderHeader(body, detail);
      return;
    }

    renderHeader(body, detail);
    // metadata row (size/updated_at) -- reflects server-returned values
    // after save/replace so the user can verify the refresh.
    const meta = el('div', 'artifacts-detail__metadata muted');
    meta.textContent = '大小: ' + (detail.size != null ? detail.size : '-') + ' B'
      + ' · 更新: ' + fmtTime(detail.updated_at);
    // T10: publish_sync_state badge (unpublished/current/outdated) from the
    // server-enriched detail, distinct from the active-publish link segment.
    meta.appendChild(renderPublishSyncBadge(detail));
    // publish status segment (populated by refreshPublishState when active):
    // appends "；已发布: 链接" with the share link, per the prd format.
    const pubStatus = el('span', 'artifacts-detail__publish-status');
    meta.appendChild(pubStatus);
    body.appendChild(meta);
    if (state.view === 'edit') {
      renderEditor(body, detail);
    } else {
      renderPreview(body, detail);
    }
    // T10: revision history panel (loads async into the container).
    const revPanel = el('div', 'artifacts-revisions-panel');
    revPanel.id = 'artifacts-revisions-panel';
    body.appendChild(revPanel);
    renderRevisionsPanel();
    refreshPublishState();
  }

  // T10: publish_sync_state badge. Renders the raw server state as a labeled
  // badge; unknown states fall back to their raw value. textContent only.
  function renderPublishSyncBadge(detail) {
    const badge = el('span', 'artifacts-detail__sync-badge');
    const state_ = detail && typeof detail.publish_sync_state === 'string' ? detail.publish_sync_state : '';
    badge.className = 'artifacts-detail__sync-badge artifacts-detail__sync-badge--' + (state_ || 'unpublished');
    badge.textContent = ARTIFACT_SYNC_LABELS[state_] || state_ || '未发布';
    return badge;
  }

  function renderHeader(body, detail) {
    const header = el('div', 'artifacts-detail__header');
    const name = el('span', 'artifacts-detail__name');
    name.textContent = detail.name || detail.id;
    // source backlink (only valid controlled task ids)
    const backHref = sourceBacklink(detail);
    let standaloneBack = null;
    if (backHref) {
      const back = el('a', 'artifacts-detail__backlink');
      back.href = backHref;
      const sk = detail.source_kind;
      const isTaskSource = sk === 'task_attachment' || sk === 'task_artifact';
      // 任务来源：紧跟名称显示"(任务)"，其中括号为纯文本、仅"任务"两字为链接，
      // 形如"产出物名称(任务)"；其他来源（如 session）保持独立链接，位于名称之后。
      back.textContent = isTaskSource ? '任务' : sourceLabel(detail.source_kind);
      back.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (global.NAGENT && global.NAGENT.navigation && typeof global.NAGENT.navigation.navigatePath === 'function') {
          global.NAGENT.navigation.navigatePath(backHref);
        } else { global.location.href = backHref; }
      });
      if (isTaskSource) {
        const wrap = el('span', 'artifacts-detail__backlink-wrap');
        const open = el('span', 'artifacts-detail__backlink-paren');
        open.textContent = '(';
        const close = el('span', 'artifacts-detail__backlink-paren');
        close.textContent = ')';
        wrap.append(open, back, close);
        name.appendChild(wrap);
      } else {
        standaloneBack = back;
      }
    }
    header.appendChild(name);
    if (standaloneBack) header.appendChild(standaloneBack);
    const actions = el('div', 'panel-actions artifacts-detail__actions');
    // edit button
    const editBtn = el('button', 'btn artifacts-detail__edit');
    editBtn.type = 'button';
    editBtn.textContent = state.view === 'edit' ? '取消' : '编辑';
    editBtn.addEventListener('click', () => toggleEdit());
    actions.appendChild(editBtn);
    // delete button
    const delBtn = el('button', 'btn artifacts-detail__delete');
    delBtn.type = 'button';
    delBtn.textContent = '删除';
    delBtn.addEventListener('click', () => doDelete(detail));
    actions.appendChild(delBtn);
    // export button: opens a standard modal to confirm the export format
    const expBtn = el('button', 'btn artifacts-detail__export');
    expBtn.type = 'button';
    expBtn.textContent = '导出';
    expBtn.addEventListener('click', () => openExportModal(state.detail));
    actions.appendChild(expBtn);
    // publish button: toggles between 发布 (publish) and 撤回 (revoke) based on
    // the active publish state. Text is kept in sync by refreshPublishState;
    // the handler branches at click time so no listener swap is needed.
    const pubBtn = el('button', 'btn artifacts-detail__publish');
    pubBtn.type = 'button';
    pubBtn.textContent = '发布';
    pubBtn.addEventListener('click', () => {
      const pub = state.publish;
      const active = pub && (pub.status === 'active' || pub.status === 'published');
      if (active) { doRevoke(state.detail && state.detail.id); }
      else { doPublish(); }
    });
    actions.appendChild(pubBtn);
    header.appendChild(actions);
    body.appendChild(header);
  }

  // Export: a standard button (项目标准按钮) opens a project-standard modal
  // (modal-backdrop / modal-dialog / providers-form) where the user confirms
  // the format before downloading. Available formats come from
  // GET /export/capabilities (server truth); until they arrive, 'original' is
  // offered as a fallback. html stays server-side for markdown/document preview
  // but is offered as a download only when the server advertises it.
  function exportFormats() {
    return (state.capabilities && state.capabilities.length) ? state.capabilities : ['original'];
  }

  function exportFormatLabel(fmt) {
    const map = { original: '原始文件', html: 'HTML', docx: 'DOCX', pptx: 'PPTX', xlsx: 'XLSX' };
    return map[fmt] || fmt;
  }

  function closeExportModal() {
    const modal = document.getElementById('artifacts-export-modal');
    if (modal) modal.remove();
  }

  function openExportModal(detail) {
    if (!detail) return;
    closeExportModal(); // never stack two export modals
    const formats = exportFormats();
    const defaultFmt = formats.indexOf('original') !== -1 ? 'original' : formats[0];

    const backdrop = el('div', 'modal-backdrop');
    backdrop.id = 'artifacts-export-modal';
    const dialog = el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-label', '导出制品');
    const form = el('form', 'providers-form');

    const header = el('div', 'modal-header');
    const title = el('h4');
    title.textContent = '导出制品';
    const closeBtn = el('button', 'modal-close');
    closeBtn.type = 'button';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭');
    header.append(title, closeBtn);
    form.appendChild(header);

    // format selector (radio group): the extra info the user confirms
    const optionsWrap = el('div', 'export-modal__options');
    const radios = [];
    formats.forEach((fmt) => {
      const opt = el('label', 'export-modal__option');
      const radio = el('input');
      radio.type = 'radio';
      radio.name = 'export-format';
      radio.value = fmt;
      radio.dataset.format = fmt;
      radio.checked = (fmt === defaultFmt);
      radio.addEventListener('change', updateFilename);
      radios.push(radio);
      const lbl = el('span');
      lbl.textContent = exportFormatLabel(fmt);
      opt.append(radio, lbl);
      optionsWrap.appendChild(opt);
    });
    form.appendChild(optionsWrap);

    // resulting filename hint (artifact name + chosen format, PRD line 85)
    const filenameHint = el('div', 'providers-form__hint export-modal__filename');
    form.appendChild(filenameHint);

    // inline error (hidden until export fails; modal stays open for retry)
    const errorHint = el('div', 'providers-form__hint badge badge--danger');
    errorHint.style.display = 'none';
    form.appendChild(errorHint);

    const actions = el('div', 'providers-form__actions');
    const cancelBtn = el('button', 'btn');
    cancelBtn.type = 'button';
    cancelBtn.textContent = '取消';
    const confirmBtn = el('button', 'btn btn--primary');
    confirmBtn.type = 'submit';
    confirmBtn.textContent = '导出';
    actions.append(cancelBtn, confirmBtn);
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);

    function selectedFormat() {
      const r = radios.find((x) => x.checked);
      return r ? r.value : defaultFmt;
    }
    function updateFilename() {
      filenameHint.textContent = '文件名: ' + exportFilename(detail.name, selectedFormat());
    }
    updateFilename();

    let closed = false;
    function close() { if (closed) return; closed = true; backdrop.remove(); }
    function setBusy(busy) {
      confirmBtn.disabled = busy;
      cancelBtn.disabled = busy;
      confirmBtn.textContent = busy ? '导出中...' : '导出';
    }

    backdrop.addEventListener('click', (event) => { if (event.target === backdrop) close(); });
    closeBtn.addEventListener('click', close);
    cancelBtn.addEventListener('click', close);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (confirmBtn.disabled) return;
      errorHint.style.display = 'none';
      setBusy(true);
      try {
        await runExport(detail, selectedFormat());
        close();
      } catch (e) {
        setBusy(false);
        errorHint.textContent = '导出失败：' + (e && e.message ? e.message : e);
        errorHint.style.display = '';
      }
    });

    document.body.appendChild(backdrop);
  }

  // Derive the download filename from the artifact name (not a hardcoded
  // "export"): original keeps the name as-is (it already carries the right
  // extension, e.g. "report.md"); html replaces the extension with .html so
  // the downloaded file matches its actual content type.
  function exportFilename(name, format) {
    const base = (name && String(name).trim()) || 'export';
    if (format === 'html') {
      const dot = base.lastIndexOf('.');
      const stem = dot > 0 ? base.slice(0, dot) : base;
      return stem + '.html';
    }
    return base;
  }

  // Download an export of `detail` in `format`. Throws on failure so the
  // export modal can surface an inline error and let the user retry. The blob
  // URL bypasses Content-Disposition (see knowledge/05-key-patterns.md).
  async function runExport(detail, format) {
    const resp = await fetchExport(detail.id, format);
    if (!resp.ok) throw new Error('export failed');
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = el('a');
    a.href = url;
    a.download = exportFilename(detail.name, format);
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---- preview by kind ----
  function renderPreview(body, detail) {
    const content = state.content;
    if (!content) { renderState(body, '加载中...', 'muted loading-state'); return; }
    const kind = detail.kind;
    const previewWrap = el('div', 'artifacts-detail__preview');
    if (kind === 'markdown' || kind === 'document') {
      previewWrap.appendChild(renderMarkdownHtml(detail));
    } else if (kind === 'html') {
      previewWrap.appendChild(renderHtmlSrcdoc(content.text));
    } else if (kind === 'code' || kind === 'text') {
      previewWrap.appendChild(renderPre(content.text));
    } else if (kind === 'json') {
      previewWrap.appendChild(renderJson(content.text));
    } else if (kind === 'csv') {
      previewWrap.appendChild(renderCsv(content.text));
    } else if (kind === 'image') {
      previewWrap.appendChild(renderImage(content.blob));
    } else if (kind === 'pdf') {
      previewWrap.appendChild(renderPdf(content.blob, detail));
    } else {
      // data / other -> text if available, else binary notice
      if (content.text != null) previewWrap.appendChild(renderPre(content.text));
      else renderState(previewWrap, '该类型暂不支持预览', 'muted empty-state');
    }
    body.appendChild(previewWrap);
  }

  // markdown/document: server-side safe HTML in a sandbox="" iframe (NO allow-*).
  // Uses the active AbortController signal so the export fetch is cancelled on
  // artifact switch/deactivate (race cancellation consistent with detail/content).
  function renderMarkdownHtml(detail) {
    const wrap = el('div', 'artifacts-preview__markdown');
    const iframe = el('iframe', 'artifacts-preview__iframe');
    iframe.setAttribute('title', 'Markdown 预览');
    iframe.setAttribute('sandbox', '');
    // fetch server-rendered safe HTML and set as srcdoc via textContent-safe path
    const id = detail.id;
    const sig = inflight ? inflight.signal : undefined;
    fetchExport(id, 'html', sig).then(async (resp) => {
      // guard: if the user navigated away while loading, do not render.
      if (state.selectedId !== id) return;
      if (!resp.ok) { renderState(wrap, '预览不可用', 'muted error-state'); return; }
      const html = await resp.text();
      if (state.selectedId !== id) return;
      // srcdoc is a safe attribute (no script execution under sandbox="")
      iframe.setAttribute('srcdoc', html);
    }).catch(() => {
      if (state.selectedId !== id) return;
      renderState(wrap, '预览加载失败', 'muted error-state');
    });
    wrap.appendChild(iframe);
    return wrap;
  }

  // html: srcdoc in sandbox="" iframe (NO allow-*).
  function renderHtmlSrcdoc(htmlText) {
    const wrap = el('div', 'artifacts-preview__html');
    const iframe = el('iframe', 'artifacts-preview__iframe');
    iframe.setAttribute('title', 'HTML 预览');
    iframe.setAttribute('sandbox', '');
    iframe.setAttribute('srcdoc', htmlText || '');
    wrap.appendChild(iframe);
    return wrap;
  }

  function renderPre(text) {
    const pre = el('pre', 'artifacts-preview__pre');
    const code = el('code', '');
    code.textContent = text == null ? '' : String(text);
    pre.appendChild(code);
    return pre;
  }

  function renderJson(text) {
    const wrap = el('div', 'artifacts-preview__json');
    try {
      const parsed = JSON.parse(text);
      const pre = el('pre', 'artifacts-preview__pre');
      pre.textContent = JSON.stringify(parsed, null, 2);
      wrap.appendChild(pre);
    } catch (e) {
      // parse fail: show raw + error
      const err = el('div', 'muted error-state');
      err.textContent = 'JSON 解析失败：' + (e && e.message ? e.message : e);
      wrap.appendChild(err);
      wrap.appendChild(renderPre(text));
    }
    return wrap;
  }

  function renderCsv(text) {
    const wrap = el('div', 'artifacts-preview__csv');
    const table = el('table', 'document-table artifacts-preview__table');
    const lines = String(text || '').split(/\r?\n/).filter((l) => l.length);
    const head = el('thead');
    const hr = el('tr');
    const firstCols = (lines[0] || '').split(',').slice(0, CSV_MAX_COLS);
    firstCols.forEach((c) => { const th = el('th'); th.textContent = c; hr.appendChild(th); });
    head.appendChild(hr);
    table.appendChild(head);
    const tbody = el('tbody');
    const rowLimit = Math.min(lines.length, CSV_MAX_ROWS);
    for (let i = 1; i < rowLimit; i++) {
      const tr = el('tr');
      const cols = lines[i].split(',').slice(0, CSV_MAX_COLS);
      cols.forEach((c) => { const td = el('td'); td.textContent = c; tr.appendChild(td); });
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    if (lines.length > CSV_MAX_ROWS) {
      const note = el('div', 'muted');
      note.textContent = '仅显示前 ' + CSV_MAX_ROWS + ' 行（共 ' + lines.length + ' 行）';
      wrap.appendChild(note);
    }
    return wrap;
  }

  function renderImage(blobUrl) {
    const wrap = el('div', 'artifacts-preview__image');
    const img = el('img', 'artifacts-preview__img');
    img.alt = '图片预览';
    if (blobUrl) img.src = blobUrl;
    img.addEventListener('error', () => {
      clear(wrap);
      renderState(wrap, '图片暂不可用', 'muted error-state');
    });
    wrap.appendChild(img);
    return wrap;
  }

  function renderPdf(blobUrl, detail) {
    const wrap = el('div', 'artifacts-preview__pdf');
    const iframe = el('iframe', 'artifacts-preview__iframe');
    iframe.setAttribute('title', 'PDF 预览');
    // PDF is NOT sandboxed (unlike HTML/markdown srcdoc): the blob is routed
    // to the browser's built-in PDF viewer, which sandbox="" blocks (viewer is
    // treated as a plugin; empty sandbox also strips same-origin for the blob).
    // PDFs carry no page scripts, so no sandbox is needed for preview safety.
    if (blobUrl) iframe.src = blobUrl;
    wrap.appendChild(iframe);
    // download fallback
    const dl = el('a', 'btn artifacts-preview__pdf-download');
    dl.textContent = '下载 PDF';
    if (blobUrl) dl.href = blobUrl;
    if (detail && detail.name) dl.download = detail.name;
    wrap.appendChild(dl);
    return wrap;
  }

  // ---- edit ----
  function toggleEdit() {
    if (state.view === 'edit') { state.view = 'preview'; renderDetail(); return; }
    state.view = 'edit';
    renderDetail();
  }

  function renderEditor(body, detail) {
    const kind = detail.kind;
    if (BINARY_KINDS.indexOf(kind) !== -1) {
      renderBinaryEditor(body, detail);
    } else {
      renderTextEditor(body, detail);
    }
  }

  function renderTextEditor(body, detail) {
    const wrap = el('div', 'artifacts-detail__editor');
    const ta = el('textarea', 'artifacts-detail__textarea');
    ta.value = (state.content && state.content.text != null) ? state.content.text : '';
    ta.placeholder = '编辑内容...';
    wrap.appendChild(ta);
    const actions = el('div', 'providers-form__actions artifacts-detail__edit-actions');
    const save = el('button', 'btn btn--primary artifacts-detail__save');
    save.type = 'button';
    save.textContent = '保存';
    save.addEventListener('click', () => saveText(detail, ta.value));
    const cancel = el('button', 'btn artifacts-detail__cancel');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', () => { state.view = 'preview'; renderDetail(); });
    actions.append(save, cancel);
    wrap.appendChild(actions);
    body.appendChild(wrap);
  }

  async function saveText(detail, value) {
    if (!detail || !detail.id) return;
    // T10: content PATCH always carries the detail's expected_revision_id
    // (CAS). A stale token -> artifact_revision_conflict (409): keep the
    // current display, refresh server state, prompt the user, NO auto-replay.
    // Metadata-only PATCH (no content) stays token-less (handled elsewhere).
    const expected = detail.current_revision_id;
    if (!expected) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('当前版本未知，请刷新后重试');
      }
      return;
    }
    try {
      const updated = await patchArtifact(detail.id, { content: value, expected_revision_id: expected });
      // refresh with server-returned size/checksum/updated_at
      state.detail = updated;
      state.content = { text: value, kind: updated.kind, mime: updated.mime };
      state.view = 'preview';
      // content edit revokes the active publish on the server (snapshot
      // diverges); reset local state so the UI immediately shows unpublished.
      // loadPublishStatus confirms the server state.
      state.publish = null;
      renderDetail();
      // refresh list (metadata may have changed)
      reloadList();
      loadPublishStatus(detail.id, inflight ? inflight.signal : undefined);
    } catch (e) {
      if (e && e.code === 'artifact_revision_conflict') {
        if (getModal() && typeof getModal().alert === 'function') {
          await getModal().alert('版本已变化，请刷新后重试');
        }
        if (inflight) { try { inflight.abort(); } catch (_) {} }
        revokeActiveBlobs();
        inflight = new AbortController();
        loadDetail(detail.id, inflight.signal);
      } else if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('保存失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  function renderBinaryEditor(body, detail) {
    const wrap = el('div', 'artifacts-detail__editor');
    const note = el('div', 'muted');
    note.textContent = '二进制制品：选择文件替换内容';
    wrap.appendChild(note);
    const fileInput = el('input', 'artifacts-detail__file');
    fileInput.type = 'file';
    wrap.appendChild(fileInput);
    const actions = el('div', 'providers-form__actions');
    const save = el('button', 'btn btn--primary artifacts-detail__save');
    save.type = 'button';
    save.textContent = '替换';
    save.addEventListener('click', async () => {
      if (!fileInput.files || !fileInput.files.length) {
        if (getModal() && typeof getModal().alert === 'function') await getModal().alert('请选择文件');
        return;
      }
      // T10: binary content replace carries the CAS token via If-Match.
      const expected = detail.current_revision_id;
      if (!expected) {
        if (getModal() && typeof getModal().alert === 'function') {
          await getModal().alert('当前版本未知，请刷新后重试');
        }
        return;
      }
      const fd = new FormData();
      fd.append('content', fileInput.files[0]);
      try {
        const updated = await patchArtifactMultipart(detail.id, fd, undefined, expected);
        // refresh with server-returned size/checksum/updated_at
        state.detail = updated;
        state.view = 'preview';
        // content edit revokes the active publish on the server; reset local
        // state. loadDetail (below) reloads publish status to confirm.
        state.publish = null;
        // reload content (binary)
        if (inflight) { try { inflight.abort(); } catch (_) {} }
        revokeActiveBlobs();
        inflight = new AbortController();
        loadDetail(detail.id, inflight.signal);
        reloadList();
      } catch (e) {
        if (e && e.code === 'artifact_revision_conflict') {
          if (getModal() && typeof getModal().alert === 'function') {
            await getModal().alert('版本已变化，请刷新后重试');
          }
          if (inflight) { try { inflight.abort(); } catch (_) {} }
          revokeActiveBlobs();
          inflight = new AbortController();
          loadDetail(detail.id, inflight.signal);
        } else if (getModal() && typeof getModal().alert === 'function') {
          await getModal().alert('替换失败：' + (e && e.message ? e.message : e));
        }
      }
    });
    const cancel = el('button', 'btn artifacts-detail__cancel');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', () => { state.view = 'preview'; renderDetail(); });
    actions.append(save, cancel);
    wrap.appendChild(actions);
    body.appendChild(wrap);
  }

  // ---- publish ----
  // Reflect state.publish in the header: the publish button text (发布/撤回)
  // and the publish-status segment of the metadata row (已发布: 链接). This is
  // a targeted update -- it does NOT re-render the preview, so calling it when
  // the publish status arrives (async) won't reload iframes or reset scroll.
  function refreshPublishState() {
    const host = byId('artifacts-detail-body');
    if (!host) return;
    const pub = state.publish;
    const active = !!(pub && (pub.status === 'active' || pub.status === 'published'));
    const btn = host.querySelector('.artifacts-detail__publish');
    if (btn) btn.textContent = active ? '撤回' : '发布';
    const span = host.querySelector('.artifacts-detail__publish-status');
    if (!span) return;
    clear(span);
    if (!active) return;
    const shareUrl = pub.share_url || (pub.share_path ? global.location.origin + pub.share_path : '');
    span.appendChild(document.createTextNode('；已发布: '));
    if (shareUrl) {
      const a = el('a', 'artifacts-detail__publish-link');
      a.href = shareUrl;
      a.textContent = '链接';
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      span.appendChild(a);
    } else {
      span.appendChild(document.createTextNode('已发布'));
    }
  }

  async function doPublish() {
    const detail = state.detail;
    if (!detail) return;
    // binary publish: explicit-PUBLIC confirmation (server does not scan
    // file-internal secrets).
    if (BINARY_KINDS.indexOf(detail.kind) !== -1) {
      const confirmed = getModal() && typeof getModal().confirm === 'function'
        ? await getModal().confirm('仅显式 PUBLIC，服务端不扫描文件内部秘密。确认发布该二进制制品？')
        : global.confirm('仅显式 PUBLIC，服务端不扫描文件内部秘密。确认发布？');
      if (!confirmed) return;
    }
    try {
      const result = await publishArtifact(detail.id);
      state.publish = { status: 'active', share_url: result.share_url, share_path: result.share_path };
      refreshPublishState();
      // refresh publish status (thread active signal for cancellation)
      loadPublishStatus(detail.id, inflight ? inflight.signal : undefined);
    } catch (e) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('发布失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  async function doDelete(detail) {
    if (!detail || !detail.id) return;
    const name = detail.name || detail.id;
    const confirmed = getModal() && typeof getModal().confirm === 'function'
      ? await getModal().confirm('确认删除制品「' + name + '」？删除后不可恢复。')
      : global.confirm('确认删除制品「' + name + '」？删除后不可恢复。');
    if (!confirmed) return;
    try {
      await fetchDeleteArtifact(detail.id);
      // clear selection and return to list
      if (inflight) { try { inflight.abort(); } catch (_) {} inflight = null; }
      revokeActiveBlobs();
      state.selectedId = null;
      state.detail = null;
      state.content = null;
      state.publish = null;
      state.error = null;
      // T10: clear revision/capabilities/diff state with the selection.
      state.revisions = null;
      state.capabilities = null;
      state.diffView = null;
      if (window.location.pathname !== '/artifacts') {
        history.pushState({ tab: 'artifacts' }, '', '/artifacts');
      }
      renderDetail();
      await reloadList();
    } catch (e) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('删除失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  async function doRevoke(id) {
    if (!id) return;
    try {
      const result = await revokePublish(id);
      state.publish = { status: 'revoked', revoked_at: result && result.revoked_at };
      refreshPublishState();
    } catch (e) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('撤销失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  // ---- T10: revision history + diff + rollback ----
  // Capabilities drive the export modal (loaded in parallel with detail); the
  // modal reads state.capabilities when it opens, so no explicit refresh is
  // needed here.
  async function loadCapabilities(id, sig) {
    try {
      const data = await fetchExportCapabilities(id, sig);
      if (state.selectedId !== id) return;
      // Response shape: {capabilities: [...]} (or legacy array). Coerce defensively.
      const caps = data && Array.isArray(data.capabilities) ? data.capabilities
        : (Array.isArray(data) ? data : ['original']);
      state.capabilities = caps.length ? caps : ['original'];
    } catch (_) {
      if (state.selectedId !== id) return;
      state.capabilities = ['original']; // degrade to original on failure
    }
  }

  async function loadRevisions(id, sig) {
    try {
      const data = await fetchRevisions(id, sig);
      if (state.selectedId !== id) return;
      state.revisions = (data && Array.isArray(data.items)) ? data.items : [];
    } catch (_) {
      if (state.selectedId !== id) return;
      state.revisions = [];
    }
    renderRevisionsPanel();
  }

  function renderRevisionsPanel() {
    const host = byId('artifacts-revisions-panel');
    if (!host) return;
    clear(host);
    const detail = state.detail;
    if (!detail) return;
    const revs = state.revisions;
    if (!revs) { renderState(host, '加载版本...', 'muted loading-state'); return; }
    if (!revs.length) { renderState(host, '无版本记录', 'muted empty-state'); return; }
    const details = el('details', 'artifacts-revisions');
    details.open = false;
    const summary = el('summary', 'artifacts-revisions__summary');
    summary.textContent = '版本历史 (' + revs.length + ')';
    details.appendChild(summary);
    const list = el('ul', 'artifacts-revisions__list');
    const currentId = detail.current_revision_id || null;
    revs.forEach((rev) => list.appendChild(renderRevisionRow(rev, currentId, detail)));
    details.appendChild(list);
    host.appendChild(details);
    // diff view (safe textContent in a <pre>; no raw HTML assignment)
    if (state.diffView && state.diffView.text != null) {
      const diffWrap = el('div', 'artifacts-diff');
      const diffTitle = el('div', 'artifacts-diff__title');
      diffTitle.textContent = '差异: v' + (state.diffView.fromLabel != null ? state.diffView.fromLabel : '?')
        + ' -> v' + (state.diffView.toLabel != null ? state.diffView.toLabel : '?');
      diffWrap.appendChild(diffTitle);
      const pre = el('pre', 'artifacts-diff__pre');
      pre.textContent = state.diffView.text;
      diffWrap.appendChild(pre);
      const closeBtn = el('button', 'btn artifacts-diff__close');
      closeBtn.type = 'button';
      closeBtn.textContent = '关闭差异';
      closeBtn.addEventListener('click', () => { state.diffView = null; renderRevisionsPanel(); });
      diffWrap.appendChild(closeBtn);
      host.appendChild(diffWrap);
    }
  }

  function renderRevisionRow(rev, currentId, detail) {
    const li = el('li', 'artifacts-revisions__item');
    if (rev.is_current) li.classList.add('artifacts-revisions__item--current');
    const meta = el('div', 'artifacts-revisions__meta');
    const num = el('span', 'artifacts-revisions__num');
    num.textContent = 'v' + (rev.revision_number != null ? rev.revision_number : '?');
    meta.appendChild(num);
    if (rev.is_current) {
      const cur = el('span', 'artifacts-revisions__badge artifacts-revisions__badge--current');
      cur.textContent = '当前';
      meta.appendChild(cur);
    }
    if (rev.is_published) {
      const pub = el('span', 'artifacts-revisions__badge artifacts-revisions__badge--published');
      pub.textContent = '已发布';
      meta.appendChild(pub);
    }
    const time = el('span', 'artifacts-revisions__time muted');
    time.textContent = fmtTime(rev.created_at);
    meta.appendChild(time);
    li.appendChild(meta);
    const summaryText = el('div', 'artifacts-revisions__summary-text');
    summaryText.textContent = rev.change_summary || '';
    li.appendChild(summaryText);
    // Actions for non-current revisions: compare with current + rollback.
    // Rollback carries the page's current expected_revision_id (CAS); on
    // conflict the server rejects and we refresh (NO auto-replay).
    if (!rev.is_current && rev.id && currentId) {
      const actions = el('div', 'artifacts-revisions__actions');
      const diffBtn = el('button', 'btn artifacts-revisions__diff');
      diffBtn.type = 'button';
      diffBtn.textContent = '对比当前';
      diffBtn.addEventListener('click', () => doDiff(detail.id, rev.id, currentId, rev.revision_number));
      actions.appendChild(diffBtn);
      const rbBtn = el('button', 'btn artifacts-revisions__rollback');
      rbBtn.type = 'button';
      rbBtn.textContent = '回滚到此版本';
      rbBtn.addEventListener('click', () => doRollback(detail, rev));
      actions.appendChild(rbBtn);
      li.appendChild(actions);
    }
    return li;
  }

  async function doDiff(id, fromId, toId, fromNum) {
    try {
      const sig = inflight ? inflight.signal : undefined;
      const data = await fetchDiff(id, fromId, toId, sig);
      // diff_text is a server-produced unified diff; render as safe textContent.
      const text = data && typeof data.diff_text === 'string' ? data.diff_text
        : (data && data.binary_changed ? '（二进制内容变化，无文本差异）' : '（无差异）');
      state.diffView = { text: text, fromLabel: fromNum != null ? fromNum : '?', toLabel: '当前' };
      renderRevisionsPanel();
    } catch (e) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('对比失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  async function doRollback(detail, rev) {
    if (!detail || !detail.id || !rev || !rev.id) return;
    const expected = detail.current_revision_id;
    if (!expected) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('当前版本未知，请刷新后重试');
      }
      return;
    }
    const targetNum = rev.revision_number != null ? rev.revision_number : '?';
    const confirmed = getModal() && typeof getModal().confirm === 'function'
      ? await getModal().confirm('回滚到 v' + targetNum + '？将基于该版本创建一个新版本（不删除中间版本）。')
      : global.confirm('回滚到 v' + targetNum + '？将基于该版本创建一个新版本。');
    if (!confirmed) return;
    try {
      await rollbackArtifact(detail.id, rev.id, expected);
      // success: reload detail + revisions (new current revision). No auto-replay.
      state.diffView = null;
      if (inflight) { try { inflight.abort(); } catch (_) {} }
      revokeActiveBlobs();
      inflight = new AbortController();
      loadDetail(detail.id, inflight.signal);
      reloadList();
    } catch (e) {
      if (e && e.code === 'artifact_revision_conflict') {
        // CAS conflict: keep current version, prompt re-read, NO auto-replay.
        if (getModal() && typeof getModal().alert === 'function') {
          await getModal().alert('版本已变化，请刷新后重试');
        }
        if (inflight) { try { inflight.abort(); } catch (_) {} }
        revokeActiveBlobs();
        inflight = new AbortController();
        loadDetail(detail.id, inflight.signal);
      } else if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('回滚失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  // ---- lifecycle ----
  async function init() {
    const root = byId('tab-artifacts');
    if (!root) return;
    buildShell(root);
    window.addEventListener('popstate', handlePathChange);
    await reloadList();
    const pendingId = pendingArtifactIdFromPath();
    if (pendingId) selectArtifact(pendingId);
  }

  async function refresh() {
    // reload list; keep current selection if any
    await reloadList();
    if (state.selectedId) {
      // re-render detail from cached state
      renderDetail();
    } else {
      // navigated to /artifacts/{id} from elsewhere: open it
      const pendingId = pendingArtifactIdFromPath();
      if (pendingId) selectArtifact(pendingId);
    }
  }

  function deactivate() {
    // abort in-flight fetches
    if (inflight) { try { inflight.abort(); } catch (_) {} inflight = null; }
    // revoke all blob URLs
    revokeActiveBlobs();
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
  }

  namespace.artifacts = { init, refresh, deactivate, renderListItem };
  global.NAGENT = namespace;
  global.NAGENT.artifacts = namespace.artifacts;

  // probe API and reveal nav on success (disabled service -> stays hidden)
  probeAndToggleNav();
}(window));
