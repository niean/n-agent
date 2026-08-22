(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;
  const modal = namespace.modal;

  const SESSION_SOURCE_FILTER_STORAGE_KEY = 'nagent.sessions.source-filter.v1';
  const SESSION_SOURCE_OPTIONS = [
    ['dashboard', 'Dashboard'],
    ['api', 'API'],
    ['cli', 'CLI'],
    ['feishu', '飞书'],
    ['dingtalk', '钉钉'],
    ['wecom', '企微'],
    ['acp', 'ACP'],
    ['schedule', '定时任务'],
    ['task', '任务'],
    ['curator', 'Curator'],
    ['delegation', '委派'],
  ];
  const SESSION_SOURCE_VALUES = new Set(SESSION_SOURCE_OPTIONS.map(([value]) => value));
  let selectedSessionSources = loadSessionSourceFilter();

  function defaultSessionSourceFilter() {
    return new Set(SESSION_SOURCE_OPTIONS.map(([value]) => value).filter((value) => value !== 'delegation'));
  }

  function loadSessionSourceFilter() {
    try {
      const raw = global.localStorage && global.localStorage.getItem(SESSION_SOURCE_FILTER_STORAGE_KEY);
      if (!raw) return defaultSessionSourceFilter();
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed)) return defaultSessionSourceFilter();
      const selected = new Set(parsed.filter((value) => typeof value === 'string' && SESSION_SOURCE_VALUES.has(value)));
      return selected.size ? selected : defaultSessionSourceFilter();
    } catch (_) {
      return defaultSessionSourceFilter();
    }
  }

  function saveSessionSourceFilter(sources) {
    selectedSessionSources = new Set(sources);
    try {
      if (global.localStorage) {
        global.localStorage.setItem(SESSION_SOURCE_FILTER_STORAGE_KEY, JSON.stringify([...selectedSessionSources]));
      }
    } catch (_) {
      // Local storage is an optional Dashboard convenience; the in-memory filter still applies.
    }
  }

  function isSessionVisible(session) {
    return !!session && typeof session.source === 'string'
      && SESSION_SOURCE_VALUES.has(session.source) && selectedSessionSources.has(session.source);
  }

  function closeSessionSourceFilterModal(backdrop, onKeydown) {
    if (onKeydown) document.removeEventListener('keydown', onKeydown);
    if (backdrop && backdrop.parentNode) backdrop.remove();
  }

  function openSessionSourceFilterModal() {
    const existing = document.getElementById('sessions-source-filter-modal');
    if (existing) existing.remove();
    const backdrop = document.createElement('div');
    backdrop.id = 'sessions-source-filter-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'sessions-source-filter-title');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.id = 'sessions-source-filter-title';
    title.textContent = '筛选会话类型';
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭筛选会话类型弹框');
    header.append(title, closeBtn);
    form.appendChild(header);

    const options = ui.el('div', 'session-source-filter');
    const draftSources = new Set(selectedSessionSources);
    SESSION_SOURCE_OPTIONS.forEach(([value, label]) => {
      const option = ui.el('label', 'session-source-filter__option');
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.value = value;
      checkbox.checked = draftSources.has(value);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) draftSources.add(value);
        else draftSources.delete(value);
      });
      const text = document.createElement('span');
      text.textContent = label;
      option.append(checkbox, text);
      options.appendChild(option);
    });
    form.appendChild(options);

    const actions = ui.el('div', 'providers-form__actions');
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'btn';
    cancelBtn.textContent = '取消';
    const applyBtn = document.createElement('button');
    applyBtn.type = 'submit';
    applyBtn.className = 'btn btn--primary';
    applyBtn.textContent = '应用';
    actions.append(cancelBtn, applyBtn);
    form.appendChild(actions);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);

    const onKeydown = (event) => {
      if (event.key === 'Escape') closeSessionSourceFilterModal(backdrop, onKeydown);
    };
    closeBtn.addEventListener('click', () => closeSessionSourceFilterModal(backdrop, onKeydown));
    cancelBtn.addEventListener('click', () => closeSessionSourceFilterModal(backdrop, onKeydown));
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeSessionSourceFilterModal(backdrop, onKeydown);
    });
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      saveSessionSourceFilter(draftSources);
      closeSessionSourceFilterModal(backdrop, onKeydown);
      refresh();
    });
    document.addEventListener('keydown', onKeydown);
    document.body.appendChild(backdrop);
    closeBtn.focus();
  }

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
      const sessions = (await api.listSessions()).filter(isSessionVisible);
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
    const filterBtn = ui.byId('sessions-filter-btn');
    if (filterBtn) filterBtn.addEventListener('click', openSessionSourceFilterModal);
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.sessions = { init, refresh };
}(window));
