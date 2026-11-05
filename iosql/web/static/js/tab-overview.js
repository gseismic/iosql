/* =====================================================================
 * iosql Web · tab-overview.js
 * Overview 库驾驶舱（PLAN-064 D2）：库级汇总条 + 表卡片网格。
 * 每张卡片展示行数、分区、segment、策略及分区分布提示
 * （分区行数分布迷你条形图，max/中位数 > 4 判偏斜 ⚠）。
 * 行数统计和分区分布由用户按需加载；后者聚合 /segments。
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
    var middle = Math.floor(sorted.length / 2);
    var mid = sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
    var max = sorted[sorted.length - 1];
    if (!mid) return max > 0 ? Infinity : 0;
    return max / mid;
  }
  IU.partitionSkew = skewRatio;
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
          h('span', { id: 'stat-total-hint', class: 'faint', style: { fontSize: '12px', marginLeft: '6px' } }, '')),
        ctx.mode === 'write' ? IU.badge('WRITABLE', 'warn') : IU.badge('READ-ONLY', 'ok'),
        h('span', { class: 'faint' }, '·'),
        h('span', { class: 'faint', style: { fontSize: '12px' } },
          '统计按需加载，可能扫描数据文件 · 行数可能延迟约 1 分钟'),
        h('span', { class: 'spacer' }),
        tables.length ? h('button', { id: 'stats-all-button', class: 'btn sm',
          title: '统计所有表的行数、分区数与 segment 数', onclick: ctx.requestAllStats }, '统计全部表') : null,
        h('button', { class: 'btn sm', title: '重新读取表清单；统计需要再次加载',
          onclick: function () { location.reload(); } }, '重新加载')));

      if (!tables.length) {
        root.append(h('div', { class: 'empty-state' },
          h('div', { style: { fontSize: '30px' } }, '▦'),
          h('div', { style: { color: 'var(--tx1)', fontWeight: 600, marginTop: '8px' } }, '这个库还没有表'),
          h('div', null, '用 Python API 创建第一张表后，这里会出现表卡片与分区分布'),
          h('div', { class: 'cmd-hint' },
            h('span', { class: 'mono' }, 'db.timeseries("ticks", time="ts", …)'),
            h('button', {
              class: 'btn sm',
              onclick: function () {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                  navigator.clipboard.writeText('db.timeseries("ticks", time="ts", pk=["symbol", "ts"], columns={"symbol": "text", "ts": "integer", "price": "float"}, partition_every="1day")')
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
      : (s && s.loading ? '…' : '—');

    /* 统计单元格占位：stats 到达后由 app.js 就地更新 */
    var partsTd = h('span', {
      dataset: { statsFor: name, kind: 'parts' },
      title: s && s.loading ? '统计中' : '尚未统计'
    }, '—');
    var segsTd = h('span', {
      dataset: { statsFor: name, kind: 'segs' },
      title: s && s.loading ? '统计中' : '尚未统计'
    }, '—');

    /* 时间范围来自各 segment 的实际时间统计，缺失时不推断 */
    var rangeEl = h('span', {
      class: 'faint',
      dataset: { partRange: name }
    }, '—');

    var content = h('a', {
      class: 'tc-content', href: '#/table/' + encodeURIComponent(name) + '/data',
      'aria-label': '打开 ' + name + ' 的数据'
    },
      h('div', { class: 'tc-head' },
        h('span', { class: 'tc-name' }, name),
        hasTime ? IU.badge('时序', 'info') : null),
      h('div', { class: 'tc-rows' },
        h('span', {
          dataset: { statsFor: name, kind: 'rows' },
          title: s && s.loading ? '统计中' : '尚未统计'
        }, rowsText + ' 行')),
      h('div', { class: 'tc-meta' },
        h('div', null, h('span', { class: 'k' }, '分区'), partsTd, ' 分区 · ', segsTd, ' segments'),
        h('div', null, h('span', { class: 'k' }, '时间（段统计）'), rangeEl),
        h('div', null, h('span', { class: 'k' }, '策略'),
          pstr !== undefined && pstr !== null
            ? h('span', { class: 'v', title: String(pstr) }, String(pstr))
            : h('span', { class: 'faint' }, '—'))),
      signalRow(name, dist, s));
    return h('article', { class: 'table-card' }, content,
      h('div', { class: 'tc-actions', dataset: { overviewActions: name } }));
  }

  IU.overviewTimeRange = function (dist) {
    if (!dist || dist.error || dist.timeMin === null || dist.timeMin === undefined ||
        dist.timeMax === null || dist.timeMax === undefined) return null;
    function format(v) { return typeof v === 'number' ? IU.fmt.ts(v) : String(v); }
    return format(dist.timeMin) + ' ~ ' + format(dist.timeMax);
  };

  /* 分布信号行：偏斜、均衡、单分区、加载或失败 */
  function signalRow(name, dist, s) {
    var slot = h('div', { class: 'tc-signal' });
    slot.dataset.partSignal = name;
    if (!dist) {
      slot.append(h('div', { class: 'tc-signal-row' },
        h('span', { class: 'faint' }, '分区分布未加载')));
      return slot;
    }
    if (dist.loading) {
      slot.append(h('div', { class: 'tc-signal-row' },
        h('span', { class: 'spinner', style: { width: '11px', height: '11px' } }),
        h('span', { class: 'faint' }, '分区分布统计中…')));
      return slot;
    }
    fillSignal(slot, name, dist);
    return slot;
  }

  /* 就地更新入口：app.js 在 parts 到达后调用 IU.overviewPartSignal */
  IU.overviewPartSignal = signalRow;

  IU.renderOverviewActions = function (slot, name, stats, dist, requestStats, requestParts) {
    IU.clear(slot);
    if (!stats || stats.loading || stats.error) {
      slot.append(h('button', { class: 'btn sm', disabled: !!(stats && stats.loading),
        onclick: function () { requestStats(name); } },
        stats && stats.loading ? '统计中…' : (stats && stats.error ? '重试统计' : '统计本表')));
    }
    if (!slot.dataset.compact && (!dist || dist.loading || dist.error)) {
      slot.append(h('button', { class: 'btn sm', disabled: !!(dist && dist.loading),
        title: '加载分区行数及物理 segment 统计，可能扫描数据文件',
        onclick: function () { requestParts(name).catch(function () {}); } },
        dist && dist.loading ? '分布加载中…' : (dist && dist.error ? '重试分布' : '加载分布')));
    }
  };

  function fillSignal(slot, name, dist) {
    if (dist.error) {
      slot.append(h('div', { class: 'tc-signal-row' },
        h('span', { class: 'badge err' }, '统计失败'),
        h('span', { class: 'faint' }, '分区分布不可用')));
      return;
    }
    if (!dist.order || !dist.order.length) {
      slot.append(h('div', { class: 'tc-signal-row' },
        h('span', { class: 'badge gray' }, '无分区数据')));
      return;
    }
    if (dist.order.length === 1) {
      slot.append(h('div', { class: 'tc-signal-row' },
        h('span', { class: 'badge gray' }, '单分区'),
        h('span', { class: 'faint' }, '不比较分布')));
      return;
    }
    var ratio = skewRatio(dist.order, dist.byPart);
    var hot = ratio > 4;
    var max = 0;
    dist.order.forEach(function (k) { max = Math.max(max, dist.byPart[k] || 0); });
    var maxBars = 60;
    var groupSize = Math.ceil(dist.order.length / maxBars);
    var bars = h('div', { class: 'mini-bars', title: dist.order.length + ' 个分区行数分布' +
      (groupSize > 1 ? '；每柱表示最多 ' + groupSize + ' 个分区中的最大行数' : '') });
    for (var i = 0; i < dist.order.length; i += groupSize) {
      var r = 0;
      for (var j = i; j < Math.min(i + groupSize, dist.order.length); j++) {
        r = Math.max(r, dist.byPart[dist.order[j]] || 0);
      }
      bars.append(h('div', {
        class: 'mb' + (hot && r === max ? ' hot' : ''),
        style: 'height:' + (r ? Math.max(8, (r / (max || 1)) * 100) : 0) + '%'
      }));
    }
    slot.append(h('div', { class: 'tc-signal-row' },
      hot
        ? h('span', {
          class: 'badge warn',
          title: '最大分区 ' + max + ' 行，约为中位数的 ' + (ratio === Infinity ? '∞' : ratio.toFixed(1)) + ' 倍；建议查看 Structure 或 layout_report'
        }, '⚠ 偏斜')
        : h('span', { class: 'badge info', title: '仅比较分区行数，不代表数据质量' }, '分布均衡'),
      bars));
    if (hot) {
      slot.append(h('div', { class: 'tc-hint' }, '最大分区行数显著偏高，可在 Structure 查看分布'));
    }
  }

  /* ---------------- 紧凑表格（>30 表退化，A4） ---------------- */
  function buildCompactTable(tables, stats) {
    var cols = ['名称', '行数', '分区数', 'Segment 数', 'Partition 策略', '能力', '统计状态'];
    var wrap = h('div', { class: 'table-wrap' });
    var table = h('table', { class: 'data-table' });
    table.append(h('thead', null, h('tr', null, cols.map(function (c, i) {
      return h('th', { class: i >= 1 && i <= 3 ? 'num' : '' }, c);
    }))));
    var tbody = h('tbody');
    tables.forEach(function (t) {
      var name = String(IU.pick(t, 'name', 'table'));
      var hasTime = !!(IU.pick(t, 'has_time', 'is_timeseries', 'time'));
      var tr = h('tr', null,
        h('td', { style: { fontWeight: 600 } },
          h('a', { class: 'table-link', href: '#/table/' + encodeURIComponent(name) + '/data' }, name)),
        statsTd(name, 'rows', stats),
        statsTd(name, 'parts', stats),
        statsTd(name, 'segs', stats),
        h('td', null, IU.pick(t, 'partition_str', 'partition') !== undefined ? String(IU.pick(t, 'partition_str', 'partition')) : '—'),
        h('td', null, hasTime ? IU.badge('时序', 'info') : h('span', { class: 'faint' }, '—')),
        h('td', null, h('span', { class: 'tc-actions', dataset: { overviewActions: name, compact: 'true' } })));
      tbody.append(tr);
    });
    table.append(tbody);
    wrap.append(table);

    return h('div', { class: 'card' },
      h('div', { class: 'card-title' },
        h('span', { class: 'micro-label' }, '表清单（' + tables.length + ' 张，紧凑视图）'),
        h('span', { class: 'faint', style: { fontSize: '12px' } }, '点击表名进入 Data')),
      wrap);

    function statsTd(n, kind, statsMap) {
      var s = statsMap[n];
      var key = { rows: 'row_count', parts: 'part_count', segs: 'segment_count' }[kind];
      var v = s ? s[key] : undefined;
      var text = typeof v === 'number' ? IU.fmt.int(v) : (s && s.loading ? '…' : '—');
      return h('td', {
        class: 'num',
        dataset: { statsFor: n, kind: kind },
        title: typeof v === 'number' ? null : (s && s.loading ? '统计中' : '尚未统计')
      }, text);
    }
  }
})();
