/* =====================================================================
 * iosql Web · tab-structure.js
 * Structure 三区块：契约卡（spec）→ 逻辑分区表（parts，chips 或条形图）
 * → 物理 segment 表（include_obsolete 开关重拉）。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;
  var DURATION_SCALES = {
    milliseconds: 1, millisecond: 1, msecs: 1, msec: 1, ms: 1,
    seconds: 1000, second: 1000, secs: 1000, sec: 1000, s: 1000,
    minutes: 60000, minute: 60000, mins: 60000, min: 60000,
    hours: 3600000, hour: 3600000, hrs: 3600000, hr: 3600000, h: 3600000,
    days: 86400000, day: 86400000, d: 86400000,
    weeks: 604800000, week: 604800000, w: 604800000,
    months: 2592000000, month: 2592000000,
    years: 31536000000, year: 31536000000
  };

  IU.tabs = IU.tabs || {};

  IU.tabs.structure = {
    id: 'structure',
    label: 'Structure',

    render: function (root, ctx) {
      var base = '/api/table/' + encodeURIComponent(ctx.table);
      IU.clear(root);

      root.append(contractCard(ctx.spec));

      var partsBox = h('div', { class: 'card' },
        h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '逻辑分区 · Parts'),
          h('span', { class: 'spinner', style: { width: '11px', height: '11px' } })));
      root.append(partsBox);

      var segBox = h('div', { class: 'card' });
      root.append(segBox);

      var reloadSegments = renderSegmentsCard(base, segBox, ctx, function () {
        if (partsBox.refreshDistribution) partsBox.refreshDistribution();
      });
      loadParts(base, partsBox, ctx, reloadSegments);
    }
  };

  /* ---------- 区块 A：契约卡 ---------- */
  function contractCard(spec) {
    spec = spec || {};
    var colTypes = (spec.columns && typeof spec.columns === 'object') ? spec.columns : {};
    var colListNode = Object.keys(colTypes).length
      ? h('div', null, Object.keys(colTypes).map(function (c) {
        return h('div', { class: 'mono', style: { fontSize: '13px' } },
          c, h('span', { class: 'faint' }, ' : ' + colTypes[c]));
      }))
      : null;

    var seg = spec.segment;
    var segText;
    if (seg === null || seg === undefined) segText = null;
    else if (typeof seg === 'string') segText = seg;
    else if (typeof seg === 'object') {
      segText = h('span', null, Object.keys(seg).map(function (k) { return k + '=' + JSON.stringify(seg[k]); }).join(', '));
    } else segText = String(seg);

    return h('div', { class: 'card' },
      h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '契约 · Spec')),
      IU.kvGrid([
        ['pk 主键', Array.isArray(spec.primary_key) ? spec.primary_key.join(', ') : (spec.pk || null)],
        ['columns 列定义', colListNode],
        ['partition 分区策略', typeof spec.partition === 'object' && spec.partition !== null
          ? (spec.partition.str || JSON.stringify(spec.partition))
          : (spec.partition || null)],
        ['segment 参数', segText],
        ['order_key 排序键', spec.order_key || null],
        ['time 时间契约', IU.describeTime(spec.time)],
        ['session_column', spec.session_column || null],
        ['symbol_column', spec.symbol_column || null],
        ['conflict 冲突策略', spec.conflict || null],
        ['schema_policy', spec.schema_policy || null]
      ]));
  }

  /* ---------- 区块 B：逻辑分区 ----------
   * /parts 只提供 key 列表；行数/segment 分布来自 app.js 的 ctx.parts
   * （/segments 客户端聚合，PLAN-064 D4）。分布未到达时先渲染可读 chips，
   * 完成后重绘为条形图；偏斜热点 amber 高亮；
   * 点击分区行 → 跳 Data 并携带时间范围过滤（可分享 URL，Q2）。 */
  function loadParts(base, box, ctx, reloadSegments) {
    IU.clear(box);
    box.append(h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '逻辑分区 · Parts'),
      h('span', { class: 'spinner', style: { width: '11px', height: '11px' } })));
    IU.api(base + '/parts').then(function (resp) {
      var arr = IU.toArray(resp);
      var keys = arr.map(function (it) {
        if (it === null || it === undefined) return String(it);
        if (typeof it !== 'object') return String(it);
        var k = IU.pick(it, 'key', 'partition_key', 'partition', 'name', 'id');
        return String(k !== undefined ? k : '');
      });

      function distNow() {
        return ctx.parts ? ctx.parts[ctx.table] : undefined;
      }

      function draw() {
        var dist = distNow();
        var hasDist = dist && !dist.error && dist.order && dist.order.length;
        IU.clear(box);
        box.append(h('div', { class: 'card-title' },
          h('span', { class: 'micro-label' }, '逻辑分区 · Parts'),
          h('span', { class: 'faint mono', style: { fontSize: '12px' } }, keys.length + ' 个')));

        if (!keys.length) {
          box.append(h('div', { class: 'empty-state' }, '暂无已写入的分区；写入数据后可在此查看分布。'));
          return;
        }

        if (!hasDist) {
          box.append(h('div', { class: 'chips' }, keys.map(partChip)));
          if (!dist || dist.loading) {
            box.append(h('div', { class: 'faint', style: { fontSize: '12px', marginTop: '6px' } },
              h('span', { class: 'spinner', style: { width: '11px', height: '11px' } }),
              ' 行数分布统计中…'));
            ctx.requestParts(ctx.table).then(redrawIfConnected, redrawIfConnected);
          } else if (dist.error) {
            box.append(h('div', { class: 'faint', style: { fontSize: '12px', marginTop: '6px' } },
              '分区行数分布不可用。 ',
              h('button', { class: 'btn sm', onclick: function () {
                ctx.requestParts(ctx.table).catch(function () {});
                if (reloadSegments) reloadSegments();
                draw();
              } }, '重试分布')));
          } else {
            box.append(h('div', { class: 'faint', style: { fontSize: '12px', marginTop: '6px' } }, '暂无行数分布。'));
          }
          return;
        }

        // 行数条形图（key 全为毫秒时间戳时按数值排序）
        var allMs = keys.every(function (k) { return /^\d+$/.test(k) && Number(k) >= 1e12; });
        var order = hasDist ? dist.order.slice() : keys.slice();
        if (allMs) order.sort(function (a, b) { return Number(a) - Number(b); });
        // keys 里可能存在 dist 未覆盖的分区（防御式）：行数取 0
        var items = order.map(function (k) {
          return {
            key: k,
            rows: dist.byPart[k] !== undefined ? dist.byPart[k] : 0,
            segs: dist.segCount ? dist.segCount[k] : undefined
          };
        });
        var seen = Object.create(null);
        order.forEach(function (k) { seen[k] = true; });
        keys.forEach(function (k) {
          if (!seen[k]) items.push({ key: k, rows: dist.byPart[k] || 0 });
        });

        var rowsList = items.map(function (it) { return it.rows || 0; });
        var max = rowsList.reduce(function (a, b) { return Math.max(a, b); }, 0);
        var skew = IU.partitionSkew(items.map(function (it) { return it.key; }), dist.byPart) > 4;

        var chart = h('div', { class: 'bar-chart' });
        var displayed = 0;
        var more = h('button', { class: 'btn sm section-gap', onclick: appendItems });
        function appendItems() {
          var end = Math.min(displayed + 200, items.length);
          items.slice(displayed, end).forEach(appendItem);
          displayed = end;
          more.hidden = displayed >= items.length;
          more.textContent = '显示更多分区（已显示 ' + displayed + ' / ' + items.length + '）';
        }
        function appendItem(it) {
          var pct = max ? ((it.rows || 0) / max) * 100 : 0;
          var isHot = skew && (it.rows || 0) === max;
          var readable = IU.fmt.date(it.key);
          var keyEl = h('span', { class: 'bar-key', title: it.key },
            readable ? h('span', { style: { color: 'var(--acc)' } }, readable) : it.key,
            readable ? h('span', { class: 'raw' }, it.key) : null);
          var target = partFilterHref(ctx, it.key);
          var line = h(target ? 'a' : 'div', {
            class: 'bar-line' + (target ? ' part-link' : ''),
            href: target,
            title: target ? '在 Data 页查看此分区的数据' : null
          },
            keyEl,
            h('div', { class: 'bar-track' },
              h('div', { class: 'bar-fill' + (isHot ? ' hot' : ''), style: { width: pct + '%' } })),
            h('span', { class: 'bar-meta' },
              IU.fmt.int(it.rows || 0) + ' 行'
              + (it.segs !== undefined ? ' · ' + it.segs + ' segs' : '')));
          chart.append(line);
        }
        appendItems();
        box.append(chart, more);
        box.append(h('div', { class: 'faint', style: { fontSize: '12px', marginTop: '6px' } },
          (skew ? '琥珀 = 偏斜热点（最大/中位数 > 4）。' : '')
          + (items.some(function (it) { return !!partFilterHref(ctx, it.key); })
            ? '点击分区行 → Data 页按该分区范围过滤（URL 可分享）。' : '')));
      }

      function redrawIfConnected() { if (box.isConnected) draw(); }

      box.refreshDistribution = redrawIfConnected;
      draw();
    }).catch(function () {
      IU.clear(box);
      box.append(h('div', { class: 'empty-state' }, '分区信息加载失败。 ',
        h('button', { class: 'btn sm', onclick: function () { loadParts(base, box, ctx, reloadSegments); } }, '重试')));
    });

    function partChip(key) {
      var readable = IU.fmt.date(key);
      return h('span', { class: 'chip' },
        readable ? h('span', { style: { color: 'var(--acc)' } }, readable) : key,
        readable ? h('span', { style: { color: 'var(--tx2)', marginLeft: '6px', fontSize: '10.5px' } }, key) : null);
    }
  }

  function timeColOf(ctx) {
    var t = ctx.spec && ctx.spec.time;
    if (!t) return null;
    return typeof t === 'string' ? t : (IU.pick(t, 'column', 'col', 'field', 'name') || null);
  }

  /* 与 duration 固定时长单位一致；无法精确识别时不提供分区跳转。 */
  function durationIntervalMs(value) {
    if (typeof value === 'number') return Number.isSafeInteger(value) && value > 0 ? value : null;
    if (typeof value !== 'string') return null;
    var text = value.trim().toLowerCase();
    if (/^\d+$/.test(text)) {
      var number = Number(text);
      return Number.isSafeInteger(number) && number > 0 ? number : null;
    }
    var match = /^(\d*)([a-z]+)$/.exec(text);
    if (!match) return null;
    if (!Object.prototype.hasOwnProperty.call(DURATION_SCALES, match[2])) return null;
    var count = match[1] ? Number(match[1]) : 1;
    var width = count * DURATION_SCALES[match[2]];
    return Number.isSafeInteger(count) && count > 0 && Number.isSafeInteger(width) ? width : null;
  }

  /* 仅 duration 且分区字段就是毫秒时间列时，可以从契约精确计算左闭右开边界。
   * 不能用下一现存分区作为上界：中间可能有空分区，between 还会包含上界。 */
  function partFilterHref(ctx, key) {
    var timeCol = timeColOf(ctx);
    var part = ctx.spec && ctx.spec.partition;
    if (!part || part.kind !== 'duration' || part.column !== timeCol || part.time_unit !== 'ms') return null;
    var lo = Number(key), width = durationIntervalMs(part.every), hi = lo + width;
    if (!Number.isSafeInteger(lo) || !Number.isSafeInteger(width) || width <= 0 || !Number.isSafeInteger(hi)) return null;
    var q = 'f.' + encodeURIComponent(timeCol) + '=' + encodeURIComponent(lo + '~<' + hi);
    return '#/table/' + encodeURIComponent(ctx.table) + '/data?' + q;
  }

  /* ---------- 区块 C：物理 segment 表 ---------- */
  function renderSegmentsCard(base, box, ctx, onDistributionStart) {
    var includeObsolete = false;

    var tbodySlot = h('tbody');
    var tableWrap = h('div', { class: 'table-wrap' });
    var requestSeq = 0;

    function draw() {
      IU.clear(box);
      var toggle = h('input', { type: 'checkbox', id: 'inc-obs' });
      toggle.checked = includeObsolete;
      toggle.addEventListener('change', function () {
        includeObsolete = toggle.checked;
        fetchRows(true);
      });
      box.append(
        h('div', { class: 'card-title' },
          h('span', { class: 'micro-label' }, '物理 Segment'),
          h('span', { class: 'spacer' }),
          h('label', { for: 'inc-obs', style: { display: 'flex', gap: '6px', alignItems: 'center', cursor: 'pointer', fontSize: '13px', color: 'var(--tx1)' } },
            toggle, '包含 obsolete')),
        tableWrap);
      if (!tableWrap.contains(tbodySlot.parentNode)) {
        tableWrap.append(h('table', { class: 'data-table' },
          headEl(), tbodySlot));
      }
      renderLoading();
      fetchRows(true);
    }

    function headEl() {
      return h('thead', null, h('tr', null,
        ['file_key', '逻辑分区', 'rows', 'state', '时间范围 min ~ max', 'stats'].map(function (t, i) {
          return h('th', { class: i === 2 ? 'num' : '' }, t);
        })));
    }

    function renderLoading() {
      IU.clear(tbodySlot);
      tbodySlot.append(h('tr', null,
        h('td', { class: 'loading-cell', colspan: '6' }, h('span', { class: 'spinner' }), ' 加载中…')));
    }

    function fetchRows(refresh) {
      var seq = ++requestSeq;
      renderLoading();
      var request = includeObsolete
        ? IU.api(base + '/segments?include_obsolete=true').then(IU.toArray)
        : ctx.requestParts(ctx.table, refresh);
      if (!includeObsolete && onDistributionStart) onDistributionStart();
      request.then(function (arr) {
        if (seq !== requestSeq || !box.isConnected) return;
        IU.clear(tbodySlot);
        if (!arr.length) {
          tbodySlot.append(h('tr', null,
            h('td', { class: 'empty-cell', colspan: '6' }, '无 segment')));
          return;
        }
        arr.forEach(function (sgRaw) {
          var sg = sgRaw && typeof sgRaw === 'object' ? sgRaw : { file_key: String(sgRaw) };
          tbodySlot.append(segmentRow(sg));
        });
      }).catch(function () {
        if (seq !== requestSeq || !box.isConnected) return;
        IU.clear(tbodySlot);
        tbodySlot.append(h('tr', null,
          h('td', { class: 'empty-cell', colspan: '6' }, 'segment 加载失败。 ',
            h('button', { class: 'btn sm', onclick: function () { fetchRows(true); } }, '重试'))));
      });
    }

    function stateBadge(state) {
      var s = String(state || 'unknown').toLowerCase();
      if (s === 'active') return IU.badge('active', 'ok');
      if (s === 'sealed') return IU.badge('sealed', 'info');
      if (s === 'obsolete') return IU.badge('obsolete', 'gray');
      return IU.badge(s, 'gray');
    }

    function segmentRow(sg) {
      var rawPath = IU.pick(sg, 'file_key', 'file', 'key', 'filename', 'path');
      var fileName = rawPath !== undefined ? String(rawPath).split('/').pop() : '—';
      var partition = IU.pick(sg, 'partition', 'partition_key', 'logical_part', 'part_key', 'logical');
      var rows = IU.pick(sg, 'rows', 'row_count');
      var state = IU.pick(sg, 'state', 'status');
      var stats = IU.pick(sg, 'stats', 'stats_json');
      if (typeof stats === 'string') {
        try { stats = JSON.parse(stats); } catch (e) { /* 保持字符串 */ }
      }

      var timeRange = timeRangeOf(sg, stats);
      var statsNode;
      if (stats && typeof stats === 'object' && Object.keys(stats).length) {
        statsNode = h('details', { class: 'stats-details' },
          h('summary', null, '列统计 (' + Object.keys(stats).length + ')'),
          h('pre', null, JSON.stringify(stats, null, 2)));
      } else {
        statsNode = h('span', { class: 'faint' }, '—');
      }

      var tr = h('tr', null,
        h('td', { title: rawPath !== undefined ? String(rawPath) : null }, fileName),
        h('td', null, partition !== undefined ? String(partition) : '—'),
        h('td', { class: 'num' }, rows !== undefined ? IU.fmt.int(rows) : h('span', { class: 'null-cell' }, '∅')),
        h('td', null, stateBadge(state)),
        h('td', null, timeRange),
        h('td', null, statsNode));
      return tr;
    }

    /* 时间范围：优先 stats 中 time/order_key 列的 min/max；容忍专用字段 */
    function timeRangeOf(sg, stats) {
      var lo = IU.pick(sg, 'time_min', 'min_ts', 'ts_min');
      var hi = IU.pick(sg, 'time_max', 'max_ts', 'ts_max');
      if ((lo === undefined || hi === undefined) && stats && typeof stats === 'object') {
        // 在 stats 里找疑似时间列（time/ts/order_key 命名优先）
        var keys = Object.keys(stats);
        var tKey = null;
        ['time', 'ts', 'order_key'].some(function (hint) {
          tKey = keys.find(function (k) { return k.toLowerCase().indexOf(hint) >= 0; });
          return !!tKey;
        });
        if (tKey && stats[tKey] && typeof stats[tKey] === 'object') {
          lo = lo !== undefined ? lo : stats[tKey].min;
          hi = hi !== undefined ? hi : stats[tKey].max;
        }
      }
      if (lo === undefined && hi === undefined) return h('span', { class: 'faint' }, '—');
      var fmtOne = function (v) {
        var n = Number(v);
        return Number.isFinite(n) && n > 1e12 ? IU.fmt.ts(n) : String(v);
      };
      return h('span', { class: 'mono', style: { fontSize: '12.5px' } }, fmtOne(lo) + ' ~ ' + fmtOne(hi));
    }

    draw();
    return function () { if (!includeObsolete) fetchRows(); };
  }
})();
