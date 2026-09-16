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
    setAttribute(k, v) { (this._attrs = this._attrs || {})[k] = String(v); },
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
  // 安全渲染红线：任何 innerHTML 写入立即抛错，不靠"桩不解析 HTML"漏检。
  Object.defineProperty(node, 'innerHTML', {
    get: function () { return ''; },
    set: function () { throw new Error('innerHTML assignment is forbidden (use textContent)'); },
  });
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
function findByClass(node, cls) { return findOne(node, (n) => (n.className || '').split(' ').indexOf(cls) !== -1); }
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

// 观测页契约：每次执行记录请求都无 task_id 自有属性且 limit === 50。
// 放在 apiStub 入口，所有刷新/重试/重入代次都校验；共享 helper 显式
// task_id 的用例走真实 management-api.js，不受此限制。
function assertExecArgs(args, label) {
  ok(!!args && typeof args === 'object', label + ': exec args object present');
  ok(!!args && !Object.prototype.hasOwnProperty.call(args, 'task_id'), label + ': exec args have no own task_id');
  ok(!!args && args.limit === 50, label + ': exec args limit === 50, got ' + (args && args.limit));
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
        assertExecArgs(args, 'api');
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

// deferredEnv: 每代的 tasks/exec 各入一个 deferred，按下标精确控制每代两端。
function deferredEnv() {
  const tasksDs = [];
  const execDs = [];
  const stub = apiStub({
    tasks: () => { const d = deferred(); tasksDs.push(d); return d.promise; },
    exec: () => { const d = deferred(); execDs.push(d); return d.promise; },
  });
  const env = freshEnv(stub.api);
  const ns = env.ctx.NAGENT.scheduledTasksObservations;
  return { env: env, ns: ns, stub: stub, tasksDs: tasksDs, execDs: execDs };
}

// ---------------------------------------------------------------- scenarios -
async function scenario1() {
  // 标准 status-panel 骨架 + 任务名称回落
  const tasks = [
    { id: 't1', name: 'Alpha' },
    { id: 't2' },                       // name 缺失
    { id: 't3', name: null },           // name null
    { id: 't4', name: '' },             // name 空字符串
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
  // 骨架层级（ok 只累计失败不抛异常，全部先守卫再解引用）
  const panels = env.container.children.filter((c) => c.tag === 'section' && c.className === 'status-panel');
  ok(panels.length === 1, '1: exactly one section.status-panel in root, got ' + panels.length);
  const panel = panels[0] || null;
  ok(!!panel && panel.children.length === 2, '1: panel has exactly header+body, got ' + (panel && panel.children.length));
  const header = panel && panel.children[0];
  const body = panel && panel.children[1];
  ok(!!header && header.className === 'panel-header', '1: first child is panel-header, got ' + (header && header.className));
  ok(!!body && body.className === 'panel-body', '1: second child is panel-body, got ' + (body && body.className));
  const titleGroup = findByClass(header, 'panel-title-group');
  ok(!!titleGroup, '1: panel-title-group under header');
  ok(!!titleGroup && !!titleGroup._attrs && titleGroup._attrs.tabindex === '0', '1: title group keyboard-focusable');
  ok(!!titleGroup && !!titleGroup._attrs && titleGroup._attrs['aria-describedby'] === 'scheduled-obs-tips', '1: title group aria-describedby');
  const title = findByClass(titleGroup, 'panel-title');
  ok(!!title && textOf(title) === '执行记录', '1: panel title 执行记录, got ' + (title && textOf(title)));
  const tips = findByClass(titleGroup, 'panel-tips');
  ok(!!tips && tips.id === 'scheduled-obs-tips' && !!tips._attrs && tips._attrs.role === 'tooltip', '1: tips id + role=tooltip');
  ok((tips ? textOf(tips) : '').indexOf('最近 50 条') !== -1, '1: tips notes 最近 50 条');
  ok(!findByClass(header, 'panel-actions'), '1: no panel-actions (refresh button removed)');
  ok(!findButton(header, '刷新'), '1: no 刷新 button in header');
  ok(!!body && body.children.length === 2, '1: body has notice+table areas, got ' + (body && body.children.length));
  const tableArea = body && body.children[1];
  const wrap = findByClass(tableArea, 'scheduled-table-wrap');
  ok(!!wrap, '1: scheduled-table-wrap inside table area');
  const table = findTable(wrap);
  ok(!!table && table.className === 'document-table scheduled-history-table', '1: standard table classes, got ' + (table && table.className));
  ok(!findOne(env.container, (n) => n.tag === 'select'), '1: no select element anywhere');
  const thead = findOne(table, (n) => n.tag === 'thead');
  const headRow = thead && thead.children[0];
  const headLabels = (headRow ? headRow.children : []).map((c) => textOf(c));
  ok(JSON.stringify(headLabels) === JSON.stringify(['开始时间', '任务', '状态', '输出', '投递', '操作']),
    '1: six columns (错误 removed, 操作 added), got ' + headLabels.join(','));
  // 名称回落
  const rows = table ? tableRows(table) : [];
  ok(rows.length === 5, '1: 5 rows rendered, got ' + rows.length);
  const taskCol = rows.map((r) => textOf(rowCells(r)[1]));
  ok(taskCol[0] === 'Alpha', '1: t1 shows name Alpha, got ' + taskCol[0]);
  ok(taskCol[1] === 't2', '1: missing name falls back to id, got ' + taskCol[1]);
  ok(taskCol[2] === 't3', '1: null name falls back to id, got ' + taskCol[2]);
  ok(taskCol[3] === 't4', '1: empty name falls back to id, got ' + taskCol[3]);
  ok(taskCol[4] === 't-missing', '1: missing task object falls back to id, got ' + taskCol[4]);
}

async function scenario2() {
  // 时间渲染: UTC+8 / started_at 缺失回落 created_at / 非法值 '-'
  const execs = [
    makeExec('e1', 't1', { started_at: '2026-09-16T02:03:04Z' }),          // -> 10:03:04
    makeExec('e2', 't1', { started_at: null, created_at: '2026-01-02T16:00:00Z' }), // -> 2026-01-03 00:00:00
    makeExec('e3', 't1', { started_at: 'not-a-date', created_at: 'also-bad' }),
    makeExec('e4', 't1', { started_at: null, created_at: null }),
    makeExec('e5', 't1', { started_at: 'not-a-date', created_at: '2026-01-02T16:00:00Z' }), // started 非法不得回落 created
  ];
  const stub = apiStub({ tasks: () => Promise.resolve([{ id: 't1', name: 'A' }]), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const rows = tableRows(findTable(env.container));
  ok(rows.length === 5, '2: 5 rows, got ' + rows.length);
  const times = rows.map((r) => textOf(rowCells(r)[0]));
  ok(times[0] === '2026-09-16 10:03:04', '2: UTC+8 conversion, got ' + times[0]);
  ok(times[1] === '2026-01-03 00:00:00', '2: started_at missing falls back to created_at, got ' + times[1]);
  ok(times[2] === '-', '2: invalid date renders -, got ' + times[2]);
  ok(times[3] === '-', '2: both missing renders -, got ' + times[3]);
  ok(times[4] === '-', '2: invalid started_at must not fall back to created_at, got ' + times[4]);
}

async function scenario3() {
  // HTML 字符串按文本展示（innerHTML 抛错 setter 兜底任何写入）；
  // 错误列已移除，错误内容在详情 modal 中按纯文本展示
  const payload = '<img src=x onerror=alert(1)><b>bold</b>';
  const execs = [makeExec('e1', 't1', { output: payload, error: payload })];
  const stub = apiStub({ tasks: () => Promise.resolve([{ id: 't1', name: 'A' }]), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const row = tableRows(findTable(env.container))[0];
  const outCell = rowCells(row)[3];
  ok(textOf(outCell) === payload, '3: output HTML shown as text');
  ok(!hasTag(outCell, 'img') && !hasTag(outCell, 'b'), '3: no element injection into output cell');
  const detailBtn = findButton(rowCells(row)[5], '详情');
  ok(!!detailBtn, '3: 详情 button in action column');
  detailBtn.click();
  await flush();
  const backdrop = findOne(env.body, (n) => n.className === 'modal-backdrop');
  ok(!!backdrop, '3: 详情 opens modal-backdrop');
  const pres = findAll(backdrop, (n) => n.tag === 'pre');
  ok(pres.length === 2 && textOf(pres[0]) === payload && textOf(pres[1]) === payload,
    '3: modal output/error pre hold payload as text');
  ok(!hasTag(backdrop, 'img') && !hasTag(backdrop, 'b'), '3: no element injection into modal');
  findOne(backdrop, (n) => n.className === 'modal-close').click();
  await flush();
}

async function scenario4() {
  // 投递列: 仅展示投递状态，成功/失败详情（投递错误）移入详情 modal
  const execs = [
    makeExec('e1', 't1', { status: 'succeeded', delivery_status: 'failed', delivery_error: 'webhook 500' }),
    makeExec('e2', 't1', { delivery_status: '', delivery_error: '' }),
    makeExec('e3', 't1', { delivery_status: 'delivered', delivery_error: '' }),
    makeExec('e4', 't1', { delivery_status: '', delivery_error: 'queue down' }),
    makeExec('e5', 't1', { delivery_status: '', delivery_error: 'd'.repeat(150) }),
  ];
  const stub = apiStub({ tasks: () => Promise.resolve([{ id: 't1', name: 'A' }]), exec: () => Promise.resolve(execs) });
  const env = initEnv(stub);
  await flush();
  const rows = tableRows(findTable(env.container));
  ok(rows.length === 5, '4: 5 rows, got ' + rows.length);
  const cells = rows.map((r) => rowCells(r)[4]);
  ok(textOf(cells[0]) === 'failed', '4: delivery column shows status only, got ' + textOf(cells[0]));
  ok(textOf(cells[0]).indexOf('webhook 500') === -1, '4: delivery column hides delivery_error detail');
  ok(textOf(rowCells(rows[0])[2]) === 'succeeded', '4: status column still succeeded');
  ok(textOf(cells[1]) === '-', '4: empty delivery status shows -, got ' + textOf(cells[1]));
  ok(textOf(cells[2]) === 'delivered', '4: delivered status shown, got ' + textOf(cells[2]));
  ok(textOf(cells[3]) === '-', '4: error-only delivery shows - in list (detail in modal), got ' + textOf(cells[3]));
  ok(textOf(cells[4]) === '-', '4: long delivery_error not shown in list');
  ok(!findButton(cells[4], '查看'), '4: delivery column has no 查看 button');
  // 详情 modal 呈现完整投递信息
  findButton(rowCells(rows[0])[5], '详情').click();
  await flush();
  const backdrop = findOne(env.body, (n) => n.className === 'modal-backdrop');
  ok(!!backdrop, '4: 详情 opens modal');
  const modalText = textOf(backdrop);
  ok(modalText.indexOf('投递: failed') !== -1, '4: modal shows delivery status, got ' + modalText.slice(0, 200));
  ok(modalText.indexOf('webhook 500') !== -1, '4: modal shows delivery_error detail');
  findOne(backdrop, (n) => n.className === 'modal-close').click();
  await flush();
}

async function scenario5() {
  // 输出列定长截断（无查看按钮）+ 详情 modal 全量呈现 + 代理对按 JS 码元计数 + styles.css 规则
  const s119 = 'x'.repeat(119);
  const s120 = 'y'.repeat(120);
  const s121 = 'z'.repeat(121);
  const multi = 'line1\n<b>html</b>\n' + 'w'.repeat(200);
  const emoji = '\u{1F600}'.repeat(121); // JS length 242，slice(0,120) = 60 个代理对
  const execs = [
    makeExec('e1', 't1', { output: s119, error: s119 }),
    makeExec('e2', 't1', { output: s120, error: s120 }),
    makeExec('e3', 't1', { output: s121, error: s121 }),
    makeExec('e4', 't1', { output: multi, error: multi }),
    makeExec('e5', 't1', { output: emoji, error: '' }),
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
  ok(textOf(out2) === 'z'.repeat(120) + '...', '5: 121 chars truncated to 120 + ..., got ' + textOf(out2).slice(0, 130));
  ok(!findButton(out2, '查看'), '5: 121 chars has no 查看 button (detail modal instead)');
  const out4 = rowCells(rows[4])[3];
  ok(textOf(out4) === '\u{1F600}'.repeat(60) + '...', '5: surrogate pairs counted by JS code units');
  ok(!findButton(out4, '查看'), '5: emoji text has no 查看 button');
  // 输出列一律无查看按钮；操作列一律有详情按钮
  rows.forEach((row, i) => {
    ok(!findButton(rowCells(row)[3], '查看'), '5: row ' + i + ' output column never has 查看 button');
    ok(!!findButton(rowCells(row)[5], '详情'), '5: row ' + i + ' action column has 详情 button');
  });
  // 详情 modal: 完整输出/错误 + 标准结构
  findButton(rowCells(rows[2])[5], '详情').click();
  await flush();
  const backdrop = findOne(env.body, (n) => n.className === 'modal-backdrop');
  ok(!!backdrop, '5: 详情 opens shared modal-backdrop');
  ok(!!findOne(backdrop, (n) => n.className === 'modal-dialog'), '5: modal has modal-dialog');
  ok(!!findOne(backdrop, (n) => n.className === 'modal-header'), '5: modal has modal-header');
  ok(!!findOne(backdrop, (n) => n.className === 'providers-form'), '5: modal uses providers-form standard style');
  ok(!!findByClass(backdrop, 'scheduled-exec-detail-modal-body'), '5: modal has detail body class');
  const closeBtn = findOne(backdrop, (n) => n.className === 'modal-close');
  ok(!!closeBtn, '5: modal has modal-close');
  const pres = findAll(backdrop, (n) => n.tag === 'pre');
  ok(pres.length === 2 && textOf(pres[0]) === s121 && textOf(pres[1]) === s121,
    '5: modal pre holds full output/error plain text');
  ok(textOf(backdrop).indexOf('执行时间: 2026-09-16 10:03:04') !== -1, '5: modal shows 执行时间 (UTC+8)');
  ok(!!findOne(backdrop, (n) => (n.className || '').indexOf('badge') !== -1), '5: modal shows status badge');
  ok(!!findOne(backdrop, (n) => n.className === 'providers-form__actions'), '5: modal has standard actions');
  closeBtn.click();
  await flush();
  ok(!findOne(env.body, (n) => n.className === 'modal-backdrop'), '5: modal closed without residue');
  // 详情 modal: 多行 HTML 文本逐字呈现; 空白处点击不退出, 仅 × / 关闭 按钮退出
  findButton(rowCells(rows[3])[5], '详情').click();
  await flush();
  const backdrop2 = findOne(env.body, (n) => n.className === 'modal-backdrop');
  const pres2 = backdrop2 ? findAll(backdrop2, (n) => n.tag === 'pre') : [];
  ok(pres2.length === 2 && textOf(pres2[0]) === multi && textOf(pres2[1]) === multi,
    '5: multi-line HTML text shown verbatim in modal pre');
  ok(textOf(pres2[0]).indexOf('\n') !== -1, '5: modal pre keeps newlines');
  ok(!hasTag(backdrop2, 'b'), '5: modal does not inject HTML elements');
  backdrop2.dispatch('click', { target: backdrop2, preventDefault() {} });
  await flush();
  ok(!!findOne(env.body, (n) => n.className === 'modal-backdrop'), '5: backdrop click does NOT close modal');
  findButton(backdrop2, '关闭').click();
  await flush();
  ok(!findOne(env.body, (n) => n.className === 'modal-backdrop'), '5: 关闭 button closes modal');
  // styles.css pre rules + detail body rule (Node stub does not compute CSS)
  const css = fs.readFileSync(STYLES_CSS, 'utf8');
  const preRuleMatch = css.match(/(^|\})\s*pre\s*\{([^}]*)\}/);
  ok(!!preRuleMatch, '5: styles.css has pre rule');
  if (preRuleMatch) {
    const rule = preRuleMatch[2];
    ok(rule.indexOf('pre-wrap') !== -1, '5: pre rule has pre-wrap');
    ok(/max-height:\s*320px/.test(rule), '5: pre rule has max-height 320px');
    ok(/overflow:\s*auto/.test(rule), '5: pre rule has overflow auto');
  }
  ok(css.indexOf('.scheduled-exec-detail-modal-body') !== -1, '5: styles.css has detail modal body rule');
}

async function scenario6() {
  // 任务列表失败: notice 降级 + 名称回落 + 重试, 不自动重载
  const stub = apiStub({
    tasks: () => Promise.reject(new Error('boom')),
    exec: () => Promise.resolve([makeExec('e1', 't9')]),
  });
  const env = initEnv(stub);
  await flush();
  const body = findByClass(env.container, 'panel-body');
  ok(!!body && body.children.length === 2, '6: panel-body has notice+table areas');
  const noticeArea = body && body.children[0];
  ok(!!noticeArea && textOf(noticeArea).indexOf('任务列表加载失败，名称暂以任务 ID 显示') !== -1,
    '6: tasks failure shows exact degraded notice, got ' + (noticeArea && textOf(noticeArea)));
  const row = tableRows(findTable(env.container))[0];
  ok(!!row && textOf(rowCells(row)[1]) === 't9', '6: names fall back to task_id when list fails');
  ok(!!findButton(noticeArea || env.container, '重试'), '6: retry button available in notice area');
  ok(stub.calls.tasks === 1, '6: tasks failure triggers no auto reload');

  // 独立处理: 列表先成功, 执行仍 pending -> 表格区保持加载态; exec 到后应用名称
  const execD = deferred();
  const stub2 = apiStub({
    tasks: () => Promise.resolve([{ id: 't1', name: 'Alpha' }]),
    exec: () => execD.promise,
  });
  const env2 = initEnv(stub2);
  await flush();
  const body2 = findByClass(env2.container, 'panel-body');
  const tableArea2 = body2 && body2.children[1];
  ok(!!tableArea2 && textOf(tableArea2).indexOf('加载中') !== -1, '6: table area stays loading while exec pending');
  execD.resolve([makeExec('e1', 't1')]);
  await flush();
  const row2 = tableRows(findTable(env2.container))[0];
  ok(!!row2 && textOf(rowCells(row2)[1]) === 'Alpha', '6: exec resolves after list, name applied');

  // 执行先成功, 列表 pending -> 用 id 显示; 后到列表补全名称
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
  // 执行失败: 错误态 + 手动重试重新发起一组请求; 无自动重试
  let execAttempts = 0;
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 't1', name: 'Alpha' }]),
    exec: () => { execAttempts++; return execAttempts === 1 ? Promise.reject(new Error('io')) : Promise.resolve([makeExec('e1', 't1')]); },
  });
  const env = initEnv(stub);
  await flush();
  ok(textOf(env.container).indexOf('暂无执行记录') === -1, '7: exec failure must not show 暂无执行记录');
  ok(textOf(env.container).indexOf('执行记录加载失败') !== -1, '7: exec failure shows error state');
  ok(stub.calls.exec.length === 1 && stub.calls.tasks === 1, '7: no auto retry after exec failure');
  const retry = findButton(env.container, '重试');
  ok(!!retry, '7: retry button shown on exec failure');
  retry.click();
  await flush();
  ok(stub.calls.exec.length === 2 && stub.calls.tasks === 2, '7: retry re-issues 1+1 requests');
  ok(tableRows(findTable(env.container)).length === 1, '7: retry success renders rows');
  ok(textOf(env.container).indexOf('加载失败') === -1, '7: retry success clears error states');

  // 双失败: notice 区与表格区提示并存, 无自动重试; 两类重试按钮各自重新发起一组请求。
  // 两个按钮用独立环境: 首次点击后另一按钮已随重渲染从 DOM 移除。
  async function doubleFailureEnv(clickArea) {
    let t = 0;
    let e = 0;
    const stubX = apiStub({
      tasks: () => { t++; return t === 1 ? Promise.reject(new Error('list-down')) : Promise.resolve([{ id: 't1', name: 'Alpha' }]); },
      exec: () => { e++; return e === 1 ? Promise.reject(new Error('exec-down')) : Promise.resolve([makeExec('e1', 't1')]); },
    });
    const envX = initEnv(stubX);
    await flush();
    const body = findByClass(envX.container, 'panel-body');
    ok(!!body && body.children.length === 2, '7: ' + clickArea + ': panel-body has notice+table areas');
    const noticeArea = body && body.children[0];
    const tableArea = body && body.children[1];
    ok(!!noticeArea && textOf(noticeArea).indexOf('任务列表加载失败，名称暂以任务 ID 显示') !== -1,
      '7: ' + clickArea + ': double failure keeps tasks notice');
    ok(!!tableArea && textOf(tableArea).indexOf('执行记录加载失败') !== -1, '7: ' + clickArea + ': double failure keeps exec error');
    ok(textOf(envX.container).indexOf('暂无执行记录') === -1, '7: ' + clickArea + ': double failure not shown as empty');
    ok(stubX.calls.tasks === 1 && stubX.calls.exec.length === 1, '7: ' + clickArea + ': no auto retry after double failure');
    const btn = findButton(clickArea === 'notice' ? noticeArea : tableArea, '重试');
    ok(!!btn, '7: ' + clickArea + ': retry button present in ' + clickArea + ' area');
    if (btn) btn.click();
    await flush();
    ok(stubX.calls.tasks === 2 && stubX.calls.exec.length === 2, '7: ' + clickArea + ': retry re-issues 1+1');
    ok(textOf(envX.container).indexOf('加载失败') === -1, '7: ' + clickArea + ': retry success clears both errors');
    const rowsX = tableRows(findTable(envX.container));
    ok(rowsX.length === 1 && textOf(rowCells(rowsX[0])[1]) === 'Alpha', '7: ' + clickArea + ': retry success renders rows with names');
  }
  await doubleFailureEnv('notice');
  await doubleFailureEnv('table');

  // 404 scheduled_task_not_found 不再专属恢复: 统一错误态 + 手动重试, 无自动补发
  const err404 = Object.assign(new Error('scheduled_task_not_found'), { status: 404 });
  const stub404 = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => Promise.reject(err404),
  });
  const env404 = initEnv(stub404);
  await flush();
  ok(textOf(env404.container).indexOf('执行记录加载失败') !== -1, '7: 404 shows generic exec failure');
  ok(textOf(env404.container).indexOf('不可用') === -1, '7: 404 shows no 任务已不可用 notice');
  ok(textOf(env404.container).indexOf('全部任务') === -1, '7: 404 shows no switch-to-all notice');
  ok(stub404.calls.tasks === 1 && stub404.calls.exec.length === 1, '7: 404 triggers no auto recovery request');
  ok(!!findButton(env404.container, '重试'), '7: 404 shows manual retry');

  // 列表 pending 时执行失败: 后到列表成功不得把执行错误态变为空态
  const tasksD7 = deferred();
  const stub7b = apiStub({
    tasks: () => tasksD7.promise,
    exec: () => Promise.reject(new Error('net-down')),
  });
  const env7b = initEnv(stub7b);
  await flush();
  ok(textOf(env7b.container).indexOf('执行记录加载失败') !== -1, '7: exec-first failure shows error while list pending');
  tasksD7.resolve([{ id: 't1', name: 'Alpha' }]);
  await flush();
  ok(textOf(env7b.container).indexOf('执行记录加载失败') !== -1, '7: late list success keeps exec error state');
  ok(textOf(env7b.container).indexOf('暂无执行记录') === -1, '7: late list success does not flip to empty state');

  // 旧代次仍 pending 时点表格重试: 强制开新代次, 不共享旧请求; 旧响应被丢弃
  const x7 = deferredEnv();
  x7.ns.init();
  await flush(3);
  x7.execDs[0].reject(new Error('io'));
  await flush();
  const body7 = findByClass(x7.env.container, 'panel-body');
  const retryC = body7 && findButton(body7.children[1], '重试');
  ok(!!retryC, '7: table retry present while old tasks pending');
  if (retryC) retryC.click();
  await flush(3);
  ok(x7.stub.calls.tasks === 2 && x7.stub.calls.exec.length === 2, '7: table retry forces new generation without sharing');
  if (x7.stub.calls.tasks === 2 && x7.tasksDs[1]) {
    x7.tasksDs[0].resolve([{ id: 't1', name: 'StaleOld' }]);
    await flush();
    ok(textOf(x7.env.container).indexOf('StaleOld') === -1, '7: stale tasks response discarded');
    x7.tasksDs[1].resolve([{ id: 't1', name: 'FreshNew' }]);
    x7.execDs[1].resolve([makeExec('e1', 't1', { output: 'gen2-row' })]);
    await flush();
    ok(textOf(x7.env.container).indexOf('gen2-row') !== -1 && textOf(x7.env.container).indexOf('FreshNew') !== -1, '7: new generation renders after forced retry');
  }
}

