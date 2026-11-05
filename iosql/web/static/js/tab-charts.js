/* =====================================================================
 * iosql Web · tab-charts.js
 * Charts：kind(line|candle) + 数值列 + symbol/start/end/对数轴
 * → /chart NDJSON 全量原始点 → uPlot 渲染。
 * candle 为 v1 近似方案：OHLC 四线叠加（uPlot 无内置蜡烛图），图下标注。
 * PLAN-071 可视区按需加载：拖拽缩放/双击复位 → setScale hook → 防抖后
 * 仅按可视区 start/end 重查（可视区内不抽样），
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

  function symbolColor(index) {
    return 'hsl(' + Math.round((index * 137.508) % 360) + ', 72%, 62%)';
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
      symbol: '', start: '', end: '', log: false
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
      IU.tabs.charts.cleanup = null;
      var s = ensureState(ctx);
      destroyChart();
      IU.clear(root);

      if (!s.numericCols.length) {
        root.append(h('div', { class: 'card' },
          h('div', { class: 'empty-state' }, '契约中未识别可绘制的数值列，请选择包含数值列的表。')));
        return;
      }

      /* --- 配置行 --- */
      var kindSel = h('select', { class: 'input' },
        h('option', { value: 'line', selected: s.kind === 'line' }, 'line 折线'),
        h('option', {
          value: 'candle', disabled: !s.ohlcCols, selected: s.kind === 'candle'
        }, s.ohlcCols ? 'OHLC 四线（无蜡烛实体）' : 'OHLC 四线（该表无 OHLC）'));
      kindSel.addEventListener('change', function (e) { s.kind = e.target.value; });

      var colSel = h('select', { class: 'input' }, s.numericCols.map(function (c) {
        return h('option', { value: c, selected: c === s.col }, c);
      }));
      colSel.addEventListener('change', function (e) { s.col = e.target.value; });

      var symbolIn = h('input', {
        class: 'input mono', placeholder: '全部标的，逗号分隔',
        title: '留空时全部标的分别成线；多个标的用逗号分隔',
        value: s.symbol, autocomplete: 'off'
      });
      symbolIn.addEventListener('input', function (e) { s.symbol = e.target.value; });

      var startIn = h('input', {
        class: 'input mono', placeholder: '2025-01-01 或毫秒', value: s.start, autocomplete: 'off',
        title: '支持 2025-01-01 / 2025-01-01 10:30 / 毫秒时间戳（PLAN-064 D3）'
      });
      startIn.addEventListener('input', function (e) { s.start = e.target.value; });

      var endIn = h('input', {
        class: 'input mono', placeholder: '2025-04-30 或毫秒', value: s.end, autocomplete: 'off',
        title: '支持 2025-04-30 / 2025-04-30 16:00 / 毫秒时间戳'
      });
      endIn.addEventListener('input', function (e) { s.end = e.target.value; });

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
          h('label', { for: 'chart-log', style: { display: 'flex', gap: '6px', alignItems: 'center', cursor: 'pointer', fontSize: '13px', color: 'var(--tx1)', paddingBottom: '6px' } },
            logChk, '对数轴'),
          h('button', { class: 'btn primary', onclick: draw }, '绘制')),
        h('div', { class: 'chart-time-hint' }, '无时区的日期和时间按浏览器本地时间解析；也可输入毫秒或带时区的 ISO 时间。')));

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
      var requestControllers = [];
      var loadingStatus = null;
      var drawVersion = 0;

      function validWindow(w) {
        return !!(w && Number.isFinite(w.min) && Number.isFinite(w.max));
      }

      function closeWindow(a, b) {
        if (!validWindow(a) || !validWindow(b)) return false;
        var span = Math.max(Math.abs(a.max - a.min), Math.abs(b.max - b.min), 1e-9);
        // 忽略不到 0.1% 视口的变化；原始点没有桶宽，不按采样密度放宽容差。
        var tol = span * 0.001;
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

      function noteRange(startMs, endMs, count, seriesList, isFull) {
        var fmt = viewXTime ? function (v) { return IU.fmt.ts(v); } : String;
        var seen = new Set();
        var seriesCounts = [];
        (seriesList || []).forEach(function (sl) {
          var symbol = sl.label.indexOf(' · ') >= 0 ? sl.label.split(' · ')[0] : sl.label;
          if (seen.has(symbol)) return;
          seen.add(symbol);
          seriesCounts.push(symbol + ' ' + Number(sl.xs.length).toLocaleString('zh-CN'));
        });
        noteText((isFull ? '全范围' : '可视区 ') + fmt(startMs) + ' ~ ' +
          fmt(endMs) + ' · ' + Number(count || 0).toLocaleString('zh-CN') +
          ' 条原始记录 · ' + (seriesList || []).length + ' 条序列（无抽样）' +
          (seriesCounts.length ? ' · 各线 ' + seriesCounts.join('，') : '') +
          (isFull ? '（拖拽缩放 · 双击复位）' : ''));
      }

      function resetViewState() {
        if (viewTimer) { clearTimeout(viewTimer); viewTimer = null; }
        requestControllers.forEach(function (controller) { controller.abort(); });
        requestControllers = [];
        viewSeq += 1;           // 使在途视口请求全部过期
        viewApplied = null;
        fullScale = null;
        active = null;
      }

      IU.tabs.charts.cleanup = function () {
        drawVersion++;
        resetViewState();
        if (resizeHandler === onResize) {
          window.removeEventListener('resize', onResize);
          resizeHandler = null;
        }
        destroyChart();
        IU.tabs.charts.cleanup = null;
      };

      /* 合并各 symbol 的 x 序列；union 保留每条序列的所有时间点，其他序列缺值填 null。 */
      function mergeXs(list) {
        var first = list.find(function (sl) { return sl.xs.length > 0; }) || list[0];
        if (!first) return [];
        var same = list.every(function (sl) {
          if (sl.xs.length !== first.xs.length) return false;
          for (var i = 0; i < first.xs.length; i++) {
            if (Number(sl.xs[i]) !== Number(first.xs[i])) return false;
          }
          return true;
        });
        if (same) return first.xs.map(Number);

        var at = list.map(function () { return 0; });
        var union = [];
        while (true) {
          var next = Infinity;
          for (var j = 0; j < list.length; j++) {
            while (at[j] < list[j].xs.length && !Number.isFinite(Number(list[j].xs[at[j]]))) at[j]++;
            if (at[j] < list[j].xs.length) next = Math.min(next, Number(list[j].xs[at[j]]));
          }
          if (next === Infinity) break;
          var maxMultiplicity = 0;
          for (var m = 0; m < list.length; m++) {
            var countAtX = 0;
            while (at[m] + countAtX < list[m].xs.length &&
                   Number(list[m].xs[at[m] + countAtX]) === next) countAtX++;
            maxMultiplicity = Math.max(maxMultiplicity, countAtX);
          }
          for (var repeat = 0; repeat < maxMultiplicity; repeat++) union.push(next);
          for (var k = 0; k < list.length; k++) {
            while (at[k] < list[k].xs.length && Number(list[k].xs[at[k]]) === next) at[k]++;
          }
        }
        return union;
      }

      function alignY(xs, sl) {
        if (xs.length === sl.xs.length && xs.every(function (x, i) { return x === Number(sl.xs[i]); })) {
          if (!s.log) return sl.ys;
          return sl.ys.map(function (v) {
            var n = v == null ? NaN : Number(v);
            return Number.isFinite(n) && n > 0 ? n : null;
          });
        }
        var out = new Array(xs.length).fill(null);
        var i = 0;
        var j = 0;
        while (i < xs.length && j < sl.xs.length) {
          var x = Number(sl.xs[j]);
          if (x < xs[i]) { j++; continue; }
          if (x > xs[i]) { i++; continue; }
          var value = sl.ys[j];
          var n = value == null ? NaN : Number(value);
          out[i] = Number.isFinite(n) && (!s.log || n > 0) ? n : null;
          i++;
          j++;
        }
        return out;
      }

      /* 原始平行数组直接进入 uPlot；避免每个点分配对象，null 指标值保持断线。 */
      function buildUData(list) {
        var raw = mergeXs(list);
        var sx = viewXTime ? raw.map(function (v) { return v / 1000; }) : raw;
        return [sx].concat(list.map(function (sl) { return alignY(raw, sl); }));
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
        requestControllers.forEach(function (controller) { controller.abort(); });
        requestControllers = [];
        noteLoading();
        Promise.all(active.map(function (sr) {
          return fetchSeries(base, sr, winMs);
        })).then(function (lists) {
          if (seq !== viewSeq || !chart || !chartBox.isConnected) return;
          var seriesList = [].concat.apply([], lists);
          seriesList.rowCount = lists.reduce(function (sum, list) { return sum + (list.rowCount || 0); }, 0);
          var firstWithPoints = seriesList.find(function (sr) { return sr.xs.length > 0; });
          if (!firstWithPoints) { noteText('可视区无数据 · 无抽样'); return; }
          suppressScale = true;
          try {
            chart.setData(buildUData(seriesList), true);
            // 记录"请求窗口"而非 chart.scales：resetScales 的刻度提交可能
            // 滞后于 setData 返回，用滞后的 scale 做去重会形成回环
            viewApplied = { min: win.min, max: win.max };
          } finally {
            suppressScale = false;
          }
          noteRange(startMs, endMs, seriesList.rowCount, seriesList, isFullReset);
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
        drawVersion += 1;
        var thisDraw = drawVersion;
        destroyChart();
        IU.clear(chartBox);
        resetViewState();
        loadingStatus = h('div', { class: 'chart-empty' }, h('span', { class: 'spinner' }), ' 流式读取全部数据…');
        chartBox.append(loadingStatus);

        var base = '/api/table/' + encodeURIComponent(ctx.table) + '/chart';

        if (s.kind === 'candle') {
          drawCandle(base, thisDraw);
        } else {
          active = [{ label: s.col, color: '#45c6ff', col: s.col }];
          fetchSeries(base, active[0], null, function (count) {
            if (loadingStatus && loadingStatus.isConnected) loadingStatus.textContent = '流式读取全部数据… ' + Number(count).toLocaleString('zh-CN') + ' 条';
          }).then(function (seriesList) {
            if (thisDraw !== drawVersion) return;
            renderLine(seriesList, false);
          }).catch(function (err) {
            if (thisDraw !== drawVersion || err.name === 'AbortError') return;
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

      /* candle：单次流式请求拉取所有原始 OHLC 记录，避免四次重复扫描。 */
      function drawCandle(base, thisDraw) {
        if (!s.ohlcCols) {
          renderEmpty('当前表没有完整 OHLC 四列，无法绘制 OHLC 线');
          return;
        }
        active = [{ kind: 'candle', columns: s.ohlcCols }];
        fetchSeries(base, active[0], null, function (count) {
          if (loadingStatus && loadingStatus.isConnected) loadingStatus.textContent = '流式读取全部 OHLC… ' + Number(count).toLocaleString('zh-CN') + ' 条';
        }).then(function (seriesList) {
          if (thisDraw !== drawVersion) return;
          renderLine(seriesList, true);
        }).catch(function (err) {
          if (thisDraw !== drawVersion || err.name === 'AbortError') return;
          renderEmpty(chartErrText(err));
        });
      }

      /* 拉取一个图表来源。流式响应只包含窗口内的原始行，不做抽样。 */
      function fetchSeries(base, source, winMs, onProgress) {
        var col = source.kind === 'candle' ? source.columns[3] : source.col;
        var qs = '?col=' + encodeURIComponent(col);
        if (source.kind === 'candle') qs += '&kind=candle';
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
        var controller = new AbortController();
        requestControllers.push(controller);
        return readChartStream(base + qs, source, controller.signal, onProgress)
          .finally(function () {
            requestControllers = requestControllers.filter(function (item) { return item !== controller; });
          });
      }

      async function readChartStream(url, source, signal, onProgress) {
        var response = await fetch(url, { signal: signal });
        if (!response.ok) {
          var body = null;
          try { body = await response.json(); } catch (e) { /* 错误响应可能不是 JSON */ }
          var err = new Error((body && body.detail) || ('HTTP ' + response.status));
          err.hint = body && body.hint;
          throw err;
        }
        if (!response.body || !response.body.getReader) {
          throw new Error('当前浏览器不支持流式图表数据读取');
        }

        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var meta = null;
        var doneEvent = null;
        var received = 0;
        var seriesMap = new Map();
        var symbolIndices = new Map();
        var longNames = { o: 'open', h: 'high', l: 'low', c: 'close' };
        var candleNames = source.kind === 'candle'
          ? source.columns.map(function (column) {
              var shortName = column.toLowerCase();
              return longNames[shortName] || column.toLowerCase();
            })
          : null;

        function getSeries(symbolValue, suffix, color, index) {
          var symbol = symbolValue == null ? '' : String(symbolValue);
          var key = symbol + '\u0000' + (suffix || '');
          var item = seriesMap.get(key);
          if (!item) {
            var label = symbol ? symbol + ' · ' + suffix : (suffix || source.label);
            item = {
              label: label,
              color: source.kind === 'candle' ? (color || source.color) :
                (symbol ? symbolColor(index) : (color || source.color)),
              xs: [], ys: []
            };
            seriesMap.set(key, item);
          }
          return item;
        }

        function getSymbolIndex(symbolValue) {
          var symbol = symbolValue == null ? '' : String(symbolValue);
          if (!symbolIndices.has(symbol)) symbolIndices.set(symbol, symbolIndices.size);
          return symbolIndices.get(symbol);
        }

        function consumeLine(line) {
          if (!line) return;
          var event = JSON.parse(line);
          if (event.type === 'meta') {
            meta = event;
            return;
          }
          if (event.type === 'error') {
            var streamError = new Error(event.detail || '图表数据读取失败');
            streamError.hint = event.hint;
            throw streamError;
          }
          if (event.type === 'done') {
            doneEvent = event;
            return;
          }
          if (event.type !== 'data' || !Array.isArray(event.points)) return;
          var symbolIndex = getSymbolIndex(event.symbol);
          event.points.forEach(function (point) {
            if (!Array.isArray(point) || point.length < 2) return;
            var x = Number(point[0]);
            if (!Number.isFinite(x)) throw new Error('图表响应包含无效时间点');
            if (source.kind === 'candle') {
              for (var i = 0; i < 4; i++) {
                var name = candleNames[i];
                var line = getSeries(event.symbol, name + ' (' + source.columns[i] + ')', SERIES_COLORS[name], symbolIndex);
                var y = point[i + 1] == null ? null : Number(point[i + 1]);
                line.xs.push(x);
                line.ys.push(Number.isFinite(y) ? y : null);
              }
            } else {
              var lineSeries = getSeries(event.symbol, source.label, source.color, symbolIndex);
              var value = point[1] == null ? null : Number(point[1]);
              lineSeries.xs.push(x);
              lineSeries.ys.push(Number.isFinite(value) ? value : null);
            }
            received++;
          });
          if (onProgress) onProgress(received);
        }

        while (true) {
          var read = await reader.read();
          buffer += decoder.decode(read.value || new Uint8Array(), { stream: !read.done });
          var newline;
          while ((newline = buffer.indexOf('\n')) >= 0) {
            consumeLine(buffer.slice(0, newline));
            buffer = buffer.slice(newline + 1);
          }
          if (read.done) break;
        }
        if (buffer) consumeLine(buffer);
        if (!meta || !doneEvent) throw new Error('图表数据流未正常结束');
        if (received !== doneEvent.rows) throw new Error('图表点数校验失败：接收 ' + received + '，服务端 ' + doneEvent.rows);

        var seriesList = Array.from(seriesMap.values());
        var expectedBySymbol = new Map((doneEvent.series || []).map(function (item) {
          return [item.symbol == null ? '' : String(item.symbol), Number(item.rows)];
        }));
        var actualBySymbol = new Map();
        seriesMap.forEach(function (item, key) {
          var symbol = key.slice(0, key.indexOf('\u0000'));
          if (!actualBySymbol.has(symbol)) actualBySymbol.set(symbol, []);
          actualBySymbol.get(symbol).push(item);
        });
        if (actualBySymbol.size !== expectedBySymbol.size) throw new Error('图表分线校验失败：symbol 数量不一致');
        expectedBySymbol.forEach(function (expected, symbol) {
          var actual = actualBySymbol.get(symbol) || [];
          if (!actual.length || actual.some(function (item) { return item.xs.length !== expected; })) {
            throw new Error('图表分线校验失败：' + (symbol || '单序列') + ' 点数与服务端不一致');
          }
        });
        seriesList.rowCount = doneEvent.rows;
        seriesList.forEach(function (item) { item.observations = item.xs.length; });
        return seriesList;
      }

      function renderEmpty(msg) {
        destroyChart();
        IU.clear(chartBox);
        chartBox.append(h('div', { class: 'chart-empty' }, msg || '暂无数据（调整筛选或确认表内有数据）'));
      }

      /* uPlot 渲染：多序列使用 x union；毫秒时间戳自动转秒启用时间轴。 */
      function renderLine(seriesList, isCandle) {
        var first = seriesList.find(function (sl) { return sl.xs.length > 0; }) || seriesList[0];
        if (!first || !first.xs.length) { renderEmpty(); return; }

        var xsRaw = mergeXs(seriesList);
        var maxAbs = xsRaw.reduce(function (m, v) { return Math.max(m, Math.abs(v)); }, 0);
        viewXTime = maxAbs > 1e11; // 秒级 epoch ≈1e9，毫秒级 ≈1e12

        var seriesDefs = seriesList.map(function (sl) {
          return { label: sl.label, stroke: sl.color, width: 1.5, points: { show: sl.xs.length < 40 } };
        });

        destroyChart();
        IU.clear(chartBox);
        var mount = h('div');
        chartBox.append(mount);
        if (isCandle) {
          chartBox.append(h('div', { class: 'chart-note' },
            'OHLC 四线近似显示：当前没有蜡烛实体；每条原始记录均保留。'));
        } else if (new Set(seriesList.map(function (sl) { return sl.label.split(' · ')[0]; })).size > 1) {
          chartBox.append(h('div', { class: 'chart-note' },
            '多 symbol 共用 Y 轴；SMA/EMA/ATR 等币价量纲不同的指标可能压扁低价币线。绘制这类指标时可筛选单个 symbol；RSI 等归一化指标适合并列查看。'));
        }
        chartBox.append(viewNote);

        try {
          // 时间轴交给 uPlot 原生渲染（可读日期）；仅非时间横轴才自定义整数刻度
          // （PLAN-064 Q3：原先强制 Math.round 覆盖导致 X 轴显示原始毫秒）。
          // 注意：isTime 时不传 space/values 键——旧版 uPlot 对 null 轴参数处理不佳
          var xAxis = { stroke: '#8998ae', grid: { stroke: 'rgba(255,255,255,.06)' } };
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
              { stroke: '#8998ae', grid: { stroke: 'rgba(255,255,255,.06)' },
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
          var rowCount = seriesList.rowCount || seriesList.reduce(function (sum, sl) { return sum + sl.xs.length; }, 0);
          noteRange(xsRaw[0], xsRaw[xsRaw.length - 1], rowCount, seriesList, true);
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
