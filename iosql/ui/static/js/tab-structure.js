/* =====================================================================
 * iosql UI · tab-structure.js
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

      loadParts(base, partsBox);
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
   * parts 仅 key 列表 → chips；含行数数据 → 条形图（防御式渲染）。 */
  function loadParts(base, box) {
    IU.api(base + '/parts').then(function (resp) {
      var arr = IU.toArray(resp);
      IU.clear(box);
      box.append(h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '逻辑分区 · Parts'),
        h('span', { class: 'faint mono', style: { fontSize: '11px' } }, arr.length + ' 个')));

      if (!arr.length) {
        box.append(h('div', { class: 'empty-state' }, '无分区信息'));
        return;
      }

      // 提取每项的 key 与可选行数/segment 数
      var items = arr.map(function (it) {
        if (it === null || it === undefined) return { key: String(it), rows: undefined };
        if (typeof it !== 'object') return { key: String(it), rows: undefined };
        return {
          key: String(IU.pick(it, 'key', 'partition_key', 'partition', 'name', 'id') !== undefined ? IU.pick(it, 'key', 'partition_key', 'partition', 'name', 'id') : ''),
          rows: toNum(IU.pick(it, 'rows', 'row_count', 'count')),
          segs: toNum(IU.pick(it, 'segment_count', 'segments_count'))
        };
      });

      var hasRows = items.some(function (it) { return it.rows !== undefined; });
      if (!hasRows) {
        box.append(h('div', { class: 'chips' }, items.map(function (it) {
          return h('span', { class: 'chip' }, it.key);
        })));
        return;
      }

      var max = Math.max.apply(null, items.map(function (it) { return it.rows || 0; }).concat([1]));
      var chart = h('div', { class: 'bar-chart' });
      items.forEach(function (it) {
        var pct = Math.max(1.5, ((it.rows || 0) / max) * 100);
        chart.append(h('div', { class: 'bar-line' },
          h('span', { class: 'bar-key', title: it.key }, it.key),
          h('div', { class: 'bar-track' },
            h('div', { class: 'bar-fill', style: { width: pct + '%' } })),
          h('span', { class: 'bar-meta' },
            IU.fmt.int(it.rows || 0) + ' 行' + (it.segs !== undefined ? ' · ' + it.segs + ' segs' : ''))));
      });
      box.append(chart);
    }).catch(function () {
      IU.clear(box);
      box.append(h('div', { class: 'empty-state' }, '分区信息加载失败'));
    });

    function toNum(v) { var n = Number(v); return Number.isFinite(n) ? n : undefined; }
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
