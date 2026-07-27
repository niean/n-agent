'use strict';
// Minimal, dependency-free behavior harness for browser.js (T16).
// Run with: node tests/interfaces/browser_frontend_harness.js
// Exits 0 on success, 1 on any failure. Loaded by test_browser_frontend.py
// via subprocess; skipped when Node is unavailable.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const BROWSER_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'browser.js');
const code = fs.readFileSync(BROWSER_JS, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// ---- DOM mock ----
const byId = {};
function makeNode(tag) {
  const n = {
    tag: tag,
    className: '',
    _text: '',
    children: [],
    _listeners: {},
    dataset: {},
    style: { _p: {}, setProperty(k, v) { this._p[k] = String(v); } },
    hidden: false,
    type: '',
    value: '',
    checked: false,
    disabled: false,
    required: false,
    placeholder: '',
    src: '',
    alt: '',
    _id: null,
    _removed: false,
    set textContent(v) { this._text = (v === null || v === undefined) ? '' : String(v); },
    get textContent() { return this._text; },
    set id(v) { this._id = v; if (v != null) byId[v] = this; },
    get id() { return this._id; },
    appendChild(c) { this.children.push(c); return c; },
    append() { for (let i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },
    replaceChildren() { this.children = []; },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    setAttribute(k, v) { if (k === 'sandbox') this._sandbox = v; },
    remove() { this._removed = true; },
    focus() { document.activeElement = this; },
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, force) { if (force === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); } else { force ? this._s.add(c) : this._s.delete(c); } },
      contains(c) { return this._s.has(c); },
    },
  };
  // classList needs to be per-node; reset the shared Set
  n.classList._s = new Set();
  return n;
}

const document = {
  createElement: makeNode,
  getElementById: (id) => byId[id] || null,
  body: makeNode('body'),
  activeElement: null,
  _listeners: {},
  addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
  removeEventListener(ev, fn) {
    if (this._listeners[ev]) this._listeners[ev] = this._listeners[ev].filter((f) => f !== fn);
  },
};

// ---- localStorage mock ----
const lsStore = {};
const lsSetCalls = [];
const localStorage = {
  getItem(k) { return Object.prototype.hasOwnProperty.call(lsStore, k) ? lsStore[k] : null; },
  setItem(k, v) { lsStore[k] = String(v); lsSetCalls.push({ key: k, value: String(v) }); },
  removeItem(k) { delete lsStore[k]; },
};

// ---- setInterval mock ----
let intervalId = 0;
let activeIntervals = [];
const setInterval = (fn, ms) => {
  const id = ++intervalId;
  activeIntervals.push({ id, fn, ms });
  return id;
};
const clearInterval = (id) => {
  activeIntervals = activeIntervals.filter((i) => i.id !== id);
};

