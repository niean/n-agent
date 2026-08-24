(function (global) {
  const namespace = global.NAGENT || {};
  const ui = (namespace.ui || {});
  const api = (namespace.api || {});
  const modal = (namespace.modal || {});

  function root() {
    return ui.byId ? ui.byId('tab-tools-skill') : document.getElementById('tab-tools-skill');
  }

  let skills = [];
  let pendingItems = [];
  let usageMap = {};

  function render() {
    const node = root();
    if (!node) return;
    node.replaceChildren();

    const panel = ui.el('section', 'status-panel');
    const header = ui.el('div', 'panel-header');
    const title = ui.el('span');
    title.textContent = 'Skills';
    const actions = ui.el('span', 'panel-actions');
    const refreshBtn = ui.el('button', 'btn');
    refreshBtn.type = 'button';
    refreshBtn.textContent = '扫描';
    refreshBtn.addEventListener('click', refresh);
    actions.append(refreshBtn);
    header.append(title, actions);
    panel.appendChild(header);

    const body = ui.el('div', 'panel-body');
    renderListBody(body);
    panel.appendChild(body);
    node.appendChild(panel);
  }

  function renderListBody(body) {
    if (!skills.length && !pendingItems.length) {
      ui.renderEmpty(body, '尚未发现 Skill；请检查 skills_root 配置或点击重扫描');
      return;
    }

    // 待审批写入按 skill_name 索引：既有 skill 行内显示"待审批"并提供审批/拒绝；
    // 不在列表中的 pending（新建）追加为独立行，统一在状态列体现待审批。
    const pendingByName = new Map();
    pendingItems.forEach((pw) => {
      if (pw && pw.skill_name) pendingByName.set(pw.skill_name, pw);
    });

    const table = ui.el('table', 'document-table skills-table');
    const thead = ui.el('thead');
    const trh = ui.el('tr');
    ['名称', '描述', '来源', '就绪状态', '启用状态', '对话可见', '健康状态', '扫描状态', '格式状态', '操作'].forEach((h) => {
      const th = ui.el('th');
      th.textContent = h;
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);

    const tbody = ui.el('tbody');
    const existingNames = new Set(skills.map((s) => s.name));
    // 列表排序：先按来源(seed/user/agent)、再按 ASC(名称)
    const SOURCE_ORDER = { seed: 0, user: 1, agent: 2 };
    const sorted = skills.slice().sort((a, b) => {
      const oa = SOURCE_ORDER[a.source] ?? 99;
      const ob = SOURCE_ORDER[b.source] ?? 99;
      if (oa !== ob) return oa - ob;
      return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
    });
    sorted.forEach((s) => tbody.appendChild(renderRow(s, pendingByName.get(s.name))));
    pendingItems.forEach((pw) => {
      if (pw && pw.skill_name && !existingNames.has(pw.skill_name)) {
        tbody.appendChild(renderPendingCreateRow(pw));
      }
    });
    table.appendChild(tbody);
    body.appendChild(table);
  }

  function renderRow(s, pending) {
    const tr = ui.el('tr');
    const usage = usageMap[s.name] || {};
    // 名称、描述
    ['name', 'description'].forEach((key) => {
      const td = ui.el('td');
      const v = s[key];
      td.textContent = Array.isArray(v) ? v.join(',') : (v === null || v === undefined ? '' : String(v));
      tr.appendChild(td);
    });

    // 来源（Skill.source：seed/agent/user）
    const tdSource = ui.el('td');
    tdSource.textContent = s.source || '-';
    tr.appendChild(tdSource);

    // 就绪状态、启用状态
    ['readiness', 'enabled'].forEach((key) => {
      const td = ui.el('td');
      const v = s[key];
      if (key === 'enabled') {
        const badge = ui.el('span', 'badge badge--' + (s.enabled ? 'success' : 'warning'));
        badge.textContent = s.enabled ? '启用' : '停用';
        td.appendChild(badge);
      } else {
        const isOk = v === 'available';
        const badge = ui.el('span', 'badge badge--' + (isOk ? 'success' : 'warning'));
        badge.textContent = v == null ? '' : String(v);
        td.appendChild(badge);
      }
      tr.appendChild(td);
    });

    // 对话可见（chat_selectable）：仅 badge 显示状态，无操作按钮（编辑 modal 负责修改）
    const tdChat = ui.el('td');
    const chatSelectable = s.chat_selectable !== false;
    const chatBadge = ui.el('span', 'badge badge--' + (chatSelectable ? 'success' : 'warning'));
    chatBadge.textContent = chatSelectable ? '是' : '否';
    tdChat.appendChild(chatBadge);
    tr.appendChild(tdChat);

    const tdState = ui.el('td');
    if (pending) {
      const pendingBadge = ui.el('span', 'badge badge--warning');
      pendingBadge.textContent = '待审批';
      tdState.appendChild(pendingBadge);
    } else {
      const stateBadge = ui.el('span', 'badge badge--' + (usage.state === 'active' || !usage.state ? 'success' : 'warning'));
      stateBadge.textContent = usage.state || 'active';
      tdState.appendChild(stateBadge);
    }
    tr.appendChild(tdState);

    const tdScan = ui.el('td');
    const scanV = s.last_scan_status;
    const isOk = scanV === 'ok' || scanV === 'manual' || scanV == null || scanV === '';
    const scanBadge = ui.el('span', 'badge badge--' + (isOk ? 'success' : 'warning'));
    scanBadge.textContent = scanV == null || scanV === '' ? '未扫描' : String(scanV);
    tdScan.appendChild(scanBadge);
    tr.appendChild(tdScan);

    // 格式状态（Anthropic 规范合规性，从 last_scan_error 派生）
    const tdFmt = ui.el('td');
    const fmtOk = s.format_status === 'valid';
    const fmtBadge = ui.el('span', 'badge badge--' + (fmtOk ? 'success' : 'warning'));
    fmtBadge.textContent = fmtOk ? '合规' : (s.format_status ? String(s.format_status) : '');
    tdFmt.appendChild(fmtBadge);
    tr.appendChild(tdFmt);

    const td = ui.el('td');
    td.className = 'row-actions-cell';
    const group = ui.el('div', 'row-actions');
    if (pending) {
      const diffBtn = ui.el('button', 'btn');
      diffBtn.type = 'button';
      diffBtn.textContent = 'diff';
      diffBtn.addEventListener('click', () => openDiffModal(pending.pending_id));
      const approveBtn = ui.el('button', 'btn primary');
      approveBtn.type = 'button';
      approveBtn.textContent = '审批';
      approveBtn.addEventListener('click', () => approvePendingItem(pending.pending_id));
      const rejectBtn = ui.el('button', 'btn');
      rejectBtn.type = 'button';
      rejectBtn.textContent = '拒绝';
      rejectBtn.addEventListener('click', () => rejectPendingItem(pending.pending_id));
      group.append(diffBtn, approveBtn, rejectBtn);
    } else {
      const viewBtn = ui.el('button', 'btn');
      viewBtn.type = 'button';
      viewBtn.textContent = '详情';
      viewBtn.addEventListener('click', () => openDetail(s.name));
      const editBtn = ui.el('button', 'btn');
      editBtn.type = 'button';
      editBtn.textContent = '编辑';
      editBtn.addEventListener('click', () => openForm(s));
      const toggle = ui.el('button', 'btn');
      toggle.type = 'button';
      toggle.textContent = s.enabled ? '停用' : '启用';
      toggle.addEventListener('click', () => toggleEnabled(s.name, !s.enabled));
      const del = ui.el('button', 'btn');
      del.type = 'button';
      del.textContent = '删除';
      del.addEventListener('click', () => deleteSkill(s.name));
      group.append(toggle, editBtn, del, viewBtn);
    }
    td.appendChild(group);
    tr.appendChild(td);
    return tr;
  }

  // pending create（目标 skill 尚不在列表中）：以一行呈现，状态列标"待审批"，提供 diff/审批/拒绝
  function renderPendingCreateRow(pw) {
    const tr = ui.el('tr');
    const tdName = ui.el('td');
    tdName.textContent = (pw.skill_name || '(unknown)') + ' (新建)';
    tr.appendChild(tdName);
    // 描述 未知 -> 空单元格
    tr.appendChild(ui.el('td'));
    // 来源 未知
    const tdCreatedBy = ui.el('td');
    tdCreatedBy.textContent = '-';
    tr.appendChild(tdCreatedBy);
    // 就绪/启用/对话可见 未知 -> 空单元格
    for (let i = 0; i < 3; i += 1) tr.appendChild(ui.el('td'));
    const tdState = ui.el('td');
    const pendingBadge = ui.el('span', 'badge badge--warning');
    pendingBadge.textContent = '待审批';
    tdState.appendChild(pendingBadge);
    tr.appendChild(tdState);
    // 扫描状态 未知 -> 空单元格
    tr.appendChild(ui.el('td'));
    // 格式状态 未知 -> 空单元格
    tr.appendChild(ui.el('td'));
    const td = ui.el('td');
    td.className = 'row-actions-cell';
    const group = ui.el('div', 'row-actions');
    const diffBtn = ui.el('button', 'btn');
    diffBtn.type = 'button';
    diffBtn.textContent = 'diff';
    diffBtn.addEventListener('click', () => openDiffModal(pw.pending_id));
    const approveBtn = ui.el('button', 'btn primary');
    approveBtn.type = 'button';
    approveBtn.textContent = '审批';
    approveBtn.addEventListener('click', () => approvePendingItem(pw.pending_id));
    const rejectBtn = ui.el('button', 'btn');
    rejectBtn.type = 'button';
    rejectBtn.textContent = '拒绝';
    rejectBtn.addEventListener('click', () => rejectPendingItem(pw.pending_id));
    group.append(diffBtn, approveBtn, rejectBtn);
    td.appendChild(group);
    tr.appendChild(td);
    return tr;
  }

  async function refresh() {
    try {
      const res = await api.refreshSkills();
      if (res && res.warnings && res.warnings.length) {
        const lines = res.warnings.map((w) => (w.reason || 'warn') + ': ' + (w.relative_path || ''));
        await modal.alert('扫描完成，警告:\n' + lines.join('\n'));
      }
    } catch (err) {
      await modal.alert('扫描失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  async function toggleEnabled(name, enabled) {
    try {
      await api.setSkillEnabled(name, enabled);
    } catch (err) {
      await modal.alert('更新失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  async function deleteSkill(name) {
    if (!(await modal.confirm('删除 Skill 元数据 ' + name + '？'))) return;
    try {
      await api.deleteSkill(name);
    } catch (err) {
      await modal.alert('删除失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  function closeDetail() {
    const existing = ui.byId ? ui.byId('skill-detail-modal') : document.getElementById('skill-detail-modal');
    if (existing) existing.remove();
  }

  function closeForm() {
    const existing = ui.byId ? ui.byId('skill-form-modal') : document.getElementById('skill-form-modal');
    if (existing) existing.remove();
  }

  function closeDiffModal() {
    const existing = ui.byId ? ui.byId('skill-diff-modal') : document.getElementById('skill-diff-modal');
    if (existing) existing.remove();
  }

  function field(form, name, labelText, value, options) {
    const label = ui.el('label');
    const span = ui.el('span');
    span.textContent = labelText;
    let input;
    if (options && options.type === 'textarea') {
      input = ui.el('textarea');
      input.rows = options.rows || 4;
    } else if (options && options.type === 'select') {
      input = ui.el('select');
      (options.choices || []).forEach((choice) => {
        const option = ui.el('option');
        option.value = choice;
        option.textContent = choice;
        input.appendChild(option);
      });
    } else {
      input = ui.el('input');
      input.type = (options && options.type) || 'text';
    }
    input.name = name;
    input.value = value || '';
    if (options && options.disabled) input.disabled = true;
    if (options && options.placeholder) input.placeholder = options.placeholder;
    label.append(span, input);
    form.appendChild(label);
    return input;
  }

  function checkbox(form, name, labelText, checked) {
    const label = ui.el('label');
    const input = ui.el('input');
    input.type = 'checkbox';
    input.name = name;
    input.checked = !!checked;
    const span = ui.el('span');
    span.textContent = labelText;
    label.append(input, span);
    form.appendChild(label);
    return input;
  }

  function payloadFromForm(inputs) {
    const platforms = inputs.platforms.value.split(',').map((item) => item.trim()).filter(Boolean);
    let frontmatter = {};
    if (inputs.frontmatter.value.trim()) {
      frontmatter = JSON.parse(inputs.frontmatter.value);
    }
    return {
      name: inputs.name.value.trim(),
      relative_path: inputs.relativePath.value.trim(),
      description: inputs.description.value.trim(),
      platforms,
      enabled: inputs.enabled.checked,
      readiness: inputs.readiness.value,
      frontmatter,
    };
  }

  function openForm(skill) {
    closeForm();
    const isEdit = !!skill;
    const backdrop = ui.el('div', 'modal-backdrop');
    backdrop.id = 'skill-form-modal';
    const dialog = ui.el('section', 'modal-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const form = ui.el('form', 'providers-form');
    const header = ui.el('div', 'modal-header');
    const title = ui.el('h4');
    title.textContent = isEdit ? '编辑 Skill' : '新增 Skill';
    const close = ui.el('button', 'modal-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭 Skill 表单');
    close.textContent = '×';
    close.addEventListener('click', closeForm);
    header.append(title, close);
    form.appendChild(header);

    const inputs = {
      name: field(form, 'name', '名称', skill ? skill.name : '', { disabled: isEdit, placeholder: '唯一名称' }),
      relativePath: field(form, 'relative_path', '相对路径', skill ? skill.relative_path : '', { placeholder: 'example/SKILL.md' }),
      description: field(form, 'description', '描述', skill ? skill.description : ''),
      platforms: field(form, 'platforms', '平台', skill && Array.isArray(skill.platforms) ? skill.platforms.join(',') : '', { placeholder: 'linux,darwin' }),
      readiness: field(form, 'readiness', '就绪状态', skill ? skill.readiness : 'available', { type: 'select', choices: ['available', 'unsupported', 'setup_needed', 'scan_error'] }),
      enabled: checkbox(form, 'enabled', '启用状态', skill ? skill.enabled : true),
      chatSelectable: checkbox(form, 'chatSelectable', '在对话框技能选择器中显示', isEdit ? (skill.chat_selectable !== false) : true),
      frontmatter: field(form, 'frontmatter', 'Frontmatter JSON', skill && skill.frontmatter ? JSON.stringify(skill.frontmatter, null, 2) : '{}', { type: 'textarea', rows: 8 }),
    };
    const actions = ui.el('div', 'providers-form__actions');
    const cancel = ui.el('button', 'btn');
    cancel.type = 'button';
    cancel.textContent = '取消';
    cancel.addEventListener('click', closeForm);
    const save = ui.el('button', 'btn btn--primary');
    save.type = 'submit';
    save.textContent = '保存';
    actions.append(cancel, save);
    form.appendChild(actions);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      try {
        const payload = payloadFromForm(inputs);
        if (isEdit) {
          // 第一步：完整 Skill 更新（不含 chat_selectable）
          await api.updateSkill(skill.name, payload);
          // 第二步：仅在值变化时提交 chat_selectable 单字段 PATCH。
          // 第二步失败不回滚第一步（与 spec 中"两步独立、失败不联动"约定一致）。
          const prev = skill.chat_selectable !== false;
          const nowVal = inputs.chatSelectable.checked;
          if (prev !== nowVal) {
            try {
              await api.setSkillChatSelectable(skill.name, nowVal);
            } catch (err) {
              await modal.alert('对话可见保存失败: ' + (err && err.message ? err.message : err));
              return;
            }
          }
        } else {
          await api.createSkill(payload);
        }
        closeForm();
        await load();
      } catch (err) {
        await modal.alert('保存失败: ' + (err && err.message ? err.message : err));
      }
    });
    dialog.appendChild(form);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeForm();
    });
    document.body.appendChild(backdrop);
    inputs.name.focus();
  }

  async function openDetail(name) {
    let data;
    try {
      data = await api.getSkill(name);
    } catch (err) {
      await modal.alert('获取详情失败: ' + (err && err.message ? err.message : err));
      return;
    }
    closeDetail();
    const backdrop = ui.el('div', 'modal-backdrop');
    backdrop.id = 'skill-detail-modal';
    backdrop.setAttribute('role', 'presentation');

    const dialog = ui.el('section', 'modal-dialog tools-schema-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'skill-detail-title');

    const content = ui.el('div', 'tools-schema-content');
    const header = ui.el('div', 'modal-header');
    const title = ui.el('h4');
    title.id = 'skill-detail-title';
    title.textContent = (data && data.skill && data.skill.name) || name;
    const close = ui.el('button', 'modal-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭 Skill 详情弹出框');
    close.textContent = '×';
    close.addEventListener('click', closeDetail);
    header.append(title, close);

    const pre = ui.el('pre');
    pre.textContent = (data && data.content) || '';
    content.append(header, pre);
    dialog.appendChild(content);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeDetail();
    });
    document.body.appendChild(backdrop);
    close.focus();
  }

  async function openDiffModal(pendingId) {
    let data;
    try {
      data = await api.getPendingDiff(pendingId);
    } catch (err) {
      await modal.alert('获取 diff 失败: ' + (err && err.message ? err.message : err));
      return;
    }
    closeDiffModal();
    const backdrop = ui.el('div', 'modal-backdrop');
    backdrop.id = 'skill-diff-modal';
    backdrop.setAttribute('role', 'presentation');

    const dialog = ui.el('section', 'modal-dialog tools-schema-dialog');
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    dialog.setAttribute('aria-labelledby', 'skill-diff-title');

    const content = ui.el('div', 'tools-schema-content');
    const header = ui.el('div', 'modal-header');
    const title = ui.el('h4');
    title.id = 'skill-diff-title';
    title.textContent = 'Diff - ' + pendingId;
    const close = ui.el('button', 'modal-close');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭 diff 弹出框');
    close.textContent = '×';
    close.addEventListener('click', closeDiffModal);
    header.append(title, close);

    const summaryDiv = ui.el('div', 'skill-diff-summary');
    summaryDiv.textContent = (data && data.summary) || '';

    const pre = ui.el('pre');
    pre.textContent = (data && data.diff) || '';
    content.append(header, summaryDiv, pre);
    dialog.appendChild(content);
    backdrop.appendChild(dialog);
    backdrop.addEventListener('click', (event) => {
      if (event.target === backdrop) closeDiffModal();
    });
    document.body.appendChild(backdrop);
    close.focus();
  }

  async function approvePendingItem(pendingId) {
    try {
      const result = await api.approvePending(pendingId);
      if (result && result.error) {
        await modal.alert('审批失败: ' + result.error);
      }
    } catch (err) {
      await modal.alert('审批失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  async function rejectPendingItem(pendingId) {
    if (!(await modal.confirm('拒绝该待审批写入 ' + pendingId + '？'))) return;
    try {
      await api.rejectPending(pendingId);
    } catch (err) {
      await modal.alert('拒绝失败: ' + (err && err.message ? err.message : err));
    }
    await load();
  }

  async function load() {
    const node = root();
    if (!node) return;
    node.replaceChildren();
    const loading = ui.el('div');
    ui.renderLoading(loading);
    node.appendChild(loading);
    try {
      const tasks = [api.listSkills()];
      if (api.listPendingSkills) tasks.push(api.listPendingSkills());
      if (api.listSkillUsage) tasks.push(api.listSkillUsage());
      const results = await Promise.all(tasks);
      const data = results[0];
      skills = (data && Array.isArray(data.skills)) ? data.skills : [];

      pendingItems = [];
      if (results.length > 1 && results[1] && Array.isArray(results[1].pending)) {
        pendingItems = results[1].pending;
      }

      usageMap = {};
      if (results.length > 2 && results[2] && Array.isArray(results[2].usage)) {
        results[2].usage.forEach((u) => {
          if (u && u.name) usageMap[u.name] = u;
        });
      }

      render();
    } catch (err) {
      node.replaceChildren();
      ui.renderError(node, '加载 Skill 失败: ' + (err && err.message ? err.message : err));
    }
  }

  namespace.skills = { init: load, refresh: load, load: load };
  global.NAGENT = namespace;
}(window));
