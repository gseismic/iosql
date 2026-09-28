/* =====================================================================
 * iosql Web · tab-charts.js
 * Charts：kind(line|candle) + 数值列 + symbol/start/end/buckets/对数轴
 * → GET /chart → uPlot 渲染。
 * candle 为 v1 近似方案：OHLC 四线叠加（uPlot 无内置蜡烛图），图下标注。
 * PLAN-071 可视区按需加载：拖拽缩放/双击复位 → setScale hook → 防抖后
 * 仅按可视区 start/end 重查（buckets 不变，放大即局部高分辨率），
 * setData 原地更新不重建实例；双击复位判定为全范围重查。
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
    return h('label', { style: { display: 'flex', flexDirection: 'column', gap: '3px', fontSize: '12px', color: 'var(--tx2)' } },
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
          h('label', { for: 'chart-log', style: { display: 'flex', gap: '6px', alignItems: 'center', cursor: 'pointer', fontSize: '13px', color: 'var(--tx1)', paddingBottom: '6px' } },
            logChk, '对数轴'),
          h('button', { class: 'btn primary', onclick: draw }, '绘制'))));

      var chartBox = h('div', { class: 'chart-box' });
      root.append(chartBox);

      /* ---- 可视区按需加载状态（PLAN-071，render 作用域）----
       * active：当前图序列配置（重查时按它决定拉哪几列）；
       * viewXTime：当前图 x 是否毫秒时间轴（视口秒值 → 毫秒换算）；
       * viewApplied：已加载数据对应的视口（去重防回环）；
       * fullScale：初始全范围（双击复位判定）；viewSeq：过期响应守卫；
       * suppressScale：setData 内部 setScale 抑制标志。 */
      var active = null;
      var viewXTime = false;
      var viewApplied = null;
      var fullScale = null;
      var viewTimer = null;
      var viewSeq = 0;
      var suppressScale = false;
      var viewNote = h('div', { class: 'chart-note' });

      function bucketCount() {
        var b = parseInt(s.buckets, 10);
        return Number.isFinite(b) && b > 0 ? b : 2000;
      }

      function validWindow(w) {
        return !!(w && Number.isFinite(w.min) && Number.isFinite(w.max));
      }

      function closeWindow(a, b) {
        if (!validWindow(a) || !validWindow(b)) return false;
        var span = Math.max(Math.abs(a.max - a.min), Math.abs(b.max - b.min), 1e-9);
        // 容差取约 2.5 个桶宽：setData(resetScales) 后 uPlot 重定刻度会使
        // 数据范围相对请求窗口内缩约一个桶宽，容差小于桶宽会形成
        // "重查→内缩→再重查"的回环；小于 0.1% 视口的缩放无意义
        var tol = span * 2.5 / bucketCount();
        return Math.abs(a.min - b.min) <= tol &&
               Math.abs(a.max - b.max) <= tol;
      }

      function noteText(txt) {
        IU.clear(viewNote);
        if (txt) viewNote.append(String(txt));
      }

      function noteLoading() {
        IU.clear(viewNote);
        viewNote.append(h('span', { class: 'spinner' }), ' 可视区加载中…');
      }

      function noteRange(startMs, endMs, count, isFull) {
        var fmt = viewXTime ? function (v) { return IU.fmt.ts(v); } : String;
        noteText((isFull ? '全范围' : '可视区 ') + fmt(startMs) + ' ~ ' +
          fmt(endMs) + ' · ' + count + ' 点' +
          (isFull ? '（拖拽缩放 · 双击复位）' : ''));
      }

      function resetViewState() {
        if (viewTimer) { clearTimeout(viewTimer); viewTimer = null; }
        viewSeq += 1;           // 使在途视口请求全部过期
        viewApplied = null;
        fullScale = null;
        active = null;
      }

      /* 由序列点集构建 uPlot data：第一序列 x 为主轴，其余按 x 对齐；
       * 对数轴下非正值断开为 null。初始渲染与视口重查共用（PLAN-071）。 */
      function buildUData(list) {
        var f = list[0];
        var raw = f.pts.map(function (p) { return Number(p.x); });
        var sx = viewXTime ? raw.map(function (v) { return v / 1000; }) : raw;
        return [sx].concat(list.map(function (sl) {
          var byX = new Map();
          sl.pts.forEach(function (p) { byX.set(Number(p.x), p.y); });
          return sx.map(function (x) {
            var v = byX.get(viewXTime ? x * 1000 : x);
            if (v === undefined) return null;
            var n = Number(v);
            if (!Number.isFinite(n)) return null;
            if (s.log && n <= 0) return null;
            return n;
          });
        }));
      }

      function scheduleViewFetch(min, max) {
        if (viewTimer) clearTimeout(viewTimer);
        viewTimer = setTimeout(function () {
          viewTimer = null;
          fetchView(min, max);
        }, 250);
      }

      function fetchView(min, max) {
        if (!chart || !chartBox.isConnected || !active || !validWindow(viewApplied)) return;
        var win = { min: min, max: max };
        if (closeWindow(win, viewApplied)) return;      // 视口未实质变化
        var isFullReset = closeWindow(win, fullScale);  // 双击复位 → 全范围重查
        var startMs = viewXTime ? Math.floor(min * 1000) : Math.floor(min);
        var endMs = viewXTime ? Math.ceil(max * 1000) : Math.ceil(max);
        var winMs = isFullReset ? null : [startMs, endMs];
        var seq = ++viewSeq;
        var base = '/api/table/' + encodeURIComponent(ctx.table) + '/chart';
        noteLoading();
        Promise.all(active.map(function (sr) {
          return fetchSeries(base, sr.col, winMs);
        })).then(function (lists) {
          if (seq !== viewSeq || !chart || !chartBox.isConnected) return;
          var seriesList = active.map(function (sr, i) {
            return { label: sr.label, color: sr.color, pts: lists[i] };
          });
          if (!seriesList[0].pts.length) { noteText('可视区无数据'); return; }
          suppressScale = true;
          try {
            chart.setData(buildUData(seriesList), true);
            // 记录"请求窗口"而非 chart.scales：resetScales 的刻度提交可能
            // 滞后于 setData 返回，用滞后的 scale 做去重会形成回环
            viewApplied = { min: win.min, max: win.max };
          } finally {
            suppressScale = false;
          }
          noteRange(startMs, endMs, seriesList[0].pts.length, isFullReset);
        }).catch(function (err) {
          if (seq !== viewSeq) return;
          noteText('可视区加载失败：' +
            (err && err.hint ? err.hint : (err && err.message) || ''));
        });
      }

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
        resetViewState();
        chartBox.append(h('div', { class: 'chart-empty' }, h('span', { class: 'spinner' }), ' 拉取数据…'));

        var base = '/api/table/' + encodeURIComponent(ctx.table) + '/chart';

        if (s.kind === 'candle') {
          drawCandle(base);
        } else {
          active = [{ label: s.col, color: '#45c6ff', col: s.col }];
          fetchSeries(base, s.col, null).then(function (pts) {
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
          active = [{ label: s.col, color: '#45c6ff', col: s.col }];
          fetchSeries(base, s.col, null).then(function (pts) {
            renderLine([{ label: s.col, color: '#45c6ff', pts: pts }], false);
          }).catch(function (err) { renderEmpty(chartErrText(err)); });
          return;
        }
        var longNames = { o: 'open', h: 'high', l: 'low', c: 'close' };
        active = s.ohlcCols.map(function (c) {
          return {
            label: longNames[c] + ' (' + c + ')',
            color: SERIES_COLORS[longNames[c]],
            col: c,
          };
        });
        Promise.all(active.map(function (sr) {
          return fetchSeries(base, sr.col, null);
        }))
          .then(function (r) {
            renderLine(active.map(function (sr, i) {
              return { label: sr.label, color: sr.color, pts: r[i] };
            }), true);
          })
          .catch(function (err) { renderEmpty(chartErrText(err)); });
      }

      /* 拉取一条序列：winMs 为 null 时用配置输入的 start/end，
       * 否则用可视区毫秒窗口（PLAN-071，缩放重查走此参数） */
      function fetchSeries(base, col, winMs) {
        var qs = '?col=' + encodeURIComponent(col);
        if (String(s.symbol).trim() !== '') qs += '&symbol=' + encodeURIComponent(s.symbol.trim());
        if (winMs) {
          qs += '&start=' + encodeURIComponent(String(winMs[0]));
          qs += '&end=' + encodeURIComponent(String(winMs[1]));
        } else {
          var startStr = String(s.start).trim();
          var endStr = String(s.end).trim();
          if (startStr !== '') {
            var startMs = IU.parseTimeInput(s.start);
            if (!startMs.ok) { IU.toast('start 无法解析：试试 2025-01-01 或毫秒时间戳', 'warn'); return Promise.reject(new Error('bad start')); }
            qs += '&start=' + encodeURIComponent(String(startMs.ms));
          }
          if (endStr !== '') {
            var endMs = IU.parseTimeInput(s.end);
            if (!endMs.ok) { IU.toast('end 无法解析：试试 2025-04-30 或毫秒时间戳', 'warn'); return Promise.reject(new Error('bad end')); }
            qs += '&end=' + encodeURIComponent(String(endMs.ms));
          }
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
        viewXTime = maxAbs > 1e11; // 秒级 epoch ≈1e9，毫秒级 ≈1e12

        var seriesDefs = seriesList.map(function (sl) {
          return { label: sl.label, stroke: sl.color, width: 1.5, points: { show: sl.pts.length < 40 } };
        });

        destroyChart();
        IU.clear(chartBox);
        var mount = h('div');
        chartBox.append(mount);
        if (isCandle) {
          chartBox.append(h('div', { class: 'chart-note' },
            '⚠ K线为 v1 近似渲染：uPlot 无内置蜡烛图，此处以 open/high/low/close 四条线叠加显示。'));
        }
        chartBox.append(viewNote);

        try {
          // 时间轴交给 uPlot 原生渲染（可读日期）；仅非时间横轴才自定义整数刻度
          // （PLAN-064 Q3：原先强制 Math.round 覆盖导致 X 轴显示原始毫秒）。
          // 注意：isTime 时不传 space/values 键——旧版 uPlot 对 null 轴参数处理不佳
          var xAxis = { stroke: '#5f6a82', grid: { stroke: 'rgba(255,255,255,.06)' } };
          if (!viewXTime) {
            xAxis.space = 80;
            xAxis.values = function (u, ticks) {
              return ticks.map(function (v) { return v == null ? '' : String(Math.round(v)); });
            };
          }
          chart = new uPlot({
            width: Math.max(chartBox.clientWidth - 24, 320),
            height: 360,
            scales: {
              x: { time: viewXTime },
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
            ],
            hooks: {
              // 拖拽缩放/双击复位后 x 刻度变化 → 防抖重查可视区（PLAN-071）。
              // 首个 setScale（构造期，数据范围）只初始化基准窗口不查询：
              // 此刻 chart.scales.x.min/max 尚未提交（null），不能事后读取
              setScale: [function (u, key) {
                if (key !== 'x' || suppressScale) return;
                var win = { min: u.scales.x.min, max: u.scales.x.max };
                if (!validWindow(viewApplied)) {
                  viewApplied = win;
                  fullScale = win;
                  return;
                }
                scheduleViewFetch(win.min, win.max);
              }]
            }
          }, buildUData(seriesList), mount);
          // 双击复位：uPlot v1.6 无内置缩放复位，手动回到初始全范围，
          // setScale 触发 setScale hook → closeWindow(fullScale) → 全量重查
          mount.addEventListener('dblclick', function () {
            if (chart && validWindow(fullScale)) {
              chart.setScale('x', { min: fullScale.min, max: fullScale.max });
            }
          });
          // 初始"全范围"提示：直接取数据首末点（毫秒），不依赖未提交的 scale
          noteRange(xsRaw[0], xsRaw[xsRaw.length - 1], xsRaw.length, true);
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