function freshEnv(api, search) {
  for (const k of Object.keys(byId)) delete byId[k];
  activeIntervals = [];
  for (const k of Object.keys(lsStore)) delete lsStore[k];
  lsSetCalls.length = 0;

  // Pre-register the tab container (mirrors index.html #tab-browser)
  byId['tab-browser'] = makeNode('div');

  const ui = {
    el: (tag, className) => { const n = makeNode(tag); if (className) n.className = className; return n; },
    clear: (node) => { if (node) node.replaceChildren(); },
    byId: (id) => byId[id] || null,
    renderEmpty: (parent, message) => { if (parent) { parent.replaceChildren(); const d = makeNode('div'); d.className = 'empty-state'; d.textContent = message || ''; parent.appendChild(d); } },
    renderLoading: (parent, message) => { if (parent) { parent.replaceChildren(); const d = makeNode('div'); d.className = 'loading-state'; d.textContent = message || ''; parent.appendChild(d); } },
    renderError: (parent, message) => { if (parent) { parent.replaceChildren(); const d = makeNode('div'); d.className = 'error-state'; d.textContent = message || ''; parent.appendChild(d); } },
  };
  const modal = {
    confirm: () => Promise.resolve(true),
    alert: () => Promise.resolve(),
  };
  const win = { NAGENT: { api: api, ui: ui, modal: modal }, location: { search: search === undefined ? '?nagent=nagent-1' : search, pathname: '/browser' } };
  const ctx = {
    NAGENT: win.NAGENT,
    document: document,
    console: console,
    window: win,
    localStorage: localStorage,
    setInterval: setInterval,
    clearInterval: clearInterval,
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  return ctx;
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }
async function tickN(n) { for (let i = 0; i < n; i++) await tick(); }

function allTexts(node, out) {
  out = out || [];
  if (node._text) out.push(node._text);
  (node.children || []).forEach((c) => allTexts(c, out));
  return out;
}

function findButtons(node, out) {
  out = out || [];
  if (node.tag === 'button') out.push(node);
  (node.children || []).forEach((c) => findButtons(c, out));
  return out;
}

function findIframes(node, out) {
  out = out || [];
  if (node.tag === 'iframe') out.push(node);
  (node.children || []).forEach((c) => findIframes(c, out));
  return out;
}

function findByClass(node, className, out) {
  out = out || [];
  if (String(node.className || '').split(/\s+/).indexOf(className) !== -1) out.push(node);
  (node.children || []).forEach((c) => findByClass(c, className, out));
  return out;
}

// ---- test data ----

function makeNSession(id) {
  return { id: id, title: 'Session ' + id };
}

function makeBrowserSession(id, nagentId, status, backend) {
  return {
    id: id,
    n_agent_session_id: nagentId,
    backend_type: backend || 'container',
    status: status || 'active',
    profile_ref: 'bp-test',
    document_revision: 0,
    created_at: '2026-07-27T10:00:00+00:00',
    updated_at: '2026-07-27T10:05:00+00:00',
    closed_at: null,
  };
}

function makeChallenges(status) {
  // Mirror _valid_write_ops from browser_routes.py
  const c = {};
  if (status === 'pending_authorization') { c.close = 'tok-close'; c.host_grant = 'tok-host'; }
  else if (status === 'active') { c.pause = 'tok-pause'; c.takeover = 'tok-takeover'; c.close = 'tok-close'; c.host_grant = 'tok-host'; c.revoke_host = 'tok-revoke'; }
  else if (status === 'paused') { c.resume = 'tok-resume'; c.takeover = 'tok-takeover'; c.close = 'tok-close'; }
  else if (status === 'takeover') { c.release = 'tok-release'; c.close = 'tok-close'; }
  else if (status === 'degraded') { c.close = 'tok-close'; }
  return c;
}

function makeSessionDetail(id, nagentId, status, backend) {
  return {
    id: id,
    n_agent_session_id: nagentId,
    backend_type: backend || 'container',
    status: status,
    profile_ref: 'bp-test',
    document_revision: 0,
    write_challenges: makeChallenges(status),
    created_at: '2026-07-27T10:00:00+00:00',
    updated_at: '2026-07-27T10:05:00+00:00',
  };
}

function makeActions() {
  return [
    { id: 'act-1', action_type: 'navigate', status: 'success', safe_url: 'https://example.com/page?secret=1', title: 'Example Page', duration_ms: 500, created_at: '2026-07-27T10:04:00+00:00', error_code: null },
    { id: 'act-2', action_type: 'click', status: 'success', safe_url: 'https://example.com/page', title: 'Example Page', duration_ms: 100, created_at: '2026-07-27T10:05:00+00:00', error_code: null },
  ];
}

function makeTakeoverView(url) {
  return {
    url: url || 'http://browser:9222/vnc/websockify?session=bsess-1&cap=secret-token',
    expires_at: '2026-07-27T10:15:00+00:00',
    message: null,
  };
}

// ---- tests ----

async function testSourceSafety() {
  // No innerHTML assignment / insertAdjacentHTML call / document.write call in source
  ok(code.indexOf('innerHTML =') === -1, 'source: no innerHTML assignment');
  ok(code.indexOf('innerHTML=') === -1, 'source: no innerHTML assignment (no space)');
  ok(code.indexOf('.insertAdjacentHTML(') === -1, 'source: no insertAdjacentHTML call');
  ok(code.indexOf('document.write(') === -1, 'source: no document.write call');
  ok(code.indexOf('.outerHTML') === -1, 'source: no outerHTML');
  ok(code.indexOf('onclick=') === -1, 'source: no inline onclick');
  // textContent must be used for safe rendering
  ok(code.indexOf('textContent') !== -1, 'source: uses textContent');
  // Only poll_ms stored in localStorage
  ok(code.indexOf('POLL_KEY') !== -1, 'source: uses POLL_KEY for localStorage');
}

async function testInitRendersDetailWithoutSessionSelector() {
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1'), makeNSession('nagent-2')]),
    browser: {
      listSessions: (nid) => {
        if (nid === 'nagent-1') return Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] });
        return Promise.resolve({ sessions: [] });
      },
      getSession: (sid, nid) => Promise.resolve(makeSessionDetail(sid, nid, 'active')),
      listActions: (sid, nid) => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.browser.init();
  await tickN(8);

  ok(!byId['browser-sessions-body'], 'browser session selector is not rendered');
  ok(!!byId['browser-main-body'], 'detail main view rendered');
}

