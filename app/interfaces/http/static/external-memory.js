(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  let providers = [];
  let selectedProject = null;
  let currentTarget = 'memory';
  let editingEntryIndex = -1;
  let currentFullContent = '';

  function root() {
    return ui.byId ? ui.byId('tab-tools-external-memory') : document.getElementById('tab-tools-external-memory');
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

  function button(label, className, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = className || 'btn';
    btn.textContent = label;
    btn.addEventListener('click', onClick);
    return btn;
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
    input.id = 'external-memory-' + name;
    if (value != null) input.value = value;
    if (options && options.placeholder) input.placeholder = options.placeholder;
    if (options && options.step) input.step = options.step;
    if (options && options.min != null) input.min = String(options.min);
    if (options && options.max != null) input.max = String(options.max);
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
    input.id = 'external-memory-' + name;
    input.checked = !!checked;
    const span = document.createElement('span');
    span.textContent = labelText;
    label.append(input, span);
    form.appendChild(label);
    return input;
  }

  function buildPage(node) {
    const systemPanel = ui.el('section', 'status-panel');
    const systemHeader = ui.el('div', 'panel-header');
    const systemTitleGroup = ui.el('div', 'panel-title-group');
    const systemTitle = ui.el('span', 'panel-title');
    systemTitle.textContent = '系统记忆';
    const systemTips = ui.el('span', 'panel-tips muted');
    systemTips.textContent = '内置系统记忆。builtin 是单组全局系统记忆。';
    systemTitleGroup.append(systemTitle, systemTips);
    systemHeader.append(systemTitleGroup);
    const systemBody = ui.el('div', 'panel-body');
    const systemList = ui.el('div', '');
    systemList.id = 'system-providers-list';
    systemBody.appendChild(systemList);
    systemPanel.append(systemHeader, systemBody);
    node.appendChild(systemPanel);

    const projectPanel = ui.el('section', 'status-panel');
    const projectHeader = ui.el('div', 'panel-header');
    const projectTitleGroup = ui.el('div', 'panel-title-group');
    const projectTitle = ui.el('span', 'panel-title');
    projectTitle.textContent = '文件记忆';
    const projectTips = ui.el('span', 'panel-tips muted');
    projectTips.textContent = '每个子目录对应一组独立的文件记忆，可以分别勾选启用。点击文件记忆名称编辑记忆内容。';
    projectTitleGroup.append(projectTitle, projectTips);
    const projectActions = ui.el('span', 'panel-actions');
    projectActions.append(
      button('+ 新建', 'btn', () => openProjectForm()),
      button('刷新', 'btn', load)
    );
    projectHeader.append(projectTitleGroup, projectActions);
    const projectBody = ui.el('div', 'panel-body');
    const projectList = ui.el('div', '');
    projectList.id = 'external-memory-list';
    projectBody.appendChild(projectList);
    projectPanel.append(projectHeader, projectBody);
    node.appendChild(projectPanel);

    renderSystemTable(systemList);
    renderProjectTable(projectList);
  }

  function renderSystemTable(node) {
    ui.clear(node);
    const { system } = splitProviders();
    if (!system.length) {
      ui.renderEmpty(node, '暂无系统记忆');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table memory-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['名称', '类型', '详情', '启用', '操作'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    system.forEach((provider) => tbody.appendChild(renderSystemRow(provider)));
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function renderSystemRow(provider) {
    const tr = document.createElement('tr');
    appendCell(tr, provider.name);
    appendCell(tr, '系统记忆');
    // 详情列 - builtin 也显示内存预览
    appendCell(tr, (provider.description || '').trim().substring(0, 256) + ((provider.description || '').length > 256 ? '...' : ''));
    appendBadgeCell(tr, provider.enabled_global ? '启用' : '停用', provider.enabled_global ? 'success' : 'warning');
    const actions = document.createElement('td');
    actions.className = 'row-actions-cell';
    const actionGroup = document.createElement('div');
    actionGroup.className = 'row-actions row-actions--memory';
    actionGroup.append(
      button('查看', 'btn', () => openViewProjectForm(provider)),
      button('编辑', 'btn', () => openProjectForm(provider))
    );
    actions.appendChild(actionGroup);
    tr.appendChild(actions);
    return tr;
  }

  function renderProjectTable(node) {
    ui.clear(node);
    const { projects } = splitProviders();
    if (!projects.length) {
      ui.renderEmpty(node, '暂无文件记忆');
      return;
    }
    const table = document.createElement('table');
    table.className = 'document-table memory-table';
    const thead = document.createElement('thead');
    const headerRow = document.createElement('tr');
    ['名称', '类型', '详情', '启用', '操作'].forEach((label) => {
      const th = document.createElement('th');
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    const tbody = document.createElement('tbody');
    projects.forEach((provider) => tbody.appendChild(renderProjectRow(provider)));
    table.append(thead, tbody);
    node.appendChild(table);
  }

  function renderProjectRow(provider) {
    const tr = document.createElement('tr');
    appendCell(tr, provider.name);
    appendCell(tr, '文件记忆');
    // 详情列 - 显示记忆内容预览
    const descTd = document.createElement('td');
    let preview = (provider.description || '').trim();
    if (preview.length > 256) {
      preview = preview.substring(0, 256) + '...';
    }
    descTd.textContent = preview || '-';
    tr.appendChild(descTd);
    appendBadgeCell(tr, provider.enabled_global ? '启用' : '停用', provider.enabled_global ? 'success' : 'warning');
    const actions = document.createElement('td');
    actions.className = 'row-actions-cell';
    const actionGroup = document.createElement('div');
    actionGroup.className = 'row-actions row-actions--memory';
    actionGroup.append(
      button('查看', 'btn', () => openViewProjectForm(provider)),
      button('编辑', 'btn', () => openProjectForm(provider)),
      button('删除', 'btn', () => deleteProject(provider))
    );
    actions.appendChild(actionGroup);
    tr.appendChild(actions);
    return tr;
  }

  function splitProviders() {
    const system = [];
    const projects = [];
    providers.forEach((provider) => {
      const slot = provider.slot || (provider.name === 'builtin' ? 'builtin' : 'multi-project');
      if (slot === 'builtin') system.push(provider);
      else if (slot === 'multi-project') projects.push(provider);
      // external-query 不属于本页，由检索记忆页管理
    });
    return { system, projects };
  }

  function closeProjectForm() {
    const modal = document.getElementById('external-memory-project-modal');
    if (modal) modal.remove();
  }

  function closeEntryForm() {
    const modal = document.getElementById('external-memory-entry-modal');
    if (modal) modal.remove();
  }

  function closeFullContentForm() {
    const modal = document.getElementById('external-memory-full-modal');
    if (modal) modal.remove();
  }

  function closeViewProjectForm() {
    const modal = document.getElementById('external-memory-view-modal');
    if (modal) modal.remove();
  }

  async function openViewProjectForm(provider) {
    closeViewProjectForm();
    const isSystem = provider.name === 'builtin';
    const backdrop = document.createElement('div');
    backdrop.id = 'external-memory-view-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = '查看' + (isSystem ? '系统记忆' : '文件记忆') + ': ' + provider.name;
    const close = button('×', 'modal-close', closeViewProjectForm);
    close.setAttribute('aria-label', '关闭表单');
    header.append(title, close);
    form.appendChild(header);

    const loading = ui.el('div', '');
    loading.textContent = '加载中...';
    form.appendChild(loading);

    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeViewProjectForm();
    });
    document.body.appendChild(backdrop);

    try {
      const [contentData, entriesData] = await Promise.all([
        api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(provider.name) + '/memory?target=memory'),
        api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(provider.name) + '/entries?target=memory'),
      ]);
      ui.clear(form);
      form.appendChild(header);

      // 与编辑框保持相同的要素
      if (!isSystem) {
        field(form, 'project_name', '文件记忆名称', provider.name, { disabled: true });
      }
      // 显示启用状态
      const enabledDiv = document.createElement('div');
      enabledDiv.style.marginBottom = 'var(--space-6)';
      const enabledLabel = document.createElement('label');
      const enabledSpan = document.createElement('span');
      enabledSpan.textContent = ' 全局启用: ' + (provider.enabled_global ? '是' : '否');
      enabledLabel.appendChild(enabledSpan);
      enabledDiv.appendChild(enabledLabel);
      form.appendChild(enabledDiv);

      const contentLabel = document.createElement('label');
      contentLabel.textContent = '记忆内容';
      const contentTextarea = document.createElement('textarea');
      contentTextarea.value = contentData.content || '';
      contentTextarea.readOnly = true;
      contentTextarea.rows = 12;
      contentLabel.appendChild(contentTextarea);
      form.appendChild(contentLabel);

      const entriesLabel = document.createElement('label');
      entriesLabel.textContent = '条目列表 (' + ((entriesData.entries || []).length) + ')';
      const entriesDiv = document.createElement('div');
      entriesDiv.style.maxHeight = '200px';
      entriesDiv.style.overflowY = 'auto';
      entriesDiv.style.border = '1px solid var(--color-border-default)';
      entriesDiv.style.borderRadius = '4px';
      entriesDiv.style.padding = '8px';
      (entriesData.entries || []).forEach((entry, idx) => {
        const entryDiv = document.createElement('div');
        entryDiv.style.padding = '4px 0';
        entryDiv.style.borderBottom = idx < (entriesData.entries || []).length - 1 ? '1px solid var(--color-border-light)' : 'none';
        const preview = entry.length > 100 ? entry.substring(0, 100) + '...' : entry;
        entryDiv.textContent = (idx + 1) + '. ' + preview;
        entriesDiv.appendChild(entryDiv);
        entryDiv.style.overflow = 'hidden';
      });
      entriesLabel.appendChild(entriesDiv);
      form.appendChild(entriesLabel);

      const actions = ui.el('div', 'providers-form__actions');
      actions.append(
        button('关闭', 'btn', closeViewProjectForm)
      );
      form.appendChild(actions);
    } catch (err) {
      ui.clear(form);
      form.appendChild(header);
      const error = ui.el('div', 'badge badge--danger');
      error.textContent = '加载失败: ' + (err && err.message ? err.message : err);
      form.appendChild(error);
      const actions = ui.el('div', 'providers-form__actions');
      actions.append(
        button('关闭', 'btn', closeViewProjectForm)
      );
      form.appendChild(actions);
    }
  }

  async function openProjectForm(provider) {
    closeProjectForm();
    const isEdit = !!provider;
    const isSystem = provider && provider.name === 'builtin';
    const backdrop = document.createElement('div');
    backdrop.id = 'external-memory-project-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = isEdit ? (isSystem ? '编辑系统记忆' : '编辑文件记忆') : '新建文件记忆';
    const close = button('×', 'modal-close', closeProjectForm);
    close.setAttribute('aria-label', '关闭表单');
    header.append(title, close);
    form.appendChild(header);

    if (!isSystem) {
      field(form, 'project_name', '文件记忆名称', provider ? provider.name : '', { disabled: isEdit, placeholder: '仅允许字母、数字、连字符、下划线' });
    }
    // 新建和编辑都显示"全局启用"复选框
    const enabledChecked = isEdit ? provider.enabled_global : true;
    checkbox(form, 'enabled', '全局启用', enabledChecked);
    // 新建和编辑都允许编辑记忆内容
    const contentField = field(form, 'initial_content', '记忆内容', '', { type: 'textarea', rows: 10, placeholder: '记忆内容会作为文件记忆保存' });
    // 编辑时加载完整内容和条目列表
    let entriesData = { entries: [] };
    if (isEdit) {
      ui.renderLoading(form, '加载中...');
      try {
        const [contentRes, entriesRes] = await Promise.all([
          api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(provider.name) + '/memory?target=memory'),
          api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(provider.name) + '/entries?target=memory'),
        ]);
        contentField.value = contentRes.content || '';
        entriesData = entriesRes;
        // Remove loading, keep all fields
        ui.clear(form);
        form.appendChild(header);
        if (!isSystem) {
          field(form, 'project_name', '文件记忆名称', provider ? provider.name : '', { disabled: isEdit, placeholder: '仅允许字母、数字、连字符、下划线' });
        }
        checkbox(form, 'enabled', '全局启用', enabledChecked);
        contentField = field(form, 'initial_content', '记忆内容', contentRes.content || '', { type: 'textarea', rows: 8, placeholder: '记忆内容会作为文件记忆保存' });
      } catch (err) {
        contentField.value = provider.description || '';
      }
    }

    // 添加条目列表 - 仅预览展示，不支持在此编辑
    if (isEdit && entriesData.entries && entriesData.entries.length > 0) {
      const entriesLabel = document.createElement('label');
      entriesLabel.textContent = '条目列表 (' + entriesData.entries.length + ')';
      const entriesDiv = document.createElement('div');
      entriesDiv.style.maxHeight = '180px';
      entriesDiv.style.overflowY = 'auto';
      entriesDiv.style.border = '1px solid var(--color-border-default)';
      entriesDiv.style.borderRadius = '4px';
      entriesDiv.style.padding = '8px';
      entriesData.entries.forEach((entry, idx) => {
        const entryDiv = document.createElement('div');
        entryDiv.style.padding = '4px 0';
        entryDiv.style.borderBottom = idx < entriesData.entries.length - 1 ? '1px solid var(--color-border-light)' : 'none';
        const preview = entry.length > 80 ? entry.substring(0, 80) + '...' : entry;
        entryDiv.textContent = (idx + 1) + '. ' + preview;
        entryDiv.style.overflow = 'hidden';
        entriesDiv.appendChild(entryDiv);
      });
      entriesLabel.appendChild(entriesDiv);
      form.appendChild(entriesLabel);
    }

    const message = ui.el('div', 'providers-form__hint muted');
    form.appendChild(message);
    const actions = ui.el('div', 'providers-form__actions');
    actions.append(
      button('取消', 'btn', closeProjectForm)
    );
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'btn btn--primary';
    submit.textContent = '保存';
    actions.appendChild(submit);
    form.appendChild(actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await submitProjectForm(form, provider, message);
    });
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeProjectForm();
    });
    document.body.appendChild(backdrop);
  }

  async function submitProjectForm(form, provider, message) {
    const isEdit = !!provider;
    const isSystem = provider && provider.name === 'builtin';
    const data = new FormData(form);
    const enabled = data.get('enabled') === 'on';
    const initialContent = String(data.get('initial_content') || '').trim();
    try {
      if (isEdit) {
        // 从 providers 状态获取当前启用列表，只修改当前项目的状态
        let enabledList = providers.filter(p => p.enabled_global).map(p => p.name);
        if (enabled) {
          // 启用：如果不在列表中则添加
          if (!enabledList.includes(provider.name)) {
            enabledList.push(provider.name);
          }
        } else {
          // 禁用：从列表中移除
          enabledList = enabledList.filter(name => name !== provider.name);
        }
        // 保存启用状态
        await api.fetchJson('/chat/external-memory/set-enabled', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ enabled: enabledList }),
        });
        // 保存内容（总是保存，允许清空）
        await api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(provider.name) + '/memory', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: initialContent, target: 'memory' }),
        });
      } else {
        // New project
        const name = String(data.get('project_name') || '').trim();
        const nameRe = /^[A-Za-z0-9_-]+$/;
        if (!nameRe.test(name)) {
          message.className = 'providers-form__hint badge badge--danger';
          message.textContent = '文件记忆名称仅允许字母、数字、连字符(-)和下划线(_)';
          return;
        }
        await api.fetchJson('/chat/external-memory/projects/create', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name }),
        });
        // If there's initial content, save it
        await api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(name) + '/memory', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: initialContent, target: 'memory' }),
        });
        // Add to enabled list since it's checked by default
        if (enabled) {
          const enabledList = providers.filter(p => p.enabled_global).map(p => p.name);
          if (!enabledList.includes(name)) {
            enabledList.push(name);
          }
          await api.fetchJson('/chat/external-memory/set-enabled', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabledList }),
          });
        }
      }
      message.className = 'providers-form__hint badge badge--success';
      message.textContent = '保存成功';
      closeProjectForm();
      await load();
    } catch (err) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '保存失败: ' + (err && err.message ? err.message : err);
    }
  }

  function openEntryForm(index, content) {
    closeEntryForm();
    const isEdit = index != null && index >= 0;
    editingEntryIndex = isEdit ? index : -1;
    const backdrop = document.createElement('div');
    backdrop.id = 'external-memory-entry-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = isEdit ? '编辑条目' : '添加条目';
    const close = button('×', 'modal-close', closeEntryForm);
    close.setAttribute('aria-label', '关闭表单');
    header.append(title, close);
    form.appendChild(header);

    field(form, 'entry_content', '条目内容', content || '', { type: 'textarea', rows: 10 });

    const message = ui.el('div', 'providers-form__hint muted');
    form.appendChild(message);
    const actions = ui.el('div', 'providers-form__actions');
    actions.append(
      button('取消', 'btn', closeEntryForm)
    );
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'btn btn--primary';
    submit.textContent = '保存';
    actions.appendChild(submit);
    form.appendChild(actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await submitEntryForm(form, message);
    });
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeEntryForm();
    });
    document.body.appendChild(backdrop);
  }

  async function submitEntryForm(form, message) {
    if (!selectedProject) return;
    const data = new FormData(form);
    const content = String(data.get('entry_content') || '').trim();
    if (!content) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '内容不能为空';
      return;
    }
    try {
      if (editingEntryIndex === -1) {
        await api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(selectedProject) + '/entries', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: content, target: currentTarget }),
        });
      } else {
        await api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(selectedProject) + '/entries/' + editingEntryIndex, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ content: content, target: currentTarget }),
        });
      }
      message.className = 'providers-form__hint badge badge--success';
      message.textContent = '保存成功';
      closeEntryForm();
    } catch (err) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '保存失败: ' + (err && err.message ? err.message : err);
    }
  }

  function openFullContentForm() {
    closeFullContentForm();
    const backdrop = document.createElement('div');
    backdrop.id = 'external-memory-full-modal';
    backdrop.className = 'modal-backdrop';
    const dialog = document.createElement('section');
    dialog.className = 'modal-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = document.createElement('form');
    form.className = 'providers-form';
    const header = ui.el('div', 'modal-header');
    const title = document.createElement('h4');
    title.textContent = '编辑全文';
    const close = button('×', 'modal-close', closeFullContentForm);
    close.setAttribute('aria-label', '关闭表单');
    header.append(title, close);
    form.appendChild(header);

    field(form, 'full_content', '记忆内容', currentFullContent || '', { type: 'textarea', rows: 15 });

    const message = ui.el('div', 'providers-form__hint muted');
    form.appendChild(message);
    const actions = ui.el('div', 'providers-form__actions');
    actions.append(
      button('取消', 'btn', closeFullContentForm)
    );
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'btn btn--primary';
    submit.textContent = '保存';
    actions.appendChild(submit);
    form.appendChild(actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      await submitFullContentForm(form, message);
    });
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeFullContentForm();
    });
    document.body.appendChild(backdrop);
  }

  async function submitFullContentForm(form, message) {
    if (!selectedProject) return;
    const data = new FormData(form);
    const content = String(data.get('full_content') || '');
    try {
      await api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(selectedProject) + '/memory', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: content, target: currentTarget }),
      });
      message.className = 'providers-form__hint badge badge--success';
      message.textContent = '保存成功';
      closeFullContentForm();
    } catch (err) {
      message.className = 'providers-form__hint badge badge--danger';
      message.textContent = '保存失败: ' + (err && err.message ? err.message : err);
    }
  }

  async function deleteProject(provider) {
    if (!window.confirm('确认删除文件记忆 ' + provider.name + '？')) return;
    try {
      await api.fetchJson('/chat/external-memory/projects/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: provider.name }),
      });
      if (selectedProject === provider.name) {
        selectedProject = null;
      }
      await load();
    } catch (err) {
      alert('删除失败: ' + err.message);
    }
  }

  async function deleteEntryFn(index) {
    if (!selectedProject) return;
    if (!window.confirm('确认删除这个条目？')) return;
    try {
      await api.fetchJson('/chat/external-memory/projects/' + encodeURIComponent(selectedProject) + '/entries/' + index + '?target=' + currentTarget, {
        method: 'DELETE',
      });
    } catch (err) {
      alert('删除失败: ' + err.message);
    }
  }

  async function load() {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    ui.renderLoading(node, '加载中...');
    try {
      const data = await api.fetchJson('/chat/external-memory/memory-providers');
      providers = data.providers || [];
      node.replaceChildren();
      buildPage(node);
    } catch (err) {
      node.replaceChildren();
      ui.renderError(node, '加载失败: ' + (err && err.message ? err.message : err));
    }
  }

  namespace.externalMemory = { init: load, refresh: load, load: load };
  global.NAGENT = namespace;
}(window));
