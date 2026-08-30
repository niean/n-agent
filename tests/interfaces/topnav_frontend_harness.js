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
    'observations-sessions', 'observations-modules', 'security',
    'security-overview', 'security-sessions', 'security-memory', 'security-sandbox'];
  tabIds.forEach((id) => {
    const el = makeEl('div');
    el._cls.add('tab-content');
    el.id = 'tab-' + id;
  });

  const sidebarTabs = ['summary', 'chat', 'tasks', 'scheduled-tasks', 'sessions', 'memory',
    'tools', 'tools-knowledge', 'tools-mcp', 'tools-skill', 'tools-plugin', 'tools-builtin',
    'executors', 'sandbox', 'executors-host', 'models', 'platforms',
    'observations', 'observations-sessions', 'observations-modules'];
  sidebarTabs.forEach((tab) => {
    const item = makeEl('a');
    item._cls.add('sidebar__item');
    item.dataset.tab = tab;
    if (['tools', 'executors', 'observations', 'security'].indexOf(tab) !== -1) {
      item._cls.add('sidebar__item--parent');
      const submenu = makeEl('div');
      submenu._cls.add('sidebar__submenu');
      submenu.dataset.submenuOf = tab;
    }
  });
  // Security mirrors the production parent -> submenu -> child hierarchy,
  // while the other legacy mock items remain flat for their existing tests.
  const securityGroup = makeEl('div');
  securityGroup.dataset.tabGroup = 'security';
  const securityParent = makeEl('button');
  securityParent.type = 'button';
  securityParent._cls.add('sidebar__item');
  securityParent._cls.add('sidebar__item--parent');
  securityParent.dataset.tab = 'security';
  securityParent.setAttribute('aria-expanded', 'false');
  const securitySubmenu = makeEl('div');
  securitySubmenu._cls.add('sidebar__submenu');
  securitySubmenu.dataset.submenuOf = 'security';
  securityGroup.append(securityParent, securitySubmenu);
  [
    ['security-overview', '/security'],
    ['security-sessions', '/security/sessions'],
    ['security-memory', '/security/memory'],
    ['security-sandbox', '/security/sandbox'],
  ].forEach(([tab, href]) => {
    const child = makeEl('a');
    child._cls.add('sidebar__item');
    child._cls.add('sidebar__item--child');
    child.dataset.tab = tab;
    child.setAttribute('href', href);
    securitySubmenu.appendChild(child);
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
    URL,
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
// T2 Assertion 4: resolveRoute('/sessions') -> currentSubdomain:'sessions'
// ===========================================================================
state = nav.resolveRoute('/sessions');
ok(state.activeTab === 'sessions', 'A4 activeTab (got ' + state.activeTab + ')');
ok(state.currentSubdomain === 'sessions', 'A4 currentSubdomain (got ' + state.currentSubdomain + ')');

// ===========================================================================
// T1: security is a parent subdomain with four independently routable scopes.
// ===========================================================================
const securityRoutes = [
  ['/security', 'overview'],
  ['/security/sessions', 'sessions'],
  ['/security/memory', 'memory'],
  ['/security/sandbox', 'sandbox'],
];
securityRoutes.forEach(([path, scope]) => {
  const s = nav.resolveRoute(path);
  const child = 'security-' + scope;
  ok(s.activeTab === child, 'security ' + path + ' activeTab === ' + child);
  ok(s.renderTab === 'security', 'security ' + path + ' renderTab === security');
  ok(s.sidebarTab === child, 'security ' + path + ' sidebarTab === ' + child);
  ok(s.currentSubdomain === 'security', 'security ' + path + ' currentSubdomain === security');
  ok(s.route && s.route.scope === scope, 'security ' + path + ' route.scope === ' + scope);
});
ok(nav.DEFAULT_CHILD && nav.DEFAULT_CHILD.security === 'security-overview',
  'security DEFAULT_CHILD is security-overview');
['overview', 'sessions', 'memory', 'sandbox'].forEach((scope) => {
  const tab = 'security-' + scope;
  const items = nav.topnavConfig[tab];
  const expectedPath = scope === 'overview' ? '/security' : '/security/' + scope;
  ok(items && items.length === 1, tab + ' private topnav has exactly one own item');
  ok(items && items[0] && items[0].tab === tab, tab + ' private topnav item owns its tab');
  ok(items && items[0] && items[0].path === expectedPath, tab + ' private topnav item has canonical path');
});
// The security parent opens its submenu by default when no preference exists.
nav.applyRoute(nav.resolveRoute('/security'));
const securitySubmenu = env.doc.querySelector('[data-submenu-of="security"]');
ok(securitySubmenu && securitySubmenu.classList.contains('sidebar__submenu--open'),
  'security submenu opens when no preference exists');

// ===========================================================================
// T2 Assertion 4c: /sessions topnav config has 2 items (管理 | 观测), /observations
// topnav config has 1 item (观测 only). The new sessions[1] reuses the
// observations-sessions tab but uses the parent-subdomain scope pattern
// (topnavParent/scope === 'sessions') and points to the independent
// /sessions/observations entry (NOT /observations/sessions, which is the
// canonical observations-subdomain entry).
// ===========================================================================
ok(nav.topnavConfig.sessions && nav.topnavConfig.sessions.length === 3,
  'A4c topnavConfig.sessions length === 3 (got ' + (nav.topnavConfig.sessions && nav.topnavConfig.sessions.length) + ')');
ok(nav.topnavConfig.sessions[0].tab === 'sessions', 'A4c sessions[0].tab === sessions');
ok(nav.topnavConfig.sessions[0].label === '管理', 'A4c sessions[0].label === 管理');
ok(nav.topnavConfig.sessions[0].path === '/sessions', 'A4c sessions[0].path === /sessions');
ok(nav.topnavConfig.sessions[0].topnavParent === 'sessions', 'A4c sessions[0].topnavParent === sessions');
ok(nav.topnavConfig.sessions[0].scope === 'sessions', 'A4c sessions[0].scope === sessions');
const sessionObservation = nav.topnavConfig.sessions && nav.topnavConfig.sessions[1];
const expectedSessionObservation = {
  tab: 'observations-sessions',
  path: '/sessions/observations',
  label: '观测',
  concern: 'observation',
  scope: 'sessions',
  topnavParent: 'sessions',
};
ok(!!sessionObservation, 'A4c sessions[1] exists');
ok(sessionObservation && JSON.stringify(sessionObservation) === JSON.stringify(expectedSessionObservation),
  'A4c sessions[1] deep-equals {tab, path, label, concern, scope, topnavParent} (got ' + JSON.stringify(sessionObservation) + ')');
ok(sessionObservation && sessionObservation.path !== nav.topnavConfig.observations[0].path,
  'A4c sessions[1].path !== observations[0].path (independent entry, not alias)');
const sessionSecurity = nav.topnavConfig.sessions && nav.topnavConfig.sessions[2];
const expectedSessionSecurity = {
  tab: 'security-sessions',
  path: '/sessions/security',
  label: '安全',
  concern: 'security',
  scope: 'sessions',
  topnavParent: 'sessions',
};
ok(!!sessionSecurity, 'A4c sessions[2] exists');
ok(sessionSecurity && JSON.stringify(sessionSecurity) === JSON.stringify(expectedSessionSecurity),
  'A4c sessions[2] deep-equals {tab, path, label, concern, scope, topnavParent} (got ' + JSON.stringify(sessionSecurity) + ')');
ok(sessionSecurity && sessionSecurity.path !== nav.topnavConfig['security-sessions'][0].path,
  'A4c sessions[2].path !== security-sessions[0].path (independent entry, not alias)');
ok(nav.topnavConfig.observations && nav.topnavConfig.observations.length === 1,
  'A4c topnavConfig.observations length === 1 (got ' + (nav.topnavConfig.observations && nav.topnavConfig.observations.length) + ')');
ok(nav.topnavConfig.observations && nav.topnavConfig.observations[0] && nav.topnavConfig.observations[0].tab === 'observations-sessions', 'A4c observations[0].tab === observations-sessions');
ok(nav.topnavConfig.observations && nav.topnavConfig.observations[0] && nav.topnavConfig.observations[0].label === '会话', 'A4c observations[0].label === 会话');
ok(nav.topnavConfig.observations && nav.topnavConfig.observations[0] && nav.topnavConfig.observations[0].path === '/observations/sessions', 'A4c observations[0].path === /observations/sessions (canonical)');
ok(nav.topnavConfig.observations && nav.topnavConfig.observations[0] && nav.topnavConfig.observations[0].topnavParent === 'observations', 'A4c observations[0].topnavParent === observations');
ok(nav.topnavConfig.observations && nav.topnavConfig.observations[0] && nav.topnavConfig.observations[0].scope === 'observations', 'A4c observations[0].scope === observations');
ok(nav.topnavConfig.observations && nav.topnavConfig.observations[0] && nav.topnavConfig.observations[0].concern === 'observation', 'A4c observations[0].concern === observation');
const expectedComponentTopnav = {
  tab: 'observations-modules',
  path: '/observations/modules',
  label: '组件',
  concern: 'observation',
  scope: 'observations-modules',
  topnavParent: 'observations-modules',
};
ok(nav.topnavConfig['observations-modules'] && nav.topnavConfig['observations-modules'].length === 1,
  'A4c component private topnav has exactly one item');
ok(nav.topnavConfig['observations-modules']
  && JSON.stringify(nav.topnavConfig['observations-modules'][0]) === JSON.stringify(expectedComponentTopnav),
  'A4c component private topnav item matches the complete contract');
ok(nav.topnavConfig.observations.length === 1
  && nav.topnavConfig.observations[0].tab === 'observations-sessions',
  'A4c observations topnav remains session-only');

// ===========================================================================
// T2 Assertion 4a: /sessions/observations and /observations/sessions are
// independent routeConfig entries to the same renderer (会话观测 page).
// /sessions/observations is OWNED by sessions subdomain (topnav 管理|观测,
// leftnav 会话). /observations/sessions is OWNED by observations subdomain
// (topnav 观测, leftnav 观测-会话). Both share renderer via renderTab.
// ===========================================================================
state = nav.resolveRoute('/sessions/observations');
ok(state.activeTab === 'observations-sessions', 'A4a /sessions/observations activeTab');
ok(state.renderTab === 'observations-sessions', 'A4a /sessions/observations renderTab');
ok(state.sidebarTab === 'sessions', 'A4a /sessions/observations sidebarTab');
ok(state.currentSubdomain === 'sessions', 'A4a /sessions/observations currentSubdomain');
state = nav.resolveRoute('/observations/sessions');
ok(state.activeTab === 'observations-sessions', 'A4a /observations/sessions activeTab');
ok(state.renderTab === 'observations-sessions', 'A4a /observations/sessions renderTab');
ok(state.sidebarTab === 'observations-sessions', 'A4a /observations/sessions sidebarTab');
ok(state.currentSubdomain === 'observations', 'A4a /observations/sessions currentSubdomain');
// The two routes are independent entries (not sharing one entry)
state = nav.resolveRoute('/sessions/observations');
const sessionsObsRoute = state.route;
state = nav.resolveRoute('/observations/sessions');
const observationsRoute = state.route;
ok(sessionsObsRoute !== null && observationsRoute !== null, 'A4a both routes are split entries (non-null)');
ok(sessionsObsRoute !== observationsRoute, 'A4a /sessions/observations and /observations/sessions are independent routeConfig entries');

// ===========================================================================
// T2 Assertion 4d: /sessions/observations is owned by sessions subdomain
// (topnavParent='sessions', scope='sessions', sidebarTab='sessions').
// Renderer (observations.js) is shared via renderTab='observations-sessions'.
// ===========================================================================
state = nav.resolveRoute('/sessions/observations');
ok(state.route !== null, 'A4d /sessions/observations route is split into own entry (non-null)');
ok(state.route.topnavParent === 'sessions', 'A4d route.topnavParent === sessions (independent entry, owned by sessions)');
ok(state.route.scope === 'sessions', 'A4d route.scope === sessions');
ok(state.route.sidebarTab === 'sessions', 'A4d route.sidebarTab === sessions (leftnav 会话)');
ok(state.route.renderTab === 'observations-sessions', 'A4d route.renderTab === observations-sessions (shared renderer)');
ok(state.route.tab === 'observations-sessions', 'A4d route.tab === observations-sessions');

// ===========================================================================
// T2 Assertion 4g: /sessions/security is owned by sessions subdomain
// (topnavParent='sessions', scope='sessions', sidebarTab='sessions').
// Renderer (security.js) is shared via renderTab='security'.
// Independent from /security/sessions entry (different routeConfig row).
// ===========================================================================
state = nav.resolveRoute('/sessions/security');
ok(state.activeTab === 'security-sessions', 'A4g /sessions/security activeTab (got ' + state.activeTab + ')');
ok(state.renderTab === 'security', 'A4g /sessions/security renderTab (got ' + state.renderTab + ')');
ok(state.sidebarTab === 'sessions', 'A4g /sessions/security sidebarTab (got ' + state.sidebarTab + ')');
ok(state.currentSubdomain === 'sessions', 'A4g /sessions/security currentSubdomain (got ' + state.currentSubdomain + ')');
ok(state.route !== null, 'A4g /sessions/security route non-null');
ok(state.route.scope === 'sessions', 'A4g /sessions/security route.scope');
ok(state.route.topnavParent === 'sessions', 'A4g /sessions/security route.topnavParent');
ok(state.route.paths.length === 1 && state.route.paths[0] === '/sessions/security',
  'A4g /sessions/security route.paths');
// /sessions/security and /security/sessions are independent routeConfig entries
const securitySessionsState = nav.resolveRoute('/security/sessions');
ok(state.route !== securitySessionsState.route,
  'A4g /sessions/security and /security/sessions are independent routeConfig entries');
// buildRouteByPath accepts the new path
const routeMap = nav.buildRouteByPath([state.route]);
ok(routeMap['/sessions/security'] === state.route,
  'A4g buildRouteByPath maps /sessions/security to the same route entry');

// ===========================================================================
// T2 Assertion 4e: /observations/sessions sidebarTab comes from routeConfig.
// ===========================================================================
state = nav.resolveRoute('/observations/sessions');
ok(state.route !== null, 'A4e /observations/sessions route is split into own entry (non-null)');
ok(state.route.topnavParent === 'observations', 'A4e route.topnavParent === observations');
ok(state.route.scope === 'observations', 'A4e route.scope === observations');
ok(state.route.sidebarTab === 'observations-sessions', 'A4e route.sidebarTab === observations-sessions (explicit)');
ok(state.route.renderTab === 'observations-sessions', 'A4e route.renderTab === observations-sessions (shared renderer)');
ok(state.route.tab === 'observations-sessions', 'A4e route.tab === observations-sessions');

// ===========================================================================
// T2 Assertion 4f: sidebarOverride no longer contains /sessions/observations.
// Only /observations/tasks (untouched) remains for backward compatibility.
// ===========================================================================
// sidebarOverride is module-private; verify via behavior: /observations/tasks
// still gets its alias sidebar override (observations-sessions), proving
// sidebarOverride is still consulted where intended.
state = nav.resolveRoute('/observations/tasks');
ok(state.sidebarTab === 'observations-sessions', 'A4f /observations/tasks sidebarTab from sidebarOverride (preserved)');
// And the inverse: /sessions/observations sidebarTab must come from routeConfig
// (not sidebarOverride). Already covered by A4d; this is the regression check.
state = nav.resolveRoute('/sessions/observations');
ok(state.sidebarTab === 'sessions' && state.route.sidebarTab === 'sessions',
  'A4f /sessions/observations sidebarTab NOT from sidebarOverride (routeConfig owns it)');

// ===========================================================================
// T2 Assertion 4g: /observations/modules owns a private topnav scope, while
// /observations/sessions/{id} keeps the existing observations fallback.
// ===========================================================================
state = nav.resolveRoute('/observations/modules');
ok(state.route !== null, 'A_obs_modules /observations/modules routeConfig entry exists (replaces A4g fallback)');
ok(state.currentSubdomain === 'observations-modules', 'A_obs_modules currentSubdomain === observations-modules (private topnav scope)');
ok(state.activeTab === 'observations-modules', 'A_obs_modules activeTab === observations-modules');
ok(state.renderTab === 'observations-modules', 'A_obs_modules renderTab === observations-modules (status renderer)');
ok(state.sidebarTab === 'observations-modules', 'A_obs_modules sidebarTab === observations-modules (leftnav highlight)');
if (state.route) {
  ok(state.route.topnavParent === 'observations-modules', 'A_obs_modules route.topnavParent');
  ok(state.route.scope === 'observations-modules', 'A_obs_modules route.scope');
  ok(state.route.tab === 'observations-modules', 'A_obs_modules route.tab');
  ok(state.route.paths.length === 1 && state.route.paths[0] === '/observations/modules',
    'A_obs_modules route.paths is the single component path');
  ok(nav.buildRouteByPath([state.route])['/observations/modules'] === state.route,
    'A_obs_modules buildRouteByPath accepts /observations/modules and maps the same entry');
}

state = nav.resolveRoute('/observations/sessions/sess_1');
ok(state.activeTab === 'observations-sessions', 'A4g /observations/sessions/{id} activeTab');
ok(state.route === null, 'A4g /observations/sessions/{id} route null (no entry)');
ok(state.currentSubdomain === 'observations', 'A4g /observations/sessions/{id} currentSubdomain === observations (parentByChild)');
ok(state.sidebarTab === 'observations-sessions', 'A4g /observations/sessions/{id} sidebarTab');

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
// 6b2: missing scope
try {
  nav.buildRouteByPath([
    { paths: ['/a'], tab: 'x', renderTab: 'x', sidebarTab: 'x', topnavParent: 'x' },
  ]);
  ok(false, 'A6b2 missing scope should throw');
} catch (e) { ok(true, 'A6b2 missing scope throws'); }
// 6b3: empty paths
try {
  nav.buildRouteByPath([
    { paths: [], tab: 'x', renderTab: 'x', sidebarTab: 'x', topnavParent: 'x', scope: 'x' },
  ]);
  ok(false, 'A6b3 empty paths should throw');
} catch (e) { ok(true, 'A6b3 empty paths throws'); }
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

const securityNav = loadNavigation({ pathname: '/security' });
const securityStates = [];
securityNav.win.NAGENT.app = { onTabActivated(s) { securityStates.push(s); } };
securityNav.nav.navigatePath('/security/memory');
const memoryState = securityStates[0];
ok(memoryState && memoryState.activeTab === 'security-memory', 'security navigatePath preserves memory active child');
ok(memoryState && memoryState.renderTab === 'security', 'security navigatePath renders security module');
ok(memoryState && memoryState.route && memoryState.route.scope === 'memory', 'security navigatePath preserves memory scope');
ok(securityNav.byId['tab-security'] && securityNav.byId['tab-security'].classList.contains('active'),
  'security navigatePath activates shared security container');

const componentNav = loadNavigation({ pathname: '/chat' });
const componentStates = [];
componentNav.win.NAGENT.app = {
  onTabActivated(state2) { componentStates.push(state2); },
};
componentNav.nav.navigatePath('/observations/modules');
const componentState = componentStates[0];
ok(componentState && componentState.activeTab === 'observations-modules', 'component navigatePath activeTab');
ok(componentState && componentState.currentSubdomain === 'observations-modules', 'component navigatePath private subdomain');
ok(componentState && componentState.sidebarTab === 'observations-modules', 'component navigatePath sidebarTab');
ok(componentState && componentState.renderTab === 'observations-modules', 'component navigatePath renderTab');
ok(componentState && componentState.route !== null, 'component navigatePath route entry');
ok(componentNav.byId['tab-observations-modules'].classList.contains('active'),
  'component navigatePath activates #tab-observations-modules');
ok(componentNav.pushStateCalls.length === 1, 'component navigatePath pushes exactly once');
ok(componentNav.pushStateCalls[0] && componentNav.pushStateCalls[0].url === '/observations/modules',
  'component navigatePath pushes /observations/modules');

// ===========================================================================
// T2 Extra: popstate -> /sessions/observations resolves to sessions subdomain
// (proves initial direct load and browser back/forward reuse the independent
// /sessions/observations route entry, owned by sessions subdomain).
// ===========================================================================
const nav3 = loadNavigation({ pathname: '/sessions' });
const popstateStates = [];
nav3.win.NAGENT.app = {
  onTabActivated(state) { popstateStates.push(state); },
};
nav3.nav.initNavigation();
ok(popstateStates.length >= 1, 'initNavigation: onTabActivated invoked with initial state (got ' + popstateStates.length + ')');
const initState = popstateStates[0];
ok(initState && initState.currentSubdomain === 'sessions',
  'initNavigation initial /sessions state.currentSubdomain === sessions (got ' + (initState && initState.currentSubdomain) + ')');
ok(initState && initState.activeTab === 'sessions',
  'initNavigation initial /sessions state.activeTab === sessions (got ' + (initState && initState.activeTab) + ')');
ok(nav3.pushStateCalls.length === 0, 'initNavigation: no pushState when initial path matches location (got ' + nav3.pushStateCalls.length + ')');
const popstateListeners = nav3.win._listeners.popstate || [];
ok(popstateListeners.length >= 1, 'initNavigation registered popstate listener (got ' + popstateListeners.length + ')');
// Simulate browser back/forward to /sessions/observations (independent entry)
nav3.win.location.pathname = '/sessions/observations';
popstateListeners.forEach((fn) => fn({ path: '/sessions/observations' }));
const lastState = popstateStates[popstateStates.length - 1];
ok(!!lastState, 'popstate -> app.onTabActivated received state');
ok(lastState && lastState.activeTab === 'observations-sessions',
  'popstate /sessions/observations activeTab === observations-sessions (got ' + (lastState && lastState.activeTab) + ')');
ok(lastState && lastState.renderTab === 'observations-sessions',
  'popstate /sessions/observations renderTab === observations-sessions (got ' + (lastState && lastState.renderTab) + ')');
ok(lastState && lastState.sidebarTab === 'sessions',
  'popstate /sessions/observations sidebarTab === sessions (got ' + (lastState && lastState.sidebarTab) + ')');
ok(lastState && lastState.currentSubdomain === 'sessions',
  'popstate /sessions/observations currentSubdomain === sessions (independent entry, got ' + (lastState && lastState.currentSubdomain) + ')');
ok(nav3.pushStateCalls.length === 0, 'popstate does not pushState (got ' + nav3.pushStateCalls.length + ')');

const securityPopstate = loadNavigation({ pathname: '/security' });
const securityPopstateStates = [];
securityPopstate.win.NAGENT.app = { onTabActivated(s) { securityPopstateStates.push(s); } };
securityPopstate.nav.initNavigation();
securityPopstate.win.location.pathname = '/security/sandbox';
(securityPopstate.win._listeners.popstate || []).forEach((fn) => fn({}));
const sandboxState = securityPopstateStates[securityPopstateStates.length - 1];
ok(sandboxState && sandboxState.activeTab === 'security-sandbox', 'security popstate preserves sandbox active child');
ok(sandboxState && sandboxState.renderTab === 'security', 'security popstate preserves shared renderer');
ok(sandboxState && sandboxState.route && sandboxState.route.scope === 'sandbox', 'security popstate preserves sandbox scope');
ok(securityPopstate.pushStateCalls.length === 0, 'security popstate does not push history');

// Direct load and browser back/forward keep /observations/modules in its
// component-private scope without writing history.
const componentDirect = loadNavigation({ pathname: '/observations/modules' });
const componentDirectStates = [];
componentDirect.win.NAGENT.app = {
  onTabActivated(state2) { componentDirectStates.push(state2); },
};
componentDirect.nav.initNavigation();
const componentInitState = componentDirectStates[0];
ok(componentInitState && componentInitState.activeTab === 'observations-modules', 'component direct activeTab');
ok(componentInitState && componentInitState.renderTab === 'observations-modules', 'component direct renderTab');
ok(componentInitState && componentInitState.sidebarTab === 'observations-modules', 'component direct sidebarTab');
ok(componentInitState && componentInitState.currentSubdomain === 'observations-modules', 'component direct private subdomain');
ok(componentInitState && componentInitState.route !== null, 'component direct route entry');
ok(componentInitState && componentInitState.route
  && componentInitState.route.topnavParent === 'observations-modules', 'component direct route parent');
ok(componentInitState && componentInitState.route
  && componentInitState.route.paths.length === 1
  && componentInitState.route.paths[0] === '/observations/modules', 'component direct single route path');
ok(componentDirect.pushStateCalls.length === 0, 'component direct load does not pushState');
const componentPopstateListeners = componentDirect.win._listeners.popstate || [];
componentDirect.win.location.pathname = '/observations/modules';
componentPopstateListeners.forEach((fn) => fn({ path: '/observations/modules' }));
const componentPopState = componentDirectStates[componentDirectStates.length - 1];
ok(componentPopState && componentInitState
  && componentPopState.activeTab === componentInitState.activeTab
  && componentPopState.renderTab === componentInitState.renderTab
  && componentPopState.sidebarTab === componentInitState.sidebarTab
  && componentPopState.currentSubdomain === componentInitState.currentSubdomain
  && componentPopState.route === componentInitState.route,
  'component popstate preserves all route state fields');
ok(componentDirect.pushStateCalls.length === 0, 'component popstate does not pushState');

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

  ['tasks-observations', 'tasks', 'chat', 'summary', 'observations-modules', 'security'].forEach((id) => {
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
  const statusInitCalls = [];
  const statusRefreshCalls = [];
  const securityInitCalls = [];
  const securityRefreshCalls = [];
  const securityActivateCalls = [];

  const topnavConfig = {
    tasks: [
      { tab: 'tasks', path: '/tasks', label: '管理', concern: 'management', scope: 'tasks', topnavParent: 'tasks' },
      { tab: 'tasks-observations', path: '/tasks/observations', label: '观测', concern: 'observation', scope: 'tasks', topnavParent: 'tasks' },
    ],
    sessions: [
      { tab: 'sessions', path: '/sessions', label: '管理', concern: 'management', scope: 'sessions', topnavParent: 'sessions' },
      { tab: 'observations-sessions', path: '/sessions/observations', label: '观测', concern: 'observation', scope: 'sessions', topnavParent: 'sessions' },
      { tab: 'security-sessions', path: '/sessions/security', label: '安全', concern: 'security', scope: 'sessions', topnavParent: 'sessions' },
    ],
    observations: [
      { tab: 'observations-sessions', path: '/observations/sessions', label: '会话', concern: 'observation', scope: 'observations', topnavParent: 'observations' },
    ],
    'observations-modules': [
      { tab: 'observations-modules', path: '/observations/modules', label: '组件', concern: 'observation', scope: 'observations-modules', topnavParent: 'observations-modules' },
    ],
    'security-overview': [{ tab: 'security-overview', path: '/security', label: '概览', concern: 'security', scope: 'overview', topnavParent: 'security' }],
    'security-sessions': [{ tab: 'security-sessions', path: '/security/sessions', label: '会话', concern: 'security', scope: 'sessions', topnavParent: 'security' }],
    'security-memory': [{ tab: 'security-memory', path: '/security/memory', label: '记忆', concern: 'security', scope: 'memory', topnavParent: 'security' }],
    'security-sandbox': [{ tab: 'security-sandbox', path: '/security/sandbox', label: '沙盒', concern: 'security', scope: 'sandbox', topnavParent: 'security' }],
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
      status: {
        init() { statusInitCalls.push('observations-modules'); },
        refresh() { statusRefreshCalls.push('observations-modules'); },
      },
      security: {
        init(state) { securityInitCalls.push(state); return initPromise; },
        refresh(state) { securityRefreshCalls.push(state); },
        activate(state) { securityActivateCalls.push(state); },
      },
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
    statusInitCalls: statusInitCalls, statusRefreshCalls: statusRefreshCalls,
    securityInitCalls: securityInitCalls, securityRefreshCalls: securityRefreshCalls,
    securityActivateCalls: securityActivateCalls,
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

  // --- T5 security: a scope switch during pending init activates the new
  // scope once; it must not refresh the stale sessions scope.
  env = loadApp({ deferInit: true });
  const sessionsState = { renderTab: 'security', activeTab: 'security-sessions', currentSubdomain: 'security', sidebarTab: 'security-sessions', route: { scope: 'sessions' } };
  const memoryState = { renderTab: 'security', activeTab: 'security-memory', currentSubdomain: 'security', sidebarTab: 'security-memory', route: { scope: 'memory' } };
  const pendingSecurityInit = env.app.onTabActivated(sessionsState);
  const switchToMemory = env.app.onTabActivated(memoryState);
  ok(env.securityInitCalls.length === 1, 'T5 security first activation calls init once');
  ok(env.securityActivateCalls.length === 1, 'T5 security scope switch calls activate once');
  ok(env.securityActivateCalls[0] === memoryState, 'T5 security activate receives memory state');
  ok(env.securityRefreshCalls.length === 0, 'T5 security scope switch does not call refresh');
  env.resolveInit();
  await Promise.all([pendingSecurityInit, switchToMemory]);

  // --- T5 security route integration: a child route renders its one-item
  // private TopNav, rather than a shared/security-parent configuration.
  env = loadApp();
  await env.app.onTabActivated(memoryState);
  ok(env.renderCalls.length === 1, 'T5 security memory route renders TopNav once');
  if (env.renderCalls.length > 0) {
    ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items.length === 1,
      'T5 security memory private topnav has one item');
    ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[0].tab === 'security-memory',
      'T5 security memory private item owns security-memory');
    ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[0].path === '/security/memory',
      'T5 security memory private item uses the memory route');
    ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[0].label === '记忆',
      'T5 security memory private item has the memory label');
    ok(env.renderCalls[0].opts.activeTab === 'security-memory',
      'T5 security memory private topnav marks memory active');
  } else {
    ok(false, 'T5 security memory route skipped private TopNav assertions: no render');
  }

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

  // --- T5 A3b: /sessions topnav render receives sessions config (3 items: 管理 | 观测 | 安全) ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'sessions', activeTab: 'sessions', currentSubdomain: 'sessions', sidebarTab: 'sessions', route: null });
  ok(env.renderCalls.length === 1, 'T5 A3b /sessions topnav.render called once (got ' + env.renderCalls.length + ')');
  if (env.renderCalls.length > 0) {
    ok(env.renderCalls[0].opts.items === env.NAGENT.navigation.topnavConfig.sessions, 'T5 A3b /sessions topnav.render received topnavConfig.sessions items');
    ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items.length === 3, 'T5 A3b /sessions topnav.render received 3 items');
    ok(env.renderCalls[0].opts.activeTab === 'sessions', 'T5 A3b /sessions topnav.render activeTab === sessions (no cross-subdomain double activation)');
    const secondItem = env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[1];
    ok(!secondItem || secondItem.tab !== 'sessions',
      'T5 A3b /sessions sessions[1].tab is not "sessions" (no cross-subdomain double activation by item tab)');
  } else {
    ok(false, 'T5 A3b skipped: no topnav.render call');
  }

  // --- T4 A3d: /sessions/security topnav render receives sessions config (3 items) ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'security', activeTab: 'security-sessions', currentSubdomain: 'sessions', sidebarTab: 'sessions', route: { scope: 'sessions' } });
  ok(env.renderCalls.length === 1, 'T4 A3d /sessions/security topnav.render called once (got ' + env.renderCalls.length + ')');
  if (env.renderCalls.length > 0) {
    ok(env.renderCalls[0].opts.items === env.NAGENT.navigation.topnavConfig.sessions,
      'T4 A3d /sessions/security topnav.render received topnavConfig.sessions items');
    ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items.length === 3,
      'T4 A3d /sessions/security topnav.render received 3 items');
    ok(env.renderCalls[0].opts.activeTab === 'security-sessions',
      'T4 A3d /sessions/security topnav.render activeTab === security-sessions');
    // /security/sessions regression: must still use its private topnav (1 item, currentSubdomain=security)
    env = loadApp();
    await env.app.onTabActivated({ renderTab: 'security', activeTab: 'security-sessions', currentSubdomain: 'security', sidebarTab: 'security-sessions', route: { scope: 'sessions' } });
    ok(env.renderCalls.length === 1, 'T4 A3d /security/sessions regression: topnav.render called once (got ' + env.renderCalls.length + ')');
    if (env.renderCalls.length > 0) {
      ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items.length === 1,
        'T4 A3d /security/sessions regression: private topnav has 1 item (security-sessions)');
      ok(env.renderCalls[0].opts.items[0] && env.renderCalls[0].opts.items[0].tab === 'security-sessions',
        'T4 A3d /security/sessions regression: private topnav tab is security-sessions');
    }
  } else {
    ok(false, 'T4 A3d skipped: no topnav.render call');
  }

  // --- T4 A3e: /sessions/security DOM: 安全项 active + aria-current=page, 管理/观测 非 active ---
  env = loadTopnav();
  const sessionsItems = [
    { tab: 'sessions', path: '/sessions', label: '管理', concern: 'management', scope: 'sessions', topnavParent: 'sessions' },
    { tab: 'observations-sessions', path: '/sessions/observations', label: '观测', concern: 'observation', scope: 'sessions', topnavParent: 'sessions' },
    { tab: 'security-sessions', path: '/sessions/security', label: '安全', concern: 'security', scope: 'sessions', topnavParent: 'sessions' },
  ];
  env.topnav.render(env.container, { items: sessionsItems, activeTab: 'security-sessions', onActivate: function () {} });
  const links = liveQuery(env.container, 'a[href]');
  ok(links.length === 3, 'T4 A3e /sessions/security topnav renders 3 <a> items (got ' + links.length + ')');
  const activeLinks = links.filter((a) => a.getAttribute('aria-current') === 'page');
  ok(activeLinks.length === 1, 'T4 A3e exactly one aria-current=page (got ' + activeLinks.length + ')');
  if (activeLinks.length === 1) {
    ok(activeLinks[0]._cls.has('topnav__item--active'), 'T4 A3e active link has topnav__item--active class');
    ok(activeLinks[0].getAttribute('href') === '/sessions/security', 'T4 A3e active link href === /sessions/security');
    ok(activeLinks[0]._text === '安全', 'T4 A3e active link label is "安全"');
  } else {
    ok(false, 'T4 A3e skipped: no active link');
  }
  const inactiveLinks = links.filter((a) => a.getAttribute('aria-current') !== 'page');
  ok(inactiveLinks.length === 2, 'T4 A3e two inactive links (got ' + inactiveLinks.length + ')');
  inactiveLinks.forEach((a) => {
    ok(!a._cls.has('topnav__item--active'), 'T4 A3e inactive link ' + a.getAttribute('href') + ' has NO topnav__item--active');
  });

  // --- T4 A4c: /sessions topnav items navigate to their independent paths ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'sessions', activeTab: 'sessions', currentSubdomain: 'sessions', sidebarTab: 'sessions', route: null });
  if (env.renderCalls.length > 0) {
    const sessionManagementItem = env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[0];
    const sessionObservationItem = env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[1];
    const sessionSecurityItem = env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[2];
    ok(sessionManagementItem && sessionManagementItem.path === '/sessions', 'T4 A4c /sessions items[0] is 管理 -> /sessions');
    ok(sessionObservationItem && sessionObservationItem.path === '/sessions/observations', 'T4 A4c /sessions items[1] is 观测 -> /sessions/observations');
    ok(!!sessionSecurityItem, 'T4 A4c /sessions has items[2] (new 安全 item)');
    if (sessionSecurityItem) {
      ok(sessionSecurityItem.path === '/sessions/security', 'T4 A4c /sessions items[2].path === /sessions/security');
      env.renderCalls[0].opts.onActivate(sessionSecurityItem);
      ok(env.navigateCalls.length === 1, 'T4 A4c /sessions onActivate(sessions[2]) calls navigatePath once (got ' + env.navigateCalls.length + ')');
      ok(env.navigateCalls[0] === '/sessions/security', 'T4 A4c /sessions onActivate(sessions[2]) navigates to /sessions/security (got ' + env.navigateCalls[0] + ')');
    }
  } else {
    ok(false, 'T4 A4c skipped: no topnav.render call');
  }

  // --- T5 A3c: /observations/sessions topnav render receives observations config (1 item, active) ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'observations-sessions', activeTab: 'observations-sessions', currentSubdomain: 'observations', sidebarTab: 'observations-sessions', route: {} });
  ok(env.renderCalls.length === 1, 'T5 A3c /observations/sessions topnav.render called once (got ' + env.renderCalls.length + ')');
  if (env.renderCalls.length > 0) {
    ok(env.renderCalls[0].opts.items === env.NAGENT.navigation.topnavConfig.observations, 'T5 A3c /observations/sessions topnav.render received topnavConfig.observations items');
    ok(env.renderCalls[0].opts.items && env.renderCalls[0].opts.items.length === 1, 'T5 A3c /observations/sessions topnav.render received 1 item (no 管理)');
    ok(env.renderCalls[0].opts.activeTab === 'observations-sessions', 'T5 A3c /observations/sessions topnav.render activeTab === observations-sessions');
    ok(env.renderCalls[0].opts.items[0] && env.renderCalls[0].opts.items[0].label === '会话', 'T5 A3c /observations/sessions topnav.render items[0].label === 会话');
  } else {
    ok(false, 'T5 A3c skipped: no topnav.render call');
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

  // --- T5 A4b: /sessions topnav onActivate for sessions[1] navigates to /sessions/observations ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'sessions', activeTab: 'sessions', currentSubdomain: 'sessions', sidebarTab: 'sessions', route: null });
  if (env.renderCalls.length > 0) {
    const sessionObsItem = env.renderCalls[0].opts.items && env.renderCalls[0].opts.items[1];
    ok(!!sessionObsItem, 'T5 A4b /sessions has items[1] (new 观测 item)');
    if (sessionObsItem) {
      env.renderCalls[0].opts.onActivate(sessionObsItem);
      ok(env.navigateCalls.length === 1, 'T5 A4b /sessions onActivate(sessions[1]) calls navigatePath once (got ' + env.navigateCalls.length + ')');
      ok(env.navigateCalls[0] === '/sessions/observations', 'T5 A4b /sessions onActivate(sessions[1]) navigates to /sessions/observations (independent entry, got ' + env.navigateCalls[0] + ')');
    }
  } else {
    ok(false, 'T5 A4b skipped: no topnav.render call');
  }

  // --- T5 A11: component scope renders one private item and initializes status ---
  env = loadApp();
  await env.app.onTabActivated({ renderTab: 'observations-modules', activeTab: 'observations-modules', currentSubdomain: 'observations-modules', sidebarTab: 'observations-modules', route: {} });
  ok(env.renderCalls.length === 1, 'T5 A11 component topnav.render called once');
  if (env.renderCalls.length > 0) {
    const componentItems = env.renderCalls[0].opts.items;
    ok(componentItems === env.NAGENT.navigation.topnavConfig['observations-modules'],
      'T5 A11 render receives the component private scope');
    ok(componentItems && componentItems.length === 1, 'T5 A11 component scope has one item');
    ok(componentItems && componentItems[0].tab === 'observations-modules', 'T5 A11 component item tab');
    ok(componentItems && componentItems[0].topnavParent === 'observations-modules', 'T5 A11 component item parent');
    ok(componentItems && componentItems[0].scope === 'observations-modules', 'T5 A11 component item scope');
    ok(env.renderCalls[0].opts.activeTab === 'observations-modules', 'T5 A11 component item active');
    env.renderCalls[0].opts.onActivate(componentItems[0]);
    ok(env.navigateCalls.length === 1 && env.navigateCalls[0] === '/observations/modules',
      'T5 A11 component onActivate stays on /observations/modules');
  }
  ok(env.statusInitCalls.length === 1, 'T5 A11 first component activation initializes status');
  ok(env.statusRefreshCalls.length === 0, 'T5 A11 first component activation does not refresh status');

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
