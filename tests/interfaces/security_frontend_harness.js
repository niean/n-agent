'use strict';
// Minimal, dependency-free behavior harness for security.js.
// Run with: node tests/interfaces/security_frontend_harness.js
// Exits 0 on success, 1 on any failure. Loaded by test_security_frontend.py
// via subprocess; skipped when Node is unavailable.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SECURITY_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'security.js');
const code = fs.readFileSync(SECURITY_JS, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

function makeNode(tag) {
  const n = {
    tag: tag,
    className: '',
    _text: '',
    children: [],
    _listeners: {},
    set textContent(v) { this._text = (v === null || v === undefined) ? '' : String(v); this.children = []; },
    get textContent() { return this._text; },
    appendChild(c) { this.children.push(c); return c; },
    append() { for (let i = 0; i < arguments.length; i++) this.children.push(arguments[i]); },
    addEventListener(ev, fn) { (this._listeners[ev] = this._listeners[ev] || []).push(fn); },
    setAttribute() {},
    focus() {},
  };
  return n;
}

const elements = {};
function resetDom() {
  elements['tab-security'] = makeNode('div');
  elements['last-update'] = makeNode('span');
}
const document = {
  createElement: makeNode,
  getElementById: (id) => elements[id] || null,
};

function freshEnv(api) {
  resetDom();
  const ctx = { NAGENT: { api: api }, document: document, console: console };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  return ctx;
}

function tick() { return new Promise((r) => setTimeout(r, 0)); }

function stateFor(scope) {
  const tab = 'security-' + scope;
  return {
    activeTab: tab,
    renderTab: 'security',
    sidebarTab: tab,
    currentSubdomain: 'security',
    route: { scope: scope, tab: tab, renderTab: 'security', sidebarTab: tab, topnavParent: 'security' },
  };
}

function validPayload(version) {
  const meta = [
    ['turn', 'TurnPolicy', '轮次策略', '迭代上限、结束原因', 'AgentGraph 路由', 'turn_policy.py', [['iteration_limit', '迭代上限', 10]]],
    ['context', 'ContextPolicy', '上下文策略', '压缩阈值', 'ContextService', 'context_policy.py', [['context_length', '上下文长度', 32000]]],
    ['llm', 'LLMPolicy', 'LLM 策略', 'fallback', 'ModelService.call_llm', 'llm_policy.py', [['fallback_enabled', 'Fallback 启用', false]]],
    ['tool', 'ToolPolicy', '工具策略', '审批', 'ToolService.execute', 'tool_policy.py', [['version', '版本', 'system-v1']]],
    ['memory', 'MemoryPolicy', '记忆策略', '门控', 'RuntimeMemoryService', 'memory_policy.py', [['cross_session_read_enabled', '跨会话读', false]]],
    ['sandbox', 'SandboxPolicy', '沙盒策略', '授权', 'SandboxToolExecutor', 'sandbox_policy.py', [['timeout_seconds', '超时', 300]]],
    ['gateway', 'GatewayPolicy', '网关策略', '出站', 'GatewayService', 'gateway_policy.py', [['enabled', '启用', true]]],
    ['schedule', 'SchedulePolicy', '调度策略', 'cron', 'ScheduleRunService', 'schedule_policy.py', [['tick_seconds', '轮询', 30]]],
    ['budget', 'BudgetPolicy', '预算策略', '配额', 'BudgetService', 'budget_policy.py', [['max_token_cost', '最大Token', null], ['max_llm_calls', '最大LLM调用', 10]]],
    ['information_flow', 'InformationFlowPolicy', '信息流策略', '脱敏', 'InformationFlowService', 'information_flow_policy.py', [['redact_secrets', '脱敏密钥', true]]],
    ['delegation', 'DelegationPolicy', '委派策略', '多 Agent 委派并发、深度、预算、取消恢复', 'DelegationService、DelegationRunService', 'delegation_policy.py', [['enabled', '启用', true]]],
  ];
  return {
    profile_version: version || 'v1',
    policies: meta.map((m) => ({
      key: m[0], name: m[1], display_name: m[2], dimension: m[3], execution_point: m[4], domain_file: m[5],
      config: m[6].map((c) => ({ key: c[0], label: c[1], value: c[2] })),
    })),
  };
}

function sectors() {
  return elements['tab-security'].children.filter((c) => c.className === 'status-panel');
}
function headerText(sector) {
  const head = sector.children.find((c) => c.className === 'panel-header');
  return head ? allTexts(head).join('') : '';
}
function policySectors() {
  return sectors().filter((s) => headerText(s) !== '整体概览');
}
function overviewVersion() {
  const ov = sectors().find((s) => headerText(s) === '整体概览');
  if (!ov) return null;
  const body = ov.children.find((c) => c.className === 'panel-body');
  const bar = body && body.children.find((c) => c.className === 'stats-bar');
  const card = bar && bar.children[0];
  const val = card && card.children.find((c) => c.className === 'value');
  return val ? val.textContent : null;
}
function allTexts(node, out) {
  out = out || [];
  if (node._text) out.push(node._text);
  (node.children || []).forEach((c) => allTexts(c, out));
  return out;
}

async function main() {
  // 1. success renders 10 sectors
  let count = 0;
  const api1 = { listPolicies: () => { count++; return Promise.resolve(validPayload('v1')); } };
  const env1 = freshEnv(api1);
  env1.NAGENT.security.init(stateFor('overview'));
  await tick();
  ok(policySectors().length === 11, 'render 11 policy sectors, got ' + policySectors().length);
  ok(sectors().length === 12, 'overview + 11 policy sectors, got ' + sectors().length);
  ok(count === 1, 'init triggered 1 fetch, got ' + count);
  ok(overviewVersion() === 'v1', 'overview shows profile version v1, got ' + overviewVersion());
  // overview "Policy 数量" 值 = 11
  const ov = sectors().find((s) => headerText(s) === '整体概览');
  const bar = ov && ov.children.find((c) => c.className === 'panel-body').children.find((c) => c.className === 'stats-bar');
  const countCard = bar && bar.children[1];
  const countVal = countCard && countCard.children.find((c) => c.className === 'value');
  ok(countVal && countVal.textContent === '11', 'overview count card shows 11, got ' + (countVal && countVal.textContent));
  // 末项为委派策略
  const lastPolicy = policySectors()[policySectors().length - 1];
  ok(lastPolicy && headerText(lastPolicy) === '委派策略', 'last policy sector is 委派策略');

  // 2. header only has display_name (no name/domain_file)
  const llmSector = sectors().find((s) => headerText(s) === 'LLM 策略');
  ok(!!llmSector, 'llm sector found');
  const llmTexts = allTexts(llmSector).join('|');
  ok(llmTexts.indexOf('LLMPolicy') === -1, 'header does not leak Policy class name');
  ok(llmTexts.indexOf('llm_policy.py') === -1, 'header does not leak domain_file');

  // 3. bool -> 是/否, null -> -
  const budgetSector = sectors().find((s) => headerText(s) === '预算策略');
  const budgetTexts = allTexts(budgetSector).join('|');
  ok(budgetTexts.indexOf('-') !== -1, 'null value renders as -');
  const gatewaySector = sectors().find((s) => headerText(s) === '网关策略');
  ok(allTexts(gatewaySector).join('|').indexOf('是') !== -1, 'bool true renders as 是');
  ok(allTexts(llmSector).join('|').indexOf('否') !== -1, 'bool false renders as 否');

  // 4. failure -> 策略加载失败 + 重试, no leak
  const api2 = { listPolicies: () => Promise.reject(new Error('boom-secret')) };
  const env2 = freshEnv(api2);
  env2.NAGENT.security.init(stateFor('overview'));
  await tick();
  const errTexts = allTexts(elements['tab-security']).join('|');
  ok(errTexts.indexOf('策略加载失败') !== -1, 'failure shows 策略加载失败');
  ok(errTexts.indexOf('boom') === -1, 'failure does not leak exception text');
  ok(errTexts.indexOf('重试') !== -1, 'failure shows retry button');
  ok(sectors().length === 0, 'failure clears sectors, got ' + sectors().length);

  // 5. contract violation: 9 items -> failure
  const tooFew = validPayload(); tooFew.policies = tooFew.policies.slice(0, 9);
  const api3 = { listPolicies: () => Promise.resolve(tooFew) };
  const env3 = freshEnv(api3);
  env3.NAGENT.security.init(stateFor('overview'));
  await tick();
  ok(allTexts(elements['tab-security']).join('|').indexOf('策略加载失败') !== -1, '9 policies shows failure');
  ok(sectors().length === 0, '9 policies clears sectors, got ' + sectors().length);

  // 5b. contract violation: extra top-level field -> failure
  const extraTop = validPayload(); extraTop.extra = 'forbidden';
  const api3b = { listPolicies: () => Promise.resolve(extraTop) };
  const env3b = freshEnv(api3b);
  env3b.NAGENT.security.init(stateFor('overview'));
  await tick();
  ok(allTexts(elements['tab-security']).join('|').indexOf('策略加载失败') !== -1, 'extra top-level field shows failure');
  ok(sectors().length === 0, 'extra top-level field clears sectors, got ' + sectors().length);

  // 6. race: late request must not overwrite the latest
  let r1, r2, n = 0;
  const p1 = new Promise((res) => { r1 = res; });
  const p2 = new Promise((res) => { r2 = res; });
  const api4 = { listPolicies: () => { n++; return n === 1 ? p1 : p2; } };
  const env4 = freshEnv(api4);
  env4.NAGENT.security.refresh(stateFor('overview')); // token 1
  env4.NAGENT.security.refresh(stateFor('overview')); // token 2
  r2(validPayload('EARLY')); // latest resolves first
  await tick();
  ok(overviewVersion() === 'EARLY', 'latest request wins, got ' + overviewVersion());
  r1(validPayload('LATE')); // stale resolves late
  await tick();
  ok(overviewVersion() === 'EARLY', 'stale request ignored, got ' + overviewVersion());

  // 7. single init: second init() does not fetch again
  let m = 0;
  const api5 = { listPolicies: () => { m++; return Promise.resolve(validPayload('v1')); } };
  const env5 = freshEnv(api5);
  env5.NAGENT.security.init(stateFor('overview'));
  env5.NAGENT.security.init(stateFor('overview'));
  await tick();
  ok(m === 1, 'init called twice fetches once, got ' + m);

  // 8. Each security child uses its route scope to render the intended policy set.
  const expectedCounts = { overview: 11, sessions: 7, memory: 1, sandbox: 1 };
  for (const scope of Object.keys(expectedCounts)) {
    const env = freshEnv({ listPolicies: () => Promise.resolve(validPayload(scope)) });
    env.NAGENT.security.init(stateFor(scope));
    await tick();
    ok(policySectors().length === expectedCounts[scope],
      scope + ' renders ' + expectedCounts[scope] + ' policy sectors, got ' + policySectors().length);
  }

  // 9. Invalid or missing route scope is a safe, retryable load error.
  for (const badState of [stateFor('unknown'), { renderTab: 'security', route: {} }]) {
    const env = freshEnv({ listPolicies: () => Promise.resolve(validPayload()) });
    env.NAGENT.security.init(badState);
    await tick();
    ok(allTexts(elements['tab-security']).join('|').indexOf('策略加载失败') !== -1,
      'invalid or missing scope shows 策略加载失败');
  }

  // 10. The memory view rejects a payload missing its selected policy instead
  // of silently rendering an empty/stale scope. All remaining policies retain
  // the normal valid contract shape.
  const missingMemoryPayload = validPayload();
  missingMemoryPayload.policies = missingMemoryPayload.policies.filter((p) => p.key !== 'memory');
  const missingMemory = freshEnv({ listPolicies: () => Promise.resolve(missingMemoryPayload) });
  missingMemory.NAGENT.security.init(stateFor('memory'));
  await tick();
  ok(allTexts(elements['tab-security']).join('|').indexOf('策略加载失败') !== -1,
    'missing selected memory policy shows 策略加载失败 (policy_load_failed)');
  ok(sectors().length === 0, 'missing selected memory policy clears stale sectors');

  // 11. Retry remembers the selected sessions scope.
  let sessionAttempts = 0;
  const sessionScopes = [];
  const retryEnv = freshEnv({ listPolicies: (scope) => {
    sessionAttempts++;
    sessionScopes.push(scope);
    if (sessionAttempts === 1) return Promise.reject(new Error('offline'));
    return Promise.resolve(validPayload(scope));
  } });
  retryEnv.NAGENT.security.init(stateFor('sessions'));
  await tick();
  const retryButton = (function findButton(node) {
    if (node && node.tag === 'button' && node.textContent === '重试') return node;
    for (const child of (node && node.children) || []) {
      const found = findButton(child);
      if (found) return found;
    }
    return null;
  }(elements['tab-security']));
  ok(!!retryButton, 'sessions failure exposes retry button');
  if (retryButton) retryButton.click ? retryButton.click() : (retryButton._listeners.click || []).forEach((fn) => fn());
  await tick();
  ok(sessionAttempts === 2, 'sessions retry performs a second request');
  ok(sessionScopes.length === 2 && sessionScopes[0] === 'sessions' && sessionScopes[1] === 'sessions',
    'sessions retry requests the sessions scope both times');
  ok(policySectors().length === 7, 'sessions retry retains sessions scope (got ' + policySectors().length + ')');

  // 12. A late sessions response cannot overwrite a later memory activation.
  let resolveSessions;
  const delayedSessions = new Promise((resolve) => { resolveSessions = resolve; });
  const raceEnv = freshEnv({ listPolicies: (scope) => scope === 'sessions'
    ? delayedSessions : Promise.resolve(validPayload(scope)) });
  raceEnv.NAGENT.security.init(stateFor('sessions'));
  ok(typeof raceEnv.NAGENT.security.activate === 'function', 'security exports activate(state)');
  if (typeof raceEnv.NAGENT.security.activate === 'function') {
    raceEnv.NAGENT.security.activate(stateFor('memory'));
    await tick();
    ok(policySectors().length === 1, 'later memory activation renders one memory sector');
    resolveSessions(validPayload('sessions'));
    await tick();
    ok(policySectors().length === 1, 'late sessions response does not overwrite memory');
  } else {
    resolveSessions(validPayload('sessions'));
    await tick();
  }

  if (failures) { console.error(failures + ' failure(s)'); process.exit(1); }
  console.log('OK security frontend harness passed');
  process.exit(0);
}

main().catch((e) => { console.error('HARNESS ERROR', e); process.exit(1); });
