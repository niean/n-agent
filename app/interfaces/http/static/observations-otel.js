(function (global) {
  'use strict';
  // observations-otel scoped tab: OTel self-check read-only diagnostic page.
  // Renders the snapshot from GET /chat/observations/otel (a read-only,
  // no-store API backed by the composition-root-injected snapshot callable).
  // Rendering is textContent-only (no HTML injection channels); external
  // links are server-validated URLs (http/https, no userinfo) rendered as
  // anchors with target="_blank" and rel="noopener noreferrer".
  // Mount Point: <div class="tab-content" id="tab-observations-otel"></div>
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  const CONTAINER_ID = 'tab-observations-otel';
  const OTEL_API_URL = '/chat/observations/otel';
  const LINKS_CONFIG_API_URL = '/chat/observations/otel/links';
  const LINK_CONFIG_FIELDS = [
    ['metric_base_url', 'Metric'],
    ['log_base_url', 'Log'],
    ['trace_base_url', 'Trace'],
    ['profile_base_url', 'Profile'],
  ];

  // Async guards: monotonic render token discards late responses; the
  // in-flight promise de-duplicates concurrent init/refresh calls.
  let renderToken = 0;
  let inflight = null;
  // Modal guards: modalToken invalidates pending GETs on deactivate/close;
  // entryButton/configEntryDisabled track the 配置链接 entry across renders.
  let modalToken = 0;
  let entryButton = null;
  let configEntryDisabled = false;
  let openBackdrop = null;
  const MODAL_BTN_FONT_SIZE = 'var(--font-size-md)';

  function root() {
    return ui.byId ? ui.byId(CONTAINER_ID) : document.getElementById(CONTAINER_ID);
  }

  function isActive() {
    const node = root();
    return !!(node && node.classList && node.classList.contains('active'));
  }

  function isCurrent(token) {
    return token === renderToken && isActive();
  }

  function buildLoading() {
    const panel = ui.el('section', 'status-panel');
    const body = ui.el('div', 'panel-body');
    ui.renderLoading(body, '加载 OTel 自检数据...');
    panel.appendChild(body);
    return panel;
  }

  function buildError(message, onRetry) {
    const panel = ui.el('section', 'status-panel');
    const body = ui.el('div', 'panel-body');
    const err = ui.el('div', 'error-state');
    err.textContent = message || '加载失败';
    body.appendChild(err);
    if (typeof onRetry === 'function') {
      const retryBtn = ui.el('button', 'btn');
      retryBtn.type = 'button';
      retryBtn.textContent = '重试';
      retryBtn.addEventListener('click', onRetry);
      body.appendChild(retryBtn);
    }
    panel.appendChild(body);
    return panel;
  }

  // 统一时间格式化：服务端时间统一 UTC 存储，展示一律按 UTC+8（Asia/Shanghai）渲染，
  // 不依赖浏览器本地时区（与 chat.js / sandbox.js / host.js 等保持一致）。
  function formatTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (isNaN(d.getTime())) return String(value);
    // East-8 (Asia/Shanghai) regardless of browser timezone
    const tz = new Date(d.getTime() + 8 * 3600 * 1000);
    const pad = (n) => (n < 10 ? '0' + n : '' + n);
    return tz.getUTCFullYear() + '-' + pad(tz.getUTCMonth() + 1) + '-' + pad(tz.getUTCDate())
      + ' ' + pad(tz.getUTCHours()) + ':' + pad(tz.getUTCMinutes()) + ':' + pad(tz.getUTCSeconds());
  }

  function el(tag, className, text) {
    const node = ui.el(tag, className);
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // Status badge variant mapping, mirrors health.js badgeVariant.
  function badgeVariant(status) {
    const s = String(status || '');
    if (s === 'ok' || s === 'running' || s === 'enabled' || s === 'healthy') return 'success';
    if (s.indexOf('error') === 0 || s === 'failed' || s === 'unhealthy') return 'danger';
    return 'warning';
  }

  function statusBadge(status) {
    const badge = el('span', 'badge badge--' + badgeVariant(status), status);
    return badge;
  }

  // One label/value row, reusing the shared policy-item/policy-k/policy-v
  // pattern (same as host.js / security.js); both cells textContent-only.
  function kvRow(label, value) {
    const row = el('div', 'policy-item');
    row.appendChild(el('span', 'policy-k', label + '：'));
    row.appendChild(el('span', 'policy-v', value === undefined || value === null || value === '' ? '-' : value));
    return row;
  }

  function kvRowBadge(label, status) {
    const row = el('div', 'policy-item');
    row.appendChild(el('span', 'policy-k', label + '：'));
    if (status === undefined || status === null || status === '') {
      row.appendChild(el('span', 'policy-v', '-'));
    } else {
      row.appendChild(statusBadge(status));
    }
    return row;
  }

  // Shared document-table builder (same component as browser.js / knowledge.js).
  function table(headers, rows) {
    const t = el('table', 'document-table');
    const thead = document.createElement('thead');
    const htr = document.createElement('tr');
    headers.forEach(function (h) {
      const th = document.createElement('th');
      if (h.numeric) th.className = 'document-table__numeric';
      th.textContent = h.label;
      htr.appendChild(th);
    });
    thead.appendChild(htr);
    t.appendChild(thead);
    const tbody = document.createElement('tbody');
    rows.forEach(function (cells) {
      const tr = document.createElement('tr');
      cells.forEach(function (cell, i) {
        const td = document.createElement('td');
        if (headers[i] && headers[i].numeric) td.className = 'document-table__numeric';
        if (cell && cell.nodeType) td.appendChild(cell);
        else td.textContent = (cell === undefined || cell === null || cell === '') ? '-' : String(cell);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    return t;
  }

  function panel(title) {
    const section = el('section', 'status-panel');
    const header = el('div', 'panel-header');
    header.appendChild(el('span', null, title));
    const body = el('div', 'panel-body');
    section.appendChild(header);
    section.appendChild(body);
    return { section: section, body: body };
  }

  function renderStatus(data) {
    const p = panel('OTel 自检状态');
    const grid = el('div', 'policy-meta');
    grid.appendChild(kvRowBadge('状态', data.status));
    grid.appendChild(kvRow('模式', data.mode));
    grid.appendChild(kvRow('实例 ID', data.instance_id));
    grid.appendChild(kvRow('采样率', data.sample_ratio));
    grid.appendChild(kvRow('semconv 版本', data.semconv_version));
    grid.appendChild(kvRow('导出 endpoint', data.exporter_endpoint));
    if (data.dropped_after_deadline) {
      grid.appendChild(kvRow('关停截止后丢弃', data.dropped_after_deadline));
    }
    p.body.appendChild(grid);
    return p.section;
  }

  function renderSignals(data) {
    const p = panel('信号状态');
    const signals = data.signals || {};
    const names = Object.keys(signals);
    if (!names.length) {
      p.body.appendChild(el('div', 'muted empty-state', '无信号状态'));
      return p.section;
    }
    const rows = names.map(function (name) {
      const sig = signals[name] || {};
      return [
        el('span', 'otel-strong', name),
        sig.status ? statusBadge(sig.status) : '-',
        formatTime(sig.delivery),
        sig.failure_count,
        formatTime(sig.last_success),
        formatTime(sig.last_failure),
        sig.current_error,
      ];
    });
    p.body.appendChild(table([
      { label: '信号' },
      { label: '状态' },
      { label: '最近交付' },
      { label: '累计失败', numeric: true },
      { label: '最近成功' },
      { label: '最近失败' },
      { label: '当前故障' },
    ], rows));
    return p.section;
  }

  function renderInstruments(data) {
    const p = panel('Instrument 清单(metric)');
    const instruments = data.instruments || [];
    if (!instruments.length) {
      p.body.appendChild(el('div', 'muted empty-state', '无 instrument'));
      return p.section;
    }
    const rows = instruments.map(function (inst) {
      return [inst && inst.name, inst && inst.kind, inst && inst.unit];
    });
    p.body.appendChild(table([
      { label: '名称' },
      { label: '类型' },
      { label: '单位' },
    ], rows));
    return p.section;
  }

  function renderSpans(data) {
    const p = panel('最近执行片段(span)');
    const spans = data.spans || [];
    if (!spans.length) {
      p.body.appendChild(el('div', 'muted empty-state', '暂无已采样 span'));
      return p.section;
    }
    const pageSize = 10;
    function renderPage(page) {
      const totalPages = Math.ceil(spans.length / pageSize);
      page = Math.max(1, Math.min(page, totalPages));
      const rows = spans.slice((page - 1) * pageSize, page * pageSize).map(function (span) {
        const s = span || {};
        const duration = (s.duration_ms === undefined || s.duration_ms === null) ? '-' : s.duration_ms + 'ms';
        return [s.name, s.status ? statusBadge(s.status) : '-', duration];
      });
      p.body.replaceChildren();
      p.body.appendChild(table([
        { label: '名称' },
        { label: '状态' },
        { label: '耗时', numeric: true },
      ], rows));
      p.body.appendChild(namespace.observations.renderers.buildPager(
        page, spans.length, pageSize, renderPage
      ));
    }
    renderPage(1);
    return p.section;
  }

  function renderLinks(data) {
    const section = el('section', 'status-panel');
    const header = el('div', 'panel-header');
    header.appendChild(el('span', null, '外部链接'));
    // Entry sits on the right of the 13px panel header (inherits font size)
    // and renders even when the link list is empty.
    const configBtn = el('button', 'btn', '配置链接');
    configBtn.type = 'button';
    configBtn.disabled = configEntryDisabled;
    configBtn.addEventListener('click', function () { openConfigModal(); });
    entryButton = configBtn;
    header.appendChild(configBtn);
    const body = el('div', 'panel-body');
    section.appendChild(header);
    section.appendChild(body);
    const links = data.links || {};
    const names = Object.keys(links);
    if (!names.length) {
      body.appendChild(el('div', 'muted empty-state', '未配置外部链接'));
      return section;
    }
    const list = el('div', 'otel-links');
    names.forEach(function (name) {
      const href = links[name];
      if (typeof href !== 'string' || !href) return;
      const anchor = document.createElement('a');
      anchor.className = 'otel-link';
      anchor.setAttribute('href', href);
      anchor.setAttribute('target', '_blank');
      anchor.setAttribute('rel', 'noopener noreferrer');
      anchor.textContent = name;
      list.appendChild(anchor);
    });
    body.appendChild(list);
    return section;
  }

  function renderNotes(data) {
    const notes = data.notes || {};
    const keys = Object.keys(notes);
    if (!keys.length) return null;
    const p = panel('说明');
    keys.forEach(function (key) {
      const value = notes[key];
      if (typeof value !== 'string' || !value) return;
      p.body.appendChild(kvRow(key, value));
    });
    return p.section;
  }

  function renderSnapshot(data) {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    const sections = [
      renderStatus(data),
      renderLinks(data),
      renderSignals(data),
      renderInstruments(data),
      renderSpans(data),
      renderNotes(data),
    ];
    sections.forEach(function (section) {
      if (section) node.appendChild(section);
    });
  }

  async function load(token) {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    node.appendChild(buildLoading());
    let data;
    try {
      data = api && typeof api.fetchJson === 'function'
        ? await api.fetchJson(OTEL_API_URL)
        : await (async function () {
          const response = await fetch(OTEL_API_URL);
          if (!response.ok) throw new Error('request_failed');
          return response.json();
        })();
    } catch (err) {
      if (!isCurrent(token)) return;
      node.replaceChildren();
      node.appendChild(buildError('加载 OTel 自检数据失败: ' + (err && err.message ? err.message : err), function () { refresh(); }));
      return;
    }
    if (!isCurrent(token)) return;
    renderSnapshot(data || {});
  }

  // Shared load runner with in-flight de-duplication (same pattern as
  // tasks-observations.js): a forced load supersedes the pending one.
  function runLoad(force) {
    if (!force && inflight) return inflight;
    if (force) inflight = null;
    renderToken++;
    const token = renderToken;
    const p = (async function () {
      try { await load(token); }
      finally { if (inflight === p) inflight = null; }
    })();
    inflight = p;
    return p;
  }

  async function init() {
    // A fresh tab init re-enables the config entry after an earlier 404.
    configEntryDisabled = false;
    return runLoad(false);
  }

  async function refresh() {
    return runLoad(false);
  }

  // =========================================================================
  // Link config modal (T7)
  // =========================================================================

  function fieldOriginal(state, field) {
    const v = state.snapshot && state.snapshot.config ? state.snapshot.config[field] : '';
    return (v === undefined || v === null) ? '' : String(v);
  }

  function refreshFieldState(state, field) {
    const input = state.inputs[field];
    const overridden = !!state.overridden[field];
    const original = fieldOriginal(state, field);
    let label;
    if (state.pendingNull[field]) {
      label = '跟随环境';
    } else if (input.value.trim() === '') {
      label = (overridden && original === '') ? '已禁用' : '跟随环境';
    } else if (overridden || input.value.trim() !== original.trim()) {
      label = '自定义';
    } else {
      label = '跟随环境';
    }
    state.stateEls[field].textContent = label;
    state.followBtns[field].hidden = label !== '已禁用';
  }

  function alertShared(message) {
    if (namespace.modal && typeof namespace.modal.alert === 'function') {
      return namespace.modal.alert(message);
    }
    return Promise.resolve();
  }

  async function requestJson(url, options) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let info = {};
      try {
        const errBody = await response.json();
        info = (errBody && errBody.error) || {};
      } catch (parseErr) { /* fall through to the generic message */ }
      const err = new Error(info.message || ('请求失败（HTTP ' + response.status + '）'));
      err.status = response.status;
      err.code = info.code || null;
      throw err;
    }
    if (response.status === 204) return null;
    let body = null;
    try {
      body = await response.json();
    } catch (parseErr) {
      const err = new Error('响应格式异常');
      err.status = response.status;
      throw err;
    }
    if (!body || typeof body !== 'object') {
      // A 2xx with a non-object body is a load failure, not an empty config.
      const err = new Error('响应格式异常');
      err.status = response.status;
      throw err;
    }
    return body;
  }

  function modalButton(className, label, type) {
    const btn = el('button', className, label);
    btn.type = type || 'button';
    btn.style.fontSize = MODAL_BTN_FONT_SIZE;
    return btn;
  }

  function openConfigModal() {
    if (openBackdrop) return;
    modalToken++;
    const token = modalToken;
    // DOM mirrors management-ui.js buildModalFrame (private there, re-created
    // here): modal-backdrop > section.modal-dialog > form.providers-form with
    // modal-header + h4 + modal-close. No backdrop click-to-close: the config
    // modal exits only via the close/cancel buttons.
    const backdrop = ui.el('div', 'modal-backdrop');
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = '配置外部链接';
    const closeBtn = modalButton('modal-close', '×');
    closeBtn.setAttribute('aria-label', '关闭');
    header.appendChild(title);
    header.appendChild(closeBtn);
    form.appendChild(header);
    const body = ui.el('div', 'otel-link-config');
    form.appendChild(body);
    form.appendChild(el('div', 'providers-form__hint muted', '修改后留空表示跟随环境变量默认配置，未修改字段保持原配置'));
    const footer = ui.el('div', 'providers-form__actions');
    const restoreBtn = modalButton('btn btn--danger', '恢复默认');
    restoreBtn.style.marginRight = 'auto';
    const cancelBtn = modalButton('btn', '取消');
    const saveBtn = modalButton('btn btn--primary', '保存', 'submit');
    footer.appendChild(restoreBtn);
    footer.appendChild(cancelBtn);
    footer.appendChild(saveBtn);
    form.appendChild(footer);
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    document.body.appendChild(backdrop);
    openBackdrop = backdrop;

    const state = { snapshot: null, pendingNull: {}, submitting: false,
                    inputs: {}, stateEls: {}, followBtns: {}, overridden: {} };

    function alive() {
      return token === modalToken && isActive();
    }
    function close() {
      if (openBackdrop !== backdrop) return;
      // Invalidate every pending async callback bound to this modal.
      modalToken++;
      openBackdrop = null;
      backdrop.remove();
    }
    function setButtons() {
      saveBtn.disabled = state.submitting || !state.snapshot;
      restoreBtn.disabled = state.submitting || !state.snapshot;
    }

    closeBtn.addEventListener('click', function () { close(); });
    cancelBtn.addEventListener('click', function () { close(); });
    form.addEventListener('submit', function (ev) { ev.preventDefault(); onSubmit(); });
    restoreBtn.addEventListener('click', function () { onRestore(); });

    function renderLoadError(message, allowRetry) {
      body.replaceChildren();
      body.appendChild(el('div', 'error-state', message));
      if (allowRetry) {
        const retryBtn = modalButton('btn', '重试');
        retryBtn.addEventListener('click', function () { loadConfig(); });
        body.appendChild(retryBtn);
      }
    }

    function renderForm() {
      body.replaceChildren();
      const dto = state.snapshot;
      const overriddenList = (dto && dto.overridden_fields) || [];
      LINK_CONFIG_FIELDS.forEach(function (pair) {
        const field = pair[0];
        const labelText = pair[1];
        state.overridden[field] = overriddenList.indexOf(field) !== -1;
        state.pendingNull[field] = false;
        const row = ui.el('div', 'otel-link-config__row');
        row.appendChild(el('label', 'otel-link-config__label', labelText));
        const input = document.createElement('input');
        input.type = 'text';
        input.name = field;
        input.value = fieldOriginal(state, field);
        const stateEl = el('span', 'otel-link-config__state', '');
        const followBtn = el('button', 'btn btn--small', '跟随环境');
        followBtn.type = 'button';
        followBtn.style.fontSize = 'var(--font-size-md)';
        followBtn.addEventListener('click', function () {
          state.pendingNull[field] = true;
          input.value = '';
          refreshFieldState(state, field);
        });
        input.addEventListener('input', function () {
          // New input clears the pending follow-env marker so the typed value
          // is never silently dropped by the null marker.
          if (input.value.trim() !== '') state.pendingNull[field] = false;
          refreshFieldState(state, field);
        });
        state.inputs[field] = input;
        state.stateEls[field] = stateEl;
        state.followBtns[field] = followBtn;
        refreshFieldState(state, field);
        row.appendChild(input);
        row.appendChild(stateEl);
        row.appendChild(followBtn);
        body.appendChild(row);
      });
    }

    async function loadConfig() {
      state.snapshot = null;
      setButtons();
      try {
        const dto = await requestJson(LINKS_CONFIG_API_URL);
        if (!alive()) return;
        if (!dto) { renderLoadError('加载配置失败: 响应为空', true); return; }
        state.snapshot = dto;
        // A successful load re-enables the entry after an earlier 404.
        configEntryDisabled = false;
        if (entryButton) entryButton.disabled = false;
        renderForm();
      } catch (err) {
        if (!alive()) return;
        if (err && err.status === 404) {
          renderLoadError('链接配置服务不可用（未注入管理接口）', false);
          configEntryDisabled = true;
          if (entryButton) entryButton.disabled = true;
        } else {
          renderLoadError('加载配置失败: ' + (err && err.message ? err.message : err), true);
        }
      } finally {
        if (alive()) setButtons();
      }
    }

    function buildDiff() {
      const diff = {};
      LINK_CONFIG_FIELDS.forEach(function (pair) {
        const field = pair[0];
        if (state.pendingNull[field]) { diff[field] = null; return; }
        // Contract: compare the trimmed input against the trimmed snapshot;
        // send the trimmed value, or null when it trims to empty (follow env).
        const original = fieldOriginal(state, field).trim();
        const trimmed = state.inputs[field].value.trim();
        if (trimmed === original) return;
        diff[field] = trimmed === '' ? null : trimmed;
      });
      return diff;
    }

    async function sendPatch(payload) {
      return requestJson(LINKS_CONFIG_API_URL, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    }

    function handlePatchError(err) {
      state.submitting = false;
      setButtons();
      if (err && err.status === 409) {
        // Never auto-retry or overwrite: keep inputs and the old version.
        alertShared('配置已被他人修改，请重新打开弹窗后重试');
      } else {
        alertShared(err && err.message ? err.message : '保存失败');
      }
    }

    async function onSubmit() {
      if (state.submitting || !state.snapshot) return;
      const diff = buildDiff();
      if (Object.keys(diff).length === 0) { close(); return; }
      diff.expected_version = state.snapshot.version;
      state.submitting = true;
      setButtons();
      try {
        await sendPatch(diff);
        if (!alive()) return;
        close();
        alertShared('链接配置已保存');
        runLoad(true);
      } catch (err) {
        if (!alive()) return;
        handlePatchError(err);
      }
    }

    async function onRestore() {
      if (state.submitting || !state.snapshot) return;
      // Take the submit lock before awaiting the shared confirm so neither a
      // second restore nor a save can issue a PATCH while waiting.
      state.submitting = true;
      setButtons();
      // Missing shared confirm defaults to "not confirmed": never write.
      const confirmed = (namespace.modal && typeof namespace.modal.confirm === 'function')
        ? await namespace.modal.confirm('确认恢复默认？所有字段将跟随环境变量配置。')
        : false;
      if (!alive()) return;
      if (!confirmed) {
        state.submitting = false;
        setButtons();
        return;
      }
      const payload = { expected_version: state.snapshot.version };
      LINK_CONFIG_FIELDS.forEach(function (pair) { payload[pair[0]] = null; });
      try {
        await sendPatch(payload);
        if (!alive()) return;
        close();
        alertShared('已恢复默认链接配置');
        runLoad(true);
      } catch (err) {
        if (!alive()) return;
        handlePatchError(err);
      }
    }

    setButtons();
    loadConfig();
  }

  function deactivate() {
    renderToken++;
    inflight = null;
    modalToken++;
    if (openBackdrop) {
      openBackdrop.remove();
      openBackdrop = null;
    }
  }

  namespace.observationsOtel = { init: init, refresh: refresh, deactivate: deactivate };
  global.NAGENT = namespace;
}(window));
