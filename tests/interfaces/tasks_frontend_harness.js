'use strict';
// Minimal, dependency-free behavior harness for tasks.js (Kanban board).
// Run with: node tests/interfaces/tasks_frontend_harness.js
// Exits 0 on success, 1 on any failure. Loaded by test_tasks_frontend.py
// via subprocess; skipped when Node is unavailable.
//
// Regression: /chat/tasks/board returns `columns` as an ARRAY of
// {status, cards, total}; tasks.js must index by status, not treat it as a
// dict keyed by status (which yields undefined and renders zero cards).
//
// Regression: 新增任务 must use a standard modal (not window.prompt) with
// at least title + goal inputs; submit builds the create payload and closes
// the modal; Esc / backdrop close without creating.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TASKS_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'tasks.js');
const code = fs.readFileSync(TASKS_JS, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// Minimal DOM mock. Nodes auto-register by id so getElementById finds
// elements created and assigned .id during renderShell (e.g. kanban-board-root)
// and the create modal (tasks-create-modal / tasks-create-title ...).
const byId = {};
const created = [];
function makeNode(tag) {
  const n = {
    tag: tag,
    className: '',
    _text: '',
    children: [],
    _listeners: {},
    dataset: {},
    hidden: false,
    type: '',
    draggable: false,
    _id: null,
    // Form-control value state (inputs/textareas/checkboxes).
    value: '',
    checked: false,
    disabled: false,
    required: false,
    placeholder: '',
    _removed: false,
    set textContent(v) { this._text = (v === null || v === undefined) ? '' : String(v); },
    get textContent() { return this._text; },
    set id(v) { this._id = v; if (v != null) byId[v] = this; },
    get id() { return this._id; },
    appendChild(c) { this.children.push(c); return c; },
    append() { for (let i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },
    replaceChildren() { this.children = []; },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    setAttribute() {},
    focus() {},
    remove() { this._removed = true; },
  };
  created.push(n);
  return n;
}
const document = {
  createElement: makeNode,
  getElementById: (id) => byId[id] || null,
  body: makeNode('body'),
  _listeners: {},
  addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
  removeEventListener(ev, fn) {
    if (this._listeners[ev]) this._listeners[ev] = this._listeners[ev].filter((f) => f !== fn);
  },
};

function freshEnv(api) {
  for (const k of Object.keys(byId)) delete byId[k];
  created.length = 0;
  document._listeners = {};
  document.body = makeNode('body');
  byId['tasks-board'] = makeNode('div');
  // Static "新增任务" button lives in the index.html panel-header (mirrors
  // scheduled-tasks' #scheduled-task-new). Pre-register it so tasks.js init()
  // can bind via getElementById('task-new').
  const newBtn = makeNode('button');
  newBtn.type = 'button';
  newBtn.textContent = '新增';
  newBtn.id = 'task-new';
  const ui = {
    el: (tag, className) => { const n = makeNode(tag); if (className) n.className = className; return n; },
    clear: (node) => { if (node) node.replaceChildren(); },
  };
  const win = {
    NAGENT: { api: api, ui: ui },
    location: { protocol: 'http:', host: '127.0.0.1:8201' },
    WebSocket: function () { return { close() {} }; },
  };
  const ctx = {
    NAGENT: win.NAGENT,
    document: document,
    console: console,
    window: win,
    WebSocket: win.WebSocket,
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  return ctx;
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

// Real backend /chat/tasks/board shape.
function makeBoard(triageCards, opts) {
  opts = opts || {};
  const cols = ['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done'];
  const columns = cols.map((st) => ({
    status: st,
    cards: st === 'triage' ? triageCards : [],
    total: st === 'triage' ? triageCards.length : 0,
  }));
  if (opts.archivedColumn) columns.push(opts.archivedColumn);
  return { columns: columns, archived: !!opts.archivedColumn, assignees: opts.assignees || [] };
}

function card(id, title) {
  return { id: id, title: title, body: '', assignee: null, priority: 0, status: 'triage',
    goal_mode: false, version: 1, created_at: '2026-07-19T00:00:00+00:00' };
}

// Open the create modal by clicking the 新增任务 button bound in init() to the
// static #task-new element (mirrors scheduled-tasks' #scheduled-task-new).
function openCreateViaButton() {
  const newBtn = byId['task-new'];
  ok(!!newBtn, '新增 button exists (id=task-new, bound in init)');
  if (newBtn) (newBtn._listeners.click || []).forEach((fn) => fn());
}

async function testRendersTriageCards() {
  const calls = [];
  const api = { task: { board: () => Promise.resolve(makeBoard([card('t1', 'T1'), card('t2', 'T2')])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  const root = byId['kanban-board-root'];
  ok(!!root, 'kanban-board-root created');
  // 8 active columns appended in COLUMNS order.
  ok(root.children.length === 8, '8 active columns rendered (got ' + root.children.length + ')');

  const triageCol = root.children[0];
  // children: [header, list]
  ok(triageCol.children.length === 2, 'triage column has header+list (got ' + triageCol.children.length + ')');
  const header = triageCol.children[0];
  ok(header.textContent.indexOf('(2)') !== -1, 'triage header shows total (2) (got ' + header.textContent + ')');
  const list = triageCol.children[1];
  ok(list.children.length === 2, 'triage list has 2 cards (got ' + list.children.length + ')');
  if (list.children.length >= 1) {
    ok(list.children[0].dataset.id === 't1', 'first card id t1 (got ' + list.children[0].dataset.id + ')');
  }

  // todo column empty.
  const todoCol = root.children[1];
  ok(todoCol.children[1].children.length === 0, 'todo list empty');
}

async function testEmptyBoardNoCrash() {
  const api = { task: { board: () => Promise.resolve(makeBoard([])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();
  const root = byId['kanban-board-root'];
  ok(root.children.length === 8, 'empty board still 8 columns');
  ok(root.children[0].children[1].children.length === 0, 'triage list empty on empty board');
}

async function testToolbarOnlyCreateButton() {
  // prd 20260719: drop the 刷新 button and 显示已归档 toggle from the Kanban
  // toolbar. 新增任务 moves into the panel-header top-right (panel-actions),
  // mirroring scheduled-tasks' #scheduled-task-new (class="btn"). refresh()
  // still runs on init / WS event / after create, so the manual button is
  // redundant; archived tasks are no longer surfaced in the board.
  const api = { task: { board: () => Promise.resolve(makeBoard([])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  ok(byId['task-new'], '新增 button in panel-header (id=task-new)');
  const labels = created.filter((n) => n.tag === 'button').map((n) => n.textContent);
  ok(labels.indexOf('刷新') === -1, 'no 刷新 button (got ' + JSON.stringify(labels) + ')');
  ok(labels.indexOf('新增') !== -1, '新增 button present (got ' + JSON.stringify(labels) + ')');
  // renderShell must not create its own button or archived toggle; the only
  // button is the static #task-new pre-registered in freshEnv.
  ok(labels.length === 1, 'exactly one button (static #task-new) (got ' + JSON.stringify(labels) + ')');
  const checkboxes = created.filter((n) => n.type === 'checkbox');
  ok(checkboxes.length === 0, 'no archived toggle checkbox in shell (got ' + checkboxes.length + ')');
}

async function testCreateModalSubmits() {
  const createCalls = [];
  const api = {
    task: {
      board: () => Promise.resolve(makeBoard([])),
      create: (body) => { createCalls.push(body); return Promise.resolve({ id: 't_new' }); },
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  openCreateViaButton();
  await tick();

  const backdrop = byId['tasks-create-modal'];
  ok(!!backdrop, 'create modal backdrop created with id tasks-create-modal');
  ok(backdrop && !backdrop._removed, 'modal open (not removed)');

  // Title + goal inputs auto-registered by id via field().
  const titleInput = byId['tasks-create-title'];
  const goalInput = byId['tasks-create-goal'];
  ok(!!titleInput, 'title input registered by id');
  ok(!!goalInput, 'goal input registered by id');
  titleInput.value = 'My Task';
  goalInput.value = 'compute 23 * 17';

  // form is dialog > form (dialog is backdrop.children[0]).
  const form = backdrop.children[0].children[0];
  ok(form && form.tag === 'form', 'form is dialog child');
  form._listeners.submit[0]({ preventDefault() {} });
  await tick();

  ok(createCalls.length === 1, 'create called once (got ' + createCalls.length + ')');
  if (createCalls.length === 1) {
    ok(createCalls[0].title === 'My Task', 'payload title (got ' + createCalls[0].title + ')');
    ok(createCalls[0].body === 'compute 23 * 17', 'payload body carries goal (got ' + createCalls[0].body + ')');
    ok(createCalls[0].goal_mode === false, 'payload goal_mode false (got ' + createCalls[0].goal_mode + ')');
    ok(!('triage' in createCalls[0]), 'legacy triage flag dropped from payload');
    ok(!('assignee' in createCalls[0]), 'assignee not sent (prd direction: drop human-PM fields)');
  }
  ok(backdrop._removed === true, 'modal closed after successful create');
}

async function testCreateModalValidatesEmptyTitle() {
  const createCalls = [];
  const api = {
    task: {
      board: () => Promise.resolve(makeBoard([])),
      create: (body) => { createCalls.push(body); return Promise.resolve({}); },
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  openCreateViaButton();
  await tick();

  const backdrop = byId['tasks-create-modal'];
  const form = backdrop.children[0].children[0];
  // Submit with empty title.
  form._listeners.submit[0]({ preventDefault() {} });
  await tick();

  ok(createCalls.length === 0, 'create NOT called on empty title (got ' + createCalls.length + ')');
  ok(!backdrop._removed, 'modal stays open on validation failure');
  const hint = form.children.find((c) => c.className && c.className.indexOf('providers-form__hint') !== -1);
  ok(!!hint && hint.className.indexOf('badge--danger') !== -1, 'hint shows danger on empty title');
  ok(hint && hint.textContent === '请填写标题', 'hint message text (got ' + (hint && hint.textContent) + ')');
}

async function testCreateModalEscapeCloses() {
  const api = { task: { board: () => Promise.resolve(makeBoard([])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  openCreateViaButton();
  await tick();

  const backdrop = byId['tasks-create-modal'];
  ok(!backdrop._removed, 'modal open before ESC');
  // Dispatch ESC keydown (per-modal listener, aligned with chat.js pattern).
  (document._listeners.keydown || []).forEach((fn) => fn({ key: 'Escape' }));
  ok(backdrop._removed === true, 'modal closed on ESC');
}

async function testCreateModalBackdropClickCloses() {
  const api = { task: { board: () => Promise.resolve(makeBoard([])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  openCreateViaButton();
  await tick();

  const backdrop = byId['tasks-create-modal'];
  // Clicking the backdrop (target === backdrop) closes; clicking the dialog does not.
  (backdrop._listeners.click || []).forEach((fn) => fn({ target: backdrop }));
  ok(backdrop._removed === true, 'modal closed on backdrop click');
}

(async () => {
  await testRendersTriageCards();
  await testEmptyBoardNoCrash();
  await testToolbarOnlyCreateButton();
  await testCreateModalSubmits();
  await testCreateModalValidatesEmptyTitle();
  await testCreateModalEscapeCloses();
  await testCreateModalBackdropClickCloses();
  if (failures) { console.error('\n' + failures + ' test(s) failed'); process.exit(1); }
  console.log('tasks_frontend_harness: all tests passed');
  process.exit(0);
})();
