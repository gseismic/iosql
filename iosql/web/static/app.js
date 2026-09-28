/* =====================================================================
 * iosql Web · app.js
 * SPA 外壳：启动引导（/api/db + /api/tables）→ 顶栏（URI/模式徽章）
 * → 左侧栏（总览 + 表列表）→ Tab 栏与 hash 路由 → 全局快捷键 → 状态栏。
 *
 * PLAN-064 重构（设计：docs/design/ui-20260928-redesign.md）：
 * - 6 Tab → 5 Tab：Overview | Data | Structure | Charts | Maintain
 *   （Browse Data 与 Query 合并为 Data，旧 hash 路由 browse/query 兼容映射）
 * - Overview 升级为驾驶舱：表卡片 + 健康信号（分区偏斜由 /segments 聚合）
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
    stats: {},         // 表名 → /stats 结果（渐进拉取，PLAN-060）
    parts: {},         // 表名 → /segments 按分区聚合的行数分布（PLAN-064 D2）
    mode: 'readonly',  // 'readonly' | 'write'
    table: null,       // 当前表名；null = 总览
    tab: 'overview',
    specCache: {}      // 表名 → spec
  };

  /* ---------------- 启动 ---------------- */
  function boot() {
    var content = document.getElementById('content');
    IU.clear(content);
    content.append(h('div', { class: 'boot-screen' }, h('span', { class: 'spinner' }), '正在连接数据库…'));

    Promise.all([IU.api('/api/db'), IU.api('/api/tables')]).then(function (results) {
      state.db = results[0] || {};
      state.tables = IU.toArray(results[1]);
      state.mode = String(IU.pick(state.db, 'mode') || 'readonly').toLowerCase() === 'write' ? 'write' : 'readonly';
      renderTopbar();
      renderSidebar();
      renderTabbar();
      route();
      pumpStats(); // 非阻塞：先出界面，行数/分区数异步逐表填充
    }).catch(function () {
      IU.clear(content);
      content.append(h('div', { class: 'boot-screen' },
        h('div', { style: { fontSize: '30px' } }, '⚠'),
        h('div', null, '无法连接后端 API（/api/db）。后端服务可能尚未就绪。'),
        h('button', { class: 'btn primary', onclick: boot }, '重试')));
    });
  }

  /* ---------------- 渐进统计（PLAN-060） ----------------
   * 启动契约轻量化后，行数/分区数/segment 数由前端逐表拉取填充；
   * 并发 4，避免大库上瞬间打满连接预算。 */
  function pumpStats() {
    var pending = [];
    state.tables.forEach(function (t) {
      var name = String(IU.pick(t, 'name', 'table'));
      if (!state.stats[name]) pending.push(name);
    });
    var idx = 0;
    function worker() {
      if (idx >= pending.length) return;
      var name = pending[idx++];
      IU.api('/api/table/' + encodeURIComponent(name) + '/stats').then(
        function (resp) {
          state.stats[name] = resp || {};
          refreshStatsViews();
          pumpParts(name); // 统计到达后再拉分区分布（Overview 偏斜信号）
          worker();
        },
        function () {
          state.stats[name] = { error: true };
          refreshStatsViews();
          worker();
        });
    }
    for (var i = 0; i < 4 && i < pending.length; i++) worker();
  }

  /* ---------------- 分区行数分布泵（PLAN-064 D2/D4） ----------------
   * /parts 只有 key 没有行数；/segments 带 row_count + part_key，
   * 客户端按分区聚合一次即可得到偏斜信号、迷你条形图与每分区 segment 数。 */
  function pumpParts(name) {
    if (state.parts[name]) return;
    IU.api('/api/table/' + encodeURIComponent(name) + '/segments').then(function (resp) {
      var arr = IU.toArray(resp);
      var byPart = {};
      var segCount = {};
      var order = [];
      arr.forEach(function (sg) {
        if (!sg || typeof sg !== 'object') return;
        var key = IU.pick(sg, 'partition', 'partition_key', 'logical_part', 'part_key', 'logical');
        if (key === undefined || key === null) return;
        key = String(key);
        var rows = Number(IU.pick(sg, 'rows', 'row_count'));
        if (!Number.isFinite(rows)) rows = 0;
        if (byPart[key] === undefined) { byPart[key] = 0; order.push(key); }
        byPart[key] += rows;
        segCount[key] = (segCount[key] || 0) + 1;
      });
      state.parts[name] = { byPart: byPart, order: order, segCount: segCount };
      refreshStatsViews();
    }).catch(function () {
      state.parts[name] = { error: true };
    });
  }

  /* stats/parts 到达后就地更新所有标注单元格（侧栏 + Overview），不整页重渲染 */
  function refreshStatsViews() {
    document.querySelectorAll('[data-stats-for]').forEach(function (el) {
      var s = state.stats[el.dataset.statsFor];
      if (!s) return;
      var key = { rows: 'row_count', parts: 'part_count', segs: 'segment_count' }[el.dataset.kind];
      var v = s[key];
      if (v === undefined) { el.textContent = '—'; return; }
      el.textContent = IU.fmt.int(v) + (el.dataset.kind === 'rows' ? ' 行' : '');
      el.removeAttribute('title'); // 清除"统计中"占位标记
    });
    var totalEl = document.getElementById('stat-total-rows');
    if (totalEl) {
      var sum = 0, done = 0;
      state.tables.forEach(function (t) {
        var s2 = state.stats[String(IU.pick(t, 'name', 'table'))];
        if (s2 && typeof s2.row_count === 'number') { sum += s2.row_count; done++; }
      });
      totalEl.textContent = IU.fmt.int(sum);
      var hint = document.getElementById('stat-total-hint');
      if (hint) hint.textContent = done < state.tables.length ? '统计中…' : '';
    }
    /* Overview 卡片的分区分布信号（data-part-signal 标注） */
    document.querySelectorAll('[data-part-signal]').forEach(function (el) {
      var name = el.dataset.partSignal;
      var dist = state.parts[name];
      if (!dist || dist.error) return;
      var fresh = IU.overviewPartSignal ? IU.overviewPartSignal(name, dist) : null;
      if (fresh) { IU.clear(el); el.append(fresh); }
    });
    /* Overview 卡片的时间范围（由分区 key 推导，parts 到达后填充） */
    document.querySelectorAll('[data-part-range]').forEach(function (el) {
      var dist = state.parts[el.dataset.partRange];
      if (!dist || dist.error) return;
      var keys = (dist.order || []).map(Number).filter(function (n) {
        return Number.isFinite(n) && n >= 1e12;
      });
      if (!keys.length) return;
      var lo = IU.fmt.date(Math.min.apply(null, keys));
      var hi = IU.fmt.date(Math.max.apply(null, keys));
      el.textContent = lo ? lo + ' ~ ' + (hi || '') : '—';
      el.className = 'v';
    });
    renderStatusbar();
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
    var sum = 0, done = 0;
    state.tables.forEach(function (t) {
      var s2 = state.stats[String(IU.pick(t, 'name', 'table'))];
      if (s2 && typeof s2.row_count === 'number') { sum += s2.row_count; done++; }
    });
    var left = state.tables.length + ' 张表 · ' + IU.fmt.int(sum) + ' 行';
    if (done < state.tables.length && state.tables.length) left += '（统计中…）';
    if (state.table) {
      var s3 = state.stats[state.table];
      left = state.table + ' · ' +
        (s3 && typeof s3.row_count === 'number' ? IU.fmt.int(s3.row_count) + ' 行' : '') +
        ' · ' + left;
    }
    el.textContent = left;
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
      nav.append(h('div', { style: { padding: '8px 10px', color: 'var(--tx2)', fontSize: '12px' } }, '暂无表'));
    }
    state.tables.forEach(function (t) {
      var name = String(IU.pick(t, 'name', 'table'));
      var pstr = IU.pick(t, 'partition_str', 'partition');
      var s = state.stats[name];
      // 占位一致性（与 Overview 对齐）：统计失败显示 —，未到达显示 …，成功显示行数
      var rowsText = s && s.error
        ? '—'
        : (s && typeof s.row_count === 'number'
          ? IU.fmt.int(s.row_count) + ' 行' : '…');
      nav.append(h('a', {
        class: 'side-item', href: '#/table/' + encodeURIComponent(name) + '/' + lastTableTab(),
        'data-nav': 'table:' + name
      },
        h('div', { class: 'side-name' }, h('span', { class: 'glyph' }, '▸'), name),
        h('div', { class: 'side-meta' },
          h('span', {
            class: 'side-rows',
            title: s ? (s.error ? '统计失败' : null) : '行数统计中…',
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
      var locked = t.id === 'maintain' && state.mode !== 'write';
      bar.append(h('button', {
        class: 'tab-btn', role: 'tab', 'data-tab': t.id,
        disabled: locked,
        title: locked ? '只读模式下不可用' : null,
        onclick: function () { switchTab(t.id); }
      },
        h('span', { class: 'tab-num' }, String(i + 1)),
        t.label));
    });
  }

  function updateActiveUi() {
    document.querySelectorAll('#tabbar .tab-btn').forEach(function (b) {
      b.classList.toggle('active', b.dataset.tab === state.tab);
    });
    document.querySelectorAll('#sidebar .side-item').forEach(function (a) {
      var key = a.dataset.nav;
      var active = state.table === null ? key === 'overview' : key === 'table:' + state.table;
      a.classList.toggle('active', active);
    });
  }

  /* ---------------- 路由 ---------------- */
  function switchTab(tabId) {
    if (TAB_IDS.indexOf(tabId) < 0) return;
    if (tabId === 'maintain' && state.mode !== 'write') {
      IU.toast('只读模式下不可用', 'warn', 'Maintain 维护台需要 --write 模式启动');
      return;
    }
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
  function renderContent() {
    var content = document.getElementById('content');
    IU.clear(content);

    // 总览：无需 spec
    if (state.tab === 'overview') {
      safeRender(IU.tabs.overview, content, {
        mode: state.mode, tables: state.tables, table: null, spec: null,
        stats: state.stats, parts: state.parts
      });
      return;
    }

    // 表级 Tab：懒加载并缓存 spec
    var spec = state.specCache[state.table];
    if (spec) {
      safeRender(IU.tabs[state.tab], content, ctxOf(spec));
      return;
    }
    content.append(h('div', { class: 'boot-screen' }, h('span', { class: 'spinner' }), '加载表契约…'));
    IU.api('/api/table/' + encodeURIComponent(state.table) + '/spec').then(function (specResp) {
      state.specCache[state.table] = (specResp && specResp.spec) || {};
      if (parseHash().table === state.table) { // 防止用户已切走
        IU.clear(content);
        safeRender(IU.tabs[state.tab], content, ctxOf(state.specCache[state.table]));
      }
    }).catch(function () {
      IU.clear(content);
      content.append(h('div', { class: 'empty-state' },
        '表契约加载失败：', h('span', { class: 'mono' }, state.table),
        h('div', { style: { marginTop: '10px' } },
          h('button', { class: 'btn', onclick: renderContent }, '重试'))));
    });

    function ctxOf(spec) {
      return { mode: state.mode, tables: state.tables, table: state.table, spec: spec, stats: state.stats, parts: state.parts };
    }
  }

  function safeRender(mod, root, ctx) {
    if (!mod || typeof mod.render !== 'function') {
      root.append(h('div', { class: 'empty-state' }, '该 Tab 未实现'));
      return;
    }
    try {
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
      renderContent();
      return;
    }
    // ←/→ 仅在 Data 浏览视图生效
    if (state.tab === 'data' && typeof IU.browseNav === 'function') {
      if (e.key === 'ArrowRight') { IU.browseNav(1); e.preventDefault(); }
      else if (e.key === 'ArrowLeft') { IU.browseNav(-1); e.preventDefault(); }
    }
  });

  window.addEventListener('hashchange', route);

  boot();
})();
