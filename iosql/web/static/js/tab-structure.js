/* =====================================================================
 * iosql Web · tab-structure.js
 * Structure 三区块：契约卡（spec）→ 逻辑分区表（parts，chips 或条形图）
 * → 物理 segment 表（include_obsolete 开关重拉）。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

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

      loadParts(base, partsBox, ctx);
      renderSegmentsCard(base, segBox);
    }
  };

  /* ---------- 区块 A：契约卡 ---------- */
  function contractCard(spec) {
    spec = spec || {};
    var colTypes = (spec.columns && typeof spec.columns === 'object') ? spec.columns : {};
    var colListNode = Object.keys(colTypes).length
      ? h('div', null, Object.keys(colTypes).map(function (c) {
        return h('div', { class: 'mono', style: { fontSize: '12px' } },
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
   * （/segments 客户端聚合，PLAN-064 D4）。分布未到达时先渲染可读 chips
   * 并短暂轮询，到达后重绘为条形图；偏斜热点 amber 高亮；
   * 点击分区行 → 跳 Data 并携带时间范围过滤（可分享 URL，Q2）。 */
  function loadParts(base, box, ctx) {
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
          h('span', { class: 'faint mono', style: { fontSize: '11px' } }, keys.length + ' 个')));

        if (!keys.length) {
          box.append(h('div', { class: 'empty-state' }, '无分区信息'));
          return;
        }

        if (!hasDist) {
          box.append(h('div', { class: 'chips' }, keys.map(partChip)));
          if (!dist) {
            box.append(h('div', { class: 'faint', style: { fontSize: '11px', marginTop: '6px' } },
              h('span', { class: 'spinner', style: { width: '11px', height: '11px' } }),
              ' 行数分布统计中…'));
            scheduleRedraw();
          } else if (dist.error) {
            box.append(h('div', { class: 'faint', style: { fontSize: '11px', marginTop: '6px' } },
              '分区行数分布不可用（聚合失败）'));
          }
          return;
        }

        // 行数条形图（key 全为毫秒时间戳时按数值排序并推算下一分区上界）
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
        keys.forEach(function (k) {
          if (order.indexOf(k) < 0) items.push({ key: k, rows: dist.byPart[k] || 0 });
        });

        var rowsList = items.map(function (it) { return it.rows || 0; });
        var max = Math.max.apply(null, rowsList.concat([1]));
        var sorted = rowsList.slice().sort(function (a, b) { return a - b; });
        var median = sorted[Math.floor(sorted.length / 2)];
        var skew = median > 0 ? max / median > 3 : max > 0;

        var chart = h('div', { class: 'bar-chart' });
        items.forEach(function (it, i) {
          var pct = Math.max(1.5, ((it.rows || 0) / max) * 100);
          var isHot = skew && (it.rows || 0) === max;
          var readable = IU.fmt.date(it.key);
          var keyEl = h('span', { class: 'bar-key', title: it.key },
            readable ? h('span', { style: { color: 'var(--acc)' } }, readable) : it.key,
            readable ? h('span', { class: 'raw' }, it.key) : null);
          var line = h('div', { class: 'bar-line part-link', title: partClickTitle(ctx) },
            keyEl,
            h('div', { class: 'bar-track' },
              h('div', { class: 'bar-fill' + (isHot ? ' hot' : ''), style: { width: pct + '%' } })),
            h('span', { class: 'bar-meta' },
              IU.fmt.int(it.rows || 0) + ' 行'
              + (it.segs !== undefined ? ' · ' + it.segs + ' segs' : '')));
          if (allMs && it.segs !== undefined) {
            line.addEventListener('click', function () { gotoDataWithPartFilter(ctx, items, i); });
          }
          chart.append(line);
        });
        box.append(chart);
        box.append(h('div', { class: 'faint', style: { fontSize: '11px', marginTop: '6px' } },
          (skew ? '琥珀 = 偏斜热点（最大/中位数 > 3）。' : '')
          + (allMs ? '点击分区行 → Data 页按该时间范围过滤（URL 可分享）。' : '')));
      }

      function scheduleRedraw() {
        var tries = 0;
        var timer = setInterval(function () {
          var d = distNow();
          if ((d && d.order && d.order.length) || d && d.error || ++tries > 20) {
            clearInterval(timer);
            if (box.isConnected) draw();
          }
        }, 700);
      }

      draw();
    }).catch(function () {
      IU.clear(box);
      box.append(h('div', { class: 'empty-state' }, '分区信息加载失败'));
    });

    function partChip(key) {
      var readable = IU.fmt.date(key);
      return h('span', { class: 'chip' },
        readable ? h('span', { style: { color: 'var(--acc)' } }, readable) : key,
        readable ? h('span', { style: { color: 'var(--tx2)', marginLeft: '6px', fontSize: '10px' } }, key) : null);
    }
  }

  function partClickTitle(ctx) {
    var timeCol = timeColOf(ctx);
    return timeCol ? '点击：在 Data 页按该分区时间范围过滤' : '分区';
  }

  function timeColOf(ctx) {
    var t = ctx.spec && ctx.spec.time;
    if (!t) return null;
    return typeof t === 'string' ? t : (IU.pick(t, 'column', 'col', 'field', 'name') || null);
  }

  /* 跳转 Data 并带上该分区的时间范围过滤（between lo~hi，hi = 下一分区 key）。
   * 过滤语法与表头过滤行一致，Data 页从 URL query 种子读取（可分享）。 */
  function gotoDataWithPartFilter(ctx, items, idx) {
    var timeCol = timeColOf(ctx);
    if (!timeCol) return;
    var lo = items[idx].key;
    var next = items[idx + 1];
    var raw = next ? (lo + '~' + next.key) : '>=' + lo;
    var q = 'f.' + encodeURIComponent(timeCol) + '=' + encodeURIComponent(raw);
    location.hash = '#/table/' + encodeURIComponent(ctx.table) + '/data?' + q;
    IU.toast('已按分区 ' + (IU.fmt.date(lo) || lo) + ' 过滤', 'ok');
  }

  /* ---------- 区块 C：物理 segment 表 ---------- */
  function renderSegmentsCard(base, box) {
    var includeObsolete = false;

    var tbodySlot = h('tbody');
    var tableWrap = h('div', { class: 'table-wrap' });

    function draw() {
      IU.clear(box);
      var toggle = h('input', { type: 'checkbox', id: 'inc-obs' });
      toggle.checked = includeObsolete;
      toggle.addEventListener('change', function () {
        includeObsolete = toggle.checked;
        fetchRows();
      });
      box.append(
        h('div', { class: 'card-title' },
          h('span', { class: 'micro-label' }, '物理 Segment'),
          h('span', { class: 'spacer' }),
          h('label', { for: 'inc-obs', style: { display: 'flex', gap: '6px', alignItems: 'center', cursor: 'pointer', fontSize: '12px', color: 'var(--tx1)' } },
            toggle, '包含 obsolete')),
        tableWrap);
      if (!tableWrap.contains(tbodySlot.parentNode)) {
        tableWrap.append(h('table', { class: 'data-table' },
          headEl(), tbodySlot));
      }
      renderLoading();
      fetchRows();
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

    function fetchRows() {
      renderLoading();
      IU.api(base + '/segments?include_obsolete=' + (includeObsolete ? 'true' : 'false')).then(function (resp) {
        var arr = IU.toArray(resp);
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
        IU.clear(tbodySlot);
        tbodySlot.append(h('tr', null,
          h('td', { class: 'empty-cell', colspan: '6' }, 'segment 加载失败')));
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
      return h('span', { class: 'mono', style: { fontSize: '11.5px' } }, fmtOne(lo) + ' ~ ' + fmtOne(hi));
    }

    draw();
  }
})();