async function scenario8() {
  // 竞态与代次防护: 两次手动刷新 A/B; 旧成功/旧失败不覆盖 B; A 收尾不清 B inflight;
  // 公开 refresh/activate 共享当前 inflight; 非 active 不发请求; deactivate 后迟到响应不写 DOM
  async function warmup(x) {
    const p = x.ns.init();
    await flush(3);
    x.tasksDs[0].resolve([{ id: 'a', name: 'Warm' }]);
    x.execDs[0].resolve([makeExec('e0', 'a', { output: 'warm-row' })]);
    await p;
    await flush();
  }

  // B 先成功后 A 成功: B 原样保留
  {
    const x = deferredEnv();
    await warmup(x);
    x.ns.refresh(); // A -> idx 1
    await flush(3);
    ok(x.stub.calls.tasks === 2 && x.stub.calls.exec.length === 2, '8: manual refresh A issues exactly 1+1');
    x.ns.deactivate(); // 刷新按钮已移除，并发旧/新代次经 deactivate+init 构造
    x.ns.init(); // B -> idx 2
    await flush(3);
    ok(x.stub.calls.tasks === 3 && x.stub.calls.exec.length === 3, '8: re-init B issues exactly 1+1');
    x.tasksDs[2].resolve([{ id: 'a', name: 'NameGenB' }]);
    x.execDs[2].resolve([makeExec('eb', 'a', { output: 'row-of-b' })]);
    await flush();
    ok(textOf(x.env.container).indexOf('row-of-b') !== -1 && textOf(x.env.container).indexOf('NameGenB') !== -1, '8: B result rendered');
    const snapshotB = allTexts(x.env.container).join('|');
    x.tasksDs[1].resolve([{ id: 'a', name: 'NameGenA' }]);
    x.execDs[1].resolve([makeExec('ea', 'a', { output: 'row-of-a-stale' })]);
    await flush();
    ok(textOf(x.env.container).indexOf('row-of-a-stale') === -1 && textOf(x.env.container).indexOf('NameGenA') === -1,
      '8: stale A success must not overwrite B');
    ok(allTexts(x.env.container).join('|') === snapshotB, '8: B DOM unchanged after stale A success');
  }

  // B 先成功后 A 失败: B 保留且无旧错误提示
  {
    const x = deferredEnv();
    await warmup(x);
    x.ns.refresh();
    await flush(3);
    x.ns.deactivate();
    x.ns.init();
    await flush(3);
    x.tasksDs[2].resolve([{ id: 'a', name: 'NameGenB' }]);
    x.execDs[2].resolve([makeExec('eb', 'a', { output: 'row-of-b' })]);
    await flush();
    const snapshotB = allTexts(x.env.container).join('|');
    x.tasksDs[1].reject(new Error('a-tasks-failed'));
    x.execDs[1].reject(new Error('a-exec-failed'));
    await flush();
    ok(allTexts(x.env.container).join('|') === snapshotB, '8: B DOM unchanged after stale A failure');
    ok(textOf(x.env.container).indexOf('加载失败') === -1, '8: stale A failure raises no error state');
  }

  // 收尾隔离: A 的 allSettled 收尾不得清掉 B 的 inflight; 公开 refresh/activate 共享 B
  {
    const x = deferredEnv();
    await warmup(x);
    x.ns.refresh();
    await flush(3);
    x.ns.deactivate();
    x.ns.init();
    await flush(3);
    const bPromise = x.ns.refresh();
    ok(x.stub.calls.tasks === 3 && x.stub.calls.exec.length === 3, '8: public refresh shares B inflight without new requests');
    x.tasksDs[1].resolve([{ id: 'a', name: 'StaleA' }]);
    x.execDs[1].resolve([makeExec('ea', 'a', { output: 'stale-a-row' })]);
    await flush(); // A 的 allSettled 收尾在此执行
    ok(x.ns.refresh() === bPromise, '8: A cleanup must not clear B inflight (refresh still shared)');
    ok(x.ns.activate() === bPromise, '8: A cleanup must not clear B inflight (activate still shared)');
    ok(x.stub.calls.tasks === 3 && x.stub.calls.exec.length === 3, '8: no extra requests while B inflight');
    x.tasksDs[2].resolve([{ id: 'a', name: 'NameB' }]);
    x.execDs[2].resolve([makeExec('eb', 'a', { output: 'b-final' })]);
    await bPromise;
    await flush();
    ok(textOf(x.env.container).indexOf('b-final') !== -1 && textOf(x.env.container).indexOf('NameB') !== -1,
      '8: B renders after inflight preserved');
  }

  // 非 active: activate 不发请求; 重新 active 后发起新一组
  {
    const x = deferredEnv();
    await warmup(x);
    x.env.container.classList.remove('active');
    x.ns.deactivate();
    await x.ns.activate();
    await flush(3);
    ok(x.stub.calls.tasks === 1 && x.stub.calls.exec.length === 1, '8: activate on inactive page issues no requests');
    x.env.container.classList.add('active');
    const p = x.ns.activate();
    await flush(3);
    ok(x.stub.calls.tasks === 2 && x.stub.calls.exec.length === 2, '8: activate after re-active issues 1+1');
    x.tasksDs[1].resolve([{ id: 'a', name: 'Again' }]);
    x.execDs[1].resolve([makeExec('e1', 'a', { output: 'again-row' })]);
    await p;
  }

  // deactivate: 两端 pending 的迟到成功不写 DOM
  {
    const x = deferredEnv();
    const p = x.ns.init();
    await flush(3);
    x.ns.deactivate();
    const before = allTexts(x.env.container).join('|');
    x.tasksDs[0].resolve([{ id: 'a', name: 'LateName' }]);
    x.execDs[0].resolve([makeExec('e1', 'a', { output: 'after-deactivate' })]);
    await flush();
    ok(textOf(x.env.container).indexOf('after-deactivate') === -1 && textOf(x.env.container).indexOf('LateName') === -1,
      '8: no DOM write from late success after deactivate');
    ok(allTexts(x.env.container).join('|') === before, '8: DOM unchanged after deactivate (success)');
    await p;
  }

  // deactivate: 两端 pending 的迟到失败不写 DOM、不出错误态
  {
    const x = deferredEnv();
    const p = x.ns.init();
    await flush(3);
    x.ns.deactivate();
    const before = allTexts(x.env.container).join('|');
    x.tasksDs[0].reject(new Error('late-tasks'));
    x.execDs[0].reject(new Error('late-exec'));
    await flush();
    ok(allTexts(x.env.container).join('|') === before, '8: DOM unchanged after deactivate (failure)');
    ok(textOf(x.env.container).indexOf('加载失败') === -1, '8: late failure raises no error state');
    await p;
  }

  // deactivate 关闭本页 Modal
  {
    const stub = apiStub({
      tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
      exec: () => Promise.resolve([makeExec('e1', 'a', { output: 'z'.repeat(121) })]),
    });
    const env = initEnv(stub);
    await flush();
    const detailBtn = findButton(env.container, '详情');
    ok(!!detailBtn, '8: 详情 button present in action column');
    detailBtn.click();
    await flush();
    ok(!!findOne(env.body, (n) => n.className === 'modal-backdrop'), '8: modal opened');
    env.ctx.NAGENT.scheduledTasksObservations.deactivate();
    ok(!findOne(env.body, (n) => n.className === 'modal-backdrop'), '8: deactivate closes page modal');
  }

  // 返回本页: 新代次成功后, deactivate 前的旧响应不影响名称与 notice
  {
    const x = deferredEnv();
    x.ns.init();
    await flush(3);
    x.ns.deactivate();
    const p2 = x.ns.init(); // 新代次 idx1
    await flush(3);
    x.tasksDs[1].resolve([{ id: 'a', name: 'NewName' }]);
    x.execDs[1].resolve([makeExec('e1', 'a', { output: 'gen2-row' })]);
    await p2;
    await flush();
    x.tasksDs[0].resolve([{ id: 'a', name: 'OldName' }]);
    x.execDs[0].resolve([makeExec('e0', 'a', { output: 'gen1-stale' })]);
    await flush();
    ok(textOf(x.env.container).indexOf('gen2-row') !== -1 && textOf(x.env.container).indexOf('NewName') !== -1,
      '8: new generation renders after return');
    ok(textOf(x.env.container).indexOf('gen1-stale') === -1 && textOf(x.env.container).indexOf('OldName') === -1,
      '8: stale pre-deactivate responses discarded');
  }
}

