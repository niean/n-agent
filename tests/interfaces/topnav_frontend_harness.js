'use strict';
// Harness for topnav feature.
// T2: management-navigation.js routing (resolveRoute/applyRoute/navigatePath).
// T3/T5/T6/T7 will extend this file with TopNav/app/tasks-observations/observations tests.
// Run: node tests/interfaces/topnav_frontend_harness.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const NAV_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'management-navigation.js');
const navCode = fs.readFileSync(NAV_JS, 'utf8');

const TOPNAV_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'topnav.js');
let topnavCode = '';
try { topnavCode = fs.readFileSync(TOPNAV_JS, 'utf8'); } catch (_) { topnavCode = ''; }

const APP_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'app.js');
let appCode = '';
try { appCode = fs.readFileSync(APP_JS, 'utf8'); } catch (_) { appCode = ''; }

const TO_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'tasks-observations.js');
let toCode = '';
try { toCode = fs.readFileSync(TO_JS, 'utf8'); } catch (_) { toCode = ''; }

const OBS_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'observations.js');
let obsCode = '';
try { obsCode = fs.readFileSync(OBS_JS, 'utf8'); } catch (_) { obsCode = ''; }

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// --- DOM mock ---------------------------------------------------------------
const allEls = [];
const byId = {};

function makeEl(tag) {
  const el = {
    tag: tag || 'div',
    _cls: new Set(),
    dataset: {},
    _attrs: {},
    style: { _p: {}, setProperty(k, v) { this._p[k] = String(v); } },
    hidden: false,
    _listeners: {},
    _kids: [],
    _text: '',
    _id: null,
    value: '',
    type: '',
    _removed: false,
    // layout/disabled metrics for overflow testing (defaults = no overflow)
    scrollWidth: 0,
    clientWidth: 0,
    scrollLeft: 0,
    scrollHeight: 0,
    clientHeight: 0,
    scrollTop: 0,
    disabled: false,
    set textContent(v) { this._text = (v == null) ? '' : String(v); },
    get textContent() { return this._text; },
    set id(v) { this._id = v; if (v != null) byId[v] = this; },
    get id() { return this._id; },
    get classList() {
      const self = this;
      return {
        add(...cs) { cs.forEach((c) => self._cls.add(c)); },
        remove(...cs) { cs.forEach((c) => self._cls.delete(c)); },
        toggle(c, force) {
          if (force === true) { self._cls.add(c); return true; }
          if (force === false) { self._cls.delete(c); return false; }
          if (self._cls.has(c)) { self._cls.delete(c); return false; }
          self._cls.add(c); return true;
        },
        contains(c) { return self._cls.has(c); },
      };
    },
    appendChild(c) { this._kids.push(c); return c; },
    append(...cs) { cs.forEach((c) => this._kids.push(c)); },
    replaceChildren() { this._kids = []; },
    removeChild(c) { const i = this._kids.indexOf(c); if (i >= 0) this._kids.splice(i, 1); },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (this._listeners[type]) this._listeners[type] = this._listeners[type].filter((f) => f !== fn);
    },
    setAttribute(k, v) {
      if (k === 'class') { /* noop */ }
      else this._attrs[k] = String(v);
    },
    getAttribute(k) {
      if (k in this._attrs) return this._attrs[k];
      if (k.startsWith('data-')) {
        const prop = k.replace(/^data-/, '').replace(/-([a-z])/g, (m, c) => c.toUpperCase());
        if (prop in this.dataset) return this.dataset[prop];
      }
      return null;
    },
    hasAttribute(k) { return k in this._attrs || this.getAttribute(k) !== null; },
    removeAttribute(k) { delete this._attrs[k]; },
    remove() { this._removed = true; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    focus() {},
    click() {
      (this._listeners.click || []).forEach((fn) => fn({ type: 'click', target: this, preventDefault() {}, stopPropagation() {} }));
    },
  };
  allEls.push(el);
  return el;
}

function matchSelector(el, selector) {
  if (!selector) return false;
  const sel = selector.trim();
  const classParts = sel.match(/\.[\w-]+/g) || [];
  const attrParts = [...sel.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)];
  for (const cp of classParts) {
    if (!el._cls.has(cp.slice(1))) return false;
  }
  for (const ap of attrParts) {
    const key = ap[1];
    const val = ap[2];
    if (key.startsWith('data-')) {
      const prop = key.replace(/^data-/, '').replace(/-([a-z])/g, (m, c) => c.toUpperCase());
      if (val === undefined) { if (!(prop in el.dataset)) return false; }
      else { if (el.dataset[prop] !== val) return false; }
    } else {
      if (val === undefined) { if (!(key in el._attrs)) return false; }
      else { if (el._attrs[key] !== val) return false; }
    }
  }
  return true;
}

