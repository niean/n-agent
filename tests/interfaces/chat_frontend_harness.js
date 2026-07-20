'use strict';
// Harness for chat.js /task slash command. Loads chat.js in a Node vm with
// minimal DOM/api stubs and exercises parseTaskCommand (pure), runTaskCommand
// (dispatch + error mapping + [任务指令] prefix) and send() routing.
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
    appendChild(c) { kids.push(c); return c; },
    removeChild(c) { const i = kids.indexOf(c); if (i >= 0) kids.splice(i, 1); return c; },
    replaceChildren() { kids.length = 0; },
    append(...cs) { cs.forEach((c) => kids.push(c)); },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener() {},
    dispatchEvent(ev) { (this._listeners[(ev && ev.type) || ''] || []).forEach((fn) => fn(ev)); },
    querySelector() { return null; },
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
    approve(id, note) { taskCalls.push({ name: 'approve', id, note }); return Promise.resolve({}); },
    reject(id, note) { taskCalls.push({ name: 'reject', id, note }); return Promise.resolve({}); },
    cancel(id) { taskCalls.push({ name: 'cancel', id }); return Promise.resolve({}); },
    retry(id) { taskCalls.push({ name: 'retry', id }); return Promise.resolve({}); },
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
    createElement: () => { const el = makeEl(); createdElements.push(el); return el; },
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
  // create: binds to currentSessionId, [任务指令] prefix, fetch not called
  let env = loadChat();
  env.input.value = '/task create 报告 --body 正文 --priority 2 --goal';
  await env.chat.send();
  const createCall = taskCalls.find((c) => c.name === 'create');
  ok(!!createCall, 'create called api.task.create');
  ok(createCall && createCall.payload.title === '报告' && createCall.payload.body === '正文' && createCall.payload.priority === 2 && createCall.payload.goal_mode === true, 'create payload maps body/priority/goal_mode (got ' + JSON.stringify(createCall && createCall.payload) + ')');
  ok(createCall && createdSessionId && createCall.payload.origin_session_id === createdSessionId, 'create binds origin_session_id to current session (got ' + JSON.stringify(createCall && createCall.payload.origin_session_id) + ', session=' + createdSessionId + ')');
  ok(fetchCalls.length === 0, 'create does not call /chat/completions');
  let msg = lastSystemText(env);
  ok(typeof msg === 'string' && msg.indexOf('[任务指令]') !== -1 && msg.indexOf('已创建任务 t_1') !== -1, 'create system message has [任务指令] prefix and task id (got ' + JSON.stringify(msg) + ')');
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
  ok(lastSystemText(env).indexOf('[任务指令] 已批准任务 t_7') !== -1, 'approve message (got ' + JSON.stringify(lastSystemText(env)) + ')');

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
  ok(errMsg.indexOf('[任务指令]') !== -1 && errMsg.indexOf('任务不存在') !== -1 && errMsg.indexOf('task_not_found') !== -1, 'error maps task_not_found with code (got ' + JSON.stringify(errMsg) + ')');
  ok(fetchCalls.length === 0, 'error path does not call /chat/completions');

  // unknown error: safe generic message includes String(error)
  env = loadChat();
  env.win.NAGENT.api.task.cancel = () => Promise.reject(new Error('something_unexpected'));
  env.input.value = '/task cancel t_x';
  await env.chat.send();
  ok(lastSystemText(env).indexOf('something_unexpected') !== -1, 'unknown error includes raw code (got ' + JSON.stringify(lastSystemText(env)) + ')');

  // parser error: no api call, no fetch, [任务指令] usage message
  env = loadChat();
  env.input.value = '/task frobnicate x';
  await env.chat.send();
  ok(taskCalls.length === 0 && fetchCalls.length === 0, 'parser error calls no api and no fetch');
  ok(lastSystemText(env).indexOf('[任务指令]') !== -1 && lastSystemText(env).indexOf('未知子命令') !== -1, 'parser error renders [任务指令] usage (got ' + JSON.stringify(lastSystemText(env)) + ')');

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
  ok(lastSystemText(env) && lastSystemText(env).indexOf('[任务指令]') !== -1, 'local system message still shown when persist fails');

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
    { id: 'msg_' + (coordAppendBase + 1), role: 'system', content: '[任务指令] 执行命令: /task list' },
    { id: lastPersistedId, role: 'system', content: '[任务指令] 当前会话无关联任务' },
  ];
  const beforePoll = env.messageStack._kids.length;
  env.tickTimers();
  await env.waitMicro();
  ok(env.messageStack._kids.length === beforePoll, '/task coordination: next poll no spurious re-render (got ' + env.messageStack._kids.length + ', before=' + beforePoll + ')');
}

runIntegration().then(() => {
  if (failures) { console.error('\n' + failures + ' test(s) failed'); process.exit(1); }
  console.log('chat_frontend_harness: all tests passed');
  process.exit(0);
}).catch((e) => {
  console.error('HARNESS ERROR: ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
