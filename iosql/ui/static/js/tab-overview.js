/* =====================================================================
 * iosql UI · tab-overview.js
 * Overview：库级统计卡片行 + 表清单表格（名称/行数/分区数/segment 数/
 * partition 策略/时序徽标）。统计字段来自渐进拉取（PLAN-060）：
 * 未到达时显示占位并标注 data-stats-for，由 app.js 就地更新。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  IU.tabs = IU.tabs || {};

  IU.tabs.overview = {
    id: 'overview',
    label: 'Overview',

    render: function (root, ctx) {
      var tables = ctx.tables || [];
      var stats = ctx.stats || {};
      var sum = 0;
      tables.forEach(function (t) {
        var s = stats[String(IU.pick(t, 'name', 'table'))];
        if (s && typeof s.row_count === 'number') sum += s.row_count;
      });

      IU.clear(root);
      root.append(
        h('div', { class: 'stat-row' },
          statCard('表数量', IU.fmt.int(tables.length)),
          statCard('总行数合计',
            h('span', null,
              h('span', { id: 'stat-total-rows' }, IU.fmt.int(sum)),
              h('span', { id: 'stat-total-hint', class: 'faint', style: { fontSize: '11px', marginLeft: '6px' } }, ''))),
          statCard('当前模式', ctx.mode === 'write'
            ? IU.badge('WRITABLE', 'warn')
            : IU.badge('READ-ONLY', 'ok'))),

        h('div', { class: 'card' },
          h('div', { class: 'card-title' },
            h('span', { class: 'micro-label' }, '表清单'),
            h('span', { class: 'faint', style: { fontSize: '11px' } }, '点击行进入该表 Browse Data')),
          buildTableEl(tables, stats)));
    }
  };

  function statCard(label, value) {
    return h('div', { class: 'stat-card' },
      h('div', { class: 'micro-label' }, label),
      h('div', { class: 'stat-value' }, value));
  }

  function buildTableEl(tables, stats) {
    var wrap = h('div', { class: 'table-wrap' });
    var table = h('table', { class: 'data-table' });
    var cols = [
      { key: 'name', label: '名称', numeric: false },
      { key: 'row_count', label: '行数', numeric: true },
      { key: 'partition_count', label: '分区数', numeric: true },
      { key: 'segment_count', label: 'Segment 数', numeric: true },
      { key: 'partition_str', label: 'Partition 策略', numeric: false },
      { key: 'has_time', label: '能力', numeric: false }
    ];

    table.append(h('thead', null, h('tr', null, cols.map(function (c) {
      return h('th', { class: c.numeric ? 'num' : '' }, c.label);
    }))));

    var tbody = h('tbody');
    if (!tables.length) {
      tbody.append(h('tr', null,
        h('td', { class: 'empty-cell', colspan: String(cols.length) }, '库中没有表')));
    }
    tables.forEach(function (t) {
      var name = IU.pick(t, 'name', 'table') || '(未知)';
      var hasTime = !!(IU.pick(t, 'has_time', 'is_timeseries', 'time'));
      var cells = [
        h('td', { style: { fontWeight: 600 } }, name),
        statsTd(name, 'rows', stats),
        statsTd(name, 'parts', stats),
        statsTd(name, 'segs', stats),
        h('td', null, String(IU.pick(t, 'partition_str', 'partition') !== undefined ? IU.pick(t, 'partition_str', 'partition') : '—')),
        h('td', null, hasTime ? IU.badge('时序', 'info') : h('span', { class: 'faint' }, '—'))
      ];
      var tr = h('tr', { class: 'clickable', title: '打开 ' + name });
      cells.forEach(function (td) { tr.append(td); });
      tr.addEventListener('click', function () {
        location.hash = '#/table/' + encodeURIComponent(name) + '/browse';
      });
      tbody.append(tr);
    });

    table.append(tbody);
    wrap.append(table);
    return wrap;

    /* 统计单元格：stats 已到显示数值，未到显示占位并标注由 app.js 更新 */
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
