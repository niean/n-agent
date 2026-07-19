// Node behavior harness for app/interfaces/http/static/host.js
// No external deps. Mocks DOM + NAGENT.ui/api just enough to exercise load()
// and assert: 3 panels when enabled, unavailable placeholder when enabled:false,
// status failure shows version-mismatch error (not "未启用"), per-panel error
// isolation, and no write-operation buttons.
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
    addEventListener() {},
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

function makeApi({ status, policy, history, statusReject }) {
  return {
    host: {
      getStatus: () => (statusReject ? Promise.reject(new Error('request_failed')) : Promise.resolve(status)),
      getPolicy: () => Promise.resolve(policy),
      listHistory: () => Promise.resolve(history),
    },
  };
}

function loadHost(api, ui, dom) {
  const sandbox = {
    window: {},
    document: dom,
    NAGENT: { ui, api },
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
  assert(dom.root.children.length === 3, 'enabled 时渲染 3 个面板，实际 ' + dom.root.children.length);
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
  assert(dom.root.children.length === 3, 'status 失败仍渲染 3 面板，实际 ' + dom.root.children.length);
  const statusPanelText = collectText(dom.root.children[0]);
  assert(statusPanelText.indexOf('未启用') < 0, 'status 失败不误报未启用');
  assert(statusPanelText.indexOf('部署版本') >= 0 || statusPanelText.indexOf('加载状态失败') >= 0, 'status 失败显示版本不匹配/加载失败');

  if (failures) { console.error(`\n${failures} assertion(s) failed`); process.exit(1); }
  console.log('\nall host frontend harness assertions passed');
}

run();
