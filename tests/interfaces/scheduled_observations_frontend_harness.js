'use strict';
// Behavior harness for scheduled-tasks-observations.js (+ management-api helper).
// Run with: node tests/interfaces/scheduled_observations_frontend_harness.js
// Exits 0 on success, 1 on any failure. Loaded by
// test_scheduled_observations_frontend.py via subprocess; skipped when Node is
// unavailable.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC_DIR = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static');
const RENDERER_JS = path.join(STATIC_DIR, 'scheduled-tasks-observations.js');
const API_JS = path.join(STATIC_DIR, 'management-api.js');
const APP_JS = path.join(STATIC_DIR, 'app.js');
const STYLES_CSS = path.join(STATIC_DIR, 'styles.css');

const rendererCode = fs.readFileSync(RENDERER_JS, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// ---------------------------------------------------------------- DOM stub --
function makeNode(tag) {
  const node = {
    tag: tag,
    className: '',
    _text: '',
    children: [],
    _listeners: {},
    parent: null,
    value: '',
    disabled: false,
    hidden: false,
    type: '',
    dataset: {},
    _classes: new Set(),
    set textContent(v) { this._text = (v === null || v === undefined) ? '' : String(v); this.children = []; },
    get textContent() { return this._text; },
    appendChild(c) { if (c.parent) c.remove(); c.parent = this; this.children.push(c); return c; },
    append() { for (let i = 0; i < arguments.length; i++) this.appendChild(arguments[i]); },
    replaceChildren() {
      this.children.forEach((c) => { c.parent = null; });
      this.children = [];
      this._text = '';
      for (let i = 0; i < arguments.length; i++) this.appendChild(arguments[i]);
    },
    remove() {
      if (this.parent) {
        const i = this.parent.children.indexOf(this);
        if (i >= 0) this.parent.children.splice(i, 1);
        this.parent = null;
      }
    },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    dispatch(ev, event) {
      (this._listeners[ev] || []).forEach((fn) => fn(event || { target: this, preventDefault() {} }));
    },
    click() { this.dispatch('click', { target: this, preventDefault() {} }); },
    setAttribute() {},
    focus() {},
  };
  node.classList = {
    add: function () { for (let i = 0; i < arguments.length; i++) node._classes.add(arguments[i]); },
    remove: function () { for (let i = 0; i < arguments.length; i++) node._classes.delete(arguments[i]); },
    contains: (n) => node._classes.has(n),
    toggle: (n, force) => {
      const want = force === undefined ? !node._classes.has(n) : force;
      if (want) node._classes.add(n); else node._classes.delete(n);
    },
  };
  if (tag === 'select') {
    // 浏览器语义：value 只能取现存 option 的 value；赋不存在值时
    // selectedIndex=-1、value 读回 ''；选项变化后当前值失效则回落首个选项。
    let current = '';
    const revalidate = () => {
      const values = node.children.map((o) => o.value);
      if (values.indexOf(current) === -1) current = values.length ? values[0] : '';
    };
    Object.defineProperty(node, 'value', {
      get: () => current,
      set: (v) => {
        const values = node.children.map((o) => o.value);
        current = values.indexOf(v) !== -1 ? v : '';
      },
    });
    const origAppendChild = node.appendChild;
    node.appendChild = (c) => { const r = origAppendChild.call(node, c); revalidate(); return r; };
    const origReplaceChildren = node.replaceChildren;
    node.replaceChildren = function () {
      const r = origReplaceChildren.apply(node, arguments);
      revalidate();
      return r;
    };
  }
  return node;
}

function makeDocument() {
  const elements = {};
  const body = makeNode('body');
  elements['tab-scheduled-tasks-observations'] = makeNode('div');
  elements['tab-scheduled-tasks-observations'].classList.add('active');
  return {
    elements: elements,
    body: body,
    readyState: 'complete',
    createElement: (t) => makeNode(t),
    getElementById: (id) => elements[id] || null,
    addEventListener() {},
  };
}

function freshEnv(api) {
  const document = makeDocument();
  const ctx = {
    NAGENT: { api: api },
    document: document,
    console: console,
    URLSearchParams: URLSearchParams,
    Promise: Promise,
    setTimeout: setTimeout,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(rendererCode, ctx);
  return { ctx: ctx, document: document, container: document.elements['tab-scheduled-tasks-observations'], body: document.body };
}

// ---------------------------------------------------------------- helpers ---
function tick() { return new Promise((r) => setTimeout(r, 0)); }
async function flush(n) { for (let i = 0; i < (n || 12); i++) await tick(); }

function allTexts(node, out) {
  out = out || [];
  if (node._text) out.push(node._text);
  (node.children || []).forEach((c) => allTexts(c, out));
  return out;
}
function textOf(node) { return allTexts(node).join(''); }
function findAll(node, pred, out) {
  out = out || [];
  if (node && pred(node)) out.push(node);
  ((node && node.children) || []).forEach((c) => findAll(c, pred, out));
  return out;
}
function findOne(node, pred) { return findAll(node, pred)[0] || null; }
function findButton(node, label) {
  return findOne(node, (n) => n.tag === 'button' && textOf(n) === label);
}
function findTable(node) { return findOne(node, (n) => n.tag === 'table'); }
function tableRows(table) {
  const tbody = findOne(table, (n) => n.tag === 'tbody');
  return tbody ? tbody.children.filter((c) => c.tag === 'tr') : [];
}
function rowCells(row) { return row.children.filter((c) => c.tag === 'td'); }
function findSelect(node) { return findOne(node, (n) => n.tag === 'select'); }
function optionLabels(select) { return select.children.map((o) => textOf(o)); }
function hasTag(node, tag) { return !!findOne(node, (n) => n.tag === tag); }

function deferred() {
  let resolve, reject;
  const p = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise: p, resolve: resolve, reject: reject };
}

function makeExec(id, taskId, over) {
  return Object.assign({
    id: id,
    task_id: taskId,
    session_id: 's-' + id,
    status: 'succeeded',
    started_at: '2026-09-16T02:03:04Z',
    completed_at: '2026-09-16T02:04:04Z',
    output: 'out-' + id,
    error: '',
    delivery_status: 'delivered',
    delivery_error: '',
    created_at: '2026-09-16T02:00:00Z',
  }, over || {});
}

function apiStub(impl) {
  impl = impl || {};
  const calls = { tasks: 0, exec: [] };
  return {
    calls: calls,
    api: {
      listScheduledTasks: function () { calls.tasks++; return impl.tasks(); },
      listScheduledTaskExecutionsAll: function (args) {
        calls.exec.push(args);
        return impl.exec(args);
      },
    },
  };
}

function initEnv(stub) {
  const env = freshEnv(stub.api);
  env.ctx.NAGENT.scheduledTasksObservations.init();
  return env;
}

// ---------------------------------------------------------------- scenarios -
async function scenario1() {
  // 正常渲染 + 任务名称回落 + 同名任务标签
  const tasks = [
    { id: 't1', name: 'Alpha' },
    { id: 't2' },                       // name 缺失
    { id: 't3', name: null },           // name null
    { id: 't4', name: '' },             // name 空字符串
    { id: 't5', name: 'Dup' },
    { id: 't6', name: 'Dup' },
  ];
  const execs = [
    makeExec('e1', 't1'),
    makeExec('e2', 't2'),
    makeExec('e3', 't3'),
    makeExec('e4', 't4'),
    makeExec('e5', 't-missing'),        // 任务对象完全缺失
  ];
  const stub = apiStub({ tasks: () => Promise.resolve(tasks), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const table = findTable(env.container);
  ok(!!table, '1: table rendered');
  ok(tableRows(table).length === 5, '1: 5 rows rendered, got ' + tableRows(table).length);
  const rows = tableRows(table);
  const taskCol = rows.map((r) => textOf(rowCells(r)[1]));
  ok(taskCol[0] === 'Alpha', '1: t1 shows name Alpha, got ' + taskCol[0]);
  ok(taskCol[1] === 't2', '1: missing name falls back to id, got ' + taskCol[1]);
  ok(taskCol[2] === 't3', '1: null name falls back to id, got ' + taskCol[2]);
  ok(taskCol[3] === 't4', '1: empty name falls back to id, got ' + taskCol[3]);
  ok(taskCol[4] === 't-missing', '1: missing task object falls back to id, got ' + taskCol[4]);
  const select = findSelect(env.container);
  ok(!!select, '1: filter select rendered');
  const labels = optionLabels(select);
  ok(labels[0] === '全部任务', '1: first option is 全部任务, got ' + labels[0]);
  ok(labels.indexOf('Alpha') !== -1, '1: option label Alpha present');
  ok(labels.indexOf('t2') !== -1 && labels.indexOf('t3') !== -1 && labels.indexOf('t4') !== -1,
    '1: options fall back to id for missing/null/empty names, got ' + labels.join(','));
  ok(labels.indexOf('Dup (t5)') !== -1 && labels.indexOf('Dup (t6)') !== -1,
    '1: duplicate names get id suffix, got ' + labels.join(','));
}

async function scenario2() {
  // 时间渲染: UTC+8 / started_at 缺失回落 created_at / 非法值 '-'
  const execs = [
    makeExec('e1', 't1', { started_at: '2026-09-16T02:03:04Z' }),          // -> 10:03:04
    makeExec('e2', 't1', { started_at: null, created_at: '2026-01-02T16:00:00Z' }), // -> 2026-01-03 00:00:00
    makeExec('e3', 't1', { started_at: 'not-a-date', created_at: 'also-bad' }),
    makeExec('e4', 't1', { started_at: null, created_at: null }),
  ];
  const stub = apiStub({ tasks: () => Promise.resolve([{ id: 't1', name: 'A' }]), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const rows = tableRows(findTable(env.container));
  ok(rows.length === 4, '2: 4 rows, got ' + rows.length);
  const times = rows.map((r) => textOf(rowCells(r)[0]));
  ok(times[0] === '2026-09-16 10:03:04', '2: UTC+8 conversion, got ' + times[0]);
  ok(times[1] === '2026-01-03 00:00:00', '2: started_at missing falls back to created_at, got ' + times[1]);
  ok(times[2] === '-', '2: invalid date renders -, got ' + times[2]);
  ok(times[3] === '-', '2: both missing renders -, got ' + times[3]);
}

async function scenario3() {
  // HTML 字符串按文本展示
  const payload = '<img src=x onerror=alert(1)><b>bold</b>';
  const execs = [makeExec('e1', 't1', { output: payload, error: payload })];
  const stub = apiStub({ tasks: () => Promise.resolve([{ id: 't1', name: 'A' }]), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const row = tableRows(findTable(env.container))[0];
  const outCell = rowCells(row)[3];
  const errCell = rowCells(row)[4];
  ok(textOf(outCell) === payload, '3: output HTML shown as text');
  ok(textOf(errCell) === payload, '3: error HTML shown as text');
  ok(!hasTag(outCell, 'img') && !hasTag(outCell, 'b'), '3: no element injection into output cell');
  ok(!hasTag(errCell, 'img') && !hasTag(errCell, 'b'), '3: no element injection into error cell');
}

async function scenario4() {
  // 执行成功但投递失败: 投递列同时呈现状态与 delivery_error
  const execs = [makeExec('e1', 't1', { status: 'succeeded', delivery_status: 'failed', delivery_error: 'webhook 500' })];
  const stub = apiStub({ tasks: () => Promise.resolve([{ id: 't1', name: 'A' }]), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const delivery = rowCells(tableRows(findTable(env.container))[0])[5];
  const txt = textOf(delivery);
  ok(txt.indexOf('failed') !== -1, '4: delivery_status failed shown, got ' + txt);
  ok(txt.indexOf('webhook 500') !== -1, '4: delivery_error shown alongside status, got ' + txt);
  const statusCell = rowCells(tableRows(findTable(env.container))[0])[2];
  ok(textOf(statusCell) === 'succeeded', '4: status column still succeeded');
}

async function scenario5() {
  // 截断 119/120/121 + 查看 modal + styles.css pre 规则
  const s119 = 'x'.repeat(119);
  const s120 = 'y'.repeat(120);
  const s121 = 'z'.repeat(121);
  const multi = 'line1\n<b>html</b>\n' + 'w'.repeat(200);
  const execs = [
    makeExec('e1', 't1', { output: s119, error: s119 }),
    makeExec('e2', 't1', { output: s120, error: s120 }),
    makeExec('e3', 't1', { output: s121, error: s121 }),
    makeExec('e4', 't1', { output: multi, error: '' }),
  ];
  const stub = apiStub({ tasks: () => Promise.resolve([{ id: 't1', name: 'A' }]), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const rows = tableRows(findTable(env.container));
  const out0 = rowCells(rows[0])[3];
  const out1 = rowCells(rows[1])[3];
  const out2 = rowCells(rows[2])[3];
  ok(textOf(out0) === s119, '5: 119 chars not truncated');
  ok(!findButton(out0, '查看'), '5: 119 chars has no 查看 button');
  ok(textOf(out1) === s120, '5: 120 chars not truncated');
  ok(!findButton(out1, '查看'), '5: 120 chars has no 查看 button');
  ok(textOf(out2).indexOf('z'.repeat(120) + '...') === 0, '5: 121 chars truncated to 120 + ..., got ' + textOf(out2).slice(0, 130));
  const viewBtn = findButton(out2, '查看');
  ok(!!viewBtn, '5: 121 chars has 查看 button');
  const err2 = rowCells(rows[0])[4];
  ok(textOf(err2) === s119, '5: error 119 chars not truncated');
  // modal for truncated output
  viewBtn.click();
  await flush();
  const backdrop = findOne(env.body, (n) => n.className === 'modal-backdrop');
  ok(!!backdrop, '5: 查看 opens shared modal-backdrop');
  ok(!!findOne(backdrop, (n) => n.className === 'modal-dialog'), '5: modal has modal-dialog');
  ok(!!findOne(backdrop, (n) => n.className === 'modal-header'), '5: modal has modal-header');
  const closeBtn = findOne(backdrop, (n) => n.className === 'modal-close');
  ok(!!closeBtn, '5: modal has modal-close');
  const pre = findOne(backdrop, (n) => n.tag === 'pre');
  ok(!!pre && textOf(pre) === s121, '5: modal pre holds full plain text');
  closeBtn.click();
  await flush();
  ok(!findOne(env.body, (n) => n.className === 'modal-backdrop'), '5: modal closed without residue');
  // modal for multi-line HTML text
  const out3 = rowCells(rows[3])[3];
  const viewBtn2 = findButton(out3, '查看');
  ok(!!viewBtn2, '5: multi-line long text has 查看 button');
  viewBtn2.click();
  await flush();
  const backdrop2 = findOne(env.body, (n) => n.className === 'modal-backdrop');
  const pre2 = backdrop2 && findOne(backdrop2, (n) => n.tag === 'pre');
  ok(!!pre2 && textOf(pre2) === multi, '5: multi-line HTML text shown verbatim in modal pre');
  ok(!hasTag(backdrop2, 'b'), '5: modal does not inject HTML elements');
  // backdrop click closes
  backdrop2.dispatch('click', { target: backdrop2, preventDefault() {} });
  await flush();
  ok(!findOne(env.body, (n) => n.className === 'modal-backdrop'), '5: backdrop click closes modal');
  // styles.css pre rules (Node stub does not compute CSS)
  const css = fs.readFileSync(STYLES_CSS, 'utf8');
  const preRuleMatch = css.match(/(^|\})\s*pre\s*\{([^}]*)\}/);
  ok(!!preRuleMatch, '5: styles.css has pre rule');
  if (preRuleMatch) {
    const rule = preRuleMatch[2];
    ok(rule.indexOf('pre-wrap') !== -1, '5: pre rule has pre-wrap');
    ok(/max-height:\s*320px/.test(rule), '5: pre rule has max-height 320px');
    ok(/overflow:\s*auto/.test(rule), '5: pre rule has overflow auto');
  }
}

async function scenario6() {
  // 任务列表失败: 降级 + 名称回落 + 重试提示
  const stub = apiStub({
    tasks: () => Promise.reject(new Error('boom')),
    exec: () => Promise.resolve([makeExec('e1', 't9')]),
  });
  const env = initEnv(stub);
  await flush();
  const select = findSelect(env.container);
  ok(optionLabels(select).length === 1 && optionLabels(select)[0] === '全部任务',
    '6: tasks failure degrades filter to 全部任务 only, got ' + optionLabels(select).join(','));
  const row = tableRows(findTable(env.container))[0];
  ok(!!row && textOf(rowCells(row)[1]) === 't9', '6: names fall back to task_id when list fails');
  const noticeText = textOf(env.container);
  ok(noticeText.indexOf('失败') !== -1 || noticeText.indexOf('重试') !== -1,
    '6: degraded retry notice shown');
  ok(!!findButton(env.container, '重试'), '6: retry button available');

  // 独立处理: 列表先成功 -> 过滤器可操作, 执行仍 pending
  const execD = deferred();
  const stub2 = apiStub({
    tasks: () => Promise.resolve([{ id: 't1', name: 'Alpha' }]),
    exec: () => execD.promise,
  });
  const env2 = initEnv(stub2);
  await flush();
  ok(optionLabels(findSelect(env2.container)).indexOf('Alpha') !== -1,
    '6: filter usable after list success while exec pending');
  execD.resolve([makeExec('e1', 't1')]);
  await flush();
  ok(tableRows(findTable(env2.container)).length === 1, '6: exec resolves after list');

  // 执行先成功, 列表 pending -> 用 id 显示
  const tasksD = deferred();
  const stub3 = apiStub({
    tasks: () => tasksD.promise,
    exec: () => Promise.resolve([makeExec('e1', 't7')]),
  });
  const env3 = initEnv(stub3);
  await flush();
  const row3 = tableRows(findTable(env3.container))[0];
  ok(!!row3 && textOf(rowCells(row3)[1]) === 't7', '6: exec-first renders with id fallback');
  tasksD.resolve([{ id: 't7', name: 'LateName' }]);
  await flush();
  const row3b = tableRows(findTable(env3.container))[0];
  ok(textOf(rowCells(row3b)[1]) === 'LateName', '6: late list success completes names');

  // 执行先失败, 列表 pending -> 可重试错误, 不等待列表
  const tasksD2 = deferred();
  const stub4 = apiStub({
    tasks: () => tasksD2.promise,
    exec: () => Promise.reject(new Error('net-down')),
  });
  const env4 = initEnv(stub4);
  await flush();
  ok(!!findButton(env4.container, '重试'), '6: exec-first failure shows retry without waiting for list');
  ok(textOf(env4.container).indexOf('暂无执行记录') === -1, '6: exec failure not shown as empty');
}

async function scenario7() {
  // 执行查询失败: 错误态 + 重试, 不显示暂无记录; 任务过滤器仍渲染
  let execAttempts = 0;
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 't1', name: 'Alpha' }]),
    exec: () => { execAttempts++; return execAttempts === 1 ? Promise.reject(new Error('io')) : Promise.resolve([makeExec('e1', 't1')]); },
  });
  const env = initEnv(stub);
  await flush();
  ok(textOf(env.container).indexOf('暂无执行记录') === -1, '7: exec failure must not show 暂无执行记录');
  ok(optionLabels(findSelect(env.container)).indexOf('Alpha') !== -1, '7: filter still rendered on exec failure');
  const retry = findButton(env.container, '重试');
  ok(!!retry, '7: retry button shown on exec failure');
  retry.click();
  await flush();
  ok(execAttempts === 2, '7: retry re-issues exec query, attempts=' + execAttempts);
  ok(tableRows(findTable(env.container)).length === 1, '7: retry success renders rows');
}

async function scenario8() {
  // 过滤 404: 提示已不可用 + 清空过滤 + 重新加载全部 (仅一次)
  let unfiltered = 0;
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: (args) => {
      if (args && args.task_id === 'a') return Promise.reject(new Error('scheduled_task_not_found'));
      unfiltered++;
      if (unfiltered <= 2) return Promise.resolve([makeExec('e1', 'a')]);
      return Promise.reject(new Error('still-broken'));
    },
  });
  const env = initEnv(stub);
  await flush();
  // 初始无过滤加载成功
  const select = findSelect(env.container);
  select.value = 'a';
  select.dispatch('change', { target: select, preventDefault() {} });
  await flush();
  ok(textOf(env.container).indexOf('不可用') !== -1, '8: 404 shows 任务已不可用 notice');
  ok(select.value === '', '8: filter cleared after 404, got "' + select.value + '"');
  const lastArgs = stub.calls.exec[stub.calls.exec.length - 1];
  ok(!lastArgs || !lastArgs.task_id, '8: auto-recovery queries without task_id');
  ok(tableRows(findTable(env.container)).length === 1, '8: recovered all-records rendered');
  // 再次过滤 404 -> 恢复后仍失败 -> 错误态, 不无限循环
  select.value = 'a';
  select.dispatch('change', { target: select, preventDefault() {} });
  await flush();
  const execCalls = stub.calls.exec.length;
  await flush();
  ok(stub.calls.exec.length === execCalls, '8: no recursive retry loop after recovery failure');
  ok(!!findButton(env.container, '重试'), '8: post-recovery failure shows error + retry');
}

async function scenario9() {
  // 竞态: 过滤 A->B, A 迟响应不覆盖 B; deactivate 后响应不写 DOM
  const execPromises = {};
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }, { id: 'b', name: 'B' }]),
    exec: (args) => {
      const key = (args && args.task_id) || 'all';
      execPromises[key] = deferred();
      return execPromises[key].promise;
    },
  });
  const env = initEnv(stub);
  await flush();
  execPromises['all'].resolve([]);
  await flush();
  const select = findSelect(env.container);
  select.value = 'a';
  select.dispatch('change', { target: select, preventDefault() {} });
  await flush(3);
  select.value = 'b';
  select.dispatch('change', { target: select, preventDefault() {} });
  await flush(3);
  execPromises['b'].resolve([makeExec('eb', 'b', { output: 'row-of-b' })]);
  await flush();
  ok(textOf(env.container).indexOf('row-of-b') !== -1, '9: B result rendered');
  execPromises['a'].resolve([makeExec('ea', 'a', { output: 'row-of-a-stale' })]);
  await flush();
  ok(textOf(env.container).indexOf('row-of-a-stale') === -1, '9: stale A response must not overwrite B');
  ok(textOf(env.container).indexOf('row-of-b') !== -1, '9: B result still visible after stale A');

  // deactivate: pending responses must not write DOM
  const d1 = deferred();
  const stub2 = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => d1.promise,
  });
  const env2 = initEnv(stub2);
  await flush(3);
  env2.ctx.NAGENT.scheduledTasksObservations.deactivate();
  const before = allTexts(env2.container).join('|');
  d1.resolve([makeExec('e1', 'a', { output: 'after-deactivate' })]);
  await flush();
  ok(textOf(env2.container).indexOf('after-deactivate') === -1, '9: no DOM write after deactivate');
  ok(allTexts(env2.container).join('|') === before, '9: DOM unchanged after deactivate');
}

