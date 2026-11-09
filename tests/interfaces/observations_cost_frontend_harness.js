'use strict';
// Behavior harness for observations.js cost display (spec-261110-observations-cost).
// Run with: node tests/interfaces/observations_cost_frontend_harness.js
// Exits 0 on success, 1 on any failure. Loaded by
// test_observations_cost_frontend.py via subprocess; skipped when Node is
// unavailable.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const STATIC_DIR = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static');
const UI_JS = path.join(STATIC_DIR, 'management-ui.js');
const OBSERVATIONS_JS = path.join(STATIC_DIR, 'observations.js');

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
    title: '',
    dataset: {},
    style: {},
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
  // 安全渲染红线：任何 innerHTML 写入立即抛错。
  Object.defineProperty(node, 'innerHTML', {
    get: function () { return ''; },
    set: function () { throw new Error('innerHTML assignment is forbidden (use textContent)'); },
  });
  return node;
}

function makeDocument() {
  const elements = {};
  const body = makeNode('body');
  elements['tab-observations-sessions'] = makeNode('div');
  elements['tab-observations-sessions'].classList.add('active');
  return {
    elements: elements,
    body: body,
    readyState: 'complete',
    createElement: (t) => makeNode(t),
    getElementById: (id) => elements[id] || null,
    addEventListener() {},
  };
}

function freshEnv(stubs) {
  const s = stubs || {};
  const document = makeDocument();
  const namespace = {};
  if (s.api) namespace.api = s.api;
  const ctx = {
    NAGENT: namespace,
    document: document,
    console: console,
    URLSearchParams: URLSearchParams,
    Promise: Promise,
    setTimeout: setTimeout,
    // management-ui.js 在模块作用域创建 MutationObserver 并 observe(document.body)，
    // vm 上下文必须提供 no-op 桩，否则脚本会在 observer 初始化处抛错，freshEnv 无法完成。
    MutationObserver: function MutationObserverStub() {
      return { observe() {}, disconnect() {}, takeRecords() { return []; } };
    },
  };
  if (s.location) ctx.location = s.location;
  if (s.history) ctx.history = s.history;
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(UI_JS, 'utf8'), ctx);
  vm.runInContext(fs.readFileSync(OBSERVATIONS_JS, 'utf8'), ctx);
  return { ctx: ctx, document: document, container: document.elements['tab-observations-sessions'] };
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
function textOf(node) { return node ? allTexts(node).join('') : ''; }
function findAll(node, pred, out) {
  out = out || [];
  if (node && pred(node)) out.push(node);
  ((node && node.children) || []).forEach((c) => findAll(c, pred, out));
  return out;
}
function findOne(node, pred) { return findAll(node, pred)[0] || null; }

// 找 label 匹配的 stat-card（结构：div.stat-card > div.stat-card__value + div.stat-card__label）
function findStatCard(rootNode, label) {
  return findOne(rootNode, (n) => n.className === 'stat-card'
    && findOne(n, (c) => c.className === 'stat-card__label' && textOf(c) === label) != null);
}
function statCardValue(card) {
  const v = findOne(card, (c) => c.className === 'stat-card__value');
  return v ? textOf(v) : null;
}

// ------------------------------------------------------------------ tests ---
const tests = [];
function t(name, fn) { tests.push([name, fn]); }

// T1: 概览/总览费用卡片
function runOverviewAssertions(env, stats) {
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.overviewCards(env.container, stats);
}

t('overview cards: 费用 card exists after 归一化 Token, before API 调用数', () => {
  const env = freshEnv();
  const stats = {
    session_count: 2, input_tokens: 1000, output_tokens: 500,
    cache_read_tokens: 100, cache_write_tokens: 20,
    normalized_tokens: 1700, api_call_count: 3,
    estimated_cost_usd: '0.0123456',
  };
  runOverviewAssertions(env, stats);
  const labels = findAll(env.container, (n) => n.className === 'stat-card__label').map(textOf);
  ok(labels.indexOf('费用') >= 0, 'overview: 费用 card missing');
  ok(labels.filter((x) => x === '费用').length === 1 && labels.indexOf('费用') === labels.indexOf('归一化 Token') + 1 && labels.indexOf('API 调用数') === labels.indexOf('费用') + 1, 'cost card must appear exactly once and be adjacent');
  ok(labels.indexOf('归一化 Token') < labels.indexOf('费用'), 'overview: 费用 must follow 归一化 Token');
  ok(labels.indexOf('费用') < labels.indexOf('API 调用数'), 'overview: 费用 must precede API 调用数');
  const card = findStatCard(env.container, '费用');
  ok(statCardValue(card) === '$0.012346', 'overview: cost value should be $0.012346, got ' + statCardValue(card));
  ok(card && card.title === '仅累计已知估算费用，未知费用未计入；零值不代表全部调用免费', 'overview: cost card title missing');
});

