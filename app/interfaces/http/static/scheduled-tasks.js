(function (global) {
  const namespace = global.NAGENT || {};
  const api = namespace.api;
  const state = {
    tasks: [],
    loading: false,
    error: '',
    view: 'list',
    detail: null,
    modal: null,
  };

  // 当前弹框 backdrop 引用；弹框挂载到 document.body，接入全局 modal 管理与统一样式。
  let modalBackdrop = null;

  function text(value, fallback) {
    if (value === null || value === undefined || value === '') return fallback || '-';
    return String(value);
  }

  function truncate(value, size) {
    const raw = text(value, '');
    if (!raw) return '-';
    return raw.length > size ? `${raw.slice(0, size)}...` : raw;
  }

  function formatDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    // East-8 (Asia/Shanghai) regardless of browser timezone
    const tz = new Date(date.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${tz.getUTCFullYear()}-${pad(tz.getUTCMonth() + 1)}-${pad(tz.getUTCDate())} ${pad(tz.getUTCHours())}:${pad(tz.getUTCMinutes())}:${pad(tz.getUTCSeconds())}`;
  }

  function clear(node) {
    if (node) node.textContent = '';
  }

  function appendText(parent, tag, content, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = content;
    parent.appendChild(node);
    return node;
  }

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
  }

  function button(label, className, onClick) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = className || 'btn';
    node.style.fontSize = 'var(--font-size-md)';
    node.textContent = label;
    node.addEventListener('click', onClick);
    return node;
  }

  function badge(value, kind) {
    const node = document.createElement('span');
    node.className = `badge${kind ? ` badge--${kind}` : ''}`;
    node.textContent = value;
    return node;
  }

  function statusKind(value) {
    if (value === 'active' || value === 'succeeded') return 'success';
    if (value === 'paused' || value === 'running') return 'warning';
    if (value === 'failed' || value === 'blocked' || value === 'session_missing') return 'danger';
    return '';
  }

  function getRoot() {
    return document.getElementById('scheduled-tasks-list-view');
  }

  function showError(message) {
    state.error = message;
    render();
  }

  async function refresh() {
    const root = getRoot();
    if (!root) return;
    // URL 回到列表规范路径时，重置 detail 视图（左导菜单点击会 pushState 到 /scheduled-tasks）
    if (window.location.pathname === '/scheduled-tasks') {
      state.view = 'list';
      state.detail = null;
    }
    state.loading = true;
    state.error = '';
    render();
    try {
      state.tasks = await api.listScheduledTasks();
    } catch (error) {
      state.error = error.message || 'scheduled_tasks_load_failed';
    } finally {
      state.loading = false;
      render();
    }
  }

  function render() {
    const listView = document.getElementById('scheduled-tasks-list-view');
    const detailView = document.getElementById('scheduled-tasks-detail-view');
    if (state.view === 'detail') {
      if (listView) listView.hidden = true;
      if (detailView) {
        detailView.hidden = false;
        clear(detailView);
        renderDetailPage(detailView);
      }
      return;
    }
    if (detailView) detailView.hidden = true;
    if (listView) listView.hidden = false;
    const statsRoot = document.getElementById('scheduled-tasks-stats');
    const listRoot = document.getElementById('scheduled-tasks-list');
    if (statsRoot) {
      clear(statsRoot);
      if (state.error) appendText(statsRoot, 'div', state.error, 'error-state');
      else if (state.loading) appendText(statsRoot, 'div', '加载中...', 'loading-state');
      else statsRoot.appendChild(renderStats());
    }
    if (listRoot) {
      clear(listRoot);
      if (!state.loading && !state.error) listRoot.appendChild(renderTable());
    }
  }

  function renderStats() {
    const stats = document.createElement('div');
    stats.className = 'scheduled-stats';
    const all = state.tasks.length;
    const active = state.tasks.filter((task) => task.status === 'active' && task.enabled !== false).length;
    const attention = state.tasks.filter((task) => task.status === 'session_missing' || task.last_status === 'failed' || task.last_status === 'blocked').length;
    const unread = state.tasks.filter((task) => task.unread || task.unread_count).length;
    [
      ['全部', all, '任务总数'],
      ['运行中', active, 'active + enabled'],
      ['需处理', attention, '失败、阻断或会话缺失'],
      ['未读', unread, '待查看运行结果'],
    ].forEach(([label, value, sub]) => {
      const card = document.createElement('div');
      card.className = 'stat-card';
      appendText(card, 'div', label, 'label');
      appendText(card, 'div', value, 'value');
      appendText(card, 'div', sub, 'sub');
      stats.appendChild(card);
    });
    return stats;
  }

  function renderTable() {
    const wrap = document.createElement('div');
    wrap.className = 'scheduled-table-wrap';
    if (!state.tasks.length) {
      appendText(wrap, 'div', '暂无任务，点击“新增”开始。', 'empty-state');
      return wrap;
    }

    const table = document.createElement('table');
    table.className = 'document-table scheduled-table';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    ['任务名称', '调度表达式', '启用', '下次运行', '最近结果', '操作'].forEach((label) => appendText(headRow, 'th', label));
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    const sortedTasks = state.tasks.slice().sort((a, b) =>
      text(a.name, a.id).localeCompare(text(b.name, b.id), 'zh-CN'));
    sortedTasks.forEach((task) => tbody.appendChild(renderTaskRow(task)));
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function renderTaskRow(task) {
    const row = document.createElement('tr');
    const titleCell = document.createElement('td');
    appendText(titleCell, 'strong', text(task.name, task.id));
    appendText(titleCell, 'div', task.id, 'muted scheduled-small');
    row.appendChild(titleCell);

    const scheduleCell = document.createElement('td');
    appendText(scheduleCell, 'div', text(task.cron_expression));
    appendText(scheduleCell, 'div', text(task.timezone), 'muted scheduled-small');
    row.appendChild(scheduleCell);

    const statusCell = document.createElement('td');
    const isEnabled = !(task.status === 'paused' || task.enabled === false);
    statusCell.appendChild(badge(isEnabled ? '启用' : '停用', isEnabled ? 'success' : 'warning'));
    row.appendChild(statusCell);

    appendText(row, 'td', formatDate(task.next_run_at));

    const lastCell = document.createElement('td');
    lastCell.appendChild(badge(text(task.last_status), statusKind(task.last_status)));
    row.appendChild(lastCell);

    const actionCell = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'row-actions';
    actions.appendChild(button(task.status === 'paused' || task.enabled === false ? '启用' : '停用', 'btn', () => toggleTask(task)));
    actions.appendChild(button('执行', 'btn', () => confirmRunTask(task)));
    actions.appendChild(button('编辑', 'btn', () => openTaskForm(task)));
    actions.appendChild(button('删除', 'btn', () => confirmDeleteTask(task)));
    actions.appendChild(button('详情', 'btn', () => goToDetail(task.id)));
    actionCell.appendChild(actions);
    row.appendChild(actionCell);
    return row;
  }

  function openTaskForm(task) {
    const delivery = task && task.delivery || {};
    const kind = delivery.kind || (task && task.delivery_target) || 'silent';
    const draft = {
      name: task ? task.name : '', prompt: task ? task.prompt : '',
      cron: task ? task.cron_expression : '', timezone: task ? task.timezone : 'Asia/Shanghai',
      tools: task ? (task.allowed_tools || []).join(',') : '', session: task ? task.session_id : '',
      deliveryKind: kind, deliveryId: delivery.conversation_id || '', deliveryDirty: !task,
    };
    const modal = { type: 'form', task: task || null, draft, error: '', generation: 0,
      sessions: [], selectedSession: null, total: null, offset: 0, loading: false, sessionsError: '', selectedError: '' };
    state.modal = modal;
    showModal();
    loadDeliverySessions(modal, true);
  }

  // 详情作为独立页面视图，URL 形如 /scheduled-tasks/{task_id}。
  function openTaskDetail(id) {
    state.view = 'detail';
    state.detail = { loading: true, task: null, executions: [], error: '' };
    render();
    (async () => {
      try {
        const detail = await api.getScheduledTask(id);
        const executions = await api.listScheduledTaskExecutions(id, 10);
        state.detail = { loading: false, task: detail, executions, error: '' };
      } catch (error) {
        state.detail = { loading: false, task: null, executions: [], error: error.message || 'scheduled_task_detail_failed' };
      }
      render();
    })();
  }

  function backToList() {
    const path = '/scheduled-tasks';
    if (window.location.pathname !== path) {
      history.pushState({ tab: 'scheduled-tasks' }, '', path);
    }
    state.view = 'list';
    state.detail = null;
    render();
  }

  // 本 Tab 内导航到任务详情：更新 URL 并切换视图，不新建标签页。
  function goToDetail(id) {
    const path = `/scheduled-tasks/${encodeURIComponent(id)}`;
    if (window.location.pathname !== path) {
      history.pushState({ tab: 'scheduled-tasks' }, '', path);
    }
    openTaskDetail(id);
  }

  function pendingTaskIdFromPath() {
    const match = window.location.pathname.match(/^\/scheduled-tasks\/([^/]+)$/);
    if (!match) return null;
    try { return decodeURIComponent(match[1]); } catch (_) { return null; }
  }

  function confirmDeleteTask(task) {
    state.modal = { type: 'delete', task, error: '' };
    showModal();
  }

  function confirmRunTask(task) {
    state.modal = { type: 'run_confirm', task, error: '' };
    showModal();
  }

  function closeModal() {
    state.modal = null;
    if (modalBackdrop) {
      modalBackdrop.remove();
      modalBackdrop = null;
    }
  }

  function showModal() {
    if (!state.modal) return;
    if (modalBackdrop) modalBackdrop.remove();
    const backdrop = el('div', 'modal-backdrop');
    const dialog = el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    if (state.modal.type === 'form') renderTaskForm(dialog);
    else if (state.modal.type === 'delete') renderDeleteConfirm(dialog);
    else if (state.modal.type === 'run_confirm') renderRunConfirm(dialog);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeModal();
    });
    document.body.appendChild(backdrop);
    modalBackdrop = backdrop;
    const closeBtn = dialog.querySelector('.modal-close');
    if (closeBtn) requestAnimationFrame(() => closeBtn.focus());
  }

  function renderModalHeader(parent, title) {
    const header = document.createElement('div');
    header.className = 'modal-header';
    appendText(header, 'h4', title);
    const closeBtn = button('×', 'modal-close', closeModal);
    closeBtn.setAttribute('aria-label', '关闭');
    header.appendChild(closeBtn);
    parent.appendChild(header);
  }

  function deliveryName(delivery) {
    return delivery.kind === 'feishu' ? '飞书IM' : delivery.kind === 'silent' || delivery.kind === 'dashboard' ? '不投递飞书IM' : '历史接收会话';
  }

  function deliveryLocation(delivery) {
    return delivery.kind === 'dashboard' || delivery.kind === 'silent' ? '不投递飞书IM' : delivery.label;
  }

  function deliveryReason(reason) {
    const reasons = {
      platform_unavailable: '飞书连接不可用', unsupported_platform: '历史平台暂不支持投递',
      home_target_not_configured: '尚未配置默认通知会话', invalid_legacy_target: '历史接收位置不完整',
      conversation_not_found: '接收会话已不存在', same_chat_representative: '同一父聊天请使用可选的代表会话',
      unknown_chat_type: '聊天类型尚未确认', ambiguous_chat_type: '聊天类型存在冲突',
      unsupported_address_type: '聊天类型尚未确认或暂不支持', address_type_conflict: '聊天类型存在冲突',
      snapshot_mismatch: '接收会话已变化，请重新选择', unsupported_target: '暂不支持此接收位置',
      invalid_delivery_target: '接收位置无效', invalid_import_target: '导入的接收位置无效',
    };
    return reasons[reason] || (reason ? '接收位置当前不可用，请重新选择或刷新' : '状态未知');
  }

  // Accept the original gateway projection while preferring the stable delivery-prefixed aliases.
  function deliverySession(item) {
    return { id: item.id, delivery_available: item.delivery_available,
      delivery_chat_name: item.delivery_chat_name,
      delivery_label: item.delivery_label ?? item.label,
      delivery_unavailable_reason: item.delivery_unavailable_reason ?? item.unavailable_reason,
      delivery_parent_chat: item.delivery_parent_chat ?? item.parent_chat };
  }

  function currentDeliveryRequest(modal, generation) {
    return state.modal === modal && modal.generation === generation;
  }

  // Each modal load owns a generation; late responses cannot mutate another draft.
  async function loadDeliverySessions(modal, reset) {
    if (state.modal !== modal || (modal.loading && !reset)) return;
    if (reset) {
      modal.generation += 1;
      modal.sessions = []; modal.sessionIds = new Set(); modal.total = null; modal.offset = 0; modal.sessionsError = ''; modal.selectedError = '';
    }
    const generation = modal.generation;
    modal.loading = true;
    showModal();
    if (reset && modal.draft.deliveryId) {
      const selectedId = modal.draft.deliveryId;
      (async () => {
        try {
          const item = await api.getGatewaySession('feishu', selectedId);
          if (!item || item.id !== selectedId) throw new Error('当前会话响应不完整');
          if (!currentDeliveryRequest(modal, generation) || modal.draft.deliveryId !== selectedId) return;
          modal.selectedSession = deliverySession(item);
        } catch (_) {
          if (!currentDeliveryRequest(modal, generation) || modal.draft.deliveryId !== selectedId) return;
          modal.selectedError = '当前接收会话加载失败；保留原选择，请刷新浏览器后重试。';
        }
        if (currentDeliveryRequest(modal, generation)) showModal();
      })();
    }
    try {
      do {
        const offset = modal.offset;
        const page = await api.listGatewaySessions('feishu', { limit: 20, offset });
        if (!currentDeliveryRequest(modal, generation)) return;

        if (!page || !Array.isArray(page.items) || !Number.isInteger(page.total) || page.total < 0 ||
            page.offset !== offset || page.limit !== 20 || (modal.total !== null && page.total !== modal.total) ||
            page.items.length > page.limit || offset + page.items.length > page.total ||
            (!page.items.length && offset < page.total)) throw new Error('分页数据不完整');
        for (const item of page.items) {
          if (!item || typeof item.id !== 'string' || !item.id || modal.sessionIds.has(item.id)) throw new Error('分页会话重复或缺失');
          modal.sessionIds.add(item.id);
        }
        modal.sessions.push(...page.items.filter(item => item.delivery_available === true).map(deliverySession));
        modal.total = page.total; modal.offset += page.items.length;
      } while (modal.offset < modal.total);
    } catch (_) {
      if (!currentDeliveryRequest(modal, generation)) return;
      modal.sessionsError = '接收会话加载失败或列表已变化，请刷新浏览器后重试。';
    }
    if (currentDeliveryRequest(modal, generation)) { modal.loading = false; showModal(); }
  }

  function renderDeliveryPicker(parent, modal) {
    const draft = modal.draft;
    const selectedId = draft.deliveryId;
    const rows = modal.sessions.slice();
    const selected = modal.selectedSession;
    if (selectedId && selected && selected.id === selectedId && selected.delivery_available === true && !rows.some(item => item.id === selectedId)) rows.unshift(selected);
    const unresolved = (selectedId && !rows.some(item => item.id === selectedId)) ||
      (!draft.deliveryDirty && draft.deliveryKind === 'feishu' && !selectedId);
    const field = selectField('飞书IM接收会话', '', [['', '不投递飞书IM']]);
    if (unresolved) {
      const placeholder = document.createElement('option');
      placeholder.value = '__saved_unavailable__'; placeholder.disabled = true;
      placeholder.textContent = '请选择接收会话';
      field.input.appendChild(placeholder);
    }
    for (const item of rows) {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = `${item.delivery_label || '未命名飞书聊天'}${item.delivery_parent_chat ? ` · ${item.delivery_parent_chat}` : ''}${item.delivery_chat_name ? `（${item.delivery_chat_name}）` : ''}`;
      field.input.appendChild(option);
    }
    field.input.value = unresolved ? '__saved_unavailable__' : selectedId;
    field.input.addEventListener('change', () => {
      const item = rows.find(item => item.id === field.input.value);
      if (field.input.value !== '' && !item) return;
      draft.deliveryId = item ? item.id : '';
      draft.deliveryKind = item ? 'feishu' : 'silent'; draft.deliveryDirty = true;
      modal.selectedSession = item || null; modal.selectedError = ''; modal.error = '';
      showModal();
    });
    parent.appendChild(field.label);
    if (modal.loading) appendText(parent, 'div', '加载接收会话中…', 'loading-state');
    if (modal.sessionsError) appendText(parent, 'div', modal.sessionsError, 'error-state');
    if (modal.selectedError) appendText(parent, 'div', modal.selectedError, 'error-state');
    if (!modal.loading && !modal.sessionsError && rows.length === 0) appendText(parent, 'div', '暂无可用飞书会话，请在飞书中建立会话后刷新浏览器。', 'empty-state');
  }

  function renderTaskForm(parent) {
    const modal = state.modal;
    const task = modal.task;
    const draft = modal.draft;
    const isEdit = Boolean(task);
    const hasOrigin = task && (task.delivery_target === 'origin' || Object.keys(task.origin || {}).length > 0);
    const form = document.createElement('form');
    form.className = 'providers-form scheduled-task-form';
    renderModalHeader(form, isEdit ? '编辑' : '新增');
    if (modal.error) appendText(form, 'div', modal.error, 'error-state');
    const grid = el('div', 'scheduled-modal-grid');
    const bind = (label, type, key, placeholder, parent) => {
      const field = inputField(label, type, draft[key], placeholder);
      field.input.addEventListener('input', () => { draft[key] = field.input.value; });
      parent.appendChild(field.label);
      return field.input;
    };
    bind('名称', 'text', 'name', '例如：每日总结', grid);
    bind('Cron 表达式', 'text', 'cron', '*/30 * * * *', grid);
    bind('时区', 'text', 'timezone', 'Asia/Shanghai', grid);
    bind('允许的工具', 'text', 'tools', '逗号分隔，如 host_terminal', grid);
    if (!hasOrigin) bind('Session ID', 'text', 'session', '留空则后端创建', grid);
    renderDeliveryPicker(grid, modal);
    form.appendChild(grid);
    bind('Prompt', 'textarea', 'prompt', '输入定时执行的提示词', form);
    const actions = el('div', 'providers-form__actions');
    actions.appendChild(button('取消', 'btn', closeModal));
    const submit = button(isEdit ? '保存' : '创建', 'btn btn--primary', () => {});
    submit.type = 'submit'; submit.disabled = Boolean(modal.saving);
    actions.appendChild(submit); form.appendChild(actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (state.modal !== modal || modal.saving) return;
      const payload = { name: draft.name, cron_expression: draft.cron, timezone: draft.timezone,
        prompt: draft.prompt, allowed_tools: draft.tools };
      if (!hasOrigin && draft.session) payload.session_id = draft.session;
      if (draft.deliveryDirty) {
        if (draft.deliveryKind === 'feishu') {
          if (draft.deliveryId) {
            const item = modal.sessions.find(row => row.id === draft.deliveryId) || modal.selectedSession;
            if (!item || item.id !== draft.deliveryId || item.delivery_available !== true || modal.selectedError) {
              modal.error = '请选择可用的飞书接收会话。'; showModal(); return;
            }
            payload.delivery_target = 'feishu'; payload.delivery_conversation_id = draft.deliveryId;
          } else { modal.error = '请选择飞书接收会话。'; showModal(); return; }
        } else if (draft.deliveryKind === 'silent') payload.delivery_target = draft.deliveryKind;
        else { modal.error = '请选择飞书IM接收会话。'; showModal(); return; }
      }
      modal.saving = true; modal.error = ''; showModal();
      try {
        if (isEdit) await api.updateScheduledTask(task.id, payload);
        else await api.createScheduledTask(payload);
        if (state.modal !== modal) return;
        closeModal(); await refresh();
      } catch (error) {
        if (state.modal !== modal) return;
        modal.saving = false; modal.error = error.message || 'scheduled_task_save_failed'; showModal();
      }
    });
    parent.appendChild(form);
  }

  function inputField(labelText, type, value, placeholder) {
    const label = document.createElement('label');
    appendText(label, 'span', labelText);
    const input = type === 'textarea' ? document.createElement('textarea') : document.createElement('input');
    if (type !== 'textarea') input.type = type;
    input.value = value || '';
    input.placeholder = placeholder || '';
    label.appendChild(input);
    return { label, input };
  }

  function selectField(labelText, value, options) {
    const label = document.createElement('label');
    appendText(label, 'span', labelText);
    const input = document.createElement('select');
    options.forEach(([optionValue, optionLabel]) => {
      const option = document.createElement('option');
      option.value = optionValue;
      option.textContent = optionLabel;
      input.appendChild(option);
    });
    input.value = value || options[0][0];
    label.appendChild(input);
    return { label, input };
  }

  function renderDetailPage(root) {
    const wrapper = document.createElement('div');
    wrapper.className = 'scheduled-detail-page';

    const detail = state.detail || {};
    // 详情页头部对齐会话详情页样式：返回链接 + 任务标识导航条，不放置操作按钮。
    const header = document.createElement('div');
    header.className = 'scheduled-detail-header';
    const back = document.createElement('a');
    back.className = 'scheduled-detail-header__back';
    back.href = '/scheduled-tasks';
    back.textContent = '返回';
    back.addEventListener('click', (event) => {
      event.preventDefault();
      backToList();
    });
    const sep = document.createElement('span');
    sep.className = 'scheduled-detail-header__sep';
    sep.textContent = '/';
    const idLabel = document.createElement('span');
    idLabel.className = 'scheduled-detail-header__id';
    idLabel.textContent = detail.task ? text(detail.task.name, detail.task.id) : '加载中...';
    header.append(back, sep, idLabel);
    wrapper.appendChild(header);

    if (!detail || detail.loading) {
      appendText(wrapper, 'div', '加载中...', 'loading-state');
      root.appendChild(wrapper);
      return;
    }
    if (detail.error) {
      appendText(wrapper, 'div', detail.error, 'error-state');
      root.appendChild(wrapper);
      return;
    }

    const task = detail.task;
    const card = document.createElement('div');
    card.className = 'status-panel scheduled-detail';
    const cardHeader = document.createElement('div');
    cardHeader.className = 'panel-header';
    appendText(cardHeader, 'span', text(task.name, task.id));
    card.appendChild(cardHeader);

    const cardBody = document.createElement('div');
    cardBody.className = 'panel-body';
    const grid = document.createElement('div');
    grid.className = 'scheduled-modal-grid';
    [
      ['ID', task.id],
      ['名称', task.name],
      ['Cron', task.cron_expression],
      ['时区', task.timezone],
      ['状态', task.status],
      ['飞书IM接收会话', deliveryName(task.delivery || {})],
      ['当前接收会话', task.delivery && deliveryLocation(task.delivery)],
      ['投递状态', task.delivery && task.delivery.available ? '可用' : '不可用'],
      ['不可用原因', task.delivery && task.delivery.available ? '无' : deliveryReason(task.delivery && task.delivery.reason)],
      ['默认通知会话', task.delivery && task.delivery.home ? (task.delivery.home_configured ? '已配置' : '未配置') : '不适用'],
      ['Session', task.session_id],
      ['下次运行', formatDate(task.next_run_at)],
      ['创建时间', formatDate(task.created_at)],
      ['更新时间', formatDate(task.updated_at)],
    ].forEach(([key, value]) => grid.appendChild(detailItem(key, value)));
    cardBody.appendChild(grid);
    cardBody.appendChild(detailBlock('Prompt', task.prompt));
    cardBody.appendChild(renderExecutions(detail.executions));
    card.appendChild(cardBody);
    wrapper.appendChild(card);

    root.appendChild(wrapper);
  }

  function detailItem(key, value) {
    const node = document.createElement('div');
    node.className = 'scheduled-detail-item';
    appendText(node, 'div', key, 'muted scheduled-small');
    appendText(node, 'div', text(value));
    return node;
  }

  function detailBlock(title, value) {
    const block = document.createElement('div');
    block.className = 'scheduled-detail-block';
    appendText(block, 'h5', title);
    appendText(block, 'pre', truncate(value, 1200));
    return block;
  }

  function renderExecutions(executions) {
    const section = document.createElement('div');
    section.className = 'scheduled-history';
    appendText(section, 'h5', '最近执行历史');
    if (!executions || !executions.length) {
      appendText(section, 'div', '暂无执行记录', 'empty-state');
      return section;
    }
    const table = document.createElement('table');
    table.className = 'document-table scheduled-history-table';
    const head = document.createElement('tr');
    ['状态', '开始', '结束', '输出', '错误', '投递'].forEach((label) => appendText(head, 'th', label));
    const thead = document.createElement('thead');
    thead.appendChild(head);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    executions.forEach((execution) => {
      const row = document.createElement('tr');
      const status = document.createElement('td');
      status.appendChild(badge(text(execution.status), statusKind(execution.status)));
      row.appendChild(status);
      appendText(row, 'td', formatDate(execution.started_at || execution.created_at));
      appendText(row, 'td', formatDate(execution.completed_at));
      appendText(row, 'td', truncate(execution.output, 120));
      appendText(row, 'td', truncate(execution.error, 120));
      appendText(row, 'td', truncate(execution.delivery_status || execution.delivery_error, 80));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    section.appendChild(table);
    return section;
  }

  function renderDeleteConfirm(parent) {
    const wrapper = document.createElement('div');
    wrapper.className = 'providers-form';
    renderModalHeader(wrapper, '删除');
    if (state.modal.error) appendText(wrapper, 'div', state.modal.error, 'error-state');
    appendText(wrapper, 'p', `确认删除“${text(state.modal.task.name, state.modal.task.id)}”？删除后不可恢复。`);
    const actions = document.createElement('div');
    actions.className = 'providers-form__actions';
    const cancelBtn = button('取消', 'btn', closeModal);
    const confirmBtn = button('确认', 'btn btn--danger', async () => {
      try {
        await api.deleteScheduledTask(state.modal.task.id);
        closeModal();
        if (state.view === 'detail' && state.detail && state.detail.task && state.detail.task.id === state.modal.task.id) {
          backToList();
        }
        await refresh();
      } catch (error) {
        state.modal.error = error.message || 'scheduled_task_delete_failed';
        showModal();
      }
    });
    actions.append(cancelBtn, confirmBtn);
    wrapper.appendChild(actions);
    parent.appendChild(wrapper);
    requestAnimationFrame(() => cancelBtn.focus());
  }

  function renderRunConfirm(parent) {
    const wrapper = document.createElement('div');
    wrapper.className = 'providers-form';
    renderModalHeader(wrapper, '执行任务');
    if (state.modal.error) appendText(wrapper, 'div', state.modal.error, 'error-state');
    appendText(wrapper, 'p', `确认立即执行“${text(state.modal.task.name, state.modal.task.id)}”？执行请求受理后，可在执行历史中查看结果。`);
    const actions = document.createElement('div');
    actions.className = 'providers-form__actions';
    const cancelBtn = button('取消', 'btn', closeModal);
    const confirmBtn = button('确认执行', 'btn btn--primary', () => runTask(state.modal.task));
    actions.append(cancelBtn, confirmBtn);
    wrapper.appendChild(actions);
    parent.appendChild(wrapper);
    requestAnimationFrame(() => cancelBtn.focus());
  }

  // 确认执行：关闭确认弹框后触发执行，成功后刷新列表，失败在页内提示错误。
  async function runTask(task) {
    closeModal();
    try {
      await api.runScheduledTask(task.id);
      refresh();
    } catch (error) {
      showError(error.message || 'scheduled_task_run_failed');
    }
  }

  async function toggleTask(task) {
    try {
      if (task.status === 'paused' || task.enabled === false) await api.resumeScheduledTask(task.id);
      else await api.pauseScheduledTask(task.id);
      await refresh();
    } catch (error) {
      showError(error.message || 'scheduled_task_toggle_failed');
    }
  }

  function init() {
    window.addEventListener('popstate', handlePathChange);
    const newBtn = document.getElementById('scheduled-task-new');
    if (newBtn) newBtn.addEventListener('click', () => openTaskForm());
    refresh().then(() => {
      const pendingId = pendingTaskIdFromPath();
      if (pendingId) openTaskDetail(pendingId);
    });
  }

  function handlePathChange() {
    const pendingId = pendingTaskIdFromPath();
    if (pendingId) {
      const current = state.detail && state.detail.task ? state.detail.task.id : null;
      if (current !== pendingId) {
        openTaskDetail(pendingId);
      }
    } else if (state.view === 'detail') {
      state.view = 'list';
      state.detail = null;
      render();
    }
  }

  namespace.scheduledTasks = { init, refresh, openTaskForm, openTaskDetail, confirmDeleteTask };
  namespace['scheduled-tasks'] = namespace.scheduledTasks;
  global.NAGENT = namespace;
}(window));
