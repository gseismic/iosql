/* =====================================================================
 * iosql Web · app.js
 * SPA 外壳：启动引导（/api/db + /api/tables）→ 顶栏（URI/模式徽章）
 * → 左侧栏（总览 + 表列表）→ Tab 栏与 hash 路由 → 全局快捷键 → 状态栏。
 *
 * PLAN-064 重构（设计：docs/design/ui-20260928-redesign.md）：
 * - 6 Tab → 5 Tab：Overview | Data | Structure | Charts | Maintain
 *   （Browse Data 与 Query 合并为 Data，旧 hash 路由 browse/query 兼容映射）
 * - Overview 升级为驾驶舱：表卡片 + 分区分布提示（由 /segments 聚合）
 * - 快捷键提示从顶栏移至底部状态栏
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var TABS = [
    { id: 'overview', label: 'Overview' },
    { id: 'data', label: 'Data' },
    { id: 'structure', label: 'Structure' },
    { id: 'charts', label: 'Charts' },
    { id: 'maintain', label: 'Maintain' }
  ];
  var TAB_IDS = TABS.map(function (t) { return t.id; });
  var LAST_TAB_KEY = 'iosql_ui_last_tab';
  /* 旧版 hash 兼容：browse/query 已合并为 data */
  var LEGACY_TAB = { browse: 'data', query: 'data' };

  var state = {
    db: null,          // GET /api/db 结果
    tables: [],        // GET /api/tables 数组（轻量契约，无统计字段）
    stats: Object.create(null), // 表名 → /stats 结果（表名可能与对象原型同名）
    parts: Object.create(null), // 表名 → /segments 按分区聚合的行数分布
    mode: 'readonly',  // 'readonly' | 'write'
    table: null,       // 当前表名；null = 总览
    tab: 'overview',
    specCache: Object.create(null) // 表名 → spec
  };
  var currentModule = null;

  /* ---------------- 启动 ---------------- */
  function boot() {
    var content = document.getElementById('content');
    IU.clear(content);
    content.append(h('div', { class: 'boot-screen' }, h('span', { class: 'spinner' }), '正在连接数据库…'));

    Promise.all([IU.api('/api/db'), IU.api('/api/tables')]).then(function (results) {
      state.db = results[0] || {};
      state.tables = IU.toArray(results[1]);
      state.mode = String(IU.pick(state.db, 'mode') || 'readonly').toLowerCase() === 'write' ? 'write' : 'readonly';
      applyFontScale(loadFontScale());
      renderTopbar();
      renderSidebar();
      renderTabbar();
      route();
    }).catch(function () {
      IU.clear(content);
      content.append(h('div', { class: 'boot-screen' },
        h('div', { style: { fontSize: '30px' } }, '⚠'),
        h('div', null, '无法连接数据库服务或读取表清单，请确认服务是否在线。'),
        h('button', { class: 'btn primary', onclick: boot }, '重试')));
    });
  }

  /* ---------------- 按需统计 ----------------
   * 用户请求全部统计时限制并发，避免大库瞬间打满连接预算。 */
  function pumpStats() {
    var pending = [];
    state.tables.forEach(function (t) {
      var name = String(IU.pick(t, 'name', 'table'));
      if (!state.stats[name] || state.stats[name].error) pending.push(name);
    });
    var idx = 0;
    function worker() {
      if (idx >= pending.length) return;
      var name = pending[idx++];
      loadStats(name).then(worker);
    }
    for (var i = 0; i < 4 && i < pending.length; i++) worker();
  }

  function loadStats(name) {
    var current = state.stats[name];
    if (current && current.loading) return current.promise;
    if (current && !current.error) return Promise.resolve(current);
    state.stats[name] = { loading: true };
    refreshStatsViews();
    var promise = IU.api('/api/table/' + encodeURIComponent(name) + '/stats').then(function (resp) {
      if (!resp || ['row_count', 'part_count', 'segment_count'].some(function (key) {
        return typeof resp[key] !== 'number' || !Number.isFinite(resp[key]) || resp[key] < 0;
      })) {
        state.stats[name] = { error: true };
        refreshStatsViews();
        return;
      }
      state.stats[name] = resp;
      refreshStatsViews();
      return resp;
    }, function () {
      state.stats[name] = { error: true };
      refreshStatsViews();
    });
    state.stats[name].promise = promise;
    return promise;
  }

  /* ---------------- 按需分区行数分布 ----------------
   * /parts 只有 key 没有行数；/segments 带 row_count + part_key，
   * 共享在途请求和结果，供 Overview 与 Structure 使用。 */
  function pumpParts(name, refresh) {
    var current = state.parts[name];
    if (current && current.loading) return current.promise;
    if (current && !current.error && !refresh) return Promise.resolve(current.segments);
    state.parts[name] = { loading: true };
    refreshStatsViews();
    var promise = IU.api('/api/table/' + encodeURIComponent(name) + '/segments').then(function (resp) {
      var arr = IU.toArray(resp);
      var byPart = Object.create(null);
      var segCount = Object.create(null);
      var order = [];
      var timeMin = null, timeMax = null, timeType = null, completeTime = arr.length > 0;
      arr.forEach(function (sg) {
        if (!sg || typeof sg !== 'object') { completeTime = false; return; }
        var lo = IU.pick(sg, 'time_min');
        var hi = IU.pick(sg, 'time_max');
        var valueType = typeof lo;
        if ((valueType !== 'number' && valueType !== 'string') || typeof hi !== valueType ||
            (valueType === 'number' && (!Number.isFinite(lo) || !Number.isFinite(hi))) ||
            (timeType !== null && timeType !== valueType) || lo > hi) {
          completeTime = false;
        } else {
          timeType = valueType;
          if (timeMin === null || lo < timeMin) timeMin = lo;
          if (timeMax === null || hi > timeMax) timeMax = hi;
        }
        var key = IU.pick(sg, 'partition', 'partition_key', 'logical_part', 'part_key', 'logical');
        if (key === undefined || key === null) return;
        key = String(key);
        var rows = Number(IU.pick(sg, 'rows', 'row_count'));
        if (!Number.isFinite(rows)) rows = 0;
        if (byPart[key] === undefined) { byPart[key] = 0; order.push(key); }
        byPart[key] += rows;
        segCount[key] = (segCount[key] || 0) + 1;
      });
      state.parts[name] = { byPart: byPart, order: order, segCount: segCount,
        timeMin: completeTime ? timeMin : null, timeMax: completeTime ? timeMax : null,
        segments: arr };
      refreshStatsViews();
      return arr;
    }).catch(function (err) {
      state.parts[name] = { error: true };
      refreshStatsViews();
      throw err;
    });
    state.parts[name].promise = promise;
    return promise;
  }

  function statsSummary() {
    var summary = { sum: 0, done: 0, pending: 0, loading: 0, failed: 0 };
    state.tables.forEach(function (t) {
      var s = state.stats[String(IU.pick(t, 'name', 'table'))];
      if (!s) summary.pending++;
      else if (s.loading) { summary.pending++; summary.loading++; }
      else if (s.error || typeof s.row_count !== 'number') summary.failed++;
      else { summary.sum += s.row_count; summary.done++; }
    });
    return summary;
  }

  /* stats/parts 到达后就地更新所有标注单元格（侧栏 + Overview），不整页重渲染 */
  function refreshStatsViews() {
    document.querySelectorAll('[data-stats-for]').forEach(function (el) {
      var s = state.stats[el.dataset.statsFor];
      if (!s || s.loading) {
        el.textContent = s ? '…' : '—';
        el.title = s ? '统计中' : '尚未统计';
        return;
      }
      var key = { rows: 'row_count', parts: 'part_count', segs: 'segment_count' }[el.dataset.kind];
      var v = s[key];
      if (v === undefined) { el.textContent = '—'; el.title = '统计失败'; return; }
      el.textContent = IU.fmt.int(v) + (el.dataset.kind === 'rows' ? ' 行' : '');
      el.removeAttribute('title'); // 清除"统计中"占位标记
    });
    var totalEl = document.getElementById('stat-total-rows');
    if (totalEl) {
      var total = statsSummary();
      totalEl.textContent = total.pending || total.failed ? '—' : IU.fmt.int(total.sum);
      var hint = document.getElementById('stat-total-hint');
      if (hint) hint.textContent = total.pending || total.failed
        ? '（已统计 ' + total.done + '/' + state.tables.length + ' 张' +
          (total.failed ? '，' + total.failed + ' 张失败' : '') +
          (total.loading ? '，统计中' : '') + '）'
        : '';
      var allButton = document.getElementById('stats-all-button');
      if (allButton) allButton.disabled = total.loading > 0 || (!total.pending && !total.failed);
    }
    /* Overview 卡片的分区分布信号（data-part-signal 标注） */
    document.querySelectorAll('[data-part-signal]').forEach(function (el) {
      var name = el.dataset.partSignal;
      var dist = state.parts[name];
      var fresh = IU.overviewPartSignal ? IU.overviewPartSignal(name, dist, state.stats[name]) : null;
      if (fresh) el.replaceWith(fresh);
    });
    /* Overview 卡片的时间范围（来自 segment 时间统计） */
    document.querySelectorAll('[data-part-range]').forEach(function (el) {
      var dist = state.parts[el.dataset.partRange];
      var range = IU.overviewTimeRange ? IU.overviewTimeRange(dist) : null;
      el.textContent = range || '—';
      el.className = range ? 'v' : 'faint';
    });
    document.querySelectorAll('[data-overview-actions]').forEach(function (el) {
      var name = el.dataset.overviewActions;
      if (IU.renderOverviewActions) IU.renderOverviewActions(el, name, state.stats[name], state.parts[name],
        loadStats, pumpParts);
    });
    renderStatusbar();
  }

  /* ---------------- 字号缩放（PLAN-068 P2） ----------------
 * 三档 body zoom：1 / 1.1 / 1.2；localStorage 持久化，boot 时应用。 */
