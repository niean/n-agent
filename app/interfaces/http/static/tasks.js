/* tasks.js -- NAGENT.tasks Kanban board (Manus-aligned 7-state, 5 swimlanes).
 * Board API returns 5 swimlanes (queued/running/waiting_approval/
 * failed+expired / succeeded+cancelled). textContent-only rendering (no
 * innerHTML/insertAdjacentHTML) per the frontend security contract.
 */
(function (global) {
  const namespace = global.NAGENT || (global.NAGENT = {});
  const api = namespace.api;
  const ui = namespace.ui;
  const el = ui.el;
  const modal = namespace.modal || {};

  // Terminal task statuses (failed/expired/succeeded/cancelled) expose
  // deletion only from the task detail modal. RUNNING and in-flight tasks use
  // cancel/retry instead. Mirrors task_service.delete_task which rejects
  // only RUNNING.
  const DELETABLE_STATUSES = ['failed', 'expired', 'succeeded', 'cancelled'];

  let state = { board: { columns: [] } };
  let ws = null;

  function init() {
    if (!api || !api.task) return;
    renderShell();
    const newBtn = document.getElementById('task-new');
    if (newBtn) newBtn.addEventListener('click', openCreateModal);
    refresh();
  }

  function renderShell() {
    const root = document.getElementById('tasks-board');
    if (!root) return;
    ui.clear(root);

    const board = el('div', 'kanban-board');
    board.id = 'kanban-board-root';
    root.appendChild(board);

  }

  function renderBoard() {
    const board = document.getElementById('kanban-board-root');
    if (!board) return;
    ui.clear(board);

    const columns = state.board.columns || [];
    // Adaptive swimlane count: grid track count follows the actual number of
    // swimlanes so they fill the horizontal width (and reflow when the left
    // sidebar collapses/expands, since the board width tracks main-content).
    if (board.style && board.style.setProperty) {
      board.style.setProperty('--kanban-column-count', String(columns.length || 5));
    }
    columns.forEach((col) => {
      const colEl = el('div', 'kanban-column');
      colEl.dataset.lane = col.id;
      const header = el('div', 'kanban-column__header');
      header.textContent = (col.label || col.id) + ' (' + (col.total != null ? col.total : (col.cards || []).length) + ')';
      colEl.appendChild(header);

      const list = el('div', 'kanban-column__list');
      list.addEventListener('dragover', (e) => { e.preventDefault(); });
      list.addEventListener('drop', (e) => {
        e.preventDefault();
        const id = e.dataTransfer.getData('text/plain');
        if (id) moveTask(id, col.id);
      });

      const items = col.cards || [];
      items.forEach((t) => list.appendChild(renderCard(t)));
      colEl.appendChild(list);
      board.appendChild(colEl);
    });
  }

  function renderCard(t) {
    const card = el('div', 'kanban-card');
    card.draggable = true;
    card.dataset.id = t.id;
    card.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', t.id); });
    card.addEventListener('click', () => openDetail(t.id));

    const title = el('div', 'kanban-card__title');
    title.textContent = t.title || t.id;
    card.appendChild(title);

    const meta = el('div', 'kanban-card__meta');
    const idSpan = el('span', 'kanban-card__id');
    idSpan.textContent = t.id;
    meta.appendChild(idSpan);
    if (t.priority) {
      const pri = el('span', 'kanban-card__priority');
      pri.textContent = 'P' + t.priority;
      meta.appendChild(pri);
    }
    if (t.goal_mode) {
      const g = el('span', 'kanban-card__badge');
      g.textContent = 'goal';
      meta.appendChild(g);
    }
    if (t.is_archived) {
      const a = el('span', 'kanban-card__badge');
      a.textContent = '归档';
      meta.appendChild(a);
    }
    card.appendChild(meta);
    return card;
  }

  async function refresh() {
    try {
      const board = await api.task.board();
      state.board = board || { columns: [] };
      renderBoard();
      connectWs();
    } catch (e) {
      const root = document.getElementById('tasks-board');
      if (root) { const msg = el('div', 'tasks-error'); msg.textContent = '加载看板失败：' + (e && e.message ? e.message : e); root.appendChild(msg); }
    }
  }

  function connectWs() {
    if (ws) return;
    try {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      ws = new WebSocket(proto + '//' + window.location.host + '/chat/tasks/events?since=0');
      ws.onmessage = (ev) => {
        try { const msg = JSON.parse(ev.data); if (msg && msg.kind) refresh(); } catch (_) {}
      };
      ws.onclose = () => { ws = null; };
      ws.onerror = () => { try { ws.close(); } catch (_) {} };
    } catch (_) { ws = null; }
  }

  async function moveTask(id, targetLane) {
    try {
      // Drag routes to a dedicated action API (status is not PATCH-able).
      if (targetLane === 'succeeded_cancelled') {
        await api.task.cancel(id);
      } else if (targetLane === 'queued') {
        await api.task.retry(id);
      } else {
        await modal.alert(
          '该泳道不支持拖拽迁移（请使用详情中的批准、拒绝、取消或重试操作）。',
          { title: '无法迁移任务' },
        );
        return;
      }
      refresh();
    } catch (e) {
      await modal.alert(
        '操作失败（可能状态冲突）：' + (e && e.message ? e.message : e),
        { title: '任务迁移失败' },
      );
      refresh();
    }
  }

  async function showTaskActionError(message) {
    await modal.alert(message, { title: '任务操作失败' });
  }

  // Toggle disabled state on all buttons in an approval action row. Used to
  // prevent duplicate submissions while an approve/reject request is in-flight.
  function setApprovalBusy(container, busy) {
    Array.prototype.forEach.call(container.children, (btn) => { btn.disabled = busy; });
  }

  // Delete a terminal task (failed/expired/succeeded/cancelled). Backend
  // delete_task rejects only RUNNING, but the board exposes delete only on
  // the completed swimlanes to avoid clobbering in-flight work.
  async function removeTask(id) {
    try {
      const confirmed = modal.confirm
        ? await modal.confirm('确认删除任务 ' + id + '？删除后无法恢复。')
        : window.confirm('确认删除任务 ' + id + '？删除后无法恢复。');
      if (!confirmed) return;
      await api.task.remove(id);
      closeDetailModal();
      await refresh();
    } catch (e) {
      await showTaskActionError('删除失败（可能状态冲突）：' + (e && e.message ? e.message : e));
      refresh();
    }
  }

  // ---- create modal ----
  function closeCreateModal() {
    const modal = document.getElementById('tasks-create-modal');
    if (modal) modal.remove();
    document.removeEventListener('keydown', onCreateModalKeydown);
  }

  function onCreateModalKeydown(event) {
    if (event.key === 'Escape') closeCreateModal();
  }

  function field(form, name, labelText, options) {
    options = options || {};
    const label = el('label', '');
    label.textContent = labelText;
    let input;
    if (options.type === 'textarea') {
      input = el('textarea', '');
    } else {
      input = el('input', '');
      input.type = options.type || 'text';
    }
    input.name = name;
    input.id = 'tasks-create-' + name;
    if (options.value != null) input.value = options.value;
    if (options.placeholder) input.placeholder = options.placeholder;
    if (options.min != null) input.min = String(options.min);
    if (options.required) input.required = true;
    label.appendChild(input);
    form.appendChild(label);
    return input;
  }

  function checkbox(form, name, labelText, checked) {
    const label = el('label', '');
    const input = el('input', '');
    input.type = 'checkbox';
    input.name = name;
    input.id = 'tasks-create-' + name;
    input.checked = !!checked;
    const span = el('span', '');
    span.textContent = labelText;
    label.append(input, span);
    form.appendChild(label);
    return input;
  }

  function openCreateModal() {
    closeCreateModal();
    const backdrop = el('div', 'modal-backdrop');
    backdrop.id = 'tasks-create-modal';
    const dialog = el('section', 'modal-dialog tasks-modal');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = el('form', 'providers-form tasks-form');
    const header = el('div', 'modal-header');
    const titleEl = el('h4', '');
    titleEl.textContent = '新增任务';
    const closeBtn = el('button', 'modal-close');
    closeBtn.type = 'button';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭');
    closeBtn.addEventListener('click', closeCreateModal);
    header.append(titleEl, closeBtn);
    form.appendChild(header);

    const titleInput = field(form, 'title', '标题', { required: true, placeholder: '任务标题（必填）' });
    const goalInput = field(form, 'goal', '目标', { type: 'textarea', placeholder: '描述任务要达成的目标' });
    const priorityInput = field(form, 'priority', '优先级', { type: 'number', min: 0, value: '0' });
    const goalModeInput = checkbox(form, 'goal_mode', '自主目标驱动执行（goal_mode）', false);

    const hint = el('div', 'providers-form__hint muted');
    form.appendChild(hint);

    const actions = el('div', 'providers-form__actions');
    const cancelBtn = el('button', 'btn');
    cancelBtn.type = 'button';
    cancelBtn.textContent = '取消';
    cancelBtn.addEventListener('click', closeCreateModal);
    const submitBtn = el('button', 'btn btn--primary');
    submitBtn.type = 'submit';
    submitBtn.textContent = '创建';
    actions.append(cancelBtn, submitBtn);
    form.appendChild(actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const titleVal = String(titleInput.value || '').trim();
      if (!titleVal) {
        hint.className = 'providers-form__hint badge badge--danger';
        hint.textContent = '请填写标题';
        return;
      }
      const goalVal = String(goalInput.value || '').trim();
      const priorityVal = String(priorityInput.value || '').trim();
      const payload = { title: titleVal, goal_mode: !!goalModeInput.checked };
      if (goalVal) payload.body = goalVal;
      if (priorityVal) payload.priority = Number(priorityVal);
      submitBtn.disabled = true;
      hint.className = 'providers-form__hint muted';
      hint.textContent = '';
      try {
        await api.task.create(payload);
        closeCreateModal();
        await refresh();
      } catch (e) {
        hint.className = 'providers-form__hint badge badge--danger';
        hint.textContent = '创建失败：' + (e && e.message ? e.message : e);
        submitBtn.disabled = false;
      }
    });

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeCreateModal();
    });
    document.body.appendChild(backdrop);
    document.addEventListener('keydown', onCreateModalKeydown);
    if (titleInput.focus) titleInput.focus();
  }

  function closeDetailModal() {
    const modal = document.getElementById('tasks-detail-modal');
    if (modal) modal.remove();
    document.removeEventListener('keydown', onDetailModalKeydown);
  }

  function onDetailModalKeydown(event) {
    if (event.key === 'Escape') closeDetailModal();
  }

  function detailHeader(form, title) {
    const header = el('div', 'modal-header');
    const titleEl = el('h4', '');
    titleEl.textContent = title;
    const closeBtn = el('button', 'modal-close');
    closeBtn.type = 'button';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭');
    closeBtn.addEventListener('click', closeDetailModal);
    header.append(titleEl, closeBtn);
    form.appendChild(header);
  }

  function detailValue(value) {
    if (value === null || value === undefined || value === '') return '-';
    if (value === true) return '是';
    if (value === false) return '否';
    if (Array.isArray(value)) return value.length ? value.join(', ') : '-';
    return String(value);
  }

  function formatTaskTime(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    // Render task timestamps in UTC+8 regardless of the browser timezone.
    const utc8 = new Date(date.getTime() + 8 * 3600 * 1000);
    const pad = (number) => String(number).padStart(2, '0');
    return `${utc8.getUTCFullYear()}-${pad(utc8.getUTCMonth() + 1)}-${pad(utc8.getUTCDate())} ${pad(utc8.getUTCHours())}:${pad(utc8.getUTCMinutes())}:${pad(utc8.getUTCSeconds())}`;
  }

  function appendDetailSection(form, label, value) {
    const sectionLabel = el('div', 'tasks-detail__section-label');
    sectionLabel.textContent = label;
    const body = el('div', 'tasks-detail__body');
    body.textContent = detailValue(value);
    form.append(sectionLabel, body);
  }

  function appendDetailList(form, label, items, renderItem) {
    const sectionLabel = el('div', 'tasks-detail__section-label');
    sectionLabel.textContent = label;
    const body = el('div', 'tasks-detail__body');
    if (!items || !items.length) {
      body.textContent = '暂无';
    } else {
      items.forEach((item) => {
        const row = el('div', 'tasks-detail__list-item');
        row.textContent = renderItem(item);
        body.appendChild(row);
      });
    }
    form.append(sectionLabel, body);
  }

  async function openDetail(id) {
    closeDetailModal();
    const backdrop = el('div', 'modal-backdrop');
    backdrop.id = 'tasks-detail-modal';
    const dialog = el('section', 'modal-dialog tasks-modal');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = el('div', 'providers-form tasks-detail-modal');
    detailHeader(form, '任务详情');
    const loading = el('div', 'muted loading-state');
    loading.textContent = '加载中...';
    form.appendChild(loading);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeDetailModal();
    });
    document.body.appendChild(backdrop);
    document.addEventListener('keydown', onDetailModalKeydown);

    try {
      const response = await api.task.get(id);
      const detail = response && response.task ? response.task : response;
      if (document.getElementById('tasks-detail-modal') !== backdrop) return;
      ui.clear(form);
      detailHeader(form, detail.title || detail.id);

      const grid = el('div', 'tasks-detail__grid');
      const fields = [
        ['ID', detail.id],
        ['状态', detail.status],
        ['优先级', detail.priority != null ? String(detail.priority) : '-'],
        ['创建人', detail.created_by],
        ['看板', detail.board],
        ['版本', detail.version != null ? String(detail.version) : '-'],
        ['目标模式', detail.goal_mode ? '是' : '否'],
        ['目标最大轮数', detail.goal_max_turns],
        ['已归档', detail.is_archived ? '是' : '否'],
        ['创建时间', formatTaskTime(detail.created_at)],
        ['更新时间', formatTaskTime(detail.updated_at)],
        ['计划执行', formatTaskTime(detail.scheduled_at)],
        ['开始时间', formatTaskTime(detail.started_at)],
        ['完成时间', formatTaskTime(detail.completed_at)],
        ['当前执行', detail.current_run_id],
        ['最近心跳', formatTaskTime(detail.last_heartbeat_at)],
        ['失败次数 / 最大重试', `${detail.consecutive_failures || 0} / ${detail.max_retries || 0}`],
      ];
      fields.forEach(([k, v]) => {
        const row = el('div', 'tasks-detail__row');
        const kl = el('span', 'tasks-detail__k'); kl.textContent = k + '：';
        const vl = el('span', 'tasks-detail__v'); vl.textContent = detailValue(v);
        row.appendChild(kl); row.appendChild(vl); grid.appendChild(row);
      });
      form.appendChild(grid);

      appendDetailSection(form, '描述', detail.body);
      appendDetailSection(form, '执行配置', [
        `工作区：${detailValue(detail.workspace_kind)}`,
        `路径：${detailValue(detail.workspace_path)}`,
        `模型：${detailValue(detail.model_override)}`,
        `最长执行：${detail.max_runtime_seconds == null ? '-' : `${detail.max_runtime_seconds} 秒`}`,
        `技能：${detailValue(detail.skills)}`,
        `允许工具：${detailValue(detail.allowed_tools)}`,
      ].join('\n'));
      appendDetailSection(form, '关联会话', [
        `来源会话：${detailValue(detail.origin_session_id)}`,
        `执行会话：${detailValue(detail.execution_session_id)}`,
      ].join('\n'));

      if (detail.result) {
        appendDetailSection(form, '结果', detail.result);
      }
      if (detail.last_failure_error) {
        appendDetailSection(form, '最近失败原因', detail.last_failure_error);
      }
      if (response && response.task) {
        appendDetailList(form, '执行记录', response.runs, (run) => [
          `#${detailValue(run.id)}`, detailValue(run.status), detailValue(run.outcome),
          formatTaskTime(run.started_at), formatTaskTime(run.ended_at), run.summary || run.error || '',
        ].filter(Boolean).join(' | '));
        appendDetailList(form, '事件记录', response.events, (event) => [
          formatTaskTime(event.created_at), detailValue(event.kind),
          event.run_id == null ? '' : `执行 #${event.run_id}`,
          event.payload && Object.keys(event.payload).length ? JSON.stringify(event.payload) : '',
        ].filter(Boolean).join(' | '));
        appendDetailList(form, '评论', response.comments, (comment) => [
          formatTaskTime(comment.created_at), detailValue(comment.author), detailValue(comment.body),
        ].join(' | '));
        appendDetailList(form, '附件', response.attachments, (attachment) => [
          detailValue(attachment.filename), detailValue(attachment.content_type),
          attachment.size == null ? '' : `${attachment.size} B`,
        ].filter(Boolean).join(' | '));
        if (response.worker_context) appendDetailSection(form, '执行上下文', response.worker_context);
      }

      // Intent approval: if waiting_approval, show approve/reject actions.
      if (detail.status === 'waiting_approval') {
        const approvalLabel = el('div', 'tasks-detail__section-label');
        approvalLabel.textContent = '待审批提案';
        form.appendChild(approvalLabel);
        const proposalDiv = el('div', 'tasks-detail__body');
        proposalDiv.textContent = detail.latest_proposal || '（worker 提出了需要审批的修改）';
        form.appendChild(proposalDiv);

        const noteLabel = el('label', 'tasks-approval-note');
        noteLabel.textContent = '审批意见/指示/拒绝理由（可选）';
        const noteInput = el('textarea', '');
        noteInput.id = 'tasks-approval-note';
        noteInput.maxLength = 2000;
        noteLabel.appendChild(noteInput);
        form.appendChild(noteLabel);

        const approvalActions = el('div', 'providers-form__actions');
        const approveBtn = el('button', 'btn btn--primary');
        approveBtn.type = 'button'; approveBtn.textContent = '批准';
        approveBtn.addEventListener('click', async () => {
          const note = noteInput.value.trim() || null;
          setApprovalBusy(approvalActions, true);
          try { await api.task.approve(id, note); closeDetailModal(); await refresh(); }
          catch (e) { setApprovalBusy(approvalActions, false); await showTaskActionError('批准失败：' + (e && e.message ? e.message : e)); }
        });
        const rejectBtn = el('button', 'btn');
        rejectBtn.type = 'button'; rejectBtn.textContent = '拒绝';
        rejectBtn.addEventListener('click', async () => {
          const note = noteInput.value.trim() || null;
          setApprovalBusy(approvalActions, true);
          try { await api.task.reject(id, note); closeDetailModal(); await refresh(); }
          catch (e) { setApprovalBusy(approvalActions, false); await showTaskActionError('拒绝失败：' + (e && e.message ? e.message : e)); }
        });
        approvalActions.append(rejectBtn, approveBtn);
        form.appendChild(approvalActions);
      }

      // Terminal-ish actions: cancel / retry via buttons.
      const statusActions = el('div', 'providers-form__actions');
      if (['queued', 'running', 'waiting_approval', 'failed'].indexOf(detail.status) !== -1) {
        const cancelBtn = el('button', 'btn');
        cancelBtn.type = 'button'; cancelBtn.textContent = '取消任务';
        cancelBtn.addEventListener('click', async () => {
          try { await api.task.cancel(id); closeDetailModal(); await refresh(); }
          catch (e) { await showTaskActionError('取消失败：' + (e && e.message ? e.message : e)); }
        });
        statusActions.appendChild(cancelBtn);
      }
      if (['failed', 'expired'].indexOf(detail.status) !== -1) {
        const retryBtn = el('button', 'btn');
        retryBtn.type = 'button'; retryBtn.textContent = '重试';
        retryBtn.addEventListener('click', async () => {
          try { await api.task.retry(id); closeDetailModal(); await refresh(); }
          catch (e) { await showTaskActionError('重试失败：' + (e && e.message ? e.message : e)); }
        });
        statusActions.appendChild(retryBtn);
      }
      if (DELETABLE_STATUSES.indexOf(detail.status) !== -1) {
        const deleteBtn = el('button', 'btn btn--danger');
        deleteBtn.type = 'button'; deleteBtn.textContent = '删除任务';
        deleteBtn.addEventListener('click', () => { removeTask(id); });
        statusActions.appendChild(deleteBtn);
      }
      if (statusActions.children.length) form.appendChild(statusActions);
    } catch (e) {
      if (document.getElementById('tasks-detail-modal') !== backdrop) return;
      ui.clear(form);
      detailHeader(form, '任务详情');
      const err = el('div', 'tasks-error'); err.textContent = '加载详情失败：' + (e && e.message ? e.message : e);
      form.appendChild(err);
    }
  }

  namespace.tasks = { init, refresh };
}(window));
