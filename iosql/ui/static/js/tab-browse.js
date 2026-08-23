/* =====================================================================
 * iosql UI · tab-browse.js
 * Browse Data：条件构造器 + keyset 翻页（next_cursor 前进 / 游标栈回退）
 * + 列头排序 + 单元格详情侧滑 + CSV/JSONL 导出 + 异步行数统计。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var PAGE_SIZE = 200;

  /* 模块级状态：切表时重置，Tab 来回切换时保留筛选与页位 */
  var st = null;

  function ensureState(ctx) {
    if (st && st.table === ctx.table) return st;
    var spec = ctx.spec || {};
    var colTypes = (spec.columns && typeof spec.columns === 'object') ? spec.columns : {};
    var columns = Object.keys(colTypes);
    st = {
      table: ctx.table,
      columns: columns,
      colTypes: colTypes,
      builder: new IU.ConditionBuilder(columns, colTypes),
      sortCol: defaultSortCol(spec, columns),
      sortDir: 'asc',
      chain: [null],   // cursorChain[i] = 第 i+1 页的请求游标；第 1 页为 null
      pos: 0,          // 当前页下标
      nextCursor: null,
      appliedOrderBy: '',
      countCache: {}   // whereJSON → 行数（进程内缓存）
    };
    return st;
  }

  /* 默认排序列：time → order_key → pk[0] → 首列 */
  function defaultSortCol(spec, columns) {
    var t = spec && spec.time;
    var timeCol = t && typeof t === 'object' ? IU.pick(t, 'column', 'col', 'field', 'name') : (typeof t === 'string' ? t : null);
    var pk = spec && Array.isArray(spec.pk) ? spec.pk : null;
    var cand = [timeCol, spec && spec.order_key, pk && pk[0], columns[0]];
    for (var i = 0; i < cand.length; i++) {
      if (typeof cand[i] === 'string' && cand[i]) return cand[i];
    }
    return columns[0] || '';
  }

  function orderByStr(s) {
    return s.sortDir === 'desc' ? s.sortCol + ' desc' : s.sortCol;
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

      /* --- 条件构造器卡 --- */
      root.append(h('div', { class: 'card' },
        s.builder.el,
        h('div', { class: 'toolbar' },
          h('button', {
            class: 'btn primary',
            onclick: function () { applyFilters(); }
          }, '应用筛选'),
          h('button', {
            class: 'btn ghost',
            onclick: function () {
              s.builder.reset();
              applyFilters();
            }
          }, '重置'),
          h('span', { class: 'spacer' }),
          IU.menu('导出', [
            { label: 'CSV', onClick: function () { doExport('csv'); } },
            { label: 'JSONL', onClick: function () { doExport('jsonl'); } }
          ]))));

      /* --- 数据区 --- */
      var infoBar = h('div', { class: 'pager-bar' });
      var tableBox = h('div');
      var pagerBar = h('div', { class: 'pager-bar' });
      root.append(tableBox, pagerBar, infoBar);

      function currentWhere() {
        var r = s.builder.getWhere();
        if (!r.ok) { IU.toast(r.msg, 'err'); return undefined; }
        return r.where;
      }

      function doExport(format) {
        var where = currentWhere();
        if (where === undefined) return;
        var url = base + '/export?format=' + format;
        if (where && where.length) url += '&where=' + encodeURIComponent(JSON.stringify(where));
        window.open(url, '_blank');
        IU.toast('已发起 ' + format.toUpperCase() + ' 导出下载', 'ok');
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
        s.pos--;
        fetchPage(where);
      }

      function fetchPage(where) {
        renderLoading();
        var body = { where: where || [], order_by: orderByStr(s), limit: PAGE_SIZE };
        var cursor = s.chain[s.pos];
        if (cursor !== null && cursor !== undefined) body.cursor = cursor;

        IU.api(base + '/rows', { method: 'POST', body: body }).then(function (resp) {
          var norm = IU.normalizeRows(resp);
          s.nextCursor = IU.pick(resp || {}, 'next_cursor', 'nextCursor') || null;
          s.appliedOrderBy = String(IU.pick(resp || {}, 'order_by', 'orderBy') || body.order_by);
          renderTable(norm);
          renderPager();
          fetchCount(where);
        }).catch(function () {
          renderError();
          renderPager();
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
          h('span', null, '总行数：', n === undefined ? h('span', { class: 'spinner', style: { width: '11px', height: '11px' } }) : h('b', { class: 'mono' }, IU.fmt.int(n)),
            h('span', { class: 'faint' }, n === undefined ? ' 统计中…' : ' （已缓存）')));
      }

      function renderLoading() {
        IU.clear(tableBox);
        tableBox.append(h('div', { class: 'table-wrap' },
          h('table', { class: 'data-table' },
            headEl(),
            h('tbody', null, h('tr', null,
              h('td', { class: 'loading-cell', colspan: String(Math.max(s.columns.length, 1)) },
                h('span', { class: 'spinner' }), ' 加载中…'))))));
      }

      function renderError() {
        IU.clear(tableBox);
        tableBox.append(h('div', { class: 'empty-state' }, '加载失败，请重试（错误详情见右下角提示）'));
      }

      function headEl() {
        return h('thead', null, h('tr', null, s.columns.map(function (c) {
          var sorted = s.sortCol === c;
          return h('th', {
            class: 'sortable' + (sorted ? ' sorted' : '') + (IU.isNumericCol(c, s.colTypes) ? ' num' : ''),
            title: '点击按 ' + c + ' 排序（重置到第一页）',
            onclick: function () {
              if (s.sortCol === c) s.sortDir = s.sortDir === 'asc' ? 'desc' : 'asc';
              else { s.sortCol = c; s.sortDir = 'asc'; }
              applyFilters();
            }
          }, c, sorted ? (s.sortDir === 'desc' ? ' ↓' : ' ↑') : '');
        })));
      }

      function renderTable(norm) {
        // 动态列兜底：行里出现契约外列时补进列头
        norm.rows.forEach(function (row) {
          Object.keys(row).forEach(function (k) {
            if (s.columns.indexOf(k) < 0) s.columns.push(k);
          });
        });
        IU.renderResultTable(tableBox, {
          columns: s.columns,
          rows: norm.rows,
          colTypes: s.colTypes,
          emptyText: '无数据（当前筛选条件下没有命中行）',
          onCellClick: function (col, value) {
            IU.DetailPanel.open(col, value);
          }
        });
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
            '每页 ' + PAGE_SIZE + ' 行 · keyset: ' + (s.nextCursor ? '有下一页游标' : '无下一页')));
      }

      /* 首次渲染即拉第一页 */
      fetchPage(currentWhereSafe());

      function currentWhereSafe() {
        var r = s.builder.getWhere();
        return r.ok ? r.where : [];
      }
    }
  };
})();
