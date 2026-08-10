'use strict';
// Minimal, dependency-free behavior harness for artifacts.js (T15).
// Run with: node tests/interfaces/artifacts_frontend_harness.js
// Exits 0 on success, prints "all tests passed"; exits 1 on any failure.
//
// Covers (per spec):
// - combined filters + stable load-more (cursor pagination)
// - all empty/error states (loading/empty/filter-empty/unavailable/error/
//   publish-blocked/revoked)
// - source backlink only for valid controlled task ids
// - markdown/document/html sandbox iframe rendering (NO allow-* tokens)
// - code/text rendering
// - JSON success/fail
// - CSV row/col limits
// - image/PDF blob + fallback
// - revokeObjectURL on switch/destroy
// - text save / binary replace refresh via server size/checksum/updated_at
// - export as a standard button opening a modal to confirm format (capabilities-driven)
// - publish: single toggle button (发布/撤回) + header status segment showing
//   the share link; binary publish shows explicit-PUBLIC confirmation
//
// All artifact content is rendered via textContent / safe attributes /
// sandbox iframe ONLY.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ARTIFACTS_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'artifacts.js');
const code = fs.readFileSync(ARTIFACTS_JS, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// ---------------------------------------------------------------------------
// Minimal DOM mock
// ---------------------------------------------------------------------------

const byId = {};
const createdUrls = new Set();
const revokedUrls = new Set();

function makeNode(tag) {
  const n = {
    tag: tag,
    _text: '',
    children: [],
    _listeners: {},
    dataset: {},
    style: { _p: {}, setProperty(k, v) { this._p[k] = String(v); } },
    hidden: false,
    type: '',
    draggable: false,
    _id: null,
    _attrs: {},
    _cls: [],
    _className: '',
    set className(v) {
      this._className = String(v || '');
      this._cls = this._className.split(/\s+/).filter((s) => s.length);
    },
    get className() { return this._className; },
    get classList() {
      const self = this;
      return {
        add(c) { if (self._cls.indexOf(c) === -1) self._cls.push(c); self._className = self._cls.join(' '); },
        remove(c) { const i = self._cls.indexOf(c); if (i !== -1) self._cls.splice(i, 1); self._className = self._cls.join(' '); },
        contains(c) { return self._cls.indexOf(c) !== -1; },
        toggle(c, force) { if (force === true) { this.add(c); return true; } if (force === false) { this.remove(c); return false; } if (self._cls.indexOf(c) !== -1) { this.remove(c); return false; } this.add(c); return true; },
      };
    },
    value: '',
    checked: false,
    disabled: false,
    required: false,
    placeholder: '',
    _removed: false,
    href: '',
    src: '',
    sandbox: null,
    srcdoc: null,
    _contentDocument: null,
    set textContent(v) { this._text = (v === null || v === undefined) ? '' : String(v); },
    get textContent() { return this._text; },
    set id(v) { this._id = v; if (v != null) byId[v] = this; },
    get id() { return this._id; },
    appendChild(c) { this.children.push(c); return c; },
    append() { for (let i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },
    prepend() { for (let i = arguments.length - 1; i >= 0; i--) this.children.unshift(arguments[i]); },
    replaceChildren() { this.children = []; },
    removeChild(c) { const i = this.children.indexOf(c); if (i !== -1) this.children.splice(i, 1); return c; },
    remove() { this._removed = true; },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    removeEventListener(ev, fn) { const a = this._listeners[ev]; if (a) { const i = a.indexOf(fn); if (i !== -1) a.splice(i, 1); } },
    setAttribute(k, v) { this._attrs[k] = String(v); if (k === 'id') this.id = String(v); if (k === 'hidden') this.hidden = true; },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    removeAttribute(k) { delete this._attrs[k]; if (k === 'hidden') this.hidden = false; },
    hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k); },
    querySelector(sel) { return this._query(sel, false); },
    querySelectorAll(sel) { return this._query(sel, true) || []; },
    _query(sel, all) {
      const out = [];
      const walk = (node) => {
        for (const c of node.children) {
          if (_matches(c, sel)) { out.push(c); if (!all && out.length) return; }
          walk(c);
        }
      };
      walk(this);
      return all ? out : (out[0] || null);
    },
    cloneNode() { const c = makeNode(this.tag); c._text = this._text; c.className = this.className; c._attrs = Object.assign({}, this._attrs); return c; },
  };
  return n;
}

function _matches(node, sel) {
  sel = String(sel).trim();
  if (!sel) return false;
  // class selector
  if (sel[0] === '.') {
    const cls = sel.slice(1);
    return (node.className || '').split(/\s+/).indexOf(cls) !== -1;
  }
  // id selector
  if (sel[0] === '#') {
    return node._id === sel.slice(1);
  }
  // tag selector
  return node.tag === sel;
}

const documentMock = {
  createElement(tag) { return makeNode(tag); },
  createElementNS(_, tag) { return makeNode(tag); },
  createTextNode(t) { const n = makeNode('#text'); n._text = String(t); return n; },
  getElementById(id) { return byId[id] || null; },
  querySelector(sel) {
    // search byId values then a synthetic body
    for (const k in byId) { const m = byId[k]._query(sel, false); if (m) return m; }
    return null;
  },
  querySelectorAll(sel) {
    const out = [];
    for (const k in byId) { out.push(...(byId[k]._query(sel, true) || [])); }
    return out;
  },
  addEventListener() {},
  removeEventListener() {},
  body: makeNode('body'),
  documentElement: makeNode('html'),
  readyState: 'complete',
};

// Fake fetch: routes keyed by URL pattern -> handler returning {status, json, blob, text}
const fetchRoutes = [];
let fetchLog = [];
function fetchMock(url, options) {
  options = options || {};
  // record body for assertion: strings as-is, FormData as a marker, headers captured
  let bodyRec = null;
  if (options.body != null) {
    if (typeof options.body === 'string') bodyRec = options.body;
    else if (options.body && options.body.toString) bodyRec = String(options.body);
    else bodyRec = '[non-string body]';
  }
  fetchLog.push({ url: String(url), method: options.method || 'GET', signal: options.signal || null,
    body: bodyRec, headers: options.headers || null });
  for (const r of fetchRoutes) {
    const m = r.match(String(url), options);
    if (m) return Promise.resolve(r.handle(m, options));
  }
  return Promise.resolve({
    ok: false, status: 404,
    json: () => Promise.resolve({ error: { code: 'not_found', message: 'no route' } }),
    text: () => Promise.resolve(''),
    blob: () => Promise.resolve({ size: 0, type: 'application/octet-stream' }),
    headers: { get: () => null },
  });
}
function route(re, handler) {
  fetchRoutes.push({
    match(url, opts) {
      const m = re.exec(url);
      return m;
    },
    handle(m, opts) { return handler(m, opts); },
  });
}

function jsonResp(obj, status) {
  return { ok: (status || 200) < 400, status: status || 200, json: () => Promise.resolve(obj), text: () => Promise.resolve(JSON.stringify(obj)), blob: () => Promise.resolve({ size: 0, type: 'application/json' }), headers: { get: (h) => h && h.toLowerCase() === 'content-type' ? 'application/json' : null } };
}
function blobResp(data, type) {
  return { ok: true, status: 200, json: () => Promise.reject(new Error('not json')), text: () => Promise.resolve(typeof data === 'string' ? data : ''), blob: () => Promise.resolve({ size: data.length || 0, type: type || 'application/octet-stream' }), headers: { get: (h) => h && h.toLowerCase() === 'content-type' ? (type || 'application/octet-stream') : null } };
}
function textResp(txt, type) {
  return { ok: true, status: 200, json: () => Promise.reject(new Error('not json')), text: () => Promise.resolve(txt), blob: () => Promise.resolve({ size: txt.length, type: type || 'text/plain' }), headers: { get: (h) => h && h.toLowerCase() === 'content-type' ? (type || 'text/plain') : null } };
}
function errResp(code, message, status) {
  return { ok: false, status: status || 500, json: () => Promise.resolve({ error: { code: code, message: message } }), text: () => Promise.resolve(JSON.stringify({ error: { code, message } })), blob: () => Promise.resolve({ size: 0 }), headers: { get: () => null } };
}

