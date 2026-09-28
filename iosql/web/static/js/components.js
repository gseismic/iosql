/* =====================================================================
 * iosql Web · components.js
 * 跨 Tab 复用组件：条件构造器、结果表格、单元格详情面板、Explain 计划。
 * 安全约定：所有接口数据经 textContent 渲染，禁止 innerHTML 插值动态数据。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  /* ================= 条件构造器 =================
   * 一行 = field 下拉 + op 下拉 + value 输入 + 行间 AND/OR 切换 + 删除。
   * between/in 的 value 按逗号解析为数组；数值型列自动强转数字。
   * 序列化：全 AND → 平铺 dict 数组（与 iosql 文档示例一致）；
   *         含 OR  → 按 OR 分组为 {or:[组...]}，纯 AND 组内平铺。 */
  var OPS = ['==', '!=', '>', '>=', '<', '<=', 'between', 'in'];

  function ConditionBuilder(columns, colTypes) {
    this.columns = columns && columns.length ? columns.slice() : [];
    this.colTypes = colTypes || {};
    this.rows = [this._newRow()];
    this.el = h('div', { class: 'cond-builder' });
    this.render();
  }

  ConditionBuilder.prototype._newRow = function () {
    return { field: this.columns[0] || '', op: '==', value: '', join: 'AND' };
  };

  ConditionBuilder.prototype.addRow = function () {
    var last = this.rows[this.rows.length - 1];
    var row = this._newRow();
    if (last) row.join = last.join; // 新行默认延续上一行的连接词
    this.rows.push(row);
    this.render();
  };

  ConditionBuilder.prototype.removeRow = function (i) {
    this.rows.splice(i, 1);
    if (!this.rows.length) this.rows.push(this._newRow());
    this.render();
  };

  ConditionBuilder.prototype.reset = function () {
    this.rows = [this._newRow()];
    this.render();
  };

  ConditionBuilder.prototype.render = function () {
    var self = this;
    IU.clear(this.el);

    var toolbar = h('div', { class: 'cond-toolbar' },
      h('span', { class: 'micro-label' }, '筛选条件'),
      h('span', { class: 'spacer' }),
      h('button', { class: 'btn sm ghost', onclick: function () { self.addRow(); } }, '＋ 条件行'),
      h('button', { class: 'btn sm ghost', onclick: function () { self.reset(); } }, '清空'));
    this.el.append(toolbar);

    this.rows.forEach(function (row, i) {
      var fieldSel = h('select', {
        class: 'input field-sel',
        onchange: function (e) { row.field = e.target.value; }
      }, self.columns.map(function (c) {
        return h('option', { value: c, selected: c === row.field }, c);
      }));

      var opSel = h('select', {
        class: 'input op-sel',
        onchange: function (e) { row.op = e.target.value; self.render(); }
      }, OPS.map(function (o) {
        return h('option', { value: o, selected: o === row.op }, o);
      }));

      var placeholder = row.op === 'between' ? '起, 止（逗号分隔）'
        : row.op === 'in' ? 'v1, v2, …（逗号分隔）' : '值';
      var valInput = h('input', {
        class: 'input mono val-input', type: 'text', value: row.value,
        placeholder: placeholder, autocomplete: 'off', spellcheck: 'false',
        oninput: function (e) { row.value = e.target.value; }
      });

      var joiner = h('span', { class: 'joiner' + (i === 0 ? ' first' : '') });
      if (i > 0) {
        joiner.append(h('select', {
          class: 'input', style: { fontSize: '11px', padding: '2px 4px' },
          onchange: function (e) { row.join = e.target.value; }
        },
          h('option', { value: 'AND', selected: row.join !== 'OR' }, 'AND'),
          h('option', { value: 'OR', selected: row.join === 'OR' }, 'OR')));
      }

      self.el.append(h('div', { class: 'cond-row' },
        joiner, fieldSel, opSel, valInput,
        h('button', {
          class: 'icon-btn', title: '删除此行',
          onclick: function () { self.removeRow(i); }
        }, '✕')));
    });
  };

  /* 校验并序列化 where；返回 {ok, where, msg} */
  ConditionBuilder.prototype.getWhere = function () {
    var conds = [];
    for (var i = 0; i < this.rows.length; i++) {
      var r = this.rows[i];
      var raw = String(r.value == null ? '' : r.value).trim();
      if (raw === '') continue; // 空值行跳过
      var t = this.colTypes[r.field];
      if (r.op === 'between') {
        var pair = IU.fmt.parseList(raw, t);
        if (pair.length !== 2) return { ok: false, where: null, msg: 'between 需要恰好 2 个逗号分隔的值：' + r.field };
        conds.push({ field: r.field, op: 'between', value: pair });
      } else if (r.op === 'in') {
        var list = IU.fmt.parseList(raw, t);
        if (!list.length) return { ok: false, where: null, msg: 'in 至少需要 1 个值：' + r.field };
        conds.push({ field: r.field, op: 'in', value: list });
      } else {
        conds.push({ field: r.field, op: r.op, value: IU.fmt.coerce(raw, t) });
      }
    }
    if (!conds.length) return { ok: true, where: null, msg: null };

    // 按 OR 分组：join 描述的是「本行与上一行之间」的连接词。
    // conds 与「非空值行」按出现顺序一一对应，线性扫描重建分组。
    var groups = [];
    var cur = [];
    var ci = 0;
    for (var k = 0; k < this.rows.length; k++) {
      var rr = this.rows[k];
      if (String(rr.value || '').trim() === '') continue;
      if (cur.length && rr.join === 'OR') { groups.push(cur); cur = []; }
      cur.push(conds[ci]);
      ci++;
    }
    if (cur.length) groups.push(cur);

    var where;
    if (groups.length === 1) {
      where = groups[0]; // 全 AND：平铺数组，与 README 示例格式一致
    } else {
      where = [{
        or: groups.map(function (g) {
          return g.length === 1 ? g[0] : { and: g };
        })
      }];
    }
    return { ok: true, where: where, msg: null };
  };

  IU.ConditionBuilder = ConditionBuilder;

  /* ================= 结果表格 =================
   * 斑马纹 + sticky 表头；数值列右对齐千分位；NULL → 灰色 ∅；
   * 可选列头排序、行点击回调（详情面板）。 */
  function renderResultTable(container, opts) {
    opts = opts || {};
    var columns = opts.columns || [];
    var rows = opts.rows || [];
    IU.clear(container);

    if (!rows.length) {
      container.append(h('div', { class: 'empty-state' }, opts.emptyText || '无数据'));
      return;
    }

    var thead = h('thead', null, h('tr', null, columns.map(function (c) {
      var sorted = opts.sortCol === c;
      var th = h('th', {
        class: (opts.sortable ? 'sortable ' : '') + (sorted ? 'sorted ' : '') + (IU.isNumericCol(c, opts.colTypes) ? 'num' : ''),
        onclick: opts.sortable && opts.onSort ? function () { opts.onSort(c); } : null,
        title: opts.sortable ? '点击按 ' + c + ' 排序' : null
      }, c, sorted ? (opts.sortDir === 'desc' ? ' ↓' : ' ↑') : '');
      return th;
    })));

    var tbody = h('tbody');
    rows.forEach(function (row) {
      var tr = h('tr', { class: (opts.onRowClick || opts.onCellClick) ? 'clickable' : '' });
      columns.forEach(function (c) {
        var v = row[c];
        var numeric = IU.isNumericCol(c, opts.colTypes, v) && typeof v === 'number';
        var td;
        if (v === null || v === undefined) {
          td = h('td', { class: numeric ? 'num' : '', title: 'NULL' }, h('span', { class: 'null-cell' }, '∅'));
        } else if (numeric) {
          td = h('td', { class: 'num', title: String(v) }, Number(v).toLocaleString('zh-CN'));
        } else if (typeof v === 'object') {
          td = h('td', { title: safeJson(v) }, safeJson(v));
        } else {
          td = h('td', null, String(v));
        }
        if (opts.onCellClick) {
          td.addEventListener('click', function () { opts.onCellClick(c, v, row); });
        }
        tr.append(td);
      });
      if (opts.onRowClick) tr.addEventListener('click', function () { opts.onRowClick(row); });
      tbody.append(tr);
    });

    container.append(h('div', { class: 'table-wrap' }, h('table', { class: 'data-table' }, thead, tbody)));
  }

  function safeJson(v) {
    try { return JSON.stringify(v); } catch (e) { return String(v); }
  }
  IU.renderResultTable = renderResultTable;

  /* ================= 单元格详情侧滑面板 =================
   * open(colName, value, opts)
   * - opts.actions: [{label, onClick}] 额外动作按钮（如"以此值过滤"）
   * - opts.note: 头部下方的备注行（如时间戳的可读时间） */
  var DetailPanel = {
    open: function (colName, value, opts) {
      opts = opts || {};
      var pre = document.getElementById('detail-pre');
      document.getElementById('detail-colname').textContent = colName || '';
      pre.textContent = prettyValue(value);

      var actionsSlot = document.getElementById('detail-actions');
      IU.clear(actionsSlot);
      (opts.actions || []).forEach(function (a) {
        actionsSlot.append(h('button', {
          class: 'btn sm ghost', onclick: a.onClick
        }, a.label));
      });

      var noteEl = document.getElementById('detail-note');
      noteEl.textContent = opts.note || '';
      noteEl.style.display = opts.note ? '' : 'none';

      document.body.classList.add('detail-open');
      this._current = value;
    },
    close: function () { document.body.classList.remove('detail-open'); },
    _current: null
  };

  /* 完整值文本化：对象/JSON 字符串自动 pretty，其余 String() */
  function prettyValue(v) {
    if (v === null || v === undefined) return '∅ (NULL)';
    if (typeof v === 'object') return JSON.stringify(v, null, 2);
    if (typeof v === 'string') {
      var s = v.trim();
      if ((s[0] === '{' && s[s.length - 1] === '}') || (s[0] === '[' && s[s.length - 1] === ']')) {
        try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { /* 非 JSON 字符串 */ }
      }
    }
    return String(v);
  }

  document.getElementById('detail-close').addEventListener('click', function () { DetailPanel.close(); });
  document.getElementById('detail-mask').addEventListener('click', function () { DetailPanel.close(); });
  // 标题承诺的 Esc 关闭；输入框内的 Esc（如过滤行清空本列）已 stopPropagation，
  // 不会误关；确认弹窗打开时优先让弹窗响应，不穿透关闭面板
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var modalRoot = document.getElementById('modal-root');
    if (modalRoot && modalRoot.childElementCount) return;
    DetailPanel.close();
  });
  document.getElementById('detail-copy').addEventListener('click', function () {
    var text = document.getElementById('detail-pre').textContent;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { IU.toast('已复制到剪贴板', 'ok'); });
    }
  });

  IU.DetailPanel = DetailPanel;

  /* ================= Explain 计划面板 =================
   * Partitions 与 Segments 两级块：candidate/scanned/skipped 计数 +
   * skip_reasons 列表；字段缺失则隐藏对应块（防御式）。 */
  function renderPlanPanel(container, plan) {
    IU.clear(container);
    if (!plan || typeof plan !== 'object') {
      container.append(h('div', { class: 'empty-state' }, '计划数据为空'));
      return;
    }
    var blocks = h('div', { class: 'plan-blocks' });
    var p = pickLevel(plan, ['partitions', 'partition_plan', 'partition']);
    var s = pickLevel(plan, ['segments', 'segment_plan', 'segment']);
    var any = false;
    if (p) { blocks.append(planBlock('Partitions · 一级裁剪（逻辑分区）', p)); any = true; }
    if (s) { blocks.append(planBlock('Segments · 二级裁剪（物理段）', s)); any = true; }

    container.append(
      h('div', { class: 'card-title' }, h('span', { class: 'micro-label' }, '查询计划'),
        h('span', { class: 'faint mono', style: { fontSize: '11px' } }, explainSummary(plan))),
      any ? blocks : h('div', { class: 'empty-state' }, '计划未包含 partitions/segments 结构（契约外形态），原始输出见下'),
      !any ? h('details', { class: 'stats-details' }, h('summary', null, '原始 JSON'), h('pre', null, JSON.stringify(plan, null, 2))) : null);

    function explainSummary(pl) {
      var total = IU.pick(pl, 'total_partitions', 'candidate_partitions');
      var scanned = IU.pick(pl, 'scanned_partitions');
      if (total === undefined && scanned === undefined) return '';
      return '候选 ' + (total === undefined ? '?' : total) + ' · 扫描 ' + (scanned === undefined ? '?' : scanned);
    }
  }

  /* 从 plan 中提取一级对象；容忍计数平铺在根上的形态 */
  function pickLevel(plan, keys) {
    for (var i = 0; i < keys.length; i++) {
      var v = plan[keys[i]];
      if (v && typeof v === 'object') return v;
    }
    // 兜底：partitions 计数直接挂在 plan 根上
    var cand = IU.pick(plan, 'candidate_partitions', 'total_partitions');
    if (cand !== undefined) {
      return {
        candidate: cand,
        scanned: IU.pick(plan, 'scanned_partitions'),
        skipped: IU.pick(plan, 'skipped_partitions'),
        skip_reasons: IU.pick(plan, 'skip_reasons')
      };
    }
    return null;
  }

  function countOf(v) {
    if (typeof v === 'number') return v;
    if (Array.isArray(v)) return v.length;
    return undefined;
  }

  function planBlock(title, obj) {
    var candidate = countOf(IU.pick(obj, 'candidate', 'candidates', 'total', 'total_count'));
    var scanned = countOf(IU.pick(obj, 'scanned', 'scanned_count', 'visited'));
    var skipped = countOf(IU.pick(obj, 'skipped', 'skipped_count', 'pruned'));
    var reasons = IU.pick(obj, 'skip_reasons', 'reasons');

    var counts = h('div', { class: 'plan-counts' },
      planCount('候选', candidate, 'c-cand'),
      planCount('扫描', scanned, 'c-scan'),
      planCount('跳过', skipped, 'c-skip'));

    var reasonsEl = null;
    if (Array.isArray(reasons) && reasons.length) {
      reasonsEl = h('ul', { class: 'skip-reasons' },
        reasons.map(function (r) {
          return h('li', null, typeof r === 'string' ? r : JSON.stringify(r));
        }));
    } else if (reasons && typeof reasons === 'object') {
      reasonsEl = h('pre', { style: { fontSize: '11px', color: 'var(--tx1)', margin: '4px 0 0' } },
        JSON.stringify(reasons, null, 2));
    }

    return h('div', { class: 'plan-block' },
      h('div', { class: 'micro-label' }, title),
      counts,
      reasonsEl);
  }

  function planCount(label, n, cls) {
    return h('div', { class: 'plan-count' },
      h('div', { class: 'micro-label' }, label),
      h('div', { class: 'n ' + (cls || '') }, n === undefined ? '—' : String(n)));
  }

  IU.renderPlanPanel = renderPlanPanel;

  /* ================= KV 网格卡（契约卡等） =================
   * pairs: [label, value]；value 为 null/undefined/'' 时显示 — */
  function kvGrid(pairs) {
    return h('div', { class: 'kv-grid' }, pairs.map(function (p) {
      var v = p[1];
      var shown = (v === null || v === undefined || v === '') ? h('span', { class: 'faint' }, '—') : v;
      return h('div', { class: 'kv-item' },
        h('div', { class: 'kv-key' }, p[0]),
        h('div', { class: 'kv-val' }, shown));
    }));
  }
  IU.kvGrid = kvGrid;

  /* spec 中 time 契约的展示文本（可能是字符串或对象） */
  function describeTime(t) {
    if (!t) return null;
    if (typeof t === 'string') return t;
    if (typeof t === 'object') {
      var col = IU.pick(t, 'column', 'col', 'field', 'name');
      var unit = IU.pick(t, 'unit', 'time_unit');
      return col ? (unit ? col + ' (' + unit + ')' : col) : JSON.stringify(t);
    }
    return String(t);
  }
  IU.describeTime = describeTime;
})();