// 金额矩阵同时通过 overviewCards、statsBar 与 sessionsTable 执行，期望一致。
// V8 浮点实测：(0.0000005).toFixed(6) === '0.000000'，期望值按实现实测校准。
const COST_MATRIX_CASES = [
  ['0', '$0.000000'], ['0.1', '$0.100000'], [0, '$0.000000'], [0.25, '$0.250000'],
  ['1e-7', '$0.000000'], ['0.0000005', '$0.000000'],
];
const COST_UNKNOWN_CASES = [undefined, null, '', '  ', 'abc', '-5', true, false, {}, [], NaN, Infinity, -Infinity, -0.5, 'NaN', 'Infinity', '0x10', '0b10', '1e309'];

// runCase(env, input) 返回实际展示字符串；默认按 stat-card（overviewCards/statsBar）
// 提取，sessionsTable 等表格渲染器传入自定义提取函数（取费用列单元格文本）。
function assertCostMatrix(rendererName, runCase) {
  const run = runCase || ((env, input) => {
    env.container.replaceChildren();
    env.ctx.NAGENT.observations.renderers[rendererName](env.container, { estimated_cost_usd: input });
    const card = findStatCard(env.container, '费用');
    return statCardValue(card);
  });
  COST_MATRIX_CASES.forEach(([input, want]) => {
    const env = freshEnv();
    const got = run(env, input);
    ok(got === want, rendererName + ' amount ' + JSON.stringify(input) + ': want ' + want + ' got ' + got);
  });
  COST_UNKNOWN_CASES.forEach((input) => {
    const env = freshEnv();
    const got = run(env, input);
    ok(got === '未知', rendererName + ' unknown input ' + JSON.stringify(String(input)) + ': want 未知 got ' + got);
  });
}

t('overview cards: amount matrix via estimated_cost_usd', () => {
  assertCostMatrix('overviewCards');
});

t('stats bar: amount matrix via estimated_cost_usd matches overview expectations', () => {
  assertCostMatrix('statsBar');
});

t('stats bar: 费用 card after 归一化 Token, before API 调用数, with title', () => {
  const env = freshEnv();
  const stats = {
    input_tokens: 800, output_tokens: 400, cache_read_tokens: 60,
    cache_write_tokens: 10, normalized_tokens: 1330, api_call_count: 2,
    estimated_cost_usd: '2.5', cost_status: 'unknown',
  };
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.statsBar(env.container, stats);
  const labels = findAll(env.container, (n) => n.className === 'stat-card__label').map(textOf);
  ok(labels.filter((x) => x === '费用').length === 1 && labels.indexOf('费用') === labels.indexOf('归一化 Token') + 1 && labels.indexOf('API 调用数') === labels.indexOf('费用') + 1, 'cost card must appear exactly once and be adjacent');
  ok(labels.indexOf('归一化 Token') < labels.indexOf('费用'), 'stats bar: 费用 must follow 归一化 Token');
  ok(labels.indexOf('费用') < labels.indexOf('API 调用数'), 'stats bar: 费用 must precede API 调用数');
  const card = findStatCard(env.container, '费用');
  ok(statCardValue(card) === '$2.500000', 'stats bar: want $2.500000 got ' + statCardValue(card));
  ok(card && card.title === '仅累计已知估算费用，未知费用未计入；零值不代表全部调用免费', 'stats bar: title missing');
  ok(labels.indexOf('输入 Token') >= 0 && labels.indexOf('API 调用数') >= 0, 'stats bar: existing cards must remain');
});