async function testExecutorEntryRendersHistoryAndDetailNavigates() {
  let actionListCalls = 0;
  const browserSession = makeBrowserSession('bsess-1', 'nagent-1', 'active');
  browserSession.action_count = 1234;
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [browserSession] }),
      getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'active')),
      listActions: () => { actionListCalls++; return Promise.resolve({ actions: makeActions(), next_cursor: null }); },
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  const ctx = freshEnv(api, '');
  ctx.NAGENT.browser.init();
  await tickN(8);

  const history = findByClass(byId['tab-browser'], 'browser-history');
  ok(history.length === 1, 'executor entry renders browser execution history');
  const texts = allTexts(byId['tab-browser']).join('|');
  ok(texts.indexOf('执行历史') !== -1, 'history title rendered');
  ok(texts.indexOf('Session') !== -1 && texts.indexOf('对话 Session') === -1
    && texts.indexOf('浏览器 Session') !== -1 && texts.indexOf('操作次数') !== -1,
    'history groups rows by conversation and browser session');
  ok(texts.indexOf('nagent-1') !== -1 && texts.indexOf('bsess-1') !== -1,
    'history renders the conversation and browser session IDs');
  ok(texts.indexOf('navigate') === -1 && texts.indexOf('click') === -1,
    'history does not render operation details');
  ok(actionListCalls === 0, 'history does not load operation details');
  ok(texts.indexOf('1,234') !== -1, 'history formats action count with digit grouping');
  ok(texts.indexOf('详情') !== -1, 'history has detail action');
  const detail = findButtons(byId['tab-browser']).find((b) => b.textContent === '详情');
  ok(!!detail, 'history detail button found');
  if (detail) {
    (detail._listeners.click || []).forEach((fn) => fn());
    ok(ctx.window.location.href === '/browser/session?nagent=nagent-1&browser_session_id=bsess-1',
      'detail opens the original browser view for the selected browser session');
  }
}

