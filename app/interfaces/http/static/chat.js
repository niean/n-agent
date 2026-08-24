(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;
  const modal = namespace.modal;

  let currentSessionId = null;
  const SESSION_SOURCE_FILTER_STORAGE_KEY = 'nagent.chat.session-source-filter.v1';
  const SESSION_SOURCE_OPTIONS = [
    ['dashboard', 'Dashboard'],
    ['api', 'API'],
    ['cli', 'CLI'],
    ['feishu', '飞书'],
    ['dingtalk', '钉钉'],
    ['wecom', '企微'],
    ['acp', 'ACP'],
    ['schedule', '定时任务'],
    ['task', '任务'],
    ['curator', 'Curator'],
    ['delegation', '委派'],
  ];
  const SESSION_SOURCE_VALUES = new Set(SESSION_SOURCE_OPTIONS.map(([value]) => value));
  let selectedSessionSources = loadSessionSourceFilter();
  let currentSessionSearch = null;
  let activeSideTab = 'tool';
  let artifactPanelRequestSeq = 0;
  let isSending = false;
  let initialized = false;
  let externalMemoryProviders = [];
  let memoryPopoverOpen = false;
  // 调试设置弹框开关；工具调试/任务状态/对话压缩显隐按会话独立生效（每会话一份，互不影响）。
  // 持久化到 localStorage（按 sessionId 分桶）；新建会话默认 任务状态选中/对话压缩选中/工具调试未选中。
  const DEBUG_SETTINGS_KEY = 'nagent.chat.debug';
  const DEBUG_DEFAULTS = { task: true, compression: true, tool: false };
  // sessionId -> {task, compression, tool}；尚未创建会话时用 draft 兜底（首轮发送后转入对应会话）
  let sessionDebugSettings = {};
  let draftDebugSettings = null;
  let settingsPopoverOpen = false;
  // 存储每个会话的外部记忆配置
  let sessionExternalMemoryConfig = {};
  // 尚未创建会话时，允许用户先勾选首轮要使用的外部记忆。
  let draftExternalMemoryConfig = null;
  // 用户是否在本会话中操作过外部记忆勾选；
  // 仅当为 true 时才向 /chat/completions 携带 options.external_memory_enabled，
  // 未操作时不发送该字段，由后端按会话默认 profile 派生。
  let externalMemoryTouched = false;
  // 后端真实默认模型 id，启动时从 /chat/models 拉取；硬编码占位符会导致
  // provider 拒绝 tool 调用（如 Ark "does not support agent plan feature"）。
  let defaultModel = '';
  // 待发送的图片列表，每项 {dataUrl, name}；发送后清空。
  let pendingImages = [];

  // === 激活态消息自动刷新控制器 ===
  // 唯一定时器 + 世代 + 请求序号 + 单飞 + 复合版本(count+lastId)。
  // 仅当会话激活、页面可见、非发送中时轮询 GET /chat/sessions/{id}；
  // 版本相同不渲染（防闪烁），不同则按滚动规则刷新。system 历史消息已被
  // 后端 build_context_state 过滤，不进 LLM 上下文；本控制器只管展示刷新。
  const AUTO_REFRESH_INTERVAL_MS = 4000;
  const SCROLL_BOTTOM_THRESHOLD_PX = 48;
  let autoRefreshTimer = null;
  let autoRefreshGeneration = 0;
  let autoRefreshInFlight = null; // {generation, seq}
  let autoRefreshSeq = 0;
  let renderedMessageVersion = null; // {count, lastId} | null；空会话用 {count:0, lastId:null}
  // 已渲染消息节点按稳定 key 缓存。轮询只协调变化的节点，避免重建旧 <details>
  // 而丢失用户手动展开的工具调用、任务状态等界面状态。
  let renderedMessageNodes = new Map();
  let renderedMessageFingerprints = new Map();
  // Render token: monotonic counter to prevent stale async renders from overwriting newer content.
  // Incremented at the start of each renderSessionMessages; checked after await to abort stale renders.
  let renderToken = 0;
  // Active stream's assistant element — tracks the streaming bubble so that
  // session switches can disable its outstanding approval cards (no late POST).
  let activeStreamEl = null;

  function messageVersionOf(detail) {
    const msgs = Array.isArray(detail && detail.messages) ? detail.messages : null;
    if (!msgs) return null;
    if (msgs.length === 0) return { count: 0, lastId: null };
    const last = msgs[msgs.length - 1];
    if (!last || !last.id) return null; // 非空但末条无 id -> 无效快照
    return { count: msgs.length, lastId: last.id };
  }

  function versionsEqual(a, b) {
    return !!a && !!b && a.count === b.count && a.lastId === b.lastId;
  }

  function startAutoRefresh(sessionId, options) {
    options = options || {};
    stopAutoRefresh();
    if (!sessionId || sessionId !== currentSessionId) return;
    if (document.hidden) return;
    autoRefreshGeneration++;
    autoRefreshTimer = setInterval(autoRefreshTick, AUTO_REFRESH_INTERVAL_MS);
    if (options.immediate) autoRefreshTick();
  }

  function stopAutoRefresh() {
    if (autoRefreshTimer) { clearInterval(autoRefreshTimer); autoRefreshTimer = null; }
    autoRefreshGeneration++;
    autoRefreshInFlight = null;
  }

  // 仅当本地追加伴随成功持久化、返回真实服务端 id 时调用。
  // preVersion 为调用前捕获的 renderedMessageVersion；若期间版本已被权威详情改变，跳过。
  function advanceVersionAfterPersistedAppend(realId, preVersion) {
    if (!realId || preVersion !== renderedMessageVersion) return;
    if (!preVersion) { renderedMessageVersion = { count: 1, lastId: realId }; return; }
    renderedMessageVersion = { count: preVersion.count + 1, lastId: realId };
  }

  async function autoRefreshTick() {
    const sessionId = currentSessionId;
    const gen = autoRefreshGeneration;
    if (!sessionId || isSending || document.hidden) return;
    maybeRefreshSessionListTitles();  // 标题自动刷新（独立于消息版本，rate-limited）
    if (autoRefreshInFlight && autoRefreshInFlight.generation === gen) return; // 单飞
    const seq = ++autoRefreshSeq;
    autoRefreshInFlight = { generation: gen, seq };
    try {
      const detail = await api.getSessionDetail(sessionId);
      // 归属校验：会话、世代、请求序号三重，防切换/乱序串台
      if (sessionId !== currentSessionId || gen !== autoRefreshGeneration || seq !== autoRefreshSeq) return;
      if (isSending) return; // 进入发送态后丢弃，不覆盖 SSE DOM
      const version = messageVersionOf(detail);
      if (!version) { console.warn('auto-refresh: invalid snapshot'); return; }
      if (versionsEqual(version, renderedMessageVersion)) return; // 无变化跳过
      await applySessionDetail(detail, {
        preserveScroll: true, skipToolCalls: true, skipSessionList: true, applyExternalMemoryState: false,
        partialMessages: true,
      });
      if (sessionId === currentSessionId && gen === autoRefreshGeneration && seq === autoRefreshSeq) {
        renderedMessageVersion = version;
        // 消息版本变化（任务异步产出新消息/工具调用/制品）时，同步刷新工具调用与制品面板，
        // 避免需手动刷新浏览器才能看到最新制品信息与工具调用记录
        loadToolCalls();
        renderArtifactPanel({ silent: true });
      }
    } catch (e) {
      handleAutoRefreshError(e, sessionId, gen, seq);
    } finally {
      // 仅清除自身 token，旧世代请求不得释放新世代单飞锁
      if (autoRefreshInFlight && autoRefreshInFlight.generation === gen && autoRefreshInFlight.seq === seq) {
        autoRefreshInFlight = null;
      }
    }
  }

  function handleAutoRefreshError(e, sessionId, gen, seq) {
    if (sessionId !== currentSessionId || gen !== autoRefreshGeneration || seq !== autoRefreshSeq) return;
    // fetchJson 失败抛 new Error(code)，无 HTTP status；按 error.message 识别
    if (e && e.message === 'session_not_found') {
      stopAutoRefresh();
      currentSessionId = null;
      renderArtifactPanel();
      renderedMessageVersion = null;
      setHeader(null);
      setStatusMessage('会话不存在或已删除', 'error');
      loadSessions();
      return;
    }
    console.warn('auto-refresh failed:', e);
  }

  function appendText(parent, value) {
    parent.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  }

  function safeUrl(value) {
    if (typeof value !== 'string') return null;
    const trimmed = value.trim();
    return /^https?:\/\//i.test(trimmed) ? trimmed : null;
  }

  // 不可信的对话文本按 markdown 子集（图片、链接）渲染。全部用 DOM API 构建：
  // 文本与链接标签走 textContent（自动转义），img/a 用 createElement 后属性赋值，
  // URL 限定 http(s)；不使用 innerHTML，避免 XSS。
  function renderMessageText(parent, value) {
    const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    while (parent.firstChild) parent.removeChild(parent.firstChild);
    const pattern = /!\[([^\]]*)\]\(([^)\s]+)\)|\[([^\]]+)\]\(([^)\s]+)\)/g;
    let lastIndex = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > lastIndex) {
        parent.appendChild(document.createTextNode(text.slice(lastIndex, match.index)));
      }
      if (match[1] !== undefined) {
        const url = safeUrl(match[2]);
        if (url) {
          const img = document.createElement('img');
          img.src = url;
          img.alt = match[1] || '';
          img.loading = 'lazy';
          parent.appendChild(img);
        } else {
          parent.appendChild(document.createTextNode(match[0]));
        }
      } else {
        const url = safeUrl(match[4]);
        if (url) {
          const a = document.createElement('a');
          a.href = url;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          a.textContent = match[3];
          parent.appendChild(a);
        } else {
          parent.appendChild(document.createTextNode(match[0]));
        }
      }
      lastIndex = pattern.lastIndex;
    }
    if (lastIndex < text.length) {
      parent.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
  }

  // 飞书风格消息时间：Hover 时展示在气泡上方。服务端 created_at 为 UTC ISO 字符串，
  // 统一按 UTC+8（Asia/Shanghai）渲染，不依赖浏览器本地时区；按 今天/昨天/今年/往年
  // 分级格式化。返回空串表示无时间。
  function formatMessageTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (isNaN(date.getTime())) return '';
    // East-8 (Asia/Shanghai) regardless of browser timezone
    const tz = new Date(date.getTime() + 8 * 3600 * 1000);
    const nowTz = new Date(Date.now() + 8 * 3600 * 1000);
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    const hhmm = pad(tz.getUTCHours()) + ':' + pad(tz.getUTCMinutes());
    const startOfToday = Date.UTC(nowTz.getUTCFullYear(), nowTz.getUTCMonth(), nowTz.getUTCDate());
    const startOfMsgDay = Date.UTC(tz.getUTCFullYear(), tz.getUTCMonth(), tz.getUTCDate());
    const dayDiff = Math.round((startOfToday - startOfMsgDay) / 86400000);
    if (dayDiff === 0) return hhmm;
    if (dayDiff === 1) return '昨天 ' + hhmm;
    if (tz.getUTCFullYear() === nowTz.getUTCFullYear()) {
      return (tz.getUTCMonth() + 1) + '月' + tz.getUTCDate() + '日 ' + hhmm;
    }
    return tz.getUTCFullYear() + '/' + (tz.getUTCMonth() + 1) + '/' + tz.getUTCDate() + ' ' + hhmm;
  }

  // 统一时间格式化：服务端时间统一 UTC 存储，展示一律按 UTC+8（Asia/Shanghai）渲染，
  // 不依赖浏览器本地时区（与 sandbox.js / host.js / tasks.js 等保持一致）。
  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    // East-8 (Asia/Shanghai) regardless of browser timezone
    const tz = new Date(d.getTime() + 8 * 3600 * 1000);
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    return tz.getUTCFullYear() + '-' + pad(tz.getUTCMonth() + 1) + '-' + pad(tz.getUTCDate())
      + ' ' + pad(tz.getUTCHours()) + ':' + pad(tz.getUTCMinutes()) + ':' + pad(tz.getUTCSeconds());
  }

  function formatDebugJson(value) {
    if (typeof value !== 'string') return JSON.stringify(value, null, 2);
    try {
      const parsed = JSON.parse(value);
      return JSON.stringify(parsed, null, 2);
    } catch (error) {
      return value;
    }
  }

  // Browser screenshots stay in the server-side screenshot store: tool results
  // deliberately contain neither bytes nor a storage reference.  The Dashboard
  // can still recognise the safe result envelope and resolve the associated
  // browser session through its existing, session-scoped API.
  function isSuccessfulBrowserScreenshot(content) {
    const items = Array.isArray(content) ? content : [content];
    return items.some((item) => {
      let payload = item;
      if (typeof payload === 'string') {
        try { payload = JSON.parse(payload); } catch (_) { return false; }
      }
      if (!payload || typeof payload !== 'object') return false;
      const result = payload.content;
      return payload.name === 'browser_screenshot'
        && payload.status === 'success'
        && result && typeof result === 'object'
        && result.action_type === 'screenshot'
        && result.status === 'success'
        && result.screenshot_captured === true;
    });
  }

  function appendBrowserScreenshotPreview(parent, content, capturedAt) {
    if (!parent || !isSuccessfulBrowserScreenshot(content) || !currentSessionId) return;
    const browser = api && api.browser;
    if (!browser || typeof browser.listSessions !== 'function') return;
    const sessionId = currentSessionId;
    const preview = document.createElement('div');
    preview.className = 'tool-screenshot-preview';
    const image = document.createElement('img');
    image.className = 'tool-screenshot-preview__image';
    image.alt = '浏览器截图';
    image.loading = 'lazy';
    image.hidden = true;
    preview.appendChild(image);
    parent.appendChild(preview);

    browser.listSessions(sessionId).then((result) => {
      if (currentSessionId !== sessionId) return;
      const sessions = result && Array.isArray(result.sessions) ? result.sessions : [];
      // BrowserService binds browser sessions to one N-Agent session. Prefer the
      // newest record, then let the existing authenticated image endpoint decide
      // whether its screenshot is still available.
      const latest = sessions.slice().sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))[0];
      if (!latest || !latest.id) { preview.remove(); return; }
      image.src = '/chat/browser/sessions/' + encodeURIComponent(latest.id)
        + '/screenshot?n_agent_session_id=' + encodeURIComponent(sessionId)
        + (capturedAt ? '&captured_at=' + encodeURIComponent(capturedAt) : '');
      image.hidden = false;
      image.addEventListener('error', () => { preview.remove(); });
    }).catch(() => { preview.remove(); });
  }

  function appendDebugJson(parent, value) {
    parent.textContent = formatDebugJson(value);
  }

  function formatToolDebugContent(content) {
    if (!Array.isArray(content)) return formatDebugJson(content);
    return content.map((item, index) => `#${index + 1}\n${formatDebugJson(item)}`).join('\n\n');
  }

  function appendToolDebugContent(parent, content) {
    parent.textContent = formatToolDebugContent(content);
  }

  function hasVisibleContent(value) {
    if (typeof value === 'string') return value.length > 0;
    if (value === null || typeof value === 'undefined') return false;
    if (Array.isArray(value)) return value.length > 0;
    if (typeof value === 'object') return Object.keys(value).length > 0;
    return true;
  }

  function clearNode(node) { ui.clear(node); }

  function createSvgElement(tag, attrs) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs || {}).forEach(([key, value]) => el.setAttribute(key, value));
    return el;
  }

  function createMemoryTriggerIcon() {
    const svg = createSvgElement('svg', {
      class: 'chat-memory-trigger__icon',
      viewBox: '0 0 24 24',
      fill: 'none',
      'aria-hidden': 'true',
    });
    svg.append(
      createSvgElement('path', {
        d: 'M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 0 0 12 18a3 3 0 0 0 0-6 3 3 0 0 0 0-6Z',
        stroke: 'currentColor',
        'stroke-width': '2',
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
      }),
      createSvgElement('path', {
        d: 'M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 0 1 12 18',
        stroke: 'currentColor',
        'stroke-width': '2',
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
      })
    );
    return svg;
  }

  function setSending(next) {
    isSending = next;
    const btn = ui.byId('chat-send');
    const input = ui.byId('chat-input');
    if (btn) btn.disabled = next;
    if (input) input.disabled = next;
  }

  function setStatusMessage(text, role) {
    const stack = ui.byId('chat-message-stack');
    if (!stack) return;
    clearNode(stack);
    const el = document.createElement('div');
    el.className = `msg ${role || 'system'}`;
    appendText(el, text);
    stack.appendChild(el);
  }

  function showEmptyState() {
    const stack = ui.byId('chat-message-stack');
    if (!stack) return;
    clearNode(stack);
    const empty = document.createElement('div');
    empty.className = 'empty-hero';
    const title = document.createElement('h1');
    title.textContent = 'How can N-Agent help?';
    const text = document.createElement('p');
    text.textContent = '开始一段新会话，或从左侧选择已有会话。';
    empty.append(title, text);
    stack.appendChild(empty);
  }

  function scrollToBottom() {
    const messages = ui.byId('chat-messages');
    if (messages) messages.scrollTop = messages.scrollHeight;
  }

  // 任务状态中等待用户交互的状态文案（如"等待批准"），这类消息默认展开；其余任务状态默认折叠
  const TASK_LIFECYCLE_AWAITING_LABELS = ['等待批准'];

  // === Task card schema (T6/T7) ===
  // Canonical card payload from lifecycle_writer (_lifecycle_card in task_run_service):
  //   { schema_version, kind, task_id, status, title, summary, available_actions }
  // Only ui.task_lifecycle messages with a valid card render as inline interactive cards;
  // invalid/no-card messages fall back to the existing <details><pre> rendering.
  const TASK_CARD_SCHEMA_VERSION = 1;
  const TASK_CARD_KIND = 'task_lifecycle';
  const TASK_CARD_STATUSES = new Set(['waiting_approval', 'failed', 'expired']);
  // interaction_type is only meaningful for waiting_approval cards; it selects
  // the button group (approval -> approve/reject, intent_request -> revise/cancel).
  const TASK_CARD_INTERACTION_TYPES = new Set(['approval', 'intent_request']);
  const TASK_ACTION_LABELS = {
    approve: '批准',
    reject: '拒绝',
    revise: '补充并继续',
    cancel: '取消',
    retry: '重试',
  };
  // Button class per action semantic, aligned to the shared .btn system
  // (primary=positive action, danger=destructive action, btn=neutral).
  const TASK_ACTION_BTN_CLASS = {
    approve: 'btn btn--primary',
    retry: 'btn btn--primary',
    reject: 'btn btn--danger',
    cancel: 'btn btn--danger',
    revise: 'btn',
  };
  // Explicit handler allowlist: action string -> fixed api.task function.
  // Prevents dynamic api.task[action] indexing that could call arbitrary API methods.
  const TASK_CARD_ACTION_HANDLERS = {
    approve: (apiRef, id) => apiRef.task.approve(id),
    reject: (apiRef, id) => apiRef.task.reject(id),
    revise: (apiRef, id, note) => apiRef.task.revise(id, note),
    cancel: (apiRef, id) => apiRef.task.cancel(id),
    retry: (apiRef, id) => apiRef.task.retry(id),
  };
  const TASK_CARD_ALLOWED_ACTIONS = new Set(Object.keys(TASK_CARD_ACTION_HANDLERS));

  // validateTaskCard(card) -> canonical card object | null.
  // Returns a NEW object (does not mutate server payload). Only allowlist actions
  // survive, preserving their original order; unknown actions are dropped.
  // Returns null if the card is missing required fields, has wrong types,
  // unknown schema_version/kind/status, blank task_id, or no valid actions.
  // interaction_type is an optional field meaningful only for waiting_approval:
  //   - present -> must be 'approval' or 'intent_request' (else null)
  //   - absent  -> defaults to 'approval'
  //   - non-waiting_approval cards never carry interaction_type in canonical
  //     (failed/expired have no interaction_type on the wire)
  function validateTaskCard(card) {
    if (!card || typeof card !== 'object' || Array.isArray(card)) return null;
    if (card.schema_version !== TASK_CARD_SCHEMA_VERSION) return null;
    if (card.kind !== TASK_CARD_KIND) return null;
    if (!TASK_CARD_STATUSES.has(card.status)) return null;
    const taskId = typeof card.task_id === 'string' ? card.task_id.trim() : '';
    if (!taskId) return null;
    if (typeof card.title !== 'string') return null;
    if (typeof card.summary !== 'string') return null;
    if (!Array.isArray(card.available_actions)) return null;
    let interactionType = undefined;
    if (card.status === 'waiting_approval') {
      const raw = card.interaction_type;
      if (raw === undefined || raw === null) {
        interactionType = 'approval';
      } else {
        if (typeof raw !== 'string' || !TASK_CARD_INTERACTION_TYPES.has(raw)) return null;
        interactionType = raw;
      }
    }
    const actions = [];
    for (const a of card.available_actions) {
      if (typeof a === 'string' && TASK_CARD_ALLOWED_ACTIONS.has(a) && actions.indexOf(a) === -1) {
        actions.push(a);
      }
    }
    if (actions.length === 0) return null;
    const canonical = {
      schema_version: TASK_CARD_SCHEMA_VERSION,
      kind: TASK_CARD_KIND,
      task_id: taskId,
      status: card.status,
      title: card.title,
      summary: card.summary,
      available_actions: actions,
    };
    if (interactionType !== undefined) canonical.interaction_type = interactionType;
    return canonical;
  }

  // resolveTaskCardStates(messages) -> Map<task_id, {kind, status?}>.
  // Collects unique task_ids from valid cards, issues one GET per task (dedup),
  // and returns per-task authoritative state. Each card later compares its own
  // card.status against the entry to determine active/stale/unavailable.
  //   { kind: 'resolved', status }  GET succeeded, status is authoritative
  //   { kind: 'stale' }              task_not_found / task_state_invalid / task_conflict
  //   { kind: 'unavailable' }        network error / unknown code
  async function resolveTaskCardStates(messages) {
    const taskIds = new Set();
    for (const msg of messages) {
      const card = validateTaskCard(msg && msg.card);
      if (card) taskIds.add(card.task_id);
    }
    if (taskIds.size === 0) return new Map();
    const entries = await Promise.all(Array.from(taskIds).map(async (id) => {
      try {
        const detail = await api.task.get(id);
        // GET /chat/tasks/{id} returns {task: {...}, comments, events, ...};
        // authoritative status is on detail.task.status. Support flat {status}
        // fallback for harness stubs / legacy shapes.
        const taskStatus = (detail && detail.task && detail.task.status) || (detail && detail.status);
        return [id, { kind: 'resolved', status: taskStatus }];
      } catch (e) {
        const code = (e && e.message) ? String(e.message) : '';
        if (code === 'task_not_found' || code === 'task_state_invalid' || code === 'task_conflict') {
          return [id, { kind: 'stale' }];
        }
        return [id, { kind: 'unavailable' }];
      }
    }));
    return new Map(entries);
  }

  // Decision receipts written by TaskService for approve/reject/revise/cancel
  // (content: "已批准: {task_id} - {title}"; revise appends " | 修订指示: ...").
  // resolveTaskDecisions(messages) -> Map<task_id, decision label>.
  // Scans ui.task_lifecycle text receipts (no card) so a stale waiting_approval
  // card can surface the recorded approval/intent-supplement (revise) decision
  // as a light-gray hint. This covers the primary "任务状态已变更" trigger --
  // fresh load/switch of a session whose task already transitioned -- where no
  // in-memory decision is available but the receipt is already in the session.
  // Card-bearing messages are skipped (they are cards, not receipts). Later
  // receipts overwrite earlier ones, so the latest decision per task wins.
  const TASK_DECISION_PREFIXES = ['已批准', '已拒绝', '已修订', '已取消'];
  function resolveTaskDecisions(messages) {
    const decisions = new Map();
    for (const msg of messages) {
      if (!msg || msg.role !== 'system' || msg.name !== 'ui.task_lifecycle') continue;
      if (validateTaskCard(msg.card)) continue;
      const content = typeof msg.content === 'string' ? msg.content : '';
      for (const decision of TASK_DECISION_PREFIXES) {
        const prefix = decision + ': ';
        if (content.indexOf(prefix) === 0) {
          const rest = content.slice(prefix.length);
          const dash = rest.indexOf(' - ');
          const taskId = (dash === -1 ? rest : rest.slice(0, dash)).trim();
          if (taskId) decisions.set(taskId, decision);
          break;
        }
      }
    }
    return decisions;
  }

  function resolveToolApprovalDecisions(messages) {
    const decisions = new Map();
    for (const msg of messages) {
      const card = msg && msg.card;
      if (!card || card.kind !== 'tool_approval_resolution') continue;
      if (typeof card.confirmation_id !== 'string') continue;
      if (card.status === 'approved' || card.status === 'rejected') {
        decisions.set(card.confirmation_id, {
          status: card.status,
          scope: typeof card.scope === 'string' ? card.scope : undefined,
        });
      }
    }
    return decisions;
  }

  // computeCardState(card, entry) -> 'active' | 'stale' | 'unavailable'.
  // Per-card comparison: card.status vs authoritative entry status.
  function computeCardState(card, entry) {
    if (!entry) return 'unavailable';
    if (entry.kind === 'unavailable') return 'unavailable';
    if (entry.kind === 'stale') return 'stale';
    if (entry.kind === 'resolved') return entry.status === card.status ? 'active' : 'stale';
    return 'unavailable';
  }

  function isTaskLifecycleAwaitingInteraction(message) {
    // Prefer canonical card status (three interaction states); no-card messages
    // fall back to text matching for backward compatibility with history messages.
    const card = validateTaskCard(message && message.card);
    if (card) return TASK_CARD_STATUSES.has(card.status);
    const content = message && message.content;
    const text = typeof content === 'string' ? content : String(content || '');
    return TASK_LIFECYCLE_AWAITING_LABELS.some((label) => text.indexOf(label) !== -1);
  }

  // 去掉任务消息抬头 [任务指令] / [任务状态]：新增消息已无抬头，历史消息按行剥离行首抬头
  // （合并块中每行各带一个抬头，逐行剥离避免误伤正文中的同类字符串）。
  function stripTaskMessageHeader(text) {
    if (typeof text !== 'string') return text;
    return text.split('\n').map((line) =>
      line.replace(/^\[任务指令\]\s?/, '').replace(/^\[任务状态\]\s?/, '')
    ).join('\n');
  }

  // 非真人进程来源的 user 消息渲染为左对齐状态卡片（灰底卡片，区别于真人 user 蓝底右对齐气泡）。
  // work task / judge task 前缀的消息样式打平 ui.task_lifecycle 任务状态卡片（className `msg system`）：
  //   单独时 summary 固定 = `任务状态`（与合并卡、standalone lifecycle 一致），pre 放原始 content，
  //   details 默认 open=false；与 ui.task_command/ui.task_lifecycle 折叠卡片结构一致。
  // 相邻的 work task / judge task 与 ui.task_lifecycle（无 card）合并为一个 details 卡片：
  //   summary 固定 = `任务状态`，pre 多行拼接（lifecycle 行用原文，work/judge 行带前缀），open=false。
  // 其余进程消息（schedule/curator 等）原样渲染为非折叠 msg--process-card 左对齐卡片。
  const PROCESS_SOURCES = new Set(['task', 'schedule', 'curator']);
  const PROCESS_CONTENT_PREFIXES = [
    { match: 'work task ', label: '查询状态' },
    { match: 'judge task ', label: '判断结束' },
  ];

  // T10: ui.artifact card publish_sync_state -> badge label. The badge class
  // also carries the raw state (chat-artifact-card__badge--<state>) so CSS can
  // color it. Unknown states fall back to their raw value, then "未发布".
  const ARTIFACT_PUBLISH_LABELS = {
    unpublished: '未发布',
    current: '已发布',
    outdated: '已过期',
  };

  // work task / judge task 前缀的消息：
  // - 单独卡片 summary 固定 = `任务状态`（与合并卡、standalone lifecycle 一致，不再用前缀+content）
  // - 合并卡片行 = `<label>: <content>`（work task -> 查询状态、judge task -> 判断结束，行级前缀区分 lifecycle 行）
  // 仅 string content 且行首命中任一 PROCESS_CONTENT_PREFIXES 时返回对应 label；否则返回 null。
  function foldableProcessLabel(content) {
    if (typeof content !== 'string') return null;
    for (const rule of PROCESS_CONTENT_PREFIXES) {
      if (content.startsWith(rule.match)) return rule.label;
    }
    return null;
  }

  // 单独 work task / judge task 卡片 summary 已统一为固定 `任务状态`（见 isFoldedProcess 分支），
  // `查询状态`/`判断结束` label 仅作为合并卡片内的行级前缀（taskStatusLineForMessage）。

  // work task / judge task 前缀的消息渲染为折叠卡片（进程内部消息不占 Chat 空间），
  // 仅 string content 且行首命中任一 PROCESS_CONTENT_PREFIXES 时为 true；多模态/空串等为 false。
  function isFoldableProcessContent(content) {
    return foldableProcessLabel(content) !== null;
  }

  // 合并卡片行生成：
  // - work task / judge task（role=user, source=task, 前缀命中）: `<label>: <content>`（带前缀）
  // - ui.task_lifecycle 无 card: 原文（[任务状态] 抬头由 stripTaskMessageHeader 在渲染时剥离）
  function taskStatusLineForMessage(msg) {
    if (msg && msg.role === 'user' && PROCESS_SOURCES.has(msg.source) && typeof msg.content === 'string') {
      const label = foldableProcessLabel(msg.content);
      if (label !== null) return label + ': ' + msg.content;
    }
    return String(msg && msg.content !== undefined ? msg.content : '');
  }

  // === T7: Task card builder + action handler ===
  // buildTaskCardElement(message, card, state, decision) -> DOM element.
  // state is 'active' | 'stale' | 'unavailable'. 'settled' is a post-action
  // client-side state managed by handleTaskCardAction (removes actions, updates feedback).
  // `decision` is an optional recorded decision label (已批准/已拒绝/已修订/已取消)
  // scanned from session receipts by resolveTaskDecisions; when present on a
  // stale card it is shown as a light-gray outcome hint on the last line,
  // replacing the generic "任务状态已变更" prompt.
  // All dynamic values are written via textContent (no innerHTML). Buttons are
  // native <button type="button">. The feedback node is always created with
  // aria-live="polite" so screen readers announce state changes.
  function buildTaskCardElement(message, card, state, decision) {
    const el = document.createElement('div');
    el.className = 'msg system task-card';
    if (state === 'stale') el.classList.add('task-card__stale');
    if (state === 'unavailable') el.classList.add('task-card__unavailable');
    el.dataset.name = message.name || 'ui.task_lifecycle';
    el.dataset.debugKind = 'task';

    const details = document.createElement('details');
    details.open = true;  // three interaction states default open

    const summary = document.createElement('summary');
    summary.textContent = card.status === 'waiting_approval' ? '任务待审批'
      : card.status === 'failed' ? '任务已失败'
      : card.status === 'expired' ? '任务已过期'
      : '任务状态';

    const body = document.createElement('div');
    body.className = 'task-card__body';

    const titleEl = document.createElement('div');
    titleEl.className = 'task-card__title';
    titleEl.textContent = card.title;
    body.appendChild(titleEl);

    const meta = document.createElement('div');
    meta.className = 'task-card__meta';
    meta.textContent = 'ID: ' + card.task_id + ' · 状态: ' + card.status;
    body.appendChild(meta);

    if (card.summary) {
      const sumEl = document.createElement('div');
      sumEl.className = 'task-card__summary';
      sumEl.textContent = card.summary;
      body.appendChild(sumEl);
    }

    const feedback = document.createElement('div');
    feedback.className = 'task-card__feedback';
    feedback.setAttribute('aria-live', 'polite');

    // Only active waiting-approval cards are actionable in the conversation.
    // Failed and expired cards are terminal status records: keep their details
    // visible, but never render retry/cancel (or any future server-supplied)
    // action buttons. stale/unavailable cards also render body + feedback only.
    if (state === 'active' && card.status === 'waiting_approval' && card.available_actions.length > 0) {
      const actions = document.createElement('div');
      actions.className = 'task-card__actions';
      const inflight = { value: false };
      const buttonRefs = [];
      let textareaRef = null;

      for (const action of card.available_actions) {
        if (action === 'revise') {
          const label = document.createElement('label');
          label.className = 'task-card__label';
          label.textContent = '补充说明';
          const ta = document.createElement('textarea');
          ta.className = 'task-card__textarea';
          ta.id = 'task-card-revise-' + card.task_id;
          label.htmlFor = ta.id;
          actions.append(label, ta);
          textareaRef = ta;
        }
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'task-card__btn ' + (TASK_ACTION_BTN_CLASS[action] || 'btn');
        btn.dataset.action = action;
        btn.dataset.taskId = card.task_id;
        btn.textContent = TASK_ACTION_LABELS[action] || action;
        // Explicit async handler consumption: .catch prevents unhandledRejection.
        btn.addEventListener('click', () => {
          handleTaskCardAction({
            card, action, actions, feedback, inflight,
            textarea: textareaRef, buttons: buttonRefs,
          }).catch(() => {});
        });
        actions.appendChild(btn);
        buttonRefs.push(btn);
      }
      body.appendChild(actions);
    } else {
      if (state === 'stale') {
        // Surface the recorded approval/intent-supplement (revise) decision as
        // a light-gray hint when a receipt is present in the session; otherwise
        // fall back to a concise status-changed notice. Drops the old
        // "任务状态已变更，操作不可用" prompt: "操作不可用" is already implied by
        // the absence of action buttons, and a known decision is informational
        // rather than a warning. Scoped to waiting_approval cards -- the receipts
        // (已批准/已拒绝/已修订/已取消) are decisions on a pending-approval task,
        // so showing one on a failed/expired historical card would be misleading.
        feedback.textContent = (decision && card.status === 'waiting_approval') ? decision : '任务状态已变更';
      } else if (state === 'unavailable') {
        feedback.textContent = '无法获取任务状态，请稍后重试';
      }
    }

    body.appendChild(feedback);
    details.append(summary, body);
    el.appendChild(details);
    return el;
  }

  // handleTaskCardAction(ctx) -> Promise<void>.
  // State machine: success -> settled (remove actions, show action result, await refresh);
  // task_state_invalid/task_conflict/task_not_found -> stale (remove actions, refresh);
  // task_invalid/network/unknown -> show error, restore controls (still active).
  // revise: trim + Array.from(note).length code-point validation (1..2000) before any API call.
  // In-flight flag prevents duplicate submissions. finally only restores controls if
  // the actions container is still attached (i.e., card is still active/retryable).
  async function handleTaskCardAction(ctx) {
    const { card, action, actions, feedback, inflight, textarea, buttons } = ctx;
    if (inflight.value) return;  // dedup: second click during in-flight is a no-op

    // revise: validate note (Unicode code points, not UTF-16 units) before API call.
    // Array.from splits into code points; textarea.maxLength counts UTF-16 units.
    if (action === 'revise') {
      const raw = (textarea && typeof textarea.value === 'string') ? textarea.value : '';
      const note = raw.trim();
      const codePoints = Array.from(note).length;
      if (codePoints < 1) {
        feedback.textContent = '补充内容不能为空';
        return;
      }
      if (codePoints > 2000) {
        feedback.textContent = '补充内容过长（最多 2000 个字符）';
        return;
      }
    }

    inflight.value = true;
    buttons.forEach((b) => { b.disabled = true; });

    try {
      const handler = TASK_CARD_ACTION_HANDLERS[action];
      const note = (action === 'revise')
        ? (textarea.value || '').trim()
        : undefined;
      await handler(api, card.task_id, note);

      const successText = action === 'approve' ? '已批准'
        : action === 'reject' ? '已拒绝'
        : action === 'revise' ? '补充: ' + note
        : '操作已提交';
      // Success -> settled: remove actions, show the concrete result, await refresh.
      // Direct getSessionDetail + applySessionDetail (not refreshCurrentSession)
      // so refresh failures are catchable and can update the feedback text.
      if (actions.parentNode) actions.parentNode.removeChild(actions);
      feedback.textContent = successText;
      if (currentSessionId) {
        try {
          const detail = await api.getSessionDetail(currentSessionId);
          await applySessionDetail(detail, {
            preserveScroll: false, skipToolCalls: false, skipSessionList: false, applyExternalMemoryState: true,
            partialMessages: true,
          });
        } catch (refreshErr) {
          feedback.textContent = successText + '，刷新失败';
        }
      }
    } catch (e) {
      const code = (e && e.message) ? String(e.message) : '';
      if (code === 'task_state_invalid' || code === 'task_conflict' || code === 'task_not_found') {
        // stale: remove actions, show error, attempt refresh.
        if (actions.parentNode) actions.parentNode.removeChild(actions);
        feedback.textContent = describeTaskError(e);
        if (currentSessionId) {
          try {
          const detail = await api.getSessionDetail(currentSessionId);
          await applySessionDetail(detail, {
            preserveScroll: false, skipToolCalls: false, skipSessionList: false, applyExternalMemoryState: true,
            partialMessages: true,
            });
          } catch (_) { /* best-effort; stale feedback already shown */ }
        }
      } else {
        // task_invalid / network / unknown: show stable error mapping, controls restored.
        feedback.textContent = describeTaskError(e);
      }
    } finally {
      inflight.value = false;
      // Only restore controls if actions container is still attached (still active/retryable).
      // If settled/stale removed actions, do not re-enable.
      if (actions.parentNode) {
        buttons.forEach((b) => { b.disabled = false; });
      }
    }
  }

  // === T4: Generic tool approval card (independent of task cards) ===
  // Reusable renderer for any CONFIRM tool. Accepts only the 5-field approval
  // payload (confirmation_id, tool_name, description, arguments_summary,
  // expires_at). No browser-specific branches. All display fields set via
  // textContent (never innerHTML). The card is ephemeral: it lives only in the
  // assistant bubble during the stream that produced it, and is never persisted
  // to session messages.

  // Cross-environment child traversal: harness stubs store children in _kids,
  // real DOM uses childNodes. Returns an array of child elements.
  function childNodesOf(node) {
    if (!node) return [];
    if (node._kids) return node._kids;
    var arr = [];
    if (node.childNodes) {
      for (var i = 0; i < node.childNodes.length; i++) arr.push(node.childNodes[i]);
    }
    return arr;
  }

  // Buffered SSE parser: accumulates text across network chunks, splits on
  // COMPLETE events (terminated by \n\n, \r\n\r\n, or \r\r), reassembles
  // multi-line data: fields per the SSE spec (concatenated with \n). Maintains
  // a bounded buffer (drops if > 1 MiB to avoid unbounded memory).
  function createSSEParser() {
    var buffer = '';
    var MAX_BUFFER = 1 << 20; // 1 MiB

    function findBoundary(buf) {
      var best = -1, bestLen = 0;
      var i;
      i = buf.indexOf('\n\n');
      if (i !== -1) { best = i; bestLen = 2; }
      i = buf.indexOf('\r\n\r\n');
      if (i !== -1 && (best === -1 || i < best)) { best = i; bestLen = 4; }
      i = buf.indexOf('\r\r');
      if (i !== -1 && (best === -1 || i < best)) { best = i; bestLen = 2; }
      return best === -1 ? null : { idx: best, len: bestLen };
    }

    function parseEvent(raw) {
      var lines = raw.replace(/\r\n/g, '\n').replace(/\r/g, '\n').split('\n');
      var dataLines = [];
      for (var k = 0; k < lines.length; k++) {
        var line = lines[k];
        if (line.indexOf('data:') === 0) {
          dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      if (dataLines.length === 0) return null;
      return dataLines.join('\n');
    }

    return {
      feed: function (text) {
        buffer += text;
        if (buffer.length > MAX_BUFFER) buffer = '';
        var events = [];
        var b;
        while ((b = findBoundary(buffer))) {
          var raw = buffer.slice(0, b.idx);
          buffer = buffer.slice(b.idx + b.len);
          var data = parseEvent(raw);
          if (data !== null) events.push(data);
        }
        return events;
      },
      flush: function () {
        var trimmed = buffer;
        buffer = '';
        if (!trimmed.trim()) return [];
        var data = parseEvent(trimmed);
        return data !== null ? [data] : [];
      },
      getBufferLength: function () { return buffer.length; }
    };
  }

  // Validate the approval payload: must be a non-null object with all 5
  // required fields as strings, and confirmation_id non-empty after trim.
  function isValidApprovalPayload(approval) {
    if (!approval || typeof approval !== 'object') return false;
    var fields = ['confirmation_id', 'tool_name', 'description', 'arguments_summary', 'expires_at'];
    for (var i = 0; i < fields.length; i++) {
      if (typeof approval[fields[i]] !== 'string') return false;
    }
    if (!approval.confirmation_id || !approval.confirmation_id.trim()) return false;
    return true;
  }

  function validateToolApprovalCard(card) {
    if (!card || typeof card !== 'object' || card.kind !== 'tool_approval') return null;
    return isValidApprovalPayload(card.approval) ? card.approval : null;
  }

  function findApprovalCardById(container, id) {
    function walk(node) {
      if (!node) return null;
      if (node.dataset && node.dataset.confirmationId === id) return node;
      var kids = childNodesOf(node);
      for (var i = 0; i < kids.length; i++) {
        var found = walk(kids[i]);
        if (found) return found;
      }
      return null;
    }
    return walk(container);
  }

  function findApprovalCards(container) {
    var result = [];
    function walk(node) {
      if (!node) return;
      if (node.className && typeof node.className === 'string' &&
          node.className.indexOf('tool-approval-card') !== -1 &&
          node.dataset && node.dataset.confirmationId) {
        result.push(node);
      }
      var kids = childNodesOf(node);
      for (var i = 0; i < kids.length; i++) walk(kids[i]);
    }
    walk(container);
    return result;
  }

  function findCardButtons(card) {
    var result = [];
    function walk(node) {
      if (!node) return;
      if (node.tagName === 'BUTTON' && node.dataset && node.dataset.choice) {
        result.push(node);
      }
      var kids = childNodesOf(node);
      for (var i = 0; i < kids.length; i++) walk(kids[i]);
    }
    walk(card);
    return result;
  }

  // Choice labels and button order for the generic approval card. Defined once
  // at module scope so label changes need only one edit.
  var APPROVAL_CHOICE_LABELS = { once: '仅本次允许', trust_session: '信任本会话', cancel: '拒绝' };
  var APPROVAL_CHOICE_ORDER = ['once', 'trust_session', 'cancel'];
  // Button class per choice semantic, aligned to the shared .btn system:
  // approve choices (once/trust_session) use primary, reject (cancel) uses danger.
  var APPROVAL_CHOICE_BTN_CLASS = { once: 'btn btn--primary', trust_session: 'btn btn--primary', cancel: 'btn btn--danger' };
  // Trust-scope labels surfaced on the resolved card after a refresh. The
  // server persists decision.scope ("once" | "session"); an approved card
  // shows "已批准 · {scope label}" so the post-resolution state retains the
  // full approval context instead of collapsing to a bare "已批准".
  var APPROVAL_SCOPE_LABELS = { once: '仅信任本次', session: '信任本会话' };

  // Render the generic approval card into the container (the streaming assistant
  // bubble). Dedup by confirmation_id: if a card for that ID already exists in
  // the container, no duplicate is rendered.
  function renderToolApprovalCard(container, approval, streamSessionId, resolution) {
    if (!container || !approval || !isValidApprovalPayload(approval)) return;
    var id = String(approval.confirmation_id);
    if (findApprovalCardById(container, id)) return;

    var card = document.createElement('div');
    card.className = 'tool-approval-card';
    card.dataset.confirmationId = id;

    var title = document.createElement('div');
    title.className = 'tool-approval-card__title';
    title.textContent = '工具操作确认';
    card.appendChild(title);

    var toolField = document.createElement('div');
    toolField.className = 'tool-approval-card__field';
    toolField.textContent = '工具: ' + approval.tool_name;
    card.appendChild(toolField);

    var descField = document.createElement('div');
    descField.className = 'tool-approval-card__field';
    descField.textContent = '描述: ' + approval.description;
    card.appendChild(descField);

    var argsField = document.createElement('div');
    argsField.className = 'tool-approval-card__field tool-approval-card__args';
    argsField.textContent = '参数: ' + approval.arguments_summary;
    card.appendChild(argsField);

    var expiresField = document.createElement('div');
    expiresField.className = 'tool-approval-card__field tool-approval-card__expires';
    expiresField.textContent = '过期时间: ' + formatTime(approval.expires_at);
    card.appendChild(expiresField);

    var feedback = document.createElement('div');
    feedback.className = 'tool-approval-card__feedback';
    feedback.setAttribute('aria-live', 'polite');

    var actions = document.createElement('div');
    actions.className = 'tool-approval-card__actions';

    var cardState = { ended: false, submitted: false };
    card._approvalState = cardState;
    var buttons = [];
    for (var ci = 0; ci < APPROVAL_CHOICE_ORDER.length; ci++) {
      (function (choice) {
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'tool-approval-card__btn ' + (APPROVAL_CHOICE_BTN_CLASS[choice] || 'btn');
        btn.dataset.choice = choice;
        btn.textContent = APPROVAL_CHOICE_LABELS[choice] || choice;
        btn.addEventListener('click', function () {
          if (cardState.ended || cardState.submitted) return;
          submitToolApproval(id, choice, streamSessionId, buttons, feedback, cardState).catch(function () {});
        });
        actions.appendChild(btn);
        buttons.push(btn);
      })(APPROVAL_CHOICE_ORDER[ci]);
    }

    card.appendChild(actions);
    card.appendChild(feedback);
    var resolvedStatus = resolution && resolution.status;
    if (resolvedStatus === 'approved' || resolvedStatus === 'rejected') {
      cardState.ended = true;
      buttons.forEach(function (button) { button.disabled = true; });
      if (resolvedStatus === 'approved') {
        var scopeLabel = APPROVAL_SCOPE_LABELS[resolution && resolution.scope];
        feedback.textContent = scopeLabel ? ('已批准 · ' + scopeLabel) : '已批准';
      } else {
        feedback.textContent = '已拒绝';
      }
    }
    container.appendChild(card);
  }

  // Submit a choice to /chat/tool-approvals/{confirmation_id}. Disables all
  // buttons immediately, sends exactly one POST with the stream-captured
  // session ID, and handles 204/404/409/5xx/network per spec.
  async function submitToolApproval(confirmationId, choice, streamSessionId, buttons, feedback, cardState) {
    if (cardState.submitted) return;
    cardState.submitted = true;
    buttons.forEach(function (b) { b.disabled = true; });

    try {
      var res = await fetch('/chat/tool-approvals/' + encodeURIComponent(confirmationId), {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Session-ID': streamSessionId
        },
        body: JSON.stringify({ choice: choice })
      });
      if (res.status === 204) {
        feedback.textContent = '已提交: ' + (APPROVAL_CHOICE_LABELS[choice] || choice);
        // buttons stay disabled, keep waiting for the rest of the stream
      } else if (res.status === 404 || res.status === 409) {
        feedback.textContent = '已过期或已处理';
        // terminal: keep buttons disabled, no auto-retry
      } else {
        // 5xx or unexpected: retryable (unless stream has ended)
        if (!cardState.ended) {
          cardState.submitted = false;
          feedback.textContent = '提交失败，请重试';
          buttons.forEach(function (b) { b.disabled = false; });
        } else {
          feedback.textContent = '提交失败';
        }
      }
    } catch (e) {
      // network error: retryable (unless stream has ended)
      if (!cardState.ended) {
        cardState.submitted = false;
        feedback.textContent = '网络错误，请重试';
        buttons.forEach(function (b) { b.disabled = false; });
      } else {
        feedback.textContent = '网络错误';
      }
    }
  }

  // Disable all approval cards in the container: set ended flag (prevents
  // future clicks from POSTing) and disable all buttons. Called on stream
  // end, stream failure, or session switch.
  function disableApprovalCards(container) {
    if (!container) return;
    var cards = findApprovalCards(container);
    for (var i = 0; i < cards.length; i++) {
      if (cards[i]._approvalState) cards[i]._approvalState.ended = true;
      var btns = findCardButtons(cards[i]);
      for (var j = 0; j < btns.length; j++) btns[j].disabled = true;
    }
  }

  // Process a single SSE data payload: route approval envelopes to the card
  // renderer, OpenAI chunks to text streaming. Returns false if the stream
  // should terminate (invalid approval payload), true otherwise.
  function processSSEData(data, streaming, streamSessionId) {
    if (data === '[DONE]') return true;
    var json;
    try { json = JSON.parse(data); } catch (e) { json = null; }
    if (!json) return true; // malformed non-approval chunk: ignore (existing behavior)
    if (json.object === 'n-agent.tool_approval') {
      var approval = json.approval;
      if (!isValidApprovalPayload(approval)) {
        // terminate stream with error, never render partial fields or guess allow
        if (streaming) {
          streaming.className = 'msg error';
          streaming.textContent = '[Error: invalid approval payload]';
        }
        return false;
      }
      renderToolApprovalCard(streaming, approval, streamSessionId);
      return true;
    }
    // OpenAI chunk: append text delta (existing behavior)
    var content = (json.choices && json.choices[0] && json.choices[0].delta && json.choices[0].delta.content) || '';
    if (content && streaming) streaming.textContent += content;
    return true;
  }

  // ui.task_result 吸收相邻 ui.task_artifact 后，在结果气泡内追加制品详情链接。
  // 复用 ui.task_artifact 气泡的详情链接样式（chat-artifact-link）与 SPA 导航逻辑。
  function appendResultArtifactLinks(el, artifacts) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-task-artifacts';
    let added = false;
    for (let i = 0; i < artifacts.length; i++) {
      const art = artifacts[i];
      if (!art) continue;
      const line = document.createElement('div');
      line.className = 'chat-task-artifact-line';
      const label = document.createElement('span');
      label.textContent = '产出制品: ' + (art.name || '');
      line.appendChild(label);
      // 详情链接仅在有 artifact_id 时追加（与独立 ui.task_artifact 气泡一致）
      if (art.artifact_id) {
        const href = '/artifacts/' + encodeURIComponent(art.artifact_id);
        const link = document.createElement('a');
        link.href = href;
        link.textContent = '详情';
        link.className = 'chat-artifact-link';
        link.addEventListener('click', (ev) => {
          ev.preventDefault();
          const nav = global.NAGENT && global.NAGENT.navigation;
          if (nav && typeof nav.navigatePath === 'function') {
            nav.navigatePath(href);
          } else { global.location.href = href; }
        });
        line.appendChild(document.createTextNode(' '));
        line.appendChild(link);
      }
      wrap.appendChild(line);
      added = true;
    }
    if (added) el.appendChild(wrap);
  }

  function createMessageElement(message, cardStates, taskDecisions, approvalDecisions) {
    if (message.is_summary) {
      const el = document.createElement('div');
      el.className = 'msg msg--summary';
      el.dataset.debugKind = 'compression';
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      const content = document.createElement('pre');
      summary.textContent = '对话压缩';
      const prefix = '[CONTEXT SUMMARY]: ';
      const raw = typeof message.content === 'string' ? message.content : String(message.content || '');
      content.textContent = raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
      details.append(summary, content);
      el.appendChild(details);
      return el;
    }
    const el = document.createElement('div');
    // 任务最终结果（ui.task_result）以普通消息渲染（区别于 ui.task_lifecycle/
    // ui.task_command 状态卡片），样式对齐 assistant 气泡、支持 Hover 时间与 markdown 子集。
    const isTaskResult = message.role === 'system' && message.name === 'ui.task_result';
    const isTaskArtifact = message.role === 'system' && message.name === 'ui.task_artifact';
    // T10: conversational artifact write-tool card (create/update/rollback/publish
    // success). Distinct from ui.task_artifact (task-worker artifacts). Renders
    // independently (classifyTaskMessage -> 'other'), never absorbed into groups.
    const isArtifact = message.role === 'system' && message.name === 'ui.artifact';
    const isProcessUser = message.role === 'user' && PROCESS_SOURCES.has(message.source);
    // 多消息合并卡片（groupTaskMessages 产出的 _mergedTaskStatus=true）：
    // summary 固定=任务状态，pre 多行拼接，open=false，className `msg system`（与 ui.task_lifecycle 一致）。
    const isMergedTaskStatus = !!message._mergedTaskStatus;
    // work task / judge task 单独卡片（1-message group，无 _mergedTaskStatus）：
    // summary 固定=任务状态（与合并卡/standalone lifecycle 一致），pre 放原始 content，open=false，className `msg system`。
    const isFoldedProcess = isProcessUser && isFoldableProcessContent(message.content) && !isMergedTaskStatus;
    // 进程来源 user 消息：右对齐状态卡片（灰底卡片样式，与真人 user 蓝底右对齐气泡同处右侧、靠灰底区分）。
    // work task / judge task / 合并卡片 样式打平 ui.task_lifecycle 任务状态卡片（msg system）；
    // 其余进程消息（schedule/curator 等）沿用 msg--process-card 非折叠样式。
    el.className = (isTaskResult || isTaskArtifact || isArtifact) ? 'msg assistant'
      : ((isMergedTaskStatus || isFoldedProcess) ? 'msg system'
      : (isProcessUser ? 'msg msg--process-card' : `msg ${message.role || 'assistant'}`));
    // 合并卡片 / work task / judge task 卡片样式打平 ui.task_lifecycle（不携带 dataset.source，与 system 消息一致）；
    // 其余进程消息（schedule/curator）保留 dataset.source 标识来源。
    if (isProcessUser && !isFoldedProcess && !isMergedTaskStatus) el.dataset.source = message.source;
    // ui.task_artifact: 制品产出通知，渲染为 assistant 气泡 + "详情"链接到 /artifacts/{id}
    if (isTaskArtifact) {
      const text = typeof message.content === 'string' ? message.content : String(message.content || '');
      const textNode = document.createElement('span');
      textNode.textContent = text;
      el.appendChild(textNode);
      const card = message.card;
      if (card && card.artifact_id) {
        const href = '/artifacts/' + encodeURIComponent(card.artifact_id);
        const link = document.createElement('a');
        link.href = href;
        link.textContent = '详情';
        link.className = 'chat-artifact-link';
        link.addEventListener('click', (ev) => {
          ev.preventDefault();
          const nav = global.NAGENT && global.NAGENT.navigation;
          if (nav && typeof nav.navigatePath === 'function') {
            nav.navigatePath(href);
          } else { global.location.href = href; }
        });
        el.appendChild(document.createTextNode(' '));
        el.appendChild(link);
      }
      const timeLabel = formatMessageTime(message.created_at);
      if (timeLabel) el.dataset.time = timeLabel;
      return el;
    }
    // T10: ui.artifact card -- name / version / publish-status badge / fixed
    // in-site 详情 link. The link target is built ONLY from structured card
    // metadata (artifact_id), never from a model-provided URL. All text uses
    // textContent; no innerHTML. Falls back to plain content text when the
    // card or artifact_id is missing.
    if (isArtifact) {
      const card = message.card;
      if (card && card.artifact_id) {
        const wrap = document.createElement('span');
        wrap.className = 'chat-artifact-card';
        const nameEl = document.createElement('span');
        nameEl.className = 'chat-artifact-card__name';
        nameEl.textContent = card.name || (typeof message.content === 'string' ? message.content : '');
        wrap.appendChild(nameEl);
        if (card.revision_number != null) {
          const verEl = document.createElement('span');
          verEl.className = 'chat-artifact-card__version';
          verEl.textContent = 'v' + card.revision_number;
          wrap.appendChild(verEl);
        }
        const state = typeof card.publish_sync_state === 'string' ? card.publish_sync_state : '';
        const badgeEl = document.createElement('span');
        badgeEl.className = 'chat-artifact-card__badge chat-artifact-card__badge--' + (state || 'unpublished');
        badgeEl.textContent = ARTIFACT_PUBLISH_LABELS[state] || state || '未发布';
        wrap.appendChild(badgeEl);
        el.appendChild(wrap);
        const href = '/artifacts/' + encodeURIComponent(card.artifact_id);
        const link = document.createElement('a');
        link.href = href;
        link.textContent = '详情';
        link.className = 'chat-artifact-link';
        link.addEventListener('click', (ev) => {
          ev.preventDefault();
          const nav = global.NAGENT && global.NAGENT.navigation;
          if (nav && typeof nav.navigatePath === 'function') {
            nav.navigatePath(href);
          } else { global.location.href = href; }
        });
        el.appendChild(document.createTextNode(' '));
        el.appendChild(link);
      } else {
        const text = typeof message.content === 'string' ? message.content : String(message.content || '');
        const textNode = document.createElement('span');
        textNode.textContent = text;
        el.appendChild(textNode);
      }
      const timeLabel = formatMessageTime(message.created_at);
      if (timeLabel) el.dataset.time = timeLabel;
      return el;
    }
    if (message.role === 'tool') {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      const content = document.createElement('pre');
      summary.textContent = '工具调用';
      appendToolDebugContent(content, message.content || '');
      details.append(summary, content);
      el.appendChild(details);
      const isScreenshot = isSuccessfulBrowserScreenshot(message.content);
      appendBrowserScreenshotPreview(el, message.content, message.created_at);
      // A successful screenshot is user-requested chat content, not debug
      // telemetry. Keep the enclosing card visible even when the per-session
      // "工具调试" switch is off; all other tool results retain that setting.
      if (!isScreenshot) el.dataset.debugKind = 'tool';
      return el;
    }
    // 合并卡片（_mergedTaskStatus=true）：summary 固定=任务状态，open=false，pre=多行 content。
    // 跨 role：first message 可能是 work task (role=user source=task) 或 lifecycle (role=system)，
    // 统一在此分支渲染为 msg system 折叠卡片，避免进入 system/isFoldedProcess 分支误判。
    if (isMergedTaskStatus) {
      const details = document.createElement('details');
      details.open = false;
      const summary = document.createElement('summary');
      summary.textContent = '任务状态';
      el.dataset.name = message.name || 'ui.task_lifecycle';
      el.dataset.debugKind = 'task';
      const content = document.createElement('pre');
      const rawText = typeof message.content === 'string' ? message.content : String(message.content || '');
      content.textContent = stripTaskMessageHeader(rawText);
      details.append(summary, content);
      el.appendChild(details);
      return el;
    }
    // system 消息：遵循消息渲染规范，按工具调用样式渲染为可折叠气泡（默认展开，
    // 保证命令结果可见、给人聊天感），textContent 安全渲染，无 innerHTML。
    // ui.task_result 不走此分支（普通消息，上方 isTaskResult 已分流到 assistant 样式）。
    if (message.role === 'system' && !isTaskResult && !isTaskArtifact) {
      // Server-persisted approval cards are reconstructed from session history
      // after a Dashboard refresh. The payload is the same whitelist used by
      // the SSE envelope, so it never contains raw tool arguments or actor data.
      const persistedApproval = validateToolApprovalCard(message.card);
      if (persistedApproval) {
        el.dataset.name = message.name || 'ui.tool_approval';
        const resolution = approvalDecisions ? approvalDecisions.get(persistedApproval.confirmation_id) : undefined;
        renderToolApprovalCard(el, persistedApproval, currentSessionId, resolution);
        return el;
      }
      // Valid card with resolved state -> inline interactive card (T6/T7).
      // cardStates is undefined when createMessageElement is called outside
      // renderSessionMessages (e.g. appendMessage); in that case fall back to details/pre.
      const validatedCard = validateTaskCard(message.card);
      if (validatedCard && cardStates) {
        const entry = cardStates.get(validatedCard.task_id);
        const state = computeCardState(validatedCard, entry);
        if (state) {
          const decision = taskDecisions ? taskDecisions.get(validatedCard.task_id) : null;
          return buildTaskCardElement(message, validatedCard, state, decision);
        }
      }
      const details = document.createElement('details');
      // 任务指令和等待用户交互的任务状态（如等待批准）默认展开；
      // 其余 system 消息默认折叠。合并卡片 content 多行拼接，命中任一等待行即展开。
      details.open = message.name === 'ui.task_command' || isTaskLifecycleAwaitingInteraction(message);
      const summary = document.createElement('summary');
      summary.textContent = message.name === 'ui.task_command' ? '任务指令'
        : message.name === 'ui.task_lifecycle' ? '任务状态' : '系统消息';
      el.dataset.name = message.name || '';
      if (message.name === 'ui.task_command' || message.name === 'ui.task_lifecycle') {
        el.dataset.debugKind = 'task';
      }
      const content = document.createElement('pre');
      const rawText = typeof message.content === 'string' ? message.content : String(message.content || '');
      content.textContent = stripTaskMessageHeader(rawText);
      details.append(summary, content);
      el.appendChild(details);
      return el;
    }
    // user/assistant/ui.task_result 消息：附加 Hover 时间（飞书风格），由 CSS ::before 展示
    const timeLabel = formatMessageTime(message.created_at);
    if (timeLabel) el.dataset.time = timeLabel;
    // work task / judge task 单独卡片（1-message group）：样式打平 ui.task_lifecycle 任务状态卡片，
    // 折叠卡片（details 默认 open=false），summary 固定=任务状态（与合并卡、standalone lifecycle 一致），pre = 原始 content；
    // 与 ui.task_command/ui.task_lifecycle 折叠卡片结构一致（details+summary+pre），
    // textContent 安全渲染，无 innerHTML。
    if (isFoldedProcess) {
      const details = document.createElement('details');
      details.open = false;
      const summary = document.createElement('summary');
      summary.textContent = '任务状态';
      const pre = document.createElement('pre');
      pre.textContent = typeof message.content === 'string' ? message.content : String(message.content || '');
      details.append(summary, pre);
      el.appendChild(details);
      el.dataset.debugKind = 'task';
      return el;
    }
    // 进程来源 user 消息（task/schedule/curator，非 work task/judge task 前缀）：
    // 原样渲染为非折叠右对齐状态卡片；多模态 list content 原样处理。
    const content = message.content;
    if (Array.isArray(content)) {
      let hasText = false;
      let hasImage = false;
      content.forEach((part) => {
        if (!part || typeof part !== 'object') return;
        if (part.type === 'text' && part.text) {
          hasText = true;
          el.appendChild(document.createTextNode(part.text));
        } else if (part.type === 'image_url' && part.image_url && part.image_url.url) {
          hasImage = true;
          const imgEl = document.createElement('img');
          imgEl.src = part.image_url.url;
          imgEl.alt = '';
          el.appendChild(imgEl);
        }
      });
      if (hasImage && !hasText) el.classList.add('msg--image-only');
      return el;
    }
    if (hasVisibleContent(content)) renderMessageText(el, content);
    // ui.task_result 吸收了相邻 ui.task_artifact 时，在结果气泡内追加制品详情链接
    if (isTaskResult && Array.isArray(message._resultArtifacts) && message._resultArtifacts.length) {
      appendResultArtifactLinks(el, message._resultArtifacts);
    }
    return el;
  }

  function shouldRenderMessage(message) {
    if (message && message.card && message.card.kind === 'tool_approval_resolution') return false;
    if (message.is_summary) return true;
    if (message.role === 'tool') return true;
    // 进程来源（task/curator）的 assistant 推理属 worker 内部思考过程，不在
    // Dashboard 对话框展示：worker 推理虽落库供 goal_mode 续轮与 LLM 上下文，
    // 但对用户不可见（task 经 ui.task_lifecycle/ui.task_result 卡片对外，curator
    // 为内部维护）。worker 的工具调用结果仍按工具调试卡片独立渲染；realtime
    // （api/dashboard/无 source）assistant 正常渲染。Regression: worker CoT
    // "The task requires querying weather..." 泄露为普通 assistant 气泡。
    // schedule 例外：其 assistant 消息是定时任务投递记录（无独立 ui.task_result
    // 卡片机制），必须在 Dashboard 对话框可见；空内容（仅 tool_calls 的中间步）
    // 由下方 hasVisibleContent 兜底隐藏。Regression: 定时任务投递记录被误隐藏。
    if (message.role === 'assistant' && PROCESS_SOURCES.has(message.source) && message.source !== 'schedule') return false;
    return hasVisibleContent(message.content);
  }

  function groupToolMessages(messages) {
    const grouped = [];
    messages.forEach((message) => {
      const previous = grouped[grouped.length - 1];
      if (message.role === 'tool' && previous && previous.role === 'tool') {
        previous.content.push(message.content || '');
        return;
      }
      if (message.role === 'tool') {
        grouped.push({ ...message, content: [message.content || ''] });
        return;
      }
      grouped.push(message);
    });
    return grouped;
  }

  function appendMessage(role, content, createdAt, name) {
    const stack = ui.byId('chat-message-stack');
    if (!stack) return null;
    const empty = stack.querySelector('.empty-hero');
    if (empty) clearNode(stack);
    const el = createMessageElement({ role, content, created_at: createdAt, name: name || null });
    stack.appendChild(el);
    scrollToBottom();
    return el;
  }

  function messageRenderKey(message, index) {
    // 分组合并后的消息保留首条消息 id；后续连续 tool/task 消息增加时仍更新同一节点。
    // 服务端消息均有 id，index 仅用于旧数据/异常响应的安全回退。
    return message && message.id ? String(message.id) : `message-${index}`;
  }

  function messageRenderFingerprint(message) {
    return JSON.stringify(message);
  }

  function preserveDetailsState(from, to) {
    const oldDetails = from.querySelector && from.querySelector('details');
    const newDetails = to.querySelector && to.querySelector('details');
    if (oldDetails && newDetails) newDetails.open = oldDetails.open;
  }

  // renderSessionMessages is async: it awaits resolveTaskCardStates (GET per task)
  // before writing any message DOM. partial=true 时只新增或替换变更节点；未变消息
  // 沿用既有 DOM，确保 details 展开状态和卡片中的用户输入不受轮询影响。
  async function renderSessionMessages(detail, options) {
    options = options || {};
    const stack = ui.byId('chat-message-stack');
    if (!stack) return;
    const myToken = ++renderToken;
    const allMessages = detail.messages || [];
    let visibleMessages = groupToolMessages(allMessages.filter(shouldRenderMessage));
    visibleMessages = groupTaskMessages(visibleMessages);
    const cardStates = await resolveTaskCardStates(visibleMessages);
    const taskDecisions = resolveTaskDecisions(visibleMessages);
    const approvalDecisions = resolveToolApprovalDecisions(allMessages);
    // Stale render guard: a newer render has incremented renderToken; abort without writing DOM.
    if (myToken !== renderToken) return;
    if (!options.partial) {
      clearNode(stack);
      renderedMessageNodes = new Map();
      renderedMessageFingerprints = new Map();
      if (!visibleMessages.length) { showEmptyState(); return; }
      visibleMessages.forEach((message, index) => {
        const key = messageRenderKey(message, index);
        const el = createMessageElement(message, cardStates, taskDecisions, approvalDecisions);
        el.dataset.messageKey = key;
        stack.appendChild(el);
        renderedMessageNodes.set(key, el);
        renderedMessageFingerprints.set(key, messageRenderFingerprint(message));
      });
      return;
    }

    const nextKeys = new Set();
    visibleMessages.forEach((message, index) => {
      const key = messageRenderKey(message, index);
      const fingerprint = messageRenderFingerprint(message);
      nextKeys.add(key);
      const existing = renderedMessageNodes.get(key);
      if (existing && renderedMessageFingerprints.get(key) === fingerprint) return;
      const el = createMessageElement(message, cardStates, taskDecisions, approvalDecisions);
      el.dataset.messageKey = key;
      if (existing && existing.parentNode === stack) {
        preserveDetailsState(existing, el);
        stack.replaceChild(el, existing);
      } else {
        stack.appendChild(el);
      }
      renderedMessageNodes.set(key, el);
      renderedMessageFingerprints.set(key, fingerprint);
    });
    // 正常消息历史是只追加的；删除会话消息等少数情况移除已不在权威快照中的节点。
    [...renderedMessageNodes.keys()].forEach((key) => {
      if (nextKeys.has(key)) return;
      const el = renderedMessageNodes.get(key);
      if (el && el.parentNode === stack) stack.removeChild(el);
      renderedMessageNodes.delete(key);
      renderedMessageFingerprints.delete(key);
    });
    if (!visibleMessages.length) showEmptyState();
  }

  async function loadSessions() {
    const list = ui.byId('chat-session-list');
    if (!list) return;
    try {
      const sessions = (await api.listSessions()).filter(isSessionVisible);
      clearNode(list);
      if (!sessions.length) { ui.renderEmpty(list, '暂无会话'); return; }
      sessions.forEach((session) => list.appendChild(buildSessionItem(session)));
    } catch (error) {
      clearNode(list);
      ui.renderError(list, '加载会话失败: ' + error.message);
    }
  }

  function defaultSessionSourceFilter() {
    return new Set(SESSION_SOURCE_OPTIONS.map(([value]) => value).filter((value) => value !== 'delegation'));
  }

  function loadSessionSourceFilter() {
    try {
      const raw = global.localStorage && global.localStorage.getItem(SESSION_SOURCE_FILTER_STORAGE_KEY);
      if (!raw) return defaultSessionSourceFilter();
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return defaultSessionSourceFilter();
      const selected = new Set(parsed.filter((value) => typeof value === 'string' && SESSION_SOURCE_VALUES.has(value)));
      return selected.size ? selected : defaultSessionSourceFilter();
    } catch (_) {
      return defaultSessionSourceFilter();
    }
  }

  function saveSessionSourceFilter(sources) {
    selectedSessionSources = new Set(sources);
    try {
      if (global.localStorage) {
        global.localStorage.setItem(SESSION_SOURCE_FILTER_STORAGE_KEY, JSON.stringify([...selectedSessionSources]));
      }
    } catch (_) {
      // Local storage is an optional Dashboard convenience; the in-memory filter still applies.
    }
  }

  function isSessionVisible(session) {
    return !!session && typeof session.source === 'string'
      && SESSION_SOURCE_VALUES.has(session.source) && selectedSessionSources.has(session.source);
  }

  function isCurrentSessionSearch(instance) {
    return !!instance && !instance.closed && currentSessionSearch === instance
      && !!instance.backdrop && instance.backdrop.isConnected;
  }

  function openSessionSearchModal() {
    const trigger = ui.byId('chat-session-search-btn');
    if (currentSessionSearch) currentSessionSearch.close({ restoreFocus: false });
    const leftover = document.getElementById('chat-session-search-modal');
    if (leftover) leftover.remove();

    const sourceSnapshot = new Set(selectedSessionSources);
    const backdrop = document.createElement('div');
    backdrop.id = 'chat-session-search-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog session-search-modal__dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'chat-session-search-title');
    const form = document.createElement('div');
    form.className = 'providers-form session-search-modal__form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.id = 'chat-session-search-title';
    title.textContent = '搜索会话';
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭搜索会话弹框');
    header.append(title, closeBtn);
    const input = document.createElement('input');
    input.id = 'chat-session-search-input';
    input.type = 'search';
    input.value = '';
    input.setAttribute('aria-label', '搜索会话');
    input.placeholder = '输入会话名称';
    const status = document.createElement('p');
    status.id = 'chat-session-search-status';
    status.setAttribute('aria-live', 'polite');
    status.setAttribute('aria-atomic', 'true');
    const results = document.createElement('ul');
    results.id = 'chat-session-search-results';
    results.setAttribute('aria-label', '搜索结果');
    form.append(header, input, status, results);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);

    const instance = { backdrop, dialog, input, status, results, records: [], closed: false, selected: false, onKeydown: null, close: null };
    const render = () => {
      if (!isCurrentSessionSearch(instance)) return;
      clearNode(results);
      const query = input.value.trim().toLowerCase();
      const matched = instance.records.filter((record) => !query || record.matchText.includes(query));
      if (!instance.records.length) { status.textContent = '暂无可见会话'; return; }
      if (!matched.length) { status.textContent = '未找到匹配的会话'; return; }
      status.textContent = `找到 ${matched.length} 个会话`;
      matched.forEach((record) => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'session-search-modal__result';
        button.textContent = record.name;
        button.addEventListener('click', () => {
          if (!isCurrentSessionSearch(instance) || instance.selected) return;
          instance.selected = true;
          button.disabled = true;
          instance.close({ restoreFocus: false });
          selectSession(record.id);
        });
        item.appendChild(button);
        results.appendChild(item);
      });
    };
    instance.close = (options) => {
      if (instance.closed) return;
      instance.closed = true;
      const wasCurrent = currentSessionSearch === instance;
      const focusedInside = !!(document.activeElement && dialog.contains(document.activeElement));
      if (wasCurrent) currentSessionSearch = null;
      document.removeEventListener('keydown', instance.onKeydown, true);
      backdrop.remove();
      if (wasCurrent && focusedInside && (!options || options.restoreFocus !== false) && trigger && trigger.isConnected) trigger.focus();
    };
    instance.onKeydown = (event) => {
      if (event.key !== 'Escape' || !isCurrentSessionSearch(instance) || !dialog.contains(document.activeElement)) return;
      event.preventDefault();
      event.stopPropagation();
      instance.close();
    };
    closeBtn.addEventListener('click', () => instance.close());
    backdrop.addEventListener('click', (event) => { if (event.target === backdrop) instance.close(); });
    input.addEventListener('input', render);
    input.addEventListener('search', render);
    currentSessionSearch = instance;
    document.body.appendChild(backdrop);
    document.addEventListener('keydown', instance.onKeydown, true);
    status.textContent = '正在加载会话';
    input.focus();

    if (!api || typeof api.listSessions !== 'function') {
      status.textContent = '加载会话失败';
      return;
    }
    Promise.resolve().then(() => api.listSessions()).then((sessions) => {
      if (!isCurrentSessionSearch(instance)) return;
      instance.records = (Array.isArray(sessions) ? sessions : []).filter((session) => (
        !!session && !Array.isArray(session) && typeof session.source === 'string'
        && SESSION_SOURCE_VALUES.has(session.source) && sourceSnapshot.has(session.source)
        && typeof session.id === 'string' && session.id.trim()
      )).map((session) => {
        const name = typeof session.title === 'string' && session.title.trim() ? session.title.trim() : session.id;
        return { id: session.id, name, matchText: name.trim().toLowerCase() };
      });
      render();
    }).catch(() => {
      if (!isCurrentSessionSearch(instance)) return;
      clearNode(results);
      status.textContent = '加载会话失败';
    });
  }

  function closeSessionSourceFilterModal(backdrop, onKeydown) {
    if (onKeydown) document.removeEventListener('keydown', onKeydown);
    if (backdrop && backdrop.parentNode) backdrop.remove();
  }

  function openSessionSourceFilterModal() {
    const existing = document.getElementById('chat-session-source-filter-modal');
    if (existing) existing.remove();
    const backdrop = document.createElement('div');
    backdrop.id = 'chat-session-source-filter-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'chat-session-source-filter-title');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.id = 'chat-session-source-filter-title';
    title.textContent = '筛选会话类型';
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭筛选会话类型弹框');
    header.append(title, closeBtn);
    form.appendChild(header);

    const options = ui.el('div', 'session-source-filter');
    const draftSources = new Set(selectedSessionSources);
    SESSION_SOURCE_OPTIONS.forEach(([value, label]) => {
      const option = ui.el('label', 'session-source-filter__option');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = value;
      checkbox.checked = draftSources.has(value);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) draftSources.add(value);
        else draftSources.delete(value);
      });
      const text = document.createElement('span');
      text.textContent = label;
      option.append(checkbox, text);
      options.appendChild(option);
    });
    form.appendChild(options);

    const actions = ui.el('div', 'providers-form__actions');
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn';
    cancelBtn.textContent = '取消';
    const applyBtn = document.createElement('button');
    applyBtn.type = 'submit';
    applyBtn.className = 'btn btn--primary';
    applyBtn.textContent = '应用';
    actions.append(cancelBtn, applyBtn);
    form.appendChild(actions);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);

    const onKeydown = (event) => {
      if (event.key === 'Escape') closeSessionSourceFilterModal(backdrop, onKeydown);
    };
    closeBtn.addEventListener('click', () => closeSessionSourceFilterModal(backdrop, onKeydown));
    cancelBtn.addEventListener('click', () => closeSessionSourceFilterModal(backdrop, onKeydown));
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeSessionSourceFilterModal(backdrop, onKeydown);
    });
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      saveSessionSourceFilter(draftSources);
      closeSessionSourceFilterModal(backdrop, onKeydown);
      loadSessions();
    });
    document.addEventListener('keydown', onKeydown);
    document.body.appendChild(backdrop);
    closeBtn.focus();
  }

  // 轻量标题刷新：不全量 re-render 会话列表（保留重命名输入态/避免闪烁），仅按 id 更新
  // 各 session-item 的标题文本。供 auto-refresh 周期性同步标题（尤其 New Session 经
  // ensure_title 生成实际标题后，会话列表自动展示最新命名）。
  let titleRefreshInFlight = false;
  let titleRefreshTickCounter = 0;
  const TITLE_REFRESH_EVERY_N_TICKS = 3;  // 每 3 个 auto-refresh tick 刷一次（~12s）

  async function refreshSessionListTitles() {
    if (titleRefreshInFlight) return;
    const list = ui.byId('chat-session-list');
    if (!list) return;
    titleRefreshInFlight = true;
    let sessions;
    try {
      sessions = await api.listSessions();
    } catch (e) {
      return;  // best-effort，静默失败
    } finally {
      titleRefreshInFlight = false;
    }
    const titleById = new Map();
    (sessions || []).filter(isSessionVisible).forEach((s) => titleById.set(s.id, s.title || s.id));
    const items = list.querySelectorAll('.session-item');
    items.forEach((item) => {
      const id = item.dataset && item.dataset.sessionId;
      if (!id) return;
      const newTitle = titleById.get(id);
      if (newTitle === undefined) return;
      const titleBtn = item.querySelector('.session-item__title');
      if (titleBtn && titleBtn.textContent !== newTitle) {
        titleBtn.textContent = newTitle;
      }
    });
  }

  function maybeRefreshSessionListTitles() {
    titleRefreshTickCounter++;
    if (titleRefreshTickCounter < TITLE_REFRESH_EVERY_N_TICKS) return;
    titleRefreshTickCounter = 0;
    refreshSessionListTitles();  // fire-and-forget
  }

  function buildSessionItem(session) {
    const item = document.createElement('div');
    item.className = `session-item${session.id === currentSessionId ? ' active' : ''}`;
    item.dataset.sessionId = session.id;

    const titleBtn = document.createElement('button');
    titleBtn.type = 'button';
    titleBtn.className = 'session-item__title';
    titleBtn.textContent = session.title || session.id;
    titleBtn.addEventListener('click', () => selectSession(session.id));

    const actions = document.createElement('span');
    actions.className = 'session-item__actions';

    const renameBtn = document.createElement('button');
    renameBtn.type = 'button';
    renameBtn.className = 'session-item__action';
    renameBtn.setAttribute('aria-label', '编辑会话标题');
    renameBtn.textContent = '✎';
    renameBtn.addEventListener('click', (event) => {
      event.stopPropagation();
      enterRenameMode(item, session);
    });

    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.className = 'session-item__action';
    deleteBtn.setAttribute('aria-label', '删除会话');
    deleteBtn.textContent = '🗑';
    deleteBtn.addEventListener('click', async (event) => {
      event.stopPropagation();
      await handleDelete(session);
    });

    actions.append(renameBtn, deleteBtn);
    item.append(titleBtn, actions);
    return item;
  }

  function enterRenameMode(item, session) {
    clearNode(item);
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'session-item__input';
    input.value = session.title || '';
    input.setAttribute('aria-label', '会话标题');
    item.appendChild(input);
    let committed = false;
    const commit = async () => {
      if (committed) return;
      committed = true;
      const next = input.value.trim();
      if (!next || next === session.title) {
        await loadSessions();
        return;
      }
      try {
        await api.renameSession(session.id, next);
      } catch (error) {
        setStatusMessage('重命名失败: ' + error.message, 'error');
      }
      await loadSessions();
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); commit(); }
      if (event.key === 'Escape') { committed = true; loadSessions(); }
    });
    input.addEventListener('blur', commit);
    input.focus();
    input.select();
  }

  async function handleDelete(session) {
    const label = session.title || session.id;
    if (!(await modal.confirm(`确定删除会话「${label}」？关联消息将一并删除`))) return;
    try {
      await api.deleteSession(session.id);
    } catch (error) {
      setStatusMessage('删除失败: ' + error.message, 'error');
      return;
    }
    // 清理被删会话的调试设置，避免 localStorage 残留孤儿记录
    if (sessionDebugSettings[session.id]) {
      delete sessionDebugSettings[session.id];
      saveDebugSettings();
    }
    if (currentSessionId === session.id) {
      stopAutoRefresh();
      renderedMessageVersion = null;
      currentSessionId = null;
      renderArtifactPanel();
      setHeader(null);
      showEmptyState();
      updateInfo({});
      // 当前会话已删：调试设置回落默认（draft 为空），刷新显隐与弹框勾选态
      applyDebugVisibility();
      renderSettingsUI();
    }
    await loadSessions();
  }

  async function selectSession(id) {
    stopAutoRefresh();
    // Session switch: disable outstanding approval cards from the previous
    // stream so they can't be clicked or re-POSTed after the stream is gone.
    if (activeStreamEl) {
      disableApprovalCards(activeStreamEl);
    }
    currentSessionId = id;
    renderArtifactPanel();
    draftExternalMemoryConfig = null;
    setHeader(id);
    // 会话切换：重置外部记忆操作标记，避免沿用上一会话的 options 注入
    externalMemoryTouched = false;
    // 会话切换：套用目标会话的调试设置（每会话独立），并刷新设置弹框勾选态
    draftDebugSettings = null;
    applyDebugVisibility();
    renderSettingsUI();
    try {
      const detail = await api.getSessionDetail(id);
      if (id !== currentSessionId) return; // 切换串台防护
      await applySessionDetail(detail, {
        preserveScroll: false, skipToolCalls: false, skipSessionList: false, applyExternalMemoryState: true,
      });
      scrollToBottom();
      renderedMessageVersion = messageVersionOf(detail);
      startAutoRefresh(id);
    } catch (error) {
      setStatusMessage('加载会话失败: ' + error.message, 'error');
    }
  }

  async function ensureSession() {
    if (currentSessionId) return currentSessionId;
    const id = 'dashboard-' + (crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(36).slice(2));
    await api.createSession(id);
    currentSessionId = id;
    setHeader(id);
    renderArtifactPanel();
    if (draftExternalMemoryConfig?.modified === true) {
      sessionExternalMemoryConfig[id] = {
        providers: draftExternalMemoryConfig.providers,
        modified: true,
        locked: false
      };
      draftExternalMemoryConfig = null;
      externalMemoryTouched = true;
    } else {
      // 新建会话：重置外部记忆操作标记，后续请求由后端按默认 profile 派生
      externalMemoryTouched = false;
    }
    // 空态勾选的调试设置转入新会话；未勾选则新会话用默认值（无映射记录）
    if (draftDebugSettings) {
      sessionDebugSettings[id] = draftDebugSettings;
      saveDebugSettings();
      draftDebugSettings = null;
    }
    applyDebugVisibility();
    renderSettingsUI();
    // Chat 会话默认关闭 builtin 记忆，外部记忆需用户手动勾选。
    renderExternalMemoryUI();
    showEmptyState();
    updateInfo({});
    await loadSessions();
    renderedMessageVersion = { count: 0, lastId: null };
    startAutoRefresh(id);
    return id;
  }

  // 会话是否承接过浏览器工具调用：任一 role=tool 且 name 以 browser_ 开头的消息。
  function sessionHasBrowserTool(detail) {
    const msgs = Array.isArray(detail && detail.messages) ? detail.messages : [];
    return msgs.some((m) => m && m.role === 'tool'
      && typeof m.name === 'string' && m.name.indexOf('browser_') === 0);
  }

  // 对话框标题：展示会话 ID，并按 links 后缀视图链接（浏览器视图/任务/定时任务）。
  // 每段链接形如 ` (文本)`，文本为指向对应详情页的 _blank 链接。
  function setHeader(id, links) {
    links = links || {};
    const header = ui.byId('chat-header');
    if (!header) return;
    clearNode(header);
    if (!id) { header.textContent = 'N-Agent Chat'; return; }
    header.appendChild(document.createTextNode(id));
    if (links.browser) {
      appendHeaderLink(header, '浏览器视图', '/browser/session?nagent=' + encodeURIComponent(id));
    }
    if (links.taskId) {
      appendHeaderLink(header, '任务', '/tasks/' + encodeURIComponent(links.taskId));
    }
    if (links.scheduledTaskId) {
      appendHeaderLink(header, '定时任务', '/scheduled-tasks/' + encodeURIComponent(links.scheduledTaskId));
    }
  }

  function appendHeaderLink(header, text, href) {
    header.appendChild(document.createTextNode(' ('));
    const link = document.createElement('a');
    link.textContent = text;
    link.target = '_blank';
    link.rel = 'noopener';
    link.className = 'chat-header-link';
    link.href = href;
    header.appendChild(link);
    header.appendChild(document.createTextNode(')'));
  }

  // 会话视图链接缓存：按 sessionId 存最新 taskId/scheduledTaskId 与任务关联消息序号。
  const sessionViewLinksCache = {};

  // 任务关联消息数：source==='task' 或 role=system 且 name 以 ui.task 开头。
  // 用于 4s 轮询时判定是否需要失效重拉任务链接。
  function countTaskAssocMessages(detail) {
    const msgs = Array.isArray(detail && detail.messages) ? detail.messages : [];
    let n = 0;
    for (const m of msgs) {
      if (!m) continue;
      if (m.source === 'task') { n++; continue; }
      if (m.role === 'system' && typeof m.name === 'string' && m.name.indexOf('ui.task') === 0) n++;
    }
    return n;
  }

  function buildHeaderLinks(detail, sessionId) {
    const cached = sessionId ? sessionViewLinksCache[sessionId] : null;
    return {
      browser: sessionHasBrowserTool(detail),
      taskId: cached ? cached.taskId : null,
      scheduledTaskId: cached ? cached.scheduledTaskId : null,
    };
  }

  // 按 created_at 倒序取首条；空 created_at 视为最旧；并列时按 id 倒序稳定。
  function latestByCreated(items) {
    return items.slice().sort((a, b) => {
      const ta = (a.created_at || '');
      const tb = (b.created_at || '');
      if (ta !== tb) return ta < tb ? 1 : -1;
      return (a.id || '') < (b.id || '') ? 1 : -1;
    })[0] || null;
  }

  // 拉取并缓存该会话关联的任务/定时任务最新 id；fire-and-forget，失败静默。
  // 拉取完成后若仍是当前会话则增量刷新标题。
  async function loadSessionViewLinks(sessionId, detail) {
    if (!sessionId) return;
    try {
      const [boardResp, scheduled] = await Promise.all([
        api.task.board(),
        api.listScheduledTasks(),
      ]);
      const columns = (boardResp && boardResp.columns) || [];
      const cards = [];
      for (const col of columns) {
        for (const c of (col.cards || [])) {
          if (c && (c.origin_session_id === sessionId || c.execution_session_id === sessionId)) cards.push(c);
        }
      }
      const latestTask = latestByCreated(cards);
      const scheds = (Array.isArray(scheduled) ? scheduled : []).filter((s) => s && s.session_id === sessionId);
      const latestSched = latestByCreated(scheds);
      sessionViewLinksCache[sessionId] = {
        taskId: latestTask ? latestTask.id : null,
        scheduledTaskId: latestSched ? latestSched.id : null,
        taskMsgSeq: countTaskAssocMessages(detail),
      };
      if (sessionId === currentSessionId) {
        setHeader(sessionId, buildHeaderLinks(detail, sessionId));
      }
    } catch (e) {
      // 静默降级：拉取失败不展示对应链接，不影响对话与其他链接。
    }
  }

  function updateInfo(detail, options) {
    options = options || {};
    const summary = ui.byId('chat-summary');
    const taskState = ui.byId('chat-task-state');
    const toolCalls = ui.byId('chat-tool-calls');
    if (!summary || !taskState || !toolCalls) return;
    if (!currentSessionId) {
      summary.textContent = '暂未选择会话';
      taskState.textContent = '暂未选择会话';
      toolCalls.textContent = '暂未选择会话';
      return;
    }
    summary.textContent = detail.summary ? detail.summary.summary : '暂无摘要';
    clearNode(taskState);
    const ts = detail.task_state;
    if (ts) {
      const badge = document.createElement('span');
      badge.className = `status-badge ${ts.status}`;
      badge.textContent = ts.status;
      const meta = document.createElement('span');
      meta.textContent = ` iter=${ts.iteration_count}${ts.last_error ? ' err=' + ts.last_error : ''}`;
      taskState.append(badge, meta);
    } else {
      taskState.textContent = '暂无任务状态';
    }
    if (!options.skipToolCalls) loadToolCalls();
  }

  function isAtScrollBottom() {
    const el = ui.byId('chat-messages');
    if (!el) return true;
    return el.scrollHeight - (el.scrollTop + el.clientHeight) <= SCROLL_BOTTOM_THRESHOLD_PX;
  }

  function restoreScroll(wasAtBottom, prevScrollTop) {
    const el = ui.byId('chat-messages');
    if (!el) return;
    if (wasAtBottom) { scrollToBottom(); return; }
    const maxTop = Math.max(0, el.scrollHeight - el.clientHeight);
    el.scrollTop = Math.min(prevScrollTop != null ? prevScrollTop : el.scrollTop, maxTop);
  }

  // 已通过响应归属校验后的共享渲染入口。
  // preserveScroll=true（轮询）：记录滚动状态，渲染后恢复（底部跟随/上翻保持）。
  // preserveScroll=false（selectSession/refreshCurrentSession）：不滚动，由调用方自行 scrollToBottom 或保持原位。
  // skipToolCalls/skipSessionList=true（轮询）：跳过工具调用列表与会话列表刷新。
  // applyExternalMemoryState=false（轮询）：不周期覆盖用户正在操作的外部记忆选择。
  async function applySessionDetail(detail, options) {
    options = options || {};
    const el = ui.byId('chat-messages');
    const wasAtBottom = options.preserveScroll ? isAtScrollBottom() : false;
    const prevScrollTop = el ? el.scrollTop : 0;
    if (options.applyExternalMemoryState) {
      applySessionExternalMemoryState(detail);
      renderExternalMemoryUI();
    }
    // Await state resolution + render before restoring scroll / updating info,
    // so clickable actions are not inserted mid-render.
    await renderSessionMessages(detail, { partial: options.partialMessages === true });
    // 会话视图链接：浏览器视图实时判定；任务/定时任务用缓存，任务关联消息数变化时失效重拉。
    if (currentSessionId) {
      const cached = sessionViewLinksCache[currentSessionId];
      const seq = countTaskAssocMessages(detail);
      const invalidate = !cached || cached.taskMsgSeq !== seq;
      setHeader(currentSessionId, buildHeaderLinks(detail, currentSessionId));
      if (invalidate) loadSessionViewLinks(currentSessionId, detail);
    }
    updateInfo(detail, { skipToolCalls: options.skipToolCalls });
    if (options.preserveScroll) restoreScroll(wasAtBottom, prevScrollTop);
    if (!options.skipSessionList) await loadSessions();
  }

  async function loadToolCalls() {
    const target = ui.byId('chat-tool-calls');
    if (!target) return;
    if (!currentSessionId) { target.textContent = '暂未选择会话'; return; }
    try {
      const calls = await api.getSessionToolCalls(currentSessionId);
      clearNode(target);
      if (!calls.length) { target.textContent = '暂无工具调用记录'; return; }
      calls.forEach((call) => {
        const el = document.createElement('div');
        el.className = 'tool-call';
        appendText(el, `${call.tool_name} → ${call.status} (${call.duration_ms}ms)\n${formatDebugJson(call.arguments)}`);
        target.appendChild(el);
      });
    } catch (error) {
      target.textContent = '加载工具调用失败: ' + error.message;
    }
  }

  function handleComposerKeydown(event) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }

  function handlePaste(event) {
    const items = (event.clipboardData && event.clipboardData.items) || [];
    for (const item of items) {
      if (item.kind === 'file' && item.type && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) addPendingImage(file);
      }
    }
  }

  function handleFileSelect(event) {
    const files = event.target.files || [];
    for (const file of files) {
      if (file.type && file.type.startsWith('image/')) addPendingImage(file);
    }
    event.target.value = '';
  }

  function addPendingImage(file) {
    const reader = new FileReader();
    reader.onload = (e) => {
      pendingImages.push({ dataUrl: e.target.result, name: file.name });
      renderImagePreviews();
    };
    reader.readAsDataURL(file);
  }

  function renderImagePreviews() {
    const container = ui.byId('chat-image-previews');
    if (!container) return;
    clearNode(container);
    pendingImages.forEach((img, index) => {
      const wrapper = document.createElement('div');
      wrapper.className = 'chat-image-preview';
      const thumb = document.createElement('img');
      thumb.src = img.dataUrl;
      thumb.alt = img.name || '';
      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'chat-image-preview__remove';
      removeBtn.textContent = '×';
      removeBtn.setAttribute('aria-label', '移除图片');
      removeBtn.addEventListener('click', () => {
        pendingImages.splice(index, 1);
        renderImagePreviews();
      });
      wrapper.append(thumb, removeBtn);
      container.appendChild(wrapper);
    });
  }

  function buildChatRequestBody(text, images) {
    const outgoingImages = images || pendingImages;
    let content;
    if (outgoingImages.length > 0) {
      content = [];
      if (text) content.push({ type: 'text', text });
      for (const img of outgoingImages) {
        content.push({ type: 'image_url', image_url: { url: img.dataUrl } });
      }
    } else {
      content = text;
    }
    const body = {
      model: defaultModel || 'model',
      messages: [{ role: 'user', content }],
      stream: true,
    };
    // 仅当用户在本会话中操作过外部记忆勾选时才携带 options.external_memory_enabled；
    // 未操作时不发送该字段，由后端按会话默认 profile 派生（builtin 默认关闭）。
    if (externalMemoryTouched) {
      body.options = { external_memory_enabled: getExternalMemoryEnabled() };
    }
    return body;
  }

  async function refreshCurrentSession() {
    if (!currentSessionId) return;
    const sessionId = currentSessionId;
    const seq = ++autoRefreshSeq;
    try {
      const detail = await api.getSessionDetail(sessionId);
      if (sessionId !== currentSessionId || seq !== autoRefreshSeq) return; // 归属校验
      await applySessionDetail(detail, {
        preserveScroll: false, skipToolCalls: false, skipSessionList: false, applyExternalMemoryState: true,
      });
      renderedMessageVersion = messageVersionOf(detail);
      if (sessionId === currentSessionId && seq === autoRefreshSeq) renderArtifactPanel();
    } catch (error) {
      const summary = ui.byId('chat-summary');
      if (summary) summary.textContent = '刷新会话失败: ' + error.message;
    }
  }

  // /task slash 命令统一用法与错误码映射。错误码来自 task_routes 的 _task_error_response
  // （fetchJson 失败时 throw new Error(code)），映射为可读说明并保留原码。
  const TASK_USAGE = '用法：/task create <title> [--body <text>] [--priority <n>] [--goal] | list | approve|reject <id> [--note <text>] | cancel|retry <id>';
  const TASK_ERROR_MAP = {
    task_not_found: '任务不存在',
    task_state_invalid: '任务状态不允许该操作',
    task_invalid: '任务参数无效',
    task_conflict: '任务状态冲突',
    task_claim_failed: '任务抢占失败',
    task_internal_error: '任务服务内部错误',
  };

  // 命令结果统一以 system 消息呈现（spec UI Design 要求，正文不带抬头）。
  // 正文 UTF-8 字节安全截断（DOM 与 POST 同一字符串）；仅当 session 仍为当前会话时
  // 追加本地 system 气泡；无论会话是否切换都向捕获 session best-effort 持久化。
  const TASK_MESSAGE_MAX_BYTES = 65536;
  const TASK_MESSAGE_TRUNCATE_SUFFIX = '…[内容已截断]';

  function truncateTaskMessageUtf8(text) {
    const data = new TextEncoder().encode(text);
    if (data.length <= TASK_MESSAGE_MAX_BYTES) return text;
    const suffixBytes = new TextEncoder().encode(TASK_MESSAGE_TRUNCATE_SUFFIX);
    const budget = TASK_MESSAGE_MAX_BYTES - suffixBytes.length;
    if (budget <= 0) return TASK_MESSAGE_TRUNCATE_SUFFIX;
    // 按 code point 累加字节，超 budget 前停止（避免 TextDecoder 默认 U+FFFD 替换导致
    // 重编码超限；与 Python 侧 errors="ignore" 行为一致）。
    const chars = Array.from(text);
    let out = '';
    let outBytes = 0;
    for (let i = 0; i < chars.length; i++) {
      const chBytes = new TextEncoder().encode(chars[i]);
      if (outBytes + chBytes.length > budget) break;
      out += chars[i];
      outBytes += chBytes.length;
    }
    return (out + TASK_MESSAGE_TRUNCATE_SUFFIX).trim();
  }

  async function persistTaskSystemMessage(sessionId, content) {
    try {
      return await api.appendSessionMessage(sessionId, content);
    } catch (e) {
      console.warn('persist task message failed', e);
      return null;
    }
  }

  async function taskSystemMessage(sessionId, message) {
    const body = truncateTaskMessageUtf8(message);
    // Keep the optimistic task-command card addressable by the server message
    // id once persistence succeeds. A poll may complete between the optimistic
    // DOM update and advanceVersionAfterPersistedAppend(); without this bridge,
    // partial rendering cannot find the local node and appends the authoritative
    // copy as a second, identical card. A full page reload naturally removes
    // that local-only copy, which is why the former symptom disappeared on
    // refresh.
    const optimisticEl = currentSessionId === sessionId ? appendOrMergeTaskCommand(body) : null;
    const preVersion = renderedMessageVersion;
    const persisted = await persistTaskSystemMessage(sessionId, body);
    if (optimisticEl && persisted && persisted.id) {
      reconcileOptimisticTaskCommand(optimisticEl, persisted.id);
    }
    // 持久化成功且返回真实 id 时推进版本，避免下一次轮询误判变更触发无意义重渲。
    // 期间版本若被权威详情改变（并发追加/切换），跳过以权威为准。
    if (persisted && persisted.id) advanceVersionAfterPersistedAppend(persisted.id, preVersion);
  }

  // 相邻任务指令合并：/task 命令记录（"执行命令: ..."）与其回执（结果/错误）渲染为同一条
  // 任务指令气泡。命令记录开新气泡，回执追加到上一条任务指令气泡的 <pre>。
  // 兼容历史消息：记录历史曾带 [任务指令] 抬头，先剥离抬头再判定。
  const TASK_CMD_EXEC_PREFIX = '执行命令: ';
  const TASK_LEGACY_HEADER = '[任务指令] ';

  function isTaskCommandRecord(content) {
    if (typeof content !== 'string') return false;
    const c = content.startsWith(TASK_LEGACY_HEADER) ? content.slice(TASK_LEGACY_HEADER.length) : content;
    return c.startsWith(TASK_CMD_EXEC_PREFIX);
  }

  function _lastMessageEl(stack) {
    if (stack.lastElementChild) return stack.lastElementChild;
    const kids = stack._kids || [];
    return kids.length ? kids[kids.length - 1] : null;
  }

  function _detailsPre(el) {
    if (el.querySelector) {
      const pre = el.querySelector('pre');
      if (pre) return pre;
    }
    const details = el._kids && el._kids[0];
    const pre = details && details._kids && details._kids[1];
    return pre || null;
  }

  function appendOrMergeTaskCommand(body) {
    // 回执（非"执行命令:"）追加到上一条任务指令气泡；命令记录开新气泡
    if (!isTaskCommandRecord(body)) {
      const stack = ui.byId('chat-message-stack');
      if (stack) {
        const last = _lastMessageEl(stack);
        if (last && last.dataset && last.dataset.name === 'ui.task_command') {
          const pre = _detailsPre(last);
          if (pre) {
            pre.textContent = (pre.textContent || '') + '\n' + body;
            return last;
          }
        }
      }
    }
    return appendMessage('system', body, undefined, 'ui.task_command');
  }

  function reconcileOptimisticTaskCommand(el, messageId) {
    // Consecutive command record/result messages render as one group keyed by
    // the first server message. Preserve that first key when the result is
    // persisted, so the next partial render replaces this node instead of
    // appending another card.
    if (!el || !messageId || !el.dataset) return;
    if (el.dataset.messageKey) return;
    const key = String(messageId);
    el.dataset.messageKey = key;
    renderedMessageNodes.set(key, el);
    // Deliberately omit a fingerprint: the next authoritative snapshot must
    // replace the optimistic content with its canonical grouped content.
    renderedMessageFingerprints.delete(key);
  }

  // 任务消息类型分类（按"类型"分组，同类型相邻合并，不同类型不合并/断开）：
  //   'command'      任务指令：ui.task_command（相邻的多条合并，summary=任务指令，content 多行原文）
  //   'task_status'  任务消息：ui.task_lifecycle 无 card + work task + judge task
  //                  （相邻的多条合并，summary=任务状态，content 多行：lifecycle 原文 /
  //                   work task `查询状态: ...` / judge task `判断结束: ...`）
  //   'card'         交互卡片：ui.task_lifecycle 带 card payload（独立，不参与任何合并）
  //   'other'        非 task 消息（assistant / user dashboard / schedule / curator 等）断开合并链
  // 关键：
  //   - 任务指令 vs 任务消息：不同类型，相邻不合并（断开）
  //   - 同类型相邻合并：2 条相连 ui.task_command 合并；相连的 lifecycle+work+judge 合并
  //   - 跨 role：work/judge role=user + lifecycle role=system 同属任务消息，可合并
  function classifyTaskMessage(msg) {
    if (!msg) return 'other';
    // 交互卡片（ui.task_lifecycle 带 card payload）：独立，断开合并链
    if (msg.role === 'system' && msg.name === 'ui.task_lifecycle' && validateTaskCard(msg.card)) {
      return 'card';
    }
    // 任务指令（ui.task_command）
    if (msg.role === 'system' && msg.name === 'ui.task_command') {
      return 'command';
    }
    // 任务消息（ui.task_lifecycle 无 card + work task + judge task）
    const isLifecycleNoCard = msg.role === 'system' && msg.name === 'ui.task_lifecycle';
    const isWorkOrJudgeTask = msg.role === 'user'
      && PROCESS_SOURCES.has(msg.source)
      && isFoldableProcessContent(msg.content);
    if (isLifecycleNoCard || isWorkOrJudgeTask) {
      return 'task_status';
    }
    // 任务最终结果（ui.task_result）：独立 1 消息组，可吸收后续相邻 ui.task_artifact
    if (msg.role === 'system' && msg.name === 'ui.task_result') {
      return 'task_result';
    }
    // 制品产出通知（ui.task_artifact）：被前导 task_result 组吸收，否则独立
    if (msg.role === 'system' && msg.name === 'ui.task_artifact') {
      return 'task_artifact';
    }
    return 'other';
  }

  function groupTaskMessages(messages) {
    // 合并规则（按"类型"分组，同类型相邻合并，不同类型不合并/断开）：
    // - 类型 1 任务指令（ui.task_command）：相邻的多条合并为一组（summary=任务指令，content 多行原文）
    // - 类型 2 任务消息（ui.task_lifecycle 无 card + work task + judge task）：相邻的多条合并为一组
    //   （summary=任务状态，content 多行：lifecycle 原文 / work task `查询状态: ...` / judge task `判断结束: ...`）
    //   跨 role（work/judge role=user + lifecycle role=system）同属任务消息，可合并
    // - 类型 3 交互卡片（ui.task_lifecycle 带 card payload）：独立，不参与任何合并
    // - 其他：非 task 消息（assistant / user dashboard / schedule / curator 等）断开合并链
    // - 不同类型相邻不合并（断开）：任务指令 vs 任务消息 不合并；任务指令/任务消息 vs 交互卡片 不合并
    // - 1-message group（无相邻同类型）：保持原 role/name，按各自规则渲染
    //   （单独 work/judge task -> summary=前缀+content；单独 ui.task_lifecycle 无 card -> summary=任务状态；
    //    单独 ui.task_command -> summary=任务指令）
    // - Multi-message merged card：
    //   * 任务指令组：name=ui.task_command，content 多行原文拼接（无前缀），走 system 分支渲染（summary=任务指令）
    //   * 任务消息组：_mergedTaskStatus=true，content 多行（lifecycle 原文 + work `查询状态: ...` + judge `判断结束: ...`），
    //     走 isMergedTaskStatus 分支渲染（summary=任务状态、open=false）
    const result = [];
    let currentGroup = null;
    let currentGroupType = null;
    let currentGroupSources = null;
    for (const msg of messages) {
      const type = classifyTaskMessage(msg);
      if (type === 'other' || type === 'card') {
        // 非合并对象 / 交互卡片：独立，断开合并链
        currentGroup = null;
        currentGroupType = null;
        currentGroupSources = null;
        result.push(msg);
        continue;
      }
      // ui.task_artifact：前导 task_result 组存在时吸收（收集制品引用，供结果气泡内渲染详情链接），
      // 否则独立渲染（保持原有 ui.task_artifact 气泡 + 详情链接）
      if (type === 'task_artifact') {
        if (currentGroupType === 'task_result' && currentGroup) {
          const card = msg.card || {};
          if (!currentGroup._resultArtifacts) currentGroup._resultArtifacts = [];
          currentGroup._resultArtifacts.push({
            name: card.name || (typeof msg.content === 'string' ? msg.content : ''),
            artifact_id: card.artifact_id || '',
          });
          continue;
        }
        currentGroup = null;
        currentGroupType = null;
        currentGroupSources = null;
        result.push(msg);
        continue;
      }
      // ui.task_result：独立 1 消息组（多个 task_result 不互相合并），可吸收后续相邻 task_artifact
      if (type === 'task_result') {
        currentGroup = { ...msg };
        currentGroupSources = [msg];
        currentGroupType = type;
        result.push(currentGroup);
        continue;
      }
      // type === 'command' or 'task_status'
      if (currentGroupType !== type) {
        // 不同类型：断开合并链，开新组
        currentGroup = null;
        currentGroupType = null;
        currentGroupSources = null;
      }
      if (currentGroup === null) {
        // 开新 1-message 组：保留原始 content，不加合并标志
        currentGroup = { ...msg };
        currentGroupSources = [msg];
        currentGroupType = type;
        result.push(currentGroup);
      } else {
        // 同类型相邻：追加到现有组 -> 多消息合并卡片
        currentGroupSources.push(msg);
        if (type === 'command') {
          // 任务指令组：content 多行原文拼接（无前缀）
          if (!currentGroup._mergedTaskCommand) {
            currentGroup.content = currentGroupSources
              .map((m) => String(m.content !== undefined ? m.content : ''))
              .join('\n');
            currentGroup._mergedTaskCommand = true;
          } else {
            currentGroup.content = String(currentGroup.content) + '\n'
              + String(msg.content !== undefined ? msg.content : '');
          }
        } else {
          // 任务消息组：lifecycle 原文 + work `查询状态: ...` + judge `判断结束: ...`
          if (!currentGroup._mergedTaskStatus) {
            currentGroup.content = currentGroupSources.map(taskStatusLineForMessage).join('\n');
            currentGroup._mergedTaskStatus = true;
          } else {
            currentGroup.content = String(currentGroup.content) + '\n' + taskStatusLineForMessage(msg);
          }
        }
      }
    }
    return result;
  }

  function describeTaskError(e) {
    const code = (e && e.message) ? String(e.message) : String(e || '');
    const desc = TASK_ERROR_MAP[code];
    if (desc) return desc + '（' + code + '）';
    return '任务指令失败：' + (code || '未知错误');
  }

  // Parse a "/task ..." slash command into {subcommand, title, id, body,
  // priority, goal, note} or {error}. Pure function (no DOM/api) for testing.
  // 引号感知分词（spec 要求 title "支持引号"）；按子命令校验位置/命名参数，坏值返回 {error}。
  function parseTaskCommand(text) {
    const rest = text.slice('/task'.length).trim();
    if (!rest) return {error: TASK_USAGE};
    const tokens = [];
    let cur = '';
    let quote = null;
    for (let i = 0; i < rest.length; i++) {
      const ch = rest[i];
      if (quote) {
        if (ch === quote) { quote = null; } else { cur += ch; }
      } else if (ch === '"' || ch === "'") {
        quote = ch;
      } else if (ch === ' ' || ch === '\t') {
        if (cur.length) { tokens.push(cur); cur = ''; }
      } else {
        cur += ch;
      }
    }
    if (quote) return {error: '未闭合的引号。' + TASK_USAGE};
    if (cur.length) tokens.push(cur);

    const subcommand = tokens[0];
    const valid = ['create', 'list', 'approve', 'reject', 'cancel', 'retry'];
    if (valid.indexOf(subcommand) === -1) {
      return {error: '未知子命令 ' + subcommand + '。' + TASK_USAGE};
    }
    const allowedNamed = {
      create: {body: true, priority: true, goal: true},
      list: {},
      approve: {note: true},
      reject: {note: true},
      cancel: {},
      retry: {},
    }[subcommand];

    const result = {subcommand: subcommand};
    const positional = [];
    let i = 1;
    while (i < tokens.length) {
      const t = tokens[i];
      if (t === '--body' || t === '--note') {
        const key = t.slice(2);
        if (!allowedNamed[key]) return {error: t + ' 不适用于 ' + subcommand + '。' + TASK_USAGE};
        i++;
        const parts = [];
        while (i < tokens.length && !tokens[i].startsWith('--')) { parts.push(tokens[i]); i++; }
        if (!parts.length) return {error: t + ' 需要值。' + TASK_USAGE};
        result[key] = parts.join(' ');
      } else if (t === '--priority') {
        if (!allowedNamed.priority) return {error: '--priority 不适用于 ' + subcommand + '。' + TASK_USAGE};
        i++;
        if (i >= tokens.length) return {error: '--priority 需要值。' + TASK_USAGE};
        const raw = tokens[i];
        if (!/^-?\d+$/.test(raw)) return {error: '--priority 需要整数。' + TASK_USAGE};
        result.priority = Number(raw);
        i++;
      } else if (t === '--goal') {
        if (!allowedNamed.goal) return {error: '--goal 不适用于 ' + subcommand + '。' + TASK_USAGE};
        result.goal = true;
        i++;
      } else if (t.startsWith('--')) {
        return {error: '未知参数 ' + t + '。' + TASK_USAGE};
      } else {
        positional.push(t);
        i++;
      }
    }
    if (subcommand === 'create') {
      result.title = positional.join(' ').trim();
      if (!result.title) return {error: '/task create 需要标题。' + TASK_USAGE};
    } else if (subcommand === 'list') {
      if (positional.length) return {error: '/task list 不接受额外参数。' + TASK_USAGE};
    } else {
      if (!positional.length) return {error: '/task ' + subcommand + ' 需要 task id。' + TASK_USAGE};
      if (positional.length > 1) return {error: '/task ' + subcommand + ' 不接受多个 id。' + TASK_USAGE};
      result.id = positional[0];
    }
    return result;
  }

  // Execute a parsed /task command via api.task.*, appending a system
  // message with the result. create binds to currentSessionId (origin_session_id).
  async function runTaskCommand(text, sessionId) {
    await taskSystemMessage(sessionId, '执行命令: ' + text);
    const parsed = parseTaskCommand(text);
    if (parsed.error) {
      await taskSystemMessage(sessionId, parsed.error);
      return;
    }
    let msg = '';
    try {
      if (parsed.subcommand === 'create') {
        const payload = {title: parsed.title, origin_session_id: sessionId};
        if (parsed.body) payload.body = parsed.body;
        if (parsed.priority != null) payload.priority = parsed.priority;
        if (parsed.goal) payload.goal_mode = true;
        const task = await api.task.create(payload);
        msg = '已创建任务 ' + ((task && task.id) || '') + '：' + ((task && task.title) || parsed.title);
      } else if (parsed.subcommand === 'list') {
        const page = await api.task.list();
        const items = ((page && page.items) || []).filter((t) => t.origin_session_id === sessionId);
        if (!items.length) msg = '当前会话无关联任务';
        else msg = '当前会话任务（' + items.length + '）：\n' + items.map((t) => '- ' + t.id + ' [' + (t.status || '') + '] ' + (t.title || '')).join('\n');
      } else if (parsed.subcommand === 'approve') {
        await api.task.approve(parsed.id, parsed.note);
        msg = '已批准任务 ' + parsed.id;
      } else if (parsed.subcommand === 'reject') {
        await api.task.reject(parsed.id, parsed.note);
        msg = '已拒绝任务 ' + parsed.id;
      } else if (parsed.subcommand === 'cancel') {
        await api.task.cancel(parsed.id);
        msg = '已取消任务 ' + parsed.id;
      } else if (parsed.subcommand === 'retry') {
        await api.task.retry(parsed.id);
        msg = '已重试任务 ' + parsed.id;
      }
      await taskSystemMessage(sessionId, msg);
    } catch (e) {
      await taskSystemMessage(sessionId, describeTaskError(e));
    }
  }

  async function send() {
    if (isSending) return;
    const input = ui.byId('chat-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text && !pendingImages.length) return;
    if (text.startsWith('/task')) {
      setSending(true);
      try {
        await ensureSession();
        const commandSessionId = currentSessionId;
        if (!commandSessionId) { input.focus(); return; }
        input.value = '';
        await runTaskCommand(text, commandSessionId);
      } catch (e) {
        // ensureSession 失败尚无可靠 session id，仅显示本地错误、不调持久化端点
        appendMessage('system', '会话创建失败：' + ((e && e.message) || e), undefined, 'ui.task_command');
      } finally {
        setSending(false);
        input.focus();
      }
      return;
    }
    await ensureSession();
    input.value = '';
    const sentImages = pendingImages.slice();
    pendingImages = [];
    renderImagePreviews();
    const userContent = sentImages.length > 0
      ? [
          ...(text ? [{ type: 'text', text }] : []),
          ...sentImages.map((img) => ({ type: 'image_url', image_url: { url: img.dataUrl } })),
        ]
      : text;
    appendMessage('user', userContent, new Date().toISOString());
    const streaming = appendMessage('assistant', '');
    setSending(true);
    // Capture the session ID at stream creation time. The approval card POST
    // must use THIS id, not a subsequently-switched global currentSessionId.
    const streamSessionId = currentSessionId;
    activeStreamEl = streaming;
    var reader = null;
    try {
      const res = await fetch('/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Session-ID': streamSessionId,
        },
        body: JSON.stringify(buildChatRequestBody(text, sentImages)),
      });
      if (!res.ok) {
        let errMsg = 'HTTP ' + res.status;
        try {
          const errJson = await res.json();
          if (errJson && errJson.error && errJson.error.message) errMsg = errJson.error.message;
        } catch (parseErr) { /* ignore */ }
        if (streaming) {
          streaming.className = 'msg error';
          streaming.textContent = '[Error: ' + errMsg + ']';
        }
        return;
      }
      reader = res.body.getReader();
      const decoder = new TextDecoder('utf-8', { stream: true });
      const parser = createSSEParser();
      let streamAlive = true;
      while (streamAlive) {
        const { done, value } = await reader.read();
        if (done) break;
        const text = decoder.decode(value, { stream: true });
        const events = parser.feed(text);
        for (let ei = 0; ei < events.length; ei++) {
          if (!streamAlive) break;
          if (!processSSEData(events[ei], streaming, streamSessionId)) {
            streamAlive = false;
            break;
          }
        }
        scrollToBottom();
      }
      // Flush decoder (finishes any pending multi-byte sequence) and feed any
      // trailing text to the parser before flushing the parser itself.
      const tail = decoder.decode();
      if (tail) parser.feed(tail);
      // Flush parser (trailing event without blank-line terminator)
      if (streamAlive) {
        const trailing = parser.flush();
        for (let ti = 0; ti < trailing.length; ti++) {
          if (!processSSEData(trailing[ti], streaming, streamSessionId)) break;
        }
      }
    } catch (error) {
      if (streaming) {
        streaming.className = 'msg error';
        streaming.textContent = '[Error: ' + error.message + ']';
      }
    } finally {
      // Release the underlying ReadableStream promptly (e.g. when the loop
      // breaks early on invalid approval), instead of waiting for GC.
      if (reader && typeof reader.cancel === 'function') {
        reader.cancel().catch(function () {});
      }
      // Stream end/failure: disable outstanding approval cards (no late POST)
      disableApprovalCards(streaming);
      activeStreamEl = null;
      setSending(false);
      await refreshCurrentSession();
      input.focus();
    }
  }

  function syncSideCollapse() {
    const shell = ui.byId('chat-shell');
    const btn = ui.byId('chat-side-toggle-btn');
    if (!shell || !btn) return;
    const collapsed = shell.classList.contains('chat-shell--side-collapsed');
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
  }

  function bindSideToggle() {
    const btn = ui.byId('chat-side-toggle-btn');
    const shell = ui.byId('chat-shell');
    if (!btn || !shell) return;
    // 展开/收起按钮视作右侧边栏的一部分：展开时移入侧栏 header（右上角），收起时回到对话区 header。
    // querySelector 按 class 定位 header；宿主 DOM 缺失时（如测试桩）跳过移动，不影响 toggle 语义。
    const stackHeader = shell.querySelector('.chat-stack > .panel-header');
    const sidePanel = ui.byId('chat-side-panel');
    const sideHeader = sidePanel ? sidePanel.querySelector('.panel-header') : null;
    function placeToggleBtn() {
      const collapsed = shell.classList.contains('chat-shell--side-collapsed');
      const target = (collapsed || !sideHeader) ? stackHeader : sideHeader;
      if (target && target !== btn.parentNode) target.appendChild(btn);
    }
    btn.addEventListener('click', () => {
      shell.classList.toggle('chat-shell--side-collapsed');
      placeToggleBtn();
      syncSideCollapse();
    });
    placeToggleBtn();
    syncSideCollapse();
  }

  function syncSessionsCollapse() {
    const shell = ui.byId('chat-shell');
    const hideBtn = ui.byId('chat-session-toggle-btn');
    const expandBtn = ui.byId('chat-session-expand-btn');
    if (!shell || !hideBtn || !expandBtn) return;
    const collapsed = shell.classList.contains('chat-shell--sessions-collapsed');
    hideBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    expandBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    expandBtn.hidden = !collapsed;
  }

  function bindSessionsToggle() {
    const shell = ui.byId('chat-shell');
    const hideBtn = ui.byId('chat-session-toggle-btn');
    const expandBtn = ui.byId('chat-session-expand-btn');
    if (!shell || !hideBtn || !expandBtn) return;
    hideBtn.addEventListener('click', () => {
      shell.classList.add('chat-shell--sessions-collapsed');
      syncSessionsCollapse();
      expandBtn.focus();
    });
    expandBtn.addEventListener('click', () => {
      shell.classList.remove('chat-shell--sessions-collapsed');
      syncSessionsCollapse();
      hideBtn.focus();
    });
    // 初始同步：隐藏按钮 aria 与展开按钮 hidden 跟随面板状态；展开按钮 aria
    // 保留静态模板初值（false），仅在交互后由 syncSessionsCollapse 同步。
    const collapsed = shell.classList.contains('chat-shell--sessions-collapsed');
    hideBtn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    expandBtn.hidden = !collapsed;
  }

  function activateSideTab(value, focus) {
    if (value !== 'tool' && value !== 'artifact') return;
    activeSideTab = value;
    const toolBtn = ui.byId('chat-tab-tool-button');
    const artBtn = ui.byId('chat-tab-artifact-button');
    const toolPanel = ui.byId('chat-tab-tool');
    const artPanel = ui.byId('chat-tab-artifact');
    const tabs = [
      { btn: toolBtn, panel: toolPanel, key: 'tool' },
      { btn: artBtn, panel: artPanel, key: 'artifact' },
    ];
    tabs.forEach((t) => {
      const active = t.key === value;
      if (t.btn) {
        t.btn.classList.toggle('chat-tab--active', active);
        t.btn.setAttribute('aria-selected', active ? 'true' : 'false');
        t.btn.setAttribute('tabindex', active ? '0' : '-1');
      }
      if (t.panel) t.panel.hidden = !active;
    });
    const focusBtn = value === 'tool' ? toolBtn : artBtn;
    if (focus && focusBtn && typeof focusBtn.focus === 'function') focusBtn.focus();
  }

  function bindTabSwitch() {
    const toolBtn = ui.byId('chat-tab-tool-button');
    const artBtn = ui.byId('chat-tab-artifact-button');
    const tabs = [
      { btn: toolBtn, key: 'tool' },
      { btn: artBtn, key: 'artifact' },
    ];
    tabs.forEach((t, idx) => {
      if (!t.btn) return;
      t.btn.addEventListener('click', () => activateSideTab(t.key, false));
      t.btn.addEventListener('keydown', (ev) => {
        const k = ev && ev.key;
        if (k !== 'ArrowLeft' && k !== 'ArrowRight' && k !== 'Home' && k !== 'End') return;
        if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();
        let target;
        if (k === 'ArrowLeft') target = tabs[(idx + tabs.length - 1) % tabs.length];
        else if (k === 'ArrowRight') target = tabs[(idx + 1) % tabs.length];
        else if (k === 'Home') target = tabs[0];
        else target = tabs[tabs.length - 1];
        if (target && target.btn) activateSideTab(target.key, true);
      });
    });
    activateSideTab(activeSideTab, false);
  }

  async function renderArtifactPanel(options) {
    options = options || {};
    const target = ui.byId('chat-artifact-list');
    artifactPanelRequestSeq++;
    const seq = artifactPanelRequestSeq;
    const sid = currentSessionId;
    if (!target) return;
    if (!sid) { clearNode(target); target.textContent = '暂未选择会话'; return; }
    // silent 刷新（轮询）保留现有内容，避免每次 tick 闪烁「加载中...」
    if (!options.silent) target.textContent = '加载中...';
    try {
      // Query by the session-id association (source_session_id) so task-produced
      // artifacts (source_kind=task_artifact/task_attachment) registered against
      // this session are found. The old source_kind=session filter never matched
      // because no artifact is ever created with that source_kind.
      const params = new URLSearchParams({ source_session_id: sid, limit: '50' });
      const resp = await fetch('/chat/artifacts?' + params.toString());
      if (seq !== artifactPanelRequestSeq || sid !== currentSessionId) return;
      if (!resp.ok) throw new Error('load failed');
      const data = await resp.json();
      if (seq !== artifactPanelRequestSeq || sid !== currentSessionId) return;
      const items = (data && !Array.isArray(data) && Array.isArray(data.items)) ? data.items : null;
      if (items === null) throw new Error('invalid payload');
      const renderer = global.NAGENT && global.NAGENT.artifacts && global.NAGENT.artifacts.renderListItem;
      if (typeof renderer !== 'function') { target.textContent = '加载失败'; return; }
      if (!items.length) { clearNode(target); target.textContent = '暂无关联制品'; return; }
      const frag = document.createDocumentFragment();
      items.forEach((a) => {
        frag.appendChild(renderer(a, (artifact) => {
          const href = '/artifacts/' + encodeURIComponent(artifact.id);
          const nav = global.NAGENT && global.NAGENT.navigation;
          if (nav && typeof nav.navigatePath === 'function') nav.navigatePath(href);
          else global.location.href = href;
        }));
      });
      clearNode(target);
      target.appendChild(frag);
    } catch (e) {
      if (seq !== artifactPanelRequestSeq || sid !== currentSessionId) return;
      target.textContent = '加载失败';
    }
  }

  function loadExternalMemoryProviders() {
    fetch('/chat/external-memory/memory-providers')
      .then(res => res.json())
      .then(data => {
        externalMemoryProviders = data.providers || [];
        renderExternalMemoryUI();
      })
      .catch(err => {
        console.warn('Failed to load external memory providers:', err);
      });
  }

  function inferPhantomExternalMemorySlot(name, sessionSlots) {
    if (sessionSlots && sessionSlots[name]) return sessionSlots[name];
    if (/^external_memory_\d+$/.test(name) || /^project_memory_\d+$/.test(name)) {
      return 'multi-project';
    }
    const externalQueryPrefixes = ['mem0', 'holographic', 'honcho'];
    if (externalQueryPrefixes.some(prefix => name === prefix || name.startsWith(prefix + '-') || name.startsWith(prefix + '_'))) {
      return 'external-query';
    }
    return 'removed';
  }

  function renderExternalMemoryUI() {
    const container = document.getElementById('chat-external-memory');
    if (!container) return;
    container.replaceChildren();

    // 豆包风格 mode switcher：工具栏按钮 + Popover 分组选择卡片。
    const sessionConfig = currentSessionId ? sessionExternalMemoryConfig[currentSessionId] : draftExternalMemoryConfig;
    const locked = sessionConfig?.locked === true;
    const useSessionConfig = sessionConfig?.modified === true;
    const enabledProviders = useSessionConfig ? sessionConfig.providers : [];

    // 描述文案（作为 label 的 title tooltip，同时保留字符串供锁定提示）
    const descText = locked
      ? '此会话的记忆已锁定'
      : '文件记忆最多选择 1 个；首轮发送后锁定';

    const bar = document.createElement('div');
    bar.className = 'chat-memory-bar';

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'chat-memory-trigger';
    trigger.title = descText;
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-expanded', memoryPopoverOpen ? 'true' : 'false');
    const triggerLabel = document.createElement('span');
    triggerLabel.className = 'chat-memory-trigger__label';
    triggerLabel.textContent = '记忆';
    trigger.append(createMemoryTriggerIcon(), triggerLabel);
    if (enabledProviders.length > 0) trigger.classList.add('active');
    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      memoryPopoverOpen = !memoryPopoverOpen;
      renderExternalMemoryUI();
    });

    // 按 slot 过滤出可见 provider，保留原始顺序
    const visibleProviders = externalMemoryProviders.filter(p => {
      if (p.slot === 'external-query') {
        // active 的检索 provider 始终展示（可勾选）；
        // 非 active 的仅在历史会话锁定 profile 引用时展示（忠实展示）
        if (p.active === true) return true;
        if (useSessionConfig && enabledProviders.includes(p.name)) return true;
        return false;
      }
      if (p.name !== 'builtin' && !p.enabled_global && !enabledProviders.includes(p.name)) {
        return false;
      }
      return true;
    });

    // phantom 兜底：历史会话 profile 引用但既不在 manager 也不在 registry 的 provider（已删除）。
    // slot 优先取锁定时持久化的 external_memory_slots；旧会话无 slot 快照时按历史命名兼容推断。
    const sessionSlots = (useSessionConfig && sessionConfig?.slots) ? sessionConfig.slots : null;
    if (useSessionConfig) {
      const knownNames = new Set(visibleProviders.map(p => p.name));
      enabledProviders.forEach(name => {
        if (name !== 'builtin' && !knownNames.has(name)) {
          visibleProviders.push({
            name,
            slot: inferPhantomExternalMemorySlot(name, sessionSlots),
            active: false,
            phantom: true,
          });
        }
      });
    }

    const saveSelectionFromPopover = () => {
      const checked = [];
      document.querySelectorAll('#chat-external-memory .chat-memory-option').forEach(optionEl => {
        if (optionEl.classList.contains('active') && optionEl.dataset.providerName) {
          checked.push(optionEl.dataset.providerName);
        }
      });
      const nextConfig = {
        providers: checked,
        modified: true
      };
      if (currentSessionId) {
        sessionExternalMemoryConfig[currentSessionId] = nextConfig;
      } else {
        draftExternalMemoryConfig = nextConfig;
      }
      externalMemoryTouched = true;
    };

    const buildOption = (p) => {
      const pill = document.createElement('button');
      pill.type = 'button';
      pill.className = 'chat-memory-option';
      pill.dataset.providerName = p.name;
      pill.dataset.slot = p.slot || '';
      pill.disabled = locked;

      const isActive = useSessionConfig && enabledProviders.includes(p.name);
      if (isActive) pill.classList.add('active');
      if (p.phantom === true) pill.classList.add('chat-memory-option--phantom');
      if (p.slot === 'external-query' && p.active === false) pill.classList.add('chat-memory-option--disabled-slot');

      // phantom: provider 已删除；非 active 检索 provider: 已禁用（adapter 未装载）
      const labelText = (function () {
        if (p.phantom === true) return p.name + ' (已删除)';
        if (p.slot === 'external-query' && p.active === false) return p.name + ' (已禁用)';
        return p.name;
      })();
      pill.textContent = labelText;
      pill.title = labelText;

      pill.addEventListener('click', () => {
        if (pill.disabled) return;
        const nextActive = !pill.classList.contains('active');
        // 仅 multi-project slot 互斥（文件记忆最多 1 个）；external-query 与 multi-project 可共存
        if (nextActive && pill.dataset.slot === 'multi-project') {
          document.querySelectorAll('#chat-external-memory .chat-memory-option').forEach(other => {
            if (other !== pill && other.classList.contains('active') && other.dataset.slot === 'multi-project') {
              other.classList.remove('active');
            }
          });
        }
        pill.classList.toggle('active', nextActive);
        // 标记用户已操作记忆勾选，后续请求需显式携带 options.external_memory_enabled
        saveSelectionFromPopover();
      });

      return pill;
    };

    bar.appendChild(trigger);
    container.appendChild(bar);

    if (memoryPopoverOpen) {
      const popover = document.createElement('div');
      popover.className = 'chat-memory-popover';
      popover.setAttribute('role', 'dialog');
      popover.setAttribute('aria-label', '选择记忆');
      popover.addEventListener('click', event => event.stopPropagation());

      const desc = document.createElement('div');
      desc.className = 'chat-memory-popover__desc';
      desc.textContent = descText;
      popover.appendChild(desc);

      const GROUP_ORDER = ['builtin', 'multi-project', 'external-query', 'removed'];
      const GROUP_LABELS = {
        'builtin': '系统',
        'multi-project': '文件',
        'external-query': '检索',
        'removed': '已移除',
      };

      GROUP_ORDER.forEach(slot => {
        const providers = visibleProviders.filter(p => (p.slot || '') === slot);
        if (providers.length === 0) return;

        const group = document.createElement('section');
        group.className = 'chat-memory-popover__group';

        const groupTitle = document.createElement('div');
        groupTitle.className = 'chat-memory-popover__group-title';
        groupTitle.textContent = GROUP_LABELS[slot] || slot;

        const groupItems = document.createElement('div');
        groupItems.className = 'chat-memory-popover__group-items';
        providers.forEach(p => groupItems.appendChild(buildOption(p)));

        group.append(groupTitle, groupItems);
        popover.appendChild(group);
      });

      if (!visibleProviders.length) {
        const empty = document.createElement('div');
        empty.className = 'chat-memory-popover__empty';
        empty.textContent = '暂无可用记忆';
        popover.appendChild(empty);
      }

      container.appendChild(popover);
    }
  }

  function handleMemoryDocumentClick(event) {
    const container = document.getElementById('chat-external-memory');
    if (!memoryPopoverOpen || (container && container.contains(event.target))) return;
    memoryPopoverOpen = false;
    renderExternalMemoryUI();
  }

  // === 调试设置：工具调试 / 任务状态 显隐（按会话独立） ===
  // 设置弹框复用记忆弹框的 .chat-memory-trigger / .chat-memory-popover / .chat-memory-option
  // 样式，与记忆弹框保持一致；显隐状态按会话独立持久化到 localStorage
  // （DEBUG_SETTINGS_KEY 的值为 {sessionId: {task, tool}} 映射），切换会话各用各的设置。
  function loadDebugSettings() {
    try {
      if (typeof localStorage === 'undefined') return {};
      const raw = localStorage.getItem(DEBUG_SETTINGS_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
      const map = {};
      Object.keys(parsed).forEach((sid) => {
        const v = parsed[sid];
        if (v && typeof v === 'object' && !Array.isArray(v)) {
          map[sid] = {
            task: typeof v.task === 'boolean' ? v.task : DEBUG_DEFAULTS.task,
            compression: typeof v.compression === 'boolean' ? v.compression : DEBUG_DEFAULTS.compression,
            tool: typeof v.tool === 'boolean' ? v.tool : DEBUG_DEFAULTS.tool,
          };
        }
      });
      return map;
    } catch (e) {
      return {};
    }
  }

  function saveDebugSettings() {
    try {
      if (typeof localStorage === 'undefined') return;
      localStorage.setItem(DEBUG_SETTINGS_KEY, JSON.stringify(sessionDebugSettings));
    } catch (e) { /* ignore persistence failure */ }
  }

  // 当前生效的调试设置：有会话用会话配置（无记录则默认值），无会话用 draft（无则默认值）。
  function getDebugSettings() {
    if (currentSessionId) {
      return sessionDebugSettings[currentSessionId] || DEBUG_DEFAULTS;
    }
    return draftDebugSettings || DEBUG_DEFAULTS;
  }

  // 写入当前上下文的调试设置：有会话则写入会话映射并持久化，无会话则写入 draft。
  function setDebugSettings(next) {
    if (currentSessionId) {
      sessionDebugSettings[currentSessionId] = next;
      saveDebugSettings();
    } else {
      draftDebugSettings = next;
    }
  }

  // 按当前会话的调试设置在稳定的 #chat-messages 滚动容器上切换 hide class；
  // CSS 据此隐藏对应 data-debug-kind 卡片。容器在消息重渲染中保持稳定。
  // 切换会话后须重新调用以套用目标会话的设置。
  function applyDebugVisibility() {
    const el = ui.byId('chat-messages');
    if (!el) return;
    const s = getDebugSettings();
    el.classList.toggle('chat-debug--hide-tool', !s.tool);
    el.classList.toggle('chat-debug--hide-task', !s.task);
    el.classList.toggle('chat-debug--hide-compression', !s.compression);
  }

  // 互斥：记忆与设置弹框同一时刻只能开一个，新开一个则收回另一个。
  // closeSettingsPopover / closeMemoryPopover 各自幂等（已关闭则 no-op）。
  function closeMemoryPopover() {
    if (!memoryPopoverOpen) return;
    memoryPopoverOpen = false;
    renderExternalMemoryUI();
  }

  function closeSettingsPopover() {
    if (!settingsPopoverOpen) return;
    settingsPopoverOpen = false;
    renderSettingsUI();
  }

  // capture 阶段拦截 trigger 点击：点击记忆区域则收起设置、点击设置区域则收起记忆。
  // 在 trigger 自身 stopPropagation 之前完成互斥，因此无需改动记忆弹框代码。
  function handlePopoverMutualExclusion(event) {
    const target = event.target;
    if (!target || !target.closest) return;
    if (target.closest('#chat-settings')) {
      closeMemoryPopover();
    } else if (target.closest('#chat-external-memory')) {
      closeSettingsPopover();
    }
  }

  function createSettingsTriggerIcon() {
    const svg = createSvgElement('svg', {
      class: 'chat-memory-trigger__icon',
      viewBox: '0 0 24 24',
      fill: 'none',
      'aria-hidden': 'true',
    });
    svg.append(
      createSvgElement('path', {
        d: 'M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z',
        stroke: 'currentColor',
        'stroke-width': '2',
        'stroke-linecap': 'round',
        'stroke-linejoin': 'round',
      }),
      createSvgElement('circle', {
        cx: '12',
        cy: '12',
        r: '3',
        stroke: 'currentColor',
        'stroke-width': '2',
      })
    );
    return svg;
  }

  function renderSettingsUI() {
    const container = document.getElementById('chat-settings');
    if (!container) return;
    container.replaceChildren();

    const bar = document.createElement('div');
    bar.className = 'chat-memory-bar';

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'chat-memory-trigger';
    trigger.title = '调试设置';
    trigger.setAttribute('aria-haspopup', 'dialog');
    trigger.setAttribute('aria-expanded', settingsPopoverOpen ? 'true' : 'false');
    const triggerLabel = document.createElement('span');
    triggerLabel.className = 'chat-memory-trigger__label';
    triggerLabel.textContent = '设置';
    trigger.append(createSettingsTriggerIcon(), triggerLabel);
    trigger.addEventListener('click', (event) => {
      event.stopPropagation();
      settingsPopoverOpen = !settingsPopoverOpen;
      renderSettingsUI();
    });

    bar.appendChild(trigger);
    container.appendChild(bar);

    if (settingsPopoverOpen) {
      const popover = document.createElement('div');
      popover.className = 'chat-memory-popover';
      popover.setAttribute('role', 'dialog');
      popover.setAttribute('aria-label', '调试设置');
      popover.addEventListener('click', (event) => event.stopPropagation());

      const group = document.createElement('section');
      group.className = 'chat-memory-popover__group';
      const groupTitle = document.createElement('div');
      groupTitle.className = 'chat-memory-popover__group-title';
      groupTitle.textContent = '调试';
      const groupItems = document.createElement('div');
      groupItems.className = 'chat-memory-popover__group-items';

      const options = [
        { key: 'task', label: '任务状态' },
        { key: 'compression', label: '对话压缩' },
        { key: 'tool', label: '工具调试' },
      ];
      const current = getDebugSettings();
      options.forEach((opt) => {
        const pill = document.createElement('button');
        pill.type = 'button';
        pill.className = 'chat-memory-option';
        pill.textContent = opt.label;
        if (current[opt.key]) pill.classList.add('active');
        pill.setAttribute('aria-pressed', current[opt.key] ? 'true' : 'false');
        pill.addEventListener('click', () => {
          // 写入当前上下文（有会话写会话映射、无会话写 draft），保证按会话独立
          const next = { ...getDebugSettings(), [opt.key]: !getDebugSettings()[opt.key] };
          setDebugSettings(next);
          applyDebugVisibility();
          renderSettingsUI();
        });
        groupItems.appendChild(pill);
      });

      group.append(groupTitle, groupItems);
      popover.appendChild(group);
      container.appendChild(popover);
    }
  }

  function handleSettingsDocumentClick(event) {
    const container = document.getElementById('chat-settings');
    if (!settingsPopoverOpen || (container && container.contains(event.target))) return;
    settingsPopoverOpen = false;
    renderSettingsUI();
  }

  function getExternalMemoryEnabled() {
    const config = currentSessionId ? sessionExternalMemoryConfig[currentSessionId] : draftExternalMemoryConfig;
    if (!config?.modified) return [];
    return config.providers;
  }

  function applySessionExternalMemoryState(detail) {
    const session = detail && detail.session;
    if (!session || !session.id) return;
    const messageCount = Array.isArray(detail.messages) ? detail.messages.length : 0;
    const locked = messageCount > 0;
    if (Array.isArray(session.external_memory_enabled)) {
      sessionExternalMemoryConfig[session.id] = {
        providers: session.external_memory_enabled,
        slots: (session.external_memory_slots && typeof session.external_memory_slots === 'object')
          ? session.external_memory_slots : null,
        modified: true,
        locked
      };
      return;
    }
    if (locked) {
      sessionExternalMemoryConfig[session.id] = {
        providers: [],
        slots: null,
        modified: true,
        locked: true
      };
      return;
    }
    delete sessionExternalMemoryConfig[session.id];
  }

  let imagePreviewModal = null;
  function ensureImagePreviewModal() {
    if (imagePreviewModal) return imagePreviewModal;
    const modalEl = document.createElement('div');
    modalEl.className = 'image-preview-modal';
    modalEl.hidden = true;
    modalEl.setAttribute('role', 'dialog');
    modalEl.setAttribute('aria-modal', 'true');
    modalEl.setAttribute('aria-label', '图片预览');
    const img = document.createElement('img');
    img.className = 'image-preview-modal__img';
    img.alt = '';
    modalEl.appendChild(img);
    modalEl.addEventListener('click', () => { modalEl.hidden = true; });
    document.body.appendChild(modalEl);
    imagePreviewModal = modalEl;
    return modalEl;
  }
  function openImagePreview(src) {
    const modalEl = ensureImagePreviewModal();
    modalEl.querySelector('.image-preview-modal__img').src = src;
    modalEl.hidden = false;
  }
  function initImagePreview() {
    document.addEventListener('click', (e) => {
      const target = e.target;
      if (!target || target.tagName !== 'IMG') return;
      if (!target.closest('.msg')) return;
      e.stopPropagation();
      openImagePreview(target.src);
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && imagePreviewModal && !imagePreviewModal.hidden) {
        imagePreviewModal.hidden = true;
      }
    });
  }

  function init() {
    if (initialized) return;
    initialized = true;
    const sendBtn = ui.byId('chat-send');
    const input = ui.byId('chat-input');
    const sessionFilterBtn = ui.byId('chat-session-filter-btn');
    const sessionSearchBtn = ui.byId('chat-session-search-btn');
    if (sessionFilterBtn) sessionFilterBtn.addEventListener('click', openSessionSourceFilterModal);
    if (sessionSearchBtn) sessionSearchBtn.addEventListener('click', openSessionSearchModal);
    if (sendBtn) sendBtn.addEventListener('click', send);
    if (input) {
      input.addEventListener('keydown', handleComposerKeydown);
      input.addEventListener('paste', handlePaste);
    }
    const imageBtn = ui.byId('chat-image-button');
    const imageInput = ui.byId('chat-image-input');
    if (imageBtn && imageInput) {
      imageBtn.addEventListener('click', () => imageInput.click());
      imageInput.addEventListener('change', handleFileSelect);
    }
    bindSideToggle();
    bindSessionsToggle();
    bindTabSwitch();
    renderArtifactPanel();
    document.addEventListener('click', handleMemoryDocumentClick);
    document.addEventListener('click', handleSettingsDocumentClick);
    document.addEventListener('click', handlePopoverMutualExclusion, true);
    initImagePreview();
    // 激活态自动刷新：页面隐藏时停止（不浪费请求），可见时立即追赶一次并恢复周期。
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { stopAutoRefresh(); return; }
      if (currentSessionId && !isSending) startAutoRefresh(currentSessionId, { immediate: true });
    });
    window.addEventListener('beforeunload', stopAutoRefresh);
    // Memory mode switcher lives in the composer toolbar, before the send button.
    const composerBar = document.querySelector('.chat-composer__bar');
    const emContainer = document.createElement('div');
    emContainer.id = 'chat-external-memory';
    emContainer.className = 'chat-external-memory';
    // 调试设置容器：紧随记忆之后、发送按钮之前，复用记忆弹框样式
    const settingsContainer = document.createElement('div');
    settingsContainer.id = 'chat-settings';
    settingsContainer.className = 'chat-settings';
    if (composerBar) {
      if (sendBtn) {
        composerBar.insertBefore(emContainer, sendBtn);
        composerBar.insertBefore(settingsContainer, sendBtn);
      } else {
        composerBar.appendChild(emContainer);
        composerBar.appendChild(settingsContainer);
      }
      loadExternalMemoryProviders();
      sessionDebugSettings = loadDebugSettings();
      renderSettingsUI();
      applyDebugVisibility();
    }
    showEmptyState();
    updateInfo({});
    loadSessions();
    loadDefaultModel();
  }

  async function loadDefaultModel() {
    try {
      const data = await api.getAdminModels();
      defaultModel = (data && data.default_model) ? data.default_model : '';
    } catch (err) {
      defaultModel = '';
    }
  }

  global.NAGENT = namespace;
  global.NAGENT.chat = { init, parseTaskCommand, runTaskCommand, send, createMessageElement, validateTaskCard, validateToolApprovalCard, resolveToolApprovalDecisions, groupTaskMessages, applySessionDetail, shouldRenderMessage, getDebugSettings, setDebugSettings, createSSEParser, renderToolApprovalCard, disableApprovalCards, isValidApprovalPayload, isSuccessfulBrowserScreenshot, setHeader, buildHeaderLinks, loadSessionViewLinks };
}(window));
