/* =====================================================================
 * iosql UI · tab-query.js
 * Query：条件构造器（复用 Browse 组件）→ /rows 查询、/explain 计划面板；
 * has_time 表额外提供快捷区（start/end/date/session/symbols → /query）
 * 与 latest 截面区；无 time 契约时隐藏快捷区。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var st = null;

  function ensureState(ctx) {
    if (st && st.table === ctx.table) return st;
    var spec = ctx.spec || {};
    var colTypes = (spec.columns && typeof spec.columns === 'object') ? spec.columns : {};
    var columns = Object.keys(colTypes);
    st = {
      table: ctx.table,
      builder: new IU.ConditionBuilder(columns, colTypes),
      limit: '200'
    };
    return st;
  }

  IU.tabs = IU.tabs || {};

  IU.tabs.query = {
    id: 'query',
    label: 'Query',

    render: function (root, ctx) {
      var s = ensureState(ctx);
      var base = '/api/table/' + encodeURIComponent(ctx.table);
      var spec = ctx.spec || {};
      var hasTime = !!spec.time;

      IU.clear(root);

      /* --- 构造器 + 查询/Explain --- */
      var planBox = h('div', { class: 'card hidden' });
      var resultBox = h('div');
      root.append(
        h('div', { class: 'card' },
          s.builder.el,
          h('div', { class: 'toolbar' },
            h('button', { class: 'btn primary', onclick: runRows }, '查询'),
            h('button', { class: 'btn', onclick: runExplain }, 'Explain'),
            h('label', { style: { display: 'flex', gap: '6px', alignItems: 'center', fontSize: '12px', color: 'var(--tx1)' } },
              'limit',
              h('input', {
                class: 'input mono', type: 'text', value: s.limit, style: { width: '80px' },
                oninput: function (e) { s.limit = e.target.value; }
              })))),
        planBox,
        resultBox);

      /* --- timeseries 快捷区（能力门控：仅 has_time 契约显示） --- */
      if (hasTime) root.append(buildQuickArea(base));

      function getWhere() {
        var r = s.builder.getWhere();
        if (!r.ok) { IU.toast(r.msg, 'err'); return undefined; }
        return r.where;
      }

      function limitNum() {
        var n = parseInt(s.limit, 10);
        return Number.isFinite(n) && n > 0 ? n : 200;
      }

      function runRows() {
        var where = getWhere();
        if (where === undefined) return;
        IU.clear(resultBox);
        resultBox.append(loadingCard());
        IU.api(base + '/rows', {
          method: 'POST',
          body: { where: where || [], order_by: defaultOrder(spec), limit: limitNum() }
        }).then(function (resp) {
          renderResultCard(resultBox, resp, '查询结果');
        }).catch(function () {
          IU.clear(resultBox);
          resultBox.append(h('div', { class: 'empty-state' }, '查询失败'));
        });
      }

      function runExplain() {
        var where = getWhere();
        if (where === undefined) return;
        IU.clear(planBox);
        planBox.classList.remove('hidden');
        planBox.append(h('div', { class: 'empty-state' }, h('span', { class: 'spinner' }), ' 计算查询计划…'));
        IU.api(base + '/explain', {
          method: 'POST',
          body: { where: where || [], order_by: defaultOrder(spec), limit: limitNum() }
        }).then(function (plan) {
          IU.renderPlanPanel(planBox, plan);
        }).catch(function () {
          IU.clear(planBox);
          planBox.append(h('div', { class: 'empty-state' }, '计划获取失败'));
        });
      }
    }
  };

  /* 默认排序：time → order_key → pk[0] → 不传 */
  function defaultOrder(spec) {
    var t = spec.time;
    var timeCol = t && typeof t === 'object' ? IU.pick(t, 'column', 'col', 'field', 'name') : (typeof t === 'string' ? t : null);
    if (timeCol) return timeCol;
    if (spec.order_key) return spec.order_key;
    if (Array.isArray(spec.pk) && spec.pk[0]) return spec.pk[0];
    return '';
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
        h('span', { class: 'faint mono', style: { fontSize: '11px' } }, norm.rows.length + ' 行')),
      h('div', null)));
    var slot = box.lastChild.lastChild;
    IU.renderResultTable(slot, {
      columns: norm.columns,
      rows: norm.rows,
      emptyText: '无数据',
      onCellClick: function (col, value) { IU.DetailPanel.open(col, value); }
    });
  }

  /* ---------- timeseries 快捷区 ---------- */
  function buildQuickArea(base) {
    var queryResult = h('div');
    var latestResult = h('div');

    /* 通用小输入框：label 与 input 包裹在同一单元格 */
    function field(labelText, placeholder, mono) {
      var input = h('input', {
        class: 'input' + (mono ? ' mono' : ''), placeholder: placeholder || '',
        autocomplete: 'off', spellcheck: 'false'
      });
      return {
        node: h('label', { style: { display: 'flex', flexDirection: 'column', gap: '3px', fontSize: '11px', color: 'var(--tx2)' } },
          labelText, input),
        input: input
      };
    }

    var fStart = field('start', '如 1735689600000', true);
    var fEnd = field('end', '如 1735776000000', true);
    var fDate = field('date', '如 2026-01-06');
    var fSession = field('session', '交易日，如 2026-01-06');
    var fSymbolsQ = field('symbols', '逗号分隔：AAPL,MSFT');

    var btnQuery = h('button', {
      class: 'btn primary',
      onclick: function () {
        var body = {};
        if (fStart.input.value.trim() !== '') body.start = IU.fmt.coerce(fStart.input.value, 'integer');
        if (fEnd.input.value.trim() !== '') body.end = IU.fmt.coerce(fEnd.input.value, 'integer');
        if (fDate.input.value.trim() !== '') body.date = fDate.input.value.trim();
        if (fSession.input.value.trim() !== '') body.session = fSession.input.value.trim();
        if (fSymbolsQ.input.value.trim() !== '') body.symbols = splitSymbols(fSymbolsQ.input.value);

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

    var fSymbolsL = field('symbols', '逗号分隔：AAPL,MSFT');
    var fAsOf = field('as_of', '毫秒时间戳', true);
    var fBy = field('by', '分组列，逗号分隔：symbol,timeframe');
    var fCols = field('columns', '返回列，逗号分隔（可空）');

    var btnLatest = h('button', {
      class: 'btn primary',
      onclick: function () {
        var body = {};
        if (fSymbolsL.input.value.trim() !== '') body.symbols = splitSymbols(fSymbolsL.input.value);
        if (fAsOf.input.value.trim() !== '') body.as_of = IU.fmt.coerce(fAsOf.input.value, 'integer');
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

    function splitSymbols(raw) {
      return String(raw).split(/[,\s]+/).map(function (x) { return x.trim(); }).filter(Boolean);
    }

    return h('div', { class: 'card section-gap' },
      h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '时序快捷查询'),
        IU.badge('timeseries', 'info')),
      h('div', { class: 'quick-grid' },
        fStart.node, fEnd.node, fDate.node, fSession.node, fSymbolsQ.node),
      h('div', { class: 'toolbar' }, btnQuery),
      queryResult,
      h('div', { class: 'card-title section-gap' }, h('span', { class: 'micro-label' }, 'Latest 最新截面')),
      h('div', { class: 'quick-grid' },
        fSymbolsL.node, fAsOf.node, fBy.node, fCols.node),
      h('div', { class: 'toolbar' }, btnLatest),
      latestResult);
  }
})();