async function scenario10() {
  // 刷新按钮保留当前过滤重新请求
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => Promise.resolve([makeExec('e1', 'a')]),
  });
  const env = initEnv(stub);
  await flush();
  const select = findSelect(env.container);
  select.value = 'a';
  select.dispatch('change', { target: select, preventDefault() {} });
  await flush();
  const before = stub.calls.exec.length;
  const refreshBtn = findButton(env.container, '刷新');
  ok(!!refreshBtn, '10: refresh button present');
  refreshBtn.click();
  await flush();
  ok(stub.calls.exec.length === before + 1,
    '10: refresh issues exactly one new exec request, calls=' + stub.calls.exec.length + ' before=' + before);
  const lastArgs = stub.calls.exec[stub.calls.exec.length - 1];
  ok(lastArgs && lastArgs.task_id === 'a', '10: refresh keeps current filter, got ' + JSON.stringify(lastArgs));
  ok(select.value === 'a', '10: filter selection preserved after refresh');
}

async function scenario11() {
  // 任务数为零但仍有历史记录
  const stub = apiStub({
    tasks: () => Promise.resolve([]),
    exec: () => Promise.resolve([makeExec('e1', 'gone-1'), makeExec('e2', 'gone-2')]),
  });
  const env = initEnv(stub);
  await flush();
  const rows = tableRows(findTable(env.container));
  ok(rows.length === 2, '11: history rows render with zero tasks, got ' + rows.length);
  ok(textOf(rowCells(rows[0])[1]) === 'gone-1' && textOf(rowCells(rows[1])[1]) === 'gone-2',
    '11: task column falls back to task_id for all rows');
  const labels = optionLabels(findSelect(env.container));
  ok(labels.length === 1 && labels[0] === '全部任务', '11: filter only has 全部任务');
}

