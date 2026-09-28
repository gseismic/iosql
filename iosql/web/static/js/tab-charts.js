/* =====================================================================
 * iosql Web · tab-charts.js
 * Charts：kind(line|candle) + 数值列 + symbol/start/end/buckets/对数轴
 * → GET /chart → uPlot 渲染。
 * candle 为 v1 近似方案：OHLC 四线叠加（uPlot 无内置蜡烛图），图下标注。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var OHLC_LONG = ['open', 'high', 'low', 'close'];
  var OHLC_SHORT = ['o', 'h', 'l', 'c'];
  var SERIES_COLORS = { open: '#9aa4ba', high: '#46c982', low: '#ff6b6b', close: '#ffb454' };
  /* 时间列名启发式：与 tab-data.js 一致；时间列不该当数值列画（PLAN-064 D7） */
  var TS_NAME_RE = /^(ts|time|timestamp|datetime)$|(_ts|_time|_at)$/i;

  var st = null;
  var chart = null; // uPlot 实例（单例，重绘前销毁）
  var resizeHandler = null; // resize 监听引用：重渲染前移除，避免监听器累积

  function resolveTimeCol(spec) {
    var t = spec && spec.time;
    if (!t) return null;
    if (typeof t === 'string') return t;
    if (typeof t === 'object') return IU.pick(t, 'column', 'col', 'field', 'name') || null;
    return null;
  }

  function isTimeLikeCol(c, colTypes, spec) {
    if (resolveTimeCol(spec) === c) return true;
    var ty = String(colTypes[c] || '').toLowerCase();
    return ty.indexOf('int') >= 0 && TS_NAME_RE.test(c);
  }

  function ensureState(ctx) {
    if (st && st.table === ctx.table) return st;
    var spec = ctx.spec || {};
    var colTypes = (spec.columns && typeof spec.columns === 'object') ? spec.columns : {};
    var numericCols = Object.keys(colTypes).filter(function (c) {
      return IU.fmt.NUMERIC_RE.test(String(colTypes[c])) && !isTimeLikeCol(c, colTypes, spec);
    });
    st = {
      table: ctx.table,
      numericCols: numericCols,
      ohlcCols: OHLC_LONG.filter(function (c) { return colTypes[c]; }).length === 4
        ? OHLC_LONG
        : (OHLC_SHORT.filter(function (c) { return colTypes[c]; }).length === 4 ? OHLC_SHORT : null),
      kind: 'line',
      col: numericCols[0] || '',   // 默认第一个非时间数值列（D7 修正）
      symbol: '', start: '', end: '', buckets: '2000', log: false
    };
    return st;
  }

  function destroyChart() {
    if (chart) {
      try { chart.destroy(); } catch (e) { /* 已销毁 */ }
      chart = null;
    }
  }

  function cfgField(labelText, control) {
    return h('label', { style: { display: 'flex', flexDirection: 'column', gap: '3px', fontSize: '11px', color: 'var(--tx2)' } },
      labelText, control);
  }

  IU.tabs = IU.tabs || {};

  IU.tabs.charts = {
    id: 'charts',
    label: 'Charts',

    render: function (root, ctx) {
      var s = ensureState(ctx);
      destroyChart();
      IU.clear(root);

      if (!s.numericCols.length) {
        root.append(h('div', { class: 'card' },
          h('div', { class: 'empty-state' }, '契约中未识别数值列，无法绘图')));
        return;
      }

      /* --- 配置行 --- */
      var kindSel = h('select', { class: 'input' },
        h('option', { value: 'line', selected: s.kind === 'line' }, 'line 折线'),
        h('option', { value: 'candle', selected: s.kind === 'candle' }, 'candle K线(近似)'));
      kindSel.addEventListener('change', function (e) { s.kind = e.target.value; });

      var colSel = h('select', { class: 'input' }, s.numericCols.map(function (c) {
        return h('option', { value: c, selected: c === s.col }, c);
      }));
      colSel.addEventListener('change', function (e) { s.col = e.target.value; });

      var symbolIn = h('input', { class: 'input mono', placeholder: 'symbol（可选）', value: s.symbol, autocomplete: 'off' });
      symbolIn.addEventListener('input', function (e) { s.symbol = e.target.value; });

      var startIn = h('input', {
        class: 'input mono', placeholder: 'start（2025-01-01 或毫秒）', value: s.start, autocomplete: 'off',
        title: '支持 2025-01-01 / 2025-01-01 10:30 / 毫秒时间戳（PLAN-064 D3）'
      });
      startIn.addEventListener('input', function (e) { s.start = e.target.value; });

      var endIn = h('input', {
        class: 'input mono', placeholder: 'end（2025-04-30 或毫秒）', value: s.end, autocomplete: 'off',
        title: '支持 2025-04-30 / 2025-04-30 16:00 / 毫秒时间戳'
      });
      endIn.addEventListener('input', function (e) { s.end = e.target.value; });

      var bucketsIn = h('input', { class: 'input mono', type: 'text', value: s.buckets, style: { width: '90px' } });
      bucketsIn.addEventListener('input', function (e) { s.buckets = e.target.value; });

      var logChk = h('input', { type: 'checkbox', id: 'chart-log' });
      logChk.checked = s.log;
      logChk.addEventListener('change', function () { s.log = logChk.checked; });

      root.append(h('div', { class: 'card' },
        h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '图表配置')),
        h('div', { class: 'chart-config' },
          cfgField('图型', kindSel),
          cfgField('数值列', colSel),
          cfgField('symbol', symbolIn),
          cfgField('start', startIn),
          cfgField('end', endIn),
          cfgField('buckets', bucketsIn),
          h('label', { for: 'chart-log', style: { display: 'flex', gap: '6px', alignItems: 'center', cursor: 'pointer', fontSize: '12px', color: 'var(--tx1)', paddingBottom: '6px' } },
            logChk, '对数轴'),
          h('button', { class: 'btn primary', onclick: draw }, '绘制'))));

      var chartBox = h('div', { class: 'chart-box' });
      root.append(chartBox);

      // 先清理上一次渲染留下的 resize 监听，再挂新的（防止反复切换 Tab 累积）
      if (resizeHandler) { window.removeEventListener('resize', resizeHandler); }
      resizeHandler = onResize;
      window.addEventListener('resize', onResize);

      function onResize() {
        if (chart && chartBox.isConnected) {
          chart.setSize({ width: Math.max(chartBox.clientWidth - 24, 320), height: 360 });
        } else {
          window.removeEventListener('resize', onResize);
          if (resizeHandler === onResize) resizeHandler = null;
        }
      }

      /* --- 拉取并渲染 --- */
      function draw() {
        destroyChart();
        IU.clear(chartBox);
        chartBox.append(h('div', { class: 'chart-empty' }, h('span', { class: 'spinner' }), ' 拉取数据…'));

        var base = '/api/table/' + encodeURIComponent(ctx.table) + '/chart';

        if (s.kind === 'candle') {
          drawCandle(base);
        } else {
          fetchSeries(base, s.col).then(function (pts) {
            renderLine([{ label: s.col, color: '#45c6ff', pts: pts }], false);
          }).catch(function (err) {
            renderEmpty(chartErrText(err));
          });
        }
      }

      /* 错误盒透出服务端 hint（如扫描预算超限的"缩小时间窗"指引） */
      function chartErrText(err) {
        var msg = '图表数据加载失败';
        if (err && err.hint) msg += '：' + err.hint;
        else if (err && err.message) msg += '：' + err.message;
        return msg;
      }

      /* candle：对 o/h/l/c（或 open/high/low/close）四列各拉一条序列叠加 */
      function drawCandle(base) {
        if (!s.ohlcCols) {
          IU.toast('契约未识别 OHLC 四列，退化为所选列折线', 'warn');
          fetchSeries(base, s.col).then(function (pts) {
            renderLine([{ label: s.col, color: '#45c6ff', pts: pts }], false);
          }).catch(function (err) { renderEmpty(chartErrText(err)); });
          return;
        }
        Promise.all(s.ohlcCols.map(function (c) { return fetchSeries(base, c); }))
          .then(function (r) {
            renderLine([
              { label: 'open (' + s.ohlcCols[0] + ')', color: SERIES_COLORS.open, pts: r[0] },
              { label: 'high (' + s.ohlcCols[1] + ')', color: SERIES_COLORS.high, pts: r[1] },
              { label: 'low (' + s.ohlcCols[2] + ')', color: SERIES_COLORS.low, pts: r[2] },
              { label: 'close (' + s.ohlcCols[3] + ')', color: SERIES_COLORS.close, pts: r[3] }
            ], true);
          })
          .catch(function (err) { renderEmpty(chartErrText(err)); });
      }

      function fetchSeries(base, col) {
        var qs = '?col=' + encodeURIComponent(col);
        if (String(s.symbol).trim() !== '') qs += '&symbol=' + encodeURIComponent(s.symbol.trim());
        var startMs = IU.parseTimeInput(s.start);
        var endMs = IU.parseTimeInput(s.end);
        if (String(s.start).trim() !== '') {
          if (!startMs.ok) { IU.toast('start 无法解析：试试 2025-01-01 或毫秒时间戳', 'warn'); return Promise.reject(new Error('bad start')); }
          qs += '&start=' + encodeURIComponent(String(startMs.ms));
        }
        if (String(s.end).trim() !== '') {
          if (!endMs.ok) { IU.toast('end 无法解析：试试 2025-04-30 或毫秒时间戳', 'warn'); return Promise.reject(new Error('bad end')); }
          qs += '&end=' + encodeURIComponent(String(endMs.ms));
        }
        var b = parseInt(s.buckets, 10);
        qs += '&buckets=' + (Number.isFinite(b) && b > 0 ? b : 2000);
        return IU.api(base + qs).then(function (resp) { return normalizePoints(resp); });
      }

      /* 响应防御式归一化：容忍 points/series/data/buckets 键名；
       * 点形态容忍 {x,y} / [x,y] / {t|ts|time, v|value}；
       * data 为 [xs, ys] 双数组时按 uPlot 原生形态 zip。 */
      function normalizePoints(resp) {
        var raw = resp;
        if (resp && typeof resp === 'object' && !Array.isArray(resp)) {
          /* 契约形态优先：{kind:'line', x:[], y[]} 平行双数组 */
          if (Array.isArray(resp.x) && Array.isArray(resp.y)) {
            return zipXY(resp.x, resp.y);
          }
          raw = IU.pick(resp, 'points', 'series', 'data', 'buckets', 'items') || [];
          if (Array.isArray(raw) && raw.length === 2 && Array.isArray(raw[0]) && Array.isArray(raw[1])) {
            return zipXY(raw[0], raw[1]);
          }
        }
        if (!Array.isArray(raw)) return [];
        return raw.map(function (p) {
          if (Array.isArray(p)) return { x: p[0], y: p[1] };
          if (p && typeof p === 'object') {
            return {
              x: IU.pick(p, 'x', 't', 'ts', 'time', 'bucket'),
              y: IU.pick(p, 'y', 'v', 'value', 'avg', 'mean')
            };
          }
          return null;
        }).filter(function (p) { return p && p.x !== undefined && p.y !== undefined; });
      }

      function zipXY(xs, ys) {
        var out = [];
        for (var i = 0; i < Math.min(xs.length, ys.length); i++) out.push({ x: xs[i], y: ys[i] });
        return out;
      }

      function renderEmpty(msg) {
        destroyChart();
        IU.clear(chartBox);
        chartBox.append(h('div', { class: 'chart-empty' }, msg || '暂无数据（调整筛选或确认表内有数据）'));
      }

      /* uPlot 渲染：多序列共用第一序列横轴；毫秒时间戳自动转秒启用时间轴 */
      function renderLine(seriesList, isCandle) {
        var first = seriesList[0];
        if (!first || !first.pts.length) { renderEmpty(); return; }

        var xsRaw = first.pts.map(function (p) { return Number(p.x); });
        var maxAbs = xsRaw.reduce(function (m, v) { return Math.max(m, Math.abs(v)); }, 0);
        var isTime = maxAbs > 1e11; // 秒级 epoch ≈1e9，毫秒级 ≈1e12
        var xs = isTime ? xsRaw.map(function (v) { return v / 1000; }) : xsRaw;

        var seriesDefs = seriesList.map(function (sl) {
          return { label: sl.label, stroke: sl.color, width: 1.5, points: { show: sl.pts.length < 40 } };
        });

        // 各序列按 x 对齐到第一序列横轴；对数轴下非正值断开为 null
        var data = [xs].concat(seriesList.map(function (sl) {
          var byX = new Map();
          sl.pts.forEach(function (p) { byX.set(Number(p.x), p.y); });
          return xs.map(function (x) {
            var v = byX.get(isTime ? x * 1000 : x);
            if (v === undefined) return null;
            var n = Number(v);
            if (!Number.isFinite(n)) return null;
            if (s.log && n <= 0) return null;
            return n;
          });
        }));

        destroyChart();
        IU.clear(chartBox);
        var mount = h('div');
        chartBox.append(mount);
        if (isCandle) {
          chartBox.append(h('div', { class: 'chart-note' },
            '⚠ K线为 v1 近似渲染：uPlot 无内置蜡烛图，此处以 open/high/low/close 四条线叠加显示。'));
        }

        try {
          // 时间轴交给 uPlot 原生渲染（可读日期）；仅非时间横轴才自定义整数刻度
          // （PLAN-064 Q3：原先强制 Math.round 覆盖导致 X 轴显示原始毫秒）。
          // 注意：isTime 时不传 space/values 键——旧版 uPlot 对 null 轴参数处理不佳
          var xAxis = { stroke: '#5f6a82', grid: { stroke: 'rgba(255,255,255,.06)' } };
          if (!isTime) {
            xAxis.space = 80;
            xAxis.values = function (u, ticks) {
              return ticks.map(function (v) { return v == null ? '' : String(Math.round(v)); });
            };
          }
          chart = new uPlot({
            width: Math.max(chartBox.clientWidth - 24, 320),
            height: 360,
            scales: {
              x: { time: isTime },
              y: s.log ? { distr: 3 } : {}
            },
            series: [{}].concat(seriesDefs),
            legend: { show: true },
            axes: [
              xAxis,
              { stroke: '#5f6a82', grid: { stroke: 'rgba(255,255,255,.06)' },
                values: function (u, ticks) {
                  return ticks.map(function (v) { return v == null ? '' : Number(v).toLocaleString('en-US', { maximumFractionDigits: 2 }); });
                } }
            ]
          }, data, mount);
        } catch (e) {
          IU.clear(chartBox);
          chartBox.append(h('div', { class: 'chart-empty' }, '渲染失败：' + e.message));
        }
      }

      /* 进入 Tab 自动绘制一次 */
      draw();
    }
  };
})();
