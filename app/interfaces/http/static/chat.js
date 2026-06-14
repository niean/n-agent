(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  let currentSessionId = null;
  let isSending = false;
  let initialized = false;

  function appendText(parent, value) {
    parent.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  }

  function clearNode(node) { ui.clear(node); }

  function setSending(next) {
    isSending = next;
    const btn = ui.byId('chat-send');
    const input = ui.byId('chat-input');
    if (btn) { btn.disabled = next; btn.textContent = next ? 'Sending' : 'Send'; }
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
    appendText(el, message.content || '');
    return el;
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
    if (!window.confirm(`确定删除会话「${label}」？关联消息将一并删除`)) return;
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
    setHeader(id);
    try {
      const detail = await api.getSessionDetail(id);
      const stack = ui.byId('chat-message-stack');
      if (stack) {
        clearNode(stack);
        const messages = detail.messages || [];
        if (!messages.length) showEmptyState();
        messages.forEach((message) => stack.appendChild(createMessageElement(message)));
      }
      updateInfo(detail);
      await loadSessions();
      scrollToBottom();
    } catch (error) {
      setStatusMessage('加载会话失败: ' + error.message, 'error');
    }
  }

  async function ensureSession() {
    if (currentSessionId) return currentSessionId;
    const id = 'session-' + Date.now();
    await api.createSession(id);
    currentSessionId = id;
    setHeader(id);
    showEmptyState();
    updateInfo({});
    await loadSessions();
    return id;
  }

  async function newSession() {
    currentSessionId = null;
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
        appendText(el, `${call.tool_name}: ${JSON.stringify(call.arguments)} → ${call.status} (${call.duration_ms}ms)`);
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

  async function refreshCurrentSession() {
    if (!currentSessionId) return;
    try {
      const detail = await api.getSessionDetail(currentSessionId);
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
    if (!text) return;
    await ensureSession();
    input.value = '';
    appendMessage('user', text);
    const streaming = appendMessage('assistant', '');
    setSending(true);
    try {
      const res = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: 'model',
          messages: [{ role: 'user', content: text }],
          stream: true,
          metadata: { session_id: currentSessionId },
        }),
      });
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

  function init() {
    if (initialized) return;
    initialized = true;
    const sendBtn = ui.byId('chat-send');
    const input = ui.byId('chat-input');
    const newBtn = ui.byId('chat-new');
    if (sendBtn) sendBtn.addEventListener('click', send);
    if (input) input.addEventListener('keydown', handleComposerKeydown);
    if (newBtn) newBtn.addEventListener('click', newSession);
    showEmptyState();
    updateInfo({});
    loadSessions();
  }

  global.NAGENT = namespace;
  global.NAGENT.chat = { init };
}(window));
