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
const actionButton = (text, fn) => { const b = el('button', 'btn', text); b.type = 'button'; b.addEventListener('click', fn); return b; };
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
    return { items: [{ execution_id: 'execution-one', name: 'vision test', dataset_id: 'agent-e2e-v1', dataset_version: '1', phase }], next_cursor: listCursor };
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
const controller = context.NAGENT.evaluationExecutions.createController({ api, el, actionButton,
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
  phase='completed'; await controller.openDetail('execution-one',1);
  assert.ok(document.body.textContent.includes('执行完成'));
  assert.ok(document.body.textContent.includes('已登记保留'), '详情展示保留账目状态');
  assert.ok(document.body.textContent.includes('保留登记:2'), '展示保留登记数量');
  assert.ok(!document.body.textContent.includes('测试通过'));
  // 执行记录列表：无手动刷新按钮，区块标题走 blockTitle，无游标时不渲染分页行
  const listBody = new Element('div');
  await controller.renderList(listBody, 1);
  assert.ok(listBody.textContent.includes('实验执行'));
  assert.ok(find(listBody, n => n.className === 'evaluations-block-title'), '区块标题复用 blockTitle');
  assert.ok(!listBody.textContent.includes('刷新执行记录'), '手动刷新按钮应删除，Tab 重入即重新加载');
  assert.ok(!find(listBody, n => n.className === 'evaluations-pager'), '首页无游标不渲染分页行');
  // next_cursor 存在时渲染 evaluations-pager + 下一页，点击携带游标
  listCursor = 'tok2';
  await controller.renderList(listBody, 1);
  const pager = find(listBody, n => n.className === 'evaluations-pager');
  assert.ok(pager, '分页按钮统一收纳进 evaluations-pager');
  const nextBtn = find(listBody, n => n.tag === 'button' && n.textContent === '下一页');
  assert.ok(nextBtn, '分页文案与评估结果列表一致');
  await nextBtn.handlers.click();
  assert.equal(listCalls.at(-1).cursor, 'tok2', '下一页携带 next_cursor');
  // 带游标渲染时出现上一页，点击回到无游标首页
  await controller.renderList(listBody, 1, 'tok2');
  const prevBtn = find(listBody, n => n.tag === 'button' && n.textContent === '上一页');
  assert.ok(prevBtn);
  await prevBtn.handlers.click();
  assert.equal(listCalls.at(-1).cursor, undefined, '上一页回到最新一页');
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
