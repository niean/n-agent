'use strict';
// Harness for chat.js /task slash command. Loads chat.js in a Node vm with
// minimal DOM/api stubs and exercises parseTaskCommand (pure), runTaskCommand
// (dispatch + error mapping + 任务指令 system message) and send() routing.
// Run: node tests/interfaces/chat_frontend_harness.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CHAT_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'chat.js');
const code = fs.readFileSync(CHAT_JS, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// --- stub element -----------------------------------------------------------
function makeEl() {
  const kids = [];
  const el = {
    className: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    style: {},
    dataset: {},
    hidden: false,
    tagName: 'DIV',
    _kids: kids,
    _listeners: {},
    appendChild(c) { c.parentNode = this; kids.push(c); return c; },
    removeChild(c) { const i = kids.indexOf(c); if (i >= 0) kids.splice(i, 1); c.parentNode = null; return c; },
    replaceChild(next, previous) { const i = kids.indexOf(previous); if (i >= 0) { previous.parentNode = null; next.parentNode = this; kids[i] = next; } return previous; },
    replaceChildren() { kids.forEach((k) => { k.parentNode = null; }); kids.length = 0; },
    append(...cs) { cs.forEach((c) => { c.parentNode = this; kids.push(c); }); },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener() {},
    dispatchEvent(ev) { (this._listeners[(ev && ev.type) || ''] || []).forEach((fn) => fn(ev)); },
    querySelector(selector) {
      const tag = String(selector || '').toUpperCase();
      const visit = (node) => {
        if (!node) return null;
        if (node.tagName === tag) return node;
        for (const child of node._kids || []) {
          const found = visit(child);
          if (found) return found;
        }
        return null;
      };
      return visit(this);
    },
    querySelectorAll() { return []; },
    setAttribute() {}, getAttribute() { return null; },
    focus() {}, click() { this.dispatchEvent({ type: 'click' }); },
    get firstChild() { return kids[0] || null; },
    get textContent() {
      return kids.map((c) => (c && typeof c._text === 'string') ? c._text : (c && c.textContent) || '').join('');
    },
    set textContent(v) { kids.length = 0; kids.push({ _text: String(v) }); },
  };
  return el;
}

// --- api stub (records calls) ----------------------------------------------
const taskCalls = [];
function taskApi() {
  return {
    create(payload) { taskCalls.push({ name: 'create', payload }); return Promise.resolve({ id: 't_1', title: payload && payload.title }); },
    list() { taskCalls.push({ name: 'list' }); return Promise.resolve({ items: listItems.slice() }); },
    get(id) { taskCalls.push({ name: 'get', id }); return Promise.resolve({ id, status: 'waiting_approval' }); },
    approve(id, note) { taskCalls.push({ name: 'approve', id, note }); return Promise.resolve({}); },
    reject(id, note) { taskCalls.push({ name: 'reject', id, note }); return Promise.resolve({}); },
    cancel(id) { taskCalls.push({ name: 'cancel', id }); return Promise.resolve({}); },
    retry(id) { taskCalls.push({ name: 'retry', id }); return Promise.resolve({}); },
    revise(id, note) { taskCalls.push({ name: 'revise', id, note }); return Promise.resolve({}); },
  };
}

let listItems = [];
let fetchCalls = [];
let createdSessionId = null;
let appendCalls = [];
let sessionsList = [];

function makeTimerEnv() {
  const timers = [];
  const setInterval = (fn, ms) => { const id = timers.length + 1; timers.push({ id, fn, ms }); return id; };
  const clearInterval = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };
  const tickTimers = () => { for (const t of [...timers]) t.fn(); };
  const timerCount = () => timers.length;
  return { setInterval, clearInterval, tickTimers, timerCount };
}

function freshStubs() {
  taskCalls.length = 0;
  fetchCalls = [];
  listItems = [];
  createdSessionId = null;
  appendCalls = [];
  sessionsList = [];
  const messageStack = makeEl();
  const input = makeEl();
  input.value = '';
  // chat-messages 是滚动容器；赋予可读写 scrollTop/scrollHeight/clientHeight 供滚动测试
  const messagesScroll = makeEl();
  messagesScroll.scrollTop = 0;
  messagesScroll.scrollHeight = 1000;
  messagesScroll.clientHeight = 800;
  const byIdMap = {
    'chat-message-stack': messageStack,
    'chat-input': input,
    'chat-messages': messagesScroll,
    'chat-session-list': makeEl(),
    'chat-header': makeEl(),
    'chat-summary': makeEl(),
    'chat-task-state': makeEl(),
    'chat-tool-calls': makeEl(),
  };
  const ui = {
    byId: (id) => (byIdMap[id] !== undefined ? byIdMap[id] : makeEl()),
    el: () => makeEl(),
    clear: () => {},
    renderEmpty: () => {},
    renderError: () => {},
  };
  const api = {
    task: taskApi(),
    createSession: (id) => { createdSessionId = id; return Promise.resolve({ id }); },
    appendSessionMessage: (id, content) => {
      appendCalls.push({ id, content });
      return Promise.resolve({ id: 'msg_' + appendCalls.length, role: 'system', content });
    },
    listSessions: () => Promise.resolve(sessionsList.slice()),
    getAdminModels: () => Promise.resolve({}),
    getSessionDetail: () => Promise.resolve({ messages: [] }),
    getSessionToolCalls: () => Promise.resolve([]),
    renameSession: () => Promise.resolve({}),
    deleteSession: () => Promise.resolve({}),
  };
  const createdElements = [];
  const document = {
    _elements: createdElements,
    _listeners: {},
    createElement: (tagName) => { const el = makeEl(); el.tagName = String(tagName || 'div').toUpperCase(); createdElements.push(el); return el; },
    createTextNode: (t) => ({ _text: String(t) }),
    getElementById: (id) => (byIdMap[id] !== undefined ? byIdMap[id] : null),
    querySelector: () => null,
    querySelectorAll: (sel) => {
      const cls = String(sel || '').replace(/^\./, '');
      return createdElements.filter((el) => el.className && el.className.indexOf(cls) !== -1);
    },
    addEventListener: (type, fn) => { (document._listeners[type] = document._listeners[type] || []).push(fn); },
    hidden: false,
    visibilityState: 'visible',
    body: makeEl(),
  };
  const fetchStub = (url, opts) => {
    fetchCalls.push({ url, opts });
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({}),
      body: { getReader: () => ({ read: async () => ({ done: true }) }) },
    });
  };
  const win = {
    NAGENT: { api, ui, modal: { confirm: () => Promise.resolve(true), alert: () => {} } },
    _listeners: {},
    document,
    location: { protocol: 'http:', host: 'x' },
    crypto: { randomUUID: () => 'stub-uuid' },
    fetch: fetchStub,
    WebSocket: function () {},
    TextDecoder: function () { this.decode = () => ''; },
    addEventListener: (type, fn) => { (win._listeners[type] = win._listeners[type] || []).push(fn); },
  };
  const timerEnv = makeTimerEnv();
  return { win, messageStack, input, messagesScroll, byIdMap, document, timerEnv };
}