// T1 补充: task-aggregator 输入形状（无 estimated_cost_usd 字段）
t('overview cards: task-aggregator stats without estimated_cost_usd shows 未知', () => {
  const env = freshEnv();
  runOverviewAssertions(env, {
    session_count: 4, input_tokens: 100, output_tokens: 50,
    cache_read_tokens: 10, cache_write_tokens: 5,
    normalized_tokens: 165, api_call_count: 4,
  });
  const card = findStatCard(env.container, '费用');
  ok(!!card, 'task-aggregator: 费用 card present');
  ok(statCardValue(card) === '未知', 'task-aggregator: missing estimated_cost_usd shows 未知, got ' + statCardValue(card));
  const inputCard = findStatCard(env.container, '输入 Token');
  ok(!!inputCard && statCardValue(inputCard) === '100', 'task-aggregator: existing Token cards render normally');
});

// T1 补充: 聚合来源完整性 —— 概览/总览费用必须来自聚合接口，不得由明细求和
function makeUsageStub(impl) {
  const calls = {
    getOverview: 0,
    listSessions: [],
    getStats: [],
    getRecords: [],
    getCompressions: 0,
    getBreakdown: 0,
  };
  const usage = {
    getOverview: () => { calls.getOverview++; return impl.getOverview(); },
    listSessions: (page, pageSize) => { calls.listSessions.push([page, pageSize]); return impl.listSessions(page, pageSize); },
    getStats: (id) => { calls.getStats.push(id); return impl.getStats(id); },
    getRecords: (id, limit) => { calls.getRecords.push([id, limit]); return impl.getRecords(id, limit); },
    getCompressions: (id) => { calls.getCompressions++; return impl.getCompressions(id); },
    getBreakdown: (id) => { calls.getBreakdown++; return impl.getBreakdown(id); },
  };
  return { calls: calls, api: { usage: usage } };
}

function makeRecord(id) {
  return {
    id: id, model: 'gpt-test', trigger_type: 'chat',
    input_tokens: 10, output_tokens: 5, cache_read_tokens: 0, cache_write_tokens: 0,
    normalized_tokens: 15, latency_ms: 100, created_at: '2026-11-10T00:00:00Z',
    estimated_cost_usd: '0.1', cost_status: 'estimated',
  };
}

t('aggregate source: overview cost comes from getOverview, not session list', async () => {
  const stub = makeUsageStub({
    getOverview: () => Promise.resolve({ estimated_cost_usd: '9.25', session_count: 3, input_tokens: 1000, output_tokens: 500, cache_read_tokens: 100, cache_write_tokens: 20, normalized_tokens: 1700, api_call_count: 3 }),
    listSessions: (page, pageSize) => Promise.resolve({ items: [{ session_id: 'source-test', estimated_cost_usd: '0.1', normalized_tokens: 1, api_call_count: 1, turn_count: 1, title: 't', source: 'dashboard' }], page: page, page_size: 10, total: 1 }),
    getStats: () => Promise.resolve({}),
    getRecords: () => Promise.resolve([]),
    getCompressions: () => Promise.resolve([]),
    getBreakdown: () => Promise.resolve({}),
  });
  const env = freshEnv({
    api: stub.api,
    location: { pathname: '/observations/sessions', search: '' },
    history: { pushState() {} },
  });
  await env.ctx.NAGENT.observations.refresh();
  await flush();
  const card = findStatCard(env.container, '费用');
  ok(statCardValue(card) === '$9.250000', 'aggregate: overview cost $9.250000, got ' + statCardValue(card));
  ok(stub.calls.getOverview === 1, 'aggregate: getOverview called once, got ' + stub.calls.getOverview);
});

t('aggregate source: detail cost comes from getStats, not sum of records', async () => {
  const stub = makeUsageStub({
    getOverview: () => Promise.resolve({}),
    listSessions: () => Promise.resolve({ items: [], page: 1, page_size: 10, total: 0 }),
    getStats: () => Promise.resolve({ estimated_cost_usd: '8.5', cost_status: 'unknown', input_tokens: 800, output_tokens: 400, cache_read_tokens: 60, cache_write_tokens: 10, normalized_tokens: 1330, api_call_count: 2 }),
    getRecords: () => Promise.resolve([makeRecord(1), makeRecord(2)]),
    getCompressions: () => Promise.resolve([]),
    getBreakdown: () => Promise.resolve({}),
  });
  const env = freshEnv({
    api: stub.api,
    location: { pathname: '/observations/sessions/source-test', search: '' },
    history: { pushState() {} },
  });
  await env.ctx.NAGENT.observations.refresh();
  await flush();
  const card = findStatCard(env.container, '费用');
  ok(statCardValue(card) === '$8.500000', 'aggregate: detail cost $8.500000, got ' + statCardValue(card));
  ok(stub.calls.getRecords.length === 1 && stub.calls.getRecords[0][0] === 'source-test' && stub.calls.getRecords[0][1] === 50,
    'aggregate: getRecords called with (source-test, 50), got ' + JSON.stringify(stub.calls.getRecords));
});