async function testSelectRendersMainView() {
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] }),
      getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'active')),
      listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.browser.init();
  await tickN(8);

  const mainBody = byId['browser-main-body'];
  ok(!!mainBody, 'main body rendered');
  // status badge
  const texts = allTexts(mainBody).join('|');
  ok(texts.indexOf('运行中') !== -1, 'main shows active status');
  // screenshot img
  const imgs = mainBody.children.filter((c) => c.className === 'browser-screenshot-wrap');
  ok(imgs.length === 1, 'screenshot wrap rendered');
  if (imgs.length > 0) {
    const img = imgs[0].children.find((c) => c.tag === 'img');
    ok(!!img, 'img element present');
    if (img) {
      ok(img.src.indexOf('/chat/browser/sessions/bsess-1/screenshot') !== -1, 'img src points to screenshot endpoint');
      ok(img.src.indexOf('n_agent_session_id=nagent-1') !== -1, 'img src includes n_agent_session_id');
    }
  }
  // safe URL (query stripped) and title
  ok(texts.indexOf('https://example.com/page') !== -1, 'safe URL rendered (query stripped)');
  ok(texts.indexOf('Example Page') !== -1, 'title rendered');
  // action history (side panel)
  const sideBody = byId['browser-side-body'];
  ok(!!sideBody, 'side body rendered');
  const sideTexts = allTexts(sideBody).join('|');
  ok(sideTexts.indexOf('navigate') !== -1, 'action history shows action type');
  ok(sideTexts.indexOf('click') !== -1, 'action history shows latest action');
  ok(findByClass(sideBody, 'browser-actions-table').length === 1,
    'action history uses the standard document table');
}

async function testControlMatrixByStatus() {
  const statuses = [
    { status: 'pending_authorization', expected: ['主机授权', '关闭'] },
    { status: 'active', expected: ['暂停', '接管', '关闭'] },
    { status: 'paused', expected: ['恢复', '接管', '关闭'] },
    { status: 'takeover', expected: ['释放', '关闭'] },
    { status: 'degraded', expected: ['关闭'] },
    { status: 'closed', expected: [] },
  ];

  for (const tc of statuses) {
    const api = {
      listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
      browser: {
        listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', tc.status)] }),
        getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', tc.status)),
        listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
        getTakeoverView: () => Promise.resolve(makeTakeoverView()),
        write: () => Promise.resolve({ ok: true }),
      },
    };
    const ctx = freshEnv(api);
    ctx.NAGENT.browser.init();
    await tickN(8);

    const sideBody = byId['browser-side-body'];
    ok(!!sideBody, 'side body rendered for status ' + tc.status);
    const btns = findButtons(sideBody);
    const labels = btns.map((b) => b.textContent).filter((l) => l && l !== '×');
    // Check each expected button is present
    for (const expected of tc.expected) {
      ok(labels.indexOf(expected) !== -1, 'status ' + tc.status + ' shows button: ' + expected + ' (got: ' + labels.join(',') + ')');
    }
    // Check no unexpected buttons (besides poll select which is a <select> not button)
    for (const label of labels) {
      ok(tc.expected.indexOf(label) !== -1, 'status ' + tc.status + ' has no unexpected button: ' + label);
    }
  }
}

async function testPollingStartsAndStops() {
  let getSessionCount = 0;
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] }),
      getSession: () => { getSessionCount++; return Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'active')); },
      listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.browser.init();
  await tickN(8);

  // Polling started: at least 1 getSession call (from initial pollTick)
  ok(getSessionCount >= 1, 'polling triggered getSession, count=' + getSessionCount);
  ok(activeIntervals.length === 1, '1 active interval after init, got ' + activeIntervals.length);

  // deactivate stops polling
  ctx.NAGENT.browser.deactivate();
  ok(activeIntervals.length === 0, '0 active intervals after deactivate, got ' + activeIntervals.length);
}

async function testPollingStopsWhenSessionCloses() {
  let getSessionCount = 0;
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] }),
      getSession: () => {
        getSessionCount++;
        const status = getSessionCount === 1 ? 'active' : 'closed';
        return Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', status));
      },
      listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  freshEnv(api).NAGENT.browser.init();
  await tickN(8);

  ok(activeIntervals.length === 1, '1 active interval before session closes');
  activeIntervals[0].fn();
  await tickN(4);

  ok(getSessionCount === 2, 'closed status fetched on next poll');
  ok(activeIntervals.length === 0,
    '0 active intervals after session closes, got ' + activeIntervals.length);
}

