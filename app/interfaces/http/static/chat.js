(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;
  const modal = namespace.modal;

  let currentSessionId = null;
  let isSending = false;
  let initialized = false;
  let externalMemoryProviders = [];
  let memoryPopoverOpen = false;
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
      });
      if (sessionId === currentSessionId && gen === autoRefreshGeneration && seq === autoRefreshSeq) {
        renderedMessageVersion = version;
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
  // 浏览器按本地时区解析后，按 今天/昨天/今年/往年 分级格式化。返回空串表示无时间。
  function formatMessageTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (isNaN(date.getTime())) return '';
    const now = new Date();
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    const hhmm = pad(date.getHours()) + ':' + pad(date.getMinutes());
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const startOfMsgDay = new Date(date.getFullYear(), date.getMonth(), date.getDate());
    const dayDiff = Math.round((startOfToday - startOfMsgDay) / 86400000);
    if (dayDiff === 0) return hhmm;
    if (dayDiff === 1) return '昨天 ' + hhmm;
    if (date.getFullYear() === now.getFullYear()) {
      return (date.getMonth() + 1) + '月' + date.getDate() + '日 ' + hhmm;
    }
    return date.getFullYear() + '/' + (date.getMonth() + 1) + '/' + date.getDate() + ' ' + hhmm;
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

  function createMessageElement(message) {
    if (message.is_summary) {
      const el = document.createElement('div');
      el.className = 'msg msg--summary';
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      const content = document.createElement('pre');
      summary.textContent = '对话摘要调试信息';
      const prefix = '[CONTEXT SUMMARY]: ';
      const raw = typeof message.content === 'string' ? message.content : String(message.content || '');
      content.textContent = raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;
      details.append(summary, content);
      el.appendChild(details);
      return el;
    }
    const el = document.createElement('div');
    el.className = `msg ${message.role || 'assistant'}`;
    if (message.role === 'tool') {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      const content = document.createElement('pre');
      summary.textContent = '工具调用调试信息';
      appendToolDebugContent(content, message.content || '');
      details.append(summary, content);
      el.appendChild(details);
      return el;
    }
    // system 消息：遵循消息渲染规范，按工具调用调试信息样式渲染为可折叠气泡（默认展开，
    // 保证命令结果可见、给人聊天感），textContent 安全渲染，无 innerHTML。
    if (message.role === 'system') {
      const details = document.createElement('details');
      details.open = false;  // 默认折叠（任务指令/任务状态/系统消息），点击展开
      const summary = document.createElement('summary');
      summary.textContent = message.name === 'ui.task_command' ? '任务指令'
        : message.name === 'ui.task_lifecycle' ? '任务状态' : '系统消息';
      el.dataset.name = message.name || '';
      const content = document.createElement('pre');
      content.textContent = typeof message.content === 'string' ? message.content : String(message.content || '');
      details.append(summary, content);
      el.appendChild(details);
      return el;
    }
    // user/assistant 消息：附加 Hover 时间（飞书风格），由 CSS ::before 展示
    const timeLabel = formatMessageTime(message.created_at);
    if (timeLabel) el.dataset.time = timeLabel;
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
    return el;
  }

  function shouldRenderMessage(message) {
    if (message.is_summary) return true;
    if (message.role === 'tool') return true;
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

  function renderSessionMessages(detail) {
    const stack = ui.byId('chat-message-stack');
    if (!stack) return;
    clearNode(stack);
    let visibleMessages = groupToolMessages((detail.messages || []).filter(shouldRenderMessage));
    visibleMessages = groupTaskCommandMessages(visibleMessages);
    if (!visibleMessages.length) showEmptyState();
    visibleMessages.forEach((message) => stack.appendChild(createMessageElement(message)));
  }

  async function loadSessions() {
    const list = ui.byId('chat-session-list');
    if (!list) return;
    try {
      const sessions = await api.listSessions();
      clearNode(list);
      if (!sessions.length) { ui.renderEmpty(list, '暂无会话'); return; }
      sessions.forEach((session) => list.appendChild(buildSessionItem(session)));
    } catch (error) {
      clearNode(list);
      ui.renderError(list, '加载会话失败: ' + error.message);
    }
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
    (sessions || []).forEach((s) => titleById.set(s.id, s.title || s.id));
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
    if (currentSessionId === session.id) {
      stopAutoRefresh();
      renderedMessageVersion = null;
      currentSessionId = null;
      setHeader(null);
      showEmptyState();
      updateInfo({});
    }
    await loadSessions();
  }

  async function selectSession(id) {
    stopAutoRefresh();
    currentSessionId = id;
    draftExternalMemoryConfig = null;
    setHeader(id);
    // 会话切换：重置外部记忆操作标记，避免沿用上一会话的 options 注入
    externalMemoryTouched = false;
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
    // Chat 会话默认关闭 builtin 记忆，外部记忆需用户手动勾选。
    renderExternalMemoryUI();
    showEmptyState();
    updateInfo({});
    await loadSessions();
    renderedMessageVersion = { count: 0, lastId: null };
    startAutoRefresh(id);
    return id;
  }

  async function newSession() {
    stopAutoRefresh();
    currentSessionId = null;
    draftExternalMemoryConfig = null;
    await ensureSession();
  }

  function setHeader(id) {
    const header = ui.byId('chat-header');
    if (header) header.textContent = id || 'N-Agent Chat';
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
    renderSessionMessages(detail);
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

  // 命令结果统一以 [任务指令] 前缀的 system 消息呈现（spec UI Design 要求）。
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
    const body = truncateTaskMessageUtf8('[任务指令] ' + message);
    if (currentSessionId === sessionId) appendOrMergeTaskCommand(body);
    const preVersion = renderedMessageVersion;
    const persisted = await persistTaskSystemMessage(sessionId, body);
    // 持久化成功且返回真实 id 时推进版本，避免下一次轮询误判变更触发无意义重渲。
    // 期间版本若被权威详情改变（并发追加/切换），跳过以权威为准。
    if (persisted && persisted.id) advanceVersionAfterPersistedAppend(persisted.id, preVersion);
  }

  // 相邻任务指令合并：/task 命令记录（"[任务指令] 执行命令: ..."）与其回执（结果/错误）
  // 渲染为同一条任务指令气泡。命令记录开新气泡，回执追加到上一条任务指令气泡的 <pre>。
  const TASK_CMD_EXEC_PREFIX = '[任务指令] 执行命令: ';

  function isTaskCommandRecord(content) {
    return typeof content === 'string' && content.startsWith(TASK_CMD_EXEC_PREFIX);
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
            return;
          }
        }
      }
    }
    appendMessage('system', body, undefined, 'ui.task_command');
  }

  function groupTaskCommandMessages(messages) {
    // 渲染时合并相邻 ui.task_command：命令记录开新组，回执追加到当前组（命令记录+回执一条）
    const result = [];
    let currentGroup = null;
    for (const msg of messages) {
      if (msg.role === 'system' && msg.name === 'ui.task_command') {
        if (isTaskCommandRecord(msg.content) || currentGroup === null) {
          currentGroup = {role: 'system', name: 'ui.task_command', content: String(msg.content)};
          result.push(currentGroup);
        } else {
          currentGroup.content = String(currentGroup.content) + '\n' + String(msg.content);
        }
      } else {
        currentGroup = null;
        result.push(msg);
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

  // Execute a parsed /task command via api.task.*, appending a [任务指令] system
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
    try {
      const res = await fetch('/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Session-ID': currentSessionId,
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
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value);
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') continue;
          try {
            const json = JSON.parse(data);
            const content = (json.choices && json.choices[0] && json.choices[0].delta && json.choices[0].delta.content) || '';
            if (content && streaming) streaming.textContent += content;
          } catch (error) {
            // ignore malformed chunk
          }
        }
        scrollToBottom();
      }
    } catch (error) {
      if (streaming) {
        streaming.className = 'msg error';
        streaming.textContent = '[Error: ' + error.message + ']';
      }
    } finally {
      setSending(false);
      await refreshCurrentSession();
      input.focus();
    }
  }

  function bindDebugToggle() {
    const panel = ui.byId('chat-debug-panel');
    const toggle = ui.byId('chat-debug-toggle');
    const shell = ui.byId('chat-shell');
    if (!panel || !toggle) return;
    toggle.addEventListener('click', () => {
      const collapsed = panel.classList.toggle('collapsed');
      if (shell) shell.classList.toggle('chat-shell--debug-collapsed', collapsed);
      toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    });
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
    const newBtn = ui.byId('chat-new');
    if (sendBtn) sendBtn.addEventListener('click', send);
    if (input) {
      input.addEventListener('keydown', handleComposerKeydown);
      input.addEventListener('paste', handlePaste);
    }
    if (newBtn) newBtn.addEventListener('click', newSession);
    const imageBtn = ui.byId('chat-image-button');
    const imageInput = ui.byId('chat-image-input');
    if (imageBtn && imageInput) {
      imageBtn.addEventListener('click', () => imageInput.click());
      imageInput.addEventListener('change', handleFileSelect);
    }
    bindDebugToggle();
    document.addEventListener('click', handleMemoryDocumentClick);
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
    if (composerBar) {
      if (sendBtn) {
        composerBar.insertBefore(emContainer, sendBtn);
      } else {
        composerBar.appendChild(emContainer);
      }
      loadExternalMemoryProviders();
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
  global.NAGENT.chat = { init, parseTaskCommand, runTaskCommand, send };
}(window));
