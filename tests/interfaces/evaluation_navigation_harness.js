const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.handlers = {}; this.classList = { contains: () => true }; }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(n => n.textContent).join(''); }
  appendChild(node) { this.children.push(node); node.parentElement = this; return node; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  querySelector() { return find(this, node => node.type === 'submit'); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(n => n !== this); }
  setAttribute() {}
}
function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) { const result = find(child, predicate); if (result) return result; }
}
const root = new Element();
const body = new Element();
const calls = [];
let enabled = false;
let pendingStatus = null;
const api = {};
for (const method of ['getConfig', 'overview', 'listExperiments', 'listResults', 'listReviewQueue', 'listDatasets']) {
  api[method] = async () => { calls.push(method); return { config: {}, items: [] }; };
}
api.status = async () => { calls.push('status'); return pendingStatus ? pendingStatus : { enabled }; };
const context = { Node: Element, document: { getElementById: () => root, createElement: tag => new Element(tag), body }, NAGENT: { api: { evaluations: api } } };
context.window = context;
vm.runInNewContext(fs.readFileSync('app/interfaces/http/static/management-navigation.js', 'utf8'), context);
// 六菜单导航：顺序 = 概览、数据集、评估器、实验、结果、人工标注；
// 每个 entry 独立 routeConfig（sidebarTab/topnavParent/scope 各自归属，
// renderTab:'evaluations' 共享）；各页顶导 scope 单元素。
const evaluationViews = [
  ['overview', '/evaluations', '概览'],
  ['datasets', '/evaluations/datasets', '数据集'],
  ['evaluators', '/evaluations/evaluators', '评估器'],
  ['experiments', '/evaluations/experiments', '实验评估'],
  ['annotations', '/evaluations/annotations', '人工标注'],
];
const nav = context.NAGENT.navigation;
const evaluationsParent = nav.tabConfig.find(c => c.tab === 'evaluations');
assert.ok(evaluationsParent && evaluationsParent.parent === true, 'evaluations must stay an independent parent');
// 跨 vm realm 数组原型不同，assert.deepEqual 直接比较必失败；
// 先 JSON 归一成宿主 realm 普通数组再比较，勿删归一步骤。
// results 行移出循环数组：线上评估子域顶导拆为三项，由下方专属断言块覆盖。
assert.deepEqual(JSON.parse(JSON.stringify(evaluationsParent.children)), [
  'evaluations-overview', 'evaluations-datasets', 'evaluations-evaluators',
  'evaluations-experiments', 'evaluations-results', 'evaluations-annotations',
], 'six-menu children order');
assert.ok(!evaluationsParent.children.includes('evaluations-review'), 'review must not be a sidebar child');
for (const [view, path, label] of evaluationViews) {
  const route = nav.resolveRoute(path);
  assert.equal(route.activeTab, 'evaluations-' + view);
  assert.equal(route.sidebarTab, 'evaluations-' + view);
  assert.equal(route.renderTab, 'evaluations');
  assert.equal(route.currentSubdomain, 'evaluations-' + view);
  assert.equal(route.route.scope, view);
  const items = nav.topnavConfig[route.currentSubdomain];
  if (view === 'evaluators') {
    // 评估器左导子域顶导为三入口：能力目录 + 评估器 + 模型（顺序固定）
    assert.equal(items.length, 3);
    assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.tab))),
      ['evaluations-capabilities', 'evaluations-evaluators', 'evaluations-judge-model']);
    assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.path))),
      ['/evaluations/capabilities', '/evaluations/evaluators', '/evaluations/judge-model']);
    assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.label))),
      ['能力目录', '评估器', '模型']);
    assert.ok(items.every(i => i.concern === 'evaluation'
      && i.topnavParent === 'evaluations-evaluators'));
    const current = items.find(i => i.tab === route.activeTab);
    assert.equal(current.path, path);
    assert.equal(current.label, label);
    assert.equal(current.scope, 'evaluators');
  } else if (view === 'experiments') {
    // 实验左导子域顶导拆为双入口：执行任务 + 实验结果（顺序固定）
    assert.equal(items.length, 3);
    assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.tab))),
      ['evaluations-executions', 'evaluations-experiments', 'evaluations-experiment-results']);
    assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.path))),
      ['/evaluations/executions', '/evaluations/experiments', '/evaluations/experiment-results']);
    assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.label))),
      ['执行任务', '实验汇总', '评分明细']);
    assert.ok(items.every(i => i.concern === 'evaluation'
      && i.topnavParent === 'evaluations-experiments'));
    const current = items.find(i => i.tab === route.activeTab);
    assert.equal(current.path, path);
    assert.equal(current.label, '实验汇总', '顶导标签与左导"实验"区分，实验结果页顶导称实验结果');
    assert.equal(current.scope, 'experiments');
  } else {
    assert.equal(items.length, 1);
    assert.equal(items[0].tab, route.activeTab);
    assert.equal(items[0].path, path);
    assert.equal(items[0].label, label);
    assert.equal(items[0].concern, 'evaluation');
    assert.equal(items[0].scope, view);
  }
}
// 线上评估子域顶导三项：会话评分 -> 运行评分 -> 评分明细，归属 evaluations-results 子域
{
  const resultsTopnav = nav.topnavConfig['evaluations-results'];
  assert.deepEqual(JSON.parse(JSON.stringify(resultsTopnav.map(i => [i.tab, i.path, i.label]))), [
    ['evaluations-session-scores', '/evaluations/session-scores', '会话评分'],
    ['evaluations-run-scores', '/evaluations/run-scores', '运行评分'],
    ['evaluations-results', '/evaluations/results', '评分明细'],
  ], '线上评估顶导三项顺序固定');
  assert.ok(resultsTopnav.every(i => i.topnavParent === 'evaluations-results' && i.concern === 'evaluation'));
  // 路由解析：两个新路径独立 scope，共享 sidebarTab/topnavParent/renderTab
  for (const [path, scope] of [
    ['/evaluations/session-scores', 'session-scores'],
    ['/evaluations/run-scores', 'run-scores'],
  ]) {
    const route = nav.resolveRoute(path);
    assert.ok(route && route.route, 'routeConfig 缺少 ' + path);
    assert.equal(route.activeTab, path === '/evaluations/session-scores'
      ? 'evaluations-session-scores' : 'evaluations-run-scores');
    assert.equal(route.sidebarTab, 'evaluations-results');
    assert.equal(route.currentSubdomain, 'evaluations-results');
    assert.equal(route.route.topnavParent, 'evaluations-results');
    assert.equal(route.renderTab, 'evaluations');
    assert.equal(route.route.scope, scope);
  }
  // 顶导高亮互不串扰：同子域内每个 tab 仅出现一次
  const seenTabs = resultsTopnav.map(i => i.tab);
  assert.equal(new Set(seenTabs).size, seenTabs.length);
  // tabConfig 两个新 tab 有元数据、不在评估父 children（左导不变）
  for (const [tab, label] of [
    ['evaluations-session-scores', '会话评分'],
    ['evaluations-run-scores', '运行评分'],
  ]) {
    const item = nav.tabConfig.find(i => i.tab === tab);
    assert.ok(item, 'tabConfig 缺少 ' + tab);
    assert.equal(item.parentTab, 'evaluations');
    assert.equal(item.label, label);
    assert.ok(!evaluationsParent.children.includes(tab),
      tab + ' 是顶导入口，不得成为左导 children');
  }
  // 左导落地规范（无例外）：顶导首项落地——线上评估落会话评分；
  // 相邻子域落点规则不受影响
  assert.equal(nav.sidebarLandingPath('evaluations-results'), '/evaluations/session-scores');
  assert.equal(nav.sidebarLandingPath('evaluations-experiments'), '/evaluations/executions');
  // 相邻子域不串扰：实验子域顶导与 results 子域顶导互不含对方 tab
  assert.ok(!resultsTopnav.some(i => i.tab === 'evaluations-experiment-results'));
  assert.ok(!nav.topnavConfig['evaluations-experiments']
    .some(i => i.tab === 'evaluations-session-scores'));
  // 左导评估 children 数量不变（6 项，无新 children）
  assert.equal(evaluationsParent.children.length, 6);
}
// 能力目录：独立 routeConfig entry，与评估器共享左导/顶导子域，不进左导 children
{
  const capTab = nav.tabConfig.find(c => c.tab === 'evaluations-capabilities');
  assert.ok(capTab && capTab.path === '/evaluations/capabilities' && capTab.label === '能力目录');
  assert.ok(!evaluationsParent.children.includes('evaluations-capabilities'),
    '能力目录是顶导入口，不得成为左导 children');
  const route = nav.resolveRoute('/evaluations/capabilities');
  assert.equal(route.activeTab, 'evaluations-capabilities');
  assert.equal(route.sidebarTab, 'evaluations-evaluators');
  assert.equal(route.renderTab, 'evaluations');
  assert.equal(route.currentSubdomain, 'evaluations-evaluators');
  assert.equal(route.route.scope, 'capabilities');
}
// 模型页：评估器左导子域顶导第三入口，独立 routeConfig entry，
// 与能力目录同范式（sidebarTab/topnavParent 共享，scope 独立）。
{
  const jmTab = nav.tabConfig.find(c => c.tab === 'evaluations-judge-model');
  assert.ok(jmTab && jmTab.path === '/evaluations/judge-model' && jmTab.label === '模型');
  assert.ok(!evaluationsParent.children.includes('evaluations-judge-model'),
    '模型是顶导入口，不得成为左导 children');
  const route = nav.resolveRoute('/evaluations/judge-model');
  assert.equal(route.activeTab, 'evaluations-judge-model');
  assert.equal(route.sidebarTab, 'evaluations-evaluators');
  assert.equal(route.renderTab, 'evaluations');
  assert.equal(route.currentSubdomain, 'evaluations-evaluators');
  assert.equal(route.route.scope, 'judge-model');
  const items = nav.topnavConfig['evaluations-evaluators'];
  // 顶导高亮：/evaluations/judge-model 激活"模型"项
  const active = nav.activeTopnavItem(items, route.activeTab);
  assert.ok(active && active.label === '模型' && active.path === '/evaluations/judge-model',
    '/evaluations/judge-model 顶导必须高亮模型项');
  // 无跨子域双重激活：同子域内仅模型项 tab 匹配 activeTab
  assert.equal(items.filter(i => i.tab === route.activeTab).length, 1);
  assert.ok(items.filter(i => i.tab === route.activeTab)
    .every(i => i.topnavParent === 'evaluations-evaluators'),
    '激活项必须归属评估器子域，不跨子域');
  // 左导落地不变：仍落顶导首项能力目录
  assert.equal(nav.sidebarLandingPath('evaluations-evaluators'), '/evaluations/capabilities');
}
// 左导落地规则：进入带顶导的子域，默认选中顶导最左侧第一个菜单。
// 评估器子域首项为能力目录，故左导"评估器"落地 /evaluations/capabilities。
{
  assert.equal(nav.sidebarLandingPath('evaluations-evaluators'), '/evaluations/capabilities',
    '评估器左导入口必须落在顶导首项能力目录');
  // 单项顶导子域首项 path 与自身 path 相同，行为不变
  assert.equal(nav.sidebarLandingPath('tasks'), '/tasks');
  assert.equal(nav.sidebarLandingPath('evaluations-datasets'), '/evaluations/datasets');
  assert.equal(nav.sidebarLandingPath('evaluations-overview'), '/evaluations');
  // 无顶导的普通 tab 回落 pathByTab
  assert.equal(nav.sidebarLandingPath('chat'), '/chat');
  assert.equal(nav.sidebarLandingPath('models'), '/models');
}
// 执行任务：独立 routeConfig entry，与实验结果共享左导/顶导子域，不进左导 children
{
  const execTab = nav.tabConfig.find(c => c.tab === 'evaluations-executions');
  assert.ok(execTab && execTab.path === '/evaluations/executions' && execTab.label === '执行任务');
  assert.ok(!evaluationsParent.children.includes('evaluations-executions'),
    '执行任务是顶导入口，不得成为左导 children');
  const route = nav.resolveRoute('/evaluations/executions');
  assert.equal(route.activeTab, 'evaluations-executions');
  assert.equal(route.sidebarTab, 'evaluations-experiments');
  assert.equal(route.renderTab, 'evaluations');
  assert.equal(route.currentSubdomain, 'evaluations-experiments');
  assert.equal(route.route.scope, 'executions');
  const items = nav.topnavConfig['evaluations-experiments'];
  assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.label))), ['执行任务', '实验汇总', '评分明细']);
  assert.deepEqual(JSON.parse(JSON.stringify(items.map(i => i.path))),
    ['/evaluations/executions', '/evaluations/experiments', '/evaluations/experiment-results']);
  assert.equal(nav.sidebarLandingPath('evaluations-experiments'), '/evaluations/executions',
    '实验左导入口必须落在顶导首项执行任务');
}
// review 兼容页：不在左导 children，但 routeConfig 仍可解析 /evaluations/review。
{
  const route = nav.resolveRoute('/evaluations/review');
  assert.equal(route.activeTab, 'evaluations-review');
  assert.equal(route.sidebarTab, 'evaluations-review');
  assert.equal(route.renderTab, 'evaluations');
  assert.equal(route.route.scope, 'review');
  const items = nav.topnavConfig['evaluations-review'];
  assert.equal(items.length, 1);
  assert.equal(items[0].tab, 'evaluations-review');
  assert.equal(items[0].path, '/evaluations/review');
}
vm.runInNewContext(fs.readFileSync('app/interfaces/http/static/evaluation.js', 'utf8'), context);
const moduleApi = context.NAGENT.evaluations;
const state = view => ({ activeTab: 'evaluations-' + view, route: { scope: view } });
(async () => {
  await moduleApi.init(state('experiments'));
  assert.deepEqual(calls, ['status'], 'disabled subsystem must not call unregistered management APIs');
  assert.match(root.textContent, /评估功能未启用/);
  enabled = true;
  calls.length = 0;
  await moduleApi.refresh(state('experiments'));
  assert.deepEqual(calls, ['status', 'listExperiments']);
  assert.match(root.textContent, /暂无实验/);
  calls.length = 0;
  let finish;
  pendingStatus = new Promise(resolve => { finish = resolve; });
  const old = moduleApi.refresh(state('experiments'));
  pendingStatus = null;
  await moduleApi.activate(state('results'));
  finish({ enabled: true });
  await old;
  assert.deepEqual(calls, ['status', 'status', 'listResults'], 'late route responses must not render previous view');
  assert.match(root.textContent, /暂无评估结果/);
  calls.length = 0;
  await moduleApi.refresh(state('datasets'));
  assert.deepEqual(calls, ['status', 'listDatasets']);
  assert.match(root.textContent, /暂无数据集/);
  // 执行任务视图已注册：本 harness 未加载 evaluation-executions.js，
  // 控制器为 null 时必须显式不可用提示，不得解引用抛错或静默空白。
  calls.length = 0;
  await moduleApi.refresh(state('executions'));
  assert.deepEqual(calls, ['status'], '空控制器不得调用未注册的 listExecutions');
  assert.match(root.textContent, /暂不可用/);
  api.listDatasets = async () => ({ items: [{ dataset_id: 'manual', dataset_version: 'v1',
    target_kind: 'agent_run', case_count: 1, status: 'valid' }] });
  api.getDataset = async () => ({ dataset_id: 'manual', dataset_version: 'v1',
    target_kind: 'agent_run', dataset_hash: 'hash', cases: [{ case_id: 'case-1', input: { query: '<script>unsafe</script>' } }] });
  await moduleApi.refresh(state('datasets'));
  await find(root, node => node.textContent === '详情' && node.handlers.click).handlers.click();
  assert.match(body.textContent, /<script>unsafe<\/script>/);
  moduleApi.deactivate();
  assert.equal(body.children.length, 0);
  await moduleApi.refresh(state('datasets'));
  find(root, node => node.textContent === '导入标注 JSON' && node.handlers.click).handlers.click();
  const form = find(body, node => node.tag === 'form');
  const textarea = find(form, node => node.tag === 'textarea');
  let imports = 0;
  api.importDataset = async payload => { imports++; assert.equal(payload.dataset_id, 'manual'); };
  textarea.value = '{bad json';
  await form.handlers.submit({ preventDefault() {} });
  assert.equal(imports, 0);
  assert.match(body.textContent, /JSON 格式不正确/);
  textarea.value = JSON.stringify({ dataset_id: 'manual' });
  await form.handlers.submit({ preventDefault() {} });
  assert.equal(imports, 1);
  assert.equal(body.children.length, 0);
  // 模型视图已注册（views key 与 route scope 'judge-model' 对齐）：
  // status 探测通过后依次拉取 Provider 列表与供给投影（plan-261116 T6：
  // 页面为 Provider 列表形态，无旧默认模型编辑输入）。
  api.getJudgeModel = async () => {
    calls.push('getJudgeModel');
    return { judge_ready: true, supply_source: 'provider',
      effective_model: 'cfg-model', env_baseline_model: 'env-model',
      provider_base_url_host: 'api.example.com', default_provider_id: '',
      default_provider_name: null, default_provider_model: null,
      default_provider_missing: false, config_version: 7 };
  };
  context.NAGENT.api.listProviders = async () => {
    calls.push('listProviders');
    return [];
  };
  calls.length = 0;
  await moduleApi.refresh(state('judge-model'));
  assert.deepEqual(calls, ['status', 'listProviders', 'getJudgeModel']);
  assert.match(root.textContent, /Provider 列表/);
  assert.match(root.textContent, /B 类 env-only，Dashboard 不可改/);
  assert.match(root.textContent, /未设置默认 Provider，回落 env 基线/,
    '空列表且未设默认时显示回落提示');
  assert.ok(find(root, node => node.textContent === '新增' && node.handlers.click),
    'Provider 列表 Sector header 提供新增入口');
  assert.ok(!find(root, node => node.name === 'evaluation_judge_model'),
    '旧默认模型编辑输入已移除');
  console.log('evaluation navigation and dataset management passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

{
  const route = nav.resolveRoute('/evaluations/experiment-results');
  assert.equal(route.activeTab, 'evaluations-experiment-results');
  assert.equal(route.sidebarTab, 'evaluations-experiments');
  assert.equal(route.currentSubdomain, 'evaluations-experiments');
  assert.equal(route.route.scope, 'experiment-results');
  assert.ok(!evaluationsParent.children.includes('evaluations-experiment-results'));
}