async function scenario12() {
  // 列表失败且原过滤 A: 清空选择, 新代次查询全部, 丢弃 A 的成功结果
  let tasksCall = 0;
  const execA2 = deferred();
  const seenFilters = [];
  const stub2 = apiStub({
    tasks: () => { tasksCall++; return tasksCall === 1 ? Promise.resolve([{ id: 'a', name: 'A' }]) : Promise.reject(new Error('list-down')); },
    exec: (args) => {
      seenFilters.push((args && args.task_id) || null);
      if (args && args.task_id === 'a') return execA2.promise;
      return Promise.resolve([makeExec('e1', 'z', { output: 'all-after-recover' })]);
    },
  });
  const env2 = freshEnv(stub2.api);
  env2.ctx.NAGENT.scheduledTasksObservations.init();
  await flush();
  const select2 = findSelect(env2.container);
  select2.value = 'a';
  select2.dispatch('change', { target: select2, preventDefault() {} });
  await flush();
  // 这一代 tasks(第2次) 失败 -> 清空过滤, 新代次查询全部
  ok(select2.value === '', '12: selection cleared after list failure with filter');
  ok(seenFilters[seenFilters.length - 1] === null, '12: new generation queries all records');
  await flush();
  ok(textOf(env2.container).indexOf('all-after-recover') !== -1, '12: recovered all-records rendered');
  // A 的迟成功结果必须被丢弃
  execA2.resolve([makeExec('ea', 'a', { output: 'stale-a-rows' })]);
  await flush();
  ok(textOf(env2.container).indexOf('stale-a-rows') === -1, '12: stale filtered success discarded');

  // 列表成功但 A 消失 -> 同样恢复
  const stub3 = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: (args) => Promise.resolve([makeExec('e1', (args && args.task_id) || 'z', { output: 'rows-' + ((args && args.task_id) || 'all') })]),
  });
  const env3 = freshEnv(stub3.api);
  env3.ctx.NAGENT.scheduledTasksObservations.init();
  await flush();
  const select3 = findSelect(env3.container);
  // 选择 A 后, 下一代列表返回中 A 消失
  stub3.api.listScheduledTasks = () => Promise.resolve([{ id: 'b', name: 'B' }]);
  select3.value = 'a';
  select3.dispatch('change', { target: select3, preventDefault() {} });
  await flush();
  ok(textOf(env3.container).indexOf('不可用') !== -1, '12: selected id vanished shows 不可用');
  ok(select3.value === '', '12: filter cleared when selected id vanishes');
  ok(textOf(env3.container).indexOf('rows-all') !== -1, '12: vanished id recovery loads all records');

  // 恢复后再失败不得递归; 重试按钮恢复成功
  let n = 0;
  const stub4 = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: (args) => {
      if (args && args.task_id === 'a') return Promise.reject(new Error('scheduled_task_not_found'));
      n++;
      return n <= 1 ? Promise.reject(new Error('all-broken')) : Promise.resolve([makeExec('e1', 'a')]);
    },
  });
  const env4 = freshEnv(stub4.api);
  env4.ctx.NAGENT.scheduledTasksObservations.init();
  await flush();
  const select4 = findSelect(env4.container);
  select4.value = 'a';
  select4.dispatch('change', { target: select4, preventDefault() {} });
  await flush();
  const callsAfterRecover = stub4.calls.exec.length;
  await flush();
  ok(stub4.calls.exec.length === callsAfterRecover, '12: recovery failure does not recurse');
  const retry4 = findButton(env4.container, '重试');
  ok(!!retry4, '12: retry button available after recovery failure');
  retry4.click();
  await flush();
  ok(tableRows(findTable(env4.container)).length === 1, '12: manual retry recovers successfully');
}

