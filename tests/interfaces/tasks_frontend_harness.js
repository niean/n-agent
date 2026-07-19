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
    style: {
      _p: {},
      setProperty(k, v) { this._p[k] = String(v); },
    },
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

function freshEnv(api, opts) {
  opts = opts || {};
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
  // modal.confirm mirrors NAGENT.modal.confirm (management-ui.js): returns a
  // Promise<boolean>. opts.confirmReturn (default true) drives the resolution
  // so delete-flow tests can simulate user confirm / cancel.
  const confirmReturn = opts.confirmReturn !== false;
  const alertCalls = [];
  const modal = {
    confirm: () => Promise.resolve(confirmReturn),
    alert: (message, options) => {
      alertCalls.push({ message: message, options: options || {} });
      return Promise.resolve();
    },
  };
  const win = {
    NAGENT: { api: api, ui: ui, modal: modal },
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
  ctx.modalAlertCalls = alertCalls;
  return ctx;
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

// Real backend /chat/tasks/board shape.
function makeBoard(queuedCards) {
  const lanes = [
    { id: 'queued', label: '排队', statuses: ['queued'] },
    { id: 'running', label: '运行中', statuses: ['running'] },
    { id: 'waiting_approval', label: '待批准', statuses: ['waiting_approval'] },
    { id: 'failed_expired', label: '失败/过期', statuses: ['failed', 'expired'] },
    { id: 'succeeded_cancelled', label: '成功/取消', statuses: ['succeeded', 'cancelled'] },
  ];
  const columns = lanes.map((l) => ({
    id: l.id, label: l.label, statuses: l.statuses,
    cards: l.id === 'queued' ? queuedCards : [],
    total: l.id === 'queued' ? queuedCards.length : 0,
  }));
  return { columns: columns };
}

function card(id, title) {
  return { id: id, title: title, body: '', priority: 0, status: 'queued',
    goal_mode: false, version: 1, created_at: '2026-07-19T00:00:00+00:00', is_archived: false };
}

// Build a board with cards placed into arbitrary swimlanes. laneMap keys are
// lane ids (queued/running/waiting_approval/failed_expired/succeeded_cancelled);
// values are card arrays. Used by delete-flow tests that need terminal cards.
function makeBoardLanes(laneMap) {
  const lanes = [
    { id: 'queued', label: '排队', statuses: ['queued'] },
    { id: 'running', label: '运行中', statuses: ['running'] },
    { id: 'waiting_approval', label: '待批准', statuses: ['waiting_approval'] },
    { id: 'failed_expired', label: '失败/过期', statuses: ['failed', 'expired'] },
    { id: 'succeeded_cancelled', label: '成功/取消', statuses: ['succeeded', 'cancelled'] },
  ];
  const columns = lanes.map((l) => {
    const cards = laneMap[l.id] || [];
    return { id: l.id, label: l.label, statuses: l.statuses, cards: cards, total: cards.length };
  });
  return { columns: columns };
}

// Find a delete button inside a card. Task deletion must remain in the detail
// modal, so cards in every swimlane must return null.
function findCardDeleteButton(cardNode) {
  const meta = cardNode.children.find((c) => c.className === 'kanban-card__meta');
  if (!meta) return null;
  return meta.children.find((c) => c.tag === 'button' && c.className.indexOf('kanban-card__delete') !== -1) || null;
}

// Open the create modal by clicking the 新增任务 button bound in init() to the
// static #task-new element (mirrors scheduled-tasks' #scheduled-task-new).
function openCreateViaButton() {
  const newBtn = byId['task-new'];
  ok(!!newBtn, '新增 button exists (id=task-new, bound in init)');
  if (newBtn) (newBtn._listeners.click || []).forEach((fn) => fn());
}

async function testRendersQueuedCards() {
  const api = { task: { board: () => Promise.resolve(makeBoard([card('t1', 'T1'), card('t2', 'T2')])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  const root = byId['kanban-board-root'];
  ok(!!root, 'kanban-board-root created');
  ok(root.children.length === 5, '5 swimlanes rendered (got ' + root.children.length + ')');
  // Adaptive column count: --kanban-column-count follows the actual swimlane
  // count so the grid fills the horizontal width.
  ok(root.style._p['--kanban-column-count'] === '5', 'kanban-column-count set to 5 (got ' + root.style._p['--kanban-column-count'] + ')');

  const queuedCol = root.children[0];
  ok(queuedCol.children.length === 2, 'queued lane has header+list (got ' + queuedCol.children.length + ')');
  const header = queuedCol.children[0];
  ok(header.textContent.indexOf('(2)') !== -1, 'queued header shows total (2) (got ' + header.textContent + ')');
  const list = queuedCol.children[1];
  ok(list.children.length === 2, 'queued list has 2 cards (got ' + list.children.length + ')');
  if (list.children.length >= 1) {
    ok(list.children[0].dataset.id === 't1', 'first card id t1 (got ' + list.children[0].dataset.id + ')');
  }

  const runningCol = root.children[1];
  ok(runningCol.children[1].children.length === 0, 'running list empty');
}

async function testEmptyBoardNoCrash() {
  const api = { task: { board: () => Promise.resolve(makeBoard([])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();
  const root = byId['kanban-board-root'];
  ok(root.children.length === 5, 'empty board still 5 swimlanes');
  ok(root.children[0].children[1].children.length === 0, 'queued list empty on empty board');
}

async function testUnsupportedLaneDropUsesStandardModal() {
  const api = { task: { board: () => Promise.resolve(makeBoard([card('t1', '待迁移任务')])) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  const runningList = byId['kanban-board-root'].children[1].children[1];
  (runningList._listeners.drop || []).forEach((fn) => fn({
    preventDefault() {},
    dataTransfer: { getData: () => 't1' },
  }));
  await tick();

  ok(ctx.modalAlertCalls.length === 1, 'unsupported lane drop opens one standard modal');
  if (ctx.modalAlertCalls.length === 1) {
    ok(ctx.modalAlertCalls[0].options.title === '无法迁移任务', 'drag modal has migration title');
    ok(ctx.modalAlertCalls[0].message.indexOf('该泳道不支持拖拽迁移') !== -1, 'drag modal explains unsupported lane');
  }
}

async function testCardDetailUsesStandardModal() {
  const task = card('t1', '详情任务');
  task.created_by = 'alice';
  task.updated_at = '2026-07-19T01:00:00+00:00';
  task.workspace_kind = 'dir';
  task.workspace_path = '/workspace/demo';
  task.skills = ['review'];
  task.model_override = 'model-x';
  task.max_runtime_seconds = 120;
  const api = {
    task: {
      board: () => Promise.resolve(makeBoard([task])),
      get: () => Promise.resolve({
        task,
        runs: [{ id: 7, status: 'completed', outcome: 'completed', started_at: '2026-07-19T00:00:00+00:00', ended_at: '2026-07-19T01:00:00+00:00', summary: 'done' }],
        events: [{ created_at: '2026-07-19T00:00:00+00:00', kind: 'created', payload: { source: 'dashboard' } }],
        comments: [{ created_at: '2026-07-19T00:00:00+00:00', author: 'alice', body: '请优先处理' }],
        attachments: [{ filename: 'brief.md', content_type: 'text/markdown', size: 12 }],
        worker_context: '任务上下文',
      }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  const board = byId['kanban-board-root'];
  const taskCard = board.children[0].children[1].children[0];
  (taskCard._listeners.click || []).forEach((fn) => fn());
  await tick();

  const backdrop = byId['tasks-detail-modal'];
  ok(!!backdrop && !backdrop._removed, 'task detail opens in a modal');
  ok(backdrop && backdrop.className === 'modal-backdrop', 'task detail uses modal-backdrop');
  const dialog = backdrop && backdrop.children[0];
  ok(dialog && dialog.className.indexOf('modal-dialog') !== -1, 'task detail uses modal-dialog');
  const form = dialog && dialog.children[0];
  ok(form && form.className.indexOf('providers-form') !== -1, 'task detail uses standard modal content');
  ok(form && form.children[0].className === 'modal-header', 'task detail renders standard modal header');
  const detailGrid = form && form.children.find((node) => node.className === 'tasks-detail__grid');
  ok(detailGrid && detailGrid.children.some((row) => row.children[1] && row.children[1].textContent === 't1'), 'task detail renders task fields');
  const detailText = created.map((node) => node.textContent).join('\n');
  ok(detailText.indexOf('alice') !== -1, 'task detail renders task owner');
  ok(detailText.indexOf('/workspace/demo') !== -1, 'task detail renders execution configuration');
  ok(detailText.indexOf('执行记录') !== -1 && detailText.indexOf('评论') !== -1, 'task detail renders related records');
  ok(detailText.indexOf('2026-07-19 08:00:00') !== -1, 'task detail renders timestamps in UTC+8');

  (document._listeners.keydown || []).forEach((fn) => fn({ key: 'Escape' }));
  ok(backdrop._removed === true, 'task detail modal closes on ESC');
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

// 删除入口仅保留在详情 modal：任意泳道卡片都不渲染删除按钮；终态任务的
// 详情 modal 仍会显示删除任务（见 testDetailDeleteButtonOnTerminalTask）。
async function testCardsDoNotRenderDeleteButton() {
  const api = { task: { board: () => Promise.resolve(makeBoardLanes({
    queued: [card('q1', '排队中')],
    failed_expired: [Object.assign(card('f1', '失败'), { status: 'failed' })],
    succeeded_cancelled: [
      Object.assign(card('s1', '成功'), { status: 'succeeded' }),
      Object.assign(card('c1', '取消'), { status: 'cancelled' }),
    ],
  })) } };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  const root = byId['kanban-board-root'];
  const queuedCard = root.children[0].children[1].children[0];
  ok(!findCardDeleteButton(queuedCard), 'queued card has no delete button');
  const failedCard = root.children[3].children[1].children[0];
  ok(!findCardDeleteButton(failedCard), 'failed card has no delete button');
  const succList = root.children[4].children[1];
  ok(succList.children.length === 2, 'succeeded lane has 2 cards (got ' + succList.children.length + ')');
  ok(succList.children.every((c) => !findCardDeleteButton(c)), 'succeeded/cancelled cards have no delete button');
}

async function testDetailDeleteButtonOnTerminalTask() {
  const task = Object.assign(card('f1', '失败任务'), { status: 'failed' });
  const api = {
    task: {
      board: () => Promise.resolve(makeBoardLanes({ failed_expired: [task] })),
      get: () => Promise.resolve({ task, runs: [], events: [], comments: [], attachments: [] }),
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  const board = byId['kanban-board-root'];
  const taskCard = board.children[3].children[1].children[0];
  (taskCard._listeners.click || []).forEach((fn) => fn());
  await tick();

  const backdrop = byId['tasks-detail-modal'];
  ok(!!backdrop && !backdrop._removed, 'detail modal open for terminal task');
  const delBtn = created.find((n) => n.tag === 'button' && n.textContent === '删除任务' && n.className.indexOf('btn--danger') !== -1);
  ok(!!delBtn, 'detail renders 删除任务 button (btn--danger) for terminal task');

  // Regression: queued (in-flight) task detail must NOT show 删除任务.
  const queuedTask = card('q1', '排队任务');
  const ctx2 = freshEnv({
    task: {
      board: () => Promise.resolve(makeBoardLanes({ queued: [queuedTask] })),
      get: () => Promise.resolve({ task: queuedTask, runs: [], events: [], comments: [], attachments: [] }),
    },
  });
  ctx2.NAGENT.tasks.init();
  await tick();
  const qCard = byId['kanban-board-root'].children[0].children[1].children[0];
  (qCard._listeners.click || []).forEach((fn) => fn());
  await tick();
  const qDel = created.find((n) => n.tag === 'button' && n.textContent === '删除任务');
  ok(!qDel, 'queued task detail has no 删除任务 button');
}

async function testApprovalNoteTextareaAndSubmit() {
  const task = Object.assign(card('w1', '待批准'), { status: 'waiting_approval', latest_proposal: 'propose X' });
  const approveCalls = [];
  const rejectCalls = [];
  const api = {
    task: {
      board: () => Promise.resolve(makeBoardLanes({ waiting_approval: [task] })),
      get: () => Promise.resolve({ task, runs: [], events: [], comments: [], attachments: [] }),
      approve: (id, note) => { approveCalls.push({ id: id, note: note }); return Promise.resolve({}); },
      reject: (id, note) => { rejectCalls.push({ id: id, note: note }); return Promise.resolve({}); },
    },
  };
  const ctx = freshEnv(api);
  ctx.NAGENT.tasks.init();
  await tick();

  const board = byId['kanban-board-root'];
  const taskCard = board.children[2].children[1].children[0]; // waiting_approval lane index 2
  (taskCard._listeners.click || []).forEach((fn) => fn());
  await tick();

  const noteInput = byId['tasks-approval-note'];
  ok(!!noteInput, 'approval note textarea registered by id');
  ok(noteInput && noteInput.maxLength === 2000, 'note textarea maxLength 2000');
  if (!noteInput) return;
  noteInput.value = '  proceed  ';

  const approveBtn = created.find((n) => n.tag === 'button' && n.textContent === '批准');
  ok(!!approveBtn, 'approve button present');
  (approveBtn._listeners.click || []).forEach((fn) => fn());
  // Busy: both buttons disabled while promise pending.
  ok(approveBtn.disabled === true, 'approve button disabled while in-flight');
  await tick();
  await tick();

  ok(approveCalls.length === 1, 'approve called once (got ' + approveCalls.length + ')');
  ok(approveCalls[0] && approveCalls[0].id === 'w1' && approveCalls[0].note === 'proceed',
     'approve called with trimmed note (got ' + JSON.stringify(approveCalls[0]) + ')');

  // Empty note -> null (backward-compatible empty POST).
  noteInput.value = '   ';
  const rejectBtn = created.find((n) => n.tag === 'button' && n.textContent === '拒绝');
  (rejectBtn._listeners.click || []).forEach((fn) => fn());
  await tick();
  await tick();
  ok(rejectCalls.length === 1, 'reject called once (got ' + rejectCalls.length + ')');
  ok(rejectCalls[0] && rejectCalls[0].note === null, 'reject called with null note when empty (got ' + JSON.stringify(rejectCalls[0]) + ')');
}

(async () => {
  await testRendersQueuedCards();
  await testEmptyBoardNoCrash();
  await testUnsupportedLaneDropUsesStandardModal();
  await testCardDetailUsesStandardModal();
  await testToolbarOnlyCreateButton();
  await testCreateModalSubmits();
  await testCreateModalValidatesEmptyTitle();
  await testCreateModalEscapeCloses();
  await testCreateModalBackdropClickCloses();
  await testCardsDoNotRenderDeleteButton();
  await testDetailDeleteButtonOnTerminalTask();
  await testApprovalNoteTextareaAndSubmit();
  if (failures) { console.error('\n' + failures + ' test(s) failed'); process.exit(1); }
  console.log('tasks_frontend_harness: all tests passed');
  process.exit(0);
})();
