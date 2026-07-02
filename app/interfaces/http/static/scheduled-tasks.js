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
    return date.toLocaleString();
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

  function button(label, className, onClick) {
    const node = document.createElement('button');
    node.type = 'button';
    node.className = className || 'btn';
    node.textContent = label;
    node.addEventListener('click', onClick);
    return node;
  }

  // 新标签页打开的链接按钮，用于详情等需要独立 URL 的页面。
  function linkButton(label, className, href) {
    const node = document.createElement('a');
    node.className = className || 'btn';
    node.href = href;
    node.target = '_blank';
    node.rel = 'noopener';
    node.textContent = label;
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
    return document.getElementById('scheduled-tasks-list');
  }

  function showError(message) {
    state.error = message;
    render();
  }

  async function refresh() {
    const root = getRoot();
    if (!root) return;
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
    const root = getRoot();
    if (!root) return;
    clear(root);
    if (state.view === 'detail') {
      renderDetailPage(root);
      renderModal(root);
      return;
    }
    root.appendChild(renderHeader());
    if (state.error) appendText(root, 'div', state.error, 'error-state');
    if (state.loading) {
      appendText(root, 'div', '加载中...', 'loading-state');
      renderModal(root);
      return;
    }
    root.appendChild(renderStats());
    root.appendChild(renderTable());
    renderModal(root);
  }

  function renderHeader() {
    const header = document.createElement('div');
    header.className = 'scheduled-page-header';
    const copy = document.createElement('div');
    appendText(copy, 'h3', '任务');
    appendText(copy, 'p', '管理 Dashboard 任务，并查看最近执行结果。', 'muted');
    header.appendChild(copy);

    const actions = document.createElement('div');
    actions.className = 'panel-actions';
    actions.appendChild(button('刷新', 'btn', refresh));
    actions.appendChild(button('新建任务', 'btn btn--primary', () => openTaskForm()));
    header.appendChild(actions);
    return header;
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
    const panel = document.createElement('div');
    panel.className = 'status-panel scheduled-table-panel';
    const header = document.createElement('div');
    header.className = 'panel-header';
    appendText(header, 'span', '任务列表');
    panel.appendChild(header);

    const body = document.createElement('div');
    body.className = 'panel-body scheduled-table-wrap';
    if (!state.tasks.length) {
      appendText(body, 'div', '暂无任务，点击“新建任务”开始。', 'empty-state');
      panel.appendChild(body);
      return panel;
    }

    const table = document.createElement('table');
    table.className = 'document-table scheduled-table';
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    ['任务', '调度', '状态', '下次运行', '最近结果', '操作'].forEach((label) => appendText(headRow, 'th', label));
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement('tbody');
    state.tasks.forEach((task) => tbody.appendChild(renderTaskRow(task)));
    table.appendChild(tbody);
    body.appendChild(table);
    panel.appendChild(body);
    return panel;
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
    statusCell.appendChild(badge(text(task.status), statusKind(task.status)));
    if (task.enabled === false) statusCell.appendChild(badge('disabled', 'warning'));
    row.appendChild(statusCell);

    appendText(row, 'td', formatDate(task.next_run_at));

    const lastCell = document.createElement('td');
    lastCell.appendChild(badge(text(task.last_status), statusKind(task.last_status)));
    row.appendChild(lastCell);

    const actionCell = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'row-actions';
    actions.appendChild(linkButton('详情', 'btn', `/scheduled-tasks/${encodeURIComponent(task.id)}`));
    actions.appendChild(button('编辑', 'btn', () => openTaskForm(task)));
    actions.appendChild(button('立即运行', 'btn', () => runTask(task)));
    actions.appendChild(button(task.status === 'paused' || task.enabled === false ? '恢复' : '暂停', 'btn', () => toggleTask(task)));
    actions.appendChild(button('删除', 'btn', () => confirmDeleteTask(task)));
    actionCell.appendChild(actions);
    row.appendChild(actionCell);
    return row;
  }

  function openTaskForm(task) {
    state.modal = { type: 'form', task: task || null, error: '' };
    render();
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

  function pendingTaskIdFromPath() {
    const match = window.location.pathname.match(/^\/scheduled-tasks\/([^/]+)$/);
    if (!match) return null;
    try { return decodeURIComponent(match[1]); } catch (_) { return null; }
  }

  function confirmDeleteTask(task) {
    state.modal = { type: 'delete', task, error: '' };
    render();
  }

  function closeModal() {
    state.modal = null;
    render();
  }

  function renderModal(root) {
    if (!state.modal) return;
    const backdrop = document.createElement('div');
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('div');
    dialog.className = 'modal-dialog scheduled-modal';
    if (state.modal.type === 'form') renderTaskForm(dialog);
    if (state.modal.type === 'delete') renderDeleteConfirm(dialog);
    if (state.modal.type === 'run_result') renderRunResult(dialog);
    backdrop.appendChild(dialog);
    root.appendChild(backdrop);
  }

  function renderModalHeader(parent, title) {
    const header = document.createElement('div');
    header.className = 'modal-header';
    appendText(header, 'h4', title);
    header.appendChild(button('×', 'modal-close', closeModal));
    parent.appendChild(header);
  }

  function renderTaskForm(parent) {
    const task = state.modal.task;
    const isEdit = Boolean(task);
    const isOrigin = task && task.delivery_target === 'origin';
    const form = document.createElement('form');
    form.className = 'providers-form scheduled-task-form';
    renderModalHeader(form, isEdit ? '编辑任务' : '新建任务');
    if (state.modal.error) appendText(form, 'div', state.modal.error, 'error-state');

    const grid = document.createElement('div');
    grid.className = 'scheduled-modal-grid';
    const name = inputField('名称', 'text', task ? task.name : '', '例如：每日总结');
    const cron = inputField('Cron 表达式', 'text', task ? task.cron_expression : '', '*/30 * * * *');
    const timezone = inputField('时区', 'text', task ? task.timezone : 'Asia/Shanghai', 'Asia/Shanghai');
    grid.appendChild(name.label);
    grid.appendChild(cron.label);
    grid.appendChild(timezone.label);

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
      notice.textContent = 'Origin 任务的投递目标和会话由 Gateway 管理，Dashboard 仅允许编辑名称、调度、时区和 Prompt。';
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
        render();
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

    const header = document.createElement('div');
    header.className = 'scheduled-page-header';
    const copy = document.createElement('div');
    appendText(copy, 'h3', '任务详情');
    appendText(copy, 'p', '查看任务配置与最近执行历史。', 'muted');
    header.appendChild(copy);

    const actions = document.createElement('div');
    actions.className = 'panel-actions';
    actions.appendChild(button('← 返回列表', 'btn', backToList));
    const detail = state.detail || {};
    if (detail.task) {
      actions.appendChild(button('刷新', 'btn', () => openTaskDetail(detail.task.id)));
      actions.appendChild(button('立即运行', 'btn', () => runTask(detail.task)));
    }
    header.appendChild(actions);
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
    renderModalHeader(wrapper, '删除任务');
    if (state.modal.error) appendText(wrapper, 'div', state.modal.error, 'error-state');
    appendText(wrapper, 'p', `确认删除“${text(state.modal.task.name, state.modal.task.id)}”？删除后不可恢复。`);
    const actions = document.createElement('div');
    actions.className = 'providers-form__actions';
    actions.appendChild(button('取消', 'btn', closeModal));
    actions.appendChild(button('确认删除', 'btn btn--danger', async () => {
      try {
        await api.deleteScheduledTask(state.modal.task.id);
        closeModal();
        if (state.view === 'detail' && state.detail && state.detail.task && state.detail.task.id === state.modal.task.id) {
          backToList();
        }
        await refresh();
      } catch (error) {
        state.modal.error = error.message || 'scheduled_task_delete_failed';
        render();
      }
    }));
    wrapper.appendChild(actions);
    parent.appendChild(wrapper);
  }

  const RUN_RESULT_MESSAGES = {
    triggered: { title: '任务已触发', detail: '任务执行请求已受理，正在后台运行。可在执行历史中查看结果。', kind: 'info' },
    not_claimed: { title: '任务正在运行', detail: '任务已被其他实例锁定或正在运行中，请稍后重试。', kind: 'warning' },
  };

  function renderRunResult(parent) {
    const { task, result } = state.modal;
    const status = String(result.status || '').toLowerCase();
    const message = RUN_RESULT_MESSAGES[status] || { title: '任务已提交', detail: '任务执行请求已受理。', kind: 'info' };
    const wrapper = document.createElement('div');
    wrapper.className = 'providers-form';
    renderModalHeader(wrapper, message.title);
    appendText(wrapper, 'p', `任务：${text(task.name, task.id)}`);
    appendText(wrapper, 'p', message.detail);
    const actions = document.createElement('div');
    actions.className = 'providers-form__actions';
    if (state.view === 'detail' && state.detail && state.detail.task && state.detail.task.id === task.id) {
      // 已在详情页：关闭弹框并刷新本页执行历史
      actions.appendChild(button('刷新执行历史', 'btn btn--primary', async () => {
        closeModal();
        await openTaskDetail(task.id);
      }));
    } else {
      // 在列表页：新标签页打开任务详情
      actions.appendChild(linkButton('查看执行历史', 'btn btn--primary', `/scheduled-tasks/${encodeURIComponent(task.id)}`));
    }
    actions.appendChild(button('关闭', 'btn', closeModal));
    wrapper.appendChild(actions);
    parent.appendChild(wrapper);
  }

  async function runTask(task) {
    try {
      const result = await api.runScheduledTask(task.id);
      state.modal = { type: 'run_result', task, result: result || {} };
      render();
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