async function scenario13() {
  // 真实 app.js: 首次 init 未完成时离开再进入
  const document = makeDocument();
  const ctx = {
    NAGENT: {},
    document: document,
    console: console,
    URLSearchParams: URLSearchParams,
    Promise: Promise,
    setTimeout: setTimeout,
  };
  ctx.window = ctx;
  const taskCalls = [];
  const execCalls = [];
  let round = 0;
  ctx.NAGENT.api = {
    listScheduledTasks: () => { const d = deferred(); taskCalls.push(d); return d.promise; },
    listScheduledTaskExecutionsAll: (args) => { const d = deferred(); execCalls.push({ d: d, args: args }); return d.promise; },
  };
  vm.createContext(ctx);
  vm.runInContext(rendererCode, ctx);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), ctx);
  const onTabActivated = ctx.NAGENT.app && ctx.NAGENT.app.onTabActivated;
  ok(typeof onTabActivated === 'function', '13: app.js exposes onTabActivated');
  // 首次进入: init 挂起
  const first = onTabActivated('scheduled-tasks-observations');
  await flush(3);
  ok(taskCalls.length === 1 && execCalls.length === 1, '13: first activation issues 1+1 requests');
  // 离开再进入 (init 尚未完成)
  onTabActivated('chat');
  await flush(3);
  const second = onTabActivated('scheduled-tasks-observations');
  await flush(3);
  ok(taskCalls.length === 2 && execCalls.length === 2,
    '13: re-entry during pending init immediately issues new requests, tasks=' + taskCalls.length + ' exec=' + execCalls.length);
  // 新代次先完成
  taskCalls[1].resolve([{ id: 'a', name: 'A' }]);
  execCalls[1].d.resolve([makeExec('e1', 'a', { output: 'new-gen-rows' })]);
  await flush();
  const container = document.elements['tab-scheduled-tasks-observations'];
  ok(textOf(container).indexOf('new-gen-rows') !== -1, '13: new generation result rendered');
  // 旧请求随后失败也不得覆盖, 且不得触发 app.js 的 renderModuleError
  execCalls[0].d.reject(new Error('old-exec-failed'));
  taskCalls[0].reject(new Error('old-tasks-failed'));
  await flush();
  ok(textOf(container).indexOf('new-gen-rows') !== -1, '13: old failure does not overwrite new result');
  ok(!findOne(container, (n) => n.className === 'module-error'), '13: stale failure must not raise app.js module-error');
  await Promise.allSettled([first, second]);
}