// T1 补充: 错误路径 —— 聚合接口失败时保持既有错误态，不出费用成功卡片
t('error path: getOverview rejection shows error state without cost card', async () => {
  const stub = makeUsageStub({
    getOverview: () => Promise.reject(new Error('down')),
    listSessions: () => Promise.resolve({ items: [], page: 1, page_size: 10, total: 0 }),
    getStats: () => Promise.resolve({}),
    getRecords: () => Promise.resolve([]),
    getCompressions: () => Promise.resolve([]),
    getBreakdown: () => Promise.resolve({}),
  });
  const env = freshEnv({
    api: stub.api,
    location: { pathname: '/observations/sessions', search: '' },
    history: { pushState() {} },
  });
  await env.ctx.NAGENT.observations.refresh();
  await flush();
  ok(textOf(env.container).indexOf('加载观测数据失败') !== -1, 'error: overview error text shown, got ' + textOf(env.container).slice(0, 120));
  ok(!findStatCard(env.container, '费用'), 'error: no 费用 card on overview failure');
});

t('error path: getStats rejection shows detail error state without cost card', async () => {
  const stub = makeUsageStub({
    getOverview: () => Promise.resolve({}),
    listSessions: () => Promise.resolve({ items: [], page: 1, page_size: 10, total: 0 }),
    getStats: () => Promise.reject(new Error('down')),
    getRecords: () => Promise.resolve([]),
    getCompressions: () => Promise.resolve([]),
    getBreakdown: () => Promise.resolve({}),
  });
  const env = freshEnv({
    api: stub.api,
    location: { pathname: '/observations/sessions/source-test', search: '' },
    history: { pushState() {} },
  });
  await env.ctx.NAGENT.observations.refresh();
  await flush();
  ok(textOf(env.container).indexOf('加载会话观测数据失败') !== -1, 'error: detail error text shown, got ' + textOf(env.container).slice(0, 120));
  ok(!findStatCard(env.container, '费用'), 'error: no 费用 card on detail failure');
});

// T2: 会话列表费用列
function headerTexts(table) {
  const ths = findAll(table, (n) => n.tag === 'th');
  return ths.map((th) => ({ text: textOf(th), cls: th.className, title: th.title }));
}

t('sessions table: 费用 column after 归一化 Token, numeric header with title', () => {
  const env = freshEnv();
  const items = [{
    session_id: 's-1', title: '会话甲', source: 'dashboard',
    turn_count: 2, api_call_count: 5, normalized_tokens: 12345,
    estimated_cost_usd: '0.4242424',
  }, {
    session_id: 's-2', title: '会话乙', source: 'cli',
    turn_count: 1, api_call_count: 1, normalized_tokens: 999,
    estimated_cost_usd: 'bad-amount',
  }, {
    // 覆盖 spec 验收"末次 unknown 且累计金额大于零"：会话级不按 cost_status 门控
    session_id: 's-3', title: '会话丙', source: 'api',
    turn_count: 3, api_call_count: 7, normalized_tokens: 8000,
    estimated_cost_usd: '1.500000', cost_status: 'unknown',
  }];
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.sessionsTable(env.container, items, 1, 10, 3, {});
  const table = findOne(env.container, (n) => n.tag === 'table');
  const headers = headerTexts(table);
  const texts = headers.map((h) => h.text);
  ok(texts.join('|') === '标题|来源|ID|对话轮数|API 调用|归一化 Token|费用|操作',
    'sessions table header order, got: ' + texts.join('|'));
  const costHeader = headers[texts.indexOf('费用')];
  ok(costHeader && costHeader.cls === 'document-table__numeric', 'sessions table: 费用 header must be numeric');
  ok(costHeader && costHeader.title === '仅累计已知估算费用，未知费用未计入；零值不代表全部调用免费', 'sessions table: cost header title missing');
  const rows = findAll(table, (n) => n.tag === 'tr').filter((r) => findOne(r, (c) => c.tag === 'td') != null);
  ok(rows.length === 3, 'sessions table: want 3 rows, got ' + rows.length);
  rows.forEach((row, i) => {
    const tds = findAll(row, (n) => n.tag === 'td');
    ok(tds.length === texts.length, 'row/header column counts match');
    const costTd = tds[texts.indexOf('费用')];
    ok(costTd && costTd.className === 'document-table__numeric', 'sessions table row ' + i + ': cost cell must be numeric');
    const want = ['$0.424242', '未知', '$1.500000'][i];
    ok(textOf(costTd) === want, 'sessions table row ' + i + ': want ' + want + ' got ' + textOf(costTd));
    const normTd = tds[texts.indexOf('归一化 Token')];
    ok(normTd.className === 'document-table__numeric', 'sessions table: 归一化 Token cell must stay numeric');
  });
});

