/* =====================================================================
 * iosql Web · tab-data.js
 * Data 数据工作台（PLAN-064 D1：合并原 Browse Data 与 Query）：
 * - 分段控件 浏览 | 时序 | 截面（后两者仅 has_time 契约显示）
 * - 浏览视图：表头微语法过滤行（300ms 防抖自动生效）+ 高级条件构造器
 *   （默认折叠）+ where 预览条（两入口一个真相）+ Explain 附属 Dock
 *   + keyset 翻页 + 列头排序 + 列选择器（D12）+ 单元格详情
 * - 时序视图：start/end/date/session/symbols → /query（时间支持自然日期，D3）
 * - 截面视图：symbols/as_of/by/columns → /latest
 * 安全约定：接口数据一律 textContent 渲染，禁止 innerHTML 插值动态数据。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var PAGE_SIZES = [100, 200, 500, 1000, 2000];
  var LS_PAGE_SIZE = 'iosql_ui_page_size';
  var LS_TS_READABLE = 'iosql_ui_ts_readable';
  var DEBOUNCE_MS = 300;
  /* 大表提示阈值：与后端 BIG_TABLE_WARN_ROWS 对齐 */
  var BIG_ROWS_HINT = 1000000;
  /* 时间列名启发式：精确名或后缀；仅整型且毫秒量级才格式化 */
  var TS_NAME_RE = /^(ts|time|timestamp|datetime)$|(_ts|_time|_at)$/i;
  /* 宽表列选择（D12）：超过该列数时默认只保留前 KEEP 列 + 主键/时间列 */
  var WIDE_COLS_AUTO_HIDE = 20;
  var WIDE_COLS_KEEP = 10;

  function loadPageSize() {
    try {
      var v = parseInt(localStorage.getItem(LS_PAGE_SIZE), 10);
      if (PAGE_SIZES.indexOf(v) >= 0) return v;
    } catch (e) { /* 隐私模式等 */ }
    return 200;
  }

  function loadTsReadable() {
    try {
      var v = localStorage.getItem(LS_TS_READABLE);
      if (v === null) return true;
      return v === '1';
    } catch (e) { return true; }
  }

  /* 模块级状态：切表时重置，Tab 来回切换时保留筛选与页位 */
  var st = null;

  function ensureState(ctx) {
    var urlSeed = location.hash.slice(location.hash.indexOf('?') + 1);
    if (st && st.table === ctx.table && st.urlSeed === urlSeed) return st;
    var sameTable = st && st.table === ctx.table;
    if (sameTable) {
      // 同表但 URL 种子变化（如从 Structure 分区行再次跳入）：重种 f.* 过滤
      st.urlSeed = urlSeed;
      seedFromUrl(st);
      st.chain = [null];
      st.pos = 0;
      st.refetchNeeded = true;
      return st;
    }
    var spec = ctx.spec || {};
    var colTypes = (spec.columns && typeof spec.columns === 'object') ? spec.columns : {};
    var columns = Object.keys(colTypes);
    var ds = defaultSort(spec, columns);
    st = {
      table: ctx.table,
      spec: spec,
      columns: columns,
      colTypes: colTypes,
      timeCol: resolveTimeCol(spec),
      tsCols: detectTsCols(colTypes, spec),
      pkCols: Array.isArray(spec.primary_key) ? spec.primary_key.slice() : [],
      builder: new IU.ConditionBuilder(columns, colTypes),
      builderOpen: false,   // 高级条件构造器默认折叠（D5）
      explainOpen: false,   // Explain Dock 默认收起（D9）
      view: 'browse',       // browse | timeseries | latest（D1 分段控件）
      sortCol: ds.col,
      sortDir: ds.dir,
      filters: {},        // 列名 → 过滤输入原文（未解析）
      chain: [null],      // cursorChain[i] = 第 i+1 页的请求游标；第 1 页为 null
      pos: 0,
      nextCursor: null,
      appliedOrderBy: '',
      pageSize: loadPageSize(),
      tsReadable: loadTsReadable(),
      countCache: {},     // whereJSON → 行数（进程内缓存，-1 = 统计失败/超预算）
      countHint: {},      // whereJSON → 失败提示
      badCols: {},        // 排序列 → 服务端 NULL 拒绝提示（禁用该列排序）
      lastSig: '',        // 最近一次成功渲染的条件签名
      lastNorm: null,     // 最近一次成功页数据（视图切换不重拉）
      shell: null,        // 表格骨架（thead 两行 + tbody），数据更新只换 tbody
      hidden: {},         // D12 列选择器：隐藏列 → 1
      hiddenAuto: 0,      // 宽表自动隐藏的列数（提示用）
      limit: '200',
      urlSeed: urlSeed,
      refetchNeeded: false
    };
    seedHiddenForWide(st);
    seedFromUrl(st); // URL cols 优先级高于宽表默认隐藏
    return st;
  }

  /* URL query 种子：#/table/x/data?f.col=raw&view=timeseries&cols=c1,c2
   * - f.*：Structure 分区行点击即以 f.<timeCol>=lo~hi 跳入（Q2 可分享 URL）
   * - cols：可见列白名单（D12 列显隐可分享），优先级高于宽表默认隐藏 */
  function seedFromUrl(s) {
    if (location.hash.indexOf('?') < 0) return;
    var params = new URLSearchParams(location.hash.slice(location.hash.indexOf('?') + 1));
    params.forEach(function (v, k) {
      if (k.slice(0, 2) === 'f.' && k.length > 2) s.filters[k.slice(2)] = v;
    });
    var vw = params.get('view');
    if (vw === 'timeseries' || vw === 'latest') s.view = vw;
    var cols = params.get('cols');
    if (cols !== null && cols !== '') {
      var keep = {};
      cols.split(',').forEach(function (c) { if (c !== '') keep[c] = 1; });
      if (Object.keys(keep).length) {
        s.hidden = {};
        s.columns.forEach(function (c) { if (!keep[c]) s.hidden[c] = 1; });
      }
    }
  }

  /* 列显隐变化回写 URL（replaceState 不触发重渲染/请求，D12 可分享） */
  function syncColsToUrl(s) {
    var qi = location.hash.indexOf('?');
    var params = new URLSearchParams(qi >= 0 ? location.hash.slice(qi + 1) : '');
    params.set('cols', visibleCols(s).join(','));
    var base = qi >= 0 ? location.hash.slice(0, qi) : location.hash;
    var qs = params.toString();
    if (history.replaceState) history.replaceState(null, '', base + '?' + qs);
    s.urlSeed = qs; // 保持 ensureState 种子一致性
  }

  /* 宽表默认隐藏（D12）：>20 列时只显示前 10 列 + 主键/时间列，其余进选择器 */
  function seedHiddenForWide(s) {
    if (s.columns.length <= WIDE_COLS_AUTO_HIDE) return;
    var keep = {};
    s.columns.slice(0, WIDE_COLS_KEEP).forEach(function (c) { keep[c] = 1; });
    s.pkCols.forEach(function (c) { keep[c] = 1; });
    if (s.timeCol) keep[s.timeCol] = 1;
    Object.keys(s.tsCols).forEach(function (c) { keep[c] = 1; });
    s.columns.forEach(function (c) { if (!keep[c]) s.hidden[c] = 1; });
    s.hiddenAuto = s.columns.length - Object.keys(keep).length;
  }

  function visibleCols(s) {
    return s.columns.filter(function (c) { return !s.hidden[c]; });
  }

  /* time 契约列：spec.time 可能是字符串或对象 */
  function resolveTimeCol(spec) {
    var t = spec && spec.time;
    if (!t) return null;
    if (typeof t === 'string') return t;
    if (typeof t === 'object') return IU.pick(t, 'column', 'col', 'field', 'name') || null;
    return null;
  }

  /* 可读化候选列：time 契约列 ∪ 整型且名字像时间的列 */
  function detectTsCols(colTypes, spec) {
    var set = {};
    var tc = resolveTimeCol(spec);
    if (tc) set[tc] = true;
    Object.keys(colTypes).forEach(function (c) {
      var ty = String(colTypes[c]).toLowerCase();
      if (ty.indexOf('int') >= 0 && TS_NAME_RE.test(c)) set[c] = true;
    });
    return set;
  }

  /* 默认排序：time 契约表倒序（最新优先），其余正序（F02） */
  function defaultSort(spec, columns) {
    var t = spec && spec.time;
    var timeCol = t && typeof t === 'object'
      ? IU.pick(t, 'column', 'col', 'field', 'name')
      : (typeof t === 'string' ? t : null);
    var pk = spec && Array.isArray(spec.primary_key) ? spec.primary_key : null;
    var cand = [timeCol, spec && spec.order_key, pk && pk[0], columns[0]];
    var col = columns[0] || '';
    for (var i = 0; i < cand.length; i++) {
      if (typeof cand[i] === 'string' && cand[i]) { col = cand[i]; break; }
    }
    return { col: col, dir: timeCol ? 'desc' : 'asc' };
  }

  function orderByStr(s) {
    return s.sortDir === 'desc' ? s.sortCol + ' desc' : s.sortCol;
  }

  /* ---------------- 表头过滤语法解析（映射到后端 op 白名单） ----------------
   * 返回 {comp} 有效条件 / {skip:true} 空或未写完 / {error:msg} 明确错误 */
  function parseFilterInput(raw, colName, colType) {
    var s = String(raw == null ? '' : raw).trim();
    if (!s) return { skip: true };

    function leaf(op, text, keepText) {
      text = text.trim();
      if (text === '') return { skip: true }; // 只输入了运算符：视为未写完
      var value = keepText ? text : IU.fmt.coerce(text, colType);
      return { comp: { field: colName, op: op, value: value } };
    }

    if (s.slice(0, 2) === '!=') return leaf('!=', s.slice(2), true);
    if (s[0] === '!') return leaf('!=', s.slice(1), true);
    if (s.slice(0, 2) === '>=') return leaf('>=', s.slice(2));
    if (s[0] === '>') return leaf('>', s.slice(1));
    if (s.slice(0, 2) === '<=') return leaf('<=', s.slice(2));
    if (s[0] === '<') return leaf('<', s.slice(1));
    if (s[0] === '=') return leaf('==', s.slice(1), true);

    var tilde = s.indexOf('~');
    if (tilde > 0) {
      var loText = s.slice(0, tilde).trim();
      var hiText = s.slice(tilde + 1).trim();
      if (!loText || !hiText) return { skip: true };
      return {
        comp: {
          field: colName, op: 'between',
          value: [IU.fmt.coerce(loText, colType), IU.fmt.coerce(hiText, colType)]
        }
      };
    }

    if (s.indexOf(',') >= 0) {
      var list = IU.fmt.parseList(s, colType);
      if (list.length >= 1) return { comp: { field: colName, op: 'in', value: list } };
    }
    return leaf('==', s);
  }

  /* 收集全部激活条件：表头行（AND 平铺）∪ 高级构造器（支持 or 分组）*/
  function collectWhere() {
    var headerComps = [];
    Object.keys(st.filters).forEach(function (col) {
      var r = parseFilterInput(st.filters[col], col, st.colTypes[col]);
      if (r.comp) headerComps.push(r.comp);
    });
    var adv = st.builder.getWhere();
    if (!adv.ok) { IU.toast(adv.msg, 'err'); return undefined; }
    return headerComps.concat(adv.where || []);
  }

  /* ---------------- where 人读文本（D5 预览条） ---------------- */
  function whereText(s) {
    var parts = [];
    Object.keys(s.filters).forEach(function (col) {
      var r = parseFilterInput(s.filters[col], col, s.colTypes[col]);
      if (r.comp) parts.push(compText(r.comp));
    });
    var adv = s.builder.getWhere();
    if (adv.ok && adv.where && adv.where.length) parts.push(groupText(adv.where));
    return parts.length ? parts.join(' AND ') : null;
  }

  function compText(c) {
    function show(v) {
      var n = Number(v);
      // 毫秒时间戳附可读日期（D3：时间语义统一）
      if (Number.isFinite(n) && n >= 1e12 && IU.fmt.date(n)) {
        return v + ' (' + IU.fmt.date(n) + ')';
      }
      return String(v);
    }
    var v = Array.isArray(c.value)
      ? (c.op === 'between' ? c.value.map(show).join(' ~ ') : c.value.map(show).join(', '))
      : show(c.value);
    return c.field + ' ' + c.op + ' ' + v;
  }

  function groupText(w) {
    if (Array.isArray(w)) return w.map(groupText).join(' AND ');
    if (w && w.or) return '(' + w.or.map(groupText).join(' OR ') + ')';
    if (w && w.and) return '(' + w.and.map(groupText).join(' AND ') + ')';
    if (w && w.field) return compText(w);
    return '';
  }

  /* 供全局快捷键 ←/→ 调用 */
  IU.browseNav = function (dir) {
    if (!st || st.view !== 'browse') return;
    if (dir > 0) gotoNext();
    else gotoPrev();
  };

  IU.tabs = IU.tabs || {};

  IU.tabs.data = {
    id: 'data',
    label: 'Data',
    render: renderTab
  };

  function renderTab(root, ctx) {
    var s = ensureState(ctx);
    var base = '/api/table/' + encodeURIComponent(ctx.table);
    var spec = ctx.spec || {};

    IU.clear(root);

    /* --- 页头：表名 + 分段控件（D1，仅时序表） --- */
    var head = h('div', { class: 'data-head' },
      h('span', { class: 'table-name', style: { fontWeight: 700, fontFamily: 'var(--font-mono)', fontSize: '16px' } }, ctx.table),
      spec.time ? IU.badge('时序', 'info') : null,
      h('span', { class: 'faint mono', style: { fontSize: '12.5px' } },
        String(IU.pick(spec.partition && spec.partition.str ? spec.partition : null, 'str') || '无分区')));
    if (spec.time) {
      var seg = h('div', { class: 'seg-control' });
      [['browse', '浏览'], ['timeseries', '时序'], ['latest', '截面']].forEach(function (v) {
        seg.append(h('button', {
          class: s.view === v[0] ? 'active' : '',
          onclick: function () {
            s.view = v[0];
            renderTab(root, ctx); // 视图切换重画，st 保留筛选与数据
          }
        }, v[1]));
      });
      head.append(seg);
    }
    root.append(head);

    if (s.view === 'timeseries') { buildTimeseriesView(root, base); return; }
    if (s.view === 'latest') { buildLatestView(root, base); return; }
    buildBrowseView(root, ctx, base);
  }

  /* =====================================================================
   * 浏览视图（原 Browse Data 主体 + D5/D7/D9/D12 增强）
   * ===================================================================== */
  function buildBrowseView(root, ctx, base) {
    var s = st;

    /* --- where 预览条（D5） --- */
    var wpExpr = h('span', { class: 'wp-expr' });
    var whereBar = h('div', { class: 'where-preview' },
      h('span', { class: 'micro-label' }, 'WHERE'),
      wpExpr,
      h('span', { class: 'spacer', style: { flex: '1' } }),
      h('button', {
        class: 'btn sm ghost', title: '复制当前 where 条件 JSON',
        onclick: function () {
          var w = collectWhere();
          var json = JSON.stringify(w || [], null, 0);
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(json).then(function () { IU.toast('where JSON 已复制', 'ok'); });
          }
        }
      }, '复制'),
      h('button', {
        class: 'btn sm ghost', title: '清空表头过滤与高级筛选并重新查询',
        onclick: function () { clearAllFilters(); }
      }, '清空'));

    /* --- 高级条件构造器（默认折叠，D5） --- */
    var builderWrap = h('div', { class: s.builderOpen ? 'card section-gap' : 'card section-gap hidden' }, s.builder.el);

    /* --- Explain Dock（D9） --- */
    var dock = h('div', { class: 'dock hidden' });
    var dockBody = h('div');
    dock.append(
      h('div', { class: 'dock-head' },
        h('span', { class: 'micro-label' }, '查询计划 · explain_select'),
        h('span', { class: 'faint', style: { fontSize: '12px' } }, '一级分区裁剪 → 二级 segment 裁剪'),
        h('span', { class: 'spacer', style: { flex: '1' } }),
        h('button', {
          class: 'btn sm ghost',
          onclick: function () { s.explainOpen = false; dock.classList.add('hidden'); }
        }, '收起 ▴')),
      dockBody);

    /* --- 工具栏 --- */
    var colMenuWrap = buildColMenu(s);
    var toolbar = h('div', { class: 'toolbar' },
      h('button', {
        class: 'btn primary',
        onclick: function () { applyFilters(); }
      }, '应用筛选'),
      h('button', {
        class: 'btn ghost',
        onclick: function () {
          s.builderOpen = !s.builderOpen;
          builderWrap.classList.toggle('hidden', !s.builderOpen);
        }
      }, '⊕ 高级条件'),
      h('button', {
        class: 'btn ghost', onclick: runExplain
      }, 'Explain'),
      h('button', {
        class: 'btn ghost', title: '保持当前页位置与筛选，重新拉取（外部写入后同步）',
        onclick: function () { fetchPage(lastWhere()); }
      }, '刷新'),
      h('span', { class: 'spacer' }),
      h('label', { class: 'chk', title: '把毫秒时间戳列显示为本地日期时间（悬停看原值）' },
        tsToggleEl(), '时间可读'),
      h('label', { class: 'chk' }, pageSizeEl(), '每页行数'),
      IU.menu('导出', [
        { label: 'CSV', onClick: function () { doExport('csv'); } },
        { label: 'JSONL', onClick: function () { doExport('jsonl'); } }
      ]),
      colMenuWrap);

    var tableBox = h('div');
    var pagerBar = h('div', { class: 'pager-bar' });
    var infoBar = h('div', { class: 'pager-bar' });

    root.append(whereBar, toolbar, builderWrap, dock, tableBox, pagerBar, infoBar);

    function tsToggleEl() {
      var cb = h('input', { type: 'checkbox' });
      cb.checked = s.tsReadable;
      cb.addEventListener('change', function () {
        s.tsReadable = cb.checked;
        try { localStorage.setItem(LS_TS_READABLE, s.tsReadable ? '1' : '0'); } catch (e) { /* 忽略 */ }
        renderBodyCurrent(); // 只重渲染当前页，不重新请求
      });
      return cb;
    }

    function pageSizeEl() {
      var sel = h('select', { class: 'input', style: { fontSize: '12px', padding: '2px 4px' } },
        PAGE_SIZES.map(function (n) {
          return h('option', { value: String(n), selected: n === s.pageSize }, String(n));
        }));
      sel.addEventListener('change', function () {
        s.pageSize = parseInt(sel.value, 10) || 200;
        try { localStorage.setItem(LS_PAGE_SIZE, String(s.pageSize)); } catch (e) { /* 忽略 */ }
        applyFilters(); // 改页大小回到第一页
      });
      return sel;
    }

    /* --- where 预览条刷新 --- */
    function refreshWherePreview() {
      IU.clear(wpExpr);
      var txt = whereText(s);
      if (!txt) {
        wpExpr.append(h('span', { class: 'wp-empty' }, '（无过滤条件：显示全部行）'));
        return;
      }
      // AND/OR 关键词高亮为琥珀色
      txt.split(' ').forEach(function (tok, i) {
        if (i) wpExpr.append(' ');
        if (tok === 'AND' || tok === 'OR') wpExpr.append(h('span', { class: 'op' }, tok));
        else wpExpr.append(tok);
      });
    }

    function doExport(format) {
      var where = currentWhere();
      if (where === undefined) return;
      var url = base + '/export?format=' + format;
      if (where && where.length) url += '&where=' + encodeURIComponent(JSON.stringify(where));
      window.open(url, '_blank');
      IU.toast('已发起 ' + format.toUpperCase() + ' 导出下载', 'ok');
    }

    function clearAllFilters() {
      s.filters = {};
      s.builder.reset();
      syncBuilderInputs();
      applyFilters();
    }

    /* 构造器 el 由组件持有，清空后需同步其内部 DOM */
    function syncBuilderInputs() { s.builder.render(); }

    function applyFilters() {
      var where = currentWhere();
      if (where === undefined) return;
      s.chain = [null];
      s.pos = 0;
      fetchPage(where);
    }

    function gotoNext() {
      if (!s.nextCursor) return;
      var where = currentWhere();
      if (where === undefined) return;
      s.pos++;
      if (s.chain[s.pos] === undefined) s.chain[s.pos] = s.nextCursor;
      fetchPage(where);
    }

    function gotoPrev() {
      if (s.pos === 0) return;
      var where = currentWhere();
      if (where === undefined) return;
      s.pos--; // 回到上一页（chain[s.pos] 即上一页游标；首页为 null）
      fetchPage(where);
    }

    /* 请求序号：快速切表/翻页时丢弃过期响应，防止旧数据覆盖新页 */
    var pageSeq = 0;
    /* aborter：新请求自动取消上一个（PLAN-069 P4），大表排序不必干等 */
    var aborter = null;
    var loadTicker = null;

    function stopLoadTicker() {
      if (loadTicker) { clearInterval(loadTicker); loadTicker = null; }
    }

    function startLoadTicker() {
      stopLoadTicker();
      var start = Date.now();
      loadTicker = setInterval(function () {
        var el = tableBox.querySelector('.load-elapsed');
        if (el) el.textContent = ((Date.now() - start) / 1000).toFixed(1) + 's';
      }, 100);
    }

    function fetchPage(where) {
      var seq = ++pageSeq;
      if (aborter) aborter.abort();
      aborter = ('AbortController' in window) ? new AbortController() : null;
      var signal = aborter ? aborter.signal : undefined;
      renderBodyLoading();
      startLoadTicker();
      refreshWherePreview();
      var body = { where: where || [], order_by: orderByStr(s), limit: s.pageSize };
      var cursor = s.chain[s.pos];
      if (cursor !== null && cursor !== undefined) body.cursor = cursor;

      IU.api(base + '/rows', { method: 'POST', body: body, signal: signal }).then(function (resp) {
        if (seq !== pageSeq) return; // 过期响应：丢弃
        stopLoadTicker();
        var norm = IU.normalizeRows(resp);
        s.nextCursor = IU.pick(resp || {}, 'next_cursor', 'nextCursor') || null;
        s.appliedOrderBy = String(IU.pick(resp || {}, 'order_by', 'orderBy') || body.order_by);
        s.lastSig = JSON.stringify(where || []);
        s.lastNorm = norm;
        syncColumns(norm);
        renderBody(norm);
        renderPager();
        fetchCount(where);
      }).catch(function (err) {
        if (seq !== pageSeq) return;
        stopLoadTicker();
        if (err && err.aborted) {
          renderBodyCurrent(); // 已被更新请求取代：回退到上一份数据
          renderPager();
          return;
        }
        handlePageError(err);
        renderPager();
      });
    }

    /* Explain Dock 拉取（D9） */
    function runExplain() {
      var where = currentWhere();
      if (where === undefined) return;
      s.explainOpen = true;
      dock.classList.remove('hidden');
      IU.clear(dockBody);
      dockBody.append(h('div', { class: 'empty-state' }, h('span', { class: 'spinner' }), ' 计算查询计划…'));
      IU.api(base + '/explain', {
        method: 'POST',
        body: { where: where || [], order_by: orderByStr(s), limit: s.pageSize }
      }).then(function (resp) {
        IU.clear(dockBody);
        IU.renderPlanPanel(dockBody, IU.unwrap(resp, 'plan'));
      }).catch(function () {
        IU.clear(dockBody);
        dockBody.append(h('div', { class: 'empty-state' }, '计划获取失败'));
      });
    }

    /* 分页失败处理：排序列含 NULL（服务端 422）时禁用该列排序并在表体提示 */
    function handlePageError(err) {
      var msg = (err && (err.hint || err.message)) || '';
      if (err && err.status === 422 && msg.indexOf('NULL') >= 0) {
        s.badCols[s.sortCol] = msg;
        s.shell = null; // 重建骨架以给该列打上禁用态
        renderBodyError('排序列 `' + s.sortCol + '` 含 NULL，keyset 分页不可用：' + msg
          + ' 请点击其他列排序后重试。');
        IU.toast('该排序列含 NULL，已禁用；请更换排序列', 'warn');
        return;
      }
      renderBodyError();
    }

    /* 动态列兜底：行里出现契约外列时补进列头（触发骨架重建） */
    function syncColumns(norm) {
      norm.rows.forEach(function (row) {
        Object.keys(row).forEach(function (k) {
          if (s.columns.indexOf(k) < 0) s.columns.push(k);
        });
      });
    }

    /* 总行数异步统计：按 where 签名缓存；服务端扫描预算超限时显示提示 */
    var countSeq = 0;
    function fetchCount(where) {
      var sig = JSON.stringify(where || []);
      if (s.countCache[sig] !== undefined) { renderInfo(sig); return; }
      var seq = ++countSeq;
      IU.api(base + '/count', { method: 'POST', body: { where: where || [] } }).then(function (resp) {
        var n = IU.pick(resp || {}, 'count', 'total', 'rows');
        if (n !== undefined) s.countCache[sig] = Number(n);
        if (seq === countSeq) renderInfo(sig);
      }).catch(function (err) {
        if (seq === countSeq) {
          s.countCache[sig] = -1; // 失败/超预算占位：不显示错误数字
          s.countHint[sig] = (err && (err.hint || err.message)) || '统计失败';
        }
        renderInfo(sig);
      });
    }

    function renderInfo(sig) {
      IU.clear(infoBar);
      var n = s.countCache[sig];
      var failed = n === -1;
      // 大表未过滤首页提示（服务端排序尚未下推时全量物化排序可能很慢）
      var bigUnfiltered = !failed && typeof n === 'number' &&
        n > BIG_ROWS_HINT && String(sig) === '[]';
      infoBar.append(
        h('span', null, '总行数：',
          n === undefined
            ? h('span', { class: 'spinner', style: { width: '11px', height: '11px' } })
            : failed
              ? h('b', { class: 'mono' }, '—')
              : h('b', { class: 'mono' }, IU.fmt.int(n)),
          h('span', { class: 'faint' }, n === undefined
            ? ' 统计中…'
            : (failed ? ' ' + (s.countHint[sig] || '统计失败') : '')),
          bigUnfiltered
            ? h('span', { class: 'faint' }, ' · 大表未过滤：如需更快可加表头过滤缩小扫描范围')
            : null,
          activeFilterHint()));
    }

    function activeFilterHint() {
      var n = Object.keys(st.filters).filter(function (k) {
        return String(st.filters[k] || '').trim() !== '';
      }).length;
      return n ? h('span', { class: 'faint' }, ' · ' + n + ' 个表头过滤已生效') : null;
    }

    /* ---------------- 表格骨架：标题行 + 过滤行 + tbody ---------------- */

    function ensureShell() {
      var cols = visibleCols(s); // D12：只渲染可见列
      if (s.shell && s.shell.cols === cols.join('\u0001')) return s.shell;
      IU.clear(tableBox);

      var filterTr = h('tr', { class: 'filter-row' });
      var titleTr = h('tr');
      cols.forEach(function (c) {
        titleTr.append(buildTitleTh(c));
        filterTr.append(buildFilterTh(c));
      });

      var tbody = h('tbody');
      var table = h('table', { class: 'data-table browse-table' },
        h('thead', null, titleTr, filterTr), tbody);
      tableBox.append(h('div', { class: 'table-wrap' }, table));
      s.shell = { cols: cols.join('\u0001'), tbody: tbody, titleTr: titleTr };
      return s.shell;
    }

    function buildTitleTh(c) {
      var sorted = s.sortCol === c;
      var bad = s.badCols[c];
      return h('th', {
        class: 'sortable' + (sorted ? ' sorted' : '') +
          (bad ? ' sort-bad' : '') +
          (isFilteredCol(c) ? ' filtered' : '') +
          (IU.isNumericCol(c, s.colTypes) ? ' num' : ''),
        title: bad
          ? ('该列含 NULL，无法分页排序：' + bad)
          : '点击按 ' + c + ' 排序（重置到第一页）',
        onclick: function () {
          if (bad) {
            IU.toast('该列含 NULL，keyset 分页不可用；请更换排序列', 'warn', bad);
            return;
          }
          if (s.sortCol === c) s.sortDir = s.sortDir === 'asc' ? 'desc' : 'asc';
          else { s.sortCol = c; s.sortDir = 'asc'; }
          s.shell = null; // 重建骨架以刷新列头排序箭头（过滤输入由 st.filters 恢复）
          applyFilters();
        }
      }, c, sorted ? (s.sortDir === 'desc' ? ' ↓' : ' ↑') : '');
    }

    function isFilteredCol(c) {
      return String(st.filters[c] || '').trim() !== '';
    }

    function buildFilterTh(c) {
      var hint = IU.isNumericCol(c, s.colTypes)
        ? '如 >100 · 1~5 · a,b · =7'
        : '精确值 · a,b 多个 · =原文';
      var input = h('input', {
        class: 'input filter-input', type: 'text',
        value: st.filters[c] || '',
        placeholder: hint, autocomplete: 'off', spellcheck: 'false',
        title: '语法：裸词精确 · >v >=v <v <=v · =v 原文 · !=v 排除 · a~b 范围 · a,b,c 任一',
        dataset: { col: c }
      });
      var timer = null;
      input.addEventListener('input', function () {
        st.filters[c] = input.value;
        refreshWherePreview(); // 预览条即时跟随（不触发请求）
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () { applyFilters(); }, DEBOUNCE_MS);
      });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          if (timer) clearTimeout(timer);
          st.filters[c] = input.value;
          applyFilters();
        } else if (e.key === 'Escape') {
          e.preventDefault();
          input.value = '';
          st.filters[c] = '';
          refreshWherePreview();
          applyFilters();
        }
        e.stopPropagation(); // 不触发全局快捷键
      });
      input.addEventListener('click', function (e) { e.stopPropagation(); }); // 不触发行头排序
      return h('th', { class: 'filter-cell' + (isFilteredCol(c) ? ' filtered' : '') }, input);
    }

    /* ---------------- 列选择器（D12：几百列场景） ---------------- */
    function buildColMenu(s) {
      var wrap = h('div', { class: 'menu' });
      var btn = h('button', { class: 'btn ghost', title: '选择显示的列（勾选即改）' });
      var picker = h('div', { class: 'col-picker hidden' });
      var list = h('div', { class: 'cp-list' });
      var search = h('input', {
        class: 'input mono cp-search', placeholder: '搜索列名，如 price、f01…',
        autocomplete: 'off', spellcheck: 'false'
      });

      function label() {
        btn.textContent = '⚙ 列 ' + visibleCols(s).length + '/' + s.columns.length;
      }

      function buildList() {
        IU.clear(list);
        s.columns.forEach(function (c) {
          var on = !s.hidden[c];
          var lb = h('label', { dataset: { col: c } },
            h('input', { type: 'checkbox' }),
            h('span', null, c),
            h('span', { class: 'cp-type' }, colTypeLabel(c)));
          var cb = lb.querySelector('input');
          cb.checked = on;
          cb.addEventListener('change', function () {
            setColVisible(c, cb.checked);
          });
          list.append(lb);
        });
        applySearch();
      }

      function colTypeLabel(c) {
        if (s.tsCols[c]) return '时间';
        var t = String(s.colTypes[c] || '').toLowerCase();
        if (IU.fmt.NUMERIC_RE.test(t)) return '数值';
        return '文本';
      }

      function applySearch() {
        var q = (search.value || '').toLowerCase();
        Array.prototype.forEach.call(list.querySelectorAll('label'), function (lb) {
          lb.style.display = lb.dataset.col.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
        });
      }
      search.addEventListener('input', applySearch);

      function setColVisible(c, show) {
        if (!show && visibleCols(s).length <= 1) {
          IU.toast('至少保留一列', 'warn');
          buildList();
          return;
        }
        if (show) delete s.hidden[c]; else s.hidden[c] = 1;
        label();
        syncColsToUrl(s);
        // 可见列集合变化：重建骨架并重画当前页（不重新请求）
        s.shell = null;
        if (s.lastNorm) { renderBody(s.lastNorm); }
      }

      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var open = picker.classList.contains('hidden');
        picker.classList.toggle('hidden', !open);
        if (open) buildList();
      });
      // 阻止 picker 内部点击冒泡触发 util.js 的 .menu 委托关闭
      picker.addEventListener('click', function (e) { e.stopPropagation(); });

      picker.append(
        h('div', { class: 'cp-head' },
          h('span', { class: 'micro-label' }, '显示列'),
          h('span', { class: 'faint mono', style: { fontSize: '12px' } },
            visibleCols(s).length + '/' + s.columns.length),
          h('span', { class: 'spacer', style: { flex: '1' } }),
          h('button', {
            class: 'btn sm ghost',
            onclick: function () { picker.classList.add('hidden'); }
          }, '完成')),
        search,
        h('div', { class: 'cp-quick' },
          h('button', {
            class: 'btn sm', title: '显示全部列',
            onclick: function () {
              s.hidden = {}; label(); syncColsToUrl(s); s.shell = null; if (s.lastNorm) renderBody(s.lastNorm); buildList();
            }
          }, '全部显示'),
          h('button', {
            class: 'btn sm', title: '回到本 Tab 打开时的默认列集',
            onclick: function () {
              s.hidden = {}; s.hiddenAuto = 0; seedHiddenForWide(s);
              label(); syncColsToUrl(s); s.shell = null; if (s.lastNorm) renderBody(s.lastNorm); buildList();
            }
          }, '恢复默认'),
          h('button', {
            class: 'btn sm', title: '只显示主键列与时间列',
            onclick: function () {
              var keep = {};
              s.pkCols.forEach(function (c) { keep[c] = 1; });
              if (s.timeCol) keep[s.timeCol] = 1;
              Object.keys(s.tsCols).forEach(function (c) { keep[c] = 1; });
              s.hidden = {};
              s.columns.forEach(function (c) { if (!keep[c]) s.hidden[c] = 1; });
              label(); syncColsToUrl(s); s.shell = null; if (s.lastNorm) renderBody(s.lastNorm); buildList();
            }
          }, '仅键与时间列')),
        list,
        h('div', { class: 'cp-foot' },
          h('span', null, '勾选即改，实时生效'),
          h('span', null, s.hiddenAuto ? '宽表已默认隐藏 ' + s.hiddenAuto + ' 列' : '')));

      label();
      wrap.append(btn, picker);
      return wrap;
    }

    /* ---------------- tbody 渲染（不触碰过滤输入框） ---------------- */

    function renderBodyCurrent() {
      if (s.lastNorm) renderBody(s.lastNorm);
    }

    function renderBody(norm) {
      var shell = ensureShell();
      var tbody = shell.tbody;
      IU.clear(tbody);

      if (!norm.rows.length) {
        tbody.append(h('tr', null, h('td', {
          class: 'loading-cell', colspan: String(Math.max(visibleCols(s).length, 1))
        }, '无命中行：调整表头过滤或高级筛选后重试')));
        refreshTitleMarks();
        return;
      }

      norm.rows.forEach(function (row) {
        var tr = h('tr', { class: 'clickable' });
        visibleCols(s).forEach(function (c) {
          tr.append(buildTd(c, row[c]));
        });
        tbody.append(tr);
      });
      refreshTitleMarks();
    }

    function buildTd(c, v) {
      var numeric = IU.isNumericCol(c, s.colTypes, v) && typeof v === 'number';
      var td;
      if (s.tsCols[c] && typeof v === 'number') {
        // 时间语义列：可读化开启且毫秒量级 → 本地时间；否则原始数字
        // （不做千分位分组，13 位时间戳加逗号无法阅读比对）
        if (s.tsReadable && v >= 1e12) {
          td = h('td', { class: 'num ts-cell', title: '原始值: ' + v }, IU.fmt.ts(v));
        } else {
          td = h('td', { class: 'num', title: String(v) }, String(v));
        }
      } else if (v === null || v === undefined) {
        td = h('td', { class: numeric ? 'num' : '', title: 'NULL' },
          h('span', { class: 'null-cell' }, '∅'));
      } else if (numeric) {
        td = h('td', { class: 'num', title: String(v) }, Number(v).toLocaleString('zh-CN'));
      } else if (typeof v === 'object') {
        var json = safeJson(v);
        td = h('td', { title: json }, json);
      } else {
        td = h('td', { title: typeof v === 'string' && v.length > 60 ? v : null }, String(v));
      }
      td.addEventListener('click', function () { openDetail(c, v); });
      return td;
    }

    function openDetail(c, v) {
      var note = null;
      if (typeof v === 'number' && v >= 1e12 && s.tsCols[c]) {
        note = '可读时间: ' + IU.fmt.ts(v) + ' · 原始值: ' + v;
      }
      IU.DetailPanel.open(c, v, {
        note: note,
        actions: [{
          label: '以此值过滤',
          onClick: function () { filterByValue(c, v); }
        }]
      });
    }

    /* F12：以单元格精确值反哺对应列表头（NULL 不支持，见设计文档 §4） */
    function filterByValue(c, v) {
      if (v === null || v === undefined) {
        IU.toast('该值为 NULL，暂不支持按 NULL 过滤', 'warn',
          '核心查询算子不含 IS NULL，可用其他列缩小范围');
        return;
      }
      var text = '=' + (typeof v === 'object' ? JSON.stringify(v) : String(v));
      st.filters[c] = text;
      // 骨架不重建（防焦点丢失），这里需手动同步对应输入框的显示值
      var esc = window.CSS && CSS.escape ? CSS.escape(c) : c;
      var inp = tableBox.querySelector('.filter-input[data-col="' + esc + '"]');
      if (inp) inp.value = text;
      refreshWherePreview();
      applyFilters();
      IU.toast('已在 ' + c + ' 列应用精确过滤', 'ok');
    }

    function refreshTitleMarks() {
      var shell = s.shell;
      if (!shell) return;
      var cols = visibleCols(s);
      Array.prototype.forEach.call(shell.titleTr.children, function (th, idx) {
        var c = cols[idx];
        th.classList.toggle('filtered', isFilteredCol(c));
      });
      Array.prototype.forEach.call(
        tableBox.querySelectorAll('.filter-cell'), function (th, idx) {
          th.classList.toggle('filtered', isFilteredCol(cols[idx]));
        });
    }

    function renderBodyLoading() {
      var shell = ensureShell();
      IU.clear(shell.tbody);
      var elapsed = h('span', { class: 'load-elapsed mono' }, '0.0s');
      shell.tbody.append(h('tr', null,
        h('td', { class: 'loading-cell', colspan: String(Math.max(visibleCols(s).length, 1)) },
          h('span', { class: 'spinner' }), ' 加载中 ',
          elapsed,
          h('button', {
            class: 'btn sm', style: { marginLeft: '12px' },
            onclick: function () { if (aborter) aborter.abort(); }
          }, '取消'))));
    }

    function renderBodyError(msg) {
      var shell = ensureShell();
      IU.clear(shell.tbody);
      shell.tbody.append(h('tr', null,
        h('td', { class: 'loading-cell', colspan: String(Math.max(visibleCols(s).length, 1)) },
          msg || '加载失败，请重试（错误详情见右下角提示）')));
    }

    function renderPager() {
      IU.clear(pagerBar);
      pagerBar.append(
        h('button', {
          class: 'btn sm', disabled: s.pos === 0,
          onclick: gotoPrev
        }, '← 上一页'),
        h('span', null, '第 ', h('b', { class: 'mono' }, String(s.pos + 1)), ' 页'),
        h('button', {
          class: 'btn sm', disabled: !s.nextCursor,
          onclick: gotoNext
        }, '下一页 →'),
        h('span', { class: 'faint' }, '·'),
        h('span', null, 'order_by: ', h('span', { class: 'mono' }, s.appliedOrderBy || orderByStr(s))),
        h('span', { class: 'faint' }, '·'),
        h('span', { class: 'faint mono', style: { fontSize: '12px' } },
          '每页 ' + s.pageSize + ' 行 · keyset: ' + (s.nextCursor ? '有下一页游标' : '无下一页')));
    }

    refreshWherePreview();

    /* 有缓存页（视图切回）直接重画；首次/被 URL 种子刷新时拉第一页 */
    if (s.lastNorm && !s.refetchNeeded) {
      renderBody(s.lastNorm);
      renderPager();
      renderInfo(s.lastSig);
    } else {
      fetchPage(currentWhereSafe());
      s.refetchNeeded = false;
    }

    function currentWhereSafe() {
      var w = collectWhere();
      return w === undefined ? [] : w;
    }

    function currentWhere() {
      return collectWhere();
    }

    function lastWhere() {
      var w = collectWhere();
      return w === undefined ? [] : w;
    }
  }

  /* =====================================================================
   * 时序视图（原 Query 快捷区，D3：时间支持自然日期）
   * ===================================================================== */
  function buildTimeseriesView(root, base) {
    var s = st;
    var queryResult = h('div');

    var fStart = IU.timeField('start', '2025-01-01 或毫秒时间戳');
    var fEnd = IU.timeField('end', '2025-01-31 或毫秒时间戳');
    var fDate = textField('date', '交易日，如 2025-01-06');
    var fSession = textField('session', '交易时段，如 2025-01-06');
    var fSymbols = textField('symbols', '逗号分隔：AAPL,MSFT');

    var btn = h('button', {
      class: 'btn primary',
      onclick: function () {
        var body = {};
        var ms;
        if (fStart.input.value.trim() !== '') {
          ms = fStart.value();
          if (ms === null) { IU.toast('start 无法解析为时间', 'warn'); return; }
          body.start = ms;
        }
        if (fEnd.input.value.trim() !== '') {
          ms = fEnd.value();
          if (ms === null) { IU.toast('end 无法解析为时间', 'warn'); return; }
          body.end = ms;
        }
        if (fDate.input.value.trim() !== '') body.date = fDate.input.value.trim();
        if (fSession.input.value.trim() !== '') body.session = fSession.input.value.trim();
        if (fSymbols.input.value.trim() !== '') body.symbols = splitSymbols(fSymbols.input.value);

        IU.clear(queryResult);
        queryResult.append(loadingCard());
        IU.api(base + '/query', { method: 'POST', body: body }).then(function (resp) {
          renderResultCard(queryResult, resp, '/query 结果');
        }).catch(function () {
          IU.clear(queryResult);
          queryResult.append(h('div', { class: 'empty-state' }, 'query 失败'));
        });
      }
    }, '时间窗查询');

    root.append(h('div', { class: 'card section-gap' },
      h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '时序快捷查询'),
        IU.badge('timeseries', 'info'),
        h('span', { class: 'faint', style: { fontSize: '12px' } }, '时间支持 2025-01-01 / 2025-01-01 10:30 / 毫秒时间戳')),
      h('div', { class: 'quick-grid' },
        fStart.node, fEnd.node, fDate.node, fSession.node, fSymbols.node),
      h('div', { class: 'toolbar' }, btn),
      queryResult));
  }

  /* =====================================================================
   * 截面视图（原 Query latest 区，D3：as_of 支持自然日期）
   * ===================================================================== */
  function buildLatestView(root, base) {
    var latestResult = h('div');

    var fSymbols = textField('symbols', '逗号分隔：AAPL,MSFT');
    var fAsOf = IU.timeField('as_of', '2025-01-06 15:00 或毫秒时间戳');
    var fBy = textField('by', '分组列，逗号分隔：symbol,timeframe');
    var fCols = textField('columns', '返回列，逗号分隔（可空）');

    var btn = h('button', {
      class: 'btn primary',
      onclick: function () {
        var body = {};
        if (fSymbols.input.value.trim() !== '') body.symbols = splitSymbols(fSymbols.input.value);
        if (fAsOf.input.value.trim() !== '') {
          var ms = fAsOf.value();
          if (ms === null) { IU.toast('as_of 无法解析为时间', 'warn'); return; }
          body.as_of = ms;
        }
        if (fBy.input.value.trim() !== '') body.by = IU.fmt.parseList(fBy.input.value);
        if (fCols.input.value.trim() !== '') body.columns = IU.fmt.parseList(fCols.input.value);

        IU.clear(latestResult);
        latestResult.append(loadingCard());
        IU.api(base + '/latest', { method: 'POST', body: body }).then(function (resp) {
          renderResultCard(latestResult, resp, '/latest 截面结果');
        }).catch(function () {
          IU.clear(latestResult);
          latestResult.append(h('div', { class: 'empty-state' }, 'latest 失败'));
        });
      }
    }, '最新截面');

    root.append(h('div', { class: 'card section-gap' },
      h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, 'Latest 最新截面'),
        IU.badge('as-of', 'info'),
        h('span', { class: 'faint', style: { fontSize: '12px' } }, '所有 symbol 截至某时刻的最新一行')),
      h('div', { class: 'quick-grid' },
        fSymbols.node, fAsOf.node, fBy.node, fCols.node),
      h('div', { class: 'toolbar' }, btn),
      latestResult));
  }

  /* ---------------- 共用小件 ---------------- */
  function textField(labelText, placeholder) {
    var input = h('input', {
      class: 'input', placeholder: placeholder || '',
      autocomplete: 'off', spellcheck: 'false'
    });
    return {
      node: h('label', { style: { display: 'flex', flexDirection: 'column', gap: '3px', fontSize: '12px', color: 'var(--tx2)' } },
        labelText, input),
      input: input
    };
  }

  function splitSymbols(raw) {
    return String(raw).split(/[,\s]+/).map(function (x) { return x.trim(); }).filter(Boolean);
  }

  function loadingCard() {
    return h('div', { class: 'empty-state' }, h('span', { class: 'spinner' }), ' 加载中…');
  }

  /* 结果卡：normalizeRows → 复用结果表格 */
  function renderResultCard(box, resp, title) {
    var norm = IU.normalizeRows(resp);
    IU.clear(box);
    box.append(h('div', { class: 'card' },
      h('div', { class: 'card-title' },
        h('span', { class: 'micro-label' }, title),
        h('span', { class: 'faint mono', style: { fontSize: '12px' } }, norm.rows.length + ' 行')),
      h('div', null)));
    var slot = box.lastChild.lastChild;
    IU.renderResultTable(slot, {
      columns: norm.columns,
      rows: norm.rows,
      emptyText: '无数据',
      onCellClick: function (col, value) { IU.DetailPanel.open(col, value); }
    });
  }

  function safeJson(v) {
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }
})();