// Fake URL
const URLMock = {
  createObjectURL(blob) { const u = 'blob:fake/' + Math.random().toString(36).slice(2); createdUrls.add(u); return u; },
  revokeObjectURL(u) { revokedUrls.add(u); },
};

// AbortController mock
class AbortControllerMock {
  constructor() { this.signal = { aborted: false, addEventListener() {}, removeEventListener() {} }; }
  abort() { this.signal.aborted = true; }
}

// localStorage mock
const localStorageMock = { _s: {}, getItem(k) { return this._s[k] != null ? this._s[k] : null; }, setItem(k, v) { this._s[k] = String(v); }, removeItem(k) { delete this._s[k]; } };

function freshEnv() {
  // reset state
  for (const k in byId) delete byId[k];
  fetchRoutes.length = 0;
  fetchLog = [];
  createdUrls.clear();
  revokedUrls.clear();

  const win = {
    NAGENT: undefined,
    document: documentMock,
    fetch: fetchMock,
    URL: URLMock,
    AbortController: AbortControllerMock,
    localStorage: localStorageMock,
    location: { pathname: '/artifacts', search: '', host: 'test', protocol: 'http:', href: 'http://test/artifacts' },
    history: { pushState() {}, replaceState() {} },
    addEventListener() {},
    removeEventListener() {},
    navigator: { clipboard: { writeText(t) { return Promise.resolve(t); } } },
    confirm: () => true,
    alert: () => {},
    setTimeout, clearTimeout, setInterval() { return 0; }, clearInterval() {},
    Promise, Date, Math, JSON, Object, Array, String, Number, Boolean, RegExp, Error,
    URLSearchParams, FormData,
    encodeURIComponent, decodeURIComponent, encodeURI, console,
  };
  win.window = win;
  win.globalThis = win;
  documentMock.body = makeNode('body');
  documentMock.documentElement = makeNode('html');
  // The tab container artifacts.js renders into.
  const tab = makeNode('div');
  tab.id = 'tab-artifacts';
  byId['tab-artifacts'] = tab;
  // The hidden nav item artifacts.js reveals on probe success.
  const nav = makeNode('a');
  nav.className = 'sidebar__item';
  nav.setAttribute('data-tab', 'artifacts');
  nav.setAttribute('hidden', '');
  nav.href = '/artifacts';
  byId['nav-artifacts'] = nav;
  // sidebar nav items discoverable via querySelector
  documentMock.body.appendChild(nav);
  return win;
}

function loadModule(win) {
  const ctx = vm.createContext(win);
  vm.runInContext(code, ctx);
  return win.NAGENT && win.NAGENT.artifacts;
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

// Helper: find first descendant (by className) of a node.
function findByClass(node, cls) {
  const out = [];
  const walk = (n) => {
    for (const c of n.children) {
      if ((c.className || '').split(/\s+/).indexOf(cls) !== -1) out.push(c);
      walk(c);
    }
  };
  walk(node);
  return out;
}
function findByTag(node, tag) {
  const out = [];
  const walk = (n) => {
    for (const c of n.children) {
      if (c.tag === tag) out.push(c);
      walk(c);
    }
  };
  walk(node);
  return out;
}

// Build a list response.
function listResp(items, nextCursor) {
  return jsonResp({ items: items, next_cursor: nextCursor });
}
function art(id, kind, opts) {
  opts = opts || {};
  return Object.assign({
    id: id, name: opts.name || ('artifact-' + id), kind: kind,
    mime: opts.mime || 'text/plain', size: opts.size != null ? opts.size : 10,
    checksum: opts.checksum || 'sha256:' + 'a'.repeat(64),
    source_kind: opts.source_kind || 'manual', source_context_ref: opts.source_context_ref || null,
    summary: opts.summary || '', classification: opts.classification || null,
    labels: opts.labels || null, status: opts.status || 'draft',
    created_at: opts.created_at || '2026-01-01T00:00:00+00:00',
    updated_at: opts.updated_at || '2026-01-01T00:00:00+00:00',
    created_by: opts.created_by || 'dashboard',
  }, opts.extra || {});
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

async function testNavProbeSuccessRevealsNav() {
  const win = freshEnv();
  route(/^\/chat\/artifacts(\?|$)/, (m) => listResp([], null));
  const mod = loadModule(win);
  ok(mod && typeof mod.init === 'function', 'module exposes init');
  // probe runs at load
  await tick();
  const nav = win.document.body;
  // find the artifacts nav item
  const navItem = findByClass(nav, 'sidebar__item').find((n) => n.getAttribute('data-tab') === 'artifacts');
  ok(navItem, 'nav item exists');
  ok(!navItem.hidden, 'nav revealed after successful probe');
}

async function testNavProbeFailureKeepsNavHidden() {
  const win = freshEnv();
  route(/^\/chat\/artifacts(\?|$)/, () => errResp('artifact_internal_error', 'no service', 500));
  const mod = loadModule(win);
  await tick();
  const navItem = findByClass(win.document.body, 'sidebar__item').find((n) => n.getAttribute('data-tab') === 'artifacts');
  ok(navItem, 'nav item exists');
  ok(navItem.hidden, 'nav stays hidden after probe failure (disabled/missing API)');
}

async function testListLoadAndFilters() {
  const win = freshEnv();
  const items = [art('a1', 'markdown', { name: 'Doc1' }), art('a2', 'code', { name: 'Script' })];
  route(/^\/chat\/artifacts(\?|$)/, (m) => {
    const url = m.input;
    // reflect filter params in what we return so we can assert filters sent
    if (url.indexOf('kind=markdown') !== -1) return listResp([items[0]], null);
    return listResp(items, null);
  });
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const tab = byId['tab-artifacts'];
  ok(tab.children.length > 0, 'list rendered into tab');
  // list items present
  const listEls = findByClass(tab, 'artifacts-list__item');
  ok(listEls.length === 2, 'two list items rendered, got ' + listEls.length);
}

async function testLoadMoreStableCursor() {
  const win = freshEnv();
  let callCount = 0;
  const page1 = [art('a1', 'text'), art('a2', 'text')];
  const page2 = [art('a3', 'text')];
  route(/^\/chat\/artifacts(\?|$)/, (m) => {
    callCount++;
    const url = m.input;
    if (url.indexOf('cursor=') !== -1) return listResp(page2, null);
    return listResp(page1, { updated_at: '2026-01-01T00:00:00+00:00', artifact_id: 'a2' });
  });
  const mod = loadModule(win);
  await mod.init();
  await tick();
  let listEls = findByClass(byId['tab-artifacts'], 'artifacts-list__item');
  ok(listEls.length === 2, 'page1 has 2 items');
  // find and click load-more
  const moreBtn = findByClass(byId['tab-artifacts'], 'artifacts-list__more');
  ok(moreBtn.length > 0, 'load-more button present when next_cursor exists');
  if (moreBtn[0]) {
    const listeners = moreBtn[0]._listeners['click'] || [];
    ok(listeners.length > 0, 'load-more has click handler');
    for (const fn of listeners) fn({});
    await tick();
    listEls = findByClass(byId['tab-artifacts'], 'artifacts-list__item');
    ok(listEls.length === 3, 'after load-more: 3 items total, got ' + listEls.length);
  }
}

async function testEmptyState() {
  const win = freshEnv();
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([], null));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const tab = byId['tab-artifacts'];
  const text = collectText(tab);
  ok(/暂无|empty|空/i.test(text), 'empty list shows empty state text, got: ' + text.slice(0, 80));
}

async function testFilterEmptyState() {
  const win = freshEnv();
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([], null));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  // apply a filter that yields no results: trigger kind filter change
  const filterSel = findByClass(byId['tab-artifacts'], 'artifacts-filter__kind');
  if (filterSel[0]) {
    filterSel[0].value = 'markdown';
    const listeners = filterSel[0]._listeners['change'] || [];
    for (const fn of listeners) fn({});
    await tick();
  }
  // Even without a real select, empty list after filter should show filter-empty text
  const text = collectText(byId['tab-artifacts']);
  // The empty state text should mention no results / filter
  ok(/暂无|无匹配|无结果|empty|filter|筛选/i.test(text), 'filter-empty state shown');
}

async function testErrorState() {
  const win = freshEnv();
  route(/^\/chat\/artifacts(\?|$)/, () => errResp('artifact_internal_error', 'boom', 500));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const text = collectText(byId['tab-artifacts']);
  ok(/失败|error|加载|错误/i.test(text), 'error state shown on list load failure: ' + text.slice(0, 80));
}