t('sessions table: rerender keeps single cost column and pagination intact', () => {
  const env = freshEnv();
  const items = [];
  for (let i = 0; i < 12; i++) {
    items.push({
      session_id: 's-' + i, title: 't' + i, source: 'dashboard',
      turn_count: 1, api_call_count: 1, normalized_tokens: 100 + i,
      estimated_cost_usd: '0.0' + (i + 1),
    });
  }
  let lastPage = 0;
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.sessionsTable(env.container, items.slice(0, 10), 1, 10, 12, { onPage: (p) => { lastPage = p; } });
  let table = findOne(env.container, (n) => n.tag === 'table');
  ok(headerTexts(table).filter((h) => h.text === '费用').length === 1, 'sessions table: exactly one 费用 header');
  const nextBtn = findOne(env.container, (n) => n.tag === 'button' && textOf(n) === '>');
  ok(nextBtn != null, 'sessions table: next page button present');
  nextBtn.click();
  ok(lastPage === 2, 'sessions: next callback receives page 2');
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.sessionsTable(env.container, items.slice(10), 2, 10, 12, { onPage: (p) => { lastPage = p; } });
  table = findOne(env.container, (n) => n.tag === 'table');
  ok(headerTexts(table).filter((h) => h.text === '费用').length === 1, 'sessions table: rerender page 2 keeps one 费用 header');
  const pageRows = findAll(table, (n) => n.tag === 'tr' && n.dataset.sessionId);
  ok(pageRows.length === 2 && pageRows[0].dataset.sessionId === 's-10', 'sessions: page 2 receives only its server page');
  ok(findAll(table, (n) => n.tag === 'td' && textOf(n) === '$0.011000').length === 1, 'sessions table: page 2 exact cost cell');
});

t('sessions table: amount matrix via estimated_cost_usd matches overview expectations', () => {
  assertCostMatrix('sessionsTable', (env, input) => {
    env.container.replaceChildren();
    env.ctx.NAGENT.observations.renderers.sessionsTable(env.container, [{
      session_id: 'mx', title: 'm', source: 'dashboard',
      turn_count: 1, api_call_count: 1, normalized_tokens: 100,
      estimated_cost_usd: input,
    }], 1, 10, 1, {});
    const table = findOne(env.container, (n) => n.tag === 'table');
    if (!table) return null;
    const texts = headerTexts(table).map((h) => h.text);
    const idx = texts.indexOf('费用');
    if (idx < 0) return null;
    const tds = findAll(table, (n) => n.tag === 'td');
    return tds[idx] ? textOf(tds[idx]) : null;
  });
});

t('empty states, session detail and back contracts', () => {
  const env = freshEnv();
  const R = env.ctx.NAGENT.observations.renderers;
  R.sessionsTable(env.container, [], 1, 10, 0, {});
  ok(textOf(env.container).includes('暂无会话'), 'sessions empty state');
  ok(!findOne(env.container, (n) => n.tag === 'table'), 'empty sessions have no table');
  env.container.replaceChildren();
  R.records(env.container, []);
  ok(textOf(env.container).includes('暂无 API 调用记录'), 'records empty state');
  ok(!findOne(env.container, (n) => n.tag === 'table'), 'empty records have no table');
  env.container.replaceChildren();
  let selected;
  R.sessionsTable(env.container, [{session_id: 'selected-session', estimated_cost_usd: '0.3'}], 1, 10, 1,
    {onDetail: (id) => { selected = id; }});
  findOne(env.container, (n) => n.tag === 'button' && textOf(n) === '详情').click();
  ok(selected === 'selected-session', 'session detail retains session identity');
  env.container.replaceChildren();
  let returned = 0;
  R.detailHeader(env.container, 'selected-session', {backHref: '/observations/tasks', onBack: () => { returned++; }});
  const back = findOne(env.container, (n) => n.tag === 'a');
  ok(back._attrs.href === '/observations/tasks', 'custom back href preserved');
  back.click();
  ok(returned === 1, 'custom back callback preserved');
});

