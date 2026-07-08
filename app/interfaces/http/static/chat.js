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
  // 仅当为 true 时才向 /v1/chat/completions 携带 options.external_memory_enabled，
  // 未操作时不发送该字段，由后端按会话默认 profile 派生。
  let externalMemoryTouched = false;
  // 后端真实默认模型 id，启动时从 /chat/models 拉取；硬编码占位符会导致
  // provider 拒绝 tool 调用（如 Ark "does not support agent plan feature"）。
  let defaultModel = '';
  // 待发送的图片列表，每项 {dataUrl, name}；发送后清空。
  let pendingImages = [];

  function appendText(parent, value) {
    parent.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
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
    if (hasVisibleContent(content)) appendText(el, content);
    return el;
  }

  function shouldRenderMessage(message) {
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

  function appendMessage(role, content) {
    const stack = ui.byId('chat-message-stack');
    if (!stack) return null;
    const empty = stack.querySelector('.empty-hero');
    if (empty) clearNode(stack);
    const el = createMessageElement({ role, content });
    stack.appendChild(el);
    scrollToBottom();
    return el;
  }

  function renderSessionMessages(detail) {
    const stack = ui.byId('chat-message-stack');
    if (!stack) return;
    clearNode(stack);
    const visibleMessages = groupToolMessages((detail.messages || []).filter(shouldRenderMessage));
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

  function buildSessionItem(session) {
    const item = document.createElement('div');
    item.className = `session-item${session.id === currentSessionId ? ' active' : ''}`;

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
      currentSessionId = null;
      setHeader(null);
      showEmptyState();
      updateInfo({});
    }
    await loadSessions();
  }

  async function selectSession(id) {
    currentSessionId = id;
    draftExternalMemoryConfig = null;
    setHeader(id);
    // 会话切换：重置外部记忆操作标记，避免沿用上一会话的 options 注入
    externalMemoryTouched = false;
    try {
      const detail = await api.getSessionDetail(id);
      applySessionExternalMemoryState(detail);
      renderExternalMemoryUI();
      renderSessionMessages(detail);
      updateInfo(detail);
      await loadSessions();
      scrollToBottom();
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
    return id;
  }

  async function newSession() {
    currentSessionId = null;
    draftExternalMemoryConfig = null;
    await ensureSession();
  }

  function setHeader(id) {
    const header = ui.byId('chat-header');
    if (header) header.textContent = id || 'N-Agent Chat';
  }

  function updateInfo(detail) {
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
    loadToolCalls();
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
      metadata: { session_id: currentSessionId },
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
    try {
      const detail = await api.getSessionDetail(currentSessionId);
      applySessionExternalMemoryState(detail);
      renderExternalMemoryUI();
      renderSessionMessages(detail);
      updateInfo(detail);
      await loadSessions();
    } catch (error) {
      const summary = ui.byId('chat-summary');
      if (summary) summary.textContent = '刷新会话失败: ' + error.message;
    }
  }

  async function send() {
    if (isSending) return;
    const input = ui.byId('chat-input');
    if (!input) return;
    const text = input.value.trim();
    if (!text && !pendingImages.length) return;
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
    appendMessage('user', userContent);
    const streaming = appendMessage('assistant', '');
    setSending(true);
    try {
      const res = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
      refreshCurrentSession();
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
      : 'builtin 默认关闭；文件记忆最多选择 1 个；首轮发送后锁定';

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

      if (useSessionConfig && !locked) {
        const resetBtn = document.createElement('button');
        resetBtn.className = 'chat-memory-popover__reset';
        resetBtn.type = 'button';
        resetBtn.textContent = '重置为默认配置';
        resetBtn.addEventListener('click', () => {
          if (currentSessionId) {
            delete sessionExternalMemoryConfig[currentSessionId];
          } else {
            draftExternalMemoryConfig = null;
          }
          externalMemoryTouched = false;
          renderExternalMemoryUI();
        });
        popover.appendChild(resetBtn);
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
  global.NAGENT.chat = { init };
}(window));
