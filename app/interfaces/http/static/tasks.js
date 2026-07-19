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
  const ARCHIVED_KEY = 'archived';

  let state = { board: { columns: {}, archived: [] }, showArchived: false, detail: null };
  let ws = null;

  function init() {
    if (!api || !api.task) return;
    renderShell();
    refresh();
    bindCreate();
  }

  function renderShell() {
    const root = document.getElementById('tasks-board');
    if (!root) return;
    ui.clear(root);

    const bar = el('div', 'tasks-bar');
    const newBtn = el('button', 'btn btn--primary');
    newBtn.type = 'button';
    newBtn.textContent = '新增任务';
    newBtn.addEventListener('click', openCreateModal);
    bar.appendChild(newBtn);

    const refreshBtn = el('button', 'btn');
    refreshBtn.type = 'button';
    refreshBtn.textContent = '刷新';
    refreshBtn.addEventListener('click', refresh);
    bar.appendChild(refreshBtn);

    const archivedToggle = el('label', 'tasks-archived-toggle');
    const cb = el('input', '');
    cb.type = 'checkbox';
    cb.addEventListener('change', () => { state.showArchived = cb.checked; renderBoard(); });
    const span = el('span', '');
    span.textContent = '显示已归档';
    archivedToggle.appendChild(cb);
    archivedToggle.appendChild(span);
    bar.appendChild(archivedToggle);
    root.appendChild(bar);

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

    const columns = state.showArchived
      ? [{ key: ARCHIVED_KEY, label: '已归档' }]
      : COLUMNS;

    columns.forEach((col) => {
      const colEl = el('div', 'kanban-column');
      colEl.dataset.column = col.key;
      const header = el('div', 'kanban-column__header');
      header.textContent = col.label + ' (' + ((state.board.columns[col.key] || state.board.archived || []).length) + ')';
      colEl.appendChild(header);

      const list = el('div', 'kanban-column__list');
      list.addEventListener('dragover', (e) => { e.preventDefault(); });
      list.addEventListener('drop', (e) => { e.preventDefault(); const id = e.dataTransfer.getData('text/plain'); if (id) moveTask(id, col.key); });

      const items = state.board.columns[col.key] || (col.key === ARCHIVED_KEY ? state.board.archived : []) || [];
      items.forEach((t) => list.appendChild(renderCard(t, col.key)));
      colEl.appendChild(list);
      board.appendChild(colEl);
    });
  }

  function renderCard(t, columnKey) {
    const card = el('div', 'kanban-card');
    card.draggable = (columnKey !== ARCHIVED_KEY);
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
      const board = await api.task.board();
      state.board = board || { columns: {}, archived: [] };
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

  function openCreateModal() {
    const title = window.prompt('任务标题');
    if (!title) return;
    const body = { title: title, triage: true };
    api.task.create(body).then(() => refresh()).catch((e) => alert('创建失败：' + (e && e.message ? e.message : e)));
  }

  function bindCreate() { /* placeholder for future modal binding */ }

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
