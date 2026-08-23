/* =====================================================================
 * iosql UI · app.js
 * SPA 外壳：启动引导（/api/db + /api/tables）→ 顶栏（URI/模式徽章）
 * → 左侧栏（总览 + 表列表）→ Tab 栏与 hash 路由 → 全局快捷键。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var TABS = [
    { id: 'overview', label: 'Overview' },
    { id: 'browse', label: 'Browse Data' },
    { id: 'structure', label: 'Structure' },
    { id: 'query', label: 'Query' },
    { id: 'charts', label: 'Charts' },
    { id: 'maintain', label: 'Maintain' }
  ];
  var TAB_IDS = TABS.map(function (t) { return t.id; });
  var LAST_TAB_KEY = 'iosql_ui_last_tab';

  var state = {
    db: null,          // GET /api/db 结果
    tables: [],        // GET /api/tables 数组
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
    }).catch(function () {
      IU.clear(content);
      content.append(h('div', { class: 'boot-screen' },
        h('div', { style: { fontSize: '30px' } }, '⚠'),
        h('div', null, '无法连接后端 API（/api/db）。后端服务可能尚未就绪。'),
        h('button', { class: 'btn primary', onclick: boot }, '重试')));
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

  /* ---------------- 左侧栏 ---------------- */
  function lastTableTab() {
    var v = '';
    try { v = localStorage.getItem(LAST_TAB_KEY) || ''; } catch (e) { /* 隐私模式等 */ }
    return TAB_IDS.indexOf(v) > 0 ? v : 'browse'; // 下标 0 是 overview，不算表级 Tab
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
      var name = String(IU.pick(t, 'name', 'table') || '(未知)');
      var rows = IU.pick(t, 'row_count', 'rows');
      var pstr = IU.pick(t, 'partition_str', 'partition');
      nav.append(h('a', {
        class: 'side-item', href: '#/table/' + encodeURIComponent(name) + '/' + lastTableTab(),
        'data-nav': 'table:' + name
      },
        h('div', { class: 'side-name' }, h('span', { class: 'glyph' }, '▸'), name),
        h('div', { class: 'side-meta' },
          h('span', { class: 'side-rows' }, rows !== undefined ? IU.fmt.int(rows) + ' 行' : ''),
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
      return { table: decodeURIComponent(m[1]), tab: m[2] || lastTableTab() };
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
  }

  /* ---------------- 工作区渲染 ---------------- */
  function renderContent() {
    var content = document.getElementById('content');
    IU.clear(content);

    // 总览：无需 spec
    if (state.tab === 'overview') {
      safeRender(IU.tabs.overview, content, { mode: state.mode, tables: state.tables, table: null, spec: null });
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
      return { mode: state.mode, tables: state.tables, table: state.table, spec: spec };
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
    // 数字 1-6 切 Tab
    if (e.key >= '1' && e.key <= '6') {
      switchTab(TABS[Number(e.key) - 1].id);
      return;
    }
    // R 刷新当前工作区
    if (e.key === 'r' || e.key === 'R') {
      renderContent();
      return;
    }
    // ←/→ 仅在 Browse Data 生效
    if (state.tab === 'browse' && typeof IU.browseNav === 'function') {
      if (e.key === 'ArrowRight') { IU.browseNav(1); e.preventDefault(); }
      else if (e.key === 'ArrowLeft') { IU.browseNav(-1); e.preventDefault(); }
    }
  });

  window.addEventListener('hashchange', route);

  boot();
})();
