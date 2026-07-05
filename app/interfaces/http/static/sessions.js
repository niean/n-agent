(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;
  const modal = namespace.modal;

  function button(label, className, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className || 'btn';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
  }

  async function deleteSession(sessionId) {
    if (!sessionId) {
      await modal.alert('缺少会话ID');
      return;
    }
    if (!(await modal.confirm(`确认删除会话 ${sessionId}？此操作将级联删除消息、工具调用、摘要和任务状态，不可恢复。`))) return;
    try {
      await api.deleteSession(sessionId);
      await refresh();
    } catch (err) {
      await modal.alert(`删除失败：${err && err.message ? err.message : err}`);
    }
  }

  async function refresh() {
    const list = ui.byId('sessions-list');
    if (!list) return;
    ui.clear(list);
    ui.renderLoading(list, '加载会话中...');
    try {
      const sessions = await api.listSessions();
      ui.clear(list);
      if (!sessions.length) { ui.renderEmpty(list, '暂无会话'); return; }
      const table = document.createElement('table');
      table.className = 'document-table';
      const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      ['标题', '来源', 'ID', '操作'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      const tbody = document.createElement('tbody');
      sessions.forEach((session) => {
        const tr = document.createElement('tr');
        const td1 = document.createElement('td'); td1.textContent = session.title || session.id;
        const td2 = document.createElement('td'); td2.textContent = session.source || '-';
        const td3 = document.createElement('td'); td3.textContent = session.id;
        const td4 = document.createElement('td');
        td4.className = 'row-actions';
        td4.append(button('删除', 'btn', () => deleteSession(session.id)));
        td4.append(button('详情', 'btn', () => openSessionDetailModal(session.id)));
        tr.append(td1, td2, td3, td4);
        tbody.appendChild(tr);
      });
      table.append(thead, tbody);
      list.appendChild(table);
    } catch (error) {
      ui.clear(list);
      ui.renderError(list, error.message);
    }
  }

  function closeSessionDetailModal() {
    const modal = document.getElementById('sessions-detail-modal');
    if (modal) modal.remove();
  }

  async function openSessionDetailModal(sessionId) {
    closeSessionDetailModal();
    const backdrop = document.createElement('div');
    backdrop.id = 'sessions-detail-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const titleEl = document.createElement('h4');
    titleEl.textContent = '查看会话: ' + sessionId;
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭会话详情弹框');
    closeBtn.addEventListener('click', closeSessionDetailModal);
    header.append(titleEl, closeBtn);
    form.appendChild(header);

    const body = ui.el('div', 'sessions-detail-modal-body');
    ui.renderLoading(body, '加载会话详情...');
    form.appendChild(body);

    const actions = ui.el('div', 'providers-form__actions');
    const closeAction = document.createElement('button');
    closeAction.type = 'button';
    closeAction.className = 'btn';
    closeAction.textContent = '关闭';
    closeAction.addEventListener('click', closeSessionDetailModal);
    actions.appendChild(closeAction);
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeSessionDetailModal();
    });
    document.body.appendChild(backdrop);
    closeBtn.focus();

    try {
      const [info, calls] = await Promise.all([
        api.getSessionDetail(sessionId),
        api.getSessionToolCalls(sessionId),
      ]);
      ui.clear(body);

      const infoSection = ui.el('div', 'debug-section');
      const iH = document.createElement('h4'); iH.textContent = '基本信息';
      const iList = ui.el('div', 'session-info-list');
      ui.appendText(iList, '会话 ID:', info.session ? info.session.id : sessionId);
      ui.appendText(iList, '标题:', info.session ? info.session.title : '-');
      ui.appendText(iList, '来源:', info.session ? info.session.source : '-');
      ui.appendText(iList, '消息数:', (info.messages || []).length);
      infoSection.append(iH, iList);
      body.appendChild(infoSection);

      const summarySection = ui.el('div', 'debug-section');
      const sH = document.createElement('h4'); sH.textContent = 'Summary';
      const sP = document.createElement('pre'); sP.textContent = info.summary ? info.summary.summary : '暂无摘要';
      summarySection.append(sH, sP);
      body.appendChild(summarySection);

      const taskSection = ui.el('div', 'debug-section');
      const tH = document.createElement('h4'); tH.textContent = 'Task State';
      const tP = document.createElement('pre');
      tP.textContent = info.task_state ? JSON.stringify(info.task_state, null, 2) : '暂无任务状态';
      taskSection.append(tH, tP);
      body.appendChild(taskSection);

      const toolSection = ui.el('div', 'debug-section');
      const cH = document.createElement('h4'); cH.textContent = `Tool Calls (${calls.length})`;
      toolSection.appendChild(cH);
      if (!calls.length) {
        const empty = document.createElement('div'); empty.className = 'muted'; empty.textContent = '暂无工具调用';
        toolSection.appendChild(empty);
      } else {
        calls.forEach((call) => {
          const el = document.createElement('div');
          el.className = 'tool-call';
          el.textContent = `${call.tool_name}: ${JSON.stringify(call.arguments)} → ${call.status} (${call.duration_ms}ms)`;
          toolSection.appendChild(el);
        });
      }
      body.appendChild(toolSection);
    } catch (error) {
      ui.clear(body);
      ui.renderError(body, error.message);
    }
  }

  function init() {
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.sessions = { init, refresh };
}(window));