async function scenario9() {
  // 刷新按钮: 恰好重新发起一组请求, 参数无 task_id 且 limit=50
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => Promise.resolve([makeExec('e1', 'a')]),
  });
  const env = initEnv(stub);
  await flush();
  ok(stub.calls.tasks === 1 && stub.calls.exec.length === 1, '9: initial load issues 1+1');
  ok(!findButton(env.container, '刷新'), '9: no 刷新 button (removed from header)');
  env.ctx.NAGENT.scheduledTasksObservations.refresh();
  await flush();
  ok(stub.calls.tasks === 2 && stub.calls.exec.length === 2,
    '9: refresh issues exactly one new 1+1 group, tasks=' + stub.calls.tasks + ' exec=' + stub.calls.exec.length);
  const lastArgs = stub.calls.exec[stub.calls.exec.length - 1];
  ok(!!lastArgs && !Object.prototype.hasOwnProperty.call(lastArgs, 'task_id'), '9: refresh exec args have no own task_id');
  ok(!!lastArgs && lastArgs.limit === 50, '9: refresh exec args limit === 50');
}

async function scenario10() {
  // 任务数为零但仍有历史记录
  const stub = apiStub({
    tasks: () => Promise.resolve([]),
    exec: () => Promise.resolve([makeExec('e1', 'gone-1'), makeExec('e2', 'gone-2')]),
  });
  const env = initEnv(stub);
  await flush();
  const rows = tableRows(findTable(env.container));
  ok(rows.length === 2, '10: history rows render with zero tasks, got ' + rows.length);
  ok(textOf(rowCells(rows[0])[1]) === 'gone-1' && textOf(rowCells(rows[1])[1]) === 'gone-2',
    '10: task column falls back to task_id for all rows');
}

