/* =====================================================================
 * iosql Web · util.js
 * 核心工具层：DOM 构造辅助 h()、fetch 统一封装、toast、格式化、弹窗。
 * 安全约定：接口数据一律经 textContent 渲染（h() 的字符串子节点走
 * createTextNode），全站禁止用 innerHTML 插值任何动态数据。
 * ===================================================================== */
(function () {
  'use strict';
  window.IU = window.IU || {};

  /* ---------- DOM 构造辅助 ----------
   * h('div', {class:'x', onclick: fn, dataset:{id:'1'}}, child, ...)
   * 子节点支持字符串 / Node / 嵌套数组 / null（忽略）。
   * 字符串一律 textContent 化，天然防注入。 */
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined || v === false) return;
        if (k === 'class') el.className = String(v);
        else if (k === 'dataset') Object.assign(el.dataset, v);
        else if (k.slice(0, 2) === 'on' && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
        else if (v === true) el.setAttribute(k, '');
        else el.setAttribute(k, String(v));
      });
    }
    for (var i = 2; i < arguments.length; i++) appendChild(el, arguments[i]);
    return el;
  }
  function appendChild(el, c) {
    if (c === null || c === undefined || c === false || c === '') return;
    if (Array.isArray(c)) { c.forEach(function (x) { appendChild(el, x); }); return; }
    el.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  IU.h = h;

  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  IU.clear = clear;

  /* ---------- fetch 统一封装 ----------
   * - 全部相对路径（同源部署）
   * - 错误契约统一为 {detail: str, hint?: str}，此处解析并弹 toast
   * - 成功返回已解析 JSON；失败抛 Error（附 .status/.hint） */
  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (resp) {
      return resp.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { /* 非 JSON 响应体 */ }
        if (!resp.ok) {
          var detail = (data && (data.detail || data.message)) || ('HTTP ' + resp.status + ' ' + resp.statusText);
          var hint = data && data.hint;
          toast(detail, 'err', hint);
          var err = new Error(detail);
          err.status = resp.status;
          err.hint = hint;
          throw err;
        }
        return data;
      });
    }, function () {
      toast('网络请求失败', 'err', path + ' 无法访问，请确认服务是否在线');
      throw new Error('network error: ' + path);
    });
  }
  IU.api = api;

  /* ---------- toast 轻提示 ---------- */
  function toast(msg, kind, hint) {
    var root = document.getElementById('toast-root');
    if (!root) return;
    var item = h('div', { class: 'toast ' + (kind || '') },
      h('div', { class: 't-msg' }, String(msg)),
      hint ? h('div', { class: 't-hint' }, String(hint)) : null);
    item.addEventListener('click', function () { item.remove(); });
    root.append(item);
    setTimeout(function () { item.remove(); }, 4600);
  }
  IU.toast = toast;

  /* ---------- 格式化 ---------- */
  var RE_INT = /^-?\d+$/;
  var RE_FLOAT = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/;

  /* 千分位整数；非有限数值原样返回 */
  function fmtInt(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    return Number.isFinite(n) ? n.toLocaleString('zh-CN') : String(v);
  }

  /* 字节数人性化 */
  function fmtBytes(v) {
    if (v === null || v === undefined || v === '') return '—';
    var n = Number(v);
    if (!Number.isFinite(n)) return String(v);
    var units = ['B', 'KB', 'MB', 'GB', 'TB'];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + ' ' + units[i];
  }

  /* 毫秒时间戳 → 本地时间文本；非毫秒量级原样返回 */
  function fmtTs(v) {
    if (typeof v !== 'number' || v < 1e12) return String(v);
    var d = new Date(v);
    return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleString('zh-CN', { hour12: false });
  }

  /* 按列类型把输入字符串强转为查询值：
   * 数值型列尝试转 int/float，其余保持字符串（避免误转 symbol="007"）。 */
  function coerceValue(raw, colType) {
    var s = String(raw).trim();
    var t = (colType || '').toLowerCase();
    var numeric = t.indexOf('int') >= 0 || t.indexOf('real') >= 0 ||
                  t.indexOf('float') >= 0 || t.indexOf('double') >= 0 ||
                  t.indexOf('numeric') >= 0 || t.indexOf('number') >= 0;
    if (numeric) {
      if (RE_INT.test(s)) return parseInt(s, 10);
      if (RE_FLOAT.test(s)) return parseFloat(s);
    }
    return s;
  }

  /* between/in 的逗号分隔解析 → 数组值 */
  function parseListValue(raw, colType) {
    return String(raw).split(',').map(function (s) { return s.trim(); })
      .filter(function (s) { return s !== ''; })
      .map(function (s) { return coerceValue(s, colType); });
  }

  IU.fmt = {
    int: fmtInt,
    bytes: fmtBytes,
    ts: fmtTs,
    coerce: coerceValue,
    parseList: parseListValue,
    NUMERIC_RE: /^(int|integer|bigint|smallint|real|float|double|numeric|decimal|number)$/i
  };

  /* 判断列是否数值列：优先看 spec 类型，退化看值本身 */
  function isNumericCol(colName, colTypes, sampleVal) {
    var t = colTypes && colTypes[colName];
    if (t) return IU.fmt.NUMERIC_RE.test(t);
    return typeof sampleVal === 'number';
  }
  IU.isNumericCol = isNumericCol;

  /* ---------- 确认弹窗 ----------
   * confirmModal({title, message, note, confirmText, danger, typeName})
   * - danger: 红色主题
   * - typeName: 提供后必须精确手输该名称才能点击确认（危险操作防呆）
   * 返回 Promise<boolean>。Esc / 遮罩点击 = 取消。 */
  function confirmModal(cfg) {
    return new Promise(function (resolve) {
      var root = document.getElementById('modal-root');
      var settled = false;
      function done(ok) {
        if (settled) return;
        settled = true;
        document.removeEventListener('keydown', onKey);
        root.innerHTML = '';
        resolve(ok);
      }
      function onKey(e) { if (e.key === 'Escape') done(false); }

      var confirmBtn = h('button', { class: 'btn ' + (cfg.danger ? 'danger' : 'primary'), onclick: function () { done(true); } },
        cfg.confirmText || '确认');

      var bodyKids = [];
      if (cfg.message) bodyKids.push(h('p', { style: { margin: '0 0 8px' } }, cfg.message));
      if (cfg.note) bodyKids.push(h('div', { class: 'modal-note' }, cfg.note));

      if (cfg.typeName) {
        var input = h('input', {
          class: 'input mono', placeholder: '请输入 "' + cfg.typeName + '" 以确认',
          autocomplete: 'off', spellcheck: 'false'
        });
        confirmBtn.disabled = true;
        input.addEventListener('input', function () {
          confirmBtn.disabled = input.value.trim() !== cfg.typeName;
        });
        input.addEventListener('keydown', function (e) {
          if (e.key === 'Enter' && !confirmBtn.disabled) done(true);
        });
        bodyKids.push(h('div', { class: 'confirm-input-row' }, input));
      }

      var modal = h('div', { class: 'modal' + (cfg.danger ? ' danger' : '') },
        h('h3', null, cfg.title || '确认操作'),
        bodyKids,
        h('div', { class: 'modal-actions' },
          h('button', { class: 'btn ghost', onclick: function () { done(false); } }, '取消'),
          confirmBtn));

      var mask = h('div', { class: 'modal-mask', onclick: function (e) { if (e.target === mask) done(false); } }, modal);
      root.append(mask);
      document.addEventListener('keydown', onKey);
      var firstInput = modal.querySelector('input');
      if (firstInput) firstInput.focus();
    });
  }
  IU.confirmModal = confirmModal;

  /* ---------- 下拉菜单（导出等） ---------- */
  function menu(summaryText, items) {
    var wrap = h('div', { class: 'menu' },
      h('button', { class: 'btn', onclick: function () { wrap.classList.toggle('open'); } }, summaryText, ' ▾'),
      h('div', { class: 'menu-list' },
        items.map(function (it) {
          return h('button', { onclick: function () { wrap.classList.remove('open'); it.onClick(); } }, it.label);
        })));
    return wrap;
  }
  IU.menu = menu;

  /* 点击菜单外部收起：全局只挂一次委托监听，避免每次 menu() 累积监听器 */
  document.addEventListener('click', function (e) {
    document.querySelectorAll('.menu.open').forEach(function (m) {
      if (!m.contains(e.target)) m.classList.remove('open');
    });
  });

  /* ---------- 徽章 ---------- */
  function badge(text, kind) { return h('span', { class: 'badge ' + (kind || 'gray') }, text); }
  IU.badge = badge;

  /* ---------- 结果对象防御式取值 ----------
   * 后端序列化层字段名可能存在别名，按候选键顺序取第一个非空值 */
  function pick(obj) {
    if (!obj || typeof obj !== 'object') return undefined;
    for (var i = 1; i < arguments.length; i++) {
      var v = obj[arguments[i]];
      if (v !== null && v !== undefined) return v;
    }
    return undefined;
  }
  IU.pick = pick;

  /* 解包包裹式响应：后端读写端点返回 {report}/{validation}/{result}，
   * 前端统一取内层对象；键不存在或为 null 时原样返回响应 */
  function unwrap(resp, key) {
    if (resp && typeof resp === 'object' &&
        resp[key] !== undefined && resp[key] !== null) {
      return resp[key];
    }
    return resp;
  }
  IU.unwrap = unwrap;

  /* 把响应统一成数组：容忍裸数组 / {rows:[]} / {items:[]} / {data:[]} 等形态 */
  function toArray(resp) {
    if (Array.isArray(resp)) return resp;
    if (resp && typeof resp === 'object') {
      var cand = pick(resp, 'rows', 'items', 'data', 'list', 'parts', 'segments', 'tables');
      if (Array.isArray(cand)) return cand;
    }
    return [];
  }
  IU.toArray = toArray;

  /* 行结果归一化 → {columns:[], rows:[dict]}。
   * 容忍三种形态：dict 行 / 数组行(+columns) / 数组行(无 columns，生成 colN)。 */
  function normalizeRows(resp) {
    var columns = resp && Array.isArray(resp.columns) ? resp.columns.slice() : null;
    var rawRows = toArray(resp);
    var rows = rawRows.map(function (r) {
      if (r && typeof r === 'object' && !Array.isArray(r)) return r;
      if (Array.isArray(r)) {
        var o = {};
        (columns || r.map(function (_, i) { return 'col' + i; })).forEach(function (c, i) { o[c] = r[i]; });
        return o;
      }
      return { value: r };
    });
    if (!columns) {
      var seen = [];
      rows.forEach(function (r) {
        Object.keys(r).forEach(function (k) { if (seen.indexOf(k) < 0) seen.push(k); });
      });
      columns = seen;
    }
    return { columns: columns, rows: rows };
  }
  IU.normalizeRows = normalizeRows;

  /* ---------- 全局快捷键注册 ----------
   * 输入控件聚焦时不拦截；←/→ 翻页仅在 Browse Tab 生效（由 app.js 裁决）。 */
  function globalKeys(handler) {
    document.addEventListener('keydown', function (e) {
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return;
      handler(e);
    });
  }
  IU.globalKeys = globalKeys;

  /* ---------- 时间语义统一（PLAN-064 D3） ----------
   * 毫秒时间戳是存储细节，不该要求用户输入。接受三种写法：
   *   2025-01-05 / 2025-01-05 10:30 / 1735689600000（≥12 位纯数字按毫秒）
   * 返回 {ok, ms, text}；解析失败 ok=false，调用方自行提示。 */
  function parseTimeInput(raw) {
    var s = String(raw == null ? '' : raw).trim();
    if (!s) return { ok: false, ms: null, text: s };
    if (/^-?\d+$/.test(s) && s.replace('-', '').length >= 12) {
      var n = Number(s);
      return { ok: Number.isFinite(n), ms: n, text: s };
    }
    var iso = s.replace(' ', 'T');
    var t = Date.parse(iso);
    if (Number.isNaN(t)) return { ok: false, ms: null, text: s };
    return { ok: true, ms: t, text: s };
  }
  IU.parseTimeInput = parseTimeInput;

  /* 毫秒 → 本地 YYYY-MM-DD（ Overview 卡片/分区 key 可读化用）；非毫秒原样返回 null */
  function fmtDate(v) {
    var n = Number(v);
    if (!Number.isFinite(n) || n < 1e12) return null;
    var d = new Date(n);
    var p = function (x) { return String(x).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
  }
  IU.fmt.date = fmtDate;

  /* 时间输入框组件：label + input + 解析回显行（绿 ✓ / 琥珀提示）。
   * 返回 {node, input, value()}；value() 给出毫秒数或 null（空值/非法）。 */
  function timeField(labelText, placeholder, initial) {
    var parsed = h('span', { class: 'tf-parsed' });
    var input = h('input', {
      class: 'input mono', placeholder: placeholder || '2025-01-01 或 毫秒时间戳',
      autocomplete: 'off', spellcheck: 'false', value: initial || ''
    });
    function sync() {
      var s = input.value.trim();
      if (!s) { parsed.textContent = ''; parsed.className = 'tf-parsed'; return; }
      var r = parseTimeInput(s);
      if (r.ok) {
        parsed.textContent = '✓ ' + fmtTs(r.ms);
        parsed.className = 'tf-parsed ok';
      } else {
        parsed.textContent = '无法解析，试试 2025-01-05 或毫秒时间戳';
        parsed.className = 'tf-parsed bad';
      }
    }
    input.addEventListener('input', sync);
    sync();
    return {
      node: h('label', { class: 'time-field' }, labelText, input, parsed),
      input: input,
      value: function () {
        var r = parseTimeInput(input.value);
        return r.ok ? r.ms : null;
      }
    };
  }
  IU.timeField = timeField;
})();