// Load chat.js into a fresh context. Calls init() so visibilitychange/beforeunload
// listeners are bound; exposes timer/visibility/session-item helpers for auto-refresh tests.
function loadChat() {
  const env = freshStubs();
  const ctx = {
    NAGENT: env.win.NAGENT,
    document: env.win.document,
    window: env.win,
    console,
    fetch: env.win.fetch,
    crypto: env.win.crypto,
    WebSocket: env.win.WebSocket,
    TextDecoder,
    TextEncoder,
    setInterval: env.timerEnv.setInterval,
    clearInterval: env.timerEnv.clearInterval,
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  env.chat = ctx.NAGENT.chat;
  // Note: init() is NOT called here. Tests that need visibilitychange/beforeunload
  // listeners or a populated session list call env.chat.init() after setting
  // sessionsList, so init's loadSessions builds the expected session items.
  env.tickTimers = env.timerEnv.tickTimers;
  env.timerCount = env.timerEnv.timerCount;
  env.setHidden = (h) => {
    env.win.document.hidden = !!h;
    env.win.document.visibilityState = h ? 'hidden' : 'visible';
    (env.win.document._listeners.visibilitychange || []).forEach((fn) => fn({ type: 'visibilitychange' }));
  };
  env.fireBeforeUnload = () => {
    (env.win._listeners.beforeunload || []).forEach((fn) => fn({ type: 'beforeunload' }));
  };
  env.findSessionItem = (sessionId) => {
    const items = env.win.document.querySelectorAll('.session-item');
    for (const item of items) {
      const titleBtn = item._kids && item._kids[0];
      if (titleBtn && (titleBtn.textContent === sessionId || titleBtn.textContent === sessionId)) return titleBtn;
    }
    return null;
  };
  env.fireClick = (el) => { if (el) el.click(); };
  env.waitMicro = () => new Promise((res) => setTimeout(res, 0));
  return env;
}

function lastSystemText(env) {
  const kids = env.messageStack._kids;
  if (!kids.length) return null;
  return kids[kids.length - 1].textContent;
}

// Extract summary / body of the last system message (details > [summary, pre]).
function _lastSystemDetails(env) {
  const kids = env.messageStack._kids;
  if (!kids.length) return null;
  const el = kids[kids.length - 1];
  return (el && el._kids && el._kids[0]) ? el._kids[0] : null;
}
function lastSystemSummary(env) {
  const d = _lastSystemDetails(env);
  const s = d && d._kids && d._kids[0];
  return s ? s.textContent : null;
}
function lastSystemBody(env) {
  const d = _lastSystemDetails(env);
  const p = d && d._kids && d._kids[1];
  return p ? p.textContent : null;
}

// ===========================================================================
// 增量消息刷新：旧 details 节点必须保留展开状态，新增/合并的尾部消息只更新对应节点。
// ===========================================================================
async function testPartialMessageRefresh() {
  const env = loadChat();
  const first = { messages: [{ id: 'tool-1', role: 'tool', content: '{"step":1}' }] };
  await env.chat.applySessionDetail(first);
  const oldTool = env.messageStack._kids[0];
  oldTool._kids[0].open = true;

  await env.chat.applySessionDetail({
    messages: [
      { id: 'tool-1', role: 'tool', content: '{"step":1}' },
      { id: 'tool-2', role: 'tool', content: '{"step":2}' },
    ],
  }, { partialMessages: true });
  const refreshedTool = env.messageStack._kids[0];
  ok(refreshedTool !== oldTool, 'merged tool group updates only its changed node');
  ok(refreshedTool._kids[0].open === true, 'expanded tool call remains expanded after partial refresh');

  const taskEnv = loadChat();
  await taskEnv.chat.applySessionDetail({
    messages: [{ id: 'task-1', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 等待执行' }],
  });
  const oldTask = taskEnv.messageStack._kids[0];
  oldTask._kids[0].open = true;
  await taskEnv.chat.applySessionDetail({
    messages: [
      { id: 'task-1', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 等待执行' },
      { id: 'task-2', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 已完成' },
    ],
  }, { partialMessages: true });
  const refreshedTask = taskEnv.messageStack._kids[0];
  ok(refreshedTask !== oldTask, 'merged task status updates only its changed node');
  ok(refreshedTask._kids[0].open === true, 'expanded task status remains expanded after partial refresh');
}

// ===========================================================================
// T1: parseTaskCommand (pure)
// ===========================================================================
const env0 = loadChat();
const parse = env0.chat.parseTaskCommand;
ok(typeof parse === 'function', 'parseTaskCommand exposed');

let r;
r = parse('/task create 完成报告 --body 生成 Q3 总结 --priority 2 --goal');
ok(r.subcommand === 'create' && r.title === '完成报告' && r.body === '生成 Q3 总结' && r.priority === 2 && r.goal === true, 'create with named args (got ' + JSON.stringify(r) + ')');

r = parse('/task create 修复登录问题');
ok(r.subcommand === 'create' && r.title === '修复登录问题', 'create multi-word title (got ' + JSON.stringify(r) + ')');

r = parse('/task create "完成 季度 报告"');
ok(r.subcommand === 'create' && r.title === '完成 季度 报告', 'create double-quoted title (got ' + JSON.stringify(r) + ')');

r = parse("/task create '单引号 标题'");
ok(r.subcommand === 'create' && r.title === '单引号 标题', 'create single-quoted title (got ' + JSON.stringify(r) + ')');

r = parse('/task create 报告 --body "含 -- 破折号 的正文"');
ok(r.subcommand === 'create' && r.title === '报告' && r.body === '含 -- 破折号 的正文', 'create quoted body protects -- (got ' + JSON.stringify(r) + ')');

r = parse('/task create');
ok(!!r.error && r.error.indexOf('标题') !== -1, 'create missing title errors (got ' + JSON.stringify(r) + ')');

r = parse('/task create foo --body');
ok(!!r.error && r.error.indexOf('需要值') !== -1, 'create --body missing value errors (got ' + JSON.stringify(r) + ')');

r = parse('/task create foo --priority abc');
ok(!!r.error && r.error.indexOf('整数') !== -1, 'create --priority non-integer errors (got ' + JSON.stringify(r) + ')');

r = parse('/task create foo --unknown x');
ok(!!r.error && r.error.indexOf('未知参数') !== -1, 'create --unknown errors (got ' + JSON.stringify(r) + ')');

r = parse('/task cancel t_1 --note x');
ok(!!r.error && r.error.indexOf('不适用') !== -1, 'cancel rejects --note (got ' + JSON.stringify(r) + ')');

r = parse('/task create "未闭合');
ok(!!r.error && r.error.indexOf('未闭合') !== -1, 'unclosed quote errors (got ' + JSON.stringify(r) + ')');

r = parse('/task list');
ok(r.subcommand === 'list', 'list parsed (got ' + JSON.stringify(r) + ')');

r = parse('/task list extra');
ok(!!r.error && r.error.indexOf('不接受额外参数') !== -1, 'list rejects extra positional (got ' + JSON.stringify(r) + ')');

r = parse('/task approve t_123 --note 同意');
ok(r.subcommand === 'approve' && r.id === 't_123' && r.note === '同意', 'approve with note (got ' + JSON.stringify(r) + ')');

r = parse('/task reject t_456 --note 风险');
ok(r.subcommand === 'reject' && r.id === 't_456' && r.note === '风险', 'reject with note (got ' + JSON.stringify(r) + ')');

r = parse('/task cancel t_789');
ok(r.subcommand === 'cancel' && r.id === 't_789', 'cancel parsed (got ' + JSON.stringify(r) + ')');

r = parse('/task retry t_000');
ok(r.subcommand === 'retry' && r.id === 't_000', 'retry parsed (got ' + JSON.stringify(r) + ')');

r = parse('/task approve t_1 t_2');
ok(!!r.error && r.error.indexOf('不接受多个 id') !== -1, 'approve rejects second id (got ' + JSON.stringify(r) + ')');

r = parse('/task frobnicate x');
ok(!!r.error && r.error.indexOf('未知子命令') !== -1, 'unknown subcommand errors (got ' + JSON.stringify(r) + ')');

r = parse('/task approve');
ok(!!r.error && r.error.indexOf('task id') !== -1, 'approve missing id errors (got ' + JSON.stringify(r) + ')');

r = parse('/task');
ok(!!r.error && r.error.indexOf('用法') !== -1, 'bare /task shows usage (got ' + JSON.stringify(r) + ')');

// ===========================================================================
// T2/T3: runTaskCommand via send() - dispatch, prefix, error map, routing
// ===========================================================================
async function runIntegration() {
  // create: binds to currentSessionId, 任务指令 system message, fetch not called
  let env = loadChat();
  env.input.value = '/task create 报告 --body 正文 --priority 2 --goal';
  await env.chat.send();
  const createCall = taskCalls.find((c) => c.name === 'create');
  ok(!!createCall, 'create called api.task.create');
  ok(createCall && createCall.payload.title === '报告' && createCall.payload.body === '正文' && createCall.payload.priority === 2 && createCall.payload.goal_mode === true, 'create payload maps body/priority/goal_mode (got ' + JSON.stringify(createCall && createCall.payload) + ')');
  ok(createCall && createdSessionId && createCall.payload.origin_session_id === createdSessionId, 'create binds origin_session_id to current session (got ' + JSON.stringify(createCall && createCall.payload.origin_session_id) + ', session=' + createdSessionId + ')');
  ok(fetchCalls.length === 0, 'create does not call /chat/completions');
  let msg = lastSystemText(env);
  ok(typeof msg === 'string' && msg.indexOf('执行命令') !== -1 && msg.indexOf('已创建任务 t_1') !== -1 && msg.indexOf('[任务指令]') === -1, 'create system message has command record + task id, no header (got ' + JSON.stringify(msg) + ')');
  ok(env.input.value === '', 'create cleared input');

  // list: filters by origin_session_id, does not leak other sessions
  env = loadChat();
  env.input.value = '/task create 本会话任务';
  await env.chat.send();
  const sid = createdSessionId;
  listItems = [
    { id: 't_a', status: 'todo', title: '本会话任务', origin_session_id: sid },
    { id: 't_b', status: 'todo', title: '其它会话任务', origin_session_id: 'other-session' },
  ];
  env.input.value = '/task list';
  await env.chat.send();
  const listMsg = lastSystemText(env);
  ok(typeof listMsg === 'string' && listMsg.indexOf('本会话任务') !== -1 && listMsg.indexOf('其它会话任务') === -1, 'list filters by current session, no leak (got ' + JSON.stringify(listMsg) + ')');
  ok(fetchCalls.length === 0, 'list does not call /chat/completions');

  // empty list
  env = loadChat();
  env.input.value = '/task list';
  await env.chat.send();
  ok(lastSystemText(env).indexOf('当前会话无关联任务') !== -1, 'list empty shows 无关联任务 (got ' + JSON.stringify(lastSystemText(env)) + ')');

  // approve/reject pass id + note; cancel/retry pass id only
  env = loadChat();
  env.input.value = '/task approve t_7 --note 同意';
  await env.chat.send();
  let c = taskCalls.find((x) => x.name === 'approve');
  ok(c && c.id === 't_7' && c.note === '同意', 'approve passes id+note (got ' + JSON.stringify(c) + ')');
  ok(lastSystemText(env).indexOf('已批准任务 t_7') !== -1 && lastSystemText(env).indexOf('[任务指令]') === -1, 'approve message, no header (got ' + JSON.stringify(lastSystemText(env)) + ')');

  env = loadChat();
  env.input.value = '/task reject t_8 --note 风险';
  await env.chat.send();
  c = taskCalls.find((x) => x.name === 'reject');
  ok(c && c.id === 't_8' && c.note === '风险', 'reject passes id+note (got ' + JSON.stringify(c) + ')');

  env = loadChat();
  env.input.value = '/task cancel t_9';
  await env.chat.send();
  c = taskCalls.find((x) => x.name === 'cancel');
  ok(c && c.id === 't_9' && c.note === undefined, 'cancel passes id only (got ' + JSON.stringify(c) + ')');

  env = loadChat();
  env.input.value = '/task retry t_10';
  await env.chat.send();
  c = taskCalls.find((x) => x.name === 'retry');
  ok(c && c.id === 't_10' && c.note === undefined, 'retry passes id only (got ' + JSON.stringify(c) + ')');

  // error code mapping: api rejects with Error('task_not_found')
  env = loadChat();
  env.win.NAGENT.api.task.approve = () => Promise.reject(new Error('task_not_found'));
  env.input.value = '/task approve bad-id';
  await env.chat.send();
  const errMsg = lastSystemText(env);
  ok(errMsg.indexOf('任务不存在') !== -1 && errMsg.indexOf('task_not_found') !== -1 && errMsg.indexOf('[任务指令]') === -1, 'error maps task_not_found with code, no header (got ' + JSON.stringify(errMsg) + ')');
  ok(fetchCalls.length === 0, 'error path does not call /chat/completions');

  // unknown error: safe generic message includes String(error)
  env = loadChat();
  env.win.NAGENT.api.task.cancel = () => Promise.reject(new Error('something_unexpected'));
  env.input.value = '/task cancel t_x';
  await env.chat.send();
  ok(lastSystemText(env).indexOf('something_unexpected') !== -1, 'unknown error includes raw code (got ' + JSON.stringify(lastSystemText(env)) + ')');

  // parser error: no api call, no fetch, 任务指令 usage message
  env = loadChat();
  env.input.value = '/task frobnicate x';
  await env.chat.send();
  ok(taskCalls.length === 0 && fetchCalls.length === 0, 'parser error calls no api and no fetch');
  ok(lastSystemText(env).indexOf('未知子命令') !== -1 && lastSystemText(env).indexOf('[任务指令]') === -1, 'parser error renders usage, no header (got ' + JSON.stringify(lastSystemText(env)) + ')');

  // safe rendering: a malicious title must not produce an IMG/innerHTML injection
  env = loadChat();
  env.win.NAGENT.api.task.create = (p) => { taskCalls.push({ name: 'create', payload: p }); return Promise.resolve({ id: 't_z', title: '<img src=x onerror=alert(1)>' }); };
  env.input.value = '/task create 标题';
  await env.chat.send();
  const lastKid = env.messageStack._kids[env.messageStack._kids.length - 1];
  const hasImg = env.messageStack._kids.some((k) => k._kids && k._kids.some((gc) => gc.tagName === 'IMG'));
  ok(!hasImg, 'malicious title renders no IMG node (textContent only)');
  ok(lastKid && lastKid.textContent.indexOf('<img src=x onerror=alert(1)>') !== -1, 'malicious title preserved as text (got ' + JSON.stringify(lastKid && lastKid.textContent) + ')');

  // non-command routing: plain text calls /chat/completions, no task api
  env = loadChat();
  env.input.value = '帮我写一段总结';
  await env.chat.send();
  ok(fetchCalls.some((f) => f.url === '/chat/completions'), 'non-command calls /chat/completions');
  ok(taskCalls.length === 0, 'non-command calls no task api');

  // === persistence: /task command record + result persist to session ===
  env = loadChat();
  env.input.value = '/task create 报告';
  await env.chat.send();
  ok(appendCalls.length >= 2, 'create persisted command record + result (got ' + appendCalls.length + ')');
  ok(appendCalls.length > 0 && appendCalls[0].id === createdSessionId, 'persist bound to current session (got ' + appendCalls[0].id + ', session=' + createdSessionId + ')');
  ok(appendCalls.length > 0 && appendCalls[0].content.indexOf('执行命令') !== -1, 'command record persisted first (got ' + JSON.stringify(appendCalls[0].content) + ')');
  ok(lastSystemSummary(env) === '任务指令', 'system summary is 任务指令 by name (got ' + JSON.stringify(lastSystemSummary(env)) + ')');
  // 合并：命令记录 + 回执渲染为同一条任务指令气泡（非两条）
  const taskCmdBubbles = env.messageStack._kids.filter(k => k && k.dataset && k.dataset.name === 'ui.task_command');
  ok(taskCmdBubbles.length === 1, 'command record + result merged into 1 task-command bubble (got ' + taskCmdBubbles.length + ')');

  // === truncation: oversize content truncated with suffix; DOM body contains POST body ===
  env = loadChat();
  const big = '中'.repeat(22000);  // ~66000 bytes > 65536
  env.input.value = '/task create ' + big;
  await env.chat.send();
  const lastPersist = appendCalls[appendCalls.length - 1];
  ok(!!lastPersist && lastPersist.content.indexOf('…[内容已截断]') !== -1, 'oversize persisted body truncated with suffix (got tail=' + JSON.stringify((lastPersist && lastPersist.content || '').slice(-30)) + ')');
  ok(!!lastPersist && new TextEncoder().encode(lastPersist.content).length <= 65536, 'truncated body within byte limit (got ' + (lastPersist && new TextEncoder().encode(lastPersist.content).length) + ')');
  ok(lastSystemBody(env).indexOf(lastPersist && lastPersist.content) !== -1, 'DOM body contains POST body (merged block) (got dom tail=' + JSON.stringify((lastSystemBody(env) || '').slice(-30)) + ')');

  // === persistence failure does not block task api; local message still shown ===
  env = loadChat();
  env.win.NAGENT.api.appendSessionMessage = () => Promise.reject(new Error('network'));
  env.input.value = '/task create 报告';
  await env.chat.send();
  ok(taskCalls.some((c) => c.name === 'create'), 'task api still called when persist fails');
  ok(lastSystemText(env) && lastSystemText(env).indexOf('已创建任务') !== -1 && lastSystemText(env).indexOf('[任务指令]') === -1, 'local system message still shown when persist fails, no header');

  // ===========================================================================
  // T4: auto-refresh controller (start/stop, version, single-flight, attribution,
  //     scroll, lifecycle, /task version coordination)
  // ===========================================================================
  // Note: sessionsList must be set AFTER loadChat (freshStubs resets it) but
  // BEFORE env.chat.init() so init's loadSessions builds session items.

  function detailWith(id, msgs) {
    return { session: { id }, messages: msgs, summary: null, task_state: null };
  }

  // --- start/stop + version same skip / different render ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  let s1msgs = [{ id: 'm1', role: 'user', content: 'hi' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve(detailWith(id, s1msgs));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  ok(env.timerCount() === 1, 'auto-refresh timer started after select (got ' + env.timerCount() + ')');
  const stackCountAfterSelect = env.messageStack._kids.length;
  env.tickTimers();
  await env.waitMicro();
  ok(env.messageStack._kids.length === stackCountAfterSelect, 'same version does not re-render (got ' + env.messageStack._kids.length + ')');
  s1msgs = [{ id: 'm1', role: 'user', content: 'hi' }, { id: 'm2', role: 'assistant', content: 'reply' }];
  env.tickTimers();
  await env.waitMicro();
  ok(env.messageStack._kids.length > stackCountAfterSelect, 'new version re-renders (got ' + env.messageStack._kids.length + ')');

  // --- single-flight: in-flight request blocks second tick ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  let resolvePending = null;
  let sfCalls = 0;
  env.win.NAGENT.api.getSessionDetail = () => {
    sfCalls++;
    if (sfCalls === 1) return Promise.resolve(detailWith('s1', [{ id: 'm1', role: 'user', content: 'hi' }]));
    return new Promise((res) => { resolvePending = res; });
  };
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro(); // selectSession completes (call 1), startAutoRefresh
  env.tickTimers(); // call 2: in-flight (pending)
  await env.waitMicro();
  env.tickTimers(); // single-flight: skip
  await env.waitMicro();
  ok(sfCalls === 2, 'single-flight: second tick does not start new request (got sfCalls=' + sfCalls + ')');
  if (resolvePending) resolvePending(detailWith('s1', [{ id: 'm1', role: 'user', content: 'hi' }]));
  await env.waitMicro();

  // --- visibility hidden/visible ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  let visCalls = 0;
  const baseDetail = () => Promise.resolve(detailWith('s1', [{ id: 'm1', role: 'user', content: 'hi' }]));
  env.win.NAGENT.api.getSessionDetail = () => { visCalls++; return baseDetail(); };
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  const visCallsAfterSelect = visCalls;
  env.setHidden(true);
  ok(env.timerCount() === 0, 'hidden stops timer (got ' + env.timerCount() + ')');
  env.setHidden(false);
  await env.waitMicro();
  ok(env.timerCount() === 1, 'visible restarts timer (got ' + env.timerCount() + ')');
  ok(visCalls > visCallsAfterSelect, 'visible triggers immediate catch-up (got visCalls=' + visCalls + ', afterSelect=' + visCallsAfterSelect + ')');

  // --- beforeunload stops timer ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  env.win.NAGENT.api.getSessionDetail = () => baseDetail();
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  ok(env.timerCount() === 1, 'timer active before unload (got ' + env.timerCount() + ')');
  env.fireBeforeUnload();
  ok(env.timerCount() === 0, 'beforeunload stops timer (got ' + env.timerCount() + ')');

  // --- session_not_found stops + clears; network error keeps timer ---
  env = loadChat();
  sessionsList = [];
  env.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('session_not_found'));
  env.input.value = 'hello';
  await env.chat.send(); // ensureSession -> startAutoRefresh; refreshCurrentSession -> session_not_found (caught, no stop)
  await env.waitMicro();
  env.tickTimers(); // autoRefreshTick -> session_not_found -> stopAutoRefresh
  await env.waitMicro();
  ok(env.timerCount() === 0, 'session_not_found stops timer (got ' + env.timerCount() + ')');

  env = loadChat();
  sessionsList = [];
  env.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network_failed'));
  env.input.value = 'hello';
  await env.chat.send();
  await env.waitMicro();
  env.tickTimers();
  await env.waitMicro();
  ok(env.timerCount() === 1, 'network error keeps timer (got ' + env.timerCount() + ')');

  // --- /task version coordination: persist returns real id, next poll no spurious re-render ---
  // /task list does 2 taskSystemMessage (command record + result), each persists 1 system msg.
  // After: renderedMessageVersion = {count: 1+2=3, lastId: id of 2nd persist}.
  env = loadChat();
  sessionsList = [];
  let coordMsgs = [{ id: 'm1', role: 'user', content: 'hi' }];
  env.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', coordMsgs));
  env.input.value = 'hello';
  await env.chat.send(); // ensureSession; refreshCurrentSession sets version {count:1,lastId:'m1'}
  await env.waitMicro();
  // appendSessionMessage returns distinct ids; /task list persists 2 messages
  const coordAppendBase = appendCalls.length;
  env.input.value = '/task list';
  await env.chat.send();
  await env.waitMicro();
  const lastPersistedId = 'msg_' + appendCalls.length; // 2nd persist id
  // Next poll returns detail with [m1, sys1, sys2] matching version {count:3, lastId:lastPersistedId}
  coordMsgs = [
    { id: 'm1', role: 'user', content: 'hi' },
    { id: 'msg_' + (coordAppendBase + 1), role: 'system', content: '执行命令: /task list' },
    { id: lastPersistedId, role: 'system', content: '当前会话无关联任务' },
  ];
  const beforePoll = env.messageStack._kids.length;
  env.tickTimers();
  await env.waitMicro();
  ok(env.messageStack._kids.length === beforePoll, '/task coordination: next poll no spurious re-render (got ' + env.messageStack._kids.length + ', before=' + beforePoll + ')');

  // A refresh can race the local optimistic append before its version advance.
  // The server returns the same task-command group keyed by the first persisted
  // message; it must replace the local card, not append a duplicate card.
  env = loadChat();
  sessionsList = [];
  let raceMsgs = [{ id: 'm1', role: 'user', content: 'hi' }];
  env.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', raceMsgs));
  env.input.value = 'hello';
  await env.chat.send();
  await env.waitMicro();
  const raceAppendBase = appendCalls.length;
  env.input.value = '/task list';
  await env.chat.send();
  await env.waitMicro();
  raceMsgs = [
    { id: 'm1', role: 'user', content: 'hi' },
    { id: 'msg_' + (raceAppendBase + 1), role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    { id: 'msg_' + (raceAppendBase + 2), role: 'system', name: 'ui.task_command', content: '当前会话无关联任务' },
  ];
  await env.chat.applySessionDetail(detailWith('s1', raceMsgs), { partialMessages: true });
  const raceTaskCards = env.messageStack._kids.filter((k) => k && k.dataset && k.dataset.name === 'ui.task_command');
  ok(raceTaskCards.length === 1, 'task command refresh race replaces optimistic card instead of duplicating it (got ' + raceTaskCards.length + ')');

  // ===========================================================================
  // T5: 任务状态合并卡片默认折叠；等待批准交互卡片（带 card payload）默认展开
  //     合并卡片（ui.task_lifecycle 无 card 相邻合并）默认 open=false；
  //     ui.task_command 独立不参与合并（断开合并链）；交互卡片（带 card）默认展开（T6/T7 覆盖）。
  // ===========================================================================
  function findSystemDetailsByContent(stack, needle) {
    const kids = stack._kids || [];
    for (const k of kids) {
      if (!k.textContent || k.textContent.indexOf(needle) === -1) continue;
      const details = k._kids && k._kids[0];
      if (details && 'open' in details) return details;
    }
    return null;
  }

  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  const lifecycleMsgs = [
    { id: 'm1', role: 'user', content: '帮我调研 X' },
    { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 开始运行: t_1 - 调研X' },
    { id: 'm3', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 等待批准: t_1 - 调研X | 提案: 修改方案A' },
    { id: 'm4', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 已完成: t_1 - 调研X | 已交付' },
    { id: 'm5', role: 'system', name: 'ui.task_command', content: '[任务指令] 执行命令: /task list' },
  ];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve(detailWith(id, lifecycleMsgs));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();

  // 相邻的 ui.task_lifecycle 无 card（m2-m4）合并为一个卡片；ui.task_command（m5）独立不参与合并
  const mergedDetails = findSystemDetailsByContent(env.messageStack, '等待批准');
  ok(!!mergedDetails, 'merged task card rendered (contains 等待批准)');
  // 合并卡片默认折叠（open=false），即使 content 含 等待批准 文本（仅带 card payload 的交互卡片才默认展开）
  ok(mergedDetails && mergedDetails.open === false, 'merged task card folded by default (open=false) (got open=' + (mergedDetails && mergedDetails.open) + ')');
  // 合并卡片 summary 固定 = 任务状态
  const mergedSummary = mergedDetails && mergedDetails._kids && mergedDetails._kids[0];
  ok(mergedSummary && mergedSummary.textContent === '任务状态', 'merged card summary = 任务状态 (got ' + JSON.stringify(mergedSummary && mergedSummary.textContent) + ')');
  // 历史消息（带 [任务状态] 抬头）渲染时剥离行首抬头，正文保留
  function detailsPreText(details) {
    if (!details || !details._kids) return '';
    const pre = details._kids[1];
    return pre ? (pre.textContent || '') : '';
  }
  // 合并卡片含 m2-m4 全部 lifecycle 内容（不含 m5 执行命令，m5 独立）
  const mergedText = detailsPreText(mergedDetails);
  ok(mergedText.indexOf('开始运行') !== -1 && mergedText.indexOf('等待批准') !== -1 && mergedText.indexOf('已完成') !== -1, 'merged card contains m2-m4 lifecycle content (got ' + JSON.stringify(mergedText) + ')');
  ok(mergedText.indexOf('执行命令') === -1, 'merged card does NOT contain m5 task_command (independent) (got ' + JSON.stringify(mergedText) + ')');
  ok(mergedText.indexOf('[任务状态]') === -1, 'merged card headers stripped, content kept');
  // m2-m4 合并为 1 卡片 + m5 独立 1 卡片 = 2 张 task 卡片（非 1 张，非 4 张）
  const taskCards = env.messageStack._kids.filter(k => k && k.dataset && (k.dataset.name === 'ui.task_command' || k.dataset.name === 'ui.task_lifecycle'));
  ok(taskCards.length === 2, 'm2-m4 merged into 1 + m5 independent = 2 task cards (got ' + taskCards.length + ')');
  // m5 task_command 独立卡片 summary = 任务指令
  const cmdCard = taskCards.find((k) => k.dataset.name === 'ui.task_command');
  ok(!!cmdCard, 'm5 task_command card exists (independent)');
  const cmdDetails = cmdCard && cmdCard._kids && cmdCard._kids[0];
  const cmdSummary = cmdDetails && cmdDetails._kids && cmdDetails._kids[0];
  ok(cmdSummary && cmdSummary.textContent === '任务指令', 'm5 task_command summary = 任务指令 (got ' + JSON.stringify(cmdSummary && cmdSummary.textContent) + ')');

  // ===========================================================================
  // T6: task final result (ui.task_result) renders as a regular message
  //     所有任务结束情况（成功/失败/取消/过期）均以普通消息渲染，非可折叠状态卡片
  // ===========================================================================
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  const resultMsgs = [
    { id: 'm1', role: 'user', content: '帮我调研 X' },
    { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1 - 调研X' },
    { id: 'm3', role: 'assistant', content: '正在处理调研' },
    { id: 'm4', role: 'system', name: 'ui.task_result', content: '任务已完成：调研X\n\n已交付调研报告', created_at: '2026-07-21T10:00:00Z' },
    { id: 'm5', role: 'system', name: 'ui.task_result', content: '任务已失败：其它任务\n\n工具不可用', created_at: '2026-07-21T11:00:00Z' },
    { id: 'm6', role: 'system', name: 'ui.task_result', content: '任务已取消：取消任务', created_at: '2026-07-21T12:00:00Z' },
  ];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve(detailWith(id, resultMsgs));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();

  function findResultEl(needle) {
    return env.messageStack._kids.find((k) => k && k.className === 'msg assistant' && k.textContent && k.textContent.indexOf(needle) !== -1);
  }
  // 所有结束情况均渲染为普通消息（className 'msg assistant'）
  const succeededEl = findResultEl('任务已完成');
  const failedEl = findResultEl('任务已失败');
  const cancelledEl = findResultEl('任务已取消');
  ok(!!succeededEl, 'SUCCEEDED ui.task_result rendered as regular message');
  ok(!!failedEl, 'FAILED ui.task_result rendered as regular message');
  ok(!!cancelledEl, 'CANCELLED ui.task_result rendered as regular message');
  // 均非可折叠卡片：子节点无 <details>（无 open 属性）
  [succeededEl, failedEl, cancelledEl].forEach((el, i) => {
    const isCard = !!(el && el._kids && el._kids.some((k) => k && typeof k === 'object' && 'open' in k));
    ok(!isCard, 'ui.task_result #' + i + ' not rendered as collapsible card');
  });
  // 正文以普通消息渲染（textContent 可见）
  ok(succeededEl && succeededEl.textContent.indexOf('已交付调研报告') !== -1, 'SUCCEEDED content visible');
  ok(failedEl && failedEl.textContent.indexOf('工具不可用') !== -1, 'FAILED content visible');
  // 普通消息附带 Hover 时间
  ok(succeededEl && succeededEl.dataset && succeededEl.dataset.time, 'ui.task_result has hover time');
}

// ===========================================================================
// createMessageElement: 非真人进程来源 user 消息渲染为左对齐状态卡片
// work task / judge task -> 折叠卡片，样式打平 ui.task_lifecycle 任务状态卡片
//   （className `msg system`，与 ui.task_command/ui.task_lifecycle 一致）：
//   summary 显示固定标题（work task -> 任务状态、judge task -> 判断完成），
//   pre 放原始 content，details 默认 open=false；
// schedule/curator 等其它进程消息 -> 非折叠左对齐状态卡片（msg--process-card）
// ===========================================================================
(function testProcessSourceRendering() {
  const env = loadChat();
  const createMessageElement = env.chat.createMessageElement;
  ok(typeof createMessageElement === 'function', 'createMessageElement exposed');

  function hasDetails(el) {
    return !!(el && el._kids && el._kids.some((k) => k && typeof k === 'object' && 'open' in k));
  }
  function findDetails(el) {
    if (!el || !el._kids) return null;
    return el._kids.find((k) => k && typeof k === 'object' && 'open' in k) || null;
  }

  // ui.task_lifecycle 任务状态卡片 className，用于样式打平对比
  const lifecycleEl = createMessageElement({ role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1 - 调研X' });
  const lifecycleClassName = lifecycleEl.className;
  ok(lifecycleClassName === 'msg system', 'ui.task_lifecycle renders msg system (got ' + lifecycleClassName + ')');

  // task work -> 折叠卡片（details，默认 open=false）+ summary = 查询状态: <content>（前缀+content）
  let el = createMessageElement({ role: 'user', source: 'task', content: 'work task t1' });
  ok(el.className === lifecycleClassName,
    'task work renders same className as ui.task_lifecycle (got ' + el.className + ', want ' + lifecycleClassName + ')');
  ok(el.className.indexOf('msg--process-card') === -1 && el.className.indexOf('msg--process-folded') === -1,
    'task work not using process-card/process-folded classes (got ' + el.className + ')');
  ok(!el.dataset || el.dataset.source === undefined,
    'task work card has no dataset.source (aligned with ui.task_lifecycle)');
  ok(hasDetails(el), 'task work card is a details (collapsible)');
  let d = findDetails(el);
  ok(d && d.open === false, 'task work card folded by default (open=false) (got open=' + (d && d.open) + ')');
  let sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '查询状态: work task t1',
    'work task summary = 查询状态: work task t1 (prefix+content) (got ' + JSON.stringify(sm && sm.textContent) + ')');
  let pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === 'work task t1',
    'work task pre = raw content (got ' + JSON.stringify(pre && pre.textContent) + ')');

  // task judge -> 折叠卡片 + summary = 判断结束: <content>（前缀+content，注意"结束"非"完成"）
  el = createMessageElement({ role: 'user', source: 'task', content: 'judge task t_xxx: has the goal been achieved?' });
  ok(el.className === lifecycleClassName,
    'judge task renders same className as ui.task_lifecycle (got ' + el.className + ', want ' + lifecycleClassName + ')');
  ok(el.className.indexOf('msg--process-card') === -1 && el.className.indexOf('msg--process-folded') === -1,
    'judge task not using process-card/process-folded classes (got ' + el.className + ')');
  ok(!el.dataset || el.dataset.source === undefined,
    'judge task card has no dataset.source (aligned with ui.task_lifecycle)');
  ok(hasDetails(el), 'judge task card is a details (collapsible)');
  d = findDetails(el);
  ok(d && d.open === false, 'judge task card folded by default (open=false) (got open=' + (d && d.open) + ')');
  sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '判断结束: judge task t_xxx: has the goal been achieved?',
    'judge task summary = 判断结束: judge task... (prefix+content, 结束 not 完成) (got ' + JSON.stringify(sm && sm.textContent) + ')');
  pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === 'judge task t_xxx: has the goal been achieved?',
    'judge task pre = raw content (got ' + JSON.stringify(pre && pre.textContent) + ')');

  // 其他 task 进程消息（无 work/judge task 前缀）-> 非折叠左对齐状态卡片，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'some other task content' });
  ok(el.className === 'msg msg--process-card', 'other task content renders msg--process-card (non-folded) (got ' + el.className + ')');
  ok(!hasDetails(el), 'other task content not collapsible (no details)');
  ok(el.textContent === 'some other task content', 'other task content preserved without prefix (got ' + JSON.stringify(el.textContent) + ')');

  // 前缀匹配边界：精确前缀匹配（含尾空格），避免误伤；无尾空格或非行首 -> 不折叠、不加前缀
  // 'work task' 无尾空格 -> 不折叠，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'work task' });
  ok(el.className === 'msg msg--process-card', 'work task without trailing space: non-folded msg--process-card (got ' + el.className + ')');
  ok(!hasDetails(el), 'work task without trailing space: not collapsible');
  ok(el.textContent === 'work task', 'work task without trailing space: no prefix (got ' + JSON.stringify(el.textContent) + ')');
  // 'work tasks' (复数) -> 不折叠，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'work tasks abc' });
  ok(el.className === 'msg msg--process-card', 'work tasks (plural): non-folded msg--process-card (got ' + el.className + ')');
  ok(el.textContent === 'work tasks abc', 'work tasks (plural): no prefix (got ' + JSON.stringify(el.textContent) + ')');
  // 'judge task' 无尾空格 -> 不折叠，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'judge task' });
  ok(el.className === 'msg msg--process-card', 'judge task without trailing space: non-folded msg--process-card (got ' + el.className + ')');
  ok(el.textContent === 'judge task', 'judge task without trailing space: no prefix (got ' + JSON.stringify(el.textContent) + ')');
  // 前缀在中间 -> 不折叠，不加前缀（仅行首前缀匹配）
  el = createMessageElement({ role: 'user', source: 'task', content: 'prefix work task abc' });
  ok(el.className === 'msg msg--process-card', 'work task in middle: non-folded msg--process-card (got ' + el.className + ')');
  ok(el.textContent === 'prefix work task abc', 'work task in middle: no prefix (got ' + JSON.stringify(el.textContent) + ')');

  // schedule / curator -> 非折叠左对齐状态卡片（无前缀）
  el = createMessageElement({ role: 'user', source: 'schedule', content: 'run prompt' });
  ok(el.className === 'msg msg--process-card', 'schedule renders msg--process-card (non-folded) (got ' + el.className + ')');
  ok(!hasDetails(el), 'schedule not collapsible (no details)');
  ok(el.textContent === 'run prompt', 'schedule content no prefix (got ' + JSON.stringify(el.textContent) + ')');

  el = createMessageElement({ role: 'user', source: 'curator', content: 'digest' });
  ok(el.className === 'msg msg--process-card', 'curator renders msg--process-card (non-folded) (got ' + el.className + ')');
  ok(!hasDetails(el), 'curator not collapsible (no details)');
  ok(el.textContent === 'digest', 'curator content no prefix (got ' + JSON.stringify(el.textContent) + ')');

  // 多模态 content（list）-> 走状态卡片样式但不折叠（折叠仅对 string content 生效）
  el = createMessageElement({ role: 'user', source: 'task', content: [{ type: 'text', text: 'work task t1' }] });
  ok(el.className === 'msg msg--process-card', 'multimodal task content renders msg--process-card (non-folded)');
  ok(!hasDetails(el), 'multimodal task content not collapsible (no details)');
  ok(el.textContent.indexOf('work task t1') !== -1, 'multimodal text part preserved');
  ok(el.textContent.indexOf('查询状态:') === -1, 'multimodal content: no legacy prefix (string-only fold rule)');

  // 真人渠道 -> 普通蓝底气泡（msg user）
  el = createMessageElement({ role: 'user', source: 'dashboard', content: 'hi' });
  ok(el.className === 'msg user', 'dashboard user renders bubble (got ' + el.className + ')');
  ok(!el.dataset || !el.dataset.source, 'dashboard bubble has no dataset.source');

  // null / 未知 / 缺失 source -> 普通气泡
  ok(createMessageElement({ role: 'user', source: null, content: 'hi' }).className === 'msg user', 'null source renders bubble');
  ok(createMessageElement({ role: 'user', source: 'unknown', content: 'hi' }).className === 'msg user', 'unknown source renders bubble');
  ok(createMessageElement({ role: 'user', content: 'hi' }).className === 'msg user', 'missing source renders bubble');

  // 非 user 角色即使带 task source 也不走进程状态卡片
  el = createMessageElement({ role: 'assistant', source: 'task', content: 'ok' });
  ok(el.className !== 'msg msg--process-card', 'assistant with task source not process card (got ' + el.className + ')');

  // === 合并卡片渲染（_mergedTaskStatus=true）：summary 固定=任务状态，open=false，pre 多行 ===
  // 跨 role 合并：first message 是 work task (role=user source=task) -> 合并卡片 className=msg system（非 msg--process-card）
  const mergedFirstWork = {
    role: 'user', source: 'task', name: null,
    content: '查询状态: work task t_aaa\n开始运行: t_aaa - 查天气',
    _mergedTaskStatus: true,
  };
  el = createMessageElement(mergedFirstWork);
  ok(el.className === 'msg system', 'merged card (first work task) className = msg system (got ' + el.className + ')');
  ok(el.className.indexOf('msg--process-card') === -1, 'merged card not using msg--process-card (got ' + el.className + ')');
  ok(!el.dataset || el.dataset.source === undefined, 'merged card has no dataset.source (aligned with system)');
  ok(hasDetails(el), 'merged card is a details (collapsible)');
  d = findDetails(el);
  ok(d && d.open === false, 'merged card folded by default (open=false) (got open=' + (d && d.open) + ')');
  sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '任务状态', 'merged card summary = 任务状态 (fixed) (got ' + JSON.stringify(sm && sm.textContent) + ')');
  pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === '查询状态: work task t_aaa\n开始运行: t_aaa - 查天气', 'merged card pre = multi-line content (got ' + JSON.stringify(pre && pre.textContent) + ')');
  ok(el.dataset && el.dataset.name === 'ui.task_lifecycle', 'merged card dataset.name = ui.task_lifecycle (got ' + JSON.stringify(el.dataset && el.dataset.name) + ')');

  // 合并卡片：first message 是 lifecycle (role=system name=ui.task_lifecycle)
  const mergedFirstLifecycle = {
    role: 'system', name: 'ui.task_lifecycle',
    content: '开始运行: t_aaa\n判断结束: judge task t_aaa: goal?',
    _mergedTaskStatus: true,
  };
  el = createMessageElement(mergedFirstLifecycle);
  ok(el.className === 'msg system', 'merged card (first lifecycle) className = msg system (got ' + el.className + ')');
  d = findDetails(el);
  ok(d && d.open === false, 'merged card (lifecycle first) folded (open=false)');
  sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '任务状态', 'merged card (lifecycle first) summary = 任务状态');
  pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === '开始运行: t_aaa\n判断结束: judge task t_aaa: goal?', 'merged card (lifecycle first) pre = multi-line content');

  // 端到端：相邻 lifecycle + work task 经 groupTaskMessages + createMessageElement 渲染为合并卡片
  const envE2E = loadChat();
  const e2eMessages = [
    { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa - 查天气' },
    { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
  ];
  const groupedE2E = envE2E.chat.groupTaskMessages(e2eMessages);
  ok(groupedE2E.length === 1 && groupedE2E[0]._mergedTaskStatus === true, 'e2e: lifecycle + work task merged into 1 group');
  const e2eEl = envE2E.chat.createMessageElement(groupedE2E[0]);
  ok(e2eEl.className === 'msg system', 'e2e merged card className = msg system (got ' + e2eEl.className + ')');
  const e2eDetails = e2eEl._kids && e2eEl._kids[0];
  ok(e2eDetails && e2eDetails.open === false, 'e2e merged card open=false');
  const e2eSummary = e2eDetails && e2eDetails._kids && e2eDetails._kids[0];
  ok(e2eSummary && e2eSummary.textContent === '任务状态', 'e2e merged card summary = 任务状态');
  const e2ePre = e2eDetails && e2eDetails._kids && e2eDetails._kids[1];
  ok(e2ePre && e2ePre.textContent === '开始运行: t_aaa - 查天气\n查询状态: work task t_aaa', 'e2e merged card pre = lifecycle 原文 + work task 带前缀 (got ' + JSON.stringify(e2ePre && e2ePre.textContent) + ')');
})();

// ===========================================================================
// shouldRenderMessage: 进程来源（task/schedule/curator）的 assistant 推理消息属
// worker 内部思考过程，不在 Dashboard 对话框展示（与 judge 推理不落库同理）；
// worker 推理虽落库供 goal_mode 续轮与上下文，但对用户不可见。realtime
// （api/dashboard/无 source）assistant 正常渲染；进程来源 user/tool 消息不受影响。
// Regression: worker CoT "The task requires querying weather..." 泄露为普通气泡。
// ===========================================================================
(function testProcessAssistantReasoningHidden() {
  const env = loadChat();
  const shouldRenderMessage = env.chat.shouldRenderMessage;
  ok(typeof shouldRenderMessage === 'function', 'shouldRenderMessage exposed');

  // 进程来源 assistant 推理 -> 隐藏（worker 思考过程不进对话框）
  ok(shouldRenderMessage({ role: 'assistant', source: 'task', content: 'The task requires querying weather...' }) === false,
    'task-sourced assistant reasoning hidden');
  ok(shouldRenderMessage({ role: 'assistant', source: 'schedule', content: 'run prompt reasoning' }) === false,
    'schedule-sourced assistant reasoning hidden');
  ok(shouldRenderMessage({ role: 'assistant', source: 'curator', content: 'consolidation reasoning' }) === false,
    'curator-sourced assistant reasoning hidden');
  ok(shouldRenderMessage({ role: 'assistant', source: 'task', content: '' }) === false,
    'task-sourced assistant with empty content hidden by source');

  // realtime assistant（api/dashboard/无 source）-> 正常渲染
  ok(shouldRenderMessage({ role: 'assistant', source: 'api', content: 'reply' }) === true,
    'api-sourced assistant rendered');
  ok(shouldRenderMessage({ role: 'assistant', source: 'dashboard', content: 'reply' }) === true,
    'dashboard-sourced assistant rendered');
  ok(shouldRenderMessage({ role: 'assistant', content: 'reply' }) === true,
    'untagged assistant rendered');

  // 进程来源 user 消息仍渲染（折叠卡片，不受 assistant 隐藏影响）
  ok(shouldRenderMessage({ role: 'user', source: 'task', content: 'work task t1' }) === true,
    'task-sourced user still rendered (folded card)');
  // tool 消息仍渲染
  ok(shouldRenderMessage({ role: 'tool', content: '{}' }) === true,
    'tool message still rendered');
})();

// ===========================================================================
// 调试设置：createMessageElement 为工具调用/任务状态卡片打 data-debug-kind 标记，
// 供设置弹框的显隐规则（CSS）控制。任务结果/普通消息/进程消息不打标记。
// ===========================================================================
(function testDebugKindTagging() {
  const env = loadChat();
  const createMessageElement = env.chat.createMessageElement;
  ok(typeof createMessageElement === 'function', 'createMessageElement exposed for debug-kind');

  // 工具调用卡片 -> tool
  let el = createMessageElement({ role: 'tool', content: '{"k":1}' });
  ok(el && el.dataset && el.dataset.debugKind === 'tool', 'tool message tagged data-debug-kind=tool (got ' + JSON.stringify(el && el.dataset && el.dataset.debugKind) + ')');

  // 任务指令卡片 -> task
  el = createMessageElement({ role: 'system', name: 'ui.task_command', content: '执行命令: /task list' });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'task_command tagged data-debug-kind=task (got ' + JSON.stringify(el && el.dataset && el.dataset.debugKind) + ')');

  // 任务状态卡片(无 card) -> task
  el = createMessageElement({ role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1 - 调研' });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'task_lifecycle (no card) tagged data-debug-kind=task');

  // 任务状态交互卡片(带 card) -> task
  el = createMessageElement(
    { role: 'system', name: 'ui.task_lifecycle', content: '等待批准: t_1',
      card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve', 'cancel'] } },
    new Map([['t_1', { kind: 'resolved', status: 'waiting_approval' }]]),
    new Map()
  );
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'task-card (with card) tagged data-debug-kind=task');

  // 合并任务状态卡片 -> task
  el = createMessageElement({ role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1\n查询状态: work task t_1', _mergedTaskStatus: true });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'merged task status tagged data-debug-kind=task');

  // work task 折叠卡片 -> task
  el = createMessageElement({ role: 'user', source: 'task', content: 'work task t_1' });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'work task folded card tagged data-debug-kind=task');

  // 普通消息/任务结果/进程消息 不打标记（不受调试显隐控制）
  ok(!(createMessageElement({ role: 'user', source: 'dashboard', content: 'hi' }).dataset.debugKind), 'dashboard user has no debugKind');
  ok(!(createMessageElement({ role: 'assistant', content: 'ok' }).dataset.debugKind), 'assistant has no debugKind');
  ok(!(createMessageElement({ role: 'system', name: 'ui.task_result', content: '任务已完成' }).dataset.debugKind), 'task_result has no debugKind');
  ok(!(createMessageElement({ role: 'user', source: 'schedule', content: 'run' }).dataset.debugKind), 'schedule process card has no debugKind');
})();

// ===========================================================================
// T6/T7: task card interaction (validate / state-resolve / group / render / action)
// ===========================================================================
async function testTaskCardInteraction() {
  // Recursive descendant finder (harness makeEl has no querySelector).
  function findDescendants(el, predicate) {
    const out = [];
    function walk(node) {
      if (!node || !node._kids) return;
      for (const k of node._kids) {
        if (predicate(k)) out.push(k);
        walk(k);
      }
    }
    walk(el);
    return out;
  }
  const findByClass = (el, cls) => findDescendants(el, (n) => {
    if (!n || typeof n.className !== 'string') return false;
    return n.className.split(/\s+/).indexOf(cls) !== -1;
  });
  const hasButton = (card, action) => findByClass(card, 'task-card__btn').some((b) => b.dataset && b.dataset.action === action);
  const detailWith = (id, msgs) => ({ session: { id }, messages: msgs, summary: null, task_state: null });

  // unhandledRejection monitor for the duration of this test block.
  let unhandledCount = 0;
  const unhandledReasons = [];
  const unhandledHandler = (reason) => { unhandledCount++; unhandledReasons.push(reason); };
  process.on('unhandledRejection', unhandledHandler);

  const VC = {
    schema_version: 1, kind: 'task_lifecycle', task_id: 't_1',
    status: 'waiting_approval', title: 'T', summary: 'S',
    available_actions: ['approve', 'reject', 'revise', 'cancel'],
  };

  try {
    // --- T6: validateTaskCard (table-driven) ---
    const env1 = loadChat();
    const validate = env1.chat.validateTaskCard;
    ok(typeof validate === 'function', 'validateTaskCard exposed');

    ok(validate(null) === null, 'validate null -> null');
    ok(validate(undefined) === null, 'validate undefined -> null');
    ok(validate('x') === null, 'validate string -> null');
    ok(validate([]) === null, 'validate array -> null');
    ok(validate(42) === null, 'validate number -> null');
    ok(validate({}) === null, 'empty object -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S' }) === null, 'missing available_actions -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: [] }) === null, 'empty available_actions -> null');
    ok(validate({ kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing schema_version -> null');
    ok(validate({ schema_version: 1, task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing kind -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing task_id -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing status -> null');
    ok(validate({ ...VC, schema_version: '1' }) === null, 'schema_version string -> null');
    ok(validate({ ...VC, kind: 'other' }) === null, 'unknown kind -> null');
    ok(validate({ ...VC, status: 'running' }) === null, 'unknown status -> null');
    ok(validate({ ...VC, task_id: '' }) === null, 'blank task_id -> null');
    ok(validate({ ...VC, task_id: '   ' }) === null, 'whitespace task_id -> null');
    ok(validate({ ...VC, available_actions: 'approve' }) === null, 'available_actions string -> null');
    ok(validate({ ...VC, available_actions: [{ action: 'approve' }] }) === null, 'available_actions non-string elements -> null');
    ok(validate({ ...VC, schema_version: 2 }) === null, 'unknown schema_version -> null');
    ok(validate({ ...VC, available_actions: ['foo', 'bar'] }) === null, 'only unknown actions -> null');

    // mixed: allowlist kept in original order, unknown dropped
    const mixed = validate({ ...VC, available_actions: ['foo', 'approve', 'bar', 'reject', 'cancel', 'baz', 'revise'] });
    ok(mixed !== null, 'mixed card valid');
    ok(mixed.available_actions.length === 4, 'mixed actions filtered to 4 (got ' + JSON.stringify(mixed && mixed.available_actions) + ')');
    ok(mixed.available_actions[0] === 'approve' && mixed.available_actions[1] === 'reject' && mixed.available_actions[2] === 'cancel' && mixed.available_actions[3] === 'revise', 'mixed actions in original order');

    // canonical: new object, no mutation of input
    const input = { ...VC, available_actions: ['approve', 'foo'] };
    const inputActionsBefore = input.available_actions.slice();
    const out = validate(input);
    ok(input.available_actions.length === inputActionsBefore.length && input.available_actions[1] === 'foo', 'validate does not mutate input');
    ok(out !== input, 'validate returns new object');
    ok(out.task_id === 't_1' && out.status === 'waiting_approval' && out.title === 'T' && out.summary === 'S', 'canonical preserves scalar fields');

    // each valid status accepted
    ok(validate({ ...VC, status: 'failed', available_actions: ['retry', 'cancel'] }) !== null, 'failed status valid');
    ok(validate({ ...VC, status: 'expired', available_actions: ['retry'] }) !== null, 'expired status valid');

    // --- interaction_type validation (waiting_approval only) ---
    // missing interaction_type on waiting_approval -> default 'approval'
    const vcApproval = { ...VC, available_actions: ['approve', 'reject'] };
    const approvalOut = validate(vcApproval);
    ok(approvalOut !== null, 'waiting_approval without interaction_type valid');
    ok(approvalOut.interaction_type === 'approval', 'missing interaction_type defaults to approval (got ' + JSON.stringify(approvalOut && approvalOut.interaction_type) + ')');

    // explicit interaction_type='approval'
    ok(validate({ ...vcApproval, interaction_type: 'approval' }).interaction_type === 'approval', 'explicit interaction_type=approval preserved');

    // interaction_type='intent_request'
    const intentOut = validate({ ...VC, available_actions: ['revise', 'cancel'], interaction_type: 'intent_request' });
    ok(intentOut !== null && intentOut.interaction_type === 'intent_request', 'interaction_type=intent_request preserved');

    // invalid interaction_type value -> null
    ok(validate({ ...vcApproval, interaction_type: 'bogus' }) === null, 'invalid interaction_type value -> null');
    ok(validate({ ...vcApproval, interaction_type: 42 }) === null, 'non-string interaction_type -> null');

    // non-waiting_approval card ignores interaction_type (not in canonical)
    const failedOut = validate({ ...VC, status: 'failed', available_actions: ['retry', 'cancel'], interaction_type: 'approval' });
    ok(failedOut !== null, 'failed card with stray interaction_type still valid');
    ok(!('interaction_type' in failedOut), 'failed canonical excludes interaction_type (got ' + JSON.stringify(failedOut) + ')');

    // canonical: new object, no mutation of input (interaction_type field)
    const inputIT = { ...vcApproval, interaction_type: 'intent_request' };
    const outIT = validate(inputIT);
    ok(outIT !== inputIT, 'validate returns new object (interaction_type)');
    ok(inputIT.interaction_type === 'intent_request', 'validate does not mutate input.interaction_type');

    // --- T6: state resolution (GET dedup + per-card active/stale) ---
    let env2 = loadChat();
    let getCalls = [];
    env2.win.NAGENT.api.task.get = (id) => {
      getCalls.push(id);
      if (id === 't_a') return Promise.resolve({ id, status: 'queued' });
      if (id === 't_b') return Promise.resolve({ id, status: 'waiting_approval' });
      return Promise.resolve({ id, status: 'unknown' });
    };
    const cardMsgs = [
      { id: 'm1', role: 'user', content: 'hi' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '等待批准: t_a',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_a', status: 'waiting_approval', title: 'TA', summary: 'S', available_actions: ['approve', 'cancel'] } },
      { id: 'm3', role: 'system', name: 'ui.task_lifecycle', content: '已失败: t_a',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_a', status: 'failed', title: 'TA', summary: 'S', available_actions: ['retry', 'cancel'] } },
      { id: 'm4', role: 'system', name: 'ui.task_lifecycle', content: '等待批准: t_b',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_b', status: 'waiting_approval', title: 'TB', summary: 'S', available_actions: ['approve'] } },
    ];
    await env2.chat.applySessionDetail(detailWith('s1', cardMsgs));
    await env2.waitMicro();
    ok(getCalls.length === 2, 'GET dedup: 2 unique task_ids (got ' + getCalls.length + ', calls=' + JSON.stringify(getCalls) + ')');
    const cards2 = findByClass(env2.messageStack, 'task-card');
    ok(cards2.length === 3, '3 task cards rendered (got ' + cards2.length + ')');
    ok(!hasButton(cards2[0], 'approve') && !hasButton(cards2[0], 'cancel'), 'm2 (auth=queued != card=waiting_approval) -> stale, no buttons');
    ok(!hasButton(cards2[1], 'retry') && !hasButton(cards2[1], 'cancel'), 'm3 (auth=queued != card=failed) -> stale, no buttons');
    ok(hasButton(cards2[2], 'approve'), 'm4 (auth=waiting_approval == card) -> active, approve present');

    // task_not_found -> stale
    let env3 = loadChat();
    env3.win.NAGENT.api.task.get = () => Promise.reject(new Error('task_not_found'));
    await env3.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: { ...VC, task_id: 't_x' } },
    ]));
    await env3.waitMicro();
    const cards3 = findByClass(env3.messageStack, 'task-card');
    ok(cards3.length === 1 && !hasButton(cards3[0], 'approve'), 'task_not_found -> stale, no buttons');

    // network error -> unavailable
    let env4 = loadChat();
    env4.win.NAGENT.api.task.get = () => Promise.reject(new Error('network_down'));
    await env4.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: { ...VC, task_id: 't_y' } },
    ]));
    await env4.waitMicro();
    const cards4 = findByClass(env4.messageStack, 'task-card');
    ok(cards4.length === 1 && !hasButton(cards4[0], 'approve'), 'network error -> unavailable, no buttons');

    // --- GET in-flight: no clickable actions in DOM before GET resolves ---
    let env5 = loadChat();
    let resolveGet;
    env5.win.NAGENT.api.task.get = () => new Promise((res) => { resolveGet = res; });
    const p5 = env5.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'user', content: 'FIRST' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ])).catch(() => {});
    await env5.waitMicro();
    ok(findByClass(env5.messageStack, 'task-card__btn').length === 0, 'no clickable actions before GET resolves');
    resolveGet({ id: 't_1', status: 'waiting_approval' });
    await p5;
    await env5.waitMicro();
    ok(hasButton(findByClass(env5.messageStack, 'task-card')[0], 'approve'), 'approve button appears after GET resolves (active)');

    // --- render token: stale render does not overwrite newer content ---
    let env6 = loadChat();
    let resolveGet6;
    env6.win.NAGENT.api.task.get = () => new Promise((res) => { resolveGet6 = res; });
    const p6 = env6.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'user', content: 'FIRST' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ])).catch(() => {});
    await env6.waitMicro();
    await env6.chat.applySessionDetail(detailWith('s1', [
      { id: 'm3', role: 'user', content: 'SECOND' },
    ]));
    await env6.waitMicro();
    ok(env6.messageStack.textContent.indexOf('SECOND') !== -1, 'second render applied (got ' + JSON.stringify(env6.messageStack.textContent) + ')');
    ok(env6.messageStack.textContent.indexOf('FIRST') === -1, 'first render not in DOM (pending GET)');
    resolveGet6({ id: 't_1', status: 'waiting_approval' });
    await p6;
    await env6.waitMicro();
    ok(env6.messageStack.textContent.indexOf('SECOND') !== -1, 'after first GET resolves: second still wins (render token)');
    ok(env6.messageStack.textContent.indexOf('FIRST') === -1, 'first render did not overwrite (render token)');

    // --- T6: grouping ---
    let env7 = loadChat();
    const g = env7.chat.groupTaskMessages;
    ok(typeof g === 'function', 'groupTaskMessages exposed');
    const grouped = g([
      { id: 'm1', role: 'system', name: 'ui.task_command', content: 'c1' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: 'l1', card: VC },
      { id: 'm3', role: 'system', name: 'ui.task_lifecycle', content: 'l2', card: VC },
      { id: 'm4', role: 'system', name: 'ui.task_command', content: 'c2' },
      { id: 'm5', role: 'system', name: 'ui.task_lifecycle', content: 'l3' },
      { id: 'm6', role: 'system', name: 'ui.task_lifecycle', content: 'l4', card: { broken: true } },
    ]);
    // m1/m4 (task_command) 独立；m2/m3 (valid card) 独立；m5+m6 (lifecycle 无/坏 card) 合并 = 5 组
    ok(grouped.length === 5, 'groupTaskMessages: 5 groups (m1,m2,m3,m4 independent + m5+m6 merged) (got ' + grouped.length + ')');
    ok(grouped[0].content === 'c1' && grouped[0].name === 'ui.task_command', 'group 0 is m1 task_command (independent)');
    ok(grouped[1].card && grouped[1].card.task_id === 't_1', 'group 1 is m2 valid card (independent)');
    ok(grouped[2].card && grouped[2].card.task_id === 't_1', 'group 2 is m3 valid card (independent)');
    ok(grouped[3].content === 'c2' && grouped[3].name === 'ui.task_command', 'group 3 is m4 task_command (independent, breaks merge chain)');
    // m4 task_command 断开合并链：m5+m6 合并为 g4（不含 c2）
    ok(grouped[4].content.indexOf('l3') !== -1 && grouped[4].content.indexOf('l4') !== -1, 'group 4 merged m5+l3 + m6+l4 (got ' + JSON.stringify(grouped[4].content) + ')');
    ok(grouped[4].content.indexOf('c2') === -1, 'group 4 merged does NOT contain c2 (command broke chain) (got ' + JSON.stringify(grouped[4].content) + ')');
    ok(grouped[4].id === 'm5' && grouped[4].name === 'ui.task_lifecycle' && grouped[4]._mergedTaskStatus === true, 'group 4 merged keeps first message metadata + _mergedTaskStatus flag (got ' + JSON.stringify({ id: grouped[4].id, name: grouped[4].name, flag: grouped[4]._mergedTaskStatus }) + ')');
    // valid card message preserved by reference (not cloned/mutated)
    const cardInput = { id: 'mx', role: 'system', name: 'ui.task_lifecycle', content: 'l', card: VC };
    const g2 = env7.chat.groupTaskMessages([cardInput, { id: 'my', role: 'assistant', content: 'a' }]);
    ok(g2[0] === cardInput, 'valid card message preserved by reference');

    // --- 合并规则：work task / judge task / lifecycle 无 card 相邻合并 ---
    // 单独 work task（无相邻可合并对象）：1-message group，保持 role=user source=task，无 _mergedTaskStatus 标志
    const singleWork = g([
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
    ]);
    ok(singleWork.length === 1, 'single work task: 1 group (got ' + singleWork.length + ')');
    ok(singleWork[0].role === 'user' && singleWork[0].source === 'task' && singleWork[0].content === 'work task t_aaa', 'single work task keeps raw content + role/user (no merge)');
    ok(singleWork[0]._mergedTaskStatus === undefined, 'single work task: no _mergedTaskStatus flag (got ' + JSON.stringify(singleWork[0]._mergedTaskStatus) + ')');

    // 单独 judge task：1-message group
    const singleJudge = g([
      { id: 'j1', role: 'user', source: 'task', content: 'judge task t_bbb: goal?' },
    ]);
    ok(singleJudge.length === 1 && singleJudge[0].content === 'judge task t_bbb: goal?', 'single judge task keeps raw content (got ' + JSON.stringify(singleJudge[0].content) + ')');

    // 合并：lifecycle 无 card + work task -> 一个卡片，summary=任务状态，pre 多行（lifecycle 原文 + 查询状态: work task...）
    const merged1 = g([
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa - 查天气' },
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
    ]);
    ok(merged1.length === 1, 'lifecycle + work task: 1 merged group (got ' + merged1.length + ')');
    ok(merged1[0]._mergedTaskStatus === true, 'lifecycle + work task: _mergedTaskStatus=true');
    ok(merged1[0].content === '开始运行: t_aaa - 查天气\n查询状态: work task t_aaa', 'merged content: lifecycle 原文 + work task 带前缀 (got ' + JSON.stringify(merged1[0].content) + ')');
    ok(merged1[0].role === 'system' && merged1[0].name === 'ui.task_lifecycle', 'merged keeps first message role/name');

    // 合并：judge task + lifecycle 无 card -> summary=任务状态，pre 多行（判断结束: judge task... + lifecycle 原文）
    const merged2 = g([
      { id: 'j1', role: 'user', source: 'task', content: 'judge task t_bbb: goal?' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '已完成: t_bbb - 交付' },
    ]);
    ok(merged2.length === 1, 'judge task + lifecycle: 1 merged group (got ' + merged2.length + ')');
    ok(merged2[0]._mergedTaskStatus === true, 'judge task + lifecycle: _mergedTaskStatus=true');
    ok(merged2[0].content === '判断结束: judge task t_bbb: goal?\n已完成: t_bbb - 交付', 'merged content: judge task 带前缀 + lifecycle 原文 (got ' + JSON.stringify(merged2[0].content) + ')');
    // 跨 role 合并：first message work task (role=user) -> 合并卡片保留 role=user source=task
    ok(merged2[0].role === 'user' && merged2[0].source === 'task', 'cross-role merge keeps first message role/source');

    // 不合并：上一条 ui.task_command 断开合并链（当前 task 状态卡片不与它合并）
    const cmdBreak = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
    ]);
    ok(cmdBreak.length === 2, 'task_command + lifecycle: 2 groups (command breaks chain) (got ' + cmdBreak.length + ')');
    ok(cmdBreak[0].name === 'ui.task_command' && cmdBreak[0].content === '执行命令: /task list', 'cmdBreak[0] is task_command (independent)');
    ok(cmdBreak[1].name === 'ui.task_lifecycle' && cmdBreak[1].content === '开始运行: t_aaa', 'cmdBreak[1] is lifecycle (not merged with command)');

    // 不合并：非 task 消息（assistant/user dashboard）断开合并链
    const breakChain = g([
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
      { id: 'a1', role: 'assistant', content: '处理中' },
      { id: 'w2', role: 'user', source: 'task', content: 'work task t_bbb' },
    ]);
    ok(breakChain.length === 3, 'work + assistant + work: 3 groups (assistant breaks chain) (got ' + breakChain.length + ')');
    ok(breakChain[0]._mergedTaskStatus === undefined && breakChain[2]._mergedTaskStatus === undefined, 'non-adjacent work tasks not merged');

    // 三方合并：lifecycle + work task + judge task 相邻 -> 一个卡片，多行拼接
    const tripleMerge = g([
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
      { id: 'j1', role: 'user', source: 'task', content: 'judge task t_aaa: goal?' },
    ]);
    ok(tripleMerge.length === 1 && tripleMerge[0]._mergedTaskStatus === true, 'lifecycle + work + judge: 1 merged group');
    ok(tripleMerge[0].content === '开始运行: t_aaa\n查询状态: work task t_aaa\n判断结束: judge task t_aaa: goal?', 'triple merge content (got ' + JSON.stringify(tripleMerge[0].content) + ')');

    // === 按类型分组：同类型相邻合并，不同类型断开（任务指令 vs 任务消息）===
    // 2 条相邻 ui.task_command 合并为一组（summary=任务指令，content 多行原文）
    const twoCmds = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create 报告' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：报告' },
    ]);
    ok(twoCmds.length === 1, '2 adjacent task_command: 1 merged group (got ' + twoCmds.length + ')');
    ok(twoCmds[0].name === 'ui.task_command', 'merged command group keeps name=ui.task_command (got ' + twoCmds[0].name + ')');
    ok(twoCmds[0].role === 'system', 'merged command group keeps role=system');
    ok(twoCmds[0].content === '执行命令: /task create 报告\n已创建任务 t_1：报告', 'merged command content = multi-line raw (got ' + JSON.stringify(twoCmds[0].content) + ')');
    ok(twoCmds[0]._mergedTaskStatus === undefined, 'merged command group: NO _mergedTaskStatus flag (got ' + JSON.stringify(twoCmds[0]._mergedTaskStatus) + ')');

    // 3 条相邻 ui.task_command 合并为一组
    const threeCmds = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create A' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：A' },
      { id: 'c3', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    ]);
    ok(threeCmds.length === 1, '3 adjacent task_command: 1 merged group (got ' + threeCmds.length + ')');
    ok(threeCmds[0].content === '执行命令: /task create A\n已创建任务 t_1：A\n执行命令: /task list', '3 merged commands content multi-line (got ' + JSON.stringify(threeCmds[0].content) + ')');
    ok(threeCmds[0].name === 'ui.task_command' && threeCmds[0]._mergedTaskStatus === undefined, '3 merged commands: name=ui.task_command, no _mergedTaskStatus');

    // ui.task_command + ui.task_lifecycle（无 card）相邻不合并（不同类型，2 张独立卡片）
    const cmdThenLifecycle = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
    ]);
    ok(cmdThenLifecycle.length === 2, 'task_command + lifecycle (no card): 2 groups, different types not merged (got ' + cmdThenLifecycle.length + ')');
    ok(cmdThenLifecycle[0].name === 'ui.task_command' && cmdThenLifecycle[0].content === '执行命令: /task list', 'cmdThenLifecycle[0] is task_command (single)');
    ok(cmdThenLifecycle[1].name === 'ui.task_lifecycle' && cmdThenLifecycle[1].content === '开始运行: t_aaa', 'cmdThenLifecycle[1] is lifecycle (single, not merged)');
    ok(cmdThenLifecycle[1]._mergedTaskStatus === undefined, 'single lifecycle after command: no _mergedTaskStatus flag');

    // work task + ui.task_command 相邻不合并（不同类型）
    const workThenCmd = g([
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    ]);
    ok(workThenCmd.length === 2, 'work task + task_command: 2 groups, different types not merged (got ' + workThenCmd.length + ')');
    ok(workThenCmd[0].role === 'user' && workThenCmd[0].source === 'task' && workThenCmd[0].content === 'work task t_aaa', 'workThenCmd[0] is work task (single, raw content)');
    ok(workThenCmd[0]._mergedTaskStatus === undefined, 'single work task before command: no _mergedTaskStatus flag');
    ok(workThenCmd[1].name === 'ui.task_command' && workThenCmd[1].content === '执行命令: /task list', 'workThenCmd[1] is task_command (single)');

    // command + lifecycle + command: 3 groups（不同类型断开）
    const cmdLifecycleCmd = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create A' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    ]);
    ok(cmdLifecycleCmd.length === 3, 'command + lifecycle + command: 3 groups (different types break chain) (got ' + cmdLifecycleCmd.length + ')');
    ok(cmdLifecycleCmd[0].name === 'ui.task_command' && cmdLifecycleCmd[0].content === '执行命令: /task create A', 'cmdLifecycleCmd[0] is single command');
    ok(cmdLifecycleCmd[1].name === 'ui.task_lifecycle' && cmdLifecycleCmd[1].content === '开始运行: t_aaa', 'cmdLifecycleCmd[1] is single lifecycle');
    ok(cmdLifecycleCmd[2].name === 'ui.task_command' && cmdLifecycleCmd[2].content === '执行命令: /task list', 'cmdLifecycleCmd[2] is single command');

    // lifecycle + command + lifecycle: 3 groups（不同类型断开）
    const lifecycleCmdLifecycle = g([
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
      { id: 'l2', role: 'system', name: 'ui.task_lifecycle', content: '已完成: t_aaa' },
    ]);
    ok(lifecycleCmdLifecycle.length === 3, 'lifecycle + command + lifecycle: 3 groups (got ' + lifecycleCmdLifecycle.length + ')');
    ok(lifecycleCmdLifecycle[0].name === 'ui.task_lifecycle' && lifecycleCmdLifecycle[1].name === 'ui.task_command' && lifecycleCmdLifecycle[2].name === 'ui.task_lifecycle', 'lifecycle+cmd+lifecycle: name sequence preserved');

    // 2 条相邻 ui.task_command + lifecycle（无 card）：command 合并 + lifecycle 独立 = 2 组
    const twoCmdsThenLifecycle = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create A' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：A' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
    ]);
    ok(twoCmdsThenLifecycle.length === 2, '2 commands + lifecycle: 2 groups (commands merged + lifecycle single) (got ' + twoCmdsThenLifecycle.length + ')');
    ok(twoCmdsThenLifecycle[0].name === 'ui.task_command' && twoCmdsThenLifecycle[0].content === '执行命令: /task create A\n已创建任务 t_1：A', 'twoCmdsThenLifecycle[0] is merged commands');
    ok(twoCmdsThenLifecycle[1].name === 'ui.task_lifecycle' && twoCmdsThenLifecycle[1].content === '开始运行: t_aaa', 'twoCmdsThenLifecycle[1] is single lifecycle');

    // 渲染：merged command group -> summary=任务指令, pre=多行 content, open=true, className=msg system
    const mergedCmdEl = env7.chat.createMessageElement(twoCmds[0]);
    ok(mergedCmdEl.className === 'msg system', 'merged command card className = msg system (got ' + mergedCmdEl.className + ')');
    const mergedCmdDetails = mergedCmdEl._kids && mergedCmdEl._kids[0];
    ok(mergedCmdDetails && 'open' in mergedCmdDetails, 'merged command card is a details (collapsible)');
    ok(mergedCmdDetails && mergedCmdDetails.open === true, 'merged command card open=true (got open=' + (mergedCmdDetails && mergedCmdDetails.open) + ')');
    const mergedCmdSummary = mergedCmdDetails && mergedCmdDetails._kids && mergedCmdDetails._kids[0];
    ok(mergedCmdSummary && mergedCmdSummary.textContent === '任务指令', 'merged command card summary = 任务指令 (got ' + JSON.stringify(mergedCmdSummary && mergedCmdSummary.textContent) + ')');
    const mergedCmdPre = mergedCmdDetails && mergedCmdDetails._kids && mergedCmdDetails._kids[1];
    ok(mergedCmdPre && mergedCmdPre.textContent === '执行命令: /task create 报告\n已创建任务 t_1：报告', 'merged command card pre = multi-line content (got ' + JSON.stringify(mergedCmdPre && mergedCmdPre.textContent) + ')');
    ok(mergedCmdEl.dataset && mergedCmdEl.dataset.name === 'ui.task_command', 'merged command card dataset.name = ui.task_command (got ' + JSON.stringify(mergedCmdEl.dataset && mergedCmdEl.dataset.name) + ')');

    // 端到端：2 条 command 经 groupTaskMessages + createMessageElement 渲染为 1 张任务指令卡片
    const e2eCmdMessages = [
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create 报告' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：报告' },
    ];
    const groupedCmdE2E = env7.chat.groupTaskMessages(e2eCmdMessages);
    ok(groupedCmdE2E.length === 1, 'e2e: 2 commands merged into 1 group (got ' + groupedCmdE2E.length + ')');
    const e2eCmdEl = env7.chat.createMessageElement(groupedCmdE2E[0]);
    ok(e2eCmdEl.className === 'msg system', 'e2e merged command card className = msg system (got ' + e2eCmdEl.className + ')');
    const e2eCmdDetails = e2eCmdEl._kids && e2eCmdEl._kids[0];
    ok(e2eCmdDetails && e2eCmdDetails.open === true, 'e2e merged command card open=true');
    const e2eCmdSummary = e2eCmdDetails && e2eCmdDetails._kids && e2eCmdDetails._kids[0];
    ok(e2eCmdSummary && e2eCmdSummary.textContent === '任务指令', 'e2e merged command card summary = 任务指令');
    const e2eCmdPre = e2eCmdDetails && e2eCmdDetails._kids && e2eCmdDetails._kids[1];
    ok(e2eCmdPre && e2eCmdPre.textContent === '执行命令: /task create 报告\n已创建任务 t_1：报告', 'e2e merged command card pre = multi-line content (got ' + JSON.stringify(e2eCmdPre && e2eCmdPre.textContent) + ')');

    // --- T7: waiting_approval active: approve/reject/revise/cancel + textarea + label ---
    let env8 = loadChat();
    env8.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    await env8.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准', card: VC },
    ]));
    await env8.waitMicro();
    const card8 = findByClass(env8.messageStack, 'task-card')[0];
    ok(!!card8, 'waiting_approval active card rendered');
    ok(hasButton(card8, 'approve'), 'waiting_approval: approve button');
    ok(hasButton(card8, 'reject'), 'waiting_approval: reject button');
    ok(hasButton(card8, 'revise'), 'waiting_approval: revise button');
    ok(hasButton(card8, 'cancel'), 'waiting_approval: cancel button');
    const ta8 = findByClass(card8, 'task-card__textarea');
    ok(ta8.length === 1, 'waiting_approval: 1 textarea');
    const labels8 = findByClass(card8, 'task-card__label');
    ok(labels8.length === 1 && ta8[0].id && labels8[0].htmlFor === ta8[0].id, 'textarea associated with label via for/id');
    const feedback8 = findByClass(card8, 'task-card__feedback');
    ok(feedback8.length === 1, 'feedback node exists from creation');
    const details8 = card8._kids.find((k) => k && 'open' in k);
    ok(details8 && details8.open === true, 'card details open by default');
    const btns8 = findByClass(card8, 'task-card__btn');
    ok(btns8.length === 4 && btns8.every((b) => b.type === 'button'), 'all buttons are native type=button');

    // --- T7: failed active: terminal status, no actions ---
    let env9 = loadChat();
    env9.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'failed' });
    await env9.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'failed', card: { ...VC, status: 'failed', available_actions: ['retry', 'cancel'] } },
    ]));
    await env9.waitMicro();
    const card9 = findByClass(env9.messageStack, 'task-card')[0];
    ok(findByClass(card9, 'task-card__btn').length === 0, 'failed: no action buttons');
    ok(findByClass(card9, 'task-card__textarea').length === 0, 'failed: no textarea');

    // --- T7: expired active: terminal status, no actions ---
    let env10 = loadChat();
    env10.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'expired' });
    await env10.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'expired', card: { ...VC, status: 'expired', available_actions: ['retry'] } },
    ]));
    await env10.waitMicro();
    const card10 = findByClass(env10.messageStack, 'task-card')[0];
    ok(findByClass(card10, 'task-card__btn').length === 0, 'expired: no action buttons');

    // --- T7: approval card (interaction_type=approval): 2 buttons (批准/拒绝), NO textarea ---
    let envAppr = loadChat();
    envAppr.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    await envAppr.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve', 'reject'], interaction_type: 'approval' } },
    ]));
    await envAppr.waitMicro();
    const cardAppr = findByClass(envAppr.messageStack, 'task-card')[0];
    ok(!!cardAppr, 'approval card rendered');
    ok(hasButton(cardAppr, 'approve') && hasButton(cardAppr, 'reject'), 'approval: 批准+拒绝 buttons');
    ok(!hasButton(cardAppr, 'revise') && !hasButton(cardAppr, 'cancel'), 'approval: no revise/cancel');
    ok(findByClass(cardAppr, 'task-card__textarea').length === 0, 'approval: no textarea');
    ok(findByClass(cardAppr, 'task-card__btn').length === 2, 'approval: exactly 2 buttons (got ' + findByClass(cardAppr, 'task-card__btn').length + ')');

    // --- T7: intent_request card (interaction_type=intent_request): 2 buttons (补充并继续/取消) + textarea ---
    let envIR = loadChat();
    envIR.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    await envIR.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['revise', 'cancel'], interaction_type: 'intent_request' } },
    ]));
    await envIR.waitMicro();
    const cardIR = findByClass(envIR.messageStack, 'task-card')[0];
    ok(!!cardIR, 'intent_request card rendered');
    ok(hasButton(cardIR, 'revise') && hasButton(cardIR, 'cancel'), 'intent_request: 补充并继续+取消 buttons');
    ok(!hasButton(cardIR, 'approve') && !hasButton(cardIR, 'reject'), 'intent_request: no approve/reject');
    ok(findByClass(cardIR, 'task-card__textarea').length === 1, 'intent_request: 1 textarea (for revise)');
    ok(findByClass(cardIR, 'task-card__btn').length === 2, 'intent_request: exactly 2 buttons (got ' + findByClass(cardIR, 'task-card__btn').length + ')');
    // textarea associated with label
    const taIR = findByClass(cardIR, 'task-card__textarea')[0];
    const labelsIR = findByClass(cardIR, 'task-card__label');
    ok(labelsIR.length === 1 && taIR.id && labelsIR[0].htmlFor === taIR.id, 'intent_request: textarea labeled');

    // --- T7: stale/unavailable: no buttons or textarea created ---
    let env11 = loadChat();
    env11.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env11.waitMicro();
    const card11 = findByClass(env11.messageStack, 'task-card')[0];
    ok(findByClass(card11, 'task-card__btn').length === 0, 'stale: no buttons created (not disabled-after-render)');
    ok(findByClass(card11, 'task-card__textarea').length === 0, 'stale: no textarea created');
    ok(card11.textContent.indexOf('T') !== -1 && card11.textContent.indexOf('t_1') !== -1, 'stale still shows title/task_id');
    // stale no-receipt: concise fallback replaces the old generic prompt.
    ok(card11.textContent.indexOf('任务状态已变更') !== -1, 'stale no-receipt: 任务状态已变更 fallback shown');
    ok(card11.textContent.indexOf('操作不可用') === -1, 'stale no-receipt: 操作不可用 dropped (replaced)');

    // --- T7: stale card surfaces recorded decision from session receipt ---
    let env11b = loadChat();
    env11b.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11b.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准', card: VC },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '已批准: t_1 - T', card: null },
    ]));
    await env11b.waitMicro();
    const card11b = findByClass(env11b.messageStack, 'task-card')[0];
    ok(!!card11b, 'stale+receipt: card rendered');
    ok(findByClass(card11b, 'task-card__btn').length === 0, 'stale+receipt: no buttons');
    ok(card11b.textContent.indexOf('已批准') !== -1, 'stale+receipt: 已批准 decision shown (got ' + JSON.stringify(card11b.textContent) + ')');
    ok(card11b.textContent.indexOf('任务状态已变更，操作不可用') === -1, 'stale+receipt: generic prompt replaced');

    // revise receipt (intent-supplement) surfaces 已修订 on a stale card.
    let env11c = loadChat();
    env11c.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11c.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准', card: VC },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '已修订: t_1 - T | 修订指示: use plan C', card: null },
    ]));
    await env11c.waitMicro();
    const card11c = findByClass(env11c.messageStack, 'task-card')[0];
    ok(card11c.textContent.indexOf('已修订') !== -1, 'stale+receipt: 已修订 (intent-supplement) shown (got ' + JSON.stringify(card11c.textContent) + ')');

    // decision is scoped to waiting_approval: a stale failed card must NOT
    // surface an approve receipt (the decision belongs to a pending-approval task).
    let env11d = loadChat();
    env11d.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11d.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'failed', card: { ...VC, status: 'failed', available_actions: ['retry', 'cancel'] } },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '已批准: t_1 - T', card: null },
    ]));
    await env11d.waitMicro();
    const card11d = findByClass(env11d.messageStack, 'task-card')[0];
    ok(card11d.textContent.indexOf('已批准') === -1, 'stale failed card: no approve decision (scoped to waiting_approval) (got ' + JSON.stringify(card11d.textContent) + ')');
    ok(card11d.textContent.indexOf('任务状态已变更') !== -1, 'stale failed card: 任务状态已变更 fallback shown');

    // --- T7: safe rendering (textContent only, no innerHTML) ---
    let env13 = loadChat();
    env13.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    const XSS = '<img src=x onerror=alert(1)>';
    await env13.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: XSS, status: 'waiting_approval', title: XSS, summary: XSS, available_actions: ['approve'] } },
    ]));
    await env13.waitMicro();
    const allDescs = findDescendants(env13.messageStack, () => true);
    ok(!allDescs.some((n) => n.tagName === 'IMG'), 'XSS: no IMG element created (textContent only)');
    ok(env13.messageStack.textContent.indexOf(XSS) !== -1, 'XSS: text preserved as text');

    // --- T7: action dispatch + in-flight dedup ---
    let env14 = loadChat();
    const aCalls = [];
    env14.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env14.win.NAGENT.api.task.approve = (id) => { aCalls.push({ name: 'approve', id }); return Promise.resolve({}); };
    env14.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', []));
    await env14.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env14.waitMicro();
    const card14 = findByClass(env14.messageStack, 'task-card')[0];
    const approveBtn14 = findByClass(card14, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn14.click();
    await env14.waitMicro();
    ok(aCalls.length === 1 && aCalls[0].name === 'approve' && aCalls[0].id === 't_1', 'approve click dispatched with task_id (got ' + JSON.stringify(aCalls) + ')');
    // settled: buttons removed, 操作已提交 shown (refresh returns empty -> re-render -> card14 gone, so check before refresh)
    // Actually refresh re-renders with empty messages, removing the card. So check the feedback text right after click+await.
    // The handler sets settled BEFORE refresh, so check card14 feedback before refresh completes.
    // But refresh is awaited inside the handler, so by the time we get here refresh is done and card14 is gone.
    // Instead, make refresh fail to keep card14 in DOM.

    // reject reports its concrete result instead of the generic submission text.
    let env14b = loadChat();
    env14b.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env14b.win.NAGENT.api.task.reject = () => Promise.resolve({});
    await env14b.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env14b.waitMicro();
    const card14b = findByClass(env14b.messageStack, 'task-card')[0];
    const rejectBtn14b = findByClass(card14b, 'task-card__btn').find((b) => b.dataset.action === 'reject');
    rejectBtn14b.click();
    await env14b.waitMicro();
    ok(card14b.textContent.indexOf('已拒绝') !== -1, 'reject success: 已拒绝 shown');

    // --- success -> settled -> refresh fails -> "已批准，刷新失败" ---
    // Uses selectSession to set currentSessionId so the handler's refresh path runs.
    let env15 = loadChat();
    env15.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env15.win.NAGENT.api.task.approve = () => Promise.resolve({});
    sessionsList = [{ id: 's1', title: 's1' }];
    env15.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    env15.chat.init();
    await env15.waitMicro();
    env15.fireClick(env15.findSessionItem('s1'));
    await env15.waitMicro();
    await env15.waitMicro();
    // currentSessionId is now 's1'; switch getSessionDetail to reject so refresh fails.
    env15.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    const card15 = findByClass(env15.messageStack, 'task-card')[0];
    const approveBtn15 = findByClass(card15, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn15.click();
    await env15.waitMicro();
    await env15.waitMicro();
    ok(findByClass(card15, 'task-card__btn').length === 0, 'success: buttons removed (settled)');
    ok(card15.textContent.indexOf('已批准') !== -1, 'approve success: 已批准 shown');
    ok(card15.textContent.indexOf('刷新失败') !== -1, 'refresh failure: 刷新失败 shown');

    // --- in-flight dedup: second click produces no second request ---
    let env16 = loadChat();
    let resolveApprove16;
    let approveCount16 = 0;
    env16.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env16.win.NAGENT.api.task.approve = () => { approveCount16++; return new Promise((res) => { resolveApprove16 = res; }); };
    env16.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', []));
    await env16.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env16.waitMicro();
    const card16 = findByClass(env16.messageStack, 'task-card')[0];
    const approveBtn16 = findByClass(card16, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn16.click();
    await env16.waitMicro();
    ok(approveCount16 === 1, 'first click: 1 approve call');
    const btns16mid = findByClass(card16, 'task-card__btn');
    ok(btns16mid.every((b) => b.disabled === true), 'in-flight: all buttons disabled');
    approveBtn16.click();
    await env16.waitMicro();
    ok(approveCount16 === 1, 'second click during in-flight: no new call (got ' + approveCount16 + ')');
    resolveApprove16({});
    await env16.waitMicro();
    await env16.waitMicro();

    // --- revise Unicode code-point validation ---
    let env17 = loadChat();
    const reviseCalls = [];
    env17.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env17.win.NAGENT.api.task.revise = (id, note) => { reviseCalls.push({ id, note, len: Array.from(note).length }); return Promise.resolve({}); };
    env17.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    await env17.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env17.waitMicro();
    const card17 = findByClass(env17.messageStack, 'task-card')[0];
    const reviseBtn17 = findByClass(card17, 'task-card__btn').find((b) => b.dataset.action === 'revise');
    const ta17 = findByClass(card17, 'task-card__textarea')[0];

    ta17.value = '   ';
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 0, 'revise empty trim -> rejected, no api call');
    ok(card17.textContent.indexOf('不能为空') !== -1, 'revise empty feedback shown');

    ta17.value = 'a';
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 1 && reviseCalls[0].len === 1, 'revise 1 code point accepted');
    ok(card17.textContent.indexOf('补充: a') !== -1, 'revise success: entered intent shown');

    ta17.value = 'a'.repeat(2000);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 2 && reviseCalls[1].len === 2000, 'revise 2000 BMP code points accepted');

    ta17.value = 'a'.repeat(2001);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 2, 'revise 2001 BMP code points rejected');

    const ASTRAL = '𝕏';
    ok(Array.from(ASTRAL).length === 1 && ASTRAL.length === 2, 'astral char sanity: 1 code point = 2 UTF-16 units');
    ta17.value = ASTRAL.repeat(2001);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 2, 'revise 2001 astral code points rejected');

    ta17.value = ASTRAL.repeat(2000);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 3 && reviseCalls[2].len === 2000, 'revise 2000 astral code points accepted');

    // --- task_state_invalid -> stale, buttons removed ---
    let env18 = loadChat();
    env18.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env18.win.NAGENT.api.task.approve = () => Promise.reject(new Error('task_state_invalid'));
    sessionsList = [{ id: 's1', title: 's1' }];
    env18.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    env18.chat.init();
    await env18.waitMicro();
    env18.fireClick(env18.findSessionItem('s1'));
    await env18.waitMicro();
    await env18.waitMicro();
    env18.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    const card18 = findByClass(env18.messageStack, 'task-card')[0];
    const approveBtn18 = findByClass(card18, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn18.click();
    await env18.waitMicro();
    await env18.waitMicro();
    ok(findByClass(card18, 'task-card__btn').length === 0, 'task_state_invalid -> stale, buttons removed');

    // --- task_invalid -> controls restored ---
    let env19 = loadChat();
    env19.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env19.win.NAGENT.api.task.approve = () => Promise.reject(new Error('task_invalid'));
    env19.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    await env19.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env19.waitMicro();
    const card19 = findByClass(env19.messageStack, 'task-card')[0];
    const approveBtn19 = findByClass(card19, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn19.click();
    await env19.waitMicro();
    await env19.waitMicro();
    const btns19 = findByClass(card19, 'task-card__btn');
    ok(btns19.length > 0 && btns19.every((b) => !b.disabled), 'task_invalid -> controls restored, not disabled');
    ok(card19.textContent.indexOf('task_invalid') !== -1, 'task_invalid shows error code');

    await env1.waitMicro();
    ok(unhandledCount === 0, 'no unhandledRejection (got ' + unhandledCount + ', reasons=' + JSON.stringify(unhandledReasons.map((r) => r && r.message)) + ')');
  } finally {
    process.removeListener('unhandledRejection', unhandledHandler);
  }
}

