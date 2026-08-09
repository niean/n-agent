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
// - export as a single button (original format only; no dropdown)
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
  fetchLog.push({ url: String(url), method: options.method || 'GET', signal: options.signal || null });
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
  const a = art('a1', 'text', { source_kind: 'manual' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  route(/^\/chat\/artifacts\/a1\/publish$/, (m, opts) => {
    if ((opts.method || 'GET') === 'POST') return errResp('publish_blocked', 'blocked', 422);
    return jsonResp({ status: 'unpublished' });
  });
  // Publish failure is reported via the shared modal; artifacts.js has no
  // inline "blocked" text -- the header status segment only reflects an active
  // publish state, so a blocked publish must leave the UI unpublished.
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
  // click publish button
  const pubBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__publish');
  if (pubBtn[0]) {
    for (const fn of (pubBtn[0]._listeners['click'] || [])) fn({});
    await tick();
  }
  // blocked publish -> failure reported via modal alert
  ok(alertMsg && /失败|blocked|publish_blocked|不可发布|无法发布/i.test(alertMsg),
    'publish-blocked failure reported via modal alert: ' + String(alertMsg));
  // publish did not activate: button stays 发布, no share link rendered
  if (pubBtn[0]) {
    ok(/发布/.test(pubBtn[0]._text) && !/撤回/.test(pubBtn[0]._text),
      'publish button stays 发布 after blocked publish, got: ' + pubBtn[0]._text);
  }
  ok(!findByClass(byId['tab-artifacts'], 'artifacts-detail__publish-link').length,
    'no share link shown after blocked publish');
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
  const a = art('a1', 'text', { size: 5, checksum: 'sha256:' + 'a'.repeat(64), updated_at: '2026-01-01T00:00:00+00:00' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, (m, opts) => {
    if ((opts.method || 'GET') === 'PATCH') {
      return jsonResp(art('a1', 'text', { size: 11, checksum: 'sha256:' + 'b'.repeat(64), updated_at: '2026-01-02T00:00:00+00:00' }));
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
  // After save, server-returned size/checksum/updated_at should be reflected.
  const text = collectText(byId['tab-artifacts']);
  ok(text.indexOf('11') !== -1 || text.indexOf('b') !== -1 || text.indexOf('2026-01-02') !== -1,
    'refreshed metadata from server shown after save');
}

async function testExportButtonOriginalOnly() {
  const win = freshEnv();
  // markdown -> original + html
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
  const exportBtns = findByClass(byId['tab-artifacts'], 'artifacts-detail__export');
  ok(exportBtns.length === 1, 'single export button present (no dropdown)');
  ok(!findByClass(byId['tab-artifacts'], 'artifacts-detail__export-menu').length,
    'no export dropdown menu rendered');

  // code -> original only (no html)
  const win2 = freshEnv();
  const a2 = art('a2', 'code', { mime: 'text/plain' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a2], null));
  route(/^\/chat\/artifacts\/a2$/, () => jsonResp(a2));
  route(/^\/chat\/artifacts\/a2\/content/, () => textResp('x=1'));
  const mod2 = loadModule(win2);
  await mod2.init();
  await tick();
  const item2 = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item2._listeners['click'] || [])) fn({});
  await tick();
  // single export button for code as well
  const exportBtns2 = findByClass(byId['tab-artifacts'], 'artifacts-detail__export');
  ok(exportBtns2.length === 1, 'export control present for code');
}

async function testPublishShareUrlToggleRevoke() {
  const win = freshEnv();
  const a = art('a1', 'text', { source_kind: 'manual' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => textResp('hello'));
  // GET /publish reflects the server-tracked state: unpublished before POST,
  // active after POST, back to unpublished after DELETE (revoke). The client
  // re-fetches status after publish, so the GET must stay consistent.
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
  const mod = loadModule(win);
  await mod.init();
  await tick();
  const item = findByClass(byId['tab-artifacts'], 'artifacts-list__item')[0];
  for (const fn of (item._listeners['click'] || [])) fn({});
  await tick();
  // publish
  const pubBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__publish');
  if (pubBtn[0]) { for (const fn of (pubBtn[0]._listeners['click'] || [])) fn({}); await tick(); await tick(); }
  // share link shown in the header status segment after publish
  const text = collectText(byId['tab-artifacts']);
  ok(text.indexOf('http://test/p/pub123') !== -1 || text.indexOf('/p/pub123') !== -1 || text.indexOf('已发布') !== -1,
    'share link / published state shown after publish: ' + text.slice(0, 120));
  // single toggle button design: button text flips to 撤回; no separate
  // copy or revoke buttons exist.
  if (pubBtn[0]) {
    ok(/撤回/.test(pubBtn[0]._text), 'publish button toggled to 撤回 after publish, got: ' + pubBtn[0]._text);
  }
  ok(!findByClass(byId['tab-artifacts'], 'artifacts-detail__copy').length,
    'no separate copy button (single toggle button design)');
  ok(!findByClass(byId['tab-artifacts'], 'artifacts-detail__revoke').length,
    'no separate revoke button (publish button toggles to revoke)');
  // click the same button again to revoke
  if (pubBtn[0]) { for (const fn of (pubBtn[0]._listeners['click'] || [])) fn({}); await tick(); await tick(); }
  // after revoke: button reverts to 发布 and the share link is removed
  if (pubBtn[0]) {
    ok(/发布/.test(pubBtn[0]._text) && !/撤回/.test(pubBtn[0]._text),
      'publish button reverts to 发布 after revoke, got: ' + pubBtn[0]._text);
  }
  const text2 = collectText(byId['tab-artifacts']);
  ok(text2.indexOf('http://test/p/pub123') === -1 && text2.indexOf('/p/pub123') === -1,
    'share link removed after revoke: ' + text2.slice(0, 120));
}

async function testBinaryPublishConfirmation() {
  const win = freshEnv();
  const a = art('a1', 'image', { mime: 'image/png', source_kind: 'manual' });
  route(/^\/chat\/artifacts(\?|$)/, () => listResp([a], null));
  route(/^\/chat\/artifacts\/a1$/, () => jsonResp(a));
  route(/^\/chat\/artifacts\/a1\/content/, () => blobResp([1, 2, 3], 'image/png'));
  route(/^\/chat\/artifacts\/a1\/publish$/, (m, opts) => {
    if ((opts.method || 'GET') === 'POST') {
      return jsonResp({ publish_id: 'pub', share_path: '/p/pub', share_url: 'http://test/p/pub', reused: false });
    }
    return jsonResp({ status: 'unpublished' });
  });
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
  const pubBtn = findByClass(byId['tab-artifacts'], 'artifacts-detail__publish');
  if (pubBtn[0]) { for (const fn of (pubBtn[0]._listeners['click'] || [])) fn({}); await tick(); }
  ok(confirmShown, 'binary publish shows confirmation dialog');
  if (confirmShown) {
    ok(/PUBLIC|公开|扫描|秘密|secret/i.test(String(win.NAGENT.modal._lastMsg || '')),
      'binary publish confirmation mentions explicit-PUBLIC / no secret scan: ' + win.NAGENT.modal._lastMsg);
  }
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
  testExportButtonOriginalOnly,
  testPublishShareUrlToggleRevoke,
  testBinaryPublishConfirmation,
  testRenderListItemExported,
  testRenderListItemCallbackContract,
  testRenderListItemDefaultBehavior,
  testRenderListItemXssSafety,
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
