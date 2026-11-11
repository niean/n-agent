// 评估器管理视图 harness（Task 11/spec 4.2-4.3）。
// Part A：management-api.js evaluations 对象新增 5 个评估器管理方法与
// 端点契约（list/get/create/usage/capabilities）。
// Part B：evaluation.js evaluators 子视图生命周期——列表/过滤/能力目录
// 状态文案/详情 modal/复制新建（能力目录生成 + 新版本号强制 + 自动使用
// 默认关闭）/expected_revision CAS 启停/409 保留输入提示刷新/同指标多
// 版本启用提示/迟到响应废弃/安全渲染（textContent，无 script 元素）。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

let activeElement = null;
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.handlers = {};
    this.attributes = {};
    this.classList = {
      contains: () => true,
      add: (...names) => { this.classAdds = (this.classAdds || []).concat(names); },
      remove: (...names) => {
        this.classAdds = (this.classAdds || []).filter(name => !names.includes(name));
      },
    };
  }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(n => n.textContent).join(''); }
  appendChild(node) { this.children.push(node); node.parentElement = this; return node; }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  // 截断提示等场景使用；harness 不校验插入位置，退化为 append
  insertBefore(node) { return this.appendChild(node); }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  querySelector() { return find(this, node => node.type === 'submit'); }
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(n => n !== this); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }
  // 最小焦点桩：记录焦点节点，供 S8 焦点恢复合同验证
  focus() { this.focused = true; activeElement = this; }
}
function getActiveElement() { return activeElement; }
function find(rootEl, predicate) {
  if (predicate(rootEl)) return rootEl;
  for (const child of rootEl.children) { const result = find(child, predicate); if (result) return result; }
}
function findAll(rootEl, predicate, out) {
  out = out || [];
  if (predicate(rootEl)) out.push(rootEl);
  for (const child of rootEl.children) findAll(child, predicate, out);
  return out;
}

