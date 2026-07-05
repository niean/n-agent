(function (global) {
  const namespace = global.NAGENT || {};
  const ui = namespace.ui;
  const api = namespace.api;

  function statusBadge(status) {
    if (status === 'ok') return 'success';
    if (status === 'error' || (typeof status === 'string' && status.startsWith('error'))) return 'danger';
    if (status === 'warn' || status === 'disabled') return 'warning';
    return 'warning';
  }

  async function refresh() {
    const stats = ui.byId('health-stats');
    const target = ui.byId('health-detail');
    if (!stats || !target) return;
    ui.clear(stats); ui.clear(target);
    ui.renderLoading(target, '加载依赖健康...');
    try {
      const [service, dependencies] = await Promise.all([
        api.getHealth(),
        api.getDependencyHealth(),
      ]);
      ui.clear(target);
      const statCards = [
        { label: 'Service', value: service.status || 'unknown' },
        { label: 'Provider', value: (dependencies.provider || {}).status || 'unknown' },
        { label: 'Memory', value: (dependencies.memory || {}).status || 'unknown' },
        { label: 'Knowledge', value: (dependencies.knowledge || {}).status || 'unknown' },
      ];
      statCards.forEach((s) => {
        const card = document.createElement('div'); card.className = 'stat-card';
        const label = document.createElement('div'); label.className = 'label'; label.textContent = s.label;
        const value = document.createElement('div'); value.className = 'value'; value.textContent = s.value;
        card.append(label, value);
        stats.appendChild(card);
      });
      Object.entries(dependencies).forEach(([name, info]) => {
        const panel = document.createElement('section');
        panel.className = 'status-panel';
        const header = document.createElement('div'); header.className = 'panel-header'; header.textContent = name;
        const body = document.createElement('div'); body.className = 'panel-body';
        if (info && typeof info === 'object') {
          if (info.status) {
            const badge = document.createElement('span');
            badge.className = `badge badge--${statusBadge(info.status)}`;
            badge.textContent = String(info.status);
            body.appendChild(badge);
          }
          Object.entries(info).forEach(([k, v]) => { if (k !== 'status') ui.appendText(body, k + ':', String(v)); });
        } else {
          body.textContent = String(info);
        }
        panel.append(header, body);
        target.appendChild(panel);
      });
    } catch (error) {
      ui.clear(target);
      ui.renderError(target, error.message);
    }
  }

  function init() {
    refresh();
  }

  global.NAGENT = namespace;
  global.NAGENT.status = { init, refresh };
}(window));
