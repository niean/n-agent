const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
assert.ok(fs.existsSync('app/interfaces/http/static/evaluation-executions.js'), 'execution UI module missing');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.handlers = {}; this.attributes = {}; this._value = ''; this.classList = { contains: () => true }; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(n => n.textContent).join(''); }
  set value(value) { this._value = String(value); }
  get value() { return this._value || (this.tag === 'select' && this.children.length ? this.children[0].value : ''); }
  appendChild(node) { this.children.push(node); node.parentElement = this; return node; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(n => n !== this); }
  querySelector(selector) { return find(this, n => selector.includes('submit') && n.type === 'submit'); }
  querySelectorAll() { return []; }
}
function find(node, predicate) { if (predicate(node)) return node; for (const child of node.children) { const found = find(child, predicate); if (found) return found; } }
const document = { body: new Element('body'), createElement: tag => new Element(tag) };
const el = (tag, cls, text) => { const n = new Element(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const actionButton = (text, fn) => { const b = el('button', 'btn', text); b.type = 'button'; b.disabled = false; b.addEventListener('click', fn); return b; };
const disabledButton = (text) => { const b = el('button', 'btn', text); b.type = 'button'; b.disabled = true; return b; };
const LIST_PAGE_SIZE = 20;
// 与 evaluation.js 共享 pagerBar 同构：evaluations-pager + info + 始终占位按钮
const pagerBar = (options) => {
  const row = el('div', 'evaluations-pager');
  const info = el('span', 'evaluations-pager__info');
  const pageNo = options.pageIndex + 1;
  info.textContent = '第 ' + pageNo
    + (options.totalPages !== undefined ? '/' + options.totalPages : '') + ' 页';
  row.appendChild(info);
  if (options.hasPrev) row.appendChild(actionButton('上一页', options.onPrev));
  else row.appendChild(disabledButton('上一页'));
  if (options.hasNext) row.appendChild(actionButton('下一页', options.onNext));
  else row.appendChild(disabledButton('下一页'));
  return row;
};
let active = true, failSubmission = true, phase = 'scoring', capabilitiesReady = true, detailCalls = 0;
let preflightRunnable = true;
let listCursor = null, failList = false;
const submissions = [], timers = [], listCalls = [], preflightCalls = [];
const dataset = { dataset_id: 'agent-e2e-v1', dataset_version: '1', dataset_hash: 'a'.repeat(64), target_kind: 'agent_run', case_count: 1, status: 'valid' };
const api = {
  getExecutionCapabilities: async () => ({ ready: capabilitiesReady, judge_ready: capabilitiesReady, self_judge: true, judge_model: 'current-model',
    definitions: [{ definition_id: 'agent.answer_correctness', definition_version: 'experiment-v1', metric_name: 'agent.answer_correctness', judge_model: 'old-model', executable: capabilitiesReady },
      { definition_id: 'agent.answer_correctness', definition_version: 'experiment-v2', metric_name: 'agent.answer_correctness', judge_model: 'current-model', executable: capabilitiesReady }] }),
  listDatasets: async () => ({ items: [dataset] }),
  createExecution: async body => { submissions.push(JSON.parse(JSON.stringify(body))); if (failSubmission) throw new Error('network'); return { execution_id: 'execution-one', phase: 'queued' }; },
  listExecutions: async params => {
    listCalls.push(Object.assign({}, params));
    if (failList) throw new Error('network');
    return { items: [{ execution_id: 'execution-one', name: 'vision test', dataset_id: 'agent-e2e-v1', dataset_version: '1', phase }], next_cursor: listCursor, total_pages: 7 };
  },
  preflightExecution: async body => { preflightCalls.push(JSON.parse(JSON.stringify(body)));
    return preflightRunnable
      ? { runnable: true, blocked: [], cases: [], selected_case_ids: ['case-00'], item_count: 12, judge_calls: 12, max_replicates: 8, total_budget_seconds: 10200 }
      : { runnable: false, blocked: ['场景通道未接入执行适配: cli'], cases: [], selected_case_ids: [], item_count: 0, judge_calls: 0, max_replicates: 0, total_budget_seconds: 1800 }; },
  getExecution: async () => { detailCalls++; return { execution_id: 'execution-one', phase, experiment_id: 'experiment-one', quality_counts: { value: 0, skipped: 1, error: 0, pending: 0 }, report: { cleanup_failures: 0, release: { state: 'retention_registered', retained: 2, cleanup_failed: 0 }, items: [{ case_id: 'one', outcome: 'succeeded', answer: '<script>unsafe</script>' }] } }; },
};
const context = { document, NAGENT: {}, setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout: () => {}, crypto: { randomUUID: () => 'request-'+submissions.length }, Node: Element };
context.window = context;
vm.runInNewContext(fs.readFileSync('app/interfaces/http/static/evaluation-executions.js', 'utf8'), context);
const controller = context.NAGENT.evaluationExecutions.createController({ api, el, actionButton, disabledButton,
  pagerBar, LIST_PAGE_SIZE,
  buildFrame: title => { const backdrop = el('div','modal-backdrop'); const form = el('form','providers-form'); const closeBtn = actionButton('关闭', () => backdrop.remove()); backdrop.append(form,closeBtn); return {backdrop, form, closeBtn}; },
  formActions: (form, cancel, submit) => { const actions = el('div','providers-form__actions'); actions.append(actionButton(cancel, () => form.parentElement.remove())); if (submit) { const b=actionButton(submit,()=>{}); b.type='submit'; b.className='btn btn--primary'; actions.append(b); } form.append(actions); return actions; },
  mountDetailModal: frame => document.body.append(frame.backdrop), closeBackdrop: node => node.remove(),
  filterItem: (name, control) => { const label=el('label','evaluations-filter__item',name); label.append(control); return label; },
  blockTitle: title => el('div','evaluations-block-title', title),
  kvRow: (name,value) => el('div','policy-item', name+':'+value),
  table: (headers,rows) => { const node=el('table','document-table'); rows.forEach(row => row.forEach(cell => node.append(cell instanceof Element ? cell : el('span','',cell)))); return node; },
  scrollTable: node => node, formatTime: value => value || '-', isCurrent: () => active, openExperimentModal: () => {},
});

(async () => {
  await controller.openCreate(dataset, 1);
  let form = find(document.body, n => n.tag === 'form');
  const name = find(form,n=>n.name==='name'); name.value='vision test';
  assert.equal(find(form,n=>n.name==='dataset_hash').value, dataset.dataset_hash);
  const submit = find(form,n=>n.type==='submit');
  // plan T5：只读预检（valid != runnable）——不提交、不运行，只展示
  const preflightBtn = find(form, n => n.tag === 'button' && n.textContent === '预检');
  assert.ok(preflightBtn, '发起实验弹窗提供预检入口');
  await preflightBtn.handlers.click();
  assert.equal(preflightCalls.length, 1, '预检只读调用');
  assert.ok(form.textContent.includes('预检通过'), '预检通过展示分母与 Judge 调用数');
  assert.ok(form.textContent.includes('12 项'), '展示目标项数');
  preflightRunnable = false;
  await preflightBtn.handlers.click();
  assert.ok(form.textContent.includes('预检未通过'), 'blocked 展示阻塞原因');
  assert.ok(form.textContent.includes('cli'), '阻塞原因含通道名');
  preflightRunnable = true;
  await form.handlers.submit({preventDefault(){}});
  assert.equal(submissions.length,1);
  assert.equal(submissions[0].dataset_id,'agent-e2e-v1');
  assert.equal(submissions[0].replicates,1);
  assert.equal(submissions[0].evaluator_set.length,1,'多个正确性版本最多默认选择一个');
  assert.equal(submissions[0].evaluator_set[0].definition_version,'experiment-v2','优先选择当前 Judge 对应版本');
  assert.match(form.textContent,/network/);
  assert.equal(name.value,'vision test');
  const key = submissions[0].request_key;
  failSubmission=false;
  await form.handlers.submit({preventDefault(){}});
  assert.equal(submissions[1].request_key,key,'网络重试必须复用 key');
  assert.ok(document.body.textContent.includes('评分中'));
  assert.ok(document.body.textContent.includes('<script>unsafe</script>'),'回答按文本渲染');
  const footerClose=find(document.body,n=>n.tag==='button' && n.textContent==='关闭' && n.parentElement.className==='providers-form__actions');
  footerClose.handlers.click();
  const callsBefore=detailCalls;
  for (const timer of timers.splice(0)) await timer();
  assert.equal(detailCalls,callsBefore,'底部关闭后不再请求执行详情');
  // 多版本 flat 契约回归：数据集页聚合仅为前端展示，发起实验仍消费扁平
  // items——同 ID 全部有效版本可选，invalid 与非 agent_run 目标排除
  const realListDatasets = api.listDatasets.bind(api);
  api.listDatasets = async () => ({ items: [
    dataset,
    { dataset_id: 'agent-e2e-v1', dataset_version: '2', dataset_hash: 'b'.repeat(64), target_kind: 'agent_run', case_count: 2, status: 'valid' },
    { dataset_id: 'agent-e2e-v1', dataset_version: '3', dataset_hash: 'x', target_kind: 'agent_run', case_count: 1, status: 'invalid' },
    { dataset_id: 'other-retrieval', dataset_version: '1', dataset_hash: 'y', target_kind: 'retrieval', case_count: 1, status: 'valid' },
  ] });
  await controller.openCreate(null, 1);
  const mvForm = find(document.body, n => n.tag === 'form');
  const datasetSelect = find(mvForm, n => n.name === 'dataset');
  assert.equal(datasetSelect.className, 'select-control', '数据集下拉须接入共享 .select-control 外观');
  const options = datasetSelect.children.filter(n => n.tag === 'option');
  assert.equal(options.length, 2, '两个有效 agent_run 版本可选，invalid/retrieval 排除');
  assert.ok(options.some(o => o.textContent.includes('agent-e2e-v1 / 1')), '版本 1 在选项中');
  assert.ok(options.some(o => o.textContent.includes('agent-e2e-v1 / 2')), '版本 2 在选项中');
  assert.ok(!options.some(o => /\/ 3|other-retrieval/.test(o.textContent)), 'invalid 与 retrieval 不出现');
  datasetSelect.value = '1';
  datasetSelect.handlers.change();
  assert.equal(find(mvForm, n => n.name === 'dataset_hash').value, 'b'.repeat(64),
    '切换版本后 hash 随选项更新');
  find(mvForm, n => n.tag === 'button' && n.textContent === '取消'
    && n.parentElement.className === 'providers-form__actions').handlers.click();
  api.listDatasets = realListDatasets;

  phase='completed'; await controller.openDetail('execution-one',1);
  assert.ok(document.body.textContent.includes('执行完成'));
  assert.ok(document.body.textContent.includes('已登记保留'), '详情展示保留账目状态');
  assert.ok(document.body.textContent.includes('保留登记:2'), '展示保留登记数量');
  assert.ok(!document.body.textContent.includes('测试通过'));
  // 执行记录列表：无手动刷新按钮，不渲染区块标题行（入口名即执行任务，标题冗余）；
  // 共享 pagerBar 始终占位——首页无 next_cursor 时下一页禁用而非隐藏。
  const listBody = new Element('div');
  await controller.renderList(listBody, 1);
  assert.ok(!listBody.textContent.includes('实验执行'), '列表页不再渲染实验执行区块标题');
  assert.equal(find(listBody, n => n.className === 'evaluations-block-title'), undefined,
    '列表页不挂载 blockTitle 行组件');
  assert.ok(!listBody.textContent.includes('刷新执行记录'), '手动刷新按钮应删除，Tab 重入即重新加载');
  assert.equal(listCalls.at(-1).limit, 20, '统一每页 20 条');
  assert.equal(listCalls.at(-1).cursor, undefined, '首页请求无游标');
  const homePager = find(listBody, n => n.className === 'evaluations-pager');
  assert.ok(homePager, '首页仍渲染共享 evaluations-pager');
  assert.match(homePager.textContent, /第 1\/7 页/, '执行任务使用后端总页数');
  const homeNext = find(homePager, n => n.tag === 'button' && n.textContent === '下一页');
  const homePrev = find(homePager, n => n.tag === 'button' && n.textContent === '上一页');
  assert.equal(homeNext.disabled, true, '无 next_cursor 时下一页禁用占位');
  assert.equal(homePrev.disabled, true, '首页上一页禁用占位');
  // next_cursor 存在时下一页可用，点击携带游标入栈
  listCursor = 'tok2';
  await controller.renderList(listBody, 1);
  const pager = find(listBody, n => n.className === 'evaluations-pager');
  assert.ok(pager, '分页按钮统一收纳进 evaluations-pager');
  const nextBtn = find(listBody, n => n.tag === 'button' && n.textContent === '下一页');
  assert.ok(nextBtn, '分页文案与评估结果列表一致');
  assert.equal(nextBtn.disabled, false);
  await nextBtn.handlers.click();
  assert.equal(listCalls.at(-1).cursor, 'tok2', '下一页携带 next_cursor');
  assert.equal(listCalls.at(-1).limit, 20);
  // 第 2 页出现可用上一页，点击从游标栈回退到无游标首页
  const prevBtn = find(listBody, n => n.tag === 'button' && n.textContent === '上一页');
  assert.ok(prevBtn);
  assert.equal(prevBtn.disabled, false);
  await prevBtn.handlers.click();
  assert.equal(listCalls.at(-1).cursor, undefined, '上一页从栈回退到首页');
  // 视图入口 renderList 重置回首页（生命周期合同）
  listCursor = 'tok3';
  await nextBtn.handlers.click();
  assert.equal(listCalls.at(-1).cursor, 'tok2');
  await controller.renderList(listBody, 1);
  assert.equal(listCalls.at(-1).cursor, undefined, '重入视图回第 1 页');
  // 加载失败：清空旧内容并提供重试入口，重试后恢复
  failList = true;
  await controller.renderList(listBody, 1);
  assert.ok(find(listBody, n => n.className === 'error-state'), '失败渲染错误态');
  const retryBtn = find(listBody, n => n.tag === 'button' && n.textContent === '重试');
  assert.ok(retryBtn, '错误态可重试');
  failList = false;
  await retryBtn.handlers.click();
  assert.ok(listBody.textContent.includes('vision test'), '重试后恢复列表');
  controller.deactivate(); active=false;
  const before=document.body.children.length;
  await controller.openDetail('execution-one',1);
  assert.equal(document.body.children.length,before,'迟到响应不得打开弹窗');
  active=true; capabilitiesReady=false;
  await controller.openCreate(dataset,2);
  form=find(document.body,n=>n.tag==='form');
  assert.equal(find(form,n=>n.type==='submit').disabled,true);
  controller.deactivate();
  console.log('evaluation executions harness passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
