(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});

  const PAGE_SIZE = 10;
  const PAGES_SHOWN = 5;

  function root() {
    return ui.byId ? ui.byId('tab-observations') : document.getElementById('tab-observations');
  }

  function formatNumber(value) {
    const n = Number(value || 0);
    if (!isFinite(n)) return '0';
    return n.toLocaleString();
  }

  function appendHeaderCell(row, label, className) {
    const th = ui.el('th', className || '');
    th.textContent = label;
    row.appendChild(th);
    return th;
  }

  function appendNumericHeaderCell(row, label) {
    return appendHeaderCell(row, label, 'document-table__numeric');
  }

  function appendNumericCell(row, value) {
    const td = ui.el('td', 'document-table__numeric');
    td.textContent = value;
    row.appendChild(td);
    return td;
  }

  function appendTextCell(row, value) {
    const td = ui.el('td');
    td.textContent = value;
    row.appendChild(td);
    return td;
  }

  function formatCost(value) {
    const n = Number(value || 0);
    if (!isFinite(n)) return '0';
    return '$' + n.toFixed(6);
  }

  function formatPercent(part, total) {
    if (!total || total <= 0) return '0%';
    const pct = (part / total) * 100;
    return pct.toFixed(1) + '%';
  }

  function formatCacheHitRate(item) {
    const read = Number((item && item.cache_read_tokens) || 0);
    const write = Number((item && item.cache_write_tokens) || 0);
    const input = Number((item && item.input_tokens) || 0);
    return formatPercent(read, input + read + write);
  }

  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    // East-8 (Asia/Shanghai) regardless of browser timezone
    const tz = new Date(d.getTime() + 8 * 3600 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${tz.getUTCFullYear()}-${pad(tz.getUTCMonth() + 1)}-${pad(tz.getUTCDate())} ${pad(tz.getUTCHours())}:${pad(tz.getUTCMinutes())}:${pad(tz.getUTCSeconds())}`;
  }

  function parseSessionIdFromPath() {
    const path = window.location.pathname || '';
    const prefix = '/chat/observations/';
    const idx = path.indexOf(prefix);
    if (idx < 0) return '';
    const rest = path.slice(idx + prefix.length);
    const trimmed = rest.split('/')[0].trim();
    return trimmed;
  }

  function buildPagination(current, total, pageSize) {
    const totalPages = Math.max(1, Math.ceil(total / pageSize));
    const pages = [];
    if (totalPages <= PAGES_SHOWN) {
      for (let i = 1; i <= totalPages; i++) pages.push(i);
    } else {
      let start = Math.max(1, current - Math.floor(PAGES_SHOWN / 2));
      let end = start + PAGES_SHOWN - 1;
      if (end > totalPages) {
        end = totalPages;
        start = Math.max(1, end - PAGES_SHOWN + 1);
      }
      for (let i = start; i <= end; i++) pages.push(i);
      if (start > 1) {
        pages.unshift('...');
        pages.unshift(1);
      }
      if (end < totalPages) {
        pages.push('...');
        pages.push(totalPages);
      }
    }
    return { totalPages, pages };
  }

  function buildPager(page, total, pageSize, onPageChange) {
    const { totalPages, pages } = buildPagination(page, total, pageSize);
    const pager = ui.el('div', 'observations-pager');
    pager.style.display = 'flex';
    pager.style.justifyContent = 'flex-end';
    pager.style.alignItems = 'center';
    pager.style.gap = '12px';
    pager.style.marginTop = '12px';
    const start = (page - 1) * pageSize + 1;
    const end = Math.min(page * pageSize, total);
    const info = ui.el('span', 'observations-pager__info');
    info.textContent = '第 ' + start + '-' + end + ' 条 / 共 ' + total + ' 条';
    pager.appendChild(info);

    const ctrl = ui.el('div', 'observations-pager__ctrl');
    ctrl.style.display = 'flex';
    ctrl.style.alignItems = 'center';
    ctrl.style.gap = '4px';
    const prevBtn = ui.el('button', 'btn');
    prevBtn.type = 'button';
    prevBtn.textContent = '<';
    prevBtn.setAttribute('aria-label', '上一页');
    prevBtn.disabled = page <= 1;
    if (prevBtn.disabled) prevBtn.classList.add('btn--disabled');
    prevBtn.addEventListener('click', () => onPageChange(page - 1));
    ctrl.appendChild(prevBtn);

    pages.forEach((p) => {
      if (p === '...') {
        const dots = ui.el('span', 'observations-pager__dots');
        dots.textContent = '...';
        ctrl.appendChild(dots);
      } else {
        const btn = ui.el('button', 'btn');
        btn.type = 'button';
        btn.textContent = String(p);
        if (p === page) btn.classList.add('btn--primary');
        btn.addEventListener('click', () => onPageChange(p));
        ctrl.appendChild(btn);
      }
    });

    const nextBtn = ui.el('button', 'btn');
    nextBtn.type = 'button';
    nextBtn.textContent = '>';
    nextBtn.setAttribute('aria-label', '下一页');
    nextBtn.disabled = page >= totalPages;
    if (nextBtn.disabled) nextBtn.classList.add('btn--disabled');
    nextBtn.addEventListener('click', () => onPageChange(page + 1));
    ctrl.appendChild(nextBtn);

    pager.appendChild(ctrl);
    return pager;
  }

  function renderOverviewCards(container, stats) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = '整体总览';
    header.appendChild(title);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    const bar = ui.el('div', 'stats-bar observations-stats-bar');
    const cards = [
      { label: '会话总数', value: formatNumber(stats.session_count) },
      { label: '输入 Token', value: formatNumber(stats.input_tokens) },
      { label: '输出 Token', value: formatNumber(stats.output_tokens) },
      { label: '缓存读', value: formatNumber(stats.cache_read_tokens) },
      { label: '缓存写', value: formatNumber(stats.cache_write_tokens) },
      { label: '缓存命中率', value: formatCacheHitRate(stats) },
      { label: '归一化 Token', value: formatNumber(stats.normalized_tokens) },
      { label: 'API 调用数', value: formatNumber(stats.api_call_count) },
    ];
    cards.forEach((c) => {
      const card = ui.el('div', 'stat-card');
      const value = ui.el('div', 'stat-card__value');
      value.textContent = c.value;
      const label = ui.el('div', 'stat-card__label');
      label.textContent = c.label;
      card.append(value, label);
      bar.appendChild(card);
    });
    body.appendChild(bar);

    panel.appendChild(body);
    container.appendChild(panel);
  }

  function renderSessionsTable(container, items, page, pageSize, total) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = '会话列表';
    header.appendChild(title);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');

    if (!items || !items.length) {
      ui.renderEmpty(body, '暂无会话');
      panel.appendChild(body);
      container.appendChild(panel);
      return;
    }

    const table = ui.el('table', 'document-table');
    const thead = ui.el('thead');
    const trh = ui.el('tr');
    ['标题', '来源', 'ID', '对话轮数', 'API 调用', '归一化 Token', '操作'].forEach((h) => {
      if (h === '对话轮数' || h === 'API 调用' || h === '归一化 Token') appendNumericHeaderCell(trh, h);
      else appendHeaderCell(trh, h);
    });
    thead.appendChild(trh);
    table.appendChild(thead);

    const tbody = ui.el('tbody');
    items.forEach((s) => {
      const tr = ui.el('tr');
      const titleTd = ui.el('td');
      titleTd.textContent = s.title || '(未命名)';
      const sourceTd = ui.el('td');
      sourceTd.textContent = s.source || '-';
      const idTd = ui.el('td');
      idTd.style.fontFamily = 'monospace';
      idTd.style.fontSize = '12px';
      idTd.textContent = s.session_id || '';
      tr.append(titleTd, sourceTd, idTd);
      appendNumericCell(tr, formatNumber(s.turn_count));
      const apiTd = appendNumericCell(tr, formatNumber(s.api_call_count));
      const normTd = appendNumericCell(tr, formatNumber(s.normalized_tokens));
      const actionTd = ui.el('td');
      actionTd.style.textAlign = 'center';
      const detailBtn = ui.el('button', 'btn btn--primary');
      detailBtn.type = 'button';
      detailBtn.textContent = '详情';
      detailBtn.addEventListener('click', () => {
        route.goToDetail(s.session_id);
      });
      actionTd.appendChild(detailBtn);
      tr.appendChild(actionTd);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    body.appendChild(table);

    // Pagination
    const pager = buildPager(page, total, pageSize, (p) => route.goToPage(p));
    body.appendChild(pager);

    panel.appendChild(body);
    container.appendChild(panel);
  }

  function renderDetailHeader(container, sessionId) {
    const wrap = ui.el('div', 'observations-detail-header');
    const back = ui.el('a', 'observations-detail-header__back');
    back.href = '/chat/observations';
    back.textContent = '返回';
    back.addEventListener('click', (ev) => {
      ev.preventDefault();
      route.goToIndex();
    });
    const sep = ui.el('span', 'observations-detail-header__sep');
    sep.textContent = '|';
    const idLabel = ui.el('span', 'observations-detail-header__id');
    idLabel.style.fontFamily = 'monospace';
    idLabel.style.fontSize = '12px';
    idLabel.textContent = sessionId || '';
    wrap.append(back, sep, idLabel);
    container.appendChild(wrap);
  }

  function renderStatsBar(container, stats) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = '会话总览';
    header.appendChild(title);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    const bar = ui.el('div', 'stats-bar observations-stats-bar');
    const cards = [
      { label: '输入 Token', value: formatNumber(stats.input_tokens) },
      { label: '输出 Token', value: formatNumber(stats.output_tokens) },
      { label: '缓存读', value: formatNumber(stats.cache_read_tokens) },
      { label: '缓存写', value: formatNumber(stats.cache_write_tokens) },
      { label: '缓存命中率', value: formatCacheHitRate(stats) },
      { label: '归一化 Token', value: formatNumber(stats.normalized_tokens) },
      { label: 'API 调用数', value: formatNumber(stats.api_call_count) },
    ];
    cards.forEach((c) => {
      const card = ui.el('div', 'stat-card');
      const value = ui.el('div', 'stat-card__value');
      value.textContent = c.value;
      const label = ui.el('div', 'stat-card__label');
      label.textContent = c.label;
      card.append(value, label);
      bar.appendChild(card);
    });
    body.appendChild(bar);
    panel.appendChild(body);
    container.appendChild(panel);
  }

  function renderBreakdown(container, breakdown) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = '上下文分布';
    header.appendChild(title);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    const total = breakdown.total || 0;
    const items = [
      { key: 'system_prompt', label: '系统提示' },
      { key: 'tool_definitions', label: '工具定义' },
      { key: 'memory', label: '外部记忆' },
      { key: 'conversation', label: '对话历史' },
    ];
    items.forEach((item) => {
      const value = Number(breakdown[item.key] || 0);
      const row = ui.el('div', 'breakdown-row');
      const head = ui.el('div', 'breakdown-row__head');
      const name = ui.el('span', 'breakdown-row__name');
      name.textContent = item.label;
      const stat = ui.el('span', 'breakdown-row__stat');
      stat.textContent = formatNumber(value) + ' / ' + formatPercent(value, total);
      head.append(name, stat);
      const track = ui.el('div', 'breakdown-row__track');
      const fill = ui.el('div', 'breakdown-row__fill');
      const pct = total > 0 ? (value / total) * 100 : 0;
      fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
      track.appendChild(fill);
      row.append(head, track);
      body.appendChild(row);
    });
    panel.appendChild(body);
    container.appendChild(panel);
  }

  function safeParseJson(value) {
    if (!value) return null;
    if (typeof value === 'object') return value;
    try { return JSON.parse(value); } catch (_) { return value; }
  }

  function buildKVSection(title, items) {
    const section = ui.el('div');
    section.style.marginTop = '16px';
    const t = ui.el('div');
    t.style.fontSize = '13px';
    t.style.fontWeight = '600';
    t.style.marginBottom = '6px';
    t.textContent = title;
    section.appendChild(t);
    const grid = ui.el('div');
    grid.style.display = 'flex';
    grid.style.flexWrap = 'wrap';
    grid.style.gap = '12px 20px';
    items.forEach(([k, v]) => {
      const item = ui.el('span');
      item.style.fontSize = '12px';
      item.style.color = 'var(--color-fg-muted, #606266)';
      const key = ui.el('span');
      key.textContent = k + ': ';
      const val = ui.el('span');
      val.style.color = 'var(--color-fg-default, #303133)';
      val.textContent = v;
      item.append(key, val);
      grid.appendChild(item);
    });
    section.appendChild(grid);
    return section;
  }

  const COPY_ICON_SVG = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M0 6.75C0 5.784.784 5 1.75 5h1.5a.75.75 0 0 1 0 1.5h-1.5a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-1.5a.75.75 0 0 1 1.5 0v1.5A1.75 1.75 0 0 1 9.25 16h-7.5A1.75 1.75 0 0 1 0 14.25Z"/><path d="M5 1.75C5 .784 5.784 0 6.75 0h7.5C15.216 0 16 .784 16 1.75v7.5A1.75 1.75 0 0 1 14.25 11h-7.5A1.75 1.75 0 0 1 5 9.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h7.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z"/></svg>';
  const CHECK_ICON_SVG = '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>';

  async function copyToClipboard(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (_) { /* fall through to legacy path */ }
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.top = '0';
      ta.style.left = '0';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return !!ok;
    } catch (_) { return false; }
  }

  function buildJsonSection(title, subtitle, jsonValue, emptyHint, options) {
    const opts = options || {};
    const section = ui.el('div', 'observations-json-section');
    const titleText = title + (subtitle ? ' · ' + subtitle : '');
    const t = opts.collapsible ? ui.el('button', 'observations-json-section__toggle') : ui.el('div', 'observations-json-section__title');
    if (opts.collapsible) {
      t.type = 'button';
      t.setAttribute('aria-expanded', opts.defaultCollapsed ? 'false' : 'true');
      const label = ui.el('span');
      label.textContent = titleText;
      const icon = ui.el('span', 'observations-json-section__icon');
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '›';
      t.append(label, icon);
    } else {
      t.textContent = titleText;
    }
    const wrap = ui.el('div', 'observations-json-pre-wrap');
    const pre = ui.el('pre', 'sandbox-detail-output');
    pre.style.maxHeight = '280px';
    const jsonText = jsonValue ? JSON.stringify(jsonValue, null, 2) : '';
    pre.textContent = jsonText || emptyHint;
    wrap.appendChild(pre);

    if (jsonValue) {
      const copyBtn = ui.el('button', 'observations-json-copy');
      copyBtn.type = 'button';
      copyBtn.setAttribute('aria-label', '复制 JSON');
      copyBtn.title = '复制';
      copyBtn.innerHTML = COPY_ICON_SVG;
      let resetTimer = null;
      copyBtn.addEventListener('click', async (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const ok = await copyToClipboard(jsonText);
        if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
        copyBtn.innerHTML = ok ? CHECK_ICON_SVG : COPY_ICON_SVG;
        copyBtn.classList.toggle('observations-json-copy--done', ok);
        copyBtn.title = ok ? '已复制' : '复制失败，请手动选择文本';
        resetTimer = setTimeout(() => {
          copyBtn.innerHTML = COPY_ICON_SVG;
          copyBtn.classList.remove('observations-json-copy--done');
          copyBtn.title = '复制';
          resetTimer = null;
        }, 1200);
      });
      wrap.appendChild(copyBtn);
    }

    if (opts.collapsible) {
      wrap.hidden = !!opts.defaultCollapsed;
      section.classList.toggle('observations-json-section--collapsed', !!opts.defaultCollapsed);
      t.addEventListener('click', () => {
        const collapsed = !wrap.hidden;
        wrap.hidden = collapsed;
        section.classList.toggle('observations-json-section--collapsed', collapsed);
        t.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      });
    }
    section.appendChild(t);
    section.appendChild(wrap);
    return section;
  }

  function openRecordModal(record) {
    const backdrop = ui.el('div', 'modal-backdrop');
    const dialog = ui.el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = ui.el('form', 'providers-form');
    const header = ui.el('div', 'modal-header');
    const titleEl = ui.el('h4');
    titleEl.textContent = 'API 调用详情 · ' + formatTime(record.created_at);
    const closeBtn = ui.el('button', 'modal-close');
    closeBtn.type = 'button';
    closeBtn.textContent = '×';
    closeBtn.setAttribute('aria-label', '关闭');
    header.append(titleEl, closeBtn);
    form.appendChild(header);

    const body = ui.el('div', 'modal-body');
    body.style.maxHeight = '70vh';
    body.style.overflowY = 'auto';

    // Sector 1: Token 用量与成本（合并）
    body.appendChild(buildKVSection('Token 用量与成本 (Usage & Cost)', [
      ['输入', formatNumber(record.input_tokens)],
      ['输出', formatNumber(record.output_tokens)],
      ['缓存读', formatNumber(record.cache_read_tokens)],
      ['缓存写', formatNumber(record.cache_write_tokens)],
      ['推理', formatNumber(record.reasoning_tokens)],
      ['归一化', formatNumber(record.normalized_tokens)],
      ['估算成本', formatCost(record.estimated_cost_usd)],
      ['状态', record.cost_status || '-'],
    ]));

    // Sector 2: 调用元信息（含生成参数，合并）
    const _genParams = safeParseJson(record.generation_params);
    const _genParamsText = (_genParams && typeof _genParams === 'object' && Object.keys(_genParams).length > 0)
      ? Object.entries(_genParams).map(([k, v]) => k + '=' + v).join(', ')
      : '(默认)';
    body.appendChild(buildKVSection('调用元信息 (Meta)', [
      ['记录ID', record.id != null ? '#' + record.id : '-'],
      ['模型', record.model || '-'],
      ['请求模型', record.requested_model || '-'],
      ['提供商', record.provider || '-'],
      ['调起类型', record.trigger_type || '-'],
      ['延迟', record.latency_ms != null ? record.latency_ms + 'ms' : '-'],
      ['生成参数', _genParamsText],
    ]));

    // Sector 3: 工具定义
    const toolsJson = safeParseJson(record.tools);
    const toolsCount = Array.isArray(toolsJson) ? toolsJson.length : 0;
    const toolsData = toolsJson && toolsCount > 0
      ? toolsJson.map((t) => {
          const fn = (t && t.function) || {};
          return { type: t.type, name: fn.name, description: fn.description, parameters: fn.parameters };
        })
      : null;
    body.appendChild(buildJsonSection(
      '工具定义 (Capability Context)',
      toolsJson ? toolsCount + ' 个' : '',
      toolsData,
      '(无记录或本次调用未启用工具)',
      { collapsible: true, defaultCollapsed: true }
    ));

    // Sector 4: 输入
    body.appendChild(buildJsonSection(
      '输入 (Request Messages)',
      '',
      safeParseJson(record.request_messages),
      '(无记录)'
    ));

    // Sector 5: 输出
    body.appendChild(buildJsonSection(
      '输出 (Response Message)',
      '',
      safeParseJson(record.response_message),
      '(无记录)'
    ));

    form.appendChild(body);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);

    function close() { backdrop.remove(); }
    closeBtn.addEventListener('click', close);
    backdrop.addEventListener('click', (ev) => { if (ev.target === backdrop) close(); });
  }

  function renderRecords(container, records) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = 'API 调用历史';
    header.appendChild(title);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    if (!records || !records.length) {
      ui.renderEmpty(body, '暂无 API 调用记录');
      panel.appendChild(body);
      container.appendChild(panel);
      return;
    }

    const total = records.length;
    function renderPage(page) {
      const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      page = Math.max(1, Math.min(page, totalPages));
      body.replaceChildren();
      const start = (page - 1) * PAGE_SIZE;
      const end = Math.min(start + PAGE_SIZE, total);
      const pageItems = records.slice(start, end);

      const table = ui.el('table', 'document-table');
      const thead = ui.el('thead');
      const trh = ui.el('tr');
      ['时间', '模型', '调起类型', '输入', '输出', '缓存读', '缓存写', '命中率', '归一化', '延迟(ms)', '操作'].forEach((h) => {
        if (['输入', '输出', '缓存读', '缓存写', '命中率', '归一化', '延迟(ms)'].includes(h)) appendNumericHeaderCell(trh, h);
        else appendHeaderCell(trh, h);
      });
      thead.appendChild(trh);
      table.appendChild(thead);

      const tbody = ui.el('tbody');
      pageItems.forEach((r) => {
        const tr = ui.el('tr');
        const timeTd = ui.el('td');
        timeTd.textContent = formatTime(r.created_at);
        tr.appendChild(timeTd);

        const modelTd = ui.el('td');
        modelTd.textContent = r.model || '-';
        if (r.requested_model && r.requested_model !== r.model) {
          modelTd.title = '请求模型: ' + r.requested_model;
          const hint = ui.el('span');
          hint.style.color = 'var(--color-fg-subtle, #909399)';
          hint.style.fontSize = '11px';
          hint.style.marginLeft = '4px';
          hint.textContent = '(' + r.requested_model + ')';
          modelTd.appendChild(hint);
        }
        tr.appendChild(modelTd);

        const triggerTd = ui.el('td');
        triggerTd.textContent = r.trigger_type || '-';
        tr.appendChild(triggerTd);

        [
          formatNumber(r.input_tokens),
          formatNumber(r.output_tokens),
          formatNumber(r.cache_read_tokens),
          formatNumber(r.cache_write_tokens),
          formatCacheHitRate(r),
          formatNumber(r.normalized_tokens),
          r.latency_ms != null ? formatNumber(r.latency_ms) : '-',
        ].forEach((c) => {
          appendNumericCell(tr, c);
        });

        const actionTd = ui.el('td');
        actionTd.style.textAlign = 'center';
        const detailBtn = ui.el('button', 'btn btn--primary');
        detailBtn.type = 'button';
        detailBtn.textContent = '详情';
        detailBtn.addEventListener('click', () => openRecordModal(r));
        actionTd.appendChild(detailBtn);
        tr.appendChild(actionTd);

        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      body.appendChild(table);

      const pager = buildPager(page, total, PAGE_SIZE, (p) => renderPage(p));
      body.appendChild(pager);
    }

    renderPage(1);
    panel.appendChild(body);
    container.appendChild(panel);
  }

  function renderCompressions(container, compressions) {
    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = '上下文压缩记录';
    header.appendChild(title);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    if (!compressions || !compressions.length) {
      ui.renderEmpty(body, '暂无压缩记录');
      panel.appendChild(body);
      container.appendChild(panel);
      return;
    }

    const sorted = compressions.slice().sort((a, b) => {
      const ta = a && a.created_at ? new Date(a.created_at).getTime() : 0;
      const tb = b && b.created_at ? new Date(b.created_at).getTime() : 0;
      return tb - ta;
    });
    const total = sorted.length;
    function renderPage(page) {
      const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      page = Math.max(1, Math.min(page, totalPages));
      body.replaceChildren();
      const start = (page - 1) * PAGE_SIZE;
      const end = Math.min(start + PAGE_SIZE, total);
      const pageItems = sorted.slice(start, end);

      const table = ui.el('table', 'document-table');
      const thead = ui.el('thead');
      const trh = ui.el('tr');
      ['时间', '压缩前', '压缩后', '节省', '压缩比'].forEach((h) => {
        if (['压缩前', '压缩后', '节省', '压缩比'].includes(h)) appendNumericHeaderCell(trh, h);
        else appendHeaderCell(trh, h);
      });
      thead.appendChild(trh);
      table.appendChild(thead);

      const tbody = ui.el('tbody');
      pageItems.forEach((c) => {
        const tr = ui.el('tr');
        const ratio = Number(c.compression_ratio || 0);
        const cells = [
          formatTime(c.created_at),
          formatNumber(c.before_tokens),
          formatNumber(c.after_tokens),
          formatNumber(c.tokens_saved),
          (ratio * 100).toFixed(1) + '%',
        ];
        cells.forEach((val, index) => {
          if (index === 0) appendTextCell(tr, val);
          else appendNumericCell(tr, val);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      body.appendChild(table);

      const pager = buildPager(page, total, PAGE_SIZE, (p) => renderPage(p));
      body.appendChild(pager);
    }

    renderPage(1);
    panel.appendChild(body);
    container.appendChild(panel);
  }

  async function renderIndex(page) {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    const loading = ui.el('div');
    ui.renderLoading(loading, '加载观测数据...');
    node.appendChild(loading);
    try {
      const [overview, sessionsResp] = await Promise.all([
        api.usage.getOverview(),
        api.usage.listSessions(page || 1, PAGE_SIZE),
      ]);
      const items = sessionsResp.items || [];
      const total = sessionsResp.total || 0;
      const current = sessionsResp.page || 1;
      node.replaceChildren();
      renderOverviewCards(node, overview || {});
      renderSessionsTable(node, items, current, PAGE_SIZE, total);
    } catch (err) {
      node.replaceChildren();
      ui.renderError(node, '加载观测数据失败: ' + (err && err.message ? err.message : err));
    }
  }

  async function renderDetail(sessionId) {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    const loading = ui.el('div');
    ui.renderLoading(loading, '加载会话观测数据...');
    node.appendChild(loading);
    try {
      const [stats, records, compressions, breakdown] = await Promise.all([
        api.usage.getStats(sessionId),
        api.usage.getRecords(sessionId, 50),
        api.usage.getCompressions(sessionId),
        api.usage.getBreakdown(sessionId),
      ]);
      node.replaceChildren();
      renderDetailHeader(node, sessionId);
      renderStatsBar(node, stats || {});
      renderBreakdown(node, breakdown || {});
      renderRecords(node, records || []);
      renderCompressions(node, compressions || []);
    } catch (err) {
      node.replaceChildren();
      renderDetailHeader(node, sessionId);
      ui.renderError(node, '加载会话观测数据失败: ' + (err && err.message ? err.message : err));
    }
  }

  const route = {
    async render() {
      const sessionId = parseSessionIdFromPath();
      if (sessionId) {
        await renderDetail(sessionId);
      } else {
        const page = parsePageFromQuery();
        await renderIndex(page);
      }
    },
    goToDetail(sessionId) {
      const url = '/chat/observations/' + encodeURIComponent(sessionId);
      history.pushState({ tab: 'observations' }, '', url);
      this.render();
    },
    goToIndex() {
      history.pushState({ tab: 'observations' }, '', '/chat/observations');
      this.render();
    },
    goToPage(page) {
      page = Math.max(1, page);
      const url = '/chat/observations?page=' + encodeURIComponent(page);
      history.pushState({ tab: 'observations', page }, '', url);
      this.render();
    },
  };

  function parsePageFromQuery() {
    const params = new URLSearchParams(window.location.search || '');
    const raw = params.get('page');
    const n = parseInt(raw, 10);
    return isNaN(n) || n < 1 ? 1 : n;
  }

  function init() {
    route.render();
    window.addEventListener('popstate', () => route.render());
  }

  function refresh() {
    route.render();
  }

  namespace.observations = { init, refresh, render: () => route.render() };
  global.NAGENT = namespace;
}(window));
