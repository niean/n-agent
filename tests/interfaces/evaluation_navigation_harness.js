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
  ['experiments', '/evaluations/experiments', '实验'],
  ['results', '/evaluations/results', '结果'],
  ['annotations', '/evaluations/annotations', '人工标注'],
];
const nav = context.NAGENT.navigation;
const evaluationsParent = nav.tabConfig.find(c => c.tab === 'evaluations');
assert.ok(evaluationsParent && evaluationsParent.parent === true, 'evaluations must stay an independent parent');
// 跨 vm realm 数组原型不同，assert.deepEqual 直接比较必失败；
// 先 JSON 归一成宿主 realm 普通数组再比较，勿删归一步骤。
assert.deepEqual(JSON.parse(JSON.stringify(evaluationsParent.children)), evaluationViews.map(([view]) => 'evaluations-' + view), 'six-menu children order');
assert.ok(!evaluationsParent.children.includes('evaluations-review'), 'review must not be a sidebar child');
for (const [view, path, label] of evaluationViews) {
  const route = nav.resolveRoute(path);
  assert.equal(route.activeTab, 'evaluations-' + view);
  assert.equal(route.sidebarTab, 'evaluations-' + view);
  assert.equal(route.renderTab, 'evaluations');
  assert.equal(route.currentSubdomain, 'evaluations-' + view);
  assert.equal(route.route.scope, view);
  const items = nav.topnavConfig[route.currentSubdomain];
  assert.equal(items.length, 1);
  assert.equal(items[0].tab, route.activeTab);
  assert.equal(items[0].path, path);
  assert.equal(items[0].label, label);
  assert.equal(items[0].concern, 'evaluation');
  assert.equal(items[0].scope, view);
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
  console.log('evaluation navigation and dataset management passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