function makeLocalStorage() {
  const store = {};
  return {
    getItem(k) { return k in store ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
  };
}

function loadNavigation(opts) {
  opts = opts || {};
  allEls.length = 0;
  for (const k of Object.keys(byId)) delete byId[k];

  const pushStateCalls = [];
  const localStorage = makeLocalStorage();
  const alertCalls = [];

  const body = makeEl('body');
  const title = makeEl('span');
  title.id = 'topbar-title';
  const toggle = makeEl('button');
  toggle.id = 'sidebar-toggle';

  const tabIds = ['summary', 'chat', 'tasks', 'scheduled-tasks', 'sessions', 'memory',
    'tools-knowledge', 'tools-mcp', 'tools-skill', 'tools-plugin', 'tools-builtin',
    'sandbox', 'executors-host', 'models', 'platforms',
    'observations-sessions', 'observations-modules', 'security'];
  tabIds.forEach((id) => {
    const el = makeEl('div');
    el._cls.add('tab-content');
    el.id = 'tab-' + id;
  });

  const sidebarTabs = ['summary', 'chat', 'tasks', 'scheduled-tasks', 'sessions', 'memory',
    'tools', 'tools-knowledge', 'tools-mcp', 'tools-skill', 'tools-plugin', 'tools-builtin',
    'executors', 'sandbox', 'executors-host', 'models', 'platforms',
    'observations', 'observations-sessions', 'observations-modules', 'security'];
  sidebarTabs.forEach((tab) => {
    const item = makeEl('a');
    item._cls.add('sidebar__item');
    item.dataset.tab = tab;
    if (['tools', 'executors', 'observations'].indexOf(tab) !== -1) {
      item._cls.add('sidebar__item--parent');
      const submenu = makeEl('div');
      submenu._cls.add('sidebar__submenu');
      submenu.dataset.submenuOf = tab;
    }
  });

  const win = {
    NAGENT: { modal: { alert(msg, opts2) { alertCalls.push({ msg, opts: opts2 }); }, confirm() { return Promise.resolve(true); } } },
    location: { pathname: opts.pathname || '/', protocol: 'http:', host: 'x' },
    _listeners: {},
    addEventListener(type, fn) { (win._listeners[type] = win._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (win._listeners[type]) win._listeners[type] = win._listeners[type].filter((f) => f !== fn);
    },
    history: {
      pushState(state, t, url) { pushStateCalls.push({ state, title: t, url }); if (url) win.location.pathname = url; },
    },
  };

  const doc = {
    _listeners: {},
    body,
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { const el = makeEl('#text'); el._text = String(t); return el; },
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) { return allEls.filter((el) => matchSelector(el, sel))[0] || null; },
    querySelectorAll(sel) { return allEls.filter((el) => matchSelector(el, sel)); },
    addEventListener(type, fn) { (doc._listeners[type] = doc._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (doc._listeners[type]) doc._listeners[type] = doc._listeners[type].filter((f) => f !== fn);
    },
  };

  const ctx = {
    NAGENT: win.NAGENT,
    document: doc,
    window: win,
    console,
    localStorage,
    history: win.history,
  };
  vm.createContext(ctx);
  vm.runInContext(navCode, ctx);

  return { win, doc, ctx, nav: ctx.NAGENT.navigation, pushStateCalls, alertCalls, byId, allEls };
}

// ===========================================================================
// T2: Exports
// ===========================================================================
const env = loadNavigation();
const nav = env.nav;
ok(typeof nav === 'object', 'NAGENT.navigation exported');
ok(typeof nav.resolveRoute === 'function', 'resolveRoute exported');
ok(typeof nav.applyRoute === 'function', 'applyRoute exported');
ok(typeof nav.navigatePath === 'function', 'navigatePath exported');
ok(typeof nav.activeTopnavItem === 'function', 'activeTopnavItem exported');
ok(typeof nav.buildRouteByPath === 'function', 'buildRouteByPath exported');
ok(typeof nav.navigateTo === 'function', 'navigateTo preserved (compat)');
ok(typeof nav.switchTab === 'function', 'switchTab preserved (compat)');
ok(typeof nav.initNavigation === 'function', 'initNavigation preserved');
ok(nav.topnavConfig && typeof nav.topnavConfig === 'object', 'topnavConfig exported');

// ===========================================================================
// T2 Assertion 1: resolveRoute('/tasks/observations')
// ===========================================================================
let state = nav.resolveRoute('/tasks/observations');
ok(state.activeTab === 'tasks-observations', 'A1 activeTab (got ' + state.activeTab + ')');
ok(state.sidebarTab === 'tasks', 'A1 sidebarTab (got ' + state.sidebarTab + ')');
ok(state.currentSubdomain === 'tasks', 'A1 currentSubdomain (got ' + state.currentSubdomain + ')');
ok(state.renderTab === 'tasks-observations', 'A1 renderTab (got ' + state.renderTab + ')');
ok(state.route !== null, 'A1 route non-null');

// ===========================================================================
// T2 Assertion 2: resolveRoute('/observations/tasks') (alias override)
// ===========================================================================
state = nav.resolveRoute('/observations/tasks');
ok(state.activeTab === 'tasks-observations', 'A2 activeTab (got ' + state.activeTab + ')');
ok(state.sidebarTab === 'observations-sessions', 'A2 sidebarTab (got ' + state.sidebarTab + ')');
ok(state.currentSubdomain === 'tasks', 'A2 currentSubdomain (got ' + state.currentSubdomain + ')');
ok(state.renderTab === 'tasks-observations', 'A2 renderTab (got ' + state.renderTab + ')');
ok(state.route !== null, 'A2 route non-null');

// ===========================================================================
// T2 Assertion 3: resolveRoute('/tasks') -> currentSubdomain:'tasks' (leaf)
// ===========================================================================
state = nav.resolveRoute('/tasks');
ok(state.activeTab === 'tasks', 'A3 activeTab (got ' + state.activeTab + ')');
ok(state.currentSubdomain === 'tasks', 'A3 currentSubdomain (got ' + state.currentSubdomain + ')');
ok(state.route === null, 'A3 route null (not scoped)');

// ===========================================================================
// T2 Assertion 4: resolveRoute('/chat') -> currentSubdomain:null
// ===========================================================================
state = nav.resolveRoute('/chat');
ok(state.activeTab === 'chat', 'A4 activeTab (got ' + state.activeTab + ')');
ok(state.currentSubdomain === null, 'A4 currentSubdomain (got ' + state.currentSubdomain + ')');

// ===========================================================================
// T2 Assertion 4b: browser detail route keeps the browser module active
// ===========================================================================
state = nav.resolveRoute('/browser/session');
ok(state.activeTab === 'browser', 'A4b activeTab (got ' + state.activeTab + ')');
ok(state.currentSubdomain === 'executors', 'A4b currentSubdomain (got ' + state.currentSubdomain + ')');

// ===========================================================================
// T2 Assertion 5: precise topnav item matching, no indexOf fallback
// ===========================================================================
const tasksItems = nav.topnavConfig.tasks;
ok(tasksItems && tasksItems.length === 3, 'A5 topnavConfig.tasks has 3 items');
ok(nav.activeTopnavItem(tasksItems, 'tasks') !== null, 'A5 matches tasks');
ok(nav.activeTopnavItem(tasksItems, 'tasks-observations') !== null, 'A5 matches tasks-observations');
ok(nav.activeTopnavItem(tasksItems, 'tasks-security') !== null, 'A5 matches tasks-security');
ok(nav.activeTopnavItem(tasksItems, 'tools-operation') === null, 'A5 no fuzzy match for tools-operation');
ok(nav.activeTopnavItem(tasksItems, 'tools') === null, 'A5 no match for tools');
ok(nav.activeTopnavItem(tasksItems, 'summary') === null, 'A5 no match for summary');
state = nav.resolveRoute('/tools/operation');
ok(state.activeTab === 'summary', 'A5 /tools/operation -> summary (got ' + state.activeTab + ')');
ok(nav.activeTopnavItem(tasksItems, state.activeTab) === null, 'A5 /tools/operation no topnav highlight');

// ===========================================================================
// T2 Assertion 6: validation + no-throw on missing config
// ===========================================================================
// 6a: duplicate path
try {
  nav.buildRouteByPath([
    { paths: ['/a', '/b'], tab: 'x', renderTab: 'x', sidebarTab: 'x', topnavParent: 'x', scope: 'x' },
    { paths: ['/b', '/c'], tab: 'y', renderTab: 'y', sidebarTab: 'y', topnavParent: 'y', scope: 'y' },
  ]);
  ok(false, 'A6a duplicate path should throw');
} catch (e) { ok(true, 'A6a duplicate path throws'); }
// 6b: missing topnavParent
try {
  nav.buildRouteByPath([
    { paths: ['/a'], tab: 'x', renderTab: 'x', sidebarTab: 'x', scope: 'x' },
  ]);
  ok(false, 'A6b missing topnavParent should throw');
} catch (e) { ok(true, 'A6b missing topnavParent throws'); }
// 6c: empty field
try {
  nav.buildRouteByPath([
    { paths: ['/a'], tab: '', renderTab: 'x', sidebarTab: 'x', topnavParent: 'x', scope: 'x' },
  ]);
  ok(false, 'A6c empty tab should throw');
} catch (e) { ok(true, 'A6c empty tab throws'); }
// 6d: applyRoute no throw when currentSubdomain is null
try {
  nav.applyRoute(nav.resolveRoute('/chat'));
  ok(true, 'A6d applyRoute(/chat) no throw');
} catch (e) { ok(false, 'A6d applyRoute(/chat) threw: ' + e.message); }
// 6e: applyRoute no throw when currentSubdomain has no topnavConfig
try {
  nav.applyRoute(nav.resolveRoute('/observations/sessions'));
  ok(true, 'A6e applyRoute(/observations/sessions) no throw');
} catch (e) { ok(false, 'A6e applyRoute(/observations/sessions) threw: ' + e.message); }

// ===========================================================================
// T2 Assertion 7: items not containing activeTab -> no aria-current
// ===========================================================================
const singleItem = [{ tab: 'tasks', path: '/tasks', label: 'M', concern: 'management', scope: 'tasks', topnavParent: 'tasks' }];
ok(nav.activeTopnavItem(singleItem, 'tasks-observations') === null, 'A7 returns null when activeTab not in items');
ok(nav.activeTopnavItem(singleItem, 'tasks') !== null, 'A7 returns item when activeTab in items');
try {
  nav.applyRoute({ activeTab: 'tasks-observations', renderTab: 'tasks-observations', sidebarTab: 'tasks', currentSubdomain: 'tasks', route: null });
  ok(true, 'A7 applyRoute no throw when activeTab not in topnav items');
} catch (e) { ok(false, 'A7 applyRoute threw: ' + e.message); }

// ===========================================================================
// T2 Assertion 8: existing path resolution unchanged
// ===========================================================================
const regressions = [
  ['/tasks/t_123', 'tasks'],
  ['/scheduled-tasks/42', 'scheduled-tasks'],
  ['/observations/sessions/sess_1', 'observations-sessions'],
  ['/tools/external-memory', 'memory'],
  ['/tools/sandbox', 'sandbox'],
  ['/', 'summary'],
];
regressions.forEach(([p, expectedTab]) => {
  const s = nav.resolveRoute(p);
  ok(s.activeTab === expectedTab, 'A8 ' + p + ' -> ' + expectedTab + ' (got ' + s.activeTab + ')');
  ok(s.route === null, 'A8 ' + p + ' route=null');
});

// ===========================================================================
// T2 Extra: scoped route missing container -> error state + modal.alert
// ===========================================================================
env.alertCalls.length = 0;
nav.applyRoute(nav.resolveRoute('/tasks/observations'));
ok(env.alertCalls.length === 1, 'scoped route missing container -> modal.alert called');

// ===========================================================================
// T2 Extra: navigatePath does pushState + resolveRoute + applyRoute
// ===========================================================================
const nav2 = loadNavigation({ pathname: '/chat' });
nav2.nav.navigatePath('/tasks');
ok(nav2.pushStateCalls.length === 1, 'navigatePath pushes state when path changes');
ok(nav2.pushStateCalls[0].url === '/tasks', 'navigatePath pushes /tasks');
ok(nav2.byId['tab-tasks'] && nav2.byId['tab-tasks']._cls.has('active'), 'navigatePath activates tab-tasks');
ok(nav2.byId['topbar-title']._text === '任务', 'navigatePath sets title (got ' + nav2.byId['topbar-title']._text + ')');
// Same path -> no extra pushState
nav2.nav.navigatePath('/tasks');
ok(nav2.pushStateCalls.length === 1, 'navigatePath no pushState when path unchanged');

// ===========================================================================
// T3: TopNav component (topnav.js)
// ===========================================================================
// Live-subtree queries: allEls accumulates every element ever created (even
// removed ones), so querySelector would return stale nodes after re-render.
// These helpers walk the live container subtree only.
function liveDescendants(root) {
  const result = [];
  function walk(el) {
    if (!el || !el._kids) return;
    for (const k of el._kids) { result.push(k); walk(k); }
  }
  walk(root);
  return result;
}
function liveQuery(root, sel) {
  return liveDescendants(root).filter((el) => matchSelector(el, sel));
}
function liveQueryOne(root, sel) {
  const m = liveQuery(root, sel);
  return m.length ? m[m.length - 1] : null;
}

function makeResizeObserverMock() {
  const instances = [];
  const Ctor = function (cb) {
    const inst = {
      cb: cb,
      _observed: [],
      _disconnected: false,
      observe(t) { this._observed.push(t); },
      unobserve(t) { this._observed = this._observed.filter((x) => x !== t); },
      disconnect() { this._disconnected = true; this._observed = []; },
      trigger(entries) {
        if (this._disconnected) return;
        const e = entries || this._observed.map((t) => ({ target: t }));
        this.cb(e);
      },
    };
    instances.push(inst);
    return inst;
  };
  return { Ctor: Ctor, instances: instances };
}

function loadTopnav(opts) {
  opts = opts || {};
  allEls.length = 0;
  for (const k of Object.keys(byId)) delete byId[k];

  const pushStateCalls = [];
  const localStorage = makeLocalStorage();
  const consoleErrors = [];
  const consoleWarns = [];

  const container = makeEl('div');
  container.id = 'topnav-mount';

  const ro = makeResizeObserverMock();

  const win = {
    NAGENT: (opts.dev === false) ? {} : { __DEV__: true },
    location: { pathname: opts.pathname || '/', protocol: 'http:', host: 'x' },
    _listeners: {},
    addEventListener(type, fn) { (win._listeners[type] = win._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (win._listeners[type]) win._listeners[type] = win._listeners[type].filter((f) => f !== fn);
    },
    history: { pushState(state, t, url) { pushStateCalls.push({ state, title: t, url }); } },
    matchMedia(query) {
      return {
        matches: !!opts.reducedMotion, media: query,
        addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
      };
    },
    ResizeObserver: opts.noResizeObserver ? undefined : ro.Ctor,
  };

  const doc = {
    _listeners: {},
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { const el = makeEl('#text'); el._text = String(t); return el; },
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) { return allEls.filter((el) => matchSelector(el, sel))[0] || null; },
    querySelectorAll(sel) { return allEls.filter((el) => matchSelector(el, sel)); },
    addEventListener(type, fn) { (doc._listeners[type] = doc._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (doc._listeners[type]) doc._listeners[type] = doc._listeners[type].filter((f) => f !== fn);
    },
  };

  const ctx = {
    NAGENT: win.NAGENT,
    document: doc,
    window: win,
    console: {
      log() {},
      error(m) { consoleErrors.push(String(m)); },
      warn(m) { consoleWarns.push(String(m)); },
    },
    localStorage,
    history: win.history,
  };
  vm.createContext(ctx);
  if (topnavCode) vm.runInContext(topnavCode, ctx);

  return {
    win: win, doc: doc, ctx: ctx, topnav: ctx.NAGENT.topnav,
    pushStateCalls: pushStateCalls, consoleErrors: consoleErrors, consoleWarns: consoleWarns,
    byId: byId, allEls: allEls, container: container, roInstances: ro.instances,
  };
}

function loadApp(opts) {
  opts = opts || {};
  allEls.length = 0;
  for (const k of Object.keys(byId)) delete byId[k];

  const localStorage = makeLocalStorage();

  // DOM elements: topbar title + wrap + topnav mount + tab containers
  const topbarTitle = makeEl('span');
  topbarTitle.id = 'topbar-title';
  const topbarTitleWrap = makeEl('div');
  topbarTitleWrap.id = 'topbar-title-wrap';
  const topnavMount = makeEl('div');
  topnavMount.id = 'topnav-mount';

  ['tasks-observations', 'tasks', 'chat', 'summary'].forEach((id) => {
    const el = makeEl('div');
    el._cls.add('tab-content');
    el.id = 'tab-' + id;
  });

  // Stub call records
  const initCalls = [];
  const refreshCalls = [];
  const deactivateCalls = [];
  const renderCalls = [];
  const destroyCalls = [];
  const navigateCalls = [];

  const topnavConfig = {
    tasks: [
      { tab: 'tasks', path: '/tasks', label: '管理', concern: 'management', scope: 'tasks', topnavParent: 'tasks' },
      { tab: 'tasks-observations', path: '/tasks/observations', label: '观测', concern: 'observation', scope: 'tasks', topnavParent: 'tasks' },
    ],
  };

  // Deferred init for in-flight Promise test
  let resolveInit = null;
  const initPromise = opts.deferInit ? new Promise((r) => { resolveInit = r; }) : null;

  const tasksObservations = {
    init() { initCalls.push('tasks-observations'); return initPromise; },
    refresh() { refreshCalls.push('tasks-observations'); return undefined; },
    deactivate() { deactivateCalls.push('tasks-observations'); },
  };

  const win = {
    NAGENT: {
      tasksObservations: tasksObservations,
      topnav: {
        render(container, opts2) { renderCalls.push({ container: container, opts: opts2 }); },
        destroy() { destroyCalls.push('destroy'); },
      },
      navigation: {
        topnavConfig: topnavConfig,
        navigatePath(path) { navigateCalls.push(path); },
        initNavigation() {},
      },
    },
    location: { pathname: opts.pathname || '/', protocol: 'http:', host: 'x' },
    _listeners: {},
    addEventListener(type, fn) { (win._listeners[type] = win._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (win._listeners[type]) win._listeners[type] = win._listeners[type].filter((f) => f !== fn);
    },
    history: { pushState() {} },
  };

  const doc = {
    _listeners: {},
    body: makeEl('body'),
    readyState: 'complete',
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { const el = makeEl('#text'); el._text = String(t); return el; },
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) { return allEls.filter((el) => matchSelector(el, sel))[0] || null; },
    querySelectorAll(sel) { return allEls.filter((el) => matchSelector(el, sel)); },
    addEventListener(type, fn) { (doc._listeners[type] = doc._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (doc._listeners[type]) doc._listeners[type] = doc._listeners[type].filter((f) => f !== fn);
    },
  };

  const ctx = {
    NAGENT: win.NAGENT,
    document: doc,
    window: win,
    console: console,
    localStorage: localStorage,
    history: win.history,
  };
  vm.createContext(ctx);
  if (appCode) vm.runInContext(appCode, ctx);

  return {
    win: win, doc: doc, ctx: ctx,
    NAGENT: ctx.NAGENT, app: ctx.NAGENT.app,
    initCalls: initCalls, refreshCalls: refreshCalls, deactivateCalls: deactivateCalls,
    renderCalls: renderCalls, destroyCalls: destroyCalls, navigateCalls: navigateCalls,
    byId: byId, allEls: allEls,
    topnavMount: topnavMount, topbarTitle: topbarTitle, topbarTitleWrap: topbarTitleWrap,
    resolveInit: resolveInit,
  };
}

function loadTasksObservations(opts) {
  opts = opts || {};
  allEls.length = 0;
  for (const k of Object.keys(byId)) delete byId[k];

  const navigateCalls = [];
  const apiCalls = { board: 0, listSessions: 0, getStats: 0, getRecords: 0, getCompressions: 0, getBreakdown: 0 };

  // Container
  const container = makeEl('div');
  container._cls.add('tab-content');
  container.id = 'tab-tasks-observations';
  if (opts.active !== false) container._cls.add('active');

  // UI helpers (stub matching management-ui.js surface). el uses classList.add
  // so the mock's matchSelector (which checks _cls) can find elements by class.
  const ui = {
    byId: (id) => byId[id] || null,
    clear: (el) => { if (el) el.replaceChildren(); },
    el: (tag, className) => {
      const node = makeEl(tag);
      if (className) String(className).split(/\s+/).filter(Boolean).forEach((c) => node._cls.add(c));
      return node;
    },
    renderEmpty: (parent, message) => {
      const node = makeEl('div');
      node.classList.add('muted', 'empty-state');
      node.textContent = message || '暂无数据';
      parent.appendChild(node);
    },
    renderLoading: (parent, message) => {
      const node = makeEl('div');
      node.classList.add('muted', 'loading-state');
      node.textContent = message || '加载中...';
      parent.appendChild(node);
    },
    renderError: (parent, message) => {
      const node = makeEl('div');
      node.classList.add('muted', 'error-state');
      node.textContent = message || '加载失败';
      parent.appendChild(node);
    },
  };

  // API stubs with call tracking. All usage methods are swappable via setters
  // so tests can flip behavior between failure/success. listSessions returns
  // the paginated {items, total, page} shape (matching /chat/usage/sessions).
  // board returns the {columns:[{cards:[task,...]}]} shape (matching
  // /chat/tasks/board), where each task dict carries origin_session_id and
  // execution_session_id (exposed by task_service._task_to_dict).
  let boardImpl = opts.board || (async () => ({ columns: [] }));
  let listSessionsImpl = opts.listSessions || (async () => ({ items: [], total: 0, page: 1 }));
  let getStatsImpl = opts.getStats || (async () => ({}));
  let getRecordsImpl = opts.getRecords || (async () => []);
  let getCompressionsImpl = opts.getCompressions || (async () => []);
  let getBreakdownImpl = opts.getBreakdown || (async () => ({}));

  const api = {
    usage: {
      listSessions: (...args) => { apiCalls.listSessions++; return listSessionsImpl(...args); },
      getStats: (...args) => { apiCalls.getStats++; return getStatsImpl(...args); },
      getRecords: (...args) => { apiCalls.getRecords++; return getRecordsImpl(...args); },
      getCompressions: (...args) => { apiCalls.getCompressions++; return getCompressionsImpl(...args); },
      getBreakdown: (...args) => { apiCalls.getBreakdown++; return getBreakdownImpl(...args); },
      getOverview: () => Promise.resolve({}),
    },
    task: {
      board: (...args) => { apiCalls.board++; return boardImpl(...args); },
    },
  };

  const navigation = {
    navigatePath: (path) => { navigateCalls.push(path); },
  };

  const win = {
    NAGENT: { ui: ui, api: api, navigation: navigation },
    location: { pathname: '/tasks/observations', search: '', protocol: 'http:', host: 'x' },
    _listeners: {},
    addEventListener(type, fn) { (win._listeners[type] = win._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (win._listeners[type]) win._listeners[type] = win._listeners[type].filter((f) => f !== fn);
    },
    history: { pushState() {} },
  };

  const doc = {
    _listeners: {},
    body: makeEl('body'),
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { const el = makeEl('#text'); el._text = String(t); return el; },
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) { return allEls.filter((el) => matchSelector(el, sel))[0] || null; },
    querySelectorAll(sel) { return allEls.filter((el) => matchSelector(el, sel)); },
    addEventListener(type, fn) { (doc._listeners[type] = doc._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (doc._listeners[type]) doc._listeners[type] = doc._listeners[type].filter((f) => f !== fn);
    },
  };

  const ctx = {
    NAGENT: win.NAGENT,
    document: doc,
    window: win,
    console: console,
    localStorage: makeLocalStorage(),
    history: win.history,
    URLSearchParams: URLSearchParams,
  };
  vm.createContext(ctx);
  // Load observations.js first so its renderers are available for reuse by
  // tasks-observations.js.
  if (obsCode) vm.runInContext(obsCode, ctx);
  if (toCode) vm.runInContext(toCode, ctx);

  return {
    win: win, doc: doc, ctx: ctx,
    NAGENT: ctx.NAGENT, container: container,
    apiCalls: apiCalls, navigateCalls: navigateCalls,
    byId: byId, allEls: allEls,
    setListSessions: (fn) => { listSessionsImpl = fn; },
    setBoard: (fn) => { boardImpl = fn; },
    setGetStats: (fn) => { getStatsImpl = fn; },
    setGetRecords: (fn) => { getRecordsImpl = fn; },
    setGetCompressions: (fn) => { getCompressionsImpl = fn; },
    setGetBreakdown: (fn) => { getBreakdownImpl = fn; },
  };
}

function loadObservations(opts) {
  opts = opts || {};
  allEls.length = 0;
  for (const k of Object.keys(byId)) delete byId[k];

  const navigateCalls = [];
  const pushStateCalls = [];
  const apiCalls = { getOverview: 0, listSessions: 0, getStats: 0, getRecords: 0, getCompressions: 0, getBreakdown: 0 };

  // Container: #tab-observations-sessions (active by default, like applyRoute sets it)
  const container = makeEl('div');
  container._cls.add('tab-content');
  container.id = 'tab-observations-sessions';
  if (opts.active !== false) container._cls.add('active');

  // UI helpers (stub matching management-ui.js surface used by observations.js)
  const ui = {
    byId: (id) => byId[id] || null,
    clear: (el) => { if (el) el.replaceChildren(); },
    el: (tag, className) => {
      const node = makeEl(tag);
      if (className) String(className).split(/\s+/).filter(Boolean).forEach((c) => node._cls.add(c));
      return node;
    },
    renderEmpty: (parent, message) => {
      const node = makeEl('div');
      node.classList.add('muted', 'empty-state');
      node.textContent = message || '暂无数据';
      parent.appendChild(node);
    },
    renderLoading: (parent, message) => {
      const node = makeEl('div');
      node.classList.add('muted', 'loading-state');
      node.textContent = message || '加载中...';
      parent.appendChild(node);
    },
    renderError: (parent, message) => {
      const node = makeEl('div');
      node.classList.add('muted', 'error-state');
      node.textContent = message || '加载失败';
      parent.appendChild(node);
    },
  };

  // Swappable API stubs with call tracking
  let getOverviewImpl = opts.getOverview || (async () => ({}));
  let listSessionsImpl = opts.listSessions || (async () => ({ items: [], total: 0, page: 1 }));
  let getStatsImpl = opts.getStats || (async () => ({}));
  let getRecordsImpl = opts.getRecords || (async () => []);
  let getCompressionsImpl = opts.getCompressions || (async () => []);
  let getBreakdownImpl = opts.getBreakdown || (async () => ({}));

  const api = {
    usage: {
      getOverview: (...args) => { apiCalls.getOverview++; return getOverviewImpl(...args); },
      listSessions: (...args) => { apiCalls.listSessions++; return listSessionsImpl(...args); },
      getStats: (...args) => { apiCalls.getStats++; return getStatsImpl(...args); },
      getRecords: (...args) => { apiCalls.getRecords++; return getRecordsImpl(...args); },
      getCompressions: (...args) => { apiCalls.getCompressions++; return getCompressionsImpl(...args); },
      getBreakdown: (...args) => { apiCalls.getBreakdown++; return getBreakdownImpl(...args); },
    },
  };

  const navigation = {
    navigatePath: (path) => { navigateCalls.push(path); },
  };

  const win = {
    NAGENT: { ui: ui, api: api, navigation: navigation },
    location: {
      pathname: opts.pathname || '/observations/sessions',
      search: opts.search || '',
      protocol: 'http:', host: 'x',
    },
    _listeners: {},
    addEventListener(type, fn) { (win._listeners[type] = win._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (win._listeners[type]) win._listeners[type] = win._listeners[type].filter((f) => f !== fn);
    },
    history: {
      pushState(state, t, url) { pushStateCalls.push({ state, title: t, url }); if (url) win.location.pathname = url; },
    },
  };

  const doc = {
    _listeners: {},
    body: makeEl('body'),
    createElement(tag) { return makeEl(tag); },
    createTextNode(t) { const el = makeEl('#text'); el._text = String(t); return el; },
    getElementById(id) { return byId[id] || null; },
    querySelector(sel) { return allEls.filter((el) => matchSelector(el, sel))[0] || null; },
    querySelectorAll(sel) { return allEls.filter((el) => matchSelector(el, sel)); },
    addEventListener(type, fn) { (doc._listeners[type] = doc._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      if (doc._listeners[type]) doc._listeners[type] = doc._listeners[type].filter((f) => f !== fn);
    },
  };

  const ctx = {
    NAGENT: win.NAGENT,
    document: doc,
    window: win,
    console: console,
    localStorage: makeLocalStorage(),
    history: win.history,
    URLSearchParams: URLSearchParams,
  };
  vm.createContext(ctx);
  if (obsCode) vm.runInContext(obsCode, ctx);

  return {
    win: win, doc: doc, ctx: ctx,
    NAGENT: ctx.NAGENT, container: container,
    apiCalls: apiCalls, navigateCalls: navigateCalls, pushStateCalls: pushStateCalls,
    byId: byId, allEls: allEls,
    setGetOverview: (fn) => { getOverviewImpl = fn; },
    setListSessions: (fn) => { listSessionsImpl = fn; },
  };
}

(function () {
  const items = [
    { tab: 'tasks', path: '/tasks', label: '管理' },
    { tab: 'tasks-observations', path: '/tasks/observations', label: '观测' },
  ];

  let env = loadTopnav();
  if (!env.topnav) {
    ok(false, 'T3 NAGENT.topnav not defined (topnav.js not loaded)');
  } else {
    ok(typeof env.topnav.render === 'function', 'T3 render exported');
    ok(typeof env.topnav.destroy === 'function', 'T3 destroy exported');

    // --- T3 A1: nav structure, aria-label, a[href], aria-current, active class, no border-bottom ---
    env = loadTopnav();
    const onAct = [];
    env.topnav.render(env.container, { items: items, activeTab: 'tasks-observations', onActivate: function (it) { onAct.push(it); } });
    let nav = liveQueryOne(env.container, 'nav[aria-label="子域导航"]');
    ok(nav !== null, 'T3 A1 nav[aria-label="子域导航"] rendered');
    let links = liveQuery(env.container, 'a[href]');
    ok(links.length === 2, 'T3 A1 two a[href] items (got ' + links.length + ')');
    let activeLink = links.filter((a) => a.getAttribute('aria-current') === 'page')[0];
    ok(activeLink && activeLink._cls.has('topnav__item--active'), 'T3 A1 active item has aria-current=page + topnav__item--active');
    ok(activeLink && activeLink.getAttribute('href') === '/tasks/observations', 'T3 A1 active link href');
    ok(activeLink && activeLink.style._p['border-bottom'] === undefined && activeLink.style._p['borderBottom'] === undefined, 'T3 A1 no border-bottom style on active item');
    ok(!activeLink._cls.has('topnav__item--border'), 'T3 A1 no border-bottom class');
    ok(links.filter((a) => a.getAttribute('aria-current') === 'page').length === 1, 'T3 A1 exactly one aria-current');

    // --- T3 A3: labels via textContent (no innerHTML) ---
    ok(links[0]._text === '管理' && links[1]._text === '观测', 'T3 A3 labels set via textContent');

    // --- T3 A2: no overflow -> controls hidden + tabindex=-1; overflow -> shown + boundary disabled ---
    env = loadTopnav();
    env.topnav.render(env.container, { items: items, activeTab: 'tasks' });
    let leftBtn = liveQueryOne(env.container, 'button[aria-label="向左滚动"]');
    let rightBtn = liveQueryOne(env.container, 'button[aria-label="向右滚动"]');
    ok(leftBtn && rightBtn, 'T3 A2 control buttons exist');
    ok(leftBtn.hidden === true && rightBtn.hidden === true, 'T3 A2 no overflow -> controls hidden');
    ok(leftBtn.getAttribute('tabindex') === '-1' && rightBtn.getAttribute('tabindex') === '-1', 'T3 A2 no overflow -> tabindex=-1');
    ok(leftBtn.getAttribute('type') === 'button' && rightBtn.getAttribute('type') === 'button', 'T3 A2 controls type=button');
    // simulate overflow
    let scroll = liveQueryOne(env.container, '.topnav__scroll');
    ok(scroll !== null, 'T3 A2 scroll element exists');
    scroll.scrollWidth = 150; scroll.clientWidth = 100;
    env.roInstances[0].trigger();
    ok(leftBtn.hidden === false && rightBtn.hidden === false, 'T3 A2 overflow -> controls shown');
    ok(leftBtn.getAttribute('tabindex') !== '-1' && rightBtn.getAttribute('tabindex') !== '-1', 'T3 A2 overflow -> tabindex not -1');
    ok(leftBtn.disabled === true, 'T3 A2 at left boundary -> left disabled');
    ok(rightBtn.disabled === false, 'T3 A2 at left boundary -> right enabled');
    rightBtn.click();
    ok(leftBtn.disabled === false, 'T3 A2 after scroll right -> left enabled');
    ok(rightBtn.disabled === true, 'T3 A2 at right boundary -> right disabled');

    // --- T3 A4: re-render destroys old; no duplicate listeners; one active handler ---
    env = loadTopnav();
    env.topnav.render(env.container, { items: items, activeTab: 'tasks' });
    ok(env.roInstances.length === 1, 'T3 A4 first render creates 1 observer');
    const oldObserver = env.roInstances[0];
    env.topnav.render(env.container, { items: items, activeTab: 'tasks' });
    ok(oldObserver._disconnected === true, 'T3 A4 re-render disconnects old observer');
    ok(env.roInstances.length === 2, 'T3 A4 second render creates new observer');
    let newLinks = liveQuery(env.container, 'a[href]');
    ok(newLinks.length === 2, 'T3 A4 re-render produces 2 live links (got ' + newLinks.length + ')');
    ok(newLinks[0]._listeners.click && newLinks[0]._listeners.click.length === 1, 'T3 A4 link click listener count == 1 (no duplicate)');
    // old observer trigger has no effect (disconnected); new observer updates
    let scrollNew = liveQueryOne(env.container, '.topnav__scroll');
    scrollNew.scrollWidth = 500; scrollNew.clientWidth = 200;
    let lb = liveQueryOne(env.container, 'button[aria-label="向左滚动"]');
    ok(lb.hidden === true, 'T3 A4 before trigger buttons hidden');
    oldObserver.trigger();
    ok(lb.hidden === true, 'T3 A4 old disconnected observer trigger no effect');
    env.roInstances[1].trigger();
    env.roInstances[1].trigger();
    ok(lb.hidden === false, 'T3 A4 new observer trigger (twice) shows buttons');
    // onActivate exactly once per click
    let actCount = 0;
    env.topnav.render(env.container, { items: items, activeTab: 'tasks', onActivate: function () { actCount++; } });
    liveQuery(env.container, 'a[href]')[0].click();
    ok(actCount === 1, 'T3 A4 onActivate called exactly once');

    // --- T3 A5: destroy cleans observer/listeners ---
    env = loadTopnav();
    env.topnav.render(env.container, { items: items, activeTab: 'tasks' });
    const obs = env.roInstances[0];
    env.topnav.destroy();
    ok(obs._disconnected === true, 'T3 A5 destroy disconnects observer');
    ok(env.container._kids.length === 0, 'T3 A5 destroy clears container');
    try { env.topnav.destroy(); ok(true, 'T3 A5 destroy twice no throw'); }
    catch (e) { ok(false, 'T3 A5 destroy twice threw: ' + e.message); }
    // fallback path: no ResizeObserver -> window resize listener removed on destroy
    env = loadTopnav({ noResizeObserver: true });
    env.topnav.render(env.container, { items: items, activeTab: 'tasks' });
    ok(env.win._listeners.resize && env.win._listeners.resize.length === 1, 'T3 A5 fallback registers 1 window resize listener');
    env.topnav.destroy();
    ok(env.win._listeners.resize.length === 0, 'T3 A5 destroy removes window resize listener');

    // --- T3 A6: wheel/scroll/keyboard recalc; reduced motion no transform ---
    env = loadTopnav();
    env.topnav.render(env.container, { items: items, activeTab: 'tasks' });
    scroll = liveQueryOne(env.container, '.topnav__scroll');
    scroll.scrollWidth = 300; scroll.clientWidth = 100;
    env.roInstances[0].trigger();
    leftBtn = liveQueryOne(env.container, 'button[aria-label="向左滚动"]');
    rightBtn = liveQueryOne(env.container, 'button[aria-label="向右滚动"]');
    ok(leftBtn.hidden === false, 'T3 A6 overflow controls shown');
    ok(leftBtn.disabled === true, 'T3 A6 before wheel left disabled (offset 0)');
    // wheel -> recalc
    scroll._listeners.wheel[0]({ deltaY: 50, preventDefault() {}, stopPropagation() {} });
    ok(leftBtn.disabled === false, 'T3 A6 after wheel left enabled (offset>0)');
    // scroll event -> recalc (no throw)
    scroll._listeners.scroll[0]({});
    ok(true, 'T3 A6 scroll event recalc no throw');
    // keyboard: focus + keydown listeners registered and recalc
    let inner = liveQueryOne(env.container, '.topnav__inner');
    ok(inner._listeners.focus && inner._listeners.focus.length >= 1, 'T3 A6 focus listener registered');
    inner._listeners.focus[0]({ type: 'focus', target: liveQuery(env.container, 'a[href]')[0] });
    ok(inner._listeners.keydown && inner._listeners.keydown.length >= 1, 'T3 A6 keydown listener registered');
    let link0 = liveQuery(env.container, 'a[href]')[0];
    inner._listeners.keydown[0]({ key: 'ArrowRight', target: link0, preventDefault() {} });
    ok(true, 'T3 A6 keydown ArrowRight recalc no throw');
    // default motion sets transform
    ok(inner.style._p['transform'] !== undefined, 'T3 A6 default motion sets transform (got ' + inner.style._p['transform'] + ')');
    // reduced motion: no transform, uses scrollLeft
    env = loadTopnav({ reducedMotion: true });
    env.topnav.render(env.container, { items: items, activeTab: 'tasks' });
    scroll = liveQueryOne(env.container, '.topnav__scroll');
    scroll.scrollWidth = 300; scroll.clientWidth = 100;
    env.roInstances[0].trigger();
    inner = liveQueryOne(env.container, '.topnav__inner');
    rightBtn = liveQueryOne(env.container, 'button[aria-label="向右滚动"]');
    rightBtn.click();
    ok(inner.style._p['transform'] === undefined, 'T3 A6 reduced motion: no transform on inner');
    ok(scroll.scrollLeft > 0, 'T3 A6 reduced motion: scrollLeft used (got ' + scroll.scrollLeft + ')');

    // --- T3 A7: long label truncation preserves accessible name; onActivate once; no pushState ---
    env = loadTopnav();
    const longLabel = '这是一个非常非常非常长的子域导航标签文本用于测试视觉截断与可访问名称保留';
    const longItems = [
      { tab: 'tasks', path: '/tasks', label: longLabel },
      { tab: 'tasks-observations', path: '/tasks/observations', label: '观测' },
    ];
    let act7 = 0;
    env.topnav.render(env.container, { items: longItems, activeTab: 'tasks', onActivate: function () { act7++; } });
    let l7 = liveQuery(env.container, 'a[href]');
    ok(l7[0]._text === longLabel, 'T3 A7 long label accessible name preserved (textContent full)');
    ok(l7[0]._cls.has('topnav__item--truncate'), 'T3 A7 long label visual truncation class');
    l7[0].click();
    ok(act7 === 1, 'T3 A7 onActivate called once');
    ok(env.pushStateCalls.length === 0, 'T3 A7 component does not call history.pushState');

    // --- T3 A8: missing fields / activeTab not in items; dev reports, prod degrades ---
    env = loadTopnav({ dev: true });
    env.topnav.render(env.container, {
      items: [
        { tab: 'tasks', path: '/tasks', label: '管理' },
        { tab: 'bad', path: '/bad' },
        'not-an-object',
      ],
      activeTab: 'tasks',
    });
    let l8 = liveQuery(env.container, 'a[href]');
    ok(l8.length === 1, 'T3 A8 dev: invalid items skipped (got ' + l8.length + ')');
    ok(env.consoleErrors.length >= 1, 'T3 A8 dev: config error reported for missing fields');
    // activeTab not in items -> no aria-current, reported
    env = loadTopnav({ dev: true });
    env.topnav.render(env.container, {
      items: [{ tab: 'tasks', path: '/tasks', label: '管理' }],
      activeTab: 'tasks-observations',
    });
    let l8b = liveQuery(env.container, 'a[href]');
    ok(l8b.every((a) => a.getAttribute('aria-current') !== 'page'), 'T3 A8 dev: no aria-current when activeTab not in items');
    ok(env.consoleErrors.length >= 1, 'T3 A8 dev: activeTab-not-in-items config error reported');
    // prod mode: safe degradation, no reports, no throw
    env = loadTopnav({ dev: false });
    env.topnav.render(env.container, {
      items: [
        { tab: 'tasks', path: '/tasks', label: '管理' },
        { tab: 'bad', path: '/bad' },
      ],
      activeTab: 'tasks-observations',
    });
    let l8c = liveQuery(env.container, 'a[href]');
    ok(l8c.length === 1, 'T3 A8 prod: invalid items skipped silently');
    ok(l8c.every((a) => a.getAttribute('aria-current') !== 'page'), 'T3 A8 prod: no aria-current when activeTab not in items');
    ok(env.consoleErrors.length === 0, 'T3 A8 prod: no config error reported (silent degradation)');
  }
})();

// ===========================================================================
// T5: app.js assembly & navigation state passing
// ===========================================================================
async function runT5() {
  if (!appCode) {
    ok(false, 'T5 app.js not loaded');
    return;
  }

  // --- T5 A1: resolveModule('tasks-observations') -> tasksObservations.init ---
  let env = loadApp();
  await env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  ok(env.initCalls.length === 1, 'T5 A1 first activation calls tasksObservations.init (got ' + env.initCalls.length + ')');
  ok(env.refreshCalls.length === 0, 'T5 A1 first activation does not call refresh');

  // --- T5 A2: subsequent activation calls refresh, not init ---
  await env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  ok(env.initCalls.length === 1, 'T5 A2 subsequent activation: init not called again (got ' + env.initCalls.length + ')');
  ok(env.refreshCalls.length === 1, 'T5 A2 subsequent activation calls refresh (got ' + env.refreshCalls.length + ')');

  // --- T5 A2b: leaving task observations clears its transient detail state ---
  await env.app.onTabActivated({ renderTab: 'chat', activeTab: 'chat', currentSubdomain: null, sidebarTab: 'chat', route: null });
  ok(env.deactivateCalls.length === 1, 'T5 A2b leaving task observations calls deactivate');

  // --- T5 A3: TopNav.render called with items, activeTab, onActivate ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  ok(env.renderCalls.length === 1, 'T5 A3 topnav.render called once (got ' + env.renderCalls.length + ')');
  if (env.renderCalls.length > 0) {
    ok(env.renderCalls[0].opts.items === env.NAGENT.navigation.topnavConfig.tasks, 'T5 A3 topnav.render received topnavConfig.tasks items');
    ok(env.renderCalls[0].opts.activeTab === 'tasks-observations', 'T5 A3 topnav.render received activeTab (got ' + env.renderCalls[0].opts.activeTab + ')');
    ok(typeof env.renderCalls[0].opts.onActivate === 'function', 'T5 A3 topnav.render received onActivate callback');
    ok(env.renderCalls[0].container === env.topnavMount, 'T5 A3 topnav.render received topnav-mount container');
  } else {
    ok(false, 'T5 A3 skipped: no topnav.render call');
  }

  // --- T5 A4: onActivate callback calls navigatePath ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  if (env.renderCalls.length > 0) {
    const actItem = { tab: 'tasks', path: '/tasks', label: '管理' };
    env.renderCalls[0].opts.onActivate(actItem);
    ok(env.navigateCalls.length === 1, 'T5 A4 onActivate calls navigatePath (got ' + env.navigateCalls.length + ')');
    ok(env.navigateCalls[0] === '/tasks', 'T5 A4 onActivate calls navigatePath with item.path (got ' + env.navigateCalls[0] + ')');
  } else {
    ok(false, 'T5 A4 skipped: no topnav.render call');
  }

  // --- T5 A5: no currentSubdomain -> destroy + show title ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'chat', activeTab: 'chat', currentSubdomain: null, sidebarTab: 'chat', route: null });
  ok(env.destroyCalls.length === 1, 'T5 A5 no subdomain -> topnav.destroy called (got ' + env.destroyCalls.length + ')');
  ok(env.renderCalls.length === 0, 'T5 A5 no subdomain -> topnav.render not called');
  ok(env.topnavMount.hidden === true, 'T5 A5 no subdomain -> topnav-mount hidden');
  ok(env.topbarTitleWrap.hidden === false, 'T5 A5 no subdomain -> title-wrap visible');

  // --- T5 A6: with currentSubdomain -> hide title, show topnav mount ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  ok(env.topnavMount.hidden === false, 'T5 A6 with subdomain -> topnav-mount visible');
  ok(env.topbarTitleWrap.hidden === true, 'T5 A6 with subdomain -> title-wrap hidden');

  // --- T5 A7: concurrent first init shares in-flight Promise ---
  env = loadApp({ deferInit: true });
  const p1 = env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  const p2 = env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  ok(env.initCalls.length === 1, 'T5 A7 concurrent init: init called once (got ' + env.initCalls.length + ')');
  env.resolveInit();
  await Promise.all([p1, p2]);
  ok(env.initCalls.length === 1, 'T5 A7 after resolve: init still called once (got ' + env.initCalls.length + ')');

  // --- T5 A8: string compat (legacy callers) ---
  env = loadApp();
  await env.app.onTabActivated('tasks-observations');
  ok(env.initCalls.length === 1, 'T5 A8 string input: init called (got ' + env.initCalls.length + ')');
  ok(env.renderCalls.length === 1, 'T5 A8 string input: topnav.render called (got ' + env.renderCalls.length + ')');
  if (env.renderCalls.length > 0) {
    ok(env.renderCalls[0].opts.activeTab === 'tasks-observations', 'T5 A8 string input: activeTab normalized (got ' + env.renderCalls[0].opts.activeTab + ')');
  } else {
    ok(false, 'T5 A8 skipped: no topnav.render call');
  }

  // --- T5 A9: module init error doesn't block nav state ---
  env = loadApp();
  env.NAGENT.tasksObservations.init = async function () { throw new Error('init failed'); };
  await env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
  ok(env.renderCalls.length === 1, 'T5 A9 module error: topnav.render still called (got ' + env.renderCalls.length + ')');
  ok(env.topnavMount.hidden === false, 'T5 A9 module error: topnav-mount still visible');
  const errContainer = env.byId['tab-tasks-observations'];
  if (errContainer && errContainer._kids.length > 0) {
    ok(true, 'T5 A9 module error: error displayed in container');
    ok(errContainer._kids[0]._text.indexOf('模块加载失败') !== -1, 'T5 A9 module error: error message text (got ' + errContainer._kids[0]._text + ')');
  } else {
    ok(false, 'T5 A9 module error: error displayed in container');
  }

  // --- T5 A10: no topnav module -> safe degradation ---
  env = loadApp();
  delete env.NAGENT.topnav;
  try {
    await env.app.onTabActivated({ renderTab: 'tasks-observations', activeTab: 'tasks-observations', currentSubdomain: 'tasks', sidebarTab: 'tasks', route: {} });
    ok(true, 'T5 A10 no topnav module: no throw');
  } catch (e) {
    ok(false, 'T5 A10 no topnav module threw: ' + e.message);
  }
  ok(env.byId['topnav-mount'].hidden === true, 'T5 A10 no topnav module: mount hidden');
  ok(env.byId['topbar-title-wrap'].hidden === false, 'T5 A10 no topnav module: title visible');
}

// ===========================================================================
// T6: tasks-observations.js
// ===========================================================================
async function runT6() {
  if (!toCode) {
    ok(false, 'T6 tasks-observations.js not loaded');
    return;
  }
  const tick = () => new Promise((r) => setTimeout(r, 0));

  // --- T6 A1: init lists sessions, filters by task association (board +
  // source==='task'), renders table; no top header in index view ---
  // Board exposes task execution sessions: a Dashboard /task create task runs
  // in its origin chat session (origin_session_id set, source=dashboard); a
  // kanban/CLI task has neither origin nor execution session (falls back to
  // task-{uuid5}, caught by source==='task' on the session side).
  let env = loadTasksObservations({
    board: async () => ({
      columns: [
        { title: 'todo', cards: [
          { id: 't-dash', origin_session_id: 'dashboard-5400b4b7', execution_session_id: null },
          { id: 't-kanban', origin_session_id: null, execution_session_id: null },
        ] },
      ],
    }),
    listSessions: async () => ({
      items: [
        { session_id: 'task-uuid5-kanban', source: 'task', title: 'Kanban task session', input_tokens: 10, output_tokens: 5 },
        { session_id: 'dashboard-5400b4b7', source: 'dashboard', title: 'Dashboard origin session', input_tokens: 20, output_tokens: 10 },
        { session_id: 'chat-xyz', source: 'chat', title: 'Unrelated chat' },
      ],
      total: 3, page: 1,
    }),
  });
  await env.NAGENT.tasksObservations.init();
  ok(env.apiCalls.board === 1, 'T6 A1 board called once');
  ok(env.apiCalls.listSessions === 1, 'T6 A1 listSessions called once');
  ok(env.apiCalls.getRecords === 0, 'T6 A1 getRecords not called for session filtering');
  let rows = liveQuery(env.container, '[data-session-id]');
  ok(rows.length === 2, 'T6 A1 task-associated sessions rendered (got ' + rows.length + ')');
  // source==='task' session included; dashboard-origin session (matches task
  // origin_session_id) included; unrelated chat session excluded.
  let renderedIds = rows.map((r) => r.dataset.sessionId).sort();
  ok(renderedIds[0] === 'dashboard-5400b4b7', 'T6 A1 dashboard-origin session included (got ' + renderedIds[0] + ')');
  ok(renderedIds[1] === 'task-uuid5-kanban', 'T6 A1 source=task session included (got ' + renderedIds[1] + ')');
  // overview cards rendered (reuses observations.js layout); aggregation is
  // over the filtered task-associated sessions.
  let statCards = liveQuery(env.container, '.stat-card');
  ok(statCards.length >= 8, 'T6 A1 overview cards rendered (got ' + statCards.length + ')');
  // No top header in index view (removed: 全部观测 back link + 任务 · 观测 title).
  ok(liveQueryOne(env.container, '[href="/observations/sessions"]') === null,
    'T6 A1 global observations link removed from task observations');
  ok(liveQueryOne(env.container, '.observations-detail-header__title') === null,
    'T6 A1 task observations heading removed');

  // --- T6 A1b: empty state when no task sessions ---
  env = loadTasksObservations({ listSessions: async () => ({ items: [], total: 0, page: 1 }) });
  await env.NAGENT.tasksObservations.init();
  rows = liveQuery(env.container, '[data-session-id]');
  ok(rows.length === 0, 'T6 A1b empty: no session rows');
  let emptyState = liveQueryOne(env.container, '.empty-state');
  ok(emptyState !== null && emptyState._text.indexOf('暂无会话') !== -1, 'T6 A1b empty state shown');

  // --- T6 A1c: empty state when sessions exist but none are task ---
  env = loadTasksObservations({
    listSessions: async () => ({ items: [{ session_id: 's1', source: 'chat', title: 'Chat' }], total: 1, page: 1 }),
  });
  await env.NAGENT.tasksObservations.init();
  rows = liveQuery(env.container, '[data-session-id]');
  ok(rows.length === 0, 'T6 A1c non-task sessions filtered out');

  // --- T6 A2: detail uses usage API (getStats/getRecords/getCompressions/getBreakdown) ---
  env = loadTasksObservations({
    listSessions: async () => ({ items: [{ session_id: 's1', source: 'task', title: 'T1' }], total: 1, page: 1 }),
    getStats: async () => ({ input_tokens: 100, output_tokens: 50, normalized_tokens: 120, api_call_count: 3 }),
    getRecords: async () => [{ model: 'gpt-4', created_at: '2026-01-01T00:00:00Z', input_tokens: 10 }],
    getCompressions: async () => [],
    getBreakdown: async () => ({ total: 100, system_prompt: 50, tool_definitions: 20, memory: 10, conversation: 20 }),
  });
  await env.NAGENT.tasksObservations.init();
  ok(env.apiCalls.getRecords === 0, 'T6 A2 getRecords not called after init');
  // Select a session via detail button (reuses observations.js sessions table)
  let detailBtn = liveQueryOne(env.container, '[data-action="detail"]');
  ok(detailBtn !== null, 'T6 A2 detail button exists');
  detailBtn.click();
  await tick();
  ok(env.apiCalls.getStats === 1, 'T6 A2 getStats called on select');
  ok(env.apiCalls.getRecords === 1, 'T6 A2 getRecords called on select (detail)');
  ok(env.apiCalls.getCompressions === 1, 'T6 A2 getCompressions called on select');
  ok(env.apiCalls.getBreakdown === 1, 'T6 A2 getBreakdown called on select');

  // --- T6 A3: selecting session renders detail (stats + breakdown + records) ---
  statCards = liveQuery(env.container, '.stat-card');
  ok(statCards.length >= 5, 'T6 A3 detail stats cards rendered (got ' + statCards.length + ')');
  let detailTable = liveQuery(env.container, '.document-table');
  ok(detailTable !== null, 'T6 A3 detail table rendered');

  // --- T6 A3b: selecting session with missing fields does not crash ---
  env = loadTasksObservations({
    listSessions: async () => ({ items: [{ session_id: 's1', source: 'task', title: 'T1' }], total: 1, page: 1 }),
    getStats: async () => ({}),
    getRecords: async () => null,
    getCompressions: async () => null,
    getBreakdown: async () => ({}),
  });
  await env.NAGENT.tasksObservations.init();
  detailBtn = liveQueryOne(env.container, '[data-action="detail"]');
  detailBtn.click();
  await tick();
  ok(env.apiCalls.getStats === 1, 'T6 A3b getStats called');
  ok(env.apiCalls.getRecords === 1, 'T6 A3b getRecords called');
  statCards = liveQuery(env.container, '.stat-card');
  ok(statCards.length >= 5, 'T6 A3b detail rendered despite missing fields');
  let emptyRecords = liveQueryOne(env.container, '.empty-state');
  ok(emptyRecords !== null, 'T6 A3b empty records state shown');

  // --- T6 A4: async guard -- switch away discards stale response ---
  let resolveList;
  env = loadTasksObservations({
    listSessions: () => new Promise((r) => { resolveList = r; }),
  });
  const initP = env.NAGENT.tasksObservations.init();
  // Switch away before response arrives
  env.container._cls.delete('active');
  resolveList({ items: [{ session_id: 's1', source: 'task', title: 'Stale' }], total: 1, page: 1 });
  await initP;
  await tick();
  rows = liveQuery(env.container, '[data-session-id]');
  ok(rows.length === 0, 'T6 A4 stale response discarded (no session rows)');
  let loadingState = liveQueryOne(env.container, '.loading-state');
  ok(loadingState !== null, 'T6 A4 loading state remains (stale response did not overwrite)');

  // --- T6 A5: all text via textContent (no innerHTML) ---
  ok(toCode.indexOf('innerHTML') === -1, 'T6 A5 no innerHTML in source');
  ok(toCode.indexOf('insertAdjacentHTML') === -1, 'T6 A5 no insertAdjacentHTML in source');
  ok(toCode.indexOf('.textContent') !== -1, 'T6 A5 textContent used for rendering');

  // --- T6 A6: listSessions failure -> retryable error; retry no listener accumulation ---
  env = loadTasksObservations({
    listSessions: async () => { throw new Error('network'); },
  });
  await env.NAGENT.tasksObservations.init();
  ok(env.apiCalls.listSessions === 1, 'T6 A6 listSessions called (failed)');
  let errorState = liveQueryOne(env.container, '.error-state');
  ok(errorState !== null && errorState._text.indexOf('加载任务会话失败') !== -1, 'T6 A6 error state shown');
  let retryBtn = liveQueryOne(env.container, '[data-action="retry"]');
  ok(retryBtn !== null, 'T6 A6 retry button shown');
  ok(liveQueryOne(env.container, '[href="/observations/sessions"]') === null,
    'T6 A6 error view keeps global-observations link removed');
  // Fix mock and retry
  env.setListSessions(async () => ({ items: [{ session_id: 's1', source: 'task', title: 'T1' }], total: 1, page: 1 }));
  retryBtn.click();
  await tick();
  ok(env.apiCalls.listSessions === 2, 'T6 A6 retry calls listSessions again');
  rows = liveQuery(env.container, '[data-session-id]');
  ok(rows.length === 1, 'T6 A6 retry renders session list');
  // Old retry button removed (no duplicate)
  let retryBtns = liveQuery(env.container, '[data-action="retry"]');
  ok(retryBtns.length === 0, 'T6 A6 no duplicate retry buttons after re-render');

  // --- T6 A6b: detail failure -> retryable error in container ---
  env = loadTasksObservations({
    listSessions: async () => ({ items: [{ session_id: 's1', source: 'task', title: 'T1' }], total: 1, page: 1 }),
    getStats: async () => { throw new Error('detail-fail'); },
    getRecords: async () => [],
    getCompressions: async () => [],
    getBreakdown: async () => ({}),
  });
  await env.NAGENT.tasksObservations.init();
  detailBtn = liveQueryOne(env.container, '[data-action="detail"]');
  detailBtn.click();
  await tick();
  errorState = liveQueryOne(env.container, '.error-state');
  ok(errorState !== null && errorState._text.indexOf('加载会话观测失败') !== -1, 'T6 A6b detail error state shown');
  retryBtn = liveQueryOne(env.container, '[data-action="retry"]');
  ok(retryBtn !== null, 'T6 A6b retry button shown for detail error');
  // Retry: fix mock, click retry -> refresh re-fetches detail
  env.setGetStats(async () => ({ input_tokens: 10 }));
  retryBtn.click();
  await tick();
  statCards = liveQuery(env.container, '.stat-card');
  ok(statCards.length >= 5, 'T6 A6b retry renders detail after fix');

  // --- T6 A7: in-flight guard -- repeat init/refresh does not duplicate request ---
  env = loadTasksObservations({
    listSessions: () => new Promise((r) => { resolveList = r; }),
  });
  const p1 = env.NAGENT.tasksObservations.init();
  const p2 = env.NAGENT.tasksObservations.init();
  ok(env.apiCalls.listSessions === 1, 'T6 A7 duplicate init: listSessions called once (got ' + env.apiCalls.listSessions + ')');
  resolveList({ items: [], total: 0, page: 1 });
  await Promise.all([p1, p2]);
  ok(env.apiCalls.listSessions === 1, 'T6 A7 after resolve: listSessions still once');

  // --- T6 A7b: refresh during in-flight init reuses promise ---
  env = loadTasksObservations({
    listSessions: () => new Promise((r) => { resolveList = r; }),
  });
  const initP2 = env.NAGENT.tasksObservations.init();
  const refreshP = env.NAGENT.tasksObservations.refresh();
  ok(env.apiCalls.listSessions === 1, 'T6 A7b refresh during in-flight init: no duplicate (got ' + env.apiCalls.listSessions + ')');
  resolveList({ items: [{ session_id: 's1', source: 'task', title: 'T1' }], total: 1, page: 1 });
  await Promise.all([initP2, refreshP]);
  ok(env.apiCalls.listSessions === 1, 'T6 A7b after resolve: still one call');

  // --- T6 A7c: switch away then back -> stale discarded, refresh re-fetches ---
  let listCallCount = 0;
  env = loadTasksObservations({
    listSessions: () => {
      listCallCount++;
      if (listCallCount === 1) {
        return new Promise((r) => { resolveList = r; });
      }
      return Promise.resolve({ items: [{ session_id: 's1', source: 'task', title: 'Fresh' }], total: 1, page: 1 });
    },
  });
  const staleP = env.NAGENT.tasksObservations.init();
  env.container._cls.delete('active');
  resolveList({ items: [{ session_id: 's1', source: 'task', title: 'Stale' }], total: 1, page: 1 });
  await staleP;
  await tick();
  ok(env.apiCalls.listSessions === 1, 'T6 A7c first call completed');
  rows = liveQuery(env.container, '[data-session-id]');
  ok(rows.length === 0, 'T6 A7c stale response discarded after switch away');
  // Switch back and refresh
  env.container._cls.add('active');
  await env.NAGENT.tasksObservations.refresh();
  ok(env.apiCalls.listSessions === 2, 'T6 A7c refresh after switch back: new request (got ' + env.apiCalls.listSessions + ')');
  rows = liveQuery(env.container, '[data-session-id]');
  ok(rows.length === 1, 'T6 A7c session list rendered after refresh');

  // --- T6 A8: index view keeps global-observations link removed ---
  env = loadTasksObservations({
    listSessions: async () => ({ items: [{ session_id: 's1', source: 'task', title: 'T1' }], total: 1, page: 1 }),
  });
  await env.NAGENT.tasksObservations.init();
  ok(liveQueryOne(env.container, '[href="/observations/sessions"]') === null,
    'T6 A8 global-observations link remains absent after rerender');
}

// ===========================================================================
// T7: observations.js top controls removal + async guard + popstate removal
// ===========================================================================
async function runT7() {
  if (!obsCode) {
    ok(false, 'T7 observations.js not loaded');
    return;
  }
  const tick = () => new Promise((r) => setTimeout(r, 0));

  // --- T7 A1: index view does not render the retired 全部/任务 controls ---
  let env = loadObservations({
    getOverview: async () => ({ session_count: 5, input_tokens: 100 }),
    listSessions: async () => ({ items: [], total: 0, page: 1 }),
  });
  await env.NAGENT.observations.init();
  let scopeBar = liveQueryOne(env.container, '.observations-scope');
  let chips = liveQuery(env.container, '.observations-scope__chip');
  ok(scopeBar === null, 'T7 A1 scope bar is not rendered in index view');
  ok(chips.length === 0, 'T7 A1 no 全部/任务 scope chips (got ' + chips.length + ')');
  ok(liveQueryOne(env.container, '[data-scope="all"]') === null, 'T7 A1 全部 control removed');
  ok(liveQueryOne(env.container, '[data-scope="tasks"]') === null, 'T7 A1 任务 control removed');

  // --- T7 A2: no retired controls in detail view either ---
  env = loadObservations({
    pathname: '/observations/sessions/sess_1',
    getStats: async () => ({ input_tokens: 10 }),
    getRecords: async () => [],
    getCompressions: async () => [],
    getBreakdown: async () => ({}),
  });
  await env.NAGENT.observations.init();
  scopeBar = liveQueryOne(env.container, '.observations-scope');
  ok(scopeBar === null, 'T7 A2 scope bar not rendered in detail view');
  let detailHeader = liveQueryOne(env.container, '.observations-detail-header');
  ok(detailHeader !== null, 'T7 A3 detail header rendered (detail view)');

  // --- T7 A3: in-flight guard -- switching views discards stale response ---
  let resolveOverview;
  env = loadObservations({
    getOverview: () => new Promise((r) => { resolveOverview = r; }),
    listSessions: async () => ({ items: [], total: 0, page: 1 }),
  });
  const initP = env.NAGENT.observations.init();
  // Switch away; the observations tab loses "active".
  env.container._cls.delete('active');
  resolveOverview({ session_count: 99, input_tokens: 999 });
  await initP;
  await tick();
  // Stale response must NOT have written overview cards
  let statCards = liveQuery(env.container, '.stat-card');
  ok(statCards.length === 0, 'T7 A3 stale response discarded: no stat-cards written (got ' + statCards.length + ')');
  let loadingState = liveQueryOne(env.container, '.loading-state');
  ok(loadingState !== null, 'T7 A3 loading state remains (stale response did not overwrite DOM)');

  // --- T7 A4: when not active, refresh does not fetch or write DOM ---
  env = loadObservations({
    getOverview: async () => ({ session_count: 1 }),
    listSessions: async () => ({ items: [], total: 0, page: 1 }),
  });
  await env.NAGENT.observations.init();
  ok(env.apiCalls.getOverview === 1, 'T7 A4 init fetched overview once');
  // Switch away
  env.container._cls.delete('active');
  env.apiCalls.getOverview = 0;
  env.apiCalls.listSessions = 0;
  const beforeKids = env.container._kids.length;
  await env.NAGENT.observations.refresh();
  ok(env.apiCalls.getOverview === 0, 'T7 A4 refresh when not active: no overview fetch (got ' + env.apiCalls.getOverview + ')');
  ok(env.apiCalls.listSessions === 0, 'T7 A4 refresh when not active: no listSessions fetch (got ' + env.apiCalls.listSessions + ')');
  ok(env.container._kids.length === beforeKids, 'T7 A4 refresh when not active: DOM unchanged (no write)');

  // --- T7 A5: repeated init/refresh/back-forward doesn't increase popstate ---
  env = loadObservations({
    getOverview: async () => ({}),
    listSessions: async () => ({ items: [], total: 0, page: 1 }),
  });
  await env.NAGENT.observations.init();
  let popstateCount = (env.win._listeners.popstate || []).length;
  ok(popstateCount === 0, 'T7 A5 init registers 0 popstate listeners (got ' + popstateCount + ')');
  await env.NAGENT.observations.refresh();
  await env.NAGENT.observations.refresh();
  // simulate back-forward (popstate -> navigation layer -> refresh)
  await env.NAGENT.observations.refresh();
  popstateCount = (env.win._listeners.popstate || []).length;
  ok(popstateCount === 0, 'T7 A5 after repeated refresh: still 0 popstate listeners (got ' + popstateCount + ')');
  chips = liveQuery(env.container, '.observations-scope__chip');
  ok(chips.length === 0, 'T7 A5 scope chips remain absent after repeated render (got ' + chips.length + ')');
  // source-level: observations.js does not self-register popstate (navigation layer owns it)
  ok(obsCode.indexOf('popstate') === -1, 'T7 A5 observations.js source has no popstate reference');

  // --- T7 A7: left nav / submenu not modified ---
  env = loadObservations({
    getOverview: async () => ({}),
    listSessions: async () => ({ items: [], total: 0, page: 1 }),
  });
  // Create sidebar elements (outside observations container, like real sidebar)
  const sidebarItem = makeEl('a');
  sidebarItem._cls.add('sidebar__item');
  sidebarItem.dataset.tab = 'observations-sessions';
  const submenu = makeEl('div');
  submenu._cls.add('sidebar__submenu');
  submenu.dataset.submenuOf = 'observations';
  const sidebarKidsBefore = sidebarItem._kids.length;
  const submenuKidsBefore = submenu._kids.length;
  const sidebarClsBefore = Array.from(sidebarItem._cls).join(',');
  const submenuClsBefore = Array.from(submenu._cls).join(',');
  await env.NAGENT.observations.init();
  await env.NAGENT.observations.refresh();
  ok(sidebarItem._kids.length === sidebarKidsBefore, 'T7 A7 sidebar item children unchanged');
  ok(submenu._kids.length === submenuKidsBefore, 'T7 A7 submenu children unchanged');
  ok(Array.from(sidebarItem._cls).join(',') === sidebarClsBefore, 'T7 A7 sidebar item classes unchanged');
  ok(Array.from(submenu._cls).join(',') === submenuClsBefore, 'T7 A7 submenu classes unchanged');
  // source-level: observations.js does not touch sidebar DOM
  ok(obsCode.indexOf('sidebar') === -1, 'T7 A7 observations.js source does not reference sidebar');

  // --- T7 A8: safe rendering (no insertAdjacentHTML, no native alert) ---
  ok(obsCode.indexOf('insertAdjacentHTML') === -1, 'T7 A8 no insertAdjacentHTML in observations.js');
  ok(!/[^\w.]alert\s*\(/.test(obsCode), 'T7 A8 no native alert() in observations.js');
  ok(obsCode.indexOf('observations-scope') === -1, 'T7 A8 observations.js contains no retired scope control');
}

// --- Runner ---
Promise.resolve()
  .then(() => runT5())
  .then(() => runT6())
  .then(() => runT7())
  .then(() => {
    if (failures) {
      console.error('\n' + failures + ' test(s) failed');
      process.exit(1);
    }
    console.log('topnav_frontend_harness: all tests passed');
    process.exit(0);
  })
  .catch((e) => {
    console.error('harness error: ' + (e && e.message ? e.message : String(e)));
    process.exit(1);
  });