async function scenario11() {
  // init 重入: 返回同一 Promise、骨架不清空、不增发请求, 待处理响应仍能渲染
  {
    const x = deferredEnv();
    const p1 = x.ns.init();
    const p2 = x.ns.init();
    ok(p1 === p2, '11: init during pending returns same promise');
    ok(!!findByClass(x.env.container, 'status-panel'), '11: skeleton survives init reentry');
    await flush(3); // api 调用在微任务中发起，flush 后再计数
    ok(x.stub.calls.tasks === 1 && x.stub.calls.exec.length === 1, '11: init reentry issues no extra requests');
    x.tasksDs[0].resolve([{ id: 'a', name: 'Alpha' }]);
    x.execDs[0].resolve([makeExec('e1', 'a', { output: 'reentry-row' })]);
    await p1;
    await flush();
    const row = tableRows(findTable(x.env.container))[0];
    ok(!!row && textOf(rowCells(row)[3]) === 'reentry-row' && textOf(rowCells(row)[1]) === 'Alpha',
      '11: pending responses still render after init reentry');
  }

  // 新代次清名: 上轮名称存在而新列表 pending 时以 id 显示; 失败保持 id + notice; 成功补全
  async function nameClearEnv(listOutcome) {
    const x = deferredEnv();
    const warm = x.ns.init();
    await flush(3);
    x.tasksDs[0].resolve([{ id: 'a', name: 'Old' }]);
    x.execDs[0].resolve([makeExec('e1', 'a', { output: 'row1' })]);
    await warm;
    await flush();
    const rowWarm = tableRows(findTable(x.env.container))[0];
    ok(!!rowWarm && textOf(rowCells(rowWarm)[1]) === 'Old', '11: ' + listOutcome + ': warmup shows previous name Old');
    x.ns.refresh(); // 新代次 idx1
    await flush(3);
    x.execDs[1].resolve([makeExec('e2', 'a', { output: 'row2' })]); // exec 先完成, 列表 pending
    await flush();
    const row2 = tableRows(findTable(x.env.container))[0];
    ok(!!row2 && textOf(rowCells(row2)[3]) === 'row2', '11: ' + listOutcome + ': new generation rows render');
    ok(!!row2 && textOf(rowCells(row2)[1]) === 'a',
      '11: ' + listOutcome + ': previous name cleared while new list pending, got ' + (row2 && textOf(rowCells(row2)[1])));
    if (listOutcome === 'reject') {
      x.tasksDs[1].reject(new Error('list-down'));
      await flush();
      const rowR = tableRows(findTable(x.env.container))[0];
      ok(!!rowR && textOf(rowCells(rowR)[1]) === 'a', '11: reject: id kept after new list failure');
      ok(textOf(x.env.container).indexOf('任务列表加载失败，名称暂以任务 ID 显示') !== -1, '11: reject: notice shown after new list failure');
    } else {
      x.tasksDs[1].resolve([{ id: 'a', name: 'New' }]);
      await flush();
      const rows = tableRows(findTable(x.env.container));
      ok(rows.length === 1 && textOf(rowCells(rows[0])[1]) === 'New', '11: resolve: late list success backfills New');
      ok(rows.length === 1 && textOf(rowCells(rows[0])[3]) === 'row2', '11: resolve: row output unchanged after name backfill');
    }
  }
  await nameClearEnv('reject');
  await nameClearEnv('resolve');
}