async function scenario14() {
  // 请求中保留控件 + 加载态; 空态/五态/未知状态/空值/最近50条/服务端顺序
  const d = deferred();
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => d.promise,
  });
  const env = initEnv(stub);
  await flush(3);
  ok(!!findSelect(env.container), '14: filter control kept during load');
  ok(!!findButton(env.container, '刷新'), '14: refresh control kept during load');
  ok(!findSelect(env.container).disabled, '14: filter not disabled during load');
  ok(textOf(env.container).indexOf('加载') !== -1, '14: loading state visible');
  d.resolve([]);
  await flush();
  ok(textOf(env.container).indexOf('暂无执行记录') !== -1, '14: empty array shows 暂无执行记录');

  const statuses = ['running', 'succeeded', 'failed', 'blocked', 'skipped_missed', 'mystery'];
  const execs = statuses.map((s, i) => makeExec('e' + i, 'a', { status: s, output: '', error: null }));
  const stub2 = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => Promise.resolve(execs),
  });
  const env2 = initEnv(stub2);
  await flush();
  const rows = tableRows(findTable(env2.container));
  ok(rows.length === 6, '14: six rows rendered in server order');
  statuses.forEach((s, i) => {
    const cell = rowCells(rows[i])[2];
    ok(textOf(cell) === s, '14: status badge shows raw value ' + s + ', got ' + textOf(cell));
  });
  const mysteryBadge = findOne(rowCells(rows[5])[2], (n) => n.className.indexOf('badge') !== -1);
  ok(!!mysteryBadge, '14: unknown status renders badge');
  ok(mysteryBadge.className === 'badge', '14: unknown status uses neutral badge, got ' + mysteryBadge.className);
  ['running', 'succeeded', 'failed', 'blocked', 'skipped_missed'].forEach((s, i) => {
    const b = findOne(rowCells(rows[i])[2], (n) => n.className.indexOf('badge') !== -1);
    ok(b && b.className.indexOf('badge--') !== -1, '14: known status ' + s + ' has styled badge, got ' + (b && b.className));
  });
  ok(textOf(rowCells(rows[0])[3]) === '-', '14: empty output shows -');
  ok(textOf(rowCells(rows[0])[4]) === '-', '14: null error shows -');
  ok(textOf(env2.container).indexOf('最近 50 条') !== -1, '14: header notes 最近 50 条');
  // 服务端顺序保留 (按 id 倒序给出, 渲染必须同序)
  const ids = rows.map((r, i) => 'e' + i);
  ok(JSON.stringify(ids) === JSON.stringify(['e0', 'e1', 'e2', 'e3', 'e4', 'e5']),
    '14: server order preserved, got ' + ids.join(','));
}

