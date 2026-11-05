/* =====================================================================
 * iosql Web · 通用图表：条件 + X/Y + 可选分组 → 原始点流 → mchart3。
 * 数值时间轴使用折线，其他类型组合使用散点；保留原有 OHLC 四线。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;
  var st = null;
  var enginePromise = null;
  var OHLC_LONG = ['open', 'high', 'low', 'close'];
  var OHLC_SHORT = ['o', 'h', 'l', 'c'];

  function loadChartEngine() {
    if (window.MChart && typeof window.MChart.Figure === 'function') return Promise.resolve(window.MChart);
    if (enginePromise) return enginePromise;
    enginePromise = new Promise(function (resolve, reject) {
      var script = document.createElement('script');
      script.src = 'vendor/mchart3-0.5.0.min.js';
      script.onload = function () {
        if (window.MChart && typeof window.MChart.Figure === 'function') resolve(window.MChart);
        else { script.remove(); reject(new Error('图表引擎加载失败，请重试')); }
      };
      script.onerror = function () { script.remove(); reject(new Error('图表引擎加载失败，请重试')); };
      document.head.appendChild(script);
    }).catch(function (err) { enginePromise = null; throw err; });
    return enginePromise;
  }

  function typeOfColumn(column, types, spec) {
    var t = String(types[column] || '').toLowerCase().split('(')[0].trim();
    var numeric = /^(int|integer|bigint|smallint|real|float|double|double precision|numeric|decimal|number)$/.test(t);
    if (numeric && spec.time && spec.time.name === column && spec.time.unit === 'ms') return 'time';
    if (numeric) return 'numeric';
    var category = /^(text|string|str|varchar|char|character|character varying|date|datetime|timestamp|timestamptz|timestamp without time zone|timestamp with time zone|time|time without time zone|time with time zone|boolean|bool)$/.test(t);
    if (category && (t === 'date' || (spec.time && spec.time.name === column && spec.time.unit === 'date'))) return 'date';
    if (category) return 'category';
    return null;
  }

  function ensureState(ctx) {
    if (st && st.table === ctx.table) return st;
    var spec = ctx.spec || {};
    var types = spec.columns || {};
    var columns = Object.keys(types).filter(function (column) { return typeOfColumn(column, types, spec); });
    var time = spec.time && columns.indexOf(spec.time.name) >= 0 ? spec.time.name : null;
    var numeric = columns.filter(function (column) {
      var type = typeOfColumn(column, types, spec);
      return type === 'numeric' || type === 'time';
    });
    var byLower = Object.create(null);
    columns.forEach(function (column) { byLower[column.toLowerCase()] = column; });
    var names = OHLC_LONG.every(function (c) { return byLower[c]; }) ? OHLC_LONG :
      (OHLC_SHORT.every(function (c) { return byLower[c]; }) ? OHLC_SHORT : null);
    var ohlc = names ? names.map(function (c) { return byLower[c]; }) : null;
    var defaultX = time || numeric[0] || columns[0] || '';
    var defaultY = numeric.find(function (c) { return c !== defaultX; }) ||
      columns.find(function (c) { return c !== defaultX; }) || defaultX;
    st = {
      table: ctx.table, spec: spec, types: types, columns: columns, ohlc: ohlc,
      x: defaultX, y: defaultY,
      group: columns.indexOf(spec.symbol_column) >= 0 ? spec.symbol_column : '',
      kind: 'auto', log: false, start: '', end: '',
      builder: new IU.ConditionBuilder(Object.keys(types), types)
    };
    return st;
  }

  function cfgField(label, control) {
    return h('label', null, label, control);
  }

  function colorFor(group, index) {
    var text = JSON.stringify([group, index]);
    var hash = 0;
    for (var i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    return 'hsl(' + (hash % 360) + ', 72%, 64%)';
  }

  function groupLabel(group, grouped) {
    if (!grouped) return '全部';
    if (group === null) return '∅';
    if (group === '') return '（空字符串）';
    return String(group);
  }

  function axisLabel(values) {
    return function (value) {
      var n = Number(value), i = Math.round(n);
      if (!Number.isFinite(n) || Math.abs(n - i) > 0.00001 || i < 0 || i >= values.length) return '';
      return String(values[i]);
    };
  }

  function timeLabel(value, axes) {
    var date = new Date(Number(value));
    if (Number.isNaN(date.getTime())) return '';
    var span = Math.abs(axes.xScale.max - axes.xScale.min);
    var options = span <= 180000
      ? { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }
      : span <= 172800000
        ? { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }
        : { year: 'numeric', month: '2-digit', day: '2-digit' };
    return date.toLocaleString('zh-CN', options);
  }

  function calendarValue(value) {
    var match = String(value).match(/^(\d{4})-?(\d{2})-?(\d{2})$/);
    if (!match) throw new Error('日期轴包含无效日期');
    var text = match[1] + '-' + match[2] + '-' + match[3];
    var ms = Date.parse(text + 'T00:00:00.000Z');
    if (!Number.isFinite(ms) || new Date(ms).toISOString().slice(0, 10) !== text) {
      throw new Error('日期轴包含无效日期');
    }
    return ms;
  }

  function calendarLabel(value) {
    var date = new Date(Number(value));
    return Number.isNaN(date.getTime()) ? '' : date.toISOString().slice(0, 10);
  }

  function makeCategoryMap() {
    var keys = new Map(), values = [];
    return {
      values: values,
      index: function (value) {
        var key = JSON.stringify(value);
        if (!keys.has(key)) { keys.set(key, values.length); values.push(value); }
        return keys.get(key);
      }
    };
  }

  IU.tabs = IU.tabs || {};
  IU.tabs.charts = {
    id: 'charts',
    label: 'Charts',
    render: function (root, ctx) {
      var s = ensureState(ctx);
      IU.tabs.charts.cleanup = null;
      IU.clear(root);
      if (!s.columns.length) {
        root.append(h('div', { class: 'card empty-state' }, '没有可绘制的数值、文本或日期列。'));
        return;
      }

      function columnOptions(selected) {
        return s.columns.map(function (column) {
          var type = typeOfColumn(column, s.types, s.spec);
          var label = type === 'time' ? '时间' : (type === 'date' ? '日期' : (type === 'numeric' ? '数值' : '类别'));
          return h('option', { value: column, selected: column === selected }, column + ' · ' + label);
        });
      }
      var xSel = h('select', { class: 'input', 'aria-label': 'X 轴' }, columnOptions(s.x));
      var ySel = h('select', { class: 'input', 'aria-label': 'Y 轴' }, columnOptions(s.y));
      var groupSel = h('select', { class: 'input', 'aria-label': '分组列' },
        h('option', { value: '', selected: !s.group }, '不分组'), columnOptions(s.group));
      var kindSel = h('select', { class: 'input', 'aria-label': '图型' },
        h('option', { value: 'auto', selected: s.kind === 'auto' }, '按坐标类型自动选择'),
        h('option', { value: 'ohlc', disabled: !s.ohlc, selected: s.kind === 'ohlc' },
          s.ohlc ? 'OHLC 四线' : 'OHLC 四线（缺少列）'));
      var logChk = h('input', { type: 'checkbox', id: 'chart-log' });
      logChk.checked = s.log;
      var timeCol = s.spec.time && typeOfColumn(s.spec.time.name, s.types, s.spec) === 'time'
        ? s.spec.time.name : null;
      var startIn = h('input', { class: 'input mono', 'aria-label': '时间开始',
        placeholder: '2026-01-01 或毫秒', value: s.start, autocomplete: 'off' });
      var endIn = h('input', { class: 'input mono', 'aria-label': '时间结束',
        placeholder: '2026-01-02 或毫秒', value: s.end, autocomplete: 'off' });
      startIn.addEventListener('input', function () { s.start = startIn.value; });
      endIn.addEventListener('input', function () { s.end = endIn.value; });
      var typeHint = h('div', { class: 'chart-time-hint', 'aria-live': 'polite' });
      var chartBox = h('div', { class: 'chart-box' });
      var status = h('div', { id: 'chart-status', class: 'chart-note', 'aria-live': 'polite' });
      var figure = null, axes = null, controller = null, viewTimer = null;
      var requestSeq = 0, active = null, fullScale = null, appliedWindow = null, settledStatus = '';
      var resizeHandler = null;

      function updateHint() {
        var xType = typeOfColumn(s.x, s.types, s.spec);
        var yType = typeOfColumn(s.y, s.types, s.spec);
        var label = s.kind === 'ohlc' ? 'OHLC 四线' :
          ((xType === 'time' || xType === 'date') && (yType === 'numeric' || yType === 'time') ? '折线图' : '散点图');
        typeHint.textContent = '当前图型：' + label + '。类别使用离散刻度；时间列按表契约识别。';
        logChk.disabled = (yType === 'category' || yType === 'date') && s.kind !== 'ohlc';
        if (logChk.disabled) { s.log = false; logChk.checked = false; }
      }
      xSel.addEventListener('change', function () { s.x = xSel.value; updateHint(); });
      ySel.addEventListener('change', function () { s.y = ySel.value; updateHint(); });
      groupSel.addEventListener('change', function () { s.group = groupSel.value; });
      kindSel.addEventListener('change', function () { s.kind = kindSel.value; updateHint(); });
      logChk.addEventListener('change', function () { s.log = logChk.checked; });
      updateHint();

      root.append(h('div', { class: 'card chart-settings' },
        h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '图表配置')),
        h('div', { class: 'chart-config' },
          cfgField('X 轴', xSel), cfgField('Y 轴', ySel), cfgField('分组列', groupSel),
          cfgField('图型', kindSel),
          timeCol ? cfgField('时间开始', startIn) : null,
          timeCol ? cfgField('时间结束', endIn) : null,
          h('label', { class: 'chart-log', for: 'chart-log' }, logChk, '对数轴'),
          h('button', { class: 'btn primary', onclick: draw }, '绘制')),
        typeHint,
        h('div', { class: 'chart-conditions' }, s.builder.el),
        h('div', { class: 'chart-time-hint' },
          '筛选条件留空表示全部数据；分组过多时请先添加条件。无时区日期按浏览器本地时间解析。图表保留所选范围内的原始记录。')));
      root.append(chartBox);
      showEmpty('选择坐标并按需设置筛选条件，然后点击「绘制」。');

      function disposeFigure() {
        if (figure) { figure.dispose(); figure = null; axes = null; }
      }
      function stopRequest() {
        requestSeq++;
        if (viewTimer) { clearTimeout(viewTimer); viewTimer = null; }
        if (controller) { controller.abort(); controller = null; }
      }
      function cleanup() {
        stopRequest();
        if (resizeHandler) window.removeEventListener('resize', resizeHandler);
        resizeHandler = null;
        disposeFigure();
        IU.tabs.charts.cleanup = null;
      }
      IU.tabs.charts.cleanup = cleanup;
      resizeHandler = function () {
        if (figure && chartBox.isConnected) figure.resize(Math.max(chartBox.clientWidth - 24, 280), 360);
      };
      window.addEventListener('resize', resizeHandler);

      function showEmpty(message) {
        disposeFigure();
        IU.clear(chartBox);
        chartBox.append(h('div', { class: 'chart-empty' }, message));
      }
      function setStatus(message) { status.textContent = message; }
      function setSettledStatus(message) { settledStatus = message; setStatus(message); }
      function errorText(err) {
        return (err && (err.hint || err.message)) || '请检查筛选条件和列类型';
      }
      function snapshot() {
        var result = s.builder.getWhere();
        if (!result.ok) { IU.toast(result.msg, 'warn'); return null; }
        function timeValue(value) {
          if (typeof value === 'number' && Number.isFinite(value)) return value;
          var parsed = IU.parseTimeInput(value);
          if (!parsed.ok) throw new Error('时间无法解析：' + String(value));
          return parsed.ms;
        }
        function normalizeTime(comp) {
          if (comp.and || comp.or) {
            var key = comp.and ? 'and' : 'or', group = {};
            group[key] = comp[key].map(normalizeTime);
            return group;
          }
          if (!timeCol || comp.field !== timeCol) return comp;
          return { field: comp.field, op: comp.op,
            value: Array.isArray(comp.value) ? comp.value.map(timeValue) : timeValue(comp.value) };
        }
        var where, start = null, end = null;
        try {
          where = (result.where || []).map(normalizeTime);
          if (timeCol && s.start.trim()) start = timeValue(s.start);
          if (timeCol && s.end.trim()) end = timeValue(s.end);
          if (start !== null && end !== null && start > end) throw new Error('时间开始不能晚于时间结束');
        } catch (err) {
          IU.toast(err.message, 'warn');
          return null;
        }
        if (start !== null) where.push({ field: timeCol, op: '>=', value: start });
        if (end !== null) where.push({ field: timeCol, op: '<=', value: end });
        return {
          x: s.x, y: s.y, group_by: s.group || null, where: where,
          kind: s.kind, log: s.log
        };
      }
      function draw() {
        var config = snapshot();
        if (!config) return;
        stopRequest();
        active = config;
        fullScale = null;
        appliedWindow = null;
        settledStatus = '';
        showEmpty('正在读取所选范围的原始记录…');
        requestChart(config, null, false);
      }
      function sameWindow(a, b) {
        if (!a || !b) return a === b;
        var span = Math.max(Math.abs(a.max - a.min), Math.abs(b.max - b.min), 1e-9);
        return Math.abs(a.min - b.min) <= span * 0.001 && Math.abs(a.max - b.max) <= span * 0.001;
      }
      function scheduleViewFetch(win) {
        if (!active || !chartBox.isConnected) return;
        if (win && sameWindow(win, fullScale)) win = null;
        if (viewTimer) { clearTimeout(viewTimer); viewTimer = null; }
        if (controller) { requestSeq++; controller.abort(); controller = null; }
        if (sameWindow(win, appliedWindow)) {
          if (settledStatus) setStatus(settledStatus);
          return;
        }
        viewTimer = setTimeout(function () { viewTimer = null; requestChart(active, win, true); }, 250);
      }
      function requestChart(config, win, viewport) {
        var seq = ++requestSeq;
        if (controller) controller.abort();
        controller = new AbortController();
        if (viewport) setStatus('可视区加载中…');
        var body = { x: config.x, y: config.y, group_by: config.group_by,
          kind: config.kind, where: config.where.slice() };
        if (win) {
          body.where.push({ field: config.x, op: '>=', value: win.min });
          body.where.push({ field: config.x, op: '<=', value: win.max });
        }
        var signal = controller.signal;
        loadChartEngine().then(function () {
          if (seq !== requestSeq || !chartBox.isConnected) return null;
          return readChartStream('/api/table/' + encodeURIComponent(ctx.table) + '/chart', body, signal);
        })
          .then(function (result) {
            if (!result || seq !== requestSeq || !chartBox.isConnected) return;
            controller = null;
            appliedWindow = win;
            if (!result.rows) {
              if (viewport) setSettledStatus('可视区无数据 · 无抽样');
              else showEmpty('没有匹配的数据，请调整筛选条件。');
              return;
            }
            renderChart(config, result, win);
          }).catch(function (err) {
            if (seq !== requestSeq || err.name === 'AbortError' || !chartBox.isConnected) return;
            controller = null;
            if (viewport) setStatus('可视区加载失败：' + errorText(err));
            else showEmpty('图表数据加载失败：' + errorText(err));
          });
      }

      async function readChartStream(url, body, signal) {
        var response = await fetch(url, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body), signal: signal
        });
        if (!response.ok) {
          var problem = null;
          try { problem = await response.json(); } catch (_) { /* 响应可能不是 JSON */ }
          var failure = new Error((problem && problem.detail) || ('HTTP ' + response.status));
          failure.hint = problem && problem.hint;
          throw failure;
        }
        if (!response.body || !response.body.getReader) throw new Error('当前浏览器不支持图表数据流');
        var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = '';
        var meta = null, done = null, received = 0;
        var groups = new Map(), xCategories = makeCategoryMap(), yCategories = makeCategoryMap();
        function consume(line) {
          if (!line) return;
          var event = JSON.parse(line);
          if (event.type === 'meta') {
            if (meta || ['line', 'scatter', 'ohlc'].indexOf(event.kind) < 0 ||
                ['time', 'date', 'numeric', 'category'].indexOf(event.x_type) < 0 ||
                ['numeric', 'category'].indexOf(event.y_type) < 0) {
              throw new Error('图表元信息无效');
            }
            meta = event;
            return;
          }
          if (event.type === 'error') {
            var failure = new Error(event.detail || '读取图表失败');
            failure.hint = event.hint;
            throw failure;
          }
          if (event.type === 'done') { done = event; return; }
          if (event.type !== 'data' || !Array.isArray(event.points) || !meta) return;
          var key = JSON.stringify(event.group);
          if (!groups.has(key)) {
            var count = meta.kind === 'ohlc' ? 4 : 1;
            groups.set(key, { group: event.group, lines: Array.from({ length: count }, function () {
              return { xs: [], ys: [] };
            }) });
          }
          var lines = groups.get(key).lines;
          event.points.forEach(function (point) {
            if (!Array.isArray(point) || point.length !== lines.length + 1) throw new Error('图表数据格式错误');
            var rawX = point[0];
            if (rawX === null || rawX === undefined ||
                (meta.x_type === 'category' && typeof rawX !== 'string' && typeof rawX !== 'boolean') ||
                (meta.x_type === 'date' && typeof rawX !== 'string' && !Number.isSafeInteger(rawX)) ||
                (meta.x_type !== 'category' && meta.x_type !== 'date' &&
                  (typeof rawX !== 'number' || !Number.isFinite(rawX)))) {
              throw new Error('图表 X 轴包含无效值');
            }
            var x = meta.x_type === 'category' ? xCategories.index(rawX) :
              (meta.x_type === 'date' ? calendarValue(rawX) : rawX);
            lines.forEach(function (line, index) {
              var raw = point[index + 1], y = null;
              if (raw !== null && raw !== undefined) {
                if ((meta.y_type === 'category' && typeof raw !== 'string' && typeof raw !== 'boolean') ||
                    (meta.y_type !== 'category' && (typeof raw !== 'number' || !Number.isFinite(raw)))) {
                  throw new Error('图表 Y 轴包含无效值');
                }
                y = meta.y_type === 'category' ? yCategories.index(raw) : raw;
              }
              line.xs.push(x);
              line.ys.push(y);
            });
            received++;
          });
        }
        var finished = false;
        try {
          while (true) {
            var read = await reader.read();
            buffer += decoder.decode(read.value || new Uint8Array(), { stream: !read.done });
            var newline;
            while ((newline = buffer.indexOf('\n')) >= 0) {
              consume(buffer.slice(0, newline));
              buffer = buffer.slice(newline + 1);
            }
            if (read.done) break;
          }
          if (buffer) consume(buffer);
          finished = true;
        } finally {
          if (!finished) {
            try { await reader.cancel(); } catch (_) { /* 请求可能已取消 */ }
          }
          reader.releaseLock();
        }
        if (!meta || !done || received !== done.rows) throw new Error('图表数据流未正常结束或记录数不一致');
        var expected = new Map((done.series || []).map(function (item) { return [JSON.stringify(item.group), item.rows]; }));
        if (expected.size !== groups.size) throw new Error('图表分组数量不一致');
        groups.forEach(function (group, key) {
          if (!expected.has(key) || group.lines.some(function (line) { return line.xs.length !== expected.get(key); })) {
            throw new Error('图表分组记录数不一致');
          }
        });
        return { meta: meta, groups: Array.from(groups.values()), rows: received,
          xCategories: xCategories.values, yCategories: yCategories.values };
      }

      function renderChart(config, result, win) {
        var hiddenByLog = 0, positiveValues = 0;
        if (config.log) {
          result.groups.forEach(function (group) {
            group.lines.forEach(function (line) {
              line.ys.forEach(function (value) {
                if (value === null) return;
                if (value > 0) positiveValues++;
                else hiddenByLog++;
              });
            });
          });
          if (!positiveValues) {
            if (win) setSettledStatus('可视区没有可显示的正数，请调整范围。');
            else showEmpty('对数轴没有可显示的正数，请关闭对数轴或调整筛选条件。');
            return;
          }
        }
        disposeFigure();
        IU.clear(chartBox);
        var canvas = h('canvas', { class: 'chart-canvas', role: 'img',
          'aria-label': '数据图表', 'aria-describedby': 'chart-status' });
        chartBox.append(canvas, status);
        var meta = result.meta;
        var xFormatter = meta.x_type === 'category' ? axisLabel(result.xCategories) : null;
        var yFormatter = meta.y_type === 'category' ? axisLabel(result.yCategories) : null;
        try {
          figure = new MChart.Figure(canvas, { width: Math.max(chartBox.clientWidth - 24, 280),
            height: 360, backgroundColor: '#0f141e', theme: 'dark' });
          axes = figure.addSubplot(1, 1, 0, 0, {
            xScale: meta.x_type === 'time' || meta.x_type === 'date' ? 'time' : 'linear',
            yScale: config.log ? 'log' : 'linear', autoLayout: true,
            padding: { left: 58, right: 58, top: 18, bottom: 40 },
            showGrid: true, gridColor: 'rgba(255,255,255,.06)',
            backgroundColor: '#0f141e', axisColor: '#44536a', labelColor: '#a4b1c6',
            xLabelFormatter: xFormatter, yLabelFormatter: yFormatter,
            timeLabelFormatter: meta.x_type === 'date' ? calendarLabel : (meta.x_type === 'time' ? timeLabel : null),
            crosshair: { visible: true, color: '#8798af' }
          });
          axes.setXLabel(config.x);
          axes.setYLabel(config.kind === 'ohlc' ? 'OHLC' : config.y);
          result.groups.forEach(function (group) {
            group.lines.forEach(function (line, index) {
              var label = groupLabel(group.group, !!meta.group_by);
              if (meta.kind === 'ohlc') label += ' · ' + meta.value_cols[index];
              else if (!meta.group_by) label = config.y;
              var ys = config.log ? line.ys.map(function (value) {
                return value !== null && value > 0 ? value : null;
              }) : line.ys;
              var options = { color: colorFor(group.group, index), label: label,
                downsample: false, sorted: meta.x_type !== 'category', lineWidth: 1.5, size: 3 };
              if (meta.kind === 'line' || meta.kind === 'ohlc') axes.addLineSeries(line.xs, ys, options);
              else axes.addScatterSeries(line.xs, ys, options);
            });
          });
          axes.addLegend();
          figure.show();
          if (!fullScale) fullScale = { min: axes.xScale.min, max: axes.xScale.max };
          if (win) axes.setXLim(win.min, win.max);
          else axes.setXLim(fullScale.min, fullScale.max, { remember: true });
          var currentAxes = axes;
          var oldPan = currentAxes.onPan.bind(currentAxes), oldZoom = currentAxes.onZoom.bind(currentAxes);
          function afterChange() {
            if (meta.x_type === 'category' || meta.x_type === 'date') return;
            var min = currentAxes.xScale.min, max = currentAxes.xScale.max;
            if (Number.isFinite(min) && Number.isFinite(max) && min < max) scheduleViewFetch({ min: min, max: max });
          }
          currentAxes.onPan = function () { oldPan.apply(null, arguments); afterChange(); };
          currentAxes.onZoom = function () { oldZoom.apply(null, arguments); afterChange(); };
          canvas.addEventListener('dblclick', function () {
            if (!axes || !fullScale) return;
            axes.setXLim(fullScale.min, fullScale.max);
            if (meta.x_type !== 'category' && meta.x_type !== 'date') scheduleViewFetch(null);
          });
          var counts = result.groups.map(function (group) {
            return groupLabel(group.group, !!meta.group_by) + ' ' + group.lines[0].xs.length.toLocaleString('zh-CN');
          });
          setSettledStatus((win ? '可视区' : '全范围') + ' · ' + result.rows.toLocaleString('zh-CN') +
            ' 条原始记录 · ' + result.groups.length + ' 组（无抽样） · ' + counts.join('，') +
            (hiddenByLog ? ' · 对数轴不显示 ' + hiddenByLog.toLocaleString('zh-CN') + ' 个非正值' : '') +
            (meta.x_type === 'category' || meta.x_type === 'date' ? ' · 本地缩放' : ' · 缩放后重新读取，双击复位'));
        } catch (err) {
          showEmpty('渲染失败：' + (err.message || String(err)));
        }
      }
    }
  };
})();