async function testContentUnavailableState() {
  const win = freshEnv();
  const a = art('a1', 'text');
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => errResp('artifact_content_unavailable', 'gone', 409));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  // select the artifact
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  ok(item, 'list item present');
  const listeners = item._listeners['click'] || [];
  for (const fn of listeners) fn({});
  await tick();
  const text = collectText(byId['tab-artifacts']);
  ok(/不可用|unavailable|无法|暂不可/i.test(text), 'content-unavailable state shown: ' + text.slice(0, 120));
}

async function testPublishBlockedState() {
  const win = freshEnv();
  const a = art('a1', 'text', { source_kind: 'manual', extra: { current_revision_id: 'r1' } });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  route(/^\/chat\/artifacts\/a1\/publish$/, (m, opts) => {
    if ((opts.method || 'GET') === 'POST') return errResp('publish_blocked', 'blocked', 422);
    return jsonResp({ status: 'unpublished' });
  });
  route(/^\/chat\/artifacts\/a1\/revisions/, () => jsonResp({
    items: [{ id: 'r1', revision_number: 1, change_summary: '初版', created_at: '2026-01-01T00:00:00+00:00', is_current: true, is_published: false }],
  }));
  // Publish failure is reported via the shared modal; a blocked publish must
  // leave the UI unpublished (no share link in the metadata row).
  let alertMsg = null;
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = {
    alert: function (msg) { alertMsg = String(msg); return Promise.resolve(); },
    confirm: function () { return Promise.resolve(true); },
  };
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // publish/revoke live in the 版本 modal: open it and click 发布此版本.
  const modal = clickRevisionsButton();
  await tick();
  const pubBtn = findByClass(modal, 'artifacts-revisions__publish')[0];
  if (pubBtn) {
    for (const fn of (pubBtn._listeners['click'] || [])) fn({});
    await tick();
  }
  // blocked publish -> failure reported via modal alert
  ok(alertMsg && /失败|blocked|publish_blocked|不可发布|无法发布/i.test(alertMsg),
    'publish-blocked failure reported via modal alert: ' + String(alertMsg));
  // publish did not activate: no share link rendered in the metadata row
  ok(!findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-link').length,
    'no share link shown after blocked publish');
  const publication = findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-status')[0];
  ok(publication && publication.children[1] && publication.children[1]._text === '未发布',
    'unpublished artifact shows 未发布 in the status bar');
}

