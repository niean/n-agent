// Node behavior harness for app/interfaces/http/static/host.js
// No external deps. Mocks DOM + NAGENT.ui/api just enough to exercise load()
// and assert: 4 panels when enabled, unavailable placeholder when enabled:false,
// status failure shows version-mismatch error (not "未启用"), per-panel error
// isolation, and confirmed grant revocation.
// Usage: node tests/interfaces/host_terminal_frontend_harness.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function makeEl(tag) {
  const el = {
    tag, className: '', id: '', textContent: '', type: '', colSpan: null,
    children: [], attrs: {}, dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(n) { this.children.push(n); return n; },
    append(...ns) { ns.forEach((n) => this.children.push(n)); },
    replaceChildren() { this.children = []; },
    addEventListener(name, fn) { (this.events || (this.events = {}))[name] = fn; },
    removeEventListener() {},
    querySelector(sel) {
      const cls = sel.replace(/^\./, '');
      for (const c of this.children) if (c && c.className === cls) return c;
      return null;
    },
    querySelectorAll(sel) {
      const cls = sel.replace(/^\./, '');
      return this.children.filter((c) => c && c.className === cls);
    },
    focus() {},
  };
  return el;
}

function makeDom(rootId) {
  const root = makeEl('div');
  root.id = rootId;
  return {
    root,
    getElementById: (id) => (id === rootId ? root : null),
    createElement: makeEl,
    body: makeEl('body'),
  };
}

function makeUi(dom) {
  return {
    el: (tag, cls) => { const n = dom.createElement(tag); if (cls) n.className = cls; return n; },
    byId: (id) => dom.getElementById(id),
    clear: (n) => { if (n) n.replaceChildren(); },
    renderEmpty: (n, m) => { if (n) { n.replaceChildren(); n.textContent = m || '暂无数据'; } },
    renderLoading: (n, m) => { if (n) { n.replaceChildren(); n.textContent = m || '加载中...'; } },
    renderError: (n, m) => { if (n) { n.replaceChildren(); n.textContent = m || '加载失败'; } },
  };
}

function makeApi({ status, policy, history, statusReject, grants, grantsReject, revoke }) {
  return {
    host: {
      getStatus: () => (statusReject ? Promise.reject(new Error('request_failed')) : Promise.resolve(status)),
      getPolicy: () => Promise.resolve(policy),
      listHistory: () => Promise.resolve(history),
      listScriptGrants: () => grantsReject ? Promise.reject(new Error('grants_offline')) : Promise.resolve(grants || {enabled:true, grants:[]}),
      revokeScriptGrant: revoke || (() => Promise.resolve({success:true})),
    },
  };
}

function loadHost(api, ui, dom, modal) {
  const sandbox = {
    window: {},
    document: dom,
    NAGENT: { ui, api, modal },
    Promise,
    Date,
    isNaN,
  };
  sandbox.window.NAGENT = sandbox.NAGENT;
  sandbox.global = sandbox;
  const code = fs.readFileSync(
    path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'host.js'),
    'utf8',
  );
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return sandbox.NAGENT.host;
}

function collectText(el) {
  let out = (el.textContent || '');
  for (const c of el.children) out += ' ' + collectText(c);
  return out;
}

function collectButtons(el, acc) {
  acc = acc || [];
  if (el.tag === 'button' || el.type === 'button') {
    acc.push((el.textContent || '').trim());
  }
  for (const c of el.children) collectButtons(c, acc);
  return acc;
}

