(function (global) {
  'use strict';
  // scheduled-tasks-observations scoped tab.
  // 跨任务执行记录总览页：标准 status-panel 骨架 + 执行记录表格（最近 50 条）。
  // Mount Point: <div class="tab-content" id="tab-scheduled-tasks-observations"></div>
  const namespace = global.NAGENT || {};
  const api = namespace.api || {};

  const CONTAINER_ID = 'tab-scheduled-tasks-observations';
  const DEFAULT_LIMIT = 50;
  const TRUNCATE_AT = 120;

  // 异步防护（对齐 tasks-observations.js 的代次模式）：
  // - renderToken 单调递增；每次加载捕获快照，过期响应一律丢弃。
  // - inflight 记录当前代次 { token, promise }；同代次共享，
  //   过期 finally 不得清除新代次引用（按 promise 身份比对）。
  // - deactivate 递增 token 并释放 inflight；首次 init 未完成的重入经
  //   activate 启动新代次。
  let renderToken = 0;
  let inflight = null;

  let nameById = {};
  let tasksState = 'idle'; // idle | loading | ok | error
  let execState = 'idle'; // idle | loading | ok | error
  let executions = [];
  let notice = '';
  let skeleton = null; // { noticeEl, tableEl }
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

  function deliveryKind(value) {
    if (value === 'success' || value === 'delivered') return 'success';
    if (value === 'skipped') return 'warning';
    if (value === 'failed') return 'danger';
    return '';
  }

  // ------------------------------------------------------------ 骨架与渲染 --
  function ensureSkeleton() {
    const node = root();
    if (!node) return null;
    if (skeleton) return skeleton;
    node.replaceChildren();

    const panel = el('section', 'status-panel');
    const header = el('div', 'panel-header');
    const titleGroup = el('div', 'panel-title-group');
    titleGroup.setAttribute('tabindex', '0');
    titleGroup.setAttribute('aria-describedby', 'scheduled-obs-tips');
    appendText(titleGroup, 'span', '执行记录', 'panel-title');
    const tips = appendText(titleGroup, 'span', `跨任务执行总览，展示最近 ${DEFAULT_LIMIT} 条记录`, 'panel-tips muted');
    tips.id = 'scheduled-obs-tips';
    tips.setAttribute('role', 'tooltip');
    header.appendChild(titleGroup);
    panel.appendChild(header);

    const body = el('div', 'panel-body');
    const noticeEl = el('div');
    const tableEl = el('div');
    body.appendChild(noticeEl);
    body.appendChild(tableEl);
    panel.appendChild(body);
    node.appendChild(panel);
    skeleton = { noticeEl, tableEl };
    renderNotice();
    renderTableArea();
    return skeleton;
  }

  function renderNotice() {
    if (!skeleton) return;
    const host = skeleton.noticeEl;
    host.replaceChildren();
    if (!notice) return;
    appendText(host, 'div', notice, 'error-state');
    host.appendChild(button('重试', 'btn', () => { startLoad(true); }));
  }

  // 列表单元格只做定长截断（无查看按钮），完整内容统一在详情 modal 呈现。
  function clipText(value) {
    const raw = text(value, '');
    if (!raw) return '-';
    if (raw.length <= TRUNCATE_AT) return raw;
    return `${raw.slice(0, TRUNCATE_AT)}...`;
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
    const wrap = el('div', 'scheduled-table-wrap');
    const table = el('table', 'document-table scheduled-history-table');
    const thead = document.createElement('thead');
    const headRow = document.createElement('tr');
    ['开始时间', '任务', '状态', '输出', '投递', '操作'].forEach((label) => appendText(headRow, 'th', label));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = document.createElement('tbody');
    // 保留服务端顺序（created_at 倒序），不按显示时间重排。
    executions.forEach((execution) => tbody.appendChild(renderRow(execution)));
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
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
    outputCell.textContent = clipText(execution.output);
    row.appendChild(outputCell);

    const deliveryCell = document.createElement('td');
    // 列表只呈现投递状态，成功/失败详情（投递错误）移入详情 modal；取值与状态列统一用 badge 底色呈现。
    deliveryCell.appendChild(badge(text(execution.delivery_status), deliveryKind(execution.delivery_status)));
    row.appendChild(deliveryCell);

    const actionCell = document.createElement('td');
    actionCell.appendChild(button('详情', 'btn', () => showDetailModal(execution)));
    row.appendChild(actionCell);
    return row;
  }

  // ------------------------------------------------------------ 弹框 --
  function closeModal() {
    if (modalBackdrop) {
      modalBackdrop.remove();
      modalBackdrop = null;
    }
  }

  function preText(value) {
    const pre = document.createElement('pre');
    pre.textContent = text(value);
    return pre;
  }

  function detailSection(label, contentNode) {
    const section = el('div', 'debug-section');
    appendText(section, 'h4', label);
    section.appendChild(contentNode);
    return section;
  }

  // 详情 modal：执行时间、状态、输出、投递（含失败详情）、错误全量呈现。
  // 共享 Modal DOM 范式：.modal-backdrop/.modal-dialog + form.providers-form
  // + .modal-header/.modal-close + <page>-detail-modal-body + providers-form__actions，
  // 正文纯文本渲染（textContent），挂 document.body 接入全局焦点/滚动锁。
  function showDetailModal(execution) {
    closeModal();
    const backdrop = el('div', 'modal-backdrop');
    const dialog = el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = el('div', 'modal-header');
    appendText(header, 'h4', '执行详情');
    const closeBtn = button('×', 'modal-close', closeModal);
    closeBtn.setAttribute('aria-label', '关闭执行详情弹框');
    header.appendChild(closeBtn);
    form.appendChild(header);

    const body = el('div', 'scheduled-exec-detail-modal-body');

    const infoList = el('div', 'session-info-list');
    appendText(infoList, 'div', `执行时间: ${formatDate(execution.started_at || execution.created_at)}`);
    const statusLine = el('div');
    appendText(statusLine, 'span', '状态: ');
    statusLine.appendChild(badge(text(execution.status), statusKind(execution.status)));
    infoList.appendChild(statusLine);
    const hasStatus = execution.delivery_status !== null && execution.delivery_status !== undefined && execution.delivery_status !== '';
    const hasError = execution.delivery_error !== null && execution.delivery_error !== undefined && execution.delivery_error !== '';
    const deliveryLine = el('div');
    if (!hasStatus && !hasError) {
      deliveryLine.textContent = '投递: -';
    } else {
      // 投递状态与非空投递错误同时呈现，不因状态存在隐藏失败原因；取值与状态列统一用 badge 底色呈现。
      appendText(deliveryLine, 'span', '投递: ');
      deliveryLine.appendChild(badge(hasStatus ? text(execution.delivery_status) : '-', deliveryKind(execution.delivery_status)));
      if (hasError) appendText(deliveryLine, 'div', String(execution.delivery_error), 'error-state');
    }
    infoList.appendChild(deliveryLine);
    body.appendChild(detailSection('基本信息', infoList));

    body.appendChild(detailSection('输出', preText(execution.output)));
    body.appendChild(detailSection('错误', preText(execution.error)));
    form.appendChild(body);

    const actions = el('div', 'providers-form__actions');
    actions.appendChild(button('关闭', 'btn', closeModal));
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    // 项目规范：详情 modal 不响应空白处点击，仅经 × / 关闭 按钮显式退出。
    document.body.appendChild(backdrop);
    closeBtn.focus();
    modalBackdrop = backdrop;
  }

  // ------------------------------------------------------------ 加载 --
  function onTasksOk(tasks) {
    tasksState = 'ok';
    nameById = {};
    (tasks || []).forEach((task) => {
      if (!task || task.id === null || task.id === undefined) return;
      const name = task.name;
      nameById[task.id] = (name === null || name === undefined || name === '') ? null : String(name);
    });
    renderTableArea();
  }

  function onTasksError() {
    tasksState = 'error';
    nameById = {};
    notice = '任务列表加载失败，名称暂以任务 ID 显示';
    renderNotice();
    renderTableArea();
  }

  function onExecOk(rows) {
    execState = 'ok';
    executions = rows || [];
    renderTableArea();
  }

  function onExecError() {
    execState = 'error';
    renderTableArea();
  }

  // force=false 时同代次共享 inflight；force=true 启动新代次。
  // 刷新/重试按钮始终 force=true；公开 refresh/activate 共享当前 inflight。
  function startLoad(force) {
    if (!ensureSkeleton()) return Promise.resolve();
    if (!force) {
      const current = validInflight();
      if (current) return current.promise;
    }
    renderToken++;
    const token = renderToken;
    tasksState = 'loading';
    execState = 'loading';
    executions = [];
    nameById = {};
    notice = '';
    renderNotice();
    renderTableArea();

    const tasksP = Promise.resolve().then(() => api.listScheduledTasks());
    const execP = Promise.resolve().then(() => api.listScheduledTaskExecutionsAll({ limit: DEFAULT_LIMIT }));
    // 两个请求独立成败：完成一端即更新其负责区域，不等待另一端。
    tasksP.then(
      (tasks) => { if (isCurrent(token)) onTasksOk(tasks); },
      () => { if (isCurrent(token)) onTasksError(); },
    );
    execP.then(
      (rows) => { if (isCurrent(token)) onExecOk(rows); },
      () => { if (isCurrent(token)) onExecError(); },
    );
    const p = Promise.allSettled([tasksP, execP]).then(() => {
      if (inflight && inflight.promise === p) inflight = null;
    });
    inflight = { token: token, promise: p };
    return p;
  }

  function init() {
    // 复用当前 inflight 必须先于状态/骨架重置，否则待处理响应无处渲染。
    const current = validInflight();
    if (current) return current.promise;
    nameById = {};
    executions = [];
    notice = '';
    tasksState = 'idle';
    execState = 'idle';
    skeleton = null;
    return startLoad(true);
  }

  function refresh() {
    const current = validInflight();
    if (current) return current.promise;
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