// T3: API 调用历史费用列
// 与既有 makeRecord(id) 不同名，避免函数声明遮蔽改变 T1 聚合测试的记录形状。
function makeCostRecord(i, costStatus, amount) {
  return {
    id: i, model: 'gpt-test', trigger_type: 'dashboard',
    input_tokens: 1000 + i, output_tokens: 500, cache_read_tokens: 10,
    cache_write_tokens: 2, normalized_tokens: 1600,
    estimated_cost_usd: amount, cost_status: costStatus,
    latency_ms: 123, created_at: '2026-11-10T00:00:0' + (i % 10) + 'Z',
  };
}

function recordsCostCell(env, record) {
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.records(env.container, [record]);
  const table = findOne(env.container, (n) => n.tag === 'table');
  if (!table) return null;
  const texts = headerTexts(table).map((h) => h.text);
  const idx = texts.indexOf('费用');
  if (idx < 0) return null;
  const tds = findAll(table, (n) => n.tag === 'td');
  return tds[idx] ? textOf(tds[idx]) : null;
}

t('records table: 费用 column after 归一化, gated by cost_status', () => {
  const env = freshEnv();
  const records = [
    makeCostRecord(1, 'estimated', '0.111111'),
    makeCostRecord(2, 'unknown', '0'),
    makeCostRecord(3, 'unknown', '9.999999'),
    makeCostRecord(4, undefined, '7.777777'),
    makeCostRecord(5, 'estimated', 'not-a-number'),
  ];
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.records(env.container, records);
  const table = findOne(env.container, (n) => n.tag === 'table');
  const headers = headerTexts(table);
  const texts = headers.map((h) => h.text);
  ok(texts.join('|') === '时间|模型|调起类型|输入|输出|缓存读|缓存写|命中率|归一化|费用|延迟(ms)|操作',
    'records header order, got: ' + texts.join('|'));
  ok(headers[texts.indexOf('费用')] && headers[texts.indexOf('费用')].cls === 'document-table__numeric', 'records: 费用 header must be numeric');
  const rows = findAll(table, (n) => n.tag === 'tr').filter((r) => findOne(r, (c) => c.tag === 'td') != null);
  ok(rows.length === 5, 'records: want 5 rows, got ' + rows.length);
  const want = ['$0.111111', '未知', '未知', '未知', '未知'];
  rows.forEach((row, i) => {
    const tds = findAll(row, (n) => n.tag === 'td');
    ok(tds.length === texts.length, 'row/header column counts match');
    const costTd = tds[texts.indexOf('费用')];
    ok(costTd && costTd.className === 'document-table__numeric', 'records row ' + i + ': cost cell must be numeric');
    ok(textOf(costTd) === want[i], 'records row ' + i + ': want ' + want[i] + ' got ' + textOf(costTd));
    const normTd = tds[texts.indexOf('归一化')];
    ok(normTd.className === 'document-table__numeric',
      'records row ' + i + ': 归一化 cell must stay numeric');
  });
});