var FS_KEY = 'iosql_web_font_scale';
var FS_LEVELS = [
  { label: 'A−', zoom: 1 },
  { label: '标准', zoom: 1.1 },
  { label: 'A+', zoom: 1.2 }
];

function loadFontScale() {
  try {
    var v = parseFloat(localStorage.getItem(FS_KEY));
    if (v === 1 || v === 1.1 || v === 1.2) return v;
  } catch (e) { /* 隐私模式等 */ }
  return 1.1; // 默认档：比初版大一号
}

function applyFontScale(zoom) {
  // 经 CSS 变量作用于 body zoom；#layout/#table-wrap 的 vh 高度按变量
  // 除回原始值，避免 zoom 放大 100vh 把状态栏挤出视口
  document.documentElement.style.setProperty('--fs-zoom', String(zoom));
}

function renderFontScale() {
  var slot = document.getElementById('font-scale-slot');
  if (!slot) return;
  var current = loadFontScale();
  IU.clear(slot);
  slot.append(h('span', { class: 'faint' }, '字号'));
  FS_LEVELS.forEach(function (lv) {
    slot.append(h('button', {
      class: current === lv.zoom ? 'on' : '',
      title: '界面缩放 ' + Math.round(lv.zoom * 100) + '%',
      onclick: function () {
        try { localStorage.setItem(FS_KEY, String(lv.zoom)); } catch (e) { /* 忽略 */ }
        applyFontScale(lv.zoom);
        renderFontScale();
      }
    }, lv.label));
  });
}