async function scenario15() {
  // management-api.js helper: URL 构造与错误
  const calls = [];
  let responder = () => ({ ok: true, status: 200, json: async () => [] });
  const ctx = {
    NAGENT: {},
    console: console,
    URLSearchParams: URLSearchParams,
    Promise: Promise,
    fetch: (url, opts) => { calls.push(url); return Promise.resolve(responder(url, opts)); },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(API_JS, 'utf8'), ctx);
  const api = ctx.NAGENT.api;
  ok(typeof api.listScheduledTaskExecutionsAll === 'function', '15: helper exported on namespace.api');

  await api.listScheduledTaskExecutionsAll();
  ok(calls[0] === '/chat/scheduled-tasks/executions?limit=50',
    '15: no filter omits task_id, limit=50, got ' + calls[0]);

  await api.listScheduledTaskExecutionsAll({});
  ok(calls[1] === '/chat/scheduled-tasks/executions?limit=50', '15: empty args same as no args');

  const weirdId = 'a&b?c d中';
  await api.listScheduledTaskExecutionsAll({ task_id: weirdId });
  const expected = '/chat/scheduled-tasks/executions?' + new URLSearchParams({ task_id: weirdId, limit: '50' }).toString();
  ok(calls[2] === expected, '15: special chars URLSearchParams-encoded, got ' + calls[2]);

  await api.listScheduledTaskExecutionsAll({ task_id: 'plain', limit: 7 });
  ok(calls[3] === '/chat/scheduled-tasks/executions?task_id=plain&limit=7',
    '15: explicit limit honored, got ' + calls[3]);

  responder = () => ({ ok: false, status: 404, json: async () => ({ error: { code: 'scheduled_task_not_found' } }) });
  let errMsg = null;
  try { await api.listScheduledTaskExecutionsAll({ task_id: 'gone' }); } catch (e) { errMsg = e.message; }
  ok(errMsg === 'scheduled_task_not_found', '15: non-2xx rejects with business code, got ' + errMsg);
}

async function main() {
  await scenario1();
  await scenario2();
  await scenario3();
  await scenario4();
  await scenario5();
  await scenario6();
  await scenario7();
  await scenario8();
  await scenario9();
  await scenario10();
  await scenario11();
  await scenario12();
  await scenario13();
  await scenario14();
  await scenario15();
  if (failures) { console.error(failures + ' failure(s)'); process.exit(1); }
  console.log('OK scheduled observations frontend harness passed');
  process.exit(0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(1); });