// ===========================================================================
// 调试设置按会话独立：spec line 501 — 设置只针对一个会话生效、而不是对所有会话。
// 默认 task=on/tool=off；空态勾选的 draft 在首轮发送后转入新会话；两会话各自独立、
// 切走再切回保留各自设置。getDebugSettings/setDebugSettings 暴露数据模型供断言。
// ===========================================================================
async function testDebugSettingsPerSession() {
  const detailWith = (id, msgs) => ({ session: { id }, messages: msgs, summary: null, task_state: null });

  // --- 空态默认值 ---
  let env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const defaults = env.chat.getDebugSettings();
  ok(defaults.task === true && defaults.tool === false, 'empty state defaults: task on, tool off (got ' + JSON.stringify(defaults) + ')');

  // --- 空态勾选 tool 写入 draft；首轮发送后 draft 转入新会话 ---
  env.chat.setDebugSettings({ task: true, tool: true });
  ok(env.chat.getDebugSettings().tool === true, 'draft toggle: tool visible in empty state');
  env.input.value = 'hello';
  await env.chat.send(); // ensureSession 创建会话并转入 draft
  await env.waitMicro();
  const afterCreate = env.chat.getDebugSettings();
  ok(afterCreate.tool === true, 'draft transferred to new session on first send (got ' + JSON.stringify(afterCreate) + ')');

  // --- 两会话独立：s1 设 tool=true，切到 s2 应回落默认（不受 s1 影响）；切回 s1 保留 ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }, { id: 's2', title: 's2' }];
  env.win.NAGENT.api.getSessionDetail = (sid) => Promise.resolve(detailWith(sid, []));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  env.chat.setDebugSettings({ task: true, tool: true });
  ok(env.chat.getDebugSettings().tool === true, 's1: tool set to visible');
  env.fireClick(env.findSessionItem('s2'));
  await env.waitMicro();
  const s2 = env.chat.getDebugSettings();
  ok(s2.tool === false, 's2 independent: tool reverts to default, not inherited from s1 (got ' + JSON.stringify(s2) + ')');
  env.chat.setDebugSettings({ task: false, tool: false });
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  const s1again = env.chat.getDebugSettings();
  ok(s1again.tool === true && s1again.task === true, 's1 retains its own settings after switching away and back (got ' + JSON.stringify(s1again) + ')');
}

runIntegration().then(async () => {
  await testTaskCardInteraction();
  await testPartialMessageRefresh();
  await testDebugSettingsPerSession();
  if (failures) { console.error('\n' + failures + ' test(s) failed'); process.exit(1); }
  console.log('chat_frontend_harness: all tests passed');
  process.exit(0);
}).catch((e) => {
  console.error('HARNESS ERROR: ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