async function scenario12() {
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
  ctx.NAGENT.api = {
    listScheduledTasks: () => { const d = deferred(); taskCalls.push(d); return d.promise; },
    listScheduledTaskExecutionsAll: (args) => {
      assertExecArgs(args, '12');
      const d = deferred();
      execCalls.push({ d: d, args: args });
      return d.promise;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(rendererCode, ctx);
  vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), ctx);
  const onTabActivated = ctx.NAGENT.app && ctx.NAGENT.app.onTabActivated;
  ok(typeof onTabActivated === 'function', '12: app.js exposes onTabActivated');
  // 首次进入: init 挂起
  const first = onTabActivated('scheduled-tasks-observations');
  await flush(3);
  ok(taskCalls.length === 1 && execCalls.length === 1, '12: first activation issues 1+1 requests');
  // 离开再进入 (init 尚未完成)
  onTabActivated('chat');
  await flush(3);
  const second = onTabActivated('scheduled-tasks-observations');
  await flush(3);
  ok(taskCalls.length === 2 && execCalls.length === 2,
    '12: re-entry during pending init immediately issues new requests, tasks=' + taskCalls.length + ' exec=' + execCalls.length);
  // 新代次先完成
  taskCalls[1].resolve([{ id: 'a', name: 'A' }]);
  execCalls[1].d.resolve([makeExec('e1', 'a', { output: 'new-gen-rows' })]);
  await flush();
  const container = document.elements['tab-scheduled-tasks-observations'];
  ok(textOf(container).indexOf('new-gen-rows') !== -1, '12: new generation result rendered');
  // 旧请求随后失败也不得覆盖, 且不得触发 app.js 的 renderModuleError
  execCalls[0].d.reject(new Error('old-exec-failed'));
  taskCalls[0].reject(new Error('old-tasks-failed'));
  await flush();
  ok(textOf(container).indexOf('new-gen-rows') !== -1, '12: old failure does not overwrite new result');
  ok(!findOne(container, (n) => n.className === 'module-error'), '12: stale failure must not raise app.js module-error');
  await Promise.allSettled([first, second]);
}