async function run() {
  let failures = 0;
  function assert(cond, msg) {
    if (!cond) { failures++; console.error('FAIL:', msg); } else console.log('ok  :', msg);
  }

  const enabledStatus = {
    enabled: true, health_code: 'ok', policy_version: 'v1', policy_loaded_at: null,
    policy_content_digest: 'abcd1234', policy_last_error: null,
    limits_summary: { default_timeout_seconds: 120, max_concurrency: 1, max_stdout_bytes: 8192, max_stderr_bytes: 8192 },
  };
  const enabledPolicy = {
    enabled: true, limits: { max_concurrency: 1 }, command_rules: [], skill_script_rules: [],
  };

  // 1. enabled -> 3 status-panel sections
  let dom = makeDom('tab-executors-host');
  let host = loadHost(makeApi({ status: enabledStatus, policy: enabledPolicy, history: [] }), makeUi(dom), dom);
  await host.load();
  assert(dom.root.children.length === 4, 'enabled 时渲染 4 个面板，实际 ' + dom.root.children.length);
  const titles = dom.root.children.map((c) => c.children[0] && c.children[0].children[0] && c.children[0].children[0].textContent);
  assert(titles.indexOf('执行器状态') >= 0 && titles.indexOf('授权策略') >= 0 && titles.indexOf('执行历史') >= 0, '三个面板标题正确: ' + JSON.stringify(titles));

  // 2. no write-operation buttons anywhere
  const btns = collectButtons(dom.root);
  const writeLabels = btns.filter((b) => /刷新|删除|启停|启用|停用|编辑|重载/.test(b));
  assert(writeLabels.length === 0, '无写操作按钮，实际按钮: ' + JSON.stringify(btns));
  // only "详情" buttons allowed
  assert(btns.every((b) => b === '详情' || b === '×' || b === '关闭' || b === ''), '仅有详情/关闭按钮');

  // 3. enabled:false -> unavailable placeholder, single child
  dom = makeDom('tab-executors-host');
  host = loadHost(makeApi({ status: { enabled: false, health_code: 'host_terminal_disabled' }, policy: { enabled: false }, history: [] }), makeUi(dom), dom);
  await host.load();
  assert(dom.root.children.length === 1, 'enabled:false 时整页占位，单子节点，实际 ' + dom.root.children.length);
  assert(collectText(dom.root).indexOf('未启用') >= 0, '占位含"未启用"');
  assert(collectText(dom.root).indexOf('host_terminal_disabled') >= 0, '占位含原因码');

  // 4. status request rejected -> version-mismatch error, NOT "未启用"
  dom = makeDom('tab-executors-host');
  host = loadHost(makeApi({ status: null, policy: enabledPolicy, history: [], statusReject: true }), makeUi(dom), dom);
  await host.load();
  assert(dom.root.children.length === 4, 'status 失败仍渲染 4 面板，实际 ' + dom.root.children.length);
  const statusPanelText = collectText(dom.root.children[0]);
  assert(statusPanelText.indexOf('未启用') < 0, 'status 失败不误报未启用');
  assert(statusPanelText.indexOf('部署版本') >= 0 || statusPanelText.indexOf('加载状态失败') >= 0, 'status 失败显示版本不匹配/加载失败');


  // Grant actions exercise actual event handlers, including precision and failure persistence.
  function findButton(el, label) {
    if (el.tag === 'button' && el.textContent === label) return el;
    for (const child of el.children) { const found = findButton(child, label); if (found) return found; }
    return null;
  }
  let confirmed = false, revokes = [], alerts = [];
  const row = {revision:'9007199254740993', skill_name:'demo', script_relative_path:'scripts/run.py', sha256:'a'.repeat(64), origin:'foreground', updated_at:'2026-11-06T00:00:00Z', state:'stale', error:'hash_mismatch'};
  dom = makeDom('tab-executors-host');
  host = loadHost(makeApi({status:enabledStatus, policy:enabledPolicy, history:[], grants:{enabled:true,grants:[row]}, revoke:async (id) => {revokes.push(id); return {success:false,state:'pending_revoke',error:'pending_sync'};}}), makeUi(dom), dom, {confirm:async()=>confirmed,alert:async(m)=>alerts.push(m)});
  await host.load();
  let button = findButton(dom.root, '撤销');
  assert(!!button, '授权行有撤销按钮');
  assert(collectText(dom.root).includes('foreground') && collectText(dom.root).includes('hash_mismatch') && collectText(dom.root).includes('2026-11-06 08:00:00'), '授权显示来源、错误与 UTC+8 时间');
  if (button) {
    await button.events.click();
    assert(revokes.length === 0, '取消确认不撤销');
    confirmed = true;
    await button.events.click();
    assert(revokes[0] === row.revision, '确认撤销保留大 revision 字符串精度');
    assert(alerts[0].includes('pending_sync'), '待撤销错误调用共享 alert');
    assert(collectText(dom.root).includes('pending_revoke') && collectText(dom.root).includes('pending_sync'), '刷新列表后保留待撤销状态与错误');
  }
  dom = makeDom('tab-executors-host');
  let releaseRevoke;
  host = loadHost(makeApi({status:enabledStatus,policy:enabledPolicy,history:[],grants:{enabled:true,grants:[row]},revoke:()=>new Promise((resolve,reject)=>{releaseRevoke=reject;})}),makeUi(dom),dom,{confirm:async()=>true,alert:async(m)=>alerts.push(m)});
  await host.load();
  button = findButton(dom.root, '撤销');
  const pendingClick = button.events.click();
  await new Promise(setImmediate);
  assert(button.disabled === true, '撤销请求期间禁用按钮');
  releaseRevoke(new Error('host_script_grants_stale_revision'));
  await pendingClick;
  assert(collectText(dom.root).includes('host_script_grants_stale_revision'), '旧 revision 错误刷新后仍可见');
  dom = makeDom('tab-executors-host');
  host = loadHost(makeApi({status:enabledStatus,policy:enabledPolicy,history:[],grants:{enabled:true,grants:[{...row,state:'prepared'}]}}),makeUi(dom),dom);
  await host.load();
  assert(!findButton(dom.root, '撤销'), 'prepared 意图不可撤销');
  dom = makeDom('tab-executors-host');
  host = loadHost(makeApi({status:enabledStatus,policy:enabledPolicy,history:[],grantsReject:true}),makeUi(dom),dom);
  await host.load();
  assert(collectText(dom.root.children[0]).includes('ok') && collectText(dom.root).includes('grants_offline'), '授权列表错误不影响状态面板');

  dom = makeDom('tab-executors-host');
  let grantReads = 0;
  alerts = [];
  const reloadApi = makeApi({status:enabledStatus,policy:enabledPolicy,history:[]});
  reloadApi.host.listScriptGrants = async () => {
    if (grantReads++) throw new Error('reload_offline');
    return {enabled:true,grants:[row]};
  };
  host = loadHost(reloadApi,makeUi(dom),dom,{confirm:async()=>true,alert:async(m)=>alerts.push(m)});
  await host.load();
  await findButton(dom.root, '撤销').events.click();
  assert(alerts.some((message)=>message.includes('reload_offline')), '撤销后重新加载失败调用共享 alert');
  assert(collectText(dom.root).includes('reload_offline'), '撤销后重新加载失败保留行内错误');

  if (failures) { console.error(`\n${failures} assertion(s) failed`); process.exit(1); }
  console.log('\nall host frontend harness assertions passed');
}

run();