async function testPollingRefreshesOnlyRealtimeViewAndPreservesFocus() {
  let actionListCalls = 0;
  let getSessionCalls = 0;
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] }),
      getSession: () => { getSessionCalls++; return Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'active')); },
      listActions: () => { actionListCalls++; return Promise.resolve({ actions: makeActions(), next_cursor: null }); },
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  freshEnv(api).NAGENT.browser.init();
  await tickN(8);

  const sideBody = byId['browser-side-body'];
  const sideContent = sideBody.children[0];
  const pollSelect = findByClass(byId['tab-browser'], 'browser-poll-select')[0];
  pollSelect.focus();
  const mainBody = byId['browser-main-body'];
  const statusBar = findByClass(mainBody, 'browser-status-bar')[0];
  const screenshotWrap = findByClass(mainBody, 'browser-screenshot-wrap')[0];
  const screenshot = screenshotWrap.children.find((child) => child.tag === 'img');
  const firstScreenshotSrc = screenshot.src;
  activeIntervals[0].fn();
  await tickN(4);

  ok(getSessionCalls >= 2, 'polling refreshes the realtime view');
  ok(actionListCalls === 1, 'polling does not refresh action history');
  ok(byId['browser-side-body'] === sideBody && sideBody.children[0] === sideContent,
    'polling leaves the side panel DOM untouched');
  ok(document.activeElement === pollSelect,
    'polling preserves focus in controls outside the realtime view');
  ok(byId['browser-main-body'] === mainBody
      && findByClass(mainBody, 'browser-status-bar')[0] === statusBar,
    'polling keeps the realtime view shell stable');
  ok(findByClass(mainBody, 'browser-screenshot-wrap')[0] === screenshotWrap
      && screenshotWrap.children.find((child) => child.tag === 'img') === screenshot,
    'polling updates the existing screenshot without replacing its DOM');
  ok(screenshot.src !== firstScreenshotSrc,
    'polling cache-busts the existing screenshot source');
}

async function testScreenshotRecoversAfterInitialLoadError() {
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] }),
      getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'active')),
      listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  freshEnv(api).NAGENT.browser.init();
  await tickN(8);

  const mainBody = byId['browser-main-body'];
  const wrap = findByClass(mainBody, 'browser-screenshot-wrap')[0];
  const screenshot = wrap.children.find((child) => child.tag === 'img');
  screenshot._listeners.error[0]();
  const fallback = wrap.children.find(
    (child) => child.className === 'browser-screenshot-placeholder');
  ok(wrap.classList.contains('browser-screenshot-wrap--errored'),
    'initial screenshot failure enters error state');
  ok(!!fallback, 'initial screenshot failure renders fallback');

  screenshot._listeners.load[0]();
  ok(!wrap.classList.contains('browser-screenshot-wrap--errored'),
    'later screenshot load clears error state');
  ok(fallback._removed, 'later screenshot load removes stale fallback');
}

async function testTakeoverViewUrlNotInLocalStorage() {
  const takeoverUrl = 'http://browser:9222/vnc/websockify?session=bsess-1&cap=SECRET-CAP-TOKEN';
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'takeover')] }),
      getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'takeover', 'container')),
      listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView(takeoverUrl)),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.browser.init();
  await tickN(10);

  // takeover-view URL must NOT be in localStorage
  for (const call of lsSetCalls) {
    ok(call.value.indexOf(takeoverUrl) === -1, 'takeover URL not in localStorage setItem value');
    ok(call.value.indexOf('SECRET-CAP-TOKEN') === -1, 'capability token not in localStorage');
  }

  // takeover-view URL must NOT appear as visible text
  const mainBody = byId['browser-main-body'];
  if (mainBody) {
    const texts = allTexts(mainBody).join('|');
    ok(texts.indexOf(takeoverUrl) === -1, 'takeover URL not rendered as text');
    ok(texts.indexOf('SECRET-CAP-TOKEN') === -1, 'capability token not rendered as text');
  }

  // But iframe src should be set to the takeover URL (container)
  const iframes = findIframes(mainBody || document.body);
  ok(iframes.length === 1, '1 iframe rendered for container takeover, got ' + iframes.length);
  if (iframes.length > 0) {
    ok(iframes[0].src === takeoverUrl, 'iframe src set to takeover URL');
  }
}

