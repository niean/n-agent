'use strict';
// Harness for the Task Security topnav page (tasks-security.js) and the
// shared renderer extraction from security.js.
// Run: node tests/interfaces/task_security_frontend_harness.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static');
const SECURITY_JS = fs.readFileSync(path.join(STATIC, 'security.js'), 'utf8');
const TASKS_SECURITY_JS = fs.readFileSync(path.join(STATIC, 'tasks-security.js'), 'utf8');
const NAV_JS = fs.readFileSync(path.join(STATIC, 'management-navigation.js'), 'utf8');
const API_JS = fs.readFileSync(path.join(STATIC, 'management-api.js'), 'utf8');
const INDEX_HTML = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');

let failures = 0;
let testCount = 0;
function ok(cond, msg) { testCount++; if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// --- DOM mock ---------------------------------------------------------------
function makeEl(tag) {
  const el = {
    tag: tag || 'div', _cls: new Set(), dataset: {}, _attrs: {},
    style: { setProperty() {} }, hidden: false, _listeners: {}, _kids: [],
    _text: '', _id: null, value: '', type: '', disabled: false,
    set textContent(v) { this._text = (v == null) ? '' : String(v); },
    get textContent() { return this._text; },
    set id(v) { this._id = v; if (v != null) sandbox.document._byId[v] = this; },
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
    replaceChildren() { this._kids = []; this._text = ''; },
    removeChild(c) { const i = this._kids.indexOf(c); if (i >= 0) this._kids.splice(i, 1); },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener() {},
    setAttribute(k, v) { this._attrs[k] = String(v); },
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; },
    hasAttribute(k) { return k in this._attrs; },
    removeAttribute(k) { delete this._attrs[k]; },
    remove() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    focus() {},
    click() { (this._listeners.click || []).forEach((fn) => fn({ preventDefault() {}, stopPropagation() {} })); },
  };
  return el;
}

function textOf(node) {
  if (!node) return '';
  let s = node._text || '';
  (node._kids || []).forEach((k) => { s += textOf(k); });
  return s;
}

function findElWithText(node, text) {
  if (!node) return null;
  if ((node._text || '').indexOf(text) !== -1) return node;
  for (const k of (node._kids || [])) {
    const f = findElWithText(k, text);
    if (f) return f;
  }
  return null;
}

function countTags(node, tags) {
  let n = 0;
  (function walk(nd) {
    if (!nd) return;
    if (tags.indexOf(nd.tag) !== -1) n++;
    (nd._kids || []).forEach(walk);
  })(node);
  return n;
}

