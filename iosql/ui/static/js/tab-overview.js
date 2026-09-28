/* =====================================================================
 * iosql UI · tab-overview.js
 * Overview 库驾驶舱（PLAN-064 D2）：库级汇总条 + 表卡片网格。
 * 每张卡片回答"这张表数据进对了没有"：行数/分区/segment/策略 + 健康信号
 * （分区行数分布迷你条形图，max/中位数 > 4 判偏斜 ⚠）。
 * 数据来源：stats 渐进泵（PLAN-060）+ parts 分布泵（app.js，聚合 /segments）。
 * 布局演进：>30 张表自动退化为紧凑表格（A4）。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  var CARD_VIEW_MAX = 30;

  /* 偏斜判定：最大分区行数 / 中位数 > 4（中位数为 0 时任一分区有行即偏斜） */
  function skewRatio(order, byPart) {
    var rows = order.map(function (k) { return byPart[k] || 0; });
    if (!rows.length) return 0;
    var sorted = rows.slice().sort(function (a, b) { return a - b; });
    var mid = sorted[Math.floor(sorted.length / 2)];
    var max = sorted[sorted.length - 1];
    if (!mid) return max > 0 ? Infinity : 0;
    return max / mid;
  }
  IU.overviewPartSignal = null; // 由下方赋值：app.js refreshStatsViews 调用

  IU.tabs = IU.tabs || {};

  IU.tabs.overview = {
    id: 'overview',
    label: 'Overview',

    render: function (root, ctx) {
      var tables = ctx.tables || [];
      var stats = ctx.stats || {};
      var parts = ctx.parts || {};
      var sum = 0;
      tables.forEach(function (t) {
        var s = stats[String(IU.pick(t, 'name', 'table'))];
        if (s && typeof s.row_count === 'number') sum += s.row_count;
      });

      IU.clear(root);

      /* --- 库级汇总条（弱化为一行，P2 层） --- */
      root.append(h('div', { class: 'lib-strip' },
        h('span', null, h('b', null, String(tables.length)), ' 张表'),
        h('span', null,
          h('b', { id: 'stat-total-rows' }, IU.fmt.int(sum)),
          ' 行合计',
          h('span', { id: 'stat-total-hint', class: 'faint', style: { fontSize: '11px', marginLeft: '6px' } }, '')),
        ctx.mode === 'write' ? IU.badge('WRITABLE', 'warn') : IU.badge('READ-ONLY', 'ok'),
        h('span', { class: 'faint' }, '·'),
        h('span', { class: 'faint mono', style: { fontSize: '11px' } },
          '行数渐进加载 · 偏斜 = 最大分区/中位数 > 4')));

      if (!tables.length) {
        root.append(h('div', { class: 'empty-state' },
          h('div', { style: { fontSize: '30px' } }, '▦'),
          h('div', { style: { color: 'var(--tx1)', fontWeight: 600, marginTop: '8px' } }, '这个库还没有表'),
          h('div', null, '用 Python API 或 CLI 建第一张表后，这里会出现表卡片与健康度信号'),
          h('div', { class: 'cmd-hint' },
            h('span', { class: 'mono' }, 'db.timeseries("ticks", time="ts", …)'),
            h('button', {
              class: 'btn sm',
              onclick: function () {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                  navigator.clipboard.writeText('db.timeseries("ticks", time="ts", pk=["symbol", "ts"], columns={"symbol": "text"}, partition_every="1day")')
                    .then(function () { IU.toast('示例代码已复制', 'ok'); });
                }
              }
            }, '复制示例'))));
        return;
      }

      if (tables.length > CARD_VIEW_MAX) {
        root.append(buildCompactTable(tables, stats));
        return;
      }
      root.append(buildCardGrid(tables, stats, parts));
    }
  };

  /* ---------------- 卡片网格（默认） ---------------- */
  function buildCardGrid(tables, stats, parts) {
    var grid = h('div', { class: 'table-card-grid' });
    tables.forEach(function (t) {
      grid.append(tableCard(t, stats, parts));
    });
    return grid;
  }

  function tableCard(t, stats, parts) {
    var name = String(IU.pick(t, 'name', 'table'));
    var hasTime = !!(IU.pick(t, 'has_time', 'is_timeseries', 'time'));
    var pstr = IU.pick(t, 'partition_str', 'partition');
    var s = stats[name];
    var dist = parts[name];

    var rowsText = typeof (s || {}).row_count === 'number'
      ? IU.fmt.int(s.row_count)
      : (s && s.error ? '—' : '…');

    /* 统计单元格占位：stats 到达后由 app.js 就地更新 */
    var partsTd = h('span', {
      dataset: { statsFor: name, kind: 'parts' },
      title: s ? null : '统计中，稍后自动填充'
    }, '…');
    var segsTd = h('span', {
      dataset: { statsFor: name, kind: 'segs' },
      title: s ? null : '统计中，稍后自动填充'
    }, '…');

    /* 时间范围由分区分布推导，parts 到达后就地更新（data-part-range） */
    var rangeEl = h('span', {
      class: 'faint',
      dataset: { partRange: name }
    }, '—');

    var card = h('div', {
      class: 'table-card', title: '打开 ' + name + ' 的 Data 页',
      onclick: function () {
        location.hash = '#/table/' + encodeURIComponent(name) + '/data';
      }
    },
      h('div', { class: 'tc-head' },
        h('span', { class: 'tc-name' }, name),
        hasTime ? IU.badge('时序', 'info') : null,
        s && s.error ? IU.badge('统计失败', 'err') : null),
      h('div', { class: 'tc-rows' },
        h('span', {
          dataset: { statsFor: name, kind: 'rows' },
          title: s ? null : '统计中，稍后自动填充'
        }, rowsText + ' 行')),
      h('div', { class: 'tc-meta' },
        h('div', null, h('span', { class: 'k' }, '分区'), partsTd, ' 分区 · ', segsTd, ' segments'),
        h('div', null, h('span', { class: 'k' }, '范围'), rangeEl),
        h('div', null, h('span', { class: 'k' }, '策略'),
          pstr !== undefined && pstr !== null
            ? h('span', { class: 'v', title: String(pstr) }, String(pstr))
            : h('span', { class: 'faint' }, '—'))),
      signalRow(name, dist, s));

    return card;
  }

  /* 时间范围：由分区 key（毫秒量级）推导；无分区/非毫秒显示 — */
  function partRangeEl(name, dist) {
    if (!dist || dist.error || !dist.order || !dist.order.length) {
      return h('span', { class: 'faint' }, '—');
    }
    var keys = dist.order.map(Number).filter(function (n) { return Number.isFinite(n) && n >= 1e12; });
    if (!keys.length) return h('span', { class: 'faint' }, '—');
    var lo = IU.fmt.date(Math.min.apply(null, keys));
    var hi = IU.fmt.date(Math.max.apply(null, keys));
    return h('span', { class: 'v' }, lo ? lo + ' ~ ' + (hi || '') : '—');
  }

  /* 健康信号行：偏斜 ⚠ + 迷你条形图 / 健康 / 无分区 / 等待统计 */
  function signalRow(name, dist, s) {
    var slot = h('div', { class: 'tc-signal' });
    if (!dist) {
      slot.append(h('div', { class: 'tc-signal-row' },
        h('span', { class: 'spinner', style: { width: '11px', height: '11px' } }),
        h('span', { class: 'faint' }, '分区分布统计中…')));
      slot.dataset.partSignal = name; // parts 泵到达后就地更新
      return slot;
    }
    fillSignal(slot, name, dist);
    return slot;
  }

  /* 就地更新入口：app.js 在 parts 到达后调用 IU.overviewPartSignal */
  IU.overviewPartSignal = function (name, dist) {
    var slot = h('div', { class: 'tc-signal' });
    slot.dataset.partSignal = name;
    fillSignal(slot, name, dist);
    return slot;
  };

  function fillSignal(slot, name, dist) {
    if (dist.error || !dist.order || !dist.order.length) {
      slot.append(h('div', { class: 'tc-signal-row' },
        h('span', { class: 'badge gray' }, '无分区'),
        h('span', { class: 'faint' }, '普通表')));
      return;
    }
    var ratio = skewRatio(dist.order, dist.byPart);
    var hot = ratio > 4;
    var max = 0;
    dist.order.forEach(function (k) { max = Math.max(max, dist.byPart[k] || 0); });
    var bars = h('div', { class: 'mini-bars', title: dist.order.length + ' 个分区行数分布' });
    dist.order.forEach(function (k) {
      var r = dist.byPart[k] || 0;
      bars.append(h('div', {
        class: 'mb' + (hot && r === max ? ' hot' : ''),
        // 注意：style 必须是字符串（h() 的 style 对象不受支持，会导致高度丢失）
        style: 'height:' + Math.max(8, (r / (max || 1)) * 100) + '%'
      }));
    });
    slot.append(h('div', { class: 'tc-signal-row' },
      hot
        ? h('span', {
          class: 'badge warn',
          title: '最大分区 ' + max + ' 行，约为中位数的 ' + (ratio === Infinity ? '∞' : ratio.toFixed(1)) + ' 倍；建议查看 Structure 或 layout_report'
        }, '⚠ 偏斜')
        : h('span', { class: 'badge ok' }, '健康'),
      bars));
    if (hot) {
      slot.append(h('div', { class: 'tc-hint' }, '尾部高亮分区行数显著偏肥 · 点击卡片查看数据，Structure 看分布'));
    }
  }

  /* ---------------- 紧凑表格（>30 表退化，A4） ---------------- */
  function buildCompactTable(tables, stats) {
    var cols = ['名称', '行数', '分区数', 'Segment 数', 'Partition 策略', '能力'];
    var wrap = h('div', { class: 'table-wrap' });
    var table = h('table', { class: 'data-table' });
    table.append(h('thead', null, h('tr', null, cols.map(function (c, i) {
      return h('th', { class: i >= 1 && i <= 3 ? 'num' : '' }, c);
    }))));
    var tbody = h('tbody');
    tables.forEach(function (t) {
      var name = String(IU.pick(t, 'name', 'table'));
      var hasTime = !!(IU.pick(t, 'has_time', 'is_timeseries', 'time'));
      var tr = h('tr', {
        class: 'clickable', title: '打开 ' + name,
        onclick: function () { location.hash = '#/table/' + encodeURIComponent(name) + '/data'; }
      },
        h('td', { style: { fontWeight: 600 } }, name),
        statsTd(name, 'rows', stats),
        statsTd(name, 'parts', stats),
        statsTd(name, 'segs', stats),
        h('td', null, IU.pick(t, 'partition_str', 'partition') !== undefined ? String(IU.pick(t, 'partition_str', 'partition')) : '—'),
        h('td', null, hasTime ? IU.badge('时序', 'info') : h('span', { class: 'faint' }, '—')));
      tbody.append(tr);
    });
    table.append(tbody);
    wrap.append(table);

    return h('div', { class: 'card' },
      h('div', { class: 'card-title' },
        h('span', { class: 'micro-label' }, '表清单（' + tables.length + ' 张，紧凑视图）'),
        h('span', { class: 'faint', style: { fontSize: '11px' } }, '点击行进入该表 Data')),
      wrap);

    function statsTd(n, kind, statsMap) {
      var s = statsMap[n];
      var key = { rows: 'row_count', parts: 'part_count', segs: 'segment_count' }[kind];
      var v = s ? s[key] : undefined;
      var text = typeof v === 'number' ? IU.fmt.int(v) : (s && s.error ? '—' : '…');
      return h('td', {
        class: 'num',
        dataset: { statsFor: n, kind: kind },
        title: typeof v === 'number' ? null : '统计中，稍后自动填充'
      }, text);
    }
  }
})();
