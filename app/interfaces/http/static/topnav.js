(function (global) {
  'use strict';
  // TopNav: 子域导航组件。仅负责 DOM 结构、高亮、溢出控制与生命周期。
  // 不解析 URL、不写 history；点击链接通过 onActivate(item) 回调交由调用方导航。
  const namespace = global.NAGENT || {};

  let instance = null;

  function prefersReducedMotion() {
    try {
      if (global.matchMedia) {
        return !!global.matchMedia('(prefers-reduced-motion: reduce)').matches;
      }
    } catch (_) { /* matchMedia 不可用时回落默认 */ }
    return false;
  }

  // 开发夹具报告配置错误；生产环境（无 __DEV__）静默降级，不抛错。
  function devReport(msg) {
    if (!namespace.__DEV__) return;
    try {
      if (typeof console !== 'undefined' && console.error) {
        console.error('[topnav] ' + msg);
      }
    } catch (_) { /* console 不可用时忽略 */ }
  }

  function render(container, opts) {
    if (!container) return;
    // 重复 render 先销毁旧实例，避免重复监听器
    destroy();

    opts = opts || {};
    const rawItems = Array.isArray(opts.items) ? opts.items : [];
    const activeTab = opts.activeTab;
    const onActivate = typeof opts.onActivate === 'function' ? opts.onActivate : null;
    const reduceMotion = prefersReducedMotion();

    // 校验 items：缺字段的安全降级（跳过非法项），开发夹具报告
    const items = [];
    for (let i = 0; i < rawItems.length; i++) {
      const it = rawItems[i];
      if (!it || typeof it !== 'object') {
        devReport('item #' + i + ' is not an object, skipped');
        continue;
      }
      if (typeof it.tab !== 'string' || !it.tab ||
          typeof it.path !== 'string' || !it.path ||
          typeof it.label !== 'string') {
        devReport('item "' + (it.tab || ('#' + i)) + '" missing required fields (tab/path/label), skipped');
        continue;
      }
      items.push(it);
    }

    // 构造 DOM：单个 nav + a 项，不得在既有 nav 内再嵌套 nav
    container.replaceChildren();
    const nav = document.createElement('nav');
    nav.setAttribute('aria-label', '子域导航');
    nav.classList.add('topnav');

    const leftBtn = document.createElement('button');
    leftBtn.setAttribute('type', 'button');
    leftBtn.setAttribute('aria-label', '向左滚动');
    leftBtn.classList.add('topnav__ctrl', 'topnav__ctrl--left');
    leftBtn.hidden = true;
    leftBtn.setAttribute('tabindex', '-1');

    const scroll = document.createElement('div');
    scroll.classList.add('topnav__scroll');

    const inner = document.createElement('div');
    inner.classList.add('topnav__inner');

    let activeFound = false;
    const linkEls = [];
    items.forEach(function (it) {
      const a = document.createElement('a');
      // 视觉截断 class（CSS 实现截断），同时保留完整可访问名称
      a.classList.add('topnav__item', 'topnav__item--truncate');
      a.setAttribute('href', it.path);
      a.textContent = it.label; // 安全文本渲染，禁 innerHTML
      if (!activeFound && it.tab === activeTab) {
        a.setAttribute('aria-current', 'page');
        a.classList.add('topnav__item--active'); // 主色+加粗，无 border-bottom
        activeFound = true;
      }
      a.addEventListener('click', function (event) {
        if (event && typeof event.preventDefault === 'function') event.preventDefault();
        if (onActivate) onActivate(it); // 不直接 pushState，交由调用方
      });
      inner.appendChild(a);
      linkEls.push(a);
    });

    // activeTab 不在 items 中 -> 无当前项；开发夹具报告配置错误
    if (activeTab && !activeFound) {
      devReport('activeTab "' + activeTab + '" not found in items; rendered with no current item');
    }

    const rightBtn = document.createElement('button');
    rightBtn.setAttribute('type', 'button');
    rightBtn.setAttribute('aria-label', '向右滚动');
    rightBtn.classList.add('topnav__ctrl', 'topnav__ctrl--right');
    rightBtn.hidden = true;
    rightBtn.setAttribute('tabindex', '-1');

    scroll.appendChild(inner);
    nav.appendChild(leftBtn);
    nav.appendChild(scroll);
    nav.appendChild(rightBtn);
    container.appendChild(nav);

    const inst = { disposed: false, container: container };
    instance = inst;

    let translateX = 0; // 默认动效用平移偏移（<=0）

    function readMetrics() {
      const sw = (typeof scroll.scrollWidth === 'number') ? scroll.scrollWidth : 0;
      const cw = (typeof scroll.clientWidth === 'number') ? scroll.clientWidth : 0;
      return { sw: sw, cw: cw, overflow: sw > cw, maxScroll: Math.max(0, sw - cw) };
    }

    function currentOffset() {
      if (reduceMotion) {
        return (typeof scroll.scrollLeft === 'number') ? scroll.scrollLeft : 0;
      }
      return -translateX;
    }

    function applyOffset(offset) {
      if (reduceMotion) {
        // 减少动态效果偏好：用原生 scrollLeft，不保留平移动画
        try { scroll.scrollLeft = offset; } catch (_) { /* ignore */ }
      } else {
        translateX = -offset;
        inner.style.setProperty('transform', 'translateX(' + (-offset) + 'px)');
      }
    }

    function updateEdges() {
      if (inst.disposed) return;
      const m = readMetrics();
      if (!m.overflow) {
        leftBtn.hidden = true;
        rightBtn.hidden = true;
        leftBtn.setAttribute('tabindex', '-1');
        rightBtn.setAttribute('tabindex', '-1');
        leftBtn.disabled = false;
        rightBtn.disabled = false;
        applyOffset(0);
        return;
      }
      leftBtn.hidden = false;
      rightBtn.hidden = false;
      // 溢出时移除 tabindex=-1，使控制按钮进入 tab 序列
      leftBtn.removeAttribute('tabindex');
      rightBtn.removeAttribute('tabindex');
      const offset = currentOffset();
      leftBtn.disabled = offset <= 0;
      rightBtn.disabled = offset >= m.maxScroll;
    }

    function step(direction) {
      if (inst.disposed) return;
      const m = readMetrics();
      if (!m.overflow) return;
      const stepSize = Math.max(40, Math.floor(m.cw * 0.8));
      let offset = currentOffset();
      offset = direction === 'right' ? offset + stepSize : offset - stepSize;
      if (offset < 0) offset = 0;
      if (offset > m.maxScroll) offset = m.maxScroll;
      applyOffset(offset);
      updateEdges();
    }

    leftBtn.addEventListener('click', function () { step('left'); });
    rightBtn.addEventListener('click', function () { step('right'); });

    // 滚轮转水平滚动 + 重新计算边界
    const wheelHandler = function (event) {
      if (inst.disposed) return;
      const dy = event && (event.deltaY || 0);
      if (!dy) return;
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
      const m = readMetrics();
      if (!m.overflow) return;
      let offset = currentOffset() + dy;
      if (offset < 0) offset = 0;
      if (offset > m.maxScroll) offset = m.maxScroll;
      applyOffset(offset);
      updateEdges();
    };
    scroll.addEventListener('wheel', wheelHandler, { passive: false });

    // 原生触摸/scroll -> 重新计算边界
    const scrollHandler = function () { if (!inst.disposed) updateEdges(); };
    scroll.addEventListener('scroll', scrollHandler);

    // 键盘链接导航（focus）-> 重新计算边界
    const focusHandler = function () { if (!inst.disposed) updateEdges(); };
    inner.addEventListener('focus', focusHandler, true);

    // 方向键在链接间移动焦点 -> 重新计算边界
    const keyHandler = function (event) {
      if (inst.disposed || !event) return;
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      const idx = linkEls.indexOf(event.target);
      if (idx === -1) return;
      if (event && typeof event.preventDefault === 'function') event.preventDefault();
      const next = event.key === 'ArrowRight' ? idx + 1 : idx - 1;
      if (next >= 0 && next < linkEls.length) {
        if (typeof linkEls[next].focus === 'function') linkEls[next].focus();
      }
      updateEdges();
    };
    inner.addEventListener('keydown', keyHandler);

    // 初始边界计算
    updateEdges();

    // 溢出检测：ResizeObserver 优先，不可用时退回单个 window resize 监听
    const hasRO = typeof global.ResizeObserver === 'function';
    if (hasRO) {
      const observer = new global.ResizeObserver(function () { if (!inst.disposed) updateEdges(); });
      observer.observe(scroll);
      observer.observe(inner);
      inst.observer = observer;
    } else if (typeof global.addEventListener === 'function') {
      global.addEventListener('resize', updateEdges);
      inst.windowResizeHandler = updateEdges;
    }

    inst.nav = nav;
    inst.scroll = scroll;
    inst.inner = inner;
    inst.leftBtn = leftBtn;
    inst.rightBtn = rightBtn;
    inst.updateEdges = updateEdges;
  }

  function destroy() {
    if (!instance) return;
    const inst = instance;
    instance = null;
    inst.disposed = true; // 使残留闭包回调不再生效
    if (inst.observer && typeof inst.observer.disconnect === 'function') {
      inst.observer.disconnect();
    }
    if (inst.windowResizeHandler && typeof global.removeEventListener === 'function') {
      global.removeEventListener('resize', inst.windowResizeHandler);
    }
    if (inst.container) {
      try { inst.container.replaceChildren(); } catch (_) { /* ignore */ }
    }
  }

  namespace.topnav = { render: render, destroy: destroy };
  global.NAGENT = namespace;
}(window));