t('records table: pagination keeps cost column on later pages', () => {
  const env = freshEnv();
  const records = [];
  for (let i = 0; i < 25; i++) records.push(makeCostRecord(i + 1, 'estimated', ((i + 1) / 100).toFixed(2)));
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.records(env.container, records);
  let table = findOne(env.container, (n) => n.tag === 'table');
  ok(headerTexts(table).filter((h) => h.text === '费用').length === 1, 'records: exactly one 费用 header on page 1');
  const nextBtn = findOne(env.container, (n) => n.tag === 'button' && textOf(n) === '>');
  ok(nextBtn != null, 'records: next page button present');
  nextBtn.click();
  table = findOne(env.container, (n) => n.tag === 'table');
  ok(headerTexts(table).filter((h) => h.text === '费用').length === 1, 'records: 费用 header present on page 2');
  const rows = findAll(table, (n) => n.tag === 'tr' && n.children.some((c) => c.tag === 'td'));
  ok(rows.length === 10, 'records: page 2 has ten rows');
  rows.forEach((row, i) => {
    ok(row.children.length === 12, 'records: exact row column count');
    ok(textOf(row.children[9]) === '$' + ((i + 11) / 100).toFixed(6), 'records: correct page 2 cost');
    ok(row.children[9].className === 'document-table__numeric', 'records: page 2 numeric cost');
  });
});

// T3 补充: 金额矩阵经 records 渲染（每条记录 cost_status=estimated，单条数组逐例执行）
t('records table: amount matrix via estimated_cost_usd with estimated status', () => {
  assertCostMatrix('records', (env, input) => recordsCostCell(env, makeCostRecord(1, 'estimated', input)));
});

// T3 补充: 门控边界 —— estimated 状态金额 0 显示 $0.000000；
// 非 estimated 状态即使携带非零金额（占位值）也只能显示 未知。
t('records table: cost_status gating edge cases', () => {
  const cases = [
    ['estimated', 0, '$0.000000'],
    ['estimated', '0', '$0.000000'],
    [null, '9.999999', '未知'],
    ['', '9.999999', '未知'],
    ['pending', '9.999999', '未知'],
    ['unknown', '0.500000', '未知'],
  ];
  cases.forEach(([status, amount, want]) => {
    const env = freshEnv();
    const got = recordsCostCell(env, makeCostRecord(1, status, amount));
    ok(got === want, 'records gating status=' + JSON.stringify(status) + ' amount=' + JSON.stringify(amount) + ': want ' + want + ' got ' + got);
  });
});

// T3 补充: 详情弹窗 —— 记录 ID 与估算成本进入弹窗，关闭后移除。
// makeCostRecord 不携带 request_messages/response_message/tools/buildJsonSection 的
// jsonValue 为 null，不触发复制按钮的 innerHTML 分支，安全渲染红线不被触碰。
t('records table: detail modal shows record id and estimated cost', () => {
  const env = freshEnv();
  const records = [];
  for (let i = 0; i < 25; i++) records.push(makeCostRecord(i + 1, 'estimated', ((i + 1) / 100).toFixed(2)));
  env.container.replaceChildren();
  env.ctx.NAGENT.observations.renderers.records(env.container, records);
  const nextBtn = findOne(env.container, (n) => n.tag === 'button' && textOf(n) === '>');
  nextBtn.click();
  const table = findOne(env.container, (n) => n.tag === 'table');
  const rows = findAll(table, (n) => n.tag === 'tr' && n.children.some((c) => c.tag === 'td'));
  const detailBtn = findOne(rows[0], (n) => n.tag === 'button' && textOf(n) === '详情');
  detailBtn.click();
  const backdrops = env.document.body.children.filter((c) => c.className === 'modal-backdrop');
  ok(backdrops.length === 1, 'modal: exactly one backdrop in document.body, got ' + backdrops.length);
  const modalText = textOf(backdrops[0]);
  ok(modalText.indexOf('记录ID: #11') !== -1, 'modal: contains 记录ID: #11, got: ' + modalText.slice(0, 200));
  ok(modalText.indexOf('估算成本: $0.110000') !== -1, 'modal: contains 估算成本: $0.110000');
  const closeBtn = findOne(backdrops[0], (n) => n.tag === 'button' && n.className === 'modal-close');
  ok(closeBtn != null, 'modal: close button present');
  closeBtn.click();
  ok(env.document.body.children.filter((c) => c.className === 'modal-backdrop').length === 0, 'modal: close removes backdrop from document.body');
});

// ------------------------------------------------------------------- main ---
async function main() {
  for (const [name, fn] of tests) {
    try { await fn(); } catch (e) { failures++; console.error('FAIL: ' + name + ' threw: ' + (e && e.message)); }
  }
  if (failures) {
    console.error('observations cost frontend harness: ' + failures + ' failure(s)');
    process.exit(1);
  }
  console.log('OK observations cost frontend harness passed');
}

main().catch((e) => { console.error(e); process.exit(1); });
