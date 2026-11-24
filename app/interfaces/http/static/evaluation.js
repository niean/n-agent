(function (global) {
  'use strict';
  // evaluations scoped tab: 评估闭环管理页（T21）。
  // 展示：概览（C 类配置 + 容量积压/日预算/producer 心跳/证据保留边界/
  // 低覆盖提示，M3a/b/e）、实验（列表/详情）、结果四态浏览与详情
  // （M3d：质量不通过/评估失败/跳过/通过；投递含等待与未关联）、
  // 待复核队列（低分/争议/异常三类筛选 + 审核后受控导出 M1：金标由
  // 审核人逐 case 提供，fixture/rubric 内容内联上传、服务端落盘并经
  // T12 校验）。数据来自 /chat/evaluations/*（同源、no-store 错误契约）。
  // 渲染一律 textContent（禁 HTML 注入通道），弹窗使用项目标准 modal
  // 帧（modal-backdrop/modal-dialog/providers-form，禁原生 alert/
  // confirm），时间一律 UTC+8（Asia/Shanghai）渲染。
  // Mount Point: <div class="tab-content" id="tab-evaluations"></div>
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  // NAGENT.api（management-api.js 提供 /chat/* 端点，评估面取 api.evaluations）
  const api = global.NAGENT.api;
  // NAGENT.modal（management-ui.js 提供 confirm/alert，禁原生 alert/confirm）
  const modal = global.NAGENT.modal;
  // m6：Langfuse 会话 URL 单源（management-ui.js）
  const LANGFUSE_URL = ui.LANGFUSE_SESSIONS_URL;
  const langfuseSessionUrl = ui.langfuseSessionUrl;
  const CONTAINER_ID = 'tab-evaluations';
  const REVIEW_REASONS = ['low_score', 'disputed', 'anomalous'];
  const REVIEW_REASON_LABELS = {
    low_score: '低分',
    disputed: '争议',
    anomalous: '异常',
  };
  // 结果四态（M3d）：质量不通过/评估失败/跳过/通过 + 投递态（等待/未关联）
  const RESULT_STATE_LABELS = {
    failed: '质量不通过',
    error: '评估失败',
    skipped: '跳过',
    passed: '通过',
  };

  // 跳过原因中文标签（SkipReason 封闭枚举，domain/evaluation/state.py）；
  // 未知值原样展示原始值，缺失显示"未记录"
  const SKIP_REASON_LABELS = {
    missing_reference: '缺少参考答案',
    not_applicable: '指标不适用',
    no_answer: '参考相关性标注中无相关项',
    evidence_missing: '缺少可核实证据',
    redacted_evidence: '证据已脱敏',
    evidence_oversized: '证据超出大小上限',
    budget_exhausted: '评估预算耗尽',
    consistency: '一致性检查未通过',
  };

  // 作业失败分类中文标签（EVAL_ERROR_TYPES 封闭词汇，
  // application/evaluation_telemetry.py）；未知值原样展示，缺失显示"未记录"
  const JOB_ERROR_TYPE_LABELS = {
    network: '网络错误',
    rate_limit: '限流',
    timeout: '超时',
    format_error: '响应格式错误',
    deadline_exceeded: '作业超期',
    attempt_cap_exceeded: '尝试次数上限耗尽',
    budget_exhausted: '预算耗尽',
    auth: '认证失败',
    conflict: '冲突',
    judge_error: 'Judge 调用错误',
    worker_error: 'worker 内部错误',
    cancelled: '已取消',
  };

  // Async guards: monotonic render token discards late responses; the
  // in-flight promise de-duplicates concurrent init/refresh calls.
  let renderToken = 0;
  let inflight = null;
  let currentView = null;
  // 同路由请求序号：筛选/重试会在同一 renderToken 内直接重渲染，
  // 仅靠 token 无法识别同路由旧响应；每视图递增，成功/失败分支双校验，
  // 防止旧响应覆盖新筛选、错误态或 Loading。deactivate 不重置，token 兜底。
  const viewRequestSeq = {
    overview: 0, datasets: 0, evaluators: 0, capabilities: 0, experiments: 0,
    results: 0, 'session-scores': 0, 'run-scores': 0,
    review: 0, annotations: 0, judgeModel: 0,
  };
  // Judge 供给共享缓存（plan-261116 T6）：providers 为 Provider 注册表
  // 列表快照（模型页 GET /chat/providers 刷新），projection 为 GET
  // /judge-model 投影快照，configVersion 为下一次 CAS 的 expected_version
  //（GET config_version 起步，PATCH 响应 version 跟进）；usedModels 由
  // 评估器列表合并（method=llm_judge 且 judge_model 非空，全串去重），
  // 供建表单 datalist。声明见下方"模型页"章节（TDZ 安全：仅运行时访问）。
  // datalist id 跨弹窗唯一序号
  let judgeModelListSeq = 0;
  // 当前待复核筛选状态（reason + threshold；跨渲染保留用户选择）
  let reviewReason = 'low_score';
  let reviewThreshold = '0.5';

  // 评估域列表分页状态（组件 2）：cursor 模式保存游标栈与页码，slice
  // 模式保存最近一次全量响应快照与页码；视图间相互隔离，进入/刷新由
  // resetViewPaging 统一回第 1 页，视图内翻页只改本闭包状态。
  let evaluatorCursorStack = [''];
  let evaluatorPageIndex = 0;
  let annotationCursorStack = [''];
  let annotationPageIndex = 0;
  // 人工标注当前成功页 items 快照：导出闭包只允许引用当前页，新请求
  // 发出前清空，catch/finally 不得恢复旧页（组件 4 导出合同）。
  let annotationPageItems = [];
  let datasetsItems = [];
  let datasetsPageIndex = 0;
  let experimentsItems = [];
  let experimentsPageIndex = 0;
  let capabilitiesItems = [];
  let capabilitiesPageIndex = 0;
  let reviewItems = [];
  let reviewPageIndex = 0;
  // 概览两个全量返回列表（评分趋势/Producer 心跳）的 slice 分页快照与
  // 页码，视图间隔离，进入/刷新由 resetViewPaging 统一回第 1 页。
  let trendItems = [];
  let trendPageIndex = 0;
  let producerItems = [];
  let producerPageIndex = 0;

  function resetEvaluatorPaging() {
    evaluatorCursorStack = [''];
    evaluatorPageIndex = 0;
  }
  function resetAnnotationPaging() {
    annotationCursorStack = [''];
    annotationPageIndex = 0;
  }
  function resetViewPaging(view) {
    if (view === 'evaluators') resetEvaluatorPaging();
    else if (view === 'annotations') resetAnnotationPaging();
    else if (view === 'results' || view === 'experiment-results'
      || view === 'session-scores' || view === 'run-scores') {
      resetResultsPaging();
      if (view === 'experiment-results') experimentResultExperiments = null;
    }
    else if (view === 'datasets') datasetsPageIndex = 0;
    else if (view === 'experiments') experimentsPageIndex = 0;
    else if (view === 'capabilities') capabilitiesPageIndex = 0;
    else if (view === 'review') reviewPageIndex = 0;
    else if (view === 'judge-model') judgeSupplyProviderPageIndex = 0;
    else if (view === 'overview') {
      trendPageIndex = 0;
      producerPageIndex = 0;
    }
  }
  // 模块内当前打开的弹窗（deactivate 时统一关闭）
  let openBackdrop = null;

  function root() {
    return ui.byId ? ui.byId(CONTAINER_ID) : document.getElementById(CONTAINER_ID);
  }

  function isActive() {
    const node = root();
    return !!(node && node.classList && node.classList.contains('active'));
  }

  // 统一时间格式化：服务端时间统一 UTC 存储，展示按 UTC+8 渲染
  // （与 observations-otel.js / chat.js 同模式）。
  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    const tz = new Date(d.getTime() + 8 * 3600 * 1000);
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    return tz.getUTCFullYear() + '-' + pad(tz.getUTCMonth() + 1) + '-' + pad(tz.getUTCDate())
      + ' ' + pad(tz.getUTCHours()) + ':' + pad(tz.getUTCMinutes()) + ':' + pad(tz.getUTCSeconds());
  }

  function el(tag, className, text) {
    const node = ui.el ? ui.el(tag, className) : document.createElement(tag);
    // 无 ui.el 的降级环境（harness/独立页面）不会代设 className，在此补齐，
    // 避免 muted/loading-state 等状态类丢失
    if (!ui.el && className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // 数值格式化：仅有限 number 或非空十进制数值字符串入格式，其余回落 '-'，
  // 禁止 Number('')/Number(null) 产生假零；en-US 千分位，小数最多 20 位
  // （默认三位会把微小差值和 CI 舍入成零）。只改显示，不回写原始数据。
  const NUMERIC_STRING_RE = /^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/;
  function formatNumber(value) {
    if (typeof value === 'number') {
      return Number.isFinite(value)
        ? value.toLocaleString('en-US', { maximumFractionDigits: 20 })
        : '-';
    }
    if (typeof value === 'string') {
      const trimmed = value.trim();
      if (!trimmed || !NUMERIC_STRING_RE.test(trimmed)) return '-';
      const n = Number(trimmed);
      return Number.isFinite(n)
        ? n.toLocaleString('en-US', { maximumFractionDigits: 20 })
        : '-';
    }
    return '-';
  }

  // Tips 节点全局序号：titleGroup 与 filterItem 共用，保证 id 跨刷新唯一。
  let evaluationsTipSeq = 0;
  // 已注册浮层：横向滚动容器（overflow:auto）会裁切绝对定位浮层，
  // 打开时改用 fixed 定位按触发器视口坐标重排。
  const floatingTips = [];
  let floatingListenersBound = false;

  function positionFloatingTip(group, tip) {
    const rect = group.getBoundingClientRect();
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const tipWidth = Math.min(480, Math.max(160, viewportWidth - 16));
    let left = rect.left;
    const maxLeft = viewportWidth - tipWidth - 8;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    tip.style.position = 'fixed';
    tip.style.top = (rect.bottom + 8) + 'px';
    tip.style.left = left + 'px';
    tip.style.maxWidth = tipWidth + 'px';
  }

  function ensureFloatingListeners() {
    if (floatingListenersBound || typeof window === 'undefined' || !window.addEventListener) return;
    floatingListenersBound = true;
    const update = () => {
      for (let i = floatingTips.length - 1; i >= 0; i--) {
        const floater = floatingTips[i];
        if ('isConnected' in floater.group && !floater.group.isConnected) {
          floatingTips.splice(i, 1);
          continue;
        }
        if (floater.open) positionFloatingTip(floater.group, floater.tip);
      }
    };
    window.addEventListener('scroll', update, true);
    window.addEventListener('resize', update);
  }

  // 注册浮层定位；harness 缺布局 API（无 getBoundingClientRect）时守卫跳过，
  // 显隐仍完全由 CSS :hover/:focus-within 控制。
  function registerFloatingTip(group, tip) {
    if (typeof group.getBoundingClientRect !== 'function') return;
    const floater = { group: group, tip: tip, open: false };
    const openTip = () => {
      floater.open = true;
      positionFloatingTip(group, tip);
    };
    const closeTip = () => {
      floater.open = false;
      tip.style.position = '';
      tip.style.top = '';
      tip.style.left = '';
      tip.style.maxWidth = '';
    };
    group.addEventListener('mouseenter', openTip);
    group.addEventListener('mouseleave', closeTip);
    group.addEventListener('focusin', openTip);
    group.addEventListener('focusout', closeTip);
    floatingTips.push(floater);
    ensureFloatingListeners();
  }

  // 标题补充说明三件套（panel-title-group/panel-title/panel-tips），
  // 与 external-memory.js 同模式；根节点用 span 以便合法嵌入 h3 与 th。
  // 无 tip 时退化为纯标题，不产生无意义的焦点停靠。
  function titleGroup(titleText, tipText) {
    const group = el('span', 'panel-title-group');
    group.appendChild(el('span', 'panel-title', titleText));
    if (tipText) {
      const tipId = 'evaluations-tip-' + (++evaluationsTipSeq);
      const tip = el('span', 'panel-tips muted', tipText);
      tip.id = tipId;
      tip.setAttribute('role', 'tooltip');
      group.setAttribute('tabindex', '0');
      group.setAttribute('aria-describedby', tipId);
      group.appendChild(tip);
      registerFloatingTip(group, tip);
    }
    return group;
  }

  function alertMessage(message) {
    if (modal && typeof modal.alert === 'function') {
      modal.alert(String(message));
    }
  }

  function kvRow(label, value) {
    const row = el('div', 'policy-item');
    row.appendChild(el('span', 'policy-k', label + '：'));
    const content = el('span', 'policy-v');
    if (value instanceof Node) content.appendChild(value);
    else content.textContent = value === undefined || value === null || value === '' ? '-' : String(value);
    row.appendChild(content);
    return row;
  }

  function badgeVariant(status) {
    const s = String(status || '');
    if (s === '通过' || s === 'verified' || s === 'created' || s === 'succeeded' || s === '已启用') return 'success';
    if (s === '质量不通过' || s === 'error' || s === '评估失败' || s === 'dead_letter') return 'danger';
    return 'warning';
  }

  function statusBadge(status) {
    return el('span', 'badge badge--' + badgeVariant(status), status || '-');
  }

  // 共享表格构建（与 observations-otel.js 同款 document-table 组件）。
  // headers 允许字符串或 { label, numeric?, tip? }；numeric 为唯一数值列
  // 声明，纯文本数值单元格右对齐并经 formatNumber 千分位；Node 单元格
  // （badge/按钮）保持原对象不格式化。
  function table(headers, rows) {
    const t = el('table', 'document-table');
    const thead = document.createElement('thead');
    const htr = document.createElement('tr');
    const normalized = headers.map((header) => (typeof header === 'string'
      ? { label: header, numeric: false, tip: '' }
      : { label: String(header.label || ''), numeric: !!header.numeric, tip: header.tip || '' }));
    normalized.forEach((header) => {
      const th = document.createElement('th');
      th.setAttribute('scope', 'col');
      if (header.numeric) th.className = 'document-table__numeric';
      if (header.tip) {
        th.appendChild(titleGroup(header.label, header.tip));
      } else {
        th.textContent = header.label;
      }
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    t.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach((cells) => {
      const tr = document.createElement('tr');
      cells.forEach((cell, index) => {
        const td = document.createElement('td');
        if (cell instanceof Node) {
          td.appendChild(cell);
        } else if (normalized[index] && normalized[index].numeric) {
          td.className = 'document-table__numeric';
          td.textContent = cell === undefined || cell === null || cell === '' ? '-' : formatNumber(cell);
        } else {
          td.textContent = cell === undefined || cell === null || cell === '' ? '-' : String(cell);
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    return t;
  }

  // 面板壳：status-panel > panel-header(panel-title-group + panel-actions)
  // + panel-body，与 external-memory.js/tasks.js 全站面板结构对齐。
  // 旧调用解构 { panel, body } 不受影响；主操作经 body.evaluationsHeaderActions
  // 挂载，避免依赖 harness/真实 DOM 的 querySelector 路径。
  function sectionPanel(title, options) {
    options = options || {};
    const panel = el('section', 'status-panel evaluations-panel');
    const header = el('div', 'panel-header');
    const heading = el('h3', 'evaluations-panel__heading');
    heading.appendChild(titleGroup(title, options.tip));
    header.appendChild(heading);
    const actions = el('span', 'panel-actions');
    (options.actions || []).forEach((button) => actions.appendChild(button));
    header.appendChild(actions);
    panel.appendChild(header);
    const body = el('div', 'panel-body');
    panel.appendChild(body);
    body.evaluationsHeaderActions = actions;
    return { panel: panel, body: body, header: header, actions: actions };
  }

  // Loading：优先共享 helper ui.renderLoading（不返回节点，不可依赖返回值
  // 清理）；harness/无 ui 环境降级为 div.muted.loading-state 并返回节点。
  function showLoading(container, message) {
    const text = message || '加载中...';
    if (ui.renderLoading) {
      ui.renderLoading(container, text);
      return null;
    }
    const node = el('div', 'muted loading-state', text);
    container.appendChild(node);
    return node;
  }

  // 区块标题：视图内分块统一走 titleGroup（panel-title-group + 可选 tip），
  // 外层 div 保证 span 组以块级方式占据一行
  function blockTitle(titleText, tipText) {
    const block = el('div', 'evaluations-block-title');
    block.appendChild(titleGroup(titleText, tipText));
    return block;
  }

  // 概览 Sector 壳：复用 sectionPanel（status-panel + panel-header +
  // panel-body），对齐安全-概览 security.js sector() 的分块结构；概览备注
  // 按主题分 Sector 展示，禁止 kvRow 无边界堆叠。
  function overviewSector(titleText, tipText) {
    return sectionPanel(titleText, tipText ? { tip: tipText } : {});
  }

  // 概览备注网格：复用安全页 policy-cfg 双列网格 + policy-item kv 行，
  // 入参为 [label, value] 二元组列表。
  function overviewGrid(rows) {
    const grid = el('div', 'policy-cfg');
    rows.forEach(([label, value]) => grid.appendChild(kvRow(label, value)));
    return grid;
  }

  // 置灰但不隐藏的不可用操作：无 click handler，harness findButton 不可见。
  function disabledButton(label) {
    const btn = el('button', 'btn');
    btn.type = 'button';
    btn.textContent = label;
    btn.disabled = true;
    return btn;
  }

  // 评估域列表统一每页数量，禁止各视图散落字面量。待复核的 50 是后端
  // 窗口大小而非每页大小，使用独立常量 REVIEW_WINDOW_LIMIT，不得引用本常量。
  const LIST_PAGE_SIZE = 20;
  const REVIEW_WINDOW_LIMIT = 50;

  // 统一翻页条：div.evaluations-pager + info + 上一页/下一页（始终占位、
  // 边界 disabled 不隐藏，保持布局不跳动）。有真实 totalPages 显示
  // 「第 N/M 页」；cursor 模式后端无总数，只显示「第 N 页」。
  function pagerBar(options) {
    const row = el('div', 'evaluations-pager');
    const info = el('span', 'evaluations-pager__info');
    const pageNo = options.pageIndex + 1;
    info.textContent = '第 ' + pageNo
      + (options.totalPages !== undefined ? '/' + options.totalPages : '') + ' 页';
    row.appendChild(info);
    if (options.hasPrev) row.appendChild(actionButton('上一页', options.onPrev));
    else row.appendChild(disabledButton('上一页'));
    if (options.hasNext) row.appendChild(actionButton('下一页', options.onNext));
    else row.appendChild(disabledButton('下一页'));
    return row;
  }

  // slice 模式通用切片与总页数：空数组总页数为 1，杜绝第 1/0 页。
  function slicePage(items, pageIndex) {
    const totalPages = Math.max(1, Math.ceil(items.length / LIST_PAGE_SIZE));
    const start = pageIndex * LIST_PAGE_SIZE;
    return { pageItems: items.slice(start, start + LIST_PAGE_SIZE), totalPages: totalPages };
  }

  // 筛选区：filterBar 弹性容器 + filterItem（Label+Control+HelpText）。
  function filterBar() { return el('div', 'evaluations-filter'); }

  function filterItem(labelText, control, helpText) {
    const item = el('label', 'evaluations-filter__item');
    item.appendChild(el('span', 'evaluations-filter__label', labelText));
    item.appendChild(control);
    return appendFilterHelp(item, control, helpText);
  }

  // 无标题筛选项：控件自描述（placeholder/首项文本），标题仅进 aria-label
  function filterControl(labelText, control, helpText) {
    const item = el('span', 'evaluations-filter__item');
    control.setAttribute('aria-label', labelText);
    item.appendChild(control);
    return appendFilterHelp(item, control, helpText);
  }

  function appendFilterHelp(item, control, helpText) {
    if (helpText) {
      const help = el('small', 'evaluations-filter__help muted', helpText);
      help.id = 'evaluations-help-' + (++evaluationsTipSeq);
      control.setAttribute('aria-describedby', help.id);
      item.appendChild(help);
    }
    return item;
  }

  // 查询过滤器按钮组（非 tab 面板）：role=group + 可访问名称 +
  // aria-pressed 表达选中态，保留原生 Tab/Enter/Space 操作。
  function choiceGroup(groupLabel, options, current, onSelect) {
    const group = el('div', 'evaluations-filter-group');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', groupLabel);
    options.forEach(([value, label]) => {
      const btn = actionButton(label, () => onSelect(value));
      btn.setAttribute('data-value', value);
      btn.setAttribute('aria-pressed', value === current ? 'true' : 'false');
      if (value === current) btn.classList.add('btn--primary');
      group.appendChild(btn);
    });
    return group;
  }

  // 焦点恢复：仅组按钮主动触发筛选时记录目标，重渲染同步阶段结束后
  // 立即把焦点移到同组同值的新按钮（await 之前完成，响应迟到不会抢焦）。
  // 程序 refresh/初次加载不写入 pendingGroupFocus，不自动聚焦。
  const groupButtonRegistry = new Map();
  let pendingGroupFocus = null;
  function focusableChoiceGroup(groupKey, groupLabel, options, current, onSelect) {
    const valueToButton = new Map();
    const group = choiceGroup(groupLabel, options, current, (value) => {
      pendingGroupFocus = { groupKey: groupKey, value: value };
      onSelect(value);
      if (pendingGroupFocus && pendingGroupFocus.groupKey === groupKey) {
        const target = groupButtonRegistry.get(groupKey)
          && groupButtonRegistry.get(groupKey).get(value);
        if (target && typeof target.focus === 'function') target.focus();
        pendingGroupFocus = null;
      }
    });
    // 真实 DOM 的 children 是 HTMLCollection（无数组方法），必须先 Array.from
    Array.from(group.children)
      .filter(node => node.tag === 'button')
      .forEach(btn => valueToButton.set(btn.getAttribute('data-value'), btn));
    groupButtonRegistry.set(groupKey, valueToButton);
    return group;
  }

  // 宽表横向滚动包裹：表格在容器内滚动，不产生整页横向溢出。
  function scrollTable(tableNode) {
    const wrap = el('div', 'evaluations-table-scroll');
    wrap.appendChild(tableNode);
    return wrap;
  }

  function actionButton(label, onClick) {
    const btn = el('button', 'btn');
    btn.type = 'button';
    btn.textContent = label;
    btn.disabled = false;
    btn.addEventListener('click', onClick);
    return btn;
  }

  function externalLink(href, label) {
    // 与 observations-otel.js 外链同款 otel-link 类 + noopener 约束
    const a = el('a', 'otel-link', label || href);
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    return a;
  }

  function renderErrorState(body, message, onRetry) {
    body.textContent = '';
    const err = el('div', 'error-state', message || '加载失败');
    body.appendChild(err);
    if (typeof onRetry === 'function') {
      body.appendChild(actionButton('重试', onRetry));
    }
  }

  // ------------------------------------------------------------------
  // 项目标准弹窗帧（M2：modal-backdrop / modal-dialog / providers-form /
  // modal-header / modal-close，与 management-ui.js / artifacts.js 同款）
  // ------------------------------------------------------------------

  function buildFrame(title) {
    const backdrop = el('div', 'modal-backdrop evaluations-modal');
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = el('div', 'modal-header');
    const titleEl = document.createElement('h4');
    titleEl.textContent = title;
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'modal-close';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭');
    header.append(titleEl, closeBtn);
    form.appendChild(header);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    return { backdrop: backdrop, form: form, closeBtn: closeBtn, dialog: dialog };
  }

  function showBackdrop(backdrop) {
    openBackdrop = backdrop;
    document.body.appendChild(backdrop);
    backdrop.hidden = false;
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeBackdrop(backdrop);
    });
    return backdrop;
  }

  function closeBackdrop(backdrop) {
    if (openBackdrop === backdrop) openBackdrop = null;
    backdrop.remove();
  }

  // N5：详情类 Modal 挂载公共函数——不响应空白处点击关闭（项目约定：
  // 详情 Modal 不做 backdrop click 关闭），只挂打开逻辑与关闭按钮。
  // 表单类 Modal 仍走 showBackdrop（带 backdrop click 关闭）。
  function mountDetailModal(frame) {
    openBackdrop = frame.backdrop;
    document.body.appendChild(frame.backdrop);
    frame.backdrop.hidden = false;
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));
  }

  function formActions(form, cancelLabel, submitLabel) {
    const actions = el('div', 'providers-form__actions');
    const cancelBtn = actionButton(cancelLabel, () => closeBackdrop(currentBackdropOf(form)));
    cancelBtn.type = 'button';
    actions.appendChild(cancelBtn);
    if (submitLabel) {
      const submitBtn = el('button', 'btn btn--primary', submitLabel);
      submitBtn.type = 'submit';
      actions.appendChild(submitBtn);
    }
    form.appendChild(actions);
    return actions;
  }

  function currentBackdropOf(node) {
    let cursor = node;
    while (cursor && cursor !== document.body) {
      if (cursor.classList && cursor.classList.contains('modal-backdrop')) return cursor;
      cursor = cursor.parentElement;
    }
    return openBackdrop;
  }

  function inputRow(form, key, label, current) {
    const row = el('div', 'policy-item');
    row.appendChild(el('span', 'policy-k', label + '：'));
    const input = document.createElement('input');
    input.name = key;
    input.value = current === undefined || current === null ? '' : String(current);
    row.appendChild(input);
    form.appendChild(row);
    return input;
  }

  // ------------------------------------------------------------------
  // 概览（M3a/b/e：C 类配置 + 容量积压 + 日预算 + producer 心跳 +
  // 证据保留边界 + 低覆盖提示）
  // ------------------------------------------------------------------

  // spec 4.5：无值分组 count=0 其余统计 null——不得把 null 当 0 渲染成
  // 0% 成功或 100% 质量，无值一律"暂无有效评分"
  function trendStatsText(group) {
    const outputType = group.output_type;
    if (outputType === 'number') {
      if (!group.count) return '暂无有效评分';
      return 'count=' + group.count + '，均值 ' + group.mean
        + '，min ' + group.min + '，max ' + group.max;
    }
    if (outputType === 'boolean') {
      const total = (group.true_count || 0) + (group.false_count || 0);
      if (!total || group.true_ratio === null || group.true_ratio === undefined) {
        return '暂无有效评分';
      }
      return 'true ' + group.true_count + ' / false ' + group.false_count
        + '（true_ratio ' + Math.round(group.true_ratio * 1000) / 10 + '%）';
    }
    const counts = group.counts || {};
    const keys = Object.keys(counts);
    const total = keys.reduce((sum, key) => sum + (counts[key] || 0), 0);
    if (!total) return '暂无有效评分';
    return keys.map((key) => key + ':' + counts[key]).join('，');
  }

  // 执行态与质量态分开计数（spec 4.5：error/skipped 不当 0 也不当通过）
  function trendExecText(group) {
    const statusCounts = group.status_counts || {};
    return '异常 ' + (statusCounts.error || 0)
      + ' / 跳过 ' + (statusCounts.skipped || 0);
  }

  async function renderOverview(body, token) {
    body.textContent = '';
    if (body.evaluationsHeaderActions) body.evaluationsHeaderActions.textContent = '';
    const seq = ++viewRequestSeq.overview;
    const wrap = el('div', 'evaluations-overview');
    body.appendChild(wrap);
    showLoading(wrap, '评估概览加载中...');
    let resolved;
    let overview;
    let experiments;
    let analytics;
    try {
      [resolved, overview, experiments, analytics] = await Promise.all([
        api.evaluations.getConfig(),
        api.evaluations.overview(),
        api.evaluations.listExperiments(),
        api.evaluations.getAnalytics(),
      ]);
    } catch (err) {
      // Mi7：重试闭包取点击时刻的 renderToken，避免捕获过期 token 后
      // 重试渲染被守卫直接丢弃
      if (token !== renderToken || seq !== viewRequestSeq.overview) return;
      renderErrorState(body, '评估概览加载失败：' + (err && err.message ? err.message : ''), () => renderOverview(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.overview) return;
    wrap.textContent = '';
    const cfg = resolved.config || {};
    // M3a：容量积压与日预算用量
    const capacity = overview.capacity || {};
    const budget = overview.budget || {};
    const ceiling = budget.daily_call_ceiling;
    // M3b：低覆盖提示（实验尚无任何 current 结果）
    const counts = overview.experiment_results || {};
    const lowCoverage = (experiments.items || [])
      .filter((item) => !counts[item.experiment_id])
      .map((item) => item.experiment_id);
    // 评分趋势（Task 13/spec 4.5）：按 (DefinitionKey, 版本, provenance)
    // 分组展示样本数/时间窗/分布；时间窗 UTC 展示 UTC+8；覆盖率 unknown
    // 须解释"缺少应评估目标总数"，不宣称采样覆盖率
    const trend = analytics || {};
    const trendWindow = trend.window || {};
    const trendCoverage = trend.coverage || {};
    const trendGroups = Array.isArray(trend.groups) ? trend.groups : [];
    const states = overview.producer_states || [];

    // 列表 Sector 1：评分趋势。全量返回接口走 slice 模式（LIST_PAGE_SIZE）。
    // 统计时间窗/快照/覆盖率口径/低覆盖提示等备注收进标题 hover tips
    //（PRD 161 行精调），列表下不再保留备注块。
    trendItems = trendGroups;
    trendPageIndex = 0;
    // tips 分事项换行（.panel-tips white-space: pre-line），一条一行，
    // 低覆盖实验 id 逐行列出
    const trendTipLines = [
      '统计时间窗：' + formatTime(trendWindow.from) + ' ~ ' + formatTime(trendWindow.to),
      '统计快照：' + formatTime(trend.snapshot_at),
      '覆盖率口径：' + (trendCoverage.status || 'unknown')
        + '（' + (trendCoverage.reason || '缺少应评估目标总数') + '）',
      '趋势按 UTC+8 自然日分桶',
    ];
    if (lowCoverage.length) {
      trendTipLines.push('低覆盖（以下实验尚无评估结果 0 条）：');
      lowCoverage.forEach((id) => trendTipLines.push('- ' + id));
    }
    const trendTip = trendTipLines.join('\n');
    const trendSection = overviewSector('评分趋势', trendTip);
    wrap.appendChild(trendSection.panel);
    const drawTrend = () => {
      trendSection.body.textContent = '';
      const sliced = slicePage(trendItems, trendPageIndex);
      if (!trendItems.length) {
        // 空态三要素：前缀事实 + 原因/下一步 + 筛选影响说明
        trendSection.body.appendChild(el('div', 'empty-state',
          '暂无有效评分。当前统计时间窗内评估作业尚未产出有效值'
            + '（结果可能仍在排队，或全部为异常/跳过）；请等待评估完成或检查 worker 作业状态。'
            + '本概览为全局汇总，不受查询筛选条件影响。'));
      } else {
        trendSection.body.appendChild(scrollTable(table(
          ['指标', '版本', '来源', '输出类型', { label: '样本数', numeric: true }, '有效值统计', '异常/跳过'],
          sliced.pageItems.map((group) => [
            group.definition_id,
            group.definition_version,
            group.provenance,
            group.output_type,
            group.total_count,
            trendStatsText(group),
            trendExecText(group),
          ]),
        )));
      }
      trendSection.body.appendChild(pagerBar({
        pageIndex: trendPageIndex,
        hasPrev: trendPageIndex > 0,
        hasNext: trendPageIndex + 1 < sliced.totalPages,
        totalPages: sliced.totalPages,
        onPrev: () => {
          trendPageIndex -= 1;
          drawTrend();
        },
        onNext: () => {
          trendPageIndex += 1;
          drawTrend();
        },
      }));
      // 统计时间窗/快照/覆盖率口径/低覆盖提示均已收进标题 hover tips
      //（见 trendTip），列表下不再保留备注块
    };
    drawTrend();

    // 列表 Sector 2：Producer 心跳（M3a，仅有心跳数据时渲染），同样走
    // 标准 slice 分页。
    if (states.length) {
      producerItems = states;
      producerPageIndex = 0;
      const producerSection = overviewSector('Producer 心跳');
      wrap.appendChild(producerSection.panel);
      const drawProducer = () => {
        producerSection.body.textContent = '';
        const sliced = slicePage(producerItems, producerPageIndex);
        producerSection.body.appendChild(scrollTable(table(
          ['Producer',
            { label: 'Epoch', numeric: true },
            { label: '累计捕获', numeric: true },
            { label: '捕获错误', numeric: true },
            { label: '队列满', numeric: true },
            '观测时间'],
          sliced.pageItems.map((s) => [
            s.producer_id,
            s.counter_epoch,
            s.captured_count,
            s.capture_error_count,
            s.queue_full_count,
            formatTime(s.observed_at),
          ]),
        )));
        producerSection.body.appendChild(pagerBar({
          pageIndex: producerPageIndex,
          hasPrev: producerPageIndex > 0,
          hasNext: producerPageIndex + 1 < sliced.totalPages,
          totalPages: sliced.totalPages,
          onPrev: () => {
            producerPageIndex -= 1;
            drawProducer();
          },
          onNext: () => {
            producerPageIndex += 1;
            drawProducer();
          },
        }));
      };
      drawProducer();
    }

    // 运行配置 Sector：修订配置入口挂本 Sector 右上角（PRD 160 行精调，
    // 不再放在外层页壳 header）
    const configSection = sectionPanel('运行配置', {
      actions: [actionButton('修订配置',
        () => openConfigModal(resolved, () => renderOverview(body, renderToken)))],
    });
    configSection.body.appendChild(overviewGrid([
      ['评估开关', cfg.evaluation_enabled ? '启用' : '停用'],
      ['采样率', cfg.evaluation_sampling_rate],
      ['Worker 并发', cfg.evaluation_worker_concurrency],
      ['配置版本', resolved.version],
      ['最近更新', formatTime(resolved.updated_at)],
    ]));
    wrap.appendChild(configSection.panel);

    // 容量与预算 Sector：M3a 容量积压/日预算 + M3e 证据保留边界
    // （7 天到期清理，评分值不受影响）。
    const capacitySection = overviewSector('容量与预算');
    const capacityRows = [];
    if (!capacity.probe_available) {
      capacityRows.push(['容量', '容量探针不可用']);
    } else {
      capacityRows.push([
        '待处理作业（积压）',
        capacity.pending_jobs + ' / ' + capacity.max_pending_jobs,
      ]);
      capacityRows.push([
        '证据正文占用',
        capacity.evidence_bytes + ' / ' + capacity.max_evidence_bytes + ' 字节',
      ]);
    }
    capacityRows.push([
      '当日 Judge 调用（预算）',
      (budget.settled_calls || 0) + ' 结算 / ' + (budget.reserved_calls || 0)
        + ' 预留' + (ceiling ? (' / 上限 ' + ceiling) : ' / 未设上限'),
    ]);
    capacityRows.push([
      '证据快照保留',
      (overview.evidence_retention_days || 7) + ' 天（到期清理快照正文；'
        + '评分值与 Langfuse 历史投影不受影响）',
    ]);
    capacitySection.body.appendChild(overviewGrid(capacityRows));
    if (capacity.probe_available) {
      const pendingRatio = capacity.max_pending_jobs
        ? capacity.pending_jobs / capacity.max_pending_jobs : 0;
      if (pendingRatio >= 0.8) {
        // N1：长句提示不用状态徽章（徽章只承载封闭短状态）
        capacitySection.body.appendChild(el('div', 'evaluations-hint',
          '积压接近容量上限，请检查 worker'));
      }
    }
    wrap.appendChild(capacitySection.panel);
  }

  // C 类配置 CAS 修订弹窗（禁原生 alert/confirm；冲突 409 不自动重试）。
  // M4（质量复审）：输入框取值恒为字符串，数值字段必须解析后提交——
  // 字符串直传会被 domain 数值类型校验拒绝（422），配置无法保存。
  const NUMERIC_CONFIG_FIELDS = {
    evaluation_sampling_rate: 'float',
    evaluation_failure_sampling_rate: 'float',
    evaluation_worker_concurrency: 'int',
    evaluation_daily_call_ceiling: 'int',
  };

  function parseConfigFieldValue(key, raw) {
    const kind = NUMERIC_CONFIG_FIELDS[key];
    if (!kind) return raw;
    const parsed = kind === 'int' ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
    return Number.isNaN(parsed) ? null : parsed;
  }

  // 会话类型白名单复选框选项（顺序对齐 SessionSource 枚举，dashboard 在
  // 首，与会话来源领域定义同源）。值集合与 chat.js SESSION_SOURCE_OPTIONS
  // 一致；两文件模块独立禁止互引，有序值序列一致性由静态合同测试比对。
  // 「评估配置」弹窗与概览页「修订配置」弹窗共用本数组。
  const SESSION_EVAL_SOURCE_OPTIONS = [
    ['dashboard', 'dashboard'], ['api', 'api'], ['cli', 'cli'],
    ['feishu', 'feishu'], ['dingtalk', 'dingtalk'], ['wecom', 'wecom'],
    ['acp', 'acp'], ['schedule', 'schedule'], ['curator', 'curator'],
    ['task', 'task'], ['delegation', 'delegation'], ['experiment', 'experiment'],
  ];

  // 会话类型白名单解析：字段缺失（旧投影/无 DB 行前的桩数据）回落领域
  // 默认（dashboard），与后端 EvaluationConfig.evaluation_session_source_
  // allowlist 默认值一致；形状非法（非字符串数组）返回 null，由调用方
  // 禁止保存。
  function resolveSessionEvalAllowlist(raw) {
    if (raw === undefined || raw === null) return ['dashboard'];
    if (!Array.isArray(raw) || !raw.every((item) => typeof item === 'string')) {
      return null;
    }
    return raw;
  }

  // 无序集合相等：勾选集与打开时快照比较用（后端会按枚举序归一输出，
  // 顺序差异不算变更，避免无谓版本推进）
  function sameStringValues(a, b) {
    if (a.length !== b.length) return false;
    const bSet = new Set(b);
    return a.every((item) => bSet.has(item));
  }

  // 会话类型白名单复选框组：两列排布经内联 grid style（样式全部复用
  // 共享类，不新增 CSS 文件）；标签文本复用 evaluations-filter__label
  // （共享类显式声明字号 token，不依赖 body 继承）。label 挂
  // evaluations-checkbox-label：覆盖全局 label 的 flex-direction:column
  // （styles.css 全局规则会把 checkbox 与文本压成上下两行），名称在左、
  // checkbox 在右，文本 span 撑满剩余宽度使同列 checkbox 右缘垂直对齐。
  function buildSessionSourceCheckboxGroup(checkedValues) {
    const group = el('div');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', '会话类型白名单');
    group.setAttribute('style',
      'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));column-gap:12px;row-gap:4px;');
    const inputs = [];
    SESSION_EVAL_SOURCE_OPTIONS.forEach(([value, label]) => {
      const item = el('label', 'evaluations-checkbox-label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'evaluation_session_source_allowlist';
      input.value = value;
      input.checked = checkedValues.includes(value);
      // 名称在前、checkbox 在后；点击 label 文本仍可切换勾选
      item.append(el('span', 'evaluations-filter__label', label), input);
      group.appendChild(item);
      inputs.push(input);
    });
    return { group: group, inputs: inputs };
  }

  function openConfigModal(resolved, onSaved) {
    const frame = buildFrame('修订评估配置（CAS）');
    const form = frame.form;
    const base = resolved.config || {};
    // 评估开关为布尔语义：select 控件（启用/停用），提交严格 boolean，
    // 不再使用自由文本 true/false 输入。
    const enabledControl = selectControl('evaluation_enabled',
      [['true', '启用'], ['false', '停用']],
      base.evaluation_enabled === undefined || base.evaluation_enabled === null
        ? 'true' : String(!!base.evaluation_enabled));
    form.appendChild(filterItem('评估开关', enabledControl, '控制运行时评估开关'));
    const fields = [
      ['evaluation_sampling_rate', '采样率', base.evaluation_sampling_rate],
      ['evaluation_failure_sampling_rate', '失败采样率', base.evaluation_failure_sampling_rate],
      ['evaluation_worker_concurrency', 'Worker 并发（重启后生效）', base.evaluation_worker_concurrency],
      ['evaluation_daily_call_ceiling', '日调用上限（即时生效）', base.evaluation_daily_call_ceiling],
    ];
    const inputs = { evaluation_enabled: enabledControl };
    fields.forEach(([key, label, current]) => {
      inputs[key] = inputRow(form, key, label, current);
    });
    // 会话类型白名单复选框组（与 session-scores 视图「评估配置」弹窗共用
    // SESSION_EVAL_SOURCE_OPTIONS）：勾选集与打开时 resolved 数组不一致才
    // 把该字段混入 patch（清空时显式提交 []），数组直传、不经过
    // parseConfigFieldValue。形状非法（非字符串数组）时契约返回 null：
    // 与 session 弹窗同语义，渲染警示且不提供保存路径，禁止静默回落 []。
    const baseAllowlist = resolveSessionEvalAllowlist(
      base.evaluation_session_source_allowlist);
    if (!baseAllowlist) {
      form.appendChild(el('div', 'error-state', '配置数据形状非法，禁止保存'));
      showBackdrop(frame.backdrop);
      frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));
      return;
    }
    const allowlistGroup = buildSessionSourceCheckboxGroup(baseAllowlist);
    const allowlistRow = el('div', 'policy-item');
    allowlistRow.appendChild(el('span', 'policy-k', '会话类型白名单：'));
    allowlistRow.appendChild(allowlistGroup.group);
    form.appendChild(allowlistRow);
    const errorLine = el('div', 'error-state');
    errorLine.hidden = true;
    form.appendChild(errorLine);
    formActions(form, '取消', '保存');
    // 统一保存互斥锁（与会话评分「评估配置」弹窗同语义）：处理函数开始
    // 先判断 saving，finally 恢复，不能只禁按钮却允许第二次 submit 事件
    const configSaveBtn = form.querySelector('[type="submit"]');
    configSaveBtn.disabled = false;
    let configSaving = false;
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (configSaving) return;
      configSaving = true;
      configSaveBtn.disabled = true;
      try {
        const patch = {};
        let invalid = null;
        // select 值恒为 'true'/'false'，变更时提交严格布尔
        const enabledRaw = enabledControl.value;
        if (enabledRaw !== String(!!base.evaluation_enabled)) {
          patch.evaluation_enabled = normalizeBool(enabledRaw);
        }
        fields.forEach(([key]) => {
          const raw = inputs[key].value.trim();
          if (raw === '') return;
          if (raw !== String(base[key])) {
            const parsed = parseConfigFieldValue(key, raw);
            if (parsed === null) {
              invalid = invalid || {};
              invalid[key] = raw;
              return;
            }
            patch[key] = parsed;
          }
        });
        // 白名单勾选集与打开时快照不一致才提交（数组直传，清空为 []）
        const allowlistSelected = allowlistGroup.inputs
          .filter((input) => input.checked)
          .map((input) => input.value);
        if (!sameStringValues(allowlistSelected, baseAllowlist)) {
          patch.evaluation_session_source_allowlist = allowlistSelected;
        }
        if (invalid) {
          errorLine.hidden = false;
          errorLine.textContent = '数值字段格式无效：'
            + Object.keys(invalid).join(', ') + '（需为有效数字）';
          return;
        }
        if (Object.keys(patch).length === 0) {
          closeBackdrop(frame.backdrop);
          return;
        }
        try {
          await api.evaluations.patchConfig({
            patch: patch,
            expected_version: resolved.version,
          });
          closeBackdrop(frame.backdrop);
          alertMessage('评估配置已保存');
          if (typeof onSaved === 'function') onSaved();
        } catch (err) {
          const code = err && err.message ? err.message : '';
          if (code === 'conflict') {
            errorLine.textContent = '配置已被他人修改（版本冲突），请关闭后重试';
          } else {
            errorLine.textContent = '保存失败：' + code;
          }
          errorLine.hidden = false;
        }
      } finally {
        configSaving = false;
        configSaveBtn.disabled = false;
      }
    });
  }

  function normalizeBool(value) {
    if (value === undefined) return undefined;
    if (value === 'true') return true;
    if (value === 'false') return false;
    return value;
  }

  // 会话评分视图「评估配置」弹窗（spec-261119）：每次打开先 GET 最新
  // resolved + version（CAS 快照），复选框勾选集取白名单数组；提交构造
  // {patch: {evaluation_session_source_allowlist: 勾选数组}, expected_version}，
  // 空名单显式提交 []（= 关闭会话评估），保存保持可用。与概览页修订配置
  // 弹窗共用保存互斥锁语义：saving 期间重复 submit 直接忽略，409 后锁死
  // 旧快照保存按钮（重新打开读取新版本后才允许提交），不自动重放。
  async function openSessionEvalConfigModal(onSaved) {
    const tokenAtOpen = renderToken;
    const viewAtOpen = currentView;
    let resolved;
    try {
      resolved = await api.evaluations.getConfig();
    } catch (err) {
      // GET 失败：提示且不渲染保存入口；切页后迟到的失败也不弹过期提示
      if (tokenAtOpen === renderToken && viewAtOpen === currentView) {
        alertMessage('评估配置加载失败：' + (err && err.message ? err.message : ''));
      }
      return;
    }
    // 打开期间切页/重渲染：过期响应不得弹出 Modal
    if (tokenAtOpen !== renderToken || viewAtOpen !== currentView) return;
    const config = (resolved && resolved.config) || {};
    const version = resolved && typeof resolved.version === 'number'
      && Number.isInteger(resolved.version) && resolved.version >= 0
      ? resolved.version : null;
    const baseAllowlist = resolveSessionEvalAllowlist(
      config.evaluation_session_source_allowlist);
    const frame = buildFrame('评估配置');
    const form = frame.form;
    const note = el('div', 'evaluations-note',
      '勾选允许进入线上评估的会话类型；保存后立即生效。');
    form.appendChild(note);
    if (!baseAllowlist || version === null) {
      // GET 成功但形状非法：展示警示、不渲染保存按钮，禁止保存
      form.appendChild(el('div', 'error-state', '配置数据形状非法，禁止保存'));
    } else {
      const group = buildSessionSourceCheckboxGroup(baseAllowlist);
      form.appendChild(group.group);
      // 空名单警示：全空时显示，保存保持可用（空名单 = 关闭会话评估）
      const warning = el('div', 'error-state', '空名单 = 关闭会话评估');
      warning.hidden = baseAllowlist.length > 0;
      group.inputs.forEach((input) => {
        input.addEventListener('change', () => {
          warning.hidden = group.inputs.some((item) => item.checked);
        });
      });
      form.appendChild(warning);
      const errorLine = el('div', 'error-state');
      errorLine.hidden = true;
      form.appendChild(errorLine);
      formActions(form, '取消', '保存');
      const saveBtn = form.querySelector('[type="submit"]');
      saveBtn.disabled = false;
      // 统一保存互斥锁：saving/冲突锁死期间重复 submit 事件直接忽略，
      // 不能只禁按钮却允许第二次 submit
      let saving = false;
      let conflicted = false;
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (saving || conflicted) return;
        const selected = group.inputs.filter((input) => input.checked)
          .map((input) => input.value);
        saving = true;
        saveBtn.disabled = true;
        try {
          await api.evaluations.patchConfig({
            patch: { evaluation_session_source_allowlist: selected },
            expected_version: version,
          });
          // 保存期间切页/重渲染：不关旧弹窗、不弹提示、不重绘旧视图
          if (tokenAtOpen !== renderToken || viewAtOpen !== currentView) return;
          closeBackdrop(frame.backdrop);
          alertMessage('评估配置已保存');
          if (typeof onSaved === 'function') onSaved();
        } catch (err) {
          const code = err && err.message ? err.message : '';
          if (code === 'conflict') {
            conflicted = true;
            errorLine.textContent = '配置已被他人修改，请关闭后重新打开核对';
          } else {
            errorLine.textContent = '保存失败：' + code;
          }
          errorLine.hidden = false;
        } finally {
          saving = false;
          saveBtn.disabled = conflicted;
        }
      });
    }
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));
  }

  // ------------------------------------------------------------------
  // 实验列表与详情
  // ------------------------------------------------------------------

  // 数据集版本名自然序比较：数字块按数值（去前导零后先比长度再比字符串，
  // 禁止 Number 转换以防超出安全整数丢精度），非数字块按 UTF-16 码元序；
  // 同位置类型不一致时数字块在前；前缀较短者在前；块比较相等而原串不同
  // （如 v01/v1）时以完整原串 UTF-16 兜底，仅完全相同返回 0。
  function compareDatasetVersions(left, right) {
    const a = String(left === undefined || left === null ? '' : left);
    const b = String(right === undefined || right === null ? '' : right);
    if (a === b) return 0;
    const tokenize = (value) => {
      const chunks = [];
      const re = /\d+|\D+/g;
      let match;
      while ((match = re.exec(value)) !== null) {
        if (/\d/.test(match[0])) {
          chunks.push({ numeric: true, text: match[0].replace(/^0+/, '') || '0' });
        } else {
          chunks.push({ numeric: false, text: match[0] });
        }
      }
      return chunks;
    };
    const ca = tokenize(a);
    const cb = tokenize(b);
    const count = Math.min(ca.length, cb.length);
    for (let i = 0; i < count; i++) {
      const x = ca[i];
      const y = cb[i];
      if (x.numeric !== y.numeric) return x.numeric ? -1 : 1;
      if (x.numeric) {
        if (x.text.length !== y.text.length) return x.text.length < y.text.length ? -1 : 1;
        if (x.text !== y.text) return x.text < y.text ? -1 : 1;
      } else if (x.text !== y.text) {
        return x.text < y.text ? -1 : 1;
      }
    }
    if (ca.length !== cb.length) return ca.length < cb.length ? -1 : 1;
    return a < b ? -1 : 1;
  }

  // 扁平 items 按 dataset_id 保序聚合（Map，区分大小写，规避 __proto__
  // 原型键冲突）；组内按自然序升序复制排序，latest 取最大，history 为其余
  // 版本新到旧；不修改输入数组与 item 字段，invalid 版本保留。
  function groupDatasetItems(items) {
    const groups = new Map();
    items.forEach((item) => {
      const key = item.dataset_id;
      if (!groups.has(key)) groups.set(key, { dataset_id: key, versions: [] });
      groups.get(key).versions.push(item);
    });
    return Array.from(groups.values()).map((group) => {
      const sorted = group.versions.slice().sort(
        (x, y) => compareDatasetVersions(x.dataset_version, y.dataset_version));
      return {
        dataset_id: group.dataset_id,
        latest: sorted[sorted.length - 1],
        history: sorted.slice(0, -1).reverse(),
      };
    });
  }

  async function renderDatasets(body, token) {
    body.textContent = '';
    if (body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.textContent = '';
      body.evaluationsHeaderActions.appendChild(
        actionButton('导入标注 JSON', () => openDatasetImport()));
    }
    const seq = ++viewRequestSeq.datasets;
    const wrap = el('div', 'evaluations-datasets');
    body.appendChild(wrap);
    wrap.appendChild(el('div', 'evaluations-note',
      '展示已导入及待复核审核后导出的数据集。版本不可覆盖，修改内容请导入新版本。'
        + '同一数据集按版本号自然序默认仅展示最新版本，旧版本通过「历史」查看。'));
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '数据集加载中...');
    try {
      const data = await api.evaluations.listDatasets();
      if (token !== renderToken || seq !== viewRequestSeq.datasets) return;
      dataBody.textContent = '';
      const items = data.items || [];
      // 先按 dataset_id 聚合（分页单位是聚合后的数据集行，不是原始版本行），
      // 再对聚合结果做内存切片
      datasetsItems = groupDatasetItems(items);
      datasetsPageIndex = 0;
      const drawDatasets = () => {
        const sliced = slicePage(datasetsItems, datasetsPageIndex);
        if (!datasetsItems.length) {
          dataBody.appendChild(el('div', 'empty-state',
            '暂无数据集。当前尚未导入或导出任何数据集；可通过「导入标注 JSON」创建，'
              + '或在待复核页面审核后导出。此列表为全量展示，不受筛选条件影响。'));
        } else {
          dataBody.appendChild(scrollTable(table(
            ['数据集 ID', '版本', '目标', { label: '用例数', numeric: true }, '校验', '操作'],
            sliced.pageItems.map((group) => {
              const item = group.latest;
              const actions = el('div', 'row-actions');
              actions.appendChild(actionButton('详情',
                () => openDatasetDetail(item.dataset_id, item.dataset_version)));
              if (group.history.length) {
                const history = group.history;
                actions.appendChild(actionButton('历史',
                  () => openDatasetHistory(item.dataset_id, history)));
              } else {
                actions.appendChild(disabledButton('历史'));
              }
              return [
                item.dataset_id, item.dataset_version, item.target_kind || '-',
                item.case_count,
                item.status === 'valid' ? '通过' : '校验失败',
                actions,
              ];
            }),
          )));
        }
        dataBody.appendChild(pagerBar({
          pageIndex: datasetsPageIndex,
          hasPrev: datasetsPageIndex > 0,
          hasNext: datasetsPageIndex + 1 < sliced.totalPages,
          totalPages: sliced.totalPages,
          onPrev: () => {
            datasetsPageIndex -= 1;
            dataBody.textContent = '';
            drawDatasets();
          },
          onNext: () => {
            datasetsPageIndex += 1;
            dataBody.textContent = '';
            drawDatasets();
          },
        }));
      };
      drawDatasets();
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.datasets) return;
      renderErrorState(dataBody, '数据集加载失败：' + err.message, () => renderDatasets(body, renderToken));
    }
  }

  async function openDatasetDetail(id, version) {
    const token = renderToken;
    try {
      const data = await api.evaluations.getDataset(id, version);
      if (token !== renderToken || !isActive()) return;
      const frame = buildFrame('数据集详情');
      frame.form.appendChild(kvRow('数据集', data.dataset_id + ' / ' + data.dataset_version));
      frame.form.appendChild(kvRow('目标', data.target_kind));
      frame.form.appendChild(kvRow('内容哈希', data.dataset_hash));
      frame.form.appendChild(kvRow('用例数', data.cases.length));
      data.cases.forEach(item => {
        const box = el('div', 'evaluations-export-case');
        box.appendChild(el('h4', '', item.case_id));
        const content = el('pre', 'evaluations-dataset-content');
        content.textContent = JSON.stringify(item, null, 2);
        box.appendChild(content);
        frame.form.appendChild(box);
      });
      const actions = formActions(frame.form, '关闭');
      if (executions && data.target_kind === 'agent_run') {
        actions.appendChild(actionButton('发起实验', () => {
          closeBackdrop(frame.backdrop);
          executions.openCreate(data, token);
        }));
      }
      mountDetailModal(frame);
    } catch (err) {
      if (token !== renderToken) return;
      alertMessage(err.message === 'dataset_invalid' ? '数据集校验失败，请检查内容及配对文件。' : '数据集详情加载失败：' + err.message);
    }
  }

  // 数据集历史 Modal：详情类弹窗（mountDetailModal，不响应 backdrop 点击），
  // 数据为成功加载主表时的内存快照，无独立请求；行内详情先关弹窗再复用
  // openDatasetDetail 的逐版本请求。
  function openDatasetHistory(datasetId, historyItems) {
    const frame = buildFrame('数据集历史 - ' + datasetId);
    frame.form.appendChild(scrollTable(table(
      ['版本', '目标', { label: '用例数', numeric: true }, '校验', '操作'],
      historyItems.map((item) => {
        const actions = el('div', 'row-actions');
        actions.appendChild(actionButton('详情', () => {
          closeBackdrop(frame.backdrop);
          openDatasetDetail(datasetId, item.dataset_version);
        }));
        return [
          item.dataset_version, item.target_kind || '-', item.case_count,
          item.status === 'valid' ? '通过' : '校验失败',
          actions,
        ];
      }),
    )));
    formActions(frame.form, '关闭');
    mountDetailModal(frame);
  }

  function openDatasetImport() {
    const frame = buildFrame('导入标注 JSON');
    frame.form.appendChild(el('div', 'evaluations-note',
      '填写数据集 ID、版本、审核人及人工标注的用例。fixture_content 为内联素材；不接受服务端文件路径。可粘贴 JSON 或选择文件。'));
    const file = document.createElement('input');
    file.type = 'file';
    file.accept = '.json,application/json';
    file.setAttribute('aria-label', '选择标注 JSON 文件');
    const input = document.createElement('textarea');
    input.rows = 18;
    input.setAttribute('aria-label', '数据集标注 JSON');
    input.value = JSON.stringify({
      dataset_id: '', dataset_version: 'v1', target_kind: 'agent_run', approved_by: '',
      cases: [{ case_id: 'case-1', input: { query: '' }, target_kind: 'agent_run',
        expected_output: '', fixture_content: { query: '' } }],
    }, null, 2);
    const error = el('div', 'error-state');
    error.hidden = true;
    file.addEventListener('change', async () => {
      try {
        if (file.files && file.files[0]) input.value = await file.files[0].text();
      } catch (_) {
        error.textContent = '读取文件失败，请重新选择或粘贴 JSON。';
        error.hidden = false;
      }
    });
    frame.form.append(file, input, error);
    const actions = formActions(frame.form, '取消', '校验并导入');
    const submit = actions.querySelector('button[type="submit"]');
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));
    const token = renderToken;
    frame.form.addEventListener('submit', async event => {
      event.preventDefault();
      let payload;
      try { payload = JSON.parse(input.value); } catch (_) {
        error.textContent = 'JSON 格式不正确。';
        error.hidden = false;
        return;
      }
      submit.disabled = true;
      try {
        await api.evaluations.importDataset(payload);
        closeBackdrop(frame.backdrop);
        if (token === renderToken && isActive()) await refresh();
      } catch (err) {
        error.textContent = err.message === 'conflict' ? '该数据集版本已存在，请填写新版本。'
          : '导入失败：请检查必填字段、人工金标与内联素材，且不要携带会话或 Trace 身份。';
        error.hidden = false;
      } finally { submit.disabled = false; }
    });
    showBackdrop(frame.backdrop);
  }

  // 执行任务视图（实验子域顶导首项）：发起实验入口挂 Sector Header，
  // 执行列表占满 panel-body；控制器缺失时显式不可用提示，不解引用 null。
  async function renderExecutions(body, token) {
    body.textContent = '';
    if (body.evaluationsHeaderActions) body.evaluationsHeaderActions.textContent = '';
    if (!executions) {
      body.appendChild(el('div', 'empty-state', '执行任务暂不可用：执行模块未加载，请刷新页面重试。'));
      return;
    }
    if (body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.appendChild(actionButton('发起实验',
        () => executions.openCreate(null, token, () => executions.renderList(body, renderToken))));
    }
    await executions.renderList(body, token);
  }

  async function renderExperiments(body, token) {
    body.textContent = '';
    if (body.evaluationsHeaderActions) body.evaluationsHeaderActions.textContent = '';
    const seq = ++viewRequestSeq.experiments;
    const wrap = el('div', 'evaluations-experiments');
    body.appendChild(wrap);
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '实验列表加载中...');
    let data;
    try {
      data = await api.evaluations.listExperiments();
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.experiments) return;
      renderErrorState(dataBody, '实验列表加载失败：' + (err && err.message ? err.message : ''), () => renderExperiments(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.experiments) return;
    dataBody.textContent = '';
    experimentsItems = Array.isArray(data.items) ? data.items : [];
    experimentsPageIndex = 0;
    const drawExperiments = () => {
      const sliced = slicePage(experimentsItems, experimentsPageIndex);
      if (!experimentsItems.length) {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无实验。实验由 execute/replay 评估作业创建；可通过「发起实验」启动数据集测试，也可通过 CLI evaluate 发起。'
            + '当前列表未设置筛选条件。'));
      } else {
        const rows = sliced.pageItems.map((item) => {
          const detailBtn = el('div', 'row-actions');
          detailBtn.appendChild(actionButton('详情', () => openExperimentModal(item.experiment_id)));
          detailBtn.appendChild(experimentResultsLink(item.experiment_id));
          return [
            item.experiment_id,
            item.dataset_id + '/' + item.dataset_version,
            item.mode,
            statusBadge(item.status),
            formatTime(item.created_at),
            detailBtn,
          ];
        });
        dataBody.appendChild(scrollTable(
          table(['实验ID', '数据集', '模式', '状态', '创建时间', '操作'], rows)));
      }
      dataBody.appendChild(pagerBar({
        pageIndex: experimentsPageIndex,
        hasPrev: experimentsPageIndex > 0,
        hasNext: experimentsPageIndex + 1 < sliced.totalPages,
        totalPages: sliced.totalPages,
        onPrev: () => {
          experimentsPageIndex -= 1;
          dataBody.textContent = '';
          drawExperiments();
        },
        onNext: () => {
          experimentsPageIndex += 1;
          dataBody.textContent = '';
          drawExperiments();
        },
      }));
    };
    drawExperiments();
  }

  function experimentResultsLink(experimentId) {
    const link = document.createElement('a');
    link.className = 'btn';
    link.textContent = '查看评分明细';
    link.href = '/evaluations/experiment-results?experiment_id=' + encodeURIComponent(experimentId);
    return link;
  }

  async function openExperimentModal(experimentId) {
    // Mi-#21：迟到响应防护——离开评估页（tab 切换重置 renderToken）后
    // 才返回的详情请求不得再打开弹窗
    const requestToken = renderToken;
    let overview;
    try {
      overview = await api.evaluations.getExperiment(experimentId);
    } catch (err) {
      alertMessage('实验详情加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    if (requestToken !== renderToken) return;
    const frame = buildFrame('实验详情 ' + experimentId);
    const form = frame.form;
    const kv = el('div', 'policy-list');
    const experiment = overview.experiment || {};
    kv.appendChild(kvRow('状态', experiment.status));
    kv.appendChild(kvRow('数据集', experiment.dataset_id + '/' + experiment.dataset_version));
    kv.appendChild(kvRow('模式', experiment.mode));
    kv.appendChild(kvRow('创建时间', formatTime(experiment.created_at)));
    const coverage = overview.coverage || {};
    kv.appendChild(kvRow('覆盖率（结果数）', coverage.results_total));
    kv.appendChild(kvRow('其中有值结果', coverage.value_results));
    kv.appendChild(kvRow('异常/跳过结果', coverage.error_or_skipped_results));
    // M3b：低覆盖提示（详情口径）；N1：长句提示不用状态徽章
    if (!coverage.results_total) {
      form.appendChild(el('div', 'evaluations-hint',
        '低覆盖：该实验尚无评估结果'));
    }
    // Mi-#20：扫描帽截断提示——覆盖率/分布按前 5000 行聚合
    if (coverage.scan_truncated) {
      form.appendChild(el('div', 'evaluations-hint',
        '结果数超出扫描上限（5000），以上覆盖率与分布按已扫描部分聚合，'
        + '非全量口径'));
    }
    const baseline = overview.baseline;
    kv.appendChild(kvRow('Baseline', baseline ? baseline.baseline_id : '-'));
    kv.appendChild(kvRow('Gate 报告引用', overview.gate_report_ref || '-'));
    form.appendChild(kv);

    // 分布（按指标聚合）
    const distribution = overview.distribution || {};
    const distKeys = Object.keys(distribution);
    if (distKeys.length) {
      form.appendChild(blockTitle('指标分布'));
      const distRows = distKeys.map((metric) => {
        const bucket = distribution[metric] || {};
        const hasVerdicts = (bucket.passed || 0) + (bucket.failed || 0) > 0;
        return [
          metric,
          bucket.results_total,
          hasVerdicts ? bucket.passed : null,
          hasVerdicts ? bucket.failed : null,
          bucket.numeric_count || null,
          bucket.numeric_mean,
          bucket.numeric_min,
          bucket.numeric_max,
          bucket.error_or_skipped,
        ];
      });
      form.appendChild(scrollTable(table(
        ['指标',
          { label: '结果数', numeric: true },
          { label: '通过', numeric: true },
          { label: '质量不通过', numeric: true },
          { label: '评分数', numeric: true },
          { label: '均分', numeric: true },
          { label: '最低分', numeric: true },
          { label: '最高分', numeric: true },
          { label: '异常跳过', numeric: true }],
        distRows)));
      form.appendChild(el('div', 'evaluations-note',
        '通过/质量不通过仅统计已有布尔判定；数值评分展示实际分数，未设置通过阈值时不判定通过。'));
    }

    // 投递状态（按 sink 聚合）+ Langfuse 会话入口
    const deliveryStatus = overview.delivery_status || {};
    const sinkKeys = Object.keys(deliveryStatus);
    if (sinkKeys.length) {
      form.appendChild(blockTitle('投递状态',
        'Langfuse Score 为不可变历史投影；本地账本为 latest 口径，计数为普通文本而非状态徽章。'));
      const deliveryRows = sinkKeys.map((sink) => {
        const bucket = deliveryStatus[sink] || {};
        // 去徽章化：四态计数是计量值不是封闭状态，用普通文本行 + tabular-nums
        const cell = el('div', 'evaluations-delivery-counts');
        cell.appendChild(el('div', '', 'total: ' + formatNumber(bucket.total)));
        cell.appendChild(el('div', '', 'verified: ' + formatNumber(bucket.verified || 0)));
        cell.appendChild(el('div', '', 'pending: ' + formatNumber(bucket.pending || 0)));
        cell.appendChild(el('div', '', 'dead_letter: ' + formatNumber(bucket.dead_letter || 0)));
        const linkCell = document.createElement('div');
        if (sink === 'langfuse' && LANGFUSE_URL) {
          linkCell.appendChild(externalLink(LANGFUSE_URL, '查看 Langfuse 会话'));
        }
        return [sink, cell, linkCell];
      });
      form.appendChild(scrollTable(table(['Sink', '状态计数', '链接'], deliveryRows)));
    } else {
      form.appendChild(kvRow('投递状态', '暂无投递'));
    }
    // M3c：Langfuse 不可变历史 vs 本地 latest 口径区分
    form.appendChild(el('div', 'evaluations-note',
      '口径说明：Langfuse Score 为不可变历史投影（重评/人评修订产生新投影，'
      + '旧值不覆盖）；本地账本 latest 口径以 supersedes 链为准（详情页结果'
      + '列表为 current 视图）。'));

    const actions = el('div', 'providers-form__actions');
    actions.appendChild(experimentResultsLink(experimentId));
    actions.appendChild(actionButton('与基线对比', () => {
      closeBackdrop(frame.backdrop);
      return openCompareModal(experiment);
    }));
    actions.appendChild(actionButton('关闭', () => closeBackdrop(frame.backdrop)));
    form.appendChild(actions);
    mountDetailModal(frame);
  }

  // ------------------------------------------------------------------
  // 只读实验对比（Task 13/spec 4.6）：同数据集基线选择 + 可选策略 JSON
  // 输入 + 描述性/四态结论渲染 + 不可比 422 差异维度展示；只读，不重跑
  // 目标、不启动 Judge、不改变实验状态
  // ------------------------------------------------------------------

  const COMPARE_CONCLUSION_LABELS = {
    gate_pass: '质量通过（gate_pass）',
    gate_fail: '质量不通过（gate_fail）',
    infrastructure_error: '基础设施错误（infrastructure_error）',
    inconclusive: '结论不足（inconclusive）',
  };

  // 结论四态徽章变体：长标签经 statusBadge 的通用映射会全部落 warning，
  // 这里按语义独立映射（gate_pass 成功 / 失败与基础设施错误危险 /
  // 结论不足中性），未收录状态保持中性 warning
  const COMPARE_CONCLUSION_VARIANTS = {
    gate_pass: 'success',
    gate_fail: 'danger',
    infrastructure_error: 'danger',
    inconclusive: 'warning',
  };

  async function openCompareModal(experiment) {
    // Mi-#21：迟到响应防护——离开评估页后返回的列表不得再打开弹窗
    const requestToken = renderToken;
    let list;
    try {
      list = await api.evaluations.listExperiments();
    } catch (err) {
      alertMessage('实验列表加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    if (requestToken !== renderToken) return;
    const candidates = (Array.isArray(list.items) ? list.items : [])
      .filter((item) => (
        item.experiment_id !== experiment.experiment_id
        && item.dataset_id === experiment.dataset_id
        && item.dataset_version === experiment.dataset_version
      ));
    const frame = buildFrame('与基线对比（候选 ' + experiment.experiment_id + '）');
    const form = frame.form;
    form.appendChild(el('div', 'evaluations-note',
      '仅同 dataset_id 且同版本的实验可选为基线；服务端将核验数据集 hash'
        + '与冻结配置。对比只读，不重跑目标、不启动 Judge。'));
    if (!candidates.length) {
      form.appendChild(el('div', 'evaluations-hint', '暂无同数据集实验可作为基线'));
    }
    const baselineSelect = selectControl('baseline_id',
      candidates.map((item) => [item.experiment_id, item.experiment_id]),
      candidates.length ? candidates[0].experiment_id : '');
    fieldRow(form, '基线实验', baselineSelect);
    const policyInput = document.createElement('textarea');
    policyInput.name = 'compare_policy';
    policyInput.rows = 6;
    policyInput.placeholder = '可选：门禁策略 JSON（不填仅描述性对比）';
    fieldRow(form, '门禁策略 JSON（可选）', policyInput);
    const errorLine = el('div', 'error-state');
    errorLine.hidden = true;
    const resultBox = el('div', 'policy-list');
    form.append(errorLine, resultBox);
    formActions(form, '取消', '开始对比');
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const baselineId = String(baselineSelect.value || '').trim();
      if (!baselineId) {
        errorLine.textContent = '请选择基线实验';
        errorLine.hidden = false;
        return;
      }
      const payload = {
        candidate_id: experiment.experiment_id,
        baseline_id: baselineId,
      };
      const policyRaw = String(policyInput.value || '').trim();
      if (policyRaw) {
        let policy;
        try {
          policy = JSON.parse(policyRaw);
        } catch (_) {
          errorLine.textContent = '策略 JSON 格式不正确';
          errorLine.hidden = false;
          return;
        }
        if (!policy || typeof policy !== 'object' || Array.isArray(policy)) {
          errorLine.textContent = '策略必须是 JSON 对象';
          errorLine.hidden = false;
          return;
        }
        payload.policy = policy;
      }
      errorLine.hidden = true;
      try {
        const result = await api.evaluations.compareExperiments(payload);
        renderCompareResult(resultBox, result);
      } catch (err) {
        const code = err && err.message ? err.message : '';
        // 409/422 保留输入（对齐 Task 11/12），不自动覆盖
        if (code === 'incomparable_experiments') {
          // spec 9：差异维度布尔映射为可公开信息，逐项展示
          const dims = (err && err.data && err.data.error
            && err.data.error.diff_dimensions) || {};
          errorLine.hidden = true;
          resultBox.textContent = '';
          resultBox.appendChild(el('div', 'error-state',
            '两实验不可比（422），差异维度如下：'));
          Object.keys(dims).forEach((key) => {
            resultBox.appendChild(kvRow(key, dims[key] ? '不一致' : '一致'));
          });
        } else if (code === 'not_found') {
          errorLine.textContent = '实验不存在（404），请刷新实验列表后重试';
          errorLine.hidden = false;
        } else {
          errorLine.textContent = '对比失败：' + code;
          errorLine.hidden = false;
        }
      }
    });
  }

  // 对比响应渲染：证据/分母依据 + 两实验身份 + 配对指标行 + 投递状态
  // 单列；有策略时展示四态结论/原因/策略指纹，无策略仅描述性输出
  function renderCompareResult(box, result) {
    box.textContent = '';
    const candidate = result.candidate || {};
    const baseline = result.baseline || {};
    const kv = el('div', 'policy-list');
    kv.appendChild(kvRow('证据依据', result.evidence_basis || '-'));
    kv.appendChild(kvRow('分母依据', result.denominator_basis || '-'));
    kv.appendChild(kvRow('候选', (candidate.experiment_id || '-') + '（'
      + (candidate.dataset_id || '-') + '/' + (candidate.dataset_version || '-') + '）'));
    kv.appendChild(kvRow('基线', (baseline.experiment_id || '-') + '（'
      + (baseline.dataset_id || '-') + '/' + (baseline.dataset_version || '-') + '）'));
    box.appendChild(kv);
    const metrics = Array.isArray(result.metrics) ? result.metrics : [];
    if (metrics.length) {
      box.appendChild(blockTitle('配对指标'));
      box.appendChild(scrollTable(table(
        ['指标',
          { label: '配对case', numeric: true },
          { label: '候选样本', numeric: true },
          { label: '候选均值', numeric: true },
          { label: '基线样本', numeric: true },
          { label: '基线均值', numeric: true },
          { label: '差值', numeric: true },
          { label: 'CI下界', numeric: true }],
        metrics.map((row) => {
          const cand = row.candidate || {};
          const base = row.baseline || {};
          return [
            row.metric_name,
            row.paired_cases,
            cand.value_count,
            cand.macro_value,
            base.value_count,
            base.macro_value,
            row.diff,
            row.paired_ci_lower,
          ];
        }),
      )));
      box.appendChild(el('div', 'evaluations-note',
        '差值为按 case 配对的均值差；各侧缺失（missing_count）与异常'
        + '（error_count）条目不计入配对样本。'));
    }
    const delivery = result.delivery_status_counts || {};
    const sinks = Object.keys(delivery);
    if (sinks.length) {
      box.appendChild(blockTitle('投递状态（候选，单列不参与质量结论）'));
      box.appendChild(scrollTable(table(
        ['Sink', '明细'],
        sinks.map((sink) => {
          const counts = delivery[sink] || {};
          return [sink, Object.keys(counts)
            .map((key) => key + ':' + counts[key]).join('，')];
        }),
      )));
    }
    if (result.conclusion) {
      // 有策略：复用既有 EvaluationGate 四态结论（不得为页面放宽算法）
      const conclusion = result.conclusion;
      box.appendChild(blockTitle('门禁结论'));
      box.appendChild(el('span',
        'badge badge--' + (COMPARE_CONCLUSION_VARIANTS[conclusion.state] || 'warning'),
        COMPARE_CONCLUSION_LABELS[conclusion.state] || conclusion.state));
      const reasons = Array.isArray(conclusion.reasons) ? conclusion.reasons : [];
      reasons.forEach((reason) => {
        box.appendChild(el('div', 'evaluations-note', '原因：' + reason));
      });
      box.appendChild(kvRow('策略指纹', result.policy_fingerprint));
      box.appendChild(kvRow('覆盖率分母依据', conclusion.denominator_basis || '-'));
    } else {
      box.appendChild(el('div', 'evaluations-note',
        '未提供门禁策略：仅描述性对比，不生成质量结论。'));
    }
  }

  // ------------------------------------------------------------------
  // 结果四态浏览与详情（M3c/M3d）
  // ------------------------------------------------------------------

  function resultState(item) {
    if (item.status === 'error') return RESULT_STATE_LABELS.error;
    if (item.status === 'skipped') return RESULT_STATE_LABELS.skipped;
    if (item.status === 'value') {
      // Mi-#19：boolean 结果按 passed 判定；数值结果（passed=null）不是
      // 门禁判定，不得显示为"通过"——展示中性"数值"口径
      if (item.passed === false) return RESULT_STATE_LABELS.failed;
      if (item.passed === true) return RESULT_STATE_LABELS.passed;
      return '数值';
    }
    return String(item.status || '-');
  }

  // 详情可选字段安全文本化：字符串原样，对象/数组 JSON 化展示结构，
  // 序列化失败降级，不抛错中断弹窗渲染（spec-261123）
  function safeDetailText(value) {
    try {
      if (value === null || value === undefined) return '';
      if (typeof value === 'string') return value;
      if (typeof value === 'object') {
        const serialized = JSON.stringify(value);
        return typeof serialized === 'string' ? serialized : '无法展示';
      }
      return String(value);
    } catch (_) { return '无法展示'; }
  }

  function isBlankDetailValue(value) {
    return value === null || value === undefined
      || (typeof value === 'string' && value.trim() === '');
  }

  // "结果"文字框多段文本（spec-261123）：各段以空行分隔，逐段按适用性
  // 降级，不抛错。passed 用严格比较（不使用 truthiness）；attempt_count
  // 是作业领取次数（领取时累加），不等同于 Judge 调用次数或重试次数，
  // 禁止输出"已重试 N 次"。unknown 状态不映射四态。
  function buildResultDetailText(result, job) {
    const status = result.status;
    const passed = result.passed;
    let stateLabel;
    let stateMeaning;
    if (status === 'error') {
      stateLabel = RESULT_STATE_LABELS.error;
      stateMeaning = '本条结果未形成有效质量判定，可能来自执行异常或作业终止'
        + '（不计入统计分母，单独计入异常）；不据此断言整个作业从未产生其它指标的判定';
    } else if (status === 'skipped') {
      stateLabel = RESULT_STATE_LABELS.skipped;
      stateMeaning = '该指标对此样本未执行或不可评估，原因可来自评估器或调度'
        + '预算等前置检查（不是 0 分也不是失败，统计分母不含它）';
    } else if (status === 'value') {
      if (passed === false) {
        stateLabel = RESULT_STATE_LABELS.failed;
        stateMeaning = '评估器产出了判定值，但未达到通过标准';
      } else if (passed === true) {
        stateLabel = RESULT_STATE_LABELS.passed;
        stateMeaning = '评估器产出了判定值，质量判定通过';
      } else {
        stateLabel = '数值';
        stateMeaning = '已产出判定值，未提供门禁判定，不推断通过或质量不通过';
      }
    } else {
      stateLabel = isBlankDetailValue(status) ? '状态未知' : safeDetailText(status);
      stateMeaning = '无法解释该结果状态，不映射为通过、跳过或失败';
    }
    const sections = [];
    sections.push('状态：' + stateLabel + ' — ' + stateMeaning);
    if (status === 'value') {
      const gateText = passed === true ? '通过' : passed === false ? '不通过' : '无门禁判定';
      const valueText = isBlankDetailValue(result.typed_value)
        ? '-' : safeDetailText(result.typed_value);
      sections.push('判定值：' + valueText + '；门禁判定：' + gateText);
    }
    if (!isBlankDetailValue(result.explanation)) {
      sections.push('结果说明：' + safeDetailText(result.explanation));
    }
    if (status === 'skipped') {
      const reason = result.skip_reason;
      sections.push('跳过原因：' + (isBlankDetailValue(reason)
        ? '未记录'
        : (typeof reason === 'string' && Object.prototype.hasOwnProperty.call(SKIP_REASON_LABELS, reason)
          ? reason + '（' + SKIP_REASON_LABELS[reason] + '）'
          : safeDetailText(reason))));
    }
    if (status === 'error' && job) {
      const errorType = job.error_type;
      sections.push('失败分类：' + (isBlankDetailValue(errorType)
        ? '未记录'
        : (typeof errorType === 'string' && Object.prototype.hasOwnProperty.call(JOB_ERROR_TYPE_LABELS, errorType)
          ? errorType + '（' + JOB_ERROR_TYPE_LABELS[errorType] + '）'
          : safeDetailText(errorType))));
    }
    if (job) {
      sections.push('作业状态：' + (isBlankDetailValue(job.status)
        ? '未记录' : safeDetailText(job.status)));
      const count = job.attempt_count;
      sections.push('作业领取次数：'
        + (typeof count === 'number' && Number.isSafeInteger(count) && count >= 0
          ? String(count) : '未记录'));
    }
    if (!isBlankDetailValue(result.evaluation_trace_id)) {
      sections.push('评估 Trace：' + safeDetailText(result.evaluation_trace_id));
    }
    return sections.join('\n\n');
  }

  // 结果筛选与游标分页状态（Task 13/spec 4.5）：跨渲染保留用户选择；
  // 游标 v2 是不透明 token，上一页用历史游标栈回退，过滤变更重置栈
  const defaultResultsFilters = () => ({
    created_from: '',
    created_to: '',
    target_kind: '',
    definition_id: '',
    provenance: '',
    status: '',
  });
  // 专用视图固定 target_kind（spec-261119）：视图身份即类型，不计为用户
  // 筛选；普通 results/experiment-results 不应用锁定
  const LOCKED_RESULTS_TARGET_KIND = {
    'session-scores': 'session_feedback',
    'run-scores': 'agent_run',
  };
  const resultFiltersByView = {
    results: defaultResultsFilters(),
    'experiment-results': { ...defaultResultsFilters(), experiment_id: '' },
    'session-scores': { ...defaultResultsFilters(),
      target_kind: LOCKED_RESULTS_TARGET_KIND['session-scores'] },
    'run-scores': { ...defaultResultsFilters(),
      target_kind: LOCKED_RESULTS_TARGET_KIND['run-scores'] },
  };
  let resultsFilters = resultFiltersByView.results;
  let experimentResultExperiments = null;
  let resultsCursorStack = [''];
  let resultsPageIndex = 0;

  function resetResultsPaging() {
    resultsCursorStack = [''];
    resultsPageIndex = 0;
  }

  function resultsQueryParams() {
    const params = { limit: LIST_PAGE_SIZE };
    const cursor = resultsCursorStack[resultsPageIndex];
    if (cursor) params.cursor = cursor;
    Object.keys(resultsFilters).forEach((key) => {
      if (resultsFilters[key]) params[key] = resultsFilters[key];
    });
    return params;
  }

  async function renderResults(body, token) {
    body.textContent = '';
    // 会话评分专用视图（spec-261119）按视图身份锁定 target_kind：
    // seq 按视图独立递增，锁定类型在请求构造前强制覆盖，不依赖筛选状态。
    // run-scores 已切换独立端点 renderRunScores（plan-261124 T7），不再
    // 进入本函数。
    const viewAtStart = currentView;
    const seqKey = viewAtStart === 'session-scores' ? viewAtStart : 'results';
    const seq = ++viewRequestSeq[seqKey];
    const lockedKind = LOCKED_RESULTS_TARGET_KIND[viewAtStart] || '';
    const experimentView = currentView === 'experiment-results';
    // 会话评分视图头部操作区挂「评估配置」入口（spec-261119）：renderResults
    // 为 results/experiment-results/session-scores 共享 renderer，按钮只在
    // session-scores 出现；先清空再追加，防筛选重绘重复叠加。
    if (viewAtStart === 'session-scores' && body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.textContent = '';
      body.evaluationsHeaderActions.appendChild(actionButton('评估配置', () =>
        openSessionEvalConfigModal(() => renderResults(body, renderToken))));
    }
    const wrap = el('div', 'evaluations-results');
    body.appendChild(wrap);

    if (experimentView && experimentResultExperiments === null) {
      showLoading(wrap, '实验列表加载中...');
      let experimentData;
      try {
        experimentData = await api.evaluations.listExperiments();
      } catch (err) {
        if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
        renderErrorState(wrap, '实验列表加载失败：' + err.message, () => renderResults(body, renderToken));
        return;
      }
      if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
      experimentResultExperiments = experimentData.items || [];
      wrap.textContent = '';
    }
    // 筛选区（spec 4.5：时间范围/目标类型/DefinitionKey/provenance/状态；
    // 变更任一过滤重置游标栈）。
    const filterRow = filterBar();
    // 统一接线：变更任一过滤重置游标栈并重渲染；文本输入做 trim，
    // 下拉选项值原样写入
    const bindResultsFilter = (control, key, trim) => {
      control.addEventListener('change', () => {
        const raw = String(control.value || '');
        resultsFilters[key] = trim ? raw.trim() : raw;
        resetResultsPaging();
        renderResults(body, renderToken);
      });
    };
    const createdFromInput = document.createElement('input');
    createdFromInput.name = 'results_created_from';
    createdFromInput.value = resultsFilters.created_from;
    createdFromInput.placeholder = '开始时间（ISO，如 2026-11-05T00:00:00Z）';
    bindResultsFilter(createdFromInput, 'created_from', true);
    const createdToInput = document.createElement('input');
    createdToInput.name = 'results_created_to';
    createdToInput.value = resultsFilters.created_to;
    createdToInput.placeholder = '结束时间（ISO，不含该时刻）';
    bindResultsFilter(createdToInput, 'created_to', true);
    const defIdInput = document.createElement('input');
    defIdInput.name = 'results_definition_id';
    defIdInput.value = resultsFilters.definition_id;
    defIdInput.placeholder = 'DefinitionKey（如 kb.recall_at_5）';
    bindResultsFilter(defIdInput, 'definition_id', true);
    // 目标类型下拉仅在非锁定视图渲染；锁定视图的类型即视图身份，不提供
    // 切换入口（spec-261119）。评分明细下拉补全 session_feedback。
    const kindSelect = lockedKind ? null : selectControl('results_target_kind', [
      ['', '全部目标'],
      ['agent_run', 'agent_run'],
      ['retrieval', 'retrieval'],
      ['session_feedback', 'session_feedback'],
    ], resultsFilters.target_kind);
    if (kindSelect) bindResultsFilter(kindSelect, 'target_kind', false);
    const provenanceSelect = selectControl('results_provenance', [
      ['', '全部来源'],
      ['automatic', 'automatic'],
      ['human', 'human'],
    ], resultsFilters.provenance);
    bindResultsFilter(provenanceSelect, 'provenance', false);
    const statusSelect = selectControl('results_status', [
      ['', '全部状态'],
      ['value', '有值'],
      ['error', '评估失败'],
      ['skipped', '跳过'],
    ], resultsFilters.status);
    bindResultsFilter(statusSelect, 'status', false);
    filterRow.append(
      filterControl('开始时间', createdFromInput),
      filterControl('结束时间', createdToInput),
    );
    if (kindSelect) filterRow.append(filterControl('目标类型', kindSelect));
    filterRow.append(
      filterControl('DefinitionKey', defIdInput),
      filterControl('来源', provenanceSelect),
      filterControl('状态', statusSelect),
    );
    if (experimentView) {
      const options = [['', '全部实验'], ...experimentResultExperiments.map(item =>
        [item.experiment_id, item.experiment_id + ' · ' + item.dataset_id + '/' + item.dataset_version])];
      if (resultsFilters.experiment_id && !options.some(option => option[0] === resultsFilters.experiment_id)) {
        options.push([resultsFilters.experiment_id, resultsFilters.experiment_id]);
      }
      const experimentSelect = selectControl('results_experiment_id', options, resultsFilters.experiment_id);
      bindResultsFilter(experimentSelect, 'experiment_id', false);
      filterRow.appendChild(filterControl('实验', experimentSelect));
    }
    wrap.appendChild(filterRow);
    // 数据容器独立于筛选区：Loading/错误/空态/重试只清 dataBody，
    // 不删除筛选控件与当前查询条件。
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '评估结果加载中...');

    const params = resultsQueryParams();
    // 锁定视图以映射覆盖请求类型：筛选状态被意外改写也不影响请求目标
    if (lockedKind) params.target_kind = lockedKind;
    const hadCursor = !!params.cursor;
    let data;
    try {
      data = await (experimentView
        ? api.evaluations.listExperimentResults(params)
        : api.evaluations.listResults(params));
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
      dataBody.textContent = '';
      const code = err && err.message ? err.message : '';
      if (hadCursor && (code === 'cursor_invalid' || code === 'protocol_invalid')) {
        // 游标 v2 绑定过滤与查询快照上界：过滤不匹配/游标非法时提示
        // 失效并提供重新查询入口（重置游标、保留过滤），不静默重试
        dataBody.appendChild(el('div', 'error-state',
          '游标已失效（过滤条件或查询快照不匹配），请重新查询'));
        dataBody.appendChild(actionButton('重新查询', () => {
          resetResultsPaging();
          renderResults(body, renderToken);
        }));
        return;
      }
      renderErrorState(dataBody, '结果加载失败：' + code, () => renderResults(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
    dataBody.textContent = '';
    const items = Array.isArray(data.items) ? data.items : [];
    // 游标分页（spec 4.5）：上一页走历史游标栈回退，下一页携带响应
    // next_cursor 入栈。统一 pagerBar：按钮始终占位、边界态禁用而非隐藏；
    // pager 先于空态构建，第 2+ 页返回空 items 时仍保留"上一页"入口，
    // 避免用户被卡死在空页。
    const nextCursor = data.next_cursor ? String(data.next_cursor) : '';
    const pagerRow = pagerBar({
      pageIndex: resultsPageIndex,
      totalPages: data.total_pages,
      hasPrev: resultsPageIndex > 0,
      hasNext: !!nextCursor,
      onPrev: () => {
        resultsPageIndex -= 1;
        return renderResults(body, renderToken);
      },
      onNext: () => {
        resultsCursorStack = resultsCursorStack.slice(0, resultsPageIndex + 1);
        resultsCursorStack.push(nextCursor);
        resultsPageIndex += 1;
        return renderResults(body, renderToken);
      },
    });
    if (!items.length) {
      // 空态分支：空分页优先（保留上一页入口），其次按有无"用户筛选"给
      // 下一步；锁定视图的固定 target_kind 是视图身份不计入用户筛选，
      // 空数据给出类型语义提示（spec-261119）
      const userFilterKeys = Object.keys(resultsFilters)
        .filter((key) => !(lockedKind && key === 'target_kind'));
      const hasUserFilter = userFilterKeys.some((key) => resultsFilters[key]);
      if (resultsPageIndex > 0) {
        dataBody.appendChild(el('div', 'empty-state', '当前分页无结果，请返回上一页'));
      } else if (hasUserFilter) {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无评估结果，可调整或清空筛选条件后重试。'));
      } else if (lockedKind === 'session_feedback') {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无会话评分，会话人评提交后将展示在此处。'));
      } else {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无评估结果，评估作业完成后结果将展示在此处。'));
      }
      dataBody.appendChild(pagerRow);
      return;
    }
    const sessionTitles = await loadSessionTitles();
    // loadSessionTitles 跨模块缓存 Promise，解析晚于切换/重渲染时旧渲染
    // 不得把旧 items 追加回 DOM（与列表响应同等的过期防护）
    if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
    const rows = items.map((item) => [
      resultSessionCell(item, sessionTitles),
      resultExperimentCell(item.experiment_id),
      item.metric_name,
      item.typed_value === undefined || item.typed_value === null ? '-' : String(item.typed_value),
      statusBadge(resultState(item)),
      item.provenance,
      formatTime(item.created_at),
      actionButton('详情', () => openResultModal(item.result_id)),
    ]);
    dataBody.appendChild(scrollTable(table(
      ['来源会话', '实验', '指标', '评分',
        { label: '状态', tip: '质量不通过/通过来自判定；评估失败/跳过为执行态；数值表示无门禁判定' },
        '评分来源', '时间', '操作'],
      rows,
    )));
    dataBody.appendChild(pagerRow);
    // 状态口径备注收进"结果"标题 hover tips（views.results shellTip）
  }

  // 会话标题映射：来源会话列/详情展示会话名称（fallback 会话ID），
  // 链接仍按会话ID直达 observations 会话详情。api 无 listSessions（如
  // harness 注入的最小 api 面）或加载失败时回落空映射，不阻塞结果渲染。
  let sessionTitlesPromise = null;
  function loadSessionTitles() {
    if (!sessionTitlesPromise) {
      const fetchSessions = typeof api.listSessions === 'function'
        ? api.listSessions() : Promise.resolve([]);
      sessionTitlesPromise = Promise.resolve(fetchSessions)
        .then((sessions) => {
          const map = {};
          (Array.isArray(sessions) ? sessions : []).forEach((s) => {
            if (s && s.id) map[s.id] = s.title || '';
          });
          return map;
        })
        .catch(() => ({}));
    }
    return sessionTitlesPromise;
  }

  function resultSessionCell(item, sessionTitles) {
    if (!item.session_id) return '未关联会话';
    if (item.target_service !== 'n-agent') return item.session_id;
    const link = el('a', 'otel-link');
    const title = sessionTitles && sessionTitles[item.session_id];
    link.textContent = title || item.session_id;
    link.href = '/observations/sessions/' + encodeURIComponent(item.session_id);
    return link;
  }

  function resultExperimentCell(experimentId) {
    if (!experimentId) return '—';
    const link = experimentResultsLink(experimentId);
    link.textContent = experimentId;
    return link;
  }

  // ------------------------------------------------------------------
  // 运行评分视图（plan-261124 T7）：run 粒度独立端点 /chat/evaluations/
  // run-scores。与 renderResults 同构（筛选/游标分页/过期防护/空态），
  // 差异：无目标类型下拉（类型即视图身份）、八项白名单请求参数、5 列
  // 表格 + run 级详情弹窗（复用列表已加载 sessionTitles，零额外请求）。
  // ------------------------------------------------------------------

  // run-scores 请求/空态判定白名单：不含 target_kind（端点固定 run 粒度，
  // 锁定类型不得外发）；definition_version 预留给后续版本筛选。
  const RUN_SCORES_FILTER_KEYS = [
    'definition_id', 'definition_version', 'provenance', 'status',
    'created_from', 'created_to',
  ];

  async function renderRunScores(body, token) {
    body.textContent = '';
    const seqKey = 'run-scores';
    const seq = ++viewRequestSeq[seqKey];
    const wrap = el('div', 'evaluations-results');
    body.appendChild(wrap);
    // 持久筛选对象清除锁定字段：resultFiltersByView 初始化播种时保留
    // LOCKED_RESULTS_TARGET_KIND['run-scores']（锁定口径占位），本视图随后
    // 剥离该字段——请求与 hasUserFilter 均不把 target_kind 当作筛选键。
    const filters = resultFiltersByView['run-scores'];
    delete filters.target_kind;

    // 筛选区：时间范围/DefinitionKey/来源/状态（无目标类型下拉，与
    // renderResults 锁定分支一致）；变更任一过滤重置游标栈并重渲染。
    const filterRow = filterBar();
    const bindRunFilter = (control, key, trim) => {
      control.addEventListener('change', () => {
        const raw = String(control.value || '');
        filters[key] = trim ? raw.trim() : raw;
        resetResultsPaging();
        renderRunScores(body, renderToken);
      });
    };
    const createdFromInput = document.createElement('input');
    createdFromInput.name = 'results_created_from';
    createdFromInput.value = filters.created_from;
    createdFromInput.placeholder = '开始时间（ISO，如 2026-11-05T00:00:00Z）';
    bindRunFilter(createdFromInput, 'created_from', true);
    const createdToInput = document.createElement('input');
    createdToInput.name = 'results_created_to';
    createdToInput.value = filters.created_to;
    createdToInput.placeholder = '结束时间（ISO，不含该时刻）';
    bindRunFilter(createdToInput, 'created_to', true);
    const defIdInput = document.createElement('input');
    defIdInput.name = 'results_definition_id';
    defIdInput.value = filters.definition_id;
    defIdInput.placeholder = 'DefinitionKey（如 kb.recall_at_5）';
    bindRunFilter(defIdInput, 'definition_id', true);
    const provenanceSelect = selectControl('results_provenance', [
      ['', '全部来源'],
      ['automatic', 'automatic'],
      ['human', 'human'],
    ], filters.provenance);
    bindRunFilter(provenanceSelect, 'provenance', false);
    const statusSelect = selectControl('results_status', [
      ['', '全部状态'],
      ['value', '有值'],
      ['error', '评估失败'],
      ['skipped', '跳过'],
    ], filters.status);
    bindRunFilter(statusSelect, 'status', false);
    filterRow.append(
      filterControl('开始时间', createdFromInput),
      filterControl('结束时间', createdToInput),
      filterControl('DefinitionKey', defIdInput),
      filterControl('来源', provenanceSelect),
      filterControl('状态', statusSelect),
    );
    wrap.appendChild(filterRow);
    // 数据容器独立于筛选区：Loading/错误/空态/重试只清 dataBody，
    // 不删除筛选控件与当前查询条件。
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '评估结果加载中...');

    // 请求参数：八项白名单逐个构造（limit/cursor + 筛选键），不透传
    // target_* 或 experiment_id（run-scores 端点固定类型）
    const params = { limit: LIST_PAGE_SIZE };
    const cursor = resultsCursorStack[resultsPageIndex];
    if (cursor) params.cursor = cursor;
    RUN_SCORES_FILTER_KEYS.forEach((key) => {
      if (filters[key]) params[key] = filters[key];
    });
    const hadCursor = !!params.cursor;
    let data;
    try {
      data = await api.evaluations.listRunScores(params);
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
      dataBody.textContent = '';
      const code = err && err.message ? err.message : '';
      if (hadCursor && (code === 'cursor_invalid' || code === 'protocol_invalid')) {
        // 游标 v2 绑定过滤与查询快照上界：过滤不匹配/游标非法时提示
        // 失效并提供重新查询入口（重置游标、保留过滤），不静默重试
        dataBody.appendChild(el('div', 'error-state',
          '游标已失效（过滤条件或查询快照不匹配），请重新查询'));
        dataBody.appendChild(actionButton('重新查询', () => {
          resetResultsPaging();
          renderRunScores(body, renderToken);
        }));
        return;
      }
      renderErrorState(dataBody, '结果加载失败：' + code, () => renderRunScores(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
    dataBody.textContent = '';
    const items = Array.isArray(data.items) ? data.items : [];
    // 游标分页：上一页走历史游标栈回退，下一页携带响应 next_cursor 入栈；
    // pager 先于空态构建，第 2+ 页返回空 items 时仍保留"上一页"入口。
    const nextCursor = data.next_cursor ? String(data.next_cursor) : '';
    const pagerRow = pagerBar({
      pageIndex: resultsPageIndex,
      totalPages: data.total_pages,
      hasPrev: resultsPageIndex > 0,
      hasNext: !!nextCursor,
      onPrev: () => {
        resultsPageIndex -= 1;
        return renderRunScores(body, renderToken);
      },
      onNext: () => {
        resultsCursorStack = resultsCursorStack.slice(0, resultsPageIndex + 1);
        resultsCursorStack.push(nextCursor);
        resultsPageIndex += 1;
        return renderRunScores(body, renderToken);
      },
    });
    if (!items.length) {
      // 空态分支：空分页优先（保留上一页入口），其次按有无"用户筛选"
      // 给下一步；target_kind 已从筛选对象清除，锁定类型不计入用户筛选
      const hasUserFilter = RUN_SCORES_FILTER_KEYS.some((key) => filters[key]);
      if (resultsPageIndex > 0) {
        dataBody.appendChild(el('div', 'empty-state', '当前分页无结果，请返回上一页'));
      } else if (hasUserFilter) {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无评估结果，可调整或清空筛选条件后重试。'));
      } else {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无运行评分，运行人评提交后将展示在此处。'));
      }
      dataBody.appendChild(pagerRow);
      return;
    }
    const sessionTitles = await loadSessionTitles();
    // loadSessionTitles 跨模块缓存 Promise，解析晚于切换/重渲染时旧渲染
    // 不得把旧 items 追加回 DOM（与列表响应同等的过期防护）
    if (token !== renderToken || seq !== viewRequestSeq[seqKey]) return;
    const rows = items.map((item) => [
      resultSessionCell({ session_id: item.session_id, target_service: item.target_service }, sessionTitles),
      item.run_id,
      runScoreCell(item.metrics),
      formatTime(item.last_created_at),
      actionButton('详情', () => openRunModal(item, sessionTitles)),
    ]);
    dataBody.appendChild(scrollTable(table(
      ['来源会话', 'AgentRun', '评分', '最近时间', '操作'], rows)));
    dataBody.appendChild(pagerRow);
  }

  // 评分值文本化：undefined/null 显示 '-'，对象 JSON 文本，0/false 原样
  // 字符串化（textContent 渲染，载荷不注入节点）
  function runScoreValue(value) {
    if (value === undefined || value === null) return '-';
    if (typeof value === 'object') return JSON.stringify(value);
    return String(value);
  }

  // 评分列：同 run 多指标逐行展示 指标名=值 + 状态徽标（passed 三态判定
  // 复用 resultState 的 Mi-#19 口径）
  function runScoreCell(metrics) {
    const cell = el('div', 'evaluations-run-score-cell');
    (Array.isArray(metrics) ? metrics : []).forEach((m) => {
      const line = el('div', 'evaluations-run-score-line');
      const label = el('span', 'evaluations-run-score-metric');
      label.textContent = m.metric_name + '=' + runScoreValue(m.typed_value);
      line.appendChild(label);
      line.appendChild(statusBadge(resultState(m)));
      cell.appendChild(line);
    });
    return cell;
  }

  // run 级详情弹窗：仅消费列表项已加载数据，复用列表 sessionTitles，
  // 打开零额外请求；全部字段经 textContent/字符串参数渲染
  function openRunModal(item, sessionTitles) {
    const frame = buildFrame('运行详情');
    const kv = el('div', 'policy-list');
    kv.appendChild(kvRow('服务', item.target_service || '-'));
    kv.appendChild(kvRow('Run ID', item.run_id));
    kv.appendChild(kvRow('来源会话', resultSessionCell(
      { session_id: item.session_id, target_service: item.target_service },
      sessionTitles)));
    frame.form.appendChild(kv);
    frame.form.appendChild(el('div', 'evaluations-note',
      '当前筛选下的评分结果（非该运行全量历史）。'));
    frame.form.appendChild(scrollTable(table(
      ['指标', '值', '状态', '评分来源', '时间'],
      (Array.isArray(item.metrics) ? item.metrics : []).map((m) => [
        m.metric_name,
        runScoreValue(m.typed_value),
        statusBadge(resultState(m)),
        m.provenance,
        formatTime(m.created_at),
      ]),
    )));
    const actions = el('div', 'providers-form__actions');
    actions.appendChild(actionButton('关闭', () => closeBackdrop(frame.backdrop)));
    frame.form.appendChild(actions);
    mountDetailModal(frame);
  }

  async function openResultModal(resultId) {
    let detail;
    try {
      detail = await api.evaluations.getResult(resultId);
    } catch (err) {
      alertMessage('结果详情加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    if (!detail || typeof detail !== 'object' || !detail.result
      || Array.isArray(detail) || typeof detail.result !== 'object'
      || Array.isArray(detail.result)) {
      alertMessage('结果详情加载失败：非法响应');
      return;
    }
    const frame = buildFrame('评分详情');
    const form = frame.form;
    const result = detail.result || {};
    const evidence = detail.evidence_summary || {};
    const target = (detail.sample && detail.sample.target)
      || (detail.feedback && detail.feedback.target) || evidence.target || {};
    const kv = el('div', 'policy-list');
    kv.appendChild(kvRow('结果ID', result.result_id || resultId));
    const sessionTitles = await loadSessionTitles();
    kv.appendChild(kvRow('来源会话', resultSessionCell({ session_id: target.session_id, target_service: target.service }, sessionTitles)));
    kv.appendChild(kvRow('Run ID', target.run_id || '—'));
    kv.appendChild(kvRow('实验', resultExperimentCell(detail.job && detail.job.experiment_id)));
    kv.appendChild(kvRow('指标', result.metric_name));
    kv.appendChild(kvRow('来源', result.provenance));
    kv.appendChild(kvRow('状态', statusBadge(resultState(result))));
    kv.appendChild(kvRow('值', result.typed_value === null || result.typed_value === undefined ? '-' : result.typed_value));
    kv.appendChild(kvRow('时间', formatTime(result.created_at)));
    kv.appendChild(kvRow('证据快照 hash', evidence.snapshot_hash || '-'));
    kv.appendChild(kvRow('捕获时间', formatTime(evidence.captured_at)));
    form.appendChild(kv);

    // spec-261123："结果"文字框：状态含义/判定值/结果说明/跳过原因/
    // 失败分类/作业状态/领取次数/评估 Trace，段间空行，textContent 安全渲染
    form.appendChild(blockTitle('结果',
      '状态含义/判定值/结果说明/跳过原因/失败分类/作业状态与领取次数，来自评估器与作业账本'));
    const resultDetailBox = el('pre', 'evaluations-result-detail');
    resultDetailBox.textContent = buildResultDetailText(result, detail.job);
    form.appendChild(resultDetailBox);

    const deliveries = detail.deliveries || [];
    if (deliveries.length) {
      form.appendChild(blockTitle('投递'));
      form.appendChild(scrollTable(table(
        ['Sink', '投影', '状态', { label: '尝试', numeric: true }],
        deliveries.map((d) => [
          d.sink,
          d.projection_version,
          statusBadge(d.status),
          d.attempt_count,
        ]),
      )));
      // Langfuse 会话详情直达：session_id 与 Langfuse sessionId 同源
      // （langfuse_sink target_fields.sessionId），经单源 helper 拼接；
      // run 粒度等无 session_id 的评分不渲染入口，禁止回落 List 误导。
      const sessionLangfuseUrl = typeof langfuseSessionUrl === 'function'
        ? langfuseSessionUrl(target.session_id) : '';
      if (sessionLangfuseUrl) {
        const langfuseRow = document.createElement('div');
        langfuseRow.appendChild(externalLink(sessionLangfuseUrl, '查看 Langfuse 会话'));
        form.appendChild(langfuseRow);
      }
      // M3c：不可变历史 vs 本地 latest 口径
      form.appendChild(el('div', 'evaluations-note',
        'Langfuse Score 为不可变历史投影；本地 latest 以 supersedes 链为准。'
        + 'pending/retry_wait/dead_letter 为投递等待，unlinked 为目标无真实'
        + '身份（不创建悬空 Score），verified 为已核实送达。'));
      // 可重试投递（dead_letter/retry_wait/disabled）提供重试入口
      const retryable = deliveries.find(
        (d) => ['dead_letter', 'retry_wait', 'disabled'].indexOf(d.status) >= 0,
      );
      if (retryable) {
        form.appendChild(actionButton('重试投递', async () => {
          try {
            await api.evaluations.retryDelivery({
              sink: retryable.sink,
              result_id: resultId,
            });
            alertMessage('投递已重置为 pending，等待 worker 补发');
            closeBackdrop(frame.backdrop);
            openResultModal(resultId);
          } catch (err) {
            alertMessage('重试失败：' + (err && err.message ? err.message : ''));
          }
        }));
      }
    }

    const actions = el('div', 'providers-form__actions');
    actions.appendChild(actionButton('关闭', () => closeBackdrop(frame.backdrop)));
    form.appendChild(actions);
    mountDetailModal(frame);
  }

  // ------------------------------------------------------------------
  // 待复核队列与受控导出（M1：逐 case 金标 + 内联 fixture/rubric）
  // ------------------------------------------------------------------

  // review 兼容页与人工标注页打通：review 不在左导 children（spec 4.1），
  // 提供站内入口链接（href /evaluations/annotations）
  function annotationEntryLink() {
    const link = el('a', 'otel-link', '前往人工标注');
    link.href = '/evaluations/annotations';
    return link;
  }

  async function renderReviewQueue(body, token) {
    body.textContent = '';
    const seq = ++viewRequestSeq.review;
    const wrap = el('div', 'evaluations-review');
    body.appendChild(wrap);

    // 原因筛选（低分/争议/异常三类；status 过滤无法代替），role=group
    wrap.appendChild(focusableChoiceGroup('review-reason', '待复核原因',
      REVIEW_REASONS.map(reason => [reason, REVIEW_REASON_LABELS[reason]]),
      reviewReason,
      // Mi7：切换闭包取点击时刻的 renderToken，避免捕获过期 token
      (reason) => {
        reviewReason = reason;
        renderReviewQueue(body, renderToken);
      }));

    // 低分阈值进 filterItem；非 low_score 原因明示阈值不生效，查询参数保留
    const thresholdInput = document.createElement('input');
    thresholdInput.name = 'threshold';
    thresholdInput.value = reviewThreshold;
    thresholdInput.addEventListener('change', () => {
      // Mi5：阈值变更立即以新阈值重查队列（原实现只更新状态不刷新）
      reviewThreshold = thresholdInput.value.trim() || '0.5';
      renderReviewQueue(body, renderToken);
    });
    const thresholdHelp = reviewReason === 'low_score'
      ? '仅 reason=低分 时生效，默认 0.5'
      : '当前原因为「' + REVIEW_REASON_LABELS[reviewReason]
        + '」，低分阈值不生效；切换回低分原因时恢复作用';
    const filterRow = filterBar();
    filterRow.appendChild(filterControl('低分阈值', thresholdInput, thresholdHelp));
    wrap.appendChild(filterRow);

    const listBody = el('div', 'evaluations-review__list');
    wrap.appendChild(listBody);
    showLoading(listBody, '待复核队列加载中...');
    // 50 是后端单次返回窗口大小而非每页大小；slice pager 只在窗口内翻页
    const params = { reason: reviewReason, threshold: reviewThreshold, limit: REVIEW_WINDOW_LIMIT };
    let data;
    try {
      data = await api.evaluations.listReviewQueue(params);
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.review) return;
      renderErrorState(listBody, '待复核队列加载失败：' + (err && err.message ? err.message : ''), () => renderReviewQueue(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.review) return;
    listBody.textContent = '';
    reviewItems = Array.isArray(data.items) ? data.items : [];
    reviewPageIndex = 0;
    const drawReview = () => {
      const sliced = slicePage(reviewItems, reviewPageIndex);
      if (!reviewItems.length) {
        const emptyText = reviewReason === 'low_score'
          ? '当前筛选暂无待复核结果，当前低分原因及阈值下无匹配，可切换原因或调高低分阈值以扩大候选范围。'
          : '当前筛选暂无待复核结果，可切换其他原因或等待符合条件的结果。';
        listBody.appendChild(el('div', 'empty-state', emptyText));
      } else {
        const rows = sliced.pageItems.map((item) => [
          item.result_id,
          item.metric_name,
          item.provenance,
          item.query ? item.query : '-',
          item.typed_value === undefined || item.typed_value === null ? '-' : String(item.typed_value),
          formatTime(item.created_at),
        ]);
        listBody.appendChild(scrollTable(table(
          ['结果ID', '指标', '来源', '样本 Query', '值', '时间'],
          rows,
        )));
      }
      listBody.appendChild(pagerBar({
        pageIndex: reviewPageIndex,
        hasPrev: reviewPageIndex > 0,
        hasNext: reviewPageIndex + 1 < sliced.totalPages,
        totalPages: sliced.totalPages,
        onPrev: () => {
          reviewPageIndex -= 1;
          listBody.textContent = '';
          drawReview();
        },
        onNext: () => {
          reviewPageIndex += 1;
          listBody.textContent = '';
          drawReview();
        },
      }));
    };
    drawReview();
    // 窗口边界明示：不宣称已覆盖全部候选
    wrap.appendChild(el('div', 'evaluations-note',
      '仅对本次返回窗口分页，最多 ' + REVIEW_WINDOW_LIMIT + ' 条。'));

    // 审核后受控导出：金标由审核人逐 case 提供（禁止自动生成），导出经
    // T12 校验并记录审批来源；导出范围为当前展示页切片
    if (reviewItems.length) {
      const currentReviewPage = () => slicePage(reviewItems, reviewPageIndex).pageItems;
      wrap.appendChild(actionButton('审核后导出数据集',
        () => openExportModal(currentReviewPage(), () => renderReviewQueue(body, renderToken))));
    }
    wrap.appendChild(annotationEntryLink());
  }

  // 导出条目 id 归一：复核队列行携带 result_id，标注队列行只携带
  // source_result_id（store.list_annotation_queue 投影）；载荷
  // source_result_ids 逐项取该 id（非空字符串），禁止 undefined/null
  // 入载荷——真实后端按非空字符串数组校验，否则 422 export_invalid
  function exportSourceIdOf(item) {
    const id = item.source_result_id !== undefined && item.source_result_id !== null
      ? item.source_result_id
      : item.result_id;
    return id === undefined || id === null ? '' : String(id);
  }

  // T12 审查修复项 1：openExportModal 异步防护——(1) 迟到弹窗：并发取
  // 上下文期间用户切换视图（renderToken 变化）后丢弃本次弹窗，不挂到新
  // 视图（与 Mi-#21 详情弹窗同口径）；(2) 重复弹窗：加载窗口内重复触发
  // 直接忽略，不叠加 backdrop（模块级 in-flight 布尔，加载完成或放弃后
  // 在 finally 释放）。
  let exportModalInFlight = false;
  async function openExportModal(items, onExported) {
    if (exportModalInFlight) return;
    exportModalInFlight = true;
    try {
      // 逐 case query 取数（受控证据路径，不伪造正文）：列表行自带 query
      // （待复核队列证据读取成功时）直接使用；标注队列行不携带 query，
      // 逐选中条目经标注上下文的受控证据投影（evidence.request，spec 6.2
      // 脱敏快照正文）取真实样本 query。证据不可用或上下文加载失败时
      // 置空串——不补造正文，金标仍只来自审核人输入。
      const requestToken = renderToken;
      const selected = await Promise.all(items.slice(0, 20).map(async (item) => {
        if (typeof item.query === 'string') return item;
        try {
          const context = await api.evaluations.getAnnotation(exportSourceIdOf(item));
          const request = context && context.evidence && context.evidence.request;
          return Object.assign({}, item, {
            query: typeof request === 'string' ? request : '',
          });
        } catch (_) {
          return Object.assign({}, item, { query: '' });
        }
      }));
      if (requestToken !== renderToken) return;
      buildExportModal(selected, items, onExported);
    } finally {
      exportModalInFlight = false;
    }
  }

  function buildExportModal(selected, items, onExported) {
    const frame = buildFrame('审核后导出（金标由审核人逐 case 提供，禁止自动生成）');
    const form = frame.form;
    const metaInputs = {};
    [
      // N6：数据集版本必填（不再默认 'v1' 静默兜底）
      ['dataset_id', '数据集 ID'],
      ['dataset_version', '数据集版本（必填）'],
      ['approved_by', '审批来源'],
    ].forEach(([key, label]) => {
      metaInputs[key] = inputRow(form, key, label, '');
    });
    form.appendChild(blockTitle('逐 case 金标（expected_output 必填；rubric 可选）'));
    const caseInputs = [];
    // Mi6：上限 20 case：内联导出面向人工逐条审核，防止整批无脑全导；
    // 截断时明确提示，不做静默缩减
    const caseList = el('div', 'evaluations-export-cases');
    if (items.length > selected.length) {
      // 截断提示挂进 modal 表单内（case 列表之前），不在队列局部作用域
      form.insertBefore(el('div', 'evaluations-hint',
        '仅导出前 20 条（共 ' + items.length + ' 条）'), caseList);
    }
    selected.forEach((item, index) => {
      const box = el('div', 'evaluations-export-case');
      box.appendChild(el('div', 'evaluations-export-case__meta',
        'case-' + (index + 1) + '（' + item.metric_name + '）样本 query：'
        + (item.query ? item.query : '（无快照 query，导出将失败）')));
      const expectedInput = document.createElement('input');
      expectedInput.name = 'expected_output_' + index;
      expectedInput.placeholder = '金标 expected_output';
      box.appendChild(expectedInput);
      const rubricInput = document.createElement('input');
      rubricInput.name = 'rubric_' + index;
      rubricInput.placeholder = '评阅说明（可选）';
      box.appendChild(rubricInput);
      caseList.appendChild(box);
      caseInputs.push({ item: item, expected: expectedInput, rubric: rubricInput });
    });
    form.appendChild(caseList);
    const errorLine = el('div', 'error-state');
    errorLine.hidden = true;
    form.appendChild(errorLine);
    formActions(form, '取消', '导出');
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const datasetId = metaInputs.dataset_id.value.trim();
      // N6：数据集版本必填（同版本重试 409 的提示路径要求显式版本）
      const datasetVersion = metaInputs.dataset_version.value.trim();
      const approvedBy = metaInputs.approved_by.value.trim();
      if (!datasetId || !datasetVersion || !approvedBy) {
        errorLine.textContent = '数据集 ID、数据集版本与审批来源必填';
        errorLine.hidden = false;
        return;
      }
      // 逐 case 组装：金标必须逐条提供；fixture/rubric 内容内联上传，
      // 服务端计算 sha 并落盘后走既有配对校验 + T12 回读（语义不变）
      const cases = [];
      for (let index = 0; index < caseInputs.length; index++) {
        const entry = caseInputs[index];
        // 输入控件取值统一 String 归一（测试 harness 的元素无默认 value）
        const expectedOutput = String((entry.expected && entry.expected.value) || '').trim();
        if (!expectedOutput) {
          errorLine.textContent = 'case-' + (index + 1) + ' 缺少金标 expected_output（由审核人提供）';
          errorLine.hidden = false;
          return;
        }
        const record = {
          case_id: 'case-' + (index + 1),
          input: { query: entry.item.query || '' },
          target_kind: 'agent_run',
          expected_output: expectedOutput,
          fixture_content: { query: entry.item.query || '' },
          slice: 'review',
        };
        const rubricText = String((entry.rubric && entry.rubric.value) || '').trim();
        if (rubricText) record.rubric_content = rubricText;
        cases.push(record);
      }
      try {
        const result = await api.evaluations.exportReviewQueue({
          dataset_id: datasetId,
          dataset_version: datasetVersion,
          target_kind: 'agent_run',
          approved_by: approvedBy,
          source_result_ids: selected.map(exportSourceIdOf),
          cases: cases,
        });
        closeBackdrop(frame.backdrop);
        // spec 4.4：导出完成后 dataset_id/version/hash/audit_id 可关联，
        // 并提示可进入数据集列表核对该版本
        alertMessage('导出完成：' + result.dataset_id + '/' + result.dataset_version
          + '（cases=' + result.case_count + '，hash=' + result.dataset_hash
          + '，audit_id=' + result.audit_id + '）。可在数据集列表查看该版本。');
        if (typeof onExported === 'function') onExported();
      } catch (err) {
        const code = err && err.message ? err.message : '';
        if (code === 'conflict') {
          errorLine.textContent = '该数据集版本已导出（409），请换版本号';
        } else if (code === 'identity_leak') {
          errorLine.textContent = '导出内容含身份键（422），请移除后再导出';
        } else {
          errorLine.textContent = '导出失败：' + code;
        }
        errorLine.hidden = false;
      }
    });
  }

  // ------------------------------------------------------------------
  // 评估器管理视图（Task 11/spec 4.2/4.3）：能力目录 + 列表 + 详情 +
  // 版本创建（组合由能力目录生成）+ 单版本在线启停（expected_revision CAS）
  // ------------------------------------------------------------------

  const EVALUATOR_METHOD_LABELS = {
    deterministic: '确定性公式',
    llm_judge: 'LLM Judge',
    human: '人工评分',
  };
  // 列表过滤状态（跨渲染保留用户选择，与 reviewReason 同模式）
  let evaluatorFilterTargetKind = '';
  let evaluatorFilterMethod = '';

  // spec 4.2：页面区分可配置/执行已就绪/未配置 Judge，不把注册成功当
  // 作执行成功；llm_judge 按 judge_configured 门控显示未就绪
  function evaluatorStatusText(entry, judgeConfigured) {
    if (entry.method === 'human') {
      return '可配置（人工评分，不参与自动执行）';
    }
    if (entry.judge_required && !judgeConfigured) {
      return '未配置 Judge（未就绪）';
    }
    return '执行已就绪';
  }

  function fieldRow(container, label, control) {
    const row = el('div', 'policy-item');
    row.appendChild(el('span', 'policy-k', label + '：'));
    row.appendChild(control);
    container.appendChild(row);
    return control;
  }

  // select 通用构建（真实 DOM 与 harness Element 均适用；value 仅在
  // 选项存在时设置，避免落空）
  function setOptions(select, options, current) {
    select.textContent = '';
    options.forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    });
    if (options.some(([value]) => value === current)) select.value = current;
    else if (options.length) select.value = options[0][0];
    return select;
  }

  function selectControl(name, options, current) {
    const select = document.createElement('select');
    // 共享下拉外观：自定义 chevron 与右侧边距，禁止裸用原生箭头（见 styles.css .select-control）
    select.className = 'select-control';
    select.name = name;
    return setOptions(select, options, current);
  }

  // 能力目录视图（独立顶导入口，只读）：展示服务端目录内可注册的指标组合。
  // 新建评估器入口保留在"评估器"页，本页不放任何写操作。
  async function renderCapabilities(body, token) {
    body.textContent = '';
    const seq = ++viewRequestSeq.capabilities;
    const wrap = el('div', 'evaluators-view');
    body.appendChild(wrap);
    if (body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.textContent = '';
    }
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '能力目录加载中...');

    let caps;
    try {
      caps = await api.evaluations.getCapabilities();
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.capabilities) return;
      renderErrorState(dataBody, '能力目录加载失败：' + (err && err.message ? err.message : ''),
        () => renderCapabilities(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.capabilities) return;
    dataBody.textContent = '';

    const judgeConfigured = !!(caps && caps.judge_configured);
    capabilitiesItems = (caps && Array.isArray(caps.entries)) ? caps.entries : [];
    capabilitiesPageIndex = 0;
    const drawCapabilities = () => {
      const sliced = slicePage(capabilitiesItems, capabilitiesPageIndex);
      if (!capabilitiesItems.length) {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无能力目录。当前没有可注册的指标组合，请检查评估服务配置。'));
      } else {
        // spec 4.2：仅目录内组合可注册自动评估器（页面即目录，不再重复标题）
        dataBody.appendChild(scrollTable(table(
          ['方法', '指标', '描述', '目标类型', '输出类型', '状态'],
          sliced.pageItems.map((entry) => [
            EVALUATOR_METHOD_LABELS[entry.method] || entry.method,
            entry.metric_name,
            entry.description || '-',
            entry.target_kind,
            entry.output_type,
            evaluatorStatusText(entry, judgeConfigured),
          ]),
        )));
      }
      dataBody.appendChild(pagerBar({
        pageIndex: capabilitiesPageIndex,
        hasPrev: capabilitiesPageIndex > 0,
        hasNext: capabilitiesPageIndex + 1 < sliced.totalPages,
        totalPages: sliced.totalPages,
        onPrev: () => {
          capabilitiesPageIndex -= 1;
          dataBody.textContent = '';
          drawCapabilities();
        },
        onNext: () => {
          capabilitiesPageIndex += 1;
          dataBody.textContent = '';
          drawCapabilities();
        },
      }));
    };
    drawCapabilities();
  }

  async function renderEvaluators(body, token) {
    body.textContent = '';
    const seq = ++viewRequestSeq.evaluators;
    const wrap = el('div', 'evaluators-view');
    body.appendChild(wrap);
    if (body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.textContent = '';
      body.evaluationsHeaderActions.appendChild(
        actionButton('新建评估器', () => openEvaluatorCreate()));
    }

    // spec 4.2：按目标种类/方法过滤（filterBar + filterItem，name 合同不变）
    const filterRow = filterBar();
    const kindSelect = selectControl('evaluator_target_kind_filter', [
      ['', '全部目标'],
      ['agent_run', 'agent_run'],
      ['retrieval', 'retrieval'],
    ], evaluatorFilterTargetKind);
    const methodSelect = selectControl('evaluator_method_filter', [
      ['', '全部方法'],
      ['deterministic', EVALUATOR_METHOD_LABELS.deterministic],
      ['llm_judge', EVALUATOR_METHOD_LABELS.llm_judge],
      ['human', EVALUATOR_METHOD_LABELS.human],
    ], evaluatorFilterMethod);
    kindSelect.addEventListener('change', () => {
      evaluatorFilterTargetKind = kindSelect.value;
      resetEvaluatorPaging();
      renderEvaluators(body, renderToken);
    });
    methodSelect.addEventListener('change', () => {
      evaluatorFilterMethod = methodSelect.value;
      resetEvaluatorPaging();
      renderEvaluators(body, renderToken);
    });
    filterRow.append(
      filterControl('目标类型', kindSelect),
      filterControl('方法', methodSelect),
    );
    wrap.appendChild(filterRow);
    // 数据容器独立于筛选区：Loading/错误/重试只清 dataBody，筛选不被删除
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '评估器列表加载中...');

    const params = { limit: LIST_PAGE_SIZE };
    const evaluatorCursor = evaluatorCursorStack[evaluatorPageIndex];
    if (evaluatorCursor) params.cursor = evaluatorCursor;
    if (evaluatorFilterTargetKind) params.target_kind = evaluatorFilterTargetKind;
    if (evaluatorFilterMethod) params.method = evaluatorFilterMethod;

    let data;
    try {
      // 能力目录已拆为独立顶导入口（renderCapabilities），列表页只拉注册评估器
      data = await api.evaluations.listEvaluators(params);
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.evaluators) return;
      const code = err && err.message ? err.message : '';
      // protocol_invalid 在本接口也用于非游标参数校验，只有确认游标错误码
      // 才走「重新查询」清栈分支；其余错误保留筛选并给出同页重试。
      if (evaluatorCursor && code === 'cursor_invalid') {
        dataBody.textContent = '';
        dataBody.appendChild(el('div', 'error-state',
          '游标已失效（过滤条件或查询快照不匹配），请重新查询'));
        dataBody.appendChild(actionButton('重新查询', () => {
          resetEvaluatorPaging();
          renderEvaluators(body, renderToken);
        }));
        return;
      }
      renderErrorState(dataBody, '评估器列表加载失败：' + code,
        () => renderEvaluators(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.evaluators) return;
    dataBody.textContent = '';

    const items = Array.isArray(data.items) ? data.items : [];
    // 评估器列表兼作 Judge 模型候选来源：llm_judge 定义的非空 judge_model
    // 全串去重合并进共享缓存，供建表单 datalist（过滤/分页只增不减）
    mergeJudgeSupplyModels(items);
    const evaluatorNextCursor = data.next_cursor ? String(data.next_cursor) : '';
    // pager 先于空态构建：第 2+ 页空响应仍保留上一页出口
    const evaluatorPager = pagerBar({
      pageIndex: evaluatorPageIndex,
      totalPages: data.total_pages,
      hasPrev: evaluatorPageIndex > 0,
      hasNext: !!evaluatorNextCursor,
      onPrev: () => {
        evaluatorPageIndex -= 1;
        return renderEvaluators(body, renderToken);
      },
      onNext: () => {
        evaluatorCursorStack = evaluatorCursorStack.slice(0, evaluatorPageIndex + 1);
        evaluatorCursorStack.push(evaluatorNextCursor);
        evaluatorPageIndex += 1;
        return renderEvaluators(body, renderToken);
      },
    });
    if (!items.length) {
      const hasEvaluatorFilter = !!(evaluatorFilterTargetKind || evaluatorFilterMethod);
      if (evaluatorPageIndex > 0) {
        dataBody.appendChild(el('div', 'empty-state', '当前分页无结果，请返回上一页'));
      } else {
        dataBody.appendChild(el('div', 'empty-state', hasEvaluatorFilter
          ? '暂无评估器。当前目标类型/方法筛选下无匹配注册项，可调整筛选条件，或点击「新建评估器」注册版本。'
          : '暂无评估器。尚未注册任何评估器版本，可点击「新建评估器」新建；'
            + '自动评估器仅能注册能力目录内的指标组合（见顶导「能力目录」），注册后默认关闭，需显式启用。'));
      }
      dataBody.appendChild(evaluatorPager);
      return;
    }

    dataBody.appendChild(scrollTable(table(
      ['评估器ID', '版本', '指标', '目标类型', '方法',
        { label: '在线状态', tip: '注册版本默认关闭，启用后才参与线上评估；同指标多版本结果不合并' },
        '操作'],
      items.map((item) => {
        const usage = item.usage || {};
        const actions = el('div', 'row-actions');
        if (item.method === 'human') {
          // spec 4.3：human 禁线上启用（服务端 422）。置灰不隐藏：
          // 无 click handler，不产生任何启停请求
          actions.appendChild(disabledButton('停用'));
          actions.appendChild(actionButton('详情',
            () => openEvaluatorDetail(item.definition_id, item.version)));
        } else {
          // 操作列顺序：先启停后详情
          const enable = !usage.online_enabled;
          const clickToken = renderToken;
          actions.appendChild(actionButton(enable ? '启用' : '停用', async () => {
            try {
              await api.evaluations.setEvaluatorUsage(item.definition_id, item.version, {
                online_enabled: enable,
                expected_revision: typeof usage.revision === 'number' ? usage.revision : 0,
              });
              alertMessage(enable ? '评估器已启用在线使用' : '评估器已停用在线使用');
              if (clickToken === renderToken) await renderEvaluators(body, renderToken);
            } catch (err) {
              if (err && err.message === 'conflict') {
                // spec 4.3：409 不自动覆盖，提示显式刷新后重试
                alertMessage('启停冲突（409）：使用状态已被他人修改，请刷新列表核对后重试');
              } else {
                alertMessage('在线启停失败：' + (err && err.message ? err.message : ''));
              }
            }
          }));
          actions.appendChild(actionButton('详情',
            () => openEvaluatorDetail(item.definition_id, item.version)));
        }
        // 历史：多版本评估器展示旧版本列表（modal 风格对齐数据集历史）
        if (item.version_count > 1) {
          actions.appendChild(actionButton('历史',
            () => openEvaluatorHistory(
              item.definition_id, item.target_kind, item.version)));
        } else {
          actions.appendChild(disabledButton('历史'));
        }
        return [
          item.definition_id,
          item.version,
          item.metric_name,
          item.target_kind,
          EVALUATOR_METHOD_LABELS[item.method] || item.method,
          statusBadge(usage.online_enabled ? '已启用' : '未启用'),
          actions,
        ];
      }),
      )));
    dataBody.appendChild(evaluatorPager);
  }

  // 评估器历史 Modal：详情类弹窗（mountDetailModal，不响应 backdrop 点击），
  // 风格对齐数据集历史。数据按评估器 ID + 目标类型实时拉取全部版本（新到
  // 旧），过滤掉列表已展示的最新版本；行内详情先关弹窗再复用
  // openEvaluatorDetail 的逐版本请求。
  async function openEvaluatorHistory(definitionId, targetKind, latestVersion) {
    const requestToken = renderToken;
    let data;
    try {
      const params = { definition_id: definitionId };
      if (targetKind) params.target_kind = targetKind;
      data = await api.evaluations.listEvaluators(params);
    } catch (err) {
      alertMessage('评估器历史加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    if (requestToken !== renderToken) return;
    const items = (Array.isArray(data.items) ? data.items : [])
      .filter((item) => item.version !== latestVersion);
    const frame = buildFrame('评估器历史 - ' + definitionId);
    if (!items.length) {
      frame.form.appendChild(el('div', 'empty-state', '该评估器没有历史版本。'));
    } else {
      frame.form.appendChild(scrollTable(table(
        ['版本', '目标类型', '方法', '在线状态', '操作'],
        items.map((item) => {
          const usage = item.usage || {};
          const actions = el('div', 'row-actions');
          actions.appendChild(actionButton('详情', () => {
            closeBackdrop(frame.backdrop);
            openEvaluatorDetail(item.definition_id, item.version);
          }));
          return [
            item.version,
            item.target_kind || '-',
            EVALUATOR_METHOD_LABELS[item.method] || item.method,
            statusBadge(usage.online_enabled ? '已启用' : '未启用'),
            actions,
          ];
        }),
      )));
    }
    formActions(frame.form, '关闭');
    mountDetailModal(frame);
  }

  async function openEvaluatorDetail(definitionId, version) {
    // Mi-#21：迟到响应防护——离开评估页后返回的详情请求不得再打开弹窗
    const requestToken = renderToken;
    let detail;
    try {
      detail = await api.evaluations.getEvaluator(definitionId, version);
    } catch (err) {
      alertMessage('评估器详情加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    if (requestToken !== renderToken) return;
    const definition = (detail && detail.definition) || {};
    const usage = (detail && detail.usage) || {};
    const frame = buildFrame('评估器详情 ' + definitionId + ' / ' + version);
    const form = frame.form;
    const kv = el('div', 'policy-list');
    kv.appendChild(kvRow('指标', definition.metric_name));
    kv.appendChild(kvRow('方法', EVALUATOR_METHOD_LABELS[definition.method] || definition.method));
    kv.appendChild(kvRow('目标类型', definition.target_kind));
    kv.appendChild(kvRow('输出类型', definition.output_type));
    kv.appendChild(kvRow('方向', definition.direction));
    kv.appendChild(kvRow('必填字段', (definition.required_fields || []).join(', ')));
    if (definition.method === 'llm_judge') {
      kv.appendChild(kvRow('Judge 模型', definition.judge_model));
      kv.appendChild(kvRow('Judge 参数',
        definition.judge_params ? JSON.stringify(definition.judge_params) : ''));
    }
    kv.appendChild(kvRow('超时（秒）', definition.timeout_seconds));
    kv.appendChild(kvRow('材料 hash', definition.material_hash));
    kv.appendChild(kvRow('在线状态',
      usage.online_enabled ? '已启用' : '未启用（注册后自动使用默认关闭）'));
    kv.appendChild(kvRow('使用状态版本', usage.revision));
    // spec 4.2/C10：只读校准摘要；未校准显示未校准，不显示"已校准"
    const calibration = detail && detail.calibration;
    if (calibration) {
      kv.appendChild(kvRow('校准状态', calibration.effective_state === 'passed'
        ? '已校准（通过）'
        : 'advisory：' + (calibration.message || '校准结果仅供参考')));
    } else {
      kv.appendChild(kvRow('校准状态', '未校准（advisory，校准结果仅供参考）'));
    }
    form.appendChild(kv);
    const actions = el('div', 'providers-form__actions');
    actions.appendChild(actionButton('基于此版本新建', () => {
      closeBackdrop(frame.backdrop);
      return openEvaluatorCreate(definition);
    }));
    actions.appendChild(actionButton('关闭', () => closeBackdrop(frame.backdrop)));
    form.appendChild(actions);
    mountDetailModal(frame);
  }

  async function openEvaluatorCreate(source) {
    // 创建表单由服务端能力目录生成（spec 4.2）；复制场景预填来源定义，
    // 版本必须填写新版本号（不可覆盖旧版本）
    const requestToken = renderToken;
    let caps;
    try {
      caps = await api.evaluations.getCapabilities();
    } catch (err) {
      alertMessage('能力目录加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    if (requestToken !== renderToken) return;
    const entries = (caps && Array.isArray(caps.entries)) ? caps.entries : [];
    const frame = buildFrame(source ? '基于此版本新建评估器' : '新建评估器');
    const form = frame.form;
    // spec 4.3：新版本注册后自动使用默认关闭——提交前明示，不隐藏
    form.appendChild(el('div', 'evaluations-note',
      '组合以能力目录为准；新版本注册后自动使用默认关闭（不参与线上评估），需手动启用。'));
    const errorLine = el('div', 'error-state');
    errorLine.hidden = true;

    const methods = [];
    entries.forEach((entry) => {
      if (!methods.includes(entry.method)) methods.push(entry.method);
    });
    if (!methods.length) {
      // 目录为空时不凭空生成目录外方法选项（表单组合一律以目录为准）
      alertMessage('能力目录为空，无法新建评估器');
      return;
    }

    // Judge 模型候选/有效供给模型（plan-261116 T6）：每次打开弹窗都刷新
    // GET 投影（不要求先访问模型页）；失败仅提示并保留手输，不阻塞表单。
    // per-modal 状态：touched（用户触碰过输入）/defaultApplied（异步默认
    // 只回填一次）/draft（方法切换重建时保留已编辑草稿）。
    let judgeModelTouched = false;
    let judgeModelDefaultApplied = false;
    let judgeModelDraft = source && source.method === 'llm_judge'
      ? String(source.judge_model || '') : '';
    const judgeModelListId = 'judge-model-options-' + (++judgeModelListSeq);
    const applyJudgeModelDefault = () => {
      // source（复制）冻结值永远优先，异步默认不覆盖；触碰过/已回填/弹窗已关/
      // 已离开评估页/方法不是 llm_judge/无有效供给均不回填（无供给留空，
      // 提交省略 judge_model 由后端归一为 None）
      if (source || judgeModelTouched || judgeModelDefaultApplied) return;
      if (openBackdrop !== frame.backdrop) return;
      if (requestToken !== renderToken) return;
      if (methodSelect.value !== 'llm_judge' || !judgeModelInput) return;
      const projection = judgeSupply.projection;
      const model = String((projection && projection.effective_model) || '');
      if (!model) return;
      judgeModelInput.value = model;
      judgeModelDraft = model;
      judgeModelDefaultApplied = true;
    };
    api.evaluations.getJudgeModel().then((projection) => {
      if (requestToken !== renderToken) return;
      judgeSupply.projection = projection || null;
      if (projection && typeof projection.config_version === 'number') {
        judgeSupply.configVersion = projection.config_version;
      }
      // 冷缓存缺口：表单已打开且 judge 字段在位时同步重建 datalist，
      // 使异步回填的供给候选与预填值同批可见（方法切走时不重建，
      // 切回时 rebuildDynamic 已按新缓存构建）
      if (openBackdrop === frame.backdrop && judgeModelInput && judgeModelDatalist) {
        fillJudgeModelDatalist(judgeModelDatalist);
      }
      applyJudgeModelDefault();
    }).catch(() => {
      if (requestToken !== renderToken) return;
      alertMessage('默认评估模型候选加载失败，可手动输入 Judge 模型');
    });

    const definitionIdInput = document.createElement('input');
    definitionIdInput.name = 'definition_id';
    definitionIdInput.value = source ? String(source.definition_id || '') : '';
    const versionInput = document.createElement('input');
    versionInput.name = 'version';
    versionInput.value = '';
    versionInput.placeholder = source
      ? '必须填写新版本号（不可覆盖旧版本）'
      : '版本号（如 v1）';
    const methodSelect = selectControl('method',
      methods.map((m) => [m, EVALUATOR_METHOD_LABELS[m] || m]),
      source && methods.includes(source.method) ? source.method : methods[0]);

    // 动态区：指标/输出类型/Judge 字段随 method 变化，选项由能力目录生成
    const dynamic = el('div', 'evaluations-export-cases');
    const kindSelect = document.createElement('select');
    kindSelect.className = 'select-control';
    kindSelect.name = 'target_kind';
    let metricControl = null;
    let outputTypeSelect = null;
    let judgeModelInput = null;
    let judgeModelDatalist = null;
    let rubricInput = null;
    let temperatureInput = null;

    function rebuildDynamic() {
      dynamic.textContent = '';
      const method = methodSelect.value;
      const scoped = entries.filter((entry) => entry.method === method);
      const kinds = [];
      scoped.forEach((entry) => {
        if (!kinds.includes(entry.target_kind)) kinds.push(entry.target_kind);
      });
      setOptions(kindSelect, kinds.map((k) => [k, k]),
        source && source.method === method ? String(source.target_kind || '') : '');
      fieldRow(dynamic, '目标类型', kindSelect);
      // 指标：human 由创建者提供（目录按 '*' 声明）；其余从目录生成
      if (method === 'human') {
        metricControl = document.createElement('input');
        metricControl.name = 'metric_name';
        metricControl.value = source && source.method === 'human'
          ? String(source.metric_name || '') : '';
      } else {
        const metrics = [];
        scoped.forEach((entry) => {
          if (!metrics.includes(entry.metric_name)) metrics.push(entry.metric_name);
        });
        metricControl = document.createElement('select');
        metricControl.className = 'select-control';
        metricControl.name = 'metric_name';
        setOptions(metricControl, metrics.map((m) => [m, m]),
          source && source.method === method && metrics.includes(source.metric_name)
            ? source.metric_name : (metrics[0] || ''));
      }
      fieldRow(dynamic, '指标', metricControl);
      // 输出类型：目录内该组合可用的 output_type（human 枚举全部）
      const outputTypes = [];
      scoped.forEach((entry) => {
        if (method !== 'human' && entry.metric_name !== metricControl.value) return;
        if (!outputTypes.includes(entry.output_type)) outputTypes.push(entry.output_type);
      });
      outputTypeSelect = document.createElement('select');
      outputTypeSelect.className = 'select-control';
      outputTypeSelect.name = 'output_type';
      setOptions(outputTypeSelect, outputTypes.map((o) => [o, o]),
        source && source.method === method && outputTypes.includes(source.output_type)
          ? source.output_type : (outputTypes[0] || ''));
      fieldRow(dynamic, '输出类型', outputTypeSelect);
      if (method === 'llm_judge') {
        judgeModelInput = document.createElement('input');
        judgeModelInput.name = 'judge_model';
        // 方法切走再切回保留已编辑草稿；datalist 候选 = 默认 Provider model
        // + env 基线模型 + 已加载 llm_judge 定义 judge_model（全串去重），
        // 经 input[list] 关联
        judgeModelInput.value = judgeModelDraft;
        judgeModelInput.setAttribute('list', judgeModelListId);
        judgeModelInput.addEventListener('input', () => {
          judgeModelTouched = true;
          judgeModelDraft = judgeModelInput.value;
        });
        fieldRow(dynamic, 'Judge 模型', judgeModelInput);
        judgeModelDatalist = document.createElement('datalist');
        judgeModelDatalist.setAttribute('id', judgeModelListId);
        fillJudgeModelDatalist(judgeModelDatalist);
        dynamic.appendChild(judgeModelDatalist);
        rubricInput = document.createElement('textarea');
        rubricInput.name = 'rubric';
        rubricInput.value = source && source.method === 'llm_judge' && source.judge_params
          ? String(source.judge_params.rubric || '') : '';
        fieldRow(dynamic, '评审标准 rubric（可选）', rubricInput);
        temperatureInput = document.createElement('input');
        temperatureInput.name = 'temperature';
        temperatureInput.value = source && source.method === 'llm_judge'
          && source.judge_params && source.judge_params.temperature !== undefined
          ? String(source.judge_params.temperature) : '';
        fieldRow(dynamic, '采样温度 temperature（可选，0-2）', temperatureInput);
      } else {
        judgeModelInput = null;
        judgeModelDatalist = null;
        rubricInput = null;
        temperatureInput = null;
      }
    }

    fieldRow(form, '评估器 ID', definitionIdInput);
    fieldRow(form, '版本', versionInput);
    fieldRow(form, '方法', methodSelect);
    form.appendChild(dynamic);
    methodSelect.addEventListener('change', () => {
      rebuildDynamic();
      // 切回 llm_judge 时热缓存即时预填（backdrop 已挂载，守卫放行）
      applyJudgeModelDefault();
    });
    rebuildDynamic();
    form.appendChild(errorLine);
    const actions = formActions(form, '取消', '注册版本');
    const submit = actions.querySelector('button[type="submit"]');
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));
    // 热缓存即时预填：rebuildDynamic 内不调 applyJudgeModelDefault（彼时
    // backdrop 未挂载会被守卫拒绝），弹窗打开后补一次
    applyJudgeModelDefault();

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const definitionId = definitionIdInput.value.trim();
      const version = versionInput.value.trim();
      const metricName = String(metricControl.value || '').trim();
      if (!definitionId || !version || !metricName) {
        errorLine.textContent = '评估器 ID、版本与指标必填'
          + (source ? '；复制版本必须填写新版本号，不可覆盖旧版本' : '');
        errorLine.hidden = false;
        return;
      }
      if (source && version === String(source.version)) {
        errorLine.textContent = '复制版本必须使用新版本号（'
          + source.version + ' 已存在，不可覆盖）';
        errorLine.hidden = false;
        return;
      }
      const payload = {
        definition_id: definitionId,
        version: version,
        metric_name: metricName,
        target_kind: String(kindSelect.value || ''),
        method: String(methodSelect.value || ''),
        output_type: String(outputTypeSelect.value || ''),
      };
      if (payload.method === 'llm_judge') {
        // T6：空输入不再强制拦截——未触碰且无有效供给（或显式清空）时
        // 省略 judge_model 字段提交，后端归一为 None（跟随默认供给）；
        // 显式输入仍严格校验：原始值不 trim 直接提交（服务端权威校验），
        // 客户端只拦截首尾空白/控制字符/>200 码点（按 Array.from 码点计数）
        const rawJudgeModel = judgeModelInput ? String(judgeModelInput.value) : '';
        if (rawJudgeModel !== '') {
          const judgeModelCodepoints = Array.from(rawJudgeModel);
          const hasJudgeModelControlChar = judgeModelCodepoints.some((ch) => {
            const cp = ch.codePointAt(0);
            return cp <= 0x001f || (cp >= 0x007f && cp <= 0x009f);
          });
          if (rawJudgeModel !== rawJudgeModel.trim() || hasJudgeModelControlChar
              || judgeModelCodepoints.length > 200) {
            errorLine.textContent = 'Judge 模型不得含首尾空白或控制字符，'
              + '且不超过 200 字符';
            errorLine.hidden = false;
            return;
          }
          payload.judge_model = rawJudgeModel;
        }
        const judgeParams = {};
        const rubric = rubricInput ? rubricInput.value.trim() : '';
        if (rubric) judgeParams.rubric = rubric;
        const temperatureRaw = temperatureInput ? temperatureInput.value.trim() : '';
        if (temperatureRaw !== '') {
          const parsed = Number.parseFloat(temperatureRaw);
          if (Number.isNaN(parsed) || parsed < 0 || parsed > 2) {
            errorLine.textContent = '采样温度必须是有效数字（0-2）';
            errorLine.hidden = false;
            return;
          }
          judgeParams.temperature = parsed;
        }
        payload.judge_params = judgeParams;
      }
      submit.disabled = true;
      try {
        await api.evaluations.createEvaluator(payload);
        closeBackdrop(frame.backdrop);
        // spec 4.2：注册成功≠执行成功，不显示"已就绪执行"类文案；
        // 新版本自动使用默认关闭，需手动启用
        alertMessage('评估器版本已注册（自动使用默认关闭，需手动启用才会用于线上评估）');
        if (requestToken === renderToken && isActive()) await refresh();
      } catch (err) {
        const code = err && err.message ? err.message : '';
        if (code === 'conflict') {
          // spec 4.3：409 保留输入、提示显式刷新，不自动覆盖
          errorLine.textContent = '注册冲突（409）：该版本已存在且内容不同。'
            + '输入已保留；请填写新版本号或刷新列表核对后重试，不会自动覆盖。';
        } else if (code === 'unsupported_capability') {
          errorLine.textContent = '能力目录不支持该组合（422），请按目录选择指标/目标/方法。';
        } else {
          errorLine.textContent = '注册失败：' + code;
        }
        errorLine.hidden = false;
      } finally { submit.disabled = false; }
    });
  }

  // ------------------------------------------------------------------
  // 模型页（plan-261116 T6）：Sector 1 Provider 列表（与 Agent 共用同一
  // 注册表，/chat/providers 读取 + 页内编辑/新增/删除 modal；评估默认是
  // C 类配置引用，走 PATCH /chat/evaluations/config CAS）+ Sector 2 评估
  // 供给精简只读区。is_active 仅展示 Agent 当前启用状态，本页不调用
  // activateProvider；设默认/取消默认成功后以服务端返回版本刷新 CAS 基线
  // 并重载列表与供给投影（版本未变也刷新）；409 保留操作意图提示显式
  // 刷新，禁止自动重放；列表/投影加载失败保留骨架可重试。Provider CRUD
  // 成功后同时重拉列表与投影；modal 内密钥输入不落浏览器存储，关闭/成功
  // 即清空。总闸关闭由 render() 的 status 探测拦截，不进入本视图。
  // ------------------------------------------------------------------

  // Judge 供给共享缓存（plan-261116 T6）：providers 为 Provider 注册表
  // 列表快照，projection 为 GET /judge-model 投影快照，configVersion 为
  // 下一次 CAS 的 expected_version（GET config_version 起步，PATCH 响应
  // version 跟进）；usedModels 由评估器列表合并（method=llm_judge 且
  // judge_model 非空，全串去重），供建表单 datalist。
  const judgeSupply = { providers: [], projection: null, configVersion: null, usedModels: [] };
  // Provider 列表 slice 分页页码（组件 2）：快照即 judgeSupply.providers，
  // 进入/刷新/CRUD 成功由 renderJudgeModel 统一回第 1 页，翻页只改本状态。
  let judgeSupplyProviderPageIndex = 0;

  function mergeJudgeSupplyModels(items) {
    const models = judgeSupply.usedModels.slice();
    (Array.isArray(items) ? items : []).forEach((item) => {
      if (!item || item.method !== 'llm_judge') return;
      const model = String(item.judge_model || '');
      if (model && !models.includes(model)) models.push(model);
    });
    judgeSupply.usedModels = models;
  }

  // 填充 datalist 选项（textContent 渲染）；调用方负责节点创建/挂载，
  // 可重复调用以同步重建（异步投影回填后的冷缓存缺口修复）
  function fillJudgeModelDatalist(datalist) {
    datalist.textContent = '';
    judgeModelCandidateValues().forEach((model) => {
      const option = document.createElement('option');
      option.value = model;
      option.textContent = model;
      datalist.appendChild(option);
    });
    return datalist;
  }

  // datalist 候选（plan-261116 T6）：默认 Provider model + env 基线模型 +
  // 已加载 llm_judge 定义 judge_model（全串去重；textContent 渲染，
  // 特殊字符天然安全）
  function judgeModelCandidateValues() {
    const projection = judgeSupply.projection || {};
    const values = [];
    [projection.default_provider_model, projection.env_baseline_model]
      .forEach((model) => {
        const value = String(model || '');
        if (value && !values.includes(value)) values.push(value);
      });
    judgeSupply.usedModels.forEach((model) => {
      const value = String(model || '');
      if (value && !values.includes(value)) values.push(value);
    });
    return values;
  }

  // Provider 行操作列：编辑 + 删除（"设为评估默认"入口移至编辑 modal 勾选项）。
  // is_active 行删除禁用（Agent 启用中的 Provider 不可删，后端 ProviderInUse
  // 仍为权威）；评估默认但非 active 的行允许删除。
  function judgeProviderActions(provider, body) {
    const actions = el('div', 'row-actions');
    actions.appendChild(actionButton('编辑',
      () => openEvaluationProviderModal(provider, body)));
    if (provider.is_active) {
      actions.appendChild(disabledButton('删除'));
    } else {
      actions.appendChild(actionButton('删除', async () => {
        const requestToken = renderToken;
        if (!(await modal.confirm('确认删除 Provider「' + (provider.name || '') + '」？'
            + '删除后评估默认引用将回落 env 基线。'))) return;
        // 切页后（renderToken 失效）才返回的确认不再执行删除
        if (requestToken !== renderToken) return;
        try {
          await api.deleteProvider(provider.id);
          alertMessage('Provider 已删除');
          if (requestToken === renderToken) await renderJudgeModel(body, requestToken);
        } catch (err) {
          const code = err && err.message ? err.message : '';
          if (code === 'conflict') {
            // 409：Provider 状态已被他人修改（如评估默认引用变化），删除未生效
            alertMessage('删除冲突（409）：Provider 状态已被他人修改，删除未生效；'
              + '请刷新页面核对后重试。');
          } else {
            // 兜底错误码不上屏（无操作指引价值），console 留诊断
            console.error('evaluation provider delete failed:', code);
            alertMessage('删除失败：操作失败，请稍后重试或刷新页面');
          }
        }
      }));
    }
    return actions;
  }

  // 关闭即清空密钥输入（节点随 modal 移除，绝不写浏览器存储）
  function closeEvaluationProviderModal(frame, apiKeyInput) {
    apiKeyInput.value = '';
    closeBackdrop(frame.backdrop);
  }

  // Provider 编辑/新增 modal（页内独立 DOM id，eval-provider-* 前缀；
  // 结构对齐主模型页 providers-modal：字段名称置于控件上方，同构
  // providers-form label 布局，不复用 policy-item 左侧标签）。密钥脱敏规则
  // 同主页面：留空=不变（请求省略 api_key）、空格=清空（api_key=""）、
  // 新增必填；"设为评估默认"勾选项在 Provider 保存成功后按需走 evaluations
  // config CAS（openai-compatible 专用，anthropic 禁用并提示）；CRUD 失败
  // 保留表单草稿。
  function openEvaluationProviderModal(provider, body) {
    const isEdit = !!provider;
    const requestToken = renderToken;
    const initialDefaultId = String(
      (judgeSupply.projection || {}).default_provider_id || '');
    const wasDefault = isEdit && String(provider.id) === initialDefaultId;
    const frame = buildFrame(isEdit
      ? '编辑 Provider：' + (provider.name || '') : '新增 Provider');
    const form = frame.form;
    form.appendChild(el('div', 'evaluations-note',
      'Provider 配置与 Agent 共用，修改会影响共用该 Provider 的调用。'));
    const nameInput = document.createElement('input');
    nameInput.id = 'eval-provider-name';
    nameInput.value = isEdit ? String(provider.name || '') : '';
    const typeSelect = selectControl('eval-provider-type',
      [['openai-compatible', 'openai-compatible'], ['anthropic', 'anthropic']],
      isEdit ? String(provider.provider_type || 'openai-compatible')
        : 'openai-compatible');
    typeSelect.id = 'eval-provider-type';
    const baseUrlInput = document.createElement('input');
    baseUrlInput.id = 'eval-provider-base-url';
    baseUrlInput.placeholder = 'http://...';
    baseUrlInput.value = isEdit ? String(provider.base_url || '') : '';
    const modelInput = document.createElement('input');
    modelInput.id = 'eval-provider-model';
    modelInput.value = isEdit ? String(provider.model || '') : '';
    const apiKeyInput = document.createElement('input');
    apiKeyInput.id = 'eval-provider-api-key';
    apiKeyInput.type = 'password';
    apiKeyInput.value = '';
    apiKeyInput.placeholder = isEdit ? '留空保持不变；输入空格清空' : 'API Key（必填）';
    const visionCheckbox = document.createElement('input');
    visionCheckbox.id = 'eval-provider-vision';
    visionCheckbox.type = 'checkbox';
    visionCheckbox.checked = isEdit ? !!provider.supports_vision : true;
    // 字段名称置于控件上方（对齐主模型页 providers-form 的 label 布局）
    const fieldTop = (text, control) => {
      const label = document.createElement('label');
      label.appendChild(document.createTextNode(text));
      label.appendChild(control);
      form.appendChild(label);
    };
    fieldTop('名称', nameInput);
    fieldTop('类型', typeSelect);
    fieldTop('Base URL', baseUrlInput);
    fieldTop('Model', modelInput);
    fieldTop('API Key', apiKeyInput);
    const visionLabel = el('label', 'providers-form__checkbox');
    visionLabel.appendChild(visionCheckbox);
    visionLabel.appendChild(document.createTextNode(' 支持 Vision（图片输入）'));
    form.appendChild(visionLabel);
    // "设为评估默认"勾选项：默认行勾选态跟随当前投影；openai-compatible
    // 专用，类型切换为 anthropic 时禁用并清空，附原因提示
    const defaultCheckbox = document.createElement('input');
    defaultCheckbox.id = 'eval-provider-default';
    defaultCheckbox.type = 'checkbox';
    defaultCheckbox.checked = wasDefault;
    const defaultLabel = el('label', 'providers-form__checkbox');
    defaultLabel.appendChild(defaultCheckbox);
    defaultLabel.appendChild(document.createTextNode(' 设为评估默认'));
    form.appendChild(defaultLabel);
    const defaultHint = el('div', 'providers-form__hint muted');
    form.appendChild(defaultHint);
    const syncDefaultAvailability = () => {
      const supported = String(typeSelect.value || '') === 'openai-compatible';
      defaultCheckbox.disabled = !supported;
      if (!supported) defaultCheckbox.checked = false;
      defaultHint.textContent = supported ? '' : 'anthropic 协议不支持作为评估默认';
    };
    typeSelect.addEventListener('change', syncDefaultAvailability);
    syncDefaultAvailability();
    form.appendChild(el('div', 'providers-form__hint muted', isEdit
      ? 'API Key 状态: '
        + (provider.api_key_present ? '已配置（脱敏不可见）' : '未配置')
        + '。留空=不变；输入空格=清空。'
      : '新增时 API Key 必填。'));
    const errorLine = el('div', 'error-state');
    errorLine.hidden = true;
    form.appendChild(errorLine);
    // 动作区按 formActions 同构手搭：取消路径同样走
    // closeEvaluationProviderModal，与 × / 成功路径一致清空密钥输入
    const actions = el('div', 'providers-form__actions');
    actions.appendChild(actionButton('取消',
      () => closeEvaluationProviderModal(frame, apiKeyInput)));
    const submitBtn = el('button', 'btn btn--primary', '保存');
    submitBtn.type = 'submit';
    actions.appendChild(submitBtn);
    form.appendChild(actions);
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click',
      () => closeEvaluationProviderModal(frame, apiKeyInput));
    // backdrop 空白点击关闭与 × 同路径：清空密钥输入后再关闭
    //（showBackdrop 自带的空白关闭只做 closeBackdrop，不清密钥）
    frame.backdrop.addEventListener('click', (event) => {
      if (event.target === frame.backdrop) {
        closeEvaluationProviderModal(frame, apiKeyInput);
      }
    });

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (submitBtn.disabled) return; // pending 防重复提交
      const name = nameInput.value.trim();
      const baseUrl = baseUrlInput.value.trim();
      const model = modelInput.value.trim();
      const rawKey = apiKeyInput.value;
      if (!name || !baseUrl || !model) {
        errorLine.textContent = '名称、Base URL 与 Model 必填';
        errorLine.hidden = false;
        return;
      }
      if (!isEdit && rawKey.trim() === '') {
        errorLine.textContent = '新增 Provider 时 API Key 必填';
        errorLine.hidden = false;
        return;
      }
      const payload = {
        name: name,
        provider_type: String(typeSelect.value || ''),
        base_url: baseUrl,
        model: model,
        supports_vision: !!visionCheckbox.checked,
      };
      if (isEdit) {
        if (rawKey === '') {
          // 留空 = 不变：请求省略 api_key
        } else if (rawKey.trim() === '') {
          payload.api_key = ''; // 纯空白 = 清空
        } else {
          payload.api_key = rawKey;
        }
      } else {
        payload.api_key = rawKey;
      }
      // 勾选意图在 await 前捕获，避免请求期间表单状态漂移
      const wantDefault = defaultCheckbox.checked;
      errorLine.hidden = true;
      submitBtn.disabled = true;
      let savedId = isEdit ? String(provider.id) : '';
      try {
        if (isEdit) await api.updateProvider(provider.id, payload);
        else {
          const created = await api.createProvider(payload);
          savedId = String((created && created.id) || '');
        }
      } catch (err) {
        // 弹窗已关闭：迟到失败不回写（竞态隔离）
        if (openBackdrop !== frame.backdrop) return;
        const code = err && err.message ? err.message : '';
        if (code === 'conflict') {
          // 409：Provider 已被他人修改，本次保存未生效；提示显式刷新
          errorLine.textContent = '保存冲突（409）：Provider 已被他人修改，本次未生效；'
            + '请刷新页面核对后重试，不会自动重放。';
        } else {
          // 兜底错误码不上屏（无操作指引价值），console 留诊断
          console.error('evaluation provider save failed:', code);
          errorLine.textContent = '保存失败：操作失败，请稍后重试或刷新页面';
        }
        errorLine.hidden = false;
        submitBtn.disabled = false;
        return;
      }
      // Provider 已保存；评估默认勾选变化按需走 config CAS（失败不影响
      // 已保存的 Provider，也不自动重放）
      let defaultFailure = '';
      if (wantDefault !== wasDefault) {
        if (!savedId) {
          defaultFailure = 'Provider 已保存，但设置评估默认失败：未取得 Provider 标识';
        } else {
          try {
            const resp = await api.evaluations.patchConfig({
              expected_version: judgeSupply.configVersion,
              patch: { evaluation_judge_provider_id: wantDefault ? savedId : '' },
            });
            if (resp && typeof resp.version === 'number') {
              judgeSupply.configVersion = resp.version;
            }
          } catch (err) {
            const code = err && err.message ? err.message : '';
            console.error('evaluation judge default failed:', code);
            defaultFailure = 'Provider 已保存，但设置评估默认'
              + (code === 'conflict'
                ? '冲突（409）：评估配置已被他人修改，请刷新页面核对后重试'
                : '失败：操作失败，请稍后重试或刷新页面');
          }
        }
      }
      closeEvaluationProviderModal(frame, apiKeyInput);
      // 成功路径不弹提示 modal：modal 关闭 + 列表/默认列刷新即是反馈，
      // 仅默认设置失败时提示
      if (defaultFailure) alertMessage(defaultFailure);
      // Provider CRUD/默认变更完成：重拉列表与供给投影（版本未变也刷新）
      if (requestToken === renderToken) await renderJudgeModel(body, requestToken);
    });
  }

  // Sector 渲染：Provider 列表表格 + 评估供给精简区（全部 textContent/value
  // 渲染，不拼接 HTML；empty-state 复用共享样式类）。表格列与主模型页
  // Provider 列表同构（名称/类型/Base URL/Model/Key/Vision/操作），"启用"
  // 列替换为"默认"（badge 徽章：是/否，当前评估默认，渲染效果与主模型页
  // "启用"列一致）；不包滚动层，宽度与主模型页一致。
  function renderJudgeSupplySectors(providerBody, supplyBody, body) {
    const projection = judgeSupply.projection || {};
    const defaultId = String(projection.default_provider_id || '');
    providerBody.textContent = '';
    // Provider 列表统一共享翻页（组件 2，slice 模式）：内存切片不重新取数，
    // 空列表也显示 pagerBar（第 1/1 页、按钮禁用），保持布局一致。
    const drawProviderList = () => {
      const sliced = slicePage(judgeSupply.providers, judgeSupplyProviderPageIndex);
      if (!judgeSupply.providers.length) {
        providerBody.appendChild(el('div', 'empty-state',
          '暂无 Provider。点击「新增」创建，或先在左导「模型」页配置 Agent Provider。'));
      } else {
        providerBody.appendChild(table(
          ['名称', '类型', 'Base URL', 'Model', 'Key', 'Vision', '默认', '操作'],
          sliced.pageItems.map((provider) => [
            String(provider.name || '-'),
            provider.provider_type || '-',
            provider.base_url || '-',
            provider.model || '-',
            provider.api_key_present ? '已配置' : '未配置',
            provider.supports_vision ? '✓' : '-',
            // 默认列渲染效果与主模型页"启用"列一致：badge 徽章（是=success，否=warning）
            String(provider.id) === defaultId
              ? el('span', 'badge badge--success', '是')
              : el('span', 'badge badge--warning', '否'),
            judgeProviderActions(provider, body),
          ]),
        ));
      }
      providerBody.appendChild(pagerBar({
        pageIndex: judgeSupplyProviderPageIndex,
        hasPrev: judgeSupplyProviderPageIndex > 0,
        hasNext: judgeSupplyProviderPageIndex + 1 < sliced.totalPages,
        totalPages: sliced.totalPages,
        onPrev: () => {
          judgeSupplyProviderPageIndex -= 1;
          providerBody.textContent = '';
          drawProviderList();
        },
        onNext: () => {
          judgeSupplyProviderPageIndex += 1;
          providerBody.textContent = '';
          drawProviderList();
        },
      }));
    };
    drawProviderList();
    supplyBody.textContent = '';
    const supplyGrid = el('div', 'policy-cfg');
    supplyGrid.appendChild(kvRow('Judge 就绪状态',
      projection.judge_ready ? '已就绪' : '未就绪'));
    supplyGrid.appendChild(kvRow('供给来源', projection.supply_source === 'provider'
      ? '默认 Provider' : (projection.supply_source === 'env' ? 'env 基线' : '')));
    supplyGrid.appendChild(kvRow('有效模型', projection.effective_model));
    supplyGrid.appendChild(kvRow('env 基线模型', projection.env_baseline_model));
    supplyGrid.appendChild(kvRow('Provider 地址', projection.provider_base_url_host));
    supplyBody.appendChild(supplyGrid);
    if (!defaultId) {
      supplyBody.appendChild(el('div', 'evaluations-note',
        '未设置默认 Provider，回落 env 基线。'));
    }
    if (projection.default_provider_missing) {
      supplyBody.appendChild(el('div', 'error-state',
        '警示：默认 Provider 记录已不存在（可能已被删除），当前供给回落 env 基线；'
        + '请重新设置默认或取消默认。'));
    }
    supplyBody.appendChild(el('div', 'evaluations-note',
      'env 基线为 B 类 env-only，Dashboard 不可改；Provider 列表与 Agent 共用，'
      + '评估默认仅为引用关系。'));
    if (!projection.judge_ready) {
      supplyBody.appendChild(el('div', 'error-state',
        'Judge 未就绪：无完整供给（默认 Provider 缺失/不完整且 env 基线未配置），'
        + '本次评估将返回未配置类错误。'));
    }
  }

  async function renderJudgeModel(body, token) {
    body.textContent = '';
    const seq = ++viewRequestSeq.judgeModel;
    const wrap = el('div', 'evaluators-view');
    body.appendChild(wrap);
    if (body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.textContent = '';
    }
    // Sector 1：Provider 列表（新增入口挂 Sector header 右上角）；
    // Sector 2：评估供给精简区（加载失败时保留骨架）
    const providerPanel = sectionPanel('Provider 列表', {
      actions: [actionButton('新增', () => openEvaluationProviderModal(null, body))],
    });
    wrap.appendChild(providerPanel.panel);
    const providerBody = providerPanel.body;
    showLoading(providerBody, 'Provider 列表加载中...');
    const supplyPanel = sectionPanel('评估供给');
    wrap.appendChild(supplyPanel.panel);
    const supplyBody = supplyPanel.body;
    showLoading(supplyBody, '评估供给加载中...');

    let providers;
    let projection;
    try {
      providers = await api.listProviders();
      projection = await api.evaluations.getJudgeModel();
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.judgeModel) return;
      // 错误码不上屏（无操作指引价值），console 留诊断
      console.error('evaluation provider list failed:',
        err && err.message ? err.message : '');
      renderErrorState(providerBody, 'Provider 列表加载失败：操作失败，请稍后重试或刷新页面',
        () => renderJudgeModel(body, renderToken));
      // 供给区保留加载骨架：重试成功后随视图整体恢复
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.judgeModel) return;

    judgeSupply.providers = Array.isArray(providers) ? providers : [];
    judgeSupply.projection = projection || null;
    // 列表快照刷新即回第 1 页（首次进入/刷新/CRUD 成功都经此路径）
    judgeSupplyProviderPageIndex = 0;
    if (projection && typeof projection.config_version === 'number') {
      judgeSupply.configVersion = projection.config_version;
    }
    renderJudgeSupplySectors(providerBody, supplyBody, body);
  }

  // ------------------------------------------------------------------
  // 人工标注视图（Task 12/spec 4.4）：标注队列（状态/原因两个原生 select
  // 同一筛选行，全部/待标注/已标注 + 低分/争议/异常，低分阈值固定 0.5）
  // + 详情（请求/回答证据并排 + 评分契约 + 原自动值/
  // 当前人工值/修订号/证据状态）+ 受约束提交（request_key 重放 +
  // expected_revision CAS，409 显式重载不自动覆盖）+ 审核后导出集成
  // ------------------------------------------------------------------

  // 标注队列筛选状态（跨渲染保留用户选择，与 reviewReason 同模式）
  let annotationState = 'all';
  let annotationReason = 'low_score';
  // 低分阈值不再暴露用户配置，固定 0.5（与待复核队列默认值一致）
  const ANNOTATION_LOW_SCORE_THRESHOLD = '0.5';

  const ANNOTATION_STATE_LABELS = {
    all: '全部',
    pending: '待标注',
    annotated: '已标注',
  };

  const ANNOTATION_REJECT_REASONS = {
    target_kind_not_supported: '暂不支持在此标注（仅 automatic + agent_run 来源结果可提交人工评分）',
    target_not_found: '标注目标核验失败（真实 Run 不存在），请刷新后重试',
    target_revision_mismatch: '目标 revision 与真实 Run 不匹配（原结果已过期），请显式重载',
    evidence_unavailable: '证据不可用（缺失/过期/脱敏后不足），禁止提交新标注，不补造正文',
  };

  // request_key：同 payload 重试复用同 key、修改内容换新 key
  //（spec 4.4）；vm/旧浏览器无 randomUUID 时退化为时间戳+随机串
  function newRequestKey() {
    try {
      if (typeof crypto !== 'undefined' && crypto && typeof crypto.randomUUID === 'function') {
        return 'annotation-' + crypto.randomUUID();
      }
    } catch (_) { /* fall through */ }
    return 'annotation-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  }

  async function renderAnnotations(body, token) {
    body.textContent = '';
    // Sector Header 操作区：导出按钮在加载/错误/空队列时禁用占位（不禁用隐藏，
    // 保持操作区结构稳定），成功且当前页有数据时才可点；导出范围为当前页
    // 切片，与表格/pager 展示严格一致，不跨页收集。
    const mountAnnotationHeader = (canExport) => {
      const actions = body.evaluationsHeaderActions;
      if (!actions) return;
      actions.textContent = '';
      if (canExport) {
        actions.appendChild(actionButton('审核后导出数据集',
          () => openExportModal(slicePage(annotationPageItems, annotationPageIndex).pageItems)));
      } else {
        actions.appendChild(disabledButton('审核后导出数据集'));
      }
      actions.appendChild(actionButton('查看数据集列表',
        () => init({ route: { scope: 'datasets' } })));
    };
    mountAnnotationHeader(false);
    const seq = ++viewRequestSeq.annotations;
    const wrap = el('div', 'evaluations-annotations');
    body.appendChild(wrap);

    // 筛选区：标注状态/标注原因两个原生 select 同一 filterBar 一行排布
    // （共享 .select-control 外观，窄屏按 filterBar 合同 flex-wrap）。
    // 状态组 all 项对应后端 all 枚举；原因枚举无 all（reason=all 会 422），
    // 原因下拉不提供「全部」项，始终一项选中。低分阈值固定 0.5，不再暴露配置。
    const filterRow = filterBar();
    const stateSelect = selectControl('annotation_state',
      Object.keys(ANNOTATION_STATE_LABELS)
        .map(state => [state, ANNOTATION_STATE_LABELS[state]]),
      annotationState);
    stateSelect.addEventListener('change', () => {
      annotationState = stateSelect.value;
      resetAnnotationPaging();
      renderAnnotations(body, renderToken);
    });
    const reasonSelect = selectControl('annotation_reason',
      REVIEW_REASONS.map(reason => [reason, REVIEW_REASON_LABELS[reason]]),
      annotationReason);
    reasonSelect.addEventListener('change', () => {
      annotationReason = reasonSelect.value;
      resetAnnotationPaging();
      renderAnnotations(body, renderToken);
    });
    filterRow.append(
      filterControl('标注状态', stateSelect),
      filterControl('标注原因', reasonSelect),
    );
    wrap.appendChild(filterRow);

    const listBody = el('div', 'evaluations-annotations__list');
    wrap.appendChild(listBody);
    showLoading(listBody, '标注队列加载中...');
    // 新请求发出即清除旧成功页导出闭包，失败/迟到响应不得恢复
    annotationPageItems = [];
    const params = {
      state: annotationState,
      reason: annotationReason,
      threshold: ANNOTATION_LOW_SCORE_THRESHOLD,
      limit: LIST_PAGE_SIZE,
    };
    const annotationCursor = annotationCursorStack[annotationPageIndex];
    if (annotationCursor) params.cursor = annotationCursor;
    let data;
    try {
      data = await api.evaluations.listAnnotations(params);
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.annotations) return;
      const code = err && err.message ? err.message : '';
      if (annotationCursor && code === 'cursor_invalid') {
        listBody.textContent = '';
        listBody.appendChild(el('div', 'error-state',
          '游标已失效（过滤条件或查询快照不匹配），请重新查询'));
        listBody.appendChild(actionButton('重新查询', () => {
          resetAnnotationPaging();
          renderAnnotations(body, renderToken);
        }));
        return;
      }
      renderErrorState(listBody, '标注队列加载失败：' + code,
        () => renderAnnotations(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.annotations) return;
    listBody.textContent = '';
    const items = Array.isArray(data.items) ? data.items : [];
    const annotationNextCursor = data.next_cursor ? String(data.next_cursor) : '';
    // pager 先于空态构建：第 2+ 页空响应仍保留上一页出口
    const annotationPager = pagerBar({
      pageIndex: annotationPageIndex,
      totalPages: data.total_pages,
      hasPrev: annotationPageIndex > 0,
      hasNext: !!annotationNextCursor,
      onPrev: () => {
        annotationPageIndex -= 1;
        return renderAnnotations(body, renderToken);
      },
      onNext: () => {
        annotationCursorStack = annotationCursorStack.slice(0, annotationPageIndex + 1);
        annotationCursorStack.push(annotationNextCursor);
        annotationPageIndex += 1;
        return renderAnnotations(body, renderToken);
      },
    });
    if (!items.length) {
      // 空态保留前缀，并展示当前状态/原因；阈值固定 0.5 不再作为用户手段提示
      const stateLabel = ANNOTATION_STATE_LABELS[annotationState] || annotationState;
      const reasonLabel = REVIEW_REASON_LABELS[annotationReason] || annotationReason;
      const emptyText = '当前筛选暂无待标注结果。当前标注状态「' + stateLabel + '」、原因「'
        + reasonLabel + '」下无匹配，可切换状态/原因，或等待符合条件的结果。';
      if (annotationPageIndex > 0) {
        listBody.appendChild(el('div', 'empty-state', '当前分页无结果，请返回上一页'));
      } else {
        listBody.appendChild(el('div', 'empty-state', emptyText));
      }
      listBody.appendChild(annotationPager);
      return;
    }
    annotationPageItems = items;
    // 当前页有数据后启用 header 导出（入参为当前页切片，最多 20 条）
    mountAnnotationHeader(items.length > 0);
    // 分列：原自动值 / 当前人工值 / 修订号（spec 4.4 已标注口径）
    listBody.appendChild(scrollTable(table(
      ['结果ID', '指标', '目标', '原自动值', '当前人工值',
        { label: '修订号', numeric: true, tip: '人工反馈修订号，0 为首次标注；409 后需显式重载' },
        '时间', '操作'],
      items.map((item) => {
        const human = item.latest_human_feedback || {};
        return [
          item.source_result_id,
          item.metric_name,
          item.target_service + '/' + item.target_kind + '/' + item.target_subject_id,
          item.typed_value === undefined || item.typed_value === null ? '-' : String(item.typed_value),
          human.value === undefined || human.value === null ? '-' : String(human.value),
          human.revision === undefined || human.revision === null ? null : human.revision,
          formatTime(item.created_at),
          actionButton('详情', () => openAnnotationDetail(item, body)),
        ];
      }),
    )));
    listBody.appendChild(annotationPager);
  }

  async function openAnnotationDetail(item, viewBody) {
    // Mi-#21：迟到响应防护——离开评估页后返回的上下文不得再打开弹窗
    const requestToken = renderToken;
    let context;
    try {
      context = await api.evaluations.getAnnotation(item.source_result_id);
    } catch (err) {
      alertMessage('标注上下文加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    if (requestToken !== renderToken) return;
    const definition = (context && context.definition) || {};
    const human = (context && context.latest_human_feedback) || null;
    const target = (context && context.target) || {};
    const frame = buildFrame('人工标注 ' + item.source_result_id);
    const form = frame.form;

    // 评分契约 + 分列状态（原自动值/当前人工值/修订号/证据状态）
    const contract = el('div', 'policy-list');
    contract.appendChild(kvRow('指标', definition.metric_name));
    contract.appendChild(kvRow('评估器', definition.definition_id + ' / ' + definition.version));
    contract.appendChild(kvRow('输出类型', definition.output_type));
    const categories = Array.isArray(definition.range_or_categories)
      ? definition.range_or_categories : [];
    if (categories.length) {
      contract.appendChild(kvRow('合法取值', categories.join(', ')));
    }
    contract.appendChild(kvRow('评分契约 rubric', definition.rubric_summary));
    contract.appendChild(kvRow('目标', [
      target.service, target.kind, target.subject_id, target.subject_revision,
    ].map((part) => (part === undefined || part === null || part === '' ? '-' : part)).join(' / ')));
    contract.appendChild(kvRow('原自动值',
      item.typed_value === undefined || item.typed_value === null ? '-' : String(item.typed_value)));
    contract.appendChild(kvRow('当前人工值',
      human && human.value !== undefined && human.value !== null ? String(human.value) : '未标注'));
    contract.appendChild(kvRow('修订号', context.current_revision));
    contract.appendChild(kvRow('人工反馈时间', human ? formatTime(human.created_at) : '-'));
    contract.appendChild(kvRow('证据状态',
      context.evidence_status === 'available' ? '可用' : '不可用'));
    form.appendChild(contract);

    // 请求/回答证据并排展示（spec 6.2 脱敏证据）；上下文未携带正文时
    // 仅展示状态口径，不补造正文
    form.appendChild(blockTitle('证据（请求 / 回答）'));
    const evidence = context.evidence;
    if (evidence && typeof evidence === 'object') {
      const requestBox = el('div', 'evaluations-export-case');
      requestBox.appendChild(el('div', 'evaluations-export-case__meta', '请求'));
      const requestPre = el('pre', 'evaluations-dataset-content');
      requestPre.textContent = evidence.request === undefined || evidence.request === null
        ? '-' : JSON.stringify(evidence.request, null, 2);
      requestBox.appendChild(requestPre);
      const answerBox = el('div', 'evaluations-export-case');
      answerBox.appendChild(el('div', 'evaluations-export-case__meta', '回答'));
      const answerPre = el('pre', 'evaluations-dataset-content');
      answerPre.textContent = evidence.answer === undefined || evidence.answer === null
        ? '-' : JSON.stringify(evidence.answer, null, 2);
      answerBox.appendChild(answerPre);
      form.append(requestBox, answerBox);
    } else if (context.evidence_status === 'available') {
      form.appendChild(el('div', 'evaluations-note',
        '证据状态：可用（快照正文经脱敏存取，未随上下文返回，此处不展开原文）。'));
    } else {
      form.appendChild(el('div', 'evaluations-note',
        '证据状态：不可用（缺失/过期/脱敏后不足）——不补造正文。'));
    }

    // 拒绝原因（不支持类型/证据过期等，提交按钮禁用）
    const rejectReason = context.reject_reason
      && ANNOTATION_REJECT_REASONS[context.reject_reason];
    if (rejectReason) {
      form.appendChild(el('div', 'evaluations-hint', rejectReason));
    }

    // 评分值输入（沿用原定义输出类型；无自由文本评论输入——本期不引入
    // 评论存储，编辑表单只提交受契约约束的分值）
    const valueKind = definition.output_type === 'number' ? 'number'
      : definition.output_type === 'boolean' ? 'boolean'
        : categories.length ? 'categorical' : 'text';
    let valueControl = null;
    if (context.can_submit) {
      const valueRow = el('div', 'policy-item');
      valueRow.appendChild(el('span', 'policy-k', '人工评分值：'));
      if (valueKind === 'boolean') {
        valueControl = selectControl('annotation_value',
          [['true', 'true'], ['false', 'false']], 'true');
      } else if (valueKind === 'categorical') {
        valueControl = selectControl('annotation_value',
          categories.map((category) => [String(category), String(category)]),
          String(categories[0]));
      } else {
        valueControl = document.createElement('input');
        valueControl.name = 'annotation_value';
        valueControl.placeholder = valueKind === 'number' ? '数值评分' : '评分值';
      }
      valueRow.appendChild(valueControl);
      form.appendChild(valueRow);
    }

    const errorLine = el('div', 'error-state');
    errorLine.hidden = true;
    form.appendChild(errorLine);
    const actions = formActions(form, '关闭', '提交标注');
    const submitBtn = actions.querySelector('button[type="submit"]');
    if (!context.can_submit) submitBtn.disabled = true;

    // request_key 状态按弹窗会话维护：同 payload 重试复用，修改内容换新
    const closeBtnHandler = () => closeBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', closeBtnHandler);
    let requestKey = null;
    let lastValueJson = null;

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (!context.can_submit) return;
      let value;
      if (valueKind === 'number') {
        const raw = String(valueControl.value || '').trim();
        const parsed = Number.parseFloat(raw);
        if (raw === '' || Number.isNaN(parsed)) {
          errorLine.textContent = '评分值必须是有效数字';
          errorLine.hidden = false;
          return;
        }
        value = parsed;
      } else if (valueKind === 'boolean') {
        value = valueControl.value === 'true';
      } else {
        value = valueControl.value;
      }
      const valueJson = JSON.stringify(value);
      // spec 4.4：同 payload 重复提交复用同 request_key，修改内容生成新 key
      if (!requestKey || valueJson !== lastValueJson) {
        requestKey = newRequestKey();
        lastValueJson = valueJson;
      }
      errorLine.hidden = true;
      submitBtn.disabled = true;
      try {
        const result = await api.evaluations.submitAnnotation(item.source_result_id, {
          value: value,
          request_key: requestKey,
          // 首次提交 expected_revision 取上下文 current_revision；409 后
          // 不自动递增/重试，由用户显式重载后再提交
          expected_revision:
            typeof context.current_revision === 'number' ? context.current_revision : 0,
        });
        closeBackdrop(frame.backdrop);
        alertMessage('标注已保存（revision=' + result.revision + '）'
          + (result.is_current === false ? '；该修订已非线程最新，请重载查看 latest 人评' : ''));
        // 保存成功：保留筛选并刷新当前条目（重查队列，模块级筛选不变）
        if (requestToken === renderToken && isActive() && viewBody) {
          await renderAnnotations(viewBody, renderToken);
        }
      } catch (err) {
        const code = err && err.message ? err.message : '';
        if (code === 'conflict') {
          // spec 4.4：409 必须提示重新加载，不能自动覆盖或静默重试
          errorLine.textContent = '提交冲突（409）：该标注已被他人修订。输入已保留，'
            + '不会自动覆盖；请关闭本窗口后重新打开详情以显式重载最新修订号，再重试。';
        } else if (code === 'evidence_unavailable') {
          errorLine.textContent = '证据不可用（缺失/过期/脱敏后不足），禁止提交，不补造正文。';
        } else if (code === 'target_kind_not_supported') {
          errorLine.textContent = '暂不支持在此标注（仅 automatic + agent_run 来源结果可提交）。';
        } else {
          errorLine.textContent = '提交失败：' + code;
        }
        errorLine.hidden = false;
      } finally {
        submitBtn.disabled = false;
      }
    });
    mountDetailModal(frame);
  }

  // ------------------------------------------------------------------
  // 生命周期
  // ------------------------------------------------------------------

  // 评分明细族共享状态口径 hover tips：results 与会话/运行评分专用视图
  // 同文案（专用视图定义在 results 之前，提取常量避免重复字面量）
  const resultsShellTip = '质量不通过/通过来自 boolean 或阈值判定\n'
    + '评估失败/跳过为执行态\n'
    + '投递等待与未关联在详情中展示\n'
    + 'unlinked 为目标无真实身份，不创建悬空 Score';

  const views = {
    // bareShell：概览页不套"概览"标题外壳面板（与顶导标签重复的多余
    // 一层），内容直达；其余视图维持标题外壳。
    overview: { title: '概览', render: renderOverview, bareShell: true },
    datasets: { title: '数据集', render: renderDatasets },
    capabilities: { title: '能力目录', render: renderCapabilities },
    evaluators: { title: '评估器', render: renderEvaluators },
    'judge-model': { title: '模型', render: renderJudgeModel },
    executions: { title: '执行任务', render: renderExecutions },
    experiments: { title: '实验汇总', render: renderExperiments },
    'experiment-results': { title: '评分明细', render: renderResults },
    'session-scores': { title: '会话评分', render: renderResults, shellTip: resultsShellTip },
    // run-scores 切换 run 粒度独立端点（plan-261124 T7）：不再走 renderResults
    // 锁定分支；LOCKED_RESULTS_TARGET_KIND 保留 'run-scores' 条目仅作锁定
    // 口径占位，供 resultFiltersByView 初始化播种筛选对象（renderRunScores
    // 随后剥离其中锁定字段），视图身份/空态不读取该映射
    'run-scores': { title: '运行评分', render: renderRunScores, shellTip: resultsShellTip },
    results: { title: '评分明细', render: renderResults, shellTip: resultsShellTip },
    annotations: { title: '人工标注', render: renderAnnotations },
    review: { title: '待复核', render: renderReviewQueue },
  };

  async function render(token, view) {
    const node = root();
    if (!node) return;
    node.textContent = '';
    // bareShell 视图（概览）：无标题外壳面板，状态探测/渲染共用裸挂载体；
    // 其余视图维持 sectionPanel(title) 外壳并经其 header 挂主操作。
    let shellBody;
    if (views[view].bareShell) {
      shellBody = document.createElement('div');
      node.appendChild(shellBody);
    } else {
      // shellTip：视图标题 hover tips（分事项换行，见 views.results）
      const section = sectionPanel(views[view].title,
        views[view].shellTip ? { tip: views[view].shellTip } : {});
      node.appendChild(section.panel);
      shellBody = section.body;
    }
    showLoading(shellBody, '评估状态加载中...');
    let status;
    try {
      status = await api.evaluations.status();
    } catch (err) {
      if (token !== renderToken) return;
      shellBody.textContent = '';
      renderErrorState(shellBody, '评估状态加载失败：' + (err.message || ''), () => refresh());
      return;
    }
    if (token !== renderToken) return;
    shellBody.textContent = '';
    if (!status.enabled) {
      shellBody.appendChild(el('div', 'empty-state',
        '评估功能未启用。请由管理员配置 N_AGENT_EVALUATION_ENABLED=true 并重启服务。'));
      return;
    }
    // 入口路径（首次进入/离开后重进/显式 refresh）统一回第 1 页；
    // 视图内翻页与筛选回调直接调 renderXxx，绕过本重置。
    resetViewPaging(view);
    await views[view].render(shellBody, token);
  }

  async function init(state) {
    const requestedView = state && state.route && state.route.scope;
    const view = views[requestedView] ? requestedView : (currentView || 'overview');
    if (inflight && view === currentView) return inflight;
    if (view !== currentView) deactivate();
    if (resultFiltersByView[view]) {
      resultsFilters = resultFiltersByView[view];
      // 锁定视图重进时强制回写固定类型，防止外部改写筛选状态；
      // run-scores 已切换 run 粒度独立端点（plan-261124 T7）：端点固定
      // 类型，target_kind 不进入请求参数，持久筛选对象保持无锁定字段
      const lockedKind = LOCKED_RESULTS_TARGET_KIND[view];
      if (lockedKind && view !== 'run-scores') resultsFilters.target_kind = lockedKind;
      if (view !== currentView && view === 'experiment-results' && global.location && global.URLSearchParams) {
        const query = new global.URLSearchParams(global.location.search || '');
        if (query.has('experiment_id')) resultsFilters.experiment_id = query.get('experiment_id') || '';
      }
    }
    currentView = view;
    const token = ++renderToken;
    inflight = render(token, view).finally(() => {
      if (token === renderToken) inflight = null;
    });
    return inflight;
  }

  async function refresh(state) {
    return init(state);
  }

  function deactivate() {
    if (executions) executions.deactivate();
    renderToken++;
    inflight = null;
    floatingTips.length = 0;
    if (openBackdrop) {
      openBackdrop.remove();
      openBackdrop = null;
    }
  }

  const executions = namespace.evaluationExecutions ? namespace.evaluationExecutions.createController({
    api: api.evaluations, el, actionButton, disabledButton, buildFrame, formActions, mountDetailModal, closeBackdrop,
    filterItem, kvRow, table, scrollTable, formatTime, blockTitle,
    pagerBar, LIST_PAGE_SIZE,
    isCurrent: token => token === renderToken && isActive(), openExperimentModal,
  }) : null;
  namespace.evaluations = { init: init, refresh: refresh, activate: init, deactivate: deactivate };
  global.NAGENT = namespace;
}(window));