/* ---------------- 顶栏 ---------------- */
  function renderTopbar() {
    var uri = String(IU.pick(state.db, 'uri', 'database_uri', 'source', 'path') || '(未知)');
    var uriEl = document.getElementById('db-uri');
    uriEl.textContent = uri;
    uriEl.title = uri;

    var slot = document.getElementById('mode-badge-slot');
    IU.clear(slot);
    slot.append(state.mode === 'write'
      ? h('span', { class: 'mode-badge write', title: '可修改模式：写端点已注册，危险操作有确认防呆' },
          h('span', { class: 'dot' }), 'WRITABLE')
      : h('span', { class: 'mode-badge readonly', title: '只读模式：服务端未注册任何写端点' },
          h('span', { class: 'dot' }), 'READ-ONLY'));
  }

  /* ---------------- 底部状态栏（PLAN-064 P10） ---------------- */
  function renderStatusbar() {
    var el = document.getElementById('status-left');
    if (!el) return;
    var total = statsSummary();
    var left = state.tables.length + ' 张表 · ' + (total.pending || total.failed
      ? '已统计 ' + total.done + '/' + state.tables.length + ' 张' +
        (total.failed ? '，' + total.failed + ' 张失败' : '') + (total.loading ? '，统计中' : '')
      : IU.fmt.int(total.sum) + ' 行');
    if (state.table) {
      var s3 = state.stats[state.table];
      left = state.table +
        (s3 && typeof s3.row_count === 'number' ? ' · ' + IU.fmt.int(s3.row_count) + ' 行' : '') +
        ' · ' + left;
    }
    el.textContent = left;
    renderFontScale();
  }

  /* ---------------- 左侧栏 ---------------- */
  function lastTableTab() {
    var v = '';
    try { v = localStorage.getItem(LAST_TAB_KEY) || ''; } catch (e) { /* 隐私模式等 */ }
    return TAB_IDS.indexOf(v) > 0 ? v : 'data'; // 下标 0 是 overview，不算表级 Tab
  }

  function renderSidebar() {
    var nav = document.getElementById('sidebar');
    IU.clear(nav);

    nav.append(h('a', {
      class: 'side-item overview-item', href: '#/overview', 'data-nav': 'overview'
    },
      h('div', { class: 'side-name' }, h('span', { class: 'glyph' }, '▦'), '总览'),
      h('div', { class: 'side-meta' }, h('span', { class: 'side-part' }, '库级概览'))));

    nav.append(h('div', { class: 'side-section-label micro-label' }, '表 (' + state.tables.length + ')'));

    if (!state.tables.length) {
      nav.append(h('div', { style: { padding: '8px 10px', color: 'var(--tx2)', fontSize: '13px' } }, '暂无表'));
    }
    state.tables.forEach(function (t) {
      var name = String(IU.pick(t, 'name', 'table'));
      var pstr = IU.pick(t, 'partition_str', 'partition');
      var s = state.stats[name];
      // 未请求或失败显示 —，在途显示 …，成功显示行数。
      var rowsText = s && s.error
        ? '—'
        : (s && typeof s.row_count === 'number'
          ? IU.fmt.int(s.row_count) + ' 行' : (s && s.loading ? '…' : '—'));
      nav.append(h('a', {
        class: 'side-item', href: '#/table/' + encodeURIComponent(name) + '/' + lastTableTab(),
        'data-nav': 'table:' + name
      },
        h('div', { class: 'side-name' }, h('span', { class: 'glyph' }, '▸'),
          h('span', { class: 'side-table-name', title: name }, name)),
        h('div', { class: 'side-meta' },
          h('span', {
            class: 'side-rows',
            title: !s ? '尚未统计' : (s.error ? '统计失败' : (s.loading ? '行数统计中' : null)),
            dataset: { statsFor: name, kind: 'rows' }
          }, rowsText),
          pstr !== undefined ? h('span', { class: 'side-part', title: String(pstr) }, String(pstr)) : null)));
    });
  }

  /* ---------------- Tab 栏 ---------------- */
  function renderTabbar() {
    var bar = document.getElementById('tabbar');
    IU.clear(bar);
    TABS.forEach(function (t, i) {
      var readonlyMaintain = t.id === 'maintain' && state.mode !== 'write';
      bar.append(h('button', {
        class: 'tab-btn', role: 'tab', 'data-tab': t.id,
        id: 'tab-' + t.id, 'aria-controls': 'content',
        title: readonlyMaintain ? '查看如何开启维护模式' : null,
        onclick: function () { switchTab(t.id); },
        onkeydown: function (e) {
          var next = e.key === 'ArrowRight' ? (i + 1) % TABS.length
            : e.key === 'ArrowLeft' ? (i + TABS.length - 1) % TABS.length
            : e.key === 'Home' ? 0 : e.key === 'End' ? TABS.length - 1 : -1;
          if (next < 0) return;
          e.preventDefault(); e.stopPropagation();
          switchTab(TABS[next].id);
          document.getElementById('tab-' + TABS[next].id).focus();
        }
      },
        h('span', { class: 'tab-num' }, String(i + 1)),
        t.label));
    });
  }

  function updateActiveUi() {
    document.querySelectorAll('#tabbar .tab-btn').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === state.tab);
      b.setAttribute('aria-selected', b.dataset.tab === state.tab ? 'true' : 'false');
    });
    var activeTab = document.getElementById('tab-' + state.tab);
    if (activeTab) activeTab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    document.getElementById('content').setAttribute('aria-labelledby', 'tab-' + state.tab);
    document.querySelectorAll('#sidebar .side-item').forEach(function (a) {
      var key = a.dataset.nav;
      var active = state.table === null ? key === 'overview' : key === 'table:' + state.table;
      a.classList.toggle('active', active);
      if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
  }

  /* ---------------- 路由 ---------------- */
  function switchTab(tabId) {
    if (TAB_IDS.indexOf(tabId) < 0) return;
    if (tabId === 'overview') {
      location.hash = '#/overview';
      return;
    }
    // 表级 Tab：无当前表时自动选第一张表
    if (!state.table) {
      var first = state.tables[0];
      if (!first) { IU.toast('库中没有表', 'warn'); return; }
      state.table = String(IU.pick(first, 'name', 'table'));
    }
    try { localStorage.setItem(LAST_TAB_KEY, tabId); } catch (e) { /* 忽略 */ }
    location.hash = '#/table/' + encodeURIComponent(state.table) + '/' + tabId;
  }

  function parseHash() {
    var hash = location.hash || '#/overview';
    var m = hash.match(/^#\/table\/([^/]+)(?:\/([A-Za-z]+))?/);
    if (m) {
      var tab = m[2] || lastTableTab();
      if (LEGACY_TAB[tab]) tab = LEGACY_TAB[tab]; // 旧书签 browse/query → data
      return { table: decodeURIComponent(m[1]), tab: tab };
    }
    return { table: null, tab: 'overview' };
  }

  function route() {
    var r = parseHash();
    state.table = r.table;
    state.tab = TAB_IDS.indexOf(r.tab) >= 0 ? r.tab : 'overview';
    if (state.tab === 'overview') state.table = null;
    if (state.table !== null && TAB_IDS.indexOf(state.tab) > 0) {
      try { localStorage.setItem(LAST_TAB_KEY, state.tab); } catch (e) { /* 忽略 */ }
    }
    updateActiveUi();
    renderContent();
    renderStatusbar();
  }

  /* ---------------- 工作区渲染 ---------------- */
  function cleanupCurrentModule() {
    if (currentModule && typeof currentModule.cleanup === 'function') currentModule.cleanup();
    currentModule = null;
  }

  function renderContent() {
    var content = document.getElementById('content');
    cleanupCurrentModule();
    IU.browseNav = null;
    IU.refreshData = null;
    IU.clear(content);

    // 总览：无需 spec
    if (state.tab === 'overview') {
      safeRender(IU.tabs.overview, content, {
        mode: state.mode, tables: state.tables, table: null, spec: null,
        stats: state.stats, parts: state.parts, requestAllStats: pumpStats
      });
      refreshStatsViews();
      return;
    }

    // 表级 Tab：懒加载并缓存 spec
    var spec = state.specCache[state.table];
    if (spec) {
      safeRender(IU.tabs[state.tab], content, ctxOf(spec));
      return;
    }
    content.append(h('div', { class: 'boot-screen' }, h('span', { class: 'spinner' }), '加载表契约…'));
    var requestedTable = state.table;
    IU.api('/api/table/' + encodeURIComponent(requestedTable) + '/spec').then(function (specResp) {
      var requestedSpec = (specResp && specResp.spec) || {};
      if (!requestedSpec.columns && specResp && specResp.columns) requestedSpec.columns = specResp.columns;
      state.specCache[requestedTable] = requestedSpec;
      if (state.table === requestedTable && parseHash().table === requestedTable) {
        IU.clear(content);
        safeRender(IU.tabs[state.tab], content, ctxOf(requestedSpec));
      }
    }).catch(function () {
      if (state.table !== requestedTable || parseHash().table !== requestedTable) return;
      IU.clear(content);
      content.append(h('div', { class: 'empty-state' },
        '表契约加载失败：', h('span', { class: 'mono' }, requestedTable),
        h('div', { style: { marginTop: '10px' } },
          h('button', { class: 'btn', onclick: renderContent }, '重试'))));
    });

    function ctxOf(spec) {
      return { mode: state.mode, tables: state.tables, table: state.table, spec: spec,
        stats: state.stats, parts: state.parts, requestParts: pumpParts };
    }
  }

  function safeRender(mod, root, ctx) {
    if (!mod || typeof mod.render !== 'function') {
      root.append(h('div', { class: 'empty-state' }, '该 Tab 未实现'));
      return;
    }
    try {
      cleanupCurrentModule();
      currentModule = mod;
      mod.render(root, ctx);
    } catch (e) {
      root.append(h('div', { class: 'empty-state' }, '渲染失败：' + e.message));
    }
  }

  /* ---------------- 全局快捷键 ---------------- */
  IU.globalKeys(function (e) {
    // 数字 1-5 切 Tab
    if (e.key >= '1' && e.key <= '5') {
      switchTab(TABS[Number(e.key) - 1].id);
      return;
    }
    // R 刷新当前工作区
    if (e.key === 'r' || e.key === 'R') {
      if (state.tab === 'overview') location.reload();
      else if (state.tab === 'data' && typeof IU.refreshData === 'function') IU.refreshData();
      else renderContent();
      return;
    }
    // ←/→ 仅在 Data 浏览视图生效
    if (state.tab === 'data' && typeof IU.browseNav === 'function') {
      if (e.key === 'ArrowRight') { IU.browseNav(1); e.preventDefault(); }
      else if (e.key === 'ArrowLeft') { IU.browseNav(-1); e.preventDefault(); }
    }
  });

  /* 窄屏侧栏抽屉；桌面保持常驻。 */
  var sidebarToggle = document.getElementById('sidebar-toggle');
  function setSidebarOpen(open, restoreFocus) {
    document.body.classList.toggle('sidebar-open', open);
    sidebarToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    sidebarToggle.setAttribute('aria-label', open ? '关闭表列表' : '打开表列表');
    if (restoreFocus) sidebarToggle.focus();
  }
  sidebarToggle.addEventListener('click', function () {
    var open = !document.body.classList.contains('sidebar-open');
    setSidebarOpen(open, false);
    if (open) {
      var firstLink = document.querySelector('#sidebar a');
      if (firstLink) firstLink.focus();
    }
  });
  document.getElementById('sidebar-backdrop').addEventListener('click', function () { setSidebarOpen(false, true); });
  document.getElementById('sidebar').addEventListener('click', function (e) {
    if (e.target.closest('a') && document.body.classList.contains('sidebar-open')) setSidebarOpen(false, true);
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && document.body.classList.contains('sidebar-open')) setSidebarOpen(false, true);
  });
  window.addEventListener('resize', function () {
    if (window.innerWidth > 800 && document.body.classList.contains('sidebar-open')) setSidebarOpen(false, false);
  });

  window.addEventListener('hashchange', route);

  boot();
})();
