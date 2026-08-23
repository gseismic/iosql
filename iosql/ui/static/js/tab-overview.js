/* =====================================================================
 * iosql UI · tab-overview.js
 * Overview：库级统计卡片行 + 表清单表格（名称/行数/分区数/segment 数/
 * partition 策略/时序徽标）。字段按 /api/tables 契约防御式读取。
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
      var totalRows = tables.reduce(function (sum, t) {
        var n = Number(IU.pick(t, 'row_count', 'rows'));
        return sum + (Number.isFinite(n) ? n : 0);
      }, 0);

      IU.clear(root);
      root.append(
        h('div', { class: 'stat-row' },
          statCard('表数量', IU.fmt.int(tables.length)),
          statCard('总行数合计', tables.length ? IU.fmt.int(totalRows) : '—'),
          statCard('当前模式', ctx.mode === 'write'
            ? IU.badge('WRITABLE', 'warn')
            : IU.badge('READ-ONLY', 'ok'))),

        h('div', { class: 'card' },
          h('div', { class: 'card-title' },
            h('span', { class: 'micro-label' }, '表清单'),
            h('span', { class: 'faint', style: { fontSize: '11px' } }, '点击行进入该表 Browse Data')),
          buildTableEl(tables, ctx)));
    }
  };

  function statCard(label, value) {
    return h('div', { class: 'stat-card' },
      h('div', { class: 'micro-label' }, label),
      h('div', { class: 'stat-value' }, value));
  }

  function buildTableEl(tables, ctx) {
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
        numTd(IU.pick(t, 'row_count', 'rows')),
        numTd(IU.pick(t, 'part_count', 'partition_count', 'parts_count')),
        numTd(IU.pick(t, 'segment_count', 'segments_count')),
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

    function numTd(v) {
      if (v === null || v === undefined) return h('td', { class: 'num' }, h('span', { class: 'null-cell' }, '∅'));
      return h('td', { class: 'num' }, IU.fmt.int(v));
    }
  }
})();
