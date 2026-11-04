'use strict';
// Harness for chat.js /task slash command. Loads chat.js in a Node vm with
// minimal DOM/api stubs and exercises parseTaskCommand (pure), runTaskCommand
// (dispatch + error mapping + 任务指令 system message) and send() routing.
// Run: node tests/interfaces/chat_frontend_harness.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CHAT_JS = path.join(__dirname, '..', '..', 'app', 'interfaces', 'http', 'static', 'chat.js');
const code = fs.readFileSync(CHAT_JS, 'utf8');

let failures = 0;
function ok(cond, msg) { if (!cond) { failures++; console.error('FAIL: ' + msg); } }

// --- stub element -----------------------------------------------------------
// Module-level reference to the current document (set in freshStubs) so that
// el.focus() can update document.activeElement.
let _currentDoc = null;

function makeEl() {
  const kids = [];
  const el = {
    _dead: false,
    className: '',
    classList: {
      add() { const parts = (el.className || '').split(/\s+/).filter(Boolean); for (const t of arguments) { if (parts.indexOf(t) === -1) parts.push(t); } el.className = parts.join(' '); },
      remove() { const parts = (el.className || '').split(/\s+/).filter(Boolean); const args = Array.prototype.slice.call(arguments); el.className = parts.filter(function (c) { return args.indexOf(c) === -1; }).join(' '); },
      toggle(token, force) { const parts = (el.className || '').split(/\s+/).filter(Boolean); const has = parts.indexOf(token) !== -1; if (force !== undefined) { if (force && !has) { parts.push(token); el.className = parts.join(' '); return true; } if (!force && has) { el.className = parts.filter(function (c) { return c !== token; }).join(' '); return false; } return force; } if (has) { el.className = parts.filter(function (c) { return c !== token; }).join(' '); return false; } parts.push(token); el.className = parts.join(' '); return true; },
      contains(token) { return (el.className || '').split(/\s+/).indexOf(token) !== -1; },
    },
    style: {},
    dataset: {},
    hidden: false,
    disabled: false,
    tagName: 'DIV',
    _kids: kids,
    _attrs: {},
    _listeners: {},
    appendChild(c) {
      if (c && c.tagName === '#document-fragment') {
        while (c._kids && c._kids.length > 0) { const child = c._kids.shift(); child.parentNode = this; kids.push(child); }
        return c;
      }
      c.parentNode = this; kids.push(c); return c;
    },
    removeChild(c) { const i = kids.indexOf(c); if (i >= 0) kids.splice(i, 1); c.parentNode = null; c._dead = true; markDescendantsDead(c); return c; },
    replaceChild(next, previous) { const i = kids.indexOf(previous); if (i >= 0) { previous.parentNode = null; previous._dead = true; markDescendantsDead(previous); next.parentNode = this; kids[i] = next; } return previous; },
    replaceChildren() { kids.forEach((k) => { k.parentNode = null; k._dead = true; markDescendantsDead(k); }); kids.length = 0; },
    append() { const args = Array.prototype.slice.call(arguments); args.forEach((c) => { if (c && c.tagName === '#document-fragment') { while (c._kids && c._kids.length > 0) { const child = c._kids.shift(); child.parentNode = this; kids.push(child); } } else { c.parentNode = this; kids.push(c); } }); },
    insertBefore(newNode, referenceNode) {
      // 标准 DOM 语义：referenceNode 必须是当前子节点；不合法抛 NotFoundError
      if (referenceNode == null) {
        // 仿浏览器行为：referenceNode = null 时追加到末尾
        if (newNode && newNode.tagName === '#document-fragment') {
          while (newNode._kids && newNode._kids.length > 0) { const child = newNode._kids.shift(); child.parentNode = this; kids.push(child); }
          return newNode;
        }
        newNode.parentNode = this; kids.push(newNode); return newNode;
      }
      const i = kids.indexOf(referenceNode);
      if (i < 0) {
        const err = new Error('insertBefore: reference node is not a child of this node');
        err.name = 'NotFoundError';
        err.code = 8;
        throw err;
      }
      if (newNode && newNode.tagName === '#document-fragment') {
        const frags = (newNode._kids || []).slice();
        newNode._kids.length = 0;
        // 按原顺序逐个插入到 referenceNode 之前（保持 fragment 内部顺序）
        for (let k = 0; k < frags.length; k++) {
          frags[k].parentNode = this;
          kids.splice(i + k, 0, frags[k]);
        }
        return newNode;
      }
      newNode.parentNode = this;
      kids.splice(i, 0, newNode);
      return newNode;
    },
    remove() { if (el.parentNode) el.parentNode.removeChild(el); },
    contains(node) {
      for (let cur = node; cur; cur = cur.parentNode) if (cur === el) return true;
      return false;
    },
    get isConnected() {
      if (!_currentDoc) return false;
      return el === _currentDoc.body || _currentDoc.body.contains(el);
    },
    addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
    removeEventListener(type, fn) {
      this._listeners[type] = (this._listeners[type] || []).filter((listener) => listener !== fn);
    },
    dispatchEvent(ev) { (this._listeners[(ev && ev.type) || ''] || []).forEach((fn) => fn(ev)); },
    closest(selector) {
      // 支持 tag / #id / .cls 简单选择器；沿 parentNode 向上逐个检查
      if (typeof selector !== 'string' || !selector) return null;
      const match = (node) => {
        if (!node) return false;
        if (selector.charAt(0) === '#') {
          // 同时检查 _attrs.id 和 node.id（chat.js 直接给容器赋 id 字段）
          const wanted = selector.slice(1);
          return (node._attrs && node._attrs.id === wanted) || node.id === wanted;
        }
        if (selector.charAt(0) === '.') {
          const cls = selector.slice(1);
          return !!(node.className && node.className.split(/\s+/).indexOf(cls) !== -1);
        }
        return node.tagName === selector.toUpperCase();
      };
      for (let cur = el; cur; cur = cur.parentNode) {
        if (match(cur)) return cur;
      }
      return null;
    },
    querySelector(selector) {
      // 支持：tag / .cls / #id / 任意后代链（无 >）/ parent > .child / #ancestor .descendant
      // 标准 DOM 选择器使用 getElementById/_attrs.className/_attrs.id 判定
      const sel = String(selector || '');
      // 拆分为简单 selector 段：按空白分隔（不拆带 # . 等的复合串）
      const parts = sel.split(/\s+/).filter(Boolean);
      // 匹配单段简单选择器（id / class / tag）
      const matchSimple = (node, part) => {
        if (!node) return false;
        // 复合段支持：tag#id.cls1.cls2（按 '.' 分段，首段可为 tag/#id/空）
        const segs = String(part).split('.');
        const head = segs[0];
        if (head.charAt(0) === '#') {
          if (!(node._attrs && node._attrs.id === head.slice(1))) return false;
        } else if (head) {
          if (node.tagName !== head.toUpperCase()) return false;
        }
        for (let si = 1; si < segs.length; si++) {
          if (!segs[si]) return false;
          if (!(node.className && node.className.split(/\s+/).indexOf(segs[si]) !== -1)) return false;
        }
        return true;
      };
      // 单段：走深度优先查找第一个匹配
      if (parts.length === 1) {
        const part = parts[0];
        if (part.charAt(0) === '#') {
          // id 查找：递归全树（DOM API 允许任意位置 id 匹配）
          const wanted = part.slice(1);
          const visit = (node) => {
            if (!node) return null;
            if (node._attrs && node._attrs.id === wanted) return node;
            for (const child of node._kids || []) {
              const found = visit(child);
              if (found) return found;
            }
            return null;
          };
          return visit(this);
        }
        let found = null;
        const visit = (node) => {
          if (!node || found) return;
          if (matchSimple(node, part)) { found = node; return; }
          for (const child of node._kids || []) visit(child);
        };
        visit(this);
        return found;
      }
      // 多段：首段从 this 开始查，剩余段按后代关系
      const visitRoot = (node) => {
        if (!node) return null;
        if (matchSimple(node, parts[0])) {
          let cur = node;
          let ok = true;
          for (let p = 1; p < parts.length; p++) {
            let next = null;
            for (const c of (cur._kids || [])) {
              if (matchSimple(c, parts[p])) { next = c; break; }
            }
            if (!next) { ok = false; break; }
            cur = next;
          }
          if (ok) return cur;
        }
        for (const child of (node._kids || [])) {
          const r = visitRoot(child);
          if (r) return r;
        }
        return null;
      };
      return visitRoot(this);
    },
    querySelectorAll(selector) {
      // 支持：单段 class / tag / id，以及任意后代链（空格分隔），不只支持 > 子代
      const sel = String(selector || '');
      const parts = sel.split(/\s+/).filter(Boolean);
      const matchSimple = (node, part) => {
        if (!node) return false;
        // 复合段支持：tag#id.cls1.cls2（按 '.' 分段，首段可为 tag/#id/空）
        const segs = String(part).split('.');
        const head = segs[0];
        if (head.charAt(0) === '#') {
          if (!(node._attrs && node._attrs.id === head.slice(1))) return false;
        } else if (head) {
          if (node.tagName !== head.toUpperCase()) return false;
        }
        for (let si = 1; si < segs.length; si++) {
          if (!segs[si]) return false;
          if (!(node.className && node.className.split(/\s+/).indexOf(segs[si]) !== -1)) return false;
        }
        return true;
      };
      const out = [];
      if (parts.length === 1) {
        const part = parts[0];
        if (part.charAt(0) === '#') {
          const wanted = part.slice(1);
          const visit = (node) => {
            if (!node) return;
            if (node._attrs && node._attrs.id === wanted) out.push(node);
            for (const child of node._kids || []) visit(child);
          };
          visit(this);
          return out;
        }
        const visit = (node) => {
          if (!node) return;
          if (matchSimple(node, part)) out.push(node);
          for (const child of node._kids || []) visit(child);
        };
        visit(this);
        return out;
      }
      // 多段：标准后代关系
      const visitRoot = (node) => {
        if (!node) return;
        if (matchSimple(node, parts[0])) {
          let cur = node;
          let ok = true;
          for (let p = 1; p < parts.length; p++) {
            let next = null;
            for (const c of (cur._kids || [])) {
              if (matchSimple(c, parts[p])) { next = c; break; }
            }
            if (!next) { ok = false; break; }
            cur = next;
          }
          if (ok) out.push(cur);
        }
        for (const child of (node._kids || [])) visitRoot(child);
      };
      visitRoot(this);
      return out;
    },
    setAttribute(name, value) { this._attrs[name] = String(value); },
    getAttribute(name) { return this._attrs[name] !== undefined ? this._attrs[name] : null; },
    hasAttribute(name) { return this._attrs && Object.prototype.hasOwnProperty.call(this._attrs, name) && this._attrs[name] !== null && this._attrs[name] !== undefined; },
    focus() { if (_currentDoc) _currentDoc.activeElement = this; },
    click() {
      // 走真实 capture->target->bubble 派发，使 popover 等的 stopPropagation/preventDefault 链路正确触发。
      // 调用元素自身的 click 不再仅做内部 dispatchEvent，测试也应使用 fireClick 走真 capture->target->bubble。
      if (typeof dispatchClick === 'function') dispatchClick(this);
      else this.dispatchEvent({ type: 'click' });
    },
    get firstChild() { return kids[0] || null; },
    get textContent() {
      return kids.map((c) => (c && typeof c._text === 'string') ? c._text : (c && c.textContent) || '').join('');
    },
    set textContent(v) { kids.length = 0; kids.push({ _text: String(v) }); },
  };
  return el;
}

// Capture/target/bubble 派发 click：document capture -> 祖先 capture -> 目标 -> 祖先 bubble -> document bubble。
// event 暴露 target/currentTarget/stopPropagation()/preventDefault()。listener 主动 e.stopPropagation() 阻断后续阶段。
function dispatchClick(target, opts) {
  opts = opts || {};
  const event = {
    type: 'click',
    target: target,
    currentTarget: target,
    bubbles: true,
    cancelable: true,
    defaultPrevented: false,
    propagationStopped: false,
    stopPropagation() { this.propagationStopped = true; },
    stopImmediatePropagation() { this.propagationStopped = true; this.immediateStopped = true; },
    preventDefault() { this.defaultPrevented = true; },
  };
  // 构造祖先链：target.parentNode 一直上升到 document
  const ancestors = [];
  for (let cur = target && target.parentNode; cur; cur = cur.parentNode) {
    ancestors.push(cur);
  }
  // ancestors[0] 是最近祖先；列表逆序即从 document 到最近祖先
  // 1) document capture 阶段（已经由 document.addEventListener 默认 bubble，但 capture 须显式 addEventListener 第三参 true）
  if (_currentDoc && _currentDoc._captureListeners && _currentDoc._captureListeners.click) {
    for (const fn of _currentDoc._captureListeners.click.slice()) {
      if (event.propagationStopped) break;
      event.currentTarget = _currentDoc;
      fn(event);
    }
  }
  // 2) 祖先 capture 阶段（按 document->target 顺序）
  for (let i = ancestors.length - 1; i >= 0; i--) {
    if (event.propagationStopped) break;
    const a = ancestors[i];
    event.currentTarget = a;
    const capture = (a._captureListeners && a._captureListeners.click) || [];
    for (const fn of capture.slice()) fn(event);
  }
  // 3) target 阶段
  if (!event.propagationStopped) {
    event.currentTarget = target;
    const targetListeners = (target._listeners && target._listeners.click) || [];
    for (const fn of targetListeners.slice()) {
      if (event.immediateStopped) break;
      fn(event);
    }
  }
  // 4) 祖先 bubble 阶段（按 target->document 顺序）
  for (let i = 0; i < ancestors.length; i++) {
    if (event.propagationStopped) break;
    const a = ancestors[i];
    event.currentTarget = a;
    const bubble = (a._listeners && a._listeners.click) || [];
    for (const fn of bubble.slice()) fn(event);
  }
  // 5) document bubble 阶段
  if (_currentDoc && !event.propagationStopped) {
    event.currentTarget = _currentDoc;
    const docBubble = (_currentDoc._listeners && _currentDoc._listeners.click) || [];
    for (const fn of docBubble.slice()) fn(event);
  }
  return event;
}

// 标记一棵子树为 _dead（replaceChildren/replaceChild/removeChild 时调用，避免
// createdElements 平表里的旧节点被 querySelectorAll 误认为当前 DOM 节点）。
function markDescendantsDead(node) {
  if (!node) return;
  for (const c of (node._kids || [])) {
    c._dead = true;
    markDescendantsDead(c);
  }
}

// --- api stub (records calls) ----------------------------------------------
const taskCalls = [];
function taskApi() {
  return {
    create(payload) { taskCalls.push({ name: 'create', payload }); return Promise.resolve({ id: 't_1', title: payload && payload.title }); },
    list() { taskCalls.push({ name: 'list' }); return Promise.resolve({ items: listItems.slice() }); },
    get(id) { taskCalls.push({ name: 'get', id }); return Promise.resolve({ id, status: 'waiting_approval' }); },
    approve(id, note) { taskCalls.push({ name: 'approve', id, note }); return Promise.resolve({}); },
    reject(id, note) { taskCalls.push({ name: 'reject', id, note }); return Promise.resolve({}); },
    cancel(id) { taskCalls.push({ name: 'cancel', id }); return Promise.resolve({}); },
    retry(id) { taskCalls.push({ name: 'retry', id }); return Promise.resolve({}); },
    revise(id, note) { taskCalls.push({ name: 'revise', id, note }); return Promise.resolve({}); },
    board() { taskCalls.push({ name: 'board' }); return Promise.resolve({ columns: boardColumns.slice() }); },
  };
}

let listItems = [];
let fetchCalls = [];
let createdSessionId = null;
let appendCalls = [];
let sessionsList = [];
let boardColumns = [];
let scheduledTasks = [];
// SSE stream + fetch override state for tool-approval card tests.
let sseQueue = [];
let fetchOverrides = [];
let streamKeepOpen = false;
let streamEndResolver = null;
// T4: artifact panel fetch tracking (separate from main fetchCalls)
let artifactFetchCalls = [];
let artifactFetchHandler = null;
// T4: navigation.navigatePath spy tracking
let navPathCalls = [];
// T5: skills api replaceable handler + call records
let listSkillsCalls = [];
let listSkillsHandler = null;
// T5: replaceable session lifecycle handlers
let createSessionCalls = [];
let createSessionHandler = null;
let getSessionDetailCalls = [];
let getSessionDetailHandler = null;
let deleteSessionCalls = [];
let deleteSessionHandler = null;
// T5(Run footer): runs 列表 / Run 详情 / OTel 链接配置 replaceable handlers + 调用记录
let listSessionRunsCalls = [];
let listSessionRunsHandler = null;
let getSessionRunCalls = [];
let getSessionRunHandler = null;
let getOtelLinksCalls = [];
let getOtelLinksHandler = null;
// T6(Run 详情 tab): 工具调用 replaceable handler + 调用记录
let getSessionToolCallsCalls = [];
let getSessionToolCallsHandler = null;
const localStorageData = new Map();

function makeTimerEnv() {
  const timers = [];
  const setInterval = (fn, ms) => { const id = timers.length + 1; timers.push({ id, fn, ms }); return id; };
  const clearInterval = (id) => { const i = timers.findIndex((t) => t.id === id); if (i >= 0) timers.splice(i, 1); };
  const tickTimers = () => { for (const t of [...timers]) t.fn(); };
  const timerCount = () => timers.length;
  return { setInterval, clearInterval, tickTimers, timerCount };
}

function freshStubs(options) {
  options = options || {};
  // options.missingIds: 数组；其中的 id 对 ui.byId / document.getElementById 均返回 null
  const missingIds = new Set(options.missingIds || []);
  taskCalls.length = 0;
  fetchCalls = [];
  listItems = [];
  createdSessionId = null;
  appendCalls = [];
  sessionsList = [];
  boardColumns = [];
  scheduledTasks = [];
  sseQueue = [];
  fetchOverrides = [];
  streamKeepOpen = false;
  streamEndResolver = null;
  artifactFetchCalls = [];
  artifactFetchHandler = null;
  navPathCalls = [];
  listSkillsCalls = [];
  listSkillsHandler = null;
  createSessionCalls = [];
  createSessionHandler = null;
  getSessionDetailCalls = [];
  getSessionDetailHandler = null;
  deleteSessionCalls = [];
  deleteSessionHandler = null;
  listSessionRunsCalls = [];
  listSessionRunsHandler = null;
  getSessionRunCalls = [];
  getSessionRunHandler = null;
  getOtelLinksCalls = [];
  getOtelLinksHandler = null;
  getSessionToolCallsCalls = [];
  getSessionToolCallsHandler = null;
  const messageStack = makeEl();
  const input = makeEl();
  input.value = '';
  // chat-messages 是滚动容器；赋予可读写 scrollTop/scrollHeight/clientHeight 供滚动测试
  const messagesScroll = makeEl();
  messagesScroll.scrollTop = 0;
  messagesScroll.scrollHeight = 1000;
  messagesScroll.clientHeight = 800;
  const byIdMap = {
    'chat-message-stack': messageStack,
    'chat-input': input,
    'chat-messages': messagesScroll,
    'chat-session-list': makeEl(),
    'chat-header': makeEl(),
    'chat-summary': makeEl(),
    'chat-task-state': makeEl(),
    'chat-tool-calls': makeEl(),
    'chat-session-search-btn': makeEl(),
    'chat-session-filter-btn': makeEl(),
  };
  // T4: side panel structure (matching index.html)
  const chatShell = makeEl();
  chatShell.className = 'chat-shell chat-shell--side-collapsed';
  const chatSidePanel = makeEl();
  chatSidePanel.className = 'status-panel chat-side-panel';
  const chatSideToggleBtn = makeEl();
  chatSideToggleBtn.tagName = 'BUTTON';
  chatSideToggleBtn.setAttribute('aria-expanded', 'false');
  const chatSideBody = makeEl();
  chatSideBody.className = 'panel-body';
  const chatTabToolButton = makeEl();
  chatTabToolButton.tagName = 'BUTTON';
  chatTabToolButton.className = 'chat-tab chat-tab--active';
  chatTabToolButton.dataset.tab = 'tool';
  chatTabToolButton.setAttribute('role', 'tab');
  chatTabToolButton.setAttribute('aria-selected', 'true');
  chatTabToolButton.setAttribute('tabindex', '0');
  const chatTabArtifactButton = makeEl();
  chatTabArtifactButton.tagName = 'BUTTON';
  chatTabArtifactButton.className = 'chat-tab';
  chatTabArtifactButton.dataset.tab = 'artifact';
  chatTabArtifactButton.setAttribute('role', 'tab');
  chatTabArtifactButton.setAttribute('aria-selected', 'false');
  chatTabArtifactButton.setAttribute('tabindex', '-1');
  const chatTabTool = makeEl();
  chatTabTool.className = 'chat-tab-content';
  chatTabTool.setAttribute('role', 'tabpanel');
  chatTabTool.hidden = false;
  const chatTabArtifact = makeEl();
  chatTabArtifact.className = 'chat-tab-content';
  chatTabArtifact.setAttribute('role', 'tabpanel');
  chatTabArtifact.hidden = true;
  // T6: Run 详情 tab（匹配 index.html 静态模板第三 tab）
  const chatTabRunButton = makeEl();
  chatTabRunButton.tagName = 'BUTTON';
  chatTabRunButton.className = 'chat-tab';
  chatTabRunButton.dataset.tab = 'run';
  chatTabRunButton.setAttribute('role', 'tab');
  chatTabRunButton.setAttribute('aria-selected', 'false');
  chatTabRunButton.setAttribute('aria-controls', 'chat-tab-run');
  chatTabRunButton.setAttribute('tabindex', '-1');
  const chatTabRun = makeEl();
  chatTabRun.className = 'chat-tab-content';
  chatTabRun.setAttribute('role', 'tabpanel');
  chatTabRun.setAttribute('aria-labelledby', 'chat-tab-run-button');
  chatTabRun.hidden = true;
  const chatRunDetail = makeEl();
  chatRunDetail.setAttribute('aria-live', 'polite');
  chatRunDetail.textContent = '暂未选择 Run';
  const chatArtifactList = makeEl();
  chatArtifactList.className = 'artifacts-list__items';
  chatArtifactList.textContent = '暂未选择会话';
  byIdMap['chat-shell'] = chatShell;
  byIdMap['chat-side-panel'] = chatSidePanel;
  byIdMap['chat-side-toggle-btn'] = chatSideToggleBtn;
  byIdMap['chat-side-body'] = chatSideBody;
  byIdMap['chat-tab-tool-button'] = chatTabToolButton;
  byIdMap['chat-tab-artifact-button'] = chatTabArtifactButton;
  byIdMap['chat-tab-tool'] = chatTabTool;
  byIdMap['chat-tab-artifact'] = chatTabArtifact;
  byIdMap['chat-tab-run-button'] = chatTabRunButton;
  byIdMap['chat-tab-run'] = chatTabRun;
  byIdMap['chat-run-detail'] = chatRunDetail;
  byIdMap['chat-artifact-list'] = chatArtifactList;
  // T3: 左侧会话面板结构（匹配 index.html 静态模板）
  const hasId = (id) => !missingIds.has(id);
  // T5: chat-send + chat-composer__bar + chat-image-* 容器，使 chat.js init 装配块（chat.js:3523+）真实运行
  const chatSendBtn = makeEl();
  chatSendBtn.tagName = 'BUTTON';
  chatSendBtn.setAttribute('type', 'button');
  byIdMap['chat-send'] = chatSendBtn;
  const chatComposer = makeEl();
  chatComposer.id = 'chat-composer';
  chatComposer.className = 'chat-composer';
  const chatComposerBar = makeEl();
  chatComposerBar.className = 'chat-composer__bar';
  chatComposerBar.setAttribute('id', 'chat-composer-bar');
  chatComposer.appendChild(chatComposerBar);
  // sendBtn 必须挂在 composer bar 内部，保证 insertBefore(emContainer, sendBtn) 不会抛 NotFoundError
  chatComposerBar.appendChild(chatSendBtn);
  // chat-image-button / chat-image-input / chat-image-previews 容器
  const chatImageBtn = makeEl();
  chatImageBtn.tagName = 'BUTTON';
  const chatImageInput = makeEl();
  chatImageInput.tagName = 'INPUT';
  chatImageInput.setAttribute('type', 'file');
  byIdMap['chat-image-button'] = chatImageBtn;
  byIdMap['chat-image-input'] = chatImageInput;
  const chatImagePreviews = makeEl();
  byIdMap['chat-image-previews'] = chatImagePreviews;
  const sessionFilterBtn = byIdMap['chat-session-filter-btn'];
  const sessionSearchBtn = byIdMap['chat-session-search-btn'];
  const sessionHideBtn = makeEl();
  sessionHideBtn.tagName = 'BUTTON';
  sessionHideBtn.setAttribute('aria-expanded', 'true'); // 静态模板初始：左侧展开
  const sessionExpandBtn = makeEl();
  sessionExpandBtn.tagName = 'BUTTON';
  sessionExpandBtn.setAttribute('aria-expanded', 'false');
  sessionExpandBtn.hidden = true; // 静态模板初始：展开按钮隐藏
  const sessionPanel = makeEl();
  sessionPanel.className = 'status-panel chat-session-panel';
  const sessionHeader = makeEl();
  sessionHeader.className = 'panel-header chat-session-panel__header';
  const sessionActions = makeEl();
  sessionActions.className = 'chat-session-panel__actions';
  // 顺序：隐藏按钮在前，筛选按钮在后（同容器）
  if (hasId('chat-session-toggle-btn')) sessionActions.appendChild(sessionHideBtn);
  if (hasId('chat-session-search-btn')) sessionActions.appendChild(sessionSearchBtn);
  if (hasId('chat-session-filter-btn')) sessionActions.appendChild(sessionFilterBtn);
  sessionHeader.appendChild(sessionActions);
  if (hasId('chat-session-panel')) sessionPanel.appendChild(sessionHeader);
  // 对话区 header（bindSideToggle 移动右侧按钮的目标之一）
  const chatStack = makeEl();
  chatStack.className = 'chat-stack';
  const chatStackHeader = makeEl();
  chatStackHeader.className = 'panel-header';
  chatStack.appendChild(chatStackHeader);
  // T5: composer 挂到 chatStack，使 chat.js init 装配块中的 querySelector('.chat-composer__bar') 真实命中
  chatStack.appendChild(chatComposer);
  // 右侧面板 header（展开时的按钮目标）
  const sideHeader = makeEl();
  sideHeader.className = 'panel-header';
  chatSidePanel.appendChild(sideHeader);
  // 真实 DOM 顺序：session panel, expand btn, chat stack, right panel
  if (hasId('chat-shell')) {
    if (hasId('chat-session-panel')) chatShell.appendChild(sessionPanel);
    if (hasId('chat-session-expand-btn')) chatShell.appendChild(sessionExpandBtn);
    chatShell.appendChild(chatStack);
    chatShell.appendChild(chatSidePanel);
  }
  if (hasId('chat-session-panel')) byIdMap['chat-session-panel'] = sessionPanel;
  if (hasId('chat-session-toggle-btn')) byIdMap['chat-session-toggle-btn'] = sessionHideBtn;
  if (hasId('chat-session-expand-btn')) byIdMap['chat-session-expand-btn'] = sessionExpandBtn;
  const ui = {
    byId: (id) => {
      if (missingIds.has(id)) return null;
      return byIdMap[id] !== undefined ? byIdMap[id] : makeEl();
    },
    el: () => makeEl(),
    clear: (node) => { if (node && node._kids) { for (const k of node._kids) if (k) k.parentNode = null; node._kids.length = 0; } },
    renderEmpty: () => {},
    renderError: () => {},
  };
  const api = {
    task: taskApi(),
    createSession: (id) => {
      createSessionCalls.push({ id });
      createdSessionId = id;
      if (createSessionHandler) return createSessionHandler(id);
      return Promise.resolve({ id });
    },
    appendSessionMessage: (id, content) => {
      appendCalls.push({ id, content });
      return Promise.resolve({ id: 'msg_' + appendCalls.length, role: 'system', content });
    },
    listSessions: () => Promise.resolve(sessionsList.map((session) => (
      session.source ? session : Object.assign({}, session, { source: 'api' })
    ))),
    getAdminModels: () => Promise.resolve({}),
    getSessionDetail: (id) => {
      getSessionDetailCalls.push({ id });
      if (getSessionDetailHandler) return getSessionDetailHandler(id);
      return Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    },
    getSessionToolCalls: (id) => {
      getSessionToolCallsCalls.push({ id });
      if (getSessionToolCallsHandler) return getSessionToolCallsHandler(id);
      return Promise.resolve([]);
    },
    // T5(Run footer): runs 列表 / Run 详情 / OTel 链接配置；数组 DTO 直接消费（不假定 {items:...}）
    listSessionRuns: (id) => {
      listSessionRunsCalls.push({ id });
      if (listSessionRunsHandler) return listSessionRunsHandler(id);
      return Promise.resolve([]);
    },
    getSessionRun: (id, runId) => {
      getSessionRunCalls.push({ id, runId });
      if (getSessionRunHandler) return getSessionRunHandler(id, runId);
      return Promise.resolve(null);
    },
    getOtelLinks: () => {
      getOtelLinksCalls.push({});
      if (getOtelLinksHandler) return getOtelLinksHandler();
      return Promise.resolve({ config: {} });
    },
    renameSession: () => Promise.resolve({}),
    // T5: listSkills 不走全局 fetch；直接返回已解析的 {skills: [...]}；由 listSkillsHandler 替换用于倒序/失败场景
    listSkills: () => {
      listSkillsCalls.push({});
      if (listSkillsHandler) return listSkillsHandler();
      return Promise.resolve({ skills: [] });
    },
    deleteSession: (id) => {
      deleteSessionCalls.push({ id });
      if (deleteSessionHandler) return deleteSessionHandler(id);
      return Promise.resolve({});
    },
    listScheduledTasks: () => Promise.resolve(scheduledTasks.slice()),
  };
  const createdElements = [];
  function findById(id) {
    if (missingIds.has(id)) return null;
    if (byIdMap[id] !== undefined) return byIdMap[id];
    const visit = (node) => {
      if (!node) return null;
      if (node.id === id) return node;
      for (const child of node._kids || []) {
        const found = visit(child);
        if (found) return found;
      }
      return null;
    };
    return visit(document.body);
  }
  const document = {
    _elements: createdElements,
    _listeners: {},
    _captureListeners: {},
    activeElement: null,
    createElement: (tagName) => { const el = makeEl(); el.tagName = String(tagName || 'div').toUpperCase(); createdElements.push(el); return el; },
    createElementNS: (_namespace, tagName) => { const el = makeEl(); el.tagName = String(tagName || 'div').toUpperCase(); createdElements.push(el); return el; },
    createTextNode: (t) => ({ _text: String(t) }),
    createDocumentFragment: () => { const frag = makeEl(); frag.tagName = '#document-fragment'; return frag; },
    getElementById: findById,
    // T5: document.querySelector 真返回挂在 body 上的 .chat-composer__bar；其它 selector 兜底沿用 byIdMap/createdElements
    querySelector: (sel) => {
      const s = String(sel || '');
      const wantCls = (node, cls) => node && node.className && node.className.split(/\s+/).indexOf(cls) !== -1;
      if (s === '.chat-composer__bar' || s === '#chat-composer-bar') {
        if (byIdMap['chat-composer-bar']) return byIdMap['chat-composer-bar'];
        // 兜底扫描 body
        const visit = (node) => {
          if (!node) return null;
          if (s === '#chat-composer-bar' && node._attrs && node._attrs.id === 'chat-composer-bar') return node;
          if (s === '.chat-composer__bar' && wantCls(node, 'chat-composer__bar')) return node;
          for (const child of node._kids || []) { const f = visit(child); if (f) return f; }
          return null;
        };
        return visit(document.body);
      }
      return null;
    },
    querySelectorAll: (sel) => {
      const s = String(sel || '');
      // 优先用 createdElements + byIdMap 的扁平集合（生产代码的 list/querySelectorAll
      // 依赖的若干 byIdMap 节点并未挂到 body 上；不能用纯 DOM 树遍历，否则漏报）。
      // 同时过滤掉 replaceChildren/replaceChild/removeChild 后已脱离的旧节点（_dead）。
      const all = createdElements.concat(Object.keys(byIdMap).map((k) => byIdMap[k]));
      const seen = new Set();
      const live = [];
      for (const el of all) {
        if (!el || seen.has(el) || el._dead) continue;
        seen.add(el);
        live.push(el);
      }
      // 支持属性选择器：[role="dialog"] / [role='dialog']
      const attrMatch = s.match(/^\[([a-zA-Z-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?\]$/);
      if (attrMatch) {
        const attr = attrMatch[1];
        const val = attrMatch[2] != null ? attrMatch[2] : (attrMatch[3] != null ? attrMatch[3] : null);
        const out = [];
        for (const el of live) {
          const has = el.hasAttribute ? el.hasAttribute(attr) : !!(el._attrs && Object.prototype.hasOwnProperty.call(el._attrs, attr));
          if (!has) continue;
          if (val != null) {
            const got = el.getAttribute ? el.getAttribute(attr) : (el._attrs ? el._attrs[attr] : null);
            if (got !== val) continue;
          }
          out.push(el);
        }
        return out;
      }
      const cls = s.replace(/^\./, '');
      const out = [];
      for (const el of live) {
        if (el.className && el.className.split(/\s+/).indexOf(cls) !== -1) out.push(el);
      }
      return out;
    },
    addEventListener: (type, fn, capture) => {
      if (capture) {
        (document._captureListeners[type] = document._captureListeners[type] || []).push(fn);
      } else {
        (document._listeners[type] = document._listeners[type] || []).push(fn);
      }
    },
    removeEventListener: (type, fn, capture) => {
      const bucket = capture ? document._captureListeners : document._listeners;
      bucket[type] = (bucket[type] || []).filter((listener) => listener !== fn);
    },
    hidden: false,
    visibilityState: 'visible',
    body: makeEl(),
  };
  // 让 makeEl 内的 addEventListener 也能挂 capture 监听（target.closest 链路上的 capture 阶段）
  // （仅在 document 拼装后注入；makeEl 在被创建时已存在的 _listeners 不变）
  _currentDoc = document;
  document.body.appendChild(chatShell);
  const fetchStub = (url, opts) => {
    const u = String(url);
    // T4: artifact list requests are recorded separately (not in fetchCalls)
    if (u.indexOf('/chat/artifacts') !== -1) {
      artifactFetchCalls.push({ url, opts });
      if (artifactFetchHandler) return artifactFetchHandler(url, opts);
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ items: [], next_cursor: null }),
      });
    }
    fetchCalls.push({ url, opts });
    for (const ov of fetchOverrides) {
      const u = String(url);
      const m = typeof ov.match === 'string' ? u.indexOf(ov.match) !== -1 : ov.match.test(u);
      if (m) return ov.handler(url, opts);
    }
    const chunks = sseQueue.slice();
    sseQueue = [];
    const keepOpen = streamKeepOpen;
    streamKeepOpen = false;
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({}),
      body: { getReader: () => {
        let i = 0;
        return { read: async () => {
          if (i < chunks.length) return { done: false, value: new TextEncoder().encode(chunks[i++]) };
          if (keepOpen) await new Promise((res) => { streamEndResolver = res; });
          return { done: true };
        } };
      } },
    });
  };
  const win = {
    NAGENT: {
      api, ui,
      modal: { confirm: () => Promise.resolve(true), alert: () => {} },
      navigation: { navigatePath: (path) => { navPathCalls.push(path); } },
      artifacts: {
        renderListItem: (artifact, onClick) => {
          const item = makeEl();
          item.className = 'artifacts-list__item';
          item._artifact = artifact;
          item.textContent = artifact.name || artifact.id;
          if (typeof onClick === 'function') {
            item.addEventListener('click', () => onClick(artifact));
          }
          return item;
        },
      },
    },
    _listeners: {},
    document,
    location: { protocol: 'http:', host: 'x', href: '', pathname: '/chat' },
    crypto: { randomUUID: () => 'stub-uuid' },
    fetch: fetchStub,
    localStorage: {
      getItem: (key) => (localStorageData.has(key) ? localStorageData.get(key) : null),
      setItem: (key, value) => { localStorageData.set(key, String(value)); },
      removeItem: (key) => { localStorageData.delete(key); },
    },
    WebSocket: function () {},
    TextDecoder: function () { this.decode = () => ''; },
    addEventListener: (type, fn) => { (win._listeners[type] = win._listeners[type] || []).push(fn); },
  };
  const timerEnv = makeTimerEnv();
  return { win, messageStack, input, messagesScroll, byIdMap, document, timerEnv,
    enqueueSseChunks: (chunks) => { sseQueue.push(...chunks); },
    setFetchHandler: (match, handler) => { fetchOverrides.push({ match, handler }); },
    clearFetchHandlers: () => { fetchOverrides.length = 0; },
    setStreamKeepOpen: () => { streamKeepOpen = true; },
    endStream: () => { if (streamEndResolver) { const r = streamEndResolver; streamEndResolver = null; r(); } },
    // T4: artifact panel helpers
    setArtifactFetchHandler: (handler) => { artifactFetchHandler = handler; },
    clearArtifactFetchHandler: () => { artifactFetchHandler = null; },
    getArtifactFetchCalls: () => artifactFetchCalls.slice(),
    clearArtifactFetchCalls: () => { artifactFetchCalls.length = 0; },
    getNavPathCalls: () => navPathCalls.slice(),
    clearNavPathCalls: () => { navPathCalls.length = 0; },
    fireKeydown: (el, key) => {
      let prevented = false;
      if (el) el.dispatchEvent({ type: 'keydown', key, preventDefault: () => { prevented = true; } });
      return { prevented };
    },
    fireDocumentKeydown: (key) => {
      (document._listeners.keydown || []).slice().forEach((fn) => fn({ type: 'keydown', key }));
    },
    clearLocalStorage: () => { localStorageData.clear(); },
    // T5: localStorage 显式 helper（测试主动预置/读取；不要隐式 clear）
    setLocalStorage: (key, value) => { localStorageData.set(key, String(value)); },
    getLocalStorage: (key) => (localStorageData.has(key) ? localStorageData.get(key) : null),
    // T5: listSkills 替换 handler / 调用记录
    setListSkillsHandler: (handler) => { listSkillsHandler = handler; },
    getListSkillsCalls: () => listSkillsCalls.slice(),
    clearListSkillsCalls: () => { listSkillsCalls.length = 0; },
    setCreateSessionHandler: (handler) => { createSessionHandler = handler; },
    getCreateSessionCalls: () => createSessionCalls.slice(),
    setGetSessionDetailHandler: (handler) => { getSessionDetailHandler = handler; },
    getGetSessionDetailCalls: () => getSessionDetailCalls.slice(),
    setDeleteSessionHandler: (handler) => { deleteSessionHandler = handler; },
    getDeleteSessionCalls: () => deleteSessionCalls.slice(),
    // T5(Run footer): handlers / 调用记录
    setListSessionRunsHandler: (handler) => { listSessionRunsHandler = handler; },
    getListSessionRunsCalls: () => listSessionRunsCalls.slice(),
    setGetSessionRunHandler: (handler) => { getSessionRunHandler = handler; },
    getGetSessionRunCalls: () => getSessionRunCalls.slice(),
    setGetOtelLinksHandler: (handler) => { getOtelLinksHandler = handler; },
    getGetOtelLinksCalls: () => getOtelLinksCalls.slice(),
    setGetSessionToolCallsHandler: (handler) => { getSessionToolCallsHandler = handler; },
    getGetSessionToolCallsCalls: () => getSessionToolCallsCalls.slice(),
    // T5: capture/target/bubble click 派发（document + 祖先 + 目标 + 真实 stopPropagation/preventDefault）
    fireClick: (el) => {
      if (!el) return null;
      return dispatchClick(el);
    },
  };
}

// Load chat.js into a fresh context. Calls init() so visibilitychange/beforeunload
// listeners are bound; exposes timer/visibility/session-item helpers for auto-refresh tests.
function loadChat(options) {
  const env = freshStubs(options);
  const ctx = {
    NAGENT: env.win.NAGENT,
    document: env.win.document,
    window: env.win,
    console,
    fetch: env.win.fetch,
    crypto: env.win.crypto,
    WebSocket: env.win.WebSocket,
    TextDecoder,
    TextEncoder,
    URLSearchParams,
    URL,
    encodeURIComponent,
    setInterval: env.timerEnv.setInterval,
    clearInterval: env.timerEnv.clearInterval,
    // T5: VM-top-level localStorage（与 window.localStorage 共享同一份 storage）
    localStorage: env.win.localStorage,
  };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  env.chat = ctx.NAGENT.chat;
  // Note: init() is NOT called here. Tests that need visibilitychange/beforeunload
  // listeners or a populated session list call env.chat.init() after setting
  // sessionsList, so init's loadSessions builds the expected session items.
  env.tickTimers = env.timerEnv.tickTimers;
  env.timerCount = env.timerEnv.timerCount;
  env.setHidden = (h) => {
    env.win.document.hidden = !!h;
    env.win.document.visibilityState = h ? 'hidden' : 'visible';
    (env.win.document._listeners.visibilitychange || []).forEach((fn) => fn({ type: 'visibilitychange' }));
  };
  env.fireBeforeUnload = () => {
    (env.win._listeners.beforeunload || []).forEach((fn) => fn({ type: 'beforeunload' }));
  };
  env.findSessionItem = (sessionId) => {
    const items = env.win.document.querySelectorAll('.session-item');
    for (const item of items) {
      const titleBtn = item._kids && item._kids[0];
      if (titleBtn && (titleBtn.textContent === sessionId || titleBtn.textContent === sessionId)) return titleBtn;
    }
    return null;
  };
  env.fireClick = (el) => { if (el) el.click(); };
  env.waitMicro = () => new Promise((res) => setTimeout(res, 0));
  return env;
}

function _lastMessageBubble(env) {
  const messages = env.messageStack.querySelectorAll('.msg');
  return messages.length ? messages[messages.length - 1] : null;
}

function lastSystemText(env) {
  const el = _lastMessageBubble(env);
  return el ? el.textContent : null;
}

// Extract summary / body of the last system message (details > [summary, pre]).
function _lastSystemDetails(env) {
  const el = _lastMessageBubble(env);
  return (el && el._kids && el._kids[0]) ? el._kids[0] : null;
}
function lastSystemSummary(env) {
  const d = _lastSystemDetails(env);
  const s = d && d._kids && d._kids[0];
  return s ? s.textContent : null;
}
function lastSystemBody(env) {
  const d = _lastSystemDetails(env);
  const p = d && d._kids && d._kids[1];
  return p ? p.textContent : null;
}

// ===========================================================================
// 增量消息刷新：旧 details 节点必须保留展开状态，新增/合并的尾部消息只更新对应节点。
// ===========================================================================
async function testPartialMessageRefresh() {
  const env = loadChat();
  const first = { messages: [{ id: 'tool-1', role: 'tool', content: '{"step":1}' }] };
  await env.chat.applySessionDetail(first);
  const oldTool = env.messageStack._kids[0];
  oldTool.querySelector('details').open = true;

  await env.chat.applySessionDetail({
    messages: [
      { id: 'tool-1', role: 'tool', content: '{"step":1}' },
      { id: 'tool-2', role: 'tool', content: '{"step":2}' },
    ],
  }, { partialMessages: true });
  const refreshedTool = env.messageStack._kids[0];
  ok(refreshedTool !== oldTool, 'merged tool group updates only its changed node');
  ok(refreshedTool.querySelector('details').open === true, 'expanded tool call remains expanded after partial refresh');

  const taskEnv = loadChat();
  await taskEnv.chat.applySessionDetail({
    messages: [{ id: 'task-1', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 等待执行' }],
  });
  const oldTask = taskEnv.messageStack._kids[0];
  oldTask.querySelector('details').open = true;
  await taskEnv.chat.applySessionDetail({
    messages: [
      { id: 'task-1', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 等待执行' },
      { id: 'task-2', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 已完成' },
    ],
  }, { partialMessages: true });
  const refreshedTask = taskEnv.messageStack._kids[0];
  ok(refreshedTask !== oldTask, 'merged task status updates only its changed node');
  ok(refreshedTask.querySelector('details').open === true, 'expanded task status remains expanded after partial refresh');
}

// ===========================================================================
// T1: parseTaskCommand (pure)
// ===========================================================================
const env0 = loadChat();
const parse = env0.chat.parseTaskCommand;
ok(typeof parse === 'function', 'parseTaskCommand exposed');

let r;
r = parse('/task create 完成报告 --body 生成 Q3 总结 --priority 2 --goal');
ok(r.subcommand === 'create' && r.title === '完成报告' && r.body === '生成 Q3 总结' && r.priority === 2 && r.goal === true, 'create with named args (got ' + JSON.stringify(r) + ')');

r = parse('/task create 修复登录问题');
ok(r.subcommand === 'create' && r.title === '修复登录问题', 'create multi-word title (got ' + JSON.stringify(r) + ')');

r = parse('/task create "完成 季度 报告"');
ok(r.subcommand === 'create' && r.title === '完成 季度 报告', 'create double-quoted title (got ' + JSON.stringify(r) + ')');

r = parse("/task create '单引号 标题'");
ok(r.subcommand === 'create' && r.title === '单引号 标题', 'create single-quoted title (got ' + JSON.stringify(r) + ')');

r = parse('/task create 报告 --body "含 -- 破折号 的正文"');
ok(r.subcommand === 'create' && r.title === '报告' && r.body === '含 -- 破折号 的正文', 'create quoted body protects -- (got ' + JSON.stringify(r) + ')');

r = parse('/task create');
ok(!!r.error && r.error.indexOf('标题') !== -1, 'create missing title errors (got ' + JSON.stringify(r) + ')');

r = parse('/task create foo --body');
ok(!!r.error && r.error.indexOf('需要值') !== -1, 'create --body missing value errors (got ' + JSON.stringify(r) + ')');

r = parse('/task create foo --priority abc');
ok(!!r.error && r.error.indexOf('整数') !== -1, 'create --priority non-integer errors (got ' + JSON.stringify(r) + ')');

r = parse('/task create foo --unknown x');
ok(!!r.error && r.error.indexOf('未知参数') !== -1, 'create --unknown errors (got ' + JSON.stringify(r) + ')');

r = parse('/task cancel t_1 --note x');
ok(!!r.error && r.error.indexOf('不适用') !== -1, 'cancel rejects --note (got ' + JSON.stringify(r) + ')');

r = parse('/task create "未闭合');
ok(!!r.error && r.error.indexOf('未闭合') !== -1, 'unclosed quote errors (got ' + JSON.stringify(r) + ')');

r = parse('/task list');
ok(r.subcommand === 'list', 'list parsed (got ' + JSON.stringify(r) + ')');

r = parse('/task list extra');
ok(!!r.error && r.error.indexOf('不接受额外参数') !== -1, 'list rejects extra positional (got ' + JSON.stringify(r) + ')');

r = parse('/task approve t_123 --note 同意');
ok(r.subcommand === 'approve' && r.id === 't_123' && r.note === '同意', 'approve with note (got ' + JSON.stringify(r) + ')');

r = parse('/task reject t_456 --note 风险');
ok(r.subcommand === 'reject' && r.id === 't_456' && r.note === '风险', 'reject with note (got ' + JSON.stringify(r) + ')');

r = parse('/task cancel t_789');
ok(r.subcommand === 'cancel' && r.id === 't_789', 'cancel parsed (got ' + JSON.stringify(r) + ')');

r = parse('/task retry t_000');
ok(r.subcommand === 'retry' && r.id === 't_000', 'retry parsed (got ' + JSON.stringify(r) + ')');

r = parse('/task approve t_1 t_2');
ok(!!r.error && r.error.indexOf('不接受多个 id') !== -1, 'approve rejects second id (got ' + JSON.stringify(r) + ')');

r = parse('/task frobnicate x');
ok(!!r.error && r.error.indexOf('未知子命令') !== -1, 'unknown subcommand errors (got ' + JSON.stringify(r) + ')');

r = parse('/task approve');
ok(!!r.error && r.error.indexOf('task id') !== -1, 'approve missing id errors (got ' + JSON.stringify(r) + ')');

r = parse('/task');
ok(!!r.error && r.error.indexOf('用法') !== -1, 'bare /task shows usage (got ' + JSON.stringify(r) + ')');

// ===========================================================================
// T2/T3: runTaskCommand via send() - dispatch, prefix, error map, routing
// ===========================================================================
async function runIntegration() {
  // create: binds to currentSessionId, 任务指令 system message, fetch not called
  let env = loadChat();
  env.input.value = '/task create 报告 --body 正文 --priority 2 --goal';
  await env.chat.send();
  const createCall = taskCalls.find((c) => c.name === 'create');
  ok(!!createCall, 'create called api.task.create');
  ok(createCall && createCall.payload.title === '报告' && createCall.payload.body === '正文' && createCall.payload.priority === 2 && createCall.payload.goal_mode === true, 'create payload maps body/priority/goal_mode (got ' + JSON.stringify(createCall && createCall.payload) + ')');
  ok(createCall && createdSessionId && createCall.payload.origin_session_id === createdSessionId, 'create binds origin_session_id to current session (got ' + JSON.stringify(createCall && createCall.payload.origin_session_id) + ', session=' + createdSessionId + ')');
  ok(fetchCalls.length === 0, 'create does not call /chat/completions');
  let msg = lastSystemText(env);
  ok(typeof msg === 'string' && msg.indexOf('执行命令') !== -1 && msg.indexOf('已创建任务 t_1') !== -1 && msg.indexOf('[任务指令]') === -1, 'create system message has command record + task id, no header (got ' + JSON.stringify(msg) + ')');
  ok(env.input.value === '', 'create cleared input');

  // list: filters by origin_session_id, does not leak other sessions
  env = loadChat();
  env.input.value = '/task create 本会话任务';
  await env.chat.send();
  const sid = createdSessionId;
  listItems = [
    { id: 't_a', status: 'todo', title: '本会话任务', origin_session_id: sid },
    { id: 't_b', status: 'todo', title: '其它会话任务', origin_session_id: 'other-session' },
  ];
  env.input.value = '/task list';
  await env.chat.send();
  const listMsg = lastSystemText(env);
  ok(typeof listMsg === 'string' && listMsg.indexOf('本会话任务') !== -1 && listMsg.indexOf('其它会话任务') === -1, 'list filters by current session, no leak (got ' + JSON.stringify(listMsg) + ')');
  ok(fetchCalls.length === 0, 'list does not call /chat/completions');

  // empty list
  env = loadChat();
  env.input.value = '/task list';
  await env.chat.send();
  ok(lastSystemText(env).indexOf('当前会话无关联任务') !== -1, 'list empty shows 无关联任务 (got ' + JSON.stringify(lastSystemText(env)) + ')');

  // approve/reject pass id + note; cancel/retry pass id only
  env = loadChat();
  env.input.value = '/task approve t_7 --note 同意';
  await env.chat.send();
  let c = taskCalls.find((x) => x.name === 'approve');
  ok(c && c.id === 't_7' && c.note === '同意', 'approve passes id+note (got ' + JSON.stringify(c) + ')');
  ok(lastSystemText(env).indexOf('已批准任务 t_7') !== -1 && lastSystemText(env).indexOf('[任务指令]') === -1, 'approve message, no header (got ' + JSON.stringify(lastSystemText(env)) + ')');

  env = loadChat();
  env.input.value = '/task reject t_8 --note 风险';
  await env.chat.send();
  c = taskCalls.find((x) => x.name === 'reject');
  ok(c && c.id === 't_8' && c.note === '风险', 'reject passes id+note (got ' + JSON.stringify(c) + ')');

  env = loadChat();
  env.input.value = '/task cancel t_9';
  await env.chat.send();
  c = taskCalls.find((x) => x.name === 'cancel');
  ok(c && c.id === 't_9' && c.note === undefined, 'cancel passes id only (got ' + JSON.stringify(c) + ')');

  env = loadChat();
  env.input.value = '/task retry t_10';
  await env.chat.send();
  c = taskCalls.find((x) => x.name === 'retry');
  ok(c && c.id === 't_10' && c.note === undefined, 'retry passes id only (got ' + JSON.stringify(c) + ')');

  // error code mapping: api rejects with Error('task_not_found')
  env = loadChat();
  env.win.NAGENT.api.task.approve = () => Promise.reject(new Error('task_not_found'));
  env.input.value = '/task approve bad-id';
  await env.chat.send();
  const errMsg = lastSystemText(env);
  ok(errMsg.indexOf('任务不存在') !== -1 && errMsg.indexOf('task_not_found') !== -1 && errMsg.indexOf('[任务指令]') === -1, 'error maps task_not_found with code, no header (got ' + JSON.stringify(errMsg) + ')');
  ok(fetchCalls.length === 0, 'error path does not call /chat/completions');

  // unknown error: safe generic message includes String(error)
  env = loadChat();
  env.win.NAGENT.api.task.cancel = () => Promise.reject(new Error('something_unexpected'));
  env.input.value = '/task cancel t_x';
  await env.chat.send();
  ok(lastSystemText(env).indexOf('something_unexpected') !== -1, 'unknown error includes raw code (got ' + JSON.stringify(lastSystemText(env)) + ')');

  // parser error: no api call, no fetch, 任务指令 usage message
  env = loadChat();
  env.input.value = '/task frobnicate x';
  await env.chat.send();
  ok(taskCalls.length === 0 && fetchCalls.length === 0, 'parser error calls no api and no fetch');
  ok(lastSystemText(env).indexOf('未知子命令') !== -1 && lastSystemText(env).indexOf('[任务指令]') === -1, 'parser error renders usage, no header (got ' + JSON.stringify(lastSystemText(env)) + ')');

  // safe rendering: a malicious title must not produce an IMG/innerHTML injection
  env = loadChat();
  env.win.NAGENT.api.task.create = (p) => { taskCalls.push({ name: 'create', payload: p }); return Promise.resolve({ id: 't_z', title: '<img src=x onerror=alert(1)>' }); };
  env.input.value = '/task create 标题';
  await env.chat.send();
  const lastKid = env.messageStack._kids[env.messageStack._kids.length - 1];
  const hasImg = env.messageStack.querySelectorAll('img').length > 0;
  ok(!hasImg, 'malicious title renders no IMG node (textContent only)');
  ok(lastKid && lastKid.textContent.indexOf('<img src=x onerror=alert(1)>') !== -1, 'malicious title preserved as text (got ' + JSON.stringify(lastKid && lastKid.textContent) + ')');

  // non-command routing: plain text calls /chat/completions, no task api
  env = loadChat();
  env.input.value = '帮我写一段总结';
  await env.chat.send();
  ok(fetchCalls.some((f) => f.url === '/chat/completions'), 'non-command calls /chat/completions');
  ok(taskCalls.every((c) => c.name === 'board'), 'non-command calls no task mutation api (board read ok)');

  // === persistence: /task command record + result persist to session ===
  env = loadChat();
  env.input.value = '/task create 报告';
  await env.chat.send();
  ok(appendCalls.length >= 2, 'create persisted command record + result (got ' + appendCalls.length + ')');
  ok(appendCalls.length > 0 && appendCalls[0].id === createdSessionId, 'persist bound to current session (got ' + appendCalls[0].id + ', session=' + createdSessionId + ')');
  ok(appendCalls.length > 0 && appendCalls[0].content.indexOf('执行命令') !== -1, 'command record persisted first (got ' + JSON.stringify(appendCalls[0].content) + ')');
  ok(lastSystemSummary(env) === '任务指令', 'system summary is 任务指令 by name (got ' + JSON.stringify(lastSystemSummary(env)) + ')');
  // 合并：命令记录 + 回执渲染为同一条任务指令气泡（非两条）
  const taskCmdBubbles = env.messageStack.querySelectorAll('.msg').filter(k => k && k.dataset && k.dataset.name === 'ui.task_command');
  ok(taskCmdBubbles.length === 1, 'command record + result merged into 1 task-command bubble (got ' + taskCmdBubbles.length + ')');

  // === truncation: oversize content truncated with suffix; DOM body contains POST body ===
  env = loadChat();
  const big = '中'.repeat(22000);  // ~66000 bytes > 65536
  env.input.value = '/task create ' + big;
  await env.chat.send();
  const lastPersist = appendCalls[appendCalls.length - 1];
  ok(!!lastPersist && lastPersist.content.indexOf('…[内容已截断]') !== -1, 'oversize persisted body truncated with suffix (got tail=' + JSON.stringify((lastPersist && lastPersist.content || '').slice(-30)) + ')');
  ok(!!lastPersist && new TextEncoder().encode(lastPersist.content).length <= 65536, 'truncated body within byte limit (got ' + (lastPersist && new TextEncoder().encode(lastPersist.content).length) + ')');
  ok(lastSystemBody(env).indexOf(lastPersist && lastPersist.content) !== -1, 'DOM body contains POST body (merged block) (got dom tail=' + JSON.stringify((lastSystemBody(env) || '').slice(-30)) + ')');

  // === persistence failure does not block task api; local message still shown ===
  env = loadChat();
  env.win.NAGENT.api.appendSessionMessage = () => Promise.reject(new Error('network'));
  env.input.value = '/task create 报告';
  await env.chat.send();
  ok(taskCalls.some((c) => c.name === 'create'), 'task api still called when persist fails');
  ok(lastSystemText(env) && lastSystemText(env).indexOf('已创建任务') !== -1 && lastSystemText(env).indexOf('[任务指令]') === -1, 'local system message still shown when persist fails, no header');

  // ===========================================================================
  // T4: auto-refresh controller (start/stop, version, single-flight, attribution,
  //     scroll, lifecycle, /task version coordination)
  // ===========================================================================
  // Note: sessionsList must be set AFTER loadChat (freshStubs resets it) but
  // BEFORE env.chat.init() so init's loadSessions builds session items.

  function detailWith(id, msgs) {
    return { session: { id }, messages: msgs, summary: null, task_state: null };
  }

  // --- start/stop + version same skip / different render ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  let s1msgs = [{ id: 'm1', role: 'user', content: 'hi' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve(detailWith(id, s1msgs));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  ok(env.timerCount() === 1, 'auto-refresh timer started after select (got ' + env.timerCount() + ')');
  const stackCountAfterSelect = env.messageStack._kids.length;
  env.tickTimers();
  await env.waitMicro();
  ok(env.messageStack._kids.length === stackCountAfterSelect, 'same version does not re-render (got ' + env.messageStack._kids.length + ')');
  s1msgs = [{ id: 'm1', role: 'user', content: 'hi' }, { id: 'm2', role: 'assistant', content: 'reply' }];
  env.tickTimers();
  await env.waitMicro();
  ok(env.messageStack._kids.length > stackCountAfterSelect, 'new version re-renders (got ' + env.messageStack._kids.length + ')');

  // --- single-flight: in-flight request blocks second tick ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  let resolvePending = null;
  let sfCalls = 0;
  env.win.NAGENT.api.getSessionDetail = () => {
    sfCalls++;
    if (sfCalls === 1) return Promise.resolve(detailWith('s1', [{ id: 'm1', role: 'user', content: 'hi' }]));
    return new Promise((res) => { resolvePending = res; });
  };
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro(); // selectSession completes (call 1), startAutoRefresh
  env.tickTimers(); // call 2: in-flight (pending)
  await env.waitMicro();
  env.tickTimers(); // single-flight: skip
  await env.waitMicro();
  ok(sfCalls === 2, 'single-flight: second tick does not start new request (got sfCalls=' + sfCalls + ')');
  if (resolvePending) resolvePending(detailWith('s1', [{ id: 'm1', role: 'user', content: 'hi' }]));
  await env.waitMicro();

  // --- visibility hidden/visible ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  let visCalls = 0;
  const baseDetail = () => Promise.resolve(detailWith('s1', [{ id: 'm1', role: 'user', content: 'hi' }]));
  env.win.NAGENT.api.getSessionDetail = () => { visCalls++; return baseDetail(); };
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  const visCallsAfterSelect = visCalls;
  env.setHidden(true);
  ok(env.timerCount() === 0, 'hidden stops timer (got ' + env.timerCount() + ')');
  env.setHidden(false);
  await env.waitMicro();
  ok(env.timerCount() === 1, 'visible restarts timer (got ' + env.timerCount() + ')');
  ok(visCalls > visCallsAfterSelect, 'visible triggers immediate catch-up (got visCalls=' + visCalls + ', afterSelect=' + visCallsAfterSelect + ')');

  // --- beforeunload stops timer ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  env.win.NAGENT.api.getSessionDetail = () => baseDetail();
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  ok(env.timerCount() === 1, 'timer active before unload (got ' + env.timerCount() + ')');
  env.fireBeforeUnload();
  ok(env.timerCount() === 0, 'beforeunload stops timer (got ' + env.timerCount() + ')');

  // --- session_not_found stops + clears; network error keeps timer ---
  env = loadChat();
  sessionsList = [];
  env.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('session_not_found'));
  env.input.value = 'hello';
  await env.chat.send(); // ensureSession -> startAutoRefresh; refreshCurrentSession -> session_not_found (caught, no stop)
  await env.waitMicro();
  env.tickTimers(); // autoRefreshTick -> session_not_found -> stopAutoRefresh
  await env.waitMicro();
  ok(env.timerCount() === 0, 'session_not_found stops timer (got ' + env.timerCount() + ')');

  env = loadChat();
  sessionsList = [];
  env.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network_failed'));
  env.input.value = 'hello';
  await env.chat.send();
  await env.waitMicro();
  env.tickTimers();
  await env.waitMicro();
  ok(env.timerCount() === 1, 'network error keeps timer (got ' + env.timerCount() + ')');

  // --- /task version coordination: persist returns real id, next poll no spurious re-render ---
  // /task list does 2 taskSystemMessage (command record + result), each persists 1 system msg.
  // After: renderedMessageVersion = {count: 1+2=3, lastId: id of 2nd persist}.
  env = loadChat();
  sessionsList = [];
  let coordMsgs = [{ id: 'm1', role: 'user', content: 'hi' }];
  env.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', coordMsgs));
  env.input.value = 'hello';
  await env.chat.send(); // ensureSession; refreshCurrentSession sets version {count:1,lastId:'m1'}
  await env.waitMicro();
  // appendSessionMessage returns distinct ids; /task list persists 2 messages
  const coordAppendBase = appendCalls.length;
  env.input.value = '/task list';
  await env.chat.send();
  await env.waitMicro();
  const lastPersistedId = 'msg_' + appendCalls.length; // 2nd persist id
  // Next poll returns detail with [m1, sys1, sys2] matching version {count:3, lastId:lastPersistedId}
  coordMsgs = [
    { id: 'm1', role: 'user', content: 'hi' },
    { id: 'msg_' + (coordAppendBase + 1), role: 'system', content: '执行命令: /task list' },
    { id: lastPersistedId, role: 'system', content: '当前会话无关联任务' },
  ];
  const beforePoll = env.messageStack._kids.length;
  env.tickTimers();
  await env.waitMicro();
  ok(env.messageStack._kids.length === beforePoll, '/task coordination: next poll no spurious re-render (got ' + env.messageStack._kids.length + ', before=' + beforePoll + ')');

  // A refresh can race the local optimistic append before its version advance.
  // The server returns the same task-command group keyed by the first persisted
  // message; it must replace the local card, not append a duplicate card.
  env = loadChat();
  sessionsList = [];
  let raceMsgs = [{ id: 'm1', role: 'user', content: 'hi' }];
  env.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', raceMsgs));
  env.input.value = 'hello';
  await env.chat.send();
  await env.waitMicro();
  const raceAppendBase = appendCalls.length;
  env.input.value = '/task list';
  await env.chat.send();
  await env.waitMicro();
  raceMsgs = [
    { id: 'm1', role: 'user', content: 'hi' },
    { id: 'msg_' + (raceAppendBase + 1), role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    { id: 'msg_' + (raceAppendBase + 2), role: 'system', name: 'ui.task_command', content: '当前会话无关联任务' },
  ];
  await env.chat.applySessionDetail(detailWith('s1', raceMsgs), { partialMessages: true });
  const raceTaskCards = env.messageStack.querySelectorAll('.msg').filter((k) => k && k.dataset && k.dataset.name === 'ui.task_command');
  ok(raceTaskCards.length === 1, 'task command refresh race replaces optimistic card instead of duplicating it (got ' + raceTaskCards.length + ')');

  // ===========================================================================
  // T5: 任务状态合并卡片默认折叠；等待批准交互卡片（带 card payload）默认展开
  //     合并卡片（ui.task_lifecycle 无 card 相邻合并）默认 open=false；
  //     ui.task_command 独立不参与合并（断开合并链）；交互卡片（带 card）默认展开（T6/T7 覆盖）。
  // ===========================================================================
  function findSystemDetailsByContent(stack, needle) {
    const kids = stack._kids || [];
    for (const k of kids) {
      if (!k.textContent || k.textContent.indexOf(needle) === -1) continue;
      const details = k.querySelector('details');
      if (details && 'open' in details) return details;
    }
    return null;
  }

  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  const lifecycleMsgs = [
    { id: 'm1', role: 'user', content: '帮我调研 X' },
    { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 开始运行: t_1 - 调研X' },
    { id: 'm3', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 等待批准: t_1 - 调研X | 提案: 修改方案A' },
    { id: 'm4', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 已完成: t_1 - 调研X | 已交付' },
    { id: 'm5', role: 'system', name: 'ui.task_command', content: '[任务指令] 执行命令: /task list' },
  ];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve(detailWith(id, lifecycleMsgs));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();

  // 相邻的 ui.task_lifecycle 无 card（m2-m4）合并为一个卡片；ui.task_command（m5）独立不参与合并
  const mergedDetails = findSystemDetailsByContent(env.messageStack, '等待批准');
  ok(!!mergedDetails, 'merged task card rendered (contains 等待批准)');
  // 合并卡片默认折叠（open=false），即使 content 含 等待批准 文本（仅带 card payload 的交互卡片才默认展开）
  ok(mergedDetails && mergedDetails.open === false, 'merged task card folded by default (open=false) (got open=' + (mergedDetails && mergedDetails.open) + ')');
  // 合并卡片 summary 固定 = 任务状态
  const mergedSummary = mergedDetails && mergedDetails._kids && mergedDetails._kids[0];
  ok(mergedSummary && mergedSummary.textContent === '任务状态', 'merged card summary = 任务状态 (got ' + JSON.stringify(mergedSummary && mergedSummary.textContent) + ')');
  // 历史消息（带 [任务状态] 抬头）渲染时剥离行首抬头，正文保留
  function detailsPreText(details) {
    if (!details || !details._kids) return '';
    const pre = details._kids[1];
    return pre ? (pre.textContent || '') : '';
  }
  // 合并卡片含 m2-m4 全部 lifecycle 内容（不含 m5 执行命令，m5 独立）
  const mergedText = detailsPreText(mergedDetails);
  ok(mergedText.indexOf('开始运行') !== -1 && mergedText.indexOf('等待批准') !== -1 && mergedText.indexOf('已完成') !== -1, 'merged card contains m2-m4 lifecycle content (got ' + JSON.stringify(mergedText) + ')');
  ok(mergedText.indexOf('执行命令') === -1, 'merged card does NOT contain m5 task_command (independent) (got ' + JSON.stringify(mergedText) + ')');
  ok(mergedText.indexOf('[任务状态]') === -1, 'merged card headers stripped, content kept');
  // m2-m4 合并为 1 卡片 + m5 独立 1 卡片 = 2 张 task 卡片（非 1 张，非 4 张）
  const taskCards = env.messageStack.querySelectorAll('.msg').filter(k => k && k.dataset && (k.dataset.name === 'ui.task_command' || k.dataset.name === 'ui.task_lifecycle'));
  ok(taskCards.length === 2, 'm2-m4 merged into 1 + m5 independent = 2 task cards (got ' + taskCards.length + ')');
  // m5 task_command 独立卡片 summary = 任务指令
  const cmdCard = taskCards.find((k) => k.dataset.name === 'ui.task_command');
  ok(!!cmdCard, 'm5 task_command card exists (independent)');
  const cmdDetails = cmdCard && cmdCard._kids && cmdCard._kids[0];
  const cmdSummary = cmdDetails && cmdDetails._kids && cmdDetails._kids[0];
  ok(cmdSummary && cmdSummary.textContent === '任务指令', 'm5 task_command summary = 任务指令 (got ' + JSON.stringify(cmdSummary && cmdSummary.textContent) + ')');

  // ===========================================================================
  // T6: task final result (ui.task_result) renders as a regular message
  //     所有任务结束情况（成功/失败/取消/过期）均以普通消息渲染，非可折叠状态卡片
  // ===========================================================================
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  const resultMsgs = [
    { id: 'm1', role: 'user', content: '帮我调研 X' },
    { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1 - 调研X' },
    { id: 'm3', role: 'assistant', content: '正在处理调研' },
    { id: 'm4', role: 'system', name: 'ui.task_result', content: '任务已完成：调研X\n\n已交付调研报告', created_at: '2026-07-21T10:00:00Z' },
    { id: 'm5', role: 'system', name: 'ui.task_result', content: '任务已失败：其它任务\n\n工具不可用', created_at: '2026-07-21T11:00:00Z' },
    { id: 'm6', role: 'system', name: 'ui.task_result', content: '任务已取消：取消任务', created_at: '2026-07-21T12:00:00Z' },
  ];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve(detailWith(id, resultMsgs));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();

  function findResultEl(needle) {
    return env.messageStack.querySelectorAll('.msg.assistant').find((k) => k && k.textContent && k.textContent.indexOf(needle) !== -1);
  }
  // 所有结束情况均渲染为普通消息（className 'msg assistant'）
  const succeededEl = findResultEl('任务已完成');
  const failedEl = findResultEl('任务已失败');
  const cancelledEl = findResultEl('任务已取消');
  ok(!!succeededEl, 'SUCCEEDED ui.task_result rendered as regular message');
  ok(!!failedEl, 'FAILED ui.task_result rendered as regular message');
  ok(!!cancelledEl, 'CANCELLED ui.task_result rendered as regular message');
  // 均非可折叠卡片：子节点无 <details>（无 open 属性）
  [succeededEl, failedEl, cancelledEl].forEach((el, i) => {
    const isCard = !!(el && el._kids && el._kids.some((k) => k && typeof k === 'object' && 'open' in k));
    ok(!isCard, 'ui.task_result #' + i + ' not rendered as collapsible card');
  });
  // 正文以普通消息渲染（textContent 可见）
  ok(succeededEl && succeededEl.textContent.indexOf('已交付调研报告') !== -1, 'SUCCEEDED content visible');
  ok(failedEl && failedEl.textContent.indexOf('工具不可用') !== -1, 'FAILED content visible');
  // 旧气泡顶部 data-time 已移除，时间由通用消息容器渲染到 footer。
  ok(succeededEl && succeededEl.dataset && !succeededEl.dataset.time, 'ui.task_result has no legacy header timestamp');
}

// ===========================================================================
// createMessageElement: 非真人进程来源 user 消息渲染为左对齐状态卡片
// work task / judge task -> 折叠卡片，样式打平 ui.task_lifecycle 任务状态卡片
//   （className `msg system`，与 ui.task_command/ui.task_lifecycle 一致）：
//   summary 显示固定标题 任务状态（work task / judge task 统一，与合并卡、standalone lifecycle 一致），
//   pre 放原始 content，details 默认 open=false；
// schedule/curator 等其它进程消息 -> 非折叠左对齐状态卡片（msg--process-card）
// ===========================================================================
(function testProcessSourceRendering() {
  const env = loadChat();
  const createMessageElement = env.chat.createMessageElement;
  ok(typeof createMessageElement === 'function', 'createMessageElement exposed');

  function hasDetails(el) {
    return !!(el && el._kids && el._kids.some((k) => k && typeof k === 'object' && 'open' in k));
  }
  function findDetails(el) {
    if (!el || !el._kids) return null;
    return el._kids.find((k) => k && typeof k === 'object' && 'open' in k) || null;
  }

  // ui.task_lifecycle 任务状态卡片 className，用于样式打平对比
  const lifecycleEl = createMessageElement({ role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1 - 调研X' });
  const lifecycleClassName = lifecycleEl.className;
  ok(lifecycleClassName === 'msg system', 'ui.task_lifecycle renders msg system (got ' + lifecycleClassName + ')');

  // task work -> 折叠卡片（details，默认 open=false）+ summary = 任务状态（固定标题，与合并卡一致）
  let el = createMessageElement({ role: 'user', source: 'task', content: 'work task t1' });
  ok(el.className === lifecycleClassName,
    'task work renders same className as ui.task_lifecycle (got ' + el.className + ', want ' + lifecycleClassName + ')');
  ok(el.className.indexOf('msg--process-card') === -1 && el.className.indexOf('msg--process-folded') === -1,
    'task work not using process-card/process-folded classes (got ' + el.className + ')');
  ok(!el.dataset || el.dataset.source === undefined,
    'task work card has no dataset.source (aligned with ui.task_lifecycle)');
  ok(hasDetails(el), 'task work card is a details (collapsible)');
  let d = findDetails(el);
  ok(d && d.open === false, 'task work card folded by default (open=false) (got open=' + (d && d.open) + ')');
  let sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '任务状态',
    'work task summary = 任务状态 (fixed title, aligned with merged card / standalone lifecycle) (got ' + JSON.stringify(sm && sm.textContent) + ')');
  let pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === 'work task t1',
    'work task pre = raw content (got ' + JSON.stringify(pre && pre.textContent) + ')');

  // task judge -> 折叠卡片 + summary = 任务状态（固定标题，与 work task / 合并卡一致）
  el = createMessageElement({ role: 'user', source: 'task', content: 'judge task t_xxx: has the goal been achieved?' });
  ok(el.className === lifecycleClassName,
    'judge task renders same className as ui.task_lifecycle (got ' + el.className + ', want ' + lifecycleClassName + ')');
  ok(el.className.indexOf('msg--process-card') === -1 && el.className.indexOf('msg--process-folded') === -1,
    'judge task not using process-card/process-folded classes (got ' + el.className + ')');
  ok(!el.dataset || el.dataset.source === undefined,
    'judge task card has no dataset.source (aligned with ui.task_lifecycle)');
  ok(hasDetails(el), 'judge task card is a details (collapsible)');
  d = findDetails(el);
  ok(d && d.open === false, 'judge task card folded by default (open=false) (got open=' + (d && d.open) + ')');
  sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '任务状态',
    'judge task summary = 任务状态 (fixed title, aligned with work task / merged card) (got ' + JSON.stringify(sm && sm.textContent) + ')');
  pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === 'judge task t_xxx: has the goal been achieved?',
    'judge task pre = raw content (got ' + JSON.stringify(pre && pre.textContent) + ')');

  // 其他 task 进程消息（无 work/judge task 前缀）-> 非折叠左对齐状态卡片，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'some other task content' });
  ok(el.className === 'msg msg--process-card', 'other task content renders msg--process-card (non-folded) (got ' + el.className + ')');
  ok(!hasDetails(el), 'other task content not collapsible (no details)');
  ok(el.textContent === 'some other task content', 'other task content preserved without prefix (got ' + JSON.stringify(el.textContent) + ')');

  // 前缀匹配边界：精确前缀匹配（含尾空格），避免误伤；无尾空格或非行首 -> 不折叠、不加前缀
  // 'work task' 无尾空格 -> 不折叠，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'work task' });
  ok(el.className === 'msg msg--process-card', 'work task without trailing space: non-folded msg--process-card (got ' + el.className + ')');
  ok(!hasDetails(el), 'work task without trailing space: not collapsible');
  ok(el.textContent === 'work task', 'work task without trailing space: no prefix (got ' + JSON.stringify(el.textContent) + ')');
  // 'work tasks' (复数) -> 不折叠，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'work tasks abc' });
  ok(el.className === 'msg msg--process-card', 'work tasks (plural): non-folded msg--process-card (got ' + el.className + ')');
  ok(el.textContent === 'work tasks abc', 'work tasks (plural): no prefix (got ' + JSON.stringify(el.textContent) + ')');
  // 'judge task' 无尾空格 -> 不折叠，不加前缀
  el = createMessageElement({ role: 'user', source: 'task', content: 'judge task' });
  ok(el.className === 'msg msg--process-card', 'judge task without trailing space: non-folded msg--process-card (got ' + el.className + ')');
  ok(el.textContent === 'judge task', 'judge task without trailing space: no prefix (got ' + JSON.stringify(el.textContent) + ')');
  // 前缀在中间 -> 不折叠，不加前缀（仅行首前缀匹配）
  el = createMessageElement({ role: 'user', source: 'task', content: 'prefix work task abc' });
  ok(el.className === 'msg msg--process-card', 'work task in middle: non-folded msg--process-card (got ' + el.className + ')');
  ok(el.textContent === 'prefix work task abc', 'work task in middle: no prefix (got ' + JSON.stringify(el.textContent) + ')');

  // schedule / curator -> 非折叠左对齐状态卡片（无前缀）
  el = createMessageElement({ role: 'user', source: 'schedule', content: 'run prompt' });
  ok(el.className === 'msg msg--process-card', 'schedule renders msg--process-card (non-folded) (got ' + el.className + ')');
  ok(!hasDetails(el), 'schedule not collapsible (no details)');
  ok(el.textContent === 'run prompt', 'schedule content no prefix (got ' + JSON.stringify(el.textContent) + ')');

  el = createMessageElement({ role: 'user', source: 'curator', content: 'digest' });
  ok(el.className === 'msg msg--process-card', 'curator renders msg--process-card (non-folded) (got ' + el.className + ')');
  ok(!hasDetails(el), 'curator not collapsible (no details)');
  ok(el.textContent === 'digest', 'curator content no prefix (got ' + JSON.stringify(el.textContent) + ')');

  // 多模态 content（list）-> 走状态卡片样式但不折叠（折叠仅对 string content 生效）
  el = createMessageElement({ role: 'user', source: 'task', content: [{ type: 'text', text: 'work task t1' }] });
  ok(el.className === 'msg msg--process-card', 'multimodal task content renders msg--process-card (non-folded)');
  ok(!hasDetails(el), 'multimodal task content not collapsible (no details)');
  ok(el.textContent.indexOf('work task t1') !== -1, 'multimodal text part preserved');
  ok(el.textContent.indexOf('查询状态:') === -1, 'multimodal content: no legacy prefix (string-only fold rule)');

  // 真人渠道 -> 普通蓝底气泡（msg user）
  el = createMessageElement({ role: 'user', source: 'dashboard', content: 'hi' });
  ok(el.className === 'msg user', 'dashboard user renders bubble (got ' + el.className + ')');
  ok(!el.dataset || !el.dataset.source, 'dashboard bubble has no dataset.source');

  // null / 未知 / 缺失 source -> 普通气泡
  ok(createMessageElement({ role: 'user', source: null, content: 'hi' }).className === 'msg user', 'null source renders bubble');
  ok(createMessageElement({ role: 'user', source: 'unknown', content: 'hi' }).className === 'msg user', 'unknown source renders bubble');
  ok(createMessageElement({ role: 'user', content: 'hi' }).className === 'msg user', 'missing source renders bubble');

  // 非 user 角色即使带 task source 也不走进程状态卡片
  el = createMessageElement({ role: 'assistant', source: 'task', content: 'ok' });
  ok(el.className !== 'msg msg--process-card', 'assistant with task source not process card (got ' + el.className + ')');

  // === 合并卡片渲染（_mergedTaskStatus=true）：summary 固定=任务状态，open=false，pre 多行 ===
  // 跨 role 合并：first message 是 work task (role=user source=task) -> 合并卡片 className=msg system（非 msg--process-card）
  const mergedFirstWork = {
    role: 'user', source: 'task', name: null,
    content: '查询状态: work task t_aaa\n开始运行: t_aaa - 查天气',
    _mergedTaskStatus: true,
  };
  el = createMessageElement(mergedFirstWork);
  ok(el.className === 'msg system', 'merged card (first work task) className = msg system (got ' + el.className + ')');
  ok(el.className.indexOf('msg--process-card') === -1, 'merged card not using msg--process-card (got ' + el.className + ')');
  ok(!el.dataset || el.dataset.source === undefined, 'merged card has no dataset.source (aligned with system)');
  ok(hasDetails(el), 'merged card is a details (collapsible)');
  d = findDetails(el);
  ok(d && d.open === false, 'merged card folded by default (open=false) (got open=' + (d && d.open) + ')');
  sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '任务状态', 'merged card summary = 任务状态 (fixed) (got ' + JSON.stringify(sm && sm.textContent) + ')');
  pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === '查询状态: work task t_aaa\n开始运行: t_aaa - 查天气', 'merged card pre = multi-line content (got ' + JSON.stringify(pre && pre.textContent) + ')');
  ok(el.dataset && el.dataset.name === 'ui.task_lifecycle', 'merged card dataset.name = ui.task_lifecycle (got ' + JSON.stringify(el.dataset && el.dataset.name) + ')');

  // 合并卡片：first message 是 lifecycle (role=system name=ui.task_lifecycle)
  const mergedFirstLifecycle = {
    role: 'system', name: 'ui.task_lifecycle',
    content: '开始运行: t_aaa\n判断结束: judge task t_aaa: goal?',
    _mergedTaskStatus: true,
  };
  el = createMessageElement(mergedFirstLifecycle);
  ok(el.className === 'msg system', 'merged card (first lifecycle) className = msg system (got ' + el.className + ')');
  d = findDetails(el);
  ok(d && d.open === false, 'merged card (lifecycle first) folded (open=false)');
  sm = d && d._kids && d._kids[0];
  ok(sm && sm.textContent === '任务状态', 'merged card (lifecycle first) summary = 任务状态');
  pre = d && d._kids && d._kids[1];
  ok(pre && pre.textContent === '开始运行: t_aaa\n判断结束: judge task t_aaa: goal?', 'merged card (lifecycle first) pre = multi-line content');

  // 端到端：相邻 lifecycle + work task 经 groupTaskMessages + createMessageElement 渲染为合并卡片
  const envE2E = loadChat();
  const e2eMessages = [
    { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa - 查天气' },
    { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
  ];
  const groupedE2E = envE2E.chat.groupTaskMessages(e2eMessages);
  ok(groupedE2E.length === 1 && groupedE2E[0]._mergedTaskStatus === true, 'e2e: lifecycle + work task merged into 1 group');
  const e2eEl = envE2E.chat.createMessageElement(groupedE2E[0]);
  ok(e2eEl.className === 'msg system', 'e2e merged card className = msg system (got ' + e2eEl.className + ')');
  const e2eDetails = e2eEl._kids && e2eEl._kids[0];
  ok(e2eDetails && e2eDetails.open === false, 'e2e merged card open=false');
  const e2eSummary = e2eDetails && e2eDetails._kids && e2eDetails._kids[0];
  ok(e2eSummary && e2eSummary.textContent === '任务状态', 'e2e merged card summary = 任务状态');
  const e2ePre = e2eDetails && e2eDetails._kids && e2eDetails._kids[1];
  ok(e2ePre && e2ePre.textContent === '开始运行: t_aaa - 查天气\n查询状态: work task t_aaa', 'e2e merged card pre = lifecycle 原文 + work task 带前缀 (got ' + JSON.stringify(e2ePre && e2ePre.textContent) + ')');
})();

// ===========================================================================
// ui.task_result 吸收相邻 ui.task_artifact：制品气泡合并到任务完成气泡内渲染
// 任务完成后写入 task_result + N 条 task_artifact，前端合并为一个气泡：
// 结果正文 + 逐行「产出制品: name 详情」链接（复用 /artifacts/{id} 导航）
// ===========================================================================
(function testTaskResultAbsorbsArtifacts() {
  const env = loadChat();
  const g = env.chat.groupTaskMessages;
  const createMessageElement = env.chat.createMessageElement;
  function collectAnchors(node) {
    const out = [];
    (function walk(n) {
      if (!n || !n._kids) return;
      for (const k of n._kids) { if (k.tagName === 'A') out.push(k); walk(k); }
    })(node);
    return out;
  }

  // --- 分组：task_result 吸收后续相邻 task_artifact ---
  const grouped = g([
    { id: 'r1', role: 'system', name: 'ui.task_result', content: '任务已完成：测试：任务制品\n\nCreated task-output-a.txt' },
    { id: 'a1', role: 'system', name: 'ui.task_artifact', content: '产出制品: task-output-a.txt', card: { schema_version: 1, kind: 'task_artifact', artifact_id: 'artA', name: 'task-output-a.txt' } },
    { id: 'a2', role: 'system', name: 'ui.task_artifact', content: '产出制品: task-output-b.md', card: { schema_version: 1, kind: 'task_artifact', artifact_id: 'artB', name: 'task-output-b.md' } },
  ]);
  ok(grouped.length === 1, 'task_result + 2 task_artifact: 1 merged group (got ' + grouped.length + ')');
  ok(grouped[0].id === 'r1' && grouped[0].name === 'ui.task_result', 'merged group keeps task_result id/name');
  ok(Array.isArray(grouped[0]._resultArtifacts) && grouped[0]._resultArtifacts.length === 2, 'merged group carries 2 _resultArtifacts');
  ok(grouped[0]._resultArtifacts[0].artifact_id === 'artA' && grouped[0]._resultArtifacts[0].name === 'task-output-a.txt', 'first artifact ref (name+artifact_id) captured');
  ok(grouped[0]._resultArtifacts[1].artifact_id === 'artB' && grouped[0]._resultArtifacts[1].name === 'task-output-b.md', 'second artifact ref captured');

  // --- 渲染：合并后的 task_result 气泡含结果正文 + 制品详情链接 ---
  const el = createMessageElement(grouped[0]);
  ok(el.className === 'msg assistant', 'merged task_result renders as msg assistant (got ' + el.className + ')');
  ok(el.textContent.indexOf('任务已完成') !== -1, 'merged bubble shows result text');
  ok(el.textContent.indexOf('产出制品: task-output-a.txt') !== -1 && el.textContent.indexOf('产出制品: task-output-b.md') !== -1, 'merged bubble shows both artifact labels');
  ok(el.textContent.indexOf('详情') !== -1, 'merged bubble shows 详情 links');
  const hrefs = collectAnchors(el).map((a) => a.href).sort();
  ok(hrefs.length === 2 && hrefs[0] === '/artifacts/artA' && hrefs[1] === '/artifacts/artB', 'merged bubble has 2 artifact links with correct hrefs (got ' + JSON.stringify(hrefs) + ')');

  // --- 独立 task_artifact（无前导 task_result）保持独立渲染 ---
  const standalone = g([
    { id: 'a1', role: 'system', name: 'ui.task_artifact', content: '产出制品: x.txt', card: { artifact_id: 'artX', name: 'x.txt' } },
  ]);
  ok(standalone.length === 1 && standalone[0].id === 'a1' && standalone[0]._resultArtifacts === undefined, 'standalone task_artifact: independent, no _resultArtifacts');

  // --- task_artifact 不被非 task_result 消息吸收（command 断开链）---
  const notAbsorbed = g([
    { id: 'c1', role: 'system', name: 'ui.task_command', content: 'cmd' },
    { id: 'a1', role: 'system', name: 'ui.task_artifact', content: '产出制品: a.txt', card: { artifact_id: 'artA', name: 'a.txt' } },
  ]);
  ok(notAbsorbed.length === 2, 'command + task_artifact: 2 groups (not absorbed by non-result)');
  ok(notAbsorbed[1].id === 'a1' && notAbsorbed[1]._resultArtifacts === undefined, 'task_artifact after command stays independent');

  // --- task_result 后跟 command 再跟 task_artifact：command 断开吸收链 ---
  const broken = g([
    { id: 'r1', role: 'system', name: 'ui.task_result', content: '任务已完成：t' },
    { id: 'c1', role: 'system', name: 'ui.task_command', content: 'cmd' },
    { id: 'a1', role: 'system', name: 'ui.task_artifact', content: '产出制品: a.txt', card: { artifact_id: 'artA', name: 'a.txt' } },
  ]);
  ok(broken.length === 3, 'result + command + artifact: 3 groups (command breaks absorption)');
  ok(broken[0]._resultArtifacts === undefined, 'task_result has no absorbed artifacts when command breaks chain');

  // --- 无 artifact_id 的 task_artifact 仍被吸收；渲染时显示名称但不生成链接 ---
  const noId = g([
    { id: 'r1', role: 'system', name: 'ui.task_result', content: '任务已完成：t' },
    { id: 'a1', role: 'system', name: 'ui.task_artifact', content: '产出制品: a.txt', card: { name: 'a.txt' } },
  ]);
  ok(noId.length === 1 && noId[0]._resultArtifacts.length === 1, 'task_artifact without artifact_id still absorbed');
  const noIdEl = createMessageElement(noId[0]);
  ok(noIdEl.textContent.indexOf('产出制品: a.txt') !== -1, 'artifact name still rendered without artifact_id');
  ok(collectAnchors(noIdEl).length === 0, 'no link rendered when artifact_id missing (got ' + collectAnchors(noIdEl).length + ')');
})();

// ===========================================================================
// shouldRenderMessage: task/curator 进程来源的 assistant 推理属 worker 内部
// 思考过程，不在 Dashboard 对话框展示（task 经 ui.task_lifecycle 卡片对外，
// curator 为内部维护）；realtime（api/dashboard/无 source）assistant 正常渲染；
// 进程来源 user/tool 消息不受影响。Regression: worker CoT "The task requires
// querying weather..." 泄露为普通气泡。
// schedule 例外：其最终 assistant 消息是定时任务投递记录（无独立 ui.task_result
// 卡片机制），必须在 Dashboard 对话框可见；带 tool_calls 的中间步即使带正文也隐藏。
// Regression: 定时任务投递记录被误隐藏、realtime 工具调用中间步泄露。
// ===========================================================================
(function testProcessAssistantReasoningHidden() {
  const env = loadChat();
  const shouldRenderMessage = env.chat.shouldRenderMessage;
  ok(typeof shouldRenderMessage === 'function', 'shouldRenderMessage exposed');

  // task/curator 进程来源 assistant 推理 -> 隐藏（worker 思考过程不进对话框）
  ok(shouldRenderMessage({ role: 'assistant', source: 'task', content: 'The task requires querying weather...' }) === false,
    'task-sourced assistant reasoning hidden');
  ok(shouldRenderMessage({ role: 'assistant', source: 'curator', content: 'consolidation reasoning' }) === false,
    'curator-sourced assistant reasoning hidden');
  ok(shouldRenderMessage({ role: 'assistant', source: 'task', content: '' }) === false,
    'task-sourced assistant with empty content hidden by source');

  // schedule 进程来源 assistant 消息是投递记录 -> 可见（非空内容）
  ok(shouldRenderMessage({ role: 'assistant', source: 'schedule', content: '定点报时：\n- UTC+8：2026-07-26 18:46:06' }) === true,
    'schedule-sourced assistant delivery record rendered');
  // schedule 空内容（仅 tool_calls 中间步）仍隐藏
  ok(shouldRenderMessage({ role: 'assistant', source: 'schedule', content: '' }) === false,
    'schedule-sourced assistant with empty content hidden by hasVisibleContent');

  // realtime assistant（api/dashboard/无 source）-> 正常渲染
  ok(shouldRenderMessage({ role: 'assistant', source: 'api', content: 'reply' }) === true,
    'api-sourced assistant rendered');
  ok(shouldRenderMessage({ role: 'assistant', source: 'dashboard', content: 'reply' }) === true,
    'dashboard-sourced assistant rendered');
  ok(shouldRenderMessage({ role: 'assistant', content: 'reply' }) === true,
    'untagged assistant rendered');

  // realtime 带正文的工具调用中间步仍是过程消息；最终答复没有 tool_calls，保持可见。
  ok(shouldRenderMessage({
    role: 'assistant',
    source: null,
    content: '我来帮您查询天气。先看看是否有您之前保存的常用地点，同时加载天气查询技能。',
    tool_calls: [
      { id: 'call-memory', function: { name: 'mem0_search', arguments: '{"query":"常用地点"}' } },
      { id: 'call-skill', function: { name: 'skill_view', arguments: '{"name":"weather"}' } },
    ],
  }) === false, 'realtime assistant content with tool calls hidden');
  ok(shouldRenderMessage({
    role: 'assistant',
    source: null,
    content: '已确认您的偏好：未指定城市时默认查询**北京**。正在获取实时天气。',
    tool_calls: [
      { id: 'call-weather', function: { name: 'web_fetch', arguments: '{"url":"https://wttr.in/Beijing?format=j2"}' } },
    ],
  }) === false, 'second realtime assistant tool-calling step hidden');
  ok(shouldRenderMessage({ role: 'assistant', source: null, content: '北京当前晴，气温 12°C。' }) === true,
    'final realtime assistant answer remains rendered');
  ok(shouldRenderMessage({
    role: 'assistant',
    source: 'schedule',
    content: '正在获取定时天气。',
    tool_calls: [{ id: 'call-weather', function: { name: 'web_fetch', arguments: '{}' } }],
  }) === false, 'schedule tool-calling intermediate step hidden');

  // 进程来源 user 消息仍渲染（折叠卡片，不受 assistant 隐藏影响）
  ok(shouldRenderMessage({ role: 'user', source: 'task', content: 'work task t1' }) === true,
    'task-sourced user still rendered (folded card)');
  // tool 消息仍渲染
  ok(shouldRenderMessage({ role: 'tool', content: '{}' }) === true,
    'tool message still rendered');
})();

// ===========================================================================
// 调试设置：createMessageElement 为工具调用/任务状态/对话压缩卡片打 data-debug-kind 标记，
// 供设置弹框的显隐规则（CSS）控制。任务结果/普通消息/进程消息不打标记。
// ===========================================================================
(function testDebugKindTagging() {
  const env = loadChat();
  const createMessageElement = env.chat.createMessageElement;
  ok(typeof createMessageElement === 'function', 'createMessageElement exposed for debug-kind');

  // 工具调用卡片 -> tool
  let el = createMessageElement({ role: 'tool', content: '{"k":1}' });
  ok(el && el.dataset && el.dataset.debugKind === 'tool', 'tool message tagged data-debug-kind=tool (got ' + JSON.stringify(el && el.dataset && el.dataset.debugKind) + ')');

  // 任务指令卡片 -> task
  el = createMessageElement({ role: 'system', name: 'ui.task_command', content: '执行命令: /task list' });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'task_command tagged data-debug-kind=task (got ' + JSON.stringify(el && el.dataset && el.dataset.debugKind) + ')');

  // 任务状态卡片(无 card) -> task
  el = createMessageElement({ role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1 - 调研' });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'task_lifecycle (no card) tagged data-debug-kind=task');

  // 任务状态交互卡片(带 card) -> task
  el = createMessageElement(
    { role: 'system', name: 'ui.task_lifecycle', content: '等待批准: t_1',
      card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve', 'cancel'] } },
    new Map([['t_1', { kind: 'resolved', status: 'waiting_approval' }]]),
    new Map()
  );
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'task-card (with card) tagged data-debug-kind=task');

  // 合并任务状态卡片 -> task
  el = createMessageElement({ role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_1\n查询状态: work task t_1', _mergedTaskStatus: true });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'merged task status tagged data-debug-kind=task');

  // work task 折叠卡片 -> task
  el = createMessageElement({ role: 'user', source: 'task', content: 'work task t_1' });
  ok(el && el.dataset && el.dataset.debugKind === 'task', 'work task folded card tagged data-debug-kind=task');

  // 对话压缩摘要卡片 -> compression
  el = createMessageElement({ role: 'user', is_summary: true, content: '[CONTEXT SUMMARY]: 历史摘要' });
  ok(el && el.dataset && el.dataset.debugKind === 'compression', 'summary message tagged data-debug-kind=compression (got ' + JSON.stringify(el && el.dataset && el.dataset.debugKind) + ')');

  // 普通消息/任务结果/进程消息 不打标记（不受调试显隐控制）
  ok(!(createMessageElement({ role: 'user', source: 'dashboard', content: 'hi' }).dataset.debugKind), 'dashboard user has no debugKind');
  ok(!(createMessageElement({ role: 'assistant', content: 'ok' }).dataset.debugKind), 'assistant has no debugKind');
  ok(!(createMessageElement({ role: 'system', name: 'ui.task_result', content: '任务已完成' }).dataset.debugKind), 'task_result has no debugKind');
  ok(!(createMessageElement({ role: 'user', source: 'schedule', content: 'run' }).dataset.debugKind), 'schedule process card has no debugKind');
})();

// ===========================================================================
// Browser screenshot tool result: only the canonical successful envelope may
// create a chat screenshot preview. Failed/malformed results remain ordinary
// tool-debug output and never trigger an image fetch.
// ===========================================================================
(function testBrowserScreenshotRecognition() {
  const env = loadChat();
  ok(typeof env.chat.isSuccessfulBrowserScreenshot === 'function', 'browser screenshot recognizer exposed');
  const success = JSON.stringify({
    name: 'browser_screenshot', status: 'success',
    content: { action_type: 'screenshot', status: 'success', screenshot_captured: true },
  });
  ok(env.chat.isSuccessfulBrowserScreenshot(success) === true, 'recognizes successful browser screenshot result');
  ok(env.chat.isSuccessfulBrowserScreenshot([JSON.stringify({ name: 'calculator', status: 'success', content: { screenshot_captured: true } }), success]) === true,
    'recognizes screenshot inside grouped tool messages');
  ok(env.chat.isSuccessfulBrowserScreenshot(JSON.stringify({
    name: 'browser_screenshot', status: 'error',
    content: { action_type: 'screenshot', status: 'error', screenshot_captured: true },
  })) === false, 'does not render failed screenshot result');
  ok(env.chat.isSuccessfulBrowserScreenshot('{invalid') === false, 'does not render malformed tool payload');

  const screenshotCard = env.chat.createMessageElement({
    role: 'tool', content: success,
  });
  ok(!screenshotCard.dataset.debugKind,
    'successful screenshot is visible even when tool-debug cards are hidden');
})();

// ===========================================================================
// T6/T7: task card interaction (validate / state-resolve / group / render / action)
// ===========================================================================
async function testTaskCardInteraction() {
  // Recursive descendant finder (harness makeEl has no querySelector).
  function findDescendants(el, predicate) {
    const out = [];
    function walk(node) {
      if (!node || !node._kids) return;
      for (const k of node._kids) {
        if (predicate(k)) out.push(k);
        walk(k);
      }
    }
    walk(el);
    return out;
  }
  const findByClass = (el, cls) => findDescendants(el, (n) => {
    if (!n || typeof n.className !== 'string') return false;
    return n.className.split(/\s+/).indexOf(cls) !== -1;
  });
  const hasButton = (card, action) => findByClass(card, 'task-card__btn').some((b) => b.dataset && b.dataset.action === action);
  const detailWith = (id, msgs) => ({ session: { id }, messages: msgs, summary: null, task_state: null });

  // unhandledRejection monitor for the duration of this test block.
  let unhandledCount = 0;
  const unhandledReasons = [];
  const unhandledHandler = (reason) => { unhandledCount++; unhandledReasons.push(reason); };
  process.on('unhandledRejection', unhandledHandler);

  const VC = {
    schema_version: 1, kind: 'task_lifecycle', task_id: 't_1',
    status: 'waiting_approval', title: 'T', summary: 'S',
    available_actions: ['approve', 'reject', 'revise', 'cancel'],
  };

  try {
    // --- T6: validateTaskCard (table-driven) ---
    const env1 = loadChat();
    const validate = env1.chat.validateTaskCard;
    ok(typeof validate === 'function', 'validateTaskCard exposed');

    ok(validate(null) === null, 'validate null -> null');
    ok(validate(undefined) === null, 'validate undefined -> null');
    ok(validate('x') === null, 'validate string -> null');
    ok(validate([]) === null, 'validate array -> null');
    ok(validate(42) === null, 'validate number -> null');
    ok(validate({}) === null, 'empty object -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S' }) === null, 'missing available_actions -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: [] }) === null, 'empty available_actions -> null');
    ok(validate({ kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing schema_version -> null');
    ok(validate({ schema_version: 1, task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing kind -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing task_id -> null');
    ok(validate({ schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', title: 'T', summary: 'S', available_actions: ['approve'] }) === null, 'missing status -> null');
    ok(validate({ ...VC, schema_version: '1' }) === null, 'schema_version string -> null');
    ok(validate({ ...VC, kind: 'other' }) === null, 'unknown kind -> null');
    ok(validate({ ...VC, status: 'running' }) === null, 'unknown status -> null');
    ok(validate({ ...VC, task_id: '' }) === null, 'blank task_id -> null');
    ok(validate({ ...VC, task_id: '   ' }) === null, 'whitespace task_id -> null');
    ok(validate({ ...VC, available_actions: 'approve' }) === null, 'available_actions string -> null');
    ok(validate({ ...VC, available_actions: [{ action: 'approve' }] }) === null, 'available_actions non-string elements -> null');
    ok(validate({ ...VC, schema_version: 2 }) === null, 'unknown schema_version -> null');
    ok(validate({ ...VC, available_actions: ['foo', 'bar'] }) === null, 'only unknown actions -> null');

    // mixed: allowlist kept in original order, unknown dropped
    const mixed = validate({ ...VC, available_actions: ['foo', 'approve', 'bar', 'reject', 'cancel', 'baz', 'revise'] });
    ok(mixed !== null, 'mixed card valid');
    ok(mixed.available_actions.length === 4, 'mixed actions filtered to 4 (got ' + JSON.stringify(mixed && mixed.available_actions) + ')');
    ok(mixed.available_actions[0] === 'approve' && mixed.available_actions[1] === 'reject' && mixed.available_actions[2] === 'cancel' && mixed.available_actions[3] === 'revise', 'mixed actions in original order');

    // canonical: new object, no mutation of input
    const input = { ...VC, available_actions: ['approve', 'foo'] };
    const inputActionsBefore = input.available_actions.slice();
    const out = validate(input);
    ok(input.available_actions.length === inputActionsBefore.length && input.available_actions[1] === 'foo', 'validate does not mutate input');
    ok(out !== input, 'validate returns new object');
    ok(out.task_id === 't_1' && out.status === 'waiting_approval' && out.title === 'T' && out.summary === 'S', 'canonical preserves scalar fields');

    // each valid status accepted
    ok(validate({ ...VC, status: 'failed', available_actions: ['retry', 'cancel'] }) !== null, 'failed status valid');
    ok(validate({ ...VC, status: 'expired', available_actions: ['retry'] }) !== null, 'expired status valid');

    // --- interaction_type validation (waiting_approval only) ---
    // missing interaction_type on waiting_approval -> default 'approval'
    const vcApproval = { ...VC, available_actions: ['approve', 'reject'] };
    const approvalOut = validate(vcApproval);
    ok(approvalOut !== null, 'waiting_approval without interaction_type valid');
    ok(approvalOut.interaction_type === 'approval', 'missing interaction_type defaults to approval (got ' + JSON.stringify(approvalOut && approvalOut.interaction_type) + ')');

    // explicit interaction_type='approval'
    ok(validate({ ...vcApproval, interaction_type: 'approval' }).interaction_type === 'approval', 'explicit interaction_type=approval preserved');

    // interaction_type='intent_request'
    const intentOut = validate({ ...VC, available_actions: ['revise', 'cancel'], interaction_type: 'intent_request' });
    ok(intentOut !== null && intentOut.interaction_type === 'intent_request', 'interaction_type=intent_request preserved');

    // invalid interaction_type value -> null
    ok(validate({ ...vcApproval, interaction_type: 'bogus' }) === null, 'invalid interaction_type value -> null');
    ok(validate({ ...vcApproval, interaction_type: 42 }) === null, 'non-string interaction_type -> null');

    // non-waiting_approval card ignores interaction_type (not in canonical)
    const failedOut = validate({ ...VC, status: 'failed', available_actions: ['retry', 'cancel'], interaction_type: 'approval' });
    ok(failedOut !== null, 'failed card with stray interaction_type still valid');
    ok(!('interaction_type' in failedOut), 'failed canonical excludes interaction_type (got ' + JSON.stringify(failedOut) + ')');

    // canonical: new object, no mutation of input (interaction_type field)
    const inputIT = { ...vcApproval, interaction_type: 'intent_request' };
    const outIT = validate(inputIT);
    ok(outIT !== inputIT, 'validate returns new object (interaction_type)');
    ok(inputIT.interaction_type === 'intent_request', 'validate does not mutate input.interaction_type');

    // --- T6: state resolution (GET dedup + per-card active/stale) ---
    let env2 = loadChat();
    let getCalls = [];
    env2.win.NAGENT.api.task.get = (id) => {
      getCalls.push(id);
      if (id === 't_a') return Promise.resolve({ id, status: 'queued' });
      if (id === 't_b') return Promise.resolve({ id, status: 'waiting_approval' });
      return Promise.resolve({ id, status: 'unknown' });
    };
    const cardMsgs = [
      { id: 'm1', role: 'user', content: 'hi' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '等待批准: t_a',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_a', status: 'waiting_approval', title: 'TA', summary: 'S', available_actions: ['approve', 'cancel'] } },
      { id: 'm3', role: 'system', name: 'ui.task_lifecycle', content: '已失败: t_a',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_a', status: 'failed', title: 'TA', summary: 'S', available_actions: ['retry', 'cancel'] } },
      { id: 'm4', role: 'system', name: 'ui.task_lifecycle', content: '等待批准: t_b',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_b', status: 'waiting_approval', title: 'TB', summary: 'S', available_actions: ['approve'] } },
    ];
    await env2.chat.applySessionDetail(detailWith('s1', cardMsgs));
    await env2.waitMicro();
    ok(getCalls.length === 2, 'GET dedup: 2 unique task_ids (got ' + getCalls.length + ', calls=' + JSON.stringify(getCalls) + ')');
    const cards2 = findByClass(env2.messageStack, 'task-card');
    ok(cards2.length === 3, '3 task cards rendered (got ' + cards2.length + ')');
    ok(!hasButton(cards2[0], 'approve') && !hasButton(cards2[0], 'cancel'), 'm2 (auth=queued != card=waiting_approval) -> stale, no buttons');
    ok(!hasButton(cards2[1], 'retry') && !hasButton(cards2[1], 'cancel'), 'm3 (auth=queued != card=failed) -> stale, no buttons');
    ok(hasButton(cards2[2], 'approve'), 'm4 (auth=waiting_approval == card) -> active, approve present');

    // task_not_found -> stale
    let env3 = loadChat();
    env3.win.NAGENT.api.task.get = () => Promise.reject(new Error('task_not_found'));
    await env3.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: { ...VC, task_id: 't_x' } },
    ]));
    await env3.waitMicro();
    const cards3 = findByClass(env3.messageStack, 'task-card');
    ok(cards3.length === 1 && !hasButton(cards3[0], 'approve'), 'task_not_found -> stale, no buttons');

    // network error -> unavailable
    let env4 = loadChat();
    env4.win.NAGENT.api.task.get = () => Promise.reject(new Error('network_down'));
    await env4.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: { ...VC, task_id: 't_y' } },
    ]));
    await env4.waitMicro();
    const cards4 = findByClass(env4.messageStack, 'task-card');
    ok(cards4.length === 1 && !hasButton(cards4[0], 'approve'), 'network error -> unavailable, no buttons');

    // --- GET in-flight: no clickable actions in DOM before GET resolves ---
    let env5 = loadChat();
    let resolveGet;
    env5.win.NAGENT.api.task.get = () => new Promise((res) => { resolveGet = res; });
    const p5 = env5.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'user', content: 'FIRST' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ])).catch(() => {});
    await env5.waitMicro();
    ok(findByClass(env5.messageStack, 'task-card__btn').length === 0, 'no clickable actions before GET resolves');
    resolveGet({ id: 't_1', status: 'waiting_approval' });
    await p5;
    await env5.waitMicro();
    ok(hasButton(findByClass(env5.messageStack, 'task-card')[0], 'approve'), 'approve button appears after GET resolves (active)');

    // --- render token: stale render does not overwrite newer content ---
    let env6 = loadChat();
    let resolveGet6;
    env6.win.NAGENT.api.task.get = () => new Promise((res) => { resolveGet6 = res; });
    const p6 = env6.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'user', content: 'FIRST' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ])).catch(() => {});
    await env6.waitMicro();
    await env6.chat.applySessionDetail(detailWith('s1', [
      { id: 'm3', role: 'user', content: 'SECOND' },
    ]));
    await env6.waitMicro();
    ok(env6.messageStack.textContent.indexOf('SECOND') !== -1, 'second render applied (got ' + JSON.stringify(env6.messageStack.textContent) + ')');
    ok(env6.messageStack.textContent.indexOf('FIRST') === -1, 'first render not in DOM (pending GET)');
    resolveGet6({ id: 't_1', status: 'waiting_approval' });
    await p6;
    await env6.waitMicro();
    ok(env6.messageStack.textContent.indexOf('SECOND') !== -1, 'after first GET resolves: second still wins (render token)');
    ok(env6.messageStack.textContent.indexOf('FIRST') === -1, 'first render did not overwrite (render token)');

    // --- T6: grouping ---
    let env7 = loadChat();
    const g = env7.chat.groupTaskMessages;
    ok(typeof g === 'function', 'groupTaskMessages exposed');
    const grouped = g([
      { id: 'm1', role: 'system', name: 'ui.task_command', content: 'c1' },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: 'l1', card: VC },
      { id: 'm3', role: 'system', name: 'ui.task_lifecycle', content: 'l2', card: VC },
      { id: 'm4', role: 'system', name: 'ui.task_command', content: 'c2' },
      { id: 'm5', role: 'system', name: 'ui.task_lifecycle', content: 'l3' },
      { id: 'm6', role: 'system', name: 'ui.task_lifecycle', content: 'l4', card: { broken: true } },
    ]);
    // m1/m4 (task_command) 独立；m2/m3 (valid card) 独立；m5+m6 (lifecycle 无/坏 card) 合并 = 5 组
    ok(grouped.length === 5, 'groupTaskMessages: 5 groups (m1,m2,m3,m4 independent + m5+m6 merged) (got ' + grouped.length + ')');
    ok(grouped[0].content === 'c1' && grouped[0].name === 'ui.task_command', 'group 0 is m1 task_command (independent)');
    ok(grouped[1].card && grouped[1].card.task_id === 't_1', 'group 1 is m2 valid card (independent)');
    ok(grouped[2].card && grouped[2].card.task_id === 't_1', 'group 2 is m3 valid card (independent)');
    ok(grouped[3].content === 'c2' && grouped[3].name === 'ui.task_command', 'group 3 is m4 task_command (independent, breaks merge chain)');
    // m4 task_command 断开合并链：m5+m6 合并为 g4（不含 c2）
    ok(grouped[4].content.indexOf('l3') !== -1 && grouped[4].content.indexOf('l4') !== -1, 'group 4 merged m5+l3 + m6+l4 (got ' + JSON.stringify(grouped[4].content) + ')');
    ok(grouped[4].content.indexOf('c2') === -1, 'group 4 merged does NOT contain c2 (command broke chain) (got ' + JSON.stringify(grouped[4].content) + ')');
    ok(grouped[4].id === 'm5' && grouped[4].name === 'ui.task_lifecycle' && grouped[4]._mergedTaskStatus === true, 'group 4 merged keeps first message metadata + _mergedTaskStatus flag (got ' + JSON.stringify({ id: grouped[4].id, name: grouped[4].name, flag: grouped[4]._mergedTaskStatus }) + ')');
    // valid card message preserved by reference (not cloned/mutated)
    const cardInput = { id: 'mx', role: 'system', name: 'ui.task_lifecycle', content: 'l', card: VC };
    const g2 = env7.chat.groupTaskMessages([cardInput, { id: 'my', role: 'assistant', content: 'a' }]);
    ok(g2[0] === cardInput, 'valid card message preserved by reference');

    // --- 合并规则：work task / judge task / lifecycle 无 card 相邻合并 ---
    // 单独 work task（无相邻可合并对象）：1-message group，保持 role=user source=task，无 _mergedTaskStatus 标志
    const singleWork = g([
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
    ]);
    ok(singleWork.length === 1, 'single work task: 1 group (got ' + singleWork.length + ')');
    ok(singleWork[0].role === 'user' && singleWork[0].source === 'task' && singleWork[0].content === 'work task t_aaa', 'single work task keeps raw content + role/user (no merge)');
    ok(singleWork[0]._mergedTaskStatus === undefined, 'single work task: no _mergedTaskStatus flag (got ' + JSON.stringify(singleWork[0]._mergedTaskStatus) + ')');

    // 单独 judge task：1-message group
    const singleJudge = g([
      { id: 'j1', role: 'user', source: 'task', content: 'judge task t_bbb: goal?' },
    ]);
    ok(singleJudge.length === 1 && singleJudge[0].content === 'judge task t_bbb: goal?', 'single judge task keeps raw content (got ' + JSON.stringify(singleJudge[0].content) + ')');

    // 合并：lifecycle 无 card + work task -> 一个卡片，summary=任务状态，pre 多行（lifecycle 原文 + 查询状态: work task...）
    const merged1 = g([
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa - 查天气' },
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
    ]);
    ok(merged1.length === 1, 'lifecycle + work task: 1 merged group (got ' + merged1.length + ')');
    ok(merged1[0]._mergedTaskStatus === true, 'lifecycle + work task: _mergedTaskStatus=true');
    ok(merged1[0].content === '开始运行: t_aaa - 查天气\n查询状态: work task t_aaa', 'merged content: lifecycle 原文 + work task 带前缀 (got ' + JSON.stringify(merged1[0].content) + ')');
    ok(merged1[0].role === 'system' && merged1[0].name === 'ui.task_lifecycle', 'merged keeps first message role/name');

    // 合并：judge task + lifecycle 无 card -> summary=任务状态，pre 多行（判断结束: judge task... + lifecycle 原文）
    const merged2 = g([
      { id: 'j1', role: 'user', source: 'task', content: 'judge task t_bbb: goal?' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '已完成: t_bbb - 交付' },
    ]);
    ok(merged2.length === 1, 'judge task + lifecycle: 1 merged group (got ' + merged2.length + ')');
    ok(merged2[0]._mergedTaskStatus === true, 'judge task + lifecycle: _mergedTaskStatus=true');
    ok(merged2[0].content === '判断结束: judge task t_bbb: goal?\n已完成: t_bbb - 交付', 'merged content: judge task 带前缀 + lifecycle 原文 (got ' + JSON.stringify(merged2[0].content) + ')');
    // 跨 role 合并：first message work task (role=user) -> 合并卡片保留 role=user source=task
    ok(merged2[0].role === 'user' && merged2[0].source === 'task', 'cross-role merge keeps first message role/source');

    // 不合并：上一条 ui.task_command 断开合并链（当前 task 状态卡片不与它合并）
    const cmdBreak = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
    ]);
    ok(cmdBreak.length === 2, 'task_command + lifecycle: 2 groups (command breaks chain) (got ' + cmdBreak.length + ')');
    ok(cmdBreak[0].name === 'ui.task_command' && cmdBreak[0].content === '执行命令: /task list', 'cmdBreak[0] is task_command (independent)');
    ok(cmdBreak[1].name === 'ui.task_lifecycle' && cmdBreak[1].content === '开始运行: t_aaa', 'cmdBreak[1] is lifecycle (not merged with command)');

    // 不合并：非 task 消息（assistant/user dashboard）断开合并链
    const breakChain = g([
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
      { id: 'a1', role: 'assistant', content: '处理中' },
      { id: 'w2', role: 'user', source: 'task', content: 'work task t_bbb' },
    ]);
    ok(breakChain.length === 3, 'work + assistant + work: 3 groups (assistant breaks chain) (got ' + breakChain.length + ')');
    ok(breakChain[0]._mergedTaskStatus === undefined && breakChain[2]._mergedTaskStatus === undefined, 'non-adjacent work tasks not merged');

    // 三方合并：lifecycle + work task + judge task 相邻 -> 一个卡片，多行拼接
    const tripleMerge = g([
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
      { id: 'j1', role: 'user', source: 'task', content: 'judge task t_aaa: goal?' },
    ]);
    ok(tripleMerge.length === 1 && tripleMerge[0]._mergedTaskStatus === true, 'lifecycle + work + judge: 1 merged group');
    ok(tripleMerge[0].content === '开始运行: t_aaa\n查询状态: work task t_aaa\n判断结束: judge task t_aaa: goal?', 'triple merge content (got ' + JSON.stringify(tripleMerge[0].content) + ')');

    // === 按类型分组：同类型相邻合并，不同类型断开（任务指令 vs 任务消息）===
    // 2 条相邻 ui.task_command 合并为一组（summary=任务指令，content 多行原文）
    const twoCmds = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create 报告' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：报告' },
    ]);
    ok(twoCmds.length === 1, '2 adjacent task_command: 1 merged group (got ' + twoCmds.length + ')');
    ok(twoCmds[0].name === 'ui.task_command', 'merged command group keeps name=ui.task_command (got ' + twoCmds[0].name + ')');
    ok(twoCmds[0].role === 'system', 'merged command group keeps role=system');
    ok(twoCmds[0].content === '执行命令: /task create 报告\n已创建任务 t_1：报告', 'merged command content = multi-line raw (got ' + JSON.stringify(twoCmds[0].content) + ')');
    ok(twoCmds[0]._mergedTaskStatus === undefined, 'merged command group: NO _mergedTaskStatus flag (got ' + JSON.stringify(twoCmds[0]._mergedTaskStatus) + ')');

    // 3 条相邻 ui.task_command 合并为一组
    const threeCmds = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create A' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：A' },
      { id: 'c3', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    ]);
    ok(threeCmds.length === 1, '3 adjacent task_command: 1 merged group (got ' + threeCmds.length + ')');
    ok(threeCmds[0].content === '执行命令: /task create A\n已创建任务 t_1：A\n执行命令: /task list', '3 merged commands content multi-line (got ' + JSON.stringify(threeCmds[0].content) + ')');
    ok(threeCmds[0].name === 'ui.task_command' && threeCmds[0]._mergedTaskStatus === undefined, '3 merged commands: name=ui.task_command, no _mergedTaskStatus');

    // ui.task_command + ui.task_lifecycle（无 card）相邻不合并（不同类型，2 张独立卡片）
    const cmdThenLifecycle = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
    ]);
    ok(cmdThenLifecycle.length === 2, 'task_command + lifecycle (no card): 2 groups, different types not merged (got ' + cmdThenLifecycle.length + ')');
    ok(cmdThenLifecycle[0].name === 'ui.task_command' && cmdThenLifecycle[0].content === '执行命令: /task list', 'cmdThenLifecycle[0] is task_command (single)');
    ok(cmdThenLifecycle[1].name === 'ui.task_lifecycle' && cmdThenLifecycle[1].content === '开始运行: t_aaa', 'cmdThenLifecycle[1] is lifecycle (single, not merged)');
    ok(cmdThenLifecycle[1]._mergedTaskStatus === undefined, 'single lifecycle after command: no _mergedTaskStatus flag');

    // work task + ui.task_command 相邻不合并（不同类型）
    const workThenCmd = g([
      { id: 'w1', role: 'user', source: 'task', content: 'work task t_aaa' },
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    ]);
    ok(workThenCmd.length === 2, 'work task + task_command: 2 groups, different types not merged (got ' + workThenCmd.length + ')');
    ok(workThenCmd[0].role === 'user' && workThenCmd[0].source === 'task' && workThenCmd[0].content === 'work task t_aaa', 'workThenCmd[0] is work task (single, raw content)');
    ok(workThenCmd[0]._mergedTaskStatus === undefined, 'single work task before command: no _mergedTaskStatus flag');
    ok(workThenCmd[1].name === 'ui.task_command' && workThenCmd[1].content === '执行命令: /task list', 'workThenCmd[1] is task_command (single)');

    // command + lifecycle + command: 3 groups（不同类型断开）
    const cmdLifecycleCmd = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create A' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
    ]);
    ok(cmdLifecycleCmd.length === 3, 'command + lifecycle + command: 3 groups (different types break chain) (got ' + cmdLifecycleCmd.length + ')');
    ok(cmdLifecycleCmd[0].name === 'ui.task_command' && cmdLifecycleCmd[0].content === '执行命令: /task create A', 'cmdLifecycleCmd[0] is single command');
    ok(cmdLifecycleCmd[1].name === 'ui.task_lifecycle' && cmdLifecycleCmd[1].content === '开始运行: t_aaa', 'cmdLifecycleCmd[1] is single lifecycle');
    ok(cmdLifecycleCmd[2].name === 'ui.task_command' && cmdLifecycleCmd[2].content === '执行命令: /task list', 'cmdLifecycleCmd[2] is single command');

    // lifecycle + command + lifecycle: 3 groups（不同类型断开）
    const lifecycleCmdLifecycle = g([
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task list' },
      { id: 'l2', role: 'system', name: 'ui.task_lifecycle', content: '已完成: t_aaa' },
    ]);
    ok(lifecycleCmdLifecycle.length === 3, 'lifecycle + command + lifecycle: 3 groups (got ' + lifecycleCmdLifecycle.length + ')');
    ok(lifecycleCmdLifecycle[0].name === 'ui.task_lifecycle' && lifecycleCmdLifecycle[1].name === 'ui.task_command' && lifecycleCmdLifecycle[2].name === 'ui.task_lifecycle', 'lifecycle+cmd+lifecycle: name sequence preserved');

    // 2 条相邻 ui.task_command + lifecycle（无 card）：command 合并 + lifecycle 独立 = 2 组
    const twoCmdsThenLifecycle = g([
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create A' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：A' },
      { id: 'l1', role: 'system', name: 'ui.task_lifecycle', content: '开始运行: t_aaa' },
    ]);
    ok(twoCmdsThenLifecycle.length === 2, '2 commands + lifecycle: 2 groups (commands merged + lifecycle single) (got ' + twoCmdsThenLifecycle.length + ')');
    ok(twoCmdsThenLifecycle[0].name === 'ui.task_command' && twoCmdsThenLifecycle[0].content === '执行命令: /task create A\n已创建任务 t_1：A', 'twoCmdsThenLifecycle[0] is merged commands');
    ok(twoCmdsThenLifecycle[1].name === 'ui.task_lifecycle' && twoCmdsThenLifecycle[1].content === '开始运行: t_aaa', 'twoCmdsThenLifecycle[1] is single lifecycle');

    // 渲染：merged command group -> summary=任务指令, pre=多行 content, open=true, className=msg system
    const mergedCmdEl = env7.chat.createMessageElement(twoCmds[0]);
    ok(mergedCmdEl.className === 'msg system', 'merged command card className = msg system (got ' + mergedCmdEl.className + ')');
    const mergedCmdDetails = mergedCmdEl._kids && mergedCmdEl._kids[0];
    ok(mergedCmdDetails && 'open' in mergedCmdDetails, 'merged command card is a details (collapsible)');
    ok(mergedCmdDetails && mergedCmdDetails.open === true, 'merged command card open=true (got open=' + (mergedCmdDetails && mergedCmdDetails.open) + ')');
    const mergedCmdSummary = mergedCmdDetails && mergedCmdDetails._kids && mergedCmdDetails._kids[0];
    ok(mergedCmdSummary && mergedCmdSummary.textContent === '任务指令', 'merged command card summary = 任务指令 (got ' + JSON.stringify(mergedCmdSummary && mergedCmdSummary.textContent) + ')');
    const mergedCmdPre = mergedCmdDetails && mergedCmdDetails._kids && mergedCmdDetails._kids[1];
    ok(mergedCmdPre && mergedCmdPre.textContent === '执行命令: /task create 报告\n已创建任务 t_1：报告', 'merged command card pre = multi-line content (got ' + JSON.stringify(mergedCmdPre && mergedCmdPre.textContent) + ')');
    ok(mergedCmdEl.dataset && mergedCmdEl.dataset.name === 'ui.task_command', 'merged command card dataset.name = ui.task_command (got ' + JSON.stringify(mergedCmdEl.dataset && mergedCmdEl.dataset.name) + ')');

    // 端到端：2 条 command 经 groupTaskMessages + createMessageElement 渲染为 1 张任务指令卡片
    const e2eCmdMessages = [
      { id: 'c1', role: 'system', name: 'ui.task_command', content: '执行命令: /task create 报告' },
      { id: 'c2', role: 'system', name: 'ui.task_command', content: '已创建任务 t_1：报告' },
    ];
    const groupedCmdE2E = env7.chat.groupTaskMessages(e2eCmdMessages);
    ok(groupedCmdE2E.length === 1, 'e2e: 2 commands merged into 1 group (got ' + groupedCmdE2E.length + ')');
    const e2eCmdEl = env7.chat.createMessageElement(groupedCmdE2E[0]);
    ok(e2eCmdEl.className === 'msg system', 'e2e merged command card className = msg system (got ' + e2eCmdEl.className + ')');
    const e2eCmdDetails = e2eCmdEl._kids && e2eCmdEl._kids[0];
    ok(e2eCmdDetails && e2eCmdDetails.open === true, 'e2e merged command card open=true');
    const e2eCmdSummary = e2eCmdDetails && e2eCmdDetails._kids && e2eCmdDetails._kids[0];
    ok(e2eCmdSummary && e2eCmdSummary.textContent === '任务指令', 'e2e merged command card summary = 任务指令');
    const e2eCmdPre = e2eCmdDetails && e2eCmdDetails._kids && e2eCmdDetails._kids[1];
    ok(e2eCmdPre && e2eCmdPre.textContent === '执行命令: /task create 报告\n已创建任务 t_1：报告', 'e2e merged command card pre = multi-line content (got ' + JSON.stringify(e2eCmdPre && e2eCmdPre.textContent) + ')');

    // --- T7: waiting_approval active: approve/reject/revise/cancel + textarea + label ---
    let env8 = loadChat();
    env8.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    await env8.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准', card: VC },
    ]));
    await env8.waitMicro();
    const card8 = findByClass(env8.messageStack, 'task-card')[0];
    ok(!!card8, 'waiting_approval active card rendered');
    ok(hasButton(card8, 'approve'), 'waiting_approval: approve button');
    ok(hasButton(card8, 'reject'), 'waiting_approval: reject button');
    ok(hasButton(card8, 'revise'), 'waiting_approval: revise button');
    ok(hasButton(card8, 'cancel'), 'waiting_approval: cancel button');
    const ta8 = findByClass(card8, 'task-card__textarea');
    ok(ta8.length === 1, 'waiting_approval: 1 textarea');
    const labels8 = findByClass(card8, 'task-card__label');
    ok(labels8.length === 1 && ta8[0].id && labels8[0].htmlFor === ta8[0].id, 'textarea associated with label via for/id');
    const feedback8 = findByClass(card8, 'task-card__feedback');
    ok(feedback8.length === 1, 'feedback node exists from creation');
    const details8 = card8._kids.find((k) => k && 'open' in k);
    ok(details8 && details8.open === true, 'card details open by default');
    const btns8 = findByClass(card8, 'task-card__btn');
    ok(btns8.length === 4 && btns8.every((b) => b.type === 'button'), 'all buttons are native type=button');

    // --- T7: failed active: terminal status, no actions ---
    let env9 = loadChat();
    env9.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'failed' });
    await env9.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'failed', card: { ...VC, status: 'failed', available_actions: ['retry', 'cancel'] } },
    ]));
    await env9.waitMicro();
    const card9 = findByClass(env9.messageStack, 'task-card')[0];
    ok(findByClass(card9, 'task-card__btn').length === 0, 'failed: no action buttons');
    ok(findByClass(card9, 'task-card__textarea').length === 0, 'failed: no textarea');

    // --- T7: expired active: terminal status, no actions ---
    let env10 = loadChat();
    env10.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'expired' });
    await env10.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'expired', card: { ...VC, status: 'expired', available_actions: ['retry'] } },
    ]));
    await env10.waitMicro();
    const card10 = findByClass(env10.messageStack, 'task-card')[0];
    ok(findByClass(card10, 'task-card__btn').length === 0, 'expired: no action buttons');

    // --- T7: approval card (interaction_type=approval): 2 buttons (批准/拒绝), NO textarea ---
    let envAppr = loadChat();
    envAppr.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    await envAppr.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['approve', 'reject'], interaction_type: 'approval' } },
    ]));
    await envAppr.waitMicro();
    const cardAppr = findByClass(envAppr.messageStack, 'task-card')[0];
    ok(!!cardAppr, 'approval card rendered');
    ok(hasButton(cardAppr, 'approve') && hasButton(cardAppr, 'reject'), 'approval: 批准+拒绝 buttons');
    ok(!hasButton(cardAppr, 'revise') && !hasButton(cardAppr, 'cancel'), 'approval: no revise/cancel');
    ok(findByClass(cardAppr, 'task-card__textarea').length === 0, 'approval: no textarea');
    ok(findByClass(cardAppr, 'task-card__btn').length === 2, 'approval: exactly 2 buttons (got ' + findByClass(cardAppr, 'task-card__btn').length + ')');

    // --- T7: intent_request card (interaction_type=intent_request): 2 buttons (补充并继续/取消) + textarea ---
    let envIR = loadChat();
    envIR.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    await envIR.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: 't_1', status: 'waiting_approval', title: 'T', summary: 'S', available_actions: ['revise', 'cancel'], interaction_type: 'intent_request' } },
    ]));
    await envIR.waitMicro();
    const cardIR = findByClass(envIR.messageStack, 'task-card')[0];
    ok(!!cardIR, 'intent_request card rendered');
    ok(hasButton(cardIR, 'revise') && hasButton(cardIR, 'cancel'), 'intent_request: 补充并继续+取消 buttons');
    ok(!hasButton(cardIR, 'approve') && !hasButton(cardIR, 'reject'), 'intent_request: no approve/reject');
    ok(findByClass(cardIR, 'task-card__textarea').length === 1, 'intent_request: 1 textarea (for revise)');
    ok(findByClass(cardIR, 'task-card__btn').length === 2, 'intent_request: exactly 2 buttons (got ' + findByClass(cardIR, 'task-card__btn').length + ')');
    // textarea associated with label
    const taIR = findByClass(cardIR, 'task-card__textarea')[0];
    const labelsIR = findByClass(cardIR, 'task-card__label');
    ok(labelsIR.length === 1 && taIR.id && labelsIR[0].htmlFor === taIR.id, 'intent_request: textarea labeled');

    // --- T7: stale/unavailable: no buttons or textarea created ---
    let env11 = loadChat();
    env11.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env11.waitMicro();
    const card11 = findByClass(env11.messageStack, 'task-card')[0];
    ok(findByClass(card11, 'task-card__btn').length === 0, 'stale: no buttons created (not disabled-after-render)');
    ok(findByClass(card11, 'task-card__textarea').length === 0, 'stale: no textarea created');
    ok(card11.textContent.indexOf('T') !== -1 && card11.textContent.indexOf('t_1') !== -1, 'stale still shows title/task_id');
    // stale no-receipt: concise fallback replaces the old generic prompt.
    ok(card11.textContent.indexOf('任务状态已变更') !== -1, 'stale no-receipt: 任务状态已变更 fallback shown');
    ok(card11.textContent.indexOf('操作不可用') === -1, 'stale no-receipt: 操作不可用 dropped (replaced)');

    // --- T7: stale card surfaces recorded decision from session receipt ---
    let env11b = loadChat();
    env11b.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11b.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准', card: VC },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '已批准: t_1 - T', card: null },
    ]));
    await env11b.waitMicro();
    const card11b = findByClass(env11b.messageStack, 'task-card')[0];
    ok(!!card11b, 'stale+receipt: card rendered');
    ok(findByClass(card11b, 'task-card__btn').length === 0, 'stale+receipt: no buttons');
    ok(card11b.textContent.indexOf('已批准') !== -1, 'stale+receipt: 已批准 decision shown (got ' + JSON.stringify(card11b.textContent) + ')');
    ok(card11b.textContent.indexOf('任务状态已变更，操作不可用') === -1, 'stale+receipt: generic prompt replaced');

    // revise receipt (intent-supplement) surfaces 已修订 on a stale card.
    let env11c = loadChat();
    env11c.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11c.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: '等待批准', card: VC },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '已修订: t_1 - T | 修订指示: use plan C', card: null },
    ]));
    await env11c.waitMicro();
    const card11c = findByClass(env11c.messageStack, 'task-card')[0];
    ok(card11c.textContent.indexOf('已修订') !== -1, 'stale+receipt: 已修订 (intent-supplement) shown (got ' + JSON.stringify(card11c.textContent) + ')');

    // decision is scoped to waiting_approval: a stale failed card must NOT
    // surface an approve receipt (the decision belongs to a pending-approval task).
    let env11d = loadChat();
    env11d.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'queued' });
    await env11d.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'failed', card: { ...VC, status: 'failed', available_actions: ['retry', 'cancel'] } },
      { id: 'm2', role: 'system', name: 'ui.task_lifecycle', content: '已批准: t_1 - T', card: null },
    ]));
    await env11d.waitMicro();
    const card11d = findByClass(env11d.messageStack, 'task-card')[0];
    ok(card11d.textContent.indexOf('已批准') === -1, 'stale failed card: no approve decision (scoped to waiting_approval) (got ' + JSON.stringify(card11d.textContent) + ')');
    ok(card11d.textContent.indexOf('任务状态已变更') !== -1, 'stale failed card: 任务状态已变更 fallback shown');

    // --- T7: safe rendering (textContent only, no innerHTML) ---
    let env13 = loadChat();
    env13.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    const XSS = '<img src=x onerror=alert(1)>';
    await env13.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting',
        card: { schema_version: 1, kind: 'task_lifecycle', task_id: XSS, status: 'waiting_approval', title: XSS, summary: XSS, available_actions: ['approve'] } },
    ]));
    await env13.waitMicro();
    const allDescs = findDescendants(env13.messageStack, () => true);
    ok(!allDescs.some((n) => n.tagName === 'IMG'), 'XSS: no IMG element created (textContent only)');
    ok(env13.messageStack.textContent.indexOf(XSS) !== -1, 'XSS: text preserved as text');

    // --- T7: action dispatch + in-flight dedup ---
    let env14 = loadChat();
    const aCalls = [];
    env14.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env14.win.NAGENT.api.task.approve = (id) => { aCalls.push({ name: 'approve', id }); return Promise.resolve({}); };
    env14.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', []));
    await env14.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env14.waitMicro();
    const card14 = findByClass(env14.messageStack, 'task-card')[0];
    const approveBtn14 = findByClass(card14, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn14.click();
    await env14.waitMicro();
    ok(aCalls.length === 1 && aCalls[0].name === 'approve' && aCalls[0].id === 't_1', 'approve click dispatched with task_id (got ' + JSON.stringify(aCalls) + ')');
    // settled: buttons removed, 操作已提交 shown (refresh returns empty -> re-render -> card14 gone, so check before refresh)
    // Actually refresh re-renders with empty messages, removing the card. So check the feedback text right after click+await.
    // The handler sets settled BEFORE refresh, so check card14 feedback before refresh completes.
    // But refresh is awaited inside the handler, so by the time we get here refresh is done and card14 is gone.
    // Instead, make refresh fail to keep card14 in DOM.

    // reject reports its concrete result instead of the generic submission text.
    let env14b = loadChat();
    env14b.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env14b.win.NAGENT.api.task.reject = () => Promise.resolve({});
    await env14b.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env14b.waitMicro();
    const card14b = findByClass(env14b.messageStack, 'task-card')[0];
    const rejectBtn14b = findByClass(card14b, 'task-card__btn').find((b) => b.dataset.action === 'reject');
    rejectBtn14b.click();
    await env14b.waitMicro();
    ok(card14b.textContent.indexOf('已拒绝') !== -1, 'reject success: 已拒绝 shown');

    // --- success -> settled -> refresh fails -> "已批准，刷新失败" ---
    // Uses selectSession to set currentSessionId so the handler's refresh path runs.
    let env15 = loadChat();
    env15.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env15.win.NAGENT.api.task.approve = () => Promise.resolve({});
    sessionsList = [{ id: 's1', title: 's1' }];
    env15.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    env15.chat.init();
    await env15.waitMicro();
    env15.fireClick(env15.findSessionItem('s1'));
    await env15.waitMicro();
    await env15.waitMicro();
    // currentSessionId is now 's1'; switch getSessionDetail to reject so refresh fails.
    env15.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    const card15 = findByClass(env15.messageStack, 'task-card')[0];
    const approveBtn15 = findByClass(card15, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn15.click();
    await env15.waitMicro();
    await env15.waitMicro();
    ok(findByClass(card15, 'task-card__btn').length === 0, 'success: buttons removed (settled)');
    ok(card15.textContent.indexOf('已批准') !== -1, 'approve success: 已批准 shown');
    ok(card15.textContent.indexOf('刷新失败') !== -1, 'refresh failure: 刷新失败 shown');

    // --- in-flight dedup: second click produces no second request ---
    let env16 = loadChat();
    let resolveApprove16;
    let approveCount16 = 0;
    env16.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env16.win.NAGENT.api.task.approve = () => { approveCount16++; return new Promise((res) => { resolveApprove16 = res; }); };
    env16.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', []));
    await env16.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env16.waitMicro();
    const card16 = findByClass(env16.messageStack, 'task-card')[0];
    const approveBtn16 = findByClass(card16, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn16.click();
    await env16.waitMicro();
    ok(approveCount16 === 1, 'first click: 1 approve call');
    const btns16mid = findByClass(card16, 'task-card__btn');
    ok(btns16mid.every((b) => b.disabled === true), 'in-flight: all buttons disabled');
    approveBtn16.click();
    await env16.waitMicro();
    ok(approveCount16 === 1, 'second click during in-flight: no new call (got ' + approveCount16 + ')');
    resolveApprove16({});
    await env16.waitMicro();
    await env16.waitMicro();

    // --- revise Unicode code-point validation ---
    let env17 = loadChat();
    const reviseCalls = [];
    env17.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env17.win.NAGENT.api.task.revise = (id, note) => { reviseCalls.push({ id, note, len: Array.from(note).length }); return Promise.resolve({}); };
    env17.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    await env17.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env17.waitMicro();
    const card17 = findByClass(env17.messageStack, 'task-card')[0];
    const reviseBtn17 = findByClass(card17, 'task-card__btn').find((b) => b.dataset.action === 'revise');
    const ta17 = findByClass(card17, 'task-card__textarea')[0];

    ta17.value = '   ';
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 0, 'revise empty trim -> rejected, no api call');
    ok(card17.textContent.indexOf('不能为空') !== -1, 'revise empty feedback shown');

    ta17.value = 'a';
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 1 && reviseCalls[0].len === 1, 'revise 1 code point accepted');
    ok(card17.textContent.indexOf('补充: a') !== -1, 'revise success: entered intent shown');

    ta17.value = 'a'.repeat(2000);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 2 && reviseCalls[1].len === 2000, 'revise 2000 BMP code points accepted');

    ta17.value = 'a'.repeat(2001);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 2, 'revise 2001 BMP code points rejected');

    const ASTRAL = '𝕏';
    ok(Array.from(ASTRAL).length === 1 && ASTRAL.length === 2, 'astral char sanity: 1 code point = 2 UTF-16 units');
    ta17.value = ASTRAL.repeat(2001);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 2, 'revise 2001 astral code points rejected');

    ta17.value = ASTRAL.repeat(2000);
    reviseBtn17.click();
    await env17.waitMicro();
    ok(reviseCalls.length === 3 && reviseCalls[2].len === 2000, 'revise 2000 astral code points accepted');

    // --- task_state_invalid -> stale, buttons removed ---
    let env18 = loadChat();
    env18.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env18.win.NAGENT.api.task.approve = () => Promise.reject(new Error('task_state_invalid'));
    sessionsList = [{ id: 's1', title: 's1' }];
    env18.win.NAGENT.api.getSessionDetail = () => Promise.resolve(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    env18.chat.init();
    await env18.waitMicro();
    env18.fireClick(env18.findSessionItem('s1'));
    await env18.waitMicro();
    await env18.waitMicro();
    env18.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    const card18 = findByClass(env18.messageStack, 'task-card')[0];
    const approveBtn18 = findByClass(card18, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn18.click();
    await env18.waitMicro();
    await env18.waitMicro();
    ok(findByClass(card18, 'task-card__btn').length === 0, 'task_state_invalid -> stale, buttons removed');

    // --- task_invalid -> controls restored ---
    let env19 = loadChat();
    env19.win.NAGENT.api.task.get = () => Promise.resolve({ id: 't_1', status: 'waiting_approval' });
    env19.win.NAGENT.api.task.approve = () => Promise.reject(new Error('task_invalid'));
    env19.win.NAGENT.api.getSessionDetail = () => Promise.reject(new Error('network'));
    await env19.chat.applySessionDetail(detailWith('s1', [
      { id: 'm1', role: 'system', name: 'ui.task_lifecycle', content: 'waiting', card: VC },
    ]));
    await env19.waitMicro();
    const card19 = findByClass(env19.messageStack, 'task-card')[0];
    const approveBtn19 = findByClass(card19, 'task-card__btn').find((b) => b.dataset.action === 'approve');
    approveBtn19.click();
    await env19.waitMicro();
    await env19.waitMicro();
    const btns19 = findByClass(card19, 'task-card__btn');
    ok(btns19.length > 0 && btns19.every((b) => !b.disabled), 'task_invalid -> controls restored, not disabled');
    ok(card19.textContent.indexOf('task_invalid') !== -1, 'task_invalid shows error code');

    await env1.waitMicro();
    ok(unhandledCount === 0, 'no unhandledRejection (got ' + unhandledCount + ', reasons=' + JSON.stringify(unhandledReasons.map((r) => r && r.message)) + ')');
  } finally {
    process.removeListener('unhandledRejection', unhandledHandler);
  }
}

// ===========================================================================
// 调试设置按会话独立：spec line 501 — 设置只针对一个会话生效、而不是对所有会话。
// 默认 task=on/tool=off；空态勾选的 draft 在首轮发送后转入新会话；两会话各自独立、
// 切走再切回保留各自设置。getDebugSettings/setDebugSettings 暴露数据模型供断言。
// ===========================================================================
async function testDebugSettingsPerSession() {
  const detailWith = (id, msgs) => ({ session: { id }, messages: msgs, summary: null, task_state: null });

  // --- 空态默认值 ---
  let env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const defaults = env.chat.getDebugSettings();
  ok(defaults.task === true && defaults.tool === false, 'empty state defaults: task on, tool off (got ' + JSON.stringify(defaults) + ')');

  // --- 空态勾选 tool 写入 draft；首轮发送后 draft 转入新会话 ---
  env.chat.setDebugSettings({ task: true, tool: true });
  ok(env.chat.getDebugSettings().tool === true, 'draft toggle: tool visible in empty state');
  env.input.value = 'hello';
  await env.chat.send(); // ensureSession 创建会话并转入 draft
  await env.waitMicro();
  const afterCreate = env.chat.getDebugSettings();
  ok(afterCreate.tool === true, 'draft transferred to new session on first send (got ' + JSON.stringify(afterCreate) + ')');

  // --- 两会话独立：s1 设 tool=true，切到 s2 应回落默认（不受 s1 影响）；切回 s1 保留 ---
  env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }, { id: 's2', title: 's2' }];
  env.win.NAGENT.api.getSessionDetail = (sid) => Promise.resolve(detailWith(sid, []));
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  env.chat.setDebugSettings({ task: true, tool: true });
  ok(env.chat.getDebugSettings().tool === true, 's1: tool set to visible');
  env.fireClick(env.findSessionItem('s2'));
  await env.waitMicro();
  const s2 = env.chat.getDebugSettings();
  ok(s2.tool === false, 's2 independent: tool reverts to default, not inherited from s1 (got ' + JSON.stringify(s2) + ')');
  env.chat.setDebugSettings({ task: false, tool: false });
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  const s1again = env.chat.getDebugSettings();
  ok(s1again.tool === true && s1again.task === true, 's1 retains its own settings after switching away and back (got ' + JSON.stringify(s1again) + ')');
}

// ===========================================================================
// T4: 通用工具确认卡片 (generic tool approval card)
// 覆盖：跨 chunk / CRLF 的 SSE 事件识别、有界缓冲、通用卡片渲染与文本转义、
//       三个选择请求、session 捕获、按钮去重、并发卡片、404/409 终态、
//       网络/5xx 重试、流结束/会话切换后禁止提交、敏感字段不渲染。
// ===========================================================================
async function testToolApprovalCard() {
  function findDescendants(el, predicate) {
    const out = [];
    function walk(node) {
      if (!node || !node._kids) return;
      for (const k of node._kids) {
        if (predicate(k)) out.push(k);
        walk(k);
      }
    }
    walk(el);
    return out;
  }
  const findByClass = (el, cls) => findDescendants(el, (n) => {
    if (!n || typeof n.className !== 'string') return false;
    return n.className.split(/\s+/).indexOf(cls) !== -1;
  });
  const approvalEnvelope = (id, opts) => {
    opts = opts || {};
    return 'data: ' + JSON.stringify({
      object: 'n-agent.tool_approval',
      approval: {
        confirmation_id: id,
        tool_name: opts.tool_name || 'browser.click',
        description: opts.description || '点击页面按钮',
        arguments_summary: opts.arguments_summary || '{"selector":"#btn"}',
        expires_at: opts.expires_at || '2026-07-28T12:00:00Z',
      },
    }) + (opts.sep || '\n\n');
  };

  let unhandledCount = 0;
  const unhandledReasons = [];
  const unhandledHandler = (reason) => { unhandledCount++; unhandledReasons.push(reason); };
  process.on('unhandledRejection', unhandledHandler);

  try {
    // --- SSE parser: cross-chunk event recognition ---
    let env = loadChat();
    ok(typeof env.chat.createSSEParser === 'function', 'createSSEParser exposed');
    const parser = env.chat.createSSEParser();
    const full = approvalEnvelope('c1');
    const part1 = full.slice(0, 23);
    const part2 = full.slice(23);
    const ev1 = parser.feed(part1);
    ok(ev1.length === 0, 'cross-chunk: first half produces no events (got ' + ev1.length + ')');
    const ev2 = parser.feed(part2);
    ok(ev2.length === 1, 'cross-chunk: second half completes event (got ' + ev2.length + ')');
    let j = JSON.parse(ev2[0]);
    ok(j.object === 'n-agent.tool_approval' && j.approval.confirmation_id === 'c1', 'cross-chunk: event parsed correctly');

    // --- SSE parser: CRLF line endings (\r\n\r\n boundary) ---
    const parser2 = env.chat.createSSEParser();
    const crlf = approvalEnvelope('c2', { sep: '\r\n\r\n' });
    const crlfEvents = parser2.feed(crlf);
    ok(crlfEvents.length === 1, 'CRLF boundary: event recognized (got ' + crlfEvents.length + ')');
    j = JSON.parse(crlfEvents[0]);
    ok(j.approval.confirmation_id === 'c2', 'CRLF: event parsed correctly');

    // --- SSE parser: CRLF split across chunks ---
    const parser2b = env.chat.createSSEParser();
    const crlf2 = approvalEnvelope('c2b', { sep: '\r\n\r\n' });
    const mid = Math.floor(crlf2.length / 2);
    ok(parser2b.feed(crlf2.slice(0, mid)).length === 0, 'CRLF cross-chunk: first half no events');
    ok(parser2b.feed(crlf2.slice(mid)).length === 1, 'CRLF cross-chunk: second half completes event');

    // --- SSE parser: multi-line data: fields concatenated with \n ---
    const parser3 = env.chat.createSSEParser();
    const multiData = 'data: line1\ndata: line2\n\n';
    const multiEvents = parser3.feed(multiData);
    ok(multiEvents.length === 1 && multiEvents[0] === 'line1\nline2', 'multi-line data: concatenated with \\n (got ' + JSON.stringify(multiEvents[0]) + ')');

    // --- SSE parser: bounded buffer (drop if > 1 MiB) ---
    const parser4 = env.chat.createSSEParser();
    ok(parser4.getBufferLength() === 0, 'parser starts with empty buffer');
    // Feed a huge incomplete event (> 1 MiB, no boundary)
    parser4.feed('x'.repeat(700000));
    ok(parser4.getBufferLength() > 0, 'parser buffers incomplete event');
    parser4.feed('y'.repeat(700000));
    ok(parser4.getBufferLength() === 0, 'parser drops buffer beyond 1 MiB (got ' + parser4.getBufferLength() + ')');

    // --- SSE parser: [DONE] sentinel ---
    const parser5 = env.chat.createSSEParser();
    ok(parser5.feed('data: [DONE]\n\n')[0] === '[DONE]', '[DONE] sentinel recognized');

    // --- isValidApprovalPayload ---
    ok(typeof env.chat.isValidApprovalPayload === 'function', 'isValidApprovalPayload exposed');
    ok(env.chat.isValidApprovalPayload({ confirmation_id: 'c', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e' }) === true, 'valid approval passes');
    ok(env.chat.isValidApprovalPayload(null) === false, 'null approval invalid');
    ok(env.chat.isValidApprovalPayload({}) === false, 'empty approval invalid');
    ok(env.chat.isValidApprovalPayload({ confirmation_id: 'c', tool_name: 't' }) === false, 'missing fields invalid');
    ok(env.chat.isValidApprovalPayload({ confirmation_id: '', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e' }) === false, 'empty confirmation_id invalid');
    ok(env.chat.isValidApprovalPayload({ confirmation_id: 'c', tool_name: 42, description: 'd', arguments_summary: 'a', expires_at: 'e' }) === false, 'non-string field invalid');
    ok(env.chat.isValidApprovalPayload({ confirmation_id: 'c', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e', extra: 'x' }) === true, 'extra fields OK (only required checked)');

    // --- Card rendering + text escaping (XSS) ---
    env = loadChat();
    ok(typeof env.chat.renderToolApprovalCard === 'function', 'renderToolApprovalCard exposed');
    const container = env.win.document.createElement('div');
    const xss = '<img src=x onerror=alert(1)>';
    env.chat.renderToolApprovalCard(container, {
      confirmation_id: 'c3', tool_name: xss, description: xss, arguments_summary: xss, expires_at: xss,
    }, 'sess-3');
    const cards3 = findByClass(container, 'tool-approval-card');
    ok(cards3.length === 1, 'card rendered (got ' + cards3.length + ')');
    ok(cards3[0].dataset.confirmationId === 'c3', 'card data-confirmation-id set');

    // Refresh path: the server returns this card as a persisted session message,
    // so no live SSE approval envelope is needed to reconstruct it.
    const persisted = env.chat.createMessageElement({
      role: 'system', name: 'ui.tool_approval', content: '工具操作等待确认',
      card: { kind: 'tool_approval', approval: {
        confirmation_id: 'persisted-c3', tool_name: 'browser.click',
        description: '打开文章', arguments_summary: '{}', expires_at: '2030-01-01T00:00:00Z',
      } },
    });
    ok(findByClass(persisted, 'tool-approval-card').length === 1,
      'persisted approval card renders from session history');
    ok(typeof env.chat.resolveToolApprovalDecisions === 'function',
      'approval resolution reader exposed');
    const approvalDecisions = env.chat.resolveToolApprovalDecisions([
      { role: 'system', name: 'ui.tool_approval_resolution', card: {
        kind: 'tool_approval_resolution', confirmation_id: 'persisted-c3', status: 'approved',
      } },
    ]);
    ok(approvalDecisions.get('persisted-c3').status === 'approved',
      'persisted approval result is indexed by confirmation id');
    const resolvedPersisted = env.chat.createMessageElement({
      role: 'system', name: 'ui.tool_approval', content: '工具操作等待确认',
      card: { kind: 'tool_approval', approval: {
        confirmation_id: 'persisted-c3', tool_name: 'browser.click',
        description: '打开文章', arguments_summary: '{}', expires_at: '2030-01-01T00:00:00Z',
      } },
    }, undefined, undefined, approvalDecisions);
    const resolvedButtons = findByClass(resolvedPersisted, 'tool-approval-card__btn');
    ok(resolvedButtons.length === 3 && resolvedButtons.every((button) => button.disabled),
      'approved persisted card disables all three actions after refresh');
    // A resolution without scope (legacy / rejected) collapses to bare 已批准.
    ok(resolvedPersisted.textContent.indexOf('已批准') !== -1,
      'approved card without scope shows bare 已批准');

    // --- Resolved card surfaces the trust scope after refresh ---
    // approved + session -> 已批准 · 信任本会话; approved + once -> 已批准 · 仅信任本次.
    // Assert against the feedback node (button labels like 信任本会话 also live in
    // the card text, so the whole-card text would be ambiguous).
    function approvalFeedback(card) {
      const nodes = findByClass(card, 'tool-approval-card__feedback');
      return nodes.length ? nodes[0].textContent : '';
    }
    for (const scopeCase of [['session', '信任本会话'], ['once', '仅信任本次']]) {
      const scope = scopeCase[0];
      const label = scopeCase[1];
      const decisions = env.chat.resolveToolApprovalDecisions([
        { role: 'system', name: 'ui.tool_approval_resolution', card: {
          kind: 'tool_approval_resolution', confirmation_id: 'scope-' + scope,
          status: 'approved', scope: scope,
        } },
      ]);
      ok(decisions.get('scope-' + scope).scope === scope,
        'resolveToolApprovalDecisions exposes scope=' + scope);
      const resolved = env.chat.createMessageElement({
        role: 'system', name: 'ui.tool_approval', content: '工具操作等待确认',
        card: { kind: 'tool_approval', approval: {
          confirmation_id: 'scope-' + scope, tool_name: 'browser.click',
          description: '打开文章', arguments_summary: '{}', expires_at: '2030-01-01T00:00:00Z',
        } },
      }, undefined, undefined, decisions);
      const fb = approvalFeedback(resolved);
      ok(fb === ('已批准 · ' + label),
        'resolved feedback is 已批准 · ' + label + ' for scope=' + scope + " (got '" + fb + "')");
    }
    // rejected -> 已拒绝, no scope label
    const rejectedDecisions = env.chat.resolveToolApprovalDecisions([
      { role: 'system', name: 'ui.tool_approval_resolution', card: {
        kind: 'tool_approval_resolution', confirmation_id: 'scope-rejected',
        status: 'rejected', scope: 'deny',
      } },
    ]);
    const rejectedCard = env.chat.createMessageElement({
      role: 'system', name: 'ui.tool_approval', content: '工具操作等待确认',
      card: { kind: 'tool_approval', approval: {
        confirmation_id: 'scope-rejected', tool_name: 'browser.click',
        description: '打开文章', arguments_summary: '{}', expires_at: '2030-01-01T00:00:00Z',
      } },
    }, undefined, undefined, rejectedDecisions);
    ok(approvalFeedback(rejectedCard) === '已拒绝',
      'rejected feedback is 已拒绝 without scope label');
    const allDescs = findDescendants(container, () => true);
    ok(!allDescs.some((n) => n.tagName === 'IMG' || n.tagName === 'SCRIPT'), 'XSS: no IMG/SCRIPT created (textContent only)');
    ok(container.textContent.indexOf(xss) !== -1, 'XSS: payload preserved as text');

    console.error("MARKER_TZ_TEST_REACHED");
    // --- expires_at rendered in UTC+8 (Asia/Shanghai), not raw UTC ---
    // 服务端 expires_at 为 UTC RFC 3339（trailing Z），前端须按项目规范转 UTC+8 展示。
    const tzCard = env.win.document.createElement('div');
    env.chat.renderToolApprovalCard(tzCard, {
      confirmation_id: 'tz1', tool_name: 'browser.click', description: '点击',
      arguments_summary: '{}', expires_at: '2026-07-28T12:00:00Z',
    }, 'sess-tz');
    const tzExpires = findByClass(findByClass(tzCard, 'tool-approval-card')[0], 'tool-approval-card__expires');
    ok(tzExpires[0].textContent === '过期时间: 2026-07-28 20:00:00',
      'expires_at rendered in UTC+8 (got "' + tzExpires[0].textContent + '")');
    // 非法日期回退为原文字符串（textContent 安全渲染，不使用 innerHTML）。
    const badCard = env.win.document.createElement('div');
    env.chat.renderToolApprovalCard(badCard, {
      confirmation_id: 'tz2', tool_name: 'browser.click', description: '点击',
      arguments_summary: '{}', expires_at: 'not-a-date',
    }, 'sess-tz2');
    const badExpires = findByClass(findByClass(badCard, 'tool-approval-card')[0], 'tool-approval-card__expires');
    ok(badExpires[0].textContent === '过期时间: not-a-date',
      'invalid expires_at falls back to raw text (got "' + badExpires[0].textContent + '")');

    // --- 旧 data-time 不再写入气泡；footer 的 UTC+8 实际渲染由通用 footer 场景覆盖 ---
    const tzMsg = env.chat.createMessageElement({ role: 'user', content: 'hi', created_at: '2020-01-01T00:00:00Z', name: null });
    ok(!tzMsg.dataset.time, 'message bubble does not carry legacy data-time');

    // Three buttons
    const btns3 = findByClass(cards3[0], 'tool-approval-card__btn');
    ok(btns3.length === 3, 'three choice buttons rendered (got ' + btns3.length + ')');
    ok(btns3.every((b) => b.type === 'button'), 'all buttons native type=button');
    const choices3 = btns3.map((b) => b.dataset.choice).sort();
    ok(JSON.stringify(choices3) === JSON.stringify(['cancel', 'once', 'trust_session'].sort()), 'three choices: once/trust_session/cancel (got ' + JSON.stringify(choices3) + ')');

    // --- Three choice buttons each POST correctly ---
    for (const choice of ['once', 'trust_session', 'cancel']) {
      env = loadChat();
      const c = env.win.document.createElement('div');
      env.setFetchHandler('/chat/tool-approvals/', () => Promise.resolve({ status: 204, ok: true, json: async () => ({}) }));
      env.chat.renderToolApprovalCard(c, {
        confirmation_id: 'c4-' + choice, tool_name: 'browser.type', description: '输入', arguments_summary: '{}', expires_at: 'e',
      }, 'sess-stream-4');
      const btns = findByClass(c, 'tool-approval-card__btn');
      const btn = btns.find((b) => b.dataset.choice === choice);
      btn.click();
      await env.waitMicro();
      const posts = fetchCalls.filter((f) => f.url && f.url.indexOf('/chat/tool-approvals/c4-' + choice) !== -1);
      ok(posts.length === 1, choice + ': POST to /chat/tool-approvals/c4-' + choice + ' (got ' + posts.length + ')');
      ok(posts[0] && posts[0].opts && posts[0].opts.method === 'POST', choice + ': POST method');
      ok(posts[0] && posts[0].opts.headers && posts[0].opts.headers['X-Session-ID'] === 'sess-stream-4', choice + ': X-Session-ID = stream session (sess-stream-4)');
      const body = posts[0] && JSON.parse(posts[0].opts.body);
      ok(body && body.choice === choice, choice + ': body choice correct (got ' + JSON.stringify(body) + ')');
    }

    // --- Session capture: POST uses streamSessionId, not currentSessionId ---
    env = loadChat();
    sessionsList = [{ id: 's-global', title: 's-global' }];
    env.win.NAGENT.api.getSessionDetail = () => Promise.resolve({ session: { id: 's-global' }, messages: [], summary: null, task_state: null });
    env.chat.init();
    await env.waitMicro();
    env.fireClick(env.findSessionItem('s-global'));
    await env.waitMicro();
    // currentSessionId is now 's-global'; render card with a DIFFERENT streamSessionId
    const cCap = env.win.document.createElement('div');
    env.setFetchHandler('/chat/tool-approvals/', () => Promise.resolve({ status: 204, ok: true, json: async () => ({}) }));
    env.chat.renderToolApprovalCard(cCap, {
      confirmation_id: 'c-cap', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-captured');
    findByClass(cCap, 'tool-approval-card__btn')[0].click();
    await env.waitMicro();
    const capPost = fetchCalls.find((f) => f.url && f.url.indexOf('/chat/tool-approvals/c-cap') !== -1);
    ok(capPost && capPost.opts.headers['X-Session-ID'] === 'sess-captured', 'session capture: POST uses stream session (sess-captured), not global (s-global)');

    // --- Button dedup: rapid clicks = 1 POST, all buttons disable on first click ---
    env = loadChat();
    const c5 = env.win.document.createElement('div');
    let resolvePost5;
    env.setFetchHandler('/chat/tool-approvals/', () => new Promise((res) => { resolvePost5 = res; }));
    env.chat.renderToolApprovalCard(c5, {
      confirmation_id: 'c5', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-5');
    const btns5 = findByClass(c5, 'tool-approval-card__btn');
    btns5[0].click();  // once
    btns5[0].click();  // dedup
    btns5[1].click();  // dedup (different button)
    await env.waitMicro();
    const posts5 = fetchCalls.filter((f) => f.url && f.url.indexOf('/chat/tool-approvals/c5') !== -1);
    ok(posts5.length === 1, 'dedup: rapid clicks produce 1 POST (got ' + posts5.length + ')');
    ok(btns5.every((b) => b.disabled === true), 'dedup: all buttons disabled on first click');
    resolvePost5({ status: 204, ok: true, json: async () => ({}) });
    await env.waitMicro();

    // --- Concurrent cards: two approvals in one stream render independent cards ---
    env = loadChat();
    const c6 = env.win.document.createElement('div');
    env.chat.renderToolApprovalCard(c6, {
      confirmation_id: 'c6a', tool_name: 'browser.click', description: 'd1', arguments_summary: 'a1', expires_at: 'e',
    }, 'sess-6');
    env.chat.renderToolApprovalCard(c6, {
      confirmation_id: 'c6b', tool_name: 'browser.type', description: 'd2', arguments_summary: 'a2', expires_at: 'e',
    }, 'sess-6');
    const cards6 = findByClass(c6, 'tool-approval-card');
    ok(cards6.length === 2, 'concurrent: two independent cards (got ' + cards6.length + ')');
    ok(cards6[0].dataset.confirmationId === 'c6a' && cards6[1].dataset.confirmationId === 'c6b', 'concurrent: distinct confirmation IDs');
    // Clicking one card does not disable the other card's buttons
    env.setFetchHandler('/chat/tool-approvals/', () => Promise.resolve({ status: 204, ok: true, json: async () => ({}) }));
    const btns6a = findByClass(cards6[0], 'tool-approval-card__btn');
    const btns6b = findByClass(cards6[1], 'tool-approval-card__btn');
    btns6a[0].click();
    await env.waitMicro();
    ok(btns6a.every((b) => b.disabled === true), 'concurrent: clicked card buttons disabled');
    ok(btns6b.every((b) => !b.disabled), 'concurrent: other card buttons still enabled');

    // --- Dedup: same confirmation_id not rendered twice ---
    env = loadChat();
    const c7 = env.win.document.createElement('div');
    env.chat.renderToolApprovalCard(c7, {
      confirmation_id: 'c7', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-7');
    env.chat.renderToolApprovalCard(c7, {
      confirmation_id: 'c7', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-7');
    ok(findByClass(c7, 'tool-approval-card').length === 1, 'dedup: same confirmation_id not rendered twice');

    // --- 404/409 -> terminal state (buttons disabled, no retry) ---
    for (const status of [404, 409]) {
      env = loadChat();
      const c = env.win.document.createElement('div');
      env.setFetchHandler('/chat/tool-approvals/', () => Promise.resolve({ status, ok: false, json: async () => ({ error: { code: 'x', message: 'm' } }) }));
      env.chat.renderToolApprovalCard(c, {
        confirmation_id: 'c-' + status, tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
      }, 'sess-' + status);
      const btns = findByClass(c, 'tool-approval-card__btn');
      btns[0].click();
      await env.waitMicro();
      await env.waitMicro();
      ok(btns.every((b) => b.disabled === true), status + ': buttons stay disabled (terminal)');
      ok(c.textContent.indexOf('已过期或已处理') !== -1, status + ': expired/processed message shown');
      // Retry click -> no new POST
      const postsBefore = fetchCalls.filter((f) => f.url && f.url.indexOf('/chat/tool-approvals/c-' + status) !== -1).length;
      btns[0].click();
      await env.waitMicro();
      const postsAfter = fetchCalls.filter((f) => f.url && f.url.indexOf('/chat/tool-approvals/c-' + status) !== -1).length;
      ok(postsAfter === postsBefore, status + ': no retry POST on re-click');
    }

    // --- 5xx -> buttons restored (retryable) ---
    env = loadChat();
    const c500 = env.win.document.createElement('div');
    env.setFetchHandler('/chat/tool-approvals/', () => Promise.resolve({ status: 500, ok: false, json: async () => ({}) }));
    env.chat.renderToolApprovalCard(c500, {
      confirmation_id: 'c500', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-500');
    const btns500 = findByClass(c500, 'tool-approval-card__btn');
    btns500[0].click();
    await env.waitMicro();
    await env.waitMicro();
    ok(btns500.every((b) => !b.disabled), '500: buttons restored for retry');
    ok(c500.textContent.indexOf('失败') !== -1 || c500.textContent.indexOf('重试') !== -1, '500: error message shown');
    // Retry: second click sends another POST
    const posts500Before = fetchCalls.filter((f) => f.url && f.url.indexOf('/chat/tool-approvals/c500') !== -1).length;
    btns500[0].click();
    await env.waitMicro();
    const posts500After = fetchCalls.filter((f) => f.url && f.url.indexOf('/chat/tool-approvals/c500') !== -1).length;
    ok(posts500After === posts500Before + 1, '500: retry sends another POST');

    // --- Network error -> buttons restored ---
    env = loadChat();
    const cNet = env.win.document.createElement('div');
    env.setFetchHandler('/chat/tool-approvals/', () => Promise.reject(new Error('network down')));
    env.chat.renderToolApprovalCard(cNet, {
      confirmation_id: 'cNet', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-net');
    const btnsNet = findByClass(cNet, 'tool-approval-card__btn');
    btnsNet[0].click();
    await env.waitMicro();
    await env.waitMicro();
    ok(btnsNet.every((b) => !b.disabled), 'network error: buttons restored for retry');
    ok(cNet.textContent.indexOf('网络') !== -1 || cNet.textContent.indexOf('重试') !== -1, 'network error: message shown');

    // --- 204 success: shows submitted choice, buttons stay disabled ---
    env = loadChat();
    const c204 = env.win.document.createElement('div');
    env.setFetchHandler('/chat/tool-approvals/', () => Promise.resolve({ status: 204, ok: true, json: async () => ({}) }));
    env.chat.renderToolApprovalCard(c204, {
      confirmation_id: 'c204', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-204');
    const btns204 = findByClass(c204, 'tool-approval-card__btn');
    btns204.find((b) => b.dataset.choice === 'once').click();
    await env.waitMicro();
    await env.waitMicro();
    ok(btns204.every((b) => b.disabled === true), '204: buttons stay disabled');
    ok(c204.textContent.indexOf('已提交') !== -1 && c204.textContent.indexOf('仅本次允许') !== -1, '204: submitted choice shown');

    // --- Stream end -> buttons disabled, no late POST ---
    env = loadChat();
    ok(typeof env.chat.disableApprovalCards === 'function', 'disableApprovalCards exposed');
    const cEnd = env.win.document.createElement('div');
    env.chat.renderToolApprovalCard(cEnd, {
      confirmation_id: 'cEnd', tool_name: 't', description: 'd', arguments_summary: 'a', expires_at: 'e',
    }, 'sess-end');
    env.chat.disableApprovalCards(cEnd);
    const btnsEnd = findByClass(cEnd, 'tool-approval-card__btn');
    ok(btnsEnd.every((b) => b.disabled === true), 'stream end: all buttons disabled');
    const postsBeforeEnd = fetchCalls.length;
    btnsEnd[0].click();
    await env.waitMicro();
    ok(fetchCalls.length === postsBeforeEnd, 'stream end: clicking disabled button makes no POST');

    // --- Sensitive fields: approval payload NOT written to session messages ---
    env = loadChat();
    const cPriv = env.win.document.createElement('div');
    env.chat.renderToolApprovalCard(cPriv, {
      confirmation_id: 'cPriv', tool_name: 'secret_tool', description: 'secret desc', arguments_summary: '{"password":"hunter2"}', expires_at: 'e',
    }, 'sess-priv');
    ok(appendCalls.length === 0, 'approval payload NOT persisted to session messages (got ' + appendCalls.length + ')');

    // --- Integration: send() with approval SSE envelope renders card during stream ---
    env = loadChat();
    sessionsList = [];
    env.win.NAGENT.api.getSessionDetail = () => Promise.resolve({ session: { id: 's-int' }, messages: [], summary: null, task_state: null });
    env.input.value = 'hello';
    const textChunk = 'data: ' + JSON.stringify({ choices: [{ delta: { content: 'hello' } }] }) + '\n\n';
    const approvalChunk = approvalEnvelope('c-int', { tool_name: 'browser.click', description: '点击', arguments_summary: '{}' });
    env.enqueueSseChunks([textChunk, approvalChunk]);
    env.setStreamKeepOpen();
    const sendPromise = env.chat.send();
    // Wait for send to process chunks (multiple microtask hops: ensureSession + fetch + reads)
    for (let i = 0; i < 10; i++) await env.waitMicro();
    const intCards = findByClass(env.messageStack, 'tool-approval-card');
    ok(intCards.length === 1, 'integration: card rendered during stream (got ' + intCards.length + ')');
    ok(intCards[0] && intCards[0].dataset.confirmationId === 'c-int', 'integration: card confirmation_id correct');
    // Text was streamed into the assistant bubble
    const assistantBubbles = findByClass(env.messageStack, 'assistant');
    ok(assistantBubbles.length >= 1 && assistantBubbles[0].textContent.indexOf('hello') !== -1, 'integration: text streamed to assistant bubble');
    // End stream -> card buttons disabled
    env.endStream();
    await sendPromise;
    // After stream end, card buttons should be disabled (card may be gone after refresh,
    // but if present, buttons must be disabled). Check the captured card reference.
    const intBtnsAfter = intCards[0] ? findByClass(intCards[0], 'tool-approval-card__btn') : [];
    ok(intBtnsAfter.every((b) => b.disabled === true), 'integration: card buttons disabled after stream end');

    // --- Integration: invalid approval payload terminates stream with error ---
    env = loadChat();
    sessionsList = [];
    env.win.NAGENT.api.getSessionDetail = () => Promise.resolve({ session: { id: 's-inv' }, messages: [], summary: null, task_state: null });
    env.input.value = 'hello';
    const invalidApproval = 'data: ' + JSON.stringify({ object: 'n-agent.tool_approval', approval: { confirmation_id: 'c-inv', tool_name: 't' } }) + '\n\n';
    const textAfter = 'data: ' + JSON.stringify({ choices: [{ delta: { content: 'should-not-appear' } }] }) + '\n\n';
    env.enqueueSseChunks([invalidApproval, textAfter]);
    await env.chat.send();
    // The key assertion: no card was rendered and no POST was made
    ok(!fetchCalls.some((f) => f.url && f.url.indexOf('/chat/tool-approvals/') !== -1), 'invalid approval: no POST made');

    // --- Integration: existing text streaming still works (regression) ---
    env = loadChat();
    sessionsList = [];
    env.win.NAGENT.api.getSessionDetail = () => Promise.resolve({ session: { id: 's-reg' }, messages: [], summary: null, task_state: null });
    env.input.value = '帮我写总结';
    env.enqueueSseChunks(['data: ' + JSON.stringify({ choices: [{ delta: { content: '回复' } }] }) + '\n\n']);
    await env.chat.send();
    ok(fetchCalls.some((f) => f.url === '/chat/completions'), 'regression: text streaming calls /chat/completions');
    ok(!fetchCalls.some((f) => f.url && f.url.indexOf('/chat/tool-approvals/') !== -1), 'regression: no approval POST for plain text');

    await env.waitMicro();
    ok(unhandledCount === 0, 'no unhandledRejection (got ' + unhandledCount + ', reasons=' + JSON.stringify(unhandledReasons.map((r) => r && r.message)) + ')');
  } finally {
    process.removeListener('unhandledRejection', unhandledHandler);
  }
}

// ===========================================================================
// setHeader: 会话 ID 后缀多视图链接（浏览器视图/任务/定时任务）
// ===========================================================================
(function testSetHeaderLinks() {
  const env = loadChat();
  const setHeader = env.chat.setHeader;
  ok(typeof setHeader === 'function', 'setHeader exposed');
  const header = env.byIdMap['chat-header'];
  function linkHrefs() {
    return (header._kids || []).filter((k) => k && k.tagName === 'A').map((a) => a.href);
  }
  setHeader(null);
  ok(header.textContent === 'N-Agent Chat', 'no id -> N-Agent Chat');
  setHeader('s1');
  ok(header.textContent === 's1', 'id only -> id, no links');
  ok(linkHrefs().length === 0, 'id only -> no links');
  setHeader('s1', { browser: true });
  ok(linkHrefs().length === 1 && linkHrefs()[0] === '/browser/session?nagent=s1', 'browser link');
  setHeader('s1', { browser: true, taskId: 't_9', scheduledTaskId: 'sched-1' });
  const hrefs = linkHrefs();
  ok(hrefs.length === 3 && hrefs[0] === '/browser/session?nagent=s1' && hrefs[1] === '/tasks/t_9' && hrefs[2] === '/scheduled-tasks/sched-1', 'all three links in order');
  setHeader('s1', { taskId: 't_x' });
  ok(linkHrefs().length === 1 && linkHrefs()[0] === '/tasks/t_x', 'task only link');
  // 空字符串 taskId/scheduledTaskId 视为无关联
  setHeader('s1', { taskId: '' });
  ok(linkHrefs().length === 0, 'empty taskId -> no link');
})();

// ===========================================================================
// loadSessionViewLinks: 按会话过滤任务看板/定时任务列表，取最新一条，失败静默降级
// ===========================================================================
async function testSessionViewLinks() {
  const emptyDetail = { session: { id: 's1' }, messages: [], summary: null, task_state: null };
  function headerHrefs(env) {
    return (env.byIdMap['chat-header']._kids || []).filter((k) => k && k.tagName === 'A').map((a) => a.href);
  }

  // 任务链接：origin_session_id 命中，取最新一条（t_new > t_old，t_other 属 s2 不命中）
  let env = loadChat();
  env.win.NAGENT.api.task.board = () => Promise.resolve({ columns: [
    { cards: [
      { id: 't_old', origin_session_id: 's1', created_at: '2026-07-01T00:00:00Z' },
      { id: 't_new', origin_session_id: 's1', created_at: '2026-07-31T00:00:00Z' },
      { id: 't_other', origin_session_id: 's2', created_at: '2026-08-01T00:00:00Z' },
    ] },
  ] });
  env.win.NAGENT.api.listScheduledTasks = () => Promise.resolve([]);
  await env.chat.loadSessionViewLinks('s1', emptyDetail);
  await env.waitMicro();
  env.chat.setHeader('s1', env.chat.buildHeaderLinks(emptyDetail, 's1'));
  const hrefs1 = headerHrefs(env);
  ok(hrefs1.indexOf('/tasks/t_new') !== -1 && hrefs1.indexOf('/tasks/t_old') === -1 && hrefs1.indexOf('/tasks/t_other') === -1, 'task link = latest by created_at (t_new), session-scoped');

  // execution_session_id 命中亦可
  env = loadChat();
  env.win.NAGENT.api.task.board = () => Promise.resolve({ columns: [
    { cards: [{ id: 't_exec', execution_session_id: 's1', created_at: '2026-07-31T00:00:00Z' }] },
  ] });
  env.win.NAGENT.api.listScheduledTasks = () => Promise.resolve([]);
  await env.chat.loadSessionViewLinks('s1', emptyDetail);
  await env.waitMicro();
  env.chat.setHeader('s1', env.chat.buildHeaderLinks(emptyDetail, 's1'));
  ok(headerHrefs(env).indexOf('/tasks/t_exec') !== -1, 'task link matches execution_session_id');

  // 定时任务链接：session_id 命中，取最新一条（sched_b > sched_a，sched_c 属 s2 不命中）
  env = loadChat();
  env.win.NAGENT.api.task.board = () => Promise.resolve({ columns: [] });
  env.win.NAGENT.api.listScheduledTasks = () => Promise.resolve([
    { id: 'sched_a', session_id: 's1', created_at: '2026-07-01T00:00:00Z' },
    { id: 'sched_b', session_id: 's1', created_at: '2026-07-31T00:00:00Z' },
    { id: 'sched_c', session_id: 's2', created_at: '2026-08-01T00:00:00Z' },
  ]);
  await env.chat.loadSessionViewLinks('s1', emptyDetail);
  await env.waitMicro();
  env.chat.setHeader('s1', env.chat.buildHeaderLinks(emptyDetail, 's1'));
  const hrefs2 = headerHrefs(env);
  ok(hrefs2.indexOf('/scheduled-tasks/sched_b') !== -1 && hrefs2.indexOf('/scheduled-tasks/sched_c') === -1, 'schedule link = latest by created_at (sched_b), session-scoped');

  // 无关联 -> 无链接
  env = loadChat();
  env.win.NAGENT.api.task.board = () => Promise.resolve({ columns: [{ cards: [{ id: 't_x', origin_session_id: 's2', created_at: '2026-07-31T00:00:00Z' }] }] });
  env.win.NAGENT.api.listScheduledTasks = () => Promise.resolve([{ id: 'sched_x', session_id: 's2', created_at: '2026-07-31T00:00:00Z' }]);
  await env.chat.loadSessionViewLinks('s1', emptyDetail);
  await env.waitMicro();
  env.chat.setHeader('s1', env.chat.buildHeaderLinks(emptyDetail, 's1'));
  ok(headerHrefs(env).length === 0, 'no association -> no links');

  // 拉取失败静默降级，不抛未处理 rejection，不展示对应链接
  env = loadChat();
  env.win.NAGENT.api.task.board = () => Promise.reject(new Error('network'));
  env.win.NAGENT.api.listScheduledTasks = () => Promise.resolve([]);
  let unhandled = 0;
  const uh = () => { unhandled++; };
  process.on('unhandledRejection', uh);
  await env.chat.loadSessionViewLinks('s1', emptyDetail);
  await env.waitMicro();
  env.chat.setHeader('s1', env.chat.buildHeaderLinks(emptyDetail, 's1'));
  ok(unhandled === 0, 'fetch failure silent, no unhandled rejection');
  ok(headerHrefs(env).length === 0, 'fetch failure -> no task/schedule links rendered');
  process.removeListener('unhandledRejection', uh);
}

async function testSidePanelAndArtifacts() {
  // 1. init 空态：无会话显示"暂未选择会话"，不发 artifact 请求
  let env = loadChat();
  env.chat.init();
  await env.waitMicro();
  ok(env.byIdMap['chat-artifact-list'].textContent === '暂未选择会话', 'init empty shows 暂未选择会话');
  ok(env.getArtifactFetchCalls().length === 0, 'init empty: no artifact fetch');

  // 2. bindSideToggle：点击同步 collapsed / shell class / aria-expanded
  env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const btn = env.byIdMap['chat-side-toggle-btn'];
  const shell = env.byIdMap['chat-shell'];
  ok(shell.classList.contains('chat-shell--side-collapsed') && btn.getAttribute('aria-expanded') === 'false', 'init: shell collapsed + btn aria false');
  btn.click();
  ok(!shell.classList.contains('chat-shell--side-collapsed') && btn.getAttribute('aria-expanded') === 'true', 'click btn expands: shell class removed, aria true');
  btn.click();
  ok(shell.classList.contains('chat-shell--side-collapsed') && btn.getAttribute('aria-expanded') === 'false', 'click btn collapses again');

  // 3. bindTabSwitch 点击切换：恰好一个 active，aria/tabindex/hidden 一致；切换不发 artifact 请求
  env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const toolBtn = env.byIdMap['chat-tab-tool-button'];
  const artBtn = env.byIdMap['chat-tab-artifact-button'];
  const toolP = env.byIdMap['chat-tab-tool'];
  const artP = env.byIdMap['chat-tab-artifact'];
  ok(toolBtn.classList.contains('chat-tab--active') && toolBtn.getAttribute('aria-selected') === 'true' && toolBtn.getAttribute('tabindex') === '0', 'init: tool tab active');
  ok(artBtn.getAttribute('aria-selected') === 'false' && artBtn.getAttribute('tabindex') === '-1' && artP.hidden === true, 'init: artifact tab inactive + hidden');
  artBtn.click();
  ok(artBtn.classList.contains('chat-tab--active') && artBtn.getAttribute('aria-selected') === 'true' && artBtn.getAttribute('tabindex') === '0', 'click artifact: active');
  ok(!toolBtn.classList.contains('chat-tab--active') && toolBtn.getAttribute('aria-selected') === 'false' && toolP.hidden === true && artP.hidden === false, 'click artifact: tool inactive+hidden, artifact visible');
  ok(env.getArtifactFetchCalls().length === 0, 'tab switch does not fetch artifacts');
  toolBtn.click();
  ok(toolBtn.classList.contains('chat-tab--active') && toolP.hidden === false && artP.hidden === true, 'click tool: back to tool');

  // 4. bindTabSwitch 键盘：ArrowRight/Left/Home/End + preventDefault + focus
  env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const tb = env.byIdMap['chat-tab-tool-button'];
  const ab = env.byIdMap['chat-tab-artifact-button'];
  // T6: 第三 tab（Run 详情）加入键盘循环；End 直达末位（run），Home 回首位
  const rb = env.byIdMap['chat-tab-run-button'];
  let r = env.fireKeydown(tb, 'ArrowRight');
  ok(r.prevented === true && ab.classList.contains('chat-tab--active'), 'ArrowRight -> artifact active + prevented');
  r = env.fireKeydown(ab, 'ArrowLeft');
  ok(r.prevented === true && tb.classList.contains('chat-tab--active'), 'ArrowLeft -> tool active + prevented');
  r = env.fireKeydown(tb, 'End');
  ok(r.prevented === true && rb.classList.contains('chat-tab--active'), 'End -> run (third tab) active');
  r = env.fireKeydown(rb, 'Home');
  ok(r.prevented === true && tb.classList.contains('chat-tab--active'), 'Home -> tool active');

  // 5. renderArtifactPanel 有会话+items：send 触发 ensureSession -> 加载 -> renderer -> nav
  env = loadChat();
  env.setArtifactFetchHandler(() => Promise.resolve({ ok: true, status: 200, json: async () => ({ items: [
    { id: 'a1', name: '制品一', kind: 'text', source_kind: 'session', updated_at: '2026-08-03T10:00:00Z' },
    { id: 'a2', name: '制品二', kind: 'code', source_kind: 'session', updated_at: '2026-08-03T11:00:00Z' },
  ], next_cursor: null }) }));
  env.input.value = '/task list';
  await env.chat.send();
  await env.waitMicro(); await env.waitMicro(); await env.waitMicro();
  const list = env.byIdMap['chat-artifact-list'];
  ok(list._kids && list._kids.length === 2, 'session artifacts rendered 2 items');
  ok(env.getArtifactFetchCalls().length === 1, 'exactly one artifact fetch on session create');
  ok(env.getArtifactFetchCalls()[0].url.indexOf('source_session_id=') !== -1 && env.getArtifactFetchCalls()[0].url.indexOf('limit=50') !== -1, 'fetch URL has source_session_id/limit');
  env.clearNavPathCalls();
  list._kids[0].click();
  ok(env.getNavPathCalls().indexOf('/artifacts/a1') !== -1, 'click item navigates to /artifacts/a1');

  // 6. 空制品 -> "暂无关联制品"
  env = loadChat();
  env.setArtifactFetchHandler(() => Promise.resolve({ ok: true, status: 200, json: async () => ({ items: [], next_cursor: null }) }));
  env.input.value = '/task list';
  await env.chat.send();
  await env.waitMicro(); await env.waitMicro(); await env.waitMicro();
  ok(env.byIdMap['chat-artifact-list'].textContent === '暂无关联制品', 'empty items -> 暂无关联制品');

  // 7. 非 2xx -> "加载失败"
  env = loadChat();
  env.setArtifactFetchHandler(() => Promise.resolve({ ok: false, status: 500, json: async () => ({}) }));
  env.input.value = '/task list';
  await env.chat.send();
  await env.waitMicro(); await env.waitMicro(); await env.waitMicro();
  ok(env.byIdMap['chat-artifact-list'].textContent === '加载失败', 'non-2xx -> 加载失败');

  // 8. applySessionDetail（auto-poll 路径）不触发 artifact 请求
  env = loadChat();
  env.chat.init();
  await env.waitMicro();
  env.clearArtifactFetchCalls();
  await env.chat.applySessionDetail({ messages: [] });
  await env.waitMicro(); await env.waitMicro(); await env.waitMicro(); await env.waitMicro();
  ok(env.getArtifactFetchCalls().length === 0, 'applySessionDetail does not fetch artifacts (no auto-poll)');
}

// ===========================================================================
// T4: 工具调用与制品面板随消息版本变化自动刷新（无需手动 F5）
// autoRefreshTick 检测到消息版本变化时，同步调用 loadToolCalls() 与
// renderArtifactPanel({silent:true})；版本无变化时不触发额外请求；silent 不闪烁。
// ===========================================================================
async function testAutoRefreshPanels() {
  const detailWith = (id, msgs) => ({ session: { id }, messages: msgs, summary: null, task_state: null });

  let env = loadChat();
  sessionsList = [{ id: 's1', title: 's1' }];
  let s1msgs = [{ id: 'm1', role: 'user', content: 'hi' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve(detailWith(id, s1msgs));
  let toolCallsCount = 0;
  env.win.NAGENT.api.getSessionToolCalls = () => { toolCallsCount++; return Promise.resolve([]); };
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro(); await env.waitMicro(); await env.waitMicro(); // selectSession 完成：初始加载工具调用+制品并 startAutoRefresh
  // 清零：排除 selectSession 初始加载计数，只观察轮询行为
  toolCallsCount = 0;
  env.clearArtifactFetchCalls();

  // 版本无变化：轮询不应触发面板刷新
  env.tickTimers();
  await env.waitMicro(); await env.waitMicro();
  ok(toolCallsCount === 0, 'no version change: tool calls not re-fetched (got ' + toolCallsCount + ')');
  ok(env.getArtifactFetchCalls().length === 0, 'no version change: artifacts not re-fetched');

  // 版本变化（任务异步产出新消息）：轮询应刷新工具调用与制品面板
  s1msgs = [{ id: 'm1', role: 'user', content: 'hi' }, { id: 'm2', role: 'assistant', content: 'reply' }];
  env.tickTimers();
  await env.waitMicro(); await env.waitMicro(); await env.waitMicro();
  ok(toolCallsCount >= 1, 'version change: tool calls re-fetched (got ' + toolCallsCount + ')');
  ok(env.getArtifactFetchCalls().length >= 1, 'version change: artifacts re-fetched (got ' + env.getArtifactFetchCalls().length + ')');
  // silent 刷新保留现有内容，不闪烁「加载中...」
  ok(env.byIdMap['chat-artifact-list'].textContent !== '加载中...', 'silent refresh: no 加载中 flicker');
}

// ===========================================================================
// T10: ui.artifact 卡片渲染 (conversational artifact write-tool card)
// - name / version(vN) / publish-status badge / fixed /artifacts/{id} 详情 link
// - link built ONLY from structured card.artifact_id (encodeURIComponent),
//   never from a model-provided URL (card carries no url/share_url field)
// - publish_sync_state badge: unpublished->未发布, current->已发布, outdated->已过期
// - missing artifact_id -> content text only, no link
// - NOT absorbed into task_result groups (renders independently)
// ===========================================================================
(function testUiArtifactCard() {
  const env = loadChat();
  const createMessageElement = env.chat.createMessageElement;
  const g = env.chat.groupTaskMessages;
  function collectAnchors(node) {
    const out = [];
    (function walk(n) {
      if (!n || !n._kids) return;
      for (const k of n._kids) { if (k.tagName === 'A') out.push(k); walk(k); }
    })(node);
    return out;
  }
  function collectByClass(node, cls) {
    const out = [];
    (function walk(n) {
      if (!n || !n._kids) return;
      for (const k of n._kids) {
        if (k.className && k.className.split(/\s+/).indexOf(cls) !== -1) out.push(k);
        walk(k);
      }
    })(node);
    return out;
  }

  // --- create success: unpublished ---
  const card = { artifact_id: 'art-1', revision_id: 'r1', name: '季度报告', kind: 'document', revision_number: 1, publish_sync_state: 'unpublished' };
  let el = createMessageElement({ id: 'm1', role: 'system', name: 'ui.artifact', content: '制品已更新: 季度报告', card: card });
  ok(el.className === 'msg assistant', 'ui.artifact renders as msg assistant (got ' + el.className + ')');
  ok(el.textContent.indexOf('季度报告') !== -1, 'card shows artifact name');
  ok(el.textContent.indexOf('v1') !== -1, 'card shows version v1');
  ok(el.textContent.indexOf('未发布') !== -1, 'card shows unpublished badge label');
  const anchors = collectAnchors(el);
  ok(anchors.length === 1, 'card has exactly one 详情 link (got ' + anchors.length + ')');
  ok(anchors[0].href === '/artifacts/art-1', 'link href built from card.artifact_id (got ' + anchors[0].href + ')');
  ok(anchors[0].textContent === '详情', 'link text is 详情');
  const badges = collectByClass(el, 'chat-artifact-card__badge');
  ok(badges.length === 1 && badges[0].className.indexOf('chat-artifact-card__badge--unpublished') !== -1, 'badge carries unpublished state class');

  // --- publish success: current ---
  const pubCard = { artifact_id: 'art-1', revision_id: 'r2', name: '季度报告', kind: 'document', revision_number: 2, publish_sync_state: 'current' };
  el = createMessageElement({ id: 'm2', role: 'system', name: 'ui.artifact', content: '制品已更新: 季度报告', card: pubCard });
  ok(el.textContent.indexOf('已发布') !== -1, 'publish card shows 已发布 badge');
  ok(el.textContent.indexOf('v2') !== -1, 'publish card shows v2');
  ok(collectByClass(el, 'chat-artifact-card__badge')[0].className.indexOf('--current') !== -1, 'badge carries current state class');

  // --- rollback: outdated ---
  const rbCard = { artifact_id: 'art-1', revision_id: 'r3', name: '季度报告', kind: 'document', revision_number: 3, publish_sync_state: 'outdated' };
  el = createMessageElement({ id: 'm3', role: 'system', name: 'ui.artifact', content: '制品已更新: 季度报告', card: rbCard });
  ok(el.textContent.indexOf('已过期') !== -1, 'rollback card shows 已过期 badge');

  // --- encodeURIComponent applied to artifact_id ---
  const encCard = { artifact_id: 'art 1', revision_id: 'r1', name: 'x', kind: 'text', revision_number: 1, publish_sync_state: 'unpublished' };
  el = createMessageElement({ id: 'm4', role: 'system', name: 'ui.artifact', content: '制品已更新: x', card: encCard });
  ok(collectAnchors(el)[0].href === '/artifacts/art%201', 'artifact_id is encodeURIComponent-ed in link (got ' + collectAnchors(el)[0].href + ')');

  // --- no model URL rendering: even if a malicious url/share_url were present
  //     in the card, the card schema excludes it and the renderer ignores it ---
  const leakCard = { artifact_id: 'art-1', revision_id: 'r1', name: 'x', kind: 'text', revision_number: 1, publish_sync_state: 'unpublished', share_url: 'https://evil.example/x', url: 'javascript:alert(1)' };
  el = createMessageElement({ id: 'm5', role: 'system', name: 'ui.artifact', content: '制品已更新: x', card: leakCard });
  const leakAnchors = collectAnchors(el);
  ok(leakAnchors.length === 1 && leakAnchors[0].href === '/artifacts/art-1', 'renderer ignores model-provided url/share_url; link stays fixed in-site path (got ' + JSON.stringify(leakAnchors.map((a) => a.href)) + ')');

  // --- missing artifact_id -> content text only, no link ---
  el = createMessageElement({ id: 'm6', role: 'system', name: 'ui.artifact', content: '制品已更新: x', card: { name: 'x', revision_number: 1, publish_sync_state: 'unpublished' } });
  ok(collectAnchors(el).length === 0, 'no link when artifact_id missing (got ' + collectAnchors(el).length + ')');
  ok(el.textContent.indexOf('制品已更新: x') !== -1, 'content text rendered as fallback');

  // --- no card at all -> content text only ---
  el = createMessageElement({ id: 'm7', role: 'system', name: 'ui.artifact', content: '制品已更新: x' });
  ok(collectAnchors(el).length === 0, 'no link when card absent');
  ok(el.textContent.indexOf('制品已更新: x') !== -1, 'content text rendered when no card');

  // --- NOT absorbed into task_result groups (renders independently) ---
  const grouped = g([
    { id: 'r1', role: 'system', name: 'ui.task_result', content: '任务已完成：t' },
    { id: 'a1', role: 'system', name: 'ui.artifact', content: '制品已更新: x', card: card },
  ]);
  ok(grouped.length === 2, 'ui.artifact not absorbed by task_result (got ' + grouped.length + ' groups)');
  ok(grouped[1].id === 'a1' && grouped[1].name === 'ui.artifact', 'ui.artifact stays independent after task_result');
})();

// ===========================================================================
// 会话来源筛选：默认隐藏 delegation，Modal 显式应用才改变列表；取消、遮罩和 ESC
// 不提交草稿，localStorage 无效值回退为默认筛选。
// ===========================================================================
async function testSessionSourceFilter() {
  let env = loadChat();
  env.clearLocalStorage();
  env = loadChat();
  sessionsList = [
    { id: 'normal', title: '普通会话', source: 'dashboard' },
    { id: 'delegated', title: '委派会话', source: 'delegation' },
  ];
  env.chat.init();
  await env.waitMicro();
  const list = env.byIdMap['chat-session-list'];
  ok(list._kids.length === 1 && list._kids[0].textContent.indexOf('普通会话') !== -1, 'session filter hides delegation by default');

  const openModal = () => {
    env.fireClick(env.byIdMap['chat-session-filter-btn']);
    return env.document.getElementById('chat-session-source-filter-modal');
  };
  const checkbox = (backdrop, value) => {
    const visit = (node) => {
      if (!node) return null;
      if (node.tagName === 'INPUT' && node.value === value) return node;
      for (const child of node._kids || []) {
        const found = visit(child);
        if (found) return found;
      }
      return null;
    };
    return visit(backdrop);
  };
  const formOf = (backdrop) => backdrop._kids[0]._kids[0];

  let backdrop = openModal();
  let delegation = checkbox(backdrop, 'delegation');
  delegation.checked = true;
  delegation.dispatchEvent({ type: 'change' });
  formOf(backdrop).dispatchEvent({ type: 'submit', preventDefault: () => {} });
  await env.waitMicro();
  ok(list._kids.length === 2, 'applying delegation selection shows delegated session');

  backdrop = openModal();
  delegation = checkbox(backdrop, 'delegation');
  delegation.checked = false;
  delegation.dispatchEvent({ type: 'change' });
  const cancel = formOf(backdrop)._kids[formOf(backdrop)._kids.length - 1]._kids[0];
  cancel.click();
  ok(list._kids.length === 2, 'cancel does not apply source-filter draft');

  backdrop = openModal();
  delegation = checkbox(backdrop, 'delegation');
  delegation.checked = false;
  delegation.dispatchEvent({ type: 'change' });
  backdrop.dispatchEvent({ type: 'click', target: backdrop });
  ok(list._kids.length === 2, 'backdrop close does not apply source-filter draft');

  backdrop = openModal();
  delegation = checkbox(backdrop, 'delegation');
  delegation.checked = false;
  delegation.dispatchEvent({ type: 'change' });
  env.fireDocumentKeydown('Escape');
  ok(!env.document.getElementById('chat-session-source-filter-modal') && list._kids.length === 2, 'ESC closes modal without applying source-filter draft');

  backdrop = openModal();
  delegation = checkbox(backdrop, 'delegation');
  delegation.checked = false;
  delegation.dispatchEvent({ type: 'change' });
  formOf(backdrop).dispatchEvent({ type: 'submit', preventDefault: () => {} });
  await env.waitMicro();
  ok(list._kids.length === 1, 'unapplying delegation hides delegated session again');

  env = loadChat();
  sessionsList = [
    { id: 'normal', title: '普通会话', source: 'dashboard' },
    { id: 'delegated', title: '委派会话', source: 'delegation' },
  ];
  env.chat.init();
  await env.waitMicro();
  ok(env.byIdMap['chat-session-list']._kids.length === 1, 'stored source filter survives a new page instance');

  env.clearLocalStorage();
  const seed = loadChat();
  seed.win.localStorage.setItem('nagent.chat.session-source-filter.v1', '{bad json');
  env = loadChat();
  sessionsList = [
    { id: 'normal', title: '普通会话', source: 'dashboard' },
    { id: 'delegated', title: '委派会话', source: 'delegation' },
  ];
  env.chat.init();
  await env.waitMicro();
  ok(env.byIdMap['chat-session-list']._kids.length === 1, 'invalid stored source filter falls back to default');
}

// ===========================================================================
// 会话搜索：独立 Modal、打开时来源快照、安全标题匹配和常规关闭。
// ===========================================================================
async function testSessionSearch() {
  const find = (node, predicate) => {
    if (!node) return null;
    if (predicate(node)) return node;
    for (const child of node._kids || []) {
      const found = find(child, predicate);
      if (found) return found;
    }
    return null;
  };
  let env = loadChat();
  sessionsList = [
    { id: 'api-alpha', title: ' Alpha <safe> ', source: 'api' },
    { id: 'dashboard-beta', title: 'Beta', source: 'dashboard' },
    { id: 'delegated', title: 'Alpha delegated', source: 'delegation' },
    { id: 'bad', title: 'bad', source: 'unknown' },
  ];
  env.chat.init();
  await env.waitMicro();
  env.fireClick(env.byIdMap['chat-session-search-btn']);
  const backdrop = env.document.getElementById('chat-session-search-modal');
  ok(!!backdrop, 'session search opens a dedicated backdrop');
  const input = find(backdrop, (node) => node.id === 'chat-session-search-input');
  const results = find(backdrop, (node) => node.id === 'chat-session-search-results');
  const status = find(backdrop, (node) => node.id === 'chat-session-search-status');
  ok(env.document.activeElement === input, 'session search focuses input after opening');
  await env.waitMicro(); await env.waitMicro();
  ok(results._kids.length === 2, 'search keeps only visible valid source records (status=' + status.textContent + ')');
  ok(results.textContent.indexOf('Alpha <safe>') !== -1, 'search renders title as text content');
  input.value = 'alpha';
  input.dispatchEvent({ type: 'input' });
  ok(results._kids.length === 1 && results.textContent.indexOf('Alpha <safe>') !== -1, 'search applies case-insensitive title matching');
  input.value = '';
  input.dispatchEvent({ type: 'search' });
  ok(results._kids.length === 2, 'native search clear event restores results');
  const closeBtn = find(backdrop, (node) => node.className === 'modal-close');
  closeBtn.focus();
  closeBtn.click();
  ok(!env.document.getElementById('chat-session-search-modal'), 'search close removes only its backdrop');
  ok(env.document.activeElement === env.byIdMap['chat-session-search-btn'], 'search close restores trigger focus');
}

// ===========================================================================
// T3: 左侧会话面板独立折叠（bindSessionsToggle）
// 左右折叠互不干扰；aria/hidden 同步；焦点移动；无网络/定时器副作用；
// 宿主 DOM 缺失时安全降级。
// ===========================================================================
async function testSessionsPanelCollapse() {
  const hasSessions = (sh) => sh.classList.contains('chat-shell--sessions-collapsed');
  const hasSide = (sh) => sh.classList.contains('chat-shell--side-collapsed');

  // 1. 初始态：左展开（无 sessions-collapsed）、右折叠（side-collapsed）
  let env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const shell = env.byIdMap['chat-shell'];
  const hideBtn = env.byIdMap['chat-session-toggle-btn'];
  const expandBtn = env.byIdMap['chat-session-expand-btn'];
  ok(hasSide(shell) && !hasSessions(shell), 'init: left expanded, right collapsed');
  ok(hideBtn.getAttribute('aria-expanded') === 'true', 'init: hide btn aria-expanded=true');
  ok(expandBtn.getAttribute('aria-expanded') === 'false' && expandBtn.hidden === true, 'init: expand btn aria-expanded=false + hidden');

  // 2. 点击隐藏：仅新增 sessions-collapsed，保留 side-collapsed；焦点移到展开按钮
  hideBtn.click();
  ok(hasSessions(shell) && hasSide(shell), 'hide click: only adds sessions-collapsed, keeps side-collapsed');
  ok(expandBtn.hidden === false, 'hide click: expand btn shown');
  ok(hideBtn.getAttribute('aria-expanded') === 'false' && expandBtn.getAttribute('aria-expanded') === 'false', 'hide click: both aria-expanded=false');
  ok(env.document.activeElement === expandBtn, 'hide click: focus moves to expand btn');
  ok(sessionPanelStillChild(env), 'hide click: collapsed panel remains DOM child of shell');

  // 3. 点击展开：仅移除 sessions-collapsed；焦点回到隐藏按钮
  expandBtn.click();
  ok(!hasSessions(shell) && hasSide(shell), 'expand click: only removes sessions-collapsed, side-collapsed kept');
  ok(expandBtn.hidden === true, 'expand click: expand btn hidden');
  ok(hideBtn.getAttribute('aria-expanded') === 'true' && expandBtn.getAttribute('aria-expanded') === 'true', 'expand click: both aria-expanded=true');
  ok(env.document.activeElement === hideBtn, 'expand click: focus back to hide btn');

  function sessionPanelStillChild(e) {
    const sh = e.byIdMap['chat-shell'];
    const panel = e.byIdMap['chat-session-panel'];
    return panel && panel.parentNode === sh;
  }

  // 4. 正交性 + 四态列数表（镜像 CSS 网格规则；桩无 CSSOM，用数据表断言）
  env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const sh = env.byIdMap['chat-shell'];
  const hb = env.byIdMap['chat-session-toggle-btn'];
  const eb = env.byIdMap['chat-session-expand-btn'];
  const sb = env.byIdMap['chat-side-toggle-btn'];
  // 数据表镜像 styles.css 规则：
  //   默认 .chat-shell = 3 列；+side-collapsed = 2 列；+sessions-collapsed = 2 列；双折叠 = 1 列
  const columnTable = [
    { sessions: false, side: false, cols: 3 }, // 双展开
    { sessions: false, side: true, cols: 2 },  // 默认：左展开+右折叠
    { sessions: true, side: false, cols: 2 },  // 左折叠+右展开
    { sessions: true, side: true, cols: 1 },   // 双折叠
  ];
  const expectedCols = (sessions, side) => {
    const row = columnTable.find((r) => r.sessions === sessions && r.side === side);
    return row ? row.cols : -1;
  };
  function assertCombo(name) {
    ok(expectedCols(hasSessions(sh), hasSide(sh)) === comboCols(name), name + ': column count matches CSS table (sessions=' + hasSessions(sh) + ', side=' + hasSide(sh) + ')');
  }
  let comboStep = 0;
  const combos = ['left-expanded+right-collapsed', 'right-expanded', 'left-collapsed+right-expanded', 'both-collapsed'];
  const comboCols = (name) => ({ 'left-expanded+right-collapsed': 2, 'right-expanded': 3, 'left-collapsed+right-expanded': 2, 'both-collapsed': 1 })[name];

  // 默认：左展开+右折叠（2 列）
  assertCombo(combos[comboStep++]);
  // 右侧展开（3 列）；随后左侧隐藏不影响右侧展开
  sb.click();
  ok(!hasSide(sh), 'right toggle expands right panel');
  assertCombo(combos[comboStep++]);
  hb.click();
  ok(hasSessions(sh) && !hasSide(sh), 'left hide keeps right expanded');
  assertCombo(combos[comboStep++]);
  ok(sessionPanelStillChild(env), 'collapsed left panel remains DOM child');
  ok(eb.hidden === false, 'expand btn visible when left collapsed');
  // 右侧折叠不影响左侧已折叠状态；随后左侧展开不影响右侧折叠
  sb.click();
  ok(hasSessions(sh) && hasSide(sh), 'right collapse keeps left collapsed');
  assertCombo(combos[comboStep++]);
  eb.click();
  ok(!hasSessions(sh) && hasSide(sh), 'left expand keeps right collapsed');
  const sidePanel = env.byIdMap['chat-side-panel'];
  ok(sidePanel.parentNode === sh, 'collapsed right panel remains DOM child of shell');

  // 5. 宿主 DOM 缺失安全降级（每个场景独立 VM）
  for (const missing of ['chat-shell', 'chat-session-toggle-btn', 'chat-session-expand-btn']) {
    const e2 = loadChat({ missingIds: [missing] });
    let threw = null;
    try {
      e2.chat.init();
    } catch (err) {
      threw = err;
    }
    ok(!threw, 'init safe when ' + missing + ' missing' + (threw ? ' (threw: ' + threw.message + ')' : ''));
    await e2.waitMicro();
    if (missing !== 'chat-shell') {
      const sb2 = e2.byIdMap['chat-side-toggle-btn'];
      ok(sb2 && sb2.getAttribute('aria-expanded') === 'false', 'side toggle initial ARIA still synced when ' + missing + ' missing');
    } else {
      // chat-shell 缺失：静态初始属性不受影响（无 JS 同步）
      ok(e2.byIdMap['chat-session-toggle-btn'].getAttribute('aria-expanded') === 'true', 'static initial aria kept on hide btn when chat-shell missing');
    }
  }

  // 6. 副作用快照：两次点击不引入 fetch / 定时器
  env = loadChat();
  env.chat.init();
  await env.waitMicro();
  const beforeFetch = fetchCalls.length;
  const beforeTimers = env.timerCount();
  env.byIdMap['chat-session-toggle-btn'].click();
  env.byIdMap['chat-session-expand-btn'].click();
  ok(fetchCalls.length === beforeFetch, 'sessions toggle adds no fetch calls');
  ok(env.timerCount() === beforeTimers, 'sessions toggle adds no timers');
}

// ===========================================================================
// T5/T6/T7: 技能选择器（chat composer skill picker）
// 覆盖 8 个行为场景：
//   #1  loadAvailableSkills 过滤/分组/排序/安全渲染
//   #2  localStorage 归一化（含坏 JSON/非对象降级）+ 刷新恢复
//   #3  首败保留持久化 / 后续败保留最近成功 / 无操作不写回
//   #4  两个 deferred 倒序：旧响应不覆盖新；请求期间切换/删除不重开
//   #5  满 10：未选 disabled，已选可取消；trigger 计数 + .active
//   #6  有/无 sendBtn init 路径顺序；三方互斥 + 外部点击
//   #7  selectSession 立即切换 trigger/药丸；删除成功清键/失败不清；删当前重置 draft
//   #8  空态选择 -> createSession 成功用于首条；reject 保留 draft 可重试
// ===========================================================================

// 通用辅助：找 chat-skill 容器（按真实 id 链）
function _skillContainer(env) {
  return env.byIdMap['chat-skill'] || env.document.getElementById('chat-skill');
}
// 通用辅助：按 className 收集所有匹配节点（深度优先）
function _collectByClass(root, cls) {
  const out = [];
  (function walk(node) {
    if (!node) return;
    if (node.className && node.className.split(/\s+/).indexOf(cls) !== -1) out.push(node);
    for (const c of (node._kids || [])) walk(c);
  })(root);
  return out;
}
// 通用辅助：找 chat-skill 容器下的 trigger
function _skillTrigger(env) {
  const cont = _skillContainer(env);
  if (!cont) return null;
  const triggers = _collectByClass(cont, 'chat-memory-trigger');
  return triggers[0] || null;
}
// 通用辅助：找 chat-skill 容器下的 popover（dialog）
function _skillPopover(env) {
  const cont = _skillContainer(env);
  if (!cont) return null;
  const popovers = _collectByClass(cont, 'chat-memory-popover');
  return popovers[0] || null;
}
// 通用辅助：找会话 item 内 aria-label='删除会话' 的按钮（用于删除路径）
function _sessionDeleteButton(env) {
  const items = env.document.querySelectorAll('.session-item');
  for (const it of items) {
    const found = (function visit(n) {
      if (!n) return null;
      for (const c of (n._kids || [])) {
        if (c.tagName === 'BUTTON' && c.getAttribute('aria-label') === '删除会话') return c;
        const r = visit(c);
        if (r) return r;
      }
      return null;
    })(it);
    if (found) return found;
  }
  return null;
}

async function testSkillPicker() {
  // === #1: loadAvailableSkills 过滤/分组/排序/安全渲染 ===
  {
    const env = loadChat();
    env.setListSkillsHandler(() => Promise.resolve({
      skills: [
        { name: 'inactive-1', enabled: false, readiness: 'available', source: 'user' },
        { name: 'pending-1', enabled: true, readiness: 'pending', source: 'user' },
        { name: '', enabled: true, readiness: 'available', source: 'user' },
        { enabled: true, readiness: 'available', source: 'user' },
        { name: 'duplicate', enabled: true, readiness: 'available', source: 'user' },
        { name: 'duplicate', enabled: true, readiness: 'available', source: 'user' },
        { name: '<img src=x onerror=alert(1)>', enabled: true, readiness: 'available', source: 'user', description: '<bad>desc' },
        { name: 'beta', enabled: true, readiness: 'available', source: 'seed' },
        { name: 'gamma', enabled: true, readiness: 'available', source: 'agent' },
        { name: 'alpha', enabled: true, readiness: 'available', source: 'user' },
        { name: 'unknown-source', enabled: true, readiness: 'available', source: 'plugin' },
      ],
    }));
    await env.chat.init();
    await env.waitMicro();
    await env.waitMicro();
    const trigger = _skillTrigger(env);
    ok(!!trigger, '#1 init: chat-skill trigger rendered');
    if (!trigger) return;
    ok(!trigger.classList.contains('active'), '#1 init empty: trigger has no .active class');
    env.fireClick(trigger);
    await env.waitMicro();
    await env.waitMicro();
    const popover = _skillPopover(env);
    ok(!!popover, '#1 click trigger: popover rendered');
    if (!popover) return;
    const groupTitles = _collectByClass(popover, 'chat-memory-popover__group-title').map((n) => n.textContent);
    ok(JSON.stringify(groupTitles) === JSON.stringify(['用户', '系统', '自建', '其他']),
      '#1 groups: 用户 -> 系统 -> 自建 -> 其他 (got ' + JSON.stringify(groupTitles) + ')');
    const pills = _collectByClass(popover, 'chat-memory-option');
    const pillNames = pills.map((p) => p.textContent);
    // 数据有 11 项；去掉 1 个 inactive、1 个 pending、1 个 空名、1 个 缺名、1 个 重复 (留下 1 个 duplicate)
    // 剩余 5 个 (alpha, beta, duplicate, gamma, unknown-source, <img...>) — 等等，重新数：
    // 原数据 11 项：inactive-1, pending-1, '', no-name, duplicate x2, <img>, beta, gamma, alpha, unknown-source
    // 过滤后保留 (按 name 升序): alpha, beta, duplicate, gamma, unknown-source, <img src=x onerror=alert(1)>
    // 实际上 localeCompare 把 '<' 排在字母前，所以 <img...> 第一
    // localeCompare 默认：'<' (60) < 'a' (97)，所以 <img> 排在字母前；同组内按 name 升序
    const expectedNames = ['<img src=x onerror=alert(1)>', 'alpha', 'duplicate', 'beta', 'gamma', 'unknown-source'];
    ok(JSON.stringify(pillNames) === JSON.stringify(expectedNames),
      '#1 pills: enabled+available+non-empty, duplicates collapsed, source bucketing (got ' + JSON.stringify(pillNames) + ')');
    // XSS name：不应创建 IMG 节点
    const hasImg = (function findImg(n) {
      if (!n) return false;
      if (n.tagName === 'IMG') return true;
      for (const c of (n._kids || [])) if (findImg(c)) return true;
      return false;
    })(popover);
    ok(!hasImg, '#1 XSS name: no IMG element created (textContent only)');
    // description -> title attribute
    const xssPill = pills.find((p) => p.textContent === '<img src=x onerror=alert(1)>');
    ok(!!xssPill && xssPill.title && xssPill.title.indexOf('<bad>desc') !== -1,
      '#1 description -> title attribute (got ' + (xssPill && xssPill.title) + ')');
    ok(!!xssPill && xssPill.dataset && xssPill.dataset.skillName === '<img src=x onerror=alert(1)>',
      '#1 dataset.skillName set');
  }

  // === #2: localStorage 归一化 + 刷新恢复 + 坏 JSON 降级 + 非对象降级 ===
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({
      '': ['ghost'],
      '  ': ['whitespace'],
      's1': ['  strip-me  ', '', 'dup', 'dup', 'order-b', 'order-a', 'order-b', 'x1', 'x2', 'x3', 'x4', 'x5', 'x6', 'x7'],
      's2': 'not-array',
      's3': null,
    }));
    // listSkills 必返回 s1 用到的 10 个名字（否则 filter 会把 raw 全部剔除）
    const skillsForS1 = () => Promise.resolve({ skills: [
      { name: 'strip-me', enabled: true, readiness: 'available', source: 'user' },
      { name: 'dup', enabled: true, readiness: 'available', source: 'user' },
      { name: 'order-b', enabled: true, readiness: 'available', source: 'user' },
      { name: 'order-a', enabled: true, readiness: 'available', source: 'user' },
      { name: 'x1', enabled: true, readiness: 'available', source: 'user' },
      { name: 'x2', enabled: true, readiness: 'available', source: 'user' },
      { name: 'x3', enabled: true, readiness: 'available', source: 'user' },
      { name: 'x4', enabled: true, readiness: 'available', source: 'user' },
      { name: 'x5', enabled: true, readiness: 'available', source: 'user' },
      { name: 'x6', enabled: true, readiness: 'available', source: 'user' },
    ] });
    env.setListSkillsHandler(skillsForS1);
    sessionsList = [{ id: 's1', title: 's1' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    await env.chat.init();
    // 等 init 内部 loadSessions 完成后才出现 session-item 节点
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const item = env.findSessionItem('s1');
    if (item) item.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const got = env.chat.getActivatedSkills();
    ok(Array.isArray(got) && got.length === 10, '#2 getActivatedSkills caps at 10 (got ' + (got && got.length) + ')');
    ok(got[0] === 'strip-me' && got[1] === 'dup' && got[2] === 'order-b' && got[3] === 'order-a',
      '#2 strip + dedup + order preserved (got ' + JSON.stringify(got) + ')');
    // 刷新：再 loadChat 验证恢复
    const env2 = loadChat();
    sessionsList = [{ id: 's1', title: 's1' }];
    env2.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    env2.setListSkillsHandler(skillsForS1);
    await env2.chat.init();
    await env2.waitMicro();
    env2.fireClick(env2.findSessionItem('s1'));
    await env2.waitMicro();
    await env2.waitMicro();
    const got2 = env2.chat.getActivatedSkills();
    ok(JSON.stringify(got2) === JSON.stringify(got), '#2 refresh (re-loadChat): same selection restored');
  }
  // 坏 JSON 降级
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', '{not valid');
    await env.chat.init();
    await env.waitMicro();
    const got = env.chat.getActivatedSkills();
    ok(Array.isArray(got) && got.length === 0, '#2b bad JSON: degrades to empty (got ' + JSON.stringify(got) + ')');
  }
  // 非对象（数组）降级
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', '[1,2,3]');
    await env.chat.init();
    await env.waitMicro();
    const got = env.chat.getActivatedSkills();
    ok(Array.isArray(got) && got.length === 0, '#2c non-object (array): degrades to empty (got ' + JSON.stringify(got) + ')');
  }

  // === #3: 首败保留持久化 / 后续败保留最近成功 / 成功刷新不写回 localStorage ===
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['kept'] }));
    env.setListSkillsHandler(() => Promise.reject(new Error('network')));
    sessionsList = [{ id: 's1', title: 's1' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const _item3 = env.findSessionItem('s1');
    if (_item3) _item3.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    // 首败：getActivatedSkills 返回 raw
    const got = env.chat.getActivatedSkills();
    ok(JSON.stringify(got) === JSON.stringify(['kept']), '#3 first load fails: persisted selection preserved (got ' + JSON.stringify(got) + ')');
    const ls = env.getLocalStorage('nagent.chat.activated-skills');
    ok(!!ls && ls.indexOf('kept') !== -1, '#3 first load fails: localStorage not rewritten');
  }
  // 后续败保留最近成功：先提供会成功的 handler 一次，再 reject
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['kept'] }));
    let n = 0;
    env.setListSkillsHandler(() => {
      n++;
      if (n === 1) return Promise.resolve({ skills: [
        { name: 'kept', enabled: true, readiness: 'available', source: 'user' },
        { name: 'new', enabled: true, readiness: 'available', source: 'user' },
      ] });
      return Promise.reject(new Error('again network'));
    });
    sessionsList = [{ id: 's1', title: 's1' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const _item3 = env.findSessionItem('s1');
    if (_item3) _item3.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const trigger = _skillTrigger(env);
    // 第一次成功（n=1）
    env.fireClick(trigger); await env.waitMicro(); await env.waitMicro();
    // 关闭 + 重新打开 -> 第二次调用 listSkills -> reject
    env.fireClick(trigger); await env.waitMicro();
    env.fireClick(trigger); await env.waitMicro(); await env.waitMicro();
    // 仍有最近成功缓存（首次成功），filter 后保留 'kept'（raw 只有 kept）
    const gotAfter = env.chat.getActivatedSkills();
    ok(JSON.stringify(gotAfter) === JSON.stringify(['kept']),
      '#3b subsequent failure: last successful cache kept (got ' + JSON.stringify(gotAfter) + ')');
  }
  // 成功刷新移除某 skill 后 filter，但 localStorage 未被改写
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['removed-skill', 'kept'] }));
    let n = 0;
    env.setListSkillsHandler(() => {
      n++;
      if (n === 1) return Promise.resolve({ skills: [
        { name: 'removed-skill', enabled: true, readiness: 'available', source: 'user' },
        { name: 'kept', enabled: true, readiness: 'available', source: 'user' },
      ] });
      return Promise.resolve({ skills: [
        { name: 'kept', enabled: true, readiness: 'available', source: 'user' },
      ] });
    });
    sessionsList = [{ id: 's1', title: 's1' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const _item3 = env.findSessionItem('s1');
    if (_item3) _item3.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    let got = env.chat.getActivatedSkills();
    ok(JSON.stringify(got) === JSON.stringify(['removed-skill', 'kept']),
      '#3c first refresh: both kept (got ' + JSON.stringify(got) + ')');
    const trigger = _skillTrigger(env);
    env.fireClick(trigger); await env.waitMicro(); // close
    env.fireClick(trigger); await env.waitMicro(); await env.waitMicro(); // open -> listSkills 第二次
    got = env.chat.getActivatedSkills();
    ok(JSON.stringify(got) === JSON.stringify(['kept']),
      '#3d second refresh: removed skill filtered out (got ' + JSON.stringify(got) + ')');
    const ls = env.getLocalStorage('nagent.chat.activated-skills');
    ok(!!ls && ls.indexOf('removed-skill') !== -1,
      '#3e localStorage not rewritten on background refresh (got ' + ls + ')');
  }

  // === #4: 两个 deferred 倒序：旧响应不覆盖新；请求期间切换/删除不重开 ===
  // 三个 listSkills 调用：init（renderWhenClosed=true）、click1 打开、click3 再次打开
  // 第三个调用直接 resolve 一个已知 skills 数据（init + click1 两次使用 deferred）
  {
    const env = loadChat();
    let firstResolve, secondResolve;
    let firstCalled = false, secondCalled = false;
    env.setListSkillsHandler(() => {
      if (!firstCalled) { firstCalled = true; return new Promise((res) => { firstResolve = res; }); }
      if (!secondCalled) { secondCalled = true; return new Promise((res) => { secondResolve = res; }); }
      return Promise.resolve({ skills: [{ name: 'newer-skill', enabled: true, readiness: 'available', source: 'user' }] });
    });
    sessionsList = [{ id: 's1', title: 's1' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const _item3 = env.findSessionItem('s1');
    if (_item3) _item3.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    // init 调用 listSkills -> firstResolve pending (init)
    // 第一次 click: 打开 -> listSkills -> secondResolve pending (click1)
    // 关闭
    // 再次打开 -> listSkills 第三次 -> 直接 resolve newer-skill (click3)
    let trigger = _skillTrigger(env);
    env.fireClick(trigger); await env.waitMicro();
    env.fireClick(trigger); await env.waitMicro();
    env.fireClick(trigger); await env.waitMicro();
    // 倒序：先 resolve 第二次（newer from click1, seq=2），再 resolve 第一次（older from init, seq=1）
    secondResolve({ skills: [{ name: 'newer-skill', enabled: true, readiness: 'available', source: 'user' }] });
    firstResolve({ skills: [{ name: 'older-skill', enabled: true, readiness: 'available', source: 'user' }] });
    await env.waitMicro(); await env.waitMicro();
    await env.waitMicro();
    trigger = _skillTrigger(env);
    const popover = _skillPopover(env);
    if (popover) {
      const pillNames = _collectByClass(popover, 'chat-memory-option').map((p) => p.textContent);
      ok(pillNames.indexOf('newer-skill') !== -1 && pillNames.indexOf('older-skill') === -1,
        '#4 out-of-order: older response does not overwrite newer cache (got ' + JSON.stringify(pillNames) + ')');
    } else {
      ok(false, '#4 popover should still be open after older+newer resolve');
    }
    // 关闭 popover 期间旧响应不应再重开
    env.fireClick(trigger); await env.waitMicro();
    const popoverAfter = _skillPopover(env);
    ok(!popoverAfter, '#4b out-of-order: stale response does not reopen closed popover');
  }
  // 请求期间切换会话不重开
  {
    const env = loadChat();
    let deferred;
    env.setListSkillsHandler(() => new Promise((res) => { deferred = res; }));
    sessionsList = [{ id: 's1', title: 's1' }, { id: 's2', title: 's2' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    env.fireClick(env.findSessionItem('s1'));
    await env.waitMicro();
    const trigger = _skillTrigger(env);
    env.fireClick(trigger); await env.waitMicro();
    // 切换到 s2
    env.fireClick(env.findSessionItem('s2'));
    await env.waitMicro();
    // resolve 旧请求
    deferred({ skills: [{ name: 'old-s1', enabled: true, readiness: 'available', source: 'user' }] });
    await env.waitMicro(); await env.waitMicro();
    // 当前是 s2，popover 不应被旧响应重开
    const popover = _skillPopover(env);
    ok(!popover, '#4c switch during pending: popover not reopened for new session');
  }

  // === #5: 满 10：未选 disabled，已选可取消；trigger 计数 + .active ===
  {
    const env = loadChat();
    const skillList = [];
    for (let i = 0; i < 12; i++) skillList.push({ name: 's' + i, enabled: true, readiness: 'available', source: 'user' });
    env.setListSkillsHandler(() => Promise.resolve({ skills: skillList }));
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['s0','s1','s2','s3','s4','s5','s6','s7','s8','s9'] }));
    sessionsList = [{ id: 's1', title: 's1' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const _item3 = env.findSessionItem('s1');
    if (_item3) _item3.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const trigger = _skillTrigger(env);
    ok(trigger.textContent.indexOf('技能 10') !== -1, '#5 trigger label = 技能 10 (got "' + trigger.textContent + '")');
    ok(trigger.classList.contains('active'), '#5 trigger has .active when count > 0');
    env.fireClick(trigger); await env.waitMicro(); await env.waitMicro();
    const popover = _skillPopover(env);
    const pills = _collectByClass(popover, 'chat-memory-option');
    const enabledPills = pills.filter((p) => !p.disabled);
    const disabledPills = pills.filter((p) => p.disabled);
    ok(enabledPills.length === 10, '#5 all 10 selected pills remain enabled (got ' + enabledPills.length + ')');
    ok(disabledPills.length === 2, '#5 2 unselected pills disabled at cap (got ' + disabledPills.length + ')');
    // 取消一个已选：cap 10 -> 9，未选全部恢复 enabled
    const firstSelected = enabledPills[0];
    firstSelected.click();
    await env.waitMicro();
    const popover2 = _skillPopover(env);
    const pills2 = popover2 ? _collectByClass(popover2, 'chat-memory-option') : [];
    const disabled2 = pills2.filter((p) => p.disabled);
    ok(disabled2.length === 0, '#5b after cancel: cap relaxes, all unselected re-enabled (got ' + disabled2.length + ' disabled)');
  }

  // === #6: 有/无 sendBtn init 路径顺序；三方互斥 + 外部点击 ===
  // 有 sendBtn（默认）：3 个容器按 记忆 -> 技能 -> 设置 顺序
  {
    const env = loadChat();
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const composerBar = env.document.querySelector('.chat-composer__bar');
    ok(!!composerBar, '#6 init with sendBtn: composer bar exists');
    if (composerBar) {
      // Use document.getElementById (which uses node.id, not _attrs.id) since chat.js sets emContainer.id directly
      const em = (function findById(root, id) {
        const visit = (n) => {
          if (!n) return null;
          if (n.id === id) return n;
          for (const c of (n._kids || [])) { const f = visit(c); if (f) return f; }
          return null;
        };
        return visit(root);
      })(composerBar, 'chat-external-memory');
      const _sk6 = (function findById(root, id) {
        const visit = (n) => {
          if (!n) return null;
          if (n.id === id) return n;
          for (const c of (n._kids || [])) { const f = visit(c); if (f) return f; }
          return null;
        };
        return visit(root);
      })(composerBar, 'chat-skill');
      const _st6 = (function findById(root, id) {
        const visit = (n) => {
          if (!n) return null;
          if (n.id === id) return n;
          for (const c of (n._kids || [])) { const f = visit(c); if (f) return f; }
          return null;
        };
        return visit(root);
      })(composerBar, 'chat-settings');
      const sk = (function findById(root, id) {
        const visit = (n) => {
          if (!n) return null;
          if (n._attrs && n._attrs.id === id) return n;
          for (const c of (n._kids || [])) { const f = visit(c); if (f) return f; }
          return null;
        };
        return visit(root);
      })(composerBar, 'chat-skill');
      const st = (function findById(root, id) {
        const visit = (n) => {
          if (!n) return null;
          if (n._attrs && n._attrs.id === id) return n;
          for (const c of (n._kids || [])) { const f = visit(c); if (f) return f; }
          return null;
        };
        return visit(root);
      })(composerBar, 'chat-settings');
      ok(!!em && !!_sk6 && !!_st6, '#6 all three composer containers present');
      if (em && _sk6 && _st6) {
        const idx = (n) => composerBar._kids.indexOf(n);
        ok(idx(em) >= 0 && idx(_sk6) >= 0 && idx(_st6) >= 0, '#6 all three are direct children of composer bar');
        ok(idx(em) < idx(_sk6) && idx(_sk6) < idx(_st6),
          '#6 with sendBtn: 记忆 -> 技能 -> 设置 (em=' + idx(em) + ', sk=' + idx(_sk6) + ', st=' + idx(_st6) + ')');
      }
    }
  }
  // 无 sendBtn 路径
  {
    const env = loadChat({ missingIds: ['chat-send'] });
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const composerBar = env.document.querySelector('.chat-composer__bar');
    ok(!!composerBar, '#6 init without sendBtn: composer bar exists');
    if (composerBar) {
      // 用 node.id 而非 _attrs.id（chat.js 直接给 emContainer.id 赋值）
      const findById = (root, id) => {
        const visit = (n) => {
          if (!n) return null;
          if (n.id === id) return n;
          for (const c of (n._kids || [])) { const f = visit(c); if (f) return f; }
          return null;
        };
        return visit(root);
      };
      const em_nosend = findById(composerBar, 'chat-external-memory');
      const sk_nosend = findById(composerBar, 'chat-skill');
      const st_nosend = findById(composerBar, 'chat-settings');
      ok(!!em_nosend && !!sk_nosend && !!st_nosend, '#6 no-sendBtn path: all three containers present');
      if (em_nosend && sk_nosend && st_nosend) {
        const idx = (n) => composerBar._kids.indexOf(n);
        ok(idx(em_nosend) < idx(sk_nosend) && idx(sk_nosend) < idx(st_nosend),
          '#6 no-sendBtn fallback: 记忆 -> 技能 -> 设置 (em=' + idx(em_nosend) + ', sk=' + idx(sk_nosend) + ', st=' + idx(st_nosend) + ')');
      }
    }
  }
  // 三方互斥 + 外部点击
  {
    const env = loadChat();
    await env.chat.init();
    await env.waitMicro();
    // chat-external-memory / chat-settings 容器由 init 动态创建并赋值给 node.id；用 getElementById 查找
    const emEl = env.document.getElementById('chat-external-memory');
    const stEl = env.document.getElementById('chat-settings');
    const skTrigger = _skillTrigger(env);
    const emTrigger = emEl ? _collectByClass(emEl, 'chat-memory-trigger')[0] : null;
    const stTrigger = stEl ? _collectByClass(stEl, 'chat-memory-trigger')[0] : null;
    ok(!!skTrigger && !!emTrigger && !!stTrigger, '#6 three triggers rendered');
    if (!skTrigger || !emTrigger || !stTrigger) return;
    // 点开 skill
    env.fireClick(skTrigger); await env.waitMicro();
    ok(!!_skillPopover(env), '#6 open skill popover');
    const emElFresh = env.document.getElementById('chat-external-memory');
    const emTriggerFresh = emElFresh ? _collectByClass(emElFresh, 'chat-memory-trigger')[0] : null;
    env.fireClick(emTriggerFresh); await env.waitMicro();
    ok(!_skillPopover(env), '#6 click em trigger: skill popover closed (mutual exclusion)');
    // 点开 st -> em 应被收起（trigger 重新获取以避免 stale）
    const stElFresh = env.document.getElementById('chat-settings');
    const stTriggerFresh = stElFresh ? _collectByClass(stElFresh, 'chat-memory-trigger')[0] : null;
    env.fireClick(stTriggerFresh); await env.waitMicro();
    ok(!_collectByClass(emElFresh, 'chat-memory-popover')[0], '#6 click settings trigger: em popover closed (mutual exclusion)');
    // 现在 st 打开；点开 skill -> st 应被收起（trigger 重新获取以避免 stale）
    const skTriggerFresh2 = _skillTrigger(env);
    env.fireClick(skTriggerFresh2); await env.waitMicro();
    ok(!_collectByClass(stElFresh, 'chat-memory-popover')[0], '#6 click skill trigger: settings popover closed (mutual exclusion)');
    // round-robin 始终最多 1 个 dialog（每轮重新获取 trigger 以避免 stale）
    for (let round = 0; round < 3; round++) {
      const d = env.document.querySelectorAll('[role="dialog"]');
      ok(d.length === 1, '#6 round-robin round ' + round + ': exactly 1 dialog open (got ' + d.length + ')');
      // 按顺序点开下一个 trigger
      const targets = [
        env.document.getElementById('chat-external-memory'),
        env.document.getElementById('chat-settings'),
        env.document.getElementById('chat-skill'),
      ];
      const next = targets[round % 3];
      const nextTrigger = next ? _collectByClass(next, 'chat-memory-trigger')[0] : null;
      env.fireClick(nextTrigger); await env.waitMicro();
    }
    // 外部点击：点 document.body
    const dialog = env.document.querySelectorAll('[role="dialog"]');
    if (dialog.length > 0) {
      env.fireClick(env.document.body);
      await env.waitMicro();
      const d2 = env.document.querySelectorAll('[role="dialog"]');
      ok(d2.length === 0, '#6 outside click: closes popover (got ' + d2.length + ' dialogs)');
    }
  }

  // === #7: selectSession 立即切换 trigger/药丸；删除成功清键/失败不清；删当前重置 draft ===
  // selectSession 立即切换（在 detail 返回前）
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['kept-in-s1'], s2: ['kept-in-s2'] }));
    env.setListSkillsHandler(() => Promise.resolve({ skills: [
      { name: 'kept-in-s1', enabled: true, readiness: 'available', source: 'user' },
      { name: 'kept-in-s2', enabled: true, readiness: 'available', source: 'user' },
    ] }));
    let deferredDetail = null;
    env.win.NAGENT.api.getSessionDetail = (id) => {
      if (id === 's2') return new Promise((res) => { deferredDetail = res; });
      return Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    };
    sessionsList = [{ id: 's1', title: 's1' }, { id: 's2', title: 's2' }];
    await env.chat.init();
    await env.waitMicro();
    env.fireClick(env.findSessionItem('s1'));
    await env.waitMicro();
    const trigger = _skillTrigger(env);
    ok(trigger.textContent.indexOf('技能 1') !== -1,
      '#7 selectSession s1: trigger shows 1 (got "' + trigger.textContent + '")');
    // 立即点 s2（detail 还没返回）
    env.fireClick(env.findSessionItem('s2'));
    await env.waitMicro();
    // trigger 立即反映 s2 选择
    const trigger2 = _skillTrigger(env);
    ok(trigger2.textContent.indexOf('技能 1') !== -1,
      '#7b selectSession s2 (before detail): trigger immediately reflects s2 selection (got "' + trigger2.textContent + '")');
    if (deferredDetail) deferredDetail({ session: { id: 's2' }, messages: [], summary: null, task_state: null });
    await env.waitMicro();
  }
  // 删除成功清键（含空数组键）；失败不清
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['a'], s2: [] }));
    sessionsList = [{ id: 's1', title: 's1' }, { id: 's2', title: 's2' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    env.win.NAGENT.modal.confirm = () => Promise.resolve(true);
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const _item7 = env.findSessionItem('s1');
    if (_item7) _item7.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const beforeLs = env.getLocalStorage('nagent.chat.activated-skills');
    ok(!!beforeLs && beforeLs.indexOf('s1') !== -1, '#7c precondition: s1 in localStorage');
    // delete s1 (success)
    const delBtn = _sessionDeleteButton(env);
    if (delBtn) {
      // 把 confirm 设为 true；通过初始 modal 配置保证
      env.setDeleteSessionHandler(() => Promise.resolve({}));
      // delBtn 已 bind handleDelete；click 走真实路径
      delBtn.click();
      await env.waitMicro();
      await env.waitMicro();
      const ls1 = env.getLocalStorage('nagent.chat.activated-skills');
      // s1 应被清；s2 也应被清（handleDelete 总清目标键）
      ok(!!ls1 && ls1.indexOf('"s1"') === -1, '#7d delete success: s1 key removed (ls=' + ls1 + ')');
    } else {
      ok(false, '#7d delete button for s1 not found');
    }
  }
  // 失败不清
  {
    const env = loadChat();
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['a'], s2: ['b'] }));
    sessionsList = [{ id: 's1', title: 's1' }, { id: 's2', title: 's2' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    env.win.NAGENT.modal.confirm = () => Promise.resolve(true);
    await env.chat.init();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const _item7 = env.findSessionItem('s1');
    if (_item7) _item7.click();
    for (let w = 0; w < 10; w++) await env.waitMicro();
    const lsBefore = env.getLocalStorage('nagent.chat.activated-skills') || '';
    env.setDeleteSessionHandler(() => Promise.reject(new Error('forbidden')));
    const delBtn = _sessionDeleteButton(env);
    if (delBtn) {
      delBtn.click();
      await env.waitMicro();
      await env.waitMicro();
      const lsAfter = env.getLocalStorage('nagent.chat.activated-skills') || '';
      ok(lsBefore === lsAfter, '#7e delete fail: localStorage unchanged (before=' + lsBefore + ', after=' + lsAfter + ')');
    } else {
      ok(false, '#7e delete button for s1 not found');
    }
  }
  // 删当前会话 -> draft/空态
  {
    const env = loadChat();
    env.setListSkillsHandler(() => Promise.resolve({ skills: [{ name: 'a', enabled: true, readiness: 'available', source: 'user' }] }));
    env.setLocalStorage('nagent.chat.activated-skills', JSON.stringify({ s1: ['a'] }));
    sessionsList = [{ id: 's1', title: 's1' }];
    env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({ session: { id }, messages: [], summary: null, task_state: null });
    env.win.NAGENT.modal.confirm = () => Promise.resolve(true);
    await env.chat.init();
    await env.waitMicro();
    env.fireClick(env.findSessionItem('s1'));
    await env.waitMicro();
    await env.waitMicro();
    const trigger = _skillTrigger(env);
    ok(trigger.textContent.indexOf('技能 1') !== -1, '#7f current session: trigger has 1');
    env.setDeleteSessionHandler(() => Promise.resolve({}));
    const delBtn = _sessionDeleteButton(env);
    if (delBtn) {
      delBtn.click();
      await env.waitMicro();
      await env.waitMicro();
      const trigger2 = _skillTrigger(env);
      ok(!!trigger2 && (trigger2.textContent === '技能' || trigger2.textContent.indexOf('技能 0') !== -1),
        '#7g after deleting current: trigger shows 0/empty (got "' + (trigger2 && trigger2.textContent) + '")');
    } else {
      ok(false, '#7g delete button for current session not found');
    }
  }

  // === #8: 空态选择 -> createSession 成功用于首条；reject 保留 draft 可重试 ===
  {
    const env = loadChat();
    env.setListSkillsHandler(() => Promise.resolve({ skills: [
      { name: 'pick-me', enabled: true, readiness: 'available', source: 'user' },
    ] }));
    await env.chat.init();
    await env.waitMicro();
    const trigger = _skillTrigger(env);
    env.fireClick(trigger); await env.waitMicro(); await env.waitMicro();
    const popover = _skillPopover(env);
    const pill = _collectByClass(popover, 'chat-memory-option')[0];
    pill.click();
    await env.waitMicro();
    // 空态：getActivatedSkills 返回 draft
    const draft = env.chat.getActivatedSkills();
    ok(JSON.stringify(draft) === JSON.stringify(['pick-me']),
      '#8 empty state: draft selection (got ' + JSON.stringify(draft) + ')');
    // createSession success
    env.setCreateSessionHandler((id) => Promise.resolve({ id }));
    env.input.value = 'hello';
    await env.chat.send();
    await env.waitMicro();
    const completionCall = fetchCalls.find((f) => f.url === '/chat/completions');
    ok(!!completionCall, '#8 first send: /chat/completions called');
    if (completionCall) {
      const body = JSON.parse(completionCall.opts.body);
      ok(!!body && !!body.options && JSON.stringify(body.options.activated_skills) === JSON.stringify(['pick-me']),
        '#8 first send body has activated_skills (got ' + JSON.stringify(body && body.options) + ')');
    }
    const ls = env.getLocalStorage('nagent.chat.activated-skills');
    ok(!!ls && ls.indexOf('pick-me') !== -1, '#8 first send: localStorage updated for new session');
  }
  // createSession reject -> draft 保留，UI 可重试
  {
    const env = loadChat();
    env.setListSkillsHandler(() => Promise.resolve({ skills: [
      { name: 'retry-me', enabled: true, readiness: 'available', source: 'user' },
    ] }));
    await env.chat.init();
    await env.waitMicro();
    const trigger = _skillTrigger(env);
    env.fireClick(trigger); await env.waitMicro(); await env.waitMicro();
    const popover = _skillPopover(env);
    const pill = _collectByClass(popover, 'chat-memory-option')[0];
    pill.click();
    await env.waitMicro();
    // createSession reject
    env.setCreateSessionHandler(() => Promise.reject(new Error('create failed')));
    env.input.value = 'hi';
    await env.chat.send();
    await env.waitMicro();
    // draft 仍在
    const got = env.chat.getActivatedSkills();
    ok(JSON.stringify(got) === JSON.stringify(['retry-me']),
      '#8b createSession reject: draft preserved for retry (got ' + JSON.stringify(got) + ')');
    const ls = env.getLocalStorage('nagent.chat.activated-skills');
    ok(!ls || ls.indexOf('retry-me') === -1,
      '#8c createSession reject: localStorage not written (ls=' + ls + ')');
    const trigger2 = _skillTrigger(env);
    ok(trigger2.textContent.indexOf('技能 1') !== -1,
      '#8d createSession reject: trigger still shows 1 for retry (got "' + trigger2.textContent + '")');
  }
}

// ===========================================================================
// T5(Run footer): 对话 Turn 底部 Run 轻量入口（结果 · 耗时 · 工具 + 详情/Trace）
// ===========================================================================
function _messageFooters(env) {
  return _collectByClass(env.messageStack, 'chat-run-footer');
}
function _runFooters(env) {
  return _messageFooters(env).filter((footer) => _runFooterLinks(footer).some(
    (link) => link.getAttribute('aria-label') === '查看执行详情'
  ));
}
function _runFooterLinks(footer) {
  return footer && typeof footer.querySelectorAll === 'function' ? footer.querySelectorAll('a') : [];
}
function _runFooterTraceLink(footer) {
  return _runFooterLinks(footer).find((a) => a.getAttribute('aria-label') === '查看 Trace') || null;
}
async function _settle(env, n) { for (let i = 0; i < (n || 10); i++) await env.waitMicro(); }
function _runRow(overrides) {
  return Object.assign({
    id: 'run-1', session_id: 's1', status: 'completed', source: 'dashboard',
    execution_kind: 'chat', started_at: 1000, ended_at: 1018, end_reason: 'stop',
    error: null, tool_call_count: 3,
  }, overrides || {});
}
// 建会话并选中：使 currentSessionId 就绪（footer 批次逻辑依赖当前会话归属校验）
async function _prepareSession(env, id, messages) {
  sessionsList = [{ id, title: id }];
  env.win.NAGENT.api.getSessionDetail = () => Promise.resolve({ session: { id }, messages, summary: null, task_state: null });
  await env.chat.init();
  await _settle(env);
  env.fireClick(env.findSessionItem(id));
  await _settle(env);
}

// --- 渲染范围 / 状态映射 / 耗时与计数格式 / 失败降级 ---
async function testRunFooterRenderAndFormat() {
  // 通用 footer：user/system/assistant 均有气泡外时间；无 Run 身份不显示操作，无时间不补造。
  const genericEnv = loadChat();
  genericEnv.setListSessionRunsHandler(() => Promise.resolve([]));
  await _prepareSession(genericEnv, 's-generic', [
    { id: 'gu', role: 'user', content: '问', created_at: '2020-01-01T00:00:00Z' },
    { id: 'gs', role: 'system', name: 'ui.notice', content: '系统', created_at: '2020-01-01T00:00:01Z' },
    { id: 'ga', role: 'assistant', content: '流式片段', created_at: null },
  ]);
  const genericFooters = _messageFooters(genericEnv);
  ok(genericFooters.length === 3, 'all message bubble types receive generic footer');
  ok(genericFooters[0].querySelector('.chat-run-footer__time').textContent === '2020/1/1 08:00'
    && genericFooters[1].querySelector('.chat-run-footer__time').textContent !== '',
    'user and system timestamps render in footer using UTC+8');
  ok(genericFooters[2].querySelector('.chat-run-footer__time').textContent === '',
    'transient message without created_at does not invent timestamp');
  ok(_runFooters(genericEnv).length === 0, 'generic footers do not invent Run actions');

  // 1. 仅 assistant+run_id 渲染 Run 操作；同 Run 多消息各一个操作组
  let env = loadChat();
  const msgs2 = [
    { id: 'm1', role: 'user', content: '问题' },
    { id: 'm2', role: 'assistant', content: '回答一', run_id: 'run-1', created_at: '2020-01-01T00:00:00Z' },
    { id: 'm3', role: 'assistant', content: '回答二', run_id: 'run-1' },
    { id: 'm4', role: 'assistant', content: '无身份' },
    { id: 'm5', role: 'system', name: 'ui.task_lifecycle', content: '[任务状态] 运行' },
  ];
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
  env.setGetSessionRunHandler(() => Promise.resolve(_runRow({ id: 'run-1', trace_links: [] })));
  await _prepareSession(env, 's1', msgs2);
  let footers = _runFooters(env);
  ok(footers.length === 2, 'same run on 2 assistant messages -> exactly 2 footers (got ' + footers.length + ')');
  const f0 = footers[0];
  const bubble = env.messageStack.querySelector('.msg.assistant');
  ok(!!bubble && bubble.parentNode === f0.parentNode && bubble.parentNode.classList.contains('chat-turn')
    && bubble.parentNode !== env.messageStack,
    'footer is outside assistant bubble in a shared turn wrapper');
  ok(f0 && f0.querySelector('.chat-run-footer__time').textContent === '2020/1/1 08:00'
    && f0.textContent.indexOf('已完成') === -1 && f0.textContent.indexOf('18秒') === -1
    && f0.textContent.indexOf('工具调用') === -1 && _runFooterLinks(f0).length === 1,
    'Run reconciliation preserves timestamp and shows no visible run metadata');
  const detailAction = _runFooterLinks(f0)[0];
  ok(detailAction.getAttribute('aria-label') === '查看执行详情'
    && detailAction.getAttribute('title') === '详情'
    && !!detailAction.querySelector('svg'),
    'Details icon has accessible label, native hover tooltip and SVG');
  ok(footers[1] && _runFooterLinks(footers[1]).length === 1, 'same-run footers keep identical icon-only actions');
  ok(_collectByClass(env.messageStack, 'msg').filter((m) => m.className.indexOf('user') !== -1
    && _collectByClass(m, 'chat-run-footer').length).length === 0, 'user message has no footer');

  // 2. 重复刷新：每消息最多一个 footer
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: msgs2, summary: null, task_state: null });
  await _settle(env);
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: msgs2, summary: null, task_state: null }, { partialMessages: true });
  await _settle(env);
  ok(_runFooters(env).length === 2, 'repeat refresh keeps one footer per message (got ' + _runFooters(env).length + ')');

  // 3. 状态全映射及未知
  env = loadChat();
  let statusRow = _runRow({ id: 'r', status: 'completed' });
  env.setListSessionRunsHandler(() => Promise.resolve([statusRow]));
  env.setGetSessionRunHandler(() => Promise.resolve(statusRow));
  const statusMsgs = [{ id: 'm', role: 'assistant', content: '回复', run_id: 'r' }];
  await _prepareSession(env, 's1', statusMsgs);
  const statusCases = [
    ['completed', '已完成'], ['failed', '失败'], ['cancelled', '已取消'], ['interrupted', '已中断'],
    ['running', '运行中'], ['pending', '运行中'], ['not_started', '未开始'], ['mystery', '未知'],
  ];
  for (const [status, label] of statusCases) {
    statusRow = _runRow({ id: 'r', status });
    await env.chat.applySessionDetail({ session: { id: 's1' }, messages: statusMsgs, summary: null, task_state: null });
    await _settle(env);
    const fs = _runFooters(env);
    ok(fs.length === 1 && fs[0].textContent.trim() === '' && fs[0].textContent.indexOf(label) === -1,
      'status ' + status + ' remains out of icon-only footer');
  }

  // 4. 耗时（Unix 秒差取整 / 零 / 倒序 / 非法 / 非有限）与计数（0/正数/缺失/负数/分数/Infinity/NaN/字符串）
  env = loadChat();
  let fmtRow = _runRow({ id: 'r' });
  env.setListSessionRunsHandler(() => Promise.resolve([fmtRow]));
  env.setGetSessionRunHandler(() => Promise.resolve(fmtRow));
  const fmtMsgs = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'r' }];
  await _prepareSession(env, 's1', fmtMsgs);
  const noCount = _runRow({ id: 'r' });
  delete noCount.tool_call_count;
  const fmtCases = [
    [_runRow({ id: 'r', started_at: 1000, ended_at: 1018.9, tool_call_count: 2 }), '18秒', '工具调用 2 次'],
    [_runRow({ id: 'r', started_at: 1000, ended_at: 1000, tool_call_count: 0 }), '0秒', '未调用工具'],
    [_runRow({ id: 'r', started_at: 1010, ended_at: 1000, tool_call_count: 1 }), '—', '工具调用 1 次'],
    [_runRow({ id: 'r', started_at: null, ended_at: 1000, tool_call_count: 1 }), '—', '工具调用 1 次'],
    [_runRow({ id: 'r', started_at: 1000, ended_at: null, tool_call_count: 1 }), '—', '工具调用 1 次'],
    [_runRow({ id: 'r', started_at: '1000', ended_at: 1000, tool_call_count: 1 }), '—', '工具调用 1 次'],
    [_runRow({ id: 'r', started_at: NaN, ended_at: 1000, tool_call_count: 1 }), '—', '工具调用 1 次'],
    [_runRow({ id: 'r', started_at: Infinity, ended_at: Infinity, tool_call_count: 1 }), '—', '工具调用 1 次'],
    [_runRow({ id: 'r', tool_call_count: '3' }), '18秒', '工具调用数未知'],
    [_runRow({ id: 'r', tool_call_count: 1.5 }), '18秒', '工具调用数未知'],
    [_runRow({ id: 'r', tool_call_count: -1 }), '18秒', '工具调用数未知'],
    [_runRow({ id: 'r', tool_call_count: NaN }), '18秒', '工具调用数未知'],
    [_runRow({ id: 'r', tool_call_count: Infinity }), '18秒', '工具调用数未知'],
    [noCount, '18秒', '工具调用数未知'],
  ];
  for (const [row, dur, tool] of fmtCases) {
    fmtRow = row;
    await env.chat.applySessionDetail({ session: { id: 's1' }, messages: fmtMsgs, summary: null, task_state: null });
    await _settle(env);
    const fs = _runFooters(env);
    const text = fs[0] ? fs[0].textContent : '';
    ok(text.trim() === '' && text.indexOf(dur) === -1 && text.indexOf(tool) === -1,
      'duration/tool formatting does not leak into icon-only footer');
  }

  // 5. 初次失败隐藏；恢复渲染；刷新失败保留快照；成功空列表清除；列表持续失败时详情兜底
  env = loadChat();
  let listFail = true;
  let detailFail = true;
  const oneMsg = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'r' }];
  env.setListSessionRunsHandler(() => listFail ? Promise.reject(new Error('boom')) : Promise.resolve([_runRow({ id: 'r' })]));
  env.setGetSessionRunHandler(() => detailFail ? Promise.reject(new Error('boom')) : Promise.resolve(_runRow({ id: 'r', trace_links: [] })));
  await _prepareSession(env, 's1', oneMsg);
  ok(_runFooters(env).length === 0, 'first list+detail failure hides footer (got ' + _runFooters(env).length + ')');
  listFail = false;
  detailFail = false;
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: oneMsg, summary: null, task_state: null });
  await _settle(env);
  ok(_runFooters(env).length === 1, 'recovered batch renders footer');
  const snapshotText = _runFooters(env)[0].textContent;
  listFail = true;
  detailFail = true;
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: oneMsg, summary: null, task_state: null });
  await _settle(env);
  ok(_runFooters(env).length === 1 && _runFooters(env)[0].textContent === snapshotText,
    'refresh failure keeps last snapshot footer (got ' + JSON.stringify(_runFooters(env)[0] && _runFooters(env)[0].textContent) + ')');
  env.setListSessionRunsHandler(() => Promise.resolve([]));
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: oneMsg, summary: null, task_state: null });
  await _settle(env);
  ok(_runFooters(env).length === 0, 'successful empty list clears orphan footer (got ' + _runFooters(env).length + ')');
  // 列表首次失败（无成功快照）时详情快照兜底渲染；新 env 避免上一成功空列表快照干扰
  env = loadChat();
  env.setListSessionRunsHandler(() => Promise.reject(new Error('boom')));
  env.setGetSessionRunHandler(() => Promise.resolve(_runRow({ id: 'r', trace_links: [] })));
  const oneMsg2 = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'r' }];
  await _prepareSession(env, 's1', oneMsg2);
  ok(_runFooters(env).length === 1, 'list first failure: detail snapshot renders footer (got ' + _runFooters(env).length + ')');
}

// --- 直播：终结 chunk 身份捕获 / 强制刷新 / 无身份不猜测 ---
async function testRunFooterLiveCapture() {
  // 终结 chunk 携带 run_id → 强制 getSessionRun，陈旧 running 缓存不可代替请求
  let env = loadChat();
  sessionsList = [];
  let detailMsgs = [];
  let sessionDetailCalls = 0;
  env.win.NAGENT.api.getSessionDetail = () => {
    sessionDetailCalls++;
    return Promise.resolve({ session: { id: createdSessionId }, messages: detailMsgs, summary: null, task_state: null });
  };
  let listRows = [];
  env.setListSessionRunsHandler(() => Promise.resolve(listRows));
  let detailRow = _runRow({ id: 'run-live', status: 'running', ended_at: null, tool_call_count: 1 });
  let runDetailCalls = 0;
  env.setGetSessionRunHandler(() => { runDetailCalls++; return Promise.resolve(detailRow); });

  // 第一轮：终结 chunk 不带 run_id（旧形态）→ 仅消息重载；批次把 running 快照写入缓存
  detailMsgs = [
    { id: 'u1', role: 'user', content: '第一问' },
    { id: 'a1', role: 'assistant', content: '第一答', run_id: 'run-live' },
  ];
  listRows = [_runRow({ id: 'run-live', status: 'running', ended_at: null, tool_call_count: 1 })];
  env.input.value = '第一问';
  const textChunk = 'data: ' + JSON.stringify({ choices: [{ delta: { content: '第一答' } }] }) + '\n\n';
  const doneNoId = 'data: ' + JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] }) + '\n\n';
  env.enqueueSseChunks([textChunk, doneNoId]);
  await env.chat.send();
  await _settle(env);
  ok(runDetailCalls === 1, 'turn1: batch queued one detail request for running row (got ' + runDetailCalls + ')');
  let footers = _runFooters(env);
  ok(footers.length === 1 && footers[0].dataset.runId === 'run-live' && footers[0].textContent.trim() === '',
    'turn1: running list row still produces icon-only footer');
  const callsAfterTurn1 = runDetailCalls;
  // 非终态详情跨批次重排：新批次重新请求 running Run（终态缓存才跳过）
  await env.chat.applySessionDetail({ session: { id: createdSessionId }, messages: detailMsgs, summary: null, task_state: null });
  await _settle(env);
  ok(runDetailCalls === callsAfterTurn1 + 1,
    'non-terminal detail requeued on next batch (got ' + runDetailCalls + ', want ' + (callsAfterTurn1 + 1) + ')');

  // 第二轮：终结 chunk 携带 run_id → 强制刷新（缓存命中也不替代请求）→ 完成态覆盖
  detailRow = _runRow({ id: 'run-live', status: 'completed', ended_at: 1018, tool_call_count: 4 });
  env.input.value = '第二问';
  const doneWithId = 'data: ' + JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], run_id: 'run-live' }) + '\n\n';
  env.enqueueSseChunks([textChunk, doneWithId]);
  await env.chat.send();
  await _settle(env);
  ok(runDetailCalls > callsAfterTurn1, 'terminal chunk with run_id forces fresh getSessionRun (stale running cache not reused)');
  footers = _runFooters(env);
  ok(footers.length === 1 && footers[0].dataset.runId === 'run-live' && footers[0].textContent.trim() === '',
    'turn2: forced refresh preserves matched icon-only footer');
  ok(sessionDetailCalls >= 2, 'stream end reloads persisted messages (got ' + sessionDetailCalls + ')');

  // 中途 chunk run_id 不捕获；ERROR 无身份不猜测（仅重载）
  env = loadChat();
  sessionsList = [];
  let reloads2 = 0;
  env.win.NAGENT.api.getSessionDetail = () => {
    reloads2++;
    return Promise.resolve({ session: { id: createdSessionId }, messages: [], summary: null, task_state: null });
  };
  env.setListSessionRunsHandler(() => Promise.resolve([]));
  let runDetailCalls2 = 0;
  env.setGetSessionRunHandler(() => { runDetailCalls2++; return Promise.resolve(_runRow({ id: 'x' })); });
  const midWithId = 'data: ' + JSON.stringify({ choices: [{ index: 0, delta: { content: '部分' } }], run_id: 'run-x' }) + '\n\n';
  const errNoId = 'data: ' + JSON.stringify({ choices: [{ index: 0, delta: { content: 'err' }, finish_reason: 'error' }] }) + '\n\n';
  env.input.value = 'hi';
  env.enqueueSseChunks([midWithId, errNoId]);
  await env.chat.send();
  await _settle(env);
  ok(runDetailCalls2 === 0, 'mid-stream/ERROR without identity: no guessed getSessionRun (got ' + runDetailCalls2 + ')');
  ok(reloads2 >= 1, 'error stream end still reloads messages (got ' + reloads2 + ')');

  // 断连（fetch reject）：无身份仅重载
  env = loadChat();
  sessionsList = [];
  let reloads3 = 0;
  env.win.NAGENT.api.getSessionDetail = () => {
    reloads3++;
    return Promise.resolve({ session: { id: createdSessionId }, messages: [], summary: null, task_state: null });
  };
  env.setListSessionRunsHandler(() => Promise.resolve([]));
  let runDetailCalls3 = 0;
  env.setGetSessionRunHandler(() => { runDetailCalls3++; return Promise.resolve(_runRow({ id: 'x' })); });
  env.setFetchHandler('/chat/completions', () => Promise.reject(new Error('network_failed')));
  env.input.value = 'hi';
  await env.chat.send();
  await _settle(env);
  ok(runDetailCalls3 === 0, 'disconnected stream without identity: no getSessionRun (got ' + runDetailCalls3 + ')');
  ok(reloads3 >= 1, 'disconnected stream end reloads messages (got ' + reloads3 + ')');
}

// --- 会话 A→B→A / 删除 / 旧批次迟到 ---
async function testRunFooterSessionIsolation() {
  let env = loadChat();
  let resolveAList = null;
  let aListCalls = 0;
  env.setListSessionRunsHandler((id) => {
    if (id === 'sA') {
      aListCalls++;
      if (aListCalls === 1) return new Promise((res) => { resolveAList = res; });
      return Promise.resolve([_runRow({ id: 'run-a' })]);
    }
    return Promise.resolve([_runRow({ id: 'run-b', status: 'failed', ended_at: 1020, tool_call_count: 0, error: 'x' })]);
  });
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({
    id: runId, status: runId === 'run-a' ? 'completed' : 'failed', ended_at: 1019, trace_links: [],
  })));
  sessionsList = [{ id: 'sA', title: 'sA' }, { id: 'sB', title: 'sB' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({
    session: { id },
    messages: [{ id: 'm-' + id, role: 'assistant', content: 'x', run_id: id === 'sA' ? 'run-a' : 'run-b' }],
    summary: null, task_state: null,
  });
  await env.chat.init();
  await _settle(env);
  env.fireClick(env.findSessionItem('sA'));
  await _settle(env, 4);
  env.fireClick(env.findSessionItem('sB'));
  await _settle(env);
  let footers = _runFooters(env);
  ok(footers.length === 1 && footers[0].dataset.runId === 'run-b',
    'switch to B before A list resolves: B footer identity correct');
  if (resolveAList) resolveAList([_runRow({ id: 'run-a' })]);
  await _settle(env);
  footers = _runFooters(env);
  ok(footers.length === 1 && footers[0].dataset.runId === 'run-b',
    'late A list resolution does not corrupt B footer identity');
  env.fireClick(env.findSessionItem('sA'));
  await _settle(env);
  footers = _runFooters(env);
  ok(footers.length === 1 && footers[0].dataset.runId === 'run-a',
    'switch back to A renders A footer identity');

  // 迟到详情不串台；删除当前会话清 footer 并使批次失效
  env = loadChat();
  let resolveBDetail = null;
  env.setListSessionRunsHandler((id) => Promise.resolve([_runRow({ id: id === 'sB' ? 'run-b' : 'run-c' })]));
  env.setGetSessionRunHandler((id, runId) => {
    if (runId === 'run-b') return new Promise((res) => { resolveBDetail = res; });
    return Promise.resolve(_runRow({ id: runId, trace_links: [] }));
  });
  sessionsList = [{ id: 'sB', title: 'sB' }, { id: 'sC', title: 'sC' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({
    session: { id },
    messages: [{ id: 'm-' + id, role: 'assistant', content: 'x', run_id: id === 'sB' ? 'run-b' : 'run-c' }],
    summary: null, task_state: null,
  });
  await env.chat.init();
  await _settle(env);
  env.fireClick(env.findSessionItem('sB'));
  await _settle(env, 4);
  env.fireClick(env.findSessionItem('sC'));
  await _settle(env);
  let footers2 = _runFooters(env);
  ok(footers2.length === 1 && footers2[0].dataset.runId === 'run-c',
    'C footer from own row identity');
  if (resolveBDetail) resolveBDetail(_runRow({ id: 'run-b', status: 'cancelled' }));
  await _settle(env);
  footers2 = _runFooters(env);
  ok(footers2.length === 1 && footers2[0].dataset.runId === 'run-c',
    'late B detail does not corrupt C footer identity');
  env.win.NAGENT.modal.confirm = () => Promise.resolve(true);
  env.setDeleteSessionHandler(() => Promise.resolve({}));
  // 定位当前会话（sC）条目内的删除按钮，避免误删其他会话
  let delBtn = null;
  for (const it of env.document.querySelectorAll('.session-item')) {
    if (!(it.dataset && it.dataset.sessionId === 'sC')) continue;
    delBtn = (function visit(n) {
      for (const c of (n._kids || [])) {
        if (c.tagName === 'BUTTON' && c.getAttribute('aria-label') === '删除会话') return c;
        const r = visit(c);
        if (r) return r;
      }
      return null;
    })(it);
    break;
  }
  ok(!!delBtn, 'delete button found for current session');
  if (delBtn) {
    delBtn.click();
    await _settle(env);
    ok(_runFooters(env).length === 0, 'deleting current session clears footers (got ' + _runFooters(env).length + ')');
  }
}

// --- Trace：队列去重/并发上限/排序筛选/配置状态/URL 合同 ---
async function testRunFooterTraceAndQueue() {
  // 有界队列：并发上限 4、同 Run 去重、完成后补位
  let env = loadChat();
  const runIds = ['r1', 'r2', 'r3', 'r4', 'r5', 'r6'];
  const queueMsgs = runIds.map((rid, i) => ({ id: 'm' + i, role: 'assistant', content: 'x' + i, run_id: rid }));
  queueMsgs.push({ id: 'm-dup', role: 'assistant', content: 'dup', run_id: 'r1' });
  let inflight = 0;
  let maxInflight = 0;
  let started = [];
  const resolvers = {};
  const done = {};
  env.setGetSessionRunHandler((id, runId) => {
    started.push(runId);
    inflight++;
    maxInflight = Math.max(maxInflight, inflight);
    return new Promise((res) => {
      resolvers[runId] = () => {
        if (done[runId]) return;
        done[runId] = true;
        inflight--;
        res(_runRow({ id: runId, trace_links: [] }));
      };
    });
  });
  env.setListSessionRunsHandler(() => Promise.resolve(runIds.map((rid) => _runRow({ id: rid }))));
  await _prepareSession(env, 's1', queueMsgs);
  await _settle(env, 6);
  ok(started.length === 4, 'bounded queue starts exactly 4 detail requests (got ' + started.length + ')');
  ok(maxInflight <= 4, 'concurrency bounded at 4 (got ' + maxInflight + ')');
  ok(started.filter((r) => r === 'r1').length === 1, 'same run from multiple messages merged into one request');
  resolvers.r1();
  resolvers.r2();
  await _settle(env, 4);
  ok(started.length === 6, 'completed requests release queue slots (got ' + started.length + ')');
  ok(maxInflight <= 4, 'still bounded after refill (got ' + maxInflight + ')');
  Object.keys(resolvers).forEach((k) => resolvers[k]());
  await _settle(env, 6);
  ok(_runFooters(env).length === 7, 'all candidate messages have footers (got ' + _runFooters(env).length + ')');

  // Trace 排序/筛选/首条 + 空关联降级 + 不借用邻近 Run + 无需点击详情
  env = loadChat();
  const ID_UPPER = 'A1'.repeat(16);
  const ID_LOWER = 'a1'.repeat(16);
  const ID_OTHER = 'b2'.repeat(16);
  const ID_ZERO = '0'.repeat(32);
  const detailByRun = {
    'run-t': _runRow({ id: 'run-t', trace_links: [
      { trace_id: ID_OTHER, created_at: '2026-01-02T00:00:00Z', id: 'l2' },
      { trace_id: 'ZZZ-not-hex', created_at: '2026-01-01T00:00:00Z', id: 'l0' },
      { trace_id: ID_UPPER, created_at: '2026-01-01T00:00:00Z', id: 'l1' },
      { trace_id: ID_ZERO, created_at: '2026-01-03T00:00:00Z', id: 'l3' },
    ] }),
    'run-e': _runRow({ id: 'run-e', trace_links: [] }),
  };
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(detailByRun[runId] || _runRow({ id: runId, trace_links: [] })));
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-t' }), _runRow({ id: 'run-e' })]));
  env.setGetOtelLinksHandler(() => Promise.resolve({ config: { trace_base_url: 'https://grafana.example/grafana/explore?orgId=1' }, version: 1, overridden_fields: [] }));
  const tMsgs = [
    { id: 'mt', role: 'assistant', content: 't', run_id: 'run-t' },
    { id: 'me', role: 'assistant', content: 'e', run_id: 'run-e' },
  ];
  await _prepareSession(env, 's1', tMsgs);
  const tf = _runFooters(env);
  ok(tf.length === 2, 'two footers rendered (got ' + tf.length + ')');
  const traceLink = _runFooterTraceLink(tf[0]);
  ok(!!traceLink, 'Trace link appears without clicking 详情 (got footer ' + JSON.stringify(tf[0] && tf[0].textContent) + ')');
  ok(traceLink && traceLink.getAttribute('aria-label') === '查看 Trace'
    && traceLink.getAttribute('title') === 'Trace' && !!traceLink.querySelector('svg'),
    'Trace icon has accessible label, native hover tooltip and SVG');
  const href = traceLink ? traceLink.href : '';
  ok(href.indexOf(encodeURIComponent(ID_LOWER)) !== -1 && href.indexOf(ID_UPPER) === -1,
    'trace id normalized to lowercase in link (got ' + href + ')');
  ok(href.indexOf('traceql') !== -1 && href.indexOf('nagent-tempo') !== -1, 'link carries traceql panes with nagent-tempo datasource');
  ok(href.indexOf(encodeURIComponent('ZZZ-not-hex')) === -1 && href.indexOf(encodeURIComponent(ID_ZERO)) === -1
    && href.indexOf(encodeURIComponent(ID_OTHER)) === -1, 'invalid/all-zero/other-run trace ids absent');
  const ef = tf[1];
  ok(ef && ef.textContent.trim() === '' && !_runFooterTraceLink(ef),
    'empty valid links omit Trace action without footer text');
  ok(traceLink && traceLink.target === '_blank' && traceLink.rel === 'noopener', 'Trace link opens new window with noopener');

  // 配置失败重试 / 配置更新 / 未配置
  env = loadChat();
  let otelFail = true;
  let baseUrl = 'https://grafana.example/grafana/explore?orgId=1';
  env.setGetOtelLinksHandler(() => otelFail ? Promise.reject(new Error('cfg')) : Promise.resolve({ config: { trace_base_url: baseUrl } }));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [{ trace_id: ID_LOWER, created_at: '2026-01-01T00:00:00Z', id: 'l1' }] })));
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'r1' })]));
  const oneMsg = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'r1' }];
  await _prepareSession(env, 's1', oneMsg);
  let cf = _runFooters(env)[0];
  ok(cf && cf.textContent.trim() === '' && !_runFooterTraceLink(cf),
    'config failure omits Trace action without footer text');
  otelFail = false;
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: oneMsg, summary: null, task_state: null });
  await _settle(env);
  cf = _runFooters(env)[0];
  const link1 = _runFooterTraceLink(cf);
  ok(!!link1, 'next batch retries config -> Trace link appears (got ' + JSON.stringify(cf && cf.textContent) + ')');
  baseUrl = 'https://grafana2.example/explore?orgId=7';
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: oneMsg, summary: null, task_state: null });
  await _settle(env);
  const link2 = _runFooterTraceLink(_runFooters(env)[0]);
  ok(!!link2 && link2.href !== (link1 && link1.href) && link2.href.indexOf('grafana2.example') !== -1,
    'config update picked up on next batch (got ' + (link2 && link2.href) + ')');
  env.setGetOtelLinksHandler(() => Promise.resolve({ config: {} }));
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: oneMsg, summary: null, task_state: null });
  await _settle(env);
  cf = _runFooters(env)[0];
  ok(cf && cf.textContent.trim() === '' && !_runFooterTraceLink(cf),
    'valid links without base url omit Trace action without footer text');

  // buildTraceUrl 合同：与 tests/e2e/result_report.py::trace_url 一致
  const btu = env.chat.buildTraceUrl;
  ok(typeof btu === 'function', 'buildTraceUrl exposed');
  const tid = 'ABCDEF0123456789ABCDEF0123456789';
  const base = 'http://grafana.example/grafana/explore?orgId=1&left=%7Bold%7D&panes=%7Bold%7D&schemaVersion=9&panes=dup#frag';
  const panesJson = JSON.stringify({ trace: {
    datasource: 'nagent-tempo',
    queries: [{ refId: 'A', queryType: 'traceql', query: tid.toLowerCase(), datasource: { type: 'tempo', uid: 'nagent-tempo' } }],
    range: { from: 'now-1h', to: 'now' },
  } });
  const expected = 'http://grafana.example/grafana/explore?orgId=1&schemaVersion=1&panes=' + encodeURIComponent(panesJson);
  ok(btu(base, tid) === expected, 'trace url mirrors result_report.py::trace_url (got ' + btu(base, tid) + ')');
  ok(btu('https://h/p', '0'.repeat(32)) === null, 'all-zero trace id rejected');
  ok(btu('https://h/p', 'abc') === null, 'short trace id rejected');
  ok(btu('https://h/p', 'zzz' + 'f'.repeat(29)) === null, 'non-hex trace id rejected');
  ok(btu('ftp://h/p', tid) === null, 'non-http(s) base rejected');
  ok(btu('http://user:pass@h/p', tid) === null, 'userinfo base rejected');
  ok(btu('not a url', tid) === null, 'unparsable base rejected');
  ok(btu('', tid) === null, 'empty base rejected');
  const got2 = btu('https://g.example/o/org-9/grafana/explore?orgId=9', 'B'.repeat(32));
  ok(!!got2 && got2.indexOf('https://g.example/o/org-9/grafana/explore') === 0
    && got2.indexOf('explore/explore') === -1 && got2.indexOf('#') === -1 && got2.indexOf('orgId=9') !== -1,
    'subpath/orgId preserved, no explore append, hash cleared (got ' + got2 + ')');
}

// --- 批次并行：runs 请求在消息渲染完成前发起；迟到消息仍触发对账 ---
async function testRunFooterBatchParallelPrefetch() {
  const env = loadChat();
  let stackMsgCountAtListFetch = -1;
  env.setListSessionRunsHandler(() => {
    // 在请求发起时刻记录消息栈状态：批次先于渲染开启时该值为 0
    stackMsgCountAtListFetch = _collectByClass(env.messageStack, 'msg').length;
    return Promise.resolve([_runRow({ id: 'run-p', trace_links: [] })]);
  });
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  let releaseDetail = null;
  const gate = new Promise((res) => { releaseDetail = res; });
  env.win.NAGENT.api.getSessionDetail = () => gate.then(() => Promise.resolve({
    session: { id: 's1' },
    messages: [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-p' }],
    summary: null, task_state: null,
  }));
  sessionsList = [{ id: 's1', title: 's1' }];
  await env.chat.init();
  await _settle(env);
  env.fireClick(env.findSessionItem('s1'));
  await env.waitMicro();
  ok(env.getListSessionRunsCalls().length === 0,
    'runs fetch waits for session detail resolution before batch start (got ' + env.getListSessionRunsCalls().length + ')');
  releaseDetail();
  await _settle(env);
  ok(stackMsgCountAtListFetch === 0,
    'runs list fetch initiated before message rendering completes (msg nodes at fetch time: ' + stackMsgCountAtListFetch + ')');
  ok(_runFooters(env).length === 1, 'late-arrival reconcile still renders footer (got ' + _runFooters(env).length + ')');
}

// --- 账本未注入：无 listSessionRuns/getSessionRun/getOtelLinks 时对话行为完全不变 ---
async function testRunFooterLedgerAbsent() {
  const env = loadChat();
  delete env.win.NAGENT.api.listSessionRuns;
  delete env.win.NAGENT.api.getSessionRun;
  delete env.win.NAGENT.api.getOtelLinks;
  let toolCallsCalls = 0;
  const baseToolCalls = env.win.NAGENT.api.getSessionToolCalls;
  env.win.NAGENT.api.getSessionToolCalls = () => { toolCallsCalls++; return baseToolCalls(); };
  await _prepareSession(env, 's1', [
    { id: 'm1', role: 'user', content: '问' },
    { id: 'm2', role: 'assistant', content: '答' },
  ]);
  ok(_runFooters(env).length === 0, 'ledger absent: zero footers (got ' + _runFooters(env).length + ')');
  const bubbleCount = _collectByClass(env.messageStack, 'msg').length;
  ok(bubbleCount === 2, 'ledger absent: messages render normally (got ' + bubbleCount + ')');
  ok(toolCallsCalls >= 1, 'ledger absent: tool-calls loading still works (got ' + toolCallsCalls + ')');
  let threw = null;
  try {
    await env.chat.applySessionDetail({ session: { id: 's1' }, messages: [{ id: 'm2', role: 'assistant', content: '答' }], summary: null, task_state: null });
    await _settle(env);
  } catch (error) { threw = error; }
  ok(!threw, 'ledger absent: session refresh does not throw' + (threw ? ' (' + threw.message + ')' : ''));
  ok(_runFooters(env).length === 0, 'ledger absent: still zero footers after refresh');
  // 直播路径：终结 chunk 携带 run_id 但账本 API 缺失，收尾不得抛错、不得尝试详情请求
  let threwSend = null;
  env.input.value = 'hi';
  env.enqueueSseChunks(['data: ' + JSON.stringify({ choices: [{ index: 0, delta: { content: 'x' }, finish_reason: 'stop' }], run_id: 'run-x' }) + '\n\n']);
  try { await env.chat.send(); await _settle(env); } catch (error) { threwSend = error; }
  ok(!threwSend, 'ledger absent: live stream completion does not throw' + (threwSend ? ' (' + threwSend.message + ')' : ''));
  ok(_runFooters(env).length === 0, 'ledger absent: no footer after live stream');
}

// --- 详情队列韧性：force 抢占在途请求（去重窗口完好）+ reconcile 抛错不死锁 ---
async function testRunFooterDetailQueueResilience() {
  // (a) force 抢占：陈旧响应不误删新 holder、不发重复请求、getRunDetail 以新数据结算
  let env = loadChat();
  let calls = [];
  let releaseA = null;
  const gateA = new Promise((res) => { releaseA = res; });
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-s', status: 'running', ended_at: null })]));
  env.setGetSessionRunHandler((id, runId) => {
    calls.push(runId);
    if (calls.length === 1) return gateA.then(() => _runRow({ id: runId, status: 'running', ended_at: null }));
    return Promise.resolve(_runRow({ id: runId, status: 'completed', ended_at: 1018 }));
  });
  await _prepareSession(env, 's1', [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-s' }]);
  ok(calls.length === 1, 'supersede: initial detail request issued (got ' + calls.length + ')');
  const forcedPromise = env.chat.getRunDetail('s1', 'run-s', { force: true });
  await env.waitMicro();
  ok(calls.length === 2, 'supersede: forced request issued while old one still in flight (got ' + calls.length + ')');
  releaseA();
  const freshRow = await forcedPromise;
  await _settle(env);
  ok(calls.length === 2, 'supersede: no duplicate request after stale resolution (got ' + calls.length + ')');
  ok(!!freshRow && freshRow.status === 'completed',
    'getRunDetail resolves with fresh forced row (got ' + JSON.stringify(freshRow && freshRow.status) + ')');
  const sf = _runFooters(env);
  ok(sf.length === 1 && sf[0].dataset.runId === 'run-s' && sf[0].textContent.trim() === '',
    'supersede: fresh forced data retains icon-only footer');

  // (b) reconcile 抛错一次：finally 仍释放槽位并补位，队列不死锁
  env = loadChat();
  const runIds2 = ['q1', 'q2', 'q3', 'q4', 'q5', 'q6'];
  const resolvers2 = {};
  let inflightCalls = [];
  env.setGetSessionRunHandler((id, runId) => {
    inflightCalls.push(runId);
    return new Promise((res) => { resolvers2[runId] = () => res(_runRow({ id: runId, trace_links: [] })); });
  });
  env.setListSessionRunsHandler(() => Promise.resolve(runIds2.map((rid) => _runRow({ id: rid }))));
  const qMsgs = runIds2.map((rid, i) => ({ id: 'q' + i, role: 'assistant', content: 'x', run_id: rid }));
  await _prepareSession(env, 's1', qMsgs);
  await _settle(env, 4);
  ok(inflightCalls.length === 4, 'resilience: queue starts 4 requests (got ' + inflightCalls.length + ')');
  // 仅下一次 reconcile 抛错：reconcile 是唯一对 messageStack 调 querySelectorAll('.msg.assistant') 的代码
  const qStack = env.messageStack;
  const origQSA = qStack.querySelectorAll;
  let reconcileThrew = false;
  qStack.querySelectorAll = function (sel) {
    if (!reconcileThrew && sel === '.msg.assistant') { reconcileThrew = true; throw new Error('reconcile-boom'); }
    return origQSA.call(this, sel);
  };
  resolvers2.q2();
  await _settle(env, 4);
  ok(reconcileThrew, 'resilience: reconcile threw once during detail completion');
  ok(inflightCalls.length >= 5,
    'resilience: throw did not deadlock queue, waiting request pumped (got ' + inflightCalls.length + ')');
  qStack.querySelectorAll = origQSA;
  Object.keys(resolvers2).forEach((k) => resolvers2[k]());
  await _settle(env, 8);
  ok(inflightCalls.length === 6, 'resilience: all queued requests completed (got ' + inflightCalls.length + ')');
  ok(_runFooters(env).length === 6, 'resilience: footers intact after reconcile throw (got ' + _runFooters(env).length + ')');
}

// ===========================================================================
// T6(Run 详情 tab): 点 footer 详情 -> 展开侧栏 + 激活 Run tab + 详情读取归属
// ===========================================================================
function _runFooterDetailLink(footer) {
  return _runFooterLinks(footer).find((a) => a.getAttribute('aria-label') === '查看执行详情') || null;
}
function _clickRunDetail(env, footer) {
  const link = _runFooterDetailLink(footer);
  ok(!!link, 'footer has 详情 link');
  if (link) env.fireClick(link);
}
function _detailMetaValue(detail, label) {
  const rows = detail.querySelectorAll('.run-detail__meta-row');
  for (const row of rows) {
    const k = row.querySelector('.run-detail__meta-label');
    const v = row.querySelector('.run-detail__meta-value');
    if (k && v && k.textContent === label) return v;
  }
  return null;
}

// --- 打开：展开侧栏 / 激活 Run tab / loading 清旧 / 每次打开刷新 / 键盘焦点 ---
async function testRunFooterDetailOpenAndTabs() {
  let env = loadChat();
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  await _prepareSession(env, 's1', [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-1' }]);
  const shell = env.byIdMap['chat-shell'];
  ok(shell.classList.contains('chat-shell--side-collapsed'), 'sidebar starts collapsed');
  const runBtn = env.byIdMap['chat-tab-run-button'];
  const runPanel = env.byIdMap['chat-tab-run'];
  const toolBtn = env.byIdMap['chat-tab-tool-button'];
  const artBtn = env.byIdMap['chat-tab-artifact-button'];
  ok(runBtn.getAttribute('aria-selected') === 'false' && runPanel.hidden === true,
    'Run tab initially inactive');

  // 点击 footer 详情：同步展开 + 激活 Run tab + loading 清除旧内容
  _clickRunDetail(env, _runFooters(env)[0]);
  ok(!shell.classList.contains('chat-shell--side-collapsed'), 'clicking footer detail expands sidebar');
  ok(env.byIdMap['chat-side-toggle-btn'].getAttribute('aria-expanded') === 'true',
    'expand helper syncs aria-expanded to true');
  ok(runBtn.getAttribute('aria-selected') === 'true' && runBtn.getAttribute('tabindex') === '0',
    'Run tab activated on open');
  ok(runPanel.hidden === false && env.byIdMap['chat-tab-tool'].hidden === true
    && env.byIdMap['chat-tab-artifact'].hidden === true, 'Run panel visible, other panels hidden');
  ok(env.byIdMap['chat-run-detail'].textContent === '加载中...',
    'open clears previous content and shows loading (got ' + JSON.stringify(env.byIdMap['chat-run-detail'].textContent) + ')');
  await _settle(env);
  const detail = env.byIdMap['chat-run-detail'];
  ok(detail.textContent.indexOf('执行详情') !== -1, 'detail shows title Run 详情');
  const overview = detail.querySelector('.run-detail__overview');
  ok(!!overview && !!overview.querySelector('.run-detail__header')
    && !!overview.querySelector('.run-detail__meta'),
    'detail groups status header and metadata in overview card');
  ok(!!detail.querySelector('.run-detail__section--tools')
    && !!detail.querySelector('.run-detail__section--trace'),
    'detail renders distinct tool and Trace groups');
  ok(detail.textContent.indexOf('已完成') !== -1, 'detail shows status text');
  const metaLabels = Array.from(overview.querySelector('.run-detail__meta').querySelectorAll('.run-detail__meta-label'))
    .map((label) => label.textContent);
  ok(metaLabels.join('|') === 'Run ID|来源|状态|结束原因|开始时间|耗时',
    'detail metadata orders source, status, then end reason (got ' + JSON.stringify(metaLabels) + ')');
  ok(_detailMetaValue(detail, '状态') && _detailMetaValue(detail, '状态').textContent === '已完成',
    'detail shows status as a metadata field');
  ok(overview.querySelector('.run-detail__header').querySelector('.run-detail__status') === null,
    'detail header no longer contains the status field');
  ok(_detailMetaValue(detail, '耗时') && _detailMetaValue(detail, '耗时').textContent === '18秒',
    'detail reuses footer duration (18秒)');
  ok(_detailMetaValue(detail, '来源') && _detailMetaValue(detail, '来源').textContent === 'dashboard',
    'detail shows source');
  ok(_detailMetaValue(detail, '结束原因') && _detailMetaValue(detail, '结束原因').textContent === 'stop',
    'detail shows end_reason');
  ok(detail.textContent.indexOf('未调用工具') !== -1, 'empty tool calls shows 未调用工具');

  // 侧栏已展开时再次打开不收起；再次打开的 loading 清除已渲染的旧详情内容
  _clickRunDetail(env, _runFooters(env)[0]);
  ok(!shell.classList.contains('chat-shell--side-collapsed'), 'reopen keeps sidebar expanded');
  ok(env.byIdMap['chat-run-detail'].textContent === '加载中...',
    'reopen loading clears previously rendered detail content');
  await _settle(env);
  artBtn.click();
  ok(artBtn.getAttribute('aria-selected') === 'true' && runBtn.getAttribute('aria-selected') === 'false',
    'manual tab switch still works with third tab present');

  // 键盘：方向键循环覆盖第三 tab，焦点跟随；Home/End 直达首尾
  toolBtn.focus();
  toolBtn.dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault: () => {} });
  ok(artBtn.getAttribute('aria-selected') === 'true', 'ArrowRight tool -> artifact');
  artBtn.dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault: () => {} });
  ok(runBtn.getAttribute('aria-selected') === 'true' && env.document.activeElement === runBtn,
    'ArrowRight artifact -> run with focus moved');
  runBtn.dispatchEvent({ type: 'keydown', key: 'ArrowRight', preventDefault: () => {} });
  ok(toolBtn.getAttribute('aria-selected') === 'true', 'ArrowRight wraps run -> tool');
  runBtn.dispatchEvent({ type: 'keydown', key: 'ArrowLeft', preventDefault: () => {} });
  ok(artBtn.getAttribute('aria-selected') === 'true', 'ArrowLeft run -> artifact');
  toolBtn.dispatchEvent({ type: 'keydown', key: 'End', preventDefault: () => {} });
  ok(runBtn.getAttribute('aria-selected') === 'true' && env.document.activeElement === runBtn,
    'End on first tab reaches third tab');
  runBtn.dispatchEvent({ type: 'keydown', key: 'Home', preventDefault: () => {} });
  ok(toolBtn.getAttribute('aria-selected') === 'true' && env.document.activeElement === toolBtn,
    'Home on third tab returns to first tab');

  // 每次打开刷新：同批次内复用工具缓存；新批次后重新读取工具调用
  env = loadChat();
  let toolsPayload = [];
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  env.setGetSessionToolCallsHandler(() => Promise.resolve(toolsPayload));
  const msgs = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-1' }];
  await _prepareSession(env, 's1', msgs);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  const callsAfterFirstOpen = env.getGetSessionToolCallsCalls().filter((c) => c.id === 's1').length;
  ok(callsAfterFirstOpen >= 1, 'open fetches session tool calls');
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.getGetSessionToolCallsCalls().filter((c) => c.id === 's1').length === callsAfterFirstOpen,
    'same-batch reopen reuses batch tool cache');
  // 新批次：批内自动重发详情打开（计划修订），工具缓存失效并重读——无需点击即渲染新批次工具
  toolsPayload = [{ run_id: 'run-1', tool_name: 'tool_new', duration_ms: 7, arguments: { k: 'v' } }];
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: msgs, summary: null, task_state: null });
  await _settle(env);
  const detailBatch = env.byIdMap['chat-run-detail'];
  ok(detailBatch.textContent.indexOf('tool_new') !== -1 && detailBatch.textContent.indexOf('7ms') !== -1
    && detailBatch.textContent.indexOf('加载中') === -1,
    'new batch re-issues detail open and renders refreshed tool calls without click');
  const callsAfterBatch = env.getGetSessionToolCallsCalls().filter((c) => c.id === 's1').length;
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.getGetSessionToolCallsCalls().filter((c) => c.id === 's1').length === callsAfterBatch,
    'same-batch re-click reuses batch tool cache');
  const detail2 = env.byIdMap['chat-run-detail'];
  ok(detail2.textContent.indexOf('tool_new') !== -1 && detail2.textContent.indexOf('7ms') !== -1,
    'new-batch tool calls rendered after re-click');
  // 切换会话后占位恢复
  env = loadChat();
  env.setListSessionRunsHandler((id) => Promise.resolve([_runRow({ id: id === 'sB' ? 'run-b' : 'run-a' })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  sessionsList = [{ id: 'sA', title: 'sA' }, { id: 'sB', title: 'sB' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({
    session: { id },
    messages: [{ id: 'm-' + id, role: 'assistant', content: 'x', run_id: id === 'sA' ? 'run-a' : 'run-b' }],
    summary: null, task_state: null,
  });
  await env.chat.init();
  await _settle(env);
  env.fireClick(env.findSessionItem('sA'));
  await _settle(env, 4);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent.indexOf('执行详情') !== -1, 'detail open on sA');
  env.fireClick(env.findSessionItem('sB'));
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].querySelector('.run-list__link').dataset.runId === 'run-b',
    'session switch resets selection and shows the new session run list');

  // 用户折叠侧栏后（Run tab 仍激活），同会话新批次的后台重发不得弹回展开；
  // 面板内容仍在新批次 token 下刷新
  env = loadChat();
  let collapsedToolsPayload = [];
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  env.setGetSessionToolCallsHandler(() => Promise.resolve(collapsedToolsPayload));
  const collapsedMsgs = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-1' }];
  await _prepareSession(env, 's1', collapsedMsgs);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  const collapsedShell = env.byIdMap['chat-shell'];
  ok(!collapsedShell.classList.contains('chat-shell--side-collapsed'), 'sidebar expanded after open');
  collapsedShell.classList.add('chat-shell--side-collapsed');
  collapsedToolsPayload = [{ run_id: 'run-1', tool_name: 'tool_bg', duration_ms: 3, arguments: null }];
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: collapsedMsgs, summary: null, task_state: null });
  await _settle(env);
  ok(collapsedShell.classList.contains('chat-shell--side-collapsed'),
    'background reissue does not re-expand user-collapsed sidebar');
  const bgDetail = env.byIdMap['chat-run-detail'];
  ok(bgDetail.textContent.indexOf('tool_bg') !== -1 && bgDetail.textContent.indexOf('3ms') !== -1
    && bgDetail.textContent.indexOf('加载中') === -1,
    'background reissue still refreshes panel content under new token');
}

// --- 元信息与工具列表：普通状态文本 / Run ID 截断 / 缺失降级 / 安全参数摘要 ---
async function testRunFooterDetailMetaAndTools() {
  const statusCases = [
    ['completed', '已完成'],
    ['failed', '失败'],
    ['cancelled', '已取消'],
    ['interrupted', '已中断'],
    ['not_started', '未开始'],
    ['running', '运行中'],
    ['pending', '运行中'],
    ['mystery', '未知'],
  ];
  for (const [status, label] of statusCases) {
    const env = loadChat();
    const rid = 'run-' + status;
    env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: rid, status })]));
    env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, status, trace_links: [] })));
    await _prepareSession(env, 's1', [{ id: 'm', role: 'assistant', content: 'x', run_id: rid }]);
    await _settle(env);
    _clickRunDetail(env, _runFooters(env)[0]);
    await _settle(env);
    const detail = env.byIdMap['chat-run-detail'];
    const statusEl = _detailMetaValue(detail, '状态');
    ok(!!statusEl && statusEl.textContent === label,
      'status ' + status + ' -> text ' + label + ' (got ' + JSON.stringify(statusEl && statusEl.textContent) + ')');
    ok(!!statusEl && statusEl.className === 'run-detail__meta-value'
      && detail.querySelector('.run-detail__status') === null,
      'status ' + status + ' uses plain metadata styling (got ' + JSON.stringify(statusEl && statusEl.className) + ')');
  }

  // Run ID 中段截断（title 全量）、source/end_reason 缺失 —、开始时间与 error 块
  let env = loadChat();
  const longId = 'run-' + 'abcdef1234567890' + 'WXYZ9876543210';
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: longId })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({
    id: runId, trace_links: [], source: null, end_reason: null, error: 'boom failed',
  })));
  await _prepareSession(env, 's1', [{ id: 'm', role: 'assistant', content: 'x', run_id: longId }]);
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  let detail = env.byIdMap['chat-run-detail'];
  const idEl = _detailMetaValue(detail, 'Run ID');
  ok(!!idEl && idEl.textContent.indexOf('…') !== -1 && idEl.textContent.length < longId.length
    && idEl.textContent.indexOf(longId.slice(0, 8)) === 0,
    'long run id middle-truncated (got ' + JSON.stringify(idEl && idEl.textContent) + ')');
  ok(!!idEl && idEl.getAttribute('title') === longId, 'run id title keeps full id');
  ok(_detailMetaValue(detail, '来源') && _detailMetaValue(detail, '来源').textContent === '—',
    'missing source shows —');
  ok(_detailMetaValue(detail, '结束原因') && _detailMetaValue(detail, '结束原因').textContent === '—',
    'missing end_reason shows —');
  const startedEl = _detailMetaValue(detail, '开始时间');
  // 期望值按 UTC+8 偏移 + getUTC* 分量手工拼出（与知识库约定一致），不依赖运行环境时区
  const startedTz = new Date(1000 * 1000 + 8 * 3600 * 1000);
  const padTz = (n) => (n < 10 ? '0' + n : '' + n);
  const expectedStarted = startedTz.getUTCFullYear() + '-' + padTz(startedTz.getUTCMonth() + 1) + '-'
    + padTz(startedTz.getUTCDate()) + ' ' + padTz(startedTz.getUTCHours()) + ':'
    + padTz(startedTz.getUTCMinutes()) + ':' + padTz(startedTz.getUTCSeconds());
  ok(!!startedEl && startedEl.textContent === expectedStarted,
    'started_at unix seconds renders full UTC+8 datetime (got ' + JSON.stringify(startedEl && startedEl.textContent)
    + ', expected ' + JSON.stringify(expectedStarted) + ')');
  const errorEl = detail.querySelector('.run-detail__error');
  ok(!!errorEl && errorEl.textContent === 'boom failed' && errorEl.className.indexOf('run-status--bad') !== -1,
    'non-empty string error renders red error block');
  ok(detail.textContent.indexOf('undefined') === -1, 'no undefined leaked into detail');

  // 开始时间非法/非有限/无效日期 -> —（每个变体独立 Run，终态缓存不串行）；error 非空字符串才显示错误块
  env = loadChat();
  const metaVariants = [
    [_runRow({ started_at: '1000' }), 'started_at-string'],
    [_runRow({ started_at: NaN }), 'started_at-NaN'],
    [_runRow({ started_at: Infinity }), 'started_at-Infinity'],
    [_runRow({ started_at: 1e21 }), 'started_at-invalid-date'],
    [_runRow({ started_at: null }), 'started_at-null'],
    [_runRow({ error: null }), 'error-null'],
    [_runRow({ error: '' }), 'error-empty'],
    [_runRow({ error: 123 }), 'error-number'],
    [_runRow({ error: undefined }), 'error-undefined'],
  ];
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-base' })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  await _prepareSession(env, 's1', [{ id: 'm0', role: 'assistant', content: 'x', run_id: 'run-base' }]);
  for (let vi = 0; vi < metaVariants.length; vi++) {
    const overrides = metaVariants[vi][0];
    const tag = metaVariants[vi][1];
    const rid = 'run-variant-' + vi;
    env.setListSessionRunsHandler(() => Promise.resolve([_runRow(Object.assign({}, overrides, { id: rid }))]));
    env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow(Object.assign({}, overrides, { id: rid, trace_links: [] }))));
    await env.chat.applySessionDetail({ session: { id: 's1' }, messages: [{ id: 'm', role: 'assistant', content: 'x', run_id: rid }], summary: null, task_state: null });
    await _settle(env);
    _clickRunDetail(env, _runFooters(env)[0]);
    await _settle(env);
    const d = env.byIdMap['chat-run-detail'];
    if (tag.indexOf('error-') === 0) {
      ok(!d.querySelector('.run-detail__error'), tag + ' renders no error block');
    } else {
      const startedEl2 = _detailMetaValue(d, '开始时间');
      ok(!!startedEl2 && startedEl2.textContent === '—',
        tag + ' -> — (got ' + JSON.stringify(startedEl2 && startedEl2.textContent) + ')');
    }
  }

  // 工具列表：run_id 精确过滤、duration 变体、参数摘要（对象/空值/截断/不可序列化）
  env = loadChat();
  let toolsPayload = [
    { run_id: 'run-1', tool_name: 'tool_a', duration_ms: 12, arguments: { x: 1 } },
    { run_id: 'run-other', tool_name: 'tool_noise', duration_ms: 5, arguments: {} },
  ];
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  env.setGetSessionToolCallsHandler(() => Promise.resolve(toolsPayload));
  const toolMsgs = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-1' }];
  await _prepareSession(env, 's1', toolMsgs);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  detail = env.byIdMap['chat-run-detail'];
  let nameEls = detail.querySelectorAll('.run-detail__tool-name');
  ok(nameEls.length === 1 && nameEls[0].textContent === 'tool_a',
    'tool calls filtered by run_id (got ' + JSON.stringify(nameEls.map((n) => n.textContent)) + ')');
  ok(detail.textContent.indexOf('tool_noise') === -1, 'other run tool calls not shown');
  let durEl = detail.querySelector('.run-detail__tool-duration');
  ok(!!durEl && durEl.textContent === '12ms', 'duration_ms milliseconds shown (got ' + JSON.stringify(durEl && durEl.textContent) + ')');
  let argsEl = detail.querySelector('.run-detail__tool-args');
  ok(!!argsEl && argsEl.textContent === '{"x":1}', 'object arguments serialized to single-line JSON');
  ok(!!argsEl && argsEl.getAttribute('title') === '{"x":1}', 'arguments title keeps full safe text');

  // duration 变体：0 有效，缺失/负数/字符串/NaN/Infinity —，1.5 有限非负有效
  const durCases = [
    [0, '0ms'], [undefined, '—'], [-5, '—'], ['12', '—'], [NaN, '—'], [Infinity, '—'], [1.5, '1.5ms'],
  ];
  for (const [ms, label] of durCases) {
    toolsPayload = [{ run_id: 'run-1', tool_name: 'tool_z', duration_ms: ms, arguments: null }];
    await env.chat.applySessionDetail({ session: { id: 's1' }, messages: toolMsgs, summary: null, task_state: null });
    await _settle(env);
    _clickRunDetail(env, _runFooters(env)[0]);
    await _settle(env);
    const dEl = env.byIdMap['chat-run-detail'].querySelector('.run-detail__tool-duration');
    ok(!!dEl && dEl.textContent === label,
      'duration_ms ' + JSON.stringify(ms) + ' -> ' + label + ' (got ' + JSON.stringify(dEl && dEl.textContent) + ')');
  }

  // 参数变体：空值 —、长文本截断 80 字符（title 全量）、不可序列化 —
  const circular = {};
  circular.self = circular;
  const longJson = JSON.stringify({ payload: 'y'.repeat(200) });
  const argCases = [
    [null, '—', null],
    [undefined, '—', null],
    [{ payload: 'y'.repeat(200) }, longJson.slice(0, 80) + '…', longJson],
    [circular, '—', null],
  ];
  for (const [args, expectedText, expectedTitle] of argCases) {
    toolsPayload = [{ run_id: 'run-1', tool_name: 'tool_arg', duration_ms: 1, arguments: args }];
    await env.chat.applySessionDetail({ session: { id: 's1' }, messages: toolMsgs, summary: null, task_state: null });
    await _settle(env);
    _clickRunDetail(env, _runFooters(env)[0]);
    await _settle(env);
    const aEl = env.byIdMap['chat-run-detail'].querySelector('.run-detail__tool-args');
    const argLabel = args === circular ? 'circular' : (args === null || args === undefined ? String(args) : JSON.stringify(args).slice(0, 20));
    ok(!!aEl && aEl.textContent === expectedText,
      'arguments ' + argLabel + ' -> summary (got '
        + JSON.stringify(aEl && aEl.textContent.slice(0, 30)) + ')');
    if (expectedTitle !== null) {
      ok(!!aEl && aEl.getAttribute('title') === expectedTitle, 'long arguments title keeps full text');
    }
  }

  // 工具读取失败 / null 响应：元信息保留，工具区无法加载；空数组与过滤后为空均未调用工具
  env.setGetSessionToolCallsHandler(() => Promise.reject(new Error('boom')));
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: toolMsgs, summary: null, task_state: null });
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  detail = env.byIdMap['chat-run-detail'];
  ok(detail.textContent.indexOf('无法加载工具调用') !== -1,
    'tool fetch failure -> 无法加载工具调用 (got ' + JSON.stringify(detail.textContent.slice(0, 80)) + ')');
  ok(detail.textContent.indexOf('已完成') !== -1 && detail.textContent.indexOf('执行详情') !== -1,
    'run meta kept on tool failure');
  ok(detail.querySelectorAll('.run-detail__trace-link').length === 0 && detail.textContent.indexOf('无 Trace 记录') !== -1,
    'trace section still rendered on tool failure');
  env.setGetSessionToolCallsHandler(() => Promise.resolve(null));
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: toolMsgs, summary: null, task_state: null });
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent.indexOf('无法加载工具调用') !== -1,
    'null tool response -> 无法加载工具调用');
  env.setGetSessionToolCallsHandler(() => Promise.resolve([]));
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: toolMsgs, summary: null, task_state: null });
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent.indexOf('未调用工具') !== -1,
    'empty tool array -> 未调用工具');
  env.setGetSessionToolCallsHandler(() => Promise.resolve([{ run_id: 'other-run', tool_name: 'n', duration_ms: 1 }]));
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: toolMsgs, summary: null, task_state: null });
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent.indexOf('未调用工具') !== -1,
    'calls only for other runs -> 未调用工具');

  // Run 404 / 详情读取失败：只显示无法加载执行详情，不留上一 Run 内容
  env = loadChat();
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
  env.setGetSessionRunHandler(() => Promise.resolve(null)); // 404 -> null
  env.setGetSessionToolCallsHandler(() => Promise.resolve([{ run_id: 'run-1', tool_name: 't', duration_ms: 1 }]));
  await _prepareSession(env, 's1', [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-1' }]);
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent === '无法加载执行详情',
    'run 404 -> 无法加载执行详情 (got ' + JSON.stringify(env.byIdMap['chat-run-detail'].textContent) + ')');
  ok(env.byIdMap['chat-run-detail'].querySelectorAll('.run-detail__meta-row').length === 0,
    '404 leaves no stale run content');
  // 详情请求异常同样降级
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-err' })]));
  env.setGetSessionRunHandler(() => Promise.reject(new Error('boom')));
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-err' }], summary: null, task_state: null });
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent === '无法加载执行详情',
    'run detail fetch error -> 无法加载执行详情');
}

// --- Run 详情 Trace 区：逐条链接（前 8 位 ID）/ 无记录 / 未配置 / 加载失败 ---
async function testRunFooterDetailTrace() {
  const ID_A = 'a1'.repeat(16);
  const ID_B = 'b2'.repeat(16);
  const ID_C = 'c3'.repeat(16);
  const baseUrl = 'https://grafana.example/grafana/explore?orgId=1';
  let env = loadChat();
  const traceLinks = [
    { trace_id: ID_B, created_at: '2026-01-02T00:00:00Z', id: 'l2' },
    { trace_id: 'ZZZ-not-hex', created_at: '2026-01-01T00:00:00Z', id: 'l0' },
    { trace_id: ID_A.toUpperCase(), created_at: '2026-01-01T00:00:00Z', id: 'l1' },
    { trace_id: ID_C, created_at: '2026-01-03T00:00:00Z', id: 'l3' },
  ];
  env.setGetOtelLinksHandler(() => Promise.resolve({ config: { trace_base_url: baseUrl } }));
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: traceLinks })));
  await _prepareSession(env, 's1', [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-1' }]);
  await _settle(env);
  _clickRunDetail(env, _runFooters(env)[0]);
  await _settle(env);
  const detail = env.byIdMap['chat-run-detail'];
  const links = detail.querySelectorAll('.run-detail__trace-link');
  ok(links.length === 3, 'all valid trace links listed, invalid filtered (got ' + links.length + ')');
  const expectedOrder = [ID_A, ID_B, ID_C];
  for (let i = 0; i < expectedOrder.length; i++) {
    const a = links[i];
    ok(!!a && a.textContent === expectedOrder[i].slice(0, 8),
      'trace link ' + i + ' shows first 8 chars of id (got ' + JSON.stringify(a && a.textContent) + ')');
    ok(!!a && a.getAttribute('title') === expectedOrder[i], 'trace link title keeps full id');
    ok(!!a && a.href === env.chat.buildTraceUrl(baseUrl, expectedOrder[i]),
      'trace link href matches buildTraceUrl contract');
    ok(!!a && a.target === '_blank' && a.rel === 'noopener',
      'trace link opens new window with noopener');
  }
  ok(detail.textContent.indexOf('00000000') === -1, 'no all-zero/invalid ids rendered');

  // 无记录 / 未配置 / 加载失败 三态区分
  const traceEnv = async (otelHandler, traceLinksValue) => {
    const e = loadChat();
    e.setGetOtelLinksHandler(otelHandler);
    e.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-1' })]));
    e.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: traceLinksValue })));
    await _prepareSession(e, 's1', [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-1' }]);
    await _settle(e);
    _clickRunDetail(e, _runFooters(e)[0]);
    await _settle(e);
    return e;
  };
  let e2 = await traceEnv(() => Promise.resolve({ config: { trace_base_url: baseUrl } }), []);
  ok(e2.byIdMap['chat-run-detail'].textContent.indexOf('无 Trace 记录') !== -1
    && e2.byIdMap['chat-run-detail'].querySelectorAll('.run-detail__trace-link').length === 0,
    'empty trace links -> 无 Trace 记录 without links');
  e2 = await traceEnv(() => Promise.resolve({ config: {} }), traceLinks);
  ok(e2.byIdMap['chat-run-detail'].textContent.indexOf('Trace 地址未配置') !== -1
    && e2.byIdMap['chat-run-detail'].querySelectorAll('.run-detail__trace-link').length === 0,
    'valid links without base url -> Trace 地址未配置, no empty link');
  e2 = await traceEnv(() => Promise.reject(new Error('cfg')), traceLinks);
  ok(e2.byIdMap['chat-run-detail'].textContent.indexOf('无法加载 Trace 入口') !== -1
    && e2.byIdMap['chat-run-detail'].querySelectorAll('.run-detail__trace-link').length === 0,
    'otel config failure -> 无法加载 Trace 入口, not disguised as 无记录');
  // 降级优先级与 footer 对齐：otel 失败优先于空链接（配置失败无法确认“确实无记录”）
  e2 = await traceEnv(() => Promise.reject(new Error('cfg')), []);
  ok(e2.byIdMap['chat-run-detail'].textContent.indexOf('无法加载 Trace 入口') !== -1
    && e2.byIdMap['chat-run-detail'].textContent.indexOf('无 Trace 记录') === -1,
    'otel failure + empty links -> 无法加载 Trace 入口 wins over 无 Trace 记录');
  // otel loading 不与“未配置”混淆：侧栏与 footer 一致不表态（不出链接、不出降级文案）
  e2 = await traceEnv(() => new Promise(() => {}), traceLinks);
  ok(e2.byIdMap['chat-run-detail'].querySelectorAll('.run-detail__trace-link').length === 0
    && e2.byIdMap['chat-run-detail'].textContent.indexOf('Trace 地址未配置') === -1
    && e2.byIdMap['chat-run-detail'].textContent.indexOf('无法加载 Trace 入口') === -1,
    'otel loading -> no links and no unset/failed note, consistent with footer');
}

// --- 选中请求归属：快速 A→B / 同会话新批次 / 切换与删除会话后旧请求不提交 ---
async function testRunFooterDetailStaleRequests() {
  // (a) 快速选 Run A→B：A 迟到不提交
  let env = loadChat();
  let releaseA = null;
  env.setListSessionRunsHandler(() => Promise.resolve([
    _runRow({ id: 'run-a', source: 'dashboard' }),
    _runRow({ id: 'run-b', source: 'cli' }),
  ]));
  env.setGetSessionRunHandler((id, runId) => {
    if (runId === 'run-a') {
      return new Promise((res) => { releaseA = (row) => res(row); });
    }
    return Promise.resolve(_runRow({ id: runId, source: 'cli', end_reason: 'done-b', trace_links: [] }));
  });
  const abMsgs = [
    { id: 'ma', role: 'assistant', content: 'a', run_id: 'run-a' },
    { id: 'mb', role: 'assistant', content: 'b', run_id: 'run-b' },
  ];
  await _prepareSession(env, 's1', abMsgs);
  await _settle(env);
  let linkA = null;
  let linkB = null;
  for (const f of _runFooters(env)) {
    const link = _runFooterLinks(f).find((x) => x.getAttribute('aria-label') === '查看执行详情');
    if (f.dataset.runId === 'run-a') linkA = link; else linkB = link;
  }
  ok(!!linkA && !!linkB, 'both footers expose detail links');
  env.fireClick(linkA);
  ok(env.byIdMap['chat-run-detail'].textContent === '加载中...', 'A open shows loading');
  env.fireClick(linkB);
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent.indexOf('done-b') !== -1,
    'B renders immediately (got ' + JSON.stringify(env.byIdMap['chat-run-detail'].textContent.slice(0, 60)) + ')');
  releaseA(_runRow({ id: 'run-a', source: 'dashboard', end_reason: 'late-a', trace_links: [] }));
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent.indexOf('done-b') !== -1
    && env.byIdMap['chat-run-detail'].textContent.indexOf('late-a') === -1,
    'late A response does not overwrite B detail');

  // (b) 同会话新批次：旧打开请求不提交；新批次自动重发打开（计划修订），
  // 最终面板在新 token 下重渲染成功，而非停留在“加载中...”
  env = loadChat();
  let releaseA2 = null;
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-a' })]));
  env.setGetSessionRunHandler((id, runId) => new Promise((res) => {
    releaseA2 = () => res(_runRow({ id: runId, end_reason: 'late-a', trace_links: [] }));
  }));
  const aMsgs = [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-a' }];
  await _prepareSession(env, 's1', aMsgs);
  _clickRunDetail(env, _runFooters(env)[0]);
  ok(env.byIdMap['chat-run-detail'].textContent === '加载中...', 'open shows loading');
  await env.chat.applySessionDetail({ session: { id: 's1' }, messages: aMsgs, summary: null, task_state: null });
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent === '加载中...',
    'new batch re-issues open and keeps loading until shared request settles');
  releaseA2();
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].textContent.indexOf('late-a') !== -1
    && env.byIdMap['chat-run-detail'].textContent.indexOf('加载中') === -1,
    'panel re-renders under new batch token, stale open did not strand loading (got '
      + JSON.stringify(env.byIdMap['chat-run-detail'].textContent.slice(0, 80)) + ')');

  // (c) 切换会话：清选中与占位，旧请求不提交
  env = loadChat();
  let releaseA3 = null;
  env.setListSessionRunsHandler((id) => Promise.resolve([_runRow({ id: id === 'sB' ? 'run-b' : 'run-a' })]));
  env.setGetSessionRunHandler((id, runId) => {
    if (runId === 'run-a') {
      return new Promise((res) => { releaseA3 = () => res(_runRow({ id: runId, end_reason: 'late-a', trace_links: [] })); });
    }
    return Promise.resolve(_runRow({ id: runId, trace_links: [] }));
  });
  sessionsList = [{ id: 'sA', title: 'sA' }, { id: 'sB', title: 'sB' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({
    session: { id },
    messages: [{ id: 'm-' + id, role: 'assistant', content: 'x', run_id: id === 'sA' ? 'run-a' : 'run-b' }],
    summary: null, task_state: null,
  });
  await env.chat.init();
  await _settle(env);
  env.fireClick(env.findSessionItem('sA'));
  await _settle(env, 4);
  _clickRunDetail(env, _runFooters(env)[0]);
  ok(env.byIdMap['chat-run-detail'].textContent === '加载中...', 'switch scenario: open shows loading');
  env.fireClick(env.findSessionItem('sB'));
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].querySelector('.run-list__link').dataset.runId === 'run-b',
    'session switch clears selection and shows run-b');
  releaseA3();
  await _settle(env);
  ok(env.byIdMap['chat-run-detail'].querySelector('.run-list__link').dataset.runId === 'run-b',
    'late A after session switch does not overwrite the new session list');

  // (d) 删除当前会话：清选中与占位，旧请求不提交
  env = loadChat();
  let releaseA4 = null;
  env.setListSessionRunsHandler(() => Promise.resolve([_runRow({ id: 'run-a' })]));
  env.setGetSessionRunHandler((id, runId) => new Promise((res) => {
    releaseA4 = () => res(_runRow({ id: runId, end_reason: 'late-a', trace_links: [] }));
  }));
  sessionsList = [{ id: 'sA', title: 'sA' }];
  env.win.NAGENT.api.getSessionDetail = (id) => Promise.resolve({
    session: { id },
    messages: [{ id: 'm', role: 'assistant', content: 'x', run_id: 'run-a' }],
    summary: null, task_state: null,
  });
  await env.chat.init();
  await _settle(env);
  env.fireClick(env.findSessionItem('sA'));
  await _settle(env, 4);
  _clickRunDetail(env, _runFooters(env)[0]);
  ok(env.byIdMap['chat-run-detail'].textContent === '加载中...', 'delete scenario: open shows loading');
  env.win.NAGENT.modal.confirm = () => Promise.resolve(true);
  env.setDeleteSessionHandler(() => Promise.resolve({}));
  let delBtn = null;
  for (const it of env.document.querySelectorAll('.session-item')) {
    if (!(it.dataset && it.dataset.sessionId === 'sA')) continue;
    delBtn = (function visit(n) {
      for (const c of (n._kids || [])) {
        if (c.tagName === 'BUTTON' && c.getAttribute('aria-label') === '删除会话') return c;
        const r = visit(c);
        if (r) return r;
      }
      return null;
    })(it);
    break;
  }
  ok(!!delBtn, 'delete button found for current session');
  if (delBtn) {
    delBtn.click();
    await _settle(env);
    ok(env.byIdMap['chat-run-detail'].textContent === '暂未选择 Run',
      'session delete clears selection to placeholder (got ' + JSON.stringify(env.byIdMap['chat-run-detail'].textContent) + ')');
    releaseA4();
    await _settle(env);
    ok(env.byIdMap['chat-run-detail'].textContent === '暂未选择 Run',
      'late A after session delete does not commit');
    ok(_runFooters(env).length === 0, 'delete clears footers as before');
  }
}


async function testUnselectedSessionRunList() {
  let env = loadChat();
  env.setListSessionRunsHandler(() => Promise.resolve([
    _runRow({ id: 'run-late', created_at: 30, started_at: 30 }),
    _runRow({ id: 'run-early', created_at: 10, started_at: 10 }),
    _runRow({ id: 'run-middle', created_at: 20, started_at: 20 }),
  ]));
  env.setGetSessionRunHandler((id, runId) => Promise.resolve(_runRow({ id: runId, trace_links: [] })));
  // 没有关联消息的执行也必须显示；列表不触发逐执行详情读取。
  await _prepareSession(env, 'list-session', []);
  const target = env.byIdMap['chat-run-detail'];
  const list = target.querySelector('.run-list');
  const rows = target.querySelectorAll('.run-list__row');
  const links = target.querySelectorAll('.run-list__link');
  ok(links.length === 3 && links[0].dataset.runId === 'run-early'
    && links[1].dataset.runId === 'run-middle' && links[2].dataset.runId === 'run-late',
    'unselected list includes every ledger run in chronological order without messages');
  ok(!!list && list.tagName === 'DIV' && list.getAttribute('role') === 'list'
    && list.querySelectorAll('ol').length === 0 && list.querySelectorAll('li').length === 0,
    'run list uses an unnumbered list structure');
  ok(rows.length === 3 && rows.every((row, index) => row._kids.length === 2
    && row._kids[0].classList.contains('run-list__time')
    && row._kids[0].textContent === '1970-01-01 08:00:' + (index === 0 ? '10' : index === 1 ? '20' : '30')
    && row._kids[1] === links[index]
    && links[index].textContent === '详情'
    && row._kids[0].querySelectorAll('a').length === 0
    && row.querySelectorAll('.run-list__status').length === 0),
    'each run row has a complete plain-text time and a final detail link without status or Run ID');
  env.fireClick(links[1]);
  await _settle(env);
  ok(_detailMetaValue(target, 'Run ID').textContent === 'run-middle',
    'run list link opens the corresponding existing detail view');
  const detail = target.querySelector('.run-detail');
  const traceSection = target.querySelector('.run-detail__section--trace');
  const header = detail.querySelector('.run-detail__header');
  const back = target.querySelector('.run-detail__back');
  ok(!!back && back.tagName === 'A' && back.textContent === '返回列表'
    && !back.classList.contains('btn') && back.parentNode === header
    && header._kids[header._kids.length - 1] === back
    && header.querySelector('.run-detail__title').textContent === '执行详情'
    && traceSection.parentNode === detail && traceSection.querySelector('.run-detail__back') === null
    && detail.querySelectorAll('.run-detail__back').length === 1
    && target.textContent.indexOf('返回执行列表') === -1,
    'text-only back link is the final header child, to the right of the detail title');
  env.fireClick(back);
  ok(target.querySelectorAll('.run-list__link').length === 3, 'back restores the session run list');

  env = loadChat();
  env.setListSessionRunsHandler(() => Promise.resolve([]));
  await _prepareSession(env, 'empty-session', []);
  ok(env.byIdMap['chat-run-detail'].textContent === '暂无执行记录', 'empty ledger has explicit empty state');

  env = loadChat();
  env.setListSessionRunsHandler(() => Promise.reject(new Error('offline')));
  await _prepareSession(env, 'failed-session', []);
  ok(env.byIdMap['chat-run-detail'].textContent === '无法加载执行列表', 'list failure differs from empty ledger');
}

runIntegration().then(async () => {
  await testTaskCardInteraction();
  await testPartialMessageRefresh();
  await testDebugSettingsPerSession();
  await testToolApprovalCard();
  await testSessionViewLinks();
  await testSidePanelAndArtifacts();
  await testSessionsPanelCollapse();
  await testAutoRefreshPanels();
  await testSessionSourceFilter();
  await testSessionSearch();
  await testSkillPicker();
  await testRunFooterRenderAndFormat();
  await testRunFooterLiveCapture();
  await testRunFooterSessionIsolation();
  await testRunFooterTraceAndQueue();
  await testRunFooterBatchParallelPrefetch();
  await testRunFooterLedgerAbsent();
  await testRunFooterDetailQueueResilience();
  await testRunFooterDetailOpenAndTabs();
  await testRunFooterDetailMetaAndTools();
  await testRunFooterDetailTrace();
  await testRunFooterDetailStaleRequests();
  await testUnselectedSessionRunList();
  if (failures) { console.error('\n' + failures + ' test(s) failed'); process.exit(1); }
  console.log('chat_frontend_harness: all tests passed');
  process.exit(0);
}).catch((e) => {
  console.error('HARNESS ERROR: ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
