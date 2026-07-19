/* tasks.js -- NAGENT.tasks Kanban board (T21).
 * Renders the Task board by status column. textContent-only rendering (no
 * innerHTML/insertAdjacentHTML) per the frontend security contract.
 */
(function (global) {
  const namespace = global.NAGENT || (global.NAGENT = {});
  const api = namespace.api;
  const ui = namespace.ui;
  const el = ui.el;

  const COLUMNS = [
    { key: 'triage', label: '待评估' },
    { key: 'todo', label: '待办' },
    { key: 'scheduled', label: '已排期' },
    { key: 'ready', label: '就绪' },
    { key: 'running', label: '运行中' },
    { key: 'blocked', label: '阻塞' },
    { key: 'review', label: '审阅' },
    { key: 'done', label: '完成' },
  ];
  let state = { board: { columns: [], archived: false, assignees: [] }, detail: null };
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

    const detail = el('div', 'tasks-detail-drawer');
    detail.id = 'tasks-detail-drawer';
    detail.hidden = true;
    root.appendChild(detail);
  }

  function renderBoard() {
    const board = document.getElementById('kanban-board-root');
    if (!board) return;
    ui.clear(board);

    const cols = COLUMNS;

    // Backend /chat/tasks/board returns `columns` as an ARRAY of
    // {status, cards, total}. Index by status so each Kanban column can look
    // up its cards and total. Treating columns as a dict keyed by status yields
    // undefined and renders zero cards.
    const cardsByStatus = {};
    const totalByStatus = {};
    (state.board.columns || []).forEach((c) => {
      cardsByStatus[c.status] = c.cards || [];
      totalByStatus[c.status] = (c.total != null ? c.total : (c.cards || []).length);
    });

    cols.forEach((col) => {
      const colEl = el('div', 'kanban-column');
      colEl.dataset.column = col.key;
      const header = el('div', 'kanban-column__header');
      header.textContent = col.label + ' (' + (totalByStatus[col.key] || 0) + ')';
      colEl.appendChild(header);

      const list = el('div', 'kanban-column__list');
      list.addEventListener('dragover', (e) => { e.preventDefault(); });
      list.addEventListener('drop', (e) => { e.preventDefault(); const id = e.dataTransfer.getData('text/plain'); if (id) moveTask(id, col.key); });

      const items = cardsByStatus[col.key] || [];
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
    if (t.assignee) {
      const asg = el('span', 'kanban-card__assignee');
      asg.textContent = '@' + t.assignee;
      meta.appendChild(asg);
    }
    if (t.goal_mode) {
      const g = el('span', 'kanban-card__badge');
      g.textContent = 'goal';
      meta.appendChild(g);
    }
    card.appendChild(meta);
    return card;
  }

  async function refresh() {
    try {
      const board = await api.task.board(false);
      state.board = board || { columns: [], archived: false, assignees: [] };
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

  async function moveTask(id, targetStatus) {
    try {
      const task = await api.task.get(id);
      await api.task.patch(id, { status: targetStatus, expected_version: task.version });
      refresh();
    } catch (e) {
      alert('移动任务失败（可能状态冲突）：' + (e && e.message ? e.message : e));
      refresh();
    }
  }

  // ---- create modal ----
  // Standard modal popup (mirrors knowledge.js openKbForm): replaces the old
  // window.prompt which only collected a title. Inputs: title (required),
  // goal (-> body), priority, goal_mode. Esc / backdrop / × close. All
  // textContent rendering, no innerHTML. The legacy `triage: true` flag is
  // dropped: the backend create_task never consumed it (new tasks default to
  // the triage state); assignee is intentionally omitted (prd 20260719
  // direction: drop human-PM fields).
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
    titleEl.textContent = '新增';
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

  async function openDetail(id) {
    const drawer = document.getElementById('tasks-detail-drawer');
    if (!drawer) return;
    ui.clear(drawer);
    try {
      const detail = await api.task.get(id);
      drawer.hidden = false;
      const close = el('button', 'btn tasks-detail__close');
      close.type = 'button'; close.textContent = '关闭';
      close.addEventListener('click', () => { drawer.hidden = true; });
      drawer.appendChild(close);

      const h = el('h3', 'tasks-detail__title');
      h.textContent = detail.title || detail.id;
      drawer.appendChild(h);

      const grid = el('div', 'tasks-detail__grid');
      const fields = [
        ['ID', detail.id],
        ['状态', detail.status],
        ['负责人', detail.assignee || '-'],
        ['优先级', detail.priority != null ? String(detail.priority) : '-'],
        ['版本', detail.version != null ? String(detail.version) : '-'],
        ['目标模式', detail.goal_mode ? '是' : '否'],
        ['创建时间', detail.created_at || '-'],
      ];
      fields.forEach(([k, v]) => {
        const row = el('div', 'tasks-detail__row');
        const kl = el('span', 'tasks-detail__k'); kl.textContent = k + '：';
        const vl = el('span', 'tasks-detail__v'); vl.textContent = String(v);
        row.appendChild(kl); row.appendChild(vl); grid.appendChild(row);
      });
      drawer.appendChild(grid);

      if (detail.body) {
        const bodyLabel = el('div', 'tasks-detail__section-label');
        bodyLabel.textContent = '描述';
        const bodyDiv = el('div', 'tasks-detail__body');
        bodyDiv.textContent = detail.body;
        drawer.appendChild(bodyLabel);
        drawer.appendChild(bodyDiv);
      }

      if (detail.result) {
        const rl = el('div', 'tasks-detail__section-label'); rl.textContent = '结果';
        const rv = el('div', 'tasks-detail__body'); rv.textContent = detail.result;
        drawer.appendChild(rl); drawer.appendChild(rv);
      }
    } catch (e) {
      const err = el('div', 'tasks-error'); err.textContent = '加载详情失败：' + (e && e.message ? e.message : e);
      drawer.appendChild(err);
    }
  }

  namespace.tasks = { init, refresh };
}(window));