async function scenario13() {
  // 加载态 + 空态 + 徽标 + 空值 + 最近50条提示 + 服务端顺序
  const d = deferred();
  const stub = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => d.promise,
  });
  const env = initEnv(stub);
  await flush(3);
  ok(textOf(env.container).indexOf('加载') !== -1, '13: loading state visible');
  d.resolve([]);
  await flush();
  ok(textOf(env.container).indexOf('暂无执行记录') !== -1, '13: empty array shows 暂无执行记录');

  const statuses = ['running', 'succeeded', 'failed', 'blocked', 'skipped_missed', 'mystery'];
  const execs = statuses.map((s, i) => makeExec('e' + i, 'a', { status: s, output: '', error: null }));
  const stub2 = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => Promise.resolve(execs),
  });
  const env2 = initEnv(stub2);
  await flush();
  const rows = tableRows(findTable(env2.container));
  ok(rows.length === 6, '13: six rows rendered, got ' + rows.length);
  statuses.forEach((s, i) => {
    const cell = rowCells(rows[i])[2];
    ok(textOf(cell) === s, '13: status badge shows raw value ' + s + ', got ' + textOf(cell));
  });
  const mysteryBadge = findOne(rowCells(rows[5])[2], (n) => n.className.indexOf('badge') !== -1);
  ok(!!mysteryBadge, '13: unknown status renders badge');
  ok(mysteryBadge.className === 'badge', '13: unknown status uses neutral badge, got ' + mysteryBadge.className);
  ['running', 'succeeded', 'failed', 'blocked', 'skipped_missed'].forEach((s, i) => {
    const b = findOne(rowCells(rows[i])[2], (n) => n.className.indexOf('badge') !== -1);
    ok(b && b.className.indexOf('badge--') !== -1, '13: known status ' + s + ' has styled badge, got ' + (b && b.className));
  });
  ok(textOf(rowCells(rows[0])[3]) === '-', '13: empty output shows -');
  ok(textOf(rowCells(rows[0])[4]) === 'delivered', '13: delivery column shows status only');
  ok(!!findButton(rowCells(rows[0])[5], '详情'), '13: action column has 详情 button');
  ok(textOf(env2.container).indexOf('最近 50 条') !== -1, '13: tips note 最近 50 条');

  // 服务端顺序保留: created_at 倒序输入但 started_at 升序, 渲染必须同输入序;
  // 从实际 DOM 输出列提取标记比较, 重排会被检出
  const stub3 = apiStub({
    tasks: () => Promise.resolve([{ id: 'a', name: 'A' }]),
    exec: () => Promise.resolve([
      makeExec('x1', 'a', { created_at: '2026-09-16T03:00:00Z', started_at: '2026-09-16T01:00:00Z', output: 'marker-first' }),
      makeExec('x2', 'a', { created_at: '2026-09-16T02:00:00Z', started_at: '2026-09-16T05:00:00Z', output: 'marker-second' }),
    ]),
  });
  const env3 = initEnv(stub3);
  await flush();
  const outs = tableRows(findTable(env3.container)).map((r) => textOf(rowCells(r)[3]));
  ok(JSON.stringify(outs) === JSON.stringify(['marker-first', 'marker-second']),
    '13: server order preserved (created_at desc), got ' + outs.join(','));
}

