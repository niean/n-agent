/* artifacts.js -- NAGENT.artifacts Artifact workbench (T15).
 *
 * Two-column workbench at /artifacts:
 *   left  -> artifact list (kind icon, name, source label, updated time) +
 *            search + source_kind/kind/status filters + load-more (cursor)
 *   right -> header (name, valid source backlink, [编辑][导出▾][发布]) +
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
  async function patchArtifactMultipart(id, formData, signal) {
    return fetch(API_BASE + '/' + encodeURIComponent(id), {
      method: 'PATCH', body: formData, signal,
    }).then(async (resp) => {
      const data = await resp.json();
      if (!resp.ok) { const e = new Error(data && data.error && data.error.code || 'request_failed'); e.code = e.message; throw e; }
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

  function renderListItem(a) {
    const item = el('div', 'artifacts-list__item');
    if (state.selectedId === a.id) item.classList.add('artifacts-list__item--active');
    item.addEventListener('click', () => selectArtifact(a.id));
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

  async function loadDetail(id, sig) {
    try {
      const [detail, contentResp] = await Promise.all([
        fetchDetail(id, sig),
        fetchContent(id, sig).catch((e) => { if (e.code === 'artifact_content_unavailable') return { unavailable: true }; throw e; }),
      ]);
      if (state.selectedId !== id) return; // raced
      state.detail = detail;
      state.content = await parseContent(contentResp, detail);
      // load publish status in parallel (non-blocking)
      loadPublishStatus(id, sig);
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
      renderPublishArea();
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
    body.appendChild(meta);
    if (state.view === 'edit') {
      renderEditor(body, detail);
    } else {
      renderPreview(body, detail);
    }
    renderPublishArea(body);
  }

  function renderHeader(body, detail) {
    const header = el('div', 'artifacts-detail__header');
    const name = el('span', 'artifacts-detail__name');
    name.textContent = detail.name || detail.id;
    header.appendChild(name);
    // source backlink (only valid controlled task ids)
    const backHref = sourceBacklink(detail);
    if (backHref) {
      const back = el('a', 'artifacts-detail__backlink');
      back.href = backHref;
      back.textContent = sourceLabel(detail.source_kind);
      back.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (global.NAGENT && global.NAGENT.navigation && typeof global.NAGENT.navigation.navigatePath === 'function') {
          global.NAGENT.navigation.navigatePath(backHref);
        } else { global.location.href = backHref; }
      });
      header.appendChild(back);
    }
    const actions = el('div', 'panel-actions artifacts-detail__actions');
    // edit button
    const editBtn = el('button', 'btn artifacts-detail__edit');
    editBtn.type = 'button';
    editBtn.textContent = state.view === 'edit' ? '取消' : '编辑';
    editBtn.addEventListener('click', () => toggleEdit());
    actions.appendChild(editBtn);
    // export dropdown
    actions.appendChild(buildExportDropdown(detail));
    // publish button
    const pubBtn = el('button', 'btn artifacts-detail__publish');
    pubBtn.type = 'button';
    pubBtn.textContent = '发布';
    pubBtn.addEventListener('click', () => doPublish());
    actions.appendChild(pubBtn);
    header.appendChild(actions);
    body.appendChild(header);
  }

  function buildExportDropdown(detail) {
    const wrap = el('div', 'artifacts-detail__export');
    const btn = el('button', 'btn artifacts-detail__export-btn');
    btn.type = 'button';
    btn.textContent = '导出▾';
    const menu = el('div', 'artifacts-detail__export-menu');
    menu.hidden = true;
    // original always
    const orig = el('button', 'btn artifacts-detail__export-item');
    orig.type = 'button';
    orig.textContent = '原始文件 (original)';
    orig.addEventListener('click', () => { doExport(detail.id, 'original'); menu.hidden = true; });
    menu.appendChild(orig);
    // html only for markdown/document
    if (detail.kind === 'markdown' || detail.kind === 'document') {
      const html = el('button', 'btn artifacts-detail__export-item');
      html.type = 'button';
      html.textContent = 'HTML (html)';
      html.addEventListener('click', () => { doExport(detail.id, 'html'); menu.hidden = true; });
      menu.appendChild(html);
    }
    btn.addEventListener('click', () => { menu.hidden = !menu.hidden; });
    wrap.append(btn, menu);
    return wrap;
  }

  async function doExport(id, format) {
    try {
      const resp = await fetchExport(id, format);
      if (!resp.ok) throw new Error('export failed');
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = el('a');
      a.href = url;
      a.download = (format === 'html' ? 'export.html' : 'export');
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('导出失败：' + (e && e.message ? e.message : e));
      }
    }
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
    iframe.setAttribute('sandbox', '');
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
    save.addEventListener('click', () => saveText(detail.id, ta.value));
    const cancel = el('button', 'btn artifacts-detail__cancel');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', () => { state.view = 'preview'; renderDetail(); });
    actions.append(save, cancel);
    wrap.appendChild(actions);
    body.appendChild(wrap);
  }

  async function saveText(id, value) {
    try {
      const updated = await patchArtifact(id, { content: value });
      // refresh with server-returned size/checksum/updated_at
      state.detail = updated;
      state.content = { text: value, kind: updated.kind, mime: updated.mime };
      state.view = 'preview';
      renderDetail();
      // refresh list (metadata may have changed)
      reloadList();
    } catch (e) {
      if (getModal() && typeof getModal().alert === 'function') {
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
      const fd = new FormData();
      fd.append('content', fileInput.files[0]);
      try {
        const updated = await patchArtifactMultipart(detail.id, fd);
        // refresh with server-returned size/checksum/updated_at
        state.detail = updated;
        state.view = 'preview';
        // reload content (binary)
        if (inflight) { try { inflight.abort(); } catch (_) {} }
        revokeActiveBlobs();
        inflight = new AbortController();
        loadDetail(detail.id, inflight.signal);
        reloadList();
      } catch (e) {
        if (getModal() && typeof getModal().alert === 'function') {
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
  function renderPublishArea(body) {
    const host = body || byId('artifacts-detail-body');
    if (!host) return;
    // remove old publish area if present
    const old = host.querySelector('.artifacts-publish');
    if (old) old.remove();
    const pub = state.publish;
    if (!pub) return;
    if (pub.status === 'unpublished' || !pub.status) return;
    const wrap = el('div', 'artifacts-publish');
    if (pub.status === 'active' || pub.status === 'published') {
      const shareUrl = pub.share_url || (pub.share_path ? global.location.origin + pub.share_path : '');
      const urlBox = el('div', 'artifacts-publish__url');
      // Render the share URL as textContent (read-only display) so it is
      // visible without depending on input value rendering.
      const urlText = el('span', 'artifacts-publish__share-url-text');
      urlText.textContent = shareUrl;
      urlBox.appendChild(urlText);
      const input = el('input', 'artifacts-publish__share-url');
      input.type = 'hidden';
      input.value = shareUrl;
      urlBox.appendChild(input);
      const copy = el('button', 'btn artifacts-detail__copy');
      copy.type = 'button';
      copy.textContent = '复制';
      copy.addEventListener('click', () => copyToClipboard(shareUrl));
      urlBox.appendChild(copy);
      wrap.appendChild(urlBox);
      const revoke = el('button', 'btn btn--danger artifacts-detail__revoke');
      revoke.type = 'button';
      revoke.textContent = '撤销发布';
      revoke.addEventListener('click', () => doRevoke(state.detail && state.detail.id));
      wrap.appendChild(revoke);
    } else if (pub.status === 'revoked') {
      const msg = el('div', 'muted artifacts-publish__revoked');
      msg.textContent = '链接已失效（发布已撤销）';
      wrap.appendChild(msg);
    }
    host.appendChild(wrap);
  }

  async function copyToClipboard(text) {
    try {
      if (global.navigator && global.navigator.clipboard && global.navigator.clipboard.writeText) {
        await global.navigator.clipboard.writeText(text);
      }
    } catch (_) { /* clipboard may be unavailable */ }
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
      renderPublishArea();
      // refresh publish status (thread active signal for cancellation)
      loadPublishStatus(detail.id, inflight ? inflight.signal : undefined);
    } catch (e) {
      const code = e && e.code ? e.code : '';
      if (code === 'publish_blocked') {
        renderPublishBlocked();
      } else if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('发布失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  function renderPublishBlocked() {
    const host = byId('artifacts-detail-body');
    if (!host) return;
    const old = host.querySelector('.artifacts-publish');
    if (old) old.remove();
    const wrap = el('div', 'artifacts-publish artifacts-publish__blocked');
    const msg = el('div', 'muted error-state');
    msg.textContent = '发布被阻止（publish_blocked）：该制品不满足发布条件';
    wrap.appendChild(msg);
    host.appendChild(wrap);
  }

  async function doRevoke(id) {
    if (!id) return;
    try {
      const result = await revokePublish(id);
      state.publish = { status: 'revoked', revoked_at: result && result.revoked_at };
      renderPublishArea();
    } catch (e) {
      if (getModal() && typeof getModal().alert === 'function') {
        await getModal().alert('撤销失败：' + (e && e.message ? e.message : e));
      }
    }
  }

  // ---- lifecycle ----
  async function init() {
    const root = byId('tab-artifacts');
    if (!root) return;
    buildShell(root);
    await reloadList();
  }

  async function refresh() {
    // reload list; keep current selection if any
    await reloadList();
    if (state.selectedId) {
      // re-render detail from cached state
      renderDetail();
    }
  }

  function deactivate() {
    // abort in-flight fetches
    if (inflight) { try { inflight.abort(); } catch (_) {} inflight = null; }
    // revoke all blob URLs
    revokeActiveBlobs();
    if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
  }

  namespace.artifacts = { init, refresh, deactivate };
  global.NAGENT = namespace;
  global.NAGENT.artifacts = namespace.artifacts;

  // probe API and reveal nav on success (disabled service -> stays hidden)
  probeAndToggleNav();
}(window));
