(function (global) {
  'use strict';
  // scheduled-tasks-observations scoped tab.
  // 跨任务执行记录总览页：任务过滤下拉 + 执行记录表格（最近 50 条）。
  // Mount Point: <div class="tab-content" id="tab-scheduled-tasks-observations"></div>
  const namespace = global.NAGENT || {};
  const api = namespace.api || {};

  const CONTAINER_ID = 'tab-scheduled-tasks-observations';
  const DEFAULT_LIMIT = 50;
  const TRUNCATE_AT = 120;

  // 异步防护（对齐 tasks-observations.js 的代次模式）：
  // - renderToken 单调递增；每次加载捕获快照，过期响应一律丢弃。
  // - inflight 记录当前代次 { token, filter, promise }；同代次同过滤共享，
  //   过期 finally 不得清除新代次引用（按 promise 身份比对）。
  // - deactivate 递增 token 并释放 inflight；首次 init 未完成的重入经
  //   activate 启动新代次。
  let renderToken = 0;
  let inflight = null;

  let filterTaskId = '';
  let nameById = {};
  let tasksState = 'idle'; // idle | loading | ok | error
  let execState = 'idle'; // idle | loading | ok | error
  let executions = [];
  let notice = '';
  let recovered = false; // 同一加载周期只自动恢复一次（手动操作重置）
  let skeleton = null; // { select, refreshBtn, noticeEl, tableEl }
  let modalBackdrop = null;

  function root() {
    return document.getElementById(CONTAINER_ID);
  }

  function isActive() {
    const node = root();
    return !!(node && node.classList && node.classList.contains('active'));
  }

  function isCurrent(token) {
    return token === renderToken && isActive();
  }

  function validInflight() {
    return inflight && inflight.token === renderToken ? inflight : null;
  }

  function text(value, fallback) {
    if (value === null || value === undefined || value === '') return fallback || '-';
    return String(value);
  }

  // UTC+8 渲染，与浏览器时区无关；非法时间返回 '-'（不照抄管理页的回落原值）。
  function formatDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '-';
    const tz = new Date(date.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${tz.getUTCFullYear()}-${pad(tz.getUTCMonth() + 1)}-${pad(tz.getUTCDate())} ${pad(tz.getUTCHours())}:${pad(tz.getUTCMinutes())}:${pad(tz.getUTCSeconds())}`;
  }

  function el(tag, className) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    return node;
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

  function badge(value, kind) {
    const node = document.createElement('span');
    node.className = `badge${kind ? ` badge--${kind}` : ''}`;
    node.textContent = value;
    return node;
  }

  function statusKind(value) {
    if (value === 'succeeded') return 'success';
    if (value === 'running' || value === 'skipped_missed') return 'warning';
    if (value === 'failed' || value === 'blocked') return 'danger';
    return '';
  }

  // ------------------------------------------------------------ 骨架与渲染 --
  function ensureSkeleton() {
    const node = root();
    if (!node) return null;
    if (skeleton) return skeleton;
    node.replaceChildren();

    const header = el('div', 'scheduled-obs-header');
    appendText(header, 'span', '任务过滤', 'muted');
    const select = el('select', 'scheduled-obs-filter');
    select.addEventListener('change', () => onFilterChange(select.value));
    const refreshBtn = button('刷新', 'btn', () => {
      recovered = false;
      startLoad(true);
    });
    header.appendChild(select);
    header.appendChild(refreshBtn);
    appendText(header, 'span', `最近 ${DEFAULT_LIMIT} 条`, 'muted');

    const noticeEl = el('div', 'scheduled-obs-notice');
    const tableEl = el('div', 'scheduled-obs-table');
    node.appendChild(header);
    node.appendChild(noticeEl);
    node.appendChild(tableEl);
    skeleton = { select, refreshBtn, noticeEl, tableEl };
    renderFilterOptions();
    renderNotice();
    renderTableArea();
    return skeleton;
  }

  function renderFilterOptions() {
    if (!skeleton) return;
    const select = skeleton.select;
    if (tasksState === 'loading') {
      // 加载中保留既有选项（含当前过滤），避免重建为仅“全部任务”后浏览器对
      // 不存在的 option 回落 selectedIndex=-1 而清空 select.value/filterTaskId。
      // 首次尚无选项时仅补“全部任务”；不重写 select.value/filterTaskId。
      if (!select.children.length) {
        const all = document.createElement('option');
        all.value = '';
        all.textContent = '全部任务';
        select.appendChild(all);
      }
      return;
    }
    // error：nameById 已清空，重建后仅剩“全部任务”（此时 filterTaskId 已被
    // onTasksError 清空，不会因选项缺失误清过滤）。
    select.replaceChildren();
    const all = document.createElement('option');
    all.value = '';
    all.textContent = '全部任务';
    select.appendChild(all);
    const names = Object.keys(nameById).map((id) => nameById[id]).filter(Boolean);
    const dup = {};
    names.forEach((n) => { dup[n] = (dup[n] || 0) + 1; });
    Object.keys(nameById).forEach((id) => {
      const name = nameById[id];
      const option = document.createElement('option');
      option.value = id;
      option.textContent = name ? (dup[name] > 1 ? `${name} (${id})` : name) : id;
      select.appendChild(option);
    });
    select.value = filterTaskId || '';
    if (select.value !== (filterTaskId || '')) {
      // 选项不存在时浏览器回落；同步内部状态。
      filterTaskId = select.value;
    }
  }

  function renderNotice() {
    if (!skeleton) return;
    const host = skeleton.noticeEl;
    host.replaceChildren();
    if (!notice) return;
    appendText(host, 'div', notice, 'error-state');
    host.appendChild(button('重试', 'btn', () => { startLoad(true); }));
  }

  function renderClipped(cell, value) {
    const raw = text(value, '');
    if (!raw) {
      cell.textContent = '-';
      return;
    }
    if (raw.length <= TRUNCATE_AT) {
      cell.textContent = raw;
      return;
    }
    appendText(cell, 'span', `${raw.slice(0, TRUNCATE_AT)}...`);
    cell.appendChild(button('查看', 'btn', () => showTextModal('完整内容', raw)));
  }

  function renderTableArea() {
    if (!skeleton) return;
    const host = skeleton.tableEl;
    host.replaceChildren();
    if (execState === 'loading' || execState === 'idle') {
      appendText(host, 'div', '加载中...', 'loading-state');
      return;
    }
    if (execState === 'error') {
      appendText(host, 'div', '执行记录加载失败', 'error-state');
      host.appendChild(button('重试', 'btn', () => { startLoad(true); }));
      return;
    }
    if (!executions.length) {
      appendText(host, 'div', '暂无执行记录', 'empty-state');
      return;
    }
    const table = el('table', 'document-table scheduled-history-table');
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    ['开始时间', '任务', '状态', '输出', '错误', '投递'].forEach((label) => appendText(headRow, 'th', label));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    // 保留服务端顺序（created_at 倒序），不按显示时间重排。
    executions.forEach((execution) => tbody.appendChild(renderRow(execution)));
    table.appendChild(tbody);
    host.appendChild(table);
  }

  function renderRow(execution) {
    const row = document.createElement('tr');
    appendText(row, 'td', formatDate(execution.started_at || execution.created_at));

    const name = execution.task_id ? nameById[execution.task_id] : null;
    appendText(row, 'td', text(name, execution.task_id));

    const statusCell = document.createElement('td');
    statusCell.appendChild(badge(text(execution.status), statusKind(execution.status)));
    row.appendChild(statusCell);

    const outputCell = document.createElement('td');
    renderClipped(outputCell, execution.output);
    row.appendChild(outputCell);

    const errorCell = document.createElement('td');
    renderClipped(errorCell, execution.error);
    row.appendChild(errorCell);

    const deliveryCell = document.createElement('td');
    const hasStatus = execution.delivery_status !== null && execution.delivery_status !== undefined && execution.delivery_status !== '';
    const hasError = execution.delivery_error !== null && execution.delivery_error !== undefined && execution.delivery_error !== '';
    if (!hasStatus && !hasError) {
      deliveryCell.textContent = '-';
    } else {
      // 投递状态与非空投递错误同时呈现，不因状态存在隐藏失败原因。
      if (hasStatus) appendText(deliveryCell, 'div', String(execution.delivery_status));
      if (hasError) appendText(deliveryCell, 'div', String(execution.delivery_error), 'error-state');
    }
    row.appendChild(deliveryCell);
    return row;
  }

  // ------------------------------------------------------------ 弹框 --
  function closeModal() {
    if (modalBackdrop) {
      modalBackdrop.remove();
      modalBackdrop = null;
    }
  }

  // 共享 Modal DOM 范式：.modal-backdrop/.modal-dialog/.modal-header/.modal-close，
  // 正文 pre.textContent 纯文本，挂 document.body 接入全局焦点/滚动锁。
  function showTextModal(title, fullText) {
    closeModal();
    const backdrop = el('div', 'modal-backdrop');
    const dialog = el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const header = el('div', 'modal-header');
    appendText(header, 'h4', title || '完整内容');
    const closeBtn = button('×', 'modal-close', closeModal);
    closeBtn.setAttribute('aria-label', '关闭');
    header.appendChild(closeBtn);
    dialog.appendChild(header);
    const pre = document.createElement('pre');
    pre.textContent = fullText;
    dialog.appendChild(pre);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeModal();
    });
    document.body.appendChild(backdrop);
    modalBackdrop = backdrop;
  }

  // ------------------------------------------------------------ 加载 --
  function onFilterChange(value) {
    recovered = false;
    filterTaskId = value || '';
    startLoad(true);
  }

  function onTasksOk(tasks) {
    tasksState = 'ok';
    nameById = {};
    (tasks || []).forEach((task) => {
      if (!task || task.id === null || task.id === undefined) return;
      const name = task.name;
      nameById[task.id] = (name === null || name === undefined || name === '') ? null : String(name);
    });
    if (filterTaskId && !(filterTaskId in nameById)) {
      // 列表成功但选中 id 已消失：提示并恢复一次。
      taskUnavailable();
      return;
    }
    renderFilterOptions();
    renderTableArea();
  }

  function onTasksError() {
    tasksState = 'error';
    nameById = {};
    if (filterTaskId && !recovered) {
      // 原选中任务时列表失败：清空选择，新代次查询全部记录（丢弃原过滤响应）。
      recovered = true;
      filterTaskId = '';
      startLoad(true);
      notice = '任务列表加载失败，已切换为全部任务';
      renderNotice();
      return;
    }
    filterTaskId = '';
    notice = '任务列表加载失败，名称暂以任务 ID 显示';
    renderFilterOptions();
    renderNotice();
    renderTableArea();
  }

  function onExecOk(rows) {
    execState = 'ok';
    executions = rows || [];
    renderTableArea();
  }

  function onExecError(err) {
    if (filterTaskId && err && err.message === 'scheduled_task_not_found' && !recovered) {
      taskUnavailable();
      return;
    }
    execState = 'error';
    renderTableArea();
  }

  function taskUnavailable() {
    if (recovered) {
      filterTaskId = '';
      renderFilterOptions();
      renderNotice();
      return;
    }
    recovered = true;
    filterTaskId = '';
    startLoad(true);
    notice = '任务已不可用，已切换为全部任务';
    renderNotice();
  }

  // force=false 时同代次同过滤共享 inflight；force=true 启动新代次。
  function startLoad(force) {
    if (!ensureSkeleton()) return Promise.resolve();
    if (!force) {
      const current = validInflight();
      if (current && current.filter === filterTaskId) return current.promise;
    }
    renderToken++;
    const token = renderToken;
    tasksState = 'loading';
    execState = 'loading';
    executions = [];
    notice = '';
    renderFilterOptions();
    renderNotice();
    renderTableArea();

    const filter = filterTaskId;
    const tasksP = Promise.resolve().then(() => api.listScheduledTasks());
    const execP = Promise.resolve().then(() => api.listScheduledTaskExecutionsAll({
      task_id: filter || undefined,
      limit: DEFAULT_LIMIT,
    }));
    // 两个请求独立成败：完成一端即更新其负责区域，不等待另一端。
    tasksP.then(
      (tasks) => { if (isCurrent(token)) onTasksOk(tasks); },
      (err) => { if (isCurrent(token)) onTasksError(err); },
    );
    execP.then(
      (rows) => { if (isCurrent(token)) onExecOk(rows); },
      (err) => { if (isCurrent(token)) onExecError(err); },
    );
    const p = Promise.allSettled([tasksP, execP]).then(() => {
      if (inflight && inflight.promise === p) inflight = null;
    });
    inflight = { token, filter, promise: p };
    return p;
  }

  function init() {
    filterTaskId = '';
    recovered = false;
    nameById = {};
    executions = [];
    notice = '';
    tasksState = 'idle';
    execState = 'idle';
    skeleton = null;
    const current = validInflight();
    if (current) return current.promise;
    return startLoad(true);
  }

  function refresh() {
    const current = validInflight();
    if (current && current.filter === filterTaskId) return current.promise;
    return startLoad(true);
  }

  function activate() {
    if (!isActive()) return Promise.resolve();
    const current = validInflight();
    if (current) return current.promise;
    return startLoad(true);
  }

  function deactivate() {
    renderToken++;
    inflight = null;
    closeModal();
  }

  namespace.scheduledTasksObservations = { init, refresh, activate, deactivate };
  global.NAGENT = namespace;
}(window));