async function testWriteActionSendsChallengeHeader() {
  let writeCalls = [];
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] }),
      getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'active')),
      listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: (op, sid, nid, token, body) => {
        writeCalls.push({ op, sid, nid, token, body });
        return Promise.resolve({ ok: true });
      },
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.browser.init();
  await tickN(8);

  // Find the pause button and click it
  const sideBody = byId['browser-side-body'];
  const btns = findButtons(sideBody);
  const pauseBtn = btns.find((b) => b.textContent === '暂停');
  ok(!!pauseBtn, 'pause button found');
  if (pauseBtn) {
    (pauseBtn._listeners.click || []).forEach((fn) => fn());
    await tickN(5);
    ok(writeCalls.length === 1, '1 write call after click, got ' + writeCalls.length);
    if (writeCalls.length > 0) {
      ok(writeCalls[0].op === 'pause', 'write op is pause');
      ok(writeCalls[0].token === 'tok-pause', 'write token from write_challenges');
      ok(writeCalls[0].sid === 'bsess-1', 'write sid correct');
      ok(writeCalls[0].nid === 'nagent-1', 'write nid correct');
    }
  }
}

async function testSafeUrlStripsQueryAndFragment() {
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'active')] }),
      getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'active')),
      listActions: () => Promise.resolve({
        actions: [{
          id: 'act-1', action_type: 'navigate', status: 'success',
          safe_url: 'https://example.com/page?secret=token#frag',
          title: 'Test', duration_ms: 100,
          created_at: '2026-07-27T10:05:00+00:00', error_code: null,
        }],
        next_cursor: null,
      }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.browser.init();
  await tickN(8);

  const mainBody = byId['browser-main-body'];
  const texts = allTexts(mainBody).join('|');
  ok(texts.indexOf('https://example.com/page?secret=token') === -1, 'query stripped from safe URL display');
  ok(texts.indexOf('https://example.com/page') !== -1, 'safe URL (origin+path) displayed');
}

async function testClosedStatusNoScreenshot() {
  const api = {
    listSessions: () => Promise.resolve([makeNSession('nagent-1')]),
    browser: {
      listSessions: () => Promise.resolve({ sessions: [makeBrowserSession('bsess-1', 'nagent-1', 'closed')] }),
      getSession: () => Promise.resolve(makeSessionDetail('bsess-1', 'nagent-1', 'closed')),
      listActions: () => Promise.resolve({ actions: makeActions(), next_cursor: null }),
      getTakeoverView: () => Promise.resolve(makeTakeoverView()),
      write: () => Promise.resolve({ ok: true }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.browser.init();
  await tickN(8);

  const mainBody = byId['browser-main-body'];
  const imgs = mainBody.children.filter((c) => c.className === 'browser-screenshot-wrap');
  // Closed status should show placeholder, not screenshot
  const texts = allTexts(mainBody).join('|');
  ok(texts.indexOf('会话已关闭') !== -1, 'closed status shows closed message');
  // No img element in screenshot wrap
  if (imgs.length > 0) {
    const img = imgs[0].children.find((c) => c.tag === 'img');
    ok(!img, 'no img for closed session');
  }
}

async function main() {
  await testSourceSafety();
  await testInitRendersDetailWithoutSessionSelector();
  await testExecutorEntryRendersHistoryAndDetailNavigates();
  await testSelectRendersMainView();
  await testControlMatrixByStatus();
  await testPollingStartsAndStops();
  await testPollingStopsWhenSessionCloses();
  await testPollingRefreshesOnlyRealtimeViewAndPreservesFocus();
  await testScreenshotRecoversAfterInitialLoadError();
  await testTakeoverViewUrlNotInLocalStorage();
  await testWriteActionSendsChallengeHeader();
  await testSafeUrlStripsQueryAndFragment();
  await testClosedStatusNoScreenshot();

  if (failures) { console.error(failures + ' failure(s)'); process.exit(1); }
  console.log('OK browser frontend harness passed');
  process.exit(0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(1); });
