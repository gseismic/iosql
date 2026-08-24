/* =====================================================================
 * iosql UI · tab-browse.js
 * Browse Data：表头每列过滤行（DB4S 式贴数据过滤）+ 条件构造器叠加
 * + keyset 翻页 + 列头排序 + 时间戳可读化 + 页大小选择 + 单元格详情。
 *
 * 交互约定（PLAN-059）：
 * - 表头过滤行：裸词精确匹配、>/>=/</<= 比较、=v 强制文本、!=v 排除、
 *   a~b 范围、逗号分隔多值 in；300ms 防抖自动生效，Enter 立即，Esc 清本列。
 * - 时序契约表默认按时间倒序（最新优先）；普通表正序。
 * - 数据到达只替换 tbody，不重建过滤输入框，避免输入焦点丢失。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var PAGE_SIZES = [100, 200, 500, 1000, 2000];
  var LS_PAGE_SIZE = 'iosql_ui_page_size';
  var LS_TS_READABLE = 'iosql_ui_ts_readable';
  var DEBOUNCE_MS = 300;
  /* 时间列名启发式：精确名或后缀；仅整型且毫秒量级才格式化 */
  var TS_NAME_RE = /^(ts|time|timestamp|datetime)$|(_ts|_time|_at)$/i;

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
    if (st && st.table === ctx.table) return st;
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
      builder: new IU.ConditionBuilder(columns, colTypes),
      sortCol: ds.col,
      sortDir: ds.dir,
      filters: {},        // 列名 → 过滤输入原文（未解析）
      chain: [null],      // cursorChain[i] = 第 i+1 页的请求游标；第 1 页为 null
      pos: 0,
      nextCursor: null,
      appliedOrderBy: '',
      pageSize: loadPageSize(),
      tsReadable: loadTsReadable(),
      countCache: {},     // whereJSON → 行数（进程内缓存）
      lastSig: '',        // 最近一次成功渲染的条件签名
      shell: null         // 表格骨架（thead 两行 + tbody），数据更新只换 tbody
    };
    return st;
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
    var pk = spec && Array.isArray(spec.pk) ? spec.pk : null;
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

  /* 供全局快捷键 ←/→ 调用 */
  IU.browseNav = function (dir) {
    if (!st) return;
    if (dir > 0) gotoNext();
    else gotoPrev();
  };

  IU.tabs = IU.tabs || {};

  IU.tabs.browse = {
    id: 'browse',
    label: 'Browse Data',

    render: function (root, ctx) {
      var s = ensureState(ctx);
      var base = '/api/table/' + encodeURIComponent(ctx.table);

      IU.clear(root);

      /* --- 筛选与工具卡 --- */
      root.append(h('div', { class: 'card' },
        s.builder.el,
        h('div', { class: 'toolbar' },
          h('button', {
            class: 'btn primary',
            onclick: function () { applyFilters(); }
          }, '应用筛选'),
          h('button', {
            class: 'btn ghost', title: '清空表头过滤与高级筛选并重新查询',
            onclick: function () { clearAllFilters(); }
          }, '清空过滤'),
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
          ]))));

      /* --- 数据区 --- */
      var tableBox = h('div');
      var pagerBar = h('div', { class: 'pager-bar' });
      var infoBar = h('div', { class: 'pager-bar' });
      root.append(tableBox, pagerBar, infoBar);

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
        var sel = h('select', { class: 'input', style: { fontSize: '11px', padding: '2px 4px' } },
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

      function currentWhere() {
        var w = collectWhere();
        if (w === undefined) return undefined;
        return w;
      }

      function lastWhere() {
        var w = collectWhere();
        return w === undefined ? [] : w;
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
        applyFilters();
      }

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
        fetchPage(where);
      }

      function fetchPage(where) {
        renderBodyLoading();
        var body = { where: where || [], order_by: orderByStr(s), limit: s.pageSize };
        var cursor = s.chain[s.pos];
        if (cursor !== null && cursor !== undefined) body.cursor = cursor;

        IU.api(base + '/rows', { method: 'POST', body: body }).then(function (resp) {
          var norm = IU.normalizeRows(resp);
          s.nextCursor = IU.pick(resp || {}, 'next_cursor', 'nextCursor') || null;
          s.appliedOrderBy = String(IU.pick(resp || {}, 'order_by', 'orderBy') || body.order_by);
          s.lastSig = JSON.stringify(where || []);
          s.lastNorm = norm;
          syncColumns(norm);
          renderBody(norm);
          renderPager();
          fetchCount(where);
        }).catch(function () {
          renderBodyError();
          renderPager();
        });
      }

      /* 动态列兜底：行里出现契约外列时补进列头（触发骨架重建） */
      function syncColumns(norm) {
        norm.rows.forEach(function (row) {
          Object.keys(row).forEach(function (k) {
            if (s.columns.indexOf(k) < 0) s.columns.push(k);
          });
        });
      }

      /* 总行数异步统计：按 where 签名缓存 */
      var countSeq = 0;
      function fetchCount(where) {
        var sig = JSON.stringify(where || []);
        if (s.countCache[sig] !== undefined) { renderInfo(sig); return; }
        var seq = ++countSeq;
        IU.api(base + '/count', { method: 'POST', body: { where: where || [] } }).then(function (resp) {
          var n = IU.pick(resp || {}, 'count', 'total', 'rows');
          if (n !== undefined) s.countCache[sig] = Number(n);
          if (seq === countSeq) renderInfo(sig);
        }).catch(function () { /* toast 已由 api() 弹出 */ });
      }

      function renderInfo(sig) {
        IU.clear(infoBar);
        var n = s.countCache[sig];
        infoBar.append(
          h('span', null, '总行数：',
            n === undefined
              ? h('span', { class: 'spinner', style: { width: '11px', height: '11px' } })
              : h('b', { class: 'mono' }, IU.fmt.int(n)),
            h('span', { class: 'faint' }, n === undefined ? ' 统计中…' : ''),
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
        var cols = s.columns;
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
        return h('th', {
          class: 'sortable' + (sorted ? ' sorted' : '') +
            (isFilteredCol(c) ? ' filtered' : '') +
            (IU.isNumericCol(c, s.colTypes) ? ' num' : ''),
          title: '点击按 ' + c + ' 排序（重置到第一页）',
          onclick: function () {
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
            applyFilters();
          }
          e.stopPropagation(); // 不触发全局快捷键
        });
        input.addEventListener('click', function (e) { e.stopPropagation(); }); // 不触发行头排序
        return h('th', { class: 'filter-cell' + (isFilteredCol(c) ? ' filtered' : '') }, input);
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
            class: 'loading-cell', colspan: String(Math.max(s.columns.length, 1))
          }, '无命中行：调整表头过滤或高级筛选后重试')));
          refreshTitleMarks();
          return;
        }

        norm.rows.forEach(function (row) {
          var tr = h('tr', { class: 'clickable' });
          s.columns.forEach(function (c) {
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
        applyFilters();
        IU.toast('已在 ' + c + ' 列应用精确过滤', 'ok');
      }

      function refreshTitleMarks() {
        var shell = s.shell;
        if (!shell) return;
        Array.prototype.forEach.call(shell.titleTr.children, function (th, idx) {
          var c = s.columns[idx];
          th.classList.toggle('filtered', isFilteredCol(c));
        });
        Array.prototype.forEach.call(
          tableBox.querySelectorAll('.filter-cell'), function (th, idx) {
            th.classList.toggle('filtered', isFilteredCol(s.columns[idx]));
          });
      }

      function renderBodyLoading() {
        var shell = ensureShell();
        IU.clear(shell.tbody);
        shell.tbody.append(h('tr', null,
          h('td', { class: 'loading-cell', colspan: String(Math.max(s.columns.length, 1)) },
            h('span', { class: 'spinner' }), ' 加载中…')));
      }

      function renderBodyError() {
        var shell = ensureShell();
        IU.clear(shell.tbody);
        shell.tbody.append(h('tr', null,
          h('td', { class: 'loading-cell', colspan: String(Math.max(s.columns.length, 1)) },
            '加载失败，请重试（错误详情见右下角提示）')));
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
          h('span', { class: 'faint mono', style: { fontSize: '11px' } },
            '每页 ' + s.pageSize + ' 行 · keyset: ' + (s.nextCursor ? '有下一页游标' : '无下一页')));
      }

      /* 首次渲染即拉第一页 */
      fetchPage(currentWhereSafe());

      function currentWhereSafe() {
        var w = collectWhere();
        return w === undefined ? [] : w;
      }
    }
  };

  function safeJson(v) {
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }
})();