// ---------------------------------------------------------------------------
// Part A：management-api.js 评估器管理方法与端点契约
// ---------------------------------------------------------------------------
const fetchCalls = [];
const apiContext = {
  fetch: (url, options) => {
    fetchCalls.push({ url: url, options: options || {} });
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  },
  FormData: function FormData() { this.append = () => {}; },
};
apiContext.window = apiContext;
vm.runInNewContext(fs.readFileSync('app/interfaces/http/static/management-api.js', 'utf8'), apiContext);
const evaluationsApi = apiContext.NAGENT.api.evaluations;
for (const method of ['listEvaluators', 'getEvaluator', 'createEvaluator', 'setEvaluatorUsage', 'getCapabilities']) {
  assert.equal(typeof evaluationsApi[method], 'function', 'evaluations API 缺少方法: ' + method);
}
(async () => {
  await evaluationsApi.listEvaluators({ target_kind: 'agent_run', method: 'llm_judge', limit: 10, cursor: 'abc' });
  assert.equal(fetchCalls.at(-1).url,
    '/chat/evaluations/evaluators?target_kind=agent_run&method=llm_judge&limit=10&cursor=abc');
  await evaluationsApi.getEvaluator('kb.recall_at_5', 'v1');
  assert.equal(fetchCalls.at(-1).url, '/chat/evaluations/evaluators/kb.recall_at_5/v1');
  await evaluationsApi.createEvaluator({ definition_id: 'x' });
  assert.equal(fetchCalls.at(-1).url, '/chat/evaluations/evaluators');
  assert.equal(fetchCalls.at(-1).options.method, 'POST');
  await evaluationsApi.setEvaluatorUsage('kb.recall_at_5', 'v1', { online_enabled: true, expected_revision: 3 });
  assert.equal(fetchCalls.at(-1).url, '/chat/evaluations/evaluators/kb.recall_at_5/v1/usage');
  assert.equal(fetchCalls.at(-1).options.method, 'PATCH');
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body), { online_enabled: true, expected_revision: 3 });
  await evaluationsApi.getCapabilities();
  assert.equal(fetchCalls.at(-1).url, '/chat/evaluations/evaluators/capabilities');

  // Part A2（Task 12/spec 4.4）：人工标注 API——队列/上下文/提交端点契约
  for (const method of ['listAnnotations', 'getAnnotation', 'submitAnnotation']) {
    assert.equal(typeof evaluationsApi[method], 'function', 'evaluations API 缺少标注方法: ' + method);
  }
  await evaluationsApi.listAnnotations({ state: 'pending', reason: 'disputed', limit: 10, cursor: 'c1', threshold: '0.6' });
  assert.equal(fetchCalls.at(-1).url,
    '/chat/evaluations/annotations?state=pending&reason=disputed&limit=10&cursor=c1&threshold=0.6');
  await evaluationsApi.getAnnotation('res-1');
  assert.equal(fetchCalls.at(-1).url, '/chat/evaluations/annotations/res-1');
  await evaluationsApi.submitAnnotation('res-1', { value: 0.42, request_key: 'k-1', expected_revision: 0 });
  assert.equal(fetchCalls.at(-1).url, '/chat/evaluations/annotations/res-1');
  assert.equal(fetchCalls.at(-1).options.method, 'POST');
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body),
    { value: 0.42, request_key: 'k-1', expected_revision: 0 });

  // Part A3（Task 13/spec 4.5-4.6）：概览分析 + 只读实验对比端点契约，
  // 以及错误正文挂载（422 incomparable_experiments 携带 diff_dimensions）
  for (const method of ['getAnalytics', 'compareExperiments']) {
    assert.equal(typeof evaluationsApi[method], 'function', 'evaluations API 缺少方法: ' + method);
  }
  await evaluationsApi.getAnalytics({
    created_from: '2026-11-01T00:00:00+00:00', target_kind: 'agent_run',
    definition_id: 'kb.recall_at_5', definition_version: 'v1',
    provenance: 'automatic', status: 'value',
  });
  assert.equal(fetchCalls.at(-1).url,
    '/chat/evaluations/analytics?created_from=2026-11-01T00%3A00%3A00%2B00%3A00'
    + '&target_kind=agent_run&definition_id=kb.recall_at_5&definition_version=v1'
    + '&provenance=automatic&status=value');
  await evaluationsApi.compareExperiments({ candidate_id: 'a', baseline_id: 'b', policy: { schema_version: '1' } });
  assert.equal(fetchCalls.at(-1).url, '/chat/evaluations/experiments/compare');
  assert.equal(fetchCalls.at(-1).options.method, 'POST');
  assert.deepEqual(JSON.parse(fetchCalls.at(-1).options.body),
    { candidate_id: 'a', baseline_id: 'b', policy: { schema_version: '1' } });
  {
    const originalFetch = apiContext.fetch;
    apiContext.fetch = async () => ({
      ok: false, status: 422,
      json: async () => ({ error: { code: 'incomparable_experiments', diff_dimensions: { dataset_id: true } } }),
    });
    const thrown = await evaluationsApi
      .compareExperiments({ candidate_id: 'a', baseline_id: 'b' })
      .then(() => null, err => err);
    apiContext.fetch = originalFetch;
    assert.ok(thrown, '422 业务错误必须抛错');
    assert.equal(thrown.message, 'incomparable_experiments');
    assert.deepEqual(JSON.parse(JSON.stringify(thrown.data.error.diff_dimensions)),
      { dataset_id: true }, '错误正文须挂 error.data 供展示差异维度');
  }

  // -------------------------------------------------------------------------
  // Part B：evaluation.js evaluators 视图
  // -------------------------------------------------------------------------
  const root = new Element();
  const body = new Element();
  const createdTags = [];
  const calls = [];
  const alerts = [];
  const listParamsHistory = [];
  const usageCalls = [];
  let createdPayloads = [];
  let listResult = { items: [], next_cursor: null };
  let createBehavior = 'ok';
  let usageBehavior = 'ok';
  // Part C（Task 12）：人工标注视图 mock 状态
  const annParamsHistory = [];
  const submitCalls = [];
  let annQueue = { items: [], next_cursor: null, snapshot_at: '' };
  let submitBehavior = 'ok';
  let annContext = {
    source_result_id: 'res-ann-1',
    target: { service: 'n-agent', kind: 'agent_run', subject_id: 'run-1', subject_revision: 'ok' },
    definition: {
      definition_id: 'agent.answer_relevance', version: 'v1',
      metric_name: 'agent.answer_relevance', output_type: 'number',
      range_or_categories: null, rubric_summary: '按事实核对回答相关性',
    },
    evidence_status: 'available',
    evidence: { request: '如何重置密码', answer: '在设置页点击重置' },
    latest_human_feedback: null,
    current_revision: 0,
    can_submit: true,
    reject_reason: null,
  };
  const CAPS = {
    judge_configured: false,
    entries: [
      { metric_name: 'kb.recall_at_5', method: 'deterministic', target_kind: 'retrieval',
        output_type: 'number', executable: true, judge_required: false, online_forbidden: false },
      { metric_name: 'agent.answer_relevance', method: 'llm_judge', target_kind: 'agent_run',
        output_type: 'number', executable: false, judge_required: true, online_forbidden: false },
      { metric_name: '*', method: 'human', target_kind: 'agent_run',
        output_type: 'number', executable: false, judge_required: false, online_forbidden: true },
      { metric_name: '*', method: 'human', target_kind: 'retrieval',
        output_type: 'number', executable: false, judge_required: false, online_forbidden: true },
    ],
  };
  const DETAIL = {
    definition: {
      definition_id: 'kb.recall_at_5', version: 'v1', metric_name: 'kb.recall_at_5',
      target_kind: 'retrieval', method: 'deterministic', output_type: 'number',
      direction: '<script>unsafe</script>', required_fields: ['query'],
      judge_model: '', judge_params: {}, timeout_seconds: 30, budget: {},
      material_hash: 'abc12345',
    },
    usage: { definition_id: 'kb.recall_at_5', definition_version: 'v1',
      online_enabled: false, revision: 1, updated_at: '', updated_by: 'n-agent' },
    calibration: null,
  };
  const api = {
    status: async () => { calls.push(['status']); return { enabled: true }; },
    listDatasets: async () => { calls.push(['listDatasets']); return { items: [] }; },
    listEvaluators: async params => {
      calls.push(['listEvaluators']);
      listParamsHistory.push(params);
      return listResult;
    },
    getCapabilities: async () => { calls.push(['getCapabilities']); return CAPS; },
    getEvaluator: async () => { calls.push(['getEvaluator']); return DETAIL; },
    createEvaluator: async payload => {
      calls.push(['createEvaluator']);
      createdPayloads.push(payload);
      if (createBehavior === 'conflict') throw new Error('conflict');
      return { definition: payload, usage: { online_enabled: false, revision: 1 } };
    },
    setEvaluatorUsage: async (id, version, usagePayload) => {
      calls.push(['setEvaluatorUsage']);
      usageCalls.push([id, version, usagePayload]);
      if (usageBehavior === 'conflict') throw new Error('conflict');
      return { usage: { online_enabled: usagePayload.online_enabled, revision: usagePayload.expected_revision + 1 } };
    },
    listAnnotations: async params => {
      calls.push(['listAnnotations']);
      annParamsHistory.push(params);
      return annQueue;
    },
    getAnnotation: async id => {
      calls.push(['getAnnotation', id]);
      return annContext;
    },
    submitAnnotation: async (id, payload) => {
      calls.push(['submitAnnotation']);
      submitCalls.push([id, payload]);
      if (submitBehavior === 'conflict') throw new Error('conflict');
      if (submitBehavior === 'error') throw new Error('request_failed');
      return { feedback_id: 'fb-1', result_id: 'hres-1', revision: 1, is_current: true, duplicate: false, superseded_feedback_id: null };
    },
  };
  const context = {
    Node: Element,
    document: {
      getElementById: () => root,
      createElement: tag => { createdTags.push(tag); return new Element(tag); },
      body,
      get activeElement() { return getActiveElement(); },
    },
    NAGENT: {
      api: { evaluations: api },
      modal: { alert: message => { alerts.push(String(message)); } },
    },
  };
  context.window = context;
  vm.runInNewContext(fs.readFileSync('app/interfaces/http/static/evaluation.js', 'utf8'), context);
  const moduleApi = context.NAGENT.evaluations;
  const state = view => ({ activeTab: 'evaluations-' + view, route: { scope: view } });
  const findButton = (label, scope) =>
    find(scope || root, node => node.textContent === label && node.handlers.click);

  // 1) 空列表占位 + 能力目录状态文案（judge_configured=false 时 llm_judge
  //    条目未就绪；页面区分可配置/执行已就绪/未配置 Judge）
  await moduleApi.refresh(state('evaluators'));
  assert.match(root.textContent, /暂无评估器/);
  assert.match(root.textContent, /未配置 Judge/, 'llm_judge 条目须显示未配置 Judge');
  assert.match(root.textContent, /执行已就绪/);
  assert.match(root.textContent, /可配置/);

  // 2) 过滤参数进入 listEvaluators（spec 4.2：按目标种类/方法过滤）
  const methodFilter = find(root, node => node.name === 'evaluator_method_filter' && node.handlers.change);
  assert.ok(methodFilter, '缺少方法过滤控件');
  methodFilter.value = 'deterministic';
  listParamsHistory.length = 0;
  await methodFilter.handlers.change();
  // 跨 vm realm 数组/对象原型不同，assert.deepEqual 直接比较必失败；
  // 先 JSON 归一成宿主 realm 普通对象再比较（与 navigation harness 同）。
  assert.deepEqual(JSON.parse(JSON.stringify(listParamsHistory.at(-1))),
    { limit: 50, method: 'deterministic' });
  methodFilter.value = '';
  await methodFilter.handlers.change();

  // 3) 列表渲染 + 启停按钮携带 expected_revision（取 usage.revision）
  listResult = { items: [
    { definition_id: 'agent.answer_relevance', version: 'v1', metric_name: 'agent.answer_relevance',
      target_kind: 'agent_run', method: 'llm_judge',
      usage: { online_enabled: false, revision: 1 } },
  ], next_cursor: null };
  await moduleApi.refresh(state('evaluators'));
  assert.match(root.textContent, /agent\.answer_relevance/);
  usageCalls.length = 0;
  calls.length = 0;
  await findButton('启用').handlers.click();
  assert.deepEqual(JSON.parse(JSON.stringify(usageCalls[0])),
    ['agent.answer_relevance', 'v1', { online_enabled: true, expected_revision: 1 }],
    '启停必须携带 expected_revision（取列表 usage.revision）');
  assert.ok(calls.some(c => c[0] === 'setEvaluatorUsage'));
  assert.ok(calls.some(c => c[0] === 'listEvaluators'), '启停成功后刷新列表');

  // 4) 启停 409：不自动重试，提示冲突并要求显式刷新
  usageBehavior = 'conflict';
  calls.length = 0;
  alerts.length = 0;
  await findButton('启用').handlers.click();
  assert.equal(calls.filter(c => c[0] === 'setEvaluatorUsage').length, 1, '409 不得自动重试');
  assert.match(alerts.join('\n'), /冲突/);
  assert.match(alerts.join('\n'), /刷新/);
  usageBehavior = 'ok';

  // 5) 同指标多版本同时启用：页面提示会增加评估作业，结果不合并
  listResult = { items: [
    { definition_id: 'kb.recall_at_5', version: 'v1', metric_name: 'kb.recall_at_5',
      target_kind: 'retrieval', method: 'deterministic',
      usage: { online_enabled: true, revision: 2 } },
    { definition_id: 'kb.recall_at_5', version: 'v2', metric_name: 'kb.recall_at_5',
      target_kind: 'retrieval', method: 'deterministic',
      usage: { online_enabled: true, revision: 1 } },
  ], next_cursor: null };
  await moduleApi.refresh(state('evaluators'));
  assert.match(root.textContent, /会增加评估作业，结果不合并/);

  // 6) 迟到响应守卫：evaluators 的列表响应晚于页面切换到达时必须被丢弃。
  //    先放行 status 让 renderEvaluators 发出 listEvaluators 并挂起，
  //    再切换页面废弃 token。
  let resolveStatus;
  let resolveLate;
  api.status = () => new Promise(resolve => { resolveStatus = resolve; });
  api.listEvaluators = () => new Promise(resolve => { resolveLate = resolve; });
  const late = moduleApi.refresh(state('evaluators'));
  api.status = async () => { calls.push(['status']); return { enabled: true }; };
  resolveStatus({ enabled: true });
  // await 链解包需多个微任务轮次；宏任务冲刷确保 listEvaluators 已发出
  await new Promise(resolve => setImmediate(resolve));
  api.listEvaluators = async params => {
    calls.push(['listEvaluators']);
    listParamsHistory.push(params);
    return listResult;
  };
  await moduleApi.refresh(state('datasets'));
  resolveLate({ items: [
    { definition_id: 'kb.recall_at_5', version: 'v3', metric_name: 'kb.recall_at_5',
      target_kind: 'retrieval', method: 'deterministic',
      usage: { online_enabled: true, revision: 1 } },
  ], next_cursor: null });
  await late;
  assert.doesNotMatch(root.textContent, /会增加评估作业/, '迟到响应不得渲染前一个视图');
  assert.match(root.textContent, /暂无数据集/);

  // 7) 详情 modal + 复制新建（表单由能力目录生成；复制版本要求新版本号；
  //    提交前展示"自动使用默认关闭"；注册成功不显示"已就绪执行"）
  listResult = { items: [
    { definition_id: 'kb.recall_at_5', version: 'v1', metric_name: 'kb.recall_at_5',
      target_kind: 'retrieval', method: 'deterministic',
      usage: { online_enabled: false, revision: 1 } },
  ], next_cursor: null };
  await moduleApi.refresh(state('evaluators'));
  calls.length = 0;
  await findButton('详情').handlers.click();
  assert.ok(calls.some(c => c[0] === 'getEvaluator'), '详情须调用 getEvaluator');
  assert.match(body.textContent, /<script>unsafe<\/script>/);
  assert.ok(!createdTags.includes('script'), '安全渲染：不得创建 script 元素');
  assert.match(body.textContent, /未校准/, '未校准不得显示已校准');
  await findButton('基于此版本新建', body).handlers.click();
  const form = find(body, node => node.tag === 'form');
  assert.ok(form, '复制新建表单未打开');
  assert.match(body.textContent, /自动使用默认关闭/, '提交前须展示自动使用默认关闭');
  const versionInput = find(form, node => node.name === 'version');
  assert.equal(versionInput.value, '', '复制版本必须要求填写新版本号');
  createdPayloads = [];
  await form.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 0, '版本为空不得提交');
  assert.match(body.textContent, /新版本号/);
  versionInput.value = 'v1';
  await form.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 0, '复制版本不得使用旧版本号');
  assert.match(body.textContent, /新版本号/);
  versionInput.value = 'v2';
  await form.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(createdPayloads[0])), {
    definition_id: 'kb.recall_at_5', version: 'v2', metric_name: 'kb.recall_at_5',
    target_kind: 'retrieval', method: 'deterministic', output_type: 'number',
  });
  assert.match(alerts.at(-1), /已注册/);
  assert.ok(!alerts.join('\n').includes('已就绪执行'), '注册成功不得显示"已就绪执行"');
  assert.equal(body.children.length, 0, '注册成功后弹窗关闭');

  // 8) 创建 409：保留输入、提示刷新，不自动覆盖
  await moduleApi.refresh(state('evaluators'));
  await findButton('详情').handlers.click();
  await findButton('基于此版本新建', body).handlers.click();
  const conflictForm = find(body, node => node.tag === 'form');
  createdPayloads = [];
  createBehavior = 'conflict';
  const conflictVersion = find(conflictForm, node => node.name === 'version');
  conflictVersion.value = 'v2';
  await conflictForm.handlers.submit({ preventDefault() {} });
  createBehavior = 'ok';
  assert.equal(createdPayloads.length, 1);
  assert.equal(conflictVersion.value, 'v2', '409 后输入必须保留');
  assert.match(body.textContent, /409|冲突/);
  assert.match(body.textContent, /刷新|重新加载/);
  assert.ok(find(conflictForm, node => node.type === 'submit'), '409 后表单仍在，不自动覆盖');
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  // 9) 非复制新建表单同样由能力目录生成（方法字段来自目录）
  calls.length = 0;
  await findButton('新建评估器').handlers.click();
  const freshForm = find(body, node => node.tag === 'form');
  assert.ok(freshForm, '新建表单未打开');
  assert.ok(find(freshForm, node => node.name === 'method'), '创建表单须包含方法字段');
  assert.equal(find(freshForm, node => node.name === 'definition_id').value, '');
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  // 10) llm_judge 表单：切换 method 触发条件字段（judge_model/rubric/
  //     temperature 出现）；temperature NaN 与越界（0-2 之外）走 errorLine
  //     不发请求；必填 judge_model 缺失不发请求；合法值正常提交
  await moduleApi.refresh(state('evaluators'));
  calls.length = 0;
  createdPayloads = [];
  await findButton('新建评估器').handlers.click();
  const judgeForm = find(body, node => node.tag === 'form');
  assert.ok(judgeForm, '新建表单未打开');
  const judgeMethodSelect = find(judgeForm, node => node.name === 'method');
  assert.ok(judgeMethodSelect && judgeMethodSelect.handlers.change, '方法字段须有 change 处理');
  judgeMethodSelect.value = 'llm_judge';
  judgeMethodSelect.handlers.change();
  for (const name of ['judge_model', 'rubric', 'temperature']) {
    assert.ok(find(judgeForm, node => node.name === name), 'llm_judge 缺少字段: ' + name);
  }
  find(judgeForm, node => node.name === 'definition_id').value = 'judge.metric';
  find(judgeForm, node => node.name === 'version').value = 'v1';
  find(judgeForm, node => node.name === 'judge_model').value = 'gpt-x';
  find(judgeForm, node => node.name === 'rubric').value = '按事实核对';
  const tempInput = find(judgeForm, node => node.name === 'temperature');
  tempInput.value = 'abc';
  await judgeForm.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 0, 'temperature NaN 不得提交');
  assert.match(body.textContent, /必须是有效数字（0-2）/);
  tempInput.value = '3';
  await judgeForm.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 0, 'temperature 越界（>2）不得提交');
  assert.match(body.textContent, /必须是有效数字（0-2）/);
  tempInput.value = '1';
  find(judgeForm, node => node.name === 'judge_model').value = '';
  await judgeForm.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 0, 'judge_model 缺失不得提交');
  assert.match(body.textContent, /Judge 模型（judge_model）/);
  find(judgeForm, node => node.name === 'judge_model').value = 'gpt-x';
  await judgeForm.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 1, '合法 llm_judge 提交应发出请求');
  assert.deepEqual(JSON.parse(JSON.stringify(createdPayloads[0])), {
    definition_id: 'judge.metric', version: 'v1',
    metric_name: 'agent.answer_relevance', target_kind: 'agent_run',
    method: 'llm_judge', output_type: 'number',
    judge_model: 'gpt-x', judge_params: { rubric: '按事实核对', temperature: 1 },
  });
  assert.equal(body.children.length, 0, '注册成功后弹窗关闭');

  // 11) 能力目录为空：提示能力目录为空，不再凭空生成目录外方法选项
  await moduleApi.refresh(state('evaluators'));
  calls.length = 0;
  const realCaps = api.getCapabilities;
  api.getCapabilities = async () => ({ entries: [], judge_configured: false });
  alerts.length = 0;
  await findButton('新建评估器').handlers.click();
  api.getCapabilities = realCaps;
  assert.match(alerts.join('\n'), /能力目录为空/);
  assert.ok(!find(body, node => node.tag === 'form'), '目录为空不得打开创建表单');

  // -------------------------------------------------------------------------
  // Part C：annotations 视图（Task 12/spec 4.4 人工标注）
  // -------------------------------------------------------------------------
  const baseQueueItem = {
    source_result_id: 'res-ann-1', created_at: '2026-11-12T00:00:00+00:00',
    provenance: 'automatic', metric_name: 'agent.answer_relevance', status: 'value',
    typed_value: 0.2, evaluated_at: '', target_service: 'n-agent',
    target_kind: 'agent_run', target_subject_id: 'run-1',
    target_subject_revision: 'ok', definition_id: 'agent.answer_relevance',
    definition_version: 'v1', output_type: 'number', latest_human_feedback: null,
  };
  let exportBehavior = 'ok';
  let exportResult = { dataset_id: 'ds-rev', dataset_version: 'v3', dataset_hash: 'hash-xyz', case_count: 2, audit_id: 'audit-9' };
  const exportCalls = [];
  api.exportReviewQueue = async body => {
    calls.push(['exportReviewQueue']);
    exportCalls.push(body);
    if (exportBehavior === 'conflict') throw new Error('conflict');
    if (exportBehavior === 'identity_leak') throw new Error('identity_leak');
    return exportResult;
  };
  const annParams = () => JSON.parse(JSON.stringify(annParamsHistory.at(-1)));

  // C1) 空队列占位 + 默认筛选参数（state=all/reason=low_score/threshold=0.5）
  await moduleApi.refresh(state('annotations'));
  assert.match(root.textContent, /当前筛选暂无待标注结果/);
  assert.deepEqual(annParams(), { state: 'all', reason: 'low_score', threshold: '0.5', limit: 50 });

  // C2) 筛选 tabs：全部/待标注/已标注 + 低分/争议/异常
  annParamsHistory.length = 0;
  await findButton('待标注').handlers.click();
  assert.deepEqual(annParams(), { state: 'pending', reason: 'low_score', threshold: '0.5', limit: 50 });
  await findButton('争议').handlers.click();
  assert.equal(annParams().reason, 'disputed');
  await findButton('已标注').handlers.click();
  assert.equal(annParams().state, 'annotated');
  await findButton('全部').handlers.click();
  await findButton('低分').handlers.click();
  assert.deepEqual(annParams(), { state: 'all', reason: 'low_score', threshold: '0.5', limit: 50 });

  // C3) 列表渲染 + 详情：调用 getAnnotation，并排展示评分契约/原自动值/
  //     当前人工值/修订号；可提交条目提供评分值输入与可用提交按钮
  annQueue = { items: [baseQueueItem], next_cursor: null };
  calls.length = 0;
  await moduleApi.refresh(state('annotations'));
  assert.match(root.textContent, /res-ann-1/);
  assert.match(root.textContent, /0\.2/);
  calls.length = 0;
  await findButton('详情').handlers.click();
  assert.ok(calls.some(c => c[0] === 'getAnnotation' && c[1] === 'res-ann-1'), '详情须调用 getAnnotation');
  assert.match(body.textContent, /agent\.answer_relevance/);
  assert.match(body.textContent, /按事实核对回答相关性/, '评分契约 rubric 摘要须展示');
  assert.match(body.textContent, /0\.2/, '详情须展示原自动值');
  assert.match(body.textContent, /修订号/);
  assert.match(body.textContent, /未标注/, '无人工值时显示未标注');
  // verify 3.1（修复项 3/4 配套）：上下文携带脱敏证据正文时详情并排
  // 展示请求/回答，不补造、不省略
  assert.match(body.textContent, /证据（请求 \/ 回答）/, '详情须提供证据并排展示区');
  assert.match(body.textContent, /如何重置密码/, '详情须展示请求证据正文');
  assert.match(body.textContent, /在设置页点击重置/, '详情须展示回答证据正文');
  const detailForm = find(body, node => node.tag === 'form');
  assert.ok(find(detailForm, node => node.name === 'annotation_value'), '可标注条目须提供评分值输入');
  assert.ok(!find(detailForm, node => node.type === 'submit').disabled, '可标注条目提交按钮可用');
  assert.ok(!find(detailForm, node => node.tag === 'textarea'), '人工标注详情不得出现自由文本 textarea');
  assert.ok(!find(detailForm, node => node.name === 'comment'), '不得引入评论字段');

  // C4) 提交流程：expected_revision 取上下文 current_revision；同 payload
  //     重试复用同 request_key、修改内容换新 key；409 保留输入提示显式
  //     重载不自动覆盖；成功后保留筛选并刷新列表
  calls.length = 0;
  submitCalls.length = 0;
  submitBehavior = 'error';
  const valueInput = find(detailForm, node => node.name === 'annotation_value');
  valueInput.value = '0.42';
  await detailForm.handlers.submit({ preventDefault() {} });
  assert.equal(submitCalls.length, 1);
  assert.equal(submitCalls[0][0], 'res-ann-1');
  assert.deepEqual(JSON.parse(JSON.stringify(submitCalls[0][1])),
    { value: 0.42, request_key: submitCalls[0][1].request_key, expected_revision: 0 },
    '首次提交 expected_revision 取上下文 current_revision');
  const firstKey = submitCalls[0][1].request_key;
  assert.ok(firstKey, '提交必须携带 request_key');
  await detailForm.handlers.submit({ preventDefault() {} });
  assert.equal(submitCalls.length, 2);
  assert.equal(submitCalls[1][1].request_key, firstKey, '同 payload 重试必须复用同 request_key');
  valueInput.value = '0.5';
  await detailForm.handlers.submit({ preventDefault() {} });
  assert.equal(submitCalls.length, 3);
  assert.notEqual(submitCalls[2][1].request_key, firstKey, '修改内容必须换新 request_key');
  submitBehavior = 'conflict';
  await detailForm.handlers.submit({ preventDefault() {} });
  assert.equal(submitCalls.length, 4, '409 不得自动重试');
  assert.equal(valueInput.value, '0.5', '409 后用户输入必须保留');
  assert.match(body.textContent, /409|冲突/);
  assert.match(body.textContent, /重载|重新打开|刷新/);
  assert.ok(find(detailForm, node => node.type === 'submit'), '409 后表单仍在，不自动覆盖');
  submitBehavior = 'ok';
  await detailForm.handlers.submit({ preventDefault() {} });
  assert.deepEqual(JSON.parse(JSON.stringify(submitCalls[3][1])),
    { value: 0.5, request_key: submitCalls[2][1].request_key, expected_revision: 0 },
    '409 后同 payload 重试复用同 key 且 expected_revision 不自动递增');
  assert.match(alerts.at(-1), /已保存/);
  assert.equal(body.children.length, 0, '保存成功后弹窗关闭');
  assert.ok(calls.some(c => c[0] === 'listAnnotations'), '保存成功后刷新列表');
  assert.deepEqual(annParams(), { state: 'all', reason: 'low_score', threshold: '0.5', limit: 50 },
    '保存成功保留筛选');

  // C5) 不支持标注的目标类型：显示"暂不支持在此标注"且提交按钮禁用；
  //     证据过期显示原因文案，不补造正文
  annContext.can_submit = false;
  annContext.reject_reason = 'target_kind_not_supported';
  await moduleApi.refresh(state('annotations'));
  await findButton('详情').handlers.click();
  assert.match(body.textContent, /暂不支持在此标注/);
  const unsupportedForm = find(body, node => node.tag === 'form');
  assert.ok(find(unsupportedForm, node => node.type === 'submit'), '不支持类型仍展示提交按钮');
  assert.ok(find(unsupportedForm, node => node.type === 'submit').disabled, '不支持类型提交按钮禁用');
  assert.ok(!find(unsupportedForm, node => node.name === 'annotation_value'), '不可提交时不提供评分值输入');
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  annContext.reject_reason = 'evidence_unavailable';
  annContext.evidence_status = 'unavailable';
  annContext.evidence = null;
  await moduleApi.refresh(state('annotations'));
  await findButton('详情').handlers.click();
  assert.match(body.textContent, /证据不可用/);
  assert.match(body.textContent, /不补造正文/, '证据过期说明原因，不补造正文');
  assert.doesNotMatch(body.textContent, /如何重置密码/, '证据不可用不得展示或补造正文');
  const evidenceForm = find(body, node => node.tag === 'form');
  assert.ok(find(evidenceForm, node => node.type === 'submit').disabled, '证据不可用提交按钮禁用');
  assert.ok(!find(body, node => node.tag === 'textarea'), '证据不可用详情同样无自由文本输入');
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  // C6) 导出集成：逐 case 金标表单 + 版本冲突/身份键/目录越界保护；
  //     成功后展示 dataset_id/version/hash/audit_id 并可进入数据集列表
  annContext.can_submit = true;
  annContext.reject_reason = null;
  annContext.evidence_status = 'available';
  annContext.evidence = { request: '如何重置密码', answer: '在设置页点击重置' };
  annQueue = { items: [
    baseQueueItem,
    Object.assign({}, baseQueueItem, { source_result_id: 'res-ann-2' }),
  ], next_cursor: null };
  await moduleApi.refresh(state('annotations'));
  calls.length = 0;
  await findButton('审核后导出数据集').handlers.click();
  // openExportModal 异步取证据 query（标注队列行不带 query）；冲刷微任务
  await new Promise(resolve => setImmediate(resolve));
  const exportForm = find(body, node => node.tag === 'form');
  assert.ok(exportForm, '导出弹窗未打开');
  find(exportForm, node => node.name === 'dataset_id').value = 'ds-rev';
  find(exportForm, node => node.name === 'dataset_version').value = 'v3';
  find(exportForm, node => node.name === 'approved_by').value = 'reviewer';
  findAll(exportForm, node => node.name && node.name.indexOf('expected_output_') === 0)
    .forEach(inp => { inp.value = 'gold'; });
  exportBehavior = 'conflict';
  await exportForm.handlers.submit({ preventDefault() {} });
  assert.match(body.textContent, /409|已导出/);
  assert.match(body.textContent, /版本/);
  exportBehavior = 'identity_leak';
  await exportForm.handlers.submit({ preventDefault() {} });
  assert.match(body.textContent, /身份键/);
  exportBehavior = 'ok';
  await exportForm.handlers.submit({ preventDefault() {} });
  assert.match(alerts.at(-1), /ds-rev/);
  assert.match(alerts.at(-1), /v3/);
  assert.match(alerts.at(-1), /hash-xyz/, '导出完成须展示 dataset hash');
  assert.match(alerts.at(-1), /audit-9/, '导出完成须展示 audit_id');
  assert.equal(body.children.length, 0, '导出成功后弹窗关闭');
  // C6a（Task 12 审查修复项 1）：标注队列行只有 source_result_id（无
  // result_id），导出载荷 source_result_ids 必须逐项取到非空字符串 id，
  // 否则真实后端按非空字符串数组校验将 422 export_invalid
  const exportPayload = exportCalls.at(-1);
  assert.ok(Array.isArray(exportPayload.source_result_ids), '导出载荷必须携带 source_result_ids 数组');
  // 跨 vm realm 数组原型不同，先 JSON 归一再比较（与本文件既有模式一致）
  assert.deepEqual(JSON.parse(JSON.stringify(exportPayload.source_result_ids)),
    ['res-ann-1', 'res-ann-2'],
    '标注队列导出载荷 id 必须取 source_result_id（复核队列行为 result_id）');
  // C6b（修复项 2）：case input/fixture 的 query 必须来自标注上下文的
  // 受控证据投影（evidence.request），不得恒为空串
  assert.ok(exportPayload.cases.length >= 1, '导出载荷必须携带 cases');
  for (const c of exportPayload.cases) {
    assert.equal(c.input.query, '如何重置密码', '导出 case input.query 须取证据 request 正文');
    assert.equal(c.fixture_content.query, '如何重置密码', '导出 fixture query 须取证据 request 正文');
  }
  await findButton('查看数据集列表').handlers.click();
  assert.match(root.textContent, /暂无数据集/, '导出后可进入数据集列表');

  // C7) 目录越界保护：超过 20 条时明确截断提示
  annQueue = { items: Array.from({ length: 25 }, (_, i) =>
    Object.assign({}, baseQueueItem, { source_result_id: 'res-' + i })), next_cursor: null };
  await moduleApi.refresh(state('annotations'));
  await findButton('审核后导出数据集').handlers.click();
  assert.match(body.textContent, /仅导出前 20 条/);
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  // -------------------------------------------------------------------------
  // Part D（Task 12 审查修复项 1/2）：导出弹窗异步防护 + 空队列不渲染导出入口
  // -------------------------------------------------------------------------
  const realGetAnnotation = api.getAnnotation;
  const pendingContexts = [];
  api.getAnnotation = () => new Promise(resolve => { pendingContexts.push(resolve); });

  // D1（修复项 2）：空标注队列 early-return——不渲染导出按钮与数据集入口
  annQueue = { items: [], next_cursor: null };
  await moduleApi.refresh(state('annotations'));
  assert.match(root.textContent, /当前筛选暂无待标注结果/);
  assert.ok(!findButton('审核后导出数据集'), '空队列不得渲染导出按钮');
  assert.ok(!findButton('查看数据集列表'), '空队列不得渲染数据集入口');

  // D2（修复项 1a）：上下文挂起期间切换视图 → 迟到的导出弹窗不得出现
  annQueue = { items: [baseQueueItem], next_cursor: null };
  await moduleApi.refresh(state('annotations'));
  findButton('审核后导出数据集').handlers.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pendingContexts.length, 1, '导出取上下文已挂起');
  await moduleApi.refresh(state('datasets'));
  pendingContexts.splice(0).forEach(resolve => resolve(annContext));
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!find(body, node => node.tag === 'form'), '切换视图后迟到的导出弹窗不得出现');

  // D3（修复项 1b）：加载窗口内重复点击 → 仅一个导出弹窗、不重复取数
  await moduleApi.refresh(state('annotations'));
  const exportBtn = findButton('审核后导出数据集');
  exportBtn.handlers.click();
  exportBtn.handlers.click();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(pendingContexts.length, 1, '加载窗口内重复点击不得重复发起上下文取数');
  pendingContexts.splice(0).forEach(resolve => resolve(annContext));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(findAll(body, node => node.tag === 'form').length, 1,
    '加载窗口内重复点击只允许一个导出弹窗');
  api.getAnnotation = realGetAnnotation;
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  // -------------------------------------------------------------------------
  // Part E（Task 13/spec 4.5）：概览趋势卡 + 结果筛选游标分页 + review 链接
  // -------------------------------------------------------------------------
  const flush = () => new Promise(resolve => setImmediate(resolve));
  const analyticsParamsHistory = [];
  const resultsParamsHistory = [];
  const reviewParamsHistory = [];
  let analyticsBehavior = 'ok';
  let analyticsResult = null;
  let resultsPage = { items: [], next_cursor: null };
  let resultsBehavior = 'ok';
  let reviewQueuePage = { items: [], next_cursor: null };
  api.getConfig = async () => {
    calls.push(['getConfig']);
    return {
      config: { evaluation_enabled: true, evaluation_sampling_rate: '0.5', evaluation_worker_concurrency: 2 },
      version: 3, updated_at: '2026-11-12T00:00:00+00:00',
    };
  };
  api.overview = async () => {
    calls.push(['overview']);
    return { capacity: { probe_available: false }, budget: {}, evidence_retention_days: 7, experiment_results: {}, producer_states: [] };
  };
  api.getAnalytics = async params => {
    calls.push(['getAnalytics']);
    analyticsParamsHistory.push(params);
    if (analyticsBehavior === 'error') throw new Error('request_failed');
    return analyticsResult;
  };
  api.listResults = async params => {
    calls.push(['listResults']);
    resultsParamsHistory.push(params);
    if (params.cursor && resultsBehavior === 'cursor_invalid') throw new Error('cursor_invalid');
    return resultsPage;
  };
  api.listReviewQueue = async params => {
    calls.push(['listReviewQueue']);
    reviewParamsHistory.push(params);
    return reviewQueuePage;
  };
  // 实验视图 mock：Part E1 概览 Promise.all 依赖 listExperiments，
  // Part F 对比流程依赖 getExperiment/listExperiments，两者共用此定义；
  // experimentDetail / experimentsList 由各 Part 按场景覆写
  let experimentDetail = null;
  let experimentsList = { items: [], next_cursor: null };
  api.getExperiment = async id => {
    calls.push(['getExperiment', id]);
    return experimentDetail;
  };
  api.listExperiments = async () => {
    calls.push(['listExperiments']);
    return experimentsList;
  };

  // E1) 概览趋势卡：调用 getAnalytics，展示指标/版本/来源/样本数/时间窗；
  //     无值分组显示"暂无有效评分"，不渲染 0% 成功/100% 质量
  analyticsResult = {
    snapshot_at: '2026-11-12T02:00:00+00:00',
    window: { from: '2026-11-05T00:00:00+00:00', to: '2026-11-12T02:00:00+00:00' },
    buckets: [{ date: '2026-11-05', total: 2 }],
    groups: [
      { definition_id: 'agent.answer_relevance', definition_version: 'v1', provenance: 'automatic',
        output_type: 'number', total_count: 5, count: 0, mean: null, min: null, max: null,
        status_counts: { value: 3, skipped: 1, error: 1 }, daily_counts: {} },
      { definition_id: 'kb.recall_at_5', definition_version: 'v2', provenance: 'automatic',
        output_type: 'boolean', total_count: 4, true_count: 3, false_count: 1, true_ratio: 0.75,
        status_counts: { value: 4, skipped: 0, error: 0 }, daily_counts: {} },
    ],
    coverage: { status: 'unknown', reason: '缺少应评估目标总数' },
  };
  await moduleApi.refresh(state('overview'));
  assert.ok(calls.some(c => c[0] === 'getAnalytics'), '趋势卡须调用 getAnalytics');
  assert.match(root.textContent, /评分趋势/);
  assert.match(root.textContent, /统计时间窗/);
  assert.match(root.textContent, /2026-11-05/, '时间窗须展示');
  assert.match(root.textContent, /agent\.answer_relevance/, '指标须展示');
  assert.match(root.textContent, /automatic/, '来源须展示');
  assert.match(root.textContent, /缺少应评估目标总数/, 'coverage unknown 须解释原因');
  assert.match(root.textContent, /暂无有效评分/, '无值 number 分组须显示暂无有效评分');
  assert.match(root.textContent, /75%/, 'boolean 有值分组展示 true_ratio');
  assert.doesNotMatch(root.textContent, /0% 成功/, '不得把无值渲染成 0% 成功');
  assert.doesNotMatch(root.textContent, /100% 质量/, '不得把无值渲染成 100% 质量');

  // E1b) 分析分组为空：显示"暂无有效评分"，不显示 0%
  analyticsResult = {
    snapshot_at: '2026-11-12T02:00:00+00:00',
    window: { from: '2026-11-05T00:00:00+00:00', to: '2026-11-12T02:00:00+00:00' },
    buckets: [{ date: '2026-11-05', total: 0 }],
    groups: [],
    coverage: { status: 'unknown', reason: '缺少应评估目标总数' },
  };
  await moduleApi.refresh(state('overview'));
  assert.match(root.textContent, /暂无有效评分/);
  assert.doesNotMatch(root.textContent, /0%/);

  // E2) 结果筛选 + 游标分页：首页无游标无上一页；下一页携带 next_cursor；
  //     上一页从历史游标栈回退；过滤变更重置游标；"暂无翻页"提示移除
  //     （注意：响应在每次渲染时取数，翻页按钮捕获当次响应的 next_cursor，
  //     mock 需在点击前更新）
  const resultRow = {
    result_id: 'res-1', metric_name: 'kb.recall_at_5', provenance: 'automatic',
    status: 'value', passed: true, typed_value: 0.8, created_at: '2026-11-12T00:00:00+00:00',
  };
  resultsPage = { items: [resultRow], next_cursor: 'tok2' };
  await moduleApi.refresh(state('results'));
  assert.deepEqual(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))), { limit: 50 },
    '首页不得携带游标或过滤');
  assert.match(root.textContent, /res-1/);
  assert.doesNotMatch(root.textContent, /暂无翻页/, '固定 50 条"暂无翻页"提示须移除');
  assert.ok(!findButton('上一页', root), '首页不得渲染上一页');
  assert.ok(findButton('下一页', root), '有 next_cursor 须渲染下一页');
  resultsPage = { items: [resultRow], next_cursor: 'tok3' };
  await findButton('下一页', root).handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, 'tok2', '下一页携带游标');
  resultsPage = { items: [resultRow], next_cursor: null };
  await findButton('下一页', root).handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, 'tok3');
  assert.ok(!findButton('下一页', root), '末页无下一页');
  await findButton('上一页', root).handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, 'tok2', '上一页从游标栈回退');
  await findButton('上一页', root).handlers.click();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))), { limit: 50 }, '回到首页不携带游标');
  assert.ok(!findButton('上一页', root), '首页无上一页');

  // E2b) 过滤维度（时间/目标类型/DefinitionKey/provenance/状态）进入查询；
  //      过滤变更重置游标；跨渲染保留用户选择
  resultsPage = { items: [resultRow], next_cursor: 'tok2' };
  await moduleApi.refresh(state('results'));
  await findButton('下一页', root).handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, 'tok2');
  resultsPage = { items: [resultRow], next_cursor: null };
  const fromInput = find(root, node => node.name === 'results_created_from' && node.handlers.change);
  assert.ok(fromInput, '缺少开始时间筛选控件');
  fromInput.value = '2026-11-01T00:00:00+00:00';
  await fromInput.handlers.change();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))),
    { limit: 50, created_from: '2026-11-01T00:00:00+00:00' }, '过滤变更重置游标');
  const toInput = find(root, node => node.name === 'results_created_to' && node.handlers.change);
  assert.ok(toInput, '缺少结束时间筛选控件');
  toInput.value = '2026-11-12T00:00:00+00:00';
  await toInput.handlers.change();
  await flush();
  const kindFilter = find(root, node => node.name === 'results_target_kind' && node.handlers.change);
  assert.ok(kindFilter, '缺少目标类型筛选控件');
  kindFilter.value = 'agent_run';
  await kindFilter.handlers.change();
  await flush();
  const defFilter = find(root, node => node.name === 'results_definition_id' && node.handlers.change);
  assert.ok(defFilter, '缺少 DefinitionKey 筛选控件');
  defFilter.value = 'kb.recall_at_5';
  await defFilter.handlers.change();
  await flush();
  const provFilter = find(root, node => node.name === 'results_provenance' && node.handlers.change);
  assert.ok(provFilter, '缺少 provenance 筛选控件');
  provFilter.value = 'automatic';
  await provFilter.handlers.change();
  await flush();
  const statusFilter = find(root, node => node.name === 'results_status' && node.handlers.change);
  assert.ok(statusFilter, '缺少状态筛选控件');
  statusFilter.value = 'value';
  await statusFilter.handlers.change();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))), {
    limit: 50,
    created_from: '2026-11-01T00:00:00+00:00',
    created_to: '2026-11-12T00:00:00+00:00',
    target_kind: 'agent_run',
    definition_id: 'kb.recall_at_5',
    provenance: 'automatic',
    status: 'value',
  }, '全部过滤维度进入查询且跨渲染保留');

  // E2c) 游标失效（422 cursor_invalid）：提示游标失效并提供重新查询
  //      （重置游标、保留过滤），不静默重试
  resultsPage = { items: [resultRow], next_cursor: 'tok2' };
  await moduleApi.refresh(state('results'));
  resultsBehavior = 'cursor_invalid';
  await findButton('下一页', root).handlers.click();
  await flush();
  resultsBehavior = 'ok';
  assert.match(root.textContent, /游标已失效|游标失效/);
  assert.match(root.textContent, /重新查询/);
  await findButton('重新查询', root).handlers.click();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))), {
    limit: 50,
    created_from: '2026-11-01T00:00:00+00:00',
    created_to: '2026-11-12T00:00:00+00:00',
    target_kind: 'agent_run',
    definition_id: 'kb.recall_at_5',
    provenance: 'automatic',
    status: 'value',
  }, '重新查询保留过滤并重置游标');

  // E2d) 空第二页可回退：items 为空仍渲染"上一页"，点击后回到首页
  resultsPage = { items: [resultRow], next_cursor: 'tok2' };
  await moduleApi.refresh(state('results'));
  resultsPage = { items: [], next_cursor: null };
  await findButton('下一页', root).handlers.click();
  await flush();
  assert.match(root.textContent, /当前分页无结果，请返回上一页/, '空第二页展示分页空态');
  const prevOnEmpty = findButton('上一页', root);
  assert.ok(prevOnEmpty, '空第二页仍须渲染上一页按钮可回退');
  resultsPage = { items: [resultRow], next_cursor: 'tok2' };
  await prevOnEmpty.handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, undefined,
    '空页上一页回退到首页不携带游标');
  assert.match(root.textContent, /res-1/, '回退后重新渲染结果列表');

  // E3) review 兼容视图：含"前往人工标注"链接（href /evaluations/annotations）
  reviewQueuePage = {
    items: [{ result_id: 'res-rev-1', metric_name: 'kb.recall_at_5', provenance: 'automatic',
      query: 'q', typed_value: 0.1, created_at: '2026-11-12T00:00:00+00:00' }],
    next_cursor: null,
  };
  await moduleApi.refresh(state('review'));
  assert.ok(calls.some(c => c[0] === 'listReviewQueue'));
  const annotateLink = find(root, node => node.tag === 'a' && node.href === '/evaluations/annotations');
  assert.ok(annotateLink, 'review 兼容视图须提供前往人工标注链接');
  assert.equal(annotateLink.textContent, '前往人工标注');

  // -------------------------------------------------------------------------
  // Part F（Task 13/spec 4.6）：实验对比入口 + 基线选择 + 可选策略 JSON +
  // 描述性/四态结论渲染 + 不可比 422 差异维度展示
  // -------------------------------------------------------------------------
  const experimentDetailFor = (id, datasetId, version) => ({
    experiment: { experiment_id: id, dataset_id: datasetId, dataset_version: version,
      mode: 'replay', status: 'completed', created_at: '2026-11-12T00:00:00+00:00' },
    coverage: { results_total: 4, value_results: 4, error_or_skipped_results: 0, scan_truncated: false },
    distribution: {}, delivery_status: {}, baseline: null, gate_report_ref: null,
  });
  experimentDetail = experimentDetailFor('exp-1', 'ds-1', 'v1');
  experimentsList = { items: [
    { experiment_id: 'exp-1', dataset_id: 'ds-1', dataset_version: 'v1', mode: 'replay', status: 'completed', created_at: '' },
    { experiment_id: 'exp-2', dataset_id: 'ds-1', dataset_version: 'v1', mode: 'replay', status: 'completed', created_at: '' },
    { experiment_id: 'exp-3', dataset_id: 'ds-2', dataset_version: 'v1', mode: 'replay', status: 'completed', created_at: '' },
  ], next_cursor: null };
  // getExperiment / listExperiments mock 已在 Part E 统一定义（Part E1 与
  // Part F 共同依赖），此处仅覆写返回数据，不重复绑定
  const compareCalls = [];
  let compareResult = null;
  let compareBehavior = 'ok';
  api.compareExperiments = async payload => {
    calls.push(['compareExperiments']);
    compareCalls.push(payload);
    if (compareBehavior === 'incomparable') {
      const err = new Error('incomparable_experiments');
      err.data = { error: { code: 'incomparable_experiments', diff_dimensions: {
        dataset_id: true, dataset_version: false, dataset_hash: true } } };
      throw err;
    }
    return compareResult;
  };

  await moduleApi.refresh(state('experiments'));
  assert.match(root.textContent, /exp-1/);
  calls.length = 0;
  await findButton('详情', root).handlers.click();
  await flush();
  assert.ok(calls.some(c => c[0] === 'getExperiment' && c[1] === 'exp-1'), '详情须调用 getExperiment');
  const compareEntry = findButton('与基线对比', body);
  assert.ok(compareEntry, '实验详情须提供与基线对比入口');
  await compareEntry.handlers.click();
  await flush();
  const compareForm = find(body, node => node.tag === 'form');
  assert.ok(compareForm, '对比弹窗未打开');
  const baselineOptions = findAll(compareForm, node => node.tag === 'option').map(o => o.value);
  assert.deepEqual(JSON.parse(JSON.stringify(baselineOptions)), ['exp-2'],
    '基线选项仅同 dataset_id 同版本且排除自身');
  const policyInput = find(compareForm, node => node.name === 'compare_policy');
  assert.ok(policyInput, '对比弹窗须提供可选策略 JSON 输入');

  // F1) 非法策略 JSON：提示且不发请求
  policyInput.value = '{bad json';
  await compareForm.handlers.submit({ preventDefault() {} });
  await flush();
  assert.equal(compareCalls.length, 0, '非法策略 JSON 不得发请求');
  assert.match(body.textContent, /JSON 格式不正确/);

  // F2) 无策略描述性对比：渲染配对指标/差值/分母依据，不渲染门禁结论
  policyInput.value = '';
  const descResult = {
    evidence_basis: 'ledger_reconstruction', denominator_basis: 'observed',
    candidate: { experiment_id: 'exp-1', dataset_id: 'ds-1', dataset_version: 'v1', dataset_hash: 'h1' },
    baseline: { experiment_id: 'exp-2', dataset_id: 'ds-1', dataset_version: 'v1', dataset_hash: 'h1' },
    metrics: [{ metric_name: 'kb.recall_at_5',
      candidate: { value_count: 3, macro_value: 0.6, denominator: 4, missing_count: 1, error_count: 0 },
      baseline: { value_count: 3, macro_value: 0.5, denominator: 4, missing_count: 1, error_count: 0 },
      paired_cases: 2, diff: 0.1, paired_ci_lower: 0.02 }],
    delivery_status_counts: {},
  };
  compareResult = descResult;
  await compareForm.handlers.submit({ preventDefault() {} });
  await flush();
  assert.equal(compareCalls.length, 1);
  assert.deepEqual(JSON.parse(JSON.stringify(compareCalls[0])),
    { candidate_id: 'exp-1', baseline_id: 'exp-2' }, '无策略时载荷不携带 policy');
  assert.match(body.textContent, /kb\.recall_at_5/, '描述性对比须展示配对指标');
  assert.match(body.textContent, /0\.1/, '描述性对比须展示配对差值');
  assert.match(body.textContent, /observed/, '须展示分母依据');
  assert.doesNotMatch(body.textContent, /gate_pass|gate_fail|质量通过|质量不通过（/,
    '无策略不得渲染四态门禁结论');

  // F3) 有策略：渲染四态结论/原因/策略指纹
  policyInput.value = '{"schema_version":"1","thresholds":{}}';
  compareResult = Object.assign({}, descResult, {
    policy_fingerprint: 'fp-1',
    conclusion: { state: 'gate_pass', reasons: ['coverage:ok'], metrics: [],
      policy_version: '1', insufficient_data: false, experiment_id: 'exp-1',
      baseline_experiment_id: 'exp-2', delivery_status_counts: {}, denominator_basis: 'observed' },
  });
  await compareForm.handlers.submit({ preventDefault() {} });
  await flush();
  assert.equal(compareCalls.length, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(compareCalls[1].policy)),
    { schema_version: '1', thresholds: {} }, '合法策略 JSON 随载荷提交');
  assert.match(body.textContent, /gate_pass/, '有策略须渲染四态结论');
  assert.match(body.textContent, /coverage:ok/, '结论原因须展示');
  assert.match(body.textContent, /fp-1/, '策略指纹须展示');

  // F4) 不可比 422：展示差异维度（dataset_id/version/hash）并保留输入
  compareBehavior = 'incomparable';
  await compareForm.handlers.submit({ preventDefault() {} });
  await flush();
  compareBehavior = 'ok';
  assert.match(body.textContent, /不可比/);
  assert.match(body.textContent, /dataset_id/, '差异维度须逐项展示');
  assert.match(body.textContent, /dataset_version/, '差异维度须逐项展示');
  assert.match(body.textContent, /dataset_hash/, '差异维度须逐项展示');
  assert.equal(find(compareForm, node => node.name === 'compare_policy').value,
    '{"schema_version":"1","thresholds":{}}', '422 后策略输入保留');
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  // -------------------------------------------------------------------------
  // Part G：FE 规范共享 helper 行为合同（spec-261112 T1）
  // -------------------------------------------------------------------------
  // G0) Element 属性桩可用
  const probe = new Element('button');
  probe.setAttribute('aria-pressed', 'true');
  assert.equal(probe.getAttribute('aria-pressed'), 'true');
  assert.equal(probe.getAttribute('missing-attr'), null);

  // 独立 vm 上下文：仅替换模块导出点暴露私有 helper，不修改生产公开接口；
  // 尚未存在的函数以 typeof 守卫暴露 undefined，避免 ReferenceError 冒充 RED。
  const exportMarker = 'namespace.evaluations = { init: init, refresh: refresh, activate: init, deactivate: deactivate };';
  const rawHelperSource = fs.readFileSync('app/interfaces/http/static/evaluation.js', 'utf8');
  assert.ok(rawHelperSource.includes(exportMarker), '模块导出点须保持可替换');
  const helperSource = rawHelperSource.replace(exportMarker, `namespace.evaluations = {
      init: init, refresh: refresh, activate: init, deactivate: deactivate,
      formatNumber: typeof formatNumber === 'function' ? formatNumber : undefined,
      table: typeof table === 'function' ? table : undefined,
      titleGroup: typeof titleGroup === 'function' ? titleGroup : undefined,
      sectionPanel: typeof sectionPanel === 'function' ? sectionPanel : undefined,
      showLoading: typeof showLoading === 'function' ? showLoading : undefined,
      disabledButton: typeof disabledButton === 'function' ? disabledButton : undefined,
      filterBar: typeof filterBar === 'function' ? filterBar : undefined,
      filterItem: typeof filterItem === 'function' ? filterItem : undefined,
      choiceGroup: typeof choiceGroup === 'function' ? choiceGroup : undefined,
      scrollTable: typeof scrollTable === 'function' ? scrollTable : undefined,
    };`);
  const helperRoot = new Element();
  const helperContext = {
    Node: Element,
    document: {
      getElementById: () => helperRoot,
      createElement: tag => new Element(tag),
      body: new Element(),
    },
    NAGENT: {
      api: { evaluations: new Proxy({}, { get: () => async () => ({}) }) },
      // 镜像生产 management-ui.js 的 ui.el（设置 className）
      ui: {
        el: (tag, className) => {
          const node = new Element(tag);
          if (className) node.className = className;
          return node;
        },
      },
    },
  };
  helperContext.window = helperContext;
  vm.runInNewContext(helperSource, helperContext);
  const helpers = helperContext.NAGENT.evaluations;

  // G0b) 共享 helper 存在性
  for (const name of ['formatNumber', 'table', 'titleGroup', 'sectionPanel', 'showLoading',
    'disabledButton', 'filterBar', 'filterItem', 'choiceGroup', 'scrollTable']) {
    assert.equal(typeof helpers[name], 'function', '缺少共享 helper: ' + name);
  }

  // G1-helper) formatNumber：千分位/小数精度/假零拒绝/完整边界
  assert.equal(helpers.formatNumber(0), '0');
  assert.equal(helpers.formatNumber(1234), '1,234', '整数不补小数');
  assert.equal(helpers.formatNumber(1000000), '1,000,000');
  assert.equal(helpers.formatNumber(-1234.5), '-1,234.5');
  assert.equal(helpers.formatNumber(1234.5), '1,234.5');
  assert.equal(helpers.formatNumber(0.00001), '0.00001', '微小差值不得被三位小数舍入成零');
  for (const bad of [null, undefined, '', '   ', true, false, {}, '-', NaN, Infinity, -Infinity]) {
    assert.equal(helpers.formatNumber(bad), '-', '非法值须回落 -: ' + String(bad));
  }
  assert.equal(helpers.formatNumber('1234.5'), '1,234.5');
  assert.equal(helpers.formatNumber('-1234.5'), '-1,234.5');
  assert.equal(helpers.formatNumber('1e3'), '1,000');
  assert.equal(helpers.formatNumber('.5'), '0.5');
  assert.equal(helpers.formatNumber('0x10'), '-', '非十进制字符串拒绝');
  assert.equal(helpers.formatNumber('12abc'), '-');

  // G2-helper) table：对象表头 numeric/tip、字符串表头兼容、Node 单元格不格式化
  const badgeNode = new Element('span');
  badgeNode.textContent = 'badge-x';
  const tbl = helpers.table(
    ['名称', { label: '数量', numeric: true }, { label: '状态', tip: '状态说明' }, '混合'],
    [['a', 1234.5, '通过', badgeNode], ['b', null, '-', 'text']]);
  const ths = findAll(tbl, node => node.tag === 'th');
  assert.equal(ths.length, 4);
  ths.forEach(th => assert.equal(th.getAttribute('scope'), 'col', 'th 须声明 scope=col'));
  assert.equal(ths[0].textContent, '名称', '字符串表头完全兼容');
  assert.equal(ths[1].className, 'document-table__numeric');
  assert.equal(ths[1].textContent, '数量');
  const headerTip = find(ths[2], node => node.attributes.role === 'tooltip');
  assert.ok(headerTip, '表头 tip 走 titleGroup tooltip');
  assert.match(ths[2].textContent, /状态说明/);
  const tds = findAll(tbl, node => node.tag === 'td');
  assert.equal(tds[1].className, 'document-table__numeric');
  assert.equal(tds[1].textContent, '1,234.5');
  assert.equal(tds[5].className, 'document-table__numeric', 'null 数值单元格仍保留 numeric 列样式');
  assert.equal(tds[5].textContent, '-');
  assert.equal(tds[3].children[0], badgeNode, 'Node 单元格保持对象身份，不转字符串');
  assert.equal(tds[7].textContent, 'text', '混合语义文本列不格式化');
  assert.equal(tds[7].className, undefined, '混合语义文本列不加 numeric 类');

  // G3-helper) titleGroup：span 根、tooltip 唯一 id、无 tip 无焦点停靠
  const withTip = helpers.titleGroup('标题', '说明文本');
  assert.equal(withTip.tag, 'span');
  assert.equal(withTip.children[0].className, 'panel-title');
  const tip = find(withTip, node => node.attributes.role === 'tooltip');
  assert.ok(tip);
  assert.match(tip.id, /^evaluations-tip-\d+$/);
  assert.equal(tip.textContent, '说明文本');
  assert.equal(withTip.getAttribute('aria-describedby'), tip.id);
  assert.equal(withTip.getAttribute('tabindex'), '0');
  const noTip = helpers.titleGroup('裸标题');
  assert.equal(noTip.textContent, '裸标题');
  assert.equal(noTip.getAttribute('tabindex'), null, '无 tip 不产生焦点停靠');
  assert.equal(noTip.getAttribute('aria-describedby'), null);
  const secondTipGroup = helpers.titleGroup('t', 'd');
  const secondTip = find(secondTipGroup, node => node.attributes.role === 'tooltip');
  assert.notEqual(secondTip.id, tip.id, 'tip id 全局递增不重复');

  // G3b-helper) sectionPanel：h3 + panel-header/panel-actions 壳
  const actionBtn = new Element('button');
  actionBtn.textContent = '主操作';
  const section = helpers.sectionPanel('面板标题', { tip: '面板提示', actions: [actionBtn] });
  assert.equal(section.panel.tag, 'section');
  assert.match(section.panel.className, /evaluations-panel/);
  const h3 = find(section.panel, node => node.tag === 'h3');
  assert.equal(h3.className, 'evaluations-panel__heading');
  assert.ok(find(section.panel, node => node.className === 'panel-header'));
  const actions = find(section.panel, node => node.className === 'panel-actions');
  assert.ok(actions);
  assert.equal(actions.children[0], actionBtn, 'actions 移入既有节点而非复制');
  assert.equal(section.body.evaluationsHeaderActions, actions);
  const bareSection = helpers.sectionPanel('裸面板');
  assert.ok(find(bareSection.panel, node => node.tag === 'h3'));
  assert.equal(bareSection.body.evaluationsHeaderActions.tag, 'span');

  // G3c-helper) filterBar/filterItem：label 包控件、HelpText aria 关联
  const bar = helpers.filterBar();
  assert.equal(bar.className, 'evaluations-filter');
  const control = new Element('input');
  control.name = 'demo';
  const item = helpers.filterItem('标签', control, '帮助文本');
  assert.equal(item.tag, 'label');
  assert.equal(item.className, 'evaluations-filter__item');
  assert.ok(item.children.includes(control), 'label 须包含控件');
  assert.equal(find(item, node => node.className === 'evaluations-filter__label').textContent, '标签');
  const help = find(item, node => node.tag === 'small');
  assert.match(help.className, /evaluations-filter__help/);
  assert.equal(control.getAttribute('aria-describedby'), help.id);
  const itemNoHelp = helpers.filterItem('标签2', new Element('input'));
  assert.equal(findAll(itemNoHelp, node => node.tag === 'small').length, 0);

  // G3d-helper) choiceGroup：role=group + aria-pressed，选中态 primary
  let selected = null;
  const group = helpers.choiceGroup('原因', [['a', '甲'], ['b', '乙']], 'b', value => { selected = value; });
  assert.equal(group.getAttribute('role'), 'group');
  assert.equal(group.getAttribute('aria-label'), '原因');
  const choiceBtns = findAll(group, node => node.tag === 'button');
  assert.deepEqual(choiceBtns.map(btn => btn.getAttribute('aria-pressed')), ['false', 'true']);
  assert.ok(choiceBtns[1].classAdds.includes('btn--primary'), '当前选中项加 primary 视觉');
  choiceBtns[0].handlers.click();
  assert.equal(selected, 'a');

  // G3e-helper) showLoading 降级路径 / disabledButton 无 handler / scrollTable 包裹
  const loadContainer = new Element();
  const loadingNode = helpers.showLoading(loadContainer, '加载中');
  assert.equal(loadingNode.className, 'muted loading-state');
  assert.equal(loadingNode.textContent, '加载中');
  assert.equal(loadContainer.children[0], loadingNode);
  const disabled = helpers.disabledButton('停用');
  assert.equal(disabled.tag, 'button');
  assert.equal(disabled.disabled, true);
  assert.equal(disabled.handlers.click, undefined, '置灰按钮不绑 click');
  const scrollWrap = helpers.scrollTable(tbl);
  assert.equal(scrollWrap.className, 'evaluations-table-scroll');
  assert.equal(scrollWrap.children[0], tbl);

  // G3f-helper) focusableChoiceGroup 兼容真实 DOM 的 HTMLCollection
  // 背景：真实 Element.children 是 HTMLCollection，无 filter/find/some；
  // 数组桩曾让 group.children.filter(...) 在 review/annotations 视图整页崩溃而 harness 漏测。
  function htmlCollectionLike() {
    const items = [];
    return {
      get length() { return items.length; },
      item(index) { return index >= 0 && index < items.length ? items[index] : null; },
      [Symbol.iterator]() { return items[Symbol.iterator](); },
      _push(node) { items.push(node); },
    };
  }
  class DomLikeElement {
    constructor(tag) {
      this.tag = tag;
      this.attributes = {};
      this.children = htmlCollectionLike();
      this.handlers = {};
      this.style = {};
      this.classList = { add() {}, remove() {}, contains() { return false; } };
      this._text = '';
      this.focused = false;
    }
    set textContent(value) { this._text = String(value); }
    get textContent() {
      return this._text + Array.from(this.children).map(node => node.textContent).join('');
    }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }
    addEventListener(event, handler) {
      (this.handlers[event] = this.handlers[event] || []).push(handler);
    }
    appendChild(node) { this.children._push(node); node.parentElement = this; return node; }
    focus() { this.focused = true; }
  }
  assert.equal(typeof group.children.filter, 'function', '前置对照：数组桩 children 具备 filter');
  const domSource = rawHelperSource.replace(exportMarker, `namespace.evaluations = {
      focusableChoiceGroup: typeof focusableChoiceGroup === 'function' ? focusableChoiceGroup : undefined,
    };`);
  const domContext = {
    NAGENT: {
      api: { evaluations: new Proxy({}, { get: () => async () => ({}) }) },
      ui: { el: (tag) => new DomLikeElement(tag) },
    },
  };
  domContext.window = domContext;
  vm.runInNewContext(domSource, domContext);
  const domHelpers = domContext.NAGENT.evaluations;
  assert.equal(typeof domHelpers.focusableChoiceGroup, 'function', 'focusableChoiceGroup 须可独立验证');
  let domSelected = null;
  const domGroup = domHelpers.focusableChoiceGroup('dom-gk', '待复核原因',
    [['a', '甲'], ['b', '乙']], 'a', value => { domSelected = value; });
  assert.equal(typeof domGroup.children.filter, 'undefined', 'HTMLCollection 不得暴露 filter 等数组方法');
  assert.equal(domGroup.children.length, 2, 'HTMLCollection length 可读');
  assert.equal(domGroup.children.item(1).getAttribute('data-value'), 'b', 'item() 索引可读');
  const domBButton = domGroup.children.item(1);
  domBButton.handlers.click[0]();
  assert.equal(domSelected, 'b', '组选择回调正常触发');
  assert.equal(domBButton.focused, true, '重渲染同步阶段焦点恢复到同组同值按钮');

  // -------------------------------------------------------------------------
  // Part G（视图接入）：spec-261112 T2/T3
  // -------------------------------------------------------------------------
  // G1) datasets 用例数 numeric：千分位/假零回落 '-'/numeric 样式保留
  const realListDatasets = api.listDatasets;
  api.listDatasets = async () => ({ items: [
    { dataset_id: 'ds-num', dataset_version: 'v1', target_kind: 'agent_run', case_count: 1234.5, status: 'valid' },
    { dataset_id: 'ds-big', dataset_version: 'v1', target_kind: 'agent_run', case_count: 1234567, status: 'valid' },
    { dataset_id: 'ds-null', dataset_version: 'v1', target_kind: 'agent_run', case_count: null, status: 'invalid' },
  ] });
  await moduleApi.refresh(state('datasets'));
  assert.match(root.textContent, /1,234\.5/);
  assert.match(root.textContent, /1,234,567/);
  assert.match(root.textContent, /校验失败/);
  {
    const datasetTable = find(root, node => node.tag === 'table');
    const numericCells = findAll(datasetTable,
      node => node.tag === 'td' && node.className === 'document-table__numeric');
    assert.equal(numericCells.length, 3, '用例数列三行均须声明 numeric');
    assert.ok(numericCells.some(node => node.textContent === '-'), 'null 数值回落 - 且保留 numeric 样式');
    // 操作列 Node 单元格不被格式化
    const detailBtn = findButton('详情', root);
    assert.ok(detailBtn, 'Node 单元格按钮保持可点击');
  }
  api.listDatasets = realListDatasets;

  // G1 续) review 队列 typed_value 混合语义列保持文本，不被 numeric 化
  reviewQueuePage = {
    items: [{ result_id: 'res-tiny', metric_name: 'm', provenance: 'automatic',
      query: 'q', typed_value: '0.00001', created_at: '2026-11-12T00:00:00+00:00' }],
    next_cursor: null,
  };
  await moduleApi.refresh(state('review'));
  assert.match(root.textContent, /0\.00001/, '混合语义值列原样展示');
  {
    const reviewTable = find(root, node => node.tag === 'table');
    const numericCells = findAll(reviewTable, node => node.className === 'document-table__numeric');
    assert.equal(numericCells.length, 0, 'review 值列不得声明 numeric');
  }
  reviewQueuePage = { items: [], next_cursor: null };

  // G2) 配置 Modal 评估开关 select 化：布尔载荷/预选/数值解析/CAS/409 保留
  const configPatches = [];
  const realPatchConfig = api.patchConfig;
  let patchBehavior = 'ok';
  api.patchConfig = async payload => {
    calls.push(['patchConfig']);
    configPatches.push(payload);
    if (patchBehavior === 'conflict') throw new Error('conflict');
    return {};
  };
  await moduleApi.refresh(state('overview'));
  await findButton('修订配置').handlers.click();
  let configForm = find(body, node => node.tag === 'form');
  const enabledControl = find(configForm, node => node.name === 'evaluation_enabled');
  assert.equal(enabledControl.tag, 'select', '评估开关须为 select 控件');
  assert.deepEqual(
    findAll(enabledControl, node => node.tag === 'option').map(o => o.value),
    ['true', 'false']);
  assert.equal(enabledControl.value, 'true', 'base=true 须预选启用');
  enabledControl.value = 'false';
  await configForm.handlers.submit({ preventDefault() {} });
  assert.equal(configPatches.at(-1).patch.evaluation_enabled, false, '载荷须为严格布尔 false');
  assert.equal(configPatches.at(-1).expected_version, 3);
  assert.equal(body.children.length, 0, '保存成功后弹窗关闭');

  // base=false 预选停用，切 true；数值字段解析为 number，expected_version 随行
  const realGetConfig = api.getConfig;
  api.getConfig = async () => ({
    config: { evaluation_enabled: false, evaluation_sampling_rate: '0.5', evaluation_worker_concurrency: 2 },
    version: 4, updated_at: '',
  });
  await moduleApi.refresh(state('overview'));
  await findButton('修订配置').handlers.click();
  configForm = find(body, node => node.tag === 'form');
  const falseControl = find(configForm, node => node.name === 'evaluation_enabled');
  assert.equal(falseControl.value, 'false', 'base=false 须预选停用');
  falseControl.value = 'true';
  const concurrencyControl = find(configForm, node => node.name === 'evaluation_worker_concurrency');
  concurrencyControl.value = '3';
  await configForm.handlers.submit({ preventDefault() {} });
  assert.equal(configPatches.at(-1).patch.evaluation_enabled, true);
  assert.equal(configPatches.at(-1).patch.evaluation_worker_concurrency, 3, '数值字段解析为 number');
  assert.equal(configPatches.at(-1).expected_version, 4);
  api.getConfig = realGetConfig;

  // 409 冲突：弹窗与输入保留，提示显式刷新，不自动重试
  patchBehavior = 'conflict';
  await moduleApi.refresh(state('overview'));
  await findButton('修订配置').handlers.click();
  configForm = find(body, node => node.tag === 'form');
  find(configForm, node => node.name === 'evaluation_enabled').value = 'false';
  await configForm.handlers.submit({ preventDefault() {} });
  assert.equal(find(body, node => node.tag === 'form'), configForm, '409 后弹窗保留');
  assert.match(body.textContent, /冲突/);
  patchBehavior = 'ok';
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();
  api.patchConfig = realPatchConfig;

  // G4) 同路由请求序号：筛选/重试触发的重叠请求，旧响应晚结算不得覆盖
  const realListResults = api.listResults;
  const realListReview = api.listReviewQueue;
  const realListAnnotations = api.listAnnotations;
  const realListEvaluators = api.listEvaluators;
  try {
    const resultRows = id => ({ items: [{ result_id: id, metric_name: 'm', provenance: 'automatic',
      status: 'value', typed_value: 1, created_at: '' }], next_cursor: null });
    const resultsResolvers = [];
    api.listResults = () => new Promise((resolve, reject) => resultsResolvers.push({ resolve, reject }));

    // 分支 1：B 先成功、A 后成功，A 不得覆盖 B
    const renderA = moduleApi.refresh(state('results'));
    await flush();
    let fromFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    fromFilter.value = '2026-11-01T00:00:00+00:00';
    fromFilter.handlers.change();
    await flush();
    assert.equal(resultsResolvers.length, 2, '筛选变更须发起第二次取数');
    resultsResolvers[1].resolve(resultRows('fresh-B'));
    await flush();
    assert.match(root.textContent, /fresh-B/);
    resultsResolvers[0].resolve(resultRows('stale-A'));
    await flush();
    assert.doesNotMatch(root.textContent, /stale-A/, 'A 晚于 B 完成不得覆盖');
    assert.match(root.textContent, /fresh-B/);
    await renderA;

    // 分支 2：B 成功后 A 失败，A 的错误不得覆盖 B（两次筛选制造重叠请求）
    fromFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    fromFilter.value = '2026-11-02T00:00:00+00:00';
    fromFilter.handlers.change();
    await flush();
    fromFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    fromFilter.value = '2026-11-05T00:00:00+00:00';
    fromFilter.handlers.change();
    await flush();
    assert.equal(resultsResolvers.length, 4);
    resultsResolvers[3].resolve(resultRows('fresh-B2'));
    await flush();
    resultsResolvers[2].reject(new Error('request_failed'));
    await flush();
    assert.match(root.textContent, /fresh-B2/);
    assert.doesNotMatch(root.textContent, /结果加载失败/, '旧请求失败不得覆盖新数据');

    // 分支 4：B 挂起时 A 先结算，A 丢弃；B 结算后展示 B
    fromFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    fromFilter.value = '2026-11-04T00:00:00+00:00';
    fromFilter.handlers.change();
    await flush();
    fromFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    fromFilter.value = '2026-11-07T00:00:00+00:00';
    fromFilter.handlers.change();
    await flush();
    assert.equal(resultsResolvers.length, 6);
    resultsResolvers[4].resolve(resultRows('stale-A4'));
    await flush();
    assert.doesNotMatch(root.textContent, /stale-A4/, 'B 仍在加载，A 先结算须丢弃');
    resultsResolvers[5].resolve(resultRows('fresh-B4'));
    await flush();
    assert.match(root.textContent, /fresh-B4/);

    // 分支 3：B 失败后 A 成功，B 的错误态保留不被 A 覆盖（置末：错误态
    // 会清空数据区，后续分支不再依赖筛选控件）
    fromFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    fromFilter.value = '2026-11-03T00:00:00+00:00';
    fromFilter.handlers.change();
    await flush();
    fromFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    fromFilter.value = '2026-11-06T00:00:00+00:00';
    fromFilter.handlers.change();
    await flush();
    assert.equal(resultsResolvers.length, 8);
    resultsResolvers[7].reject(new Error('request_failed'));
    await flush();
    assert.match(root.textContent, /结果加载失败/);
    resultsResolvers[6].resolve(resultRows('stale-A3'));
    await flush();
    assert.doesNotMatch(root.textContent, /stale-A3/, '旧请求成功不得覆盖新错误态');
    assert.match(root.textContent, /结果加载失败/);

    // 分支 5：deactivate/切视图后 A 结算，root 与旧容器均无新写入
    const pendingView = moduleApi.refresh(state('results'));
    await flush();
    await moduleApi.refresh(state('datasets'));
    await flush();
    const lastResolver = resultsResolvers.at(-1);
    lastResolver.resolve(resultRows('late-after-leave'));
    await pendingView;
    await flush();
    assert.doesNotMatch(root.textContent, /late-after-leave/, '离页后结算不得写 DOM');
    assert.match(root.textContent, /暂无数据集/);

    // 其余可重叠视图：各走真实重渲染入口，验证同一序号守卫
    const staleGuard = async (viewName, trigger, apiSlot, realApi, rowIdField) => {
      const resolvers = [];
      api[apiSlot] = () => new Promise((resolve, reject) => resolvers.push({ resolve, reject }));
      const first = moduleApi.refresh(state(viewName));
      await flush();
      await trigger();
      await flush();
      assert.equal(resolvers.length, 2, viewName + ' 重渲染须发起第二次取数');
      resolvers[1].resolve({ items: [{ [rowIdField]: 'fresh-' + viewName }], next_cursor: null });
      await flush();
      resolvers[0].resolve({ items: [{ [rowIdField]: 'stale-' + viewName }], next_cursor: null });
      await first;
      await flush();
      assert.match(root.textContent, new RegExp('fresh-' + viewName), viewName + ' 新数据保留');
      assert.doesNotMatch(root.textContent, new RegExp('stale-' + viewName), viewName + ' 旧响应丢弃');
      api[apiSlot] = realApi;
    };
    await staleGuard('review', async () => { await findButton('争议').handlers.click(); },
      'listReviewQueue', realListReview, 'result_id');
    await staleGuard('annotations', async () => { await findButton('争议').handlers.click(); },
      'listAnnotations', realListAnnotations, 'source_result_id');
    await staleGuard('evaluators', async () => {
      const method = find(root, node => node.name === 'evaluator_method_filter' && node.handlers.change);
      method.value = 'deterministic';
      await method.handlers.change();
    }, 'listEvaluators', realListEvaluators, 'definition_id');

    // 分支 6：旧请求失败走共享 body 的 renderErrorState，不得覆盖新成功态
    const evalResolvers = [];
    api.listEvaluators = () => new Promise((resolve, reject) => evalResolvers.push({ resolve, reject }));
    const evalFirst = moduleApi.refresh(state('evaluators'));
    await flush();
    const methodFilter2 = find(root, node => node.name === 'evaluator_method_filter' && node.handlers.change);
    methodFilter2.value = 'llm_judge';
    await methodFilter2.handlers.change();
    await flush();
    assert.equal(evalResolvers.length, 2);
    evalResolvers[1].resolve({ items: [{ definition_id: 'fresh-eval-fail' }], next_cursor: null });
    await flush();
    evalResolvers[0].reject(new Error('request_failed'));
    await evalFirst;
    await flush();
    assert.match(root.textContent, /fresh-eval-fail/);
    assert.doesNotMatch(root.textContent, /评估器列表加载失败/, '旧请求失败不得覆盖新视图');
    api.listEvaluators = realListEvaluators;
  } finally {
    api.listResults = realListResults;
    api.listReviewQueue = realListReview;
    api.listAnnotations = realListAnnotations;
    api.listEvaluators = realListEvaluators;
  }

  // G5) Loading 生命周期：pending 有真实 loading-state；成功/空/失败均清除
  const makeDeferred = () => {
    const d = {};
    d.promise = new Promise((resolve, reject) => { d.resolve = resolve; d.reject = reject; });
    return d;
  };
  const hasLoadingState = scope => findAll(scope, node =>
    typeof node.className === 'string' && node.className.split(/\s+/).includes('loading-state')).length > 0;
  const noLoadingState = scope => findAll(scope, node =>
    typeof node.className === 'string' && node.className.split(/\s+/).includes('loading-state')).length === 0;
  const savedApi = {
    status: api.status, listDatasets: api.listDatasets, listExperiments: api.listExperiments,
    listResults: api.listResults, listReviewQueue: api.listReviewQueue,
    listAnnotations: api.listAnnotations, listEvaluators: api.listEvaluators,
    getCapabilities: api.getCapabilities, getConfig: api.getConfig, overview: api.overview,
    getAnalytics: api.getAnalytics,
  };
  try {
    // G5a) render() 层 status：pending Loading；enabled=false 与 reject 均清除
    let sd = makeDeferred();
    api.status = () => sd.promise;
    let p = moduleApi.refresh(state('datasets'));
    await flush();
    assert.ok(hasLoadingState(root), 'status 挂起期间展示 Loading');
    sd.resolve({ enabled: false });
    await flush(); await p;
    assert.ok(noLoadingState(root), '总闸关闭后 Loading 清除');
    assert.match(root.textContent, /评估功能未启用/);

    sd = makeDeferred();
    api.status = () => sd.promise;
    p = moduleApi.refresh(state('datasets'));
    await flush();
    assert.ok(hasLoadingState(root));
    sd.reject(new Error('boom'));
    await flush(); await p;
    assert.ok(noLoadingState(root), 'status 失败后 Loading 清除');
    assert.match(root.textContent, /评估状态加载失败/);
    api.status = savedApi.status;

    // G5b) 单 API 视图：pending/空成功/失败三态 Loading 生命周期
    const singleApiViews = [
      ['datasets', 'listDatasets', { items: [] }, /暂无数据集/, '数据集加载失败'],
      ['experiments', 'listExperiments', { items: [], next_cursor: null }, /暂无实验/, '实验列表加载失败'],
      ['results', 'listResults', { items: [], next_cursor: null }, /暂无评估结果/, '结果加载失败'],
      ['review', 'listReviewQueue', { items: [], next_cursor: null }, /当前筛选暂无待复核结果/, '待复核队列加载失败'],
      ['annotations', 'listAnnotations', { items: [], next_cursor: null }, /当前筛选暂无待标注结果/, '标注队列加载失败'],
    ];
    for (const [viewName, apiName, emptyPayload, emptyRegex, errorFragment] of singleApiViews) {
      let d = makeDeferred();
      api[apiName] = () => d.promise;
      const pending = moduleApi.refresh(state(viewName));
      await flush();
      assert.ok(hasLoadingState(root), viewName + ' 挂起期间展示 Loading');
      d.resolve(emptyPayload);
      await flush(); await pending;
      assert.ok(noLoadingState(root), viewName + ' 空成功后 Loading 清除');
      assert.match(root.textContent, emptyRegex, viewName + ' 空态文案');

      d = makeDeferred();
      api[apiName] = () => d.promise;
      const failing = moduleApi.refresh(state(viewName));
      await flush();
      assert.ok(hasLoadingState(root), viewName + ' 二次挂起仍展示 Loading');
      d.reject(new Error('boom'));
      await flush(); await failing;
      assert.ok(noLoadingState(root), viewName + ' 失败后 Loading 清除');
      assert.match(root.textContent, new RegExp(errorFragment.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')),
        viewName + ' 错误态文案');
      assert.ok(findButton('重试', root), viewName + ' 错误态提供重试');
      api[apiName] = savedApi[apiName];
    }

    // G5c) overview 组合请求：未全部成功保持 Loading，任一失败转错误
    let cfgD = makeDeferred();
    let ovD = makeDeferred();
    let expD = makeDeferred();
    let anD = makeDeferred();
    api.getConfig = () => cfgD.promise;
    api.overview = () => ovD.promise;
    api.listExperiments = () => expD.promise;
    api.getAnalytics = () => anD.promise;
    const overviewPending = moduleApi.refresh(state('overview'));
    await flush();
    assert.ok(hasLoadingState(root));
    cfgD.resolve({ config: {}, version: 1, updated_at: '' });
    ovD.resolve({ capacity: {}, budget: {}, evidence_retention_days: 7, experiment_results: {}, producer_states: [] });
    expD.resolve({ items: [], next_cursor: null });
    await flush();
    assert.ok(hasLoadingState(root), '组合请求未全部结算时保持 Loading');
    anD.resolve({ groups: [], buckets: [], coverage: { status: 'unknown' } });
    await flush(); await overviewPending;
    assert.ok(noLoadingState(root), 'overview 全成功后 Loading 清除');

    cfgD = makeDeferred(); ovD = makeDeferred(); expD = makeDeferred(); anD = makeDeferred();
    api.getConfig = () => cfgD.promise;
    api.overview = () => ovD.promise;
    api.listExperiments = () => expD.promise;
    api.getAnalytics = () => anD.promise;
    const overviewFailing = moduleApi.refresh(state('overview'));
    await flush();
    anD.reject(new Error('boom'));
    await flush(); await overviewFailing;
    assert.ok(noLoadingState(root), 'overview 任一失败后 Loading 清除');
    assert.match(root.textContent, /评估概览加载失败/);

    // G5d) evaluators 组合请求同生命周期
    let leD = makeDeferred();
    let capD = makeDeferred();
    api.listEvaluators = () => leD.promise;
    api.getCapabilities = () => capD.promise;
    const evalPending = moduleApi.refresh(state('evaluators'));
    await flush();
    assert.ok(hasLoadingState(root), 'evaluators 挂起期间展示 Loading');
    capD.resolve({ entries: [], judge_configured: false });
    await flush();
    assert.ok(hasLoadingState(root), '能力目录先返回仍保持 Loading');
    leD.resolve({ items: [], next_cursor: null });
    await flush(); await evalPending;
    assert.ok(noLoadingState(root), 'evaluators 成功后 Loading 清除');

    // G5e) 失败不清筛选：results 错误态保留筛选控件，重试携带原过滤
    const failD = makeDeferred();
    api.listResults = () => failD.promise;
    const failRender = moduleApi.refresh(state('results'));
    await flush();
    const resultsFilter = find(root, node => node.name === 'results_created_from' && node.handlers.change);
    assert.ok(resultsFilter, '挂起期间筛选控件已渲染');
    resultsFilter.value = '2026-11-01T00:00:00+00:00';
    await resultsFilter.handlers.change();
    await flush();
    failD.reject(new Error('boom'));
    await flush(); await failRender;
    assert.ok(find(root, node => node.name === 'results_created_from'), '失败后筛选控件保留');
    assert.ok(noLoadingState(root));
    const retryD = makeDeferred();
    const retryParams = [];
    api.listResults = params => { retryParams.push(params); return retryD.promise; };
    // 不直接 await click：渲染 Promise 依赖后续 retryD 兑现，先持有再结算
    const retryClick = findButton('重试', root).handlers.click();
    await flush();
    retryD.resolve({ items: [], next_cursor: null });
    await flush();
    await retryClick;
    assert.deepEqual(JSON.parse(JSON.stringify(retryParams.at(-1))).created_from,
      '2026-11-01T00:00:00+00:00', '重试保留筛选条件');
  } finally {
    Object.assign(api, savedApi);
  }

  // G5f) 独立 fixture：ui.renderLoading 共享路径同样可被清理
  {
    const uiRoot2 = new Element();
    const loadingCreated = [];
    const uiContext = {
      Node: Element,
      document: {
        getElementById: () => uiRoot2,
        createElement: tag => new Element(tag),
        body: new Element(),
      },
      NAGENT: {
        api: { evaluations: new Proxy({}, { get: () => async () => ({}) }) },
        ui: {
          el: (tag, className) => {
            const node = new Element(tag);
            if (className) node.className = className;
            return node;
          },
          renderLoading: (parent, message) => {
            const node = new Element('div');
            node.className = 'muted loading-state';
            node.textContent = message;
            loadingCreated.push(node);
            parent.appendChild(node);
          },
        },
        modal: { alert() {} },
      },
    };
    uiContext.window = uiContext;
    const datasetD = makeDeferred();
    uiContext.NAGENT.api.evaluations = {
      status: async () => ({ enabled: true }),
      listDatasets: () => datasetD.promise,
    };
    vm.runInNewContext(rawHelperSource, uiContext);
    const uiModule = uiContext.NAGENT.evaluations;
    const pending = uiModule.refresh(state('datasets'));
    await new Promise(resolve => setImmediate(resolve));
    // render() 状态 Loading 与 datasets 视图 Loading 各一次，均走共享 helper
    assert.equal(loadingCreated.length, 2, '共享 ui.renderLoading 被调用');
    assert.ok(hasLoadingState(uiRoot2));
    datasetD.resolve({ items: [] });
    await pending;
    assert.ok(noLoadingState(uiRoot2), '共享路径 Loading 随 dataBody 清除');
    assert.match(uiRoot2.textContent, /暂无数据集/);
  }

  // G8) S5/S8/S9/S10 展示合同：header actions、titleGroup 区块标题、
  // 数字列声明、空态三要素、投递去徽章、scrollTable、对比七列 numeric
  const g8Saved = {};
  ['getConfig', 'overview', 'listExperiments', 'getAnalytics', 'getExperiment',
    'compareExperiments', 'listDatasets', 'listResults', 'getResult'].forEach((name) => {
    g8Saved[name] = api[name];
  });
  const hasClass = (node, token) => typeof node.className === 'string'
    && node.className.split(/\s+/).includes(token);
  const findTh = (scope, label) => find(scope, node => node.tag === 'th' && node.textContent === label);
  try {
    // G8a) overview S5：修订配置入 header actions；趋势/Producer titleGroup
    // 与 numeric 列；无有效评分空态三要素
    api.getConfig = async () => ({ config: { evaluation_enabled: true }, version: 7, updated_at: '' });
    api.overview = async () => ({
      capacity: { probe_available: false }, budget: {}, evidence_retention_days: 7,
      experiment_results: {},
      producer_states: [{
        producer_id: 'p-1', counter_epoch: 1234, captured_count: 5678,
        capture_error_count: 0, queue_full_count: 2, observed_at: '2026-11-12T00:00:00+00:00',
      }],
    });
    api.listExperiments = async () => ({ items: [], next_cursor: null });
    api.getAnalytics = async () => ({
      snapshot_at: '', window: { from: '', to: '' }, groups: [],
      coverage: { status: 'unknown', reason: '缺少应评估目标总数' },
    });
    await moduleApi.refresh(state('overview'));
    const headerActions = find(root, node => hasClass(node, 'panel-actions'));
    assert.ok(headerActions, '面板 header actions 容器存在');
    assert.ok(find(headerActions, node => node.textContent === '修订配置' && node.handlers.click),
      '修订配置按钮位于 header actions');
    assert.ok(find(root, node => hasClass(node, 'panel-title') && node.textContent === '评分趋势'),
      '评分趋势区块使用 titleGroup');
    assert.ok(find(root, node => hasClass(node, 'panel-title') && node.textContent === 'Producer 心跳'),
      'Producer 心跳区块使用 titleGroup');
    const trendEmpty = find(root, node => hasClass(node, 'empty-state') && /暂无有效评分/.test(node.textContent));
    assert.ok(trendEmpty, '空态保留暂无有效评分前缀');
    assert.match(trendEmpty.textContent, /评估作业|评估完成/, '空态说明下一步');
    assert.match(trendEmpty.textContent, /筛选/, '空态说明无筛选影响');
    for (const label of ['Epoch', '累计捕获', '捕获错误', '队列满']) {
      const th = findTh(root, label);
      assert.ok(th && hasClass(th, 'document-table__numeric'), 'Producer numeric 列: ' + label);
    }
    assert.match(root.textContent, /1,234/, 'Epoch 千分位');
    assert.match(root.textContent, /5,678/, '累计捕获千分位');

    // 趋势有分组：样本数列 numeric
    api.getAnalytics = async () => ({
      snapshot_at: '', window: { from: '', to: '' },
      groups: [{
        definition_id: 'm-1', definition_version: 'v1', provenance: 'automatic',
        output_type: 'number', total_count: 2000, count: 1, mean: 0.5, min: 0.5, max: 0.5,
        status_counts: { value: 1 }, daily_counts: {},
      }],
      coverage: { status: 'unknown', reason: '缺少应评估目标总数' },
    });
    await moduleApi.refresh(state('overview'));
    const sampleTh = findTh(root, '样本数');
    assert.ok(sampleTh && hasClass(sampleTh, 'document-table__numeric'), '趋势样本数 numeric');
    assert.match(root.textContent, /2,000/, '样本数千分位');

    // G8b) datasets S8：导入按钮入 header actions；空态三要素；表格 scrollTable
    api.listDatasets = async () => ({ items: [] });
    await moduleApi.refresh(state('datasets'));
    const datasetActions = find(root, node => hasClass(node, 'panel-actions'));
    assert.ok(find(datasetActions, node => node.textContent === '导入标注 JSON' && node.handlers.click),
      '导入按钮位于 header actions');
    const datasetEmpty = find(root, node => hasClass(node, 'empty-state') && /暂无数据集/.test(node.textContent));
    assert.ok(datasetEmpty, '数据集空态保留前缀');
    assert.match(datasetEmpty.textContent, /导入|导出/, '空态说明下一步');
    assert.match(datasetEmpty.textContent, /筛选/, '空态说明无筛选影响');
    api.listDatasets = async () => ({
      items: [{
        dataset_id: 'ds-1', dataset_version: 'v1', target_kind: 'agent_run',
        case_count: 10, status: 'valid',
      }],
    });
    await moduleApi.refresh(state('datasets'));
    const scrollWrap = find(root, node => hasClass(node, 'evaluations-table-scroll')
      && find(node, child => child.tag === 'table'));
    assert.ok(scrollWrap, '数据集表格包 scrollTable');

    // G8c) experiments S9：空态文案；详情分布 numeric、投递去徽章、titleGroup
    api.listExperiments = async () => ({ items: [], next_cursor: null });
    await moduleApi.refresh(state('experiments'));
    const expEmpty = find(root, node => hasClass(node, 'empty-state') && /暂无实验/.test(node.textContent));
    assert.ok(expEmpty, '实验空态保留前缀');
    assert.match(expEmpty.textContent, /execute\/replay/, '空态说明实验来源');
    assert.match(expEmpty.textContent, /筛选/, '空态说明筛选情况');

    api.listExperiments = async () => ({
      items: [{
        experiment_id: 'exp-g8', dataset_id: 'ds-1', dataset_version: 'v1',
        mode: 'replay', status: 'running', created_at: '',
      }], next_cursor: null,
    });
    api.getExperiment = async () => ({
      experiment: {
        experiment_id: 'exp-g8', dataset_id: 'ds-1', dataset_version: 'v1',
        mode: 'replay', status: 'running', created_at: '',
      },
      coverage: { results_total: 1234, value_results: 1000, error_or_skipped_results: 234 },
      distribution: {
        'm-1': { results_total: 1234, passed: 1000, failed: 200, error_or_skipped: 34 },
      },
      delivery_status: {
        langfuse: { total: 1234, verified: 1000, pending: 230, dead_letter: 4 },
      },
      baseline: null, gate_report_ref: null,
    });
    await moduleApi.refresh(state('experiments'));
    await findButton('详情', root).handlers.click();
    await flush();
    assert.ok(find(body, node => hasClass(node, 'panel-title') && node.textContent === '指标分布'),
      '指标分布区块使用 titleGroup');
    assert.ok(find(body, node => hasClass(node, 'panel-title') && node.textContent === '投递状态'),
      '投递状态区块使用 titleGroup');
    for (const label of ['结果数', '通过', '质量不通过', '异常跳过']) {
      const th = findTh(body, label);
      assert.ok(th && hasClass(th, 'document-table__numeric'), '分布 numeric 列: ' + label);
    }
    const deliveryCounts = find(body, node => hasClass(node, 'evaluations-delivery-counts'));
    assert.ok(deliveryCounts, '投递计数去徽章化容器');
    assert.ok(!find(deliveryCounts, node => typeof node.className === 'string'
      && node.className.split(/\s+/).includes('badge')), '投递计数不得使用徽章');
    assert.match(deliveryCounts.textContent, /1,234/);
    assert.match(deliveryCounts.textContent, /dead_letter/);
    const detailClose = find(body, node => hasClass(node, 'modal-close') && node.handlers.click);
    if (detailClose) await detailClose.handlers.click();

    // G8c-2) openResultModal：尝试列 numeric，0 与 null 分别落 0 与 -
    api.listResults = async () => ({
      items: [{
        result_id: 'res-g8', metric_name: 'm-1', provenance: 'automatic',
        status: 'value', passed: true, typed_value: 0.9, created_at: '',
      }], next_cursor: null,
    });
    api.getResult = async () => ({
      result: {
        result_id: 'res-g8', metric_name: 'm-1', provenance: 'automatic',
        status: 'value', typed_value: 0.9, created_at: '',
      },
      evidence_summary: {},
      deliveries: [
        { sink: 'langfuse', projection_version: 'p1', status: 'verified', attempt_count: 0 },
        { sink: 'webhook', projection_version: 'p1', status: 'pending', attempt_count: null },
      ],
    });
    await moduleApi.refresh(state('results'));
    await findButton('详情', root).handlers.click();
    await flush();
    const attemptTh = findTh(body, '尝试');
    assert.ok(attemptTh && hasClass(attemptTh, 'document-table__numeric'), '投递尝试列 numeric');
    const attemptTds = findAll(body, node => node.tag === 'td' && hasClass(node, 'document-table__numeric'))
      .map(node => node.textContent);
    assert.ok(attemptTds.includes('0'), 'attempt_count=0 显示 0');
    assert.ok(attemptTds.includes('-'), 'attempt_count=null 回落 -');
    const resultClose = find(body, node => hasClass(node, 'modal-close') && node.handlers.click);
    if (resultClose) await resultClose.handlers.click();

    // G8d) compare S10：七列 numeric、null 回落 -、区块 titleGroup
    api.listExperiments = async () => ({
      items: [
        { experiment_id: 'exp-g8', dataset_id: 'ds-1', dataset_version: 'v1', mode: 'replay', status: 'completed', created_at: '' },
        { experiment_id: 'exp-base', dataset_id: 'ds-1', dataset_version: 'v1', mode: 'replay', status: 'completed', created_at: '' },
      ], next_cursor: null,
    });
    api.getExperiment = async id => ({
      experiment: {
        experiment_id: id, dataset_id: 'ds-1', dataset_version: 'v1',
        mode: 'replay', status: 'completed', created_at: '',
      },
      coverage: { results_total: 0, value_results: 0, error_or_skipped_results: 0 },
      distribution: {}, delivery_status: {}, baseline: null, gate_report_ref: null,
    });
    await moduleApi.refresh(state('experiments'));
    await findButton('详情', root).handlers.click();
    await flush();
    await findButton('与基线对比', body).handlers.click();
    await flush();
    const g8CompareForm = find(body, node => node.tag === 'form');
    api.compareExperiments = async () => ({
      evidence_basis: 'ledger_reconstruction', denominator_basis: 'observed',
      candidate: { experiment_id: 'exp-g8' }, baseline: { experiment_id: 'exp-base' },
      metrics: [{
        metric_name: 'm-1', paired_cases: 1000,
        candidate: { value_count: 500, macro_value: null },
        baseline: { value_count: 500, macro_value: 0.42 },
        diff: null, paired_ci_lower: null,
      }],
      delivery_status_counts: { langfuse: { verified: 1, pending: 0 } },
      policy_fingerprint: 'fp-g8',
      conclusion: { state: 'inconclusive', reasons: ['r1'], denominator_basis: 'observed' },
    });
    await g8CompareForm.handlers.submit({ preventDefault() {} });
    await flush();
    assert.ok(find(body, node => hasClass(node, 'panel-title') && node.textContent === '配对指标'),
      '配对指标区块使用 titleGroup');
    assert.ok(find(body, node => hasClass(node, 'panel-title') && /投递状态/.test(node.textContent)),
      '投递状态区块使用 titleGroup');
    assert.ok(find(body, node => hasClass(node, 'panel-title') && node.textContent === '门禁结论'),
      '门禁结论区块使用 titleGroup');
    for (const label of ['配对case', '候选样本', '候选均值', '基线样本', '基线均值', '差值', 'CI下界']) {
      const th = findTh(body, label);
      assert.ok(th && hasClass(th, 'document-table__numeric'), '对比 numeric 列: ' + label);
    }
    const compareTds = findAll(body, node => node.tag === 'td' && hasClass(node, 'document-table__numeric'))
      .map(node => node.textContent);
    const dashCount = compareTds.filter(text => text === '-').length;
    assert.ok(dashCount >= 3, 'null 数值回落 -（macro_value/diff/ci）');
    assert.ok(compareTds.includes('1,000'), '配对样本千分位');
  } finally {
    Object.assign(api, g8Saved);
  }

  // G6) human 评估器：停用按钮置灰不隐藏、无 click、常驻说明、不发请求；
  // 非 human 行操作顺序启停→详情，CAS 合同不回归
  {
    const savedListEvaluators = api.listEvaluators;
    const savedListResult = listResult;
    const usageBefore = usageCalls.length;
    try {
      listResult = { items: [
        { definition_id: 'human.feedback', version: 'v1', metric_name: '*',
          target_kind: 'agent_run', method: 'human',
          usage: { online_enabled: false, revision: 0 } },
        { definition_id: 'kb.recall_at_5', version: 'v1', metric_name: 'kb.recall_at_5',
          target_kind: 'retrieval', method: 'deterministic',
          usage: { online_enabled: true, revision: 2 } },
      ], next_cursor: null };
      api.listEvaluators = async params => {
        calls.push(['listEvaluators']);
        listParamsHistory.push(params);
        return listResult;
      };
      await moduleApi.refresh(state('evaluators'));
      const stopButtons = findAll(root, node => node.tag === 'button' && node.textContent === '停用');
      assert.equal(stopButtons.length, 2, 'human 与已启用自动评估器各有一个停用按钮');
      const humanRow = stopButtons.map(btn => {
        let cursor = btn;
        while (cursor && cursor.tag !== 'tr') cursor = cursor.parentElement;
        return cursor;
      }).find(tr => tr && /human\.feedback/.test(tr.textContent));
      assert.ok(humanRow, 'human 行存在');
      const humanStop = find(humanRow, node => node.tag === 'button' && node.textContent === '停用');
      assert.equal(humanStop.disabled, true, 'human 停用按钮置灰');
      assert.ok(!humanStop.handlers.click, 'human 停用按钮无 click handler');
      assert.match(humanRow.textContent, /人工评估器不支持自动使用启停/, 'human 行常驻说明');
      assert.ok(findButton('详情', humanRow), 'human 行仍可打开详情');
      assert.equal(usageCalls.length, usageBefore, 'human 渲染不发起启停请求');

      const autoRow = (() => {
        let cursor = findButton('停用', root);
        while (cursor && cursor.tag !== 'tr') cursor = cursor.parentElement;
        return cursor;
      })();
      assert.ok(autoRow && /kb\.recall_at_5/.test(autoRow.textContent), '自动评估器行存在');
      const autoButtons = findAll(autoRow, node => node.tag === 'button').map(node => node.textContent);
      assert.ok(autoButtons.indexOf('停用') < autoButtons.indexOf('详情'), '操作列顺序启停→详情');
      const autoStop = findButton('停用', autoRow);
      assert.ok(autoStop && autoStop.disabled !== true, '自动评估器停用按钮可点击');
      await autoStop.handlers.click();
      assert.deepEqual(JSON.parse(JSON.stringify(usageCalls.at(-1))),
        ['kb.recall_at_5', 'v1', { online_enabled: false, expected_revision: 2 }],
        '自动评估器停用 CAS 载荷不变');
    } finally {
      api.listEvaluators = savedListEvaluators;
      listResult = savedListResult;
    }
  }

  // G7) ARIA 语义与 Tips/HelpText id 唯一：choiceGroup role=group +
  // aria-pressed 单 true；tooltip/help id 批内唯一且跨刷新无交集
  {
    const collectIdSnapshot = (scope) => {
      const groups = findAll(scope, node => node.attributes && node.attributes.role === 'group');
      const tooltips = findAll(scope, node => node.attributes && node.attributes.role === 'tooltip');
      const helps = findAll(scope, node => typeof node.className === 'string'
        && node.className.split(/\s+/).includes('evaluations-filter__help'));
      return { groups, tooltips, helps };
    };
    const assertGroupSemantics = (groups) => {
      assert.ok(groups.length >= 2, '至少两个 choiceGroup（状态 + 原因）');
      groups.forEach((group) => {
        const buttons = group.children.filter(node => node.tag === 'button');
        const pressed = buttons.map(btn => btn.attributes['aria-pressed']);
        assert.ok(pressed.every(value => value === 'true' || value === 'false'),
          'aria-pressed 仅 true/false');
        assert.equal(pressed.filter(value => value === 'true').length, 1,
          '每组恰一个选中态');
        assert.ok(group.attributes['aria-label'], '组须有可访问名称');
      });
    };
    const assertTipContracts = (scope) => {
      const titledGroups = findAll(scope, node => hasClass(node, 'panel-title-group'));
      titledGroups.forEach((group) => {
        const tip = find(group, node => node.attributes && node.attributes.role === 'tooltip');
        if (tip) {
          assert.equal(group.attributes['aria-describedby'], tip.id, 'tip 关联 aria-describedby');
          assert.equal(group.attributes.tabindex, '0', '带 tip 组可聚焦');
        } else {
          assert.equal(group.attributes.tabindex, undefined, '无 tip 组不设 tabindex');
          assert.equal(group.attributes['aria-describedby'], undefined, '无 tip 组无 describedby');
        }
      });
    };

    await moduleApi.refresh(state('annotations'));
    const first = collectIdSnapshot(root);
    assertGroupSemantics(first.groups);
    const firstTipIds = first.tooltips.map(node => node.id).filter(Boolean);
    const firstHelpIds = first.helps.map(node => node.id).filter(Boolean);
    assert.equal(new Set(firstTipIds).size, firstTipIds.length, 'tooltip id 批内唯一');
    assert.equal(new Set(firstHelpIds).size, firstHelpIds.length, 'help id 批内唯一');
    assertTipContracts(root);
    // HelpText 必须被控件 aria-describedby 引用
    first.helps.forEach((help) => {
      const control = find(root, node => node.attributes
        && node.attributes['aria-describedby'] === help.id);
      assert.ok(control, 'help id 存在控制器引用: ' + help.id);
    });

    await moduleApi.refresh(state('annotations'));
    const second = collectIdSnapshot(root);
    assertGroupSemantics(second.groups);
    const secondTipIds = second.tooltips.map(node => node.id).filter(Boolean);
    const secondHelpIds = second.helps.map(node => node.id).filter(Boolean);
    assert.equal(new Set(secondTipIds).size, secondTipIds.length, '第二批 tooltip id 唯一');
    assert.equal(new Set(secondHelpIds).size, secondHelpIds.length, '第二批 help id 唯一');
    assert.equal(secondTipIds.some(id => firstTipIds.includes(id)), false, 'tooltip id 跨刷新无交集');
    assert.equal(secondHelpIds.some(id => firstHelpIds.includes(id)), false, 'help id 跨刷新无交集');

    // results 视图六个 filterItem（三项带 HelpText），且无 tablist
    await moduleApi.refresh(state('results'));
    const resultsItems = findAll(root, node => hasClass(node, 'evaluations-filter__item'));
    assert.equal(resultsItems.length, 6, 'results 六个筛选项结构化为 filterItem');
    const resultsHelps = findAll(root, node => hasClass(node, 'evaluations-filter__help'));
    assert.equal(resultsHelps.length, 3, 'results 时间/DefinitionKey 三项带 HelpText');
    assert.ok(!find(root, node => node.attributes && node.attributes.role === 'tablist'),
      '禁止 tablist 角色');
  }

  // G9) 焦点恢复：仅组按钮主动触发筛选时聚焦同组同值新节点；
  // 程序 refresh/初次加载不自动聚焦；两组互不串焦
  {
    const findGroup = label => find(root, node => node.attributes
      && node.attributes.role === 'group' && node.attributes['aria-label'] === label);
    const groupButton = (group, value) => group.children
      .find(node => node.tag === 'button' && node.getAttribute('data-value') === value);

    // 初次/程序加载不聚焦：新树内无聚焦节点，残留焦点只可能在已分离旧节点
    await moduleApi.refresh(state('annotations'));
    assert.ok(!find(root, node => node.focused === true), '程序 refresh 新视图无聚焦节点');
    if (getActiveElement()) {
      // 向下遍历判定真实挂载（harness 的向上 parent 链在 textContent 清空后仍保留）
      assert.ok(!find(root, node => node === getActiveElement()),
        '程序 refresh 不把焦点移入新树');
    }

    // 标注原因组切换：焦点落到重渲染后的同组同值新按钮
    const reasonGroup = findGroup('标注原因');
    const oldReasonBtn = groupButton(reasonGroup, 'disputed');
    await oldReasonBtn.handlers.click();
    await flush();
    const newReasonGroup = findGroup('标注原因');
    const newReasonBtn = groupButton(newReasonGroup, 'disputed');
    assert.ok(newReasonBtn !== oldReasonBtn, '重渲染生成新按钮节点');
    assert.equal(newReasonBtn.focused, true, '同组同值新按钮获得焦点');
    assert.equal(getActiveElement(), newReasonBtn, 'activeElement 指向新按钮');
    const stateGroup = findGroup('标注状态');
    assert.ok(!stateGroup.children.some(node => node.focused === true), '状态组不串焦');

    // 标注状态组独立焦点记录
    const oldStateBtn = groupButton(findGroup('标注状态'), 'pending');
    await oldStateBtn.handlers.click();
    await flush();
    const newStateBtn = groupButton(findGroup('标注状态'), 'pending');
    assert.equal(newStateBtn.focused, true, '状态组同值新按钮获得焦点');
    assert.equal(getActiveElement(), newStateBtn);

    // review 原因组同样恢复焦点；程序 refresh 后新树无聚焦
    await moduleApi.refresh(state('review'));
    assert.ok(!find(root, node => node.focused === true), '程序 refresh 不保留焦点');
    const reviewOld = groupButton(findGroup('待复核原因'), 'anomalous');
    await reviewOld.handlers.click();
    await flush();
    const reviewNew = groupButton(findGroup('待复核原因'), 'anomalous');
    assert.equal(reviewNew.focused, true, 'review 同组同值新按钮获得焦点');
    assert.equal(getActiveElement(), reviewNew);
  }

  console.log('evaluation workbench harness passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
