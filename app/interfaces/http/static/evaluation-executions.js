(function (global) {
  'use strict';
  const namespace = global.NAGENT || {};
  const labels = { queued: '排队中', running: '执行中', scoring: '评分中', completed: '执行完成', failed: '执行失败', interrupted: '执行中断' };
  const releaseLabels = {
    not_applicable: '无场景资源', released: '活跃资源已释放', retention_registered: '已登记保留',
    release_failed: '释放失败（可恢复）', release_pending: '待释放', releasing: '释放中',
  };
  const terminal = new Set(['completed', 'failed', 'interrupted']);

  function createController(deps) {
    const { api, el, actionButton, buildFrame, formActions, mountDetailModal, closeBackdrop,
      filterItem, kvRow, table, scrollTable, formatTime, isCurrent, openExperimentModal, blockTitle } = deps;
    let frame = null;
    let timer = null;
    let generation = 0;
    let listGeneration = 0;

    function deactivate() {
      generation++;
      listGeneration++;
      if (timer !== null) global.clearTimeout(timer);
      timer = null;
      if (frame) closeBackdrop(frame.backdrop);
      frame = null;
    }

    function alive(token, seq, ownFrame) {
      return isCurrent(token) && generation === seq && frame === ownFrame
        && (!('isConnected' in ownFrame.backdrop) || ownFrame.backdrop.isConnected);
    }

    function mount(ownFrame) {
      frame = ownFrame;
      ownFrame.closeBtn.addEventListener('click', deactivate);
      mountDetailModal(ownFrame);
    }

    function actionsFor(ownFrame, cancelLabel, submitLabel) {
      const actions = formActions(ownFrame.form, cancelLabel, submitLabel);
      if (actions.children[0]) actions.children[0].addEventListener('click', deactivate);
      return actions;
    }

    function key() {
      return global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID()
        : 'execution-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
    }

    async function openCreate(preselected, token) {
      deactivate();
      const seq = generation;
      let caps, data;
      try {
        [caps, data] = await Promise.all([api.getExecutionCapabilities(), api.listDatasets()]);
      } catch (error) {
        if (!isCurrent(token) || seq !== generation) return;
        const own = buildFrame('发起实验');
        own.form.appendChild(el('div', 'error-state', '加载实验配置失败：' + error.message));
        actionsFor(own, '关闭');
        mount(own);
        return;
      }
      if (!isCurrent(token) || seq !== generation) return;
      const datasets = (data.items || []).filter(item => item.status === 'valid' && item.target_kind === 'agent_run');
      const definitions = (caps.definitions || []).filter(item => item.executable);
      const own = buildFrame('发起实验');
      const name = document.createElement('input');
      name.name = 'name'; name.required = true; name.maxLength = 200;
      name.value = '评估实验 ' + formatTime(new Date().toISOString());
      const dataset = document.createElement('select');
      dataset.name = 'dataset';
      datasets.forEach((item, index) => {
        const option = el('option', '', item.dataset_id + ' / ' + item.dataset_version);
        option.value = String(index); dataset.appendChild(option);
      });
      if (preselected) {
        const index = datasets.findIndex(item => item.dataset_id === preselected.dataset_id && item.dataset_version === preselected.dataset_version);
        if (index >= 0) dataset.value = String(index);
      }
      const hash = document.createElement('input'); hash.type = 'hidden'; hash.name = 'dataset_hash';
      const requestKey = document.createElement('input'); requestKey.type = 'hidden'; requestKey.name = 'request_key';
      const replicates = document.createElement('input');
      replicates.type = 'number'; replicates.name = 'replicates'; replicates.min = '1'; replicates.max = '10'; replicates.step = '1'; replicates.value = '1';
      own.form.append(filterItem('实验名称', name), filterItem('数据集 / 版本', dataset), hash, requestKey);
      const checks = [];
      const choices = el('div', 'policy-list');
      let recommended = definitions.findIndex(item => item.metric_name === 'agent.answer_correctness' && item.judge_model === caps.judge_model);
      if (recommended < 0) recommended = definitions.findIndex(item => item.metric_name === 'agent.answer_correctness' && item.definition_version === 'experiment-v1');
      if (recommended < 0) recommended = definitions.findIndex(item => item.metric_name === 'agent.answer_correctness');
      definitions.forEach((item, index) => {
        const check = document.createElement('input'); check.type = 'checkbox';
        check.checked = index === recommended;
        const label = el('label', 'policy-item');
        label.append(check, el('span', 'policy-v', item.metric_name + ' / ' + item.definition_version));
        choices.appendChild(label); checks.push({ check, item });
      });
      own.form.append(blockTitle('评分器'), choices, filterItem('重复次数', replicates));
      const estimate = el('div', 'evaluations-note');
      const updateEstimate = () => {
        const selected = datasets[Number(dataset.value)];
        hash.value = selected ? selected.dataset_hash : '';
        estimate.textContent = selected ? '预计执行 ' + selected.case_count + ' × ' + replicates.value + ' 项；另有评分模型调用。' : '暂无可执行的有效数据集。';
      };
      dataset.addEventListener('change', updateEstimate);
      replicates.addEventListener('input', updateEstimate);
      updateEstimate();
      own.form.appendChild(estimate);
      // plan T5：只读预检（valid != runnable；无副作用，不登记、不运行）
      const preflightNote = el('div', 'evaluations-note', '尚未预检。');
      const preflightError = el('div', 'error-state');
      preflightError.hidden = true;
      const preflightBtn = actionButton('预检', async () => {
        const selected = datasets[Number(dataset.value)];
        if (!selected) return;
        preflightBtn.disabled = true;
        preflightError.hidden = true;
        preflightNote.textContent = '预检中...';
        try {
          const report = await api.preflightExecution({
            dataset_id: selected.dataset_id, dataset_version: selected.dataset_version,
            dataset_hash: selected.dataset_hash,
            evaluator_set: checks.filter(choice => choice.check.checked).map(choice => ({
              definition_id: choice.item.definition_id, definition_version: choice.item.definition_version,
            })),
            replicates: Number(replicates.value) || 1,
          });
          if (!alive(token, seq, own)) return;
          if (report.runnable) {
            preflightNote.textContent = '预检通过：执行 ' + report.item_count + ' 项（'
              + report.selected_case_ids.length + ' 题 × ' + replicates.value + '），'
              + 'Judge 调用约 ' + report.judge_calls + ' 次。';
          } else {
            preflightNote.textContent = '';
            preflightError.hidden = false;
            preflightError.textContent = '预检未通过：' + (report.blocked || []).join('；');
          }
        } catch (failure) {
          if (!alive(token, seq, own)) return;
          preflightNote.textContent = '';
          preflightError.hidden = false;
          preflightError.textContent = '预检失败：' + failure.message;
        } finally {
          preflightBtn.disabled = false;
        }
      });
      own.form.appendChild(preflightBtn);
      own.form.appendChild(preflightNote);
      own.form.appendChild(preflightError);
      own.form.appendChild(el('div', 'evaluations-note', '单题冒烟结果不代表总体质量。'
        + (caps.self_judge ? '当前使用同一模型生成与评分（self_judge），适合调试回归。' : '')));
      const error = el('div', 'error-state');
      error.hidden = true; own.form.appendChild(error);
      const actions = actionsFor(own, '取消', '启动实验');
      const submit = actions.querySelector('button[type="submit"]');
      const available = !!(caps.ready && datasets.length && definitions.length);
      submit.disabled = !available;
      if (!available) {
        error.hidden = false;
        error.textContent = !caps.ready ? '执行服务或评分 worker 尚未就绪。'
          : !datasets.length ? '暂无有效数据集。' : !caps.judge_ready ? 'Judge 未配置，暂无兼容且就绪的评分器。' : '暂无兼容且就绪的评分器。';
      }
      let inflight = false;
      let submittedBody = null;
      own.form.addEventListener('submit', async event => {
        event.preventDefault();
        if (!available || inflight || !alive(token, seq, own)) return;
        const selected = datasets[Number(dataset.value)];
        const selectedDefinitions = checks.filter(choice => choice.check.checked).map(choice => ({
          definition_id: choice.item.definition_id, definition_version: choice.item.definition_version,
        }));
        const repeat = Number(replicates.value);
        error.hidden = false;
        if (!name.value.trim() || name.value.trim().length > 200 || !selectedDefinitions.length
          || !Number.isInteger(repeat) || repeat < 1 || repeat > 10 || selected.case_count * repeat > 100) {
          error.textContent = '请填写名称、选择评分器，并将重复次数设为 1–10，执行总项数不超过 100。';
          return;
        }
        const body = { name: name.value.trim(), dataset_id: selected.dataset_id, dataset_version: selected.dataset_version,
          dataset_hash: selected.dataset_hash, evaluator_set: selectedDefinitions, replicates: repeat };
        const signature = JSON.stringify(body);
        if (!submittedBody || submittedBody.signature !== signature) submittedBody = { signature, requestKey: key() };
        requestKey.value = submittedBody.requestKey;
        inflight = true; submit.disabled = true; error.hidden = true;
        try {
          const result = await api.createExecution(Object.assign({}, body, { request_key: submittedBody.requestKey }));
          if (!alive(token, seq, own)) return;
          await openDetail(result.execution_id, token);
        } catch (failure) {
          if (!alive(token, seq, own)) return;
          error.hidden = false; error.textContent = '提交失败：' + failure.message + '。输入已保留，可重试。';
        } finally {
          inflight = false;
          if (alive(token, seq, own)) submit.disabled = !available;
        }
      });
      mount(own);
    }

    async function renderList(body, token, cursor) {
      const seq = ++listGeneration;
      body.textContent = '';
      body.appendChild(blockTitle('实验执行'));
      body.appendChild(el('div', 'loading-state', '执行记录加载中...'));
      try {
        const data = await api.listExecutions({ limit: 20, cursor });
        if (!isCurrent(token) || seq !== listGeneration) return;
        body.textContent = '';
        body.appendChild(blockTitle('实验执行'));
        const rows = (data.items || []).map(item => [item.name, item.dataset_id + '/' + item.dataset_version,
          labels[item.phase] || item.phase, formatTime(item.created_at), actionButton('查看执行', () => openDetail(item.execution_id, token))]);
        if (rows.length) body.appendChild(scrollTable(table(['实验名称', '数据集', '执行状态', '创建时间', '操作'], rows)));
        else body.appendChild(el('div', 'empty-state', '暂无执行记录，可通过「发起实验」启动。'));
        // 游标分页与评估结果列表同款：evaluations-pager 行 + 上一页/下一页短文案
        const pager = el('div', 'evaluations-pager');
        if (cursor) pager.appendChild(actionButton('上一页', () => renderList(body, token)));
        if (data.next_cursor) pager.appendChild(actionButton('下一页', () => renderList(body, token, data.next_cursor)));
        if (pager.children.length) body.appendChild(pager);
      } catch (error) {
        if (!isCurrent(token) || seq !== listGeneration) return;
        body.textContent = '';
        body.appendChild(blockTitle('实验执行'));
        body.appendChild(el('div', 'error-state', '执行记录加载失败：' + error.message));
        body.appendChild(actionButton('重试', () => renderList(body, token, cursor)));
      }
    }

    async function openDetail(executionId, token) {
      deactivate();
      const seq = generation;
      const own = buildFrame('实验执行详情');
      const content = el('div', 'policy-list');
      own.form.appendChild(content);
      actionsFor(own, '关闭');
      async function poll(initial) {
        if (!initial && !alive(token, seq, own)) return;
        try {
          const data = await api.getExecution(executionId);
          if (!isCurrent(token) || generation !== seq || (!initial && !alive(token, seq, own))) return;
          content.textContent = '';
          content.append(kvRow('执行ID', data.execution_id), kvRow('状态', labels[data.phase] || data.phase));
          content.appendChild(el('div', 'evaluations-note', '执行完成表示流程已结束，不代表回答质量通过。'));
          if (data.error_code) content.appendChild(el('div', 'error-state', data.error_code));
          const counts = data.quality_counts || {};
          content.append(kvRow('有效评分', counts.value || 0), kvRow('评分异常', counts.error || 0),
            kvRow('跳过评分', counts.skipped || 0), kvRow('待评分', counts.pending || 0));
          const report = data.report || {};
          if (report.cleanup_failures) content.appendChild(kvRow('清理失败', report.cleanup_failures));
          // plan T5：释放/保留账目展示（completed 表示资源已收敛，不是质量通过）
          if (report.release) {
            content.appendChild(kvRow('资源释放', releaseLabels[report.release.state] || report.release.state));
            if (report.release.retained) content.appendChild(kvRow('保留登记', report.release.retained));
            if (report.release.cleanup_failed) content.appendChild(kvRow('释放失败项', report.release.cleanup_failed));
          }
          (report.items || []).forEach(item => {
            content.appendChild(kvRow('用例', item.item_id || item.case_id));
            content.appendChild(kvRow('执行结果', item.outcome));
            if (item.failure_reason || item.skip_reason) content.appendChild(kvRow('原因', item.failure_reason || item.skip_reason));
            if (item.answer) content.appendChild(el('pre', 'evaluations-dataset-content', item.answer));
            if (item.answer_truncated) content.appendChild(el('div', 'evaluations-note', '回答展示已截断，请查看实验结果。'));
            if (item.evidence_unavailable) content.appendChild(el('div', 'evaluations-note', '回答证据已不可用。'));
          });
          // plan T8：比较分组（scope/profile/fixture/binding 不同不混成
          // 同一可比总体；派生题单列；不可比原因随分组展示）
          const groups = data.comparison_groups || {};
          if ((groups.groups || []).length) {
            content.appendChild(blockTitle('比较分组'));
            groups.groups.forEach(group => {
              const dims = [group.scope, group.profile, group.fixture,
                (group.bindings || []).length ? '绑定: ' + group.bindings.join(', ') : null]
                .filter(Boolean).join(' / ');
              content.appendChild(kvRow('分组', dims || '未分组'));
              content.appendChild(kvRow('用例', (group.case_ids || []).join(', ')));
              if ((group.derived_case_ids || []).length) {
                content.appendChild(kvRow('派生用例（单列）', group.derived_case_ids.join(', ')));
              }
              if (group.note) content.appendChild(el('div', 'evaluations-note', group.note));
            });
            if ((groups.reasons || []).length) {
              content.appendChild(el('div', 'evaluations-note', groups.reasons.join('；') + '。'));
            }
          }
          if (data.experiment_id) content.appendChild(actionButton('查看实验结果', () => {
            deactivate(); openExperimentModal(data.experiment_id);
          }));
          if (initial) mount(own);
          if (!terminal.has(data.phase) && alive(token, seq, own)) timer = global.setTimeout(() => poll(false), 2000);
        } catch (error) {
          if (!isCurrent(token) || generation !== seq) return;
          content.appendChild(el('div', 'error-state', '执行详情加载失败：' + error.message));
          content.appendChild(actionButton('重新加载', () => openDetail(executionId, token)));
          if (initial) mount(own);
        }
      }
      await poll(true);
    }

    return { openCreate, openDetail, renderList, deactivate };
  }
  namespace.evaluationExecutions = { createController };
  global.NAGENT = namespace;
}(window));