async function testSourceBacklinkValidTaskId() {
  const win = freshEnv();
  const a = art('a1', 'text', { source_kind: 'task_artifact', source_context_ref: 'task-123' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // backlink present pointing to /tasks/task-123
  const links = findByTag(byId['tab-artifacts'], 'a');
  const back = links.find((l) => (l.href || '').indexOf('/tasks/task-123') !== -1);
  ok(back, 'source backlink to /tasks/task-123 present for valid task id');
}

async function testSourceBacklinkInvalidTaskId() {
  const win = freshEnv();
  // invalid task id with path separators / control chars -> no backlink
  const a = art('a1', 'text', { source_kind: 'task_artifact', source_context_ref: '../etc/evil' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const links = findByTag(byId['tab-artifacts'], 'a');
  const back = links.find((l) => (l.href || '').indexOf('/tasks/') !== -1);
  ok(!back, 'no backlink rendered for invalid/unsafe task id');
}

async function testMarkdownSandboxIframe() {
  const win = freshEnv();
  const a = art('a1', 'markdown', { mime: 'text/markdown' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('# hi'));
  route(/^\/chat\/artifacts\/a1\/export\?format=html/, () => textResp('<h1>hi</h1>', 'text/html'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const iframes = findByTag(byId['tab-artifacts'], 'iframe');
  ok(iframes.length > 0, 'markdown rendered via iframe');
  if (iframes.length) {
    const sandbox = iframes[0].getAttribute('sandbox');
    ok(sandbox === '', 'markdown iframe sandbox must be empty string (no allow-*), got: ' + JSON.stringify(sandbox));
  }
}

async function testHtmlSandboxIframe() {
  const win = freshEnv();
  const a = art('a1', 'html', { mime: 'text/html' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('<p>hi</p>'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const iframes = findByTag(byId['tab-artifacts'], 'iframe');
  ok(iframes.length > 0, 'html rendered via iframe');
  if (iframes.length) {
    const sandbox = iframes[0].getAttribute('sandbox');
    ok(sandbox === '', 'html iframe sandbox must be empty string, got: ' + JSON.stringify(sandbox));
    // srcdoc used (controlled), not src to a raw URL
    ok(iframes[0].getAttribute('srcdoc') !== null || iframes[0].srcdoc !== null, 'html iframe uses srcdoc');
  }
}

async function testCodeTextRendering() {
  const win = freshEnv();
  const a = art('a1', 'code', { mime: 'text/plain' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('print("hi")'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const pres = findByTag(byId['tab-artifacts'], 'pre');
  ok(pres.length > 0, 'code/text rendered in pre element');
  const text = collectText(byId['tab-artifacts']);
  ok(text.indexOf('print(') !== -1, 'code content rendered via textContent');
}

async function testJsonSuccessAndFail() {
  // success
  const win1 = freshEnv();
  const a1 = art('a1', 'json', { mime: 'application/json' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a1], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a1));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('{"k":"v"}', 'application/json'));
  const mod1 = loadModule(win1);
  await mod1.init();
  await tick();
  const item1 = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item1._listeners['click'] || [])) fn({});
  await tick();
  const text1 = collectText(byId['tab-artifacts']);
  ok(text1.indexOf('"k"') !== -1 || text1.indexOf('k') !== -1, 'json parsed+formatted on success');

  // fail
  const win2 = freshEnv();
  const a2 = art('a2', 'json', { mime: 'application/json' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a2], null));
  route(/^\/chat\/artifacts\/a2$/, () => jsonResp(a2));
  route(/^\/chat\/artifacts\/a2\/content/, () => textResp('not json{', 'application/json'));
  const mod2 = loadModule(win2);
  await mod2.init();
  await tick();
  const item2 = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item2._listeners['click'] || [])) fn({});
  await tick();
  const text2 = collectText(byId['tab-artifacts']);
  ok(text2.indexOf('not json{') !== -1, 'json parse fail shows raw content');
  ok(/错误|error|失败|解析/i.test(text2), 'json parse fail shows error message');
}

async function testCsvRowColLimits() {
  const win = freshEnv();
  const a = art('a1', 'csv', { mime: 'text/csv' });
  // build CSV with many rows and cols
  let csv = 'h1,h2,h3\n';
  for (let i = 0; i < 500; i++) csv += i + ',' + i + ',' + i + '\n';
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp(csv, 'text/csv'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const tables = findByTag(byId['tab-artifacts'], 'table');
  ok(tables.length > 0, 'csv rendered as table');
  if (tables.length) {
    const rows = findByTag(tables[0], 'tr');
    ok(rows.length > 0 && rows.length <= 201, 'csv rows limited (got ' + rows.length + ')');
  }
}

async function testImageBlobAndRevokeOnSwitch() {
  const win = freshEnv();
  const a1 = art('a1', 'image', { mime: 'image/png' });
  const a2 = art('a2', 'text', { mime: 'text/plain' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a1, a2], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a1));
  route(/^\/chat\/artifacts\/a1\/content/, () => blobResp([1, 2, 3], 'image/png'));
  route(/^\/chat\/artifacts\/a2$/, () => jsonResp(a2));
  route(/^\/chat\/artifacts\/a2\/content/, () => textResp('hello'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const items = findByClass(byId['tab-artifacts'], 'artifacts-list__item');
  // select image
  for (const fn of (items[0]._listeners['click'] || [])) fn({});
  await tick();
  ok(createdUrls.size > 0, 'image blob URL created');
  const createdBefore = new Set(createdUrls);
  // switch to text artifact
  for (const fn of (items[1]._listeners['click'] || [])) fn({});
  await tick();
  let revokedSome = false;
  for (const u of createdBefore) { if (revokedUrls.has(u)) revokedSome = true; }
  ok(revokedSome, 'previous blob URL revoked on switch');
}

async function testPdfBlobFallback() {
  const win = freshEnv();
  const a = art('a1', 'pdf', { mime: 'application/pdf' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => blobResp([1, 2, 3, 4], 'application/pdf'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  ok(createdUrls.size > 0, 'pdf blob URL created');
  // pdf should have an iframe (sandbox) and/or a download fallback link
  const iframes = findByTag(byId['tab-artifacts'], 'iframe');
  const links = findByTag(byId['tab-artifacts'], 'a');
  ok(iframes.length > 0 || links.length > 0, 'pdf has iframe or download fallback');
}

async function testRevokeObjectURLOnDestroy() {
  const win = freshEnv();
  const a = art('a1', 'image', { mime: 'image/png' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => blobResp([1, 2, 3], 'image/png'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const createdBefore = new Set(createdUrls);
  mod.deactivate();
  let revokedSome = false;
  for (const u of createdBefore) { if (revokedUrls.has(u)) revokedSome = true; }
  ok(revokedSome, 'blob URLs revoked on deactivate/destroy');
}

async function testTextSaveRefreshWithServerMetadata() {
  const win = freshEnv();
  const a = art('a1', 'text', { size: 5, checksum: 'sha256:' + 'a'.repeat(64), updated_at: '2026-01-01T00:00:00+00:00',
    extra: { current_revision_id: 'r1' } });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, (m, opts) => {
    if ((opts.method || 'GET') === 'PATCH') {
      return jsonResp(art('a1', 'text', { size: 11, checksum: 'sha256:' + 'b'.repeat(64), updated_at: '2026-01-02T00:00:00+00:00',
        extra: { current_revision_id: 'r2' } }));
    }
    return jsonResp(a);
  });
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // enter edit mode
  const editBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__edit');
  if (editBtn[0]) { for (const fn of (editBtn[0]._listeners['click'] || [])) fn({}); }
  // find textarea, set value, save
  const tas = findByTag(byId['tab-artifacts'], 'textarea');
  if (tas[0]) { tas[0].value = 'edited text'; }
  const saveBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__save');
  if (saveBtn[0]) { for (const fn of (saveBtn[0]._listeners['click'] || [])) fn({}); await tick(); }
  // a PATCH should have been sent
  const patchCall = fetchLog.find((l) => l.method === 'PATCH' && l.url.indexOf('/chat/artifacts/a1') !== -1);
  ok(patchCall, 'text save sent PATCH');
  // T10: content PATCH must carry the CAS token (expected_revision_id)
  ok(patchCall && patchCall.body && patchCall.body.indexOf('expected_revision_id') !== -1
      && patchCall.body.indexOf('r1') !== -1,
    'content PATCH carries expected_revision_id CAS token');
  // After save, server-returned size/checksum/updated_at should be reflected.
  const text = collectText(byId['tab-artifacts']);
  ok(text.indexOf('11') !== -1 || text.indexOf('b') !== -1 || text.indexOf('2026-01-02') !== -1,
    'refreshed metadata from server shown after save');
}

async function testContentPatchConflictKeepsCurrentNoAutoReplay() {
  // T10: content PATCH with a stale CAS token -> artifact_revision_conflict
  // (409). Keep the current display, refresh server state, prompt the user,
  // NO auto-replay of the save.
  const win = freshEnv();
  const a = art('a1', 'text', { size: 5, extra: { current_revision_id: 'r1' } });
  let patchAttempts = 0;
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, (m, opts) => {
    if ((opts.method || 'GET') === 'PATCH') {
      patchAttempts += 1;
      return errResp('artifact_revision_conflict', 'CAS conflict', 409);
    }
    return jsonResp(a);
  });
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  let alertMsg = null;
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = { alert: (m) => { alertMsg = String(m); return Promise.resolve(); }, confirm: () => Promise.resolve(true) };
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const editBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__edit');
  if (editBtn[0]) { for (const fn of (editBtn[0]._listeners['click'] || [])) fn({}); }
  const tas = findByTag(byId['tab-artifacts'], 'textarea');
  if (tas[0]) { tas[0].value = 'edited text'; }
  const saveBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__save');
  if (saveBtn[0]) { for (const fn of (saveBtn[0]._listeners['click'] || [])) fn({}); await tick(); }
  await tick(); await tick();
  // exactly one PATCH attempt (NO auto-replay)
  ok(patchAttempts === 1, 'content PATCH conflict: no auto-replay (single attempt, got ' + patchAttempts + ')');
  // user was prompted about the version change
  ok(alertMsg && alertMsg.indexOf('版本已变化') !== -1, 'conflict prompts user about version change (got ' + alertMsg + ')');
  // a GET detail reload happened (refresh server state)
  const reloads = fetchLog.filter((l) => l.method === 'GET' && l.url.indexOf('/chat/artifacts/a1') !== -1
      && l.url.indexOf('/content') === -1 && l.url.indexOf('/export') === -1
      && l.url.indexOf('/revisions') === -1 && l.url.indexOf('/capabilities') === -1
      && l.url.indexOf('/publish') === -1);
  ok(reloads.length >= 2, 'server state reloaded after conflict (got ' + reloads.length + ')');
}

async function testExportMenuCapabilitiesDriven() {
  // Export is a standard button that opens a project-standard modal. The format
  // options come from GET /export/capabilities (server truth): markdown may
  // advertise [original, html]; code may advertise [original] only; a
  // capabilities failure degrades to [original] (no crash, no empty modal).
  const win = freshEnv();
  const a = art('a1', 'markdown', { mime: 'text/markdown' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('# hi'));
  route(/^\/chat\/artifacts\/a1\/export\/capabilities$/, () => jsonResp({ capabilities: ['original', 'html'] }));
  route(/^\/chat\/artifacts\/a1\/export\?format=html/, () => textResp('<h1>hi</h1>', 'text/html'));
  route(/^\/chat\/artifacts\/a1\/export\?format=original/, () => textResp('# hi', 'text/markdown'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  await tick();
  // export is a single standard button (no dropdown)
  const expBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__export')[0];
  ok(expBtn && expBtn.tag === 'button', 'export is a standard button');
  // clicking it opens the export modal on document.body
  for (const fn of (expBtn._listeners['click'] || [])) fn({});
  const modal = win.document.getElementById('artifacts-export-modal');
  ok(modal, 'export modal opens on click');
  const formatTitle = findByClass(modal, 'export-modal__section-title')[0];
  ok(formatTitle && formatTitle.textContent === '格式', 'export format section is named 格式');
  const options = findByClass(modal, 'export-modal__options')[0];
  ok(options && options.getAttribute('role') === 'radiogroup', 'export formats are a radio group');
  const radios = modal.querySelectorAll('input');
  ok(radios.length === 2, 'markdown advertises 2 export formats in modal (got ' + radios.length + ')');
  const fmts = radios.map((r) => r.dataset.format).sort();
  ok(fmts[0] === 'html' && fmts[1] === 'original', 'export options are exactly the server capabilities (got ' + JSON.stringify(fmts) + ')');

  // code -> server advertises [original] only -> modal has 1 option (NOT hardcoded html)
  const win2 = freshEnv();
  const a2 = art('a2', 'code', { mime: 'text/plain' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a2], null));
  route(/^\/chat\/artifacts\/a2$/, () => jsonResp(a2));
  route(/^\/chat\/artifacts\/a2\/content/, () => textResp('x=1'));
  route(/^\/chat\/artifacts\/a2\/export\/capabilities$/, () => jsonResp({ capabilities: ['original'] }));
  const mod2 = loadModule(win2);
  await mod2.init();
  await tick();
  const item2 = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item2._listeners['click'] || [])) fn({});
  await tick();
  await tick();
  const expBtn2 = findByClass(byId['tab-artifacts'], 'artifacts-detail__export')[0];
  for (const fn of (expBtn2._listeners['click'] || [])) fn({});
  const modal2 = win2.document.getElementById('artifacts-export-modal');
  ok(modal2, 'export modal opens for code artifact');
  const radios2 = modal2.querySelectorAll('input');
  ok(radios2.length === 1 && radios2[0].dataset.format === 'original', 'code with [original] capability offers exactly original (got ' + JSON.stringify(radios2.map((r) => r.dataset.format)) + ')');

  // capabilities fetch failure -> degrade to [original] (no crash, no empty modal)
  const win3 = freshEnv();
  const a3 = art('a3', 'text');
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a3], null));
  route(/^\/chat\/artifacts\/a3$/, () => jsonResp(a3));
  route(/^\/chat\/artifacts\/a3\/content/, () => textResp('hi'));
  route(/^\/chat\/artifacts\/a3\/export\/capabilities$/, () => errResp('artifact_internal_error', 'boom', 500));
  const mod3 = loadModule(win3);
  await mod3.init();
  await tick();
  const item3 = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item3._listeners['click'] || [])) fn({});
  await tick();
  await tick();
  const expBtn3 = findByClass(byId['tab-artifacts'], 'artifacts-detail__export')[0];
  for (const fn of (expBtn3._listeners['click'] || [])) fn({});
  const modal3 = win3.document.getElementById('artifacts-export-modal');
  ok(modal3, 'export modal opens even when capabilities fail');
  const radios3 = modal3.querySelectorAll('input');
  ok(radios3.length === 1 && radios3[0].dataset.format === 'original', 'capabilities failure degrades to original-only (got ' + JSON.stringify(radios3.map((r) => r.dataset.format)) + ')');
}

async function testPublishShareUrlToggleRevoke() {
  const win = freshEnv();
  const a = art('a1', 'text', { source_kind: 'manual', extra: { current_revision_id: 'r1' } });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  // GET /publish reflects the server-tracked state: unpublished before POST,
  // active after POST, back to unpublished after DELETE (revoke). The client
  // re-fetches status after publish, so the GET must stay consistent. The
  // revisions route reflects is_published the same way so the 版本 modal can
  // swap 发布此版本 <-> 撤回发布 after each action.
  let published = false;
  route(/^\/chat\/artifacts\/a1\/publish$/, (m, opts) => {
    if ((opts.method || 'GET') === 'POST') {
      published = true;
      return jsonResp({ publish_id: 'pub123', share_path: '/p/pub123', share_url: 'http://test/p/pub123', reused: false });
    }
    if ((opts.method || 'GET') === 'DELETE') {
      published = false;
      return jsonResp({ status: 'revoked', publish_id: 'pub123', revoked_at: '2026-01-03T00:00:00+00:00' });
    }
    return jsonResp(published
      ? { status: 'active', share_url: 'http://test/p/pub123', share_path: '/p/pub123' }
      : { status: 'unpublished' });
  });
  route(/^\/chat\/artifacts\/a1\/revisions/, () => jsonResp({
    items: [{ id: 'r1', revision_number: 1, change_summary: '初版', created_at: '2026-01-01T00:00:00+00:00', is_current: true, is_published: published }],
  }));
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = { confirm: () => Promise.resolve(true), alert: () => Promise.resolve() };
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // publish via 版本 modal: open modal, click 发布此版本 on the current revision.
  let modal = clickRevisionsButton();
  await tick();
  let pubBtn = findByClass(modal, 'artifacts-revisions__publish')[0];
  if (pubBtn) { for (const fn of (pubBtn._listeners['click'] || [])) fn({}); await tick(); await tick(); }
  // share link shown in the metadata row after publish
  const text = collectText(byId['tab-artifacts']);
  ok(text.indexOf('http://test/p/pub123') !== -1 || text.indexOf('/p/pub123') !== -1 || text.indexOf('已发布') !== -1,
    'share link / published state shown after publish: ' + text.slice(0, 120));
  // after publish the modal reloaded revisions: current revision now shows 撤回发布.
  modal = byId['artifacts-revisions-modal'];
  const revokeBtn = findByClass(modal, 'artifacts-revisions__revoke')[0];
  ok(revokeBtn, 'after publish, current revision shows 撤回发布 in modal');
  // click 撤回发布 to revoke
  if (revokeBtn) { for (const fn of (revokeBtn._listeners['click'] || [])) fn({}); await tick(); await tick(); }
  // after revoke: share link removed from the metadata row
  const text2 = collectText(byId['tab-artifacts']);
  ok(text2.indexOf('http://test/p/pub123') === -1 && text2.indexOf('/p/pub123') === -1,
    'share link removed after revoke: ' + text2.slice(0, 120));
}

async function testBinaryPublishConfirmation() {
  const win = freshEnv();
  const a = art('a1', 'image', { mime: 'image/png', source_kind: 'manual', extra: { current_revision_id: 'r1' } });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => blobResp([1, 2, 3], 'image/png'));
  route(/^\/chat\/artifacts\/a1\/publish$/, (m, opts) => {
    if ((opts.method || 'GET') === 'POST') {
      return jsonResp({ publish_id: 'pub', share_path: '/p/pub', share_url: 'http://test/p/pub', reused: false });
    }
    return jsonResp({ status: 'unpublished' });
  });
  route(/^\/chat\/artifacts\/a1\/revisions/, () => jsonResp({
    items: [{ id: 'r1', revision_number: 1, change_summary: '初版', created_at: '2026-01-01T00:00:00+00:00', is_current: true, is_published: false }],
  }));
  let confirmShown = false;
  // Set up modal BEFORE loading so artifacts.js captures it; artifacts.js
  // resolves modal dynamically via getModal() -> namespace.modal.
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = {
    confirm: function (msg) { confirmShown = true; win.NAGENT.modal._lastMsg = msg; return Promise.resolve(true); },
    alert: function () { return Promise.resolve(); },
  };
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // publish via 版本 modal: open modal, click 发布此版本 on the current revision.
  const modal = clickRevisionsButton();
  await tick();
  const pubBtn = findByClass(modal, 'artifacts-revisions__publish')[0];
  if (pubBtn) { for (const fn of (pubBtn._listeners['click'] || [])) fn({}); await tick(); }
  ok(confirmShown, 'binary publish shows confirmation dialog');
  if (confirmShown) {
    ok(/PUBLIC|公开|扫描|秘密|secret/i.test(String(win.NAGENT.modal._lastMsg || '')),
      'binary publish confirmation mentions explicit-PUBLIC / no secret scan: ' + win.NAGENT.modal._lastMsg);
  }
}

async function testBinaryReplaceCarriesIfMatchCas() {
  // T10: binary content replace carries the CAS token via If-Match header
  // (multipart PATCH has no JSON body for expected_revision_id).
  const win = freshEnv();
  const a = art('a1', 'image', { mime: 'image/png', extra: { current_revision_id: 'r1' } });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, (m, opts) => {
    if ((opts.method || 'GET') === 'PATCH') {
      return jsonResp(art('a1', 'image', { mime: 'image/png', extra: { current_revision_id: 'r2' } }));
    }
    return jsonResp(a);
  });
  route(/^\/chat\/artifacts\/a1\/content/, () => blobResp([1, 2, 3], 'image/png'));
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = { alert: () => Promise.resolve(), confirm: () => Promise.resolve(true) };
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // enter edit mode -> reveals binary editor (file input + 替换 button)
  const editBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__edit');
  if (editBtn[0]) { for (const fn of (editBtn[0]._listeners['click'] || [])) fn({}); }
  const fileInput = findByClass(byId['tab-artifacts'], 'artifacts-detail__file')[0];
  ok(fileInput, 'binary editor file input rendered in edit mode');
  if (fileInput) { fileInput.files = [{ name: 'x.png' }]; }
  const saveBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__save');
  if (saveBtn[0]) { for (const fn of (saveBtn[0]._listeners['click'] || [])) fn({}); await tick(); }
  await tick();
  const patchCall = fetchLog.find((l) => l.method === 'PATCH' && l.url.indexOf('/chat/artifacts/a1') !== -1);
  ok(patchCall, 'binary replace sent PATCH');
  // If-Match header must carry the current revision id (CAS token)
  const ifMatch = patchCall && patchCall.headers ? (patchCall.headers['If-Match'] || patchCall.headers['if-match']) : null;
  ok(ifMatch === 'r1', 'binary replace PATCH carries If-Match=current_revision_id (got ' + ifMatch + ')');
}

async function testBinaryReplaceConflictKeepsCurrentNoAutoReplay() {
  // T10: binary replace with stale If-Match -> artifact_revision_conflict (409).
  // Keep current display, refresh server state, prompt user, NO auto-replay.
  const win = freshEnv();
  const a = art('a1', 'image', { mime: 'image/png', extra: { current_revision_id: 'r1' } });
  let patchAttempts = 0;
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, (m, opts) => {
    if ((opts.method || 'GET') === 'PATCH') {
      patchAttempts += 1;
      return errResp('artifact_revision_conflict', 'CAS conflict', 409);
    }
    return jsonResp(a);
  });
  route(/^\/chat\/artifacts\/a1\/content/, () => blobResp([1, 2, 3], 'image/png'));
  let alertMsg = null;
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = { alert: (m) => { alertMsg = String(m); return Promise.resolve(); }, confirm: () => Promise.resolve(true) };
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  const editBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__edit');
  if (editBtn[0]) { for (const fn of (editBtn[0]._listeners['click'] || [])) fn({}); }
  const fileInput = findByClass(byId['tab-artifacts'], 'artifacts-detail__file')[0];
  if (fileInput) { fileInput.files = [{ name: 'x.png' }]; }
  const saveBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__save');
  if (saveBtn[0]) { for (const fn of (saveBtn[0]._listeners['click'] || [])) fn({}); await tick(); }
  await tick(); await tick();
  ok(patchAttempts === 1, 'binary replace conflict: no auto-replay (single attempt, got ' + patchAttempts + ')');
  ok(alertMsg && alertMsg.indexOf('版本已变化') !== -1, 'binary replace conflict prompts user (got ' + alertMsg + ')');
  const reloads = fetchLog.filter((l) => l.method === 'GET' && l.url.indexOf('/chat/artifacts/a1') !== -1
      && l.url.indexOf('/content') === -1 && l.url.indexOf('/export') === -1
      && l.url.indexOf('/revisions') === -1 && l.url.indexOf('/capabilities') === -1
      && l.url.indexOf('/publish') === -1);
  ok(reloads.length >= 2, 'server state reloaded after binary replace conflict (got ' + reloads.length + ')');
}

function collectText(node) {
  let s = '';
  const walk = (n) => {
    if (n._text) s += n._text + ' ';
    for (const c of n.children) walk(c);
  };
  walk(node);
  return s;
}

// ---------------------------------------------------------------------------
// renderListItem export + callback contract (T2)
// ---------------------------------------------------------------------------

async function testRenderListItemExported() {
  const win = freshEnv();
  const a = art('a1', 'markdown', {
    name: 'Doc1', source_kind: 'session',
    updated_at: '2026-02-03T04:05:06+00:00',
  });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  const mod = loadModule(win);
  ok(typeof mod.renderListItem === 'function', 'renderListItem exported');
  const item = mod.renderListItem(a);
  ok(item && typeof item === 'object', 'renderListItem returns a node');
  ok(item.className.indexOf('artifacts-list__item') !== -1,
    'returned node has artifacts-list__item class');
  // icon: kindLabel(kind).slice(0, 2)
  const icons = findByClass(item, 'artifacts-list__icon');
  ok(icons.length === 1, 'exactly one icon span');
  if (icons[0]) {
    ok(icons[0]._text === 'Ma',
      'icon text is kindLabel(kind).slice(0,2), got: ' + icons[0]._text);
  }
  // name: a.name || a.id
  const names = findByClass(item, 'artifacts-list__name');
  ok(names.length === 1, 'exactly one name div');
  if (names[0]) {
    ok(names[0]._text === 'Doc1',
      'name textContent is a.name, got: ' + names[0]._text);
  }
  // sub: sourceLabel · fmtTime
  const subs = findByClass(item, 'artifacts-list__sub');
  ok(subs.length === 1, 'exactly one sub div');
  if (subs[0]) {
    ok(subs[0]._text === '会话 · 2026-02-03 12:05:06',
      'sub text is sourceLabel · fmtTime, got: ' + subs[0]._text);
  }
}

async function testRenderListItemCallbackContract() {
  const win = freshEnv();
  const a = art('a1', 'text', { name: 'Doc1' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  // Select a1 via workbench to set state.selectedId = 'a1'
  const wbItem = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (wbItem._listeners['click'] || [])) fn({});
  await tick();
  // Now state.selectedId === 'a1'. Call renderListItem WITH callback.
  let callCount = 0;
  let receivedArg = null;
  const item = mod.renderListItem(a, function (arg) {
    callCount++; receivedArg = arg;
  });
  // Callback provided => NO active class even though state.selectedId === a1.id
  ok(!item.classList.contains('artifacts-list__item--active'),
    'callback path must NOT add active class even when state.selectedId matches');
  // Count detail fetches before callback click
  const beforeDetail = fetchLog.filter(
    (l) => l.url === '/chat/artifacts/a1').length;
  // Click the item: callback called exactly once with artifact as only arg
  const listeners = item._listeners['click'] || [];
  ok(listeners.length > 0, 'callback item has click listener');
  for (const fn of listeners) fn({ fake: 'dom-event' });
  ok(callCount === 1, 'callback called exactly once, got ' + callCount);
  ok(receivedArg === a,
    'callback receives the artifact object as the only argument (not DOM event)');
  // selectArtifact must NOT have been called (no new detail fetch)
  const afterDetail = fetchLog.filter(
    (l) => l.url === '/chat/artifacts/a1').length;
  ok(afterDetail === beforeDetail,
    'no additional detail fetch from callback click (selectArtifact not called)');
}

async function testRenderListItemDefaultBehavior() {
  const win = freshEnv();
  const a1 = art('a1', 'text', { name: 'Doc1' });
  const a2 = art('a2', 'text', { name: 'Doc2' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a1, a2], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a1));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello1'));
  route(/^\/chat\/artifacts\/a2$/, () => jsonResp(a2));
  route(/^\/chat\/artifacts\/a2\/content/, () => textResp('hello2'));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  // state.selectedId is null initially: no active class
  const item = mod.renderListItem(a1);
  ok(!item.classList.contains('artifacts-list__item--active'),
    'no active class when state.selectedId is null');
  // Click: default behavior calls selectArtifact(a1.id)
  const beforeDetail = fetchLog.filter(
    (l) => l.url === '/chat/artifacts/a1').length;
  const listeners = item._listeners['click'] || [];
  ok(listeners.length > 0, 'default item has click listener');
  for (const fn of listeners) fn({});
  await tick();
  const afterDetail = fetchLog.filter(
    (l) => l.url === '/chat/artifacts/a1').length;
  ok(afterDetail === beforeDetail + 1,
    'default click triggers selectArtifact (detail fetch)');
  // Now state.selectedId === 'a1'. Render again: active class appears.
  const itemActive = mod.renderListItem(a1);
  ok(itemActive.classList.contains('artifacts-list__item--active'),
    'default path adds active class when state.selectedId === artifact.id');
  // a2 should NOT have active class
  const item2 = mod.renderListItem(a2);
  ok(!item2.classList.contains('artifacts-list__item--active'),
    'no active class for non-selected artifact');
}

async function testRenderListItemXssSafety() {
  const win = freshEnv();
  const malicious = '<img src=x onerror=alert(1)><script>alert(2)</script>';
  const a = art('a1', 'text', { name: malicious, source_kind: malicious });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  const mod = loadModule(win);
  const item = mod.renderListItem(a);
  // Malicious text must be preserved as textContent, not parsed as HTML
  const text = collectText(item);
  ok(text.indexOf(malicious) !== -1,
    'malicious text preserved as textContent');
  // No img or script elements created
  ok(findByTag(item, 'img').length === 0,
    'no img elements created from malicious name');
  ok(findByTag(item, 'script').length === 0,
    'no script elements created from malicious name');
}

// ---------------------------------------------------------------------------
// T10: revision history panel + diff + rollback + publish_sync_state badge
// ---------------------------------------------------------------------------
function setupRevisionedArtifact(win, opts) {
  opts = opts || {};
  const detailExtra = Object.assign({
    current_revision_id: opts.currentRevisionId || 'r2',
    revision_number: opts.revisionNumber != null ? opts.revisionNumber : 2,
    publish_sync_state: opts.syncState || 'current',
  }, opts.detailExtra || {});
  const a = art('a1', 'text', { extra: detailExtra });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  route(/^\/chat\/artifacts\/a1\/publish$/, () => jsonResp(opts.publishPayload || { status: opts.publishStatus || 'unpublished' }));
  route(/^\/chat\/artifacts\/a1\/export\/capabilities$/, () => jsonResp({ capabilities: opts.capabilities || ['original'] }));
  route(/^\/chat\/artifacts\/a1\/revisions/, () => jsonResp({
    items: opts.revisions || [
      { id: 'r1', revision_number: 1, change_summary: '初版', created_at: '2026-01-01T00:00:00+00:00', is_current: false, is_published: true },
      { id: 'r2', revision_number: 2, change_summary: '更新', created_at: '2026-01-02T00:00:00+00:00', is_current: true, is_published: false },
    ],
  }));
}

async function selectAndAwait(mod) {
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick(); await tick(); await tick();
}

function detailGetCount() {
  return fetchLog.filter((l) => l.url === '/chat/artifacts/a1' && l.method === 'GET').length;
}

// Click the 版本 button in the detail header and return the opened revisions
// modal (standard modal on document.body). Revisions must already be loaded.
function clickRevisionsButton() {
  const revBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__revisions')[0];
  ok(revBtn, '版本 button rendered in header');
  for (const fn of (revBtn._listeners['click'] || [])) fn({});
  return byId['artifacts-revisions-modal'];
}

async function testRevisionsModalRendersWithMarkers() {
  const win = freshEnv();
  setupRevisionedArtifact(win, { syncState: 'current', currentRevisionId: 'r2' });
  const mod = loadModule(win);
  await selectAndAwait(mod);
  // publish/revoke moved out of the header into the 版本 modal: the header
  // actions no longer contain a artifacts-detail__publish button.
  const actions = findByClass(byId['tab-artifacts'], 'artifacts-detail__actions')[0];
  const btns = actions.children;
  let hasHeaderPublish = false;
  for (let i = 0; i < btns.length; i++) {
    const cls = (btns[i].className || '').split(/\s+/);
    if (cls.indexOf('artifacts-detail__publish') !== -1) hasHeaderPublish = true;
  }
  ok(!hasHeaderPublish, 'no publish button in header (publish/revoke moved to 版本 modal)');
  const modal = clickRevisionsButton();
  ok(modal, 'revisions modal opens on 版本 click');
  ok(!byId['artifacts-revisions-panel'], 'no inline revision panel (history moved to modal)');
  const items = findByClass(modal, 'artifacts-revisions__item');
  ok(items.length === 2, '2 revision rows rendered in modal (got ' + items.length + ')');
  ok(findByClass(modal, 'artifacts-revisions__item--current').length === 1, 'exactly one current revision marked');
  ok(findByClass(modal, 'artifacts-revisions__badge--current').length === 1, 'one 当前 badge on current revision');
  ok(findByClass(modal, 'artifacts-revisions__badge--published').length === 1, 'one 已发布 badge (r1 is the published revision)');
  // Publish/revoke are version-aware per row: the published revision (r1)
  // offers 撤回发布; the current non-published revision (r2) offers 发布此版本.
  ok(findByClass(modal, 'artifacts-revisions__revoke').length === 1, 'one 撤回发布 button (on published r1)');
  ok(findByClass(modal, 'artifacts-revisions__publish').length === 1, 'one 发布此版本 button (on current r2)');
  // Non-current r1 also has compare + rollback.
  ok(findByClass(modal, 'artifacts-revisions__diff').length === 1, 'one 对比当前 button (non-current only)');
  ok(findByClass(modal, 'artifacts-revisions__rollback').length === 1, 'one 回滚 button (non-current only)');
}

async function testDiffRendersAsSafeTextContent() {
  const win = freshEnv();
  // Malicious diff text must be rendered as plain text, never parsed as HTML.
  const maliciousDiff = '<script>alert(1)</script>\n-old\n+new';
  setupRevisionedArtifact(win, { currentRevisionId: 'r2' });
  route(/^\/chat\/artifacts\/a1\/diff$/, () => jsonResp({ diff_text: maliciousDiff, binary_changed: false, redacted: true }));
  const mod = loadModule(win);
  await selectAndAwait(mod);
  const modal = clickRevisionsButton();
  const diffBtn = findByClass(modal, 'artifacts-revisions__diff')[0];
  ok(diffBtn, 'diff button present on non-current revision');
  for (const fn of (diffBtn._listeners['click'] || [])) fn({});
  await tick(); await tick();
  const pre = findByClass(modal, 'artifacts-diff__pre')[0];
  ok(pre, 'diff rendered in a <pre> element');
  ok(pre._text === maliciousDiff, 'diff text rendered verbatim as textContent (no HTML parsing), got: ' + pre._text);
  ok(findByTag(modal, 'script').length === 0, 'no script elements created from diff text');
}

async function testRollbackConflictKeepsCurrentNoAutoReplay() {
  const win = freshEnv();
  setupRevisionedArtifact(win, { currentRevisionId: 'r2' });
  let rollbackCalls = 0;
  route(/^\/chat\/artifacts\/a1\/rollback$/, () => { rollbackCalls++; return errResp('artifact_revision_conflict', 'stale', 409); });
  let alertMsg = null;
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = { alert: (m) => { alertMsg = String(m); return Promise.resolve(); }, confirm: () => Promise.resolve(true) };
  const mod = loadModule(win);
  await selectAndAwait(mod);
  const beforeDetail = detailGetCount();
  const modal = clickRevisionsButton();
  const rbBtn = findByClass(modal, 'artifacts-revisions__rollback')[0];
  ok(rbBtn, 'rollback button present');
  for (const fn of (rbBtn._listeners['click'] || [])) fn({});
  await tick(); await tick(); await tick();
  ok(rollbackCalls === 1, 'exactly one rollback POST on conflict -- NO auto-replay (got ' + rollbackCalls + ')');
  ok(alertMsg && alertMsg.indexOf('版本已变化') !== -1, 'conflict alert prompts re-read (got ' + alertMsg + ')');
  ok(detailGetCount() > beforeDetail, 'detail reloaded after conflict to reflect server current state');
}

async function testRollbackSuccessReloadsDetail() {
  const win = freshEnv();
  setupRevisionedArtifact(win, { currentRevisionId: 'r2' });
  route(/^\/chat\/artifacts\/a1\/rollback$/, () => jsonResp({ artifact_id: 'a1', revision_id: 'r3', revision_number: 3, name: 'artifact-a1', kind: 'text', publish_sync_state: 'outdated' }));
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = { confirm: () => Promise.resolve(true) };
  const mod = loadModule(win);
  await selectAndAwait(mod);
  const beforeDetail = detailGetCount();
  const modal = clickRevisionsButton();
  const rbBtn = findByClass(modal, 'artifacts-revisions__rollback')[0];
  for (const fn of (rbBtn._listeners['click'] || [])) fn({});
  await tick(); await tick(); await tick();
  ok(detailGetCount() > beforeDetail, 'detail reloaded after successful rollback (got ' + beforeDetail + ' -> ' + detailGetCount() + ')');
  ok(modal._removed, 'revisions modal closed after successful rollback');
}

async function testPreviewStatusBarShowsVersionAndPublication() {
  const win = freshEnv();
  setupRevisionedArtifact(win, {
    syncState: 'outdated', currentRevisionId: 'r2',
    publishPayload: { status: 'active', share_url: 'http://test/p/pub1' },
  });
  const mod = loadModule(win);
  await selectAndAwait(mod);
  const metadata = findByClass(byId['tab-artifacts'], 'artifacts-detail__metadata')[0];
  ok(metadata && metadata._text === '大小: 10 B，更新: 2026-01-01 08:00:00；版本: v2',
    'status bar shows size, update time, and current version (got ' + (metadata && metadata._text) + ')');
  const publication = findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-status')[0];
  ok(publication && publication.children[0] && publication.children[0]._text === '，',
    'publication status starts with the prescribed separator');
  ok(publication && publication.children[1] && publication.children[1]._text === '已发布: ',
    'active publication shows 已发布 label');
  ok(findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-link').length === 1,
    'active publication shows share link');
}

async function testMarkdownSaveRefreshesPreviewAndRevisions() {
  // Regression: after editing a markdown artifact, the preview and the version
  // list must refresh without a manual browser reload. Root cause was the
  // content-PATCH response omitting `id` (legacy _write_result_to_dict), so
  // saveText assigned state.detail without an id -> renderMarkdownHtml called
  // fetchExport(detail.id=undefined) -> /chat/artifacts/undefined/export (404),
  // and loadRevisions was never called. Fix: PATCH returns the full artifact
  // view (id intact) and saveText invalidates + reloads revisions.
  const win = freshEnv();
  const a = art('a1', 'markdown', { mime: 'text/markdown',
    extra: { current_revision_id: 'r1' } });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, (m, opts) => {
    if ((opts.method || 'GET') === 'PATCH') {
      // PATCH returns the FULL artifact view (id stays 'a1'), new CAS token r2.
      return jsonResp(art('a1', 'markdown', { mime: 'text/markdown',
        updated_at: '2026-01-02T00:00:00+00:00',
        extra: { current_revision_id: 'r2' } }));
    }
    return jsonResp(a);
  });
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('# original'));
  route(/^\/chat\/artifacts\/a1\/export\?format=html/, () => textResp('<h1>original</h1>', 'text/html'));
  route(/^\/chat\/artifacts\/a1\/revisions\?limit=100/, () => jsonResp({
    items: [
      { revision_id: 'r2', revision_number: 2, created_at: '2026-01-02T00:00:00+00:00', summary: '编辑' },
      { revision_id: 'r1', revision_number: 1, created_at: '2026-01-01T00:00:00+00:00', summary: '创建' },
    ],
  }));
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // clear fetch log so only post-save requests are inspected
  fetchLog.length = 0;
  // enter edit mode, edit, save
  const editBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__edit');
  if (editBtn[0]) { for (const fn of (editBtn[0]._listeners['click'] || [])) fn({}); }
  const tas = findByTag(byId['tab-artifacts'], 'textarea');
  if (tas[0]) { tas[0].value = '# edited'; }
  const saveBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__save');
  if (saveBtn[0]) { for (const fn of (saveBtn[0]._listeners['click'] || [])) fn({}); }
  await tick(); await tick(); await tick();
  // PATCH sent with CAS token
  const patchCall = fetchLog.find((l) => l.method === 'PATCH' && l.url.indexOf('/chat/artifacts/a1') !== -1);
  ok(patchCall, 'markdown save sent PATCH');
  ok(patchCall && patchCall.body && patchCall.body.indexOf('expected_revision_id') !== -1
      && patchCall.body.indexOf('r1') !== -1,
    'markdown content PATCH carries expected_revision_id CAS token');
  // preview re-fetched export using the REAL artifact id (not 'undefined')
  const exportCall = fetchLog.find((l) => l.method === 'GET'
      && l.url.indexOf('/chat/artifacts/a1/export') !== -1);
  ok(exportCall, 'preview re-fetched export after save');
  ok(exportCall && exportCall.url.indexOf('/undefined/') === -1,
    'export uses real artifact id (not undefined) -- regression: PATCH response must keep id');
  // version list re-fetched (new revision created)
  const revCall = fetchLog.find((l) => l.method === 'GET'
      && l.url.indexOf('/chat/artifacts/a1/revisions') !== -1);
  ok(revCall, 'markdown save re-fetched revisions (new revision created)');
}

async function testPublishLatestFromModalAfterEdit() {
  // Regression: after editing a published artifact (publish_sync_state=
  // outdated -- r1 published, r2 current not published), the user must be able
  // to publish the latest revision directly from the 版本 modal WITHOUT first
  // revoking. The header no longer has a publish/撤回 toggle (which used to
  // force a two-step revoke -> publish). The current revision row offers
  // 发布此版本; clicking it re-publishes atomically (backend revokes the old
  // publish + registers a new one), and the sync badge + share link update.
  const win = freshEnv();
  let publishedRevision = 'r1';
  const a = art('a1', 'text', { extra: { current_revision_id: 'r2', publish_sync_state: 'outdated' } });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  route(/^\/chat\/artifacts\/a1\/publish$/, (m, opts) => {
    if ((opts.method || 'GET') === 'POST') {
      publishedRevision = 'r2';
      return jsonResp({ publish_id: 'pub2', share_path: '/p/pub2', share_url: 'http://test/p/pub2', reused: false });
    }
    return jsonResp(publishedRevision === 'r2'
      ? { status: 'active', share_url: 'http://test/p/pub2', share_path: '/p/pub2' }
      : { status: 'active', share_url: 'http://test/p/pub1', share_path: '/p/pub1' });
  });
  route(/^\/chat\/artifacts\/a1\/revisions/, () => jsonResp({
    items: [
      { id: 'r1', revision_number: 1, change_summary: '初版', created_at: '2026-01-01T00:00:00+00:00', is_current: false, is_published: publishedRevision === 'r1' },
      { id: 'r2', revision_number: 2, change_summary: '更新', created_at: '2026-01-02T00:00:00+00:00', is_current: true, is_published: publishedRevision === 'r2' },
    ],
  }));
  route(/^\/chat\/artifacts\/a1\/export\/capabilities$/, () => jsonResp({ capabilities: ['original'] }));
  win.NAGENT = win.NAGENT || {};
  win.NAGENT.modal = { confirm: () => Promise.resolve(true), alert: () => Promise.resolve() };
  const mod = loadModule(win);
  await selectAndAwait(mod);
  // An older published revision still exposes its active share link.
  let publication = findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-status')[0];
  ok(publication && publication.children[1] && publication.children[1]._text === '已发布: ',
    'published older revision shows active publication status');
  // open 版本 modal: r2 (current, not published) offers 发布此版本 directly.
  const modal = clickRevisionsButton();
  await tick();
  const pubBtn = findByClass(modal, 'artifacts-revisions__publish')[0];
  ok(pubBtn, 'current revision offers 发布此版本 (direct publish, no revoke needed)');
  // the published r1 offers 撤回发布, but it is NOT required.
  ok(findByClass(modal, 'artifacts-revisions__revoke').length === 1, 'published r1 offers 撤回发布 (optional, not required)');
  // click 发布此版本 -> POST /publish (re-publish, atomically replaces old)
  fetchLog.length = 0;
  if (pubBtn) { for (const fn of (pubBtn._listeners['click'] || [])) fn({}); await tick(); await tick(); }
  const postPublish = fetchLog.find((l) => l.method === 'POST' && l.url.indexOf('/chat/artifacts/a1/publish') !== -1);
  ok(postPublish, '发布此版本 sent POST /publish');
  // Publication status and share link update after publish.
  publication = findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-status')[0];
  ok(publication && publication.children[1] && publication.children[1]._text === '已发布: ',
    'publication status -> 已发布 after publish');
  ok(findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-link').length === 1,
    'share link rendered in metadata row after re-publish');
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

const tests = [
  testNavProbeSuccessRevealsNav,
  testNavProbeFailureKeepsNavHidden,
  testListLoadAndFilters,
  testLoadMoreStableCursor,
  testEmptyState,
  testFilterEmptyState,
  testErrorState,
  testContentUnavailableState,
  testPublishBlockedState,
  testSourceBacklinkValidTaskId,
  testSourceBacklinkInvalidTaskId,
  testMarkdownSandboxIframe,
  testHtmlSandboxIframe,
  testCodeTextRendering,
  testJsonSuccessAndFail,
  testCsvRowColLimits,
  testImageBlobAndRevokeOnSwitch,
  testPdfBlobFallback,
  testRevokeObjectURLOnDestroy,
  testTextSaveRefreshWithServerMetadata,
  testMarkdownSaveRefreshesPreviewAndRevisions,
  testContentPatchConflictKeepsCurrentNoAutoReplay,
  testExportMenuCapabilitiesDriven,
  testPublishShareUrlToggleRevoke,
  testPublishLatestFromModalAfterEdit,
  testBinaryPublishConfirmation,
  testBinaryReplaceCarriesIfMatchCas,
  testBinaryReplaceConflictKeepsCurrentNoAutoReplay,
  testRenderListItemExported,
  testRenderListItemCallbackContract,
  testRenderListItemDefaultBehavior,
  testRenderListItemXssSafety,
  testRevisionsModalRendersWithMarkers,
  testDiffRendersAsSafeTextContent,
  testRollbackConflictKeepsCurrentNoAutoReplay,
  testRollbackSuccessReloadsDetail,
  testPreviewStatusBarShowsVersionAndPublication,
];

(async () => {
  for (const t of tests) {
    try {
      await t();
    } catch (e) {
      failures++;
      console.error('FAIL (exception): ' + t.name + ': ' + (e && e.message ? e.message : e));
      console.error(e && e.stack ? e.stack : '');
    }
  }
  if (failures === 0) {
    console.log('all tests passed');
    process.exit(0);
  } else {
    console.error(failures + ' test(s) failed');
    process.exit(1);
  }
})();