const sandbox = {
  window: {}, document: {
    _byId: {},
    createElement(tag) { return makeEl(tag); },
    getElementById(id) { return this._byId[id] || null; },
    querySelectorAll() { return []; }, addEventListener() {},
  },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  matchMedia() { return { matches: false }; },
  ResizeObserver: function () { this.observe = function () {}; this.disconnect = function () {}; },
  console: console, setTimeout: setTimeout, clearTimeout: clearTimeout,
  Promise: Promise, Object: Object, Array: Array, JSON: JSON,
  Number: Number, Math: Math, Date: Date, Error: Error,
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// tasks-security.js captures `const api = namespace.api` at load time, so
// NAGENT.api must exist BEFORE loading; tests mutate api.task.security rather
// than reassigning NAGENT.api.
sandbox.NAGENT = sandbox.NAGENT || {};
sandbox.NAGENT.api = { task: { security: () => Promise.resolve({}) } };
// management-navigation.js sets NAGENT.navigation (topnavConfig/resolveRoute);
// load before security.js -- it does not touch NAGENT.api.
vm.runInContext(NAV_JS, sandbox);
vm.runInContext(SECURITY_JS, sandbox);
vm.runInContext(TASKS_SECURITY_JS, sandbox);

// --- T6: topnav config, route resolution, api URL, html wiring (sync) -------
(function testNavigationWiring() {
  const nav = sandbox.NAGENT.navigation;
  const items = nav.topnavConfig.tasks;
  ok(Array.isArray(items) && items.length === 3, 'topnavConfig.tasks has 3 items');
  const sec = items[2];
  ok(sec && sec.tab === 'tasks-security' && sec.path === '/tasks/security' && sec.label === '安全'
    && sec.concern === 'security' && sec.scope === 'tasks' && sec.topnavParent === 'tasks',
    '安全 topnav item has 6 spec fields');

  const r = nav.resolveRoute('/tasks/security');
  ok(r.activeTab === 'tasks-security' && r.renderTab === 'tasks-security'
    && r.sidebarTab === 'tasks' && r.currentSubdomain === 'tasks',
    'resolveRoute(/tasks/security) state');

  // Existing routes unchanged.
  ok(nav.resolveRoute('/tasks').activeTab === 'tasks', '/tasks resolves to tasks');
  ok(nav.resolveRoute('/tasks/observations').activeTab === 'tasks-observations', '/tasks/observations unchanged');
  ok(nav.resolveRoute('/observations/tasks').activeTab === 'tasks-observations', '/observations/tasks unchanged');
  // Non-tasks route must not resolve to tasks subdomain.
  ok(nav.resolveRoute('/security').currentSubdomain !== 'tasks', '/security not tasks subdomain');

  // api.task.security targets the right URL (source scan).
  ok(API_JS.indexOf("/chat/tasks/security") !== -1, 'management-api.js has /chat/tasks/security');

  // index.html: container once, script order security.js -> tasks-security.js -> app.js.
  ok(INDEX_HTML.indexOf('id="tab-tasks-security"') !== -1, 'index.html has tab-tasks-security container');
  ok((INDEX_HTML.match(/tab-tasks-security/g) || []).length === 1, 'tab-tasks-security appears once');
  const iSec = INDEX_HTML.indexOf('/static/security.js');
  const iTs = INDEX_HTML.indexOf('/static/tasks-security.js');
  const iApp = INDEX_HTML.indexOf('/static/app.js');
  ok(iSec !== -1 && iTs !== -1 && iApp !== -1 && iSec < iTs && iTs < iApp,
    'script order: security.js -> tasks-security.js -> app.js');
})();

// --- T4: renderer exposure (synchronous) -----------------------------------
(function testRendererExposure() {
  const r = sandbox.NAGENT.security.renderers;
  const need = ['overview', 'sector', 'meta', 'cfg', 'policyItem', 'statCard', 'formatValue'];
  ok(r && need.every((k) => typeof r[k] === 'function'), 'renderers expose 7 functions');
  ok(typeof sandbox.NAGENT.security.init === 'function' && typeof sandbox.NAGENT.security.refresh === 'function',
    'security init/refresh preserved');
  const n1 = r.overview({ profile_version: 'v1', policies: [{}, {}] });
  ok(textOf(n1).indexOf('Policy 数量') !== -1, 'overview default label Policy 数量');
  const n2 = r.overview({ profile_version: 'v1', policies: [{}, {}] }, { countLabel: 'Sector 数量' });
  ok(textOf(n2).indexOf('Sector 数量') !== -1 && textOf(n2).indexOf('Policy 数量') === -1, 'overview countLabel override');
  const sNo = r.sector({ display_name: 'S1', dimension: 'd', execution_point: 'e', config: [] });
  ok(textOf(sNo).indexOf('来源文件') === -1, 'sector default hides source_files');
  const sYes = r.sector({ display_name: 'S1', dimension: 'd', execution_point: 'e', config: [], source_files: ['a.py', 'b.py'] }, { showSourceFiles: true, sourceLabel: '来源文件' });
  ok(textOf(sYes).indexOf('来源文件') !== -1 && textOf(sYes).indexOf('a.py, b.py') !== -1, 'sector showSourceFiles renders paths');
})();

// --- Helper: reset container + module state --------------------------------
function resetPage(securityFn) {
  sandbox.document._byId = {};
  const container = sandbox.document.createElement('div');
  container.id = 'tab-tasks-security';
  container._cls.add('active');
  sandbox.NAGENT.api.task.security = securityFn; // mutate, not reassign
  sandbox.NAGENT.tasksSecurity.deactivate(); // invalidate tokens + clear in-flight/data
  return container;
}

function validPayload() {
  return {
    profile_version: 'task-security-v1',
    policies: [
      { key: 'task_policy', name: 'TaskPolicy', display_name: '任务策略', dimension: 'd1', execution_point: 'e1', source_files: ['a.py'], config: [{ key: 'state_count', label: '状态数量', value: 7 }] },
      { key: 'task_execution', name: 'X', display_name: '执行管控', dimension: 'd2', execution_point: 'e2', source_files: ['b.py'], config: [{ key: 'task_enabled', label: '启用', value: true }] },
      { key: 'task_planning', name: 'X', display_name: '规划', dimension: 'd3', execution_point: 'e3', source_files: ['c.py'], config: [{ key: 'g', label: 'g', value: 10 }] },
      { key: 'worker_security', name: 'X', display_name: 'Worker', dimension: 'd4', execution_point: 'e4', source_files: ['d.py'], config: [{ key: 's', label: 's', value: 'task' }] },
      { key: 'approval_security', name: 'X', display_name: '审批', dimension: 'd5', execution_point: 'e5', source_files: ['e.py'], config: [{ key: 'n', label: 'n', value: null }] },
    ],
  };
}

// tick: wait one macrotask so microtasks (await) drain.
function tick() { return new Promise((res) => setTimeout(res, 0)); }

// --- T5 tests (sequential async) -------------------------------------------
async function runTests() {
  // #1: exports
  const m = sandbox.NAGENT.tasksSecurity;
  ok(typeof m.init === 'function' && typeof m.refresh === 'function' && typeof m.deactivate === 'function',
    'tasksSecurity exposes init/refresh/deactivate');

  // #3: valid payload renders
  {
    const c = resetPage(() => Promise.resolve(validPayload()));
    await sandbox.NAGENT.tasksSecurity.refresh();
    const txt = textOf(c);
    ok(txt.indexOf('Sector 数量') !== -1, 'overview uses Sector 数量 countLabel');
    ok(txt.indexOf('来源文件') !== -1, 'sectors show 来源文件');
    ok(txt.indexOf('任务策略') !== -1 && txt.indexOf('执行管控') !== -1 && txt.indexOf('审批') !== -1,
      'all 5 sector display_names rendered');
  }

  // #2: renderer missing -> fixed error, no API call
  {
    let called = 0;
    const real = sandbox.NAGENT.security.renderers;
    const c = resetPage(() => { called++; return Promise.resolve(validPayload()); });
    delete sandbox.NAGENT.security.renderers;
    await sandbox.NAGENT.tasksSecurity.refresh();
    ok(called === 0, 'no API call when renderers missing');
    ok(findElWithText(c, '安全页面渲染器不可用') !== null, 'renderer-missing shows fixed error');
    sandbox.NAGENT.security.renderers = real;
  }

  // #4-6: invalid payloads -> fixed error
  const invalidPayloads = [
    ['null', null], ['array', [1, 2]],
    ['empty version', { profile_version: '', policies: [] }],
    ['empty policies', { profile_version: 'v', policies: [] }],
    ['missing policies', { profile_version: 'v' }],
    ['extra top field', { profile_version: 'v', policies: [], x: 1 }],
    ['sector missing field', { profile_version: 'v', policies: [{ key: 'k', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: ['a.py'] }] }],
    ['sector dup key', { profile_version: 'v', policies: [
      { key: 'dup', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: ['a.py'], config: [{ key: 'a', label: 'b', value: 1 }] },
      { key: 'dup', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: ['a.py'], config: [{ key: 'a', label: 'b', value: 1 }] },
    ] }],
    ['empty source_files', { profile_version: 'v', policies: [{ key: 'k', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: [], config: [{ key: 'a', label: 'b', value: 1 }] }] }],
    ['empty config', { profile_version: 'v', policies: [{ key: 'k', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: ['a.py'], config: [] }] }],
    ['config extra field', { profile_version: 'v', policies: [{ key: 'k', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: ['a.py'], config: [{ key: 'a', label: 'b', value: 1, z: 9 }] }] }],
    ['config non-finite number', { profile_version: 'v', policies: [{ key: 'k', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: ['a.py'], config: [{ key: 'a', label: 'b', value: Infinity }] }] }],
    ['config object value', { profile_version: 'v', policies: [{ key: 'k', name: 'n', display_name: 'd', dimension: 'x', execution_point: 'e', source_files: ['a.py'], config: [{ key: 'a', label: 'b', value: { x: 1 } }] }] }],
  ];
  for (const [name, payload] of invalidPayloads) {
    const c = resetPage(() => Promise.resolve(payload));
    await sandbox.NAGENT.tasksSecurity.refresh();
    ok(textOf(c).indexOf('任务安全配置加载失败') !== -1, 'invalid payload [' + name + '] shows fixed error');
  }

  // #7: API reject + no leak
  {
    const c = resetPage(() => Promise.reject(new Error('leaky: /Users/secret sqlite3 api_key=sk-xxx')));
    await sandbox.NAGENT.tasksSecurity.refresh();
    const txt = textOf(c);
    ok(txt.indexOf('任务安全配置加载失败') !== -1, 'API reject shows fixed error');
    ok(txt.indexOf('leaky') === -1 && txt.indexOf('secret') === -1 && txt.indexOf('api_key') === -1,
      'API reject does not leak err.message');
  }

  // #8: concurrent init() shares in-flight Promise, one request
  {
    let calls = 0;
    const c = resetPage(() => { calls++; return Promise.resolve(validPayload()); });
    const p1 = sandbox.NAGENT.tasksSecurity.init();
    const p2 = sandbox.NAGENT.tasksSecurity.init();
    ok(p1 === p2, 'concurrent init returns same in-flight Promise');
    await p2;
    ok(calls === 1, 'concurrent init issues one request');
    // Repeat init() after completion -> no reload.
    const before = calls;
    await sandbox.NAGENT.tasksSecurity.init();
    ok(calls === before, 'repeat init after completion does not reload');
  }

  // #9: refresh() invalidates old token; stale response does not overwrite
  {
    let resolveFirst;
    const c = resetPage(() => new Promise((res) => { resolveFirst = () => res(validPayload()); }));
    sandbox.NAGENT.tasksSecurity.refresh(); // stalls on resolveFirst
    // Second refresh with immediate resolve supersedes.
    resetPage(() => Promise.resolve(validPayload()));
    // NOTE: resetPage called deactivate() -> first load invalidated. Re-trigger:
    const c2 = sandbox.document.getElementById('tab-tasks-security');
    const p = sandbox.NAGENT.tasksSecurity.refresh();
    await p;
    if (resolveFirst) resolveFirst(); // stale first response resolves late
    await tick();
    ok(textOf(c2).indexOf('任务策略') !== -1, 'refresh keeps current load; stale response discarded');
  }

  // #10: deactivate() discards late response
  {
    let resolveLate;
    const c = resetPage(() => new Promise((res) => { resolveLate = () => res(validPayload()); }));
    sandbox.NAGENT.tasksSecurity.refresh();
    sandbox.NAGENT.tasksSecurity.deactivate();
    if (resolveLate) resolveLate();
    await tick();
    ok(textOf(c).indexOf('任务策略') === -1, 'deactivate discards late response');
  }

  // #13: malicious strings only become text
  {
    const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>';
    const payload = {
      profile_version: 'task-security-v1',
      policies: [{
        key: 'k', name: 'n', display_name: evil, dimension: evil, execution_point: 'e',
        source_files: [evil], config: [{ key: 'a', label: evil, value: evil }],
      }],
    };
    for (let i = 0; i < 4; i++) {
      payload.policies.push({ key: 'k' + i, name: 'n', display_name: 's' + i, dimension: 'd', execution_point: 'e', source_files: ['a.py'], config: [{ key: 'a', label: 'b', value: 1 }] });
    }
    const c = resetPage(() => Promise.resolve(payload));
    await sandbox.NAGENT.tasksSecurity.refresh();
    const txt = textOf(c);
    ok(txt.indexOf(evil) !== -1, 'evil string rendered as text');
    ok(countTags(c, ['script', 'img']) === 0, 'no script/img elements created from evil string');
  }

  // Source safety scan
  const src = TASKS_SECURITY_JS + '\n' + SECURITY_JS;
  ok(!/\.innerHTML\s*=/.test(src), 'no innerHTML assignment');
  ok(!/\.insertAdjacentHTML\s*\(/.test(src), 'no insertAdjacentHTML call');
  ok(!/\balert\s*\(/.test(src), 'no native alert()');
  ok(!/\beval\s*\(/.test(src) && !/\bFunction\s*\(/.test(src), 'no eval/Function dynamic exec');
}

runTests().then(() => {
  if (failures === 0) {
    console.log('all tests passed (' + testCount + ' assertions)');
    process.exit(0);
  } else {
    console.error(failures + ' test(s) failed (' + testCount + ' assertions)');
    process.exit(1);
  }
}).catch((e) => { console.error('harness error:', e); process.exit(1); });
