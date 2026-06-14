(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

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
        const btn = document.createElement('button');
        btn.className = 'btn';
        btn.type = 'button';
        btn.textContent = '查看';
        btn.addEventListener('click', () => showDetail(session.id));
        td4.appendChild(btn);
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

  async function showDetail(sessionId) {
    const detail = ui.byId('sessions-detail');
    if (!detail) return;
    ui.clear(detail);
    ui.renderLoading(detail, '加载会话详情...');
    try {
      const [info, calls] = await Promise.all([
        api.getSessionDetail(sessionId),
        api.getSessionToolCalls(sessionId),
      ]);
      ui.clear(detail);
      ui.appendText(detail, '会话 ID:', info.session ? info.session.id : sessionId);
      ui.appendText(detail, '标题:', info.session ? info.session.title : '-');
      ui.appendText(detail, '来源:', info.session ? info.session.source : '-');
      ui.appendText(detail, '消息数:', (info.messages || []).length);
      const summarySection = document.createElement('div');
      summarySection.className = 'debug-section';
      const sH = document.createElement('h4'); sH.textContent = 'Summary';
      const sP = document.createElement('pre'); sP.textContent = info.summary ? info.summary.summary : '暂无摘要';
      summarySection.append(sH, sP);
      detail.appendChild(summarySection);
      const taskSection = document.createElement('div');
      taskSection.className = 'debug-section';
      const tH = document.createElement('h4'); tH.textContent = 'Task State';
      const tP = document.createElement('pre');
      tP.textContent = info.task_state ? JSON.stringify(info.task_state, null, 2) : '暂无任务状态';
      taskSection.append(tH, tP);
      detail.appendChild(taskSection);
      const toolSection = document.createElement('div');
      toolSection.className = 'debug-section';
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
      detail.appendChild(toolSection);
    } catch (error) {
      ui.clear(detail);
      ui.renderError(detail, error.message);
    }
  }

  function init() {
    const refreshBtn = ui.byId('sessions-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', refresh);
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.sessions = { init, refresh };
}(window));
