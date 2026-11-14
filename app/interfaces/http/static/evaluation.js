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

  // Async guards: monotonic render token discards late responses; the
  // in-flight promise de-duplicates concurrent init/refresh calls.
  let renderToken = 0;
  let inflight = null;
  let currentView = null;
  // 同路由请求序号：筛选/重试会在同一 renderToken 内直接重渲染，
  // 仅靠 token 无法识别同路由旧响应；每视图递增，成功/失败分支双校验，
  // 防止旧响应覆盖新筛选、错误态或 Loading。deactivate 不重置，token 兜底。
  const viewRequestSeq = {
    overview: 0, datasets: 0, evaluators: 0, experiments: 0,
    results: 0, review: 0, annotations: 0,
  };
  // 当前待复核筛选状态（reason + threshold；跨渲染保留用户选择）
  let reviewReason = 'low_score';
  let reviewThreshold = '0.5';
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
    row.appendChild(el('span', 'policy-v', value === undefined || value === null || value === '' ? '-' : value));
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

  // 置灰但不隐藏的不可用操作：无 click handler，harness findButton 不可见。
  function disabledButton(label) {
    const btn = el('button', 'btn');
    btn.type = 'button';
    btn.textContent = label;
    btn.disabled = true;
    return btn;
  }

  // 筛选区：filterBar 弹性容器 + filterItem（Label+Control+HelpText）。
  function filterBar() { return el('div', 'evaluations-filter'); }

  function filterItem(labelText, control, helpText) {
    const item = el('label', 'evaluations-filter__item');
    item.appendChild(el('span', 'evaluations-filter__label', labelText));
    item.appendChild(control);
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
    // 主动作入面板 header：重渲染先清空再挂，避免重复按钮
    if (body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.textContent = '';
      body.evaluationsHeaderActions.appendChild(
        actionButton('修订配置', () => openConfigModal(resolved, () => renderOverview(body, renderToken))));
    }
    const cfg = resolved.config || {};
    wrap.appendChild(kvRow('评估开关', cfg.evaluation_enabled ? '启用' : '停用'));
    wrap.appendChild(kvRow('采样率', cfg.evaluation_sampling_rate));
    wrap.appendChild(kvRow('Worker 并发', cfg.evaluation_worker_concurrency));
    wrap.appendChild(kvRow('配置版本', resolved.version));
    wrap.appendChild(kvRow('最近更新', formatTime(resolved.updated_at)));

    // M3a：容量积压与日预算用量
    const capacity = overview.capacity || {};
    if (!capacity.probe_available) {
      wrap.appendChild(kvRow('容量', '容量探针不可用'));
    } else {
      wrap.appendChild(kvRow(
        '待处理作业（积压）',
        capacity.pending_jobs + ' / ' + capacity.max_pending_jobs,
      ));
      wrap.appendChild(kvRow(
        '证据正文占用',
        capacity.evidence_bytes + ' / ' + capacity.max_evidence_bytes + ' 字节',
      ));
      const pendingRatio = capacity.max_pending_jobs
        ? capacity.pending_jobs / capacity.max_pending_jobs : 0;
      if (pendingRatio >= 0.8) {
        // N1：长句提示不用状态徽章（徽章只承载封闭短状态）
        wrap.appendChild(el('div', 'evaluations-hint',
          '积压接近容量上限，请检查 worker'));
      }
    }
    const budget = overview.budget || {};
    const ceiling = budget.daily_call_ceiling;
    wrap.appendChild(kvRow(
      '当日 Judge 调用（预算）',
      (budget.settled_calls || 0) + ' 结算 / ' + (budget.reserved_calls || 0)
        + ' 预留' + (ceiling ? (' / 上限 ' + ceiling) : ' / 未设上限'),
    ));

    // M3e：观测后端证据保留边界（7 天到期清理，评分值不受影响）
    wrap.appendChild(kvRow(
      '证据快照保留',
      (overview.evidence_retention_days || 7) + ' 天（到期清理快照正文；'
        + '评分值与 Langfuse 历史投影不受影响）',
    ));

    // M3b：低覆盖提示（实验尚无任何 current 结果）
    const counts = overview.experiment_results || {};
    const lowCoverage = (experiments.items || [])
      .filter((item) => !counts[item.experiment_id])
      .map((item) => item.experiment_id);
    if (lowCoverage.length) {
      const hint = el('div', 'evaluations-hint',
        '低覆盖提示：以下实验尚无评估结果（0 条）—— ' + lowCoverage.join(', '));
      wrap.appendChild(hint);
    }

    // 评分趋势卡（Task 13/spec 4.5）：按 (DefinitionKey, 版本, provenance)
    // 分组展示样本数/时间窗/分布；时间窗 UTC 展示 UTC+8；覆盖率 unknown
    // 须解释"缺少应评估目标总数"，不宣称采样覆盖率
    wrap.appendChild(blockTitle('评分趋势',
      '覆盖率 unknown 表示缺少应评估目标总数；趋势按 UTC+8 自然日分桶。'));
    const trend = analytics || {};
    const trendWindow = trend.window || {};
    wrap.appendChild(kvRow('统计时间窗',
      formatTime(trendWindow.from) + ' ~ ' + formatTime(trendWindow.to)));
    wrap.appendChild(kvRow('统计快照', formatTime(trend.snapshot_at)));
    const trendCoverage = trend.coverage || {};
    wrap.appendChild(el('div', 'evaluations-note',
      '覆盖率口径：' + (trendCoverage.status || 'unknown')
        + '（' + (trendCoverage.reason || '缺少应评估目标总数') + '）；'
        + '趋势按 UTC+8 自然日分桶。'));
    const trendGroups = Array.isArray(trend.groups) ? trend.groups : [];
    if (!trendGroups.length) {
      // 空态三要素：前缀事实 + 原因/下一步 + 筛选影响说明
      wrap.appendChild(el('div', 'empty-state',
        '暂无有效评分。当前统计时间窗内评估作业尚未产出有效值'
          + '（结果可能仍在排队，或全部为异常/跳过）；请等待评估完成或检查 worker 作业状态。'
          + '本概览为全局汇总，不受查询筛选条件影响。'));
    } else {
      wrap.appendChild(scrollTable(table(
        ['指标', '版本', '来源', '输出类型', { label: '样本数', numeric: true }, '有效值统计', '异常/跳过'],
        trendGroups.map((group) => [
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

    // producer 心跳（M3a）
    const states = overview.producer_states || [];
    if (states.length) {
      wrap.appendChild(blockTitle('Producer 心跳'));
      wrap.appendChild(scrollTable(table(
        ['Producer',
          { label: 'Epoch', numeric: true },
          { label: '累计捕获', numeric: true },
          { label: '捕获错误', numeric: true },
          { label: '队列满', numeric: true },
          '观测时间'],
        states.map((s) => [
          s.producer_id,
          s.counter_epoch,
          s.captured_count,
          s.capture_error_count,
          s.queue_full_count,
          formatTime(s.observed_at),
        ]),
      )));
    }
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
    const errorLine = el('div', 'error-state');
    errorLine.hidden = true;
    form.appendChild(errorLine);
    formActions(form, '取消', '保存');
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
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
    });
  }

  function normalizeBool(value) {
    if (value === undefined) return undefined;
    if (value === 'true') return true;
    if (value === 'false') return false;
    return value;
  }

  // ------------------------------------------------------------------
  // 实验列表与详情
  // ------------------------------------------------------------------

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
      '展示已导入及待复核审核后导出的数据集。版本不可覆盖，修改内容请导入新版本。'));
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '数据集加载中...');
    try {
      const data = await api.evaluations.listDatasets();
      if (token !== renderToken || seq !== viewRequestSeq.datasets) return;
      dataBody.textContent = '';
      const items = data.items || [];
      if (!items.length) {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无数据集。当前尚未导入或导出任何数据集；可通过「导入标注 JSON」创建，'
            + '或在待复核页面审核后导出。此列表为全量展示，不受筛选条件影响。'));
        return;
      }
      dataBody.appendChild(scrollTable(table(['数据集 ID', '版本', '目标', { label: '用例数', numeric: true }, '校验', '操作'], items.map(item => [
        item.dataset_id, item.dataset_version, item.target_kind || '-',
        item.case_count,
        item.status === 'valid' ? '通过' : '校验失败',
        actionButton('详情', () => openDatasetDetail(item.dataset_id, item.dataset_version)),
      ]))));
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

  async function renderExperiments(body, token) {
    body.textContent = '';
    if (executions && body.evaluationsHeaderActions) {
      body.evaluationsHeaderActions.textContent = '';
      body.evaluationsHeaderActions.appendChild(actionButton('发起实验', () => executions.openCreate(null, token)));
    }
    const seq = ++viewRequestSeq.experiments;
    const wrap = el('div', 'evaluations-experiments');
    body.appendChild(wrap);
    if (executions) {
      const executionList = el('div', 'evaluations-block');
      wrap.appendChild(executionList);
      executions.renderList(executionList, token);
    }
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
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      dataBody.appendChild(el('div', 'empty-state',
        '暂无实验。实验由 execute/replay 评估作业创建；可通过「发起实验」启动数据集测试，也可通过 CLI evaluate 发起。'
          + '当前列表未设置筛选条件。'));
      return;
    }
    const rows = items.map((item) => {
      const detailBtn = actionButton('详情', () => openExperimentModal(item.experiment_id));
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

  // 结果筛选与游标分页状态（Task 13/spec 4.5）：跨渲染保留用户选择；
  // 游标 v2 是不透明 token，上一页用历史游标栈回退，过滤变更重置栈
  let resultsFilters = {
    created_from: '',
    created_to: '',
    target_kind: '',
    definition_id: '',
    provenance: '',
    status: '',
  };
  let resultsCursorStack = [''];
  let resultsPageIndex = 0;

  function resetResultsPaging() {
    resultsCursorStack = [''];
    resultsPageIndex = 0;
  }

  function resultsQueryParams() {
    const params = { limit: 50 };
    const cursor = resultsCursorStack[resultsPageIndex];
    if (cursor) params.cursor = cursor;
    Object.keys(resultsFilters).forEach((key) => {
      if (resultsFilters[key]) params[key] = resultsFilters[key];
    });
    return params;
  }

  async function renderResults(body, token) {
    body.textContent = '';
    const seq = ++viewRequestSeq.results;
    const wrap = el('div', 'evaluations-results');
    body.appendChild(wrap);

    // 筛选区（spec 4.5：时间范围/目标类型/DefinitionKey/provenance/状态；
    // 变更任一过滤重置游标栈）。filterBar + 6 个 filterItem，name 合同不变。
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
    const kindSelect = selectControl('results_target_kind', [
      ['', '全部目标'],
      ['agent_run', 'agent_run'],
      ['retrieval', 'retrieval'],
    ], resultsFilters.target_kind);
    bindResultsFilter(kindSelect, 'target_kind', false);
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
      filterItem('开始时间', createdFromInput, 'ISO 8601 UTC，如 2026-11-05T00:00:00Z'),
      filterItem('结束时间', createdToInput, 'ISO 8601 UTC，不含该时刻'),
      filterItem('目标类型', kindSelect),
      filterItem('DefinitionKey', defIdInput, '指标键，如 kb.recall_at_5'),
      filterItem('来源', provenanceSelect),
      filterItem('状态', statusSelect),
    );
    wrap.appendChild(filterRow);
    // 数据容器独立于筛选区：Loading/错误/空态/重试只清 dataBody，
    // 不删除筛选控件与当前查询条件。
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '评估结果加载中...');

    const params = resultsQueryParams();
    const hadCursor = !!params.cursor;
    let data;
    try {
      data = await api.evaluations.listResults(params);
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.results) return;
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
    if (token !== renderToken || seq !== viewRequestSeq.results) return;
    dataBody.textContent = '';
    const items = Array.isArray(data.items) ? data.items : [];
    // 游标分页（spec 4.5）：上一页走历史游标栈回退，下一页携带响应
    // next_cursor 入栈。pager 对齐项目标准（observations-pager）：右对齐、
    // 页码 info、按钮始终占位、边界态禁用而非隐藏；pager 先于空态构建，
    // 第 2+ 页返回空 items 时仍保留"上一页"入口，避免用户被卡死在空页
    const pagerRow = el('div', 'evaluations-pager');
    const pageInfo = el('span', 'evaluations-pager__info');
    pageInfo.textContent = '第 ' + (resultsPageIndex + 1) + ' 页';
    pagerRow.appendChild(pageInfo);
    if (resultsPageIndex > 0) {
      pagerRow.appendChild(actionButton('上一页', () => {
        resultsPageIndex -= 1;
        renderResults(body, renderToken);
      }));
    } else {
      pagerRow.appendChild(disabledButton('上一页'));
    }
    if (data.next_cursor) {
      const nextCursor = String(data.next_cursor);
      pagerRow.appendChild(actionButton('下一页', () => {
        resultsCursorStack = resultsCursorStack.slice(0, resultsPageIndex + 1);
        resultsCursorStack.push(nextCursor);
        resultsPageIndex += 1;
        renderResults(body, renderToken);
      }));
    } else {
      pagerRow.appendChild(disabledButton('下一页'));
    }
    if (!items.length) {
      // 空态三分支：空分页优先（保留上一页入口），其次按有无筛选给下一步
      if (resultsPageIndex > 0) {
        dataBody.appendChild(el('div', 'empty-state', '当前分页无结果，请返回上一页'));
      } else if (Object.values(resultsFilters).some(Boolean)) {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无评估结果，可调整或清空筛选条件后重试。'));
      } else {
        dataBody.appendChild(el('div', 'empty-state',
          '暂无评估结果，评估作业完成后结果将展示在此处。'));
      }
      dataBody.appendChild(pagerRow);
      return;
    }
    const rows = items.map((item) => [
      item.result_id,
      item.metric_name,
      item.provenance,
      statusBadge(resultState(item)),
      item.typed_value === undefined || item.typed_value === null ? '-' : String(item.typed_value),
      formatTime(item.created_at),
      actionButton('详情', () => openResultModal(item.result_id)),
    ]);
    dataBody.appendChild(scrollTable(table(
      ['结果ID', '指标', '来源',
        { label: '状态', tip: '质量不通过/通过来自判定；评估失败/跳过为执行态；数值表示无门禁判定' },
        '值', '时间', '操作'],
      rows,
    )));
    dataBody.appendChild(pagerRow);
    dataBody.appendChild(el('div', 'evaluations-note',
      '状态口径：质量不通过/通过来自 boolean 或阈值判定；评估失败/跳过为'
      + '执行态；投递等待与未关联在详情中展示（unlinked 为目标无真实身份，'
      + '不创建悬空 Score）。'));
  }

  async function openResultModal(resultId) {
    let detail;
    try {
      detail = await api.evaluations.getResult(resultId);
    } catch (err) {
      alertMessage('结果详情加载失败：' + (err && err.message ? err.message : ''));
      return;
    }
    const frame = buildFrame('结果详情 ' + resultId);
    const form = frame.form;
    const result = detail.result || {};
    const kv = el('div', 'policy-list');
    kv.appendChild(kvRow('指标', result.metric_name));
    kv.appendChild(kvRow('来源', result.provenance));
    kv.appendChild(kvRow('状态', statusBadge(resultState(result))));
    kv.appendChild(kvRow('值', result.typed_value === null || result.typed_value === undefined ? '-' : result.typed_value));
    kv.appendChild(kvRow('时间', formatTime(result.created_at)));
    const evidence = detail.evidence_summary || {};
    kv.appendChild(kvRow('证据快照 hash', evidence.snapshot_hash || '-'));
    kv.appendChild(kvRow('捕获时间', formatTime(evidence.captured_at)));
    form.appendChild(kv);

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
      const langfuseRow = document.createElement('div');
      if (LANGFUSE_URL) {
        langfuseRow.appendChild(externalLink(LANGFUSE_URL, '查看 Langfuse 会话'));
      }
      form.appendChild(langfuseRow);
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
    filterRow.appendChild(filterItem('低分阈值', thresholdInput, thresholdHelp));
    wrap.appendChild(filterRow);

    const listBody = el('div', 'evaluations-review__list');
    wrap.appendChild(listBody);
    showLoading(listBody, '待复核队列加载中...');
    const params = { reason: reviewReason, threshold: reviewThreshold, limit: 50 };
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
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      const emptyText = reviewReason === 'low_score'
        ? '当前筛选暂无待复核结果，当前低分原因及阈值下无匹配，可切换原因或调高低分阈值以扩大候选范围。'
        : '当前筛选暂无待复核结果，可切换其他原因或等待符合条件的结果。';
      listBody.appendChild(el('div', 'empty-state', emptyText));
      wrap.appendChild(annotationEntryLink());
      return;
    }
    const rows = items.map((item) => [
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

    // 审核后受控导出：金标由审核人逐 case 提供（禁止自动生成），导出经
    // T12 校验并记录审批来源
    wrap.appendChild(actionButton('审核后导出数据集', () => openExportModal(items, () => renderReviewQueue(body, renderToken))));
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
    select.name = name;
    return setOptions(select, options, current);
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
      renderEvaluators(body, renderToken);
    });
    methodSelect.addEventListener('change', () => {
      evaluatorFilterMethod = methodSelect.value;
      renderEvaluators(body, renderToken);
    });
    filterRow.append(
      filterItem('目标类型', kindSelect),
      filterItem('方法', methodSelect),
    );
    wrap.appendChild(filterRow);
    // 数据容器独立于筛选区：Loading/错误/重试只清 dataBody，筛选不被删除
    const dataBody = el('div', 'evaluations-data-body');
    wrap.appendChild(dataBody);
    showLoading(dataBody, '评估器列表加载中...');

    const params = { limit: 50 };
    if (evaluatorFilterTargetKind) params.target_kind = evaluatorFilterTargetKind;
    if (evaluatorFilterMethod) params.method = evaluatorFilterMethod;

    let data;
    let caps;
    try {
      [data, caps] = await Promise.all([
        api.evaluations.listEvaluators(params),
        api.evaluations.getCapabilities(),
      ]);
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.evaluators) return;
      renderErrorState(dataBody, '评估器列表加载失败：' + (err && err.message ? err.message : ''),
        () => renderEvaluators(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.evaluators) return;
    dataBody.textContent = '';

    // 能力目录（spec 4.2：仅目录内组合可注册自动评估器）
    const judgeConfigured = !!(caps && caps.judge_configured);
    const entries = (caps && Array.isArray(caps.entries)) ? caps.entries : [];
    if (entries.length) {
      dataBody.appendChild(blockTitle('能力目录（仅目录内组合可注册自动评估器）'));
      dataBody.appendChild(scrollTable(table(
        ['方法', '指标', '目标类型', '输出类型', '状态'],
        entries.map((entry) => [
          EVALUATOR_METHOD_LABELS[entry.method] || entry.method,
          entry.metric_name,
          entry.target_kind,
          entry.output_type,
          evaluatorStatusText(entry, judgeConfigured),
        ]),
      )));
    }

    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      const hasEvaluatorFilter = !!(evaluatorFilterTargetKind || evaluatorFilterMethod);
      dataBody.appendChild(el('div', 'empty-state', hasEvaluatorFilter
        ? '暂无评估器。当前目标类型/方法筛选下无匹配注册项，可调整筛选条件，或点击「新建评估器」注册版本。'
        : '暂无评估器。尚未注册任何评估器版本，可点击「新建评估器」新建；'
          + '自动评估器仅能注册能力目录内的指标组合，注册后默认关闭，需显式启用。'));
      return;
    }

    // spec 4.3：允许同指标多版本显式同时启用，页面提示会增加评估作业、
    // 结果不合并
    const enabledCountByMetric = {};
    items.forEach((item) => {
      if (item.method !== 'human' && item.usage && item.usage.online_enabled) {
        enabledCountByMetric[item.metric_name] =
          (enabledCountByMetric[item.metric_name] || 0) + 1;
      }
    });
    if (Object.keys(enabledCountByMetric)
      .some((metric) => enabledCountByMetric[metric] > 1)) {
      dataBody.appendChild(el('div', 'evaluations-hint',
        '同一指标存在多个已启用版本：会增加评估作业，结果不合并。'));
    }

    dataBody.appendChild(scrollTable(table(
      ['评估器ID', '版本', '指标', '目标类型', '方法',
        { label: '在线状态', tip: '注册版本默认关闭，启用后才参与线上评估；同指标多版本结果不合并' },
        '操作'],
      items.map((item) => {
        const usage = item.usage || {};
        const actions = document.createElement('div');
        if (item.method === 'human') {
          // spec 4.3：human 禁线上启用（服务端 422）。置灰不隐藏：
          // 无 click handler，同行常驻说明，不产生任何启停请求
          actions.appendChild(disabledButton('停用'));
          const humanNote = el('small', 'evaluations-filter__help muted',
            '人工评估器不支持自动使用启停');
          actions.appendChild(humanNote);
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
    kindSelect.name = 'target_kind';
    let metricControl = null;
    let outputTypeSelect = null;
    let judgeModelInput = null;
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
      outputTypeSelect.name = 'output_type';
      setOptions(outputTypeSelect, outputTypes.map((o) => [o, o]),
        source && source.method === method && outputTypes.includes(source.output_type)
          ? source.output_type : (outputTypes[0] || ''));
      fieldRow(dynamic, '输出类型', outputTypeSelect);
      if (method === 'llm_judge') {
        judgeModelInput = document.createElement('input');
        judgeModelInput.name = 'judge_model';
        judgeModelInput.value = source && source.method === 'llm_judge'
          ? String(source.judge_model || '') : '';
        fieldRow(dynamic, 'Judge 模型', judgeModelInput);
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
        rubricInput = null;
        temperatureInput = null;
      }
    }

    fieldRow(form, '评估器 ID', definitionIdInput);
    fieldRow(form, '版本', versionInput);
    fieldRow(form, '方法', methodSelect);
    form.appendChild(dynamic);
    methodSelect.addEventListener('change', () => rebuildDynamic());
    rebuildDynamic();
    form.appendChild(errorLine);
    const actions = formActions(form, '取消', '注册版本');
    const submit = actions.querySelector('button[type="submit"]');
    showBackdrop(frame.backdrop);
    frame.closeBtn.addEventListener('click', () => closeBackdrop(frame.backdrop));

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
        payload.judge_model = judgeModelInput ? judgeModelInput.value.trim() : '';
        // judge_model 为 llm_judge 必填项（服务端 422 同规则），缺失时不发请求
        if (!payload.judge_model) {
          errorLine.textContent = 'llm_judge 必须声明 Judge 模型（judge_model）';
          errorLine.hidden = false;
          return;
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
  // 人工标注视图（Task 12/spec 4.4）：标注队列（全部/待标注/已标注 +
  // 低分/争议/异常）+ 详情（请求/回答证据并排 + 评分契约 + 原自动值/
  // 当前人工值/修订号/证据状态）+ 受约束提交（request_key 重放 +
  // expected_revision CAS，409 显式重载不自动覆盖）+ 审核后导出集成
  // ------------------------------------------------------------------

  // 标注队列筛选状态（跨渲染保留用户选择，与 reviewReason 同模式）
  let annotationState = 'all';
  let annotationReason = 'low_score';
  let annotationThreshold = '0.5';

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
    const seq = ++viewRequestSeq.annotations;
    const wrap = el('div', 'evaluations-annotations');
    body.appendChild(wrap);

    // 标注状态筛选（全部/待标注/已标注），role=group + aria-pressed
    wrap.appendChild(focusableChoiceGroup('annotation-state', '标注状态',
      Object.keys(ANNOTATION_STATE_LABELS)
        .map(state => [state, ANNOTATION_STATE_LABELS[state]]),
      annotationState,
      (state) => {
        annotationState = state;
        renderAnnotations(body, renderToken);
      }));

    // 原因筛选（低分/争议/异常，与待复核队列同口径）
    wrap.appendChild(focusableChoiceGroup('annotation-reason', '标注原因',
      REVIEW_REASONS.map(reason => [reason, REVIEW_REASON_LABELS[reason]]),
      annotationReason,
      (reason) => {
        annotationReason = reason;
        renderAnnotations(body, renderToken);
      }));

    // 低分阈值（reason=low_score 时生效，与待复核队列同参数）
    const thresholdInput = document.createElement('input');
    thresholdInput.name = 'annotation_threshold';
    thresholdInput.value = annotationThreshold;
    thresholdInput.addEventListener('change', () => {
      annotationThreshold = thresholdInput.value.trim() || '0.5';
      renderAnnotations(body, renderToken);
    });
    const annotationThresholdHelp = annotationReason === 'low_score'
      ? '仅 reason=低分 时生效，默认 0.5'
      : '当前原因为「' + REVIEW_REASON_LABELS[annotationReason]
        + '」，低分阈值不生效；切换回低分原因时恢复作用';
    const filterRow = filterBar();
    filterRow.appendChild(filterItem('低分阈值', thresholdInput, annotationThresholdHelp));
    wrap.appendChild(filterRow);

    const listBody = el('div', 'evaluations-annotations__list');
    wrap.appendChild(listBody);
    showLoading(listBody, '标注队列加载中...');
    const params = {
      state: annotationState,
      reason: annotationReason,
      threshold: annotationThreshold,
      limit: 50,
    };
    let data;
    try {
      data = await api.evaluations.listAnnotations(params);
    } catch (err) {
      if (token !== renderToken || seq !== viewRequestSeq.annotations) return;
      renderErrorState(listBody, '标注队列加载失败：' + (err && err.message ? err.message : ''),
        () => renderAnnotations(body, renderToken));
      return;
    }
    if (token !== renderToken || seq !== viewRequestSeq.annotations) return;
    listBody.textContent = '';
    const items = Array.isArray(data.items) ? data.items : [];
    // T12 审查修复项 2：空队列对齐 renderReviewQueue 空态模式——early-
    // return，不渲染导出/数据集入口（空队列无条目可审核导出）
    if (!items.length) {
      // 空态保留前缀，并展示当前状态/原因；low_score 才提示阈值手段
      const stateLabel = ANNOTATION_STATE_LABELS[annotationState] || annotationState;
      const reasonLabel = REVIEW_REASON_LABELS[annotationReason] || annotationReason;
      const emptyText = annotationReason === 'low_score'
        ? '当前筛选暂无待标注结果。当前标注状态「' + stateLabel + '」、原因「' + reasonLabel
          + '」及阈值下无匹配，可切换状态/原因或调高低分阈值以扩大候选范围。'
        : '当前筛选暂无待标注结果。当前标注状态「' + stateLabel + '」、原因「' + reasonLabel
          + '」下无匹配，可切换状态/原因，或等待符合条件的结果。';
      listBody.appendChild(el('div', 'empty-state', emptyText));
      return;
    }
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

    // 审核导出集成（spec 4.4：逐 case 金标与审核来源沿用既有流程，
    // 不把自动评分/已提交人评直接当 expected_output/qrels）；导出完成
    // 可进入数据集列表（dataset_id/version/hash/audit_id 可关联）
    wrap.appendChild(actionButton('审核后导出数据集', () => openExportModal(items)));
    wrap.appendChild(actionButton('查看数据集列表', () => init({ route: { scope: 'datasets' } })));
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

  const views = {
    overview: { title: '概览', render: renderOverview },
    datasets: { title: '数据集', render: renderDatasets },
    evaluators: { title: '评估器', render: renderEvaluators },
    experiments: { title: '实验', render: renderExperiments },
    results: { title: '结果', render: renderResults },
    annotations: { title: '人工标注', render: renderAnnotations },
    review: { title: '待复核', render: renderReviewQueue },
  };

  async function render(token, view) {
    const node = root();
    if (!node) return;
    node.textContent = '';
    const section = sectionPanel(views[view].title);
    node.appendChild(section.panel);
    showLoading(section.body, '评估状态加载中...');
    let status;
    try {
      status = await api.evaluations.status();
    } catch (err) {
      if (token !== renderToken) return;
      section.body.textContent = '';
      renderErrorState(section.body, '评估状态加载失败：' + (err.message || ''), () => refresh());
      return;
    }
    if (token !== renderToken) return;
    section.body.textContent = '';
    if (!status.enabled) {
      section.body.appendChild(el('div', 'empty-state',
        '评估功能未启用。请由管理员配置 N_AGENT_EVALUATION_ENABLED=true 并重启服务。'));
      return;
    }
    await views[view].render(section.body, token);
  }

  async function init(state) {
    const requestedView = state && state.route && state.route.scope;
    const view = views[requestedView] ? requestedView : (currentView || 'overview');
    if (inflight && view === currentView) return inflight;
    if (view !== currentView) deactivate();
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
    api: api.evaluations, el, actionButton, buildFrame, formActions, mountDetailModal, closeBackdrop,
    filterItem, kvRow, table, scrollTable, formatTime, blockTitle,
    isCurrent: token => token === renderToken && isActive(), openExperimentModal,
  }) : null;
  namespace.evaluations = { init: init, refresh: refresh, activate: init, deactivate: deactivate };
  global.NAGENT = namespace;
}(window));
