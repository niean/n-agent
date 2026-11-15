// 评估器管理视图 harness（Task 11/spec 4.2-4.3）。
// Part A：management-api.js evaluations 对象新增 5 个评估器管理方法与
// 端点契约（list/get/create/usage/capabilities）。
// Part B：evaluation.js evaluators/capabilities 子视图生命周期——评估器
// 列表/过滤 + 能力目录独立顶导视图/详情 modal/复制新建（能力目录生成 + 新版本号强制 + 自动使用
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
  // plan-261115 T1：实验执行控制器（evaluation-executions.js）在 evaluation.js
  // 加载时即创建，实验视图会调用 listExecutions；默认空首页夹具
  const execParamsHistory = [];
  let execListPage = { items: [], next_cursor: null };
  let submitBehavior = 'ok';
  // Part I（plan-261116 T6）：模型页 mock 状态——GET 投影按 T5 新字段形状
  // 模拟；置 null 模拟投影加载失败
  let jmProjection = { judge_ready: true, supply_source: 'provider',
    effective_model: 'gpt-provider-model', env_baseline_model: 'env-model-x',
    provider_base_url_host: 'api.example.com', default_provider_id: 'prov-1',
    default_provider_name: 'Prov One', default_provider_model: 'gpt-provider-model',
    default_provider_missing: false, config_version: 3 };
  let jmPatchResult = { config: { evaluation_judge_provider_id: 'prov-1' }, version: 4,
    overridden_fields: ['evaluation_judge_provider_id'], updated_at: '', updated_by: 'n-agent' };
  let jmPatchBehavior = 'ok';
  const jmPatchCalls = [];
  // Part I（plan-261116 T6）：Provider 注册表 mock 状态——评估页复用
  // /chat/providers CRUD；activateProvider 仅记录，收尾断言零调用
  let providers = [
    { id: 'prov-1', name: 'Prov One', provider_type: 'openai-compatible',
      base_url: 'https://api.example.com/v1', model: 'gpt-provider-model',
      api_key_present: true, supports_vision: true, is_active: false },
    { id: 'prov-2', name: 'Claude One', provider_type: 'anthropic',
      base_url: 'https://api.anthropic.com', model: 'claude-x',
      api_key_present: true, supports_vision: false, is_active: false },
    { id: 'prov-3', name: 'Active One', provider_type: 'openai-compatible',
      base_url: 'https://api.two.com/v1', model: 'gpt-two',
      api_key_present: false, supports_vision: false, is_active: true },
  ];
  let providersFail = false;
  let providerBehavior = 'ok';
  const providerCalls = [];
  const activateCalls = [];
  let confirmAnswer = true;
  const confirmCalls = [];
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
    getDataset: async (id, version) => {
      calls.push(['getDataset', id, version]);
      return { dataset_id: id, dataset_version: version, target_kind: 'agent_run',
        dataset_hash: 'hash-' + version, cases: [{ case_id: 'c-1' }] };
    },
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
    listExecutions: async params => {
      calls.push(['listExecutions']);
      execParamsHistory.push(params);
      return execListPage;
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
    getJudgeModel: async () => {
      calls.push(['getJudgeModel']);
      if (jmProjection === null) throw new Error('request_failed');
      return jmProjection;
    },
    patchConfig: async payload => {
      calls.push(['patchConfig']);
      jmPatchCalls.push(payload);
      if (jmPatchBehavior === 'conflict') throw new Error('conflict');
      if (jmPatchBehavior === 'config_invalid') throw new Error('config_invalid');
      if (jmPatchBehavior === 'network') throw new Error('request_failed');
      // 模拟服务端：PATCH 成功后 evaluation config 落库，judge-model 投影的
      // default_provider_id/config_version 同步推进（badge/CAS 依据）
      const nextProviderId = payload && payload.patch
        && typeof payload.patch.evaluation_judge_provider_id === 'string'
        ? payload.patch.evaluation_judge_provider_id : null;
      if (nextProviderId !== null) {
        jmProjection = Object.assign({}, jmProjection,
          { default_provider_id: nextProviderId, config_version: jmPatchResult.version });
      }
      return jmPatchResult;
    },
  };
  // Part I（plan-261116 T6）：Provider 注册表方法挂 NAGENT.api 顶层
  //（与 management-api.js 暴露面一致：list/create/update/delete/activate）
  const providerApi = {
    listProviders: async () => {
      providerCalls.push(['listProviders']);
      if (providersFail) throw new Error('request_failed');
      return JSON.parse(JSON.stringify(providers));
    },
    createProvider: async payload => {
      providerCalls.push(['createProvider', payload]);
      if (providerBehavior !== 'ok') throw new Error(providerBehavior);
      const created = Object.assign(
        { id: 'prov-new-' + providerCalls.length },
        payload,
        { api_key_present: true },
      );
      providers = providers.concat([created]);
      return JSON.parse(JSON.stringify(created));
    },
    updateProvider: async (id, payload) => {
      providerCalls.push(['updateProvider', id, payload]);
      if (providerBehavior !== 'ok') throw new Error(providerBehavior);
      providers = providers.map(p => p.id === id
        ? Object.assign({}, p, payload,
          { api_key_present: payload.api_key === '' ? false : (p.api_key_present || typeof payload.api_key === 'string') })
        : p);
      return JSON.parse(JSON.stringify(providers.find(p => p.id === id)));
    },
    deleteProvider: async id => {
      providerCalls.push(['deleteProvider', id]);
      if (providerBehavior !== 'ok') throw new Error(providerBehavior);
      providers = providers.filter(p => p.id !== id);
      return null;
    },
    activateProvider: async id => {
      providerCalls.push(['activateProvider', id]);
      activateCalls.push(id);
      return null;
    },
  };
  const context = {
    Node: Element,
    document: {
      getElementById: () => root,
      createElement: tag => { createdTags.push(tag); return new Element(tag); },
      // Provider modal 的 label 文本节点（label 置于控件上方布局）
      createTextNode: text => ({ textContent: String(text), children: [] }),
      body,
      get activeElement() { return getActiveElement(); },
    },
    NAGENT: {
      api: Object.assign({ evaluations: api }, providerApi),
      modal: {
        alert: message => { alerts.push(String(message)); },
        confirm: message => {
          confirmCalls.push(String(message));
          return Promise.resolve(confirmAnswer);
        },
      },
    },
  };
  context.window = context;
  // 执行控制器在 evaluation.js 模块加载时即 createController，必须先加载
  // evaluation-executions.js，使 namespace.evaluationExecutions 已存在
  vm.runInNewContext(fs.readFileSync('app/interfaces/http/static/evaluation-executions.js', 'utf8'), context);
  vm.runInNewContext(fs.readFileSync('app/interfaces/http/static/evaluation.js', 'utf8'), context);
  const moduleApi = context.NAGENT.evaluations;
  // Provider CRUD 挂 NAGENT.api 顶层（plan-261116 T6），竞态覆写需针对
  // 顶层对象，evaluation.js modal 读取的是 api.updateProvider 而非
  // api.evaluations.updateProvider
  const topLevelApi = context.NAGENT.api;
  const state = view => ({ activeTab: 'evaluations-' + view, route: { scope: view } });
  const findButton = (label, scope) =>
    find(scope || root, node => node.textContent === label && node.handlers.click);

  // 1) 空列表占位；能力目录已拆为独立顶导入口（capabilities 视图），
  //    评估器页不再内嵌目录（judge_configured=false 时 llm_judge 条目未就绪；
  //    目录页区分可配置/执行已就绪/未配置 Judge）
  await moduleApi.refresh(state('evaluators'));
  assert.match(root.textContent, /暂无评估器/);
  assert.doesNotMatch(root.textContent, /能力目录（仅目录内组合可注册自动评估器）/,
    '评估器页不再内嵌能力目录区块');
  await moduleApi.refresh(state('capabilities'));
  assert.match(root.textContent, /未配置 Judge/, 'llm_judge 条目须显示未配置 Judge');
  assert.match(root.textContent, /执行已就绪/);
  assert.match(root.textContent, /可配置/);
  assert.ok(!find(root, node => node.textContent === '新建评估器' && node.handlers.click),
    '能力目录为只读视图，不提供新建评估器入口');
  // 回到评估器视图继续后续过滤/列表用例
  await moduleApi.refresh(state('evaluators'));

  // 2) 过滤参数进入 listEvaluators（spec 4.2：按目标种类/方法过滤）
  const methodFilter = find(root, node => node.name === 'evaluator_method_filter' && node.handlers.change);
  assert.ok(methodFilter, '缺少方法过滤控件');
  methodFilter.value = 'deterministic';
  listParamsHistory.length = 0;
  await methodFilter.handlers.change();
  // 跨 vm realm 数组/对象原型不同，assert.deepEqual 直接比较必失败；
  // 先 JSON 归一成宿主 realm 普通对象再比较（与 navigation harness 同）。
  assert.deepEqual(JSON.parse(JSON.stringify(listParamsHistory.at(-1))),
    { limit: 20, method: 'deterministic' });
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

  // 5) 同指标多版本同时启用：不展示额外备注
  listResult = { items: [
    { definition_id: 'kb.recall_at_5', version: 'v1', metric_name: 'kb.recall_at_5',
      target_kind: 'retrieval', method: 'deterministic',
      usage: { online_enabled: true, revision: 2 } },
    { definition_id: 'kb.recall_at_5', version: 'v2', metric_name: 'kb.recall_at_5',
      target_kind: 'retrieval', method: 'deterministic',
      usage: { online_enabled: true, revision: 1 } },
  ], next_cursor: null };
  await moduleApi.refresh(state('evaluators'));
  assert.doesNotMatch(root.textContent, /同一指标存在多个已启用版本/);

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
  //     不发请求；空 judge_model 不再阻塞提交（T6 去掉旧前端门禁，省略
  //     字段提交由后端归一为 None）；显式输入仍严格校验并提交
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
  // T6：空 judge_model 不再阻塞提交——未触碰且无有效供给时省略字段提交
  //（后端归一为 None，跟随默认供给）
  find(judgeForm, node => node.name === 'judge_model').value = '';
  await judgeForm.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 1, '空 judge_model 不再阻塞提交');
  assert.ok(!('judge_model' in createdPayloads[0]), '空 judge_model 省略字段提交');
  assert.equal(body.children.length, 0, '注册成功后弹窗关闭');
  // 显式输入仍提交（原始值不 trim；显式声明的模型被冻结）
  await moduleApi.refresh(state('evaluators'));
  await findButton('新建评估器').handlers.click();
  const judgeForm2 = find(body, node => node.tag === 'form');
  const judgeMethod2 = find(judgeForm2, node => node.name === 'method');
  judgeMethod2.value = 'llm_judge';
  judgeMethod2.handlers.change();
  find(judgeForm2, node => node.name === 'definition_id').value = 'judge.metric';
  find(judgeForm2, node => node.name === 'version').value = 'v1';
  find(judgeForm2, node => node.name === 'judge_model').value = 'gpt-x';
  find(judgeForm2, node => node.name === 'rubric').value = '按事实核对';
  find(judgeForm2, node => node.name === 'temperature').value = '1';
  await judgeForm2.handlers.submit({ preventDefault() {} });
  assert.equal(createdPayloads.length, 2, '合法 llm_judge 提交应发出请求');
  assert.deepEqual(JSON.parse(JSON.stringify(createdPayloads[1])), {
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
  assert.deepEqual(annParams(), { state: 'all', reason: 'low_score', threshold: '0.5', limit: 20 });

  // C2) 筛选下拉（原生 select，共享 .select-control）：状态下拉
  //     全部/待标注/已标注，原因下拉 低分/争议/异常（无 all，后端
  //     reason=all 会 422），两个下拉同处一个 filterBar 筛选行
  const filterSelect = name => find(root, node => node.tag === 'select' && node.name === name);
  const selectOptions = selectNode => findAll(selectNode, node => node.tag === 'option')
    .map(option => option.value);
  const annotationFilterRow = () => find(root, node => node.className === 'evaluations-filter');
  assert.ok(annotationFilterRow(), '两个筛选下拉同处一个 filterBar 筛选行');
  const stateSelect = filterSelect('annotation_state');
  assert.ok(stateSelect, '标注状态筛选须为原生 select');
  assert.equal(stateSelect.className, 'select-control', '状态下拉接入共享 .select-control');
  assert.ok(annotationFilterRow().children.includes(stateSelect.parentElement),
    '状态下拉位于筛选行 filterItem 内');
  assert.deepEqual(selectOptions(stateSelect), ['all', 'pending', 'annotated']);
  assert.equal(stateSelect.value, 'all', '默认 all 选中');
  annParamsHistory.length = 0;
  stateSelect.value = 'pending';
  await stateSelect.handlers.change();
  assert.deepEqual(annParams(), { state: 'pending', reason: 'low_score', threshold: '0.5', limit: 20 });
  const reasonSelect = filterSelect('annotation_reason');
  assert.ok(annotationFilterRow().children.includes(reasonSelect.parentElement),
    '原因下拉与状态下拉同一筛选行');
  assert.equal(reasonSelect.className, 'select-control', '原因下拉接入共享 .select-control');
  assert.deepEqual(selectOptions(reasonSelect), ['low_score', 'disputed', 'anomalous']);
  assert.ok(!selectOptions(reasonSelect).includes('all'), '原因下拉禁止造 all 项');
  assert.equal(reasonSelect.value, 'low_score', '默认 low_score 选中');
  reasonSelect.value = 'disputed';
  await reasonSelect.handlers.change();
  assert.equal(annParams().reason, 'disputed');
  // 重渲染后控件重建，按 name 重新查找：状态走完 annotated/all，原因切回 low_score
  const stateSelectAnnotated = filterSelect('annotation_state');
  stateSelectAnnotated.value = 'annotated';
  await stateSelectAnnotated.handlers.change();
  assert.equal(annParams().state, 'annotated');
  const stateSelectAll = filterSelect('annotation_state');
  stateSelectAll.value = 'all';
  await stateSelectAll.handlers.change();
  const reasonSelectRefreshed = filterSelect('annotation_reason');
  reasonSelectRefreshed.value = 'low_score';
  await reasonSelectRefreshed.handlers.change();
  assert.deepEqual(annParams(), { state: 'all', reason: 'low_score', threshold: '0.5', limit: 20 });
  // 阈值输入已移除：固定 0.5 随查询参数发送，页面不存在 threshold 控件
  assert.equal(find(root, node => node.name === 'annotation_threshold'), undefined,
    '低分阈值不再暴露可配置控件');

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
  assert.deepEqual(annParams(), { state: 'all', reason: 'low_score', threshold: '0.5', limit: 20 },
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
  // 操作按钮位于 Sector Header 右上角 panel-actions 区
  const annHeader = () => find(root, node => node.className === 'panel-actions');
  assert.ok(annHeader(), 'annotations 视图渲染 panel-actions 操作区');
  assert.equal(find(annHeader(), node => node.textContent.includes('导出当前页')), undefined,
    '操作区不再展示导出当前页备注');
  const headerExportBtn = find(annHeader(), node => node.tag === 'button'
    && node.textContent === '审核后导出数据集');
  assert.ok(headerExportBtn && headerExportBtn.handlers.click, '导出按钮在 header 且可点');
  assert.ok(!find(root, node => node.className === 'evaluations-annotations'
    && find(node, child => child.tag === 'button' && child.textContent === '审核后导出数据集')),
    'wrap 底部不再渲染导出按钮');
  await headerExportBtn.handlers.click();
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
  const headerDatasetsBtn = find(annHeader(), node => node.tag === 'button'
    && node.textContent === '查看数据集列表');
  assert.ok(headerDatasetsBtn && headerDatasetsBtn.handlers.click,
    '查看数据集列表按钮在 header 且可点');
  await headerDatasetsBtn.handlers.click();
  assert.match(root.textContent, /暂无数据集/, '导出后可进入数据集列表');

  // C7) 当前页导出合同：21 条队列按每页 20 切片，第 1 页点导出只传当前页
  // 20 条；入参本身不超过 20，弹窗不再出现截断提示（截断能力仍保留给
  // openExportModal 的其他调用方，本路径不触发）
  annQueue = { items: Array.from({ length: 21 }, (_, i) =>
    Object.assign({}, baseQueueItem, { source_result_id: 'res-' + i })), next_cursor: null };
  await moduleApi.refresh(state('annotations'));
  const c7Header = find(root, node => node.className === 'panel-actions');
  await find(c7Header, node => node.tag === 'button'
    && node.textContent === '审核后导出数据集').handlers.click();
  await new Promise(resolve => setImmediate(resolve));
  const c7Form = find(body, node => node.tag === 'form');
  assert.ok(c7Form, '导出弹窗打开');
  const c7CaseInputs = findAll(c7Form, node => node.name
    && node.name.indexOf('expected_output_') === 0);
  assert.equal(c7CaseInputs.length, 20, '弹窗金标输入数等于当前页切片 20 条');
  assert.doesNotMatch(body.textContent, /仅导出前 20 条/,
    '当前页导出入参最多 20 条，不出现截断提示');
  find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

  // -------------------------------------------------------------------------
  // Part D（Task 12 审查修复项 1/2）：导出弹窗异步防护 + 空队列不渲染导出入口
  // -------------------------------------------------------------------------
  const realGetAnnotation = api.getAnnotation;
  const pendingContexts = [];
  api.getAnnotation = () => new Promise(resolve => { pendingContexts.push(resolve); });

  // D1：空标注队列时操作按钮仍在 Sector Header——导出按钮禁用且无 click
  // handler（禁用而非隐藏，保持操作区结构稳定），数据集入口始终可点
  annQueue = { items: [], next_cursor: null };
  await moduleApi.refresh(state('annotations'));
  assert.match(root.textContent, /当前筛选暂无待标注结果/);
  const emptyHeader = find(root, node => node.className === 'panel-actions');
  const disabledExport = find(emptyHeader, node => node.tag === 'button'
    && node.textContent === '审核后导出数据集');
  assert.ok(disabledExport, '空队列仍在 header 渲染导出按钮占位');
  assert.equal(disabledExport.disabled, true, '空队列导出按钮禁用');
  assert.equal(disabledExport.handlers.click, undefined, '禁用按钮无 click handler');
  const headerDatasets = find(emptyHeader, node => node.tag === 'button'
    && node.textContent === '查看数据集列表');
  assert.ok(headerDatasets && headerDatasets.handlers.click,
    '查看数据集列表始终在 header 且可点');

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

  // E1c) 概览重构：列表接入标准 slice pager（空列表显第 1/1 页、按钮
  // 禁用占位）；备注从列表上方移到下方，按 Sector 分块（对齐安全-概览）。
  const hasClassName = (node, token) => typeof node.className === 'string'
    && node.className.split(/\s+/).includes(token);
  const panelTitles = () => findAll(root, node => node.tag === 'span'
    && hasClassName(node, 'panel-title')).map(node => node.textContent);
  assert.deepEqual(panelTitles(),
    ['评分趋势', '运行配置', '容量与预算'],
    '概览不套标题外壳面板，按 列表 Sector -> 备注 Sector 排序');
  let pagerInfos = findAll(root, node => hasClassName(node, 'evaluations-pager__info'));
  assert.equal(pagerInfos.length, 1, '无 Producer 心跳时仅评分趋势 pager');
  assert.equal(pagerInfos[0].textContent, '第 1/1 页', '空列表显第 1/1 页');
  const overviewPagerButton = label => find(root,
    node => node.tag === 'button' && node.textContent === label);
  assert.equal(overviewPagerButton('上一页').disabled, true, '空列表上一页禁用占位');
  assert.equal(overviewPagerButton('下一页').disabled, true, '空列表下一页禁用占位');
  // 备注保留但下移到列表 Sector 之后
  assert.match(root.textContent, /评估开关/);
  assert.match(root.textContent, /当日 Judge 调用（预算）/);
  assert.match(root.textContent, /证据快照保留/);

  // 25 个分组 -> 2 页 slice 分页：info/切片随翻页同步，回翻可返回首页
  analyticsResult = {
    snapshot_at: '2026-11-12T02:00:00+00:00',
    window: { from: '2026-11-05T00:00:00+00:00', to: '2026-11-12T02:00:00+00:00' },
    groups: Array.from({ length: 25 }, (_, i) => ({
      definition_id: 'm-' + i, definition_version: 'v1', provenance: 'automatic',
      output_type: 'number', total_count: i, count: 1, mean: 1, min: 1, max: 1,
      status_counts: { value: 1 }, daily_counts: {},
    })),
    coverage: { status: 'unknown', reason: '缺少应评估目标总数' },
  };
  await moduleApi.refresh(state('overview'));
  const pagerInfo = () => findAll(root, node => hasClassName(node, 'evaluations-pager__info'))[0];
  assert.equal(pagerInfo().textContent, '第 1/2 页');
  assert.equal(overviewPagerButton('下一页').disabled, false, '首页下一页可用');
  assert.match(root.textContent, /m-0/, '首页含第 1 条');
  assert.doesNotMatch(root.textContent, /m-24/, '首页不含末页条目');
  overviewPagerButton('下一页').handlers.click();
  assert.equal(pagerInfo().textContent, '第 2/2 页', '翻页后 info 同步');
  assert.match(root.textContent, /m-24/, '第二页含末页条目');
  assert.doesNotMatch(root.textContent, /m-0/, '第二页不含首页条目');
  assert.equal(overviewPagerButton('上一页').disabled, false, '第二页上一页可用');
  overviewPagerButton('上一页').handlers.click();
  assert.equal(pagerInfo().textContent, '第 1/2 页', '回翻返回首页');
  // 还原空分组，避免污染后续 Part 的 overview 默认 mock
  analyticsResult = {
    snapshot_at: '2026-11-12T02:00:00+00:00',
    window: { from: '2026-11-05T00:00:00+00:00', to: '2026-11-12T02:00:00+00:00' },
    buckets: [{ date: '2026-11-05', total: 0 }],
    groups: [],
    coverage: { status: 'unknown', reason: '缺少应评估目标总数' },
  };

  // E2) 结果筛选 + 游标分页：首页上一页禁用占位（标准 pager 不隐藏）；
  //     下一页携带 next_cursor；上一页从历史游标栈回退；过滤变更重置游标；
  //     "暂无翻页"提示移除
  //     （注意：响应在每次渲染时取数，翻页按钮捕获当次响应的 next_cursor，
  //     mock 需在点击前更新）
  const pagerButton = (label, scope) =>
    find(scope || root, node => node.tag === 'button' && node.textContent === label);
  const assertPagerState = (label, enabled) => {
    const node = pagerButton(label);
    assert.ok(node, 'pager 须始终渲染按钮: ' + label);
    if (enabled) {
      assert.ok(!node.disabled, label + ' 须可用');
    } else {
      assert.equal(node.disabled, true, label + ' 须禁用');
    }
  };
  const resultRow = {
    result_id: 'res-1', metric_name: 'kb.recall_at_5', provenance: 'automatic',
    status: 'value', passed: true, typed_value: 0.8, created_at: '2026-11-12T00:00:00+00:00',
  };
  resultsPage = { items: [resultRow], next_cursor: 'tok2' };
  await moduleApi.refresh(state('results'));
  assert.deepEqual(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))), { limit: 20 },
    '首页不得携带游标或过滤');
  assert.match(root.textContent, /res-1/);
  // 状态口径备注收进"结果"标题 hover tips（多行），列表下不再有备注块
  assert.ok(find(root, node => typeof node.className === 'string'
    && node.className.split(/\s+/).includes('panel-tips')
    && /质量不通过\/通过来自 boolean 或阈值判定/.test(node.textContent)
    && /unlinked 为目标无真实身份，不创建悬空 Score/.test(node.textContent)
    && node.textContent.includes('\n')),
    '结果页标题 hover tips 含多行状态口径');
  assert.equal(find(root, node => typeof node.className === 'string'
    && node.className.split(/\s+/).includes('evaluations-note')
    && /状态口径/.test(node.textContent)), undefined,
    '列表下不再渲染状态口径备注块');
  assert.doesNotMatch(root.textContent, /暂无翻页/, '固定 50 条"暂无翻页"提示须移除');
  assertPagerState('上一页', false);
  assertPagerState('下一页', true);
  assert.match(root.textContent, /第 1\/2 页/, 'pager 须展示统一页码 info');
  resultsPage = { items: [resultRow], next_cursor: 'tok3' };
  await findButton('下一页', root).handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, 'tok2', '下一页携带游标');
  resultsPage = { items: [resultRow], next_cursor: null };
  await findButton('下一页', root).handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, 'tok3');
  assertPagerState('下一页', false);
  await findButton('上一页', root).handlers.click();
  await flush();
  assert.equal(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))).cursor, 'tok2', '上一页从游标栈回退');
  await findButton('上一页', root).handlers.click();
  await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(resultsParamsHistory.at(-1))), { limit: 20 }, '回到首页不携带游标');
  assertPagerState('上一页', false);

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
    { limit: 20, created_from: '2026-11-01T00:00:00+00:00' }, '过滤变更重置游标');
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
    limit: 20,
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
    limit: 20,
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
      compareDatasetVersions: typeof compareDatasetVersions === 'function' ? compareDatasetVersions : undefined,
      groupDatasetItems: typeof groupDatasetItems === 'function' ? groupDatasetItems : undefined,
      pagerBar: typeof pagerBar === 'function' ? pagerBar : undefined,
      slicePage: typeof slicePage === 'function' ? slicePage : undefined,
      LIST_PAGE_SIZE: typeof LIST_PAGE_SIZE === 'number' ? LIST_PAGE_SIZE : undefined,
      REVIEW_WINDOW_LIMIT: typeof REVIEW_WINDOW_LIMIT === 'number' ? REVIEW_WINDOW_LIMIT : undefined,
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

  // G0c) 共享分页 helper：统一组件、每页 20、两模式文案、占位/禁用/回调
  assert.equal(typeof helpers.pagerBar, 'function', '缺少共享 helper: pagerBar');
  assert.equal(typeof helpers.slicePage, 'function', '缺少共享 helper: slicePage');
  assert.equal(helpers.LIST_PAGE_SIZE, 20, '评估域统一每页 20 条');
  assert.equal(helpers.REVIEW_WINDOW_LIMIT, 50, '待复核窗口常量保持 50');
  {
    // 统一 info 文案 第 N/M 页（斜杠无空格），两按钮始终占位
    const sliceBar = helpers.pagerBar({ pageIndex: 0, hasPrev: false, hasNext: true,
      totalPages: 3, onPrev() {}, onNext() {} });
    assert.equal(sliceBar.className, 'evaluations-pager');
    assert.match(sliceBar.textContent, /第 1\/3 页/);
    const sliceButtons = findAll(sliceBar, n => n.tag === 'button');
    assert.equal(sliceButtons.length, 2, '翻页按钮始终占位');
    assert.deepEqual(sliceButtons.map(b => b.textContent), ['上一页', '下一页']);
    assert.equal(sliceButtons[0].disabled, true, '首页上一页禁用');
    assert.equal(sliceButtons[0].handlers.click, undefined, '禁用按钮无 click handler');
    assert.equal(sliceButtons[1].disabled, false);
    // cursor 模式：无 totalPages，分母按已知页数下界推断——
    // hasNext=false 已到底，分母=当前页码；hasNext=true 分母=当前页码+1
    const cursorBar = helpers.pagerBar({ pageIndex: 1, hasPrev: true, hasNext: false,
      onPrev() {}, onNext() {} });
    assert.match(cursorBar.textContent, /第 2\/2 页/);
    const cursorMoreBar = helpers.pagerBar({ pageIndex: 0, hasPrev: false, hasNext: true,
      onPrev() {}, onNext() {} });
    assert.match(cursorMoreBar.textContent, /第 1\/2 页/, '有下一页时分母表达已知至少两页');
    const cursorButtons = findAll(cursorBar, n => n.tag === 'button');
    assert.equal(cursorButtons[1].disabled, true, '末页下一页禁用');
    assert.equal(cursorButtons[1].handlers.click, undefined);
    // 回调接线
    let clicked = null;
    const bar = helpers.pagerBar({ pageIndex: 0, hasPrev: false, hasNext: true,
      totalPages: 2, onPrev() { clicked = 'prev'; }, onNext() { clicked = 'next'; } });
    findAll(bar, n => n.tag === 'button' && n.textContent === '下一页')[0].handlers.click();
    assert.equal(clicked, 'next');
    findAll(bar, n => n.tag === 'button' && n.textContent === '上一页')[0]; // 禁用项存在
  }
  {
    // slicePage：切片边界、空数组总页数为 1（杜绝 1/0）、页码夹紧由调用方处理
    const empty = helpers.slicePage([], 0);
    assert.equal(empty.totalPages, 1);
    assert.deepEqual(empty.pageItems, []);
    const twenty = Array.from({ length: 20 }, (_, i) => i);
    assert.equal(helpers.slicePage(twenty, 0).totalPages, 1);
    const twentyOne = Array.from({ length: 21 }, (_, i) => i);
    const first = helpers.slicePage(twentyOne, 0);
    assert.equal(first.totalPages, 2);
    assert.equal(first.pageItems.length, 20);
    const second = helpers.slicePage(twentyOne, 1);
    assert.deepEqual(second.pageItems, [20]);
    const fortyOne = Array.from({ length: 41 }, (_, i) => i);
    assert.equal(helpers.slicePage(fortyOne, 0).totalPages, 3);
    assert.equal(helpers.slicePage(fortyOne, 2).pageItems.length, 1);
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

  // G4) compareDatasetVersions：版本名自然序（spec-261114 组件 1）
  for (const name of ['compareDatasetVersions', 'groupDatasetItems']) {
    assert.equal(typeof helpers[name], 'function', '缺少数据集聚合 helper: ' + name);
  }
  const cmp = helpers.compareDatasetVersions;
  const sign = value => (value < 0 ? -1 : value > 0 ? 1 : 0);
  // 字典序失效场景：1 < 2 < 10 < v1 < v2 < v10
  const chain = [['1', '2'], ['2', '10'], ['10', 'v1'], ['v1', 'v2'], ['v2', 'v10']];
  for (const [low, high] of chain) {
    assert.equal(sign(cmp(low, high)), -1, low + ' 应排在 ' + high + ' 之前');
    assert.equal(sign(cmp(high, low)), 1, '反对称性: ' + high + ' > ' + low);
  }
  assert.deepEqual(
    ['v10', 'v2', '10', '2', '1', 'v1'].sort(cmp),
    ['1', '2', '10', 'v1', 'v2', 'v10'],
    '自然序整体排序结果确定');
  // 前缀规则：较短者在前
  assert.equal(sign(cmp('v1', 'v1-beta')), -1);
  assert.equal(sign(cmp('v1-beta', 'v1')), 1);
  // 大整数精度：禁止 Number 转换，按长度+字符串比较
  assert.equal(sign(cmp('v9007199254740992', 'v9007199254740993')), -1);
  // 前导零归一后相等，原串 UTF-16 兜底，仅完全相同返回 0
  assert.equal(sign(cmp('v01', 'v1')), -1);
  assert.equal(sign(cmp('v1', 'v01')), 1);
  assert.equal(cmp('v1', 'v1'), 0);
  // 传递性抽查
  assert.ok(cmp('1', 'v1') < 0 && cmp('v1', 'v1-beta') < 0 && cmp('1', 'v1-beta') < 0);
  // 容忍空白/标点/非 ASCII，不抛异常且结果确定
  assert.equal(sign(cmp('版本 1', '版本 2')), -1);
  assert.notEqual(cmp('a-b', 'a.b'), 0);
  assert.doesNotThrow(() => { cmp('  ', '标点.版本-2'); cmp('', '1'); });
  // UTF-16 确定性：大小写、空输入、标点序、全角数字不按 ASCII 数值块处理
  assert.equal(sign(cmp('A', 'a')), -1);
  assert.equal(sign(cmp('', '1')), -1);
  assert.equal(sign(cmp('a-b', 'a.b')), -1);
  assert.equal(sign(cmp('版本 １', '版本 ２')), -1, '全角数字属非数字块，按 UTF-16 码元序');
  assert.equal(sign(cmp('v0', 'v00')), -1, '数字块归一相等时原串兜底');
  // 自反/反对称/三元传递性
  for (const v of ['1', 'v1', 'v01', 'a.b', '版本 １', '']) assert.equal(cmp(v, v), 0);
  assert.ok(cmp('1', 'v1') < 0 && cmp('v1', 'v10') < 0 && cmp('1', 'v10') < 0);

  // G5) groupDatasetItems：保序分组/最新判定/不可变/原型键安全（spec 组件 2）
  const groupItems = helpers.groupDatasetItems;
  const datasetItem = (id, version, extra) => Object.assign({
    dataset_id: id, dataset_version: version, target_kind: 'agent_run',
    case_count: 1, status: 'valid',
  }, extra || {});
  const interleaved = [
    datasetItem('ds-b', 'v1'), datasetItem('ds-a', 'v10'), datasetItem('ds-b', 'v2'),
    datasetItem('ds-a', 'v2'), datasetItem('ds-b', 'v10'), datasetItem('ds-a', 'v1'),
  ];
  const groups = groupItems(interleaved);
  assert.deepEqual(Array.from(groups, g => g.dataset_id), ['ds-b', 'ds-a'], '按首次出现顺序保序');
  const groupA = groups.find(g => g.dataset_id === 'ds-a');
  assert.equal(groupA.latest.dataset_version, 'v10', 'latest 取自然序最大');
  assert.deepEqual(Array.from(groupA.history, i => i.dataset_version), ['v2', 'v1'], 'history 新到旧且不含 latest');
  // 数字版本组 latest=4
  const numeric = groupItems([datasetItem('ds-n', '1'), datasetItem('ds-n', '4'), datasetItem('ds-n', '2'), datasetItem('ds-n', '3')]);
  assert.equal(numeric[0].latest.dataset_version, '4');
  assert.deepEqual(Array.from(numeric[0].history, i => i.dataset_version), ['3', '2', '1']);
  // 反转输入结果不变
  const reversed = groupItems(interleaved.slice().reverse());
  const reversedA = reversed.find(g => g.dataset_id === 'ds-a');
  assert.equal(reversedA.latest.dataset_version, 'v10');
  assert.deepEqual(Array.from(reversedA.history, i => i.dataset_version), ['v2', 'v1']);
  // 单版本 history 为空数组
  assert.equal(groupItems([datasetItem('solo', 'v1')])[0].history.length, 0);
  // 区分大小写，不串组
  assert.equal(groupItems([datasetItem('DS', 'v1'), datasetItem('ds', 'v1')]).length, 2);
  // __proto__ 目录名不与对象原型键冲突、不污染原型
  const protoGroups = groupItems([datasetItem('__proto__', 'v1'), datasetItem('__proto__', 'v2'), datasetItem('normal', 'v1')]);
  assert.equal(protoGroups.length, 2);
  assert.equal(protoGroups[0].dataset_id, '__proto__');
  assert.equal(protoGroups[0].latest.dataset_version, 'v2');
  assert.equal(Object.prototype.dataset_id, undefined, '禁止原型污染');
  // invalid 版本保留并参与排序
  const invalidGroup = groupItems([datasetItem('ds-i', 'v1', { status: 'invalid', case_count: null }), datasetItem('ds-i', 'v2')]);
  assert.equal(invalidGroup[0].latest.dataset_version, 'v2');
  assert.equal(invalidGroup[0].history[0].status, 'invalid');
  // 输入不可变：冻结后不抛错，顺序与字段不变
  const frozenInput = [datasetItem('ds-f', 'v2'), datasetItem('ds-f', 'v1')];
  frozenInput.forEach(Object.freeze);
  Object.freeze(frozenInput);
  let frozenGroups;
  assert.doesNotThrow(() => { frozenGroups = groupItems(frozenInput); });
  assert.deepEqual(frozenInput.map(i => i.dataset_version), ['v2', 'v1'], '禁止原地排序输入');
  // 空输入、成员引用身份保持（latest/history 是原 item，不复制不改字段）
  assert.deepEqual(Array.from(groupItems([])), []);
  assert.equal(frozenGroups[0].latest, frozenInput[0], 'latest 保持原 item 引用');
  assert.equal(frozenGroups[0].history[0], frozenInput[1], 'history 成员保持原 item 引用');
  assert.equal(frozenGroups[0].history[0].status, 'valid');
  assert.equal(frozenGroups[0].history[0].case_count, 1);

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

  // G1b) 数据集按 dataset_id 聚合：主表每组一行、latest 自然序、历史置灰态
  const realGetDataset = api.getDataset;
  api.listDatasets = async () => ({ items: [
    { dataset_id: 'ds-multi', dataset_version: 'v1', target_kind: 'agent_run', case_count: 3, status: 'valid' },
    { dataset_id: 'ds-solo', dataset_version: 'v1', target_kind: 'agent_run', case_count: 5, status: 'valid' },
    { dataset_id: 'ds-multi', dataset_version: 'v10', target_kind: 'agent_run', case_count: 9, status: 'valid' },
    { dataset_id: 'ds-num', dataset_version: '1', target_kind: 'agent_run', case_count: 1, status: 'valid' },
    { dataset_id: 'ds-multi', dataset_version: 'v2', target_kind: 'agent_run', case_count: 7, status: 'valid' },
    { dataset_id: 'ds-num', dataset_version: '4', target_kind: 'retrieval', case_count: 4, status: 'valid' },
    { dataset_id: 'ds-num', dataset_version: '2', target_kind: 'retrieval', case_count: 2, status: 'valid' },
    { dataset_id: '<b>escape</b>', dataset_version: 'v2', target_kind: 'agent_run', case_count: 2, status: 'valid' },
    { dataset_id: '<b>escape</b>', dataset_version: 'v1', target_kind: 'agent_run', case_count: 1, status: 'valid' },
    { dataset_id: 'ds-bad', dataset_version: 'v3', target_kind: 'agent_run', case_count: null, status: 'invalid' },
    { dataset_id: 'ds-bad', dataset_version: 'v2', target_kind: 'agent_run', case_count: 8, status: 'valid' },
    { dataset_id: 'ds-bad', dataset_version: 'v1', target_kind: 'agent_run', case_count: 2, status: 'valid' },
    { dataset_id: 'ds-bad', dataset_version: 'v0', target_kind: 'agent_run', case_count: null, status: 'invalid' },
  ] });
  await moduleApi.refresh(state('datasets'));
  {
    const datasetTable = find(root, node => node.tag === 'table');
    const headers = findAll(datasetTable, node => node.tag === 'th').map(th => th.textContent);
    assert.deepEqual(headers, ['数据集 ID', '版本', '目标', '用例数', '校验', '操作'], '六列结构保持');
    const rows = findAll(datasetTable, node => node.tag === 'tr' && node.parentElement.tag === 'tbody');
    assert.equal(rows.length, 5, '五个 dataset_id 各一行（13 个版本聚合为 5 行）');
    const rowText = rows.map(row => row.textContent);
    assert.ok(rowText.some(text => /ds-multi/.test(text) && /v10/.test(text) && !/v2/.test(text)),
      'ds-multi 行只显示 latest=v10');
    assert.ok(rowText.some(text => /<b>escape<\/b>/.test(text) && /v2/.test(text)),
      'HTML 特殊字符以文本显示，不解析为标签，latest 为 v2');
    assert.ok(!createdTags.includes('b') && !createdTags.includes('script'), '禁止创建注入标签节点');
    // 精确单元格：ds-num 第二列固定为 4，避免整行正则把别的 4 误当版本
    const numRow = rows.find(row => /ds-num/.test(row.textContent));
    const numCells = findAll(numRow, node => node.tag === 'td');
    assert.equal(numCells[1].textContent, '4', 'ds-num latest 版本列为 4');
    assert.equal(numCells[2].textContent, 'retrieval');
    // invalid latest：校验列如实显示，null 用例数回落 - 且保留 numeric 类
    const badRow = rows.find(row => /ds-bad/.test(row.textContent));
    const badCells = findAll(badRow, node => node.tag === 'td');
    assert.equal(badCells[1].textContent, 'v3', 'ds-bad 自然序 latest 为 v3');
    assert.equal(badCells[3].textContent, '-');
    assert.equal(badCells[3].className, 'document-table__numeric');
    assert.equal(badCells[4].textContent, '校验失败');
    // 多版本组有可点历史按钮，单版本组为置灰按钮（findButton 忽略无 handler 节点，须直接查 DOM）
    const historyButtons = findAll(datasetTable, node => node.tag === 'button' && node.textContent === '历史');
    assert.equal(historyButtons.length, 5, '每行常驻历史按钮，置灰不隐藏');
    const enabledHistory = historyButtons.filter(btn => btn.handlers.click);
    const disabledHistory = historyButtons.filter(btn => !btn.handlers.click);
    assert.equal(enabledHistory.length, 4, 'ds-multi/ds-num/escape/ds-bad 四组有老版本');
    assert.equal(disabledHistory.length, 1, 'ds-solo 单版本置灰');
    disabledHistory.forEach(btn => assert.equal(btn.disabled, true, '置灰按钮声明 disabled'));
    assert.ok(findButton('详情', datasetTable), '详情按钮保持可点击');
  }

  // G1c) 历史 Modal：仅老版本/新到旧/无新请求/详情类弹窗生命周期
  const historyBackdrop = () => find(body, node => typeof node.className === 'string'
    && node.className.split(/\s+/).includes('modal-backdrop')
    && /数据集历史 - ds-multi/.test(node.textContent));
  const anyBackdrop = () => find(body, node => typeof node.className === 'string'
    && node.className.split(/\s+/).includes('modal-backdrop'));
  const multiHistoryBtn = findAll(root, node => node.tag === 'button' && node.textContent === '历史')
    .find(btn => btn.handlers.click);
  calls.length = 0;
  await multiHistoryBtn.handlers.click();
  {
    assert.equal(calls.length, 0, '打开历史 Modal 不产生任何 API 请求');
    const backdrop = historyBackdrop();
    assert.ok(backdrop, '历史 Modal 挂载到 body');
    const modalHeaders = findAll(backdrop, node => node.tag === 'th').map(th => th.textContent);
    assert.deepEqual(modalHeaders, ['版本', '目标', '用例数', '校验', '操作'], '历史表五列');
    const modalTable = find(backdrop, node => node.tag === 'table');
    const modalRows = findAll(modalTable, node => node.tag === 'tr' && node.parentElement.tag === 'tbody');
    assert.equal(modalRows.length, 2, '历史只含老版本，不含 latest=v10');
    assert.match(modalRows[0].textContent, /v2/, '新到旧：v2 在前');
    assert.match(modalRows[1].textContent, /v1/);
    assert.equal(backdrop.handlers.click, undefined, '详情类 Modal 不响应 backdrop 空白点击');
    // 行内详情：先关历史弹窗，再以正确 (id, version) 请求详情；
    // openDatasetDetail 成功后挂载自己的「数据集详情」Modal。
    let backdropAtCall = '未调用';
    api.getDataset = async (id, version) => {
      backdropAtCall = historyBackdrop();
      return realGetDataset(id, version);
    };
    const rowDetail = findButton('详情', backdrop);
    rowDetail.handlers.click();
    await flush(); // handler 不返回 openDatasetDetail 的 Promise，必须排空异步任务
    assert.equal(backdropAtCall, undefined, 'getDataset 发起时历史弹窗已移除（先关闭再请求）');
    assert.deepEqual(calls[0], ['getDataset', 'ds-multi', 'v2'], '历史行详情携带正确版本');
    assert.equal(historyBackdrop(), undefined, '历史弹窗先关闭（按标题判定，不与详情弹窗混淆）');
    const detailBackdrop = find(body, node => typeof node.className === 'string'
      && node.className.split(/\s+/).includes('modal-backdrop')
      && /数据集详情/.test(node.textContent));
    assert.ok(detailBackdrop, 'openDatasetDetail 打开独立详情 Modal');
    find(detailBackdrop, node => node.textContent === '×' && node.handlers.click).handlers.click();
    assert.equal(anyBackdrop(), undefined, '详情 Modal 已关闭，body 无残留弹窗');
    api.getDataset = realGetDataset;
  }
  // 历史第二行 v1 逐次重开点击，同样先关闭再请求
  await multiHistoryBtn.handlers.click();
  {
    const backdrop = historyBackdrop();
    const detailButtons = findAll(backdrop, node => node.tag === 'button' && node.textContent === '详情');
    detailButtons[1].handlers.click();
    await flush();
    assert.deepEqual(calls.at(-1), ['getDataset', 'ds-multi', 'v1'], 'v1 行详情携带正确版本');
    const detailBackdrop = find(body, node => typeof node.className === 'string'
      && node.className.split(/\s+/).includes('modal-backdrop') && /数据集详情/.test(node.textContent));
    find(detailBackdrop, node => node.textContent === '×' && node.handlers.click).handlers.click();
    assert.equal(anyBackdrop(), undefined);
  }
  // 特殊 ID 历史：标题与版本文本原样显示，无注入节点。
  // 行序为 ds-multi/ds-num/escape/ds-bad，可点历史按钮索引 2 即 escape 行。
  await moduleApi.refresh(state('datasets'));
  const escapeHistoryBtn = findAll(root, node => node.tag === 'button' && node.textContent === '历史')
    .filter(btn => btn.handlers.click)[2];
  await escapeHistoryBtn.handlers.click();
  {
    const backdrop = find(body, node => typeof node.className === 'string'
      && node.className.split(/\s+/).includes('modal-backdrop') && /数据集历史 -/.test(node.textContent));
    assert.match(backdrop.textContent, /数据集历史 - <b>escape<\/b>/, '特殊字符标题按文本显示');
    const modalRows = findAll(backdrop, node => node.tag === 'tr' && node.parentElement.tag === 'tbody');
    assert.equal(modalRows.length, 1, 'escape 历史仅 v1 一个老版本');
    assert.match(modalRows[0].textContent, /v1/);
    assert.ok(!createdTags.includes('b') && !createdTags.includes('script'));
    find(backdrop, node => node.textContent === '×' && node.handlers.click).handlers.click();
    assert.equal(anyBackdrop(), undefined);
  }
  // 重新打开：× 路径整节点移除（mountDetailModal 的 closeBtn 直接
  // closeBackdrop(frame.backdrop)），关闭后 body 干净
  await multiHistoryBtn.handlers.click();
  let backdrop = historyBackdrop();
  assert.ok(backdrop, '历史 Modal 可重新打开');
  find(backdrop, node => node.textContent === '×' && node.handlers.click).handlers.click();
  assert.equal(historyBackdrop(), undefined, '× 关闭历史 Modal');
  assert.equal(anyBackdrop(), undefined, '× 整节点移除，body 无残留弹窗');
  // 底部「关闭」合同：formActions 取消按钮存在且点击后历史内容消失。
  // 桩环境限制：classList.contains 恒 true 使 currentBackdropOf(form) 返回
  // form 自身，该路径只移除 form、backdrop 外壳仍由 openBackdrop 引用
  // （真实 DOM 会正确上溯外壳并整节点移除，真实行为由 verify 浏览器验收确认）。
  // 因此把「关闭」安排为 deactivate 前最后一个弹窗交互，由 deactivate 清壳。
  await multiHistoryBtn.handlers.click();
  backdrop = historyBackdrop();
  const closeBtn = find(backdrop, node => node.tag === 'button'
    && node.textContent === '关闭' && node.handlers.click);
  assert.ok(closeBtn, 'providers-form__actions 提供关闭按钮');
  const callsBeforeClose = calls.length;
  closeBtn.handlers.click();
  assert.equal(historyBackdrop(), undefined, '关闭按钮退出 Modal（标题内容已随表单移除）');
  assert.equal(calls.length, callsBeforeClose, '关闭不产生请求');
  // 切离视图清理：deactivate 经 openBackdrop 移除残留外壳
  moduleApi.deactivate();
  assert.equal(historyBackdrop(), undefined, 'deactivate 移除 Modal');
  assert.equal(anyBackdrop(), undefined, 'deactivate 后 body 无残留弹窗');
  // 历史详情失败：复用既有 alertMessage 提示，无弹窗残留、不自动请求 latest
  for (const [code, fragment] of [['dataset_invalid', '数据集校验失败'], ['gone', '数据集详情加载失败']]) {
    calls.length = 0;
    await moduleApi.refresh(state('datasets'));
    const btn = findAll(root, node => node.tag === 'button' && node.textContent === '历史')
      .find(b => b.handlers.click);
    await btn.handlers.click();
    const hBackdrop = find(body, node => typeof node.className === 'string'
      && node.className.split(/\s+/).includes('modal-backdrop') && /数据集历史 -/.test(node.textContent));
    let failCalls = 0;
    api.getDataset = async () => { failCalls += 1; throw new Error(code); };
    const alertsBefore = alerts.length;
    await findButton('详情', hBackdrop).handlers.click();
    await flush();
    assert.equal(alerts.length, alertsBefore + 1, '失败只提示一次: ' + code);
    assert.match(alerts.at(-1), new RegExp(fragment));
    assert.equal(anyBackdrop(), undefined, '失败后无弹窗残留: ' + code);
    assert.equal(failCalls, 1, '不自动重试或跳转 latest: ' + code);
    calls.length = 0;
    api.getDataset = realGetDataset;
  }
  // 迟到响应防护：旧请求后 resolve 不得覆盖新视图结果；deactivate 后响应不渲染
  {
    const deferred = () => {
      const d = {};
      d.promise = new Promise((resolve, reject) => { d.resolve = resolve; d.reject = reject; });
      return d;
    };
    const d1 = deferred();
    const d2 = deferred();
    api.listDatasets = () => d1.promise;
    const p1 = moduleApi.refresh(state('datasets'));
    api.listDatasets = () => d2.promise;
    const p2 = moduleApi.refresh(state('datasets'));
    d1.resolve({ items: [{ dataset_id: 'stale-ds', dataset_version: 'v1',
      target_kind: 'agent_run', case_count: 1, status: 'valid' }] });
    await flush();
    d2.resolve({ items: [] });
    await Promise.all([p1, p2]);
    assert.match(root.textContent, /暂无数据集/, '旧响应不得覆盖新结果');
    assert.ok(!/stale-ds/.test(root.textContent), 'stale 数据集不渲染');

    const d3 = deferred();
    api.listDatasets = () => d3.promise;
    const p3 = moduleApi.refresh(state('datasets'));
    await flush();
    moduleApi.deactivate();
    d3.resolve({ items: [{ dataset_id: 'late-ds', dataset_version: 'v1',
      target_kind: 'agent_run', case_count: 1, status: 'valid' }] });
    await p3;
    await flush();
    assert.ok(!/late-ds/.test(root.textContent), 'deactivate 后迟到响应不渲染');
    assert.equal(anyBackdrop(), undefined);
  }
  api.listDatasets = realListDatasets;
  api.getDataset = realGetDataset;

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
  assert.equal(enabledControl.className, 'select-control', '评估开关下拉须接入共享 .select-control 外观');
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
    await staleGuard('annotations', async () => {
      const reasonDisputed = find(root, node => node.tag === 'select'
        && node.name === 'annotation_reason');
      reasonDisputed.value = 'disputed';
      await reasonDisputed.handlers.change();
    }, 'listAnnotations', realListAnnotations, 'source_result_id');
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
      // 能力目录独立后与评估器列表均为单 API 视图（原组合请求已拆除）
      ['evaluators', 'listEvaluators', { items: [], next_cursor: null }, /暂无评估器/, '评估器列表加载失败'],
      ['capabilities', 'getCapabilities', { entries: [], judge_configured: false }, /暂无能力目录/, '能力目录加载失败'],
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

    // G5d) evaluators/capabilities 已在 G5b 单 API 视图表覆盖三态生命周期
    // （原 listEvaluators + getCapabilities 组合请求随能力目录拆视图移除）。

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
    // G8a) overview S5：修订配置挂运行配置 Sector 右上角（无"概览"外壳）；
    // 趋势/Producer titleGroup 与 numeric 列；无有效评分空态三要素
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
    // 概览页不套"概览"标题外壳（PRD 160 行精调）；修订配置挂运行配置 Sector 右上角
    assert.equal(find(root, node => hasClass(node, 'panel-title')
      && node.textContent === '概览'), undefined,
      '概览页不再渲染"概览"标题外壳面板');
    const configPanel = find(root, node => hasClass(node, 'status-panel')
      && find(node, n => hasClass(n, 'panel-title') && n.textContent === '运行配置'));
    assert.ok(configPanel, '运行配置面板存在');
    assert.ok(find(configPanel, node => hasClass(node, 'panel-actions')
      && node.children.some(c => c.textContent === '修订配置' && c.handlers.click)),
      '修订配置按钮位于运行配置 Sector 右上角');
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
    // 概览 Sector 顺序：两个列表 Sector 在前，备注 Sector 移至下方；
    // 趋势空列表与 Producer 单页均显第 1/1 页且按钮禁用占位
    const overviewTitles = findAll(root, node => node.tag === 'span'
      && hasClass(node, 'panel-title')).map(node => node.textContent);
    assert.deepEqual(overviewTitles,
      ['评分趋势', 'Producer 心跳', '运行配置', '容量与预算'],
      '无"概览"外壳标题，备注 Sector 位于列表 Sector 之后');
    const overviewPagerInfos = findAll(root, node => hasClass(node, 'evaluations-pager__info'));
    assert.equal(overviewPagerInfos.length, 2, '趋势与 Producer 各一个 pager');
    overviewPagerInfos.forEach(node => assert.equal(node.textContent, '第 1/1 页'));
    const disabledPagerButtons = findAll(root,
      node => node.tag === 'button' && (node.textContent === '上一页' || node.textContent === '下一页'));
    assert.equal(disabledPagerButtons.length, 4, '两个 pager 共四个始终占位按钮');
    assert.ok(disabledPagerButtons.every(node => node.disabled === true), '单页按钮全部禁用');

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
        'numeric-perfect': { results_total: 1, passed: 0, failed: 0, error_or_skipped: 0,
          numeric_count: 1, numeric_mean: 1, numeric_min: 1, numeric_max: 1 },
        'numeric-zero': { results_total: 1, passed: 0, failed: 0, error_or_skipped: 0,
          numeric_count: 1, numeric_mean: 0, numeric_min: 0, numeric_max: 0 },
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
    for (const [metric, score] of [['numeric-perfect', '1'], ['numeric-zero', '0']]) {
      const row = find(body, node => node.tag === 'tr'
        && node.children.some(child => child.textContent === metric));
      assert.deepEqual(row.children.map(child => child.textContent),
        [metric, '1', '-', '-', '1', score, score, score, '0'],
        '数值评分显示真实分数，布尔通过数为不适用');
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
      assert.doesNotMatch(humanRow.textContent, /人工评估器不支持自动使用启停/, 'human 行不展示操作备注');
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
    const assertAnnotationSelectSemantics = () => {
      // 人工标注两个筛选为同筛选行内的原生 select（共享 .select-control）
      const stateSelect = find(root, node => node.tag === 'select'
        && node.name === 'annotation_state');
      const reasonSelect = find(root, node => node.tag === 'select'
        && node.name === 'annotation_reason');
      assert.ok(stateSelect && reasonSelect, '状态/原因两个筛选下拉同时存在');
      [stateSelect, reasonSelect].forEach((select) => {
        assert.equal(select.className, 'select-control',
          '筛选下拉须接入共享 .select-control');
        const options = findAll(select, node => node.tag === 'option');
        assert.ok(options.length >= 2, '每个下拉至少两个选项');
        assert.ok(select.value, '下拉始终有选中值');
        assert.ok(options.some(option => option.value === select.value),
          '选中值必须是选项之一');
        assert.ok(select.name, 'select 须有 name 提交键');
      });
      const reasonValues = findAll(reasonSelect, node => node.tag === 'option')
        .map(option => option.value);
      assert.ok(!reasonValues.includes('all'), '原因下拉禁止造 all 项');
      // 下拉筛选不再渲染 role=group 控件组
      assert.equal(find(root, node => node.attributes && node.attributes.role === 'group'
        && (node.attributes['aria-label'] === '标注状态'
          || node.attributes['aria-label'] === '标注原因')), undefined,
        '人工标注不再使用 role=group 筛选组');
    };
    const assertPressedGroupSemantics = (groups) => {
      assert.ok(groups.length >= 1, 'review 至少保留一个按钮选择组');
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
    assertAnnotationSelectSemantics();
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
    assertAnnotationSelectSemantics();
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

    // review 视图保留 focusableChoiceGroup：role=group + aria-pressed 按钮组
    await moduleApi.refresh(state('review'));
    const reviewGroups = collectIdSnapshot(root).groups;
    assertPressedGroupSemantics(reviewGroups);
    assert.ok(find(root, node => node.tag === 'button'
      && node.attributes['aria-pressed'] !== undefined), 'review 保留 aria-pressed 选择按钮');
  }

  // G9) 焦点行为：annotations 筛选改为原生 select 后不做焦点恢复，用户
  // change 触发重渲染不抢焦，程序 refresh/初次加载也不自动聚焦，两个下拉
  // 互不串焦。review 仍为 focusableChoiceGroup 按钮组，焦点合同保持不变。
  {
    const findGroup = label => find(root, node => node.attributes
      && node.attributes.role === 'group' && node.attributes['aria-label'] === label);
    const groupButton = (group, value) => group.children
      .find(node => node.tag === 'button' && node.getAttribute('data-value') === value);
    const annotationSelect = name => find(root, node => node.tag === 'select'
      && node.name === name);

    // 初次/程序加载不聚焦：新树内无聚焦节点，残留焦点只可能在已分离旧节点
    await moduleApi.refresh(state('annotations'));
    assert.ok(!find(root, node => node.focused === true), '程序 refresh 新视图无聚焦节点');
    if (getActiveElement()) {
      // 向下遍历判定真实挂载（harness 的向上 parent 链在 textContent 清空后仍保留）
      assert.ok(!find(root, node => node === getActiveElement()),
        '程序 refresh 不把焦点移入新树');
    }

    // 前置：更早的迟到响应用例可能把原因留在 disputed，先经 UI 复位到
    // low_score，保证后续 disputed 选择是真实切换
    const resetReason = annotationSelect('annotation_reason');
    if (resetReason.value !== 'low_score') {
      resetReason.value = 'low_score';
      await resetReason.handlers.change();
      await flush();
    }

    // 标注原因下拉切换：重渲染生成新 select 节点、保留用户选择，但不抢焦
    const oldReasonSelect = annotationSelect('annotation_reason');
    oldReasonSelect.value = 'disputed';
    await oldReasonSelect.handlers.change();
    await flush();
    const newReasonSelect = annotationSelect('annotation_reason');
    assert.ok(newReasonSelect !== oldReasonSelect, '重渲染生成新 select 节点');
    assert.equal(newReasonSelect.value, 'disputed', '重渲染保留原因选择');
    assert.ok(!newReasonSelect.focused, '原因下拉重渲染不抢焦点');
    const stateSelect = annotationSelect('annotation_state');
    assert.ok(!stateSelect.focused, '状态下拉不串焦');

    // 标注状态下拉独立切换，同样不抢焦且保留选择
    const oldStateSelect = annotationSelect('annotation_state');
    oldStateSelect.value = 'pending';
    await oldStateSelect.handlers.change();
    await flush();
    const newStateSelect = annotationSelect('annotation_state');
    assert.ok(newStateSelect !== oldStateSelect, '重渲染生成新状态 select 节点');
    assert.equal(newStateSelect.value, 'pending', '重渲染保留状态选择');
    assert.ok(!newStateSelect.focused, '状态下拉重渲染不抢焦点');

    // review 原因组保持按钮焦点恢复；程序 refresh 后新树无聚焦
    await moduleApi.refresh(state('review'));
    assert.ok(!find(root, node => node.focused === true), '程序 refresh 不保留焦点');
    const reviewOld = groupButton(findGroup('待复核原因'), 'anomalous');
    await reviewOld.handlers.click();
    await flush();
    const reviewNew = groupButton(findGroup('待复核原因'), 'anomalous');
    assert.equal(reviewNew.focused, true, 'review 同组同值新按钮获得焦点');
    assert.equal(getActiveElement(), reviewNew);
  }

  // -------------------------------------------------------------------------
  // Part H：统一分页合同（plan-261115 T1 S5/S9）
  // H1/H2 cursor 视图（evaluators/annotations）：栈式前进回退、limit=20、
  // 筛选/重入重置、cursor_invalid 重新查询、非游标 422 不误清栈；
  // H3 翻页/筛选重叠的迟到响应；H4-H7 slice 视图矩阵/聚合/窗口/夹紧；
  // H8 实验视图内执行控制器（evaluation-executions.js）接入。
  // -------------------------------------------------------------------------
  {
    const pagerOf = scope => find(scope || root, node => node.className === 'evaluations-pager');
    const pagerBtn = (label, scope) =>
      find(pagerOf(scope), node => node.tag === 'button' && node.textContent === label);
    // 人工标注筛选为原生 select 下拉：经 name 定位后设值并触发 change
    const annFilter = (name, value) => {
      const select = find(root, node => node.tag === 'select' && node.name === name);
      select.value = value;
      return select.handlers.change();
    };
    const countCalls = name => calls.filter(call => call[0] === name).length;
    const dataBodyOf = className => find(root, node => node.className === className);
    const viewScope = {
      datasets: 'evaluations-data-body',
      experiments: 'evaluations-data-body',
      capabilities: 'evaluations-data-body',
      review: 'evaluations-review__list',
    };
    const rowsIn = scope => {
      const tableNode = find(scope, node => node.tag === 'table');
      return tableNode
        ? findAll(tableNode, node => node.tag === 'tr' && node.parentElement.tag === 'tbody')
        : [];
    };
    const realApis = {
      listEvaluators: api.listEvaluators,
      listAnnotations: api.listAnnotations,
      listDatasets: api.listDatasets,
      listExperiments: api.listExperiments,
      getCapabilities: api.getCapabilities,
      listReviewQueue: api.listReviewQueue,
    };
    const evalItem = id => ({ definition_id: id, version: 'v1', metric_name: id,
      target_kind: 'agent_run', method: 'deterministic',
      usage: { online_enabled: false, revision: 1 } });
    const annItem = id => Object.assign({}, baseQueueItem, { source_result_id: id });
    try {
      // H1) evaluators cursor 栈：首页 limit=20 无游标，前进带游标，回退清游标
      // 注意：每次重渲染都重建筛选节点，控件必须在点击时重新查找，不能缓存
      const evalMethodFilter = () =>
        find(root, node => node.name === 'evaluator_method_filter' && node.handlers.change);
      const evalKindFilter = () =>
        find(root, node => node.name === 'evaluator_target_kind_filter' && node.handlers.change);
      await moduleApi.refresh(state('evaluators'));
      evalMethodFilter().value = '';
      await evalMethodFilter().handlers.change();
      evalKindFilter().value = '';
      await evalKindFilter().handlers.change();
      listParamsHistory.length = 0;
      listResult = { items: [evalItem('eval-page1')], next_cursor: 'eval-c2' };
      await moduleApi.refresh(state('evaluators'));
      assert.deepEqual(JSON.parse(JSON.stringify(listParamsHistory.at(-1))), { limit: 20 },
        'evaluators 首页只发 limit=20');
      assert.match(pagerOf().textContent, /第 1\/2 页/);
      assert.match(pagerOf().textContent, /\//, 'cursor 模式统一带已知页数分母');
      assert.equal(pagerBtn('上一页').disabled, true);
      assert.equal(pagerBtn('下一页').disabled, false);
      api.listEvaluators = async params => {
        listParamsHistory.push(params);
        if (params.cursor === 'eval-c2') {
          return { items: [evalItem('eval-page2')], next_cursor: null };
        }
        return listResult;
      };
      await pagerBtn('下一页').handlers.click();
      await flush();
      assert.deepEqual(JSON.parse(JSON.stringify(listParamsHistory.at(-1))),
        { limit: 20, cursor: 'eval-c2' }, '下一页携带栈顶游标');
      assert.match(root.textContent, /eval-page2/);
      assert.match(pagerOf().textContent, /第 2\/2 页/);
      assert.equal(pagerBtn('上一页').disabled, false);
      assert.equal(pagerBtn('下一页').disabled, true, '无 next_cursor 末页禁用占位');
      listResult = { items: [evalItem('eval-page1')], next_cursor: 'eval-c2' };
      await pagerBtn('上一页').handlers.click();
      await flush();
      assert.deepEqual(JSON.parse(JSON.stringify(listParamsHistory.at(-1))), { limit: 20 },
        '上一页从游标栈回退到无游标首页');
      // 筛选变更重置回第 1 页
      await pagerBtn('下一页').handlers.click();
      await flush();
      assert.equal(listParamsHistory.at(-1).cursor, 'eval-c2');
      evalMethodFilter().value = 'deterministic';
      await evalMethodFilter().handlers.change();
      await flush();
      assert.deepEqual(JSON.parse(JSON.stringify(listParamsHistory.at(-1))),
        { limit: 20, method: 'deterministic' }, '筛选变更清空游标栈');
      assert.match(pagerOf().textContent, /第 1\/2 页/);
      // 视图重入重置
      await pagerBtn('下一页').handlers.click();
      await flush();
      await moduleApi.refresh(state('evaluators'));
      assert.equal(listParamsHistory.at(-1).cursor, undefined, '重入视图回第 1 页');
      // cursor_invalid：携带游标失败出现「重新查询」，点击清栈重发首页
      listResult = { items: [evalItem('eval-page1')], next_cursor: 'eval-bad' };
      evalMethodFilter().value = '';
      await evalMethodFilter().handlers.change();
      await flush();
      api.listEvaluators = async params => {
        listParamsHistory.push(params);
        if (params.cursor) throw new Error('cursor_invalid');
        return listResult;
      };
      await pagerBtn('下一页').handlers.click();
      await flush();
      assert.match(root.textContent, /游标已失效/);
      assert.ok(findButton('重新查询', root), '游标失效提供显式重新查询入口');
      await findButton('重新查询', root).handlers.click();
      await flush();
      assert.deepEqual(JSON.parse(JSON.stringify(listParamsHistory.at(-1))), { limit: 20 },
        '重新查询清空游标栈回首页');
      // 非游标 422（protocol_invalid 也用于参数校验）：不得走重新查询清栈分支
      api.listEvaluators = async () => { throw new Error('protocol_invalid'); };
      await moduleApi.refresh(state('evaluators'));
      assert.equal(findButton('重新查询', root), undefined, '非游标错误不渲染重新查询');
      assert.ok(findButton('重试', root), '普通错误走同页重试');
      assert.match(root.textContent, /评估器列表加载失败/);
      api.listEvaluators = realApis.listEvaluators;

      // H2) annotations cursor 栈：同构合同，参数经 annParams() 断言
      await moduleApi.refresh(state('annotations'));
      await annFilter('annotation_state', 'all');
      await flush();
      await annFilter('annotation_reason', 'low_score');
      await flush();
      annParamsHistory.length = 0;
      annQueue = { items: [annItem('ann-page1')], next_cursor: 'ann-c2' };
      await moduleApi.refresh(state('annotations'));
      assert.deepEqual(annParams(), { state: 'all', reason: 'low_score', threshold: '0.5', limit: 20 });
      assert.equal(pagerBtn('上一页').disabled, true);
      api.listAnnotations = async params => {
        annParamsHistory.push(params);
        if (params.cursor === 'ann-c2') {
          return { items: [annItem('ann-page2')], next_cursor: null };
        }
        return annQueue;
      };
      await pagerBtn('下一页').handlers.click();
      await flush();
      assert.equal(annParams().cursor, 'ann-c2');
      assert.equal(annParams().limit, 20);
      assert.match(root.textContent, /ann-page2/);
      assert.equal(pagerBtn('下一页').disabled, true);
      annQueue = { items: [annItem('ann-page1')], next_cursor: 'ann-c2' };
      await pagerBtn('上一页').handlers.click();
      await flush();
      assert.equal(annParams().cursor, undefined, '回退清游标');
      // 原因筛选变更重置
      await pagerBtn('下一页').handlers.click();
      await flush();
      await annFilter('annotation_reason', 'disputed');
      await flush();
      assert.equal(annParams().cursor, undefined, '原因变更重置游标栈');
      assert.equal(annParams().reason, 'disputed');
      assert.match(pagerOf().textContent, /第 1\/2 页/);
      // 状态筛选变更同样重置游标栈；低分阈值固定 0.5，页面无阈值控件
      await annFilter('annotation_reason', 'low_score');
      await flush();
      await annFilter('annotation_state', 'pending');
      await flush();
      assert.equal(annParams().cursor, undefined, '状态变更重置游标栈');
      assert.equal(annParams().state, 'pending');
      assert.equal(annParams().threshold, '0.5', '阈值固定 0.5 随查询参数发送');
      await annFilter('annotation_state', 'all');
      await flush();
      // cursor_invalid 重新查询
      annQueue = { items: [annItem('ann-page1')], next_cursor: 'ann-bad' };
      api.listAnnotations = async params => {
        annParamsHistory.push(params);
        if (params.cursor) throw new Error('cursor_invalid');
        return annQueue;
      };
      await pagerBtn('下一页').handlers.click();
      await flush();
      assert.ok(findButton('重新查询', root));
      await findButton('重新查询', root).handlers.click();
      await flush();
      assert.equal(annParams().cursor, undefined, '重新查询回无游标首页');
      // 非游标参数错误不走清栈分支
      api.listAnnotations = async () => { throw new Error('protocol_invalid'); };
      await moduleApi.refresh(state('annotations'));
      assert.equal(findButton('重新查询', root), undefined);
      assert.ok(findButton('重试', root));
      assert.match(root.textContent, /标注队列加载失败/);
      api.listAnnotations = realApis.listAnnotations;
      annQueue = { items: [], next_cursor: null };

      // H3) 翻页/筛选重叠：加载期无陈旧 pager；旧响应晚结算不得覆盖新页
      const pagingRace = async ({ view, slot, trigger, idField }) => {
        const resolvers = [];
        api[slot] = () => new Promise((resolve, reject) => resolvers.push({ resolve, reject }));
        const first = moduleApi.refresh(state(view));
        await flush();
        assert.ok(find(root, node => typeof node.className === 'string'
          && node.className.includes('loading-state')), view + ' 加载态展示 loading-state');
        assert.equal(pagerOf(), undefined, view + ' 加载态不渲染陈旧 pager，无按钮可误点');
        await trigger();
        await flush();
        assert.equal(resolvers.length, 2, view + ' 筛选触发第二次取数');
        resolvers[1].resolve({ items: [{ [idField]: 'race-fresh-' + view }], next_cursor: null });
        await flush();
        resolvers[0].resolve({ items: [{ [idField]: 'race-stale-' + view }], next_cursor: 'late-cursor' });
        await first;
        await flush();
        assert.match(root.textContent, new RegExp('race-fresh-' + view));
        assert.doesNotMatch(root.textContent, new RegExp('race-stale-' + view),
          view + ' 迟到成功响应不得覆盖新页 DOM');
        assert.match(pagerOf().textContent, /第 1\/1 页/, view + ' 迟到响应不得改变 pager 状态');
        api[slot] = realApis[slot];
      };
      await pagingRace({
        view: 'evaluators', slot: 'listEvaluators', idField: 'definition_id',
        trigger: async () => {
          const select = evalMethodFilter();
          select.value = 'llm_judge';
          await select.handlers.change();
        },
      });
      await pagingRace({
        view: 'annotations', slot: 'listAnnotations', idField: 'source_result_id',
        trigger: async () => annFilter('annotation_reason', 'disputed'),
      });

      // H4) slice 四视图行为矩阵：0/1/20/21/40/41 → 总页数 1/1/1/2/2/3
      const matrix = [
        { view: 'datasets', slot: 'listDatasets',
          make: n => Array.from({ length: n }, (_, i) => ({
            dataset_id: 'ds-m' + i, dataset_version: 'v1', target_kind: 'agent_run',
            case_count: 1, status: 'valid',
          })),
          wrap: items => ({ items }) },
        { view: 'experiments', slot: 'listExperiments',
          make: n => Array.from({ length: n }, (_, i) => ({
            experiment_id: 'exp-m' + i, dataset_id: 'd', dataset_version: 'v1',
            mode: 'replay', status: 'running', created_at: '',
          })),
          wrap: items => ({ items, next_cursor: null }) },
        { view: 'capabilities', slot: 'getCapabilities',
          make: n => Array.from({ length: n }, (_, i) => ({
            metric_name: 'metric-m' + i, method: 'deterministic', target_kind: 'agent_run',
            output_type: 'number', executable: true, judge_required: false, online_forbidden: false,
          })),
          wrap: entries => ({ judge_configured: true, entries }) },
        { view: 'review', slot: 'listReviewQueue',
          make: n => Array.from({ length: n }, (_, i) => ({
            result_id: 'rv-m' + i, metric_name: 'm', provenance: 'automatic',
            query: 'q', typed_value: 1, created_at: '',
          })),
          wrap: items => ({ items, next_cursor: null }) },
      ];
      const sizes = [[0, 1], [1, 1], [20, 1], [21, 2], [40, 2], [41, 3]];
      for (const spec of matrix) {
        const scope = () => dataBodyOf(viewScope[spec.view]);
        for (const [size, pages] of sizes) {
          api[spec.slot] = async () => spec.wrap(spec.make(size));
          await moduleApi.refresh(state(spec.view));
          await flush();
          const pager = pagerOf(scope());
          assert.ok(pager, spec.view + ' 空/非空均常驻 pager（size=' + size + '）');
          assert.match(pager.textContent, new RegExp('第 1/' + pages + ' 页'),
            spec.view + ' size=' + size + ' 总页数 ' + pages);
          assert.equal(pagerBtn('上一页', scope()).disabled, true);
          assert.equal(pagerBtn('下一页', scope()).disabled, pages === 1);
          assert.equal(rowsIn(scope()).length, Math.min(size, 20),
            spec.view + ' 首屏切片行数 size=' + size);
          if (size === 0) {
            assert.ok(find(scope(), node => typeof node.className === 'string'
              && node.className.includes('empty-state')), spec.view + ' 空态与 pager 共存');
          }
          // 翻页只做内存切片：全程不新增 API 调用
          const callsAfterRender = countCalls(spec.slot);
          for (let page = 2; page <= pages; page += 1) {
            await pagerBtn('下一页', scope()).handlers.click();
            assert.match(pagerOf(scope()).textContent, new RegExp('第 ' + page + '/' + pages + ' 页'));
            assert.equal(rowsIn(scope()).length, (page === pages) ? (size - 20 * (pages - 1)) : 20);
          }
          assert.equal(countCalls(spec.slot), callsAfterRender,
            spec.view + ' 翻页不重新取数（size=' + size + '）');
          if (pages > 1) {
            assert.equal(pagerBtn('下一页', scope()).disabled, true, '末页下一页禁用');
            for (let page = pages - 1; page >= 1; page -= 1) {
              await pagerBtn('上一页', scope()).handlers.click();
              assert.match(pagerOf(scope()).textContent,
                new RegExp('第 ' + page + '/' + pages + ' 页'));
            }
            assert.equal(pagerBtn('上一页', scope()).disabled, true, '回到首页后上一页禁用');
          }
        }
      }
      // 还原矩阵视图 mock
      api.listDatasets = realApis.listDatasets;
      api.listExperiments = realApis.listExperiments;
      api.getCapabilities = realApis.getCapabilities;
      api.listReviewQueue = realApis.listReviewQueue;

      // H5) datasets 聚合后分页：21 个不同 dataset_id、各两版本且顺序交错，
      //     42 原始行聚合为 21 行 → 20+1，同一数据集不跨页，翻页零新请求
      const aggRows = [];
      const aggId = i => 'agg-' + String(i).padStart(2, '0');
      for (let i = 0; i < 21; i += 1) {
        aggRows.push({ dataset_id: aggId(i), dataset_version: 'v1', target_kind: 'agent_run',
          case_count: 1, status: 'valid' });
      }
      for (let i = 20; i >= 0; i -= 1) {
        aggRows.push({ dataset_id: aggId(i), dataset_version: 'v2', target_kind: 'agent_run',
          case_count: 2, status: 'valid' });
      }
      api.listDatasets = async () => ({ items: aggRows });
      await moduleApi.refresh(state('datasets'));
      await flush();
      const dsScope = () => dataBodyOf('evaluations-data-body');
      const rowIds = scopeNode => rowsIn(scopeNode).map(row => findAll(row, node => node.tag === 'td')[0].textContent);
      let pageIds = rowIds(dsScope());
      assert.equal(pageIds.length, 20, '聚合后首屏 20 个数据集行');
      assert.equal(new Set(pageIds).size, 20, '首屏无重复 dataset_id');
      assert.match(pagerOf(dsScope()).textContent, /第 1\/2 页/);
      const dsCallsAfterRender = countCalls('listDatasets');
      await pagerBtn('下一页', dsScope()).handlers.click();
      const secondIds = rowIds(dsScope());
      assert.equal(secondIds.length, 1, '第二页仅 1 个聚合行');
      assert.equal(new Set(secondIds).size, 1);
      assert.ok(!pageIds.includes(secondIds[0]), '同一 dataset_id 不跨页重复');
      assert.equal(countCalls('listDatasets'), dsCallsAfterRender, '聚合视图翻页不重新取数');
      api.listDatasets = realApis.listDatasets;

      // H6) review 窗口合同：请求 limit=50（REVIEW_WINDOW_LIMIT），窗口说明
      //     文案常驻，50 条窗口内切 20/20/10 三页，翻页不增请求
      reviewQueuePage = {
        items: Array.from({ length: 50 }, (_, i) => ({
          result_id: 'win-' + i, metric_name: 'm', provenance: 'automatic',
          query: 'q', typed_value: 1, created_at: '',
        })),
        next_cursor: null,
      };
      reviewParamsHistory.length = 0;
      await moduleApi.refresh(state('review'));
      await flush();
      assert.equal(reviewParamsHistory.at(-1).limit, 50, '待复核窗口请求保持 50');
      assert.match(root.textContent, /仅对本次返回窗口分页，最多 50 条/);
      const reviewScope = () => dataBodyOf('evaluations-review__list');
      assert.match(pagerOf(reviewScope()).textContent, /第 1\/3 页/);
      const reviewCallsAfterRender = countCalls('listReviewQueue');
      await pagerBtn('下一页', reviewScope()).handlers.click();
      assert.equal(rowsIn(reviewScope()).length, 20);
      await pagerBtn('下一页', reviewScope()).handlers.click();
      assert.equal(rowsIn(reviewScope()).length, 10, '窗口末页 10 条');
      assert.equal(countCalls('listReviewQueue'), reviewCallsAfterRender, '窗口内翻页不增请求');
      reviewQueuePage = { items: [], next_cursor: null };

      // H7) 数据缩减与生命周期：停在第 2 页时刷新返回更少 items，重入夹紧
      //     回第 1 页，不越界、不报错（四视图刷新均重置 pageIndex=0）
      api.listDatasets = async () => ({
        items: Array.from({ length: 21 }, (_, i) => ({
          dataset_id: 'shrink-' + i, dataset_version: 'v1', target_kind: 'agent_run',
          case_count: 1, status: 'valid',
        })),
      });
      await moduleApi.refresh(state('datasets'));
      await flush();
      await pagerBtn('下一页', dsScope()).handlers.click();
      assert.match(pagerOf(dsScope()).textContent, /第 2\/2 页/);
      api.listDatasets = async () => ({
        items: Array.from({ length: 5 }, (_, i) => ({
          dataset_id: 'shrink-small-' + i, dataset_version: 'v1', target_kind: 'agent_run',
          case_count: 1, status: 'valid',
        })),
      });
      await moduleApi.refresh(state('datasets'));
      await flush();
      assert.match(pagerOf(dsScope()).textContent, /第 1\/1 页/, '刷新后页码夹紧回第 1 页');
      assert.equal(rowsIn(dsScope()).length, 5);
      assert.equal(pagerBtn('下一页', dsScope()).disabled, true);
      assert.equal(find(root, node => typeof node.className === 'string'
        && node.className.includes('error-state')), undefined, '缩减刷新不产生错误态');
      api.listDatasets = realApis.listDatasets;

      // H8) 实验子域拆分为双顶导入口（plan-261115 T2 S5）：
      //     executions 视图承载执行任务（发起实验 + 执行表 cursor pager），
      //     experiments 视图只保留实验结果表，不再内嵌执行区块与发起入口。
      execParamsHistory.length = 0;
      calls.length = 0;
      execListPage = {
        items: [{ execution_id: 'exec-wb-1', name: 'wb exec', dataset_id: 'd1',
          dataset_version: 'v1', phase: 'running', created_at: '' }],
        next_cursor: 'exec-c2',
      };
      await moduleApi.refresh(state('executions'));
      await flush();
      assert.deepEqual(JSON.parse(JSON.stringify(execParamsHistory.at(-1))), { limit: 20 },
        '执行任务视图首请 limit=20 无游标');
      assert.ok(calls.some(c => c[0] === 'listExecutions'), '执行任务视图调用 listExecutions');
      assert.ok(!calls.some(c => c[0] === 'listExperiments'),
        '执行任务视图不得请求实验结果列表');
      const headerActions = find(root, node => node.className === 'panel-actions');
      assert.ok(headerActions, '面板提供 Sector Header 操作区');
      const createBtn = find(headerActions, node => node.tag === 'button' && node.textContent === '发起实验');
      assert.ok(createBtn, '发起实验入口位于 Sector Header 右上角');
      const execBlock = find(root, node => node.className === 'evaluations-block-title'
        && node.textContent === '实验执行');
      assert.equal(execBlock, undefined, '执行任务视图不再渲染实验执行区块标题行');
      const execPager = () => find(root, node => node.className === 'evaluations-pager');
      assert.ok(execPager(), '执行区块使用共享 evaluations-pager');
      assert.match(execPager().textContent, /第 1\/2 页/);
      assert.ok(!find(root, node => node.tag === 'th' && node.textContent === '实验ID'),
        '执行任务视图不渲染实验结果表列头');
      const execPagers = findAll(root, node => node.className === 'evaluations-pager');
      assert.equal(execPagers.length, 1, '执行任务视图只有执行 cursor pager');
      execListPage = {
        items: [{ execution_id: 'exec-wb-2', name: 'wb exec 2', dataset_id: 'd1',
          dataset_version: 'v1', phase: 'completed', created_at: '' }],
        next_cursor: null,
      };
      await find(execPager(), node => node.tag === 'button' && node.textContent === '下一页').handlers.click();
      await flush();
      assert.equal(execParamsHistory.at(-1).cursor, 'exec-c2', '执行区下一页携带游标');
      assert.match(root.textContent, /wb exec 2/, '执行区渲染第二页执行名称');
      // experiments 视图瘦身：只有实验结果 slice 表，无发起入口与执行区块
      calls.length = 0;
      execParamsHistory.length = 0;
      await moduleApi.refresh(state('experiments'));
      await flush();
      assert.ok(calls.some(c => c[0] === 'listExperiments'), '实验结果视图调用 listExperiments');
      assert.ok(!calls.some(c => c[0] === 'listExecutions'),
        '实验结果视图不得再内嵌执行列表请求');
      assert.equal(execParamsHistory.length, 0, '实验结果视图不触发 listExecutions');
      assert.ok(!find(root, node => node.tag === 'button' && node.textContent === '发起实验'),
        '实验结果视图不提供发起实验按钮');
      assert.ok(!find(root, node => node.className === 'evaluations-block'),
        '实验结果视图移除执行区块');
      assert.ok(find(root, node => node.tag === 'th' && node.textContent === '实验ID'),
        '实验结果视图保留实验结果表');
      const expPagers = findAll(root, node => node.className === 'evaluations-pager');
      assert.equal(expPagers.length, 1, '实验结果视图只剩实验 slice pager');
      execListPage = { items: [], next_cursor: null };
    } finally {
      api.listEvaluators = realApis.listEvaluators;
      api.listAnnotations = realApis.listAnnotations;
      api.listDatasets = realApis.listDatasets;
      api.listExperiments = realApis.listExperiments;
      api.getCapabilities = realApis.getCapabilities;
      api.listReviewQueue = realApis.listReviewQueue;
      listResult = { items: [], next_cursor: null };
      annQueue = { items: [], next_cursor: null, snapshot_at: '' };
      reviewQueuePage = { items: [], next_cursor: null };
      execListPage = { items: [], next_cursor: null };
    }
  }

  // -------------------------------------------------------------------------
  // Part I：模型页 Provider 列表形态（plan-261116 T6 + PRD 159 行精调）。
  // I1 总闸关闭不调管理 API；I2 Provider 列表 Sector + 供给精简 Sector
  //    （表格列含"默认"是/否/无"模型列表"区/供给字段/is_active 禁删）；
  // I3 编辑 modal 勾选设默认 CAS 载荷 + 成功重载列表与投影；I3b 下一次
  //    expected_version 取服务端返回版本；I3c 取消默认空串 + 回落提示；
  // I4 pending 防重复提交；I5 默认 CAS 409 不自动重放（Provider 已保存，
  //    modal 关闭）；I6 listProviders/getJudgeModel 失败 skeleton + 重试恢复；
  // I7 modal CRUD（共用提示/独立 DOM id/名称置于控件上方/默认勾选
  //    anthropic 禁用/API Key 三态/失败保留草稿/确认删除/is_active 禁删/
  //    默认非 active 可删/关闭清空密钥）；I8 datalist 候选与预填
  //    （effective_model 优先/来源冻结值优先/触碰不回填/方法切换保留草稿/
  //    无供给留空且提交省略）；I9 显式输入校验不变；I10 切页/关 modal 迟到
  //    响应隔离；I12 Provider 列表共享翻页（slice 模式/翻页/刷新回第 1 页）；
  //    I11 全程 activateProvider 零调用。
  // -------------------------------------------------------------------------
  {
    const providerCallsOf = kind => providerCalls.filter(c => c[0] === kind).length;
    const saveCallCount = () => calls.filter(c => c[0] === 'patchConfig').length;
    const jmCallCount = () => calls.filter(c => c[0] === 'getJudgeModel').length;
    const retryBtn = () => find(root, node => node.tag === 'button'
      && node.textContent === '重试' && node.handlers.click);
    const judgeModelInputOf = form => find(form, node => node.name === 'judge_model');
    const datalistOf = form => find(form, node => node.tag === 'datalist');

    // I1) 总闸关闭（子域未启用）：走既有 status 探测展示未启用，不调管理
    //     API，不出现 Provider 列表
    api.status = async () => { calls.push(['status']); return { enabled: false }; };
    calls.length = 0;
    await moduleApi.refresh(state('judge-model'));
    assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['status']],
      '子域未启用不得调用管理 API');
    assert.match(root.textContent, /评估功能未启用/);
    assert.doesNotMatch(root.textContent, /Provider 列表/, '未启用不得出现 Provider 列表');
    api.status = async () => { calls.push(['status']); return { enabled: true }; };

    // I2) Provider 列表 Sector（表格列/默认列/操作列）+ 供给精简 Sector
    calls.length = 0;
    providerCalls.length = 0;
    await moduleApi.refresh(state('judge-model'));
    assert.ok(calls.some(c => c[0] === 'getJudgeModel'), '模型页加载 getJudgeModel');
    assert.ok(providerCalls.some(c => c[0] === 'listProviders'), '模型页加载 listProviders');
    assert.match(root.textContent, /Provider 列表/, 'Sector 1 为 Provider 列表');
    for (const label of ['名称', '类型', 'Base URL', 'Model', 'Key', 'Vision', '默认', '操作']) {
      assert.ok(root.textContent.includes(label), 'Provider 表格缺少列: ' + label);
    }
    // "默认"列取值 是/否，仅当前默认行（prov-1）为"是"；不用 badge 重复展示
    const jmProviderTable = find(root, node => node.tag === 'table'
      && node.children[0] && node.children[0].children[0]
      && node.children[0].children[0].children.some(
        th => th.textContent === '默认'));
    assert.ok(jmProviderTable, '存在含"默认"列的 Provider 表格');
    const defaultColumnOf = tableNode => tableNode.children[1].children
      .map(tr => tr.children[6] && tr.children[6].textContent);
    const defaultColumn = defaultColumnOf(jmProviderTable);
    assert.ok(defaultColumn.includes('是') && defaultColumn.includes('否'),
      '默认列取值为 是/否');
    assert.equal(defaultColumn.filter(v => v === '是').length, 1, '仅默认行取值是');
    assert.equal(findAll(root, node => node.className === 'badge badge--success'
      && node.textContent === '评估默认').length, 0, '默认行不再用 badge 展示');
    assert.match(root.textContent, /api\.example\.com/, 'Base URL 展示');
    assert.doesNotMatch(root.textContent, /模型列表（当前Provider）/, '不渲染模型列表区');
    // 供给精简区：judge_ready/supply_source/effective_model/env 基线/host
    assert.match(root.textContent, /已就绪/, 'judge_ready 状态文案');
    assert.match(root.textContent, /gpt-provider-model/, 'effective_model 展示');
    assert.match(root.textContent, /env-model-x/, 'env_baseline_model 展示');
    assert.match(root.textContent, /B 类 env-only，Dashboard 不可改/);
    assert.doesNotMatch(root.textContent, /未设置默认 Provider，回落 env 基线/,
      '已设默认时不显示回落提示');
    // 操作列只保留编辑/删除；"设为评估默认"入口移入编辑 modal
    assert.equal(findButton('设为评估默认'), undefined,
      '行内不提供设为评估默认按钮');
    assert.equal(findButton('取消默认'), undefined, '行内不提供取消默认按钮');
    assert.equal(findAll(root, node => node.className === 'muted'
      && node.textContent === 'Agent 启用中').length, 0,
      'is_active 行不展示冗余启用提示');
    assert.equal(findAll(root, node => node.tag === 'button'
      && node.textContent === '启用').length, 0, '不提供 Agent 启用操作');

    // 编辑 modal 打开辅助：行序同 providers 数组（0=prov-1, 1=anthropic, 2=prov-3）
    const defaultCheckboxOf = form => find(form,
      node => node.id === 'eval-provider-default');
    const openEditModal = async rowIndex => {
      const editBtns = findAll(root, node => node.tag === 'button'
        && node.textContent === '编辑' && node.handlers.click);
      await editBtns[rowIndex].handlers.click();
      return find(body, node => node.tag === 'form');
    };

    // I3) 编辑 modal 勾选"设为评估默认"：Provider 保存成功后按需 PATCH
    //     config CAS；成功后重拉列表与供给投影，默认列跟随新默认行
    calls.length = 0;
    providerCalls.length = 0;
    let provForm3 = await openEditModal(2); // prov-3（非默认 openai 行）
    assert.ok(defaultCheckboxOf(provForm3), '编辑 modal 提供设为评估默认勾选项');
    assert.equal(defaultCheckboxOf(provForm3).checked, false, '非默认行勾选态为否');
    assert.equal(defaultCheckboxOf(provForm3).disabled, false, 'openai 行勾选可用');
    defaultCheckboxOf(provForm3).checked = true;
    const alertsBeforeSet = alerts.length;
    await provForm3.handlers.submit({ preventDefault() {} });
    assert.equal(saveCallCount(), 1);
    assert.deepEqual(JSON.parse(JSON.stringify(jmPatchCalls.at(-1))),
      { expected_version: 3, patch: { evaluation_judge_provider_id: 'prov-3' } },
      'PATCH 载荷必须为 {expected_version, patch:{evaluation_judge_provider_id}}');
    assert.equal(jmCallCount(), 1, '成功后重载供给投影（版本未变也刷新）');
    assert.equal(providerCallsOf('listProviders'), 1, '成功后重拉列表');
    assert.equal(alerts.length, alertsBeforeSet,
      '保存成功不弹提示 modal（modal 关闭 + 默认列刷新即是反馈）');
    assert.equal(body.children.length, 0, '保存成功后 modal 关闭');
    const jmTableAfterSet = find(root, node => node.tag === 'table'
      && node.children[0] && node.children[0].children[0]
      && node.children[0].children[0].children.some(
        th => th.textContent === '默认'));
    assert.ok(jmTableAfterSet, '重拉后 Provider 表格存在');
    const defaultColAfterSet = defaultColumnOf(jmTableAfterSet);
    assert.equal(defaultColAfterSet[2], '是', '新默认行（prov-3）默认列取值是');
    assert.equal(defaultColAfterSet[0], '否', '原默认行（prov-1）默认列取值否');

    // I3b) 下一次设默认 expected_version 取服务端返回版本（prov-1）
    calls.length = 0;
    let provForm1 = await openEditModal(0);
    assert.equal(defaultCheckboxOf(provForm1).checked, false,
      '原默认行在新默认生效后勾选态为否');
    defaultCheckboxOf(provForm1).checked = true;
    await provForm1.handlers.submit({ preventDefault() {} });
    assert.deepEqual(JSON.parse(JSON.stringify(jmPatchCalls.at(-1))),
      { expected_version: 4, patch: { evaluation_judge_provider_id: 'prov-1' } },
      '下次 CAS expected_version 必须取服务端返回版本');

    // I3c) 取消默认：modal 取消勾选，patch 传空串；成功后显示回落 env 提示
    calls.length = 0;
    provForm1 = await openEditModal(0);
    assert.equal(defaultCheckboxOf(provForm1).checked, true, '默认行勾选态跟随投影');
    defaultCheckboxOf(provForm1).checked = false;
    await provForm1.handlers.submit({ preventDefault() {} });
    assert.deepEqual(JSON.parse(JSON.stringify(jmPatchCalls.at(-1))),
      { expected_version: 4, patch: { evaluation_judge_provider_id: '' } },
      '取消默认必须传空串清除引用');
    assert.equal(alerts.length, alertsBeforeSet,
      '取消默认成功同样不弹提示 modal');
    assert.match(root.textContent, /未设置默认 Provider，回落 env 基线/);

    // I4) pending 期间禁用重复提交（modal 保存按钮 disabled 守卫）
    const realPatchConfig = api.patchConfig;
    let releaseDefault;
    api.patchConfig = payload => {
      calls.push(['patchConfig']);
      jmPatchCalls.push(payload);
      return new Promise(resolve => { releaseDefault = resolve; });
    };
    calls.length = 0;
    const pendingForm = await openEditModal(2); // prov-3 非默认
    defaultCheckboxOf(pendingForm).checked = true;
    const pendingSubmit = pendingForm.handlers.submit({ preventDefault() {} });
    await flush();
    assert.equal(saveCallCount(), 1, 'pending 中仅一次请求');
    await pendingForm.handlers.submit({ preventDefault() {} });
    assert.equal(saveCallCount(), 1, '操作期间禁用重复提交');
    api.patchConfig = realPatchConfig;
    releaseDefault(jmPatchResult);
    await pendingSubmit;
    await flush();

    // I5) 默认 CAS 409：Provider 已保存、modal 关闭，提示显式刷新；
    //     禁止自动重放（patchConfig 不再调用），视图仍重拉一次
    jmPatchBehavior = 'conflict';
    calls.length = 0;
    alerts.length = 0;
    const conflictForm = await openEditModal(0); // prov-1（当前非默认）
    defaultCheckboxOf(conflictForm).checked = true;
    await conflictForm.handlers.submit({ preventDefault() {} });
    jmPatchBehavior = 'ok';
    assert.equal(saveCallCount(), 1, '409 不得自动重放');
    assert.equal(jmCallCount(), 1, 'Provider 已保存后仍重拉视图');
    assert.equal(body.children.length, 0, 'Provider 已保存，modal 关闭');
    assert.match(alerts.join('\n'), /409|冲突/);
    assert.match(alerts.join('\n'), /刷新/);

    // I6) listProviders / getJudgeModel 失败：保留 skeleton + 可点击重试；
    //     重试成功恢复默认操作
    providersFail = true;
    await moduleApi.refresh(state('judge-model'));
    assert.match(root.textContent, /加载失败/);
    assert.ok(retryBtn(), '加载失败提供可点击重试');
    providersFail = false;
    await retryBtn().handlers.click();
    assert.match(root.textContent, /Provider 列表/, '重试成功恢复 Provider 列表');
    assert.ok(findButton('新增'), '重试成功恢复默认操作');
    const realGetJudgeModel = api.getJudgeModel;
    api.getJudgeModel = async () => {
      calls.push(['getJudgeModel']);
      throw new Error('request_failed');
    };
    await moduleApi.refresh(state('judge-model'));
    assert.match(root.textContent, /加载失败/, '投影失败同样报错');
    assert.match(root.textContent, /评估供给加载中/, '供给区保留加载 skeleton');
    assert.ok(retryBtn());
    api.getJudgeModel = realGetJudgeModel;
    await retryBtn().handlers.click();
    assert.match(root.textContent, /已就绪|未就绪/, '重试成功恢复供给区');

    // I7a) 新增 modal：独立 DOM id + 共用提示 + 名称置于控件上方 + 新增
    //      API Key 必填（空白拒绝）；未勾选默认时不触发 config CAS
    calls.length = 0;
    providerCalls.length = 0;
    await findButton('新增').handlers.click();
    let provForm = find(body, node => node.tag === 'form');
    assert.ok(provForm, '新增 Provider modal 未打开');
    assert.match(body.textContent,
      /Provider 配置与 Agent 共用，修改会影响共用该 Provider 的调用/);
    for (const domId of ['eval-provider-name', 'eval-provider-type',
      'eval-provider-base-url', 'eval-provider-model',
      'eval-provider-api-key', 'eval-provider-vision',
      'eval-provider-default']) {
      assert.ok(find(provForm, node => node.id === domId), '缺少独立 DOM id: ' + domId);
    }
    // 布局对齐主模型页 providers-form：字段名称置于控件上方（label 包裹），
    // 不复用评估器表单的 policy-item 左侧标签
    assert.ok(find(provForm, node => node.tag === 'label'
      && node.children.some(c => c.id === 'eval-provider-name')),
      '名称控件由 label 包裹（名称在控件上方）');
    assert.equal(find(provForm, node => node.className === 'policy-k'), undefined,
      'Provider modal 不使用左侧标签布局');
    assert.equal(defaultCheckboxOf(provForm).checked, false, '新增默认不勾选');
    assert.equal(defaultCheckboxOf(provForm).disabled, false, '新增 openai 默认可勾选');
    find(provForm, node => node.id === 'eval-provider-name').value = 'Eval Prov';
    find(provForm, node => node.id === 'eval-provider-base-url').value = 'https://api.eval.com/v1';
    find(provForm, node => node.id === 'eval-provider-model').value = 'gpt-eval';
    await provForm.handlers.submit({ preventDefault() {} });
    assert.equal(providerCallsOf('createProvider'), 0, '新增空白 API Key 不得提交');
    assert.match(body.textContent, /API Key 必填/);
    find(provForm, node => node.id === 'eval-provider-api-key').value = 'sk-eval-1';
    await provForm.handlers.submit({ preventDefault() {} });
    assert.equal(providerCallsOf('createProvider'), 1);
    assert.equal(saveCallCount(), 0, '未勾选默认时创建不触发 config CAS');
    assert.deepEqual(JSON.parse(JSON.stringify(providerCalls.find(c => c[0] === 'createProvider')[1])), {
      name: 'Eval Prov', provider_type: 'openai-compatible',
      base_url: 'https://api.eval.com/v1', model: 'gpt-eval',
      supports_vision: true, api_key: 'sk-eval-1',
    }, '创建载荷形状对齐主注册表 API');
    assert.equal(body.children.length, 0, '创建成功后 modal 关闭');
    assert.equal(providerCallsOf('listProviders'), 1, '创建成功重拉列表');
    assert.equal(jmCallCount(), 1, '创建成功重拉供给投影');

    // I7a-默认勾选可用性：anthropic 行禁用并提示；类型切换同步清空/恢复
    const anthropicForm = await openEditModal(1); // prov-2（anthropic）
    const anthCheckbox = defaultCheckboxOf(anthropicForm);
    assert.equal(anthCheckbox.disabled, true, 'anthropic 行默认勾选禁用');
    assert.equal(anthCheckbox.checked, false, 'anthropic 行默认勾选不勾选');
    assert.match(body.textContent, /协议不支持作为评估默认/);
    find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();
    await openEditModal(0); // prov-1（openai）
    const openaiForm = find(body, node => node.tag === 'form');
    const openaiTypeSelect = find(openaiForm, node => node.id === 'eval-provider-type');
    const openaiCheckbox = defaultCheckboxOf(openaiForm);
    assert.equal(openaiCheckbox.disabled, false, 'openai 行默认勾选可用');
    openaiTypeSelect.value = 'anthropic';
    openaiTypeSelect.handlers.change();
    assert.equal(openaiCheckbox.disabled, true, '切换 anthropic 后勾选禁用');
    assert.equal(openaiCheckbox.checked, false, '切换 anthropic 后勾选清空');
    assert.match(body.textContent, /协议不支持作为评估默认/);
    openaiTypeSelect.value = 'openai-compatible';
    openaiTypeSelect.handlers.change();
    assert.equal(openaiCheckbox.disabled, false, '切回 openai 后勾选恢复可用');
    find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

    // I7b) 编辑 modal：预填 + API Key 三态（留空省略 / 纯空白清空 / 值更新）
    await findButton('编辑').handlers.click();
    let editForm = find(body, node => node.tag === 'form');
    assert.equal(find(editForm, node => node.id === 'eval-provider-name').value, 'Prov One',
      '编辑 modal 预填名称');
    assert.equal(find(editForm, node => node.id === 'eval-provider-api-key').value, '',
      '密钥脱敏：不回填');
    find(editForm, node => node.id === 'eval-provider-model').value = 'gpt-provider-model-2';
    await editForm.handlers.submit({ preventDefault() {} });
    let lastUpdate = providerCalls.filter(c => c[0] === 'updateProvider').at(-1);
    assert.equal(lastUpdate[1], 'prov-1', '编辑提交 updateProvider（目标 id）');
    assert.ok(!('api_key' in lastUpdate[2]), '编辑留空时请求省略 api_key');
    assert.equal(lastUpdate[2].model, 'gpt-provider-model-2');
    assert.equal(body.children.length, 0, '编辑成功后 modal 关闭');
    assert.equal(jmCallCount(), 2, '编辑成功重拉供给投影');

    await findButton('编辑').handlers.click();
    editForm = find(body, node => node.tag === 'form');
    find(editForm, node => node.id === 'eval-provider-api-key').value = '  ';
    await editForm.handlers.submit({ preventDefault() {} });
    lastUpdate = providerCalls.filter(c => c[0] === 'updateProvider').at(-1);
    assert.equal(lastUpdate[2].api_key, '', '纯空白输入发送 api_key=""');

    await findButton('编辑').handlers.click();
    editForm = find(body, node => node.tag === 'form');
    find(editForm, node => node.id === 'eval-provider-api-key').value = 'sk-rotated';
    providerBehavior = 'request_failed';
    await editForm.handlers.submit({ preventDefault() {} });
    assert.equal(body.children.length, 1, 'CRUD 失败保留表单草稿（modal 不关闭）');
    assert.match(body.textContent, /保存失败/);
    providerBehavior = 'ok';
    await editForm.handlers.submit({ preventDefault() {} });
    lastUpdate = providerCalls.filter(c => c[0] === 'updateProvider').at(-1);
    assert.equal(lastUpdate[2].api_key, 'sk-rotated', '重试提交保留的密钥输入');
    assert.equal(body.children.length, 0, '成功后 modal 关闭');

    // I7c) 关闭 modal 清空密钥输入（× 与 取消 两条路径行为一致）
    await findButton('编辑').handlers.click();
    editForm = find(body, node => node.tag === 'form');
    const raceKeyInput = find(editForm, node => node.id === 'eval-provider-api-key');
    raceKeyInput.value = 'sk-temp';
    find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();
    assert.equal(raceKeyInput.value, '', '关闭后密钥输入清空');
    assert.equal(body.children.length, 0);
    await findButton('编辑').handlers.click();
    editForm = find(body, node => node.tag === 'form');
    const cancelKeyInput = find(editForm, node => node.id === 'eval-provider-api-key');
    cancelKeyInput.value = 'sk-temp-2';
    findButton('取消', editForm).handlers.click();
    assert.equal(cancelKeyInput.value, '', '取消路径同样清空密钥输入');
    assert.equal(body.children.length, 0);
    // backdrop 空白点击关闭同样清空密钥输入
    await findButton('编辑').handlers.click();
    editForm = find(body, node => node.tag === 'form');
    const backdropKeyInput = find(editForm, node => node.id === 'eval-provider-api-key');
    backdropKeyInput.value = 'sk-temp-3';
    const backdropNode = find(body,
      node => node.className === 'modal-backdrop evaluations-modal');
    assert.ok(backdropNode, 'modal backdrop 挂载于 body');
    backdropNode.handlers.click({ target: backdropNode });
    assert.equal(backdropKeyInput.value, '', 'backdrop 空白点击关闭同样清空密钥输入');
    assert.equal(body.children.length, 0);

    // I7d) 删除走确认 modal；is_active 行禁删；默认但非 active 行允许删除
    // prov-1 重新设为默认（编辑 modal 勾选路径）
    const resetDefaultForm = await openEditModal(0);
    defaultCheckboxOf(resetDefaultForm).checked = true;
    await resetDefaultForm.handlers.submit({ preventDefault() {} });
    await flush();
    confirmAnswer = false;
    const delDefaultRow = findButton('删除');
    assert.ok(delDefaultRow, '默认但非 active 的行允许删除');
    await delDefaultRow.handlers.click();
    assert.equal(providerCallsOf('deleteProvider'), 0, '取消确认不删除');
    assert.match(confirmCalls.at(-1), /Prov One/, '确认弹窗指明删除对象');
    confirmAnswer = true;
    await delDefaultRow.handlers.click();
    assert.equal(providerCallsOf('deleteProvider'), 1);
    assert.equal(providerCalls.find(c => c[0] === 'deleteProvider')[1], 'prov-1');
    assert.ok(findAll(root, node => node.tag === 'button'
      && node.textContent === '删除' && !node.handlers.click).length >= 1,
      'is_active 行删除按钮禁用');
    assert.equal(findAll(root, node => node.className === 'muted'
      && node.textContent === 'Agent 启用中').length, 0,
      'is_active 禁删不带冗余提示');
    // 切页后才返回的确认：不执行删除（renderToken 失效校验）
    let releaseConfirm;
    const realModalConfirm = context.NAGENT.modal.confirm;
    context.NAGENT.modal.confirm = () => new Promise(resolve => { releaseConfirm = resolve; });
    const delTokenBtn = findButton('删除');
    const delTokenClick = delTokenBtn.handlers.click();
    await flush();
    const delCallsBeforeToken = providerCallsOf('deleteProvider');
    await moduleApi.refresh(state('evaluators')); // 切页使 renderToken 失效
    releaseConfirm(true);
    await delTokenClick;
    assert.equal(providerCallsOf('deleteProvider'), delCallsBeforeToken,
      '切页后确认不执行删除');
    context.NAGENT.modal.confirm = realModalConfirm;

    // I8) 创建表单 datalist：候选 = 默认 Provider model + env 基线模型 +
    //     既有定义模型（去重）；预填优先 effective_model；特殊字符安全渲染
    jmProjection = { judge_ready: true, supply_source: 'provider',
      effective_model: 'gpt-provider-model', env_baseline_model: 'env-model-x',
      provider_base_url_host: 'api.example.com', default_provider_id: 'prov-1',
      default_provider_name: 'Prov One', default_provider_model: 'gpt-provider-model',
      default_provider_missing: false, config_version: 7 };
    listResult = { items: [
      { definition_id: 'e1', version: 'v1', metric_name: 'agent.answer_relevance',
        target_kind: 'agent_run', method: 'llm_judge', judge_model: 'gpt-a',
        usage: { online_enabled: false, revision: 1 } },
      { definition_id: 'e2', version: 'v1', metric_name: 'agent.answer_relevance',
        target_kind: 'agent_run', method: 'llm_judge', judge_model: 'gpt-a',
        usage: { online_enabled: false, revision: 1 } },
      { definition_id: 'e3', version: 'v1', metric_name: 'agent.answer_relevance',
        target_kind: 'agent_run', method: 'llm_judge', judge_model: '',
        usage: { online_enabled: false, revision: 1 } },
      { definition_id: 'e4', version: 'v1', metric_name: 'agent.answer_relevance',
        target_kind: 'agent_run', method: 'llm_judge',
        judge_model: '<script>unsafe</script>',
        usage: { online_enabled: false, revision: 1 } },
    ], next_cursor: null };
    await moduleApi.refresh(state('evaluators'));
    alerts.length = 0;
    await findButton('新建评估器').handlers.click();
    await flush();
    const dlForm = find(body, node => node.tag === 'form');
    assert.ok(dlForm, '新建表单未打开');
    const dlMethod = find(dlForm, node => node.name === 'method');
    dlMethod.value = 'llm_judge';
    dlMethod.handlers.change();
    const dlInput = judgeModelInputOf(dlForm);
    assert.ok(dlInput, 'llm_judge 分支提供 judge_model 输入');
    assert.equal(dlInput.value, 'gpt-provider-model', '预填优先 effective_model');
    const datalist = datalistOf(dlForm);
    assert.ok(datalist, 'judge_model 输入提供 datalist');
    assert.equal(datalist.getAttribute('id'), dlInput.getAttribute('list'),
      'datalist 经 input[list] 关联');
    assert.deepEqual(
      findAll(datalist, node => node.tag === 'option').map(o => o.value),
      ['gpt-provider-model', 'env-model-x', 'gpt-a', '<script>unsafe</script>'],
      '候选 = 默认 Provider model + env 基线模型 + 既有定义模型（去重）');
    assert.match(body.textContent, /<script>unsafe<\/script>/,
      '特殊字符候选以 textContent 安全渲染');
    assert.ok(!createdTags.includes('script'), '不得创建 script 元素');
    // 用户键入后清空：per-modal touched 标记生效，不得再次回填默认
    dlInput.value = 'gpt-typed';
    dlInput.handlers.input();
    dlInput.value = '';
    dlInput.handlers.input();
    await flush();
    assert.equal(judgeModelInputOf(dlForm).value, '', '清空输入后不得再触发默认回填');
    // 方法切走再切回：保留编辑草稿
    dlInput.value = 'gpt-typed';
    dlInput.handlers.input();
    dlMethod.value = 'deterministic';
    dlMethod.handlers.change();
    dlMethod.value = 'llm_judge';
    dlMethod.handlers.change();
    assert.equal(judgeModelInputOf(dlForm).value, 'gpt-typed', '方法切走再切回保留编辑草稿');
    find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

    // I8a) 冷缓存缺口：先清空共享投影缓存（模拟从未加载），再打开表单——
    //      投影未返回时 datalist 无供给候选、输入留空；异步回填完成后
    //      datalist 重建并含预填值（候选与输入同批可见）
    let releaseColdProjection = null;
    const realGetJmCold = api.getJudgeModel;
    let coldCacheCleared = false;
    api.getJudgeModel = () => {
      if (!coldCacheCleared) {
        coldCacheCleared = true;
        return Promise.resolve(null); // 第一击：清空共享缓存
      }
      return new Promise(resolve => { releaseColdProjection = resolve; });
    };
    await findButton('新建评估器').handlers.click();
    const warmupForm = find(body, node => node.tag === 'form');
    find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();
    await flush(); // null 投影清空 judgeSupply.projection
    await moduleApi.refresh(state('evaluators'));
    await findButton('新建评估器').handlers.click();
    const coldForm = find(body, node => node.tag === 'form');
    const coldMethod = find(coldForm, node => node.name === 'method');
    coldMethod.value = 'llm_judge';
    coldMethod.handlers.change();
    await flush();
    const coldDatalist = datalistOf(coldForm);
    assert.ok(coldDatalist, '冷缓存仍提供 datalist');
    assert.ok(!findAll(coldDatalist, node => node.tag === 'option')
      .some(o => o.value === 'gpt-cold-model'),
      '投影未返回前 datalist 不含供给候选');
    assert.equal(judgeModelInputOf(coldForm).value, '', '投影未返回前输入留空');
    releaseColdProjection({ judge_ready: true, supply_source: 'provider',
      effective_model: 'gpt-cold-model', env_baseline_model: 'env-model-x',
      provider_base_url_host: 'api.example.com', default_provider_id: 'prov-1',
      default_provider_name: 'Prov One', default_provider_model: 'gpt-cold-model',
      default_provider_missing: false, config_version: 9 });
    await flush();
    assert.equal(judgeModelInputOf(coldForm).value, 'gpt-cold-model',
      '冷缓存异步回填完成时输入预填 effective_model');
    assert.deepEqual(
      findAll(datalistOf(coldForm), node => node.tag === 'option').map(o => o.value),
      ['gpt-cold-model', 'env-model-x', 'gpt-a', '<script>unsafe</script>'],
      '冷缓存异步回填完成时 datalist 重建并含预填值');
    api.getJudgeModel = realGetJmCold;
    find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();

    // I8b) 复制场景：source 冻结值始终优先，异步默认不覆盖
    const realDetail = api.getEvaluator;
    api.getEvaluator = async () => ({ definition: {
      definition_id: 'agent.answer_relevance', version: 'v1',
      metric_name: 'agent.answer_relevance', target_kind: 'agent_run',
      method: 'llm_judge', output_type: 'number', direction: 'higher',
      required_fields: [], judge_model: 'gpt-source-model',
      judge_params: { rubric: 'r' }, timeout_seconds: 30, material_hash: 'h',
    }, usage: { online_enabled: false, revision: 1 }, calibration: null });
    await moduleApi.refresh(state('evaluators'));
    await findButton('详情').handlers.click();
    await findButton('基于此版本新建', body).handlers.click();
    await flush();
    const srcForm = find(body, node => node.tag === 'form');
    const srcInput = judgeModelInputOf(srcForm);
    assert.equal(srcInput.value, 'gpt-source-model',
      '复制场景 source 冻结值优先且不被异步默认覆盖');
    find(srcForm, node => node.name === 'version').value = 'v9';

    // I9) 显式输入：原始值不 trim 直接发送；首尾空白/控制字符/>200 码点
    //     客户端报错不发请求；码点计数用 Array.from（增补平面字符按 1 计）
    createdPayloads = [];
    srcInput.value = '  gpt-edge  ';
    await srcForm.handlers.submit({ preventDefault() {} });
    assert.equal(createdPayloads.length, 0, '首尾空白不得提交');
    assert.match(body.textContent, /不得含首尾空白或控制字符/);
    srcInput.value = 'gpt\u0007x';
    await srcForm.handlers.submit({ preventDefault() {} });
    assert.equal(createdPayloads.length, 0, '控制字符不得提交');
    assert.match(body.textContent, /不得含首尾空白或控制字符/);
    srcInput.value = 'x'.repeat(201);
    await srcForm.handlers.submit({ preventDefault() {} });
    assert.equal(createdPayloads.length, 0, '>200 码点不得提交');
    assert.match(body.textContent, /不得含首尾空白或控制字符/);
    const cpModel = '\u{1D400}'.repeat(150);
    createdPayloads = [];
    srcInput.value = cpModel;
    await srcForm.handlers.submit({ preventDefault() {} });
    assert.equal(createdPayloads.length, 1, '150 个增补平面码点（UTF-16 长度 300）应放行');
    assert.equal(createdPayloads[0].judge_model, cpModel,
      'judge_model 原始值不 trim 直接提交');
    assert.equal(body.children.length, 0, '注册成功后弹窗关闭');

    // I9b) 方法切离 llm_judge：judge_model 字段不得进入提交载荷
    await moduleApi.refresh(state('evaluators'));
    await findButton('详情').handlers.click();
    await findButton('基于此版本新建', body).handlers.click();
    const srcForm2 = find(body, node => node.tag === 'form');
    const srcMethod2 = find(srcForm2, node => node.name === 'method');
    srcMethod2.value = 'deterministic';
    srcMethod2.handlers.change();
    find(srcForm2, node => node.name === 'version').value = 'v9';
    createdPayloads = [];
    await srcForm2.handlers.submit({ preventDefault() {} });
    assert.equal(createdPayloads.length, 1, 'deterministic 合法提交');
    assert.ok(!('judge_model' in createdPayloads[0]),
      '方法切离 llm_judge 不得提交 judge_model');
    api.getEvaluator = realDetail;

    // I8c) 无有效供给：预填留空；未触碰提交省略 judge_model（后端归一 None）
    jmProjection = { judge_ready: false, supply_source: null, effective_model: null,
      env_baseline_model: '', provider_base_url_host: null,
      default_provider_id: '', default_provider_name: null,
      default_provider_model: null, default_provider_missing: false,
      config_version: 8 };
    listResult = { items: [], next_cursor: null };
    await moduleApi.refresh(state('evaluators'));
    alerts.length = 0;
    await findButton('新建评估器').handlers.click();
    await flush();
    const emptyForm = find(body, node => node.tag === 'form');
    const emptyMethod = find(emptyForm, node => node.name === 'method');
    emptyMethod.value = 'llm_judge';
    emptyMethod.handlers.change();
    assert.equal(judgeModelInputOf(emptyForm).value, '', '无有效供给时预填留空');
    find(emptyForm, node => node.name === 'definition_id').value = 'judge.none';
    find(emptyForm, node => node.name === 'version').value = 'v1';
    createdPayloads = [];
    await emptyForm.handlers.submit({ preventDefault() {} });
    assert.equal(createdPayloads.length, 1, '无供给留空可提交');
    assert.ok(!('judge_model' in createdPayloads[0]),
      '未触碰且无供给时提交省略 judge_model');
    assert.equal(body.children.length, 0, '注册成功后弹窗关闭');

    // I10) 竞态：切页后迟到响应不改写新视图（renderToken 沿用现有键）
    let releaseLateProjection;
    const realGetJudgeModel2 = api.getJudgeModel;
    api.getJudgeModel = () => new Promise(resolve => { releaseLateProjection = resolve; });
    const lateRender = moduleApi.refresh(state('judge-model'));
    await flush();
    await moduleApi.refresh(state('evaluators'));
    api.getJudgeModel = realGetJudgeModel2;
    releaseLateProjection(jmProjection);
    await lateRender;
    assert.ok(findButton('新建评估器'), '切页后新视图保持评估器页');
    assert.doesNotMatch(root.textContent, /评估供给/, '迟到响应不得改写新视图');
    // 关 modal 后迟到失败：错误不回写、视图不改写
    await moduleApi.refresh(state('judge-model'));
    await findButton('编辑').handlers.click();
    const raceForm = find(body, node => node.tag === 'form');
    let rejectUpdate;
    const realUpdateProvider = topLevelApi.updateProvider;
    topLevelApi.updateProvider = () => new Promise((_, reject) => { rejectUpdate = reject; });
    const raceSubmit = raceForm.handlers.submit({ preventDefault() {} });
    await flush();
    const alertsBeforeRace = alerts.length;
    find(body, node => node.textContent === '×' && node.handlers.click).handlers.click();
    rejectUpdate(new Error('request_failed'));
    await raceSubmit;
    await flush();
    assert.equal(alerts.length, alertsBeforeRace, '关 modal 后迟到失败不弹错');
    assert.equal(body.children.length, 0, '关 modal 后迟到失败不重开弹窗');
    topLevelApi.updateProvider = realUpdateProvider;

    // I12) Provider 列表共享翻页（组件 2，slice 模式）：多于 LIST_PAGE_SIZE
    //      （20）时仅渲染 20 行且 pagerBar 显示正确页码；翻页渲染下一批；
    //      刷新列表（CRUD 成功同路径）后回第 1 页
    const realProvidersForPaging = providers;
    providers = [];
    for (let i = 1; i <= 25; i++) {
      providers.push({ id: 'prov-page-' + i,
        name: 'PageProv-' + String(i).padStart(2, '0'),
        provider_type: 'openai-compatible', base_url: 'https://p.example.com/v1',
        model: 'm-' + i, api_key_present: false, supports_vision: false,
        is_active: false });
    }
    providerCalls.length = 0;
    await moduleApi.refresh(state('judge-model'));
    const providerDeleteButtons = () => findAll(root, node => node.tag === 'button'
      && node.textContent === '删除' && node.handlers.click).length;
    assert.equal(providerDeleteButtons(), 20, '第 1 页仅渲染 LIST_PAGE_SIZE 行');
    assert.match(root.textContent, /第 1\/2 页/, 'pagerBar 显示第 1/2 页');
    assert.match(root.textContent, /PageProv-01/, '第 1 页含首批数据');
    assert.doesNotMatch(root.textContent, /PageProv-21/, '第 1 页不含第二批数据');
    const providerNextBtn = () => findAll(root, node => node.tag === 'button'
      && node.textContent === '下一页' && node.handlers.click)[0];
    assert.ok(providerNextBtn(), '第 1 页提供可点击的下一页');
    providerNextBtn().handlers.click();
    assert.equal(providerDeleteButtons(), 5, '翻页后第 2 页渲染剩余行');
    assert.match(root.textContent, /第 2\/2 页/, '翻页后 pagerBar 显示第 2/2 页');
    assert.match(root.textContent, /PageProv-21/, '第 2 页含第二批数据');
    assert.doesNotMatch(root.textContent, /PageProv-01/, '第 2 页不含第一批数据');
    // 刷新列表（首次进入/刷新/CRUD 成功都走 renderJudgeModel 重拉路径）回第 1 页
    await moduleApi.refresh(state('judge-model'));
    assert.equal(providerDeleteButtons(), 20, '刷新列表后回第 1 页仅渲染 20 行');
    assert.match(root.textContent, /第 1\/2 页/, '刷新列表后 pagerBar 回第 1/2 页');
    providers = realProvidersForPaging;

    // I11) 全程断言：评估默认/CRUD 操作零 activateProvider 调用
    assert.equal(activateCalls.length, 0,
      '评估页所有操作不得调用 Agent activate API');
    // 还原 mock 状态，避免污染后续（Part I 为最后一个 Part）
  }

  console.log('evaluation workbench harness passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