async function scenario14() {
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
  ok(typeof api.listScheduledTaskExecutionsAll === 'function', '14: helper exported on namespace.api');

  await api.listScheduledTaskExecutionsAll();
  ok(calls[0] === '/chat/scheduled-tasks/executions?limit=50',
    '14: no filter omits task_id, limit=50, got ' + calls[0]);

  await api.listScheduledTaskExecutionsAll({});
  ok(calls[1] === '/chat/scheduled-tasks/executions?limit=50', '14: empty args same as no args');

  const weirdId = 'a&b?c d中';
  await api.listScheduledTaskExecutionsAll({ task_id: weirdId });
  const expected = '/chat/scheduled-tasks/executions?' + new URLSearchParams({ task_id: weirdId, limit: '50' }).toString();
  ok(calls[2] === expected, '14: special chars URLSearchParams-encoded, got ' + calls[2]);

  await api.listScheduledTaskExecutionsAll({ task_id: 'plain', limit: 7 });
  ok(calls[3] === '/chat/scheduled-tasks/executions?task_id=plain&limit=7',
    '14: explicit limit honored, got ' + calls[3]);

  // 模块实际参数形状 { limit: 50 } -> 精确 URL
  await api.listScheduledTaskExecutionsAll({ limit: 50 });
  ok(calls[4] === '/chat/scheduled-tasks/executions?limit=50',
    '14: module arg shape {limit:50} yields exact URL, got ' + calls[4]);

  responder = () => ({ ok: false, status: 404, json: async () => ({ error: { code: 'scheduled_task_not_found' } }) });
  let errMsg = null;
  try { await api.listScheduledTaskExecutionsAll({ task_id: 'gone' }); } catch (e) { errMsg = e.message; }
  ok(errMsg === 'scheduled_task_not_found', '14: non-2xx rejects with business code, got ' + errMsg);
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
  if (failures) { console.error(failures + ' failure(s)'); process.exit(1); }
  console.log('OK scheduled observations frontend harness passed');
  process.exit(0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(1); });
