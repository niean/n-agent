(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});

  let providers = [];
  let initialized = false;

  const PROVIDER_TYPES = [
    { value: 'mem0', label: 'mem0' },
    { value: 'honcho', label: 'honcho' },
    { value: 'holographic', label: 'holographic' },
  ];

  function root() {
    return ui.byId ? ui.byId('external-memory-providers-list') : document.getElementById('external-memory-providers-list');
  }

  // 确保容器存在：外部记忆 tab 由 external-memory.js 接管渲染并在刷新时清空节点，
  // 因此本模块在初始化时若找不到容器则自行创建并追加到 tab-memory。
  function ensureContainer() {
    let container = root();
    if (container) return container;
    container = document.createElement('div');
    container.id = 'external-memory-providers-list';
    const tab = ui.byId ? ui.byId('tab-memory') : document.getElementById('tab-memory');
    if (tab) tab.appendChild(container);
    return container;
  }

  function button(label, className, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className || 'btn';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
  }

  function appendCell(row, value) {
    const td = document.createElement('td');
    td.textContent = value == null || value === '' ? '-' : String(value);
    row.appendChild(td);
    return td;
  }

  function appendBadgeCell(row, text, kind) {
    const td = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = 'badge badge--' + kind;
    badge.textContent = text;
    td.appendChild(badge);
    row.appendChild(td);
  }

  function field(form, name, labelText, value, options) {
    const label = document.createElement('label');
    label.textContent = labelText;
    let input;
    if (options && options.type === 'select') {
      input = document.createElement('select');
      (options.items || []).forEach((item) => {
        const option = document.createElement('option');
        option.value = item.value;
        option.textContent = item.label;
        input.appendChild(option);
      });
    } else if (options && options.type === 'textarea') {
      input = document.createElement('textarea');
    } else {
      input = document.createElement('input');
      input.type = options && options.type ? options.type : 'text';
    }
    input.name = name;
    input.id = 'emp-' + name;
    if (value != null) input.value = value;
    if (options && options.placeholder) input.placeholder = options.placeholder;
    if (options && options.disabled) input.disabled = true;
    if (options && options.rows) input.rows = options.rows;
    label.appendChild(input);
    form.appendChild(label);
    return input;
  }

  function checkbox(form, name, labelText, checked) {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.name = name;
    input.id = 'emp-' + name;
    input.checked = !!checked;
    const span = document.createElement('span');
    span.textContent = labelText;
    label.append(input, span);
    form.appendChild(label);
    return input;
  }

  function statusKind(enabled) {
    return enabled ? 'success' : 'warning';
  }

  function probeKind(status) {
    if (status === 'ok') return 'success';
    if (status === 'failed') return 'danger';
    return 'warning';
  }

  function buildDetailPreview(provider) {
    const parts = [];
    if (provider.base_url) parts.push('base_url: ' + provider.base_url);
    const extra = provider.extra_config;
    if (extra && typeof extra === 'object') {
      const items = Object.entries(extra);
      if (items.length) {
        parts.push('extra: ' + items.map(([k, v]) => k + '=' + v).join(', '));
      }
    }
    return parts.join('\n');
  }

  // 召回模式取值：
  // - mem0: 固定 tools（prefetch 永远返回空，仅暴露 mem0_search 等工具由 LLM 主动调用）
  // - holographic: 从 extra_config.recall_mode 读，缺省 hybrid（context 仅 prefetch；tools 仅 fact_search 只读检索；hybrid 两者 + 写入管理）
  // - honcho: 从 extra_config.recall_mode 读，缺省 hybrid
  function recallModeOf(provider) {
    if (provider.provider_type === 'mem0') return 'tools';
    if (provider.provider_type === 'holographic') {
      const extra = provider.extra_config;
      if (!extra || typeof extra !== 'object') return 'hybrid';
      const mode = extra.recall_mode;
      return mode === 'context' || mode === 'tools' || mode === 'hybrid' ? mode : 'hybrid';
    }
    if (provider.provider_type === 'honcho') {
      const extra = provider.extra_config;
      if (!extra || typeof extra !== 'object') return 'hybrid';
      const mode = extra.recall_mode;
      return mode === 'context' || mode === 'tools' || mode === 'hybrid' ? mode : 'hybrid';
    }
    return '';
  }

  function renderList(container) {
    ui.clear(container);

    const panel = ui.el('section', 'status-panel');
    const panelHeader = ui.el('div', 'panel-header');
    const titleGroup = ui.el('div', 'panel-title-group');
    const title = ui.el('span', 'panel-title');
    title.textContent = '检索记忆';
    const tips = ui.el('span', 'panel-tips muted');
    tips.textContent = '管理 mem0 / honcho / holographic 检索记忆 Provider。同一时刻仅一个 Provider 处于激活状态，激活时会刷新工具面。';
    titleGroup.append(title, tips);
    const actions = ui.el('span', 'panel-actions');
    actions.append(
      button('+ 新建', 'btn', () => openForm()),
      button('刷新', 'btn', load)
    );
    panelHeader.append(titleGroup, actions);
    panel.appendChild(panelHeader);

    const body = ui.el('div', 'panel-body');

    if (!providers.length) {
      ui.renderEmpty(body, '暂无 Provider');
    } else {
      const table = document.createElement('table');
      table.className = 'document-table memory-table memory-table--retrieval';
      const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      const headers = [
        { label: '名称' },
        { label: '类型' },
        { label: '详情' },
        { label: '召回模式', tip: 'context：自动注入上下文；tools：暴露给工具，由LLM自主决定何时查询；hybrid：自自动注入上下文，同时暴露给工具' },
        { label: '探测' },
        { label: '启用' },
        { label: '操作' },
      ];
      headers.forEach((h) => {
        const th = document.createElement('th');
        if (h.tip) {
          // 复用 panel-title-group + panel-tips 统一 tooltip 样式，与"系统记忆/文件记忆/检索记忆"面板标题一致
          const group = document.createElement('span');
          group.className = 'panel-title-group';
          const labelSpan = document.createElement('span');
          labelSpan.className = 'panel-title';
          labelSpan.textContent = h.label;
          const tipSpan = document.createElement('span');
          tipSpan.className = 'panel-tips muted';
          tipSpan.textContent = h.tip;
          group.append(labelSpan, tipSpan);
          th.appendChild(group);
        } else {
          th.textContent = h.label;
        }
        headerRow.appendChild(th);
      });
      thead.appendChild(headerRow);
      const tbody = document.createElement('tbody');
      providers.forEach((provider) => tbody.appendChild(renderRow(provider)));
      table.append(thead, tbody);
      body.appendChild(table);
    }

    panel.appendChild(body);
    container.appendChild(panel);
  }

  function renderRow(provider) {
    const tr = document.createElement('tr');

    appendCell(tr, provider.name);
    appendCell(tr, provider.provider_type);

    // 详情列 - base_url + extra_config 摘要，256 字符截断
    const descTd = document.createElement('td');
    let preview = buildDetailPreview(provider);
    if (preview.length > 256) {
      preview = preview.substring(0, 256) + '...';
    }
    descTd.textContent = preview || '-';
    tr.appendChild(descTd);

    // 召回模式列 - honcho 与 holographic 从 extra_config.recall_mode 读（缺省 hybrid），mem0 固定 tools，其余显示 -
    const recallTd = document.createElement('td');
    const recallMode = recallModeOf(provider);
    recallTd.textContent = recallMode || '-';
    tr.appendChild(recallTd);

    // 探测列 - 独立成列，窄宽度
    const probeTd = document.createElement('td');
    if (provider.probe_status) {
      const probeBadge = document.createElement('span');
      probeBadge.className = 'badge badge--' + probeKind(provider.probe_status);
      probeBadge.textContent = provider.probe_status;
      probeTd.appendChild(probeBadge);
    } else {
      probeTd.textContent = '-';
    }
    tr.appendChild(probeTd);

    // 启用列 - 激活状态 badge
    const enabledTd = document.createElement('td');
    const enabled = provider.enabled === true;
    const enabledBadge = document.createElement('span');
    enabledBadge.className = 'badge badge--' + statusKind(enabled);
    enabledBadge.textContent = enabled ? '已激活' : '停用';
    enabledTd.appendChild(enabledBadge);
    tr.appendChild(enabledTd);

    // 操作列 - 激活/探测/查看/编辑/删除，全部水平排列
    const actions = document.createElement('td');
    actions.className = 'row-actions-cell';
    const actionGroup = document.createElement('div');
    actionGroup.className = 'row-actions row-actions--memory';
    if (!enabled) {
      actionGroup.append(button('激活', 'btn', () => activateProvider(provider.id)));
    }
    actionGroup.append(
      button('探测', 'btn', () => probeProvider(provider.id)),
      button('查看', 'btn', () => openViewForm(provider)),
      button('编辑', 'btn', () => openForm(provider)),
      button('删除', 'btn', () => deleteProvider(provider))
    );
    actions.appendChild(actionGroup);
    tr.appendChild(actions);
    return tr;
  }

  function closeModal() {
    const modal = document.getElementById('external-memory-providers-modal');
    if (modal) modal.remove();
  }

  function closeViewModal() {
    const modal = document.getElementById('external-memory-providers-view-modal');
    if (modal) modal.remove();
  }

  // 只读查看弹出框，对齐文件记忆 openViewProjectForm 的交互规则：
  // 与编辑表单共享要素布局，区别为 readOnly + 无保存按钮、底部仅"关闭"
  function openViewForm(provider) {
    closeViewModal();
    const backdrop = document.createElement('div');
    backdrop.id = 'external-memory-providers-view-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = '查看检索记忆: ' + provider.name;
    const close = button('×', 'modal-close', closeViewModal);
    close.setAttribute('aria-label', '关闭表单');
    header.append(title, close);
    form.appendChild(header);

    field(form, 'name', '名称', provider.name, { disabled: true });
    field(form, 'provider_type', '类型', provider.provider_type, { disabled: true });
    field(form, 'base_url', 'Base URL', provider.base_url || '', { disabled: true });

    // extra_config 只读展示
    const extraLabel = document.createElement('label');
    extraLabel.textContent = 'Extra Config';
    const extraTextarea = document.createElement('textarea');
    extraTextarea.value = JSON.stringify(provider.extra_config || {}, null, 2);
    extraTextarea.readOnly = true;
    extraTextarea.rows = 4;
    extraLabel.appendChild(extraTextarea);
    form.appendChild(extraLabel);

    // 激活状态
    const enabledDiv = document.createElement('div');
    enabledDiv.style.marginBottom = 'var(--space-6)';
    const enabledLabel = document.createElement('label');
    const enabledSpan = document.createElement('span');
    enabledSpan.textContent = ' 已激活: ' + (provider.enabled ? '是' : '否');
    enabledLabel.appendChild(enabledSpan);
    enabledDiv.appendChild(enabledLabel);
    form.appendChild(enabledDiv);

    // 探测状态
    const probeDiv = document.createElement('div');
    probeDiv.style.marginBottom = 'var(--space-6)';
    const probeLabel = document.createElement('label');
    const probeSpan = document.createElement('span');
    probeSpan.textContent = ' 探测状态: ' + (provider.probe_status || 'unknown');
    probeLabel.appendChild(probeSpan);
    probeDiv.appendChild(probeLabel);
    form.appendChild(probeDiv);

    const actions = ui.el('div', 'providers-form__actions');
    actions.append(button('关闭', 'btn', closeViewModal));
    form.appendChild(actions);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeViewModal();
    });
    document.body.appendChild(backdrop);
  }

  // 按 provider_type 动态渲染字段：
  // - mem0: base_url + api_key + user_id（user_id 进入 extra_config）
  // - honcho: base_url + api_key + workspace_id + user_id + ai_peer_id + session_strategy + recall_mode（均进入 extra_config）
  // - holographic: db_path + auto_extract + recall_mode（均进入 extra_config，base_url 留空）
  function renderDynamicFields(form, providerType, provider) {
    const dynamic = document.createElement('div');
    dynamic.id = 'emp-dynamic-fields';

    const extra = (provider && provider.extra_config) || {};
    if (providerType === 'mem0') {
      field(dynamic, 'base_url', 'Base URL', (provider && provider.base_url) || '', { placeholder: 'https://api.mem0.ai/v3' });
      field(dynamic, 'api_key', 'API Key', '', {
        type: 'password',
        placeholder: provider && provider.api_key_present ? '留空保持不变；输入空格清空' : '留空表示不设置',
      });
      field(dynamic, 'user_id', 'User ID', extra.user_id || '', { placeholder: 'n-agent-user' });
    } else if (providerType === 'honcho') {
      field(dynamic, 'base_url', 'Base URL', (provider && provider.base_url) || 'https://api.honcho.dev', { placeholder: 'https://api.honcho.dev' });
      field(dynamic, 'api_key', 'API Key', '', {
        type: 'password',
        placeholder: provider && provider.api_key_present ? '留空保持不变；输入空格清空' : '留空表示不设置',
      });
      field(dynamic, 'workspace_id', 'Workspace ID', extra.workspace_id || '', { placeholder: 'workspace 标识（必填）' });
      field(dynamic, 'user_id', 'User Peer ID', extra.user_id || '', { placeholder: 'n-agent-user' });
      field(dynamic, 'ai_peer_id', 'AI Peer ID', extra.ai_peer_id || '', { placeholder: 'n-agent' });
      field(dynamic, 'session_strategy', 'Session Strategy', extra.session_strategy || 'per-session', {
        type: 'select',
        items: [
          { value: 'per-session', label: 'per-session' },
          { value: 'persistent', label: 'persistent' },
        ],
      });
      field(dynamic, 'recall_mode', 'Recall Mode', extra.recall_mode || 'hybrid', {
        type: 'select',
        items: [
          { value: 'hybrid', label: 'hybrid' },
          { value: 'context', label: 'context' },
          { value: 'tools', label: 'tools' },
        ],
      });
    } else if (providerType === 'holographic') {
      field(dynamic, 'db_path', 'DB Path', extra.db_path || '', { placeholder: 'holographic.db' });
      checkbox(dynamic, 'auto_extract', ' 自动抽取记忆', extra.auto_extract === true);
      field(dynamic, 'recall_mode', 'Recall Mode', extra.recall_mode || 'hybrid', {
        type: 'select',
        items: [
          { value: 'hybrid', label: 'hybrid' },
          { value: 'context', label: 'context' },
          { value: 'tools', label: 'tools' },
        ],
      });
    } else {
      const hint = ui.el('p', 'muted');
      hint.textContent = '未知 Provider 类型：' + providerType;
      dynamic.appendChild(hint);
    }
    form.appendChild(dynamic);
  }

  function rebuildDynamicFields(form, providerType, provider) {
    const existing = form.querySelector('#emp-dynamic-fields');
    if (existing) existing.remove();
    renderDynamicFields(form, providerType, provider);
  }

  function openForm(provider) {
    closeModal();
    const isEdit = !!provider;
    const backdrop = document.createElement('div');
    backdrop.id = 'external-memory-providers-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = isEdit ? '编辑检索记忆' : '新建检索记忆';
    const close = button('×', 'modal-close', closeModal);
    close.setAttribute('aria-label', '关闭表单');
    header.append(title, close);
    form.appendChild(header);

    const nameField = field(form, 'name', '名称', (provider && provider.name) || '', {
      placeholder: 'provider 唯一名称',
    });

    const typeLabel = document.createElement('label');
    typeLabel.textContent = '类型';
    const typeSelect = document.createElement('select');
    typeSelect.name = 'provider_type';
    typeSelect.id = 'emp-provider_type';
    if (isEdit) typeSelect.disabled = true;
    PROVIDER_TYPES.forEach((item) => {
      const option = document.createElement('option');
      option.value = item.value;
      option.textContent = item.label;
      if (provider && provider.provider_type === item.value) option.selected = true;
      typeSelect.appendChild(option);
    });
    typeLabel.appendChild(typeSelect);
    form.appendChild(typeLabel);

    const initialType = (provider && provider.provider_type) || 'mem0';
    renderDynamicFields(form, initialType, provider);

    typeSelect.addEventListener('change', () => {
      rebuildDynamicFields(form, typeSelect.value, provider);
    });

    const message = ui.el('div', 'providers-form__hint muted');
    form.appendChild(message);
    const actions = ui.el('div', 'providers-form__actions');
    actions.append(button('取消', 'btn', closeModal));
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'btn btn--primary';
    submit.textContent = '保存';
    actions.appendChild(submit);
    form.appendChild(actions);

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await submitForm(form, provider, message);
    });

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeModal();
    });
    document.body.appendChild(backdrop);
  }

  function collectFormPayload(form, provider) {
    const data = new FormData(form);
    // provider_type 在编辑模式下为 disabled select，FormData 不收录，需从 provider 回填；
    // name 字段可编辑（支持改名），仅在 FormData 缺失时回填，空字符串保留以触发校验
    const providerType = String(
      data.get('provider_type') || (provider && provider.provider_type) || 'mem0'
    );
    const rawName = data.get('name');
    const name = (rawName === null || rawName === undefined)
      ? String((provider && provider.name) || '').trim()
      : String(rawName).trim();
    const extraConfig = {};

    if (providerType === 'mem0') {
      const baseUrl = String(data.get('base_url') || '').trim();
      const apiKeyRaw = data.get('api_key');
      const apiKey = apiKeyRaw === null || apiKeyRaw === undefined ? '' : String(apiKeyRaw);
      const userId = String(data.get('user_id') || '').trim();
      if (userId) extraConfig.user_id = userId;
      return {
        provider_type: providerType,
        name,
        base_url: baseUrl,
        api_key: apiKey,
        extra_config: extraConfig,
      };
    }
    if (providerType === 'honcho') {
      const baseUrl = String(data.get('base_url') || '').trim();
      const apiKeyRaw = data.get('api_key');
      const apiKey = apiKeyRaw === null || apiKeyRaw === undefined ? '' : String(apiKeyRaw);
      const workspaceId = String(data.get('workspace_id') || '').trim();
      const userId = String(data.get('user_id') || '').trim();
      const aiPeerId = String(data.get('ai_peer_id') || '').trim();
      const sessionStrategy = String(data.get('session_strategy') || 'per-session');
      const recallMode = String(data.get('recall_mode') || 'hybrid');
      if (workspaceId) extraConfig.workspace_id = workspaceId;
      if (userId) extraConfig.user_id = userId;
      if (aiPeerId) extraConfig.ai_peer_id = aiPeerId;
      extraConfig.session_strategy = sessionStrategy;
      extraConfig.recall_mode = recallMode;
      return {
        provider_type: providerType,
        name,
        base_url: baseUrl,
        api_key: apiKey,
        extra_config: extraConfig,
      };
    }
    if (providerType === 'holographic') {
      const dbPath = String(data.get('db_path') || '').trim();
      const autoExtract = data.get('auto_extract') === 'on';
      const recallMode = String(data.get('recall_mode') || 'hybrid');
      if (dbPath) extraConfig.db_path = dbPath;
      extraConfig.auto_extract = autoExtract;
      extraConfig.recall_mode = recallMode;
      return {
        provider_type: providerType,
        name,
        base_url: '',
        api_key: '',
        extra_config: extraConfig,
      };
    }
    return null;
  }

  async function submitForm(form, provider, message) {
    const payload = collectFormPayload(form, provider);
    if (!payload) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '无法识别的 Provider 类型';
      return;
    }
    if (!payload.name) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '名称不能为空';
      return;
    }
    const isEdit = !!provider;
    try {
      if (isEdit) {
        // PATCH：api_key 三态 —— 空串清空 / 非空覆盖 / 不发送则不变
        const body = {
          name: payload.name,
          base_url: payload.base_url,
          extra_config: payload.extra_config,
        };
        if (payload.api_key === '') {
          body.api_key = '';
        } else if (payload.api_key) {
          body.api_key = payload.api_key;
        }
        await api.fetchJson('/chat/external-memory/providers/' + encodeURIComponent(provider.id), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } else {
        const body = {
          name: payload.name,
          provider_type: payload.provider_type,
          base_url: payload.base_url,
          extra_config: payload.extra_config,
        };
        // 新建时 api_key 仅在非空时携带
        if (payload.api_key) body.api_key = payload.api_key;
        await api.fetchJson('/chat/external-memory/providers', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      }
      message.className = 'providers-form__hint badge badge--success';
      message.textContent = '保存成功';
      closeModal();
      await load();
    } catch (err) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '保存失败: ' + (err && err.message ? err.message : err);
    }
  }

  async function activateProvider(id) {
    try {
      const result = await api.fetchJson('/chat/external-memory/providers/' + encodeURIComponent(id) + '/activate', {
        method: 'POST',
      });
      if (result && result.tool_surface_refresh_failed) {
        window.alert('Provider 已激活，但工具面刷新失败，请稍后重试或检查日志');
      }
      await load();
    } catch (err) {
      window.alert('激活失败: ' + (err && err.message ? err.message : err));
    }
  }

  async function probeProvider(id) {
    try {
      const result = await api.fetchJson('/chat/external-memory/providers/' + encodeURIComponent(id) + '/probe', {
        method: 'POST',
      });
      await load();
      if (!result || result.probe_status !== 'ok') {
        const detail = result && result.last_probe_error ? result.last_probe_error : (result && result.probe_status || 'unknown');
        window.alert('探测失败: ' + detail);
      }
    } catch (err) {
      window.alert('探测失败: ' + (err && err.message ? err.message : err));
    }
  }

  async function deleteProvider(provider) {
    if (!window.confirm('确认删除 Provider ' + provider.name + '？')) return;
    try {
      await api.fetchJson('/chat/external-memory/providers/' + encodeURIComponent(provider.id), {
        method: 'DELETE',
      });
      await load();
    } catch (err) {
      window.alert('删除失败: ' + (err && err.message ? err.message : err));
    }
  }

  async function load() {
    const container = ensureContainer();
    if (!container) return;
    ui.clear(container);
    ui.renderLoading(container, '加载中...');
    try {
      const data = await api.fetchJson('/chat/external-memory/providers');
      providers = data.providers || [];
      renderList(container);
    } catch (err) {
      ui.clear(container);
      ui.renderError(container, '加载失败: ' + (err && err.message ? err.message : err));
    }
  }

  function init() {
    if (initialized) return load();
    initialized = true;
    return load();
  }

  function refresh() {
    return load();
  }

  namespace.externalMemoryProviders = { init, refresh, load };
  global.NAGENT = namespace;
}(window));
