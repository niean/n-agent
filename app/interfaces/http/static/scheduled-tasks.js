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
    state.modal = { type: 'form', task: task || null, error: '' };
    showModal();
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

  function renderTaskForm(parent) {
    const task = state.modal.task;
    const isEdit = Boolean(task);
    const isOrigin = task && task.delivery_target === 'origin';
    const form = document.createElement('form');
    form.className = 'providers-form scheduled-task-form';
    renderModalHeader(form, isEdit ? '编辑' : '新增');
    if (state.modal.error) appendText(form, 'div', state.modal.error, 'error-state');

    const grid = document.createElement('div');
    grid.className = 'scheduled-modal-grid';
    const name = inputField('名称', 'text', task ? task.name : '', '例如：每日总结');
    const cron = inputField('Cron 表达式', 'text', task ? task.cron_expression : '', '*/30 * * * *');
    const timezone = inputField('时区', 'text', task ? task.timezone : 'Asia/Shanghai', 'Asia/Shanghai');
    const tools = inputField('允许的工具', 'text', task ? (task.allowed_tools || []).join(',') : '', '逗号分隔，如 host_terminal');
    grid.appendChild(name.label);
    grid.appendChild(cron.label);
    grid.appendChild(timezone.label);
    grid.appendChild(tools.label);

    if (!isOrigin) {
      const target = selectField('投递目标', task ? task.delivery_target : 'dashboard', [['dashboard', 'Dashboard'], ['silent', 'Silent']]);
      const session = inputField('Session ID', 'text', task ? task.session_id : '', '留空则后端创建');
      grid.appendChild(target.label);
      grid.appendChild(session.label);
      form._target = target.input;
      form._session = session.input;
    } else {
      const notice = document.createElement('div');
      notice.className = 'scheduled-origin-notice';
      notice.textContent = 'Origin 任务的投递目标和会话由 Gateway 管理，Dashboard 仅允许编辑名称、调度、时区、允许的工具和 Prompt。';
      grid.appendChild(notice);
    }
    form.appendChild(grid);

    const prompt = inputField('Prompt', 'textarea', task ? task.prompt : '', '输入定时执行的提示词');
    form.appendChild(prompt.label);

    const actions = document.createElement('div');
    actions.className = 'providers-form__actions';
    actions.appendChild(button('取消', 'btn', closeModal));
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'btn btn--primary';
    submit.textContent = isEdit ? '保存' : '创建';
    actions.appendChild(submit);
    form.appendChild(actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const payload = {
        name: name.input.value,
        cron_expression: cron.input.value,
        timezone: timezone.input.value,
        prompt: prompt.input.value,
        allowed_tools: tools.input.value,
      };
      if (!isOrigin) {
        payload.delivery_target = form._target.value;
        if (form._session.value) payload.session_id = form._session.value;
      }
      try {
        if (isEdit) await api.updateScheduledTask(task.id, payload);
        else await api.createScheduledTask(payload);
        closeModal();
        await refresh();
      } catch (error) {
        state.modal.error = error.message || 'scheduled_task_save_failed';
        showModal();
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
      ['投递目标', task.delivery_target],
      ['Session', task.session_id],
      ['下次运行', formatDate(task.next_run_at)],
      ['创建时间', formatDate(task.created_at)],
      ['更新时间', formatDate(task.updated_at)],
    ].forEach(([key, value]) => grid.appendChild(detailItem(key, value)));
    cardBody.appendChild(grid);
    cardBody.appendChild(detailBlock('Prompt', task.prompt));
    cardBody.appendChild(detailBlock('Origin', JSON.stringify(task.origin || {}, null, 2)));
    cardBody.appendChild(detailBlock('Delivery Context', JSON.stringify(task.delivery_context || {}, null, 2)));
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
